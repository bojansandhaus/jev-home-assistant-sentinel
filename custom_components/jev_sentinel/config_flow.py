"""Config flow for the Jev connection.

Three routes are offered, named the way the DOGA fork names them. The hosted
route and the local route are alternatives: ``jev_api`` is hosted Jev over an
OpenRouter API key, and ``laya_local`` is Laya on this machine with no key. The
third, ``laya_with_jev_fallback``, answers from the local server first and falls
through to the hosted key. The canonical route names ``openrouter``,
``laya``, and ``laya_then_hosted`` are accepted as aliases and are stored as
they were before, so an existing entry keeps the route it already had and an
entry without a ``provider`` field still keeps the hosted route.
"""

from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback

from . import DEFAULT_PROVIDER, DOMAIN
from .runtime import (
    CHAINED_PROVIDER,
    LAYA_BASE_URL,
    LAYA_MODEL,
    LOCAL_PROVIDER,
    resolve_provider,
)

# The three named arrangements, in the DOGA fork's vocabulary.
MODE_LABELS = {
    "jev_api": "Jev over the OpenRouter API key (hosted route)",
    "laya_local": "Laya on this machine, no API key (local route)",
    "laya_with_jev_fallback": (
        "Laya first, with the hosted key as fallback (chained route)"
    ),
}

# The canonical route names, kept selectable so an existing entry and the two
# spellings of the same route stay interchangeable.
ALIAS_LABELS = {
    "openrouter": "Hosted route alias: openrouter (same as jev_api)",
    LOCAL_PROVIDER: "Local route alias: laya (same as laya_local)",
    CHAINED_PROVIDER: "Chained route alias: laya_then_hosted",
}

PROVIDER_LABELS = {**MODE_LABELS, **ALIAS_LABELS}


class JevSentinelConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return OptionsFlowHandler(config_entry)

    async def async_step_user(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            provider = resolve_provider(user_input.get("provider", DEFAULT_PROVIDER))
            # Both hosted routes need a key: the hosted route calls it directly,
            # and the chained route needs it behind the local hop.
            if provider != LOCAL_PROVIDER:
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
                "provider": (
                    "a hosted Jev key, a local Laya server,"
                    " or Laya first with the hosted key as fallback"
                )
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
