"""Ask a layer up: an agent with a doubt asks instead of guessing, and the question climbs
until someone can answer it.

    worker   prints a line `QUESTION: <the question>` and stops
    verifier / approver may return the verdict "question" with a "question" field

The question goes to the next tier above whoever asked (verifier, then approver). Each is
asked to answer ONLY if the task, the repo and the task's memory make the answer clear;
otherwise it passes the question up. If no model tier can answer, the task goes back to
Held with the question open, and a person answers it on the board (POST .../answer). Every
answer is kept on the task and in its memory, and every later run's prompt carries it.

Why: a typo in a task ("Hello Wortld") was copied faithfully and passed by two reviewers,
because nothing could stop and ask. Asking is the right move whenever the intent is unclear.
"""
from __future__ import annotations

import datetime
import re
import subprocess

from . import chain

_Q = re.compile(r"^\s*(?:[●•*>-]\s*)?QUESTION:\s*(.+?)\s*$", re.I | re.M)

# What every worker is told. Kept short: it is appended to every prompt.
WORKER_RULE = (
    "If anything in this task is unclear, contradictory, or looks like a mistake (for example a "
    "likely typo), do NOT guess and do NOT silently correct it. Print one line starting with "
    "`QUESTION:` followed by your question, and stop. Someone above you will answer it.")

ANSWER = """You are being asked a question by an agent working on this task, because you sit
above it. First decide which kind of question it is.

1. A BLOCKER in the tooling around the task: the task's check command is broken (a quoting or
   escaping bug, a wrong path, a command that cannot run), the environment is missing
   something, or a framework step misbehaved. Nobody needs to be asked about these. YOU own
   them: decide, and unblock the work. You have a shell in the repository; use it to confirm.
   If the task's check command is the problem, give a corrected one in "check". It must test
   the same thing the task asks for, FAIL on the code as it was before the work, and PASS once
   the task is done. Never weaken it into something that passes anyway.

2. A question about INTENT: what the person who wrote the task meant. Answer ONLY if the task
   text, the repository and the task's memory make the answer clear. If it is not stated
   anywhere, do not guess: pass it up.

TASK:
{prompt}

THE TASK'S CHECK COMMAND:
{check}

TASK:
{prompt}

THE QUESTION (asked by the {asker}):
{question}

Answers already given on this task:
{answers}

End with ONE JSON object and nothing after it:
{{"verdict": "answer", "answer": "<the answer, one or two sentences>", "because": "<where it is clear from>",
  "check": "<ONLY for a broken check: the corrected check command; omit otherwise>"}}
or
{{"verdict": "escalate", "why": "<why you cannot answer it with certainty>"}}
"""


def find_question(text: str) -> str | None:
    """The first `QUESTION:` line an agent printed (a leading "● " or indent is allowed)."""
    m = _Q.search(text or "")
    return m.group(1).strip() if m else None


def tiers_above(asker: str, review: dict | None) -> list[str]:
    order = ["worker", "verifier", "approver"]
    have = [t for t in ("verifier", "approver") if (review or {}).get(t)]
    return [t for t in order[order.index(asker) + 1:] if t in have]


def answered_so_far(task: dict) -> str:
    rows = task.get("answers") or []
    return "\n".join("- Q: %s\n  A (%s): %s" % (a.get("question"), a.get("by"), a.get("answer"))
                     for a in rows) or "(none)"


def climb(task: dict, cfg: dict, question: str, asker: str, run) -> dict:
    """Ask each tier above `asker` in turn. `run(argv, text) -> (exit, output)`.

    Returns {"answered": True, "by": tier, "model": ..., "answer": ...} or
    {"answered": False, "tried": [{tier, model, why}]}: then a person must answer.
    """
    review = cfg.get("review") or {}
    tried = []
    for tier in tiers_above(asker, review):
        argv = list(review[tier])
        rc, out = run(argv, ANSWER.format(prompt=task["prompt"], asker=asker, question=question,
                                          check=task.get("check") or "(none)",
                                          answers=answered_so_far(task)))
        v = chain.parse_verdict(out, {"answer", "escalate"})
        model = chain.model_of(argv)
        if v.get("verdict") == "answer" and str(v.get("answer") or "").strip():
            fix = str(v.get("check") or "").strip() or None
            return {"answered": True, "by": tier, "model": model, "answer": v["answer"].strip(),
                    "because": v.get("because"), "check": fix, "tried": tried}
        tried.append({"tier": tier, "model": model, "why": v.get("why") or v.get("judgement") or "no answer"})
    return {"answered": False, "tried": tried}


def run_text(argv: list[str], text: str, cwd: str, log, env=None, timeout: int = 1800):
    """Run a tier's command with `text` on stdin; append to the run log; return (exit, output)."""
    with open(log, "ab") as fh:
        fh.write(("\n===== question to %s =====\n$ %s\n" % (chain.model_of(argv), " ".join(argv))).encode("utf-8"))
    try:
        r = subprocess.run(argv, cwd=cwd, input=text.encode("utf-8"), capture_output=True,
                           timeout=timeout, env=env)
        out = r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")
        rc = r.returncode
    except (OSError, subprocess.TimeoutExpired) as e:
        out, rc = "could not ask: %s" % e, 127
    with open(log, "ab") as fh:
        fh.write(out.encode("utf-8"))
    return rc, out


def now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")
