"""Atomic counters shared by processes that do not know about each other.

The ladder and the budget both count across **spawns**, and a spawn is a separate OS
process. That is the whole reason they cannot live in a hook: a hook is one call with
no memory, and two hooks firing at once would each read 2, each write 3, and the third
attempt would never be counted.

So: one lock, one read-modify-write inside it, one atomic replace.

Concurrency is CRITICAL by §1's own list, and the test that matters is the one that
runs real concurrent processes rather than threads — `tests/test_store.py` does.
"""
from __future__ import annotations

import json
import os
import pathlib
import time

LOCK_TIMEOUT_S = 10.0
LOCK_POLL_S = 0.005
STALE_LOCK_S = 60.0          # a crashed holder must not wedge everything forever


def root() -> pathlib.Path:
    from . import paths
    return paths.home() / "counters"


def _path(run_id: str) -> pathlib.Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in run_id)[:120]
    return root() / ("%s.json" % safe)


class LockTimeout(RuntimeError):
    pass


class _Lock:
    """Exclusive-create lock file. Portable, and visible when it goes wrong.

    `O_EXCL` is atomic on Windows and POSIX alike. A lock older than STALE_LOCK_S is
    broken rather than waited on — a crashed process must not stop every later run,
    and a wedged estate is a worse failure than a rare double-count.
    """

    def __init__(self, target: pathlib.Path):
        self.lock = target.with_suffix(".lock")
        self.fd = None

    def __enter__(self):
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + LOCK_TIMEOUT_S
        while True:
            try:
                self.fd = os.open(str(self.lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                try:
                    if time.time() - self.lock.stat().st_mtime > STALE_LOCK_S:
                        self.lock.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if time.time() > deadline:
                    raise LockTimeout("could not lock %s within %.0fs"
                                      % (self.lock, LOCK_TIMEOUT_S))
                time.sleep(LOCK_POLL_S)

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
        self.lock.unlink(missing_ok=True)
        return False


SHARING_RETRIES = 200        # x LOCK_POLL_S = ~1 s


def read(run_id: str) -> dict:
    """Lock-free read. A missing file is {}; a file another process holds open is NOT.

    Windows refuses to open a file mid-`replace` (PermissionError, a sharing violation).
    Measured 2026-09-22: returning {} for that made a task briefly not exist to a racing
    process. Only a genuinely absent file means empty state.
    """
    p = _path(run_id)
    for _ in range(SHARING_RETRIES):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except PermissionError:
            time.sleep(LOCK_POLL_S)
        except ValueError:
            return {}                        # corrupt; writes are atomic, so not in-flight
    raise LockTimeout("could not read %s: held open by another process" % p)


def _replace(tmp: pathlib.Path, p: pathlib.Path) -> None:
    """os.replace, retried while a lock-free reader has the target open (Windows)."""
    for _ in range(SHARING_RETRIES):
        try:
            tmp.replace(p)
            return
        except PermissionError:
            time.sleep(LOCK_POLL_S)
    tmp.replace(p)                           # final attempt raises the real error


def update(run_id: str, fn) -> dict:
    """Read-modify-write under the lock. `fn(state) -> state`.

    Returns the state as written, so a caller never has to re-read and race.
    """
    p = _path(run_id)
    with _Lock(p):
        try:
            state = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        state = fn(state)
        tmp = p.with_suffix(".tmp.%d" % os.getpid())
        tmp.write_text(json.dumps(state), encoding="utf-8", newline="\n")
        _replace(tmp, p)      # atomic: a reader never sees a half-written file
        return state


def reset(run_id: str) -> None:
    for suffix in (".json", ".lock"):
        try:
            _path(run_id).with_suffix(suffix).unlink(missing_ok=True)
        except OSError:
            pass
