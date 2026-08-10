"""Base entity for UniFi Play."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MODEL_NAMES
from .coordinator import UnifiPlayCoordinator, UnifiPlayDeviceState
from .mqtt_client import UnifiPlayMqttClient


def async_setup_platform_entities(
    coordinator: UnifiPlayCoordinator,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    factory: Callable[[str], Iterable[Entity]],
) -> None:
    """Create entities for current devices and for any discovered later.

    The coordinator re-scans every five minutes, so a device adopted (or
    powered on) after setup appears in coordinator.data mid-flight; without
    this listener its entities would only exist after a reload.
    """
    known: set[str] = set()

    def _sync() -> None:
        new_ids = [dev_id for dev_id in coordinator.data if dev_id not in known]
        if not new_ids:
            return
        known.update(new_ids)
        async_add_entities([entity for dev_id in new_ids for entity in factory(dev_id)])

    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))


class UnifiPlayEntity(CoordinatorEntity[UnifiPlayCoordinator]):
    """Base class for all UniFi Play entities."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: UnifiPlayCoordinator,
        device_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id

    @property
    def device_info(self) -> DeviceInfo:
        """Live device info: platform and firmware can arrive after creation.

        A device identified over MQTT starts as its topic root (UPL-DEVICE)
        with no firmware; the extra_info event upgrades both.
        """
        state = self._device_state
        return DeviceInfo(
            identifiers={(DOMAIN, state.mac)},
            name=state.device_name or state.name,
            manufacturer="Ubiquiti",
            model=MODEL_NAMES.get(state.platform, state.platform),
            sw_version=state.firmware,
        )

    @property
    def _device_state(self) -> UnifiPlayDeviceState:
        return self.coordinator.data[self._device_id]

    @property
    def available(self) -> bool:
        """Available only while the device has a live MQTT connection.

        Every value an entity reports arrives over MQTT, and every command
        goes back out the same way, so without a connection the state shown
        is whatever the device state was initialised with. Reporting
        unavailable makes that visible instead of showing plausible defaults
        (#15).
        """
        return self._connected_mqtt() is not None and super().available

    def _mqtt(self) -> UnifiPlayMqttClient | None:
        """The device's MQTT client, whether or not it is connected."""
        return self.coordinator.get_mqtt_client(self._device_id)

    def _connected_mqtt(self) -> UnifiPlayMqttClient | None:
        """The device's MQTT client, but only while it is actually connected.

        The coordinator registers the client before dialling out and only
        removes it once the attempt has failed, so a client object exists
        during every connect attempt and for any period the broker has
        dropped. Presence alone therefore does not mean commands will land:
        ``publish_action`` logs and returns when disconnected, which is the
        same silent no-op this was meant to end (#14).
        """
        client = self._mqtt()
        return client if client is not None and client.is_connected else None

    def _require_mqtt(self) -> UnifiPlayMqttClient:
        """The device's connected MQTT client, raising if there isn't one.

        Commands used to no-op silently when disconnected, which reported
        success to the caller and left the speaker untouched — a fault that
        surfaced only through the services, which do check (#14).
        """
        client = self._connected_mqtt()
        if client is None:
            raise HomeAssistantError(
                f"No MQTT connection to {self._device_state.device_name}. "
                "The device may be offline, or unreachable on TCP 8883."
            )
        return client

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()
