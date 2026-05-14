from __future__ import annotations
import asyncio
import json
import os
import subprocess
import sys
import time
import tomllib
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .adapters.base import MessagingAdapter
from .risk import Risk, classify_tool_call
from .sessions import ApprovalDecision, SessionState, SessionStore


store = SessionStore()
adapters: list[MessagingAdapter] = []
config: dict = {}

approval_timeout_seconds = 600
stall_threshold_seconds = 240

# Dispatcher mode is enabled by setting AGENTRELAY_MODE=dispatcher in the env
# before booting the server. The CLI's `agentrelay run` does this for you.
# In dispatcher mode we load Slack creds from the OS keychain (via login)
# instead of from config.toml, and we open a websocket to the hosted
# dispatcher to receive button-click callbacks.
_dispatcher_client = None  # set during lifespan if dispatcher mode is on


def load_config() -> dict:
    path = Path(os.environ.get("AGENTRELAY_CONFIG", "config.toml"))
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global config, approval_timeout_seconds, stall_threshold_seconds, _dispatcher_client
    config = load_config()
    approval_timeout_seconds = int(
        config.get("approval_timeout_seconds", approval_timeout_seconds)
    )
    stall_threshold_seconds = int(
        config.get("stall_threshold_seconds", stall_threshold_seconds)
    )

    mode = os.environ.get("AGENTRELAY_MODE", "self-hosted")

    if mode == "dispatcher":
        # Load creds from OS keychain populated by `agentrelay login`. DM the
        # installer directly (channel = their Slack user id) and embed the
        # install_id into button values so the dispatcher can route clicks.
        from .adapters.slack import SlackAdapter
        from .dispatcher_client import DispatcherClient
        from .keychain import load as load_creds

        creds = load_creds()
        if creds is None:
            raise RuntimeError(
                "AGENTRELAY_MODE=dispatcher but no credentials found. "
                "Run `agentrelay login` first."
            )
        adapters.append(
            SlackAdapter(
                bot_token=creds.bot_token,
                default_channel=creds.slack_user_id,
                install_id=creds.install_id,
            )
        )
        _dispatcher_client = DispatcherClient(
            dispatcher_url=creds.dispatcher_url,
            install_id=creds.install_id,
            install_secret=creds.install_secret,
            store=store,
        )
        _dispatcher_client.start()
    elif "slack" in config:
        # Self-hosted mode: load Slack creds from config.toml as before.
        from .adapters.slack import SlackAdapter

        adapters.append(
            SlackAdapter(
                bot_token=config["slack"]["bot_token"],
                default_channel=config["slack"]["channel"],
            )
        )

    stall_task = asyncio.create_task(stall_watcher())
    try:
        yield
    finally:
        stall_task.cancel()
        if _dispatcher_client is not None:
            await _dispatcher_client.stop()


app = FastAPI(lifespan=lifespan, title="AgentRelay")


def check_token(token: str | None) -> None:
    expected = os.environ.get("AGENTRELAY_TOKEN") or config.get("auth_token")
    if not expected:
        return
    if token != expected:
        raise HTTPException(status_code=401, detail="invalid token")


async def broadcast(method: str, *args, **kwargs) -> None:
    for a in adapters:
        try:
            await getattr(a, method)(*args, **kwargs)
        except Exception as e:
            print(f"[adapter:{a.name}] {method} failed: {e}", file=sys.stderr)


async def announce_session_start(sess) -> None:
    """Send the session-started message to every adapter, capturing the Slack
    thread ts on the session so all later messages thread under it."""
    project = Path(sess.project_dir).name
    for a in adapters:
        try:
            ts = await a.send_session_started(sess.id, project, sess.task)
            if a.name == "slack" and ts:
                sess.slack_thread_ts = ts
        except Exception as e:
            print(f"[adapter:{a.name}] send_session_started failed: {e}", file=sys.stderr)


