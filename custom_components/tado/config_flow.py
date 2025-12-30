"""Config flow for Tado integration."""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from PyTado.exceptions import TadoException
from PyTado.http import DeviceActivationStatus
from PyTado.interface import Tado
import asyncio
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
    CONF_SCAN_INTERVAL,
    CONF_SCAN_INTERVAL_SECONDS,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    CONF_TOKEN_FILE,
    CONST_OVERLAY_TADO_DEFAULT,
    CONST_OVERLAY_TADO_OPTIONS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

ATTR_VERIFICATION_URL = "verification_url"


def _log_request_exception(
    context: str, ex: requests.exceptions.RequestException
) -> None:
    response = getattr(ex, "response", None)
    if response is None:
        _LOGGER.warning("%s failed: %s", context, ex)
        return
    status = getattr(response, "status_code", None)
    reason = getattr(response, "reason", None)
    body = getattr(response, "text", None)
    headers = getattr(response, "headers", None)
    _LOGGER.warning(
        "%s failed: %s (status=%s reason=%s) headers=%s body=%s",
        context,
        ex,
        status,
        reason,
        headers,
        body,
    )


def _log_tado_debug_dump(tado: Tado) -> None:
    """Log raw Tado responses for debugging."""
    try:
        response = tado.get_me()
        _LOGGER.error("Tado debug get_me response: %s", response)
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.error("Tado debug get_me failed: %s", ex)
    try:
        status = tado.device_activation_status()
        _LOGGER.error("Tado debug activation status: %s", status)
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.error("Tado debug activation status failed: %s", ex)
    try:
        url = tado.device_verification_url()
        _LOGGER.error("Tado debug verification URL: %s", url)
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.error("Tado debug verification URL failed: %s", ex)


