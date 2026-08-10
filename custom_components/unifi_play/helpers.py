"""Shared device-resolution helpers used by both services and config flow."""
from __future__ import annotations

import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .coordinator import UnifiPlayCoordinator, UnifiPlayDeviceState, UnifiPlayGroupState


def mac_normalise(mac: str) -> str:
    """Return a MAC address as uppercase hex without delimiters."""
    return mac.upper().replace(":", "")


def resolve_device(
    hass: HomeAssistant, device_id: str
) -> tuple[UnifiPlayCoordinator, str]:
    """Map an HA device_id to its coordinator and internal device id.

    Raises ServiceValidationError so the error surfaces cleanly in both
    service and config-flow callers regardless of how they catch it.
    """
    entry = dr.async_get(hass).async_get(device_id)
    if entry is None:
        raise ServiceValidationError(f"Unknown device: {device_id}")
    norm_macs = {mac_normalise(ident[1]) for ident in entry.identifiers if ident[0] == DOMAIN}
    if not norm_macs:
        raise ServiceValidationError(f"Device {device_id} is not a UniFi Play device")
    # The device registry keys on MAC, so two entries for the same hardware -
    # a console one and a direct one - merge into a single registry device.
    # Prefer whichever coordinator actually holds a live connection, or the
    # caller fails on a dead entry while a working one sits beside it (#15).
    # A registered client is not necessarily a connected one either: the
    # coordinator adds it before dialling out (#14).
    fallback: tuple[UnifiPlayCoordinator, str] | None = None
    for coordinator in hass.data.get(DOMAIN, {}).values():
        for dev_id, state in (coordinator.data or {}).items():
            if mac_normalise(state.mac) not in norm_macs:
                continue
            client = coordinator.get_mqtt_client(dev_id)
            if client is not None and client.is_connected:
                return coordinator, dev_id
            if fallback is None:
                fallback = (coordinator, dev_id)
    if fallback is not None:
        return fallback
    raise ServiceValidationError(f"No live UniFi Play device for {device_id}")


def dev_info_entry(
    state: UnifiPlayDeviceState, *, host: bool = False
) -> dict[str, Any]:
    """Build a dev_info member dict from a device state."""
    return {
        "type": state.platform,
        "mac": state.mac,
        "name": state.name,
        "ip": state.ip,
        "color": "black",
        "host": host,
    }


def gs_to_dict(gs: UnifiPlayGroupState) -> dict[str, Any]:
    """Serialise a UnifiPlayGroupState to the dict format set_groups expects."""
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
        "timestamp": int(time.time()),
    }


def move_zone_to_new_host(
    coordinator: UnifiPlayCoordinator,
    gs: UnifiPlayGroupState,
    new_dev_info: list[dict[str, Any]],
    removed_mac: str,
) -> None:
    """Hand a zone to a new host after its current host has been removed.

    ``new_dev_info`` is the surviving device list with the removed device
    already filtered out; the first entry is promoted to host. Callers must
    have enforced the two-device zone minimum first, so index 0 always exists.
    The list is mutated, so pass copies - ``gs.dev_info`` is the live list the
    coordinator holds, and editing it in place would corrupt cached state.

    Each device's group list is replace-all, so moving a zone is two
    publishes: give it to the new host, then strip it from the old host's
    list. Both ends must be reachable or the zone could end up owned by
    nobody, so that is checked before anything is written.

    The two publishes are deliberately adjacent and synchronous
    (``publish_action`` does not await). Do NOT introduce an await between
    them: it would open a window in which both devices host the same zone.

    Raises ServiceValidationError when either end is offline.
    """
    for idx, dev in enumerate(new_dev_info):
        dev["host"] = idx == 0
    new_host_mac = new_dev_info[0].get("mac", "")
    old_host_mac = gs.host_mac

    new_client = coordinator.get_mqtt_client_for_mac(new_host_mac)
    old_client = coordinator.get_mqtt_client_for_mac(old_host_mac)
    if new_client is None or old_client is None:
        raise ServiceValidationError(
            "No MQTT connection to the old or new zone host - both must be "
            "online to move a zone between hosts"
        )

    # If the removed device was the one broadcasting a wired source, that
    # source leaves with it, so the zone falls back to streaming.
    keep_wb = gs.wb_enable and mac_normalise(
        gs.wb_device or old_host_mac
    ) != mac_normalise(removed_mac)

    new_host_siblings = [
        gs_to_dict(g)
        for g in coordinator.get_groups_hosted_by(
            new_host_mac, exclude_group_id=gs.group_id
        )
    ]
    new_client.update_group(
        group_id=gs.group_id,
        name=gs.name,
        dev_info=new_dev_info,
        group_index=gs.group_index,
        broadcasting_mode=gs.broadcasting_mode,
        wb_enable=keep_wb,
        wb_device=gs.wb_device if keep_wb else "",
        wb_input=gs.wb_input if keep_wb else "",
        sibling_groups=new_host_siblings,
    )
    old_host_groups = [
        gs_to_dict(g) for g in coordinator.get_groups_hosted_by(old_host_mac)
    ]
    old_client.delete_group(gs.group_id, all_groups=old_host_groups)
