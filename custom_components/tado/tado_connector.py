"""Tado Connector a class to store the data as an object."""

from datetime import datetime, timedelta
from collections.abc import Callable
import logging
import re
from typing import Any

from PyTado.exceptions import TadoException
from PyTado.interface import Tado
from requests import RequestException

from homeassistant.components.climate import PRESET_AWAY, PRESET_HOME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import dispatcher_send
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DEVICE_ID_OVERRIDES,
    CONF_DEVICE_OFFSETS,
    CONF_DEVICE_ZONE_MAP,
    CONF_SCAN_INTERVAL,
    CONF_SCAN_INTERVAL_SECONDS,
    CONF_ZONE_SENSOR_MAP,
    CONST_MODE_COOL,
    CONST_MODE_FAN,
    CONST_MODE_HEAT,
    CONST_MODE_OFF,
    CONST_MODE_SMART_SCHEDULE,
    INSIDE_TEMPERATURE_MEASUREMENT,
    LINKABLE_DEVICE_PREFIXES,
    PRESET_AUTO,
    SIGNAL_TADO_API_CALLS_UPDATED,
    SIGNAL_TADO_MOBILE_DEVICE_UPDATE_RECEIVED,
    SIGNAL_TADO_UPDATE_RECEIVED,
    TEMP_OFFSET,
    TYPE_HEATING,
)

SCAN_INTERVAL = timedelta(minutes=5)
SCAN_MOBILE_DEVICE_INTERVAL = timedelta(seconds=30)


_LOGGER = logging.getLogger(__name__)


