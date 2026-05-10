"""Cross-platform secret storage for AgentRelay.

We store everything OAuth gives us (bot token, install_id, install_secret,
team metadata) in the OS keychain via the `keyring` package — Windows
Credential Manager, macOS Keychain, or freedesktop Secret Service on Linux.

If `keyring` is unavailable or refuses to work (common on headless Linux
without a Secret Service daemon), we fall back to a JSON file under
`~/.agentrelay/credentials.json` with 0600 perms — explicitly logged.
"""
from __future__ import annotations
import json
import os
import stat
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


SERVICE_NAME = "agentrelay"
CREDENTIAL_KEY = "default"
FALLBACK_PATH = Path.home() / ".agentrelay" / "credentials.json"


@dataclass
class Credentials:
    bot_token: str
    install_id: str
    install_secret: str
    team_id: str
    team_name: str
    slack_user_id: str
    dispatcher_url: str

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, s: str) -> "Credentials":
        return cls(**json.loads(s))


def _try_keyring():
    try:
        import keyring  # type: ignore[import-not-found]

        # Probe whether a real backend is available.
        backend = keyring.get_keyring()
        name = type(backend).__name__
        if "fail" in name.lower() or "null" in name.lower():
            return None
        return keyring
    except Exception:
        return None


def save(creds: Credentials) -> str:
    """Persist credentials. Returns the storage backend used: 'keyring' or 'file'."""
    payload = creds.to_json()
    keyring = _try_keyring()
    if keyring is not None:
        try:
            keyring.set_password(SERVICE_NAME, CREDENTIAL_KEY, payload)
            return "keyring"
        except Exception as e:
            print(f"[agentrelay] keyring backend failed ({e!r}); falling back to file.", file=sys.stderr)

    FALLBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
    FALLBACK_PATH.write_text(payload)
    if not sys.platform.startswith("win"):
        os.chmod(FALLBACK_PATH, stat.S_IRUSR | stat.S_IWUSR)
    return "file"


def load() -> Optional[Credentials]:
    """Return stored credentials or None if nothing saved."""
    keyring = _try_keyring()
    if keyring is not None:
        try:
            payload = keyring.get_password(SERVICE_NAME, CREDENTIAL_KEY)
            if payload:
                return Credentials.from_json(payload)
        except Exception:
            pass
    if FALLBACK_PATH.exists():
        try:
            return Credentials.from_json(FALLBACK_PATH.read_text())
        except Exception:
            return None
    return None


def clear() -> None:
    """Remove stored credentials from both keyring and fallback file."""
    keyring = _try_keyring()
    if keyring is not None:
        try:
            keyring.delete_password(SERVICE_NAME, CREDENTIAL_KEY)
        except Exception:
            pass
    if FALLBACK_PATH.exists():
        try:
            FALLBACK_PATH.unlink()
        except Exception:
            pass
