"""Services for UniFi Play.

These cover device features that have no natural Home Assistant entity:
firing an announcement, and CRUD over alarms, quiet-hours windows and custom
EQ presets. Everything here maps to an MQTT action captured from the official
app - see the repository docs for the protocol.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .coordinator import UnifiPlayCoordinator
from .mqtt_client import UnifiPlayMqttClient

ATTR_DEVICE_ID = "device_id"

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


def _resolve(hass: HomeAssistant, device_id: str) -> tuple[UnifiPlayCoordinator, str]:
    """Map a Home Assistant device id to its coordinator and internal id.

    Entities key off the device's MAC, which is also its registry identifier,
    so the lookup goes registry entry -> identifier -> coordinator state.
    """
    entry = dr.async_get(hass).async_get(device_id)
    if entry is None:
        raise ServiceValidationError(f"Unknown device: {device_id}")
    macs = {ident[1] for ident in entry.identifiers if ident[0] == DOMAIN}
    if not macs:
        raise ServiceValidationError(f"Device {device_id} is not a UniFi Play device")
    for coordinator in hass.data.get(DOMAIN, {}).values():
        for dev_id, state in coordinator.data.items():
            if state.mac in macs:
                return coordinator, dev_id
    raise ServiceValidationError(f"No live UniFi Play device for {device_id}")


def _client(hass: HomeAssistant, call: ServiceCall) -> UnifiPlayMqttClient:
    coordinator, dev_id = _resolve(hass, call.data[ATTR_DEVICE_ID])
    client = coordinator.get_mqtt_client(dev_id)
    if client is None:
        raise ServiceValidationError("No MQTT connection to that device")
    return client


def async_register_services(hass: HomeAssistant) -> None:
    """Register the integration's services once per Home Assistant run."""
    if hass.services.has_service(DOMAIN, "play_announcement"):
        return

    def _state(call: ServiceCall):
        coordinator, dev_id = _resolve(hass, call.data[ATTR_DEVICE_ID])
        return coordinator.data[dev_id]

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
    ]
    for name, handler, schema in handlers:
        hass.services.async_register(DOMAIN, name, handler, schema=schema)
