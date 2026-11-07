"""Diagnostics, with secrets and identifiers redacted (H22)."""

from __future__ import annotations

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.components.diagnostics import get_diagnostics_for_config_entry
from pytest_homeassistant.typing import ClientSessionGenerator
from syrupy.assertion import SnapshotAssertion

from .fake_agent import KEY, MAC, FakeAgent


@pytest.mark.freeze_time("2026-11-07 12:00:00+00:00")
async def test_diagnostics_redact_the_key_ids_macs_and_names(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    agent: FakeAgent,
    loaded_entry: MockConfigEntry,
    hass_client: ClientSessionGenerator,
    snapshot: SnapshotAssertion,
) -> None:
    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, loaded_entry)

    assert diagnostics == snapshot
    text = str(diagnostics)
    for secret in (loaded_entry.data["key"], loaded_entry.unique_id, MAC, "'desk'"):
        assert secret not in text
    assert KEY.hex() not in text
