"""Provider selection: the four decision modes over a local slot or the API.

Configuration exposes exactly four decision modes. Each names which side leads
and whether the other side is a fallback:

=================  ================  =============  ===========================
Mode               Leads             Fallback       Provider order
=================  ================  =============  ===========================
``api_with_local_`` hosted API       local          every configured hosted
                     ``fallback``                     provider, then the local
                                                     slot
``api_only``       hosted API       none           one configured hosted
                                                     provider
``local_only``     local slot       none           the local slot alone
``local_with_api_`` local slot      hosted API     the local slot, then every
``fallback``                                        configured hosted provider
=================  ================  =============  ===========================

The two ``_only`` modes are single-provider routes: no chain, no fallback, and
no cooldown list beyond the one provider. A failure there is reported, never
rerouted. The two ``_with_..._fallback`` modes are two-provider chains and use
the existing cooldown, trigger, and breaker machinery unchanged.

Which hosted provider answers the API side is still a separate choice, and it is
not renamed: ``openrouter`` (hosted Jev over an OpenRouter key) and ``clef``
(Cloudflare Workers AI) remain individually selectable and remain the members of
the hosted fallback order. The local provider name stays ``laya`` and is now a
generic local decision-model slot: ``local_model`` selects which local engine
answers, so a different local model is configuration rather than a new provider.

Consequences, all of them enforced here:

- Selecting a local-only mode returns a provider order of exactly one provider.
- Selecting a fallback mode that promises the other side and has no usable
  provider for it fails at load, naming the missing environment variable, rather
  than silently degrading into the single-provider mode under another name.
- The default, ``auto``, resolves to ``api_with_local_fallback`` when a usable
  local model is configured and to ``api_only`` otherwise, preserving what
  ``auto`` meant before: pick among the configured hosted providers, and put the
  local slot behind them rather than selecting it on its own initiative.
- The hosted order accepts only hosted provider names, so ``laya``, a mode name,
  and ``local_model`` are all rejected there rather than being appended as a last
  resort.
- Clef needs two variables, a token and an account id. A provider is
  considered configured only when both are present, so a mode that would call
  Clef is refused before any request rather than failing halfway through one.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from .jev import (
    CLEF_ACCOUNT_ENV,
    CLEF_DEFAULT_MODEL,
    CLEF_MODELS,
    CLEF_TOKEN_ENV,
    LAYA_BASE_URL,
    LAYA_MODEL,
    LAYA_TIMEOUT,
    LOCAL_FALLBACK_FAILURE_LIMIT,
    LOCAL_PROVIDER,
    ChainedJev,
    ClefJev,
    LayaJev,
    OpenRouterJev,
    local_checkpoint,
    local_failure_count,
    note_local_failure,
    reset_local_failures,
)

# The hosted providers. They are not renamed by the four-mode contract: the mode
# names the API as one side, and which hosted model answers it stays its own
# individually selectable choice, exactly as it was before.
HOSTED_PROVIDERS = ("openrouter", "clef")
# The local slot. The provider name stays ``laya``; ``local_model`` selects the
# engine behind it, so a different local model needs no new provider name.
LOCAL_PROVIDERS = ("laya",)

# The four canonical mode names. They are the only names a diagnostic, a log
# line, or a config entry should ever carry: every alias resolves onto one of
# them before it reaches a chain, a URL, or an error message.
API_WITH_LOCAL_FALLBACK = "api_with_local_fallback"
API_ONLY = "api_only"
LOCAL_ONLY = "local_only"
LOCAL_WITH_API_FALLBACK = "local_with_api_fallback"
CANONICAL_MODES = (
    API_WITH_LOCAL_FALLBACK,
    API_ONLY,
    LOCAL_ONLY,
    LOCAL_WITH_API_FALLBACK,
)

# The modes that promise a fallback and therefore fail at load when the other
# side has no usable provider. Both names are carried in a failure message.
FALLBACK_MODES = (API_WITH_LOCAL_FALLBACK, LOCAL_WITH_API_FALLBACK)
# The modes that call one provider and report its failure rather than rerouting.
SINGLE_PROVIDER_MODES = (API_ONLY, LOCAL_ONLY)

PROVIDER_ENV = {
    "openrouter": "OPENROUTER_API_KEY",
    "laya": "LAYA_API_KEY",
    # Clef needs a credential and a configuration value. The provider is
    # configured only when both are present; the primary variable names the
    # credential in the failure message.
    "clef": CLEF_TOKEN_ENV,
}
# The variables that make each provider usable. A hosted provider whose every
# variable is present is eligible for the mode; one with any missing is not.
PROVIDER_REQUIRED_ENV = {"clef": (CLEF_TOKEN_ENV, CLEF_ACCOUNT_ENV)}
DEFAULT_FALLBACK_ORDER = ("openrouter",)

# Every name that resolves to a canonical mode. Each entry is either one of the
# four canonical names, a pre-existing arrangement name, or a pre-existing
# canonical route name. Nothing is removed, so no deployed configuration breaks.
#
# The pre-existing names are grouped by the canonical mode they denote, and the
# table is documented in docs/reference.md:
#
# - ``api_with_local_fallback``: ``clef_with_local_fallback``. This mode is new to
#   this repository: v1.3.0 had no hosted-first chain, because both of its chains
#   were local-first or hosted-only. A hosted provider reached this mode only by
#   naming the chain explicitly, so the legacy hosted provider names keep
#   resolving to ``api_only``, which is exactly what they selected before.
# - ``api_only``: ``openrouter``, ``jev_api``, ``clef``, ``clef_api``, and
#   ``auto``. Each of these named one hosted provider and reported its failure
#   without rerouting, so each keeps meaning one hosted provider and no fallback.
# - ``local_only``: ``laya``, ``laya_local``.
# - ``local_with_api_fallback``: ``laya_then_hosted``,
#   ``laya_with_jev_fallback``, ``clef_then_jev``,
#   ``clef_with_jev_fallback``.
#
# ``clef_then_jev`` is a hosted-only chain in v1.3.0: Clef first, then hosted
# Jev. It is not one of the four canonical modes, because both sides it chains are
# hosted. It is kept as an alias of ``local_with_api_fallback`` so that an entry
# storing it keeps routing the same way, Clef first and hosted Jev behind it, and
# ``_clef_pinned_mode`` keeps the Clef lead that the mode name alone cannot
# express. That is the one place this repository's vocabulary does not map
# cleanly onto the shared four-mode shape, and preserving the routing is what
# backwards compatibility requires.
#
# ``auto`` is dynamic: it resolves to ``api_only`` when no usable local model is
# configured and to ``api_with_local_fallback`` when one is, which is what
# ``resolve_auto_mode`` decides from the environment. It is mapped to
# ``api_only`` here so that a lookup of the mode constant is total, and
# ``PROVIDER_MODES["auto"]`` carries the same fallback.
MODE_ALIASES = {
    # hosted API leading, local slot behind it
    API_WITH_LOCAL_FALLBACK: API_WITH_LOCAL_FALLBACK,
    "clef_with_local_fallback": API_WITH_LOCAL_FALLBACK,
    # ``auto`` is dynamic rather than a fixed alias: see ``resolve_auto_mode``.
    "auto": API_ONLY,
    # the hosted API alone. Each name pins one hosted provider, which is how
    # each of them behaved before the four-mode contract: a single provider
    # whose failure is reported and never rerouted.
    API_ONLY: API_ONLY,
    "openrouter": API_ONLY,
    "jev_api": API_ONLY,
    "clef": API_ONLY,
    "clef_api": API_ONLY,
    # the local slot alone
    LOCAL_ONLY: LOCAL_ONLY,
    "laya": LOCAL_ONLY,
    "laya_local": LOCAL_ONLY,
    # the local slot leading, the hosted API behind it
    LOCAL_WITH_API_FALLBACK: LOCAL_WITH_API_FALLBACK,
    "laya_then_hosted": LOCAL_WITH_API_FALLBACK,
    "laya_with_jev_fallback": LOCAL_WITH_API_FALLBACK,
    "clef_then_jev": LOCAL_WITH_API_FALLBACK,
    "clef_with_jev_fallback": LOCAL_WITH_API_FALLBACK,
}
MODE_NAMES = CANONICAL_MODES

# The canonical mode for a name. Every alias above is listed here too, so the
# reverse lookup is total and neither table can name a mode the other does not.
PROVIDER_MODES = {
    API_WITH_LOCAL_FALLBACK: API_WITH_LOCAL_FALLBACK,
    API_ONLY: API_ONLY,
    LOCAL_ONLY: LOCAL_ONLY,
    LOCAL_WITH_API_FALLBACK: LOCAL_WITH_API_FALLBACK,
    "clef_with_local_fallback": API_WITH_LOCAL_FALLBACK,
    "auto": API_ONLY,
    "openrouter": API_ONLY,
    "jev_api": API_ONLY,
    "clef": API_ONLY,
    "clef_api": API_ONLY,
    "laya": LOCAL_ONLY,
    "laya_local": LOCAL_ONLY,
    "laya_then_hosted": LOCAL_WITH_API_FALLBACK,
    "laya_with_jev_fallback": LOCAL_WITH_API_FALLBACK,
    "clef_then_jev": LOCAL_WITH_API_FALLBACK,
    "clef_with_jev_fallback": LOCAL_WITH_API_FALLBACK,
}


# The mode names this repository shipped before the four-mode contract, kept as
# names so a caller that imports them still works. ``CHAINED_PROVIDER`` was the
# local-first chain and ``CLEF_CHAINED_PROVIDER`` the Clef-first chain; both now
# denote ``local_with_api_fallback``.
CHAINED_PROVIDER = "laya_then_hosted"
CLEF_CHAINED_PROVIDER = "clef_then_jev"
# ``clef`` is both a hosted provider name and the Clef checkpoint provider, as
# it was in v1.3.0. The two are the same string here.
CLEF_CHECKPOINT_PROVIDER = "clef"
# Every accepted name, canonical mode or alias. An unknown name is refused with
# a message that names every mode and alias the caller may use.
PROVIDER_NAMES = tuple(MODE_ALIASES)


def accepted_provider_names() -> str:
    """Every accepted name, for an error message that must not hide them."""
    return ", ".join(sorted(PROVIDER_NAMES))


def resolve_provider(name: object) -> str:
    """Map a mode name or a legacy route name onto one of the four modes.

    Every name in ``MODE_ALIASES`` is rewritten onto the canonical mode it
    denotes, so no alias string reaches a chain, a diagnostic, a log line, or a
    URL. Any other value is returned unchanged so the existing validation still
    decides whether it is valid.

    Case handling is unchanged from v1.3.0: the arrangement names and the four
    canonical mode names are recognised in either case, and the canonical route
    names are matched exactly as they were, so a value like ``Laya`` is still
    refused rather than newly accepted.
    """
    if not isinstance(name, str):
        return ""
    candidate = name.strip()
    if candidate in MODE_ALIASES:
        return MODE_ALIASES[candidate]
    folded = candidate.lower()
    if folded in CASE_INSENSITIVE_ALIASES:
        return MODE_ALIASES[folded]
    return candidate


def provider_mode(provider: str) -> str:
    """The canonical mode for a name, accepted as either a mode or an alias."""
    canonical = resolve_provider(provider)
    if canonical not in PROVIDER_MODES:
        raise ValueError("invalid provider: " + accepted_provider_names())
    return PROVIDER_MODES[canonical]


def _configured(name: str, env: Mapping[str, str]) -> bool:
    """Whether a provider has every variable it needs, in this environment."""
    required = PROVIDER_REQUIRED_ENV.get(name, (PROVIDER_ENV[name],))
    return all((env.get(var) or "").strip() for var in required)


def validate_fallback_order(order: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Accept a hosted provider order, and reject every other name.

    A member of the order is a hosted provider. The local provider is not one,
    because it is a slot of its own and the local side is never a member of the
    hosted order, and neither is a mode name such as ``local_with_api_fallback``,
    which would be a chain inside a chain. ``local_model`` is rejected here too,
    so a model name can never be appended to a fallback order.
    """
    if (
        not order
        or len(set(order)) != len(order)
        or any(name not in HOSTED_PROVIDERS for name in order)
    ):
        raise ValueError("invalid fallback_order")
    return tuple(order)


