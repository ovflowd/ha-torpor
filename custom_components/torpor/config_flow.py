"""Pairing a PC (and re-pairing it through Reconfigure), and the options flow."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, override

from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.util import dt as dt_util

from . import protocol
from .capabilities import Capabilities
from .const import DATA_AGENT_ID, DATA_CAPABILITIES, DATA_CAPABILITIES_AT, DATA_KEY, DOMAIN
from .endpoint import async_ensure_view
from .options import build_schema, default_options, mode_displays_text, normalize, unavailable_text, validate
from .pairing import Outcome, PendingPairing
from .protocol import Close

DOCS_URL = "https://github.com/ovflowd/torpor/blob/main/docs/quick-start.md"
URL_PLACEHOLDER = "http://homeassistant.local:8123"


class TorporConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._pairing: PendingPairing | None = None
        self._task: asyncio.Task[Outcome] | None = None
        self._failure = ""
        self._close_reason = "pairing cancelled"
        self._paired = False

    @staticmethod
    @callback
    @override
    def async_get_options_flow(config_entry: ConfigEntry) -> TorporOptionsFlow:
        return TorporOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="user", description_placeholders={"docs": DOCS_URL})
        result = await self.async_step_pair()
        return result

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="reconfigure", description_placeholders={"name": self._get_reconfigure_entry().title}
            )
        result = await self.async_step_pair()
        return result

    async def async_step_pair(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._pairing is None:
            await async_ensure_view(self.hass)
            self._pairing = PendingPairing(self.hass)
            self._task = self.hass.async_create_background_task(self._pairing.wait(), "torpor pairing")
        assert self._task is not None
        if not self._task.done():
            return self.async_show_progress(
                step_id="pair",
                progress_action="pair",
                progress_task=self._task,
                description_placeholders={
                    "code": self._pairing.text,
                    "command": f"torporctl pair --url {self._url()}",
                    "docs": DOCS_URL,
                },
            )
        outcome = self._task.result()
        if outcome is Outcome.READY:
            return self.async_show_progress_done(next_step_id="confirm")
        self._failure = outcome.value
        return self.async_show_progress_done(next_step_id="failed")

    def _url(self) -> str:
        try:
            return get_url(self.hass, prefer_external=False)
        except NoURLAvailableError:
            return URL_PLACEHOLDER

    async def async_step_failed(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_abort(reason=self._failure)

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        pairing = self._ready_pairing()
        assert pairing.hello is not None
        hello = pairing.hello
        await self.async_set_unique_id(hello.agent_id)
        if self.source == SOURCE_RECONFIGURE:
            self._abort_if_unique_id_mismatch(reason="wrong_pc")
            menu = ["finish", "cancel"]
        else:
            try:
                self._abort_if_unique_id_configured()
            except AbortFlow:
                self._close_reason = "already paired, use Reconfigure"
                raise
            menu = ["features", "cancel"]
        return self.async_show_menu(
            step_id="confirm",
            menu_options=menu,
            description_placeholders={
                "hostname": hello.hostname,
                "os": hello.os or "-",
                "agent": hello.agent,
                "agent_id": hello.agent_id[:8],
                "fingerprint": pairing.fingerprint or "",
            },
        )

    async def async_step_cancel(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_abort(reason="pairing_rejected")

    async def async_step_features(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        pairing = self._ready_pairing()
        capabilities = Capabilities(pairing.capabilities)
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = validate(user_input)
            if not errors:
                options = normalize(user_input)
                confirmed = await pairing.confirm()
                if not confirmed:
                    return self.async_abort(reason=self._lost())
                self._paired = True
                return self.async_create_entry(title=capabilities.hostname, data=self._entry_data(), options=options)
        schema = self.add_suggested_values_to_schema(
            build_schema(capabilities, {}), user_input or default_options(capabilities)
        )
        return self.async_show_form(
            step_id="features",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "unavailable": unavailable_text(capabilities),
                "mode_displays": mode_displays_text(capabilities, {}),
            },
        )

    async def async_step_finish(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        pairing = self._ready_pairing()
        confirmed = await pairing.confirm()
        if not confirmed:
            return self.async_abort(reason=self._lost())
        self._paired = True
        return self.async_update_reload_and_abort(self._get_reconfigure_entry(), data_updates=self._entry_data())

    def _ready_pairing(self) -> PendingPairing:
        assert self._pairing is not None
        return self._pairing

    def _lost(self) -> str:
        return Outcome.TIMEOUT.value if self._ready_pairing().expired else Outcome.LOST.value

    def _entry_data(self) -> dict[str, Any]:
        pairing = self._ready_pairing()
        assert pairing.hello is not None
        assert pairing.key is not None
        return {
            DATA_AGENT_ID: pairing.hello.agent_id,
            DATA_KEY: protocol.b64(pairing.key),
            DATA_CAPABILITIES: pairing.capabilities,
            DATA_CAPABILITIES_AT: dt_util.utcnow().isoformat(),
        }

    @callback
    @override
    def async_remove(self) -> None:
        """The flow ended, however it ended: hand the PC off (4000) or tell it to give up (4410)."""
        if self._pairing is None:
            return
        if self._paired:
            close = self._pairing.close(Close.RECONNECT, "paired")
        else:
            close = self._pairing.close(Close.PAIRING_CANCELLED, self._close_reason)
        self.hass.async_create_task(close, "torpor pairing close")


class TorporOptionsFlow(OptionsFlowWithReload):
    """Works from the stored capabilities, so it works while the PC sleeps."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self.config_entry
        capabilities = Capabilities(entry.data.get(DATA_CAPABILITIES))
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = validate(user_input)
            if not errors:
                return self.async_create_entry(data=normalize(user_input))
        schema = self.add_suggested_values_to_schema(
            build_schema(capabilities, entry.options), user_input or entry.options
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "name": entry.title,
                "updated": _when(entry.data.get(DATA_CAPABILITIES_AT)),
                "unavailable": unavailable_text(capabilities),
                "mode_displays": mode_displays_text(capabilities, entry.options),
            },
        )


def _when(value: object) -> str:
    parsed: datetime | None = dt_util.parse_datetime(value) if isinstance(value, str) else None
    return dt_util.as_local(parsed).strftime("%Y-%m-%d %H:%M") if parsed is not None else "-"
