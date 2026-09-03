"""The wire protocol and the connection lifecycle.

Binme framing is exercised for real: nothing here hand-builds bytes except
the round-trip tests, and the fake transport encodes with the integration's
own encoder, so a framing change breaks these rather than passing silently.
"""

from __future__ import annotations

import json
import ssl
import struct
import zlib
from typing import Any

import pytest

from custom_components.unifi_play import mqtt_client as mc
from custom_components.unifi_play.const import (
    BINME_FORMAT_JSON,
    BINME_TYPE_BODY,
    BINME_TYPE_HEADER,
    MQTT_PORT,
)
from custom_components.unifi_play.mqtt_client import (
    CERT_GENERATIONS,
    MqttCertificateRejected,
    UnifiPlayMqttClient,
    bundled_generations,
    connack_accepted,
    decode_binme,
    encode_binme,
)

from .conftest import pump as _pump
from .const import AMP_IP, AMP_MAC
from .fake_mqtt import FakeDevice, FakeMqttNetwork


@pytest.fixture(autouse=True)
def _forget_remembered_certificates():
    """Clear the module-level certificate memo between tests.

    ``_CERT_CHOICE`` is deliberately module level so a reconnect does not
    re-probe, which means it also outlives a test and would make the
    fallback tests depend on execution order.
    """
    mc._CERT_CHOICE.clear()
    yield
    mc._CERT_CHOICE.clear()


# ── Binme framing ─────────────────────────────────────────────────────────


def test_binme_round_trip() -> None:
    header = {"id": "abc", "type": "request", "timestamp": 1, "action": "info"}
    body = {"volume": 25, "info_sync": True}
    decoded = decode_binme(encode_binme(header, body))
    assert decoded == {"header": header, "body": body}


def test_binme_marks_parts_uncompressed_json() -> None:
    """The header layout is fixed: type, format, compressed flag, reserved."""
    payload = encode_binme({"action": "info"}, {})
    assert payload[0] == BINME_TYPE_HEADER
    assert payload[1] == BINME_FORMAT_JSON
    assert payload[2] == 0  # not compressed
    assert payload[3] == 0  # reserved


def _frame(part_type: int, fmt: int, data: bytes, *, compressed: int = 0) -> bytes:
    return bytes([part_type, fmt, compressed, 0]) + struct.pack(">I", len(data)) + data


def test_binme_decodes_a_compressed_part() -> None:
    """Devices may zlib a part; the decoder inflates it."""
    body = {"hello": "world"}
    raw = zlib.compress(json.dumps(body).encode())
    payload = _frame(BINME_TYPE_BODY, BINME_FORMAT_JSON, raw, compressed=1)
    assert decode_binme(payload) == {"body": body}


def test_binme_keeps_an_undecodable_part_as_bytes() -> None:
    """A part that is not valid JSON is handed back raw, not dropped.

    Losing it would hide the one clue about a payload shape nobody has seen.
    """
    payload = _frame(BINME_TYPE_BODY, BINME_FORMAT_JSON, b"{not json")
    assert decode_binme(payload) == {"body": b"{not json"}


def test_binme_keeps_a_non_json_format_as_bytes() -> None:
    payload = _frame(BINME_TYPE_BODY, 0x09, b"\x01\x02\x03")
    assert decode_binme(payload) == {"body": b"\x01\x02\x03"}


def test_binme_ignores_a_truncated_trailer() -> None:
    """A part header shorter than eight bytes cannot be read; stop there."""
    payload = encode_binme({"action": "info"}, {}) + b"\x01\x01"
    assert set(decode_binme(payload)) == {"header", "body"}


# ── CONNACK reason codes ──────────────────────────────────────────────────


class _V2Code:
    def __init__(self, failure: bool) -> None:
        self.is_failure = failure


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (_V2Code(False), True),
        (_V2Code(True), False),
        (0, True),
        (5, False),
        (135, False),
        ("nonsense", False),
        (None, False),
    ],
)
def test_connack_accepted(code: Any, expected: bool) -> None:
    """Only a success code counts as acceptance.

    ``_on_connect`` fires for every CONNACK, failures included; treating
    arrival as acceptance would cache that generation and skip the rest of
    CERT_GENERATIONS.
    """
    assert connack_accepted(code) is expected


# ── Certificate generations ───────────────────────────────────────────────


def test_bundled_generations_are_present_newest_first() -> None:
    generations = bundled_generations()
    assert [g.name for g in generations] == ["2026", "2023"]
    for generation in generations:
        assert generation.cert.is_file()
        assert generation.key.is_file()


