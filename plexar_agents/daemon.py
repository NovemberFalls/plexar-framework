"""B3n — the daemon leases work off the pool and runs it. D16, D25, D26.

    python -m plexar_agents.daemon            # loop forever
    python -m plexar_agents.daemon --once     # one pass: recover, expire, lease, run, exit
    python -m plexar_agents.daemon configure BUCKET --cwd DIR --runner CMD --gate CMD
    python -m plexar_agents.daemon show       # what is configured, and what is runnable

**What it runs is configured on disk, never over the API.** A bucket is runnable only when
`daemon.json` (next to the task store) names three things for it:

    {"buckets": {"my-app": {
        "cwd":    "C:\\\\src\\\\my-app",
        "runner": ["claude", "-p", "--permission-mode", "acceptEdits"],
        "gate":   ["python", "-m", "pytest", "-q"]}}}

The prompt reaches the runner on STDIN, byte-identical (`claude -p` reads it there), and
`{prompt_file}` in the argv is replaced by a path to the same bytes, for runners that
want a file. A runner
command settable over HTTP would be remote code execution for anything that can reach the
port — so the API can read this file's effect and never write it.

**The gate decides, not the runner.** `claude -p` exits 0 whether or not the work is right;
a runner's exit code is its opinion. After the runner, `gate` runs in the same `cwd`, and
ITS exit code is `gate_exit`. A bucket with no gate is not runnable — a task that cannot
be checked cannot land, and the daemon says so rather than guessing.

**Dying is survivable.** Every running task is heartbeated. If this process is killed the
beats stop, and the next daemon's startup `recover()` returns those tasks to HELD as
ABANDONED-then-requeued — never as failed, because nobody took a verdict.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import tempfile
import threading
import time

from . import ask, cards, chain, ledger, memory, pool, sweeper, tasks

BEAT_EVERY_S = 30.0
POLL_S = 5.0

# ONE TASK PER WORKING TREE AT A TIME. Found by compose_m2e: two tasks of one bucket ran
# concurrently in the same cwd, the second overwrote the first's file, and the first's
# gate judged the second's work. The pool's concurrency cap bounds *agents*; it says
# nothing about two agents sharing a checkout. Until runs get their own worktree, a cwd
# is a mutex. In-process only: run ONE daemon per machine.
_busy: set[str] = set()
_busy_lock = threading.Lock()


def _claim_cwd(cwd: str) -> bool:
    key = os.path.normcase(os.path.abspath(cwd))
    with _busy_lock:
        if key in _busy:
            return False
        _busy.add(key)
        return True


def _release_cwd(cwd: str) -> None:
    with _busy_lock:
        _busy.discard(os.path.normcase(os.path.abspath(cwd)))


def config_path() -> pathlib.Path:
    return tasks.dir_().parent / "daemon.json"


def load_config() -> dict:
    try:
        return json.loads(config_path().read_text(encoding="utf-8")).get("buckets", {})
    except (OSError, ValueError):
        return {}


def runnable(cfg: dict) -> str | None:
    """None if the bucket can run; otherwise the reason it cannot, stated."""
    if not cfg.get("cwd") or not pathlib.Path(cfg["cwd"]).is_dir():
        return "cwd missing or not a directory"
    if not isinstance(cfg.get("runner"), list) or not cfg["runner"]:
        return "runner must be a non-empty argv list"
    if not isinstance(cfg.get("gate"), list) or not cfg["gate"]:
        return "no gate — a task that cannot be checked cannot land"
    return None


def _work_dir(run_id: str) -> pathlib.Path:
    d = tasks.dir_().parent / "daemon-runs" / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _run(argv: list[str], cwd: str, log: pathlib.Path,
         stdin: pathlib.Path | None = None, env: dict | None = None) -> int:
    with open(log, "ab") as fh:
        fh.write(("\n$ %s\n" % " ".join(argv)).encode("utf-8"))
        fh.flush()
        try:
            if stdin is None:
                return subprocess.run(argv, cwd=cwd, stdout=fh, stderr=subprocess.STDOUT,
                                      stdin=subprocess.DEVNULL, env=env).returncode
            with open(stdin, "rb") as src:
                return subprocess.run(argv, cwd=cwd, stdout=fh, stderr=subprocess.STDOUT,
                                      stdin=src, env=env).returncode
        except OSError as e:
            fh.write(("could not start: %s\n" % e).encode("utf-8"))
            return 127


def _git(cwd: str, *args: str) -> tuple[int, str]:
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    except OSError as e:
        return 127, str(e)
    return r.returncode, (r.stdout + r.stderr).strip()


def _is_git(cwd: str) -> bool:
    return _git(cwd, "rev-parse", "--is-inside-work-tree") == (0, "true")


class TreeNotClean(RuntimeError):
    """The repo has uncommitted changes nobody attributed to a task. Refuse to run."""


def _branch_in(cwd: str, task: dict) -> dict:
    """One branch per task, cut from wherever the repo is. The base is never written to.

    A dirty tree is REFUSED, not stashed: those changes are someone's, and a stash the
    daemon made and then forgot is the most common way work silently disappears.
    """
    code, out = _git(cwd, "status", "--porcelain")
    if code or out:
        raise TreeNotClean("working tree of %s is not clean — commit or discard first:\n%s"
                           % (cwd, out[:400]))
    _, base = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    _, base_sha = _git(cwd, "rev-parse", "HEAD")
    branch = "plexar/%s" % task["id"]
    exists = _git(cwd, "rev-parse", "--verify", "-q", "refs/heads/" + branch)[0] == 0
    if exists:
        # A task sent back by a person (rework/reject with a note) continues on its own
        # branch: the next attempt builds on the last one, and the diff still starts at the
        # point it left the base.
        code, out = _git(cwd, "checkout", "-q", branch)
        if code == 0:
            base_sha = _git(cwd, "merge-base", base_sha, branch)[1] or base_sha
    else:
        code, out = _git(cwd, "checkout", "-q", "-b", branch)
    if code:
        raise TreeNotClean("could not %s %s: %s" % ("switch to" if exists else "create", branch, out))
    return {"branch": branch, "base": base, "base_sha": base_sha}


def _commit_and_return(cwd: str, task: dict, git: dict, gate_exit) -> dict:
    """Commit whatever the run left — pass or fail — then put the repo back on its base.

    A failed run is committed too: it is the evidence of what went wrong, and it lives
    on its own branch where nothing will merge it by accident. Merging is a human act.
    """
    _git(cwd, "add", "-A")
    title = (task["prompt"].strip().splitlines() or [""])[0][:60]
    msg = "[plexar][%s] %s\n\ngate_exit=%s run=%s" % (task["id"], title, gate_exit,
                                                     task.get("run_id"))
    code, _ = _git(cwd, "-c", "user.name=Plexar daemon", "-c", "user.email=daemon@plexar.local",
                   "commit", "-q", "-m", msg)
    head = _git(cwd, "rev-parse", "HEAD")[1]
    # With a review chain there are earlier attempt commits, so "nothing new to commit" is
    # not "no commit": the branch head is the evidence whenever it moved off the base.
    commit = head if (code == 0 or head != git.get("base_sha")) else None
    _git(cwd, "checkout", "-q", git["base"])
    return {**git, "commit": commit}


# ----------------------------------------------------------------- telemetry
#
# Every task the daemon runs is written as a §10-shaped (dispatch, outcome, run) triple
# under the task's own run_id, so a daemon run is exactly as trainable as an orchestrated
# one. The fields are chosen for the classifiers this data exists to train (feedback:
# maximal internal telemetry): the INPUT (the prompt as a hashed card, where it came from),
# the DECISIONS around it (who selected it, why, with what batch, who approved, how long
# each wait was), and the OUTCOME (exits, timings, the size and shape of the diff). The
# human verdict on the result arrives later as `task_review` (api.review).

def _phase_times(task: dict) -> dict:
    first = {}
    for h in task.get("history", []):
        first.setdefault(h.get("to"), h.get("at"))
    import datetime as _dt

    def secs(a, b):
        try:
            return round((_dt.datetime.fromisoformat(first[b]) -
                          _dt.datetime.fromisoformat(first[a])).total_seconds())
        except (KeyError, TypeError, ValueError):
            return None
    return {"created_to_held_s": secs("drafted", "held"),
            "held_to_selected_s": secs("held", "selected"),
            "selected_to_running_s": secs("selected", "running")}


def _log_dispatch(bucket: str, task: dict, cfg: dict) -> dict:
    run_id = task["run_id"]
    card = cards.write(run_id, "task", task["prompt"])
    sid = task.get("selection_id")
    sel = (pool.selection(bucket, sid) if sid else None) or {}
    ledger.write("dispatch", run_id, task["id"], worker="daemon", phase="task",
                 lane=os.path.basename(cfg["runner"][0]), effort=None,
                 lane_reason="bucket runner configured on disk (D53)",
                 runner=cfg["runner"], gate=cfg["gate"], bucket=bucket, cwd=cfg["cwd"],
                 brief_path=card["path"], brief_sha256=card["sha256"],
                 prompt_sha256=task.get("prompt_sha256"), prompt_chars=len(task["prompt"]),
                 brief_author_model=None, brief_source=task.get("source"),
                 slice_id=task.get("slice_id"), candidate_id=task.get("candidate_id"),
                 group=task.get("group"), session_id=task.get("session_id"),
                 selection_id=sid, selection_reason=sel.get("reason"),
                 selection_size=len(sel.get("tasks") or []), selection_agent=sel.get("agent"),
                 approval=sel.get("approval"), approved_by=sel.get("decided_by"),
                 attempt=sum(1 for h in task.get("history", []) if h.get("to") == "running"),
                 files_owned=None, spec_sections=None, diffable=None, retry_mode=card.get("retry_mode"),
                 pred=None, **_phase_times(task))
    return card


def _diff_stats(cwd: str, evidence: dict) -> dict:
    base, commit = evidence.get("base_sha"), evidence.get("commit")
    if not (base and commit):
        return {"files_touched": None, "diff_files": None, "diff_insertions": None,
                "diff_deletions": None}
    _, names = _git(cwd, "diff", "--name-only", base, commit)
    _, num = _git(cwd, "diff", "--numstat", base, commit)
    ins = dele = 0
    for line in num.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            ins += int(parts[0])
            dele += int(parts[1])
    files = [n for n in names.splitlines() if n.strip()]
    return {"files_touched": files, "diff_files": len(files), "diff_insertions": ins,
            "diff_deletions": dele}


def _log_outcome(bucket: str, task: dict, cfg: dict, runner_exit, gate_exit,
                 evidence: dict, wall_ms: int, log: pathlib.Path) -> None:
    run_id = task["run_id"]
    refused = evidence.get("refused")
    chained = evidence.get("review_final")
    landed = runner_exit == 0 and gate_exit == 0 and chained in (None, "approved")
    status = ("DONE" if landed else "BLOCKED" if refused
              else "LADDER_EXHAUSTED" if chained == "LADDER_EXHAUSTED" else "GATE_FAIL")
    try:
        log_bytes = log.stat().st_size
    except OSError:
        log_bytes = None
    ledger.write("outcome", run_id, task["id"], status=status,
                 blocked_question=refused, blocked_cause="environment" if refused else None,
                 brief_defect=None, attempts=evidence.get("review_attempts") or 1,
                 escalated_to="human" if chained == "LADDER_EXHAUSTED" else None,
                 scope_violation=None, review_final=chained, qa_file=evidence.get("qa_file"),
                 node_check_cmd=" ".join(cfg["gate"]), node_check_exit=gate_exit,
                 runner_exit=runner_exit, wall_ms=wall_ms, branch=evidence.get("branch"),
                 commit=evidence.get("commit"), base_sha=evidence.get("base_sha"),
                 log_path=str(log), log_bytes=log_bytes, bucket=bucket,
                 **_diff_stats(cfg["cwd"], evidence))
    ledger.write("run", run_id, "RUN", status="COMPLETED" if not refused else "ABANDONED",
                 gate_exit=gate_exit, gate_failing_check=None if gate_exit == 0 else " ".join(cfg["gate"]),
                 gate_evidence="daemon task %s in %s" % (task["id"], bucket),
                 gate_verified_against_defect=False, interfaces_declared_uncalled_at_landing=[],
                 orchestrator_repairs=0, orchestrator_repair_notes=None, wall_ms=wall_ms)


def runner_for(cfg: dict, task: dict) -> list[str]:
    """A plan task runs with its lane's command (cfg "lanes"), else the bucket's runner."""
    lane = (cfg.get("lanes") or {}).get(task.get("lane")) if task.get("plan_id") else None
    if isinstance(lane, str) and lane.strip():
        return _argv(lane)
    return list(lane) if isinstance(lane, list) and lane else cfg["runner"]


def execute(bucket: str, task: dict, cfg: dict) -> tuple[bool, int | None, dict]:
    """Run one leased task to a verdict. Heartbeats while it runs.

    Returns (ok, gate_exit, evidence). In a git repo the run happens on its own branch
    `plexar/<task>` and is committed there; the base branch is untouched.
    """
    cfg = {**cfg, "runner": runner_for(cfg, task)}
    run_id = task["run_id"]
    wd = _work_dir(run_id)
    prompt_file = wd / "prompt.md"
    # The task's own memory, in the project (.plexar/tasks/<id>.json): what earlier runs,
    # checks, reviewers and people said. The agent reads it first and may add notes.
    mem_path = memory.path(cfg["cwd"], task["id"])
    memory.record(cfg["cwd"], task, "run_start", run_id=run_id,
                  runner=chain.model_of(cfg["runner"]), sent_back_rounds=len(task.get("feedback") or []))
    base_prompt = (prompt_with_feedback(task) + "\n---\n" + ask.WORKER_RULE + "\n"
                   + memory_block(cfg["cwd"], task["id"], mem_path))
    prompt_file.write_bytes(base_prompt.encode("utf-8"))
    runner_env = dict(os.environ, PLEXAR_TASK_MEMORY=str(mem_path), PLEXAR_TASK_ID=task["id"])
    log = wd / "output.log"
    evidence: dict = {"run_dir": str(wd), "error": None, "gate_output": None}
    if cfg.get("review"):
        # A task sent back by a person reruns: the previous run's chain result and the
        # per-case verdicts belonged to THAT run (they stay in the log), not this one.
        evidence.update({"review_final": None, "qa_cases": [], "qa_results": {}})
    t0 = time.time()
    _log_dispatch(bucket, task, cfg)
    use_git = cfg.get("git", True) and _is_git(cfg["cwd"])
    if use_git:
        try:
            git = _branch_in(cfg["cwd"], task)
        except TreeNotClean as e:
            log.write_text(str(e) + "\n", encoding="utf-8")
            ledger.write("daemon_refused_dirty", "pool", task["id"], bucket=bucket,
                         cwd=cfg["cwd"], detail=str(e)[:400])
            ev = {**evidence, "refused": str(e)[:400]}
            _log_outcome(bucket, task, cfg, None, None, ev, int((time.time() - t0) * 1000), log)
            return False, None, ev

    stop = threading.Event()

    def beat():
        while not stop.wait(BEAT_EVERY_S):
            pool.beat(bucket, task["id"])
    threading.Thread(target=beat, daemon=True).start()
    runner_exit = gate_exit = None
    review = cfg.get("review") if use_git else None
    if use_git:
        evidence["base_sha"] = git["base_sha"]
    if task.get("check"):
        _stage(bucket, task, "check_before")
        before = _check_on_base(cfg["cwd"], task["check"], git["base_sha"] if use_git else None, log)
        evidence.update({"check": task["check"], "check_before_exit": before[0],
                         "check_before_output": before[1]})
        memory.record(cfg["cwd"], task, "check_before", run_id=run_id, command=task["check"],
                      exit=before[0], output=before[1])
        ledger.write("task_check_before", run_id, task["id"], bucket=bucket, command=task["check"],
                     exit=before[0], proves_something=before[0] not in (0, None))
        if before[0] == 0:
            stop.set()
            evidence["error"] = ("This task's check already passes on the code as it was BEFORE any "
                                 "work, so it cannot tell whether the task was done. Fix the check "
                                 "(it must fail now and pass once the task is done) and requeue.")
            if use_git:
                evidence.update(_commit_and_return(cfg["cwd"], task, git, None))
            memory.record(cfg["cwd"], task, "run_end", run_id=run_id, result="check proves nothing")
            _log_outcome(bucket, task, cfg, None, None, evidence, int((time.time() - t0) * 1000), log)
            return False, None, evidence
    hops: list[dict] = []
    final, feedback, stage_from, attempt = None, None, None, 0
    try:
        max_loops = int((review or {}).get("max_loops", 3))
        while True:
            attempt += 1
            pf = prompt_file
            if feedback:
                pf = wd / ("prompt-attempt%d.md" % attempt)
                pf.write_bytes((base_prompt + chain.WORKER_FEEDBACK.format(
                    attempt=attempt - 1, stage=stage_from, feedback=feedback)).encode("utf-8"))
            argv = [a.replace("{prompt_file}", str(pf)) for a in cfg["runner"]]
            mark = log.stat().st_size if log.exists() else 0
            _stage(bucket, task, "worker", attempt)
            runner_exit = _run(argv, cfg["cwd"], log, stdin=pf, env=runner_env)
            memory.record(cfg["cwd"], task, "runner", run_id=run_id, attempt=attempt, exit=runner_exit)
            why = not_signed_in(argv, runner_exit, _tail_since(log, mark))
            if why:
                memory.record(cfg["cwd"], task, "not_signed_in", run_id=run_id, attempt=attempt)
                # Retrying or reviewing cannot sign anyone in: stop, and say what to do.
                evidence["error"] = why
                ledger.write("runner_not_signed_in", run_id, task["id"], bucket=bucket,
                             runner=os.path.basename(argv[0]) if argv else None, runner_exit=runner_exit)
                break
            q = ask.find_question(_tail_since(log, mark))
            if q:
                got = _ask_up(bucket, task, cfg, q, "worker", run_id, log, runner_env, evidence)
                if not got:
                    break                                   # a person must answer; task goes to Held
                feedback, stage_from = got, "question"
                if attempt >= max_loops:
                    final = "LADDER_EXHAUSTED"
                    break
                continue
            gmark = log.stat().st_size if log.exists() else 0
            _stage(bucket, task, "gate", attempt)
            gate_exit = _run(list(cfg["gate"]), cfg["cwd"], log)
            # What the check itself said, kept apart from the agent's chatter so the board can
            # show "expected X, found Y" without anyone digging through the log.
            said = _tail_since(log, gmark, 4000).strip().splitlines()
            if said and said[0].startswith("$ "):        # _run's own "$ <command>" header
                said = said[1:]
            evidence["gate_output"] = "\n".join(said).strip()[-1500:]
            memory.record(cfg["cwd"], task, "gate", run_id=run_id, attempt=attempt, exit=gate_exit,
                          command=" ".join(cfg["gate"]), output=evidence["gate_output"])
            if task.get("check"):
                cmark = log.stat().st_size if log.exists() else 0
                _stage(bucket, task, "check_after", attempt)
                cexit = _run(_argv(task["check"]), cfg["cwd"], log)
                cout = "\n".join(l for l in _tail_since(log, cmark, 4000).strip().splitlines()
                                 if not l.startswith("$ ")).strip()[-1500:]
                evidence.update({"check_after_exit": cexit, "check_after_output": cout})
                memory.record(cfg["cwd"], task, "check_after", run_id=run_id, attempt=attempt,
                              exit=cexit, output=cout)
                if gate_exit == 0 and cexit != 0:
                    gate_exit = cexit                        # the task is not done until its check passes
            if not review:
                break
            _commit(cfg["cwd"], task, "attempt %d, gate_exit=%s" % (attempt, gate_exit))
            v = _review_stage(bucket, task, cfg, "verifier", review, hops, attempt, wd, log,
                              gate_exit=gate_exit, base_sha=git["base_sha"])
            if v["verdict"] == "question":
                got = _ask_up(bucket, task, cfg, v.get("question") or v.get("judgement"), "verifier",
                              run_id, log, runner_env, evidence)
                if not got:
                    break
                feedback, stage_from = got, "question"
            elif v["verdict"] != "pass":
                feedback, stage_from = v.get("feedback") or v.get("judgement"), "verifier"
            else:
                a = _review_stage(bucket, task, cfg, "approver", review, hops, attempt, wd,
                                  log, gate_exit=gate_exit, base_sha=git["base_sha"], verifier=v)
                if a["verdict"] == "approve":
                    final = "approved"
                    break
                if a["verdict"] == "question":
                    got = _ask_up(bucket, task, cfg, a.get("question") or a.get("judgement"), "approver",
                                  run_id, log, runner_env, evidence)
                    if not got:
                        break
                    feedback, stage_from = got, "question"
                    if attempt >= max_loops:
                        final = "LADDER_EXHAUSTED"
                        break
                    continue
                # The verifier relays the approver's catch to the worker, with its own note.
                feedback = "The approver rejected it: %s\n(The verifier had judged: %s)" % (
                    a.get("feedback") or a.get("judgement"), v.get("judgement"))
                stage_from = "approver"
            if attempt >= max_loops:
                final = "LADDER_EXHAUSTED"
                break
        if review and not evidence.get("error") and not evidence.get("question_open"):
            qa_dir = pathlib.Path(cfg["cwd"]) / "qa"
            qa_dir.mkdir(exist_ok=True)
            qa_file = qa_dir / ("QA-%s.md" % task["id"])
            head = _git(cfg["cwd"], "rev-parse", "HEAD")[1]
            qa_file.write_text(chain.qa_markdown({**task, "branch": git["branch"]}, hops,
                                                 final, head), encoding="utf-8")
            last_cases = next((h["qa_cases"] for h in reversed(hops)
                               if h["stage"] == "verifier" and h.get("qa_cases")), [])
            evidence.update({"review_final": final, "review_hops": len(hops),
                             "qa_cases": last_cases,
                             "review_attempts": attempt,
                             "qa_file": qa_file.relative_to(cfg["cwd"]).as_posix()})
    finally:
        stop.set()
        _stage(bucket, task, None)
        if use_git:
            evidence.update(_commit_and_return(cfg["cwd"], task, git, gate_exit))
    evidence["runner_exit"] = runner_exit
    if review:
        ledger.write("review_chain", run_id, task["id"], bucket=bucket, final=final,
                     attempts=attempt, hops=len(hops),
                     worker_model=chain.model_of(cfg["runner"]),
                     verifier_model=chain.model_of(review.get("verifier") or []),
                     approver_model=chain.model_of(review.get("approver") or []),
                     verdicts=[(h["stage"], h["verdict"]) for h in hops],
                     qa_file=evidence.get("qa_file"))
    ledger.write("daemon_ran", "pool", task["id"], bucket=bucket, task_run_id=run_id,
                 runner_exit=runner_exit, gate_exit=gate_exit, log=str(log),
                 branch=evidence.get("branch"), commit=evidence.get("commit"))
    _log_outcome(bucket, task, cfg, runner_exit, gate_exit, evidence,
                 int((time.time() - t0) * 1000), log)
    # The runner's exit is recorded, not trusted: a runner that crashed but left the gate
    # green is still suspicious, so both must be clean. With a review chain, the approver's
    # approval is required too: a green gate the chain did not approve has not landed.
    ok = runner_exit == 0 and (not review or final == "approved")
    memory.record(cfg["cwd"], task, "run_end", run_id=run_id,
                  result=("landed" if ok and gate_exit == 0 else "did not land"),
                  runner_exit=runner_exit, gate_exit=gate_exit, chain=final, error=evidence.get("error"))
    return ok, gate_exit, evidence


def _commit(cwd: str, task: dict, note: str) -> str | None:
    """Commit what is on the task branch now (one commit per worker attempt)."""
    _git(cwd, "add", "-A")
    title = (task["prompt"].strip().splitlines() or [""])[0][:60]
    code, _ = _git(cwd, "-c", "user.name=Plexar daemon", "-c", "user.email=daemon@plexar.local",
                   "commit", "-q", "-m", "[plexar][%s] %s\n\n%s" % (task["id"], title, note))
    return _git(cwd, "rev-parse", "HEAD")[1] if code == 0 else None


def _reset_tree(cwd: str) -> list[str]:
    """Discard what a REVIEWER left behind. Reviewers judge; they never change the branch.

    Only ever runs on the task branch, after the worker's attempt is committed, so the only
    things it can remove are the reviewer's own scratch files. Returns what it discarded,
    which is logged: a reviewer that edits code is a finding.
    """
    _, st = _git(cwd, "status", "--porcelain", "--untracked-files=all")
    # qa/evidence/ is the one place a reviewer may write: screenshots it captured and
    # looked at. It is kept and committed with the QA file; everything else is discarded.
    touched = [l[3:] for l in st.splitlines()
               if l.strip() and not l[3:].replace("\\", "/").startswith("qa/evidence/")]
    if touched:
        _git(cwd, "checkout", "--", ".")
        _git(cwd, "clean", "-fdq", "-e", "qa/evidence/")
    return touched


def _stage(bucket: str, task: dict, name: str | None, attempt: int = 0) -> None:
    """Record the step a running task is in, so the board can show it live.

    worker | gate | check_before | check_after | verifier | approver | asking; None when the
    run ends. Display only: nothing reads it to decide anything, so a failed write is ignored.
    """
    try:
        tasks.annotate(bucket, task["id"], "stage", None if name is None else
                       {"name": name, "attempt": attempt, "since": time.time()})
    except Exception:
        pass


def _review_stage(bucket: str, task: dict, cfg: dict, stage: str, review: dict, hops: list,
                  attempt: int, wd: pathlib.Path, log: pathlib.Path, gate_exit, base_sha: str,
                  verifier: dict | None = None) -> dict:
    """One reviewer hop: build its prompt, run it, parse its verdict, log a `review` row."""
    argv = list(review[stage])
    hop = len(hops) + 1
    _, diff = _git(cfg["cwd"], "diff", "--stat", "--patch", base_sha, "HEAD")
    try:
        tail = log.read_bytes()[-2500:].decode("utf-8", "replace")
    except OSError:
        tail = ""
    if stage == "verifier":
        text = chain.VERIFY.format(prompt=task["prompt"], gate=" ".join(cfg["gate"]),
                                   gate_exit=gate_exit, gate_tail=tail, attempt=attempt,
                                   task_id=task["id"], base_sha=base_sha,
                                   diff=diff[:40000])
        allowed = {"pass", "fail", "question"}
    else:
        ev = "\n".join("- `%s` -> %s" % (e.get("cmd"), str(e.get("observed"))[:500])
                       for e in (verifier or {}).get("evidence") or [] if isinstance(e, dict))
        text = chain.APPROVE.format(prompt=task["prompt"], diff=diff[:40000],
                                    v_verdict=(verifier or {}).get("verdict"),
                                    v_judgement=(verifier or {}).get("judgement"),
                                    v_evidence=ev or "(none recorded)")
        allowed = {"approve", "reject", "question"}
    _stage(bucket, task, stage, attempt)
    pf = wd / ("%s-hop%d.md" % (stage, hop))
    pf.write_bytes(text.encode("utf-8"))
    card = cards.write(task["run_id"], "%s-hop%d" % (stage, hop), text)
    out = wd / ("%s-hop%d.log" % (stage, hop))
    t0 = time.time()
    rc = _run(argv, cfg["cwd"], out, stdin=pf)
    wall = int((time.time() - t0) * 1000)
    reply = out.read_bytes().decode("utf-8", "replace") if out.exists() else ""
    with open(log, "ab") as fh:
        fh.write(("\n===== %s hop %d (exit %s) =====\n" % (stage, hop, rc)).encode("utf-8"))
        fh.write(reply[-20000:].encode("utf-8"))
    v = chain.parse_verdict(reply, allowed)
    if rc != 0 and v["verdict"] in allowed and v["verdict"] in ("pass", "approve"):
        v = {**v, "verdict": "error", "judgement": "reviewer exited %s: %s" % (rc, v.get("judgement"))}
    discarded = _reset_tree(cfg["cwd"])
    h = {"stage": stage, "hop": hop, "attempt": attempt, "reviewer_model": chain.model_of(argv),
         "verdict": v["verdict"], "judgement": v.get("judgement"), "feedback": v.get("feedback"),
         "evidence": v.get("evidence") or [], "qa_cases": v.get("qa_cases") or []}
    hops.append(h)
    subject = "worker" if stage == "verifier" else "verifier"
    ledger.write("review", task["run_id"], task["id"], stage=stage,
                 reviewer_model=h["reviewer_model"], subject_node=subject,
                 subject_model=chain.model_of(cfg["runner"] if stage == "verifier"
                                              else review.get("verifier") or []),
                 hop=hop, attempt=attempt, verdict=h["verdict"], judgement=h["judgement"],
                 feedback=h["feedback"], evidence=h["evidence"], wall_ms=wall,
                 reviewer_exit=rc, brief_path=card["path"], brief_sha256=card["sha256"],
                 qa_cases=len(h["qa_cases"]), discarded_changes=discarded, bucket=bucket,
                 gate_exit=gate_exit)
    memory.record(cfg["cwd"], task, "review", run_id=task["run_id"], attempt=attempt, stage=stage,
                  model=h["reviewer_model"], verdict=h["verdict"], judgement=h["judgement"],
                  feedback=h["feedback"])
    return h


def pass_once(config: dict | None = None, wait: bool = True) -> dict:
    """Recover, expire, then lease what can start. Returns what it did.

    With `wait`, passes repeat until nothing more starts — so `--once` drains everything
    approved, one task per working tree at a time.
    """
    config = load_config() if config is None else config
    report = {"started": [], "skipped": {}, "unrunnable": {}}
    while True:
        n = len(report["started"])
        _one_pass(config, wait, report)
        if not wait or len(report["started"]) == n:
            break
    for tid in report["started"]:
        report["skipped"].pop(tid, None)
    return report


def _one_pass(config: dict, wait: bool, report: dict) -> None:
    threads = []
    for bucket, cfg in config.items():
        why = runnable(cfg)
        if why:
            report["unrunnable"][bucket] = why
            continue
        try:
            sweeper.tick(bucket, cfg)          # approved plans: start ready tasks, retry, review
        except Exception as e:                 # a plan going wrong never stops the daemon
            ledger.write("sweeper_error", "pool", None, bucket=bucket, error=str(e)[:400])
        pool.recover(bucket)
        pool.expire(bucket)
        for t in sorted(tasks.in_state(bucket, tasks.SELECTED), key=lambda x: x["created"]):
            run_id = "%s-%s" % (t.get("selection_id") or "S-none", t["id"])
            if not _claim_cwd(cfg["cwd"]):
                report["skipped"][t["id"]] = "cwd busy"
                continue
            try:
                leased = pool.start(bucket, t["id"], run_id)
            except pool.ApprovalRequired:
                _release_cwd(cfg["cwd"])
                report["skipped"][t["id"]] = "awaiting approval"
                continue
            except pool.DrainSkipped as e:
                _release_cwd(cfg["cwd"])
                report["skipped"][t["id"]] = type(e).__name__
                continue
            report["started"].append(t["id"])
            th = threading.Thread(target=_finish, args=(bucket, leased, cfg), daemon=False)
            th.start()
            threads.append(th)
    if wait:
        for th in threads:
            th.join()


def _finish(bucket: str, task: dict, cfg: dict) -> None:
    ok, gate_exit, evidence = False, None, {}
    plan_ctx = None
    try:
        # A plan task is cut from, and merged back into, its plan branch -- all inside this
        # claimed cwd -- and the repo always ends on the branch it was on (plan.base).
        plan_ctx = sweeper.enter(bucket, task, cfg)
        ok, gate_exit, evidence = execute(bucket, task, cfg)
        if plan_ctx:
            ok, evidence = sweeper.land(bucket, task, cfg, plan_ctx, ok, gate_exit, evidence)
    except Exception as e:                       # a runner blowing up fails the task,
        ok, gate_exit = False, None              # never the daemon
        evidence = {**evidence, "error": str(e)[:400]}
        ledger.write("task_runner_error", "pool", task["id"], bucket=bucket, error=str(e))
    finally:
        if plan_ctx:
            sweeper.leave(cfg, plan_ctx)
    try:
        pool.finish(bucket, task["id"], ok, gate_exit, **evidence)
        if evidence.get("question_open"):
            # Not a failure: it is waiting for an answer. Back on the list, question visible.
            tasks.transition(bucket, task["id"], tasks.HELD, selection_id=None)
    finally:
        _release_cwd(cfg["cwd"])       # after the verdict is recorded, not before


def _check_on_base(cwd: str, check: str, base_sha: str | None, log) -> tuple:
    """Run the task's check on the code as it was before the work (a temp worktree at the
    base commit). It must FAIL there, or it cannot tell whether the task was done."""
    import shutil
    import tempfile
    if not base_sha:
        return None, "not a git repo: the check could not be run on the code before the work"
    tmp = tempfile.mkdtemp(prefix="plexar-base-")
    wt = pathlib.Path(tmp) / "base"
    try:
        code, out = _git(cwd, "worktree", "add", "--detach", "-q", str(wt), base_sha)
        if code:
            return None, "could not check out the base commit: %s" % out[:300]
        mark = log.stat().st_size if log.exists() else 0
        with open(log, "ab") as fh:
            fh.write(b"\n===== the task's check, on the code BEFORE the work (it must fail) =====\n")
        rc = _run(_argv(check), str(wt), log)
        said = "\n".join(l for l in _tail_since(log, mark, 4000).strip().splitlines()
                         if not l.startswith("$ ") and not l.startswith("=====")).strip()[-1500:]
        return rc, said
    finally:
        _git(cwd, "worktree", "remove", "--force", str(wt))
        shutil.rmtree(tmp, ignore_errors=True)


def _ask_up(bucket, task, cfg, question, asker, run_id, log, env, evidence):
    """Climb with a question. Returns the text to hand the worker, or None if a person must
    answer (then evidence["question_open"] is set and the run stops)."""
    _stage(bucket, task, "asking")
    question = (question or "").strip() or "(the agent asked without saying what)"
    res = ask.climb(task, cfg, question, asker,
                    lambda argv, text: ask.run_text(argv, text, cfg["cwd"], log, env=env))
    ledger.write("question", run_id, task["id"], bucket=bucket, asker=asker, question=question,
                 answered=res["answered"], answered_by=res.get("by"), answerer_model=res.get("model"),
                 tried=res.get("tried"))
    memory.record(cfg["cwd"], task, "question", run_id=run_id, asker=asker, question=question,
                  answered_by=res.get("by") or ("a person" if not res["answered"] else None),
                  answer=res.get("answer"), tried=res.get("tried"))
    if res["answered"] and res.get("check"):
        why = _replace_check(bucket, task, cfg, res, run_id, log, evidence)
        if why:                                   # the fix did not prove itself: a person decides
            res = {"answered": False, "tried": list(res.get("tried") or []) + [
                {"tier": res["by"], "model": res["model"], "why": why}]}
    if res["answered"]:
        rec = {"question": question, "asked_by": asker, "answer": res["answer"],
               "by": "%s (%s)" % (res["by"], res["model"]), "at": ask.now()}
        task["answers"] = list(task.get("answers") or []) + [rec]
        tasks.annotate(bucket, task["id"], "answers", task["answers"])
        return "You (or a reviewer) asked: %s\nAnswer, from the %s: %s" % (question, res["by"], res["answer"])
    evidence["question_open"] = {"question": question, "asked_by": asker, "at": ask.now(),
                                 "tried": res.get("tried")}
    evidence["error"] = "Waiting for your answer: %s" % question
    return None


def _replace_check(bucket, task, cfg, res, run_id, log, evidence) -> str | None:
    """A reviewer settled a blocker by correcting the task's check. Accept it only if it proves
    something (fails on the code before the work); then the run carries on with it.
    Returns None when accepted, else why not (and a person is asked instead)."""
    new, old = res["check"], task.get("check")
    try:
        _argv(new)
    except ValueError as e:
        return "the corrected check could not be parsed: %s" % e
    before = _check_on_base(cfg["cwd"], new, evidence.get("base_sha"), log)
    if before[0] is None:
        return "its corrected check could not be run on the code before the work: %s" % before[1]
    if before[0] == 0:
        return ("its corrected check already passes on the code before the work, so it would "
                "prove nothing: %s" % new)
    task["check"] = new
    tasks.annotate(bucket, task["id"], "check", new)
    fix = {"old": old, "new": new, "by": "%s (%s)" % (res["by"], res["model"]), "at": ask.now(),
           "before_exit": before[0]}
    task["check_fixes"] = list(task.get("check_fixes") or []) + [fix]
    tasks.annotate(bucket, task["id"], "check_fixes", task["check_fixes"])
    evidence.update({"check": new, "check_before_exit": before[0], "check_before_output": before[1]})
    ledger.write("check_replaced", run_id, task["id"], bucket=bucket, **fix)
    memory.record(cfg["cwd"], task, "check_replaced", run_id=run_id, **fix)
    return None


def reask(bucket: str, tid: str) -> dict:
    """Send a task's open question back up the review tiers, for example after the framework
    was fixed or the tiers' rules changed. Answered: it is recorded (a corrected check is
    applied if it proves itself) and the question closes, so a plan picks the task up again.
    Not answered: it stays open for a person, with who tried and why. Returns the task."""
    task = tasks.get(bucket, tid)
    q = (task or {}).get("question_open")
    if not q:
        raise ValueError("task %s has no open question" % tid)
    cfg = load_config().get(bucket) or {}
    run_id = task.get("run_id") or "reask"
    wd = pathlib.Path(task.get("run_dir") or tempfile.mkdtemp(prefix="plexar-reask-"))
    wd.mkdir(parents=True, exist_ok=True)
    log = wd / "output.log"
    evidence = {"base_sha": task.get("base_sha")}
    got = _ask_up(bucket, task, cfg, q["question"], q.get("asked_by") or "worker", run_id, log,
                  None, evidence)
    if got:
        tasks.annotate(bucket, tid, "question_open", None)
    else:
        tasks.annotate(bucket, tid, "question_open", evidence["question_open"])
    _stage(bucket, task, None)
    return tasks.get(bucket, tid)


def prompt_with_feedback(task: dict) -> str:
    """The task's prompt, plus every note a person attached when sending it back.

    A task returned with "needs rework" or "rejected" carries the person's note; the next run
    must see it, or the loop repeats the same mistake. Oldest first, each numbered.
    """
    notes = task.get("feedback") or []
    answers = task.get("answers") or []
    head = task["prompt"]
    if answers:
        head = head.rstrip() + "\n\n---\nQuestions asked about this task, and their answers:\n" + "\n".join(
            "- Q (%s): %s\n  A (%s): %s" % (a.get("asked_by"), a.get("question"), a.get("by"), a.get("answer"))
            for a in answers) + "\n"
    if not notes:
        return head
    lines = [head.rstrip(), "", "---",
             "A person reviewed earlier attempts of this task and sent it back. Fix exactly this;",
             "your earlier work is already on this branch."]
    for f in notes:
        lines.append("- Round %s (%s, by %s): %s" % (f.get("round"), f.get("verdict"), f.get("by"), f.get("note")))
    return "\n".join(lines) + "\n"


def memory_block(cwd: str, task_id: str, mem_path) -> str:
    """The prompt lines that point the agent at its memory, with what earlier runs did."""
    past = memory.summary(cwd, task_id)
    lines = ["", "---", "Your memory for this task is the JSON file %s (also in env PLEXAR_TASK_MEMORY)." % mem_path,
             "Read it first. Add what you learn to its \"notes\" list as {\"text\": ...} entries;",
             "the next run of this task (after a crash, a retry or a review) will see them."]
    if past:
        lines += ["What happened on this task before (from that file):", past]
    return "\n".join(lines) + "\n"


RUNNER_ACCESS = ("the runner uses your own login or API key for that agent; the Framework "
                 "holds none")

# What agent CLIs print when they have no credentials. Conservative on purpose: only phrases
# that mean "not signed in / no key", so an ordinary failure is never mislabelled.
_NOT_SIGNED_IN = re.compile(
    r"not logged in|please run /login|run .{0,20}\blogin\b|login required|log ?in to continue|"
    r"invalid api key|no api key|api key (?:is )?(?:missing|not set|required)|missing api key|"
    r"authentication (?:failed|required|error)|unauthori[sz]ed|\b401\b|"
    r"not signed in|sign in (?:first|required|to continue)|no credentials|"
    # Plexar Harness -p (its auth.mjs / revocation.ts wording, confirmed by the harness team)
    r"please sign in again|access is paused|access has been turned off|"
    r"doesn't have plexar harness access|access was revoked",
    re.IGNORECASE)


def _tail_since(log: pathlib.Path, mark: int, limit: int = 20000) -> str:
    try:
        with open(log, "rb") as fh:
            fh.seek(mark)
            return fh.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


def not_signed_in(argv: list[str], runner_exit, output: str) -> str | None:
    """A plain message when a failed runner's own output says it has no login or key."""
    if runner_exit in (0, None) or not _NOT_SIGNED_IN.search(output or ""):
        return None
    prog = os.path.splitext(os.path.basename(argv[0]))[0] if argv else "the agent"
    return ("%s is not signed in: %s. Sign in to %s (or set its API key) on this machine, "
            "then requeue the task." % (prog, RUNNER_ACCESS, prog))


