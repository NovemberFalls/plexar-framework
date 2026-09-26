"""Plans — a frontier planner turns a goal into tasks with lanes, dependencies and checks.

    plans.create(bucket, goal, by)          planner runs; plan is `questions`, `draft` or `failed`
    plans.answer(bucket, pid, [...], by)    answers its questions; the planner runs again
    plans.approve(bucket, pid, by)          ONCE, by a named person; its tasks go DRAFTED -> HELD
    plans.reject(bucket, pid, by, note)     no; its tasks are cancelled

After approval the sweeper (sweeper.py) runs the plan without asking again: each task's own
check, the bucket gate and the review chain still guard every task. Passing tasks merge into
the plan branch `plexar/plan/<id>`, never into the person's base branch.

The planner is a configured command (bucket cfg "planner", default `claude -p`): the framework
stays agent-agnostic. Its prompt goes on stdin; its reply ends with ONE JSON object, or it
prints `QUESTION:` lines when the goal is unclear (ask.py) instead of guessing.
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
import subprocess
import time
import uuid

from . import ask, ledger, store, tasks

LANES = ("critical", "workhorse", "mundane")
DEFAULT_PLANNER = ["claude", "-p"]
PLANNER_TIMEOUT_S = 3600      # UNMEASURED. A frontier planner reading a repo takes minutes.

PLAN = """You are the PLANNER. Turn the goal below into a small set of tasks that other agents
will do, one at a time, in this repository (you are running in its root).

Rules:
- Each task's prompt must STAND ALONE: the agent doing it sees only that prompt and the repo.
- Each task has a CHECK: one shell command, run from the repo root, that FAILS now and PASSES
  once that task is done. A check that already passes proves nothing and the task will stop.
- Each task has a TITLE: a few plain words a person scans on a board ("Notes survive a reload"),
  not the prompt's opening.
- Prefer small tasks. A task that depends on another lists that task's key in "deps"; it will
  run on top of the finished work of its deps.
- lane is one of critical | workhorse | mundane. Use critical ONLY for shared contracts, auth,
  money, concurrency or anything destructive; workhorse for ordinary features; mundane for
  small mechanical edits.
- If the goal is unclear or contradictory, do NOT guess: print one or more lines starting with
  `QUESTION:` and stop. You will be run again with the answers.

End your reply with ONE JSON object and nothing after it:
{{"tasks": [{{"key": "t1", "title": "...", "prompt": "...", "lane": "workhorse", "deps": [], "check": "<cmd>"}},
            {{"key": "t2", "title": "...", "prompt": "...", "lane": "mundane", "deps": ["t1"], "check": "<cmd>"}}]}}

GOAL:
{goal}
{answers}"""


class PlanError(RuntimeError):
    """An illegal move on a plan: unknown id, wrong status, a missing note."""


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


# ----------------------------------------------------------------- store
#
# One JSON file per bucket, plan id -> plan. Same lock + atomic replace as the task store.

def _path(bucket: str) -> pathlib.Path:
    from . import paths
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in bucket)[:80]
    return paths.home() / "plans" / ("%s.json" % safe)


def _read(bucket: str) -> dict:
    p = _path(bucket)
    for _ in range(store.SHARING_RETRIES):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except PermissionError:           # Windows: mid-replace by another writer
            time.sleep(store.LOCK_POLL_S)
        except ValueError:
            return {}
    raise store.LockTimeout("could not read %s: held open by another process" % p)


def _update(bucket: str, fn) -> dict:
    """Read-modify-write the bucket's plans under the lock. Returns what was written."""
    p = _path(bucket)
    p.parent.mkdir(parents=True, exist_ok=True)
    with store._Lock(p):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = {}
        doc = fn(doc)
        tmp = p.with_suffix(".tmp.%d" % os.getpid())
        tmp.write_text(json.dumps(doc), encoding="utf-8", newline="\n")
        store._replace(tmp, p)
        return doc


def save(bucket: str, plan_id: str, **fields) -> dict:
    """Change fields of one plan, atomically. Raises PlanError for an unknown id."""
    out = {}

    def fn(doc):
        if plan_id not in doc:
            out["err"] = PlanError("no plan %s in bucket %s" % (plan_id, bucket))
            return doc
        doc[plan_id] = {**doc[plan_id], **fields}
        out["plan"] = doc[plan_id]
        return doc
    _update(bucket, fn)
    if "err" in out:
        raise out["err"]
    return out["plan"]


def get(bucket: str, plan_id: str) -> dict | None:
    return _read(bucket).get(plan_id)


def all_(bucket: str) -> list[dict]:
    return sorted(_read(bucket).values(), key=lambda p: p.get("created") or "", reverse=True)


