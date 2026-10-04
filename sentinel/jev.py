"""Typed Jev adapter boundary.

The public core accepts any DecisionProvider. Four adapters ship here:

- ``OpenRouterJev`` calls hosted Jev over an OpenRouter API key.
- ``ClefJev`` calls Cloudflare Workers AI, which hosts the Clef decision model.
  It needs an API token and a Cloudflare account id, and it offers two
  checkpoints of one model rather than two providers.
- ``LayaJev`` calls a local ``laya-serve`` process on this machine. It needs no
  key.
- ``ChainedJev`` answers from the first adapter in an ordered chain that
  succeeds, so a caller can put Clef first and the hosted Jev key behind it, or
  the local server first and a hosted key behind it.

Every single adapter sends the same rubric, reads the same Decisions shaped
answer, validates the typed answers the same way, and maps the score answer with
the same helper, so a case reviewed on one route and a case reviewed on another
produce the same ``Decision`` fields. The chain adds the name of the adapter
that answered to ``Decision.raw``. Nothing here imports Hermes internals.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from .models import Decision

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # the chain is structural; nothing is imported at runtime
    from collections.abc import Mapping, Sequence

    from .workflow import DecisionProvider

ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
MODEL = "typesafe/jev-1.13"

LAYA_BASE_URL = "http://127.0.0.1:8000"
LAYA_ENDPOINT_PATH = "/v1/systemone"
LAYA_MODEL = "convaiinnovations/laya"
# A CPU checkpoint answers in a couple of seconds, but a cold process or a
# first request after a restart is slower. The hosted 30 second default is too
# short for the local route, so it carries its own budget.
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
# The shape a local model name may take. This is deliberately a character
# shape and not a list of model names: a local model published after this
# release must work by configuration alone, with no code change. It rejects
# empty and whitespace-only values, control characters, quotes and backslashes
# that would corrupt a JSON string, and the URL delimiters ``?``, ``#``, and
# ``&`` that would corrupt a path segment if the value is ever placed in one.
_LOCAL_MODEL_SHAPE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:+/-]*\Z")

# Clef is a Cloudflare Workers AI decision model, so its endpoint is per account
# and the account id is part of the URL rather than an optional setting. The
# account id is configuration; the API token is the credential. Both are read
# from the environment and neither is stored in a config entry, because a
# Home Assistant config entry is written to disk in plain text.
CLEF_API_BASE = "https://api.cloudflare.com/client/v4/accounts"
CLEF_RUN_PATH = "/ai/run/@cf/cloudflare/{model}"
CLEF_DEFAULT_MODEL = "clef"
CLEF_MODELS = ("clef", "clef-flash")
CLEF_ACCOUNT_ENV = "CLOUDFLARE_ACCOUNT_ID"
CLEF_TOKEN_ENV = "CLOUDFLARE_API_TOKEN"
CLEF_TIMEOUT = 30.0

# Clef accepts a question id built from letters, digits, '_', '.', and '-',
# at most 100 characters, with at most 64 questions per request. This
# repository builds ids of the form "candidate:id" and "hook:name", and ':' is
# not permitted, so the mapping in ``clef_question_ids`` is load bearing rather
# than defensive. The answer is mapped back, so a caller never sees a renamed
# question.
CLEF_ID_ALLOWED = re.compile(r"\A[A-Za-z0-9_.-]+\Z")
CLEF_ID_MAX_LENGTH = 100
CLEF_MAX_QUESTIONS = 64

# A failed attempt falls through to the next provider in a chain only when the
# provider was unreachable, refused, rate limited, or broken. Any other status,
# such as 400 or 422, says the request itself was rejected, and the same request
# is not retried somewhere else.
FALLBACK_STATUS_CODES = frozenset({401, 403, 429})

# The name of the route that runs on this machine. The chain uses it to tell its
# local hop from a hosted one.
LOCAL_PROVIDER = "laya"

# A repeated local outage must not quietly turn every household case into remote
# traffic, which is the privacy property this repository exists to protect.
# Three consecutive local failures that qualified for the hosted fallback still
# fall back; the fourth, and every one after it, is suppressed, the local error
# is re-raised instead of being answered remotely, and a warning is logged. The
# counter is per process and resets on restart, and any successful local call
# resets it to zero.
LOCAL_FALLBACK_FAILURE_LIMIT = 3
_local_failure_lock = threading.Lock()
_local_failure_count = 0


def local_failure_count() -> int:
    """The consecutive local failures recorded in this process."""
    with _local_failure_lock:
        return _local_failure_count


def reset_local_failures() -> None:
    """Clear the local failure counter after a successful local call."""
    global _local_failure_count
    with _local_failure_lock:
        _local_failure_count = 0


def note_local_failure(exc: BaseException) -> bool:
    """Record one failed local attempt and say whether the hosted hop may answer.

    Only the exception class name is logged. The case, the entity state, and the
    answer never reach a log record. ``True`` means the count is still at or
    below ``LOCAL_FALLBACK_FAILURE_LIMIT``, so the hosted fallback may be
    attempted. ``False`` means the breaker is tripped and the caller must
    re-raise the local error rather than answer it remotely.
    """
    global _local_failure_count
    logger.warning(
        "local %s decision failed (%s); considering the hosted fallback",
        LOCAL_PROVIDER,
        type(exc).__name__,
    )
    with _local_failure_lock:
        _local_failure_count += 1
        return _local_failure_count <= LOCAL_FALLBACK_FAILURE_LIMIT


# The confidence question is a ``score`` question, and a score rubric is an
# ordered list of level descriptions with index 0 first. A local Laya server
# rejects ``{"min": 0, "max": 1}`` with "a score question takes 'criteria' as a
# list of level descriptions, index 0 first". The level count therefore fixes
# the resolution of the derived confidence value, which
# ``confidence_from_score`` documents.
CONFIDENCE_LEVELS = (
    "very low confidence, the case is ambiguous or mostly missing",
    "low confidence",
    "moderate confidence",
    "high confidence",
    "very high confidence, the case points one way",
)


def decision_questions() -> dict[str, dict]:
    """The rubric both adapters send. A fresh mapping on every call."""
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
    """The number of levels a score answer's index scale has.

    A ``legend`` in the answer is authoritative, because it is what the provider
    actually scored against; the configured rubric is the fallback. Both the
    clamp in ``confidence_from_score`` and the range check in
    ``validate_score_answer`` read the scale through this one function, so the
    two cannot disagree about how many levels exist.
    """
    legend = answer.get("legend")
    return len(legend) if isinstance(legend, (dict, list)) else len(levels)


def score_index(answer: object, *, levels: tuple[str, ...] = CONFIDENCE_LEVELS):
    """The expected level index a score answer reports, or ``None``.

    This is the same read of the answer that ``confidence_from_score`` performs
    before it rescales onto 0 to 1, exposed on its own so a caller that must
    reject an out-of-range index validates against the identical number instead
    of a second implementation of the scale.
    """
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
    index = score_index(answer, levels=levels)
    if index is None or not isinstance(answer, dict):
        raise ValueError("invalid score answer: no numeric score index")
    count = _legend_count(answer, levels)
    if count < 2:
        raise ValueError("invalid score answer: the legend needs at least two levels")
    if not 0 <= index <= count - 1:
        raise ValueError(
            f"invalid score answer: index {index:g} is outside the"
            f" 0 to {count - 1} legend scale"
        )


def validate_answers(
    answers: object,
    questions: dict[str, dict],
    *,
    label: str,
) -> dict[str, Any]:
    """Check a Decisions shaped answer against the rubric that asked for it.

    This is the one typed answer validation every route shares: a hosted Jev
    reply, a local Laya reply, and a Cloudflare Clef reply are all checked here,
    so an unknown choice or an out-of-range score index is refused the same way
    whatever answered. The rubric is the authority, so a provider cannot widen
    the outcome or action set by returning a value this repository never
    offered.
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

    A missing, non numeric, or single level answer returns ``None``, because no
    position on a 0 to 1 scale can be stated for it.
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
    outcome = answers["outcome"]["choice"]
    selected = answers["action"]["choice"]
    return Decision(
        outcome,
        outcome.replace("_", " "),
        confidence_from_score(answers.get("confidence")),
        None if selected == "none" else selected,
        shadow=True,
        raw={"model": body.get("model", model)},
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

    def decide(self, state: dict) -> Decision:
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY is required for live Jev decisions")
        payload = {"model": MODEL, "state": state, "questions": decision_questions()}
        request = Request(
            ENDPOINT,
            data=json.dumps(payload).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "X-Title": "Jev Home Sentinel",
            },
        )
        with urlopen(request, timeout=self.timeout) as response:
            body = json.loads(response.read().decode())
        return decision_from_body(body, model=MODEL)


