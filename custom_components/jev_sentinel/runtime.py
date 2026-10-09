"""Self contained runtime used when Home Assistant loads the component.

Home Assistant installs custom_components without installing the repository's
Python package, so this small bridge keeps the integration independently usable.
The public ``sentinel`` package carries the same provider neutral contract for
applications and tests outside Home Assistant, including the same rubric and the
same provider selection rules.

Four decision modes ship here, and each names which side leads:

- ``api_with_local_fallback``: the hosted API leads, the local slot is behind it;
- ``api_only``: the hosted API alone, and a failure is never rerouted;
- ``local_only``: the local slot alone, and a failure is never rerouted;
- ``local_with_api_fallback``: the local slot leads, the hosted API is behind it.

Which hosted model answers the API side is still its own choice and is not
renamed: hosted Jev over an OpenRouter API key, and Cloudflare Clef over a
Workers AI account and API token with ``clef`` and ``clef-flash`` as two
checkpoints of the one provider. The local slot is named ``laya`` and is generic:
``local_model`` selects which local decision model answers, so Laya or another
pre-deterministic routing model is configuration rather than a new provider name.

Every name this repository shipped before the four-mode contract still resolves,
so an existing config entry keeps the routing it stored.
"""

from __future__ import annotations

import inspect
import ipaddress
import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
MODEL = "typesafe/jev-1.13"

LAYA_BASE_URL = "http://127.0.0.1:8000"
LAYA_ENDPOINT_PATH = "/v1/systemone"
LAYA_MODEL = "convaiinnovations/laya"
LAYA_TIMEOUT = 120.0
_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")

# The local route is a generic local decision-model slot. The provider name in
# configuration stays ``laya``; ``local_model`` is the engine or checkpoint name
# the local server is asked for, and it is interchangeable with any other model
# that publishes the same ``/v1/systemone`` contract.
LOCAL_MODEL = "laya"
# The stored config-entry field that selects the local engine. It lives beside
# the Clef checkpoint field so the form and the entry builder cannot drift.
LOCAL_MODEL_FIELD = "local_model"
# The shape a local model name may take. This is deliberately a character shape
# and not a list of model names: a local model published after this release must
# work by configuration alone, with no code change. It rejects empty and
# whitespace-only values, control characters, quotes and backslashes that would
# corrupt a JSON string, and the URL delimiters ``?``, ``#``, and ``&`` that would
# corrupt a path segment if the value is ever placed in one.
_LOCAL_MODEL_SHAPE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:+/-]*\Z")

# Clef is a Cloudflare Workers AI decision model. The account id is part of the
# run URL and is configuration; the API token is the credential. Both are read
# from the environment and neither is stored in a config entry, because a Home
# Assistant config entry is written to disk in plain text.
CLEF_API_BASE = "https://api.cloudflare.com/client/v4/accounts"
CLEF_RUN_PATH = "/ai/run/@cf/cloudflare/{model}"
CLEF_DEFAULT_MODEL = "clef"
CLEF_MODELS = ("clef", "clef-flash")
CLEF_ACCOUNT_ENV = "CLOUDFLARE_ACCOUNT_ID"
CLEF_TOKEN_ENV = "CLOUDFLARE_API_TOKEN"
CLEF_TIMEOUT = 30.0
# The stored config-entry field that selects the checkpoint. It lives here so
# the config flow and the entry builder cannot drift apart on the spelling.
CLEF_CHECKPOINT_FIELD = "clef_model"

# Clef accepts question ids of letters, digits, '_', '.', and '-', at most 100
# characters, with at most 64 questions per request. This repository builds ids
# of the form "candidate:id" and "hook:name", and ':' is not permitted, so the
# id mapping is load bearing and the answer is mapped back to the caller's own
# names.
CLEF_ID_ALLOWED = re.compile(r"\A[A-Za-z0-9_.-]+\Z")
CLEF_ID_MAX_LENGTH = 100
CLEF_MAX_QUESTIONS = 64

# A failed attempt falls through to the next provider in a chain only when the
# provider was unreachable, refused, rate limited, or broken. Any other status,
# such as 400 or 422, says the request itself was rejected, and the same request
# is not retried somewhere else.
FALLBACK_STATUS_CODES = frozenset({401, 403, 429})

# A repeated local outage must not quietly turn every household case into remote
# traffic. Three consecutive local failures that qualified for the hosted
# fallback still fall back; the fourth, and every one after it, is suppressed and
# the local error is re-raised instead of being answered remotely.
#
# The breaker is keyed by the local hop it belongs to rather than held once for
# the process. It was a process global, so two config entries shared it: while
# the household had two entries and one local server was dead, the healthy entry
# could be denied its hosted fallback until Home Assistant restarted, even though
# its own local server was answering. One entry's outage must not deny another
# entry the fallback it is configured for.
#
# The counter also decays. After ``LOCAL_FALLBACK_COOLDOWN_SECONDS`` with no new
# failure the count for that hop returns to zero, so a local server that comes
# back is retried instead of staying suppressed for the life of the Home
# Assistant process. A successful local call also resets it immediately.
#
# The default scope is the empty string, which is what a caller that builds one
# provider and never names it gets, and which behaves exactly as one shared
# breaker did.
LOCAL_FALLBACK_FAILURE_LIMIT = 3
LOCAL_FALLBACK_COOLDOWN_SECONDS = 300.0
_local_failure_lock = threading.Lock()
_local_failures: dict[str, list[float]] = {}


def _decay_locked(scope: str, now: float) -> None:
    """Reset one breaker if the cooldown has passed. Caller holds the lock."""
    stamps = _local_failures.get(scope)
    if stamps and now - stamps[-1] >= LOCAL_FALLBACK_COOLDOWN_SECONDS:
        _local_failures.pop(scope, None)


def local_failure_count(scope: str | None = None) -> int:
    """The consecutive local failures recorded for one local hop.

    Zero once the cooldown has elapsed, so a caller reading the count sees the
    same thing the breaker will act on.

    A caller that names a scope gets that hop's count, which is what the breaker
    compares against the limit. A caller that names none gets the total across
    every live hop, which is a diagnostic for a process-level question ("is
    anything local failing right now?") and is deliberately not the number that
    actuates anything.
    """
    now = time.monotonic()
    with _local_failure_lock:
        if scope is None:
            for live in list(_local_failures):
                _decay_locked(live, now)
            return sum(len(stamps) for stamps in _local_failures.values())
        _decay_locked(scope, now)
        return len(_local_failures.get(scope, ()))


def reset_local_failures(scope: str | None = None) -> None:
    """Clear one local failure counter after a successful local call.

    A caller that names a scope clears that hop, which is what a successful local
    call does. A caller that names none clears every hop, which is what clearing
    the one process-global counter used to mean and what a restart means.
    """
    with _local_failure_lock:
        if scope is None:
            _local_failures.clear()
        else:
            _local_failures.pop(scope, None)


def note_local_failure(exc: BaseException, scope: str = "") -> bool:
    """Record one failed local attempt and say whether the hosted hop may answer.

    Only the exception class name is logged. The case, the entity state, and the
    answer never reach a log record. ``True`` means the count is still at or
    below ``LOCAL_FALLBACK_FAILURE_LIMIT``, so the hosted fallback may be
    attempted. ``False`` means the breaker is tripped and the caller must
    re-raise the local error rather than answer it remotely.

    The log line states which of those two happened. It used to say
    "considering the hosted fallback" unconditionally, before the count was
    compared against the limit, so a suppressed call logged that it was about to
    fall back and then did not.
    """
    now = time.monotonic()
    with _local_failure_lock:
        _decay_locked(scope, now)
        stamps = _local_failures.setdefault(scope, [])
        stamps.append(now)
        # The list is a cooldown book, not a history: a failure older than the
        # cooldown belongs to an outage that has already been forgotten, and it
        # must not push a live hop over the limit.
        cutoff = now - LOCAL_FALLBACK_COOLDOWN_SECONDS
        while len(stamps) > 1 and stamps[0] < cutoff:
            stamps.pop(0)
        count = len(stamps)
        may_fallback = count <= LOCAL_FALLBACK_FAILURE_LIMIT
    if may_fallback:
        logger.warning(
            "local %s decision failed (%s); using the hosted fallback "
            "(local failure %d of %d)",
            LOCAL_PROVIDER,
            type(exc).__name__,
            count,
            LOCAL_FALLBACK_FAILURE_LIMIT,
        )
    else:
        logger.warning(
            "local %s decision failed (%s); suppressing the hosted fallback, "
            "local failure %d exceeds the limit of %d. The local error is "
            "raised instead. The breaker resets after %d s without a new "
            "failure, or as soon as a local call succeeds.",
            LOCAL_PROVIDER,
            type(exc).__name__,
            count,
            LOCAL_FALLBACK_FAILURE_LIMIT,
            int(LOCAL_FALLBACK_COOLDOWN_SECONDS),
        )
    return may_fallback


