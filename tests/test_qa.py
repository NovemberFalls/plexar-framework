"""M3 — the QA record model, coverage refusal, and the smoke contract.

The test that matters most is the one where a NOT RUN case refuses a coverage claim.
Everything else protects it.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plexar_agents import coverage, qa, smoke  # noqa: E402


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


def seed(p="proj"):
    qa.add_case(p, "QA-001", "login rejects an expired token",
                "post with an expired token", "401", cls=qa.MACHINE, area="auth")
    qa.add_case(p, "QA-002", "the settings page feels instant",
                "open settings", "no visible delay", cls=qa.HUMAN, area="ui")
    qa.add_case(p, "QA-003", "invoice drops no EU tax line",
                "render an EU invoice", "tax line present", cls=qa.MACHINE, area="billing")
    return ["QA-001", "QA-002", "QA-003"]


# ------------------------------------------------- D19: case / issue / result

def test_a_case_holds_no_verdict(env):
    c = qa.add_case("p", "QA-1", "t", "s", "e")
    assert "verdict" not in c and "status" not in c


def test_a_verdict_belongs_to_a_dated_result_inside_a_run(env):
    seed()
    r = qa.record_result("proj", "round-1", "QA-001", qa.PASS,
                         cmd="pytest -k expired", exit_code=0)
    assert r["run"] == "round-1" and r["at"]
    assert qa.latest_verdict("proj", "QA-001")["verdict"] == qa.PASS


def test_a_reopen_does_not_erase_the_earlier_pass(env):
    seed()
    qa.record_result("proj", "round-1", "QA-001", qa.PASS, cmd="c", exit_code=0)
    qa.record_result("proj", "round-2", "QA-001", qa.FAIL)
    hist = [r["verdict"] for r in qa.read("proj")["results"] if r["case_id"] == "QA-001"]
    assert hist == [qa.PASS, qa.FAIL], "the historical pass really did happen"


# ------------------------------------------------- the AUTO evidence shape

def test_an_auto_pass_without_a_command_is_refused(env):
    seed()
    with pytest.raises(qa.QARefused) as e:
        qa.record_result("proj", "r", "QA-001", qa.PASS, auth=qa.AUTO)
    assert "no difference between a test that ran and a claim" in str(e.value)


def test_an_auto_pass_with_a_nonzero_exit_is_refused(env):
    seed()
    with pytest.raises(qa.QARefused) as e:
        qa.record_result("proj", "r", "QA-001", qa.PASS, cmd="pytest", exit_code=1)
    assert "not answerable to the implementation" in str(e.value)


def test_an_owner_pass_needs_no_command(env):
    seed()
    r = qa.record_result("proj", "r", "QA-002", qa.PASS, auth=qa.OWNER,
                         evidence="owner verdict, Chrome 141")
    assert r["auth"] == qa.OWNER


# ------------------------------------------------- three axes + null

def test_status_null_pauses_selection_and_needs_a_reason(env):
    qa.open_issue("p", "I-1", "t", "P2")
    with pytest.raises(qa.QARefused) as e:
        qa.set_lifecycle("p", "I-1", status=None)
    assert "nobody looked" in str(e.value)
    got = qa.set_lifecycle("p", "I-1", status=None, why="needs a debug fixture we lack")
    assert got["status"] is None


def test_the_three_axes_move_independently(env):
    qa.open_issue("p", "I-1", "t", "P1")
    got = qa.set_lifecycle("p", "I-1", queue="owner-review", readiness="ready")
    assert got["status"] == "open" and got["queue"] == "owner-review"
    assert got["readiness"] == "ready"


def test_a_rejection_carries_a_reason(env):
    qa.open_issue("p", "I-1", "t", "P2")
    with pytest.raises(qa.QARefused) as e:
        qa.set_lifecycle("p", "I-1", status="rejected")
    assert "can only record fixes cannot show you the decisions" in str(e.value)


# ------------------------------------------------- D36: severity is its own axis

def test_severity_lives_on_the_defect_and_is_required(env):
    with pytest.raises(qa.QARefused) as e:
        qa.open_issue("p", "I-1", "t", "High")
    assert "different fact from whether a test passed" in str(e.value)


def test_a_p0_with_every_test_passing_is_representable(env):
    seed()
    qa.open_issue("proj", "I-1", "security hole nobody wrote a test for", "P0")
    for cid in ("QA-001", "QA-003"):
        qa.record_result("proj", "r", cid, qa.PASS, cmd="pytest", exit_code=0)
    issue = qa.read("proj")["issues"]["I-1"]
    assert issue["severity"] == "P0" and issue["status"] == "open"


# ------------------------------------------------- D37: root-cause clustering

def test_one_defect_explains_many_results(env):
    seed()
    d = qa.cluster("proj", "BUG-10",
                   "isPro() compared tier to the literal 'pro' so the 'starter' tier "
                   "failed every Pro gate on desktop and mobile alike",
                   ["QA-001", "QA-003"])
    assert d["explains"] == ["QA-001", "QA-003"]


def test_closing_a_root_cause_proposes_ONE_backlog_node(env):
    seed()
    qa.cluster("proj", "BUG-10",
               "isPro() compared tier to the literal 'pro' so the 'starter' tier failed "
               "every Pro gate", ["QA-001", "QA-003"])
    got = qa.retests_for("proj", "BUG-10")
    assert got["backlog_nodes"] == 1 and len(got["retest_cases"]) == 2
    assert "fixes the same thing 2 times" in got["note"]


def test_a_cluster_with_no_named_mechanism_is_refused(env):
    seed()
    with pytest.raises(qa.QARefused) as e:
        qa.cluster("proj", "BUG-1", "integrations broken", ["QA-001"])
    assert "names a MECHANISM" in str(e.value)
    assert "is a title" in str(e.value), "it must show the difference, not just assert it"


def test_a_cluster_explaining_nothing_is_refused(env):
    with pytest.raises(qa.QARefused):
        qa.cluster("p", "BUG-2", "a" * 60, [])


# ------------------------------------------------- D4n: NOT RUN refuses coverage

def test_a_never_executed_case_REFUSES_the_claim(env):
    ids = seed()
    qa.record_result("proj", "r1", "QA-001", qa.PASS, cmd="pytest", exit_code=0)
    got = coverage.claim("proj", "r1", ids)
    assert got["granted"] is False and got["verdict"] == "REFUSED"
    blocked = {b["case"]: b for b in got["blocking"]}
    assert blocked["QA-002"]["verdict"] == qa.NOT_RUN
    assert "creation state, not a result" in blocked["QA-002"]["why"]


def test_a_blocked_case_refuses_the_claim_too(env):
    ids = seed()
    qa.record_result("proj", "r1", "QA-001", qa.PASS, cmd="c", exit_code=0)
    qa.record_result("proj", "r1", "QA-002", qa.PASS, auth=qa.OWNER, evidence="ok")
    qa.record_result("proj", "r1", "QA-003", qa.BLOCKED, evidence="needs a test account")
    got = coverage.claim("proj", "r1", ids)
    assert got["granted"] is False
    assert "needs a test account" in coverage.render(got)


def test_a_full_pass_grants_it(env):
    ids = seed()
    qa.record_result("proj", "r1", "QA-001", qa.PASS, cmd="c", exit_code=0)
    qa.record_result("proj", "r1", "QA-002", qa.PASS, auth=qa.OWNER, evidence="owner")
    qa.record_result("proj", "r1", "QA-003", qa.PASS, cmd="c", exit_code=0)
    got = coverage.claim("proj", "r1", ids)
    assert got["granted"] is True and "GRANTED" in coverage.render(got)


def test_the_word_REFUSED_is_printed_not_inferred(env):
    ids = seed()
    got = coverage.claim("proj", "r1", ids)
    text = coverage.render(got)
    assert text.startswith("COVERAGE: REFUSED"), \
        "a summary of numbers lets a tired reader infer the wrong verdict at 1am"


def test_owner_debt_is_counted_but_does_not_block(env):
    ids = seed()
    qa.record_result("proj", "r1", "QA-001", qa.PASS, cmd="c", exit_code=0)
    qa.record_result("proj", "r1", "QA-003", qa.PASS, cmd="c", exit_code=0)
    d = coverage.debt("proj", "r1", ids)
    assert d["owner_pending"] == ["QA-002"], "the HUMAN case is the debt"


# ------------------------------------------------- D5n: evidence

def test_present_evidence_is_hashed(env, tmp_path):
    f = tmp_path / "shot.png"
    f.write_bytes(b"not really a png")
    got = qa.attach_evidence("p", "QA-1", str(f))
    assert got["exists"] is True and len(got["sha256"]) == 64


def test_missing_evidence_is_declared_missing(env, tmp_path):
    got = qa.attach_evidence("p", "QA-1", str(tmp_path / "gone.png"))
    assert got["exists"] is False and got["missing"] is True
    assert "implies preservation" in got["note"]


# ------------------------------------------------- D38: the smoke contract

GOOD = """# SMOKE-REPORT — dual-secret rotation

