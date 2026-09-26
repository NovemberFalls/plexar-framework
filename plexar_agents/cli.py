"""`plexar` — one command to run the framework on this machine.

    plexar init  [--bucket NAME] [--gate CMD] [--runner CMD]   # in a repo: make it a bucket
    plexar up    [--no-open]                                   # server + daemon, detached
    plexar autostart [on|off|status]         # start at login; ON by default after the first `up`
    plexar status
    plexar down
    plexar queue "statement"                 # add one task to this repo's bucket (held)
    plexar slice [FILE | --session latest]   # a long conversation -> proposed, grouped tasks
    plexar slice --commit SLICE_ID --by NAME # after reading it: create the held tasks
    plexar model [--url U --model M --key-env VAR --extra JSON]   # which model slices; yours
    plexar spawn "prompt" --reason R --approved-by NAME   # queue + select + approve in one go
    plexar plan "<goal>" [--from FILE] [--bucket B] [--by NAME]   # a frontier plan, synchronously
    plexar plan approve PID --by NAME        # the one approval; the sweeper runs it from here
    plexar plan reject PID --by NAME --note TEXT
    plexar plans [PID]                       # every plan, or one plan's tasks
    plexar memory [TASK] [--clear-on-accept on|off]  # a task's memory (.plexar/tasks/<id>.json)
    plexar log '{"event": "run_start", "run_id": ...}'   # append one row, checked (or: ... | plexar log -)
    plexar logs [tail] [--event E --run R --task T --from D --to D --follow]   # NDJSON to stdout
    plexar logs export --out FILE [filters]  # dump them
    plexar logs where | dir [--set PATH | --reset]   # where they live; move where new rows go
    plexar logs ingest FILE --source TOOL    # bring your own NDJSON rows in (validated)

The queue is ADD-ONLY from a session. Nothing is ever pushed from the queue into a working
session; the daemon runs approved work, and the framework's views show what happened.

`up` starts two processes that outlive the shell that started them — the server
(docs/app.py: /app, /api, /api-docs) and the daemon — and records their pids next to the task
store. Logs go to the same place. Nothing here changes what either process does; it only
saves typing three commands and remembering to keep two windows open.

Local-only (D25): the server binds 127.0.0.1. `up` never binds wider.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.request
import webbrowser

from . import daemon, ledger, model, plans, pool, slicer, tasks

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT = int(os.environ.get("PLEXAR_FRAMEWORK_PORT", "8430"))
URL = "http://127.0.0.1:%d" % PORT


def _state() -> pathlib.Path:
    d = tasks.dir_().parent
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pids_file() -> pathlib.Path:
    return _state() / "plexar-up.json"


def _healthy(timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(URL + "/healthz", timeout=timeout) as r:
            return r.status == 200
    except OSError:
        return False


def _alive(pid: int) -> bool:
    # NOT os.kill(pid, 0): on Windows that call terminates the process.
    if os.name == "nt":
        r = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
                           capture_output=True, text=True)
        return str(pid) in r.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _spawn(argv: list[str], log: pathlib.Path) -> int:
    # Through a middleman, so the process is nobody's child: closing the terminal, pane or
    # app that ran `plexar up` (Studio restarting its panes) must not take it down.
    from . import detach
    return detach.spawn(argv, str(log), str(ROOT))


def _read_pids() -> dict:
    try:
        return json.loads(_pids_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def up(open_browser: bool = True, autostart_default: bool = True) -> int:
    pids = {k: v for k, v in _read_pids().items() if _alive(v)}
    if "server" not in pids:
        if _healthy():
            print("something already answers on %s — not starting a second server" % URL)
        else:
            pids["server"] = _spawn([sys.executable, str(ROOT / "docs" / "app.py")],
                                    _state() / "server.log")
    if "daemon" not in pids:
        pids["daemon"] = _spawn([sys.executable, "-m", "plexar_agents.daemon"],
                                _state() / "daemon.log")
    _pids_file().write_text(json.dumps(pids), encoding="utf-8")
    for _ in range(50):
        if _healthy():
            break
        time.sleep(0.2)
    else:
        print("server did not answer on %s — see %s" % (URL, _state() / "server.log"))
        return 1
    print("up: %s/app   (server pid %s, daemon pid %s, logs in %s)"
          % (URL, pids.get("server", "external"), pids["daemon"], _state()))
    if autostart_default:
        from . import autostart
        print("start at login: %s" % autostart.ensure_default())
    if open_browser:
        webbrowser.open(URL + "/app")
    return 0


def autostart_cmd(state: str) -> int:
    from . import autostart
    if state == "on":
        print("on: %s" % autostart.enable())
    elif state == "off":
        print("off: removed %s (remembered; `plexar up` will not re-add it)" % autostart.disable())
    else:
        print("%s: %s" % ("on" if autostart.is_on() else "off", autostart.entry_path()))
    return 0


def down() -> int:
    pids = _read_pids()
    for name, pid in pids.items():
        if not _alive(pid):
            continue
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        else:
            os.kill(pid, 15)
        print("stopped %s (pid %d)" % (name, pid))
    _pids_file().unlink(missing_ok=True)
    return 0


def status() -> int:
    pids = _read_pids()
    print("server: %s  %s" % ("up" if _healthy() else "DOWN", URL))
    for name in ("server", "daemon"):
        pid = pids.get(name)
        print("%s pid: %s" % (name, "%d (%s)" % (pid, "alive" if _alive(pid) else "dead")
                               if pid else "not started by plexar up"))
    return daemon.main(["show"])


def _default_gate(repo: pathlib.Path) -> str | None:
    if (repo / "package.json").exists():
        return "npm test"
    if any(repo.glob("test*")) or (repo / "tests").is_dir() or (repo / "pyproject.toml").exists():
        return "python -m pytest -q"
    return None


def init(bucket: str | None, gate: str | None, runner: str | None,
         verifier: str | None = None, approver: str | None = None, max_loops: int = 3) -> int:
    repo = pathlib.Path.cwd()
    bucket = bucket or "".join(c if c.isalnum() or c in "-_." else "-" for c in repo.name)
    gate = gate or _default_gate(repo)
    if not gate:
        print("refused: no gate found for %s. A task that cannot be checked cannot land — "
              "pass --gate \"<command that exits 0 when the repo is right>\"" % repo)
        return 2
    runner = runner or "claude -p --permission-mode acceptEdits"
    try:
        daemon.configure(bucket, str(repo), runner, gate, verifier, approver, max_loops)
    except ValueError as e:
        print("refused: %s" % e)
        return 2
    print("bucket %r -> %s\n  runner: %s\n  gate:   %s\nopen %s/app?bucket=%s"
          % (bucket, repo, runner, gate, URL, bucket))
    print("note:   %s. Sign in to that agent (or set its API key) before approving work."
          % daemon.RUNNER_ACCESS)
    return 0


def _bucket_for_cwd(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    here = os.path.normcase(os.path.abspath(os.getcwd()))
    for b, c in daemon.load_config().items():
        root = os.path.normcase(os.path.abspath(c.get("cwd", "")))
        if here == root or here.startswith(root + os.sep):
            return b
    return None


def _session() -> str | None:
    # PLEXAR_SESSION_ID (Studio), DSH_SESSION_ID (the harness sets it on every shell call).
    return (os.environ.get("PLEXAR_SESSION_ID") or os.environ.get("DSH_SESSION_ID")
            or os.environ.get("CLAUDE_SESSION_ID"))


def queue(statement: str, bucket: str | None, check: str | None = None) -> int:
    b = _bucket_for_cwd(bucket)
    if not b:
        print("refused: this directory is not a bucket — run `plexar init` here, or pass --bucket")
        return 2
    if len(statement.strip()) < 20:
        print("refused: a task is a prompt a stranger could act on; say more")
        return 2
    t = tasks.hold(b, tasks.create(b, statement.strip(), source="queue", session_id=_session(),
                                   meta={"check": check.strip()} if (check or "").strip() else None)["id"])
    ledger.write("queue_add", "pool", t["id"], bucket=b, session_id=_session(),
                 prompt_sha256=t["prompt_sha256"], prompt_chars=len(t["prompt"]))
    print("held %s in %s — approve it in %s/app?bucket=%s" % (t["id"], b, URL, b))
    return 0


def slice_cmd(a) -> int:
    if a.commit:
        if not a.by:
            print("refused: --by NAME — a slice is committed by the person who read it")
            return 2
        try:
            made = slicer.commit(a.commit, a.by, session_id=_session())
        except Exception as e:                       # SweepRefused or missing slice
            print("refused: %s" % e)
            return 2
        for t in made:
            print("held %s  [%s]  %s" % (t["id"], t.get("group") or "-", t["prompt"][:90]))
        print("%d task(s) held — select and approve in %s/app?bucket=%s"
              % (len(made), URL, made[0]["bucket"] if made else ""))
        return 0
    b = _bucket_for_cwd(a.bucket)
    if not b:
        print("refused: this directory is not a bucket — run `plexar init` here, or pass --bucket")
        return 2
    path = str(slicer.latest_session()) if a.session == "latest" else a.file
    if not path:
        print("refused: give a transcript FILE or --session latest")
        return 2
    text = slicer.read_transcript(path)
    s = slicer.slice_text(text, b, source="transcript", source_path=path)
    r = s["report"]
    print("slice %s  (%d chunks, coverage %.0f%%, %s)" % (
        s["slice_id"], len({c["chunk"] for c in s["candidates"]}) or 0, 100 * r["coverage"],
        "READY" if r["ok"] else "NOT COMMITTABLE"))
    groups: dict = {}
    for t in s["tasks"]:
        groups.setdefault(t["group"] or "-", []).append(t)
    for g, ts in sorted(groups.items()):
        print("\n  [%s]" % g)
        for t in ts:
            print("    %s  %s" % (t["id"], t["prompt"][:110]))
    for u in r["uncovered"][:10]:
        print("\n  UNCOVERED: %s" % u["text"][:160])
    for p in r["problems"][:10]:
        print("  PROBLEM: %s" % p)
    print("\nfull slice: %s" % (slicer.slices_dir() / (s["slice_id"] + ".json")))
    if r["ok"]:
        print("commit: plexar slice --commit %s --by <your name>" % s["slice_id"])
    return 0 if r["ok"] else 1


_MODEL_HELP = {
    "unauthorized": "the endpoint does not know this key (unset, wrong, revoked or paused)",
    "not_granted": "the key is valid but not granted for this product",
    "refused": "the endpoint refused the request",
    "unreachable": "nothing answered at that URL",
}


def model_cmd(a) -> int:
    if a.url and not a.model:
        # No model named: ask the endpoint. One chat model -> use it; several -> the user
        # chooses. The library never picks from a hard-coded list (model ids get renamed).
        key = os.environ.get(a.key_env) if a.key_env else None
        r = model.list_models(a.url, key)
        if r["status"] != "ok":
            print("refused: %s answered %s: %s (%s)" % (a.url, r["code"] or "nothing",
                  _MODEL_HELP[r["status"]], r["detail"]))
            return 2
        if len(r["models"]) != 1:
            print("choose one with --model:" if r["models"] else "the endpoint lists no chat models for this key")
            for m in r["models"]:
                print("  %s" % m)
            return 2
        a.model = r["models"][0]
        print("using the only chat model the endpoint offers: %s" % a.model)
    if a.url or a.model:
        try:
            extra = json.loads(a.extra) if a.extra else None
            model.configure(a.url, a.model, a.key_env, extra)
        except (ValueError, json.JSONDecodeError) as e:
            print("refused: %s" % e)
            return 2
    try:
        d = model.describe()
    except model.NoModel as e:
        print(str(e))
        return 1
    print("endpoint: %s\nmodel:    %s\nkey:      %s\nextra:    %s\nconfig:   %s" % (
        d["url"], d["model"], d["key_source"], json.dumps(d["extra"]), model.config_path()))
    if getattr(a, "check", False):
        r = model.list_models(d["url"], model.resolve()["key"])
        if r["status"] != "ok":
            print("check:    FAIL %s: %s (%s)" % (r["code"] or "-", _MODEL_HELP[r["status"]], r["detail"]))
            return 1
        ok = d["model"] in r["models"]
        print("check:    %s (%d chat model(s) available%s)" % (
            "ok" if ok else "FAIL: %s is not offered to this key" % d["model"], len(r["models"]),
            "" if ok else ": " + ", ".join(r["models"][:8])))
        return 0 if ok else 1
    return 0


def logs_cmd(a) -> int:
    """The logs are plain files; this reads them directly, so it works with the server down."""
    from . import logs, paths
    if a.action == "where":
        w = logs.where()
        print("logs:      %s" % w["log_dir"])
        print("artifacts: %s" % w["artifacts_dir"])
        print("settings:  %s" % w["settings"])
        for f in w["files"]:
            print("  %-34s %10d bytes" % (f["name"], f["bytes"]))
        return 0
    if a.action == "dir":
        if a.set or a.reset:
            paths.set_setting("log_dir", None if a.reset else a.set)
            print("log_dir now: %s" % paths.log_dir())
            if a.set:
                print("(existing rows stay where they were; move or copy them yourself)")
            return 0
        print(paths.log_dir())
        return 0
    if a.action == "ingest":
        rows, bad = [], 0
        with open(a.file, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        bad += 1
        r = logs.ingest(rows, a.source)
        print("accepted %d, rejected %d, unparseable lines %d" % (r["accepted"], len(r["rejected"]), bad))
        for x in r["rejected"][:20]:
            print("  row %d: %s" % (x["index"], "; ".join(x["problems"])))
        return 0 if not r["rejected"] and not bad else 1
    kw = dict(event=a.event, run_id=a.run, task=a.task, date_from=a.date_from, date_to=a.date_to)
    out = open(a.out, "w", encoding="utf-8", newline="\n") if a.action == "export" and a.out else sys.stdout
    n, cursor = 0, None
    try:
        while True:
            for cursor, row in logs.iter_rows(cursor, **kw):
                if row is None:
                    continue
                out.write(json.dumps(row, default=str) + "\n")
                n += 1
                if a.action == "tail" and a.limit and n >= a.limit:
                    return 0
            if not getattr(a, "follow", False):
                break
            out.flush()
            time.sleep(2)
    except KeyboardInterrupt:
        pass
    finally:
        if out is not sys.stdout:
            out.close()
            print("wrote %d rows to %s" % (n, a.out), file=sys.stderr)
    return 0


def log_cmd(a) -> int:
    """Append ONE row to the log, properly: `plexar log '{"event": ..., "run_id": ...}'`.

    For skills and sessions that write rows by hand. Echoing JSON into the file directly is
    how unescaped Windows paths and "type" instead of "event" ended up corrupting rows; this
    parses, checks against the pinned schema, and appends through the one writer.
    The row can also come on stdin (`... | plexar log -`).
    """
    raw = sys.stdin.read() if a.row in (None, "-") else a.row
    try:
        row = json.loads(raw)
    except ValueError as e:
        print("refused: not valid JSON (%s). Nothing was written." % e, file=sys.stderr)
        return 2
    if not isinstance(row, dict) or not row.get("event") or not row.get("run_id"):
        print("refused: a row needs at least \"event\" and \"run_id\". Nothing was written.", file=sys.stderr)
        return 2
    event, run_id, node = row.pop("event"), row.pop("run_id"), row.pop("node_id", None)
    row.pop("ts", None)                       # the writer stamps it, with a UTC offset
    problems = ledger.validate({"ts": "x", "event": event, "run_id": run_id, "node_id": node, **row})
    ledger.write(event, run_id, node, **row)
    if problems:
        print("written, with schema warnings: %s" % "; ".join(problems), file=sys.stderr)
        return 1
    print("written: %s %s" % (event, run_id))
    return 0


def memory_cmd(a) -> int:
    """A task's memory (<repo>/.plexar/tasks/<id>.json), and the clear-on-accept setting."""
    from . import memory
    if a.clear_on_accept:
        memory.set_clear_on_accept(a.clear_on_accept == "on")
    print("clear task memory when a person accepts the work: %s%s" % (
        "ON" if memory.clear_on_accept() else "off",
        "" if memory.clear_on_accept() else " (memory is kept; `plexar memory --clear-on-accept on` to change)"))
    if a.task:
        b = _bucket_for_cwd(a.bucket)
        cwd = (daemon.load_config().get(b) or {}).get("cwd") if b else os.getcwd()
        p = memory.path(cwd, a.task)
        print("%s:" % p)
        print(json.dumps(memory.load(cwd, a.task), indent=2, ensure_ascii=False) if p.exists() else "  (no memory yet)")
    return 0