# The hosted providers. They are not renamed by the four-mode contract: the mode
# names the API as one side, and which hosted model answers it stays its own
# individually selectable choice, exactly as it was before.
HOSTED_PROVIDERS = ("openrouter", "clef")
# The local slot. The provider name stays ``laya``; ``local_model`` selects the
# engine behind it, so a different local model needs no new provider name.
LOCAL_PROVIDERS = ("laya",)
# The name of the local provider, used by the chain to tell its local hop from a
# hosted one.
LOCAL_PROVIDER = "laya"

# The four canonical mode names. They are the only names a diagnostic, a log
# line, or a config entry should ever carry: every alias resolves onto one of
# them before it reaches a chain, a URL, or an error message.
API_WITH_LOCAL_FALLBACK = "api_with_local_fallback"
API_ONLY = "api_only"
LOCAL_ONLY = "local_only"
LOCAL_WITH_API_FALLBACK = "local_with_api_fallback"
CANONICAL_MODES = (
    API_WITH_LOCAL_FALLBACK,
    API_ONLY,
    LOCAL_ONLY,
    LOCAL_WITH_API_FALLBACK,
)

# The modes that promise a fallback and therefore fail at load when the other
# side has no usable provider. Both names are carried in a failure message.
FALLBACK_MODES = (API_WITH_LOCAL_FALLBACK, LOCAL_WITH_API_FALLBACK)
# The modes that call one provider and report its failure rather than rerouting.
SINGLE_PROVIDER_MODES = (API_ONLY, LOCAL_ONLY)

PROVIDER_ENV = {
    "openrouter": "OPENROUTER_API_KEY",
    "laya": "LAYA_API_KEY",
    "clef": CLEF_TOKEN_ENV,
}
# Clef needs a credential and a configuration value, so it is configured only
# when both are present.
PROVIDER_REQUIRED_ENV = {"clef": (CLEF_TOKEN_ENV, CLEF_ACCOUNT_ENV)}
DEFAULT_FALLBACK_ORDER = ("openrouter",)

# Every name that resolves to a canonical mode. Each entry is either one of the
# four canonical names, a pre-existing arrangement name, or a pre-existing
# canonical route name. Nothing is removed, so no deployed configuration breaks.
#
# The pre-existing names are grouped by the canonical mode they denote, and the
# table is documented in docs/reference.md:
#
# - ``api_with_local_fallback``: ``clef_with_local_fallback``. This mode is new to
#   this repository: v1.3.0 had no hosted-first chain, because both of its chains
#   were local-first or hosted-only. A hosted provider reached this mode only by
#   naming the chain explicitly, so the legacy hosted provider names keep
#   resolving to ``api_only``, which is exactly what they selected before.
# - ``api_only``: ``openrouter``, ``jev_api``, ``clef``, ``clef_api``, and
#   ``auto``. Each of these named one hosted provider and reported its failure
#   without rerouting, so each keeps meaning one hosted provider and no fallback.
# - ``local_only``: ``laya``, ``laya_local``.
# - ``local_with_api_fallback``: ``laya_then_hosted``,
#   ``laya_with_jev_fallback``, ``clef_then_jev``,
#   ``clef_with_jev_fallback``.
#
# ``clef_then_jev`` is a hosted-only chain in v1.3.0: Clef first, then hosted
# Jev. It is not one of the four canonical modes, because both sides it chains are
# hosted. It is kept as an alias of ``local_with_api_fallback`` so that an entry
# storing it keeps routing the same way, Clef first and hosted Jev behind it, and
# ``_clef_pinned_mode`` keeps the Clef lead that the mode name alone cannot
# express. That is the one place this repository's vocabulary does not map
# cleanly onto the shared four-mode shape, and preserving the routing is what
# backwards compatibility requires.
#
# ``auto`` is dynamic: it resolves to ``api_only`` when no usable local model is
# configured and to ``api_with_local_fallback`` when one is, which is what
# ``resolve_auto_mode`` decides. It is mapped to ``api_only`` here so that a
# lookup of the mode constant is total, and ``PROVIDER_MODES["auto"]`` carries
# the same fallback.
MODE_ALIASES = {
    # hosted API leading, local slot behind it
    API_WITH_LOCAL_FALLBACK: API_WITH_LOCAL_FALLBACK,
    "clef_with_local_fallback": API_WITH_LOCAL_FALLBACK,
    # ``auto`` is dynamic rather than a fixed alias: see ``resolve_auto_mode``.
    "auto": API_ONLY,
    # the hosted API alone. Each name pins one hosted provider, which is how
    # each of them behaved before the four-mode contract: a single provider
    # whose failure is reported and never rerouted.
    API_ONLY: API_ONLY,
    "openrouter": API_ONLY,
    "jev_api": API_ONLY,
    "clef": API_ONLY,
    "clef_api": API_ONLY,
    # the local slot alone
    LOCAL_ONLY: LOCAL_ONLY,
    "laya": LOCAL_ONLY,
    "laya_local": LOCAL_ONLY,
    # the local slot leading, the hosted API behind it
    LOCAL_WITH_API_FALLBACK: LOCAL_WITH_API_FALLBACK,
    "laya_then_hosted": LOCAL_WITH_API_FALLBACK,
    "laya_with_jev_fallback": LOCAL_WITH_API_FALLBACK,
    "clef_then_jev": LOCAL_WITH_API_FALLBACK,
    "clef_with_jev_fallback": LOCAL_WITH_API_FALLBACK,
}
MODE_NAMES = CANONICAL_MODES

# The canonical mode for a name. Every alias above is listed here too, so the
# reverse lookup is total and neither table can name a mode the other does not.
PROVIDER_MODES = {
    API_WITH_LOCAL_FALLBACK: API_WITH_LOCAL_FALLBACK,
    API_ONLY: API_ONLY,
    LOCAL_ONLY: LOCAL_ONLY,
    LOCAL_WITH_API_FALLBACK: LOCAL_WITH_API_FALLBACK,
    "clef_with_local_fallback": API_WITH_LOCAL_FALLBACK,
    "auto": API_ONLY,
    "openrouter": API_ONLY,
    "jev_api": API_ONLY,
    "clef": API_ONLY,
    "clef_api": API_ONLY,
    "laya": LOCAL_ONLY,
    "laya_local": LOCAL_ONLY,
    "laya_then_hosted": LOCAL_WITH_API_FALLBACK,
    "laya_with_jev_fallback": LOCAL_WITH_API_FALLBACK,
    "clef_then_jev": LOCAL_WITH_API_FALLBACK,
    "clef_with_jev_fallback": LOCAL_WITH_API_FALLBACK,
}

# The mode names this repository shipped before the four-mode contract, kept as
# names so a caller that imports them still works. ``CHAINED_PROVIDER`` was the
# local-first chain and ``CLEF_CHAINED_PROVIDER`` the Clef-first chain; both now
# denote ``local_with_api_fallback``.
CHAINED_PROVIDER = "laya_then_hosted"
CLEF_CHAINED_PROVIDER = "clef_then_jev"
# ``clef`` is both a hosted provider name and the Clef checkpoint provider, as
# it was in v1.3.0. The two are the same string here.
CLEF_CHECKPOINT_PROVIDER = "clef"
# Every accepted name, canonical mode or alias. An unknown name is refused with
# a message that names every mode and alias the caller may use.
PROVIDER_NAMES = tuple(MODE_ALIASES)


def accepted_provider_names() -> str:
    """Every accepted name, for an error message that must not hide them."""
    return ", ".join(sorted(PROVIDER_NAMES))


