"""A6n — D7, D8, D9.

The test that matters most is the last one: two agents agreeing does not land a change.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plexar_agents import collisions  # noqa: E402


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


# ------------------------------------------------- D7: negotiate, do not fail

def test_a_collision_opens_a_negotiation_carrying_both_parties(env):
    got = collisions.open_("R1", "lib/session.py", claimant="N04", holder="N07")
    assert got["round"] == 1
    note = pathlib.Path(got["note"]).read_text(encoding="utf-8")
    assert "## N07" in note and "## N04" in note, "both parties get a heading"
    assert "Neither of you may write it yet" in note


def test_the_note_says_agreement_will_not_be_enough(env):
    got = collisions.open_("R1", "lib/session.py", claimant="N04", holder="N07")
    note = pathlib.Path(got["note"]).read_text(encoding="utf-8")
    assert "does NOT land" in note and "gate script still decides" in note


def test_a_party_s_own_words_are_never_clobbered(env):
    got = collisions.open_("R1", "lib/session.py", claimant="N04", holder="N07")
    p = pathlib.Path(got["note"])
    p.write_text("# I need the store passed explicitly\n", encoding="utf-8")
    collisions.open_("R1", "lib/session.py", claimant="N04", holder="N07")
    assert p.read_text(encoding="utf-8") == "# I need the store passed explicitly\n"


# ------------------------------------------------- D8: bounded, then one tier up

def test_it_is_bounded_and_escalates_rather_than_looping(env):
    for i in (1, 2, 3):
        assert collisions.open_("R1", "a.py", "N04", "N07")["round"] == i
    with pytest.raises(collisions.NegotiationUnresolved) as e:
        collisions.open_("R1", "a.py", "N04", "N07")
    assert "Escalating to parent" in str(e.value)
    assert "Neither node should write it" in str(e.value)


def test_a_second_failure_reaches_the_orchestrator(env):
    for _ in range(3):
        collisions.open_("R1", "a.py", "N04", "N07")
    with pytest.raises(collisions.NegotiationUnresolved):
        collisions.open_("R1", "a.py", "N04", "N07")
    with pytest.raises(collisions.NegotiationUnresolved) as e:
        collisions.open_("R1", "a.py", "N04", "N07")
    assert "orchestrator" in str(e.value)


def test_the_round_bound_is_configurable(env):
    collisions.open_("R1", "b.py", "N04", "N07", rounds=1)
    with pytest.raises(collisions.NegotiationUnresolved):
        collisions.open_("R1", "b.py", "N04", "N07")


# ------------------------------------------------- D9: consensus is not a gate

def test_consensus_without_a_passing_gate_does_not_land(env):
    collisions.open_("R1", "c.py", "N04", "N07")
    with pytest.raises(collisions.ConsensusIsNotAGate) as e:
        collisions.resolve("R1", "c.py", consensus=True, gate_exit=1)
    assert "never sufficient" in str(e.value)
    assert collisions.state("R1", "c.py")["resolved"] is False


def test_a_gate_that_did_not_run_is_not_a_pass(env):
    collisions.open_("R1", "c.py", "N04", "N07")
    with pytest.raises(collisions.ConsensusIsNotAGate) as e:
        collisions.resolve("R1", "c.py", consensus=True, gate_exit=None)
    assert "did not run" in str(e.value)


def test_a_passing_gate_without_consensus_does_not_land_either(env):
    collisions.open_("R1", "c.py", "N04", "N07")
    with pytest.raises(collisions.ConsensusIsNotAGate) as e:
        collisions.resolve("R1", "c.py", consensus=False, gate_exit=0)
    assert "no consensus" in str(e.value)


def test_both_together_land_it(env):
    collisions.open_("R1", "c.py", "N04", "N07")
    got = collisions.resolve("R1", "c.py", consensus=True, gate_exit=0,
                             resolution="store resolved from context; N04 threads it")
    assert got["resolved"] is True


# ------------------------------------------------- the record

def test_every_round_is_recorded_with_its_bound(env, tmp_path):
    collisions.open_("R1", "d.py", "N04", "N07")
    r = [x for x in rows(tmp_path) if x["event"] == "collision"][0]
    assert r["collision_round"] == 1 and r["max_rounds"] == 3
    assert r["holder"] == "N07" and r["node_id"] == "N04"


def test_a_refusal_records_why_in_words(env, tmp_path):
    collisions.open_("R1", "e.py", "N04", "N07")
    with pytest.raises(collisions.ConsensusIsNotAGate):
        collisions.resolve("R1", "e.py", consensus=True, gate_exit=2)
    r = [x for x in rows(tmp_path) if x["event"] == "collision_resolved"][0]
    assert r["landed"] is False
    assert "gate exited 2" in r["refused_reason"]
