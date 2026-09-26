"""Scope detection that does not care which tool made the change.

**Why this exists (D32, measured 2026-09-22):** with E1 armed, `echo > file` through
Bash and a `python -c` write both landed outside `files_owned`, and the runtime logged
**zero rows**. A `PreToolUse` matcher covers the tools it names; a shell is a hole no
matcher closes, because you cannot statically decide whether a command writes.

So this layer asks the filesystem instead, through git — which sees the result of a
Bash redirect, a python script, a compiler, and a Write tool identically.

**It DETECTS. It does not prevent.** That distinction is the honest one and it is kept
in the names: there is no `enforce()` here. Prevention at this layer would mean a
sandbox or an overlay filesystem, which is a different piece of work (D33).

**Reverting is DESTRUCTIVE (§6).** `propose_revert` returns a plan and a rollback. It
never executes. An agent that can silently `git checkout --` someone's uncommitted work
is a worse problem than the one this module solves.
"""
from __future__ import annotations

import os
import subprocess

from . import ledger


def _git(repo: str, *args: str) -> tuple[int, str]:
    try:
        p = subprocess.run(["git", "-C", repo, *args],
                           capture_output=True, text=True, timeout=30)
        return p.returncode, p.stdout
    except (OSError, subprocess.SubprocessError):
        # Not a repo, git missing, or a timeout. Never raises into the caller — a
        # detection layer that can fail the run is worse than one that reports nothing.
        return 1, ""


def snapshot(repo: str) -> dict[str, str] | None:
    """Every path git considers changed right now, with its status code.

    `None` means "could not observe", which is NOT the same as "nothing changed" and
    must never be rendered as a clean sweep.
    """
    code, out = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if code != 0:
        return None
    seen: dict[str, str] = {}
    for rec in out.split("\0"):
        if len(rec) < 4:
            continue
        seen[rec[3:]] = rec[:2]
    return seen


def _norm(p: str) -> str:
    return os.path.normcase(os.path.abspath(p)).replace("/", os.sep)


def _owned(path: str, repo: str, owned: list[str]) -> bool:
    """An owned entry may be a file or a directory prefix, absolute or repo-relative."""
    target = _norm(os.path.join(repo, path))
    for entry in owned:
        o = _norm(entry if os.path.isabs(entry) else os.path.join(repo, entry))
        if target == o or target.startswith(o.rstrip(os.sep) + os.sep):
            return True
    return False


def sweep(repo: str, owned: list[str], before: dict | None = None) -> dict:
    """What changed that nobody owned.

    `before` is a snapshot taken at dispatch. Without it, every pre-existing local
    change looks like a violation — so its absence is reported rather than papered
    over, and the caller can decide whether the result is usable.
    """
    now = snapshot(repo)
    if now is None:
        return {"observable": False, "reason": "git could not observe this tree",
                "violations": [], "changed": [], "baselined": before is not None}

    changed = [p for p, st in now.items() if before is None or before.get(p) != st]
    violations = [p for p in changed if not _owned(p, repo, owned)]
    return {
        "observable": True,
        "baselined": before is not None,
        "changed": sorted(changed),
        "violations": sorted(violations),
        "reason": None if before is not None else
                  "no baseline: pre-existing local changes are indistinguishable from new ones",
    }


def record(run_id: str, node_id: str | None, repo: str, result: dict) -> None:
    """One row per sweep, including a clean one.

    A sweep that found nothing and a sweep that never ran look identical unless the
    clean one is written down — the same reason `orchestrator_repairs` is mandatory
    including zero.
    """
    ledger.write(
        "scope_sweep", run_id, node_id,
        repo=repo,
        observable=result.get("observable"),
        baselined=result.get("baselined"),
        changed=result.get("changed"),
        violations=result.get("violations"),
        violation_count=len(result.get("violations") or []),
        detector="git",              # NOT the hook: this is the surface Bash writes on
        reason=result.get("reason"),
    )


def propose_revert(repo: str, violations: list[str]) -> dict:
    """A PLAN, never an execution (§6).

    Reverting touches work outside the tree this run owns — by definition, since that
    is what a violation is. The rollback column is the point: it says how to undo the
    undo, and a plan without one is not presentable.
    """
    plan = []
    for p in violations:
        code, _ = _git(repo, "ls-files", "--error-unmatch", p)
        tracked = code == 0
        plan.append({
            "path": p,
            "tracked": tracked,
            "command": ("git -C %s checkout -- %s" % (repo, p)) if tracked
                       else ("del %s" % os.path.join(repo, p)),
            "destroys": "uncommitted edits to a tracked file" if tracked
                        else "an untracked file that may be someone's only copy",
            "rollback": ("git -C %s stash list  # the edit is NOT recoverable unless "
                         "stashed first" % repo) if tracked
                        else "NONE — deleting an untracked file is unrecoverable",
        })
    return {
        "plan": plan,
        "safe_prelude": "git -C %s stash push --include-untracked -m plexar-scope-sweep" % repo,
        "note": ("DESTRUCTIVE. Not executed. Run the prelude first or the untracked "
                 "entries have no rollback at all."),
    }
