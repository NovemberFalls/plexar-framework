"""`SMOKE-REPORT.md` is a gate artifact with a fixed shape.

A deploy should not happen without evidence. A pre-deploy hook can enforce that by refusing a
deploy command unless a `SMOKE-REPORT.md` exists; this module checks the report's shape:

    # SMOKE-REPORT — <what changed>
    **Date:** · **Change:** <files + the actual mechanism, not a summary>
    ## Method          how it was exercised, including the throwaway fixture
    ## Results         | Case | Expected | Actual | Pass |
    ## Review          security read, named, with findings or "no findings"
    ## Deploy safety   what this deploy does NOT change

**`Deploy safety` is the section nobody else writes and it is the best one.** It reasons
about the **blast radius of the deploy itself** (for example: "this deploy changes no
secret, so a key that is unset stays unset and behaviour is identical to production"),
separately from whether the code is correct. A green smoke test on a change that also
silently alters config is not a safe deploy. Only that section catches it, which is why a
report without one is refused.
"""
from __future__ import annotations

import re

from . import ledger

REQUIRED = ["Method", "Results", "Review", "Deploy safety"]
RESULTS_HEADER = re.compile(r"\|\s*Case\s*\|\s*Expected\s*\|\s*Actual\s*\|\s*Pass\s*\|",
                            re.I)


def check(text: str) -> dict:
    """Validate a report. Returns a verdict; `ok` is False unless every section is real."""
    problems = []
    lower_sections = {}
    for line in text.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            lower_sections[m.group(1).strip().lower()] = True

    for sec in REQUIRED:
        if sec.lower() not in lower_sections:
            problems.append("missing section: ## %s" % sec)

    if not re.search(r"^\*\*Date:\*\*", text, re.M):
        problems.append("no **Date:** — a report without one cannot be aged")
    if not re.search(r"^\*\*Change:\*\*", text, re.M):
        problems.append("no **Change:** — name the files and the mechanism, not a summary")
    if "results" in lower_sections and not RESULTS_HEADER.search(text):
        problems.append("Results has no | Case | Expected | Actual | Pass | table — prose "
                        "results cannot be read as pass or fail")

    # The section that earns its keep: it must say what does NOT change.
    ds = _section(text, "Deploy safety")
    if ds is not None and len(ds.strip()) < 40:
        problems.append("Deploy safety is present but empty — it must reason about what "
                        "this deploy does NOT change, which is the blast radius of the "
                        "deploy itself, separately from whether the code is right")

    # An UNFILLED TEMPLATE passes every structural check above: four sections, a Date
    # line, a Change line, a results header, a long-enough Deploy safety. Caught by
    # tests/test_qa.py. A template that validates is worse than no template — it opens
    # the deploy gate while carrying no evidence at all.
    placeholders = re.findall(r"<[^<>\n]{3,80}>", text)
    if placeholders:
        problems.append("unfilled placeholders remain (%s%s) — a template that validates "
                        "opens the deploy gate while carrying no evidence"
                        % (", ".join(placeholders[:3]),
                           "" if len(placeholders) <= 3 else ", +%d" % (len(placeholders) - 3)))
    if re.search(r"^\|(?:\s*\|)+\s*$", text, re.M):
        problems.append("the Results table has an empty row — a blank case is not a result")

    ok = not problems
    ledger.write("smoke_check", "qa", None, ok=ok, problems=problems,
                 sections=sorted(lower_sections), chars=len(text))
    return {"ok": ok, "problems": problems, "sections": sorted(lower_sections)}


def _section(text: str, name: str) -> str | None:
    m = re.search(r"^##\s+%s\s*$(.*?)(?=^##\s|\Z)" % re.escape(name), text,
                  re.M | re.S | re.I)
    return m.group(1) if m else None


def render_verdict(got: dict) -> str:
    if got["ok"]:
        return "SMOKE-REPORT: VALID — Method, Results, Review and Deploy safety all present"
    lines = ["SMOKE-REPORT: REFUSED (%d)" % len(got["problems"])]
    for p in got["problems"]:
        lines.append("  - " + p)
    return "\n".join(lines)


TEMPLATE = """# SMOKE-REPORT — <what changed>

**Date:** <YYYY-MM-DD>
**Change:** <files touched + the actual mechanism, not a summary>

## Method
<how it was exercised, including the throwaway fixture and where it ran>

## Results
| Case | Expected | Actual | Pass |
|---|---|---|---|
|  |  |  |  |

## Review
<security read, named reviewer, with findings or an explicit "no findings">

## Deploy safety
<what this deploy does NOT change. Config, secrets, schema, blobs. A green smoke test
on a change that also silently alters config is not a safe deploy.>
"""
