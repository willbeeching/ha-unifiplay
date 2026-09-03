"""MQTT client for direct communication with UniFi Play devices.

Devices authenticate the integration with mutual TLS, and the certificate
authority behind that is not fixed for the life of the hardware: PowerAmp
firmware 1.0.41 rotated it, and every device that took the update stopped
accepting the client certificate this integration had used since 2023 (#20).
The official mobile app was cut off in the same way and shipped a new
certificate to match.

So a single bundled certificate is not a safe assumption, and ``connect()``
tries each generation in ``CERT_GENERATIONS`` until one is accepted.
"""

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
from typing import Any, NamedTuple

import paho.mqtt.client as mqtt

from .const import (
    BINME_FORMAT_JSON,
    BINME_TYPE_BODY,
    BINME_TYPE_HEADER,
    MQTT_KEEPALIVE,
    MQTT_PORT,
    TOPIC_MOBILE,
)

_LOGGER = logging.getLogger(__name__)

CERTS_DIR = Path(__file__).parent / "certs"
CERT_FILE = CERTS_DIR / "mqtt_cert.crt"
KEY_FILE = CERTS_DIR / "mqtt_cert_key.key"


class CertGeneration(NamedTuple):
    """One generation of Ubiquiti-issued MQTT client credentials."""

    name: str
    cert: Path
    key: Path


# Newest first: as firmware rolls out, most devices will want the newer
# generation, and a device that wants the older one is remembered after its
# first connection anyway.
#
# A generation whose files are absent is skipped, so adding support for a new
# CA is a matter of dropping the pair into certs/ under the names listed here
# - there is no code change to make. The 2026 pair is the certificate that
# firmware 1.0.41 expects, extracted from UniFi Play 2.0.2; see docs/api.md.
CERT_GENERATIONS = (
    CertGeneration(
        "2026",
        CERTS_DIR / "mqtt_cert_2026.crt",
        CERTS_DIR / "mqtt_cert_2026_key.key",
    ),
    CertGeneration("2023", CERT_FILE, KEY_FILE),
)

# How long to wait for a CONNACK before deciding the device rejected us.
#
# The wait is what makes the rejection visible at all. Under TLS 1.3 the
# server verifies the client certificate *after* the handshake completes, so
# paho reports a successful connect and the refusal arrives as a bare
# disconnect with no exception and no CONNACK. Only forcing TLS 1.2 turns it
# into a legible "tlsv1 alert unknown ca" (#20). The device is on the LAN and
# has already completed a handshake by this point, so a CONNACK is one round
# trip away and this is generous.
CONNACK_TIMEOUT = 5.0

# Which generation each device accepted, keyed by MAC, so steady-state
# reconnects do not re-probe. Module level because the coordinator builds a
# fresh client for every reconnect, and keyed by MAC rather than IP because
# the answer is a property of the device, not of where it currently is.
#
# A remembered choice is only an ordering hint: the other generations are
# still tried if it stops working, which is exactly what a firmware update
# does to it.
_CERT_CHOICE: dict[str, str] = {}


class MqttCertificateRejected(Exception):
    """No bundled client certificate was accepted by the device."""


class _ConnackTimeout(Exception):
    """The device completed a TLS handshake and then never sent a CONNACK."""


class _ConnackRefused(Exception):
    """The device sent a CONNACK whose reason code is not success."""


def connack_accepted(rc: Any) -> bool:
    """True only when the CONNACK reason code means the broker accepted us.

    ``_on_connect`` fires for every CONNACK, including failure codes such as
    Not authorized. Treating arrival alone as acceptance would cache that
    generation and skip the rest of CERT_GENERATIONS. paho v2 passes a
    ``ReasonCode`` (``is_failure`` is true for values >= 0x80); v1 passed
    an int. 0 is success either way.
    """
    is_failure = getattr(rc, "is_failure", None)
    if isinstance(is_failure, bool):
        return not is_failure
    try:
        return int(rc) == 0
    except (TypeError, ValueError):
        return False


def bundled_generations() -> list[CertGeneration]:
    """Certificate generations whose files are actually present, newest first.

    Shared with discovery.py: the identification probe builds its own paho
    client and must offer the same credentials, or a device on firmware whose
    CA has rotated cannot be discovered at all (#24).
    """
    return [
        generation
        for generation in CERT_GENERATIONS
        if generation.cert.is_file() and generation.key.is_file()
    ]


