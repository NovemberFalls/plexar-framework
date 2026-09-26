"""One monologue into many held tasks — B0bn.

**The runtime does not split.** Splitting a twenty-minute ramble into coherent briefs is
judgement, and D2 says the reasoning is the part deliberately left free. A model does it.

**What the runtime does is prove nothing was silently dropped.**

That is the failure worth engineering against. You say twenty things, eight become tasks,
and nobody notices the other twelve — the dump *looks* processed, the pool looks full, and
the missing work is invisible because there is nothing to compare against. It is the
built-but-never-wired failure moved upstream: a silence that reads as a success.

So every candidate task cites the **source span** it came from, and `coverage()` computes
what is left over. Uncovered text is reported, never swallowed, and a splitter that
declines to use a span must say so explicitly — `not_a_task` is a decision, absence is not.

    split = model_splits(monologue)            # a model, not this module
    report = sweep.review(monologue, split)    # what is covered, what is not
    if report["ok"]: sweep.commit(bucket, report)   # only then do tasks exist
"""
from __future__ import annotations

from . import ledger, tasks

MIN_TASK_CHARS = 40          # a fragment is not a brief
COVERAGE_FLOOR = 0.85        # UNMEASURED — see review()


class SweepRefused(RuntimeError):
    pass


def _span_ok(span, n: int) -> bool:
    return (isinstance(span, (list, tuple)) and len(span) == 2
            and isinstance(span[0], int) and isinstance(span[1], int)
            and 0 <= span[0] < span[1] <= n)


def review(monologue: str, candidates: list[dict], floor: float = COVERAGE_FLOOR,
           run_id: str = "pool") -> dict:
    """Check a proposed split without creating anything.

    `candidates` is `[{prompt, span:[start,end], kind?}]`. `kind="not_a_task"` marks a
    span the splitter deliberately declined — an explicit decision, which is the only
    thing that distinguishes "I read this and it is not work" from "I missed it".

    Returns a report. **It does not raise and it does not commit** — the grouping is
    shown before anything is held (D24), so the caller (or a human) can look first.
    """
    n = len(monologue)
    problems, covered = [], [False] * n
    tasks_out, declined = [], []

    for i, c in enumerate(candidates):
        span, prompt = c.get("span"), (c.get("prompt") or "").strip()
        kind = c.get("kind", "task")
        if not _span_ok(span, n):
            problems.append("candidate %d has no usable span into the source" % i)
            continue
        for k in range(span[0], span[1]):
            covered[k] = True
        if kind == "not_a_task":
            declined.append({"span": list(span), "why": c.get("why") or "(no reason given)",
                             "text": monologue[span[0]:span[1]][:120]})
            if not c.get("why"):
                problems.append("candidate %d declines a span without saying why" % i)
            continue
        if len(prompt) < MIN_TASK_CHARS:
            problems.append("candidate %d is %d chars — a fragment, not a brief"
                            % (i, len(prompt)))
            continue
        tasks_out.append({"prompt": prompt, "span": list(span)})

    # Uncovered runs, with their text, so a human can see exactly what fell out.
    gaps, start = [], None
    for k in range(n):
        if not covered[k] and start is None:
            start = k
        elif covered[k] and start is not None:
            gaps.append((start, k))
            start = None
    if start is not None:
        gaps.append((start, n))
    gaps = [g for g in gaps if monologue[g[0]:g[1]].strip()]

    ratio = (sum(covered) / n) if n else 1.0
    report = {
        "tasks": tasks_out,
        "declined": declined,
        "coverage": round(ratio, 4),
        # UNMEASURED. The floor that belongs here is whatever correlates with a caller
        # saying "yes, that is all of it" — nobody has run that experiment.
        "floor": floor, "floor_source": "default (UNMEASURED)",
        "uncovered": [{"span": [a, b], "text": monologue[a:b].strip()[:200]} for a, b in gaps],
        "problems": problems,
        "ok": not problems and ratio >= floor and bool(tasks_out),
    }
    ledger.write("sweep_review", run_id, None,
                 chars=n, candidates=len(candidates), tasks=len(tasks_out),
                 declined=len(declined), coverage=report["coverage"], floor=floor,
                 floor_source=report["floor_source"],
                 uncovered_runs=len(gaps), problems=problems, ok=report["ok"])
    return report


def commit(bucket: str, report: dict, source: str = "monologue",
           session_id: str | None = None, hold: bool = True) -> list[dict]:
    """Create the tasks a reviewed split proposed. Refuses a report that is not ok.

    Held by default (D24): a swept monologue produces work that is ready and deliberately
    not running.
    """
    if not report.get("ok"):
        ledger.write("sweep_refused", "pool", None, bucket=bucket,
                     coverage=report.get("coverage"), problems=report.get("problems"),
                     uncovered_runs=len(report.get("uncovered") or []))
        raise SweepRefused(
            "this split is not committable: coverage %.2f against a floor of %.2f, %d "
            "uncovered run(s), %d problem(s). Nothing was created. Fix the split or "
            "declare the leftover spans not_a_task — silence is not a decision."
            % (report.get("coverage", 0), report.get("floor", 0),
               len(report.get("uncovered") or []), len(report.get("problems") or [])))

    made = []
    for t in report["tasks"]:
        task = tasks.create(bucket, t["prompt"], source=source, session_id=session_id)
        made.append(task)
    if hold:
        for t in made:
            tasks.hold(bucket, t["id"])

    ledger.write("sweep_commit", "pool", None, bucket=bucket, source=source,
                 created=len(made), coverage=report["coverage"],
                 declined=len(report.get("declined") or []), held=hold)
    return made
