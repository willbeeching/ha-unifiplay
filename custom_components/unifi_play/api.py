"""REST API client for the UniFi Play controller (Apollo)."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

API_PATH = "/proxy/apollo/api/v1"
NETWORK_PATH = "/proxy/network/api/s/default"

#: Per-request budget for the console.
#:
#: The console is on the LAN and Apollo answers /devices in tens of
#: milliseconds. Without a timeout aiohttp waits forever, which turns a
#: console that accepts a connection and then stops answering - a UniFi OS
#: mid-upgrade does exactly that - into a coordinator refresh that never
#: returns and an integration that never sets up. The connect budget is
#: shorter than the total because a LAN host that has not completed a TCP
#: handshake in five seconds is not going to.
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=5)


def _normalize_host(host: str) -> str:
    """Return a bare hostname or IP suitable for URL construction."""
    host = host.strip().rstrip("/")
    for prefix in ("https://", "http://"):
        if host.lower().startswith(prefix):
            host = host[len(prefix) :]
            break
    return host.split("/")[0]


def _normalize_mac(mac: str) -> str:
    """Return MAC without separators, lowercased."""
    return mac.lower().replace(":", "").replace("-", "")


class UnifiPlayApiError(Exception):
    """Base exception for API errors."""


class UnifiPlayAuthError(UnifiPlayApiError):
    """Authentication error (HTTP 401) — the API key was not accepted.

    Note that a 401 proves the Apollo application *is* installed: UniFi OS
    only reaches its auth layer for a proxy path that actually has an
    application behind it.
    """


class UnifiPlayForbiddenError(UnifiPlayApiError):
    """The controller refused the request (HTTP 403).

    Usually means the API key is valid but was created on a different
    console, has been revoked, or lacks access to the Apollo API.
    """


class UnifiPlayServiceUnavailableError(UnifiPlayApiError):
    """This console has no Apollo (UniFi Play) application installed.

    UniFi OS writes an nginx route for ``/proxy/<app>`` only for applications
    it has installed. With no Apollo application the request falls through to
    the UniFi OS single-page-app catch-all, which answers ``200`` with an HTML
    body — regardless of whether the API key is valid. So HTML is the signal,
    not the status code.

    A console installs Apollo only when it discovers Play hardware *and* a
    published package exists at or below its release channel. Apollo's channel
    is recorded per console in runnables.yaml and has been seen to differ by
    model, so some consoles never receive it. See docs/api.md.
    """


class UnifiPlayUnsupportedApiError(UnifiPlayApiError):
    """Apollo answered, but has no handler at the path we requested (HTTP 404).

    Apollo itself returns a plain-text 404 for an unknown path, which is
    distinct from the HTML catch-all above. This means the application is
    installed but does not expose the endpoint this integration expects.
    """


class UnifiPlayConnectionError(UnifiPlayApiError):
    """The controller could not be reached at all (DNS, TCP, TLS, timeout)."""


class UnifiPlayTransientError(UnifiPlayApiError):
    """The console answered, but with something that should be retried.

    HTTP 429 (rate limited) and any 5xx. These say nothing about whether the
    configuration is right, so they must not be reported to the user as a
    credential or setup problem, and must not be allowed to discard device
    state the integration already holds: MQTT remains the source of truth for
    everything except which devices exist.
    """


def _status_error(status: int, url: str) -> UnifiPlayApiError:
    """Map an HTTP status to the error that says what to do about it.

    Every non-2xx status maps to something. A status this function did not
    anticipate is still a refusal, and returning None for it - or falling
    through to a JSON parse - is how a proxy's 502 error page once looked
    like "the console has no devices".
    """
    if status == 401:
        return UnifiPlayAuthError(
            "Apollo rejected the API key (HTTP 401). The Apollo application is "
            "installed on this console, so this is a credential problem."
        )
    if status == 403:
        return UnifiPlayForbiddenError(
            "Controller refused the API key (HTTP 403). Check the key was "
            "created on this console and has not been revoked."
        )
    if status == 404:
        return UnifiPlayUnsupportedApiError(
            f"Apollo has no handler for {url} (HTTP 404). The application is "
            "installed but does not expose the expected API path."
        )
    if status == 429 or status >= 500:
        return UnifiPlayTransientError(
            f"Console is not answering properly right now (HTTP {status}). "
            "This is usually temporary."
        )
    return UnifiPlayApiError(f"Console refused the request (HTTP {status})")


class UnifiPlayApi:
    """Async REST client for the UDM Pro Apollo (Play) API."""

    def __init__(
        self,
        host: str,
        api_key: str,
        session: aiohttp.ClientSession,
    ) -> None:
        """Take Home Assistant's shared session; never build one.

        A session this integration created was a session this integration had
        to remember to close, on every path including the ones that raise -
        and the config flow had several that did not. Home Assistant owns the
        lifetime of the shared session, so there is nothing left to leak.
        """
        self._host = _normalize_host(host)
        self._api_key = api_key
        self._session = session

    @property
    def host(self) -> str:
        """Return the normalized controller hostname or IP."""
        return self._host

    @property
    def _base_url(self) -> str:
        return f"https://{self._host}{API_PATH}"

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-API-KEY": self._api_key, "Accept": "application/json"}

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        session = self._session
        url = f"{self._base_url}{path}"
        _LOGGER.debug("%s %s", method, url)
        try:
            async with session.request(
                method, url, headers=self._headers, timeout=REQUEST_TIMEOUT, **kwargs
            ) as resp:
                if resp.content_type == "text/html":
                    # No Apollo application on this console: UniFi OS has no
                    # nginx route for /proxy/apollo, so the request falls
                    # through to its single-page-app catch-all and answers 200
                    # with HTML. This happens whether or not the key is valid,
                    # so it must be detected before any status check - a 200
                    # here would otherwise look like success.
                    _LOGGER.debug(
                        "HTML response from %s (status=%s): no Apollo "
                        "application on this console",
                        url,
                        resp.status,
                    )
                    raise UnifiPlayServiceUnavailableError(
                        "This console has no Apollo (UniFi Play) application. "
                        "It is installed automatically when a UniFi Play "
                        "device is discovered by the console."
                    )
                if resp.status >= 400:
                    # Every non-2xx raises. The three worth diagnosing are
                    # 401/403/404; everything else still has to fail rather
                    # than fall through to a JSON parse, or a proxy's own
                    # error page becomes "the console has no devices".
                    #
                    # The body is logged only when it can help: a 404 body
                    # distinguishes Apollo's own plain-text 404 from a proxy's,
                    # while a 401 or 403 body is a credential response and
                    # some products echo the presented key back in it.
                    if resp.status in (401, 403):
                        _LOGGER.debug(
                            "HTTP %s from %s (body withheld: credential response)",
                            resp.status,
                            url,
                        )
                    else:
                        text = await resp.text()
                        _LOGGER.debug(
                            "HTTP %s from %s: content_type=%s body=%s",
                            resp.status,
                            url,
                            resp.content_type,
                            text[:500],
                        )
                    raise _status_error(resp.status, url)
                if resp.content_type != "application/json":
                    text = await resp.text()
                    _LOGGER.debug(
                        "Non-JSON response from %s: status=%s content_type=%s body=%s",
                        url,
                        resp.status,
                        resp.content_type,
                        text[:500],
                    )
                    raise UnifiPlayApiError(
                        f"Unexpected response ({resp.status}): {text[:200]}"
                    )
                try:
                    data: dict[str, Any] = await resp.json()
                except ValueError as err:
                    # Declared application/json and was not. Distinct from the
                    # HTML catch-all above, which is a console with no Apollo.
                    _LOGGER.debug("Malformed JSON from %s: %s", url, err)
                    raise UnifiPlayApiError(
                        f"Console returned malformed JSON ({resp.status})"
                    ) from err
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("Connection error for %s: %s", url, err)
            raise UnifiPlayConnectionError(f"Connection error: {err}") from err

        if not isinstance(data, dict):
            raise UnifiPlayApiError("Console returned JSON that is not an object")
        if data.get("err"):
            err_body = data["err"]
            msg = (
                err_body.get("msg", "Unknown error")
                if isinstance(err_body, dict)
                else str(err_body)
            )
            raise UnifiPlayApiError(msg)
        return data

    async def get_devices(self) -> list[dict[str, Any]]:
        """Return a list of all Play devices from the controller.

        Recent UniFi firmwares stopped populating ``ip`` in the Apollo
        ``/devices`` response, so we enrich the result with IPs fetched from
        the Network API (where the device is also listed as a client) keyed
        by MAC address.
        """
        data = await self._request("GET", "/devices")
        payload = data.get("data")
        # `data: null` is the documented empty answer (it is what /groups
        # returns with no groups). Anything that is neither a list nor null
        # is a shape nobody has seen, and reading it as "no devices" would
        # drop every speaker already known - the exact failure that made a
        # console hiccup look like the hardware disappearing.
        if payload is None:
            devices: list[dict[str, Any]] = []
        elif isinstance(payload, list):
            devices = payload
        else:
            raise UnifiPlayApiError(
                f"Apollo /devices returned {type(payload).__name__}, expected a list"
            )

        missing_ip = [d for d in devices if not d.get("ip") and d.get("mac")]
        if missing_ip:
            try:
                ip_by_mac = await self._get_client_ip_map()
            except UnifiPlayApiError as err:
                _LOGGER.debug("Network client lookup failed: %s", err)
                ip_by_mac = {}
            for dev in missing_ip:
                mac = _normalize_mac(dev.get("mac", ""))
                ip = ip_by_mac.get(mac)
                if ip:
                    dev["ip"] = ip
        return devices

    async def get_groups(self) -> list[dict[str, Any]]:
        """Return a list of speaker groups.

        Apollo answers ``data: null`` rather than ``[]`` when no groups
        exist, so this cannot null-guard by truthiness alone.
        """
        data = await self._request("GET", "/groups")
        payload = data.get("data")
        if payload is None:
            return []
        if not isinstance(payload, list):
            raise UnifiPlayApiError(
                f"Apollo /groups returned {type(payload).__name__}, expected a list"
            )
        return payload

    async def _get_client_ip_map(self) -> dict[str, str]:
        """Return a mapping of MAC (lowercase, no separators) to IP from the Network API."""
        url = f"https://{self._host}{NETWORK_PATH}/stat/sta"
        try:
            async with self._session.get(
                url, headers=self._headers, timeout=REQUEST_TIMEOUT
            ) as resp:
                if resp.status != 200:
                    raise UnifiPlayApiError(f"Network API status {resp.status}")
                data: dict[str, Any] = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise UnifiPlayApiError(f"Network API error: {err}") from err
        except ValueError as err:
            # A console without the Network application serves the UniFi OS
            # HTML shell here with status 200, so parsing can fail even though
            # the request "succeeded". IP enrichment is best-effort either way.
            raise UnifiPlayApiError(f"Network API returned non-JSON: {err}") from err

        ip_map: dict[str, str] = {}
        for client in data.get("data") or []:
            mac = _normalize_mac(client.get("mac", ""))
            ip = client.get("ip") or client.get("last_ip")
            if mac and ip:
                ip_map[mac] = ip
        return ip_map

    async def validate_connection(self) -> list[dict[str, Any]]:
        """Return the discovered devices, or raise a specific error.

        Failures are re-raised rather than flattened into a boolean so the
        config flow can map each cause to its own actionable message.
        """
        url = f"{self._base_url}/devices"
        try:
            devices = await self.get_devices()
        except UnifiPlayAuthError:
            _LOGGER.warning(
                "Authentication failed for UniFi Play controller at %s (invalid API key)",
                self._host,
            )
            raise
        except UnifiPlayApiError as err:
            _LOGGER.warning(
                "Failed to connect to UniFi Play controller at %s via %s: %s",
                self._host,
                url,
                err,
            )
            raise
        else:
            if devices:
                summary = ", ".join(
                    f"{dev.get('platform', 'unknown')} ({dev.get('name', 'unnamed')})"
                    for dev in devices
                )
                _LOGGER.info(
                    "Connected to UniFi Play controller at %s, found %d device(s): %s",
                    self._host,
                    len(devices),
                    summary,
                )
            else:
                _LOGGER.warning(
                    "Connected to the UniFi Play API at %s, but it returned no "
                    "Play devices. Adopt your Play hardware on this console first",
                    self._host,
                )
            return devices
