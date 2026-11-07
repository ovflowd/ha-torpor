"""Monitor modes and the game boost: the options, what config sends, the Mode and Game running entities, and the
repair when the PC can't read modes."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant.common import MockConfigEntry

from custom_components.torpor.capabilities import Capabilities
from custom_components.torpor.const import DOMAIN
from custom_components.torpor.options import Settings, parse_hex

from .conftest import AgentFactory, entry_options, make_entry
from .fake_agent import DISPLAY_ID, OTHER_AGENT_ID, OTHER_DISPLAY_ID, FakeAgent, capabilities, state

ASUS = [
    {"display": DISPLAY_ID, "code": "E1", "value": "00", "anchor_pct": 40, "label": "Standard"},
    {"display": DISPLAY_ID, "code": "0xe1", "value": "0x01", "anchor_pct": 75, "label": "Power Saving"},
]
MODE = "sensor.desk_example_27q_mode"
GAME = "binary_sensor.desk_game_running"


def form(**brightness: Any) -> dict[str, Any]:
    return {
        "modules": {"power": True, "keep_awake": True, "wake": True, "brightness": True},
        "wake": {"macs": ["02:00:5e:10:00:01"], "broadcast": "255.255.255.255", "port": 9, "timeout": 120},
        "keep_awake": {"hours": 12},
        "brightness": {"anchor_lux": 100, "anchor_pct": 40, "slope": 25, "floor": 10, "ceiling": 80, **brightness},
        "advanced": {"allow_remote": False},
    }


def stored(rows: list[dict[str, Any]] | None = None, boost: int = 30) -> dict[str, Any]:
    normalized = [
        {"display": DISPLAY_ID, "code": "0xE1", "value": "0x00", "anchor_pct": 40, "label": "Standard"},
        {"display": DISPLAY_ID, "code": "0xE1", "value": "0x01", "anchor_pct": 75, "label": "Power Saving"},
    ]
    return entry_options(brightness={"game_boost": boost, "modes": normalized if rows is None else rows})


def games_caps(*sources: str) -> dict[str, Any]:
    return capabilities(
        core={"enabled": True, "available": True, "backend": "logind", "problems": [], "games": list(sources)}
    )


def mode_display(value: int | None, error: str | None = None) -> dict[str, Any]:
    display = {"percent": 75, "auto": True, "overridden": False, "target": 75, "error": None}
    return {"lux": 84.0, "displays": {DISPLAY_ID: {**display, "mode": {"value": value, "error": error}}}}


async def loaded(
    hass: HomeAssistant, options: dict[str, Any], caps: dict[str, Any] | None = None, *, agent_id: str | None = None
) -> MockConfigEntry:
    entry = make_entry(options=options, caps=caps, **({"agent_id": agent_id} if agent_id else {}))
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def connected(new_agent: AgentFactory, **changes: Any) -> FakeAgent:
    pc = await new_agent()
    caps = changes.pop("caps", None)
    await pc.connect(caps=caps, **changes)
    await pc.sync()
    return pc


def test_codes_and_values_read_as_ddcutil_writes_them() -> None:
    assert [parse_hex(text, 0xFF) for text in ("E1", "e1", "0xE1", " 0x01 ", "1")] == [0xE1, 0xE1, 0xE1, 1, 1]
    assert [parse_hex(text, 0xFF) for text in ("", "0x", "G1", "100", "0x1FF", None, 225)] == [None] * 7
    assert parse_hex("FFFF", 0xFFFF) == 0xFFFF


# Options.


async def test_the_options_store_mode_rows_and_the_boost_normalized(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    result = await hass.config_entries.options.async_init(loaded_entry.entry_id)
    fields = {str(key) for key in result["data_schema"].schema["brightness"].schema.schema}
    assert {"game_boost", "modes"} <= fields
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], form(game_boost=45, modes=[*ASUS, {"display": DISPLAY_ID, "code": "E1"}])
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    brightness = loaded_entry.options["brightness"]
    assert brightness["game_boost"] == 45
    assert brightness["modes"] == [
        {"display": DISPLAY_ID, "code": "0xE1", "value": "0x00", "anchor_pct": 40, "label": "Standard"},
        {"display": DISPLAY_ID, "code": "0xE1", "value": "0x01", "anchor_pct": 75, "label": "Power Saving"},
        {"display": DISPLAY_ID, "code": "0xE1"},
    ]


@pytest.mark.parametrize(
    ("rows", "error"),
    [
        ([{"display": DISPLAY_ID, "code": "XYZ"}], "invalid_mode_code"),
        ([{"display": DISPLAY_ID, "code": "1E1"}], "invalid_mode_code"),
        ([{"display": DISPLAY_ID, "code": "E1", "value": "10000", "anchor_pct": 40}], "invalid_mode_value"),
        ([{"display": DISPLAY_ID, "code": "E1", "value": "01"}], "mode_value_without_anchor"),
        ([{"display": DISPLAY_ID, "code": "E1", "anchor_pct": 40}], "mode_value_without_anchor"),
        ([ASUS[0], {**ASUS[1], "code": "DC"}], "mode_codes_differ"),
        ([ASUS[0], {**ASUS[1], "value": "0"}], "duplicate_mode_value"),
    ],
)
async def test_the_options_check_mode_rows(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, rows: list[dict[str, Any]], error: str
) -> None:
    result = await hass.config_entries.options.async_init(loaded_entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], form(modes=rows))
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}


async def test_modes_are_offered_only_for_monitors_the_pc_can_read(hass: HomeAssistant) -> None:
    caps = capabilities()
    caps["modules"]["brightness"]["displays"] = [
        {"id": DISPLAY_ID, "label": "Example 27Q", "backend": "kde", "internal": False, "modes": True},
        {"id": OTHER_DISPLAY_ID, "label": "Laptop panel", "backend": "logind", "internal": True, "modes": False},
    ]
    entry = await loaded(hass, entry_options(), caps)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    brightness = result["data_schema"].schema["brightness"].schema.schema
    modes = next(key for key in brightness if str(key) == "modes")
    display = brightness[modes].config["fields"]["display"]["selector"]
    assert [option["value"] for option in display.config["options"]] == [DISPLAY_ID]

    caps["modules"]["brightness"]["displays"] = caps["modules"]["brightness"]["displays"][1:]
    entry = await loaded(hass, entry_options(), caps, agent_id=OTHER_AGENT_ID)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "modes" not in {str(key) for key in result["data_schema"].schema["brightness"].schema.schema}


async def test_mode_rows_show_every_field_and_the_help_names_each_display(hass: HomeAssistant) -> None:
    """The frontend draws a row from its fields only, and shows a select's raw value: without a label field the
    row lists them all, so a code-only row isn't blank, and the help says which display each ID is."""
    caps = capabilities()
    caps["modules"]["brightness"]["displays"] = [
        {"id": DISPLAY_ID, "label": "Example 27Q", "backend": "kde", "internal": False, "modes": True},
        {"id": OTHER_DISPLAY_ID, "label": "Example 32U", "backend": "kde", "internal": False, "modes": True},
    ]
    entry = await loaded(hass, entry_options(), caps)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    brightness = result["data_schema"].schema["brightness"].schema.schema
    modes = brightness[next(key for key in brightness if str(key) == "modes")]
    assert "label_field" not in modes.config
    assert modes.config["fields"]["anchor_pct"]["selector"].config["min"] == 1
    assert result["description_placeholders"]["mode_displays"] == (
        f"{DISPLAY_ID}: Example 27Q, {OTHER_DISPLAY_ID}: Example 32U"
    )
    caps["modules"]["brightness"]["displays"] = []
    other = await loaded(hass, entry_options(), caps, agent_id=OTHER_AGENT_ID)
    result = await hass.config_entries.options.async_init(other.entry_id)
    assert result["description_placeholders"]["mode_displays"] == "-"


