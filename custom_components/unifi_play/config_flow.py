"""Config flow for UniFi Play integration."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    UnifiPlayApi,
    UnifiPlayApiError,
    UnifiPlayAuthError,
    UnifiPlayCertificateError,
    UnifiPlayForbiddenError,
    UnifiPlayServiceUnavailableError,
    UnifiPlayTransientError,
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
    CONF_VERIFY_SSL,
    DOMAIN,
    MODE_CONSOLE,
    MODE_DIRECT,
    broadcast_input_label,
    broadcast_input_labels,
    source_value,
)
from .coordinator import UnifiPlayCoordinator, UnifiPlayGroupState
from .discovery import async_resolve_direct
from .helpers import mac_normalise, resolve_device
from .zone_writer import ZoneWriteResult

_LOGGER = logging.getLogger(__name__)

# Verification is offered on by default. Most consoles will refuse it - they
# present a certificate for a name nobody connects by - and the flow says so
# and offers the box. That costs one extra submit and makes handing an API key
# to an unverified endpoint a decision somebody made rather than one the
# integration made for them.
STEP_CONSOLE_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CONTROLLER_HOST): str,
        vol.Required(CONF_API_KEY): str,
        vol.Required(CONF_VERIFY_SSL, default=True): bool,
    }
)

STEP_DIRECT_DATA_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_MANUAL_HOSTS, default=""): str,
    }
)

# Reauth asks for the credential only: the host identifies the entry, and a
# different host is a different console.
STEP_REAUTH_DATA_SCHEMA = vol.Schema({vol.Required(CONF_API_KEY): str})

# Reconfigure is the other way round: the address can move, and the key is
# optional because a console that has been re-addressed usually still has the
# same one. Leaving it blank keeps the stored credential.
STEP_RECONFIGURE_CONSOLE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CONTROLLER_HOST): str,
        vol.Optional(CONF_API_KEY): str,
        vol.Required(CONF_VERIFY_SSL, default=False): bool,
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


def _entry_already_covering(
    hass: HomeAssistant, devices: list[dict[str, Any]]
) -> str | None:
    """Return the title of an existing entry already managing these devices.

    Two entries cannot share hardware. Every entity's unique ID is built from
    the device MAC and is not namespaced per entry, so a second entry covering
    the same speaker produces a full set of colliding unique IDs; Home
    Assistant rejects the later ones outright and the entry appears to create
    nothing. Worse, which entry loses is a startup race, and a rejected entity
    keeps its registry row while having no object behind it - so it reads
    ``unavailable`` forever, with the only clue buried in the log.

    The per-mode unique IDs set by each step (console host, and the literal
    "direct") cannot catch this: a console entry and a direct entry have
    genuinely different identities and still reach the same speakers. Overlap
    is only visible once the hardware has actually been discovered, which is
    why this runs after validation rather than at the top of the step.

    Only loaded entries can be checked, since an entry that failed to set up
    has no coordinator and no known devices. That is the right bias: an entry
    which is not running is not claiming anything.

    Returns the title rather than the entry so that a coordinator whose entry
    has somehow gone from the registry still blocks setup; returning None there
    would let the duplicate through on the one path where state is already
    inconsistent.
    """
    macs = {mac_normalise(d.get("mac", "")) for d in devices if d.get("mac")}
    if not macs:
        return None
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        coordinator = entry.runtime_data
        covered = {
            mac_normalise(state.mac) for state in coordinator.data.values() if state.mac
        }
        if covered & macs:
            return entry.title
    return None


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

    async def _async_validate_console(
        self, host: str, api_key: str, verify_ssl: bool
    ) -> tuple[str, list[dict[str, Any]], dict[str, str]]:
        """Probe a console. Returns (normalised host, devices, errors).

        Shared by the setup step and the reauth step so the two cannot answer
        the same failure differently - a reauth that reported "cannot connect"
        where setup said "invalid API key" would send the user looking at
        their network instead of their credential.

        Uses Home Assistant's shared aiohttp session, so there is nothing to
        close on any path out of here, including the ones that raise.
        """
        errors: dict[str, str] = {}
        api = UnifiPlayApi(
            host, api_key, async_get_clientsession(self.hass, verify_ssl=verify_ssl)
        )
        normalized_host = api.host
        devices: list[dict[str, Any]] = []

        if _is_ui_cloud_host(normalized_host):
            return normalized_host, devices, {"base": "cloud_host"}

        try:
            # An empty device list is not a setup failure: the API answered,
            # so host and key are good. api.validate_connection logs a warning
            # and the entry is created anyway, so adding the integration
            # before adopting hardware still works.
            devices = await api.validate_connection()
        except UnifiPlayAuthError:
            errors["base"] = "invalid_auth"
        except UnifiPlayForbiddenError:
            errors["base"] = "forbidden"
        except UnifiPlayServiceUnavailableError:
            errors["base"] = "apollo_unavailable"
        except UnifiPlayUnsupportedApiError:
            errors["base"] = "unsupported_api"
        except UnifiPlayTransientError:
            errors["base"] = "console_busy"
        except UnifiPlayCertificateError:
            # Not a network fault, and the form already carries the box that
            # settles it, so this says which box rather than "cannot connect".
            errors["base"] = "certificate_untrusted"
        except UnifiPlayApiError as err:
            # Deliberately not %s of the exception at warning level with the
            # host: the message can carry a response body. The host is safe
            # and is what the user needs to check.
            _LOGGER.warning(
                "UniFi Play setup failed for controller %s: %s. "
                "Enable debug logging for this integration and retry to "
                "capture request details in the Home Assistant logs.",
                normalized_host,
                type(err).__name__,
            )
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception(
                "Unexpected error during UniFi Play setup for controller %s",
                normalized_host,
            )
            errors["base"] = "unknown"
        return normalized_host, devices, errors

    async def async_step_console(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set up through a UniFi OS console's Apollo API."""
        errors: dict[str, str] = {}

        if user_input is not None:
            verify_ssl = user_input[CONF_VERIFY_SSL]
            normalized_host, devices, errors = await self._async_validate_console(
                user_input[CONF_CONTROLLER_HOST], user_input[CONF_API_KEY], verify_ssl
            )
            if not errors:
                await self.async_set_unique_id(normalized_host)
                self._abort_if_unique_id_configured()
                if existing := _entry_already_covering(self.hass, devices):
                    return self.async_abort(
                        reason="already_configured_device",
                        description_placeholders={"entry": existing},
                    )
                return self.async_create_entry(
                    title=f"UniFi Play ({normalized_host})",
                    data={
                        CONF_MODE: MODE_CONSOLE,
                        CONF_CONTROLLER_HOST: normalized_host,
                        CONF_API_KEY: user_input[CONF_API_KEY],
                        CONF_VERIFY_SSL: verify_ssl,
                    },
                )

        return self.async_show_form(
            step_id="console",
            data_schema=self.add_suggested_values_to_schema(
                STEP_CONSOLE_DATA_SCHEMA, user_input or {}
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change where an existing entry looks, without losing its entities.

        A console entry can move to a different address - a console rebuilt
        on a new IP, or a DHCP reservation that changed - and a direct entry
        can gain or lose manually listed hosts. Removing and re-adding does
        the same thing at the cost of every entity ID, every dashboard card
        and every automation, because the entry is what owns them.

        The credential is *not* changed here: a rejected key has its own flow
        (reauth), which Home Assistant starts on its own when the console
        stops accepting it.
        """
        entry = self._get_reconfigure_entry()
        if entry.data.get(CONF_MODE, MODE_CONSOLE) == MODE_DIRECT:
            return await self.async_step_reconfigure_direct()
        return await self.async_step_reconfigure_console()

    async def async_step_reconfigure_console(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Point a console entry at a different console address."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_CONTROLLER_HOST]
            api_key = user_input.get(CONF_API_KEY) or entry.data[CONF_API_KEY]
            verify_ssl = user_input[CONF_VERIFY_SSL]
            normalized_host, _devices, errors = await self._async_validate_console(
                host, api_key, verify_ssl
            )
            if not errors:
                # The unique ID is the console address, so moving the entry
                # moves its identity with it. What must not happen is two
                # entries landing on one console: their speakers overlap, and
                # every entity ID the second would mint is already taken.
                if any(
                    other.unique_id == normalized_host
                    and other.entry_id != entry.entry_id
                    for other in self._async_current_entries()
                ):
                    return self.async_abort(reason="already_configured_console")
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=normalized_host,
                    title=f"UniFi Play ({normalized_host})",
                    data_updates={
                        CONF_MODE: MODE_CONSOLE,
                        CONF_CONTROLLER_HOST: normalized_host,
                        CONF_API_KEY: api_key,
                        CONF_VERIFY_SSL: verify_ssl,
                    },
                )

        return self.async_show_form(
            step_id="reconfigure_console",
            data_schema=self.add_suggested_values_to_schema(
                STEP_RECONFIGURE_CONSOLE_SCHEMA,
                {
                    CONF_CONTROLLER_HOST: entry.data.get(CONF_CONTROLLER_HOST, ""),
                    # Entries predating this option were all set up without
                    # verification, so the box shows what the entry is doing
                    # rather than what a new entry would be offered.
                    CONF_VERIFY_SSL: entry.data.get(CONF_VERIFY_SSL, False),
                },
            ),
            errors=errors,
        )

    async def async_step_reconfigure_direct(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the manually listed hosts on a direct entry.

        Speakers on Home Assistant's own subnet are found by broadcast and
        need no entry here; this list is for the ones on another VLAN, and
        for Audio Port hardware, which does not answer the UDP probe at all
        (#5) and can only be reached by being named.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            manual_hosts = _parse_manual_hosts(user_input.get(CONF_MANUAL_HOSTS, ""))
            try:
                found = await async_resolve_direct(manual_hosts=manual_hosts)
            except OSError:
                _LOGGER.exception("UniFi Play direct discovery failed")
                found = []
                errors["base"] = "discovery_failed"

            if not errors:
                if not found:
                    # Saving a list that reaches nothing would silently
                    # disconnect every speaker on the next reload.
                    errors["base"] = (
                        "no_response" if manual_hosts else "no_devices_found"
                    )
                else:
                    return self.async_update_reload_and_abort(
                        entry, data_updates={CONF_MANUAL_HOSTS: manual_hosts}
                    )

        return self.async_show_form(
            step_id="reconfigure_direct",
            data_schema=self.add_suggested_values_to_schema(
                STEP_DIRECT_DATA_SCHEMA,
                {
                    CONF_MANUAL_HOSTS: ", ".join(
                        entry.data.get(CONF_MANUAL_HOSTS, []) or []
                    )
                },
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """A console entry's API key stopped being accepted.

        Reached when the coordinator raises ConfigEntryAuthFailed, which
        happens on a 401 or a 403: the key has been revoked, rotated, or the
        console it was made on has been rebuilt. Only console entries have a
        credential, so only they can land here.
        """
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Take a replacement API key for an existing console entry."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            # The host is not asked for again: a new host is a different
            # console and a different entry. Reconfigure changes the host.
            host = entry.data[CONF_CONTROLLER_HOST]
            _normalized, _devices, errors = await self._async_validate_console(
                host,
                user_input[CONF_API_KEY],
                entry.data.get(CONF_VERIFY_SSL, False),
            )
            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={CONF_API_KEY: user_input[CONF_API_KEY]},
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_DATA_SCHEMA,
            description_placeholders={"host": entry.data.get(CONF_CONTROLLER_HOST, "")},
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
                    if existing := _entry_already_covering(self.hass, found):
                        return self.async_abort(
                            reason="already_configured_device",
                            description_placeholders={"entry": existing},
                        )
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

    Create, rename, reorder, add or remove a member, change the source, set
    stream broadcasting, delete. Every one of them is a call to
    ``coordinator.zones``, which preflights the speakers, publishes the
    complete document to each of them and refuses rather than write to some.
    Nothing here builds a zone payload or reaches for an MQTT client itself.

    No config-entry data is modified, so the integration is never reloaded by
    this flow: the changes live on the speakers.
    """

    def __init__(self) -> None:
        self._selected_zone_id: str | None = None

    @property
    def _coordinator(self) -> UnifiPlayCoordinator:
        coordinator: UnifiPlayCoordinator = self.config_entry.runtime_data
        return coordinator

    # ── Running a mutation ───────────────────────────────────────────────────

    def _run(
        self, action: Callable[[], ZoneWriteResult]
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Perform a zone write, turning a refusal into a form error.

        The write path raises translated errors naming exactly what is wrong -
        which speaker is offline, which zone already has it. Those messages
        are worth showing, so the error key comes from the exception rather
        than being flattened into one "something went wrong" for every cause.
        """
        try:
            action()
        except (ServiceValidationError, HomeAssistantError) as err:
            key = getattr(err, "translation_key", None) or "zone_write_failed"
            placeholders = getattr(err, "translation_placeholders", None) or {}
            _LOGGER.debug("Zone write refused: %s %s", key, placeholders)
            return {"base": key}, dict(placeholders)
        return {}, {}

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
        for dev_entry in dr.async_entries_for_config_entry(
            device_reg, self.config_entry.entry_id
        ):
            mac: str | None = None
            for domain, ident_val in dev_entry.identifiers:
                # The zone_ check runs on the raw value: normalising first
                # would upper-case the prefix and stop matching.
                if (
                    domain == DOMAIN
                    and isinstance(ident_val, str)
                    and not ident_val.startswith("zone_")
                ):
                    mac = mac_normalise(ident_val)
                    break
            if mac is None:
                continue
            if exclude_macs and mac in exclude_macs:
                continue
            name = dev_entry.name_by_user or dev_entry.name or mac
            options.append(selector.SelectOptionDict(value=dev_entry.id, label=name))
        return sorted(options, key=lambda o: o["label"])

    def _mac_for_device_id(self, device_id: str) -> str | None:
        """Resolve a Home Assistant device id to a speaker MAC."""
        try:
            _coordinator, _internal_id, state = resolve_device(self.hass, device_id)
        except ServiceValidationError:
            _LOGGER.debug("Could not resolve speaker %s", device_id)
            return None
        return state.mac

    def _occupied_macs(self) -> set[str]:
        """Every speaker currently in any zone this coordinator knows about."""
        return {
            mac_normalise(dev.get("mac", ""))
            for gs in self._coordinator.groups.values()
            for dev in gs.dev_info
            if dev.get("mac")
        }

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
        placeholders: dict[str, str] = {}

        if user_input is not None:
            # A multi-select returns a list, but a selector rendered with a
            # single option has been seen to return the bare string.
            raw_ids: list[str] | str = user_input.get("device_ids") or []
            device_ids: list[str] = [raw_ids] if isinstance(raw_ids, str) else raw_ids

            macs: list[str] = []
            for device_id in device_ids:
                mac = self._mac_for_device_id(device_id)
                if mac is None:
                    errors["base"] = "resolve_failed"
                    break
                macs.append(mac)

            if not errors:
                # The order the user picked is preserved but carries no
                # meaning: the firmware elects the host itself, and naming
                # one produces a zone that registers everywhere and only
                # sounds in one room. See docs/api.md.
                errors, placeholders = self._run(
                    lambda: self._coordinator.zones.create(
                        name=user_input["name"], member_macs=macs
                    )
                )
                if not errors:
                    return await self.async_step_init()

        device_options = self._build_device_options(exclude_macs=self._occupied_macs())
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
                        selector.SelectSelectorConfig(
                            options=device_options, multiple=True
                        )
                    ),
                }
            ),
            errors=errors,
            description_placeholders=placeholders or None,
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

    def _selected_zone(self) -> UnifiPlayGroupState | None:
        return self._coordinator.groups.get(self._selected_zone_id or "")

    # ── Rename ───────────────────────────────────────────────────────────────

    async def async_step_rename_zone(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        gs = self._selected_zone()
        if gs is None:
            return self.async_abort(reason="zone_gone")
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}

        if user_input is not None:
            errors, placeholders = self._run(
                lambda: self._coordinator.zones.rename(gs.group_id, user_input["name"])
            )
            if not errors:
                return await self.async_step_zone_action()

        return self.async_show_form(
            step_id="rename_zone",
            description_placeholders={"zone_name": gs.name, **placeholders},
            data_schema=vol.Schema(
                {vol.Required("name", default=gs.name): selector.TextSelector()}
            ),
            errors=errors,
        )

    # ── Add member ───────────────────────────────────────────────────────────

    async def async_step_add_zone_member(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        gs = self._selected_zone()
        if gs is None:
            return self.async_abort(reason="zone_gone")
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}

        if user_input is not None:
            mac = self._mac_for_device_id(user_input["device_id"])
            if mac is None:
                errors["base"] = "resolve_failed"
            else:
                errors, placeholders = self._run(
                    lambda: self._coordinator.zones.add_member(gs.group_id, mac)
                )
                if not errors:
                    return await self.async_step_zone_action()

        # Exclude every speaker already in any zone.
        device_options = self._build_device_options(exclude_macs=self._occupied_macs())
        if not device_options and not errors:
            return self.async_abort(reason="no_available_devices")
        return self.async_show_form(
            step_id="add_zone_member",
            description_placeholders={"zone_name": gs.name, **placeholders},
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
        gs = self._selected_zone()
        if gs is None:
            return self.async_abort(reason="zone_gone")
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}

        if user_input is not None:
            # Any speaker can be removed, including the one currently
            # hosting: the host is an internal protocol role, not something
            # the user chose, so removing it hands the role over rather than
            # refusing.
            errors, placeholders = self._run(
                lambda: self._coordinator.zones.remove_member(
                    gs.group_id, user_input["member_mac"]
                )
            )
            if not errors:
                return await self.async_step_zone_action()

        options = [
            selector.SelectOptionDict(value=d["mac"], label=d.get("name", d["mac"]))
            for d in gs.dev_info
            if d.get("mac")
        ]
        return self.async_show_form(
            step_id="remove_zone_member",
            description_placeholders={"zone_name": gs.name, **placeholders},
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
        gs = self._selected_zone()
        if gs is None:
            return self.async_abort(reason="zone_gone")
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}

        if user_input is not None:
            errors, placeholders = self._run(
                lambda: self._coordinator.zones.delete(gs.group_id)
            )
            if not errors:
                self._selected_zone_id = None
                return await self.async_step_init()

        return self.async_show_form(
            step_id="delete_zone",
            description_placeholders={"zone_name": gs.name, **placeholders},
            data_schema=vol.Schema({}),
            errors=errors,
        )

    # ── Set audio source ─────────────────────────────────────────────────────

    async def async_step_set_zone_source(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        gs = self._selected_zone()
        if gs is None:
            return self.async_abort(reason="zone_gone")
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}

        if user_input is not None:
            if user_input["source_mode"] == "streaming":
                errors, placeholders = self._run(
                    lambda: self._coordinator.zones.clear_broadcast_source(gs.group_id)
                )
                if not errors:
                    return await self.async_step_zone_action()
            else:
                return await self.async_step_set_zone_wb_device()

        current_mode = "broadcast" if gs.wb_enable else "streaming"
        return self.async_show_form(
            step_id="set_zone_source",
            description_placeholders={"zone_name": gs.name, **placeholders},
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "source_mode", default=current_mode
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value="streaming", label="Streaming"
                                ),
                                selector.SelectOptionDict(
                                    value="broadcast", label="Broadcast wired source"
                                ),
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
        gs = self._selected_zone()
        if gs is None:
            return self.async_abort(reason="zone_gone")
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        coordinator = self._coordinator

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
            wb_input = source_value(
                member_platforms.get(source_mac, ""), user_input["input_type"]
            )
            if wb_input is None:
                # The picker offers the union across the zone, so an input can
                # be valid for one speaker and absent on another.
                errors["input_type"] = "input_not_on_device"
            else:
                errors, placeholders = self._run(
                    lambda: coordinator.zones.set_broadcast_source(
                        gs.group_id,
                        source_mac=user_input["source_device"],
                        wb_input=wb_input,
                    )
                )
                if not errors:
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
            member_platforms.get(mac_normalise(default_device), ""), gs.wb_input
        )
        if current_input_label not in input_labels:
            current_input_label = input_labels[0] if input_labels else "Line In"
        input_options = [
            selector.SelectOptionDict(value=label, label=label)
            for label in input_labels
        ]
        return self.async_show_form(
            step_id="set_zone_wb_device",
            description_placeholders={"zone_name": gs.name, **placeholders},
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "source_device", default=default_device
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=device_options)
                    ),
                    vol.Required(
                        "input_type", default=current_input_label
                    ): selector.SelectSelector(
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
        gs = self._selected_zone()
        if gs is None:
            return self.async_abort(reason="zone_gone")
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}

        if user_input is not None:
            mode = BROADCASTING_MODE_REVERSE.get(user_input["broadcasting_mode"])
            if mode is None:
                errors["broadcasting_mode"] = "resolve_failed"
            else:
                # Only the zone's advertising mode changes here, so no
                # speaker's physical input is touched.
                errors, placeholders = self._run(
                    lambda: self._coordinator.zones.set_broadcasting_mode(
                        gs.group_id, mode
                    )
                )
                if not errors:
                    return await self.async_step_zone_action()

        current = BROADCASTING_MODE_LABELS.get(
            gs.broadcasting_mode, BROADCASTING_MODE_LABELS[BROADCASTING_MODE_ZONE_ONLY]
        )
        return self.async_show_form(
            step_id="set_zone_broadcasting",
            description_placeholders={"zone_name": gs.name, **placeholders},
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "broadcasting_mode", default=current
                    ): selector.SelectSelector(
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
        gs = self._selected_zone()
        if gs is None:
            return self.async_abort(reason="zone_gone")
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}

        if user_input is not None:
            errors, placeholders = self._run(
                lambda: self._coordinator.zones.set_index(
                    gs.group_id, int(user_input["group_index"])
                )
            )
            if not errors:
                return await self.async_step_zone_action()

        return self.async_show_form(
            step_id="reorder_zone",
            description_placeholders={"zone_name": gs.name, **placeholders},
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "group_index", default=gs.group_index
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0, max=99, mode=selector.NumberSelectorMode.BOX
                        )
                    )
                }
            ),
            errors=errors,
        )
