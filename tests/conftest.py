"""Fixtures: a paired PC entry, the integration set up, and FakeAgents dialing the real view."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.typing import ClientSessionGenerator

from custom_components.torpor import protocol
from custom_components.torpor.capabilities import Capabilities
from custom_components.torpor.const import DOMAIN
from custom_components.torpor.options import default_options

from .fake_agent import AGENT_ID, KEY, MAC, FakeAgent, capabilities

AgentFactory = Callable[..., Awaitable[FakeAgent]]


def entry_options(**sections: dict[str, Any]) -> dict[str, Any]:
    """Every module on, the wired NIC selected; `sections` replaces whole option sections' keys."""
    options = default_options(Capabilities(capabilities()))
    options["wake"]["macs"] = [MAC]
    for name, values in sections.items():
        options[name] = {**options[name], **values}
    return options


def make_entry(
    *,
    options: dict[str, Any] | None = None,
    caps: dict[str, Any] | None = None,
    agent_id: str = AGENT_ID,
    key: bytes = KEY,
    title: str = "desk",
) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=title,
        unique_id=agent_id,
        data={
            "agent_id": agent_id,
            "key": protocol.b64(key),
            "capabilities": caps if caps is not None else capabilities(),
            "capabilities_at": "2026-11-07T10:00:00+00:00",
        },
        options=options if options is not None else entry_options(),
    )


@pytest.fixture
def config_entry() -> MockConfigEntry:
    return make_entry()


@pytest.fixture
async def setup_integration(hass: HomeAssistant) -> None:
    loaded = await async_setup_component(hass, DOMAIN, {})
    assert loaded
    await hass.async_block_till_done()


@pytest.fixture
async def loaded_entry(hass: HomeAssistant, config_entry: MockConfigEntry) -> MockConfigEntry:
    config_entry.add_to_hass(hass)
    loaded = await hass.config_entries.async_setup(config_entry.entry_id)
    assert loaded
    await hass.async_block_till_done()
    return config_entry


@pytest.fixture
async def new_agent(hass: HomeAssistant, hass_client_no_auth: ClientSessionGenerator) -> AsyncGenerator[AgentFactory]:
    """Make FakeAgents that share one test client. Each call opens a new WebSocket."""
    loaded = await async_setup_component(hass, DOMAIN, {})
    assert loaded
    client = await hass_client_no_auth()
    agents: list[FakeAgent] = []

    async def factory(**kwargs: Any) -> FakeAgent:
        agent = FakeAgent(client, **kwargs)
        agents.append(agent)
        return agent

    yield factory
    for agent in agents:
        await agent.close()


@pytest.fixture
async def agent(new_agent: AgentFactory, loaded_entry: MockConfigEntry) -> FakeAgent:
    """A PC that is connected, sent its capabilities and a state, and got its config."""
    pc = await new_agent()
    await pc.connect()
    await pc.expect("config")
    await pc.sync()
    return pc