@app.get("/healthz")
async def healthz() -> dict:
    return {
        "ok": True,
        "sessions": len(store.sessions),
        "pending_approvals": sum(
            1 for a in store.approvals.values() if not a.future.done()
        ),
        "adapters": [a.name for a in adapters],
    }


@app.post("/v1/approval")
async def request_approval(
    req: Request,
    x_agentrelay_token: str | None = Header(None),
) -> dict:
    check_token(x_agentrelay_token)
    body = await req.json()
    session_id = body.get("session_id")
    tool_name = body.get("tool_name", "Bash")
    tool_input = body.get("tool_input", {})

    sess = store.get_session(session_id) if session_id else None
    if not sess and session_id:
        # First hook call for a session we didn't spawn (VS Code extension or
        # ad-hoc `claude` invocation). Auto-create a session keyed on Claude
        # Code's own session_id, then send the "session started" DM so all
        # subsequent messages thread under it.
        from .sessions import Session, SessionState

        cwd = body.get("cwd") or os.getcwd()
        sess = Session(
            id=session_id,
            task="(Claude Code session)",
            project_dir=cwd,
            state=SessionState.RUNNING,
        )
        store.sessions[session_id] = sess
        await announce_session_start(sess)
    if not sess:
        # Truly no session id at all — fail open so we never wedge claude.
        return {"decision": "approve", "reason": "no session id, auto-approve"}

    store.touch(session_id)
    classification = classify_tool_call(tool_name, tool_input)

    if classification.risk == Risk.SAFE:
        return {"decision": "approve", "reason": "safe"}
    if classification.risk == Risk.BLOCKED:
        return {"decision": "block", "reason": classification.reason}

    cmd_str = tool_input.get("command") or json.dumps(tool_input)[:400]
    approval = store.new_approval(
        session_id, cmd_str, classification.risk.value, classification.reason
    )
    project = Path(sess.project_dir).name

    await broadcast(
        "send_approval_request",
        approval.id,
        session_id,
        project,
        sess.task,
        cmd_str,
        classification.risk.value,
        classification.reason,
        thread_ts=sess.slack_thread_ts,
    )

    try:
        decision = await asyncio.wait_for(
            approval.future, timeout=approval_timeout_seconds
        )
    except asyncio.TimeoutError:
        return {"decision": "block", "reason": "approval timeout"}

    store.touch(session_id)
    if decision == ApprovalDecision.APPROVE:
        return {"decision": "approve", "reason": "user approved"}
    return {"decision": "block", "reason": "user rejected"}


@app.post("/v1/start")
async def start_session(
    req: Request,
    x_agentrelay_token: str | None = Header(None),
) -> dict:
    check_token(x_agentrelay_token)
    body = await req.json()
    task = body["task"]
    project_dir = (
        body.get("project_dir") or config.get("default_project_dir") or os.getcwd()
    )
    sess = store.new_session(task=task, project_dir=project_dir)
    sess.process = spawn_claude(sess.id, task, project_dir)
    asyncio.create_task(reap_session(sess.id))
    await announce_session_start(sess)
    return {"session_id": sess.id}


