"""Every MQTT event the coordinator dispatches, and what it changes.

The dispatch table is a chain of `elif`s over an event name, and an event
whose branch is missing costs nothing at the time: the speaker keeps sending
it, the state simply never moves. That is how the In Zone sensor sat on one
value for a release. So each event the integration subscribes to is emitted
here and something observable is asserted afterwards.

Bodies are shaped as the devices send them, which for four of these is a bare
list rather than an object.
"""

from __future__ import annotations

import logging

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import entry_coordinator
from .const import AMP_ID, fixture
from .fake_mqtt import FakeDevice


def _amp_state(hass: HomeAssistant, entry: MockConfigEntry):
    return entry_coordinator(hass, entry).data[AMP_ID]


# ── Events with an entity behind them ─────────────────────────────────────


async def test_alarms(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    """A bare list, not an object. Reading it as one leaves the sensor at 0."""
    amp.emit("alarms", [{"id": "a1", "hour": 7}, {"id": "a2", "hour": 8}])
    await settle(hass)
    assert hass.states.get("sensor.living_room_alarms").state == "2"


async def test_quiet_hours(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    amp.emit("quiet_hours", [{"id": "q1", "start_hour": 22}])
    await settle(hass)
    assert hass.states.get("sensor.living_room_quiet_hours").state == "1"


async def test_a_list_event_that_is_not_a_list_is_ignored(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    """A malformed payload must not replace a good value with a broken one."""
    amp.emit("alarms", [{"id": "a1"}])
    await settle(hass)
    amp.emit("alarms", {"unexpected": "shape"})
    await settle(hass)
    assert hass.states.get("sensor.living_room_alarms").state == "1"


async def test_announcement_files_and_schedule(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    amp.emit(
        "announcement",
        {"files": [{"name": "a.wav", "length": 2}], "schedule": [{"id": "s1"}]},
    )
    await settle(hass)
    assert hass.states.get("sensor.living_room_announcements").state == "1"
    assert _amp_state(hass, setup_direct).ann_schedule == [{"id": "s1"}]


async def test_announcement_chime(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    amp.emit("announce_chime", {"chime": "None"})
    await settle(hass)
    assert hass.states.get("select.living_room_announcement_chime").state == "None"


async def test_announcement_volume(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    amp.emit("announcement_vol", {"value": 65})
    await settle(hass)
    assert hass.states.get("number.living_room_announcement_volume").state == "65.0"


async def test_voice_enhancement(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    amp.emit("voice_enhancement", {"enable": True})
    await settle(hass)
    assert hass.states.get("switch.living_room_voice_enhancement").state == "on"


async def test_streaming_timeout(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    amp.emit("streaming_timeout", {"second": 300})
    await settle(hass)
    assert hass.states.get("select.living_room_streaming_timeout").state == "5 Minutes"


async def test_sub_audio(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    amp: FakeDevice,
    settle,
) -> None:
    """The subwoofer entities exist only once a speaker reports one attached.

    An amp with nothing plugged into the sub output would otherwise carry
    three controls that do nothing.
    """
    amp.emit("sub_audio", {"crossover": 90, "level": -4, "phase": 180, "subwoofer": 1})
    await settle(hass)

    state = _amp_state(hass, setup_direct)
    assert (state.sub_crossover, state.sub_level, state.sub_phase) == (90, -4, 180)
    assert state.subwoofer == 1
    assert hass.states.get("number.living_room_sub_crossover").state == "90.0"


# ── Events read through the coordinator ───────────────────────────────────


async def test_extra_info(
    hass: HomeAssistant, setup_direct: MockConfigEntry, port: FakeDevice, settle
) -> None:
    from .const import PORT_ID

    port.emit("extra_info", fixture("mqtt_extra_info_port.json"))
    await settle(hass)

    state = entry_coordinator(hass, setup_direct).data[PORT_ID]
    # The version string carries a build suffix; a greedy version regex once
    # read "1.1.10.9" out of it.
    assert state.firmware == "1.1.10"
    assert state.platform == "UPL-PORT"
    assert state.uptime == 864000
    assert state.link_quality == 78


async def test_an_info_action_is_recorded(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    """The speaker echoes which action produced the update.

    Only useful in a diagnostics report, which is exactly where a state that
    silently stopped being tracked would go unnoticed.
    """
    amp.emit("info", {"info_action": "set_volume", "volume": 30})
    await settle(hass)
    assert _amp_state(hass, setup_direct).info_action == "set_volume"


async def test_metadata_falls_back_to_song(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    """Two spellings from two sources, both seen in captures."""
    amp.emit("online", {"status": 1})
    amp.emit("metadata", {"song": "Weightless", "artist": "Marconi Union"})
    await settle(hass)
    assert (
        hass.states.get("media_player.living_room").attributes["media_title"]
        == "Weightless"
    )


async def test_an_event_for_a_device_that_is_no_longer_set_up(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    """An event can arrive between the state being dropped and the client
    being torn down, and there is nothing to update it into."""
    coordinator = entry_coordinator(hass, setup_direct)
    coordinator.data.pop(AMP_ID)

    amp.emit("info", {"volume": 11})
    await settle(hass)
    assert AMP_ID not in coordinator.data


# ── The custom EQ preset wipe ─────────────────────────────────────────────


async def test_custom_presets_disappearing_is_recorded(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    amp: FakeDevice,
    settle,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A PowerAmp lost its saved presets across an unattended reboot once,
    and nobody noticed for weeks. The cause is still undetermined, so the
    next occurrence carries a timestamp instead."""
    amp.emit(
        "equalizer",
        {"custom_presets": [{"name": "Late Night"}, "not-a-dict"], "eq_enable": True},
    )
    await settle(hass)

    with caplog.at_level(
        logging.WARNING, logger="custom_components.unifi_play.coordinator"
    ):
        amp.emit("equalizer", {"custom_presets": []})
        await settle(hass)

    assert "custom EQ presets are now empty" in caplog.text
    assert "Late Night" in caplog.text
    assert _amp_state(hass, setup_direct).eq_custom_presets == []


async def test_presets_arriving_for_the_first_time_are_not_a_wipe(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    amp: FakeDevice,
    settle,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(
        logging.WARNING, logger="custom_components.unifi_play.coordinator"
    ):
        amp.emit("equalizer", {"custom_presets": []})
        await settle(hass)
    assert "custom EQ presets are now empty" not in caplog.text


# ── Zone lookups with nothing to look up ──────────────────────────────────


async def test_zone_lookups_on_a_zone_that_is_not_there(
    hass: HomeAssistant, setup_direct: MockConfigEntry
) -> None:
    coordinator = entry_coordinator(hass, setup_direct)
    assert coordinator.get_host_mqtt_client("nope") is None
    assert coordinator.get_zone_host_state("nope") is None
    assert coordinator.get_zone_members("nope") == []
    assert coordinator.get_groups_hosted_by("AABBCCDDEEFF") == []
    assert coordinator.get_mqtt_client_for_mac("FFFFFFFFFFFF") is None


async def test_zone_lookups_before_a_host_is_elected(
    hass: HomeAssistant, synced_zone: MockConfigEntry
) -> None:
    """The speakers elect one themselves, a few seconds after the write."""
    from .const import ZONE_ID

    coordinator = entry_coordinator(hass, synced_zone)
    assert coordinator.get_groups_hosted_by("AABBCCDDEEFF") != []

    coordinator.groups[ZONE_ID].host_mac = ""
    assert coordinator.get_host_mqtt_client(ZONE_ID) is None
    assert coordinator.get_zone_host_state(ZONE_ID) is None


async def test_a_host_no_speaker_here_knows_about(
    hass: HomeAssistant, synced_zone: MockConfigEntry
) -> None:
    """A zone whose host is on a VLAN this install cannot reach.

    The zone is still reported by the members, so it exists here with a host
    that resolves to nothing.
    """
    from .const import ZONE_ID

    coordinator = entry_coordinator(hass, synced_zone)
    coordinator.groups[ZONE_ID].host_mac = "AABBCCDDEE99"
    assert coordinator.get_host_mqtt_client(ZONE_ID) is None
    assert coordinator.get_zone_host_state(ZONE_ID) is None
