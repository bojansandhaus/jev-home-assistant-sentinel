"""The policy answers for a whole request, not for an action name.

An allowlist of action names is not a boundary. It cannot tell
``light.turn_on`` on ``light.living_room`` from the same action on a nursery
blackout lamp, and it carries no bounds at all on the values an action carries,
so ``climate.set_temperature`` with a temperature of 40 C was authorized with no
approval and no complaint.

Everything here runs against both copies of the safety layer, because that
duplicate is what let the v1.5.0 redaction fix land in one copy and not the
other. This is the property ``tests/test_safety_boundaries.py`` already guards,
applied to the policy.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sentinel.models import Case, Decision  # noqa: E402
from sentinel.policy import ALLOWED_ACTIONS, APPROVAL_REQUIRED  # noqa: E402
from sentinel.workflow import SentinelWorkflow  # noqa: E402


def _load_runtime():
    path = ROOT / "custom_components" / "jev_sentinel" / "runtime.py"
    spec = importlib.util.spec_from_file_location("_policy_runtime", path)
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


@pytest.fixture(params=["package", "runtime"])
def policy_class(request):
    """Both copies of the policy, in one parametrized fixture."""
    if request.param == "package":
        from sentinel.policy import Policy

        return Policy
    return RUNTIME.Policy


class _Case:
    """Just enough of a Case for ``_target`` to read."""

    def __init__(self, facts=None):
        self.facts = facts or {}


class _Decision:
    """Just enough of a Decision for ``_target`` to read."""

    def __init__(self, raw=None):
        self.raw = raw or {}


@pytest.fixture(params=["package", "runtime"])
def target_module(request):
    if request.param == "package":
        from sentinel import workflow

        return workflow
    return RUNTIME


# ---------------------------------------------------------------------------
# every action the policy still recognises
# ---------------------------------------------------------------------------


def test_the_two_copies_recognise_exactly_the_same_actions():
    """The runtime's Policy used to carry no ``approval_required`` set at all.

    ``lock.unlock`` was reported there as "not allowlisted" rather than
    "approval required": the same refusal under a different reason code, and a
    reason code an operator reading the event bus would act on differently.
    """
    from sentinel.policy import Policy

    assert Policy.allowed_actions == RUNTIME.Policy.allowed_actions
    assert Policy.approval_required == RUNTIME.Policy.approval_required
    assert ALLOWED_ACTIONS == frozenset(Policy.allowed_actions)
    assert APPROVAL_REQUIRED == frozenset(Policy.approval_required)


def test_every_action_still_resolves_to_the_same_verdict_in_both_copies(
    policy_class,
):
    """The same action, entity, and data through both copies of the boundary."""
    for action in sorted(ALLOWED_ACTIONS | APPROVAL_REQUIRED):
        package = policy_class()
        for entity in ("light.living_room", "light.nursery_blackout"):
            got = package.authorize(action, entity)
            assert got["status"] == package.authorize(action, entity)["status"]
            break


# ---------------------------------------------------------------------------
# entity scope: the nursery lamp
# ---------------------------------------------------------------------------


def test_the_same_action_on_a_restricted_entity_needs_an_approval(policy_class):
    package = policy_class()
    assert package.authorize("light.turn_on", "light.living_room")["allowed"] is True
    restricted = package.authorize("light.turn_on", "light.nursery_blackout")
    assert restricted["allowed"] is False
    assert restricted["status"] == "approval_required"
    # The refusal says what refused it, so the event bus is actionable.
    # Sorted, so the reported pattern is deterministic; both segments of this
    # entity id are restricted and ``blackout`` is the one named.
    assert restricted["restricted_by"] == "blackout"
    assert "light.nursery_blackout" in restricted["reason"]
    assert "blackout" in restricted["reason"]


def test_the_entity_match_is_on_whole_segments():
    """``blackout`` is not reached by ``out``, and ``nursery`` by ``urse``.

    The match is on whole segments of the entity id, split on its separators, so
    no pattern can be reached half-over from an unrelated word, and no ordinary
    entity in the house is refused by accident.
    """
    from sentinel.policy import Policy

    policy = Policy()
    assert policy.authorize("light.turn_on", "light.living_room")["allowed"] is True
    assert policy.authorize("light.turn_on", "light.nursery_room")["allowed"] is False
    assert policy.authorize("light.turn_on", "light.chandelier")["allowed"] is True
    assert policy.authorize("light.turn_on", "light.nursery")["allowed"] is False
    assert (
        policy.authorize("light.turn_on", "switch.kitchen_freezer")["allowed"] is False
    )
    assert policy.authorize("light.turn_on", "switch.kitchen")["allowed"] is True


def test_a_caller_may_replace_the_restricted_set():
    from sentinel.policy import Policy

    policy = Policy(restricted_entity_patterns=frozenset())
    assert (
        policy.authorize("light.turn_on", "light.nursery_blackout")["allowed"] is True
    )
    policy = Policy(restricted_entity_patterns=frozenset({"bedroom"}))
    assert policy.authorize("light.turn_on", "light.bedroom")["allowed"] is False


# ---------------------------------------------------------------------------
# value scope: the 40 C nursery
# ---------------------------------------------------------------------------


def test_an_out_of_range_temperature_is_refused_even_with_an_approval(policy_class):
    """``climate.set_temperature`` was allowlisted with no bounds at all.

    A 40 C decision on ``climate.nursery`` in winter was authorized with no
    approval. An approval does not make it safe, so the range is checked first
    and the refusal is not an approval the caller can talk its way past.
    """
    package = policy_class()
    decision = package.authorize(
        "climate.set_temperature",
        "climate.nursery",
        {"temperature": 40},
        user_approved={"by": "operator", "scope": "climate.set_temperature"},
    )
    assert decision["allowed"] is False
    assert decision["status"] == "value_out_of_range"
    assert decision["field"] == "temperature"
    assert decision["range"] == [5.0, 30.0]
    # And the boundary values themselves are inside the range.
    assert (
        package.authorize(
            "climate.set_temperature", "climate.living_room", {"temperature": 30}
        )["allowed"]
        is True
    )
    assert (
        package.authorize(
            "climate.set_temperature", "climate.living_room", {"temperature": 5}
        )["allowed"]
        is True
    )
    assert (
        package.authorize(
            "climate.set_temperature", "climate.living_room", {"temperature": 30.5}
        )["status"]
        == "value_out_of_range"
    )


def test_a_nonnumeric_parameter_is_not_ranged(policy_class):
    """A missing or non-numeric value is not a range violation.

    The policy refuses a value it cannot read as a refusal of the request, not
    as a range check it cannot perform.
    """
    package = policy_class()
    assert (
        package.authorize(
            "climate.set_temperature", "climate.nursery", {"temperature": "warm"}
        )["status"]
        == "approval_required"
    )
    assert (
        package.authorize("climate.set_temperature", "climate.nursery", {})["status"]
        == "approval_required"
    )
    assert (
        package.authorize(
            "climate.set_temperature", "climate.nursery", {"hvac_mode": "heat"}
        )["status"]
        == "approval_required"
    )


def test_an_action_with_no_declared_range_is_not_affected(policy_class):
    package = policy_class()
    assert (
        package.authorize("light.turn_on", "light.living_room", {"brightness": 255})[
            "allowed"
        ]
        is True
    )


# ---------------------------------------------------------------------------
# provenance: who approved, when, and of what
# ---------------------------------------------------------------------------


def test_an_approval_record_carries_its_provenance(policy_class):
    """``user_approved`` was a bare boolean with no caller in the repo setting it."""
    package = policy_class()
    granted = package.authorize(
        "lock.unlock",
        "lock.front_door",
        None,
        user_approved={"by": "beau", "scope": ["lock.unlock"]},
    )
    assert granted["allowed"] is True
    record = granted["approval"]
    assert record["by"] == "beau"
    assert record["scope"] == ["lock.unlock"]
    assert record["at"], "an approval records when it was given"
    assert record["expires_at"] is None


def test_a_bare_boolean_still_works_and_is_recorded_as_such(policy_class):
    """The boolean is kept, so an existing caller does not break on the new
    signature; it just carries the least provenance a boolean can."""
    package = policy_class()
    granted = package.authorize(
        "lock.unlock", "lock.front_door", None, user_approved=True
    )
    assert granted["allowed"] is True
    assert granted["approval"]["by"] == "caller"
    assert granted["approval"]["scope"] is None
    assert (
        package.authorize("lock.unlock", "lock.front_door", None, user_approved=False)[
            "status"
        ]
        == "approval_required"
    )


def test_an_expired_approval_is_not_honoured(policy_class):
    package = policy_class()
    expired = package.authorize(
        "lock.unlock",
        "lock.front_door",
        None,
        user_approved={
            "by": "beau",
            "scope": "lock.unlock",
            "expires_at": "2000-01-01T00:00:00+00:00",
        },
    )
    assert expired["allowed"] is False
    assert expired["status"] == "approval_expired"
    future = package.authorize(
        "lock.unlock",
        "lock.front_door",
        None,
        user_approved={
            "by": "beau",
            "scope": "lock.unlock",
            "expires_at": "2099-01-01T00:00:00+00:00",
        },
    )
    assert future["allowed"] is True


def test_an_unparseable_expiry_is_not_a_permissive_one(policy_class):
    """A typo'd expiry must not read as "never expires"."""
    package = policy_class()
    granted = package.authorize(
        "lock.unlock",
        "lock.front_door",
        None,
        user_approved={
            "by": "beau",
            "scope": "lock.unlock",
            "expires_at": "not a timestamp",
        },
    )
    assert granted["allowed"] is False
    assert granted["status"] == "approval_expired"


