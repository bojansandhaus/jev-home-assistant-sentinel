# Technical reference

This reference describes the public contracts implemented in `custom_components/jev_sentinel` and `sentinel`.

## Runtime boundaries

The Home Assistant component creates a case, calls the configured provider, and fires events. It does not dispatch a Home Assistant device service. The standalone core adds policy authorization and execution callbacks for an external consumer.

| Boundary | Source contract | Result |
|---|---|---|
| Provider | `OpenRouterJev.decide(state)` or `LayaJev.decide(state)` | `Decision` |
| Policy | `Policy.authorize(action, user_approved=False)` | authorization dictionary |
| Dispatch | caller-supplied callable | dispatch record |
| Readback | caller-supplied callable or `verify` service values | verification record |

Both providers answer the same call: they send `{"model": ..., "state": ..., "questions": ...}` and read an `answers` mapping keyed by question id.

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

## Provider routes

There are three routes. Two are alternatives rather than members of one chain, and the third is an explicit opt-in chain that uses both:

1. Hosted Jev, over an OpenRouter API key.
2. Laya, running on this machine, with no API key at all.
3. `laya_then_hosted`: Laya first, then the hosted providers that have a key, as an explicit opt-in.

`provider_order(provider, fallback_order=..., env=...)` resolves a route and returns the providers it may call, in order:

| `provider` | Result | Notes |
|---|---|---|
| `"laya"` | `["laya"]` | Exactly one provider. The local route replaces the hosted route. |
| `"openrouter"` | `["openrouter"]` | Raises `ValueError("missing OPENROUTER_API_KEY")` without a key. |
| `"laya_then_hosted"` | `["laya", "openrouter"]` with a hosted key, and no result without one | Raises `ValueError("missing OPENROUTER_API_KEY for the laya_then_hosted route")` when no hosted provider has a key. |
| `"auto"` (default) | `["openrouter"]` with a key, `[]` without | Never selects the local route on its own initiative. |

`validate_fallback_order(order)` accepts a non-empty, duplicate-free order drawn from the hosted names only. `("openrouter", "laya")` and `("laya",)` both raise `ValueError("invalid fallback_order")`, so the local route cannot be added to the hosted order as a last resort. A route name is rejected there too: `("laya_then_hosted",)` raises the same error, because a chain is not a member of a chain. `provider_order` raises `ValueError("invalid provider")` for any name outside `auto`, `openrouter`, `laya`, and `laya_then_hosted`, which here includes `typesafe`.

`build_provider(...)` returns `LayaJev` for the local route, `OpenRouterJev` for the hosted route, and `ChainedJev` for the chained route. It raises `RuntimeError("no Jev provider is configured...")` when `auto` resolves to no provider. An explicit `api_key` also satisfies the route check, so a caller holding its key in its own configuration does not need it in the environment. On the chained route that key belongs to the hosted hops; the local hop keeps its own optional `LAYA_API_KEY`. On the local route, `api_key` is the optional bearer for a `laya-serve` started with its own check.

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

### Local Laya provider

`LayaJev` points the same payload at a `laya-serve` process on loopback. Laya publishes `POST /v1/systemone` in the same Decisions contract, so the request and response path match the hosted route except for the host, the absence of a credential, and the `model` field, which names a Laya checkpoint instead of a Jev model.

| Setting | Default | Meaning |
|---|---|---|
| `laya_base_url` | `http://127.0.0.1:8000` | Where the server listens. Plain HTTP is accepted only for `localhost`, `127.0.0.1`, and `::1`; any other host raises `ValueError("nonlocal Laya server requires HTTPS")`. Home Assistant itself listens on 8123, so bind `laya-serve` elsewhere with `LAYA_PORT` and point this at that port. |
| `laya_endpoint_path` | `/v1/systemone` | The route `laya-serve` exposes. |
| `laya_model` | `convaiinnovations/laya` | Asks the server to choose a checkpoint from the script and language of the state. `english`, `multilingual`, and `typed-decisions` name a checkpoint directly. Any other value, including a Jev model id, is ignored by the server and auto-routes. |
| `LAYA_API_KEY` | unset | Forwarded only when the server was started with `LAYA_API_KEY`. Without it the request carries no `Authorization` header at all. |
| timeout | 120 s | CPU inference takes seconds per call and longer on a cold process. The hosted 30 s default is not used on this route. |

`laya_endpoint(base_url, endpoint_path)` returns the resolved URL, and raises `ValueError` for a malformed URL, a path that does not start with `/`, or cleartext HTTP to a non-loopback host.

### Laya then hosted route

`ChainedJev` holds an ordered list of named adapters and returns the answer from the first one that answers. On the `laya_then_hosted` route the order is `laya` first and then every hosted provider that has a key, so the review costs no provider request while the local server answers:

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

`raw.provider` names the hop that answered and `raw.attempted` lists the hops that were tried, in order. The plain hosted and plain local routes are unchanged by this: their `raw` still carries only `{"model": ...}`.

A fallback happens only on a trigger, all of them covered by tests:

| Trigger from the earlier hop | Falls through |
|---|---|
| Transport error, an OSError or `URLError` such as connection refused or a DNS failure | yes |
| Timeout | yes |
| HTTP 401, 403, 429 | yes |
| HTTP 5xx | yes |
| HTTP 400, 404, 422 | no, the error propagates |
| A malformed or unparseable answer | no, the error propagates |

`is_fallback_trigger(exc)` implements that rule, and `FALLBACK_STATUS_CODES` is `frozenset({401, 403, 429})`; a 5xx is matched by range. The route carries no cooldown and no retry: each hop is tried once, in order, and the error from the last hop propagates when every hop fails. This repository has never had a cooldown, and none was added here, so the timing behaviour of the two existing routes is unchanged.

