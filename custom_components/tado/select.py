"""Select entities for Tado integration."""

from __future__ import annotations

import re
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.const import UnitOfTemperature
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import TadoConfigEntry
from .const import (
    CONF_DEVICE_ZONE_MAP,
    CONF_ZONE_DEVICE_MAP,
    CONF_ZONE_SENSOR_MAP,
    LINKABLE_DEVICE_PREFIXES,
    SIGNAL_ZONE_SENSOR_MAP_UPDATED,
)
from .entity import TadoDeviceEntity, TadoZoneEntity
from .tado_connector import TadoConnector

UNASSIGNED_OPTION = "Unassigned"
_TEMP_UNITS = {
    UnitOfTemperature.CELSIUS,
    UnitOfTemperature.FAHRENHEIT,
    "C",
    "F",
    "°C",
    "°F",
    "°c",
    "°f",
}


async def async_setup_entry(
    hass, entry: TadoConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up select entities for VA04/RU04 devices and zones."""
    tado = entry.runtime_data
    entities: list[SelectEntity] = []

    for idx, device in enumerate(tado.devices):
        device_type = tado.get_device_type(device)
        if not device_type or not device_type.startswith(LINKABLE_DEVICE_PREFIXES):
            continue
        device_key = device.get("device_key") or tado.get_device_key(device, idx)
        entities.append(TadoVa04ZoneSelect(tado, entry, device, device_key))

    for zone in tado.zones:
        entities.append(TadoZoneTempSensorSelect(tado, entry, zone))

    async_add_entities(entities)


class TadoVa04ZoneSelect(TadoDeviceEntity, SelectEntity):
    """Select entity to map VA04/RU04 devices to zones."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_name = "Room"

    def __init__(
        self,
        tado: TadoConnector,
        entry: TadoConfigEntry,
        device_info: dict,
        device_key: str,
    ) -> None:
        """Initialize the VA04/RU04 device zone select."""
        self._tado = tado
        self._entry = entry
        self._device_info = device_info
        self._device_key = device_key
        super().__init__(device_info)

        self._option_map = self._build_option_map()
        self._attr_options = list(self._option_map.keys())
        self._attr_unique_id = f"va04_room_{device_key}_{tado.home_id}"
        self._attr_current_option = self._get_current_option()

    def _build_option_map(self) -> dict[str, str]:
        options = {UNASSIGNED_OPTION: ""}
        for zone in self._tado.zones:
            zone_name = zone["name"]
            zone_id = str(zone["id"])
            label = zone_name
            if label in options:
                label = f"{zone_name} ({zone_id})"
            options[label] = zone_id
        return options

    def _get_current_option(self) -> str:
        zone_map = self._entry.options.get(
            CONF_DEVICE_ZONE_MAP,
            self._entry.options.get(CONF_ZONE_DEVICE_MAP, {}),
        )
        if isinstance(zone_map, dict):
            mapped = zone_map.get(self._device_key)
            if mapped is not None:
                mapped_str = str(mapped)
                for label, zone_id in self._option_map.items():
                    if zone_id == mapped_str:
                        return label
        return UNASSIGNED_OPTION

    async def async_select_option(self, option: str) -> None:
        """Handle option selection."""
        options = dict(self._entry.options)
        zone_map = dict(
            options.get(
                CONF_DEVICE_ZONE_MAP,
                options.get(CONF_ZONE_DEVICE_MAP, {}),
            )
        )
        if option == UNASSIGNED_OPTION:
            zone_map.pop(self._device_key, None)
        else:
            zone_id = self._option_map.get(option)
            if zone_id:
                zone_map[self._device_key] = zone_id
        options[CONF_DEVICE_ZONE_MAP] = zone_map
        self.hass.config_entries.async_update_entry(self._entry, options=options)
        self._attr_current_option = option
        if self.hass:
            self.hass.loop.call_soon_threadsafe(self.async_write_ha_state)


class TadoZoneTempSensorSelect(TadoZoneEntity, SelectEntity):
    """Select entity to map a temperature sensor to a zone."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_name = "Reference Temperature Sensors"

    def __init__(
        self,
        tado: TadoConnector,
        entry: TadoConfigEntry,
        zone: dict,
    ) -> None:
        """Initialize the zone temperature sensor select."""
        self._tado = tado
        self._entry = entry
        self._zone_id = zone["id"]
        self._zone_name = zone["name"]
        super().__init__(self._zone_name, tado.home_id, self._zone_id)

        self._linked_sensors = self._get_linked_sensors()
        self._attr_unique_id = f"zone_temp_sensor_{self._zone_id}_{tado.home_id}"
        self._attr_options = self._build_options()
        self._attr_current_option = self._get_current_option()
        self._attr_extra_state_attributes = {
            "linked_sensors": self._linked_sensors
        }

    async def async_added_to_hass(self) -> None:
        """Register for zone sensor map updates."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_ZONE_SENSOR_MAP_UPDATED.format(self._tado.home_id),
                self._handle_zone_sensor_map_update,
            )
        )

    def _handle_zone_sensor_map_update(self) -> None:
        linked = self._get_linked_sensors()
        self._set_linked_sensors(linked)
        self.async_write_ha_state()

    def _get_sensor_options(self) -> list[str]:
        options: list[str] = []
        registry = er.async_get(self._tado.hass)
        entity_ids = {
            entry.entity_id
            for entry in registry.entities.values()
            if entry.domain == "sensor"
        }
        for entity_id in sorted(entity_ids):
            entry = registry.async_get(entity_id)
            state = self._tado.hass.states.get(entity_id)
            device_class = None
            unit = None
            if entry is not None:
                device_class = entry.device_class
            if state is not None:
                device_class = state.attributes.get("device_class", device_class)
                unit = state.attributes.get("unit_of_measurement")
            if device_class == "temperature":
                options.append(entity_id)
                continue
            if unit and unit in _TEMP_UNITS:
                options.append(entity_id)
                continue
            if "temperature" in entity_id:
                options.append(entity_id)
        return sorted(set(options))

    def _build_options(self) -> list[str]:
        options = [UNASSIGNED_OPTION]
        sensor_options = self._get_sensor_options()
        options.extend(sensor_options)
        for sensor_id in self._linked_sensors:
            if sensor_id not in options:
                options.append(sensor_id)
        return options

    def _normalize_sensor_list(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            values = list(value)
        elif isinstance(value, str):
            if not value:
                return []
            values = re.split(r"[,\n;]+", value)
        else:
            values = [value]
        result: list[str] = []
        for item in values:
            if item is None:
                continue
            item_str = str(item).strip()
            if not item_str or item_str in result:
                continue
            result.append(item_str)
        if not result:
            return []
        # Keep only the most recently linked sensor; multi-sensor linking is disabled.
        return [result[-1]]

    def _get_linked_sensors(self) -> list[str]:
        zone_map = self._entry.options.get(CONF_ZONE_SENSOR_MAP, {})
        if not isinstance(zone_map, dict):
            return []
        return self._normalize_sensor_list(zone_map.get(str(self._zone_id)))

    def _get_current_option(self) -> str:
        if not self._linked_sensors:
            return UNASSIGNED_OPTION
        return self._linked_sensors[0]

    def _set_linked_sensors(self, sensors: list[str]) -> None:
        self._linked_sensors = sensors
        self._attr_options = self._build_options()
        self._attr_current_option = self._get_current_option()
        self._attr_extra_state_attributes = {
            "linked_sensors": self._linked_sensors
        }

    async def async_select_option(self, option: str) -> None:
        """Handle option selection."""
        options = dict(self._entry.options)
        zone_map = dict(options.get(CONF_ZONE_SENSOR_MAP, {}))
        if option == UNASSIGNED_OPTION:
            zone_map.pop(str(self._zone_id), None)
            linked = []
        else:
            zone_map[str(self._zone_id)] = option
            linked = [option]
        options[CONF_ZONE_SENSOR_MAP] = zone_map
        self.hass.config_entries.async_update_entry(self._entry, options=options)
        self._set_linked_sensors(linked)
        if self.hass:
            self.hass.loop.call_soon_threadsafe(self.async_write_ha_state)
