"""The setup, reauth and duplicate-prevention paths of the config flow.

Zone management lives in the options flow and is covered in the zone tests;
this file is about getting an entry created and keeping its credential valid.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_play.const import (
    CONF_API_KEY,
    CONF_CONTROLLER_HOST,
    CONF_MANUAL_HOSTS,
    CONF_MODE,
    DOMAIN,
    MODE_CONSOLE,
    MODE_DIRECT,
)

from .conftest import ApolloServer
from .const import API_KEY, CONSOLE_HOST, amp_device
from .fake_mqtt import FakeDevice


async def _start(hass: HomeAssistant) -> dict[str, Any]:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def _console(hass: HomeAssistant, **overrides: Any) -> dict[str, Any]:
    """Walk the menu into the console step and submit it."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": MODE_CONSOLE}
    )
    data = {CONF_CONTROLLER_HOST: CONSOLE_HOST, CONF_API_KEY: API_KEY, **overrides}
    return await hass.config_entries.flow.async_configure(result["flow_id"], data)


# ── The menu ──────────────────────────────────────────────────────────────


async def test_the_first_step_offers_both_modes(hass: HomeAssistant) -> None:
    """Direct mode exists because some consoles never receive Apollo."""
    result = await _start(hass)
    assert result["type"] is FlowResultType.MENU
    assert set(result["menu_options"]) == {MODE_CONSOLE, MODE_DIRECT}


# ── Console setup ─────────────────────────────────────────────────────────


async def test_console_setup_creates_an_entry(
    hass: HomeAssistant, apollo: ApolloServer, amp: FakeDevice, port: FakeDevice
) -> None:
    apollo.devices()
    result = await _console(hass)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"UniFi Play ({CONSOLE_HOST})"
    assert result["data"] == {
        CONF_MODE: MODE_CONSOLE,
        CONF_CONTROLLER_HOST: CONSOLE_HOST,
        CONF_API_KEY: API_KEY,
    }


async def test_console_setup_with_no_hardware_yet(
    hass: HomeAssistant, apollo: ApolloServer
) -> None:
    """The console answered, so the host and key are good.

    Refusing here would mean nobody could add the integration before adopting
    their speakers, and adopting them is done in the UniFi app, not here.
    """
    apollo.devices({"err": None, "data": []})
    result = await _console(hass)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_the_host_is_normalised_before_it_is_stored(
    hass: HomeAssistant, apollo: ApolloServer
) -> None:
    """Users paste what the browser shows them, scheme and all."""
    apollo.devices({"err": None, "data": []})
    result = await _console(hass, **{CONF_CONTROLLER_HOST: f"https://{CONSOLE_HOST}/"})
    assert result["data"][CONF_CONTROLLER_HOST] == CONSOLE_HOST


@pytest.mark.parametrize(
    ("shape", "expected_error"),
    [
        ("auth", "invalid_auth"),
        ("forbidden", "forbidden"),
        ("no_apollo", "apollo_unavailable"),
        ("unsupported", "unsupported_api"),
        ("busy", "console_busy"),
        ("unreachable", "cannot_connect"),
        ("malformed", "cannot_connect"),
    ],
)
async def test_each_failure_gets_its_own_message(
    hass: HomeAssistant,
    apollo: ApolloServer,
    shape: str,
    expected_error: str,
) -> None:
    """Each cause points at a different fix, so none may be collapsed.

    "Could not connect" for a revoked API key sends the user to look at their
    network instead of their credential.
    """
    if shape == "auth":
        apollo.status(401, text="")
    elif shape == "forbidden":
        apollo.status(403, text="")
    elif shape == "no_apollo":
        apollo.no_apollo_application()
    elif shape == "unsupported":
        apollo.status(404, text="")
    elif shape == "busy":
        apollo.status(503, text="")
    elif shape == "unreachable":
        apollo.connection_error()
    else:
        apollo.malformed_json()

    result = await _console(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected_error}


