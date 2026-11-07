"""The endpoint: decoys, ban safety, stale keys, the local check, limits and framing (H4-H10)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import WSServerHandshakeError
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.typing import ClientSessionGenerator

from custom_components.torpor import endpoint, protocol, transport
from custom_components.torpor.const import DOMAIN
from custom_components.torpor.data import DATA

from .conftest import AgentFactory, entry_options, make_entry
from .fake_agent import AGENT_ID, KEY, OTHER_AGENT_ID, OTHER_KEY, FakeAgent

Close = protocol.Close


@pytest.fixture
def ban() -> Generator[AsyncMock]:
    with patch("custom_components.torpor.endpoint.process_wrong_login", new_callable=AsyncMock) as mock:
        yield mock


async def refused_by_ha(pc: FakeAgent, **secret: bytes | str) -> None:
    """The PC checks HA's proof, finds it wrong, and hangs up with 4401 as torpord does."""
    with pytest.raises(protocol.ProtocolError) as refused:
        await pc.authenticate(**secret)
    assert refused.value.close is Close.AUTH_FAILED
    await pc.close(Close.AUTH_FAILED)


async def test_an_unknown_pc_gets_a_decoy_and_gives_up_without_a_ban(
    hass: HomeAssistant, new_agent: AgentFactory, loaded_entry: MockConfigEntry, ban: AsyncMock
) -> None:
    pc = await new_agent(agent_id=OTHER_AGENT_ID)
    await refused_by_ha(pc, key=KEY)

    stubborn = await new_agent(agent_id=OTHER_AGENT_ID)
    await stubborn.hello(key=KEY)
    await stubborn.ws.send_str(json.dumps({"type": "auth", "proof": protocol.b64(bytes(32))}))

    code = await stubborn.closed()
    assert code == Close.AUTH_FAILED
    ban.assert_not_called()
    assert not ir.async_get(hass).issues


async def test_an_unknown_pairing_code_gets_a_decoy(
    hass: HomeAssistant, new_agent: AgentFactory, ban: AsyncMock
) -> None:
    code = protocol.format_code(bytes(20))
    pc = await new_agent()
    await refused_by_ha(pc, code=code)

    guesser = await new_agent()
    await guesser.hello(code=code)
    await guesser.ws.send_str(json.dumps({"type": "pair_auth", "proof": protocol.b64(bytes(32))}))

    code = await guesser.closed()
    assert code == Close.AUTH_FAILED
    ban.assert_not_called()


async def test_a_stale_key_raises_a_repair_after_three_tries_and_never_a_ban(
    hass: HomeAssistant, new_agent: AgentFactory, ban: AsyncMock
) -> None:
    entry = make_entry(key=OTHER_KEY)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    issue_id = f"stale_key_{entry.entry_id}"

    for attempt in range(protocol.STALE_KEY_REPAIR_AFTER):
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None, attempt
        pc = await new_agent()
        await refused_by_ha(pc, key=KEY)
        await asyncio.sleep(0.05)

    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.config_entry_id == entry.entry_id
    assert issue.severity is ir.IssueSeverity.ERROR
    ban.assert_not_called()

    fixed = await new_agent()
    await fixed.connect(key=OTHER_KEY)
    await fixed.expect("config")
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    assert hass.data[DATA].handshakes[AGENT_ID]["stale_key"] == 3


async def test_hang_ups_while_the_pc_is_connected_raise_no_stale_key_repair(
    hass: HomeAssistant, agent: FakeAgent, new_agent: AgentFactory, loaded_entry: MockConfigEntry, ban: AsyncMock
) -> None:
    for _ in range(protocol.STALE_KEY_REPAIR_AFTER):
        impostor = await new_agent()
        await refused_by_ha(impostor, key=OTHER_KEY)
        await asyncio.sleep(0.05)

    assert ir.async_get(hass).async_get_issue(DOMAIN, f"stale_key_{loaded_entry.entry_id}") is None
    assert hass.data[DATA].handshakes[AGENT_ID]["stale_key"] == 3
    ban.assert_not_called()
    await agent.sync()


async def test_a_wrong_proof_for_a_paired_pc_counts_once_toward_a_ban(
    hass: HomeAssistant, new_agent: AgentFactory, loaded_entry: MockConfigEntry, ban: AsyncMock
) -> None:
    pc = await new_agent()
    await pc.hello(key=OTHER_KEY)
    await pc.ws.send_str(json.dumps({"type": "auth", "proof": protocol.b64(bytes(32))}))

    code = await pc.closed()
    assert code == Close.AUTH_FAILED
    ban.assert_awaited_once()


async def test_a_pc_that_hangs_up_after_the_challenge_counts_nothing(
    hass: HomeAssistant, new_agent: AgentFactory, loaded_entry: MockConfigEntry, ban: AsyncMock
) -> None:
    pc = await new_agent()
    await pc.hello(key=KEY)
    await pc.close()
    await asyncio.sleep(0.05)

    ban.assert_not_called()
    assert hass.data[DATA].handshakes[AGENT_ID] == {"closed": 1}