def laya_endpoint(
    base_url: str = LAYA_BASE_URL, endpoint_path: str = LAYA_ENDPOINT_PATH
) -> str:
    """Resolve a local Laya server URL, refusing cleartext to a remote host."""
    if not endpoint_path.startswith("/"):
        raise ValueError("invalid Laya endpoint path")
    parsed = urlsplit(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("invalid Laya base URL")
    if parsed.scheme == "http" and parsed.hostname not in _LOOPBACK_HOSTS:
        raise ValueError("nonlocal Laya server requires HTTPS")
    return base_url.rstrip("/") + endpoint_path


def clef_checkpoint(model: str | None = None) -> str:
    """The Clef checkpoint to call, validated.

    Clef Flash is the smaller checkpoint Cloudflare documents for latency bound
    paths. Both answer the same typed questions, so it is a checkpoint of one
    provider and not a provider of its own. An unset or blank name means the
    default, so an entry that stored nothing keeps calling ``clef``; any other
    unknown name raises rather than silently calling a checkpoint that does not
    exist.
    """
    candidate = (model or "").strip().lower() or CLEF_DEFAULT_MODEL
    if candidate not in CLEF_MODELS:
        raise ValueError("invalid Clef checkpoint: " + ", ".join(CLEF_MODELS))
    return candidate


def clef_credentials(
    env: "Mapping[str, str] | None" = None,
) -> tuple[str, str]:
    """The Cloudflare account id and API token, validated before any request.

    Both are read from the environment and never from a config entry. The
    account id is configuration and the token is the credential, but both are
    required before a request is made, and the message names the variable that
    is missing rather than anything about the value.
    """
    account, token = _require_clef_credentials(
        None, None, os.environ if env is None else env
    )
    return account, token


def _require_clef_credentials(
    account: str | None,
    token: str | None,
    env: "Mapping[str, str]",
) -> tuple[str, str]:
    """The account id and token to use, and the name of anything missing.

    A constructor value wins over the environment, including a value that is
    present but empty: an empty credential is a missing credential, so it is
    never topped up from the process environment. ``None`` means "read the
    environment", which is what a caller with no value of its own passes.
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


def clef_endpoint(account: str, model: str = CLEF_DEFAULT_MODEL) -> str:
    """The Workers AI run URL for one account and checkpoint.

    The account id is percent encoded, because it is user supplied text placed
    in a URL path, and a value carrying a slash or a space must not be able to
    rewrite the path.
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

    Clef accepts letters, digits, '_', '.', and '-', at most 100 characters,
    and at most 64 questions in one request. A repository id such as
    ``candidate:aaa`` or ``hook:lamp_on`` contains a colon, which is not
    permitted, so it is rewritten to a safe id before the request and the
    answer is mapped back afterwards. The mapping is total and injective: an id
    already in the alphabet is passed through untouched, a rewritten id that
    would collide with another one is suffixed until it does not, and the
    returned ``safe -> original`` map restores the caller's own names.
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
    envelope carries Cloudflare's own error codes, which are more useful than a
    generic parse failure, so they are surfaced rather than swallowed. The typed
    answers are then checked by the repository's shared validator, against the
    rubric that asked for them.
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
    return {
        "model": body.get("model", model),
        "answers": answers,
    }


class ClefJev:
    """Cloudflare Clef over a Workers AI account and API token.

    Clef is not a Jev endpoint. It is a Cloudflare hosted decision model that
    answers the same typed questions in the same ``answers`` shape, so selecting
    it joins the hosted routes and can sit in a fallback order, instead of
    replacing one the way the local route does.

    Two checkpoints of one model ship: ``clef`` and ``clef-flash``. The
    checkpoint selects the run path and the model string, and never the shape
    of the answer.

    The account id and the API token are read from ``CLOUDFLARE_ACCOUNT_ID`` and
    ``CLOUDFLARE_API_TOKEN`` and are checked before any socket work, so a
    misconfigured route fails on its first review instead of degrading into
    another classifier. On failure only the exception class name is logged: the
    reviewed case, the entity state, the account, and the token never reach a
    log record.
    """

    def __init__(
        self,
        api_token: str | None = None,
        *,
        account_id: str | None = None,
        model: str = CLEF_DEFAULT_MODEL,
        timeout: float = CLEF_TIMEOUT,
        env: "Mapping[str, str] | None" = None,
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

    def _credentials(self, env: "Mapping[str, str] | None" = None) -> tuple[str, str]:
        """Both credentials, from the constructor or from its own environment."""
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
                raise HTTPError(
                    exc.url,
                    exc.code,
                    f"HTTP {exc.code} (Cloudflare code {detail})",
                    exc.headers,
                    exc.fp,
                ) from exc
            raise
        except Exception as exc:
            # Only the exception category is logged. The case, the account, and
            # the token stay out of the log record.
            logger.warning("clef decision failed (%s)", type(exc).__name__)
            raise
        try:
            normalised = clef_answers(
                body,
                questions,
                restore=restore,
                model=self.model,
            )
        except Exception as exc:
            logger.warning("clef decision failed (%s)", type(exc).__name__)
            raise
        return decision_from_body(normalised, model=self.model)


def _cloudflare_error_detail(exc: HTTPError, *, limit: int = 2000) -> str:
    """Cloudflare's own error codes from an HTTP error body, if it carries any.

    The body is read once and only its ``errors`` codes are used. No other part
    of a Cloudflare error body reaches a log record or an exception message.
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


class LayaJev:
    """A local decision-model server, over the same Decisions contract.

    The local slot is generic. It is not a hosted Jev endpoint, and it is not
    bound to one model either: it publishes ``POST /v1/systemone`` and answers
    in the same ``answers`` shape, so Laya or another pre-deterministic routing
    model is selected by the ``model`` argument, which is the value
    ``local_model`` supplies, rather than by a different provider name. The route
    is keyless: the ``Authorization`` header is omitted entirely unless the
    server was started with its own bearer check, because an empty header is not
    the same request as no header.
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

    def decide(self, state: dict) -> Decision:
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
        decision = decision_from_body(body, model=self.model)
        # A successful local call clears the consecutive-failure count, on the
        # local-only route and on the local hop of the chained route alike.
        reset_local_failures()
        return decision


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

    The chain holds named adapters in the order they are tried. A local Laya hop
    can sit first with a hosted hop behind it, so a case costs no provider
    request while the local server answers, and a local outage still returns a
    decision. The adapter that answered is recorded in ``Decision.raw``.
    """

    def __init__(self, providers: "Sequence[tuple[str, DecisionProvider]]") -> None:
        if len(providers) < 2:
            raise ValueError("a chain needs at least two providers")
        self.providers: tuple[tuple[str, DecisionProvider], ...] = tuple(providers)

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
                        if note_local_failure(exc):
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
