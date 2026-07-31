"""The UniFi Play integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import UnifiPlayApi
from .const import (
    CONF_API_KEY,
    CONF_CONTROLLER_HOST,
    CONF_MANUAL_HOSTS,
    CONF_MODE,
    DOMAIN,
    MODE_CONSOLE,
    MODE_DIRECT,
)
from .coordinator import UnifiPlayCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.BUTTON,
    Platform.MEDIA_PLAYER,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TEXT,
]

type UnifiPlayConfigEntry = ConfigEntry


async def async_setup_entry(hass: HomeAssistant, entry: UnifiPlayConfigEntry) -> bool:
    """Set up UniFi Play from a config entry.

    Entries created before connection modes existed carry no CONF_MODE and
    are console entries.
    """
    mode = entry.data.get(CONF_MODE, MODE_CONSOLE)

    if mode == MODE_DIRECT:
        coordinator = UnifiPlayCoordinator(
            hass, api=None, manual_hosts=entry.data.get(CONF_MANUAL_HOSTS, [])
        )
    else:
        session = async_get_clientsession(hass, verify_ssl=False)
        api = UnifiPlayApi(
            host=entry.data[CONF_CONTROLLER_HOST],
            api_key=entry.data[CONF_API_KEY],
            session=session,
        )
        coordinator = UnifiPlayCoordinator(hass, api)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: UnifiPlayConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: UnifiPlayCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
    return unload_ok
