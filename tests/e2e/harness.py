"""The end-to-end test's two halves: a fake PC that runs the real torpord, and Home Assistant runs.

The PC side needs a checkout of ovflowd/torpor (next to this repo, or TORPOR_CHECKOUT), an
interpreter with PyGObject, Soup 3 and cryptography (TORPOR_PYTHON, the checkout's .venv-gi, or
/usr/bin/python3), and dbus-daemon. Everything it runs gets its own home, private buses, synthetic
/sys, /proc and /dev trees and fake ddcutil and nmcli, so nothing reaches the real PC, its buses or
its monitors. Home Assistant serves its real app on 127.0.0.1 with IP bans on.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import time
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

from aiohttp.test_utils import TestServer
from homeassistant import bootstrap, loader
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import category_registry as cr
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import frame, translation
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import label_registry as lr
from homeassistant.helpers import restore_state as rs
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry, async_test_home_assistant

from custom_components.torpor.const import DOMAIN

HERE = Path(__file__).resolve().parent
PROBE = (
    "import gi; gi.require_version('Gio', '2.0'); gi.require_version('Soup', '3.0');"
    "from gi.repository import Gio, Soup; import cryptography"
)
MACHINE_ID = "5eed0000e2e0400080000000000000e2"
MAC = "02:00:5e:10:00:01"
MONITOR = {"bus": 4, "mfg": "XMP", "model": "Example 27Q", "serial": "E2E0001", "connector": "card0-DP-2"}
DISPLAY_ID = hashlib.sha256(b"torpor/display\0ddcutil\0XMP:Example 27Q:E2E0001").hexdigest()[:12]
BUS_CONF = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:path={socket}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""
# What Home Assistant's own web server gives open requests when it stops.
HTTP_SHUTDOWN_TIMEOUT_S = 10
TCP_HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"


async def eventually[T](check: Callable[[], T | None], *, what: str, timeout: float = 20) -> T:
    """Poll `check` until it returns something other than None or False."""
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value is not None and value is not False:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout:g} s waiting for {what}")
        await asyncio.sleep(0.05)


@dataclass(frozen=True)
class Tools:
    checkout: Path
    python: str
    dbus_daemon: str


def find_tools() -> Tools | str:
    """What the PC side needs, or why the test can't run here."""
    checkout = Path(os.environ.get("TORPOR_CHECKOUT") or HERE.parents[2] / "torpor")
    if not (checkout / "torpor" / "agent.py").is_file() or not (checkout / "tests" / "fakes").is_dir():
        return f"no ovflowd/torpor checkout at {checkout} (set TORPOR_CHECKOUT)"
    daemon = shutil.which("dbus-daemon")
    if daemon is None:
        return "no dbus-daemon"
    candidates = [os.environ.get("TORPOR_PYTHON"), str(checkout / ".venv-gi" / "bin" / "python"), "/usr/bin/python3"]
    for python in candidates:
        if not python or not Path(python).exists():
            continue
        probe = subprocess.run([python, "-I", "-c", PROBE], capture_output=True, timeout=60, check=False)
        if probe.returncode == 0:
            return Tools(checkout, python, daemon)
    return "no interpreter with PyGObject, Soup 3 and cryptography (set TORPOR_PYTHON)"


def build_zipapp(tools: Tools, target: Path) -> None:
    """The zipapp `torporctl install` would put in place, built from the checkout."""
    script = (
        "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]);"
        "from torpor.install import build_zipapp; build_zipapp(Path(sys.argv[2]))"
    )
    subprocess.run([tools.python, "-I", "-B", "-c", script, str(tools.checkout), str(target)], check=True, timeout=60)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Agent:
    """One torpord process and its home."""

    def __init__(self, process: asyncio.subprocess.Process, home: Path, log: Path) -> None:
        self.process = process
        self.home = home
        self.log = log

    @property
    def status_file(self) -> Path:
        return self.home / "run" / "torpor" / "status.json"

    @property
    def key_file(self) -> Path:
        return self.home / ".config" / "torpor" / "key"

    @property
    def config_file(self) -> Path:
        return self.home / ".config" / "torpor" / "torpor.conf"

    def status(self) -> dict[str, Any]:
        try:
            data: dict[str, Any] = json.loads(self.status_file.read_text())
        except OSError, ValueError:
            return {}
        return data

    def connection(self) -> str | None:
        return (self.status().get("connection") or {}).get("state")

    def output(self) -> str:
        return self.log.read_text(errors="replace") if self.log.exists() else ""

    async def stop(self) -> int:
        if self.process.returncode is None:
            self.process.send_signal(signal.SIGTERM)
        code = await asyncio.wait_for(self.process.wait(), 15)
        return code