async def test_an_unexpected_exception_is_reported_not_swallowed(
    hass: HomeAssistant, apollo: ApolloServer
) -> None:
    with patch(
        "custom_components.unifi_play.config_flow.UnifiPlayApi.validate_connection",
        side_effect=RuntimeError("boom"),
    ):
        result = await _console(hass)
    assert result["errors"] == {"base": "unknown"}


async def test_the_cloud_api_is_refused_with_its_own_message(
    hass: HomeAssistant,
) -> None:
    """api.ui.com answers a JSON 404 at the Apollo path.

    Without this check that reads as "Apollo answered but has no device API",
    which sends the user looking for a firmware problem that is not there.
    """
    result = await _console(hass, **{CONF_CONTROLLER_HOST: "api.ui.com"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cloud_host"}


async def test_the_flow_can_be_retried_after_a_failure(
    hass: HomeAssistant, apollo: ApolloServer, aioclient_mock
) -> None:
    """A wrong key, then the right one, in one flow."""
    apollo.status(401, text="")
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": MODE_CONSOLE}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_CONTROLLER_HOST: CONSOLE_HOST, CONF_API_KEY: "wrong"},
    )
    assert result["errors"] == {"base": "invalid_auth"}

    aioclient_mock.clear_requests()
    apollo.devices({"err": None, "data": []})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_CONTROLLER_HOST: CONSOLE_HOST, CONF_API_KEY: API_KEY},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_the_same_console_cannot_be_added_twice(
    hass: HomeAssistant, apollo: ApolloServer, console_entry: MockConfigEntry
) -> None:
    console_entry.add_to_hass(hass)
    apollo.devices({"err": None, "data": []})
    result = await _console(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_a_second_entry_covering_the_same_speakers_is_refused(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    apollo: ApolloServer,
) -> None:
    """Unique IDs are MAC-based and not namespaced per entry.

    A second entry reaching the same speaker mints a full set of colliding
    IDs; Home Assistant rejects the later ones, which entry loses is a
    startup race, and the rejected entities keep their registry rows and read
    unavailable forever.
    """
    apollo.devices()
    result = await _console(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured_device"
    assert result["description_placeholders"]["entry"] == "UniFi Play (Direct)"


# ── Direct setup ──────────────────────────────────────────────────────────


async def test_direct_setup_creates_an_entry(
    hass: HomeAssistant, udp_discovery, amp: FakeDevice, port: FakeDevice
) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": MODE_DIRECT}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MANUAL_HOSTS: ""}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MODE] == MODE_DIRECT


async def test_direct_setup_parses_a_typed_host_list(
    hass: HomeAssistant, udp_discovery, amp: FakeDevice
) -> None:
    """Commas or spaces, because people type both."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": MODE_DIRECT}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MANUAL_HOSTS: "10.0.0.5, 10.0.0.6 10.0.0.7"}
    )
    assert result["data"][CONF_MANUAL_HOSTS] == ["10.0.0.5", "10.0.0.6", "10.0.0.7"]


@pytest.mark.parametrize(
    ("hosts", "expected"),
    [("", "no_devices_found"), ("10.0.0.5", "no_response")],
)
async def test_nothing_answered(
    hass: HomeAssistant, udp_discovery, mqtt_network, hosts: str, expected: str
) -> None:
    """ "Your typed IPs are wrong" and "nothing on this subnet" need different advice."""
    udp_discovery.clear()
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": MODE_DIRECT}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MANUAL_HOSTS: hosts}
    )
    assert result["errors"] == {"base": expected}


async def test_a_discovery_socket_failure(hass: HomeAssistant) -> None:
    with patch(
        "custom_components.unifi_play.config_flow.async_resolve_direct",
        side_effect=OSError("no socket"),
    ):
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": MODE_DIRECT}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_MANUAL_HOSTS: ""}
        )
    assert result["errors"] == {"base": "discovery_failed"}


async def test_direct_mode_can_only_be_configured_once(
    hass: HomeAssistant, direct_entry: MockConfigEntry, udp_discovery
) -> None:
    direct_entry.add_to_hass(hass)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": MODE_DIRECT}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MANUAL_HOSTS: ""}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# ── Reauth ────────────────────────────────────────────────────────────────


async def test_a_revoked_key_starts_a_reauth_flow(
    hass: HomeAssistant,
    console_entry: MockConfigEntry,
    apollo: ApolloServer,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    """The coordinator turns a 401 into ConfigEntryAuthFailed.

    Retrying a dead key every five minutes forever, saying nothing, is the
    alternative - and the only action that fixes it is entering a new one.
    """
    apollo.devices()
    console_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(console_entry.entry_id)
    await settle(hass)

    apollo._mocker.clear_requests()
    apollo.status(401, text="")
    await hass.config_entries.async_reload(console_entry.entry_id)
    await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_reauth_accepts_a_new_key(
    hass: HomeAssistant, console_entry: MockConfigEntry, apollo: ApolloServer
) -> None:
    console_entry.add_to_hass(hass)
    result = await console_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"]["host"] == CONSOLE_HOST

    apollo.devices({"err": None, "data": []})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: "a-replacement-key"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert console_entry.data[CONF_API_KEY] == "a-replacement-key"
    # The host is not asked for again: a different host is a different
    # console and therefore a different entry.
    assert console_entry.data[CONF_CONTROLLER_HOST] == CONSOLE_HOST


async def test_reauth_rejects_a_key_that_is_also_wrong(
    hass: HomeAssistant, console_entry: MockConfigEntry, apollo: ApolloServer
) -> None:
    console_entry.add_to_hass(hass)
    result = await console_entry.start_reauth_flow(hass)

    apollo.status(401, text="")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: "still-wrong"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert console_entry.data[CONF_API_KEY] == API_KEY


async def test_reauth_reports_a_console_outage_as_transient(
    hass: HomeAssistant, console_entry: MockConfigEntry, apollo: ApolloServer
) -> None:
    """A console that is down is not a wrong key, and must not read as one."""
    console_entry.add_to_hass(hass)
    result = await console_entry.start_reauth_flow(hass)

    apollo.status(503, text="")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: "probably-fine"}
    )
    assert result["errors"] == {"base": "console_busy"}


async def test_an_abandoned_reauth_leaves_the_entry_alone(
    hass: HomeAssistant, console_entry: MockConfigEntry
) -> None:
    """Cancelling is a normal outcome; the old key stays until replaced."""
    console_entry.add_to_hass(hass)
    result = await console_entry.start_reauth_flow(hass)

    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done()

    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []
    assert console_entry.data[CONF_API_KEY] == API_KEY


# ── Legacy entries ────────────────────────────────────────────────────────


async def test_an_entry_from_before_modes_existed_is_console_mode(
    hass: HomeAssistant,
    legacy_entry: MockConfigEntry,
    apollo: ApolloServer,
    amp: FakeDevice,
    port: FakeDevice,
    settle,
) -> None:
    """Every pre-1.2 install would otherwise break on upgrade."""
    apollo.devices()
    legacy_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(legacy_entry.entry_id)
    await settle(hass)
    assert legacy_entry.state is ConfigEntryState.LOADED
    assert amp.connect_attempts == 1


# ── Transient failures keep state ─────────────────────────────────────────


async def test_a_console_outage_does_not_empty_the_integration(
    hass: HomeAssistant,
    setup_console: MockConfigEntry,
    apollo: ApolloServer,
    settle,
) -> None:
    """MQTT is the source of truth; this poll only discovers devices.

    Dropping known speakers on a console hiccup would take every entity with
    them, which looks to the user like the hardware disappearing.
    """
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from custom_components.unifi_play.coordinator import DISCOVERY_INTERVAL

    from .conftest import entry_coordinator

    coordinator = entry_coordinator(hass, setup_console)
    assert len(coordinator.data) == 2

    apollo._mocker.clear_requests()
    apollo.status(503, text="")
    async_fire_time_changed(hass, dt_util.utcnow() + DISCOVERY_INTERVAL)
    await settle(hass)

    assert len(coordinator.data) == 2
    assert hass.states.get("media_player.living_room") is not None


def test_discovered_devices_fixture_shape() -> None:
    """The direct and console paths must produce the same device shape."""
    device = amp_device()
    assert set(device) == {"id", "name", "mac", "platform", "firmware", "ip"}