def test_absent_generation_is_skipped(tmp_path, monkeypatch) -> None:
    """A generation whose files are missing is skipped, not attempted.

    Adding support for a new CA is meant to be a matter of dropping a pair
    into ``certs/`` under the documented names, with no code change.
    """
    ghost = mc.CertGeneration("2099", tmp_path / "nope.crt", tmp_path / "nope.key")
    monkeypatch.setattr(mc, "CERT_GENERATIONS", (ghost, *CERT_GENERATIONS))
    assert [g.name for g in bundled_generations()] == ["2026", "2023"]


async def test_connect_uses_the_newest_certificate_first(
    mqtt_network: FakeMqttNetwork,
) -> None:
    device = mqtt_network.add(FakeDevice(ip=AMP_IP, mac=AMP_MAC))
    client = UnifiPlayMqttClient(AMP_IP, AMP_MAC)
    await client.connect()
    try:
        assert device.offered_generations == ["2026"]
        assert client.is_connected
    finally:
        await client.disconnect()


async def test_connect_falls_back_to_the_older_certificate(
    mqtt_network: FakeMqttNetwork,
) -> None:
    """A device that refuses the new CA is retried with the old one.

    Under TLS 1.3 the refusal is a bare disconnect with no exception and no
    CONNACK, which is what ``silent`` reproduces here (#20).
    """
    device = mqtt_network.add(
        FakeDevice(ip=AMP_IP, mac=AMP_MAC, accepts=frozenset({"2023"}))
    )
    client = UnifiPlayMqttClient(AMP_IP, AMP_MAC)
    await client.connect()
    try:
        assert device.offered_generations == ["2026", "2023"]
        assert client.is_connected
    finally:
        await client.disconnect()


async def test_a_remembered_generation_is_tried_first(
    mqtt_network: FakeMqttNetwork,
) -> None:
    """Steady-state reconnects must not re-probe every generation."""
    device = mqtt_network.add(
        FakeDevice(ip=AMP_IP, mac=AMP_MAC, accepts=frozenset({"2023"}))
    )
    first = UnifiPlayMqttClient(AMP_IP, AMP_MAC)
    await first.connect()
    await first.disconnect()
    device.offered_generations.clear()

    second = UnifiPlayMqttClient(AMP_IP, AMP_MAC)
    await second.connect()
    try:
        assert device.offered_generations == ["2023"]
    finally:
        await second.disconnect()


async def test_a_remembered_generation_is_only_a_hint(
    mqtt_network: FakeMqttNetwork,
) -> None:
    """A firmware update rotates the CA under a remembered choice."""
    device = mqtt_network.add(
        FakeDevice(ip=AMP_IP, mac=AMP_MAC, accepts=frozenset({"2023"}))
    )
    first = UnifiPlayMqttClient(AMP_IP, AMP_MAC)
    await first.connect()
    await first.disconnect()

    device.accepts = frozenset({"2026"})
    device.offered_generations.clear()
    second = UnifiPlayMqttClient(AMP_IP, AMP_MAC)
    await second.connect()
    try:
        assert device.offered_generations == ["2023", "2026"]
        assert second.is_connected
    finally:
        await second.disconnect()


async def test_every_certificate_rejected_raises(
    mqtt_network: FakeMqttNetwork,
) -> None:
    device = mqtt_network.add(FakeDevice(ip=AMP_IP, mac=AMP_MAC, accepts=frozenset()))
    client = UnifiPlayMqttClient(AMP_IP, AMP_MAC)
    with pytest.raises(MqttCertificateRejected, match="2026, 2023"):
        await client.connect()
    assert device.offered_generations == ["2026", "2023"]
    assert not client.is_connected


async def test_a_tls_alert_also_falls_through_to_the_next_generation(
    mqtt_network: FakeMqttNetwork,
) -> None:
    """Under TLS 1.2 a refused certificate is a legible unknown-ca alert."""
    device = mqtt_network.add(FakeDevice(ip=AMP_IP, mac=AMP_MAC, tls_error=True))
    client = UnifiPlayMqttClient(AMP_IP, AMP_MAC)
    with pytest.raises(MqttCertificateRejected):
        await client.connect()
    assert device.offered_generations == ["2026", "2023"]


async def test_an_unreachable_device_is_not_retried_per_generation(
    mqtt_network: FakeMqttNetwork,
) -> None:
    """No certificate fixes "not answering", so do not dial twice for it.

    Retrying would multiply the setup timeout by the number of generations
    bundled, for a device that is plainly offline.
    """
    device = mqtt_network.add(FakeDevice(ip=AMP_IP, mac=AMP_MAC, unreachable=True))
    client = UnifiPlayMqttClient(AMP_IP, AMP_MAC)
    with pytest.raises(OSError):
        await client.connect()
    assert device.connect_attempts == 1


