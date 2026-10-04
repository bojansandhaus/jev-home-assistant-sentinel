"""Provider selection: hosted decision models, Laya on this machine, or a chain.

Routes can be configured, and exactly one of them is selected:

- ``openrouter`` calls hosted Jev with an API key.
- ``clef`` calls Cloudflare Workers AI, which hosts the Clef decision model. It
  is a hosted provider like ``openrouter``: it can be selected on its own, it
  can sit in a fallback order, and a chain can put it before another hosted
  provider.
- ``laya`` calls the local server, with no key and no fallback.
- ``laya_then_hosted`` chains the local server first, then every hosted provider
  that is configured, in the hosted order.

Consequences, all of them enforced here:

- Selecting the local route returns a provider order of exactly one provider.
- Selecting the chained route returns the local provider first and then only
  the hosted providers that are configured. With no configured hosted provider
  at all it fails fast, so the chained route never degrades into the local-only
  route.
- The default, ``auto``, never selects the local route on its own initiative;
  it builds the hosted order from the providers that are configured.
- The hosted order accepts only hosted provider names, so ``laya`` and a route
  name such as ``laya_then_hosted`` are rejected there rather than being
  appended as a last resort.
- Clef needs two variables, a token and an account id. A provider is
  considered configured only when both are present, so a route that would call
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
    local_failure_count,
    note_local_failure,
    reset_local_failures,
)

# The canonical route names. ``clef`` joins ``openrouter`` as a hosted provider,
# and both live and cloud hosted routes read their credentials from the
# environment rather than from a config entry.
HOSTED_PROVIDERS = ("openrouter", "clef")
LOCAL_PROVIDERS = ("laya",)
CHAINED_PROVIDER = "laya_then_hosted"
CLEF_CHECKPOINT_PROVIDER = "clef"
PROVIDER_NAMES = (
    ("auto",) + HOSTED_PROVIDERS + LOCAL_PROVIDERS + (CHAINED_PROVIDER, "clef_then_jev")
)
PROVIDER_ENV = {
    "openrouter": "OPENROUTER_API_KEY",
    "laya": "LAYA_API_KEY",
    # Clef needs a credential and a configuration value. The provider is
    # configured only when both are present; the primary variable names the
    # credential in the failure message.
    "clef": CLEF_TOKEN_ENV,
}
# The variables that make each provider usable. A hosted provider whose every
# variable is present is eligible for the route; one with any missing is not.
PROVIDER_REQUIRED_ENV = {"clef": (CLEF_TOKEN_ENV, CLEF_ACCOUNT_ENV)}
DEFAULT_FALLBACK_ORDER = ("openrouter",)

# The named arrangements, in the vocabulary the DOGA fork uses. Each name
# resolves onto exactly one canonical route name, which is what an entry stores
# and what the config flow offers.
MODE_ALIASES = {
    "jev_api": "openrouter",
    "laya_local": "laya",
    "laya_with_jev_fallback": "laya_then_hosted",
    "clef_api": "clef",
    "clef_with_jev_fallback": "clef_then_jev",
}
MODE_NAMES = (
    "jev_api",
    "laya_local",
    "laya_with_jev_fallback",
    "clef_api",
    "clef_with_jev_fallback",
)
PROVIDER_MODES = {
    "openrouter": "jev_api",
    "laya": "laya_local",
    "laya_then_hosted": "laya_with_jev_fallback",
    "clef": "clef_api",
    "clef_then_jev": "clef_with_jev_fallback",
}

# The second Clef arrangement is a chain, so it needs its own canonical route
# name and its own entry in ``PROVIDER_NAMES``. It is a hosted-only chain: Clef
# first, then the remaining configured hosted providers in the hosted order.
CLEF_CHAINED_PROVIDER = "clef_then_jev"


def resolve_provider(name: object) -> str:
    """Map a mode name or a canonical route name onto the canonical route name.

    Only the named arrangements are rewritten, and each is recognised in either
    case. Any other value, including a canonical route name, is returned
    unchanged so the existing validation still decides whether it is valid.
    """
    if not isinstance(name, str):
        return ""
    candidate = name.strip()
    return MODE_ALIASES.get(candidate, MODE_ALIASES.get(candidate.lower(), candidate))


def provider_mode(provider: str) -> str:
    """The named arrangement for a canonical route name."""
    canonical = resolve_provider(provider)
    if canonical not in PROVIDER_MODES:
        raise ValueError("invalid provider")
    return PROVIDER_MODES[canonical]


def _configured(name: str, env: Mapping[str, str]) -> bool:
    """Whether a provider has every variable it needs, in this environment."""
    required = PROVIDER_REQUIRED_ENV.get(name, (PROVIDER_ENV[name],))
    return all((env.get(var) or "").strip() for var in required)


def validate_fallback_order(order: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Accept a hosted provider order, and reject every other name.

    A member of the order is a hosted provider. The local provider is not one,
    because it is a route of its own, and neither is a route name such as
    ``laya_then_hosted``, which would be a chain inside a chain.
    """
    if (
        not order
        or len(set(order)) != len(order)
        or any(name not in HOSTED_PROVIDERS for name in order)
    ):
        raise ValueError("invalid fallback_order")
    return tuple(order)


