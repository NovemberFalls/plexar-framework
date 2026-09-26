"""The ledger. The runtime writes it; the agent never does.

A1n needs only enough of this to record a denial. A2n hardens it against the full
§10 schema. The three rules below are not provisional — every one of them is a
measured failure from real runs, and they hold from the first line written.
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib

from . import session as session_mod

from . import paths


def log_dir() -> pathlib.Path:
    """The user's log folder (paths.log_dir): env, else logs.json, else <home>/logs.

    Always a native path: `/c/Code/...` under MSYS *creates a directory* no Windows
    tool can open, and the first pilot lost three appends that way.
    """
    return paths.log_dir()


def now_ts() -> str:
    """ISO-8601 WITH a UTC offset, always.

    40 of 91 rows on 2026-09-21 carried no offset, and the host's zone changed
    mid-day — so some naive stamps are UTC and later ones are local, in the same
    field. A timestamp without an offset is not a timestamp.
    """
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def write(event: str, run_id: str, node_id: str | None = None, **fields) -> None:
    """Append one row. Never raises into the caller.

    Logging failure is never run failure — a run that dies because telemetry failed
    is worse than a run with no telemetry.
    """
    try:
        row = {"ts": now_ts(), "event": event, "run_id": run_id, "node_id": node_id}
        # Which pane did this come from? Never asked of the caller, so never forgotten.
        # Measured 2026-09-22: 50 run_start rows carried 0 session_id under the prose rule.
        if "session_id" not in fields:
            sid = session_mod.session_id(fields.get("cwd") or os.getcwd())
            if sid:
                row["session_id"] = sid
        row.update(fields)
        d = log_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / ("dispatch-%s.jsonl" % datetime.date.today().isoformat())
        # json.dumps, never string concatenation: `C:\Code` hand-written into a JSON
        # string is an invalid escape that silently corrupts the line.
        line = json.dumps(row, default=str) + "\n"
        # Append one line and close. Never read-modify-write: concurrent spawns append.
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)
    except Exception:
        return


_SCHEMA = None


def schema() -> dict:
    """The pinned row schema (ledger_schema.json). One file every writer tests against."""
    global _SCHEMA
    if _SCHEMA is None:
        _SCHEMA = json.loads((pathlib.Path(__file__).parent / "ledger_schema.json")
                             .read_text(encoding="utf-8"))
    return _SCHEMA


def validate(row: dict) -> list[str]:
    """Problems with one row, as readable strings. [] means the row conforms.

    Unknown events and extra keys are allowed (rows grow); missing required keys and
    out-of-vocabulary enum values are not. Used by the test suite and by the harness's
    H6 logging plugin, so both ends agree on the shape of the training data.
    """
    s, out = schema(), []
    ev = row.get("event")
    for k in s["common"] + s["events"].get(ev, []):
        if k not in row:
            out.append("%s: missing %s" % (ev, k))
    for key, allowed in s["enums"].items():
        e, field = key.split(".", 1)
        if e == ev and field in row and row[field] not in allowed:
            out.append("%s.%s=%r not in %r" % (ev, field, row[field], allowed))
    return out
