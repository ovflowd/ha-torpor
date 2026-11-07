"""Torpor: see, sleep, wake and dim a Linux PC from Home Assistant.

The PC runs the Torpor agent (ovflowd/torpor) and dials this integration's WebSocket endpoint.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .connection import TorporConfigEntry, TorporConnection
from .const import DOMAIN, PLATFORMS
from .data import TorporStore
from .endpoint import async_ensure_view
from .services import async_setup_services

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    await async_ensure_view(hass)
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: TorporConfigEntry) -> bool:
    """Set up from the stored capabilities. A sleeping PC is normal, so this never waits for it."""
    store = TorporStore(hass, entry.entry_id)
    await store.async_load()
    connection = TorporConnection(hass, entry, store)
    entry.runtime_data = connection
    connection.async_update_device()
    connection.async_remove_stale_entities()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    connection.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TorporConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    await entry.runtime_data.async_stop()
    await entry.runtime_data.store.async_flush()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: TorporConfigEntry) -> None:
    store = TorporStore(hass, entry.entry_id)
    await store.async_remove()
