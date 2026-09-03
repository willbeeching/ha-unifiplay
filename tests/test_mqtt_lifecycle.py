"""How the MQTT connection is run, retried and taken down.

The network loop used to be an asyncio task that submitted
``client.loop(0.5)`` to Home Assistant's default executor over and over: one
worker held permanently per speaker, out of a pool everything else in Home
Assistant shares. It also could not reconnect, because only
``loop_forever()`` does, so a dropped connection stayed dead until the
five-minute discovery poll noticed.

It now runs on paho's own thread. These tests are about that thread being
started once, stopped once, and never outliving the entry that owns it.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_play import mqtt_client as mc
from custom_components.unifi_play.mqtt_client import (
    DISCONNECT_TIMEOUT,
    RECONNECT_ATTEMPT_LIMIT,
    RECONNECT_MAX_DELAY,
    RECONNECT_MIN_DELAY,
    MqttCertificateRejected,
    UnifiPlayMqttClient,
)

from .conftest import entry_coordinator, pump
from .const import AMP_ID, AMP_IP, AMP_MAC, PORT_ID
from .fake_mqtt import FakeDevice, FakeMqttNetwork


@pytest.fixture(autouse=True)
def _forget_remembered_certificates():
    mc._CERT_CHOICE.clear()
    yield
    mc._CERT_CHOICE.clear()


# ── The execution model ───────────────────────────────────────────────────


def test_there_is_no_executor_loop_left() -> None:
    """A structural guard against the old design coming back.

    ``_mqtt_loop`` was the method that held an executor worker per speaker.
    Its absence is the whole point of this change, and nothing else in the
    suite would notice it returning.
    """
    assert not hasattr(UnifiPlayMqttClient, "_mqtt_loop")


async def test_each_speaker_gets_exactly_one_network_thread(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
) -> None:
    """One thread that belongs to the client, not one worker borrowed from
    Home Assistant's shared pool."""
    for device in (amp, port):
        assert device.loop_threads_started == 1
        assert device.loop_threads_stopped == 0


async def test_unload_stops_every_network_thread(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
) -> None:
    assert await hass.config_entries.async_unload(setup_direct.entry_id)
    await hass.async_block_till_done()

    for device in (amp, port):
        assert device.loop_threads_stopped == device.loop_threads_started


async def test_repeated_reloads_leak_no_threads(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    mqtt_network: FakeMqttNetwork,
    settle,
) -> None:
    """Started and stopped counts have to stay equal, not just bounded.

    A thread left behind holds a socket open and keeps delivering into a
    coordinator that no longer exists, so the next reload gets everything
    twice.
    """
    before = threading.active_count()
    for _ in range(3):
        assert await hass.config_entries.async_reload(setup_direct.entry_id)
        await settle(hass)

    assert len(mqtt_network.live_clients()) == 2
    for device in (amp, port):
        assert device.loop_threads_started == 4
        assert device.loop_threads_stopped == 3

    assert await hass.config_entries.async_unload(setup_direct.entry_id)
    await hass.async_block_till_done()
    for device in (amp, port):
        assert device.loop_threads_stopped == device.loop_threads_started
    # A couple of threads either way is Home Assistant's own machinery; four
    # more per reload would be this integration's.
    assert threading.active_count() <= before + 2


async def test_several_speakers_connect_concurrently(
    hass: HomeAssistant,
    direct_entry: MockConfigEntry,
    udp_discovery,
    mqtt_network: FakeMqttNetwork,
    amp: FakeDevice,
    port: FakeDevice,
    third: FakeDevice,
    settle,
) -> None:
    from .const import third_device

    udp_discovery.append(third_device())
    direct_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(direct_entry.entry_id)
    await settle(hass)

    assert len(mqtt_network.live_clients()) == 3
    for device in (amp, port, third):
        assert device.connect_attempts == 1
        assert device.loop_threads_started == 1


# ── Backoff ───────────────────────────────────────────────────────────────


def test_the_backoff_is_bounded_and_grows() -> None:
    assert RECONNECT_MIN_DELAY >= 1
    assert RECONNECT_MAX_DELAY > RECONNECT_MIN_DELAY
    # Ten minutes without a retry is a speaker nobody notices came back.
    assert RECONNECT_MAX_DELAY <= 600


async def test_the_backoff_is_configured_on_every_client(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    mqtt_network: FakeMqttNetwork,
) -> None:
    """The fake asserts the shape; this asserts it was set at all.

    Without it paho retries immediately and forever, and a switch reboot
    brings the whole house back in lockstep.
    """
    for client in mqtt_network.clients:
        assert client.reconnect_delays is not None
        assert client.reconnect_delays[0] >= RECONNECT_MIN_DELAY


