"""End to end: a real torpord, paired through the real flow, against Home Assistant restarted twice; and a
monitor mode and a game moving the brightness it writes.

The agent is the zipapp `torporctl install` builds, run by the distro interpreter, on private
buses with a fake logind, PowerDevil and GameMode, synthetic /sys, /proc and /dev, and the agent
repo's fake ddcutil and nmcli. Home Assistant serves its real app on 127.0.0.1 with IP bans after
one wrong login. Skipped when the ovflowd/torpor checkout or PyGObject isn't there, as in this
repo's CI.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import pytest
from homeassistant.components.http.ban import KEY_BAN_MANAGER, KEY_FAILED_LOGIN_ATTEMPTS
from homeassistant.config_entries import SOURCE_USER, ConfigEntry, ConfigEntryState
from homeassistant.const import EVENT_STATE_CHANGED, STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.torpor import protocol
from custom_components.torpor.const import DOMAIN
from custom_components.torpor.data import DATA

from .harness import (
    DISPLAY_ID,
    MAC,
    MACHINE_ID,
    MONITOR,
    Agent,
    EntrySnapshot,
    FakePC,
    HomeAssistantRun,
    Tools,
    build_zipapp,
    eventually,
    find_tools,
    free_port,
)

AGENT_ID = protocol.agent_id_from_machine_id(MACHINE_ID)
LUX = "sensor.room_light"
FINGERPRINT = re.compile(r"with the fingerprint (\S+)\.$")
MAGIC = b"\xff" * 6 + bytes.fromhex(MAC.replace(":", "")) * 16
KEYS = {
    "sensor": ["state", "last_boot", "sleep_blockers"],
    "binary_sensor": ["agent"],
    "event": ["power"],
    "button": ["lock", "sleep", "wake"],
    "switch": ["keep_awake", f"{DISPLAY_ID}-auto_brightness"],
    "number": [f"{DISPLAY_ID}-brightness"],
}


@pytest.fixture
def tools() -> Tools:
    """What the PC side needs. CI sets TORPOR_E2E_REQUIRED, so a missing piece fails there instead of skipping."""
    found = find_tools()
    if isinstance(found, str):
        if os.environ.get("TORPOR_E2E_REQUIRED"):
            pytest.fail(found)
        pytest.skip(found)
    return found


@pytest.fixture
def pyz(tools: Tools, tmp_path: Path) -> Path:
    target = tmp_path / "torpor.pyz"
    build_zipapp(tools, target)
    return target


class Frames:
    """Every frame the shared protocol sealed or opened on Home Assistant's side."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.opened: list[tuple[bytes, dict[str, Any]]] = []
        self.sealed: list[tuple[bytes, dict[str, Any]]] = []
        seal, open_ = protocol.Session.seal, protocol.Session.open

        def recording_seal(session: protocol.Session, message: dict[str, Any]) -> bytes:
            frame = seal(session, message)
            self.sealed.append((frame, dict(message)))
            return frame

        def recording_open(session: protocol.Session, frame: bytes) -> dict[str, Any]:
            message = open_(session, frame)
            self.opened.append((frame, message))
            return message

        monkeypatch.setattr(protocol.Session, "seal", recording_seal)
        monkeypatch.setattr(protocol.Session, "open", recording_open)

    def check(self) -> tuple[set[str], set[str]]:
        """Each frame is ciphertext of exactly its message plus the tag; returns the types each way."""
        for frame, message in [*self.opened, *self.sealed]:
            plaintext = protocol.encode(message)
            assert frame[0] == protocol.FRAME_VERSION
            assert len(frame) == protocol.FRAME_HEADER_BYTES + len(plaintext) + protocol.TAG_BYTES
            assert plaintext not in frame
            assert b'"type"' not in frame
        return {m["type"] for _, m in self.opened}, {m["type"] for _, m in self.sealed}


class WakeListener(asyncio.DatagramProtocol):
    """Stands in for the PC's network card: magic packets sent to 127.0.0.1."""

    def __init__(self) -> None:
        self.packets: list[bytes] = []

    def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        self.packets.append(data)


