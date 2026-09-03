"""Services for UniFi Play.

These cover device features that have no natural Home Assistant entity:
firing an announcement, and CRUD over alarms, quiet-hours windows and custom
EQ presets. Everything here maps to an MQTT action captured from the official
app - see the repository docs for the protocol.
"""

from __future__ import annotations

import posixpath
import uuid
from collections.abc import Callable, Coroutine
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .coordinator import UnifiPlayCoordinator, UnifiPlayDeviceState
from .helpers import resolve_device
from .mqtt_client import UnifiPlayMqttClient

ATTR_DEVICE_ID = "device_id"

#: Every service handler below has this shape.
_ServiceHandler = Callable[[ServiceCall], Coroutine[Any, Any, None]]

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

# Annotated because the keys are voluptuous markers, not strings: without
# this, every schema built by unpacking it is an un-inferrable dict.
_DEVICE: dict[Any, Any] = {vol.Required(ATTR_DEVICE_ID): cv.string}

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
_ZONE: dict[Any, Any] = {vol.Required("entity_id"): cv.entity_id}

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
        state = (coordinator.data or {}).get(dev_id)
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="device_not_connected",
            translation_placeholders={"device": state.device_name if state else dev_id},
        )
    return client


def _resolve_zone(
    hass: HomeAssistant, entity_id: str
) -> tuple[UnifiPlayCoordinator, str]:
    """Map a zone entity_id to its coordinator and group_id."""
    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)
    if entry is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_entity",
            translation_placeholders={"entity_id": entity_id},
        )
    uid = entry.unique_id or ""
    if not uid.startswith("unifi_play_zone_"):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="not_a_zone_entity",
            translation_placeholders={"entity_id": entity_id},
        )
    group_id = uid[len("unifi_play_zone_") :]
    for coordinator in hass.data.get(DOMAIN, {}).values():
        if group_id in coordinator.groups:
            return coordinator, group_id
    raise ServiceValidationError(
        translation_domain=DOMAIN, translation_key="zone_not_found"
    )


def _zone_host_client(
    coordinator: UnifiPlayCoordinator, group_id: str
) -> UnifiPlayMqttClient:
    client = coordinator.get_host_mqtt_client(group_id)
    # Registered is not connected: publish_action drops commands silently
    # while the socket is down, so a zone write would report success and
    # never reach the host (#14).
    if client is None or not client.is_connected:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="zone_host_not_connected"
        )
    return client


def async_register_services(hass: HomeAssistant) -> None:
    """Register the integration's services once per Home Assistant run."""
    if hass.services.has_service(DOMAIN, "play_announcement"):
        return

    def _state(call: ServiceCall) -> UnifiPlayDeviceState:
        coordinator, dev_id = resolve_device(hass, call.data[ATTR_DEVICE_ID])
        state = (coordinator.data or {}).get(dev_id)
        if state is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="device_state_unavailable"
            )
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
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="eq_table_unavailable"
            )
        _client(hass, call).save_eq_preset(
            call.data["name"], {k: float(v) for k, v in state.eq_table.items()}
        )

    async def delete_eq_preset(call: ServiceCall) -> None:
        _client(hass, call).delete_eq_preset(call.data["name"])

    async def rename_eq_preset(call: ServiceCall) -> None:
        _client(hass, call).rename_eq_preset(call.data["name"], call.data["new_name"])

    # --- Zone management ---
    #
    # Every one of these delegates to coordinator.zones, which preflights the
    # speakers, publishes the complete document to each of them and returns a
    # result. None of them build a zone payload or reach for an MQTT client
    # themselves: that is what let six callers each get the preflight
    # slightly differently wrong.

    def _zone_device_mac(call: ServiceCall, key: str = ATTR_DEVICE_ID) -> str:
        """Resolve a Home Assistant device id to a speaker MAC."""
        coordinator, dev_id = resolve_device(hass, call.data[key])
        state = (coordinator.data or {}).get(dev_id)
        if state is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="device_state_unavailable",
            )
        return state.mac

    async def create_zone(call: ServiceCall) -> None:
        host_coordinator, host_dev_id = resolve_device(
            hass, call.data["host_device_id"]
        )
        host_state = (host_coordinator.data or {}).get(host_dev_id)
        if host_state is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="device_state_unavailable",
            )
        macs = [host_state.mac]
        for member_device_id in call.data["member_device_ids"]:
            m_coordinator, m_dev_id = resolve_device(hass, member_device_id)
            m_state = (m_coordinator.data or {}).get(m_dev_id)
            if m_state is None:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="device_state_unavailable",
                )
            macs.append(m_state.mac)
        # No host is designated at creation: the "host" key is omitted
        # entirely, not set false. The firmware elects one and writes the
        # flag back. Pre-designating a host is the one thing an app-made zone
        # never does, and the symptom is a zone every speaker agrees on that
        # only ever sounds in one room. See docs/api.md.
        host_coordinator.zones.create(name=call.data["name"], member_macs=macs)

    async def delete_zone(call: ServiceCall) -> None:
        coordinator, group_id = _resolve_zone(hass, call.data["entity_id"])
        coordinator.zones.delete(group_id)

    async def add_zone_member(call: ServiceCall) -> None:
        coordinator, group_id = _resolve_zone(hass, call.data["entity_id"])
        coordinator.zones.add_member(group_id, _zone_device_mac(call))

    async def remove_zone_member(call: ServiceCall) -> None:
        coordinator, group_id = _resolve_zone(hass, call.data["entity_id"])
        coordinator.zones.remove_member(group_id, _zone_device_mac(call))

    async def rename_zone(call: ServiceCall) -> None:
        coordinator, group_id = _resolve_zone(hass, call.data["entity_id"])
        coordinator.zones.rename(group_id, call.data["name"])

    async def set_zone_index(call: ServiceCall) -> None:
        coordinator, group_id = _resolve_zone(hass, call.data["entity_id"])
        coordinator.zones.set_index(group_id, call.data["group_index"])

    async def play_zone_announcement(call: ServiceCall) -> None:
        coordinator, group_id = _resolve_zone(hass, call.data["entity_id"])
        client = _zone_host_client(coordinator, group_id)
        raw_name = call.data["filename"]
        # normpath collapses middle-path ".." segments (a/../b → b); the
        # explicit split check catches a leading ".." that normpath cannot
        # resolve without a base (../evil stays ../evil after normpath).
        safe_name = posixpath.normpath(raw_name.lstrip("/"))
        if ".." in safe_name.split("/"):
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="filename_traversal"
            )
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
        """Stop an announcement on every member, or say which cannot be reached.

        Skipping the offline members and reporting success leaves an
        announcement audibly playing in a room the user just silenced.
        """
        coordinator, group_id = _resolve_zone(hass, call.data["entity_id"])
        members = coordinator.get_zone_members(group_id)
        offline = [
            state.device_name
            for _, state, client in members
            if client is None or not client.is_connected
        ]
        if offline:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="zone_members_offline",
                translation_placeholders={"devices": ", ".join(offline)},
            )
        for _, _, client in members:
            if client is not None:
                client.stop_announcement()

    handlers: list[tuple[str, _ServiceHandler, vol.Schema]] = [
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
