"""Setting up, unloading and removing an entry."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from aiohttp import WSCloseCode
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry, async_fire_time_changed

from custom_components.torpor.const import DOMAIN

from .fake_agent import FakeAgent


async def test_a_connected_pc_shows_its_state(hass: HomeAssistant, agent: FakeAgent) -> None:
    assert hass.states.get("sensor.desk_state").state == "in_use"
    assert hass.states.get("binary_sensor.desk_agent").state == "on"


async def test_stopping_home_assistant_says_goodbye_to_the_pc_at_once(hass: HomeAssistant, agent: FakeAgent) -> None:
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)

    code = await agent.closed()
    assert code == WSCloseCode.GOING_AWAY
    assert agent.close_reason == "Home Assistant is stopping"
    await hass.async_block_till_done()
    assert hass.states.get("sensor.desk_state").state == "in_use"


async def test_the_actions_are_registered_without_any_entry(hass: HomeAssistant, setup_integration: None) -> None:
    assert set(hass.services.async_services_for_domain(DOMAIN)) == {"wake", "sleep", "keep_awake"}


async def test_unload_and_reload(hass: HomeAssistant, loaded_entry: MockConfigEntry) -> None:
    unloaded = await hass.config_entries.async_unload(loaded_entry.entry_id)
    assert unloaded
    assert loaded_entry.state is ConfigEntryState.NOT_LOADED

    loaded = await hass.config_entries.async_setup(loaded_entry.entry_id)
    assert loaded
    assert loaded_entry.state is ConfigEntryState.LOADED


async def test_unloading_writes_the_store_and_removing_deletes_it_for_good(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, loaded_entry: MockConfigEntry, hass_storage: dict[str, Any]
) -> None:
    key = f"torpor.{loaded_entry.entry_id}"
    connection = loaded_entry.runtime_data
    await connection.async_set_auto_brightness("3f2a9c1b04de", True)
    await hass.config_entries.async_unload(loaded_entry.entry_id)
    assert hass_storage[key]["data"]["auto_brightness"] == {"3f2a9c1b04de": True}

    await hass.config_entries.async_remove(loaded_entry.entry_id)
    freezer.tick(timedelta(seconds=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert key not in hass_storage
