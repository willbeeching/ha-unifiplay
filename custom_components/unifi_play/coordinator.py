"""Data coordinator for UniFi Play devices."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    UnifiPlayApi,
    UnifiPlayApiError,
    UnifiPlayAuthError,
    UnifiPlayForbiddenError,
)
from .const import (
    DOMAIN,
    EVENT_ZONE_CREATED,
    EVENT_ZONE_DELETED,
    EVENT_ZONE_MEMBER_CHANGED,
    EVENT_ZONE_RENAMED,
    HOST_ELECTION_REREAD_DELAYS,
    parse_firmware_version,
)
from .discovery import async_resolve_direct
from .mqtt_client import MqttCertificateRejected, UnifiPlayMqttClient
from .zone_writer import ZoneWriter

_LOGGER = logging.getLogger(__name__)

#: A config entry whose ``runtime_data`` is this integration's coordinator.
#:
#: Declared here rather than in ``__init__`` so every module can annotate an
#: entry without importing the package root, which imports every platform.
type UnifiPlayConfigEntry = ConfigEntry[UnifiPlayCoordinator]


def _norm_mac(mac: str) -> str:
    """Normalise a MAC address to uppercase hex without delimiters."""
    return mac.upper().replace(":", "")


def _invert(mac: str) -> tuple[int, ...]:
    """Sort key that makes ``max()`` prefer the LOWEST MAC.

    The merge picks the best candidate with ``max`` on (timestamp, this), so
    the tie-break has to run the other way round. Inverting the code points
    is enough: MACs here are normalised hex of equal length.
    """
    return tuple(-ord(ch) for ch in mac)


def _member_macs(gs: UnifiPlayGroupState) -> set[str]:
    """The normalised MACs of a zone's members."""
    return {_norm_mac(d["mac"]) for d in gs.dev_info if d.get("mac")}


def _zone_signature(gs: UnifiPlayGroupState) -> tuple[Any, ...]:
    """A zone reduced to what the integration actually acts on.

    Order-insensitive on members, because a device is free to list them in
    any order and a reordered copy is not a change. ``host`` is excluded on
    purpose: it is firmware-owned and legitimately differs between devices
    mid-election, so including it would report a conflict every time a zone
    changed hands.
    """
    return (
        gs.name,
        tuple(sorted(_member_macs(gs))),
        gs.group_index,
        gs.broadcasting_mode,
        gs.wb_enable,
        _norm_mac(gs.wb_device),
        gs.wb_input,
    )


@dataclass
class UnifiPlayGroupState:
    """State for a single zone/group, populated from the MQTT 'groups' event.

    The 'groups' event is pushed simultaneously to all connected devices
    whenever zone membership changes in the UniFi Play app.
    """

    group_id: str
    name: str
    dev_count: int
    group_index: int
    broadcasting_mode: str
    wb_enable: bool
    wb_device: str  # MAC of the source Port when wideband is active
    wb_input: str  # "lineIn" | "spdif" | "usb" | ""
    dev_info: list[dict[str, Any]] = field(default_factory=list)
    host_mac: str = ""  # MAC of the device with host=True in dev_info
    # Always 0. set_groups accepts a per-group timestamp, but the groups event
    # carries one only at the top level of the body - never inside a group - so
    # nothing ever populates this. The merge tie-break below that compares it
    # is therefore vestigial: it compares zeroes and falls through to the host
    # preference. Kept as a field only to document the dead end.
    timestamp: int = 0

    @classmethod
    def from_mqtt(cls, group: dict[str, Any]) -> UnifiPlayGroupState:
        host_mac = next(
            (d["mac"] for d in group.get("dev_info", []) if d.get("host")), ""
        )
        return cls(
            group_id=group["group_id"],
            name=group.get("name", ""),
            dev_count=group.get("dev_count", 0),
            group_index=group.get("group_index", 0),
            broadcasting_mode=group.get("broadcasting_mode", "zone_only"),
            wb_enable=group.get("wb_enable", False),
            wb_device=group.get("wb_device", ""),
            wb_input=group.get("wb_input", ""),
            dev_info=group.get("dev_info", []),
            host_mac=host_mac,
            timestamp=group.get("timestamp", 0) or 0,
        )


# Device state arrives via MQTT push, so this poll exists only to pick up
# devices adopted after setup, and to retry MQTT for devices that had no IP
# (or an unreachable broker) on an earlier pass.
DISCOVERY_INTERVAL = timedelta(minutes=5)

#: Consecutive successful discovery passes that must omit a device before
#: it is treated as gone enough to delete. One miss is a quiet sweep —
#: Audio Port never answers UDP — so a single absence must not unlock
#: removal. Two is a speaker the authoritative source has stopped listing.
STALE_AFTER_ABSENCES = 2

#: How long to wait after a CONNACK before the first burst of requests.
#:
#: A speaker that has only just accepted the connection drops requests that
#: arrive immediately behind the CONNACK, leaving the device with no state
#: until something else happens to ask again. Named rather than inlined so
#: the test suite can collapse it: the wait is real on hardware and pure dead
#: time against a fake transport.
POST_CONNECT_SETTLE = 0.5


