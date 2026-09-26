"""Session attribution — the runtime supplies it, so nobody can forget it.

The defect being fixed is measured: 50 `run_start` rows in the live ledger, 0 carrying
`session_id`, under a prose rule that said to record it.
"""
from __future__ import annotations

import json
import pathlib
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from plexar_agents import ledger, session  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLEXAR_AGENTS_STATE", str(tmp_path / "state" / "runs"))
    monkeypatch.setenv("PLEXAR_AGENTS_LOG_DIR", str(tmp_path / "logs"))
    return tmp_path


def rows(tmp_path):
    out = []
    for f in (tmp_path / "logs").glob("*.jsonl"):
        out += [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
    return out


def test_a_row_carries_the_session_without_the_caller_supplying_it(env):
    session.record("SID-1", str(env / "repo"))
    ledger.write("run_start", "R1", None, cwd=str(env / "repo"), task_summary="x")
    r = rows(env)[0]
    assert r["session_id"] == "SID-1", "the runtime attaches it; the agent is never asked"


def test_an_explicit_session_id_is_never_overwritten(env):
    session.record("SID-1", str(env / "repo"))
    ledger.write("run_start", "R1", None, cwd=str(env / "repo"), session_id="SID-EXPLICIT")
    assert rows(env)[0]["session_id"] == "SID-EXPLICIT"


def test_no_pointer_means_no_field_rather_than_a_guess(env):
    ledger.write("run_start", "R1", None, cwd=str(env / "nowhere"))
    assert "session_id" not in rows(env)[0], "absent is correct; a guessed pane would be a lie"


def test_a_stale_pointer_yields_nothing(env, monkeypatch):
    session.record("SID-OLD", str(env / "repo"))
    p = session.dir_() / (session.key(str(env / "repo")) + ".json")
    rec = json.loads(p.read_text(encoding="utf-8"))
    rec["at"] = time.time() - (session.MAX_AGE_S + 60)
    p.write_text(json.dumps(rec), encoding="utf-8", newline="\n")
    assert session.session_id(str(env / "repo")) is None, "a wrong pane is worse than no pane"


def test_separator_drift_does_not_split_one_pane_into_two(env):
    """Codex writes C:/Code/... and Claude writes C:\\Code\\... for one directory."""
    forward = str(env / "repo").replace("\\", "/")
    back = str(env / "repo").replace("/", "\\")
    session.record("SID-1", forward)
    assert session.session_id(back) == "SID-1"


def test_two_panes_in_two_directories_stay_distinct(env):
    session.record("SID-A", str(env / "repoA"))
    session.record("SID-B", str(env / "repoB"))
    assert session.session_id(str(env / "repoA")) == "SID-A"
    assert session.session_id(str(env / "repoB")) == "SID-B"
    assert len(session.panes()) == 2


def test_recording_without_a_session_id_writes_nothing(env):
    session.record(None, str(env / "repo"))
    assert session.session_id(str(env / "repo")) is None
    assert session.panes() == []


def test_a_corrupt_pointer_never_raises_into_the_caller(env):
    session.record("SID-1", str(env / "repo"))
    (session.dir_() / (session.key(str(env / "repo")) + ".json")).write_text(
        "{not json", encoding="utf-8")
    assert session.session_id(str(env / "repo")) is None
    ledger.write("run_start", "R1", None, cwd=str(env / "repo"))
    assert len(rows(env)) == 1, "a broken pointer must not cost a ledger row"


def test_the_pointer_is_written_atomically(env):
    """A reader must never see a half-written file, so no .tmp is left behind."""
    session.record("SID-1", str(env / "repo"))
    assert list(session.dir_().glob("*.tmp")) == []
    assert len(list(session.dir_().glob("*.json"))) == 1


def test_panes_reports_staleness_rather_than_hiding_it(env):
    session.record("SID-1", str(env / "repo"))
    p = session.dir_() / (session.key(str(env / "repo")) + ".json")
    rec = json.loads(p.read_text(encoding="utf-8"))
    rec["at"] = time.time() - (session.MAX_AGE_S + 60)
    p.write_text(json.dumps(rec), encoding="utf-8", newline="\n")
    got = session.panes()
    assert len(got) == 1 and got[0]["stale"] is True, "a stale pane is listed, marked, not dropped"
