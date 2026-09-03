"""Canonical zone state and topology events.

The property that matters throughout: **one logical change produces one
event**, whatever number of speakers report it. Every member of a zone lists
that zone in its own ``groups`` payload, so a five-speaker zone is reported
five times, and diffing each device's copy against its own previous copy
turned one rename into five renames — and turned a device leaving a zone into
a deletion of a zone that was still there.
"""

from __future__ import annotations

import logging

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_play.const import (
    EVENT_ZONE_CREATED,
    EVENT_ZONE_DELETED,
    EVENT_ZONE_MEMBER_CHANGED,
    EVENT_ZONE_RENAMED,
)

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
    empty_groups_body,
    groups_body,
    zone_member,
)
from .fake_mqtt import FakeDevice

AMP_MEMBER = zone_member(AMP_MAC, AMP_NAME, AMP_IP, platform="UPL-AMP", host=True)
PORT_MEMBER = zone_member(PORT_MAC, PORT_NAME, PORT_IP)
THIRD_MEMBER = zone_member(THIRD_MAC, THIRD_NAME, THIRD_IP)


@pytest.fixture
async def synced(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
    zone_events,
):
    """Both speakers have completed their first zone sync, reporting one zone.

    This is the state everything after startup happens from: the initial
    sync is silent by design, so a test that wants to observe an event has to
    get past it first.
    """
    body = groups_body()
    amp.emit("groups", body)
    port.emit("groups", body)
    await settle(hass)
    assert zone_events == [], "the first sync of each device must be silent"
    return setup_direct


def _types(events) -> list[str]:
    return [event_type for event_type, _ in events]


# ── Startup ───────────────────────────────────────────────────────────────


async def test_the_first_sync_is_silent(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
    zone_events,
) -> None:
    """Zones that existed before Home Assistant connected are not new.

    Announcing them would fire a burst on every start and every reload, and
    an automation cannot tell that burst from a real one.
    """
    amp.emit("groups", groups_body())
    port.emit("groups", groups_body())
    await settle(hass)

    assert zone_events == []
    coordinator = entry_coordinator(hass, setup_direct)
    assert set(coordinator.groups) == {ZONE_ID}


async def test_a_speaker_connecting_later_announces_nothing(
    hass: HomeAssistant,
    synced: MockConfigEntry,
    third: FakeDevice,
    udp_discovery,
    settle,
    zone_events,
) -> None:
    """A third speaker joining mid-session reports zones it already knew.

    Its first sync adds them to the canonical view, which is discovery, not
    a topology change.
    """
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from custom_components.unifi_play.coordinator import DISCOVERY_INTERVAL

    from .const import third_device

    udp_discovery.append(third_device())
    async_fire_time_changed(hass, dt_util.utcnow() + DISCOVERY_INTERVAL)
    await settle(hass)

    third.emit(
        "groups",
        groups_body(
            group_id="another-zone",
            name="Upstairs",
            members=[THIRD_MEMBER, PORT_MEMBER],
        ),
    )
    await settle(hass)

    assert zone_events == []
    coordinator = entry_coordinator(hass, synced)
    assert set(coordinator.groups) == {ZONE_ID, "another-zone"}


# ── One change, one event ─────────────────────────────────────────────────


async def test_two_speakers_reporting_the_same_rename_fire_one_event(
    hass: HomeAssistant,
    synced: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
    zone_events,
) -> None:
    """The event count is a property of the change, not of the fleet size."""
    renamed = groups_body(name="Ground Floor")
    amp.emit("groups", renamed)
    port.emit("groups", renamed)
    await settle(hass)

    assert _types(zone_events) == [EVENT_ZONE_RENAMED]
    assert zone_events[0][1] == {
        "group_id": ZONE_ID,
        "name": "Ground Floor",
        "previous_name": ZONE_NAME,
    }


async def test_a_member_joining_fires_one_event_from_two_reports(
    hass: HomeAssistant,
    synced: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
    zone_events,
) -> None:
    grown = groups_body(members=[AMP_MEMBER, PORT_MEMBER, THIRD_MEMBER])
    amp.emit("groups", grown)
    port.emit("groups", grown)
    await settle(hass)

    assert _types(zone_events) == [EVENT_ZONE_MEMBER_CHANGED]
    payload = zone_events[0][1]
    assert payload["added_macs"] == [THIRD_MAC]
    assert payload["removed_macs"] == []


