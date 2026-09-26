"""The review chain: a worker's DONE is a claim, and each claim goes one tier up (PLAN D60).

    worker    does the work on the task branch. It cannot execute; it reports DONE.
    verifier  one tier up. EXECUTES the work, writes QA cases with the observed output,
              and returns pass | fail with a judgement. fail -> feedback to the worker, loop.
    approver  the orchestrator's tier. Reads the verifier's evidence and returns approve |
              reject with a reason ("the colour is wrong"). reject -> the verifier's relay
              of that reason goes to the worker, loop.
    human     the QA file in the repo's qa/ folder, and the merge verdict in /app.

**There is no silver bullet for hallucination, only more honest passes.** Every hop is a
`review` row (schema: ledger_schema.json), so the data answers "how often does this tier's
pass survive the tier above, and then the human" per model. That approval accuracy is
what picks which model approves.

**Verdicts are data, not prose.** Each reviewer ends its answer with ONE JSON object. A
reviewer whose reply has no parseable verdict is recorded as `error` and counts as a FAIL:
an unreadable approval is not an approval.

Loops are capped at `max_loops` (default 3, the §4.8 ladder). After that the task is
LADDER_EXHAUSTED and goes to the human with everything each tier said.
"""
from __future__ import annotations

import datetime
import json
import os
import re

VERIFY = """You are the VERIFIER, one tier above the worker that just finished this task.
The worker cannot run code; you can. Your job is to find out, by EXECUTING it, whether the
work actually does what the task asked. Do not trust the worker's summary, and do not fix
anything yourself: judge and report.

TASK (what was asked):
{prompt}

GATE: `{gate}` exited {gate_exit}.
{gate_tail}

THE WORKER'S CHANGE (attempt {attempt}):
{diff}

Do this:
1. Run whatever demonstrates the task is done (the script, the command, the test). Record
   each command and what it actually printed.
2. Write QA cases, each with a CLASS:
   - "AUTO": you proved it by running a command. Give the exact command in "how" and what
     it printed in "observed". Backend behaviour, CLIs, APIs, tests, files: these are AUTO.
   - "VISUAL": it has to be SEEN or USED in a UI (a page, a colour, a layout, a button, a
     form, an upload). A screenshot of the page loading is NOT enough: run the GAUNTLET
     the task needs. Write a Playwright driver script and run it:
         qa/evidence/{task_id}/drive_<name>.py    (python, playwright.sync_api, headless)
     In it, do what a user would do to prove the task: really CLICK the buttons, TYPE into
     inputs, UPLOAD files (set_input_files), SUBMIT forms, wait for the result, and ASSERT
     on what changed (text, state, network response, stored data). Screenshot BEFORE and
     AFTER each meaningful action into qa/evidence/{task_id}/. Exit non-zero if any assert
     fails. Then OPEN THE SCREENSHOTS and look at them with your image-reading tool.
     ALWAYS show the change itself, before and after: check out the code as it was before
     this task (`git worktree add <a temp dir outside the repo> {base_sha}`), capture the
     same view there as qa/evidence/{task_id}/00_before_task.png, and the current state as
     qa/evidence/{task_id}/99_after_task.png. Then remove that temp worktree.
     Every screenshot must show a DIFFERENT state that proves something; never save two
     shots of the same state, and never name a shot "before" if it shows the finished work.
     When the proof is not visible in one frame (it survives a reload, it saves to disk),
     make it visible: e.g. add a note, RELOAD, and shoot the page after the reload. Name
     each shot for what it shows (01_added_two_notes.png, 02_after_reload.png) and write
     qa/evidence/{task_id}/captions.json: {{"<file name>": "<one line: what this shows>"}}.
     Start the app yourself on a FREE port with throwaway state (never use or stop a
     service that is already running), and stop it when done.
     Put the driver command in "how", the main screenshot in "screenshot", what you saw
     and what the asserts proved in "observed", and your own "ai_verdict" (pass|fail).
     Save evidence ONLY under qa/evidence/{task_id}/: everything else you create is
     discarded. The driver script is evidence too: a person must be able to re-run it.
   - "MANUAL": only when you genuinely could not boot or capture it. Say why in
     "observed", and give exactly how a person starts it and what they should see.
   Do not mark something VISUAL or MANUAL that a command could prove.
3. Decide: pass if the work does what the TASK says (not what the worker claims), else fail.
   If the TASK itself looks wrong or unclear (a likely typo, a contradiction, two readings),
   do not pass it literally and do not correct it yourself: return "question" and put the
   question in "question". It goes up to someone who can answer it.

End your answer with ONE JSON object, and nothing after it:
{{"verdict": "pass" | "fail" | "question",
  "question": "<only for question: what you need answered>",
  "judgement": "<one or two sentences: why>",
  "evidence": [{{"cmd": "<what you ran>", "observed": "<what it printed, trimmed>"}}],
  "qa_cases": [{{"id": "QA-1", "class": "AUTO" | "VISUAL" | "MANUAL", "section": "<area>",
                "test": "<what is tested>", "how": "<the command, or how to boot it and where to look>",
                "expected": "<result that means pass>", "observed": "<what you saw>",
                "screenshot": "<qa/evidence/... path, VISUAL only>", "ai_verdict": "pass" | "fail"}}],
  "feedback": "<if fail: exactly what the worker must change; else empty>"}}
"""

