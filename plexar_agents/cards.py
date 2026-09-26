"""Cards are files, written before the spawn — and hashed so the hash actually verifies.

**The measured failure (2026-09-21, live 12-node SWARM):** all three `brief_sha256`
values failed to verify, 3 for 3. The orchestrator hashed the string it held (LF) while
Windows wrote the file as CRLF, so `sha256(string) != sha256(file)` by construction. The
cards were byte-perfect; only the verification was broken.

**A hash that silently fails to verify is worse than no hash**, because whoever joins on
it gets zero matches and concludes the cards are wrong.

The fix is one line of policy — normalise to LF, write with LF, hash what you wrote —
and it has lived as a paragraph in `§10` asking a model to remember it at every spawn.
Here it is a function that cannot do it the other way, because there is no other way in.

`write()` returns the path and the hash **of the bytes on disk**, not of a string it was
handed. That is the whole mechanism: the thing hashed and the thing written are the same
object by construction, not by discipline.
"""
from __future__ import annotations

import hashlib
import os
import pathlib

from . import ledger, paths


def artifacts_dir() -> pathlib.Path:
    return paths.artifacts_dir()


def normalise(text: str) -> str:
    """CRLF and lone CR both become LF. Applied once, before anything else happens."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def sha256_of_file(path: pathlib.Path) -> str | None:
    """Hash the BYTES ON DISK. Never a string the caller believes it wrote."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def write(run_id: str, worker: str, text: str, attempt: int | None = None,
          is_delta: bool = False) -> dict:
    """Write a card and return `{path, sha256, bytes, verified}`.

    `verified` is read back from disk, so a caller can never log a hash for a file that
    was not written. It is computed, not asserted.
    """
    body = normalise(text)
    if is_delta:
        # A resumed worker received a DELTA, not a standalone card. Conflating the two
        # teaches a classifier that a card it never saw caused an outcome.
        body = ("[DELTA — this is not a standalone card. The worker kept its context and\n"
                " received only what follows.]\n\n") + body

    name = worker if attempt in (None, 1) else "%s-attempt%d" % (worker, attempt)

    try:
        # Path construction is INSIDE the try: resolving the artifacts dir can itself
        # raise (a null byte in the env var), and a failure there must be reported the
        # same way as a failure to write. It was outside, and the first test caught it.
        path = artifacts_dir() / run_id / "cards" / ("%s.txt" % name)
        path.parent.mkdir(parents=True, exist_ok=True)
        # newline="" so Python writes the LF we normalised to, byte for byte, on Windows.
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(body)
    except OSError as e:
        ledger.write("card_write_failed", run_id, None, worker=worker, error=str(e))
        return {"path": None, "sha256": None, "bytes": 0, "verified": False,
                "error": str(e)}

    digest = sha256_of_file(path)
    expected = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return {
        "path": str(path),
        "sha256": digest,
        "bytes": len(body.encode("utf-8")),
        # True only when what is on disk hashes to what we meant to write. The 3-for-3
        # failure is precisely this being False while nobody looked.
        "verified": bool(digest) and digest == expected,
        "retry_mode": "resume_delta" if is_delta else ("respawn" if (attempt or 1) > 1
                                                       else "initial"),
    }


def verify(path: str | pathlib.Path, expected_sha256: str) -> dict:
    """Re-check a logged hash against the file it names."""
    p = pathlib.Path(path)
    actual = sha256_of_file(p)
    return {"path": str(p), "exists": p.exists(), "expected": expected_sha256,
            "actual": actual, "matches": bool(actual) and actual == expected_sha256}
