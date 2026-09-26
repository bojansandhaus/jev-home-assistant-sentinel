"""Typed Jev adapter boundary.

The public core accepts any DecisionProvider. Three adapters ship here:

- ``OpenRouterJev`` calls hosted Jev over an OpenRouter API key.
- ``LayaJev`` calls a local ``laya-serve`` process on this machine. It needs no
  key.
- ``ChainedJev`` answers from the first adapter in an ordered chain that
  succeeds, so a caller can put the local server first and a hosted key behind
  it.

Both single adapters send the same rubric and read the same Decisions shaped
answer, so a case reviewed locally and a case reviewed over the hosted route
produce the same ``Decision`` fields. The chain adds the name of the adapter
that answered to ``Decision.raw``. Nothing here imports Hermes internals.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .models import Decision

if TYPE_CHECKING:  # the chain is structural; nothing is imported at runtime
    from collections.abc import Sequence

    from .workflow import DecisionProvider

ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
MODEL = "typesafe/jev-1.13"

LAYA_BASE_URL = "http://127.0.0.1:8000"
LAYA_ENDPOINT_PATH = "/v1/systemone"
LAYA_MODEL = "convaiinnovations/laya"
# A CPU checkpoint answers in a couple of seconds, but a cold process or a
# first request after a restart is slower. The hosted 30 second default is too
# short for the local route, so it carries its own budget.
LAYA_TIMEOUT = 120.0
_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")

# A failed attempt falls through to the next provider in a chain only when the
# provider was unreachable, refused, rate limited, or broken. Any other status,
# such as 400 or 422, says the request itself was rejected, and the same request
# is not retried somewhere else.
FALLBACK_STATUS_CODES = frozenset({401, 403, 429})

# The confidence question is a ``score`` question, and a score rubric is an
# ordered list of level descriptions with index 0 first. A local Laya server
# rejects ``{"min": 0, "max": 1}`` with "a score question takes 'criteria' as a
# list of level descriptions, index 0 first". The level count therefore fixes
# the resolution of the derived confidence value, which
# ``confidence_from_score`` documents.
CONFIDENCE_LEVELS = (
    "very low confidence, the case is ambiguous or mostly missing",
    "low confidence",
    "moderate confidence",
    "high confidence",
    "very high confidence, the case points one way",
)


def decision_questions() -> dict[str, dict]:
    """The rubric both adapters send. A fresh mapping on every call."""
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

    A missing, non numeric, or single level answer returns ``None``, because no
    position on a 0 to 1 scale can be stated for it.
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
    outcome = answers["outcome"]["choice"]
    selected = answers["action"]["choice"]
    return Decision(
        outcome,
        outcome.replace("_", " "),
        confidence_from_score(answers.get("confidence")),
        None if selected == "none" else selected,
        shadow=True,
        raw={"model": body.get("model", model)},
    )


class OpenRouterJev:
    """Hosted Jev over an OpenRouter API key."""

    def __init__(self, api_key: str | None = None, *, timeout: float = 30.0) -> None:
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        self.timeout = timeout

    def decide(self, state: dict) -> Decision:
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY is required for live Jev decisions")
        payload = {"model": MODEL, "state": state, "questions": decision_questions()}
        request = Request(
            ENDPOINT,
            data=json.dumps(payload).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "X-Title": "Jev Home Sentinel",
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
    was started with its own bearer check, because an empty header is not the
    same request as no header.
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

    def decide(self, state: dict) -> Decision:
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

    The chain holds named adapters in the order they are tried. A local Laya hop
    can sit first with a hosted hop behind it, so a case costs no provider
    request while the local server answers, and a local outage still returns a
    decision. The adapter that answered is recorded in ``Decision.raw``.
    """

    def __init__(self, providers: "Sequence[tuple[str, DecisionProvider]]") -> None:
        if len(providers) < 2:
            raise ValueError("a chain needs at least two providers")
        self.providers: tuple[tuple[str, DecisionProvider], ...] = tuple(providers)

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
