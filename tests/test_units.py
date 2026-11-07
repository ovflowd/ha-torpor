"""Small pieces on their own: parsing, limits, the store, redaction and lost sockets."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant.common import MockConfigEntry

from custom_components.torpor import data, endpoint, pairing, transport
from custom_components.torpor.capabilities import Capabilities
from custom_components.torpor.data import ConnectLimiter, TorporStore
from custom_components.torpor.diagnostics import _without_urls
from custom_components.torpor.protocol import Close, ProtocolError

from .conftest import AgentFactory, make_entry
from .fake_agent import KEY, MAC, OTHER_KEY, FakeAgent, capabilities, state
from .test_config_flow import features


class ClosedSocket:
    """A session socket whose PC is already gone."""

    closed = True
    close_code = None
    close_reason = ""

    async def send(self, message: dict[str, Any]) -> None:
        raise ConnectionResetError

    async def close(self, code: int, reason: str) -> None:
        return None


def test_malformed_capabilities_read_as_absent() -> None:
    caps = Capabilities(
        {
            "host": "desk",
            "modules": {
                "power": {"enabled": True, "actions": ["lock"], "problems": "broken"},
                "wake": {"enabled": True, "nics": [{"mac": "not-a-mac"}, "junk", {"mac": MAC}]},
                "brightness": {"enabled": True, "displays": [{"id": "XYZ"}, None, {"id": "0123456789ab"}]},
                "keep_awake": "on",
            },
        }
    )
    assert caps.hostname == "PC"
    assert caps.linger is None
    assert caps.problems("power") == []
    assert not caps.allows("power", "lock")
    assert caps.module("keep_awake") == {}
    assert [(nic.name, nic.wol, nic.primary) for nic in caps.nics] == [(MAC, "unknown", False)]
    assert [(display.label, display.internal) for display in caps.displays] == [("0123456789ab", False)]
    assert Capabilities(None).nics == []
    assert Capabilities({"modules": {"wake": {"nics": "x"}, "brightness": {"displays": 1}}}).displays == []
    assert Capabilities({"modules": {"wake": {"nics": "x"}}}).nics == []


def test_the_connect_limiter_forgets_quiet_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(data.time, "monotonic", lambda: now[0])
    limiter = ConnectLimiter(limit=2, window=60)
    for number in range(300):
        assert limiter.allow(f"192.0.2.{number % 250}/{number}")
    now[0] += 61
    assert limiter.allow("192.0.2.1")
    assert len(limiter._seen) == 1
    assert limiter.allow("192.0.2.1")
    assert not limiter.allow("192.0.2.1")
    now[0] += 61
    assert limiter.allow("192.0.2.1")


def test_the_connect_limiter_window_slides(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(data.time, "monotonic", lambda: now[0])
    limiter = ConnectLimiter(limit=2, window=60)
    assert limiter.allow("192.0.2.1")
    now[0] += 50
    assert limiter.allow("192.0.2.1")
    assert not limiter.allow("192.0.2.1")
    now[0] += 11
    assert limiter.allow("192.0.2.1")
    assert not limiter.allow("192.0.2.1")


def test_the_connect_limiter_counts_public_ipv6_per_64(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(data.time, "monotonic", lambda: 1000.0)
    limiter = ConnectLimiter(limit=2, window=60)
    assert limiter.allow("2001:db8:0:1::1")
    assert limiter.allow("2001:db8:0:1::2")
    assert not limiter.allow("2001:db8:0:1:ffff::3")
    assert limiter.allow("2001:db8:0:2::1")
    for address in ("fd00::1", "fd00::2", "fd00::2"):
        assert limiter.allow(address)
    assert not limiter.allow("fd00::2")
    assert limiter.allow("::ffff:192.0.2.7")
    assert limiter.allow("192.0.2.7")
    assert not limiter.allow("192.0.2.7")


def test_the_connect_limiter_holds_at_most_size_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(data.time, "monotonic", lambda: now[0])
    limiter = ConnectLimiter(limit=1, window=60, size=3)
    for number in range(5):
        assert limiter.allow(f"192.0.2.{number}")
        now[0] += 1
    assert list(limiter._seen) == ["192.0.2.2", "192.0.2.3", "192.0.2.4"]
    assert not limiter.allow("192.0.2.4")
    assert limiter.allow("192.0.2.0")


async def test_the_store_loads_what_it_saved_and_ignores_junk(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    hass_storage["torpor.abc"] = {
        "version": 1,
        "key": "torpor.abc",
        "data": {"auto_brightness": {"3f2a9c1b04de": True, "bad": "yes"}, "boot_time": 1762500000},
    }
    store = TorporStore(hass, "abc")
    await store.async_load()
    assert store.auto_brightness == {"3f2a9c1b04de": True}
    assert store.boot_time == 1762500000

    hass_storage["torpor.junk"] = {"version": 1, "key": "torpor.junk", "data": ["not", "a", "dict"]}
    junk = TorporStore(hass, "junk")
    await junk.async_load()
    assert junk.auto_brightness == {}
    assert junk.boot_time is None


def test_diagnostics_drop_every_url() -> None:
    assert _without_urls({"a": ["http://10.0.0.5:8123", "wss://example.invalid/x", "text"], "b": 1}) == {
        "a": ["**REDACTED**", "**REDACTED**", "text"],
        "b": 1,
    }


@pytest.mark.parametrize(
    ("remote", "cloud", "local"),
    [("10.0.0.5", False, True), ("unix", False, False), (None, False, False), ("10.0.0.5", True, False)],
)
def test_the_local_check(remote: str | None, cloud: bool, local: bool) -> None:
    request = MagicMock(remote=remote)
    assert endpoint._local(request, cloud) is local


async def test_stale_key_closes_older_than_an_hour_do_not_count(
    hass: HomeAssistant, new_agent: AgentFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(endpoint, "STALE_WINDOW_S", 0)
    entry = make_entry(key=OTHER_KEY)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    for _ in range(4):
        pc = await new_agent()
        with pytest.raises(ProtocolError):
            await pc.authenticate(key=KEY)
        await pc.close(Close.AUTH_FAILED)
        await asyncio.sleep(0.05)
    assert not ir.async_get(hass).issues


async def test_a_session_message_during_pairing_is_refused(hass: HomeAssistant, new_agent: AgentFactory) -> None:
    result = await hass.config_entries.flow.async_init("torpor", context={"source": SOURCE_USER})
    progress = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    pc = await new_agent()
    await pc.authenticate(code=progress["description_placeholders"]["code"])
    await pc.send(state())
    code = await pc.closed()
    assert code == Close.MALFORMED
    hass.config_entries.flow.async_abort(progress["flow_id"])


async def test_a_pairing_socket_that_drops_while_waiting_for_the_ack(
    hass: HomeAssistant, new_agent: AgentFactory
) -> None:
    result = await hass.config_entries.flow.async_init("torpor", context={"source": SOURCE_USER})
    progress = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    pc = await new_agent()
    await pc.authenticate(code=progress["description_placeholders"]["code"])
    await pc.send({"type": "capabilities", **capabilities()})
    await pc.sync()
    await hass.async_block_till_done()
    await hass.config_entries.flow.async_configure(progress["flow_id"])
    await hass.config_entries.flow.async_configure(progress["flow_id"], {"next_step_id": "features"})
    submit = asyncio.ensure_future(hass.config_entries.flow.async_configure(progress["flow_id"], features()))
    await pc.expect("paired")
    await pc.close()
    result = await submit
    assert result["reason"] == "pc_disconnected"


async def test_sending_to_a_closed_socket_raises_connection_reset() -> None:
    ws = MagicMock(closed=True)
    sock = transport.AgentSocket(ws, MagicMock(), pairing=False)
    with pytest.raises(ConnectionResetError):
        await sock.send({"type": "ping", "t": 1})


async def test_the_message_window_slides(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    monkeypatch.setattr(transport.time, "monotonic", lambda: now[0])
    sock = transport.AgentSocket(MagicMock(), MagicMock(), pairing=False)
    for _ in range(50):
        sock._count()
    now[0] += 11
    sock._count()
    assert len(sock._received) == 1


async def test_a_pc_gone_mid_send_is_handled_quietly(hass: HomeAssistant, loaded_entry: MockConfigEntry) -> None:
    connection = loaded_entry.runtime_data
    connection._socket = ClosedSocket()
    connection._configured = True
    await connection._send_lux(connection._socket)
    await connection.async_set_auto_brightness("3f2a9c1b04de", True)
    with pytest.raises(HomeAssistantError) as raised:
        await connection.async_command("lock", {})
    assert raised.value.translation_key == "error_not_connected"
    await connection.async_run(ClosedSocket())
    connection._socket = None


async def test_restore_does_nothing_once_the_pc_is_known(
    hass: HomeAssistant, agent: FakeAgent, loaded_entry: MockConfigEntry
) -> None:
    connection = loaded_entry.runtime_data
    connection.restore("asleep")
    assert connection.ha_state == "in_use"


async def test_keep_awake_without_an_expiry_has_no_end(hass: HomeAssistant, agent: FakeAgent) -> None:
    await agent.send(state(keep_awake={"on": True, "remaining": None, "backend": "kde", "applied": True}))
    await agent.sync()
    assert hass.states.get("switch.desk_keep_awake").attributes["ends_at"] is None


async def test_a_pending_command_already_answered_is_left_alone_on_disconnect(
    hass: HomeAssistant, agent: FakeAgent, loaded_entry: MockConfigEntry
) -> None:
    connection = loaded_entry.runtime_data
    done: asyncio.Future[dict[str, Any]] = hass.loop.create_future()
    done.set_result({"ok": True})
    connection._pending["c99"] = (done, "lock")
    await agent.close()
    await hass.async_block_till_done()
    assert done.result() == {"ok": True}
    del connection._pending["c99"]


async def test_a_second_grace_replaces_the_first(hass: HomeAssistant, loaded_entry: MockConfigEntry) -> None:
    connection = loaded_entry.runtime_data
    connection._start_grace(10)
    first = connection._grace
    connection._start_grace(20)
    assert connection._grace is not first


async def test_the_pairing_keepalive_stops_when_the_pc_is_gone(
    hass: HomeAssistant, setup_integration: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pairing, "PING_INTERVAL_S", 0)
    pending = pairing.PendingPairing(hass)

    async with asyncio.timeout(1):
        await pending._keepalive(ClosedSocket())

    await pending.close(Close.PAIRING_CANCELLED, "done")
    assert not hass.data[data.DATA].pairings
