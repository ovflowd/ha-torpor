"""Pairing, re-pairing and options (H1-H4, H18, H19)."""

from __future__ import annotations

import asyncio
import base64
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult, FlowResultType
from homeassistant.helpers.network import NoURLAvailableError
from pytest_homeassistant.common import MockConfigEntry

from custom_components.torpor import pairing, protocol, transport
from custom_components.torpor.const import DOMAIN

from .conftest import AgentFactory, entry_options, make_entry
from .fake_agent import AGENT_ID, KEY, MAC, OTHER_AGENT_ID, WIFI_MAC, FakeAgent, capabilities


def features(**sections: dict[str, Any]) -> dict[str, Any]:
    form: dict[str, Any] = {
        "modules": {"power": True, "keep_awake": True, "wake": True, "brightness": True},
        "wake": {"macs": [MAC], "broadcast": "255.255.255.255", "port": 9, "timeout": 120},
        "keep_awake": {"hours": 12},
        "brightness": {"anchor_lux": 100, "anchor_pct": 40, "slope": 25, "floor": 10, "ceiling": 80},
        "advanced": {"allow_remote": False},
    }
    for name, values in sections.items():
        form[name] = {**form[name], **values}
    return form


async def start_pairing(hass: HomeAssistant) -> FlowResult:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["step_id"] == "pair"
    return result


async def pair_pc(
    hass: HomeAssistant, pc: FakeAgent, progress: FlowResult, caps: dict[str, Any] | None = None
) -> FlowResult:
    """The PC proves the code and sends its capabilities; returns the confirm menu."""
    auth_ok = await pc.authenticate(code=progress["description_placeholders"]["code"])
    assert auth_ok["pairing"] is True
    await pc.send({"type": "capabilities", **(caps or capabilities())})
    await pc.sync()
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(progress["flow_id"])
    assert result["type"] is FlowResultType.MENU, result
    assert result["step_id"] == "confirm"
    return result


async def submit_and_ack(hass: HomeAssistant, pc: FakeAgent, flow_id: str, user_input: dict[str, Any]) -> FlowResult:
    submit = asyncio.ensure_future(hass.config_entries.flow.async_configure(flow_id, user_input))
    paired = await pc.expect("paired")
    assert not submit.done()
    await pc.send({"type": "ack", "id": paired["id"]})
    result = await submit
    return result


@pytest.fixture
def ban() -> AsyncMock:
    with patch("custom_components.torpor.endpoint.process_wrong_login", new_callable=AsyncMock) as mock:
        yield mock


async def test_pairing_creates_the_entry_only_after_the_pc_acks(
    hass: HomeAssistant, new_agent: AgentFactory, ban: AsyncMock
) -> None:
    progress = await start_pairing(hass)
    assert progress["description_placeholders"]["command"].startswith("torporctl pair --url ")
    assert progress["description_placeholders"]["code"].startswith("trp1-")
    pc = await new_agent()
    menu = await pair_pc(hass, pc, progress)
    assert menu["menu_options"] == ["features", "cancel"]
    assert menu["description_placeholders"] == {
        "hostname": "desk",
        "os": "Example Linux 1.0",
        "agent": "0.1.0",
        "agent_id": AGENT_ID[:8],
        "fingerprint": pc.handshake.fingerprint,
    }
    form = await hass.config_entries.flow.async_configure(progress["flow_id"], {"next_step_id": "features"})
    assert form["type"] is FlowResultType.FORM
    assert form["step_id"] == "features"

    result = await submit_and_ack(hass, pc, progress["flow_id"], features())

    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.title == "desk"
    assert entry.unique_id == AGENT_ID
    assert base64.b64decode(entry.data["key"]) == pc.handshake.pairing_key
    assert entry.data["capabilities"] == capabilities()
    assert entry.options["modules"] == {"power": True, "keep_awake": True, "wake": True, "brightness": True}
    code = await pc.closed()
    assert code == protocol.Close.RECONNECT

    again = await new_agent()
    await again.connect(key=pc.handshake.pairing_key)
    await again.expect("config")
    ban.assert_not_called()