def resolve_provider(name: object) -> str:
    """Map a mode name or a legacy route name onto one of the four modes.

    Every name in ``MODE_ALIASES`` is rewritten onto the canonical mode it
    denotes, so no alias string reaches a chain, a diagnostic, a log line, or a
    URL. Any other value is returned unchanged so the existing validation still
    decides whether it is valid.

    Case handling is unchanged from v1.3.0: the arrangement names and the four
    canonical mode names are recognised in either case, and the canonical route
    names are matched exactly as they were, so a value like ``Laya`` is still
    refused rather than newly accepted.
    """
    if not isinstance(name, str):
        return ""
    candidate = name.strip()
    if candidate in MODE_ALIASES:
        return MODE_ALIASES[candidate]
    folded = candidate.lower()
    if folded in CASE_INSENSITIVE_ALIASES:
        return MODE_ALIASES[folded]
    return candidate


def provider_mode(provider: str) -> str:
    """The canonical mode for a name, accepted as either a mode or an alias."""
    canonical = resolve_provider(provider)
    if canonical not in PROVIDER_MODES:
        raise ValueError("invalid provider: " + accepted_provider_names())
    return PROVIDER_MODES[canonical]


# The credential words a mapping key is matched on, and the key matcher itself.
# This copy lives here on purpose and is not imported from the repository's
# `sentinel` package: a HACS install copies only `custom_components/jev_sentinel`,
# because `hacs.json` sets `content_in_root: false`. An import of `sentinel.` from
# this file therefore fails at module load on every HACS install, and the
# integration cannot load at all. Commit 3ff0926 introduced that import and the
# install broke with it. `tests/test_hacs_install.py` proves the whole component
# imports with the repository root off `sys.path`, and
# `tests/test_safety_boundaries.py` keeps the two copies in agreement.
_SECRET_WORDS = ("token", "password", "secret", "apikey", "credential", "authorization")

# Segments, not substrings. `private_key` yields the segment `key` while
# `door_pin` yields `pin`, and a door's PIN code is household data this policy
# is allowed to see. Substring matching cannot tell the two apart, because
# `doorpin` contains `pin` exactly as `privatekey` contains `key`.
_SECRET_SEGMENTS = frozenset(
    {
        "key",
        "keys",
        "passwd",
        "passphrase",
        "authorisation",
        "authorisations",
        "bearer",
        "cookie",
        "clientid",
        "sessionid",
        "otp",
        "signature",
        "privatekey",
        "accesskey",
        "secretkey",
        "apikey",
        "authtoken",
        "refreshtoken",
    }
)


def _key_is_secret(key: Any) -> bool:
    """True when a mapping key names a credential.

    Separators and case are normalised first, so `api-key`, `apiKey`, `api key`
    and `API_KEY` are one word. Matching the raw lowercase key against `api_key`
    let the other three spellings through and sent those values to the provider
    in cleartext.

    `Mapping` rather than `dict` is the type checked, so a mapping that is not a
    dict is redacted on this side too and the two copies cannot drift on it.

    The key is split into segments as well as flattened. Flattening alone missed
    `private_key`, `access_key`, `key`, `passwd`, `passphrase`, `authorisation`,
    `cookie`, `clientid` and `bearer` -- nine spellings that carried their values
    onto the wire and onto the event bus in cleartext.
    """
    normalized = str(key).replace("-", "").replace("_", "").replace(" ", "").lower()
    if any(word in normalized for word in _SECRET_WORDS):
        return True
    segments = [s for s in re.split(r"[^A-Za-z0-9]+", str(key).lower()) if s]
    return any(segment in _SECRET_SEGMENTS for segment in segments)


_SECRET_TEXT = re.compile(
    r"(?i)(api[_ -]?key|token|passwd|passphrase|password|secret|credential"
    r"|authorisation?|bearer|cookie)([\"']?\s*[:=]\s*[\"']?)[^\s,;]+"
)

# `Authorization: Bearer sk-live-...` and the bare form. Applied before
# _SECRET_TEXT, whose wider value match would otherwise consume the word
# `Bearer` and leave the token itself behind it.
_BEARER_TEXT = re.compile(r"(?i)\b(bearer)(\s+)[A-Za-z0-9._~+/=-]+")

