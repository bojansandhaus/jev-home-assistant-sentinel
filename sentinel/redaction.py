"""Redact household details before optional provider calls."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_SECRET_WORDS = ("token", "password", "secret", "api_key", "apikey", "credential")


def redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key).lower()
            result[str(key)] = (
                "[REDACTED]"
                if any(word in key_text for word in _SECRET_WORDS)
                else redact(item)
            )
        return result
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    return value
