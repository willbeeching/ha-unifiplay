"""Setup, unload, reload, reconfigure, and what survives each of them.

Registry rows are the user's configuration: entity IDs in dashboards, device
IDs in automations, months of history. Anything that removes one has to be
right, and a reload is the moment when the integration knows least.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.unifi_play.const import (
    CONF_API_KEY,
    CONF_CONTROLLER_HOST,
    CONF_MANUAL_HOSTS,
    DOMAIN,
)
from custom_components.unifi_play.coordinator import (
    DISCOVERY_INTERVAL,
    STALE_AFTER_ABSENCES,
)

from .conftest import ApolloServer, entry_coordinator
from .const import (
    AMP_ID,
    AMP_MAC,
    API_KEY,
    CONSOLE_HOST,
    PORT_ID,
    PORT_MAC,
    THIRD_IP,
    THIRD_MAC,
    ZONE_ID,
    device_dict,
    empty_groups_body,
    groups_body,
    port_device,
    third_device,
)
from .fake_mqtt import FakeDevice, FakeMqttNetwork

ZONE_ENTITY = "media_player.downstairs"


def _devices(hass: HomeAssistant, entry: MockConfigEntry):
    return dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)


def _device_with(hass: HomeAssistant, entry: MockConfigEntry, identifier: str):
    for device in _devices(hass, entry):
        if (DOMAIN, identifier) in device.identifiers:
            return device
    return None


# ── runtime_data ──────────────────────────────────────────────────────────


async def test_the_coordinator_lives_on_the_entry(
    hass: HomeAssistant, setup_direct: MockConfigEntry
) -> None:
    """Typed runtime_data rather than a dict keyed by entry id.

    hass.data had no type behind it, and every reader wrote its own
    annotation and hoped.
    """
    coordinator = setup_direct.runtime_data
    assert coordinator is entry_coordinator(hass, setup_direct)
    assert coordinator.config_entry is setup_direct
    assert DOMAIN not in hass.data or setup_direct.entry_id not in hass.data.get(
        DOMAIN, {}
    )


async def test_two_entries_do_not_interfere(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    console_entry: MockConfigEntry,
    apollo: ApolloServer,
    settle,
) -> None:
    """A second entry has its own coordinator and its own devices.

    The two are only prevented from covering the *same* hardware; a console
    entry alongside a direct one for different speakers is legitimate.
    """
    apollo.devices(
        {
            "err": None,
            "data": [
                device_dict(
                    device_id=THIRD_MAC,
                    mac=THIRD_MAC,
                    ip=THIRD_IP,
                    name="Study",
                    platform="UPL-PORT",
                )
            ],
        }
    )
    console_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(console_entry.entry_id)
    await settle(hass)

    assert setup_direct.runtime_data is not console_entry.runtime_data
    assert set(setup_direct.runtime_data.data) == {AMP_ID, PORT_ID}
    assert set(console_entry.runtime_data.data) == {THIRD_MAC}


async def test_unloading_one_entry_leaves_the_other_working(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    console_entry: MockConfigEntry,
    apollo: ApolloServer,
    settle,
) -> None:
    apollo.devices({"err": None, "data": []})
    console_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(console_entry.entry_id)
    await settle(hass)

    assert await hass.config_entries.async_unload(console_entry.entry_id)
    await hass.async_block_till_done()

    assert setup_direct.state is ConfigEntryState.LOADED
    assert hass.states.get("media_player.living_room") is not None
    # Actions are registered for the run, not per entry.
    assert hass.services.has_service(DOMAIN, "create_zone")


# ── Setup failure mapping ─────────────────────────────────────────────────


async def test_a_console_outage_is_a_retry_not_a_failure(
    hass: HomeAssistant, console_entry: MockConfigEntry, apollo: ApolloServer
) -> None:
    apollo.status(503, text="")
    console_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(console_entry.entry_id)
    assert console_entry.state is ConfigEntryState.SETUP_RETRY


async def test_a_revoked_key_asks_for_a_new_one(
    hass: HomeAssistant, console_entry: MockConfigEntry, apollo: ApolloServer
) -> None:
    """Not SETUP_RETRY: retrying a dead credential never succeeds."""
    apollo.status(401, text="")
    console_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(console_entry.entry_id)
    assert console_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_a_discovery_socket_failure_is_a_retry(
    hass: HomeAssistant, direct_entry: MockConfigEntry
) -> None:
    with patch(
        "custom_components.unifi_play.coordinator.async_resolve_direct",
        side_effect=OSError("no socket"),
    ):
        direct_entry.add_to_hass(hass)
        assert not await hass.config_entries.async_setup(direct_entry.entry_id)
    assert direct_entry.state is ConfigEntryState.SETUP_RETRY


# ── Reload ────────────────────────────────────────────────────────────────


async def test_a_reload_keeps_every_entity_registration(
    hass: HomeAssistant, synced_zone: MockConfigEntry, settle
) -> None:
    """Including the zone entities, which is where this used to go wrong.

    The canonical zone view starts empty after a reload and fills as each
    speaker connects. Purging on the way through deleted every zone entity
    and its device, and with them the registry rows every dashboard card and
    automation points at.
    """
    registry = er.async_get(hass)
    before = {
        reg.unique_id
        for reg in er.async_entries_for_config_entry(registry, synced_zone.entry_id)
    }
    assert f"unifi_play_zone_{ZONE_ID}" in before

    assert await hass.config_entries.async_reload(synced_zone.entry_id)
    await settle(hass)

    after = {
        reg.unique_id
        for reg in er.async_entries_for_config_entry(registry, synced_zone.entry_id)
    }
    assert before <= after


async def test_a_zone_survives_a_reload_with_a_speaker_offline(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    """A speaker that has not reported yet is not evidence a zone is gone.

    Blocking the purge costs a stale zone entity until it returns; the
    alternative costs the user's configuration.
    """
    port.unreachable = True
    assert await hass.config_entries.async_reload(synced_zone.entry_id)
    await settle(hass)
    amp.emit("groups", groups_body())
    await settle(hass)

    coordinator = entry_coordinator(hass, synced_zone)
    assert coordinator.zones_fully_synced is False
    assert _device_with(hass, synced_zone, f"zone_{ZONE_ID}") is not None


async def test_a_zone_deleted_while_offline_is_cleaned_up_after_a_full_sync(
    hass: HomeAssistant,
    synced_zone: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    """Once every speaker has spoken, an absent zone really is absent."""
    assert _device_with(hass, synced_zone, f"zone_{ZONE_ID}") is not None

    amp.emit("groups", empty_groups_body())
    port.emit("groups", empty_groups_body())
    await settle(hass)

    coordinator = entry_coordinator(hass, synced_zone)
    assert coordinator.zones_fully_synced is True
    assert _device_with(hass, synced_zone, f"zone_{ZONE_ID}") is None


async def test_repeated_reloads_leave_one_client_per_speaker(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    mqtt_network: FakeMqttNetwork,
    settle,
) -> None:
    for _ in range(3):
        assert await hass.config_entries.async_reload(setup_direct.entry_id)
        await settle(hass)
    assert len(mqtt_network.live_clients()) == 2


# ── Device address changes ────────────────────────────────────────────────


async def test_a_speaker_that_moves_is_redialled(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    mqtt_network: FakeMqttNetwork,
    discovered_devices,
    amp: FakeDevice,
    settle,
) -> None:
    """The old client still believes it is connected.

    Nothing about the connection state says the address it holds is now
    somebody else's, so only the discovery answer can trigger the redial.
    """
    moved = mqtt_network.add(
        FakeDevice(
            ip="192.168.1.180", mac=AMP_MAC, platform="UPL-AMP", name="Living Room"
        )
    )
    discovered_devices[0] = device_dict(ip="192.168.1.180")

    async_fire_time_changed(hass, dt_util.utcnow() + DISCOVERY_INTERVAL)
    await settle(hass)

    assert moved.connect_attempts == 1
    coordinator = entry_coordinator(hass, setup_direct)
    assert coordinator.data[AMP_ID].ip == "192.168.1.180"


async def test_an_unchanged_address_does_not_redial(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    amp: FakeDevice,
    settle,
) -> None:
    """A reconnect per poll would restart every stream every five minutes."""
    async_fire_time_changed(hass, dt_util.utcnow() + DISCOVERY_INTERVAL)
    await settle(hass)
    assert amp.connect_attempts == 1


# ── Device removal ────────────────────────────────────────────────────────


async def test_a_speaker_still_present_cannot_be_deleted(
    hass: HomeAssistant, setup_direct: MockConfigEntry
) -> None:
    """Otherwise the delete comes straight back on the next poll, leaving a
    device with no entities."""
    from custom_components.unifi_play import async_remove_config_entry_device

    device = _device_with(hass, setup_direct, AMP_MAC)
    assert device is not None
    assert not await async_remove_config_entry_device(hass, setup_direct, device)


async def test_a_speaker_the_integration_no_longer_knows_can_be_deleted(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    discovered_devices,
    settle,
) -> None:
    from custom_components.unifi_play import async_remove_config_entry_device

    device = _device_with(hass, setup_direct, AMP_MAC)
    assert device is not None

    coordinator = entry_coordinator(hass, setup_direct)
    coordinator.data.pop(AMP_ID)

    assert await async_remove_config_entry_device(hass, setup_direct, device)


async def test_a_live_zone_cannot_be_deleted(
    hass: HomeAssistant, synced_zone: MockConfigEntry
) -> None:
    from custom_components.unifi_play import async_remove_config_entry_device

    device = _device_with(hass, synced_zone, f"zone_{ZONE_ID}")
    assert device is not None
    assert not await async_remove_config_entry_device(hass, synced_zone, device)


async def test_nothing_removes_a_speaker_on_its_own(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    discovered_devices,
    settle,
) -> None:
    """One quiet discovery pass is not evidence a speaker is gone.

    UDP silence certainly is not: Audio Port hardware never answers the probe
    at all (#5), so a sweep that misses it is the normal case.
    """
    discovered_devices.clear()
    async_fire_time_changed(hass, dt_util.utcnow() + DISCOVERY_INTERVAL)
    await settle(hass)

    assert _device_with(hass, setup_direct, AMP_MAC) is not None
    assert hass.states.get("media_player.living_room") is not None


async def test_a_speaker_the_console_stops_listing_becomes_deletable(
    hass: HomeAssistant,
    setup_console: MockConfigEntry,
    apollo: ApolloServer,
    aioclient_mock,
    settle,
) -> None:
    """A device removed from the console used to stay undeletable forever.

    One missed API response is not enough — that is a quiet poll — but
    consecutive authoritative absences open the door for a deliberate
    delete without a reload.
    """
    from custom_components.unifi_play import async_remove_config_entry_device

    device = _device_with(hass, setup_console, AMP_MAC)
    assert device is not None

    aioclient_mock.clear_requests()
    apollo.devices({"err": None, "data": [port_device()]})
    async_fire_time_changed(hass, dt_util.utcnow() + DISCOVERY_INTERVAL)
    await settle(hass)

    assert not await async_remove_config_entry_device(hass, setup_console, device)
    assert AMP_ID in entry_coordinator(hass, setup_console).data

    for _ in range(STALE_AFTER_ABSENCES - 1):
        async_fire_time_changed(hass, dt_util.utcnow() + DISCOVERY_INTERVAL)
        await settle(hass)

    assert await async_remove_config_entry_device(hass, setup_console, device)
    assert AMP_ID not in entry_coordinator(hass, setup_console).data


async def test_a_console_outage_does_not_age_a_speaker_out(
    hass: HomeAssistant,
    setup_console: MockConfigEntry,
    apollo: ApolloServer,
    aioclient_mock,
    settle,
) -> None:
    """UpdateFailed is not an authoritative absence."""
    from custom_components.unifi_play import async_remove_config_entry_device

    device = _device_with(hass, setup_console, AMP_MAC)
    assert device is not None

    aioclient_mock.clear_requests()
    apollo.connection_error()
    for _ in range(STALE_AFTER_ABSENCES + 1):
        async_fire_time_changed(hass, dt_util.utcnow() + DISCOVERY_INTERVAL)
        await settle(hass)

    assert not await async_remove_config_entry_device(hass, setup_console, device)
    assert AMP_ID in entry_coordinator(hass, setup_console).data


async def test_a_connected_direct_speaker_does_not_go_stale_on_udp_silence(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    discovered_devices,
    settle,
) -> None:
    """Audio Port never answers UDP; a live MQTT session is still present."""
    from custom_components.unifi_play import async_remove_config_entry_device

    device = _device_with(hass, setup_direct, PORT_MAC)
    assert device is not None

    discovered_devices.clear()
    for _ in range(STALE_AFTER_ABSENCES + 1):
        async_fire_time_changed(hass, dt_util.utcnow() + DISCOVERY_INTERVAL)
        await settle(hass)

    assert not await async_remove_config_entry_device(hass, setup_direct, device)


async def test_a_zone_that_is_gone_can_be_deleted(
    hass: HomeAssistant, synced_zone: MockConfigEntry
) -> None:
    from custom_components.unifi_play import async_remove_config_entry_device

    device = _device_with(hass, synced_zone, f"zone_{ZONE_ID}")
    assert device is not None
    entry_coordinator(hass, synced_zone).groups.clear()
    assert await async_remove_config_entry_device(hass, synced_zone, device)


async def test_foreign_identifiers_do_not_block_or_allow_a_delete(
    hass: HomeAssistant, setup_direct: MockConfigEntry
) -> None:
    """A registry row can carry identifiers this integration does not own."""
    from types import SimpleNamespace

    from custom_components.unifi_play import async_remove_config_entry_device

    still_ours = SimpleNamespace(identifiers={("other", "x"), (DOMAIN, AMP_MAC)})
    assert not await async_remove_config_entry_device(hass, setup_direct, still_ours)

    unrelated = SimpleNamespace(identifiers={("other", "x"), (DOMAIN, 123)})
    assert await async_remove_config_entry_device(hass, setup_direct, unrelated)


async def test_a_failed_platform_unload_leaves_the_coordinator_running(
    hass: HomeAssistant, setup_direct: MockConfigEntry, mqtt_network: FakeMqttNetwork
) -> None:
    """Shutdown is the coordinator's job only after platforms have gone."""
    from custom_components.unifi_play import async_unload_entry

    with patch(
        "homeassistant.config_entries.ConfigEntries.async_unload_platforms",
        return_value=False,
    ):
        assert not await async_unload_entry(hass, setup_direct)

    assert mqtt_network.live_clients()


# ── Reconfigure ───────────────────────────────────────────────────────────


async def test_reconfiguring_a_console_moves_the_entry(
    hass: HomeAssistant,
    console_entry: MockConfigEntry,
    apollo: ApolloServer,
    aioclient_mock,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    """A console rebuilt on a new address keeps its speakers and entities.

    Removing and re-adding does the same thing at the cost of every entity
    ID, every dashboard card and every automation.
    """
    apollo.devices()
    console_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(console_entry.entry_id)
    await settle(hass)

    aioclient_mock.clear_requests()
    moved = ApolloServer(aioclient_mock, host="192.168.1.2")
    moved.devices()

    result = await console_entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure_console"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONTROLLER_HOST: "192.168.1.2"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert console_entry.data[CONF_CONTROLLER_HOST] == "192.168.1.2"
    # The credential was left blank, so the stored one is kept.
    assert console_entry.data[CONF_API_KEY] == API_KEY


async def test_reconfiguring_a_console_reports_a_bad_address(
    hass: HomeAssistant, console_entry: MockConfigEntry, apollo: ApolloServer
) -> None:
    console_entry.add_to_hass(hass)
    result = await console_entry.start_reconfigure_flow(hass)

    apollo.connection_error()
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONTROLLER_HOST: CONSOLE_HOST}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert console_entry.data[CONF_CONTROLLER_HOST] == CONSOLE_HOST


async def test_reconfiguring_a_direct_entry_changes_its_host_list(
    hass: HomeAssistant,
    direct_entry: MockConfigEntry,
    udp_discovery,
    mqtt_network: FakeMqttNetwork,
    amp: FakeDevice,
    port: FakeDevice,
    third: FakeDevice,
    settle,
) -> None:
    direct_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(direct_entry.entry_id)
    await settle(hass)

    udp_discovery.append(third_device())
    result = await direct_entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure_direct"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MANUAL_HOSTS: f"{THIRD_IP}"}
    )
    await hass.async_block_till_done()

    assert result["reason"] == "reconfigure_successful"
    assert direct_entry.data[CONF_MANUAL_HOSTS] == [THIRD_IP]


