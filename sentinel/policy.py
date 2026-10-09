"""Deterministic authority boundary around Jev recommendations.

The policy used to be an allowlist of action names. That is not a boundary: it
cannot tell ``light.turn_on`` on ``light.living_room`` from the same action on a
nursery blackout lamp, and it carries no bounds at all on the values an action
carries, so ``climate.set_temperature`` with a temperature of 40 C is allowlisted
with no approval and no complaint.

``authorize`` therefore takes the request a Home Assistant service call actually
carries — the action, the entity it targets, and the service data — and answers
for that request, not for a name. Three scopes are checked:

- the action name, against ``allowed_actions`` and ``approval_required``;
- the target entity, against ``restricted_entity_patterns``. An entity whose
  identifier says the change has a consequence a policy cannot assume — a
  nursery, a blackout lamp, an incubator, a freezer — needs an approval even when
  its action is otherwise allowed. A caller may replace the default set;
- the service data, against ``value_ranges``. A parameter outside its range is
  refused outright, because no approval makes an out-of-range value a safe one.

``user_approved`` is a provenance record rather than a bare boolean. A boolean
says someone approved something; ``Approval`` says who, when, and of what, and an
approval that names a scope it does not cover is not an approval for this
request. Nothing in this repository sets one, which is why the record is a
first-class value rather than a keyword this module invents for itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

# The actions this policy considers reversible and bounded, so they need no
# human. ``notify`` and ``ask_user`` reach nobody's device, so they are declared
# rather than dispatched.
ALLOWED_ACTIONS = frozenset(
    {
        "notify",
        "ask_user",
        "light.turn_on",
        "light.turn_off",
        "switch.turn_on",
        "switch.turn_off",
        "climate.set_temperature",
    }
)

# The actions that move a lock, a valve, or a garage, or silence an alarm. A
# human decides these, every time, whoever asks.
APPROVAL_REQUIRED = frozenset(
    {
        "lock.unlock",
        "alarm_control_panel.alarm_disarm",
        "cover.open_garage",
        "water_valve.close",
    }
)

# Entity identifiers that name a consequence this policy will not assume on its
# own. The match is by segment of the entity id, so ``light.nursery_blackout``
# matches ``nursery`` and ``light.living_room`` does not. A match does not refuse
# the action; it requires an approval that covers this request, exactly as the
# ``approval_required`` names do.
RESTRICTED_ENTITY_PATTERNS = frozenset(
    {
        "nursery",
        "baby",
        "infant",
        "child",
        "blackout",
        "incubator",
        "medical",
        "medication",
        "aquarium",
        "terrarium",
        "vivarium",
        "freezer",
        "fridge",
        "refrigerator",
    }
)

# The inclusive numeric bounds for a parameter of an action, in the unit Home
# Assistant sends it in. ``climate.set_temperature`` had no bounds at all, which
# is how an AI decision of 40 C on a nursery climate was authorized with no
# approval. A value outside its range is refused: it names a request nobody
# should make, and an approval cannot make it one somebody should.
VALUE_RANGES: Mapping[str, Mapping[str, tuple[float, float]]] = {
    "climate.set_temperature": {
        "temperature": (5.0, 30.0),
        "target_temp_high": (5.0, 30.0),
        "target_temp_low": (5.0, 30.0),
    },
}


# The separators an entity id is built from. A restricted-pattern match is a
# whole-segment match, so this is the one place the segments of a name are
# decided.
_SEGMENT_SEPARATOR = re.compile(r"[^0-9a-z]+")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Approval:
    """Who approved which request, and when.

    ``by`` is the identity that approved and ``scope`` the request it approved:
    an action name, an entity id, or ``action@entity``. ``None`` approves
    anything the action itself permits, which is what an unrestricted approval
    means. ``expires_at`` is the ISO 8601 instant after which the approval is no
    longer one; an approval that has outlived its window is refused rather than
    honoured, because a decision made for one moment is not authority for the
    next.
    """

    by: str
    scope: str | tuple[str, ...] | None = None
    at: str = field(default_factory=_utcnow)
    expires_at: str | None = None

    @staticmethod
    def coerce(
        value: "Approval | Mapping[str, Any] | bool | None",
    ) -> "Approval | None":
        """Read an approval from a record, a mapping, a bare boolean, or nothing.

        A bare ``True`` still works, because that is what callers passed before
        the record existed. It is recorded as an approval by ``"caller"`` with
        no scope and no expiry, which is the least provenance the value can
        carry while remaining a boolean.
        """
        if value is None or value is False:
            return None
        if isinstance(value, Approval):
            return value
        if isinstance(value, Mapping):
            by = str(value.get("by") or value.get("approved_by") or "caller")
            scope = value.get("scope")
            if isinstance(scope, (list, tuple)):
                scope = tuple(str(item) for item in scope)
            elif scope is not None:
                scope = str(scope)
            return Approval(
                by=by,
                scope=scope,
                at=str(value.get("at") or value.get("approved_at") or _utcnow()),
                expires_at=(
                    str(value["expires_at"])
                    if value.get("expires_at") is not None
                    else None
                ),
            )
        if value is True:
            return Approval(by="caller")
        return None

    def covers(self, action: str, entity_id: str | None) -> bool:
        """Whether this approval names this request explicitly."""
        if self.scope is None:
            return True
        scopes = (self.scope,) if isinstance(self.scope, str) else tuple(self.scope)
        candidates = [action]
        if entity_id:
            candidates.extend([entity_id, f"{action}@{entity_id}"])
        return any(candidate in scopes for candidate in candidates)

    def expired(self, *, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        moment = now or datetime.now(timezone.utc)
        try:
            until = datetime.fromisoformat(self.expires_at)
        except ValueError:
            # An unparseable expiry is not a permissive one.
            return True
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return moment >= until

    def to_dict(self) -> dict[str, Any]:
        """The provenance recorded alongside the authorization decision."""
        return {
            "by": self.by,
            "at": self.at,
            "scope": (
                list(self.scope) if isinstance(self.scope, tuple) else self.scope
            ),
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class Policy:
    """Allow only declared, bounded, reversible actions unless a human approves.

    The policy answers for one request at a time: the action, the entity it
    targets, and the service data it carries. Every refusal carries the scope
    that refused it, so an operator reading the event bus sees what was checked
    rather than a verdict with no reasons attached.
    """

    allowed_actions: frozenset[str] = ALLOWED_ACTIONS
    approval_required: frozenset[str] = APPROVAL_REQUIRED
    # Replaceable by a caller whose household has entities the default set does
    # not name. An empty set disables the entity check entirely.
    restricted_entity_patterns: frozenset[str] = RESTRICTED_ENTITY_PATTERNS
    value_ranges: Mapping[str, Mapping[str, tuple[float, float]]] = field(
        default_factory=lambda: dict(VALUE_RANGES)
    )

    def authorize(
        self,
        action: str | None,
        entity_id: str | None = None,
        service_data: Mapping[str, Any] | None = None,
        *,
        user_approved: "Approval | Mapping[str, Any] | bool | None" = None,
    ) -> dict[str, object]:
        """Answer for one request, and record what was checked."""
        approval = Approval.coerce(user_approved)
        checked: dict[str, object] = {
            "action": action,
            "entity_id": entity_id,
            "checked_at": _utcnow(),
        }
        if not action:
            return self._refusal(
                "no_action",
                "No action was proposed.",
                checked,
                approval,
            )
        if action not in self.allowed_actions and action not in self.approval_required:
            return self._refusal(
                "not_allowlisted",
                "Action is not in the Sentinel policy.",
                checked,
                approval,
            )
        # The value range is checked before any approval, because an
        # out-of-range parameter is a request nobody should make and an approval
        # does not make it one somebody should.
        out_of_range = self._out_of_range(action, service_data)
        if out_of_range is not None:
            name, (low, high), value = out_of_range
            return self._refusal(
                "value_out_of_range",
                f"{name} {value} is outside the {low} to {high} range the"
                f" policy permits for {action}.",
                {**checked, "field": name, "value": value, "range": [low, high]},
                approval,
            )
        restricted = self._restricted_entity(entity_id)
        if restricted is not None:
            if approval is None:
                return self._refusal(
                    "approval_required",
                    f"{entity_id} names a restricted entity ({restricted}) and"
                    " this policy will not change it without an approval.",
                    {**checked, "restricted_by": restricted},
                    approval,
                )
            if approval.expired():
                return self._refusal(
                    "approval_expired",
                    f"The approval for {entity_id} expired at"
                    f" {approval.expires_at}.",
                    {**checked, "restricted_by": restricted},
                    approval,
                )
            if not approval.covers(action, entity_id):
                return self._refusal(
                    "approval_out_of_scope",
                    f"The approval by {approval.by} does not name {action} on"
                    f" {entity_id}.",
                    {**checked, "restricted_by": restricted},
                    approval,
                )
        if action in self.approval_required:
            if approval is None:
                return self._refusal(
                    "approval_required",
                    "This action needs explicit user approval.",
                    checked,
                    approval,
                )
            if approval.expired():
                return self._refusal(
                    "approval_expired",
                    f"The approval expired at {approval.expires_at}.",
                    checked,
                    approval,
                )
            if not approval.covers(action, entity_id):
                return self._refusal(
                    "approval_out_of_scope",
                    f"The approval by {approval.by} does not name {action} on"
                    f" {entity_id}.",
                    checked,
                    approval,
                )
        return {
            "allowed": True,
            "status": "approved",
            "reason": "Policy allows this action.",
            "approval": approval.to_dict() if approval is not None else None,
            **checked,
        }

    # -- internals -------------------------------------------------------

    def _refusal(
        self,
        status: str,
        reason: str,
        checked: dict[str, object],
        approval: Approval | None,
    ) -> dict[str, object]:
        return {
            "allowed": False,
            "status": status,
            "reason": reason,
            "approval": approval.to_dict() if approval is not None else None,
            **checked,
        }

    def _restricted_entity(self, entity_id: str | None) -> str | None:
        """The pattern a target entity matches, if any.

        The entity id is split on every separator at once and each segment is
        compared whole, so ``light.nursery_blackout`` is restricted and
        ``light.living_room`` is not, and no pattern can be reached by a
        substring of a longer word.
        """
        if not entity_id or not self.restricted_entity_patterns:
            return None
        # Split on every separator at once. Splitting on one at a time gives
        # ``light.nursery_room`` the segments ``light``, ``nursery_room``,
        # ``light.nursery``, and ``room`` — never ``nursery``, which is the one
        # the policy is looking for.
        segments = {
            part.lower() for part in _SEGMENT_SEPARATOR.split(str(entity_id)) if part
        }
        # Sorted, so the pattern reported in a refusal is the same one every
        # time. An unordered set made the reason code an implementation detail
        # of whichever pattern the interpreter reached first.
        for pattern in sorted(self.restricted_entity_patterns):
            if pattern.lower() in segments:
                return pattern
        return None

    def _out_of_range(
        self, action: str, service_data: Mapping[str, Any] | None
    ) -> tuple[str, tuple[float, float], Any] | None:
        """The first parameter of this action that is outside its range."""
        if not service_data:
            return None
        ranges = self.value_ranges.get(action)
        if not ranges:
            return None
        for name, (low, high) in ranges.items():
            if name not in service_data:
                continue
            value = service_data[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if not low <= float(value) <= high:
                return name, (low, high), value
        return None
