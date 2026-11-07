"""A fake desktop for the end-to-end test: logind on one private bus, PowerDevil's idle inhibit on another.

It runs under the interpreter that runs torpord, because it needs PyGObject. Arguments: the system
bus address, the session bus address, and the uid whose desk session it shows. It prints one JSON
object per line: {"ready": true} once it owns its names, then one line for each call the agent
makes and for each delay inhibitor the agent lets go. It reads commands the same way on stdin,
{"sleep": true|false}, {"locked": true|false}, {"gamemode": true} to start a gamemoded (as D-Bus
activation would) and {"game": true|false} for a game registering with it or leaving, and exits
when stdin closes. It never talks to the real system or session bus.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

LOGIN = "org.freedesktop.login1"
MANAGER_PATH = "/org/freedesktop/login1"
SESSION_PATH = "/org/freedesktop/login1/session/_32"
POWERDEVIL = "org.freedesktop.PowerManagement"
INHIBIT_PATH = "/org/freedesktop/PowerManagement/Inhibit"
GAMEMODE = "com.feralinteractive.GameMode"
GAMEMODE_PATH = "/com/feralinteractive/GameMode"
SLEEP_AFTER_MS = 50

LOGIND_XML = """
<node>
  <interface name="org.freedesktop.login1.Manager">
    <method name="ListSessions"><arg type="a(susso)" direction="out"/></method>
    <method name="ListInhibitors"><arg type="a(ssssuu)" direction="out"/></method>
    <method name="Inhibit">
      <arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="in"/>
      <arg type="s" direction="in"/><arg type="h" direction="out"/>
    </method>
    <method name="CanSuspend"><arg type="s" direction="out"/></method>
    <method name="CanHibernate"><arg type="s" direction="out"/></method>
    <method name="CanPowerOff"><arg type="s" direction="out"/></method>
    <method name="CanReboot"><arg type="s" direction="out"/></method>
    <method name="Suspend"><arg type="b" direction="in"/></method>
    <method name="GetUser"><arg type="u" direction="in"/><arg type="o" direction="out"/></method>
    <signal name="PrepareForSleep"><arg type="b"/></signal>
    <signal name="PrepareForShutdown"><arg type="b"/></signal>
    <signal name="PrepareForShutdownWithMetadata"><arg type="b"/><arg type="a{sv}"/></signal>
  </interface>
  <interface name="org.freedesktop.login1.Session">
    <method name="Lock"/>
    <property name="Class" type="s" access="read"/>
    <property name="Type" type="s" access="read"/>
    <property name="State" type="s" access="read"/>
    <property name="Active" type="b" access="read"/>
    <property name="Remote" type="b" access="read"/>
    <property name="Service" type="s" access="read"/>
    <property name="LockedHint" type="b" access="read"/>
    <property name="Desktop" type="s" access="read"/>
  </interface>
  <interface name="org.freedesktop.login1.User">
    <property name="Linger" type="b" access="read"/>
  </interface>
</node>
"""

POWERDEVIL_XML = """
<node>
  <interface name="org.freedesktop.PowerManagement.Inhibit">
    <method name="Inhibit">
      <arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="u" direction="out"/>
    </method>
    <method name="UnInhibit"><arg type="u" direction="in"/></method>
  </interface>
</node>
"""


# What gamemoded 1.8.2 exports, introspected in a container.
GAMEMODE_XML = """
<node>
  <interface name="com.feralinteractive.GameMode">
    <property name="ClientCount" type="i" access="read"/>
    <signal name="GameRegistered"><arg type="i"/><arg type="o"/></signal>
    <signal name="GameUnregistered"><arg type="i"/><arg type="o"/></signal>
  </interface>