async def test_a_direct_host_list_that_reaches_nothing_is_refused(
    hass: HomeAssistant,
    direct_entry: MockConfigEntry,
    udp_discovery,
    mqtt_network: FakeMqttNetwork,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    """Saving it would disconnect every speaker on the next reload."""
    direct_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(direct_entry.entry_id)
    await settle(hass)

    udp_discovery.clear()
    result = await direct_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MANUAL_HOSTS: "10.9.9.9"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_response"}
    assert direct_entry.data[CONF_MANUAL_HOSTS] == []


async def test_a_discovery_socket_failure_during_reconfigure(
    hass: HomeAssistant,
    direct_entry: MockConfigEntry,
    udp_discovery,
    mqtt_network: FakeMqttNetwork,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    """A socket that will not open is not an empty network.

    Reported as its own error rather than "nothing answered", which would
    send the user off checking IP addresses that are fine.
    """
    direct_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(direct_entry.entry_id)
    await settle(hass)

    result = await direct_entry.start_reconfigure_flow(hass)
    with patch(
        "custom_components.unifi_play.config_flow.async_resolve_direct",
        side_effect=OSError("no socket"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_MANUAL_HOSTS: THIRD_IP}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "discovery_failed"}
    assert direct_entry.data[CONF_MANUAL_HOSTS] == []


async def test_reconfigure_refuses_speakers_another_entry_already_has(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    console_entry: MockConfigEntry,
    apollo: ApolloServer,
) -> None:
    """Reconfigure used to discard the validated device list.

    Pointing a console entry at speakers a direct entry already owns mints
    the same MAC-based unique IDs initial setup correctly refuses.
    """
    console_entry.add_to_hass(hass)
    result = await console_entry.start_reconfigure_flow(hass)
    apollo.devices()
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONTROLLER_HOST: CONSOLE_HOST}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured_device"
    assert result["description_placeholders"]["entry"] == "UniFi Play (Direct)"


async def test_reconfigure_direct_refuses_speakers_a_console_already_has(
    hass: HomeAssistant,
    setup_console: MockConfigEntry,
    direct_entry: MockConfigEntry,
    udp_discovery,
    amp: FakeDevice,
    port: FakeDevice,
) -> None:
    direct_entry.add_to_hass(hass)
    result = await direct_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MANUAL_HOSTS: ""}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured_device"


