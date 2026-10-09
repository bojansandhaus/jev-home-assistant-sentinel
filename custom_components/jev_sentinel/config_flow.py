"""Config flow for the Jev connection.

Four decision modes are offered, and each names which side leads:

- ``api_with_local_fallback``: the hosted API first, the local decision model
  behind it.
- ``api_only``: the hosted API alone. A failure is reported, never rerouted.
- ``local_only``: the local decision model alone, with no key and no fallback.
- ``local_with_api_fallback``: the local decision model first, the hosted API
  behind it.

Which hosted model answers the API side stays its own choice, so the provider
names ``openrouter`` and ``clef`` are offered next to their canonical mode, and
every name this integration shipped before the four-mode contract keeps working
as an alias. An entry that stored an arrangement name such as ``laya_local``
keeps the routing it stored, and an entry without a ``provider`` field still
keeps the hosted route.

Both sides answer with a **System One decision model**, also written "typed
decision model": a model that returns typed values, each with a probability,
rather than prose. See https://systemonemodels.org/guides/what-is-a-system-one-model/
for the category and https://systemonemodels.org/ for the ecosystem index. Jev
is one member of that category, not the name of it.

The local slot is named ``laya`` and is generic. ``local_model`` selects which
local System One decision model answers, defaults to ``laya``, and is not
validated against a list of names: any local System One decision model that
publishes the same ``/v1/systemone`` contract fits by configuration alone.
``laya_base_url`` stays its own field, so pointing the slot at a different
engine is a configuration change rather than a code change. ``laya_model`` is
kept as the deprecated spelling of ``local_model``, and ``local_model`` wins when
both are present.

Clef is the one route whose credentials do not live in this form. Its token and
its account id are read from ``CLOUDFLARE_API_TOKEN`` and
``CLOUDFLARE_ACCOUNT_ID``, because a config entry is stored on disk in plain
text. The form therefore asks only which checkpoint to call, and a missing
variable is reported at the first review, naming the variable, rather than being
asked for and stored here.
"""

from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback

from . import DEFAULT_PROVIDER, DOMAIN
from .runtime import (
    API_ONLY,
    API_WITH_LOCAL_FALLBACK,
    CANONICAL_MODES,
    CHAINED_PROVIDER,
    CLEF_CHAINED_PROVIDER,
    CLEF_CHECKPOINT_FIELD,
    CLEF_DEFAULT_MODEL,
    CLEF_MODELS,
    LAYA_BASE_URL,
    LAYA_MODEL,
    LOCAL_MODEL,
    LOCAL_MODEL_FIELD,
    LOCAL_ONLY,
    LOCAL_WITH_API_FALLBACK,
    local_checkpoint,
    pins_clef,
    resolve_provider,
)

# The four canonical modes, in the order the spec lists them. These are the
# names the form offers first and the names an entry should store; every other
# name is offered as an alias beneath them so an existing selection and the two
# spellings of the same mode stay interchangeable.
MODE_LABELS = {
    API_WITH_LOCAL_FALLBACK: ("Hosted API first, local decision model as fallback"),
    API_ONLY: "Hosted API only, no fallback",
    LOCAL_ONLY: "Local System One decision model only, no API key",
    LOCAL_WITH_API_FALLBACK: (
        "Local System One decision model first, hosted API as fallback"
    ),
}

# The local engine names are deliberately not a list to choose from. The form
# takes free text, and ``local_checkpoint`` refuses only a value that cannot be
# a model name: empty, whitespace-only, or carrying a character that would
# corrupt the JSON body or a URL path segment.
LOCAL_MODEL_HELP = (
    "laya, or the name of any other local System One decision model that"
    " speaks the same /v1/systemone contract"
)

# The canonical route names, kept selectable so an existing entry and the two
# spellings of the same mode stay interchangeable.
ALIAS_LABELS = {
    "openrouter": "Hosted route alias: openrouter (same as api_only on OpenRouter)",
    "clef": "Hosted route alias: clef (same as api_only on Clef)",
    LOCAL_ONLY: "Local route alias: laya (same as local_only)",
    API_ONLY: "Hosted mode alias: api_only",
    API_WITH_LOCAL_FALLBACK: "Hosted mode alias: api_with_local_fallback",
    LOCAL_WITH_API_FALLBACK: "Local mode alias: local_with_api_fallback",
}

# Every arrangement name this integration shipped before the four-mode contract.
# They keep working, so an entry created by an earlier release needs no
# migration, and the form still offers them.
LEGACY_MODE_LABELS = {
    "jev_api": "Legacy arrangement alias: jev_api (same as api_only on OpenRouter)",
    "laya_local": "Legacy arrangement alias: laya_local (same as local_only)",
    "laya_with_jev_fallback": (
        "Legacy arrangement alias: laya_with_jev_fallback"
        " (same as local_with_api_fallback)"
    ),
    "clef_api": "Legacy arrangement alias: clef_api (same as api_only on Clef)",
    "clef_with_jev_fallback": (
        "Legacy arrangement alias: clef_with_jev_fallback"
        " (same as local_with_api_fallback, Clef first)"
    ),
    CHAINED_PROVIDER: (
        "Legacy route alias: laya_then_hosted" " (same as local_with_api_fallback)"
    ),
    CLEF_CHAINED_PROVIDER: (
        "Legacy route alias: clef_then_jev"
        " (same as local_with_api_fallback, Clef first)"
    ),
    "clef_with_local_fallback": (
        "Hosted mode alias: clef_with_local_fallback (Clef first, local behind)"
    ),
    "auto": "Automatic: the hosted providers that are configured",
}

