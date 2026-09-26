#!/usr/bin/env python
"""B7n — M2 end to end, from outside, over HTTP, with the daemon initially DOWN.

A phone-shaped client submits a brief to a real server process; an agent selects it with
a reason; a start attempted before approval is REFUSED (428); a human approves; only then
is a daemon started — a separate process — and it leases the work, runs the runner, runs
the gate, and the task lands DONE because the gate exited 0. A second task whose gate
fails lands FAILED, whatever the runner thought.

    python scripts/compose_m2e.py     ->  exit 0 or M2 is not done

Not proven here: slicing a brief into plan.json (B4n) — D29 moved decomposition inside
the run, so the runner (an /your-orchestrator invocation) does it, where the repo is fresh.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
PY = sys.executable
fails: list[str] = []


def ck(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def call(base, method, path, body=None):
    req = urllib.request.Request(base + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def main() -> int:
    work = pathlib.Path(tempfile.mkdtemp(prefix="plexar-m2e-"))
    env = dict(os.environ, PLEXAR_AGENTS_STATE=str(work / "state" / "runs"),
               PLEXAR_AGENTS_LOG_DIR=str(work / "logs"))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env["PLEXAR_FRAMEWORK_PORT"] = str(port)
    base = "http://127.0.0.1:%d" % port
    srv = subprocess.Popen([PY, str(ROOT / "docs" / "app.py")], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                if call(base, "GET", "/healthz")[0] == 200:
                    break
            except OSError:
                time.sleep(0.1)
        B = "phone"
        print("\n[1] submitted over HTTP while no daemon exists")
        st, a = call(base, "POST", "/api/buckets/%s/tasks" % B,
                     {"prompt": "Add a CHANGELOG line: shipped ✓", "source": "mobile"})
        ck(st == 202 and a["state"] == "held", "submit -> 202, held")
        st, b = call(base, "POST", "/api/buckets/%s/tasks" % B, {"prompt": "a task whose gate fails"})

        print("\n[2] selected with a reason; approval REQUIRED before anything starts")
        st, sel = call(base, "POST", "/api/buckets/%s/selections" % B,
                       {"task_ids": [a["id"], b["id"]], "agent": "agent",
                        "reason": "two small repo chores, one gate run covers both"})
        ck(st == 201 and sel["approval"] == "pending", "an ask bucket's selection is pending")
        st, _ = call(base, "POST", "/api/buckets/%s/tasks/%s/start" % (B, a["id"]), {"run_id": "x"})
        ck(st == 428, "a start before approval is refused with 428, got %s" % st)
        st, _ = call(base, "POST", "/api/buckets/%s/selections/%s/approve" % (B, sel["selection_id"]),
                     {"by": "len"})
        ck(st == 200, "a named human approves")

        print("\n[3] the daemon starts, leases, runs, and the GATE decides")
        repo = work / "repo"
        repo.mkdir()
        gate = [PY, "-c", "import pathlib,sys; t=pathlib.Path('got.md').read_text('utf-8');"
                          "sys.exit(0 if 'shipped' in t else 1)"]
        (work / "state" / "daemon.json").write_text(json.dumps({"buckets": {B: {
            "cwd": str(repo),
            "runner": [PY, "-c", "import shutil,sys; shutil.copy(sys.argv[1], 'got.md')",
                       "{prompt_file}"],
            "gate": gate}}}), encoding="utf-8")
        st, d = call(base, "GET", "/api/daemon")
        ck(d["buckets"][B]["runnable"] is True, "the API reports the bucket runnable (read-only)")
        r = subprocess.run([PY, "-m", "plexar_agents.daemon", "--once"], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=120)
        ck(r.returncode == 0, "daemon --once exits 0")
        _, ta = call(base, "GET", "/api/buckets/%s/tasks/%s" % (B, a["id"]))
        _, tb = call(base, "GET", "/api/buckets/%s/tasks/%s" % (B, b["id"]))
        ck(ta["state"] == "done" and ta["gate_exit"] == 0, "task A landed DONE with gate_exit 0")
        ck(tb["state"] == "failed" and tb["gate_exit"] == 1,
           "task B FAILED on its gate, though its runner exited 0")
        # both share one cwd here, so B's copy overwrote A's; the gate read what was there.
        # That is the sharing a real bucket has too, and why the gate runs per task.
    finally:
        srv.terminate()
        srv.wait(timeout=10)
        shutil.rmtree(work, ignore_errors=True)
    print("\n%s — %d failure(s)" % ("M2 END-TO-END: PASS" if not fails else "FAIL", len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