def spawn_cmd(a) -> int:
    """Queue, select and approve one task so the daemon runs it. The approver is a person."""
    b = _bucket_for_cwd(a.bucket)
    if not b:
        print("no bucket for this directory; pass --bucket or run `plexar init` here", file=sys.stderr)
        return 2
    t = tasks.create(b, a.prompt, source="cli-spawn", session_id=_session())
    tasks.hold(b, t["id"])
    try:
        s = pool.select(b, [t["id"]], a.reason, a.agent)
        if s.get("approval") != "auto":
            pool.approve(b, s["selection_id"], a.approved_by)
    except Exception as e:           # the pool's refusal, verbatim; the task stays held
        print("refused: %s (%s is held in %s)" % (e, t["id"], b), file=sys.stderr)
        return 2
    ledger.write("spawn", "pool", t["id"], bucket=b, selection_id=s["selection_id"],
                 approved_by=a.approved_by, session_id=_session())
    print("%s spawned in %s (selection %s, approved by %s)" % (t["id"], b, s["selection_id"], a.approved_by))
    return 0


def _plan_table(p: dict) -> None:
    print("plan %s  [%s]  %s" % (p["id"], p["status"], p["goal"][:100]))
    for q in p.get("questions") or []:
        print("  QUESTION: %s%s" % (q["question"], "" if q.get("answer") is None
              else "  -> answered: %s" % q["answer"]))
    if p.get("error"):
        print("  error: %s" % p["error"])
    for t in p.get("tasks") or []:
        print("  %-6s %-10s deps=%-14s check=%-30s %s" % (
            t["key"], t["lane"], ",".join(t["deps"]) or "-",
            (t.get("check") or "-")[:30], t["prompt"][:70]))
    if p.get("final"):
        print("  final: %s  %s" % (p["final"]["verdict"], p["final"]["judgement"]))
    if p.get("status") in ("approved", "running", "done"):
        print("  branch: %s (merge it into main yourself)" % p["branch"])


