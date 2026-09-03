"""A fake paho-mqtt transport for the UniFi Play tests.

The integration has no protocol documentation and no hardware in CI, so the
only honest place to cut a seam is the *transport*: everything above
``paho.mqtt.client.Client`` - Binme framing, the certificate-generation
fallback, CONNACK handling, the coordinator's event dispatch - runs for real
against this fake. Patching ``UnifiPlayMqttClient`` methods instead would
test the mock.

Two objects:

``FakeDevice``
    What one speaker at one IP does: which certificate generations it
    accepts, whether it answers at all, and what it publishes. Tests drive a
    device by calling :meth:`FakeDevice.emit`, which encodes a real Binme
    frame and hands it to whatever client is subscribed.

``FakeMqttNetwork``
    The address book. ``paho.mqtt.client.Client`` is replaced by a factory
    that looks the dialled IP up here, so a test can describe a network of
    speakers without knowing how the integration builds its clients.

Callbacks fire from ``loop()``, exactly as paho's do, because that ordering
is load-bearing: ``UnifiPlayMqttClient._connect_with`` starts its loop task
*before* waiting for the CONNACK precisely because a CONNACK cannot arrive
without one.
"""

from __future__ import annotations

import ssl
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import paho.mqtt.client as mqtt

from custom_components.unifi_play.mqtt_client import encode_binme

#: Reason code a broker sends when it accepts the connection.
CONNACK_ACCEPTED = 0
#: MQTT 5 "Not authorized". Used for a device that answers but refuses.
CONNACK_NOT_AUTHORIZED = 135


class _ReasonCode:
    """Stand-in for paho's ``ReasonCode``.

    ``connack_accepted`` branches on ``is_failure`` before falling back to
    ``int()``, and only a value carrying that attribute exercises the paho-v2
    branch. Codes >= 0x80 are failures, matching paho.
    """

    def __init__(self, value: int) -> None:
        self.value = value
        self.is_failure = value >= 0x80

    def __int__(self) -> int:
        return self.value

    def __repr__(self) -> str:
        return f"ReasonCode({self.value})"


@dataclass
class _Published:
    """One message a client sent to a device."""

    topic: str
    payload: bytes

    @property
    def header(self) -> dict[str, Any]:
        from custom_components.unifi_play.mqtt_client import decode_binme

        return dict(decode_binme(self.payload).get("header", {}))

    @property
    def body(self) -> dict[str, Any]:
        from custom_components.unifi_play.mqtt_client import decode_binme

        return dict(decode_binme(self.payload).get("body", {}))

    @property
    def action(self) -> str:
        value = self.header.get("action", "")
        return value if isinstance(value, str) else ""


@dataclass
class FakeDevice:
    """One speaker, addressed by IP.

    ``accepts`` names the certificate generations this device will take, by
    the names in ``CERT_GENERATIONS`` ("2026", "2023"). An empty set is a
    device whose CA has rotated past everything bundled.
    """

    ip: str
    mac: str
    platform: str = "UPL-PORT"
    accepts: frozenset[str] = frozenset({"2026", "2023"})

    #: Raise ``OSError`` from ``connect()``: the device is not answering at
    #: all. Distinct from a rejected certificate, and the integration must
    #: not retry the other generations for it.
    unreachable: bool = False
    #: Complete the TLS handshake and then never send a CONNACK. This is what
    #: a refused client certificate looks like under TLS 1.3 (#20).
    silent: bool = False
    #: Raise ``ssl.SSLError`` from ``connect()``: a refused certificate under
    #: TLS 1.2, where the alert is legible.
    tls_error: bool = False
    #: Whether the device answers the UDP discovery probe. Audio Ports have
    #: been reported not to (#5), which is the whole reason the MQTT
    #: identification fallback exists.
    udp_visible: bool = True
    #: Firmware version, as the UDP probe reports it.
    firmware: str = ""
    #: Friendly name, answered to the ``info`` request the MQTT probe sends.
    name: str = "UniFi Play"
    #: Answer an ``info`` request with ``{"deviceName": name}``.
    #:
    #: Real devices always do. It is off by default because most tests drive
    #: state explicitly with :meth:`emit` and a spontaneous half-populated
    #: ``info`` event in the middle of that is noise; the discovery probe
    #: needs it, so ``discovery_network`` turns it on.
    auto_answer_info: bool = False

    #: Every message the integration published to this device, in order.
    published: list[_Published] = field(default_factory=list)
    #: Topics the integration subscribed to.
    subscriptions: list[str] = field(default_factory=list)
    #: How many times a client has dialled this device.
    connect_attempts: int = 0
    #: Certificate generations offered, in the order they were tried.
    offered_generations: list[str] = field(default_factory=list)

    _clients: list[_FakeClient] = field(default_factory=list)

    # ── Assertions ────────────────────────────────────────────────────────

    def actions(self) -> list[str]:
        """The action name of every published message, in order."""
        return [msg.action for msg in self.published]

    def published_actions(self, action: str) -> list[_Published]:
        """Every published message with this action name."""
        return [msg for msg in self.published if msg.action == action]

    def last_action(self, action: str) -> _Published:
        """The most recent message with this action name."""
        matches = self.published_actions(action)
        if not matches:
            raise AssertionError(
                f"{self.ip} was never sent {action!r}; saw {self.actions()}"
            )
        return matches[-1]

    def clear(self) -> None:
        """Forget what has been published so far."""
        self.published.clear()

    # ── Driving the device ────────────────────────────────────────────────

    def emit(self, event: str, body: Any) -> None:
        """Push an event to every connected client, as the real device does.

        The payload is encoded with the integration's own ``encode_binme``
        and decoded by its own ``decode_binme``, so a framing change breaks
        these tests rather than passing silently.
        """
        header = {"id": "fake", "type": "event", "timestamp": 0, "name": event}
        payload = encode_binme(header, body)
        topic = f"{self.platform}/{self.mac}/status"
        for client in list(self._clients):
            client._deliver(topic, payload)

    def drop(self, reason: int = 7) -> None:
        """Drop the connection from the device's end.

        The default reason code is paho's "connection lost"; the integration
        only cares that a disconnect happened.
        """
        for client in list(self._clients):
            client._drop(reason)