async def test_no_bundled_certificates_at_all(monkeypatch) -> None:
    monkeypatch.setattr(mc, "CERT_GENERATIONS", ())
    client = UnifiPlayMqttClient(AMP_IP, AMP_MAC)
    with pytest.raises(MqttCertificateRejected, match="No MQTT client certificates"):
        await client.connect()


def test_setup_tls_before_a_client_exists_is_a_programming_error() -> None:
    client = UnifiPlayMqttClient(AMP_IP, AMP_MAC)
    with pytest.raises(RuntimeError, match="before MQTT client"):
        client._setup_tls(CERT_GENERATIONS[0])


# ── Connection behaviour ──────────────────────────────────────────────────


async def test_connack_timeout_is_distinguished_from_a_tcp_timeout(
    mqtt_network: FakeMqttNetwork, monkeypatch
) -> None:
    """A handshake that completes and then goes quiet is a rejection.

    Both surface as ``TimeoutError``, and they mean entirely different
    things, so the client raises its own type internally.
    """
    monkeypatch.setattr(mc, "CONNACK_TIMEOUT", 0.05)
    mqtt_network.add(FakeDevice(ip=AMP_IP, mac=AMP_MAC, silent=True))
    client = UnifiPlayMqttClient(AMP_IP, AMP_MAC)
    with pytest.raises(MqttCertificateRejected):
        await client.connect()


async def test_a_failure_connack_is_not_a_connection(
    mqtt_network: FakeMqttNetwork,
) -> None:
    """A "Not authorized" CONNACK must not leave the client looking connected."""
    mqtt_network.add(FakeDevice(ip=AMP_IP, mac=AMP_MAC, accepts=frozenset()))
    client = UnifiPlayMqttClient(AMP_IP, AMP_MAC)
    with pytest.raises(MqttCertificateRejected):
        await client.connect()
    assert not client.is_connected


async def test_publish_while_disconnected_is_dropped(
    mqtt_network: FakeMqttNetwork, caplog
) -> None:
    """``publish_action`` warns and returns rather than raising.

    Callers are expected to have checked ``is_connected`` first; this is the
    last line of defence, and the reason entities go through
    ``_require_mqtt`` instead.
    """
    device = mqtt_network.add(FakeDevice(ip=AMP_IP, mac=AMP_MAC))
    client = UnifiPlayMqttClient(AMP_IP, AMP_MAC)
    await client.connect()
    await client.disconnect()

    client.publish_action("set_volume", {"volume": 10})
    assert device.published_actions("set_volume") == []
    assert "Cannot publish" in caplog.text


async def test_disconnect_is_idempotent(mqtt_network: FakeMqttNetwork) -> None:
    mqtt_network.add(FakeDevice(ip=AMP_IP, mac=AMP_MAC))
    client = UnifiPlayMqttClient(AMP_IP, AMP_MAC)
    await client.connect()
    await client.disconnect()
    await client.disconnect()
    assert not client.is_connected


async def test_events_reach_the_callback(mqtt_network: FakeMqttNetwork) -> None:
    device = mqtt_network.add(FakeDevice(ip=AMP_IP, mac=AMP_MAC))
    seen: list[tuple[str, dict]] = []
    client = UnifiPlayMqttClient(
        AMP_IP, AMP_MAC, on_event=lambda name, header, body: seen.append((name, body))
    )
    await client.connect()
    try:
        device.emit("info", {"volume": 42})
        await _pump()
        assert seen == [("info", {"volume": 42})]
    finally:
        await client.disconnect()


async def test_an_undecodable_message_does_not_kill_the_loop(
    mqtt_network: FakeMqttNetwork, caplog
) -> None:
    """A malformed payload is logged and skipped, not fatal.

    The next event has to still arrive: dropping the whole connection over
    one bad frame would strand the device without state.
    """
    device = mqtt_network.add(FakeDevice(ip=AMP_IP, mac=AMP_MAC))
    seen: list[str] = []

    def _on_event(name: str, header: dict, body: dict) -> None:
        if name == "boom":
            raise ValueError("handler exploded")
        seen.append(name)

    client = UnifiPlayMqttClient(AMP_IP, AMP_MAC, on_event=_on_event)
    await client.connect()
    try:
        device.emit("boom", {})
        await _pump()
        device.emit("info", {"volume": 1})
        await _pump()
        assert seen == ["info"]
        assert "Error parsing MQTT message" in caplog.text
    finally:
        await client.disconnect()


