# ha-torpor

<!-- ha-marketplace:exclude-start -->
[![CI](https://github.com/ovflowd/ha-torpor/actions/workflows/ci.yml/badge.svg)](https://github.com/ovflowd/ha-torpor/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Open this repository in your Home Assistant Marketplace.](https://my.home-assistant.io/badges/marketplace_repository.svg)](https://my.home-assistant.io/redirect/marketplace_repository/?owner=ovflowd&repository=ha-torpor&category=integration)
<!-- ha-marketplace:exclude-end -->

Home Assistant integration for Torpor: see, sleep, wake and dim your Linux PC.

A small agent on the PC, [`torpord`](https://github.com/ovflowd/torpor), tells Home Assistant
whether someone is using it, and Home Assistant can lock it, put it to sleep, wake it and set its
monitors' brightness. The PC dials this integration over an encrypted connection, holds no Home
Assistant token, and decides what Home Assistant may ask of it.

> [!WARNING]
> Anyone who can use your Home Assistant, and any automation, can lock your PC and put it to sleep.
> Power off, restart and hibernate stay off until you allow them on the PC; read
> [docs/security.md](docs/security.md) first. Nothing has been tested against a real PC yet, so use
> it at your own risk. Torpor isn't affiliated with Home Assistant or the Open Home Foundation.

## What you need

- Home Assistant 2026.11.0 or later
- A Linux PC running the Torpor agent 0.1 or later: its
  [quick start](https://github.com/ovflowd/torpor/blob/main/docs/quick-start.md) takes about ten
  minutes
- The PC and Home Assistant on the same local network

## Install

1. Select the **Open Marketplace repository** badge above, or in the Marketplace add
   `https://github.com/ovflowd/ha-torpor` as a custom repository of type **Integration**.
2. Download **Torpor**, and restart Home Assistant if the Marketplace asks you to.

[docs/setup.md](docs/setup.md#install) says how updates arrive, and how to install by hand.

## Pair a PC

[![Add Torpor to your Home Assistant.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=torpor)

1. Go to **Settings > Devices & services > Add integration** and pick **Torpor**. It shows a
   one-time code and the command to run on the PC.
2. On the PC, run that command, such as `torporctl pair --url http://192.168.1.10:8123`, and type
   the code when it asks.
3. If both sides show the same fingerprint, select **This is my PC**. If they don't, select
   **Cancel**.
4. Pick the features you want.

[docs/setup.md](docs/setup.md#pair-a-pc) has the details, and how to
[re-pair](docs/setup.md#re-pair).

## What you get

For a PC named `desk`:

| Entity | What it does |
|---|---|
| `sensor.desk_state` | `in_use`, `remote`, `busy`, `idle`, `asleep`, `off` or `restarting` |
| `button.desk_lock`, `button.desk_sleep` | Locks or suspends the PC |
| `button.desk_hibernate`, `button.desk_power_off`, `button.desk_restart` | The same, if the PC allows them |
| `switch.desk_keep_awake` | Keeps the PC from sleeping on its own |
| `button.desk_wake` | Wakes the PC with Wake-on-LAN |
| `number.desk_<display>_brightness`, `switch.desk_<display>_auto_brightness` | Brightness per display, by hand or from a light sensor |
| `event.desk_power` | Sleep, shutdown, restart, resume, boot, and whether a wake worked |

The `torpor.wake`, `torpor.sleep` and `torpor.keep_awake` actions do the same from scripts. The
entities stay while the PC sleeps, so automations keep working.
[docs/entities.md](docs/entities.md) lists every entity, state and action, and
[docs/recipes.md](docs/recipes.md) has automations to copy.

## Options

**Configure** on the PC's entry picks the features, where the Wake-on-LAN packet goes, how long Keep
awake lasts, and automatic brightness: a light sensor, the curve, a boost while a game runs, and a
curve per monitor mode. [docs/options.md](docs/options.md) explains each one.

Wake needs Wake-on-LAN turned on in the PC's firmware and OS, and automatic brightness needs a light
sensor in the same room.

## How it works

The PC always dials Home Assistant; Home Assistant never connects to it. Both sides prove the key
you paired, then every message is encrypted. The PC pushes each change as it happens, and says
goodbye before it sleeps or shuts down, so the state turns `asleep` or `off` at once.
[docs/how-it-works.md](docs/how-it-works.md) has the details, and
[docs/security.md](docs/security.md) what the integration stores and what the PC sends.

## Something not working?

[docs/troubleshooting.md](docs/troubleshooting.md) covers the usual problems and the known
limitations. To report one, open an [issue](https://github.com/ovflowd/ha-torpor/issues) and attach
the diagnostics from the PC's device page; they leave out the key, MAC addresses and the host name.

## Removal

1. Delete the PC's entry under **Settings > Devices & services > Torpor**.
2. On the PC, run `torporctl uninstall --purge`.
3. Remove Torpor from the Marketplace, or delete `custom_components/torpor`, and restart.

[docs/setup.md](docs/setup.md#remove) says what each step removes.

## Related projects

- [torpor](https://github.com/ovflowd/torpor), the agent on the PC, with its own docs for the PC
  side.

## Development

```sh
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

The tests drive the real endpoint with a fake agent, so no PC is needed, and `tests/e2e/` runs the
real agent against fakes. `protocol.py` and the test vectors come from
[ovflowd/torpor](https://github.com/ovflowd/torpor): change them there first. See
[CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
