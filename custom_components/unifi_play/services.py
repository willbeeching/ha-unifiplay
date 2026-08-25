"""Services for UniFi Play.

These cover device features that have no natural Home Assistant entity:
firing an announcement, and CRUD over alarms, quiet-hours windows and custom
EQ presets. Everything here maps to an MQTT action captured from the official
app - see the repository docs for the protocol.
"""

from __future__ import annotations

import posixpath
import uuid
from collections.abc import Callable

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .coordinator import UnifiPlayCoordinator, UnifiPlayDeviceState, UnifiPlayGroupState
from .helpers import (
    dev_info_entry,
    mac_normalise,
    move_zone_to_new_host,
    resolve_device,
)
from .mqtt_client import UnifiPlayMqttClient

ATTR_DEVICE_ID = "device_id"

# Kept in step with services.yaml and the services block in strings.json.
SERVICE_NAMES = (
    "play_announcement",
    "stop_announcement",
    "delete_announcement_file",
    "set_alarm",
    "delete_alarm",
    "set_quiet_hours",
    "delete_quiet_hours",
    "save_eq_preset",
    "delete_eq_preset",
    "rename_eq_preset",
    # Zone management
    "create_zone",
    "delete_zone",
    "add_zone_member",
    "remove_zone_member",
    "rename_zone",
    # Zone audio
    "play_zone_announcement",
    "stop_zone_announcement",
    # Zone ordering
    "set_zone_index",
)

WEEKDAYS = vol.All(cv.ensure_list, [vol.All(vol.Coerce(int), vol.Range(0, 6))])

_DEVICE = {vol.Required(ATTR_DEVICE_ID): cv.string}

PLAY_ANNOUNCEMENT_SCHEMA = vol.Schema(
    {
        **_DEVICE,
        vol.Required("filename"): cv.string,
        vol.Optional("length", default=0): vol.Coerce(int),
        vol.Optional("zone_play", default=False): cv.boolean,
    }
)

STOP_SCHEMA = vol.Schema(_DEVICE)

DELETE_FILE_SCHEMA = vol.Schema({**_DEVICE, vol.Required("filename"): cv.string})

SET_ALARM_SCHEMA = vol.Schema(
    {
        **_DEVICE,
        vol.Optional("alarm_id"): cv.string,
        vol.Optional("name", default="Alarm"): cv.string,
        vol.Required("hour"): vol.All(vol.Coerce(int), vol.Range(0, 23)),
        vol.Required("minute"): vol.All(vol.Coerce(int), vol.Range(0, 59)),
        vol.Optional("sound", default="Lunar Chimes"): cv.string,
        vol.Optional("volume", default=25): vol.All(vol.Coerce(int), vol.Range(0, 100)),
        vol.Optional("duration", default=2): vol.Coerce(int),
        vol.Optional("repeat", default=list): WEEKDAYS,
        vol.Optional("enabled", default=True): cv.boolean,
    }
)

DELETE_ALARM_SCHEMA = vol.Schema({**_DEVICE, vol.Required("alarm_id"): cv.string})

SET_QUIET_HOURS_SCHEMA = vol.Schema(
    {
        **_DEVICE,
        vol.Optional("quiet_id"): cv.string,
        vol.Required("start_hour"): vol.All(vol.Coerce(int), vol.Range(0, 23)),
        vol.Optional("start_minute", default=0): vol.All(
            vol.Coerce(int), vol.Range(0, 59)
        ),
        vol.Required("end_hour"): vol.All(vol.Coerce(int), vol.Range(0, 23)),
        vol.Optional("end_minute", default=0): vol.All(
            vol.Coerce(int), vol.Range(0, 59)
        ),
        vol.Optional("repeat", default=list): WEEKDAYS,
        vol.Optional("wind_down", default=0): vol.Coerce(int),
    }
)

DELETE_QUIET_HOURS_SCHEMA = vol.Schema({**_DEVICE, vol.Required("quiet_id"): cv.string})

EQ_PRESET_SCHEMA = vol.Schema({**_DEVICE, vol.Required("name"): cv.string})

RENAME_EQ_PRESET_SCHEMA = vol.Schema(
    {**_DEVICE, vol.Required("name"): cv.string, vol.Required("new_name"): cv.string}
)

# Zone management schemas. Zone services target zone entities (media_player.*
# created by UnifiPlayZonePlayer) rather than individual device IDs.
_ZONE = {vol.Required("entity_id"): cv.entity_id}

