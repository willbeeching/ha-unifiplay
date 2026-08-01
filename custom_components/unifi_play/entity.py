"""Base entity for UniFi Play."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MODEL_NAMES
from .coordinator import UnifiPlayCoordinator, UnifiPlayDeviceState
from .mqtt_client import UnifiPlayMqttClient


def async_setup_platform_entities(
    coordinator: UnifiPlayCoordinator,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    factory: Callable[[str], Iterable[Entity]],
) -> None:
    """Create entities for current devices and for any discovered later.

    The coordinator re-scans every five minutes, so a device adopted (or
    powered on) after setup appears in coordinator.data mid-flight; without
    this listener its entities would only exist after a reload.
    """
    known: set[str] = set()

    def _sync() -> None:
        new_ids = [dev_id for dev_id in coordinator.data if dev_id not in known]
        if not new_ids:
            return
        known.update(new_ids)
        async_add_entities([entity for dev_id in new_ids for entity in factory(dev_id)])

    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))


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
            model=MODEL_NAMES.get(state.platform, state.platform),
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