# The confidence question is a ``score`` question, and a score rubric is an
# ordered list of level descriptions with index 0 first. A local Laya server
# rejects ``{"min": 0, "max": 1}`` with "a score question takes 'criteria' as a
# list of level descriptions, index 0 first".
CONFIDENCE_LEVELS = (
    "very low confidence, the case is ambiguous or mostly missing",
    "low confidence",
    "moderate confidence",
    "high confidence",
    "very high confidence, the case points one way",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Case:
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
            f"case_{uuid.uuid4().hex[:12]}",
            event_type,
            area,
            tuple(entities),
            dict(facts or {}),
            requested_action,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Decision:
    outcome: str
    reason: str
    confidence: float | None = None
    action: str | None = None
    shadow: bool = True
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def decision_questions() -> dict[str, dict]:
    """The rubric both routes send. A fresh mapping on every call."""
    return {
        "outcome": {
            "type": "choice",
            "instructions": "Choose the safest next outcome for this Home Assistant case.",
            "criteria": {
                "ignore": "No meaningful action",
                "notify": "Tell the user",
                "ask_user": "Need user approval or clarification",
                "recommend": "Recommend a safe allowlisted action",
                "escalate": "Treat as unresolved or risky",
            },
        },
        "action": {
            "type": "choice",
            "instructions": "Choose one action only if it is supported by the case and policy.",
            "criteria": {
                "notify": "Notify the user",
                "ask_user": "Ask the user",
                "light.turn_off": "Turn off a light",
                "switch.turn_off": "Turn off a switch",
                "climate.set_temperature": "Set a climate temperature",
                "none": "No device action",
            },
        },
        "confidence": {
            "type": "score",
            "instructions": "Score confidence in the selected outcome, from very low to very high.",
            "criteria": list(CONFIDENCE_LEVELS),
        },
    }


def _legend_count(answer: dict, levels: tuple[str, ...]) -> int:
    """The number of levels a score answer's index scale has."""
    legend = answer.get("legend")
    return len(legend) if isinstance(legend, (dict, list)) else len(levels)


def score_index(answer: object, *, levels: tuple[str, ...] = CONFIDENCE_LEVELS):
    """The expected level index a score answer reports, or ``None``."""
    if not isinstance(answer, dict):
        return None
    score = answer.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return None
    return float(score)


def validate_score_answer(
    answer: object, *, levels: tuple[str, ...] = CONFIDENCE_LEVELS
) -> None:
    """Reject a score answer whose index is not on its own legend scale.

    A ``score`` answer is a position on the index scale ``0`` to
    ``len(legend) - 1``. An index outside that range, or an answer with no
    numeric index at all, is a provider answer this repository does not accept,
    so it raises instead of being silently clamped the way
    ``confidence_from_score`` clamps for a caller that merely wants a number.
    """
    if not isinstance(answer, dict):
        raise ValueError("invalid score answer: no numeric score index")
    score = score_index(answer, levels=levels)
    if score is None:
        raise ValueError("invalid score answer: no numeric score index")
    count = _legend_count(answer, levels)
    if count < 2:
        raise ValueError("invalid score answer: the legend needs at least two levels")
    if not 0 <= score <= count - 1:
        raise ValueError(
            f"invalid score answer: index {score:g} is outside the"
            f" 0 to {count - 1} legend scale"
        )


def validate_answers(
    answers: object, questions: dict[str, dict], *, label: str
) -> dict[str, Any]:
    """Check a Decisions shaped answer against the rubric that asked for it.

    The one typed answer validation every route shares, so an unknown choice or
    an out-of-range score index is refused the same way whatever answered.
    """
    if not isinstance(answers, dict):
        raise ValueError(f"invalid {label} response: answers must be a mapping")
    for name, question in questions.items():
        answer = answers.get(name)
        if not isinstance(answer, dict):
            raise ValueError(f"invalid {label} response: missing answer for {name!r}")
        kind = question.get("type")
        if kind == "choice":
            if answer.get("choice") not in question.get("criteria", {}):
                raise ValueError(
                    f"invalid {label} response: unknown choice for {name!r}"
                )
        elif kind == "score":
            try:
                validate_score_answer(answer)
            except ValueError as exc:
                raise ValueError(f"invalid {label} response: {exc}") from None
        elif kind == "noul":
            probability = answer.get("noul")
            if (
                isinstance(probability, bool)
                or not isinstance(probability, (int, float))
                or not 0 <= probability <= 1
            ):
                raise ValueError(
                    f"invalid {label} response: invalid probability for {name!r}"
                )
    return answers


def confidence_from_score(
    answer: object, *, levels: tuple[str, ...] = CONFIDENCE_LEVELS
) -> float | None:
    """Map a score answer's expected level index onto 0 to 1.

    A ``score`` answer reports the expected level on the legend index scale,
    ``0`` to ``len(legend) - 1``, next to the ``legend`` naming each level. That
    index is rescaled onto 0 to 1 by dividing by ``len(legend) - 1``, so level 0
    is 0.0 and the last level is 1.0. The result is a quantized ordinal
    estimate, not a calibrated probability: adjacent levels sit
    ``1 / (levels - 1)`` apart. The raw index stays recoverable by multiplying
    the returned value by ``levels - 1``.

    A missing, non numeric, or single level answer returns ``None``.
    """
    if not isinstance(answer, dict):
        return None
    score = score_index(answer, levels=levels)
    if score is None:
        return None
    count = _legend_count(answer, levels)
    if count < 2:
        return None
    return min(1.0, max(0.0, score / (count - 1)))


def decision_from_body(body: dict, *, model: str = MODEL) -> Decision:
    """Build the typed ``Decision`` from a Decisions shaped response body."""
    answers = body["answers"]
    action = answers["action"]["choice"]
    return Decision(
        answers["outcome"]["choice"],
        answers["outcome"]["choice"].replace("_", " "),
        confidence_from_score(answers.get("confidence")),
        None if action == "none" else action,
        True,
        {"model": body.get("model", model)},
    )


def local_checkpoint(model: object = None) -> str:
    """The local engine or checkpoint name to ask the local server for.

    ``laya`` is the default and the existing default path is unchanged, so an
    entry that stored nothing keeps calling Laya. Any other name is accepted,
    which is the point: the local slot is interchangeable with any other local
    System One decision model that publishes the same ``/v1/systemone``
    contract, and a new one must work by configuration alone rather than by a
    code change. So there is no allowlist of model names here. What is
    rejected is a value that cannot be a name at all: empty or whitespace-only
    text, a value that is not text, and a value carrying a control character, a
    quote, a backslash, whitespace, or the URL delimiters ``?``, ``#``, and
    ``&``, any of which would corrupt the JSON body or a URL path segment.
    """
    if model is None:
        return LOCAL_MODEL
    if not isinstance(model, str):
        raise ValueError("invalid local_model: expected a model name")
    candidate = model.strip()
    if not candidate:
        raise ValueError("invalid local_model: the name is empty")
    if not _LOCAL_MODEL_SHAPE.match(candidate):
        raise ValueError(
            "invalid local_model: expected a model name built from letters,"
            " digits, '.', '_', ':', '+', '-', and '/', starting with a letter"
            " or a digit"
        )
    return candidate


class OpenRouterJev:
    """Hosted Jev over an OpenRouter API key."""

    def __init__(self, api_key: str | None = None, *, timeout: float = 30.0) -> None:
        # An explicitly supplied value wins over the environment, including a
        # value that is present but empty: an empty credential is a missing
        # credential, so it is never topped up from the process environment.
        # ``None`` means "read the environment", which is what a caller with no
        # value of its own passes.
        self.api_key = (
            api_key if api_key is not None else os.environ.get("OPENROUTER_API_KEY")
        )
        self.timeout = timeout

    def decide(self, state: dict[str, Any]) -> Decision:
        if not self.api_key or self.api_key == "***":
            raise RuntimeError(
                "Configure an OpenRouter API key before requesting a Jev review"
            )
        payload = {"model": MODEL, "state": state, "questions": decision_questions()}
        request = Request(
            ENDPOINT,
            data=json.dumps(payload).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "X-Title": "Jev Home Assistant Sentinel",
            },
        )
        with urlopen(request, timeout=self.timeout) as response:
            body = json.loads(response.read().decode())
        validate_answers(body.get("answers", {}), decision_questions(), label=MODEL)
        return decision_from_body(body, model=MODEL)


def clef_checkpoint(model: str | None = None) -> str:
    """The Clef checkpoint to call, validated.

    Clef Flash is the smaller checkpoint Cloudflare documents for latency bound
    paths. Both answer the same typed questions, so it is a checkpoint of one
    provider and not a provider of its own. An unset or blank name means the
    default; any other unknown name raises.
    """
    candidate = (model or "").strip().lower() or CLEF_DEFAULT_MODEL
    if candidate not in CLEF_MODELS:
        raise ValueError("invalid Clef checkpoint: " + ", ".join(CLEF_MODELS))
    return candidate


def _require_clef_credentials(
    account: str | None, token: str | None, env: Mapping[str, str]
) -> tuple[str, str]:
    """The account id and token to use, and the name of anything missing.

    Both are checked before any socket work. A constructor value wins over the
    environment, including a value that is present but empty: an empty
    credential is a missing credential, so it must never be topped up from the
    process environment.
    """
    resolved_account = (
        account if account is not None else (env.get(CLEF_ACCOUNT_ENV) or "")
    ).strip()
    resolved_token = (
        token if token is not None else (env.get(CLEF_TOKEN_ENV) or "")
    ).strip()
    missing = [
        name
        for name, value in (
            (CLEF_TOKEN_ENV, resolved_token),
            (CLEF_ACCOUNT_ENV, resolved_account),
        )
        if not value
    ]
    if len(missing) == 1:
        raise RuntimeError(f"{missing[0]} is required for live Clef decisions")
    if missing:
        raise RuntimeError(
            " and ".join(missing) + " are required for live Clef decisions"
        )
    return resolved_account, resolved_token


def clef_credentials(env: Mapping[str, str] | None = None) -> tuple[str, str]:
    """The Cloudflare account id and API token from the environment.

    The account id is configuration and the token is the credential, but both
    are required before a request is made, and the message names the variable
    that is missing rather than anything about the value.
    """
    return _require_clef_credentials(None, None, os.environ if env is None else env)


def clef_endpoint(account: str, model: str = CLEF_DEFAULT_MODEL) -> str:
    """The Workers AI run URL for one account and checkpoint.

    The account id is percent encoded, because it is user supplied text placed
    in a URL path.
    """
    if not account.strip():
        raise ValueError("invalid Clef account id")
    checkpoint = clef_checkpoint(model)
    return (
        CLEF_API_BASE
        + "/"
        + quote(account.strip(), safe="")
        + CLEF_RUN_PATH.format(model=checkpoint)
    )


def clef_question_ids(
    questions: dict[str, dict],
) -> tuple[dict[str, dict], dict[str, str]]:
    """Rename question ids into Clef's id alphabet and return the way back.

    An id already in the alphabet passes through untouched, a rewritten id that
    would collide is suffixed until it does not, and the returned
    ``safe -> original`` map restores the caller's own names.
    """
    if len(questions) > CLEF_MAX_QUESTIONS:
        raise ValueError(
            f"invalid Clef request: {len(questions)} questions exceeds the"
            f" limit of {CLEF_MAX_QUESTIONS}"
        )
    safe_questions: dict[str, dict] = {}
    restore: dict[str, str] = {}

    def _unique(base: str) -> str:
        candidate = base
        suffix = 2
        while candidate in restore:
            tail = f"-{suffix}"
            room = CLEF_ID_MAX_LENGTH - len(tail)
            candidate = base[: max(1, room)] + tail
            suffix += 1
        return candidate

    for name, question in questions.items():
        base = "".join(
            char if CLEF_ID_ALLOWED.match(char) else "_" for char in str(name)
        ).strip("_")
        if not base:
            base = "question"
        if len(base) > CLEF_ID_MAX_LENGTH:
            base = base[:CLEF_ID_MAX_LENGTH].strip("_") or "question"
        safe = base if CLEF_ID_ALLOWED.match(base) else _unique(base)
        if safe in restore:
            safe = _unique(base)
        restore[safe] = str(name)
        safe_questions[safe] = question
    return safe_questions, restore


