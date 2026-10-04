"""Home Assistant entry point for Jev Home Assistant Sentinel."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall

from .runtime import (
    CLEF_CHECKPOINT_FIELD,
    CLEF_DEFAULT_MODEL,
    LAYA_BASE_URL,
    LAYA_MODEL,
    LOCAL_MODEL_FIELD,
    LOCAL_ONLY,
    Case,
    SentinelWorkflow,
    build_provider,
    pins_clef,
    redact,
    resolve_provider,
)
from .runtime import verify as verify_observation

DOMAIN = "jev_sentinel"
PLATFORMS = ["sensor"]
DEFAULT_PROVIDER = "openrouter"


def _provider_for(entry: ConfigEntry):
    """Build the configured mode.

    One of the four canonical modes, or any name this repository shipped before
    the four-mode contract: ``jev_api``, ``laya_local``,
    ``laya_with_jev_fallback``, ``clef_api``, ``clef_with_jev_fallback``, and the
    canonical route names behind them. An entry without a ``provider`` field
    keeps the hosted route.

    ``api_key`` is the stored OpenRouter key and belongs to the hosted Jev hops.
    Clef reads its own token and account id from the environment, so the stored
    key is not forwarded to it: a config entry holds one credential, and sending
    it to a second provider would put it somewhere it was never meant to reach.

    The stored name is inspected before it is resolved, because resolution
    collapses a Clef-pinned name and a Jev-pinned name onto one canonical mode,
    and the difference decides which credential the entry may contribute.
    ``local_model`` selects which local decision model answers and takes
    precedence over the deprecated ``laya_model`` when it is set.
    """
    stored = str(entry.data.get("provider", DEFAULT_PROVIDER))
    provider = resolve_provider(stored)
    stored_key = entry.data.get("api_key")
    if pins_clef(stored):
        stored_key = None
    local_model = entry.data.get(LOCAL_MODEL_FIELD)
    return build_provider(
        # The stored name is passed, not the resolved mode. Both are accepted by
        # ``build_provider``, but only the stored name still says which hosted
        # provider leads, so a Clef entry keeps routing to Clef instead of
        # falling back to whatever the hosted order happens to offer first.
        stored,
        # The stored key belongs to the hosted hops. The local hop stays keyless
        # unless the local server was started with its own bearer check, in which
        # case LAYA_API_KEY supplies it.
        api_key=None if provider == LOCAL_ONLY else stored_key,
        laya_base_url=entry.data.get("laya_base_url") or LAYA_BASE_URL,
        # ``local_model`` is the current setting and ``laya_model`` is the
        # deprecated spelling; the engine is a checkpoint of the local slot, so
        # neither changes which provider answers.
        local_model=local_model,
        laya_model=entry.data.get("laya_model") or LAYA_MODEL,
        # The Clef checkpoint is a setting of the route, so an entry that never
        # set one keeps the default rather than calling an unknown checkpoint.
        clef_model=entry.data.get(CLEF_CHECKPOINT_FIELD) or CLEF_DEFAULT_MODEL,
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        **entry.data,
        "shadow": entry.options.get("shadow", True),
    }
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def review(call: ServiceCall) -> None:
        case = Case.create(
            call.data.get("event_type", "manual_review"),
            area=call.data.get("area"),
            entities=call.data.get("entities", []),
            facts=call.data.get("facts", {}),
            requested_action=call.data.get("requested_action"),
        )
        provider = _provider_for(entry)
        workflow = SentinelWorkflow(provider)
        decision = await hass.async_add_executor_job(workflow.review, case)
        hass.bus.async_fire(
            f"{DOMAIN}_decision",
            redact({"case": case.to_dict(), "decision": decision.to_dict()}),
        )

    async def verify(call: ServiceCall) -> None:
        expected = call.data.get("expected")
        actual = call.data.get("actual")
        result = verify_observation(
            expected, actual, available=call.data.get("available", True)
        )
        hass.bus.async_fire(f"{DOMAIN}_verification", result)

    if not hass.services.has_service(DOMAIN, "review"):
        hass.services.async_register(DOMAIN, "review", review)
        hass.services.async_register(DOMAIN, "verify", verify)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if unload_ok and not hass.data.get(DOMAIN):
        hass.services.async_remove(DOMAIN, "review")
        hass.services.async_remove(DOMAIN, "verify")
        hass.data.pop(DOMAIN, None)
    return unload_ok
