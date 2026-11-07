"""The base entity, and which entities a PC gets from its capabilities and the options."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .capabilities import Capabilities, Display
from .const import DOMAIN, POWER_ACTIONS
from .options import Settings

if TYPE_CHECKING:
    from .connection import TorporConfigEntry, TorporConnection

CORE_KEYS = ("state", "agent", "power", "last_boot")
POWER_KEYS = {
    "lock": "lock",
    "suspend": "sleep",
    "hibernate": "hibernate",
    "poweroff": "power_off",
    "reboot": "restart",
}


def unique_id(agent_id: str, key: str, display: Display | None = None) -> str:
    return f"{agent_id}-{display.id}-{key}" if display else f"{agent_id}-{key}"


def power_actions(capabilities: Capabilities, settings: Settings) -> list[str]:
    if not settings.module_on("power") or not capabilities.enabled("power"):
        return []
    return [action for action in POWER_ACTIONS if capabilities.allows("power", action)]


def has_power(capabilities: Capabilities, settings: Settings) -> bool:
    return settings.module_on("power") and capabilities.enabled("power")


def has_keep_awake(capabilities: Capabilities, settings: Settings) -> bool:
    return settings.module_on("keep_awake") and capabilities.allows("keep_awake", "keep_awake")


def has_wake(settings: Settings) -> bool:
    return settings.module_on("wake") and bool(settings.macs)


def brightness_displays(capabilities: Capabilities, settings: Settings) -> list[Display]:
    if not settings.module_on("brightness") or not capabilities.allows("brightness", "brightness"):
        return []
    return capabilities.displays


def mode_displays(capabilities: Capabilities, settings: Settings) -> list[Display]:
    return [d for d in brightness_displays(capabilities, settings) if settings.mode_code(d.id) is not None]


def expected_unique_ids(agent_id: str, capabilities: Capabilities, settings: Settings) -> set[str]:
    keys = [*CORE_KEYS, *(POWER_KEYS[action] for action in power_actions(capabilities, settings))]
    if has_power(capabilities, settings):
        keys.append("sleep_blockers")
    if has_keep_awake(capabilities, settings):
        keys.append("keep_awake")
    if has_wake(settings):
        keys.append("wake")
    if capabilities.games:
        keys.append("game")
    wanted = {unique_id(agent_id, key) for key in keys}
    for display in brightness_displays(capabilities, settings):
        wanted.add(unique_id(agent_id, "brightness", display))
        if settings.lux_entity:
            wanted.add(unique_id(agent_id, "auto_brightness", display))
    for display in mode_displays(capabilities, settings):
        wanted.add(unique_id(agent_id, "mode", display))
    return wanted


class TorporEntity(Entity):
    """Push only. Available while the PC is up, unless a platform says otherwise."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, connection: TorporConnection, key: str, display: Display | None = None) -> None:
        self.connection = connection
        self.display = display
        self._attr_translation_key = key
        self._attr_unique_id = unique_id(connection.agent_id, key, display)
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, connection.agent_id)})
        if display is not None:
            self._attr_translation_placeholders = {"display": display.label}

    @property
    def available(self) -> bool:
        return self.connection.up

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.connection.async_add_listener(self._handle_update))

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()


def setup_platform(
    entry: TorporConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    build: Callable[[TorporConnection], list[TorporEntity]],
) -> None:
    """Add the platform's entities now, and any new ones whenever a connected PC reports more."""
    connection = entry.runtime_data
    added: set[str | None] = set()

    @callback
    def refresh() -> None:
        current = build(connection)
        added.intersection_update(entity.unique_id for entity in current)
        new = [entity for entity in current if entity.unique_id not in added]
        added.update(entity.unique_id for entity in new)
        if new:
            async_add_entities(new)

    refresh()
    entry.async_on_unload(connection.async_add_capability_listener(refresh))
