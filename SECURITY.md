# Security policy

## Reporting a vulnerability

Please report a vulnerability privately, through
[GitHub's private vulnerability reporting](https://github.com/ovflowd/ha-torpor/security/advisories/new),
and not in a public issue. Include what you found, how to reproduce it and what it affects. You will
get an answer within a week.

If that page does not open, open an issue that only says you have a security problem to report, and
without any detail, so that a way to talk privately can be arranged.

Never paste a pairing code, a pairing key or an unredacted diagnostics file into an issue.

## Scope

In scope are problems in this repository's code, for example a way to make the integration:

- accept a connection, a command result or a state without a valid proof of the PC's key;
- accept a connection from outside the local network when that PC doesn't allow it, or pair from
  outside it at all;
- count a PC with an old key as a failed login, so that Home Assistant bans its address;
- reveal whether a PC or a pairing code exists to someone who doesn't hold its key or code;
- send a command the PC's `[allow]` list or the integration's options don't permit;
- leak the pairing key, the code or MAC addresses into logs or diagnostics, or host names into
  diagnostics.

Problems in the protocol itself, in the agent on the PC, or in `torporctl` belong to
[ovflowd/torpor](https://github.com/ovflowd/torpor/security/advisories/new). Problems in Home
Assistant or in the Marketplace belong to those projects.

## What the integration touches

See [Security and privacy](docs/security.md) in the docs and the agent's
[threat model](https://github.com/ovflowd/torpor/blob/main/docs/security.md).

## Supported versions

Only the latest commit on `main`.
