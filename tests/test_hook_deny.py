"""A1n — the hook denies a write outside files_owned, and nothing else.

Every test drives the adapter as the harness does: a real subprocess, JSON on stdin,
JSON on stdout. Importing the module and calling main() would test a function; the
harness runs a process, and the process is what has to be right.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HOOK = ROOT / "adapters" / "claude_code" / "pre_tool_use.py"


def run_hook(payload: dict, state: pathlib.Path, logs: pathlib.Path) -> dict:
    env = dict(os.environ)
    env["PLEXAR_AGENTS_STATE"] = str(state)
    env["PLEXAR_AGENTS_LOG_DIR"] = str(logs)
    p = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=20,
    )
    assert p.returncode == 0, "a hook must never crash the tool call: %s" % p.stderr
    return json.loads(p.stdout or "{}")


def decision(out: dict) -> str | None:
    return (out.get("hookSpecificOutput") or {}).get("permissionDecision")


@pytest.fixture
def run(tmp_path):
    state, logs = tmp_path / "state", tmp_path / "logs"
    state.mkdir()
    logs.mkdir()
    owned = tmp_path / "repo" / "lib"
    owned.mkdir(parents=True)

    def activate(files_owned, enforce=True, run_id="R1"):
        (state / ("%s.json" % run_id)).write_text(json.dumps({
            "run_id": run_id, "enforce": enforce,
            "nodes": {"N01": {"files_owned": [str(f) for f in files_owned]}},
        }), encoding="utf-8")
        (state / "ACTIVE").write_text(run_id, encoding="utf-8")

    def rows():
        out = []
        for f in logs.glob("*.jsonl"):
            out += [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
        return out

    return type("R", (), {
        "state": state, "logs": logs, "tmp": tmp_path, "owned_dir": owned,
        "activate": staticmethod(activate), "rows": staticmethod(rows),
    })


def test_write_outside_owned_is_denied(run):
    run.activate([run.owned_dir / "session.py"])
    out = run_hook({"tool_name": "Write",
                    "tool_input": {"file_path": str(run.tmp / "repo" / "store.py")}},
                   run.state, run.logs)
    assert decision(out) == "deny"
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "store.py" in reason, "the reason must name the file that was refused"
    assert "planning defect" in reason, "the agent must be told not to work around it"


def test_write_inside_owned_is_allowed(run):
    target = run.owned_dir / "session.py"
    run.activate([target])
    out = run_hook({"tool_name": "Write", "tool_input": {"file_path": str(target)}},
                   run.state, run.logs)
    assert decision(out) is None


def test_owned_entry_may_be_a_directory(run):
    run.activate([run.owned_dir])
    out = run_hook({"tool_name": "Edit",
                    "tool_input": {"file_path": str(run.owned_dir / "deep" / "x.py")}},
                   run.state, run.logs)
    assert decision(out) is None


def test_path_separators_do_not_split_one_file_into_two(run):
    """Codex writes C:/Code/... and Claude writes C:\\Code\\... for the same file."""
    target = run.owned_dir / "session.py"
    run.activate([str(target).replace(os.sep, "/")])
    out = run_hook({"tool_name": "Write",
                    "tool_input": {"file_path": str(target).replace("/", os.sep)}},
                   run.state, run.logs)
    assert decision(out) is None, "separator drift must not manufacture a denial"


def test_no_active_run_enforces_nothing(run):
    """An ordinary session is untouched. Enforcement is a deliberate act."""
    out = run_hook({"tool_name": "Write", "tool_input": {"file_path": str(run.tmp / "anything.py")}},
                   run.state, run.logs)
    assert decision(out) is None
    assert run.rows() == [], "an ungoverned write is not this system's business"


def test_unreadable_state_never_blocks_work(run):
    (run.state / "ACTIVE").write_text("R-missing", encoding="utf-8")
    out = run_hook({"tool_name": "Write", "tool_input": {"file_path": str(run.tmp / "x.py")}},
                   run.state, run.logs)
    assert decision(out) is None


def test_enforce_false_is_observe_only(run):
    """The dial at its lowest notch: watch, record, never refuse."""
    run.activate([run.owned_dir / "session.py"], enforce=False)
    out = run_hook({"tool_name": "Write", "tool_input": {"file_path": str(run.tmp / "stray.py")}},
                   run.state, run.logs)
    assert decision(out) is None


def test_read_tools_are_never_touched(run):
    run.activate([run.owned_dir / "session.py"])
    for tool in ("Read", "Grep", "Glob", "Bash", "Task"):
        out = run_hook({"tool_name": tool, "tool_input": {"file_path": str(run.tmp / "x.py")}},
                       run.state, run.logs)
        assert decision(out) is None, "%s is not a write" % tool


def test_malformed_payload_allows_rather_than_blocks(run):
    run.activate([run.owned_dir / "session.py"])
    env = dict(os.environ)
    env["PLEXAR_AGENTS_STATE"] = str(run.state)
    p = subprocess.run([sys.executable, str(HOOK)], input="not json{",
                       capture_output=True, text=True, env=env, timeout=20)
    assert p.returncode == 0
    assert decision(json.loads(p.stdout or "{}")) is None, "our defect never blocks the caller"


def test_denial_is_recorded_by_the_runtime(run):
    """E6. The agent is never asked to write this row, so it cannot forget to."""
    run.activate([run.owned_dir / "session.py"])
    run_hook({"tool_name": "Write", "tool_input": {"file_path": str(run.tmp / "stray.py")}},
             run.state, run.logs)
    denials = [r for r in run.rows() if r["event"] == "enforced_denial"]
    assert len(denials) == 1
    d = denials[0]
    assert d["invariant"] == "E1"
    assert d["run_id"] == "R1"
    assert "stray.py" in d["path"]


def test_every_row_carries_a_utc_offset(run):
    """40 of 91 rows on 2026-09-21 did not, spanning a timezone change."""
    import datetime
    run.activate([run.owned_dir / "session.py"])
    run_hook({"tool_name": "Write", "tool_input": {"file_path": str(run.tmp / "stray.py")}},
             run.state, run.logs)
    for r in run.rows():
        assert datetime.datetime.fromisoformat(r["ts"]).tzinfo is not None, r["ts"]


def test_windows_paths_survive_the_json_round_trip(run):
    """One run_start row was destroyed by `C:\\Code` in a hand-built JSON string."""
    win = run.tmp / "repo" / "C-ish" / "store.py"
    run.activate([run.owned_dir])
    run_hook({"tool_name": "Write", "tool_input": {"file_path": str(win)}}, run.state, run.logs)
    d = [r for r in run.rows() if r["event"] == "enforced_denial"][0]
    assert pathlib.Path(d["path"]) == win, "the path must round-trip byte-exact"
