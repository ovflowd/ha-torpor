# Entities and actions

Everything Torpor adds to Home Assistant, for a PC named `desk`. Every ID on this page is synthetic.

## Entities

| Entity | What it shows or does | Available |
|---|---|---|
| `sensor.desk_state` | `in_use`, `remote`, `busy`, `idle`, `asleep`, `off` or `restarting`. Attributes: `session`, `display_on`, `remote`, `busy` | Always |
| `binary_sensor.desk_agent` | Whether the agent is connected | Always |
| `event.desk_power` | `suspending`, `shutting_down`, `restarting`, `resumed`, `booted`, `wake_succeeded`, `wake_failed` | Always |
| `sensor.desk_last_boot` | When the PC last booted. Off by default | When known |
| `button.desk_lock` | Locks your sessions | Connected |
| `button.desk_sleep` | Suspends | Connected |
| `button.desk_hibernate` | Hibernates | Connected, and the PC can hibernate right now |
| `button.desk_power_off`, `button.desk_restart` | Off by default; enable them on the entity's page | Connected |
| `sensor.desk_sleep_blockers` | How many apps block sleep; attribute `blockers`. Off by default | Connected |
| `switch.desk_keep_awake` | Keeps the PC from sleeping on its own; attribute `ends_at` | Connected |
| `button.desk_wake` | Sends the Wake-on-LAN packet | Always |
| `number.desk_<display>_brightness` | Brightness of one display, 0 to 100 % | Connected, the display is plugged in, and its brightness is known |
| `switch.desk_<display>_auto_brightness` | Automatic brightness for one display; attributes `overridden`, `target` | Connected and the display is plugged in |
| `sensor.desk_<display>_mode` | Diagnostic. The monitor mode's label, or its raw value such as `0x01`; attributes `code`, `value`, `error`. Only for a display with a mode code | Connected and the display is plugged in |
| `binary_sensor.desk_game_running` | Whether a game runs. Only for a PC with GameMode installed, or with power-profiles-daemon installed and a performance-hold pattern | Connected, and the PC can tell |

Which buttons appear, and when automatic brightness pauses (`overridden`), is in
[options](options.md).

## States

| State | Meaning |
|---|---|
| `in_use` | Someone is at the desk: an unlocked session with the display on. When the PC can't tell, it says `in_use`, the state in which automations leave it alone |
| `remote` | Someone is logged in over SSH or a remote desktop |
| `busy` | Keep awake is on, a lease is held, or a process you listed is running |
| `idle` | None of the above |
| `asleep`, `off`, `restarting` | The PC said so before it went. `restarting` turns into unknown after 15 minutes without a reconnect |

When the connection drops without a goodbye, the state stays for 120 seconds, then turns unknown.
The entities stay in place while the PC sleeps, also across Home Assistant restarts, so automations
that target them keep working; the controls are unavailable until the PC is back.

A display that you unplug keeps its entities for 30 days, while the agent remembers it. An entity
goes away only when a connected PC stops reporting it, or when you turn its feature off.

## Actions

| Action | Fields | Does |
|---|---|---|
| `torpor.wake` | `device_id`, `wait` (default false) | Sends the magic packet unless the PC is already connected. Returns `already_awake` or `sent`, or with `wait`, `awake` or `timeout` |
| `torpor.sleep` | `device_id`, `hibernate` (default false), `force` (default false) | Suspends or hibernates. `force` sleeps even when other apps block sleep; the system itself can still refuse |
| `torpor.keep_awake` | target: the Keep awake switch; `duration` | Keeps the PC awake for that long. `0` keeps it awake until you turn the switch off |

```yaml
action: torpor.wake
data:
  device_id: 4f0b6c1e2d3a4b5c6d7e8f9a0b1c2d3e
  wait: true
response_variable: wake
```

When the PC refuses, the action fails with the reason, for example "The PC refused: 1 app blocks
sleep". A PC that doesn't answer within 10 seconds fails with a timeout.

Copy-paste automations, including waking a PC behind a smart plug and telling off from asleep with a
power sensor, are in [recipes](recipes.md).