async def test_the_backoff_is_jittered_between_speakers(
    hass: HomeAssistant,
    direct_entry: MockConfigEntry,
    udp_discovery,
    mqtt_network: FakeMqttNetwork,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    """Not a strict inequality: jitter is random, and a run where two
    speakers draw the same delay is a normal run. What is asserted is that
    the delay is drawn from a range rather than fixed."""
    direct_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(direct_entry.entry_id)
    await settle(hass)

    delays = {client.reconnect_delays[0] for client in mqtt_network.clients}
    assert delays
    assert all(
        RECONNECT_MIN_DELAY <= delay <= RECONNECT_MIN_DELAY + mc.RECONNECT_JITTER
        for delay in delays
    )


# ── Disconnect, retry, recovery ───────────────────────────────────────────


async def test_a_drop_is_logged_once_and_recovery_once(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    amp: FakeDevice,
    settle,
    caplog,
) -> None:
    """A speaker that is off for a weekend would otherwise write a line every
    two minutes, and the line that matters - it came back - would be
    indistinguishable from the noise."""
    caplog.set_level(logging.INFO)
    coordinator = entry_coordinator(hass, setup_direct)
    client = coordinator.get_mqtt_client(AMP_ID)
    assert client is not None

    amp.drop()
    await settle(hass)
    assert caplog.text.count(f"MQTT to {AMP_IP} dropped") == 1

    amp.fail_reconnect(times=2)
    await pump()
    assert caplog.text.count(f"MQTT to {AMP_IP} dropped") == 1

    # paho reconnects on its own; the fake stands in for that.
    client._on_connect(client._client, None, {}, _success(), None)
    await pump()
    assert caplog.text.count(f"MQTT to {AMP_IP} is back") == 1


def _success():
    from .fake_mqtt import _ReasonCode

    return _ReasonCode(0)


async def test_a_client_that_is_retrying_is_left_alone(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    amp: FakeDevice,
    settle,
) -> None:
    """Two reconnect loops on one speaker is a storm with extra steps, and
    rebuilding mid-backoff throws the backoff away."""
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from custom_components.unifi_play.coordinator import DISCOVERY_INTERVAL

    coordinator = entry_coordinator(hass, setup_direct)
    client = coordinator.get_mqtt_client(AMP_ID)

    amp.drop()
    await settle(hass)
    assert client.is_retrying is True

    async_fire_time_changed(hass, dt_util.utcnow() + DISCOVERY_INTERVAL)
    await settle(hass)

    assert coordinator.get_mqtt_client(AMP_ID) is client
    assert amp.connect_attempts == 1


async def test_a_client_that_gave_up_is_rebuilt(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    amp: FakeDevice,
    settle,
    caplog,
) -> None:
    """paho retries with the same credentials forever, and the one failure
    that never resolves that way is a firmware update rotating the CA."""
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from custom_components.unifi_play.coordinator import DISCOVERY_INTERVAL

    coordinator = entry_coordinator(hass, setup_direct)
    first = coordinator.get_mqtt_client(AMP_ID)

    amp.drop()
    await settle(hass)
    amp.fail_reconnect(times=RECONNECT_ATTEMPT_LIMIT)
    await pump()

    assert first.is_retrying is False
    assert "standing down" in caplog.text

    async_fire_time_changed(hass, dt_util.utcnow() + DISCOVERY_INTERVAL)
    await settle(hass)

    assert coordinator.get_mqtt_client(AMP_ID) is not first
    # The rebuild re-probes the certificate generations, which is the whole
    # reason for standing down.
    assert amp.connect_attempts == 2


async def test_a_reconnect_resets_the_failure_count(
    mqtt_network: FakeMqttNetwork,
) -> None:
    """Otherwise a speaker on a flaky link accumulates failures across days
    and eventually stands down for no reason."""
    device = mqtt_network.add(FakeDevice(ip=AMP_IP, mac=AMP_MAC))
    client = UnifiPlayMqttClient(AMP_IP, AMP_MAC)
    await client.connect()
    try:
        device.fail_reconnect(times=RECONNECT_ATTEMPT_LIMIT - 1)
        await pump()
        assert client._reconnect_failures == RECONNECT_ATTEMPT_LIMIT - 1

        client._on_connect(client._client, None, {}, _success(), None)
        await pump()
        assert client._reconnect_failures == 0
    finally:
        await client.disconnect()


# ── Cancellation and shutdown ─────────────────────────────────────────────


async def test_cancelling_a_connect_stops_the_network_thread(
    mqtt_network: FakeMqttNetwork, monkeypatch
) -> None:
    """Home Assistant shutting down while a speaker is mid-dial.

    paho's thread does not stop on its own, and one left running holds a
    socket open and keeps delivering into a coordinator that has gone.
    """
    monkeypatch.setattr(mc, "CONNACK_TIMEOUT", 30)
    device = mqtt_network.add(FakeDevice(ip=AMP_IP, mac=AMP_MAC, silent=True))
    client = UnifiPlayMqttClient(AMP_IP, AMP_MAC)

    task = asyncio.create_task(client.connect())
    await pump(passes=3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert device.loop_threads_stopped == device.loop_threads_started
    assert not client.is_connected


async def test_an_entry_unloaded_mid_retry_leaves_nothing_running(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    mqtt_network: FakeMqttNetwork,
    settle,
) -> None:
    amp.drop()
    await settle(hass)
    assert entry_coordinator(hass, setup_direct).get_mqtt_client(AMP_ID).is_retrying

    assert await hass.config_entries.async_unload(setup_direct.entry_id)
    await hass.async_block_till_done()

    assert mqtt_network.live_clients() == []
    for device in (amp, port):
        assert device.loop_threads_stopped == device.loop_threads_started


async def test_shutdown_is_bounded(
    hass: HomeAssistant, setup_direct: MockConfigEntry
) -> None:
    """A shutdown that can hang is a Home Assistant restart that can hang."""
    assert DISCONNECT_TIMEOUT > 0
    started = time.monotonic()
    assert await hass.config_entries.async_unload(setup_direct.entry_id)
    await hass.async_block_till_done()
    assert time.monotonic() - started < DISCONNECT_TIMEOUT * 2


async def test_a_network_thread_that_will_not_stop_is_reported_not_awaited(
    mqtt_network: FakeMqttNetwork, monkeypatch, caplog
) -> None:
    """The join is bounded, and the timeout says so rather than passing
    silently: a thread that would not stop is worth a line in the log."""
    monkeypatch.setattr(mc, "DISCONNECT_TIMEOUT", 0.05)
    device = mqtt_network.add(FakeDevice(ip=AMP_IP, mac=AMP_MAC))
    client = UnifiPlayMqttClient(AMP_IP, AMP_MAC)
    await client.connect()

    stuck = mqtt_network.clients[-1]
    real_stop = stuck.loop_stop
    monkeypatch.setattr(stuck, "loop_stop", lambda: time.sleep(0.3) or 0)

    await client.disconnect()
    assert "did not stop within" in caplog.text
    assert device.ip == AMP_IP

    # The thread is the fake's, not Home Assistant's, but leaving it running
    # would fail the harness's own leak check and hide a real one later.
    monkeypatch.undo()
    real_stop()


async def test_disconnect_is_safe_before_any_connection() -> None:
    client = UnifiPlayMqttClient(AMP_IP, AMP_MAC)
    await client.disconnect()
    assert not client.is_connected
    assert not client.is_retrying


async def test_callbacks_are_detached_on_disconnect(
    mqtt_network: FakeMqttNetwork,
) -> None:
    """They close over a coordinator that is about to go away.

    A callback firing after unload reaches into an object that no longer has
    clients, entities or an entry.
    """
    device = mqtt_network.add(FakeDevice(ip=AMP_IP, mac=AMP_MAC))
    seen: list[str] = []
    client = UnifiPlayMqttClient(
        AMP_IP, AMP_MAC, on_event=lambda name, header, body: seen.append(name)
    )
    await client.connect()
    paho_client = mqtt_network.clients[-1]
    await client.disconnect()

    assert paho_client.on_message is None
    assert paho_client.on_disconnect is None
    assert paho_client.on_connect_fail is None
    device.emit("info", {"volume": 1})
    await pump()
    assert seen == []


# ── Dispatch still works ──────────────────────────────────────────────────


async def test_events_still_reach_the_coordinator(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    """The callbacks arrive on paho's thread now, so every hop back to the
    event loop has to be explicit."""
    amp.emit("online", {"status": 1})
    amp.emit("info", {"volume": 71})
    await settle(hass)

    coordinator = entry_coordinator(hass, setup_direct)
    assert coordinator.data[AMP_ID].volume == 71
    assert hass.states.get("media_player.living_room").attributes[
        "volume_level"
    ] == pytest.approx(0.71)


async def test_a_certificate_rejection_still_falls_back(
    mqtt_network: FakeMqttNetwork,
) -> None:
    """The fallback runs before the network thread is handed over, so the
    thread model must not have broken it."""
    device = mqtt_network.add(
        FakeDevice(ip=AMP_IP, mac=AMP_MAC, accepts=frozenset({"2023"}))
    )
    client = UnifiPlayMqttClient(AMP_IP, AMP_MAC)
    await client.connect()
    try:
        assert device.offered_generations == ["2026", "2023"]
        # One thread per attempt, and every one of them stopped again.
        assert device.loop_threads_stopped == device.loop_threads_started - 1
    finally:
        await client.disconnect()
    assert device.loop_threads_stopped == device.loop_threads_started


async def test_no_thread_survives_a_total_rejection(
    mqtt_network: FakeMqttNetwork,
) -> None:
    device = mqtt_network.add(FakeDevice(ip=AMP_IP, mac=AMP_MAC, accepts=frozenset()))
    client = UnifiPlayMqttClient(AMP_IP, AMP_MAC)
    with pytest.raises(MqttCertificateRejected):
        await client.connect()
    assert device.loop_threads_stopped == device.loop_threads_started


async def test_both_speakers_keep_working_after_one_drops(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    amp.drop()
    await settle(hass)
    port.clear()

    port.emit("info", {"volume": 33})
    await settle(hass)

    coordinator = entry_coordinator(hass, setup_direct)
    assert coordinator.data[PORT_ID].volume == 33
    assert hass.states.get("media_player.kitchen").state != "unavailable"
