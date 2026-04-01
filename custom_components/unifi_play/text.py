"""Text platform for UniFi Play devices."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.text import TextEntity, TextEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import UnifiPlayCoordinator, UnifiPlayDeviceState
from .entity import UnifiPlayEntity

HEX_COLOR_RE = re.compile(r"^[0-9A-Fa-f]{6}$")


@dataclass(frozen=True, kw_only=True)
class UnifiPlayTextDescription(TextEntityDescription):
    """Describes a UniFi Play text entity."""

    value_fn: Callable[[UnifiPlayDeviceState], str | None]
    set_fn: str


TEXTS: tuple[UnifiPlayTextDescription, ...] = (
    UnifiPlayTextDescription(
        key="led_color",
        translation_key="led_color",
        name="LED Color",
        icon="mdi:palette",
        value_fn=lambda s: s.led_color,
        set_fn="set_led_color",
    ),
    UnifiPlayTextDescription(
        key="screen_color",
        translation_key="screen_color",
        name="Screen Color",
        icon="mdi:palette",
        value_fn=lambda s: s.screen_color,
        set_fn="set_screen_color",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up UniFi Play text entities."""
    coordinator: UnifiPlayCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[UnifiPlayText] = []
    for device_id in coordinator.data:
        for desc in TEXTS:
            entities.append(UnifiPlayText(coordinator, device_id, desc))
    async_add_entities(entities)


class UnifiPlayText(UnifiPlayEntity, TextEntity):
    """A text entity for a UniFi Play device color setting."""

    entity_description: UnifiPlayTextDescription
    _attr_pattern = r"[0-9A-Fa-f]{6}"

    def __init__(
        self,
        coordinator: UnifiPlayCoordinator,
        device_id: str,
        description: UnifiPlayTextDescription,
    ) -> None:
        super().__init__(coordinator, device_id)
        self.entity_description = description
        self._attr_unique_id = f"unifi_play_{self._device_state.mac}_{description.key}"

    @property
    def native_value(self) -> str | None:
        return self.entity_description.value_fn(self._device_state)

    async def async_set_value(self, value: str) -> None:
        if not HEX_COLOR_RE.match(value):
            return
        client = self._mqtt()
        if client:
            getattr(client, self.entity_description.set_fn)(value.upper())
