"""The local Laya route: selection, wire shape, rubric, and score mapping.

Laya is a local model, not a hosted Jev endpoint. It replaces the hosted route
for a profile instead of joining it, so these tests cover four things: the local
route is a single provider, ``auto`` never selects it, the hosted fallback order
rejects it, and the score rubric is the ordered list a local server accepts.
"""

import importlib.util
import json
import logging
import os
import sys
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from sentinel import (
    API_ONLY,
    API_WITH_LOCAL_FALLBACK,
    CANONICAL_MODES,
    CASE_INSENSITIVE_ALIASES,
    CHAINED_PROVIDER,
    CLEF_PINNED_NAMES,
    CONFIDENCE_LEVELS,
    FALLBACK_STATUS_CODES,
    LAYA_BASE_URL,
    LAYA_ENDPOINT_PATH,
    LAYA_MODEL,
    LAYA_TIMEOUT,
    LOCAL_FALLBACK_FAILURE_LIMIT,
    LOCAL_MODEL,
    LOCAL_MODEL_FIELD,
    LOCAL_ONLY,
    LOCAL_PROVIDER,
    LOCAL_WITH_API_FALLBACK,
    MODE_ALIASES,
    MODE_NAMES,
    PROVIDER_MODES,
    Case,
    ChainedJev,
    LayaJev,
    OpenRouterJev,
    SentinelWorkflow,
    build_provider,
    confidence_from_score,
    decision_questions,
    is_fallback_trigger,
    laya_endpoint,
    local_checkpoint,
    local_failure_count,
    local_model_configured,
    note_local_failure,
    provider_mode,
    provider_order,
    reset_local_failures,
    resolve_auto_mode,
    resolve_provider,
    validate_fallback_order,
)
from sentinel.jev import decision_from_body

_runtime_spec = importlib.util.spec_from_file_location(
    "jev_sentinel_runtime_laya",
    Path(__file__).parents[1] / "custom_components/jev_sentinel/runtime.py",
)
assert _runtime_spec and _runtime_spec.loader
_runtime = importlib.util.module_from_spec(_runtime_spec)
sys.modules[_runtime_spec.name] = _runtime
_runtime_spec.loader.exec_module(_runtime)

LOCAL_URL = LAYA_BASE_URL + LAYA_ENDPOINT_PATH
LIVE_URL = os.environ.get("JEV_SENTINEL_LIVE_LAYA", "")
LIVE_MODEL = os.environ.get("JEV_SENTINEL_LIVE_LAYA_MODEL", "english")
live = pytest.mark.skipif(
    not LIVE_URL, reason="set JEV_SENTINEL_LIVE_LAYA to a running laya-serve URL"
)


@pytest.fixture(autouse=True)
def isolated_local_failure_breaker():
    """The consecutive-local-failure counter is process state, not case state.

    Clear it before and after every test so one test's local failures cannot
    trip the breaker for another. This is the same counter the runtime clears
    after a successful local call, and the same one it clears on restart.
    """
    sys.modules["sentinel.jev"].reset_local_failures()
    _runtime.reset_local_failures()
    yield
    sys.modules["sentinel.jev"].reset_local_failures()
    _runtime.reset_local_failures()


# One verbatim answer set from a local laya-serve (english checkpoint, CPU),
# captured on 2026-09-26 for this repository's own rubric.
LIVE_SCORE_ANSWER = {
    "type": "score",
    "score": 2.011,
    "legend": {
        "0": "very low confidence, the case is ambiguous or mostly missing",
        "1": "low confidence",
        "2": "moderate confidence",
        "3": "high confidence",
        "4": "very high confidence, the case points one way",
    },
    "probabilities": {"0": 0.0566, "1": 0.2408, "2": 0.4043, "3": 0.2317, "4": 0.0666},
    "confidence": 0.1359,
    "answer_confidence": 0.4043,
    "action": {"act_probability": 1.0},
}


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


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


def _answered(model="laya-rl-agent"):
    return {
        "model": model,
        "answers": {
            "outcome": {"type": "choice", "choice": "recommend"},
            "action": {"type": "choice", "choice": "climate.set_temperature"},
            "confidence": LIVE_SCORE_ANSWER,
        },
    }


def test_the_local_route_is_the_only_member_of_its_order():
    assert provider_order("laya") == ["laya"]
    assert provider_order(
        "laya", env={"OPENROUTER_API_KEY": "private", "LAYA_API_KEY": "x"}
    ) == ["laya"]
    provider = build_provider(
        "laya",
        env={"OPENROUTER_API_KEY": "private"},
        laya_base_url="http://127.0.0.1:8123",
    )
    assert isinstance(provider, LayaJev)
    assert provider.endpoint == "http://127.0.0.1:8123/v1/systemone"
    assert provider.model == LAYA_MODEL
    assert provider.timeout == LAYA_TIMEOUT


def test_auto_never_selects_the_local_route_on_its_own():
    assert provider_order("auto", env={}) == []
    assert provider_order("auto", env={"OPENROUTER_API_KEY": "private"}) == [
        "openrouter"
    ]
    # A token for the local server does not make the local route selectable.
    assert provider_order("auto", env={"LAYA_API_KEY": "private"}) == []
    with pytest.raises(RuntimeError, match="no Jev provider is configured"):
        build_provider("auto", env={})


def test_the_hosted_route_requires_its_key():
    with pytest.raises(ValueError, match="missing OPENROUTER_API_KEY"):
        provider_order("openrouter", env={})
    assert isinstance(
        build_provider("openrouter", api_key="private", env={}), OpenRouterJev
    )
    assert provider_order("openrouter", env={"OPENROUTER_API_KEY": "private"}) == [
        "openrouter"
    ]


def test_a_local_member_is_rejected_in_the_hosted_fallback_order():
    assert validate_fallback_order(("openrouter",)) == ("openrouter",)
    for order in (("openrouter", "laya"), ("laya",), (), ("typesafe",)):
        with pytest.raises(ValueError, match="invalid fallback_order"):
            validate_fallback_order(order)
    with pytest.raises(ValueError, match="invalid fallback_order"):
        provider_order("laya", fallback_order=("openrouter", "laya"))
    # ``laya`` is a legacy alias of ``local_only``, not an unknown name. A value
    # that is neither a mode, an alias, nor a hosted provider is still refused.
    for name in ("local", "Laya", "typesafe", "your-local-engine"):
        with pytest.raises(ValueError, match="invalid provider"):
            provider_order(name)


def test_the_local_adapter_omits_the_authorization_header_without_a_key(
    monkeypatch,
):
    monkeypatch.delenv("LAYA_API_KEY", raising=False)
    state = {"case": {"event_type": "window_open_while_heating"}}
    seen = _capture(monkeypatch, sys.modules["sentinel.jev"], _answered())
    decision = LayaJev().decide(state)
    assert seen["url"] == LOCAL_URL
    assert seen["headers"] == {"Content-Type": "application/json"}
    assert seen["payload"] == {
        "model": LAYA_MODEL,
        "state": state,
        "questions": decision_questions(),
    }
    assert seen["timeout"] == LAYA_TIMEOUT
    assert decision.outcome == "recommend"
    assert decision.action == "climate.set_temperature"
    assert decision.shadow is True
    assert decision.raw == {"model": "laya-rl-agent"}


def test_a_configured_local_server_token_is_forwarded(monkeypatch):
    monkeypatch.setenv("LAYA_API_KEY", "private-local")
    seen = _capture(monkeypatch, sys.modules["sentinel.jev"], _answered())
    LayaJev().decide({})
    assert seen["headers"]["Authorization"] == "Bearer private-local"


def test_the_hosted_adapter_keeps_its_own_headers_and_model(monkeypatch):
    seen = _capture(
        monkeypatch, sys.modules["sentinel.jev"], _answered("laya-rl-agent")
    )
    OpenRouterJev("private").decide({})
    assert seen["url"] == "https://openrouter.ai/api/alpha/decisions"
    assert seen["headers"]["Authorization"] == "Bearer private"
    assert seen["headers"]["X-Title"] == "Jev Home Sentinel"
    assert seen["payload"]["model"] == "typesafe/jev-1.13"