async def test_a_row_for_a_display_the_pc_no_longer_reports_stays_editable(hass: HomeAssistant) -> None:
    gone = [{"display": OTHER_DISPLAY_ID, "code": "0xE1", "value": "0x01", "anchor_pct": 75}]
    entry = await loaded(hass, stored(gone))
    result = await hass.config_entries.options.async_init(entry.entry_id)
    brightness = result["data_schema"].schema["brightness"].schema.schema
    modes = next(key for key in brightness if str(key) == "modes")
    options = brightness[modes].config["fields"]["display"]["selector"].config["options"]
    assert [option["value"] for option in options] == [DISPLAY_ID, OTHER_DISPLAY_ID]
    result = await hass.config_entries.options.async_configure(result["flow_id"], form(modes=gone))
    assert result["type"] is FlowResultType.CREATE_ENTRY


def test_settings_skip_malformed_rows() -> None:
    rows = [
        {"display": DISPLAY_ID, "code": "0xE1", "value": "0x01", "anchor_pct": 75, "label": "Power Saving"},
        {"display": DISPLAY_ID, "code": "nope"},
        {"code": "0xE1"},
        "junk",
    ]
    settings = Settings.from_options(entry_options(brightness={"modes": rows}))
    assert [(row.code, row.value, row.anchor_pct, row.label) for row in settings.modes] == [
        (0xE1, 1, 75, "Power Saving")
    ]
    assert settings.mode_code(DISPLAY_ID) == 0xE1 and settings.mode_code(OTHER_DISPLAY_ID) is None
    assert Settings.from_options({}).game_boost == 30