_NAME = vol.All(cv.string, vol.Length(min=1, max=255))
_FILENAME = vol.All(cv.string, vol.Length(min=1, max=255))

CREATE_ZONE_SCHEMA = vol.Schema(
    {
        vol.Required("name"): _NAME,
        vol.Required("host_device_id"): cv.string,
        vol.Optional("member_device_ids", default=[]): vol.All(
            cv.ensure_list, [cv.string]
        ),
    }
)

DELETE_ZONE_SCHEMA = vol.Schema(_ZONE)

ADD_ZONE_MEMBER_SCHEMA = vol.Schema({**_ZONE, vol.Required(ATTR_DEVICE_ID): cv.string})

REMOVE_ZONE_MEMBER_SCHEMA = vol.Schema(
    {**_ZONE, vol.Required(ATTR_DEVICE_ID): cv.string}
)

RENAME_ZONE_SCHEMA = vol.Schema({**_ZONE, vol.Required("name"): _NAME})

PLAY_ZONE_ANNOUNCEMENT_SCHEMA = vol.Schema(
    {
        **_ZONE,
        vol.Required("filename"): _FILENAME,
        vol.Optional("length", default=0): vol.Coerce(int),
    }
)

STOP_ZONE_ANNOUNCEMENT_SCHEMA = vol.Schema(_ZONE)

SET_ZONE_INDEX_SCHEMA = vol.Schema(
    {**_ZONE, vol.Required("group_index"): vol.All(vol.Coerce(int), vol.Range(0, 99))}
)


def _client(hass: HomeAssistant, call: ServiceCall) -> UnifiPlayMqttClient:
    coordinator, dev_id = resolve_device(hass, call.data[ATTR_DEVICE_ID])
    client = coordinator.get_mqtt_client(dev_id)
    # A registered client is not necessarily a connected one: the coordinator
    # adds it before dialling out, and publish_action drops commands silently
    # while disconnected (#14).
    if client is None or not client.is_connected:
        raise ServiceValidationError("No MQTT connection to that device")
    return client


def _resolve_zone(
    hass: HomeAssistant, entity_id: str
) -> tuple[UnifiPlayCoordinator, str]:
    """Map a zone entity_id to its coordinator and group_id."""
    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)
    if entry is None:
        raise ServiceValidationError(f"Unknown entity: {entity_id}")
    uid = entry.unique_id or ""
    if not uid.startswith("unifi_play_zone_"):
        raise ServiceValidationError(f"{entity_id} is not a UniFi Play zone entity")
    group_id = uid[len("unifi_play_zone_") :]
    for coordinator in hass.data.get(DOMAIN, {}).values():
        if group_id in coordinator.groups:
            return coordinator, group_id
    raise ServiceValidationError(f"Zone {group_id} not found in any active coordinator")


def _zone_host_client(
    coordinator: UnifiPlayCoordinator, group_id: str
) -> UnifiPlayMqttClient:
    client = coordinator.get_host_mqtt_client(group_id)
    # Registered is not connected: publish_action drops commands silently
    # while the socket is down, so a zone write would report success and
    # never reach the host (#14).
    if client is None or not client.is_connected:
        raise ServiceValidationError("No MQTT connection to zone host")
    return client


