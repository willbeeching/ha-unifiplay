"""Zone management through the Configure button.

The options flow is the only zone editor most people will ever use, and it is
a state machine: twelve steps, several of which can be entered against a zone
that stopped existing between one click and the next. It builds no payloads of
its own — every mutation is a call into ``coordinator.zones`` — so what is
tested here is the flow's own behaviour: what it offers, what it refuses to
show, and whether a refusal from the write path reaches the user as the
sentence that says what to do about it.

The write path itself is tested in test_zone_actions.py.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_play.config_flow import UnifiPlayOptionsFlow

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


def _device_id(hass: HomeAssistant, entry: MockConfigEntry, mac: str) -> str:
    """The registry id for a speaker, which is what the pickers return."""
    registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        for _domain, identifier in device.identifiers:
            if identifier.replace(":", "").upper() == mac.replace(":", "").upper():
                return device.id
    raise AssertionError(f"no device registered for {mac}")


async def _open(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    return await hass.config_entries.options.async_init(entry.entry_id)


async def _step(hass: HomeAssistant, result: dict[str, Any], step: str) -> Any:
    """Pick a menu option."""
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step}
    )


async def _submit(
    hass: HomeAssistant, result: dict[str, Any], data: dict[str, Any]
) -> Any:
    return await hass.config_entries.options.async_configure(result["flow_id"], data)


async def _on_zone(
    hass: HomeAssistant, entry: MockConfigEntry, step: str, zone_id: str = ZONE_ID
) -> Any:
    """Walk menu → select_zone → zone_action → the step under test."""
    result = await _open(hass, entry)
    result = await _step(hass, result, "select_zone")
    result = await _submit(hass, result, {"zone_id": zone_id})
    return await _step(hass, result, step)


def _written(device: FakeDevice) -> list[dict[str, Any]]:
    return device.last_action("set_groups").body["groups"]


def _detached_flow(hass: HomeAssistant, entry: MockConfigEntry) -> UnifiPlayOptionsFlow:
    """An options flow not driven by the flow manager.

    For the two branches that only a malformed submission reaches. Home
    Assistant validates every submission against the schema the form was
    rendered with, so neither can be produced through async_configure; they
    are the defence against a client that gets past that, and skipping them
    would mean the defence is never executed at all.
    """
    flow = UnifiPlayOptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id
    return flow


def _marker(result: dict[str, Any], key: str) -> Any:
    """The voluptuous marker for a field, which is where the default lives.

    Indexing the schema by name gives the selector, not the marker, so a
    default read that way is silently absent.
    """
    for marker in result["data_schema"].schema:
        if marker == key:
            return marker
    raise AssertionError(f"{key} not in the form")


def _default(result: dict[str, Any], key: str) -> Any:
    return _marker(result, key).default()


def _options(result: dict[str, Any], key: str) -> list[dict[str, str]]:
    return result["data_schema"].schema[key].config["options"]


@pytest.fixture
async def three_speakers(
    hass: HomeAssistant,
    direct_entry: MockConfigEntry,
    udp_discovery,
    amp: FakeDevice,
    port: FakeDevice,
    third: FakeDevice,
    settle,
) -> MockConfigEntry:
    """Three speakers, two of them in a zone, so a third can be added."""
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


# ── The menu ──────────────────────────────────────────────────────────────


async def test_the_menu_offers_create_and_modify(
    hass: HomeAssistant, setup_direct: MockConfigEntry
) -> None:
    result = await _open(hass, setup_direct)
    assert result["type"] is FlowResultType.MENU
    assert set(result["menu_options"]) == {"create_zone", "select_zone"}


async def test_the_action_menu_lists_every_zone_operation(
    hass: HomeAssistant, synced_zone: MockConfigEntry
) -> None:
    result = await _open(hass, synced_zone)
    result = await _step(hass, result, "select_zone")
    result = await _submit(hass, result, {"zone_id": ZONE_ID})
    assert result["type"] is FlowResultType.MENU
    assert set(result["menu_options"]) == {
        "rename_zone",
        "add_zone_member",
        "remove_zone_member",
        "set_zone_source",
        "set_zone_broadcasting",
        "reorder_zone",
        "delete_zone",
        "select_zone",
        "init",
    }


async def test_the_back_options_go_back(
    hass: HomeAssistant, synced_zone: MockConfigEntry
) -> None:
    """Both of them are real steps, and a menu option that 500s is a dead end."""
    result = await _on_zone(hass, synced_zone, "select_zone")
    assert result["step_id"] == "select_zone"

    result = await _open(hass, synced_zone)
    result = await _step(hass, result, "select_zone")
    result = await _submit(hass, result, {"zone_id": ZONE_ID})
    result = await _step(hass, result, "init")
    assert result["type"] is FlowResultType.MENU
    assert set(result["menu_options"]) == {"create_zone", "select_zone"}


# ── Creating ──────────────────────────────────────────────────────────────


async def test_creating_a_zone_writes_it_to_both_speakers(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    amp.clear()
    port.clear()
    result = await _open(hass, setup_direct)
    result = await _step(hass, result, "create_zone")
    assert result["step_id"] == "create_zone"

    result = await _submit(
        hass,
        result,
        {
            "name": "Downstairs",
            "device_ids": [
                _device_id(hass, setup_direct, AMP_MAC),
                _device_id(hass, setup_direct, PORT_MAC),
            ],
        },
    )
    await settle(hass)

    # Back to the top menu, which is where a completed action lands.
    assert result["type"] is FlowResultType.MENU
    for device in (amp, port):
        written = _written(device)
        assert len(written) == 1
        assert written[0]["name"] == "Downstairs"
        assert {member["mac"] for member in written[0]["dev_info"]} == {
            AMP_MAC,
            PORT_MAC,
        }


async def test_a_single_option_arrives_as_a_bare_string(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice
) -> None:
    """A multi-select rendered with one option has been seen to do that.

    It is wrapped rather than iterated, because iterating a string produces
    one "device id" per character and the resolve failure that follows says
    nothing useful.

    Driven through the step rather than the flow, because Home Assistant
    validates a submission against the schema first and a bare string never
    gets past it. So this is not a path a browser can reach today; it is the
    defence against the one that was observed reaching it.
    """
    flow = _detached_flow(hass, setup_direct)
    result = await flow.async_step_create_zone(
        {"name": "Lonely", "device_ids": _device_id(hass, setup_direct, AMP_MAC)}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "zone_needs_two_devices"}


async def test_a_device_that_cannot_be_resolved_is_reported(
    hass: HomeAssistant, setup_direct: MockConfigEntry
) -> None:
    """The picker's options are fixed when the form renders.

    Delete the speaker between rendering and submitting and the value still
    passes schema validation while resolving to nothing.
    """
    result = await _open(hass, setup_direct)
    result = await _step(hass, result, "create_zone")
    ids = [
        _device_id(hass, setup_direct, AMP_MAC),
        _device_id(hass, setup_direct, PORT_MAC),
    ]
    dr.async_get(hass).async_remove_device(ids[1])

    result = await _submit(hass, result, {"name": "Ghosts", "device_ids": ids})
    assert result["errors"] == {"base": "resolve_failed"}


async def test_creating_is_refused_when_there_is_nothing_to_group(
    hass: HomeAssistant, synced_zone: MockConfigEntry
) -> None:
    """Both speakers are in a zone already, so the picker would be empty.

    A form whose only field has no options renders a dialog that can never be
    submitted, which reads as the integration being broken.
    """
    result = await _open(hass, synced_zone)
    result = await _step(hass, result, "create_zone")
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_enough_devices"


async def test_one_free_speaker_is_still_not_a_zone(
    hass: HomeAssistant, three_speakers: MockConfigEntry
) -> None:
    """Two of the three are already in a zone, so only one is selectable.

    The picker would render and refuse every submission, which is a worse
    answer than saying so up front.
    """
    result = await _open(hass, three_speakers)
    result = await _step(hass, result, "create_zone")
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_enough_devices"


# ── Selecting ─────────────────────────────────────────────────────────────


async def test_there_is_nothing_to_modify_without_a_zone(
    hass: HomeAssistant, setup_direct: MockConfigEntry
) -> None:
    result = await _open(hass, setup_direct)
    result = await _step(hass, result, "select_zone")
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_zones"


async def test_the_zone_picker_lists_zones_by_name(
    hass: HomeAssistant, synced_zone: MockConfigEntry
) -> None:
    result = await _open(hass, synced_zone)
    result = await _step(hass, result, "select_zone")
    options = _options(result, "zone_id")
    assert options == [{"value": ZONE_ID, "label": ZONE_NAME}]


# ── Renaming ──────────────────────────────────────────────────────────────


async def test_renaming_writes_the_new_name_everywhere(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    amp.clear()
    port.clear()
    result = await _on_zone(hass, synced_zone, "rename_zone")
    assert result["description_placeholders"]["zone_name"] == ZONE_NAME
    assert _default(result, "name") == ZONE_NAME

    result = await _submit(hass, result, {"name": "Ground Floor"})
    await settle(hass)

    assert result["type"] is FlowResultType.MENU
    for device in (amp, port):
        assert _written(device)[0]["name"] == "Ground Floor"


async def test_renaming_a_zone_that_has_gone(
    hass: HomeAssistant, synced_zone: MockConfigEntry, amp: FakeDevice, port: FakeDevice
) -> None:
    """Deleted from the Play app while the dialog was open."""
    result = await _on_zone(hass, synced_zone, "rename_zone")
    entry_coordinator(hass, synced_zone).groups.clear()

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"name": "Too late"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "zone_gone"


# ── Membership ────────────────────────────────────────────────────────────


async def test_adding_a_member(
    hass: HomeAssistant, three_speakers: MockConfigEntry, third: FakeDevice, settle
) -> None:
    result = await _on_zone(hass, three_speakers, "add_zone_member")
    result = await _submit(
        hass, result, {"device_id": _device_id(hass, three_speakers, THIRD_MAC)}
    )
    await settle(hass)

    assert result["type"] is FlowResultType.MENU
    written = _written(third)[0]
    assert {member["mac"] for member in written["dev_info"]} == {
        AMP_MAC,
        PORT_MAC,
        THIRD_MAC,
    }


async def test_adding_offers_only_speakers_that_are_free(
    hass: HomeAssistant, three_speakers: MockConfigEntry
) -> None:
    result = await _on_zone(hass, three_speakers, "add_zone_member")
    options = _options(result, "device_id")
    assert [option["label"] for option in options] == [THIRD_NAME]


async def test_adding_is_refused_when_every_speaker_is_taken(
    hass: HomeAssistant, synced_zone: MockConfigEntry
) -> None:
    result = await _on_zone(hass, synced_zone, "add_zone_member")
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_available_devices"


async def test_adding_a_device_that_cannot_be_resolved(
    hass: HomeAssistant, three_speakers: MockConfigEntry
) -> None:
    result = await _on_zone(hass, three_speakers, "add_zone_member")
    device_id = _device_id(hass, three_speakers, THIRD_MAC)
    dr.async_get(hass).async_remove_device(device_id)

    result = await _submit(hass, result, {"device_id": device_id})
    assert result["errors"] == {"base": "resolve_failed"}


async def test_adding_to_a_zone_that_has_gone(
    hass: HomeAssistant, three_speakers: MockConfigEntry
) -> None:
    entry_coordinator(hass, three_speakers).groups.clear()
    result = await _open(hass, three_speakers)
    result = await _step(hass, result, "select_zone")
    assert result["reason"] == "no_zones"


async def test_removing_a_member(
    hass: HomeAssistant,
    three_speakers: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    third: FakeDevice,
    settle,
) -> None:
    """The zone has to have three in it first, or removal is refused."""
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

    result = await _on_zone(hass, three_speakers, "remove_zone_member")
    labels = {option["label"] for option in _options(result, "member_mac")}
    assert labels == {AMP_NAME, PORT_NAME, THIRD_NAME}

    result = await _submit(hass, result, {"member_mac": THIRD_MAC})
    await settle(hass)
    assert result["type"] is FlowResultType.MENU
    assert {member["mac"] for member in _written(amp)[0]["dev_info"]} == {
        AMP_MAC,
        PORT_MAC,
    }


async def test_removing_below_two_is_refused_with_its_own_message(
    hass: HomeAssistant, synced_zone: MockConfigEntry, amp: FakeDevice
) -> None:
    """ "Delete the zone instead" is the actionable half of that sentence."""
    amp.clear()
    result = await _on_zone(hass, synced_zone, "remove_zone_member")
    result = await _submit(hass, result, {"member_mac": PORT_MAC})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "zone_would_be_too_small"}
    assert amp.published_actions("set_groups") == []


async def test_removing_from_a_zone_that_has_gone(
    hass: HomeAssistant, synced_zone: MockConfigEntry
) -> None:
    result = await _on_zone(hass, synced_zone, "remove_zone_member")
    entry_coordinator(hass, synced_zone).groups.clear()
    result = await _submit(hass, result, {"member_mac": PORT_MAC})
    assert result["reason"] == "zone_gone"


# ── Deleting ──────────────────────────────────────────────────────────────


async def test_deleting_a_zone(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    amp.clear()
    port.clear()
    result = await _on_zone(hass, synced_zone, "delete_zone")
    assert result["description_placeholders"]["zone_name"] == ZONE_NAME

    result = await _submit(hass, result, {})
    await settle(hass)

    assert result["type"] is FlowResultType.MENU
    for device in (amp, port):
        assert _written(device) == []


async def test_deleting_a_zone_that_has_gone(
    hass: HomeAssistant, synced_zone: MockConfigEntry
) -> None:
    result = await _on_zone(hass, synced_zone, "delete_zone")
    entry_coordinator(hass, synced_zone).groups.clear()
    result = await _submit(hass, result, {})
    assert result["reason"] == "zone_gone"


# ── Audio source ──────────────────────────────────────────────────────────


async def test_the_source_form_shows_what_the_zone_is_doing(
    hass: HomeAssistant, synced_zone: MockConfigEntry
) -> None:
    result = await _on_zone(hass, synced_zone, "set_zone_source")
    assert _default(result, "source_mode") == "streaming"


async def test_choosing_streaming_clears_the_broadcast(
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

    result = await _on_zone(hass, setup_direct, "set_zone_source")
    assert _default(result, "source_mode") == "broadcast"

    result = await _submit(hass, result, {"source_mode": "streaming"})
    await settle(hass)

    assert result["type"] is FlowResultType.MENU
    assert _written(amp)[0]["wb_enable"] is False


async def test_choosing_broadcast_asks_which_speaker_and_input(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    amp.clear()
    port.clear()
    result = await _on_zone(hass, synced_zone, "set_zone_source")
    result = await _submit(hass, result, {"source_mode": "broadcast"})
    assert result["step_id"] == "set_zone_wb_device"

    result = await _submit(
        hass, result, {"source_device": PORT_MAC, "input_type": "S/PDIF"}
    )
    await settle(hass)

    assert result["type"] is FlowResultType.MENU
    written = _written(amp)[0]
    assert written["wb_enable"] is True
    assert written["wb_device"] == PORT_MAC
    assert written["wb_input"] == "spdif"


async def test_the_input_list_is_the_union_across_the_zone(
    hass: HomeAssistant, synced_zone: MockConfigEntry
) -> None:
    """An amp has eARC and Line In; a Port adds S/PDIF and USB.

    Offering the intersection would hide inputs that work; the choice is
    validated against the speaker actually picked instead.
    """
    result = await _on_zone(hass, synced_zone, "set_zone_source")
    result = await _submit(hass, result, {"source_mode": "broadcast"})
    labels = [option["value"] for option in _options(result, "input_type")]
    assert set(labels) >= {"Line In", "S/PDIF", "USB"}


async def test_an_input_the_chosen_speaker_does_not_have_is_refused(
    hass: HomeAssistant, synced_zone: MockConfigEntry, amp: FakeDevice
) -> None:
    """The amp accepts `spdif` on the wire and routes nothing to it.

    Which is the whole reason the list is validated per speaker rather than
    trusted: a refusal here is the difference between an error and a zone
    that reports success and plays silence.
    """
    amp.clear()
    result = await _on_zone(hass, synced_zone, "set_zone_source")
    result = await _submit(hass, result, {"source_mode": "broadcast"})
    result = await _submit(
        hass, result, {"source_device": AMP_MAC, "input_type": "S/PDIF"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"input_type": "input_not_on_device"}
    assert amp.published_actions("set_groups") == []


async def test_the_broadcast_form_defaults_to_the_current_source(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    body = groups_body(wb_enable=True, wb_device=PORT_MAC, wb_input="spdif")
    amp.emit("groups", body)
    port.emit("groups", body)
    await settle(hass)

    result = await _on_zone(hass, setup_direct, "set_zone_source")
    result = await _submit(hass, result, {"source_mode": "broadcast"})
    assert _default(result, "source_device") == PORT_MAC
    assert _default(result, "input_type") == "S/PDIF"


async def test_the_broadcast_form_falls_back_to_the_host(
    hass: HomeAssistant, synced_zone: MockConfigEntry
) -> None:
    """No broadcast set yet, so there is no wb_device to default to."""
    result = await _on_zone(hass, synced_zone, "set_zone_source")
    result = await _submit(hass, result, {"source_mode": "broadcast"})
    assert _default(result, "source_device") == AMP_MAC


async def test_the_source_steps_abort_on_a_zone_that_has_gone(
    hass: HomeAssistant, synced_zone: MockConfigEntry
) -> None:
    result = await _on_zone(hass, synced_zone, "set_zone_source")
    entry_coordinator(hass, synced_zone).groups.clear()
    result = await _submit(hass, result, {"source_mode": "streaming"})
    assert result["reason"] == "zone_gone"


async def test_the_broadcast_step_aborts_on_a_zone_that_has_gone(
    hass: HomeAssistant, synced_zone: MockConfigEntry
) -> None:
    result = await _on_zone(hass, synced_zone, "set_zone_source")
    result = await _submit(hass, result, {"source_mode": "broadcast"})
    entry_coordinator(hass, synced_zone).groups.clear()
    result = await _submit(
        hass, result, {"source_device": PORT_MAC, "input_type": "S/PDIF"}
    )
    assert result["reason"] == "zone_gone"


# ── Stream broadcasting ───────────────────────────────────────────────────


async def test_setting_the_broadcasting_mode(
    hass: HomeAssistant, synced_zone: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    amp.clear()
    result = await _on_zone(hass, synced_zone, "set_zone_broadcasting")
    labels = [option["value"] for option in _options(result, "broadcasting_mode")]
    other = next(
        label for label in labels if label != _default(result, "broadcasting_mode")
    )

    result = await _submit(hass, result, {"broadcasting_mode": other})
    await settle(hass)
    assert result["type"] is FlowResultType.MENU
    assert _written(amp)[0]["broadcasting_mode"] != "zone_only"


async def test_a_broadcasting_label_that_is_not_one_of_the_options(
    hass: HomeAssistant, synced_zone: MockConfigEntry, amp: FakeDevice
) -> None:
    """The label is the wire value's only route back, so an unknown one
    cannot be turned into a mode and must not be guessed at."""
    amp.clear()
    flow = _detached_flow(hass, synced_zone)
    await flow.async_step_select_zone({"zone_id": ZONE_ID})
    result = await flow.async_step_set_zone_broadcasting(
        {"broadcasting_mode": "Nonsense"}
    )
    assert result["errors"] == {"broadcasting_mode": "resolve_failed"}
    assert amp.published_actions("set_groups") == []


async def test_broadcasting_aborts_on_a_zone_that_has_gone(
    hass: HomeAssistant, synced_zone: MockConfigEntry
) -> None:
    result = await _on_zone(hass, synced_zone, "set_zone_broadcasting")
    current = _default(result, "broadcasting_mode")
    entry_coordinator(hass, synced_zone).groups.clear()
    result = await _submit(hass, result, {"broadcasting_mode": current})
    assert result["reason"] == "zone_gone"


# ── Reordering ────────────────────────────────────────────────────────────


async def test_reordering_a_zone(
    hass: HomeAssistant, synced_zone: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    amp.clear()
    result = await _on_zone(hass, synced_zone, "reorder_zone")
    assert _default(result, "group_index") == 1

    result = await _submit(hass, result, {"group_index": 7})
    await settle(hass)
    assert result["type"] is FlowResultType.MENU
    assert _written(amp)[0]["group_index"] == 7


async def test_reordering_aborts_on_a_zone_that_has_gone(
    hass: HomeAssistant, synced_zone: MockConfigEntry
) -> None:
    result = await _on_zone(hass, synced_zone, "reorder_zone")
    entry_coordinator(hass, synced_zone).groups.clear()
    result = await _submit(hass, result, {"group_index": 7})
    assert result["reason"] == "zone_gone"


# ── Refusals from the write path ──────────────────────────────────────────


async def test_an_offline_speaker_stops_the_edit_and_says_which(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    """The message names the speaker, so it has to survive the trip out of
    the write path and into the form."""
    port.drop()
    await settle(hass)
    amp.clear()

    result = await _on_zone(hass, synced_zone, "rename_zone")
    result = await _submit(hass, result, {"name": "Ground Floor"})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "zone_members_offline"}
    assert PORT_NAME in result["description_placeholders"]["devices"]
    assert amp.published_actions("set_groups") == []


async def test_adding_a_speaker_that_is_in_another_zone(
    hass: HomeAssistant,
    three_speakers: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    third: FakeDevice,
    settle,
) -> None:
    """The picker hides them, but the zone can change under an open dialog."""
    result = await _on_zone(hass, three_speakers, "add_zone_member")
    device_id = _device_id(hass, three_speakers, THIRD_MAC)

    second = groups_body(
        group_id="other-zone",
        name="Upstairs",
        members=[
            zone_member(THIRD_MAC, THIRD_NAME, AMP_IP, host=True),
            zone_member(PORT_MAC, PORT_NAME, PORT_IP),
        ],
    )
    for device in (amp, port, third):
        device.emit(
            "groups",
            {
                **groups_body(),
                "groups": [
                    groups_body()["groups"][0],
                    second["groups"][0],
                ],
            },
        )
    await settle(hass)

    result = await _submit(hass, result, {"device_id": device_id})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "zone_device_in_other_zone"}


@pytest.mark.parametrize(
    ("step", "submission"),
    [
        ("delete_zone", {}),
        ("set_zone_source", {"source_mode": "streaming"}),
        ("set_zone_broadcasting", {"broadcasting_mode": "Off"}),
        ("reorder_zone", {"group_index": 4}),
    ],
)
async def test_every_edit_refuses_while_a_speaker_is_offline(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
    step: str,
    submission: dict[str, Any],
) -> None:
    """Not just the ones that change the member list.

    A rename or a reorder rewrites the whole document too, so a speaker that
    misses it keeps serving the old one and wins the next merge. Every step
    goes through the same preflight, and this is the test that says so for
    each of them rather than for the one that was easiest to reach.
    """
    result = await _on_zone(hass, synced_zone, step)
    port.drop()
    await settle(hass)
    amp.clear()

    result = await _submit(hass, result, submission)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "zone_members_offline"}
    assert amp.published_actions("set_groups") == []


async def test_setting_a_broadcast_source_refuses_while_a_speaker_is_offline(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    """Reached one step deeper than the rest, through the source picker."""
    result = await _on_zone(hass, synced_zone, "set_zone_source")
    result = await _submit(hass, result, {"source_mode": "broadcast"})
    port.drop()
    await settle(hass)
    amp.clear()

    result = await _submit(
        hass, result, {"source_device": PORT_MAC, "input_type": "Line In"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "zone_members_offline"}
    assert amp.published_actions("set_groups") == []


async def test_a_speaker_outside_the_zone_is_not_a_broadcast_source(
    hass: HomeAssistant, three_speakers: MockConfigEntry, third: FakeDevice
) -> None:
    """The input list is built from the zone's members, not from every
    speaker the integration knows about."""
    result = await _on_zone(hass, three_speakers, "set_zone_source")
    result = await _submit(hass, result, {"source_mode": "broadcast"})
    values = {option["value"] for option in _options(result, "source_device")}
    assert values == {AMP_MAC, PORT_MAC}


async def test_adding_a_member_aborts_on_a_zone_that_has_gone(
    hass: HomeAssistant, three_speakers: MockConfigEntry
) -> None:
    """The dialog is open on a zone the Play app has just deleted."""
    result = await _on_zone(hass, three_speakers, "add_zone_member")
    device_id = _device_id(hass, three_speakers, THIRD_MAC)
    entry_coordinator(hass, three_speakers).groups.clear()

    result = await _submit(hass, result, {"device_id": device_id})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "zone_gone"