def local_model_configured(local_model: object = None) -> bool:
    """Whether the caller has explicitly selected a local decision model.

    This is the ``auto`` mode's notion of "a usable local model is
    configured". The local slot's URL and model are settings rather than
    credentials, and the local server's reachability cannot be known at load
    time, so the only honest signal that an operator wants the local side in
    play is that they named a model for it. A caller who passes nothing keeps
    the v1.3.0 behaviour of ``auto``, which never put the local slot in the
    order on its own initiative.
    """
    return local_model is not None


def resolve_auto_mode(
    *, env: Mapping[str, str] | None = None, local_model: object = None
) -> str:
    """The canonical mode ``auto`` denotes in this configuration.

    ``auto`` resolves to ``api_with_local_fallback`` when a local model has
    been explicitly configured, and to ``api_only`` otherwise. That preserves
    what ``auto`` meant in v1.3.0, which was "the hosted providers that are
    configured", while giving the local side a place behind the API once an
    operator asks for it by name.
    """
    return API_WITH_LOCAL_FALLBACK if local_model_configured(local_model) else API_ONLY


# The names that pin the hosted side to Clef rather than letting the hosted
# order choose. ``clef_with_local_fallback`` is the Clef-pinned form of the
# hosted-first mode, and ``clef_then_jev`` and ``clef_with_jev_fallback`` are the
# two spellings of the Clef-first chain, which the mode name alone cannot
# express because both of its hops are hosted.
CLEF_PINNED_NAMES = (
    "clef",
    "clef_api",
    "clef_with_local_fallback",
    "clef_then_jev",
    "clef_with_jev_fallback",
)

