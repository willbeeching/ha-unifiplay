"""Console-less discovery of UniFi Play devices.

Two probe mechanisms, tried in order:

1. **UDP 10001** — the standard Ubiquiti discovery protocol (the same one the
   WiFiman app uses), returning hostname, MAC, IP, platform and firmware.
   Broadcast finds every device on the local subnet; unicast reaches devices
   on other (routed) subnets. PowerAmps (UPL-AMP) answer this; Audio Ports
   (UPL-PORT) have been reported not to (#5).
2. **MQTT identification** — for a manually entered IP that ignores UDP:
   connect to the device's own broker (TCP 8883, the same mTLS channel used
   for control), subscribe with a wildcard, and read the device's retained
   ``UPL-*/<MAC>/status`` topic to learn its MAC and platform, then request
   ``info`` for its name.

This is what lets the integration work without the console's Apollo
application, which Ubiquiti has not released for every console model.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl
import threading
import time
import uuid
from typing import Any

import paho.mqtt.client as mqtt

from .const import MQTT_PORT, TOPIC_MOBILE, parse_firmware_version
from .mqtt_client import (
    CONNACK_TIMEOUT,
    CertGeneration,
    bundled_generations,
    connack_accepted,
    decode_binme,
    encode_binme,
)

_LOGGER = logging.getLogger(__name__)

UBNT_DISCOVERY_PORT = 10001
UBNT_DISCOVERY_PROBE = b"\x01\x00\x00\x00"
DISCOVERY_TIMEOUT = 3.0

# TLV types in a v1 discovery response.
_TLV_HWADDR = 0x01
_TLV_MAC_IP = 0x02
_TLV_FWVERSION = 0x03
_TLV_HOSTNAME = 0x0B
_TLV_PLATFORM = 0x0C
_TLV_MODEL = 0x15


def _parse_response(data: bytes, source_ip: str) -> dict[str, Any] | None:
    """Parse a UBNT discovery v1 response into a device dict."""
    if len(data) < 4 or data[0] != 0x01:
        return None
    out: dict[str, Any] = {"ip": source_ip}
    pos = 4
    while pos + 3 <= len(data):
        tlv_type = data[pos]
        length = int.from_bytes(data[pos + 1 : pos + 3], "big")
        value = data[pos + 3 : pos + 3 + length]
        pos += 3 + length
        if tlv_type == _TLV_MAC_IP and length >= 10:
            out["mac"] = value[:6].hex().upper()
        elif tlv_type == _TLV_HWADDR and length >= 6:
            out.setdefault("mac", value[:6].hex().upper())
        elif tlv_type == _TLV_HOSTNAME:
            out["hostname"] = value.decode("utf-8", errors="replace")
        elif tlv_type == _TLV_PLATFORM:
            out["platform"] = value.decode("utf-8", errors="replace")
        elif tlv_type == _TLV_MODEL:
            out["model"] = value.decode("utf-8", errors="replace")
        elif tlv_type == _TLV_FWVERSION:
            out["fwversion"] = value.decode("utf-8", errors="replace")
    return out if "mac" in out else None


def _is_play_device(parsed: dict[str, Any]) -> bool:
    platform = parsed.get("platform", "")
    model = parsed.get("model", "")
    return bool(platform.startswith("UPL") or model.startswith("UPL"))


def _to_device_dict(parsed: dict[str, Any]) -> dict[str, Any]:
    """Shape a parsed response like an Apollo REST device entry.

    The coordinator's UnifiPlayDeviceState consumes either interchangeably;
    in direct mode the MAC doubles as the device id.
    """
    raw_version = parsed.get("fwversion", "")
    version = parse_firmware_version(raw_version)
    return {
        "id": parsed["mac"],
        "name": parsed.get("hostname", "UniFi Play"),
        "mac": parsed["mac"],
        "platform": parsed.get("platform", parsed.get("model", "UPL")),
        # An unparseable build string is dropped here rather than shown: the
        # UDP sweep is the one path where a later extra_info event will
        # replace it anyway, and a hostname-shaped value in the version field
        # of the device registry is worse than an empty one.
        "firmware": version if version != raw_version else "",
        "ip": parsed["ip"],
    }


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.responses: dict[str, dict[str, Any]] = {}

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        parsed = _parse_response(data, addr[0])
        if parsed is None:
            return
        _LOGGER.debug(
            "Discovery response from %s: %s (%s)",
            addr[0],
            parsed.get("hostname"),
            parsed.get("platform") or parsed.get("model"),
        )
        if _is_play_device(parsed):
            self.responses[parsed["mac"]] = parsed

    def error_received(self, exc: Exception) -> None:
        _LOGGER.debug("Discovery socket error: %s", exc)


async def async_discover(
    manual_hosts: list[str] | None = None,
    timeout: float = DISCOVERY_TIMEOUT,
    broadcast: bool = True,
) -> list[dict[str, Any]]:
    """Probe for Play devices; returns Apollo-shaped device dicts.

    Broadcasts on the local subnet and unicasts to each manual host, then
    listens for the timeout window. Raises OSError only if the socket cannot
    be created at all; unreachable hosts simply produce no response.
    """
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        _DiscoveryProtocol,
        local_addr=("0.0.0.0", 0),
        allow_broadcast=True,
        family=socket.AF_INET,
    )
    try:
        targets = list(manual_hosts or [])
        if broadcast:
            targets.append("255.255.255.255")
        for target in targets:
            try:
                transport.sendto(UBNT_DISCOVERY_PROBE, (target, UBNT_DISCOVERY_PORT))
            except OSError as err:
                _LOGGER.debug("Discovery probe to %s failed: %s", target, err)
        await asyncio.sleep(timeout)
    finally:
        transport.close()
    return [_to_device_dict(p) for p in protocol.responses.values()]


MQTT_PROBE_TIMEOUT = 8.0
# After the retained status message identifies the device, how long to wait
# for the info response that carries its friendly name.
_MQTT_INFO_TIMEOUT = 2.5


def _probe_mqtt_sync(ip: str, timeout: float) -> dict[str, Any] | None:
    """Blocking MQTT identification probe; run via async_probe_mqtt.

    Every Play device runs its own broker and publishes a retained
    ``<platform>/<MAC>/status`` message, so a wildcard subscription
    identifies the device without knowing anything but its IP. Needed for
    UPL-PORT hardware, which does not answer the UDP discovery probe.

    Tries each bundled certificate generation, for the same reason
    UnifiPlayMqttClient.connect does: the CA is firmware-owned and rotates
    per platform. This probe carrying only the 2023 pair is what made an
    Audio Port on firmware 1.1.12 or later impossible to set up at all - it
    failed here, during discovery, before any device existed for the
    coordinator fallback to help (#24).
    """
    for generation in bundled_generations():
        result, rejected = _probe_mqtt_once(ip, timeout, generation)
        if not rejected:
            # Accepted, or a failure no other certificate can fix.
            return result
        _LOGGER.debug(
            "MQTT probe of %s rejected the %s client certificate",
            ip,
            generation.name,
        )
    _LOGGER.debug(
        "MQTT probe of %s: none of the bundled client certificates were "
        "accepted. A device on newer firmware may need one this version does "
        "not carry - see issue #20",
        ip,
    )
    return None


def _probe_mqtt_once(
    ip: str, timeout: float, generation: CertGeneration
) -> tuple[dict[str, Any] | None, bool]:
    """One probe attempt. Returns (device, certificate_was_rejected).

    The flag is True only when trying another generation could help. An
    unreachable device, or one that accepts the certificate and then reports
    nothing, both return False: no certificate fixes either, and retrying
    would multiply the setup timeout by the number of generations bundled.
    """
    found: dict[str, Any] = {"ip": ip}
    got_status = threading.Event()
    got_info = threading.Event()
    # Set on any CONNACK; accepted only when its reason code says so. Under
    # TLS 1.3 a refused certificate arrives as no CONNACK at all, so waiting
    # on this is the only way to tell "wrong certificate" from "device says
    # nothing" - connect() and the handshake both succeed either way (#20).
    connacked = threading.Event()
    accepted: dict[str, bool] = {"ok": False}
    client_uuid = uuid.uuid4().hex[:12]
    action_topic = f"{TOPIC_MOBILE}/{client_uuid}/action"

    def on_connect(
        client: mqtt.Client, userdata: Any, flags: Any, rc: Any, properties: Any = None
    ) -> None:
        accepted["ok"] = connack_accepted(rc)
        connacked.set()
        if not accepted["ok"]:
            return
        client.subscribe("#")

    def on_message(client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage) -> None:
        parts = msg.topic.split("/")
        if (
            len(parts) == 3
            and parts[2] == "status"
            and parts[0].startswith("UPL")
            and not got_status.is_set()
        ):
            found["mac"] = parts[1].upper().replace(":", "")
            found["platform"] = parts[0]
            got_status.set()
            header = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "timestamp": int(time.time() * 1000),
                "action": "info",
            }
            client.publish(action_topic, encode_binme(header, {}))
            return
        try:
            parsed = decode_binme(msg.payload)
        except Exception:  # noqa: BLE001 - unknown payloads are expected here
            return
        header = parsed.get("header", {})
        body = parsed.get("body", {})
        if (
            header.get("name", header.get("action")) == "info"
            and isinstance(body, dict)
            and body.get("deviceName")
        ):
            found["name"] = body["deviceName"]
            got_info.set()

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"ha-unifiplay-probe-{client_uuid}",
    )
    client.on_connect = on_connect
    client.on_message = on_message
    client.tls_set(
        certfile=str(generation.cert),
        keyfile=str(generation.key),
        cert_reqs=ssl.CERT_NONE,
        tls_version=ssl.PROTOCOL_TLS_CLIENT,
    )
    client.tls_insecure_set(True)
    try:
        client.connect(ip, MQTT_PORT, 15)
    except ssl.SSLError as err:
        # TLS 1.2 surfaces a refused certificate here as an unknown-ca alert;
        # under TLS 1.3 it is invisible until the CONNACK never arrives.
        _LOGGER.debug("MQTT probe TLS to %s failed: %s", ip, err)
        return None, True
    except OSError as err:
        # Unreachable, refused, or timed out: not a certificate problem.
        _LOGGER.debug("MQTT probe could not connect to %s: %s", ip, err)
        return None, False
    client.loop_start()
    try:
        connacked.wait(CONNACK_TIMEOUT)
        if not accepted["ok"]:
            return None, True
        got_status.wait(timeout)
        if got_status.is_set():
            got_info.wait(_MQTT_INFO_TIMEOUT)
    finally:
        client.loop_stop()
        client.disconnect()

    if "mac" not in found:
        _LOGGER.debug(
            "MQTT probe connected to %s but saw no retained UPL status topic", ip
        )
        return None, False
    _LOGGER.debug(
        "MQTT probe identified %s: %s (%s)",
        ip,
        found.get("name", "?"),
        found["platform"],
    )
    return {
        "id": found["mac"],
        "name": found.get("name", "UniFi Play"),
        "mac": found["mac"],
        "platform": found["platform"],
        "firmware": "",
        "ip": ip,
    }, False


async def async_probe_mqtt(
    ip: str, timeout: float = MQTT_PROBE_TIMEOUT
) -> dict[str, Any] | None:
    """Identify a Play device by connecting to its MQTT broker."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _probe_mqtt_sync, ip, timeout)


async def async_resolve_direct(
    manual_hosts: list[str] | None = None,
    known_ips: frozenset[str] | set[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Full direct-mode discovery: UDP sweep, then MQTT fallback.

    Manual hosts that answered neither the UDP probe nor a previous scan
    (known_ips) are identified over MQTT — the path UPL-PORT needs.
    """
    manual_hosts = manual_hosts or []
    devices = await async_discover(manual_hosts=manual_hosts)
    answered = {d["ip"] for d in devices}
    to_probe = [h for h in manual_hosts if h not in answered and h not in known_ips]
    if to_probe:
        results = await asyncio.gather(*(async_probe_mqtt(h) for h in to_probe))
        devices.extend(dev for dev in results if dev)
    return devices