class Watcher:
    """The PC's power events as one Home Assistant run fires them."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.events: list[str] = []
        hass.bus.async_listen(EVENT_STATE_CHANGED, self._changed)

    @callback
    def _changed(self, event: Event[Any]) -> None:
        new: State | None = event.data["new_state"]
        if new is not None and new.entity_id.startswith("event.") and new.attributes.get("event_type"):
            self.events.append(new.attributes["event_type"])


def entity(hass: HomeAssistant, platform: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{AGENT_ID}-{key}")
    assert entity_id is not None, f"no {platform} for {key}"
    return entity_id


def state(hass: HomeAssistant, platform: str, key: str) -> State:
    found = hass.states.get(entity(hass, platform, key))
    assert found is not None
    return found


def value_is(hass: HomeAssistant, platform: str, key: str, expected: str) -> bool:
    found = hass.states.get(entity(hass, platform, key))
    return found is not None and found.state == expected


def set_lux(hass: HomeAssistant, lux: int) -> None:
    """A reading that has held for ten minutes, so the agent counts it as settled."""
    attributes = {"device_class": "illuminance", "unit_of_measurement": "lx"}
    hass.states.async_set(LUX, str(lux), attributes, timestamp=time.time() - 600)


def features(wake_port: int, **brightness: Any) -> dict[str, Any]:
    return {
        "modules": {"power": True, "keep_awake": True, "wake": True, "brightness": True},
        "wake": {"macs": [MAC], "broadcast": "127.0.0.1", "port": wake_port, "timeout": 30},
        "keep_awake": {"hours": 1},
        "brightness": {
            "lux_entity": LUX,
            "anchor_lux": 100,
            "anchor_pct": 40,
            "slope": 25,
            "floor": 10,
            "ceiling": 80,
            **brightness,
        },
        "advanced": {"allow_remote": False},
    }


def connected(hass: HomeAssistant) -> bool:
    entries = hass.config_entries.async_entries(DOMAIN)
    return bool(entries) and entries[0].state is ConfigEntryState.LOADED and entries[0].runtime_data.connected


async def press(hass: HomeAssistant, key: str) -> None:
    await hass.services.async_call("button", "press", {"entity_id": entity(hass, "button", key)}, blocking=True)


async def switch(hass: HomeAssistant, key: str, on: bool) -> None:
    service = "turn_on" if on else "turn_off"
    await hass.services.async_call("switch", service, {"entity_id": entity(hass, "switch", key)}, blocking=True)


async def pair(
    hass: HomeAssistant, ha: HomeAssistantRun, pc: FakePC, agent: Agent, wake_port: int, **brightness: Any
) -> ConfigEntry:
    """The user adds the integration, runs the command it shows, compares the fingerprint, picks features."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["step_id"] == "user"
    progress = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert progress["type"] is FlowResultType.SHOW_PROGRESS
    placeholders = progress["description_placeholders"]
    assert placeholders["command"] == f"torporctl pair --url {ha.url}"
    flow_id = progress["flow_id"]

    torporctl = await pc.pair(agent.home, ha.url, placeholders["code"])
    printed = await eventually(
        lambda: next((m for line in torporctl.lines if (m := FINGERPRINT.search(line))), None),
        what="torporctl to print the fingerprint",
    )
    await eventually(lambda: hass.config_entries.flow.async_get(flow_id)["step_id"] == "confirm", what="confirm")
    menu = await hass.config_entries.flow.async_configure(flow_id)
    assert menu["type"] is FlowResultType.MENU
    shown = menu["description_placeholders"]
    assert shown["fingerprint"] == printed.group(1)
    assert shown["agent_id"] == AGENT_ID[:8]
    assert shown["os"] == "Example Linux"
    assert not agent.key_file.exists()

    form = await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "features"})
    assert form["step_id"] == "features"
    created = await hass.config_entries.flow.async_configure(flow_id, features(wake_port, **brightness))
    assert created["type"] is FlowResultType.CREATE_ENTRY

    code = await torporctl.finished()
    assert code == 0, "\n".join(torporctl.lines)
    assert any(line.startswith("Paired.") for line in torporctl.lines)
    assert agent.key_file.stat().st_mode & 0o777 == 0o600
    assert f"ha-url = {ha.url}" in agent.config_file.read_text()
    entry: ConfigEntry = created["result"]
    assert entry.unique_id == AGENT_ID
    assert entry.title == shown["hostname"]
    return entry


