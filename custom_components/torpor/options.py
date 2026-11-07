"""The options schema shared by the pairing flow's features step and the options flow."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import probatio
from homeassistant.data_entry_flow import section
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    ObjectSelector,
    ObjectSelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .capabilities import Capabilities
from .const import (
    CONF_ALLOW_REMOTE,
    CONF_ANCHOR_LUX,
    CONF_ANCHOR_PCT,
    CONF_BROADCAST,
    CONF_CEILING,
    CONF_FLOOR,
    CONF_GAME_BOOST,
    CONF_HOURS,
    CONF_LUX_ENTITY,
    CONF_MACS,
    CONF_MODES,
    CONF_PORT,
    CONF_SLOPE,
    CONF_TIMEOUT,
    DEFAULT_BROADCAST,
    DEFAULT_CURVE,
    DEFAULT_GAME_BOOST,
    DEFAULT_KEEP_AWAKE_HOURS,
    DEFAULT_PORT,
    DEFAULT_WAKE_TIMEOUT,
    HA_MODULES,
    MODE_ANCHOR,
    MODE_CODE,
    MODE_DISPLAY,
    MODE_LABEL,
    MODE_VALUE,
    SECTION_ADVANCED,
    SECTION_BRIGHTNESS,
    SECTION_KEEP_AWAKE,
    SECTION_MODULES,
    SECTION_WAKE,
)

_HEX = re.compile(r"(?:0x)?([0-9a-f]{1,4})")


def parse_hex(value: object, limit: int) -> int | None:
    """A VCP code or value as ddcutil and monitor manuals write it: E1, 0xE1, 01."""
    match = _HEX.fullmatch(value.strip().lower()) if isinstance(value, str) else None
    number = int(match.group(1), 16) if match else None
    return number if number is not None and number <= limit else None


def hex_text(number: int) -> str:
    return f"0x{number:02X}"


@dataclass(frozen=True, slots=True)
class ModeRow:
    """One row of the options: on this display, this value of this code puts the curve's anchor here."""

    display: str
    code: int
    value: int | None
    anchor_pct: int | None
    label: str


def _mode_rows(rows: object) -> tuple[ModeRow, ...]:
    found: list[ModeRow] = []
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, Mapping) or not isinstance(row.get(MODE_DISPLAY), str):
            continue
        code = parse_hex(row.get(MODE_CODE), 0xFF)
        if code is None:
            continue
        value = parse_hex(row.get(MODE_VALUE), 0xFFFF)
        anchor = row.get(MODE_ANCHOR)
        label = row.get(MODE_LABEL)
        found.append(
            ModeRow(
                display=row[MODE_DISPLAY],
                code=code,
                value=value,
                anchor_pct=int(anchor) if isinstance(anchor, int | float) and not isinstance(anchor, bool) else None,
                label=label if isinstance(label, str) else "",
            )
        )
    return tuple(found)


@dataclass(frozen=True, slots=True)
class Settings:
    """The stored options, with defaults for anything missing."""

    modules: Mapping[str, bool]
    macs: tuple[str, ...]
    broadcast: str
    port: int
    wake_timeout: float
    keep_awake_hours: float
    lux_entity: str | None
    curve: Mapping[str, float]
    game_boost: int
    modes: tuple[ModeRow, ...]
    allow_remote: bool

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> Settings:
        modules = _part(options, SECTION_MODULES)
        wake = _part(options, SECTION_WAKE)
        brightness = _part(options, SECTION_BRIGHTNESS)
        return cls(
            modules={name: modules.get(name) is True for name in HA_MODULES},
            macs=tuple(wake.get(CONF_MACS) or ()),
            broadcast=wake.get(CONF_BROADCAST, DEFAULT_BROADCAST),
            port=int(wake.get(CONF_PORT, DEFAULT_PORT)),
            wake_timeout=float(wake.get(CONF_TIMEOUT, DEFAULT_WAKE_TIMEOUT)),
            keep_awake_hours=float(_part(options, SECTION_KEEP_AWAKE).get(CONF_HOURS, DEFAULT_KEEP_AWAKE_HOURS)),
            lux_entity=brightness.get(CONF_LUX_ENTITY) or None,
            curve={key: brightness.get(key, default) for key, default in DEFAULT_CURVE.items()},
            game_boost=int(brightness.get(CONF_GAME_BOOST, DEFAULT_GAME_BOOST)),
            modes=_mode_rows(brightness.get(CONF_MODES)),
            allow_remote=_part(options, SECTION_ADVANCED).get(CONF_ALLOW_REMOTE) is True,
        )

    def module_on(self, name: str) -> bool:
        return self.modules.get(name, False)

    def mode_code(self, display_id: str) -> int | None:
        return next((row.code for row in self.modes if row.display == display_id), None)

    def mode_rows(self, display_id: str) -> list[ModeRow]:
        return [row for row in self.modes if row.display == display_id]


