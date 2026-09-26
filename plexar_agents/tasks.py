"""A task is a PROMPT, held — D29.

Not a backlog node. One task enters the framework as one `/your-orchestrator` invocation
and grows its own nodes *there*, where the repo is fresh. Expanding two hundred tasks
into nodes up front spends premium tokens on work that may never be released, and the
plan would be stale by the time it ran.

**HELD is a real state, not an absence of one** (D24). A conversation produces tasks;
nothing runs until something releases them. That is the difference between a work pool
and a pipeline, and it is the whole reason the drain can be bounded.

    drafted ──► held ──► selected ──► running ──► done
                            ▲                  │
                            └── abandoned ◄────┘   (the owner died; no verdict was ever taken)
                 ▲          │            │
                 └──────────┴────────────┴──► (release returns it to held)

A transition not on that diagram is refused. A task cannot go `held -> done` because
somebody decided it was fine; the only way to `done` is through `running`, which is the
only state that produces a run id to point at.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import pathlib
import uuid

from . import ledger, store

DRAFTED, HELD, SELECTED, RUNNING, DONE, FAILED, CANCELLED, ABANDONED = (
    "drafted", "held", "selected", "running", "done", "failed", "cancelled", "abandoned")

TERMINAL = {DONE, CANCELLED}

# The only legal moves. Anything else is refused, loudly.
ALLOWED = {
    DRAFTED:   {HELD, CANCELLED},
    HELD:      {SELECTED, CANCELLED},
    SELECTED:  {RUNNING, HELD, CANCELLED},      # back to HELD if a selection is abandoned
    RUNNING:   {DONE, FAILED, ABANDONED, CANCELLED},
    FAILED:    {HELD, CANCELLED},               # a failure returns to the pool, not to done
    # ABANDONED is NOT failed. A reaped task never got a verdict — its owner died before
    # the gate ran. `node_check_exit: null is not 0`, applied to a whole task: "nobody
    # knows" and "it was wrong" are different facts and must not share a state.
    ABANDONED: {HELD, CANCELLED},
    # A person may send finished work back (rework/reject WITH a note, api.review). That
    # is the only way out of DONE; nothing automatic ever reopens a task.
    DONE:      {HELD},
    CANCELLED: set(),
}


class IllegalTransition(RuntimeError):
    def __init__(self, task_id: str, frm: str, to: str):
        super().__init__(
            "task %s cannot go %s -> %s. Legal from %s: %s. The only route to done is "
            "through running, which is the only state that produces a run to point at."
            % (task_id, frm, to, frm, ", ".join(sorted(ALLOWED.get(frm, set()))) or "nowhere"))


def dir_() -> pathlib.Path:
    from . import paths
    return paths.home() / "tasks"


def _path(bucket: str) -> pathlib.Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in bucket)[:80]
    return dir_() / ("%s.json" % safe)


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def create(bucket: str, prompt: str, source: str = "conversation",
           session_id: str | None = None, meta: dict | None = None) -> dict:
    """A task is created DRAFTED. Nothing runs until it is held and then released."""
    tid = "T-" + uuid.uuid4().hex[:10]
    task = {
        "id": tid, "bucket": bucket, "state": DRAFTED,
        # The prompt is the task. Stored whole, hashed, never summarised — it is the
        # input half of every (input, outcome) pair the ledger exists to produce.
        "prompt": prompt,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "source": source, "session_id": session_id,
        "created": _now(), "history": [{"at": _now(), "to": DRAFTED}],
        "run_id": None, "selection_id": None,
        # Where the task came from, when it came from a slice: slice_id, candidate_id,
        # span into the stored transcript, group. Never overrides the fields above.
        **{k: v for k, v in (meta or {}).items() if k not in ("id", "state", "prompt")},
    }
    _update(bucket, lambda ts: {**ts, tid: task})
    ledger.write("task_created", "pool", tid, bucket=bucket, source=source,
                 prompt_sha256=task["prompt_sha256"], prompt_chars=len(prompt),
                 session_id=session_id, state=DRAFTED, **{
                     "slice_" + k if not k.startswith("slice") else k: v
                     for k, v in (meta or {}).items() if k in ("slice_id", "candidate_id",
                                                               "group")})
    return task


def _update(bucket: str, fn) -> dict:
    p = _path(bucket)

    def wrapped(state):
        state["tasks"] = fn(state.get("tasks", {}))
        return state

    p.parent.mkdir(parents=True, exist_ok=True)
    # Reuse the counter store's lock semantics rather than inventing a second one.
    return store.update("tasks-%s" % bucket, wrapped)["tasks"]


def all_(bucket: str) -> dict:
    return store.read("tasks-%s" % bucket).get("tasks", {})


def get(bucket: str, task_id: str) -> dict | None:
    return all_(bucket).get(task_id)


def in_state(bucket: str, state: str) -> list[dict]:
    return [t for t in all_(bucket).values() if t["state"] == state]


def transition(bucket: str, task_id: str, to: str, **fields) -> dict:
    """Move a task. Refuses anything not on the diagram."""
    current = get(bucket, task_id)
    if current is None:
        raise KeyError("no task %s in bucket %s" % (task_id, bucket))
    frm = current["state"]
    if to not in ALLOWED.get(frm, set()):
        ledger.write("task_transition_refused", "pool", task_id, bucket=bucket,
                     **{"from": frm, "to": to})
        raise IllegalTransition(task_id, frm, to)

    def fn(ts):
        t = dict(ts[task_id])
        t["state"] = to
        t.update(fields)
        t["history"] = t.get("history", []) + [{"at": _now(), "to": to, **fields}]
        return {**ts, task_id: t}

    t = _update(bucket, fn)[task_id]
    # A task's own `run_id` collides with ledger.write's positional parameter of the
    # same name — two different things wearing one word. Namespaced at the boundary
    # rather than silently dropped, so the row still carries which run took the task.
    safe = {("task_%s" % k if k in ("run_id", "node_id", "event") else k): v
            for k, v in fields.items()}
    ledger.write("task_transition", "pool", task_id, bucket=bucket,
                 **{"from": frm, "to": to}, **safe)
    return t


def hold(bucket: str, task_id: str) -> dict:
    """Ready to run, and deliberately not running."""
    return transition(bucket, task_id, HELD)


def hold_all(bucket: str) -> list[dict]:
    return [hold(bucket, t["id"]) for t in in_state(bucket, DRAFTED)]


def counts(bucket: str) -> dict:
    out = {s: 0 for s in (DRAFTED, HELD, SELECTED, RUNNING, DONE, FAILED,
                          ABANDONED, CANCELLED)}
    for t in all_(bucket).values():
        out[t["state"]] = out.get(t["state"], 0) + 1
    return out


def buckets() -> list[str]:
    try:
        return sorted(p.stem.replace("tasks-", "") for p in
                      (dir_().parent / "counters").glob("tasks-*.json"))
    except OSError:
        return []


def annotate(bucket: str, task_id: str, key: str, value) -> dict:
    """Attach a non-state fact to a task (a human review, a note). Never changes state."""
    if key in ("id", "state", "prompt", "prompt_sha256", "history"):
        raise ValueError("%s is not annotatable" % key)
    if get(bucket, task_id) is None:
        raise KeyError("no task %s in bucket %s" % (task_id, bucket))

    def fn(ts):
        return {**ts, task_id: {**ts[task_id], key: value}}
    return _update(bucket, fn)[task_id]
