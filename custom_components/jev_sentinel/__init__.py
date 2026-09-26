"""Home Assistant entry point for Jev Home Assistant Sentinel."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall

from .runtime import (
    LAYA_BASE_URL,
    LAYA_MODEL,
    LOCAL_PROVIDER,
    Case,
    SentinelWorkflow,
    build_provider,
    redact,
)
from .runtime import verify as verify_observation

DOMAIN = "jev_sentinel"
PLATFORMS = ["sensor"]
DEFAULT_PROVIDER = "openrouter"


def _provider_for(entry: ConfigEntry):
    """Build the configured route: hosted Jev over a key, or Laya locally."""
    provider = entry.data.get("provider", DEFAULT_PROVIDER)
    return build_provider(
        provider,
        # Only the hosted route carries a stored key. The local route stays
        # keyless unless laya-serve was started with its own bearer check, in
        # which case LAYA_API_KEY supplies it.
        api_key=None if provider == LOCAL_PROVIDER else entry.data.get("api_key"),
        laya_base_url=entry.data.get("laya_base_url") or LAYA_BASE_URL,
        laya_model=entry.data.get("laya_model") or LAYA_MODEL,
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