async def test_a_member_leaving_fires_one_event(
    hass: HomeAssistant,
    synced: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
    zone_events,
) -> None:
    shrunk = groups_body(members=[AMP_MEMBER, THIRD_MEMBER])
    amp.emit("groups", shrunk)
    port.emit("groups", shrunk)
    await settle(hass)

    assert _types(zone_events) == [EVENT_ZONE_MEMBER_CHANGED]
    payload = zone_events[0][1]
    assert payload["added_macs"] == [THIRD_MAC]
    assert payload["removed_macs"] == [PORT_MAC]


async def test_a_new_zone_fires_one_created_event(
    hass: HomeAssistant,
    synced: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
    zone_events,
) -> None:
    extra = {
        "group_id": "zone-2",
        "name": "Upstairs",
        "dev_info": [dict(PORT_MEMBER, host=True), THIRD_MEMBER],
        "dev_count": 2,
        "group_index": 2,
        "broadcasting_mode": "zone_only",
        "wb_enable": False,
        "wb_device": "",
        "wb_input": "",
    }
    both = groups_body(extra_groups=[extra])
    amp.emit("groups", both)
    port.emit("groups", both)
    await settle(hass)

    assert _types(zone_events) == [EVENT_ZONE_CREATED]
    assert zone_events[0][1]["group_id"] == "zone-2"
    assert zone_events[0][1]["name"] == "Upstairs"
    assert zone_events[0][1]["dev_count"] == 2


async def test_a_deleted_zone_fires_one_deleted_event(
    hass: HomeAssistant,
    synced: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
    zone_events,
) -> None:
    amp.emit("groups", empty_groups_body())
    port.emit("groups", empty_groups_body())
    await settle(hass)

    assert _types(zone_events) == [EVENT_ZONE_DELETED]
    assert zone_events[0][1] == {"group_id": ZONE_ID, "name": ZONE_NAME}


async def test_one_speaker_dropping_a_zone_is_not_a_deletion(
    hass: HomeAssistant,
    synced: MockConfigEntry,
    port: FakeDevice,
    settle,
    zone_events,
) -> None:
    """A device that leaves a zone stops listing it.

    Under the old per-device diff that read as the zone being deleted, even
    though every other speaker was still reporting it - and the zone was
    still playing.
    """
    port.emit("groups", empty_groups_body())
    await settle(hass)

    assert zone_events == []
    coordinator = entry_coordinator(hass, synced)
    assert ZONE_ID in coordinator.groups


# ── Non-changes ───────────────────────────────────────────────────────────


async def test_an_identical_payload_fires_nothing(
    hass: HomeAssistant,
    synced: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
    zone_events,
) -> None:
    """Devices re-report on reconnect and after every write."""
    for _ in range(3):
        amp.emit("groups", groups_body())
        port.emit("groups", groups_body())
    await settle(hass)
    assert zone_events == []


async def test_a_reordered_member_list_fires_nothing(
    hass: HomeAssistant,
    synced: MockConfigEntry,
    amp: FakeDevice,
    settle,
    zone_events,
) -> None:
    """Nothing requires a device to list members in a stable order."""
    amp.emit("groups", groups_body(members=[PORT_MEMBER, AMP_MEMBER]))
    await settle(hass)
    assert zone_events == []


async def test_a_host_election_alone_fires_nothing(
    hass: HomeAssistant,
    synced: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
    zone_events,
) -> None:
    """``host`` is firmware-owned and moves without the zone changing.

    The membership is identical; only which speaker carries the role has
    moved, and an automation triggering on that would fire on a reboot.
    """
    handed_over = groups_body(
        members=[dict(AMP_MEMBER, host=False), dict(PORT_MEMBER, host=True)]
    )
    amp.emit("groups", handed_over)
    port.emit("groups", handed_over)
    await settle(hass)
    assert zone_events == []


# ── Conflicting copies ────────────────────────────────────────────────────


