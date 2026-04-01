"""Select platform for UniFi Play devices."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import UnifiPlayCoordinator, UnifiPlayDeviceState
from .entity import UnifiPlayEntity

CHANNEL_OPTIONS = {"0": "Stereo", "1": "Mono"}
CHANNEL_REVERSE = {v: k for k, v in CHANNEL_OPTIONS.items()}

PHASE_OPTIONS = {"0": "0\u00b0", "180": "180\u00b0"}
PHASE_REVERSE = {v: k for k, v in PHASE_OPTIONS.items()}

EQ_PRESET_OPTIONS = ["Custom", "Music", "Movie", "Night"]
EQ_PRESET_REVERSE = {v: v.lower() for v in EQ_PRESET_OPTIONS}


@dataclass(frozen=True, kw_only=True)
class UnifiPlaySelectDescription(SelectEntityDescription):
    """Describes a UniFi Play select entity."""

    value_fn: Callable[[UnifiPlayDeviceState], str | None]
    set_fn: str
    convert_fn: Callable[[str], str | int]


SELECTS: tuple[UnifiPlaySelectDescription, ...] = (
    UnifiPlaySelectDescription(
        key="eq_preset",
        translation_key="eq_preset",
        name="EQ Preset",
        icon="mdi:equalizer",
        options=EQ_PRESET_OPTIONS,
        value_fn=lambda s: s.eq_preset.capitalize() if s.eq_preset else None,
        set_fn="set_eq_preset",
        convert_fn=lambda v: v.lower(),
    ),
    UnifiPlaySelectDescription(
        key="channels",
        translation_key="channels",
        name="Channels",
        icon="mdi:surround-sound-2-0",
        options=list(CHANNEL_OPTIONS.values()),
        value_fn=lambda s: CHANNEL_OPTIONS.get(str(s.channels)),
        set_fn="set_channels",
        convert_fn=lambda v: int(CHANNEL_REVERSE[v]),
    ),
    UnifiPlaySelectDescription(
        key="sub_phase",
        translation_key="sub_phase",
        name="Sub Phase",
        icon="mdi:sine-wave",
        options=list(PHASE_OPTIONS.values()),
        value_fn=lambda s: PHASE_OPTIONS.get(str(s.sub_phase)),
        set_fn="set_sub_phase",
        convert_fn=lambda v: int(PHASE_REVERSE[v]),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up UniFi Play select entities."""
    coordinator: UnifiPlayCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[UnifiPlaySelect] = []
    for device_id in coordinator.data:
        for desc in SELECTS:
            entities.append(UnifiPlaySelect(coordinator, device_id, desc))
    async_add_entities(entities)


class UnifiPlaySelect(UnifiPlayEntity, SelectEntity):
    """A select entity for a UniFi Play device setting."""

    entity_description: UnifiPlaySelectDescription

    def __init__(
        self,
        coordinator: UnifiPlayCoordinator,
        device_id: str,
        description: UnifiPlaySelectDescription,
    ) -> None:
        super().__init__(coordinator, device_id)
        self.entity_description = description
        self._attr_unique_id = f"unifi_play_{self._device_state.mac}_{description.key}"

    @property
    def current_option(self) -> str | None:
        return self.entity_description.value_fn(self._device_state)

    async def async_select_option(self, option: str) -> None:
        client = self._mqtt()
        if client:
            value = self.entity_description.convert_fn(option)
            getattr(client, self.entity_description.set_fn)(value)
