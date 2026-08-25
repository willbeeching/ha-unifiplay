#!/usr/bin/env python3
"""Simulate certificate-generation fallback with paho stubbed.

Why this exists
---------------
``UnifiPlayMqttClient.connect`` walks ``CERT_GENERATIONS`` and decides from
the *shape* of the failure whether to try another pair. That control flow
cannot be caught by ``check_mqtt_client_calls.py``, and the shapes that
matter most — TLS 1.3 silent disconnect, a CONNACK whose reason code is a
failure, a half-open ``disconnect()`` — cannot be produced on demand
against hardware.

This is the simulation the #21 PR body described but did not commit. It
loads ``mqtt_client.py`` without Home Assistant (``__init__.py`` would
import it) and without a real paho, then asserts the invariants.

Exits non-zero on the first failed assertion.
"""

from __future__ import annotations

import asyncio
import importlib.util
import ssl
import sys
import tempfile
import types
from pathlib import Path

if sys.version_info < (3, 12):
    sys.exit(
        f"needs Python 3.12+ to parse the package (running "
        f"{sys.version_info.major}.{sys.version_info.minor})"
    )

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "custom_components" / "unifi_play"


class _FailedConnack:
    """Stand-in for paho v2 ReasonCode with is_failure true."""

    is_failure = True

    def __init__(self, name: str = "Not authorized") -> None:
        self.name = name

    def __str__(self) -> str:
        return self.name


class _StubState:
    def __init__(self) -> None:
        self.dials: list[str] = []
        self.outcome: dict[str, str] = {}
        self.path_to_name: dict[str, str] = {}
        self.disconnect_raises = False
        self.disconnects = 0


STATE = _StubState()