def async_register_services(hass: HomeAssistant) -> None:
    """Register the integration's services once per Home Assistant run."""
    if hass.services.has_service(DOMAIN, "play_announcement"):
        return

    def _state(call: ServiceCall) -> UnifiPlayDeviceState:
        coordinator, dev_id = resolve_device(hass, call.data[ATTR_DEVICE_ID])
        state = (coordinator.data or {}).get(dev_id)
        if state is None:
            raise ServiceValidationError("Device data not yet available")
        return state

    async def play_announcement(call: ServiceCall) -> None:
        state = _state(call)
        name = call.data["filename"]
        length = call.data["length"]
        # Fall back to the duration the device already reported for this clip
        # so callers don't have to know it.
        if not length:
            for f in state.ann_files:
                if isinstance(f, dict) and f.get("name") == name.split("/")[-1]:
                    length = f.get("length", 0)
                    break
        _client(hass, call).play_announcement(name, length, call.data["zone_play"])

    async def stop_announcement(call: ServiceCall) -> None:
        _client(hass, call).stop_announcement()

    async def delete_announcement_file(call: ServiceCall) -> None:
        state = _state(call)
        name = call.data["filename"]
        length = 0
        for f in state.ann_files:
            if isinstance(f, dict) and f.get("name") == name:
                length = f.get("length", 0)
                break
        _client(hass, call).delete_announcement_file(name, length)

    async def set_alarm(call: ServiceCall) -> None:
        alarm_id = call.data.get("alarm_id")
        _client(hass, call).set_alarm(
            alarm_id=alarm_id or str(uuid.uuid4()),
            name=call.data["name"],
            hour=call.data["hour"],
            minute=call.data["minute"],
            sound=call.data["sound"],
            volume=call.data["volume"],
            duration=call.data["duration"],
            repeat=call.data["repeat"],
            enabled=call.data["enabled"],
            action="mod" if alarm_id else "add",
        )

    async def delete_alarm(call: ServiceCall) -> None:
        _client(hass, call).delete_alarm(call.data["alarm_id"])

    async def set_quiet_hours(call: ServiceCall) -> None:
        quiet_id = call.data.get("quiet_id")
        _client(hass, call).set_quiet_hours(
            quiet_id=quiet_id or str(uuid.uuid4()),
            start_hour=call.data["start_hour"],
            start_minute=call.data["start_minute"],
            end_hour=call.data["end_hour"],
            end_minute=call.data["end_minute"],
            repeat=call.data["repeat"],
            wind_down=call.data["wind_down"],
            action="mod" if quiet_id else "add",
        )

    async def delete_quiet_hours(call: ServiceCall) -> None:
        _client(hass, call).delete_quiet_hours(call.data["quiet_id"])

    async def save_eq_preset(call: ServiceCall) -> None:
        state = _state(call)
        if not state.eq_table:
            raise ServiceValidationError("No EQ table reported by the device yet")
        _client(hass, call).save_eq_preset(
            call.data["name"], {k: float(v) for k, v in state.eq_table.items()}
        )

    async def delete_eq_preset(call: ServiceCall) -> None:
        _client(hass, call).delete_eq_preset(call.data["name"])

    async def rename_eq_preset(call: ServiceCall) -> None:
        _client(hass, call).rename_eq_preset(call.data["name"], call.data["new_name"])

    # --- Zone management ---

    async def create_zone(call: ServiceCall) -> None:
        host_coordinator, host_dev_id = resolve_device(
            hass, call.data["host_device_id"]
        )
        host_state = (host_coordinator.data or {}).get(host_dev_id)
        if host_state is None:
            raise ServiceValidationError("Host device data not yet available")
        host_client = host_coordinator.get_mqtt_client(host_dev_id)
        if host_client is None:
            raise ServiceValidationError("No MQTT connection to host device")

        # Two maps across all coordinators, because the rule differs by role:
        #  - member_mac_to_zone: appears as a non-host member somewhere. A
        #    device may host more than one zone, so only a MEMBER appearance
        #    disqualifies it from hosting a new one.
        #  - any_mac_to_zone: appears in any role. Joining as a member is
        #    disqualified by either, otherwise a device hosting zone A could
        #    also be made a member of zone B and end up in two zones - the
        #    thing this check exists to prevent.
        member_mac_to_zone: dict[str, str] = {}
        any_mac_to_zone: dict[str, str] = {}
        for coord in hass.data.get(DOMAIN, {}).values():
            for gs in coord.groups.values():
                for dev in gs.dev_info:
                    m = mac_normalise(dev.get("mac", ""))
                    if not m:
                        continue
                    any_mac_to_zone[m] = gs.name
                    if not dev.get("host"):
                        member_mac_to_zone[m] = gs.name

        host_mac = mac_normalise(host_state.mac)
        if host_mac in member_mac_to_zone:
            raise ServiceValidationError(
                f"'{host_state.name}' is already a member of zone "
                f"'{member_mac_to_zone[host_mac]}'. Remove it from that zone first."
            )

        # Create the zone with no host designated - the "host" key is OMITTED
        # entirely, not set false (see dev_info_entry: the app never sends it,
        # and "host": false does not work either). The firmware elects.
        # The firmware elects its own host and writes the flag back: a zone
        # created in the Play app is reported with a fully populated dev_info
        # and no host at creation, and only names one on a later read
        # (observed on five UPL-PORTs, fw 1.1.10). Pre-designating a host is
        # the one thing an app-made zone never does, and the symptom of doing
        # it is a zone every device agrees on that only ever sounds on the
        # host - the member registers membership and stays silent, over
        # AirPlay and Spotify Connect alike. See docs/api.md.
        dev_info = [dev_info_entry(host_state)]
        for member_device_id in call.data["member_device_ids"]:
            m_coordinator, m_dev_id = resolve_device(hass, member_device_id)
            m_state = (m_coordinator.data or {}).get(m_dev_id)
            if m_state is None or mac_normalise(m_state.mac) == host_mac:
                continue
            m_mac = mac_normalise(m_state.mac)
            if m_mac in any_mac_to_zone:
                raise ServiceValidationError(
                    f"'{m_state.name}' is already in zone '{any_mac_to_zone[m_mac]}'. "
                    "A device can only be in one zone at a time — remove it first."
                )
            dev_info.append(dev_info_entry(m_state))

        host_coordinator.update_zone(
            group_id=str(uuid.uuid4()),
            name=call.data["name"],
            dev_info=dev_info,
        )

    async def delete_zone(call: ServiceCall) -> None:
        coordinator, group_id = _resolve_zone(hass, call.data["entity_id"])
        coordinator.delete_zone(group_id)

    async def add_zone_member(call: ServiceCall) -> None:
        coordinator, group_id = _resolve_zone(hass, call.data["entity_id"])
        gs: UnifiPlayGroupState = coordinator.groups[group_id]
        m_coordinator, m_dev_id = resolve_device(hass, call.data[ATTR_DEVICE_ID])
        m_state = (m_coordinator.data or {}).get(m_dev_id)
        if m_state is None:
            raise ServiceValidationError("Member device data not yet available")

        if any(
            mac_normalise(d.get("mac", "")) == mac_normalise(m_state.mac)
            for d in gs.dev_info
        ):
            raise ServiceValidationError(f"{m_state.name} is already in this zone")

        m_mac = mac_normalise(m_state.mac)
        for coord in hass.data.get(DOMAIN, {}).values():
            for other_gs in coord.groups.values():
                if other_gs.group_id == group_id:
                    continue
                for dev in other_gs.dev_info:
                    if mac_normalise(dev.get("mac", "")) == m_mac:
                        raise ServiceValidationError(
                            f"'{m_state.name}' is already in zone '{other_gs.name}'. "
                            "A device can only be in one zone at a time — remove it first."
                        )

        new_dev_info = list(gs.dev_info) + [dev_info_entry(m_state)]
        coordinator.update_zone(
            group_id=gs.group_id,
            name=gs.name,
            dev_info=new_dev_info,
            group_index=gs.group_index,
            broadcasting_mode=gs.broadcasting_mode,
            wb_enable=gs.wb_enable,
            wb_device=gs.wb_device,
            wb_input=gs.wb_input,
        )

    async def remove_zone_member(call: ServiceCall) -> None:
        coordinator, group_id = _resolve_zone(hass, call.data["entity_id"])
        gs: UnifiPlayGroupState = coordinator.groups[group_id]
        m_coordinator, m_dev_id = resolve_device(hass, call.data[ATTR_DEVICE_ID])
        m_state = (m_coordinator.data or {}).get(m_dev_id)
        if m_state is None:
            raise ServiceValidationError("Member device data not yet available")

        target = mac_normalise(m_state.mac)
        new_dev_info = [
            dict(d) for d in gs.dev_info if mac_normalise(d.get("mac", "")) != target
        ]
        if len(new_dev_info) == len(gs.dev_info):
            raise ServiceValidationError(f"{m_state.name} is not in this zone")
        if len(new_dev_info) < 2:
            raise ServiceValidationError(
                "A zone needs at least 2 devices — delete the zone instead"
            )

        if target != mac_normalise(gs.host_mac):
            coordinator.update_zone(
                group_id=gs.group_id,
                name=gs.name,
                dev_info=new_dev_info,
                group_index=gs.group_index,
                broadcasting_mode=gs.broadcasting_mode,
                wb_enable=gs.wb_enable,
                wb_device=gs.wb_device,
                wb_input=gs.wb_input,
            )
            return

        # Removing the device that currently hosts: hand the role to another
        # member. Shared with the config flow's remove step so the two cannot
        # drift apart; raises ServiceValidationError if either end is offline.
        move_zone_to_new_host(coordinator, gs, new_dev_info, target)

    async def rename_zone(call: ServiceCall) -> None:
        coordinator, group_id = _resolve_zone(hass, call.data["entity_id"])
        gs: UnifiPlayGroupState = coordinator.groups[group_id]
        coordinator.update_zone(
            group_id=gs.group_id,
            name=call.data["name"],
            dev_info=gs.dev_info,
            group_index=gs.group_index,
            broadcasting_mode=gs.broadcasting_mode,
            wb_enable=gs.wb_enable,
            wb_device=gs.wb_device,
            wb_input=gs.wb_input,
        )

    async def set_zone_index(call: ServiceCall) -> None:
        coordinator, group_id = _resolve_zone(hass, call.data["entity_id"])
        gs: UnifiPlayGroupState = coordinator.groups[group_id]
        coordinator.update_zone(
            group_id=gs.group_id,
            name=gs.name,
            dev_info=gs.dev_info,
            group_index=call.data["group_index"],
            broadcasting_mode=gs.broadcasting_mode,
            wb_enable=gs.wb_enable,
            wb_device=gs.wb_device,
            wb_input=gs.wb_input,
        )

    async def play_zone_announcement(call: ServiceCall) -> None:
        coordinator, group_id = _resolve_zone(hass, call.data["entity_id"])
        client = _zone_host_client(coordinator, group_id)
        raw_name = call.data["filename"]
        # normpath collapses middle-path ".." segments (a/../b → b); the
        # explicit split check catches a leading ".." that normpath cannot
        # resolve without a base (../evil stays ../evil after normpath).
        safe_name = posixpath.normpath(raw_name.lstrip("/"))
        if ".." in safe_name.split("/"):
            raise ServiceValidationError("Invalid filename: path traversal not allowed")
        length = call.data["length"]
        if not length:
            host_state = coordinator.get_zone_host_state(group_id)
            if host_state:
                for f in host_state.ann_files:
                    if (
                        isinstance(f, dict)
                        and f.get("name") == safe_name.split("/")[-1]
                    ):
                        length = f.get("length", 0)
                        break
        client.play_announcement(safe_name, length, zone_play=True)

    async def stop_zone_announcement(call: ServiceCall) -> None:
        coordinator, group_id = _resolve_zone(hass, call.data["entity_id"])
        for _, _, client in coordinator.get_zone_members(group_id):
            if client:
                client.stop_announcement()

    handlers: list[tuple[str, Callable, vol.Schema]] = [
        ("play_announcement", play_announcement, PLAY_ANNOUNCEMENT_SCHEMA),
        ("stop_announcement", stop_announcement, STOP_SCHEMA),
        ("delete_announcement_file", delete_announcement_file, DELETE_FILE_SCHEMA),
        ("set_alarm", set_alarm, SET_ALARM_SCHEMA),
        ("delete_alarm", delete_alarm, DELETE_ALARM_SCHEMA),
        ("set_quiet_hours", set_quiet_hours, SET_QUIET_HOURS_SCHEMA),
        ("delete_quiet_hours", delete_quiet_hours, DELETE_QUIET_HOURS_SCHEMA),
        ("save_eq_preset", save_eq_preset, EQ_PRESET_SCHEMA),
        ("delete_eq_preset", delete_eq_preset, EQ_PRESET_SCHEMA),
        ("rename_eq_preset", rename_eq_preset, RENAME_EQ_PRESET_SCHEMA),
        ("create_zone", create_zone, CREATE_ZONE_SCHEMA),
        ("delete_zone", delete_zone, DELETE_ZONE_SCHEMA),
        ("add_zone_member", add_zone_member, ADD_ZONE_MEMBER_SCHEMA),
        ("remove_zone_member", remove_zone_member, REMOVE_ZONE_MEMBER_SCHEMA),
        ("rename_zone", rename_zone, RENAME_ZONE_SCHEMA),
        (
            "play_zone_announcement",
            play_zone_announcement,
            PLAY_ZONE_ANNOUNCEMENT_SCHEMA,
        ),
        (
            "stop_zone_announcement",
            stop_zone_announcement,
            STOP_ZONE_ANNOUNCEMENT_SCHEMA,
        ),
        ("set_zone_index", set_zone_index, SET_ZONE_INDEX_SCHEMA),
    ]
    for name, handler, schema in handlers:
        hass.services.async_register(DOMAIN, name, handler, schema=schema)


def async_unregister_services(hass: HomeAssistant) -> None:
    """Remove the integration's services when the last entry unloads."""
    for name in SERVICE_NAMES:
        hass.services.async_remove(DOMAIN, name)
