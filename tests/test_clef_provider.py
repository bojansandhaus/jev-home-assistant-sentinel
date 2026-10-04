"""Cloudflare Clef as a hosted provider: selection, wire shape, and answers.

Clef is a Cloudflare hosted decision model, not a Jev endpoint, so these tests
cover what makes it a hosted provider of its own: it is selected by name, it
sits in a fallback order, it can lead a chain in front of hosted Jev, and it
takes two checkpoints rather than two provider names.

Three boundaries are load bearing and are proved here rather than assumed:

- a Clef-only review makes no request to OpenRouter and no request to the local
  Laya server;
- both Cloudflare response envelopes parse, and a ``success: false`` envelope
  surfaces Cloudflare's own error code;
- question ids are renamed into Clef's id alphabet and mapped back, so a caller
  never sees a renamed question.

The Cloudflare token lives in the environment and never in this file. No live
Cloudflare call was possible on the verification machine, because every
candidate token was refused with HTTP 401, so every Clef test here is mocked.
"""

import ast
import importlib.util
import json
import logging
import sys
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from sentinel import (
    CHAINED_PROVIDER,
    CLEF_ACCOUNT_ENV,
    CLEF_API_BASE,
    CLEF_CHAINED_PROVIDER,
    CLEF_DEFAULT_MODEL,
    CLEF_ID_MAX_LENGTH,
    CLEF_MAX_QUESTIONS,
    CLEF_MODELS,
    CLEF_TIMEOUT,
    CLEF_TOKEN_ENV,
    FALLBACK_STATUS_CODES,
    LAYA_BASE_URL,
    LAYA_ENDPOINT_PATH,
    LOCAL_PROVIDER,
    MODE_ALIASES,
    MODE_NAMES,
    PROVIDER_ENV,
    PROVIDER_MODES,
    Case,
    ChainedJev,
    ClefJev,
    LayaJev,
    OpenRouterJev,
    SentinelWorkflow,
    build_provider,
    clef_answers,
    clef_checkpoint,
    clef_endpoint,
    clef_question_ids,
    confidence_from_score,
    decision_questions,
    is_fallback_trigger,
    pins_clef,
    provider_mode,
    provider_order,
    resolve_provider,
    validate_fallback_order,
    validate_score_answer,
)

_runtime_spec = importlib.util.spec_from_file_location(
    "jev_sentinel_runtime_clef",
    Path(__file__).parents[1] / "custom_components/jev_sentinel/runtime.py",
)
assert _runtime_spec and _runtime_spec.loader
_runtime = importlib.util.module_from_spec(_runtime_spec)
sys.modules[_runtime_spec.name] = _runtime
_runtime_spec.loader.exec_module(_runtime)

ACCOUNT = "0123456789abcdef0123456789abcdef"
LOCAL_URL = LAYA_BASE_URL + LAYA_ENDPOINT_PATH
OPENROUTER_URL = "https://openrouter.ai/api/alpha/decisions"
CLEF_URL = f"{CLEF_API_BASE}/{ACCOUNT}/ai/run/@cf/cloudflare/clef"
CLEF_FLASH_URL = f"{CLEF_API_BASE}/{ACCOUNT}/ai/run/@cf/cloudflare/clef-flash"
CONFIG = {"CLOUDFLARE_API_TOKEN": "cf-private", CLEF_ACCOUNT_ENV: ACCOUNT}
# Deliberately a synthetic secret, not a real credential.
TOKEN = "cf-private-synthetic-token"


# The verification machine may itself export CLOUDFLARE_API_TOKEN for unrelated
# reasons, so every test that reasons about "is this variable configured" is
# given an explicit environment rather than the process one. That keeps the
# assertions about what is missing honest.
@pytest.fixture(autouse=True)
def no_inherited_cloudflare_environment():
    """Take the real Cloudflare variables out of reach for these tests."""
    saved = {
        name: __import__("os").environ.pop(name, None)
        for name in (CLEF_TOKEN_ENV, CLEF_ACCOUNT_ENV)
    }
    yield
    for name, value in saved.items():
        if value is not None:
            __import__("os").environ[name] = value


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RecordingTransport:
    """One handler per attempt, recording every URL and header set."""

    def __init__(self, handler):
        self.handler = handler
        self.attempts = []
        self.headers = []
        self.payloads = []

    def __call__(self, request, timeout=None):
        self.attempts.append(request.full_url)
        self.headers.append(
            {key.title(): value for key, value in request.header_items()}
        )
        self.payloads.append(json.loads(request.data.decode()))
        return self.handler(request)


def _capture(monkeypatch, module, payload):
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["headers"] = {key.title(): value for key, value in request.header_items()}
        seen["payload"] = json.loads(request.data.decode())
        seen["timeout"] = timeout
        return _Response(payload)

    monkeypatch.setattr(module, "urlopen", fake_urlopen)
    return seen


