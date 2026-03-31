"""REST API client for the UniFi Play controller (Apollo)."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

API_PATH = "/proxy/apollo/api/v1"


class UnifiPlayApiError(Exception):
    """Base exception for API errors."""


class UnifiPlayAuthError(UnifiPlayApiError):
    """Authentication error."""


class UnifiPlayApi:
    """Async REST client for the UDM Pro Apollo (Play) API."""

    def __init__(
        self,
        host: str,
        api_key: str,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._host = host.rstrip("/")
        self._api_key = api_key
        self._session = session
        self._owns_session = session is None

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

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        session = await self._ensure_session()
        url = f"{self._base_url}{path}"
        try:
            async with session.request(
                method, url, headers=self._headers, **kwargs
            ) as resp:
                if resp.status == 401:
                    raise UnifiPlayAuthError("Invalid API key")
                if resp.content_type != "application/json":
                    text = await resp.text()
                    raise UnifiPlayApiError(
                        f"Unexpected response ({resp.status}): {text[:200]}"
                    )
                data: dict = await resp.json()
        except aiohttp.ClientError as err:
            raise UnifiPlayApiError(f"Connection error: {err}") from err

        if data.get("err"):
            msg = data["err"].get("msg", "Unknown error")
            raise UnifiPlayApiError(msg)
        return data

    async def get_devices(self) -> list[dict]:
        """Return a list of all Play devices from the controller."""
        data = await self._request("GET", "/devices")
        return data.get("data") or []

    async def get_groups(self) -> list[dict]:
        """Return a list of speaker groups."""
        data = await self._request("GET", "/groups")
        return data.get("data") or []

    async def validate_connection(self) -> bool:
        """Validate that we can connect and authenticate."""
        try:
            await self.get_devices()
            return True
        except UnifiPlayAuthError:
            raise
        except UnifiPlayApiError:
            return False

    async def close(self) -> None:
        """Close the session if we own it."""
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()
