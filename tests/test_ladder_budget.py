"""A3n — the two invariants a stateless hook cannot hold.

Both count across **spawns**, and a spawn is a separate OS process. So the concurrency
test uses real subprocesses, not threads: threads share an interpreter and would pass
against a counter that is not actually process-safe.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import textwrap

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plexar_agents import budget, ladder, store  # noqa: E402


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


# ---------------------------------------------------------------- ladder

def test_three_attempts_then_a_person(env):
    for i in (1, 2, 3):
        got = ladder.attempt("R1", "N01", lane="sonnet")
        assert got["attempt"] == i
        assert got["remaining"] == 3 - i
    with pytest.raises(ladder.LadderExhausted) as e:
        ladder.attempt("R1", "N01", lane="sonnet")
    assert "then a person" in str(e.value)
    assert "Do not spawn again" in str(e.value)


def test_the_count_survives_a_separate_process(env, tmp_path):
    """The actual defect: each spawn is a fresh process with no memory of the last."""
    ladder.attempt("R1", "N01", lane="haiku")
    code = textwrap.dedent("""
        import sys; sys.path.insert(0, %r)
        from plexar_agents import ladder
        print(ladder.attempt("R1", "N01", lane="haiku")["attempt"])
    """) % str(ROOT)
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env={**dict(__import__("os").environ),
                            "PLEXAR_AGENTS_STATE": str(tmp_path / "state" / "runs"),
                            "PLEXAR_AGENTS_LOG_DIR": str(tmp_path / "logs")})
    assert p.returncode == 0, p.stderr
    assert p.stdout.strip() == "2", "a second PROCESS must see the first process's attempt"


def test_concurrent_processes_do_not_lose_a_count(env, tmp_path):
    """Two hooks firing at once would each read 2, each write 3, losing an attempt."""
    code = textwrap.dedent("""
        import sys; sys.path.insert(0, %r)
        from plexar_agents import ladder
        try:
            ladder.attempt("RC", "N01", lane="haiku")
        except ladder.LadderExhausted:
            pass
    """) % str(ROOT)
    e = {**dict(__import__("os").environ),
         "PLEXAR_AGENTS_STATE": str(tmp_path / "state" / "runs"),
         "PLEXAR_AGENTS_LOG_DIR": str(tmp_path / "logs")}
    procs = [subprocess.Popen([sys.executable, "-c", code], env=e) for _ in range(6)]
    for p in procs:
        p.wait()
    assert ladder.attempts("RC", "N01") == 6, "every concurrent attempt must be counted"


def test_the_lane_goes_up_one_tier_on_a_retry(env):
    first = ladder.attempt("R1", "N01", lane="haiku")
    assert first["use_lane"] == "haiku", "the first attempt is not an escalation"
    second = ladder.attempt("R1", "N01", lane="haiku")
    assert second["use_lane"] == "sonnet"


def test_opus_has_nowhere_to_escalate_to(env):
    assert ladder.escalate("opus") == "opus"


def test_a_resumed_worker_is_distinguished_from_a_respawn(env, tmp_path):
    """Both produce attempts: 2 and they are completely different inputs."""
    ladder.attempt("R1", "N01", lane="sonnet", retry_mode="initial")
    ladder.attempt("R1", "N01", lane="sonnet", retry_mode="resume_delta")
    r = [x for x in rows(tmp_path) if x["event"] == "ladder_attempt"][-1]
    assert r["retry_mode"] == "resume_delta"


def test_the_runtime_says_who_counted(env, tmp_path):
    ladder.attempt("R1", "N01", lane="sonnet")
    r = [x for x in rows(tmp_path) if x["event"] == "ladder_attempt"][0]
    assert r["counted_by"] == "runtime", "attempts used to be a self-report; the row says so"


def test_exhaustion_is_recorded_not_only_raised(env, tmp_path):
    for _ in range(3):
        ladder.attempt("R1", "N01", lane="sonnet")
    with pytest.raises(ladder.LadderExhausted):
        ladder.attempt("R1", "N01", lane="sonnet")
    assert [x for x in rows(tmp_path) if x["event"] == "ladder_exhausted"]


# ---------------------------------------------------------------- budget

def test_a_spawn_that_would_exceed_the_ceiling_is_refused(env):
    budget.set_ceiling("R2", 1.00)
    budget.charge("R2", 0.80, source="measured")
    with pytest.raises(budget.BudgetExceeded) as e:
        budget.check("R2", projected=0.50)
    assert "The run STOPS" in str(e.value)
    assert "cheaper models" in str(e.value), "it must refuse silent degradation by name"


def test_a_spawn_inside_the_ceiling_passes(env):
    budget.set_ceiling("R2", 1.00)
    budget.charge("R2", 0.20, source="measured")
    assert budget.check("R2", projected=0.50)["remaining"] == pytest.approx(0.80)


def test_no_ceiling_means_nothing_is_enforced(env):
    assert budget.check("R3", projected=999.0)["ceiling"] is None


def test_spend_accumulates_across_processes(env, tmp_path):
    budget.set_ceiling("R4", 10.0)
    code = textwrap.dedent("""
        import sys; sys.path.insert(0, %r)
        from plexar_agents import budget
        budget.charge("R4", 1.5, source="measured")
    """) % str(ROOT)
    e = {**dict(__import__("os").environ),
         "PLEXAR_AGENTS_STATE": str(tmp_path / "state" / "runs"),
         "PLEXAR_AGENTS_LOG_DIR": str(tmp_path / "logs")}
    for _ in range(4):
        subprocess.run([sys.executable, "-c", code], env=e, check=True)
    assert budget.state("R4")["spent"] == pytest.approx(6.0)


def test_an_estimate_is_never_indistinguishable_from_a_measurement(env, tmp_path):
    budget.charge("R5", 0.10, source="estimated")
    budget.charge("R5", 0.10, source="measured")
    srcs = [r["source"] for r in rows(tmp_path) if r["event"] == "budget_charge"]
    assert srcs == ["estimated", "measured"]


def test_a_refusal_is_recorded_before_it_raises(env, tmp_path):
    budget.set_ceiling("R6", 0.10)
    with pytest.raises(budget.BudgetExceeded):
        budget.check("R6", projected=1.0)
    assert [r for r in rows(tmp_path) if r["event"] == "budget_refused"]


# ---------------------------------------------------------------- store

def test_a_stale_lock_is_broken_rather_than_wedging_the_estate(env, monkeypatch):
    p = store._path("R7")
    p.parent.mkdir(parents=True, exist_ok=True)
    lock = p.with_suffix(".lock")
    lock.write_text("99999", encoding="utf-8")
    import os as _os
    import time as _time
    _os.utime(lock, (_time.time() - 3600, _time.time() - 3600))
    store.update("R7", lambda s: {**s, "ok": True})
    assert store.read("R7")["ok"] is True


def test_a_corrupt_state_file_does_not_raise(env):
    p = store._path("R8")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    assert store.read("R8") == {}
    store.update("R8", lambda s: {**s, "recovered": True})
    assert store.read("R8")["recovered"] is True


def test_no_temp_files_are_left_behind(env):
    store.update("R9", lambda s: {**s, "a": 1})
    assert list(store.root().glob("*.tmp*")) == []
    assert list(store.root().glob("*.lock")) == []