class FakeClient:
    """Minimal paho Client: connect/loop behave as STATE.outcome says."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None
        self._generation: str | None = None
        self._pending: str | None = None
        self._connected = False
        self.subscribed: str | None = None

    def tls_set(self, certfile: str | None = None, **kwargs: object) -> None:
        self._generation = STATE.path_to_name[str(certfile)]

    def tls_insecure_set(self, value: bool) -> None:
        return None

    def connect(self, host: str, port: int, keepalive: int) -> None:
        assert self._generation is not None
        STATE.dials.append(self._generation)
        outcome = STATE.outcome[self._generation]
        if outcome == "offline":
            raise TimeoutError("timed out")
        if outcome == "ssl":
            raise ssl.SSLError("[SSL: TLSV1_ALERT_UNKNOWN_CA] tlsv1 alert unknown ca")
        self._pending = outcome

    def loop(self, timeout: float = 1.0) -> None:
        pending = self._pending
        if pending is None:
            return
        if pending == "ok":
            self._pending = None
            self._connected = True
            if self.on_connect is not None:
                self.on_connect(self, None, None, 0)
            return
        if pending == "unauthorized":
            self._pending = None
            if self.on_connect is not None:
                self.on_connect(self, None, None, _FailedConnack())
            return
        if pending == "no_connack":
            return
        raise AssertionError(f"unknown outcome {pending!r}")

    def is_connected(self) -> bool:
        return self._connected

    def disconnect(self) -> None:
        STATE.disconnects += 1
        if STATE.disconnect_raises:
            raise RuntimeError("half-open paho client")
        self._connected = False

    def subscribe(self, topic: str) -> None:
        self.subscribed = topic


def _install_paho_stub() -> None:
    paho = types.ModuleType("paho")
    paho_mqtt = types.ModuleType("paho.mqtt")
    paho_client = types.ModuleType("paho.mqtt.client")

    class CallbackAPIVersion:
        VERSION2 = 2

    paho_client.CallbackAPIVersion = CallbackAPIVersion
    paho_client.Client = FakeClient
    sys.modules["paho"] = paho
    sys.modules["paho.mqtt"] = paho_mqtt
    sys.modules["paho.mqtt.client"] = paho_client


def _load_module(name: str, path: Path, package: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    module.__package__ = package
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_mqtt_client() -> types.ModuleType:
    _install_paho_stub()
    cc = types.ModuleType("custom_components")
    cc.__path__ = [str(ROOT / "custom_components")]
    sys.modules["custom_components"] = cc
    pkg = types.ModuleType("custom_components.unifi_play")
    pkg.__path__ = [str(PACKAGE)]
    sys.modules["custom_components.unifi_play"] = pkg
    _load_module("custom_components.unifi_play.const", PACKAGE / "const.py", "custom_components.unifi_play")
    return _load_module(
        "custom_components.unifi_play.mqtt_client",
        PACKAGE / "mqtt_client.py",
        "custom_components.unifi_play",
    )


mqtt_client = _load_mqtt_client()
mqtt_client.CONNACK_TIMEOUT = 0.2

CertGeneration = mqtt_client.CertGeneration
UnifiPlayMqttClient = mqtt_client.UnifiPlayMqttClient
MqttCertificateRejected = mqtt_client.MqttCertificateRejected


class _CaseFailure(AssertionError):
    pass


def _files(*names: str) -> list[CertGeneration]:
    tmp = Path(tempfile.mkdtemp(prefix="uplay-cert-"))
    generations = []
    for name in names:
        cert = tmp / f"mqtt_cert_{name}.crt"
        key = tmp / f"mqtt_cert_{name}_key.key"
        cert.write_text("cert")
        key.write_text("key")
        generations.append(CertGeneration(name, cert, key))
    return generations


def _reset(generations: list[CertGeneration], outcome: dict[str, str]) -> None:
    STATE.dials.clear()
    STATE.outcome = dict(outcome)
    STATE.disconnect_raises = False
    STATE.disconnects = 0
    STATE.path_to_name = {
        str(generation.cert): generation.name for generation in generations
    }
    mqtt_client._CERT_CHOICE.clear()
    mqtt_client.CERT_GENERATIONS = tuple(generations)


async def _connect(mac: str = "AABBCCDDEEFF") -> UnifiPlayMqttClient:
    client = UnifiPlayMqttClient("10.0.0.154", mac)
    await client.connect()
    return client


async def _run_cases() -> None:
    both = _files("2026", "2023")
    only_2023 = _files("2023")
    # Slot 2026 is listed but absent, matching the repo before the pair landed.
    missing_2026 = [
        CertGeneration("2026", Path("/no/such/2026.crt"), Path("/no/such/2026.key")),
        only_2023[0],
    ]

    # newest-first: a 1.0.41-shaped device accepts 2026 on the first dial.
    _reset(both, {"2026": "ok", "2023": "ok"})
    client = await _connect()
    if STATE.dials != ["2026"]:
        raise _CaseFailure(f"newest-first dials {STATE.dials}")
    if mqtt_client._CERT_CHOICE != {"AABBCCDDEEFF": "2026"}:
        raise _CaseFailure(f"cached {mqtt_client._CERT_CHOICE}")
    await client.disconnect()
    print("ok  newest-first: 1.0.41-shaped device, one dial")

    # TLS 1.3 rejection is the absence of a CONNACK.
    _reset(both, {"2026": "no_connack", "2023": "ok"})
    client = await _connect()
    if STATE.dials != ["2026", "2023"]:
        raise _CaseFailure(f"tls1.3 fallback dials {STATE.dials}")
    if mqtt_client._CERT_CHOICE != {"AABBCCDDEEFF": "2023"}:
        raise _CaseFailure(f"tls1.3 cached {mqtt_client._CERT_CHOICE}")
    await client.disconnect()
    print("ok  fallback on TLS 1.3 (no CONNACK)")

    # TLS 1.2 rejection is SSLError during connect().
    _reset(both, {"2026": "ssl", "2023": "ok"})
    client = await _connect()
    if STATE.dials != ["2026", "2023"]:
        raise _CaseFailure(f"tls1.2 fallback dials {STATE.dials}")
    if mqtt_client._CERT_CHOICE != {"AABBCCDDEEFF": "2023"}:
        raise _CaseFailure(f"tls1.2 cached {mqtt_client._CERT_CHOICE}")
    await client.disconnect()
    print("ok  fallback on TLS 1.2 (SSLError)")

    # Remembered generation is tried first; the rest stay newest-first behind it.
    _reset(both, {"2026": "ok", "2023": "ok"})
    mqtt_client._CERT_CHOICE["AABBCCDDEEFF"] = "2023"
    client = await _connect()
    if STATE.dials != ["2023"]:
        raise _CaseFailure(f"remembered dials {STATE.dials}")
    await client.disconnect()
    print("ok  remembered generation skips the probe")

    # All rejected: every present generation is named, nothing is cached.
    _reset(both, {"2026": "no_connack", "2023": "ssl"})
    try:
        await _connect()
    except MqttCertificateRejected as err:
        message = str(err)
        if "2026" not in message or "2023" not in message:
            raise _CaseFailure(f"all-rejected message {message!r}") from err
    else:
        raise _CaseFailure("all-rejected did not raise")
    if mqtt_client._CERT_CHOICE:
        raise _CaseFailure(f"all-rejected cached {mqtt_client._CERT_CHOICE}")
    print("ok  all-rejected raises with both generations, caches nothing")

    # Offline is a TCP-level timeout: one dial, not one per generation.
    _reset(both, {"2026": "offline", "2023": "ok"})
    try:
        await _connect()
    except TimeoutError:
        pass
    else:
        raise _CaseFailure("offline did not raise TimeoutError")
    if STATE.dials != ["2026"]:
        raise _CaseFailure(f"offline dials {STATE.dials}")
    print("ok  offline device produces one dial")

    # Repo-shaped: 2026 listed but absent, 2023 present, one dial.
    _reset(missing_2026, {"2023": "ok"})
    client = await _connect()
    if STATE.dials != ["2023"]:
        raise _CaseFailure(f"absent-2026 dials {STATE.dials}")
    await client.disconnect()
    print("ok  only the 2023 pair present: one dial")

    # A failure CONNACK is not acceptance — the other generation is tried.
    _reset(both, {"2026": "unauthorized", "2023": "ok"})
    client = await _connect()
    if STATE.dials != ["2026", "2023"]:
        raise _CaseFailure(f"unauthorized CONNACK dials {STATE.dials}")
    if mqtt_client._CERT_CHOICE != {"AABBCCDDEEFF": "2023"}:
        raise _CaseFailure(f"unauthorized CONNACK cached {mqtt_client._CERT_CHOICE}")
    await client.disconnect()
    print("ok  failure CONNACK is not cached as success")

    # disconnect() raising after a rejection must not abort the fallback.
    _reset(both, {"2026": "no_connack", "2023": "ok"})
    STATE.disconnect_raises = True
    client = await _connect()
    if STATE.dials != ["2026", "2023"]:
        raise _CaseFailure(f"disconnect-raises dials {STATE.dials}")
    if mqtt_client._CERT_CHOICE != {"AABBCCDDEEFF": "2023"}:
        raise _CaseFailure(f"disconnect-raises cached {mqtt_client._CERT_CHOICE}")
    STATE.disconnect_raises = False
    await client.disconnect()
    print("ok  failing disconnect() does not kill the fallback")


def main() -> int:
    try:
        asyncio.run(_run_cases())
    except _CaseFailure as err:
        print(f"FAILED: {err}", file=sys.stderr)
        return 1
    print("All certificate-generation fallback cases hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
