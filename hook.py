#!/usr/bin/env python3
"""Claude Code PreToolUse hook -> AgentRelay relay server.

Reads the tool call JSON from stdin, asks the relay server for approval,
and emits Claude Code's hook decision JSON.

Environment:
  AGENTRELAY_URL      base URL of the relay (default: http://127.0.0.1:8000)
  AGENTRELAY_TOKEN    shared auth token (optional)
  AGENTRELAY_SESSION  relay's session id (only set when the server itself
                      spawned claude via /v1/start; the VS Code extension
                      and direct `claude` invocations don't set this)

If neither AGENTRELAY_SESSION nor a session_id from Claude Code's hook
payload is available, the hook fails open. Otherwise the server uses
Claude Code's session_id and auto-creates a session on first contact —
this is what lets AgentRelay work with the VS Code extension and
ad-hoc CLI use, not just server-spawned sessions.
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

    url = os.environ.get("AGENTRELAY_URL", "http://127.0.0.1:8000")
    # Prefer the env var (server-spawned mode) but fall back to Claude Code's
    # own session_id from the hook payload, which is set in BOTH the CLI and
    # the VS Code extension. That's what makes extension-launched sessions
    # supervisable.
    session_id = os.environ.get("AGENTRELAY_SESSION") or payload.get("session_id")
    token = os.environ.get("AGENTRELAY_TOKEN", "")

    if not session_id:
        emit("approve", "agentrelay: no session id available")
        return

    body = json.dumps(
        {
            "session_id": session_id,
            "tool_name": payload.get("tool_name"),
            "tool_input": payload.get("tool_input", {}),
            "cwd": payload.get("cwd") or os.getcwd(),
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
