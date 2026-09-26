"""The work pool — selection (D30, D31) and the drain (D26).

Two decisions make this different from every queue I know of.

**D30 — the agent selects its own batch.** Every scheduler I am aware of *pushes*: it
decides, assigns, and the worker obeys — FIFO, priority, fair-share. Here the agent reads
the held pool and composes its own batch, because the judgement about what groups well IS
the scheduling. That is D3 applied to the queue: a scheduler that assigns work has taken a
decision away from the agent, which is the cage at a new layer.

**D31 — and it is capped, with a stated reason.** A self-selected batch has no natural
ceiling; an agent having a confident morning takes forty tasks and spends a month before
lunch. And a batch of fourteen with no stated grouping is undiagnosable afterwards — you
cannot tell whether the tasks were badly written or badly grouped.

**The cap is a guess and is labelled one.** `DEFAULT_CAP` has no evidence behind it. The
number that belongs here is the point where first-try pass rate bends against selection
size, and no run in this estate has ever grouped tasks, so that figure does not exist
anywhere yet. `selection_size` goes in the ledger precisely so it can be measured.
"""
from __future__ import annotations

import os
import time
import uuid

from . import ledger, store, tasks

DEFAULT_CAP = 10          # UNMEASURED. See the module docstring.
DEFAULT_CONCURRENCY = 2   # lane-broker measured max_concurrent=1 for inference on one GPU;
                          # agents are not inference, so this is also unmeasured.
MIN_REASON_CHARS = 20
DEFAULT_APPROVAL = "ask"  # D18: default is ask. Tests of other mechanics set this to "auto".
APPROVAL_TIMEOUT_S = 24 * 3600   # UNMEASURED. After this a pending selection HALTS; it is
                                 # never approved by silence.


class SelectionRefused(RuntimeError):
    pass


class DrainSkipped(RuntimeError):
    """This task was not started, and that is normal. Catch this, not its subclasses.

    Two competing drains produce two different reasons to skip and both are routine:
    the slot was full, or someone else already took the task. Measured 2026-09-22 —
    three processes racing one bucket produced one start, one throttle, and one
    CRASH, because "already claimed" was raised as an IllegalTransition. The cap was
    correct; the error surface was not, and a drain loop that only caught throttling
    died on the other one.
    """


class ApprovalRequired(RuntimeError):
    """B5n / D17 — the selection this task belongs to has not been approved.

    NOT a DrainSkipped: a drain loop that swallows this as routine would sit forever on
    work nobody has said yes to, and would look busy doing it.
    """


class ConcurrencyExceeded(DrainSkipped):
    pass


class TaskAlreadyClaimed(DrainSkipped):
    pass


# ----------------------------------------------------------------- policy

def policy(bucket: str) -> dict:
    p = store.read("pool-%s" % bucket).get("policy") or {}
    return {"cap": p.get("cap", DEFAULT_CAP),
            "concurrency": p.get("concurrency", DEFAULT_CONCURRENCY),
            "approval": p.get("approval", DEFAULT_APPROVAL),   # D18: default is ask
            "cap_source": p.get("cap_source", "default (UNMEASURED)")}


def set_policy(bucket: str, **kw) -> dict:
    def fn(state):
        state.setdefault("policy", {}).update(kw)
        return state
    store.update("pool-%s" % bucket, fn)
    p = policy(bucket)
    ledger.write("pool_policy", "pool", None, bucket=bucket, **p)
    return p


# ----------------------------------------------------------------- selection

def select(bucket: str, task_ids: list[str], reason: str, agent: str) -> dict:
    """The agent hands back what it will take and WHY. Both are required.

    Refused — never silently trimmed — when the batch is over the cap, when a task is
    not actually held, or when no reason is given. Trimming would hide the fact that
    the agent wanted more than it may have.
    """
    pol = policy(bucket)
    reason = (reason or "").strip()

    if not reason or len(reason) < MIN_REASON_CHARS:
        _refuse(bucket, agent, task_ids, "no_reason",
                "a selection must say why these tasks group together — a batch with no "
                "stated grouping cannot be diagnosed afterwards (D31)")
    if not task_ids:
        _refuse(bucket, agent, task_ids, "empty", "an empty selection is not a selection")
    if len(task_ids) > pol["cap"]:
        _refuse(bucket, agent, task_ids, "over_cap",
                "selection of %d exceeds this bucket's cap of %d (%s). Take fewer, or "
                "raise the cap deliberately — it is not trimmed for you."
                % (len(task_ids), pol["cap"], pol["cap_source"]))
    if len(set(task_ids)) != len(task_ids):
        _refuse(bucket, agent, task_ids, "duplicate", "the same task appears twice")

    held = {t["id"] for t in tasks.in_state(bucket, tasks.HELD)}
    missing = [t for t in task_ids if t not in held]
    if missing:
        _refuse(bucket, agent, task_ids, "not_held",
                "these are not HELD and cannot be selected: %s" % ", ".join(missing))

    sid = "S-" + uuid.uuid4().hex[:8]
    approval = "pending" if pol["approval"] == "ask" else "not_required"
    rec = {"id": sid, "tasks": task_ids, "reason": reason, "agent": agent,
           "approval": approval, "created": time.time(), "decided_by": None}

    def keep(state):
        state.setdefault("selections", {})[sid] = rec
        return state
    store.update("pool-%s" % bucket, keep)
    for tid in task_ids:
        tasks.transition(bucket, tid, tasks.SELECTED, selection_id=sid)

    ledger.write("selection", "pool", None, bucket=bucket, selection_id=sid,
                 agent=agent, tasks=task_ids, selection_size=len(task_ids),
                 cap=pol["cap"], cap_source=pol["cap_source"], reason=reason)
    return {"selection_id": sid, "tasks": task_ids, "size": len(task_ids),
            "reason": reason, "cap": pol["cap"], "approval": approval}


