from __future__ import annotations
from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class MessagingAdapter(Protocol):
    name: str

    async def send_session_started(
        self, session_id: str, project: str, task: str
    ) -> Optional[str]:
        """Post the session-start message. Returns a thread token (Slack ts) or None."""
        ...

    async def send_approval_request(
        self,
        approval_id: str,
        session_id: str,
        project: str,
        task: str,
        command: str,
        risk: str,
        reason: str,
        thread_ts: Optional[str] = None,
    ) -> None: ...

    async def send_stall_alert(
        self,
        session_id: str,
        project: str,
        task: str,
        last_activity_age: float,
        thread_ts: Optional[str] = None,
    ) -> None: ...

    async def send_session_complete(
        self,
        session_id: str,
        task: str,
        summary: str,
        thread_ts: Optional[str] = None,
    ) -> None: ...

    async def send_message(self, text: str, thread_ts: Optional[str] = None) -> None: ...
