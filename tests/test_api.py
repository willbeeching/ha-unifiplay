"""The Apollo REST client.

Console mode is the path where a wrong answer is most expensive: this poll is
the only thing that says which speakers exist, so a response read as "no
devices" removes every entity in the integration. Most of what follows is
about refusing to read a failure as an empty list.
"""

from __future__ import annotations

import asyncio

import aiohttp
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.unifi_play.api import (
    REQUEST_TIMEOUT,
    UnifiPlayApi,
    UnifiPlayApiError,
    UnifiPlayAuthError,
    UnifiPlayConnectionError,
    UnifiPlayForbiddenError,
    UnifiPlayServiceUnavailableError,
    UnifiPlayTransientError,
    UnifiPlayUnsupportedApiError,
    _normalize_host,
    _normalize_mac,
)

from .conftest import ApolloServer
from .const import API_KEY, CONSOLE_HOST, fixture


@pytest.fixture
def api(hass: HomeAssistant, apollo: ApolloServer) -> UnifiPlayApi:
    """A client on Home Assistant's shared session, as production builds it."""
    return UnifiPlayApi(
        CONSOLE_HOST, API_KEY, async_get_clientsession(hass, verify_ssl=False)
    )


# ── Host and MAC normalisation ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("192.168.1.1", "192.168.1.1"),
        ("  192.168.1.1  ", "192.168.1.1"),
        ("https://192.168.1.1", "192.168.1.1"),
        ("http://udm.local", "udm.local"),
        ("HTTPS://UDM.local/", "UDM.local"),
        ("192.168.1.1/network/default", "192.168.1.1"),
    ],
)
def test_host_normalisation(raw: str, expected: str) -> None:
    """Users paste what their browser shows them."""
    assert _normalize_host(raw) == expected


@pytest.mark.parametrize(
    "raw", ["AA:BB:CC:DD:EE:FF", "aa-bb-cc-dd-ee-ff", "AABBCCDDEEFF"]
)
def test_mac_normalisation(raw: str) -> None:
    assert _normalize_mac(raw) == "aabbccddeeff"


# ── The happy path ────────────────────────────────────────────────────────


async def test_devices_are_returned(api: UnifiPlayApi, apollo: ApolloServer) -> None:
    apollo.devices()
    devices = await api.get_devices()
    assert [d["platform"] for d in devices] == ["UPL-AMP", "UPL-PORT"]
    assert devices[0]["ip"] == "192.168.1.100"


async def test_no_devices_is_not_a_failure(
    api: UnifiPlayApi, apollo: ApolloServer
) -> None:
    """Apollo answers ``data: null`` when it has nothing.

    The console is reachable and the key is good; the user simply has not
    adopted any hardware yet. Setup has to succeed so they can adopt it and
    have the devices appear on the next poll.
    """
    apollo.devices({"err": None, "data": None})
    assert await api.get_devices() == []


async def test_groups_null_is_empty(api: UnifiPlayApi, apollo: ApolloServer) -> None:
    apollo.groups()
    assert await api.get_groups() == []


async def test_groups_are_returned(api: UnifiPlayApi, apollo: ApolloServer) -> None:
    apollo.groups({"err": None, "data": [{"group_id": "a"}]})
    assert await api.get_groups() == [{"group_id": "a"}]


async def test_ip_is_enriched_from_the_network_api(
    api: UnifiPlayApi, apollo: ApolloServer
) -> None:
    """Recent firmware stopped populating ``ip`` in the Apollo response.

    Without an IP there is no MQTT connection and therefore no state at all,
    so the address is fetched from the Network application, where the speaker
    appears as an ordinary client.
    """
    apollo.devices_without_ip()
    apollo.network_clients()
    devices = await api.get_devices()
    assert devices[0]["ip"] == "192.168.1.101"


async def test_enrichment_falls_back_to_last_ip(
    api: UnifiPlayApi, apollo: ApolloServer
) -> None:
    """A client whose lease has expired still carries ``last_ip``."""
    apollo.devices(
        {
            "err": None,
            "data": [{"id": "x", "mac": "AABBCCDDEE22", "platform": "UPL-PORT"}],
        }
    )
    apollo.network_clients()
    devices = await api.get_devices()
    assert devices[0]["ip"] == "192.168.1.102"


async def test_enrichment_failure_is_not_fatal(
    api: UnifiPlayApi, apollo: ApolloServer, aioclient_mock
) -> None:
    """A console with no Network application still has usable Play devices.

    IP enrichment is best-effort; losing it costs the MQTT connection for one
    speaker, not the whole integration.
    """
    apollo.devices_without_ip()
    aioclient_mock.get(apollo.clients_url, status=404, text="not found")
    devices = await api.get_devices()
    assert devices[0].get("ip") in (None, "")