APPROVE = """You are the APPROVER: the orchestrator's tier. A worker did a task and a
verifier checked it by executing it. Decide whether this is really done. You are the last
model before a human; the question is whether the TASK is satisfied, not whether the
verifier sounds confident. Look for what the verifier might have missed.

TASK (what was asked):
{prompt}

THE CHANGE:
{diff}

THE VERIFIER'S REPORT (verdict {v_verdict}):
judgement: {v_judgement}
evidence:
{v_evidence}

You may run commands to check. **Only claim what you actually did:** anything you say you
ran must appear in "evidence" with its real output. A claim with no evidence entry is
treated as not done. (Measured 2026-09-23: an approver without a shell wrote "I ran it
myself"; that is exactly the failure this chain exists to catch.)

End your answer with ONE JSON object, and nothing after it:
{{"verdict": "approve" | "reject" | "question",
  "question": "<only for question: if the TASK looks wrong or unclear, ask instead of approving it literally>",
  "judgement": "<one or two sentences: why>",
  "evidence": [{{"cmd": "<what you ran, if anything>", "observed": "<its real output>"}}],
  "feedback": "<if reject: the specific thing that is wrong, for the worker; else empty>"}}
"""

WORKER_FEEDBACK = """

---
REVIEW FEEDBACK — attempt {attempt} was not accepted by the {stage}:
{feedback}
Fix exactly this. Your earlier work is already on this branch.
"""


def model_of(argv: list[str]) -> str:
    """The model a runner command names (`--model X`), else its program name."""
    for i, a in enumerate(argv):
        if a == "--model" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--model="):
            return a.split("=", 1)[1]
    return os.path.basename(argv[0]) if argv else "unknown"


