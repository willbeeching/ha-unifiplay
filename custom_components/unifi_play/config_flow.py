"""Config flow for UniFi Play integration."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr, selector

from .api import (
    UnifiPlayApi,
    UnifiPlayApiError,
    UnifiPlayAuthError,
    UnifiPlayForbiddenError,
    UnifiPlayServiceUnavailableError,
    UnifiPlayUnsupportedApiError,
)
from .const import (
    BROADCASTING_MODE_LABELS,
    BROADCASTING_MODE_REVERSE,
    BROADCASTING_MODE_ZONE_ONLY,
    CONF_API_KEY,
    CONF_CONTROLLER_HOST,
    CONF_MANUAL_HOSTS,
    CONF_MODE,
    DOMAIN,
    MODE_CONSOLE,
    MODE_DIRECT,
    broadcast_input_label,
    broadcast_input_labels,
    source_value,
)
from .coordinator import UnifiPlayCoordinator
from .discovery import async_resolve_direct
from .helpers import (
    dev_info_entry,
    mac_normalise,
    move_zone_to_new_host,
    resolve_device,
)

_LOGGER = logging.getLogger(__name__)

STEP_CONSOLE_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CONTROLLER_HOST): str,
        vol.Required(CONF_API_KEY): str,
    }
)

STEP_DIRECT_DATA_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_MANUAL_HOSTS, default=""): str,
    }
)


def _is_ui_cloud_host(host: str) -> bool:
    """Return True for Ubiquiti cloud hosts such as api.ui.com.

    The Site Manager cloud API answers a JSON 404 at the Apollo path, which
    would otherwise be misreported as "Apollo answered but has no device
    API". Only a console's own address can serve /proxy/apollo.
    """
    host = host.lower()
    return host == "ui.com" or host.endswith(".ui.com")


def _parse_manual_hosts(raw: str) -> list[str]:
    """Split a comma/space separated list of IPs or hostnames."""
    return [h for h in raw.replace(",", " ").split() if h]


class UnifiPlayConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for UniFi Play."""

    VERSION = 1

    @classmethod
    @callback
    def async_get_options_flow(cls, config_entry: ConfigEntry) -> OptionsFlow:
        return UnifiPlayOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose between console-backed and direct (console-less) setup."""
        return self.async_show_menu(
            step_id="user",
            menu_options=[MODE_CONSOLE, MODE_DIRECT],
        )

    async def async_step_console(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set up through a UniFi OS console's Apollo API."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_CONTROLLER_HOST]
            api_key = user_input[CONF_API_KEY]

            api = UnifiPlayApi(host, api_key)
            normalized_host = api.host

            if _is_ui_cloud_host(normalized_host):
                await api.close()
                return self.async_show_form(
                    step_id="console",
                    data_schema=STEP_CONSOLE_DATA_SCHEMA,
                    errors={"base": "cloud_host"},
                )

            await self.async_set_unique_id(normalized_host)
            self._abort_if_unique_id_configured()

            try:
                # An empty device list is not a setup failure: the API
                # answered, so host and key are good. api.validate_connection
                # logs a warning and we create the entry anyway, so adding the
                # integration before adopting hardware still works.
                await api.validate_connection()
            except UnifiPlayAuthError:
                errors["base"] = "invalid_auth"
            except UnifiPlayForbiddenError:
                errors["base"] = "forbidden"
            except UnifiPlayServiceUnavailableError:
                errors["base"] = "apollo_unavailable"
            except UnifiPlayUnsupportedApiError:
                errors["base"] = "unsupported_api"
            except UnifiPlayApiError as err:
                _LOGGER.warning(
                    "UniFi Play setup failed for controller %s: %s. "
                    "Enable debug logging for this integration and retry to "
                    "capture request details in the Home Assistant logs.",
                    normalized_host,
                    err,
                )
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception(
                    "Unexpected error during UniFi Play setup for controller %s",
                    normalized_host,
                )
                errors["base"] = "unknown"
            finally:
                await api.close()

            if not errors:
                return self.async_create_entry(
                    title=f"UniFi Play ({normalized_host})",
                    data={
                        CONF_MODE: MODE_CONSOLE,
                        CONF_CONTROLLER_HOST: normalized_host,
                        CONF_API_KEY: api_key,
                    },
                )

        return self.async_show_form(
            step_id="console",
            data_schema=STEP_CONSOLE_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_direct(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set up without a console, using UDP discovery + direct MQTT.

        A broadcast probe finds Play devices on Home Assistant's own subnet;
        devices on other VLANs can be listed by IP and are probed unicast.
        This is the path for consoles whose model has no Apollo application.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            manual_hosts = _parse_manual_hosts(user_input.get(CONF_MANUAL_HOSTS, ""))

            await self.async_set_unique_id(MODE_DIRECT)
            self._abort_if_unique_id_configured()

            try:
                found = await async_resolve_direct(manual_hosts=manual_hosts)
            except OSError:
                _LOGGER.exception("UniFi Play direct discovery failed")
                found = []
                errors["base"] = "discovery_failed"

            if not errors:
                if found:
                    return self.async_create_entry(
                        title="UniFi Play (Direct)",
                        data={
                            CONF_MODE: MODE_DIRECT,
                            CONF_MANUAL_HOSTS: manual_hosts,
                        },
                    )
                # Nothing answered. Distinguish "typed IPs are wrong or
                # firewalled" from "nothing on this subnet" for the message.
                errors["base"] = "no_response" if manual_hosts else "no_devices_found"

        return self.async_show_form(
            step_id="direct",
            data_schema=STEP_DIRECT_DATA_SCHEMA,
            errors=errors,
        )


class UnifiPlayOptionsFlow(OptionsFlow):
    """Zone management via the integration's Configure button.

    Exposes create / rename / add-member / remove-member / delete operations
    as a multi-step UI that matches HA's native config-flow look and feel.
    All mutations are sent to the device via MQTT; no config-entry data is
    actually modified, so the integration is never reloaded by this flow.
    """

    def __init__(self) -> None:
        self._selected_zone_id: str | None = None

    @property
    def _coordinator(self) -> UnifiPlayCoordinator:
        return self.hass.data[DOMAIN][self.config_entry.entry_id]

    def _build_device_options(
        self,
        exclude_macs: set[str] | None = None,
    ) -> list[selector.SelectOptionDict]:
        """Build a SelectOptionDict list for available Play devices.

        Devices whose MAC is in exclude_macs are omitted entirely. Callers pass
        exclude_macs already normalised, and registry identifiers are NOT
        guaranteed to be (they are stored as the device reported them - see
        entity.py - which is why resolve_device normalises defensively), so the
        identifier has to be normalised here too or the comparison silently
        never matches and occupied devices stay selectable.
        """
        device_reg = dr.async_get(self.hass)
        options: list[selector.SelectOptionDict] = []
        for dev_entry in dr.async_entries_for_config_entry(device_reg, self.config_entry.entry_id):
            mac: str | None = None
            for domain, ident_val in dev_entry.identifiers:
                # The zone_ check runs on the raw value: normalising first
                # would upper-case the prefix and stop matching.
                if domain == DOMAIN and isinstance(ident_val, str) and not ident_val.startswith("zone_"):
                    mac = mac_normalise(ident_val)
                    break
            if mac is None:
                continue
            if exclude_macs and mac in exclude_macs:
                continue
            name = dev_entry.name_by_user or dev_entry.name or mac
            options.append(selector.SelectOptionDict(value=dev_entry.id, label=name))
        return sorted(options, key=lambda o: o["label"])

    def _occupied_zone(
        self,
        mac: str,
        *,
        allow_hosting: bool = False,
    ) -> str | None:
        """Return the group_id of the zone occupying this device, else None.

        Returns the group_id rather than the display name because names are
        neither unique nor guaranteed non-empty (``name`` defaults to "" in
        UnifiPlayGroupState.from_mqtt). Keying on the name would let a device
        in one zone be added to a different zone that happens to share its
        name, and would make an unnamed zone read as "not occupied".

        If allow_hosting is True, appearances as a zone host (siblings) are
        not considered occupied — a device can host more than one zone.
        """
        for gs in self._coordinator.groups.values():
            for dev in gs.dev_info:
                if mac_normalise(dev.get("mac", "")) != mac:
                    continue
                if allow_hosting and dev.get("host"):
                    continue
                return gs.group_id
        return None

    # ── Top-level menu ───────────────────────────────────────────────────────

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options={
                "create_zone": "Create a new zone",
                "select_zone": "Modify or delete a zone",
            },
        )

    # ── Create ───────────────────────────────────────────────────────────────

    async def async_step_create_zone(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            coordinator = self._coordinator
            device_ids: list[str] = user_input.get("device_ids") or []
            if isinstance(device_ids, str):
                device_ids = [device_ids]

            if len(device_ids) < 2:
                errors["device_ids"] = "zone_needs_two_devices"
            else:
                # First selected device becomes the internal zone host (protocol
                # requirement); the rest are members. This distinction is not
                # exposed to the user — any speaker can host.
                host_dev_id = device_ids[0]
                member_ids = device_ids[1:]

                try:
                    host_coordinator, host_internal_id = resolve_device(self.hass, host_dev_id)
                    host_state = (host_coordinator.data or {}).get(host_internal_id)
                    if host_state is None:
                        raise Exception("Speaker data not yet available")
                    host_client = host_coordinator.get_mqtt_client(host_internal_id)
                except Exception:
                    _LOGGER.exception("Failed to resolve speaker %s", host_dev_id)
                    errors["base"] = "resolve_failed"
                else:
                    if host_client is None:
                        errors["base"] = "no_mqtt"
                    else:
                        host_mac = mac_normalise(host_state.mac)
                        occupied = self._occupied_zone(host_mac, allow_hosting=True)
                        if occupied is None:
                            for mid in member_ids:
                                try:
                                    m_coord, m_internal = resolve_device(self.hass, mid)
                                    m_state = (m_coord.data or {}).get(m_internal)
                                    if m_state:
                                        occupied = self._occupied_zone(mac_normalise(m_state.mac))
                                        if occupied is not None:
                                            break
                                except Exception:
                                    _LOGGER.debug(
                                        "Could not resolve %s while checking zone "
                                        "occupancy; treating as unoccupied", mid,
                                        exc_info=True,
                                    )
                        if occupied is not None:
                            errors["base"] = "device_in_other_zone"

                    if not errors:
                        dev_info = [dev_info_entry(host_state, host=True)]
                        for mid in member_ids:
                            try:
                                m_coord, m_internal = resolve_device(self.hass, mid)
                                m_state = (m_coord.data or {}).get(m_internal)
                                if m_state and mac_normalise(m_state.mac) != host_mac:
                                    dev_info.append(dev_info_entry(m_state))
                            except Exception:
                                _LOGGER.warning(
                                    "Skipping speaker %s during zone creation", mid, exc_info=True
                                )
                        _LOGGER.debug(
                            "create_zone: host=%s known_groups=%d dev_info=%s",
                            host_mac,
                            len(host_coordinator.groups),
                            [d.get("mac") for d in dev_info],
                        )
                        host_coordinator.update_zone(
                            group_id=str(uuid.uuid4()),
                            name=user_input["name"],
                            dev_info=dev_info,
                        )
                        return await self.async_step_init()

        occupied_macs = {
            mac_normalise(dev.get("mac", ""))
            for gs in self._coordinator.groups.values()
            for dev in gs.dev_info
            if dev.get("mac")
        }
        device_options = self._build_device_options(exclude_macs=occupied_macs)
        # An empty or single-entry picker renders a form that can never be
        # submitted, so say why instead of showing a dead dialog.
        if len(device_options) < 2 and not errors:
            return self.async_abort(reason="not_enough_devices")
        return self.async_show_form(
            step_id="create_zone",
            data_schema=vol.Schema(
                {
                    vol.Required("name"): selector.TextSelector(),
                    vol.Required("device_ids"): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=device_options, multiple=True)
                    ),
                }
            ),
            errors=errors,
        )

    # ── Zone selector (shared by modify + delete paths) ──────────────────────

    async def async_step_select_zone(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        zones = self._coordinator.groups
        if not zones:
            return self.async_abort(reason="no_zones")

        if user_input is not None:
            self._selected_zone_id = user_input["zone_id"]
            return await self.async_step_zone_action()

        options = [
            selector.SelectOptionDict(value=gid, label=gs.name)
            for gid, gs in zones.items()
        ]
        return self.async_show_form(
            step_id="select_zone",
            data_schema=vol.Schema(
                {
                    vol.Required("zone_id"): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=options)
                    )
                }
            ),
        )

    # ── Action menu (after a zone is selected) ────────────────────────────────

    async def async_step_zone_action(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="zone_action",
            menu_options={
                "rename_zone": "Rename this zone",
                "add_zone_member": "Add a device to this zone",
                "remove_zone_member": "Remove a device from this zone",
                "set_zone_source": "Set audio source (streaming / broadcast wired)",
                "set_zone_broadcasting": "Set stream broadcasting",
                "reorder_zone": "Change zone display order",
                "delete_zone": "Delete this zone",
                "select_zone": "← Select a different zone",
                "init": "← Back to main menu",
            },
        )

    # ── Rename ───────────────────────────────────────────────────────────────

    async def async_step_rename_zone(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        coordinator = self._coordinator
        gs = coordinator.groups.get(self._selected_zone_id or "")
        if gs is None:
            return self.async_abort(reason="zone_gone")

        if user_input is not None:
            client = coordinator.get_host_mqtt_client(self._selected_zone_id)
            if client is None:
                errors["base"] = "no_mqtt"
            else:
                coordinator.update_zone(
                    group_id=gs.group_id,
                    name=user_input["name"],
                    dev_info=gs.dev_info,
                    group_index=gs.group_index,
                    broadcasting_mode=gs.broadcasting_mode,
                    wb_enable=gs.wb_enable,
                    wb_device=gs.wb_device,
                    wb_input=gs.wb_input,
                )
                return await self.async_step_zone_action()

        return self.async_show_form(
            step_id="rename_zone",
            description_placeholders={"zone_name": gs.name},
            data_schema=vol.Schema(
                {vol.Required("name", default=gs.name): selector.TextSelector()}
            ),
            errors=errors,
        )

    # ── Add member ───────────────────────────────────────────────────────────

    async def async_step_add_zone_member(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        coordinator = self._coordinator
        gs = coordinator.groups.get(self._selected_zone_id or "")
        if gs is None:
            return self.async_abort(reason="zone_gone")

        if user_input is not None:
            try:
                m_coord, m_internal = resolve_device(self.hass, user_input["device_id"])
                m_state = m_coord.data[m_internal]
            except Exception:
                _LOGGER.exception("Failed to resolve member device %s", user_input.get("device_id"))
                errors["base"] = "resolve_failed"
            else:
                m_mac = mac_normalise(m_state.mac)
                if any(mac_normalise(d.get("mac", "")) == m_mac for d in gs.dev_info):
                    errors["base"] = "already_member"
                else:
                    occupied = self._occupied_zone(m_mac)
                    if occupied is not None and occupied != gs.group_id:
                        errors["base"] = "device_in_other_zone"
                    else:
                        client = coordinator.get_host_mqtt_client(self._selected_zone_id)
                        if client is None:
                            errors["base"] = "no_mqtt"
                        else:
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
                            return await self.async_step_zone_action()

        # Exclude all speakers already in any zone.
        occupied_macs = {
            mac_normalise(dev.get("mac", ""))
            for other_gs in self._coordinator.groups.values()
            for dev in other_gs.dev_info
            if dev.get("mac")
        }
        device_options = self._build_device_options(exclude_macs=occupied_macs)
        if not device_options and not errors:
            return self.async_abort(reason="no_available_devices")
        return self.async_show_form(
            step_id="add_zone_member",
            description_placeholders={"zone_name": gs.name},
            data_schema=vol.Schema(
                {
                    vol.Required("device_id"): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=device_options)
                    )
                }
            ),
            errors=errors,
        )

    # ── Remove member ─────────────────────────────────────────────────────────

    async def async_step_remove_zone_member(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        coordinator = self._coordinator
        gs = coordinator.groups.get(self._selected_zone_id or "")
        if gs is None:
            return self.async_abort(reason="zone_gone")

        # Any device can be removed, including the one currently hosting: the
        # host is an internal protocol role, not something the user picked, so
        # removing it hands the role to another member instead of refusing.
        removable = list(gs.dev_info)

        if user_input is not None:
            target_mac = user_input["member_mac"]
            new_dev_info = [
                dict(d) for d in gs.dev_info
                if mac_normalise(d.get("mac", "")) != mac_normalise(target_mac)
            ]
            removing_host = mac_normalise(target_mac) == mac_normalise(gs.host_mac)
            if len(new_dev_info) == len(gs.dev_info):
                errors["base"] = "not_a_member"
            elif len(new_dev_info) < 2:
                errors["base"] = "zone_needs_two_devices"
            elif not removing_host:
                client = coordinator.get_host_mqtt_client(self._selected_zone_id)
                if client is None:
                    errors["base"] = "no_mqtt"
                else:
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
                    return await self.async_step_zone_action()
            else:
                # Removing the device that hosts: hand the role to another
                # member rather than refusing. Shared with the service of the
                # same name so the two cannot drift apart.
                old_host_mac = gs.host_mac
                try:
                    move_zone_to_new_host(coordinator, gs, new_dev_info, target_mac)
                except ServiceValidationError:
                    errors["base"] = "no_mqtt"
                else:
                    _LOGGER.debug(
                        "remove_zone_member: zone %s host %s -> %s",
                        gs.group_id, old_host_mac, new_dev_info[0].get("mac", ""),
                    )
                    return await self.async_step_zone_action()

        options = [
            selector.SelectOptionDict(value=d["mac"], label=d.get("name", d["mac"]))
            for d in removable
        ]
        return self.async_show_form(
            step_id="remove_zone_member",
            description_placeholders={"zone_name": gs.name},
            data_schema=vol.Schema(
                {
                    vol.Required("member_mac"): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=options)
                    )
                }
            ),
            errors=errors,
        )

    # ── Delete ───────────────────────────────────────────────────────────────

    async def async_step_delete_zone(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        coordinator = self._coordinator
        gs = coordinator.groups.get(self._selected_zone_id or "")
        if gs is None:
            return self.async_abort(reason="zone_gone")

        if user_input is not None:
            client = coordinator.get_host_mqtt_client(self._selected_zone_id)
            if client is None:
                errors["base"] = "no_mqtt"
            else:
                coordinator.delete_zone(self._selected_zone_id)
                self._selected_zone_id = None
                return await self.async_step_init()

        return self.async_show_form(
            step_id="delete_zone",
            description_placeholders={"zone_name": gs.name},
            data_schema=vol.Schema({}),
            errors=errors,
        )

    # ── Set audio source ─────────────────────────────────────────────────────

    async def async_step_set_zone_source(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        coordinator = self._coordinator
        gs = coordinator.groups.get(self._selected_zone_id or "")
        if gs is None:
            return self.async_abort(reason="zone_gone")

        if user_input is not None:
            if user_input["source_mode"] == "streaming":
                client = coordinator.get_host_mqtt_client(self._selected_zone_id)
                if client is None:
                    errors["base"] = "no_mqtt"
                else:
                    coordinator.update_zone(
                        group_id=gs.group_id,
                        name=gs.name,
                        dev_info=gs.dev_info,
                        group_index=gs.group_index,
                        broadcasting_mode=gs.broadcasting_mode,
                        wb_enable=False,
                        wb_device="",
                        wb_input="",
                    )
                    # Hand the input back on whichever device was broadcasting,
                    # not the host — they are frequently not the same device.
                    # Offline is not an error here: the zone is already off the
                    # wired source, and the device will be on streaming anyway
                    # once it reconnects to a zone that has none.
                    if gs.wb_enable:
                        prev = coordinator.get_mqtt_client_for_mac(
                            gs.wb_device or gs.host_mac
                        )
                        if prev is not None:
                            prev.set_source("streaming")
                        else:
                            _LOGGER.debug(
                                "Zone %s: previous broadcast device %s is offline; "
                                "leaving its input alone",
                                gs.name, gs.wb_device or gs.host_mac,
                            )
                    return await self.async_step_zone_action()
            else:
                return await self.async_step_set_zone_wb_device()

        current_mode = "broadcast" if gs.wb_enable else "streaming"
        return self.async_show_form(
            step_id="set_zone_source",
            description_placeholders={"zone_name": gs.name},
            data_schema=vol.Schema(
                {
                    vol.Required("source_mode", default=current_mode): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(value="streaming", label="Streaming"),
                                selector.SelectOptionDict(value="broadcast", label="Broadcast wired source"),
                            ]
                        )
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_set_zone_wb_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        coordinator = self._coordinator
        gs = coordinator.groups.get(self._selected_zone_id or "")
        if gs is None:
            return self.async_abort(reason="zone_gone")

        # MAC -> platform for the speakers in this zone. Input names differ by
        # hardware, so the chosen label can only be resolved once we know
        # which speaker is going to broadcast.
        member_platforms: dict[str, str] = {}
        for state in (coordinator.data or {}).values():
            mac = mac_normalise(state.mac)
            if any(mac_normalise(d.get("mac", "")) == mac for d in gs.dev_info):
                member_platforms[mac] = state.platform

        if user_input is not None:
            source_mac = mac_normalise(user_input["source_device"])
            platform = member_platforms.get(source_mac, "")
            wb_input = source_value(platform, user_input["input_type"])
            client = coordinator.get_host_mqtt_client(self._selected_zone_id)
            # The zone write goes to the host (it owns the zone), but the
            # input switch has to go to the DEVICE THAT WILL BROADCAST, which
            # is often not the host. set_group would send both to the host,
            # switching the wrong device's input and leaving the chosen one
            # untouched — so the two are published separately here.
            source_client = coordinator.get_mqtt_client_for_mac(
                user_input["source_device"]
            )
            if wb_input is None:
                # The picker offers the union across the zone, so an input can
                # be valid for one device and absent on another.
                errors["input_type"] = "input_not_on_device"
            elif client is None or source_client is None:
                errors["base"] = "no_mqtt"
            else:
                coordinator.update_zone(
                    group_id=gs.group_id,
                    name=gs.name,
                    dev_info=gs.dev_info,
                    group_index=gs.group_index,
                    broadcasting_mode=gs.broadcasting_mode,
                    wb_enable=True,
                    wb_device=user_input["source_device"],
                    wb_input=wb_input,
                )
                source_client.set_source(wb_input)
                return await self.async_step_zone_action()

        # The option value is the MAC exactly as the device reported it, not a
        # normalised copy: it goes on the wire as wb_device, and every other
        # MAC in the payload (dev_info, the wb_device the device echoes back)
        # is raw. Normalisation happens only for comparisons, below.
        device_options = [
            selector.SelectOptionDict(
                value=d["mac"],
                label=d.get("name", d["mac"]),
            )
            for d in gs.dev_info
            if d.get("mac")
        ]
        # The default has to match one of the option VALUES above, so it is
        # resolved back to the raw MAC rather than compared normalised.
        wanted = mac_normalise(gs.wb_device or gs.host_mac)
        default_device = next(
            (o["value"] for o in device_options if mac_normalise(o["value"]) == wanted),
            device_options[0]["value"] if device_options else "",
        )
        # Union of every member's broadcastable inputs; validated on submit
        # against whichever speaker was picked.
        input_labels: list[str] = []
        for platform in member_platforms.values():
            for label in broadcast_input_labels(platform).values():
                if label not in input_labels:
                    input_labels.append(label)
        current_input_label = broadcast_input_label(
            member_platforms.get(default_device, ""), gs.wb_input
        )
        if current_input_label not in input_labels:
            current_input_label = input_labels[0] if input_labels else "Line In"
        input_options = [
            selector.SelectOptionDict(value=label, label=label)
            for label in input_labels
        ]
        return self.async_show_form(
            step_id="set_zone_wb_device",
            description_placeholders={"zone_name": gs.name},
            data_schema=vol.Schema(
                {
                    vol.Required("source_device", default=default_device): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=device_options)
                    ),
                    vol.Required("input_type", default=current_input_label): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=input_options)
                    ),
                }
            ),
            errors=errors,
        )

    # ── Stream broadcasting ──────────────────────────────────────────────────

    async def async_step_set_zone_broadcasting(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        coordinator = self._coordinator
        gs = coordinator.groups.get(self._selected_zone_id or "")
        if gs is None:
            return self.async_abort(reason="zone_gone")

        if user_input is not None:
            mode = BROADCASTING_MODE_REVERSE.get(user_input["broadcasting_mode"])
            client = coordinator.get_host_mqtt_client(self._selected_zone_id)
            if client is None:
                errors["base"] = "no_mqtt"
            elif mode is None:
                errors["broadcasting_mode"] = "resolve_failed"
            else:
                # update_group, not set_group: only the advertising mode
                # changes, so the host's physical input is left untouched.
                coordinator.update_zone(
                    group_id=gs.group_id,
                    name=gs.name,
                    dev_info=gs.dev_info,
                    group_index=gs.group_index,
                    broadcasting_mode=mode,
                    wb_enable=gs.wb_enable,
                    wb_device=gs.wb_device,
                    wb_input=gs.wb_input,
                )
                return await self.async_step_zone_action()

        current = BROADCASTING_MODE_LABELS.get(
            gs.broadcasting_mode, BROADCASTING_MODE_LABELS[BROADCASTING_MODE_ZONE_ONLY]
        )
        return self.async_show_form(
            step_id="set_zone_broadcasting",
            description_placeholders={"zone_name": gs.name},
            data_schema=vol.Schema(
                {
                    vol.Required("broadcasting_mode", default=current): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(value=label, label=label)
                                for label in BROADCASTING_MODE_LABELS.values()
                            ]
                        )
                    )
                }
            ),
            errors=errors,
        )

    # ── Reorder (group_index) ────────────────────────────────────────────────

    async def async_step_reorder_zone(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        coordinator = self._coordinator
        gs = coordinator.groups.get(self._selected_zone_id or "")
        if gs is None:
            return self.async_abort(reason="zone_gone")

        if user_input is not None:
            client = coordinator.get_host_mqtt_client(self._selected_zone_id)
            if client is None:
                errors["base"] = "no_mqtt"
            else:
                coordinator.update_zone(
                    group_id=gs.group_id,
                    name=gs.name,
                    dev_info=gs.dev_info,
                    group_index=user_input["group_index"],
                    broadcasting_mode=gs.broadcasting_mode,
                    wb_enable=gs.wb_enable,
                    wb_device=gs.wb_device,
                    wb_input=gs.wb_input,
                )
                return await self.async_step_zone_action()

        return self.async_show_form(
            step_id="reorder_zone",
            description_placeholders={"zone_name": gs.name},
            data_schema=vol.Schema(
                {
                    vol.Required("group_index", default=gs.group_index): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=0, max=99, mode=selector.NumberSelectorMode.BOX)
                    )
                }
            ),
            errors=errors,
        )
