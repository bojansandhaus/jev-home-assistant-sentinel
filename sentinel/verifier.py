"""Deterministic Home Assistant state readback checks."""
from __future__ import annotations

from typing import Any


def verify(expected: Any, actual: Any, *, available: bool = True) -> dict[str, Any]:
    if not available:
        return {"verified": False, "status": "unavailable", "expected": expected, "actual": actual, "next_step": "notify_and_retry"}
    if actual == expected:
        return {"verified": True, "status": "matched", "expected": expected, "actual": actual, "next_step": "close_case"}
    return {"verified": False, "status": "mismatch", "expected": expected, "actual": actual, "next_step": "reopen_case"}
