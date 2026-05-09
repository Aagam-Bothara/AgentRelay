from __future__ import annotations
import asyncio
import json
import os
import re
import subprocess
import sys
import time
import tomllib
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .adapters.base import MessagingAdapter
from .risk import Risk, classify_tool_call
from .sessions import ApprovalDecision, SessionState, SessionStore


store = SessionStore()
adapters: list[MessagingAdapter] = []
config: dict = {}

approval_timeout_seconds = 600
stall_threshold_seconds = 240
sms_fallback_seconds = 90


def load_config() -> dict:
    path = Path(os.environ.get("AGENTRELAY_CONFIG", "config.toml"))
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global config, approval_timeout_seconds, stall_threshold_seconds, sms_fallback_seconds
    config = load_config()
    approval_timeout_seconds = int(config.get("approval_timeout_seconds", approval_timeout_seconds))
    stall_threshold_seconds = int(config.get("stall_threshold_seconds", stall_threshold_seconds))
    sms_fallback_seconds = int(config.get("sms_fallback_seconds", sms_fallback_seconds))

    if "slack" in config:
        from .adapters.slack import SlackAdapter

        adapters.append(
            SlackAdapter(
                bot_token=config["slack"]["bot_token"],
                default_channel=config["slack"]["channel"],
            )
        )
    if "sms" in config:
        from .adapters.sms import SMSAdapter

        adapters.append(
            SMSAdapter(
                account_sid=config["sms"]["account_sid"],
                auth_token=config["sms"]["auth_token"],
                from_number=config["sms"]["from_number"],
                to_number=config["sms"]["to_number"],
            )
        )

    stall_task = asyncio.create_task(stall_watcher())
    try:
        yield
    finally:
        stall_task.cancel()


app = FastAPI(lifespan=lifespan, title="AgentRelay")


def check_token(token: str | None) -> None:
    expected = os.environ.get("AGENTRELAY_TOKEN") or config.get("auth_token")
    if not expected:
        return
    if token != expected:
        raise HTTPException(status_code=401, detail="invalid token")


async def broadcast(method: str, *args, only: set[str] | None = None, **kwargs) -> None:
    for a in adapters:
        if only and a.name not in only:
            continue
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
        "pending_approvals": sum(1 for a in store.approvals.values() if not a.future.done()),
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
    if not sess:
        # Hook fired for an unknown session — fail open so we never wedge claude.
        return {"decision": "approve", "reason": "unknown session, auto-approve"}

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
        only={"slack"},
    )

    try:
        decision = await asyncio.wait_for(
            _wait_for_approval(approval), timeout=approval_timeout_seconds
        )
    except asyncio.TimeoutError:
        return {"decision": "block", "reason": "approval timeout"}

    store.touch(session_id)
    if decision == ApprovalDecision.APPROVE:
        return {"decision": "approve", "reason": "user approved"}
    return {"decision": "block", "reason": "user rejected"}


async def _wait_for_approval(approval) -> ApprovalDecision:
    """Wait for user response. After sms_fallback_seconds, also page SMS."""
    try:
        return await asyncio.wait_for(
            asyncio.shield(approval.future), timeout=sms_fallback_seconds
        )
    except asyncio.TimeoutError:
        if not approval.notified_sms:
            approval.notified_sms = True
            sess = store.get_session(approval.session_id)
            project = Path(sess.project_dir).name if sess else "?"
            task = sess.task if sess else "(unknown task)"
            await broadcast(
                "send_approval_request",
                approval.id,
                approval.session_id,
                project,
                task,
                approval.command,
                approval.risk,
                approval.reason,
                only={"sms"},
            )
        return await approval.future


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


def write_session_settings(project_dir: str) -> None:
    """Write a project-local .claude/settings.local.json that wires the hook."""
    repo_root = Path(__file__).parent.parent
    hook_path = (repo_root / "hook.py").resolve()
    settings_dir = Path(project_dir) / ".claude"
    settings_dir.mkdir(exist_ok=True)
    settings_path = settings_dir / "settings.local.json"
    settings = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash|Write|Edit",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f'"{sys.executable}" "{hook_path}"',
                        }
                    ],
                }
            ]
        }
    }
    settings_path.write_text(json.dumps(settings, indent=2))


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


SMS_REPLY_RE = re.compile(r"^([AaRr])\s+([A-Za-z0-9]+)\s*$")
TWIML_OK = "<?xml version='1.0' encoding='UTF-8'?><Response/>"


@app.post("/v1/sms/incoming", response_class=PlainTextResponse)
async def sms_incoming(Body: str = Form(""), From: str = Form("")) -> str:
    body = Body.strip()
    m = SMS_REPLY_RE.match(body)
    if not m:
        return TWIML_OK
    letter = m.group(1).upper()
    approval_id = m.group(2)
    decision = ApprovalDecision.APPROVE if letter == "A" else ApprovalDecision.REJECT
    store.resolve_approval(approval_id, decision)
    return TWIML_OK


def main() -> None:
    import uvicorn

    uvicorn.run(
        "agentrelay.server:app",
        host=os.environ.get("AGENTRELAY_HOST", "0.0.0.0"),
        port=int(os.environ.get("AGENTRELAY_PORT", "8000")),
    )
