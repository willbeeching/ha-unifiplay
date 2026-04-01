"""MQTT client for direct communication with UniFi Play devices."""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import struct
import time
import uuid
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt

from .const import (
    BINME_FORMAT_JSON,
    BINME_TYPE_BODY,
    BINME_TYPE_HEADER,
    MQTT_KEEPALIVE,
    MQTT_PORT,
    TOPIC_AMP,
    TOPIC_DEVICE,
    TOPIC_MOBILE,
)

_LOGGER = logging.getLogger(__name__)

CERTS_DIR = Path(__file__).parent / "certs"
CERT_FILE = CERTS_DIR / "mqtt_cert.crt"
KEY_FILE = CERTS_DIR / "mqtt_cert_key.key"


def encode_binme(header: dict, body: dict) -> bytes:
    """Encode header + body dicts into the Binme binary wire format."""
    header_bytes = json.dumps(header).encode("utf-8")
    body_bytes = json.dumps(body).encode("utf-8")
    buf = bytearray()
    for part_type, data in (
        (BINME_TYPE_HEADER, header_bytes),
        (BINME_TYPE_BODY, body_bytes),
    ):
        buf.append(part_type)
        buf.append(BINME_FORMAT_JSON)
        buf.append(0)  # not compressed
        buf.append(0)  # reserved
        buf += struct.pack(">I", len(data))
        buf += data
    return bytes(buf)