async def test_an_abandoned_pairing_leaves_no_entry_and_never_sends_paired(
    hass: HomeAssistant, new_agent: AgentFactory
) -> None:
    progress = await start_pairing(hass)
    pc = await new_agent()
    await pair_pc(hass, pc, progress)

    hass.config_entries.flow.async_abort(progress["flow_id"])
    messages = await pc.until_closed()

    assert "paired" not in [message["type"] for message in messages]
    assert pc.close_code == protocol.Close.PAIRING_CANCELLED
    assert not hass.config_entries.async_entries(DOMAIN)
    assert not hass.data["torpor"].pairings


async def test_a_pairing_that_nobody_answers_times_out(
    hass: HomeAssistant, setup_integration: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pairing, "PAIRING_TIMEOUT_S", 0.2)
    progress = await start_pairing(hass)
    await asyncio.sleep(0.3)
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_configure(progress["flow_id"])

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "pairing_timeout"


async def test_a_pairing_left_at_the_confirm_step_times_out_with_4408(
    hass: HomeAssistant, new_agent: AgentFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pairing, "PAIRING_TIMEOUT_S", 1)
    progress = await start_pairing(hass)
    pc = await new_agent()
    await pair_pc(hass, pc, progress)
    await hass.config_entries.flow.async_configure(progress["flow_id"], {"next_step_id": "features"})

    code = await pc.closed(timeout=3)
    assert code == protocol.Close.TIMEOUT
    result = await hass.config_entries.flow.async_configure(progress["flow_id"], features())

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "pairing_timeout"
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_cancel_at_the_confirm_step_closes_the_pc_with_4410(hass: HomeAssistant, new_agent: AgentFactory) -> None:
    progress = await start_pairing(hass)
    pc = await new_agent()
    await pair_pc(hass, pc, progress)

    result = await hass.config_entries.flow.async_configure(progress["flow_id"], {"next_step_id": "cancel"})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "pairing_rejected"
    code = await pc.closed()
    assert code == protocol.Close.PAIRING_CANCELLED


async def test_an_already_paired_pc_is_told_to_use_reconfigure(
    hass: HomeAssistant, new_agent: AgentFactory, loaded_entry: MockConfigEntry
) -> None:
    progress = await start_pairing(hass)
    pc = await new_agent()
    await pc.authenticate(code=progress["description_placeholders"]["code"])
    await pc.send({"type": "capabilities", **capabilities()})
    await pc.sync()
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_configure(progress["flow_id"])

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    code = await pc.closed()
    assert code == protocol.Close.PAIRING_CANCELLED
    assert pc.close_reason == "already paired, use Reconfigure"


async def test_three_wrong_pairing_proofs_burn_the_code_without_a_ban(
    hass: HomeAssistant, new_agent: AgentFactory, ban: AsyncMock
) -> None:
    progress = await start_pairing(hass)
    code = progress["description_placeholders"]["code"]
    codes = []
    for _ in range(protocol.PAIRING_MAX_FAILURES):
        pc = await new_agent()
        await pc.hello(code=code)
        await pc.ws.send_str('{"type":"pair_auth","proof":"' + protocol.b64(bytes(32)) + '"}')
        closed = await pc.closed()
        codes.append(closed)
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_configure(progress["flow_id"])

    assert codes == [protocol.Close.AUTH_FAILED, protocol.Close.AUTH_FAILED, protocol.Close.PAIRING_CANCELLED]
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "pairing_failed"
    ban.assert_not_called()