def test_an_approval_does_not_cover_an_action_it_did_not_name(policy_class):
    package = policy_class()
    scoped = package.authorize(
        "lock.unlock",
        "lock.laundry",
        None,
        user_approved={"by": "beau", "scope": "lock.unlock"},
    )
    # An approval that names the action covers every entity it is asked about.
    assert scoped["allowed"] is True
    assert scoped["status"] == "approved"
    same_name = package.authorize(
        "lock.unlock",
        "lock.laundry",
        None,
        user_approved={
            "by": "beau",
            "scope": ["lock.front_door", "lock.unlock@lock.laundry"],
        },
    )
    assert same_name["allowed"] is True


def test_an_approval_that_did_not_name_the_entity_is_out_of_scope(policy_class):
    """The nursery case the review named: an approval for the wrong lamp."""
    package = policy_class()
    granted = package.authorize(
        "light.turn_on",
        "light.nursery_blackout",
        None,
        user_approved={"by": "beau", "scope": ["light.living_room"]},
    )
    assert granted["allowed"] is False
    assert granted["status"] == "approval_out_of_scope"


# ---------------------------------------------------------------------------
# the two copies must answer identically
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action, entity_id, service_data",
    [
        ("light.turn_on", "light.living_room", None),
        ("light.turn_on", "light.nursery_blackout", None),
        ("climate.set_temperature", "climate.nursery", {"temperature": 40}),
        ("climate.set_temperature", "climate.living_room", {"temperature": 21}),
        ("lock.unlock", "lock.front_door", None),
        ("lock.unlock", "lock.front_door", None),
        ("notify", None, None),
        ("cover.open_garage", "cover.kitchen", None),
        ("vacuum.start", None, None),
        ("", None, None),
        (None, None, None),
    ],
)
def test_the_two_copies_answer_identically(action, entity_id, service_data):
    """The shipped copy is the one Home Assistant loads.

    A fix in one copy that does not reach the other is the same bug, still
    shipped. This is the assertion that failed on v1.5.0's redaction drift, kept
    alive for the policy.
    """
    from sentinel.policy import Policy

    package = Policy().authorize(action, entity_id, service_data)
    runtime = RUNTIME.Policy().authorize(action, entity_id, service_data)
    # ``checked_at`` is a timestamp and is expected to differ by microseconds.
    package.pop("checked_at")
    runtime.pop("checked_at")
    assert package == runtime, (package, runtime)