def test_the_local_route_refuses_cleartext_to_a_remote_host():
    assert laya_endpoint() == LOCAL_URL
    assert (
        laya_endpoint("http://localhost:8123") == "http://localhost:8123/v1/systemone"
    )
    assert laya_endpoint("https://laya.example") == "https://laya.example/v1/systemone"
    with pytest.raises(ValueError, match="nonlocal Laya server requires HTTPS"):
        LayaJev("http://192.168.1.10:8000")
    with pytest.raises(ValueError, match="invalid Laya base URL"):
        laya_endpoint("127.0.0.1:8000")
    with pytest.raises(ValueError, match="invalid Laya endpoint path"):
        laya_endpoint(LAYA_BASE_URL, "v1/systemone")


def test_the_confidence_rubric_is_an_ordered_list_of_levels():
    confidence = decision_questions()["confidence"]
    assert confidence["type"] == "score"
    assert isinstance(confidence["criteria"], list)
    assert confidence["criteria"] == list(CONFIDENCE_LEVELS)
    assert len(confidence["criteria"]) == len(CONFIDENCE_LEVELS) == 5


def test_the_confidence_mapping_is_the_legend_index_rescaled():
    legend = LIVE_SCORE_ANSWER["legend"]
    answer = {"score": 2.011, "legend": legend}
    mapped = confidence_from_score(answer)
    assert mapped is not None
    assert mapped == pytest.approx(2.011 / 4)
    # The raw legend index stays recoverable from the returned value.
    assert mapped * (len(CONFIDENCE_LEVELS) - 1) == pytest.approx(2.011)
    assert confidence_from_score({"score": 0, "legend": legend}) == 0.0
    assert confidence_from_score({"score": 4, "legend": legend}) == 1.0
    assert confidence_from_score({"score": 9, "legend": legend}) == 1.0
    assert confidence_from_score({"score": -3, "legend": legend}) == 0.0
    # Half a level apart is half of one step on the 0 to 1 scale.
    assert confidence_from_score({"score": 0.5, "legend": legend}) == pytest.approx(
        0.125
    )


def test_an_unusable_score_answer_yields_no_confidence():
    legend = LIVE_SCORE_ANSWER["legend"]
    assert confidence_from_score({}) is None
    assert confidence_from_score(None) is None
    assert confidence_from_score({"score": "2.011", "legend": legend}) is None
    assert confidence_from_score({"score": True, "legend": legend}) is None
    # A single level states no position on a 0 to 1 scale.
    assert confidence_from_score({"score": 0, "legend": {"0": "only"}}) is None
    assert confidence_from_score({"score": 0}, levels=("only",)) is None
    # Without a legend the configured rubric length is the scale.
    assert confidence_from_score({"score": 2}) == pytest.approx(0.5)


def test_the_decision_uses_the_mapped_confidence():
    decision = decision_from_body(_answered())
    assert decision.confidence == pytest.approx(2.011 / 4)
    assert decision.reason == "recommend"
    assert decision.action == "climate.set_temperature"
    assert decision.shadow is True
    assert decision.raw == {"model": "laya-rl-agent"}


def test_the_decision_converts_the_none_action():
    body = _answered()
    body["answers"]["action"]["choice"] = "none"
    assert decision_from_body(body).action is None


def test_the_runtime_bridge_carries_the_same_rules_as_the_package():
    assert _runtime.decision_questions() == decision_questions()
    assert tuple(_runtime.CONFIDENCE_LEVELS) == tuple(CONFIDENCE_LEVELS)
    assert _runtime.LAYA_TIMEOUT == LAYA_TIMEOUT
    assert _runtime.LAYA_MODEL == LAYA_MODEL
    assert _runtime.LAYA_BASE_URL == LAYA_BASE_URL
    assert _runtime.PROVIDER_ENV == {
        "openrouter": "OPENROUTER_API_KEY",
        "laya": "LAYA_API_KEY",
        "clef": "CLOUDFLARE_API_TOKEN",
    }
    assert _runtime.DEFAULT_FALLBACK_ORDER == ("openrouter",)
    assert _runtime.provider_order("laya") == ["laya"]
    assert _runtime.provider_order("auto", env={}) == []
    assert _runtime.CHAINED_PROVIDER == CHAINED_PROVIDER
    # Every accepted name, canonical mode or alias, in both copies.
    assert _runtime.PROVIDER_NAMES == tuple(MODE_ALIASES)
    assert _runtime.CANONICAL_MODES == CANONICAL_MODES
    assert _runtime.API_WITH_LOCAL_FALLBACK == API_WITH_LOCAL_FALLBACK
    assert _runtime.API_ONLY == API_ONLY
    assert _runtime.LOCAL_ONLY == LOCAL_ONLY
    assert _runtime.LOCAL_WITH_API_FALLBACK == LOCAL_WITH_API_FALLBACK
    assert _runtime.CLEF_PINNED_NAMES == (
        "clef",
        "clef_api",
        "clef_with_local_fallback",
        "clef_then_jev",
        "clef_with_jev_fallback",
    )
    assert _runtime.LOCAL_MODEL == LOCAL_MODEL == "laya"
    assert _runtime.LOCAL_MODEL_FIELD == LOCAL_MODEL_FIELD == "local_model"
    assert _runtime.FALLBACK_STATUS_CODES == FALLBACK_STATUS_CODES
    assert _runtime.is_fallback_trigger(TimeoutError()) is True
    assert _runtime.is_fallback_trigger(_http_error(422)) is False
    assert _runtime.provider_order(
        _runtime.CHAINED_PROVIDER, env={"OPENROUTER_API_KEY": "private"}
    ) == provider_order(CHAINED_PROVIDER, env={"OPENROUTER_API_KEY": "private"})
    for env in ({}, {"LAYA_API_KEY": "private"}, {"TYPESAFE_API_KEY": "private"}):
        with pytest.raises(ValueError) as package_error:
            provider_order(CHAINED_PROVIDER, env=env)
        with pytest.raises(ValueError) as bridge_error:
            _runtime.provider_order(_runtime.CHAINED_PROVIDER, env=env)
        assert str(bridge_error.value) == str(package_error.value)
    with pytest.raises(ValueError, match="invalid fallback_order"):
        _runtime.validate_fallback_order(("openrouter", "laya"))
    with pytest.raises(ValueError, match="invalid fallback_order"):
        _runtime.validate_fallback_order((CHAINED_PROVIDER,))
    for answer in (
        {"score": 2.011, "legend": LIVE_SCORE_ANSWER["legend"]},
        {"score": 0, "legend": LIVE_SCORE_ANSWER["legend"]},
        {"score": 4, "legend": LIVE_SCORE_ANSWER["legend"]},
        {"score": 9, "legend": LIVE_SCORE_ANSWER["legend"]},
        {"score": -3, "legend": LIVE_SCORE_ANSWER["legend"]},
        {"score": "two", "legend": LIVE_SCORE_ANSWER["legend"]},
        {"score": 0, "legend": {"0": "only"}},
        {},
    ):
        assert _runtime.confidence_from_score(answer) == confidence_from_score(answer)
    # The breaker and the named arrangements are carried by both copies too.
    assert _runtime.LOCAL_FALLBACK_FAILURE_LIMIT == LOCAL_FALLBACK_FAILURE_LIMIT == 3
    assert _runtime.MODE_ALIASES == MODE_ALIASES
    assert _runtime.MODE_NAMES == MODE_NAMES
    assert _runtime.PROVIDER_MODES == PROVIDER_MODES
    assert tuple(_runtime.MODE_ALIASES.items()) == tuple(MODE_ALIASES.items())
    for name in (
        "jev_api",
        "laya_local",
        "laya_with_jev_fallback",
        "clef_api",
        "clef_with_jev_fallback",
        "JEV_API",
        "CLEF_API",
        "openrouter",
        "clef",
        LOCAL_PROVIDER,
        CHAINED_PROVIDER,
        "clef_then_jev",
        "Laya",
        "typesafe",
    ):
        assert _runtime.resolve_provider(name) == resolve_provider(name)
    for provider in (
        "openrouter",
        "clef",
        LOCAL_PROVIDER,
        CHAINED_PROVIDER,
        "clef_then_jev",
    ):
        assert _runtime.provider_mode(provider) == provider_mode(provider)
    for module in (sys.modules["sentinel.jev"], _runtime):
        module.reset_local_failures()
        assert module.local_failure_count() == 0
        assert [module.note_local_failure(URLError("down")) for _ in range(3)] == [
            True,
            True,
            True,
        ]
        assert module.local_failure_count() == module.LOCAL_FALLBACK_FAILURE_LIMIT
        # The fourth consecutive failure trips the breaker.
        assert module.note_local_failure(URLError("down")) is False
        assert module.local_failure_count() == module.LOCAL_FALLBACK_FAILURE_LIMIT + 1
        module.reset_local_failures()
        assert module.local_failure_count() == 0


