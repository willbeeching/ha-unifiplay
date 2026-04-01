"""Config flow for UniFi Play integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .api import UnifiPlayApi, UnifiPlayApiError, UnifiPlayAuthError
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

            await self.async_set_unique_id(host)
            self._abort_if_unique_id_configured()

            api = UnifiPlayApi(host, api_key)
            try:
                if not await api.validate_connection():
                    errors["base"] = "cannot_connect"
            except UnifiPlayAuthError:
                errors["base"] = "invalid_auth"
            except UnifiPlayApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during config")
                errors["base"] = "unknown"
            finally:
                await api.close()

            if not errors:
                return self.async_create_entry(
                    title=f"UniFi Play ({host})",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )
