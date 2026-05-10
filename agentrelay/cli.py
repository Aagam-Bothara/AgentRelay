"""Top-level CLI for AgentRelay.

Entry point: `agentrelay`. Subcommands:
  login          — sign in via Slack OAuth, store credentials in OS keychain
  logout         — clear stored credentials
  run            — start the local server (defaults to dispatcher mode)
  init           — self-hosted setup wizard (cloudflared + Slack manifest)
  wire-hook      — install the PreToolUse hook into a project
  rewire-slack   — regenerate Slack manifest after a tunnel restart (self-hosted)
"""
from __future__ import annotations
import os
import signal
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

console = Console()
app = typer.Typer(
    help="AgentRelay — supervise your coding agent from your phone.",
    no_args_is_help=True,
    add_completion=False,
)


@app.command()
def login(
    dispatcher: Optional[str] = typer.Option(
        None,
        help="Override the dispatcher URL (defaults to the hosted instance).",
    ),
) -> None:
    """Sign in via Slack OAuth. Saves credentials to your OS keychain."""
    from .auth import DEFAULT_DISPATCHER_URL, LoginError, login as run_login

    target = dispatcher or DEFAULT_DISPATCHER_URL
    console.print(f"Opening browser to authorize Slack at [bold]{target}[/bold]...")
    console.print(
        "[dim]If the browser doesn't open, copy the URL printed below and open it manually.[/dim]"
    )
    try:
        creds = run_login(dispatcher_url=target)
    except LoginError as e:
        console.print(f"[red]Login failed:[/red] {e}")
        raise typer.Exit(code=1)
    console.print(
        f"[green]✓ Connected to[/green] [bold]{creds.team_name or creds.team_id}[/bold]"
    )
    console.print(
        "[dim]Now run [cyan]agentrelay run[/cyan] to start the server. "
        "Approval messages will arrive as DMs from the AgentRelay bot.[/dim]"
    )


@app.command()
def logout() -> None:
    """Remove stored credentials from your OS keychain."""
    from .keychain import clear, load

    creds = load()
    if creds is None:
        console.print("Nothing to log out from.")
        return
    clear()
    console.print(
        f"[green]✓ Cleared credentials[/green] for [bold]{creds.team_name or creds.team_id}[/bold]."
    )


@app.command()
def run(
    host: str = typer.Option("127.0.0.1", help="Server bind host."),
    port: int = typer.Option(8000, help="Server port."),
    self_hosted: bool = typer.Option(
        False,
        "--self-hosted",
        help="Self-hosted mode: use config.toml + Cloudflare quick-tunnel "
        "instead of the hosted dispatcher.",
    ),
    tunnel: bool = typer.Option(
        True,
        "--tunnel/--no-tunnel",
        help="(Self-hosted only.) Start a Cloudflare tunnel alongside the server.",
    ),
) -> None:
    """Start the AgentRelay server.

    Default is dispatcher mode (requires a prior `agentrelay login`). Use
    `--self-hosted` for the v0.2-style flow with config.toml + cloudflared.
    """
    if self_hosted:
        _run_self_hosted(host=host, port=port, tunnel=tunnel)
    else:
        _run_dispatcher(host=host, port=port)


def _run_dispatcher(host: str, port: int) -> None:
    """Dispatcher mode: load creds from keychain, point the server at the
    hosted dispatcher, no tunnel needed (the websocket is outbound)."""
    from .keychain import load as load_creds
    import uvicorn

    creds = load_creds()
    if creds is None:
        console.print(
            "[red]No credentials found.[/red] Run [cyan]agentrelay login[/cyan] first, "
            "or pass [cyan]--self-hosted[/cyan] for the v0.2 setup."
        )
        raise typer.Exit(code=1)

    console.print(
        f"[green]Dispatcher mode[/green] → [bold]{creds.dispatcher_url}[/bold]"
    )
    console.print(
        f"[dim]Slack workspace:[/dim] {creds.team_name or creds.team_id}"
    )
    os.environ["AGENTRELAY_MODE"] = "dispatcher"
    # The server reads AGENTRELAY_URL when spawning claude so the hook can
    # phone home; in dispatcher mode we still bind locally.
    os.environ.setdefault("AGENTRELAY_URL", f"http://{host}:{port}")
    uvicorn.run("agentrelay.server:app", host=host, port=port)


def _run_self_hosted(host: str, port: int, tunnel: bool) -> None:
    """Self-hosted mode (v0.2): config.toml + optional cloudflared tunnel."""
    import uvicorn

    from .tunnel import Tunnel

    tun: Optional[Tunnel] = None
    if tunnel:
        console.print("[cyan]Starting Cloudflare tunnel...[/cyan]")
        tun = Tunnel(port=port)
        try:
            url = tun.start(timeout=60)
        except Exception as e:
            console.print(f"[red]Tunnel failed:[/red] {e}")
            console.print(
                "[yellow]Continuing without tunnel — server only reachable on localhost.[/yellow]"
            )
            tun = None
        else:
            console.print(f"[green]✓ Tunnel:[/green] [bold]{url}[/bold]")
            console.print(
                "[dim]If this URL differs from your Slack app, "
                "run [cyan]agentrelay rewire-slack[/cyan].[/dim]\n"
            )

    def _shutdown(*_):
        if tun is not None:
            tun.stop()
        sys.exit(0)

    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _shutdown)

    os.environ["AGENTRELAY_MODE"] = "self-hosted"
    try:
        uvicorn.run("agentrelay.server:app", host=host, port=port)
    finally:
        if tun is not None:
            tun.stop()


@app.command()
def init(
    port: int = typer.Option(8000, help="Local port the server will run on."),
) -> None:
    """[Self-hosted] interactive setup: tunnel + Slack manifest + config.toml."""
    from .wizard import run_init

    run_init(port=port)


@app.command(name="wire-hook")
def wire_hook(
    project_dir: Path = typer.Argument(
        ..., help="Path to the project where Claude Code will run."
    ),
) -> None:
    """Install the AgentRelay PreToolUse hook into a project."""
    from .server import write_session_settings

    project_dir = project_dir.resolve()
    if not project_dir.exists() or not project_dir.is_dir():
        console.print(f"[red]Not a directory:[/red] {project_dir}")
        raise typer.Exit(code=1)
    write_session_settings(str(project_dir))
    settings_path = project_dir / ".claude" / "settings.local.json"
    console.print(f"[green]✓[/green] Wired hook into {settings_path}")


@app.command(name="rewire-slack")
def rewire_slack(
    port: int = typer.Option(8000, help="Local port the server will run on."),
) -> None:
    """[Self-hosted] regenerate a Slack App Manifest for a fresh tunnel URL."""
    from .wizard import run_rewire_slack

    run_rewire_slack(port=port)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