def test_the_runtime_bridge_answers_the_local_wire_shape(monkeypatch):
    monkeypatch.delenv("LAYA_API_KEY", raising=False)
    seen = _capture(monkeypatch, _runtime, _answered())
    decision = _runtime.LayaJev().decide({"case": {"event_type": "manual"}})
    assert seen["url"] == LOCAL_URL
    assert seen["headers"] == {"Content-Type": "application/json"}
    assert seen["payload"]["questions"] == decision_questions()
    assert decision.confidence == pytest.approx(2.011 / 4)
    assert decision.shadow is True


def test_the_runtime_bridge_chains_laya_then_hosted(monkeypatch):
    transport = _local_fails(monkeypatch, _runtime, URLError("local server is down"))
    provider = _runtime.build_provider(
        _runtime.CHAINED_PROVIDER, api_key="private", env={}
    )
    assert provider.names == ("laya", "openrouter")
    decision = provider.decide({"case": {"event_type": "manual"}})
    assert transport.attempts == [
        LOCAL_URL,
        "https://openrouter.ai/api/alpha/decisions",
    ]
    assert transport.headers[0] == {"Content-Type": "application/json"}
    assert transport.headers[1]["Authorization"] == "Bearer private"
    assert decision.raw["provider"] == "openrouter"
    assert decision.raw["attempted"] == ["laya", "openrouter"]
    assert decision.confidence == pytest.approx(2.011 / 4)


def test_the_runtime_bridge_chained_route_needs_a_hosted_key():
    # The bridge names the canonical mode in the failure message, exactly as the
    # package copy does, so the two cannot drift on the observable text.
    with pytest.raises(ValueError, match="for the local_with_api_fallback mode"):
        _runtime.build_provider(_runtime.CHAINED_PROVIDER, env={})


class _RecordingTransport:
    """A synthetic transport: one handler per attempt, recording each attempt."""

    def __init__(self, handler):
        self.handler = handler
        self.attempts = []
        self.headers = []

    def __call__(self, request, timeout=None):
        self.attempts.append(request.full_url)
        self.headers.append(
            {key.title(): value for key, value in request.header_items()}
        )
        return self.handler(request)


def _local_fails(monkeypatch, module, error):
    """Patch a module's transport so the local URL fails and the hosted URL answers."""
    answer = _Response(_answered())

    def handler(request):
        if request.full_url == LOCAL_URL:
            raise error
        return answer

    transport = _RecordingTransport(handler)
    monkeypatch.setattr(module, "urlopen", transport)
    return transport


def test_the_chained_route_puts_the_local_server_first_and_the_hosted_key_behind_it():
    assert provider_order(CHAINED_PROVIDER, env={"OPENROUTER_API_KEY": "private"}) == [
        "laya",
        "openrouter",
    ]
    # A key for a name this repository does not host adds no hop: the hosted
    # order has one member, and the fallback follows that order.
    assert provider_order(
        CHAINED_PROVIDER,
        env={"OPENROUTER_API_KEY": "private", "TYPESAFE_API_KEY": "private"},
    ) == ["laya", "openrouter"]
    chained = build_provider(CHAINED_PROVIDER, api_key="private", env={})
    assert isinstance(chained, ChainedJev)
    assert chained.names == ("laya", "openrouter")


def test_the_chained_route_fails_fast_without_a_hosted_key():
    # The four-mode contract names the canonical mode in the failure message,
    # because no alias string may leak into observable output. The message still
    # names the variable that is missing, which is the part a caller acts on.
    for env in (
        {},
        {"LAYA_API_KEY": "private"},
        {"TYPESAFE_API_KEY": "private"},
        {"OPENROUTER_API_KEY": "   "},
    ):
        with pytest.raises(
            ValueError,
            match="missing OPENROUTER_API_KEY for the local_with_api_fallback mode",
        ):
            provider_order(CHAINED_PROVIDER, env=env)
        # The canonical name and the two legacy spellings are the same failure.
        for name in (LOCAL_WITH_API_FALLBACK, "laya_with_jev_fallback"):
            with pytest.raises(
                ValueError,
                match="missing OPENROUTER_API_KEY for the local_with_api_fallback mode",
            ):
                provider_order(name, env=env)
    with pytest.raises(
        ValueError,
        match="missing OPENROUTER_API_KEY for the local_with_api_fallback mode",
    ):
        build_provider(CHAINED_PROVIDER, env={"LAYA_API_KEY": "private"})
    with pytest.raises(ValueError, match="invalid provider"):
        provider_order("laya_then_hosted_now")


def test_build_provider_chains_the_local_hop_then_the_hosted_hop():
    provider = build_provider(
        CHAINED_PROVIDER,
        api_key="private",
        env={},
        laya_base_url="http://127.0.0.1:8123",
    )
    assert isinstance(provider, ChainedJev)
    assert provider.names == ("laya", "openrouter")
    local = provider.providers[0][1]
    hosted = provider.providers[1][1]
    assert isinstance(local, LayaJev)
    assert isinstance(hosted, OpenRouterJev)
    assert local.endpoint == "http://127.0.0.1:8123/v1/systemone"
    assert local.timeout == LAYA_TIMEOUT
    assert local.api_key is None
    assert hosted.api_key == "private"
    assert hosted.timeout == 30.0


def test_the_hosted_and_local_routes_are_unchanged_by_the_chained_route():
    assert provider_order("laya") == ["laya"]
    assert isinstance(build_provider("laya", env={}), LayaJev)
    assert provider_order("openrouter", env={"OPENROUTER_API_KEY": "private"}) == [
        "openrouter"
    ]
    assert isinstance(
        build_provider("openrouter", api_key="private", env={}), OpenRouterJev
    )
    assert provider_order("auto", env={"OPENROUTER_API_KEY": "private"}) == [
        "openrouter"
    ]
    # No route but the chained one reaches the local server, and `auto` still
    # never selects it on its own initiative.
    for provider in ("openrouter", "auto"):
        assert "laya" not in provider_order(provider, env={"OPENROUTER_API_KEY": "x"})
    assert provider_order("auto", env={"LAYA_API_KEY": "private"}) == []
    assert provider_order("auto", env={}) == []
    # The chained route name is a route, not a fallback member.
    for order in (("laya_then_hosted",), ("openrouter", "laya_then_hosted")):
        with pytest.raises(ValueError, match="invalid fallback_order"):
            validate_fallback_order(order)
    with pytest.raises(ValueError, match="invalid fallback_order"):
        provider_order(CHAINED_PROVIDER, env={}, fallback_order=("laya",))


def test_a_local_answer_never_reaches_the_hosted_hop(monkeypatch):
    seen = _capture(monkeypatch, sys.modules["sentinel.jev"], _answered())
    provider = build_provider(CHAINED_PROVIDER, api_key="private", env={})
    decision = provider.decide({"case": {"event_type": "manual"}})
    assert seen["url"] == LOCAL_URL
    assert seen["headers"] == {"Content-Type": "application/json"}
    assert decision.raw == {
        "model": "laya-rl-agent",
        "provider": "laya",
        "attempted": ["laya"],
    }


def _http_error(code):
    return HTTPError(LOCAL_URL, code, "synthetic", Message(), None)


@pytest.mark.parametrize(
    "error",
    [
        URLError(ConnectionRefusedError(111, "Connection refused")),
        URLError("temporary failure in name resolution"),
        TimeoutError("timed out"),
        _http_error(401),
        _http_error(403),
        _http_error(429),
        _http_error(500),
        _http_error(503),
    ],
)
def test_a_local_failure_falls_through_and_the_hosted_hop_answers(monkeypatch, error):
    transport = _local_fails(monkeypatch, sys.modules["sentinel.jev"], error)
    provider = build_provider(CHAINED_PROVIDER, api_key="private", env={})
    decision = provider.decide({"case": {"event_type": "manual"}})
    assert transport.attempts == [
        LOCAL_URL,
        "https://openrouter.ai/api/alpha/decisions",
    ]
    # The local hop carries no Authorization header; the hosted hop carries the
    # configured key over the same request body.
    assert transport.headers[0] == {"Content-Type": "application/json"}
    assert transport.headers[1]["Authorization"] == "Bearer private"
    assert decision.outcome == "recommend"
    assert decision.action == "climate.set_temperature"
    assert decision.raw == {
        "model": "laya-rl-agent",
        "provider": "openrouter",
        "attempted": ["laya", "openrouter"],
    }


