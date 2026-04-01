"""Switch platform for UniFi Play devices."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import UnifiPlayCoordinator, UnifiPlayDeviceState
from .entity import UnifiPlayEntity


@dataclass(frozen=True, kw_only=True)
class UnifiPlaySwitchDescription(SwitchEntityDescription):
    """Describes a UniFi Play switch entity."""

    value_fn: Callable[[UnifiPlayDeviceState], bool]
    set_fn: str


SWITCHES: tuple[UnifiPlaySwitchDescription, ...] = (
    UnifiPlaySwitchDescription(
        key="loudness",
        translation_key="loudness",
        name="Loudness",
        icon="mdi:volume-vibrate",
        value_fn=lambda s: s.loudness,
        set_fn="set_loudness",
    ),
    UnifiPlaySwitchDescription(
        key="equalizer",
        translation_key="equalizer",
        name="Equalizer",
        icon="mdi:equalizer",
        value_fn=lambda s: s.eq_enable,
        set_fn="set_eq_enable",
    ),
    UnifiPlaySwitchDescription(
        key="subwoofer",
        translation_key="subwoofer",
        name="Subwoofer",
        icon="mdi:speaker",
        value_fn=lambda s: s.subwoofer,
        set_fn="set_subwoofer",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up UniFi Play switch entities."""
    coordinator: UnifiPlayCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[UnifiPlaySwitch] = []
    for device_id in coordinator.data:
        for desc in SWITCHES:
            entities.append(UnifiPlaySwitch(coordinator, device_id, desc))
    async_add_entities(entities)


class UnifiPlaySwitch(UnifiPlayEntity, SwitchEntity):
    """A switch entity for a UniFi Play device setting."""

    entity_description: UnifiPlaySwitchDescription

    def __init__(
        self,
        coordinator: UnifiPlayCoordinator,
        device_id: str,
        description: UnifiPlaySwitchDescription,
    ) -> None:
        super().__init__(coordinator, device_id)
        self.entity_description = description
        self._attr_unique_id = f"unifi_play_{self._device_state.mac}_{description.key}"

    @property
    def is_on(self) -> bool:
        return self.entity_description.value_fn(self._device_state)

    async def async_turn_on(self, **kwargs: Any) -> None:
        client = self._mqtt()
        if client:
            getattr(client, self.entity_description.set_fn)(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        client = self._mqtt()
        if client:
            getattr(client, self.entity_description.set_fn)(False)