# The names recognised in either case. In v1.3.0 this was the set of
# arrangement names, and it is that same set plus the four canonical mode names.
# The canonical route names are deliberately not in it: they were matched
# exactly before this change and stay matched exactly, so `provider_order("Laya")`
# still raises rather than silently becoming the local mode.
CASE_INSENSITIVE_ALIASES = frozenset(
    set(CANONICAL_MODES)
    | {
        "jev_api",
        "laya_local",
        "laya_with_jev_fallback",
        "clef_api",
        "clef_with_jev_fallback",
    }
)


def pins_clef(provider: object) -> bool:
    """Whether a stored name pins the hosted side to Clef rather than the order.

    The config entry module and the config flow need this because they must
    decide which credential an entry may contribute *before* the name is
    resolved, and resolution collapses a Clef-pinned name and a Jev-pinned name
    onto one canonical mode. A mode name alone cannot express the Clef lead, so
    it has to be read off the stored spelling.
    """
    return isinstance(provider, str) and provider.strip().lower() in (CLEF_PINNED_NAMES)


def _clef_pinned_mode(provider: str) -> bool:
    """Whether the name pins the hosted side to Clef rather than the order."""
    return pins_clef(provider)


def provider_order(
    provider: str = "auto",
    *,
    fallback_order: tuple[str, ...] | list[str] = DEFAULT_FALLBACK_ORDER,
    env: Mapping[str, str] | None = None,
    local_model: object = None,
) -> list[str]:
    """Resolve the providers a configured mode may call, in order.

    The order is a provider sequence, not a mode name: the local slot answers
    under the single name ``laya`` whatever engine ``local_model`` selects,
    because the engine is a checkpoint of that slot rather than a provider of
    its own. An empty order is returned rather than raised, so ``build_provider``
    can turn "nothing is configured" into its own error, exactly as before.
    """
    mode = resolve_provider(provider)
    if mode not in PROVIDER_MODES:
        raise ValueError("invalid provider: " + accepted_provider_names())
    order = validate_fallback_order(fallback_order)
    if mode == LOCAL_ONLY:
        # The local slot stands in place of the hosted API, so this mode has no
        # chain to fall through and no credential to find.
        return [LOCAL_PROVIDER]
    source = os.environ if env is None else env
    configured = {name: _configured(name, source) for name in HOSTED_PROVIDERS}
    hosted = [name for name in order if configured[name]]
    if provider.strip().lower() == "auto":
        # ``auto`` builds the hosted order from the providers that are
        # configured, and adds the local slot behind them only once a local
        # model has been explicitly configured. It never raises on a missing
        # credential: an empty order is what says nothing is configured, so
        # naming a local model does not by itself make a hosted-first mode
        # usable. That preserves what ``auto`` meant before v1.4.0.
        if not hosted:
            return []
        if resolve_auto_mode(env=source, local_model=local_model) == (
            API_WITH_LOCAL_FALLBACK
        ):
            return [*hosted, LOCAL_PROVIDER]
        return hosted
    if mode == LOCAL_WITH_API_FALLBACK:
        # The local slot leads, so it needs at least one hosted hop behind it.
        # Without a configured hosted provider there is no fallback to route to,
        # and returning the local hop alone would silently turn this mode into
        # ``local_only``.
        if _clef_pinned_mode(provider):
            # A name that pins Clef first must actually call Clef, so the chain
            # is led by it rather than by the order, and something must sit
            # behind it for the same reason the local-first chain fails fast.
            if not configured[CLEF_CHECKPOINT_PROVIDER]:
                raise ValueError("missing " + _missing_names(CLEF_CHECKPOINT_PROVIDER))
            behind = [name for name in hosted if name != CLEF_CHECKPOINT_PROVIDER]
            if not behind:
                raise ValueError(
                    "no configured hosted provider behind Clef for the "
                    + CLEF_CHAINED_PROVIDER
                    + " route"
                )
            return [CLEF_CHECKPOINT_PROVIDER, *behind]
        if not hosted:
            raise ValueError(
                "missing "
                + " or ".join(PROVIDER_ENV[name] for name in order)
                + " for the "
                + LOCAL_WITH_API_FALLBACK
                + " mode"
            )
        return [LOCAL_PROVIDER, *hosted]
    if mode == API_WITH_LOCAL_FALLBACK:
        # The hosted API leads and the local slot stands behind it, so a case
        # costs no local request while a hosted provider answers. With no
        # configured hosted provider there is nothing to lead with, so this
        # fails fast rather than quietly answering from the local slot under the
        # name of a hosted-first mode.
        if _clef_pinned_mode(provider):
            # A Clef-pinned hosted-first mode leads with Clef and puts the local
            # slot directly behind it. The rest of the hosted order is not
            # dragged in: a name that pins Clef says which provider leads.
            if not configured[CLEF_CHECKPOINT_PROVIDER]:
                raise ValueError("missing " + _missing_names(CLEF_CHECKPOINT_PROVIDER))
            return [CLEF_CHECKPOINT_PROVIDER, LOCAL_PROVIDER]
        if not hosted:
            raise ValueError(
                "missing "
                + " or ".join(PROVIDER_ENV[name] for name in order)
                + " for the "
                + API_WITH_LOCAL_FALLBACK
                + " mode"
            )
        return [*hosted, LOCAL_PROVIDER]
    # ``api_only``. A name that pins one hosted provider is served by that
    # provider; the bare mode is served by the first configured hosted provider
    # in the order. A failure on this mode is reported and never rerouted.
    if _clef_pinned_mode(provider):
        if not configured[CLEF_CHECKPOINT_PROVIDER]:
            raise ValueError("missing " + _missing_names(CLEF_CHECKPOINT_PROVIDER))
        return [CLEF_CHECKPOINT_PROVIDER]
    # ``openrouter`` and ``jev_api`` pin OpenRouter, so a selection naming one
    # is served by OpenRouter even when another hosted provider is configured.
    # This is unchanged from v1.3.0, where the same names returned exactly
    # ``["openrouter"]`` or raised naming OPENROUTER_API_KEY.
    if provider.strip().lower() in ("openrouter", "jev_api"):
        if not configured["openrouter"]:
            raise ValueError("missing " + _missing_names("openrouter"))
        return ["openrouter"]
    if not hosted:
        # The mode's own name is used here, so the message names the variable a
        # caller must set rather than a route name that may be an alias.
        raise ValueError(
            "missing "
            + " or ".join(PROVIDER_ENV[name] for name in order)
            + " for the "
            + API_ONLY
            + " mode"
        )
    return hosted[:1]