def test_only_the_configured_fallback_triggers_fall_through(monkeypatch):
    assert is_fallback_trigger(URLError("down")) is True
    assert is_fallback_trigger(TimeoutError()) is True
    assert is_fallback_trigger(ConnectionRefusedError(111, "refused")) is True
    for code in (400, 404, 422):
        assert is_fallback_trigger(_http_error(code)) is False
    assert is_fallback_trigger(KeyError("answers")) is False
    assert FALLBACK_STATUS_CODES == frozenset({401, 403, 429})

    transport = _local_fails(monkeypatch, sys.modules["sentinel.jev"], _http_error(422))
    provider = build_provider(CHAINED_PROVIDER, api_key="private", env={})
    with pytest.raises(HTTPError) as raised:
        provider.decide({})
    assert raised.value.code == 422
    # The request was rejected as invalid, so it is not sent anywhere else.
    assert transport.attempts == [LOCAL_URL]


def test_the_last_hop_error_propagates(monkeypatch):
    def fails(request, timeout=None):
        raise URLError("hosted Jev is unreachable")

    monkeypatch.setattr(sys.modules["sentinel.jev"], "urlopen", fails)
    provider = build_provider(CHAINED_PROVIDER, api_key="private", env={})
    with pytest.raises(URLError):
        provider.decide({})


def test_a_chain_needs_more_than_one_provider():
    with pytest.raises(ValueError, match="a chain needs at least two providers"):
        ChainedJev([("laya", LayaJev())])


def test_the_config_flow_offers_the_four_modes_and_keeps_the_hosted_default():
    root = Path(__file__).parents[1] / "custom_components/jev_sentinel"
    integration = (root / "__init__.py").read_text()
    flow = (root / "config_flow.py").read_text()
    # An existing config entry without a provider field keeps the hosted route.
    assert 'DEFAULT_PROVIDER = "openrouter"' in integration
    for name in ('"openrouter"', "LOCAL_ONLY", "LOCAL_WITH_API_FALLBACK"):
        assert name in flow
    assert "api_key_required" in flow
    # The form offers every canonical mode and every legacy alias, so an entry
    # created by any earlier release is still selectable, and it resolves the
    # stored name before it validates the key.
    for mode in MODE_NAMES:
        assert mode in flow
    for name in MODE_ALIASES:
        assert name in flow
    # The local model is a free-text field on the form, not a list to choose
    # from, because the whole point is that a new local model needs no code
    # change.
    assert "LOCAL_MODEL_FIELD" in flow
    assert "local_checkpoint" in flow
    assert "local_model_invalid" in flow
    # The form still does not ask for a Cloudflare credential.
    assert "CLOUDFLARE_API_TOKEN" not in flow.replace(
        "``CLOUDFLARE_API_TOKEN``", ""
    ).replace("``CLOUDFLARE_ACCOUNT_ID``", "")
    assert "resolve_provider" in flow


@live
def test_a_live_local_server_reviews_a_case_through_the_package():
    provider = LayaJev(LIVE_URL, model=LIVE_MODEL)
    case = Case.create(
        "window_open_while_heating",
        area="living_room",
        entities=["climate.living_room", "binary_sensor.living_room_window"],
        facts={
            "window_open_minutes": 14,
            "heating_state": "heating",
            "expected_state": "off",
        },
    )
    decision = SentinelWorkflow(provider).review(case)
    rubric = decision_questions()
    assert decision.outcome in rubric["outcome"]["criteria"]
    assert decision.action is None or decision.action in rubric["action"]["criteria"]
    assert decision.confidence is None or 0.0 <= decision.confidence <= 1.0
    assert decision.shadow is True
    assert decision.raw["model"]
    print(
        "live package route",
        LIVE_URL,
        decision.outcome,
        decision.action,
        decision.confidence,
        decision.raw,
    )


@live
def test_a_live_local_server_reviews_a_case_through_the_runtime_bridge():
    provider = _runtime.LayaJev(LIVE_URL, model=LIVE_MODEL)
    case = _runtime.Case.create(
        "window_open_while_heating",
        area="living_room",
        entities=["climate.living_room"],
        facts={"window_open_minutes": 14, "expected_state": "off"},
    )
    decision = _runtime.SentinelWorkflow(provider).review(case)
    rubric = _runtime.decision_questions()
    assert decision.outcome in rubric["outcome"]["criteria"]
    assert decision.shadow is True
    assert decision.confidence is None or 0.0 <= decision.confidence <= 1.0
    print(
        "live runtime route",
        LIVE_URL,
        decision.outcome,
        decision.action,
        decision.confidence,
        decision.raw,
    )


@live
def test_a_live_local_server_answers_the_chained_route_from_the_local_hop():
    provider = build_provider(
        CHAINED_PROVIDER,
        # A synthetic hosted key. The local hop answers, so no hosted call is
        # made with it; if the local hop failed this test would fail loudly
        # instead of quietly reaching a hosted endpoint.
        api_key="private-synthetic-hosted-key",
        env={},
        laya_base_url=LIVE_URL,
        laya_model=LIVE_MODEL,
    )
    assert isinstance(provider, ChainedJev)
    assert provider.names == ("laya", "openrouter")
    case = Case.create(
        "window_open_while_heating",
        area="living_room",
        entities=["climate.living_room", "binary_sensor.living_room_window"],
        facts={
            "window_open_minutes": 14,
            "heating_state": "heating",
            "expected_state": "off",
        },
    )
    decision = SentinelWorkflow(provider).review(case)
    rubric = decision_questions()
    assert decision.raw["provider"] == "laya"
    assert decision.raw["attempted"] == ["laya"]
    assert decision.outcome in rubric["outcome"]["criteria"]
    assert decision.action is None or decision.action in rubric["action"]["criteria"]
    assert decision.confidence is None or 0.0 <= decision.confidence <= 1.0
    assert decision.shadow is True
    print(
        "live chained route",
        LIVE_URL,
        decision.outcome,
        decision.action,
        decision.confidence,
        decision.raw,
    )


def _outcome(function, provider, env):
    """The result of a provider rule, or the text of the error it raised."""
    try:
        return function(provider, env=env)
    except ValueError as exc:
        return f"ValueError: {exc}"


def _routing(function, provider, env):
    """The routing decision only: a provider order, or the failure category.

    Two names that resolve to the same mode must make the same routing decision.
    The wording of a failure message is allowed to name the provider a caller
    selected rather than the mode it resolved to, so this compares the decision
    and not the text.
    """
    try:
        return tuple(function(provider, env=env))
    except ValueError as exc:
        return f"ValueError: {type(exc).__name__}: {'missing' in str(exc)}"


def _weak_answered(choice="ignore", score=0.0, action="none"):
    """A valid but weak local answer: low confidence, or a confident non-answer."""
    return {
        "model": "laya-rl-agent",
        "answers": {
            "outcome": {"type": "choice", "choice": choice},
            "action": {"type": "choice", "choice": action},
            "confidence": {
                "type": "score",
                "score": score,
                "legend": LIVE_SCORE_ANSWER["legend"],
            },
        },
    }


def test_the_four_canonical_modes_are_the_only_names_a_mode_reports():
    # v1.4.0 replaces the five arrangement names with four canonical mode names.
    # Every pre-existing name survives as an alias, and every assertion the
    # earlier releases made about those names still holds, which is what the
    # rest of this test proves.
    assert MODE_NAMES == CANONICAL_MODES
    assert CANONICAL_MODES == (
        "api_with_local_fallback",
        "api_only",
        "local_only",
        "local_with_api_fallback",
    )
    # Each alias resolves onto exactly one canonical mode, and the reverse table
    # is total, so neither can name a mode the other does not.
    for alias, canonical in MODE_ALIASES.items():
        assert canonical in CANONICAL_MODES, alias
        assert PROVIDER_MODES[alias] == canonical, alias
        assert resolve_provider(alias) == canonical
        assert resolve_provider(f"  {alias}  ") == canonical
        assert provider_mode(canonical) == canonical
        if alias.lower() in CASE_INSENSITIVE_ALIASES:
            assert resolve_provider(alias.upper()) == canonical, alias
        for env in (
            {},
            {"OPENROUTER_API_KEY": "private"},
            {"LAYA_API_KEY": "private"},
            {"TYPESAFE_API_KEY": "private"},
            {
                "CLOUDFLARE_API_TOKEN": "private",
                "CLOUDFLARE_ACCOUNT_ID": "private-account",
            },
        ):
            if alias.lower() in CLEF_PINNED_NAMES:
                # A Clef-pinned name carries routing the canonical mode name
                # alone cannot express: it says which hosted provider leads.
                # Its Clef-first routing is asserted in the tests below rather
                # than here, because here it is not the same as the bare mode.
                continue
            if alias.lower() in ("openrouter", "jev_api"):
                # These pin OpenRouter, and a selection naming one keeps the
                # v1.3.0 message that names the provider rather than the mode,
                # because the provider is what the caller has to supply. The
                # routing itself is identical, which the comparison below would
                # show if it compared the decision rather than the wording.
                assert _routing(provider_order, alias, env) == _routing(
                    provider_order, canonical, env
                ), (alias, env)
                continue
            if alias == "auto":
                # ``auto`` returns an empty order rather than raising when
                # nothing is configured, and that is unchanged from v1.3.0.
                # Its own resolution is asserted below.
                continue
            assert _outcome(provider_order, alias, env) == _outcome(
                provider_order, canonical, env
            ), (alias, env)
    assert set(MODE_ALIASES) == set(PROVIDER_MODES)
    assert set(PROVIDER_MODES.values()) == set(CANONICAL_MODES)


