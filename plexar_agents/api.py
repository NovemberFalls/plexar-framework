"""The local HTTP API — D28: the API is the product surface, Studio is one client of it.

A thin transport over `tasks` and `pool`. **Nothing here decides anything.** Every refusal
is the pool's refusal, mapped to a status code; every transition is `tasks.transition`.
A second implementation of "what a legal move is" living in a route handler is exactly
the drift D14 exists to prevent.

    POST /api/buckets/{b}/tasks                  submit a prompt   -> 202 {id, prompt_sha256}
    GET  /api/buckets                            every bucket, its counts and policy
    GET  /api/buckets/{b}/tasks[?state=held]     the pool
    GET  /api/buckets/{b}/tasks/{id}             one task, prompt and history
    POST /api/buckets/{b}/tasks/{id}/hold        drafted -> held
    POST /api/buckets/{b}/tasks/{id}/cancel
    POST /api/buckets/{b}/tasks/{id}/requeue     failed/abandoned -> held
    GET  /api/buckets/{b}/policy · PUT to change it
    POST /api/buckets/{b}/selections             {task_ids, reason, agent}   (D30/D31)
    POST /api/buckets/{b}/tasks/{id}/start       {run_id}  -- the LEASE; 409 if full or taken
    POST /api/buckets/{b}/tasks/{id}/beat        heartbeat while running
    POST /api/buckets/{b}/tasks/{id}/finish      {ok, gate_exit}  -- gate_exit decides
    POST /api/buckets/{b}/recover                reap stale runners, requeue them
    GET  /api/buckets/{b}/tasks/{id}/run         what the run did: log tail, branch, diff
    GET  /api/buckets/{b}/selections[?approval=pending]
    POST /api/buckets/{b}/selections/{sid}/approve  {by}   -- the explicit yes (D17)
    POST /api/buckets/{b}/selections/{sid}/reject   {by}   -- tasks return to HELD
    GET  /api/logs/where                         where the log files live, and which exist
    GET  /api/logs[?cursor&event&run_id&task&date_from&date_to&limit]   rows, paged
    GET  /api/logs/export[?same filters]         one NDJSON download
    POST /api/logs                               {source, rows} -- bring your own rows in
    GET  /api/buckets/{b}/questions              tasks waiting for a person's answer
    POST /api/buckets/{b}/tasks/{id}/answer      {answer, by} -- answer an agent's question
    GET  /api/buckets/{b}/plans                  every plan, newest first (plans.all_)
    GET  /api/buckets/{b}/plans/{pid}             one plan, each task's live state merged in
    POST /api/buckets/{b}/plans                  {goal, by} -> 202 {id, status:"planning"};
                                                  the planner runs in a background thread
    POST /api/buckets/{b}/plans/{pid}/answer      {answers, by} -- fills open questions, re-plans
    POST /api/buckets/{b}/plans/{pid}/approve     {by} -- the one approval; the sweeper takes it
    POST /api/buckets/{b}/plans/{pid}/reject      {by, note} -- note required

Status codes: 404 unknown task · 409 illegal move, slot full, or already claimed ·
422 a selection the pool refused (the body says which rule, verbatim) ·
428 the task's selection has not been approved.

**Localhost only.** The server binds 127.0.0.1 by default and this router has no auth.
A submitted prompt causes code to be written; exposing it off the machine is O2, and it
needs auth before it happens.
"""
from __future__ import annotations

import json
import re
import threading

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from . import daemon, logs, plans, pool, tasks

router = APIRouter(prefix="/api")

_BUCKET = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


def _bucket(b: str) -> str:
    # tasks._path sanitises silently, so "a/b" and "a_b" would share one file. Refuse
    # instead: two names for one bucket is a collision nobody asked for.
    if not _BUCKET.match(b):
        raise HTTPException(400, "bucket must match %s" % _BUCKET.pattern)
    return b


def _task(b: str, tid: str) -> dict:
    t = tasks.get(_bucket(b), tid)
    if t is None:
        raise HTTPException(404, "no task %s in bucket %s" % (tid, b))
    return t


