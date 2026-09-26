"""The escalation ladder, counted by the runtime.

§4.8 says three attempts then a person, and the lane goes up one tier on the way. That
rule has lived as a sentence a model was asked to remember across separate spawns —
which is precisely the thing a model cannot do, because each spawn is a fresh process
with no memory of the last one.

`attempts` in the ledger has therefore always been a self-report. Here it is a counter.
"""
from __future__ import annotations

from . import ledger, store

MAX_ATTEMPTS = 3
LANES = ["haiku", "sonnet", "opus"]


class LadderExhausted(RuntimeError):
    """Raised on the fourth attempt. The human is the fourth tier, not a fourth retry."""

    def __init__(self, run_id: str, node_id: str, attempts: int, history: list):
        self.run_id, self.node_id, self.attempts, self.history = run_id, node_id, attempts, history
        super().__init__(
            "ladder exhausted for %s/%s after %d attempts (%s). §4.8: three attempts, "
            "then a person. Do not spawn again — report to the human."
            % (run_id, node_id, attempts, " -> ".join(history) or "no lanes recorded"))


def escalate(lane: str | None) -> str | None:
    """One lane up. A lane already at the top stays there — opus has nowhere to go."""
    if lane not in LANES:
        return lane
    i = LANES.index(lane)
    return LANES[min(i + 1, len(LANES) - 1)]


def attempt(run_id: str, node_id: str, lane: str | None = None,
            retry_mode: str = "initial") -> dict:
    """Record an attempt and return what the caller must obey.

    Raises LadderExhausted on the attempt AFTER the cap, so the caller cannot
    accidentally treat the cap as advisory.
    """
    def bump(state):
        nodes = state.setdefault("ladder", {})
        rec = nodes.setdefault(node_id, {"attempts": 0, "lanes": [], "modes": []})
        rec["attempts"] += 1
        rec["lanes"].append(lane or "?")
        rec["modes"].append(retry_mode)
        return state

    rec = store.update(run_id, bump)["ladder"][node_id]
    n = rec["attempts"]

    if n > MAX_ATTEMPTS:
        ledger.write("ladder_exhausted", run_id, node_id, attempts=n,
                     lanes=rec["lanes"], retry_modes=rec["modes"], counted_by="runtime")
        raise LadderExhausted(run_id, node_id, n, rec["lanes"])

    nxt = escalate(lane) if n > 1 else lane
    ledger.write("ladder_attempt", run_id, node_id, attempt=n, lane=lane,
                 next_lane_if_retried=escalate(lane), retry_mode=retry_mode,
                 remaining=MAX_ATTEMPTS - n, counted_by="runtime")
    return {"attempt": n, "lane": lane, "use_lane": nxt,
            "remaining": MAX_ATTEMPTS - n, "exhausted": False}


def attempts(run_id: str, node_id: str) -> int:
    return (store.read(run_id).get("ladder", {}).get(node_id, {}) or {}).get("attempts", 0)
