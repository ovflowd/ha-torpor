"""The WebSocket endpoint PCs dial: limits, the local check, both handshakes and the hand-off.

The order follows docs/protocol.md in ovflowd/torpor, "What Home Assistant does, in order".
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Awaitable, Callable
from ipaddress import ip_address
from typing import TYPE_CHECKING, Any

from aiohttp import WSMsgType, web
from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.components.http.ban import process_wrong_login
from homeassistant.config_entries import SOURCE_IGNORE, ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.network import is_cloud_connection
from homeassistant.loader import async_get_integration
from homeassistant.util.network import is_local

from . import protocol
from .const import DATA_KEY, DOMAIN, LOGGER
from .data import DATA, TorporData
from .options import Settings
from .protocol import HANDSHAKE_TIMEOUT_S, STALE_KEY_REPAIR_AFTER, Close, ProtocolError
from .repairs import clear_auth_issues, raise_protocol_mismatch, raise_stale_key
from .transport import AgentSocket

if TYPE_CHECKING:
    from .pairing import PendingPairing

STALE_WINDOW_S = 3600
MAX_PROTO = 999

Handoff = Callable[[], Awaitable[None]]


async def async_ensure_view(hass: HomeAssistant) -> TorporData:
    """Register the view once. Views can't be unregistered, so it must answer cleanly with no entries."""
    data = hass.data.get(DATA)
    if data is None:
        integration = await async_get_integration(hass, DOMAIN)
        data = hass.data.setdefault(DATA, TorporData(integration_version=str(integration.version or "")))
    if not data.view_registered:
        hass.http.register_view(TorporView(data))
        data.view_registered = True
    return data


@callback
def entry_for_agent(hass: HomeAssistant, agent_id: str) -> ConfigEntry | None:
    entry = hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, agent_id)
    if entry is None or entry.source == SOURCE_IGNORE:
        return None
    return entry


def _local(request: web.Request, cloud: bool) -> bool:
    if cloud or not request.remote:
        return False
    try:
        address = ip_address(request.remote)
    except ValueError:
        return False
    return is_local(address)


async def _close(ws: web.WebSocketResponse, code: int, reason: str) -> None:
    if not ws.closed:
        await ws.close(code=int(code), message=reason.encode())


async def _receive_text(ws: web.WebSocketResponse) -> str | None:
    """A handshake message. None when the PC closed the socket; MALFORMED for anything but text."""
    message = await ws.receive()
    if message.type is WSMsgType.TEXT:
        return str(message.data)
    if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
        return None
    raise ProtocolError(Close.MALFORMED, "only text messages before auth_ok")


class TorporView(HomeAssistantView):
    url = protocol.WS_PATH
    name = "api:torpor:ws"
    requires_auth = False

    def __init__(self, data: TorporData) -> None:
        self._data = data

    async def get(self, request: web.Request) -> web.StreamResponse:
        hass = request.app[KEY_HASS]
        data = self._data
        if not data.limiter.allow(request.remote or ""):
            return web.Response(status=429, text="too many connections")
        if data.pending_handshakes >= data.max_pending:
            return web.Response(status=503, text="too many pending handshakes")
        cloud = is_cloud_connection(hass)
        ws = web.WebSocketResponse(max_msg_size=protocol.MAX_FRAME)
        data.pending_handshakes += 1
        try:
            await ws.prepare(request)
            handoff = await _Handshake(hass, data, request, ws, cloud).run()
        finally:
            data.pending_handshakes -= 1
        if handoff is not None:
            await handoff()
        return ws


