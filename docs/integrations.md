# Integration guide

Sentinel fits between an event source and a Home Assistant action. Keep the boundaries visible: the event source supplies facts, Jev supplies a recommendation, a caller owns approval, Home Assistant performs a service call, and a caller reads the target state back.

## Home Assistant automations

Call `review` from an automation when a bounded case needs a recommendation.

```yaml
alias: Review an open window while heating
triggers:
  - trigger: state
    entity_id: binary_sensor.living_room_window
    to: "on"
conditions:
  - condition: state
    entity_id: climate.living_room
    state: "heat"
actions:
  - action: jev_sentinel.review
    data:
      event_type: window_open_while_heating
      area: living_room
      entities:
        - binary_sensor.living_room_window
        - climate.living_room
      facts:
        window_open_minutes: 14
        heating_state: heating
        expected_state: off
```

The service emits `jev_sentinel_decision`. The automation can notify a user or hand the event to another consumer. The integration does not execute the recommended action.

For a known observation, use:

```yaml
action: jev_sentinel.verify
data:
  expected: "off"
  actual: "off"
  available: true
```

The service compares the values in the call. It does not fetch `climate.living_room` for you.

## Approval gate pattern

Use three distinct stages:

1. Review the bounded case.
2. Ask for approval when the action is sensitive or ambiguous.
3. Dispatch only after local policy permits it, then read the target state back.

The provider-neutral core expresses the gate:

```python
from sentinel import Case, Decision, SentinelWorkflow

case = Case.create(
    "garage_request",
    entities=["cover.garage_door"],
    facts={"expected_state": "closed"},
)
decision = provider.decide({"case": case.to_dict()})
result = SentinelWorkflow(provider).execute(
    case,
    decision,
    dispatch=dispatch_home_assistant_action,
    readback=read_garage_state,
    user_approved=user_approved,
)
```

The core policy denies unknown actions. The approval set includes `lock.unlock`, `alarm_control_panel.alarm_disarm`, `cover.open_garage`, and `water_valve.close`. The Home Assistant integration itself does not provide this dispatch consumer.

## Node-RED

Node-RED can call Home Assistant services and listen for Home Assistant events. A safe flow is:

```text
Home Assistant event
        ↓
Node-RED service call: jev_sentinel.review
        ↓
Event node: jev_sentinel_decision
        ↓
Human approval or local policy
        ↓
Home Assistant device service
        ↓
Entity state readback
        ↓
Node-RED service call: jev_sentinel.verify
```

Pass only bounded facts into `review`. Keep the API key in the Home Assistant config entry. Node-RED should treat `shadow: true` as advisory and should not interpret a provider response as proof that a device changed.

## Hermes agents

Hermes can be a consumer of Sentinel rather than a dependency of the integration. The repository's runtime bridge is self-contained and does not import Hermes internals.

A Hermes workflow can:

- collect a bounded event and entity snapshot;
- call `jev_sentinel.review` through Home Assistant;
- inspect the decision event;
- ask the user for approval when local rules require it;
- call an allowlisted Home Assistant service;
- read the entity state;
- call `jev_sentinel.verify` with the observed value.

Hermes must report the distinction between command sent and state confirmed. A review event without a later readback is incomplete evidence.

## Other AI agents and the Python core

The `sentinel` package accepts any provider implementing `decide`. It can run without Home Assistant:

```python
class LocalProvider:
    def decide(self, state):
        return Decision(
            outcome="notify",
            reason="Ask the user to inspect the target",
            action="notify",
            shadow=True,
        )
```

The core does not prescribe an agent framework. Supply a dispatcher that calls the target system and a readback function that reads the target system. Preserve the returned `authorization`, `dispatch`, and `verification` records.

## Shadow mode and active mode

### Shadow mode

Shadow mode creates a recommendation and leaves execution to a consumer. This is the behavior shipped by v1.0.0. The Home Assistant review service always follows this boundary.

### Active mode

There is no shipped active Home Assistant mode. A future consumer could implement active execution by applying policy, recording approval, dispatching an allowlisted service, and reading state back. That consumer must define its retry, timeout, entity-targeting, and audit behavior before it is treated as active.

The config flow exposes a `shadow` option, but the adapter remains shadow-only regardless of its value. Do not use that option as evidence that active mode exists.

## Safety and privacy

- Send only the facts required for the case.
- Credential-shaped keys are redacted before the provider request.
- Keep a hosted key in the Home Assistant config entry. The local route stores nothing.
- Prefer the local route when a case must not leave the host. That is `laya` alone: the `laya_then_hosted` chain sends the redacted case to a hosted API whenever the local server fails.
- Treat provider confidence as a recommendation signal, not a sensor reading. On the shipped five level rubric it is an expected level rescaled onto 0 to 1, so adjacent values sit 0.25 apart.
- Never use Sentinel as certified safety equipment, alarm control, emergency automation, or access control.
- Keep approval and Home Assistant permissions authoritative.

## Local Laya route

Set `provider` to `laya`, or to its arrangement name `laya_local`, in the config flow when a `laya-serve` process runs on the same machine. The local route needs no API key, makes no outbound request, and replaces the hosted route rather than joining it: it is a single provider with no fallback, and the default `auto` route never selects it. `laya_base_url` defaults to `http://127.0.0.1:8000`, and plain HTTP is accepted for loopback hosts only.

```bash
python -m pip install laya
LAYA_HOST=127.0.0.1 LAYA_PORT=8000 LAYA_MODELS=english laya-serve
```

