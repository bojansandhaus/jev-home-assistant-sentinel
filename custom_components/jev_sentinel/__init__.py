"""Home Assistant entry point for Jev Home Sentinel."""
from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import service

from sentinel import Case, SentinelWorkflow
from sentinel.jev import OpenRouterJev

DOMAIN = "jev_sentinel"
PLATFORMS = ["sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = entry.data
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def review(call: ServiceCall) -> None:
        case = Case.create(
            call.data.get("event_type", "manual_review"),
            area=call.data.get("area"),
            entities=call.data.get("entities", []),
            facts=call.data.get("facts", {}),
            requested_action=call.data.get("requested_action"),
        )
        provider = OpenRouterJev(entry.data.get("api_key"))
        workflow = SentinelWorkflow(provider)
        decision = await hass.async_add_executor_job(workflow.review, case)
        hass.bus.async_fire(f"{DOMAIN}_decision", {"case": case.to_dict(), "decision": decision.to_dict()})

    async def verify(call: ServiceCall) -> None:
        hass.bus.async_fire(f"{DOMAIN}_verification", {"expected": call.data.get("expected"), "actual": call.data.get("actual")})

    if not hass.services.has_service(DOMAIN, "review"):
        hass.services.async_register(DOMAIN, "review", review)
        hass.services.async_register(DOMAIN, "verify", verify)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok
