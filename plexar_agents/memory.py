"""Per-task memory: one JSON file per task, in the project it works on.

    <repo>/.plexar/tasks/<task id>.json

Everything that happened to the task is appended as it happens: every run, each agent attempt
and its exit, what the gate said, each reviewer's verdict and reasons, a person's notes when
they send it back, questions and answers. The agent is told where the file is (env
PLEXAR_TASK_MEMORY and a line in its prompt), reads it first, and may add its own `notes`.
So a run that crashes, is retried, or comes back from review starts from what is known,
not from zero, and anyone can read afterwards how the task went.

Kept by default. It is removed only when a person gives the final acceptance (the task
review "merged") AND the clear-on-accept setting is on; that setting exists but is off.

`.plexar/` hides itself from git (its own .gitignore holds `*`), so memory never dirties the
working tree, is never committed onto a task branch, and survives reviewer cleanup
(`git clean` without -x leaves ignored files alone).
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib

from . import paths

SETTING = "clear_task_memory_on_accept"


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def root(cwd: str) -> pathlib.Path:
    d = pathlib.Path(cwd) / ".plexar"
    d.mkdir(parents=True, exist_ok=True)
    gi = d / ".gitignore"
    if not gi.exists():
        gi.write_text("# Plexar-Framework working files (task memory). Not part of the project.\n*\n",
                      encoding="utf-8")
    (d / "tasks").mkdir(exist_ok=True)
    return d


def path(cwd: str, task_id: str) -> pathlib.Path:
    return root(cwd) / "tasks" / ("%s.json" % task_id)


def load(cwd: str, task_id: str) -> dict:
    try:
        return json.loads(path(cwd, task_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(p: pathlib.Path, mem: dict) -> None:
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(mem, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, p)


def record(cwd: str, task: dict, kind: str, **data) -> None:
    """Append one event. Never raises: memory is a help, not a gate."""
    try:
        p = path(cwd, task["id"])
        mem = load(cwd, task["id"]) or {"task_id": task["id"], "prompt": task.get("prompt"),
                                        "created": _now(), "events": [], "notes": []}
        mem.setdefault("events", []).append({"at": _now(), "kind": kind, **data})
        mem.setdefault("notes", [])
        _save(p, mem)
    except Exception:
        return


def summary(cwd: str, task_id: str, limit: int = 30) -> str:
    """What earlier runs did, compact, for the next prompt. Empty when there is nothing yet."""
    mem = load(cwd, task_id)
    ev = [e for e in mem.get("events") or [] if e.get("kind") != "run_start"]
    notes = mem.get("notes") or []
    if not ev and not notes:
        return ""
    lines = []
    for e in ev[-limit:]:
        k = e.get("kind")
        if k == "runner":
            lines.append("- attempt %s: the agent exited %s" % (e.get("attempt"), e.get("exit")))
        elif k == "gate":
            lines.append("- attempt %s: the check exited %s: %s" % (e.get("attempt"), e.get("exit"),
                                                                   (e.get("output") or "").strip()[-200:]))
        elif k == "review":
            lines.append("- attempt %s: the %s (%s) said %s: %s%s" % (
                e.get("attempt"), e.get("stage"), e.get("model"), e.get("verdict"),
                (e.get("judgement") or "")[:200], (" Asked to change: %s" % e["feedback"][:200]) if e.get("feedback") else ""))
        elif k == "sent_back":
            lines.append("- a person sent it back (%s): %s" % (e.get("verdict"), e.get("note")))
        elif k == "run_end":
            lines.append("- run %s ended: %s" % (e.get("run_id"), e.get("result")))
        elif k == "not_signed_in":
            lines.append("- the agent was not signed in; nothing was done")
        else:
            lines.append("- %s" % k)
    for n in notes[-10:]:
        lines.append("- your own note: %s" % (n.get("text") if isinstance(n, dict) else n))
    return "\n".join(lines)


def clear_on_accept() -> bool:
    return paths.settings().get(SETTING) is True


def set_clear_on_accept(on: bool) -> None:
    s = paths.settings()
    s[SETTING] = bool(on)
    paths.settings_path().parent.mkdir(parents=True, exist_ok=True)
    paths.settings_path().write_text(json.dumps(s, indent=2), encoding="utf-8")


def accepted(cwd: str, task_id: str) -> bool:
    """Final human acceptance. Removes the memory only if the user turned that on. Returns
    whether it was removed."""
    if not clear_on_accept():
        return False
    try:
        path(cwd, task_id).unlink()
        return True
    except OSError:
        return False
