"""Diagnostics, and above all what they must never contain.

A diagnostics file is pasted into a public issue tracker. Everything here is
either about the report being useful or about it being safe to paste.
"""

from __future__ import annotations

import json

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_play.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import ApolloServer
from .const import (
    AMP_IP,
    AMP_MAC,
    AMP_NAME,
    API_KEY,
    CONSOLE_HOST,
    PORT_IP,
    PORT_MAC,
    PORT_NAME,
    ZONE_ID,
    ZONE_NAME,
    fixture,
    groups_body,
)
from .fake_mqtt import FakeDevice


def _flat(payload: object) -> str:
    """The whole report as one string, for "does this appear anywhere" checks."""
    return json.dumps(payload)


# ── Redaction ─────────────────────────────────────────────────────────────


async def test_the_api_key_is_never_in_a_report(
    hass: HomeAssistant,
    setup_console: MockConfigEntry,
    apollo: ApolloServer,
) -> None:
    report = await async_get_config_entry_diagnostics(hass, setup_console)
    assert API_KEY not in _flat(report)
    assert report["entry"]["data"]["api_key"] == "**REDACTED**"


async def test_the_console_address_is_redacted(
    hass: HomeAssistant, setup_console: MockConfigEntry
) -> None:
    report = await async_get_config_entry_diagnostics(hass, setup_console)
    assert CONSOLE_HOST not in _flat(report)


async def test_no_speaker_address_or_mac_appears(
    hass: HomeAssistant, synced_zone: MockConfigEntry
) -> None:
    report = _flat(await async_get_config_entry_diagnostics(hass, synced_zone))
    for identifier in (AMP_MAC, PORT_MAC, AMP_IP, PORT_IP):
        assert identifier not in report
        assert identifier.lower() not in report.lower()


async def test_no_name_the_user_chose_appears(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    settle,
) -> None:
    """Speaker and zone names are as identifying as an address.

    "Ellie's Bedroom" in a public issue is a worse leak than an RFC 1918
    address, so the report says whether a name is set, not what it is.
    """
    amp.emit("info", {"deviceName": "Ellie's Bedroom", "space": "Home"})
    await settle(hass)

    report = await async_get_config_entry_diagnostics(hass, synced_zone)
    flat = _flat(report)
    assert "Ellie" not in flat
    assert AMP_NAME not in flat
    assert PORT_NAME not in flat
    assert ZONE_NAME not in flat
    assert report["devices"][0]["has_name"] is True
    assert report["zones"][0]["has_name"] is True


