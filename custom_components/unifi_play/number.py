"""Number platform for UniFi Play devices."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import is_amp
from .coordinator import (
    UnifiPlayConfigEntry,
    UnifiPlayCoordinator,
    UnifiPlayDeviceState,
)
from .entity import (
    UnifiPlayEntity,
    async_setup_optional_entities,
    async_setup_platform_entities,
)


@dataclass(frozen=True, kw_only=True)
class UnifiPlayNumberDescription(NumberEntityDescription):
    """Describes a UniFi Play number entity."""

    value_fn: Callable[[UnifiPlayDeviceState], float]
    set_fn: str
    amp_only: bool = False
    # Optional hardware: created only once the device reports a subwoofer
    # attached, not merely because the model has a sub output. See #17.
    requires_sub: bool = False


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
    UnifiPlayNumberDescription(
        key="sub_crossover",
        translation_key="sub_crossover",
        name="Sub Crossover",
        icon="mdi:sine-wave",
        native_min_value=40,
        native_max_value=200,
        native_step=5,
        native_unit_of_measurement="Hz",
        mode=NumberMode.SLIDER,
        value_fn=lambda s: float(s.sub_crossover),
        set_fn="set_sub_crossover",
        amp_only=True,
        requires_sub=True,
    ),
    UnifiPlayNumberDescription(
        key="sub_level",
        translation_key="sub_level",
        name="Sub Level",
        icon="mdi:speaker",
        native_min_value=-10,
        native_max_value=10,
        native_step=1,
        mode=NumberMode.SLIDER,
        value_fn=lambda s: float(s.sub_level),
        set_fn="set_sub_level",
        amp_only=True,
        requires_sub=True,
    ),
    UnifiPlayNumberDescription(
        key="announcement_volume",
        translation_key="announcement_volume",
        name="Announcement Volume",
        icon="mdi:bullhorn-outline",
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement="%",
        mode=NumberMode.SLIDER,
        value_fn=lambda s: float(s.ann_volume),
        set_fn="set_announcement_vol",
    ),
)


# The device's own band labels, in audio order rather than dict order.
EQ_BANDS = ("32", "64", "125", "250", "500", "1k", "2k", "4k", "8k", "16k")


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
    """Set up UniFi Play number entities."""
    coordinator = entry.runtime_data

    def _factory(device_id: str) -> list[NumberEntity]:
        state = coordinator.data[device_id]
        entities: list[NumberEntity] = [
            UnifiPlayNumber(coordinator, device_id, desc)
            for desc in NUMBERS
            if (not desc.amp_only or is_amp(state.platform)) and not desc.requires_sub
        ]
        entities += [UnifiPlayEqBand(coordinator, device_id, band) for band in EQ_BANDS]
        return entities

    async_setup_platform_entities(coordinator, entry, async_add_entities, _factory)

    def _sub_factory(device_id: str) -> list[NumberEntity]:
        state = coordinator.data[device_id]
        return [
            UnifiPlayNumber(coordinator, device_id, desc)
            for desc in NUMBERS
            if desc.requires_sub and (not desc.amp_only or is_amp(state.platform))
        ]

    async_setup_optional_entities(
        coordinator,
        entry,
        async_add_entities,
        _sub_factory,
        lambda s: s.subwoofer,
    )


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
        client = self._require_mqtt()
        getattr(client, self.entity_description.set_fn)(int(value))


class UnifiPlayEqBand(UnifiPlayEntity, NumberEntity):
    """One band of the 10-band graphic EQ.

    The device only accepts the whole table at once, so a single band edit
    reads the current table, replaces one entry and sends all ten. Edits land
    on the ``custom`` profile, which is what the app does too.
    """

    _attr_native_min_value = -12
    _attr_native_max_value = 12
    _attr_native_step = 0.1
    _attr_native_unit_of_measurement = "dB"
    _attr_mode = NumberMode.SLIDER
    _attr_icon = "mdi:tune-vertical"
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: UnifiPlayCoordinator,
        device_id: str,
        band: str,
    ) -> None:
        super().__init__(coordinator, device_id)
        self._band = band
        self._attr_name = f"EQ {band}Hz" if band.isdigit() else f"EQ {band}"
        self._attr_unique_id = f"unifi_play_{self._device_state.mac}_eq_{band}"

    @property
    def available(self) -> bool:
        return super().available and bool(self._device_state.eq_table)

    @property
    def native_value(self) -> float | None:
        raw = self._device_state.eq_table.get(self._band)
        return float(raw) if raw is not None else None

    async def async_set_native_value(self, value: float) -> None:
        client = self._require_mqtt()
        table = {k: float(v) for k, v in self._device_state.eq_table.items()}
        if not table:
            return
        table[self._band] = round(value, 2)
        client.set_eq_table(table)
