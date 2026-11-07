"""One paired PC as Home Assistant sees it: the live socket, the derived state, commands and pushes."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from aiohttp import WSCloseCode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.util import dt as dt_util

from . import protocol
from .capabilities import Capabilities
from .const import (
    BOOT_WINDOW_S,
    DATA_CAPABILITIES,
    DATA_CAPABILITIES_AT,
    DOMAIN,
    EVENT_BOOTED,
    EVENT_RESTARTING,
    EVENT_RESUMED,
    EVENT_SHUTTING_DOWN,
    EVENT_SUSPENDING,
    EVENT_WAKE_FAILED,
    EVENT_WAKE_SUCCEEDED,
    LOGGER,
    RESTARTING_LIMIT_S,
    STATE_ASLEEP,
    STATE_GRACE_S,
    STATE_OFF,
    STATE_RESTARTING,
    WAKE_ALREADY_AWAKE,
    WAKE_AWAKE,
    WAKE_SENT,
    WAKE_TIMEOUT,
)
from .data import DATA, TorporStore
from .entity import expected_unique_ids
from .options import Settings
from .protocol import COMMAND_TIMEOUT_S, INPUT_MIN_INTERVAL_S, Close, ProtocolError
from .repairs import check_capabilities
from .transport import AgentSocket
from .wake import async_send_magic_packets

type TorporConfigEntry = ConfigEntry[TorporConnection]

POWER_COMMANDS = frozenset({"suspend", "hibernate", "poweroff", "reboot"})
GOODBYE_S = 1
_GOING_DOWN = {
    "sleep": (STATE_ASLEEP, EVENT_SUSPENDING),
    "poweroff": (STATE_OFF, EVENT_SHUTTING_DOWN),
    "unknown": (STATE_OFF, EVENT_SHUTTING_DOWN),
    "reboot": (STATE_RESTARTING, EVENT_RESTARTING),
}


@dataclass
class ConnectionStats:
    connected_at: datetime | None = None
    disconnected_at: datetime | None = None
    close_code: int | None = None
    close_reason: str = ""
    sessions: int = 0


def command_error(code: str, message: str = "") -> HomeAssistantError:
    key = code if code in protocol.ERRORS or code in ("timeout", "not_connected") else "failed"
    return HomeAssistantError(
        translation_domain=DOMAIN, translation_key=f"error_{key}", translation_placeholders={"message": message}
    )


def _lux(state: State | None) -> float | None:
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    try:
        value = float(state.state)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _epoch(value: object) -> int | None:
    """A Unix time a datetime can hold, or None."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    try:
        dt_util.utc_from_timestamp(value)
    except OverflowError, ValueError, OSError:
        return None
    return value