class _Handshake:
    """One connection's way from the upgrade to an authenticated socket, or to its close code."""

    def __init__(
        self, hass: HomeAssistant, data: TorporData, request: web.Request, ws: web.WebSocketResponse, cloud: bool
    ) -> None:
        self._hass = hass
        self._data = data
        self._request = request
        self._ws = ws
        self._cloud = cloud
        self._agent_id: str | None = None

    def _count(self, outcome: str) -> None:
        """Count per paired PC only, so made-up agent IDs can't grow this."""
        if self._agent_id is not None and entry_for_agent(self._hass, self._agent_id) is not None:
            self._data.handshakes[self._agent_id][outcome] += 1

    async def run(self) -> Handoff | None:
        try:
            async with asyncio.timeout(HANDSHAKE_TIMEOUT_S):
                handoff = await self._authenticate()
            return handoff
        except TimeoutError:
            self._count("timeout")
            await _close(self._ws, Close.TIMEOUT, "handshake timeout")
        except ProtocolError as error:
            self._count(error.close.name.lower())
            LOGGER.debug("Handshake from %s failed: %s", self._request.remote, error.reason)
            await _close(self._ws, error.close, error.reason)
        return None

    async def _authenticate(self) -> Handoff | None:
        text = await _receive_text(self._ws)
        if text is None:
            return None
        try:
            hello = protocol.parse_hello(text)
        except ProtocolError as error:
            if error.close is Close.UNSUPPORTED:
                self._note_mismatch(text)
            raise
        self._agent_id = hello.agent_id
        entry = None if hello.pairing else entry_for_agent(self._hass, hello.agent_id)
        if not self._admitted(entry):
            raise ProtocolError(Close.NOT_LOCAL, "only local agents may connect")
        pending: PendingPairing | None = None
        secret: bytes | None = None
        if hello.pairing:
            pending = self._data.pairings.get(hello.pair_id)
            if pending is not None and pending.claimable:
                secret = pending.code
            else:
                pending = None
        elif entry is not None:
            secret = base64.b64decode(entry.data[DATA_KEY])
        handshake = protocol.HomeAssistantHandshake(hello, ephemeral=protocol.Ephemeral.generate(), secret=secret)
        await self._ws.send_str(handshake.challenge())
        auth = await _receive_text(self._ws)
        if auth is None:
            self._note_close(entry, handshake)
            return None
        try:
            session = handshake.verify(auth)
        except ProtocolError as error:
            await self._wrong_proof(error, pending)
            raise
        if pending is not None:
            if not pending.claim(hello, handshake):
                raise ProtocolError(Close.PAIRING_CANCELLED, "this pairing is taken or over")
            self._count("paired")
            sock = AgentSocket(self._ws, session, pairing=True)
            return lambda: pending.run(sock)
        assert entry is not None
        self._data.stale_closes.pop(hello.agent_id, None)
        clear_auth_issues(self._hass, entry)
        if entry.state is not ConfigEntryState.LOADED:
            raise ProtocolError(Close.NOT_READY, "the entry for this PC is not loaded")
        self._count("ok")
        sock = AgentSocket(self._ws, session, pairing=False)
        runtime = entry.runtime_data
        return lambda: runtime.async_run(sock)

    def _admitted(self, entry: ConfigEntry | None) -> bool:
        """Local connections, and remote ones for a paired PC whose options allow them."""
        if _local(self._request, self._cloud):
            return True
        return entry is not None and Settings.from_options(entry.options).allow_remote

    async def _wrong_proof(self, error: ProtocolError, pending: PendingPairing | None) -> None:
        if error.ban:
            LOGGER.warning("A connection from %s sent a wrong proof for a paired PC", self._request.remote)
            await process_wrong_login(self._request)
        if pending is not None and pending.wrong_proof():
            error.close = Close.PAIRING_CANCELLED
            error.reason = "pairing code burned"

    def _note_close(self, entry: ConfigEntry | None, handshake: protocol.HomeAssistantHandshake) -> None:
        """The PC hung up instead of answering. With 4401 for a real challenge, it holds an old key.

        Anyone who knows the agent_id can hang up like that, so it counts toward the repair only while the PC is away.
        """
        if entry is None or handshake.decoy or self._ws.close_code != Close.AUTH_FAILED:
            self._count("closed")
            return
        self._count("stale_key")
        if entry.state is ConfigEntryState.LOADED and entry.runtime_data.connected:
            return
        now = time.monotonic()
        closes = self._data.stale_closes[handshake.hello.agent_id]
        while closes and now - closes[0] >= STALE_WINDOW_S:
            closes.popleft()
        closes.append(now)
        if len(closes) >= STALE_KEY_REPAIR_AFTER:
            raise_stale_key(self._hass, entry)

    def _note_mismatch(self, text: str) -> None:
        """parse_hello already read this text as a JSON object; only its proto was wrong.

        The hello is unauthenticated, so only a whole version number reaches the repair's text.
        """
        message: dict[str, Any] = json.loads(text)
        agent_id = message.get("agent_id")
        if not isinstance(agent_id, str) or (entry := entry_for_agent(self._hass, agent_id)) is None:
            return
        self._agent_id = agent_id
        proto = message.get("proto")
        if not isinstance(proto, int) or isinstance(proto, bool) or not 0 <= proto <= MAX_PROTO:
            return
        if self._admitted(entry):
            raise_protocol_mismatch(self._hass, entry, str(proto))