# ----------------------------------------------------------------- approval (B5n, D17)

def selections(bucket: str) -> dict:
    return store.read("pool-%s" % bucket).get("selections", {})


def selection(bucket: str, sid: str) -> dict | None:
    return selections(bucket).get(sid)


def _decide(bucket: str, sid: str, to: str, by: str) -> dict:
    out = {}

    def fn(state):
        sel = state.get("selections", {}).get(sid)
        if sel is None:
            out["err"] = KeyError("no selection %s in bucket %s" % (sid, bucket))
        elif sel["approval"] != "pending":
            out["err"] = ApprovalRequired(
                "selection %s is already %s; a decision is taken once" % (sid, sel["approval"]))
        else:
            sel.update(approval=to, decided_by=by, decided_at=time.time())
            out["sel"] = dict(sel)
        return state
    store.update("pool-%s" % bucket, fn)
    if "err" in out:
        raise out["err"]
    ledger.write("selection_" + to, "pool", None, bucket=bucket, selection_id=sid,
                 decided_by=by, tasks=out["sel"]["tasks"])
    return out["sel"]


def approve(bucket: str, sid: str, by: str) -> dict:
    """An explicit yes, from a named human. The only way a pending selection runs."""
    if not (by or "").strip():
        raise ApprovalRequired("an approval must name who gave it")
    return _decide(bucket, sid, "approved", by)


def reject(bucket: str, sid: str, by: str, note: str | None = None) -> dict:
    """No. The tasks go back to HELD, untouched, to be selected differently.

    The person's reason travels with each task (`selection_rejected`), so whoever selects
    next sees why this grouping was refused instead of repeating it.
    """
    sel = _decide(bucket, sid, "rejected", by or "unknown")
    for tid in sel["tasks"]:
        if (tasks.get(bucket, tid) or {}).get("state") == tasks.SELECTED:
            tasks.transition(bucket, tid, tasks.HELD, selection_id=None)
            tasks.annotate(bucket, tid, "selection_rejected",
                           {"selection_id": sid, "by": by, "note": note, "reason_was": sel.get("reason")})
    return sel


def expire(bucket: str, timeout_s: float = APPROVAL_TIMEOUT_S) -> list[str]:
    """A pending selection older than the timeout HALTS. Silence is not consent.

    Logged as HALTED_FOR_DECISION — §10's word for a run that stopped to ask — and the
    tasks return to HELD. Nothing here can ever produce "approved".
    """
    now, halted = time.time(), []
    for sid, sel in selections(bucket).items():
        if sel["approval"] == "pending" and now - sel["created"] > timeout_s:
            try:
                _decide(bucket, sid, "expired", "timeout")
            except (ApprovalRequired, KeyError):
                continue                     # decided by someone else in between
            for tid in sel["tasks"]:
                if (tasks.get(bucket, tid) or {}).get("state") == tasks.SELECTED:
                    tasks.transition(bucket, tid, tasks.HELD, selection_id=None)
            ledger.write("selection_halted", "pool", None, bucket=bucket, selection_id=sid,
                         status="HALTED_FOR_DECISION", waited_s=round(now - sel["created"]),
                         timeout_s=timeout_s)
            halted.append(sid)
    return halted


def _refuse(bucket, agent, task_ids, code, msg):
    ledger.write("selection_refused", "pool", None, bucket=bucket, agent=agent,
                 tasks=task_ids, selection_size=len(task_ids or []), code=code, reason=msg)
    raise SelectionRefused("%s: %s" % (code, msg))


# ----------------------------------------------------------------- the drain

def running(bucket: str) -> list[dict]:
    return tasks.in_state(bucket, tasks.RUNNING)