def test_every_legacy_name_still_resolves_to_the_same_routing():
    """The backwards-compatibility table, one assertion per pre-existing name.

    Each legacy name is paired with the canonical mode it now denotes and with
    the provider order it produced before v1.4.0, so a configuration that
    worked before this change produces the same routing decision after it.
    """
    legacy = {
        # v1.1.0: the three original routes.
        "jev_api": (API_ONLY, ["openrouter"]),
        "openrouter": (API_ONLY, ["openrouter"]),
        "laya": (LOCAL_ONLY, [LOCAL_PROVIDER]),
        "laya_local": (LOCAL_ONLY, [LOCAL_PROVIDER]),
        "laya_then_hosted": (
            LOCAL_WITH_API_FALLBACK,
            [LOCAL_PROVIDER, "openrouter"],
        ),
        "laya_with_jev_fallback": (
            LOCAL_WITH_API_FALLBACK,
            [LOCAL_PROVIDER, "openrouter"],
        ),
        # v1.3.0: the Clef routes.
        "clef": (API_ONLY, ["clef"]),
        "clef_api": (API_ONLY, ["clef"]),
        "clef_then_jev": (LOCAL_WITH_API_FALLBACK, ["clef", "openrouter"]),
        "clef_with_jev_fallback": (
            LOCAL_WITH_API_FALLBACK,
            ["clef", "openrouter"],
        ),
        # The mode that did not exist before v1.4.0.
        "clef_with_local_fallback": (
            API_WITH_LOCAL_FALLBACK,
            ["clef", LOCAL_PROVIDER],
        ),
        # The default, which keeps meaning "the configured hosted providers"
        # until a local model is named.
        "auto": (API_ONLY, ["openrouter"]),
    }
    both = {
        "CLOUDFLARE_API_TOKEN": "private",
        "CLOUDFLARE_ACCOUNT_ID": "private-account",
    }
    for name, (canonical, expected) in legacy.items():
        assert resolve_provider(name) == canonical, name
        assert provider_mode(name) == canonical, name
        assert provider_order(name, env={**both, "OPENROUTER_API_KEY": "k"}) == (
            expected
        ), name
    # The legacy spellings still build the same adapter they always built.
    assert isinstance(
        build_provider("jev_api", api_key="private", env={}), OpenRouterJev
    )
    assert isinstance(build_provider("laya_local", env={}), LayaJev)
    chained = build_provider("laya_with_jev_fallback", api_key="private", env={})
    assert isinstance(chained, ChainedJev)
    assert chained.names == (LOCAL_PROVIDER, "openrouter")
    clef_chain = build_provider(
        "clef_with_jev_fallback", api_key="private", env={**both}
    )
    assert isinstance(clef_chain, ChainedJev)
    assert clef_chain.names == ("clef", "openrouter")
    # ``laya`` is a legacy alias of ``local_only`` and has been since v1.1.0, so
    # it is checked above rather than listed here. The names the earlier releases
    # rejected are still rejected, and the error now names every accepted mode
    # and alias rather than hiding them.
    for unknown in ("Laya", "typesafe", "laya_then_hosted_now", "your-local-engine"):
        assert resolve_provider(unknown) == unknown
        with pytest.raises(ValueError, match="invalid provider"):
            provider_order(unknown)
        with pytest.raises(ValueError, match="invalid provider"):
            provider_mode(unknown)
    assert resolve_provider("") == ""
    assert resolve_provider(None) == ""
    for mode in CANONICAL_MODES:
        with pytest.raises(ValueError, match="invalid provider"):
            provider_mode(mode + "_now")


def test_an_unknown_mode_is_refused_with_every_accepted_name_in_the_message():
    with pytest.raises(ValueError) as raised:
        provider_order("nonsense")
    message = str(raised.value)
    assert message.startswith("invalid provider: ")
    for mode in CANONICAL_MODES:
        assert mode in message
    for alias in MODE_ALIASES:
        assert alias in message
    # Nothing but names: no credential, no state, and no candidate text.
    assert "OPENROUTER_API_KEY" not in message
    assert "CLOUDFLARE_API_TOKEN" not in message


def test_auto_resolves_by_whether_a_local_model_is_configured():
    # What ``auto`` meant in v1.3.0: the hosted providers that are configured,
    # and never the local slot on its own initiative.
    assert resolve_auto_mode() == API_ONLY
    assert resolve_auto_mode(local_model=None) == API_ONLY
    assert provider_order("auto", env={}) == []
    assert provider_order("auto", env={"OPENROUTER_API_KEY": "private"}) == [
        "openrouter"
    ]
    assert local_model_configured(None) is False
    # Naming a local model is what puts the local slot behind the API, which is
    # what ``api_with_local_fallback`` means.
    assert resolve_auto_mode(local_model="your-local-engine") == API_WITH_LOCAL_FALLBACK
    assert local_model_configured("your-local-engine") is True
    assert provider_order(
        "auto", env={"OPENROUTER_API_KEY": "private"}, local_model="your-local-engine"
    ) == ["openrouter", LOCAL_PROVIDER]
    # A local model with no hosted provider at all is still nothing configured.
    # ``auto`` selects among the hosted providers, and naming a local model does
    # not by itself make a hosted-first mode usable, so this preserves what
    # ``auto`` meant before v1.4.0 rather than silently answering from the local
    # slot under a hosted-first mode's name.
    assert provider_order("auto", env={}, local_model="your-local-engine") == []
    with pytest.raises(RuntimeError, match="no Jev provider is configured"):
        build_provider("auto", env={}, local_model="your-local-engine")


@pytest.mark.parametrize(
    "mode,expected",
    [
        (API_WITH_LOCAL_FALLBACK, ["openrouter", LOCAL_PROVIDER]),
        (API_ONLY, ["openrouter"]),
        (LOCAL_ONLY, [LOCAL_PROVIDER]),
        (LOCAL_WITH_API_FALLBACK, [LOCAL_PROVIDER, "openrouter"]),
    ],
)
def test_each_of_the_four_modes_resolves_to_its_provider_order(mode, expected):
    assert provider_mode(mode) == mode
    assert (
        provider_order(
            mode, env={"OPENROUTER_API_KEY": "private", "LAYA_API_KEY": "private"}
        )
        == expected
    )


def test_each_of_the_four_modes_builds_the_adapter_its_order_names():
    env = {"OPENROUTER_API_KEY": "private"}
    api_first = build_provider(API_WITH_LOCAL_FALLBACK, api_key="private", env={})
    assert isinstance(api_first, ChainedJev)
    assert api_first.names == ("openrouter", LOCAL_PROVIDER)
    assert isinstance(api_first.providers[0][1], OpenRouterJev)
    assert isinstance(api_first.providers[1][1], LayaJev)
    assert isinstance(
        build_provider(API_ONLY, api_key="private", env={}), OpenRouterJev
    )
    assert isinstance(build_provider(LOCAL_ONLY, env={}), LayaJev)
    local_first = build_provider(LOCAL_WITH_API_FALLBACK, api_key="private", env={})
    assert isinstance(local_first, ChainedJev)
    assert local_first.names == (LOCAL_PROVIDER, "openrouter")
    # The two ``_only`` modes are single-provider routes: one adapter, no chain,
    # so a failure is reported and never rerouted.
    for mode in (API_ONLY, LOCAL_ONLY):
        assert len(provider_order(mode, env=env)) == 1


