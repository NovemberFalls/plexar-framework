"""The sweeper runs approved plans. Called by daemon.pass_once for each bucket before leasing.

    tick(bucket, cfg)     start ready tasks (batches of up to BATCH), requeue failures with
                          feedback (one lane up on the 3rd attempt, then blocked), and give the
                          finished plan its final review

The plan was approved once by a named person, so each batch is selected AND approved here in
that person's name ("<by> (plan <id>)"). Nothing else is loosened: each task's own check, the
bucket gate and the review chain still decide whether it lands.

Branches. Every plan task is cut from the PLAN BRANCH `plexar/plan/<id>` (created from the
branch the repo is on the first time, recorded as plan.base). A task that lands is merged into
the plan branch with --no-ff, so its dependents run on top of it. The person's base branch is
never written to; merging the plan branch into it is the person's job. enter/land/leave are
called by daemon._finish INSIDE the claimed cwd, so no other run can move the checkout meanwhile.
"""
from __future__ import annotations

from . import chain, ledger, plans, pool, tasks

BATCH = 5
MAX_ATTEMPTS = 3
BUMP = {"mundane": "workhorse", "workhorse": "critical", "critical": "critical"}
DIFF_CAP = 60000
MERGE_CONFLICT = "merge conflict into plan branch"

FINAL = """FINAL REVIEW. You planned this work; every task in the plan is now done and merged
into the plan branch {branch} (cut from {base}). Judge whether the goal is met, as a whole.

GOAL:
{goal}

TASKS (each with its own check, which failed before the work and passed after):
{tasks}

DIFF {base}...{branch}:
{stat}

{diff}

End with ONE JSON object and nothing after it:
{{"verdict": "approve" | "reject", "judgement": "<why, two or three sentences>"}}
"""


def _d():
    from . import daemon                  # daemon imports this module; bind late
    return daemon


# ----------------------------------------------------------------- daemon hooks

def enter(bucket: str, task: dict, cfg: dict) -> dict | None:
    """Put the repo on the task's plan branch before the task branch is cut from it.

    Returns the context `land` and `leave` need, or None when there is nothing to do (not a
    plan task, no git, or a dirty tree: execute then refuses the run as it always does).
    """
    pid = task.get("plan_id")
    d = _d()
    if not pid or not (cfg.get("git", True) and d._is_git(cfg["cwd"])):
        return None
    plan = plans.get(bucket, pid)
    if plan is None:
        return None
    cwd = cfg["cwd"]
    code, out = d._git(cwd, "status", "--porcelain")
    if code or out:
        return None
    cur = d._git(cwd, "rev-parse", "--abbrev-ref", "HEAD")[1]
    base = plan.get("base") or cur
    exists = d._git(cwd, "rev-parse", "--verify", "-q", "refs/heads/" + plan["branch"])[0] == 0
    if exists:
        code, out = d._git(cwd, "checkout", "-q", plan["branch"])
    else:
        code, out = d._git(cwd, "checkout", "-q", "-b", plan["branch"], base)
    if code:
        raise RuntimeError("could not switch to plan branch %s: %s" % (plan["branch"], out[:300]))
    if not plan.get("base"):
        plans.save(bucket, pid, base=base)
    return {"plan_id": pid, "branch": plan["branch"], "base": base, "was_on": cur}


def land(bucket: str, task: dict, cfg: dict, ctx: dict, ok: bool, gate_exit, evidence: dict):
    """After the run: merge a landed task branch into the plan branch. Returns (ok, evidence).

    On a conflict the merge is aborted, the task fails with MERGE_CONFLICT and the plan is
    blocked: two tasks disagreeing about the same lines is a person's call, not a retry.
    """
    if not (ok and gate_exit == 0 and evidence.get("commit")):
        return ok, evidence
    d, cwd = _d(), cfg["cwd"]
    tb = evidence.get("branch") or "plexar/%s" % task["id"]
    d._git(cwd, "checkout", "-q", ctx["branch"])
    code, out = d._git(cwd, "-c", "user.name=Plexar daemon", "-c", "user.email=daemon@plexar.local",
                       "merge", "--no-ff", "-q", "-m",
                       "[plexar][plan %s] merge %s (%s)" % (ctx["plan_id"], task["id"], task.get("plan_key")),
                       tb)
    if code:
        d._git(cwd, "merge", "--abort")
        err = "%s: %s" % (MERGE_CONFLICT, out[:300])
        plans.save(bucket, ctx["plan_id"], status="blocked",
                   error="task %s (%s): %s" % (task.get("plan_key"), task["id"], MERGE_CONFLICT))
        ledger.write("plan_merge", ctx["plan_id"], task["id"], bucket=bucket, merged=False,
                     branch=tb, into=ctx["branch"], detail=out[:400])
        ledger.write("plan_blocked", ctx["plan_id"], task["id"], bucket=bucket, reason=MERGE_CONFLICT)
        return False, {**evidence, "error": err, "plan_merged": False}
    head = d._git(cwd, "rev-parse", "HEAD")[1]
    ledger.write("plan_merge", ctx["plan_id"], task["id"], bucket=bucket, merged=True,
                 branch=tb, into=ctx["branch"], merge_commit=head)
    return ok, {**evidence, "plan_merged": True, "plan_merge_commit": head}