async def _wait_for_activation(
    tado: Tado, timeout_seconds: int = 20, interval_seconds: int = 2
) -> bool:
    """Wait for device activation to complete."""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        try:
            status = tado.device_activation_status()
        except (TadoException, requests.exceptions.RequestException):
            return False
        if status == DeviceActivationStatus.COMPLETED:
            return True
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(interval_seconds)


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
            _LOGGER.debug(
                "Initializing Tado client with token file %s", self._token_file
            )
            last_error: Exception | None = None
            for attempt in range(1, 4):
                try:
                    self._tado = await self.hass.async_add_executor_job(
                        Tado, self._token_file
                    )
                    last_error = None
                    break
                except requests.exceptions.RequestException as ex:
                    last_error = ex
                    _log_request_exception(
                        f"Tado client initialization (attempt {attempt}/3)",
                        ex,
                    )
                except Exception as ex:  # pylint: disable=broad-except
                    last_error = ex
                    _LOGGER.exception(
                        "Unexpected error initializing Tado client (attempt %s/3)",
                        attempt,
                    )
                if attempt < 3:
                    await asyncio.sleep(1)
            if self._tado is None:
                raise CannotConnect from last_error
        try:
            status = self._tado.device_activation_status()
        except (TadoException, requests.exceptions.RequestException) as ex:
            if isinstance(ex, requests.exceptions.RequestException):
                _log_request_exception("Tado activation status check", ex)
            else:
                _LOGGER.warning("Tado activation status check failed: %s", ex)
            raise CannotConnect from ex
        _LOGGER.debug("Tado device activation status: %s", status)
        return self._tado

    async def _async_finish_setup(self, tado: Tado) -> ConfigFlowResult:
        _LOGGER.debug("Finishing Tado config flow setup")
        try:
            tado_me = await self.hass.async_add_executor_job(tado.get_me)
        except TadoException as ex:
            _LOGGER.exception("Tado get_me failed")
            raise InvalidAuth from ex
        except requests.exceptions.RequestException as ex:
            _log_request_exception("Tado get_me", ex)
            raise CannotConnect from ex
        _LOGGER.debug("Tado get_me response type: %s", type(tado_me))
        if isinstance(tado_me, dict):
            _LOGGER.debug("Tado get_me response keys: %s", list(tado_me.keys()))

        homes = getattr(tado_me, "homes", None)
        if homes is None and isinstance(tado_me, dict):
            homes = tado_me.get("homes", [])
        if not homes:
            _LOGGER.error("Tado get_me returned no homes")
            raise NoHomes

        home = homes[0]
        home_id = getattr(home, "id", None)
        home_name = getattr(home, "name", None)
        if isinstance(home, dict):
            home_id = home.get("id", home_id)
            home_name = home.get("name", home_name)
        if home_id is None or home_name is None:
            _LOGGER.error("Tado get_me returned invalid home data: %s", home)
            raise CannotConnect
        _LOGGER.debug(
            "Tado home selected: id=%s name=%s", home_id, home_name
        )
        unique_id = str(home_id)
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=home_name,
            data={CONF_TOKEN_FILE: self._token_file},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors = {}
        tado: Tado | None = None
        try:
            tado = await self._async_get_tado()
        except CannotConnect:
            errors["base"] = "cannot_connect"

        if tado is None:
            verification_url = ""
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

        if user_input is not None:
            _LOGGER.debug("User submitted Tado config flow form")
            try:
                if tado.device_activation_status() != DeviceActivationStatus.COMPLETED:
                    _LOGGER.debug("Starting device activation")
                    await self.hass.async_add_executor_job(tado.device_activation)
                _LOGGER.debug(
                    "Device activation status after submit: %s",
                    tado.device_activation_status(),
                )
                if tado.device_activation_status() == DeviceActivationStatus.COMPLETED:
                    return await self._async_finish_setup(tado)
                if await _wait_for_activation(tado):
                    return await self._async_finish_setup(tado)
                errors["base"] = "activation_pending"
            except TadoException as ex:
                _LOGGER.warning("Tado device activation failed: %s", ex)
                self._tado = None
                errors["base"] = "activation_pending"
            except KeyError as ex:
                _LOGGER.warning(
                    "Tado activation failed (missing data): %s", ex
                )
                if tado is not None:
                    _log_tado_debug_dump(tado)
                self._tado = None
                errors["base"] = "activation_failed"
            except requests.exceptions.RequestException as ex:
                _log_request_exception("Tado device activation", ex)
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception in config flow submit")
                errors["base"] = "unknown"

        if tado.device_activation_status() == DeviceActivationStatus.COMPLETED:
            return await self._async_finish_setup(tado)

        verification_url = tado.device_verification_url() or ""
        _LOGGER.debug("Device verification URL: %s", verification_url)
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

        if user_input is None:
            self._token_file = _new_token_file_path(self.hass)
            self._tado = None

        tado: Tado | None = None
        try:
            tado = await self._async_get_tado()
        except CannotConnect:
            errors["base"] = "cannot_connect"

        if user_input is not None:
            _LOGGER.debug("User submitted Tado reconfigure flow form")
            try:
                if tado is None:
                    errors["base"] = "cannot_connect"
                else:
                    if tado.device_activation_status() != DeviceActivationStatus.COMPLETED:
                        _LOGGER.debug("Starting device activation (reconfigure)")
                        await self.hass.async_add_executor_job(tado.device_activation)
                    _LOGGER.debug(
                        "Device activation status after reconfigure submit: %s",
                        tado.device_activation_status(),
                    )
                    if tado.device_activation_status() == DeviceActivationStatus.COMPLETED:
                        return self.async_update_reload_and_abort(
                            reconfigure_entry,
                            data_updates={CONF_TOKEN_FILE: self._token_file},
                        )
                    if await _wait_for_activation(tado):
                        return self.async_update_reload_and_abort(
                            reconfigure_entry,
                            data_updates={CONF_TOKEN_FILE: self._token_file},
                        )
                    errors["base"] = "activation_pending"
            except TadoException:
                _LOGGER.warning("Tado device activation failed during reconfigure")
                self._tado = None
                errors["base"] = "activation_pending"
            except KeyError as ex:
                _LOGGER.warning(
                    "Tado activation failed during reconfigure (missing data): %s",
                    ex,
                )
                if tado is not None:
                    _log_tado_debug_dump(tado)
                self._tado = None
                errors["base"] = "activation_failed"
            except requests.exceptions.RequestException as ex:
                _log_request_exception(
                    "Tado device activation during reconfigure",
                    ex,
                )
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception in reconfigure submit")
                errors["base"] = "unknown"

        if tado is None:
            verification_url = ""
        else:
            verification_url = tado.device_verification_url() or ""
        _LOGGER.debug("Device verification URL (reconfigure): %s", verification_url)
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
            options = dict(self.config_entry.options)
            options[CONF_FALLBACK] = user_input.get(
                CONF_FALLBACK,
                self.config_entry.options.get(
                    CONF_FALLBACK, CONST_OVERLAY_TADO_DEFAULT
                ),
            )
            if CONF_SCAN_INTERVAL_SECONDS in user_input:
                options[CONF_SCAN_INTERVAL_SECONDS] = user_input.get(
                    CONF_SCAN_INTERVAL_SECONDS
                )
            options.pop(CONF_SCAN_INTERVAL, None)
            return self.async_create_entry(data=options)

        data_schema: dict[Any, Any] = {
            vol.Optional(
                CONF_FALLBACK,
                default=self.config_entry.options.get(
                    CONF_FALLBACK, CONST_OVERLAY_TADO_DEFAULT
                ),
            ): vol.In(CONST_OVERLAY_TADO_OPTIONS),
            vol.Optional(
                CONF_SCAN_INTERVAL_SECONDS,
                default=self.config_entry.options.get(
                    CONF_SCAN_INTERVAL_SECONDS,
                    self.config_entry.options.get(
                        CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_SECONDS // 60
                    )
                    * 60,
                ),
            ): vol.All(vol.Coerce(int), vol.Range(min=1)),
        }

        data_schema = vol.Schema(data_schema)
        return self.async_show_form(step_id="init", data_schema=data_schema)


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""


class NoHomes(HomeAssistantError):
    """Error to indicate the account has no homes."""
