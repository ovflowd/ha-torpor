"""Keep awake, and automatic brightness per display."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, override

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .capabilities import Display
from .connection import TorporConfigEntry, TorporConnection
from .entity import TorporEntity, brightness_displays, has_keep_awake, setup_platform

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: TorporConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    def build(connection: TorporConnection) -> list[TorporEntity]:
        entities: list[TorporEntity] = []
        if has_keep_awake(connection.capabilities, connection.settings):
            entities.append(KeepAwakeSwitch(connection))
        if connection.settings.lux_entity:
            entities += [
                AutoBrightnessSwitch(connection, display)
                for display in brightness_displays(connection.capabilities, connection.settings)
            ]
        return entities

    setup_platform(entry, async_add_entities, build)


class KeepAwakeSwitch(TorporEntity, SwitchEntity):
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(self, connection: TorporConnection) -> None:
        super().__init__(connection, "keep_awake")

    @property
    @override
    def is_on(self) -> bool:
        return self.connection.section("keep_awake").get("on") is True

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        ends_at: datetime | None = self.connection.keep_awake_ends_at() if self.is_on else None
        return {"ends_at": ends_at.isoformat() if ends_at else None}

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        hours = self.connection.settings.keep_awake_hours
        await self.async_keep_awake(timedelta(hours=hours))

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.connection.async_command("keep_awake", {"on": False, "for": None})

    async def async_keep_awake(self, duration: timedelta) -> None:
        """The torpor.keep_awake action. A zero duration keeps it on until turned off."""
        seconds = duration.total_seconds()
        await self.connection.async_command("keep_awake", {"on": True, "for": seconds or None})


class AutoBrightnessSwitch(TorporEntity, SwitchEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, connection: TorporConnection, display: Display) -> None:
        super().__init__(connection, "auto_brightness", display)
        self._display_id = display.id

    @property
    @override
    def available(self) -> bool:
        return self.connection.up and self.connection.display(self._display_id) is not None

    @property
    @override
    def is_on(self) -> bool:
        return self.connection.store.auto_brightness.get(self._display_id, False)

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        display = self.connection.display(self._display_id) or {}
        return {"overridden": display.get("overridden"), "target": display.get("target")}

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.connection.async_set_auto_brightness(self._display_id, True)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.connection.async_set_auto_brightness(self._display_id, False)
