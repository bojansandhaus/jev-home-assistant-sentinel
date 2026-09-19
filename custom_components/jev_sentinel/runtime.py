"""Self contained runtime used when Home Assistant loads the component.

Home Assistant installs custom_components without installing the repository's
Python package, so this small bridge keeps the integration independently usable.
The public ``sentinel`` package carries the same provider neutral contract for
applications and tests outside Home Assistant.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.request import Request, urlopen

ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
MODEL = "typesafe/jev-1.13"


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


class OpenRouterJev:
    def __init__(self, api_key: str | None = None, *, timeout: float = 30.0) -> None:
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        self.timeout = timeout

    def decide(self, state: dict[str, Any]) -> Decision:
        if not self.api_key or self.api_key == "***":
            raise RuntimeError(
                "Configure an OpenRouter API key before requesting a Jev review"
            )
        questions = {
            "outcome": {
                "type": "choice",
                "instructions": "Choose the safest next outcome for this Home Assistant case.",
                "criteria": {
                    "ignore": "No meaningful action",
                    "notify": "Tell the user",
                    "ask_user": "Need approval or clarification",
                    "recommend": "Recommend a safe action",
                    "escalate": "Treat as unresolved or risky",
                },
            },
            "action": {
                "type": "choice",
                "instructions": "Choose one action only when supported by the case.",
                "criteria": {
                    "notify": "Notify the user",
                    "ask_user": "Ask the user",
                    "light.turn_off": "Turn off a light",
                    "switch.turn_off": "Turn off a switch",
                    "climate.set_temperature": "Set a temperature",
                    "none": "No device action",
                },
            },
            "confidence": {
                "type": "score",
                "instructions": "Score confidence from 0 to 1.",
                "criteria": {"min": 0, "max": 1},
            },
        }
        payload = {"model": MODEL, "state": state, "questions": questions}
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
        answers = body["answers"]
        action = answers["action"]["choice"]
        return Decision(
            answers["outcome"]["choice"],
            answers["outcome"]["choice"].replace("_", " "),
            answers["confidence"].get("score"),
            None if action == "none" else action,
            True,
            {"model": body.get("model", MODEL)},
        )


def redact(value: Any) -> Any:
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
    def __init__(self, provider: OpenRouterJev, policy: Policy | None = None) -> None:
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
