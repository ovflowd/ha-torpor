"""Constants for the Torpor integration."""

from __future__ import annotations

import logging
from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "torpor"
LOGGER = logging.getLogger(__package__)

PLATFORMS: Final = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.EVENT,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
]

DATA_AGENT_ID: Final = "agent_id"
DATA_KEY: Final = "key"
DATA_CAPABILITIES: Final = "capabilities"
DATA_CAPABILITIES_AT: Final = "capabilities_at"

HA_MODULES: Final = ("power", "keep_awake", "wake", "brightness")
POWER_ACTIONS: Final = ("lock", "suspend", "hibernate", "poweroff", "reboot")

SECTION_MODULES: Final = "modules"
SECTION_WAKE: Final = "wake"
SECTION_KEEP_AWAKE: Final = "keep_awake"
SECTION_BRIGHTNESS: Final = "brightness"
SECTION_ADVANCED: Final = "advanced"

CONF_MACS: Final = "macs"
CONF_BROADCAST: Final = "broadcast"
CONF_PORT: Final = "port"
CONF_TIMEOUT: Final = "timeout"
CONF_HOURS: Final = "hours"
CONF_LUX_ENTITY: Final = "lux_entity"
CONF_ANCHOR_LUX: Final = "anchor_lux"
CONF_ANCHOR_PCT: Final = "anchor_pct"
CONF_SLOPE: Final = "slope"
CONF_FLOOR: Final = "floor"
CONF_CEILING: Final = "ceiling"
CONF_GAME_BOOST: Final = "game_boost"
CONF_MODES: Final = "modes"
CONF_ALLOW_REMOTE: Final = "allow_remote"

MODE_DISPLAY: Final = "display"
MODE_CODE: Final = "code"
MODE_VALUE: Final = "value"
MODE_ANCHOR: Final = "anchor_pct"
MODE_LABEL: Final = "label"

DEFAULT_BROADCAST: Final = "255.255.255.255"
DEFAULT_PORT: Final = 9
DEFAULT_WAKE_TIMEOUT: Final = 120
DEFAULT_KEEP_AWAKE_HOURS: Final = 12
DEFAULT_CURVE: Final = {
    CONF_ANCHOR_LUX: 100,
    CONF_ANCHOR_PCT: 40,
    CONF_SLOPE: 25,
    CONF_FLOOR: 10,
    CONF_CEILING: 80,
}
DEFAULT_GAME_BOOST: Final = 30

STATE_GRACE_S: Final = 120
RESTARTING_LIMIT_S: Final = 900
BOOT_WINDOW_S: Final = 600
WAKE_SENDS: Final = 3
WAKE_GAP_S: Final = 0.1

EVENT_SUSPENDING: Final = "suspending"
EVENT_SHUTTING_DOWN: Final = "shutting_down"
EVENT_RESTARTING: Final = "restarting"
EVENT_RESUMED: Final = "resumed"
EVENT_BOOTED: Final = "booted"
EVENT_WAKE_SUCCEEDED: Final = "wake_succeeded"
EVENT_WAKE_FAILED: Final = "wake_failed"
EVENT_TYPES: Final = (
    EVENT_SUSPENDING,
    EVENT_SHUTTING_DOWN,
    EVENT_RESTARTING,
    EVENT_RESUMED,
    EVENT_BOOTED,
    EVENT_WAKE_SUCCEEDED,
    EVENT_WAKE_FAILED,
)

STATE_ASLEEP: Final = "asleep"
STATE_OFF: Final = "off"
STATE_RESTARTING: Final = "restarting"

WAKE_ALREADY_AWAKE: Final = "already_awake"
WAKE_SENT: Final = "sent"
WAKE_AWAKE: Final = "awake"
WAKE_TIMEOUT: Final = "timeout"

STORE_VERSION: Final = 1