async def test_reconfigure_refuses_a_console_another_entry_already_has(
    hass: HomeAssistant,
    console_entry: MockConfigEntry,
    apollo: ApolloServer,
    aioclient_mock,
) -> None:
    """Two entries on one console overlap on hardware.

    Every entity ID the second would mint is already taken, so it creates
    nothing at all and the failure is invisible.
    """
    other = MockConfigEntry(
        domain=DOMAIN,
        title="UniFi Play (192.168.1.2)",
        unique_id="192.168.1.2",
        data={CONF_CONTROLLER_HOST: "192.168.1.2", CONF_API_KEY: API_KEY},
    )
    other.add_to_hass(hass)
    console_entry.add_to_hass(hass)

    result = await console_entry.start_reconfigure_flow(hass)
    ApolloServer(aioclient_mock, host="192.168.1.2").devices({"err": None, "data": []})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONTROLLER_HOST: "192.168.1.2"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured_console"


# ── PARALLEL_UPDATES ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "platform",
    [
        "binary_sensor",
        "button",
        "media_player",
        "number",
        "select",
        "sensor",
        "switch",
        "text",
    ],
)
def test_every_platform_declares_parallel_updates(platform: str) -> None:
    """Home Assistant serialises entity commands unless told otherwise.

    Every command here is a fire-and-forget MQTT publish, so serialising only
    adds latency to a script that touches several speakers at once. Not
    declaring it is the thing the quality scale asks about, so it is asserted
    rather than assumed.
    """
    import importlib

    module = importlib.import_module(f"custom_components.unifi_play.{platform}")
    assert module.PARALLEL_UPDATES == 0


async def test_the_config_flow_is_registered_for_reconfigure(
    hass: HomeAssistant, console_entry: MockConfigEntry
) -> None:
    console_entry.add_to_hass(hass)
    result = await console_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure_console"
    assert (
        hass.config_entries.flow.async_progress_by_handler(DOMAIN)[0]["context"][
            "source"
        ]
        == config_entries.SOURCE_RECONFIGURE
    )