def provider_order(
    provider: str = "auto",
    *,
    fallback_order: tuple[str, ...] | list[str] = DEFAULT_FALLBACK_ORDER,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """Resolve the providers a configured route may call, in order."""
    provider = resolve_provider(provider)
    if provider not in PROVIDER_NAMES:
        raise ValueError("invalid provider")
    order = validate_fallback_order(fallback_order)
    if provider == LOCAL_PROVIDER:
        # Laya runs locally in place of the hosted provider, so the local route
        # has no chain to fall through and no credential to find.
        return [LOCAL_PROVIDER]
    source = os.environ if env is None else env
    configured = {name: _configured(name, source) for name in HOSTED_PROVIDERS}
    if provider == CHAINED_PROVIDER:
        # The chain starts on the local server, so it needs at least one hosted
        # hop behind it. Without a configured hosted provider there is no
        # fallback to route to, and returning the local hop alone would silently
        # turn the chained route into the local-only route.
        hosted = [name for name in order if configured[name]]
        if not hosted:
            raise ValueError(
                "missing "
                + " or ".join(PROVIDER_ENV[name] for name in order)
                + " for the "
                + CHAINED_PROVIDER
                + " route"
            )
        return [LOCAL_PROVIDER] + hosted
    if provider == CLEF_CHAINED_PROVIDER:
        # Clef first, then the hosted order behind it. Clef itself must be
        # configured: a route named after Clef that would never call Clef is a
        # misconfiguration, not a chain. A chain also needs a configured hop
        # behind it, for the same reason the local-first chain fails fast.
        if not configured[CLEF_CHECKPOINT_PROVIDER]:
            raise ValueError("missing " + _missing_names(CLEF_CHECKPOINT_PROVIDER))
        hosted = [
            name
            for name in order
            if name != CLEF_CHECKPOINT_PROVIDER and configured[name]
        ]
        if not hosted:
            raise ValueError(
                "no configured hosted provider behind Clef for the "
                + CLEF_CHAINED_PROVIDER
                + " route"
            )
        return [CLEF_CHECKPOINT_PROVIDER, *hosted]
    if provider == "auto":
        # The hosted order contains only configured providers. The keyless local
        # route is never selected on its own initiative.
        return [name for name in order if configured[name]]
    if not configured[provider]:
        raise ValueError("missing " + _missing_names(provider))
    return [provider]


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
    timeout: float | None = None,
    fallback_order: tuple[str, ...] | list[str] = DEFAULT_FALLBACK_ORDER,
    env: Mapping[str, str] | None = None,
):
    """Build the adapter for the configured route.

    ``api_key`` is the hosted key for a hosted route, and also for the chained
    routes, whose local hop keeps its own optional ``LAYA_API_KEY``. On the
    local route it is the optional token for a ``laya-serve`` started with its
    own bearer check; when it is absent the request carries no ``Authorization``
    header. An explicit key also supplies the hosted credential for the route
    check, so a caller that holds the key in its own configuration does not need
    it in the environment as well.
    """
    source = dict(os.environ if env is None else env)
    provider = resolve_provider(provider)
    if api_key and provider != LOCAL_PROVIDER:
        source[PROVIDER_ENV["openrouter"]] = api_key
    order = provider_order(provider, fallback_order=fallback_order, env=source)
    if not order:
        raise RuntimeError(
            "no Jev provider is configured: set OPENROUTER_API_KEY"
            " or select the local Laya provider"
        )
    if provider == LOCAL_PROVIDER:
        return LayaJev(
            laya_base_url,
            model=laya_model,
            api_key=api_key,
            timeout=LAYA_TIMEOUT if timeout is None else timeout,
        )
    if provider == CLEF_CHAINED_PROVIDER:
        # Clef first, then the hosted order behind it. This is a hosted-only
        # chain, so no local hop takes part.
        chain = [
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
            for name in order
        ]
        return ChainedJev(chain)
    if order[0] == LOCAL_PROVIDER:
        # The local-first chain. The local hop comes first and needs no hosted
        # key, so ``api_key`` belongs to the hosted hops only.
        local = LayaJev(
            laya_base_url,
            model=laya_model,
            timeout=LAYA_TIMEOUT if timeout is None else timeout,
        )
        hosted = [
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
            for name in order[1:]
        ]
        return ChainedJev([(LOCAL_PROVIDER, local), *hosted])
    return _hosted_adapter(
        order[0],
        source.get(PROVIDER_ENV[order[0]]) or None,
        timeout,
        source,
        clef_model,
    )
