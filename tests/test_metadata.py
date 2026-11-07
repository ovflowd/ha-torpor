"""manifest.json, hacs.json, services.yaml, icons.json and translations/en.json agree with the code."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

import yaml

from custom_components.torpor import protocol
from custom_components.torpor.const import EVENT_TYPES, HA_MODULES
from custom_components.torpor.options import MODULE_NAMES
from custom_components.torpor.pairing import Outcome

ROOT = Path(__file__).parent.parent
COMPONENT = ROOT / "custom_components" / "torpor"
MANIFEST = json.loads((COMPONENT / "manifest.json").read_text())
STRINGS = json.loads((COMPONENT / "translations" / "en.json").read_text())
ICONS = json.loads((COMPONENT / "icons.json").read_text())
SERVICES = yaml.safe_load((COMPONENT / "services.yaml").read_text())
SOURCE = "\n".join(path.read_text() for path in sorted(COMPONENT.glob("*.py")))

ENTITIES = {
    "sensor": ["state", "last_boot", "sleep_blockers", "mode"],
    "binary_sensor": ["agent", "game"],
    "event": ["power"],
    "button": ["lock", "sleep", "hibernate", "power_off", "restart", "wake"],
    "switch": ["keep_awake", "auto_brightness"],
    "number": ["brightness"],
}


def placeholders(text: str) -> set[str]:
    return set(re.findall(r"\{(\w+)\}", text))


def test_manifest_keys_are_domain_name_then_alphabetical() -> None:
    keys = list(MANIFEST)
    assert keys[:2] == ["domain", "name"]
    assert keys[2:] == sorted(keys[2:])
    assert MANIFEST["domain"] == COMPONENT.name
    assert MANIFEST["iot_class"] == "local_push"
    assert MANIFEST["config_flow"] is True
    assert MANIFEST["requirements"] == []


def test_the_versions_agree() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    hacs = json.loads((ROOT / "hacs.json").read_text())
    assert MANIFEST["version"] == pyproject["project"]["version"]
    assert hacs == {"name": "Torpor", "homeassistant": "2026.11.0"}


def test_no_translation_uses_a_reference() -> None:
    assert "[%key:" not in json.dumps(STRINGS)


def test_every_entity_has_a_name_and_an_icon() -> None:
    for platform, keys in ENTITIES.items():
        for key in keys:
            assert STRINGS["entity"][platform][key]["name"], (platform, key)
            assert ICONS["entity"][platform][key]["default"].startswith("mdi:"), (platform, key)
    assert set(STRINGS["entity"]) == set(ENTITIES)
    assert {platform: sorted(keys) for platform, keys in STRINGS["entity"].items()} == {
        platform: sorted(keys) for platform, keys in ENTITIES.items()
    }


def test_every_state_and_event_type_is_translated() -> None:
    assert set(STRINGS["entity"]["sensor"]["state"]["state"]) == set(protocol.HA_STATES)
    assert set(STRINGS["entity"]["event"]["power"]["state_attributes"]["event_type"]["state"]) == set(EVENT_TYPES)
    assert set(ICONS["entity"]["sensor"]["state"]["state"]) == set(protocol.HA_STATES)
    sessions = STRINGS["entity"]["sensor"]["state"]["state_attributes"]["session"]["state"]
    assert set(sessions) == set(protocol.SESSIONS)


def test_every_error_code_has_an_exception_message() -> None:
    for code in (*protocol.ERRORS, "timeout", "not_connected"):
        assert STRINGS["exceptions"][f"error_{code}"]["message"], code


def test_every_translation_key_in_the_code_exists() -> None:
    used = set(re.findall(r'translation_key="(\w+)"', SOURCE))
    assert used
    exceptions = set(STRINGS["exceptions"])
    issues = set(STRINGS["issues"])
    assert used <= exceptions | issues
    for key in ("stale_key", "protocol_mismatch", "wol_off", "linger_off", "modes_unavailable", "modes_agent_outdated"):
        assert key in issues
    for module in HA_MODULES:
        assert f"module_unavailable_{module}" in issues


def test_every_abort_reason_and_error_exists() -> None:
    reasons = set(re.findall(r'reason="(\w+)"', SOURCE)) | {
        outcome.value for outcome in Outcome if outcome.value != "ready"
    }
    reasons |= {"already_configured", "already_in_progress", "reconfigure_successful"}
    assert reasons <= set(STRINGS["config"]["abort"])
    errors = set(re.findall(r'errors\["base"\] = "(\w+)"', SOURCE))
    assert errors == set(STRINGS["config"]["error"]) == set(STRINGS["options"]["error"])


def test_every_flow_step_is_translated() -> None:
    steps = set(re.findall(r'(?<!next_)step_id="(\w+)"', SOURCE))
    assert steps - {"init"} <= set(STRINGS["config"]["step"])
    assert "init" in STRINGS["options"]["step"]
    menu = STRINGS["config"]["step"]["confirm"]["menu_options"]
    assert set(menu) == {"features", "finish", "cancel"}
    assert placeholders(STRINGS["config"]["progress"]["pair"]) == {"code", "command", "docs"}


def test_every_form_field_has_a_description() -> None:
    for sections in (
        STRINGS["config"]["step"]["features"]["sections"],
        STRINGS["options"]["step"]["init"]["sections"],
    ):
        assert set(sections) == {"modules", "wake", "keep_awake", "brightness", "advanced"}
        for name, section in sections.items():
            assert section["name"], name
            assert set(section["data"]) == set(section["data_description"]), name


def test_the_module_names_in_the_code_match_the_form_labels() -> None:
    for strings in (STRINGS["config"]["step"]["features"], STRINGS["options"]["step"]["init"]):
        assert strings["sections"]["modules"]["data"] == MODULE_NAMES


def test_every_action_is_described_translated_and_has_an_icon() -> None:
    assert set(SERVICES) == {"wake", "sleep", "keep_awake"}
    for name, service in SERVICES.items():
        strings: dict[str, Any] = STRINGS["services"][name]
        assert strings["name"]
        assert strings["description"]
        assert set(strings["fields"]) == set(service["fields"])
        assert ICONS["services"][name]["service"].startswith("mdi:")


def test_the_issue_placeholders_match_what_the_code_passes() -> None:
    for key, issue in STRINGS["issues"].items():
        used = placeholders(issue["title"]) | placeholders(issue["description"])
        assert "name" in used, key
        assert used <= {"name", "proto", "problems", "nics"}, key


def test_the_brand_images_are_square_pngs() -> None:
    for name, size in (("icon.png", 256), ("icon@2x.png", 512)):
        data = (COMPONENT / "brand" / name).read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n", name
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        assert (width, height) == (size, size), name
