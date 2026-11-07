"""Brightness per display."""

from __future__ import annotations

from typing import override

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .capabilities import Display
from .connection import TorporConfigEntry, TorporConnection
from .entity import TorporEntity, brightness_displays, setup_platform

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: TorporConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    def build(connection: TorporConnection) -> list[TorporEntity]:
        return [
            BrightnessNumber(connection, display)
            for display in brightness_displays(connection.capabilities, connection.settings)
        ]

    setup_platform(entry, async_add_entities, build)


class BrightnessNumber(TorporEntity, NumberEntity):
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, connection: TorporConnection, display: Display) -> None:
        super().__init__(connection, "brightness", display)
        self._display_id = display.id

    def _percent(self) -> int | None:
        display = self.connection.display(self._display_id) or {}
        percent = display.get("percent")
        return percent if isinstance(percent, int) and not isinstance(percent, bool) else None

    @property
    @override
    def available(self) -> bool:
        return self.connection.up and self._percent() is not None

    @property
    @override
    def native_value(self) -> int | None:
        return self._percent()

    @override
    async def async_set_native_value(self, value: float) -> None:
        await self.connection.async_command("brightness_set", {"display": self._display_id, "percent": round(value)})
