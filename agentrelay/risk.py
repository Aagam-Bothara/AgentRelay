from __future__ import annotations
import re
import shlex
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Union


class Risk(str, Enum):
    SAFE = "SAFE"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    BLOCKED = "BLOCKED"


@dataclass
class Classification:
    risk: Risk
    reason: str


# A pattern's "reason" can be a static string OR a callable that takes the
# raw command and returns a command-specific description. Callables let us
# extract branch names, packages, paths, etc. so approval messages tell
# users *what* is happening, not just *what category* of risky thing.
Reason = Union[str, Callable[[str], str]]


# ---------- per-command reason builders ----------

def _git_push_reason(cmd: str) -> str:
    """Extract remote + branch + force flag from a git push command."""
    force = bool(re.search(r"\bgit\s+push\b.*(?:--force(?:-with-lease)?|\s-f\b)", cmd))
    # `git push [<remote>] [<refspec>]` — both args optional
    m = re.search(r"\bgit\s+push\b(?:\s+--?\S+)*(?:\s+(\S+)(?:\s+(\S+))?)?", cmd)
    remote = m.group(1) if m and m.group(1) and not m.group(1).startswith("-") else None
    branch = m.group(2) if m and m.group(2) else None

    parts = []
    if branch:
        parts.append(f"branch `{branch}`")
    else:
        parts.append("the current branch")
    parts.append(f"to remote `{remote or 'origin'}`")
    base = "Pushes " + " ".join(parts)
    if force:
        base += " *— force push, may overwrite remote history*"
    return base


def _git_reset_hard_reason(cmd: str) -> str:
    m = re.search(r"\bgit\s+reset\s+--hard\b\s*(\S+)?", cmd)
    target = m.group(1) if m and m.group(1) else "HEAD"
    return (
        f"Discards all uncommitted changes and resets to `{target}` "
        "*— uncommitted work is lost*"
    )


def _rm_rf_reason(cmd: str) -> str:
    # Strip the rm and flags, leave the targets.
    m = re.search(r"\brm\s+(?:-[a-zA-Z]+\s*)+(.+)", cmd)
    if not m:
        return "Recursive delete"
    try:
        targets = [t for t in shlex.split(m.group(1)) if not t.startswith("-")]
    except ValueError:
        targets = m.group(1).split()
    if not targets:
        return "Recursive delete"
    shown = ", ".join(f"`{t}`" for t in targets[:3])
    suffix = "" if len(targets) <= 3 else f" *and {len(targets) - 3} more*"
    return f"Recursively deletes {shown}{suffix}"


def _npm_install_reason(cmd: str) -> str:
    m = re.search(r"\b(?:npm|pnpm|yarn)\s+(?:install|add|i)\b\s*(.*)", cmd)
    if not m or not m.group(1).strip():
        return "Installs dependencies from package.json (no specific packages named)"
    try:
        pkgs = [t for t in shlex.split(m.group(1)) if not t.startswith("-")]
    except ValueError:
        pkgs = m.group(1).split()
    if not pkgs:
        return "Installs dependencies from package.json"
    shown = ", ".join(f"`{p}`" for p in pkgs[:3])
    suffix = "" if len(pkgs) <= 3 else f" *and {len(pkgs) - 3} more*"
    return f"Installs npm package(s): {shown}{suffix}"


def _pip_install_reason(cmd: str) -> str:
    m = re.search(r"\bpip\s+install\b\s*(.*)", cmd)
    if not m or not m.group(1).strip():
        return "Runs `pip install` with no arguments"
    try:
        pkgs = [t for t in shlex.split(m.group(1)) if not t.startswith("-")]
    except ValueError:
        pkgs = m.group(1).split()
    if not pkgs:
        return "Installs pip packages from a requirements file"
    shown = ", ".join(f"`{p}`" for p in pkgs[:3])
    suffix = "" if len(pkgs) <= 3 else f" *and {len(pkgs) - 3} more*"
    return f"Installs Python package(s): {shown}{suffix}"


def _curl_pipe_shell_reason(cmd: str) -> str:
    m = re.search(r"curl\s+[^|]*?(https?://\S+)", cmd)
    url = m.group(1) if m else "a URL"
    return f"Downloads a script from {url} and pipes it directly into a shell"


def _git_commit_reason(cmd: str) -> str:
    m = re.search(r"-m\s+(?:\"([^\"]*)\"|'([^']*)')", cmd)
    msg = (m.group(1) or m.group(2)) if m else None
    if msg:
        shown = msg if len(msg) <= 60 else msg[:57] + "…"
        return f'Creates a commit with message: "{shown}"'
    return "Creates a new commit"