@pytest.mark.parametrize("pairing", [False, True])
async def test_a_non_local_pc_is_refused_before_any_key_work(
    hass: HomeAssistant, new_agent: AgentFactory, loaded_entry: MockConfigEntry, pairing: bool
) -> None:
    pc = await new_agent()
    await pc.open()
    secret = {"code": bytes(20)} if pairing else {"key": KEY}
    with (
        patch("custom_components.torpor.endpoint.is_local", return_value=False),
        patch.object(protocol.HomeAssistantHandshake, "__init__", side_effect=AssertionError("key work")) as keys,
    ):
        hello = protocol.AgentHandshake(
            agent_id=AGENT_ID, agent="0.1.0", ephemeral=protocol.Ephemeral.generate(), hostname="desk", **secret
        )
        await pc.ws.send_str(hello.hello_text())
        code = await pc.closed()
        assert code == Close.NOT_LOCAL
    keys.assert_not_called()


async def test_home_assistant_cloud_connections_are_not_local(
    hass: HomeAssistant, new_agent: AgentFactory, loaded_entry: MockConfigEntry
) -> None:
    pc = await new_agent()
    with patch("custom_components.torpor.endpoint.is_cloud_connection", return_value=True):
        await pc.open()
        hello = protocol.AgentHandshake(
            agent_id=AGENT_ID, agent="0.1.0", ephemeral=protocol.Ephemeral.generate(), key=KEY
        )
        await pc.ws.send_str(hello.hello_text())
        code = await pc.closed()
        assert code == Close.NOT_LOCAL


async def test_allow_remote_lets_a_paired_pc_in_from_outside(hass: HomeAssistant, new_agent: AgentFactory) -> None:
    entry = make_entry(options=entry_options(advanced={"allow_remote": True}))
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    pc = await new_agent()
    with patch("custom_components.torpor.endpoint.is_local", return_value=False):
        await pc.connect()
        await pc.expect("config")

    stranger = await new_agent(agent_id=OTHER_AGENT_ID)
    with patch("custom_components.torpor.endpoint.is_local", return_value=False):
        await stranger.open()
        hello = protocol.AgentHandshake(
            agent_id=OTHER_AGENT_ID, agent="0.1.0", ephemeral=protocol.Ephemeral.generate(), key=KEY
        )
        await stranger.ws.send_str(hello.hello_text())
        code = await stranger.closed()
        assert code == Close.NOT_LOCAL


async def test_the_eleventh_connection_in_a_minute_gets_429(hass: HomeAssistant, new_agent: AgentFactory) -> None:
    for _ in range(protocol.CONNECTS_PER_IP):
        pc = await new_agent()
        await pc.open()
        await pc.close()
    late = await new_agent()
    with pytest.raises(WSServerHandshakeError) as refused:
        await late.open()
    assert refused.value.status == 429


async def test_too_many_pending_handshakes_get_503(hass: HomeAssistant, new_agent: AgentFactory) -> None:
    hass.data[DATA].max_pending = 2
    waiting = []
    for _ in range(2):
        pc = await new_agent()
        waiting.append(pc)
    for pc in waiting:
        await pc.open()
    await asyncio.sleep(0.05)
    late = await new_agent()
    with pytest.raises(WSServerHandshakeError) as refused:
        await late.open()
    assert refused.value.status == 503
    assert hass.data[DATA].pending_handshakes == 2


async def test_a_slow_handshake_is_closed_with_4408(
    hass: HomeAssistant, new_agent: AgentFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(endpoint, "HANDSHAKE_TIMEOUT_S", 0.1)
    pc = await new_agent()
    await pc.open()
    code = await pc.closed()
    assert code == Close.TIMEOUT
    await asyncio.sleep(0.05)
    assert hass.data[DATA].pending_handshakes == 0


async def test_a_pre_auth_message_over_4_kib_is_refused(hass: HomeAssistant, new_agent: AgentFactory) -> None:
    pc = await new_agent()
    await pc.open()
    await pc.ws.send_str(json.dumps({"type": "hello", "padding": "x" * protocol.MAX_HANDSHAKE_TEXT}))
    code = await pc.closed()
    assert code == Close.MALFORMED


async def test_a_binary_message_before_auth_ok_is_refused(hass: HomeAssistant, new_agent: AgentFactory) -> None:
    pc = await new_agent()
    await pc.open()
    await pc.ws.send_bytes(b"\x01" * 40)
    code = await pc.closed()
    assert code == Close.MALFORMED


async def test_a_bad_auth_message_is_malformed_not_a_ban(
    hass: HomeAssistant, new_agent: AgentFactory, loaded_entry: MockConfigEntry, ban: AsyncMock
) -> None:
    pc = await new_agent()
    await pc.hello(key=KEY)
    await pc.ws.send_str('{"type":"auth","proof":"short"}')
    code = await pc.closed()
    assert code == Close.MALFORMED
    ban.assert_not_called()


async def test_more_than_50_messages_in_10_seconds_close_with_4429(hass: HomeAssistant, agent: FakeAgent) -> None:
    for number in range(protocol.AGENT_MESSAGES_PER_WINDOW + 1):
        await agent.send({"type": "ping", "t": number})
    code = await agent.closed()
    assert code == Close.RATE_LIMITED


async def test_ninety_seconds_without_a_frame_close_with_4408(
    hass: HomeAssistant, agent: FakeAgent, monkeypatch: pytest.MonkeyPatch, new_agent: AgentFactory
) -> None:
    monkeypatch.setattr(transport, "IDLE_TIMEOUT_S", 0.1)
    quiet = await new_agent()
    await quiet.connect()
    code = await agent.closed()
    assert code == Close.SUPERSEDED
    code = await quiet.closed()
    assert code == Close.TIMEOUT


async def test_a_disabled_entry_answers_4503_after_a_good_proof(hass: HomeAssistant, new_agent: AgentFactory) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER)
    pc = await new_agent()
    challenge = await pc.hello(key=KEY)
    await pc.ws.send_str(pc.handshake.answer(challenge))
    code = await pc.closed()
    assert code == Close.NOT_READY


