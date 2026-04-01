"""Text platform for UniFi Play devices."""

from __future__ import annotations

import re

from homeassistant.components.text import TextEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import UnifiPlayCoordinator
from .entity import UnifiPlayEntity

HEX_RE = re.compile(r"^[0-9A-Fa-f]{6}$")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up UniFi Play text entities."""
    coordinator: UnifiPlayCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[UnifiPlayLedColorText] = []
    for device_id in coordinator.data:
        entities.append(UnifiPlayLedColorText(coordinator, device_id))
    async_add_entities(entities)


class UnifiPlayLedColorText(UnifiPlayEntity, TextEntity):
    """Text entity for setting LED/screen color as a hex string."""

    _attr_name = "LED Color"
    _attr_icon = "mdi:palette"
    _attr_native_min = 6
    _attr_native_max = 6
    _attr_pattern = r"[0-9A-Fa-f]{6}"

    def __init__(
        self,
        coordinator: UnifiPlayCoordinator,
        device_id: str,
    ) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"unifi_play_{self._device_state.mac}_led_color"

    @property
    def native_value(self) -> str | None:
        return self._device_state.led_color or None

    async def async_set_value(self, value: str) -> None:
        value = value.lstrip("#").upper()
        if not HEX_RE.match(value):
            return
        client = self._mqtt()
        if client:
            client.set_led_color(value)
