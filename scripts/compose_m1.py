#!/usr/bin/env python
"""A7n — drive the runtime from outside and assert what it actually did.

Not the unit suite again. This builds a throwaway git repo, arms a governed run against
it, drives the **real hook binary as a subprocess exactly as the harness does**, performs
a **real Bash write**, and then asks the filesystem and the ledger what happened.

Why this node exists, in one measured sentence: a product once shipped with 16 green node
accepts and 74 passing tests on top of a WebSocket endpoint that was four lines of no-op,
because every module passed against a stub and nothing asserted that the pieces met.

What it proves (M1's claim):
  E1  a write outside files_owned through a WRITE TOOL is denied
  E1' a write outside files_owned through BASH is detected — the D32 hole, closed by A9n
  E6  the ledger rows were written by the RUNTIME; the agent was never asked

What it deliberately does NOT prove: E3 (budget) and E4 (ladder). Those count across
spawns and need the daemon (A4n). Claiming them here would be the same over-read that
put E1 in the plan as holding when it did not.

    python scripts/compose_m1.py     ->  exit 0 or M1 is not done
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

HOOK = ROOT / "adapters" / "claude_code" / "pre_tool_use.py"

fails: list[str] = []


def ck(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)


def drive_hook(payload: dict, env: dict) -> dict:
    """Exactly what the harness does: a process, JSON on stdin, JSON on stdout."""
    p = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                       capture_output=True, text=True, env=env, timeout=30)
    if p.returncode != 0:
        fails.append("the hook crashed, which would block every tool call: %s" % p.stderr)
        return {}
    return json.loads(p.stdout or "{}")


def decision(out: dict):
    return (out.get("hookSpecificOutput") or {}).get("permissionDecision")


def main() -> int:
    work = pathlib.Path(tempfile.mkdtemp(prefix="plexar-m1-"))
    try:
        repo = work / "fixture"
        (repo / "lib").mkdir(parents=True)
        (repo / "lib" / "session.py").write_text("x = 1\n", encoding="utf-8")
        (repo / "lib" / "store.py").write_text("y = 1\n", encoding="utf-8")
        git(repo, "init", "-q")
        git(repo, "config", "user.email", "t@t")
        git(repo, "config", "user.name", "t")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "base")

        state, logs = work / "state" / "runs", work / "logs"
        state.mkdir(parents=True)
        logs.mkdir()
        env = dict(os.environ)
        env["PLEXAR_AGENTS_STATE"] = str(state)
        env["PLEXAR_AGENTS_LOG_DIR"] = str(logs)
        os.environ["PLEXAR_AGENTS_STATE"] = str(state)
        os.environ["PLEXAR_AGENTS_LOG_DIR"] = str(logs)

        owned = [str(repo / "lib")]
        (state / "M1.json").write_text(json.dumps(
            {"run_id": "M1", "enforce": True, "nodes": {"N01": {"files_owned": owned}}}),
            encoding="utf-8")
        (state / "ACTIVE").write_text("M1", encoding="utf-8")

        from plexar_agents import ledger, scope  # imported AFTER the env is set

        print("\n[1] the runtime writes the log — the skill is never asked to")
        ledger.write("run_start", "M1", None, skill="/your-orchestrator",
                     cwd=str(repo), task_summary="composed M1 fixture")
        before = scope.snapshot(str(repo))
        ck(before is not None, "a baseline snapshot was taken before any work")

        print("\n[2] E1 through a write tool")
        allowed = drive_hook({"tool_name": "Write", "session_id": "SID-M1", "cwd": str(repo),
                              "tool_input": {"file_path": str(repo / "lib" / "session.py")}}, env)
        ck(decision(allowed) is None, "an OWNED write is allowed")

        denied = drive_hook({"tool_name": "Write", "session_id": "SID-M1", "cwd": str(repo),
                             "tool_input": {"file_path": str(repo / "secrets.txt")}}, env)
        ck(decision(denied) == "deny", "an UNOWNED write is DENIED by the hook")
        reason = (denied.get("hookSpecificOutput") or {}).get("permissionDecisionReason", "")
        ck("secrets.txt" in reason, "the refusal names the file it refused")
        ck("planning defect" in reason, "the agent is told not to work around it")
        ck(not (repo / "secrets.txt").exists(), "and the file really is absent")

        print("\n[3] E1' through Bash — the D32 hole")
        subprocess.run("echo bypass > stray.txt", shell=True, cwd=str(repo))
        subprocess.run([sys.executable, "-c",
                        "open('py-stray.txt','w').write('bypass')"], cwd=str(repo))
        ck((repo / "stray.txt").exists(), "the Bash write DID land — the hook cannot stop it")
        swept = scope.sweep(str(repo), owned, before)
        ck(swept["observable"] is True, "the tree was observable")
        ck("stray.txt" in swept["violations"], "the Bash write is DETECTED as a violation")
        ck("py-stray.txt" in swept["violations"], "the python write is DETECTED too")
        ck("lib/session.py" not in swept["violations"], "the owned edit is not a violation")
        scope.record("M1", "N01", str(repo), swept)

        print("\n[4] the revert is a PLAN, never an execution (§6)")
        plan = scope.propose_revert(str(repo), swept["violations"])
        ck((repo / "stray.txt").exists(), "proposing a revert deleted nothing")
        ck(any("unrecoverable" in e["rollback"] for e in plan["plan"]),
           "an untracked file's plan says its rollback is unrecoverable")

        print("\n[5] E6 — what the RUNTIME recorded")
        rows = []
        for f in logs.glob("*.jsonl"):
            rows += [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
        ev = [r["event"] for r in rows]
        ck("run_start" in ev, "a run_start row exists")
        ck(ev.index("run_start") == 0, "run_start is FIRST — an empty log is unambiguous")
        ck(any(r["event"] == "enforced_denial" and r.get("invariant") == "E1" for r in rows),
           "the denial was recorded, unprompted")
        ck(any(r["event"] == "scope_sweep" and r.get("detector") == "git" for r in rows),
           "the sweep names which surface observed it")
        sweeps = [r for r in rows if r["event"] == "scope_sweep"]
        ck(sweeps and sweeps[0]["violation_count"] == 2, "the sweep row counts both violations")
        ck(all(r.get("ts", "")[-6] in "+-" for r in rows),
           "every ts carries a UTC offset — a naive stamp is not a timestamp")
        ck(all(json.dumps(r) and True for r in rows), "every row is valid JSON")
        ck(any(r.get("session_id") == "SID-M1" for r in rows),
           "rows carry the pane, supplied by the runtime")

        print("\n[6] what this does NOT prove")
        print("  E3 budget and E4 ladder need the daemon (A4n). Not asserted here.")
        print("  Prevention on the Bash surface needs a sandbox (D33). Detection only.")

    finally:
        # Never leave the machine enforced, even on a crash.
        for p in (work / "state" / "runs" / "ACTIVE",):
            try:
                p.unlink()
            except OSError:
                pass
        shutil.rmtree(work, ignore_errors=True)

    print()
    if fails:
        print("COMPOSED M1: FAIL (%d)" % len(fails))
        for f in fails:
            print("  -", f)
        return 1
    print("COMPOSED M1: PASS — the runtime wrote the log, denied a tool write,")
    print("                   and detected a write no hook could have stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