# What the PC gets.


async def test_config_carries_the_boost_and_each_displays_code_and_anchors(
    hass: HomeAssistant, new_agent: AgentFactory
) -> None:
    await loaded(hass, stored([*stored()["brightness"]["modes"], {"display": DISPLAY_ID, "code": "0xE1"}], boost=45))
    pc = await new_agent()
    await pc.connect()
    config = await pc.expect("config")
    assert config["brightness"]["game_boost"] == 45
    assert config["brightness"]["modes"] == {
        DISPLAY_ID: {
            "code": 0xE1,
            "values": [
                {"value": 0, "anchor_pct": 40, "label": "Standard"},
                {"value": 1, "anchor_pct": 75, "label": "Power Saving"},
            ],
        }
    }


# The Mode sensor.


async def test_the_mode_sensor_shows_the_label_or_the_raw_value(hass: HomeAssistant, new_agent: AgentFactory) -> None:
    await loaded(hass, stored())
    registry = er.async_get(hass)
    found = registry.async_get(MODE)
    assert found is not None and found.entity_category is er.EntityCategory.DIAGNOSTIC
    assert hass.states.get(MODE).state == STATE_UNAVAILABLE  # type: ignore[union-attr]

    pc = await connected(new_agent, brightness=mode_display(None))
    await hass.async_block_till_done()
    assert hass.states.get(MODE).state == STATE_UNKNOWN  # type: ignore[union-attr]

    await pc.send(state(brightness=mode_display(1)))
    await pc.sync()
    await hass.async_block_till_done()
    sensor = hass.states.get(MODE)
    assert sensor is not None and sensor.state == "Power Saving"
    assert sensor.attributes["code"] == "0xE1" and sensor.attributes["value"] == "0x01"
    assert sensor.attributes["error"] is None

    failed = "ddcutil getvcp timed out"
    await pc.send(state(brightness=mode_display(1, failed)))
    await pc.sync()
    await hass.async_block_till_done()
    assert hass.states.get(MODE).attributes["error"] == failed  # type: ignore[union-attr]

    await pc.send(state(brightness=mode_display(5)))
    await pc.sync()
    await hass.async_block_till_done()
    assert hass.states.get(MODE).state == "0x05"  # type: ignore[union-attr]


async def test_the_mode_sensor_exists_only_for_a_display_with_a_code(hass: HomeAssistant) -> None:
    await loaded(hass, entry_options())
    assert er.async_get(hass).async_get(MODE) is None


# Game running.


