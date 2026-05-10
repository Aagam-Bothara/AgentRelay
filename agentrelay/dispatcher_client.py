"""Websocket client to the AgentRelay dispatcher.

Maintains a persistent connection to the hosted dispatcher. The dispatcher
sends one JSON message per inbound Slack button click; we resolve the
matching pending approval locally.

Reconnect strategy:
  - If the connection drops, we retry with exponential backoff capped at 30s.
  - The dispatcher buffers up to 50 pending actions per install, so brief
    disconnects don't lose decisions.
"""
from __future__ import annotations
import asyncio
import json
import sys
from typing import Optional

import websockets
from websockets.exceptions import ConnectionClosed

from .sessions import ApprovalDecision, SessionStore


class DispatcherClient:
    def __init__(
        self,
        dispatcher_url: str,
        install_id: str,
        install_secret: str,
        store: SessionStore,
    ) -> None:
        # dispatcher_url is https://...; the websocket endpoint is wss://.../ws/<install_id>
        ws_base = dispatcher_url.rstrip("/").replace("https://", "wss://").replace(
            "http://", "ws://"
        )
        self.url = f"{ws_base}/ws/{install_id}"
        self.headers = {"Authorization": f"Bearer {install_secret}"}
        self.store = store
        self._task: Optional[asyncio.Task] = None
        self._stopped = asyncio.Event()

    async def _handle_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except Exception as e:
            print(f"[dispatcher] malformed message: {e}", file=sys.stderr)
            return
        if msg.get("type") != "approval":
            return
        approval_id = msg.get("approval_id")
        decision = msg.get("decision")
        if not approval_id or decision not in ("approve", "reject"):
            return
        self.store.resolve_approval(approval_id, ApprovalDecision(decision))

    async def _run_one_connection(self) -> None:
        async with websockets.connect(
            self.url, additional_headers=self.headers, ping_interval=30, ping_timeout=10
        ) as ws:
            print(f"[dispatcher] connected: {self.url}", file=sys.stderr)
            async for raw in ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                await self._handle_message(raw)

    async def _loop(self) -> None:
        backoff = 1.0
        while not self._stopped.is_set():
            try:
                await self._run_one_connection()
                # Clean close — restart immediately.
                backoff = 1.0
            except asyncio.CancelledError:
                return
            except ConnectionClosed:
                pass
            except Exception as e:
                print(f"[dispatcher] connection error: {e}", file=sys.stderr)
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=backoff)
                return
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 30.0)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
