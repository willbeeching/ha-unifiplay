"""Text platform for UniFi Play devices."""

from __future__ import annotations

import re

from homeassistant.components.text import TextEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import UnifiPlayConfigEntry, UnifiPlayCoordinator
from .entity import UnifiPlayEntity, async_setup_platform_entities

HEX_RE = re.compile(r"^[0-9A-Fa-f]{6}$")


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
    """Set up UniFi Play text entities."""
    coordinator = entry.runtime_data

    def _factory(device_id: str) -> list[UnifiPlayLedColorText]:
        return [UnifiPlayLedColorText(coordinator, device_id)]

    async_setup_platform_entities(coordinator, entry, async_add_entities, _factory)


class UnifiPlayLedColorText(UnifiPlayEntity, TextEntity):
    """Text entity for setting LED/screen color as a hex string."""

    _attr_translation_key = "led_color"
    _attr_icon = "mdi:palette"
    # A colour is a setting, not something the speaker is doing.
    _attr_entity_category = EntityCategory.CONFIG
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
            # Unreachable through Home Assistant, which checks _attr_pattern
            # and the length bounds before the entity is called. Kept for a
            # caller that reaches the entity directly, and raising rather
            # than returning so that caller is told rather than left
            # believing the colour changed.
            raise ServiceValidationError(  # pragma: no cover
                translation_domain=DOMAIN,
                translation_key="invalid_hex_colour",
                translation_placeholders={"value": value},
            )
        client = self._require_mqtt()
        client.set_led_color(value)