def start(bucket: str, task_id: str, run_id: str) -> dict:
    """Claim a slot and move a SELECTED task into RUNNING — ATOMICALLY.

    D26: 200 released tasks never become 200 concurrent agents. `lane-broker` proved the
    law one level down and it is the same law here — one machine, finite compute.

    **The count and the claim happen inside ONE lock.** An earlier version read the
    running count, checked it, then transitioned — three steps with two gaps. Measured:
    three processes against `concurrency=1` each started a task, because all three read
    zero before any of them wrote. The synchronous composed accept printed a green
    "at most N running" line over it, because one drain never overlaps itself.

    That is why the test for this runs real competing processes, and why the check lives
    inside `store.update` rather than in front of it.
    """
    pol = policy(bucket)
    outcome = {}

    # D17: nothing dispatches from an unapproved selection. Checked before the claim; a
    # selection's approval only ever moves pending -> decided, so it cannot un-approve
    # between this read and the claim below.
    cur = tasks.get(bucket, task_id)
    sid = (cur or {}).get("selection_id")
    sel = selection(bucket, sid) if sid else None
    if cur and cur["state"] == tasks.SELECTED and sel and             sel["approval"] not in ("approved", "not_required"):
        ledger.write("start_refused_unapproved", "pool", task_id, bucket=bucket,
                     selection_id=sid, approval=sel["approval"])
        raise ApprovalRequired("%s belongs to selection %s, which is %s. Approve it first."
                               % (task_id, sid, sel["approval"]))

    def claim(state):
        ts = state.get("tasks", {})
        cur = ts.get(task_id)
        if cur is None:
            outcome["err"] = KeyError("no task %s in bucket %s" % (task_id, bucket))
            return state
        if cur["state"] != tasks.SELECTED:
            # Not a programming error — another drain got here first. Routine.
            outcome["claimed"] = cur["state"]
            return state
        live = sum(1 for x in ts.values() if x["state"] == tasks.RUNNING)
        if live >= pol["concurrency"]:
            outcome["throttled"] = live
            return state
        t2 = dict(cur)
        t2["state"] = tasks.RUNNING
        t2["run_id"] = run_id
        # Who owns this, and when did they last say so. The pid is for diagnosis only —
        # see reap() for why the heartbeat, not the pid, is the liveness signal.
        t2["owner_pid"] = os.getpid()
        t2["heartbeat"] = time.time()
        t2["history"] = t2.get("history", []) + [
            {"at": tasks._now(), "to": tasks.RUNNING, "run_id": run_id}]
        state["tasks"] = {**ts, task_id: t2}
        outcome["task"] = t2
        return state

    store.update("tasks-%s" % bucket, claim)

    if "err" in outcome:
        raise outcome["err"]
    if "claimed" in outcome:
        ledger.write("drain_already_claimed", "pool", task_id, bucket=bucket,
                     found_state=outcome["claimed"])
        raise TaskAlreadyClaimed(
            "%s is already %s — another drain took it between your read and your claim. "
            "Skip it; this is what concurrent drains look like."
            % (task_id, outcome["claimed"]))
    if "throttled" in outcome:
        ledger.write("drain_throttled", "pool", task_id, bucket=bucket,
                     running=outcome["throttled"], concurrency=pol["concurrency"])
        raise ConcurrencyExceeded(
            "%d already running against a concurrency cap of %d for bucket %s. The task "
            "stays SELECTED and waits — a released backlog is not a licence to fan out."
            % (outcome["throttled"], pol["concurrency"], bucket))

    ledger.write("task_transition", "pool", task_id, bucket=bucket,
                 **{"from": tasks.SELECTED, "to": tasks.RUNNING}, task_run_id=run_id)
    return outcome["task"]


def finish(bucket: str, task_id: str, ok: bool, gate_exit: int | None = None,
           **evidence) -> dict:
    """Done or failed. `gate_exit` decides, not the caller's opinion of how it went.

    `evidence` (branch, commit, run_dir, runner_exit ...) is recorded on the task so a
    human can see what the run did. It never influences the verdict.
    """
    landed = bool(ok) and gate_exit == 0
    return tasks.transition(bucket, task_id,
                            tasks.DONE if landed else tasks.FAILED,
                            gate_exit=gate_exit, **evidence)


