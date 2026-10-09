# Technical reference

This reference describes the public contracts implemented in `custom_components/jev_sentinel` and `sentinel`.

## What answers the decision step

A **System One decision model**, also written *typed decision model*, is a model that returns typed values, each carrying a probability, rather than prose. [TypeSafe coined the category alongside Jev on 15 September 2026](https://systemonemodels.org/guides/what-is-a-system-one-model/), and it is written System 1 as well. **Jev is one member of the category, not the name of it.**

The ecosystem index at https://systemonemodels.org/ catalogues further members of the category, both open-weight and hosted. This reference names only the members this repository reaches.

| Model | Where it runs | Weights | Adapter here |
|---|---|---|---|
| **Jev** | hosted, TypeSafe or OpenRouter | closed | `OpenRouterJev` |
| **Clef** and **Clef Flash** | hosted, Cloudflare Workers AI | closed | `ClefJev`, checkpoint chosen by `clef_model` |
| **Laya** | local, on this machine | open | `LayaJev`, the default for `local_model` |
| **Laya or other pre-deterministic routing models** | wherever you host them | whatever that engine ships | the local slot, as any `local_model` value |

## Runtime boundaries

The Home Assistant component creates a case, calls the configured provider, and fires events. It does not dispatch a Home Assistant device service. The standalone core adds policy authorization and execution callbacks for an external consumer.

| Boundary | Source contract | Result |
|---|---|---|
| Provider | `OpenRouterJev.decide(state)`, `ClefJev.decide(state)`, `LayaJev.decide(state)`, or `ChainedJev.decide(state)` | `Decision` |
| Policy | `Policy.authorize(action, user_approved=False)` | authorization dictionary |
| Dispatch | caller-supplied callable | dispatch record |
| Readback | caller-supplied callable or `verify` service values | verification record |

Every provider answers the same call: it sends `{"model": ..., "state": ..., "questions": ...}` and reads an `answers` mapping keyed by question id. The typed answers are checked against the rubric that asked for them by one shared validator, so an unknown choice or an out-of-range score index is refused the same way whatever answered.

## Case schema

`Case.create(event_type, *, area=None, entities=(), facts=None, requested_action=None)` returns a frozen dataclass.

```json
{
  "case_id": "case_0123456789ab",
  "event_type": "window_open_while_heating",
  "area": "living_room",
  "entities": ["climate.living_room", "binary_sensor.living_room_window"],
  "facts": {"window_open_minutes": 14, "expected_state": "off"},
  "requested_action": "climate.set_temperature",
  "created_at": "2026-09-19T20:00:00+00:00"
}
```

`case_id` is generated from a UUID prefix. `entities` is stored as a tuple in Python and serializes through `asdict`. `facts` accepts arbitrary JSON-friendly values. The caller supplies the bounded facts; Sentinel does not gather Home Assistant history.

## Jev recommendation schema

`Decision` contains the provider result:

```json
{
  "outcome": "recommend",
  "reason": "recommend",
  "confidence": 0.86,
  "action": "climate.set_temperature",
  "shadow": true,
  "raw": {"model": "typesafe/jev-1.13"}
}
```

The runtime asks for `outcome`, `action`, and `confidence`. Outcomes are constrained by the request to `ignore`, `notify`, `ask_user`, `recommend`, or `escalate`. Actions offered to the model are `notify`, `ask_user`, `light.turn_off`, `switch.turn_off`, `climate.set_temperature`, and `none`. The runtime converts `none` to `action: null`.

`confidence` is a float in `[0.0, 1.0]` or `null`. Its scale is defined under [Confidence scale](#confidence-scale).

`shadow` is true in the runtime and core defaults. `SentinelWorkflow.execute` refuses to dispatch a shadow decision and returns `authorization.status: shadow_only`. Only a caller-created non-shadow decision can enter the provider-neutral execution path.

## The four decision modes

Configuration exposes exactly four modes. Each names which side leads and whether the other side is a fallback:

| Mode | Leads | Fallback | Provider order |
|---|---|---|---|
| `api_with_local_fallback` | hosted API | local | every configured hosted provider, in the hosted order, then `laya` |
| `api_only` | hosted API | none | one configured hosted provider |
| `local_only` | local | none | `["laya"]` |
| `local_with_api_fallback` | local | hosted API | `["laya", ...]` every configured hosted provider, in the hosted order |

`api_only` and `local_only` are single-provider routes: no chain, no fallback, and no cooldown list beyond the one provider. A failure there is reported, never rerouted. `api_with_local_fallback` and `local_with_api_fallback` are two-provider chains and use the existing cooldown, trigger, and breaker machinery unchanged.

A mode that promises a fallback but has no usable provider for the other side fails at load, naming the missing environment variable, rather than silently degrading into a single-provider mode under another name.

### The alias table

Every name this repository shipped before v1.4.0 keeps working, so no deployed configuration breaks. Each alias resolves onto one canonical mode before it reaches a chain, a diagnostic, a log line, or a URL, so no alias string appears in observable output.

| Alias | Resolves to | Provider order it produces |
|---|---|---|
| `api_with_local_fallback` | itself | `["openrouter", "laya"]` |
| `clef_with_local_fallback` | `api_with_local_fallback`, Clef-pinned | `["clef", "laya"]` |
| `api_only` | itself | `["openrouter"]` |
| `openrouter`, `jev_api` | `api_only`, OpenRouter-pinned | `["openrouter"]` |
| `clef`, `clef_api` | `api_only`, Clef-pinned | `["clef"]` |
| `auto` | `api_only`, or `api_with_local_fallback` when a `local_model` is set | the configured hosted providers, plus `laya` when a local model is set |
| `local_only` | itself | `["laya"]` |
| `laya`, `laya_local` | `local_only` | `["laya"]` |
| `local_with_api_fallback` | itself | `["laya", "openrouter"]` |
| `laya_then_hosted`, `laya_with_jev_fallback` | `local_with_api_fallback` | `["laya", "openrouter"]` |
| `clef_then_jev`, `clef_with_jev_fallback` | `local_with_api_fallback`, Clef-pinned | `["clef", "openrouter"]` |

Three of these need explaining, because the four-mode vocabulary does not map cleanly onto everything this repository shipped:

- **`auto` is dynamic.** It resolves to `api_with_local_fallback` when a `local_model` has been explicitly configured and to `api_only` otherwise, which is what it meant before v1.4.0: the hosted providers that are configured. It never selects the local slot on its own initiative, so `provider_order("auto", env={}, local_model="your-local-engine")` is still `[]`.
- **The hosted provider names resolve to `api_only`.** `openrouter`, `jev_api`, `clef`, and `clef_api` each named one hosted provider whose failure was reported and never rerouted, so each keeps meaning exactly that. A hosted provider reaches `api_with_local_fallback` only by naming the mode or the Clef-pinned alias.
- **`clef_then_jev` is not one of the four modes.** Both of its hops are hosted, so it is a hosted-to-hosted chain rather than one side falling back to the other. It resolves to `local_with_api_fallback` so an entry storing it keeps routing the same way, and `pins_clef(stored_name)` preserves the Clef lead, because the canonical mode name alone cannot express which hosted provider leads. The Home Assistant entry module therefore reads the stored spelling before resolving it, and passes the stored name rather than the resolved mode to `build_provider`.

Case handling is unchanged from v1.3.0. The arrangement names and the four canonical mode names are recognised in either case; the canonical route names are matched exactly as they were, so `provider_order("Laya")` still raises rather than newly becoming the local mode.

`MODE_NAMES` is the four canonical mode names, `MODE_ALIASES` maps every accepted name onto one of them, and `PROVIDER_MODES` maps back. `resolve_provider(name)` rewrites an accepted name and returns anything else unchanged, so an unknown value keeps its existing validation. `provider_mode(name)` returns the canonical mode and raises `ValueError("invalid provider: ...")` for anything else, and the message names every accepted mode and alias rather than hiding them.

### The hosted providers are not renamed

Which hosted model answers the API side is its own choice and is unchanged:

1. Hosted Jev, over an OpenRouter API key. Jev is one System One decision model, served by TypeSafe and reachable here through OpenRouter.
2. Cloudflare Clef, over a Workers AI account and API token from the environment, with `clef` and `clef-flash` as two checkpoints of the one provider. Clef is a separate member of the same category, not a Jev endpoint.

Both remain individually selectable and remain the members of `HOSTED_PROVIDERS` and of the hosted fallback order.

### The local slot is a generic local decision-model slot

The provider name in configuration stays `laya`. What changed in v1.4.0 is that it is no longer a hard-coded binding to one model: the new `local_model` setting selects which local decision model answers, it defaults to `laya`, and its value is the checkpoint or engine name sent to the local server. The local routes are **Laya or other pre-deterministic routing models**: Laya is the default, and any other engine publishing the same request shape is selected by naming it.

`local_checkpoint(model)` resolves the value:

- `None` returns `laya`, so an entry that stored nothing keeps calling Laya and the default path is unchanged.
- Any other name is accepted. There is deliberately **no allowlist of model names**, because the point is interchangeability: a local model published after this release must work by configuration alone, with no code change and no new provider name.
- A value that cannot be a name at all is refused with `ValueError("invalid local_model: ...")`: an empty or whitespace-only value, a value that is not text, and a value carrying a control character, a quote, a backslash, whitespace, or the URL delimiters `?`, `#`, and `&`, any of which would corrupt the JSON body or a URL path segment.

Known to fit the slot, because they publish the same `/v1/systemone` request shape: `laya` (also `laya-multilingual` and `laya-typed-decisions`), and any other local pre-deterministic routing model serving that endpoint. Swapping `base_url` at the engine's own server is the whole migration, so a different engine costs two configuration values and no code change.

The local server URL remains its own setting, `laya_base_url`, so pointing the slot at a different engine is a configuration change. See [the integration guide](integrations.md#pointing-the-local-slot-at-a-different-engine).

`local_model` never appears in a fallback order and is never a mode alias: `validate_fallback_order(("your-local-engine",))` raises `ValueError("invalid fallback_order")` and `provider_order("your-local-engine")` raises `ValueError("invalid provider: ...")`.

### `laya_model` and `local_model`

The pre-existing `laya_model` setting is kept for backwards compatibility and is the deprecated spelling of `local_model`. **`local_model` takes precedence when it is set**; an entry that stored only `laya_model` keeps calling that checkpoint. `build_provider(local_model=..., laya_model=...)` resolves the same way, and so does the Home Assistant entry module.

`MODE_NAMES` is the five arrangement names, `MODE_ALIASES` maps each onto its canonical route, and `PROVIDER_MODES` maps back. `resolve_provider(name)` rewrites an arrangement name, in either case, and returns anything else unchanged, so the canonical names keep their existing validation. `provider_mode(provider)` returns the arrangement name for a route and raises `ValueError("invalid provider")` for anything else, including `auto`.

`provider_order(provider, fallback_order=..., env=...)` resolves a route and returns the providers it may call, in order:

| `provider` | Result | Notes |
|---|---|---|
| `"laya"` | `["laya"]` | Exactly one provider. The local route replaces the hosted routes. |
| `"openrouter"` | `["openrouter"]` | Raises `ValueError("missing OPENROUTER_API_KEY")` without a key. |
| `"clef"` | `["clef"]` | Raises `ValueError("missing CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID")` unless both Clef variables are set. A provider is configured only when every variable it needs is present. |
| `"api_with_local_fallback"` | `["openrouter", "laya"]` with a configured hosted provider, and no result without one | Raises `ValueError("missing OPENROUTER_API_KEY for the api_with_local_fallback mode")` when no hosted provider is configured. |
| `"local_with_api_fallback"` | `["laya", "openrouter"]` with a configured hosted provider, and no result without one | Raises `ValueError("missing OPENROUTER_API_KEY for the local_with_api_fallback mode")` when no hosted provider is configured. |
| `"clef_with_local_fallback"` | `["clef", "laya"]` | Raises `ValueError("missing CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID")` when Clef is not configured. |
| `"clef_with_jev_fallback"` | `["clef", "openrouter"]` | Raises when Clef is not configured, and raises `ValueError("no configured hosted provider behind Clef for the clef_then_jev route")` when nothing is configured behind it. |
| `"auto"` (default) | the configured members of `fallback_order`, `[]` without | Never selects the local route on its own initiative. `DEFAULT_FALLBACK_ORDER` is `("openrouter",)`. |

`validate_fallback_order(order)` accepts a non-empty, duplicate-free order drawn from the hosted names only, which are `("openrouter", "clef")`. `("openrouter", "laya")` and `("laya",)` both raise `ValueError("invalid fallback_order")`, so the local slot cannot be added to the hosted order as a last resort. A mode name is rejected there too: `("local_with_api_fallback",)` raises the same error, because a chain is not a member of a chain, and so does a `local_model` value such as `("your-local-engine",)`, so a model name can never become a hosted member. `provider_order` raises `ValueError("invalid provider: ...")` for any name outside the [alias table](#the-alias-table), which here includes `typesafe`, `clef-flash`, `Laya`, and `your-local-engine`.

`build_provider(...)` returns `LayaJev` for the local route, `OpenRouterJev` for the hosted Jev route, `ClefJev` for the Clef route, and `ChainedJev` for either chained route. It raises `RuntimeError("no Jev provider is configured...")` when `auto` resolves to no provider. An explicit `api_key` also satisfies the OpenRouter route check, so a caller holding its key in its own configuration does not need it in the environment. On a chained route that key belongs to the OpenRouter hops; the local hop keeps its own optional `LAYA_API_KEY`, and the Clef hop never receives it. On the local route, `api_key` is the optional bearer for a `laya-serve` started with its own check.

A Clef checkpoint is passed as `clef_model`, a setting of the route rather than a route name, so `clef-flash` is never a provider. `clef_checkpoint(model)` returns the lower-cased name, treats unset or blank as the default `clef`, and raises `ValueError("invalid Clef checkpoint: clef, clef-flash")` for anything else.

### Hosted OpenRouter provider

The runtime posts JSON to:

```text
https://openrouter.ai/api/alpha/decisions
```

The model is:

```text
typesafe/jev-1.13
```

The payload has `model`, `state`, and `questions`. The request carries an `Authorization: Bearer <key>` header, `Content-Type: application/json`, and an `X-Title` header. A missing key or provider error prevents a decision from being emitted.

Before `SentinelWorkflow.review`, the core redacts dictionary keys containing `token`, `password`, `secret`, `api_key`, or `credential`, case-insensitively. Redaction replaces the value with `[REDACTED]`, masks credential-shaped text inside string values, and recurses through dictionaries and lists.

### Hosted Cloudflare Clef provider

`ClefJev` posts the same payload to the Cloudflare Workers AI run endpoint for the configured account:

```text
https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/@cf/cloudflare/clef
https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/@cf/cloudflare/clef-flash
```

The path is built from the account id and the selected checkpoint, and the request carries an `Authorization: Bearer` header holding the token from `CLOUDFLARE_API_TOKEN`, plus `Content-Type: application/json`. `clef_endpoint(account, model)` returns the URL and percent-encodes the account id, because it is user-supplied text placed in a URL path. The timeout is `CLEF_TIMEOUT`, 30 seconds, the same budget as the other hosted route.

| Setting | Default | Meaning |
|---|---|---|
| `CLOUDFLARE_ACCOUNT_ID` | required | The Cloudflare account whose Workers AI runs the request. Configuration, not a secret. |
| `CLOUDFLARE_API_TOKEN` | required | A Cloudflare API token with `Account > Workers AI > Read`. The credential. |
| `clef_model` | `clef` | The checkpoint: `clef` or `clef-flash`. |

Both variables are read from the environment and neither is stored in a config entry, because a Home Assistant config entry is written to disk in plain text. Both are checked before any socket work, and the message names the variable that is missing:

```text
CLOUDFLARE_API_TOKEN is required for live Clef decisions
CLOUDFLARE_ACCOUNT_ID is required for live Clef decisions
CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID are required for live Clef decisions
```

A value supplied to the constructor wins over the environment, including a value that is present but empty: an empty credential is a missing credential and is never topped up from the process environment. `clef_credentials(env)` reads both from a mapping and raises the same messages.

#### Response envelopes

Both documented shapes are accepted, and a top-level `answers` mapping wins over `result.answers`:

```json
{"model": "@cf/cloudflare/clef", "answers": {"outcome": {"type": "choice", "choice": "recommend"}}}
```

```json
{"success": true, "messages": [], "result": {"model": "@cf/cloudflare/clef", "answers": {}}}
```

A `success: false` envelope carries Cloudflare's own error codes, which are surfaced rather than swallowed:

```text
Cloudflare Clef request failed with Cloudflare code 7003
```

A response with no answers in either position raises `RuntimeError("Cloudflare Clef returned an invalid response")`. An HTTP error keeps its status, so `is_fallback_trigger` still recognises a refusal or an outage and a chain still falls through; when the error body carries Cloudflare codes they are folded into the reason string. Only the exception class name is logged on any failure: the case, the entity state, the account id, and the token never reach a log record.

#### Question id sanitisation

Clef accepts question ids built from letters, digits, `_`, `.`, and `-`, at most 100 characters, with at most 64 questions per request. This repository builds ids of the form `candidate:id` and `hook:name`, and `:` is not permitted, so `clef_question_ids(questions)` is load bearing rather than defensive. It returns the rewritten questions and a `safe -> original` map, and `clef_answers` maps the answer back, so a caller never sees a renamed question:

```python
safe, restore = clef_question_ids({"candidate:aaa": question})
# safe   == {"candidate_aaa": question}
# restore == {"candidate_aaa": "candidate:aaa"}
```

The mapping is total and injective. An id already in the alphabet passes through untouched, a rewritten id that would collide is suffixed until it does not, an over-long id is truncated to the limit, and an id that reduces to nothing becomes `question`. More than 64 questions raises `ValueError`, because Clef would reject the request rather than truncate it.

### Local Laya provider

`LayaJev` points the same payload at a `laya-serve` process on loopback. Laya publishes `POST /v1/systemone`, the request shape shared across System One decision models, so the request and response path match the hosted route except for the host, the absence of a credential, and the `model` field, which names the checkpoint rather than a fixed vendor model.

| Setting | Default | Meaning |
|---|---|---|
| `laya_base_url` | `http://127.0.0.1:8000` | Where the server listens. Plain HTTP is accepted only for `localhost`, `127.0.0.1`, and `::1`; any other host raises `ValueError("nonlocal Laya server requires HTTPS")`. Home Assistant itself listens on 8123, so bind `laya-serve` elsewhere with `LAYA_PORT` and point this at that port. |
| `laya_endpoint_path` | `/v1/systemone` | The route `laya-serve` exposes. |
| `laya_model` | `convaiinnovations/laya` | Asks the server to choose a checkpoint from the script and language of the state. `english`, `multilingual`, and `typed-decisions` name a checkpoint directly. Any other value, including a Jev model id, is ignored by the server and auto-routes. |
| `LAYA_API_KEY` | unset | Forwarded only when the server was started with `LAYA_API_KEY`. Without it the request carries no `Authorization` header at all. |
| timeout | 120 s | CPU inference takes seconds per call and longer on a cold process. The hosted 30 s default is not used on this route. |

`laya_endpoint(base_url, endpoint_path)` returns the resolved URL, and raises `ValueError` for a malformed URL, a path that does not start with `/`, or cleartext HTTP to a non-loopback host.

### Laya then hosted route

`ChainedJev` holds an ordered list of named adapters and returns the answer from the first one that answers. On `local_with_api_fallback` the order is `laya` first and then every configured hosted provider; on `api_with_local_fallback` it is the configured hosted providers first and `laya` behind them; on `clef_with_jev_fallback` it is `clef` first and then the configured OpenRouter hop. So a review costs no provider request while the first hop answers:

```json
{
  "outcome": "notify",
  "reason": "notify",
  "confidence": 0.60675,
  "action": "light.turn_off",
  "shadow": true,
  "raw": {
    "model": "laya-rl-agent",
    "provider": "laya",
    "attempted": ["laya"]
  }
}
```

`raw.provider` names the hop that answered and `raw.attempted` lists the hops that were tried, in order. The single-provider routes are unchanged by this: their `raw` still carries only `{"model": ...}`.

A fallback happens only on a trigger, all of them covered by tests:

| Trigger from the earlier hop | Falls through |
|---|---|
| Transport error, an OSError or `URLError` such as connection refused or a DNS failure | yes |
| Timeout | yes |
| HTTP 401, 403, 429 | yes |
| HTTP 5xx | yes |
| HTTP 400, 404, 422 | no, the error propagates |
| A malformed or unparseable answer | no, the error propagates |

`is_fallback_trigger(exc)` implements that rule, and `FALLBACK_STATUS_CODES` is `frozenset({401, 403, 429})`; a 5xx is matched by range. The rule is the same for every hop: a hosted hop that is refused, rate limited, or unreachable falls through to the next hop, which is what makes `clef_then_jev` a chain rather than a single provider.

The route carries no cooldown and no retry: each hop is tried once, in order, and the error from the last hop propagates when every hop fails. This repository has never had a cooldown, and none was added here, so the timing behaviour of the pre-existing routes is unchanged.

### Local failure circuit breaker

A repeated local outage must not turn every household case into remote traffic, so the local hop is guarded by a consecutive-failure breaker:

| Rule | Behaviour |
|---|---|
| A local failure that qualifies for the fallback | Increments the counter, and `note_local_failure` logs the exception class name. |
| Counter at or below `LOCAL_FALLBACK_FAILURE_LIMIT`, which is `3` | The hosted fallback is attempted. |
| Counter past the limit | The fallback is suppressed, a warning is logged, and the local error is re-raised. No hosted request is made. |
| Any successful local call | Resets the counter to zero, on the chained route and on the local-only route alike. |

The counter is process state, not case state. It is per local hop, not per process: it is keyed by the endpoint the local provider was pointed at, so two config entries that point at two different local servers have two independent breakers, and one entry's dead local server no longer suppresses the other entry's hosted fallback. `local_failure_count()` with no scope reports the total across every live hop as a diagnostic; `local_failure_count(scope)` reports what the breaker actually compares against the limit. `reset_local_failures()` clears every hop, and `reset_local_failures(scope)` clears one.

The cooldown is unchanged and applies per hop: after `LOCAL_FALLBACK_COOLDOWN_SECONDS` without a new failure, that hop's count returns to zero, so a local server that comes back is retried rather than suppressed for the life of the process. Both copies of the provider rules carry the same five names, and the drift test asserts the same limit and the same trip behaviour in each.

The breaker guards the local hop only. It is not applied to a hosted hop leading to another hosted provider, so `clef_with_jev_fallback` falls through on every qualifying failure rather than after three. A hosted provider that is persistently down therefore costs one failed request per hop per review, which is the cost the plain hosted route already carries.

A local failure that is not a fallback trigger, such as an HTTP 422 for an invalid request, never reaches the breaker: it propagates immediately, because a request the local server rejected as invalid is not retried somewhere else and therefore creates no remote egress to bound.

The breaker bounds repeated remote egress. It cannot detect a valid yet incorrect local judgment: a wrong but well-formed local answer is a success, so it is returned and it resets the counter.

Each fallback mode fails fast when it has no usable provider for the side it promises. `provider_order("local_with_api_fallback")` raises `ValueError("missing OPENROUTER_API_KEY for the local_with_api_fallback mode")` and `provider_order("api_with_local_fallback")` raises the same for its own mode, and `build_provider` passes the error through rather than returning a chain with a single hop. The Clef-pinned chain fails fast on the same reasoning: it raises when Clef is not configured, and raises `ValueError("no configured hosted provider behind Clef for the clef_then_jev route")` when nothing is configured behind it, rather than degrading into a Clef-only route under another name. A chain built directly with fewer than two adapters raises `ValueError("a chain needs at least two providers")`.

**Privacy.** On this route, a failed local attempt sends the redacted case to a hosted API. The redaction runs before the first attempt, in `SentinelWorkflow.review`, so the local hop and the hosted hop receive the same redacted case and credential-shaped fields never leave the machine. The rest of the case does leave the machine whenever the local server fails. A case that must never leave the machine belongs on the `laya` route, which has no fallback.

Both copies of this rule, in `sentinel/jev.py` and in `custom_components/jev_sentinel/runtime.py`, are asserted equal by the drift test in `tests/test_laya_provider.py`.

### Confidence scale

The `confidence` question is a `score` question, and a score rubric is an ordered list of level descriptions with index 0 first. The previous `{"min": 0, "max": 1}` mapping is not a valid score rubric: a local Laya server rejects it with

```text
question 'confidence': a score question takes 'criteria' as a list of level descriptions, index 0 first
```

The shipped rubric has five levels:

```text
0  very low confidence, the case is ambiguous or mostly missing
1  low confidence
2  moderate confidence
3  high confidence
4  very high confidence, the case points one way
```

A `score` answer reports the expected level on the legend index scale, `0` to `len(legend) - 1`, next to the `legend` naming each level. `confidence_from_score(answer)` rescales that index onto `[0, 1]`:

```text
confidence = score / (len(legend) - 1), clamped to [0, 1]
```

Properties, all covered by tests:

| Input | Result |
|---|---|
| legend index `0` | `0.0` |
| legend index `4`, the last of five | `1.0` |
| legend index `2.011` | `0.50275` |
| index above the last level | clamped to `1.0` |
| index below `0` | clamped to `0.0` |
| missing, non-numeric, boolean, or single level answer | `null` |

`confidence_from_score` clamps, because a caller that only wants a number is better served by a bounded one. Validation does not clamp. `validate_score_answer(answer)` reads the same index and the same legend through the same helpers and raises `ValueError` when the index falls outside `0` to `len(legend) - 1`, so a provider cannot widen the scale by reporting a position that does not exist on the legend it sent. `validate_answers(answers, questions, label=...)` is the one typed answer check every route shares: it rejects a missing answer, a `choice` outside the rubric's own criteria, a `score` index outside the legend scale, and a `noul` probability outside `[0, 1]`.

The result is a quantized ordinal estimate, not a calibrated probability. Adjacent levels sit `1 / (levels - 1)` apart, which is `0.25` on the shipped five level rubric. The rescale is linear, so the raw index stays recoverable by multiplying the returned value by `levels - 1`. `Decision.raw` still carries only `{"model": ...}`, as before, because the index is recoverable.

Two limits are worth stating plainly:

- No code in this repository compares `confidence` against a threshold. Policy authorization is driven by the action name, so the rubric change cannot move a decision across a boundary. The only consumers are the decision payload and the emitted event.
- The hosted endpoint's acceptance of the five level list rubric, and its own `score` scale, were not verified in this version because no hosted API key was available. The mapping above is applied on both routes so the meaning of `confidence` is identical, but a hosted review should be run once to confirm the hosted scale.

The hosted OpenRouter endpoint's acceptance of the five level list rubric, and its own `score` scale, were not verified in this version because no hosted API key was available. That limit applies to the Clef route for the same reason.

The quality evidence for the local route is the DOGA fork's 100-question, three-mode benchmark, run against DOGA v1.2.0 behaviour. On 100 authored, subjective labels it measured goal agreement of 56/100 for Laya local against 88/100 for Jev, mode 41 against 68, stakes 37 against 67, and high-versus-low ambiguity at the 0.7 threshold 67 against 87, with none of the 30 authored high-ambiguity labels detected by Laya at 0.7 against 21 of 30 for Jev. Those labels are subjective and predate the Laya comparison, so the numbers are an agreement study over one authored set, not a population accuracy estimate. Do not use them as a reason to lower the threshold: doing so on that set trades missed high ambiguity for false alarms.

## Policy check engine

The core `Policy` allowlist is:

```text
notify
ask_user
light.turn_on
light.turn_off
switch.turn_on
switch.turn_off
climate.set_temperature
```

The approval set is:

```text
lock.unlock
alarm_control_panel.alarm_disarm
cover.open_garage
water_valve.close
```

`authorize` returns `allowed: false` with `status: no_action` for no action, `not_allowlisted` for an unknown action, and `approval_required` for an approval-set action without `user_approved=True`. An allowlisted action or approved sensitive action returns `allowed: true` and `status: approved`.

The Home Assistant runtime has a smaller `Policy` class used only by the standalone bridge, with the reversible allowlist and no approval-set execution path. The Home Assistant service handler does not call that policy or dispatch a device action. This is a source boundary, not an implied active mode.

## Readback verification

The deterministic function is:

```python
verify(expected, actual, *, available=True)
```

| Condition | `status` | `verified` | `next_step` |
|---|---|---:|---|
| `available` is false | `unavailable` | false | `notify_and_retry` |
| `actual == expected` | `matched` | true | `close_case` |
| otherwise | `mismatch` | false | `reopen_case` |

The function compares supplied values. It does not read an entity, poll, retry, or infer a value from a command response.

## Home Assistant services

A Home Assistant service is registered once per domain, not once per config
entry, so the integration registers each service **once** and resolves which
entry a call is for on every call. Both handlers read `hass.data[jev_sentinel]`
at call time, so no handler is ever bound to one entry.

### `jev_sentinel.review`

| Field | Required | Selector | Meaning |
|---|---:|---|---|
| `entry_id` | no | text | The config entry whose provider answers. Optional while exactly one entry is loaded, required with more than one. |
| `event_type` | yes | text | Case event name. |
| `area` | no | text | Area label. |
| `entities` | no | entity, multiple | Zero or more Home Assistant entity identifiers. |
| `facts` | no | object | Bounded case facts. |
| `requested_action` | no | text | Requested action label. |

**Entry selection.** With one entry loaded, a call that names no `entry_id` is
answered by it. With several, a call that names none is refused rather than
answered from whichever entry happens to sit first: Home Assistant registers one
handler per domain, so there is no per-entry handler to address, and an operator
who added a `local_only` entry beside a hosted one must not have household cases
routed over the first entry's OpenRouter key. The refusal fires a
`jev_sentinel_decision` event with `outcome: "unavailable"`, which moves the
status sensor off `ready`.

**Failure.** A provider that cannot be built — a misconfigured mode, a missing
key, a `laya_base_url` the policy refuses — no longer escapes the handler. The
call completes, a `jev_sentinel_decision` event is fired with
`outcome: "error"`, `error_type`, and `reason`, and the status sensor reads
`error`. Before this, the exception escaped, no event was fired, and the sensor
kept the previous value, which on a fresh install is `ready`.

The handler calls the configured provider in an executor job and fires
`jev_sentinel_decision` with `case`, `decision`, and `entry_id` data.

### `jev_sentinel.verify`

| Field | Required | Selector | Meaning |
|---|---:|---|---|
| `entry_id` | no | text | The config entry this comparison belongs to. |
| `expected` | yes | text | Expected value supplied by the caller. |
| `actual` | yes | text | Observed value supplied by the caller. |
| `available` | no | boolean | Defaults to true. |

**This is a caller-attested comparison, not a readback.** The service compares
the two values it is given and fires `jev_sentinel_verification`; it does not
read an entity, a state machine, or any other Home Assistant source. Its name
changed from "Record a verification comparison" to spell that out, because the
old name read as though the service verified something. A readback backed by
real entity state is the caller's automation, which is what
`docs/integrations.md` describes.

The `expected` and `actual` fields are plain text selectors, so a caller may put
any string in either. That is deliberate and is the reason the event carries no
more authority than the comparison itself: nothing here is tied to real entity
state, so a `verified: true` in this event attests the two strings, not the
house.

## Events

`jev_sentinel_decision` contains:

```json
{
  "case": {"case_id": "case_0123456789ab", "event_type": "manual_review"},
  "decision": {"outcome": "notify", "reason": "notify", "shadow": true}
}
```

`jev_sentinel_verification` contains `verified`, `status`, `expected`, `actual`, and `next_step`.

## Status sensor

Each config entry creates `sensor.<entry_name>_status` with unique ID `jev_sentinel_status`. Its initial native value is `ready` until the first event arrives. It listens for `jev_sentinel_decision` and updates to `event.data["decision"]["outcome"]`, falling back to `unknown` when absent, and also exposes `action`, `error_type`, `reason`, and `entry_id` as attributes. It also listens for `jev_sentinel_verification` and updates to that event's `status`.

A failed review fires the same decision event with `outcome: "error"`, so the sensor reads `error` rather than keeping a previous value; a call that named no entry while several were loaded fires `outcome: "unavailable"`.

## The authority boundary

`Policy.authorize(action, entity_id, service_data, *, user_approved=None)`
answers for a whole request, not for an action name, and returns a record of
what it checked:

| Field | Meaning |
|---|---|
| `allowed`, `status`, `reason` | The verdict, as before. |
| `action`, `entity_id`, `checked_at` | What was checked. |
| `approval` | Who approved, when, of what, and until when, or `null`. |
| `field`, `value`, `range` | Present on a `value_out_of_range` refusal. |
| `restricted_by` | Present when the target entity matched a restricted pattern. |

Three scopes are checked, in this order:

1. **Action** — `allowed_actions` needs no human; `approval_required`
   (`lock.unlock`, `alarm_control_panel.alarm_disarm`, `cover.open_garage`,
   `water_valve.close`) needs one every time.
2. **Entity** — an entity whose id carries a segment matching
   `restricted_entity_patterns` (`nursery`, `baby`, `infant`, `child`,
   `blackout`, `incubator`, `medical`, `medication`, `aquarium`, `terrarium`,
   `vivarium`, `freezer`, `fridge`, `refrigerator`) needs an approval too, so
   `light.turn_on` on `light.living_room` is allowed and the same action on
   `light.nursery_blackout` is not. The match is on whole segments, split on
   every separator at once, so `blackout` is not reached by `out`. A caller may
   replace the set; an empty set disables the check.
3. **Service data** — `value_ranges` bounds the parameters of an action. A
   `climate.set_temperature` whose `temperature`, `target_temp_high`, or
   `target_temp_low` falls outside 5 to 30 is refused with
   `value_out_of_range`. This check runs *before* any approval, because an
   out-of-range value names a request nobody should make and an approval does
   not make it one somebody should.

**Approvals carry provenance.** `user_approved` accepts an `Approval` record
(`by`, `at`, `scope`, `expires_at`), a mapping with those keys, or a bare
boolean for backwards compatibility. `scope` is an action name, an entity id, or
`action@entity`, and an approval that does not name the request it is offered
for is refused as `approval_out_of_scope`; `expires_at` produces
`approval_expired` once it passes, and an unparseable expiry is not a permissive
one. A bare `True` still works and is recorded as an approval by `"caller"`.

`SentinelWorkflow.execute` reads `entity_id` and `service_data` out of
`case.facts`, falling back to `decision.raw`, and passes them to the policy. The
shipped Home Assistant bridge carries the same `execute`, which it used to lack
entirely.

## Configuration

The config flow stores `provider`, and the fields for the selected route:

| Field | Required | Default | Meaning |
|---|---:|---|---|
| `provider` | yes | `openrouter` | The decision mode. `api_only` for the hosted API alone, `local_only` for the local model alone, `api_with_local_fallback` for the hosted API first, `local_with_api_fallback` for the local model first. Every earlier name also selects the same routing: `openrouter`, `jev_api`, `clef`, and `clef_api` are `api_only`; `laya` and `laya_local` are `local_only`; `laya_then_hosted`, `laya_with_jev_fallback`, `clef_then_jev`, and `clef_with_jev_fallback` are `local_with_api_fallback`; `clef_with_local_fallback` is `api_with_local_fallback` with Clef leading; `auto` is the configured hosted providers, adding the local slot only when `local_model` is set. See [the alias table](#the-alias-table). The stored value is not rewritten. |
| `clef_model` | no | `clef` | The Clef checkpoint: `clef` or `clef-flash`. Applies to the `clef` and `clef_then_jev` routes and is ignored on the others. An unknown value raises `ValueError("invalid Clef checkpoint: ...")`; a stored blank value falls back to the default. |
| `api_key` | for every mode that reaches hosted Jev | empty | The OpenRouter key. Selecting a mode that reaches hosted Jev with an empty key returns the `api_key_required` error. The check runs against the **resolved** canonical mode, not the stored spelling, so `openrouter`, `jev_api`, `laya_then_hosted`, `laya_with_jev_fallback`, and `clef_with_jev_fallback` all require a key, while `local_only`, `laya`, `laya_local`, `clef`, `clef_api`, and `clef_with_local_fallback` do not: Clef reads its own credential from the environment and never receives the stored OpenRouter key. Reading the stored name instead of the mode used to let `laya_with_jev_fallback` and `laya_then_hosted` save an entry with no key, and every review on it then raised. |
| `laya_base_url` | no | `http://127.0.0.1:8000` | The local decision-model server URL, used on `local_only` and on the local hop of both fallback modes. Pointing it at another engine's server is a configuration change, not a code change. The host must be loopback or a private address on the household's own network, with HTTPS required for anything that is not loopback; a public remote host is refused, because the local slot carries area, entity ids, and case facts. A URL that already carries a path is refused rather than joined, which used to produce a doubled `/v1/systemone/v1/systemone`. The form reports both as `laya_base_url_invalid`. |
| `local_model` | no | `laya` | Which local System One decision model answers. Free text, not a list: any local model publishing the same `/v1/systemone` request shape fits, and a model published after this release works by naming it. Rejected only when empty, whitespace-only, not text, or carrying a character that would corrupt a URL path segment or a JSON string, with `ValueError("invalid local_model: ...")` at build time and the `local_model_invalid` error in the form. Known to fit: `laya`, `laya-multilingual`, `laya-typed-decisions`, and any other local pre-deterministic routing model serving that endpoint. **Takes precedence over `laya_model` when it is set.** |
| `laya_model` | no | `convaiinnovations/laya` | **Deprecated.** The per-model setting kept for backwards compatibility. It is the older spelling of `local_model` and still works on its own, so an entry that stored only `laya_model` keeps calling that checkpoint. When both are present, `local_model` wins. |

An existing config entry created before v1.1.0 has no `provider` field and keeps the hosted route, so no migration is required. An entry created before v1.3.0 has no `clef_model` field, which means the default checkpoint, so it needs none either. An entry created before v1.4.0 has no `local_model` field, which means `laya`, so it needs none either, and any `provider` value it stored still resolves to the same routing. The options flow accepts optional boolean `shadow`, default `true`. The Home Assistant adapter remains shadow-only regardless of that option.

## Provider-neutral core

The public package exports `Case`, `Decision`, `Policy`, `SentinelWorkflow`, `Verification`, `OpenRouterJev`, `ClefJev`, `LayaJev`, `ChainedJev`, `build_provider`, `provider_order`, `validate_fallback_order`, `is_fallback_trigger`, `confidence_from_score`, `decision_questions`, `laya_endpoint`, `resolve_provider`, `provider_mode`, `resolve_auto_mode`, `local_model_configured`, `local_checkpoint`, `accepted_provider_names`, `pins_clef`, `local_failure_count`, `reset_local_failures`, `note_local_failure`, `clef_answers`, `clef_checkpoint`, `clef_credentials`, `clef_endpoint`, `clef_question_ids`, `score_index`, `validate_answers`, `validate_score_answer`, and the constants `API_WITH_LOCAL_FALLBACK`, `API_ONLY`, `LOCAL_ONLY`, `LOCAL_WITH_API_FALLBACK`, `CANONICAL_MODES`, `FALLBACK_MODES`, `SINGLE_PROVIDER_MODES`, `CLEF_PINNED_NAMES`, `CASE_INSENSITIVE_ALIASES`, `CHAINED_PROVIDER`, `CLEF_CHAINED_PROVIDER`, `CLEF_CHECKPOINT_PROVIDER`, `CLEF_API_BASE`, `CLEF_RUN_PATH`, `CLEF_DEFAULT_MODEL`, `CLEF_MODELS`, `CLEF_ID_MAX_LENGTH`, `CLEF_MAX_QUESTIONS`, `CLEF_TIMEOUT`, `CLEF_TOKEN_ENV`, `CLEF_ACCOUNT_ENV`, `DEFAULT_FALLBACK_ORDER`, `HOSTED_PROVIDERS`, `LOCAL_PROVIDER`, `LOCAL_PROVIDERS`, `LOCAL_MODEL`, `LOCAL_MODEL_FIELD`, `LOCAL_FALLBACK_FAILURE_LIMIT`, `MODE_ALIASES`, `MODE_NAMES`, `PROVIDER_MODES`, `PROVIDER_NAMES`, `PROVIDER_ENV`, `PROVIDER_REQUIRED_ENV`, and `FALLBACK_STATUS_CODES`. A provider implements:

```python
class DecisionProvider(Protocol):
    def decide(self, state: dict[str, Any]) -> Decision: ...
```

`review` passes a redacted case and sorted allowed actions to the provider. `execute` rejects shadow decisions, authorizes non-shadow actions, calls `dispatch(action)` when allowed, catches dispatch failures, calls `readback()`, and records verification. It never hides an authorization or readback failure behind a successful dispatch record.

`custom_components/jev_sentinel/runtime.py` is a self-contained copy of the adapter, rubric, mode selection, redaction, policy, workflow, and verification contracts, because Home Assistant installs `custom_components` without installing the package. `tests/test_laya_provider.py` and `tests/test_clef_provider.py` assert that both copies produce the same rubric, the same mode resolutions and provider orders, the same confidence mapping, the same local-model acceptance and refusal, and the same Clef endpoint, id mapping, and failure messages, so the two cannot drift silently.

### Credential resolution

A credential that is supplied explicitly wins over the process environment, **including a value that is present but empty**: an empty credential is a missing credential, so it is never topped up from `os.environ`. `None` means "read the environment", which is what a caller with no value of its own passes. This holds for `ClefJev`, `LayaJev`, and `OpenRouterJev`, and it is what stops a test that passes an empty credential from silently borrowing the machine's real key and sending it somewhere.

## Limitations

- v1.4.0 has no active Home Assistant dispatch consumer.
- The integration does not poll entities or implement delayed readback.
- Four decision modes ship: `api_only`, `local_only`, `api_with_local_fallback`, and `local_with_api_fallback`, each also addressable by the aliases in [the alias table](#the-alias-table). Two hosted providers back the API side: hosted Jev over an OpenRouter key and Cloudflare Clef over the environment. The Jev hop is OpenRouter, which serves the TypeSafe `typesafe/jev-1.13` Jev model. A separate direct TypeSafe endpoint is not implemented, and no name outside the alias table is accepted.
- **No local model other than the default has been called live.** The only verified live behaviour is the Laya route recorded in `docs/release-notes.md` under v1.1.0, v1.2.0, and v1.2.1, captured against a real `laya-serve` on 2026-09-26. `local_model` is proved with an injected transport and a payload capture, which shows what the request asks for but says nothing about whether another engine answers that shape. Run one review against the engine you configure and read it as a smoke test.
- **The Clef route has no live evidence.** No Cloudflare credential authorized for Workers AI was available on the verification machine: every candidate token was refused with HTTP 401, so no real Clef request was ever made. The route is covered by unit tests with an injected transport only. Its request shape, both response envelopes, the error codes, and the id mapping come from Cloudflare's published model documentation rather than from an observed reply. Run one review before relying on it, and treat a first review as a smoke test rather than as proof of parity with hosted Jev.
- The default `fallback_order` is still `("openrouter",)`, so the `auto` route does not select Clef until a caller names it there. Clef ships available by configuration, never selected by default order.
- The local failure breaker bounds repeated remote egress on the chained route only, and it cannot detect a valid yet incorrect local judgment. Its counter is per process, per copy, and resets on restart. Local classification quality is measured by the DOGA benchmark cited under [Confidence scale](#confidence-scale): Laya local agreed with 56 of 100 authored goal labels against 88 for Jev.
- The chained routes' hosted hops are covered by unit tests with an injected transport, not by a live hosted call, because no hosted API key was available on the verification machine. The live evidence for the local-first chain is its local hop.
- The config-flow `shadow` option cannot enable active Home Assistant execution.
- Home Assistant event handlers do not retain a durable decision ledger.
- Local scoring quality and the hosted side of the new rubric are unmeasured. See [Confidence scale](#confidence-scale).
