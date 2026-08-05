"""Media player platform for UniFi Play devices."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime

import aiohttp
from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SOURCE_REVERSE, source_label, source_labels
from .coordinator import UnifiPlayCoordinator
from .entity import UnifiPlayEntity, async_setup_platform_entities

_LOGGER = logging.getLogger(__name__)

SUPPORTED_FEATURES = (
    MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.VOLUME_STEP
    | MediaPlayerEntityFeature.VOLUME_MUTE
    | MediaPlayerEntityFeature.TURN_OFF
    | MediaPlayerEntityFeature.SELECT_SOURCE
    | MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PAUSE
)

# Source value that carries a streaming session; the other inputs are analogue
# or passthrough and have no transport or metadata of their own.
SOURCE_STREAMING = "streaming"

# Friendly names for the info event's ``service`` field. "spotify" is
# confirmed on the wire; the rest are best-effort guesses and unknown values
# fall through as-is rather than being hidden.
SERVICE_LABELS = {
    "spotify": "Spotify Connect",
    "airplay": "AirPlay",
    "cast": "Chromecast",
    "soundtrack": "Soundtrack Your Brand",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up UniFi Play media players from a config entry."""
    coordinator: UnifiPlayCoordinator = hass.data[DOMAIN][entry.entry_id]

    def _factory(device_id: str) -> list[UnifiPlayMediaPlayer]:
        return [UnifiPlayMediaPlayer(coordinator, device_id)]

    async_setup_platform_entities(coordinator, entry, async_add_entities, _factory)


class UnifiPlayMediaPlayer(UnifiPlayEntity, MediaPlayerEntity):
    """A media player entity for a single UniFi Play device."""

    _attr_name = None

    def __init__(
        self,
        coordinator: UnifiPlayCoordinator,
        device_id: str,
    ) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"unifi_play_{self._device_state.mac}"

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        """Offer skip only while the streaming source says it is available.

        The metadata event carries ``prev``/``next`` flags describing what the
        current source can do — the official app greys its buttons out the
        same way.
        """
        features = SUPPORTED_FEATURES
        ds = self._device_state
        if ds.can_prev:
            features |= MediaPlayerEntityFeature.PREVIOUS_TRACK
        if ds.can_next:
            features |= MediaPlayerEntityFeature.NEXT_TRACK
        return features

    @property
    def state(self) -> MediaPlayerState:
        ds = self._device_state
        if not ds.online:
            return MediaPlayerState.OFF
        if ds.stream_playing:
            return MediaPlayerState.PLAYING
        # Paused looks exactly like playing minus the flag: the source is still
        # streaming and the track is still loaded. Anything else is idle.
        if ds.source == SOURCE_STREAMING and ds.now_playing_song:
            return MediaPlayerState.PAUSED
        return MediaPlayerState.IDLE

    @property
    def volume_level(self) -> float | None:
        return self._device_state.volume / 100.0

    @property
    def is_volume_muted(self) -> bool | None:
        return self._device_state.muted

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
    def media_playlist(self) -> str | None:
        return self._device_state.playlist or None

    @property
    def app_name(self) -> str | None:
        """The streaming service feeding the amp (Spotify Connect, AirPlay).

        The device reports this as ``service`` in every info event while a
        streaming session exists.
        """
        service = self._device_state.service
        if not service:
            return None
        return SERVICE_LABELS.get(service, service)

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
    def media_position_updated_at(self) -> datetime | None:
        """When media_position was last refreshed by the speaker.

        Home Assistant draws the playhead as position + (now - this
        timestamp). Reporting media_position without it leaves the progress
        bar frozen at whatever value arrived last.
        """
        if self._device_state.now_playing_current <= 0:
            return None
        return self._device_state.now_playing_current_at

    @property
    def media_image_hash(self) -> str | None:
        """Cache key for the cover art.

        Home Assistant's default hashes media_image_url, but the speaker
        serves artwork from a fixed path that never changes between tracks
        (it swaps the file contents in place). With a constant URL the hash
        never changes either, so the frontend and the entity_picture proxy
        keep serving the first track's cached image forever. Seed the hash
        with the track identity instead.
        """
        if self.media_image_url is None:
            return None
        ds = self._device_state
        seed = "|".join(
            (
                ds.now_playing_cover,
                ds.now_playing_song,
                ds.now_playing_artist,
                ds.now_playing_album,
                str(ds.now_playing_length),
            )
        )
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]

    @property
    def media_image_url(self) -> str | None:
        """Cover art, served by the speaker itself.

        The metadata event carries a bare file path; the official app fetches
        it as https://{deviceIP}/{filename}.
        """
        cover = self._device_state.now_playing_cover
        if not cover:
            return None
        if cover.startswith(("http://", "https://")):
            return cover
        ip = self._device_state.ip
        if not ip:
            return None
        return f"https://{ip}/{cover.lstrip('/')}"

    async def async_get_media_image(self) -> tuple[bytes | None, str | None]:
        """Fetch the cover art ourselves: the speaker's cert is self-signed."""
        url = self.media_image_url
        if url is None:
            return None, None
        session = async_get_clientsession(self.hass, verify_ssl=False)
        try:
            async with asyncio.timeout(10):
                response = await session.get(url)
                if response.status != 200:
                    return None, None
                return await response.read(), response.content_type
        except (TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.debug("Failed to fetch cover art from %s: %s", url, err)
            return None, None

    @property
    def source(self) -> str | None:
        ds = self._device_state
        return source_label(ds.platform, ds.source)

    @property
    def source_list(self) -> list[str]:
        return list(source_labels(self._device_state.platform).values())

    async def async_select_source(self, source: str) -> None:
        device_value = SOURCE_REVERSE.get(source)
        client = self._mqtt()
        if client and device_value:
            client.set_source(device_value)

    async def async_media_play(self) -> None:
        client = self._mqtt()
        if client:
            client.set_player("play")

    async def async_media_pause(self) -> None:
        client = self._mqtt()
        if client:
            client.set_player("pause")

    async def async_media_next_track(self) -> None:
        client = self._mqtt()
        if client:
            client.set_player("next")

    async def async_media_previous_track(self) -> None:
        client = self._mqtt()
        if client:
            client.set_player("prev")

    async def async_set_volume_level(self, volume: float) -> None:
        client = self._mqtt()
        if client:
            client.set_volume(int(volume * 100))

    async def async_mute_volume(self, mute: bool) -> None:
        """Software mute.

        The speaker has no real mute channel (the client maps mute to
        set_volume(0)), so the muted flag and the pre-mute volume have to be
        tracked here. Reading restore_volume at unmute time is too late - the
        volume is already 0 by then, so it always restored the fallback - and
        because the device never reports itself muted, is_volume_muted never
        went true and a toggle could never unmute.
        """
        client = self._mqtt()
        if not client:
            return
        ds = self._device_state
        if mute:
            if ds.volume > 0:
                ds.mute_restore = ds.volume
            ds.muted = True
            # Until the device reports volume 0 back, ignore any in-flight
            # info event still carrying the pre-mute volume.
            ds.mute_confirmed = False
            client.set_mute(True)
        else:
            ds.muted = False
            client.set_mute(False, restore_volume=ds.mute_restore or 20)
        self.async_write_ha_state()

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

    async def async_turn_off(self) -> None:
        client = self._mqtt()
        if client:
            client.publish_action("stop")