def drain(bucket: str, selection: dict, runner) -> dict:
    """Walk a selection, respecting the concurrency cap. `runner(task) -> (ok, gate_exit)`.

    Returns what happened per task. A throttled task is NOT an error — it stays SELECTED
    and the caller drains again later.
    """
    # LIMIT, stated because the composed accept's own output shows it: this loop is
    # SYNCHRONOUS, so within a single drain nothing overlaps and the concurrency check
    # can never fire. The cap binds across CONCURRENT drains — two processes draining
    # the same bucket — which is the real case, and `start()` is where it is enforced.
    # A green "at most N running" line inside one synchronous drain proves very little.
    results, throttled = [], []
    for tid in selection["tasks"]:
        run_id = "%s-%s" % (selection["selection_id"], tid)
        try:
            start(bucket, tid, run_id)
        except DrainSkipped:          # full, or already taken — both are routine
            throttled.append(tid)
            continue
        try:
            ok, gate_exit = runner(tasks.get(bucket, tid))
        except Exception as e:                      # a runner blowing up is a FAILED task,
            ok, gate_exit = False, None             # not a crashed drain
            ledger.write("task_runner_error", "pool", tid, bucket=bucket, error=str(e))
        finish(bucket, tid, ok, gate_exit)
        results.append({"task": tid, "ok": ok, "gate_exit": gate_exit})

    ledger.write("drain", "pool", None, bucket=bucket,
                 selection_id=selection["selection_id"],
                 ran=len(results), throttled=len(throttled),
                 concurrency=policy(bucket)["concurrency"])
    return {"ran": results, "throttled": throttled,
            "counts": tasks.counts(bucket)}


# ----------------------------------------------------------------- durability

STALE_AFTER_S = 15 * 60      # UNMEASURED. A run legitimately quiet for longer than this
                             # has not been observed; the number is a guess and says so.


def beat(bucket: str, task_id: str) -> float | None:
    """Say you are still here. A long node must call this or it will be reaped.

    Cheap by design — one locked write of a float, no ledger row. A heartbeat that is
    expensive is a heartbeat that gets skipped inside a tight loop.
    """
    now = time.time()

    def fn(state):
        ts = state.get("tasks", {})
        cur = ts.get(task_id)
        if cur and cur["state"] == tasks.RUNNING:
            state["tasks"] = {**ts, task_id: {**cur, "heartbeat": now}}
        return state

    store.update("tasks-%s" % bucket, fn)
    return now


def reap(bucket: str, stale_after_s: float = STALE_AFTER_S) -> list[dict]:
    """Return tasks whose owner stopped beating to the pool.

    **This is the gap LangGraph closes with a checkpointer and we had nothing for.**
    Without it, a daemon that dies mid-drain leaves tasks RUNNING forever with no process
    behind them — the pool looks busy, the concurrency slots stay consumed, and nothing
    ever runs again. "The work continues while you are gone" is false the first time
    something dies.

    **Liveness is the heartbeat, not the pid.** Checking whether a pid is alive is not
    portable in the standard library, and pid reuse can report a long-dead worker as
    running — which would be worse than no check, because it would keep a slot held
    forever on the strength of a coincidence. A stopped clock cannot lie that way.
    `owner_pid` is recorded for a human reading the row, and is never consulted here.

    A reaped task becomes ABANDONED, not FAILED: it never got a verdict.
    """
    now = time.time()
    reaped = []

    def fn(state):
        ts = dict(state.get("tasks", {}))
        for tid, t in list(ts.items()):
            if t.get("state") != tasks.RUNNING:
                continue
            hb = t.get("heartbeat")
            if hb is None or (now - hb) <= stale_after_s:
                continue
            ts[tid] = {**t, "state": tasks.ABANDONED,
                       "abandoned_after_s": round(now - hb, 1),
                       "history": t.get("history", []) + [
                           {"at": tasks._now(), "to": tasks.ABANDONED,
                            "silent_for_s": round(now - hb, 1)}]}
            reaped.append(ts[tid])
        state["tasks"] = ts
        return state

    store.update("tasks-%s" % bucket, fn)

    for t in reaped:
        ledger.write("task_abandoned", "pool", t["id"], bucket=bucket,
                     silent_for_s=t["abandoned_after_s"], stale_after_s=stale_after_s,
                     stale_after_source="default (UNMEASURED)",
                     owner_pid=t.get("owner_pid"), task_run_id=t.get("run_id"),
                     detected_by="heartbeat")
    return reaped


def requeue(bucket: str, task_id: str) -> dict:
    """Put an abandoned or failed task back in the pool, where it can be selected again."""
    return tasks.transition(bucket, task_id, tasks.HELD)


def recover(bucket: str, stale_after_s: float = STALE_AFTER_S) -> dict:
    """Reap, then requeue. What a daemon runs when it starts and finds old state."""
    reaped = reap(bucket, stale_after_s)
    for t in reaped:
        requeue(bucket, t["id"])
    ledger.write("pool_recover", "pool", None, bucket=bucket,
                 reaped=len(reaped), requeued=len(reaped))
    return {"reaped": [t["id"] for t in reaped], "counts": tasks.counts(bucket)}
