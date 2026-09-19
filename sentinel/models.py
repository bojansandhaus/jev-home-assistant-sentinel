"""Small, JSON friendly Sentinel data contracts."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Case:
    """A bounded Home Assistant event presented to a decision workflow."""

    case_id: str
    event_type: str
    area: str | None
    entities: tuple[str, ...]
    facts: dict[str, Any]
    requested_action: str | None = None
    created_at: str = field(default_factory=_now)

    @classmethod
    def create(
        cls,
        event_type: str,
        *,
        area: str | None = None,
        entities: list[str] | tuple[str, ...] = (),
        facts: dict[str, Any] | None = None,
        requested_action: str | None = None,
    ) -> "Case":
        return cls(
            case_id=f"case_{uuid.uuid4().hex[:12]}",
            event_type=event_type,
            area=area,
            entities=tuple(entities),
            facts=dict(facts or {}),
            requested_action=requested_action,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Decision:
    """Jev's bounded recommendation. It is never execution authority."""

    outcome: str
    reason: str
    confidence: float | None = None
    action: str | None = None
    shadow: bool = True
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Verification:
    """Deterministic readback result after a Home Assistant action."""

    status: str
    verified: bool
    expected: Any = None
    actual: Any = None
    next_step: str = "inspect"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