</node>
"""


def report(**fields: Any) -> None:
    print(json.dumps(fields), flush=True)


def connect(address: str) -> Gio.DBusConnection:
    flags = Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
    return Gio.DBusConnection.new_for_address_sync(address, flags, None, None)


class Desktop:
    def __init__(self, system: Gio.DBusConnection, session: Gio.DBusConnection, uid: int) -> None:
        self.system = system
        self.session = session
        self.uid = uid
        self.user_path = f"/org/freedesktop/login1/user/_{uid}"
        self.locked = False
        self.delays: dict[int, tuple[str, str, str]] = {}
        self.next_delay = 1
        self.next_cookie = 1
        self.clients = 0

    def export(self) -> None:
        logind = Gio.DBusNodeInfo.new_for_xml(LOGIND_XML)
        self.system.register_object(MANAGER_PATH, logind.interfaces[0], self.manager_call, None, None)
        self.system.register_object(SESSION_PATH, logind.interfaces[1], self.session_call, self.session_get, None)
        self.system.register_object(self.user_path, logind.interfaces[2], None, self.user_get, None)
        powerdevil = Gio.DBusNodeInfo.new_for_xml(POWERDEVIL_XML)
        self.session.register_object(INHIBIT_PATH, powerdevil.interfaces[0], self.powerdevil_call, None, None)

    def manager_call(
        self, conn: Any, sender: str, _path: str, _iface: str, method: str, params: Any, invocation: Any
    ) -> None:
        args = params.unpack()
        if method == "ListSessions":
            row = ("2", self.uid, "alex", "seat0", SESSION_PATH)
            invocation.return_value(GLib.Variant("(a(susso))", ([row],)))
        elif method == "ListInhibitors":
            rows = [(what, who, why, "delay", self.uid, 0) for what, who, why in self.delays.values()]
            invocation.return_value(GLib.Variant("(a(ssssuu))", (rows,)))
        elif method == "Inhibit":
            self.inhibit(sender, args, invocation)
        elif method in ("CanSuspend", "CanPowerOff", "CanReboot"):
            invocation.return_value(GLib.Variant("(s)", ("yes",)))
        elif method == "CanHibernate":
            invocation.return_value(GLib.Variant("(s)", ("no",)))
        elif method == "Suspend":
            report(call="Suspend", sender=sender, args=list(args))
            invocation.return_value(None)
            GLib.timeout_add(SLEEP_AFTER_MS, self.prepare_for_sleep, True)
        elif method == "GetUser":
            invocation.return_value(GLib.Variant("(o)", (self.user_path,)))

    def inhibit(self, sender: str, args: tuple[str, str, str, str], invocation: Any) -> None:
        what, who, why, mode = args
        number = self.next_delay
        self.next_delay += 1
        self.delays[number] = (what, who, why)
        read, write = os.pipe()
        fds = Gio.UnixFDList.new_from_array([write])
        invocation.return_value_with_unix_fd_list(GLib.Variant("(h)", (0,)), fds)
        report(call="Inhibit", sender=sender, args=[what, who, why, mode], delay=number)

        def released(_channel: Any, _condition: Any) -> bool:
            self.delays.pop(number, None)
            os.close(read)
            report(released=number, sender=sender)
            return False

        GLib.io_add_watch(
            GLib.IOChannel.unix_new(read), GLib.PRIORITY_DEFAULT, GLib.IOCondition.HUP | GLib.IOCondition.ERR, released
        )

    def session_call(
        self, conn: Any, sender: str, _path: str, _iface: str, method: str, params: Any, invocation: Any
    ) -> None:
        if method == "Lock":
            self.locked = True
            report(call="Lock", sender=sender)
            invocation.return_value(None)

    def session_get(self, conn: Any, sender: str, _path: str, _iface: str, prop: str) -> GLib.Variant:
        values: dict[str, GLib.Variant] = {
            "Class": GLib.Variant("s", "user"),
            "Type": GLib.Variant("s", "wayland"),
            "State": GLib.Variant("s", "active"),
            "Active": GLib.Variant("b", True),
            "Remote": GLib.Variant("b", False),
            "Service": GLib.Variant("s", "sddm"),
            "LockedHint": GLib.Variant("b", self.locked),
            "Desktop": GLib.Variant("s", "KDE"),
        }
        return values[prop]

    def user_get(self, conn: Any, sender: str, _path: str, _iface: str, prop: str) -> GLib.Variant:
        return GLib.Variant("b", True)

    def powerdevil_call(
        self, conn: Any, sender: str, _path: str, _iface: str, method: str, params: Any, invocation: Any
    ) -> None:
        args = list(params.unpack())
        if method == "Inhibit":
            cookie = self.next_cookie
            self.next_cookie += 1
            report(call="PowerDevil.Inhibit", sender=sender, args=args, cookie=cookie)
            invocation.return_value(GLib.Variant("(u)", (cookie,)))
        elif method == "UnInhibit":
            report(call="PowerDevil.UnInhibit", sender=sender, args=args)
            invocation.return_value(None)

    def prepare_for_sleep(self, starting: bool) -> bool:
        self.system.emit_signal(
            None, MANAGER_PATH, "org.freedesktop.login1.Manager", "PrepareForSleep", GLib.Variant("(b)", (starting,))
        )
        report(signal="PrepareForSleep", args=[starting])
        return False

    def start_gamemode(self) -> None:
        node = Gio.DBusNodeInfo.new_for_xml(GAMEMODE_XML)
        self.session.register_object(GAMEMODE_PATH, node.interfaces[0], None, self.gamemode_get, None)
        Gio.bus_own_name_on_connection(
            self.session, GAMEMODE, Gio.BusNameOwnerFlags.NONE, lambda _conn, _name: report(gamemode=True), None
        )

    def gamemode_get(self, conn: Any, sender: str, _path: str, _iface: str, prop: str) -> GLib.Variant:
        report(call="GameMode.ClientCount", sender=sender, value=self.clients)
        return GLib.Variant("i", self.clients)

    def game(self, on: bool) -> None:
        self.clients += 1 if on else -1
        member = "GameRegistered" if on else "GameUnregistered"
        game = GLib.Variant("(io)", (4321, f"{GAMEMODE_PATH}/Games/4321"))
        self.session.emit_signal(None, GAMEMODE_PATH, GAMEMODE, member, game)
        changed = GLib.Variant("(sa{sv}as)", (GAMEMODE, {"ClientCount": GLib.Variant("i", self.clients)}, []))
        self.session.emit_signal(None, GAMEMODE_PATH, "org.freedesktop.DBus.Properties", "PropertiesChanged", changed)
        report(game=on, clients=self.clients)

    def command(self, line: str) -> None:
        order = json.loads(line)
        if order.get("gamemode"):
            self.start_gamemode()
        if "game" in order:
            self.game(bool(order["game"]))
        if "sleep" in order:
            self.prepare_for_sleep(bool(order["sleep"]))
        if "locked" in order:
            self.locked = bool(order["locked"])
            report(locked=self.locked)


def main() -> int:
    system, session = connect(sys.argv[1]), connect(sys.argv[2])
    desktop = Desktop(system, session, int(sys.argv[3]))
    desktop.export()
    loop = GLib.MainLoop()
    owned: list[str] = []

    def acquired(_conn: Any, name: str) -> None:
        owned.append(name)
        if len(owned) == 2:
            report(ready=True)

    Gio.bus_own_name_on_connection(system, LOGIN, Gio.BusNameOwnerFlags.NONE, acquired, None)
    Gio.bus_own_name_on_connection(session, POWERDEVIL, Gio.BusNameOwnerFlags.NONE, acquired, None)
    pending = b""

    def stdin_ready(_fd: Any, _condition: Any) -> bool:
        nonlocal pending
        chunk = os.read(0, 65536)
        if not chunk:
            loop.quit()
            return False
        pending += chunk
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            if line.strip():
                desktop.command(line.decode())
        return True

    GLib.io_add_watch(
        GLib.IOChannel.unix_new(0), GLib.PRIORITY_DEFAULT, GLib.IOCondition.IN | GLib.IOCondition.HUP, stdin_ready
    )
    loop.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
