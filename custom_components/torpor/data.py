"""State shared by the endpoint, the flows and the entries: pending pairings, limits, handshake counts."""

from __future__ import annotations

import time
from collections import Counter, OrderedDict, defaultdict, deque
from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv6Address, IPv6Network, ip_address
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util.hass_dict import HassKey
from homeassistant.util.network import is_local

from .const import DOMAIN, STORE_VERSION
from .protocol import CONNECTS_PER_IP, CONNECTS_WINDOW_S, MAX_PENDING_HANDSHAKES

if TYPE_CHECKING:
    from .pairing import PendingPairing


class ConnectLimiter:
    """At most `limit` new connections per remote address in a sliding window.

    The table is kept in order of each address's last connection, so quiet addresses drop off the
    front at no cost per request, and it never holds more than `size` addresses.
    """

    def __init__(self, limit: int = CONNECTS_PER_IP, window: float = CONNECTS_WINDOW_S, size: int = 4096) -> None:
        self.limit = limit
        self.window = window
        self.size = size
        self._seen: OrderedDict[str, deque[float]] = OrderedDict()

    def allow(self, remote: str) -> bool:
        now = time.monotonic()
        while self._seen and now - next(iter(self._seen.values()))[-1] >= self.window:
            self._seen.popitem(last=False)
        key = _bucket(remote)
        times = self._seen.get(key)
        if times is None:
            if len(self._seen) >= self.size:
                self._seen.popitem(last=False)
            times = self._seen[key] = deque()
        while times and now - times[0] >= self.window:
            times.popleft()
        if len(times) >= self.limit:
            return False
        times.append(now)
        self._seen.move_to_end(key)
        return True


def _bucket(remote: str) -> str:
    """Public IPv6 addresses count per /64, since one host usually holds the whole prefix.

    Local ones count per address, so PCs on the same network don't share one limit.
    """
    try:
        address = ip_address(remote)
    except ValueError:
        return remote
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if isinstance(address, IPv4Address) or is_local(address):
        return str(address)
    return str(IPv6Network((int(address) >> 64 << 64, 64)))


@dataclass
class TorporData:
    integration_version: str
    view_registered: bool = False
    pending_handshakes: int = 0
    max_pending: int = MAX_PENDING_HANDSHAKES
    limiter: ConnectLimiter = field(default_factory=ConnectLimiter)
    pairings: dict[str, PendingPairing] = field(default_factory=dict)
    stale_closes: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque))
    handshakes: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))


DATA: HassKey[TorporData] = HassKey(DOMAIN)


class TorporStore:
    """Per-entry state that is HA's own: the automatic-brightness switches and the last boot seen."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, f"{DOMAIN}.{entry_id}")
        self.auto_brightness: dict[str, bool] = {}
        self.boot_time: int | None = None
        self._unsaved = False

    async def async_load(self) -> None:
        stored = await self._store.async_load()
        if not isinstance(stored, dict):
            return
        auto = stored.get("auto_brightness")
        if isinstance(auto, dict):
            self.auto_brightness = {key: value for key, value in auto.items() if isinstance(value, bool)}
        boot_time = stored.get("boot_time")
        if isinstance(boot_time, int):
            self.boot_time = boot_time

    def save(self) -> None:
        self._unsaved = True
        self._store.async_delay_save(self._payload, 1)

    async def async_flush(self) -> None:
        """Write now, so no delayed write outlives the entry and recreates the file after a removal."""
        if self._unsaved:
            self._unsaved = False
            await self._store.async_save(self._payload())

    def _payload(self) -> dict[str, Any]:
        return {"auto_brightness": dict(self.auto_brightness), "boot_time": self.boot_time}

    async def async_remove(self) -> None:
        await self._store.async_remove()
