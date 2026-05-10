from __future__ import annotations
from typing import Optional

import httpx


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


class SlackAdapter:
    name = "slack"

    def __init__(
        self,
        bot_token: str,
        default_channel: str,
        install_id: str = "",
    ) -> None:
        # default_channel can be a channel ID (C...) or a user ID (U...) for
        # direct DMs — chat.postMessage accepts either with `im:write` scope.
        # install_id is set in dispatcher mode and gets embedded into button
        # values so the hosted dispatcher can route callbacks. Empty string
        # means self-hosted mode (the local /v1/slack/interactive parses the
        # 2-part legacy format).
        self.channel = default_channel
        self.install_id = install_id
        self._client = httpx.AsyncClient(
            base_url="https://slack.com/api",
            headers={"Authorization": f"Bearer {bot_token}"},
            timeout=10.0,
        )

    async def _post(self, method: str, payload: dict) -> dict:
        r = await self._client.post(f"/{method}", json=payload)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Slack {method} failed: {data}")
        return data

    async def _post_message(
        self,
        text: str,
        blocks: Optional[list] = None,
        thread_ts: Optional[str] = None,
    ) -> str:
        payload: dict = {"channel": self.channel, "text": text}
        if blocks is not None:
            payload["blocks"] = blocks
        if thread_ts:
            payload["thread_ts"] = thread_ts
        data = await self._post("chat.postMessage", payload)
        return data.get("ts", "")

    async def send_message(self, text: str, thread_ts: Optional[str] = None) -> None:
        await self._post_message(text, thread_ts=thread_ts)

    async def send_session_started(
        self, session_id: str, project: str, task: str
    ) -> Optional[str]:
        # The ts we return becomes the thread root for all subsequent messages
        # tied to this session — that's how concurrent sessions stay legible.
        text = (
            f":rocket: Session `{session_id}` started — *{project}*\n"
            f"> {_truncate(task, 500)}"
        )
        return await self._post_message(text)

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
        emoji = {"MEDIUM": ":warning:", "HIGH": ":no_entry:"}.get(risk, ":grey_question:")
        shown = _truncate(command, 400)
        header = (
            f"{emoji} *{risk} risk* — approve?\n"
            f"*Task:* {_truncate(task, 200)}\n"
            f"*Session:* `{session_id}` · {project}\n"
            f"*Why:* {reason}\n"
            f"```\n{shown}\n```"
        )
        if self.install_id:
            approve_value = f"{self.install_id}:{approval_id}:approve"
            reject_value = f"{self.install_id}:{approval_id}:reject"
        else:
            approve_value = f"{approval_id}:approve"
            reject_value = f"{approval_id}:reject"
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": header}},
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✅ Approve"},
                        "style": "primary",
                        "value": approve_value,
                        "action_id": "approval_approve",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "❌ Reject"},
                        "style": "danger",
                        "value": reject_value,
                        "action_id": "approval_reject",
                    },
                ],
            },
        ]
        await self._post_message(
            text=f"{risk} risk approval — {_truncate(task, 80)} ({session_id})",
            blocks=blocks,
            thread_ts=thread_ts,
        )

    async def send_stall_alert(
        self,
        session_id: str,
        project: str,
        task: str,
        last_activity_age: float,
        thread_ts: Optional[str] = None,
    ) -> None:
        mins = int(last_activity_age / 60)
        text = (
            f":thinking_face: Session `{session_id}` may be stuck — *{mins}m* idle.\n"
            f"*Task:* {_truncate(task, 200)} · *Project:* {project}"
        )
        await self._post_message(text, thread_ts=thread_ts)

    async def send_session_complete(
        self,
        session_id: str,
        task: str,
        summary: str,
        thread_ts: Optional[str] = None,
    ) -> None:
        text = (
            f":white_check_mark: Session `{session_id}` complete — "
            f"_{_truncate(task, 200)}_\n{summary}"
        )
        await self._post_message(text, thread_ts=thread_ts)