class _FakeClient:
    """Replacement for ``paho.mqtt.client.Client``.

    Only the surface the integration actually uses is implemented, so a call
    to anything else fails loudly rather than passing silently - which is the
    same class of drift ``scripts/check_mqtt_client_calls.py`` guards against
    in the other direction.
    """

    def __init__(
        self,
        network: FakeMqttNetwork,
        callback_api_version: Any = None,
        client_id: str = "",
        **_: Any,
    ) -> None:
        self._network = network
        self.client_id = client_id
        self.on_connect: Callable[..., None] | None = None
        self.on_disconnect: Callable[..., None] | None = None
        self.on_message: Callable[..., None] | None = None

        self._device: FakeDevice | None = None
        self._connected = False
        self._generation: str | None = None
        self._certfile: str | None = None
        # Callbacks waiting for the next loop() call, mirroring paho, which
        # does nothing at all until its network loop runs.
        self._pending: list[Callable[[], None]] = []
        self._lock = threading.Lock()
        self._loop_thread: threading.Thread | None = None
        self._loop_stop = threading.Event()

    # ── TLS ───────────────────────────────────────────────────────────────

    def tls_set(
        self,
        certfile: str | None = None,
        keyfile: str | None = None,
        cert_reqs: Any = None,
        tls_version: Any = None,
        **_: Any,
    ) -> None:
        self._certfile = certfile
        self._generation = self._network.generation_for(certfile)
        # The integration must never verify the speaker's own certificate:
        # it is self-signed, and the verification that matters runs the
        # other way. Asserting here stops a "hardening" change that would
        # break every connection.
        assert cert_reqs == ssl.CERT_NONE, "the speaker's cert is self-signed"

    def tls_insecure_set(self, value: bool) -> None:
        assert value is True

    # ── Connection ────────────────────────────────────────────────────────

    def connect(self, host: str, port: int, keepalive: int = 60) -> int:
        device = self._network.device_at(host)
        if device is None:
            raise OSError(f"no fake device at {host}")
        self._device = device
        device.connect_attempts += 1
        if self._generation is not None:
            device.offered_generations.append(self._generation)

        if device.unreachable:
            raise OSError(f"[Errno 113] No route to host: {host}:{port}")
        if device.tls_error:
            raise ssl.SSLError("tlsv1 alert unknown ca")

        device._clients.append(self)
        if device.silent:
            # Handshake completes, CONNACK never arrives. The caller has to
            # time out to notice.
            return 0
        accepted = self._generation in device.accepts
        code = CONNACK_ACCEPTED if accepted else CONNACK_NOT_AUTHORIZED
        self._queue(lambda: self._fire_connack(code))
        return 0

    def _fire_connack(self, code: int) -> None:
        if code == CONNACK_ACCEPTED:
            self._connected = True
        if self.on_connect is not None:
            self.on_connect(self, None, {}, _ReasonCode(code), None)

    def is_connected(self) -> bool:
        return self._connected

    def subscribe(self, topic: str, qos: int = 0) -> tuple[int, int]:
        if self._device is None:
            return (0, 1)
        self._device.subscriptions.append(topic)
        if topic == "#":
            # Every speaker publishes a retained ``<platform>/<MAC>/status``
            # message; reading it is how the identification probe learns what
            # it is talking to without knowing anything but the IP. The
            # payload is not Binme and the probe never parses it - only the
            # topic carries the answer.
            self._deliver(f"{self._device.platform}/{self._device.mac}/status", b"\x00")
        return (0, 1)

    def publish(self, topic: str, payload: bytes, qos: int = 0, retain: bool = False):
        if not self._connected:
            raise AssertionError(
                "published while disconnected; publish_action is meant to "
                "check is_connected first"
            )
        assert self._device is not None
        message = _Published(topic, payload)
        self._device.published.append(message)
        if self._device.auto_answer_info and message.action == "info":
            header = {"id": "fake", "type": "event", "timestamp": 0, "name": "info"}
            self._deliver(
                f"{self._device.platform}/{self._device.mac}/status",
                encode_binme(header, {"deviceName": self._device.name}),
            )
        return _FakePublishInfo()

    def disconnect(self) -> int:
        was_connected = self._connected
        self._connected = False
        if self._device is not None and self in self._device._clients:
            self._device._clients.remove(self)
        if was_connected and self.on_disconnect is not None:
            self.on_disconnect(self, None, {}, _ReasonCode(0), None)
        return 0

    # ── Network loop ──────────────────────────────────────────────────────

    def loop(self, timeout: float = 1.0) -> int:
        """Run every callback queued since the last call.

        Returns immediately rather than sleeping for ``timeout``: the
        integration's loop task calls this in a tight cycle, and a real
        half-second wait per iteration would make the suite unusable.
        """
        with self._lock:
            pending, self._pending = self._pending, []
        for callback in pending:
            callback()
        return 0

    def loop_start(self) -> int:
        """Background loop, as ``discovery.py`` uses it."""
        self._loop_stop.clear()

        def _run() -> None:
            while not self._loop_stop.wait(0.001):
                self.loop(0)

        self._loop_thread = threading.Thread(target=_run, daemon=True)
        self._loop_thread.start()
        return 0

    def loop_stop(self) -> int:
        self._loop_stop.set()
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=5)
            self._loop_thread = None
        return 0

    # ── Device-driven ─────────────────────────────────────────────────────

    def _queue(self, callback: Callable[[], None]) -> None:
        with self._lock:
            self._pending.append(callback)

    def _deliver(self, topic: str, payload: bytes) -> None:
        def _fire() -> None:
            if self.on_message is not None:
                self.on_message(self, None, _FakeMessage(topic, payload))

        self._queue(_fire)

    def _drop(self, reason: int) -> None:
        def _fire() -> None:
            self._connected = False
            if self.on_disconnect is not None:
                self.on_disconnect(self, None, {}, _ReasonCode(reason), None)

        self._queue(_fire)


