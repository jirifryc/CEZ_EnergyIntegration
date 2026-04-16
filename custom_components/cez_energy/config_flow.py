"""Config Flow for ČEZ Energy integration.

Collects ČEZ credentials and electrometer ID, validates login to both
PND and DIP portals, then creates the config entry.  Historical data
import runs in the background after setup completes.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .const import (
    DOMAIN,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_ELECTROMETER_ID,
)
from .rest_client.dip_client import CezDistribuceRestClient
from .rest_client.pnd_client import CezPndRestClient

_LOGGER = logging.getLogger(__name__)

DATA_SCHEMA = vol.Schema({
    vol.Required(CONF_USERNAME): str,
    vol.Required(CONF_PASSWORD): str,
    vol.Required(CONF_ELECTROMETER_ID): str,
})


async def _validate_credentials(
    hass: HomeAssistant,
    username: str,
    password: str,
    electrometer_id: str,
) -> Optional[str]:
    """Validate credentials against both portals. Returns None on success, error code otherwise."""

    def _blocking() -> Optional[str]:
        try:
            dip = CezDistribuceRestClient()
            dip.login(username, password)
            dip.common_header()
        except Exception as e:
            msg = str(e).lower()
            _LOGGER.warning("DIP login validation failed: %s", e)
            if "401" in msg or "invalid" in msg or "unauthor" in msg:
                return "invalid_auth"
            return "cannot_connect"

        try:
            pnd = CezPndRestClient()
            pnd.login(username, password)
        except Exception as e:
            _LOGGER.warning("PND login validation failed: %s", e)
            return "cannot_connect"

        return None

    return await hass.async_add_executor_job(_blocking)


class CezEnergyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        errors: Dict[str, str] = {}

        if user_input is not None:
            username = user_input.get(CONF_USERNAME, "").strip()
            password = user_input.get(CONF_PASSWORD, "")
            electrometer_id = user_input.get(CONF_ELECTROMETER_ID, "").strip()

            error = await _validate_credentials(
                self.hass, username, password, electrometer_id
            )
            if error is None:
                await self.async_set_unique_id(f"{username}_{electrometer_id}")
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=f"ČEZ Energy ({electrometer_id})",
                    data={
                        CONF_USERNAME: username,
                        CONF_PASSWORD: password,
                        CONF_ELECTROMETER_ID: electrometer_id,
                    },
                )
            errors["base"] = error

        return self.async_show_form(
            step_id="user", data_schema=DATA_SCHEMA, errors=errors
        )
