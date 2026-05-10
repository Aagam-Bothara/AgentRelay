"""Slack OAuth login flow for AgentRelay.

The CLI flow is:
  1. Generate a random session token.
  2. Open the user's browser to https://<dispatcher>/oauth/start?session=<token>.
  3. Poll https://<dispatcher>/oauth/poll?session=<token> until the dispatcher
     reports completion (or error/timeout).
  4. Save the returned credentials (bot_token, install_id, install_secret,
     team metadata) to the OS keychain.
"""
from __future__ import annotations
import secrets
import time
import urllib.parse
import webbrowser
from typing import Optional

import httpx

from .keychain import Credentials, save


DEFAULT_DISPATCHER_URL = "https://agentrelay-dispatcher.workers.dev"
POLL_TIMEOUT_SECONDS = 600  # 10 minutes — generous for users who walk away
POLL_INTERVAL_SECONDS = 2.0


class LoginError(Exception):
    pass


def login(
    dispatcher_url: Optional[str] = None,
    timeout: int = POLL_TIMEOUT_SECONDS,
) -> Credentials:
    """Run the Slack OAuth flow. Blocks until completion. Saves credentials.

    Returns the Credentials object. Raises LoginError on failure or timeout.
    """
    base = (dispatcher_url or DEFAULT_DISPATCHER_URL).rstrip("/")
    session = secrets.token_urlsafe(24)
    start_url = f"{base}/oauth/start?session={urllib.parse.quote(session)}"
    poll_url = f"{base}/oauth/poll?session={urllib.parse.quote(session)}"

    try:
        webbrowser.open(start_url)
    except Exception:
        # Some headless environments — surface the URL so the user can open it.
        pass

    deadline = time.time() + timeout
    with httpx.Client(timeout=10.0) as client:
        while time.time() < deadline:
            try:
                r = client.get(poll_url)
            except httpx.HTTPError:
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            if r.status_code != 200:
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            data = r.json()
            state = data.get("state")
            if state == "pending":
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            if state == "expired":
                raise LoginError("OAuth session expired before Slack returned.")
            if state == "error":
                raise LoginError(f"Slack OAuth failed: {data.get('error')}")
            if state == "complete":
                creds = Credentials(
                    bot_token=data["bot_token"],
                    install_id=data["install_id"],
                    install_secret=data["install_secret"],
                    team_id=data["team_id"],
                    team_name=data.get("team_name", ""),
                    slack_user_id=data.get("slack_user_id", ""),
                    dispatcher_url=base,
                )
                save(creds)
                return creds

    raise LoginError("Timed out waiting for Slack OAuth to complete.")


def login_url(dispatcher_url: Optional[str] = None, session: Optional[str] = None) -> str:
    """Return the URL we open in the browser. Useful for tests / manual flows."""
    base = (dispatcher_url or DEFAULT_DISPATCHER_URL).rstrip("/")
    session = session or secrets.token_urlsafe(24)
    return f"{base}/oauth/start?session={urllib.parse.quote(session)}"
