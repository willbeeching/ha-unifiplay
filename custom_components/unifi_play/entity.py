"""Base entity for UniFi Play."""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import UnifiPlayCoordinator, UnifiPlayDeviceState
from .mqtt_client import UnifiPlayMqttClient


class UnifiPlayEntity(CoordinatorEntity[UnifiPlayCoordinator]):
    """Base class for all UniFi Play entities."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: UnifiPlayCoordinator,
        device_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        state = self._device_state
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, state.mac)},
            name=state.device_name or state.name,
            manufacturer="Ubiquiti",
            model=state.platform,
            sw_version=state.firmware,
        )

    @property
    def _device_state(self) -> UnifiPlayDeviceState:
        return self.coordinator.data[self._device_id]

    def _mqtt(self) -> UnifiPlayMqttClient | None:
        return self.coordinator.get_mqtt_client(self._device_id)

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()
