"""Switch entities for Tado integration."""

from __future__ import annotations

import re
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import UnitOfTemperature
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import TadoConfigEntry
from .const import CONF_ZONE_SENSOR_MAP, SIGNAL_ZONE_SENSOR_MAP_UPDATED
from .entity import TadoZoneEntity
from .tado_connector import TadoConnector

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
    """Set up switch entities for zone temperature sensors."""
    tado = entry.runtime_data
    entities: list[SwitchEntity] = []

    sensors = _get_temperature_sensors(hass)
    zone_map = entry.options.get(CONF_ZONE_SENSOR_MAP, {})
    linked = _flatten_zone_sensor_map(zone_map)
    all_sensors = sorted(set(sensors + linked))

    for zone in tado.zones:
        for sensor_id in all_sensors:
            entities.append(
                TadoZoneSensorSwitch(tado, entry, zone, sensor_id)
            )

    async_add_entities(entities)


def _get_temperature_sensors(hass) -> list[str]:
    registry = er.async_get(hass)
    entity_ids = {
        entry.entity_id
        for entry in registry.entities.values()
        if entry.domain == "sensor"
    }
    options: list[str] = []
    for entity_id in sorted(entity_ids):
        entry = registry.async_get(entity_id)
        state = hass.states.get(entity_id)
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


def _normalize_sensor_list(value: Any) -> list[str]:
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
    return result


def _flatten_zone_sensor_map(zone_sensor_map: dict | None) -> list[str]:
    sensors: list[str] = []
    for value in (zone_sensor_map or {}).values():
        sensors.extend(_normalize_sensor_list(value))
    return list(dict.fromkeys(sensors))


def _sensor_label(hass, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    if state is not None:
        name = state.attributes.get("friendly_name")
        if name:
            return str(name)
    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)
    if entry is not None:
        if entry.name:
            return entry.name
        if entry.original_name:
            return entry.original_name
    return entity_id


class TadoZoneSensorSwitch(TadoZoneEntity, SwitchEntity):
    """Switch entity to link a temperature sensor to a zone."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        tado: TadoConnector,
        entry: TadoConfigEntry,
        zone: dict,
        sensor_entity_id: str,
    ) -> None:
        """Initialize the zone sensor switch."""
        self._tado = tado
        self._entry = entry
        self._zone_id = zone["id"]
        self._zone_name = zone["name"]
        self._sensor_entity_id = sensor_entity_id
        self._sensor_label = _sensor_label(tado.hass, sensor_entity_id)
        super().__init__(self._zone_name, tado.home_id, self._zone_id)

        sensor_slug = sensor_entity_id.replace(".", "_")
        self._attr_unique_id = (
            f"zone_temp_sensor_{self._zone_id}_{sensor_slug}_{tado.home_id}"
        )
        self._attr_name = f"Use {self._sensor_label}"

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
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        zone_map = self._entry.options.get(CONF_ZONE_SENSOR_MAP, {})
        sensors = _normalize_sensor_list(zone_map.get(str(self._zone_id)))
        return self._sensor_entity_id in sensors

    async def async_turn_on(self, **kwargs) -> None:
        """Enable this temperature sensor for the zone."""
        options = dict(self._entry.options)
        zone_map = dict(options.get(CONF_ZONE_SENSOR_MAP, {}))
        sensors = _normalize_sensor_list(zone_map.get(str(self._zone_id)))
        if self._sensor_entity_id not in sensors:
            sensors.append(self._sensor_entity_id)
        zone_map[str(self._zone_id)] = sensors
        options[CONF_ZONE_SENSOR_MAP] = zone_map
        self.hass.config_entries.async_update_entry(self._entry, options=options)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        """Disable this temperature sensor for the zone."""
        options = dict(self._entry.options)
        zone_map = dict(options.get(CONF_ZONE_SENSOR_MAP, {}))
        sensors = _normalize_sensor_list(zone_map.get(str(self._zone_id)))
        if self._sensor_entity_id in sensors:
            sensors.remove(self._sensor_entity_id)
        if sensors:
            zone_map[str(self._zone_id)] = sensors
        else:
            zone_map.pop(str(self._zone_id), None)
        options[CONF_ZONE_SENSOR_MAP] = zone_map
        self.hass.config_entries.async_update_entry(self._entry, options=options)
        self.async_write_ha_state()
