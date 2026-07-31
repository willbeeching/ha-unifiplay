"""Console-less discovery of UniFi Play devices.

Play devices answer the standard Ubiquiti discovery protocol on UDP 10001
(the same one the WiFiman app and the console's own discovery use), returning
their hostname, MAC, IP, platform and firmware. A broadcast probe finds every
device on the local subnet; a unicast probe reaches devices on other (routed)
subnets, so users can list those IPs explicitly.

This is what lets the integration work without the console's Apollo
application, which Ubiquiti has not released for every console model.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
from typing import Any

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

# "UPL-AMP.qcs405.v1.0.38.37ed30f.260312.07:19:19" -> "1.0.38"
_FW_VERSION_RE = re.compile(r"\.v(\d+(?:\.\d+)+)\.")


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
    return platform.startswith("UPL") or model.startswith("UPL")


def _to_device_dict(parsed: dict[str, Any]) -> dict[str, Any]:
    """Shape a parsed response like an Apollo REST device entry.

    The coordinator's UnifiPlayDeviceState consumes either interchangeably;
    in direct mode the MAC doubles as the device id.
    """
    fw_match = _FW_VERSION_RE.search(parsed.get("fwversion", ""))
    return {
        "id": parsed["mac"],
        "name": parsed.get("hostname", "UniFi Play"),
        "mac": parsed["mac"],
        "platform": parsed.get("platform", parsed.get("model", "UPL")),
        "firmware": fw_match.group(1) if fw_match else "",
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
