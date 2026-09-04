"""The shared entity base: availability, device info and the command guard."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_play.const import DOMAIN
from custom_components.unifi_play.entity import UnifiPlayEntity

from .conftest import entry_coordinator
from .const import AMP_ID, AMP_MAC, PORT_ID, PORT_MAC, fixture
from .fake_mqtt import FakeDevice


def _entity(hass: HomeAssistant, entry: MockConfigEntry, device_id: str):
    """A bare base entity bound to a real coordinator.

    The base class is the one place a direct object test earns its keep:
    ``_require_mqtt`` guards the window where an entity is still considered
    available but the socket has gone, and that window cannot be staged
    through the service registry.
    """
    entity = UnifiPlayEntity(entry_coordinator(hass, entry), device_id)
    entity.hass = hass
    return entity


async def test_require_mqtt_raises_when_the_socket_is_down(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    """Commands must fail loudly, never no-op and report success (#14)."""
    entity = _entity(hass, setup_direct, AMP_ID)
    assert entity._require_mqtt() is not None

    amp.drop()
    await settle(hass)

    with pytest.raises(HomeAssistantError, match="No MQTT connection"):
        entity._require_mqtt()


async def test_a_registered_client_is_not_a_connected_one(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    """The coordinator inserts the client before dialling out (#14).

    Presence in the dict therefore says nothing about whether a command will
    land, which is why ``_mqtt`` and ``_connected_mqtt`` are separate.
    """
    entity = _entity(hass, setup_direct, AMP_ID)
    amp.drop()
    await settle(hass)

    assert entity._mqtt() is not None
    assert entity._connected_mqtt() is None
    assert entity.available is False


async def test_a_device_known_only_by_its_topic_root_still_registers(
    hass: HomeAssistant,
    direct_entry: MockConfigEntry,
    udp_discovery,
    mqtt_network,
    settle,
) -> None:
    """Direct-mode MQTT identification yields ``UPL-DEVICE`` and no firmware.

    That is Port hardware (#4), so the registry has to show a Play Audio
    Port rather than a raw topic root. ``extra_info`` then carries the real
    platform and version, which the coordinator applies to its own state -
    the device *registry* row is written when the entity is added and does
    not follow later, so a firmware bump shows up after a restart. See
    test_lifecycle.py for the registry-refresh behaviour.
    """
    from .const import THIRD_IP, THIRD_MAC, device_dict

    udp_discovery.clear()
    udp_discovery.append(
        device_dict(
            device_id=THIRD_MAC,
            mac=THIRD_MAC,
            ip=THIRD_IP,
            name="Study",
            platform="UPL-DEVICE",
            firmware="",
        )
    )
    study = mqtt_network.add(
        FakeDevice(ip=THIRD_IP, mac=THIRD_MAC, platform="UPL-DEVICE", name="Study")
    )

    direct_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(direct_entry.entry_id)
    await settle(hass)

    registry = dr.async_get(hass)
    device = next(
        candidate
        for candidate in dr.async_entries_for_config_entry(
            registry, direct_entry.entry_id
        )
        if (DOMAIN, THIRD_MAC) in candidate.identifiers
    )
    assert device.model == "Play Audio Port"

    study.emit("extra_info", fixture("mqtt_extra_info_port.json"))
    await settle(hass)

    coordinator = entry_coordinator(hass, direct_entry)
    assert coordinator.data[THIRD_MAC].platform == "UPL-PORT"
    assert coordinator.data[THIRD_MAC].firmware == "1.1.10"


async def test_unique_ids_are_mac_based_and_stable(
    hass: HomeAssistant, setup_direct: MockConfigEntry
) -> None:
    """Unique IDs must not change: a rename orphans every registry row.

    They are also deliberately not namespaced per entry, which is why
    ``_entry_already_covering`` blocks this at setup and the coordinator
    refuses a speaker another loaded entry already has.
    """
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    unique_ids = {
        entry.unique_id
        for entry in er.async_entries_for_config_entry(registry, setup_direct.entry_id)
    }
    assert f"unifi_play_{AMP_MAC}" in unique_ids
    assert f"unifi_play_{AMP_MAC}_connectivity" in unique_ids
    assert f"unifi_play_{PORT_MAC}" in unique_ids
    assert all(uid.startswith("unifi_play_") for uid in unique_ids)


async def test_device_name_prefers_what_the_device_calls_itself(
    hass: HomeAssistant, setup_direct: MockConfigEntry, port: FakeDevice, settle
) -> None:
    """``deviceName`` from the speaker beats the name discovery reported."""
    coordinator = entry_coordinator(hass, setup_direct)
    port.emit("info", {"deviceName": "Kitchen Counter"})
    await settle(hass)
    assert coordinator.data[PORT_ID].device_name == "Kitchen Counter"
