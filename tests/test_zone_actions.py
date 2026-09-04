"""Zone mutations: validation, preflight, and refusing to write partially.

A zone definition is replace-all per speaker and does not spread between
them. Publishing to whoever happens to be online therefore produces a zone
that forms on some speakers, competes on merge, and reverts minutes later
with nothing in the log. Everything here is about that not happening.
"""

from __future__ import annotations

import pytest
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_play.const import DOMAIN
from custom_components.unifi_play.zone_writer import ZoneWriteError, ZoneWriteResult

from .conftest import entry_coordinator
from .const import (
    AMP_IP,
    AMP_MAC,
    AMP_NAME,
    PORT_IP,
    PORT_MAC,
    PORT_NAME,
    THIRD_IP,
    THIRD_MAC,
    THIRD_NAME,
    ZONE_ID,
    ZONE_NAME,
    groups_body,
    third_device,
    zone_member,
)
from .fake_mqtt import FakeDevice

ZONE_ENTITY = "media_player.downstairs"


def _writer(hass: HomeAssistant, entry: MockConfigEntry):
    return entry_coordinator(hass, entry).zones


def _written_groups(device: FakeDevice) -> list[dict]:
    """The zone list from the most recent set_groups sent to this speaker."""
    return device.last_action("set_groups").body["groups"]


@pytest.fixture
async def three_speakers(
    hass: HomeAssistant,
    direct_entry: MockConfigEntry,
    udp_discovery,
    amp: FakeDevice,
    port: FakeDevice,
    third: FakeDevice,
    settle,
):
    """Three speakers, one zone containing two of them."""
    udp_discovery.append(third_device())
    direct_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(direct_entry.entry_id)
    await settle(hass)

    body = groups_body()
    for device in (amp, port, third):
        device.emit("groups", body)
    await settle(hass)
    for device in (amp, port, third):
        device.clear()
    return direct_entry


# ── Validation, before anything is published ──────────────────────────────


async def test_a_zone_of_one_is_refused_without_publishing(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice
) -> None:
    """The firmware treats a one-member zone as a malformed document."""
    amp.clear()
    with pytest.raises(ServiceValidationError) as err:
        _writer(hass, setup_direct).create(name="Lonely", member_macs=[AMP_MAC])
    assert err.value.translation_key == "zone_needs_two_devices"
    assert amp.published_actions("set_groups") == []


async def test_a_zone_of_none_is_refused_without_publishing(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice
) -> None:
    amp.clear()
    with pytest.raises(ServiceValidationError):
        _writer(hass, setup_direct).create(name="Empty", member_macs=[])
    assert amp.published_actions("set_groups") == []


async def test_the_same_speaker_listed_twice_is_not_two_members(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice
) -> None:
    """A duplicate would be taken literally: dev_count would disagree with
    dev_info and the zone would form wrong."""
    amp.clear()
    with pytest.raises(ServiceValidationError) as err:
        _writer(hass, setup_direct).create(
            name="Doubled", member_macs=[AMP_MAC, "aa:bb:cc:dd:ee:ff"]
        )
    assert err.value.translation_key == "zone_needs_two_devices"
    assert amp.published_actions("set_groups") == []


async def test_mac_spelling_does_not_matter(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
) -> None:
    """Colons, hyphens and case all arrive from somewhere real.

    The registry stores what the speaker reported, the wire carries it raw,
    and users type it with colons.
    """
    amp.clear()
    result = _writer(hass, setup_direct).create(
        name="Mixed", member_macs=["aa:bb:cc:dd:ee:ff", "AA-BB-CC-DD-EE-11"]
    )
    assert set(result.written_macs) == {AMP_MAC, PORT_MAC}
    written = _written_groups(amp)[0]
    assert [d["mac"] for d in written["dev_info"]] == [AMP_MAC, PORT_MAC]
    assert written["dev_count"] == 2


async def test_an_unknown_speaker_is_refused(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice
) -> None:
    amp.clear()
    with pytest.raises(ServiceValidationError) as err:
        _writer(hass, setup_direct).create(
            name="Ghost", member_macs=[AMP_MAC, "DEADBEEF0000"]
        )
    assert err.value.translation_key == "zone_unknown_device"
    assert amp.published_actions("set_groups") == []