async def test_a_second_pc_with_the_code_cannot_take_a_claimed_pairing(
    hass: HomeAssistant, new_agent: AgentFactory, ban: AsyncMock
) -> None:
    progress = await start_pairing(hass)
    intruder = await new_agent()
    challenge = await intruder.hello(code=progress["description_placeholders"]["code"])
    pc = await new_agent()
    menu = await pair_pc(hass, pc, progress)
    assert menu["description_placeholders"]["fingerprint"] == pc.handshake.fingerprint

    await intruder.ws.send_str(intruder.handshake.answer(challenge))

    code = await intruder.closed()
    assert code == protocol.Close.PAIRING_CANCELLED
    await hass.config_entries.flow.async_configure(progress["flow_id"], {"next_step_id": "features"})
    result = await submit_and_ack(hass, pc, progress["flow_id"], features())
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert base64.b64decode(result["result"].data["key"]) == pc.handshake.pairing_key
    ban.assert_not_called()


async def test_a_proof_that_arrives_after_the_flow_ended_is_refused(
    hass: HomeAssistant, new_agent: AgentFactory
) -> None:
    progress = await start_pairing(hass)
    late = await new_agent()
    challenge = await late.hello(code=progress["description_placeholders"]["code"])
    hass.config_entries.flow.async_abort(progress["flow_id"])
    await hass.async_block_till_done()

    await late.ws.send_str(late.handshake.answer(challenge))

    code = await late.closed()
    assert code == protocol.Close.PAIRING_CANCELLED
    assert not hass.data["torpor"].pairings


async def test_home_assistant_pings_a_pairing_pc_past_the_idle_timeout(
    hass: HomeAssistant, new_agent: AgentFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pairing, "PING_INTERVAL_S", 0.05)
    monkeypatch.setattr(transport, "IDLE_TIMEOUT_S", 0.3)
    progress = await start_pairing(hass)
    pc = await new_agent()
    await pair_pc(hass, pc, progress)
    await hass.config_entries.flow.async_configure(progress["flow_id"], {"next_step_id": "features"})
    for _ in range(10):
        ping = await pc.receive()
        assert ping is not None
        assert ping["type"] == "ping"
        await pc.send({"type": "pong", "t": ping["t"]})

    result = await submit_and_ack(hass, pc, progress["flow_id"], features())

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_an_unexpected_error_while_pairing_closes_with_4500(hass: HomeAssistant, new_agent: AgentFactory) -> None:
    progress = await start_pairing(hass)
    pc = await new_agent()
    with patch.object(pairing.PendingPairing, "_read", side_effect=RuntimeError("boom")):
        await pc.authenticate(code=progress["description_placeholders"]["code"])
        code = await pc.closed()
    await asyncio.sleep(0.05)
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_configure(progress["flow_id"])

    assert code == protocol.Close.INTERNAL
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "pc_disconnected"


async def test_a_pc_that_leaves_before_its_capabilities_aborts_the_flow(
    hass: HomeAssistant, new_agent: AgentFactory
) -> None:
    progress = await start_pairing(hass)
    pc = await new_agent()
    await pc.authenticate(code=progress["description_placeholders"]["code"])
    await pc.close()
    await asyncio.sleep(0.05)
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_configure(progress["flow_id"])

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "pc_disconnected"