**Date:** 2026-09-22
**Change:** stripe-server/server.js — verifyKeySignature accepts a PREVIOUS secret

## Method
Booted server.js locally on port 13351 with a throwaway licenses.json.

## Results
| Case | Expected | Actual | Pass |
|---|---|---|---|
| old key in store | valid | valid | yes |

## Review
Security review PASS — timing-safe on both secrets. No findings.

## Deploy safety
Phase 1 changes no secret, so an unset key stays unset and behaviour is identical to
current prod. Changing the secret is a separate phase-2 update and restart.
"""


def test_a_conforming_report_is_valid(env):
    got = smoke.check(GOOD)
    assert got["ok"] is True and "VALID" in smoke.render_verdict(got)


def test_a_report_without_deploy_safety_is_REFUSED(env):
    bad = GOOD.split("## Deploy safety")[0]
    got = smoke.check(bad)
    assert got["ok"] is False
    assert any("Deploy safety" in p for p in got["problems"])


def test_an_empty_deploy_safety_section_is_refused(env):
    bad = GOOD.split("## Deploy safety")[0] + "## Deploy safety\nnone\n"
    got = smoke.check(bad)
    assert got["ok"] is False
    assert any("blast radius of the deploy itself" in p for p in got["problems"])


def test_prose_results_are_refused(env):
    bad = GOOD.replace("| Case | Expected | Actual | Pass |", "it all worked fine")
    got = smoke.check(bad)
    assert any("cannot be read as pass or fail" in p for p in got["problems"])


def test_a_report_with_no_date_cannot_be_aged(env):
    bad = GOOD.replace("**Date:** 2026-09-22", "sometime last week")
    got = smoke.check(bad)
    assert any("cannot be aged" in p for p in got["problems"])


def test_the_template_itself_is_not_a_valid_report(env):
    got = smoke.check(smoke.TEMPLATE)
    assert got["ok"] is False, "an unfilled template must never pass as evidence"


# ------------------------------------------------- the record

def test_every_refusal_is_recorded(env, tmp_path):
    ids = seed()
    coverage.claim("proj", "r1", ids)
    r = [x for x in rows(tmp_path) if x["event"] == "qa_coverage"][0]
    assert r["verdict"] == "REFUSED" and r["blocking"] == 3


def test_the_root_cause_row_carries_the_mechanism(env, tmp_path):
    seed()
    qa.cluster("proj", "BUG-10", "isPro() compared tier to the literal 'pro' string",
               ["QA-001", "QA-003"])
    r = [x for x in rows(tmp_path) if x["event"] == "qa_root_cause"][0]
    assert r["explains_n"] == 2 and "isPro()" in r["mechanism"]