class TadoConnector:
    """An object to store the Tado data."""

    def __init__(
        self,
        hass: HomeAssistant,
        token_file: str | None,
        fallback: str,
        scan_interval_seconds: int | None = None,
        device_id_overrides: dict[str, str] | None = None,
        device_offsets: dict[str, float] | None = None,
        device_type_id_overrides: dict[str, str] | None = None,
        device_type_offsets: dict[str, float] | None = None,
        device_zone_map: dict[str, str] | None = None,
        zone_sensor_map: dict[str, Any] | None = None,
    ) -> None:
        """Initialize Tado Connector."""
        self.hass = hass
        self._token_file = token_file
        self._fallback = fallback
        self._device_id_overrides = {
            str(key): str(value)
            for key, value in (device_id_overrides or {}).items()
            if key and value
        }
        self._device_offsets = {
            str(key): float(value)
            for key, value in (device_offsets or {}).items()
            if key is not None and value is not None
        }
        self._device_offsets_current = dict(self._device_offsets)
        self._device_type_id_overrides = {
            str(key).upper(): str(value)
            for key, value in (device_type_id_overrides or {}).items()
            if key and value
        }
        self._device_type_offsets = {
            str(key).upper(): float(value)
            for key, value in (device_type_offsets or {}).items()
            if key is not None and value is not None
        }
        self._device_zone_map = {
            str(key): str(value)
            for key, value in (device_zone_map or {}).items()
            if key and value
        }
        self._zone_sensor_map = self._normalize_zone_sensor_map(zone_sensor_map)
        self._offsets_applied = False
        self._zone_last_no_change_log: dict[int, datetime] = {}
        self._zone_last_snapshot: dict[int, tuple[Any, ...]] = {}
        self._zone_last_snapshot_log: dict[int, datetime] = {}
        self._last_poll_summary: str | None = None
        self._scan_interval_seconds = (
            int(scan_interval_seconds) if scan_interval_seconds is not None else None
        )
        self._api_call_date = dt_util.now().date()
        self._api_call_count = 0

        self.home_id: int = 0
        self.home_name = None
        self.tado = None
        self.zones: list[dict[Any, Any]] = []
        self.devices: list[dict[Any, Any]] = []
        self._zones_by_id: dict[int, Any] = {}
        self.data: dict[str, dict] = {
            "device": {},
            "mobile_device": {},
            "weather": {},
            "geofence": {},
            "zone": {},
        }
        self.is_x = False

    @property
    def fallback(self):
        """Return fallback flag to Smart Schedule."""
        return self._fallback

    def _reset_api_call_counter_if_needed(self) -> bool:
        today = dt_util.now().date()
        if today != self._api_call_date:
            self._api_call_date = today
            self._api_call_count = 0
            _LOGGER.info("API call counter reset for %s", today)
            return True
        return False

    def _track_api_call(self, count: int = 1) -> None:
        self._reset_api_call_counter_if_needed()
        self._api_call_count += count
        if self.home_id:
            dispatcher_send(
                self.hass,
                SIGNAL_TADO_API_CALLS_UPDATED.format(self.home_id),
            )

    def _api_call(self, func: Callable[..., Any], *args, **kwargs) -> Any:
        self._track_api_call()
        return func(*args, **kwargs)

    def get_api_call_count(self) -> int:
        self._reset_api_call_counter_if_needed()
        return self._api_call_count

    def get_api_call_date(self):
        self._reset_api_call_counter_if_needed()
        return self._api_call_date
 
    def _get_device_type(self, device_info: dict[str, Any]) -> str | None:
        device_type = (
            device_info.get("type")
            or device_info.get("deviceType")
            or device_info.get("model")
            or device_info.get("productType")
        )
        if device_type is None:
            return None
        return str(device_type).upper()

    def get_device_type(self, device_info: dict[str, Any]) -> str | None:
        """Return device type for options UI."""
        return self._get_device_type(device_info)

    def get_device_key(self, device_info: dict[str, Any], index: int | None = None) -> str:
        """Return a stable device key for overrides."""
        existing_key = device_info.get("device_key")
        if isinstance(existing_key, str) and existing_key:
            return existing_key
        device_type = self._get_device_type(device_info) or "UNKNOWN"
        serial = (
            device_info.get("serialNumber")
            or device_info.get("serialNo")
            or device_info.get("shortSerialNo")
        )
        if serial:
            return f"{device_type}:{serial}"
        device_id = device_info.get("id") or device_info.get("deviceId")
        if device_id:
            return f"{device_type}:{device_id}"
        name = (
            device_info.get("name")
            or device_info.get("deviceName")
            or device_info.get("roomName")
        )
        if name:
            return f"{device_type}:{name}"
        if index is not None:
            return f"{device_type}:#{index + 1}"
        return device_type

    def get_device_id_override(
        self, device_info: dict[str, Any], device_key: str | None = None
    ) -> str | None:
        if device_key is None:
            device_key = self.get_device_key(device_info)
        if device_key in self._device_id_overrides:
            return self._device_id_overrides[device_key]
        legacy_key = None
        if ":" in device_key:
            legacy_key = device_key.split(":", 1)[1]
        if legacy_key and legacy_key in self._device_id_overrides:
            return self._device_id_overrides[legacy_key]
        device_type = self._get_device_type(device_info)
        if device_type and device_type in self._device_type_id_overrides:
            return self._device_type_id_overrides[device_type]
        return self._device_type_id_overrides.get("*")

    def get_device_offset(
        self, device_info: dict[str, Any], device_key: str | None = None
    ) -> float | None:
        if device_key is None:
            device_key = self.get_device_key(device_info)
        offset = self._device_offsets_current.get(device_key)
        if offset is None and ":" in device_key:
            offset = self._device_offsets_current.get(device_key.split(":", 1)[1])
        if offset is not None:
            return offset
        device_id = (
            device_info.get("serialNumber")
            or device_info.get("serialNo")
            or device_info.get("shortSerialNo")
            or device_info.get("id")
        )
        if device_id and device_id in self._device_offsets_current:
            return self._device_offsets_current.get(device_id)
        return None

    def _set_current_offset(self, device_key: str, offset: float) -> None:
        self._device_offsets_current[device_key] = offset
        if ":" in device_key:
            legacy_key = device_key.split(":", 1)[1]
            self._device_offsets_current[legacy_key] = offset

    def _lookup_device_key_for_id(self, device_id: str) -> str | None:
        for idx, device in enumerate(self.devices):
            candidate = (
                device.get("serialNumber")
                or device.get("serialNo")
                or device.get("shortSerialNo")
                or device.get("id")
            )
            if candidate and str(candidate) == str(device_id):
                return device.get("device_key") or self.get_device_key(device, idx)
        return None

    def update_runtime_options(self, options: dict[str, Any]) -> None:
        """Update runtime options without reloading the integration."""
        device_id_overrides = options.get(CONF_DEVICE_ID_OVERRIDES, {})
        device_offsets = options.get(CONF_DEVICE_OFFSETS, {})
        device_zone_map = options.get(CONF_DEVICE_ZONE_MAP, {})
        zone_sensor_map = options.get(CONF_ZONE_SENSOR_MAP, {})
        scan_interval = options.get(CONF_SCAN_INTERVAL_SECONDS)
        if scan_interval is None:
            scan_interval = options.get(CONF_SCAN_INTERVAL)
            if scan_interval is not None:
                scan_interval = int(scan_interval) * 60
        if scan_interval is not None:
            try:
                scan_interval = int(scan_interval)
            except (TypeError, ValueError):
                scan_interval = None
        if isinstance(device_id_overrides, dict):
            self._device_id_overrides = {
                str(key): str(value)
                for key, value in device_id_overrides.items()
                if key and value
            }
        if isinstance(device_offsets, dict):
            self._device_offsets = {
                str(key): float(value)
                for key, value in device_offsets.items()
                if key is not None and value is not None
            }
        if isinstance(device_zone_map, dict):
            self._device_zone_map = {
                str(key): str(value)
                for key, value in device_zone_map.items()
                if key and value
            }
        if isinstance(zone_sensor_map, dict):
            self._zone_sensor_map = self._normalize_zone_sensor_map(zone_sensor_map)
        if isinstance(scan_interval, int):
            self._scan_interval_seconds = scan_interval

    def setup(self):
        """Connect to Tado and fetch the zones."""
        try:
            self.tado = Tado(token_file_path=self._token_file)
        except KeyError as exc:
            _LOGGER.error(
                "Failed to initialize Tado client (missing %s). Token may be invalid; reauthorize the integration.",
                exc,
            )
            raise RuntimeError("Tado authentication failed") from exc

        tado_me = self._api_call(self.tado.get_me)
        home_id, home_name, generation = self._extract_home_info(tado_me)
        self.home_id = int(home_id)
        self.home_name = home_name
        if generation is not None:
            self.is_x = generation == "LINE_X"
        elif hasattr(self.tado, "http") and hasattr(self.tado.http, "isX"):
            self.is_x = bool(self.tado.http.isX)
        elif hasattr(self.tado, "_http"):
            self.is_x = bool(self.tado._http.is_x_line)

        # Load zones and devices
        self._zones_by_id = {}
        self.zones = []
        for zone in self._api_call(self.tado.get_zones):
            zone_id = self._get_zone_id(zone)
            zone_name = self._get_zone_name(zone) or str(zone_id)
            zone_devices = self._get_zone_devices(zone)
            self._zones_by_id[zone_id] = zone
            self.zones.append(
                {
                    "id": zone_id,
                    "name": zone_name,
                    "type": self._get_zone_type(zone),
                    "devices": [
                        self._normalize_device(device, idx)
                        for idx, device in enumerate(zone_devices)
                    ],
                }
            )

        self.devices = [
            self._normalize_device(device, idx)
            for idx, device in enumerate(self._api_call(self.tado.get_devices))
        ]
        self._apply_device_offsets()

    def _extract_home_info(self, tado_me: Any) -> tuple[int, str, str | None]:
        homes: Any = None
        if isinstance(tado_me, list):
            homes = tado_me
        else:
            homes = getattr(tado_me, "homes", None)
            if homes is None and isinstance(tado_me, dict):
                homes = tado_me.get("homes") or tado_me.get("home") or []
        if isinstance(homes, dict):
            homes = [homes]
        if not homes:
            raise RuntimeError("No homes returned by Tado API")
        home = homes[0]
        if isinstance(home, dict):
            home_id = home.get("id")
            home_name = home.get("name")
            generation = home.get("generation")
        else:
            home_id = getattr(home, "id", None)
            home_name = getattr(home, "name", None)
            generation = getattr(home, "generation", None)
        if home_id is None or home_name is None:
            raise RuntimeError("Invalid home data returned by Tado API")
        return int(home_id), str(home_name), generation

    def _get_nested(self, data: dict[str, Any], *keys: str) -> Any:
        current: Any = data
        for key in keys:
            if not isinstance(current, dict) or key not in current:
                return None
            current = current[key]
        return current

    def _as_datetime(self, value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            return dt_util.parse_datetime(value)
        return None

    def _get_setting(self, data: dict[str, Any]) -> dict[str, Any]:
        setting = data.get("setting")
        if isinstance(setting, dict):
            return setting
        return {}

    def _get_sensor_data(self, data: dict[str, Any]) -> dict[str, Any]:
        return data.get("sensorDataPoints", {}) if isinstance(data, dict) else {}

    def _get_activity_data(self, data: dict[str, Any]) -> dict[str, Any]:
        return data.get("activityDataPoints", {}) if isinstance(data, dict) else {}

    def _derive_hvac_mode(self, data: dict[str, Any]) -> str:
        setting = self._get_setting(data)
        power = setting.get("power")
        mode = setting.get("mode")
        zone_type = setting.get("type")
        overlay = data.get("overlay")
        if power == "OFF":
            return CONST_MODE_OFF
        if overlay is None:
            return CONST_MODE_SMART_SCHEDULE
        if mode:
            return mode
        if zone_type in ("HEATING", "HOT_WATER"):
            return CONST_MODE_HEAT
        if zone_type == "AIR_CONDITIONING":
            return CONST_MODE_COOL
        return CONST_MODE_HEAT

    def _derive_hvac_action(self, data: dict[str, Any]) -> str:
        setting = self._get_setting(data)
        power = setting.get("power")
        mode = setting.get("mode")
        if power == "OFF":
            return "OFF"
        activity = self._get_activity_data(data)
        heating_power = self._get_nested(activity, "heatingPower", "percentage")
        if isinstance(heating_power, (int, float)) and heating_power > 0:
            return "HEATING"
        ac_power = self._get_nested(activity, "acPower", "value")
        if ac_power == "ON":
            if mode == CONST_MODE_COOL:
                return "COOLING"
            if mode == "DRY":
                return "DRYING"
            if mode == CONST_MODE_FAN:
                return "FAN"
            return "COOLING"
        return "IDLE"

    def _adapt_zone_state(self, state: dict[str, Any], zone: dict[str, Any]) -> Any:
        class ZoneStateAdapter:
            def __init__(self, outer: "TadoConnector", zone_state: dict[str, Any], zone_info: dict[str, Any]) -> None:
                self._outer = outer
                self._state = zone_state
                self._zone = zone_info

            @property
            def current_temp(self) -> float | None:
                temp = self._outer._get_nested(
                    self._state, "sensorDataPoints", "insideTemperature", "celsius"
                )
                return temp

            @property
            def current_temp_timestamp(self) -> Any:
                return self._outer._get_nested(
                    self._state, "sensorDataPoints", "insideTemperature", "timestamp"
                )

            @property
            def current_humidity(self) -> float | None:
                return self._outer._get_nested(
                    self._state, "sensorDataPoints", "humidity", "percentage"
                )

            @property
            def current_humidity_timestamp(self) -> Any:
                return self._outer._get_nested(
                    self._state, "sensorDataPoints", "humidity", "timestamp"
                )

            @property
            def target_temp(self) -> float | None:
                return self._outer._get_nested(
                    self._state, "setting", "temperature", "celsius"
                )

            @property
            def current_hvac_mode(self) -> str:
                return self._outer._derive_hvac_mode(self._state)

            @property
            def current_hvac_action(self) -> str:
                return self._outer._derive_hvac_action(self._state)

            @property
            def current_fan_speed(self) -> Any:
                return self._outer._get_nested(self._state, "setting", "fanSpeed")

            @property
            def current_fan_level(self) -> Any:
                return self._outer._get_nested(self._state, "setting", "fanLevel")

            @property
            def current_swing_mode(self) -> Any:
                return self._outer._get_nested(self._state, "setting", "swing")

            @property
            def current_vertical_swing_mode(self) -> Any:
                return self._outer._get_nested(self._state, "setting", "verticalSwing")

            @property
            def current_horizontal_swing_mode(self) -> Any:
                return self._outer._get_nested(
                    self._state, "setting", "horizontalSwing"
                )

            @property
            def heating_power_percentage(self) -> float | None:
                return self._outer._get_nested(
                    self._state, "activityDataPoints", "heatingPower", "percentage"
                )

            @property
            def heating_power_timestamp(self) -> Any:
                return self._outer._get_nested(
                    self._state, "activityDataPoints", "heatingPower", "timestamp"
                )

            @property
            def ac_power(self) -> Any:
                return self._outer._get_nested(
                    self._state, "activityDataPoints", "acPower", "value"
                )

            @property
            def ac_power_timestamp(self) -> Any:
                return self._outer._get_nested(
                    self._state, "activityDataPoints", "acPower", "timestamp"
                )

            @property
            def tado_mode(self) -> Any:
                return self._state.get("tadoMode")

            @property
            def available(self) -> bool:
                link_state = self._outer._get_nested(self._state, "link", "state")
                if link_state is None:
                    link_state = self._outer._get_nested(
                        self._state, "connection", "state"
                    )
                if isinstance(link_state, bool):
                    return link_state
                return str(link_state) in ("ONLINE", "CONNECTED")

            @property
            def power(self) -> Any:
                return self._outer._get_nested(self._state, "setting", "power")

            @property
            def overlay_active(self) -> bool:
                return self._state.get("overlay") is not None

            @property
            def overlay_termination_type(self) -> Any:
                return self._outer._get_nested(
                    self._state, "overlay", "termination", "type"
                )

            @property
            def overlay_termination_expiry_seconds(self) -> Any:
                return self._outer._get_nested(
                    self._state, "overlay", "termination", "remainingTimeInSeconds"
                )

            @property
            def default_overlay_termination_type(self) -> Any:
                return self._outer._get_nested(
                    self._state, "overlay", "termination", "type"
                )

            @property
            def default_overlay_termination_duration(self) -> Any:
                return self._outer._get_nested(
                    self._state, "overlay", "termination", "remainingTimeInSeconds"
                )

            @property
            def open_window(self) -> bool:
                if self._state.get("openWindow") is not None:
                    return True
                return bool(self._state.get("openWindowDetected"))

            @property
            def open_window_expiry_seconds(self) -> Any:
                return self._outer._get_nested(
                    self._state, "openWindow", "remainingTimeInSeconds"
                )

            @property
            def preparation(self) -> Any:
                return self._state.get("preparation")

        return ZoneStateAdapter(self, state, zone)

    def _auto_adjust_offsets(self, zone_id: int, force: bool = False) -> None:
        if not self._device_zone_map or not self._zone_sensor_map:
            return
        zone_key = str(zone_id)
        sensor_ids = self._normalize_zone_sensors(self._zone_sensor_map.get(zone_key))
        if not sensor_ids:
            return
        sensor_temps: list[float] = []
        for sensor_id in sensor_ids:
            sensor_state = self.hass.states.get(sensor_id)
            if sensor_state is None:
                continue
            if sensor_state.state in ("unknown", "unavailable"):
                continue
            try:
                sensor_temps.append(float(sensor_state.state))
            except ValueError:
                _LOGGER.warning(
                    "Invalid sensor value for %s: %s", sensor_id, sensor_state.state
                )
        if not sensor_temps:
            _LOGGER.warning(
                "No valid sensor temperature for zone %s (sensors: %s)",
                zone_id,
                ", ".join(sensor_ids),
            )
            return
        sensor_temp = min(sensor_temps)

        zone_data = self.data["zone"].get(zone_id)
        if zone_data is None:
            return
        current_temp = getattr(zone_data, "current_temp", None)
        if current_temp is None:
            return
        current_temp = float(current_temp)
        now = datetime.now()
        current_offsets: list[float] = []
        for idx, device in enumerate(self.devices):
            device_key = device.get("device_key") or self.get_device_key(device, idx)
            device_type = self._get_device_type(device)
            if not device_type or not device_type.startswith(LINKABLE_DEVICE_PREFIXES):
                continue
            mapped_zone = self._device_zone_map.get(device_key)
            if mapped_zone is None and ":" in device_key:
                mapped_zone = self._device_zone_map.get(device_key.split(":", 1)[1])
            if mapped_zone is None or str(mapped_zone) != zone_key:
                continue
            offset_value = self.get_device_offset(device, device_key)
            if offset_value is not None:
                current_offsets.append(float(offset_value))

        avg_offset = sum(current_offsets) / len(current_offsets) if current_offsets else 0.0
        raw_temp = current_temp - avg_offset
        target_offset = sensor_temp - raw_temp
        target_offset = max(-10.0, min(10.0, target_offset))
        target_offset = round(target_offset, 1)
        applied = False
        updated_devices: list[str] = []
        for idx, device in enumerate(self.devices):
            device_key = device.get("device_key") or self.get_device_key(device, idx)
            device_type = self._get_device_type(device)
            if not device_type or not device_type.startswith(LINKABLE_DEVICE_PREFIXES):
                continue
            mapped_zone = self._device_zone_map.get(device_key)
            if mapped_zone is None and ":" in device_key:
                mapped_zone = self._device_zone_map.get(device_key.split(":", 1)[1])
            if mapped_zone is None or str(mapped_zone) != zone_key:
                continue
            device_id = (
                device.get("serialNumber")
                or device.get("serialNo")
                or device.get("shortSerialNo")
                or device.get("id")
                or self.get_device_id_override(device, device_key)
            )
            if not device_id:
                _LOGGER.warning(
                    "Missing device id for auto offset on %s", device_key
                )
                continue
            last_offset = self.get_device_offset(device, device_key)
            if last_offset is not None and abs(last_offset - target_offset) < 0.1:
                continue
            self.set_temperature_offset(
                device_id,
                target_offset,
                device_key=device_key,
                update_devices=False,
            )
            updated_devices.append(device_key)
            applied = True
        if applied:
            _LOGGER.debug(
                "Auto offset: zone %s sensor=%.2f zone=%.2f raw=%.2f avg_offset=%.2f target=%.2f devices=%s",
                zone_id,
                sensor_temp,
                current_temp,
                raw_temp,
                avg_offset,
                target_offset,
                ", ".join(updated_devices),
            )
        else:
            last_no_change = self._zone_last_no_change_log.get(zone_id)
            if last_no_change is None or now - last_no_change >= timedelta(minutes=5):
                _LOGGER.debug("Auto offset: no changes for zone %s", zone_id)
                self._zone_last_no_change_log[zone_id] = now

    def auto_adjust_offsets_for_sensor(self, sensor_id: str, force: bool = False) -> None:
        """Auto adjust offsets based on a sensor update."""
        if not self._zone_sensor_map:
            return
        for zone_id, mapped_sensor in self._zone_sensor_map.items():
            if sensor_id in self._normalize_zone_sensors(mapped_sensor):
                try:
                    self._auto_adjust_offsets(int(zone_id), force=force)
                except ValueError:
                    _LOGGER.warning("Invalid zone id in zone sensor map: %s", zone_id)

    def auto_adjust_offsets_all(self) -> None:
        """Auto adjust offsets for all mapped zones."""
        if not self._zone_sensor_map:
            return
        for zone_id in list(self._zone_sensor_map):
            try:
                self._auto_adjust_offsets(int(zone_id))
            except ValueError:
                _LOGGER.warning("Invalid zone id in zone sensor map: %s", zone_id)

    def _normalize_zone_sensors(self, value: Any) -> list[str]:
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

    def _normalize_zone_sensor_map(
        self, zone_sensor_map: dict[str, Any] | None
    ) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for key, value in (zone_sensor_map or {}).items():
            if not key:
                continue
            sensors = self._normalize_zone_sensors(value)
            if sensors:
                result[str(key)] = sensors
        return result

    def _to_dict(self, data: Any) -> dict[str, Any]:
        if isinstance(data, dict):
            return data
        if hasattr(data, "to_dict"):
            return data.to_dict()
        if hasattr(data, "model_dump"):
            return data.model_dump(by_alias=True)
        return {}

    def _normalize_device(self, device: Any, index: int | None = None) -> dict[str, Any]:
        device_dict = self._to_dict(device)
        device_dict["is_x"] = self.is_x
        device_key = self.get_device_key(device_dict, index)
        device_dict["device_key"] = device_key
        has_serial = any(
            device_dict.get(key)
            for key in ("serialNumber", "serialNo", "shortSerialNo")
        )
        if not has_serial:
            override = self.get_device_id_override(device_dict, device_key)
            if override:
                device_dict["serialNumber"] = override
                device_dict.setdefault("serialNo", override)
                device_dict.setdefault("shortSerialNo", override)
                device_dict.setdefault("id", override)
                _LOGGER.debug(
                    "Applied device id override for %s: %s",
                    device_key,
                    override,
                )
        return device_dict

    def _apply_device_offsets(self) -> None:
        if self._offsets_applied or not (
            self._device_offsets or self._device_type_offsets
        ):
            return
        for idx, device in enumerate(self.devices):
            device_type = self._get_device_type(device)
            device_key = device.get("device_key") or self.get_device_key(device, idx)
            offset = self._device_offsets.get(device_key)
            if offset is None and ":" in device_key:
                legacy_key = device_key.split(":", 1)[1]
                offset = self._device_offsets.get(legacy_key)
            if offset is None:
                if device_type and device_type in self._device_type_offsets:
                    offset = self._device_type_offsets[device_type]
                elif "*" in self._device_type_offsets:
                    offset = self._device_type_offsets["*"]
                else:
                    continue
            device_id = (
                device.get("serialNumber")
                or device.get("serialNo")
                or device.get("shortSerialNo")
                or device.get("id")
            )
            if not device_id:
                _LOGGER.warning(
                    "Missing device id for offset on device %s: %s",
                    device_key,
                    device,
                )
                continue
            try:
                self._api_call(self.tado.set_temp_offset, device_id, offset)
                self._set_current_offset(device_key, float(offset))
                _LOGGER.debug(
                    "Applied temperature offset %.2f to device %s (%s)",
                    offset,
                    device_id,
                    device_key,
                )
            except RequestException as exc:
                _LOGGER.error(
                    "Could not set temperature offset for device %s: %s",
                    device_id,
                    exc,
                )
        self._offsets_applied = True

    def _get_zone_id(self, zone: Any) -> int:
        if isinstance(zone, dict):
            return int(zone.get("id") or zone.get("roomId") or zone.get("room_id"))
        return int(
            getattr(zone, "_id", None)
            or getattr(zone, "id", None)
            or getattr(zone, "room_id", None)
        )

    def _get_zone_type(self, zone: Any) -> str:
        zone_type = None
        if isinstance(zone, dict):
            zone_type = zone.get("type")
        else:
            zone_type = getattr(zone, "zone_type", None)
        if zone_type is None:
            return TYPE_HEATING
        return str(zone_type)

    def _get_zone_name(self, zone: Any) -> str | None:
        if isinstance(zone, dict):
            return zone.get("name") or zone.get("roomName") or zone.get("room_name")
        return getattr(zone, "name", None) or getattr(zone, "room_name", None)

    def _get_zone_devices(self, zone: Any) -> list[Any]:
        if isinstance(zone, dict):
            return zone.get("devices", [])
        return getattr(zone, "devices", [])

    def get_mobile_devices(self):
        """Return the Tado mobile devices."""
        return self._api_call(self.tado.get_mobile_devices)

    def update(self):
        """Update the registered zones."""
        offset_calls = self.update_devices()
        self.update_mobile_devices()
        zone_info_calls, zone_state_calls = self.update_zones()
        self.update_home()
        self._log_poll_summary(
            offset_calls=offset_calls,
            zone_info_calls=zone_info_calls,
            zone_state_calls=zone_state_calls,
        )

    def update_mobile_devices(self) -> None:
        """Update the mobile devices."""
        try:
            mobile_devices_raw = self.get_mobile_devices()
        except (RuntimeError, TadoException):
            _LOGGER.error("Unable to connect to Tado while updating mobile devices")
            return

        if not mobile_devices_raw:
            _LOGGER.debug("No linked mobile devices found for home ID %s", self.home_id)
            return

        if isinstance(mobile_devices_raw, dict) and mobile_devices_raw.get("errors"):
            _LOGGER.error(
                "Error for home ID %s while updating mobile devices: %s",
                self.home_id,
                mobile_devices_raw["errors"],
            )
            return

        mobile_devices = [self._to_dict(device) for device in mobile_devices_raw]
        for mobile_device in mobile_devices:
            self.data["mobile_device"][mobile_device["id"]] = mobile_device

        dispatcher_send(
            self.hass,
            SIGNAL_TADO_MOBILE_DEVICE_UPDATE_RECEIVED.format(self.home_id),
        )

    def update_devices(self) -> int:
        """Update the device data from Tado."""
        offset_calls = 0
        try:
            devices = self._api_call(self.tado.get_devices)
        except (RuntimeError, TadoException):
            _LOGGER.error("Unable to connect to Tado while updating devices")
            return offset_calls

        if not devices:
            _LOGGER.debug("No linked devices found for home ID %s", self.home_id)
            return offset_calls

        if isinstance(devices, dict) and devices.get("errors"):
            _LOGGER.error(
                "Error for home ID %s while updating devices: %s",
                self.home_id,
                devices["errors"],
            )
            return offset_calls

        for idx, device in enumerate(devices):
            device_info = self._normalize_device(device, idx)
            if self.is_x:
                device_id = device_info.get("serialNumber")
            else:
                device_id = device_info.get("shortSerialNo")
            if not device_id:
                _LOGGER.debug("Skipping device without id: %s", device_info)
                continue

            if not self.is_x:
                try:
                    capabilities = device_info.get("characteristics", {}).get(
                        "capabilities", []
                    )
                    if INSIDE_TEMPERATURE_MEASUREMENT in capabilities:
                        temp_offset = self._api_call(
                            self.tado.get_temp_offset, device_id
                        )
                        device_info[TEMP_OFFSET] = self._to_dict(temp_offset)
                        offset_calls += 1
                except (RuntimeError, TadoException):
                    _LOGGER.error(
                        "Unable to connect to Tado while updating device %s",
                        device_id,
                    )
                    return offset_calls

            self.data["device"][device_id] = device_info
            dispatcher_send(
                self.hass,
                SIGNAL_TADO_UPDATE_RECEIVED.format(
                    self.home_id, "device", device_id
                ),
            )
        return offset_calls

    def update_zones(self) -> tuple[int, int]:
        """Update the zone data from Tado."""
        zone_info_calls = 0
        zone_state_calls = 0
        for zone_id in list(self._zones_by_id):
            info_calls, state_calls = self.update_zone(zone_id)
            zone_info_calls += info_calls
            zone_state_calls += state_calls
        return zone_info_calls, zone_state_calls

    def update_zone(self, zone_id) -> tuple[int, int]:
        """Update the internal data from Tado."""
        zone_info_calls = 0
        zone_state_calls = 0
        zone = self._zones_by_id.get(zone_id)
        if zone is None:
            try:
                zone = self._api_call(self.tado.get_zone, zone_id)
                zone_info_calls += 1
            except (RuntimeError, TadoException):
                _LOGGER.error(
                    "Unable to connect to Tado while updating zone %s", zone_id
                )
                return zone_info_calls, zone_state_calls
            self._zones_by_id[zone_id] = zone

        if isinstance(zone, dict):
            try:
                zone_state = self._api_call(self.tado.get_zone_state, zone_id)
                zone_state_calls += 1
            except (RuntimeError, TadoException):
                _LOGGER.error(
                    "Unable to connect to Tado while updating zone %s", zone_id
                )
                return zone_info_calls, zone_state_calls
            if isinstance(zone_state, dict):
                self.data["zone"][zone_id] = self._adapt_zone_state(
                    zone_state, zone
                )
            else:
                self.data["zone"][zone_id] = zone_state
        elif hasattr(zone, "update"):
            self._track_api_call()
            zone.update()
            self.data["zone"][zone_id] = zone
            zone_state_calls += 1
        else:
            self.data["zone"][zone_id] = zone

        self._log_zone_change(zone_id)
        dispatcher_send(
            self.hass,
            SIGNAL_TADO_UPDATE_RECEIVED.format(self.home_id, "zone", zone_id),
        )
        return zone_info_calls, zone_state_calls

    def _log_poll_summary(
        self,
        offset_calls: int,
        zone_info_calls: int,
        zone_state_calls: int,
    ) -> None:
        scan_interval = (
            f"{self._scan_interval_seconds}s"
            if self._scan_interval_seconds is not None
            else "unknown"
        )
        mobile_interval = int(SCAN_MOBILE_DEVICE_INTERVAL.total_seconds())
        total_requests = (
            1
            + 1
            + 2
            + zone_info_calls
            + zone_state_calls
            + offset_calls
        )
        api_calls_today = self.get_api_call_count()
        summary = (
            "Polling summary: scan_interval=%s mobile_interval=%ss "
            "zones=%s devices=%s mobile_devices=%s "
            "requests_per_poll=%s api_calls_today=%s (device=1 mobile=1 home=2 "
            "zone_info=%s zone_state=%s temp_offset=%s)"
            % (
                scan_interval,
                mobile_interval,
                len(self._zones_by_id),
                len(self.devices),
                len(self.data.get("mobile_device", {})),
                total_requests,
                api_calls_today,
                zone_info_calls,
                zone_state_calls,
                offset_calls,
            )
        )
        if summary != self._last_poll_summary:
            _LOGGER.info(summary)
            self._last_poll_summary = summary

    def _zone_snapshot(self, zone_data: Any) -> tuple[Any, ...]:
        return (
            getattr(zone_data, "current_temp", None),
            getattr(zone_data, "current_humidity", None),
            getattr(zone_data, "target_temp", None),
            getattr(zone_data, "current_hvac_mode", None),
            getattr(zone_data, "current_hvac_action", None),
            getattr(zone_data, "heating_power_percentage", None),
            getattr(zone_data, "open_window", None),
            getattr(zone_data, "overlay_active", None),
        )

    def _log_zone_change(self, zone_id: int) -> None:
        zone_data = self.data["zone"].get(zone_id)
        if zone_data is None:
            return
        snapshot = self._zone_snapshot(zone_data)
        previous = self._zone_last_snapshot.get(zone_id)
        now = dt_util.utcnow()
        if snapshot != previous:
            self._zone_last_snapshot[zone_id] = snapshot
            self._zone_last_snapshot_log[zone_id] = now
            _LOGGER.debug("Zone %s changed", zone_id)
            return
        last_log = self._zone_last_snapshot_log.get(zone_id)
        if last_log is None or now - last_log >= timedelta(minutes=5):
            _LOGGER.debug("Zone %s unchanged", zone_id)
            self._zone_last_snapshot_log[zone_id] = now

    def update_home(self):
        """Update the home data from Tado."""
        try:
            self.data["weather"] = self._to_dict(
                self._api_call(self.tado.get_weather)
            )
            self.data["geofence"] = self._to_dict(
                self._api_call(self.tado.get_home_state)
            )
            dispatcher_send(
                self.hass,
                SIGNAL_TADO_UPDATE_RECEIVED.format(self.home_id, "home", "data"),
            )
        except (RuntimeError, TadoException):
            _LOGGER.error(
                "Unable to connect to Tado while updating weather and geofence data"
            )
            return

    def get_capabilities(self, zone_id):
        """Return the capabilities of the devices."""
        if self.is_x:
            return {"type": TYPE_HEATING}
        capabilities = self._api_call(self.tado.get_capabilities, zone_id)
        if isinstance(capabilities, dict):
            return capabilities

        caps_dict: dict[str, Any] = {"type": str(capabilities.type)}
        if getattr(capabilities, "temperatures", None) is not None:
            caps_dict["temperatures"] = self._to_dict(capabilities.temperatures)

        mode_map = {
            "AUTO": getattr(capabilities, "auto", None),
            "HEAT": getattr(capabilities, "heat", None),
            "COOL": getattr(capabilities, "cool", None),
            "DRY": getattr(capabilities, "dry", None),
            "FAN": getattr(capabilities, "fan", None),
        }
        for mode, mode_caps in mode_map.items():
            if mode_caps is not None:
                caps_dict[mode] = self._to_dict(mode_caps)

        return caps_dict

    def get_auto_geofencing_supported(self):
        """Return whether the Tado Home supports auto geofencing."""
        return self._api_call(self.tado.get_auto_geofencing_supported)

    def reset_zone_overlay(self, zone_id):
        """Reset the zone back to the default operation."""
        self._api_call(self.tado.reset_zone_overlay, zone_id)
        self.update_zone(zone_id)

    def set_presence(
        self,
        presence=PRESET_HOME,
    ):
        """Set the presence to home, away or auto."""
        if presence == PRESET_AWAY:
            self._api_call(self.tado.set_away)
        elif presence == PRESET_HOME:
            self._api_call(self.tado.set_home)
        elif presence == PRESET_AUTO:
            self._api_call(self.tado.set_auto)

        # Update everything when changing modes
        self.update_zones()
        self.update_home()

    def set_zone_overlay(
        self,
        zone_id=None,
        overlay_mode=None,
        temperature=None,
        duration=None,
        device_type="HEATING",
        mode=None,
        fan_speed=None,
        swing=None,
        fan_level=None,
        vertical_swing=None,
        horizontal_swing=None,
    ):
        """Set a zone overlay."""
        _LOGGER.debug(
            (
                "Set overlay for zone %s: overlay_mode=%s, temp=%s, duration=%s,"
                " type=%s, mode=%s fan_speed=%s swing=%s fan_level=%s vertical_swing=%s horizontal_swing=%s"
            ),
            zone_id,
            overlay_mode,
            temperature,
            duration,
            device_type,
            mode,
            fan_speed,
            swing,
            fan_level,
            vertical_swing,
            horizontal_swing,
        )

        try:
            self._api_call(
                self.tado.set_zone_overlay,
                zone_id,
                overlay_mode,
                temperature,
                int(duration) if duration else None,
                device_type,
                "ON",
                mode,
                fan_speed=fan_speed,
                swing=swing,
                fan_level=fan_level,
                vertical_swing=vertical_swing,
                horizontal_swing=horizontal_swing,
            )

        except RequestException as exc:
            _LOGGER.error("Could not set zone overlay: %s", exc)

        self.update_zone(zone_id)

    def set_zone_off(self, zone_id, overlay_mode, device_type="HEATING"):
        """Set a zone to off."""
        try:
            self._api_call(
                self.tado.set_zone_overlay,
                zone_id,
                overlay_mode,
                None,
                None,
                device_type,
                "OFF",
            )
        except RequestException as exc:
            _LOGGER.error("Could not set zone overlay: %s", exc)

        self.update_zone(zone_id)

    def set_temperature_offset(
        self, device_id, offset, device_key: str | None = None, update_devices: bool = True
    ):
        """Set temperature offset of device."""
        if not device_id:
            _LOGGER.error("Missing device id for temperature offset")
            return
        try:
            self._api_call(self.tado.set_temp_offset, device_id, offset)
        except RequestException as exc:
            _LOGGER.error("Could not set temperature offset: %s", exc)
            return
        if device_key is None:
            device_key = self._lookup_device_key_for_id(device_id)
        if device_key:
            self._set_current_offset(device_key, float(offset))
        else:
            self._device_offsets_current[str(device_id)] = float(offset)

        dispatcher_send(
            self.hass,
            SIGNAL_TADO_UPDATE_RECEIVED.format(self.home_id, "device", device_id),
        )

        if update_devices:
            self.update_devices()

    def set_meter_reading(self, reading: int) -> dict[str, Any]:
        """Send meter reading to Tado."""
        reading_date = datetime.now().date()
        if self.tado is None:
            raise HomeAssistantError("Tado client is not initialized")

        try:
            response = self._api_call(
                self.tado.set_eiq_meter_readings,
                reading_date=reading_date,
                reading=reading,
            )
            return self._to_dict(response)
        except RequestException as exc:
            raise HomeAssistantError("Could not set meter reading") from exc
