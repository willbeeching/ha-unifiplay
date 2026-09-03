"""Shared fixtures for the UniFi Play tests.

Three seams, and no others:

``paho.mqtt.client.Client``
    Replaced by :mod:`tests.fake_mqtt`. Everything above it - Binme framing,
    certificate-generation fallback, CONNACK handling, the coordinator's
    dispatch table - runs for real.

``discovery.async_discover``
    The UDP socket. ``async_resolve_direct`` and the MQTT identification
    fallback both still run for real above it.

``aioclient_mock``
    Home Assistant's own aiohttp mocker, standing in for the console's
    Apollo API.

Nothing else is patched. A test that reaches for ``patch.object`` on an
integration method is testing the patch, and the two shipped bugs this
repository is named for (a deleted client method, a zone written to one
device) would both have survived that.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Generator, Iterable
from typing import Any
from unittest.mock import PropertyMock, patch

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.unifi_play.api import API_PATH, NETWORK_PATH
from custom_components.unifi_play.const import (
    CONF_API_KEY,
    CONF_CONTROLLER_HOST,
    CONF_MANUAL_HOSTS,
    CONF_MODE,
    DOMAIN,
    MODE_CONSOLE,
    MODE_DIRECT,
)

from .const import (
    AMP_IP,
    AMP_MAC,
    API_KEY,
    CONSOLE_HOST,
    PORT_IP,
    PORT_MAC,
    THIRD_IP,
    THIRD_MAC,
    amp_device,
    fixture,
    port_device,
)
from .fake_mqtt import FakeDevice, FakeMqttNetwork

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> None:
    """Load ``custom_components/`` for every test.

    Home Assistant hides custom integrations from its test harness unless a
    test opts in; forgetting the opt-in fails with a confusing "integration
    not found", so it is applied everywhere rather than remembered.
    """


@pytest.fixture(autouse=True)
def no_mqtt_settle_delay() -> Generator[None]:
    """Collapse the post-connect settle wait to nothing.

    The coordinator waits before its first burst of requests because a
    speaker that has only just sent its CONNACK drops them otherwise. The
    wait is real and load-bearing on hardware, and pure dead time here.
    """
    from custom_components.unifi_play import coordinator as coordinator_module

    with patch.object(coordinator_module, "POST_CONNECT_SETTLE", 0):
        yield


@pytest.fixture
def entity_registry_enabled_by_default() -> Generator[None]:
    """Register the entities that ship disabled, for the tests that need them.

    Individual EQ bands and some diagnostics are off by default - ten sliders
    per speaker is noise for most installs - which means they never reach the
    state machine and a test cannot address them. Home Assistant core ships
    this fixture; pytest-homeassistant-custom-component does not re-export it.
    """
    with patch(
        "homeassistant.helpers.entity.Entity.entity_registry_enabled_default",
        PropertyMock(return_value=True),
    ):
        yield


# ── MQTT ──────────────────────────────────────────────────────────────────


@pytest.fixture
def mqtt_network() -> Generator[FakeMqttNetwork]:
    """A network of fake speakers, in place of paho's client.

    Patched in both modules that build a paho client: the coordinator's
    long-lived connection and discovery's identification probe. Missing
    either leaves a test opening real sockets, which ``pytest-socket``
    (bundled with pytest-homeassistant-custom-component) then blocks with a
    much less obvious error.
    """
    network = FakeMqttNetwork()
    with (
        patch(
            "custom_components.unifi_play.mqtt_client.mqtt.Client",
            side_effect=network.factory,
        ),
        patch(
            "custom_components.unifi_play.discovery.mqtt.Client",
            side_effect=network.factory,
        ),
    ):
        yield network


@pytest.fixture
def amp(mqtt_network: FakeMqttNetwork) -> FakeDevice:
    """The PowerAmp, reachable and accepting both bundled certificates."""
    return mqtt_network.add(
        FakeDevice(
            ip=AMP_IP,
            mac=AMP_MAC,
            platform="UPL-AMP",
            firmware="1.0.38",
            name="Living Room",
        )
    )


@pytest.fixture
def port(mqtt_network: FakeMqttNetwork) -> FakeDevice:
    """The Audio Port, reachable and accepting both bundled certificates."""
    return mqtt_network.add(
        FakeDevice(
            ip=PORT_IP,
            mac=PORT_MAC,
            platform="UPL-PORT",
            firmware="1.1.10",
            name="Kitchen",
        )
    )


@pytest.fixture
def third(mqtt_network: FakeMqttNetwork) -> FakeDevice:
    """A second Audio Port, for the tests that need three speakers."""
    return mqtt_network.add(
        FakeDevice(
            ip=THIRD_IP,
            mac=THIRD_MAC,
            platform="UPL-PORT",
            firmware="1.1.10",
            name="Study",
        )
    )


# ── Discovery ─────────────────────────────────────────────────────────────


@pytest.fixture
def discovered_devices() -> list[dict[str, Any]]:
    """What the UDP sweep finds. Overridden per test with ``pytest.mark``.

    Defaults to both speakers, which is what a healthy direct-mode network
    looks like.
    """
    return [amp_device(), port_device()]


@pytest.fixture
def udp_discovery(
    discovered_devices: list[dict[str, Any]],
) -> Generator[list[dict[str, Any]]]:
    """Stand in for the UDP broadcast sweep.

    ``async_resolve_direct`` still runs for real on top of this, including
    the MQTT identification fallback for hosts that did not answer - the
    path Audio Port hardware needs (#5).
    """

    async def _discover(
        manual_hosts: list[str] | None = None,
        timeout: float = 0.0,
        broadcast: bool = True,
    ) -> list[dict[str, Any]]:
        return list(discovered_devices)

    with patch("custom_components.unifi_play.discovery.async_discover", new=_discover):
        yield discovered_devices


# ── Apollo (console mode) ─────────────────────────────────────────────────


class ApolloServer:
    """A minimal stand-in for a console's Apollo application.

    Wraps Home Assistant's aiohttp mocker so tests describe *what the console
    answered* rather than which URL to register. Every failure shape the API
    client distinguishes has a method here.
    """

    def __init__(self, mocker: AiohttpClientMocker, host: str = CONSOLE_HOST) -> None:
        self._mocker = mocker
        self.host = host

    @property
    def devices_url(self) -> str:
        return f"https://{self.host}{API_PATH}/devices"

    @property
    def groups_url(self) -> str:
        return f"https://{self.host}{API_PATH}/groups"

    @property
    def clients_url(self) -> str:
        return f"https://{self.host}{NETWORK_PATH}/stat/sta"

    #: The mocker leaves ``content_type`` unset when handed ``json=``, and
    #: the API client checks the content type before it looks at the body -
    #: deliberately, because a console with no Apollo application answers 200
    #: with HTML. So every JSON response here has to declare itself, exactly
    #: as a real console does.
    JSON_HEADERS = {"Content-Type": "application/json"}

    def _json(self, url: str, payload: Any, status: int = 200) -> None:
        self._mocker.get(url, status=status, json=payload, headers=self.JSON_HEADERS)

    def devices(self, payload: Any | None = None) -> None:
        """A healthy ``/devices`` response."""
        self._json(
            self.devices_url,
            fixture("apollo_devices.json") if payload is None else payload,
        )

    def devices_without_ip(self) -> None:
        """A console whose firmware stopped populating ``ip``."""
        self._json(self.devices_url, fixture("apollo_devices_no_ip.json"))

    def groups(self, payload: Any | None = None) -> None:
        """A ``/groups`` response. Returns ``data: null`` when empty."""
        self._json(
            self.groups_url,
            {"err": None, "data": None} if payload is None else payload,
        )

    def network_clients(self, payload: Any | None = None) -> None:
        """The Network application's client list, used for IP enrichment."""
        self._json(
            self.clients_url,
            fixture("network_clients.json") if payload is None else payload,
        )

    def status(self, code: int, *, json: Any = None, text: str | None = None) -> None:
        """An arbitrary status on ``/devices``."""
        if json is not None:
            self._json(self.devices_url, json, status=code)
        else:
            self._mocker.get(
                self.devices_url,
                status=code,
                text=text if text is not None else "",
                headers={"Content-Type": "text/plain"},
            )

    def no_apollo_application(self) -> None:
        """UniFi OS with no Apollo route: the SPA catch-all answers 200 HTML.

        The status is 200, so HTML - not the code - is the only signal that
        the application is absent.
        """
        self._mocker.get(
            self.devices_url,
            status=200,
            text="<!doctype html><html><body>UniFi OS</body></html>",
            headers={"Content-Type": "text/html"},
        )

    def malformed_json(self) -> None:
        """``application/json`` whose body is not JSON."""
        self._mocker.get(
            self.devices_url,
            status=200,
            text="{not json",
            headers={"Content-Type": "application/json"},
        )

    def error_envelope(self, message: str = "boom") -> None:
        """A 200 whose body carries Apollo's own error envelope.

        This must never be read as "no devices": the caller would drop every
        speaker it already knows about on a transient console fault.
        """
        self._json(self.devices_url, {"err": {"msg": message}, "data": None})

    def timeout(self) -> None:
        self._mocker.get(self.devices_url, exc=TimeoutError())

    def connection_error(self) -> None:
        """DNS, TCP or TLS failure - the console is not there at all."""
        import aiohttp

        self._mocker.get(
            self.devices_url,
            exc=aiohttp.ClientConnectionError("Cannot connect to host"),
        )


@pytest.fixture
def apollo(aioclient_mock: AiohttpClientMocker) -> ApolloServer:
    """A console's Apollo API, answering on the shared Home Assistant session."""
    return ApolloServer(aioclient_mock)


# ── Config entries ────────────────────────────────────────────────────────


@pytest.fixture
def console_entry() -> MockConfigEntry:
    """A console-mode entry, as the config flow creates one."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"UniFi Play ({CONSOLE_HOST})",
        unique_id=CONSOLE_HOST,
        data={
            CONF_MODE: MODE_CONSOLE,
            CONF_CONTROLLER_HOST: CONSOLE_HOST,
            CONF_API_KEY: API_KEY,
        },
    )


@pytest.fixture
def direct_entry() -> MockConfigEntry:
    """A direct-mode entry with no manually listed hosts."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="UniFi Play (Direct)",
        unique_id=MODE_DIRECT,
        data={CONF_MODE: MODE_DIRECT, CONF_MANUAL_HOSTS: []},
    )


