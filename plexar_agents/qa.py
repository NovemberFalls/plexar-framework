"""The QA record model — D19, D36, D37.

Distilled from QA loops that had each run for months in separate repos. Everything here was
already working somewhere; the new thing is that it is machine-readable across repos.

**A CASE is a reusable procedure and holds no verdict. An ISSUE is a defect's lifecycle.
A PASS/FAIL/BLOCKED/NOT RUN belongs to a dated RESULT inside a run.** That separation is
what survives a reopen: if the verdict lives on the issue, reopening overwrites the record
of when the thing last worked.

**Three axes, because they answer three questions** (`browser-rpg`):

    status      where the defect stands      null | open | completed | rejected
    queue       who is holding it now        backlog | fix | owner-review | closed
    readiness   can an owner retest today    ready | not-ready | accepted

`status: null` means **UNSELECTED, not unknown** — deferring records a reason and pauses
selection *without erasing history*. A boolean cannot express that.

**Severity is a fourth axis and lives on the DEFECT, not the test** (D36). `P0` is a launch
blocker; a defect can be High *because it is security*, whatever its test count. Pass/fail and how-much-it-matters are different facts.

**Root-cause clustering** (D37): one defect links the results it explains.
One real defect was *"the root cause of all integration failures from both desktop and mobile"* —
A QA pass treats every FAIL independently and would have filed ten backlog nodes for one
bug. A cluster with no named mechanism is refused: a bug title is not a root cause.
"""
from __future__ import annotations

import datetime
import hashlib
import os
import pathlib

from . import ledger, store

# ---- verdicts: what a RESULT can say. NOT_RUN is a creation state, never a result.
PASS, FAIL, BLOCKED, NOT_RUN = "PASS", "FAIL", "BLOCKED", "NOT_RUN"
VERDICTS = {PASS, FAIL, BLOCKED, NOT_RUN}

# ---- the three axes
STATUS = {None, "open", "completed", "rejected"}          # None == UNSELECTED
QUEUE = {"backlog", "fix", "owner-review", "closed"}
READINESS = {"ready", "not-ready", "accepted"}

# ---- severity, on the defect
SEVERITY = {"P0", "P1", "P2"}
SEVERITY_MEANS = {"P0": "launch blocker", "P1": "first-week fix", "P2": "backlog"}

# ---- who may decide a verdict
MACHINE, HUMAN = "MACHINE", "HUMAN"      # intrinsic: can a script decide it?
AUTO, OWNER = "AUTO", "OWNER"            # granted: may an agent record the pass?


class QARefused(RuntimeError):
    pass


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _key(project: str) -> str:
    return "qa-%s" % "".join(c if c.isalnum() or c in "-_." else "_" for c in project)[:80]


def _update(project: str, fn):
    return store.update(_key(project), fn)


def read(project: str) -> dict:
    s = store.read(_key(project))
    return {"cases": s.get("cases", {}), "issues": s.get("issues", {}),
            "results": s.get("results", []), "defects": s.get("defects", {})}


# --------------------------------------------------------------- cases

def add_case(project: str, case_id: str, title: str, steps: str, expected: str,
             cls: str = MACHINE, area: str = "general") -> dict:
    """A reusable procedure. It has no verdict, now or ever."""
    if cls not in (MACHINE, HUMAN):
        raise QARefused("class must be MACHINE or HUMAN, not %r" % cls)
    case = {"id": case_id, "title": title, "steps": steps, "expected": expected,
            "cls": cls, "area": area, "updated": _now()}

    def fn(s):
        s.setdefault("cases", {})[case_id] = case
        return s

    _update(project, fn)
    ledger.write("qa_case", "qa", case_id, project=project, cls=cls, area=area, title=title)
    return case


# --------------------------------------------------------------- issues

def open_issue(project: str, issue_id: str, title: str, severity: str,
               area: str = "general", case_ids: list[str] | None = None) -> dict:
    """A defect's lifecycle. Severity is required and lives HERE, not on a test."""
    if severity not in SEVERITY:
        raise QARefused(
            "severity must be one of %s (%s), not %r — how much it matters is a "
            "different fact from whether a test passed (D36)"
            % (sorted(SEVERITY), ", ".join("%s=%s" % kv for kv in SEVERITY_MEANS.items()),
               severity))
    issue = {"id": issue_id, "title": title, "severity": severity, "area": area,
             "status": "open", "queue": "backlog", "readiness": "not-ready",
             "case_ids": case_ids or [], "root_cause": None,
             "history": [{"at": _now(), "what": "opened", "severity": severity}]}

    def fn(s):
        s.setdefault("issues", {})[issue_id] = issue
        return s

    _update(project, fn)
    ledger.write("qa_issue", "qa", issue_id, project=project, severity=severity,
                 severity_means=SEVERITY_MEANS[severity], area=area, title=title)
    return issue