def _docker_reason(cmd: str) -> str:
    m = re.search(r"\bdocker\s+(\w+)\b\s*(.*)", cmd)
    if not m:
        return "Docker operation"
    verb = m.group(1)
    rest = m.group(2).strip()
    if verb == "push" and rest:
        return f"Pushes Docker image to registry: `{rest.split()[0]}`"
    if verb == "build":
        return f"Builds a Docker image{(' from ' + rest.split()[-1]) if rest else ''}"
    if verb == "run":
        return f"Runs a Docker container{(' from image ' + rest.split()[0]) if rest else ''}"
    return f"Docker `{verb}`"


def _deploy_reason(cmd: str) -> str:
    m = re.search(r"\b(fly|heroku|vercel)\s+deploy\b", cmd)
    target = m.group(1) if m else "production"
    return f"Deploys to {target} *— this affects users*"


# ---------- patterns ----------

BLOCKED_PATTERNS: list[tuple[re.Pattern, Reason]] = [
    (re.compile(r"\brm\s+(?:-[rRf]+\s+)+/(?!\S)"), "destructive delete of root /"),
    (re.compile(r"\brm\s+(?:-[rRf]+\s+)+~/?\.ssh\b"), "deletes your SSH keys"),
    (re.compile(r"\.aws/credentials"), "touches AWS credentials file"),
    (re.compile(r"(?:^|\s)cat\s+\S*\.env(?:\s|$)"), "reads a .env file (likely contains secrets)"),
    (re.compile(r":\(\)\s*\{[^}]*\}\s*;\s*:"), "fork bomb"),
]

HIGH_PATTERNS: list[tuple[re.Pattern, Reason]] = [
    (re.compile(r"\bgit\s+push\b"), _git_push_reason),
    (re.compile(r"\bgit\s+reset\s+--hard\b"), _git_reset_hard_reason),
    (re.compile(r"\bgit\s+branch\s+-D\b"), "Force-deletes a local branch (loses unmerged commits)"),
    (re.compile(r"\brm\s+-[rRf]"), _rm_rf_reason),
    (re.compile(r"\bsudo\b"), "Runs with elevated privileges (sudo)"),
    (re.compile(r"\bcurl\s+[^|]*\|\s*(?:sh|bash|zsh)\b"), _curl_pipe_shell_reason),
    (re.compile(r"\bdrop\s+(?:table|database)\b", re.I), "Drops a database table/database (data loss)"),
    (re.compile(r"\b(?:fly|heroku|vercel)\s+deploy\b"), _deploy_reason),
]

MEDIUM_PATTERNS: list[tuple[re.Pattern, Reason]] = [
    (re.compile(r"\b(?:npm|pnpm|yarn)\s+(?:install|add|i)\b"), _npm_install_reason),
    (re.compile(r"\bpip\s+install\b"), _pip_install_reason),
    (re.compile(r"\bcargo\s+(?:add|install)\b"), "Installs a Cargo crate"),
    (re.compile(r"\bgit\s+commit\b"), _git_commit_reason),
    (re.compile(r"\bdocker\s+(?:run|build|push)\b"), _docker_reason),
    (re.compile(r"\bmake\s+(?:install|deploy|publish)\b"), "Runs a make target with side effects"),
]

SENSITIVE_WRITE_PATHS = (".env", ".ssh", "credentials", "id_rsa", ".aws/")


def _resolve_reason(reason: Reason, cmd: str) -> str:
    return reason(cmd) if callable(reason) else reason


def classify_command(command: str) -> Classification:
    cmd = command.strip()
    for pattern, reason in BLOCKED_PATTERNS:
        if pattern.search(cmd):
            return Classification(Risk.BLOCKED, _resolve_reason(reason, cmd))
    for pattern, reason in HIGH_PATTERNS:
        if pattern.search(cmd):
            return Classification(Risk.HIGH, _resolve_reason(reason, cmd))
    for pattern, reason in MEDIUM_PATTERNS:
        if pattern.search(cmd):
            return Classification(Risk.MEDIUM, _resolve_reason(reason, cmd))
    return Classification(Risk.SAFE, "")


def classify_tool_call(tool_name: str, tool_input: dict) -> Classification:
    if tool_name == "Bash":
        return classify_command(tool_input.get("command", ""))
    if tool_name in ("Write", "Edit"):
        path = tool_input.get("file_path", "")
        if any(p in path for p in SENSITIVE_WRITE_PATHS):
            return Classification(Risk.BLOCKED, f"Writes to a sensitive path: `{path}`")
    return Classification(Risk.SAFE, "")