def clef_answers(
    body: object,
    questions: dict[str, dict],
    *,
    restore: dict[str, str] | None = None,
    model: str = CLEF_DEFAULT_MODEL,
    label: str = "Cloudflare Clef",
) -> dict[str, Any]:
    """Unwrap a Clef reply, restore the caller's question ids, and validate it.

    Cloudflare serves Clef from a REST endpoint that answers with the model
    output directly, while its general API surface wraps results in a
    ``success``/``result`` envelope. Both are accepted and a top level
    ``answers`` mapping wins over ``result.answers``. A ``success: false``
    envelope carries Cloudflare's own error codes, which are surfaced rather
    than swallowed.
    """
    if not isinstance(body, dict):
        raise RuntimeError(f"{label} returned an invalid response")
    if body.get("success") is False:
        codes = [
            str(error.get("code"))
            for error in (body.get("errors") or [])
            if isinstance(error, dict) and error.get("code") is not None
        ]
        detail = ", ".join(codes) if codes else "unknown error"
        raise RuntimeError(f"{label} request failed with Cloudflare code {detail}")
    answers = body.get("answers")
    if not isinstance(answers, dict):
        inner = body.get("result")
        answers = inner.get("answers") if isinstance(inner, dict) else None
    if not isinstance(answers, dict):
        raise RuntimeError(f"{label} returned an invalid response")
    if restore:
        restored: dict[str, Any] = {}
        for key, answer in answers.items():
            safe_id = str(key)
            restored[restore.get(safe_id, safe_id)] = answer
        answers = restored
    validate_answers(answers, questions, label=label)
    return {"model": body.get("model", model), "answers": answers}


def _cloudflare_error_detail(exc: HTTPError, *, limit: int = 2000) -> str:
    """Cloudflare's own error codes from an HTTP error body, if it carries any.

    Only the ``errors`` codes are used. No other part of a Cloudflare error body
    reaches a log record or an exception message.
    """
    try:
        raw = exc.read(limit)
    except Exception:  # a body that cannot be read is not an error here
        return ""
    try:
        body = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    codes = [
        str(error.get("code"))
        for error in (body.get("errors") or [])
        if isinstance(error, dict) and error.get("code") is not None
    ]
    return ", ".join(codes)


class ClefJev:
    """Cloudflare Clef over a Workers AI account and API token.

    Clef is not a Jev endpoint. It is a Cloudflare hosted decision model that
    answers the same typed questions in the same ``answers`` shape, so selecting
    it joins the hosted routes and can sit in a fallback order, instead of
    replacing one the way the local route does.

    Two checkpoints of one model ship: ``clef`` and ``clef-flash``. The
    checkpoint selects the run path and the model string, and never the shape of
    the answer.

    The account id and the API token are read from ``CLOUDFLARE_ACCOUNT_ID`` and
    ``CLOUDFLARE_API_TOKEN`` and are checked before any socket work. On failure
    only the exception class name is logged: the reviewed case, the entity
    state, the account, and the token never reach a log record.
    """

    def __init__(
        self,
        api_token: str | None = None,
        *,
        account_id: str | None = None,
        model: str = CLEF_DEFAULT_MODEL,
        timeout: float = CLEF_TIMEOUT,
        env: Mapping[str, str] | None = None,
    ) -> None:
        # The source this adapter resolves against, remembered so a later
        # decide() consults the same environment it was built from rather than
        # the process environment, which may hold unrelated credentials.
        self.env: Mapping[str, str] = os.environ if env is None else env
        self.api_token = (
            api_token
            if api_token is not None
            else (self.env.get(CLEF_TOKEN_ENV) or "").strip()
        )
        self.account_id = (
            account_id
            if account_id is not None
            else (self.env.get(CLEF_ACCOUNT_ENV) or "").strip()
        )
        self.model = clef_checkpoint(model)
        self.timeout = timeout

    def _credentials(self, env: Mapping[str, str] | None = None) -> tuple[str, str]:
        return _require_clef_credentials(
            self.account_id or None,
            self.api_token or None,
            self.env if env is None else env,
        )

    def decide(self, state: dict[str, Any]) -> Decision:
        account, token = self._credentials()
        questions = decision_questions()
        safe_questions, restore = clef_question_ids(questions)
        payload = {
            "model": self.model,
            "state": state,
            "questions": safe_questions,
        }
        request = Request(
            clef_endpoint(account, self.model),
            data=json.dumps(payload).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode())
        except HTTPError as exc:
            # Cloudflare's error codes are more useful than the bare status, so
            # a coded body is folded into the message while the type and status
            # are preserved: a chain must still recognise this as a refusal or a
            # provider failure and fall through to its next hop.
            detail = _cloudflare_error_detail(exc)
            if detail:
                logger.warning("clef decision failed (%s)", type(exc).__name__)
                raise HTTPError(
                    exc.url,
                    exc.code,
                    f"HTTP {exc.code} (Cloudflare code {detail})",
                    exc.headers,
                    exc.fp,
                ) from exc
            logger.warning("clef decision failed (%s)", type(exc).__name__)
            raise
        except Exception as exc:
            # Only the exception category is logged. The case, the account, and
            # the token stay out of the log record.
            logger.warning("clef decision failed (%s)", type(exc).__name__)
            raise
        try:
            normalised = clef_answers(
                body, questions, restore=restore, model=self.model
            )
        except Exception as exc:
            logger.warning("clef decision failed (%s)", type(exc).__name__)
            raise
        return decision_from_body(normalised, model=self.model)


