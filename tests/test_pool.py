"""B0an / B0cn / B0dn — the work pool.

The tests that matter: a task cannot skip to done, a selection without a reason is
refused rather than trimmed, and a released backlog does not become N concurrent agents.
"""
from __future__ import annotations

import json
import pathlib
import sys

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


def seed(bucket="plexar-chat", n=4):
    made = [tasks.create(bucket, "do the thing number %d, at length" % i) for i in range(n)]
    tasks.hold_all(bucket)
    return [t["id"] for t in made]


# ------------------------------------------------- D29/D24: a task is a held prompt

def test_a_task_is_created_drafted_and_nothing_runs(env):
    t = tasks.create("b", "a long-form prompt that will become a run")
    assert t["state"] == tasks.DRAFTED
    assert tasks.counts("b")["running"] == 0


def test_the_prompt_is_stored_whole_and_hashed_never_summarised(env):
    body = "line one\nline two\nline three"
    t = tasks.create("b", body)
    assert t["prompt"] == body
    import hashlib
    assert t["prompt_sha256"] == hashlib.sha256(body.encode()).hexdigest()


def test_held_is_a_real_state(env):
    tasks.create("b", "prompt long enough to matter")
    tasks.hold_all("b")
    c = tasks.counts("b")
    assert c["held"] == 1 and c["drafted"] == 0 and c["running"] == 0


def test_a_task_cannot_skip_to_done(env):
    tid = seed("b", 1)[0]
    with pytest.raises(tasks.IllegalTransition) as e:
        tasks.transition("b", tid, tasks.DONE)
    assert "only route to done is" in str(e.value)


def test_a_failure_returns_to_the_pool_not_to_done(env):
    tid = seed("b", 1)[0]
    sel = pool.select("b", [tid], "one task, grouped alone for this test", "A")
    pool.start("b", tid, "R1")
    pool.finish("b", tid, ok=False, gate_exit=1)
    assert tasks.get("b", tid)["state"] == tasks.FAILED
    tasks.transition("b", tid, tasks.HELD)
    assert tasks.get("b", tid)["state"] == tasks.HELD


def test_a_refused_transition_is_recorded(env, tmp_path):
    tid = seed("b", 1)[0]
    with pytest.raises(tasks.IllegalTransition):
        tasks.transition("b", tid, tasks.DONE)
    assert [r for r in rows(tmp_path) if r["event"] == "task_transition_refused"]


# ------------------------------------------------- D30/D31: the agent selects, capped

def test_the_agent_picks_its_own_batch(env):
    ids = seed("b", 4)
    sel = pool.select("b", ids[:3], "all three touch the i18n surface in fp-sales", "W1")
    assert sel["size"] == 3
    assert all(tasks.get("b", t)["state"] == tasks.SELECTED for t in ids[:3])
    assert tasks.get("b", ids[3])["state"] == tasks.HELD


def test_a_selection_without_a_reason_is_refused(env):
    ids = seed("b", 2)
    with pytest.raises(pool.SelectionRefused) as e:
        pool.select("b", ids, "", "W1")
    assert "no_reason" in str(e.value)
    assert all(tasks.get("b", t)["state"] == tasks.HELD for t in ids), "nothing moved"


def test_a_one_word_reason_is_not_a_reason(env):
    ids = seed("b", 2)
    with pytest.raises(pool.SelectionRefused):
        pool.select("b", ids, "related", "W1")


def test_an_over_cap_selection_is_REFUSED_not_trimmed(env):
    ids = seed("b", 6)
    pool.set_policy("b", cap=3)
    with pytest.raises(pool.SelectionRefused) as e:
        pool.select("b", ids, "six tasks that all touch the same module", "W1")
    assert "not trimmed for you" in str(e.value)
    assert all(tasks.get("b", t)["state"] == tasks.HELD for t in ids), \
        "trimming would hide that the agent wanted more than it may have"


def test_a_task_that_is_not_held_cannot_be_selected(env):
    ids = seed("b", 2)
    pool.select("b", [ids[0]], "taking the first one for this run", "W1")
    with pytest.raises(pool.SelectionRefused) as e:
        pool.select("b", [ids[0]], "trying to take it twice, at length", "W2")
    assert "not_held" in str(e.value)


def test_the_cap_is_labelled_unmeasured(env):
    assert "UNMEASURED" in pool.policy("b")["cap_source"], \
        "the cap is a guess; the ledger must not present it as a finding"


def test_selection_size_is_recorded_so_the_cap_can_be_measured(env, tmp_path):
    ids = seed("b", 3)
    pool.select("b", ids, "all three are label changes in the same file", "W1")
    r = [x for x in rows(tmp_path) if x["event"] == "selection"][0]
    assert r["selection_size"] == 3 and r["cap"] == pool.DEFAULT_CAP
    assert r["reason"].startswith("all three")


def test_a_refusal_is_recorded_with_its_code(env, tmp_path):
    ids = seed("b", 2)
    with pytest.raises(pool.SelectionRefused):
        pool.select("b", ids, "x", "W1")
    r = [x for x in rows(tmp_path) if x["event"] == "selection_refused"][0]
    assert r["code"] == "no_reason"


