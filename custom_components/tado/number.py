"""Number entities for Tado integration."""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import UnitOfTemperature
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import TadoConfigEntry
from .const import (
    CONF_DEVICE_OFFSETS,
    LINKABLE_DEVICE_PREFIXES,
    SIGNAL_TADO_UPDATE_RECEIVED,
)
from .entity import TadoDeviceEntity
from .tado_connector import TadoConnector

_LOGGER = logging.getLogger(__name__)

MIN_OFFSET = -10.0
MAX_OFFSET = 10.0
OFFSET_STEP = 0.1


async def async_setup_entry(
    hass, entry: TadoConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up number entities for Tado devices."""
    tado = entry.runtime_data
    entities: list[TadoDeviceOffsetNumber] = []

    for idx, device in enumerate(tado.devices):
        device_type = tado.get_device_type(device)
        if not device_type or not device_type.startswith(LINKABLE_DEVICE_PREFIXES):
            continue
        device_key = device.get("device_key") or tado.get_device_key(device, idx)
        entities.append(TadoDeviceOffsetNumber(tado, entry, device, device_key))

    async_add_entities(entities)


class TadoDeviceOffsetNumber(TadoDeviceEntity, NumberEntity):
    """Number entity to set the device temperature offset."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = MIN_OFFSET
    _attr_native_max_value = MAX_OFFSET
    _attr_native_step = OFFSET_STEP
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_name = "Device Offset"

    def __init__(
        self,
        tado: TadoConnector,
        entry: TadoConfigEntry,
        device_info: dict,
        device_key: str,
    ) -> None:
        """Initialize the device offset number."""
        self._tado = tado
        self._entry = entry
        self._device_info = device_info
        self._device_key = device_key
        super().__init__(device_info)

        self._attr_native_value = self._tado.get_device_offset(
            device_info, device_key
        )
        self._attr_unique_id = f"device_offset_{device_key}_{tado.home_id}"

    async def async_added_to_hass(self) -> None:
        """Register for updates."""
        device_id = self._get_device_id()
        if device_id:
            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass,
                    SIGNAL_TADO_UPDATE_RECEIVED.format(
                        self._tado.home_id, "device", device_id
                    ),
                    self._async_update_callback,
                )
            )
        self._async_update_value()

    def _async_update_callback(self) -> None:
        """Update and write state."""
        self._async_update_value()
        if self.hass:
            self.hass.loop.call_soon_threadsafe(self.async_write_ha_state)

    def _async_update_value(self) -> None:
        self._attr_native_value = self._tado.get_device_offset(
            self._device_info, self._device_key
        )

    def _get_device_id(self) -> str | None:
        return (
            self._device_info.get("serialNumber")
            or self._device_info.get("serialNo")
            or self._device_info.get("shortSerialNo")
            or self._device_info.get("id")
            or self._tado.get_device_id_override(self._device_info, self._device_key)
        )

    async def async_set_native_value(self, value: float) -> None:
        """Set a new offset value."""
        device_id = self._get_device_id()
        if not device_id:
            _LOGGER.error(
                "Cannot set device offset without device id for %s",
                self._device_key,
            )
        else:
            await self.hass.async_add_executor_job(
                self._tado.set_temperature_offset,
                device_id,
                value,
                self._device_key,
                False,
            )

        options = dict(self._entry.options)
        offsets = dict(options.get(CONF_DEVICE_OFFSETS, {}))
        offsets[self._device_key] = value
        options[CONF_DEVICE_OFFSETS] = offsets
        self.hass.config_entries.async_update_entry(self._entry, options=options)
        self._attr_native_value = value
        if self.hass:
            self.hass.loop.call_soon_threadsafe(self.async_write_ha_state)