def _part(options: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = options.get(name)
    return value if isinstance(value, Mapping) else {}


def _number(low: float, high: float, step: float = 1, unit: str | None = None) -> NumberSelector:
    config = NumberSelectorConfig(min=low, max=high, step=step, mode=NumberSelectorMode.BOX)
    if unit is not None:
        config["unit_of_measurement"] = unit
    return NumberSelector(config)


MODULE_NAMES = {"power": "Power", "keep_awake": "Keep awake", "wake": "Wake-on-LAN", "brightness": "Brightness"}


def _nic_label(name: str, mac: str, wol: str) -> str:
    return f"{name} ({mac}, Wake-on-LAN {wol})"


def default_options(capabilities: Capabilities) -> dict[str, Any]:
    primary = [nic.mac for nic in capabilities.nics if nic.primary]
    return {
        SECTION_MODULES: {name: capabilities.offered(name) for name in HA_MODULES},
        SECTION_WAKE: {
            CONF_MACS: primary,
            CONF_BROADCAST: DEFAULT_BROADCAST,
            CONF_PORT: DEFAULT_PORT,
            CONF_TIMEOUT: DEFAULT_WAKE_TIMEOUT,
        },
        SECTION_KEEP_AWAKE: {CONF_HOURS: DEFAULT_KEEP_AWAKE_HOURS},
        SECTION_BRIGHTNESS: {**DEFAULT_CURVE, CONF_GAME_BOOST: DEFAULT_GAME_BOOST, CONF_MODES: []},
        SECTION_ADVANCED: {CONF_ALLOW_REMOTE: False},
    }


def offered_modules(capabilities: Capabilities, current: Mapping[str, Any]) -> list[str]:
    """Modules with a toggle: what the PC offers, plus any still turned on so it can be turned off."""
    settings = Settings.from_options(current)
    return [name for name in HA_MODULES if capabilities.offered(name) or settings.module_on(name)]


def unavailable_text(capabilities: Capabilities) -> str:
    lines = []
    for name in HA_MODULES:
        if capabilities.offered(name):
            continue
        reasons = capabilities.problems(name) or (["turned off on the PC"] if not capabilities.enabled(name) else [])
        lines.append(f"- {MODULE_NAMES[name]}: {'; '.join(reasons) or 'not available'}")
    return "\n".join(lines) or "-"


def mode_display_options(capabilities: Capabilities, current: Mapping[str, Any]) -> list[SelectOptionDict]:
    """The displays a mode row can name: those the PC can read over DDC/CI, and any a row still names."""
    options = [
        SelectOptionDict(value=display.id, label=display.label) for display in capabilities.displays if display.modes
    ]
    for row in Settings.from_options(current).modes:
        if all(option["value"] != row.display for option in options):
            options.append(SelectOptionDict(value=row.display, label=row.display))
    return options


def mode_displays_text(capabilities: Capabilities, current: Mapping[str, Any]) -> str:
    """The list of mode rows can only show a display by its ID, so the field's help names each one."""
    return ", ".join(f"{o['value']}: {o['label']}" for o in mode_display_options(capabilities, current)) or "-"


def _modes_selector(display_options: list[SelectOptionDict]) -> ObjectSelector:
    return ObjectSelector(
        ObjectSelectorConfig(
            fields={
                MODE_DISPLAY: {
                    "selector": SelectSelector(
                        SelectSelectorConfig(options=display_options, mode=SelectSelectorMode.DROPDOWN)
                    ),
                    "required": True,
                },
                MODE_CODE: {"selector": TextSelector(), "required": True},
                MODE_VALUE: {"selector": TextSelector()},
                MODE_ANCHOR: {"selector": _number(1, 100, unit="%")},
                MODE_LABEL: {"selector": TextSelector()},
            },
            multiple=True,
            translation_key=CONF_MODES,
        )
    )


def build_schema(capabilities: Capabilities, current: Mapping[str, Any]) -> probatio.Schema:
    """The form for the features step and the options flow. `current` adds toggles and MACs still in use."""
    nic_options = [
        SelectOptionDict(value=nic.mac, label=_nic_label(nic.name, nic.mac, nic.wol)) for nic in capabilities.nics
    ]
    known = {nic.mac for nic in capabilities.nics}
    nic_options += [
        SelectOptionDict(value=mac, label=_nic_label(mac, mac, "unknown"))
        for mac in Settings.from_options(current).macs
        if mac not in known
    ]
    display_options = mode_display_options(capabilities, current)
    schema: dict[Any, Any] = {}
    toggles = offered_modules(capabilities, current)
    if toggles:
        schema[probatio.Required(SECTION_MODULES)] = section(
            probatio.Schema(
                {probatio.Required(name, default=capabilities.offered(name)): BooleanSelector() for name in toggles}
            )
        )
    schema[probatio.Required(SECTION_WAKE)] = section(
        probatio.Schema(
            {
                probatio.Optional(CONF_MACS, default=list): SelectSelector(
                    SelectSelectorConfig(options=nic_options, multiple=True, mode=SelectSelectorMode.LIST)
                ),
                probatio.Required(CONF_BROADCAST, default=DEFAULT_BROADCAST): TextSelector(),
                probatio.Required(CONF_PORT, default=DEFAULT_PORT): _number(1, 65535),
                probatio.Required(CONF_TIMEOUT, default=DEFAULT_WAKE_TIMEOUT): _number(10, 600, unit="s"),
            }
        )
    )
    schema[probatio.Required(SECTION_KEEP_AWAKE)] = section(
        probatio.Schema({probatio.Required(CONF_HOURS, default=DEFAULT_KEEP_AWAKE_HOURS): _number(0, 168, unit="h")})
    )
    schema[probatio.Required(SECTION_BRIGHTNESS)] = section(
        probatio.Schema(
            {
                probatio.Optional(CONF_LUX_ENTITY): EntitySelector(
                    EntitySelectorConfig(domain="sensor", device_class="illuminance")
                ),
                probatio.Required(CONF_ANCHOR_LUX, default=DEFAULT_CURVE[CONF_ANCHOR_LUX]): _number(
                    1, 100000, unit="lx"
                ),
                probatio.Required(CONF_ANCHOR_PCT, default=DEFAULT_CURVE[CONF_ANCHOR_PCT]): _number(1, 100, unit="%"),
                probatio.Required(CONF_SLOPE, default=DEFAULT_CURVE[CONF_SLOPE]): _number(0, 100, step=0.5),
                probatio.Required(CONF_FLOOR, default=DEFAULT_CURVE[CONF_FLOOR]): _number(0, 100, unit="%"),
                probatio.Required(CONF_CEILING, default=DEFAULT_CURVE[CONF_CEILING]): _number(0, 100, unit="%"),
                probatio.Required(CONF_GAME_BOOST, default=DEFAULT_GAME_BOOST): _number(0, 100, unit="%"),
                **(
                    {probatio.Optional(CONF_MODES, default=list): _modes_selector(display_options)}
                    if display_options
                    else {}
                ),
            }
        ),
        {"collapsed": True},
    )
    schema[probatio.Required(SECTION_ADVANCED)] = section(
        probatio.Schema({probatio.Required(CONF_ALLOW_REMOTE, default=False): BooleanSelector()}),
        {"collapsed": True},
    )
    return probatio.Schema(schema)


def _check_modes(rows: Sequence[Mapping[str, Any]], errors: dict[str, str]) -> None:
    """One code per display, each value once, and a value only with its anchor."""
    codes: dict[str, int] = {}
    seen: set[tuple[str, int]] = set()
    for row in rows:
        code = parse_hex(row.get(MODE_CODE), 0xFF)
        text = str(row.get(MODE_VALUE) or "").strip()
        value = parse_hex(text, 0xFFFF) if text else None
        display = str(row.get(MODE_DISPLAY))
        if code is None:
            errors["base"] = "invalid_mode_code"
        elif text and value is None:
            errors["base"] = "invalid_mode_value"
        elif (value is None) != (row.get(MODE_ANCHOR) is None):
            errors["base"] = "mode_value_without_anchor"
        elif codes.setdefault(display, code) != code:
            errors["base"] = "mode_codes_differ"
        elif value is not None and (display, value) in seen:
            errors["base"] = "duplicate_mode_value"
        else:
            if value is not None:
                seen.add((display, value))
            continue
        return


def validate(user_input: Mapping[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    brightness = _part(user_input, SECTION_BRIGHTNESS)
    wake = _part(user_input, SECTION_WAKE)
    _check_modes(brightness.get(CONF_MODES) or [], errors)
    if errors:
        return errors
    if brightness.get(CONF_CEILING, 0) < brightness.get(CONF_FLOOR, 0):
        errors["base"] = "ceiling_below_floor"
    elif _part(user_input, SECTION_MODULES).get("wake") and not wake.get(CONF_MACS):
        errors["base"] = "no_mac_selected"
    else:
        try:
            ipaddress.IPv4Address(str(wake.get(CONF_BROADCAST, "")).strip())
        except ValueError:
            errors["base"] = "invalid_broadcast"
    return errors


def _mode_option(row: ModeRow) -> dict[str, Any]:
    option: dict[str, Any] = {MODE_DISPLAY: row.display, MODE_CODE: hex_text(row.code)}
    if row.value is not None and row.anchor_pct is not None:
        option[MODE_VALUE] = hex_text(row.value)
        option[MODE_ANCHOR] = row.anchor_pct
    if row.label.strip():
        option[MODE_LABEL] = row.label.strip()
    return option


def normalize(user_input: Mapping[str, Any]) -> dict[str, Any]:
    """The options to store: every module present, numbers as the right type, no empty lux entity."""
    modules = _part(user_input, SECTION_MODULES)
    wake = _part(user_input, SECTION_WAKE)
    brightness = _part(user_input, SECTION_BRIGHTNESS)
    curve: dict[str, Any] = {
        CONF_ANCHOR_LUX: float(brightness[CONF_ANCHOR_LUX]),
        CONF_ANCHOR_PCT: int(brightness[CONF_ANCHOR_PCT]),
        CONF_SLOPE: float(brightness[CONF_SLOPE]),
        CONF_FLOOR: int(brightness[CONF_FLOOR]),
        CONF_CEILING: int(brightness[CONF_CEILING]),
    }
    if brightness.get(CONF_LUX_ENTITY):
        curve[CONF_LUX_ENTITY] = brightness[CONF_LUX_ENTITY]
    curve[CONF_GAME_BOOST] = int(brightness.get(CONF_GAME_BOOST, DEFAULT_GAME_BOOST))
    curve[CONF_MODES] = [_mode_option(row) for row in _mode_rows(brightness.get(CONF_MODES))]
    return {
        SECTION_MODULES: {name: modules.get(name) is True for name in HA_MODULES},
        SECTION_WAKE: {
            CONF_MACS: list(wake.get(CONF_MACS) or []),
            CONF_BROADCAST: str(wake[CONF_BROADCAST]).strip(),
            CONF_PORT: int(wake[CONF_PORT]),
            CONF_TIMEOUT: int(wake[CONF_TIMEOUT]),
        },
        SECTION_KEEP_AWAKE: {CONF_HOURS: int(_part(user_input, SECTION_KEEP_AWAKE)[CONF_HOURS])},
        SECTION_BRIGHTNESS: curve,
        SECTION_ADVANCED: {CONF_ALLOW_REMOTE: _part(user_input, SECTION_ADVANCED).get(CONF_ALLOW_REMOTE) is True},
    }