def spawn_claude(session_id: str, task: str, project_dir: str) -> subprocess.Popen:
    write_session_settings(project_dir)
    env = {
        **os.environ,
        "AGENTRELAY_SESSION": session_id,
        "AGENTRELAY_URL": os.environ.get("AGENTRELAY_URL", "http://localhost:8000"),
        "AGENTRELAY_TOKEN": os.environ.get("AGENTRELAY_TOKEN", "")
        or config.get("auth_token", ""),
    }
    return subprocess.Popen(
        ["claude", "-p", task],
        cwd=project_dir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def _hook_block() -> dict:
    """The PreToolUse hook block we install — referenced by both
    write_session_settings (project-local) and the wire-hook --global flow."""
    repo_root = Path(__file__).parent.parent
    hook_path = (repo_root / "hook.py").resolve()
    return {
        "matcher": "Bash|Write|Edit",
        "hooks": [
            {
                "type": "command",
                "command": f'"{sys.executable}" "{hook_path}"',
            }
        ],
    }


def write_session_settings(project_dir: str) -> None:
    """Write a project-local .claude/settings.local.json that wires the hook."""
    settings_dir = Path(project_dir) / ".claude"
    settings_dir.mkdir(exist_ok=True)
    settings_path = settings_dir / "settings.local.json"
    settings = {"hooks": {"PreToolUse": [_hook_block()]}}
    settings_path.write_text(json.dumps(settings, indent=2))


def write_global_settings() -> Path:
    """Merge our hook into ~/.claude/settings.json so it fires for every
    Claude Code session on this machine, regardless of project or IDE.

    Preserves any existing user-level hooks/settings — we only append our
    matcher if it isn't already there.
    """
    settings_path = Path.home() / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict = {}
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text())
        except Exception:
            existing = {}

    existing.setdefault("hooks", {})
    pre = existing["hooks"].setdefault("PreToolUse", [])

    block = _hook_block()
    block_cmd = block["hooks"][0]["command"]
    already = any(
        any(h.get("command") == block_cmd for h in m.get("hooks", []))
        for m in pre
    )
    if not already:
        pre.append(block)

    settings_path.write_text(json.dumps(existing, indent=2))
    return settings_path


async def reap_session(session_id: str) -> None:
    sess = store.get_session(session_id)
    if not sess or not sess.process:
        return
    proc = sess.process
    out_chunks: list[str] = []
    loop = asyncio.get_event_loop()

    while True:
        line = await loop.run_in_executor(None, proc.stdout.readline)
        if not line:
            break
        out_chunks.append(line.decode(errors="replace"))
        store.touch(session_id)

    rc = await loop.run_in_executor(None, proc.wait)
    duration = time.time() - sess.started_at
    sess.state = SessionState.COMPLETED if rc == 0 else SessionState.FAILED
    tail = "".join(out_chunks[-20:])[-1500:]
    summary = (
        f"Duration: {int(duration)}s · Approvals: {sess.approval_count} · Exit: {rc}\n"
        f"```\n{tail}\n```"
    )
    await broadcast(
        "send_session_complete",
        session_id,
        sess.task,
        summary,
        thread_ts=sess.slack_thread_ts,
    )


async def stall_watcher() -> None:
    while True:
        try:
            await asyncio.sleep(30)
            now = time.time()
            for sess in list(store.sessions.values()):
                if sess.state != SessionState.RUNNING:
                    continue
                if sess.stall_notified:
                    continue
                age = now - sess.last_activity
                if age >= stall_threshold_seconds:
                    sess.stall_notified = True
                    await broadcast(
                        "send_stall_alert",
                        sess.id,
                        Path(sess.project_dir).name,
                        sess.task,
                        age,
                        thread_ts=sess.slack_thread_ts,
                    )
        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"[stall_watcher] {e}", file=sys.stderr)


@app.post("/v1/slack/interactive")
async def slack_interactive(payload: str = Form(...)) -> dict:
    data = json.loads(payload)
    if data.get("type") != "block_actions":
        return {"ok": True}
    for action in data.get("actions", []):
        value = action.get("value", "")
        if ":" not in value:
            continue
        approval_id, decision_str = value.split(":", 1)
        try:
            decision = ApprovalDecision(decision_str)
        except ValueError:
            continue
        store.resolve_approval(approval_id, decision)
    return {"ok": True}


@app.post("/v1/slack/slash")
async def slack_slash(text: str = Form("")) -> JSONResponse:
    task = text.strip()
    if not task:
        return JSONResponse({"text": "Usage: /relay <task description>"})
    project_dir = config.get("default_project_dir") or os.getcwd()
    sess = store.new_session(task=task, project_dir=project_dir)
    sess.process = spawn_claude(sess.id, task, project_dir)
    asyncio.create_task(reap_session(sess.id))
    asyncio.create_task(announce_session_start(sess))
    return JSONResponse({"text": f":rocket: Started session `{sess.id}` — _{task[:80]}_"})
