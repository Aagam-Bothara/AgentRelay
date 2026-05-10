"""Interactive `agentrelay init` wizard.

Guides a fresh user through Slack setup in ~5 minutes:
  1. Spins up a Cloudflare quick-tunnel (no signup, no token).
  2. Generates a Slack App Manifest pre-filled with that URL.
  3. Copies the manifest to the user's clipboard, opens Slack in the browser.
  4. Prompts for the bot token + channel ID, writes config.toml.
  5. Hands off to `agentrelay run` to start the server.
"""
from __future__ import annotations
import re
import secrets
import sys
import webbrowser
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.markdown import Markdown

from .manifest import build_manifest, to_yaml
from .tunnel import Tunnel


CONFIG_PATH = Path("config.toml")
SLACK_APPS_URL = "https://api.slack.com/apps?new_app=1"

_TOKEN_RE = re.compile(r"^xoxb-[A-Za-z0-9-]+$")
_CHANNEL_RE = re.compile(r"^[CGD][A-Z0-9]{6,}$")

console = Console()


def _try_clipboard(text: str) -> bool:
    try:
        import pyperclip  # type: ignore[import-not-found]

        pyperclip.copy(text)
        return True
    except Exception:
        return False


def _validate_bot_token(value: str) -> str | None:
    value = value.strip()
    if not value:
        return "Token cannot be empty."
    if not _TOKEN_RE.match(value):
        return "Token should start with 'xoxb-'."
    return None


def _validate_channel(value: str) -> str | None:
    value = value.strip()
    if not value:
        return "Channel ID cannot be empty."
    if not _CHANNEL_RE.match(value):
        return "Channel ID should start with C, G, or D (e.g. C0123456789)."
    return None


def _prompt_with_validation(label: str, validator) -> str:
    while True:
        value = Prompt.ask(label).strip()
        err = validator(value)
        if err is None:
            return value
        console.print(f"[red]{err}[/red] Try again.")


