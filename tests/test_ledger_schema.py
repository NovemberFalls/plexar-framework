"""Rows the framework writes conform to the pinned schema the harness will test against."""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plexar_agents import ledger  # noqa: E402


def test_missing_and_bad_enum_are_reported():
    bad = {"ts": "x", "event": "outcome", "run_id": "r", "node_id": "n", "status": "OK"}
    p = ledger.validate(bad)
    assert any("missing wall_ms" in x for x in p) and any("status" in x for x in p)


def test_daemon_rows_conform(tmp_path, monkeypatch):
    # reuse the live-telemetry fixture shape: one real daemon task, then validate every row
    import json
    sys.path.insert(0, str(ROOT / "tests"))
    import test_telemetry_live as T
    monkeypatch.setenv("PLEXAR_AGENTS_STATE", str(tmp_path / "state" / "runs"))
    monkeypatch.setenv("PLEXAR_AGENTS_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("PLEXAR_AGENTS_ARTIFACTS", str(tmp_path / "artifacts"))
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)
    run("init", "-q", "-b", "main")
    (repo / "a.txt").write_text("a")
    run("add", "-A")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "b")
    T.run_one(tmp_path)
    rows = T.rows(tmp_path)
    checked = [r for r in rows if r["event"] in ("dispatch", "outcome", "run")]
    assert len(checked) == 3
    for r in checked:
        assert ledger.validate(r) == [], (r["event"], ledger.validate(r))
