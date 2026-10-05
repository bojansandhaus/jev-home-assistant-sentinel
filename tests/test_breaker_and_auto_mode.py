"""The local-failure breaker and the meaning of an untouched local model.

Two defects found by review, both with a privacy consequence, both pinned here.

**The breaker never healed.** It is a process global with no clock: three
consecutive local failures that qualified for the hosted fallback still fall
back, and every failure after that was suppressed until Home Assistant restarted.
With two config entries, one entry's dead local server therefore denied the
other entry its fallback indefinitely, even once the server came back.

**`auto` gained a local hop nobody asked for.** The config form pre-filled
`local_model` with `"laya"`, and `resolve_auto_mode` treats any non-`None`
`local_model` as an explicit choice, so `auto` always resolved to
`api_with_local_fallback`. A user who selected `auto` and touched nothing got a
local hop they had not asked for, which is the opposite of what `auto` promised.

The third defect here is one of logging: `note_local_failure` logged "considering
the hosted fallback" before comparing the count against the limit, so a
suppressed call logged that it was about to fall back and then did not.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "custom_components" / "jev_sentinel"))

import runtime  # noqa: E402

MODULES = [runtime]
try:
    import sentinel.jev as package_copy

    MODULES.append(package_copy)
except ImportError:  # pragma: no cover
    pass


@pytest.fixture(autouse=True)
def _reset_breaker():
    for module in MODULES:
        module.reset_local_failures()
        module.LOCAL_FALLBACK_COOLDOWN_SECONDS = 300.0
    yield
    for module in MODULES:
        module.reset_local_failures()
        module.LOCAL_FALLBACK_COOLDOWN_SECONDS = 300.0


class TestBreakerTripsAndHeals:
    @pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__)
    def test_the_first_three_failures_may_still_fall_back(self, module):
        assert [module.note_local_failure(RuntimeError("x")) for _ in range(3)] == [
            True,
            True,
            True,
        ]

    @pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__)
    def test_the_fourth_and_later_are_suppressed(self, module):
        for _ in range(3):
            module.note_local_failure(RuntimeError("x"))
        assert module.note_local_failure(RuntimeError("x")) is False
        assert module.note_local_failure(RuntimeError("x")) is False

    @pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__)
    def test_the_counter_decays_after_the_cooldown(self, module):
        """The defect: no clock anywhere, so a server that came back stayed
        suppressed for the life of the Home Assistant process."""
        for _ in range(module.LOCAL_FALLBACK_FAILURE_LIMIT + 1):
            module.note_local_failure(RuntimeError("x"))
        assert module.local_failure_count() > module.LOCAL_FALLBACK_FAILURE_LIMIT

        module.LOCAL_FALLBACK_COOLDOWN_SECONDS = 0.0
        assert module.local_failure_count() == 0, (
            "the breaker must reset once the cooldown has elapsed with no "
            "new failure"
        )
        # And it must actually fall back again, not just report zero.
        assert module.note_local_failure(RuntimeError("x")) is True

    @pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__)
    def test_a_fresh_failure_restarts_the_cooldown(self, module):
        module.LOCAL_FALLBACK_COOLDOWN_SECONDS = 100.0
        for _ in range(module.LOCAL_FALLBACK_FAILURE_LIMIT + 1):
            module.note_local_failure(RuntimeError("x"))
        module.LOCAL_FALLBACK_COOLDOWN_SECONDS = 0.0
        assert module.local_failure_count() == 0
        # A new failure re-arms it, so it does not decay while the server is
        # still down.
        module.note_local_failure(RuntimeError("x"))
        module.LOCAL_FALLBACK_COOLDOWN_SECONDS = 100.0
        assert module.local_failure_count() == 1

    @pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__)
    def test_a_success_resets_immediately(self, module):
        for _ in range(module.LOCAL_FALLBACK_FAILURE_LIMIT + 1):
            module.note_local_failure(RuntimeError("x"))
        module.reset_local_failures()
        assert module.local_failure_count() == 0
        assert module.note_local_failure(RuntimeError("x")) is True

    @pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__)
    def test_the_cooldown_is_a_positive_finite_number(self, module):
        """A zero default would make the breaker useless, since every read would
        decay it."""
        assert isinstance(module.LOCAL_FALLBACK_COOLDOWN_SECONDS, float)
        assert module.LOCAL_FALLBACK_COOLDOWN_SECONDS > 0


class TestBreakerLoggingIsHonest:
    @pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__)
    def test_a_suppressed_call_does_not_say_it_is_falling_back(self, module, caplog):
        for _ in range(module.LOCAL_FALLBACK_FAILURE_LIMIT):
            module.note_local_failure(RuntimeError("x"))
        with caplog.at_level("WARNING"):
            module.note_local_failure(RuntimeError("x"))
        text = caplog.text
        assert "considering the hosted fallback" not in text
        assert "suppressing" in text

    @pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__)
    def test_an_allowed_call_says_it_is_using_the_fallback(self, module, caplog):
        with caplog.at_level("WARNING"):
            assert module.note_local_failure(RuntimeError("x")) is True
        assert "using the hosted fallback" in caplog.text

    @pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__)
    def test_only_the_exception_class_name_is_logged(self, module, caplog):
        """The privacy property: the case, entity state and answer never reach a
        log record, so a secret in the exception message must not either."""
        secret = "sk-live-SECRET-VALUE"
        with caplog.at_level("WARNING"):
            module.note_local_failure(RuntimeError(f"failed for {secret}"))
        assert secret not in caplog.text
        assert "RuntimeError" in caplog.text


class TestAutoMeansWhatItSays:
    def test_auto_is_api_only_when_no_local_model_was_named(self):
        assert runtime.resolve_auto_mode(local_model=None) == runtime.API_ONLY

    def test_auto_uses_the_local_fallback_when_a_model_was_named(self):
        assert (
            runtime.resolve_auto_mode(local_model="laya")
            == runtime.API_WITH_LOCAL_FALLBACK
        )

    def test_local_model_configured_is_false_only_for_none(self):
        assert runtime.local_model_configured(None) is False
        assert runtime.local_model_configured("laya") is True
        assert runtime.local_model_configured("") is True

    def test_the_form_does_not_prefill_a_local_model(self):
        """The defect: a pre-filled default is indistinguishable from a choice,
        so every `auto` entry silently gained a local hop."""
        source = (
            REPO / "custom_components" / "jev_sentinel" / "config_flow.py"
        ).read_text()
        assert (
            'LOCAL_MODEL_FIELD, default=""' in source
        ), "the local model field must default to empty so opting in is explicit"
        # A pre-filled default is what made an untouched entry look configured.
        assert "LOCAL_MODEL_FIELD, default=LOCAL_MODEL" not in source

    def test_an_untouched_value_is_normalised_to_none(self):
        """Even if an old entry stored the default name, `auto` must not read it
        as an explicit choice."""
        source = (
            REPO / "custom_components" / "jev_sentinel" / "config_flow.py"
        ).read_text()
        assert (
            "if local_model == LOCAL_MODEL:" in source
        ), "an untouched value equal to the default must be normalised to None"
        assert "local_model = None" in source
