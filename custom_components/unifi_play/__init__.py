"""The UniFi Play integration."""

from __future__ import annotations

import logging

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.typing import ConfigType

from .api import UnifiPlayApi
from .const import (
    CONF_API_KEY,
    CONF_CONTROLLER_HOST,
    CONF_MANUAL_HOSTS,
    CONF_MODE,
    CONF_VERIFY_SSL,
    DOMAIN,
    MODE_CONSOLE,
    MODE_DIRECT,
)
from .coordinator import UnifiPlayConfigEntry, UnifiPlayCoordinator
from .helpers import mac_normalise
from .services import async_register_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.MEDIA_PLAYER,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TEXT,
]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's actions, once per Home Assistant run.

    Not per config entry: an action is a property of the integration, and
    registering them in ``async_setup_entry`` meant a second entry re-ran the
    registration and unloading the first removed actions the second still
    needed. They are registered whether or not any entry loads, so an
    automation referencing one is a validation error naming the action rather
    than an "unknown service" that appears and disappears with a speaker.
    """
    async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: UnifiPlayConfigEntry) -> bool:
    """Set up UniFi Play from a config entry.

    Entries created before connection modes existed carry no CONF_MODE and
    are console entries.
    """
    mode = entry.data.get(CONF_MODE, MODE_CONSOLE)

    if mode == MODE_DIRECT:
        coordinator = UnifiPlayCoordinator(
            hass,
            entry,
            api=None,
            manual_hosts=list(entry.data.get(CONF_MANUAL_HOSTS, [])),
        )
    else:
        # False for entries created before the option existed: they were all
        # set up against an unverified console, and turning verification on
        # under them would break every one of them on upgrade. New entries
        # are offered verification first and record what was chosen.
        verify_ssl = entry.data.get(CONF_VERIFY_SSL, False)
        if not verify_ssl:
            # Once per setup, not once per request. An unverified TLS
            # connection carrying an API key is worth saying out loud, and
            # the log is where somebody auditing what Home Assistant talks to
            # will look.
            _LOGGER.warning(
                "Certificate verification is off for the UniFi Play console at "
                "%s: the API key is sent over a connection whose identity is "
                "not checked. Reconfigure the entry to turn it on once the "
                "console is reachable by a name with a trusted certificate",
                entry.data[CONF_CONTROLLER_HOST],
            )
        # Home Assistant owns this session's lifetime: it caches one per
        # verify_ssl setting and closes them on shutdown.
        session = async_get_clientsession(hass, verify_ssl=verify_ssl)
        api = UnifiPlayApi(
            host=entry.data[CONF_CONTROLLER_HOST],
            api_key=entry.data[CONF_API_KEY],
            session=session,
        )
        coordinator = UnifiPlayCoordinator(hass, entry, api)

    # Raises ConfigEntryNotReady on a transient failure and
    # ConfigEntryAuthFailed on a revoked key; the coordinator draws that
    # distinction so a dead credential prompts for a new one instead of
    # being retried every five minutes forever.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: UnifiPlayConfigEntry) -> bool:
    """Unload a config entry.

    Actions are deliberately not removed: they were registered in
    ``async_setup`` for the whole run, so taking them down with one entry
    would break a second entry that is still loaded, and an automation
    written against them would break on a reload.
    """
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_shutdown()
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: UnifiPlayConfigEntry) -> None:
    """Reload when the entry's data changes.

    Every setting this integration stores decides how it reaches hardware -
    which console, which credential, which addresses to probe - so there is
    nothing that can be applied without rebuilding the connections.
    """
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: UnifiPlayConfigEntry, device_entry: DeviceEntry
) -> bool:
    """Allow the user to delete a device this entry no longer knows about.

    Nothing here removes a device on its own. A speaker that has been
    unplugged for a week still has entities, history and automations pointing
    at it, and one failed discovery pass is not evidence that it is gone -
    UDP silence certainly is not, since Audio Port hardware never answers the
    probe at all (#5). So removal stays a deliberate action, and this only
    decides whether to accept it.

    It is accepted when the device is absent from what the integration
    currently knows: a speaker whose MAC no coordinator reports as current,
    or a zone no speaker reports. One missed scan is not enough — Audio
    Port never answers UDP — but consecutive authoritative absences make a
    retained speaker deletable. Refusing while it is still current is what
    stops a delete that immediately comes back on the next poll, leaving a
    device with no entities.
    """
    coordinator = entry.runtime_data

    for domain, identifier in device_entry.identifiers:
        if domain != DOMAIN or not isinstance(identifier, str):
            continue
        if identifier.startswith("zone_"):
            if identifier[len("zone_") :] in coordinator.groups:
                return False
            continue
        known = {
            mac_normalise(state.mac)
            for state in coordinator.data.values()
            if state.mac and coordinator.device_is_current(state.device_id)
        }
        if mac_normalise(identifier) in known:
            return False
        for device_id, state in list(coordinator.data.items()):
            if state.mac and mac_normalise(state.mac) == mac_normalise(identifier):
                await coordinator.async_forget_device(device_id)
    return True