def decode_binme(payload: bytes) -> dict[str, Any]:
    """Decode a Binme binary payload into {"header": ..., "body": ...}."""
    pos = 0
    parts: dict[str, Any] = {}
    while pos + 8 <= len(payload):
        ptype = payload[pos]
        pfmt = payload[pos + 1]
        compressed = payload[pos + 2]
        pos += 4  # skip reserved byte too
        length = struct.unpack(">I", payload[pos : pos + 4])[0]
        pos += 4
        data = payload[pos : pos + length]
        pos += length
        if compressed:
            data = zlib.decompress(data)
        label = "header" if ptype == BINME_TYPE_HEADER else "body"
        if pfmt == BINME_FORMAT_JSON:
            try:
                parts[label] = json.loads(data.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                parts[label] = data
        else:
            parts[label] = data
    return parts


class UnifiPlayMqttClient:
    """Manages an MQTT connection to a single UniFi Play device."""

    def __init__(
        self,
        device_ip: str,
        device_mac: str,
        on_event: Callable[[str, dict, dict], None] | None = None,
    ) -> None:
        self._device_ip = device_ip
        self._device_mac = device_mac.upper().replace(":", "")
        self._on_event = on_event
        self._client_uuid = uuid.uuid4().hex[:12]
        self._pub_topic = f"{TOPIC_MOBILE}/{self._client_uuid}/action"
        self._client: mqtt.Client | None = None
        self._loop_task: asyncio.Task | None = None
        self._connected = asyncio.Event()

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected()

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: Any,
        rc: Any,
        properties: Any = None,
    ) -> None:
        _LOGGER.debug("MQTT connected to %s: %s", self._device_ip, rc)
        topics = [
            (f"{TOPIC_AMP}/{self._device_mac}/status", 0),
            (f"{TOPIC_DEVICE}/{self._device_mac}/status", 0),
        ]
        client.subscribe(topics)
        self._connected.set()

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: Any,
        rc: Any,
        properties: Any = None,
    ) -> None:
        _LOGGER.debug("MQTT disconnected from %s: %s", self._device_ip, rc)
        self._connected.clear()

    def _on_message(
        self, client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage
    ) -> None:
        try:
            parsed = decode_binme(msg.payload)
            header = parsed.get("header", {})
            body = parsed.get("body", {})
            event_name = header.get("name", header.get("action", "unknown"))
            _LOGGER.debug("MQTT event from %s: %s", self._device_ip, event_name)
            if self._on_event:
                self._on_event(event_name, header, body)
        except Exception:
            _LOGGER.exception("Error parsing MQTT message from %s", self._device_ip)

    async def connect(self) -> None:
        """Connect to the device's MQTT broker."""
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"ha-unifiplay-{self._client_uuid}",
        )
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

        self._client.tls_set(
            certfile=str(CERT_FILE),
            keyfile=str(KEY_FILE),
            cert_reqs=ssl.CERT_NONE,
            tls_version=ssl.PROTOCOL_TLS_CLIENT,
        )
        self._client.tls_insecure_set(True)

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, self._client.connect, self._device_ip, MQTT_PORT, MQTT_KEEPALIVE
        )
        self._loop_task = asyncio.create_task(self._mqtt_loop())

    async def _mqtt_loop(self) -> None:
        """Run the paho loop in a non-blocking fashion."""
        loop = asyncio.get_running_loop()
        while self._client is not None:
            await loop.run_in_executor(None, self._client.loop, 0.5)
            await asyncio.sleep(0.01)

    def publish_action(self, action: str, body: dict | None = None) -> None:
        """Send a command to the device."""
        if not self.is_connected:
            _LOGGER.warning("Cannot publish, MQTT not connected to %s", self._device_ip)
            return
        header = {
            "id": str(uuid.uuid4()),
            "type": "request",
            "timestamp": int(time.time() * 1000),
            "action": action,
        }
        payload = encode_binme(header, body or {})
        self._client.publish(self._pub_topic, payload)

    def request_info(self) -> None:
        """Request current device info."""
        self.publish_action("info")

    def set_volume(self, volume: int) -> None:
        """Set volume (0-100)."""
        self.publish_action("set_volume", {"volume": volume, "info_sync": True})

    def set_mute(self, mute: bool, restore_volume: int = 20) -> None:
        """Mute or unmute. Restores to restore_volume when unmuting."""
        if mute:
            self.publish_action("set_volume", {"volume": 0, "info_sync": True})
        else:
            self.publish_action(
                "set_volume", {"volume": restore_volume, "info_sync": True}
            )

    def set_source(self, source: str) -> None:
        """Set audio input source (streaming, lineIn, spdif, usb, speakers)."""
        self.publish_action("set_audio_src", {"source": source})

    def set_loudness(self, enabled: bool) -> None:
        """Enable or disable Dynamic Boost (loudness)."""
        self.publish_action("set_loudness", {"loudness": enabled})

    def set_balance(self, balance: int) -> None:
        """Set stereo balance (-100 to 100)."""
        self.publish_action("set_balance", {"balance": balance, "info_sync": True})

    def set_vol_limit(self, limit: int) -> None:
        """Set maximum volume (0-100)."""
        self.publish_action("set_vol_limit", {"percentage": limit, "info_sync": True})

    def set_eq_enable(self, enabled: bool) -> None:
        """Enable or disable equalizer."""
        self.publish_action("set_eq_enable", {"enable": enabled})

    def set_eq_preset(self, preset: str) -> None:
        """Set EQ preset (custom, music, movie, night, off)."""
        self.publish_action("set_equalizer", {"profile": preset, "info_sync": True})

    def set_sub_crossover(self, crossover: int) -> None:
        """Set subwoofer crossover frequency in Hz."""
        self.publish_action(
            "set_sub_audio", {"crossover": crossover, "info_sync": True}
        )

    def set_sub_level(self, level: int) -> None:
        """Set subwoofer level."""
        self.publish_action("set_sub_audio", {"level": level, "info_sync": True})

    def set_sub_phase(self, phase: int) -> None:
        """Set subwoofer phase (0 or 180)."""
        self.publish_action("set_sub_audio", {"phase": phase, "info_sync": True})

    def set_channels(self, channels: int) -> None:
        """Set channel mode (0=stereo, 1=mono)."""
        self.publish_action("set_channels", {"value": channels})

    def set_screen_brightness(self, brightness: int) -> None:
        """Set screen brightness (0-100)."""
        self.publish_action(
            "set_screen_brightness", {"value": brightness, "info_sync": True}
        )

    def set_led_brightness(self, brightness: int) -> None:
        """Set LED brightness (0-100)."""
        self.publish_action(
            "set_led_brightness", {"value": brightness, "info_sync": True}
        )

    def set_led_color(self, color: str) -> None:
        """Set LED and screen color as hex string (e.g. '0000FF')."""
        self.publish_action(
            "set_color", {"screen": color, "led": color, "info_sync": True}
        )

    def set_persistent_dashboard(self, enabled: bool) -> None:
        """Enable or disable persistent dashboard display."""
        self.publish_action(
            "set_persistent_dashboard", {"enable": enabled, "info_sync": True}
        )

    def locate(self, enable: bool = True) -> None:
        """Flash the device LEDs to locate it."""
        self.publish_action("locate", {"enable": enable})

    def restart(self) -> None:
        """Reboot the device."""
        self.publish_action("reboot")

    async def disconnect(self) -> None:
        """Disconnect cleanly."""
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None
        if self._client:
            self._client.disconnect()
            self._client = None
        self._connected.clear()
