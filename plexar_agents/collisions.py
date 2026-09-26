"""The collision protocol — D7, D8, D9.

Two nodes reach for the same file. The obvious move is a hard lease: the second writer
fails. That is the cage again at file granularity — it makes an agent fail instead of
making two agents collaborate, and it throws the signal away. **Two nodes reaching for
one file usually means the graph was drawn wrong** — two nodes that should have been
one, or a missing `deps` edge. A lock hides that; a logged, resolved collision is a row
that improves the planner.

So:

  D7  a collision OPENS A NEGOTIATION. It does not fail the write.
  D8  bounded at 3 rounds, then one tier up, then the orchestrator. §4.8's ladder,
      applied to consensus instead of to retries — the cheapest model that can resolve it.
  D9  agreement is RECORDED but is NOT SUFFICIENT. The gate script still decides.

D9 is the load-bearing one. Two models can converge on an agreed wrong answer, or
ping-pong politely forever burning budget. "Happy solve on both sides" is a judgement,
and §4.7 is explicit that a model's opinion is never the gate.
"""
from __future__ import annotations

import os
import pathlib

from . import ladder, ledger, store

DEFAULT_ROUNDS = 3
TIERS = ladder.LANES          # one vocabulary for escalation, not two


class NegotiationUnresolved(RuntimeError):
    def __init__(self, key: str, rounds: int, escalate_to: str):
        super().__init__(
            "collision on %s is unresolved after %d rounds. Escalating to %s — the "
            "cheapest party that can decide, per D8. Neither node should write it."
            % (key, rounds, escalate_to))


class ConsensusIsNotAGate(RuntimeError):
    """D9. Raised when two agents agree and try to land without a passing gate."""


def dir_() -> pathlib.Path:
    base = os.environ.get("PLEXAR_AGENTS_STATE")
    root = pathlib.Path(base).parent if base else \
        pathlib.Path(os.environ.get("LOCALAPPDATA", ".")) / "plexar-agents"
    return root / "collisions"


def key_for(path: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in str(path))[-100:]


def open_(run_id: str, path: str, claimant: str, holder: str,
          rounds: int = DEFAULT_ROUNDS) -> dict:
    """Open (or advance) a negotiation. Returns what both parties must do next."""
    key = key_for(path)

    def fn(state):
        c = state.setdefault("collisions", {}).setdefault(
            key, {"path": str(path), "parties": [], "round": 0, "resolved": False,
                  "consensus": False, "escalated_to": None, "max_rounds": rounds})
        for p in (holder, claimant):
            if p and p not in c["parties"]:
                c["parties"].append(p)
        c["round"] += 1
        return state

    c = store.update(run_id, fn)["collisions"][key]
    note = dir_() / run_id / ("%s.md" % key)
    _write_note(note, c, run_id)

    over = c["round"] > c["max_rounds"]
    escalate_to = None
    if over:
        escalate_to = "orchestrator" if c.get("escalated_to") else "parent"
        store.update(run_id, lambda s: (s["collisions"][key].update(
            {"escalated_to": escalate_to}), s)[1])

    ledger.write("collision", run_id, claimant, path=str(path), holder=holder,
                 collision_round=c["round"], max_rounds=c["max_rounds"],
                 note_path=str(note), escalated_to=escalate_to, resolved=False)

    if over:
        raise NegotiationUnresolved(key, c["round"], escalate_to)

    return {"key": key, "round": c["round"], "remaining": c["max_rounds"] - c["round"],
            "note": str(note), "parties": c["parties"]}


def _write_note(note: pathlib.Path, c: dict, run_id: str) -> None:
    """The negotiation file. Both parties state a requirement; neither writes the file."""
    try:
        note.parent.mkdir(parents=True, exist_ok=True)
        if note.exists():
            return                                  # never clobber what a party wrote
        body = [
            "# collision — %s" % c["path"],
            "",
            "Run `%s`. Round 1 of %d." % (run_id, c["max_rounds"]),
            "",
            "Both of you want this file. Neither of you may write it yet. State what you",
            "need from it under your own heading, then re-read this file. If you cannot",
            "agree within %d rounds it escalates one tier up." % c["max_rounds"],
            "",
        ]
        for p in c["parties"]:
            body += ["## %s" % p, "_pending_", ""]
        body += [
            "## resolution",
            "_pending_ — and note: agreement is recorded but does NOT land the change.",
            "The gate script still decides (D9).",
            "",
        ]
        note.write_text("\n".join(body), encoding="utf-8", newline="\n")
    except OSError:
        return


def state(run_id: str, path: str) -> dict | None:
    return (store.read(run_id).get("collisions") or {}).get(key_for(path))


def resolve(run_id: str, path: str, consensus: bool, gate_exit: int | None,
            resolution: str | None = None) -> dict:
    """D9 — both must hold. Agreement alone does not land the change.

    `gate_exit is None` means the gate did not run, which is not a pass: a check that
    did not run is not a check that passed.
    """
    key = key_for(path)
    landed = bool(consensus) and gate_exit == 0

    def fn(state):
        c = state.setdefault("collisions", {}).setdefault(key, {"path": str(path)})
        c.update({"consensus": bool(consensus), "gate_exit": gate_exit,
                  "resolved": landed, "resolution": resolution})
        return state

    c = store.update(run_id, fn)["collisions"][key]
    ledger.write("collision_resolved", run_id, None, path=str(path),
                 consensus=bool(consensus), gate_exit=gate_exit, landed=landed,
                 resolution=resolution,
                 refused_reason=None if landed else _why(consensus, gate_exit))

    if not landed:
        raise ConsensusIsNotAGate(
            "%s did not land: %s. Agreement between two agents is recorded, never "
            "sufficient — §4.7, a model's opinion is never the gate."
            % (path, _why(consensus, gate_exit)))
    return c


def _why(consensus: bool, gate_exit: int | None) -> str:
    if not consensus:
        return "no consensus between the parties"
    if gate_exit is None:
        return "the gate did not run, and a check that did not run is not a check that passed"
    return "the gate exited %s" % gate_exit
