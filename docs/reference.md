# Technical reference

This reference describes the public contracts implemented in `custom_components/jev_sentinel` and `sentinel`.

## Runtime boundaries

The Home Assistant component creates a case, calls `OpenRouterJev`, and fires events. It does not dispatch a Home Assistant device service. The standalone core adds policy authorization and execution callbacks for an external consumer.

| Boundary | Source contract | Result |
|---|---|---|
| Provider | `OpenRouterJev.decide(state)` | `Decision` |
| Policy | `Policy.authorize(action, user_approved=False)` | authorization dictionary |
| Dispatch | caller-supplied callable | dispatch record |
| Readback | caller-supplied callable or `verify` service values | verification record |

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

The runtime asks for `outcome`, `action`, and `confidence`. Outcomes are constrained by the request to `ignore`, `notify`, `ask_user`, `recommend`, or `escalate`. Actions offered to Jev are `notify`, `ask_user`, `light.turn_off`, `switch.turn_off`, `climate.set_temperature`, and `none`. The runtime converts `none` to `action: null`.

`shadow` is true in the runtime and core defaults. `SentinelWorkflow.execute` refuses to dispatch a shadow decision and returns `authorization.status: shadow_only`. Only a caller-created non-shadow decision can enter the provider-neutral execution path.

## OpenRouter provider

The runtime posts JSON to:

```text
https://openrouter.ai/api/alpha/decisions
```

The model is:

```text
typesafe/jev-1.13
```

The payload has `model`, `state`, and `questions`. The request carries `Authorization: Bearer <key>`, `Content-Type: application/json`, and an `X-Title` header. A missing key or provider error prevents a decision from being emitted.

Before `SentinelWorkflow.review`, the core redacts dictionary keys containing `token`, `password`, `secret`, `api_key`, or `credential`, case-insensitively. Redaction replaces the value with `[REDACTED]` and recurses through dictionaries and lists.

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

Each config entry creates `sensor.<entry_name>_status` with unique ID `jev_sentinel_status`. Its initial native value is `ready`. It listens for `jev_sentinel_decision` and updates to `event.data["decision"]["outcome"]`, falling back to `unknown` when absent. It does not listen for verification events.

## Configuration

The config flow stores a required `api_key` in the config entry. Its options flow accepts optional boolean `shadow`, default `true`. The Home Assistant adapter remains shadow-only regardless of that option. Home Assistant version support is declared by the project target and should be validated against the actual manifest when packaging.

## Provider-neutral core

The public package exports `Case`, `Decision`, `Policy`, and `SentinelWorkflow`. A provider implements:

```python
class DecisionProvider(Protocol):
    def decide(self, state: dict[str, Any]) -> Decision: ...
```

`review` passes a redacted case and sorted allowed actions to the provider. `execute` rejects shadow decisions, authorizes non-shadow actions, calls `dispatch(action)` when allowed, catches dispatch failures, calls `readback()`, and records verification. It never hides an authorization or readback failure behind a successful dispatch record.

## Limitations

- v1.0.0 has no active Home Assistant dispatch consumer.
- The integration does not poll entities or implement delayed readback.
- The OpenRouter adapter is the only shipped Home Assistant provider.
- The config-flow `shadow` option cannot enable active Home Assistant execution.
- Home Assistant event handlers do not retain a durable decision ledger.