def _must(bucket: str, plan_id: str, *statuses: str) -> dict:
    p = get(bucket, plan_id)
    if p is None:
        raise PlanError("no plan %s in bucket %s" % (plan_id, bucket))
    if statuses and p["status"] not in statuses:
        raise PlanError("plan %s is %s; this needs it %s" % (plan_id, p["status"], " or ".join(statuses)))
    return p


# ----------------------------------------------------------------- the planner

def _bucket_cfg(bucket: str) -> dict:
    from . import daemon
    return daemon.load_config().get(bucket) or {}


def _planner_argv(bucket: str) -> list[str]:
    from . import daemon
    p = _bucket_cfg(bucket).get("planner")
    if isinstance(p, str) and p.strip():
        return daemon._argv(p)
    return list(p) if isinstance(p, list) and p else list(DEFAULT_PLANNER)


def last_json(text: str) -> dict | None:
    """The LAST JSON object in `text` carrying "tasks" (chain.parse_verdict's logic; a task
    object nested inside it is also a JSON object, so the key is what picks the outer one)."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    dec = json.JSONDecoder()
    for s in reversed([m.start() for m in re.finditer(r"\{", text)]):
        try:
            obj, _ = dec.raw_decode(text[s:])
        except ValueError:
            continue
        if isinstance(obj, dict) and "tasks" in obj:
            return obj
    return None


def parse_tasks(obj) -> list[dict]:
    """Validate the planner's tasks. Returns them in dependency order; raises ValueError."""
    if not isinstance(obj, dict) or not isinstance(obj.get("tasks"), list) or not obj["tasks"]:
        raise ValueError("the reply has no non-empty \"tasks\" list")
    rows, keys = [], set()
    for i, t in enumerate(obj["tasks"]):
        if not isinstance(t, dict):
            raise ValueError("task %d is not an object" % i)
        key = str(t.get("key") or "").strip()
        prompt = str(t.get("prompt") or "").strip()
        lane = t.get("lane") or "workhorse"
        deps = t.get("deps") or []
        check = t.get("check")
        title = " ".join(str(t.get("title") or "").split())[:80] or None
        if not key or key in keys:
            raise ValueError("task %d: missing or duplicate key %r" % (i, key))
        if not prompt:
            raise ValueError("task %s has no prompt" % key)
        if lane not in LANES:
            raise ValueError("task %s: lane %r is not one of %s" % (key, lane, ", ".join(LANES)))
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            raise ValueError("task %s: deps must be a list of keys" % key)
        if check is not None and not (isinstance(check, str) and check.strip()):
            raise ValueError("task %s: check must be a command string" % key)
        keys.add(key)
        rows.append({"key": key, "title": title, "prompt": prompt, "lane": lane, "deps": list(dict.fromkeys(deps)),
                     "check": check.strip() if check else None})
    for r in rows:
        unknown = [d for d in r["deps"] if d not in keys]
        if unknown:
            raise ValueError("task %s depends on unknown key(s): %s" % (r["key"], ", ".join(unknown)))
    # Kahn: whatever cannot be ordered is on a cycle.
    order, done, left = [], set(), list(rows)
    while left:
        ready = [r for r in left if all(d in done for d in r["deps"])]
        if not ready:
            raise ValueError("dependency cycle among: %s" % ", ".join(r["key"] for r in left))
        for r in ready:
            order.append(r)
            done.add(r["key"])
        left = [r for r in left if r["key"] not in done]
    return order


def _qa_block(questions: list[dict]) -> str:
    done = [q for q in questions if q.get("answer")]
    if not done:
        return ""
    return "\nYou asked questions before; here are the answers:\n" + "\n".join(
        "- Q: %s\n  A (%s): %s" % (q["question"], q.get("by"), q["answer"]) for q in done) + "\n"


def start(bucket: str, goal: str, by: str, planner: list[str] | None = None) -> dict:
    """Store a plan in status `planning`. No model call (run_planner does that)."""
    goal = (goal or "").strip()
    if not goal:
        raise PlanError("a plan needs a goal")
    pid = "P-" + uuid.uuid4().hex[:10]
    plan = {"id": pid, "bucket": bucket, "goal": goal, "created": _now(), "by": by,
            "status": "planning", "planner": list(planner) if planner else _planner_argv(bucket),
            "planner_output_tail": "", "questions": [], "tasks": [],
            "branch": "plexar/plan/%s" % pid, "base": None,
            "approved_by": None, "approved_at": None, "rejected": None,
            "batches": [], "final": None, "error": None}
    _update(bucket, lambda doc: {**doc, pid: plan})
    ledger.write("plan_created", pid, None, bucket=bucket, goal_chars=len(goal), by=by,
                 planner=plan["planner"])
    return plan


