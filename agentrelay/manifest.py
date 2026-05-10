"""Slack App Manifest generator.

Slack supports creating an app from a YAML or JSON manifest, which lets us
pre-fill scopes, slash commands, and interactivity URLs in one shot. The
user then only has to click 'Create' and 'Install to Workspace'.

https://api.slack.com/reference/manifests
"""
from __future__ import annotations
import json
from typing import Any


def build_manifest(public_url: str, app_name: str = "AgentRelay") -> dict[str, Any]:
    """Build a Slack App Manifest dict for the given public URL."""
    base = public_url.rstrip("/")
    return {
        "display_information": {
            "name": app_name,
            "description": "Async supervision for autonomous coding agents",
            "background_color": "#1a1a1a",
        },
        "features": {
            "bot_user": {
                "display_name": app_name,
                "always_online": True,
            },
            "slash_commands": [
                {
                    "command": "/relay",
                    "url": f"{base}/v1/slack/slash",
                    "description": "Run an autonomous coding task",
                    "usage_hint": "<task description>",
                    "should_escape": False,
                }
            ],
        },
        "oauth_config": {
            "scopes": {
                "bot": [
                    "chat:write",
                    "commands",
                ]
            }
        },
        "settings": {
            "interactivity": {
                "is_enabled": True,
                "request_url": f"{base}/v1/slack/interactive",
            },
            "org_deploy_enabled": False,
            "socket_mode_enabled": False,
            "token_rotation_enabled": False,
        },
    }


def to_yaml(manifest: dict[str, Any]) -> str:
    """Render a manifest dict as YAML. Uses PyYAML if available, otherwise
    falls back to JSON (Slack accepts both)."""
    try:
        import yaml  # type: ignore[import-not-found]

        return yaml.safe_dump(manifest, sort_keys=False)
    except ImportError:
        return json.dumps(manifest, indent=2)


def to_json(manifest: dict[str, Any]) -> str:
    return json.dumps(manifest, indent=2)