@pytest.fixture
def legacy_entry() -> MockConfigEntry:
    """An entry created before connection modes existed.

    It carries no ``CONF_MODE`` at all and must still be treated as console
    mode, or every pre-1.2 install breaks on upgrade.
    """
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"UniFi Play ({CONSOLE_HOST})",
        unique_id=CONSOLE_HOST,
        data={CONF_CONTROLLER_HOST: CONSOLE_HOST, CONF_API_KEY: API_KEY},
    )


#: One pass of the integration's MQTT loop task is an executor round trip
#: followed by a 10 ms yield, so a bare ``asyncio.sleep(0)`` cannot advance
#: it. This is long enough for a callback to be delivered and short enough
#: that the suite stays quick.
_PUMP_INTERVAL = 0.02
_PUMP_PASSES = 6


async def pump(passes: int = _PUMP_PASSES) -> None:
    """Let the fake transport's queued callbacks reach the integration.

    ``FakeDevice.emit`` queues a message the way a real broker does: it is
    delivered the next time the client's loop runs, on an executor thread.
    Nothing about that is instant from the event loop's point of view.
    """
    for _ in range(passes):
        await asyncio.sleep(_PUMP_INTERVAL)


async def _settle(hass: HomeAssistant) -> None:
    """Await the fake transport catching up, then Home Assistant catching up."""
    for _ in range(_PUMP_PASSES):
        await asyncio.sleep(_PUMP_INTERVAL)
        await hass.async_block_till_done()


