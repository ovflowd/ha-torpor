"""Repairs that follow from what the PC reports (H21). stale_key and protocol_mismatch are in test_endpoint."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant.common import MockConfigEntry

from custom_components.torpor.const import DOMAIN

from .conftest import AgentFactory, entry_options, make_entry
from .fake_agent import MAC, capabilities


def issue(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{key}_{entry.entry_id}")


async def loaded(hass: HomeAssistant, caps: dict[str, Any], **sections: dict[str, Any]) -> MockConfigEntry:
    entry = make_entry(caps=caps, options=entry_options(**sections))
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def wake_nics(wol: str) -> dict[str, Any]:
    wake = capabilities()["modules"]["wake"]
    wake["nics"] = [{"name": "enp5s0", "mac": MAC, "kind": "ethernet", "wol": wol, "primary": True}]
    return wake


@pytest.mark.parametrize("module", ["power", "keep_awake", "wake", "brightness"])
async def test_a_module_the_pc_stopped_offering_raises_a_repair_until_it_is_back(
    hass: HomeAssistant, new_agent: AgentFactory, module: str
) -> None:
    gone = {
        "enabled": True,
        "available": False,
        "backend": None,
        "problems": [{"code": "x", "message": "no backend", "hint": None}],
    }
    entry = await loaded(hass, capabilities(**{module: gone}))

    raised = issue(hass, entry, f"module_unavailable_{module}")
    assert raised is not None
    assert raised.severity is ir.IssueSeverity.WARNING
    assert raised.translation_placeholders == {"name": "desk", "problems": "no backend"}
    assert raised.config_entry_id == entry.entry_id

    pc = await new_agent()
    await pc.connect()
    await pc.expect("config")
    assert issue(hass, entry, f"module_unavailable_{module}") is None


async def test_turning_the_module_off_clears_its_repair(hass: HomeAssistant) -> None:
    gone = {"enabled": False, "available": False, "backend": None, "problems": []}
    entry = await loaded(hass, capabilities(keep_awake=gone))
    assert issue(hass, entry, "module_unavailable_keep_awake").translation_placeholders["problems"] == "-"

    hass.config_entries.async_update_entry(entry, options=entry_options(modules={"keep_awake": False}))
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert issue(hass, entry, "module_unavailable_keep_awake") is None


async def test_wake_on_lan_off_on_every_selected_nic_raises_a_repair(
    hass: HomeAssistant, new_agent: AgentFactory
) -> None:
    entry = await loaded(hass, capabilities(wake=wake_nics("off")))
    raised = issue(hass, entry, "wol_off")
    assert raised is not None
    assert raised.translation_placeholders == {"name": "desk", "nics": "enp5s0"}

    pc = await new_agent()
    await pc.connect(caps=capabilities(wake=wake_nics("unknown")))
    await pc.expect("config")
    assert issue(hass, entry, "wol_off") is None


async def test_wake_on_lan_off_is_ignored_while_wake_is_off(hass: HomeAssistant) -> None:
    entry = await loaded(hass, capabilities(wake=wake_nics("off")), modules={"wake": False})
    assert issue(hass, entry, "wol_off") is None


async def test_linger_off_raises_a_repair_and_on_clears_it(hass: HomeAssistant, new_agent: AgentFactory) -> None:
    entry = await loaded(hass, capabilities(linger=False))
    assert issue(hass, entry, "linger_off") is not None

    pc = await new_agent()
    await pc.connect(caps=capabilities(linger=None))
    await pc.expect("config")
    assert issue(hass, entry, "linger_off") is None


async def test_an_ignored_repair_stays_ignored_when_it_still_applies(
    hass: HomeAssistant, new_agent: AgentFactory
) -> None:
    entry = await loaded(hass, capabilities(linger=False))
    registry = ir.async_get(hass)
    registry.async_ignore(DOMAIN, f"linger_off_{entry.entry_id}", True)

    pc = await new_agent()
    await pc.connect(caps=capabilities(linger=False))
    await pc.expect("config")

    assert issue(hass, entry, "linger_off").dismissed_version is not None


async def test_removing_the_entry_removes_its_repairs(hass: HomeAssistant) -> None:
    entry = await loaded(hass, capabilities(linger=False))
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert issue(hass, entry, "linger_off") is None
