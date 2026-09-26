"""Provider selection: hosted Jev keys, Laya on this machine, or both in a chain.

Three routes can be configured, and exactly one of them is selected:

- ``openrouter`` calls hosted Jev with an API key.
- ``laya`` calls the local server, with no key and no fallback.
- ``laya_then_hosted`` chains the two: Laya first, then every hosted provider
  that has a usable key, in the hosted order.

Consequences, all of them enforced here:

- Selecting the local route returns a provider order of exactly one provider.
- Selecting the chained route returns the local provider first and then only
  the hosted providers with a usable key. With no hosted key at all it fails
  fast, so the chained route never degrades into the local-only route.
- The default, ``auto``, never selects the local route on its own initiative;
  it builds the hosted order from the providers that have a usable key.
- The hosted order accepts only hosted provider names, so ``laya`` and a route
  name such as ``laya_then_hosted`` are rejected there rather than being
  appended as a last resort.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from .jev import (
    LAYA_BASE_URL,
    LAYA_MODEL,
    LAYA_TIMEOUT,
    LOCAL_FALLBACK_FAILURE_LIMIT,
    LOCAL_PROVIDER,
    ChainedJev,
    LayaJev,
    OpenRouterJev,
    local_failure_count,
    note_local_failure,
    reset_local_failures,
)

HOSTED_PROVIDERS = ("openrouter",)
LOCAL_PROVIDERS = ("laya",)
CHAINED_PROVIDER = "laya_then_hosted"
PROVIDER_NAMES = ("auto",) + HOSTED_PROVIDERS + LOCAL_PROVIDERS + (CHAINED_PROVIDER,)
PROVIDER_ENV = {"openrouter": "OPENROUTER_API_KEY", "laya": "LAYA_API_KEY"}
DEFAULT_FALLBACK_ORDER = ("openrouter",)

# The three named arrangements, in the vocabulary the DOGA fork uses. Each name
# resolves onto exactly one canonical route name, which is what an entry stores
# and what the config flow offers.
MODE_ALIASES = {
    "jev_api": "openrouter",
    "laya_local": "laya",
    "laya_with_jev_fallback": "laya_then_hosted",
}
MODE_NAMES = ("jev_api", "laya_local", "laya_with_jev_fallback")
PROVIDER_MODES = {
    "openrouter": "jev_api",
    "laya": "laya_local",
    "laya_then_hosted": "laya_with_jev_fallback",
}


def resolve_provider(name: object) -> str:
    """Map a mode name or a canonical route name onto the canonical route name.

    Only the three named arrangements are rewritten, and each is recognised in
    either case. Any other value, including a canonical route name, is returned
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
    keys = {
        name: (env if env is not None else os.environ).get(var, "").strip()
        for name, var in PROVIDER_ENV.items()
    }
    if provider == CHAINED_PROVIDER:
        # The chain starts on the local server, so it needs at least one hosted
        # hop behind it. Without a hosted key there is no fallback to route to,
        # and returning the local hop alone would silently turn the chained
        # route into the local-only route.
        hosted = [name for name in order if keys[name]]
        if not hosted:
            raise ValueError(
                "missing "
                + " or ".join(PROVIDER_ENV[name] for name in order)
                + " for the "
                + CHAINED_PROVIDER
                + " route"
            )
        return [LOCAL_PROVIDER] + hosted
    if provider == "auto":
        # The hosted order contains only providers with a usable key. The
        # keyless local route is never selected on its own initiative.
        return [name for name in order if keys[name]]
    if not keys[provider]:
        raise ValueError("missing " + PROVIDER_ENV[provider])
    return [provider]


def _hosted_adapter(
    name: str, api_key: str | None, timeout: float | None
) -> OpenRouterJev:
    """Build the adapter for one hosted provider name."""
    if name != "openrouter":
        raise ValueError("invalid provider")
    return OpenRouterJev(api_key, timeout=30.0 if timeout is None else timeout)


def build_provider(
    provider: str = "auto",
    *,
    api_key: str | None = None,
    laya_base_url: str = LAYA_BASE_URL,
    laya_model: str = LAYA_MODEL,
    timeout: float | None = None,
    fallback_order: tuple[str, ...] | list[str] = DEFAULT_FALLBACK_ORDER,
    env: Mapping[str, str] | None = None,
) -> OpenRouterJev | LayaJev | ChainedJev:
    """Build the adapter for the configured route.

    ``api_key`` is the hosted key for a hosted route, and also for the chained
    route, whose local hop keeps its own optional ``LAYA_API_KEY``. On the local
    route it is the optional token for a ``laya-serve`` started with its own
    bearer check; when it is absent the request carries no ``Authorization``
    header. An explicit key also supplies the hosted key for the route check, so
    a caller that holds the key in its own configuration does not need it in the
    environment as well.
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
    if order[0] == LOCAL_PROVIDER:
        # The chained route. The local hop comes first and needs no hosted key,
        # so ``api_key`` belongs to the hosted hops only.
        local = LayaJev(
            laya_base_url,
            model=laya_model,
            timeout=LAYA_TIMEOUT if timeout is None else timeout,
        )
        hosted = [
            (
                name,
                _hosted_adapter(
                    name, source.get(PROVIDER_ENV[name], "") or None, timeout
                ),
            )
            for name in order[1:]
        ]
        return ChainedJev([(LOCAL_PROVIDER, local), *hosted])
    return _hosted_adapter(order[0], api_key, timeout)
