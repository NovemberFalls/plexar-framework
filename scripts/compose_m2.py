#!/usr/bin/env python
"""M2 composed — a monologue is swept, tasks sit held, an agent takes a batch, the drain bounds it.

This is the work pool end to end, driven from outside: a monologue swept into tasks —
with a split that drops a third of it REFUSED and the dropped text shown — then ten held
tasks from two conversations, a selection refused for being over the cap and again for
having no reason, a legal selection, and a drain that starts only as many as the
concurrency cap allows and leaves the rest waiting rather than failing.

    python scripts/compose_m2.py     ->  exit 0 or M2's core is not done

What it does NOT prove: submission over HTTP (B2n), lease-off by a daemon (B3n), and the
push approval (B5n). Those are the network half of M2 and none of it exists.
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
    work = pathlib.Path(tempfile.mkdtemp(prefix="plexar-m2-"))
    try:
        os.environ["PLEXAR_AGENTS_STATE"] = str(work / "state" / "runs")
        os.environ["PLEXAR_AGENTS_LOG_DIR"] = str(work / "logs")
        from plexar_agents import pool, sweep, tasks
        B = "plexar-chat"

        print("\n[0] a monologue is swept — and a split that drops text is REFUSED")
        parts = ["Fix the login redirect loop on mobile, it bounces twice. ",
                 "Make the invoice PDF stop dropping the EU tax line. ",
                 "The settings page needs a dark mode that persists across reloads. "]
        mono = "".join(parts)
        full, at = [], 0
        for s in parts:
            full.append({"prompt": s.strip(), "span": [at, at + len(s)]})
            at += len(s)

        bad = sweep.review(mono, full[:2])
        ck(bad["ok"] is False, "a split covering two of three ideas is not committable")
        ck("dark mode" in " ".join(u["text"] for u in bad["uncovered"]),
           "and the DROPPED TEXT is shown, not merely counted")
        try:
            sweep.commit(B, bad)
            ck(False, "committing a refused split should raise")
        except sweep.SweepRefused as e:
            ck("Nothing was created" in str(e), "a refused sweep creates nothing")
        ck(tasks.counts(B)["held"] == 0, "the pool is still empty after a refusal")

        good = sweep.review(mono, full)
        ck(good["coverage"] == 1.0 and good["ok"], "a full split covers the monologue")
        ck(tasks.counts(B)["drafted"] == 0,
           "review creates nothing — the grouping is shown BEFORE anything is held")
        sweep.commit(B, good, source="conv-a")
        ck(tasks.counts(B)["held"] == 3, "committing holds them")

        print("\n[1] a second conversation adds more, and NOTHING runs")
        made = []
        for i in range(7):
            made.append(tasks.create(
                B, "conv-b task %d: a prompt long enough to be a real brief" % i,
                source="conv-b"))
        ck(tasks.counts(B)["drafted"] == 7, "seven more drafted from a second conversation")
        ck(tasks.counts(B)["running"] == 0, "none of them is running")

        tasks.hold_all(B)
        c = tasks.counts(B)
        ck(c["held"] == 10 and c["drafted"] == 0,
           "all ten are HELD — three swept, seven direct — a real state, not an absence")
        ck(c["running"] == 0, "holding is not starting")

        print("\n[2] the agent picks its own batch — and the cap is not advisory")
        # approval="auto": this script proves the pool; approval is proved by compose_m2e.
        pool.set_policy(B, cap=4, concurrency=2, approval="auto")
        ids = [t["id"] for t in tasks.in_state(B, tasks.HELD)]

        try:
            pool.select(B, ids, "all ten, they are all tasks", "W1")
            ck(False, "a ten-task selection against a cap of four should be refused")
        except pool.SelectionRefused as e:
            ck("over_cap" in str(e), "an over-cap selection is REFUSED")
            ck("not trimmed for you" in str(e), "and is not silently trimmed")
        ck(tasks.counts(B)["held"] == 10, "nothing moved on a refusal")

        try:
            pool.select(B, ids[:3], "related", "W1")
            ck(False, "a one-word reason should be refused")
        except pool.SelectionRefused as e:
            ck("no_reason" in str(e), "a selection with no stated grouping is REFUSED")

        sel = pool.select(B, ids[:4],
                          "all four are conv-a label changes touching the same module",
                          "W1")
        ck(sel["size"] == 4, "a legal selection of four is taken")
        ck(tasks.counts(B)["selected"] == 4 and tasks.counts(B)["held"] == 6,
           "exactly four moved; the rest stay held")

        print("\n[3] the drain is bounded — a released backlog is not a fan-out licence")
        started = []

        def runner(t):
            started.append(t["id"])
            live = len(pool.running(B))
            ck(live <= pool.policy(B)["concurrency"],
               "at most %d running while %s executes" % (pool.policy(B)["concurrency"], t["id"]))
            return True, 0

        got = pool.drain(B, sel, runner)
        ck(len(got["ran"]) + len(got["throttled"]) == 4, "every selected task was accounted for")
        ck(got["counts"]["done"] == len(got["ran"]), "what ran with a zero gate exit is done")
        for tid in got["throttled"]:
            ck(tasks.get(B, tid)["state"] == tasks.SELECTED,
               "%s waits as SELECTED rather than failing" % tid)

        print("\n[4] the gate exit decides, not the runner's opinion")
        ids2 = [t["id"] for t in tasks.in_state(B, tasks.HELD)][:2]
        sel2 = pool.select(B, ids2, "two tasks whose runner will claim success wrongly", "W2")
        got2 = pool.drain(B, sel2, runner=lambda t: (True, 1))
        ck(all(r["ok"] for r in got2["ran"]), "the runner reported ok")
        ck(all(tasks.get(B, r["task"])["state"] == tasks.FAILED for r in got2["ran"]),
           "and every one of them is FAILED, because the gate said 1")

        print("\n[5] a task cannot skip to done")
        spare = [t["id"] for t in tasks.in_state(B, tasks.HELD)][0]
        try:
            tasks.transition(B, spare, tasks.DONE)
            ck(False, "held -> done should be refused")
        except tasks.IllegalTransition as e:
            ck("only route to done is" in str(e), "held -> done is refused, in words")

        print("\n[5b] durability — a dead owner does not hold a slot forever")
        import subprocess
        import textwrap
        import time as _time
        spare2 = [x["id"] for x in tasks.in_state(B, tasks.HELD)][:1]
        pool.select(B, spare2, "one task started by a process that will then die", "W3")
        code = textwrap.dedent("""
            import sys; sys.path.insert(0, %r)
            from plexar_agents import pool
            pool.start(%r, %r, "R-dead")
        """) % (str(ROOT), B, spare2[0])
        r = subprocess.run([sys.executable, "-c", code], env=dict(os.environ),
                           capture_output=True, text=True)
        ck(r.returncode == 0, "a separate process claimed a task")
        ck(tasks.get(B, spare2[0])["state"] == tasks.RUNNING,
           "then exited, leaving it RUNNING with nothing behind it")
        _time.sleep(0.05)
        rec = pool.recover(B, stale_after_s=0.01)
        ck(rec["reaped"] == spare2, "recover() reaped the orphan")
        ck(tasks.get(B, spare2[0])["state"] == tasks.HELD,
           "and put it back in the pool, where it can be selected again")

        print("\n[6] the runtime recorded it, including what it refused")
        rows = []
        for f in (work / "logs").glob("*.jsonl"):
            rows += [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
        ev = {r["event"] for r in rows}
        for e in ("task_created", "task_transition", "task_transition_refused",
                  "selection", "selection_refused", "drain"):
            ck(e in ev, "an %s row exists" % e)
        s = [r for r in rows if r["event"] == "selection"][0]
        ck("selection_size" in s and "cap_source" in s,
           "the selection row carries its size and where the cap came from")
        ck("UNMEASURED" in s["cap_source"],
           "the cap is labelled a guess — the bend in first-try rate has never been measured")
        ck(any(r["event"] == "drain_throttled" for r in rows) or not got["throttled"],
           "a throttle, if it happened, was recorded")

        print("\n[7] what this does NOT prove")
        print("  B2n submission over HTTP,")
        print("  B3n lease-off by a daemon, B5n the push approval. None exists yet.")

    finally:
        shutil.rmtree(work, ignore_errors=True)

    print()
    if fails:
        print("COMPOSED M2 (core): FAIL (%d)" % len(fails))
        for f in fails:
            print("  -", f)
        return 1
    print("COMPOSED M2 (core): PASS — tasks held, a batch chosen with a reason under a")
    print("                    cap, and a drain that waits rather than fanning out.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