async def test_the_hosts_copy_wins(
    hass: HomeAssistant,
    synced: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    """After an edit the host has the new state and members serve the old one.

    A plain merge lets a stale copy land last and silently revert the edit.
    """
    amp.emit("groups", groups_body(name="Ground Floor"))  # host, updated
    port.emit("groups", groups_body(name=ZONE_NAME))  # member, stale
    await settle(hass)

    coordinator = entry_coordinator(hass, synced)
    assert coordinator.groups[ZONE_ID].name == "Ground Floor"


async def test_a_stale_copy_arriving_last_does_not_revert(
    hass: HomeAssistant,
    synced: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
    zone_events,
) -> None:
    """Order of arrival must not decide the answer."""
    amp.emit("groups", groups_body(name="Ground Floor"))
    await settle(hass)
    assert _types(zone_events) == [EVENT_ZONE_RENAMED]

    port.emit("groups", groups_body(name=ZONE_NAME))  # the member catching up
    await settle(hass)

    coordinator = entry_coordinator(hass, synced)
    assert coordinator.groups[ZONE_ID].name == "Ground Floor"
    assert _types(zone_events) == [EVENT_ZONE_RENAMED]


async def test_two_devices_claiming_host_resolve_the_same_way_every_time(
    hass: HomeAssistant,
    synced: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    """Both ends of a handover claim the role until the old host resyncs.

    Whatever the merge picks, it has to pick the same thing however the
    reports are ordered - otherwise the zone's name flickers as events
    arrive, and so does everything routed through its host.
    """
    amp_claim = groups_body(
        name="Amp copy",
        members=[dict(AMP_MEMBER, host=True), dict(PORT_MEMBER, host=False)],
    )
    port_claim = groups_body(
        name="Port copy",
        members=[dict(AMP_MEMBER, host=False), dict(PORT_MEMBER, host=True)],
    )

    amp.emit("groups", amp_claim)
    port.emit("groups", port_claim)
    await settle(hass)
    coordinator = entry_coordinator(hass, synced)
    first = coordinator.groups[ZONE_ID].name

    # Same inputs, opposite arrival order.
    port.emit("groups", port_claim)
    amp.emit("groups", amp_claim)
    await settle(hass)
    assert coordinator.groups[ZONE_ID].name == first


async def test_a_zone_with_no_elected_host_still_resolves(
    hass: HomeAssistant,
    synced: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    """A freshly written zone has no host until the firmware elects one."""
    hostless = groups_body(
        members=[dict(AMP_MEMBER, host=False), dict(PORT_MEMBER, host=False)]
    )
    amp.emit("groups", hostless)
    port.emit("groups", hostless)
    await settle(hass)

    coordinator = entry_coordinator(hass, synced)
    assert coordinator.groups[ZONE_ID].host_mac == ""
    assert coordinator.groups[ZONE_ID].name == ZONE_NAME


async def test_a_conflict_is_logged_once_and_recovery_once(
    hass: HomeAssistant,
    synced: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
    caplog,
) -> None:
    """Disagreement is normal for a moment and a problem when it persists.

    A line per event drowns that distinction; a line per transition shows it.
    """
    caplog.set_level(logging.INFO)

    amp.emit("groups", groups_body(name="Ground Floor"))
    await settle(hass)
    assert caplog.text.count("Speakers disagree about zone") == 1

    # More reports, same disagreement: nothing new to say.
    amp.emit("groups", groups_body(name="Ground Floor"))
    port.emit("groups", groups_body(name=ZONE_NAME))
    await settle(hass)
    assert caplog.text.count("Speakers disagree about zone") == 1
    assert "Speakers now agree" not in caplog.text

    port.emit("groups", groups_body(name="Ground Floor"))
    await settle(hass)
    assert caplog.text.count("Speakers now agree about zone") == 1


async def test_a_host_election_is_not_reported_as_a_conflict(
    hass: HomeAssistant,
    synced: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
    caplog,
) -> None:
    """Both ends claim the role while a zone changes hands. That is normal."""
    caplog.set_level(logging.INFO)
    amp.emit(
        "groups",
        groups_body(members=[dict(AMP_MEMBER, host=True), dict(PORT_MEMBER)]),
    )
    port.emit(
        "groups",
        groups_body(
            members=[dict(AMP_MEMBER, host=False), dict(PORT_MEMBER, host=True)]
        ),
    )
    await settle(hass)
    assert "Speakers disagree about zone" not in caplog.text


# ── Entity state comes from the same view ─────────────────────────────────


async def test_the_zone_entity_reads_the_canonical_state(
    hass: HomeAssistant,
    synced: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    """Entity state and events must not be able to disagree.

    They come from one rebuild of one canonical view, so a rename shows on
    the entity and fires exactly one event.
    """
    state = hass.states.get("media_player.downstairs")
    assert state is not None
    assert state.attributes["group_id"] == ZONE_ID

    renamed = groups_body(name="Ground Floor")
    amp.emit("groups", renamed)
    port.emit("groups", renamed)
    await settle(hass)

    coordinator = entry_coordinator(hass, synced)
    assert coordinator.groups[ZONE_ID].name == "Ground Floor"


async def test_a_malformed_group_entry_is_skipped_not_fatal(
    hass: HomeAssistant,
    synced: MockConfigEntry,
    amp: FakeDevice,
    settle,
) -> None:
    """A group with no id cannot be addressed, so it cannot be tracked."""
    body = groups_body()
    body["groups"].append({"name": "No id here"})
    amp.emit("groups", body)
    await settle(hass)

    coordinator = entry_coordinator(hass, synced)
    assert set(coordinator.groups) == {ZONE_ID}
