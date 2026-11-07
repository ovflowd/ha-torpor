# Contributing

Thanks for helping. ha-torpor is small on purpose, and it can put PCs to sleep and power them off,
so a change should be small too: one change per pull request, and a behavior change comes with a
test.

## Setup

```sh
git clone https://github.com/ovflowd/ha-torpor.git
cd ha-torpor
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

The tests need no PC and no network. They run Home Assistant 2026.11.0 from
[pytest-homeassistant](https://pypi.org/project/pytest-homeassistant/), which needs Python 3.14.2 or
later, and drive the real WebSocket endpoint with a fake agent (`tests/fake_agent.py`) built on the
shared `protocol.py`. Coverage must stay at 95 % or more, and at 100 % for `config_flow.py`.

### The end-to-end test

`tests/e2e/test_real_agent.py` pairs a real `torpord` with Home Assistant through the config flow
and runs it through sleep, wake and two Home Assistant restarts, and through a monitor mode change
and a game. Run it before you change the endpoint, the connection, the flows or the brightness
options:

```sh
git clone https://github.com/ovflowd/torpor.git ../torpor
uv run pytest tests/e2e
```

It builds `torpord` from that checkout next to this one, or from where `TORPOR_CHECKOUT` points. It
finds an interpreter with PyGObject, Soup 3 and cryptography in `TORPOR_PYTHON`, in the checkout's
`.venv-gi` (its CONTRIBUTING.md shows how to make one), or at `/usr/bin/python3`, and it needs
`dbus-daemon`. Without them, or without the checkout, it skips. The agent runs on private buses with
a fake desktop, synthetic `/sys`, `/proc` and `/dev` and a fake `ddcutil`, so nothing on your PC
changes. CI runs it against ovflowd/torpor's `main` on Ubuntu's packages.

## Style

- Ruff formats and lints, with the settings in `pyproject.toml` (120 columns). CI checks both.
- A comment says why, not what. Do not restate the code.
- Every `await` is a statement of its own: `result = await call()`, never `f(await call())` or
  `assert await call() == value`.
- Type hints on every function, `test_*` functions excepted.
- No new requirements in `manifest.json`. The integration uses only what Home Assistant ships.
- Every string a user sees lives in `translations/en.json`, and every entity and action has an icon
  in `icons.json`. `tests/test_metadata.py` checks both. The one exception is the short lists the
  forms build from what the PC reports, such as its network cards and why a feature is missing:
  `options.py` makes those, in English.
- Commit messages are an imperative sentence with a type, as in `fix: keep the stale-key repair per
  entry`, and the body says why.

## Changing the protocol

`custom_components/torpor/protocol.py`, `tests/test_protocol.py` and `tests/vectors/` are copies
from [ovflowd/torpor](https://github.com/ovflowd/torpor), byte for byte, and
`tests/vectors/SHA256SUMS` pins them. Never edit them here alone. Change the protocol in
ovflowd/torpor first, following its CONTRIBUTING.md, then copy the three paths here unchanged. CI
compares `SHA256SUMS` with ovflowd/torpor's `main`.

## Changing what Home Assistant can make a PC do

- A new command needs a protocol change first, and an `[allow]` entry on the PC.
- The endpoint calls `process_wrong_login` only for a wrong proof against a key it holds. Keep it
  that way: a PC with an old key must never get its own address banned. `tests/test_endpoint.py`
  checks it.
- Never log or put into diagnostics a pairing code, a key or a MAC address. Diagnostics leave out
  host names too. Logs may name the PC by its entry title, as Home Assistant's own integrations do.

Fixtures and docs never hold real host names, MAC addresses or monitor serials: use synthetic ones,
such as `02:00:5e:10:00:01`.

## AI assistants

If you used an AI assistant for a change, say so in the pull request. You remain responsible for
it, and for having read it.

## License of contributions

By contributing, you agree that your work is licensed under the [MIT license](LICENSE). This
project follows a [code of conduct](CODE_OF_CONDUCT.md).

## Reporting a problem

Questions and ideas are welcome as issues too. For a bug, include the integration and agent
versions, your Home Assistant version, `torporctl status --json` from the PC, and the diagnostics
from the PC's device page, which leave out the key, MAC addresses and host name. Never paste a
pairing code or a key. For a security problem, see [SECURITY.md](SECURITY.md).