def plan_cmd(a) -> int:
    """`plexar plan "<goal>"` runs the planner synchronously and prints the plan.

    `plexar plan approve|reject PID ...` are the person's decision, made once.
    """
    b = _bucket_for_cwd(a.bucket)
    if not b:
        print("refused: this directory is not a bucket — run `plexar init` here, or pass --bucket")
        return 2
    if a.goal_or_action in ("approve", "reject"):
        if not a.pid:
            print("refused: `plexar plan %s PID --by NAME`" % a.goal_or_action)
            return 2
        if not a.by:
            print("refused: --by NAME — this decision is made once, by a named person")
            return 2
        try:
            if a.goal_or_action == "approve":
                p = plans.approve(b, a.pid, a.by)
            else:
                if not a.note:
                    print("refused: --note TEXT — a rejection needs a note saying why")
                    return 2
                p = plans.reject(b, a.pid, a.by, a.note)
        except plans.PlanError as e:
            print("refused: %s" % e)
            return 2
        _plan_table(p)
        return 0
    goal = a.goal_or_action
    if a.from_file:
        goal = pathlib.Path(a.from_file).read_text(encoding="utf-8")
    if not (goal or "").strip():
        print("refused: give a goal, or `plexar plan --from FILE`")
        return 2
    by = a.by or _session() or "cli"
    p = plans.create(b, goal, by)
    _plan_table(p)
    if p["status"] == "questions":
        print("\nanswer with: plexar plans %s   (then a future `plexar plan answer` round-trips it)" % p["id"])
        return 1
    if p["status"] == "failed":
        return 1
    print("\napprove with: plexar plan approve %s --by NAME" % p["id"])
    return 0