class UnifiPlayDeviceState:
    """State container for a single Play device, updated via MQTT events."""

    def __init__(self, device_data: dict[str, Any]) -> None:
        self.device_id: str = device_data["id"]
        self.name: str = device_data.get("name", "UniFi Play")
        self.mac: str = device_data.get("mac", "")
        self.platform: str = device_data.get("platform", "")
        self.firmware: str = device_data.get("firmware", "")
        self.ip: str = device_data.get("ip", "")
        self.online: bool = False
        self.volume: int = 0
        self.source: str = ""
        self.out: str = ""
        self.stream_playing: bool = False
        self.muted: bool = False
        # The speaker has no real mute channel - the MQTT client maps mute to
        # set_volume(0) - so the pre-mute level must be remembered here or
        # unmute has nothing to restore to.
        self.mute_restore: int = 0
        # Set once the device confirms volume actually reached zero. Info
        # events already in flight when we mute still carry the pre-mute
        # volume, and treating those as "volume rose above zero" would clear
        # the mute flag we just set.
        self.mute_confirmed: bool = False
        self.device_name: str = self.name
        self.upgrade_status: str = ""
        self.balance: int = 0
        self.loudness: bool = False
        self.eq_enable: bool = False
        self.vol_limit: int = 100
        self.locked: bool = False
        self.subwoofer: bool = False
        self.screen_brightness: int = 100
        self.led_brightness: int = 100
        self.screen_color: str = "0000FF"
        self.led_color: str = "0000FF"
        self.channels: int = 0
        self.persistent_dashboard: bool = False
        self.eq_preset: str = "custom"
        # 10-band graphic EQ, keyed by the device's own band labels
        # ("32" ... "16k"). Baseline is 0.01, not 0, in the device's own data.
        self.eq_table: dict[str, float] = {}
        self.eq_custom_presets: list[Any] = []
        self.eq_active_preset: str = ""
        self.sub_crossover: int = 85
        self.sub_level: int = 3
        self.sub_phase: int = 0
        self.now_playing_song: str = ""
        self.now_playing_artist: str = ""
        self.now_playing_album: str = ""
        self.now_playing_length: int = 0
        self.now_playing_current: int = 0
        # When the speaker last reported the play position. Home Assistant
        # extrapolates the playhead from position + (now - this timestamp);
        # without it the progress bar freezes at the last reported value.
        self.now_playing_current_at: datetime | None = None
        self.now_playing_cover: str = ""
        # The streaming source tells us whether it can skip; the official app
        # greys its buttons out accordingly (#4).
        self.can_prev: bool = False
        self.can_next: bool = False
        # Which streaming service is feeding the amp (spotify, airplay, ...) -
        # reported in every info event while streaming.
        self.service: str = ""
        self.space: str = ""
        self.tz: str = ""
        self.soundtrack_paired: str = ""
        # Zone membership fields populated from 'info' events.
        # hosting_group is only present on the zone host device.
        # sync_devices is only present on zone members (not the host, not standalone).
        # wb_broadcasting is true on the host while wideband mode is active.
        self.hosting_group: str = ""
        self.sync_devices: bool = False
        self.wb_broadcasting: bool = False
        self.playlist: str = ""
        self.uptime: int = 0
        self.link_quality: int = 0
        # Feature-level state, populated by request_features() responses.
        self.alarms: list[Any] = []
        self.quiet_hours: list[Any] = []
        self.ann_files: list[Any] = []
        self.ann_schedule: list[Any] = []
        self.ann_chime: str = ""
        self.ann_volume: int = 0
        self.voice_enhancement: bool = False
        self.streaming_timeout: int = 0
        # Present in info events only while an alert (alarm test, announcement)
        # is sounding: the temporary playback level, with self.volume left at
        # the user's real setting. Absence means nothing is being announced.
        self.temp_volume: int | None = None
        # Announcement playback state, also info-event-only and absent when
        # idle: whether one is sounding, its kind, length and schedule name.
        self.announcing: bool = False
        self.announcing_type: str = ""
        self.announce_length: int = 0
        self.announce_name: str = ""
        # Why the device sent this info event, e.g. "set_play". Not always set.
        self.info_action: str = ""

    def update_from_info(self, body: dict[str, Any]) -> None:
        """Update state from an MQTT 'info' event."""
        if "volume" in body:
            self.volume = body["volume"]
            # Mute is software-only (volume 0), so the device never reports a
            # muted flag for it. Volume rising above zero IS unmute, no
            # matter where it came from - app, dashboard slider, or dial.
            # Only once the mute has been confirmed, though: an info event
            # sent before our set_volume(0) landed still reports the old
            # volume, and acting on it would drop the flag immediately.
            if self.volume == 0:
                self.mute_confirmed = True
            elif self.mute_confirmed:
                self.muted = False
        if "source" in body:
            self.source = body["source"]
        if "out" in body:
            self.out = body["out"]
        if "stream_playing" in body:
            self.stream_playing = body["stream_playing"]
        if "muted" in body:
            # The device only knows its hardware mute channel. Our mute is
            # software (volume 0), which the device reports as muted:false -
            # honouring that would stomp the flag the moment it was set. So
            # only a positive assertion is trusted; clearing happens locally
            # in async_mute_volume(False) or when volume rises above zero.
            if body["muted"]:
                self.muted = True
        if "deviceName" in body:
            self.device_name = body["deviceName"]
        if "upgrade_status" in body:
            self.upgrade_status = body["upgrade_status"]
        if "balance" in body:
            self.balance = body["balance"]
        if "loudness" in body:
            self.loudness = body["loudness"]
        if "eq_enable" in body:
            self.eq_enable = body["eq_enable"]
        if "vol_limit" in body:
            self.vol_limit = body["vol_limit"]
        if "locked" in body:
            self.locked = body["locked"]
        if "subwoofer" in body:
            self.subwoofer = body["subwoofer"]
        if "screen_brightness" in body:
            self.screen_brightness = body["screen_brightness"]
        if "led_brightness" in body:
            self.led_brightness = body["led_brightness"]
        if "screen_color" in body:
            self.screen_color = body["screen_color"]
        if "led_color" in body:
            self.led_color = body["led_color"]
        if "channels" in body:
            self.channels = body["channels"]
        if "persistent_dashboard" in body:
            self.persistent_dashboard = body["persistent_dashboard"]
        # These appear only while an alert is sounding and vanish afterwards,
        # so absence has to CLEAR them rather than being ignored.
        self.temp_volume = body.get("temp_volume")
        self.announcing = bool(body.get("announcing", False))
        self.announcing_type = body.get("announcing_type", "")
        self.announce_length = body.get("announce_length", 0) or 0
        self.announce_name = body.get("test_schedule_announcement_name", "")
        if "info_action" in body:
            self.info_action = body["info_action"]
        if "service" in body:
            self.service = body["service"]
        if "space" in body:
            self.space = body["space"]
        if "tz" in body:
            self.tz = body["tz"]
        if "soundtrack_paired" in body:
            self.soundtrack_paired = body["soundtrack_paired"]
        if "hosting_group" in body:
            self.hosting_group = body["hosting_group"]
        if "sync_devices" in body:
            self.sync_devices = bool(body["sync_devices"])
        if "wb_broadcasting" in body:
            self.wb_broadcasting = bool(body["wb_broadcasting"])

    def update_from_equalizer(self, body: dict[str, Any]) -> None:
        """Update EQ state from an MQTT 'equalizer' event."""
        if "active_profile" in body:
            self.eq_preset = body["active_profile"]
        if "eq_enable" in body:
            self.eq_enable = body["eq_enable"]
        if isinstance(body.get("active_table"), dict):
            self.eq_table = dict(body["active_table"])
        if isinstance(body.get("custom_presets"), list):
            new_presets = body["custom_presets"]
            # A populated -> empty transition is what a device-side preset
            # wipe looks like from here. One has been seen in the field
            # (PowerAmp fw 1.0.38: presets present for days, gone after an
            # unattended reboot) while a controlled graceful restart preserved
            # them - cause undetermined, so make the next occurrence carry a
            # timestamp instead of being noticed weeks later. See docs/api.md,
            # "Graphic EQ".
            if self.eq_custom_presets and not new_presets:
                # Names are read defensively: this is a log line, and
                # _handle_event has no guard around it, so an entry that is
                # not a dict must not be able to take the state update down
                # with it. Same reason select.py checks before reading "name".
                names = [
                    p.get("name") for p in self.eq_custom_presets if isinstance(p, dict)
                ]
                _LOGGER.warning(
                    "%s: custom EQ presets are now empty (device previously "
                    "reported %s). Expected if you just deleted the last one "
                    "in the Play app; otherwise the device has lost them",
                    self.name,
                    names,
                )
            self.eq_custom_presets = new_presets
        # Which saved preset is loaded, "" when a built-in profile is active.
        if "active_preset" in body:
            self.eq_active_preset = body["active_preset"]

    def update_from_sub_audio(self, body: dict[str, Any]) -> None:
        """Update sub audio state from an MQTT 'sub_audio' event."""
        if "crossover" in body:
            self.sub_crossover = body["crossover"]
        if "level" in body:
            self.sub_level = body["level"]
        if "phase" in body:
            self.sub_phase = body["phase"]
        if "subwoofer" in body:
            self.subwoofer = body["subwoofer"]

    def update_from_metadata(self, body: dict[str, Any]) -> None:
        """Update now-playing state from an MQTT 'metadata' event."""
        if "title" in body:
            self.now_playing_song = body["title"]
        elif "song" in body:
            self.now_playing_song = body["song"]
        if "artist" in body:
            self.now_playing_artist = body["artist"]
        if "album" in body:
            self.now_playing_album = body["album"]
        if "length" in body:
            self.now_playing_length = body["length"]
        if "current" in body:
            self.now_playing_current = body["current"]
            self.now_playing_current_at = dt_util.utcnow()
        if "cover_path" in body:
            self.now_playing_cover = body["cover_path"]
        if "prev" in body:
            self.can_prev = bool(body["prev"])
        if "next" in body:
            self.can_next = bool(body["next"])
        if "playlist" in body:
            self.playlist = body["playlist"]

    def update_from_online(self, body: dict[str, Any]) -> None:
        """Update online status from an MQTT 'online' event."""
        self.online = body.get("status", 0) == 1

    def update_from_alarms(self, body: list[Any] | dict[str, Any]) -> None:
        """Update the alarm list from an MQTT 'alarms' event (a bare list)."""
        if isinstance(body, list):
            self.alarms = body

    def update_from_quiet_hours(self, body: list[Any] | dict[str, Any]) -> None:
        """Update quiet hours from an MQTT 'quiet_hours' event (a bare list)."""
        if isinstance(body, list):
            self.quiet_hours = body

    def update_from_announcement(self, body: dict[str, Any]) -> None:
        """Update announcement files/schedule from an 'announcement' event."""
        if "files" in body:
            self.ann_files = body["files"] or []
        if "schedule" in body:
            self.ann_schedule = body["schedule"] or []

    def update_from_announce_chime(self, body: dict[str, Any]) -> None:
        """Update the announcement chime from an 'announce_chime' event."""
        if "chime" in body:
            self.ann_chime = body["chime"]

    def update_from_voice_enhancement(self, body: dict[str, Any]) -> None:
        """Update voice enhancement from a 'voice_enhancement' event."""
        if "enable" in body:
            self.voice_enhancement = bool(body["enable"])

    def update_from_streaming_timeout(self, body: dict[str, Any]) -> None:
        """Update streaming timeout from a 'streaming_timeout' event."""
        if "second" in body:
            self.streaming_timeout = body["second"]

    def update_from_announcement_vol(self, body: dict[str, Any]) -> None:
        """Update announcement volume from an 'announcement_vol' event."""
        if "value" in body:
            self.ann_volume = body["value"]

    def update_from_extra_info(self, body: dict[str, Any]) -> None:
        """Update device identity from an MQTT 'extra_info' event.

        In direct mode a device identified through its MQTT topics is only
        known by its topic root (UPL-DEVICE for a Port) with no firmware
        version; extra_info carries the real platform and version.
        """
        if body.get("platform"):
            self.platform = body["platform"]
        if body.get("version"):
            self.firmware = parse_firmware_version(body["version"])
        if "uptime" in body:
            self.uptime = body["uptime"]
        if "link_quality" in body:
            self.link_quality = body["link_quality"]


