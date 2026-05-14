"""`agentrelay status` — diagnoses why supervision might not be working.

Checks, in order:
  - Login state (credentials in keychain / file fallback)
  - Local server reachable (http://127.0.0.1:8000/healthz)
  - Dispatcher reachable (the URL stored in credentials)
  - Hook wired (global ~/.claude/settings.json or local .claude/settings.local.json)
  - Auto-startup installed (platform-specific)
  - Recent hook activity (~/.agentrelay/hook.log, if AGENTRELAY_DEBUG=1 was set)
"""
from __future__ import annotations
import json
import os
import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx


HOME = Path.home()
LOCAL_SERVER = "http://127.0.0.1:8000"
USER_SETTINGS = HOME / ".claude" / "settings.json"
HOOK_LOG = HOME / ".agentrelay" / "hook.log"


@dataclass
class Check:
    label: str
    ok: bool
    detail: str = ""


def _check_login() -> Check:
    from .keychain import load as load_creds

    try:
        creds = load_creds()
    except Exception as e:
        return Check("Login", False, f"keychain read failed: {e}")
    if creds is None:
        return Check("Login", False, "not logged in (run `agentrelay login`)")
    return Check(
        "Login",
        True,
        f"{creds.team_name or creds.team_id} via {creds.dispatcher_url}",
    )


def _check_local_server() -> Check:
    try:
        r = httpx.get(f"{LOCAL_SERVER}/healthz", timeout=2.0)
    except httpx.ConnectError:
        return Check(
            "Local server",
            False,
            "not running on 127.0.0.1:8000 (run `agentrelay run`)",
        )
    except Exception as e:
        return Check("Local server", False, f"unreachable: {e}")
    if r.status_code != 200:
        return Check("Local server", False, f"healthz returned {r.status_code}")
    data = r.json()
    adapters = data.get("adapters", [])
    sessions = data.get("sessions", 0)
    return Check(
        "Local server",
        True,
        f"healthy; adapters={adapters}; active sessions={sessions}",
    )


def _check_dispatcher(creds_url: Optional[str]) -> Check:
    if not creds_url:
        return Check("Dispatcher", False, "no dispatcher URL (not logged in)")
    try:
        r = httpx.get(f"{creds_url.rstrip('/')}/healthz", timeout=3.0)
    except Exception as e:
        return Check("Dispatcher", False, f"unreachable: {e}")
    if r.status_code != 200:
        return Check("Dispatcher", False, f"healthz returned {r.status_code}")
    return Check("Dispatcher", True, creds_url)


def _hook_wired_in(settings_path: Path) -> bool:
    if not settings_path.exists():
        return False
    try:
        data = json.loads(settings_path.read_text())
    except Exception:
        return False
    matchers = data.get("hooks", {}).get("PreToolUse", [])
    return any("hook.py" in json.dumps(m) for m in matchers)


def _check_hook_wiring() -> Check:
    global_wired = _hook_wired_in(USER_SETTINGS)
    cwd_settings = Path(".claude") / "settings.local.json"
    local_wired = _hook_wired_in(cwd_settings.resolve())

    if global_wired and local_wired:
        return Check("Hook", True, f"wired globally ({USER_SETTINGS}) AND in current project")
    if global_wired:
        return Check("Hook", True, f"wired globally ({USER_SETTINGS})")
    if local_wired:
        return Check(
            "Hook",
            True,
            f"wired in current project ({cwd_settings.resolve()}); consider `agentrelay wire-hook --global` for all projects",
        )
    return Check(
        "Hook",
        False,
        "not wired here. `agentrelay wire-hook .` (this project) or `agentrelay wire-hook --global` (all projects)",
    )


def _check_autostart() -> Check:
    """Platform-specific check for whether agentrelay run is set to auto-start."""
    sys_name = platform.system().lower()
    if sys_name == "windows":
        try:
            r = subprocess.run(
                ["schtasks", "/Query", "/TN", "AgentRelay"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except Exception as e:
            return Check("Auto-startup", False, f"check failed: {e}")
        if r.returncode == 0:
            return Check("Auto-startup", True, "scheduled task 'AgentRelay' installed")
        # Fall back to checking the Startup folder.
        from .service import _windows_startup_cmd

        cmd_path = _windows_startup_cmd()
        if cmd_path.exists():
            return Check("Auto-startup", True, f"Startup folder script at {cmd_path}")
        return Check(
            "Auto-startup",
            False,
            "not installed (run `agentrelay install-service`)",
        )
    if sys_name == "darwin":
        plist = HOME / "Library" / "LaunchAgents" / "com.agentrelay.plist"
        if plist.exists():
            return Check("Auto-startup", True, f"LaunchAgent at {plist}")
        return Check(
            "Auto-startup",
            False,
            "not installed (run `agentrelay install-service`)",
        )
    if sys_name == "linux":
        unit = HOME / ".config" / "systemd" / "user" / "agentrelay.service"
        if unit.exists():
            return Check("Auto-startup", True, f"systemd user unit at {unit}")
        return Check(
            "Auto-startup",
            False,
            "not installed (run `agentrelay install-service`)",
        )
    return Check("Auto-startup", False, f"unsupported platform: {sys_name}")


def _check_recent_activity() -> Check:
    if not HOOK_LOG.exists():
        return Check(
            "Hook activity log",
            True,
            "no log file (debug logging is off — set AGENTRELAY_DEBUG=1 to enable)",
        )
    try:
        lines = HOOK_LOG.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        return Check("Hook activity log", False, f"read failed: {e}")
    if not lines:
        return Check("Hook activity log", True, "empty")
    invocations = [ln for ln in lines if "hook invoked" in ln]
    last = invocations[-1] if invocations else lines[-1]
    return Check(
        "Hook activity log",
        True,
        f"{len(invocations)} invocations; last: {last[:120]}",
    )


def gather_status() -> list[Check]:
    """Run all checks. Each is independent and shouldn't raise."""
    checks: list[Check] = []
    login = _check_login()
    checks.append(login)
    creds_url: Optional[str] = None
    if login.ok:
        from .keychain import load as load_creds

        creds = load_creds()
        if creds:
            creds_url = creds.dispatcher_url
    checks.append(_check_local_server())
    checks.append(_check_dispatcher(creds_url))
    checks.append(_check_hook_wiring())
    checks.append(_check_autostart())
    checks.append(_check_recent_activity())
    return checks
