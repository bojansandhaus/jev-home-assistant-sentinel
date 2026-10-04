"""Config flow for the Jev connection.

Routes are offered, named the way the DOGA fork names them. ``jev_api`` is hosted
Jev over an OpenRouter API key, ``laya_local`` is Laya on this machine with no
key, and ``laya_with_jev_fallback`` answers from the local server first and falls
through to the hosted key. ``clef_api`` is Cloudflare Clef, and
``clef_with_jev_fallback`` calls Clef first and falls through to hosted Jev.

Clef is the one route whose credentials do not live in this form. Its token and
its account id are read from ``CLOUDFLARE_API_TOKEN`` and
``CLOUDFLARE_ACCOUNT_ID``, because a config entry is stored on disk in plain
text. The form therefore asks only which checkpoint to call, and a missing
variable is reported at the first review, naming the variable, rather than being
asked for and stored here.

The canonical route names ``openrouter``, ``laya``, ``laya_then_hosted``,
``clef``, and ``clef_then_jev`` are accepted as aliases and are stored as they
were before, so an existing entry keeps the route it already had and an entry
without a ``provider`` field still keeps the hosted route.
"""

from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback

from . import DEFAULT_PROVIDER, DOMAIN
from .runtime import (
    CHAINED_PROVIDER,
    CLEF_CHAINED_PROVIDER,
    CLEF_CHECKPOINT_FIELD,
    CLEF_DEFAULT_MODEL,
    CLEF_MODELS,
    LAYA_BASE_URL,
    LAYA_MODEL,
    LOCAL_PROVIDER,
    resolve_provider,
)

# The named arrangements, in the DOGA fork's vocabulary.
MODE_LABELS = {
    "jev_api": "Jev over the OpenRouter API key (hosted route)",
    "laya_local": "Laya on this machine, no API key (local route)",
    "laya_with_jev_fallback": (
        "Laya first, with the hosted key as fallback (chained route)"
    ),
    "clef_api": "Cloudflare Clef over a Workers AI token (hosted route)",
    "clef_with_jev_fallback": (
        "Cloudflare Clef first, hosted Jev as fallback (chained route)"
    ),
}

# The canonical route names, kept selectable so an existing entry and the two
# spellings of the same route stay interchangeable.
ALIAS_LABELS = {
    "openrouter": "Hosted route alias: openrouter (same as jev_api)",
    LOCAL_PROVIDER: "Local route alias: laya (same as laya_local)",
    CHAINED_PROVIDER: "Chained route alias: laya_then_hosted",
    "clef": "Hosted route alias: clef (same as clef_api)",
    CLEF_CHAINED_PROVIDER: "Chained route alias: clef_then_jev",
}

PROVIDER_LABELS = {**MODE_LABELS, **ALIAS_LABELS}

# Clef ships two checkpoints of one model. The checkpoint is not a provider, so
# it is its own field and the route name stays the same whichever is chosen.
CHECKPOINT_LABELS = {
    CLEF_DEFAULT_MODEL: "clef, the larger checkpoint",
    "clef-flash": "clef-flash, the smaller and faster checkpoint",
}

# The routes that reach hosted Jev over the OpenRouter key, so they are the
# routes that need ``api_key`` in this form. Clef reads its own credential from
# the environment instead, and the local route needs no key.
OPENROUTER_ROUTES = ("openrouter", CHAINED_PROVIDER, CLEF_CHAINED_PROVIDER)


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
            # Every route that reaches hosted Jev needs a key: the hosted route
            # calls it directly, and both chained routes need it behind the
            # first hop. Clef carries its own credential from the environment,
            # and the local route needs none.
            if provider in OPENROUTER_ROUTES:
                if not str(user_input.get("api_key", "")).strip():
                    errors["api_key"] = "api_key_required"
            if not errors:
                return self.async_create_entry(
                    title="Jev Home Assistant Sentinel", data=user_input
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
                    vol.Optional(
                        CLEF_CHECKPOINT_FIELD, default=CLEF_DEFAULT_MODEL
                    ): vol.In(CHECKPOINT_LABELS),
                }
            ),
            errors=errors,
            description_placeholders={
                "provider": (
                    "a hosted Jev key, a local Laya server,"
                    " Laya first with the hosted key as fallback,"
                    " Cloudflare Clef, or Clef first with the hosted key"
                    " as fallback"
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