# ------------------------------------------------- D26: the drain is bounded

def test_a_released_backlog_does_not_become_n_concurrent_agents(env):
    ids = seed("b", 5)
    pool.set_policy("b", concurrency=2, cap=10)
    sel = pool.select("b", ids, "five tasks released together for the drain test", "W1")
    pool.start("b", ids[0], "R1")
    pool.start("b", ids[1], "R2")
    with pytest.raises(pool.ConcurrencyExceeded) as e:
        pool.start("b", ids[2], "R3")
    assert "not a licence to fan out" in str(e.value)
    assert tasks.get("b", ids[2])["state"] == tasks.SELECTED, "it waits, it does not fail"


def test_the_drain_runs_what_it_can_and_reports_what_it_throttled(env):
    ids = seed("b", 4)
    pool.set_policy("b", concurrency=1, cap=10)
    sel = pool.select("b", ids, "four tasks for a throttled drain, all one module", "W1")
    got = pool.drain("b", sel, runner=lambda t: (True, 0))
    assert len(got["ran"]) >= 1
    assert got["counts"]["done"] == len(got["ran"])
    assert len(got["ran"]) + len(got["throttled"]) == 4


def test_the_gate_exit_decides_done_not_the_runner_s_opinion(env):
    ids = seed("b", 2)
    sel = pool.select("b", ids, "two tasks, one will claim success wrongly", "W1")
    got = pool.drain("b", sel, runner=lambda t: (True, 1))     # claims ok, gate says 1
    assert all(r["ok"] for r in got["ran"])
    assert got["counts"]["done"] == 0 and got["counts"]["failed"] == len(got["ran"])


def test_a_runner_that_raises_fails_the_task_not_the_drain(env, tmp_path):
    ids = seed("b", 2)
    sel = pool.select("b", ids, "two tasks; the runner will explode on both", "W1")

    def boom(t):
        raise RuntimeError("worker died")

    got = pool.drain("b", sel, runner=boom)
    assert got["counts"]["failed"] == len(got["ran"])
    assert [r for r in rows(tmp_path) if r["event"] == "task_runner_error"]


def test_the_default_approval_is_ask(env, monkeypatch):
    monkeypatch.undo()          # the fixture relaxes it for other tests; this one reads the real default
    assert pool.DEFAULT_APPROVAL == "ask"
    assert pool.policy("b")["approval"] == "ask", "D18: a scratch bucket and the rig differ"


def test_the_cap_binds_ACROSS_processes_which_is_the_real_case(env, tmp_path):
    """A synchronous drain never overlaps, so the cap only means something between
    concurrent drains. Two real processes, one bucket."""
    import subprocess, sys, textwrap, os
    ids = seed("cross", 4)
    pool.set_policy("cross", concurrency=1, cap=10)
    sel = pool.select("cross", ids, "four tasks drained by two competing processes", "W1")
    code = textwrap.dedent("""
        import sys; sys.path.insert(0, %r)
        from plexar_agents import pool, tasks
        held = [t["id"] for t in tasks.in_state("cross", tasks.SELECTED)]
        ok = 0
        for tid in held:
            try:
                pool.start("cross", tid, "R-" + tid); ok += 1
            except pool.DrainSkipped:      # full, or someone else took it
                pass
        print(ok)
    """) % str(ROOT)
    e = {**os.environ,
         "PLEXAR_AGENTS_STATE": str(tmp_path / "state" / "runs"),
         "PLEXAR_AGENTS_LOG_DIR": str(tmp_path / "logs")}
    procs = [subprocess.Popen([sys.executable, "-c", code], env=e,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for _ in range(3)]
    outs = [p.communicate() for p in procs]
    started = sum(int(o.strip() or 0) for o, _ in outs)
    assert all(not err.strip() for _, err in outs), [err[-600:] for _, err in outs]
    assert started == 1, "three processes, concurrency 1 — exactly one task may start"
    assert len(pool.running("cross")) == 1


def test_already_claimed_is_routine_not_a_crash(env):
    """Measured: three racing processes produced one start, one throttle, and one CRASH,
    because 'someone else took it' was raised as an IllegalTransition."""
    ids = seed("claimed", 2)
    pool.set_policy("claimed", concurrency=5, cap=10)
    pool.select("claimed", ids, "two tasks; one will be claimed twice on purpose", "W1")
    pool.start("claimed", ids[0], "R1")
    with pytest.raises(pool.TaskAlreadyClaimed) as e:
        pool.start("claimed", ids[0], "R2")
    assert "another drain took it" in str(e.value)
    assert isinstance(e.value, pool.DrainSkipped), "a drain loop catches ONE thing"


def test_both_skip_reasons_share_a_base_so_a_drain_loop_survives_either(env):
    assert issubclass(pool.ConcurrencyExceeded, pool.DrainSkipped)
    assert issubclass(pool.TaskAlreadyClaimed, pool.DrainSkipped)
