"""Self contained runtime used when Home Assistant loads the component.

Home Assistant installs custom_components without installing the repository's
Python package, so this small bridge keeps the integration independently usable.
The public ``sentinel`` package carries the same provider neutral contract for
applications and tests outside Home Assistant, including the same rubric and the
same provider selection rules.

Three routes ship here:

- hosted Jev over an OpenRouter API key;
- Laya on this machine with no key;
- Laya first, then the hosted providers that have a key, as an explicit
  opt-in chain.

The first two are alternatives. The third is the only route that reaches the
local server and a hosted endpoint in one review.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
MODEL = "typesafe/jev-1.13"

LAYA_BASE_URL = "http://127.0.0.1:8000"
LAYA_ENDPOINT_PATH = "/v1/systemone"
LAYA_MODEL = "convaiinnovations/laya"
LAYA_TIMEOUT = 120.0
_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")

# A failed attempt falls through to the next provider in a chain only when the
# provider was unreachable, refused, rate limited, or broken. Any other status,
# such as 400 or 422, says the request itself was rejected, and the same request
# is not retried somewhere else.
FALLBACK_STATUS_CODES = frozenset({401, 403, 429})

HOSTED_PROVIDERS = ("openrouter",)
LOCAL_PROVIDERS = ("laya",)
CHAINED_PROVIDER = "laya_then_hosted"
PROVIDER_NAMES = ("auto",) + HOSTED_PROVIDERS + LOCAL_PROVIDERS + (CHAINED_PROVIDER,)
PROVIDER_ENV = {"openrouter": "OPENROUTER_API_KEY", "laya": "LAYA_API_KEY"}
DEFAULT_FALLBACK_ORDER = ("openrouter",)
LOCAL_PROVIDER = "laya"

_SECRET_TEXT = re.compile(
    r"(?i)(api[_ -]?key|token|password|secret|credential)(\s*[:=]\s*)[^\s,;]+"
)

# The confidence question is a ``score`` question, and a score rubric is an
# ordered list of level descriptions with index 0 first. A local Laya server
# rejects ``{"min": 0, "max": 1}`` with "a score question takes 'criteria' as a
# list of level descriptions, index 0 first".
CONFIDENCE_LEVELS = (
    "very low confidence, the case is ambiguous or mostly missing",
    "low confidence",
    "moderate confidence",
    "high confidence",
    "very high confidence, the case points one way",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Case:
    case_id: str
    event_type: str
    area: str | None
    entities: tuple[str, ...]
    facts: dict[str, Any]
    requested_action: str | None = None
    created_at: str = field(default_factory=_now)

    @classmethod
    def create(
        cls,
        event_type: str,
        *,
        area: str | None = None,
        entities: list[str] | tuple[str, ...] = (),
        facts: dict[str, Any] | None = None,
        requested_action: str | None = None,
    ) -> "Case":
        return cls(
            f"case_{uuid.uuid4().hex[:12]}",
            event_type,
            area,
            tuple(entities),
            dict(facts or {}),
            requested_action,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Decision:
    outcome: str
    reason: str
    confidence: float | None = None
    action: str | None = None
    shadow: bool = True
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def decision_questions() -> dict[str, dict]:
    """The rubric both routes send. A fresh mapping on every call."""
    return {
        "outcome": {
            "type": "choice",
            "instructions": "Choose the safest next outcome for this Home Assistant case.",
            "criteria": {
                "ignore": "No meaningful action",
                "notify": "Tell the user",
                "ask_user": "Need user approval or clarification",
                "recommend": "Recommend a safe allowlisted action",
                "escalate": "Treat as unresolved or risky",
            },
        },
        "action": {
            "type": "choice",
            "instructions": "Choose one action only if it is supported by the case and policy.",
            "criteria": {
                "notify": "Notify the user",
                "ask_user": "Ask the user",
                "light.turn_off": "Turn off a light",
                "switch.turn_off": "Turn off a switch",
                "climate.set_temperature": "Set a climate temperature",
                "none": "No device action",
            },
        },
        "confidence": {
            "type": "score",
            "instructions": "Score confidence in the selected outcome, from very low to very high.",
            "criteria": list(CONFIDENCE_LEVELS),
        },
    }


def confidence_from_score(
    answer: object, *, levels: tuple[str, ...] = CONFIDENCE_LEVELS
) -> float | None:
    """Map a score answer's expected level index onto 0 to 1.

    A ``score`` answer reports the expected level on the legend index scale,
    ``0`` to ``len(legend) - 1``, next to the ``legend`` naming each level. That
    index is rescaled onto 0 to 1 by dividing by ``len(legend) - 1``, so level 0
    is 0.0 and the last level is 1.0. The result is a quantized ordinal
    estimate, not a calibrated probability: adjacent levels sit
    ``1 / (levels - 1)`` apart. The raw index stays recoverable by multiplying
    the returned value by ``levels - 1``.

    A missing, non numeric, or single level answer returns ``None``.
    """
    if not isinstance(answer, dict):
        return None
    score = answer.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return None
    legend = answer.get("legend")
    count = len(legend) if isinstance(legend, (dict, list)) else len(levels)
    if count < 2:
        return None
    return min(1.0, max(0.0, float(score) / (count - 1)))


def decision_from_body(body: dict, *, model: str = MODEL) -> Decision:
    """Build the typed ``Decision`` from a Decisions shaped response body."""
    answers = body["answers"]
    action = answers["action"]["choice"]
    return Decision(
        answers["outcome"]["choice"],
        answers["outcome"]["choice"].replace("_", " "),
        confidence_from_score(answers.get("confidence")),
        None if action == "none" else action,
        True,
        {"model": body.get("model", model)},
    )


class OpenRouterJev:
    """Hosted Jev over an OpenRouter API key."""

    def __init__(self, api_key: str | None = None, *, timeout: float = 30.0) -> None:
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        self.timeout = timeout

    def decide(self, state: dict[str, Any]) -> Decision:
        if not self.api_key or self.api_key == "***":
            raise RuntimeError(
                "Configure an OpenRouter API key before requesting a Jev review"
            )
        payload = {"model": MODEL, "state": state, "questions": decision_questions()}
        request = Request(
            ENDPOINT,
            data=json.dumps(payload).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "X-Title": "Jev Home Assistant Sentinel",
            },
        )
        with urlopen(request, timeout=self.timeout) as response:
            body = json.loads(response.read().decode())
        return decision_from_body(body, model=MODEL)


def laya_endpoint(
    base_url: str = LAYA_BASE_URL, endpoint_path: str = LAYA_ENDPOINT_PATH
) -> str:
    """Resolve a local Laya server URL, refusing cleartext to a remote host."""
    if not endpoint_path.startswith("/"):
        raise ValueError("invalid Laya endpoint path")
    parsed = urlsplit(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("invalid Laya base URL")
    if parsed.scheme == "http" and parsed.hostname not in _LOOPBACK_HOSTS:
        raise ValueError("nonlocal Laya server requires HTTPS")
    return base_url.rstrip("/") + endpoint_path


class LayaJev:
    """A local ``laya-serve`` process, over the same Decisions contract.

    Laya is not a hosted Jev endpoint. It is a separate model that publishes
    ``POST /v1/systemone`` and answers in the same ``answers`` shape, so
    selecting it replaces the hosted route instead of extending it. The route is
    keyless: the ``Authorization`` header is omitted entirely unless the server
    was started with its own bearer check.
    """

    def __init__(
        self,
        base_url: str = LAYA_BASE_URL,
        *,
        endpoint_path: str = LAYA_ENDPOINT_PATH,
        model: str = LAYA_MODEL,
        api_key: str | None = None,
        timeout: float = LAYA_TIMEOUT,
    ) -> None:
        self.endpoint = laya_endpoint(base_url, endpoint_path)
        self.model = model
        self.api_key = (
            api_key if api_key is not None else os.environ.get("LAYA_API_KEY")
        )
        self.timeout = timeout

    def decide(self, state: dict[str, Any]) -> Decision:
        payload = {
            "model": self.model,
            "state": state,
            "questions": decision_questions(),
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(
            self.endpoint,
            data=json.dumps(payload).encode(),
            method="POST",
            headers=headers,
        )
        with urlopen(request, timeout=self.timeout) as response:
            body = json.loads(response.read().decode())
        return decision_from_body(body, model=self.model)


def is_fallback_trigger(exc: BaseException) -> bool:
    """Whether a failed attempt should fall through to the next provider.

    A transport error or timeout means the provider was not reachable. An HTTP
    401, 403, or 429 means the request was refused or rate limited, and an HTTP
    5xx means the provider failed. Each of those says nothing about the request
    itself, so the next provider may still answer it. Any other failure
    propagates: a request the provider rejected as invalid is not retried
    somewhere else.
    """
    if isinstance(exc, HTTPError):
        return exc.code in FALLBACK_STATUS_CODES or 500 <= exc.code <= 599
    return isinstance(exc, (URLError, TimeoutError, OSError))


class ChainedJev:
    """An ordered chain of adapters: the first one that answers decides.

    The chain holds named adapters in the order they are tried. The local Laya
    hop can sit first with a hosted hop behind it, so a case costs no provider
    request while the local server answers, and a local outage still returns a
    decision. The adapter that answered is recorded in ``Decision.raw``.
    """

    def __init__(self, providers: Sequence[tuple[str, Any]]) -> None:
        if len(providers) < 2:
            raise ValueError("a chain needs at least two providers")
        self.providers: tuple[tuple[str, Any], ...] = tuple(providers)

    @property
    def names(self) -> tuple[str, ...]:
        """The adapter names in the order they are tried."""
        return tuple(name for name, _ in self.providers)

    def decide(self, state: dict[str, Any]) -> Decision:
        for index, (_, provider) in enumerate(self.providers):
            try:
                decision = provider.decide(state)
            except Exception as exc:
                if index + 1 < len(self.providers) and is_fallback_trigger(exc):
                    continue
                raise
            return self._attributed(decision, index)
        raise RuntimeError("the chain has no provider left to call")

    def _attributed(self, decision: Decision, index: int) -> Decision:
        raw = dict(decision.raw)
        raw["provider"] = self.providers[index][0]
        raw["attempted"] = list(self.names[: index + 1])
        return replace(decision, raw=raw)


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
    env: dict[str, str] | None = None,
) -> list[str]:
    """Resolve the providers a configured route may call, in order."""
    if provider not in PROVIDER_NAMES:
        raise ValueError("invalid provider")
    order = validate_fallback_order(fallback_order)
    if provider == LOCAL_PROVIDER:
        return [LOCAL_PROVIDER]
    source = os.environ if env is None else env
    keys = {name: source.get(var, "").strip() for name, var in PROVIDER_ENV.items()}
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
        return [name for name in order if keys[name]]
    if not keys[provider]:
        raise ValueError("missing " + PROVIDER_ENV[provider])
    return [provider]


def _hosted_adapter(
    name: str, api_key: str | None, timeout: float | None
) -> "OpenRouterJev":
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
    env: dict[str, str] | None = None,
) -> "OpenRouterJev | LayaJev | ChainedJev":
    """Build the adapter for the configured route.

    On the chained route ``api_key`` is the hosted key and the local hop keeps
    its own optional ``LAYA_API_KEY``. An explicit hosted key also supplies the
    key for the route check, so a caller that holds the key in its own
    configuration does not need it in the environment as well.
    """
    source = dict(os.environ if env is None else env)
    if api_key and provider != LOCAL_PROVIDER:
        source[PROVIDER_ENV["openrouter"]] = api_key
    order = provider_order(provider, fallback_order=fallback_order, env=source)
    if not order:
        raise RuntimeError(
            "no Jev provider is configured: add an OpenRouter key"
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


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return _SECRET_TEXT.sub(r"\1\2[REDACTED]", value)
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if any(
                    word in str(key).lower()
                    for word in ("token", "password", "secret", "api_key", "credential")
                )
                else redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    return value


class Policy:
    allowed_actions = frozenset(
        {
            "notify",
            "ask_user",
            "light.turn_on",
            "light.turn_off",
            "switch.turn_on",
            "switch.turn_off",
            "climate.set_temperature",
        }
    )

    def authorize(self, action: str | None) -> bool:
        return action in self.allowed_actions


class SentinelWorkflow:
    def __init__(
        self, provider: OpenRouterJev | LayaJev, policy: Policy | None = None
    ) -> None:
        self.provider = provider
        self.policy = policy or Policy()

    def review(self, case: Case) -> Decision:
        return self.provider.decide(
            {
                "case": redact(case.to_dict()),
                "allowed_actions": sorted(self.policy.allowed_actions),
            }
        )


def verify(expected: Any, actual: Any, *, available: bool = True) -> dict[str, Any]:
    if not available:
        return {
            "verified": False,
            "status": "unavailable",
            "expected": expected,
            "actual": actual,
            "next_step": "notify_and_retry",
        }
    matched = expected == actual
    return {
        "verified": matched,
        "status": "matched" if matched else "mismatch",
        "expected": expected,
        "actual": actual,
        "next_step": "close_case" if matched else "reopen_case",
    }