The route fails fast when no hosted provider has a key. `provider_order` raises `ValueError("missing OPENROUTER_API_KEY for the laya_then_hosted route")`, and `build_provider` passes that error through rather than returning a chain with a single local hop. A chain built directly with fewer than two adapters raises `ValueError("a chain needs at least two providers")`.

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

The result is a quantized ordinal estimate, not a calibrated probability. Adjacent levels sit `1 / (levels - 1)` apart, which is `0.25` on the shipped five level rubric. The rescale is linear, so the raw index stays recoverable by multiplying the returned value by `levels - 1`. `Decision.raw` still carries only `{"model": ...}`, as before, because the index is recoverable.

Two limits are worth stating plainly:

- No code in this repository compares `confidence` against a threshold. Policy authorization is driven by the action name, so the rubric change cannot move a decision across a boundary. The only consumers are the decision payload and the emitted event.
- The hosted endpoint's acceptance of the five level list rubric, and its own `score` scale, were not verified in this version because no hosted API key was available. The mapping above is applied on both routes so the meaning of `confidence` is identical, but a hosted review should be run once to confirm the hosted scale.

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

The integration registers `jev_sentinel.review` and `jev_sentinel.verify` once per loaded config entry.

### `jev_sentinel.review`

| Field | Required | Selector | Meaning |
|---|---:|---|---|
| `event_type` | yes | text | Case event name. |
| `area` | no | text | Area label. |
| `entities` | no | entity, multiple | Zero or more Home Assistant entity identifiers. |
| `facts` | no | object | Bounded case facts. |
| `requested_action` | no | text | Requested action label. |

The handler calls OpenRouter in an executor job and fires `jev_sentinel_decision` with `case` and `decision` data.

### `jev_sentinel.verify`

| Field | Required | Selector | Meaning |
|---|---:|---|---|
| `expected` | yes | text | Expected value supplied by the caller. |
| `actual` | yes | text | Observed value supplied by the caller. |
| `available` | no | boolean | Defaults to true. |

The handler fires `jev_sentinel_verification` with the verification dictionary.

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

Each config entry creates `sensor.<entry_name>_status` with unique ID `jev_sentinel_status`. Its initial native value is `ready`. It listens for `jev_sentinel_decision` and updates to `event.data["decision"]["outcome"]`, falling back to `unknown` when absent. It also listens for `jev_sentinel_verification` and updates to that event's `status`.

## Configuration

The config flow stores `provider`, and the fields for the selected route:

| Field | Required | Default | Meaning |
|---|---:|---|---|
| `provider` | yes | `openrouter` | `openrouter` for hosted Jev over an API key, `laya` for a local server with no key, `laya_then_hosted` for the local server first with the hosted key as fallback. |
| `api_key` | for `openrouter` and `laya_then_hosted` | empty | The hosted key. Selecting a route that reaches the hosted provider with an empty key returns the `api_key_required` error. |
| `laya_base_url` | no | `http://127.0.0.1:8000` | Local server URL, used on the `laya` route and on the local hop of `laya_then_hosted`. |
| `laya_model` | no | `convaiinnovations/laya` | Laya checkpoint, used on the `laya` route and on the local hop of `laya_then_hosted`. |

An existing config entry created before v1.1.0 has no `provider` field and keeps the hosted route, so no migration is required. The options flow accepts optional boolean `shadow`, default `true`. The Home Assistant adapter remains shadow-only regardless of that option.

## Provider-neutral core

The public package exports `Case`, `Decision`, `Policy`, `SentinelWorkflow`, `Verification`, `OpenRouterJev`, `LayaJev`, `ChainedJev`, `build_provider`, `provider_order`, `validate_fallback_order`, `is_fallback_trigger`, `confidence_from_score`, `decision_questions`, `laya_endpoint`, and the route constants `CHAINED_PROVIDER`, `DEFAULT_FALLBACK_ORDER`, `HOSTED_PROVIDERS`, `LOCAL_PROVIDERS`, and `FALLBACK_STATUS_CODES`. A provider implements:

```python
class DecisionProvider(Protocol):
    def decide(self, state: dict[str, Any]) -> Decision: ...
```

`review` passes a redacted case and sorted allowed actions to the provider. `execute` rejects shadow decisions, authorizes non-shadow actions, calls `dispatch(action)` when allowed, catches dispatch failures, calls `readback()`, and records verification. It never hides an authorization or readback failure behind a successful dispatch record.

`custom_components/jev_sentinel/runtime.py` is a self-contained copy of the adapter, rubric, route selection, redaction, policy, workflow, and verification contracts, because Home Assistant installs `custom_components` without installing the package. `tests/test_laya_provider.py` asserts that both copies produce the same rubric, the same route orders, and the same confidence mapping, so the two cannot drift silently.

## Limitations

- v1.2.0 has no active Home Assistant dispatch consumer.
- The integration does not poll entities or implement delayed readback.
- Three provider routes ship: hosted Jev over an OpenRouter key, local Laya with no key, and the opt-in chain `laya_then_hosted`. The hosted hop is OpenRouter, which serves the TypeSafe `typesafe/jev-1.13` Jev model. A separate direct TypeSafe endpoint is not implemented, and no name outside these four providers is accepted.
- The chained route's hosted hop is covered by unit tests with an injected transport, not by a live hosted call, because no hosted API key was available on the verification machine. The live evidence for that route is its local hop.
- The config-flow `shadow` option cannot enable active Home Assistant execution.
- Home Assistant event handlers do not retain a durable decision ledger.
- Local scoring quality and the hosted side of the new rubric are unmeasured. See [Confidence scale](#confidence-scale).
