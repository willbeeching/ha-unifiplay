"""Setup, unload and reload of a config entry."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_play.const import DOMAIN

from .conftest import entry_coordinator
from .const import AMP_IP, AMP_MAC, PORT_IP, PORT_MAC
from .fake_mqtt import FakeDevice, FakeMqttNetwork


def _device_by_identifier(
    registry: dr.DeviceRegistry, entry_id: str, identifier: tuple[str, str]
):
    """Find one of an entry's devices by identifier, on any supported release.

    ``async_get_device`` is deprecated from Home Assistant 2026.9 and its
    replacement does not exist on the 2025.8 floor, and iterating
    ``registry.devices`` yields ids on one release and entries on the other.
    ``async_entries_for_config_entry`` means the same thing on both.
    """
    for device in dr.async_entries_for_config_entry(registry, entry_id):
        if identifier in device.identifiers:
            return device
    return None


async def test_direct_setup_connects_every_speaker(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
) -> None:
    """Both speakers are dialled, subscribed and interrogated."""
    assert setup_direct.state is ConfigEntryState.LOADED

    coordinator = entry_coordinator(hass, setup_direct)
    assert set(coordinator.data) == {
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
    }

    for device in (amp, port):
        assert device.connect_attempts == 1
        # The subscription wildcards the platform segment: the broker is the
        # speaker itself, so this matches only its own topics whatever
        # prefix its firmware publishes under.
        assert device.subscriptions == [f"+/{device.mac}/status"]
        # The opening burst the official app also sends.
        assert device.actions()[:7] == [
            "info",
            "extra_info",
            "metadata",
            "equalizer",
            "sub_audio",
            "alarms",
            "quiet_hours",
        ]


async def test_console_setup_uses_apollo(
    hass: HomeAssistant,
    setup_console: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
) -> None:
    """Console mode discovers through Apollo and connects the same way."""
    assert setup_console.state is ConfigEntryState.LOADED
    assert amp.connect_attempts == 1
    assert port.connect_attempts == 1


async def test_setup_registers_devices(
    hass: HomeAssistant, setup_direct: MockConfigEntry
) -> None:
    """Each speaker gets one device-registry entry, keyed on its MAC."""
    registry = dr.async_get(hass)
    entry_id = setup_direct.entry_id
    for mac in (AMP_MAC, PORT_MAC):
        device = _device_by_identifier(registry, entry_id, (DOMAIN, mac))
        assert device is not None
        assert device.manufacturer == "Ubiquiti"

    amp_device = _device_by_identifier(registry, entry_id, (DOMAIN, AMP_MAC))
    assert amp_device is not None
    assert amp_device.model == "PowerAmp"
    assert amp_device.sw_version == "1.0.38"


async def test_unload_disconnects_everything(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    mqtt_network: FakeMqttNetwork,
) -> None:
    """Unloading leaves no live MQTT client and no services behind."""
    assert hass.services.has_service(DOMAIN, "play_announcement")

    assert await hass.config_entries.async_unload(setup_direct.entry_id)
    await hass.async_block_till_done()

    assert setup_direct.state is ConfigEntryState.NOT_LOADED
    assert mqtt_network.live_clients() == []
    assert not hass.services.has_service(DOMAIN, "play_announcement")


async def test_reload_does_not_leak_clients(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    mqtt_network: FakeMqttNetwork,
    settle,
) -> None:
    """Repeated reloads leave exactly one live client per speaker.

    A client that outlives its entry keeps a socket open and a callback
    registered, so the next reload's events arrive twice.
    """
    for _ in range(3):
        assert await hass.config_entries.async_reload(setup_direct.entry_id)
        await settle(hass)
        assert setup_direct.state is ConfigEntryState.LOADED
        assert len(mqtt_network.live_clients()) == 2


async def test_offline_speaker_does_not_block_setup(
    hass: HomeAssistant,
    direct_entry: MockConfigEntry,
    udp_discovery,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    """One unreachable speaker must not take the entry down with it."""
    port.unreachable = True

    direct_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(direct_entry.entry_id)
    await settle(hass)

    assert direct_entry.state is ConfigEntryState.LOADED
    coordinator = entry_coordinator(hass, direct_entry)
    assert coordinator.mqtt_offline_reason("22222222-2222-4222-8222-222222222222") == (
        "unreachable"
    )
    assert (
        coordinator.mqtt_offline_reason("11111111-1111-4111-8111-111111111111") is None
    )


async def test_entities_are_created_for_both_models(
    hass: HomeAssistant, setup_direct: MockConfigEntry
) -> None:
    """A media player exists for each speaker."""
    assert hass.states.get("media_player.living_room") is not None
    assert hass.states.get("media_player.kitchen") is not None


async def test_speaker_ip_matches_the_fixture(
    amp: FakeDevice, port: FakeDevice
) -> None:
    """Guard against the fixtures drifting away from the shared constants."""
    assert amp.ip == AMP_IP
    assert port.ip == PORT_IP