class UnifiPlayCoordinator(DataUpdateCoordinator[dict[str, UnifiPlayDeviceState]]):
    """Coordinates REST discovery + MQTT real-time updates for all devices."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        api: UnifiPlayApi | None,
        manual_hosts: list[str] | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name="UniFi Play",
            update_interval=DISCOVERY_INTERVAL,
        )
        self.api = api
        self.manual_hosts = manual_hosts or []
        self._mqtt_clients: dict[str, UnifiPlayMqttClient] = {}
        # Why MQTT is down, keyed by device_id. Cleared on a successful
        # connect. The connectivity binary sensor stays available and
        # reports this so a dead broker is "Disconnected", not a page of
        # unavailable entities with no device-level signal (#20).
        self._mqtt_offline_reason: dict[str, str] = {}
        self._device_states: dict[str, UnifiPlayDeviceState] = {}
        # Per-device group cache: device_id → {group_id: GroupState}.
        # The merged view in `groups` is rebuilt from this on every update so
        # that a groups event from device B (which lists only B's zones) cannot
        # wipe zones hosted by device A.
        self._device_groups: dict[str, dict[str, UnifiPlayGroupState]] = {}
        # Tracks which devices have completed their initial groups sync so we
        # can suppress zone_created events that fire for every pre-existing
        # zone on first MQTT connect (startup / reload).
        self._device_groups_initialized: set[str] = set()
        # Devices whose address changed on the last discovery pass, so the
        # existing client is dialled at somewhere nothing is listening.
        self._address_changed: set[str] = set()
        # Zones currently reported differently by different speakers. Held so
        # the disagreement is logged once on the way in and once on the way
        # out, rather than on every event for as long as it lasts.
        self._conflicted_zones: set[str] = set()
        self.groups: dict[str, UnifiPlayGroupState] = {}
        # The last zone list we successfully submitted. coordinator.groups
        # is not updated until a speaker reports it back, so a rename
        # followed by an index change would otherwise rebuild the second
        # document from the pre-rename list and undo the first write.
        # Mutations serialise against this snapshot until readback confirms
        # it or the speakers converge on something else.
        self._pending_groups: dict[str, UnifiPlayGroupState] | None = None
        self._zone_writer = ZoneWriter(self)
        # Cancel handles for the post-write zone re-reads, so shutdown can
        # take them down rather than leaving them to fire into a coordinator
        # that no longer has any clients.
        self._host_reread_cancels: list[CALLBACK_TYPE] = []
        # Consecutive authoritative absences, keyed by device_id. Reset
        # whenever a successful poll lists the speaker again.
        self._discovery_misses: dict[str, int] = {}

    async def _async_update_data(self) -> dict[str, UnifiPlayDeviceState]:
        """Fetch the device list and return current state dict.

        Console mode asks the console's Apollo REST API; direct mode probes
        the network itself (UDP broadcast plus unicast to any manual hosts).
        Devices seen once are kept even if a later scan misses them — MQTT
        remains the source of truth for online state — but consecutive
        authoritative absences eventually make a deliberate delete possible.
        """
        if self.api is not None:
            try:
                devices = await self.api.get_devices()
            except (UnifiPlayAuthError, UnifiPlayForbiddenError) as err:
                # A key that was accepted at setup and is now refused has been
                # revoked or rotated. Home Assistant turns this into a repair
                # notification with a Reconfigure button, which is the only
                # action that fixes it; UpdateFailed would retry the same dead
                # key every five minutes forever and say nothing useful.
                raise ConfigEntryAuthFailed(str(err)) from err
            except UnifiPlayApiError as err:
                # Everything else keeps the devices already known. MQTT is the
                # source of truth for state; this poll only discovers devices,
                # so a console outage must not empty the integration.
                raise UpdateFailed(f"Error fetching devices: {err}") from err
        else:
            # Manual hosts already tracked as devices are excluded from the
            # MQTT identification probe — that probe is for learning a MAC
            # we do not have. A retained speaker whose client has stood
            # down is recovered below by calling _ensure_mqtt directly;
            # probing it again would block the poll on an info timeout
            # for a device we already know.
            known_ips = {s.ip for s in self._device_states.values() if s.ip}
            try:
                devices = await async_resolve_direct(
                    manual_hosts=self.manual_hosts, known_ips=known_ips
                )
            except OSError as err:
                raise UpdateFailed(f"Discovery socket error: {err}") from err

        for dev in devices:
            dev_id = dev["id"]
            if dev_id not in self._device_states:
                state = UnifiPlayDeviceState(dev)
                self._device_states[dev_id] = state
                _LOGGER.info(
                    "Discovered UniFi Play device: %s (%s) at %s",
                    state.name,
                    state.platform,
                    state.ip or "unknown IP",
                )
            else:
                state = self._device_states[dev_id]
                if dev.get("ip") and dev["ip"] != state.ip:
                    # A speaker that moved has to be redialled: the existing
                    # client still believes it is connected to an address
                    # nothing is listening on any more, so nothing about the
                    # connection state says anything is wrong.
                    _LOGGER.info(
                        "%s moved from %s to %s; reconnecting",
                        state.device_name or state.name,
                        state.ip or "an unknown address",
                        dev["ip"],
                    )
                    self._address_changed.add(dev_id)
                    state.ip = dev["ip"]
                if dev.get("firmware"):
                    state.firmware = dev["firmware"]
            ip = dev.get("ip", "")
            mac = dev.get("mac", "")
            if ip and mac:
                await self._ensure_mqtt(dev_id, ip, mac)

        # A retained speaker the sweep did not re-list still needs its
        # client rebuilt once retries are exhausted. Audio Port never
        # appears in a UDP result, so this is the path that reaches
        # _ensure_mqtt after a stand-down even if known_ips skipped it.
        for state in list(self._device_states.values()):
            if state.ip and state.mac:
                await self._ensure_mqtt(state.device_id, state.ip, state.mac)

        self._record_discovery_absences(devices)
        return self._device_states

    def _mqtt_is_held(self, device_id: str) -> bool:
        """True while a client is connected or still working on coming back."""
        client = self._mqtt_clients.get(device_id)
        return client is not None and (client.is_connected or client.is_retrying)

    def _record_discovery_absences(self, devices: list[dict[str, Any]]) -> None:
        """Count consecutive successful polls that omitted each speaker.

        A console outage never reaches here — UpdateFailed is raised first —
        so a miss is an authoritative list that no longer contains the
        device, not a failed scan. Direct mode also treats a live MQTT
        session as present: Audio Port never answers UDP, and skipping it
        from the MQTT fallback is the healthy case, not an absence.
        """
        seen = {dev["id"] for dev in devices}
        if self.api is None:
            for device_id in self._mqtt_clients:
                if self._mqtt_is_held(device_id):
                    seen.add(device_id)
        for device_id in self._device_states:
            if device_id in seen:
                self._discovery_misses[device_id] = 0
            else:
                self._discovery_misses[device_id] = (
                    self._discovery_misses.get(device_id, 0) + 1
                )

    def device_is_current(self, device_id: str) -> bool:
        """True until the authoritative source has omitted this device twice.

        One missed scan is a quiet sweep, not a removal. Two consecutive
        successful polls that leave it out is enough for a deliberate
        delete to be accepted; the speaker is still retained until then.
        """
        return self._discovery_misses.get(device_id, 0) < STALE_AFTER_ABSENCES

    async def async_forget_device(self, device_id: str) -> None:
        """Drop a retained speaker the user has just been allowed to delete.

        Leaving it in ``_device_states`` would recreate the registry device
        from ``device_info`` on the next state write, which is the bounce
        ``async_remove_config_entry_device`` exists to prevent.
        """
        self._device_states.pop(device_id, None)
        self._discovery_misses.pop(device_id, None)
        self._device_groups.pop(device_id, None)
        self._device_groups_initialized.discard(device_id)
        self._mqtt_offline_reason.pop(device_id, None)
        self._address_changed.discard(device_id)
        client = self._mqtt_clients.pop(device_id, None)
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                _LOGGER.debug(
                    "Cleanup of forgotten MQTT client for %s failed", device_id
                )
        # Do not notify listeners: entities for this device are still
        # registered and would KeyError on a missing state. Home Assistant
        # removes them as part of the delete this was called from.

    async def _ensure_mqtt(self, device_id: str, ip: str, mac: str) -> None:
        """Connect the device's MQTT client, replacing one that has dropped.

        paho's ``loop()`` does not reconnect on its own - only
        ``loop_forever()`` does - so a connection that succeeds and later
        drops stays dead for the life of the config entry. Because the client
        object remains in ``_mqtt_clients``, a guard that only asked whether
        the device was absent from that dict never retried it: the speaker is
        reachable again, the coordinator is polling happily, and nothing
        reconnects. Asking the connection rather than the dict makes this poll
        the reconnect loop the module docstring always described.

        A console outage is enough to trigger it - the brokers live on the
        speakers, but whatever takes the console away usually takes the
        speakers with it, and only the console comes back on its own.

        A client that is still working on getting itself back is left alone.
        The client reconnects on its own thread with bounded backoff now, and
        tearing it down mid-backoff would both throw the backoff away and put
        two reconnect loops on one speaker.
        """
        moved = device_id in self._address_changed
        existing = self._mqtt_clients.get(device_id)
        if existing is not None:
            if existing.is_connected and not moved:
                return
            if existing.is_retrying and not moved:
                _LOGGER.debug("MQTT to %s is reconnecting on its own; leaving it", ip)
                return
            if moved:
                self._address_changed.discard(device_id)
            else:
                _LOGGER.info("MQTT connection to %s dropped; reconnecting", ip)
                self._mqtt_offline_reason.setdefault(device_id, "disconnected")
            try:
                await existing.disconnect()
            except Exception:  # noqa: BLE001 - a dead client may fail any way
                _LOGGER.debug("Cleanup of dropped MQTT client for %s failed", ip)
            self._mqtt_clients.pop(device_id, None)
        await self._start_mqtt(device_id, ip, mac)

    async def _start_mqtt(self, device_id: str, ip: str, mac: str) -> None:
        """Start an MQTT connection for a device."""

        def _schedule_event(
            event_name: str, header: dict[str, Any], body: dict[str, Any]
        ) -> None:
            self.hass.loop.call_soon_threadsafe(
                self._handle_event, device_id, event_name, header, body
            )

        def _schedule_connection() -> None:
            # paho callbacks run inside loop() on an executor thread.
            self.hass.loop.call_soon_threadsafe(self._async_mqtt_connection_changed)

        client = UnifiPlayMqttClient(
            ip, mac, on_event=_schedule_event, on_connection=_schedule_connection
        )
        self._mqtt_clients[device_id] = client
        try:
            await client.connect()
            await asyncio.sleep(POST_CONNECT_SETTLE)
            client.request_info()
            client.request_extra_info()
            client.request_metadata()
            client.request_equalizer()
            client.request_sub_audio()
            client.request_features()
            client.request_groups()
        except MqttCertificateRejected as err:
            _LOGGER.warning("%s", err)
            self._mqtt_offline_reason[device_id] = "certificate_rejected"
            self._async_create_cert_issue(device_id, ip, mac)
            await self._async_drop_failed_mqtt(client, device_id, ip)
        except Exception:
            state = self._device_states.get(device_id)
            platform = state.platform if state else "unknown"
            _LOGGER.exception(
                "Failed to connect MQTT to %s (%s, %s), will retry", ip, mac, platform
            )
            self._mqtt_offline_reason[device_id] = "unreachable"
            self._async_delete_cert_issue(mac)
            await self._async_drop_failed_mqtt(client, device_id, ip)
        else:
            self._mqtt_offline_reason.pop(device_id, None)
            self._async_delete_cert_issue(mac)
            self.async_set_updated_data(self._device_states)

    async def _async_drop_failed_mqtt(
        self, client: UnifiPlayMqttClient, device_id: str, ip: str
    ) -> None:
        """Drop a half-built client so the next discovery poll retries.

        Leaving it in place would strand the device without state for as
        long as the config entry lives.
        """
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Cleanup of failed MQTT client for %s failed", ip)
        self._mqtt_clients.pop(device_id, None)
        self.async_set_updated_data(self._device_states)

    @callback
    def _async_mqtt_connection_changed(self) -> None:
        """Refresh entities when a live MQTT session drops or returns."""
        for device_id, client in self._mqtt_clients.items():
            state = self._device_states.get(device_id)
            if client.is_connected:
                self._mqtt_offline_reason.pop(device_id, None)
                if state is not None:
                    self._async_delete_cert_issue(state.mac)
            else:
                self._mqtt_offline_reason.setdefault(device_id, "disconnected")
        self.async_set_updated_data(self._device_states)

    def mqtt_offline_reason(self, device_id: str) -> str | None:
        """Why MQTT is down for this device, or None while it is connected."""
        client = self._mqtt_clients.get(device_id)
        if client is not None and client.is_connected:
            return None
        return self._mqtt_offline_reason.get(device_id)

    def _cert_issue_id(self, mac: str) -> str:
        return f"mqtt_certificate_rejected_{_norm_mac(mac)}"

    def _async_create_cert_issue(self, device_id: str, ip: str, mac: str) -> None:
        state = self._device_states.get(device_id)
        name = (state.device_name or state.name) if state else mac
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._cert_issue_id(mac),
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="mqtt_certificate_rejected",
            translation_placeholders={"name": name, "ip": ip},
            learn_more_url="https://github.com/willbeeching/ha-unifiplay/issues/20",
        )

    def _async_delete_cert_issue(self, mac: str) -> None:
        ir.async_delete_issue(self.hass, DOMAIN, self._cert_issue_id(mac))

    @callback
    def _handle_event(
        self,
        device_id: str,
        event_name: str,
        header: dict[str, Any],
        body: dict[str, Any],
    ) -> None:
        """Process an incoming MQTT event and update state."""
        state = self._device_states.get(device_id)
        if state is None:
            return
        if event_name == "info":
            state.update_from_info(body)
        elif event_name == "metadata":
            state.update_from_metadata(body)
        elif event_name == "online":
            state.update_from_online(body)
        elif event_name == "equalizer":
            state.update_from_equalizer(body)
        elif event_name == "sub_audio":
            state.update_from_sub_audio(body)
        elif event_name == "extra_info":
            state.update_from_extra_info(body)
        elif event_name == "alarms":
            state.update_from_alarms(body)
        elif event_name == "quiet_hours":
            state.update_from_quiet_hours(body)
        elif event_name == "announcement":
            state.update_from_announcement(body)
        elif event_name == "announce_chime":
            state.update_from_announce_chime(body)
        elif event_name == "voice_enhancement":
            state.update_from_voice_enhancement(body)
        elif event_name == "streaming_timeout":
            state.update_from_streaming_timeout(body)
        elif event_name == "announcement_vol":
            state.update_from_announcement_vol(body)
        elif event_name == "groups":
            self._update_from_groups(device_id, body)

        self.async_set_updated_data(self._device_states)

    def _update_from_groups(self, device_id: str, body: dict[str, Any]) -> None:
        """Fold one device's ``groups`` report into the canonical zone view.

        Every member of a zone reports that zone in its own list, so one
        logical zone arrives once per connected speaker. Diffing each
        device's copy against its own previous copy therefore fired one event
        per speaker for a single change, and worse: a device that left a zone
        stopped listing it, which read as a deletion even though the zone was
        still there on everyone else.

        So the order here is fixed and matters:

        1. snapshot the canonical view;
        2. replace this device's cached copy;
        3. rebuild the canonical view from every device's cache;
        4. diff old canonical against new canonical;
        5. fire one event per logical change.

        Events are suppressed while a device is doing its first sync of this
        coordinator's life. Those zones existed before Home Assistant
        connected: announcing them as newly created would fire a burst on
        every startup and reload, and an automation cannot tell that burst
        from a real one.
        """
        incoming = {
            g["group_id"]: UnifiPlayGroupState.from_mqtt(g)
            for g in body.get("groups", [])
            if "group_id" in g
        }
        _LOGGER.debug(
            "groups event from %s: %d zone(s) incoming, %d known for this device",
            device_id,
            len(incoming),
            len(self._device_groups.get(device_id, {})),
        )

        is_initial = device_id not in self._device_groups_initialized
        previous = self.groups

        self._device_groups[device_id] = dict(incoming)
        self._device_groups_initialized.add(device_id)
        self.groups = self._rebuild_canonical_zones()
        self._log_zone_conflicts()
        self.reconcile_pending_groups()

        if is_initial:
            return
        self._fire_zone_events(previous, self.groups)

    def _rebuild_canonical_zones(self) -> dict[str, UnifiPlayGroupState]:
        """Merge every device's cached copies into one view of each zone.

        Only the host's copy is authoritative: after an edit the host emits
        the new state at once while members keep serving their previous copy
        until they resync, so a plain dict merge lets a stale copy land last
        and silently revert the edit.

        A device claims a zone only when its own copy names it as host, and
        two devices can claim the same zone at once while it changes hands -
        the old host keeps serving a copy naming itself until it resyncs.
        The tie-break is deliberately NOT "whichever device reported last":
        that resurrects a stale claim every time the old host happens to
        speak, and it makes the merged result depend on event ordering, so
        rebuilding from the same caches twice could give two answers.

        Instead the order is total and stable: highest wire timestamp first,
        then the lowest source MAC. The MAC half is arbitrary - it exists to
        be *stable*, not to be right - and it only ever decides between two
        devices that both claim host, which is a transient state the next
        resync resolves. (The timestamp half is currently vestigial: the
        groups event carries a timestamp at the top level of the body and
        never inside a group, so every copy compares equal at zero. It is
        kept because a firmware that starts echoing it would immediately be
        the better signal.)
        """
        claims: dict[str, list[tuple[int, str, UnifiPlayGroupState]]] = {}
        fallbacks: dict[str, list[tuple[str, UnifiPlayGroupState]]] = {}

        for src_device_id, device_zones in self._device_groups.items():
            src_state = self._device_states.get(src_device_id)
            src_mac = _norm_mac(src_state.mac) if src_state else ""
            for gid, gs in device_zones.items():
                fallbacks.setdefault(gid, []).append((src_mac, gs))
                if src_mac and _norm_mac(gs.host_mac) == src_mac:
                    claims.setdefault(gid, []).append((gs.timestamp, src_mac, gs))

        merged: dict[str, UnifiPlayGroupState] = {}
        for gid, candidates in fallbacks.items():
            claimed = claims.get(gid)
            if claimed:
                # Highest timestamp wins; lowest source MAC breaks the tie.
                _, _, gs = max(claimed, key=lambda item: (item[0], _invert(item[1])))
                merged[gid] = gs
                continue
            # Nobody claims it - a freshly written zone before the firmware
            # has elected a host, or a zone whose host is offline. Pick by the
            # same stable rule so the answer does not depend on event order.
            merged[gid] = min(candidates, key=lambda item: item[0])[1]
        return merged

    def _log_zone_conflicts(self) -> None:
        """Say once when connected speakers disagree, and once when they agree again.

        Disagreement is normal for a moment after any edit - the writer's
        copy lands on each device at its own pace - so this is not an error.
        It becomes one when it persists, and the only way to see that in a
        log is to have the transition recorded rather than a line per event.

        The signature deliberately excludes ``host``: the firmware elects the
        host and two devices legitimately both claim it while a zone changes
        hands, so including it would report a conflict on every election.
        """
        conflicted: set[str] = set()
        for gid, signatures in self._zone_signatures().items():
            if len(signatures) > 1:
                conflicted.add(gid)

        for gid in sorted(conflicted - self._conflicted_zones):
            name = self.groups[gid].name if gid in self.groups else gid
            _LOGGER.warning(
                "Speakers disagree about zone %r (%s): %d different copies are "
                "being reported. Home Assistant is using the host's. This "
                "usually resolves itself within a few seconds of an edit; if "
                "it persists, edit the zone once from Home Assistant or the "
                "Play app to converge it",
                name,
                gid,
                len(self._zone_signatures().get(gid, ())),
            )
        for gid in sorted(self._conflicted_zones - conflicted):
            name = self.groups[gid].name if gid in self.groups else gid
            _LOGGER.info("Speakers now agree about zone %r (%s)", name, gid)
        self._conflicted_zones = conflicted

    def zone_copy_counts(self) -> dict[str, int]:
        """How many distinct documents each zone is being reported with.

        One means the speakers agree. More means a zone that will behave like
        one that keeps reverting, with nothing in the UI to say so, which is
        why diagnostics carries it.
        """
        return {
            group_id: len(signatures)
            for group_id, signatures in self._zone_signatures().items()
        }

    def _zone_signatures(self) -> dict[str, set[tuple[Any, ...]]]:
        """The distinct logical documents each zone is being reported with."""
        signatures: dict[str, set[tuple[Any, ...]]] = {}
        for device_zones in self._device_groups.values():
            for gid, gs in device_zones.items():
                signatures.setdefault(gid, set()).add(_zone_signature(gs))
        return signatures

    def _fire_zone_events(
        self,
        previous: dict[str, UnifiPlayGroupState],
        current: dict[str, UnifiPlayGroupState],
    ) -> None:
        """Emit one event per logical change between two canonical views.

        The count is a property of the change, not of how many speakers
        happened to report it.
        """
        for gid in sorted(set(previous) - set(current)):
            gs = previous[gid]
            self.hass.bus.async_fire(
                EVENT_ZONE_DELETED, {"group_id": gid, "name": gs.name}
            )

        for gid in sorted(set(current) - set(previous)):
            gs = current[gid]
            self.hass.bus.async_fire(
                EVENT_ZONE_CREATED,
                {
                    "group_id": gid,
                    "name": gs.name,
                    "host_mac": gs.host_mac,
                    "dev_count": gs.dev_count,
                },
            )

        for gid in sorted(set(current) & set(previous)):
            old_gs, new_gs = previous[gid], current[gid]
            if old_gs.name != new_gs.name:
                self.hass.bus.async_fire(
                    EVENT_ZONE_RENAMED,
                    {
                        "group_id": gid,
                        "name": new_gs.name,
                        "previous_name": old_gs.name,
                    },
                )
            old_macs = _member_macs(old_gs)
            new_macs = _member_macs(new_gs)
            if old_macs == new_macs:
                continue
            self.hass.bus.async_fire(
                EVENT_ZONE_MEMBER_CHANGED,
                {
                    "group_id": gid,
                    "name": new_gs.name,
                    # Sorted so an automation comparing payloads across two
                    # runs sees the same list; set iteration order is not
                    # stable between processes.
                    "added_macs": sorted(new_macs - old_macs),
                    "removed_macs": sorted(old_macs - new_macs),
                },
            )

    @property
    def zones(self) -> ZoneWriter:
        """The one path that writes zone topology.

        Every mutation - create, rename, reorder, membership, broadcast
        source, delete - goes through here. A caller that publishes
        ``set_groups`` itself skips the preflight, and a zone written to some
        speakers and not others forms, competes on merge and reverts minutes
        later with nothing in the log.
        """
        return self._zone_writer

    @property
    def zones_fully_synced(self) -> bool:
        """True once every known speaker has reported its zone list.

        Until then ``groups`` is a partial view - after a restart it starts
        empty and fills as each speaker connects - so anything that treats an
        absent zone as a deleted zone has to wait. Removing a zone entity
        because the speaker hosting it has not connected yet destroys its
        registry row, and with it every dashboard card and automation
        pointing at that zone.

        A speaker that is offline never reports, so this stays False while
        anything is unreachable. That is the safe bias: it costs a stale zone
        entity until the speaker returns, and the alternative costs the
        user's configuration.
        """
        known = set(self._device_states)
        return bool(known) and known <= self._device_groups_initialized

    def device_zone_cache(self) -> dict[str, dict[str, UnifiPlayGroupState]]:
        """Each device's own last-reported zone list, keyed by device id.

        Read by the write path to work out whose cached copy a change would
        leave stale. Kept per device rather than merged because a copy that
        is about to go stale is a property of the speaker holding it.
        """
        return self._device_groups

    def groups_for_write(self) -> dict[str, UnifiPlayGroupState]:
        """The zone list a subsequent write must build from.

        Submission is not acknowledgement. ``groups`` stays at the last
        device report until that report arrives, so sequential mutations
        have to serialise against what we last submitted, not against a
        snapshot that is still waiting for readback.
        """
        if self._pending_groups is not None:
            return self._pending_groups
        return self.groups

    def adopt_written_groups(self, documents: list[dict[str, Any]]) -> None:
        """Record a successfully submitted replace-all document as pending.

        Host is firmware-owned and stripped from the write, so a zone whose
        members did not change keeps the host we already knew; otherwise the
        next membership edit would look hostless until the reread lands.
        """
        previous = self.groups_for_write()
        pending = {
            doc["group_id"]: UnifiPlayGroupState.from_mqtt(doc)
            for doc in documents
            if "group_id" in doc
        }
        for gid, gs in pending.items():
            old = previous.get(gid)
            if (
                old is not None
                and not gs.host_mac
                and _member_macs(old) == _member_macs(gs)
            ):
                gs.host_mac = old.host_mac
        self._pending_groups = pending

    def reconcile_pending_groups(self) -> None:
        """Drop the pending snapshot once readback confirms the write.

        Matching signatures (host excluded) means the speakers have applied
        it. A stale copy — the normal window after a write — must not drop
        the snapshot: that is the report that would undo a rename if the
        next mutation rebuilt from it. An app edit that arrives after
        confirmation is already the new ``groups``; one that arrives before
        is last-writer-wins, same as any other overlapping edit.
        """
        if self._pending_groups is None:
            return
        pending_sigs = {
            gid: _zone_signature(gs) for gid, gs in self._pending_groups.items()
        }
        current_sigs = {gid: _zone_signature(gs) for gid, gs in self.groups.items()}
        if pending_sigs == current_sigs:
            self._pending_groups = None

    def zone_documents(
        self, group_id: str, updated: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        """The complete zone list to write, with one zone replaced or removed.

        ``set_groups`` is replace-all per device, so every write carries the
        whole list. Rebuilding it from the write snapshot means zones the
        caller is not touching survive — including a rename that has been
        submitted but not yet reported back.
        """
        from .helpers import gs_to_dict

        groups: list[dict[str, Any]] = []
        replaced = False
        for gid, gs in self.groups_for_write().items():
            if gid != group_id:
                groups.append(gs_to_dict(gs))
                continue
            replaced = True
            if updated is not None:
                groups.append(updated)
        if updated is not None and not replaced:
            groups.append(updated)  # a zone being created
        return groups

    @callback
    def _cancel_host_reread(self) -> None:
        """Drop any pending post-write zone re-reads.

        A re-read that fires thirty seconds after the entry unloaded is a
        task outliving its owner, and a reload that leaves the old one
        running ends up with two of everything.
        """
        for cancel in self._host_reread_cancels:
            cancel()
        self._host_reread_cancels.clear()

    def schedule_host_election_reread(self) -> None:
        """Re-read zones shortly after a write, to learn the elected host.

        "host" is firmware-owned: the speakers elect one after a set_groups
        write and do NOT push a groups event to announce it. Without asking
        again, host_mac stays empty until the next reconnect, and every
        host-routed operation on a freshly written zone fails - rename, add
        or remove a member, set the source, reorder, or creating a second
        zone on the same speaker.

        It is also the only confirmation the protocol offers that a write
        landed: there is no acknowledgement for set_groups, so the groups
        event that follows is what tells you the speakers agree.

        Re-reads are cheap (a groups request per connected speaker) and
        idempotent, so this fires a short series rather than betting on one
        delay being right for every firmware.
        """

        # A second write supersedes the first: the series is about learning
        # the host of the zone last written, so restarting it is both cheaper
        # and more correct than stacking two sets of timers.
        self._cancel_host_reread()

        @callback
        def _reread(_now: datetime | None = None) -> None:
            for client in self._mqtt_clients.values():
                if client is not None and client.is_connected:
                    client.request_groups()

        for delay in HOST_ELECTION_REREAD_DELAYS:
            # Held so shutdown can cancel them. A re-read that fires thirty
            # seconds after the entry unloaded is a task outliving its owner,
            # which is how a reload ends up with two of everything.
            self._host_reread_cancels.append(
                async_call_later(self.hass, delay, _reread)
            )

    def get_mqtt_client(self, device_id: str) -> UnifiPlayMqttClient | None:
        """Return the MQTT client for a device."""
        return self._mqtt_clients.get(device_id)

    def get_host_mqtt_client(self, group_id: str) -> UnifiPlayMqttClient | None:
        """Return the MQTT client for the zone host device."""
        gs = self.groups.get(group_id)
        if not gs or not gs.host_mac:
            return None
        target = _norm_mac(gs.host_mac)
        for dev_id, state in self._device_states.items():
            if _norm_mac(state.mac) == target:
                return self._mqtt_clients.get(dev_id)
        return None

    def get_mqtt_client_for_mac(self, mac: str) -> UnifiPlayMqttClient | None:
        """Return the MQTT client for a device by MAC, only if it is connected.

        A client object outlives its connection, and ``publish_action`` merely
        warns and drops the message when the socket is down. Callers that move
        a zone between hosts must know up front whether BOTH ends can be
        written: the handoff is two publishes, so a silently dropped one
        leaves the zone stripped from the old host and never given to the new
        one. Returning None here routes that into the caller's no_mqtt error
        before anything is written.
        """
        target = _norm_mac(mac)
        for dev_id, state in self._device_states.items():
            if _norm_mac(state.mac) == target:
                client = self._mqtt_clients.get(dev_id)
                return client if client and client.is_connected else None
        return None

    def get_groups_hosted_by(
        self, mac: str, exclude_group_id: str | None = None
    ) -> list[UnifiPlayGroupState]:
        """Return every zone hosted by this MAC, optionally excluding one.

        Used when a zone changes hands between hosts: each device's group list
        is replace-all, so both the old and new host need theirs rebuilt.
        """
        host = _norm_mac(mac)
        return [
            gs
            for gs in self.groups.values()
            if _norm_mac(gs.host_mac) == host and gs.group_id != exclude_group_id
        ]

    def get_zone_host_state(self, group_id: str) -> UnifiPlayDeviceState | None:
        """Return the device state for the zone host."""
        gs = self.groups.get(group_id)
        if not gs or not gs.host_mac:
            return None
        target = _norm_mac(gs.host_mac)
        for state in self._device_states.values():
            if _norm_mac(state.mac) == target:
                return state
        return None

    def get_zone_members(
        self, group_id: str
    ) -> list[tuple[str, UnifiPlayDeviceState, UnifiPlayMqttClient | None]]:
        """Return (dev_id, state, client) for every member of a zone.

        Includes the host device. Members that are offline (no MQTT client)
        are included with client=None so callers can log them if needed.
        """
        gs = self.groups.get(group_id)
        if not gs:
            return []
        result = []
        for entry in gs.dev_info:
            mac = _norm_mac(entry.get("mac", ""))
            for dev_id, state in self._device_states.items():
                if _norm_mac(state.mac) == mac:
                    result.append((dev_id, state, self._mqtt_clients.get(dev_id)))
                    break
        return result

    async def async_shutdown(self) -> None:
        """Disconnect all MQTT clients."""
        for state in self._device_states.values():
            self._async_delete_cert_issue(state.mac)
        self._cancel_host_reread()
        for client in self._mqtt_clients.values():
            await client.disconnect()
        self._mqtt_clients.clear()
        self._mqtt_offline_reason.clear()
        self._pending_groups = None
        self._discovery_misses.clear()
        # Nothing to close: the API client borrows Home Assistant's shared
        # session, which Home Assistant closes on shutdown.
