"""Config flow for the optional OpenRouter Jev connection."""

from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback

from . import DOMAIN


class JevSentinelConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return OptionsFlowHandler(config_entry)

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="Jev Home Sentinel", data=user_input)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required("api_key"): str}),
            description_placeholders={"provider": "OpenRouter Jev"},
        )


class OptionsFlowHandler(config_entries.OptionsFlow):
    def __init__(self, config_entry):
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({vol.Optional("shadow", default=True): bool}),
        )
