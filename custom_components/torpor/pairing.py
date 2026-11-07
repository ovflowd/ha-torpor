"""A pairing in progress: the one-time code, the PC that proved it, and the paired/ack hand-off."""

from __future__ import annotations

import asyncio
import itertools
from enum import StrEnum
from typing import Any

from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant

from . import protocol
from .const import LOGGER
from .data import DATA
from .protocol import (
    PAIRED_ACK_TIMEOUT_S,
    PAIRING_MAX_FAILURES,
    PAIRING_TIMEOUT_S,
    PING_INTERVAL_S,
    Close,
    ProtocolError,
)
from .transport import AgentSocket

PAIRED_ID = "p1"


class Outcome(StrEnum):
    READY = "ready"
    TIMEOUT = "pairing_timeout"
    FAILED = "pairing_failed"
    LOST = "pc_disconnected"


class PendingPairing:
    """Lives from the flow's pair step until the flow ends. The code is never stored anywhere else."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._loop = hass.loop
        self.code = protocol.new_code()
        self.pair_id = protocol.pair_id(self.code)
        self.text = protocol.format_code(self.code)
        self.deadline = self._loop.time() + PAIRING_TIMEOUT_S
        self.failures = 0
        self.claimed = False
        self.expired = False
        self.hello: protocol.Hello | None = None
        self.key: bytes | None = None
        self.fingerprint: str | None = None
        self.capabilities: dict[str, Any] | None = None
        self._socket: AgentSocket | None = None
        self._ready: asyncio.Future[Outcome] = self._loop.create_future()
        self._ack: asyncio.Future[bool] | None = None
        hass.data[DATA].pairings[self.pair_id] = self

    @property
    def claimable(self) -> bool:
        registered = self._hass.data[DATA].pairings.get(self.pair_id) is self
        return registered and not self.claimed and not self._ready.done() and self._loop.time() < self.deadline

    async def wait(self) -> Outcome:
        """The flow's progress task: the PC authenticated and sent its capabilities, or why not."""
        try:
            async with asyncio.timeout_at(self.deadline):
                await asyncio.shield(self._ready)
        except TimeoutError:
            self.expired = True
            self._finish(Outcome.TIMEOUT)
        return self._ready.result()

    def wrong_proof(self) -> bool:
        """Count a wrong pairing proof. True when that burned the code."""
        self.failures += 1
        if self.failures < PAIRING_MAX_FAILURES:
            return False
        self._finish(Outcome.FAILED)
        self._forget()
        return True

    def claim(self, hello: protocol.Hello, handshake: protocol.HomeAssistantHandshake) -> bool:
        """Take the pairing after a good proof. False when another PC that knew the code took it first, or it ended."""
        if not self.claimable:
            return False
        self.claimed = True
        self.hello = hello
        self.key = handshake.pairing_key
        self.fingerprint = handshake.fingerprint
        return True

    async def run(self, sock: AgentSocket) -> None:
        """Hold the PC's pairing socket until the flow ends it. Called by the endpoint after `pair_auth`."""
        self._socket = sock
        keepalive = self._hass.async_create_background_task(self._keepalive(sock), "torpor pairing keepalive")
        try:
            version = self._hass.data[DATA].integration_version
            await sock.send({"type": "auth_ok", "pairing": True, "ha_version": HA_VERSION, "integration": version})
            async with asyncio.timeout_at(self.deadline):
                await self._read(sock)
        except TimeoutError:
            self.expired = True
            await sock.close(Close.TIMEOUT, "pairing took too long")
        except ProtocolError as error:
            await sock.close(error.close, error.reason)
        except ConnectionResetError:
            pass
        except Exception:
            LOGGER.exception("Pairing %s failed", self.pair_id)
            await sock.close(Close.INTERNAL, "internal error")
        finally:
            keepalive.cancel()
            self._finish(Outcome.LOST)
            if self._ack is not None and not self._ack.done():
                self._ack.set_result(False)

    async def _keepalive(self, sock: AgentSocket) -> None:
        """Ping, so the PC's pongs keep the socket inside its idle timeout while the user fills in the form."""
        for number in itertools.count(1):
            await asyncio.sleep(PING_INTERVAL_S)
            try:
                await sock.send({"type": "ping", "t": number})
            except ConnectionResetError:
                return

    async def _read(self, sock: AgentSocket) -> None:
        while True:
            message = await sock.receive()
            if message is None:
                return
            kind = message["type"]
            if kind == "capabilities":
                self.capabilities = {key: value for key, value in message.items() if key != "type"}
                self._finish(Outcome.READY)
            elif kind == "ping":
                await sock.send({"type": "pong", "t": message["t"]})
            elif kind == "ack" and message["id"] == PAIRED_ID and self._ack is not None and not self._ack.done():
                self._ack.set_result(True)

    async def confirm(self) -> bool:
        """Send `paired` and wait for the PC's ack. The PC writes its key only now."""
        sock = self._socket
        if sock is None or sock.closed:
            return False
        self._ack = self._loop.create_future()
        try:
            await sock.send({"type": "paired", "id": PAIRED_ID})
            async with asyncio.timeout(PAIRED_ACK_TIMEOUT_S):
                acked = await self._ack
        except TimeoutError, ConnectionResetError:
            return False
        return acked

    async def close(self, code: int, reason: str) -> None:
        self._forget()
        self._finish(Outcome.LOST)
        if self._socket is not None:
            await self._socket.close(code, reason)

    def _finish(self, outcome: Outcome) -> None:
        if not self._ready.done():
            self._ready.set_result(outcome)

    def _forget(self) -> None:
        pairings = self._hass.data[DATA].pairings
        if pairings.get(self.pair_id) is self:
            del pairings[self.pair_id]
            LOGGER.debug("Pairing %s ended", self.pair_id)
