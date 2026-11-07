"""Wake-on-LAN from Home Assistant, confirmed by the PC connecting (H16)."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant.common import MockConfigEntry

from custom_components.torpor.const import DOMAIN
from custom_components.torpor.wake import magic_packet

from .conftest import AgentFactory, entry_options, make_entry
from .fake_agent import AGENT_ID, MAC, WIFI_MAC, FakeAgent

PACKET = bytes.fromhex("ff" * 6 + "02005e100001" * 16)


@pytest.fixture
def udp() -> Generator[MagicMock]:
    """The datagram socket, so nothing leaves the test."""
    with patch("custom_components.torpor.wake.socket") as factory:
        yield factory.return_value.__enter__.return_value


def wake(hass: HomeAssistant, entry: MockConfigEntry, **data: Any) -> Any:
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, AGENT_ID), entry.entry_id)
    assert device is not None
    return hass.services.async_call(
        DOMAIN, "wake", {"device_id": device.id, **data}, blocking=True, return_response=True
    )


async def loaded(hass: HomeAssistant, **sections: dict[str, Any]) -> MockConfigEntry:
    entry = make_entry(options=entry_options(**sections))
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def test_the_magic_packet_is_six_ff_then_the_mac_sixteen_times() -> None:
    assert magic_packet(MAC) == PACKET
    assert len(PACKET) == 102
    with pytest.raises(ValueError):
        magic_packet("02:00:5e")


async def test_wake_sends_three_packets_to_the_broadcast_address_and_port(
    hass: HomeAssistant, udp: MagicMock, new_agent: AgentFactory
) -> None:
    entry = await loaded(hass, wake={"broadcast": "192.0.2.255", "port": 7, "macs": [MAC, WIFI_MAC]})

    response = await wake(hass, entry)

    assert response == {"result": "sent"}
    second = magic_packet(WIFI_MAC)
    assert (
        udp.sendto.call_args_list
        == [
            call(PACKET, ("192.0.2.255", 7)),
            call(second, ("192.0.2.255", 7)),
        ]
        * 3
    )
    pc = await new_agent()
    await pc.connect()
    await pc.expect("config")
    await hass.async_block_till_done()
    assert hass.states.get("event.desk_power").attributes["event_type"] == "wake_succeeded"


async def test_wake_with_wait_returns_awake_when_the_pc_connects(
    hass: HomeAssistant, udp: MagicMock, new_agent: AgentFactory
) -> None:
    entry = await loaded(hass)
    task = asyncio.ensure_future(wake(hass, entry, wait=True))
    await asyncio.sleep(0.3)
    assert not task.done()
    pc = await new_agent()
    await pc.connect()

    result = await task
    assert result == {"result": "awake"}


async def test_wake_with_wait_times_out_and_fires_wake_failed(hass: HomeAssistant, udp: MagicMock) -> None:
    entry = await loaded(hass, wake={"timeout": 0.3})

    response = await wake(hass, entry, wait=True)
    await hass.async_block_till_done()

    assert response == {"result": "timeout"}
    assert hass.states.get("event.desk_power").attributes["event_type"] == "wake_failed"


async def test_a_second_wake_joins_the_first_wait(hass: HomeAssistant, udp: MagicMock, new_agent: AgentFactory) -> None:
    entry = await loaded(hass)
    first = asyncio.ensure_future(wake(hass, entry, wait=True))
    await asyncio.sleep(0.3)
    result = await wake(hass, entry)
    assert result == {"result": "sent"}
    pc = await new_agent()
    await pc.connect()
    result = await first
    assert result == {"result": "awake"}
    assert udp.sendto.call_count == 6


async def test_waking_a_connected_pc_sends_nothing(
    hass: HomeAssistant, udp: MagicMock, agent: FakeAgent, loaded_entry: MockConfigEntry
) -> None:
    result = await wake(hass, loaded_entry, wait=True)
    assert result == {"result": "already_awake"}
    udp.sendto.assert_not_called()


async def test_the_wake_button_sends_the_packet(hass: HomeAssistant, udp: MagicMock) -> None:
    await loaded(hass)
    await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: "button.desk_wake"}, blocking=True)
    assert udp.sendto.call_count == 3


async def test_wake_without_a_nic_is_a_user_error(hass: HomeAssistant, udp: MagicMock) -> None:
    entry = await loaded(hass, modules={"wake": False})
    assert hass.states.get("button.desk_wake") is None
    with pytest.raises(ServiceValidationError) as raised:
        await wake(hass, entry)
    assert raised.value.translation_key == "wake_not_set_up"


async def test_a_network_error_is_reported(hass: HomeAssistant, udp: MagicMock) -> None:
    entry = await loaded(hass)
    udp.sendto.side_effect = OSError("Network is unreachable")
    with pytest.raises(HomeAssistantError) as raised:
        await wake(hass, entry)
    assert raised.value.translation_key == "wake_send_failed"
    assert raised.value.translation_placeholders == {"error": "Network is unreachable"}


async def test_unloading_during_a_wake_wait_ends_it(hass: HomeAssistant, udp: MagicMock) -> None:
    entry = await loaded(hass)
    task = asyncio.ensure_future(wake(hass, entry, wait=True))
    await asyncio.sleep(0.3)
    await hass.config_entries.async_unload(entry.entry_id)
    result = await task
    assert result == {"result": "timeout"}
    assert hass.states.get("event.desk_power").attributes.get("event_type") is None