async def test_a_speaker_cannot_be_in_two_zones(
    hass: HomeAssistant,
    three_speakers: MockConfigEntry,
    amp: FakeDevice,
) -> None:
    """The firmware's rule, not this integration's: a speaker listed in two
    zones registers membership in both and plays in neither reliably."""
    with pytest.raises(ServiceValidationError) as err:
        _writer(hass, three_speakers).create(
            name="Overlap", member_macs=[PORT_MAC, THIRD_MAC]
        )
    assert err.value.translation_key == "zone_device_in_other_zone"
    assert err.value.translation_placeholders["zone"] == ZONE_NAME
    assert amp.published_actions("set_groups") == []


async def test_adding_a_speaker_that_is_already_a_member(
    hass: HomeAssistant, synced_zone: MockConfigEntry, amp: FakeDevice
) -> None:
    amp.clear()
    with pytest.raises(ServiceValidationError) as err:
        _writer(hass, synced_zone).add_member(ZONE_ID, PORT_MAC)
    assert err.value.translation_key == "zone_already_member"
    assert amp.published_actions("set_groups") == []


async def test_removing_a_speaker_that_is_not_a_member(
    hass: HomeAssistant, three_speakers: MockConfigEntry, amp: FakeDevice
) -> None:
    with pytest.raises(ServiceValidationError) as err:
        _writer(hass, three_speakers).remove_member(ZONE_ID, THIRD_MAC)
    assert err.value.translation_key == "zone_member_not_in_zone"
    assert amp.published_actions("set_groups") == []


async def test_removing_a_member_from_a_pair_is_refused(
    hass: HomeAssistant, synced_zone: MockConfigEntry, amp: FakeDevice
) -> None:
    amp.clear()
    with pytest.raises(ServiceValidationError) as err:
        _writer(hass, synced_zone).remove_member(ZONE_ID, PORT_MAC)
    assert err.value.translation_key == "zone_would_be_too_small"
    assert amp.published_actions("set_groups") == []


async def test_acting_on_a_zone_that_does_not_exist(
    hass: HomeAssistant, setup_direct: MockConfigEntry
) -> None:
    with pytest.raises(ServiceValidationError) as err:
        _writer(hass, setup_direct).rename("no-such-zone", "Nowhere")
    assert err.value.translation_key == "zone_not_found"


# ── Preflight ─────────────────────────────────────────────────────────────