def expected_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    registry = er.async_get(hass)
    found = {(e.domain, e.unique_id) for e in er.async_entries_for_config_entry(registry, entry.entry_id)}
    wanted = {(platform, f"{AGENT_ID}-{key}") for platform, keys in KEYS.items() for key in keys}
    assert found == wanted


async def test_a_real_agent_pairs_works_sleeps_wakes_and_survives_restarts(
    tools: Tools,
    pyz: Path,
    tmp_path: Path,
    hass_storage: dict[str, Any],
    socket_enabled: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frames = Frames(monkeypatch)
    loop = asyncio.get_running_loop()
    transport, listener = await loop.create_datagram_endpoint(WakeListener, local_addr=("127.0.0.1", 0))
    wake_port = transport.get_extra_info("sockname")[1]
    pc = FakePC(tmp_path / "pc", tools, pyz)
    ha = HomeAssistantRun(tmp_path / "ha", free_port())
    agent: Agent | None = None
    try:
        await pc.start()
        hass = await ha.start()
        watcher = Watcher(hass)
        set_lux(hass, 100)

        agent = await pc.start_agent(tmp_path / "home")
        await eventually(lambda: agent.connection() == "not_paired", what="torpord to wait for a key")
        assert agent.status()["paired"] is False
        delay = await pc.wait_for_desktop(lambda e: e.get("call") == "Inhibit", what="the delay inhibitor")
        assert delay["args"][1:] == ["Torpor", "Tell Home Assistant the PC is going down", "delay"]
        sender = delay["sender"]

        entry = await pair(hass, ha, pc, agent, wake_port)

        # Capabilities become entities, and the first state and events come through.
        await eventually(lambda: connected(hass), what="torpord to pick up the key and connect")
        await eventually(lambda: agent.connection() == "connected", what="status.json to say connected")
        expected_entities(hass, entry)
        await eventually(lambda: value_is(hass, "sensor", "state", "in_use"), what="in_use")
        main = state(hass, "sensor", "state")
        assert main.attributes["session"] == "unlocked"
        assert main.attributes["display_on"] is True
        assert state(hass, "binary_sensor", "agent").state == STATE_ON
        await eventually(lambda: value_is(hass, "number", f"{DISPLAY_ID}-brightness", "40"), what="brightness 40")
        device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, AGENT_ID), entry.entry_id)
        assert device is not None
        assert (device.manufacturer, device.model, device.sw_version) == ("Example Computers", "Tower 5", "0.1.0")
        assert device.connections == {(dr.CONNECTION_NETWORK_MAC, MAC)}
        await eventually(lambda: "booted" in watcher.events, what="the booted event")
        code, output = await pc.torporctl(agent.home, "status", "--json")
        report = json.loads(output)
        assert code == 0, output
        assert report["status"]["connection"]["state"] == "connected"
        assert report["status"]["modules"]["keep_awake"]["backend"] == "kde"
        assert [d["id"] for d in report["status"]["displays"]] == [DISPLAY_ID]

        # Brightness: auto on at 100 lx targets 40 %, where the monitor already is; 1000 lx moves it to 65 %.
        await switch(hass, f"{DISPLAY_ID}-auto_brightness", True)
        await eventually(
            lambda: state(hass, "switch", f"{DISPLAY_ID}-auto_brightness").attributes.get("target") == 40,
            what="the agent's target for 100 lx",
        )
        assert pc.ddcutil()["writes"] == []
        set_lux(hass, 1000)
        await eventually(lambda: pc.ddcutil()["writes"] == [[MONITOR["bus"], 65]], what="ddcutil setvcp 10 65")
        await eventually(lambda: value_is(hass, "number", f"{DISPLAY_ID}-brightness", "65"), what="brightness 65")
        auto = state(hass, "switch", f"{DISPLAY_ID}-auto_brightness")
        assert (auto.state, auto.attributes["target"], auto.attributes["overridden"]) == (STATE_ON, 65, False)
        number = entity(hass, "number", f"{DISPLAY_ID}-brightness")
        await hass.services.async_call("number", "set_value", {"entity_id": number, "value": 30}, blocking=True)
        assert pc.ddcutil()["writes"] == [[MONITOR["bus"], 65], [MONITOR["bus"], 30]]
        await eventually(
            lambda: state(hass, "switch", f"{DISPLAY_ID}-auto_brightness").attributes.get("overridden") is True,
            what="a brightness set from Home Assistant to pause auto",
        )
        assert all(call[0] in ("--version", "detect", "--bus") for call in pc.ddcutil()["calls"])

        # Lock, then keep awake while locked: the state follows the fake logind and the inhibit.
        await press(hass, "lock")
        await pc.wait_for_desktop(lambda e: e.get("call") == "Lock", what="Session.Lock")
        await eventually(lambda: value_is(hass, "sensor", "state", "idle"), what="idle once locked")
        assert state(hass, "sensor", "state").attributes["session"] == "locked"
        await switch(hass, "keep_awake", True)
        inhibit = await pc.wait_for_desktop(lambda e: e.get("call") == "PowerDevil.Inhibit", what="the idle inhibit")
        assert inhibit["args"] == ["Torpor", "Kept awake from Home Assistant"]
        await eventually(lambda: value_is(hass, "sensor", "state", "busy"), what="busy while kept awake")
        kept = state(hass, "switch", "keep_awake")
        assert kept.state == STATE_ON
        assert kept.attributes["ends_at"] is not None
        await switch(hass, "keep_awake", False)
        await pc.wait_for_desktop(
            lambda e: e.get("call") == "PowerDevil.UnInhibit" and e["args"] == [inhibit["cookie"]],
            what="the inhibit released",
        )
        await eventually(lambda: value_is(hass, "switch", "keep_awake", STATE_OFF), what="keep awake off")
        await pc.tell_desktop(locked=False)
        await eventually(lambda: value_is(hass, "sensor", "state", "in_use"), what="in_use once unlocked")

        # Sleep from Home Assistant: logind suspends, the agent says goodbye and lets the delay go.
        await press(hass, "sleep")
        suspend = await pc.wait_for_desktop(lambda e: e.get("call") == "Suspend", what="Manager.Suspend")
        assert suspend["args"] == [False]
        await eventually(lambda: value_is(hass, "sensor", "state", "asleep"), what="asleep")
        await pc.wait_for_desktop(lambda e: e.get("released") == delay["delay"], what="the delay inhibitor released")
        await eventually(lambda: agent.connection() == "paused", what="the agent to stop dialing while asleep")
        assert "suspending" in watcher.events
        for platform, key in (("button", "lock"), ("button", "sleep"), ("switch", "keep_awake")):
            assert state(hass, platform, key).state == STATE_UNAVAILABLE
        assert state(hass, "number", f"{DISPLAY_ID}-brightness").state == STATE_UNAVAILABLE
        assert state(hass, "button", "wake").state != STATE_UNAVAILABLE
        assert state(hass, "binary_sensor", "agent").state == STATE_OFF

        # Home Assistant restarts while the PC sleeps (M3): everything is there, from what was stored.
        await ha.stop()
        hass = await ha.start(EntrySnapshot.of(entry))
        watcher = Watcher(hass)
        set_lux(hass, 1000)
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        assert entry.state is ConfigEntryState.LOADED
        expected_entities(hass, entry)
        assert state(hass, "sensor", "state").state == "asleep"
        assert state(hass, "button", "lock").state == STATE_UNAVAILABLE
        assert state(hass, "switch", f"{DISPLAY_ID}-auto_brightness").state == STATE_UNAVAILABLE
        assert state(hass, "button", "wake").state != STATE_UNAVAILABLE
        assert state(hass, "binary_sensor", "agent").state == STATE_OFF

        # Wake: the magic packet reaches the "network card"; the PC resumes and dials the new run.
        await press(hass, "wake")
        await eventually(lambda: len(listener.packets) == 3, what="three magic packets")
        assert set(listener.packets) == {MAGIC}
        await pc.tell_desktop(sleep=False)
        await eventually(lambda: connected(hass), what="the agent to dial at once after resume")
        await eventually(lambda: value_is(hass, "sensor", "state", "in_use"), what="in_use after resume")
        await eventually(lambda: {"wake_succeeded", "resumed"} <= set(watcher.events), what="wake and resume")
        await eventually(
            lambda: len(pc.desktop_events(lambda e: e.get("call") == "Inhibit" and e["sender"] == sender)) == 2,
            what="the delay inhibitor taken again after resume",
        )
        assert state(hass, "switch", f"{DISPLAY_ID}-auto_brightness").state == STATE_ON

        # An agent with an old key (M1): it gives up after Home Assistant's proof, and nothing is banned.
        stale_home = tmp_path / "stale-home"
        pc.wrong_key_home(agent.home, stale_home)
        stale = await pc.start_agent(stale_home)
        await eventually(lambda: stale.connection() == "rejected", what="the old key to be rejected")
        assert stale.status()["connection"]["last_close"]["code"] == protocol.Close.AUTH_FAILED
        code, output = await pc.torporctl(stale_home, "status")
        assert code == 5, output
        assert "re-pair needed" in output
        await eventually(
            lambda: hass.data[DATA].handshakes[AGENT_ID]["stale_key"] == 1, what="Home Assistant to count a stale key"
        )
        assert not hass.http.app[KEY_FAILED_LOGIN_ATTEMPTS]
        assert not hass.http.app[KEY_BAN_MANAGER].ip_bans_lookup
        assert not (ha.config_dir / "ip_bans.yaml").exists()
        assert connected(hass)
        stopped = await stale.stop()
        assert stopped == 0

        # Home Assistant restarts while the PC is awake: it says goodbye at once, and the agent comes back by
        # itself, not banned.
        before = time.monotonic()
        await ha.stop()
        assert time.monotonic() - before < 5
        assert entry.runtime_data.stats.close_code == 1001
        hass = await ha.start(EntrySnapshot.of(entry))
        set_lux(hass, 1000)
        await eventually(lambda: connected(hass), what="the agent to reconnect after a restart", timeout=30)
        await eventually(lambda: value_is(hass, "sensor", "state", "in_use"), what="in_use after the restart")
        assert not (ha.config_dir / "ip_bans.yaml").exists()
        # in_use can still be the restored state; the switch needs the agent's first report.
        await eventually(
            lambda: value_is(hass, "switch", f"{DISPLAY_ID}-auto_brightness", STATE_ON), what="auto brightness back on"
        )
        await eventually(lambda: value_is(hass, "number", f"{DISPLAY_ID}-brightness", "30"), what="brightness 30")

        # torpord stops cleanly: goodbye with 1001, the delay inhibitor let go, exit 0.
        delays = pc.desktop_events(lambda e: e.get("call") == "Inhibit" and e["sender"] == sender)
        code = await agent.stop()
        assert code == 0
        await pc.wait_for_desktop(lambda e: e.get("released") == delays[-1]["delay"], what="the last delay released")
        await eventually(lambda: not connected(hass), what="Home Assistant to see the agent go")
        assert hass.config_entries.async_entries(DOMAIN)[0].runtime_data.stats.close_code == 1001
        assert agent.connection() == "stopped"
        from_agent, from_ha = frames.check()
        assert {"capabilities", "state", "event", "going_down", "result", "ack"} <= from_agent
        assert {"auth_ok", "config", "input", "command", "ack", "paired"} <= from_ha
        for log in (agent.output(), stale.output()):
            assert "Traceback" not in log
            assert "unexpected error" not in log
    except BaseException:
        if agent is not None:
            print("torpord log:\n" + agent.output())
        raise
    finally:
        transport.close()
        await pc.close()
        await ha.stop()


