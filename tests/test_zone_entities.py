"""The zone's own entities: its media player and its broadcasting select.

A zone is not a device. Its entities are created and destroyed as speakers
report and stop reporting the zone, which means every one of their properties
can be read at a moment when the zone no longer exists. Home Assistant stops
polling an entity that reports unavailable, so those guards are reached
through the entity object rather than through the state machine.

Zone commands fan out to every member, and the rule for all of them is the
same as for a zone write: reach everybody or refuse. Silencing three rooms of
four and reporting success leaves the fourth playing with nothing to say so.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.components.media_player import MediaPlayerState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_play.const import DOMAIN
from custom_components.unifi_play.media_player import UnifiPlayZonePlayer
from custom_components.unifi_play.select import UnifiPlayZoneBroadcastingSelect

from .conftest import entity_object, entry_coordinator
from .const import AMP_MAC, PORT_MAC, ZONE_ID, groups_body
from .fake_mqtt import FakeDevice

ZONE_PLAYER = "media_player.downstairs"


def _zone_select(hass: HomeAssistant) -> str:
    """The zone's broadcasting select, found by unique id.

    Its entity id was minted before any speaker had reported the zone's
    name, so it is not derived from the name and hard-coding it would be
    guessing at a slug.
    """
    from homeassistant.helpers import entity_registry as er

    entity_id = er.async_get(hass).async_get_entity_id(
        "select", DOMAIN, f"unifi_play_zone_{ZONE_ID}_broadcasting_mode"
    )
    assert entity_id is not None
    return entity_id


def _detached_player(
    hass: HomeAssistant, entry: MockConfigEntry
) -> UnifiPlayZonePlayer:
    """A zone player for a zone that is not there.

    Built rather than found, because an entity whose zone has gone reports
    unavailable and Home Assistant then never calls the properties this is
    about.
    """
    entity = UnifiPlayZonePlayer(entry_coordinator(hass, entry), "gone-zone-id")
    entity.hass = hass
    return entity


def _detached_select(
    hass: HomeAssistant, entry: MockConfigEntry
) -> UnifiPlayZoneBroadcastingSelect:
    entity = UnifiPlayZoneBroadcastingSelect(
        entry_coordinator(hass, entry), "gone-zone-id"
    )
    entity.hass = hass
    return entity


async def _call(hass: HomeAssistant, domain: str, service: str, **data: Any) -> None:
    await hass.services.async_call(domain, service, data, blocking=True)


# ── The zone media player ─────────────────────────────────────────────────


async def test_a_zone_reports_the_hosts_volume(
    hass: HomeAssistant, synced_zone: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    """One number for the zone, and the host's is the one the app shows."""
    amp.emit("info", {"volume": 40, "mute": False})
    await settle(hass)

    state = hass.states.get(ZONE_PLAYER)
    assert state is not None
    assert state.attributes["volume_level"] == pytest.approx(0.4)
    assert state.attributes["is_volume_muted"] is False


