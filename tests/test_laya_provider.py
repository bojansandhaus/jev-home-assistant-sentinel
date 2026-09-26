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
from pathlib import Path

import pytest

from sentinel import (
    CONFIDENCE_LEVELS,
    LAYA_BASE_URL,
    LAYA_ENDPOINT_PATH,
    LAYA_MODEL,
    LAYA_TIMEOUT,
    Case,
    LayaJev,
    OpenRouterJev,
    SentinelWorkflow,
    build_provider,
    confidence_from_score,
    decision_questions,
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
    with pytest.raises(ValueError, match="invalid fallback_order"):
        _runtime.validate_fallback_order(("openrouter", "laya"))
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
