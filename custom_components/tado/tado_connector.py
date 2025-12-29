"""Tado Connector a class to store the data as an object."""

from datetime import datetime, timedelta
import logging
from typing import Any

from PyTado.exceptions import TadoException
from PyTado.interface import Tado
from requests import RequestException

from homeassistant.components.climate import PRESET_AWAY, PRESET_HOME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import dispatcher_send
from homeassistant.util import Throttle

from .const import (
    INSIDE_TEMPERATURE_MEASUREMENT,
    PRESET_AUTO,
    SIGNAL_TADO_MOBILE_DEVICE_UPDATE_RECEIVED,
    SIGNAL_TADO_UPDATE_RECEIVED,
    TEMP_OFFSET,
    TYPE_HEATING,
)

MIN_TIME_BETWEEN_UPDATES = timedelta(minutes=4)
SCAN_INTERVAL = timedelta(minutes=5)
SCAN_MOBILE_DEVICE_INTERVAL = timedelta(seconds=30)


_LOGGER = logging.getLogger(__name__)


class TadoConnector:
    """An object to store the Tado data."""

    def __init__(self, hass: HomeAssistant, token_file: str | None, fallback: str) -> None:
        """Initialize Tado Connector."""
        self.hass = hass
        self._token_file = token_file
        self._fallback = fallback

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

    def setup(self):
        """Connect to Tado and fetch the zones."""
        self.tado = Tado(token_file_path=self._token_file)

        tado_me = self.tado.get_me()
        if not tado_me.homes:
            raise RuntimeError("No homes returned by Tado API")

        tado_home = tado_me.homes[0]
        self.home_id = tado_home.id
        self.home_name = tado_home.name
        self.is_x = tado_home.generation == "LINE_X"
        if tado_home.generation is None and hasattr(self.tado, "_http"):
            self.is_x = bool(self.tado._http.is_x_line)

        # Load zones and devices
        self._zones_by_id = {}
        self.zones = []
        for zone in self.tado.get_zones():
            zone_id = self._get_zone_id(zone)
            self._zones_by_id[zone_id] = zone
            self.zones.append(
                {
                    "id": zone_id,
                    "name": zone.name,
                    "type": self._get_zone_type(zone),
                    "devices": [self._normalize_device(device) for device in zone.devices],
                }
            )

        self.devices = [self._normalize_device(device) for device in self.tado.get_devices()]

    def _to_dict(self, data: Any) -> dict[str, Any]:
        if isinstance(data, dict):
            return data
        if hasattr(data, "to_dict"):
            return data.to_dict()
        if hasattr(data, "model_dump"):
            return data.model_dump(by_alias=True)
        return {}

    def _normalize_device(self, device: Any) -> dict[str, Any]:
        device_dict = self._to_dict(device)
        device_dict["is_x"] = self.is_x
        return device_dict

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

    def get_mobile_devices(self):
        """Return the Tado mobile devices."""
        return self.tado.get_mobile_devices()

    @Throttle(MIN_TIME_BETWEEN_UPDATES)
    def update(self):
        """Update the registered zones."""
        self.update_devices()
        self.update_mobile_devices()
        self.update_zones()
        self.update_home()

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
            _LOGGER.debug(
                "Dispatching update to %s mobile device: %s",
                self.home_id,
                mobile_device,
            )

        dispatcher_send(
            self.hass,
            SIGNAL_TADO_MOBILE_DEVICE_UPDATE_RECEIVED.format(self.home_id),
        )

    def update_devices(self):
        """Update the device data from Tado."""
        try:
            devices = self.tado.get_devices()
        except (RuntimeError, TadoException):
            _LOGGER.error("Unable to connect to Tado while updating devices")
            return

        if not devices:
            _LOGGER.debug("No linked devices found for home ID %s", self.home_id)
            return

        if isinstance(devices, dict) and devices.get("errors"):
            _LOGGER.error(
                "Error for home ID %s while updating devices: %s",
                self.home_id,
                devices["errors"],
            )
            return

        for device in devices:
            device_info = self._normalize_device(device)
            if self.is_x:
                device_id = device_info.get("serialNumber")
            else:
                device_id = device_info.get("shortSerialNo")
            if not device_id:
                _LOGGER.debug("Skipping device without id: %s", device_info)
                continue

            _LOGGER.debug("Updating device %s", device_id)

            if not self.is_x:
                try:
                    capabilities = device_info.get("characteristics", {}).get(
                        "capabilities", []
                    )
                    if INSIDE_TEMPERATURE_MEASUREMENT in capabilities:
                        temp_offset = self.tado.get_temp_offset(device_id)
                        device_info[TEMP_OFFSET] = self._to_dict(temp_offset)
                except (RuntimeError, TadoException):
                    _LOGGER.error(
                        "Unable to connect to Tado while updating device %s",
                        device_id,
                    )
                    return

            self.data["device"][device_id] = device_info

            _LOGGER.debug(
                "Dispatching update to %s device %s: %s",
                self.home_id,
                device_id,
                device_info,
            )
            dispatcher_send(
                self.hass,
                SIGNAL_TADO_UPDATE_RECEIVED.format(
                    self.home_id, "device", device_id
                ),
            )

    def update_zones(self):
        """Update the zone data from Tado."""
        for zone_id in list(self._zones_by_id):
            self.update_zone(zone_id)

    def update_zone(self, zone_id):
        """Update the internal data from Tado."""
        _LOGGER.debug("Updating zone %s", zone_id)
        zone = self._zones_by_id.get(zone_id)
        if zone is None:
            try:
                zone = self.tado.get_zone(zone_id)
            except (RuntimeError, TadoException):
                _LOGGER.error(
                    "Unable to connect to Tado while updating zone %s", zone_id
                )
                return
            self._zones_by_id[zone_id] = zone

        zone.update()
        self.data["zone"][zone_id] = zone

        _LOGGER.debug(
            "Dispatching update to %s zone %s: %s",
            self.home_id,
            zone_id,
            zone,
        )
        dispatcher_send(
            self.hass,
            SIGNAL_TADO_UPDATE_RECEIVED.format(self.home_id, "zone", zone_id),
        )

    def update_home(self):
        """Update the home data from Tado."""
        try:
            self.data["weather"] = self._to_dict(self.tado.get_weather())
            self.data["geofence"] = self._to_dict(self.tado.get_home_state())
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
        capabilities = self.tado.get_capabilities(zone_id)
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
        return self.tado.get_auto_geofencing_supported()

    def reset_zone_overlay(self, zone_id):
        """Reset the zone back to the default operation."""
        self.tado.reset_zone_overlay(zone_id)
        self.update_zone(zone_id)

    def set_presence(
        self,
        presence=PRESET_HOME,
    ):
        """Set the presence to home, away or auto."""
        if presence == PRESET_AWAY:
            self.tado.set_away()
        elif presence == PRESET_HOME:
            self.tado.set_home()
        elif presence == PRESET_AUTO:
            self.tado.set_auto()

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
            self.tado.set_zone_overlay(
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
            self.tado.set_zone_overlay(
                zone_id, overlay_mode, None, None, device_type, "OFF"
            )
        except RequestException as exc:
            _LOGGER.error("Could not set zone overlay: %s", exc)

        self.update_zone(zone_id)

    def set_temperature_offset(self, device_id, offset):
        """Set temperature offset of device."""
        if not device_id:
            _LOGGER.error("Missing device id for temperature offset")
            return
        try:
            self.tado.set_temp_offset(device_id, offset)
        except RequestException as exc:
            _LOGGER.error("Could not set temperature offset: %s", exc)
            return

        self.update_devices()

    def set_meter_reading(self, reading: int) -> dict[str, Any]:
        """Send meter reading to Tado."""
        reading_date = datetime.now().date()
        if self.tado is None:
            raise HomeAssistantError("Tado client is not initialized")

        try:
            response = self.tado.set_eiq_meter_readings(
                reading_date=reading_date, reading=reading
            )
            return self._to_dict(response)
        except RequestException as exc:
            raise HomeAssistantError("Could not set meter reading") from exc
