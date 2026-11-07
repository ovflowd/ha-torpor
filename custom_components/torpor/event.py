"""Power events: going down, coming back, and how a wake went."""

from __future__ import annotations

from typing import override

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .connection import TorporConfigEntry, TorporConnection
from .const import EVENT_TYPES
from .entity import TorporEntity, setup_platform

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: TorporConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    setup_platform(entry, async_add_entities, lambda connection: [PowerEvent(connection)])


class PowerEvent(TorporEntity, EventEntity):
    def __init__(self, connection: TorporConnection) -> None:
        super().__init__(connection, "power")
        self._attr_event_types = list(EVENT_TYPES)

    @property
    @override
    def available(self) -> bool:
        return True

    @override
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.connection.async_add_event_listener(self._handle_event))

    @callback
    def _handle_event(self, event_type: str) -> None:
        self._trigger_event(event_type)
        self.async_write_ha_state()
