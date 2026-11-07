"""Whether the PC's agent is connected, and whether a game runs on it."""

from __future__ import annotations

from typing import override

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .connection import TorporConfigEntry, TorporConnection
from .entity import TorporEntity, setup_platform

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: TorporConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    def build(connection: TorporConnection) -> list[TorporEntity]:
        entities: list[TorporEntity] = [AgentSensor(connection)]
        if connection.capabilities.games:
            entities.append(GameSensor(connection))
        return entities

    setup_platform(entry, async_add_entities, build)


class AgentSensor(TorporEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, connection: TorporConnection) -> None:
        super().__init__(connection, "agent")

    @property
    @override
    def available(self) -> bool:
        return True

    @property
    @override
    def is_on(self) -> bool:
        return self.connection.connected


class GameSensor(TorporEntity, BinarySensorEntity):
    """From GameMode or a power-profiles-daemon hold, whichever the PC has; unavailable while it can't tell."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, connection: TorporConnection) -> None:
        super().__init__(connection, "game")

    @property
    @override
    def available(self) -> bool:
        return self.connection.up and isinstance(self.connection.state.get("game"), bool)

    @property
    @override
    def is_on(self) -> bool:
        return self.connection.state.get("game") is True