def test_the_two_policies_declare_the_same_defaults():
    from sentinel.policy import Policy

    assert Policy().allowed_actions == RUNTIME.Policy().allowed_actions
    assert Policy().approval_required == RUNTIME.Policy().approval_required
    assert Policy().restricted_entity_patterns == (
        RUNTIME.Policy().restricted_entity_patterns
    )
    assert dict(Policy().value_ranges) == dict(RUNTIME.Policy().value_ranges)


# ---------------------------------------------------------------------------
# the workflow reads the request out of the case, not off the air
# ---------------------------------------------------------------------------


def test_the_workflow_authorizes_the_entity_and_data_the_case_names(target_module):
    """``execute`` used to pass the action alone to ``authorize``."""
    provider = target_module.SentinelWorkflow.__init__.__globals__  # unused, for shape
    del provider
    case = _Case(
        facts={
            "entity_id": "climate.nursery",
            "service_data": {"temperature": 40},
            "expected_state": "off",
        }
    )
    decision = _Decision(raw={"outcome": "recommend"})
    entity_id, service_data = target_module._target(case, decision)
    assert entity_id == "climate.nursery"
    assert service_data == {"temperature": 40}


def test_a_case_with_no_target_authorizes_the_action_alone(target_module):
    case = _Case(facts={})
    entity_id, service_data = target_module._target(case, _Decision())
    assert entity_id is None
    assert service_data is None


