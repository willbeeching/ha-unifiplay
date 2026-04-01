"""Sensor platform for UniFi Play devices."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import UnifiPlayCoordinator, UnifiPlayDeviceState
from .entity import UnifiPlayEntity


@dataclass(frozen=True, kw_only=True)
class UnifiPlaySensorDescription(SensorEntityDescription):
    """Describes a UniFi Play sensor entity."""

    value_fn: Callable[[UnifiPlayDeviceState], str | None]


SENSORS: tuple[UnifiPlaySensorDescription, ...] = (
    UnifiPlaySensorDescription(
        key="upgrade_status",
        translation_key="upgrade_status",
        name="Firmware Status",
        icon="mdi:package-up",
        value_fn=lambda s: s.upgrade_status or None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up UniFi Play sensor entities."""
    coordinator: UnifiPlayCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[UnifiPlaySensor] = []
    for device_id in coordinator.data:
        for desc in SENSORS:
            entities.append(UnifiPlaySensor(coordinator, device_id, desc))
    async_add_entities(entities)


class UnifiPlaySensor(UnifiPlayEntity, SensorEntity):
    """A sensor entity for a UniFi Play device."""

    entity_description: UnifiPlaySensorDescription

    def __init__(
        self,
        coordinator: UnifiPlayCoordinator,
        device_id: str,
        description: UnifiPlaySensorDescription,
    ) -> None:
        super().__init__(coordinator, device_id)
        self.entity_description = description
        self._attr_unique_id = f"unifi_play_{self._device_state.mac}_{description.key}"

    @property
    def native_value(self) -> str | None:
        return self.entity_description.value_fn(self._device_state)