def test_a_fallback_mode_with_no_provider_for_the_other_side_fails_at_load():
    # Each mode promises a fallback, so each names the missing variable rather
    # than degrading into the single-provider mode under another name.
    with pytest.raises(ValueError, match="missing OPENROUTER_API_KEY"):
        provider_order(LOCAL_WITH_API_FALLBACK, env={})
    with pytest.raises(ValueError, match="missing OPENROUTER_API_KEY"):
        provider_order(API_WITH_LOCAL_FALLBACK, env={})
    with pytest.raises(ValueError, match="missing OPENROUTER_API_KEY"):
        build_provider(API_WITH_LOCAL_FALLBACK, api_key="", env={})
    # The two single-provider modes report instead of rerouting.
    # ``api_only`` names the variable that is missing, in its own mode's name,
    # because the mode's API side is the provider that is absent.
    with pytest.raises(ValueError, match="missing OPENROUTER_API_KEY"):
        provider_order(API_ONLY, env={})
    # The names that pin OpenRouter did exactly the same before this change and
    # still do.
    for name in ("openrouter", "jev_api"):
        with pytest.raises(ValueError, match="missing OPENROUTER_API_KEY"):
            provider_order(name, env={})


def test_local_model_selects_the_engine_and_the_default_is_unchanged():
    # The default is unchanged: an entry that stored nothing still calls Laya.
    assert LOCAL_MODEL == "laya"
    assert LOCAL_MODEL_FIELD == "local_model"
    assert local_checkpoint() == "laya"
    assert local_checkpoint(None) == "laya"
    assert local_checkpoint("  laya  ") == "laya"
    assert build_provider(LOCAL_ONLY, env={}).model == LAYA_MODEL
    assert build_provider(LOCAL_ONLY, env={}, laya_model=LAYA_MODEL).model == LAYA_MODEL
    # A different engine is configuration, not a new provider, and not an
    # allowlist: a model published after this release works by naming it.
    for engine in (
        "your-local-engine",
        "your-local-engine-0.8b",
        "another-local-engine",
        "Another-Engine-4B",
        "Another-Engine-0.8B",
        "pre-routed-qwen3.5-0.8b",
        "pre-routed-gemma4-e2b",
        "laya-multilingual",
        "laya-typed-decisions",
        "some-model-published-next-year",
        "org/model:v2",
    ):
        assert local_checkpoint(engine) == engine, engine
        provider = build_provider(LOCAL_ONLY, env={}, local_model=engine)
        assert provider.model == engine, engine
        # The provider name stays the local slot's single name.
        assert provider_order(LOCAL_ONLY, env={}) == [LOCAL_PROVIDER]


def test_local_model_changes_what_the_local_request_asks_for(monkeypatch):
    monkeypatch.delenv("LAYA_API_KEY", raising=False)
    for engine, expected in (
        (None, LAYA_MODEL),
        ("your-local-engine", "your-local-engine"),
        ("another-local-engine", "another-local-engine"),
    ):
        seen = _capture(monkeypatch, sys.modules["sentinel.jev"], _answered(engine))
        provider = build_provider(
            LOCAL_ONLY, env={}, local_model=engine, laya_base_url=LAYA_BASE_URL
        )
        provider.decide({"case": {"event_type": "manual"}})
        assert seen["payload"]["model"] == expected, engine
        assert seen["url"] == LOCAL_URL


def test_local_model_takes_precedence_over_the_deprecated_laya_model(monkeypatch):
    monkeypatch.delenv("LAYA_API_KEY", raising=False)
    # The deprecated field still works on its own, so an entry that stored only
    # ``laya_model`` keeps calling that checkpoint.
    assert build_provider(LOCAL_ONLY, env={}, laya_model="english").model == "english"
    # When both are set, the current field wins. That precedence is documented
    # in docs/reference.md.
    assert (
        build_provider(
            LOCAL_ONLY, env={}, local_model="your-local-engine", laya_model="english"
        ).model
        == "your-local-engine"
    )
    # It is a local setting, so it never changes which provider answers.
    assert provider_order(LOCAL_ONLY, env={}) == [LOCAL_PROVIDER]


def test_the_local_model_reaches_the_chain_and_the_entry_builder(monkeypatch):
    monkeypatch.delenv("LAYA_API_KEY", raising=False)
    chain = build_provider(
        LOCAL_WITH_API_FALLBACK,
        api_key="private",
        env={},
        local_model="another-local-engine",
    )
    assert chain.names == (LOCAL_PROVIDER, "openrouter")
    assert chain.providers[0][1].model == "another-local-engine"
    hosted_first = build_provider(
        API_WITH_LOCAL_FALLBACK,
        api_key="private",
        env={},
        local_model="another-local-engine",
    )
    assert hosted_first.names == ("openrouter", LOCAL_PROVIDER)
    assert hosted_first.providers[1][1].model == "another-local-engine"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "\t",
        "\n",
        "?",
        "#",
        "&",
        "a?b",
        "a#b",
        "a&b",
        'a"b',
        "a\\b",
        "a\x00b",
        "a b",
        "a/b/c d",
        "-laya",
        ".laya",
        "a|b",
        "a<b>",
        "a{b}",
        "a^b",
    ],
)
def test_an_unusable_local_model_is_refused(bad):
    with pytest.raises(ValueError, match="invalid local_model"):
        local_checkpoint(bad)
    # The refusal happens at build time, before any request is made.
    with pytest.raises(ValueError, match="invalid local_model"):
        build_provider(LOCAL_ONLY, env={}, local_model=bad)


@pytest.mark.parametrize(
    "bad",
    [123, 4.5, True, ["your-local-engine"], {"model": "your-local-engine"}, object()],
)
def test_a_non_text_local_model_is_refused(bad):
    with pytest.raises(ValueError, match="invalid local_model"):
        local_checkpoint(bad)
    with pytest.raises(ValueError, match="invalid local_model"):
        build_provider(LOCAL_ONLY, env={}, local_model=bad)


def test_a_value_needing_url_escaping_is_refused_deliberately():
    # A model name is sent in a JSON body, not in a URL path, so the shape does
    # not percent-encode anything. The choice is deliberate: a name carrying a
    # character that would corrupt a path segment is refused rather than
    # silently rewritten, so a local engine is never misaddressed.
    assert local_checkpoint("org/model:v2") == "org/model:v2"
    for needs_escaping in ("a?b", "a#b", "a&b", "a b", "a%b"):
        with pytest.raises(ValueError, match="invalid local_model"):
            local_checkpoint(needs_escaping)
    # The characters that engine names actually use are accepted.
    for accepted in (
        "convaiinnovations/laya",
        "laya-typed-decisions",
        "pre-routed-gemma4-e2b",
        "Another-Engine-0.8B",
        "model_1.2",
    ):
        assert local_checkpoint(accepted) == accepted


def test_local_model_never_becomes_a_mode_alias_or_a_fallback_member():
    # A model name is not a route, so it is refused by the mode resolver and it
    # cannot be appended to a hosted order as a last resort.
    for engine in ("your-local-engine", "another-local-engine", "laya-multilingual"):
        assert resolve_provider(engine) == engine
        with pytest.raises(ValueError, match="invalid provider"):
            provider_order(engine)
    for engine in ("your-local-engine", "another-local-engine"):
        with pytest.raises(ValueError, match="invalid fallback_order"):
            validate_fallback_order(("openrouter", engine))
        with pytest.raises(ValueError, match="invalid fallback_order"):
            validate_fallback_order((engine,))
    assert engine not in MODE_ALIASES
    assert engine not in PROVIDER_MODES
    assert engine not in CANONICAL_MODES


def test_a_rejected_local_model_never_reaches_a_log_record(caplog):
    # The refusal message names the setting and the shape, never the case, an
    # entity state, or an answer.
    with caplog.at_level(logging.WARNING, logger="sentinel.providers"):
        with pytest.raises(ValueError) as raised:
            build_provider(LOCAL_ONLY, env={}, local_model="a b")
    assert "local_model" in str(raised.value)
    assert "MARKER" not in caplog.text


