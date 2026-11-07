"""Buttons, switches, numbers and the sleep and keep_awake actions send the right commands (H12, H17)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from datetime import timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceNotSupported, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant.common import MockConfigEntry, async_fire_time_changed

from custom_components.torpor import connection, protocol
from custom_components.torpor.const import DOMAIN

from .conftest import AgentFactory, entry_options, make_entry
from .fake_agent import AGENT_ID, DISPLAY_ID, FakeAgent, capabilities, state


async def answered(
    agent: FakeAgent, call: Awaitable[Any], *, ok: bool = True, data: dict[str, Any] | None = None, **error: Any
) -> dict[str, Any]:
    """Run `call` while the PC answers the one command it causes. Returns that command."""
    task = asyncio.ensure_future(call)
    command = await agent.expect("command")
    result: dict[str, Any] = {"type": "result", "id": command["id"], "ok": ok}
    if ok:
        result["data"] = data or {}
    else:
        result["error"] = error
    await agent.send(result)
    await task
    return command


def press(hass: HomeAssistant, entity_id: str) -> Awaitable[Any]:
    return hass.services.async_call("button", "press", {ATTR_ENTITY_ID: entity_id}, blocking=True)


def device_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, AGENT_ID), entry.entry_id)
    assert device is not None
    return device.id


@pytest.mark.parametrize(
    ("entity_id", "name", "args"),
    [
        ("button.desk_lock", "lock", {}),
        ("button.desk_sleep", "suspend", {"force": False}),
        ("button.desk_hibernate", "hibernate", {"force": False}),
        ("button.desk_power_off", "poweroff", {"force": False}),
        ("button.desk_restart", "reboot", {"force": False}),
    ],
)
async def test_each_power_button_sends_its_command(
    hass: HomeAssistant,
    entity_registry_enabled_by_default: None,
    agent: FakeAgent,
    entity_id: str,
    name: str,
    args: dict,
) -> None:
    command = await answered(agent, press(hass, entity_id))
    assert command["name"] == name
    assert command["args"] == args


async def test_power_off_and_restart_are_off_by_default(hass: HomeAssistant, agent: FakeAgent) -> None:
    assert hass.states.get("button.desk_power_off") is None
    assert hass.states.get("button.desk_restart") is None
    assert hass.states.get("button.desk_sleep") is not None


@pytest.mark.parametrize("code", [*protocol.ERRORS, "something_newer"])
async def test_each_refusal_raises_its_translated_error(hass: HomeAssistant, agent: FakeAgent, code: str) -> None:
    with pytest.raises(HomeAssistantError) as raised:
        await answered(agent, press(hass, "button.desk_sleep"), ok=False, code=code, message="1 app blocks sleep")
    expected = code if code in protocol.ERRORS else "failed"
    assert raised.value.translation_key == f"error_{expected}"
    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_placeholders == {"message": "1 app blocks sleep"}


async def test_a_pc_that_does_not_answer_raises_a_timeout(
    hass: HomeAssistant, agent: FakeAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(connection, "COMMAND_TIMEOUT_S", 0.1)
    with pytest.raises(HomeAssistantError) as raised:
        await press(hass, "button.desk_lock")
    assert raised.value.translation_key == "error_timeout"


async def test_a_sleep_that_takes_the_pc_down_before_its_result_still_succeeds(
    hass: HomeAssistant, agent: FakeAgent
) -> None:
    task = asyncio.ensure_future(press(hass, "button.desk_sleep"))
    await agent.expect("command")
    await agent.send({"type": "going_down", "id": "g1", "kind": "sleep"})
    await agent.expect("ack")
    await agent.close(1001)
    await task
    assert hass.states.get("sensor.desk_state").state == "asleep"


async def test_a_lock_cut_off_by_a_lost_connection_fails(hass: HomeAssistant, agent: FakeAgent) -> None:
    task = asyncio.ensure_future(press(hass, "button.desk_lock"))
    await agent.expect("command")
    await agent.close()
    with pytest.raises(HomeAssistantError) as raised:
        await task
    assert raised.value.translation_key == "error_not_connected"


async def test_the_sleep_action_suspends_or_hibernates(
    hass: HomeAssistant, agent: FakeAgent, loaded_entry: MockConfigEntry
) -> None:
    target = device_id(hass, loaded_entry)
    command = await answered(
        agent, hass.services.async_call(DOMAIN, "sleep", {"device_id": target, "force": True}, blocking=True)
    )
    assert command["name"] == "suspend"
    assert command["args"] == {"force": True}
    command = await answered(
        agent, hass.services.async_call(DOMAIN, "sleep", {"device_id": target, "hibernate": True}, blocking=True)
    )
    assert command["name"] == "hibernate"
    assert command["args"] == {"force": False}


async def test_the_sleep_action_refuses_what_the_pc_does_not_allow(
    hass: HomeAssistant, new_agent: AgentFactory
) -> None:
    power = capabilities()["modules"]["power"]
    power["actions"]["hibernate"] = False
    entry = make_entry(caps=capabilities(power=power))
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN, "sleep", {"device_id": device_id(hass, entry), "hibernate": True}, blocking=True
        )
    assert raised.value.translation_key == "action_not_allowed"


async def test_the_sleep_action_needs_a_torpor_device(hass: HomeAssistant, loaded_entry: MockConfigEntry) -> None:
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "sleep", {"device_id": "not-a-device"}, blocking=True)


async def test_the_sleep_action_on_a_sleeping_pc_says_it_is_not_connected(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call(DOMAIN, "sleep", {"device_id": device_id(hass, loaded_entry)}, blocking=True)
    assert raised.value.translation_key == "error_not_connected"


async def test_keep_awake_switch_uses_the_options_hours(hass: HomeAssistant, agent: FakeAgent) -> None:
    command = await answered(
        agent,
        hass.services.async_call("switch", "turn_on", {ATTR_ENTITY_ID: "switch.desk_keep_awake"}, blocking=True),
        data={"remaining": 43200},
    )
    assert command["args"] == {"on": True, "for": 43200.0}
    command = await answered(
        agent, hass.services.async_call("switch", "turn_off", {ATTR_ENTITY_ID: "switch.desk_keep_awake"}, blocking=True)
    )
    assert command["args"] == {"on": False, "for": None}


async def test_keep_awake_with_zero_hours_lasts_until_turned_off(hass: HomeAssistant, new_agent: AgentFactory) -> None:
    entry = make_entry(options=entry_options(keep_awake={"hours": 0}))
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    pc = await new_agent()
    await pc.connect()
    await pc.expect("config")
    command = await answered(
        pc, hass.services.async_call("switch", "turn_on", {ATTR_ENTITY_ID: "switch.desk_keep_awake"}, blocking=True)
    )
    assert command["args"] == {"on": True, "for": None}


async def test_keep_awake_shows_when_it_ends(hass: HomeAssistant, agent: FakeAgent) -> None:
    await agent.send(state(keep_awake={"on": True, "remaining": 600, "backend": "kde", "applied": True}))
    await agent.sync()

    switch = hass.states.get("switch.desk_keep_awake")
    assert switch.state == "on"
    assert switch.attributes["ends_at"] is not None


@pytest.mark.parametrize(("duration", "seconds"), [({"hours": 2}, 7200.0), ({"seconds": 0}, None)])
async def test_the_keep_awake_action_takes_a_duration(
    hass: HomeAssistant, agent: FakeAgent, duration: dict[str, int], seconds: float | None
) -> None:
    command = await answered(
        agent,
        hass.services.async_call(
            DOMAIN, "keep_awake", {ATTR_ENTITY_ID: "switch.desk_keep_awake", "duration": duration}, blocking=True
        ),
    )
    assert command["args"] == {"on": True, "for": seconds}


async def test_the_keep_awake_action_refuses_other_switches(hass: HomeAssistant, new_agent: AgentFactory) -> None:
    entry = make_entry(options=entry_options(brightness={"lux_entity": "sensor.desk_light"}))
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    pc = await new_agent()
    await pc.connect()
    await pc.expect("config")
    await pc.sync()
    with pytest.raises(ServiceNotSupported):
        await hass.services.async_call(
            DOMAIN,
            "keep_awake",
            {ATTR_ENTITY_ID: "switch.desk_example_27q_auto_brightness", "duration": timedelta(hours=1)},
            blocking=True,
        )


async def test_brightness_number_sends_brightness_set(hass: HomeAssistant, agent: FakeAgent) -> None:
    number = hass.states.get("number.desk_example_27q_brightness")
    assert number.state == "31"
    command = await answered(
        agent,
        hass.services.async_call(
            "number", "set_value", {ATTR_ENTITY_ID: "number.desk_example_27q_brightness", "value": 55}, blocking=True
        ),
        data={"percent": 55},
    )
    assert command["name"] == "brightness_set"
    assert command["args"] == {"display": DISPLAY_ID, "percent": 55}


async def test_brightness_is_unavailable_until_the_pc_reports_a_value(hass: HomeAssistant, agent: FakeAgent) -> None:
    await agent.send(state(brightness={"lux": None, "displays": {}}))
    await agent.sync()
    assert hass.states.get("number.desk_example_27q_brightness").state == STATE_UNAVAILABLE


async def test_the_auto_switch_is_stored_and_sent_in_config(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, new_agent: AgentFactory, hass_storage: dict[str, Any]
) -> None:
    hass.states.async_set("sensor.desk_light", "84")
    entry = make_entry(options=entry_options(brightness={"lux_entity": "sensor.desk_light"}))
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    pc = await new_agent()
    await pc.connect()
    await pc.expect("config")
    await pc.sync()

    await hass.services.async_call(
        "switch", "turn_on", {ATTR_ENTITY_ID: "switch.desk_example_27q_auto_brightness"}, blocking=True
    )
    config = await pc.expect("config")
    assert config["brightness"]["auto"] == {DISPLAY_ID: True}
    await pc.send(
        state(
            brightness={
                "lux": 84.0,
                "displays": {
                    DISPLAY_ID: {"percent": 40, "auto": True, "overridden": True, "target": 44, "error": None}
                },
            }
        )
    )
    await pc.sync()
    switch = hass.states.get("switch.desk_example_27q_auto_brightness")
    assert switch.state == "on"
    assert switch.attributes == {**switch.attributes, "overridden": True, "target": 44}

    await hass.services.async_call(
        "switch", "turn_off", {ATTR_ENTITY_ID: "switch.desk_example_27q_auto_brightness"}, blocking=True
    )
    config = await pc.expect("config")
    assert config["brightness"]["auto"] == {DISPLAY_ID: False}
    freezer.tick(timedelta(seconds=2))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass_storage[f"torpor.{entry.entry_id}"]["data"]["auto_brightness"] == {DISPLAY_ID: False}


async def test_the_auto_switch_exists_only_with_a_light_sensor(hass: HomeAssistant, agent: FakeAgent) -> None:
    assert hass.states.get("switch.desk_example_27q_auto_brightness") is None
    assert hass.states.get("number.desk_example_27q_brightness") is not None


async def test_the_auto_switch_works_while_the_pc_sleeps_and_is_sent_on_connect(
    hass: HomeAssistant, new_agent: AgentFactory
) -> None:
    entry = make_entry(options=entry_options(brightness={"lux_entity": "sensor.desk_light"}))
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await entry.runtime_data.async_set_auto_brightness(DISPLAY_ID, True)
    pc = await new_agent()
    await pc.connect()
    config = await pc.expect("config")
    assert config["brightness"]["auto"] == {DISPLAY_ID: True}


async def test_the_sleep_blockers_sensor_lists_them(
    hass: HomeAssistant, entity_registry_enabled_by_default: None, agent: FakeAgent
) -> None:
    blocker = {"who": "Example Player", "why": "Playing video", "what": "sleep"}
    await agent.send(state(power={"can_suspend": "yes", "can_hibernate": "yes", "blockers": [blocker, "junk"]}))
    await agent.sync()
    sensor = hass.states.get("sensor.desk_sleep_blockers")
    assert sensor.state == "1"
    assert sensor.attributes["blockers"] == [blocker]


async def test_the_last_boot_comes_from_the_state(
    hass: HomeAssistant, entity_registry_enabled_by_default: None, agent: FakeAgent
) -> None:
    assert hass.states.get("sensor.desk_last_boot").state == "2025-11-07T07:20:00+00:00"
