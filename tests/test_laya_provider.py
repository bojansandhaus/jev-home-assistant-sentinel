"""The local Laya route: selection, wire shape, rubric, and score mapping.

Laya is a local model, not a hosted Jev endpoint. It replaces the hosted route
for a profile instead of joining it, so these tests cover four things: the local
route is a single provider, ``auto`` never selects it, the hosted fallback order
rejects it, and the score rubric is the ordered list a local server accepts.
"""

import importlib.util
import json
import os
import sys
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from sentinel import (
    CHAINED_PROVIDER,
    CONFIDENCE_LEVELS,
    FALLBACK_STATUS_CODES,
    LAYA_BASE_URL,
    LAYA_ENDPOINT_PATH,
    LAYA_MODEL,
    LAYA_TIMEOUT,
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
    provider_order,
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
    for name in ("local", "Laya", "typesafe"):
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
    }
    assert _runtime.DEFAULT_FALLBACK_ORDER == ("openrouter",)
    assert _runtime.provider_order("laya") == ["laya"]
    assert _runtime.provider_order("auto", env={}) == []
    assert _runtime.CHAINED_PROVIDER == CHAINED_PROVIDER
    assert _runtime.PROVIDER_NAMES == ("auto", "openrouter", "laya", CHAINED_PROVIDER)
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
    with pytest.raises(ValueError, match="for the laya_then_hosted route"):
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
    for env in (
        {},
        {"LAYA_API_KEY": "private"},
        {"TYPESAFE_API_KEY": "private"},
        {"OPENROUTER_API_KEY": "   "},
    ):
        with pytest.raises(
            ValueError,
            match="missing OPENROUTER_API_KEY for the laya_then_hosted route",
        ):
            provider_order(CHAINED_PROVIDER, env=env)
    with pytest.raises(
        ValueError, match="missing OPENROUTER_API_KEY for the laya_then_hosted route"
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


def test_the_config_flow_offers_the_chained_route_and_keeps_the_hosted_default():
    root = Path(__file__).parents[1] / "custom_components/jev_sentinel"
    integration = (root / "__init__.py").read_text()
    flow = (root / "config_flow.py").read_text()
    # An existing config entry without a provider field keeps the hosted route.
    assert 'DEFAULT_PROVIDER = "openrouter"' in integration
    for name in ('"openrouter"', "LOCAL_PROVIDER", "CHAINED_PROVIDER"):
        assert name in flow
    assert "api_key_required" in flow


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
