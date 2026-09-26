"""Provider selection: one hosted Jev key, or Laya on this machine.

The two routes are mutually exclusive. A hosted route is Jev reached with an
API key. The local route is Laya, reached at a loopback address, with no key.

Consequences, all of them enforced here:

- Selecting the local route returns a provider order of exactly one provider.
- The default, ``auto``, never selects the local route on its own initiative;
  it builds the hosted order from the providers that have a usable key.
- The hosted order accepts only hosted provider names, so ``laya`` is rejected
  there rather than being appended as a last resort.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from .jev import LAYA_BASE_URL, LAYA_MODEL, LAYA_TIMEOUT, LayaJev, OpenRouterJev

HOSTED_PROVIDERS = ("openrouter",)
LOCAL_PROVIDERS = ("laya",)
PROVIDER_NAMES = ("auto",) + HOSTED_PROVIDERS + LOCAL_PROVIDERS
PROVIDER_ENV = {"openrouter": "OPENROUTER_API_KEY", "laya": "LAYA_API_KEY"}
DEFAULT_FALLBACK_ORDER = ("openrouter",)
LOCAL_PROVIDER = "laya"


def validate_fallback_order(order: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Accept a hosted provider order, and reject a local member."""
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
    if provider == "auto":
        # The hosted order contains only providers with a usable key. The
        # keyless local route is never selected on its own initiative.
        return [name for name in order if keys[name]]
    if not keys[provider]:
        raise ValueError("missing " + PROVIDER_ENV[provider])
    return [provider]


def build_provider(
    provider: str = "auto",
    *,
    api_key: str | None = None,
    laya_base_url: str = LAYA_BASE_URL,
    laya_model: str = LAYA_MODEL,
    timeout: float | None = None,
    fallback_order: tuple[str, ...] | list[str] = DEFAULT_FALLBACK_ORDER,
    env: Mapping[str, str] | None = None,
) -> OpenRouterJev | LayaJev:
    """Build the adapter for the configured route.

    ``api_key`` is the hosted key for an OpenRouter route. On the local route it
    is the optional token for a ``laya-serve`` started with its own bearer
    check; when it is absent the request carries no ``Authorization`` header.
    An explicit key also supplies the hosted key for the route check, so a
    caller that holds the key in its own configuration does not need it in the
    environment as well.
    """
    source = dict(os.environ if env is None else env)
    if api_key and provider != LOCAL_PROVIDER:
        source[PROVIDER_ENV["openrouter"]] = api_key
    order = provider_order(provider, fallback_order=fallback_order, env=source)
    if not order:
        raise RuntimeError(
            "no Jev provider is configured: set OPENROUTER_API_KEY"
            " or select the local Laya provider"
        )
    if order[0] == LOCAL_PROVIDER:
        return LayaJev(
            laya_base_url,
            model=laya_model,
            api_key=api_key,
            timeout=LAYA_TIMEOUT if timeout is None else timeout,
        )
    return OpenRouterJev(api_key, timeout=30.0 if timeout is None else timeout)
