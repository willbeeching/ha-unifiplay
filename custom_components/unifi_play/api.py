"""REST API client for the UniFi Play controller (Apollo)."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

API_PATH = "/proxy/apollo/api/v1"
NETWORK_PATH = "/proxy/network/api/s/default"


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


def _status_error(status: int, url: str) -> UnifiPlayApiError:
    """Return the specific error for an HTTP status worth diagnosing."""
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
    return UnifiPlayUnsupportedApiError(
        f"Apollo has no handler for {url} (HTTP 404). The application is "
        "installed but does not expose the expected API path."
    )


class UnifiPlayApi:
    """Async REST client for the UDM Pro Apollo (Play) API."""

    def __init__(
        self,
        host: str,
        api_key: str,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._host = _normalize_host(host)
        self._api_key = api_key
        self._session = session
        self._owns_session = session is None

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

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=False)
            self._session = aiohttp.ClientSession(connector=connector)
            self._owns_session = True
        return self._session

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        session = await self._ensure_session()
        url = f"{self._base_url}{path}"
        _LOGGER.debug("%s %s", method, url)
        try:
            async with session.request(
                method, url, headers=self._headers, **kwargs
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
                if resp.status in (401, 403, 404):
                    # These statuses each point at a distinct, fixable cause,
                    # so log the body and raise a specific error rather than
                    # collapsing them into a generic connection failure.
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
                data: dict[str, Any] = await resp.json()
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("Connection error for %s: %s", url, err)
            raise UnifiPlayConnectionError(f"Connection error: {err}") from err

        if data.get("err"):
            msg = data["err"].get("msg", "Unknown error")
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
        devices = data.get("data") or []

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
        """Return a list of speaker groups."""
        data = await self._request("GET", "/groups")
        return data.get("data") or []

    async def _get_client_ip_map(self) -> dict[str, str]:
        """Return a mapping of MAC (lowercase, no separators) to IP from the Network API."""
        session = await self._ensure_session()
        url = f"https://{self._host}{NETWORK_PATH}/stat/sta"
        try:
            async with session.get(url, headers=self._headers) as resp:
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

    async def close(self) -> None:
        """Close the session if we own it."""
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()
