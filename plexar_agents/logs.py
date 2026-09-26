"""The user's logs: read them, page through them, dump them, and bring your own in.

Every row the framework writes lands in `paths.log_dir()` as one JSON object per line,
one file per day (`dispatch-YYYY-MM-DD.jsonl`). That folder IS the log: plain files a
person can open, grep, copy or ship. This module is the programmatic door to the same
files, used by the HTTP API (`/api/logs*`), the SDK (`plexar_agents.client`) and the CLI
(`plexar logs`).

Reading
    read(cursor=..., limit=..., event=..., run_id=..., task=..., date_from=..., date_to=...)
    returns rows oldest-first plus a cursor for the next page. The cursor is opaque
    (today `<file>:<line>`); pass it back unchanged. A line that is not JSON is never
    hidden: it is counted in `corrupt` so a damaged file is visible, not silently short.

Ingesting
    ingest(rows, source=...) appends rows from ANOTHER tool (a harness, CI, a different
    orchestrator) to `ingested-YYYY-MM-DD.jsonl`, never into the framework's own files, so
    provenance is always recoverable from the file name as well as the `source` field.
    Each row must carry `event` and `run_id`, a `ts` WITH a UTC offset, and, for an event
    the pinned schema knows, every required key (`ledger.validate`). Rejected rows come back
    with their reasons; nothing is repaired or guessed.
"""
from __future__ import annotations

import datetime
import json
import pathlib
import re

from . import ledger, paths

_NAME = re.compile(r"^(dispatch|ingested)-(\d{4}-\d{2}-\d{2})\.jsonl$")
MAX_INGEST = 5000


def files(date_from: str | None = None, date_to: str | None = None) -> list[pathlib.Path]:
    d = paths.log_dir()
    if not d.is_dir():
        return []
    out = []
    for p in d.iterdir():
        m = _NAME.match(p.name)
        if not m:
            continue
        day = m.group(2)
        if (date_from and day < date_from) or (date_to and day > date_to):
            continue
        out.append(p)
    # Day first, then the framework's own file before anything ingested that day.
    return sorted(out, key=lambda p: (_NAME.match(p.name).group(2), p.name))


def where() -> dict:
    fs = files()
    return {"log_dir": str(paths.log_dir()), "artifacts_dir": str(paths.artifacts_dir()),
            "settings": str(paths.settings_path()),
            "files": [{"name": p.name, "bytes": p.stat().st_size} for p in fs]}


def _matches(row: dict, event, run_id, task) -> bool:
    if event and row.get("event") not in event:
        return False
    if run_id and row.get("run_id") != run_id:
        return False
    if task and task not in (row.get("node_id"), row.get("task_id"), row.get("subject_node")) \
            and not str(row.get("run_id") or "").endswith(task):
        return False
    return True


def _parse_cursor(cursor: str | None) -> tuple[str, int] | None:
    if not cursor:
        return None
    name, _, line = cursor.rpartition(":")
    try:
        return name, int(line)
    except ValueError:
        raise ValueError("bad cursor: pass back exactly what the last call returned")


def iter_rows(cursor=None, event=None, run_id=None, task=None, date_from=None, date_to=None):
    """(cursor, row) pairs after `cursor`, oldest first; corrupt lines yield (cursor, None)."""
    if isinstance(event, str):
        event = {e for e in event.split(",") if e}
    after = _parse_cursor(cursor)
    if after and not _NAME.match(after[0]):
        raise ValueError("bad cursor: pass back exactly what the last call returned")
    key = lambda name: (_NAME.match(name).group(2), name)
    for p in files(date_from, date_to):
        if after and key(p.name) < key(after[0]):
            continue
        with open(p, encoding="utf-8", errors="replace") as fh:
            for n, line in enumerate(fh, 1):
                if after and p.name == after[0] and n <= after[1]:
                    continue
                if not line.strip():
                    continue
                cur = "%s:%d" % (p.name, n)
                try:
                    row = json.loads(line)
                except ValueError:
                    yield cur, None
                    continue
                if isinstance(row, dict) and _matches(row, event, run_id, task):
                    yield cur, row


def read(cursor=None, limit=500, **filters) -> dict:
    rows, corrupt, last = [], 0, cursor or ""
    for cur, row in iter_rows(cursor, **filters):
        last = cur
        if row is None:
            corrupt += 1
            continue
        rows.append(row)
        if len(rows) >= limit:
            return {"rows": rows, "cursor": last, "more": True, "corrupt": corrupt}
    return {"rows": rows, "cursor": last, "more": False, "corrupt": corrupt}


def _has_offset(ts) -> bool:
    try:
        return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).utcoffset() is not None
    except ValueError:
        return False


def check(row) -> list[str]:
    if not isinstance(row, dict):
        return ["a row must be a JSON object"]
    problems = []
    for k in ("event", "run_id"):
        if not isinstance(row.get(k), str) or not row.get(k):
            problems.append("missing %s" % k)
    if not _has_offset(row.get("ts")):
        problems.append("ts must be ISO-8601 with a UTC offset (e.g. 2026-09-23T18:04:00-04:00)")
    if not problems and row["event"] in ledger.schema()["events"]:
        problems += ledger.validate(row)
    return problems


def ingest(rows: list, source: str) -> dict:
    """Append validated foreign rows. All-or-nothing per row, never per batch."""
    if not source or not str(source).strip():
        raise ValueError("source is required: name the tool the rows came from")
    if len(rows) > MAX_INGEST:
        raise ValueError("at most %d rows per call; page the upload" % MAX_INGEST)
    accepted, rejected, lines = 0, [], []
    for i, row in enumerate(rows):
        problems = check(row)
        if problems:
            rejected.append({"index": i, "problems": problems})
            continue
        out = dict(row)
        out.setdefault("source", source)
        out["ingested_at"] = ledger.now_ts()
        lines.append(json.dumps(out, default=str) + "\n")
        accepted += 1
    if lines:
        d = paths.log_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / ("ingested-%s.jsonl" % datetime.date.today().isoformat())
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.writelines(lines)
    # The ingest itself is an event: who brought how much in, and how much bounced.
    ledger.write("logs_ingested", "logs", None, source=source, accepted=accepted,
                 rejected=len(rejected))
    return {"accepted": accepted, "rejected": rejected}
