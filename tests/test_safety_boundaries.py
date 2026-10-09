"""Regression tests for the redaction gap and the validator gap.

Both were found by an independent review of the Home Assistant integration and
are pinned here so they cannot come back.

- `redact()` matched a literal substring list against a lowercased key, so
  `api-key`, `apiKey`, `api key` and `Authorization` did not match while
  `api_key` did. A secret stored under one of those spellings was sent to the
  provider in cleartext.
- Only the Clef route called `validate_answers`. The Jev and Laya routes went
  straight from `json.loads` to `decision_from_body`, so an off-rubric answer was
  accepted at whatever confidence it claimed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.error import URLError

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "custom_components" / "jev_sentinel"))

import runtime  # noqa: E402

import sentinel.jev as pkg  # noqa: E402
from sentinel.redaction import redact as pkg_redact  # noqa: E402

SECRET = "sk-live-ZZZUNIQUE"

SECRET_KEYS = [
    "api_key",
    "api-key",
    "apiKey",
    "API KEY",
    "api key",
    "API_KEY",
    "access_token",
    "accessToken",
    "password",
    "secret",
    "client_secret",
    "credential",
    "Authorization",
    "authorization",
]

# Keys that must NOT be redacted: they are configuration or content, not secrets.
NON_SECRET_KEYS = ["notes", "door_pin", "laya_base_url", "entity_id", "action"]


# --------------------------------------------------------------------------
# redaction
# --------------------------------------------------------------------------


@pytest.mark.parametrize("key", SECRET_KEYS)
def test_package_redacts_every_credential_spelling(key):
    assert SECRET not in str(pkg_redact({key: SECRET}))


@pytest.mark.parametrize("key", SECRET_KEYS)
def test_runtime_redacts_every_credential_spelling(key):
    """The two copies must agree. They drifted before."""
    assert SECRET not in str(runtime.redact({key: SECRET}))


@pytest.mark.parametrize("key", NON_SECRET_KEYS)
def test_non_secret_keys_are_left_alone(key):
    assert pkg_redact({key: SECRET})[key] == SECRET


def test_redaction_is_recursive():
    payload = {"outer": {"api-key": SECRET}, "list": [{"apiKey": SECRET}]}
    assert SECRET not in json.dumps(pkg_redact(payload))


def test_redaction_leaves_non_credential_strings_alone():
    assert pkg_redact({"notes": "the api key is stored in the vault"}) == {
        "notes": "the api key is stored in the vault"
    }


# --------------------------------------------------------------------------
# the rubric boundary is provider-independent
# --------------------------------------------------------------------------

OFF_RUBRIC_BODY = {
    "answers": {
        "outcome": {"choice": "TOTALLY_MADE_UP_OUTCOME", "probability": 1.0},
        "action": {"choice": "cover.open_garage", "probability": 1.0},
        "score": {"score": 99, "probability": 1.0},
    }
}


class _StubResponse:
    """Stands in for the object `urlopen(...)` yields inside a `with` block."""

    def __init__(self, body):
        self._payload = json.dumps(body).encode()

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _StubTransport:
    """Returns one fixed body in place of the network.

    It is a context manager because every provider does
    `with urlopen(request, ...) as response:`. An earlier version of this test
    returned the dict directly, so the providers raised
    `AttributeError('__enter__')` before reaching any validation. The test then
    passed on 3.11 while asserting on an unrelated exception's message, and
    failed on 3.10. It was not testing the rubric at all.
    """

    def __init__(self, body):
        self.body = body

    def __call__(self, *args, **kwargs):
        return _StubResponse(self.body)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Any accidental real request fails the test rather than reaching a provider."""

    def _blocked(*args, **kwargs):
        raise URLError("network is blocked in this test")

    monkeypatch.setattr(pkg, "urlopen", _blocked)
    monkeypatch.setattr(runtime, "urlopen", _blocked)


def _answers_of(questions):
    return {q["id"]: q for q in questions}


def test_off_rubric_answer_is_refused_on_the_package_jev_route(monkeypatch):
    provider = pkg.OpenRouterJev("key")
    monkeypatch.setattr(pkg, "urlopen", _StubTransport(OFF_RUBRIC_BODY))
    with pytest.raises(ValueError) as exc:
        provider.decide({})
    # ValueError, not a bare Exception: the validator raises ValueError, and
    # `pytest.raises(Exception)` also catches the AttributeError a broken stub
    # produces. An earlier version of this stub returned the body directly
    # instead of a context manager, so the provider raised AttributeError
    # before reaching validation and the test passed on the wrong failure.
    # The validator names the offending question, not the rejected choice.
    assert "unknown choice for 'outcome'" in str(exc.value), str(exc.value)