def test_the_breaker_allows_three_fallbacks_and_suppresses_the_fourth(monkeypatch):
    package = sys.modules["sentinel.jev"]
    hosted = "https://openrouter.ai/api/alpha/decisions"
    transport = _local_fails(monkeypatch, package, URLError("local server is down"))
    provider = build_provider(CHAINED_PROVIDER, api_key="private", env={})
    state = {"case": {"event_type": "manual"}}
    assert LOCAL_FALLBACK_FAILURE_LIMIT == 3
    for _ in range(LOCAL_FALLBACK_FAILURE_LIMIT):
        decision = provider.decide(state)
        assert decision.raw["provider"] == "openrouter"
        assert decision.raw["attempted"] == [LOCAL_PROVIDER, "openrouter"]
    assert transport.attempts.count(hosted) == LOCAL_FALLBACK_FAILURE_LIMIT
    assert local_failure_count() == LOCAL_FALLBACK_FAILURE_LIMIT
    # The fourth consecutive local failure is not answered remotely: the local
    # error is raised and no hosted request is made.
    with pytest.raises(URLError, match="local server is down"):
        provider.decide(state)
    assert transport.attempts.count(hosted) == LOCAL_FALLBACK_FAILURE_LIMIT
    assert transport.attempts[-1] == LOCAL_URL
    assert local_failure_count() == LOCAL_FALLBACK_FAILURE_LIMIT + 1


def test_a_healthy_local_call_resets_a_tripped_breaker(monkeypatch):
    package = sys.modules["sentinel.jev"]
    monkeypatch.setattr(package, "_local_failure_count", LOCAL_FALLBACK_FAILURE_LIMIT)
    assert local_failure_count() == LOCAL_FALLBACK_FAILURE_LIMIT
    _capture(monkeypatch, package, _answered())
    provider = build_provider(CHAINED_PROVIDER, api_key="private", env={})
    decision = provider.decide({"case": {"event_type": "manual"}})
    assert decision.raw["provider"] == LOCAL_PROVIDER
    assert local_failure_count() == 0


def test_a_local_only_success_also_resets_the_breaker(monkeypatch):
    package = sys.modules["sentinel.jev"]
    monkeypatch.setattr(package, "_local_failure_count", LOCAL_FALLBACK_FAILURE_LIMIT)
    _capture(monkeypatch, package, _answered())
    LayaJev().decide({"case": {"event_type": "manual"}})
    assert local_failure_count() == 0


@pytest.mark.parametrize(
    "payload",
    [
        _weak_answered(),
        # A confident answer that is still not the safe outcome.
        _weak_answered(choice="notify", score=4.0, action="notify"),
    ],
)
def test_a_weak_or_wrong_local_answer_never_triggers_the_hosted_fallback(
    monkeypatch, payload
):
    transport = _RecordingTransport(lambda request: _Response(payload))
    monkeypatch.setattr(sys.modules["sentinel.jev"], "urlopen", transport)
    provider = build_provider(CHAINED_PROVIDER, api_key="private", env={})
    for _ in range(LOCAL_FALLBACK_FAILURE_LIMIT + 1):
        decision = provider.decide({"case": {"event_type": "manual"}})
        assert decision.raw["provider"] == LOCAL_PROVIDER
        assert decision.raw["attempted"] == [LOCAL_PROVIDER]
        # The local answer is used as it stands, weak or not.
        assert decision.confidence is not None
    # The fallback fires on a local failure, never on a weak or wrong answer. A
    # hosted hop would have answered here, because the transport answers every
    # URL, so the recorded attempts prove no hosted call was made.
    assert transport.attempts == [LOCAL_URL] * (LOCAL_FALLBACK_FAILURE_LIMIT + 1)
    assert local_failure_count() == 0


def test_the_breaker_suppresses_a_local_outage_in_both_copies(monkeypatch):
    package = sys.modules["sentinel.jev"]
    hosted = "https://openrouter.ai/api/alpha/decisions"
    for module, chain in (
        (package, build_provider(CHAINED_PROVIDER, api_key="private", env={})),
        (
            _runtime,
            _runtime.build_provider(
                _runtime.CHAINED_PROVIDER, api_key="private", env={}
            ),
        ),
    ):
        module.reset_local_failures()
        transport = _local_fails(monkeypatch, module, URLError("local server is down"))
        for _ in range(module.LOCAL_FALLBACK_FAILURE_LIMIT):
            assert (
                chain.decide({"case": {"event_type": "manual"}}).raw["provider"]
                == "openrouter"
            )
        with pytest.raises(URLError, match="local server is down"):
            chain.decide({"case": {"event_type": "manual"}})
        assert transport.attempts.count(hosted) == module.LOCAL_FALLBACK_FAILURE_LIMIT
        assert module.local_failure_count() == module.LOCAL_FALLBACK_FAILURE_LIMIT + 1
        module.reset_local_failures()


def test_a_local_failure_logs_only_its_category(caplog):
    private = "the bedroom window is open and the alarm code is 4321"
    with caplog.at_level(logging.WARNING, logger="sentinel.jev"):
        assert note_local_failure(RuntimeError(private)) is True
    assert "RuntimeError" in caplog.text
    assert private not in caplog.text
    assert "4321" not in caplog.text


def test_a_suppressed_fallback_logs_the_category_not_the_case(monkeypatch, caplog):
    marker = "MARKER-bedroom-window-9911"
    package = sys.modules["sentinel.jev"]
    hosted = "https://openrouter.ai/api/alpha/decisions"
    transport = _local_fails(monkeypatch, package, URLError("local server is down"))
    provider = build_provider(CHAINED_PROVIDER, api_key="private", env={})
    state = {"case": {"event_type": "manual", "facts": {"note": marker}}}
    with caplog.at_level(logging.WARNING, logger="sentinel.jev"):
        for _ in range(LOCAL_FALLBACK_FAILURE_LIMIT):
            provider.decide(state)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="sentinel.jev"):
        with pytest.raises(URLError, match="local server is down"):
            provider.decide(state)
    assert "URLError" in caplog.text
    assert "suppressed" in caplog.text
    assert marker not in caplog.text
    assert transport.attempts.count(hosted) == LOCAL_FALLBACK_FAILURE_LIMIT


# ---------------------------------------------------------------------------
# the four modes: chain behaviour, fallback triggers, and redaction
# ---------------------------------------------------------------------------


def test_the_hosted_first_chain_calls_the_api_and_falls_through_to_local(monkeypatch):
    """``api_with_local_fallback`` leads with the API and the local slot behind.

    This is the one arrangement v1.3.0 did not have, so its behaviour is proved
    here rather than inferred from the local-first chain.
    """
    package = sys.modules["sentinel.jev"]
    hosted = "https://openrouter.ai/api/alpha/decisions"
    provider = build_provider(
        API_WITH_LOCAL_FALLBACK,
        api_key="private",
        env={},
        local_model="your-local-engine",
    )
    assert isinstance(provider, ChainedJev)
    assert provider.names == ("openrouter", LOCAL_PROVIDER)

    # The API answers, so the local slot is never reached.
    seen = _capture(monkeypatch, package, _answered())
    decision = provider.decide({"case": {"event_type": "manual"}})
    assert seen["url"] == hosted
    assert decision.raw["provider"] == "openrouter"
    assert decision.raw["attempted"] == ["openrouter"]

    # A refusal on the API falls through to the local slot, and the local hop
    # asks for the configured engine rather than the default.
    def _api_refuses_then_local_answers(request):
        if request.full_url == hosted:
            raise _http_error(503)
        assert json.loads(request.data.decode())["model"] == "your-local-engine"
        return _Response(_answered())

    transport = _RecordingTransport(_api_refuses_then_local_answers)
    monkeypatch.setattr(package, "urlopen", transport)
    decision = provider.decide({"case": {"event_type": "manual"}})
    assert transport.attempts == [hosted, LOCAL_URL]
    assert transport.headers[1] == {"Content-Type": "application/json"}
    assert decision.raw["provider"] == LOCAL_PROVIDER
    assert decision.raw["attempted"] == ["openrouter", LOCAL_PROVIDER]


def test_the_single_provider_modes_never_reroute(monkeypatch):
    """``api_only`` and ``local_only`` report a failure; they never reroute."""

    def _refuses(request, timeout=None):
        raise URLError("provider is unreachable")

    package = sys.modules["sentinel.jev"]
    for mode in (API_ONLY, LOCAL_ONLY):
        monkeypatch.setattr(package, "urlopen", _refuses)
        provider = build_provider(mode, api_key="private", env={})
        with pytest.raises(URLError):
            provider.decide({"case": {"event_type": "manual"}})
    # Neither single-provider mode is a chain, so there is no second hop that a
    # failure could have reached.
    assert isinstance(
        build_provider(API_ONLY, api_key="private", env={}), OpenRouterJev
    )
    assert isinstance(build_provider(LOCAL_ONLY, env={}), LayaJev)


