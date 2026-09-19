from sentinel import Case, Decision, Policy, SentinelWorkflow
from sentinel.redaction import redact


class FakeProvider:
    def __init__(self, decision):
        self.decision = decision
        self.received = None

    def decide(self, state):
        self.received = state
        return self.decision


def test_case_is_bounded_and_serializable():
    case = Case.create("window_open_while_heating", area="living_room", entities=["climate.living_room"], facts={"expected_state": "off"})
    payload = case.to_dict()
    assert payload["event_type"] == "window_open_while_heating"
    assert payload["entities"] == ("climate.living_room",)


def test_review_redacts_secrets_before_provider():
    provider = FakeProvider(Decision("notify", "Tell the user"))
    workflow = SentinelWorkflow(provider)
    case = Case.create("manual", facts={"api_token": "do-not-send", "temperature": 21})
    workflow.review(case)
    assert provider.received["case"]["facts"]["api_token"] == "[REDACTED]"
    assert provider.received["case"]["facts"]["temperature"] == 21


def test_disallowed_action_is_not_dispatched():
    provider = FakeProvider(Decision("recommend", "Do not unlock", action="lock.unlock"))
    workflow = SentinelWorkflow(provider)
    case = Case.create("manual", facts={"expected_state": "unlocked"})
    dispatched = []
    result = workflow.execute(case, provider.decision, dispatched.append, lambda: "unlocked")
    assert result["authorization"]["status"] == "approval_required"
    assert dispatched == []
    assert result["verification"]["status"] == "not_executed"


def test_reversible_action_is_sent_and_verified():
    provider = FakeProvider(Decision("recommend", "Turn off lamp", action="light.turn_off"))
    workflow = SentinelWorkflow(provider)
    case = Case.create("lamp_request", entities=["light.lamp"], facts={"expected_state": "off"})
    dispatched = []
    result = workflow.execute(case, provider.decision, dispatched.append, lambda: "off")
    assert dispatched == ["light.turn_off"]
    assert result["verification"]["status"] == "matched"
    assert result["verification"]["verified"] is True


def test_failed_readback_reopens_case():
    provider = FakeProvider(Decision("recommend", "Turn off lamp", action="light.turn_off"))
    workflow = SentinelWorkflow(provider)
    case = Case.create("lamp_request", facts={"expected_state": "off"})
    result = workflow.execute(case, provider.decision, lambda _: None, lambda: "on")
    assert result["verification"]["status"] == "mismatch"
    assert result["verification"]["next_step"] == "reopen_case"


def test_redact_handles_nested_values():
    value = redact({"nested": [{"password": "x"}], "safe": True})
    assert value == {"nested": [{"password": "[REDACTED]"}], "safe": True}
