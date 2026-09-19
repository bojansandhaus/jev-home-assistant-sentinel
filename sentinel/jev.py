"""Typed Jev adapter boundary.

The public core accepts any DecisionProvider. This adapter is intentionally
small so the project can use OpenRouter without importing Hermes internals.
"""
from __future__ import annotations

import json
import os
from urllib.request import Request, urlopen

from .models import Decision

ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
MODEL = "typesafe/jev-1.13"


class OpenRouterJev:
    def __init__(self, api_key: str | None = None, *, timeout: float = 30.0) -> None:
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        self.timeout = timeout

    def decide(self, state: dict) -> Decision:
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY is required for live Jev decisions")
        questions = {
            "outcome": {
                "type": "choice",
                "instructions": "Choose the safest next outcome for this Home Assistant case.",
                "criteria": {"ignore": "No meaningful action", "notify": "Tell the user", "ask_user": "Need user approval or clarification", "recommend": "Recommend a safe allowlisted action", "escalate": "Treat as unresolved or risky"},
            },
            "action": {
                "type": "choice",
                "instructions": "Choose one action only if it is supported by the case and policy.",
                "criteria": {"notify": "Notify the user", "ask_user": "Ask the user", "light.turn_off": "Turn off a light", "switch.turn_off": "Turn off a switch", "climate.set_temperature": "Set a climate temperature", "none": "No device action"},
            },
            "confidence": {
                "type": "score",
                "instructions": "Score confidence in the selected outcome from 0 to 1.",
                "criteria": {"min": 0, "max": 1},
            },
        }
        payload = {"model": MODEL, "state": state, "questions": questions}
        request = Request(ENDPOINT, data=json.dumps(payload).encode(), method="POST", headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json", "X-Title": "Jev Home Sentinel"})
        with urlopen(request, timeout=self.timeout) as response:
            body = json.loads(response.read().decode())
        answers = body["answers"]
        outcome = answers["outcome"]["choice"]
        selected = answers["action"]["choice"]
        return Decision(outcome, outcome.replace("_", " "), answers["confidence"].get("score"), None if selected == "none" else selected, shadow=True, raw={"model": body.get("model", MODEL)})
