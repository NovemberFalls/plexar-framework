"""File leases — the E1 check.

D3: freedom in the middle, enforcement at the boundary. Nothing here constrains how
an agent reaches its destination. It constrains only which files the destination is
allowed to be.

**Enforcement is opt-in by construction.** With no active run, `check()` returns OK
for everything. A governed run is a deliberate act; an ordinary session is untouched.
That is not a safety fallback bolted on — it is the shape of the decision.
"""
from __future__ import annotations

import json
import os
import pathlib
from dataclasses import dataclass, field

DEFAULT_STATE_DIR = pathlib.Path(os.environ.get("LOCALAPPDATA", ".")) / "plexar-agents" / "runs"

OK = "OK"
OUTSIDE_OWNED = "OUTSIDE_OWNED"
COLLISION = "COLLISION"


def state_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("PLEXAR_AGENTS_STATE", str(DEFAULT_STATE_DIR)))


@dataclass
class Verdict:
    state: str
    run_id: str | None = None
    node_id: str | None = None
    holder: str | None = None
    round: int = 0
    path: str | None = None
    owned: list = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.state == OK

    def explain(self) -> str:
        if self.state == OUTSIDE_OWNED:
            shown = ", ".join(self.owned[:6]) or "(none)"
            more = "" if len(self.owned) <= 6 else " (+%d more)" % (len(self.owned) - 6)
            return (
                "plexar-agents E1: %s is outside this run's owned files.\n"
                "Run %s owns: %s%s\n"
                "This write was not applied. If the file genuinely belongs to this "
                "node, the PLAN's files_owned is wrong and that is a planning defect "
                "to raise — do not work around it."
                % (self.path, self.run_id, shown, more)
            )
        if self.state == COLLISION:
            return (
                "plexar-agents E2: %s is held by %s. State your requirement in the "
                "negotiation file and re-read it. Round %d."
                % (self.path, self.holder, self.round)
            )
        return ""


def active_run_id() -> str | None:
    """The ACTIVE pointer file, not an env var.

    Env does not reliably reach a hook spawned mid-session, and a governed run must
    be able to start without restarting the harness.
    """
    p = state_dir() / "ACTIVE"
    try:
        rid = p.read_text(encoding="utf-8").strip()
        return rid or None
    except OSError:
        return None


def load_state(run_id: str) -> dict | None:
    try:
        return json.loads((state_dir() / ("%s.json" % run_id)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _norm(p: str) -> str:
    """Codex writes C:/Code/... and Claude writes C:\\Code\\... for one directory.

    Compared raw, the same file is two files and the check reports success by luck.
    """
    try:
        return os.path.normcase(os.path.abspath(str(p))).replace("/", os.sep)
    except Exception:
        return str(p)


def _covered(target: str, owned_entry: str) -> bool:
    """An owned entry may be a file or a directory prefix."""
    t, o = _norm(target), _norm(owned_entry)
    return t == o or t.startswith(o.rstrip(os.sep) + os.sep)


def check(path: str, node_id: str | None = None) -> Verdict:
    run_id = active_run_id()
    if not run_id:
        return Verdict(OK)                      # no governed run: nothing is enforced
    st = load_state(run_id)
    if not st:
        return Verdict(OK, run_id=run_id)       # unreadable state never blocks work
    if not st.get("enforce", True):
        return Verdict(OK, run_id=run_id)

    nodes = st.get("nodes", {})
    union = [f for n in nodes.values() for f in n.get("files_owned", [])]
    if not union:
        return Verdict(OK, run_id=run_id)

    if any(_covered(path, o) for o in union):
        # Attribution to a specific node, and therefore collision detection, is A6n.
        # A1n asserts only the outer boundary.
        return Verdict(OK, run_id=run_id, node_id=node_id, path=path, owned=union)

    return Verdict(OUTSIDE_OWNED, run_id=run_id, node_id=node_id, path=path, owned=union)