async def test_a_pc_that_leaves_at_the_features_step_aborts_without_an_entry(
    hass: HomeAssistant, new_agent: AgentFactory
) -> None:
    progress = await start_pairing(hass)
    pc = await new_agent()
    await pair_pc(hass, pc, progress)
    await hass.config_entries.flow.async_configure(progress["flow_id"], {"next_step_id": "features"})
    await pc.close()
    await asyncio.sleep(0.05)

    result = await hass.config_entries.flow.async_configure(progress["flow_id"], features())

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "pc_disconnected"
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_a_pc_that_never_acks_paired_gets_no_entry(
    hass: HomeAssistant, new_agent: AgentFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pairing, "PAIRED_ACK_TIMEOUT_S", 0.2)
    progress = await start_pairing(hass)
    pc = await new_agent()
    await pair_pc(hass, pc, progress)
    await hass.config_entries.flow.async_configure(progress["flow_id"], {"next_step_id": "features"})

    result = await hass.config_entries.flow.async_configure(progress["flow_id"], features())

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "pc_disconnected"
    assert not hass.config_entries.async_entries(DOMAIN)
    code = await pc.closed()
    assert code == protocol.Close.PAIRING_CANCELLED


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"brightness": {"floor": 60, "ceiling": 50}}, "ceiling_below_floor"),
        ({"wake": {"macs": []}}, "no_mac_selected"),
        ({"wake": {"broadcast": "not-an-address"}}, "invalid_broadcast"),
    ],
)
async def test_the_features_step_checks_its_input(
    hass: HomeAssistant, new_agent: AgentFactory, change: dict[str, Any], error: str
) -> None:
    progress = await start_pairing(hass)
    pc = await new_agent()
    await pair_pc(hass, pc, progress)
    await hass.config_entries.flow.async_configure(progress["flow_id"], {"next_step_id": "features"})

    result = await hass.config_entries.flow.async_configure(progress["flow_id"], features(**change))

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}


async def test_the_features_step_offers_only_what_the_pc_has(hass: HomeAssistant, new_agent: AgentFactory) -> None:
    progress = await start_pairing(hass)
    pc = await new_agent()
    caps = capabilities(
        brightness={
            "enabled": True,
            "available": False,
            "backend": None,
            "problems": [{"code": "no_backend", "message": "no display found", "hint": None}],
        },
        keep_awake={"enabled": False, "available": False, "backend": None, "problems": []},
    )
    await pair_pc(hass, pc, progress, caps)

    form = await hass.config_entries.flow.async_configure(progress["flow_id"], {"next_step_id": "features"})

    modules = form["data_schema"].schema["modules"].schema.schema
    assert [str(key) for key in modules] == ["power", "wake"]
    assert form["description_placeholders"]["unavailable"] == (
        "- Keep awake: turned off on the PC\n- Brightness: no display found"
    )
    offered = {**features(), "modules": {"power": True, "wake": True}}
    result = await submit_and_ack(hass, pc, progress["flow_id"], offered)
    assert result["result"].options["modules"] == {
        "power": True,
        "keep_awake": False,
        "wake": True,
        "brightness": False,
    }


async def test_without_a_known_url_the_command_shows_a_placeholder(
    hass: HomeAssistant, setup_integration: None
) -> None:
    with patch("custom_components.torpor.config_flow.get_url", side_effect=NoURLAvailableError):
        progress = await start_pairing(hass)
    assert progress["description_placeholders"]["command"] == "torporctl pair --url http://homeassistant.local:8123"
    hass.config_entries.flow.async_abort(progress["flow_id"])


async def test_closing_the_first_step_leaves_nothing_behind(hass: HomeAssistant, setup_integration: None) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done()
    assert not hass.data["torpor"].pairings


async def test_reconfigure_gives_the_pc_a_new_key_and_the_old_one_stops_working(
    hass: HomeAssistant, new_agent: AgentFactory, loaded_entry: MockConfigEntry, ban: AsyncMock
) -> None:
    result = await loaded_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    progress = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    pc = await new_agent()
    menu = await pair_pc(hass, pc, progress)
    assert menu["menu_options"] == ["finish", "cancel"]

    result = await submit_and_ack(hass, pc, progress["flow_id"], {"next_step_id": "finish"})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    new_key = pc.handshake.pairing_key
    assert base64.b64decode(loaded_entry.data["key"]) == new_key
    assert loaded_entry.options == entry_options()
    await hass.async_block_till_done()
    code = await pc.closed()
    assert code == protocol.Close.RECONNECT

    stale = await new_agent()
    with pytest.raises(protocol.ProtocolError) as refused:
        await stale.authenticate(key=KEY)
    assert refused.value.close is protocol.Close.AUTH_FAILED
    await stale.close(protocol.Close.AUTH_FAILED)

    fresh = await new_agent()
    await fresh.connect(key=new_key)
    await fresh.expect("config")
    ban.assert_not_called()


