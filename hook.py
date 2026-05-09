#!/usr/bin/env python3
"""Claude Code PreToolUse hook -> AgentRelay relay server.

Reads the tool call JSON from stdin, asks the relay server for approval,
and emits Claude Code's hook decision JSON.

Environment:
  AGENTRELAY_URL      base URL of the relay (e.g. https://app.fly.dev)
  AGENTRELAY_TOKEN    shared auth token (optional)
  AGENTRELAY_SESSION  relay's session id (set by the server when it spawned claude)

If the server is unreachable or no AGENTRELAY_SESSION is set, the hook
fails open (approves) so it never wedges your terminal.
"""
from __future__ import annotations
import json
import os
import sys
import urllib.error
import urllib.request


def emit(decision: str, reason: str = "") -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow" if decision == "approve" else "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    sys.exit(0)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        emit("approve", "could not parse hook input")
        return

    url = os.environ.get("AGENTRELAY_URL")
    session_id = os.environ.get("AGENTRELAY_SESSION")
    token = os.environ.get("AGENTRELAY_TOKEN", "")

    if not url or not session_id:
        emit("approve", "agentrelay not configured")
        return

    body = json.dumps(
        {
            "session_id": session_id,
            "tool_name": payload.get("tool_name"),
            "tool_input": payload.get("tool_input", {}),
        }
    ).encode()

    req = urllib.request.Request(
        f"{url.rstrip('/')}/v1/approval",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-AgentRelay-Token": token,
        },
        method="POST",
    )

    try:
        # No timeout — long-poll. The server caps wait time itself (approval_timeout_seconds).
        with urllib.request.urlopen(req) as r:
            response = json.loads(r.read())
    except urllib.error.URLError as e:
        emit("approve", f"agentrelay unreachable: {e}")
        return
    except Exception as e:
        emit("approve", f"agentrelay error: {e}")
        return

    decision = response.get("decision", "approve")
    reason = response.get("reason", "")
    emit(decision, reason)


if __name__ == "__main__":
    main()