@dataclass
class Pairing:
    """A running `torporctl pair`: its output lines so far, and its exit code once all of them are read."""

    process: asyncio.subprocess.Process
    lines: list[str]
    reader: asyncio.Task[None]

    async def finished(self, timeout: float = 30) -> int:
        async with asyncio.timeout(timeout):
            code = await self.process.wait()
            await self.reader
        return code


class FakePC:
    """Private buses, a fake desktop on them, synthetic /sys and /proc, and torpord processes."""

    def __init__(self, root: Path, tools: Tools, pyz: Path) -> None:
        self.root = root
        self.tools = tools
        self.pyz = pyz
        self.tree = root / "tree"
        self.events: list[dict[str, Any]] = []
        self.ddcutil_state = root / "ddcutil.json"
        self.tools_state = root / "tools.json"
        self._processes: list[asyncio.subprocess.Process] = []
        self._agents: list[Agent] = []
        self._addresses: dict[str, str] = {}
        self._desktop: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._readers: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        self._write_tree()
        self._write_fakes()
        for name in ("system", "session"):
            self._addresses[name] = await self._start_bus(name)
        desktop = await asyncio.create_subprocess_exec(
            self.tools.python,
            "-I",
            "-B",
            str(HERE / "fake_desktop.py"),
            self._addresses["system"],
            self._addresses["session"],
            str(os.getuid()),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            env=self._base_env(),
        )
        self._desktop = desktop
        self._processes.append(desktop)
        self._reader = asyncio.get_running_loop().create_task(self._read_desktop(desktop))
        await self.wait_for_desktop(lambda event: event.get("ready") is True, what="the fake desktop")

    async def _start_bus(self, name: str) -> str:
        folder = self.root / "bus" / name
        folder.mkdir(parents=True)
        conf = folder / "bus.conf"
        conf.write_text(BUS_CONF.format(socket=folder / "socket"))
        daemon = await asyncio.create_subprocess_exec(
            self.tools.dbus_daemon,
            f"--config-file={conf}",
            "--print-address=1",
            "--nofork",
            stdout=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
        self._processes.append(daemon)
        assert daemon.stdout is not None
        line = await asyncio.wait_for(daemon.stdout.readline(), 10)
        return line.decode().strip()

    async def _read_desktop(self, desktop: asyncio.subprocess.Process) -> None:
        assert desktop.stdout is not None
        while True:
            line = await desktop.stdout.readline()
            if not line:
                return
            self.events.append(json.loads(line))

    def desktop_events(self, predicate: Callable[[dict[str, Any]], bool]) -> list[dict[str, Any]]:
        return [event for event in self.events if predicate(event)]

    async def wait_for_desktop(
        self, predicate: Callable[[dict[str, Any]], bool], *, what: str, timeout: float = 20
    ) -> dict[str, Any]:
        def found() -> dict[str, Any] | None:
            matches = self.desktop_events(predicate)
            return matches[-1] if matches else None

        event = await eventually(found, what=what, timeout=timeout)
        return event

    async def tell_desktop(self, **order: Any) -> None:
        assert self._desktop is not None and self._desktop.stdin is not None
        self._desktop.stdin.write(json.dumps(order).encode() + b"\n")
        await self._desktop.stdin.drain()

    def ddcutil(self) -> dict[str, Any]:
        state: dict[str, Any] = json.loads(self.ddcutil_state.read_text())
        return state

    def osd(self, code: int, value: int) -> None:
        """A setting changed in the monitor's own menu, under the fake ddcutil's own lock."""
        with open(f"{self.ddcutil_state}.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = self.ddcutil()
            state["monitors"][0]["vcp"][f"{code:02x}"] = value
            temporary = self.ddcutil_state.with_suffix(".osd")
            temporary.write_text(json.dumps(state))
            os.replace(temporary, self.ddcutil_state)

    def _write_tree(self) -> None:
        sysfs, proc, etc, dev = self.tree / "sys", self.tree / "proc", self.tree / "etc", self.tree / "dev"
        connector = sysfs / "class" / "drm" / MONITOR["connector"]
        nic = sysfs / "class" / "net" / "enp3s0"
        dmi = sysfs / "class" / "dmi" / "id"
        for folder in (connector, nic / "device" / "power", dmi, proc / "net", proc / "sys/kernel/random", etc, dev):
            folder.mkdir(parents=True, exist_ok=True)
        (dev / f"i2c-{MONITOR['bus']}").write_text("")
        (dev / f"i2c-{MONITOR['bus']}").chmod(0o600)
        (connector / "status").write_text("connected\n")
        (connector / "dpms").write_text("On\n")
        (nic / "address").write_text(MAC + "\n")
        (nic / "type").write_text("1\n")
        (nic / "device" / "power" / "wakeup").write_text("enabled\n")
        (dmi / "sys_vendor").write_text("Example Computers\n")
        (dmi / "product_name").write_text("Tower 5\n")
        (proc / "stat").write_text("cpu  1 2 3 4\nbtime 1799990000\nprocesses 5\n")
        (proc / "uptime").write_text("120.50 200.00\n")
        (proc / "sys" / "kernel" / "random" / "boot_id").write_text("e2e-boot\n")
        (proc / "net" / "tcp").write_text(TCP_HEADER)
        (proc / "net" / "tcp6").write_text(TCP_HEADER)
        (proc / "net" / "route").write_text(
            "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
            "enp3s0\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
        )
        (etc / "machine-id").write_text(MACHINE_ID + "\n")
        (etc / "os-release").write_text('ID=arch\nPRETTY_NAME="Example Linux"\n')

    def _write_fakes(self) -> None:
        fakes = self.tools.checkout / "tests" / "fakes"
        monitor = {**MONITOR, "brightness": 40, "vcp": {"e1": 0}}
        self.ddcutil_state.write_text(json.dumps({"version": "3.0.2", "monitors": [monitor]}))
        self.tools_state.write_text(json.dumps({"nm": {"devices": {"enp3s0": "Wired"}, "wol": {"Wired": "magic"}}}))
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        wrappers = {"ddcutil": f"{fakes / 'fake_ddcutil.py'}", "nmcli": f"{fakes / 'fake_tools.py'} nmcli"}
        for name, script in wrappers.items():
            wrapper = bin_dir / name
            wrapper.write_text(f'#!/bin/sh\nexec {self.tools.python} -I -B {script} "$@"\n')
            wrapper.chmod(0o755)

    def _base_env(self) -> dict[str, str]:
        return {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "GSETTINGS_BACKEND": "memory", "NO_AT_BRIDGE": "1"}

    def env(self, home: Path) -> dict[str, str]:
        runtime = home / "run"
        runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        return {
            **self._base_env(),
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_STATE_HOME": str(home / ".local" / "state"),
            "XDG_RUNTIME_DIR": str(runtime),
            "DBUS_SYSTEM_BUS_ADDRESS": self._addresses["system"],
            "DBUS_SESSION_BUS_ADDRESS": self._addresses["session"],
            "TORPOR_SYSFS": str(self.tree / "sys"),
            "TORPOR_PROC": str(self.tree / "proc"),
            "TORPOR_DEV": str(self.tree / "dev"),
            "TORPOR_MACHINE_ID": str(self.tree / "etc" / "machine-id"),
            "TORPOR_OS_RELEASE": str(self.tree / "etc" / "os-release"),
            "TORPOR_DDCUTIL": str(self.root / "bin" / "ddcutil"),
            "FAKE_DDCUTIL_STATE": str(self.ddcutil_state),
            "TORPOR_NMCLI": str(self.root / "bin" / "nmcli"),
            "FAKE_TOOLS_STATE": str(self.tools_state),
            "TORPOR_SYSTEMCTL": "/bin/false",
            "TORPOR_SYSTEMD_RUN": "/bin/false",
            "TORPOR_LOGINCTL": "/bin/false",
            "TORPOR_JOURNALCTL": "/bin/false",
        }

    async def start_agent(self, home: Path) -> Agent:
        home.mkdir(parents=True, exist_ok=True)
        log = home / "torpord.log"
        with log.open("ab") as output:
            process = await asyncio.create_subprocess_exec(
                self.tools.python,
                "-I",
                "-B",
                str(HERE / "run_torpord.py"),
                str(self.pyz),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=output,
                stderr=asyncio.subprocess.STDOUT,
                env=self.env(home),
            )
        self._processes.append(process)
        agent = Agent(process, home, log)
        self._agents.append(agent)
        return agent

    async def pair(self, home: Path, url: str, code: str) -> Pairing:
        """Start `torporctl pair` with the code on stdin."""
        process = await asyncio.create_subprocess_exec(
            self.tools.python,
            "-I",
            "-B",
            "-u",
            str(self.pyz),
            "ctl",
            "pair",
            "--url",
            url,
            "--code-stdin",
            "--timeout",
            "120",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=self.env(home),
        )
        self._processes.append(process)
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(code.encode() + b"\n")
        await process.stdin.drain()
        process.stdin.close()
        lines: list[str] = []
        stdout = process.stdout

        async def collect() -> None:
            while True:
                line = await stdout.readline()
                if not line:
                    return
                lines.append(line.decode().rstrip("\n"))

        reader = asyncio.get_running_loop().create_task(collect())
        self._readers.append(reader)
        return Pairing(process, lines, reader)

    async def torporctl(self, home: Path, *args: str) -> tuple[int, str]:
        """Run torporctl from the zipapp. It checks the agent's PID in the real /proc, not the synthetic one."""
        env = {key: value for key, value in self.env(home).items() if key != "TORPOR_PROC"}
        process = await asyncio.create_subprocess_exec(
            self.tools.python,
            "-I",
            "-B",
            str(self.pyz),
            "ctl",
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        output, _ = await asyncio.wait_for(process.communicate(), 30)
        assert process.returncode is not None
        return process.returncode, output.decode()

    def wrong_key_home(self, paired: Path, home: Path) -> None:
        """A home with the paired PC's settings and another key: an old key, as after a re-pair elsewhere."""
        folder = home / ".config" / "torpor"
        folder.mkdir(parents=True, mode=0o700)
        shutil.copy(paired / ".config" / "torpor" / "torpor.conf", folder / "torpor.conf")
        key = folder / "key"
        key.write_text(base64.b64encode(os.urandom(32)).decode() + "\n")
        key.chmod(0o600)

    async def close(self) -> None:
        for agent in self._agents:
            if agent.process.returncode is None:
                agent.process.kill()
        if self._desktop is not None and self._desktop.stdin is not None:
            self._desktop.stdin.close()
        for process in reversed(self._processes):
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), 10)
        for task in [*self._readers, *([self._reader] if self._reader else [])]:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


@dataclass(frozen=True)
class EntrySnapshot:
    """What .storage/core.config_entries keeps of an entry across a restart."""

    entry_id: str
    title: str
    unique_id: str | None
    data: dict[str, Any]
    options: dict[str, Any]

    @classmethod
    def of(cls, entry: ConfigEntry) -> EntrySnapshot:
        return cls(entry.entry_id, entry.title, entry.unique_id, dict(entry.data), dict(entry.options))


async def load_registries(hass: HomeAssistant) -> None:
    """Load the registries and restore states from the test's storage, which the previous run wrote at its stop.

    The test harness normally starts them empty; a restart needs them back, because restore states are keyed by
    entity registry IDs.
    """
    dr.async_setup(hass)
    with (
        patch("homeassistant.helpers.restore_state.RestoreStateData.async_setup_dump", return_value=None),
        patch("homeassistant.helpers.restore_state.start.async_at_start"),
    ):
        for registry in (ar, cr, dr, er, fr, ir, lr, rs):
            await registry.async_load(hass)
    hass.data[bootstrap.DATA_REGISTRIES_LOADED] = None


class HomeAssistantRun:
    """One run of Home Assistant: http with IP bans after one wrong login, the integration, and its real app
    served on a fixed port, so the PC can find it again after a restart."""

    def __init__(self, config_dir: Path, port: int) -> None:
        self.config_dir = config_dir
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.hass: HomeAssistant | None = None
        self._server: TestServer | None = None
        self._stack: AsyncExitStack | None = None

    async def start(self, entry: EntrySnapshot | None = None) -> HomeAssistant:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        stack = AsyncExitStack()
        self._stack = stack
        run = async_test_home_assistant(config_dir=str(self.config_dir), load_registries=False)
        hass = await stack.enter_async_context(run)
        self.hass = hass
        await load_registries(hass)
        hass.data.pop(loader.DATA_CUSTOM_COMPONENTS, None)
        frame.async_setup(hass)
        await translation.async_load_integrations(hass, {"homeassistant"})
        hass.config.internal_url = self.url
        http = {"http": {"ip_ban_enabled": True, "login_attempts_threshold": 1}}
        loaded = await async_setup_component(hass, "http", http)
        assert loaded
        server = TestServer(hass.http.app, host="127.0.0.1", port=self.port)
        self._server = server

        async def stop_serving(_event: Event) -> None:
            await server.close()

        async def write_restore_states(_event: Event) -> None:
            await rs.async_get(hass).async_dump_states()

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop_serving)
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_FINAL_WRITE, write_restore_states)
        if entry is not None:
            MockConfigEntry(
                domain=DOMAIN,
                entry_id=entry.entry_id,
                title=entry.title,
                unique_id=entry.unique_id,
                data=entry.data,
                options=entry.options,
            ).add_to_hass(hass)
        loaded = await async_setup_component(hass, DOMAIN, {})
        assert loaded
        await hass.async_block_till_done()
        await server.start_server(shutdown_timeout=HTTP_SHUTDOWN_TIMEOUT_S)
        return hass

    async def stop(self) -> None:
        """Stop as Home Assistant does: the web server goes with the stop event, restore states at the final write."""
        hass, server, stack = self.hass, self._server, self._stack
        self.hass = self._server = self._stack = None
        if hass is not None:
            await hass.async_stop(force=True)
        if server is not None:
            await server.close()
        if stack is not None:
            await stack.aclose()
