"""A PC agent for tests, built on the shared protocol's AgentHandshake. It dials the real view."""

from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import ClientWebSocketResponse, WSMsgType
from aiohttp.test_utils import TestClient

from custom_components.torpor import protocol

AGENT_ID = "1f399f30-a3d9-488e-a63c-cbeb7333a961"
OTHER_AGENT_ID = "0e8a6a3c-2d4f-4b6e-9a1c-5d7e3f9b2a10"
KEY = bytes(range(32))
OTHER_KEY = bytes(range(32, 64))
DISPLAY_ID = "3f2a9c1b04de"
OTHER_DISPLAY_ID = "a1b2c3d4e5f6"
MAC = "02:00:5e:10:00:01"
WIFI_MAC = "02:00:5e:10:00:02"


def capabilities(**changes: Any) -> dict[str, Any]:
    """Synthetic capabilities of a desktop PC named desk. `changes` replaces modules or host fields."""
    modules: dict[str, Any] = {
        "core": {"enabled": True, "available": True, "backend": "logind", "problems": [], "games": []},
        "power": {
            "enabled": True,
            "available": True,
            "backend": "logind",
            "problems": [],
            "actions": {"lock": True, "suspend": True, "hibernate": True, "poweroff": True, "reboot": True},
        },
        "keep_awake": {
            "enabled": True,
            "available": True,
            "backend": "kde",
            "problems": [],
            "actions": {"keep_awake": True},
        },
        "wake": {
            "enabled": True,
            "available": True,
            "backend": None,
            "problems": [],
            "nics": [
                {"name": "enp5s0", "mac": MAC, "kind": "ethernet", "wol": "armed", "primary": True},
                {"name": "wlp6s0", "mac": WIFI_MAC, "kind": "wifi", "wol": "unknown", "primary": False},
            ],
        },
        "brightness": {
            "enabled": True,
            "available": True,
            "backend": None,
            "problems": [],
            "actions": {"brightness": True},
            "displays": [
                {"id": DISPLAY_ID, "label": "Example 27Q", "backend": "kde", "internal": False, "modes": True}
            ],
            "modes": {"available": True, "problems": []},
        },
    }
    host: dict[str, Any] = {
        "hostname": "desk",
        "os": "Example Linux 1.0",
        "kernel": "6.17.0-example",
        "agent": "0.1.0",
        "python": "3.14.0",
        "vendor": "Example Computers",
        "model": "Tower 5",
        "desktop": "KDE",
        "linger": True,
    }
    for name, value in changes.items():
        if name in modules:
            modules[name] = value
        else:
            host[name] = value
    return {"observe_only": False, "host": host, "modules": modules}


def state(**changes: Any) -> dict[str, Any]:
    message: dict[str, Any] = {
        "type": "state",
        "state": "in_use",
        "session": "unlocked",
        "display_on": True,
        "remote": [],
        "busy": [],
        "boot_time": 1762500000,
        "power": {"can_suspend": "yes", "can_hibernate": "yes", "blockers": []},
        "keep_awake": {"on": False, "remaining": None, "backend": "kde", "applied": False},
        "brightness": {
            "lux": 84.0,
            "displays": {DISPLAY_ID: {"percent": 31, "auto": False, "overridden": False, "target": 31, "error": None}},
        },
    }
    message.update(changes)
    return message