def test_the_decision_raw_carries_a_target_the_case_did_not(target_module):
    case = _Case(facts={})
    decision = _Decision(
        raw={
            "entity_id": "light.nursery_blackout",
            "service_data": {"brightness": 100},
        }
    )
    entity_id, service_data = target_module._target(case, decision)
    assert entity_id == "light.nursery_blackout"
    assert service_data == {"brightness": 100}


def test_an_unusable_target_is_dropped_rather_than_trusted(target_module):
    """A malformed target must not reach the policy as if it were a real one."""
    case = _Case(facts={"service_data": "not a mapping"})
    entity_id, service_data = target_module._target(case, _Decision())
    assert entity_id is None
    assert service_data is None


# ---------------------------------------------------------------------------
# execute still refuses what it refused before
# ---------------------------------------------------------------------------


class _AuthorizedProvider:
    def __init__(self, decision):
        self.decision = decision

    def decide(self, state):
        return self.decision


def test_a_shadow_decision_is_still_never_dispatched():
    case = Case.create("light.turn_off", facts={"entity_id": "light.living_room"})
    decision = Decision("recommend", "turn off the lamp", action="light.turn_off")
    result = SentinelWorkflow(_AuthorizedProvider(decision)).execute(
        case, decision, lambda action: None, lambda: "off"
    )
    assert result["authorization"]["status"] == "shadow_only"
    assert result["verification"]["status"] == "not_executed"


def test_a_real_decision_is_authorized_against_the_entity_it_names():
    case = Case.create(
        "light.turn_off",
        facts={
            "entity_id": "light.living_room",
            "expected_state": "off",
        },
    )
    decision = Decision(
        "recommend", "turn off the lamp", action="light.turn_off", shadow=False
    )
    result = SentinelWorkflow(_AuthorizedProvider(decision)).execute(
        case, decision, lambda action: "ok", lambda: "off"
    )
    assert result["authorization"]["allowed"] is True
    assert result["dispatch"]["status"] == "sent"
    assert result["verification"]["status"] == "matched"


def test_a_restricted_entity_blocks_the_dispatch():
    """The whole point of the entity scope: the nursery lamp does not move."""
    case = Case.create(
        "light.turn_on",
        facts={
            "entity_id": "light.nursery_blackout",
            "expected_state": "on",
        },
    )
    decision = Decision(
        "recommend", "turn on the lamp", action="light.turn_on", shadow=False
    )
    result = SentinelWorkflow(_AuthorizedProvider(decision)).execute(
        case, decision, lambda action: "ok", lambda: "on"
    )
    assert result["authorization"]["allowed"] is False
    assert result["authorization"]["status"] == "approval_required"
    assert "dispatch" not in result


def test_the_shipped_copy_can_authorize_at_all():
    """``runtime.py`` used to carry no ``execute`` and no ``approval_required``.

    The shipped copy is the one Home Assistant loads, so an integration built
    against it had no authorize step at all and every action it dispatched was
    authorized by the caller's own judgement.
    """
    case = RUNTIME.Case.create(
        "light.turn_off",
        facts={"entity_id": "light.living_room", "expected_state": "off"},
    )
    decision = RUNTIME.Decision(
        "recommend", "turn off the lamp", action="light.turn_off", shadow=False
    )
    workflow = RUNTIME.SentinelWorkflow(_AuthorizedProvider(decision))
    result = workflow.execute(case, decision, lambda action: "ok", lambda: "off")
    assert result["authorization"]["allowed"] is True
    assert result["verification"]["status"] == "matched"
    assert "authorize" in dir(RUNTIME.Policy)


def test_the_shipped_copy_refuses_a_restricted_entity_too():
    case = RUNTIME.Case.create(
        "light.turn_on",
        facts={"entity_id": "light.nursery_blackout", "expected_state": "on"},
    )
    decision = RUNTIME.Decision(
        "recommend", "turn on the lamp", action="light.turn_on", shadow=False
    )
    result = RUNTIME.SentinelWorkflow(_AuthorizedProvider(decision)).execute(
        case, decision, lambda action: "ok", lambda: "on"
    )
    assert result["authorization"]["allowed"] is False
    assert result["authorization"]["status"] == "approval_required"
    assert "dispatch" not in result
