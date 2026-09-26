"""D4n — a required case that is failing, blocked or unrun REFUSES a coverage claim.

Not "is noted alongside one". **Refuses.**

`NOT_RUN` is the state a case is created in. It is not a result, and it must never be
counted toward green. This is `node_check_exit: null is not 0` (§10), arrived at
independently in `browser-rpg` by a different route — which is the strongest kind of
evidence a rule gets.

The two failures it prevents are the same failure:

* a round that covered a third of the suite **reads exactly like** one that covered all
  of it, unless the unrun items are named;
* a checkpoint claimed over unrun cases is a green light nobody earned, and the next
  milestone lands on top of it.

So the verdict is the word **REFUSED**, printed. A summary that reports the numbers and
lets the reader infer the verdict is how the inference gets made optimistically at 1am.
"""
from __future__ import annotations

from . import ledger, qa


def claim(project: str, run: str, required: list[str]) -> dict:
    """Ask for a full-coverage claim over `required` cases. Usually the answer is no.

    Returns a report whose `granted` is False unless every required case has a PASS in
    this run. `blocking` names each case and why, so the refusal is actionable rather
    than a number.
    """
    results = {r["case_id"]: r for r in qa.results_for(project, run)}
    blocking, passed = [], []

    for cid in required:
        r = results.get(cid)
        if r is None:
            blocking.append({"case": cid, "verdict": qa.NOT_RUN,
                             "why": "never executed in this run — a creation state, "
                                    "not a result"})
        elif r["verdict"] == qa.PASS:
            passed.append(cid)
        else:
            blocking.append({"case": cid, "verdict": r["verdict"],
                             "why": {"FAIL": "failed",
                                     "BLOCKED": "blocked — %s" % (r.get("evidence")
                                                                  or "no reason recorded"),
                                     "NOT_RUN": "recorded as NOT RUN"}.get(
                                         r["verdict"], r["verdict"])})

    granted = not blocking
    report = {
        "project": project, "run": run,
        "required": len(required), "passed": len(passed),
        "blocking": blocking, "granted": granted,
        "verdict": "GRANTED" if granted else "REFUSED",
    }
    ledger.write("qa_coverage", "qa", None, project=project, qa_run=run,
                 required=len(required), passed=len(passed),
                 blocking=len(blocking), granted=granted,
                 verdict=report["verdict"],
                 blocking_cases=[b["case"] for b in blocking])
    return report


def render(report: dict) -> str:
    """The words, because a number lets a tired reader infer the wrong verdict."""
    if report["granted"]:
        return ("COVERAGE: GRANTED — %d of %d required cases PASS in run %s"
                % (report["passed"], report["required"], report["run"]))
    lines = ["COVERAGE: REFUSED — %d required case(s) are not PASS"
             % len(report["blocking"])]
    for b in report["blocking"]:
        lines.append("  %-10s %-8s %s" % (b["case"], b["verdict"], b["why"]))
    return "\n".join(lines)


def debt(project: str, run: str, required: list[str]) -> dict:
    """OWNER items outstanding. They do NOT block the next milestone — human
    availability is not the project's throughput limit — but the price is that the debt
    is restated every round, with its age, so it cannot quietly accumulate."""
    results = {r["case_id"]: r for r in qa.results_for(project, run)}
    cases = qa.read(project)["cases"]
    owed = [cid for cid in required
            if cases.get(cid, {}).get("cls") == qa.HUMAN
            and (cid not in results or results[cid]["verdict"] == qa.NOT_RUN)]
    ledger.write("qa_debt", "qa", None, project=project, qa_run=run,
                 owner_pending=len(owed), cases=owed)
    return {"owner_pending": owed, "count": len(owed)}