def _legend():
    return {
        "0": "very low confidence, the case is ambiguous or mostly missing",
        "1": "low confidence",
        "2": "moderate confidence",
        "3": "high confidence",
        "4": "very high confidence, the case points one way",
    }


def _answers(
    ids=None, *, score=3.0, choice="recommend", action="climate.set_temperature"
):
    """A well formed Decisions answer set, keyed by ``ids`` or the rubric."""
    ids = ids or list(decision_questions())
    return {
        ids[0]: {"type": "choice", "choice": choice},
        ids[1]: {"type": "choice", "choice": action},
        ids[2]: {"type": "score", "score": score, "legend": _legend()},
    }


def _bare(ids=None, **kwargs):
    """The model output object, with a top level ``answers`` mapping."""
    return {"model": "@cf/cloudflare/clef", "answers": _answers(ids, **kwargs)}


def _envelope(ids=None, **kwargs):
    """Cloudflare's REST envelope around the same model output."""
    return {"success": True, "messages": [], "result": _bare(ids, **kwargs)}


def _clef(monkeypatch, module, payload, *, account=ACCOUNT, token=TOKEN, **kwargs):
    """Point a module's transport at a Clef response and return the recorder."""
    transport = _RecordingTransport(lambda request: _Response(payload))
    monkeypatch.setattr(module, "urlopen", transport)
    return transport


def _no_network(monkeypatch, module):
    """A transport that fails the test if anything reaches it."""

    def _refuse(request):
        raise AssertionError(f"unexpected request to {request.full_url}")

    transport = _RecordingTransport(_refuse)
    monkeypatch.setattr(module, "urlopen", transport)
    return transport


def _clef_env(monkeypatch):
    """The two Clef variables in the process environment, then remove them."""
    monkeypatch.setenv(CLEF_TOKEN_ENV, TOKEN)
    monkeypatch.setenv(CLEF_ACCOUNT_ENV, ACCOUNT)


# ---------------------------------------------------------------------------
# selection: a hosted provider, with two checkpoints rather than two providers
# ---------------------------------------------------------------------------


def test_clef_is_a_hosted_provider_with_two_checkpoints():
    assert "clef" in PROVIDER_ENV
    assert PROVIDER_ENV["clef"] == CLEF_TOKEN_ENV
    assert CLEF_MODELS == ("clef", "clef-flash")
    assert CLEF_DEFAULT_MODEL == "clef"
    assert clef_checkpoint() == "clef"
    assert clef_checkpoint("clef-flash") == "clef-flash"
    assert clef_checkpoint("CLEF-FLASH") == "clef-flash"
    # The checkpoint is a setting of one provider, so the provider name stays
    # singular and the mode name stays singular. v1.4.0 replaced the arrangement
    # name with the canonical mode name, so `clef` now reports `api_only`: one
    # hosted provider whose failure is reported and never rerouted, which is
    # exactly what `clef_api` selected before.
    assert PROVIDER_MODES["clef"] == "api_only"
    assert MODE_ALIASES["clef_api"] == "api_only"
    assert MODE_ALIASES["clef"] == "api_only"
    assert "clef_api" not in MODE_NAMES
    assert "clef-flash" not in MODE_NAMES
    # A checkpoint is not a provider name: it is never a route, and it never
    # reaches the network, because provider_order refuses it.
    assert "clef-flash" not in MODE_ALIASES.values()
    with pytest.raises(ValueError, match="invalid provider"):
        provider_order("clef-flash", env=dict(CONFIG))
    assert clef_checkpoint("") == CLEF_DEFAULT_MODEL
    assert clef_checkpoint(None) == CLEF_DEFAULT_MODEL
    assert clef_checkpoint("   ") == CLEF_DEFAULT_MODEL
    for unknown in ("clef-xl", "flash", "clef flash", "clef2"):
        with pytest.raises(ValueError, match="invalid Clef checkpoint"):
            clef_checkpoint(unknown)


def test_clef_is_selected_on_its_own_and_sits_in_a_fallback_order():
    assert provider_order("clef", env=CONFIG) == ["clef"]
    assert provider_order("clef_api", env=CONFIG) == ["clef"]
    provider = build_provider("clef", env=CONFIG)
    assert isinstance(provider, ClefJev)
    assert provider.model == CLEF_DEFAULT_MODEL
    assert provider.timeout == CLEF_TIMEOUT
    # The checkpoint travels as a setting of the route, not a route name.
    flash = build_provider("clef", env=CONFIG, clef_model="clef-flash")
    assert isinstance(flash, ClefJev)
    assert flash.model == "clef-flash"
    # A hosted provider is a legal member of the hosted order.
    assert validate_fallback_order(("clef",)) == ("clef",)
    assert validate_fallback_order(("openrouter", "clef")) == ("openrouter", "clef")
    assert provider_order("auto", env={**CONFIG, "OPENROUTER_API_KEY": "k"}) == [
        "openrouter"
    ]
    assert provider_order(
        "auto",
        fallback_order=("clef", "openrouter"),
        env={**CONFIG, "OPENROUTER_API_KEY": "k"},
    ) == ["clef", "openrouter"]
    # The local route is still not a hosted member.
    for order in (
        ("clef", "laya"),
        ("laya",),
        (CHAINED_PROVIDER,),
        (CLEF_CHAINED_PROVIDER,),
    ):
        with pytest.raises(ValueError, match="invalid fallback_order"):
            validate_fallback_order(order)


