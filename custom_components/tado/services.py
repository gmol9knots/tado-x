"""Services for the Tado integration."""

import logging

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import selector

from .const import (
    ATTR_MESSAGE,
    CONF_CONFIG_ENTRY,
    CONF_READING,
    DOMAIN,
    SERVICE_ADD_METER_READING,
)

_LOGGER = logging.getLogger(__name__)
SCHEMA_ADD_METER_READING = vol.Schema(
    {
        vol.Required(CONF_CONFIG_ENTRY): selector.ConfigEntrySelector(
            {
                "integration": DOMAIN,
            }
        ),
        vol.Required(CONF_READING): vol.Coerce(int),
    }
)


@callback
def setup_services(hass: HomeAssistant) -> None:
    """Set up the services for the Tado integration."""

    async def add_meter_reading(call: ServiceCall) -> None:
        """Send meter reading to Tado."""
        entry_id: str = call.data[CONF_CONFIG_ENTRY]
        reading: int = call.data[CONF_READING]
        _LOGGER.debug("Add meter reading %s", reading)

        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            raise ServiceValidationError("Config entry not found")

        tadoconnector = entry.runtime_data

        response: dict = await hass.async_add_executor_job(
            tadoconnector.set_meter_reading, call.data[CONF_READING]
        )

        if ATTR_MESSAGE in response:
            raise HomeAssistantError(response[ATTR_MESSAGE])

    hass.services.async_register(
        DOMAIN, SERVICE_ADD_METER_READING, add_meter_reading, SCHEMA_ADD_METER_READING
    )

async def async_set_temperature_offset(hass, device_id: str, temperature_offset: float):
    """Set the offset via Tado Hops API."""
    client = hass.data[DOMAIN][DATA_CLIENT]  # existing Tado API client

    url = f"https://hops.tado.com/homes/{client.home_id}/roomsAndDevices/devices/{device_id}"
    payload = {"temperatureOffset": temperature_offset}

    headers = {
        "Authorization": f"Bearer {client.access_token}",
        "Content-Type": "application/json",
    }

    response = await hass.async_add_executor_job(
        client.session.patch,
        url,
        json=payload,
        headers=headers,
    )

    if response.status_code not in (200, 204):
        _LOGGER.error(f"Failed to set offset: {response.text}")
        return False

    return True
