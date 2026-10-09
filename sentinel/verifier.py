"""Deterministic Home Assistant state readback checks."""

from __future__ import annotations

from typing import Any


def verify(expected: Any, actual: Any, *, available: bool = True) -> dict[str, Any]:
    if not available:
        return {
            "verified": False,
            "status": "unavailable",
            "expected": expected,
            "actual": actual,
            "next_step": "notify_and_retry",
        }
    # Neither side being known is not a match. The expectation reaches this
    # function from a magic key inside an untyped facts dict, so a case built
    # without one had no verification requirement at all; and an entity renamed
    # or removed between dispatch and readback makes the readback None. The two
    # compare equal, so both closed the case as a successfully verified device
    # action with no state readback performed.
    #
    # The causes are reported apart because they mean different things to an
    # operator: no expectation is a case that was never given one, and no
    # readback is a device this integration could not see.
    if expected is None or actual is None:
        return {
            "verified": False,
            "status": "no_expectation" if expected is None else "unavailable",
            "expected": expected,
            "actual": actual,
            "next_step": "notify_and_retry",
        }
    if actual == expected:
        return {
            "verified": True,
            "status": "matched",
            "expected": expected,
            "actual": actual,
            "next_step": "close_case",
        }
    return {
        "verified": False,
        "status": "mismatch",
        "expected": expected,
        "actual": actual,
        "next_step": "reopen_case",
    }