async def test_reconfigure_refuses_another_pc(
    hass: HomeAssistant, new_agent: AgentFactory, loaded_entry: MockConfigEntry
) -> None:
    result = await loaded_entry.start_reconfigure_flow(hass)
    progress = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    pc = await new_agent(agent_id=OTHER_AGENT_ID)
    await pc.authenticate(code=progress["description_placeholders"]["code"])
    await pc.send({"type": "capabilities", **capabilities()})
    await pc.sync()
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_configure(progress["flow_id"])

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_pc"
    code = await pc.closed()
    assert code == protocol.Close.PAIRING_CANCELLED
    assert base64.b64decode(loaded_entry.data["key"]) == KEY


async def test_reconfigure_aborts_when_the_pc_leaves_before_finishing(
    hass: HomeAssistant, new_agent: AgentFactory, loaded_entry: MockConfigEntry
) -> None:
    result = await loaded_entry.start_reconfigure_flow(hass)
    progress = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    pc = await new_agent()
    await pair_pc(hass, pc, progress)
    await pc.close()
    await asyncio.sleep(0.05)

    result = await hass.config_entries.flow.async_configure(progress["flow_id"], {"next_step_id": "finish"})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "pc_disconnected"
    assert base64.b64decode(loaded_entry.data["key"]) == KEY


async def test_options_keep_a_modules_settings_while_it_is_off(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    result = await hass.config_entries.options.async_init(loaded_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["description_placeholders"]["updated"] != "-"
    tuned = features(modules={"brightness": False}, brightness={"floor": 20, "ceiling": 70, "slope": 30})

    result = await hass.config_entries.options.async_configure(result["flow_id"], tuned)
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert loaded_entry.options["modules"]["brightness"] is False
    assert loaded_entry.options["brightness"]["floor"] == 20

    result = await hass.config_entries.options.async_init(loaded_entry.entry_id)
    brightness = result["data_schema"].schema["brightness"].schema.schema
    suggested = {str(key): key.description["suggested_value"] for key in brightness if key.description}
    assert suggested["floor"] == 20
    assert suggested["slope"] == 30
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], features(brightness={"floor": 20, "ceiling": 70, "slope": 30})
    )
    await hass.async_block_till_done()
    assert loaded_entry.options["modules"]["brightness"] is True
    assert loaded_entry.options["brightness"] == {
        "anchor_lux": 100.0,
        "anchor_pct": 40,
        "slope": 30.0,
        "floor": 20,
        "ceiling": 70,
        "game_boost": 30,
        "modes": [],
    }


async def test_options_work_while_the_pc_is_offline_and_check_their_input(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    result = await hass.config_entries.options.async_init(loaded_entry.entry_id)
    wake = result["data_schema"].schema["wake"].schema.schema
    macs = next(key for key in wake if str(key) == "macs")
    assert [option["value"] for option in wake[macs].config["options"]] == [MAC, WIFI_MAC]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], features(brightness={"floor": 90, "ceiling": 10})
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "ceiling_below_floor"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], features(brightness={"lux_entity": "sensor.desk_light"})
    )
    await hass.async_block_till_done()
    assert loaded_entry.options["brightness"]["lux_entity"] == "sensor.desk_light"


async def test_options_list_a_selected_nic_the_pc_no_longer_reports(hass: HomeAssistant) -> None:
    entry = make_entry(options=entry_options(wake={"macs": ["02:00:5e:10:00:09"]}), caps=capabilities())
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)

    wake = result["data_schema"].schema["wake"].schema.schema
    macs = next(key for key in wake if str(key) == "macs")
    assert "02:00:5e:10:00:09" in [option["value"] for option in wake[macs].config["options"]]
