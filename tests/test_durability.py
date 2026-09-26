"""Durability — a task whose owner died does not hold a slot forever.

The gap LangGraph closes with a checkpointer. Without this, a daemon that dies mid-drain
leaves tasks RUNNING with no process behind them, the concurrency slots stay consumed,
and nothing ever runs again.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import textwrap
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plexar_agents import pool, tasks  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLEXAR_AGENTS_STATE", str(tmp_path / "state" / "runs"))
    monkeypatch.setenv("PLEXAR_AGENTS_LOG_DIR", str(tmp_path / "logs"))
    # These tests are about other mechanics; approval (D17) has its own tests.
    monkeypatch.setattr(pool, "DEFAULT_APPROVAL", "auto")
    return tmp_path


def rows(tmp_path):
    out = []
    for f in (tmp_path / "logs").glob("*.jsonl"):
        out += [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
    return out


def seed(bucket, n=2):
    made = [tasks.create(bucket, "a prompt long enough to be a real brief %d" % i)
            for i in range(n)]
    tasks.hold_all(bucket)
    ids = [t["id"] for t in made]
    pool.select(bucket, ids, "tasks grouped for a durability test, same module", "W1")
    return ids


def test_a_running_task_records_who_owns_it_and_when(env):
    ids = seed("d", 1)
    pool.start("d", ids[0], "R1")
    t = tasks.get("d", ids[0])
    assert t["owner_pid"] == os.getpid()
    assert isinstance(t["heartbeat"], float)


def test_a_live_task_is_not_reaped(env):
    ids = seed("d", 1)
    pool.start("d", ids[0], "R1")
    assert pool.reap("d", stale_after_s=60) == []
    assert tasks.get("d", ids[0])["state"] == tasks.RUNNING


def test_a_silent_task_is_reaped(env):
    ids = seed("d", 1)
    pool.start("d", ids[0], "R1")
    time.sleep(0.05)
    got = pool.reap("d", stale_after_s=0.01)
    assert len(got) == 1
    assert tasks.get("d", ids[0])["state"] == tasks.ABANDONED


def test_abandoned_is_NOT_failed(env):
    """A reaped task never got a verdict. 'Nobody knows' and 'it was wrong' differ."""
    ids = seed("d", 1)
    pool.start("d", ids[0], "R1")
    time.sleep(0.05)
    pool.reap("d", stale_after_s=0.01)
    c = tasks.counts("d")
    assert c["abandoned"] == 1 and c["failed"] == 0 and c["done"] == 0


def test_beating_prevents_a_reap(env):
    ids = seed("d", 1)
    pool.start("d", ids[0], "R1")
    time.sleep(0.05)
    pool.beat("d", ids[0])
    assert pool.reap("d", stale_after_s=0.03) == []


def test_a_reaped_task_frees_its_concurrency_slot(env):
    """THE failure: slots held by dead processes, so nothing ever runs again."""
    ids = seed("d", 2)
    pool.set_policy("d", concurrency=1, cap=10)
    pool.start("d", ids[0], "R1")
    with pytest.raises(pool.ConcurrencyExceeded):
        pool.start("d", ids[1], "R2")
    time.sleep(0.05)
    pool.reap("d", stale_after_s=0.01)
    pool.start("d", ids[1], "R2")          # the slot is free again
    assert tasks.get("d", ids[1])["state"] == tasks.RUNNING


def test_recover_puts_the_work_back_in_the_pool(env):
    ids = seed("d", 2)
    pool.start("d", ids[0], "R1")
    time.sleep(0.05)
    got = pool.recover("d", stale_after_s=0.01)
    assert got["reaped"] == [ids[0]]
    assert tasks.get("d", ids[0])["state"] == tasks.HELD, "it can be selected again"


def test_an_abandoned_task_cannot_be_marked_done(env):
    ids = seed("d", 1)
    pool.start("d", ids[0], "R1")
    time.sleep(0.05)
    pool.reap("d", stale_after_s=0.01)
    with pytest.raises(tasks.IllegalTransition):
        tasks.transition("d", ids[0], tasks.DONE)


def test_the_row_says_how_long_it_was_silent_and_how_it_was_detected(env, tmp_path):
    ids = seed("d", 1)
    pool.start("d", ids[0], "R1")
    time.sleep(0.05)
    pool.reap("d", stale_after_s=0.01)
    r = [x for x in rows(tmp_path) if x["event"] == "task_abandoned"][0]
    assert r["silent_for_s"] > 0
    assert r["detected_by"] == "heartbeat", "not the pid — pid reuse can lie"
    assert "UNMEASURED" in r["stale_after_source"]


def test_a_task_started_by_a_process_that_DIES_is_recovered(env, tmp_path):
    """The real case, with a real process that really exits."""
    ids = seed("d", 1)
    code = textwrap.dedent("""
        import sys; sys.path.insert(0, %r)
        from plexar_agents import pool
        pool.start("d", %r, "R-dead")
    """) % (str(ROOT), ids[0])
    e = {**os.environ,
         "PLEXAR_AGENTS_STATE": str(tmp_path / "state" / "runs"),
         "PLEXAR_AGENTS_LOG_DIR": str(tmp_path / "logs")}
    p = subprocess.run([sys.executable, "-c", code], env=e, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert tasks.get("d", ids[0])["state"] == tasks.RUNNING, "owned by a process that is now gone"

    time.sleep(0.05)
    got = pool.recover("d", stale_after_s=0.01)
    assert got["reaped"] == [ids[0]]
    assert tasks.get("d", ids[0])["state"] == tasks.HELD


def test_reaping_an_empty_bucket_is_not_an_error(env):
    assert pool.reap("nothing-here") == []
