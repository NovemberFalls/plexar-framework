"""B5n / D17 — silence is not consent.

Nothing dispatches from a selection in an `ask` bucket until a named human approves it;
a rejection returns the work to HELD; a timeout HALTS and never becomes a yes.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plexar_agents import pool, tasks  # noqa: E402

B = "ask-bucket"
WHY = "same module, one test file, one reviewer"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLEXAR_AGENTS_STATE", str(tmp_path / "state" / "runs"))
    monkeypatch.setenv("PLEXAR_AGENTS_LOG_DIR", str(tmp_path / "logs"))
    return tmp_path


def sel(n=2):
    ids = [tasks.create(B, "task %d in full" % i)["id"] for i in range(n)]
    tasks.hold_all(B)
    return pool.select(B, ids, WHY, "agent")


def test_an_ask_selection_is_pending_and_cannot_start(env):
    s = sel()
    assert s["approval"] == "pending"
    with pytest.raises(pool.ApprovalRequired):
        pool.start(B, s["tasks"][0], "r1")
    assert tasks.get(B, s["tasks"][0])["state"] == tasks.SELECTED


def test_approval_needs_a_name_and_then_starts(env):
    s = sel()
    with pytest.raises(pool.ApprovalRequired):
        pool.approve(B, s["selection_id"], "  ")
    pool.approve(B, s["selection_id"], "len")
    assert pool.start(B, s["tasks"][0], "r1")["state"] == tasks.RUNNING


def test_a_decision_is_taken_once(env):
    s = sel()
    pool.approve(B, s["selection_id"], "len")
    with pytest.raises(pool.ApprovalRequired):
        pool.reject(B, s["selection_id"], "len")


def test_reject_returns_tasks_to_held(env):
    s = sel()
    pool.reject(B, s["selection_id"], "len")
    assert {tasks.get(B, t)["state"] for t in s["tasks"]} == {tasks.HELD}


def test_timeout_halts_never_approves(env):
    s = sel()
    assert pool.expire(B, timeout_s=-1) == [s["selection_id"]]
    assert pool.selection(B, s["selection_id"])["approval"] == "expired"
    assert {tasks.get(B, t)["state"] for t in s["tasks"]} == {tasks.HELD}
    with pytest.raises(pool.ApprovalRequired):
        pool.approve(B, s["selection_id"], "len")


def test_a_fresh_pending_selection_is_not_expired(env):
    s = sel()
    assert pool.expire(B) == []
    assert pool.selection(B, s["selection_id"])["approval"] == "pending"


def test_auto_bucket_needs_no_approval(env):
    pool.set_policy(B, approval="auto")
    s = sel(1)
    assert s["approval"] == "not_required"
    assert pool.start(B, s["tasks"][0], "r")["state"] == tasks.RUNNING
