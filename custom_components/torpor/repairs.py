"""Repair issues. None is fixable from Home Assistant: each says what to run on the PC."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .capabilities import Capabilities
from .const import DOMAIN, HA_MODULES
from .options import Settings

STALE_KEY = "stale_key"
PROTOCOL_MISMATCH = "protocol_mismatch"
WOL_OFF = "wol_off"
LINGER_OFF = "linger_off"
MODES_UNAVAILABLE = "modes_unavailable"
MODES_AGENT_OUTDATED = "modes_agent_outdated"


def module_issue(module: str) -> str:
    return f"module_unavailable_{module}"


def issue_id(entry: ConfigEntry, key: str) -> str:
    return f"{key}_{entry.entry_id}"


@callback
def _create(
    hass: HomeAssistant,
    entry: ConfigEntry,
    key: str,
    severity: ir.IssueSeverity,
    placeholders: dict[str, str] | None = None,
) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id(entry, key),
        config_entry_id=entry.entry_id,
        is_fixable=False,
        severity=severity,
        translation_key=key,
        translation_placeholders={"name": entry.title, **(placeholders or {})},
    )


@callback
def raise_stale_key(hass: HomeAssistant, entry: ConfigEntry) -> None:
    _create(hass, entry, STALE_KEY, ir.IssueSeverity.ERROR)


@callback
def raise_protocol_mismatch(hass: HomeAssistant, entry: ConfigEntry, proto: str) -> None:
    _create(hass, entry, PROTOCOL_MISMATCH, ir.IssueSeverity.ERROR, {"proto": proto})


@callback
def clear_auth_issues(hass: HomeAssistant, entry: ConfigEntry) -> None:
    for key in (STALE_KEY, PROTOCOL_MISMATCH):
        ir.async_delete_issue(hass, DOMAIN, issue_id(entry, key))


@callback
def check_capabilities(hass: HomeAssistant, entry: ConfigEntry, capabilities: Capabilities, settings: Settings) -> None:
    """Raise or clear the issues that follow from what the PC last reported and the options."""
    keys = [
        *(module_issue(module) for module in HA_MODULES),
        WOL_OFF,
        LINGER_OFF,
        MODES_UNAVAILABLE,
        MODES_AGENT_OUTDATED,
    ]
    with ir.async_refresh_issues(
        hass, DOMAIN, issue_ids=[issue_id(entry, key) for key in keys], config_entry_id=entry.entry_id
    ):
        for module in HA_MODULES:
            if settings.module_on(module) and not capabilities.offered(module):
                problems = "; ".join(capabilities.problems(module)) or "-"
                _create(hass, entry, module_issue(module), ir.IssueSeverity.WARNING, {"problems": problems})
        nics = {nic.mac: nic for nic in capabilities.nics}
        selected = [nics[mac] for mac in settings.macs if mac in nics]
        if settings.module_on("wake") and selected and all(nic.wol == "off" for nic in selected):
            _create(hass, entry, WOL_OFF, ir.IssueSeverity.WARNING, {"nics": ", ".join(nic.name for nic in selected)})
        if capabilities.linger is False:
            _create(hass, entry, LINGER_OFF, ir.IssueSeverity.WARNING)
        if not (settings.module_on("brightness") and settings.modes and capabilities.offered("brightness")):
            return
        if not capabilities.modes_reported:
            _create(hass, entry, MODES_AGENT_OUTDATED, ir.IssueSeverity.WARNING)
        elif not capabilities.modes_available and capabilities.modes_problems:
            problems = "; ".join(capabilities.modes_problems)
            _create(hass, entry, MODES_UNAVAILABLE, ir.IssueSeverity.WARNING, {"problems": problems})
