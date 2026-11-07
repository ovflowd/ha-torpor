"""Diagnostics, with the key, IDs, MACs, host names, addresses and URLs taken out."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.core import HomeAssistant

from .connection import TorporConfigEntry
from .data import DATA

TO_REDACT = {"key", "agent_id", "mac", "macs", "hostname", "broadcast", "title", "unique_id"}
_URL_SCHEMES = ("http://", "https://", "ws://", "wss://")


def _without_urls(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(_URL_SCHEMES):
        return REDACTED
    if isinstance(value, dict):
        return {key: _without_urls(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_without_urls(item) for item in value]
    return value


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: TorporConfigEntry) -> dict[str, Any]:
    connection = entry.runtime_data
    stats = connection.stats
    data = {
        "entry": {"title": entry.title, "data": dict(entry.data), "options": dict(entry.options)},
        "state": connection.state,
        "ha_state": connection.ha_state,
        "connection": {
            "connected": connection.connected,
            "going_down": connection.going_down,
            "connected_at": stats.connected_at.isoformat() if stats.connected_at else None,
            "disconnected_at": stats.disconnected_at.isoformat() if stats.disconnected_at else None,
            "close_code": stats.close_code,
            "close_reason": stats.close_reason,
            "sessions": stats.sessions,
            "handshakes": dict(hass.data[DATA].handshakes.get(connection.agent_id, {})),
        },
    }
    redacted: dict[str, Any] = _without_urls(async_redact_data(data, TO_REDACT))
    return redacted
