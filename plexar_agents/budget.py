"""The spend ceiling, held by the runtime.

E3. A budget that lives in a prompt is a number a model is asked to keep adding to,
across processes, without ever being told what anything cost — and it was never told,
because until `view/usage.py` nothing in this estate read a token count at all.

So the ceiling is enforced where it can be: **before a spawn**. `check()` refuses a
dispatch that would carry the run past its limit, and `charge()` records what was
actually spent once `usage` can see it.

**It stops loudly.** A budget that silently degrades — smaller models, fewer retries —
produces a worse result that nobody ordered and nobody can see.
"""
from __future__ import annotations

from . import ledger, store


class BudgetExceeded(RuntimeError):
    def __init__(self, run_id: str, spent: float, ceiling: float, unit: str):
        self.run_id, self.spent, self.ceiling, self.unit = run_id, spent, ceiling, unit
        super().__init__(
            "budget exceeded for %s: %s %s spent against a ceiling of %s. The run STOPS. "
            "Raise the ceiling deliberately or end the run — do not continue on cheaper "
            "models, which produces a worse result nobody ordered."
            % (run_id, spent, unit, ceiling))


def set_ceiling(run_id: str, ceiling: float, unit: str = "usd") -> dict:
    def fn(state):
        state.setdefault("budget", {}).update({"ceiling": ceiling, "unit": unit})
        state["budget"].setdefault("spent", 0.0)
        return state
    b = store.update(run_id, fn)["budget"]
    ledger.write("budget_set", run_id, None, ceiling=ceiling, unit=unit)
    return b


def state(run_id: str) -> dict:
    b = store.read(run_id).get("budget") or {}
    return {"ceiling": b.get("ceiling"), "spent": b.get("spent", 0.0),
            "unit": b.get("unit", "usd"),
            "remaining": None if b.get("ceiling") is None
                         else b["ceiling"] - b.get("spent", 0.0)}


def charge(run_id: str, amount: float, node_id: str | None = None,
           source: str = "unmeasured") -> dict:
    """Record spend. `source` says how it was known — measured or estimated.

    An estimate and a measurement must not be indistinguishable afterwards; a ceiling
    enforced against guesses is a ceiling nobody can audit.
    """
    def fn(state):
        b = state.setdefault("budget", {"ceiling": None, "unit": "usd", "spent": 0.0})
        b["spent"] = round(b.get("spent", 0.0) + amount, 6)
        return state
    b = store.update(run_id, fn)["budget"]
    ledger.write("budget_charge", run_id, node_id, amount=amount, spent=b["spent"],
                 ceiling=b.get("ceiling"), unit=b.get("unit", "usd"), source=source)
    return state(run_id)


def check(run_id: str, projected: float = 0.0, node_id: str | None = None) -> dict:
    """Call BEFORE a spawn. Raises rather than returning a flag the caller may ignore."""
    s = state(run_id)
    if s["ceiling"] is None:
        return s                                   # no ceiling set: nothing to enforce
    if s["spent"] + projected > s["ceiling"]:
        ledger.write("budget_refused", run_id, node_id, spent=s["spent"],
                     projected=projected, ceiling=s["ceiling"], unit=s["unit"])
        raise BudgetExceeded(run_id, s["spent"] + projected, s["ceiling"], s["unit"])
    return s
