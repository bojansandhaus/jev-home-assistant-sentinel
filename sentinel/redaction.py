"""Redact household details before optional provider calls."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_SECRET_WORDS = ("token", "password", "secret", "api_key", "apikey", "credential")
_SECRET_TEXT = re.compile(
    r"(?i)(api[_ -]?key|token|password|secret|credential)(\s*[:=]\s*)[^\s,;]+"
)


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return _SECRET_TEXT.sub(r"\1\2[REDACTED]", value)
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