def test_clef_with_jev_fallback_chains_clef_first():
    # `clef_then_jev` is a hosted-only chain: Clef first, then hosted Jev. Both
    # of its hops are hosted, so it is not one of the four canonical modes, and
    # it resolves to `local_with_api_fallback` with the Clef lead preserved by
    # the stored spelling rather than by the mode name alone.
    assert MODE_ALIASES["clef_with_jev_fallback"] == "local_with_api_fallback"
    assert MODE_ALIASES[CLEF_CHAINED_PROVIDER] == "local_with_api_fallback"
    assert provider_mode("clef_then_jev") == "local_with_api_fallback"
    assert provider_mode("clef_with_jev_fallback") == "local_with_api_fallback"
    order = provider_order(
        "clef_with_jev_fallback",
        env={**CONFIG, "OPENROUTER_API_KEY": "private"},
    )
    assert order == ["clef", "openrouter"]
    chained = build_provider(
        "clef_with_jev_fallback",
        api_key="private",
        env=dict(CONFIG),
    )
    assert isinstance(chained, ChainedJev)
    assert chained.names == ("clef", "openrouter")
    assert isinstance(chained.providers[0][1], ClefJev)
    assert isinstance(chained.providers[1][1], OpenRouterJev)


def test_a_selection_that_would_route_to_clef_without_a_token_is_rejected():
    for env in (
        {},
        {"OPENROUTER_API_KEY": "private"},
        {CLEF_TOKEN_ENV: TOKEN},
        {CLEF_ACCOUNT_ENV: ACCOUNT},
        {CLEF_TOKEN_ENV: "  ", CLEF_ACCOUNT_ENV: ACCOUNT},
    ):
        with pytest.raises(ValueError) as raised:
            provider_order("clef", env=env)
        assert str(raised.value) in (
            f"missing {CLEF_TOKEN_ENV} and {CLEF_ACCOUNT_ENV}",
            f"missing {CLEF_ACCOUNT_ENV} and {CLEF_TOKEN_ENV}",
        )
    # A chain named after Clef is refused the same way, before any request.
    with pytest.raises(ValueError, match=f"missing {CLEF_TOKEN_ENV}"):
        provider_order("clef_with_jev_fallback", env={"OPENROUTER_API_KEY": "private"})
    with pytest.raises(ValueError, match=f"missing {CLEF_TOKEN_ENV}"):
        build_provider("clef_with_jev_fallback", env={})
    # Clef configured but nothing behind it is also refused, rather than
    # silently degrading into a Clef-only route.
    with pytest.raises(ValueError, match="no configured hosted provider behind Clef"):
        provider_order("clef_with_jev_fallback", env=dict(CONFIG))


# ---------------------------------------------------------------------------
# wire shape
# ---------------------------------------------------------------------------


def test_the_clef_endpoint_and_model_string_follow_the_checkpoint():
    assert clef_endpoint(ACCOUNT) == CLEF_URL
    assert clef_endpoint(ACCOUNT, "clef-flash") == CLEF_FLASH_URL
    # The account id is user supplied text placed in a URL path.
    assert clef_endpoint("a b/c", "clef-flash").endswith(
        "/a%20b%2Fc/ai/run/@cf/cloudflare/clef-flash"
    )
    with pytest.raises(ValueError, match="invalid Clef account id"):
        clef_endpoint("  ")