def leave(cfg: dict, ctx: dict) -> None:
    """Always put the repo back on the branch it was on before the plan task (plan.base)."""
    _d()._git(cfg["cwd"], "checkout", "-q", ctx["base"])


# ----------------------------------------------------------------- the sweep

def _feedback_note(t: dict) -> str:
    parts = []
    if t.get("error"):
        parts.append("Error: %s" % t["error"])
    parts.append("Gate exit %s." % t.get("gate_exit"))
    if t.get("gate_output"):
        parts.append("Gate said:\n%s" % t["gate_output"][-1500:])
    if t.get("check"):
        parts.append("Your check `%s` exited %s after the work." % (t["check"], t.get("check_after_exit")))
        if t.get("check_after_output"):
            parts.append("It said:\n%s" % t["check_after_output"][-1500:])
    return "\n".join(parts)


def tick(bucket: str, cfg: dict) -> dict:
    report = {"started": [], "requeued": [], "blocked": [], "reviewed": []}
    for plan in plans.all_(bucket):
        if plan["status"] in ("approved", "running"):
            _sweep(bucket, cfg, plan, report)
    return report


def _sweep(bucket: str, cfg: dict, plan: dict, report: dict) -> None:
    pid = plan["id"]
    rows = [dict(r) for r in plan["tasks"]]
    live = {r["task_id"]: tasks.get(bucket, r["task_id"]) or {} for r in rows}

    # 2. failures: retry with feedback, one lane up on the 3rd attempt, then stop for a person.
    changed = False
    for r in rows:
        t = live[r["task_id"]]
        if t.get("state") != tasks.FAILED or t.get("question_open"):
            continue
        if MERGE_CONFLICT in str(t.get("error") or ""):
            plans.save(bucket, pid, tasks=rows, status="blocked",
                       error="task %s (%s): %s" % (r["key"], r["task_id"], MERGE_CONFLICT))
            report["blocked"].append(pid)
            return
        r["attempts"] = int(r.get("attempts") or 0) + 1
        changed = True
        if r["attempts"] >= MAX_ATTEMPTS:
            err = "task %s (%s) failed %d times; a person must look at it" % (
                r["key"], r["task_id"], r["attempts"])
            plans.save(bucket, pid, tasks=rows, status="blocked", error=err)
            ledger.write("plan_blocked", pid, r["task_id"], bucket=bucket, reason=err,
                         attempts=r["attempts"], lane=r["lane"])
            report["blocked"].append(pid)
            return
        if r["attempts"] == MAX_ATTEMPTS - 1:
            r["lane"] = BUMP[r["lane"]]
        note = _feedback_note(t)
        pool.requeue(bucket, r["task_id"])
        fb = list(t.get("feedback") or []) + [{
            "round": r["attempts"], "verdict": "failed", "by": "sweeper (plan %s)" % pid,
            "note": note}]
        tasks.annotate(bucket, r["task_id"], "feedback", fb)
        tasks.annotate(bucket, r["task_id"], "lane", r["lane"])
        live[r["task_id"]] = tasks.get(bucket, r["task_id"]) or {}
        ledger.write("plan_task_requeued", pid, r["task_id"], bucket=bucket, attempts=r["attempts"],
                     lane=r["lane"], gate_exit=t.get("gate_exit"), feedback=note[:2000])
        report["requeued"].append(r["task_id"])
    if changed:
        plans.save(bucket, pid, tasks=rows)

    # 3. everything done: the final review, then done or rejected.
    if all(live[r["task_id"]].get("state") == tasks.DONE for r in rows):
        _final(bucket, cfg, plans.save(bucket, pid, status="review"), report)
        return

    # 1. ready: HELD, no open question, every dep DONE and merged into the plan branch.
    def merged(tid):
        x = live.get(tid) or tasks.get(bucket, tid) or {}
        return x.get("state") == tasks.DONE and x.get("plan_merged")
    ready = [r["task_id"] for r in rows
             if live[r["task_id"]].get("state") == tasks.HELD
             and not live[r["task_id"]].get("question_open")
             and all(merged(d) for d in r["deps"])][:BATCH]
    if not ready:
        return
    n = len(plan.get("batches") or []) + 1
    sel = pool.select(bucket, ready, "plan %s batch %d" % (pid, n), "sweeper")
    if sel["approval"] == "pending":
        pool.approve(bucket, sel["selection_id"], "%s (plan %s)" % (plan["approved_by"], pid))
    plans.save(bucket, pid, status="running",
               batches=list(plan.get("batches") or []) + [ready])
    ledger.write("plan_batch", pid, None, bucket=bucket, batch=n, tasks=ready,
                 selection_id=sel["selection_id"], approved_by=plan["approved_by"])
    report["started"] += ready


