"""Home Assistant entry point for Jev Home Assistant Sentinel.

A service in Home Assistant is registered once per domain, not once per config
entry, so the handlers below are registered once and resolve **which entry a call
is for on every call**. The previous shape registered the first entry's closures
and discarded the second entry's, which meant every ``jeve_sentinel.review``
called the first entry's provider: add a ``local_only`` entry beside a hosted one
and household cases still left the house over the OpenRouter key stored in entry
#1, contrary to the second entry's intent. That is the whole reason every call
now names its entry.

A call that names no entry is refused when more than one entry is loaded. It is
not answered from whichever entry happens to be first, because a silent default
is exactly the misrouting above, wearing a different hat.

Every failure path fires a decision event and moves the status sensor. An
operator watching the sensor must never see ``ready`` while every review on that
entry is failing.
"""

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

SERVICES = ("review", "verify")
# The field a caller uses to name the entry a call is for. It is not a field of
# the request itself; it says whose configuration answers it.
ENTRY_FIELD = "entry_id"
# Where the entry id lives in the service call data and the event payload.
REVIEW_EVENT = "jev_sentinel_decision"
VERIFY_EVENT = "jev_sentinel_verification"


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


def _entries(hass: HomeAssistant) -> dict:
    """Every loaded entry, by entry id.

    ``hass.data[DOMAIN]`` is keyed by entry id while entries are loaded, and is
    popped empty on the last unload, so an empty mapping is the ordinary "nothing
    is loaded" state rather than a missing key.
    """
    return hass.data.get(DOMAIN) or {}


def _resolve_entry(hass: HomeAssistant, call: ServiceCall) -> ConfigEntry | None:
    """The entry a service call is for, or ``None`` with the reason left to the caller.

    The call names an ``entry_id``; with exactly one entry loaded the call need
    not, because there is nothing to disambiguate. With more than one and no
    naming field, there is no honest default, so ``None`` is returned and the
    caller refuses the call instead of answering it from whichever entry happens
    to sit first in the mapping.
    """
    entries = _entries(hass)
    named = call.data.get(ENTRY_FIELD)
    if named:
        entry = hass.config_entries.async_get_entry(str(named))
        # The entry must still be loaded. A handler that keeps a reference to an
        # unloaded entry is what this resolution replaced.
        if entry is not None and entry.entry_id in entries:
            return entry
        return None
    if len(entries) == 1:
        return next(iter(entries.values()))
    return None


def _failure(
    entry: ConfigEntry | None,
    case: Case,
    exc: BaseException,
    *,
    outcome: str = "error",
) -> dict:
    """The decision event a failed review fires.

    The shape mirrors a successful one — ``case`` and ``decision`` — because the
    sensor reads ``decision.outcome`` and an event it does not recognise leaves
    the sensor showing whatever it showed before, which is the stale-reading bug
    this replaces. ``outcome`` is ``error`` for a provider that could not be
    built or a review that raised, and ``unavailable`` when no entry could be
    resolved at all and there is therefore no case to speak of.
    """
    return {
        "entry_id": getattr(entry, "entry_id", None),
        "case": redact(case.to_dict()),
        "decision": {
            "outcome": outcome,
            "reason": str(exc) or type(exc).__name__,
            "action": None,
            "confidence": None,
            "shadow": True,
            "error_type": type(exc).__name__,
        },
    }


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # The entry object is stored, not a copy of its data. A handler that resolved
    # an entry by looking its id up in a mapping of plain dicts got a dict back
    # and had no entry to hand a provider builder; the entry is the thing that
    # carries ``.data`` and ``.entry_id``, and reproducing it here means every
    # read of ``hass.data[DOMAIN]`` sees the same object Home Assistant does.
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = entry
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def review(call: ServiceCall) -> None:
        case = Case.create(
            call.data.get("event_type", "manual_review"),
            area=call.data.get("area"),
            entities=call.data.get("entities", []),
            facts=call.data.get("facts", {}),
            requested_action=call.data.get("requested_action"),
        )
        entry = _resolve_entry(hass, call)
        if entry is None:
            loaded = len(_entries(hass))
            reason = (
                f"{loaded} config entries are loaded and the call named none of"
                f" them; pass {ENTRY_FIELD} to say which one answers"
                if loaded != 1
                else "no config entry is loaded"
            )
            hass.bus.async_fire(
                REVIEW_EVENT,
                _failure(None, case, ValueError(reason), outcome="unavailable"),
            )
            return
        try:
            provider = _provider_for(entry)
            workflow = SentinelWorkflow(provider)
            decision = await hass.async_add_executor_job(workflow.review, case)
        except Exception as exc:  # a misconfigured entry must not take the call down
            hass.bus.async_fire(REVIEW_EVENT, _failure(entry, case, exc))
            return
        hass.bus.async_fire(
            REVIEW_EVENT,
            {
                "entry_id": entry.entry_id,
                "case": redact(case.to_dict()),
                "decision": decision.to_dict(),
            },
        )

    async def verify(call: ServiceCall) -> None:
        entry = _resolve_entry(hass, call)
        expected = call.data.get("expected")
        actual = call.data.get("actual")
        result = verify_observation(
            expected, actual, available=call.data.get("available", True)
        )
        payload = dict(result)
        if entry is None:
            # Verification is a local comparison, so it is answered even with no
            # entry to resolve — but it says which entry it was attributed to,
            # and a caller that named an entry which is not loaded is told so
            # rather than silently compared against nothing.
            payload["entry_id"] = None
            payload["unresolved_entry"] = True
        else:
            payload["entry_id"] = entry.entry_id
        hass.bus.async_fire(VERIFY_EVENT, payload)

    if not hass.services.has_service(DOMAIN, "review"):
        hass.services.async_register(DOMAIN, "review", review)
        hass.services.async_register(DOMAIN, "verify", verify)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    # The handlers resolve their entry on every call, so they stay valid for the
    # entries that remain and are removed only when the last one has gone.
    if unload_ok and not hass.data.get(DOMAIN):
        for service in SERVICES:
            hass.services.async_remove(DOMAIN, service)
        hass.data.pop(DOMAIN, None)
    return unload_ok
