"""One authenticated WebSocket to a PC, with its encrypted frame layer and receive limits."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

from aiohttp import WSMsgType, web

from . import protocol
from .protocol import AGENT_MESSAGES_PER_WINDOW, AGENT_MESSAGES_WINDOW_S, IDLE_TIMEOUT_S, Close, ProtocolError


class AgentSocket:
    def __init__(self, ws: web.WebSocketResponse, session: protocol.Session, *, pairing: bool) -> None:
        self._ws = ws
        self._session = session
        self.pairing = pairing
        self._send_lock = asyncio.Lock()
        self._received: deque[float] = deque()
        self.close_code: int | None = None
        self.close_reason = ""

    @property
    def closed(self) -> bool:
        return self._ws.closed

    async def send(self, message: dict[str, Any]) -> None:
        async with self._send_lock:
            if self._ws.closed:
                raise ConnectionResetError("the PC's socket is closed")
            frame = self._session.seal(message)
            await self._ws.send_bytes(frame)

    async def receive(self) -> dict[str, Any] | None:
        """The next checked message from the PC, or None once the socket is closed."""
        try:
            message = await self._ws.receive(timeout=IDLE_TIMEOUT_S)
        except TimeoutError as error:
            raise ProtocolError(Close.TIMEOUT, f"no frame for {IDLE_TIMEOUT_S} s") from error
        if message.type is WSMsgType.BINARY:
            self._count()
            return protocol.check(self._session.open(message.data), "agent", pairing=self.pairing)
        if message.type is WSMsgType.TEXT:
            raise ProtocolError(Close.MALFORMED, "text message after the handshake")
        if self.close_code is None:
            self.close_code = self._ws.close_code
            self.close_reason = message.extra if isinstance(message.extra, str) else ""
        return None

    def _count(self) -> None:
        now = time.monotonic()
        while self._received and now - self._received[0] >= AGENT_MESSAGES_WINDOW_S:
            self._received.popleft()
        self._received.append(now)
        if len(self._received) > AGENT_MESSAGES_PER_WINDOW:
            raise ProtocolError(Close.RATE_LIMITED, "too many messages")

    async def close(self, code: int, reason: str) -> None:
        if self.close_code is None:
            self.close_code = int(code)
            self.close_reason = reason
        if not self._ws.closed:
            await self._ws.close(code=int(code), message=reason.encode())
