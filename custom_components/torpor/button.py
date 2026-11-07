"""Lock, sleep, hibernate, power off, restart and wake."""

from __future__ import annotations

from typing import override

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .connection import TorporConfigEntry, TorporConnection
from .entity import POWER_KEYS, TorporEntity, has_wake, power_actions, setup_platform

PARALLEL_UPDATES = 1

OFF_BY_DEFAULT = frozenset({"poweroff", "reboot"})


async def async_setup_entry(
    hass: HomeAssistant, entry: TorporConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    def build(connection: TorporConnection) -> list[TorporEntity]:
        entities: list[TorporEntity] = [
            PowerButton(connection, action) for action in power_actions(connection.capabilities, connection.settings)
        ]
        if has_wake(connection.settings):
            entities.append(WakeButton(connection))
        return entities

    setup_platform(entry, async_add_entities, build)


class PowerButton(TorporEntity, ButtonEntity):
    def __init__(self, connection: TorporConnection, action: str) -> None:
        super().__init__(connection, POWER_KEYS[action])
        self._action = action
        self._attr_entity_registry_enabled_default = action not in OFF_BY_DEFAULT
        if action == "reboot":
            self._attr_device_class = ButtonDeviceClass.RESTART

    @property
    @override
    def available(self) -> bool:
        if self._action == "hibernate":
            return self.connection.up and self.connection.section("power").get("can_hibernate") == "yes"
        return self.connection.up

    @override
    async def async_press(self) -> None:
        await self.connection.async_command(self._action, {} if self._action == "lock" else {"force": False})


class WakeButton(TorporEntity, ButtonEntity):
    def __init__(self, connection: TorporConnection) -> None:
        super().__init__(connection, "wake")

    @property
    @override
    def available(self) -> bool:
        return True

    @override
    async def async_press(self) -> None:
        await self.connection.async_wake(wait=False)
