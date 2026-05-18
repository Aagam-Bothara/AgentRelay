"""End-to-end zero-friction setup.

`agentrelay setup` (or just `agentrelay` on first run) chains:

  1. Slack OAuth login (browser flow → OS keychain)
  2. Global hook install (~/.claude/settings.json — every Claude Code session)
  3. Auto-start service (scheduled task / LaunchAgent / systemd user unit)
  4. Background-spawn the server so supervision is active immediately

After this exits the user is fully installed. They never need to run another
AgentRelay command — supervision keeps working after reboots, in every IDE,
on every project. Subsequent commands (`status`, `logout`, `uninstall-service`)
are management, not maintenance.
"""
from __future__ import annotations
import subprocess
import sys
from typing import Optional

import httpx
from rich.console import Console
from rich.panel import Panel


console = Console()


def _server_already_running(host: str = "127.0.0.1", port: int = 8000) -> bool:
    try:
        r = httpx.get(f"http://{host}:{port}/healthz", timeout=1.0)
        return r.status_code == 200
    except Exception:
        return False


def _spawn_server_detached() -> Optional[int]:
    """Start `agentrelay run` as a detached background process so it keeps
    running after this wizard exits. Returns the PID or None on failure."""
    import shutil

    binary = shutil.which("agentrelay")
    if binary:
        args = [binary, "run"]
    else:
        args = [sys.executable, "-m", "agentrelay.cli", "run"]

    try:
        if sys.platform == "win32":
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            proc = subprocess.Popen(
                args,
                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                close_fds=True,
            )
        else:
            proc = subprocess.Popen(
                args,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                close_fds=True,
            )
        return proc.pid
    except Exception as e:
        console.print(f"[red]Could not spawn server: {e}[/red]")
        return None


def run_setup(dispatcher: Optional[str] = None) -> int:
    """Run the full chained onboarding. Returns exit code (0 = success)."""
    from .auth import DEFAULT_DISPATCHER_URL, LoginError, login
    from .keychain import load
    from .server import write_global_settings
    from .service import install_service

    console.print(
        Panel.fit(
            "[bold cyan]AgentRelay setup[/bold cyan]\n\n"
            "One-time install. About 30 seconds + one browser click.\n"
            "After this, every Claude Code session on this machine is\n"
            "supervised forever — no further setup, even after reboots.",
            border_style="cyan",
        )
    )

    # ---- 1. Login ----
    existing = load()
    if existing is not None:
        console.print(
            f"\n[bold]1/4[/bold] Already logged in to "
            f"[bold]{existing.team_name or existing.team_id}[/bold]. "
            f"[dim]Skipping login.[/dim]"
        )
    else:
        console.print("\n[bold]1/4[/bold] Logging into Slack...")
        target = dispatcher or DEFAULT_DISPATCHER_URL
        console.print(f"[dim]Opening browser to {target}...[/dim]")
        try:
            creds = login(dispatcher_url=target)
        except LoginError as e:
            console.print(f"[red]Login failed:[/red] {e}")
            return 1
        console.print(
            f"[green]✓[/green] Connected to "
            f"[bold]{creds.team_name or creds.team_id}[/bold]"
        )

    # ---- 2. Global hook ----
    console.print("\n[bold]2/4[/bold] Installing global hook...")
    try:
        hook_path = write_global_settings()
        console.print(f"[green]✓[/green] Hook merged into {hook_path}")
        console.print(
            "[dim]Every Claude Code session (CLI + extension + JetBrains) "
            "will route through AgentRelay.[/dim]"
        )
    except Exception as e:
        console.print(f"[red]Failed to install hook:[/red] {e}")
        return 1

    # ---- 3. Auto-startup ----
    console.print("\n[bold]3/4[/bold] Registering auto-startup...")
    ok, msg = install_service()
    if ok:
        console.print(f"[green]✓[/green] {msg}")
    else:
        # Non-fatal — supervision still works while server is running, user
        # just has to restart it manually after reboots.
        console.print(f"[yellow]⚠ auto-startup not installed:[/yellow] {msg}")
        console.print(
            "[dim](You can retry later with [cyan]agentrelay install-service[/cyan]. "
            "Supervision will still work as long as the server is running.)[/dim]"
        )

    # ---- 4. Start the server now ----
    console.print("\n[bold]4/4[/bold] Starting the server in the background...")
    if _server_already_running():
        console.print("[green]✓[/green] Server is already running.")
    else:
        pid = _spawn_server_detached()
        if pid:
            console.print(f"[green]✓[/green] Server started (pid {pid}).")
        else:
            console.print(
                "[yellow]⚠ couldn't auto-start the server.[/yellow] "
                "Start it manually with [cyan]agentrelay run[/cyan]."
            )

    # ---- Done ----
    console.print(
        Panel.fit(
            "[bold green]All set.[/bold green]\n\n"
            "AgentRelay is now supervising every Claude Code session.\n"
            "Approval messages will arrive as DMs from the AgentRelay bot.\n\n"
            "You don't need to run anything else. Ever.\n\n"
            "[dim]Verify anytime with[/dim] [cyan]agentrelay status[/cyan]",
            border_style="green",
        )
    )
    return 0
