#!/usr/bin/env python
"""A10n — E3 and E4 from outside, across real processes.

M1 proved the runtime denies a write and records what happened. It deliberately did not
prove the budget or the ladder, because both **count across spawns** and a spawn is a
separate OS process.

So this script does not import and call functions in one interpreter. It launches real
subprocesses, exactly as a swarm does, and asserts that state survived between them.
Threads would share an interpreter and pass against a counter that is not process-safe.

    python scripts/compose_m1b.py     ->  exit 0 or M1b is not done

What it does NOT prove: prevention on the Bash surface (D33, still detection only), and
per-node token attribution (needs the agentId join, still unwired).
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import textwrap

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

fails: list[str] = []


def ck(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def spawn(env: dict, body: str) -> subprocess.CompletedProcess:
    """One 'worker' — a genuinely separate process, like a real dispatch."""
    code = "import sys; sys.path.insert(0, %r)\n" % str(ROOT) + textwrap.dedent(body)
    return subprocess.run([sys.executable, "-c", code], env=env,
                          capture_output=True, text=True, timeout=60)


def main() -> int:
    work = pathlib.Path(tempfile.mkdtemp(prefix="plexar-m1b-"))
    try:
        env = {**os.environ,
               "PLEXAR_AGENTS_STATE": str(work / "state" / "runs"),
               "PLEXAR_AGENTS_LOG_DIR": str(work / "logs")}
        os.environ.update({k: env[k] for k in
                           ("PLEXAR_AGENTS_STATE", "PLEXAR_AGENTS_LOG_DIR")})
        from plexar_agents import budget, collisions, ladder   # after the env is set

        print("\n[1] E4 — the ladder counts across SPAWNS, not within one process")
        for i in (1, 2, 3):
            p = spawn(env, """
                from plexar_agents import ladder
                print(ladder.attempt("M1B", "N01", lane="haiku")["attempt"])
            """)
            ck(p.returncode == 0 and p.stdout.strip() == str(i),
               "spawn %d saw attempt %d — a fresh process knew what the last one did" % (i, i))

        p = spawn(env, """
            from plexar_agents import ladder
            try:
                ladder.attempt("M1B", "N01", lane="haiku")
                print("CONTINUED")
            except ladder.LadderExhausted as e:
                print("RAISED:" + str(e)[:60])
        """)
        ck(p.stdout.startswith("RAISED"), "the FOURTH spawn raised rather than continuing")
        ck("exhausted" in p.stdout, "and said so in words the orchestrator must obey")
        ck(ladder.attempts("M1B", "N01") == 4, "every attempt was counted, including the refused one")

        print("\n[2] E4 — the lane goes up one tier on a retry")
        ck(ladder.escalate("haiku") == "sonnet", "haiku escalates to sonnet")
        ck(ladder.escalate("opus") == "opus", "opus has nowhere above it")

        print("\n[3] E3 — the budget ceiling stops the run, loudly")
        budget.set_ceiling("M1B", 1.00)
        for _ in range(3):
            spawn(env, """
                from plexar_agents import budget
                budget.charge("M1B", 0.30, source="measured")
            """)
        ck(abs(budget.state("M1B")["spent"] - 0.90) < 1e-6,
           "three separate processes accumulated 0.90 against a 1.00 ceiling")

        p = spawn(env, """
            from plexar_agents import budget
            try:
                budget.check("M1B", projected=0.50)
                print("ALLOWED")
            except budget.BudgetExceeded as e:
                print("REFUSED:" + str(e)[:200])
        """)
        ck(p.stdout.startswith("REFUSED"), "a spawn that would breach the ceiling is REFUSED")
        ck("The run STOPS" in p.stdout, "it stops rather than degrading")
        ck("cheaper models" in p.stdout, "and refuses silent degradation by name")

        p = spawn(env, """
            from plexar_agents import budget
            budget.check("M1B", projected=0.05); print("ALLOWED")
        """)
        ck(p.stdout.strip() == "ALLOWED", "a spawn inside the ceiling still proceeds")

        print("\n[4] D7/D8 — a collision negotiates, bounded")
        got = collisions.open_("M1B", "lib/session.py", claimant="N04", holder="N07")
        note = pathlib.Path(got["note"])
        ck(note.exists(), "a negotiation file was created")
        txt = note.read_text(encoding="utf-8")
        ck("## N04" in txt and "## N07" in txt, "both parties have a heading")
        ck("Neither of you may write it yet" in txt, "neither party writes while it is open")
        for _ in range(2):
            collisions.open_("M1B", "lib/session.py", "N04", "N07")
        try:
            collisions.open_("M1B", "lib/session.py", "N04", "N07")
            ck(False, "round 4 should have escalated")
        except collisions.NegotiationUnresolved as e:
            ck("Escalating to parent" in str(e), "round 4 escalates one tier up, not forever")

        print("\n[5] D9 — agreement is recorded and is NOT sufficient")
        for consensus, gate, why in [(True, 1, "a failing gate"),
                                     (True, None, "a gate that never ran"),
                                     (False, 0, "no agreement")]:
            try:
                collisions.resolve("M1B", "zz.py", consensus=consensus, gate_exit=gate)
                ck(False, "%s should not have landed" % why)
            except collisions.ConsensusIsNotAGate:
                ck(True, "%s does not land" % why)
        collisions.resolve("M1B", "zz.py", consensus=True, gate_exit=0, resolution="agreed")
        ck(collisions.state("M1B", "zz.py")["resolved"] is True,
           "consensus AND a zero gate exit together do land it")

        print("\n[6] the runtime recorded all of it")
        rows = []
        for f in (work / "logs").glob("*.jsonl"):
            rows += [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
        ev = {r["event"] for r in rows}
        for e in ("ladder_attempt", "ladder_exhausted", "budget_charge",
                  "budget_refused", "collision", "collision_resolved"):
            ck(e in ev, "an %s row exists" % e)
        ck(any(r.get("counted_by") == "runtime" for r in rows if r["event"] == "ladder_attempt"),
           "the ladder row says the RUNTIME counted it, not a worker")
        ck(any(r.get("source") == "measured" for r in rows if r["event"] == "budget_charge"),
           "spend carries how it was known")
        refused = [r for r in rows if r["event"] == "collision_resolved" and not r["landed"]]
        ck(len(refused) == 3, "all three refusals were recorded with a reason")

        print("\n[7] what this does NOT prove")
        print("  Prevention on the Bash surface (D33) — still detection only.")
        print("  Per-node token attribution — the agentId join is unwired.")
        print("  No daemon was running: the file store held everything above.")

    finally:
        shutil.rmtree(work, ignore_errors=True)

    print()
    if fails:
        print("COMPOSED M1b: FAIL (%d)" % len(fails))
        for f in fails:
            print("  -", f)
        return 1
    print("COMPOSED M1b: PASS — the ladder counted across processes, the budget stopped")
    print("                     the run, and agreement alone did not land a change.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
