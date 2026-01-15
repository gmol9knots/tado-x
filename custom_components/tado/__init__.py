"""Support for the (unofficial) Tado API."""

from datetime import datetime, timedelta
import logging

import requests.exceptions
import re
from PyTado.exceptions import TadoException

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_listen_once,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_DEVICE_ID_OVERRIDE,
    CONF_DEVICE_ID_OVERRIDES,
    CONF_DEVICE_OFFSETS,
    CONF_DEVICE_TYPE_ID_OVERRIDES,
    CONF_DEVICE_TYPE_OFFSETS,
    CONF_DEVICE_ZONE_MAP,
    CONF_FALLBACK,
    CONF_HOME_WEATHER_REFRESH_INTERVAL_SECONDS,
    CONF_OFFSET_RECALC_INTERVAL_SECONDS,
    CONF_SCAN_INTERVAL,
    CONF_SCAN_INTERVAL_SECONDS,
    CONF_TEMP_OFFSET_REFRESH_INTERVAL_SECONDS,
    CONF_ZONE_SENSOR_MAP,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DEFAULT_TEMP_OFFSET_REFRESH_INTERVAL_SECONDS,
    DEFAULT_OFFSET_RECALC_INTERVAL_SECONDS,
    CONF_TEMPERATURE_OFFSET,
    CONF_TOKEN_FILE,
    CONST_OVERLAY_MANUAL,
    CONST_OVERLAY_TADO_DEFAULT,
    CONST_OVERLAY_TADO_MODE,
    CONST_OVERLAY_TADO_OPTIONS,
    DOMAIN,
    SIGNAL_ZONE_SENSOR_MAP_UPDATED,
)
from .services import setup_services
from .tado_connector import TadoConnector

_LOGGER = logging.getLogger(__name__)


PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.DEVICE_TRACKER,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.TEXT,
    Platform.WATER_HEATER,
]

