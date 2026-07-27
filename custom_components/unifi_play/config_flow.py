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
)
from .const import CONF_API_KEY, CONF_CONTROLLER_HOST, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CONTROLLER_HOST): str,
        vol.Required(CONF_API_KEY): str,
    }
)


class UnifiPlayConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for UniFi Play."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_CONTROLLER_HOST]
            api_key = user_input[CONF_API_KEY]

            api = UnifiPlayApi(host, api_key)
            normalized_host = api.host

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
                        CONF_CONTROLLER_HOST: normalized_host,
                        CONF_API_KEY: api_key,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )
