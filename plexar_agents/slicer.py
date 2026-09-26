"""The task builder — a long conversation into grouped, held tasks. B0bn's model half.

`sweep.py` already refuses a split that silently drops text. This module is the part it
left to "a model": it reads a transcript in overlapping ~500-word chunks, asks the rig's
qwen what work each chunk contains, turns the model's answers into spans, merges the
duplicates that chunk overlap produces, and hands the result to `sweep.review`. Nothing is
created here. `commit()` creates held tasks only from a reviewed, stored slice, which a
human looked at first.

**The model never emits offsets.** It copies short verbatim anchors (the first and last
words of a region); this module finds them in the text. An anchor that cannot be found is
not guessed into place. The candidate is recorded as `unlocatable` and dropped, and
coverage shows the hole.

**Every step is logged, and every slice is traceable.** One `slice_id` threads through:

    slice_start          the transcript: path, sha256, words, chunks, model, key source
    slice_chunk          per chunk: word range, request and response saved as artifacts
                         (hashed), latency, candidates proposed, parse errors
    slice_candidate      per candidate, including the dropped ones: its fate (kept ·
                         merged_into · unlocatable · declined · fragment), span, group
    sweep_review         (sweep.py) coverage, uncovered runs, ok
    slice_stored         the reviewed slice saved for a human to read
    slice_commit         candidate -> task id, one row per task created

A task created from a slice carries `slice_id`, `candidate_id`, `span` and `group`, so the
task is linked to its telemetry row and back to the exact text of the conversation it came from.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import pathlib
import re
import time
import urllib.error
import urllib.request
import uuid

from . import cards, ledger, model, sweep, tasks

CHUNK_WORDS = 500
OVERLAP_WORDS = 80
TIMEOUT_S = 300

PROMPT = """You are reading one chunk of a planning conversation between a person and an AI.
Find every piece of WORK the person wants done in this chunk, and every region that is NOT work
(discussion, questions answered, pleasantries, status reports).

Return ONLY a JSON object, no prose:
{"items": [
  {"kind": "task",
   "begin": "<the first 6-15 words of the region, copied EXACTLY from the chunk>",
   "end":   "<the last 6-15 words of the region, copied EXACTLY from the chunk>",
   "prompt": "<a standalone brief a coding agent could act on without this conversation: what to change, where, and how to know it is done>",
   "group": "<2-4 word label; tasks that belong in one batch share a label>"},
  {"kind": "not_a_task",
   "begin": "...", "end": "...",
   "why": "<why this region is not work>"}
]}

Rules: copy begin/end character-for-character from the chunk, never paraphrase them. Cover the
whole chunk with items, in order. One task per distinct piece of work; do not merge unrelated work.

