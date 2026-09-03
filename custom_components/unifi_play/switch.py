"""Switch platform for UniFi Play devices."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import (
    UnifiPlayConfigEntry,
    UnifiPlayCoordinator,
    UnifiPlayDeviceState,
)
from .entity import UnifiPlayEntity, async_setup_platform_entities


@dataclass(frozen=True, kw_only=True)
class UnifiPlaySwitchDescription(SwitchEntityDescription):
    """Describes a UniFi Play switch entity."""

    value_fn: Callable[[UnifiPlayDeviceState], bool]
    set_fn: str


SWITCHES: tuple[UnifiPlaySwitchDescription, ...] = (
    UnifiPlaySwitchDescription(
        key="dynamic_boost",
        entity_category=EntityCategory.CONFIG,
        translation_key="dynamic_boost",
        icon="mdi:volume-vibrate",
        value_fn=lambda s: s.loudness,
        set_fn="set_loudness",
    ),
    UnifiPlaySwitchDescription(
        key="equalizer",
        entity_category=EntityCategory.CONFIG,
        translation_key="equalizer",
        icon="mdi:surround-sound",
        value_fn=lambda s: s.eq_enable,
        set_fn="set_eq_enable",
    ),
    UnifiPlaySwitchDescription(
        key="persistent_dashboard",
        entity_category=EntityCategory.CONFIG,
        translation_key="persistent_dashboard",
        icon="mdi:monitor-dashboard",
        value_fn=lambda s: s.persistent_dashboard,
        set_fn="set_persistent_dashboard",
    ),
    UnifiPlaySwitchDescription(
        key="voice_enhancement",
        entity_category=EntityCategory.CONFIG,
        translation_key="voice_enhancement",
        icon="mdi:account-voice",
        value_fn=lambda s: s.voice_enhancement,
        set_fn="set_voice_enhancement",
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
    """Set up UniFi Play switch entities."""
    coordinator = entry.runtime_data

    def _factory(device_id: str) -> list[SwitchEntity]:
        entities: list[SwitchEntity] = [
            UnifiPlaySwitch(coordinator, device_id, desc) for desc in SWITCHES
        ]
        entities.append(UnifiPlayAlarmTestSwitch(coordinator, device_id))
        return entities

    async_setup_platform_entities(coordinator, entry, async_add_entities, _factory)


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
        client = self._require_mqtt()
        getattr(client, self.entity_description.set_fn)(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        client = self._require_mqtt()
        getattr(client, self.entity_description.set_fn)(False)


class UnifiPlayAlarmTestSwitch(UnifiPlayEntity, SwitchEntity):
    """Plays the device's alarm sound while on - the only play-something-now
    primitive the device offers, captured from the app's alarm sound preview.

    Optimistic: the device sends no state event for a running test, so this
    tracks what it last asked for.
    """

    _attr_translation_key = "alarm_sound_test"
    _attr_assumed_state = True

    def __init__(
        self,
        coordinator: UnifiPlayCoordinator,
        device_id: str,
    ) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"unifi_play_{self._device_state.mac}_alarm_test"
        self._test_on = False

    @property
    def is_on(self) -> bool:
        return self._test_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        client = self._require_mqtt()
        client.alarm_test(True)
        self._test_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        client = self._require_mqtt()
        client.alarm_test(False)
        self._test_on = False
        self.async_write_ha_state()
