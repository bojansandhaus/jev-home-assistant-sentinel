"""Config flow for the Jev connection.

Two routes are offered and they are alternatives: hosted Jev over an OpenRouter
API key, or Laya on this machine with no key. The stored ``provider`` field
selects one. An existing entry without that field keeps the hosted route.
"""

from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback

from . import DEFAULT_PROVIDER, DOMAIN
from .runtime import LAYA_BASE_URL, LAYA_MODEL, LOCAL_PROVIDER

PROVIDER_LABELS = {
    "openrouter": "Jev over the OpenRouter API key",
    LOCAL_PROVIDER: "Laya on this machine (no API key)",
}


class JevSentinelConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return OptionsFlowHandler(config_entry)

    async def async_step_user(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get("provider", DEFAULT_PROVIDER) != LOCAL_PROVIDER:
                if not str(user_input.get("api_key", "")).strip():
                    errors["api_key"] = "api_key_required"
            if not errors:
                return self.async_create_entry(
                    title="Jev Home Sentinel", data=user_input
                )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("provider", default=DEFAULT_PROVIDER): vol.In(
                        PROVIDER_LABELS
                    ),
                    vol.Optional("api_key", default=""): str,
                    vol.Optional("laya_base_url", default=LAYA_BASE_URL): str,
                    vol.Optional("laya_model", default=LAYA_MODEL): str,
                }
            ),
            errors=errors,
            description_placeholders={
                "provider": "a hosted Jev key or a local Laya server"
            },
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