def run_planner(bucket: str, plan_id: str) -> dict:
    """Run the planner for a `planning` plan; move it to questions, draft or failed."""
    plan = _must(bucket, plan_id, "planning")
    cwd = _bucket_cfg(bucket).get("cwd") or None      # unconfigured bucket: the current dir
    text = PLAN.format(goal=plan["goal"], answers=_qa_block(plan["questions"]))
    try:
        r = subprocess.run(plan["planner"], cwd=cwd, input=text.encode("utf-8"),
                           capture_output=True, timeout=PLANNER_TIMEOUT_S)
        out = r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")
        rc = r.returncode
    except (OSError, subprocess.TimeoutExpired) as e:
        out, rc = "could not run the planner: %s" % e, 127
    tail = out[-4000:]
    asked = [m.group(1).strip() for m in ask._Q.finditer(out)]
    if asked:
        qs = plan["questions"] + [{"question": q, "answer": None, "by": None} for q in asked]
        plan = save(bucket, plan_id, status="questions", questions=qs, planner_output_tail=tail)
        ledger.write("plan_questions", plan_id, None, bucket=bucket, questions=asked, planner_exit=rc)
        return plan
    try:
        rows = parse_tasks(last_json(out))
    except ValueError as e:
        why = "the planner's reply was unusable (exit %s): %s" % (rc, e)
        plan = save(bucket, plan_id, status="failed", error=why, planner_output_tail=tail)
        ledger.write("plan_failed", plan_id, None, bucket=bucket, error=why, planner_exit=rc)
        return plan
    # Dependency order, so every dep already has its task id when a task is created.
    ids: dict[str, str] = {}
    made = {}
    for r in rows:
        deps = [ids[d] for d in r["deps"]]
        t = tasks.create(bucket, r["prompt"], source="plan", meta={
            "plan_id": plan_id, "plan_key": r["key"], "lane": r["lane"], "deps": deps,
            "check": r["check"], "title": r["title"]})
        ids[r["key"]] = t["id"]
        made[r["key"]] = {"key": r["key"], "title": r["title"], "task_id": t["id"], "prompt": r["prompt"],
                          "lane": r["lane"], "deps": deps, "check": r["check"], "attempts": 0}
    # Stored in the order the planner wrote them.
    listed = [made[k] for k in [t["key"] for t in last_json(out)["tasks"]]]
    plan = save(bucket, plan_id, status="draft", tasks=listed, planner_output_tail=tail, error=None)
    ledger.write("plan_drafted", plan_id, None, bucket=bucket, tasks=len(listed),
                 lanes=[t["lane"] for t in listed], edges=sum(len(t["deps"]) for t in listed),
                 checks=sum(1 for t in listed if t["check"]), planner_exit=rc)
    return plan


def create(bucket: str, goal: str, by: str, planner: list[str] | None = None) -> dict:
    """Start a plan and run the planner on it, synchronously."""
    return run_planner(bucket, start(bucket, goal, by, planner)["id"])


def answer(bucket: str, plan_id: str, answers: list[str], by: str) -> dict:
    """Fill the open questions in order, then run the planner again with the Q&A."""
    plan = _must(bucket, plan_id, "questions")
    given = [str(a).strip() for a in (answers or []) if str(a or "").strip()]
    if not given:
        raise PlanError("no answers given")
    qs, it = [dict(q) for q in plan["questions"]], iter(given)
    for q in qs:
        if q.get("answer") is None:
            a = next(it, None)
            if a is None:
                break
            q.update(answer=a, by=by)
    save(bucket, plan_id, questions=qs, status="planning")
    ledger.write("plan_answered", plan_id, None, bucket=bucket, by=by, answers=len(given))
    return run_planner(bucket, plan_id)


def approve(bucket: str, plan_id: str, by: str) -> dict:
    """The one approval. Its tasks go DRAFTED -> HELD; the sweeper takes it from here."""
    if not (by or "").strip():
        raise PlanError("an approval must name who gave it")
    plan = _must(bucket, plan_id, "draft")
    for t in plan["tasks"]:
        tasks.hold(bucket, t["task_id"])
    plan = save(bucket, plan_id, status="approved", approved_by=by.strip(), approved_at=_now())
    ledger.write("plan_approved", plan_id, None, bucket=bucket, by=by.strip(),
                 tasks=[t["task_id"] for t in plan["tasks"]])
    return plan


def reject(bucket: str, plan_id: str, by: str, note: str) -> dict:
    if not (note or "").strip():
        raise PlanError("a rejection needs a note saying why")
    plan = _must(bucket, plan_id, "draft", "questions")
    for t in plan["tasks"]:
        if (tasks.get(bucket, t["task_id"]) or {}).get("state") not in tasks.TERMINAL:
            tasks.transition(bucket, t["task_id"], tasks.CANCELLED)
    plan = save(bucket, plan_id, status="rejected", rejected={"by": by, "note": note.strip()})
    ledger.write("plan_rejected", plan_id, None, bucket=bucket, by=by, note=note.strip())
    return plan