The first load takes 25 to 35 seconds, one review takes a few seconds on CPU, and the adapter waits up to 120 seconds. Home Assistant listens on 8123 itself, so bind the server elsewhere and set `laya_base_url` to that port. Keep the server on loopback, because a `laya-serve` started without `LAYA_API_KEY` accepts any request on the host.

## Laya then hosted route

Set `provider` to `laya_then_hosted`, or to its arrangement name `laya_with_jev_fallback`, to answer from the local server first and keep a hosted key behind it. The route needs both a running `laya-serve` and an OpenRouter API key. It is an explicit opt-in: the hosted route and the `laya` route behave exactly as they did before, an existing config entry without a `provider` field keeps the hosted route, and no other route reaches the local server.

The three arrangements, named the way the DOGA fork names them, are `jev_api` for the hosted route, `laya_local` for the local route, and `laya_with_jev_fallback` for this one. The canonical route names `openrouter`, `laya`, and `laya_then_hosted` remain accepted as aliases, and both spellings select the same route.

```yaml
# The chained route in the config flow, either spelling
provider: laya_then_hosted   # or laya_with_jev_fallback
api_key: your-openrouter-key
laya_base_url: http://127.0.0.1:8123
laya_model: english
```

The same shape in the Python core:

```python
from sentinel import Case, SentinelWorkflow, build_provider

provider = build_provider("laya_then_hosted", api_key=hosted_key)
# The arrangement name selects the same route.
provider = build_provider("laya_with_jev_fallback", api_key=hosted_key)
decision = SentinelWorkflow(provider).review(case)
print(decision.raw["provider"], decision.raw["attempted"])
```

A hosted hop is tried only on a transport error, a timeout, or an HTTP 401, 403, 429, or 5xx from the local server. Any other local reply propagates, so a request the local server rejected as invalid is not quietly retried on the hosted side. The decision records the hop that answered in `raw.provider` and the hops that were tried in `raw.attempted`.

### Local failure circuit breaker

A local server that stays down must not send every case to a hosted API for as long as it stays down. The route counts consecutive local failures that qualified for the fallback:

| Consecutive local failures | What happens |
|---|---|
| 1, 2, or 3 | The hosted hop is tried, and the decision records `raw.provider: openrouter`. |
| 4 and beyond | The fallback is suppressed, a warning is logged, and the local error is raised. No hosted request is made. |
| Any successful local call | The counter resets to zero. |

The counter is per process and resets on restart. Only the exception class name is logged, never the case, the entity state, or the answer. The breaker bounds repeated remote egress; it cannot detect a local answer that is well formed but wrong. If a case must never leave the machine, use `laya` alone: on that route the counter still resets on success, but there is no hosted hop to reach.

**Privacy.** On this route a failed local attempt sends the redacted case to a hosted API. Sentinel redacts before the first attempt, so both hops receive the same redacted case and credential-shaped fields never leave the machine. The rest of the case does leave the machine when the local server fails. If a case must not leave the machine, use the `laya` route alone: `laya_then_hosted` is the one route that can reach both.

Without a hosted key the route fails fast with `missing OPENROUTER_API_KEY for the laya_then_hosted route` instead of running as a local-only route under a different name.

### Local classifier quality

The headline evidence for the local route is the DOGA fork's 100-question, three-mode benchmark. Against 100 authored, subjective labels, Laya local agreed with 56 goal labels against 88 for Jev, mode 41 against 68, stakes 37 against 67, and high-versus-low ambiguity at the 0.7 threshold 67 against 87. Laya detected none of the 30 authored high-ambiguity labels at 0.7, against 21 of 30 for Jev. The labels are subjective and predate the Laya comparison, so keep `jev_api` as the default and read the numbers as agreement over one authored set.

## Troubleshooting

| Symptom | Check |
|---|---|
| `review` fails before an event | Confirm the config entry provider matches what is running. On the OpenRouter route, check the key and reachability of `https://openrouter.ai/api/alpha/decisions`. On the Laya route, check that `laya-serve` is up and that `laya_base_url` matches its port. On the `laya_then_hosted` route, check both. |
| `laya_then_hosted` raises `missing OPENROUTER_API_KEY for the laya_then_hosted route` | The chained route has no hosted key to fall back to. Add the key to the config entry; the route never runs as a local-only route under another name. |
| A chained review answered with `raw.provider: openrouter` | The local hop failed and the case went to the hosted provider. Check that `laya-serve` is up and that `laya_base_url` matches its port; `raw.attempted` lists the hops that were tried. |
| The chained route raised the local error instead of answering | The breaker has tripped after three consecutive local failures in this process. Fix the local server; one successful local call resets the counter, and restarting the process clears it. |
| Decision event exists but no device changes | This is expected in v1.2.1. The integration is shadow-only. |
| Verification says `mismatch` | Compare the actual value with the expected value and confirm the readback targeted the correct entity. |
| Verification says `unavailable` | Keep the case open and notify or retry after the target becomes available. |
| Status sensor remains `ready` | Confirm that `jev_sentinel_decision` was emitted and the sensor config entry is loaded. |
| Laya route returns connection refused | The server is not running, or it is bound to another port. Home Assistant uses 8123 itself. |
| Laya route returns 404 with `{"detail":"Not Found"}` | Something other than `laya-serve` answered on that port. |
| Laya route returns 422 naming the score criteria | The server rejected the confidence rubric. v1.1.0 and later send the required ordered list, so recheck the adapter version in use. |
