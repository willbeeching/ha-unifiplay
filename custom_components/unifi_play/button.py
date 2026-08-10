"""Button platform for UniFi Play devices."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import UnifiPlayCoordinator
from .entity import UnifiPlayEntity, async_setup_platform_entities


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

    def _factory(device_id: str) -> list[ButtonEntity]:
        entities: list[ButtonEntity] = [
            UnifiPlayButton(coordinator, device_id, desc) for desc in BUTTONS
        ]
        entities.append(UnifiPlayEqResetButton(coordinator, device_id))
        return entities

    async_setup_platform_entities(coordinator, entry, async_add_entities, _factory)


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
        client = self._require_mqtt()
        getattr(client, self.entity_description.press_fn)()


# The device's own band labels.
EQ_BANDS = ("32", "64", "125", "250", "500", "1k", "2k", "4k", "8k", "16k")


class UnifiPlayEqResetButton(UnifiPlayEntity, ButtonEntity):
    """Flatten the 10-band graphic EQ.

    The device has no reset action of its own; the app achieves it by sending
    a table of zeroes, which is what this does.
    """

    _attr_name = "Reset EQ"
    _attr_icon = "mdi:tune-vertical-variant"

    def __init__(
        self,
        coordinator: UnifiPlayCoordinator,
        device_id: str,
    ) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"unifi_play_{self._device_state.mac}_eq_reset"

    async def async_press(self) -> None:
        client = self._require_mqtt()
        bands = self._device_state.eq_table or dict.fromkeys(EQ_BANDS, 0.0)
        client.set_eq_table(dict.fromkeys(bands, 0.0))
