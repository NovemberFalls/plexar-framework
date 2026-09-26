"""The task builder: chunked proposals -> located spans -> merged -> reviewed -> held.

A fake model stands in for the rig. What is under test is everything the runtime does
around the model: anchors become spans or are dropped, overlap duplicates merge, nothing
is created before a named commit, and every candidate is in the ledger.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plexar_agents import slicer, sweep, tasks  # noqa: E402

LOGIN = ("Please fix the login redirect loop on mobile because it bounces users twice "
         "before landing on the dashboard and support is getting tickets about it daily")
PDF = ("Also make the invoice PDF stop dropping the EU tax line when the customer has a "
       "VAT number on file, finance flagged three invoices last week")
CHAT = "thanks that all sounds good to me for now"


def text():
    filler = " ".join("word%d" % i for i in range(700))    # forces several chunks
    return "%s. %s. %s. %s" % (LOGIN, filler, PDF, CHAT)


def fake(req, headers):
    """Answers only about what is actually in the chunk it was given, like a real model."""
    if "TASKS:\n" in req:                       # the consolidation pass: keep groups as-is
        ids = [l.split(" ", 1)[0] for l in req.split("TASKS:\n", 1)[1].splitlines() if l]
        return json.dumps({"tasks": [{"id": i, "group": None, "duplicate_of": None}
                                     for i in ids]})
    chunk = req.split("CHUNK:\n", 1)[1]
    items = []
    if "login redirect" in chunk:
        items.append({"kind": "task", "begin": "Please fix the login redirect",
                      "end": "tickets about it daily", "group": "Auth",
                      "prompt": "Fix the mobile login redirect loop that bounces users twice "
                                "before the dashboard; done when one redirect lands them."})
    if "word100 " in chunk or "word400 " in chunk or "word650 " in chunk:
        ws = [w for w in chunk.split() if w.startswith("word")]
        items.append({"kind": "not_a_task", "begin": ws[0], "end": ws[-1].rstrip("."),
                      "why": "filler"})
    if "invoice PDF" in chunk:
        items.append({"kind": "task", "begin": "Also make the invoice PDF",
                      "end": "three invoices last week", "group": "billing",
                      "prompt": "Make the invoice PDF keep the EU tax line when the customer "
                                "has a VAT number; done when the three flagged invoices render it."})
        items.append({"kind": "task", "begin": "this anchor is invented", "end": "nowhere",
                      "prompt": "a hallucinated task that quotes text that is not there at all"})
    if CHAT in chunk:
        items.append({"kind": "not_a_task", "begin": "thanks that all", "end": "good to me for now",
                      "why": "sign-off"})
    return json.dumps({"items": items})


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLEXAR_AGENTS_STATE", str(tmp_path / "state" / "runs"))
    monkeypatch.setenv("PLEXAR_AGENTS_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("PLEXAR_AGENTS_ARTIFACTS", str(tmp_path / "artifacts"))
    return tmp_path


def rows(tmp):
    out = []
    for f in tmp.rglob("*.jsonl"):
        out += [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
    return out


def test_chunks_overlap_and_keep_exact_offsets():
    t = text()
    cs = slicer.chunks(t)
    assert len(cs) >= 2 and cs[1]["w0"] == cs[0]["w1"] - slicer.OVERLAP_WORDS
    assert cs[0]["start"] == 0 and cs[-1]["end"] == len(t.rstrip())


def test_slice_proposes_but_creates_nothing(env):
    s = slicer.slice_text(text(), "b", llm=fake)
    assert tasks.all_("b") == {}
    fates = {c["fate"] for c in s["candidates"]}
    assert "unlocatable" in fates                        # the invented anchor was not guessed in
    kept = s["tasks"]
    assert sorted(k["group"] for k in kept) == ["auth", "billing"]
    assert s["report"]["ok"], s["report"]


def test_every_candidate_is_in_the_ledger_with_its_fate(env):
    s = slicer.slice_text(text(), "b", llm=fake)
    rs = [r for r in rows(env) if r.get("run_id") == s["slice_id"]]
    cand = [r for r in rs if r["event"] == "slice_candidate"]
    assert len(cand) == len(s["candidates"])
    assert all(r["fate"] for r in cand)
    assert sum(1 for r in rs if r["event"] == "slice_chunk") == len(slicer.chunks(text()))
    assert any(r["event"] == "slice_start" and r["transcript_sha256"] for r in rs)
    assert any(r["event"] == "sweep_review" for r in rs)          # joinable by slice_id


def test_commit_needs_a_name_and_links_task_to_slice(env):
    s = slicer.slice_text(text(), "b", llm=fake)
    with pytest.raises(sweep.SweepRefused):
        slicer.commit(s["slice_id"], "")
    made = slicer.commit(s["slice_id"], "len")
    assert len(made) == 2 and all(t["state"] == "held" for t in made)
    t = made[0]
    assert t["slice_id"] == s["slice_id"] and t["candidate_id"] and t["span"]
    src = (slicer.slices_dir() / ("%s.transcript.txt" % s["slice_id"])).read_text(encoding="utf-8")
    assert src[t["span"][0]:t["span"][1]].startswith(("Please fix", "Also make"))
    commits = [r for r in rows(env) if r.get("event") == "slice_commit"]
    assert {r["task_id"] for r in commits} == {m["id"] for m in made}


def test_a_slice_that_drops_text_cannot_be_committed(env):
    def lazy(req, headers):                              # only ever sees the login task
        if "TASKS:\n" in req:
            return fake(req, headers)
        chunk = req.split("CHUNK:\n", 1)[1]
        if "login redirect" not in chunk:
            return json.dumps({"items": []})
        return fake(req, headers)
    s = slicer.slice_text(text(), "b", llm=lazy)
    assert not s["report"]["ok"] and s["report"]["uncovered"]
    with pytest.raises(sweep.SweepRefused):
        slicer.commit(s["slice_id"], "len")
    assert tasks.all_("b") == {}


def test_a_broken_reply_is_logged_not_fatal(env):
    s = slicer.slice_text(text(), "b", llm=lambda r, h: "I am not JSON")
    errs = [r for r in rows(env) if r.get("event") == "slice_chunk" and r.get("error")]
    assert errs and not s["report"]["ok"]


def test_slices_are_visible_over_http_and_never_committable_there(env):
    from fastapi.testclient import TestClient
    sys.path.insert(0, str(ROOT / "docs"))
    import app as app_mod
    c = TestClient(app_mod.app)
    s = slicer.slice_text(text(), "b", llm=fake)
    lst = c.get("/api/slices").json()
    assert lst[0]["slice_id"] == s["slice_id"] and lst[0]["fates"]["unlocatable"] == 1
    assert lst[0]["committed"] is False
    full = c.get("/api/slices/%s" % s["slice_id"]).json()
    assert len(full["candidates"]) == len(s["candidates"])
    assert c.get("/api/slices/..%2Fdaemon").status_code in (400, 404)
    assert c.post("/api/slices/%s" % s["slice_id"]).status_code == 405


def _ids(req):
    return [l.split(" ", 1)[0] for l in req.split("TASKS:\n", 1)[1].splitlines() if l]


def test_consolidation_marks_a_later_duplicate_and_regroups(env):
    def dup(req, headers):
        if "TASKS:\n" not in req:
            return fake(req, headers)
        ids = _ids(req)
        return json.dumps({"tasks": [{"id": ids[0], "group": "Work", "duplicate_of": None},
                                     {"id": ids[1], "group": "work", "duplicate_of": ids[0]},
                                     {"id": "C999-99", "group": "x", "duplicate_of": ids[1]}]})
    s = slicer.slice_text(text(), "b", llm=dup)
    assert len(s["tasks"]) == 1 and s["tasks"][0]["group"] == "work"
    assert any((c["fate"] or "").startswith("duplicate_of:") for c in s["candidates"])
    assert s["report"]["ok"]                              # a duplicate keeps its span covered
    row = [r for r in rows(env) if r.get("event") == "slice_consolidate"][0]
    assert row["duplicates"] == 1 and row["groups_after"] == 1


def test_a_consolidation_that_points_forward_is_ignored(env):
    def fwd(req, headers):
        if "TASKS:\n" not in req:
            return fake(req, headers)
        ids = _ids(req)
        return json.dumps({"tasks": [{"id": ids[0], "duplicate_of": ids[1]}]})
    s = slicer.slice_text(text(), "b", llm=fwd)
    assert len(s["tasks"]) == 2


def test_injected_skill_text_is_not_sliced(tmp_path):
    p = tmp_path / "s.jsonl"
    rs = [{"type": "user", "message": {"role": "user", "content":
           "<command-message>x</command-message>\n<command-name>/plan-skill</command-name>\n"
           "<command-args>add a login page</command-args>"}},
          {"type": "user", "isMeta": True, "message": {"role": "user", "content":
           "# /plan-skill skill body: implement the apply-tier and remove the clean phase"}},
          {"type": "assistant", "message": {"role": "assistant", "content":
           [{"type": "text", "text": "Sure."}]}}]
    p.write_text("\n".join(json.dumps(r) for r in rs), encoding="utf-8")
    assert slicer.read_transcript(str(p)) == "USER: /plan-skill add a login page\n\nASSISTANT: Sure."
