"""Data coordinator for UniFi Play devices."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import UnifiPlayApi, UnifiPlayApiError
from .const import EVENT_ZONE_CREATED, EVENT_ZONE_DELETED, EVENT_ZONE_MEMBER_CHANGED
from .discovery import async_resolve_direct
from .mqtt_client import UnifiPlayMqttClient

_LOGGER = logging.getLogger(__name__)


def _norm_mac(mac: str) -> str:
    """Normalise a MAC address to uppercase hex without delimiters."""
    return mac.upper().replace(":", "")


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
    dev_info: list[dict] = field(default_factory=list)
    host_mac: str = ""  # MAC of the device with host=True in dev_info
    # Epoch seconds stamped into the zone when it was last written. Devices
    # echo it back unchanged, which makes it the only way to tell a fresh
    # copy of a zone from a stale one when several devices report it.
    timestamp: int = 0

    @classmethod
    def from_mqtt(cls, group: dict) -> "UnifiPlayGroupState":
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


class UnifiPlayDeviceState:
    """State container for a single Play device, updated via MQTT events."""

    def __init__(self, device_data: dict) -> None:
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
        self.eq_custom_presets: list = []
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
        self.alarms: list = []
        self.quiet_hours: list = []
        self.ann_files: list = []
        self.ann_schedule: list = []
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

    def update_from_info(self, body: dict) -> None:
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

    def update_from_equalizer(self, body: dict) -> None:
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

    def update_from_sub_audio(self, body: dict) -> None:
        """Update sub audio state from an MQTT 'sub_audio' event."""
        if "crossover" in body:
            self.sub_crossover = body["crossover"]
        if "level" in body:
            self.sub_level = body["level"]
        if "phase" in body:
            self.sub_phase = body["phase"]
        if "subwoofer" in body:
            self.subwoofer = body["subwoofer"]

    def update_from_metadata(self, body: dict) -> None:
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

    def update_from_online(self, body: dict) -> None:
        """Update online status from an MQTT 'online' event."""
        self.online = body.get("status", 0) == 1

    def update_from_alarms(self, body: list | dict) -> None:
        """Update the alarm list from an MQTT 'alarms' event (a bare list)."""
        if isinstance(body, list):
            self.alarms = body

    def update_from_quiet_hours(self, body: list | dict) -> None:
        """Update quiet hours from an MQTT 'quiet_hours' event (a bare list)."""
        if isinstance(body, list):
            self.quiet_hours = body

    def update_from_announcement(self, body: dict) -> None:
        """Update announcement files/schedule from an 'announcement' event."""
        if "files" in body:
            self.ann_files = body["files"] or []
        if "schedule" in body:
            self.ann_schedule = body["schedule"] or []

    def update_from_announce_chime(self, body: dict) -> None:
        """Update the announcement chime from an 'announce_chime' event."""
        if "chime" in body:
            self.ann_chime = body["chime"]

    def update_from_voice_enhancement(self, body: dict) -> None:
        """Update voice enhancement from a 'voice_enhancement' event."""
        if "enable" in body:
            self.voice_enhancement = bool(body["enable"])

    def update_from_streaming_timeout(self, body: dict) -> None:
        """Update streaming timeout from a 'streaming_timeout' event."""
        if "second" in body:
            self.streaming_timeout = body["second"]

    def update_from_announcement_vol(self, body: dict) -> None:
        """Update announcement volume from an 'announcement_vol' event."""
        if "value" in body:
            self.ann_volume = body["value"]

    def update_from_extra_info(self, body: dict) -> None:
        """Update device identity from an MQTT 'extra_info' event.

        In direct mode a device identified through its MQTT topics is only
        known by its topic root (UPL-DEVICE for a Port) with no firmware
        version; extra_info carries the real platform and version.
        """
        if body.get("platform"):
            self.platform = body["platform"]
        if body.get("version"):
            match = re.search(r"v?(\d+(?:\.\d+)+)", body["version"])
            self.firmware = match.group(1) if match else body["version"]
        if "uptime" in body:
            self.uptime = body["uptime"]
        if "link_quality" in body:
            self.link_quality = body["link_quality"]


class UnifiPlayCoordinator(DataUpdateCoordinator[dict[str, UnifiPlayDeviceState]]):
    """Coordinates REST discovery + MQTT real-time updates for all devices."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: UnifiPlayApi | None,
        manual_hosts: list[str] | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="UniFi Play",
            update_interval=DISCOVERY_INTERVAL,
        )
        self.api = api
        self.manual_hosts = manual_hosts or []
        self._mqtt_clients: dict[str, UnifiPlayMqttClient] = {}
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
        self.groups: dict[str, UnifiPlayGroupState] = {}

    async def _async_update_data(self) -> dict[str, UnifiPlayDeviceState]:
        """Fetch the device list and return current state dict.

        Console mode asks the console's Apollo REST API; direct mode probes
        the network itself (UDP broadcast plus unicast to any manual hosts).
        Devices seen once are kept even if a later scan misses them — MQTT
        remains the source of truth for online state.
        """
        if self.api is not None:
            try:
                devices = await self.api.get_devices()
            except UnifiPlayApiError as err:
                raise UpdateFailed(f"Error fetching devices: {err}") from err
        else:
            # Manual hosts already tracked as devices are excluded from the
            # MQTT fallback probe — no point opening a second TLS connection
            # to a speaker we hold a live connection to.
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
                if dev.get("ip"):
                    state.ip = dev["ip"]
                if dev.get("firmware"):
                    state.firmware = dev["firmware"]
            ip = dev.get("ip", "")
            mac = dev.get("mac", "")
            if ip and mac:
                await self._ensure_mqtt(dev_id, ip, mac)
        return self._device_states

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
        """
        existing = self._mqtt_clients.get(device_id)
        if existing is not None:
            if existing.is_connected:
                return
            _LOGGER.info("MQTT connection to %s dropped; reconnecting", ip)
            try:
                await existing.disconnect()
            except Exception:  # noqa: BLE001 - a dead client may fail any way
                _LOGGER.debug("Cleanup of dropped MQTT client for %s failed", ip)
            self._mqtt_clients.pop(device_id, None)
        await self._start_mqtt(device_id, ip, mac)

    async def _start_mqtt(self, device_id: str, ip: str, mac: str) -> None:
        """Start an MQTT connection for a device."""

        def _schedule_event(event_name: str, header: dict, body: dict) -> None:
            self.hass.loop.call_soon_threadsafe(
                self._handle_event, device_id, event_name, header, body
            )

        client = UnifiPlayMqttClient(ip, mac, on_event=_schedule_event)
        self._mqtt_clients[device_id] = client
        try:
            await client.connect()
            await asyncio.sleep(0.5)
            client.request_info()
            client.request_extra_info()
            client.request_metadata()
            client.request_equalizer()
            client.request_sub_audio()
            client.request_features()
            client.request_groups()
        except Exception:
            state = self._device_states.get(device_id)
            platform = state.platform if state else "unknown"
            _LOGGER.exception(
                "Failed to connect MQTT to %s (%s, %s), will retry", ip, mac, platform
            )
            # Drop the half-built client so the next discovery poll retries.
            # Leaving it in place would strand the device without state for as
            # long as the config entry lives.
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Cleanup of failed MQTT client for %s failed", ip)
            self._mqtt_clients.pop(device_id, None)

    @callback
    def _handle_event(
        self, device_id: str, event_name: str, header: dict, body: dict
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

    def _update_from_groups(self, device_id: str, body: dict) -> None:
        """Process a 'groups' event from device_id, fire topology-change events, and update state.

        Groups are tracked per source device so that a groups event from device
        B (which lists only B's zones) does not wipe zones hosted by device A.
        The public ``self.groups`` is always a merged view across all devices.

        zone_created / zone_deleted / zone_member_changed events are suppressed
        on the first sync for each device (startup / reload) to avoid flooding
        the event bus with zones that already existed before HA connected.
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
        old_device_groups = self._device_groups.get(device_id, {})

        if not is_initial:
            # Fire events for zones that disappeared from this device.
            for gid in set(old_device_groups) - set(incoming):
                gs = old_device_groups[gid]
                self.hass.bus.async_fire(
                    EVENT_ZONE_DELETED, {"group_id": gid, "name": gs.name}
                )

            # Fire events for new zones and member-list changes on this device.
            for gid, new_gs in incoming.items():
                if gid not in old_device_groups:
                    self.hass.bus.async_fire(
                        EVENT_ZONE_CREATED,
                        {
                            "group_id": gid,
                            "name": new_gs.name,
                            "host_mac": new_gs.host_mac,
                            "dev_count": new_gs.dev_count,
                        },
                    )
                else:
                    old_gs = old_device_groups[gid]
                    old_macs = {
                        _norm_mac(d["mac"]) for d in old_gs.dev_info if d.get("mac")
                    }
                    new_macs = {
                        _norm_mac(d["mac"]) for d in new_gs.dev_info if d.get("mac")
                    }
                    added = new_macs - old_macs
                    removed = old_macs - new_macs
                    if added or removed:
                        self.hass.bus.async_fire(
                            EVENT_ZONE_MEMBER_CHANGED,
                            {
                                "group_id": gid,
                                "name": new_gs.name,
                                "added_macs": list(added),
                                "removed_macs": list(removed),
                            },
                        )

        # Update per-device cache and rebuild merged view.
        self._device_groups[device_id] = dict(incoming)
        self._device_groups_initialized.add(device_id)

        # Every member of a zone reports that zone in its own groups list, so
        # the same zone arrives from several devices. Only the host's copy is
        # authoritative: after an edit the host emits the new state at once,
        # while members keep serving their previous copy until they resync. A
        # plain dict merge lets one of those stale copies land last and
        # silently revert the edit.
        # A device only claims a zone if its own copy names it as host. That is
        # still ambiguous while a zone changes hands - the old host keeps
        # serving a copy that names ITSELF until it resyncs, so two devices
        # claim the same zone at once. Break that on the wire timestamp, which
        # the writer stamps and every device echoes back unchanged: the most
        # recently written copy is the true one. Preferring "whichever device
        # reported last" instead would resurrect a stale claim every time the
        # old host happened to speak.
        merged: dict[str, UnifiPlayGroupState] = {}
        host_copies: dict[str, UnifiPlayGroupState] = {}
        for src_device_id, dg in self._device_groups.items():
            src_state = self._device_states.get(src_device_id)
            src_mac = _norm_mac(src_state.mac) if src_state else ""
            for gid, gs in dg.items():
                merged.setdefault(gid, gs)
                if not (src_mac and _norm_mac(gs.host_mac) == src_mac):
                    continue
                best = host_copies.get(gid)
                if (
                    best is None
                    or gs.timestamp > best.timestamp
                    # Equal timestamps (or firmware that omits them) fall back
                    # to the device whose event we are handling right now.
                    or (gs.timestamp == best.timestamp and src_device_id == device_id)
                ):
                    host_copies[gid] = gs

        merged.update(host_copies)
        self.groups = merged

    def update_zone(
        self,
        *,
        group_id: str,
        name: str,
        dev_info: list,
        group_index: int = 0,
        broadcasting_mode: str = "zone_only",
        wb_enable: bool = False,
        wb_device: str = "",
        wb_input: str = "",
    ) -> list[str]:
        """Create or update one zone across every connected device.

        Callers no longer assemble sibling_groups: publish_zones rebuilds the
        full list from coordinator state, so zones on other hosts survive
        automatically rather than depending on each caller remembering to
        resend them.
        """
        from .helpers import group_payload

        return self.publish_zones(
            group_id,
            group_payload(
                group_id=group_id,
                name=name,
                dev_info=dev_info,
                group_index=group_index,
                broadcasting_mode=broadcasting_mode,
                wb_enable=wb_enable,
                wb_device=wb_device,
                wb_input=wb_input,
            ),
        )

    def delete_zone(self, group_id: str) -> list[str]:
        """Remove one zone from every connected device."""
        return self.publish_zones(group_id, None)

    def publish_zones(self, group_id: str, updated: dict | None) -> list[str]:
        """Write a zone change out to EVERY connected device.

        ``updated`` is the new wire dict for the zone, or None to delete it.
        Returns the MACs actually written to.

        set_groups is replace-all *per device*, and every device carries a copy
        of every zone - including zones it is not a member of. Publishing only
        to the zone's host therefore leaves every other device serving its
        previous copy indefinitely: nothing propagates between devices. Those
        stale copies then compete in _update_from_groups, which is how an
        accepted edit could appear to revert on the next resync.

        Verified on five UPL-PORTs (fw 1.1.10): after a host handover written
        only to the new host, three of four devices still named the old host;
        re-publishing the full list to all five converged them, and the three
        zone members held that state across a reload.

        Sending the complete list to everyone also means a zone changing hands
        needs no separate "strip it from the old host" write - the old host is
        simply given the same list as everyone else.
        """
        from .helpers import gs_to_dict

        groups: list[dict] = []
        replaced = False
        for gid, gs in self.groups.items():
            if gid != group_id:
                groups.append(gs_to_dict(gs))
                continue
            replaced = True
            if updated is not None:
                groups.append(updated)
        if updated is not None and not replaced:
            groups.append(updated)  # a zone being created

        written: list[str] = []
        for dev_id, client in self._mqtt_clients.items():
            if client is None or not client.is_connected:
                continue
            client.publish_action("set_groups", {"groups": groups})
            state = self._device_states.get(dev_id)
            if state is not None:
                written.append(state.mac)
        _LOGGER.debug(
            "publish_zones: %d zone(s) -> %d device(s) %s",
            len(groups),
            len(written),
            written,
        )
        return written

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
    ) -> list["UnifiPlayGroupState"]:
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

    def get_zone_host_state(self, group_id: str) -> "UnifiPlayDeviceState | None":
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
    ) -> list[tuple[str, "UnifiPlayDeviceState", "UnifiPlayMqttClient | None"]]:
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
        for client in self._mqtt_clients.values():
            await client.disconnect()
        self._mqtt_clients.clear()
        if self.api is not None:
            await self.api.close()