CHUNK:
"""


# ----------------------------------------------------------------- transcript

def read_transcript(path: str) -> str:
    """Plain text, or a Claude Code session .jsonl reduced to `USER:` / `ASSISTANT:` turns."""
    p = pathlib.Path(path)
    raw = p.read_text(encoding="utf-8", errors="replace")
    if p.suffix != ".jsonl":
        return raw
    out = []
    for line in raw.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        msg = row.get("message") or {}
        role = msg.get("role") or row.get("type")
        if role not in ("user", "assistant"):
            continue
        # isMeta rows are what the harness injected, not what anyone said: an expanded
        # skill body, an image caption. Measured 2026-09-22: sliced, the /your-orchestrator
        # instruction text became "implement apply-tier" and "remove the clean phase" tasks.
        if row.get("isMeta"):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(c.get("text", "") for c in content
                             if isinstance(c, dict) and c.get("type") == "text")
        else:
            text = ""
        text = text.strip()
        if "<command-name>" in text:
            # A slash command: keep what the person typed, drop the wrapper tags.
            name = re.search(r"<command-name>(.*?)</command-name>", text, re.S)
            args = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
            text = ("%s %s" % (name.group(1).strip() if name else "",
                               args.group(1).strip() if args else "")).strip()
        if text:
            out.append("%s: %s" % (role.upper(), text))
    return "\n\n".join(out)


def latest_session(cwd: str | None = None) -> pathlib.Path:
    """The newest Claude Code transcript for this working directory."""
    cwd = os.path.abspath(cwd or os.getcwd())
    key = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    d = pathlib.Path.home() / ".claude" / "projects" / key
    files = sorted(d.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not files:
        raise FileNotFoundError("no session transcripts in %s" % d)
    return files[0]


# ----------------------------------------------------------------- chunking

def chunks(text: str, size: int = CHUNK_WORDS, overlap: int = OVERLAP_WORDS) -> list[dict]:
    """Word windows with character offsets into the ORIGINAL text, so spans stay exact."""
    words = [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]
    out, i = [], 0
    while i < len(words):
        j = min(i + size, len(words))
        out.append({"index": len(out), "w0": i, "w1": j,
                    "start": words[i][0], "end": words[j - 1][1]})
        if j == len(words):
            break
        i = j - overlap
    return out


# ----------------------------------------------------------------- the model

def rig_llm(prompt: str, headers: dict) -> str:
    """The configured model (plexar_agents.model), whoever the user chose. The name is
    kept for callers; it no longer means our rig (PLAN D59)."""
    return model.chat(prompt, headers)


def _parse(reply: str) -> list[dict]:
    reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.S)
    a, b = reply.find("{"), reply.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("no JSON object in the reply")
    items = json.loads(reply[a:b + 1]).get("items")
    if not isinstance(items, list):
        raise ValueError("reply has no items list")
    return [x for x in items if isinstance(x, dict)]


def _locate(text: str, lo: int, hi: int, begin: str, end: str) -> list[int] | None:
    """Find begin..end inside text[lo:hi]. Exact first, then whitespace-tolerant."""
    def find(needle, start):
        needle = (needle or "").strip()
        if not needle:
            return None
        k = text.find(needle, start, hi)
        if k >= 0:
            return k, k + len(needle)
        pat = r"\s+".join(re.escape(w) for w in needle.split())
        m = re.compile(pat).search(text, start, hi)
        return (m.start(), m.end()) if m else None
    b = find(begin, lo)
    if not b:
        return None
    e = find(end, b[0])
    if not e:
        return None
    return [b[0], e[1]]


CONSOLIDATE = """Below is a numbered list of tasks extracted, chunk by chunk, from ONE long
conversation. The same work was often discussed more than once, so some tasks repeat, and
group labels were chosen per chunk, so they are inconsistent.