class TorporConnection:
    def __init__(self, hass: HomeAssistant, entry: TorporConfigEntry, store: TorporStore) -> None:
        self.hass = hass
        self.entry = entry
        self.store = store
        self.agent_id: str = entry.data["agent_id"]
        self.settings = Settings.from_options(entry.options)
        self.capabilities = Capabilities(entry.data.get(DATA_CAPABILITIES))
        self.state: dict[str, Any] = {}
        self.state_at: datetime | None = None
        self.ha_state: str | None = None
        self.going_down: str | None = None
        self.stats = ConnectionStats()
        self._socket: AgentSocket | None = None
        self._configured = False
        self._listeners: list[CALLBACK_TYPE] = []
        self._capability_listeners: list[CALLBACK_TYPE] = []
        self._event_listeners: list[Callable[[str], None]] = []
        self._pending: dict[str, tuple[asyncio.Future[dict[str, Any]], str]] = {}
        self._ids = itertools.count(1)
        self._unknown_states: set[str] = set()
        self._grace: CALLBACK_TYPE | None = None
        self._lux_sent_at: datetime | None = None
        self._lux_timer: CALLBACK_TYPE | None = None
        self._wake: asyncio.Future[str] | None = None
        self._stopping = False

    @property
    def name(self) -> str:
        return self.entry.title

    @property
    def connected(self) -> bool:
        return self._socket is not None

    @property
    def up(self) -> bool:
        """Connected and not on its way down: controls work."""
        return self._socket is not None and self.going_down is None

    @property
    def boot_time(self) -> int | None:
        value = _epoch(self.state.get("boot_time"))
        return value if value is not None else self.store.boot_time

    def section(self, name: str) -> dict[str, Any]:
        value = self.state.get(name)
        return value if isinstance(value, dict) else {}

    def display(self, display_id: str) -> dict[str, Any] | None:
        displays = self.section("brightness").get("displays")
        value = displays.get(display_id) if isinstance(displays, dict) else None
        return value if isinstance(value, dict) else None

    @callback
    def async_add_listener(self, listener: CALLBACK_TYPE) -> CALLBACK_TYPE:
        return self._subscribe(self._listeners, listener)

    @callback
    def async_add_capability_listener(self, listener: CALLBACK_TYPE) -> CALLBACK_TYPE:
        return self._subscribe(self._capability_listeners, listener)

    @callback
    def async_add_event_listener(self, listener: Callable[[str], None]) -> CALLBACK_TYPE:
        return self._subscribe(self._event_listeners, listener)

    @staticmethod
    def _subscribe[T](listeners: list[T], listener: T) -> CALLBACK_TYPE:
        listeners.append(listener)

        @callback
        def remove() -> None:
            listeners.remove(listener)

        return remove

    @callback
    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    @callback
    def _fire(self, event_type: str) -> None:
        for listener in list(self._event_listeners):
            listener(event_type)

    @callback
    def async_start(self) -> None:
        self.entry.async_on_unload(self.hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, self._on_hass_stop))
        if self.settings.lux_entity and self.settings.module_on("brightness"):
            self.entry.async_on_unload(
                async_track_state_change_event(self.hass, [self.settings.lux_entity], self._on_lux_change)
            )
        check_capabilities(self.hass, self.entry, self.capabilities, self.settings)

    @callback
    def _on_hass_stop(self, _event: Event) -> None:
        """Close the PC's socket first: a stopping web server ignores what the PC sends and waits 10 s for it."""
        self.entry.async_create_task(
            self.hass, self._shut_down(WSCloseCode.GOING_AWAY, "Home Assistant is stopping"), "torpor stop"
        )

    async def async_stop(self) -> None:
        await self._shut_down(Close.RECONNECT, "integration reloaded")

    async def _shut_down(self, code: int, reason: str) -> None:
        self._stopping = True
        for unsub in (self._grace, self._lux_timer):
            if unsub is not None:
                unsub()
        self._grace = self._lux_timer = None
        if self._wake is not None and not self._wake.done():
            self._wake.set_result(WAKE_TIMEOUT)
        sock = self._socket
        if sock is None:
            return
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(GOODBYE_S):
                await sock.close(code, reason)

    @callback
    def restore(self, value: str | None) -> None:
        """The state sensor's last value after a Home Assistant restart, while the PC hasn't connected."""
        if self.connected or self.ha_state is not None or value is None:
            return
        self.ha_state = value
        if value in protocol.STATES:
            self._start_grace(STATE_GRACE_S)
        elif value == STATE_RESTARTING:
            self._start_grace(RESTARTING_LIMIT_S)

    async def async_run(self, sock: AgentSocket) -> None:
        """Serve one authenticated session socket until it closes. Called by the endpoint."""
        previous = self._socket
        self._attach(sock)
        if previous is not None:
            await previous.close(Close.SUPERSEDED, "another connection for this PC authenticated")
        try:
            await sock.send(
                {
                    "type": "auth_ok",
                    "pairing": False,
                    "ha_version": HA_VERSION,
                    "integration": self.hass.data[DATA].integration_version,
                }
            )
            await self._read(sock)
        except ProtocolError as error:
            LOGGER.debug("%s broke the protocol: %s", self.name, error.reason)
            await sock.close(error.close, error.reason)
        except ConnectionResetError:
            pass
        except Exception:
            LOGGER.exception("Error handling a message from %s", self.name)
            await sock.close(Close.INTERNAL, "internal error")
        finally:
            if self._socket is sock:
                self._detach(sock)

    async def _read(self, sock: AgentSocket) -> None:
        while True:
            message = await sock.receive()
            if message is None:
                return
            await self._handle(sock, message)

    @callback
    def _attach(self, sock: AgentSocket) -> None:
        if self._socket is None and self.stats.sessions:
            LOGGER.info("%s is connected again", self.name)
        self._socket = sock
        self._configured = False
        self.going_down = None
        self.stats.connected_at = dt_util.utcnow()
        self.stats.sessions += 1
        if self._grace is not None:
            self._grace()
            self._grace = None
        if self._wake is not None and not self._wake.done():
            self._wake.set_result(WAKE_AWAKE)
        self._notify()

    @callback
    def _detach(self, sock: AgentSocket) -> None:
        self._socket = None
        self.stats.disconnected_at = dt_util.utcnow()
        self.stats.close_code = sock.close_code
        self.stats.close_reason = sock.close_reason
        LOGGER.info("%s disconnected (close code %s)", self.name, sock.close_code)
        for future, name in self._pending.values():
            if future.done():
                continue
            if name in POWER_COMMANDS and self.going_down is not None:
                future.set_result({"ok": True})
            else:
                future.set_exception(command_error("not_connected"))
        if self.going_down is None and not self._stopping:
            self._start_grace(STATE_GRACE_S)
        self._notify()

    @callback
    def _start_grace(self, seconds: float) -> None:
        if self._grace is not None:
            self._grace()
        self._grace = async_call_later(self.hass, seconds, self._grace_over)

    @callback
    def _grace_over(self, _now: datetime) -> None:
        self._grace = None
        if not self.connected:
            self.ha_state = None
            self._notify()

    async def _handle(self, sock: AgentSocket, message: dict[str, Any]) -> None:
        kind = message["type"]
        if kind == "capabilities":
            await self._on_capabilities(sock, message)
        elif kind == "state":
            self._on_state(message)
        elif kind == "event":
            self._on_event(message)
        elif kind == "going_down":
            await sock.send({"type": "ack", "id": message["id"]})
            self._on_going_down(message)
        elif kind == "result":
            entry = self._pending.get(message["id"])
            if entry is not None and not entry[0].done():
                entry[0].set_result(message)
        elif kind == "ping":
            await sock.send({"type": "pong", "t": message["t"]})

    async def _on_capabilities(self, sock: AgentSocket, message: dict[str, Any]) -> None:
        raw = {key: value for key, value in message.items() if key != "type"}
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={**self.entry.data, DATA_CAPABILITIES: raw, DATA_CAPABILITIES_AT: dt_util.utcnow().isoformat()},
        )
        self.capabilities = Capabilities(raw)
        self.async_update_device()
        self.async_remove_stale_entities()
        for listener in list(self._capability_listeners):
            listener()
        check_capabilities(self.hass, self.entry, self.capabilities, self.settings)
        if not self._configured:
            self._configured = True
            await sock.send(self.config_message())
            await self._send_lux(sock)
        self._notify()

    @callback
    def async_update_device(self) -> None:
        caps = self.capabilities
        details: dict[str, Any] = {"sw_version": caps.agent_version}
        if caps.vendor is not None:
            details["manufacturer"] = caps.vendor
        if caps.model is not None:
            details["model"] = caps.model
        registry = dr.async_get(self.hass)
        connections = {(dr.CONNECTION_NETWORK_MAC, mac) for mac in self.settings.macs}
        device = registry.async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers={(DOMAIN, self.agent_id)},
            connections=connections,
            name=self.entry.title,
            **details,
        )
        if device.connections != connections:
            registry.async_update_device(device.id, new_connections=connections)

    @callback
    def async_remove_stale_entities(self) -> None:
        """Remove registry entries this PC no longer reports or the options no longer want."""
        registry = er.async_get(self.hass)
        wanted = expected_unique_ids(self.agent_id, self.capabilities, self.settings)
        for entity in er.async_entries_for_config_entry(registry, self.entry.entry_id):
            if entity.unique_id not in wanted:
                registry.async_remove(entity.entity_id)

    @callback
    def _on_state(self, message: dict[str, Any]) -> None:
        self.state = message
        self.state_at = dt_util.utcnow()
        if self.going_down is not None:
            return
        value = message["state"]
        if value in protocol.STATES:
            self.ha_state = value
        else:
            self.ha_state = None
            if value not in self._unknown_states:
                self._unknown_states.add(value)
                LOGGER.warning("%s reported a state this version doesn't know: %s", self.name, value)
        self._notify()

    @callback
    def _on_event(self, message: dict[str, Any]) -> None:
        if message["name"] == "resumed":
            self._fire(EVENT_RESUMED)
            return
        if message["name"] != "started":
            return
        boot_time = _epoch(message.get("boot_time"))
        uptime = message.get("uptime")
        if boot_time is None or boot_time == self.store.boot_time:
            return
        self.store.boot_time = boot_time
        self.store.save()
        if isinstance(uptime, int | float) and uptime < BOOT_WINDOW_S:
            self._fire(EVENT_BOOTED)
        self._notify()

    @callback
    def _on_going_down(self, message: dict[str, Any]) -> None:
        kind = message["kind"] if message["kind"] in _GOING_DOWN else "unknown"
        self.going_down = kind
        self.ha_state, event_type = _GOING_DOWN[kind]
        if kind == "reboot":
            self._start_grace(RESTARTING_LIMIT_S)
        self._fire(event_type)
        self._notify()

    def config_message(self) -> dict[str, Any]:
        settings = self.settings
        return {
            "type": "config",
            "modules": dict(settings.modules),
            "brightness": {
                "lux": settings.lux_entity is not None and settings.module_on("brightness"),
                "curve": {
                    "anchor_lux": float(settings.curve["anchor_lux"]),
                    "anchor_pct": int(settings.curve["anchor_pct"]),
                    "slope": float(settings.curve["slope"]),
                    "floor": int(settings.curve["floor"]),
                    "ceiling": int(settings.curve["ceiling"]),
                },
                "auto": {
                    display.id: self.store.auto_brightness.get(display.id, False)
                    for display in self.capabilities.displays
                },
                "game_boost": settings.game_boost,
                "modes": self._modes(),
            },
        }

    def _modes(self) -> dict[str, Any]:
        modes: dict[str, Any] = {}
        for row in self.settings.modes:
            mode = modes.setdefault(row.display, {"code": row.code, "values": []})
            if row.value is not None and row.anchor_pct is not None:
                mode["values"].append({"value": row.value, "anchor_pct": row.anchor_pct, "label": row.label})
        return modes

    @callback
    def _on_lux_change(self, _event: Event[EventStateChangedData]) -> None:
        if self._socket is None or not self._configured or self._lux_timer is not None:
            return
        since = (dt_util.utcnow() - self._lux_sent_at).total_seconds() if self._lux_sent_at else INPUT_MIN_INTERVAL_S
        if since < INPUT_MIN_INTERVAL_S:
            self._lux_timer = async_call_later(self.hass, INPUT_MIN_INTERVAL_S - since, self._lux_due)
            return
        self.entry.async_create_task(self.hass, self._send_lux(self._socket), "torpor lux")

    @callback
    def _lux_due(self, _now: datetime) -> None:
        self._lux_timer = None
        if self._socket is not None:
            self.entry.async_create_task(self.hass, self._send_lux(self._socket), "torpor lux")

    async def _send_lux(self, sock: AgentSocket) -> None:
        entity_id = self.settings.lux_entity
        if entity_id is None or not self.settings.module_on("brightness"):
            return
        state = self.hass.states.get(entity_id)
        age = (dt_util.utcnow() - state.last_changed).total_seconds() if state is not None else 0.0
        self._lux_sent_at = dt_util.utcnow()
        try:
            await sock.send({"type": "input", "name": "lux", "value": _lux(state), "age": round(max(age, 0.0), 1)})
        except ConnectionResetError:
            return

    async def async_command(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Send one command and return its `data`, or raise the translated error."""
        sock = self._socket
        if sock is None or self.going_down is not None:
            raise command_error("not_connected")
        command_id = f"c{next(self._ids)}"
        future: asyncio.Future[dict[str, Any]] = self.hass.loop.create_future()
        self._pending[command_id] = (future, name)
        try:
            await sock.send({"type": "command", "id": command_id, "name": name, "args": args})
            async with asyncio.timeout(COMMAND_TIMEOUT_S):
                result = await future
        except TimeoutError as error:
            raise command_error("timeout") from error
        except ConnectionResetError as error:
            raise command_error("not_connected") from error
        finally:
            del self._pending[command_id]
        if not result["ok"]:
            failure = result["error"]
            raise command_error(failure["code"], str(failure.get("message") or ""))
        data = result.get("data")
        return data if isinstance(data, dict) else {}

    async def async_set_auto_brightness(self, display_id: str, on: bool) -> None:
        self.store.auto_brightness[display_id] = on
        self.store.save()
        self._notify()
        sock = self._socket
        if sock is not None and self._configured:
            try:
                await sock.send(self.config_message())
            except ConnectionResetError:
                return

    async def async_wake(self, *, wait: bool) -> str:
        """Send the magic packet unless the PC is up, then watch for it to connect."""
        if self.up:
            return WAKE_ALREADY_AWAKE
        settings = self.settings
        if not settings.module_on("wake") or not settings.macs:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="wake_not_set_up",
                translation_placeholders={"name": self.name},
            )
        await async_send_magic_packets(self.hass, settings.macs, settings.broadcast, settings.port)
        if self._wake is None or self._wake.done():
            self._wake = self.hass.loop.create_future()
            self.entry.async_create_background_task(self.hass, self._watch_wake(self._wake), "torpor wake")
        if not wait:
            return WAKE_SENT
        result = await asyncio.shield(self._wake)
        return result

    async def _watch_wake(self, future: asyncio.Future[str]) -> None:
        try:
            async with asyncio.timeout(self.settings.wake_timeout):
                await asyncio.shield(future)
        except TimeoutError:
            if not future.done():
                future.set_result(WAKE_TIMEOUT)
        if self._stopping:
            return
        if future.result() == WAKE_AWAKE:
            self._fire(EVENT_WAKE_SUCCEEDED)
        else:
            self._fire(EVENT_WAKE_FAILED)

    def keep_awake_ends_at(self) -> datetime | None:
        remaining = self.section("keep_awake").get("remaining")
        if self.state_at is None or not isinstance(remaining, int | float) or isinstance(remaining, bool):
            return None
        try:
            return self.state_at + timedelta(seconds=remaining)
        except OverflowError:
            return None