def laya_endpoint(
    base_url: str = LAYA_BASE_URL, endpoint_path: str = LAYA_ENDPOINT_PATH
) -> str:
    """Resolve a local Laya server URL.

    The local slot is a slot *on this machine* by default: a host that is not
    loopback is refused unless it is a private or link-local address, because
    ``https://evil.example.com`` was accepted here unchanged and every
    redacted-but-still-descriptive case — area, entity ids,
    ``window_open_minutes``, ``heating_state`` — would then be posted to an
    arbitrary remote host. A private address stays allowed over HTTPS for the
    household that runs its local server on another box on its own network; a
    public one does not, because nothing in this form gives that host a reason
    to know about the household's windows.

    A ``base_url`` that already carries a path is refused rather than joined to
    it: ``http://127.0.0.1:8000/v1`` produced
    ``http://127.0.0.1:8000/v1/v1/systemone``, which no server answers and which
    reads in the log as a working endpoint.
    """
    if not endpoint_path.startswith("/"):
        raise ValueError("invalid Laya endpoint path")
    parsed = urlsplit(str(base_url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("invalid Laya base URL")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError(
            "invalid Laya base URL: "
            + str(base_url)
            + " carries a path, which is doubled onto "
            + LAYA_ENDPOINT_PATH
            + ". Set the server root and let the endpoint path be appended."
        )
    if parsed.username or parsed.password:
        # The endpoint this returns is the string a request is made against and
        # the string that appears in a log line, so a credential placed inside
        # the URL would be carried into both. A local server that needs a bearer
        # token takes it from ``LAYA_API_KEY`` instead.
        raise ValueError(
            "invalid Laya base URL: "
            + str(base_url)
            + " carries embedded credentials, which the endpoint and every log"
            " line built from it would then repeat. Set LAYA_API_KEY instead."
        )
    host = parsed.hostname
    if host not in _LOOPBACK_HOSTS and not _is_private_host(host):
        raise ValueError(
            "a remote Laya host is refused: "
            + str(base_url)
            + " is neither loopback nor a private address. The local slot holds"
            " household case data, which this policy will not post to a host it"
            " cannot place on the household's own network."
        )
    if parsed.scheme == "http" and host not in _LOOPBACK_HOSTS:
        raise ValueError("nonlocal Laya server requires HTTPS")
    return base_url.rstrip("/") + endpoint_path


def _is_private_host(host: str) -> bool:
    """Whether a host is a private or link-local address of the household."""
    candidate = host.strip("[]")
    if candidate.endswith(".local"):
        # An mDNS name resolves on the local network and nowhere else.
        return True
    try:
        packed = ipaddress.ip_address(candidate)
    except ValueError:
        return False
    return packed.is_private or packed.is_loopback or packed.is_link_local


class LayaJev:
    """A local decision-model server, over the same Decisions contract.

    The local slot is generic. It is not a hosted Jev endpoint, and it is not
    bound to one model either: it publishes ``POST /v1/systemone`` and answers
    in the same ``answers`` shape, so Laya or another pre-deterministic routing
    model is selected by the ``model`` argument, which is the value
    ``local_model`` supplies, rather than by a different provider name. The route
    is keyless: the ``Authorization`` header is omitted entirely unless the
    server was started with its own bearer check.
    """

    def __init__(
        self,
        base_url: str = LAYA_BASE_URL,
        *,
        endpoint_path: str = LAYA_ENDPOINT_PATH,
        model: str | None = LAYA_MODEL,
        api_key: str | None = None,
        timeout: float = LAYA_TIMEOUT,
    ) -> None:
        self.endpoint = laya_endpoint(base_url, endpoint_path)
        self.model = local_checkpoint(model)
        self.api_key = (
            api_key if api_key is not None else os.environ.get("LAYA_API_KEY")
        )
        self.timeout = timeout

    def decide(self, state: dict[str, Any]) -> Decision:
        payload = {
            "model": self.model,
            "state": state,
            "questions": decision_questions(),
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(
            self.endpoint,
            data=json.dumps(payload).encode(),
            method="POST",
            headers=headers,
        )
        with urlopen(request, timeout=self.timeout) as response:
            body = json.loads(response.read().decode())
        # Same rubric check the hosted route uses. Without it an off-rubric or
        # malformed answer is accepted at whatever confidence it claims, and
        # `confidence_from_score` silently clamps an out-of-range score instead
        # of refusing it.
        validate_answers(
            body.get("answers", {}), decision_questions(), label=self.model
        )
        decision = decision_from_body(body, model=self.model)
        # A successful local call clears the consecutive-failure count, on the
        # local-only route and on the local hop of the chained route alike.
        reset_local_failures(self.endpoint)
        return decision


def _local_scope(provider: Any) -> str:
    """The breaker scope of one local hop: the endpoint it was pointed at.

    Two config entries usually point at two different local servers, and a
    dead server takes its own entry's breaker down with it, not the others'. The
    endpoint is the identity that separates them, so it is the scope. A provider
    with no endpoint is a caller that never named one and keeps the shared
    default scope, which is what the process-global counter used to be.
    """
    return str(getattr(provider, "endpoint", "") or "")


def is_fallback_trigger(exc: BaseException) -> bool:
    """Whether a failed attempt should fall through to the next provider.

    A transport error or timeout means the provider was not reachable. An HTTP
    401, 403, or 429 means the request was refused or rate limited, and an HTTP
    5xx means the provider failed. Each of those says nothing about the request
    itself, so the next provider may still answer it. Any other failure
    propagates: a request the provider rejected as invalid is not retried
    somewhere else.
    """
    if isinstance(exc, HTTPError):
        return exc.code in FALLBACK_STATUS_CODES or 500 <= exc.code <= 599
    return isinstance(exc, (URLError, TimeoutError, OSError))


class ChainedJev:
    """An ordered chain of adapters: the first one that answers decides.

    The chain holds named adapters in the order they are tried. The local Laya
    hop can sit first with a hosted hop behind it, so a case costs no provider
    request while the local server answers, and a local outage still returns a
    decision. The adapter that answered is recorded in ``Decision.raw``.
    """

    def __init__(self, providers: Sequence[tuple[str, Any]]) -> None:
        if len(providers) < 2:
            raise ValueError("a chain needs at least two providers")
        self.providers: tuple[tuple[str, Any], ...] = tuple(providers)

    @property
    def names(self) -> tuple[str, ...]:
        """The adapter names in the order they are tried."""
        return tuple(name for name, _ in self.providers)

    def decide(self, state: dict[str, Any]) -> Decision:
        for index, (name, provider) in enumerate(self.providers):
            try:
                decision = provider.decide(state)
            except Exception as exc:
                if index + 1 < len(self.providers) and is_fallback_trigger(exc):
                    if name == LOCAL_PROVIDER:
                        # A local failure that qualifies for the hosted fallback
                        # is counted. Past the limit the local error is raised
                        # instead of being answered remotely, so a local server
                        # that stays down cannot send every case to a hosted
                        # API. The breaker guards the local hop only: a hosted
                        # hop in front of another hosted provider is bounded by
                        # the order, not by a counter.
                        if note_local_failure(exc, _local_scope(provider)):
                            continue
                        logger.warning(
                            "hosted fallback suppressed after %d consecutive"
                            " local failures",
                            LOCAL_FALLBACK_FAILURE_LIMIT,
                        )
                    else:
                        # A hosted hop that was refused, rate limited, or
                        # unreachable falls through to the next hop. This is what
                        # makes a hosted-only chain such as Clef then hosted Jev
                        # a chain rather than a single provider.
                        continue
                raise
            return self._attributed(decision, index)
        raise RuntimeError("the chain has no provider left to call")

    def _attributed(self, decision: Decision, index: int) -> Decision:
        raw = dict(decision.raw)
        raw["provider"] = self.providers[index][0]
        raw["attempted"] = list(self.names[: index + 1])
        return replace(decision, raw=raw)


def _configured(name: str, env: Mapping[str, str]) -> bool:
    """Whether a provider has every variable it needs, in this environment."""
    required = PROVIDER_REQUIRED_ENV.get(name, (PROVIDER_ENV[name],))
    return all((env.get(var) or "").strip() for var in required)


def _missing_names(name: str) -> str:
    """The variable names a provider needs, for a failure message."""
    return " and ".join(PROVIDER_REQUIRED_ENV.get(name, (PROVIDER_ENV[name],)))


def validate_fallback_order(order: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Accept a hosted provider order, and reject every other name.

    A member of the order is a hosted provider. The local provider is not one,
    because it is a slot of its own and the local side is never a member of the
    hosted order, and neither is a mode name such as ``local_with_api_fallback``,
    which would be a chain inside a chain. ``local_model`` is rejected here too,
    so a model name can never be appended to a fallback order.
    """
    if (
        not order
        or len(set(order)) != len(order)
        or any(name not in HOSTED_PROVIDERS for name in order)
    ):
        raise ValueError("invalid fallback_order")
    return tuple(order)


def local_model_configured(local_model: object = None) -> bool:
    """Whether the caller has explicitly selected a local decision model.

    This is the ``auto`` mode's notion of "a usable local model is
    configured". The local slot's URL and model are settings rather than
    credentials, and the local server's reachability cannot be known at load
    time, so the only honest signal that an operator wants the local side in
    play is that they named a model for it. A caller who passes nothing keeps
    the v1.3.0 behaviour of ``auto``, which never put the local slot in the
    order on its own initiative.
    """
    return local_model is not None


def resolve_auto_mode(
    *, env: Mapping[str, str] | None = None, local_model: object = None
) -> str:
    """The canonical mode ``auto`` denotes in this configuration.

    ``auto`` resolves to ``api_with_local_fallback`` when a local model has
    been explicitly configured, and to ``api_only`` otherwise. That preserves
    what ``auto`` meant in v1.3.0, which was "the hosted providers that are
    configured", while giving the local side a place behind the API once an
    operator asks for it by name.
    """
    return API_WITH_LOCAL_FALLBACK if local_model_configured(local_model) else API_ONLY


# The names that pin the hosted side to Clef rather than letting the hosted
# order choose. ``clef_with_local_fallback`` is the Clef-pinned form of the
# hosted-first mode, and ``clef_then_jev`` and ``clef_with_jev_fallback`` are the
# two spellings of the Clef-first chain, which the mode name alone cannot
# express because both of its hops are hosted.
CLEF_PINNED_NAMES = (
    "clef",
    "clef_api",
    "clef_with_local_fallback",
    "clef_then_jev",
    "clef_with_jev_fallback",
)

# The names recognised in either case. In v1.3.0 this was the set of
# arrangement names, and it is that same set plus the four canonical mode names.
# The canonical route names are deliberately not in it: they were matched
# exactly before this change and stay matched exactly, so `provider_order("Laya")`
# still raises rather than silently becoming the local mode.
CASE_INSENSITIVE_ALIASES = frozenset(
    set(CANONICAL_MODES)
    | {
        "jev_api",
        "laya_local",
        "laya_with_jev_fallback",
        "clef_api",
        "clef_with_jev_fallback",
    }
)


def pins_clef(provider: object) -> bool:
    """Whether a stored name pins the hosted side to Clef rather than the order.

    The config entry module and the config flow need this because they must
    decide which credential an entry may contribute *before* the name is
    resolved, and resolution collapses a Clef-pinned name and a Jev-pinned name
    onto one canonical mode. A mode name alone cannot express the Clef lead, so
    it has to be read off the stored spelling.
    """
    return isinstance(provider, str) and provider.strip().lower() in (CLEF_PINNED_NAMES)


def _clef_pinned_mode(provider: str) -> bool:
    """Whether the name pins the hosted side to Clef rather than the order."""
    return pins_clef(provider)


def provider_order(
    provider: str = "auto",
    *,
    fallback_order: tuple[str, ...] | list[str] = DEFAULT_FALLBACK_ORDER,
    env: dict[str, str] | None = None,
    local_model: object = None,
) -> list[str]:
    """Resolve the providers a configured mode may call, in order.

    The order is a provider sequence, not a mode name: the local slot answers
    under the single name ``laya`` whatever engine ``local_model`` selects,
    because the engine is a checkpoint of that slot rather than a provider of
    its own. An empty order is returned rather than raised, so ``build_provider``
    can turn "nothing is configured" into its own error, exactly as before.
    """
    mode = resolve_provider(provider)
    if mode not in PROVIDER_MODES:
        raise ValueError("invalid provider: " + accepted_provider_names())
    order = validate_fallback_order(fallback_order)
    if mode == LOCAL_ONLY:
        # The local slot stands in place of the hosted API, so this mode has no
        # chain to fall through and no credential to find.
        return [LOCAL_PROVIDER]
    source = os.environ if env is None else env
    configured = {name: _configured(name, source) for name in HOSTED_PROVIDERS}
    hosted = [name for name in order if configured[name]]
    if provider.strip().lower() == "auto":
        # ``auto`` builds the hosted order from the providers that are
        # configured, and adds the local slot behind them only once a local
        # model has been explicitly configured. It never raises on a missing
        # credential: an empty order is what says nothing is configured, so
        # naming a local model does not by itself make a hosted-first mode
        # usable. That preserves what ``auto`` meant before v1.4.0.
        if not hosted:
            return []
        if resolve_auto_mode(env=source, local_model=local_model) == (
            API_WITH_LOCAL_FALLBACK
        ):
            return [*hosted, LOCAL_PROVIDER]
        return hosted
    if mode == LOCAL_WITH_API_FALLBACK:
        # The local slot leads, so it needs at least one hosted hop behind it.
        # Without a configured hosted provider there is no fallback to route to,
        # and returning the local hop alone would silently turn this mode into
        # ``local_only``.
        if _clef_pinned_mode(provider):
            # A name that pins Clef first must actually call Clef, so the chain
            # is led by it rather than by the order, and something must sit
            # behind it for the same reason the local-first chain fails fast.
            if not configured[CLEF_CHECKPOINT_PROVIDER]:
                raise ValueError("missing " + _missing_names(CLEF_CHECKPOINT_PROVIDER))
            behind = [name for name in hosted if name != CLEF_CHECKPOINT_PROVIDER]
            if not behind:
                raise ValueError(
                    "no configured hosted provider behind Clef for the "
                    + CLEF_CHAINED_PROVIDER
                    + " route"
                )
            return [CLEF_CHECKPOINT_PROVIDER, *behind]
        if not hosted:
            raise ValueError(
                "missing "
                + " or ".join(PROVIDER_ENV[name] for name in order)
                + " for the "
                + LOCAL_WITH_API_FALLBACK
                + " mode"
            )
        return [LOCAL_PROVIDER, *hosted]
    if mode == API_WITH_LOCAL_FALLBACK:
        # The hosted API leads and the local slot stands behind it, so a case
        # costs no local request while a hosted provider answers. With no
        # configured hosted provider there is nothing to lead with, so this
        # fails fast rather than quietly answering from the local slot under the
        # name of a hosted-first mode.
        if _clef_pinned_mode(provider):
            # A Clef-pinned hosted-first mode leads with Clef and puts the local
            # slot directly behind it. The rest of the hosted order is not
            # dragged in: a name that pins Clef says which provider leads.
            if not configured[CLEF_CHECKPOINT_PROVIDER]:
                raise ValueError("missing " + _missing_names(CLEF_CHECKPOINT_PROVIDER))
            return [CLEF_CHECKPOINT_PROVIDER, LOCAL_PROVIDER]
        if not hosted:
            raise ValueError(
                "missing "
                + " or ".join(PROVIDER_ENV[name] for name in order)
                + " for the "
                + API_WITH_LOCAL_FALLBACK
                + " mode"
            )
        return [*hosted, LOCAL_PROVIDER]
    # ``api_only``. A name that pins one hosted provider is served by that
    # provider; the bare mode is served by the first configured hosted provider
    # in the order. A failure on this mode is reported and never rerouted.
    if _clef_pinned_mode(provider):
        if not configured[CLEF_CHECKPOINT_PROVIDER]:
            raise ValueError("missing " + _missing_names(CLEF_CHECKPOINT_PROVIDER))
        return [CLEF_CHECKPOINT_PROVIDER]
    # ``openrouter`` and ``jev_api`` pin OpenRouter, so a selection naming one
    # is served by OpenRouter even when another hosted provider is configured.
    # This is unchanged from v1.3.0, where the same names returned exactly
    # ``["openrouter"]`` or raised naming OPENROUTER_API_KEY.
    if provider.strip().lower() in ("openrouter", "jev_api"):
        if not configured["openrouter"]:
            raise ValueError("missing " + _missing_names("openrouter"))
        return ["openrouter"]
    if not hosted:
        # The mode's own name is used here, so the message names the variable a
        # caller must set rather than a route name that may be an alias.
        raise ValueError(
            "missing "
            + " or ".join(PROVIDER_ENV[name] for name in order)
            + " for the "
            + API_ONLY
            + " mode"
        )
    return hosted[:1]


def _missing_names(name: str) -> str:
    """The variable names a provider needs, for a failure message.

    A provider with one required variable is named by it. Clef has two, and
    both are named so a misconfiguration is visible without reading the source.
    """
    return " and ".join(PROVIDER_REQUIRED_ENV.get(name, (PROVIDER_ENV[name],)))


def _hosted_adapter(
    name: str,
    api_key: str | None,
    timeout: float | None,
    env: dict[str, str],
    clef_model: str = CLEF_DEFAULT_MODEL,
):
    """Build the adapter for one hosted provider name."""
    if name == "openrouter":
        return OpenRouterJev(api_key, timeout=30.0 if timeout is None else timeout)
    if name == CLEF_CHECKPOINT_PROVIDER:
        # The token and the account id stay in the environment. A caller that
        # holds them itself can pass them in, and an explicit value wins over
        # the environment exactly as it does for the other hosted providers.
        return ClefJev(
            api_key,
            model=clef_model,
            timeout=30.0 if timeout is None else timeout,
            env=env,
        )
    raise ValueError("invalid provider")


def build_provider(
    provider: str = "auto",
    *,
    api_key: str | None = None,
    laya_base_url: str = LAYA_BASE_URL,
    laya_model: str = LAYA_MODEL,
    clef_model: str = CLEF_DEFAULT_MODEL,
    local_model: object = None,
    timeout: float | None = None,
    fallback_order: tuple[str, ...] | list[str] = DEFAULT_FALLBACK_ORDER,
    env: dict[str, str] | None = None,
):
    """Build the adapter for the configured mode.

    ``api_key`` is the hosted key for every mode whose API side is hosted, and
    also for the modes whose local slot leads, whose local hop keeps its own
    optional ``LAYA_API_KEY``. On ``local_only`` it is the optional token for a
    local server started with its own bearer check; when it is absent the
    request carries no ``Authorization`` header. An explicit key also supplies
    the hosted credential for the mode check, so a caller that holds the key in
    its own configuration does not need it in the environment as well.

    ``local_model`` selects which local decision model answers. It takes
    precedence over the pre-existing ``laya_model`` when it is set, because the
    local slot is a generic slot and the old field is the deprecated spelling of
    the same setting. ``laya_model`` is still accepted, and an entry that stored
    only ``laya_model`` keeps calling that checkpoint.
    """
    source = dict(os.environ if env is None else env)
    mode = resolve_provider(provider)
    if api_key and mode != LOCAL_ONLY:
        source[PROVIDER_ENV["openrouter"]] = api_key
    order = provider_order(
        provider,
        fallback_order=fallback_order,
        env=source,
        local_model=local_model,
    )
    if not order:
        raise RuntimeError(
            "no Jev provider is configured: set OPENROUTER_API_KEY"
            " or select the local Laya provider"
        )
    # ``local_model`` wins when it is set, and ``laya_model`` is the deprecated
    # spelling that still works. Resolving it here means one validation of the
    # name, at build time, rather than a name that reaches the wire unvalidated.
    checkpoint = local_checkpoint(laya_model if local_model is None else local_model)
    if mode == LOCAL_ONLY:
        return LayaJev(
            laya_base_url,
            model=checkpoint,
            api_key=api_key,
            timeout=LAYA_TIMEOUT if timeout is None else timeout,
        )
    if len(order) > 1:
        # Both fallback modes are two-provider chains, and the pre-existing
        # Clef-first chain is one of them. The order decides which adapter is
        # tried first, so the same construction serves the local-first chain and
        # the hosted-first chain.
        chain = []
        for name in order:
            if name == LOCAL_PROVIDER:
                # The local hop needs no hosted key, so ``api_key`` belongs to
                # the hosted hops only.
                chain.append(
                    (
                        name,
                        LayaJev(
                            laya_base_url,
                            model=checkpoint,
                            timeout=LAYA_TIMEOUT if timeout is None else timeout,
                        ),
                    )
                )
                continue
            chain.append(
                (
                    name,
                    _hosted_adapter(
                        name,
                        source.get(PROVIDER_ENV[name]) or None,
                        timeout,
                        source,
                        clef_model,
                    ),
                )
            )
        return ChainedJev(chain)
    return _hosted_adapter(
        order[0],
        source.get(PROVIDER_ENV[order[0]]) or None,
        timeout,
        source,
        clef_model,
    )


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return _SECRET_TEXT.sub(
            r"\1\2[REDACTED]", _BEARER_TEXT.sub(r"\1\2[REDACTED]", value)
        )
    if isinstance(value, Mapping):
        return {
            str(key): ("[REDACTED]" if _key_is_secret(key) else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    return value


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


# The case fields a review targets. A decision that names an entity and service
# data is the decision the policy can answer for, so the workflow reads them out
# of the case rather than authorizing a bare action name.
ENTITY_FIELD = "entity_id"
SERVICE_DATA_FIELD = "service_data"


def _target(case: Case, decision: Decision) -> tuple[str | None, dict[str, Any] | None]:
    """The entity and service data a decision asks for.

    Both are read from the case first, because that is where an automation puts
    the request it actually made, and from the decision's ``raw`` payload second,
    because a provider that answered with a specific entity is the only thing
    that knows it. Neither source is trusted: whatever is found is passed
    through the policy, which decides for itself.
    """
    facts = case.facts or {}
    entity = facts.get(ENTITY_FIELD)
    if entity is None:
        entity = decision.raw.get(ENTITY_FIELD)
    entity = str(entity) if entity else None
    service_data = facts.get(SERVICE_DATA_FIELD)
    if service_data is None:
        service_data = decision.raw.get(SERVICE_DATA_FIELD)
    if not isinstance(service_data, Mapping):
        service_data = None
    return entity, dict(service_data) if service_data is not None else None


class SentinelWorkflow:
    def __init__(
        self,
        provider: Any,
        policy: "Policy | None" = None,
    ) -> None:
        self.provider = provider
        self.policy = policy or Policy()

    def review(self, case: Case) -> Decision:
        return self.provider.decide(
            {
                "case": redact(case.to_dict()),
                "allowed_actions": sorted(self.policy.allowed_actions),
            }
        )

    def execute(
        self,
        case: Case,
        decision: Decision,
        dispatch: Callable[[str], Any],
        readback: Callable[[], Any],
        *,
        user_approved: "Approval | Mapping[str, Any] | bool | None" = None,
    ) -> dict[str, Any]:
        """Authorize, dispatch, and verify one bounded case.

        The Home Assistant bridge carried ``review`` alone, so an integration
        built against it had no authorize step at all and every action it
        dispatched was authorized by the caller's own judgement. The workflow
        below is the same one ``sentinel.workflow`` carries, adapted to this
        module's ``verify``, which returns a mapping rather than a dataclass.
        """
        if decision.shadow:
            authorization = {
                "allowed": False,
                "status": "shadow_only",
                "reason": "Shadow decisions are recommendations and cannot be dispatched.",
            }
        else:
            entity_id, service_data = _target(case, decision)
            authorization = self.policy.authorize(
                decision.action,
                entity_id,
                service_data,
                user_approved=user_approved,
            )
        result: dict[str, Any] = {
            "case": case.to_dict(),
            "decision": decision.to_dict(),
            "authorization": authorization,
        }
        if not authorization["allowed"]:
            result["verification"] = verify(
                case.facts.get("expected_state"), None, available=False
            )
            return result
        try:
            dispatch_result = dispatch(decision.action or "")
        except Exception as exc:  # the caller receives a safe, structured failure
            result["dispatch"] = {"status": "error", "error_type": type(exc).__name__}
            result["verification"] = {
                "verified": False,
                "status": "dispatch_failed",
                "next_step": "notify_and_retry",
            }
            return result
        # An awaitable result means the dispatcher was async. In Home Assistant
        # every service call is, so this was the ordinary case rather than the
        # exotic one: the call returned a coroutine, this code recorded
        # `{"status": "sent", "result_type": "coroutine"}`, the coroutine was
        # garbage-collected without ever being awaited, and the device never
        # moved. The readback then compared the state the device was already in
        # and closed the case as a successfully verified action that physically
        # did not happen.
        #
        # A synchronous boundary cannot honour an awaitable, so it refuses
        # rather than reporting success. Escalating to an async caller is the
        # fix, and refusing is what makes that visible.
        if inspect.isawaitable(dispatch_result):
            if hasattr(dispatch_result, "close"):
                dispatch_result.close()  # do not leave a never-awaited coroutine
            result["dispatch"] = {
                "status": "unsupported_dispatch",
                "result_type": "awaitable",
            }
            result["verification"] = {
                "verified": False,
                "status": "dispatch_not_performed",
                "next_step": "notify_and_retry",
            }
            return result
        result["dispatch"] = {
            "status": "sent",
            "result_type": type(dispatch_result).__name__,
        }
        try:
            actual = readback()
            result["verification"] = verify(case.facts.get("expected_state"), actual)
        except Exception as exc:
            result["verification"] = {
                "verified": False,
                "status": "readback_failed",
                "error_type": type(exc).__name__,
                "next_step": "reopen_case",
            }
        return result


def verify(expected: Any, actual: Any, *, available: bool = True) -> dict[str, Any]:
    if not available:
        return {
            "verified": False,
            "status": "unavailable",
            "expected": expected,
            "actual": actual,
            "next_step": "notify_and_retry",
        }
    # Neither side being known is not a match. A case built without an
    # expectation and an entity whose readback returned nothing compared equal
    # and closed the case as a successfully verified device action with no state
    # readback performed.
    if expected is None or actual is None:
        unreadable = expected is None
        return {
            "verified": False,
            "status": "no_expectation" if unreadable else "unavailable",
            "expected": expected,
            "actual": actual,
            "next_step": "notify_and_retry",
        }
    matched = expected == actual
    return {
        "verified": matched,
        "status": "matched" if matched else "mismatch",
        "expected": expected,
        "actual": actual,
        "next_step": "close_case" if matched else "reopen_case",
    }
