from __future__ import annotations
import re
from dataclasses import dataclass
from enum import Enum


class Risk(str, Enum):
    SAFE = "SAFE"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    BLOCKED = "BLOCKED"


@dataclass
class Classification:
    risk: Risk
    reason: str


BLOCKED_PATTERNS = [
    (re.compile(r"\brm\s+(?:-[rRf]+\s+)+/(?!\S)"), "destructive root delete"),
    (re.compile(r"\brm\s+(?:-[rRf]+\s+)+~/?\.ssh\b"), "deletes ssh keys"),
    (re.compile(r"\.aws/credentials"), "touches AWS credentials"),
    (re.compile(r"(?:^|\s)cat\s+\S*\.env(?:\s|$)"), "reads .env file"),
    (re.compile(r":\(\)\s*\{[^}]*\}\s*;\s*:"), "fork bomb"),
]

HIGH_PATTERNS = [
    (re.compile(r"\bgit\s+push\b"), "pushes to remote"),
    (re.compile(r"\bgit\s+reset\s+--hard\b"), "discards local changes"),
    (re.compile(r"\bgit\s+branch\s+-D\b"), "force-deletes branch"),
    (re.compile(r"\bgit\s+push\s+--force\b"), "force push"),
    (re.compile(r"\brm\s+-[rRf]"), "recursive delete"),
    (re.compile(r"\bsudo\b"), "elevated privileges"),
    (re.compile(r"\bcurl\s+[^|]*\|\s*(?:sh|bash|zsh)\b"), "pipes remote script to shell"),
    (re.compile(r"\bdrop\s+(?:table|database)\b", re.I), "destructive SQL"),
    (re.compile(r"\b(?:fly|heroku|vercel)\s+deploy\b"), "deploy to production"),
]

MEDIUM_PATTERNS = [
    (re.compile(r"\b(?:npm|pnpm|yarn)\s+(?:install|add|i)\b"), "installs npm dependency"),
    (re.compile(r"\bpip\s+install\b"), "installs pip dependency"),
    (re.compile(r"\bcargo\s+(?:add|install)\b"), "installs cargo dependency"),
    (re.compile(r"\bgit\s+commit\b"), "creates commit"),
    (re.compile(r"\bdocker\s+(?:run|build|push)\b"), "docker operation"),
    (re.compile(r"\bmake\s+(?:install|deploy|publish)\b"), "make target with side-effects"),
]

SENSITIVE_WRITE_PATHS = (".env", ".ssh", "credentials", "id_rsa", ".aws/")


def classify_command(command: str) -> Classification:
    cmd = command.strip()
    for pattern, reason in BLOCKED_PATTERNS:
        if pattern.search(cmd):
            return Classification(Risk.BLOCKED, reason)
    for pattern, reason in HIGH_PATTERNS:
        if pattern.search(cmd):
            return Classification(Risk.HIGH, reason)
    for pattern, reason in MEDIUM_PATTERNS:
        if pattern.search(cmd):
            return Classification(Risk.MEDIUM, reason)
    return Classification(Risk.SAFE, "")


def classify_tool_call(tool_name: str, tool_input: dict) -> Classification:
    if tool_name == "Bash":
        return classify_command(tool_input.get("command", ""))
    if tool_name in ("Write", "Edit"):
        path = tool_input.get("file_path", "")
        if any(p in path for p in SENSITIVE_WRITE_PATHS):
            return Classification(Risk.BLOCKED, f"writes to sensitive path: {path}")
    return Classification(Risk.SAFE, "")
