"""Redact household details before optional provider calls."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_SECRET_WORDS = ("token", "password", "secret", "apikey", "credential", "authorization")


def _key_is_secret(key: Any) -> bool:
    """True when a mapping key names a credential.

    Separators and case are normalised first, so `api-key`, `apiKey`, `api key`
    and `API_KEY` are one word. Matching the raw lowercase key against `api_key`
    let the other three spellings through and sent those values to the provider
    in cleartext.
    """
    normalized = str(key).replace("-", "").replace("_", "").replace(" ", "").lower()
    return any(word in normalized for word in _SECRET_WORDS)


_SECRET_TEXT = re.compile(
    r"(?i)(api[_ -]?key|token|password|secret|credential)(\s*[:=]\s*)[^\s,;]+"
)


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return _SECRET_TEXT.sub(r"\1\2[REDACTED]", value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            result[str(key)] = "[REDACTED]" if _key_is_secret(key) else redact(item)
        return result
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    return value