def test_off_rubric_answer_is_refused_on_the_runtime_jev_route(monkeypatch):
    provider = runtime.OpenRouterJev("key")
    monkeypatch.setattr(runtime, "urlopen", _StubTransport(OFF_RUBRIC_BODY))
    with pytest.raises(ValueError) as exc:
        provider.decide({})
    # ValueError, not a bare Exception, so an AttributeError from a broken stub
    # cannot pass this as a rubric refusal. See the note in the first of these.
    assert "unknown choice for 'outcome'" in str(exc.value), str(exc.value)


def test_off_rubric_answer_is_refused_on_the_package_laya_route(monkeypatch):
    provider = pkg.LayaJev("http://127.0.0.1:8123")
    monkeypatch.setattr(pkg, "urlopen", _StubTransport(OFF_RUBRIC_BODY))
    with pytest.raises(ValueError) as exc:
        provider.decide({})
    # ValueError, not a bare Exception, so an AttributeError from a broken stub
    # cannot pass this as a rubric refusal. See the note in the first of these.
    assert "unknown choice for 'outcome'" in str(exc.value), str(exc.value)


def test_off_rubric_answer_is_refused_on_the_runtime_laya_route(monkeypatch):
    provider = runtime.LayaJev("http://127.0.0.1:8123")
    monkeypatch.setattr(runtime, "urlopen", _StubTransport(OFF_RUBRIC_BODY))
    # ValueError, not a bare Exception: the validator raises ValueError, and a
    # blind `pytest.raises(Exception)` also passes on the AttributeError a
    # broken stub produces, which is the failure this file already had once.
    with pytest.raises(ValueError) as exc:
        provider.decide({})
    assert "unknown choice for 'outcome'" in str(exc.value), str(exc.value)


def test_every_route_refuses_the_same_body():
    """The documented promise is that the rubric applies whatever answered."""
    questions = pkg.decision_questions()
    with pytest.raises(ValueError) as package_exc:
        pkg.validate_answers(OFF_RUBRIC_BODY["answers"], questions, label="t")
    with pytest.raises(ValueError) as runtime_exc:
        runtime.validate_answers(OFF_RUBRIC_BODY["answers"], questions, label="t")
    # The two copies have to refuse identically, not merely both refuse. The
    # label is the only difference between the two messages.
    assert str(package_exc.value) == str(runtime_exc.value), (
        str(package_exc.value),
        str(runtime_exc.value),
    )
    assert "unknown choice for 'outcome'" in str(package_exc.value)


# A complete, on-rubric answer set, so the out-of-range score is the only thing
# wrong with the payload. The questions are `outcome` and `action` (choices) and
# `confidence` (a score), so a payload carrying only `score` was refused for a
# missing answer before it was ever checked against the legend.
VALID_ANSWERS = {
    "outcome": {"choice": "ignore", "probability": 0.6},
    "action": {"choice": "notify", "probability": 0.6},
    "confidence": {"score": 0, "probability": 0.6},
}


def test_a_complete_answer_set_is_accepted():
    """The baseline the score test needs: this payload is otherwise valid."""
    questions = pkg.decision_questions()
    pkg.validate_answers(VALID_ANSWERS, questions, label="t")
    runtime.validate_answers(VALID_ANSWERS, questions, label="t")


def test_an_out_of_range_score_is_refused_not_clamped():
    """`confidence_from_score` used to clamp 99 to 1.0 silently."""
    questions = pkg.decision_questions()
    out_of_range = dict(VALID_ANSWERS, confidence={"score": 99, "probability": 0.6})
    with pytest.raises(ValueError) as package_exc:
        pkg.validate_answers(out_of_range, questions, label="t")
    with pytest.raises(ValueError) as runtime_exc:
        runtime.validate_answers(out_of_range, questions, label="t")
    # The refusal has to name the score, not a missing answer that was never
    # reached: this payload is complete, so the only fault left is the index.
    assert "index 99 is outside the 0 to 4 legend scale" in str(package_exc.value), str(
        package_exc.value
    )
    assert str(package_exc.value) == str(runtime_exc.value)