async def test_one_offline_member_prevents_the_whole_write(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    """The heart of it.

    Writing to the speakers that are up leaves the rest serving the previous
    definition, which then wins on merge and appears to undo the change.
    """
    amp.clear()
    port.drop()
    await settle(hass)

    with pytest.raises(ServiceValidationError) as err:
        _writer(hass, synced_zone).rename(ZONE_ID, "Ground Floor")

    assert err.value.translation_key == "zone_members_offline"
    assert PORT_NAME in err.value.translation_placeholders["devices"]
    assert amp.published_actions("set_groups") == []


async def test_an_offline_speaker_outside_the_zone_still_blocks_it(
    hass: HomeAssistant,
    three_speakers: MockConfigEntry,
    third: FakeDevice,
    amp: FakeDevice,
    settle,
) -> None:
    """A speaker holding a cached copy of this zone is required too.

    Non-member copies compete on merge whenever no member claims host, which
    is exactly the window just after a write.
    """
    third.drop()
    await settle(hass)

    with pytest.raises(ServiceValidationError) as err:
        _writer(hass, three_speakers).rename(ZONE_ID, "Ground Floor")
    assert THIRD_NAME in err.value.translation_placeholders["devices"]
    assert amp.published_actions("set_groups") == []


async def test_a_speaker_that_holds_no_zones_does_not_block_a_write(
    hass: HomeAssistant,
    direct_entry: MockConfigEntry,
    udp_discovery,
    amp: FakeDevice,
    port: FakeDevice,
    third: FakeDevice,
    settle,
) -> None:
    """A speaker that has never reported a zone list holds nothing stale.

    Blocking every zone edit on a speaker that has never been reachable
    would be a worse failure than the one the preflight prevents.
    """
    udp_discovery.append(third_device())
    third.unreachable = True
    direct_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(direct_entry.entry_id)
    await settle(hass)

    amp.clear()
    result = _writer(hass, direct_entry).create(
        name="Downstairs", member_macs=[AMP_MAC, PORT_MAC]
    )
    assert set(result.written_macs) == {AMP_MAC, PORT_MAC}


async def test_the_offline_message_names_every_unreachable_speaker(
    hass: HomeAssistant,
    three_speakers: MockConfigEntry,
    port: FakeDevice,
    third: FakeDevice,
    settle,
) -> None:
    port.drop()
    third.drop()
    await settle(hass)

    with pytest.raises(ServiceValidationError) as err:
        _writer(hass, three_speakers).rename(ZONE_ID, "Ground Floor")
    devices = err.value.translation_placeholders["devices"]
    assert PORT_NAME in devices
    assert THIRD_NAME in devices


async def test_a_publish_that_is_dropped_mid_write_is_reported(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
) -> None:
    """The socket can go between the preflight and the publish.

    Rare, and the one case where a partial write is possible. There is no
    way to unsend what already left, so saying so is the only honest option.
    """
    amp.clear()
    port.clear()

    # Connected for the preflight, gone by the time the publish reaches it.
    port.drop_after_checks = 1

    with pytest.raises(ZoneWriteError) as err:
        _writer(hass, synced_zone).rename(ZONE_ID, "Ground Floor")

    assert err.value.translation_key == "zone_publish_failed"
    assert PORT_MAC in err.value.translation_placeholders["failed"]
    assert port.published_actions("set_groups") == []


# ── Successful writes ─────────────────────────────────────────────────────


async def test_a_write_reaches_every_required_speaker_exactly_once(
    hass: HomeAssistant,
    three_speakers: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    third: FakeDevice,
) -> None:
    result = _writer(hass, three_speakers).rename(ZONE_ID, "Ground Floor")

    assert sorted(result.written_macs) == sorted([AMP_MAC, PORT_MAC, THIRD_MAC])
    for device in (amp, port, third):
        assert len(device.published_actions("set_groups")) == 1
        assert _written_groups(device)[0]["name"] == "Ground Floor"


async def test_the_whole_zone_list_goes_out_on_every_write(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    """set_groups is replace-all, so a write that carried one zone would
    delete every other zone on that speaker."""
    # Members this coordinator does not manage: a speaker holds a copy of
    # every zone on the site, including ones it is not in and ones made up of
    # hardware on another console.
    second = {
        "group_id": "zone-2",
        "name": "Upstairs",
        "dev_info": [
            zone_member("AABBCCDD1111", "Bedroom", "192.168.1.150", host=True),
            zone_member("AABBCCDD2222", "Landing", "192.168.1.151"),
        ],
        "dev_count": 2,
        "group_index": 2,
        "broadcasting_mode": "zone_only",
        "wb_enable": False,
        "wb_device": "",
        "wb_input": "",
    }
    body = groups_body(extra_groups=[second])
    amp.emit("groups", body)
    port.emit("groups", body)
    await settle(hass)
    amp.clear()

    _writer(hass, synced_zone).rename(ZONE_ID, "Ground Floor")

    written = _written_groups(amp)
    assert {g["group_id"] for g in written} == {ZONE_ID, "zone-2"}
    assert {g["name"] for g in written} == {"Ground Floor", "Upstairs"}


async def test_a_written_zone_never_asserts_the_host_flag(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice
) -> None:
    """The firmware elects the host and echoes the flag back.

    Asserting it produces a zone that registers on every speaker and only
    ever sounds in one room (#22). Note that ``host: false`` is not
    equivalent — the app omits the key entirely.
    """
    amp.clear()
    _writer(hass, setup_direct).create(name="New", member_macs=[AMP_MAC, PORT_MAC])
    for member in _written_groups(amp)[0]["dev_info"]:
        assert "host" not in member


async def test_removing_the_host_hands_the_role_over(
    hass: HomeAssistant,
    three_speakers: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    third: FakeDevice,
    settle,
) -> None:
    """The host is an internal role, not something the user chose.

    Removing it rewrites the zone with no host at all, and the survivors
    elect one — exactly as they do for a new zone. Writing the flag
    ourselves is what breaks audio sync to the members.
    """
    body = groups_body(
        members=[
            zone_member(AMP_MAC, AMP_NAME, AMP_IP, platform="UPL-AMP", host=True),
            zone_member(PORT_MAC, PORT_NAME, PORT_IP),
            zone_member(THIRD_MAC, THIRD_NAME, THIRD_IP),
        ]
    )
    for device in (amp, port, third):
        device.emit("groups", body)
    await settle(hass)
    amp.clear()

    _writer(hass, three_speakers).remove_member(ZONE_ID, AMP_MAC)

    written = _written_groups(amp)[0]
    assert [d["mac"] for d in written["dev_info"]] == [PORT_MAC, THIRD_MAC]
    assert all("host" not in d for d in written["dev_info"])


async def test_removing_the_broadcasting_speaker_returns_the_zone_to_streaming(
    hass: HomeAssistant,
    three_speakers: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    third: FakeDevice,
    settle,
) -> None:
    """The wired source leaves with the speaker that was providing it."""
    body = groups_body(
        members=[
            zone_member(AMP_MAC, AMP_NAME, AMP_IP, platform="UPL-AMP", host=True),
            zone_member(PORT_MAC, PORT_NAME, PORT_IP),
            zone_member(THIRD_MAC, THIRD_NAME, THIRD_IP),
        ],
        wb_enable=True,
        wb_device=PORT_MAC,
        wb_input="spdif",
    )
    for device in (amp, port, third):
        device.emit("groups", body)
    await settle(hass)
    amp.clear()

    _writer(hass, three_speakers).remove_member(ZONE_ID, PORT_MAC)

    written = _written_groups(amp)[0]
    assert written["wb_enable"] is False
    assert written["wb_device"] == ""
    assert written["wb_input"] == ""


async def test_removing_a_non_broadcasting_speaker_keeps_the_source(
    hass: HomeAssistant,
    three_speakers: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    third: FakeDevice,
    settle,
) -> None:
    body = groups_body(
        members=[
            zone_member(AMP_MAC, AMP_NAME, AMP_IP, platform="UPL-AMP", host=True),
            zone_member(PORT_MAC, PORT_NAME, PORT_IP),
            zone_member(THIRD_MAC, THIRD_NAME, THIRD_IP),
        ],
        wb_enable=True,
        wb_device=PORT_MAC,
        wb_input="spdif",
    )
    for device in (amp, port, third):
        device.emit("groups", body)
    await settle(hass)
    amp.clear()

    _writer(hass, three_speakers).remove_member(ZONE_ID, THIRD_MAC)

    written = _written_groups(amp)[0]
    assert written["wb_enable"] is True
    assert written["wb_device"] == PORT_MAC


async def test_the_broadcast_input_goes_to_the_broadcasting_speaker(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
) -> None:
    """Two writes to two different places.

    The zone document goes to every required speaker; the input switch
    belongs on the one that will actually broadcast, which is frequently not
    the host. Sending both to the host switches the wrong speaker's input.
    """
    amp.clear()
    port.clear()

    _writer(hass, synced_zone).set_broadcast_source(
        ZONE_ID, source_mac=PORT_MAC, wb_input="spdif"
    )

    assert _written_groups(amp)[0]["wb_device"] == PORT_MAC
    assert port.last_action("set_audio_src").body == {"source": "spdif"}
    assert amp.published_actions("set_audio_src") == []


async def test_a_refused_zone_write_never_switches_an_input(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    """Otherwise a speaker is left on an input nothing is listening to."""
    port.drop()
    await settle(hass)
    port.clear()

    with pytest.raises(ServiceValidationError):
        _writer(hass, synced_zone).set_broadcast_source(
            ZONE_ID, source_mac=PORT_MAC, wb_input="spdif"
        )
    assert port.published_actions("set_audio_src") == []


async def test_returning_to_streaming_hands_the_input_back(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    body = groups_body(wb_enable=True, wb_device=PORT_MAC, wb_input="spdif")
    amp.emit("groups", body)
    port.emit("groups", body)
    await settle(hass)
    amp.clear()
    port.clear()

    _writer(hass, synced_zone).clear_broadcast_source(ZONE_ID)

    assert _written_groups(amp)[0]["wb_enable"] is False
    assert port.last_action("set_audio_src").body == {"source": "streaming"}


async def test_deleting_a_zone_removes_it_from_every_speaker(
    hass: HomeAssistant,
    three_speakers: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    third: FakeDevice,
) -> None:
    result = _writer(hass, three_speakers).delete(ZONE_ID)

    assert result.deleted is True
    assert sorted(result.written_macs) == sorted([AMP_MAC, PORT_MAC, THIRD_MAC])
    for device in (amp, port, third):
        assert _written_groups(device) == []


async def test_a_result_says_which_speakers_were_written_to(
    hass: HomeAssistant, synced_zone: MockConfigEntry
) -> None:
    result = _writer(hass, synced_zone).rename(ZONE_ID, "Ground Floor")
    assert isinstance(result, ZoneWriteResult)
    assert result.group_id == ZONE_ID
    assert result.deleted is False
    assert bool(result) is True


# ── Services delegate to the same path ────────────────────────────────────


def _device_id(hass: HomeAssistant, entry: MockConfigEntry, mac: str) -> str:
    registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        if (DOMAIN, mac) in device.identifiers:
            return device.id
    raise AssertionError(f"no registry device for {mac}")


async def test_the_rename_service_uses_the_write_path(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
) -> None:
    amp.clear()
    await hass.services.async_call(
        DOMAIN,
        "rename_zone",
        {ATTR_ENTITY_ID: ZONE_ENTITY, "name": "Ground Floor"},
        blocking=True,
    )
    assert _written_groups(amp)[0]["name"] == "Ground Floor"
    assert len(port.published_actions("set_groups")) == 1


async def test_the_rename_service_refuses_when_a_member_is_offline(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    """The service inspects the result rather than logging and returning."""
    amp.clear()
    port.drop()
    await settle(hass)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "rename_zone",
            {ATTR_ENTITY_ID: ZONE_ENTITY, "name": "Ground Floor"},
            blocking=True,
        )
    assert amp.published_actions("set_groups") == []


async def test_the_create_zone_service_uses_the_write_path(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
) -> None:
    amp.clear()
    await hass.services.async_call(
        DOMAIN,
        "create_zone",
        {
            "name": "Downstairs",
            "host_device_id": _device_id(hass, setup_direct, AMP_MAC),
            "member_device_ids": [_device_id(hass, setup_direct, PORT_MAC)],
        },
        blocking=True,
    )
    written = _written_groups(amp)[0]
    assert written["name"] == "Downstairs"
    assert [d["mac"] for d in written["dev_info"]] == [AMP_MAC, PORT_MAC]


async def test_the_add_member_service_uses_the_write_path(
    hass: HomeAssistant,
    three_speakers: MockConfigEntry,
    amp: FakeDevice,
) -> None:
    await hass.services.async_call(
        DOMAIN,
        "add_zone_member",
        {
            ATTR_ENTITY_ID: ZONE_ENTITY,
            "device_id": _device_id(hass, three_speakers, THIRD_MAC),
        },
        blocking=True,
    )
    assert [d["mac"] for d in _written_groups(amp)[0]["dev_info"]] == [
        AMP_MAC,
        PORT_MAC,
        THIRD_MAC,
    ]


async def test_the_remove_member_service_uses_the_write_path(
    hass: HomeAssistant,
    three_speakers: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    third: FakeDevice,
    settle,
) -> None:
    body = groups_body(
        members=[
            zone_member(AMP_MAC, AMP_NAME, AMP_IP, platform="UPL-AMP", host=True),
            zone_member(PORT_MAC, PORT_NAME, PORT_IP),
            zone_member(THIRD_MAC, THIRD_NAME, THIRD_IP),
        ]
    )
    for device in (amp, port, third):
        device.emit("groups", body)
    await settle(hass)
    amp.clear()

    await hass.services.async_call(
        DOMAIN,
        "remove_zone_member",
        {
            ATTR_ENTITY_ID: ZONE_ENTITY,
            "device_id": _device_id(hass, three_speakers, THIRD_MAC),
        },
        blocking=True,
    )
    assert [d["mac"] for d in _written_groups(amp)[0]["dev_info"]] == [
        AMP_MAC,
        PORT_MAC,
    ]


async def test_the_delete_zone_service_uses_the_write_path(
    hass: HomeAssistant, synced_zone: MockConfigEntry, amp: FakeDevice
) -> None:
    amp.clear()
    await hass.services.async_call(
        DOMAIN, "delete_zone", {ATTR_ENTITY_ID: ZONE_ENTITY}, blocking=True
    )
    assert _written_groups(amp) == []


async def test_the_set_zone_index_service_uses_the_write_path(
    hass: HomeAssistant, synced_zone: MockConfigEntry, amp: FakeDevice
) -> None:
    amp.clear()
    await hass.services.async_call(
        DOMAIN,
        "set_zone_index",
        {ATTR_ENTITY_ID: ZONE_ENTITY, "group_index": 7},
        blocking=True,
    )
    assert _written_groups(amp)[0]["group_index"] == 7


# ── Zone media controls never skip a member ───────────────────────────────


async def test_zone_volume_reaches_every_member(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
) -> None:
    amp.clear()
    port.clear()
    await hass.services.async_call(
        "media_player",
        "volume_set",
        {ATTR_ENTITY_ID: ZONE_ENTITY, "volume_level": 0.4},
        blocking=True,
    )
    for device in (amp, port):
        assert device.last_action("set_volume").body["volume"] == 40


async def test_zone_volume_refuses_rather_than_skip_a_member(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    """A volume change that reaches one room and not the other, reported as
    success, is worse than one that refuses and says which room."""
    port.drop()
    await settle(hass)
    amp.clear()

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            "media_player",
            "volume_set",
            {ATTR_ENTITY_ID: ZONE_ENTITY, "volume_level": 0.4},
            blocking=True,
        )
    assert err.value.translation_key == "zone_members_offline"
    assert amp.published_actions("set_volume") == []


async def test_zone_mute_does_not_flag_a_speaker_it_cannot_reach(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    """The optimistic flags used to be set before the command was sent.

    That left a speaker showing muted while still playing, because the
    command that would have muted it was never sent.
    """
    coordinator = entry_coordinator(hass, synced_zone)
    port.drop()
    await settle(hass)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "media_player",
            "volume_mute",
            {ATTR_ENTITY_ID: ZONE_ENTITY, "is_volume_muted": True},
            blocking=True,
        )

    assert all(not state.muted for state in coordinator.data.values())


async def test_zone_mute_reaches_every_member(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    amp.emit("info", {"volume": 30})
    port.emit("info", {"volume": 50})
    await settle(hass)
    amp.clear()
    port.clear()

    await hass.services.async_call(
        "media_player",
        "volume_mute",
        {ATTR_ENTITY_ID: ZONE_ENTITY, "is_volume_muted": True},
        blocking=True,
    )
    for device in (amp, port):
        assert device.last_action("set_volume").body["volume"] == 0

    await hass.services.async_call(
        "media_player",
        "volume_mute",
        {ATTR_ENTITY_ID: ZONE_ENTITY, "is_volume_muted": False},
        blocking=True,
    )
    assert amp.last_action("set_volume").body["volume"] == 30
    assert port.last_action("set_volume").body["volume"] == 50


@pytest.mark.parametrize("service", ["volume_up", "volume_down"])
async def test_zone_volume_steps_refuse_rather_than_skip(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    port: FakeDevice,
    settle,
    service: str,
) -> None:
    port.drop()
    await settle(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "media_player", service, {ATTR_ENTITY_ID: ZONE_ENTITY}, blocking=True
        )


async def test_zone_source_selection_uses_the_write_path(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
) -> None:
    amp.clear()
    port.clear()
    await hass.services.async_call(
        "media_player",
        "select_source",
        {ATTR_ENTITY_ID: ZONE_ENTITY, "source": "Line In"},
        blocking=True,
    )
    assert _written_groups(amp)[0]["wb_input"] == "lineIn"


async def test_a_zone_source_the_broadcasting_speaker_lacks_is_refused(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    """The list offers the union across the zone.

    Falling through to "" would read as Streaming and silently drop the whole
    zone off its wired source instead of rejecting a request the hardware
    cannot serve.
    """
    body = groups_body(wb_enable=True, wb_device=AMP_MAC, wb_input="lineIn")
    amp.emit("groups", body)
    port.emit("groups", body)
    await settle(hass)
    amp.clear()

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            "media_player",
            "select_source",
            {ATTR_ENTITY_ID: ZONE_ENTITY, "source": "S/PDIF"},
            blocking=True,
        )
    assert err.value.translation_key == "zone_source_not_on_device"
    assert amp.published_actions("set_groups") == []


async def test_stopping_a_zone_announcement_refuses_rather_than_skip(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    """Skipping the unreachable members leaves an announcement audibly
    playing in a room the user just silenced."""
    port.drop()
    await settle(hass)
    amp.clear()

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "stop_zone_announcement",
            {ATTR_ENTITY_ID: ZONE_ENTITY},
            blocking=True,
        )
    assert err.value.translation_key == "zone_members_offline"
    assert amp.published_actions("announce") == []


async def test_stopping_a_zone_announcement_reaches_every_member(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
) -> None:
    amp.clear()
    port.clear()
    await hass.services.async_call(
        DOMAIN, "stop_zone_announcement", {ATTR_ENTITY_ID: ZONE_ENTITY}, blocking=True
    )
    for device in (amp, port):
        assert device.last_action("announce").body == {"enable": False}


async def test_every_zone_action_raises_a_home_assistant_error(
    hass: HomeAssistant, setup_direct: MockConfigEntry
) -> None:
    """Validation errors must be the kind Home Assistant renders to the user.

    A bare ValueError shows up as an unhandled exception in the log and a
    red toast with a traceback in it.
    """
    with pytest.raises(HomeAssistantError):
        _writer(hass, setup_direct).create(name="Lonely", member_macs=[AMP_MAC])


async def test_unloading_cancels_the_post_write_re_reads(
    hass: HomeAssistant, synced_zone: MockConfigEntry
) -> None:
    """Every write schedules a series of zone re-reads.

    The speakers elect a host after a write and never announce it, so asking
    again is the only way to learn it - and it is also the only confirmation
    the protocol offers that the write landed. Those timers must not outlive
    the entry: one firing thirty seconds after an unload is a task without an
    owner, and a reload that leaves the old series running ends up with two.
    """
    _writer(hass, synced_zone).rename(ZONE_ID, "Ground Floor")
    coordinator = entry_coordinator(hass, synced_zone)
    assert coordinator._host_reread_cancels

    assert await hass.config_entries.async_unload(synced_zone.entry_id)
    await hass.async_block_till_done()
    assert coordinator._host_reread_cancels == []


async def test_a_second_write_keeps_the_first_until_readback(
    hass: HomeAssistant, synced_zone: MockConfigEntry, amp: FakeDevice
) -> None:
    """coordinator.groups is not updated until a speaker reports the write.

    A rename immediately followed by an index change used to rebuild the
    second document from the pre-rename list and silently undo the first.
    """
    writer = _writer(hass, synced_zone)
    amp.clear()
    writer.rename(ZONE_ID, "Ground Floor")
    amp.clear()
    writer.set_index(ZONE_ID, 7)

    written = _written_groups(amp)[0]
    assert written["name"] == "Ground Floor"
    assert written["group_index"] == 7


async def test_a_stale_readback_does_not_undo_a_pending_write(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    """A groups event that still carries the old name is the normal window
    after a write, not a reason to drop the snapshot."""
    writer = _writer(hass, synced_zone)
    writer.rename(ZONE_ID, "Ground Floor")
    amp.emit("groups", groups_body())
    port.emit("groups", groups_body())
    await settle(hass)
    amp.clear()

    writer.set_index(ZONE_ID, 7)
    assert _written_groups(amp)[0]["name"] == "Ground Floor"


async def test_a_confirmed_write_then_an_app_edit_is_the_next_source(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    """Once the speakers agree on something else, the Play app wrote last."""
    writer = _writer(hass, synced_zone)
    writer.rename(ZONE_ID, "Ground Floor")
    confirmed = groups_body(name="Ground Floor")
    amp.emit("groups", confirmed)
    port.emit("groups", confirmed)
    await settle(hass)

    from_app = groups_body(name="From the app")
    amp.emit("groups", from_app)
    port.emit("groups", from_app)
    await settle(hass)
    amp.clear()

    writer.set_index(ZONE_ID, 3)
    assert _written_groups(amp)[0]["name"] == "From the app"


async def test_an_app_edit_before_readback_is_the_next_source(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    """Speakers that agree on a later Play-app edit have overwritten us.

    The pending snapshot exists so a stale pre-write echo does not undo a
    rename. It is not a lock: Home Assistant and the app are equal peers,
    and once every speaker is serving the app's document the next mutation
    has to build from that, not from the older HA name.
    """
    writer = _writer(hass, synced_zone)
    writer.rename(ZONE_ID, "Ground Floor")

    from_app = groups_body(name="From the app")
    amp.emit("groups", from_app)
    port.emit("groups", from_app)
    await settle(hass)
    amp.clear()

    writer.set_index(ZONE_ID, 3)
    assert _written_groups(amp)[0]["name"] == "From the app"


async def test_a_partial_app_edit_keeps_the_pending_write(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    """One speaker on the app's name and one still on the old is mid-edit."""
    writer = _writer(hass, synced_zone)
    writer.rename(ZONE_ID, "Ground Floor")
    amp.emit("groups", groups_body(name="From the app"))
    await settle(hass)
    amp.clear()

    writer.set_index(ZONE_ID, 3)
    assert _written_groups(amp)[0]["name"] == "Ground Floor"


async def test_a_second_write_supersedes_the_first_re_read_series(
    hass: HomeAssistant, synced_zone: MockConfigEntry
) -> None:
    """The series is about the zone last written, so it restarts rather than
    stacking a second set of timers on top of the first."""
    writer = _writer(hass, synced_zone)
    coordinator = entry_coordinator(hass, synced_zone)

    writer.rename(ZONE_ID, "One")
    first = len(coordinator._host_reread_cancels)
    writer.rename(ZONE_ID, "Two")
    assert len(coordinator._host_reread_cancels) == first


async def test_required_speakers_cover_members_and_stale_copies(
    hass: HomeAssistant, three_speakers: MockConfigEntry
) -> None:
    """Spelled out, because getting this set wrong is the whole failure mode."""
    writer = _writer(hass, three_speakers)
    # The third speaker is not a member but holds a cached copy of the zone.
    assert writer.required_macs(ZONE_ID, [AMP_MAC, PORT_MAC]) == sorted(
        [AMP_MAC, PORT_MAC, THIRD_MAC]
    )
    # A brand-new zone: only the speakers going into it.
    assert writer.required_macs("brand-new", [AMP_MAC, PORT_MAC]) == sorted(
        [AMP_MAC, PORT_MAC]
    )


# ── Zone entity state ─────────────────────────────────────────────────────


async def test_a_zone_is_unavailable_only_when_nothing_answers(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    """One speaker down still leaves real state to show.

    Hiding the whole zone would leave the user with nothing to look at while
    they work out which room is off; commands are the part that has to be all
    or nothing, and they refuse individually. With nothing connected there is
    no state at all, only what the zone was last seen with.
    """
    assert hass.states.get(ZONE_ENTITY).state != "unavailable"

    port.drop()
    await settle(hass)
    assert hass.states.get(ZONE_ENTITY).state != "unavailable"

    amp.drop()
    await settle(hass)
    assert hass.states.get(ZONE_ENTITY).state == "unavailable"


async def test_zone_volume_steps_reach_every_member(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    """The step is taken from the host's level, so the zone moves together
    rather than each speaker stepping from wherever it happens to be."""
    amp.emit("info", {"volume": 40, "vol_limit": 100})
    port.emit("info", {"volume": 10})
    await settle(hass)
    amp.clear()
    port.clear()

    await hass.services.async_call(
        "media_player", "volume_up", {ATTR_ENTITY_ID: ZONE_ENTITY}, blocking=True
    )
    for device in (amp, port):
        assert device.last_action("set_volume").body["volume"] == 45

    await hass.services.async_call(
        "media_player", "volume_down", {ATTR_ENTITY_ID: ZONE_ENTITY}, blocking=True
    )
    for device in (amp, port):
        assert device.last_action("set_volume").body["volume"] == 35


async def test_a_zone_reports_the_hosts_volume(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    """Members can drift apart; the host is the one the zone is named after."""
    amp.emit("info", {"volume": 60})
    port.emit("info", {"volume": 20})
    await settle(hass)

    state = hass.states.get(ZONE_ENTITY)
    assert state.attributes["volume_level"] == pytest.approx(0.6)


async def test_a_zone_source_list_is_the_union_of_its_members(
    hass: HomeAssistant, synced_zone: MockConfigEntry
) -> None:
    """A mixed zone offers whatever its hardware collectively supports.

    A PowerAmp has eARC and Line In; an Audio Port adds S/PDIF and USB.
    """
    options = hass.states.get(ZONE_ENTITY).attributes["source_list"]
    assert options[0] == "Streaming"
    assert set(options) == {"Streaming", "eARC", "Line In", "S/PDIF", "USB"}


async def test_a_zone_reports_its_membership_as_attributes(
    hass: HomeAssistant, synced_zone: MockConfigEntry
) -> None:
    attrs = hass.states.get(ZONE_ENTITY).attributes
    assert attrs["group_id"] == ZONE_ID
    assert sorted(attrs["group_members"]) == sorted([AMP_MAC, PORT_MAC])
    assert attrs["host_mac"] == AMP_MAC
    assert attrs["wb_enable"] is False


async def test_a_broadcasting_zone_reads_as_playing(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    assert hass.states.get(ZONE_ENTITY).state == "idle"

    body = groups_body(wb_enable=True, wb_device=PORT_MAC, wb_input="spdif")
    amp.emit("groups", body)
    port.emit("groups", body)
    await settle(hass)

    state = hass.states.get(ZONE_ENTITY)
    assert state.state == "playing"
    # Resolved against the broadcasting speaker's own platform: the same
    # label is a different device value on an Audio Port than on a PowerAmp.
    assert state.attributes["source"] == "S/PDIF"