def set_lifecycle(project: str, issue_id: str, status=..., queue=..., readiness=...,
                  why: str | None = None) -> dict:
    """Move an issue on any of the three axes. Deferring needs a reason."""
    cur = read(project)["issues"].get(issue_id)
    if cur is None:
        raise KeyError(issue_id)
    new = dict(cur)
    if status is not ...:
        if status not in STATUS:
            raise QARefused("status must be one of %s (None == UNSELECTED)" % sorted(
                x for x in STATUS if x))
        if status is None and not why:
            raise QARefused(
                "setting status to null pauses selection — record WHY. 'we decided not "
                "to test this yet' and 'nobody looked' must not be indistinguishable")
        new["status"] = status
    if queue is not ...:
        if queue not in QUEUE:
            raise QARefused("queue must be one of %s" % sorted(QUEUE))
        new["queue"] = queue
    if readiness is not ...:
        if readiness not in READINESS:
            raise QARefused("readiness must be one of %s" % sorted(READINESS))
        new["readiness"] = readiness
    if status == "rejected":
        if not why:
            raise QARefused(
                "a rejection is a decision — it carries an owner, a date and a reason. "
                "A QA library that can only record fixes cannot show you the decisions")
        new["rejected"] = {"at": _now(), "why": why}
    new["history"] = cur["history"] + [{"at": _now(), "status": new["status"],
                                        "queue": new["queue"],
                                        "readiness": new["readiness"], "why": why}]

    def fn(s):
        s["issues"][issue_id] = new
        return s

    _update(project, fn)
    ledger.write("qa_lifecycle", "qa", issue_id, project=project, status=new["status"],
                 queue=new["queue"], readiness=new["readiness"], why=why)
    return new


# --------------------------------------------------------------- results

def record_result(project: str, run: str, case_id: str, verdict: str,
                  evidence: str | None = None, cmd: str | None = None,
                  exit_code: int | None = None, by: str = "qa",
                  auth: str = AUTO, scope: str | None = None,
                  issue_id: str | None = None) -> dict:
    """A dated verdict inside a run. THIS is where PASS/FAIL lives — never on an issue.

    An AUTO-recorded pass must carry a command AND an exit code. That requirement is
    structural, not stylistic: an agent cannot produce that shape for a test nobody ran
    without writing a command that was never executed — a lie, reviewable as one, rather
    than an ambiguity.
    """
    if verdict not in VERDICTS:
        raise QARefused("verdict must be one of %s" % sorted(VERDICTS))
    if verdict == PASS and auth == AUTO and (not cmd or exit_code is None):
        raise QARefused(
            "an AUTO pass needs a command and an exit code. Without them there is no "
            "difference between a test that ran and a claim that it did")
    if verdict == PASS and auth == AUTO and exit_code != 0:
        raise QARefused("exit %s is not a pass — the gate is not answerable to the "
                        "implementation" % exit_code)

    result = {"run": run, "case_id": case_id, "verdict": verdict, "at": _now(),
              "by": by, "auth": auth, "evidence": evidence, "cmd": cmd,
              "exit_code": exit_code, "scope": scope, "issue_id": issue_id}

    def fn(s):
        s.setdefault("results", []).append(result)
        return s

    _update(project, fn)
    ledger.write("qa_result", "qa", case_id, project=project, qa_run=run,
                 verdict=verdict, auth=auth, cmd=cmd, node_check_exit=exit_code,
                 scope=scope, issue_id=issue_id)
    return result


def results_for(project: str, run: str) -> list[dict]:
    return [r for r in read(project)["results"] if r["run"] == run]


def latest_verdict(project: str, case_id: str) -> dict | None:
    rs = [r for r in read(project)["results"] if r["case_id"] == case_id]
    return rs[-1] if rs else None


# --------------------------------------------------------------- root cause (D37)

def cluster(project: str, defect_id: str, mechanism: str, case_ids: list[str],
            issue_id: str | None = None) -> dict:
    """One defect explains N failing results.

    A real example: *"isPro() only matched tier 'pro' — rejected 'starter'… root cause
    of all integration failures from both desktop and mobile."* Without this, ten failing
    cases become ten backlog nodes and you fix one thing ten times.

    **A cluster with no named mechanism is refused.** A bug title is not a root cause.
    """
    mechanism = (mechanism or "").strip()
    if len(mechanism) < 30:
        raise QARefused(
            "a root cause names a MECHANISM, not a symptom. 'integrations broken' is a "
            "title; 'isPro() compared tier to the literal \"pro\" so the \"starter\" "
            "tier failed every Pro gate' is a cause")
    if not case_ids:
        raise QARefused("a cluster explaining no results is not a cluster")

    d = {"id": defect_id, "mechanism": mechanism, "explains": list(case_ids),
         "issue_id": issue_id, "at": _now()}

    def fn(s):
        s.setdefault("defects", {})[defect_id] = d
        return s

    _update(project, fn)
    ledger.write("qa_root_cause", "qa", defect_id, project=project,
                 explains=list(case_ids), explains_n=len(case_ids),
                 issue_id=issue_id, mechanism=mechanism)
    return d


def retests_for(project: str, defect_id: str) -> dict:
    """Closing one root cause proposes ONE backlog node carrying N retests, not N nodes."""
    d = read(project)["defects"].get(defect_id)
    if d is None:
        raise KeyError(defect_id)
    return {"defect": defect_id, "mechanism": d["mechanism"],
            "retest_cases": d["explains"], "backlog_nodes": 1,
            "note": "one node, %d retests — filing one per failing case fixes the same "
                    "thing %d times" % (len(d["explains"]), len(d["explains"]))}


# --------------------------------------------------------------- evidence (D5n)

def attach_evidence(project: str, case_id: str, path: str) -> dict:
    """Durable or declared missing. A dead link that looks alive is worse than a gap."""
    p = pathlib.Path(path)
    try:
        data = p.read_bytes()
        rec = {"path": str(p), "exists": True, "bytes": len(data),
               "sha256": hashlib.sha256(data).hexdigest(), "at": _now()}
    except OSError:
        rec = {"path": str(p), "exists": False, "bytes": 0, "sha256": None,
               "at": _now(), "missing": True,
               "note": "durable evidence MISSING — recorded as missing rather than left "
                       "as a path that implies preservation"}
    ledger.write("qa_evidence", "qa", case_id, project=project, **rec)
    return rec
