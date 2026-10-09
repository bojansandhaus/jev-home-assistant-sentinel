"""The smallest complete Sentinel decision loop."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, Protocol

from .models import Case, Decision, Verification
from .policy import Policy
from .redaction import redact
from .verifier import verify


class DecisionProvider(Protocol):
    def decide(self, state: dict[str, Any]) -> Decision: ...


class SentinelWorkflow:
    """Interpret, authorize, dispatch, and verify one bounded case."""

    def __init__(
        self, provider: DecisionProvider, policy: Policy | None = None
    ) -> None:
        self.provider = provider
        self.policy = policy or Policy()

    def review(self, case: Case) -> Decision:
        state = {
            "case": redact(case.to_dict()),
            "policy": {"allowed_actions": sorted(self.policy.allowed_actions)},
        }
        return self.provider.decide(state)

    def execute(
        self,
        case: Case,
        decision: Decision,
        dispatch: Callable[[str], Any],
        readback: Callable[[], Any],
        *,
        user_approved: bool = False,
    ) -> dict[str, Any]:
        if decision.shadow:
            authorization = {
                "allowed": False,
                "status": "shadow_only",
                "reason": "Shadow decisions are recommendations and cannot be dispatched.",
            }
        else:
            authorization = self.policy.authorize(
                decision.action, user_approved=user_approved
            )
        result: dict[str, Any] = {
            "case": case.to_dict(),
            "decision": decision.to_dict(),
            "authorization": authorization,
        }
        if not authorization["allowed"]:
            result["verification"] = Verification(
                "not_executed", False, next_step="notify_user"
            ).to_dict()
            return result
        try:
            dispatch_result = dispatch(decision.action or "")
        except Exception as exc:  # the caller receives a safe, structured failure
            result["dispatch"] = {"status": "error", "error_type": type(exc).__name__}
            result["verification"] = Verification(
                "dispatch_failed", False, next_step="notify_and_retry"
            ).to_dict()
            return result
        # An awaitable result means the dispatcher was async. In Home Assistant
        # every service call is, so this was the ordinary case rather than the
        # exotic one: the call returned a coroutine, this code recorded
        # `{"status": "sent", "result_type": "coroutine"}`, the coroutine was
        # garbage-collected without ever being awaited, and the device never
        # moved. The readback then compared the state the device was already in
        # and closed the case as a successfully verified action that physically
        # did not happen.
        #
        # A synchronous boundary cannot honour an awaitable, so it refuses
        # rather than reporting success. Escalating to an async caller is the
        # fix, and refusing is what makes that visible.
        if inspect.isawaitable(dispatch_result):
            if hasattr(dispatch_result, "close"):
                dispatch_result.close()  # do not leave a never-awaited coroutine
            result["dispatch"] = {
                "status": "unsupported_dispatch",
                "result_type": "awaitable",
            }
            result["verification"] = Verification(
                "dispatch_not_performed", False, next_step="notify_and_retry"
            ).to_dict()
            return result
        result["dispatch"] = {
            "status": "sent",
            "result_type": type(dispatch_result).__name__,
        }
        try:
            actual = readback()
            result["verification"] = verify(case.facts.get("expected_state"), actual)
        except Exception as exc:
            result["verification"] = {
                "verified": False,
                "status": "readback_failed",
                "error_type": type(exc).__name__,
                "next_step": "reopen_case",
            }
        return result