def test_clef_posts_the_same_rubric_to_the_account_endpoint(monkeypatch):
    _clef_env(monkeypatch)
    transport = _clef(monkeypatch, sys.modules["sentinel.jev"], _bare())
    state = {"case": {"event_type": "window_open_while_heating"}}
    decision = ClefJev().decide(state)
    assert transport.attempts == [CLEF_URL]
    assert transport.headers == [
        {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    ]
    payload = transport.payloads[0]
    assert payload["model"] == "clef"
    assert payload["state"] == state
    assert payload["questions"] == decision_questions()
    assert decision.outcome == "recommend"
    assert decision.reason == "recommend"
    assert decision.action == "climate.set_temperature"
    assert decision.confidence == pytest.approx(0.75)
    assert decision.shadow is True
    assert decision.raw == {"model": "@cf/cloudflare/clef"}


def test_clef_flash_routes_to_the_flash_endpoint_and_model_string(monkeypatch):
    _clef_env(monkeypatch)
    transport = _clef(monkeypatch, sys.modules["sentinel.jev"], _bare(), token=TOKEN)
    provider = build_provider(
        "clef", env=dict(CONFIG), clef_model="clef-flash", timeout=12.0
    )
    assert isinstance(provider, ClefJev)
    decision = provider.decide({"case": {"event_type": "manual"}})
    assert transport.attempts == [CLEF_FLASH_URL]
    assert transport.payloads[0]["model"] == "clef-flash"
    assert provider.timeout == 12.0
    assert decision.raw == {"model": "@cf/cloudflare/clef"}


def test_a_clef_only_review_reaches_neither_openrouter_nor_laya(monkeypatch):
    _clef_env(monkeypatch)
    package = sys.modules["sentinel.jev"]
    bridge = _runtime
    package_transport = _clef(monkeypatch, package, _bare())
    bridge_transport = _clef(monkeypatch, bridge, _bare())
    # Both adapters are armed, so a request that escaped the Clef route would
    # be visible in the recorded attempts.
    case = Case.create(
        "window_open_while_heating",
        area="living_room",
        entities=["climate.living_room"],
        facts={"expected_state": "off"},
    )
    package_decision = SentinelWorkflow(
        build_provider("clef_api", env=dict(CONFIG))
    ).review(case)
    bridge_decision = SentinelWorkflow(
        bridge.build_provider("clef_api", env=dict(CONFIG))
    ).review(bridge.Case.create("window_open_while_heating"))
    for transport in (package_transport, bridge_transport):
        assert transport.attempts == [CLEF_URL]
        assert not any(OPENROUTER_URL in url for url in transport.attempts)
        assert not any(LOCAL_URL in url for url in transport.attempts)
    assert package_decision.outcome == bridge_decision.outcome
    assert package_decision.confidence == pytest.approx(bridge_decision.confidence)


# ---------------------------------------------------------------------------
# both response envelopes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("builder", [_bare, _envelope])
def test_both_cloudflare_envelopes_produce_the_same_decision(monkeypatch, builder):
    _clef_env(monkeypatch)
    _clef(monkeypatch, sys.modules["sentinel.jev"], builder())
    decision = ClefJev().decide({})
    assert decision.outcome == "recommend"
    assert decision.action == "climate.set_temperature"
    assert decision.confidence == pytest.approx(0.75)


def test_a_top_level_answers_mapping_wins_over_the_result_envelope():
    body = {
        "success": True,
        "result": _bare(score=1.0),
        "answers": _answers(score=4.0),
    }
    parsed = clef_answers(body, decision_questions(), model="clef")
    assert parsed["answers"]["confidence"]["score"] == 4.0
    # With only the envelope present, the same unwrapping still works.
    assert (
        clef_answers(_envelope(score=1.0), decision_questions())["answers"][
            "confidence"
        ]["score"]
        == 1.0
    )


def test_a_failed_envelope_surfaces_the_cloudflare_code(monkeypatch):
    _clef_env(monkeypatch)
    body = {
        "success": False,
        "errors": [{"code": 7003, "message": "could not route to model"}],
        "messages": [],
    }
    _clef(monkeypatch, sys.modules["sentinel.jev"], body)
    with pytest.raises(RuntimeError) as raised:
        ClefJev().decide({})
    message = str(raised.value)
    assert "7003" in message
    assert "Cloudflare" in message
    # A failed envelope with no codes still fails loudly rather than silently.
    with pytest.raises(RuntimeError, match="unknown error"):
        clef_answers({"success": False}, decision_questions())


def test_an_unusable_response_is_rejected_before_a_decision(monkeypatch):
    _clef_env(monkeypatch)
    for body in ([], {"usage": {}}, {"result": {"usage": {}}}):
        _clef(monkeypatch, sys.modules["sentinel.jev"], body)
        with pytest.raises(RuntimeError, match="invalid response"):
            ClefJev().decide({})


def test_an_unknown_choice_is_rejected(monkeypatch):
    _clef_env(monkeypatch)
    transport = _RecordingTransport(
        lambda request: _Response(_bare(choice="open_the_garage"))
    )
    monkeypatch.setattr(sys.modules["sentinel.jev"], "urlopen", transport)
    with pytest.raises(ValueError, match="unknown choice for 'outcome'"):
        ClefJev().decide({})
    # An action this repository never offered is refused the same way.
    monkeypatch.setattr(
        sys.modules["sentinel.jev"],
        "urlopen",
        _RecordingTransport(lambda request: _Response(_bare(action="lock.unlock"))),
    )
    with pytest.raises(ValueError, match="unknown choice for 'action'"):
        ClefJev().decide({})


@pytest.mark.parametrize("score", [9, -3, 12.5])
def test_an_out_of_range_index_scale_score_is_rejected(monkeypatch, score):
    _clef_env(monkeypatch)
    monkeypatch.setattr(
        sys.modules["sentinel.jev"],
        "urlopen",
        _RecordingTransport(lambda request: _Response(_bare(score=score))),
    )
    with pytest.raises(ValueError) as raised:
        ClefJev().decide({})
    assert "outside the 0 to 4 legend scale" in str(raised.value)
    # The same number is refused by the shared validator on its own, so the two
    # cannot drift apart on the scale.
    with pytest.raises(ValueError, match="outside the 0 to 4 legend scale"):
        validate_score_answer({"score": score, "legend": _legend()})
    # A score inside the scale is still accepted by the same helper.
    validate_score_answer({"score": 2.011, "legend": _legend()})
    assert confidence_from_score(
        {"score": 2.011, "legend": _legend()}
    ) == pytest.approx(2.011 / 4)


def test_the_shared_validator_is_the_one_every_route_uses():
    questions = decision_questions()
    assert clef_answers(_bare(), questions)["answers"].keys() == questions.keys()
    for module in (sys.modules["sentinel.jev"], _runtime):
        for bad in (
            {"score": 4.5, "legend": _legend()},
            {"score": "2", "legend": _legend()},
            {"score": 0, "legend": {"0": "only"}},
            {},
        ):
            with pytest.raises(ValueError):
                module.validate_score_answer(bad)


# ---------------------------------------------------------------------------
# question id sanitisation
# ---------------------------------------------------------------------------


def test_a_candidate_style_id_is_mapped_and_restored():
    questions = decision_questions()
    safe, restore = clef_question_ids(questions)
    assert list(safe) == list(questions)
    assert restore == {name: name for name in questions}
    # A repository id carrying a colon is renamed, because Clef's alphabet has
    # no colon.
    repo_ids = {
        "candidate:aaa": {"type": "choice", "criteria": {"recommend": "recommend"}},
        "hook:lamp_on": {"type": "choice", "criteria": {"recommend": "recommend"}},
    }
    safe, restore = clef_question_ids(repo_ids)
    assert all(":" not in name for name in safe)
    assert set(restore) == set(safe)
    # The answer is mapped back, so the caller sees its own question names.
    body = {
        "model": "clef",
        "answers": {name: {"type": "choice", "choice": "recommend"} for name in safe},
    }
    parsed = clef_answers(body, repo_ids, restore=restore)
    assert set(parsed["answers"]) == set(repo_ids)
    assert parsed["answers"]["candidate:aaa"]["choice"] == "recommend"
    assert parsed["answers"]["hook:lamp_on"]["choice"] == "recommend"


def test_the_shipped_rubric_survives_a_clef_round_trip(monkeypatch):
    _clef_env(monkeypatch)
    # Clef is answered with the ids it was sent, and the decision is built from
    # the rubric names, so a real request never fails on its own id alphabet.
    transport = _RecordingTransport(lambda request: _Response(_bare()))
    monkeypatch.setattr(sys.modules["sentinel.jev"], "urlopen", transport)
    assert ClefJev().decide({}).outcome == "recommend"
    sent = transport.payloads[0]["questions"]
    assert list(sent) == list(decision_questions())


@pytest.mark.parametrize(
    "name",
    [
        "candidate:aaa",
        "hook:lamp on",
        "light.turn_off:*",
        "a" * 250,
        "",
        "   ",
        "???",
    ],
)
def test_every_disallowed_id_maps_to_a_permitted_one(name):
    safe, restore = clef_question_ids({name: {"type": "choice"}})
    for mapped in safe:
        assert len(mapped) <= CLEF_ID_MAX_LENGTH
        assert all(
            char.isalnum() or char in "_.-" for char in mapped
        ), f"{mapped!r} is outside Clef's id alphabet"
    assert set(restore) == set(safe)
    assert list(restore.values()) == [name]


def test_the_id_mapping_is_injective_when_names_collide():
    questions = {
        "candidate:a": {"type": "choice"},
        "candidate_a": {"type": "choice"},
        "candidate a": {"type": "choice"},
    }
    safe, restore = clef_question_ids(questions)
    assert len(set(safe)) == len(questions)
    assert set(restore.values()) == set(questions)
    assert all(":" not in name and " " not in name for name in safe)


def test_a_request_larger_than_clef_allows_is_refused():
    too_many = {
        f"q{index}": {"type": "choice"} for index in range(CLEF_MAX_QUESTIONS + 1)
    }
    with pytest.raises(ValueError, match="exceeds the limit of 64"):
        clef_question_ids(too_many)
    # The limit itself is accepted.
    at_limit = {f"q{index}": {"type": "choice"} for index in range(CLEF_MAX_QUESTIONS)}
    assert len(clef_question_ids(at_limit)[0]) == CLEF_MAX_QUESTIONS


# ---------------------------------------------------------------------------
# credentials and failure behaviour
# ---------------------------------------------------------------------------


def test_a_missing_token_or_account_fails_before_any_request(monkeypatch):
    package = sys.modules["sentinel.jev"]
    for env, missing in (
        ({CLEF_ACCOUNT_ENV: ACCOUNT}, CLEF_TOKEN_ENV),
        ({CLEF_TOKEN_ENV: TOKEN}, CLEF_ACCOUNT_ENV),
        ({}, f"{CLEF_TOKEN_ENV} and {CLEF_ACCOUNT_ENV}"),
        ({CLEF_TOKEN_ENV: "  "}, f"{CLEF_TOKEN_ENV} and {CLEF_ACCOUNT_ENV}"),
    ):
        transport = _no_network(monkeypatch, package)
        provider = ClefJev(env=env)
        with pytest.raises(RuntimeError) as raised:
            provider.decide({"case": {"event_type": "manual"}})
        assert missing in str(raised.value)
        assert transport.attempts == []
    # The same failure happens before the request on the runtime bridge.
    transport = _no_network(monkeypatch, _runtime)
    with pytest.raises(RuntimeError, match=CLEF_TOKEN_ENV):
        _runtime.ClefJev(env={CLEF_ACCOUNT_ENV: ACCOUNT}).decide({})
    assert transport.attempts == []


def test_credentials_are_never_written_to_a_log_record(monkeypatch, caplog):
    marker = "MARKER-bedroom-window-9911"
    _clef_env(monkeypatch)
    package = sys.modules["sentinel.jev"]
    state = {"case": {"event_type": "manual", "facts": {"note": marker}}}
    caplog.clear()
    monkeypatch.setattr(
        package,
        "urlopen",
        _RecordingTransport(lambda request: _Response(_bare(choice="nope"))),
    )
    with caplog.at_level(logging.WARNING, logger="sentinel.jev"):
        with pytest.raises(ValueError):
            ClefJev().decide(state)
    rejected = caplog.text
    # A transport failure logs the category and nothing else.
    caplog.clear()
    monkeypatch.setattr(
        package,
        "urlopen",
        _RecordingTransport(_Raising(URLError(TOKEN))),
    )
    with caplog.at_level(logging.WARNING, logger="sentinel.jev"):
        with pytest.raises(URLError):
            ClefJev().decide(state)
    unreachable = caplog.text
    assert "ValueError" in rejected
    assert "URLError" in unreachable
    for secret in (TOKEN, marker, ACCOUNT, "9911"):
        assert secret not in rejected
        assert secret not in unreachable


class _Raising:
    """A transport handler that always raises the error it was given."""

    def __init__(self, error):
        self.error = error

    def __call__(self, request):
        raise self.error


def _http_error(url, code, reason="synthetic", body=b""):
    return HTTPError(url, code, reason, Message(), None)


def test_a_refused_request_keeps_its_status_so_a_chain_still_falls_through(monkeypatch):
    _clef_env(monkeypatch)
    monkeypatch.setattr(
        sys.modules["sentinel.jev"],
        "urlopen",
        _RecordingTransport(_Raising(_http_error(CLEF_URL, 403))),
    )
    with pytest.raises(HTTPError) as raised:
        ClefJev().decide({})
    # The status is preserved so a chain still recognises the refusal and its
    # Cloudflare code, and the token never reaches the reason string.
    assert raised.value.code == 403
    assert is_fallback_trigger(raised.value) is True
    assert TOKEN not in str(raised.value)


def test_the_chained_route_falls_through_to_hosted_jev(monkeypatch):
    _clef_env(monkeypatch)
    package = sys.modules["sentinel.jev"]

    def _fallthrough(request):
        if request.full_url == OPENROUTER_URL:
            return _Response(
                {
                    "model": "typesafe/jev-1.13",
                    "answers": _answers(score=1.0, action="light.turn_off"),
                }
            )
        raise _http_error(CLEF_URL, 401)

    transport = _RecordingTransport(_fallthrough)
    monkeypatch.setattr(package, "urlopen", transport)
    provider = build_provider(
        "clef_with_jev_fallback",
        api_key="private",
        env=dict(CONFIG),
    )
    assert isinstance(provider, ChainedJev)
    assert provider.names == ("clef", "openrouter")
    # A hosted hop that refuses falls through to the next hop, so the case is
    # answered rather than lost when Clef is unavailable.
    decision = provider.decide({})
    assert transport.attempts == [CLEF_URL, OPENROUTER_URL]
    assert transport.headers[1]["Authorization"] == "Bearer private"
    assert decision.raw["provider"] == "openrouter"
    assert decision.raw["attempted"] == ["clef", "openrouter"]
    assert decision.action == "light.turn_off"
    assert FALLBACK_STATUS_CODES == frozenset({401, 403, 429})


# ---------------------------------------------------------------------------
# the runtime bridge carries the same rules
# ---------------------------------------------------------------------------


def test_the_runtime_bridge_carries_the_same_clef_rules():
    package = sys.modules["sentinel.jev"]
    assert _runtime.CLEF_MODELS == CLEF_MODELS
    assert _runtime.CLEF_DEFAULT_MODEL == CLEF_DEFAULT_MODEL
    assert _runtime.CLEF_TOKEN_ENV == CLEF_TOKEN_ENV
    assert _runtime.CLEF_ACCOUNT_ENV == CLEF_ACCOUNT_ENV
    assert _runtime.CLEF_TIMEOUT == CLEF_TIMEOUT
    assert _runtime.CLEF_ID_MAX_LENGTH == CLEF_ID_MAX_LENGTH
    assert _runtime.CLEF_MAX_QUESTIONS == CLEF_MAX_QUESTIONS
    assert _runtime.PROVIDER_ENV == PROVIDER_ENV
    assert _runtime.HOSTED_PROVIDERS == ("openrouter", "clef")
    assert set(_runtime.HOSTED_PROVIDERS) <= set(PROVIDER_ENV)
    assert _runtime.MODE_ALIASES == MODE_ALIASES
    assert _runtime.MODE_NAMES == MODE_NAMES
    assert _runtime.PROVIDER_MODES == PROVIDER_MODES
    assert _runtime.CLEF_CHECKPOINT_FIELD == "clef_model"
    assert _runtime.CLEF_CHAINED_PROVIDER == CLEF_CHAINED_PROVIDER
    for module in (package, _runtime):
        assert module.clef_endpoint(ACCOUNT) == CLEF_URL
        assert module.clef_endpoint(ACCOUNT, "clef-flash") == CLEF_FLASH_URL
        # The map runs safe id to original id, so its values are the
        # caller's own names, and the safe id carries no colon.
        safe, restore = module.clef_question_ids({"candidate:aaa": {"type": "choice"}})
        assert list(restore) == ["candidate_aaa"]
        assert list(restore.values()) == ["candidate:aaa"]
    # The selection rules live in the providers module of the package and
    # in the single bridge file, so each copy is compared against the same
    # inputs and must produce the same answer, error text included.
    package_selection = sys.modules["sentinel.providers"]
    for env in ({}, {CLEF_TOKEN_ENV: TOKEN}, {CLEF_ACCOUNT_ENV: ACCOUNT}, dict(CONFIG)):
        assert _order_or_error(package_selection, "clef", env) == _order_or_error(
            _runtime, "clef", env
        )
        assert _order_or_error(package_selection, "clef", env) == _order_or_error(
            provider_order, "clef", env
        )
    # A malformed answer is refused identically in both copies.
    for module in (package, _runtime):
        with pytest.raises(ValueError, match="unknown choice"):
            module.clef_answers(_bare(choice="nope"), decision_questions())


def _order_or_error(module_or_function, name, env):
    """The resolved order, or the text of the error it raised.

    The selection rules exist twice: as a function in the package and as one in
    the single Home Assistant bridge file. The helper takes either form so the
    two are compared on the same inputs, errors included.
    """
    function = getattr(module_or_function, "provider_order", module_or_function)
    try:
        return function(name, env=env)
    except ValueError as exc:
        return f"ValueError: {exc}"


def _schema_calls(path):
    """The first argument of every vol.Optional()/vol.Required() in the file.

    The flow cannot be imported here because it imports Home Assistant, so the
    form's field names are read from the parsed source rather than by matching
    wrapped lines of text.
    """
    keys = set()
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"Optional", "Required"}
            and node.args
        ):
            argument = node.args[0]
            if isinstance(argument, ast.Constant):
                keys.add(argument.value)
            elif isinstance(argument, ast.Name):
                names.add(argument.id)
    return keys, names