# The canonical modes, the route aliases, and the legacy arrangement names, in
# one selector. The four canonical modes come first because those are the names
# a new entry should store.
PROVIDER_LABELS = {
    **MODE_LABELS,
    **ALIAS_LABELS,
    **LEGACY_MODE_LABELS,
}

# Clef ships two checkpoints of one model. The checkpoint is not a provider, so
# it is its own field and the route name stays the same whichever is chosen.
CHECKPOINT_LABELS = {
    CLEF_DEFAULT_MODEL: "clef, the larger checkpoint",
    "clef-flash": "clef-flash, the smaller and faster checkpoint",
}

# The modes that reach the hosted API, so they are the modes that need
# ``api_key`` in this form. A mode that names Clef reads its own credential from
# the environment instead and is not asked for the stored OpenRouter key, and
# the local-only mode needs none.
OPENROUTER_MODES = (
    API_ONLY,
    API_WITH_LOCAL_FALLBACK,
    LOCAL_WITH_API_FALLBACK,
    "openrouter",
    "jev_api",
    "auto",
)


def _needs_api_key(provider: str) -> bool:
    """Whether the stored name reaches the hosted API over the OpenRouter key."""
    if provider in OPENROUTER_MODES:
        return True
    # A name that pins Clef first still needs the hosted key behind Clef, so it
    # is asked for one exactly as the two-hop chain always has been.
    if pins_clef(provider):
        return provider in (
            LOCAL_WITH_API_FALLBACK,
            "clef_with_jev_fallback",
            "clef_then_jev",
        )
    return False


class JevSentinelConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        # No argument. Home Assistant sets the flow's `handler`, which is the
        # entry id, after this returns, and `config_entry` is resolved from it.
        # On 2025.12 and later `OptionsFlow.__init__` takes no arguments and
        # `config_entry` is a read-only property, so passing the entry here is
        # wrong on every version the manifest admits.
        return OptionsFlowHandler()

    async def async_step_user(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            stored = str(user_input.get("provider", DEFAULT_PROVIDER))
            provider = resolve_provider(stored)
            # Every mode that reaches the hosted API needs a key: ``api_only``
            # calls it directly, and both fallback modes need it behind the
            # first hop. Clef carries its own credential from the environment,
            # and the local-only mode needs none.
            if (
                _needs_api_key(stored)
                and not str(user_input.get("api_key", "")).strip()
            ):
                errors["api_key"] = "api_key_required"
            # A local model name is free text, so it is validated here rather
            # than constrained by a list. Only an empty or unusable name is
            # refused, and it is refused before the entry is stored.
            # An untouched field must read as "not configured", or `auto` gains a
            # local hop the operator never asked for: the form pre-fills the
            # field with the default model name, and `resolve_auto_mode` treats
            # any non-None `local_model` as an explicit choice. So a value equal
            # to the default and not typed by the operator is normalised to None
            # here, and `auto` stays `api_only` exactly as it was in v1.3.0.
            # Choosing `auto` and doing nothing must not send household cases to
            # a hosted provider.
            local_model = (user_input.get(LOCAL_MODEL_FIELD) or "").strip()
            if local_model == LOCAL_MODEL:
                local_model = None
            elif local_model:
                try:
                    local_checkpoint(local_model)
                except ValueError:
                    errors[LOCAL_MODEL_FIELD] = "local_model_invalid"
            if not errors and local_model is None:
                user_input = {
                    k: v for k, v in user_input.items() if k != LOCAL_MODEL_FIELD
                }
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
                    # Empty by default: a pre-filled name is indistinguishable from an
                    # explicit choice, and `auto` reads that choice. The label
                    # still names ``laya`` as the example.
                    vol.Optional(LOCAL_MODEL_FIELD, default=""): str,
                    # Kept for entries that stored it before ``local_model``
                    # existed. ``local_model`` wins when both are present.
                    vol.Optional("laya_model", default=LAYA_MODEL): str,
                    vol.Optional(
                        CLEF_CHECKPOINT_FIELD, default=CLEF_DEFAULT_MODEL
                    ): vol.In(CHECKPOINT_LABELS),
                }
            ),
            errors=errors,
            description_placeholders={
                "provider": (
                    "a hosted API with the local model behind it, a hosted API"
                    " alone, the local decision model alone, or the local"
                    " decision model with the hosted API behind it"
                ),
                "local_model": LOCAL_MODEL_HELP,
            },
        )


class OptionsFlowHandler(config_entries.OptionsFlow):
    """The options form for an existing entry.

    There is deliberately no ``__init__`` here. `config_entry` comes from the
    base class: Home Assistant 2025.6 exposed it as a property with a setter
    marked `breaks_in_ha_version="2025.12"`, the setter was removed in
    2025.12.0, and in 2026.9.0 the attribute is a read-only property that raises
    ValueError when `hass` is not yet set. Assigning it in `__init__` therefore
    raised `AttributeError: property 'config_entry' of 'OptionsFlowHandler'
    object has no setter` the moment a user opened the options dialog, on every
    version the manifest admits (`hacs.json` declares `homeassistant: 2026.9.0`).
    `tests/test_config_flow_options.py` pins the construction against a
    stand-in base class with the same read-only shape.
    """

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({vol.Optional("shadow", default=True): bool}),
        )
