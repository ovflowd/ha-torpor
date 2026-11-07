# Troubleshooting

The PC side has its own
[troubleshooting page](https://github.com/ovflowd/torpor/blob/main/docs/troubleshooting.md), and
`torporctl doctor` on the PC checks most of what can go wrong there.

**The pairing page never moves on.** Check that `torporctl pair --url` uses an address the PC can
reach, with `http://` or `https://` as you open Home Assistant. `torporctl doctor` tests the
endpoint. A reverse proxy must pass WebSocket upgrades on `/api/torpor/v1/ws`.

**"Is this your PC?" shows a fingerprint that doesn't match.** Cancel. Another machine used the
code, or something sits between the PC and Home Assistant. Start again with a fresh code.

**A repair says the PC uses an old pairing key.** The entry was re-paired or re-created, but the PC
kept its old key. Select **Reconfigure** on the entry and run `torporctl pair` on the PC.

**The agent shows as disconnected and nothing else is wrong.** Run `torporctl status` on the PC: it
shows the last close code and what it means. `4403` means the PC isn't on the local network as
Home Assistant sees it, for example behind a VPN or a proxy; see the Advanced option in
[options](options.md).

**Wake does nothing.** Check the repair list for "Wake-on-LAN is off". Try your network's broadcast
address instead of `255.255.255.255`. Wake from a power cut doesn't work; see the
[limitations](#limitations).

**A button fails with "observe-only mode".** The agent was installed with `--observe-only`, for
running beside another tool. It reports state and executes nothing.

**A feature is missing from the options.** The PC doesn't offer it right now. The options page lists
why; `torporctl doctor` on the PC says how to fix it.

## Reporting a problem

Download the diagnostics from the PC's device page and attach them to an issue. They leave out the
key, the agent ID, MAC addresses, the host name and addresses.
[CONTRIBUTING.md](../CONTRIBUTING.md#reporting-a-problem) says what else to include.

## Limitations

- Wake-on-LAN rarely survives a power cut: a PC that lost power wakes only from a button press or a
  firmware "power on after AC loss" setting. See
  [the plug recipe](recipes.md#wake-a-pc-behind-a-smart-plug).
- `off` and `asleep` come from the PC's goodbye. A PC that crashed or lost power shows its last
  state for 120 seconds, then unknown. A power sensor can tell them apart: see
  [the recipe](recipes.md#tell-off-from-asleep-with-a-power-sensor).
- On systemd older than 254 the PC can't tell a reboot from a power off, and reports `off`.
- One user per PC: the user who runs the agent. Its controls act on that user's sessions; the state
  counts anyone at the desk.
- A display manager's own idle suspend at the login screen is out of Keep awake's reach.
- The magic packet is a broadcast, so Home Assistant must be on the PC's network segment, or your
  router must forward directed broadcasts.
