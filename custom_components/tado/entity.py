"""Base class for Tado entity."""

import logging

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from . import TadoConnector
from .const import DEFAULT_NAME, DOMAIN, TADO_HOME, TADO_ZONE


class TadoDeviceEntity(Entity):
    """Base implementation for Tado device."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    _LOGGER = logging.getLogger(__name__)

    def __init__(self, device_info: dict[str, str]) -> None:
        """Initialize a Tado device."""
        super().__init__()
        self._device_info = device_info
        is_x = bool(device_info.get("is_x"))
        if is_x:
            serial = (
                device_info.get("serialNumber")
                or device_info.get("serialNo")
                or device_info.get("shortSerialNo")
                or device_info.get("id")
                or device_info.get("device_key")
            )
            if serial is None:
                serial = "unknown"
                self._LOGGER.warning(
                    "Missing serial number in Tado X device info: %s", device_info
                )
            self.device_name = str(serial)
            self.device_id = str(serial)
            self._attr_device_info = DeviceInfo(
                configuration_url=f"https://app.tado.com/en/main/settings/home/rooms-and-devices/device/{self.device_name}",
                identifiers={(DOMAIN, self.device_id)},
                name=self.device_name,
                manufacturer=DEFAULT_NAME,
                sw_version=device_info.get("firmwareVersion")
                or device_info.get("currentFwVersion"),
                model=device_info.get("type") or device_info.get("deviceType"),
            )
        else:
            serial_no = (
                device_info.get("serialNo")
                or device_info.get("serialNumber")
                or device_info.get("device_key")
            )
            short_serial = (
                device_info.get("shortSerialNo")
                or device_info.get("id")
                or device_info.get("device_key")
            )
            if serial_no is None or short_serial is None:
                self._LOGGER.warning(
                    "Missing serial number in Tado device info: %s", device_info
                )
            self.device_name = str(serial_no or short_serial or "unknown")
            self.device_id = str(short_serial or serial_no or "unknown")
            self._attr_device_info = DeviceInfo(
                configuration_url=f"https://app.tado.com/en/main/settings/rooms-and-devices/device/{self.device_name}",
                identifiers={(DOMAIN, self.device_id)},
                name=self.device_name,
                manufacturer=DEFAULT_NAME,
                sw_version=device_info.get("currentFwVersion")
                or device_info.get("firmwareVersion"),
                model=device_info.get("deviceType") or device_info.get("type"),
            )


class TadoHomeEntity(Entity):
    """Base implementation for Tado home."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, tado: TadoConnector) -> None:
        """Initialize a Tado home."""
        super().__init__()
        self.home_name = tado.home_name
        self.home_id = tado.home_id
        self._attr_device_info = DeviceInfo(
            configuration_url="https://app.tado.com",
            identifiers={(DOMAIN, str(tado.home_id))},
            manufacturer=DEFAULT_NAME,
            model=TADO_HOME,
            name=tado.home_name,
        )


class TadoZoneEntity(Entity):
    """Base implementation for Tado zone."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, zone_name: str, home_id: int, zone_id: int) -> None:
        """Initialize a Tado zone."""
        super().__init__()
        self.zone_name = zone_name
        self.zone_id = zone_id
        self._attr_device_info = DeviceInfo(
            configuration_url=(f"https://app.tado.com/en/main/home/zoneV2/{zone_id}"),
            identifiers={(DOMAIN, f"{home_id}_{zone_id}")},
            name=zone_name,
            manufacturer=DEFAULT_NAME,
            model=TADO_ZONE,
            suggested_area=zone_name,
        )
