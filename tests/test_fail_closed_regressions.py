"""Regressions for three ways the safety boundary reported success it had not earned.

All three ran on the ordinary path: the verifier on every case, the dispatcher on
every device action, the redactor on every provider call. The two-copy layout of
this repository's safety layer is exercised deliberately — every test runs against
`sentinel/` and against `custom_components/jev_sentinel/runtime.py`, because that
duplicate is what let a fix land in one copy and not the other.

Nothing here makes a provider call, and nothing here touches Home Assistant.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sentinel.models import Case, Decision  # noqa: E402
from sentinel.policy import Policy  # noqa: E402
from sentinel.redaction import redact  # noqa: E402
from sentinel.verifier import verify  # noqa: E402
from sentinel.workflow import SentinelWorkflow  # noqa: E402


def _load_runtime():
    """Load the shipped Home Assistant bridge, the second copy of the layer."""
    path = ROOT / "custom_components" / "jev_sentinel" / "runtime.py"
    spec = importlib.util.spec_from_file_location("_regression_runtime", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: `runtime.py` declares dataclasses with forward
    # references, and the dataclass machinery resolves them through
    # `sys.modules[cls.__module__]`, which is not yet set for a module that has
    # only been constructed.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RUNTIME = _load_runtime()


class _Provider:
    def __init__(self, decision: Decision) -> None:
        self.decision = decision

    def decide(self, case: Case) -> Decision:
        return self.decision


# ---------------------------------------------------------------------------
# 1. A readback that returned nothing was reported as a successful readback
# ---------------------------------------------------------------------------


def test_a_missing_expectation_is_not_a_match() -> None:
    """`verify(None, None)` returned `{"verified": True, "status": "matched",
    "next_step": "close_case"}`.

    The expectation arrives from a magic key inside an untyped facts dict, so a
    case built without one had no verification requirement at all; an entity
    renamed or removed between dispatch and readback makes the readback None
    too. Both closed a case as a successfully verified device action with zero
    state readback performed.
    """
    result = verify(None, None)
    assert result["verified"] is False
    assert result["status"] == "no_expectation"
    assert result["next_step"] == "notify_and_retry"


def test_an_unreadable_device_is_not_a_match() -> None:
    """The other cause, reported apart from the first: the case had an
    expectation and the device could not be seen."""
    result = verify("off", None)
    assert result["verified"] is False
    assert result["status"] == "unavailable"
    assert result["next_step"] == "notify_and_retry"


def test_a_real_match_still_closes_the_case() -> None:
    """The refusal must not turn into an anything-goes gate. A readback that
    genuinely matched is still a successful verification."""
    result = verify("off", "off")
    assert result["verified"] is True
    assert result["status"] == "matched"
    assert result["next_step"] == "close_case"


def test_a_real_mismatch_is_still_a_mismatch() -> None:
    assert verify("off", "on")["status"] == "mismatch"
    assert verify("off", "on")["next_step"] == "reopen_case"


def test_the_shipped_copy_agrees() -> None:
    """The safety layer exists twice in this repository. A fix in one copy that
    does not reach the other is the same bug, still shipped."""
    assert RUNTIME.verify(None, None)["verified"] is False
    assert RUNTIME.verify("off", None)["verified"] is False
    assert RUNTIME.verify("off", "off")["verified"] is True
    assert RUNTIME.verify("off", "on")["verified"] is False


# ---------------------------------------------------------------------------
# 2. An async dispatcher was recorded as sent, and the device never moved
# ---------------------------------------------------------------------------


@pytest.fixture
def workflow():
    return SentinelWorkflow(_Provider(Decision("act", "turn off the lamp")))


@pytest.fixture
def case():
    return Case.create(
        "light.turn_off",
        requested_action="light.turn_off",
        facts={"expected_state": "off"},
    )


def _decision() -> Decision:
    return Decision("act", "turn off the lamp", action="light.turn_off", shadow=False)


def test_an_awaitable_dispatch_is_refused_not_reported_as_sent(workflow, case) -> None:
    """In Home Assistant every service call is async, so `dispatch` returned a
    coroutine. This code recorded `{"status": "sent", "result_type":
    "coroutine"}`, the coroutine was collected without ever being awaited, and
    the device never moved. The readback then compared the state the device was
    already in and closed the case as a successful action that physically did
    not happen.
    """
    moved: list[str] = []

    async def async_dispatch(action: str):
        await asyncio.sleep(0)
        moved.append(action)

    with warnings.catch_warnings():
        # A never-awaited coroutine is an error here, which is the point: the
        # fix closes it rather than leaving it for the collector.
        warnings.simplefilter("error")
        result = workflow.execute(case, _decision(), async_dispatch, lambda: "off")

    assert result["dispatch"]["status"] == "unsupported_dispatch"
    assert result["verification"]["verified"] is False
    assert result["verification"]["status"] == "dispatch_not_performed"
    assert moved == [], "the device action was not performed"


def test_a_synchronous_dispatch_still_works(workflow, case) -> None:
    """The refusal must not make the integration unusable for a host that
    dispatches synchronously."""
    result = workflow.execute(
        case, _decision(), lambda action: {"moved": True}, lambda: "off"
    )
    assert result["dispatch"]["status"] == "sent"
    assert result["verification"]["status"] == "matched"
    assert result["verification"]["next_step"] == "close_case"


def test_a_failing_dispatch_is_still_an_error(workflow, case) -> None:
    def boom(action: str):
        raise RuntimeError("service unavailable")

    result = workflow.execute(case, _decision(), boom, lambda: "off")
    assert result["dispatch"]["status"] == "error"
    assert result["verification"]["next_step"] == "notify_and_retry"


# ---------------------------------------------------------------------------
# 3. Redaction missed spellings, and missed every structured form
# ---------------------------------------------------------------------------

SECRET = "sk-live-ZZZUNIQUE"


@pytest.mark.parametrize(
    "key",
    [
        "private_key",
        "access_key",
        "key",
        "passwd",
        "passphrase",
        "authorisation",
        "cookie",
        "clientid",
        "bearer",
        "api_token",
        "API-KEY",
        "Secret",
    ],
)
def test_a_credential_key_is_redacted(key: str) -> None:
    """The word list held six spellings. These nine more carried their values to
    the provider and onto the Home Assistant event bus in cleartext."""
    assert redact({key: SECRET})[key] == "[REDACTED]", key


@pytest.mark.parametrize(
    "text",
    [
        '{"password": "hunter2"}',
        "Authorization: Bearer sk-live-X",
        "Bearer sk-live-X",
        "api_key=abc123",
    ],
)
def test_a_credential_string_is_redacted(text: str) -> None:
    """Every one of these passed through untouched. JSON quotes stopped the
    match, because the old pattern required the colon to follow the word
    immediately."""
    result = redact(text)
    assert result != text, text
    for token in ("hunter2", "sk-live-X", "abc123"):
        assert token not in result, text


@pytest.mark.parametrize(
    "key",
    [
        "notes",
        "door_pin",
        "laya_base_url",
        "entity_id",
        "action",
        "window_open_minutes",
    ],
)
def test_household_data_is_not_treated_as_a_credential(key: str) -> None:
    """The widening is by segment, not by substring, and that distinction is the
    whole design. `private_key` yields the segment `key`; `door_pin` yields
    `pin`, and a door's PIN code is household data the policy is allowed to see.
    Substring matching cannot express the difference — `doorpin` contains `pin`
    exactly as `privatekey` contains `key` — so the test suite pins both sides.
    """
    assert redact({key: "value"})[key] == "value", key


def test_prose_that_names_no_credential_is_left_alone() -> None:
    """A prose form was tried and removed. Matching `the password is X` also
    matched `the api key is stored in the vault`, which names no credential; a
    redactor that damages legitimate household text is its own failure, and a
    regex cannot tell a value from the next word."""
    sentence = "the api key is stored in the vault"
    assert redact(sentence) == sentence


def test_the_shipped_copy_agrees_on_redaction() -> None:
    """The second copy of the layer must not drift from the first."""
    assert RUNTIME.redact({"private_key": SECRET})["private_key"] == "[REDACTED]"
    assert RUNTIME.redact({"door_pin": "1234"})["door_pin"] == "1234"
    assert "sk-live" not in RUNTIME.redact("Bearer sk-live-X")


def test_redaction_is_still_recursive() -> None:
    payload = {"outer": {"private_key": SECRET}, "list": [{"accessKey": SECRET}]}
    assert SECRET not in json.dumps(redact(payload))