def encode_binme(header: dict[str, Any], body: dict[str, Any]) -> bytes:
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
        on_event: Callable[[str, dict[str, Any], dict[str, Any]], None] | None = None,
        on_connection: Callable[[], None] | None = None,
    ) -> None:
        self._device_ip = device_ip
        self._device_mac = device_mac.upper().replace(":", "")
        self._on_event = on_event
        self._on_connection = on_connection
        self._client_uuid = uuid.uuid4().hex[:12]
        self._pub_topic = f"{TOPIC_MOBILE}/{self._client_uuid}/action"
        self._client: mqtt.Client | None = None
        self._loop_task: asyncio.Task[None] | None = None
        self._connected = asyncio.Event()
        # Set on any CONNACK so a failure code can wake ``_connect_with``
        # without being mistaken for acceptance. ``_connected`` is only set
        # when the reason code is success.
        self._connack_done = asyncio.Event()
        self._connack_rc: Any = None
        # The event loop these events belong to, captured when the connection
        # is dialled. paho's callbacks arrive on another thread and must not
        # touch an asyncio primitive directly - see ``_signal``.
        self._event_loop: asyncio.AbstractEventLoop | None = None

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected()

    def _signal(self, event: asyncio.Event, *, set_it: bool) -> None:
        """Set or clear an ``asyncio.Event`` from a paho callback thread.

        Every callback below runs inside ``loop()``, on an executor thread.
        ``asyncio.Event.set()`` is not thread-safe: it resolves the waiting
        futures, and resolving a future schedules its callbacks with
        ``loop.call_soon``, which assumes it is on the loop's own thread.

        Without a running loop the event is touched directly, which is only
        reachable from a client that was never dialled.
        """
        loop = self._event_loop
        action = event.set if set_it else event.clear
        if loop is None or loop.is_closed():
            action()
            return
        loop.call_soon_threadsafe(action)

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: Any,
        rc: Any,
        properties: Any = None,
    ) -> None:
        _LOGGER.debug("MQTT CONNACK from %s: %s", self._device_ip, rc)
        self._connack_rc = rc
        if not connack_accepted(rc):
            # A failure CONNACK is a rejection, not a connection. Setting
            # ``_connected`` here would cache this generation and skip the
            # rest of CERT_GENERATIONS.
            self._signal(self._connack_done, set_it=True)
            return
        # Wildcard on the platform segment: PowerAmps publish under UPL-AMP,
        # other hardware under its own prefix (UPL-DEVICE, UPL-PORT, ...), and
        # the broker is the device itself so this matches only its own topics.
        client.subscribe(f"+/{self._device_mac}/status")
        # Notify BEFORE waking connect(). Both hops land on the event loop -
        # the listener through its own call_soon_threadsafe, the events
        # through _signal - so whichever is queued first runs first. Waking
        # connect() first leaves a window where the caller has resumed and
        # started publishing while nothing has yet been told the connection
        # came up, which is a real ordering hazard and not only a flaky test.
        if self._on_connection:
            self._on_connection()
        self._signal(self._connected, set_it=True)
        self._signal(self._connack_done, set_it=True)

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: Any,
        rc: Any,
        properties: Any = None,
    ) -> None:
        _LOGGER.debug("MQTT disconnected from %s: %s", self._device_ip, rc)
        self._signal(self._connected, set_it=False)
        if self._on_connection:
            self._on_connection()

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

    def _setup_tls(self, generation: CertGeneration) -> None:
        """Configure mTLS on the paho client.

        Runs in an executor because paho's ``tls_set`` performs blocking
        file I/O (reading the cert and key) and loads system trust stores.

        ``cert_reqs=CERT_NONE`` is deliberate and unrelated to the client
        credentials: the device presents a self-signed certificate, so the
        verification that matters here runs the other way - the device
        checking us.
        """
        if self._client is None:
            raise RuntimeError("_setup_tls called before MQTT client was instantiated")
        self._client.tls_set(
            certfile=str(generation.cert),
            keyfile=str(generation.key),
            cert_reqs=ssl.CERT_NONE,
            tls_version=ssl.PROTOCOL_TLS_CLIENT,
        )
        self._client.tls_insecure_set(True)

    def _generations_to_try(self) -> list[CertGeneration]:
        """Bundled certificate generations, best candidate first."""
        present = bundled_generations()
        remembered = _CERT_CHOICE.get(self._device_mac)
        if remembered:
            # Stable sort, so the remembered generation moves to the front and
            # the rest keep their newest-first order behind it.
            present.sort(key=lambda generation: generation.name != remembered)
        return present

    async def connect(self) -> None:
        """Connect to the device's MQTT broker.

        Tries each bundled certificate generation in turn. Only a rejection
        is worth retrying with different credentials - anything else (host
        unreachable, connection refused, TCP timeout) means the device is not
        answering at all, so it propagates immediately rather than dialling a
        device that is plainly offline once per generation.
        """
        generations = self._generations_to_try()
        if not generations:
            raise MqttCertificateRejected(
                "No MQTT client certificates are bundled with the integration"
            )

        for index, generation in enumerate(generations):
            try:
                await self._connect_with(generation)
            except (ssl.SSLError, _ConnackTimeout, _ConnackRefused) as err:
                _LOGGER.debug(
                    "%s rejected the %s client certificate: %s",
                    self._device_ip,
                    generation.name,
                    err,
                )
                try:
                    await self.disconnect()
                except Exception:  # noqa: BLE001 - a half-open client can fail any way
                    _LOGGER.debug(
                        "Cleanup after rejected %s certificate for %s failed",
                        generation.name,
                        self._device_ip,
                    )
                if index == len(generations) - 1:
                    raise MqttCertificateRejected(
                        f"{self._device_ip} accepted none of the bundled client "
                        f"certificates ({', '.join(g.name for g in generations)}). "
                        "See issue #20"
                    ) from err
                continue

            if _CERT_CHOICE.get(self._device_mac) != generation.name:
                _LOGGER.info(
                    "MQTT to %s authenticated with the %s client certificate",
                    self._device_ip,
                    generation.name,
                )
            _CERT_CHOICE[self._device_mac] = generation.name
            return

    async def _connect_with(self, generation: CertGeneration) -> None:
        """Dial the device with one certificate generation and await CONNACK."""
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"ha-unifiplay-{self._client_uuid}",
        )
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

        loop = asyncio.get_running_loop()
        self._event_loop = loop
        self._connected.clear()
        self._connack_done.clear()
        self._connack_rc = None

        await loop.run_in_executor(None, self._setup_tls, generation)

        await loop.run_in_executor(
            None, self._client.connect, self._device_ip, MQTT_PORT, MQTT_KEEPALIVE
        )
        # The loop has to be running before the wait: paho only processes the
        # CONNACK from inside loop(), so _on_connect can never fire otherwise.
        self._loop_task = asyncio.create_task(self._mqtt_loop())
        try:
            await asyncio.wait_for(self._connack_done.wait(), CONNACK_TIMEOUT)
        except TimeoutError as err:
            # Raised as our own type so the caller can tell a refused
            # certificate from a TCP-level timeout, which is also a
            # TimeoutError and means something entirely different.
            raise _ConnackTimeout(f"no CONNACK within {CONNACK_TIMEOUT}s") from err
        if not self._connected.is_set():
            raise _ConnackRefused(f"CONNACK {self._connack_rc}")

    async def _mqtt_loop(self) -> None:
        """Run the paho loop in a non-blocking fashion."""
        loop = asyncio.get_running_loop()
        while self._client is not None:
            await loop.run_in_executor(None, self._client.loop, 0.5)
            await asyncio.sleep(0.01)

    def publish_action(self, action: str, body: dict[str, Any] | None = None) -> None:
        """Send a command to the device."""
        if not self.is_connected:
            _LOGGER.warning("Cannot publish, MQTT not connected to %s", self._device_ip)
            return
        client = self._client
        if client is None:  # pragma: no cover - is_connected already proved it
            return
        _LOGGER.debug("Publishing action '%s' to %s", action, self._device_ip)
        header = {
            "id": str(uuid.uuid4()),
            "type": "request",
            "timestamp": int(time.time() * 1000),
            "action": action,
        }
        payload = encode_binme(header, body or {})
        client.publish(self._pub_topic, payload)

    def request_info(self) -> None:
        """Request current device info."""
        self.publish_action("info")

    def request_groups(self) -> None:
        """Request current zone/group state from the device."""
        self.publish_action("groups")

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
        """Set audio input source (streaming, lineIn, spdif for HDMI eARC)."""
        self.publish_action("set_audio_src", {"source": source})

    def set_output(self, out: str) -> None:
        """Set audio output routing (lineOut, spdif, usb) - Audio Port only."""
        self.publish_action("set_audio_src", {"out": out})

    def set_player(self, action: str) -> None:
        """Send a transport command: "play", "pause", "prev" or "next".

        Captured from the official app in #4. The speaker forwards these to
        whatever is streaming to it (Cast, AirPlay, Soundtrack), so they only
        do anything while a streaming session is active.
        """
        self.publish_action("set_player", {"action": action})

    def request_extra_info(self) -> None:
        """Request network/firmware details (platform, version, uptime)."""
        self.publish_action("extra_info")

    def request_metadata(self) -> None:
        """Request current now-playing metadata."""
        self.publish_action("metadata")

    def request_equalizer(self) -> None:
        """Request EQ state (active profile and band table)."""
        self.publish_action("equalizer")

    def request_sub_audio(self) -> None:
        """Request subwoofer state (crossover, level, phase)."""
        self.publish_action("sub_audio")

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

    def set_eq_table(self, table: dict[str, float], profile: str = "custom") -> None:
        """Set the 10-band graphic EQ.

        The whole table goes in every message - the app never sends a single
        band - and edits land on the ``custom`` profile.
        """
        self.publish_action(
            "set_equalizer",
            {"profile": profile, "table": table, "info_sync": True},
        )

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

    def request_features(self) -> None:
        """Query feature-level state the way the official app does on open.

        Each action answers with a status event of the same name (except
        ``get_announcement``, which answers as ``announcement``). All of
        these were captured from the app; none are documented anywhere.
        """
        for action in (
            "alarms",
            "quiet_hours",
            "get_announcement",
            "announce_chime",
            "voice_enhancement",
            "streaming_timeout",
            "announcement_vol",
        ):
            self.publish_action(action)

    def set_streaming_timeout(self, seconds: int) -> None:
        """Set how long streaming mode lingers with no audio (0 = default)."""
        self.publish_action(
            "set_streaming_timeout",
            {"second": seconds, "timestamp": int(time.time())},
        )

    def set_announcement_vol(self, value: int) -> None:
        """Set the announcement playback volume (0-100)."""
        self.publish_action(
            "set_announcement_vol",
            {"value": value, "info_sync": True, "timestamp": int(time.time())},
        )

    def set_voice_enhancement(self, enabled: bool) -> None:
        """Enable or disable voice enhancement for announcements.

        Body shape matches the official app, which pairs ``enable`` with a
        timestamp rather than the ``info_sync`` flag the audio settings use.
        """
        self.publish_action(
            "set_voice_enhancement",
            {"enable": enabled, "timestamp": int(time.time())},
        )

    def set_announce_chime(self, chime: str) -> None:
        """Set the chime played before an announcement."""
        self.publish_action(
            "set_announce_chime", {"chime": chime, "timestamp": int(time.time())}
        )

    def play_announcement(
        self, filename: str, length: int = 0, zone_play: bool = False
    ) -> None:
        """Play a prerecorded announcement immediately.

        Despite the action name the device uses, this fires straight away
        rather than scheduling: it responds with ``announcing: true`` and
        pauses whatever was streaming. ``filename`` needs the ``prerecord/``
        prefix the app sends.
        """
        if not filename.startswith("prerecord/"):
            filename = f"prerecord/{filename}"
        self.publish_action(
            "announce",
            {
                "action": "schedule-announcement",
                "filename": filename,
                "length": length,
                "zone_play": zone_play,
                "enable": True,
            },
        )

    def stop_announcement(self) -> None:
        """Stop an announcement that is currently playing."""
        self.publish_action("announce", {"enable": False})

    def set_alarm(
        self,
        alarm_id: str,
        name: str,
        hour: int,
        minute: int,
        sound: str,
        volume: int,
        duration: int,
        repeat: list[int],
        enabled: bool,
        action: str = "add",
    ) -> None:
        """Create or modify an alarm. ``action`` is "add" or "mod".

        ``repeat`` is a list of weekday numbers, 0 = Sunday, matching the
        app's day picker; an empty list means one-shot.
        """
        self.publish_action(
            "set_alarm",
            {
                "action": action,
                "alarm_id": alarm_id,
                "name": name,
                "hour": hour,
                "minute": minute,
                "sound": sound,
                "volume": volume,
                "duration": duration,
                "repeat": repeat,
                "on": enabled,
                "timestamp": int(time.time()),
            },
        )

    def delete_alarm(self, alarm_id: str) -> None:
        """Delete an alarm by id."""
        self.publish_action("set_alarm", {"action": "del", "alarm_id": alarm_id})

    def stop_alarm(self, alarm_id: str) -> None:
        """Silence an alarm that is currently sounding."""
        self.publish_action("set_alarm", {"action": "stop", "alarm_id": alarm_id})

    def set_quiet_hours(
        self,
        quiet_id: str,
        start_hour: int,
        start_minute: int,
        end_hour: int,
        end_minute: int,
        repeat: list[int],
        wind_down: int = 0,
        action: str = "add",
    ) -> None:
        """Create or modify a quiet-hours window. ``action`` is "add"/"mod"."""
        self.publish_action(
            "set_quiet_hour",
            {
                "action": action,
                "id": quiet_id,
                "start_hour": start_hour,
                "start_minute": start_minute,
                "end_hour": end_hour,
                "end_minute": end_minute,
                "repeat": repeat,
                "wind_down": wind_down,
                "timestamp": int(time.time()),
            },
        )

    def delete_quiet_hours(self, quiet_id: str) -> None:
        """Delete a quiet-hours window by id."""
        self.publish_action("set_quiet_hour", {"action": "del", "id": quiet_id})

    def save_eq_preset(self, name: str, table: dict[str, float]) -> None:
        """Save the given EQ table as a named custom preset."""
        self.publish_action(
            "set_equalizer",
            {
                "profile": "custom",
                "preset_action": "add",
                "preset_name": name,
                "table": table,
                "timestamp": int(time.time()),
            },
        )

    def apply_eq_preset(self, name: str) -> None:
        """Load a saved custom preset's table onto the custom profile.

        Note the field: recall is ``active_preset``, NOT ``preset_name`` or
        ``profile``. Passing the name as ``profile`` is silently accepted and
        does nothing, and ``preset_action: "apply"`` destroys the preset -
        both were tried before the app revealed this.
        """
        self.publish_action(
            "set_equalizer", {"profile": "custom", "active_preset": name}
        )

    def rename_eq_preset(self, name: str, new_name: str) -> None:
        """Rename a saved custom preset."""
        self.publish_action(
            "set_equalizer",
            {
                "profile": "custom",
                "preset_action": "mod",
                "preset_name": name,
                "preset_rename": new_name,
            },
        )

    def delete_eq_preset(self, name: str) -> None:
        """Delete a named custom EQ preset."""
        self.publish_action(
            "set_equalizer",
            {"profile": "custom", "preset_action": "del", "preset_name": name},
        )

    def delete_announcement_file(self, name: str, length: int = 0) -> None:
        """Remove a prerecorded announcement clip from the device.

        Only deletion is offered: ``add_file`` carries just a name and
        duration, so the audio itself must reach the device by some other
        channel the app uses before announcing it here.
        """
        self.publish_action(
            "announce",
            {
                "action": "del_file",
                "files": [{"name": name, "length": length}],
                "file_count": 1,
                "timestamp": int(time.time()),
            },
        )

    def alarm_test(
        self,
        on: bool,
        sound: str = "Lunar Chimes",
        volume: int = 25,
        name: str = "Test",
    ) -> None:
        """Start or stop an audible alarm-sound test.

        The only fire-something-now primitive the device offers: the app
        uses it to preview alarm sounds, and it plays immediately at the
        given volume until stopped.
        """
        if on:
            self.publish_action(
                "alarm_test",
                {"sound": sound, "volume": volume, "name": name, "on": True},
            )
        else:
            self.publish_action("alarm_test", {"on": False})

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
        self._connack_done.clear()
        self._connack_rc = None
