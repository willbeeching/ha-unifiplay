"""Data coordinator for UniFi Play devices."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import UnifiPlayApi, UnifiPlayApiError
from .mqtt_client import UnifiPlayMqttClient

_LOGGER = logging.getLogger(__name__)


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
        self.stream_playing: bool = False
        self.muted: bool = False
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
        self.now_playing_song: str = ""
        self.now_playing_artist: str = ""
        self.now_playing_album: str = ""
        self.now_playing_length: int = 0
        self.now_playing_current: int = 0
        self.now_playing_cover: str = ""

    def update_from_info(self, body: dict) -> None:
        """Update state from an MQTT 'info' event."""
        if "volume" in body:
            self.volume = body["volume"]
        if "source" in body:
            self.source = body["source"]
        if "stream_playing" in body:
            self.stream_playing = body["stream_playing"]
        if "muted" in body:
            self.muted = body["muted"]
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

    def update_from_metadata(self, body: dict) -> None:
        """Update now-playing state from an MQTT 'metadata' event."""
        if "song" in body:
            self.now_playing_song = body["song"]
        if "artist" in body:
            self.now_playing_artist = body["artist"]
        if "album" in body:
            self.now_playing_album = body["album"]
        if "length" in body:
            self.now_playing_length = body["length"]
        if "current" in body:
            self.now_playing_current = body["current"]
        if "cover_path" in body:
            self.now_playing_cover = body["cover_path"]

    def update_from_online(self, body: dict) -> None:
        """Update online status from an MQTT 'online' event."""
        self.online = body.get("status", 0) == 1


class UnifiPlayCoordinator(DataUpdateCoordinator[dict[str, UnifiPlayDeviceState]]):
    """Coordinates REST discovery + MQTT real-time updates for all devices."""

    def __init__(self, hass: HomeAssistant, api: UnifiPlayApi) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="UniFi Play",
        )
        self.api = api
        self._mqtt_clients: dict[str, UnifiPlayMqttClient] = {}
        self._device_states: dict[str, UnifiPlayDeviceState] = {}

    async def _async_update_data(self) -> dict[str, UnifiPlayDeviceState]:
        """Fetch device list from REST and return current state dict."""
        try:
            devices = await self.api.get_devices()
        except UnifiPlayApiError as err:
            raise UpdateFailed(f"Error fetching devices: {err}") from err

        for dev in devices:
            dev_id = dev["id"]
            if dev_id not in self._device_states:
                self._device_states[dev_id] = UnifiPlayDeviceState(dev)
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
        except Exception:
            _LOGGER.exception("Failed to connect MQTT to %s (%s)", ip, mac)

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

        self.async_set_updated_data(self._device_states)

    def get_mqtt_client(self, device_id: str) -> UnifiPlayMqttClient | None:
        """Return the MQTT client for a device."""
        return self._mqtt_clients.get(device_id)

    async def async_shutdown(self) -> None:
        """Disconnect all MQTT clients."""
        for client in self._mqtt_clients.values():
            await client.disconnect()
        self._mqtt_clients.clear()
        await self.api.close()
