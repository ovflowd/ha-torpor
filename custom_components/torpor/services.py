"""The torpor.wake, torpor.sleep and torpor.keep_awake actions."""

from __future__ import annotations

import probatio
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.components.switch import SwitchDeviceClass
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import service

from .connection import TorporConnection
from .const import DOMAIN

ATTR_DEVICE_ID = "device_id"
ATTR_WAIT = "wait"
ATTR_HIBERNATE = "hibernate"
ATTR_FORCE = "force"
ATTR_DURATION = "duration"

WAKE_SCHEMA = probatio.Schema(
    {
        probatio.Required(ATTR_DEVICE_ID): cv.string,
        probatio.Optional(ATTR_WAIT, default=False): cv.boolean,
    }
)
SLEEP_SCHEMA = probatio.Schema(
    {
        probatio.Required(ATTR_DEVICE_ID): cv.string,
        probatio.Optional(ATTR_HIBERNATE, default=False): cv.boolean,
        probatio.Optional(ATTR_FORCE, default=False): cv.boolean,
    }
)


def _connection(call: ServiceCall) -> TorporConnection:
    _device, entry = service.async_get_device_and_config_entry(call.hass, DOMAIN, call.data[ATTR_DEVICE_ID])
    connection: TorporConnection = entry.runtime_data
    return connection


async def _wake(call: ServiceCall) -> ServiceResponse:
    connection = _connection(call)
    result = await connection.async_wake(wait=call.data[ATTR_WAIT])
    return {"result": result} if call.return_response else None


async def _sleep(call: ServiceCall) -> None:
    connection = _connection(call)
    action = "hibernate" if call.data[ATTR_HIBERNATE] else "suspend"
    if not connection.settings.module_on("power") or not connection.capabilities.allows("power", action):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="action_not_allowed",
            translation_placeholders={"name": connection.name, "action": action},
        )
    await connection.async_command(action, {"force": call.data[ATTR_FORCE]})


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    hass.services.async_register(DOMAIN, "wake", _wake, WAKE_SCHEMA, SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, "sleep", _sleep, SLEEP_SCHEMA)
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        "keep_awake",
        entity_domain=SWITCH_DOMAIN,
        schema={probatio.Required(ATTR_DURATION): cv.positive_time_period},
        func="async_keep_awake",
        entity_device_classes=[SwitchDeviceClass.SWITCH],
    )
