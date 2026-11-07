"""The PC's state, its last boot and what blocks sleep."""

from __future__ import annotations

from datetime import datetime
from typing import Any, override

from homeassistant.components.sensor import RestoreSensor, SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .capabilities import Display
from .connection import TorporConfigEntry, TorporConnection
from .entity import TorporEntity, has_power, mode_displays, setup_platform
from .options import hex_text
from .protocol import HA_STATES

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: TorporConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    def build(connection: TorporConnection) -> list[TorporEntity]:
        entities: list[TorporEntity] = [StateSensor(connection), LastBootSensor(connection)]
        if has_power(connection.capabilities, connection.settings):
            entities.append(SleepBlockersSensor(connection))
        entities += [ModeSensor(connection, d) for d in mode_displays(connection.capabilities, connection.settings)]
        return entities

    setup_platform(entry, async_add_entities, build)


class StateSensor(TorporEntity, RestoreSensor):
    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(self, connection: TorporConnection) -> None:
        super().__init__(connection, "state")
        self._attr_options = list(HA_STATES)

    @override
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        value = last.native_value if last is not None else None
        if isinstance(value, str) and value in HA_STATES:
            self.connection.restore(value)
            self.async_write_ha_state()

    @property
    @override
    def available(self) -> bool:
        return True

    @property
    @override
    def native_value(self) -> str | None:
        return self.connection.ha_state

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.connection.state if self.connection.up else {}
        return {
            "session": state.get("session"),
            "display_on": state.get("display_on"),
            "remote": state.get("remote", []),
            "busy": state.get("busy", []),
        }


class LastBootSensor(TorporEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, connection: TorporConnection) -> None:
        super().__init__(connection, "last_boot")

    @property
    @override
    def available(self) -> bool:
        return self.connection.boot_time is not None

    @property
    @override
    def native_value(self) -> datetime | None:
        boot_time = self.connection.boot_time
        return dt_util.utc_from_timestamp(boot_time) if boot_time is not None else None


class SleepBlockersSensor(TorporEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, connection: TorporConnection) -> None:
        super().__init__(connection, "sleep_blockers")

    def _blockers(self) -> list[dict[str, Any]]:
        blockers = self.connection.section("power").get("blockers")
        return [blocker for blocker in blockers if isinstance(blocker, dict)] if isinstance(blockers, list) else []

    @property
    @override
    def native_value(self) -> int:
        return len(self._blockers())

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "blockers": [
                {"who": blocker.get("who"), "why": blocker.get("why"), "what": blocker.get("what")}
                for blocker in self._blockers()
            ]
        }


class ModeSensor(TorporEntity, SensorEntity):
    """A monitor's mode code as the PC last read it: the value's label from the options, or the raw value."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, connection: TorporConnection, display: Display) -> None:
        super().__init__(connection, "mode", display)
        self._display_id = display.id

    def _mode(self) -> dict[str, Any]:
        display = self.connection.display(self._display_id) or {}
        mode = display.get("mode")
        return mode if isinstance(mode, dict) else {}

    def _value(self) -> int | None:
        value = self._mode().get("value")
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @property
    @override
    def available(self) -> bool:
        return self.connection.up and self.connection.display(self._display_id) is not None

    @property
    @override
    def native_value(self) -> str | None:
        value = self._value()
        if value is None:
            return None
        rows = self.connection.settings.mode_rows(self._display_id)
        label = next((row.label for row in rows if row.value == value and row.label), None)
        return label or hex_text(value)

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        code = self.connection.settings.mode_code(self._display_id)
        value = self._value()
        error = self._mode().get("error")
        return {
            "code": hex_text(code) if code is not None else None,
            "value": hex_text(value) if value is not None else None,
            "error": error if isinstance(error, str) else None,
        }