def plans_cmd(a) -> int:
    b = _bucket_for_cwd(a.bucket)
    if not b:
        print("refused: this directory is not a bucket — run `plexar init` here, or pass --bucket")
        return 2
    if a.pid:
        p = plans.get(b, a.pid)
        if p is None:
            print("refused: no plan %s in %s" % (a.pid, b))
            return 2
        _plan_table(p)
        return 0
    for p in plans.all_(b):
        print("%s  [%-9s]  %d task(s)  %s" % (p["id"], p["status"], len(p["tasks"]), p["goal"][:80]))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="plexar")
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("up")
    u.add_argument("--no-open", action="store_true")
    u.add_argument("--no-autostart", action="store_true", help="don't install the login item this time")
    au = sub.add_parser("autostart", help="start the framework at login (on by default)")
    au.add_argument("state", nargs="?", default="status", choices=["on", "off", "status"])
    sub.add_parser("down")
    sub.add_parser("status")
    i = sub.add_parser("init")
    i.add_argument("--bucket")
    i.add_argument("--gate")
    i.add_argument("--runner")
    i.add_argument("--verifier", help="the tier above the worker; it may execute")
    i.add_argument("--approver", help="the orchestrator tier; approves or rejects with a reason")
    i.add_argument("--max-loops", dest="max_loops", type=int, default=3)
    q = sub.add_parser("queue")
    q.add_argument("statement")
    q.add_argument("--bucket")
    q.add_argument("--check", help="a command that must fail now and pass once the task is done")
    sl = sub.add_parser("slice")
    sl.add_argument("file", nargs="?")
    sl.add_argument("--session", choices=["latest"])
    sl.add_argument("--bucket")
    sl.add_argument("--commit", metavar="SLICE_ID")
    sl.add_argument("--by")
    m = sub.add_parser("model")
    m.add_argument("--url")
    m.add_argument("--model")
    m.add_argument("--key-env", dest="key_env")
    m.add_argument("--extra")
    m.add_argument("--check", action="store_true", help="ask the endpoint whether this key can use the model")
    lg = sub.add_parser("logs", help="read, export, locate or ingest the logs")
    lg.add_argument("action", nargs="?", default="tail", choices=["tail", "export", "where", "dir", "ingest"])
    lg.add_argument("file", nargs="?", help="ingest: an NDJSON file of rows")
    lg.add_argument("--event", help="comma list, e.g. review,qa_result")
    lg.add_argument("--run")
    lg.add_argument("--task")
    lg.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD")
    lg.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD")
    lg.add_argument("--limit", type=int, default=0)
    lg.add_argument("--follow", "-f", action="store_true")
    lg.add_argument("--out", help="export: write here instead of stdout")
    lg.add_argument("--source", help="ingest: the tool the rows came from (required)")
    lg.add_argument("--set", help="dir: keep logs in this folder from now on")
    lg.add_argument("--reset", action="store_true", help="dir: back to the default folder")
    lo = sub.add_parser("log", help="append one row to the log, checked (JSON arg or stdin)")
    lo.add_argument("row", nargs="?", help="the row as JSON, or - for stdin")
    me = sub.add_parser("memory", help="show a task's memory; set clear-on-accept")
    me.add_argument("task", nargs="?")
    me.add_argument("--bucket")
    me.add_argument("--clear-on-accept", dest="clear_on_accept", choices=["on", "off"])
    sp = sub.add_parser("spawn", help="queue + select + approve one task in one step")
    sp.add_argument("prompt")
    sp.add_argument("--reason", required=True)
    sp.add_argument("--approved-by", dest="approved_by", required=True, help="the person saying yes")
    sp.add_argument("--bucket")
    sp.add_argument("--agent", default="cli")
    pl = sub.add_parser("plan", help='a frontier plan: "<goal>", or approve/reject PID')
    pl.add_argument("goal_or_action", help='the goal, or "approve"/"reject"')
    pl.add_argument("pid", nargs="?", help="with approve/reject: the plan id")
    pl.add_argument("--from", dest="from_file", metavar="FILE")
    pl.add_argument("--bucket")
    pl.add_argument("--by")
    pl.add_argument("--note")
    pls = sub.add_parser("plans", help="every plan, or one plan's tasks")
    pls.add_argument("pid", nargs="?")
    pls.add_argument("--bucket")
    a = ap.parse_args(argv)
    if a.cmd == "logs":
        if a.action == "ingest" and not (a.file and a.source):
            ap.error("logs ingest needs FILE and --source")
        return logs_cmd(a)
    if a.cmd == "spawn":
        return spawn_cmd(a)
    if a.cmd == "plan":
        return plan_cmd(a)
    if a.cmd == "plans":
        return plans_cmd(a)
    if a.cmd == "memory":
        return memory_cmd(a)
    if a.cmd == "log":
        return log_cmd(a)
    if a.cmd == "model":
        return model_cmd(a)
    if a.cmd == "queue":
        return queue(a.statement, a.bucket, a.check)
    if a.cmd == "slice":
        return slice_cmd(a)
    if a.cmd == "up":
        return up(not a.no_open, not a.no_autostart)
    if a.cmd == "autostart":
        return autostart_cmd(a.state)
    if a.cmd == "down":
        return down()
    if a.cmd == "status":
        return status()
    return init(a.bucket, a.gate, a.runner, a.verifier, a.approver, a.max_loops)


if __name__ == "__main__":
    raise SystemExit(main())
