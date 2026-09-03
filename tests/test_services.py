"""Device-level actions: announcements, alarms, quiet hours and EQ presets.

These cover the parts of a speaker that have no natural Home Assistant
entity. Every one maps to an MQTT action captured from the official app; the
assertions here are on the wire body, because that is the only thing the
speaker sees.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_play.const import DOMAIN
from custom_components.unifi_play.services import SERVICE_NAMES

from .conftest import entry_coordinator
from .const import AMP_MAC, fixture
from .fake_mqtt import FakeDevice


def _device_id(hass: HomeAssistant, entry: MockConfigEntry, mac: str = AMP_MAC) -> str:
    registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        if (DOMAIN, mac) in device.identifiers:
            return device.id
    raise AssertionError(f"no registry device for {mac}")


async def _call(hass: HomeAssistant, service: str, **data) -> None:
    await hass.services.async_call(DOMAIN, service, data, blocking=True)


# ── Registration ──────────────────────────────────────────────────────────


async def test_every_declared_action_is_registered(
    hass: HomeAssistant, setup_direct: MockConfigEntry
) -> None:
    """SERVICE_NAMES, services.yaml and strings.json have to agree.

    Ten actions once shipped with no names at all, showing in the UI as bare
    keys, because one of the four places was missed.
    """
    for name in SERVICE_NAMES:
        assert hass.services.has_service(DOMAIN, name), name


async def test_the_declared_names_match_services_yaml(
    hass: HomeAssistant, setup_direct: MockConfigEntry
) -> None:
    import pathlib

    import yaml

    path = (
        pathlib.Path(__file__).parent.parent
        / "custom_components"
        / "unifi_play"
        / "services.yaml"
    )
    declared = set(yaml.safe_load(path.read_text(encoding="utf-8")))
    assert declared == set(SERVICE_NAMES)


async def test_every_action_has_a_name_and_description(
    hass: HomeAssistant, setup_direct: MockConfigEntry
) -> None:
    import json
    import pathlib

    path = (
        pathlib.Path(__file__).parent.parent
        / "custom_components"
        / "unifi_play"
        / "strings.json"
    )
    services = json.loads(path.read_text(encoding="utf-8"))["services"]
    for name in SERVICE_NAMES:
        assert name in services, name
        assert services[name].get("name"), name
        assert services[name].get("description"), name


# ── Announcements ─────────────────────────────────────────────────────────


async def test_play_announcement(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice
) -> None:
    amp.clear()
    await _call(
        hass,
        "play_announcement",
        device_id=_device_id(hass, setup_direct),
        filename="closing.wav",
        length=4,
    )
    body = amp.last_action("announce").body
    assert body["filename"] == "prerecord/closing.wav"
    assert body["length"] == 4
    assert body["enable"] is True


async def test_play_announcement_reuses_the_reported_length(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    """The speaker already knows how long its own clips are.

    Making the caller supply it means an automation breaks when the clip is
    re-recorded.
    """
    amp.emit(
        "announcement",
        {"files": [{"name": "closing.wav", "length": 9}], "schedule": []},
    )
    await settle(hass)
    amp.clear()

    await _call(
        hass,
        "play_announcement",
        device_id=_device_id(hass, setup_direct),
        filename="closing.wav",
    )
    assert amp.last_action("announce").body["length"] == 9


async def test_stop_announcement(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice
) -> None:
    amp.clear()
    await _call(hass, "stop_announcement", device_id=_device_id(hass, setup_direct))
    assert amp.last_action("announce").body == {"enable": False}


async def test_delete_announcement_file(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    amp.emit(
        "announcement",
        {"files": [{"name": "old.wav", "length": 3}], "schedule": []},
    )
    await settle(hass)
    amp.clear()

    await _call(
        hass,
        "delete_announcement_file",
        device_id=_device_id(hass, setup_direct),
        filename="old.wav",
    )
    body = amp.last_action("announce").body
    assert body["action"] == "del_file"
    assert body["files"] == [{"name": "old.wav", "length": 3}]


async def test_an_action_on_a_disconnected_speaker_fails(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    device_id = _device_id(hass, setup_direct)
    amp.drop()
    await settle(hass)
    amp.clear()

    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "stop_announcement", device_id=device_id)
    assert err.value.translation_key in ("device_not_connected", "no_live_device")
    assert amp.published_actions("announce") == []


async def test_an_unknown_device_id_is_rejected(
    hass: HomeAssistant, setup_direct: MockConfigEntry
) -> None:
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "stop_announcement", device_id="not-a-device")
    assert err.value.translation_key == "unknown_device"


# ── Alarms ────────────────────────────────────────────────────────────────


async def test_set_alarm_creates(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice
) -> None:
    amp.clear()
    await _call(
        hass,
        "set_alarm",
        device_id=_device_id(hass, setup_direct),
        name="Wake up",
        hour=7,
        minute=30,
        repeat=[1, 2, 3, 4, 5],
    )
    body = amp.last_action("set_alarm").body
    assert body["action"] == "add"
    assert (body["hour"], body["minute"]) == (7, 30)
    assert body["repeat"] == [1, 2, 3, 4, 5]
    assert body["alarm_id"]


async def test_set_alarm_modifies_when_given_an_id(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice
) -> None:
    """Passing an id means "change this one", not "make another"."""
    amp.clear()
    await _call(
        hass,
        "set_alarm",
        device_id=_device_id(hass, setup_direct),
        alarm_id="alarm-1",
        hour=8,
        minute=0,
    )
    body = amp.last_action("set_alarm").body
    assert body["action"] == "mod"
    assert body["alarm_id"] == "alarm-1"


async def test_delete_alarm(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice
) -> None:
    amp.clear()
    await _call(
        hass,
        "delete_alarm",
        device_id=_device_id(hass, setup_direct),
        alarm_id="alarm-1",
    )
    assert amp.last_action("set_alarm").body == {
        "action": "del",
        "alarm_id": "alarm-1",
    }


# ── Quiet hours ───────────────────────────────────────────────────────────


async def test_set_quiet_hours(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice
) -> None:
    amp.clear()
    await _call(
        hass,
        "set_quiet_hours",
        device_id=_device_id(hass, setup_direct),
        start_hour=22,
        end_hour=7,
    )
    body = amp.last_action("set_quiet_hour").body
    assert body["action"] == "add"
    assert (body["start_hour"], body["end_hour"]) == (22, 7)


async def test_delete_quiet_hours(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice
) -> None:
    amp.clear()
    await _call(
        hass,
        "delete_quiet_hours",
        device_id=_device_id(hass, setup_direct),
        quiet_id="quiet-1",
    )
    assert amp.last_action("set_quiet_hour").body == {
        "action": "del",
        "id": "quiet-1",
    }


# ── EQ presets ────────────────────────────────────────────────────────────


async def test_save_eq_preset_uses_the_current_table(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    amp.emit("equalizer", fixture("mqtt_equalizer.json"))
    await settle(hass)
    amp.clear()

    await _call(
        hass,
        "save_eq_preset",
        device_id=_device_id(hass, setup_direct),
        name="Late night",
    )
    body = amp.last_action("set_equalizer").body
    assert body["preset_action"] == "add"
    assert body["preset_name"] == "Late night"
    assert body["table"]["125"] == 2.5


async def test_save_eq_preset_before_the_speaker_has_reported_one(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice
) -> None:
    """There is nothing to save yet, and saving an empty table would wipe it."""
    amp.clear()
    with pytest.raises(ServiceValidationError) as err:
        await _call(
            hass,
            "save_eq_preset",
            device_id=_device_id(hass, setup_direct),
            name="Empty",
        )
    assert err.value.translation_key == "eq_table_unavailable"
    assert amp.published_actions("set_equalizer") == []


async def test_delete_and_rename_eq_preset(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice
) -> None:
    device_id = _device_id(hass, setup_direct)
    amp.clear()

    await _call(hass, "delete_eq_preset", device_id=device_id, name="Late night")
    assert amp.last_action("set_equalizer").body["preset_action"] == "del"

    await _call(
        hass,
        "rename_eq_preset",
        device_id=device_id,
        name="Late night",
        new_name="Evening",
    )
    body = amp.last_action("set_equalizer").body
    assert body["preset_action"] == "mod"
    assert body["preset_rename"] == "Evening"


# ── Zone announcements ────────────────────────────────────────────────────


async def test_play_zone_announcement_goes_to_the_host(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
) -> None:
    """The host fans the announcement out to the zone; sending it to every
    member would play it several times over."""
    amp.clear()
    port.clear()
    await hass.services.async_call(
        DOMAIN,
        "play_zone_announcement",
        {"entity_id": "media_player.downstairs", "filename": "closing.wav"},
        blocking=True,
    )
    body = amp.last_action("announce").body
    assert body["zone_play"] is True
    assert port.published_actions("announce") == []


@pytest.mark.parametrize(
    "filename", ["../../etc/passwd", "/../evil.wav", "a/../../b/evil.wav"]
)
async def test_a_zone_announcement_filename_cannot_escape(
    hass: HomeAssistant, synced_zone: MockConfigEntry, amp: FakeDevice, filename: str
) -> None:
    amp.clear()
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "play_zone_announcement",
            {"entity_id": "media_player.downstairs", "filename": filename},
            blocking=True,
        )
    assert err.value.translation_key == "filename_traversal"
    assert amp.published_actions("announce") == []


async def test_a_zone_action_on_a_speaker_entity_is_rejected(
    hass: HomeAssistant, synced_zone: MockConfigEntry
) -> None:
    """Zone actions take the zone's own entity, not a speaker's."""
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "delete_zone",
            {"entity_id": "media_player.living_room"},
            blocking=True,
        )
    assert err.value.translation_key == "not_a_zone_entity"


async def test_a_zone_action_on_an_unknown_entity_is_rejected(
    hass: HomeAssistant, synced_zone: MockConfigEntry
) -> None:
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "delete_zone",
            {"entity_id": "media_player.nothing_here"},
            blocking=True,
        )
    assert err.value.translation_key == "unknown_entity"


async def test_resolve_prefers_a_connected_coordinator(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    amp: FakeDevice,
    settle,
) -> None:
    """The device registry keys on MAC, so two entries for one speaker merge
    into a single registry device. The caller must land on the entry that
    actually holds a live connection (#15)."""
    from custom_components.unifi_play.helpers import resolve_device

    coordinator, dev_id = resolve_device(hass, _device_id(hass, setup_direct))
    assert coordinator is entry_coordinator(hass, setup_direct)
    assert coordinator.get_mqtt_client(dev_id) is not None