async def test_enrichment_ignores_a_non_json_network_answer(
    api: UnifiPlayApi, apollo: ApolloServer, aioclient_mock
) -> None:
    """A console without the Network app serves its HTML shell with a 200."""
    apollo.devices_without_ip()
    aioclient_mock.get(
        apollo.clients_url,
        status=200,
        text="<!doctype html>",
        headers={"Content-Type": "text/html"},
    )
    devices = await api.get_devices()
    assert devices[0].get("ip") in (None, "")


# ── Failure shapes ────────────────────────────────────────────────────────


async def test_html_means_no_apollo_application(
    api: UnifiPlayApi, apollo: ApolloServer
) -> None:
    """UniFi OS answers 200 with its single-page-app shell.

    The status says success, so HTML - not the code - is the only signal
    that the application is absent, and it has to be checked first.
    """
    apollo.no_apollo_application()
    with pytest.raises(UnifiPlayServiceUnavailableError):
        await api.get_devices()


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, UnifiPlayAuthError),
        (403, UnifiPlayForbiddenError),
        (404, UnifiPlayUnsupportedApiError),
        (429, UnifiPlayTransientError),
        (500, UnifiPlayTransientError),
        (502, UnifiPlayTransientError),
        (503, UnifiPlayTransientError),
    ],
)
async def test_status_codes_map_to_their_own_errors(
    api: UnifiPlayApi, apollo: ApolloServer, status: int, expected: type[Exception]
) -> None:
    apollo.status(status, text="nope")
    with pytest.raises(expected):
        await api.get_devices()


async def test_a_500_with_a_json_body_is_still_transient(
    api: UnifiPlayApi, apollo: ApolloServer
) -> None:
    """The body must not be parsed before the status is judged.

    A JSON error page read as a payload is how a proxy fault turns into "the
    console has no devices".
    """
    apollo.status(500, json={"err": {"msg": "upstream down"}, "data": None})
    with pytest.raises(UnifiPlayTransientError):
        await api.get_devices()


async def test_an_unanticipated_status_still_fails(
    api: UnifiPlayApi, apollo: ApolloServer
) -> None:
    """Every non-2xx raises, including ones nobody has written a case for."""
    apollo.status(418, text="teapot")
    with pytest.raises(UnifiPlayApiError):
        await api.get_devices()


async def test_malformed_json(api: UnifiPlayApi, apollo: ApolloServer) -> None:
    """Declared application/json and was not."""
    apollo.malformed_json()
    with pytest.raises(UnifiPlayApiError, match="malformed JSON"):
        await api.get_devices()


async def test_an_error_envelope_is_never_an_empty_device_list(
    api: UnifiPlayApi, apollo: ApolloServer
) -> None:
    """``{"err": {...}, "data": null}`` is a failure, not "no speakers".

    Reading it as an empty list would leave every known device in place but
    report the console as healthy, and the next real emptiness would be
    indistinguishable from it.
    """
    apollo.error_envelope("device service unavailable")
    with pytest.raises(UnifiPlayApiError, match="device service unavailable"):
        await api.get_devices()


async def test_an_error_envelope_that_is_not_a_dict(
    api: UnifiPlayApi, apollo: ApolloServer
) -> None:
    apollo.devices({"err": "something went wrong", "data": None})
    with pytest.raises(UnifiPlayApiError, match="something went wrong"):
        await api.get_devices()


async def test_a_payload_that_is_not_a_list(
    api: UnifiPlayApi, apollo: ApolloServer
) -> None:
    apollo.devices({"err": None, "data": {"unexpected": "shape"}})
    with pytest.raises(UnifiPlayApiError, match="expected a list"):
        await api.get_devices()


async def test_groups_payload_that_is_not_a_list(
    api: UnifiPlayApi, apollo: ApolloServer
) -> None:
    apollo.groups({"err": None, "data": 42})
    with pytest.raises(UnifiPlayApiError, match="expected a list"):
        await api.get_groups()


async def test_top_level_json_that_is_not_an_object(
    api: UnifiPlayApi, apollo: ApolloServer
) -> None:
    apollo.devices([1, 2, 3])
    with pytest.raises(UnifiPlayApiError, match="not an object"):
        await api.get_devices()


async def test_connect_timeout(api: UnifiPlayApi, aioclient_mock, apollo) -> None:
    """A console that never completes a handshake."""
    aioclient_mock.get(
        apollo.devices_url,
        exc=aiohttp.ServerTimeoutError("connect timed out"),
    )
    with pytest.raises(UnifiPlayConnectionError):
        await api.get_devices()


async def test_response_timeout(api: UnifiPlayApi, aioclient_mock, apollo) -> None:
    """A console that accepts the connection and then stops answering.

    A UniFi OS mid-upgrade does exactly this. Without the request timeout the
    coordinator refresh never returns and the integration never sets up.
    """
    aioclient_mock.get(apollo.devices_url, exc=TimeoutError())
    with pytest.raises(UnifiPlayConnectionError):
        await api.get_devices()


