#!/usr/bin/env python
"""M3 composed — a QA round that refuses a coverage claim it did not earn.

Driven from outside: three cases, one of them a HUMAN judgement; a round where one case
is never executed and one is blocked; a coverage claim that is REFUSED in those words,
naming each blocker; one root cause explaining two failures and proposing ONE backlog
node instead of two; owner debt counted without blocking; and a SMOKE-REPORT contract
that refuses both a missing Deploy-safety section and an unfilled template.

    python scripts/compose_m3.py     ->  exit 0 or M3 is not done

What it does NOT prove: generated views (D3n) — master/backlog/completed lists derived
from these records, with the owner worksheet left untouched — which is a rendering
concern on top of a record model that now exists.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

fails: list[str] = []


def ck(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def main() -> int:
    work = pathlib.Path(tempfile.mkdtemp(prefix="plexar-m3-"))
    try:
        os.environ["PLEXAR_AGENTS_STATE"] = str(work / "state" / "runs")
        os.environ["PLEXAR_AGENTS_LOG_DIR"] = str(work / "logs")
        from plexar_agents import coverage, qa, smoke

        P, RUN = "grimhollow", "round-17"

        print("\n[1] cases hold no verdict; a verdict is a dated result in a run")
        qa.add_case(P, "QA-001", "expired token rejected", "post expired", "401",
                    cls=qa.MACHINE, area="auth")
        qa.add_case(P, "QA-002", "settings feels instant", "open settings",
                    "no visible delay", cls=qa.HUMAN, area="ui")
        qa.add_case(P, "QA-003", "EU invoice keeps its tax line", "render EU invoice",
                    "tax line present", cls=qa.MACHINE, area="billing")
        qa.add_case(P, "QA-004", "integration button saves", "save a spotify button",
                    "no ValidationError", cls=qa.MACHINE, area="integrations")
        required = ["QA-001", "QA-002", "QA-003", "QA-004"]
        ck(all("verdict" not in c for c in qa.read(P)["cases"].values()),
           "no case carries a verdict")

        print("\n[2] an AUTO pass must carry a command and an exit code")
        try:
            qa.record_result(P, RUN, "QA-001", qa.PASS)
            ck(False, "an AUTO pass with no command should be refused")
        except qa.QARefused as e:
            ck("claim that it did" in str(e), "a pass with no command is REFUSED")
        try:
            qa.record_result(P, RUN, "QA-001", qa.PASS, cmd="pytest", exit_code=1)
            ck(False, "exit 1 is not a pass")
        except qa.QARefused as e:
            ck("not answerable to the implementation" in str(e),
               "a nonzero exit cannot be recorded as a pass")
        qa.record_result(P, RUN, "QA-001", qa.PASS, cmd="pytest -k expired", exit_code=0)
        ck(qa.latest_verdict(P, "QA-001")["verdict"] == qa.PASS, "a real pass records")

        print("\n[3] two failures, one blocked, one never executed")
        qa.record_result(P, RUN, "QA-003", qa.FAIL, cmd="pytest -k eu", exit_code=1)
        qa.record_result(P, RUN, "QA-004", qa.FAIL, cmd="pytest -k spotify", exit_code=1)
        ck(qa.latest_verdict(P, "QA-002") is None, "QA-002 was never executed at all")

        print("\n[4] the coverage claim is REFUSED — in that word")
        got = coverage.claim(P, RUN, required)
        text = coverage.render(got)
        print("\n".join("       " + l for l in text.splitlines()))
        ck(got["granted"] is False, "the claim is not granted")
        ck(text.startswith("COVERAGE: REFUSED"),
           "the verdict is PRINTED, not left to be inferred from numbers")
        blocked = {b["case"] for b in got["blocking"]}
        ck(blocked == {"QA-002", "QA-003", "QA-004"}, "every blocker is named")
        ck(any(b["verdict"] == qa.NOT_RUN for b in got["blocking"]),
           "a NOT RUN case blocks exactly like a failure")

        print("\n[5] one root cause explains both failures — ONE backlog node, not two")
        qa.open_issue(P, "GH-QA-101", "Pro gating rejects the starter tier", "P0",
                      area="integrations", case_ids=["QA-003", "QA-004"])
        qa.cluster(P, "BUG-10",
                   "isPro() compared tier to the literal string 'pro', so the 'starter' "
                   "tier returned false from every Pro gate on desktop and mobile alike",
                   ["QA-003", "QA-004"], issue_id="GH-QA-101")
        plan = qa.retests_for(P, "BUG-10")
        ck(plan["backlog_nodes"] == 1, "one defect, one backlog node")
        ck(len(plan["retest_cases"]) == 2, "carrying two retests")
        ck("fixes the same thing 2 times" in plan["note"],
           "and it says what filing two nodes would have cost")

        try:
            qa.cluster(P, "BUG-11", "integrations broken", ["QA-003"])
            ck(False, "a title is not a root cause")
        except qa.QARefused as e:
            ck("names a MECHANISM" in str(e), "a cluster with no mechanism is REFUSED")

        print("\n[6] severity is its own axis, on the defect")
        issue = qa.read(P)["issues"]["GH-QA-101"]
        ck(issue["severity"] == "P0", "P0 = launch blocker, independent of any verdict")
        ck(issue["status"] == "open" and issue["queue"] == "backlog"
           and issue["readiness"] == "not-ready", "three axes, moving independently")
        qa.set_lifecycle(P, "GH-QA-101", queue="fix")
        ck(qa.read(P)["issues"]["GH-QA-101"]["severity"] == "P0",
           "moving the queue does not touch severity")

        print("\n[7] owner debt is counted and does NOT block")
        d = coverage.debt(P, RUN, required)
        ck(d["owner_pending"] == ["QA-002"],
           "the HUMAN case is the debt — human availability is not the throughput limit")

        print("\n[8] the SMOKE-REPORT contract")
        good = (work / "SMOKE-REPORT.md")
        good.write_text(
            "# SMOKE-REPORT — starter tier\n\n**Date:** 2026-09-22\n"
            "**Change:** entitlementService.js — isPro() accepts any non-free tier\n\n"
            "## Method\nBooted locally with a throwaway licenses.json seeded starter+pro.\n\n"
            "## Results\n| Case | Expected | Actual | Pass |\n|---|---|---|---|\n"
            "| starter key | Pro granted | Pro granted | yes |\n\n"
            "## Review\nSecurity read PASS — no tier is widened beyond paid. No findings.\n\n"
            "## Deploy safety\nNo schema, secret or config change. Behaviour for the pro "
            "tier is byte-identical; only the starter branch is newly reachable.\n",
            encoding="utf-8")
        v = smoke.check(good.read_text(encoding="utf-8"))
        ck(v["ok"] is True, "a conforming report is VALID")

        missing = good.read_text(encoding="utf-8").split("## Deploy safety")[0]
        ck(smoke.check(missing)["ok"] is False,
           "a report with no Deploy-safety section is REFUSED")
        tmpl = smoke.check(smoke.TEMPLATE)
        ck(tmpl["ok"] is False,
           "and the UNFILLED TEMPLATE is refused — it has every section and no evidence")
        ck(any("no evidence" in p for p in tmpl["problems"]),
           "the refusal says why: a template that validates opens the deploy gate")

        print("\n[9] the runtime recorded all of it")
        rows = []
        for f in (work / "logs").glob("*.jsonl"):
            rows += [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines()
                     if l.strip()]
        ev = {r["event"] for r in rows}
        for e in ("qa_case", "qa_issue", "qa_result", "qa_root_cause",
                  "qa_coverage", "qa_debt", "smoke_check", "qa_lifecycle"):
            ck(e in ev, "a %s row exists" % e)
        cov = [r for r in rows if r["event"] == "qa_coverage"][0]
        ck(cov["verdict"] == "REFUSED" and cov["blocking"] == 3,
           "the refusal and its blocker count are in the ledger, not only on screen")
        rc = [r for r in rows if r["event"] == "qa_root_cause"][0]
        ck(rc["explains_n"] == 2, "the root-cause row says how many results it explains")

        print("\n[10] what this does NOT prove")
        print("  D3n generated views — master/backlog/completed derived from these")
        print("  records, with the hand-edited owner worksheet left untouched.")

    finally:
        shutil.rmtree(work, ignore_errors=True)

    print()
    if fails:
        print("COMPOSED M3: FAIL (%d)" % len(fails))
        for f in fails:
            print("  -", f)
        return 1
    print("COMPOSED M3: PASS — a round that refused the coverage it had not earned,")
    print("             and one root cause that became one backlog node.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