def parse_verdict(text: str, allowed: set[str]) -> dict:
    """The LAST JSON object in `text` that carries an allowed verdict, else an error verdict."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    starts = [m.start() for m in re.finditer(r"\{", text)]
    dec = json.JSONDecoder()
    for s in reversed(starts):
        try:
            obj, _ = dec.raw_decode(text[s:])
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("verdict") in allowed:
            return obj
    return {"verdict": "error", "judgement": "no parseable verdict in the reply",
            "feedback": "", "evidence": [], "qa_cases": []}


def _cell(s) -> str:
    return str(s or "").replace("|", "\\|").replace("\r", " ").replace("\n", "<br>")[:400]


NOTICE = (
    "> **About this report.** The review chain ran, checked and photographed this work to "
    "get you through QA faster, and it is usually right. It is still AI judgement, and it "
    "is not a replacement for yours: results depend on how the task was prompted and on "
    "your project, and what works reliably for us may not for you. **Treat every AI "
    "verdict below as a lead to confirm, not a sign-off.** Your verdicts are recorded, and "
    "they are what makes the next report more accurate.")


def classify(cases: list[dict]) -> dict:
    out = {"AUTO": [], "VISUAL": [], "MANUAL": []}
    for c in cases:
        k = str(c.get("class", "")).upper()
        out[k if k in out else "MANUAL"].append(c)   # unclassed -> a person looks (safe side)
    return out


def qa_markdown(task: dict, hops: list[dict], final: str, commit: str | None) -> str:
    """The human's file: the house QA_PLAN table shape, plus the chain that produced it.

    Three kinds of case, in the order a person should spend attention:
      VISUAL  the verifier booted it, took a screenshot and judged it. The picture is here;
              you confirm what it claims to see.
      MANUAL  the verifier could not capture it. Boot it and look.
      AUTO    proven by a re-runnable command. Backend smoke; the chain's evidence stands,
              and you may spot-check it.
    Every case shows the AI's verdict separately from YOUR status: they are different
    facts, and the gap between them is the verification-accuracy label (D61).
    """
    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    cases = []
    for h in hops:
        if h["stage"] == "verifier" and h.get("qa_cases"):
            cases = h["qa_cases"]           # the latest verifier's cases win
    k = classify(cases)
    header = ["| ID | Section | Test | Class | AI verdict | Your status | What the AI observed | Commit | What & how to test |",
              "|---|---|---|---|---|---|---|---|---|"]

    def row(c, status):
        return "| %s | %s | %s | %s | %s | %s | %s | %s | %s — expect: %s |" % (
            _cell(c.get("id")), _cell(c.get("section")), _cell(c.get("test")),
            str(c.get("class", "MANUAL")).upper(), _cell(c.get("ai_verdict") or "—"), status,
            _cell(c.get("observed")), (commit or "")[:10], _cell(c.get("how")),
            _cell(c.get("expected")))

    lines = [
        "# QA — %s" % task["id"], "", NOTICE, "",
        "Generated %s by the review chain." % now, "",
        "**Task:** %s" % task["prompt"].strip().replace("\n", " ")[:600], "",
        "**Branch:** `%s` · **commit:** `%s` · **chain result:** `%s`"
        % (task.get("branch") or "plexar/%s" % task["id"], (commit or "-")[:12], final), "",
        "Record your verdict per case in `/app` (open the task, then *QA cases*), or edit "
        "*Your status* here.", "",
        "## 1 · Seen by the AI: confirm what it claims to see (VISUAL)", "",
    ]
    if k["VISUAL"]:
        lines += header + [row(c, "PENDING") for c in k["VISUAL"]] + [""]
        for c in k["VISUAL"]:
            if c.get("screenshot"):
                rel = str(c["screenshot"]).replace("\\", "/")
                if rel.startswith("qa/"):
                    rel = rel[3:]           # this file lives in qa/
                lines += ["**%s** — %s" % (_cell(c.get("id")), _cell(c.get("test"))), "",
                          "![%s](%s)" % (_cell(c.get("id")), rel), ""]
    else:
        lines += ["None.", ""]
    lines += ["## 2 · Needs your eyes: the AI could not capture it (MANUAL)", ""]
    lines += (header + [row(c, "PENDING") for c in k["MANUAL"]] + [""]) if k["MANUAL"] else ["None.", ""]
    lines += ["## 3 · Proven by a command (AUTO: backend smoke)", "",
              "Re-runnable; these belong in the larger QA as regression checks. Spot-check "
              "any you doubt.", ""]
    if k["AUTO"]:
        st = "VERIFIED (chain)" if final == "approved" else "UNCONFIRMED"
        lines += header + [row(c, st) for c in k["AUTO"]]
    else:
        lines.append("None.")
    if not cases:
        lines += ["", "**The verifier wrote no cases: NOT RUN. This cannot count as verified.**"]
    lines += ["", "## The chain, hop by hop", "",
              "| hop | attempt | stage | model | verdict | judgement | feedback |",
              "|---|---|---|---|---|---|---|"]
    for h in hops:
        lines.append("| %d | %d | %s | %s | **%s** | %s | %s |" % (
            h["hop"], h["attempt"], h["stage"], _cell(h["reviewer_model"]), h["verdict"],
            _cell(h.get("judgement")), _cell(h.get("feedback"))))
    ev = [(h["hop"], h["stage"], e) for h in hops for e in (h.get("evidence") or [])
          if isinstance(e, dict)]
    if ev:
        lines += ["", "## Evidence the reviewers recorded", ""]
        for hop, stage, e in ev:
            lines += ["**hop %d (%s)** `%s`" % (hop, stage, str(e.get("cmd", ""))[:200]), "",
                      "```", str(e.get("observed", ""))[:1500], "```", ""]
    return "\n".join(lines) + "\n"
