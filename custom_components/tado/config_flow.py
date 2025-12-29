"""Config flow for Tado integration."""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from PyTado.exceptions import TadoException
from PyTado.http import DeviceActivationStatus
from PyTado.interface import Tado
import requests.exceptions
import voluptuous as vol

from homeassistant.components import zeroconf
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .const import (
    CONF_FALLBACK,
    CONF_TOKEN_FILE,
    CONST_OVERLAY_TADO_DEFAULT,
    CONST_OVERLAY_TADO_OPTIONS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

ATTR_VERIFICATION_URL = "verification_url"


def _new_token_file_path(hass: HomeAssistant) -> str:
    return hass.config.path(
        ".storage", f"tado_refresh_token_{uuid4().hex}.json"
    )


class TadoConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Tado."""

    VERSION = 1

    def __init__(self) -> None:
        self._tado: Tado | None = None
        self._token_file: str | None = None

    async def _async_get_tado(self) -> Tado:
        if self._token_file is None:
            self._token_file = _new_token_file_path(self.hass)
        if self._tado is None:
            self._tado = await self.hass.async_add_executor_job(
                Tado, self._token_file
            )
        return self._tado

    async def _async_finish_setup(self, tado: Tado) -> ConfigFlowResult:
        try:
            tado_me = await self.hass.async_add_executor_job(tado.get_me)
        except TadoException as ex:
            raise InvalidAuth from ex
        except requests.exceptions.RequestException as ex:
            raise CannotConnect from ex

        if not tado_me.homes:
            raise NoHomes

        home = tado_me.homes[0]
        unique_id = str(home.id)
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=home.name,
            data={CONF_TOKEN_FILE: self._token_file},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors = {}
        tado = await self._async_get_tado()

        if user_input is not None:
            try:
                if tado.device_activation_status() != DeviceActivationStatus.COMPLETED:
                    await self.hass.async_add_executor_job(tado.device_activation)
                if tado.device_activation_status() == DeviceActivationStatus.COMPLETED:
                    return await self._async_finish_setup(tado)
                errors["base"] = "activation_failed"
            except TadoException as ex:
                _LOGGER.warning("Tado device activation failed: %s", ex)
                self._tado = None
                errors["base"] = "activation_failed"
            except requests.exceptions.RequestException as ex:
                _LOGGER.warning("Tado connection error: %s", ex)
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        if tado.device_activation_status() == DeviceActivationStatus.COMPLETED:
            return await self._async_finish_setup(tado)

        tado = await self._async_get_tado()
        verification_url = tado.device_verification_url() or ""
        data_schema = vol.Schema(
            {
                vol.Optional(
                    ATTR_VERIFICATION_URL, default=verification_url
                ): str,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={"url": verification_url},
        )

    async def async_step_homekit(
        self, discovery_info: zeroconf.ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle HomeKit discovery."""
        self._async_abort_entries_match()
        properties = {
            key.lower(): value for (key, value) in discovery_info.properties.items()
        }
        await self.async_set_unique_id(properties[zeroconf.ATTR_PROPERTIES_ID])
        self._abort_if_unique_id_configured()
        return await self.async_step_user()

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a reconfiguration flow initialized by the user."""
        errors: dict[str, str] = {}
        reconfigure_entry = self._get_reconfigure_entry()

        if self._token_file is None:
            self._token_file = reconfigure_entry.data.get(
                CONF_TOKEN_FILE
            ) or _new_token_file_path(self.hass)
            self._tado = None

        tado = await self._async_get_tado()

        if user_input is not None:
            try:
                if tado.device_activation_status() != DeviceActivationStatus.COMPLETED:
                    await self.hass.async_add_executor_job(tado.device_activation)
                if tado.device_activation_status() != DeviceActivationStatus.COMPLETED:
                    errors["base"] = "activation_failed"
                else:
                    return self.async_update_reload_and_abort(
                        reconfigure_entry,
                        data_updates={CONF_TOKEN_FILE: self._token_file},
                    )
            except TadoException:
                self._tado = None
                errors["base"] = "activation_failed"
            except requests.exceptions.RequestException:
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        tado = await self._async_get_tado()
        verification_url = tado.device_verification_url() or ""
        data_schema = vol.Schema(
            {
                vol.Optional(
                    ATTR_VERIFICATION_URL, default=verification_url
                ): str,
            }
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={"url": verification_url},
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> OptionsFlowHandler:
        """Get the options flow for this handler."""
        return OptionsFlowHandler()


class OptionsFlowHandler(OptionsFlow):
    """Handle an option flow for Tado."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle options flow."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        data_schema = vol.Schema(
            {
                vol.Optional(
                    CONF_FALLBACK,
                    default=self.config_entry.options.get(
                        CONF_FALLBACK, CONST_OVERLAY_TADO_DEFAULT
                    ),
                ): vol.In(CONST_OVERLAY_TADO_OPTIONS),
            }
        )
        return self.async_show_form(step_id="init", data_schema=data_schema)


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""


class NoHomes(HomeAssistantError):
    """Error to indicate the account has no homes."""
