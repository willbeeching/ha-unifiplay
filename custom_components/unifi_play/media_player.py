"""Media player platform for UniFi Play devices."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import UnifiPlayCoordinator, UnifiPlayDeviceState

_LOGGER = logging.getLogger(__name__)

SOURCE_MAP = {
    "lineIn": "Line In",
    "bluetooth": "Bluetooth",
    "airplay": "AirPlay",
    "spotify": "Spotify",
    "hdmi": "HDMI eARC",
    "optical": "Optical",
}

SOURCE_REVERSE_MAP = {v: k for k, v in SOURCE_MAP.items()}

SUPPORTED_FEATURES = (
    MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.VOLUME_STEP
    | MediaPlayerEntityFeature.VOLUME_MUTE
    | MediaPlayerEntityFeature.SELECT_SOURCE
    | MediaPlayerEntityFeature.TURN_OFF
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up UniFi Play media players from a config entry."""
    coordinator: UnifiPlayCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        UnifiPlayMediaPlayer(coordinator, device_id)
        for device_id in coordinator.data
    ]

    async_add_entities(entities, True)


class UnifiPlayMediaPlayer(
    CoordinatorEntity[UnifiPlayCoordinator], MediaPlayerEntity
):
    """A media player entity for a single UniFi Play device."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_source_list = list(SOURCE_MAP.values())
    _attr_supported_features = SUPPORTED_FEATURES

    def __init__(
        self,
        coordinator: UnifiPlayCoordinator,
        device_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        state = self._device_state
        self._attr_unique_id = f"unifi_play_{state.mac}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, state.mac)},
            name=state.device_name or state.name,
            manufacturer="Ubiquiti",
            model=state.platform,
            sw_version=state.firmware,
        )

    @property
    def _device_state(self) -> UnifiPlayDeviceState:
        return self.coordinator.data[self._device_id]

    @property
    def state(self) -> MediaPlayerState:
        ds = self._device_state
        if not ds.online:
            return MediaPlayerState.OFF
        if ds.stream_playing:
            return MediaPlayerState.PLAYING
        return MediaPlayerState.IDLE

    @property
    def volume_level(self) -> float | None:
        return self._device_state.volume / 100.0

    @property
    def is_volume_muted(self) -> bool | None:
        return self._device_state.muted

    @property
    def source(self) -> str | None:
        return SOURCE_MAP.get(self._device_state.source, self._device_state.source)

    @property
    def media_title(self) -> str | None:
        return self._device_state.now_playing_song or None

    @property
    def media_artist(self) -> str | None:
        return self._device_state.now_playing_artist or None

    @property
    def media_album_name(self) -> str | None:
        return self._device_state.now_playing_album or None

    @property
    def media_content_type(self) -> MediaType | None:
        if self._device_state.stream_playing:
            return MediaType.MUSIC
        return None

    @property
    def media_duration(self) -> int | None:
        length = self._device_state.now_playing_length
        return length if length > 0 else None

    @property
    def media_position(self) -> int | None:
        pos = self._device_state.now_playing_current
        return pos if pos > 0 else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        ds = self._device_state
        return {
            "firmware": ds.firmware,
            "eq_enabled": ds.eq_enable,
            "loudness": ds.loudness,
            "balance": ds.balance,
            "volume_limit": ds.vol_limit,
            "subwoofer": ds.subwoofer,
            "ip_address": ds.ip,
        }

    def _mqtt(self) -> Any:
        return self.coordinator.get_mqtt_client(self._device_id)

    async def async_set_volume_level(self, volume: float) -> None:
        client = self._mqtt()
        if client:
            client.set_volume(int(volume * 100))

    async def async_mute_volume(self, mute: bool) -> None:
        client = self._mqtt()
        if client:
            client.set_mute(mute, restore_volume=self._device_state.volume or 20)

    async def async_volume_up(self) -> None:
        ds = self._device_state
        new_vol = min(ds.volume + 5, ds.vol_limit)
        client = self._mqtt()
        if client:
            client.set_volume(new_vol)

    async def async_volume_down(self) -> None:
        ds = self._device_state
        new_vol = max(ds.volume - 5, 0)
        client = self._mqtt()
        if client:
            client.set_volume(new_vol)

    async def async_select_source(self, source: str) -> None:
        api_source = SOURCE_REVERSE_MAP.get(source, source)
        client = self._mqtt()
        if client:
            client.set_source(api_source)

    async def async_turn_off(self) -> None:
        client = self._mqtt()
        if client:
            client.publish_action("stop")

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()
