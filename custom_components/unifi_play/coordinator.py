"""Data coordinator for UniFi Play devices."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import UnifiPlayApi, UnifiPlayApiError
from .discovery import async_resolve_direct
from .mqtt_client import UnifiPlayMqttClient

_LOGGER = logging.getLogger(__name__)

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

    def update_from_equalizer(self, body: dict) -> None:
        """Update EQ state from an MQTT 'equalizer' event."""
        if "active_profile" in body:
            self.eq_preset = body["active_profile"]
        if "eq_enable" in body:
            self.eq_enable = body["eq_enable"]

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

    def update_from_online(self, body: dict) -> None:
        """Update online status from an MQTT 'online' event."""
        self.online = body.get("status", 0) == 1

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
            if ip and mac and dev_id not in self._mqtt_clients:
                await self._start_mqtt(dev_id, ip, mac)
        return self._device_states

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

        self.async_set_updated_data(self._device_states)

    def get_mqtt_client(self, device_id: str) -> UnifiPlayMqttClient | None:
        """Return the MQTT client for a device."""
        return self._mqtt_clients.get(device_id)

    async def async_shutdown(self) -> None:
        """Disconnect all MQTT clients."""
        for client in self._mqtt_clients.values():
            await client.disconnect()
        self._mqtt_clients.clear()
        if self.api is not None:
            await self.api.close()
