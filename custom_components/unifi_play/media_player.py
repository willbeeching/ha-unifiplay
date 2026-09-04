"""Media player platform for UniFi Play devices."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime
from typing import Any

import aiohttp
from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    SERVICE_LABELS,
    WB_STREAMING_LABEL,
    broadcast_input_label,
    broadcast_input_labels,
    source_label,
    source_labels,
    source_value,
)
from .coordinator import (
    UnifiPlayConfigEntry,
    UnifiPlayCoordinator,
    UnifiPlayDeviceState,
    UnifiPlayGroupState,
)
from .entity import UnifiPlayEntity, async_setup_platform_entities
from .helpers import mac_normalise, via_device_link

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
    """Set up UniFi Play media players from a config entry."""
    coordinator = entry.runtime_data

    def _device_factory(device_id: str) -> list[UnifiPlayMediaPlayer]:
        return [UnifiPlayMediaPlayer(coordinator, device_id)]

    async_setup_platform_entities(
        coordinator, entry, async_add_entities, _device_factory
    )

    # Zone entities — one per group_id, added when a zone first appears and
    # removed (including their virtual device) when the zone is deleted.
    # known_zones tracks what was added this session; stale_cleanup removes
    # registrations left over from previous sessions on every sync pass.
    known_zones: dict[str, UnifiPlayZonePlayer] = {}
    entity_reg = er.async_get(hass)
    device_reg = dr.async_get(hass)

    @callback
    def _sync_zones() -> None:
        active = set(coordinator.groups)

        # group_id -> registry device id, for the zone devices this entry owns.
        #
        # Built by walking the entry's own devices rather than by looking each
        # zone identifier up: device_registry.async_get_device is deprecated
        # from Home Assistant 2026.9 (identifiers are no longer unique across
        # config entries) and its replacement does not exist on the supported
        # floor, so neither call is usable across the range this integration
        # claims. Walking the entry's devices works on both, and is what the
        # orphan sweep below needed anyway.
        entry_devices = dr.async_entries_for_config_entry(device_reg, entry.entry_id)
        zone_device_ids: dict[str, str] = {}
        for dev_entry in entry_devices:
            for ident_domain, ident_value in dev_entry.identifiers:
                if (
                    ident_domain == DOMAIN
                    and isinstance(ident_value, str)
                    and ident_value.startswith("zone_")
                ):
                    zone_device_ids[ident_value[len("zone_") :]] = dev_entry.id
                    break

        # Purging is gated on every known speaker having reported its zones.
        #
        # After a restart the canonical view starts empty and fills as each
        # speaker connects, so an ungated purge deletes every zone entity and
        # its device on the way through - taking the registry rows with them,
        # and with those every dashboard card and automation pointing at that
        # zone. A speaker that is offline never reports, so this stays
        # blocked while anything is unreachable: the cost is a stale zone
        # entity until it returns, against destroying the user's
        # configuration.
        purge = coordinator.zones_fully_synced

        # Entity registrations for zones no longer reported by any speaker.
        # Covers both cross-session orphans (created last run, deleted while
        # Home Assistant was off) and zones removed in this session.
        for reg_entry in (
            er.async_entries_for_config_entry(entity_reg, entry.entry_id)
            if purge
            else []
        ):
            uid = reg_entry.unique_id or ""
            if not uid.startswith("unifi_play_zone_"):
                continue
            gid = uid[len("unifi_play_zone_") :]
            # UUIDs never contain underscores; a suffix like "_wideband" means
            # this is a per-zone binary sensor, not the zone media player — skip
            # it here and let the device-removal below clean it up indirectly.
            if "_" in gid:
                continue
            if gid in active:
                continue
            entity_reg.async_remove(reg_entry.entity_id)
            device_id = zone_device_ids.get(gid)
            if device_id is not None:
                device_reg.async_remove_device(device_id)
            known_zones.pop(gid, None)

        # Also purge orphaned zone DEVICES that have no matching entity.
        # This can happen when an entity was removed externally (Developer Tools,
        # MCP) while HA was running, leaving the device record behind with no
        # entity to trigger the entity-registry path above.
        _LOGGER.debug(
            "_sync_zones: %d device entries, purge=%s",
            len(entry_devices),
            purge,
        )
        for zone_gid, device_id in (zone_device_ids.items() if purge else ()):
            if zone_gid in active:
                continue
            if device_reg.async_get(device_id) is None:
                # Already removed by the entity sweep above.
                continue
            _LOGGER.warning(
                "Removing orphaned zone device: %s (gid=%s)", device_id, zone_gid
            )
            device_reg.async_remove_device(device_id)
            known_zones.pop(zone_gid, None)

        # Add entities for zones that appeared since the last sync.
        new_ids = [gid for gid in active if gid not in known_zones]
        if new_ids:
            new_entities = [UnifiPlayZonePlayer(coordinator, gid) for gid in new_ids]
            for e in new_entities:
                known_zones[e.group_id] = e
            async_add_entities(new_entities)

    _sync_zones()
    entry.async_on_unload(coordinator.async_add_listener(_sync_zones))


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
        device_value = source_value(self._device_state.platform, source)
        client = self._require_mqtt()
        if device_value:
            client.set_source(device_value)

    async def async_media_play(self) -> None:
        client = self._require_mqtt()
        client.set_player("play")

    async def async_media_pause(self) -> None:
        client = self._require_mqtt()
        client.set_player("pause")

    async def async_media_next_track(self) -> None:
        client = self._require_mqtt()
        client.set_player("next")

    async def async_media_previous_track(self) -> None:
        client = self._require_mqtt()
        client.set_player("prev")

    async def async_set_volume_level(self, volume: float) -> None:
        client = self._require_mqtt()
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
        client = self._require_mqtt()
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
        client = self._require_mqtt()
        client.set_volume(new_vol)

    async def async_volume_down(self) -> None:
        ds = self._device_state
        new_vol = max(ds.volume - 5, 0)
        client = self._require_mqtt()
        client.set_volume(new_vol)

    async def async_turn_off(self) -> None:
        client = self._require_mqtt()
        client.publish_action("stop")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        ds = self._device_state
        attrs: dict[str, Any] = {}
        if ds.hosting_group:
            attrs["hosting_group"] = ds.hosting_group
        if ds.sync_devices:
            attrs["zone_synced"] = True
        if ds.wb_broadcasting:
            attrs["wideband_broadcasting"] = ds.wb_broadcasting
        return attrs


class UnifiPlayZonePlayer(CoordinatorEntity[UnifiPlayCoordinator], MediaPlayerEntity):
    """A media player entity representing a UniFi Play zone.

    Zones are multi-device groups managed by the UniFi Play app.  All devices
    in a zone play in sync.  Any device in the zone can broadcast its physical
    input to the rest of the zone via wideband mode.  Zone state arrives via
    the MQTT 'groups' event, pushed to every connected device whenever
    membership or wideband settings change.
    """

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = (
        MediaPlayerEntityFeature.SELECT_SOURCE
        | MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.VOLUME_MUTE
    )

    def __init__(self, coordinator: UnifiPlayCoordinator, group_id: str) -> None:
        super().__init__(coordinator)
        self._group_id = group_id
        self._attr_unique_id = f"unifi_play_zone_{group_id}"

    @property
    def group_id(self) -> str:
        return self._group_id

    @property
    def _group(self) -> UnifiPlayGroupState | None:
        return self.coordinator.groups.get(self._group_id)

    @property
    def available(self) -> bool:
        """Available while the zone exists and something in it answers.

        Not "every member is connected": a zone with one speaker down still
        reports real state - who is in it, what it is playing from - and
        hiding that would leave the user with nothing to look at while they
        work out which room is off. Commands are the part that has to be all
        or nothing, and they refuse individually, naming the speaker.

        With nothing connected there is no state at all, only the values the
        zone was last seen with, which is exactly what unavailable is for.
        """
        gs = self._group
        if gs is None:
            return False
        return any(
            client is not None and client.is_connected
            for _dev_id, _state, client in self.coordinator.get_zone_members(
                self._group_id
            )
        )

    @property
    def device_info(self) -> DeviceInfo:
        gs = self._group
        if gs is None:
            return DeviceInfo(identifiers={(DOMAIN, f"zone_{self._group_id}")})
        info = DeviceInfo(
            identifiers={(DOMAIN, f"zone_{gs.group_id}")},
            name=gs.name,
            manufacturer="Ubiquiti",
            model="UniFi Play Zone",
        )
        entry = self.coordinator.config_entry
        if gs.host_mac and entry is not None:
            # via_device_id on 2026.9; via_device on the 2025.8 floor.
            # The helper picks; neither key is in both TypedDicts.
            for key, value in via_device_link(
                self.hass,
                entry.entry_id,
                (DOMAIN, mac_normalise(gs.host_mac)),
            ).items():
                info[key] = value  # type: ignore[literal-required]
        return info

    @property
    def state(self) -> MediaPlayerState | None:
        gs = self._group
        if gs is None:
            return None
        return MediaPlayerState.PLAYING if gs.wb_enable else MediaPlayerState.IDLE

    @property
    def volume_level(self) -> float | None:
        if self._group is None:
            return None
        host_state = self.coordinator.get_zone_host_state(self._group_id)
        return host_state.volume / 100.0 if host_state else None

    @property
    def is_volume_muted(self) -> bool | None:
        if self._group is None:
            return None
        host_state = self.coordinator.get_zone_host_state(self._group_id)
        return host_state.muted if host_state else None

    def _require_members(self) -> list[tuple[UnifiPlayDeviceState, Any]]:
        """Every member of this zone with a live connection, or refuse.

        A zone command that reaches three speakers out of four and reports
        success leaves one room at the old volume, or still announcing, with
        nothing to say so. Refusing is worse for exactly one case - wanting
        to turn down the speakers that *are* reachable - and better for every
        other, because the failure is visible instead of audible.
        """
        gs = self._group
        if gs is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="zone_not_found"
            )
        members = self.coordinator.get_zone_members(self._group_id)
        if not members:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="zone_has_no_members"
            )
        offline = [
            state.device_name
            for _, state, client in members
            if client is None or not client.is_connected
        ]
        if offline:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="zone_members_offline",
                translation_placeholders={"devices": ", ".join(offline)},
            )
        return [(state, client) for _, state, client in members if client is not None]

    async def async_set_volume_level(self, volume: float) -> None:
        level = int(volume * 100)
        for _state, client in self._require_members():
            client.set_volume(level)

    async def async_mute_volume(self, mute: bool) -> None:
        """Software mute across the zone.

        The optimistic flags are set only after every member has been proved
        reachable: setting them first left a speaker showing muted while
        still playing, because the command that would have muted it was
        never sent.
        """
        members = self._require_members()
        for state, client in members:
            if mute:
                if state.volume > 0:
                    state.mute_restore = state.volume
                state.muted = True
                # Until the speaker reports volume 0 back, ignore any info
                # event still carrying the pre-mute level.
                state.mute_confirmed = False
                client.set_mute(True)
            else:
                state.muted = False
                client.set_mute(False, restore_volume=state.mute_restore or 20)
        self.async_write_ha_state()

    async def async_volume_up(self) -> None:
        members = self._require_members()
        host_state = self.coordinator.get_zone_host_state(self._group_id)
        if host_state is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="zone_host_unknown"
            )
        new_vol = min(host_state.volume + 5, host_state.vol_limit)
        for _state, client in members:
            client.set_volume(new_vol)

    async def async_volume_down(self) -> None:
        members = self._require_members()
        host_state = self.coordinator.get_zone_host_state(self._group_id)
        if host_state is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="zone_host_unknown"
            )
        new_vol = max(host_state.volume - 5, 0)
        for _state, client in members:
            client.set_volume(new_vol)

    def _member_states(self) -> list[UnifiPlayDeviceState]:
        """Device states for the speakers in this zone."""
        gs = self._group
        if gs is None:
            return []
        macs = {mac_normalise(d["mac"]) for d in gs.dev_info if d.get("mac")}
        return [
            state
            for state in (self.coordinator.data or {}).values()
            if mac_normalise(state.mac) in macs
        ]

    def _source_platform(self) -> str:
        """Platform of the speaker currently broadcasting, else the host's.

        Input names are platform-specific — "eARC" is "speakers" on an Audio
        Port but "spdif" on a PowerAmp — so wb_input can only be read against
        whichever device is acting as the source.
        """
        gs = self._group
        if gs is None:
            return ""
        target = mac_normalise(gs.wb_device or gs.host_mac)
        for state in self._member_states():
            if mac_normalise(state.mac) == target:
                return state.platform
        return ""

    @property
    def source(self) -> str | None:
        gs = self._group
        if gs is None:
            return None
        if not gs.wb_enable:
            return WB_STREAMING_LABEL
        return broadcast_input_label(self._source_platform(), gs.wb_input)

    @property
    def source_list(self) -> list[str]:
        # The union across members: any speaker in the zone can be the source,
        # and a mixed zone offers whatever its hardware collectively supports.
        labels = [WB_STREAMING_LABEL]
        for state in self._member_states():
            for label in broadcast_input_labels(state.platform).values():
                if label not in labels:
                    labels.append(label)
        return labels

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        gs = self._group
        if gs is None:
            return {}
        member_macs = {mac_normalise(d["mac"]) for d in gs.dev_info if d.get("mac")}
        group_members = [
            state.mac
            for state in (self.coordinator.data or {}).values()
            if mac_normalise(state.mac) in member_macs
        ]
        return {
            "group_id": gs.group_id,
            "group_members": group_members,
            "wb_enable": gs.wb_enable,
            "wb_input": gs.wb_input,
            "wb_device": gs.wb_device,
            "host_mac": gs.host_mac,
            "dev_count": gs.dev_count,
            "broadcasting_mode": gs.broadcasting_mode,
        }

    async def async_select_source(self, source: str) -> None:
        """Switch the zone between streaming and a broadcast wired input.

        Two writes to two different places, and the order matters: the zone
        document goes to every required speaker first, and only once that has
        succeeded does the input switch go to the speaker that will actually
        broadcast. Doing it the other way round leaves a speaker on an input
        nothing is listening to when the zone write is refused.
        """
        gs = self._group
        if gs is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="zone_not_found"
            )

        if source == WB_STREAMING_LABEL:
            self.coordinator.zones.clear_broadcast_source(gs.group_id)
            return

        # source_list is the union across members, so a label valid for one
        # speaker can be absent on the one currently broadcasting. Refuse
        # rather than fall through to "": that reads as Streaming and would
        # silently drop the whole zone off its wired source instead of
        # rejecting a request the hardware cannot serve.
        device_val = source_value(self._source_platform(), source)
        if not device_val:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="zone_source_not_on_device",
                translation_placeholders={"source": source, "zone": gs.name},
            )
        # Preserve whichever member was set as the broadcast source; fall
        # back to the host only if none has been chosen yet.
        self.coordinator.zones.set_broadcast_source(
            gs.group_id,
            source_mac=gs.wb_device or gs.host_mac,
            wb_input=device_val,
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()
