"""Config flow for UniFi Play integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .api import (
    UnifiPlayApi,
    UnifiPlayApiError,
    UnifiPlayAuthError,
    UnifiPlayForbiddenError,
    UnifiPlayServiceUnavailableError,
    UnifiPlayUnsupportedApiError,
)
from .const import (
    CONF_API_KEY,
    CONF_CONTROLLER_HOST,
    CONF_MANUAL_HOSTS,
    CONF_MODE,
    DOMAIN,
    MODE_CONSOLE,
    MODE_DIRECT,
)
from .discovery import async_discover

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
                found = await async_discover(manual_hosts=manual_hosts)
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