Return ONLY a JSON object:
{"tasks": [{"id": "<id>", "group": "<consistent 2-4 word label>", "duplicate_of": "<id of the
earlier task that asks for the same work, or null>"}]}

Rules: every id appears exactly once. Use the SAME group label for tasks that belong in one
batch. Mark duplicate_of only when two tasks ask for the same outcome; a later, more specific
version of a task is a duplicate of the earlier one, pointing at the EARLIER id.

TASKS:
"""


def _consolidate(sid: str, kept: list[dict], llm) -> None:
    """One pass over the WHOLE slice: consistent groups, and duplicates across chunks.

    Span-overlap merging cannot see a task that was discussed twice, an hour apart. Measured
    on a real 5,200-word transcript: one riser-cable order appeared five times under five
    group labels. The model proposes; this function applies only well-formed answers and
    logs every change. A duplicate keeps its span, so coverage is unaffected.
    """
    if len(kept) < 2:
        return
    ids = {x["id"]: x for x in kept}
    req = CONSOLIDATE + "\n".join("%s [%s] %s" % (x["id"], x["group"] or "-", x["prompt"][:300])
                                  for x in kept)
    card = cards.write(sid, "consolidate-request", req)
    t0, err, changes = time.time(), None, []
    try:
        reply = llm(req, {"X-Plexar-Orch-Run": sid, "X-Plexar-Orch-Node": "consolidate",
                          "X-Plexar-Brief-Sha256": card["sha256"]})
        reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.S)
        a, b = reply.find("{"), reply.rfind("}")
        rows = json.loads(reply[a:b + 1]).get("tasks") if a >= 0 and b > a else None
        if not isinstance(rows, list):
            raise ValueError("no tasks list")
    except (ValueError, RuntimeError, OSError, urllib.error.URLError, KeyError) as e:
        err, reply, rows = "%s: %s" % (type(e).__name__, e), locals().get("reply", ""), []
    cards.write(sid, "consolidate-response", reply or ("ERROR " + str(err)))
    order = [x["id"] for x in kept]
    for r in rows:
        x = ids.get(r.get("id")) if isinstance(r, dict) else None
        if not x:
            continue
        g = (r.get("group") or "").strip().lower() or None
        if g and g != x["group"]:
            changes.append({"id": x["id"], "group_from": x["group"], "group_to": g})
            x["group"] = g
        d = r.get("duplicate_of")
        # Only an EARLIER, still-kept task can absorb a duplicate — no cycles, no chains
        # that delete both halves of a pair.
        if (d in ids and d != x["id"] and order.index(d) < order.index(x["id"])
                and ids[d]["fate"] == "kept"):
            x["fate"] = "duplicate_of:" + d
            changes.append({"id": x["id"], "duplicate_of": d})
    ledger.write("slice_consolidate", sid, "consolidate", tasks_in=len(kept),
                 duplicates=sum(1 for c in changes if "duplicate_of" in c),
                 regrouped=sum(1 for c in changes if "group_to" in c),
                 groups_after=len({x["group"] for x in kept if x["fate"] == "kept"}),
                 changes=changes, error=err, wall_ms=int((time.time() - t0) * 1000),
                 request_sha256=card["sha256"])


# ----------------------------------------------------------------- the slice

def slices_dir() -> pathlib.Path:
    d = tasks.dir_().parent / "slices"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def slice_text(text: str, bucket: str, source: str = "transcript", llm=None,
               source_path: str | None = None) -> dict:
    """Propose, locate, merge and review. Stores the slice; creates NO tasks."""
    llm = llm or rig_llm
    sid = "SL-%s-%s" % (datetime.date.today().strftime("%Y%m%d"), uuid.uuid4().hex[:6])
    parts = chunks(text)
    try:
        m = model.describe() if llm is rig_llm else {"model": "injected", "host": None,
                                                     "key_source": "n/a"}
    except model.NoModel as e:
        raise sweep.SweepRefused(str(e))
    ledger.write("slice_start", sid, None, bucket=bucket, source=source,
                 source_path=source_path, transcript_sha256=_sha(text), chars=len(text),
                 words=len(text.split()), chunks=len(parts), chunk_words=CHUNK_WORDS,
                 overlap_words=OVERLAP_WORDS, model=m["model"],
                 endpoint_host=m["host"], key_source=m["key_source"], capture="headers_sent_unconfirmed")

    cands = []
    for c in parts:
        chunk_text = text[c["start"]:c["end"]]
        req = PROMPT + chunk_text
        card = cards.write(sid, "chunk-%03d-request" % c["index"], req)
        t0 = time.time()
        err, items, reply = None, [], ""
        try:
            reply = llm(req, {"X-Plexar-Orch-Run": sid,
                              "X-Plexar-Orch-Node": "chunk-%03d" % c["index"],
                              "X-Plexar-Brief-Sha256": card["sha256"]})
            items = _parse(reply)
        except (ValueError, RuntimeError, OSError, urllib.error.URLError, KeyError) as e:
            err = "%s: %s" % (type(e).__name__, e)
        resp = cards.write(sid, "chunk-%03d-response" % c["index"], reply or ("ERROR " + str(err)))
        ledger.write("slice_chunk", sid, "chunk-%03d" % c["index"], words=[c["w0"], c["w1"]],
                     span=[c["start"], c["end"]], request_path=card["path"],
                     request_sha256=card["sha256"], response_path=resp["path"],
                     response_sha256=resp["sha256"], wall_ms=int((time.time() - t0) * 1000),
                     proposed=len(items), error=err)
        for k, it in enumerate(items):
            span = _locate(text, c["start"], c["end"], it.get("begin"), it.get("end"))
            cands.append({"id": "C%03d-%02d" % (c["index"], k), "chunk": c["index"],
                          "kind": "not_a_task" if it.get("kind") == "not_a_task" else "task",
                          "prompt": (it.get("prompt") or "").strip(),
                          "group": (it.get("group") or "").strip().lower() or None,
                          "why": it.get("why"), "span": span, "fate": None})

    # Merge: chunk overlap makes the same work appear twice. Same kind, spans overlapping
    # by more than half of the shorter one -> keep the longer brief, record the merge.
    located = [x for x in cands if x["span"]]
    for x in cands:
        if not x["span"]:
            x["fate"] = "unlocatable"
    located.sort(key=lambda x: (x["span"][0], -(x["span"][1] - x["span"][0])))
    kept = []
    for x in located:
        twin = None
        for y in kept:
            if y["kind"] != x["kind"]:
                continue
            ov = min(x["span"][1], y["span"][1]) - max(x["span"][0], y["span"][0])
            short = min(x["span"][1] - x["span"][0], y["span"][1] - y["span"][0])
            if short > 0 and ov > short / 2:
                twin = y
                break
        if twin is None:
            kept.append(x)
            continue
        keep, lose = (twin, x) if len(twin["prompt"]) >= len(x["prompt"]) else (x, twin)
        keep["span"] = [min(x["span"][0], twin["span"][0]), max(x["span"][1], twin["span"][1])]
        lose["fate"] = "merged_into:" + keep["id"]
        if lose is twin:
            kept[kept.index(twin)] = keep

    report = sweep.review(text, [{"prompt": x["prompt"], "span": x["span"], "kind": x["kind"],
                                  "why": x["why"]} for x in kept], run_id=sid)
    task_prompts = {t["prompt"] for t in report["tasks"]}
    for x in kept:
        if x["kind"] == "not_a_task":
            x["fate"] = "declined"
        else:
            x["fate"] = "kept" if x["prompt"] in task_prompts else "fragment"

    _consolidate(sid, [x for x in kept if x["fate"] == "kept"], llm)

    for x in cands:
        ledger.write("slice_candidate", sid, x["id"], chunk=x["chunk"], kind=x["kind"],
                     fate=x["fate"], span=x["span"], group=x["group"],
                     prompt_sha256=_sha(x["prompt"]) if x["prompt"] else None,
                     prompt_chars=len(x["prompt"]), why=x["why"])

    stored = {"slice_id": sid, "bucket": bucket, "source": source, "source_path": source_path,
              "transcript_sha256": _sha(text), "model": m["model"], "endpoint_host": m["host"],
              "key_source": m["key_source"],
              "candidates": cands, "report": report,
              "tasks": [x for x in kept if x["fate"] == "kept"]}
    path = slices_dir() / ("%s.json" % sid)
    path.write_text(json.dumps(stored, indent=2), encoding="utf-8")
    (slices_dir() / ("%s.transcript.txt" % sid)).write_text(text, encoding="utf-8")
    ledger.write("slice_stored", sid, None, path=str(path), ok=report["ok"],
                 coverage=report["coverage"], tasks=len(stored["tasks"]),
                 declined=len(report["declined"]), uncovered_runs=len(report["uncovered"]),
                 unlocatable=sum(1 for x in cands if x["fate"] == "unlocatable"),
                 merged=sum(1 for x in cands if (x["fate"] or "").startswith("merged")))
    return stored


def load(sid: str) -> dict:
    return json.loads((slices_dir() / ("%s.json" % sid)).read_text(encoding="utf-8"))


def commit(sid: str, approved_by: str, session_id: str | None = None) -> list[dict]:
    """Create held tasks from a stored slice. A person named here has read it.

    Refused, exactly as sweep.commit refuses, when the review was not ok.
    """
    s = load(sid)
    if not (approved_by or "").strip():
        raise sweep.SweepRefused("a slice is committed by a named person")
    if not s["report"].get("ok"):
        sweep.commit(s["bucket"], s["report"])       # raises SweepRefused with the reason
    made = []
    for x in s["tasks"]:
        t = tasks.create(s["bucket"], x["prompt"], source="slice:" + sid,
                         session_id=session_id,
                         meta={"slice_id": sid, "candidate_id": x["id"], "span": x["span"],
                               "group": x["group"]})
        t = tasks.hold(s["bucket"], t["id"])
        ledger.write("slice_commit", sid, x["id"], task_id=t["id"], bucket=s["bucket"],
                     group=x["group"], span=x["span"], prompt_sha256=t["prompt_sha256"],
                     approved_by=approved_by)
        made.append(t)
    return made