def _missing_names(name: str) -> str:
    """The variable names a provider needs, for a failure message.

    A provider with one required variable is named by it. Clef has two, and
    both are named so a misconfiguration is visible without reading the source.
    """
    return " and ".join(PROVIDER_REQUIRED_ENV.get(name, (PROVIDER_ENV[name],)))


def _hosted_adapter(
    name: str,
    api_key: str | None,
    timeout: float | None,
    env: Mapping[str, str],
    clef_model: str = CLEF_DEFAULT_MODEL,
):
    """Build the adapter for one hosted provider name."""
    if name == "openrouter":
        return OpenRouterJev(api_key, timeout=30.0 if timeout is None else timeout)
    if name == CLEF_CHECKPOINT_PROVIDER:
        # The token and the account id stay in the environment. A caller that
        # holds them itself can pass them in, and an explicit value wins over
        # the environment exactly as it does for the other hosted providers.
        return ClefJev(
            api_key,
            model=clef_model,
            timeout=30.0 if timeout is None else timeout,
            env=env,
        )
    raise ValueError("invalid provider")


def build_provider(
    provider: str = "auto",
    *,
    api_key: str | None = None,
    laya_base_url: str = LAYA_BASE_URL,
    laya_model: str = LAYA_MODEL,
    clef_model: str = CLEF_DEFAULT_MODEL,
    local_model: object = None,
    timeout: float | None = None,
    fallback_order: tuple[str, ...] | list[str] = DEFAULT_FALLBACK_ORDER,
    env: Mapping[str, str] | None = None,
):
    """Build the adapter for the configured mode.

    ``api_key`` is the hosted key for every mode whose API side is hosted, and
    also for the modes whose local slot leads, whose local hop keeps its own
    optional ``LAYA_API_KEY``. On ``local_only`` it is the optional token for a
    local server started with its own bearer check; when it is absent the
    request carries no ``Authorization`` header. An explicit key also supplies
    the hosted credential for the mode check, so a caller that holds the key in
    its own configuration does not need it in the environment as well.

    ``local_model`` selects which local decision model answers. It takes
    precedence over the pre-existing ``laya_model`` when it is set, because the
    local slot is a generic slot and the old field is the deprecated spelling of
    the same setting. ``laya_model`` is still accepted, and an entry that stored
    only ``laya_model`` keeps calling that checkpoint.
    """
    source = dict(os.environ if env is None else env)
    mode = resolve_provider(provider)
    if api_key and mode != LOCAL_ONLY:
        source[PROVIDER_ENV["openrouter"]] = api_key
    order = provider_order(
        provider,
        fallback_order=fallback_order,
        env=source,
        local_model=local_model,
    )
    if not order:
        raise RuntimeError(
            "no Jev provider is configured: set OPENROUTER_API_KEY"
            " or select the local Laya provider"
        )
    # ``local_model`` wins when it is set, and ``laya_model`` is the deprecated
    # spelling that still works. Resolving it here means one validation of the
    # name, at build time, rather than a name that reaches the wire unvalidated.
    checkpoint = local_checkpoint(laya_model if local_model is None else local_model)
    if mode == LOCAL_ONLY:
        return LayaJev(
            laya_base_url,
            model=checkpoint,
            api_key=api_key,
            timeout=LAYA_TIMEOUT if timeout is None else timeout,
        )
    if len(order) > 1:
        # Both fallback modes are two-provider chains, and the pre-existing
        # Clef-first chain is one of them. The order decides which adapter is
        # tried first, so the same construction serves the local-first chain and
        # the hosted-first chain.
        chain = []
        for name in order:
            if name == LOCAL_PROVIDER:
                # The local hop needs no hosted key, so ``api_key`` belongs to
                # the hosted hops only.
                chain.append(
                    (
                        name,
                        LayaJev(
                            laya_base_url,
                            model=checkpoint,
                            timeout=LAYA_TIMEOUT if timeout is None else timeout,
                        ),
                    )
                )
                continue
            chain.append(
                (
                    name,
                    _hosted_adapter(
                        name,
                        source.get(PROVIDER_ENV[name]) or None,
                        timeout,
                        source,
                        clef_model,
                    ),
                )
            )
        return ChainedJev(chain)
    return _hosted_adapter(
        order[0],
        source.get(PROVIDER_ENV[order[0]]) or None,
        timeout,
        source,
        clef_model,
    )
