"""Where the framework keeps things on disk. One place, so a user can always find their data.

    home        state, tasks, counters, daemon config   PLEXAR_AGENTS_HOME, else the
                                                        per-user data dir below
    log_dir     the ledger (one JSONL file per day)     PLEXAR_AGENTS_LOG_DIR, else
                                                        logs.json "log_dir", else home/logs
    artifacts   cards and other run artifacts           PLEXAR_AGENTS_ARTIFACTS, else
                                                        logs.json "artifacts_dir", else
                                                        home/artifacts

The logs are the user's: never a machine-local default that only exists on one developer's
box (an earlier default was a fixed developer path, which on any
other machine silently created that folder). A team that wants the rows somewhere else
sets it once with `plexar logs dir --set <path>`, which writes logs.json here.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys


def home() -> pathlib.Path:
    if os.environ.get("PLEXAR_AGENTS_HOME"):
        return pathlib.Path(os.environ["PLEXAR_AGENTS_HOME"])
    # Tests point PLEXAR_AGENTS_STATE at <tmp>/state/runs; everything else lives beside it.
    if os.environ.get("PLEXAR_AGENTS_STATE"):
        return pathlib.Path(os.environ["PLEXAR_AGENTS_STATE"]).parent
    if os.name == "nt":
        return pathlib.Path(os.environ.get("LOCALAPPDATA") or pathlib.Path.home() / "AppData" / "Local") / "plexar-agents"
    if sys.platform == "darwin":
        return pathlib.Path.home() / "Library" / "Application Support" / "plexar-agents"
    return pathlib.Path(os.environ.get("XDG_DATA_HOME") or pathlib.Path.home() / ".local" / "share") / "plexar-agents"


def settings_path() -> pathlib.Path:
    return home() / "logs.json"


def settings() -> dict:
    try:
        return json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def set_setting(key: str, value: str | None) -> dict:
    s = settings()
    if value is None:
        s.pop(key, None)
    else:
        s[key] = str(pathlib.Path(value).expanduser().resolve())
    settings_path().parent.mkdir(parents=True, exist_ok=True)
    settings_path().write_text(json.dumps(s, indent=2), encoding="utf-8")
    return s


def log_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("PLEXAR_AGENTS_LOG_DIR") or settings().get("log_dir")
                        or home() / "logs")


def artifacts_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("PLEXAR_AGENTS_ARTIFACTS") or settings().get("artifacts_dir")
                        or home() / "artifacts")
