# Security and privacy

What the integration stores, who can reach it, and what the PC sends it. The full threat model is in
the agent's [docs/security.md](https://github.com/ovflowd/torpor/blob/main/docs/security.md). To
report a vulnerability, see [SECURITY.md](../SECURITY.md).

- Home Assistant stores the PC's pairing key in the config entry, so it is in every backup. Turn on
  backup encryption.
- The pairing code crosses your network once, to your browser. Over plain HTTP, someone watching
  could read it; using it would change the fingerprint you compare. Pair over https, or from a
  browser on the Home Assistant host, to avoid even that.
- The endpoint takes connections only from your local network, unless you allow outside
  connections for a PC. It limits connection attempts per address and handshakes in flight.
- A PC with an old key never gets its own address banned: it gives up before it sends anything that
  counts as a failed login, and Home Assistant shows a repair instead.
- What the PC sends: its host name, OS, maker and model, network cards and MAC addresses, display
  names, session state, lease names, the names of apps that block sleep, whether a game runs (not
  which) and how it can tell, and, for a display with a mode code, the value the monitor reports.
  Never user names, command lines, files or the key.
- Every Home Assistant user can press every Torpor button: Home Assistant has no per-entity
  permissions.
- New entities may be exposed to voice assistants. Check **Settings > Voice assistants > Expose**
  after pairing, and don't expose Power off, Restart or Keep awake unless you mean to.