async def test_a_second_connection_supersedes_the_first(
    hass: HomeAssistant, agent: FakeAgent, new_agent: AgentFactory
) -> None:
    second = await new_agent()
    await second.connect()
    await second.expect("config")
    code = await agent.closed()
    assert code == Close.SUPERSEDED
    await second.sync()
    assert hass.states.get("binary_sensor.desk_agent").state == "on"


async def test_text_after_the_handshake_is_refused(hass: HomeAssistant, agent: FakeAgent) -> None:
    await agent.ws.send_str('{"type":"ping","t":1}')
    code = await agent.closed()
    assert code == Close.MALFORMED


async def test_a_replayed_frame_is_refused(hass: HomeAssistant, agent: FakeAgent) -> None:
    frame = agent.session.seal({"type": "ping", "t": 1})
    await agent.ws.send_bytes(frame)
    await agent.expect("pong")
    await agent.ws.send_bytes(frame)
    code = await agent.closed()
    assert code == Close.MALFORMED


async def test_a_message_from_the_wrong_side_is_refused(hass: HomeAssistant, agent: FakeAgent) -> None:
    await agent.send({"type": "command", "id": "c1", "name": "lock", "args": {}})
    code = await agent.closed()
    assert code == Close.MALFORMED


async def test_a_newer_protocol_from_a_known_pc_raises_a_repair(
    hass: HomeAssistant, new_agent: AgentFactory, loaded_entry: MockConfigEntry
) -> None:
    pc = await new_agent()
    await pc.open()
    await pc.ws.send_str(json.dumps({"type": "hello", "proto": 2, "agent_id": AGENT_ID}))
    code = await pc.closed()
    assert code == Close.UNSUPPORTED
    issue_id = f"protocol_mismatch_{loaded_entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_placeholders == {"name": "desk", "proto": "2"}

    stranger = await new_agent()
    await stranger.open()
    await stranger.ws.send_str(json.dumps({"type": "hello", "proto": 2, "agent_id": OTHER_AGENT_ID}))
    code = await stranger.closed()
    assert code == Close.UNSUPPORTED

    current = await new_agent()
    await current.connect()
    await current.expect("config")
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


@pytest.mark.parametrize(
    ("proto", "local"),
    [
        ("2. **Your PC is compromised.** [Re-pair it here](https://evil.example/fix)", True),
        (True, True),
        (10**30, True),
        (2, False),
    ],
)
async def test_a_mismatch_repair_takes_only_a_version_number_from_a_local_pc(
    hass: HomeAssistant, new_agent: AgentFactory, loaded_entry: MockConfigEntry, proto: object, local: bool
) -> None:
    pc = await new_agent()
    await pc.open()
    with patch("custom_components.torpor.endpoint.is_local", return_value=local):
        await pc.ws.send_str(json.dumps({"type": "hello", "proto": proto, "agent_id": AGENT_ID}))
        code = await pc.closed()

    assert code == Close.UNSUPPORTED
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"protocol_mismatch_{loaded_entry.entry_id}") is None


async def test_a_plain_get_is_not_a_404(hass: HomeAssistant, hass_client_no_auth: ClientSessionGenerator) -> None:
    loaded = await async_setup_component(hass, DOMAIN, {})
    assert loaded
    client = await hass_client_no_auth()
    response = await client.get(protocol.WS_PATH)
    assert response.status == 400
    assert hass.data[DATA].pending_handshakes == 0


async def test_unloading_the_entry_tells_the_pc_to_reconnect(
    hass: HomeAssistant, agent: FakeAgent, loaded_entry: MockConfigEntry
) -> None:
    await hass.config_entries.async_unload(loaded_entry.entry_id)
    code = await agent.closed()
    assert code == Close.RECONNECT