class FakeAgent:
    def __init__(self, client: TestClient, *, agent_id: str = AGENT_ID, agent: str = "0.1.0") -> None:
        self._client = client
        self.agent_id = agent_id
        self.agent = agent
        self.ws: ClientWebSocketResponse | None = None
        self.session: protocol.Session | None = None
        self.handshake: protocol.AgentHandshake | None = None
        self._pings = 0
        self.close_reason = ""
        self._close_code: int | None = None

    async def open(self) -> ClientWebSocketResponse:
        self.ws = await self._client.ws_connect(protocol.WS_PATH)
        return self.ws

    async def hello(self, *, key: bytes | None = None, code: str | None = None) -> str:
        """Send the hello and return HA's challenge text."""
        if self.ws is None:
            await self.open()
        ws = self.ws
        assert ws is not None
        self.handshake = protocol.AgentHandshake(
            agent_id=self.agent_id,
            agent=self.agent,
            ephemeral=protocol.Ephemeral.generate(),
            key=key,
            code=protocol.parse_code(code) if code is not None else None,
            hostname="desk",
            os="Example Linux 1.0",
        )
        await ws.send_str(self.handshake.hello_text())
        message = await ws.receive(timeout=5)
        assert message.type is WSMsgType.TEXT, message
        return str(message.data)

    async def authenticate(self, *, key: bytes | None = None, code: str | None = None) -> dict[str, Any]:
        """Run a whole handshake and return auth_ok."""
        challenge = await self.hello(key=key, code=code)
        assert self.handshake is not None
        auth = self.handshake.answer(challenge)
        assert self.ws is not None
        await self.ws.send_str(auth)
        self.session = self.handshake.session
        message = await self.receive()
        assert message is not None
        assert message["type"] == "auth_ok", message
        return message

    async def connect(self, key: bytes = KEY, caps: dict[str, Any] | None = None, **state_changes: Any) -> None:
        """Authenticate, then send capabilities and a state, as torpord does."""
        await self.authenticate(key=key)
        await self.send({"type": "capabilities", **(caps or capabilities())})
        await self.send(state(**state_changes))

    async def send(self, message: dict[str, Any]) -> None:
        assert self.ws is not None
        assert self.session is not None
        frame = self.session.seal(message)
        await self.ws.send_bytes(frame)

    async def receive(self, timeout: float = 5) -> dict[str, Any] | None:
        assert self.ws is not None
        message = await self.ws.receive(timeout=timeout)
        if message.type is WSMsgType.BINARY:
            assert self.session is not None
            return self.session.open(message.data)
        assert message.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED), message
        if message.type is WSMsgType.CLOSE:
            self._close_code = int(message.data)
            self.close_reason = message.extra or ""
        return None

    async def expect(self, kind: str, timeout: float = 5) -> dict[str, Any]:
        """The next message of this type; pings are answered and anything else is skipped."""
        async with asyncio.timeout(timeout):
            while True:
                message = await self.receive(timeout)
                assert message is not None, f"closed with {self.close_code} while waiting for {kind}"
                if message["type"] == kind:
                    return message
                if message["type"] == "ping":
                    await self.send({"type": "pong", "t": message["t"]})

    async def sync(self) -> None:
        """Ping and wait for the pong: HA handles messages in order, so everything sent before is done."""
        self._pings += 1
        await self.send({"type": "ping", "t": self._pings})
        pong = await self.expect("pong")
        assert pong["t"] == self._pings

    async def closed(self, timeout: float = 5) -> int | None:
        """Read until HA closes the socket and return its close code."""
        assert self.ws is not None
        async with asyncio.timeout(timeout):
            while True:
                message = await self.ws.receive()
                if message.type is WSMsgType.CLOSE:
                    self._close_code = int(message.data)
                    self.close_reason = message.extra or ""
                if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                    return self.close_code

    async def until_closed(self, timeout: float = 5) -> list[dict[str, Any]]:
        """Every message HA sends until it closes the socket."""
        messages: list[dict[str, Any]] = []
        async with asyncio.timeout(timeout):
            while True:
                message = await self.receive(timeout)
                if message is None:
                    return messages
                messages.append(message)

    @property
    def close_code(self) -> int | None:
        """The code in HA's close frame, which is what the PC acts on."""
        if self._close_code is not None:
            return self._close_code
        return self.ws.close_code if self.ws is not None else None

    async def close(self, code: int = 1000) -> None:
        if self.ws is not None and not self.ws.closed:
            await self.ws.close(code=code)
