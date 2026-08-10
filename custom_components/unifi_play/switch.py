"""Switch platform for UniFi Play devices."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import UnifiPlayCoordinator, UnifiPlayDeviceState
from .entity import UnifiPlayEntity, async_setup_platform_entities


@dataclass(frozen=True, kw_only=True)
class UnifiPlaySwitchDescription(SwitchEntityDescription):
    """Describes a UniFi Play switch entity."""

    value_fn: Callable[[UnifiPlayDeviceState], bool]
    set_fn: str


SWITCHES: tuple[UnifiPlaySwitchDescription, ...] = (
    UnifiPlaySwitchDescription(
        key="dynamic_boost",
        translation_key="dynamic_boost",
        name="Dynamic Boost",
        icon="mdi:volume-vibrate",
        value_fn=lambda s: s.loudness,
        set_fn="set_loudness",
    ),
    UnifiPlaySwitchDescription(
        key="equalizer",
        translation_key="equalizer",
        name="Dolby Atmos / Equalizer",
        icon="mdi:surround-sound",
        value_fn=lambda s: s.eq_enable,
        set_fn="set_eq_enable",
    ),
    UnifiPlaySwitchDescription(
        key="persistent_dashboard",
        translation_key="persistent_dashboard",
        name="Persistent Dashboard",
        icon="mdi:monitor-dashboard",
        value_fn=lambda s: s.persistent_dashboard,
        set_fn="set_persistent_dashboard",
    ),
    UnifiPlaySwitchDescription(
        key="voice_enhancement",
        translation_key="voice_enhancement",
        name="Voice Enhancement",
        icon="mdi:account-voice",
        value_fn=lambda s: s.voice_enhancement,
        set_fn="set_voice_enhancement",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up UniFi Play switch entities."""
    coordinator: UnifiPlayCoordinator = hass.data[DOMAIN][entry.entry_id]

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

    _attr_name = "Alarm Sound Test"
    _attr_icon = "mdi:alarm-light"
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