def _schema_keys(path):
    """The literal string field names the form declares."""
    return _schema_calls(path)[0]


def _schema_names(path):
    """The constant names the form uses as field names."""
    return _schema_calls(path)[1]


def test_the_config_flow_offers_clef_and_its_checkpoint():
    root = Path(__file__).parents[1] / "custom_components/jev_sentinel"
    integration = (root / "__init__.py").read_text()
    flow = (root / "config_flow.py").read_text()
    # An existing entry without a provider field keeps the hosted route.
    assert 'DEFAULT_PROVIDER = "openrouter"' in integration
    assert "clef_api" in flow and "clef_with_jev_fallback" in flow
    for name in ("clef", "CLEF_CHAINED_PROVIDER", "CHECKPOINT_LABELS"):
        assert name in flow
    # The checkpoint is a field of the route, with both documented values.
    assert "clef-flash" in flow
    assert "CLEF_CHECKPOINT_FIELD" in flow
    # Clef's credential is never asked for in the form, because a config entry
    # is stored on disk in plain text: the two variable names appear only in the
    # prose that says where they come from, never as a field.
    for field in ("api_key", "laya_base_url", "laya_model"):
        assert field in _schema_keys(root / "config_flow.py")
    # The checkpoint is a field of the form, declared with the shared constant
    # rather than a repeated literal, so the form and the entry builder cannot
    # drift apart on the spelling.
    assert _runtime.CLEF_CHECKPOINT_FIELD == "clef_model"
    assert "CLEF_CHECKPOINT_FIELD" in _schema_names(root / "config_flow.py")
    # Clef's two variables appear only in the prose that says where they come
    # from, never as a field of the form.
    assert f'"{CLEF_TOKEN_ENV}"' not in flow
    assert f'"{CLEF_ACCOUNT_ENV}"' not in flow
    # Every pre-existing option is still there and still required for the routes
    # that reach hosted Jev.
    for field in ("api_key", "laya_base_url", "laya_model", "shadow"):
        assert field in flow or field in integration
    assert "api_key_required" in flow
    assert "resolve_provider" in flow