def _call(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except pool.ApprovalRequired as e:
        raise HTTPException(428, {"code": "approval_required", "detail": str(e)})
    except tasks.IllegalTransition as e:
        raise HTTPException(409, str(e))
    except pool.ConcurrencyExceeded as e:
        raise HTTPException(409, {"code": "concurrency", "detail": str(e)})
    except pool.TaskAlreadyClaimed as e:
        raise HTTPException(409, {"code": "claimed", "detail": str(e)})
    except pool.DrainSkipped as e:
        raise HTTPException(409, {"code": "skipped", "detail": str(e)})
    except pool.SelectionRefused as e:
        code, _, detail = str(e).partition(": ")
        raise HTTPException(422, {"code": code, "detail": detail})
    except KeyError as e:
        raise HTTPException(404, str(e))


# ----------------------------------------------------------------- bodies

class Submit(BaseModel):
    prompt: str = Field(min_length=1)
    source: str = "api"
    session_id: str | None = None
    hold: bool = True            # a submitted brief is ready to run and deliberately not running
    # The task's own check: a command that must FAIL on the code before the work and PASS
    # after it. Optional; the bucket's gate always runs too.
    check: str | None = Field(default=None, max_length=2000)


class Selection(BaseModel):
    task_ids: list[str]
    reason: str
    agent: str


class Decision(BaseModel):
    by: str = Field(min_length=1, max_length=80)
    note: str | None = Field(default=None, max_length=2000)   # why, on a reject


class Start(BaseModel):
    run_id: str = Field(min_length=1, max_length=128)


class Finish(BaseModel):
    ok: bool
    gate_exit: int | None = None


class Policy(BaseModel):
    cap: int | None = Field(default=None, ge=1)
    concurrency: int | None = Field(default=None, ge=1)
    approval: str | None = Field(default=None, pattern="^(ask|auto)$")
    cap_source: str | None = None


# ----------------------------------------------------------------- routes

@router.get("/buckets")
def list_buckets() -> list[dict]:
    return [{"bucket": b, "counts": tasks.counts(b), "policy": pool.policy(b)}
            for b in tasks.buckets()]


@router.post("/buckets/{b}/tasks", status_code=202)
def submit(b: str, body: Submit) -> dict:
    t = tasks.create(_bucket(b), body.prompt, source=body.source, session_id=body.session_id,
                     meta={"check": body.check.strip()} if (body.check or "").strip() else None)
    if body.hold:
        t = tasks.hold(b, t["id"])
    return {"id": t["id"], "state": t["state"], "prompt_sha256": t["prompt_sha256"]}


@router.get("/buckets/{b}/tasks")
def list_tasks(b: str, state: str | None = Query(default=None)) -> list[dict]:
    ts = tasks.all_(_bucket(b)).values()
    return [t for t in ts if state is None or t["state"] == state]


@router.get("/buckets/{b}/tasks/{tid}")
def get_task(b: str, tid: str) -> dict:
    return _task(b, tid)


@router.post("/buckets/{b}/tasks/{tid}/hold")
def hold(b: str, tid: str) -> dict:
    _task(b, tid)
    return _call(tasks.hold, b, tid)


@router.post("/buckets/{b}/tasks/{tid}/cancel")
def cancel(b: str, tid: str) -> dict:
    _task(b, tid)
    return _call(tasks.transition, b, tid, tasks.CANCELLED)


@router.post("/buckets/{b}/tasks/{tid}/requeue")
def requeue(b: str, tid: str) -> dict:
    _task(b, tid)
    return _call(pool.requeue, b, tid)


@router.get("/buckets/{b}/policy")
def get_policy(b: str) -> dict:
    return pool.policy(_bucket(b))


@router.put("/buckets/{b}/policy")
def put_policy(b: str, body: Policy) -> dict:
    kw = {k: v for k, v in body.model_dump().items() if v is not None}
    return pool.set_policy(_bucket(b), **kw)


@router.post("/buckets/{b}/selections", status_code=201)
def select(b: str, body: Selection) -> dict:
    return _call(pool.select, _bucket(b), body.task_ids, body.reason, body.agent)


@router.post("/buckets/{b}/tasks/{tid}/start")
def start(b: str, tid: str, body: Start) -> dict:
    _task(b, tid)
    return _call(pool.start, b, tid, body.run_id)


@router.post("/buckets/{b}/tasks/{tid}/beat")
def beat(b: str, tid: str) -> dict:
    t = _task(b, tid)
    if t["state"] != tasks.RUNNING:
        raise HTTPException(409, "task %s is %s, not running" % (tid, t["state"]))
    return {"heartbeat": pool.beat(b, tid)}


@router.post("/buckets/{b}/tasks/{tid}/finish")
def finish(b: str, tid: str, body: Finish) -> dict:
    _task(b, tid)
    return _call(pool.finish, b, tid, body.ok, body.gate_exit)


@router.post("/buckets/{b}/recover")
def recover(b: str) -> dict:
    return pool.recover(_bucket(b))


@router.get("/buckets/{b}/selections")
def list_selections(b: str, approval: str | None = Query(default=None)) -> list[dict]:
    sels = pool.selections(_bucket(b)).values()
    return sorted((x for x in sels if approval is None or x["approval"] == approval),
                  key=lambda x: x["created"], reverse=True)


@router.post("/buckets/{b}/selections/{sid}/approve")
def approve(b: str, sid: str, body: Decision) -> dict:
    return _call(pool.approve, _bucket(b), sid, body.by)


@router.post("/buckets/{b}/selections/{sid}/reject")
def reject(b: str, sid: str, body: Decision) -> dict:
    return _call(pool.reject, _bucket(b), sid, body.by, body.note)


@router.get("/daemon")
def daemon_status() -> dict:
    """What the daemon WOULD run — read-only. Runner and gate are set on disk, never here."""
    cfg = daemon.load_config()
    return {"config": str(daemon.config_path()),
            "buckets": {b: {"cwd": c.get("cwd"), "runnable": daemon.runnable(c) is None,
                            "why_not": daemon.runnable(c)} for b, c in cfg.items()}}


LOG_TAIL = 20000
DIFF_CAP = 60000


@router.get("/buckets/{b}/tasks/{tid}/run")
def run_detail(b: str, tid: str) -> dict:
    """What the run did — the evidence behind a verdict, read-only.

    The log path and the repo come from the daemon's own records (the task and
    daemon.json), never from the request, so this cannot be pointed at other files.
    """
    t = _task(b, tid)
    out = {k: t.get(k) for k in ("state", "run_id", "gate_exit", "runner_exit", "branch",
                                 "base", "base_sha", "commit", "refused", "error",
                                 "review_final", "review_attempts", "qa_file", "qa_cases",
                                 "qa_results", "gate_output", "feedback", "selection_rejected",
                                 "review", "question_open", "answers", "check", "check_before_exit",
                                 "check_before_output", "check_after_exit", "check_after_output")}
    log = None
    if t.get("run_dir"):
        p = daemon.pathlib.Path(t["run_dir"]) / "output.log"
        if p.is_file():
            data = p.read_bytes()
            log = data[-LOG_TAIL:].decode("utf-8", "replace")
            out["log_truncated"] = len(data) > LOG_TAIL
    out["log"] = log
    # Every check that ran, in order, as data: the gate, then each reviewer hop with the
    # commands it ran and what they printed. The board shows this instead of raw JSON.
    gate = (daemon.load_config().get(b) or {}).get("gate")
    out["gate_cmd"] = " ".join(gate) if isinstance(gate, list) else gate
    from . import memory as memory_mod
    mcwd = (daemon.load_config().get(b) or {}).get("cwd")
    out["memory"] = memory_mod.load(mcwd, tid) if mcwd else {}
    out["memory_path"] = str(memory_mod.path(mcwd, tid)) if mcwd else None
    out["memory_clear_on_accept"] = memory_mod.clear_on_accept()
    # Every reviewer hop across EVERY run of this task, oldest first, each tagged with its run.
    # Found 2026-09-26: reading only the latest run hid a task's earlier verifier and approver
    # rounds whenever its newest run stopped early (a question), and the board then claimed
    # the bucket had no review chain.
    out["hops"] = []
    from . import logs as logs_mod
    for _, row in logs_mod.iter_rows(event="review", task=tid):
        if row:
            out["hops"].append({k: row.get(k) for k in (
                "hop", "attempt", "stage", "reviewer_model", "subject_model", "verdict",
                "judgement", "feedback", "evidence", "gate_exit", "wall_ms", "ts", "run_id")})
    out["runs"] = list(dict.fromkeys(h["run_id"] for h in out["hops"] if h.get("run_id")))
    out["review_configured"] = bool((daemon.load_config().get(b) or {}).get("review"))
    diff = None
    cwd = (daemon.load_config().get(b) or {}).get("cwd")
    if cwd and t.get("commit") and t.get("base_sha"):
        code, text = daemon._git(cwd, "diff", "--stat", "--patch", t["base_sha"], t["commit"])
        if code == 0:
            diff = text[:DIFF_CAP]
            out["diff_truncated"] = len(text) > DIFF_CAP
    out["diff"] = diff
    return out


# ----------------------------------------------------------------- slices (read-only)

@router.get("/slices")
def list_slices(bucket: str | None = Query(default=None)) -> list[dict]:
    """Every stored task-builder slice, newest first. Committing stays a CLI act by a
    named person; the API can show a slice and can never commit one."""
    from . import slicer
    out = []
    for p in sorted(slicer.slices_dir().glob("SL-*.json"), key=lambda f: f.stat().st_mtime,
                    reverse=True):
        if p.name.endswith(".transcript.txt"):
            continue
        s = json.loads(p.read_text(encoding="utf-8"))
        if bucket and s["bucket"] != bucket:
            continue
        r = s["report"]
        fates: dict = {}
        for c in s["candidates"]:
            k = (c["fate"] or "none").split(":")[0]
            fates[k] = fates.get(k, 0) + 1
        out.append({"slice_id": s["slice_id"], "bucket": s["bucket"], "ok": r["ok"],
                    "coverage": r["coverage"], "tasks": len(s["tasks"]),
                    "uncovered": len(r["uncovered"]), "fates": fates,
                    "source_path": s.get("source_path"), "model": s.get("model"),
                    "committed": any(t.get("slice_id") == s["slice_id"]
                                     for t in tasks.all_(s["bucket"]).values())})
    return out


@router.get("/slices/{sid}")
def get_slice(sid: str) -> dict:
    from . import slicer
    if not re.match(r"^SL-[0-9]{8}-[0-9a-f]{6}$", sid):
        raise HTTPException(400, "not a slice id")
    try:
        return slicer.load(sid)
    except FileNotFoundError:
        raise HTTPException(404, "no slice %s" % sid)


# ----------------------------------------------------------------- Studio feeds (F1, F2)

@router.get("/summary")
def summary() -> dict:
    """The TASKS badge: cheap, read-only. Counts only; no prompts."""
    per, pending, running = {}, 0, 0
    for b in tasks.buckets():
        c = tasks.counts(b)
        p = sum(1 for s in pool.selections(b).values() if s["approval"] == "pending")
        per[b] = {"counts": c, "pending_approvals": p}
        pending += p
        running += c.get("running", 0)
    return {"pending_approvals": pending, "running": running, "buckets": per}


@router.get("/events")
def events(since: str = Query(default=""), limit: int = Query(default=200, ge=1, le=1000)) -> dict:
    """Task state changes after `since`, oldest first, with a cursor for the next call.

    The cursor is `<at>|<task>|<n>`: every task's history is append-only, so position n in
    it never moves, and the triple orders changes that share a one-second timestamp.
    Studio polls this for "task finished" toasts; it never needs to re-read everything.
    """
    out = []
    for b in tasks.buckets():
        for t in tasks.all_(b).values():
            for n, h in enumerate(t.get("history", [])):
                key = "%s|%s|%04d" % (h.get("at", ""), t["id"], n)
                if key > since:
                    out.append({"cursor": key, "task_id": t["id"], "bucket": b,
                                "to": h.get("to"), "at": h.get("at"),
                                "gate_exit": h.get("gate_exit"), "branch": t.get("branch"),
                                "session_id": t.get("session_id")})
    out.sort(key=lambda e: e["cursor"])
    out = out[:limit]
    return {"events": out, "cursor": out[-1]["cursor"] if out else since}


# ----------------------------------------------------------------- the logs (the user's)
# Every row the framework writes, readable, dumpable, and open to rows from other tools.
# See plexar_agents/logs.py for the contract; these are a thin transport over it.

_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _days(date_from, date_to):
    for d in (date_from, date_to):
        if d and not _DAY.match(d):
            raise HTTPException(422, "dates are YYYY-MM-DD")


@router.get("/logs/where")
def logs_where() -> dict:
    """Where the logs live on disk, and which files exist. The folder is the log."""
    return logs.where()


@router.get("/logs")
def logs_read(cursor: str = Query(default=""), limit: int = Query(default=500, ge=1, le=5000),
              event: str = Query(default=""), run_id: str = Query(default=""),
              task: str = Query(default=""), date_from: str = Query(default=""),
              date_to: str = Query(default="")) -> dict:
    """Rows oldest-first after `cursor`; `event` may be a comma list. Loop while `more`."""
    _days(date_from, date_to)
    try:
        return logs.read(cursor or None, limit, event=event or None, run_id=run_id or None,
                         task=task or None, date_from=date_from or None, date_to=date_to or None)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/logs/export")
def logs_export(event: str = Query(default=""), run_id: str = Query(default=""),
                task: str = Query(default=""), date_from: str = Query(default=""),
                date_to: str = Query(default="")):
    """Everything that matches, as one NDJSON download. A line that is not JSON is left
    out, never repaired; /api/logs reports how many there are (`corrupt`)."""
    from fastapi.responses import StreamingResponse
    _days(date_from, date_to)
    kw = dict(event=event or None, run_id=run_id or None, task=task or None,
              date_from=date_from or None, date_to=date_to or None)

    def gen():
        for _, row in logs.iter_rows(None, **kw):
            if row is not None:
                yield json.dumps(row, default=str) + "\n"
    # run_id/task are caller text going into a header: keep only filename-safe characters.
    tag = re.sub(r"[^A-Za-z0-9._-]", "", "_".join(x for x in (date_from, date_to, run_id or task) if x))[:80]
    name = "plexar-logs-%s.ndjson" % (tag or "all")
    return StreamingResponse(gen(), media_type="application/x-ndjson",
                             headers={"content-disposition": 'attachment; filename="%s"' % name})


class Ingest(BaseModel):
    source: str = Field(min_length=1, max_length=120)
    rows: list = Field(min_length=1)


@router.post("/logs")
def logs_ingest(body: Ingest) -> dict:
    """Bring rows from another tool. Accepted rows land in ingested-<day>.jsonl, never in
    the framework's own files; rejected rows come back with their reasons."""
    try:
        return logs.ingest(body.rows, body.source)
    except ValueError as e:
        raise HTTPException(422, str(e))


# ----------------------------------------------------------------- the human verdict

class Review(BaseModel):
    verdict: str = Field(pattern="^(merged|rejected|rework)$")
    by: str = Field(min_length=1, max_length=80)
    note: str | None = Field(default=None, max_length=2000)


@router.post("/buckets/{b}/tasks/{tid}/review")
def review(b: str, tid: str, body: Review) -> dict:
    """What a person decided about a finished task's branch. The label the gate cannot give.

    The gate says whether the tests passed; only a human says whether the work was wanted.
    Recorded on the task and as a `task_review` row under the task's run id, so it joins to
    that run's dispatch and outcome rows.
    """
    t = _task(b, tid)
    if t["state"] not in (tasks.DONE, tasks.FAILED):
        raise HTTPException(409, "task %s is %s; review a finished task" % (tid, t["state"]))
    sends_back = body.verdict in ("rework", "rejected")
    if sends_back and not (body.note or "").strip():
        raise HTTPException(422, "a %s needs a note: say what is wrong, so the next run can fix it"
                            % body.verdict)
    import datetime as _dt
    rec = {"verdict": body.verdict, "by": body.by, "note": body.note,
           "at": _dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    state_before = t["state"]
    t = tasks.annotate(b, tid, "review", rec)
    if sends_back:
        # Back on the task list with the note; the next run's prompt carries it
        # (daemon.prompt_with_feedback) and the branch keeps the earlier attempts.
        fb = list(t.get("feedback") or []) + [{**rec, "round": len(t.get("feedback") or []) + 1}]
        tasks.annotate(b, tid, "feedback", fb)
        t = tasks.transition(b, tid, tasks.HELD, selection_id=None)
    from . import ledger, memory
    cwd = (daemon.load_config().get(b) or {}).get("cwd")
    memory_cleared = False
    if cwd:
        if sends_back:
            memory.record(cwd, t, "sent_back", verdict=body.verdict, by=body.by, note=body.note,
                          round=len(t.get("feedback") or []))
        elif body.verdict == "merged":
            # The final acceptance. Memory is kept unless the user switched clearing on.
            memory.record(cwd, t, "accepted", by=body.by, note=body.note)
            memory_cleared = memory.accepted(cwd, tid)
    ledger.write("task_review", t.get("run_id") or "pool", tid, bucket=b, **rec,
                 gate_exit=t.get("gate_exit"), state=state_before, branch=t.get("branch"),
                 returned_to_held=sends_back, round=len(t.get("feedback") or []),
                 memory_cleared=memory_cleared,
                 slice_id=t.get("slice_id"), session_id=t.get("session_id"))
    return t


class Answer(BaseModel):
    answer: str = Field(min_length=1, max_length=4000)
    by: str = Field(min_length=1, max_length=80)


@router.post("/buckets/{b}/tasks/{tid}/answer")
def answer(b: str, tid: str, body: Answer) -> dict:
    """A person answers the question an agent asked (after no model above it could).

    The answer is kept on the task and in its memory, and every later run's prompt carries
    it. The task stays Held: select and approve it to run again.
    """
    t = _task(b, tid)
    q = t.get("question_open")
    if not q:
        raise HTTPException(409, "task %s has no open question" % tid)
    import datetime as _dt
    rec = {"question": q.get("question"), "asked_by": q.get("asked_by"), "answer": body.answer.strip(),
           "by": body.by, "at": _dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    tasks.annotate(b, tid, "answers", list(t.get("answers") or []) + [rec])
    tasks.annotate(b, tid, "question_open", None)
    t = tasks.get(b, tid)
    from . import ledger, memory
    ledger.write("question_answered", t.get("run_id") or "pool", tid, bucket=b, **rec)
    cwd = (daemon.load_config().get(b) or {}).get("cwd")
    if cwd:
        memory.record(cwd, t, "answer", **rec)
    return t


@router.post("/buckets/{b}/tasks/{tid}/reask", status_code=202)
def reask(b: str, tid: str) -> dict:
    """Send the open question back to the review tiers (they may settle a tooling blocker,
    such as a broken check, themselves). Runs in the background; poll the task."""
    t = _task(b, tid)
    if not t.get("question_open"):
        raise HTTPException(409, "task %s has no open question" % tid)
    threading.Thread(target=lambda: _quiet(daemon.reask, b, tid), daemon=True).start()
    return {"id": tid, "status": "asking the reviewers"}


def _quiet(fn, *a):
    try:
        fn(*a)
    except Exception as e:                      # a background job: record, never crash the server
        from . import ledger
        ledger.write("reask_failed", "pool", a[-1] if a else None, error=str(e)[:500])


@router.get("/buckets/{b}/questions")
def open_questions(b: str) -> list[dict]:
    """Every task in the bucket waiting for a person's answer."""
    return [{"id": t["id"], "prompt": t["prompt"], **(t.get("question_open") or {})}
            for t in tasks.all_(_bucket(b)).values() if t.get("question_open")]


# ----------------------------------------------------------------- plans (N3)
# A thin transport over plans.py (final on disk; never modified here). POST /plans returns
# 202 immediately — plans.run_planner calls a frontier model and can take minutes — and runs
# it in a daemon thread; the stored plan shows status "planning" until that thread finishes.

def _plan(b: str, pid: str) -> dict:
    p = plans.get(_bucket(b), pid)
    if p is None:
        raise HTTPException(404, "no plan %s in bucket %s" % (pid, b))
    return p


def _plan_view(bucket: str, plan: dict) -> dict:
    """The plan with each task's live state/gate/check fields merged in from tasks.get."""
    out = dict(plan)
    rows = []
    for r in plan["tasks"]:
        t = tasks.get(bucket, r["task_id"]) or {}
        rows.append({**r, "state": t.get("state"), "gate_exit": t.get("gate_exit"),
                     "check_before_exit": t.get("check_before_exit"),
                     "check_after_exit": t.get("check_after_exit"),
                     "question_open": t.get("question_open")})
    out["tasks"] = rows
    return out


def _plan_call(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except plans.PlanError as e:
        raise HTTPException(409, str(e))


class PlanCreate(BaseModel):
    goal: str = Field(min_length=1, max_length=20000)
    by: str = Field(min_length=1, max_length=80)


class PlanAnswer(BaseModel):
    answers: list[str]
    by: str = Field(min_length=1, max_length=80)


class PlanApprove(BaseModel):
    by: str = Field(min_length=1, max_length=80)


class PlanReject(BaseModel):
    by: str = Field(min_length=1, max_length=80)
    note: str = Field(min_length=1, max_length=2000)

    @field_validator("note")
    @classmethod
    def _note_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("a rejection needs a note saying why")
        return v


@router.get("/buckets/{b}/plans")
def list_plans(b: str) -> list[dict]:
    return [_plan_view(_bucket(b), p) for p in plans.all_(_bucket(b))]


@router.get("/buckets/{b}/plans/{pid}")
def get_plan(b: str, pid: str) -> dict:
    return _plan_view(_bucket(b), _plan(b, pid))


@router.post("/buckets/{b}/plans", status_code=202)
def create_plan(b: str, body: PlanCreate) -> dict:
    bucket = _bucket(b)
    try:
        p = plans.start(bucket, body.goal, body.by)
    except plans.PlanError as e:
        raise HTTPException(409, str(e))
    threading.Thread(target=plans.run_planner, args=(bucket, p["id"]), daemon=True).start()
    return {"id": p["id"], "status": p["status"]}


@router.post("/buckets/{b}/plans/{pid}/answer")
def answer_plan(b: str, pid: str, body: PlanAnswer) -> dict:
    _plan(b, pid)
    return _plan_call(plans.answer, _bucket(b), pid, body.answers, body.by)


@router.post("/buckets/{b}/plans/{pid}/approve")
def approve_plan(b: str, pid: str, body: PlanApprove) -> dict:
    _plan(b, pid)
    return _plan_call(plans.approve, _bucket(b), pid, body.by)


@router.post("/buckets/{b}/plans/{pid}/reject")
def reject_plan(b: str, pid: str, body: PlanReject) -> dict:
    _plan(b, pid)
    return _plan_call(plans.reject, _bucket(b), pid, body.by, body.note)


# ----------------------------------------------------------------- QA: the human per case (D61)

class QAVerdict(BaseModel):
    case_id: str = Field(min_length=1, max_length=40)
    verdict: str = Field(pattern="^(PASS|FAIL|BLOCKED)$")
    by: str = Field(min_length=1, max_length=80)
    note: str | None = Field(default=None, max_length=2000)


@router.post("/buckets/{b}/tasks/{tid}/qa")
def qa_verdict(b: str, tid: str, body: QAVerdict) -> dict:
    """A person's verdict on ONE QA case, logged beside the AI's verdict on the same case.

    The pair is the verification-accuracy label: did the model that said "pass" see what a
    person sees? AUTO cases carry the chain's implied verdict (pass if the chain approved).
    """
    t = _task(b, tid)
    case = next((c for c in t.get("qa_cases") or [] if c.get("id") == body.case_id), None)
    if case is None:
        raise HTTPException(404, "task %s has no QA case %s" % (tid, body.case_id))
    cls = str(case.get("class", "MANUAL")).upper()
    ai = case.get("ai_verdict") or ("pass" if cls == "AUTO" and t.get("review_final") == "approved"
                                    else None)
    import datetime as _dt
    rec = {"verdict": body.verdict, "by": body.by, "note": body.note, "class": cls,
           "ai_verdict": ai, "at": _dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    results = dict(t.get("qa_results") or {})
    results[body.case_id] = rec
    tasks.annotate(b, tid, "qa_results", results)
    from . import ledger
    agree = None if ai is None else ((ai == "pass") == (body.verdict == "PASS"))
    ledger.write("qa_result", t.get("run_id") or "pool", tid, bucket=b, case_id=body.case_id,
                 case_class=cls, ai_verdict=ai, human_verdict=body.verdict, agree=agree,
                 by=body.by, note=body.note, screenshot=case.get("screenshot"),
                 test=case.get("test"), review_final=t.get("review_final"))
    return {"case_id": body.case_id, **rec, "agree": agree}


@router.get("/buckets/{b}/tasks/{tid}/evidence")
def evidence_file(b: str, tid: str, path: str = Query(...)):
    """A screenshot the verifier committed, read from the task's commit, never the work tree.

    Only files under qa/evidence/<task>/ are served, so this cannot read anything else.
    """
    from fastapi.responses import Response
    t = _task(b, tid)
    p = path.replace("\\", "/")
    if not p.startswith("qa/evidence/%s/" % tid) or ".." in p.split("/"):
        raise HTTPException(400, "only this task's qa/evidence/ files are served")
    cwd = (daemon.load_config().get(b) or {}).get("cwd")
    if not (cwd and t.get("commit")):
        raise HTTPException(404, "no commit recorded for this task")
    import subprocess
    r = subprocess.run(["git", "show", "%s:%s" % (t["commit"], p)], cwd=cwd, capture_output=True)
    if r.returncode:
        raise HTTPException(404, "not in the task's commit: %s" % p)
    kind = "image/png" if p.endswith(".png") else "image/jpeg" if p.endswith((".jpg", ".jpeg")) \
        else "application/octet-stream"
    return Response(content=r.stdout, media_type=kind)


@router.get("/buckets/{b}/tasks/{tid}/evidence/meta")
def evidence_meta(b: str, tid: str) -> dict:
    """The evidence files with what each one shows: the verifier's captions.json, and which
    files are byte-for-byte the same image (git's blob id), so a duplicate is never shown as
    a second piece of proof."""
    t = _task(b, tid)
    cwd = (daemon.load_config().get(b) or {}).get("cwd")
    if not (cwd and t.get("commit")):
        return {"files": [], "captions": {}}
    code, out = daemon._git(cwd, "ls-tree", "-r", t["commit"], "--", "qa/evidence/%s/" % tid)
    rows = []
    for line in (out.splitlines() if code == 0 else []):
        meta, _, path = line.partition("\t")
        if path:
            rows.append({"path": path, "blob": meta.split()[-1]})
    rows.sort(key=lambda r: r["path"])
    first = {}
    for r in rows:
        r["same_as"] = first.get(r["blob"])
        first.setdefault(r["blob"], r["path"])
    captions = {}
    cap = next((r for r in rows if r["path"].endswith("/captions.json")), None)
    if cap:
        code, text = daemon._git(cwd, "show", "%s:%s" % (t["commit"], cap["path"]))
        try:
            captions = {str(k): str(v) for k, v in json.loads(text).items()} if code == 0 else {}
        except (ValueError, AttributeError):
            captions = {}
    return {"files": rows, "captions": captions}


@router.get("/buckets/{b}/tasks/{tid}/evidence/list")
def evidence_list(b: str, tid: str) -> list[str]:
    """Every file the verifier committed under qa/evidence/<task>/, from the task's commit."""
    t = _task(b, tid)
    cwd = (daemon.load_config().get(b) or {}).get("cwd")
    if not (cwd and t.get("commit")):
        return []
    code, out = daemon._git(cwd, "ls-tree", "-r", "--name-only", t["commit"],
                            "--", "qa/evidence/%s/" % tid)
    return sorted(l for l in out.splitlines() if l.strip()) if code == 0 else []