def run_init(port: int = 8000) -> None:
    """The main wizard entry point. Called by `agentrelay init`."""
    console.print(
        Panel.fit(
            "[bold cyan]AgentRelay setup wizard[/bold cyan]\n\n"
            "This will get you running in about 5 minutes.\n"
            "You'll need a Slack workspace where you can install apps.",
            border_style="cyan",
        )
    )

    if CONFIG_PATH.exists():
        console.print(
            f"[yellow]A config.toml already exists in {Path.cwd()}.[/yellow]"
        )
        if Prompt.ask("Overwrite it?", choices=["y", "n"], default="n") == "n":
            console.print("Aborted.")
            return

    # ---- Step 1: tunnel ----
    console.print("\n[bold]Step 1/4:[/bold] Starting a Cloudflare quick-tunnel...")
    console.print(
        "[dim](This downloads ~50MB of cloudflared on first run, then never again.)[/dim]"
    )
    tunnel = Tunnel(port=port)
    try:
        url = tunnel.start(timeout=60)
    except Exception as e:
        console.print(f"[red]Tunnel failed:[/red] {e}")
        return
    console.print(f"[green]✓[/green] Tunnel up: [bold]{url}[/bold]")
    console.print(
        "[dim]Note: this URL is ephemeral. For a stable URL, deploy the server "
        "(see README → Deployment).[/dim]"
    )

    try:
        # ---- Step 2: manifest ----
        console.print("\n[bold]Step 2/4:[/bold] Generating Slack App Manifest...")
        manifest = build_manifest(url)
        manifest_yaml = to_yaml(manifest)

        copied = _try_clipboard(manifest_yaml)
        if copied:
            console.print("[green]✓[/green] Manifest copied to your clipboard.")
        else:
            console.print(
                "[yellow]Couldn't access clipboard.[/yellow] "
                "Manually copy this manifest:"
            )
            console.print(Panel(manifest_yaml, border_style="dim"))

        # ---- Step 3: Slack ----
        console.print("\n[bold]Step 3/4:[/bold] Creating the Slack app...")
        console.print(f"Opening [link]{SLACK_APPS_URL}[/link] in your browser.")
        try:
            webbrowser.open(SLACK_APPS_URL)
        except Exception:
            pass
        console.print(
            Markdown(
                """
**In Slack:**

1. Click **Create New App** → **From a manifest**
2. Pick your workspace
3. Paste the manifest (already in your clipboard) → **Next** → **Create**
4. Click **Install to Workspace** → **Allow**
5. Open **OAuth & Permissions** → copy the **Bot User OAuth Token** (starts with `xoxb-`)
6. In Slack, `/invite @AgentRelay` to a channel → click the channel name → copy the **Channel ID** at the bottom
"""
            )
        )

        # ---- Step 4: creds + write config ----
        console.print("\n[bold]Step 4/4:[/bold] Paste credentials below.\n")
        bot_token = _prompt_with_validation(
            "Bot User OAuth Token", _validate_bot_token
        )
        channel = _prompt_with_validation("Channel ID", _validate_channel)

        auth_token = secrets.token_urlsafe(24)
        default_project_dir = str(Path.cwd()).replace("\\", "\\\\")

        CONFIG_PATH.write_text(
            f"""# AgentRelay configuration. Generated by `agentrelay init`.
public_url          = "{url}"
auth_token          = "{auth_token}"
default_project_dir = "{default_project_dir}"

approval_timeout_seconds = 600
stall_threshold_seconds  = 240

[slack]
bot_token = "{bot_token}"
channel   = "{channel}"
"""
        )
        console.print(f"[green]✓[/green] Wrote {CONFIG_PATH.resolve()}")

        # ---- done ----
        console.print(
            Panel.fit(
                "[bold green]Setup complete![/bold green]\n\n"
                "Next:\n"
                "  1. Run [cyan]agentrelay run[/cyan]  (starts server + tunnel)\n"
                "  2. In Slack: [cyan]/relay <task description>[/cyan]\n\n"
                "[yellow]Heads up:[/yellow] each `agentrelay run` gets a new tunnel URL.\n"
                "If you restart and Slack stops responding, run\n"
                "  [cyan]agentrelay rewire-slack[/cyan]\n"
                "to update the Slack app with the new URL.",
                border_style="green",
            )
        )
    finally:
        tunnel.stop()


def run_rewire_slack(port: int = 8000) -> None:
    """Quickly regenerate a manifest for a fresh tunnel URL and copy it.
    The user pastes it into Slack's 'App Manifest' page to update routes."""
    console.print(
        Panel.fit(
            "[bold]Rewire Slack[/bold]\n\n"
            "Starts a new tunnel and prints a manifest you paste into\n"
            "your Slack app's [bold]Manifest[/bold] tab to update its URLs.",
            border_style="cyan",
        )
    )
    tunnel = Tunnel(port=port)
    try:
        url = tunnel.start(timeout=60)
    except Exception as e:
        console.print(f"[red]Tunnel failed:[/red] {e}")
        return
    try:
        console.print(f"[green]✓[/green] New tunnel URL: [bold]{url}[/bold]\n")
        manifest = build_manifest(url)
        manifest_yaml = to_yaml(manifest)
        if _try_clipboard(manifest_yaml):
            console.print("[green]✓[/green] Manifest copied to your clipboard.")
        else:
            console.print(Panel(manifest_yaml, border_style="dim"))

        console.print(
            Markdown(
                """
**To update Slack:**

1. Go to https://api.slack.com/apps → click your AgentRelay app
2. Open the **App Manifest** tab
3. Paste, click **Save Changes**
4. (Optional) **Install to Workspace** again if Slack asks you to reinstall

Then update `public_url` in your `config.toml` to match, and restart the server.
"""
            )
        )

        Prompt.ask("\nPress Enter to stop the tunnel", default="")
    finally:
        tunnel.stop()