def mode_reads(pc: FakePC) -> list[tuple[float, list[str]]]:
    state = pc.ddcutil()
    return [(at, call) for at, call in zip(state["times"], state["calls"], strict=True) if call[0] == "--noconfig"]


async def test_a_monitor_mode_and_a_game_drive_the_brightness_through_the_real_agent(
    tools: Tools, pyz: Path, tmp_path: Path, hass_storage: dict[str, Any], socket_enabled: None
) -> None:
    """The PG39WCDM's Power Setting (VCP 0xE1) read by the real torpord with the real ddcutil options, and a
    GameMode client, move the brightness the agent writes; Home Assistant shows both."""
    pc = FakePC(tmp_path / "pc", tools, pyz)
    ha = HomeAssistantRun(tmp_path / "ha", free_port())
    agent: Agent | None = None
    rows = [
        {"display": DISPLAY_ID, "code": "E1", "value": "00", "anchor_pct": 40, "label": "Standard"},
        {"display": DISPLAY_ID, "code": "E1", "value": "01", "anchor_pct": 60, "label": "Power Saving"},
    ]
    try:
        await pc.start()
        await pc.tell_desktop(gamemode=True)
        await pc.wait_for_desktop(lambda e: e.get("gamemode") is True, what="gamemoded on the session bus")
        hass = await ha.start()
        set_lux(hass, 1000)
        agent = await pc.start_agent(tmp_path / "home")
        await eventually(lambda: agent.connection() == "not_paired", what="torpord to wait for a key")
        entry = await pair(hass, ha, pc, agent, free_port(), game_boost=30, modes=rows)
        assert entry.options["brightness"]["modes"][1] == {
            "display": DISPLAY_ID,
            "code": "0xE1",
            "value": "0x01",
            "anchor_pct": 60,
            "label": "Power Saving",
        }
        await eventually(lambda: connected(hass), what="torpord to connect")
        mode, game = f"{DISPLAY_ID}-mode", "game"

        # Standard: the first read finds it, and auto writes the Standard curve, 65 % at 1000 lx.
        await eventually(lambda: value_is(hass, "sensor", mode, "Standard"), what="the mode sensor to read Standard")
        assert state(hass, "sensor", mode).attributes == {
            **state(hass, "sensor", mode).attributes,
            "code": "0xE1",
            "value": "0x00",
            "error": None,
        }
        assert value_is(hass, "binary_sensor", game, STATE_OFF)
        await switch(hass, f"{DISPLAY_ID}-auto_brightness", True)
        await eventually(lambda: pc.ddcutil()["writes"] == [[MONITOR["bus"], 65]], what="ddcutil setvcp 10 65")

        # Power Saving in the OSD: the next read sees it and the agent writes its curve, capped at 80 %.
        pc.osd(0xE1, 1)
        await eventually(lambda: value_is(hass, "sensor", mode, "Power Saving"), what="the mode sensor to follow")
        await eventually(lambda: pc.ddcutil()["writes"][-1] == [MONITOR["bus"], 80], what="the Power Saving curve")
        await eventually(
            lambda: state(hass, "switch", f"{DISPLAY_ID}-auto_brightness").attributes["target"] == 80,
            what="the target to follow the mode",
        )
        assert state(hass, "switch", f"{DISPLAY_ID}-auto_brightness").attributes["overridden"] is False

        # A game: +30 on top of the ceiling, up to 100; its end takes it back.
        await pc.tell_desktop(game=True)
        await eventually(lambda: value_is(hass, "binary_sensor", game, STATE_ON), what="Game running on")
        await eventually(lambda: pc.ddcutil()["writes"][-1] == [MONITOR["bus"], 100], what="the game boost")
        await pc.tell_desktop(game=False)
        await eventually(lambda: value_is(hass, "binary_sensor", game, STATE_OFF), what="Game running off")
        await eventually(lambda: pc.ddcutil()["writes"][-1] == [MONITOR["bus"], 80], what="the boost undone")
        assert [value for _bus, value in pc.ddcutil()["writes"]] == [65, 80, 100, 80]

        # Every mode read used the safe options, and none started right after a write.
        options = ["--noconfig", "--disable-udf", "--enable-cross-instance-locks", "--disable-dynamic-sleep"]
        reads = mode_reads(pc)
        assert reads
        for _at, call in reads:
            assert call[:4] == options
            assert call[4:] in (
                ["detect", "--terse"],
                ["--skip-ddc-checks", "getvcp", "--bus", "4", "--verbose", "0xE1"],
            )
        state_now = pc.ddcutil()
        write_times = [at for at, call in zip(state_now["times"], state_now["calls"], strict=True) if "setvcp" in call]
        for at, _call in reads:
            assert all(not 0 <= at - written < 2 for written in write_times), "a mode read inside the quiet window"
        code, output = await pc.torporctl(agent.home, "status", "--json")
        report = json.loads(output)
        assert code == 0, output
        assert report["status"]["displays"][0]["mode"] == {"value": 1, "error": None, "name": "0x01 (Power Saving)"}
        assert report["status"]["state"]["game"] is False
        assert report["status"]["modules"]["brightness"]["available"] is True

        code = await agent.stop()
        assert code == 0
        assert "Traceback" not in agent.output()
        assert "unexpected error" not in agent.output()
    except BaseException:
        if agent is not None:
            print("torpord log:\n" + agent.output())
        raise
    finally:
        await pc.close()
        await ha.stop()
