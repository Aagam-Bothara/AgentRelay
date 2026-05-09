from __future__ import annotations
import asyncio
from typing import Optional

from twilio.rest import Client


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


class SMSAdapter:
    name = "sms"

    def __init__(
        self, account_sid: str, auth_token: str, from_number: str, to_number: str
    ) -> None:
        self.client = Client(account_sid, auth_token)
        self.from_number = from_number
        self.to_number = to_number

    def _send_sync(self, body: str) -> None:
        # Twilio SMS body cap; trim defensively.
        self.client.messages.create(
            body=body[:1500], from_=self.from_number, to=self.to_number
        )

    async def send_message(self, text: str, thread_ts: Optional[str] = None) -> None:
        # SMS has no threading concept; thread_ts is ignored.
        await asyncio.to_thread(self._send_sync, text)

    async def send_session_started(
        self, session_id: str, project: str, task: str
    ) -> Optional[str]:
        # Don't burn an SMS on every session start. Slack handles kickoff.
        return None

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
    ) -> None:
        # Self-contained: task name + short id, since SMS has no threading.
        body = (
            f"AgentRelay {risk}: \"{_truncate(task, 80)}\" ({session_id}) wants:\n"
            f"{_truncate(command, 200)}\n"
            f"Reply 'A {approval_id}' to approve, 'R {approval_id}' to reject."
        )
        await self.send_message(body)

    async def send_stall_alert(
        self,
        session_id: str,
        project: str,
        task: str,
        last_activity_age: float,
        thread_ts: Optional[str] = None,
    ) -> None:
        mins = int(last_activity_age / 60)
        await self.send_message(
            f"AgentRelay: \"{_truncate(task, 80)}\" ({session_id}) stalled — {mins}m idle."
        )

    async def send_session_complete(
        self,
        session_id: str,
        task: str,
        summary: str,
        thread_ts: Optional[str] = None,
    ) -> None:
        first_line = summary.splitlines()[0] if summary else ""
        await self.send_message(
            f"AgentRelay: \"{_truncate(task, 60)}\" ({session_id}) done. {first_line}"
        )
