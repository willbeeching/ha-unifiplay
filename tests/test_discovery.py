"""Console-less discovery: the UDP sweep and the MQTT identification probe."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from custom_components.unifi_play import discovery
from custom_components.unifi_play.discovery import (
    UBNT_DISCOVERY_PORT,
    UBNT_DISCOVERY_PROBE,
    _is_play_device,
    _parse_response,
    _to_device_dict,
    async_discover,
    async_probe_mqtt,
    async_resolve_direct,
)

from .const import PORT_IP, PORT_MAC, amp_device
from .fake_mqtt import FakeDevice, FakeMqttNetwork

# ── UBNT discovery response parsing ───────────────────────────────────────

_TLV_HWADDR = 0x01
_TLV_MAC_IP = 0x02
_TLV_FWVERSION = 0x03
_TLV_HOSTNAME = 0x0B
_TLV_PLATFORM = 0x0C
_TLV_MODEL = 0x15


def _tlv(tlv_type: int, value: bytes) -> bytes:
    return bytes([tlv_type]) + len(value).to_bytes(2, "big") + value


def _response(*parts: bytes) -> bytes:
    # Version 1, then three bytes the parser skips before the first TLV.
    return b"\x01\x00\x00\x00" + b"".join(parts)


def test_parses_a_poweramp_response() -> None:
    """Captured shape: MAC+IP, hostname, platform and a versioned firmware."""
    data = _response(
        _tlv(_TLV_MAC_IP, bytes.fromhex("AABBCCDDEEFF") + b"\xc0\xa8\x01\x64"),
        _tlv(_TLV_HOSTNAME, b"Living Room"),
        _tlv(_TLV_PLATFORM, b"UPL-AMP"),
        _tlv(
            _TLV_FWVERSION,
            b"UPL-AMP.qcs405.v1.0.38.37ed30f.260312.07:19:19",
        ),
    )
    parsed = _parse_response(data, "192.168.1.100")
    assert parsed is not None
    assert _is_play_device(parsed)
    assert _to_device_dict(parsed) == {
        "id": "AABBCCDDEEFF",
        "name": "Living Room",
        "mac": "AABBCCDDEEFF",
        "platform": "UPL-AMP",
        "firmware": "1.0.38",
        "ip": "192.168.1.100",
    }


def test_hwaddr_is_only_a_fallback_for_the_mac() -> None:
    """``_TLV_MAC_IP`` wins when both are present; it carries the real pair."""
    data = _response(
        _tlv(_TLV_HWADDR, bytes.fromhex("111111111111")),
        _tlv(_TLV_MAC_IP, bytes.fromhex("222222222222") + b"\xc0\xa8\x01\x64"),
        _tlv(_TLV_PLATFORM, b"UPL-PORT"),
    )
    parsed = _parse_response(data, "192.168.1.101")
    assert parsed is not None
    assert parsed["mac"] == "222222222222"


def test_hwaddr_alone_is_enough() -> None:
    data = _response(
        _tlv(_TLV_HWADDR, bytes.fromhex("111111111111")),
        _tlv(_TLV_MODEL, b"UPL-PORT"),
    )
    parsed = _parse_response(data, "192.168.1.101")
    assert parsed is not None
    assert parsed["mac"] == "111111111111"
    assert _is_play_device(parsed)


def test_a_response_without_a_mac_is_discarded() -> None:
    data = _response(_tlv(_TLV_HOSTNAME, b"Something else"))
    assert _parse_response(data, "192.168.1.9") is None


@pytest.mark.parametrize("data", [b"", b"\x01\x00", b"\x02\x00\x00\x00"])
def test_a_response_that_is_not_version_one_is_discarded(data: bytes) -> None:
    assert _parse_response(data, "192.168.1.9") is None


def test_a_non_play_device_is_ignored() -> None:
    """A discovery broadcast reaches every Ubiquiti device on the subnet."""
    parsed = {"mac": "AA", "platform": "U6-Pro", "model": "U6-Pro"}
    assert not _is_play_device(parsed)


def test_firmware_falls_back_to_empty_when_unversioned() -> None:
    parsed = {"mac": "AA", "platform": "UPL-AMP", "ip": "1.2.3.4", "fwversion": "?"}
    assert _to_device_dict(parsed)["firmware"] == ""


def test_platform_falls_back_to_model_then_to_upl() -> None:
    assert (
        _to_device_dict({"mac": "AA", "ip": "1.2.3.4", "model": "UPL-PORT"})["platform"]
        == "UPL-PORT"
    )
    assert _to_device_dict({"mac": "AA", "ip": "1.2.3.4"})["platform"] == "UPL"


def test_undecodable_text_is_replaced_not_fatal() -> None:
    """Firmware has been seen to put non-UTF-8 bytes in a hostname."""
    data = _response(
        _tlv(_TLV_HWADDR, bytes.fromhex("111111111111")),
        _tlv(_TLV_HOSTNAME, b"Caf\xe9"),
        _tlv(_TLV_PLATFORM, b"UPL-AMP"),
    )
    parsed = _parse_response(data, "192.168.1.9")
    assert parsed is not None
    assert "Caf" in parsed["hostname"]


# ── The UDP sweep ─────────────────────────────────────────────────────────


class _FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []
        self.closed = False
        self.fail_for: set[str] = set()

    def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
        if addr[0] in self.fail_for:
            raise OSError("network unreachable")
        self.sent.append((data, addr))

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def udp_transport(monkeypatch) -> _FakeTransport:
    """Stand in for the datagram endpoint, one level below async_discover."""
    transport = _FakeTransport()
    protocol = discovery._DiscoveryProtocol()

    async def _create_datagram_endpoint(factory, **kwargs):
        assert kwargs["allow_broadcast"] is True
        return transport, protocol

    loop = asyncio.get_event_loop()
    monkeypatch.setattr(
        loop, "create_datagram_endpoint", _create_datagram_endpoint, raising=False
    )
    transport.protocol = protocol  # type: ignore[attr-defined]
    return transport


async def test_sweep_broadcasts_and_unicasts(udp_transport: _FakeTransport) -> None:
    """Broadcast finds the local subnet; a manual host reaches another VLAN."""
    with patch.object(discovery, "DISCOVERY_TIMEOUT", 0):
        found = await async_discover(manual_hosts=["10.0.0.5"], timeout=0)

    targets = [addr[0] for _, addr in udp_transport.sent]
    assert targets == ["10.0.0.5", "255.255.255.255"]
    assert all(port == UBNT_DISCOVERY_PORT for _, (_, port) in udp_transport.sent)
    assert all(probe == UBNT_DISCOVERY_PROBE for probe, _ in udp_transport.sent)
    assert found == []
    assert udp_transport.closed


async def test_sweep_can_skip_the_broadcast(udp_transport: _FakeTransport) -> None:
    await async_discover(manual_hosts=["10.0.0.5"], timeout=0, broadcast=False)
    assert [addr[0] for _, addr in udp_transport.sent] == ["10.0.0.5"]


async def test_an_unroutable_target_does_not_abort_the_sweep(
    udp_transport: _FakeTransport,
) -> None:
    """One host on a VLAN with no route must not stop the others."""
    udp_transport.fail_for = {"10.0.0.5"}
    await async_discover(manual_hosts=["10.0.0.5", "10.0.0.6"], timeout=0)
    assert [addr[0] for _, addr in udp_transport.sent] == [
        "10.0.0.6",
        "255.255.255.255",
    ]


async def test_sweep_returns_what_answered(udp_transport: _FakeTransport) -> None:
    protocol = udp_transport.protocol  # type: ignore[attr-defined]
    protocol.datagram_received(
        _response(
            _tlv(_TLV_HWADDR, bytes.fromhex("AABBCCDDEEFF")),
            _tlv(_TLV_PLATFORM, b"UPL-AMP"),
            _tlv(_TLV_HOSTNAME, b"Living Room"),
        ),
        ("192.168.1.100", 10001),
    )
    protocol.datagram_received(b"garbage", ("192.168.1.7", 10001))
    protocol.error_received(OSError("icmp unreachable"))

    found = await async_discover(timeout=0)
    assert [d["mac"] for d in found] == ["AABBCCDDEEFF"]


# ── The MQTT identification probe ─────────────────────────────────────────


async def test_probe_identifies_a_device_from_its_retained_topic(
    mqtt_network: FakeMqttNetwork,
) -> None:
    """Port hardware does not answer the UDP probe (#5).

    Its broker publishes a retained ``<platform>/<MAC>/status``, so a
    wildcard subscription identifies it knowing nothing but the IP.
    """
    mqtt_network.add(
        FakeDevice(
            ip=PORT_IP,
            mac=PORT_MAC,
            platform="UPL-DEVICE",
            name="Kitchen",
            auto_answer_info=True,
        )
    )
    device = await async_probe_mqtt(PORT_IP, timeout=1)
    assert device == {
        "id": PORT_MAC,
        "name": "Kitchen",
        "mac": PORT_MAC,
        "platform": "UPL-DEVICE",
        "firmware": "",
        "ip": PORT_IP,
    }


async def test_probe_offers_every_bundled_certificate(
    mqtt_network: FakeMqttNetwork,
) -> None:
    """The probe carrying only the 2023 pair made a rotated Port unsetupable.

    It failed here, during discovery, before any device existed for the
    coordinator's own fallback to help (#24).
    """
    fake = mqtt_network.add(
        FakeDevice(
            ip=PORT_IP,
            mac=PORT_MAC,
            accepts=frozenset({"2023"}),
            auto_answer_info=True,
        )
    )
    device = await async_probe_mqtt(PORT_IP, timeout=1)
    assert device is not None
    assert fake.offered_generations == ["2026", "2023"]


async def test_probe_gives_up_when_no_certificate_is_accepted(
    mqtt_network: FakeMqttNetwork,
) -> None:
    fake = mqtt_network.add(FakeDevice(ip=PORT_IP, mac=PORT_MAC, accepts=frozenset()))
    assert await async_probe_mqtt(PORT_IP, timeout=0.2) is None
    assert fake.offered_generations == ["2026", "2023"]


async def test_probe_does_not_retry_an_unreachable_host(
    mqtt_network: FakeMqttNetwork,
) -> None:
    """No certificate fixes "not answering"; retrying multiplies the timeout."""
    fake = mqtt_network.add(FakeDevice(ip=PORT_IP, mac=PORT_MAC, unreachable=True))
    assert await async_probe_mqtt(PORT_IP, timeout=0.2) is None
    assert fake.connect_attempts == 1


async def test_probe_retries_the_next_generation_after_a_tls_alert(
    mqtt_network: FakeMqttNetwork,
) -> None:
    fake = mqtt_network.add(FakeDevice(ip=PORT_IP, mac=PORT_MAC, tls_error=True))
    assert await async_probe_mqtt(PORT_IP, timeout=0.2) is None
    assert fake.offered_generations == ["2026", "2023"]


async def test_probe_of_a_broker_that_says_nothing(
    mqtt_network: FakeMqttNetwork,
) -> None:
    """Connected, but no retained UPL topic: not a certificate problem."""

    class _Quiet(FakeDevice):
        pass

    fake = mqtt_network.add(_Quiet(ip=PORT_IP, mac=PORT_MAC))
    # Suppress the retained status delivery by making the topic unrecognisable.
    fake.platform = "SOMETHING-ELSE"
    assert await async_probe_mqtt(PORT_IP, timeout=0.2) is None
    assert fake.connect_attempts == 1


async def test_probe_without_an_info_answer_still_identifies(
    mqtt_network: FakeMqttNetwork,
) -> None:
    """The name is best-effort; the MAC and platform are what matter."""
    mqtt_network.add(FakeDevice(ip=PORT_IP, mac=PORT_MAC, platform="UPL-PORT"))
    with patch.object(discovery, "_MQTT_INFO_TIMEOUT", 0.05):
        device = await async_probe_mqtt(PORT_IP, timeout=0.5)
    assert device is not None
    assert device["name"] == "UniFi Play"


# ── Full direct resolution ────────────────────────────────────────────────


async def test_resolve_direct_probes_only_hosts_that_did_not_answer(
    mqtt_network: FakeMqttNetwork,
) -> None:
    """UDP first, MQTT only for what it missed.

    A manual host already tracked as a device is skipped entirely: there is
    no point opening a second TLS connection to a speaker already held.
    """
    silent_port = mqtt_network.add(
        FakeDevice(ip=PORT_IP, mac=PORT_MAC, auto_answer_info=True)
    )
    already_known = mqtt_network.add(FakeDevice(ip="192.168.1.200", mac="AABBCCDDEE99"))

    async def _sweep(
        manual_hosts: list[str] | None = None,
        timeout: float = 0.0,
        broadcast: bool = True,
    ) -> list[dict[str, Any]]:
        return [amp_device()]

    with patch.object(discovery, "async_discover", new=_sweep):
        found = await async_resolve_direct(
            manual_hosts=[PORT_IP, "192.168.1.200"],
            known_ips={"192.168.1.200"},
        )

    assert sorted(d["ip"] for d in found) == ["192.168.1.100", PORT_IP]
    assert silent_port.connect_attempts == 1
    assert already_known.connect_attempts == 0


async def test_resolve_direct_skips_a_host_the_sweep_already_found(
    mqtt_network: FakeMqttNetwork,
) -> None:
    port_fake = mqtt_network.add(FakeDevice(ip="192.168.1.100", mac="AABBCCDDEEFF"))

    async def _sweep(
        manual_hosts: list[str] | None = None,
        timeout: float = 0.0,
        broadcast: bool = True,
    ) -> list[dict[str, Any]]:
        return [amp_device()]

    with patch.object(discovery, "async_discover", new=_sweep):
        found = await async_resolve_direct(manual_hosts=["192.168.1.100"])

    assert len(found) == 1
    assert port_fake.connect_attempts == 0