class _FakePublishInfo:
    """The bare minimum of paho's ``MQTTMessageInfo``."""

    rc = 0

    def wait_for_publish(self, timeout: float | None = None) -> None:
        return None


class _FakeMessage:
    """Stand-in for ``paho.mqtt.client.MQTTMessage``."""

    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload
        self.qos = 0
        self.retain = True


class FakeMqttNetwork:
    """The set of speakers reachable in one test."""

    def __init__(self) -> None:
        self.devices: dict[str, FakeDevice] = {}
        #: Every client built during the test, so a test can assert that
        #: unload left nothing behind.
        self.clients: list[_FakeClient] = []

    def add(self, device: FakeDevice) -> FakeDevice:
        self.devices[device.ip] = device
        return device

    def device_at(self, ip: str) -> FakeDevice | None:
        return self.devices.get(ip)

    def __iter__(self) -> Iterator[FakeDevice]:
        return iter(self.devices.values())

    def generation_for(self, certfile: str | None) -> str | None:
        """Map a certificate path back to its generation name.

        The integration picks files out of ``CERT_GENERATIONS``; this reverses
        that so a fake device can decide whether to accept them without the
        test having to know the filenames.
        """
        if certfile is None:
            return None
        from custom_components.unifi_play.mqtt_client import CERT_GENERATIONS

        for generation in CERT_GENERATIONS:
            if str(generation.cert) == certfile:
                return generation.name
        return None

    def factory(self, *args: Any, **kwargs: Any) -> _FakeClient:
        """Drop-in for ``paho.mqtt.client.Client``."""
        client = _FakeClient(self, *args, **kwargs)
        self.clients.append(client)
        return client

    def live_clients(self) -> list[_FakeClient]:
        """Clients that still believe they are connected."""
        return [client for client in self.clients if client.is_connected()]


__all__ = [
    "CONNACK_ACCEPTED",
    "CONNACK_NOT_AUTHORIZED",
    "FakeDevice",
    "FakeMqttNetwork",
    "mqtt",
]