def test_only_the_documented_triggers_fall_through_on_both_chains(monkeypatch):
    """A failure that is not a documented trigger never reaches the next hop.

    The hosted and local adapters do not run the typed answer validator, so this
    asserts the part that is guaranteed on both fallback modes: the trigger set
    is the documented one, and it is the same set whichever side leads.
    """
    package = sys.modules["sentinel.jev"]
    hosted = "https://openrouter.ai/api/alpha/decisions"
    # Documented triggers: unreachable, refused, rate limited, or broken.
    for error in (
        URLError("down"),
        TimeoutError("timed out"),
        _http_error(401),
        _http_error(429),
        _http_error(503),
    ):
        assert is_fallback_trigger(error) is True, error
    # Anything else propagates, on the hosted-first chain and the local-first one.
    for error in (_http_error(400), _http_error(422), KeyError("answers")):
        assert is_fallback_trigger(error) is False, error
    for mode, first, second in (
        (API_WITH_LOCAL_FALLBACK, hosted, LOCAL_URL),
        (LOCAL_WITH_API_FALLBACK, LOCAL_URL, hosted),
    ):

        def _first_fails(request, timeout=None):
            if request.full_url == first:
                raise _http_error(422)
            return _Response(_answered())

        transport = _RecordingTransport(_first_fails)
        monkeypatch.setattr(package, "urlopen", transport)
        provider = build_provider(
            mode, api_key="private", env={}, local_model="your-local-engine"
        )
        with pytest.raises(HTTPError):
            provider.decide({})
        assert transport.attempts == [first], mode


def test_the_four_modes_agree_between_the_package_and_the_runtime_bridge():
    """The contract exists twice, so the two copies must resolve identically."""
    package = sys.modules["sentinel.providers"]
    both = {
        "OPENROUTER_API_KEY": "private",
        "CLOUDFLARE_API_TOKEN": "private",
        "CLOUDFLARE_ACCOUNT_ID": "private-account",
    }
    for mode in CANONICAL_MODES:
        for env in (both, {}, {"LAYA_API_KEY": "k"}):
            assert _order_or_error(package, mode, env) == _order_or_error(
                _runtime, mode, env
            ), (mode, env)
    # Both copies refuse the same names, with the same text.
    for unknown in (
        "nonsense",
        "Laya",
        "your-local-engine",
        "local_with_api_fallback_now",
    ):
        assert _order_or_error(package, unknown, both) == _order_or_error(
            _runtime, unknown, both
        ), unknown
        assert package.resolve_provider(unknown) == _runtime.resolve_provider(unknown)
    for name in MODE_ALIASES:
        assert _runtime.resolve_provider(name) == package.resolve_provider(name), name
        assert _runtime.provider_mode(name) == package.provider_mode(name), name
    for env in ({}, dict(both), {"LAYA_API_KEY": "k"}):
        for model in (None, "your-local-engine", "another-local-engine"):
            assert _order_or_error(
                package, API_WITH_LOCAL_FALLBACK, env, local_model=model
            ) == _order_or_error(
                _runtime, _runtime.API_WITH_LOCAL_FALLBACK, env, local_model=model
            ), (
                env,
                model,
            )
            assert package.local_checkpoint(model) == _runtime.local_checkpoint(model)
    # The refusal text for an unusable local model is identical too.
    for bad in ("", "a b", "a?b", 123):
        texts = []
        for call in (package.local_checkpoint, _runtime.local_checkpoint):
            with pytest.raises(ValueError) as raised:
                call(bad)
            texts.append(str(raised.value))
        assert texts[0] == texts[1], bad
        assert "invalid local_model" in texts[0]


def _order_or_error(module_or_function, name, env, **kwargs):
    """The resolved order, or the text of the error it raised."""
    function = getattr(module_or_function, "provider_order", module_or_function)
    try:
        return function(name, env=env, **kwargs)
    except ValueError as exc:
        return f"ValueError: {exc}"


def test_no_diagnostic_or_failure_message_carries_a_credential_or_case(caplog):
    """Existing repository policy: no payload in a message or a log record.

    A mode resolution failure names a variable and a mode. Neither a stored key,
    nor a case marker, nor an answer, nor an entity state appears in it.
    """
    marker = "MARKER-bedroom-window-9911"
    secret = "stored-openrouter-key-9911"
    with caplog.at_level(logging.WARNING):
        for name in (
            LOCAL_WITH_API_FALLBACK,
            API_WITH_LOCAL_FALLBACK,
            API_ONLY,
            "openrouter",
            "clef",
        ):
            # No key is supplied at all, so the hosted side is genuinely absent
            # and the refusal names the variable rather than a value.
            with pytest.raises(ValueError) as raised:
                build_provider(name, env={"LAYA_API_KEY": "k"})
            message = str(raised.value)
            assert secret not in message, name
            assert marker not in message
            # The variable name is named so a misconfiguration is actionable,
            # and no value is. Each name is refused for its own side: the
            # Clef names name the Cloudflare variables, which is correct.
            assert "OPENROUTER_API_KEY" in message or (
                "CLOUDFLARE_API_TOKEN" in message and "CLOUDFLARE_ACCOUNT_ID" in message
            ), name
    assert secret not in caplog.text
    assert marker not in caplog.text


def test_the_fallback_modes_are_the_only_two_that_chain():
    """Two of the four modes are chains; two are single-provider routes."""
    env = {"OPENROUTER_API_KEY": "private"}
    assert len(provider_order(API_WITH_LOCAL_FALLBACK, env=env)) == 2
    assert len(provider_order(LOCAL_WITH_API_FALLBACK, env=env)) == 2
    assert len(provider_order(API_ONLY, env=env)) == 1
    assert len(provider_order(LOCAL_ONLY, env=env)) == 1
    for mode in CANONICAL_MODES:
        assert provider_mode(mode) == mode


# ---------------------------------------------------------------------------
# credential resolution: an empty value is a missing value, never a borrowed one
# ---------------------------------------------------------------------------


def test_an_explicitly_empty_credential_is_never_borrowed_from_the_environment(
    monkeypatch,
):
    """A caller that passes an empty credential means "none", not "look it up".

    Falling through to the process environment on an empty value is how a real
    key leaks out of a machine and into a request the caller was trying to keep
    out of one. Every adapter that resolves a credential is covered, in both
    copies.
    """
    secrets = {
        "OPENROUTER_API_KEY": "real-machine-openrouter-key",
        "CLOUDFLARE_API_TOKEN": "real-machine-cloudflare-token",
        "CLOUDFLARE_ACCOUNT_ID": "real-machine-account",
        "LAYA_API_KEY": "real-machine-laya-token",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    for module in (sys.modules["sentinel.jev"], _runtime):
        assert module.OpenRouterJev("").api_key == ""
        assert module.ClefJev("", account_id="acct").api_token == ""
        assert module.LayaJev(api_key="").api_key == ""
        # None still means "read the environment", which is the contract a
        # caller with no value of its own relies on.
        assert module.OpenRouterJev().api_key == secrets["OPENROUTER_API_KEY"]
        assert module.ClefJev().api_token == secrets["CLOUDFLARE_API_TOKEN"]
        assert module.LayaJev().api_key == secrets["LAYA_API_KEY"]


def test_the_entry_builder_does_not_forward_the_stored_key_to_clef(monkeypatch):
    """An entry holds one credential, and it belongs to the hosted Jev hops."""
    secret = "stored-openrouter-key-9911"
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "env-clef-token")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "env-acct")
    clef_env = {
        "CLOUDFLARE_API_TOKEN": "env-clef-token",
        "CLOUDFLARE_ACCOUNT_ID": "env-acct",
        "OPENROUTER_API_KEY": secret,
    }
    for name in (
        "clef",
        "clef_api",
        "clef_then_jev",
        "clef_with_jev_fallback",
        "clef_with_local_fallback",
    ):
        provider = build_provider(name, api_key=secret, env=clef_env)
        assert secret not in repr(provider), name
    # A Clef route built with the stored key still contributes no key to the
    # Clef adapter: it reads its own credential from the environment.
    clef = build_provider("clef", api_key=secret, env=clef_env)
    assert clef.api_token == "env-clef-token"
    assert secret not in repr(clef)
    # The key is still used where it belongs: on the hosted Jev hop behind Clef.
    chain = build_provider("clef_with_jev_fallback", api_key=secret, env=clef_env)
    assert isinstance(chain, ChainedJev)
    hosted = [adapter for name, adapter in chain.providers if name == "openrouter"]
    assert hosted and hosted[0].api_key == secret
