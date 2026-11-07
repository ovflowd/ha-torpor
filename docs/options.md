# Options

You choose these when you pair, and change them later with **Configure** on the PC's entry. They
work while the PC sleeps, from what it reported last. Saving reloads the integration; the PC
reconnects within seconds.

Wake needs a wired network card with Wake-on-LAN turned on in the firmware and the OS: see the
agent's [Wake-on-LAN guide](https://github.com/ovflowd/torpor/blob/main/docs/wake-on-lan.md).
Automatic brightness needs an illuminance sensor in the same room.

| Section | Option | Default | What it does |
|---|---|---|---|
| Features | Power | on | Lock, Sleep, Hibernate, Power off and Restart buttons. Only the actions the PC's `[allow]` permits appear |
| | Keep awake | on | The Keep awake switch |
| | Wake-on-LAN | on | The Wake button and the `torpor.wake` action |
| | Brightness | on | A brightness control per display, and automatic brightness |
| Wake-on-LAN | Network cards | the card that reaches Home Assistant | Where the magic packet goes. The list shows whether each card reports Wake-on-LAN as armed |
| | Broadcast address | `255.255.255.255` | Where the packet is sent. Use your network's own broadcast address, such as `192.168.1.255`, if the default doesn't reach the PC |
| | Port | `9` | The packet's UDP port |
| | Wake timeout | 120 s | How long to wait for the PC to connect before a wake counts as failed |
| Keep awake | Hours | 12 | How long the switch stays on. `0` keeps it on until you turn it off |
| Automatic brightness | Light sensor | none | An illuminance sensor. Without one there is no automatic brightness switch |
| | Anchor light level | 100 lx | The light level the curve is built around |
| | Brightness at the anchor | 40 % | The brightness at that light level |
| | Slope | 25 | Points of brightness per tenfold change in light |
| | Lowest brightness | 10 % | Automatic brightness never goes below this |
| | Highest brightness | 80 % | Automatic brightness never goes above this |
| | Game boost | 30 % | Points added while a game runs, up to 100 %, even past the highest brightness. `0` turns it off |
| | Monitor modes | none | Rows of display, VCP code, value, anchor and label: a curve anchor per value of a monitor setting that changes how bright a percentage looks. Only for monitors the PC can read over DDC/CI |
| Advanced | Allow agents outside the local network | off | Lets this PC connect from a non-local address, such as a VPN or Home Assistant Cloud. Pairing stays local only |

A feature appears when the PC reports it as on and working, or when it is still turned on here, so
you can turn it off. The others are listed with the reason, for example "no display found". Turning
a feature off keeps its settings for when you turn it on again.

## Automatic brightness

The brightness curve is `anchor % + slope × log10(lux / anchor lux)`, kept between the lowest and
highest brightness. The defaults suit a desk lamp in an office; tune them to your room.

Brightness that you change by hand, on the monitor or in the desktop, pauses automatic brightness
for that display (`overridden: true`). It resumes when the target moves 6 points or more, because
the light changed a lot or a game's boost came or went; when the monitor changes to a mode that has
a row; when you turn the switch off and on; or after the PC wakes.

## Monitor modes

On the ASUS ROG PG39WCDM, the OSD's Power Setting is VCP code `E1`: `00` Standard and `01` Power
Saving, and the same percentage looks much dimmer on Power Saving. Two rows give each its own
anchor:

| Display | Code | Value | Anchor | Label |
|---|---|---|---|---|
| PG39WCDM | `E1` | `00` | 40 % | Standard |
| PG39WCDM | `E1` | `01` | 75 % | Power Saving |

The PC reads the code with ddcutil, never writes it, and keeps clear of the desktop's own brightness
writes; [the agent's brightness page](https://github.com/ovflowd/torpor/blob/main/docs/modules/brightness.md#monitor-modes)
explains how. A change to a mode that has a row is written within about two minutes and ends a
hand-set pause; a value with no row keeps the last mode, and the Mode sensor's `error` names it. A
row with a code and no value only reads it: use that, and the display's Mode sensor, to learn a
monitor's values. In the list of rows a display shows by its ID; the field's help says which display
each ID is. Reading modes needs ddcutil 2.2 or later and access to `/dev/i2c-*` on the PC; without
them a repair says so, and the display uses its lowest anchor.

## Game boost

What counts as a game is set on the PC: a Feral GameMode client, or a power-profiles-daemon
performance hold matching `[games] performance-hold` in `torpor.conf` (on CachyOS,
`game-performance`). A start or end counts once it has held 10 seconds, and is written within a
minute of the last write. With the boost at 0, a game changes nothing.