async def test_nothing_that_is_playing_appears(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    """Which service is a capability report; a track title is someone's evening."""
    amp.emit("metadata", fixture("mqtt_metadata.json"))
    amp.emit("info", {"service": "spotify"})
    await settle(hass)

    report = _flat(await async_get_config_entry_diagnostics(hass, setup_direct))
    assert "Weightless" not in report
    assert "Marconi Union" not in report
    assert "Focus" not in report
    assert "spotify" in report


async def test_no_certificate_material_appears(
    hass: HomeAssistant, setup_direct: MockConfigEntry
) -> None:
    """Which generations are bundled, never a byte of them."""
    report = await async_get_config_entry_diagnostics(hass, setup_direct)
    flat = _flat(report)
    assert "BEGIN CERTIFICATE" not in flat
    assert "PRIVATE KEY" not in flat
    assert ".crt" not in flat
    assert ".key" not in flat
    assert report["certificates"]["bundled"] == ["2026", "2023"]


async def test_manual_hosts_are_redacted(
    hass: HomeAssistant,
    direct_entry: MockConfigEntry,
    udp_discovery,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    direct_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        direct_entry, data={**direct_entry.data, "manual_hosts": ["10.0.0.5"]}
    )
    assert await hass.config_entries.async_setup(direct_entry.entry_id)
    await settle(hass)

    report = await async_get_config_entry_diagnostics(hass, direct_entry)
    assert "10.0.0.5" not in _flat(report)
    assert report["coordinator"]["manual_host_count"] == 1


async def test_anonymisation_is_stable_within_one_report(
    hass: HomeAssistant, synced_zone: MockConfigEntry
) -> None:
    """A report has to be able to say "these are the same speaker".

    Otherwise a zone's member list cannot be matched against the speaker
    list, and half the point of the report is gone.
    """
    report = await async_get_config_entry_diagnostics(hass, synced_zone)
    device_macs = {device["mac"] for device in report["devices"]}
    zone_members = set(report["zones"][0]["members"])
    assert zone_members <= device_macs
    assert report["zones"][0]["host"] in device_macs


# ── Usefulness ────────────────────────────────────────────────────────────


async def test_a_report_says_what_the_speakers_are(
    hass: HomeAssistant, setup_direct: MockConfigEntry
) -> None:
    """Half the protocol differences in this integration are per model."""
    report = await async_get_config_entry_diagnostics(hass, setup_direct)
    platforms = {device["platform"] for device in report["devices"]}
    assert platforms == {"UPL-AMP", "UPL-PORT"}
    firmwares = {device["firmware"] for device in report["devices"]}
    assert firmwares == {"1.0.38", "1.1.10"}


async def test_a_report_says_why_a_speaker_is_offline(
    hass: HomeAssistant,
    direct_entry: MockConfigEntry,
    udp_discovery,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    """The certificate rotation in 1.0.41 looked exactly like a dead speaker.

    The machine key is reported, not the sentence: it is what the code
    branches on, and it does not move with a translation.
    """
    port.accepts = frozenset()
    direct_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(direct_entry.entry_id)
    await settle(hass)

    report = await async_get_config_entry_diagnostics(hass, direct_entry)
    reasons = {device["mqtt"]["offline_reason"] for device in report["devices"]}
    assert "certificate_rejected" in reasons


async def test_a_report_says_whether_the_speakers_agree_about_a_zone(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    settle,
) -> None:
    """A zone the speakers disagree about behaves like one that reverts.

    Nothing in the UI says so, which is why the count is here.
    """
    report = await async_get_config_entry_diagnostics(hass, synced_zone)
    assert set(report["zone_copies_reported"].values()) == {1}

    amp.emit("groups", groups_body(name="Ground Floor"))
    await settle(hass)

    report = await async_get_config_entry_diagnostics(hass, synced_zone)
    assert set(report["zone_copies_reported"].values()) == {2}


async def test_a_report_says_whether_a_zone_can_be_written_to(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    port: FakeDevice,
    settle,
) -> None:
    """Answers "why did my edit refuse" without another round trip."""
    report = await async_get_config_entry_diagnostics(hass, synced_zone)
    assert report["zones"][0]["required_speakers_connected"] is True

    port.drop()
    await settle(hass)

    report = await async_get_config_entry_diagnostics(hass, synced_zone)
    assert report["zones"][0]["required_speakers_connected"] is False


async def test_a_report_says_whether_a_host_has_been_elected(
    hass: HomeAssistant, synced_zone: MockConfigEntry
) -> None:
    """An unelected host is the cause of every host-routed action failing on
    a freshly written zone."""
    report = await async_get_config_entry_diagnostics(hass, synced_zone)
    assert report["zones"][0]["host_elected"] is True
    assert report["zones"][0]["member_count"] == 2


async def test_a_report_carries_the_coordinator_summary(
    hass: HomeAssistant, synced_zone: MockConfigEntry
) -> None:
    report = await async_get_config_entry_diagnostics(hass, synced_zone)
    summary = report["coordinator"]
    assert summary["device_count"] == 2
    assert summary["zone_count"] == 1
    assert summary["zones_fully_synced"] is True
    assert summary["uses_console_api"] is False
    assert report["entry"]["mode"] == "direct"


async def test_a_report_is_json_serialisable(
    hass: HomeAssistant, synced_zone: MockConfigEntry
) -> None:
    """Home Assistant writes it to a file; a set or a datetime in there is a
    500 on the download and no report at all."""
    report = await async_get_config_entry_diagnostics(hass, synced_zone)
    assert json.loads(json.dumps(report)) == report


async def test_the_zone_id_is_anonymised_too(
    hass: HomeAssistant, synced_zone: MockConfigEntry
) -> None:
    """It is a UUID, but it appears in entity IDs and in event payloads the
    user may have pasted elsewhere."""
    report = _flat(await async_get_config_entry_diagnostics(hass, synced_zone))
    assert ZONE_ID not in report