SCAN_INTERVAL = timedelta(minutes=5)
SCAN_MOBILE_DEVICE_INTERVAL = timedelta(seconds=30)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def _parse_device_type_map(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    entries = re.split(r"[,\n;]+", raw)
    result: dict[str, str] = {}
    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            _LOGGER.warning("Invalid device type mapping entry: %s", entry)
            continue
        key, value = entry.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not value:
            _LOGGER.warning("Invalid device type mapping entry: %s", entry)
            continue
        result[key] = value
    return result


def _parse_device_type_offsets(raw: str | None) -> dict[str, float]:
    if not raw:
        return {}
    entries = re.split(r"[,\n;]+", raw)
    result: dict[str, float] = {}
    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            _LOGGER.warning("Invalid device type offset entry: %s", entry)
            continue
        key, value = entry.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not value:
            _LOGGER.warning("Invalid device type offset entry: %s", entry)
            continue
        try:
            result[key] = float(value)
        except ValueError:
            _LOGGER.warning("Invalid offset value for %s: %s", key, value)
    return result


def _normalize_zone_sensors(value) -> list[str]:
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


def _normalize_zone_sensor_map(zone_sensor_map: dict | None) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for key, value in (zone_sensor_map or {}).items():
        if not key:
            continue
        sensors = _normalize_zone_sensors(value)
        if sensors:
            result[str(key)] = sensors
    return result


@callback
def _migrate_zone_sensor_map_single(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, str]:
    zone_map = entry.options.get(CONF_ZONE_SENSOR_MAP, {})
    if not isinstance(zone_map, dict):
        return {}
    migrated: dict[str, str] = {}
    for zone_id, value in zone_map.items():
        sensors = _normalize_zone_sensors(value)
        if sensors:
            migrated[str(zone_id)] = sensors[0]
    if migrated != zone_map:
        options = dict(entry.options)
        options[CONF_ZONE_SENSOR_MAP] = migrated
        hass.config_entries.async_update_entry(entry, options=options)
        _LOGGER.info("Migrated zone sensor map to a single sensor per zone.")
    return migrated


@callback
def _async_remove_zone_sensor_switches(hass: HomeAssistant) -> None:
    registry = er.async_get(hass)
    removed = 0
    for registry_entry in list(registry.entities.values()):
        if registry_entry.domain != "switch" or registry_entry.platform != DOMAIN:
            continue
        unique_id = registry_entry.unique_id or ""
        if not unique_id.startswith("zone_temp_sensor_"):
            continue
        registry.async_remove(registry_entry.entity_id)
        removed += 1
    if removed:
        _LOGGER.info("Removed %s legacy zone sensor switch entities.", removed)


def _flatten_zone_sensor_map(zone_sensor_map: dict | None) -> list[str]:
    sensors: list[str] = []
    for value in (zone_sensor_map or {}).values():
        sensors.extend(_normalize_zone_sensors(value))
    return list(dict.fromkeys(sensors))


def _diff_zone_sensor_map(
    old_map: dict[str, list[str]] | None, new_map: dict[str, list[str]] | None
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    added: dict[str, list[str]] = {}
    removed: dict[str, list[str]] = {}
    old_map = old_map or {}
    new_map = new_map or {}
    for zone_id in set(old_map) | set(new_map):
        old_sensors = set(_normalize_zone_sensors(old_map.get(zone_id)))
        new_sensors = set(_normalize_zone_sensors(new_map.get(zone_id)))
        added_sensors = sorted(new_sensors - old_sensors)
        removed_sensors = sorted(old_sensors - new_sensors)
        if added_sensors:
            added[str(zone_id)] = added_sensors
        if removed_sensors:
            removed[str(zone_id)] = removed_sensors
    return added, removed


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up Tado."""

    setup_services(hass)
    return True


type TadoConfigEntry = ConfigEntry[TadoConnector]


def _register_zone_sensor_listeners(
    hass: HomeAssistant, entry: TadoConfigEntry, tado: TadoConnector, zone_sensor_map: dict
):
    zone_sensor_map = _normalize_zone_sensor_map(zone_sensor_map)
    sensor_entities = _flatten_zone_sensor_map(zone_sensor_map)
    if not sensor_entities:
        return None

    async def _sensor_state_listener(event):
        entity_id = event.data.get("entity_id")
        if not entity_id:
            return
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        if new_state is None:
            return
        if new_state.state in ("unknown", "unavailable"):
            return
        force = old_state is None or old_state.state in ("unknown", "unavailable")
        hass.async_add_executor_job(
            tado.auto_adjust_offsets_for_sensor, entity_id, force
        )

    return async_track_state_change_event(
        hass, sensor_entities, _sensor_state_listener
    )


def _register_update_timer(
    hass: HomeAssistant, tado: TadoConnector, scan_interval_seconds: int
):
    interval = timedelta(seconds=max(1, scan_interval_seconds))
    return async_track_time_interval(
        hass, lambda now: tado.update(include_mobile_devices=False), interval
    )


def _register_offset_recalc_timer(
    hass: HomeAssistant, tado: TadoConnector, recalc_interval_seconds: int
):
    interval = timedelta(seconds=max(1, recalc_interval_seconds))

    async def _handle_offset_recalc(_now: datetime) -> None:
        await hass.async_add_executor_job(tado.auto_adjust_offsets_all)

    return async_track_time_interval(
        hass,
        _handle_offset_recalc,
        interval,
    )


def _schedule_initial_offset_recalc(
    hass: HomeAssistant, tado: TadoConnector, zone_sensor_map: dict
) -> None:
    if not zone_sensor_map:
        return

    @callback
    def _run(_now: datetime | None = None) -> None:
        hass.async_add_executor_job(tado.auto_adjust_offsets_all)

    if hass.is_running:
        async_call_later(hass, 5, _run)
        return

    @callback
    def _handle_start(_event) -> None:
        async_call_later(hass, 5, _run)

    async_listen_once(hass, EVENT_HOMEASSISTANT_STARTED, _handle_start)


async def async_setup_entry(hass: HomeAssistant, entry: TadoConfigEntry) -> bool:
    """Set up Tado from a config entry."""

    _async_import_options_from_data_if_missing(hass, entry)
    migrated_zone_sensor_map = _migrate_zone_sensor_map_single(hass, entry)
    _async_remove_zone_sensor_switches(hass)

    token_file = entry.data.get(CONF_TOKEN_FILE)
    fallback = entry.options.get(CONF_FALLBACK, CONST_OVERLAY_TADO_DEFAULT)
    device_id_overrides = entry.options.get(CONF_DEVICE_ID_OVERRIDES, {})
    device_offsets = entry.options.get(CONF_DEVICE_OFFSETS, {})
    device_zone_map = entry.options.get(CONF_DEVICE_ZONE_MAP, {})
    zone_sensor_map = _normalize_zone_sensor_map(migrated_zone_sensor_map)
    scan_interval_seconds = entry.options.get(CONF_SCAN_INTERVAL_SECONDS)
    if scan_interval_seconds is None:
        scan_interval_minutes = entry.options.get(CONF_SCAN_INTERVAL)
        if scan_interval_minutes is None:
            scan_interval_seconds = DEFAULT_SCAN_INTERVAL_SECONDS
        else:
            scan_interval_seconds = int(scan_interval_minutes) * 60
    type_id_overrides_raw = entry.options.get(CONF_DEVICE_TYPE_ID_OVERRIDES)
    type_offsets_raw = entry.options.get(CONF_DEVICE_TYPE_OFFSETS)
    device_type_id_overrides = _parse_device_type_map(type_id_overrides_raw)
    device_type_offsets = _parse_device_type_offsets(type_offsets_raw)

    if not isinstance(device_id_overrides, dict):
        device_id_overrides = {}
    if not isinstance(device_offsets, dict):
        device_offsets = {}
    if not isinstance(device_zone_map, dict):
        device_zone_map = {}
    if not isinstance(zone_sensor_map, dict):
        zone_sensor_map = {}
    try:
        scan_interval_seconds = int(scan_interval_seconds)
    except (TypeError, ValueError):
        scan_interval_seconds = DEFAULT_SCAN_INTERVAL_SECONDS
    if scan_interval_seconds < 1:
        scan_interval_seconds = DEFAULT_SCAN_INTERVAL_SECONDS

    temp_offset_refresh_interval_seconds = entry.options.get(
        CONF_TEMP_OFFSET_REFRESH_INTERVAL_SECONDS,
        DEFAULT_TEMP_OFFSET_REFRESH_INTERVAL_SECONDS,
    )
    try:
        temp_offset_refresh_interval_seconds = int(
            temp_offset_refresh_interval_seconds
        )
    except (TypeError, ValueError):
        temp_offset_refresh_interval_seconds = DEFAULT_TEMP_OFFSET_REFRESH_INTERVAL_SECONDS
    if temp_offset_refresh_interval_seconds < 1:
        temp_offset_refresh_interval_seconds = DEFAULT_TEMP_OFFSET_REFRESH_INTERVAL_SECONDS

    offset_recalc_interval_seconds = entry.options.get(
        CONF_OFFSET_RECALC_INTERVAL_SECONDS,
        DEFAULT_OFFSET_RECALC_INTERVAL_SECONDS,
    )
    try:
        offset_recalc_interval_seconds = int(offset_recalc_interval_seconds)
    except (TypeError, ValueError):
        offset_recalc_interval_seconds = DEFAULT_OFFSET_RECALC_INTERVAL_SECONDS
    if offset_recalc_interval_seconds < 1:
        offset_recalc_interval_seconds = DEFAULT_OFFSET_RECALC_INTERVAL_SECONDS

    home_weather_refresh_interval_seconds = entry.options.get(
        CONF_HOME_WEATHER_REFRESH_INTERVAL_SECONDS
    )
    if home_weather_refresh_interval_seconds is None:
        home_weather_refresh_interval_seconds = scan_interval_seconds
    try:
        home_weather_refresh_interval_seconds = int(
            home_weather_refresh_interval_seconds
        )
    except (TypeError, ValueError):
        home_weather_refresh_interval_seconds = scan_interval_seconds
    if home_weather_refresh_interval_seconds < 1:
        home_weather_refresh_interval_seconds = scan_interval_seconds

    legacy_override = entry.options.get(CONF_DEVICE_ID_OVERRIDE)
    legacy_offset = entry.options.get(CONF_TEMPERATURE_OFFSET)
    if legacy_override and not device_type_id_overrides:
        device_type_id_overrides = {"*": str(legacy_override)}
        _LOGGER.warning(
            "Using legacy device_id_override for all device types; migrate to device_type_id_overrides."
        )
    if legacy_offset is not None and not device_type_offsets:
        try:
            device_type_offsets = {"*": float(legacy_offset)}
            _LOGGER.warning(
                "Using legacy temperature_offset for all device types; migrate to device_type_offsets."
            )
        except ValueError:
            _LOGGER.warning("Invalid legacy temperature_offset: %s", legacy_offset)

    if not token_file:
        _LOGGER.error("Missing token file for Tado config entry, please reconfigure")
        return False

    tadoconnector = TadoConnector(
        hass,
        token_file,
        fallback,
        scan_interval_seconds=scan_interval_seconds,
        temp_offset_refresh_interval_seconds=temp_offset_refresh_interval_seconds,
        home_weather_refresh_interval_seconds=home_weather_refresh_interval_seconds,
        device_id_overrides=device_id_overrides,
        device_offsets=device_offsets,
        device_type_id_overrides=device_type_id_overrides,
        device_type_offsets=device_type_offsets,
        device_zone_map=device_zone_map,
        zone_sensor_map=zone_sensor_map,
    )

    try:
        await hass.async_add_executor_job(tadoconnector.setup)
    except RuntimeError as exc:
        _LOGGER.error("Failed to setup tado: %s", exc)
        return False
    except TadoException as exc:
        _LOGGER.error("Failed to setup tado: %s", exc)
        return False
    except requests.exceptions.Timeout as ex:
        raise ConfigEntryNotReady from ex
    except requests.exceptions.HTTPError as ex:
        if ex.response.status_code > 400 and ex.response.status_code < 500:
            _LOGGER.error("Failed to login to tado: %s", ex)
            return False
        raise ConfigEntryNotReady from ex

    # Do first update
    await hass.async_add_executor_job(tadoconnector.update)

    # Poll for updates in the background
    update_unsub = _register_update_timer(hass, tadoconnector, scan_interval_seconds)
    entry.async_on_unload(update_unsub)
    offset_unsub = _register_offset_recalc_timer(
        hass, tadoconnector, offset_recalc_interval_seconds
    )
    entry.async_on_unload(offset_unsub)

    entry.async_on_unload(
        async_track_time_interval(
            hass,
            lambda now: tadoconnector.update_mobile_devices(),
            SCAN_MOBILE_DEVICE_INTERVAL,
        )
    )

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault(entry.entry_id, {})
    hass.data[DOMAIN][entry.entry_id]["zone_sensor_map"] = zone_sensor_map
    hass.data[DOMAIN][entry.entry_id]["scan_interval"] = scan_interval_seconds
    hass.data[DOMAIN][entry.entry_id]["update_unsub"] = update_unsub
    hass.data[DOMAIN][entry.entry_id]["offset_unsub"] = offset_unsub
    hass.data[DOMAIN][entry.entry_id][
        "offset_recalc_interval"
    ] = offset_recalc_interval_seconds
    sensor_unsub = _register_zone_sensor_listeners(
        hass, entry, tadoconnector, zone_sensor_map
    )
    if sensor_unsub:
        entry.async_on_unload(sensor_unsub)
        hass.data[DOMAIN][entry.entry_id]["sensor_unsub"] = sensor_unsub

    _schedule_initial_offset_recalc(hass, tadoconnector, zone_sensor_map)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    entry.runtime_data = tadoconnector

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


@callback
def _async_import_options_from_data_if_missing(hass: HomeAssistant, entry: ConfigEntry):
    options = dict(entry.options)
    if CONF_FALLBACK not in options:
        options[CONF_FALLBACK] = entry.data.get(
            CONF_FALLBACK, CONST_OVERLAY_TADO_DEFAULT
        )
        hass.config_entries.async_update_entry(entry, options=options)

    if (
        CONF_SCAN_INTERVAL_SECONDS not in options
        and CONF_SCAN_INTERVAL not in options
    ):
        options[CONF_SCAN_INTERVAL_SECONDS] = DEFAULT_SCAN_INTERVAL_SECONDS
        hass.config_entries.async_update_entry(entry, options=options)

    if options[CONF_FALLBACK] not in CONST_OVERLAY_TADO_OPTIONS:
        if options[CONF_FALLBACK]:
            options[CONF_FALLBACK] = CONST_OVERLAY_TADO_MODE
        else:
            options[CONF_FALLBACK] = CONST_OVERLAY_MANUAL
        hass.config_entries.async_update_entry(entry, options=options)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    tado = entry.runtime_data
    if entry.options.get(CONF_FALLBACK) != tado.fallback:
        await hass.config_entries.async_reload(entry.entry_id)
        return

    tado.update_runtime_options(entry.options)

    data = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
    scan_interval = entry.options.get(CONF_SCAN_INTERVAL_SECONDS)
    if scan_interval is None:
        scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_SECONDS // 60) * 60
    try:
        scan_interval = int(scan_interval)
    except (TypeError, ValueError):
        scan_interval = DEFAULT_SCAN_INTERVAL_SECONDS
    if scan_interval < 1:
        scan_interval = DEFAULT_SCAN_INTERVAL_SECONDS

    if scan_interval != data.get("scan_interval"):
        if data.get("update_unsub"):
            data["update_unsub"]()
            data["update_unsub"] = None
        update_unsub = _register_update_timer(hass, tado, scan_interval)
        entry.async_on_unload(update_unsub)
        data["scan_interval"] = scan_interval
        data["update_unsub"] = update_unsub

    offset_recalc_interval = entry.options.get(
        CONF_OFFSET_RECALC_INTERVAL_SECONDS,
        DEFAULT_OFFSET_RECALC_INTERVAL_SECONDS,
    )
    try:
        offset_recalc_interval = int(offset_recalc_interval)
    except (TypeError, ValueError):
        offset_recalc_interval = DEFAULT_OFFSET_RECALC_INTERVAL_SECONDS
    if offset_recalc_interval < 1:
        offset_recalc_interval = DEFAULT_OFFSET_RECALC_INTERVAL_SECONDS

    if offset_recalc_interval != data.get("offset_recalc_interval"):
        if data.get("offset_unsub"):
            data["offset_unsub"]()
            data["offset_unsub"] = None
        offset_unsub = _register_offset_recalc_timer(
            hass, tado, offset_recalc_interval
        )
        entry.async_on_unload(offset_unsub)
        data["offset_recalc_interval"] = offset_recalc_interval
        data["offset_unsub"] = offset_unsub

    zone_sensor_map = _normalize_zone_sensor_map(
        entry.options.get(CONF_ZONE_SENSOR_MAP, {})
    )
    previous_zone_sensor_map = data.get("zone_sensor_map", {})
    if zone_sensor_map != previous_zone_sensor_map:
        if data.get("sensor_unsub"):
            data["sensor_unsub"]()
            data["sensor_unsub"] = None
        data["zone_sensor_map"] = zone_sensor_map
        sensor_unsub = _register_zone_sensor_listeners(
            hass, entry, tado, zone_sensor_map
        )
        if sensor_unsub:
            entry.async_on_unload(sensor_unsub)
            data["sensor_unsub"] = sensor_unsub
        added, removed = _diff_zone_sensor_map(
            previous_zone_sensor_map, zone_sensor_map
        )
        zone_name_map = {
            str(zone.get("id")): zone.get("name")
            for zone in getattr(tado, "zones", [])
        }
        for zone_id, sensors in added.items():
            zone_name = zone_name_map.get(zone_id)
            zone_label = f"{zone_id} ({zone_name})" if zone_name else zone_id
            linked = zone_sensor_map.get(zone_id, [])
            _LOGGER.info(
                "Linked temperature sensor(s) %s to zone %s. Now linked: %s",
                ", ".join(sensors),
                zone_label,
                ", ".join(linked) if linked else "none",
            )
        for zone_id, sensors in removed.items():
            zone_name = zone_name_map.get(zone_id)
            zone_label = f"{zone_id} ({zone_name})" if zone_name else zone_id
            linked = zone_sensor_map.get(zone_id, [])
            _LOGGER.info(
                "Unlinked temperature sensor(s) %s from zone %s. Now linked: %s",
                ", ".join(sensors),
                zone_label,
                ", ".join(linked) if linked else "none",
            )
        if zone_sensor_map:
            recalc_interval = data.get(
                "offset_recalc_interval",
                DEFAULT_OFFSET_RECALC_INTERVAL_SECONDS,
            )
            _LOGGER.info(
                "Zone sensor listeners refreshed; auto offset recalculation timer active (every %s seconds).",
                recalc_interval,
            )
        dispatcher_send(
            hass,
            SIGNAL_ZONE_SENSOR_MAP_UPDATED.format(tado.home_id),
        )
        if any(added.values()):
            _LOGGER.info(
                "Triggering auto offset recalculation after linking sensors."
            )
            hass.async_add_executor_job(tado.auto_adjust_offsets_all)


async def async_unload_entry(hass: HomeAssistant, entry: TadoConfigEntry) -> bool:
    """Unload a config entry."""
    unload_platforms = list(PLATFORMS)
    if Platform.SWITCH not in unload_platforms:
        unload_platforms.append(Platform.SWITCH)
    return await hass.config_entries.async_unload_platforms(entry, unload_platforms)
