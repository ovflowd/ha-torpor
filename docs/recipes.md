# Recipes

Automations and scripts to copy. Torpor itself only talks to the PC; anything about smart plugs,
power meters or presence is an ordinary Home Assistant automation, so you can change it freely.

Paste a recipe into a new automation or script with **Edit in YAML**, then replace the entity IDs
with yours. For the `device_id` fields, switch back to the visual editor and pick your PC in the
**Device** field; the YAML then shows its ID. Every ID and name below is synthetic, for a PC named
`desk`.

- [Wake a PC behind a smart plug](#wake-a-pc-behind-a-smart-plug)
- [Tell off from asleep with a power sensor](#tell-off-from-asleep-with-a-power-sensor)
- [Sleep the PC when everyone has left](#sleep-the-pc-when-everyone-has-left)
- [Keep it awake while something runs](#keep-it-awake-while-something-runs)
- [Get told when a wake fails](#get-told-when-a-wake-fails)
- [Dim the room while a game runs](#dim-the-room-while-a-game-runs)
- [Set a monitor's modes](#set-a-monitors-modes)

## Wake a PC behind a smart plug

When a PC hangs off a smart plug, or a plug behind a power strip that is itself on a smart plug,
the plugs must be on before Waking means anything. This script turns them on in order, waits for
each to show up, waits for the network card, then wakes the PC.

```yaml
alias: Wake the desk PC
mode: single
sequence:
  - alias: The power strip comes first
    action: switch.turn_on
    target:
      entity_id: switch.desk_power_strip
  - alias: The PC's plug is unavailable until the strip has power
    wait_template: "{{ has_value('switch.desk_plug') }}"
    timeout: "00:01:00"
    continue_on_timeout: false
  - action: switch.turn_on
    target:
      entity_id: switch.desk_plug
  - alias: Give the network card time to come up
    delay: "00:00:20"
  - action: torpor.wake
    data:
      device_id: 4f0b6c1e2d3a4b5c6d7e8f9a0b1c2d3e
      wait: true
    response_variable: wake
  - if:
      - condition: template
        value_template: "{{ wake.result == 'timeout' }}"
    then:
      - action: persistent_notification.create
        data:
          title: The desk PC didn't wake
          message: >-
            If the plug was off, the PC lost power, and Wake-on-LAN rarely survives that.
            Press its power button, or turn on "power on after AC loss" in its firmware.
```

With a single plug, keep the `switch.desk_plug` steps and drop the power strip and the wait.

Why the order and the wait matter: a plug behind another plug reads `unavailable` until the first
one has power, and Home Assistant skips unavailable targets, so turning both on at once silently
leaves the second one off.

Why a timeout is likely after a power cut: most network cards come back unarmed when they lose
power, so the magic packet finds nobody listening. A PC whose firmware powers on after AC loss boots
by itself when the plug turns on, and `wake.result` is `awake` once it connects. The agent's
[Wake-on-LAN guide](https://github.com/ovflowd/torpor/blob/main/docs/wake-on-lan.md) has the
details.

## Tell off from asleep with a power sensor

The PC says `asleep` or `off` before it goes. A PC that crashed, lost power or was switched off at
the button says nothing, so Torpor shows unknown. A plug with a power meter can tell them apart.

Measure your PC first: read the plug's power while the PC sleeps, and while it is off. Then create a
template sensor: **Settings > Devices & services > Helpers > Create helper > Template > Template a
sensor**, and use this state template, with your thresholds:

```jinja
{% set torpor = states('sensor.desk_state') %}
{% set watts = states('sensor.desk_plug_power') | float(-1) %}
{% if is_state('binary_sensor.desk_agent', 'on') or watts < 0 %}
  {{ torpor }}
{% elif watts < 2 %}
  off
{% elif watts < 15 %}
  asleep
{% else %}
  {{ torpor }}
{% endif %}
```

Leave the device class empty. In automations, use the same states as `sensor.desk_state`: `off`,
`asleep` and Torpor's own. While the agent is connected, the template sensor follows Torpor;
otherwise the meter decides.

## Sleep the PC when everyone has left

Only an `idle` PC is put to sleep: not one that someone uses at the desk or remotely, and not one
that is busy with Keep awake, a lease or a process you listed on the PC.

```yaml
alias: Sleep the desk PC when everyone has left
triggers:
  - trigger: numeric_state
    entity_id: zone.home
    below: 1
    for: "00:10:00"
conditions:
  - condition: state
    entity_id: sensor.desk_state
    state: idle
actions:
  - action: torpor.sleep
    data:
      device_id: 4f0b6c1e2d3a4b5c6d7e8f9a0b1c2d3e
```

If an app blocks sleep, for example a video player, the PC refuses and the automation run shows why.
Add `force: true` to sleep anyway; the system itself can still refuse.

## Keep it awake while something runs

For something Home Assistant knows about, such as a backup on another machine:

```yaml
alias: Keep the desk PC awake during the backup
mode: queued
triggers:
  - trigger: state
    entity_id: binary_sensor.nas_backup_running
    to: "on"
    id: started
  - trigger: state
    entity_id: binary_sensor.nas_backup_running
    to: "off"
    id: finished
actions:
  - choose:
      - conditions:
          - condition: trigger
            id: started
        sequence:
          - action: torpor.keep_awake
            target:
              entity_id: switch.desk_keep_awake
            data:
              duration: "04:00:00"
      - conditions:
          - condition: trigger
            id: finished
        sequence:
          - action: switch.turn_off
            target:
              entity_id: switch.desk_keep_awake
```

The duration is a ceiling: if Home Assistant restarts mid-backup and misses the end, the PC still
goes back to its normal sleep after four hours.

For something that runs on the PC itself, a lease there is simpler, and needs no automation:
`torporctl hold backup -- restic backup ~` keeps the PC awake while the command runs, and the state
shows `busy` with `lease:backup`.

## Get told when a wake fails

The `event.desk_power` entity fires `wake_failed` when a wake times out, from the Wake button, the
`torpor.wake` action, or a recipe above.

```yaml
alias: Tell me when the desk PC doesn't wake
triggers:
  - trigger: state
    entity_id: event.desk_power
    not_from: unavailable
conditions:
  - condition: state
    entity_id: event.desk_power
    attribute: event_type
    state: wake_failed
actions:
  - action: persistent_notification.create
    data:
      title: The desk PC didn't wake
      message: Check its plug, its Wake-on-LAN setting, or press its power button.
```

The same pattern works for `booted`, `resumed`, `suspending`, `shutting_down` and `restarting`.

## Dim the room while a game runs

`binary_sensor.desk_game_running` exists when the PC can tell: Feral GameMode is installed, or
`[games] performance-hold` is set in its `torpor.conf`. The monitor's own boost is the Game boost
option; this does the room.

```yaml
alias: Dim the office while a game runs
mode: restart
triggers:
  - trigger: state
    entity_id: binary_sensor.desk_game_running
    to: "on"
    id: started
  - trigger: state
    entity_id: binary_sensor.desk_game_running
    to: "off"
    for: "00:01:00"
    id: ended
actions:
  - choose:
      - conditions:
          - condition: trigger
            id: started
        sequence:
          - action: light.turn_on
            target:
              entity_id: light.office_ceiling
            data:
              brightness_pct: 20
      - conditions:
          - condition: trigger
            id: ended
        sequence:
          - action: light.turn_on
            target:
              entity_id: light.office_ceiling
            data:
              brightness_pct: 80
```

The minute before the lights come back rides out a game that crashes and starts again.

## Set a monitor's modes

Not an automation, but the steps, for a monitor whose own menu changes how bright a percentage
looks. The example is the ASUS ROG PG39WCDM's Power Setting, VCP code `E1`.

1. On the PC, check that it can read monitor modes: run `torporctl doctor` and look at `modes`. It
   says whether reads would work before any display has a mode code: they need ddcutil 2.2 or later
   and access to `/dev/i2c-*`.
2. In the integration's options, under **Automatic brightness**, add a row in **Monitor modes**:
   the display, code `E1`, no value. Save.
3. Switch the setting in the monitor's menu and watch `sensor.desk_<display>_mode`; within two
   minutes it shows the raw value, here `0x00` for Standard and `0x01` for Power Saving.
4. Replace the row with one row per value, each with the anchor that looks right at the anchor light
   level and a label:

   | Display | Code | Value | Anchor | Label |
   |---|---|---|---|---|
   | PG39WCDM | `E1` | `00` | 40 % | Standard |
   | PG39WCDM | `E1` | `01` | 75 % | Power Saving |

5. If a mode's anchor is near the highest brightness, raise that too: the ceiling still applies.

From then on, switching the setting in the monitor's menu moves the brightness to that mode's curve
within about two minutes, and ends a pause you set by hand. In the list of rows, each display shows
by its ID; the help under the list says which display each ID is.
