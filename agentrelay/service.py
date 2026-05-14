"""Platform-specific auto-startup for `agentrelay run`.

Installs a service / scheduled task / LaunchAgent that runs AgentRelay at
user login. Designed for single-user dev machines, not multi-user servers.

Platforms:
  - Windows: a Scheduled Task with ONLOGON trigger (`schtasks`)
  - macOS:   a per-user LaunchAgent plist (`~/Library/LaunchAgents/`)
  - Linux:   a systemd user unit (`~/.config/systemd/user/`)
"""
from __future__ import annotations
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Tuple


HOME = Path.home()
TASK_NAME = "AgentRelay"


def _windows_startup_dir() -> Path:
    """Per-user Startup folder. Anything dropped here runs at every login,
    no admin rights needed."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _windows_startup_cmd() -> Path:
    return _windows_startup_dir() / "AgentRelay.cmd"


def _agentrelay_path() -> str:
    """Absolute path to the `agentrelay` entry point script."""
    p = shutil.which("agentrelay")
    if p:
        return p
    # Fallback: invoke via python -m agentrelay.cli
    return f'"{sys.executable}" -m agentrelay.cli'


# ---------- Windows ----------

def _try_schtasks() -> Tuple[bool, str]:
    """Attempt to register a Scheduled Task. Often fails with 'access denied'
    on standard PowerShell. Falls back to Startup folder if so."""
    cmd = _agentrelay_path()
    args = [
        "schtasks",
        "/Create",
        "/SC", "ONLOGON",
        "/TN", TASK_NAME,
        "/TR", f'{cmd} run',
        "/RL", "LIMITED",
        "/F",
    ]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        return False, "schtasks.exe not found"
    except Exception as e:
        return False, f"schtasks failed: {e}"
    if r.returncode != 0:
        return False, f"schtasks exited {r.returncode}: {r.stderr.strip() or r.stdout.strip()}"
    return True, f"Scheduled task '{TASK_NAME}' registered."


def _install_via_startup_folder() -> Tuple[bool, str]:
    """Drop a .cmd into the Startup folder. No elevation needed."""
    startup = _windows_startup_dir()
    try:
        startup.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return False, f"could not access Startup folder ({startup}): {e}"

    # Prefer pythonw.exe so no console window pops up at login.
    pythonw = sys.executable
    if pythonw.lower().endswith("python.exe"):
        candidate = pythonw[: -len("python.exe")] + "pythonw.exe"
        if Path(candidate).exists():
            pythonw = candidate

    # `start "" /b` runs in the background with no new window. The empty
    # title "" is required because start treats a quoted first arg as a title.
    body = (
        "@echo off\r\n"
        f'start "" /b "{pythonw}" -m agentrelay.cli run\r\n'
    )
    cmd_path = _windows_startup_cmd()
    try:
        cmd_path.write_text(body, encoding="ascii")
    except Exception as e:
        return False, f"could not write {cmd_path}: {e}"
    return True, f"Startup script created at {cmd_path} — runs at every login."


def install_windows() -> Tuple[bool, str]:
    # Try schtasks first because it gives the cleanest UX (no console flash,
    # restarts on crash). Fall back to Startup folder if schtasks fails —
    # which it commonly does on a non-elevated PowerShell.
    ok, msg = _try_schtasks()
    if ok:
        return True, msg
    fb_ok, fb_msg = _install_via_startup_folder()
    if fb_ok:
        return True, f"{fb_msg} (schtasks unavailable: {msg})"
    return False, f"both methods failed. schtasks: {msg}. startup-folder: {fb_msg}"


def uninstall_windows() -> Tuple[bool, str]:
    messages = []
    # Remove scheduled task if present.
    try:
        r = subprocess.run(
            ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode == 0:
            messages.append(f"removed scheduled task '{TASK_NAME}'")
    except Exception:
        pass
    # Remove startup folder script if present.
    cmd_path = _windows_startup_cmd()
    if cmd_path.exists():
        try:
            cmd_path.unlink()
            messages.append(f"removed {cmd_path}")
        except Exception as e:
            messages.append(f"could not remove {cmd_path}: {e}")
    if not messages:
        return True, "nothing was installed."
    return True, "; ".join(messages)


# ---------- macOS ----------

MACOS_LABEL = "com.agentrelay"


def _macos_plist_path() -> Path:
    return HOME / "Library" / "LaunchAgents" / f"{MACOS_LABEL}.plist"


def install_macos() -> Tuple[bool, str]:
    p = _macos_plist_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    cmd = shutil.which("agentrelay") or sys.executable
    args_xml = (
        f"        <string>{cmd}</string>\n        <string>run</string>"
        if shutil.which("agentrelay")
        else f"        <string>{sys.executable}</string>\n        <string>-m</string>\n        <string>agentrelay.cli</string>\n        <string>run</string>"
    )
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{MACOS_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{args_xml}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{HOME}/.agentrelay/agentrelay.out.log</string>
    <key>StandardErrorPath</key>
    <string>{HOME}/.agentrelay/agentrelay.err.log</string>
</dict>
</plist>
"""
    p.write_text(plist)
    (HOME / ".agentrelay").mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(["launchctl", "load", "-w", str(p)], check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        return False, f"launchctl load failed: {e.stderr.decode(errors='replace') if e.stderr else e}"
    except FileNotFoundError:
        return True, f"LaunchAgent written to {p} (couldn't run launchctl — reboot to start it)"
    return True, f"LaunchAgent installed at {p}; runs at every login."


def uninstall_macos() -> Tuple[bool, str]:
    p = _macos_plist_path()
    if p.exists():
        try:
            subprocess.run(["launchctl", "unload", str(p)], capture_output=True)
        except Exception:
            pass
        p.unlink()
        return True, f"Removed {p}"
    return True, "No LaunchAgent was installed."


# ---------- Linux (systemd user unit) ----------

def _linux_unit_path() -> Path:
    return HOME / ".config" / "systemd" / "user" / "agentrelay.service"


def install_linux() -> Tuple[bool, str]:
    p = _linux_unit_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    cmd = shutil.which("agentrelay")
    if cmd:
        exec_line = f"ExecStart={cmd} run"
    else:
        exec_line = f"ExecStart={sys.executable} -m agentrelay.cli run"
    unit = f"""[Unit]
Description=AgentRelay — async supervision for autonomous coding agents
After=network.target

[Service]
{exec_line}
Restart=on-failure
RestartSec=5
StandardOutput=append:%h/.agentrelay/agentrelay.out.log
StandardError=append:%h/.agentrelay/agentrelay.err.log

[Install]
WantedBy=default.target
"""
    p.write_text(unit)
    (HOME / ".agentrelay").mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"], check=True, capture_output=True
        )
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", "agentrelay.service"],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        return False, f"systemctl failed: {e.stderr.decode(errors='replace') if e.stderr else e}"
    except FileNotFoundError:
        return (
            True,
            f"Unit written to {p} but systemctl not found. "
            f"Enable manually with: systemctl --user enable --now agentrelay.service",
        )
    return True, f"systemd user unit installed at {p}; runs at every login."


def uninstall_linux() -> Tuple[bool, str]:
    p = _linux_unit_path()
    if p.exists():
        try:
            subprocess.run(
                ["systemctl", "--user", "disable", "--now", "agentrelay.service"],
                capture_output=True,
            )
        except Exception:
            pass
        p.unlink()
        return True, f"Removed {p}"
    return True, "No systemd unit was installed."


# ---------- dispatcher ----------

def install_service() -> Tuple[bool, str]:
    sys_name = platform.system().lower()
    if sys_name == "windows":
        return install_windows()
    if sys_name == "darwin":
        return install_macos()
    if sys_name == "linux":
        return install_linux()
    return False, f"unsupported platform: {sys_name}"


def uninstall_service() -> Tuple[bool, str]:
    sys_name = platform.system().lower()
    if sys_name == "windows":
        return uninstall_windows()
    if sys_name == "darwin":
        return uninstall_macos()
    if sys_name == "linux":
        return uninstall_linux()
    return False, f"unsupported platform: {sys_name}"
