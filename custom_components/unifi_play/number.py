"""Number platform for UniFi Play devices."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import UnifiPlayCoordinator, UnifiPlayDeviceState
from .entity import UnifiPlayEntity


@dataclass(frozen=True, kw_only=True)
class UnifiPlayNumberDescription(NumberEntityDescription):
    """Describes a UniFi Play number entity."""

    value_fn: Callable[[UnifiPlayDeviceState], float]
    set_fn: str


NUMBERS: tuple[UnifiPlayNumberDescription, ...] = (
    UnifiPlayNumberDescription(
        key="balance",
        translation_key="balance",
        name="Balance",
        icon="mdi:arrow-left-right",
        native_min_value=-100,
        native_max_value=100,
        native_step=1,
        mode=NumberMode.SLIDER,
        value_fn=lambda s: float(s.balance),
        set_fn="set_balance",
    ),
    UnifiPlayNumberDescription(
        key="volume_limit",
        translation_key="volume_limit",
        name="Volume Limit",
        icon="mdi:volume-off",
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement="%",
        mode=NumberMode.SLIDER,
        value_fn=lambda s: float(s.vol_limit),
        set_fn="set_vol_limit",
    ),
    UnifiPlayNumberDescription(
        key="screen_brightness",
        translation_key="screen_brightness",
        name="Screen Brightness",
        icon="mdi:brightness-6",
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement="%",
        mode=NumberMode.SLIDER,
        value_fn=lambda s: float(s.screen_brightness),
        set_fn="set_screen_brightness",
    ),
    UnifiPlayNumberDescription(
        key="led_brightness",
        translation_key="led_brightness",
        name="LED Brightness",
        icon="mdi:led-on",
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement="%",
        mode=NumberMode.SLIDER,
        value_fn=lambda s: float(s.led_brightness),
        set_fn="set_led_brightness",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up UniFi Play number entities."""
    coordinator: UnifiPlayCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[UnifiPlayNumber] = []
    for device_id in coordinator.data:
        for desc in NUMBERS:
            entities.append(UnifiPlayNumber(coordinator, device_id, desc))
    async_add_entities(entities)


class UnifiPlayNumber(UnifiPlayEntity, NumberEntity):
    """A number entity for a UniFi Play device setting."""

    entity_description: UnifiPlayNumberDescription

    def __init__(
        self,
        coordinator: UnifiPlayCoordinator,
        device_id: str,
        description: UnifiPlayNumberDescription,
    ) -> None:
        super().__init__(coordinator, device_id)
        self.entity_description = description
        self._attr_unique_id = f"unifi_play_{self._device_state.mac}_{description.key}"

    @property
    def native_value(self) -> float:
        return self.entity_description.value_fn(self._device_state)

    async def async_set_native_value(self, value: float) -> None:
        client = self._mqtt()
        if client:
            getattr(client, self.entity_description.set_fn)(int(value))
