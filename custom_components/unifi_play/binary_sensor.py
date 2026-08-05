"""Binary sensor platform for UniFi Play devices."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import UnifiPlayCoordinator
from .entity import UnifiPlayEntity, async_setup_platform_entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up UniFi Play binary sensor entities."""
    coordinator: UnifiPlayCoordinator = hass.data[DOMAIN][entry.entry_id]

    def _factory(device_id: str) -> list[BinarySensorEntity]:
        return [
            UnifiPlayAdminLockSensor(coordinator, device_id),
            UnifiPlayAnnouncingSensor(coordinator, device_id),
        ]

    async_setup_platform_entities(coordinator, entry, async_add_entities, _factory)


class UnifiPlayAdminLockSensor(UnifiPlayEntity, BinarySensorEntity):
    """Reports whether the device's Admin Lock is engaged.

    Read-only on purpose: enabling/disabling requires the PIN handshake, and
    the lock only gates the official app's settings UI - device commands are
    accepted either way - so Home Assistant just surfaces the state.
    """

    _attr_name = "Admin Lock"
    _attr_device_class = BinarySensorDeviceClass.LOCK

    def __init__(
        self,
        coordinator: UnifiPlayCoordinator,
        device_id: str,
    ) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"unifi_play_{self._device_state.mac}_admin_lock"

    @property
    def is_on(self) -> bool:
        # device_class LOCK: on means UNLOCKED.
        return not self._device_state.locked


class UnifiPlayAnnouncingSensor(UnifiPlayEntity, BinarySensorEntity):
    """On while the device is playing an announcement.

    The device reports ``announcing`` plus the clip length and the schedule
    name in its info events, and pauses whatever was streaming for the
    duration.
    """

    _attr_name = "Announcing"
    _attr_icon = "mdi:bullhorn"

    def __init__(
        self,
        coordinator: UnifiPlayCoordinator,
        device_id: str,
    ) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"unifi_play_{self._device_state.mac}_announcing"

    @property
    def is_on(self) -> bool:
        return self._device_state.announcing

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        state = self._device_state
        return {
            "type": state.announcing_type or None,
            "length": state.announce_length or None,
            "schedule_name": state.announce_name or None,
            "alert_volume": state.temp_volume,
        }
