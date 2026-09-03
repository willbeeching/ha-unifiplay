"""Shared device-resolution helpers used by both services and config flow."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .coordinator import UnifiPlayCoordinator, UnifiPlayDeviceState, UnifiPlayGroupState

_LOGGER = logging.getLogger(__name__)


def loaded_coordinators(hass: HomeAssistant) -> list[UnifiPlayCoordinator]:
    """Every coordinator currently loaded, across every config entry.

    Two entries can reach the same hardware - a console one and a direct one
    - so anything resolving a device or a zone has to look across all of
    them. ``async_loaded_entries`` filters to entries that actually set up,
    which is the right bias: an entry that failed has no coordinator and is
    not claiming anything.
    """
    return [
        entry.runtime_data for entry in hass.config_entries.async_loaded_entries(DOMAIN)
    ]


def mac_normalise(mac: str) -> str:
    """Return a MAC address as uppercase hex without delimiters."""
    return mac.upper().replace(":", "")


def resolve_device(
    hass: HomeAssistant, device_id: str
) -> tuple[UnifiPlayCoordinator, str, UnifiPlayDeviceState]:
    """Map an HA device_id to its coordinator, internal device id and state.

    The state comes back with the other two because finding it is how the
    match is made: a device id resolves by looking for a speaker whose MAC
    matches, so there is always a state and callers that fetched it again
    were writing a guard against a case that cannot happen.

    Raises ServiceValidationError so the error surfaces cleanly in both
    service and config-flow callers regardless of how they catch it.
    """
    entry = dr.async_get(hass).async_get(device_id)
    if entry is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_device",
            translation_placeholders={"device_id": device_id},
        )
    norm_macs = {
        mac_normalise(ident[1]) for ident in entry.identifiers if ident[0] == DOMAIN
    }
    if not norm_macs:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="not_a_play_device",
            translation_placeholders={"device_id": device_id},
        )
    # The device registry keys on MAC, so two entries for the same hardware -
    # a console one and a direct one - merge into a single registry device.
    # Prefer whichever coordinator actually holds a live connection, or the
    # caller fails on a dead entry while a working one sits beside it (#15).
    # A registered client is not necessarily a connected one either: the
    # coordinator adds it before dialling out (#14).
    fallback: tuple[UnifiPlayCoordinator, str, UnifiPlayDeviceState] | None = None
    for coordinator in loaded_coordinators(hass):
        for dev_id, state in (coordinator.data or {}).items():
            if mac_normalise(state.mac) not in norm_macs:
                continue
            client = coordinator.get_mqtt_client(dev_id)
            if client is not None and client.is_connected:
                return coordinator, dev_id, state
            if fallback is None:
                fallback = (coordinator, dev_id, state)
    if fallback is not None:
        return fallback
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="no_live_device",
        translation_placeholders={"device_id": device_id},
    )


#: Keys the UniFi Play app sends in a dev_info entry. Everything else a device
#: echoes back (notably "host") is firmware-owned and must not be written back.
APP_DEV_INFO_KEYS = frozenset({"type", "mac", "name", "ip", "color"})


def dev_info_entry(state: UnifiPlayDeviceState) -> dict[str, Any]:
    """Build a dev_info member dict from a device state.

    Deliberately omits "host". Captured set_groups writes from the UniFi Play
    app never carry that key: the firmware elects a host itself and echoes the
    flag back afterwards. Asserting it on the wire produces a zone that
    registers membership on every device but never carries audio to the
    members. Note that "host": false is not equivalent - the app omits the key.
    """
    return {
        "type": state.platform,
        "mac": state.mac,
        "name": state.name,
        "ip": state.ip,
        "color": "black",
    }


def strip_firmware_keys(dev_info: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop firmware-owned keys from dev_info before writing it back.

    Only APP_DEV_INFO_KEYS survive. Anything else a device added - "host"
    above all - is the firmware's to set, not ours to echo.
    """
    stripped = []
    for entry in dev_info:
        dropped = set(entry) - APP_DEV_INFO_KEYS - {"host"}
        if dropped:
            _LOGGER.debug("dropping unrecognised dev_info key(s) %s", sorted(dropped))
        stripped.append({k: v for k, v in entry.items() if k in APP_DEV_INFO_KEYS})
    return stripped


def gs_to_dict(gs: UnifiPlayGroupState) -> dict[str, Any]:
    """Serialise a device-reported zone back to set_groups form, verbatim.

    Deliberately echoes the firmware-owned "host" flag rather than stripping
    it. The write path rebuilds the whole group list on every write, so this
    runs for zones that are not being edited; blanking their host would force a
    re-election on an untouched - possibly playing - zone every time the user
    renames a different one. Only the zone actually being written goes through
    group_payload(), which strips firmware-owned keys.
    """
    return {
        "group_id": gs.group_id,
        "name": gs.name,
        "dev_info": gs.dev_info,
        "dev_count": gs.dev_count,
        "group_index": gs.group_index,
        "broadcasting_mode": gs.broadcasting_mode,
        "wb_enable": gs.wb_enable,
        "wb_device": gs.wb_device,
        "wb_input": gs.wb_input,
        # No per-group timestamp, for the same reason group_payload() dropped
        # it (#22): set_groups accepts one, the groups event never echoes it
        # back inside a group, so it is write-only noise here too.
    }


def group_payload(
    *,
    group_id: str,
    name: str,
    dev_info: list[dict[str, Any]],
    group_index: int = 0,
    broadcasting_mode: str = "zone_only",
    wb_enable: bool = False,
    wb_device: str = "",
    wb_input: str = "",
) -> dict[str, Any]:
    """Build the wire dict for one zone, as set_groups expects it.

    Kept next to gs_to_dict so a zone built here and a zone echoed back by a
    device serialise identically - the fan-out below mixes both in one list.
    """
    return {
        "group_id": group_id,
        "name": name,
        "dev_info": strip_firmware_keys(dev_info),
        "dev_count": len(dev_info),
        "group_index": group_index,
        "broadcasting_mode": broadcasting_mode,
        # The app omits these three when broadcasting is off, but "off" is an
        # active command here (Set audio source -> Streaming), and whether the
        # firmware reads an absent wb_enable as "off" or as "leave unchanged"
        # is unverified. Keep sending them explicitly.
        "wb_enable": wb_enable,
        "wb_device": wb_device,
        "wb_input": wb_input,
        # No per-group timestamp: set_groups accepts one but the groups event
        # never echoes it back, so it is write-only noise.
    }