async def test_a_zone_is_playing_only_while_it_broadcasts(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    """Streaming members each play their own thing, which is not the zone
    playing anything."""
    body = groups_body()
    amp.emit("groups", body)
    port.emit("groups", body)
    await settle(hass)
    assert hass.states.get(ZONE_PLAYER).state == MediaPlayerState.IDLE

    broadcasting = groups_body(wb_enable=True, wb_device=AMP_MAC, wb_input="line_in")
    amp.emit("groups", broadcasting)
    port.emit("groups", broadcasting)
    await settle(hass)
    assert hass.states.get(ZONE_PLAYER).state == MediaPlayerState.PLAYING


async def test_a_zone_carries_its_topology_as_attributes(
    hass: HomeAssistant, synced_zone: MockConfigEntry
) -> None:
    attrs = hass.states.get(ZONE_PLAYER).attributes
    assert attrs["group_id"] == ZONE_ID
    assert set(attrs["group_members"]) == {AMP_MAC, PORT_MAC}
    assert attrs["host_mac"] == AMP_MAC
    assert attrs["dev_count"] == 2
    assert attrs["wb_enable"] is False


async def test_the_source_list_is_the_union_across_the_zone(
    hass: HomeAssistant, synced_zone: MockConfigEntry
) -> None:
    """A PowerAmp has eARC and Line In; an Audio Port adds S/PDIF and USB.

    Any member can be the broadcast source, so offering the intersection
    would hide inputs that work.
    """
    sources = hass.states.get(ZONE_PLAYER).attributes["source_list"]
    assert sources[0] == "Streaming"
    assert {"Line In", "eARC", "S/PDIF", "USB"} <= set(sources)


async def test_the_source_is_read_against_the_broadcasting_speaker(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    """`speakers` is eARC on both models, and the labels are per platform.

    Reading wb_input against the wrong speaker is how eARC was hidden from
    the Audio Port entirely.
    """
    body = groups_body(wb_enable=True, wb_device=PORT_MAC, wb_input="speakers")
    amp.emit("groups", body)
    port.emit("groups", body)
    await settle(hass)
    assert hass.states.get(ZONE_PLAYER).attributes["source"] == "eARC"


async def test_a_zone_that_is_not_broadcasting_reads_as_streaming(
    hass: HomeAssistant, synced_zone: MockConfigEntry
) -> None:
    assert hass.states.get(ZONE_PLAYER).attributes["source"] == "Streaming"


async def test_zone_volume_reaches_every_member(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
) -> None:
    amp.clear()
    port.clear()
    await _call(
        hass, "media_player", "volume_set", entity_id=ZONE_PLAYER, volume_level=0.55
    )
    for device in (amp, port):
        assert device.last_action("set_volume").body["volume"] == 55


@pytest.mark.parametrize(
    ("service", "data", "expected"),
    [("volume_up", {}, 45), ("volume_down", {}, 35)],
)
async def test_zone_volume_steps_from_the_host(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
    service: str,
    data: dict[str, Any],
    expected: int,
) -> None:
    amp.emit("info", {"volume": 40, "vol_limit": 100})
    await settle(hass)
    amp.clear()
    port.clear()

    await _call(hass, "media_player", service, entity_id=ZONE_PLAYER, **data)
    for device in (amp, port):
        assert device.last_action("set_volume").body["volume"] == expected


async def test_zone_mute_reaches_every_member(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
) -> None:
    amp.clear()
    port.clear()
    await _call(
        hass, "media_player", "volume_mute", entity_id=ZONE_PLAYER, is_volume_muted=True
    )
    for device in (amp, port):
        assert device.published_actions("set_mute") or device.published_actions(
            "set_volume"
        )


@pytest.mark.parametrize(
    ("service", "data"),
    [
        ("volume_set", {"volume_level": 0.5}),
        ("volume_up", {}),
        ("volume_down", {}),
        ("volume_mute", {"is_volume_muted": True}),
    ],
)
async def test_a_zone_command_refuses_while_a_member_is_offline(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
    service: str,
    data: dict[str, Any],
) -> None:
    """Turning down three rooms of four is a worse outcome than an error,
    because nothing says which room is still loud."""
    port.drop()
    await settle(hass)
    amp.clear()

    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "media_player", service, entity_id=ZONE_PLAYER, **data)
    assert err.value.translation_key == "zone_members_offline"
    assert amp.published_actions("set_volume") == []


async def test_a_zone_goes_unavailable_when_every_member_does(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    """Not when one drops: the zone still exists, and the last state it was
    seen in is the honest answer for the rest."""
    port.drop()
    await settle(hass)
    assert hass.states.get(ZONE_PLAYER).state != "unavailable"

    amp.drop()
    await settle(hass)
    assert hass.states.get(ZONE_PLAYER).state == "unavailable"


async def test_selecting_streaming_clears_the_broadcast(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    body = groups_body(wb_enable=True, wb_device=AMP_MAC, wb_input="line_in")
    amp.emit("groups", body)
    port.emit("groups", body)
    await settle(hass)
    amp.clear()
    port.clear()

    await _call(
        hass, "media_player", "select_source", entity_id=ZONE_PLAYER, source="Streaming"
    )
    assert amp.last_action("set_groups").body["groups"][0]["wb_enable"] is False


async def test_selecting_a_wired_input_writes_the_zone_then_the_input(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    """The order matters. A refused zone write must not leave a speaker
    switched to an input nothing is listening to."""
    amp.clear()
    port.clear()
    await _call(
        hass, "media_player", "select_source", entity_id=ZONE_PLAYER, source="Line In"
    )
    written = amp.last_action("set_groups").body["groups"][0]
    assert written["wb_enable"] is True
    assert written["wb_input"] == "lineIn"
    assert amp.published_actions("set_audio_src")


async def test_selecting_an_input_the_source_speaker_lacks(
    hass: HomeAssistant, synced_zone: MockConfigEntry, amp: FakeDevice
) -> None:
    """The amp is hosting and has no optical jack. It accepts `spdif` on the
    wire and routes nothing to it, so the refusal has to happen here."""
    amp.clear()
    with pytest.raises(ServiceValidationError) as err:
        await _call(
            hass, "media_player", "select_source", entity_id=ZONE_PLAYER, source="USB"
        )
    assert err.value.translation_key == "zone_source_not_on_device"
    assert amp.published_actions("set_groups") == []


async def test_a_zone_with_no_elected_host_cannot_be_stepped(
    hass: HomeAssistant, synced_zone: MockConfigEntry, amp: FakeDevice, port: FakeDevice
) -> None:
    """Volume up reads the current level from the host, and a zone written
    seconds ago has not elected one yet."""
    coordinator = entry_coordinator(hass, synced_zone)
    coordinator.groups[ZONE_ID].host_mac = ""

    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "media_player", "volume_up", entity_id=ZONE_PLAYER)
    assert err.value.translation_key == "zone_host_unknown"


# ── A zone entity outliving its zone ──────────────────────────────────────


async def test_every_zone_property_survives_the_zone_going(
    hass: HomeAssistant, synced_zone: MockConfigEntry
) -> None:
    """Deleted from the Play app between one state write and the next."""
    entity = _detached_player(hass, synced_zone)
    assert entity.available is False
    assert entity.state is None
    assert entity.volume_level is None
    assert entity.is_volume_muted is None
    assert entity.source is None
    assert entity.source_list == ["Streaming"]
    assert entity.extra_state_attributes == {}
    assert entity.device_info["identifiers"] == {(DOMAIN, "zone_gone-zone-id")}
    assert "via_device" not in entity.device_info
    assert "via_device_id" not in entity.device_info


async def test_a_zone_is_linked_through_its_host(
    hass: HomeAssistant, synced_zone: MockConfigEntry
) -> None:
    """2026.9 wants via_device_id; the 2025.8 floor still uses via_device."""
    from homeassistant.helpers import device_registry as dr

    entity = entity_object(hass, ZONE_PLAYER)
    assert isinstance(entity, UnifiPlayZonePlayer)
    info = entity.device_info
    identifier = (DOMAIN, AMP_MAC)
    if "via_device_id" in info:
        host = next(
            device
            for device in dr.async_entries_for_config_entry(
                dr.async_get(hass), synced_zone.entry_id
            )
            if identifier in device.identifiers
        )
        assert info["via_device_id"] == host.id
        assert "via_device" not in info
    else:
        assert info["via_device"] == identifier


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("async_set_volume_level", (0.5,)),
        ("async_volume_up", ()),
        ("async_volume_down", ()),
        ("async_mute_volume", (True,)),
        ("async_select_source", ("Streaming",)),
    ],
)
async def test_every_zone_command_refuses_once_the_zone_has_gone(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    method: str,
    args: tuple[Any, ...],
) -> None:
    entity = _detached_player(hass, synced_zone)
    with pytest.raises(ServiceValidationError) as err:
        await getattr(entity, method)(*args)
    assert err.value.translation_key == "zone_not_found"


# ── The zone broadcasting select ──────────────────────────────────────────


async def test_the_broadcasting_select_reports_the_current_mode(
    hass: HomeAssistant, synced_zone: MockConfigEntry
) -> None:
    state = hass.states.get(_zone_select(hass))
    assert state is not None
    assert state.state == "Zone Only"


async def test_choosing_a_broadcasting_mode_writes_the_zone(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
) -> None:
    """And nothing else: no set_audio_src, which would switch a real input
    as a side effect of an advertising change."""
    amp.clear()
    port.clear()
    await _call(
        hass,
        "select",
        "select_option",
        entity_id=_zone_select(hass),
        option="Zone & Devices",
    )
    for device in (amp, port):
        assert (
            device.last_action("set_groups").body["groups"][0]["broadcasting_mode"]
            != "zone_only"
        )
        assert device.published_actions("set_audio_src") == []


async def test_a_broadcasting_mode_the_integration_does_not_know(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    """Returned as-is, which Home Assistant renders as unknown.

    A firmware that adds a fourth mode should read as unknown rather than be
    silently reported as one of the three that are mapped.
    """
    body = groups_body(broadcasting_mode="something_new")
    amp.emit("groups", body)
    port.emit("groups", body)
    await settle(hass)

    assert entity_object(hass, _zone_select(hass)).current_option == "something_new"
    assert hass.states.get(_zone_select(hass)).state == "unknown"


async def test_the_broadcasting_select_survives_the_zone_going(
    hass: HomeAssistant, synced_zone: MockConfigEntry
) -> None:
    entity = _detached_select(hass, synced_zone)
    assert entity.available is False
    assert entity.current_option is None

    with pytest.raises(ServiceValidationError) as err:
        await entity.async_select_option("Zone Only")
    assert err.value.translation_key == "zone_not_found"


async def test_a_broadcasting_option_that_is_not_one_of_the_modes(
    hass: HomeAssistant, synced_zone: MockConfigEntry, amp: FakeDevice
) -> None:
    """The label is the wire value's only route back, so an unknown one
    cannot be turned into a mode and must not be guessed at."""
    amp.clear()
    entity = entity_object(hass, _zone_select(hass))
    with pytest.raises(ServiceValidationError) as err:
        await entity.async_select_option("Nonsense")
    assert err.value.translation_key == "unknown_option"
    assert amp.published_actions("set_groups") == []


async def test_a_zone_device_left_behind_without_its_entity_is_removed(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    """An entity deleted from Developer Tools leaves the device behind.

    The entity sweep cannot reach it, because there is no entity registration
    left to walk, so the device sits in the registry as an empty card for a
    zone that no longer exists.
    """
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    entity_reg = er.async_get(hass)
    device_reg = dr.async_get(hass)
    for entity in er.async_entries_for_config_entry(entity_reg, synced_zone.entry_id):
        if (entity.unique_id or "").startswith(f"unifi_play_zone_{ZONE_ID}"):
            entity_reg.async_remove(entity.entity_id)

    zone_devices = [
        device
        for device in dr.async_entries_for_config_entry(
            device_reg, synced_zone.entry_id
        )
        if any(value.startswith("zone_") for _domain, value in device.identifiers)
    ]
    assert zone_devices

    from .const import empty_groups_body

    amp.emit("groups", empty_groups_body())
    port.emit("groups", empty_groups_body())
    await settle(hass)

    remaining = [
        device
        for device in dr.async_entries_for_config_entry(
            device_reg, synced_zone.entry_id
        )
        if any(value.startswith("zone_") for _domain, value in device.identifiers)
    ]
    assert remaining == []


async def test_a_zone_with_no_speakers_left_refuses_a_command(
    hass: HomeAssistant, synced_zone: MockConfigEntry
) -> None:
    """The zone document survives with an empty member list, which no
    command can act on."""
    coordinator = entry_coordinator(hass, synced_zone)
    coordinator.groups[ZONE_ID].dev_info = []

    # Through the entity rather than the service: an entity that reports
    # unavailable is skipped by the service call, and this is about what the
    # command does when it does run.
    entity = UnifiPlayZonePlayer(coordinator, ZONE_ID)
    entity.hass = hass
    with pytest.raises(ServiceValidationError) as err:
        await entity.async_set_volume_level(0.5)
    assert err.value.translation_key == "zone_has_no_members"


async def test_a_broadcast_source_no_longer_in_the_zone(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    """wb_device names a speaker that has since left.

    The input label is resolved against the speaker broadcasting it, because
    the same wire value means different jacks on different models. With no
    such speaker there is no platform to resolve against, and the label falls
    back to the model-independent one rather than guessing at a model.
    """
    from .const import AMP_IP, AMP_NAME, PORT_IP, PORT_NAME, zone_member

    body = groups_body(
        members=[
            zone_member(AMP_MAC, AMP_NAME, AMP_IP, platform="UPL-AMP", host=True),
            zone_member(PORT_MAC, PORT_NAME, PORT_IP),
        ],
        wb_enable=True,
        wb_device="AABBCCDDEE99",
        wb_input="spdif",
    )
    amp.emit("groups", body)
    port.emit("groups", body)
    await settle(hass)

    assert hass.states.get(ZONE_PLAYER).attributes["source"] == "S/PDIF"