async def test_game_running_follows_the_pc_and_exists_only_with_a_source(
    hass: HomeAssistant, new_agent: AgentFactory
) -> None:
    entry = await loaded(hass, entry_options(), games_caps("gamemode"))
    assert hass.states.get(GAME).state == STATE_UNAVAILABLE  # type: ignore[union-attr]
    pc = await connected(new_agent, caps=games_caps("gamemode"), game=False)
    await hass.async_block_till_done()
    assert hass.states.get(GAME).state == STATE_OFF  # type: ignore[union-attr]
    assert hass.states.get(GAME).attributes["device_class"] == "running"  # type: ignore[union-attr]
    await pc.send(state(game=True))
    await pc.sync()
    await hass.async_block_till_done()
    assert hass.states.get(GAME).state == STATE_ON  # type: ignore[union-attr]
    await pc.send(state(game=None))
    await pc.sync()
    await hass.async_block_till_done()
    assert hass.states.get(GAME).state == STATE_UNAVAILABLE  # type: ignore[union-attr]

    await pc.send({"type": "capabilities", **games_caps()})
    await pc.sync()
    await hass.async_block_till_done()
    assert er.async_get(hass).async_get(GAME) is None
    assert entry.data["capabilities"]["modules"]["core"]["games"] == []


# The repair.


def issue(hass: HomeAssistant, entry: MockConfigEntry, key: str = "modes_unavailable") -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{key}_{entry.entry_id}")


async def test_a_pc_that_cannot_read_modes_raises_a_repair_until_it_can(
    hass: HomeAssistant, new_agent: AgentFactory
) -> None:
    caps = capabilities()
    caps["modules"]["brightness"]["modes"] = {
        "available": False,
        "problems": [{"code": "old_ddcutil", "message": "ddcutil 1.4.1 can't share the bus safely", "hint": None}],
    }
    entry = await loaded(hass, stored(), caps)
    found = issue(hass, entry)
    assert found is not None and found.translation_placeholders == {
        "name": "desk",
        "problems": "ddcutil 1.4.1 can't share the bus safely",
    }
    await connected(new_agent)
    await hass.async_block_till_done()
    assert issue(hass, entry) is None


async def test_a_pc_still_checking_its_ddcutil_raises_no_repair(hass: HomeAssistant) -> None:
    caps = capabilities()
    caps["modules"]["brightness"]["modes"] = {"available": False, "problems": []}
    entry = await loaded(hass, stored(), caps)
    assert issue(hass, entry) is None


async def test_an_agent_that_does_not_report_modes_raises_its_own_repair_only_with_rows(hass: HomeAssistant) -> None:
    """An agent older than mode reads ignores the rows and uses the curve's own anchor; the fix is updating it,
    not ddcutil, so it gets its own translated issue."""
    caps = capabilities()
    del caps["modules"]["brightness"]["modes"]
    parsed = Capabilities(caps)
    assert parsed.modes_reported is False and parsed.modes_problems == []
    entry = await loaded(hass, entry_options(), caps)
    assert issue(hass, entry) is None and issue(hass, entry, "modes_agent_outdated") is None
    other = await loaded(hass, stored(), caps, agent_id=OTHER_AGENT_ID)
    assert issue(hass, other) is None
    outdated = issue(hass, other, "modes_agent_outdated")
    assert outdated is not None and outdated.translation_placeholders == {"name": "desk"}


def test_malformed_mode_and_game_capabilities_read_as_absent() -> None:
    caps = capabilities()
    caps["modules"]["brightness"]["modes"] = {"available": "yes", "problems": "none"}
    caps["modules"]["core"]["games"] = "gamemode"
    parsed = Capabilities(caps)
    assert parsed.modes_available is False
    assert parsed.modes_reported is True
    assert parsed.modes_problems == []
    assert parsed.games == []


async def test_brightness_off_on_the_pc_raises_its_own_repair_not_the_modes_one(hass: HomeAssistant) -> None:
    caps = capabilities(brightness={"enabled": False, "available": False, "backend": None, "problems": []})
    entry = await loaded(hass, stored(), caps)
    assert issue(hass, entry) is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"module_unavailable_brightness_{entry.entry_id}") is not None