def _argv(cmd: str) -> list[str]:
    """A JSON list is taken as-is; anything else is split like the local shell would."""
    cmd = cmd.strip()
    if cmd.startswith("["):
        v = json.loads(cmd)
        if not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
            raise ValueError("a JSON command must be a list of strings")
        return v
    import shlex
    if os.name != "nt":
        return shlex.split(cmd)
    # Windows: split exactly as the program being started will split its own command line
    # (CommandLineToArgvW, the rule every C runtime follows): quotes group, `\"` is a literal
    # quote, backslashes elsewhere are path separators. Found 2026-09-26: shlex(posix=False)
    # kept `\"` as backslash+quote, so a planner's `node -e "...includes('id=\"x\"')"` check
    # died on a SyntaxError both before and after the work -- and "before: failed" looked like
    # the check proving something.
    import ctypes
    from ctypes import wintypes
    f = ctypes.windll.shell32.CommandLineToArgvW
    f.argtypes, f.restype = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)], ctypes.POINTER(wintypes.LPWSTR)
    n = ctypes.c_int(0)
    arr = f(cmd, ctypes.byref(n))
    if not arr:
        raise ValueError("could not split the command: %s" % cmd)
    try:
        return [arr[i] for i in range(n.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(arr)


def configure(bucket: str, cwd: str, runner: str, gate: str, verifier: str | None = None,
              approver: str | None = None, max_loops: int = 3) -> dict:
    """Write one bucket's entry. The ONLY writer of daemon.json — the API never is.

    `verifier` + `approver` turn on the review chain (chain.py). Both or neither: a verifier
    whose pass nobody above it checks is just a second opinion, not a chain.
    """
    entry = {"cwd": os.path.abspath(cwd), "runner": _argv(runner), "gate": _argv(gate)}
    if bool(verifier) != bool(approver):
        raise ValueError("%s: a review chain needs BOTH a verifier and an approver" % bucket)
    if verifier:
        if not 1 <= int(max_loops) <= 5:
            raise ValueError("%s: max_loops must be 1..5 (the ladder caps at 3)" % bucket)
        entry["review"] = {"verifier": _argv(verifier), "approver": _argv(approver),
                           "max_loops": int(max_loops)}
    why = runnable(entry)
    if why:
        raise ValueError("%s: %s" % (bucket, why))
    p = config_path()
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {}
    doc.setdefault("buckets", {})[bucket] = entry
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    ledger.write("daemon_configured", "pool", None, bucket=bucket, **entry)
    return entry


def main(argv: list[str] | None = None) -> int:
    import sys
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["configure"]:
        c = argparse.ArgumentParser(prog="plexar_agents.daemon configure")
        c.add_argument("bucket")
        c.add_argument("--cwd", required=True)
        c.add_argument("--runner", required=True, help='e.g. "claude -p --permission-mode acceptEdits" (prompt on stdin)')
        c.add_argument("--gate", required=True, help='e.g. "python -m pytest -q"')
        ca = c.parse_args(argv[1:])
        try:
            print(json.dumps(configure(ca.bucket, ca.cwd, ca.runner, ca.gate), indent=2))
        except ValueError as e:
            print("refused: %s" % e)
            return 2
        return 0
    if argv[:1] == ["show"]:
        cfg = load_config()
        print("config: %s" % config_path())
        for b, c in cfg.items():
            print("  %-20s %s" % (b, "runnable" if runnable(c) is None else runnable(c)))
        if not cfg:
            print("  (no buckets configured — nothing will run)")
        return 0
    ap = argparse.ArgumentParser(prog="plexar_agents.daemon")
    ap.add_argument("--once", action="store_true", help="one pass, wait for it, exit")
    ap.add_argument("--poll", type=float, default=POLL_S)
    a = ap.parse_args(argv)
    ledger.write("daemon_start", "pool", None, pid=os.getpid(), config=str(config_path()))
    if a.once:
        print(json.dumps(pass_once(), indent=2))
        return 0
    try:
        while True:
            # Non-blocking: a long task must not stop other buckets being leased. The
            # concurrency cap in pool.start is what bounds the fan-out, not this loop.
            pass_once(wait=False)
            time.sleep(a.poll)
    except KeyboardInterrupt:
        ledger.write("daemon_stop", "pool", None, pid=os.getpid(), reason="interrupt")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
