"""Binary sensor platform for UniFi Play devices."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, broadcast_input_label
from .coordinator import UnifiPlayCoordinator
from .entity import UnifiPlayEntity, async_setup_platform_entities
from .helpers import mac_normalise


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up UniFi Play binary sensor entities."""
    coordinator: UnifiPlayCoordinator = hass.data[DOMAIN][entry.entry_id]

    def _factory(device_id: str) -> list[BinarySensorEntity]:
        return [
            UnifiPlayAdminLockSensor(coordinator, device_id),
            UnifiPlayAnnouncingSensor(coordinator, device_id),
            UnifiPlayInZoneSensor(coordinator, device_id),
        ]

    async_setup_platform_entities(coordinator, entry, async_add_entities, _factory)

    # Dynamic zone-level sensors — one per zone, created/cleaned up as zones
    # appear and disappear, mirroring the pattern in media_player._sync_zones.
    known_zone_sensors: dict[str, UnifiPlayZoneWidebandSensor] = {}

    @callback
    def _sync_zone_sensors() -> None:
        active = set(coordinator.groups)

        # Drop tracking entries for zones that no longer exist.
        # The actual entities are cleaned up when _sync_zones removes the zone
        # device from the device registry (all entities on the device go with it).
        for gid in list(known_zone_sensors):
            if gid not in active:
                known_zone_sensors.pop(gid)

        new_ids = [gid for gid in active if gid not in known_zone_sensors]
        if not new_ids:
            return
        new_entities = [UnifiPlayZoneWidebandSensor(coordinator, gid) for gid in new_ids]
        for e in new_entities:
            known_zone_sensors[e.zone_group_id] = e
        async_add_entities(new_entities)

    _sync_zone_sensors()
    entry.async_on_unload(coordinator.async_add_listener(_sync_zone_sensors))


# ── Per-physical-device binary sensors ───────────────────────────────────────

class UnifiPlayAdminLockSensor(UnifiPlayEntity, BinarySensorEntity):
    """Reports whether the device's Admin Lock is engaged.

    Read-only on purpose: enabling/disabling requires the PIN handshake, and
    the lock only gates the official app's settings UI - device commands are
    accepted either way - so Home Assistant just surfaces the state.
    """

    _attr_name = "Admin Lock"
    _attr_device_class = BinarySensorDeviceClass.LOCK

    def __init__(
        self,
        coordinator: UnifiPlayCoordinator,
        device_id: str,
    ) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"unifi_play_{self._device_state.mac}_admin_lock"

    @property
    def is_on(self) -> bool:
        # device_class LOCK: on means UNLOCKED.
        return not self._device_state.locked


class UnifiPlayAnnouncingSensor(UnifiPlayEntity, BinarySensorEntity):
    """On while the device is playing an announcement.

    The device reports ``announcing`` plus the clip length and the schedule
    name in its info events, and pauses whatever was streaming for the
    duration.
    """

    _attr_name = "Announcing"
    _attr_icon = "mdi:bullhorn"

    def __init__(
        self,
        coordinator: UnifiPlayCoordinator,
        device_id: str,
    ) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"unifi_play_{self._device_state.mac}_announcing"

    @property
    def is_on(self) -> bool:
        return self._device_state.announcing

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        state = self._device_state
        return {
            "type": state.announcing_type or None,
            "length": state.announce_length or None,
            "schedule_name": state.announce_name or None,
            "alert_volume": state.temp_volume,
        }


class UnifiPlayInZoneSensor(UnifiPlayEntity, BinarySensorEntity):
    """On when the device is participating in a zone (as host or member).

    Useful as an automation condition: "don't change this speaker's source
    while it's grouped with others."  The ``hosting_group_id`` attribute
    is set when this device is the zone host so automations can look up
    the zone entity if needed.
    """

    _attr_name = "In Zone"
    _attr_icon = "mdi:speaker-multiple"

    def __init__(
        self,
        coordinator: UnifiPlayCoordinator,
        device_id: str,
    ) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"unifi_play_{self._device_state.mac}_in_zone"

    @property
    def is_on(self) -> bool:
        ds = self._device_state
        return bool(ds.sync_devices or ds.hosting_group)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        ds = self._device_state
        attrs: dict[str, object] = {}
        if ds.hosting_group:
            attrs["hosting_group_id"] = ds.hosting_group
        return attrs


# ── Per-zone binary sensors ───────────────────────────────────────────────────

class UnifiPlayZoneWidebandSensor(
    CoordinatorEntity[UnifiPlayCoordinator], BinarySensorEntity
):
    """On when the zone is broadcasting a wired source.

    Any device in the zone can broadcast its physical input (Line In,
    S/PDIF / eARC, USB) to all other zone members.  This sensor makes it
    easy to trigger automations on that transition without needing
    ``state_attr`` template calls.

    The device protocol calls this "wideband" (the ``wb_*`` fields), which
    is why the unique ID keeps that suffix; user-facing text uses the
    Play app's own wording, "broadcast wired source".
    """

    _attr_has_entity_name = True
    _attr_name = "Broadcast Wired Source Active"
    _attr_icon = "mdi:broadcast"

    def __init__(self, coordinator: UnifiPlayCoordinator, group_id: str) -> None:
        super().__init__(coordinator)
        self._group_id = group_id
        # Keeps the "_wideband" suffix: changing a unique_id orphans the
        # entity in the registry for anyone who already has this sensor.
        self._attr_unique_id = f"unifi_play_zone_{group_id}_wideband"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"zone_{group_id}")},
        )

    @property
    def zone_group_id(self) -> str:
        return self._group_id

    @property
    def _group(self):
        return self.coordinator.groups.get(self._group_id)

    @property
    def available(self) -> bool:
        return self._group is not None

    @property
    def is_on(self) -> bool:
        gs = self._group
        return bool(gs and gs.wb_enable)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        gs = self._group
        if not gs or not gs.wb_enable:
            return {}
        # The label depends on the broadcasting speaker's hardware, so resolve
        # wb_input against that device rather than a platform-blind map.
        platform = ""
        target = mac_normalise(gs.wb_device or gs.host_mac)
        for state in (self.coordinator.data or {}).values():
            if mac_normalise(state.mac) == target:
                platform = state.platform
                break
        return {
            "wb_input": gs.wb_input,
            "source_label": broadcast_input_label(platform, gs.wb_input),
            "wb_device_mac": gs.wb_device,
        }
