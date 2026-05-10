from __future__ import annotations
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SessionState(str, Enum):
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    KILLED = "killed"


class ApprovalDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"


@dataclass
class Approval:
    id: str
    session_id: str
    command: str
    risk: str
    reason: str
    future: asyncio.Future
    created_at: float = field(default_factory=time.time)


@dataclass
class Session:
    id: str
    task: str
    project_dir: str
    state: SessionState = SessionState.RUNNING
    process: Optional[object] = None
    last_activity: float = field(default_factory=time.time)
    started_at: float = field(default_factory=time.time)
    approval_count: int = 0
    stall_notified: bool = False
    # Slack message ts of the "Started" post — used to thread all subsequent
    # session messages, so concurrent sessions don't pollute the channel.
    slack_thread_ts: Optional[str] = None


class SessionStore:
    """In-memory store. Restart loses state — acceptable for MVP."""

    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self.approvals: dict[str, Approval] = {}

    def new_session(self, task: str, project_dir: str) -> Session:
        sid = uuid.uuid4().hex[:8]
        sess = Session(id=sid, task=task, project_dir=project_dir)
        self.sessions[sid] = sess
        return sess

    def get_session(self, sid: str) -> Optional[Session]:
        return self.sessions.get(sid)

    def touch(self, sid: str) -> None:
        sess = self.sessions.get(sid)
        if sess:
            sess.last_activity = time.time()
            sess.stall_notified = False

    def new_approval(self, session_id: str, command: str, risk: str, reason: str) -> Approval:
        approval_id = uuid.uuid4().hex[:8]
        loop = asyncio.get_event_loop()
        approval = Approval(
            id=approval_id,
            session_id=session_id,
            command=command,
            risk=risk,
            reason=reason,
            future=loop.create_future(),
        )
        self.approvals[approval_id] = approval
        sess = self.sessions.get(session_id)
        if sess:
            sess.state = SessionState.WAITING_APPROVAL
            sess.approval_count += 1
        return approval

    def resolve_approval(self, approval_id: str, decision: ApprovalDecision) -> bool:
        approval = self.approvals.get(approval_id)
        if not approval or approval.future.done():
            return False
        approval.future.set_result(decision)
        sess = self.sessions.get(approval.session_id)
        if sess:
            sess.state = (
                SessionState.RUNNING
                if decision == ApprovalDecision.APPROVE
                else SessionState.PAUSED
            )
            sess.last_activity = time.time()
        return True
