"""The smallest complete Sentinel decision loop."""

from __future__ import annotations

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
