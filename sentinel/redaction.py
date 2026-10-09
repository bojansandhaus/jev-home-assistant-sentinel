"""Redact household details before optional provider calls."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_SECRET_WORDS = ("token", "password", "secret", "apikey", "credential", "authorization")

# Segments, not substrings. A key is split on its separators, so `private_key`
# yields the segment `key` while `door_pin` yields `pin`. `pin` must stay clear:
# a door's PIN code is household data this policy is allowed to see, and the
# existing suite locks that in. Substring matching cannot express the
# difference -- `doorpin` contains `pin` exactly as `privatekey` contains `key`
# -- so the two are told apart by which segment they are, not by whether one
# contains the other.
_SECRET_SEGMENTS = frozenset(
    {
        "key",
        "keys",
        "passwd",
        "passphrase",
        "authorisation",
        "authorisations",
        "bearer",
        "cookie",
        "clientid",
        "sessionid",
        "otp",
        "signature",
        "privatekey",
        "accesskey",
        "secretkey",
        "apikey",
        "authtoken",
        "refreshtoken",
    }
)


def _key_is_secret(key: Any) -> bool:
    """True when a mapping key names a credential.

    Separators and case are normalised first, so `api-key`, `apiKey`, `api key`
    and `API_KEY` are one word. Matching the raw lowercase key against `api_key`
    let the other three spellings through and sent those values to the provider
    in cleartext.

    The key is split into segments as well as flattened. Flattening alone caught
    `apikey` and `api_token` and missed `private_key`, `access_key`, `key`,
    `passwd`, `passphrase`, `authorisation`, `cookie`, `clientid` and `bearer`
    -- nine spellings that carried their values to the provider and onto the
    event bus in cleartext.
    """
    normalized = str(key).replace("-", "").replace("_", "").replace(" ", "").lower()
    if any(word in normalized for word in _SECRET_WORDS):
        return True
    segments = [s for s in re.split(r"[^A-Za-z0-9]+", str(key).lower()) if s]
    return any(segment in _SECRET_SEGMENTS for segment in segments)


# A key and its value. The value may be quoted: JSON writes
# `{"password": "hunter2"}`, and the previous form required the colon to follow
# the word immediately, so the intervening quote stopped the match and the whole
# payload passed through unredacted.
_SECRET_TEXT = re.compile(
    r"(?i)(api[_ -]?key|token|passwd|passphrase|password|secret|credential"
    r"|authorisation?|bearer|cookie)([\"']?\s*[:=]\s*[\"']?)[^\s,;]+"
)

# `Authorization: Bearer sk-live-...` and the bare `Bearer sk-live-...` form.
# Applied before _SECRET_TEXT, whose wider value match would otherwise consume
# the word `Bearer` and leave the token itself behind it.
#
# A prose form was tried here too and removed: matching `the password is X`
# also matched `the api key is stored in the vault`, which the suite pins as
# text that must pass through untouched, because it names no credential. A
# regex cannot tell a value from the next word, and a redactor that damages
# legitimate household text is its own failure. The forms above all follow a
# delimiter, which is what makes them carry a value.
_BEARER_TEXT = re.compile(r"(?i)\b(bearer)(\s+)[A-Za-z0-9._~+/=-]+")


def redact(value: Any) -> Any:
    if isinstance(value, str):
        text = _BEARER_TEXT.sub(r"\1\2[REDACTED]", value)
        return _SECRET_TEXT.sub(r"\1\2[REDACTED]", text)
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