def _final(bucket: str, cfg: dict, plan: dict, report: dict) -> None:
    """The frontier's final say on the whole plan branch. Never merges into the base."""
    d, pid, cwd = _d(), plan["id"], cfg["cwd"]
    argv = list((cfg.get("review") or {}).get("approver") or plan.get("planner") or [])
    if not argv:
        plans.save(bucket, pid, status="done", final=None)
        ledger.write("plan_done", pid, None, bucket=bucket, reviewed=False)
        report["reviewed"].append(pid)
        return
    if not d._claim_cwd(cwd):
        plans.save(bucket, pid, status="running")      # try again next tick
        return
    try:
        base, branch = plan.get("base") or "HEAD", plan["branch"]
        _, stat = d._git(cwd, "diff", "--stat", "%s...%s" % (base, branch))
        _, diff = d._git(cwd, "diff", "%s...%s" % (base, branch))
        lines = []
        for r in plan["tasks"]:
            t = tasks.get(bucket, r["task_id"]) or {}
            lines.append("- %s [%s] deps=%s check=`%s` before=%s after=%s gate=%s\n  %s" % (
                r["key"], r["lane"], r["deps"], r.get("check"), t.get("check_before_exit"),
                t.get("check_after_exit"), t.get("gate_exit"), r["prompt"][:600]))
        text = FINAL.format(branch=branch, base=base, goal=plan["goal"], tasks="\n".join(lines),
                            stat=stat, diff=diff[:DIFF_CAP])
        import subprocess
        try:
            res = subprocess.run(argv, cwd=cwd, input=text.encode("utf-8"), capture_output=True,
                                 timeout=plans.PLANNER_TIMEOUT_S)
            out = res.stdout.decode("utf-8", "replace") + res.stderr.decode("utf-8", "replace")
        except (OSError, subprocess.TimeoutExpired) as e:
            out = "could not run the final reviewer: %s" % e
        _reset(cwd)
    finally:
        d._release_cwd(cwd)
    v = chain.parse_verdict(out, {"approve", "reject"})
    final = {"verdict": v["verdict"], "judgement": v.get("judgement") or "",
             "by_model": chain.model_of(argv)}
    status = {"approve": "done", "reject": "rejected"}.get(v["verdict"], "blocked")
    plans.save(bucket, pid, status=status, final=final,
               error=None if status != "blocked" else "final review gave no verdict: %s" % final["judgement"])
    ledger.write("plan_review", pid, None, bucket=bucket, verdict=final["verdict"],
                 judgement=final["judgement"], by_model=final["by_model"], diff_chars=len(diff))
    if status == "done":
        ledger.write("plan_done", pid, None, bucket=bucket, reviewed=True)
    report["reviewed"].append(pid)


def _reset(cwd: str) -> None:
    """A reviewer judges; it never changes the checkout. Discard anything it left."""
    d = _d()
    _, st = d._git(cwd, "status", "--porcelain")
    if st:
        d._git(cwd, "checkout", "--", ".")
        d._git(cwd, "clean", "-fdq")