async def test_connection_error(api: UnifiPlayApi, apollo: ApolloServer) -> None:
    apollo.connection_error()
    with pytest.raises(UnifiPlayConnectionError):
        await api.get_devices()


def test_there_is_a_request_timeout() -> None:
    """Named so the value is reviewable, and non-None so aiohttp cannot wait forever."""
    assert REQUEST_TIMEOUT.total is not None
    assert REQUEST_TIMEOUT.connect is not None
    assert REQUEST_TIMEOUT.connect < REQUEST_TIMEOUT.total


# ── Session ownership ─────────────────────────────────────────────────────


async def test_the_client_uses_the_session_it_was_given(
    hass: HomeAssistant, apollo: ApolloServer
) -> None:
    """No session is created here, so none can be leaked.

    Sessions this integration built were sessions it had to remember to
    close on every path out, including the ones that raise.
    """
    session = async_get_clientsession(hass, verify_ssl=False)
    api = UnifiPlayApi(CONSOLE_HOST, API_KEY, session)
    apollo.devices()
    await api.get_devices()
    assert not session.closed
    assert not hasattr(api, "close")


async def test_no_unclosed_session_warning(
    hass: HomeAssistant, apollo: ApolloServer, api: UnifiPlayApi
) -> None:
    """``filterwarnings = error`` turns a leaked session into a failure.

    aiohttp emits ResourceWarning for a session garbage-collected while open;
    this test exists so that path is actually executed.
    """
    apollo.devices()
    await api.get_devices()
    await asyncio.sleep(0)


# ── Logging ───────────────────────────────────────────────────────────────


async def test_the_api_key_is_never_logged(
    api: UnifiPlayApi, apollo: ApolloServer, caplog
) -> None:
    """Not in the request line, not in an error path, not at debug level."""
    import logging

    caplog.set_level(logging.DEBUG)
    apollo.devices()
    await api.get_devices()
    assert API_KEY not in caplog.text
    assert "X-API-KEY" not in caplog.text


@pytest.mark.parametrize("status", [401, 403])
async def test_a_credential_response_body_is_not_logged(
    api: UnifiPlayApi, apollo: ApolloServer, caplog, status: int
) -> None:
    """Some products echo the presented key back in a 401 body.

    The status and URL are enough to diagnose a rejected key; the body is
    not, and cannot be shown to be safe.
    """
    import logging

    caplog.set_level(logging.DEBUG)
    apollo.status(status, text=f"rejected key {API_KEY}")
    with pytest.raises(UnifiPlayApiError):
        await api.get_devices()
    assert API_KEY not in caplog.text
    assert "body withheld" in caplog.text


async def test_a_404_body_is_logged_because_it_diagnoses(
    api: UnifiPlayApi, apollo: ApolloServer, caplog
) -> None:
    """Apollo's own plain-text 404 differs from a proxy's, and that matters."""
    import logging

    caplog.set_level(logging.DEBUG)
    apollo.status(404, text="apollo: no route for /devices")
    with pytest.raises(UnifiPlayUnsupportedApiError):
        await api.get_devices()
    assert "no route for /devices" in caplog.text


# ── validate_connection ───────────────────────────────────────────────────


async def test_validate_connection_returns_devices(
    api: UnifiPlayApi, apollo: ApolloServer
) -> None:
    apollo.devices()
    assert len(await api.validate_connection()) == 2


async def test_validate_connection_warns_but_succeeds_with_no_devices(
    api: UnifiPlayApi, apollo: ApolloServer, caplog
) -> None:
    apollo.devices({"err": None, "data": []})
    assert await api.validate_connection() == []
    assert "Adopt your Play hardware" in caplog.text


async def test_validate_connection_re_raises(
    api: UnifiPlayApi, apollo: ApolloServer
) -> None:
    """Failures are re-raised rather than flattened into a boolean.

    Each cause has its own actionable message in the config flow, and a
    boolean loses every one of them.
    """
    apollo.status(401, text="")
    with pytest.raises(UnifiPlayAuthError):
        await api.validate_connection()


async def test_validate_connection_logs_the_host_not_the_key(
    api: UnifiPlayApi, apollo: ApolloServer, caplog
) -> None:
    apollo.status(500, text="")
    with pytest.raises(UnifiPlayTransientError):
        await api.validate_connection()
    assert CONSOLE_HOST in caplog.text
    assert API_KEY not in caplog.text


def test_devices_fixture_matches_the_documented_envelope() -> None:
    """Guard the fixture against drifting away from docs/api.md."""
    payload = fixture("apollo_devices.json")
    assert set(payload) >= {"err", "type", "data", "offset", "limit", "total"}
    assert payload["type"] == "collection"