@pytest.fixture
def settle() -> Callable[[HomeAssistant], Any]:
    """Await the fake MQTT transport catching up."""
    return _settle


@pytest.fixture
async def setup_direct(
    hass: HomeAssistant,
    direct_entry: MockConfigEntry,
    udp_discovery: list[dict[str, Any]],
    amp: FakeDevice,
    port: FakeDevice,
) -> AsyncIterator[MockConfigEntry]:
    """A loaded direct-mode entry with both speakers connected.

    The default arrangement for anything that is not specifically about
    setup: two models, both online, no console involved.
    """
    direct_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(direct_entry.entry_id)
    await _settle(hass)
    yield direct_entry


@pytest.fixture
async def setup_console(
    hass: HomeAssistant,
    console_entry: MockConfigEntry,
    apollo: ApolloServer,
    amp: FakeDevice,
    port: FakeDevice,
) -> AsyncIterator[MockConfigEntry]:
    """A loaded console-mode entry with both speakers connected."""
    apollo.devices()
    console_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(console_entry.entry_id)
    await _settle(hass)
    yield console_entry


# ── Helpers ───────────────────────────────────────────────────────────────


@pytest.fixture
def zone_events(hass: HomeAssistant) -> list[tuple[str, dict[str, Any]]]:
    """Record every zone topology event fired during a test.

    Recorded as (event_type, data) pairs in order, because the property that
    matters most is the *count*: one logical change must produce one event no
    matter how many speakers report it.
    """
    from custom_components.unifi_play.const import (
        EVENT_ZONE_CREATED,
        EVENT_ZONE_DELETED,
        EVENT_ZONE_MEMBER_CHANGED,
        EVENT_ZONE_RENAMED,
    )

    seen: list[tuple[str, dict[str, Any]]] = []

    def _record(event: Any) -> None:
        seen.append((event.event_type, dict(event.data)))

    for event_type in (
        EVENT_ZONE_CREATED,
        EVENT_ZONE_DELETED,
        EVENT_ZONE_RENAMED,
        EVENT_ZONE_MEMBER_CHANGED,
    ):
        hass.bus.async_listen(event_type, _record)
    return seen


@pytest.fixture
async def synced_zone(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
    settle: Callable[[HomeAssistant], Any],
    zone_events: list[tuple[str, dict[str, Any]]],
) -> MockConfigEntry:
    """Both speakers report one zone and have finished their first sync.

    The state everything after startup happens from. The first report from
    each speaker is applied silently by design, so a test that wants to
    observe an event - or write to a zone that exists - has to get past it.
    """
    from .const import groups_body

    body = groups_body()
    amp.emit("groups", body)
    port.emit("groups", body)
    await settle(hass)
    assert zone_events == [], "the first sync of each speaker must be silent"
    return setup_direct


def entry_coordinator(hass: HomeAssistant, entry: ConfigEntry) -> Any:
    """The coordinator behind a loaded entry.

    A single accessor, so a change of where the coordinator lives is one
    edit rather than one per test.
    """
    return entry.runtime_data


def all_devices(*devices: FakeDevice) -> Iterable[FakeDevice]:
    """Readability helper for tests that assert across every speaker."""
    return devices
