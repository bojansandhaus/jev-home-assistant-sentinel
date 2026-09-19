"""Deterministic authority boundary around Jev recommendations."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Policy:
    """Allow only declared reversible actions unless the user approves."""

    allowed_actions: frozenset[str] = frozenset(
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
    approval_required: frozenset[str] = frozenset(
        {
            "lock.unlock",
            "alarm_control_panel.alarm_disarm",
            "cover.open_garage",
            "water_valve.close",
        }
    )

    def authorize(
        self, action: str | None, *, user_approved: bool = False
    ) -> dict[str, object]:
        if not action:
            return {
                "allowed": False,
                "status": "no_action",
                "reason": "No action was proposed.",
            }
        if action not in self.allowed_actions and action not in self.approval_required:
            return {
                "allowed": False,
                "status": "not_allowlisted",
                "reason": "Action is not in the Sentinel policy.",
            }
        if action in self.approval_required and not user_approved:
            return {
                "allowed": False,
                "status": "approval_required",
                "reason": "This action needs explicit user approval.",
            }
        return {
            "allowed": True,
            "status": "approved",
            "reason": "Policy allows this action.",
        }
