"""Which pane did this come from?

Measured 2026-09-22: **50 `run_start` rows, 0 carrying `session_id`.** The spawn log
one directory over had it on all 95 of its rows. Studio knows it has nine panes, the
ledger knows it has fifty runs, and nothing joined them — so the board could show a
box labelled "Prompt" with no idea which mouth it came out of.

The orchestrator does not reliably know its own session id. A hook does: every hook
payload carries `session_id` and `cwd`. So the hook drops a pointer, keyed by working
directory, and `ledger.write` picks it up on every row afterwards.

**The agent is never asked to supply it, and therefore cannot forget to** — E6, applied
to a field rather than to a whole row. That is the difference between this and the
prose rule that produced 0 of 50.

The pointer is advisory: a stale or missing one yields `None`, never a wrong id. A row
attributed to the wrong pane is worse than one attributed to none, for the same reason
a hash that silently fails to verify is worse than no hash.
"""
from __future__ import annotations

import json
import os
import pathlib
import time

MAX_AGE_S = 60 * 60 * 12          # a pointer older than this is stale, not authoritative


def dir_() -> pathlib.Path:
    base = os.environ.get("PLEXAR_AGENTS_STATE")
    if base:
        return pathlib.Path(base).parent / "sessions"
    return pathlib.Path(os.environ.get("LOCALAPPDATA", ".")) / "plexar-agents" / "sessions"


def key(cwd: str | None) -> str:
    """One pointer per working directory.

    Codex writes `C:/Code/...` and Claude writes `C:\\Code\\...` for the same directory;
    grouping on the raw value splits one pane in two, so it is normalised here.
    """
    import hashlib
    norm = os.path.normcase(os.path.abspath(cwd or ".")).replace("/", os.sep)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def record(session_id: str | None, cwd: str | None, extra: dict | None = None) -> None:
    """Called by an adapter, which is the only thing that knows the session id."""
    if not session_id:
        return
    try:
        d = dir_()
        d.mkdir(parents=True, exist_ok=True)
        payload = {"session_id": session_id, "cwd": cwd, "at": time.time()}
        if extra:
            payload.update(extra)
        tmp = d / (key(cwd) + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8", newline="\n")
        tmp.replace(d / (key(cwd) + ".json"))    # atomic: a reader never sees a half file
    except OSError:
        return


def current(cwd: str | None = None) -> dict | None:
    try:
        p = dir_() / (key(cwd or os.getcwd()) + ".json")
        rec = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict) or not rec.get("session_id"):
        return None
    if time.time() - (rec.get("at") or 0) > MAX_AGE_S:
        return None                              # stale: no id beats a wrong id
    return rec


def session_id(cwd: str | None = None) -> str | None:
    rec = current(cwd)
    return rec.get("session_id") if rec else None


def panes() -> list[dict]:
    """Every pane that has written a pointer — every session, as the runtime sees it."""
    out = []
    try:
        for f in sorted(dir_().glob("*.json")):
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(rec, dict) and rec.get("session_id"):
                rec["stale"] = (time.time() - (rec.get("at") or 0)) > MAX_AGE_S
                out.append(rec)
    except OSError:
        pass
    return out