def test_an_existing_entry_keeps_working_unchanged():
    """Every stored shape before Clef still builds the same route it did.

    The entry module is not imported here because it imports Home Assistant, so
    the stored entry data is pushed through the runtime the module delegates to.
    The stored key is only forwarded where it belongs, which is the part a
    Clef entry must not change.
    """
    _clef_env_for_entry = {
        CLEF_TOKEN_ENV: TOKEN,
        CLEF_ACCOUNT_ENV: ACCOUNT,
    }

    def _build(data):
        # The same rules _provider_for applies, read from the entry data. The
        # stored name is inspected before it is resolved, because resolution
        # collapses a Clef-pinned name and a Jev-pinned name onto one canonical
        # mode and the difference decides which credential the entry may
        # contribute. That is the exact shape of the entry module's own rule.
        stored = str(data.get("provider", "openrouter"))
        provider = resolve_provider(stored)
        stored_key = None if pins_clef(stored) else data.get("api_key")
        return build_provider(
            stored,
            api_key=stored_key,
            clef_model=data.get("clef_model") or CLEF_DEFAULT_MODEL,
            local_model=data.get("local_model"),
            env={**_clef_env_for_entry, "OPENROUTER_API_KEY": stored_key or "stored"},
        )

    # An entry with no provider field keeps the hosted route.
    assert isinstance(_build({"api_key": "stored-private"}), OpenRouterJev)
    for provider in ("openrouter", "jev_api"):
        assert isinstance(_build({"provider": provider, "api_key": "k"}), OpenRouterJev)
    for provider in ("laya", "laya_local"):
        assert isinstance(_build({"provider": provider, "api_key": "k"}), LayaJev)
    assert isinstance(
        _build({"provider": "laya_with_jev_fallback", "api_key": "k"}), ChainedJev
    )
    assert isinstance(
        _build({"provider": "clef_with_jev_fallback", "api_key": "stored-private"}),
        ChainedJev,
    )
    # A stored OpenRouter key is never forwarded to Clef: the Clef adapter reads
    # its own credential, and the entry's key stays on the hosted Jev hop.
    clef = _build({"provider": "clef", "api_key": "stored-private"})
    assert isinstance(clef, ClefJev)
    assert clef.api_token == TOKEN
    # The checkpoint travels as a setting, and an entry without one keeps the
    # default rather than calling an unknown checkpoint.
    assert _build({"provider": "clef"}).model == CLEF_DEFAULT_MODEL
    assert (
        _build({"provider": "clef", "clef_model": "clef-flash"}).model == "clef-flash"
    )
    # A stored but unknown checkpoint is refused rather than silently sent.
    with pytest.raises(ValueError, match="invalid Clef checkpoint"):
        _build({"provider": "clef", "clef_model": "clef-xl"})
    # An entry naming Clef without a token fails fast, naming the variables.
    with pytest.raises(ValueError, match=CLEF_TOKEN_ENV):
        build_provider("clef", env={"OPENROUTER_API_KEY": "stored"})


def test_clef_and_the_local_route_stay_mutually_exclusive():
    # Clef is hosted, so the local route still returns one provider and auto
    # still never selects the local server.
    assert provider_order("laya", env={**CONFIG, "OPENROUTER_API_KEY": "k"}) == [
        LOCAL_PROVIDER
    ]
    assert provider_order(
        "auto",
        fallback_order=("clef",),
        env={**CONFIG, "LAYA_API_KEY": "k"},
    ) == ["clef"]
    for provider in ("clef", "clef_api", "openrouter", "auto"):
        order = provider_order(provider, env={**CONFIG, "OPENROUTER_API_KEY": "k"})
        assert LOCAL_PROVIDER not in order
    for unknown in ("Laya", "typesafe", "clef_then_jev_now"):
        with pytest.raises(ValueError, match="invalid provider"):
            provider_order(unknown)
