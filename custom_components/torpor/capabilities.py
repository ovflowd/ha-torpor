"""Read-only views over what the PC reported in its last capabilities message."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_MAC = re.compile(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}")
_DISPLAY_ID = re.compile(r"[0-9a-f]{12}")


@dataclass(frozen=True, slots=True)
class Nic:
    name: str
    mac: str
    kind: str
    wol: str
    primary: bool


@dataclass(frozen=True, slots=True)
class Display:
    id: str
    label: str
    backend: str
    internal: bool
    modes: bool = False


def _text(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


class Capabilities:
    """The last capabilities a PC sent, as stored in the config entry. Anything malformed reads as absent."""

    def __init__(self, raw: Mapping[str, Any] | None) -> None:
        self.raw: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        host = self.raw.get("host")
        self.host: Mapping[str, Any] = host if isinstance(host, Mapping) else {}
        modules = self.raw.get("modules")
        self._modules: Mapping[str, Any] = modules if isinstance(modules, Mapping) else {}

    @property
    def hostname(self) -> str:
        return _text(self.host.get("hostname"), "PC")

    @property
    def agent_version(self) -> str | None:
        return _text(self.host.get("agent")) or None

    @property
    def vendor(self) -> str | None:
        return _text(self.host.get("vendor")) or None

    @property
    def model(self) -> str | None:
        return _text(self.host.get("model")) or None

    @property
    def linger(self) -> bool | None:
        value = self.host.get("linger")
        return value if isinstance(value, bool) else None

    def module(self, name: str) -> Mapping[str, Any]:
        value = self._modules.get(name)
        return value if isinstance(value, Mapping) else {}

    def enabled(self, name: str) -> bool:
        return self.module(name).get("enabled") is True

    def available(self, name: str) -> bool:
        return self.module(name).get("available") is True

    def offered(self, name: str) -> bool:
        return self.enabled(name) and self.available(name)

    def allows(self, name: str, action: str) -> bool:
        actions = self.module(name).get("actions")
        return self.enabled(name) and isinstance(actions, Mapping) and actions.get(action) is True

    def problems(self, name: str) -> list[str]:
        problems = self.module(name).get("problems")
        if not isinstance(problems, list):
            return []
        return [
            problem["message"]
            for problem in problems
            if isinstance(problem, Mapping) and isinstance(problem.get("message"), str)
        ]

    @property
    def nics(self) -> list[Nic]:
        nics = self.module("wake").get("nics")
        if not isinstance(nics, list):
            return []
        found: list[Nic] = []
        for nic in nics:
            if not isinstance(nic, Mapping) or not _MAC.fullmatch(_text(nic.get("mac"))):
                continue
            found.append(
                Nic(
                    name=_text(nic.get("name"), nic["mac"]),
                    mac=nic["mac"],
                    kind=_text(nic.get("kind"), "other"),
                    wol=_text(nic.get("wol"), "unknown"),
                    primary=nic.get("primary") is True,
                )
            )
        return found

    @property
    def displays(self) -> list[Display]:
        displays = self.module("brightness").get("displays")
        if not isinstance(displays, list):
            return []
        found: list[Display] = []
        for display in displays:
            if not isinstance(display, Mapping) or not _DISPLAY_ID.fullmatch(_text(display.get("id"))):
                continue
            found.append(
                Display(
                    id=display["id"],
                    label=_text(display.get("label"), display["id"]),
                    backend=_text(display.get("backend")),
                    internal=display.get("internal") is True,
                    modes=display.get("modes") is True,
                )
            )
        return found

    @property
    def games(self) -> list[str]:
        """The sources that can see a game on the PC; empty without game detection."""
        games = self.module("core").get("games")
        return [source for source in games if isinstance(source, str)] if isinstance(games, list) else []

    @property
    def modes_available(self) -> bool:
        modes = self.module("brightness").get("modes")
        return isinstance(modes, Mapping) and modes.get("available") is True

    @property
    def modes_reported(self) -> bool:
        """Whether the agent knows monitor modes at all: an older one ignores them and uses the curve's anchor."""
        return isinstance(self.module("brightness").get("modes"), Mapping)

    @property
    def modes_problems(self) -> list[str]:
        """Why the PC can't read monitor modes."""
        modes = self.module("brightness").get("modes")
        if not isinstance(modes, Mapping):
            return []
        problems = modes.get("problems")
        if not isinstance(problems, list):
            return []
        return [
            problem["message"]
            for problem in problems
            if isinstance(problem, Mapping) and isinstance(problem.get("message"), str)
        ]
