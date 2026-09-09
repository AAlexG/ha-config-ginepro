"""Config flow for the FTP integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from aioftp.errors import AIOFTPException
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .const import CONF_BACKUP_PATH, CONF_DEFAULTS, DOMAIN
from .helpers import FtpClient

_LOGGER = logging.getLogger(__name__)


class BaseFtpConfigFlow:
    async def async_check_user_input(self, user_input: Mapping[str, Any] | None) -> bool:
        """Check FTP connection with user input"""
        # Check if we can connect to the FTP server
        client = FtpClient(
            hass=self.hass,
            host=user_input[CONF_HOST],
            port=user_input[CONF_PORT],
            username=user_input[CONF_USERNAME],
            password=user_input[CONF_PASSWORD],
        )
        try:
            _LOGGER.info("Check FTP connection on %s", client)
            async with client.connect():
                pass
        except AIOFTPException:
            _LOGGER.exception("Connection error")
            self._errors["base"] = "cannot_connect"
            return False
        return True

    @staticmethod
    def _get_config_schema(defaults=None):
        """Get configuration schema"""
        defaults = defaults or CONF_DEFAULTS
        return vol.Schema(
            {
                vol.Required(CONF_HOST, default=defaults.get(CONF_HOST)): str,
                vol.Required(CONF_PORT, default=int(defaults.get(CONF_PORT))): int,
                vol.Optional(CONF_USERNAME, default=defaults.get(CONF_USERNAME)): str,
                vol.Optional(CONF_PASSWORD): TextSelector(
                    TextSelectorConfig(
                        type=TextSelectorType.PASSWORD,
                    )
                ),
                vol.Required(CONF_BACKUP_PATH, default=defaults.get(CONF_BACKUP_PATH)): str,
            }
        )


class FtpConfigFlow(BaseFtpConfigFlow, ConfigFlow, domain=DOMAIN):
    """Handle a config flow for FTP."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial step."""
        self._errors: dict[str, str] = {}
        if user_input is not None:
            # Check if we can connect to the FTP server
            if await self.async_check_user_input(user_input) and not self._errors:
                self._async_abort_entries_match(
                    {
                        CONF_HOST: user_input[CONF_HOST],
                        CONF_PORT: user_input[CONF_PORT],
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_BACKUP_PATH: user_input[CONF_BACKUP_PATH],
                    }
                )

                return self.async_create_entry(
                    title=(
                        f"{user_input[CONF_USERNAME]}@{user_input[CONF_HOST]}:"
                        f"{user_input[CONF_PORT]}/"
                        f"{user_input[CONF_BACKUP_PATH].strip('/')}"
                    ),
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user", data_schema=self._get_config_schema(), errors=self._errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> OptionsFlow:
        """Create the options flow."""
        return OptionsFlow()


class OptionsFlow(BaseFtpConfigFlow, OptionsFlow):
    """Handle a options flow for FTP."""

    @property
    def config_entry(self):
        return self.hass.config_entries.async_get_entry(self.handler)

    async def async_step_init(self, user_input: Mapping[str, Any] | None = None) -> FlowResult:
        """Manage the options."""
        self._errors: dict[str, str] = {}
        if user_input:
            if not user_input.get(CONF_PASSWORD):
                _LOGGER.debug("User do not enter password, keep current configured one")
                user_input[CONF_PASSWORD] = self.config_entry.data.get(CONF_PASSWORD)
            if await self.async_check_user_input(user_input) and not self._errors:
                # update config entry
                self.hass.config_entries.async_update_entry(self.config_entry, data=user_input)
                # Finish
                return self.async_create_entry(data=None)

        return self.async_show_form(
            step_id="init",
            data_schema=self._get_config_schema(self.config_entry.data),
            errors=self._errors,
        )
