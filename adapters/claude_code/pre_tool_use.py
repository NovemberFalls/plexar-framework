#!/usr/bin/env python
"""Claude Code PreToolUse adapter — E1 and E6.

This is the ONLY file in the project that knows what Claude Code is. It holds no
policy: it translates a hook payload into a core question and a core answer into
the harness's deny schema. If this file ever imports another adapter, that is a
defect.

Deny schema verified against a working production hook on this machine (a working PreToolUse hook), not inferred from documentation.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from plexar_agents import ledger, leases, session  # noqa: E402

WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

# Keys different write tools use for their target path.
PATH_KEYS = ("file_path", "notebook_path", "path")


def allow() -> None:
    print("{}")


def deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        # A malformed payload is our defect, never the caller's. Never block on it.
        allow()
        return

    # Record which pane we are in before anything else. This fires on EVERY tool call,
    # so the pointer is fresh even for a session that has not run the orchestrator yet.
    session.record(payload.get("session_id"), payload.get("cwd"),
                   {"transcript": payload.get("transcript_path")})

    tool = payload.get("tool_name") or payload.get("tool") or ""
    if tool not in WRITE_TOOLS:
        allow()
        return

    ti = payload.get("tool_input") or {}
    path = next((ti[k] for k in PATH_KEYS if ti.get(k)), None)
    if not path:
        allow()
        return

    verdict = leases.check(path)

    if verdict.allowed:
        if verdict.run_id:
            # E6: written by the runtime, at the moment it happened. The agent is
            # not asked to remember, and therefore cannot forget.
            ledger.write(
                "tool_use", verdict.run_id, verdict.node_id,
                tool=tool, path=path,
                session_id=payload.get("session_id"),
                agent=payload.get("agent_type") or payload.get("subagent_type"),
                decision="allow",
            )
        allow()
        return

    ledger.write(
        "enforced_denial", verdict.run_id, verdict.node_id,
        invariant="E1", tool=tool, path=path,
        session_id=payload.get("session_id"),
        agent=payload.get("agent_type") or payload.get("subagent_type"),
        state=verdict.state, owned=verdict.owned,
    )
    deny(verdict.explain())


if __name__ == "__main__":
    main()
