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
- Prefer the local route when a case must not leave the host.
- Treat provider confidence as a recommendation signal, not a sensor reading. On the shipped five level rubric it is an expected level rescaled onto 0 to 1, so adjacent values sit 0.25 apart.
- Never use Sentinel as certified safety equipment, alarm control, emergency automation, or access control.
- Keep approval and Home Assistant permissions authoritative.

## Local Laya route

Set `provider` to `laya` in the config flow when a `laya-serve` process runs on the same machine. The local route needs no API key, makes no outbound request, and replaces the hosted route rather than joining it: it is a single provider with no fallback, and the default `auto` route never selects it. `laya_base_url` defaults to `http://127.0.0.1:8000`, and plain HTTP is accepted for loopback hosts only.

```bash
python -m pip install laya
LAYA_HOST=127.0.0.1 LAYA_PORT=8000 LAYA_MODELS=english laya-serve
```

The first load takes 25 to 35 seconds, one review takes a few seconds on CPU, and the adapter waits up to 120 seconds. Home Assistant listens on 8123 itself, so bind the server elsewhere and set `laya_base_url` to that port. Keep the server on loopback, because a `laya-serve` started without `LAYA_API_KEY` accepts any request on the host.

## Troubleshooting

| Symptom | Check |
|---|---|
| `review` fails before an event | Confirm the config entry provider matches what is running. On the OpenRouter route, check the key and reachability of `https://openrouter.ai/api/alpha/decisions`. On the Laya route, check that `laya-serve` is up and that `laya_base_url` matches its port. |
| Decision event exists but no device changes | This is expected in v1.1.0. The integration is shadow-only. |
| Verification says `mismatch` | Compare the actual value with the expected value and confirm the readback targeted the correct entity. |
| Verification says `unavailable` | Keep the case open and notify or retry after the target becomes available. |
| Status sensor remains `ready` | Confirm that `jev_sentinel_decision` was emitted and the sensor config entry is loaded. |
| Laya route returns connection refused | The server is not running, or it is bound to another port. Home Assistant uses 8123 itself. |
| Laya route returns 404 with `{"detail":"Not Found"}` | Something other than `laya-serve` answered on that port. |
| Laya route returns 422 naming the score criteria | The server rejected the confidence rubric. v1.1.0 and later send the required ordered list, so recheck the adapter version in use. |
