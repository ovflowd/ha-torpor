"""State, availability while the PC sleeps, messages and dynamic entities (H11, H13-H15, H20)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry, async_fire_time_changed, mock_restore_cache_with_extra_data

from custom_components.torpor.connection import TorporConnection
from custom_components.torpor.const import DOMAIN
from custom_components.torpor.protocol import Close

from .conftest import AgentFactory, entry_options, make_entry
from .fake_agent import AGENT_ID, DISPLAY_ID, KEY, MAC, OTHER_DISPLAY_ID, FakeAgent, capabilities, state

CONTROLS = (
    "button.desk_lock",
    "button.desk_sleep",
    "button.desk_hibernate",
    "button.desk_power_off",
    "button.desk_restart",
    "switch.desk_keep_awake",
    "number.desk_example_27q_brightness",
)


def entity_ids(hass: HomeAssistant, entry: MockConfigEntry) -> set[str]:
    return {entity.entity_id for entity in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)}


async def messages_until_pong(pc: FakeAgent) -> list[str]:
    await pc.send({"type": "ping", "t": 99})
    kinds = []
    while True:
        message = await pc.receive()
        assert message is not None
        if message["type"] == "pong":
            return kinds
        kinds.append(message["type"])


async def test_every_entity_exists_while_the_pc_sleeps_through_a_restart(
    hass: HomeAssistant, entity_registry_enabled_by_default: None
) -> None:
    entry = make_entry(options=entry_options(brightness={"lux_entity": "sensor.desk_light"}))
    mock_restore_cache_with_extra_data(
        hass,
        [(State("sensor.desk_state", "asleep"), {"native_value": "asleep", "native_unit_of_measurement": None})],
    )
    entry.add_to_hass(hass)
    loaded = await hass.config_entries.async_setup(entry.entry_id)
    assert loaded
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entity_ids(hass, entry) == {
        "sensor.desk_state",
        "binary_sensor.desk_agent",
        "event.desk_power",
        "sensor.desk_last_boot",
        "sensor.desk_sleep_blockers",
        "switch.desk_example_27q_auto_brightness",
        "button.desk_wake",
        *CONTROLS,
    }
    assert hass.states.get("sensor.desk_state").state == "asleep"
    assert hass.states.get("binary_sensor.desk_agent").state == "off"
    for entity_id in (*CONTROLS, "sensor.desk_sleep_blockers", "switch.desk_example_27q_auto_brightness"):
        assert hass.states.get(entity_id).state == STATE_UNAVAILABLE, entity_id
    assert hass.states.get("button.desk_wake").state != STATE_UNAVAILABLE
    assert hass.states.get("sensor.desk_last_boot").state == STATE_UNAVAILABLE
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, AGENT_ID), entry.entry_id)
    assert device.name == "desk"
    assert device.manufacturer == "Example Computers"
    assert device.model == "Tower 5"
    assert device.sw_version == "0.1.0"
    assert device.connections == {(dr.CONNECTION_NETWORK_MAC, MAC)}


@pytest.mark.parametrize(("restored", "after"), [("idle", 120), ("restarting", 900)])
async def test_a_restored_awake_state_turns_unknown_without_a_connection(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, restored: str, after: int
) -> None:
    mock_restore_cache_with_extra_data(
        hass, [(State("sensor.desk_state", restored), {"native_value": restored, "native_unit_of_measurement": None})]
    )
    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.desk_state").state == restored

    freezer.tick(timedelta(seconds=after - 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.desk_state").state == restored
    freezer.tick(timedelta(seconds=2))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.desk_state").state == STATE_UNKNOWN


async def test_hibernate_follows_can_hibernate_without_registry_changes(hass: HomeAssistant, agent: FakeAgent) -> None:
    registry = er.async_get(hass)
    before = registry.async_get("button.desk_hibernate")
    assert hass.states.get("button.desk_hibernate").state != STATE_UNAVAILABLE

    await agent.send(state(power={"can_suspend": "yes", "can_hibernate": "no", "blockers": []}))
    await agent.sync()

    assert hass.states.get("button.desk_hibernate").state == STATE_UNAVAILABLE
    assert registry.async_get("button.desk_hibernate") == before


async def test_hibernate_exists_only_when_the_pc_allows_it(hass: HomeAssistant, new_agent: AgentFactory) -> None:
    power = capabilities()["modules"]["power"]
    power["actions"]["hibernate"] = False
    entry = make_entry(caps=capabilities(power=power))
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("button.desk_hibernate") is None
    assert hass.states.get("button.desk_sleep") is not None


@pytest.mark.parametrize(
    ("kind", "value", "event_type"),
    [
        ("sleep", "asleep", "suspending"),
        ("poweroff", "off", "shutting_down"),
        ("unknown", "off", "shutting_down"),
        ("reboot", "restarting", "restarting"),
    ],
)
async def test_going_down_is_acked_at_once_and_sets_the_state(
    hass: HomeAssistant, agent: FakeAgent, kind: str, value: str, event_type: str
) -> None:
    await agent.send({"type": "going_down", "id": "g1", "kind": kind})
    ack = await agent.expect("ack")
    assert ack == {"type": "ack", "id": "g1"}
    await agent.send(state(state="idle"))
    await agent.sync()

    assert hass.states.get("sensor.desk_state").state == value
    assert hass.states.get("event.desk_power").attributes["event_type"] == event_type
    assert hass.states.get("button.desk_lock").state == STATE_UNAVAILABLE
    await agent.close(1001)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.desk_state").state == value
    assert hass.states.get("binary_sensor.desk_agent").state == "off"
    assert hass.states.get("button.desk_wake").state != STATE_UNAVAILABLE


async def test_a_reboot_without_a_reconnect_turns_unknown_after_15_minutes(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, agent: FakeAgent
) -> None:
    await agent.send({"type": "going_down", "id": "g1", "kind": "reboot"})
    await agent.expect("ack")
    await agent.close(1001)
    await hass.async_block_till_done()

    freezer.tick(timedelta(minutes=16))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.desk_state").state == STATE_UNKNOWN


async def test_a_lost_connection_keeps_the_state_for_120_seconds(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, agent: FakeAgent, new_agent: AgentFactory
) -> None:
    await agent.close()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.desk_state").state == "in_use"
    assert hass.states.get("button.desk_lock").state == STATE_UNAVAILABLE

    freezer.tick(timedelta(seconds=121))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.desk_state").state == STATE_UNKNOWN

    back = await new_agent()
    await back.connect(state="idle")
    await back.sync()
    assert hass.states.get("sensor.desk_state").state == "idle"
    assert hass.states.get("button.desk_lock").state != STATE_UNAVAILABLE


async def test_an_unknown_state_value_shows_as_unknown_and_is_logged_once(
    hass: HomeAssistant, agent: FakeAgent, caplog: pytest.LogCaptureFixture
) -> None:
    for _ in range(2):
        await agent.send(state(state="gaming"))
    await agent.sync()

    assert hass.states.get("sensor.desk_state").state == STATE_UNKNOWN
    assert caplog.text.count("doesn't know: gaming") == 1


async def test_the_state_sensor_carries_session_display_remote_and_busy(hass: HomeAssistant, agent: FakeAgent) -> None:
    await agent.send(state(state="busy", session="locked", display_on=False, busy=["lease:backup"], remote=[]))
    await agent.sync()

    sensor = hass.states.get("sensor.desk_state")
    assert sensor.state == "busy"
    assert sensor.attributes["session"] == "locked"
    assert sensor.attributes["display_on"] is False
    assert sensor.attributes["busy"] == ["lease:backup"]
    assert sensor.attributes["remote"] == []


async def test_events_report_resume_and_a_fresh_boot_once(
    hass: HomeAssistant, agent: FakeAgent, entity_registry_enabled_by_default: None
) -> None:
    await agent.send({"type": "event", "name": "resumed"})
    await agent.sync()
    assert hass.states.get("event.desk_power").attributes["event_type"] == "resumed"

    await agent.send({"type": "event", "name": "started", "boot_time": 1762600000, "uptime": 35.2})
    await agent.sync()
    assert hass.states.get("event.desk_power").attributes["event_type"] == "booted"
    first = hass.states.get("event.desk_power").state

    await agent.send({"type": "event", "name": "started", "boot_time": 1762600000, "uptime": 900})
    await agent.send({"type": "event", "name": "started", "boot_time": 1762700000, "uptime": 4000})
    await agent.send({"type": "event", "name": "something_new"})
    await agent.sync()
    assert hass.states.get("event.desk_power").state == first


async def test_config_comes_only_after_capabilities(
    hass: HomeAssistant, new_agent: AgentFactory, loaded_entry: MockConfigEntry
) -> None:
    pc = await new_agent()
    await pc.authenticate(key=KEY)
    await pc.send(state())
    kinds = await messages_until_pong(pc)
    assert kinds == []

    await pc.send({"type": "capabilities", **capabilities()})
    config = await pc.expect("config")

    assert config == {
        "type": "config",
        "modules": {"power": True, "keep_awake": True, "wake": True, "brightness": True},
        "brightness": {
            "lux": False,
            "curve": {"anchor_lux": 100.0, "anchor_pct": 40, "slope": 25.0, "floor": 10, "ceiling": 80},
            "auto": {DISPLAY_ID: False},
            "game_boost": 30,
            "modes": {},
        },
    }
    await pc.send({"type": "capabilities", **capabilities()})
    kinds = await messages_until_pong(pc)
    assert kinds == []


async def test_lux_input_is_sent_after_config_and_coalesced(
    hass: HomeAssistant, new_agent: AgentFactory, freezer: FrozenDateTimeFactory
) -> None:
    hass.states.async_set("sensor.desk_light", "84", {"device_class": "illuminance"})
    freezer.tick(timedelta(seconds=12))
    entry = make_entry(options=entry_options(brightness={"lux_entity": "sensor.desk_light"}))
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    pc = await new_agent()
    await pc.connect()

    config = await pc.expect("config")
    first = await pc.expect("input")
    assert config["brightness"]["lux"] is True
    assert first == {"type": "input", "name": "lux", "value": 84.0, "age": 12.0}

    hass.states.async_set("sensor.desk_light", "100", {"device_class": "illuminance"})
    hass.states.async_set("sensor.desk_light", "120", {"device_class": "illuminance"})
    await hass.async_block_till_done()
    kinds = await messages_until_pong(pc)
    assert kinds == []
    freezer.tick(timedelta(seconds=2))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    second = await pc.expect("input")
    assert second["value"] == 120.0

    freezer.tick(timedelta(seconds=3))
    hass.states.async_set("sensor.desk_light", STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    third = await pc.expect("input")
    assert third == {"type": "input", "name": "lux", "value": None, "age": 0.0}


@pytest.mark.parametrize("reading", ["dark", "nan"])
async def test_a_lux_reading_that_is_not_a_number_is_sent_as_null(
    hass: HomeAssistant, new_agent: AgentFactory, reading: str
) -> None:
    hass.states.async_set("sensor.desk_light", reading)
    entry = make_entry(options=entry_options(brightness={"lux_entity": "sensor.desk_light"}))
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    pc = await new_agent()
    await pc.connect()
    message = await pc.expect("input")
    assert message["value"] is None


async def test_a_new_display_adds_its_entities_and_a_dropped_one_removes_them(
    hass: HomeAssistant, agent: FakeAgent, loaded_entry: MockConfigEntry, new_agent: AgentFactory
) -> None:
    brightness: dict[str, Any] = capabilities()["modules"]["brightness"]
    brightness["displays"].append(
        {"id": OTHER_DISPLAY_ID, "label": "Laptop panel", "backend": "logind", "internal": True}
    )
    await agent.send({"type": "capabilities", **capabilities(brightness=brightness)})
    await agent.sync()
    await hass.async_block_till_done()
    assert "number.desk_laptop_panel_brightness" in entity_ids(hass, loaded_entry)
    assert loaded_entry.data["capabilities"]["modules"]["brightness"]["displays"][1]["id"] == OTHER_DISPLAY_ID

    await agent.close()
    await hass.config_entries.async_reload(loaded_entry.entry_id)
    await hass.async_block_till_done()
    assert "number.desk_laptop_panel_brightness" in entity_ids(hass, loaded_entry)

    back = await new_agent()
    await back.connect()
    await back.sync()
    await hass.async_block_till_done()
    assert "number.desk_laptop_panel_brightness" not in entity_ids(hass, loaded_entry)
    assert "number.desk_example_27q_brightness" in entity_ids(hass, loaded_entry)

    brightness["displays"] = brightness["displays"][1:]
    await back.send({"type": "capabilities", **capabilities(brightness=brightness)})
    await back.sync()
    await hass.async_block_till_done()
    assert "number.desk_laptop_panel_brightness" in entity_ids(hass, loaded_entry)
    assert "number.desk_example_27q_brightness" not in entity_ids(hass, loaded_entry)


async def test_a_module_turned_off_in_the_options_loses_its_entities(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    assert "switch.desk_keep_awake" in entity_ids(hass, loaded_entry)
    options = entry_options(modules={"keep_awake": False, "power": False})
    hass.config_entries.async_update_entry(loaded_entry, options=options)
    await hass.config_entries.async_reload(loaded_entry.entry_id)
    await hass.async_block_till_done()

    remaining = entity_ids(hass, loaded_entry)
    assert "switch.desk_keep_awake" not in remaining
    assert not [
        entity_id for entity_id in remaining if entity_id.startswith("button.desk_") and "wake" not in entity_id
    ]


async def test_the_device_follows_the_selected_wake_macs(hass: HomeAssistant, loaded_entry: MockConfigEntry) -> None:
    hass.config_entries.async_update_entry(loaded_entry, options=entry_options(wake={"macs": []}))
    await hass.config_entries.async_reload(loaded_entry.entry_id)
    await hass.async_block_till_done()

    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, AGENT_ID), loaded_entry.entry_id)
    assert device.connections == set()


async def test_impossible_times_from_the_pc_read_as_unknown(
    hass: HomeAssistant,
    entity_registry_enabled_by_default: None,
    new_agent: AgentFactory,
    loaded_entry: MockConfigEntry,
) -> None:
    keep_awake = {"on": True, "remaining": 1e300, "backend": "kde", "applied": True}
    pc = await new_agent()
    await pc.connect(boot_time=10**20, keep_awake=keep_awake)
    await pc.expect("config")
    await pc.send({"type": "event", "name": "started", "boot_time": 10**20, "uptime": 5})
    await pc.sync()

    assert hass.states.get("sensor.desk_last_boot").state == STATE_UNAVAILABLE
    assert hass.states.get("switch.desk_keep_awake").attributes["ends_at"] is None
    assert hass.states.get("binary_sensor.desk_agent").state == "on"
    assert loaded_entry.runtime_data.store.boot_time is None


async def test_an_unexpected_error_closes_the_session_with_4500(hass: HomeAssistant, agent: FakeAgent) -> None:
    with patch.object(TorporConnection, "_on_event", side_effect=RuntimeError("boom")):
        await agent.send({"type": "event", "name": "resumed"})
        code = await agent.closed()
    await asyncio.sleep(0.05)
    await hass.async_block_till_done()

    assert code == Close.INTERNAL
    assert hass.states.get("binary_sensor.desk_agent").state == "off"


async def test_a_pc_that_reports_the_same_capabilities_updates_when_it_was_seen(
    hass: HomeAssistant, agent: FakeAgent, loaded_entry: MockConfigEntry
) -> None:
    assert loaded_entry.data["capabilities"] == capabilities()
    assert loaded_entry.data["capabilities_at"] != "2026-11-07T10:00:00+00:00"
