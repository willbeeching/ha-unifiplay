"""Sensor platform for UniFi Play devices."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import SERVICE_LABELS
from .coordinator import (
    UnifiPlayConfigEntry,
    UnifiPlayCoordinator,
    UnifiPlayDeviceState,
)
from .entity import UnifiPlayEntity, async_setup_platform_entities


@dataclass(frozen=True, kw_only=True)
class UnifiPlaySensorDescription(SensorEntityDescription):
    """Describes a UniFi Play sensor entity."""

    value_fn: Callable[[UnifiPlayDeviceState], Any]
    attrs_fn: Callable[[UnifiPlayDeviceState], dict[str, Any]] | None = None


SENSORS: tuple[UnifiPlaySensorDescription, ...] = (
    UnifiPlaySensorDescription(
        key="upgrade_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        translation_key="upgrade_status",
        icon="mdi:package-up",
        value_fn=lambda s: s.upgrade_status or None,
    ),
    UnifiPlaySensorDescription(
        key="streaming_service",
        translation_key="streaming_service",
        icon="mdi:cast-audio",
        value_fn=lambda s: (
            SERVICE_LABELS.get(s.service, s.service) if s.service else None
        ),
    ),
    UnifiPlaySensorDescription(
        key="alarms",
        translation_key="alarms",
        icon="mdi:alarm",
        value_fn=lambda s: len(s.alarms),
        attrs_fn=lambda s: {"alarms": s.alarms},
    ),
    UnifiPlaySensorDescription(
        key="quiet_hours",
        translation_key="quiet_hours",
        icon="mdi:weather-night",
        value_fn=lambda s: len(s.quiet_hours),
        attrs_fn=lambda s: {"quiet_hours": s.quiet_hours},
    ),
    UnifiPlaySensorDescription(
        key="announcements",
        translation_key="announcements",
        icon="mdi:bullhorn",
        value_fn=lambda s: len(s.ann_files),
        attrs_fn=lambda s: {
            "files": s.ann_files,
            "schedule": s.ann_schedule,
            "chime": s.ann_chime,
        },
    ),
    UnifiPlaySensorDescription(
        key="uptime",
        translation_key="uptime",
        icon="mdi:timer-outline",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda s: s.uptime or None,
    ),
    UnifiPlaySensorDescription(
        key="space",
        translation_key="space",
        icon="mdi:home-group",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda s: s.space or None,
    ),
)


# Every command is a fire-and-forget MQTT publish to a device on the LAN:
# nothing here blocks, nothing rate-limits, and the coordinator's own poll is
# the only thing that fetches. Serialising would only add latency to a
# "turn everything down" script.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UnifiPlayConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up UniFi Play sensor entities."""
    coordinator = entry.runtime_data

    def _factory(device_id: str) -> list[UnifiPlaySensor]:
        return [UnifiPlaySensor(coordinator, device_id, desc) for desc in SENSORS]

    async_setup_platform_entities(coordinator, entry, async_add_entities, _factory)


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
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self._device_state)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self._device_state)
