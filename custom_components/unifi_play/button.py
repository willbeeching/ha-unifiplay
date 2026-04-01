"""Button platform for UniFi Play devices."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import UnifiPlayCoordinator
from .entity import UnifiPlayEntity


@dataclass(frozen=True, kw_only=True)
class UnifiPlayButtonDescription(ButtonEntityDescription):
    """Describes a UniFi Play button entity."""

    press_fn: str


BUTTONS: tuple[UnifiPlayButtonDescription, ...] = (
    UnifiPlayButtonDescription(
        key="locate",
        translation_key="locate",
        name="Locate",
        icon="mdi:map-marker-question",
        press_fn="locate",
    ),
    UnifiPlayButtonDescription(
        key="restart",
        translation_key="restart",
        name="Restart",
        icon="mdi:restart",
        press_fn="restart",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up UniFi Play button entities."""
    coordinator: UnifiPlayCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[UnifiPlayButton] = []
    for device_id in coordinator.data:
        for desc in BUTTONS:
            entities.append(UnifiPlayButton(coordinator, device_id, desc))
    async_add_entities(entities)


class UnifiPlayButton(UnifiPlayEntity, ButtonEntity):
    """A button entity for a UniFi Play device action."""

    entity_description: UnifiPlayButtonDescription

    def __init__(
        self,
        coordinator: UnifiPlayCoordinator,
        device_id: str,
        description: UnifiPlayButtonDescription,
    ) -> None:
        super().__init__(coordinator, device_id)
        self.entity_description = description
        self._attr_unique_id = f"unifi_play_{self._device_state.mac}_{description.key}"

    async def async_press(self) -> None:
        client = self._mqtt()
        if client:
            getattr(client, self.entity_description.press_fn)()
