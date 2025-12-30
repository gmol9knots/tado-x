"""Text entities for Tado integration."""

from __future__ import annotations

import logging

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import TadoConfigEntry
from .const import CONF_DEVICE_ID_OVERRIDES, LINKABLE_DEVICE_PREFIXES
from .entity import TadoDeviceEntity
from .tado_connector import TadoConnector

_LOGGER = logging.getLogger(__name__)

MAX_ID_LENGTH = 64


async def async_setup_entry(
    hass, entry: TadoConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up text entities for Tado devices."""
    tado = entry.runtime_data
    entities: list[TadoDeviceIdOverrideText] = []

    for idx, device in enumerate(tado.devices):
        device_type = tado.get_device_type(device)
        if not device_type or not device_type.startswith(LINKABLE_DEVICE_PREFIXES):
            continue
        device_key = device.get("device_key") or tado.get_device_key(device, idx)
        entities.append(TadoDeviceIdOverrideText(tado, entry, device, device_key))

    async_add_entities(entities)


class TadoDeviceIdOverrideText(TadoDeviceEntity, TextEntity):
    """Text entity to set a device id override."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = TextMode.TEXT
    _attr_max = MAX_ID_LENGTH
    _attr_name = "Device ID Override"

    def __init__(
        self,
        tado: TadoConnector,
        entry: TadoConfigEntry,
        device_info: dict,
        device_key: str,
    ) -> None:
        """Initialize the device id override text entity."""
        self._tado = tado
        self._entry = entry
        self._device_info = device_info
        self._device_key = device_key
        super().__init__(device_info)

        overrides = entry.options.get(CONF_DEVICE_ID_OVERRIDES, {})
        value = ""
        if isinstance(overrides, dict):
            value = overrides.get(device_key, "")
            if not value and ":" in device_key:
                value = overrides.get(device_key.split(":", 1)[1], "")
        self._attr_native_value = value
        self._attr_unique_id = f"device_id_override_{device_key}_{tado.home_id}"

    async def async_set_value(self, value: str) -> None:
        """Set a new device id override value."""
        normalized = value.strip() if value else ""
        options = dict(self._entry.options)
        overrides = dict(options.get(CONF_DEVICE_ID_OVERRIDES, {}))

        if normalized:
            overrides[self._device_key] = normalized
        else:
            overrides.pop(self._device_key, None)

        options[CONF_DEVICE_ID_OVERRIDES] = overrides
        self.hass.config_entries.async_update_entry(self._entry, options=options)
        self._attr_native_value = normalized
        if self.hass:
            self.hass.loop.call_soon_threadsafe(self.async_write_ha_state)