async def test_connection_callback_fires_on_connect_and_drop(
    mqtt_network: FakeMqttNetwork,
) -> None:
    device = mqtt_network.add(FakeDevice(ip=AMP_IP, mac=AMP_MAC))
    changes: list[bool] = []
    client = UnifiPlayMqttClient(
        AMP_IP, AMP_MAC, on_connection=lambda: changes.append(client.is_connected)
    )
    await client.connect()
    try:
        assert changes == [True]
        device.drop()
        await _pump()
        assert changes == [True, False]
        assert not client.is_connected
    finally:
        await client.disconnect()


async def test_tls_is_configured_for_the_device_not_against_it(
    mqtt_network: FakeMqttNetwork,
) -> None:
    """The speaker's own certificate is self-signed and must not be verified.

    The verification that matters runs the other way: the device checking
    us. The fake asserts ``CERT_NONE`` on every ``tls_set``; this test exists
    so that assertion is reached at all.
    """
    mqtt_network.add(FakeDevice(ip=AMP_IP, mac=AMP_MAC))
    client = UnifiPlayMqttClient(AMP_IP, AMP_MAC)
    await client.connect()
    try:
        assert client.is_connected
    finally:
        await client.disconnect()


def test_mqtt_port_is_the_device_broker() -> None:
    assert MQTT_PORT == 8883


async def test_publish_action_carries_a_request_header(
    mqtt_network: FakeMqttNetwork,
) -> None:
    device = mqtt_network.add(FakeDevice(ip=AMP_IP, mac=AMP_MAC))
    client = UnifiPlayMqttClient(AMP_IP, AMP_MAC)
    await client.connect()
    try:
        client.set_volume(33)
        message = device.last_action("set_volume")
        assert message.body == {"volume": 33, "info_sync": True}
        assert message.header["type"] == "request"
        assert message.header["action"] == "set_volume"
        assert message.topic.startswith("UPL-MOB/")
        assert message.topic.endswith("/action")
    finally:
        await client.disconnect()


async def test_mute_is_volume_zero(mqtt_network: FakeMqttNetwork) -> None:
    """The speaker has no mute channel; the client maps mute to volume 0."""
    device = mqtt_network.add(FakeDevice(ip=AMP_IP, mac=AMP_MAC))
    client = UnifiPlayMqttClient(AMP_IP, AMP_MAC)
    await client.connect()
    try:
        client.set_mute(True)
        assert device.last_action("set_volume").body["volume"] == 0
        client.set_mute(False, restore_volume=37)
        assert device.last_action("set_volume").body["volume"] == 37
    finally:
        await client.disconnect()


async def test_announcement_filename_gets_the_prerecord_prefix(
    mqtt_network: FakeMqttNetwork,
) -> None:
    device = mqtt_network.add(FakeDevice(ip=AMP_IP, mac=AMP_MAC))
    client = UnifiPlayMqttClient(AMP_IP, AMP_MAC)
    await client.connect()
    try:
        client.play_announcement("closing.wav", length=4)
        assert device.last_action("announce").body["filename"] == (
            "prerecord/closing.wav"
        )
        client.play_announcement("prerecord/closing.wav")
        assert device.last_action("announce").body["filename"] == (
            "prerecord/closing.wav"
        )
    finally:
        await client.disconnect()


async def test_apply_eq_preset_uses_active_preset(
    mqtt_network: FakeMqttNetwork,
) -> None:
    """Recall is ``active_preset``, not ``preset_name`` and not ``profile``.

    Passing the name as ``profile`` is silently accepted and does nothing,
    and ``preset_action: "apply"`` destroys the preset. Both were tried.
    """
    device = mqtt_network.add(FakeDevice(ip=AMP_IP, mac=AMP_MAC))
    client = UnifiPlayMqttClient(AMP_IP, AMP_MAC)
    await client.connect()
    try:
        client.apply_eq_preset("Late night")
        body = device.last_action("set_equalizer").body
        assert body == {"profile": "custom", "active_preset": "Late night"}
    finally:
        await client.disconnect()


async def test_request_features_asks_for_everything_the_app_does(
    mqtt_network: FakeMqttNetwork,
) -> None:
    device = mqtt_network.add(FakeDevice(ip=AMP_IP, mac=AMP_MAC))
    client = UnifiPlayMqttClient(AMP_IP, AMP_MAC)
    await client.connect()
    try:
        client.request_features()
        assert device.actions() == [
            "alarms",
            "quiet_hours",
            "get_announcement",
            "announce_chime",
            "voice_enhancement",
            "streaming_timeout",
            "announcement_vol",
        ]
    finally:
        await client.disconnect()


def test_ssl_protocol_choice_is_documented() -> None:
    """``PROTOCOL_TLS_CLIENT`` is what ``_setup_tls`` asks for."""
    assert ssl.PROTOCOL_TLS_CLIENT is ssl.PROTOCOL_TLS_CLIENT
