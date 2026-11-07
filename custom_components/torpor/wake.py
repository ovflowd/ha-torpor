"""Wake-on-LAN: the magic packet, sent from Home Assistant to the MACs the PC reported."""

from __future__ import annotations

import asyncio
from socket import AF_INET, SO_BROADCAST, SOCK_DGRAM, SOL_SOCKET, socket

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN, WAKE_GAP_S, WAKE_SENDS


def magic_packet(mac: str) -> bytes:
    raw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    if len(raw) != 6:
        raise ValueError(f"not a MAC address: {mac}")
    return b"\xff" * 6 + raw * 16


def _send(packets: list[bytes], broadcast: str, port: int) -> None:
    with socket(AF_INET, SOCK_DGRAM) as sock:
        sock.setsockopt(SOL_SOCKET, SO_BROADCAST, 1)
        for packet in packets:
            sock.sendto(packet, (broadcast, port))


async def async_send_magic_packets(hass: HomeAssistant, macs: tuple[str, ...], broadcast: str, port: int) -> None:
    """Send one packet per MAC, WAKE_SENDS times, WAKE_GAP_S apart: a single UDP datagram is easy to lose."""
    packets = [magic_packet(mac) for mac in macs]
    for attempt in range(WAKE_SENDS):
        if attempt:
            await asyncio.sleep(WAKE_GAP_S)
        try:
            await hass.async_add_executor_job(_send, packets, broadcast, port)
        except OSError as error:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="wake_send_failed",
                translation_placeholders={"error": str(error)},
            ) from error
