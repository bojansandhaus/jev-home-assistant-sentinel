# Jev Home Sentinel

Jev Home Sentinel is a Home Assistant integration and provider neutral Python core for turning natural language or sensor events into **bounded, verified decisions**.

It is the execution layer suggested by the command flow:

```text
User request or Home Assistant event
        ↓
Jev interprets the bounded case
        ↓
Sentinel checks policy
        ↓
Home Assistant dispatches an allowlisted action
        ↓
Sentinel reads the state back
        ↓
confirmed, failed, or uncertain result
```

The screenshot that inspired this project stops at **Command sent**. Sentinel continues to **State confirmed**. It keeps interpretation, authorization, execution, and verification separate.

## What it does

The first release supports:

- Bounded Home Assistant cases
- Typed Jev recommendations through OpenRouter
- Redaction of credentials before provider calls
- An explicit allowlist for reversible actions
- Approval gates for sensitive actions
- Deterministic state readback
- Structured failure and uncertainty results
- Home Assistant services for `review` and `verify`
- A status sensor for the latest Jev outcome

The default behavior is advisory. The `review` service asks Jev for a recommendation and fires an event. It does not execute the recommended device action. Execution is available through the provider neutral core only after the caller supplies an allowlisted dispatcher and a readback function.

## Install the Home Assistant integration

Copy `custom_components/jev_sentinel` into the `custom_components` directory of your Home Assistant configuration, restart Home Assistant, then add **Jev Home Sentinel** from Settings, Devices and services.

The config flow stores the OpenRouter key in the Home Assistant config entry. Do not put a real key in YAML, Git, examples, or issue reports.

## Example service call

```yaml
service: jev_sentinel.review
data:
  event_type: window_open_while_heating
  area: living_room
  entities:
    - climate.living_room
    - binary_sensor.living_room_window
  facts:
    window_open_minutes: 14
    heating_state: heating
    expected_state: off
```

Listen for `jev_sentinel_decision` to receive the structured recommendation. A consumer can then apply its own deterministic policy and user approval rules.

## Python core

```python
from sentinel import Case, Decision, SentinelWorkflow

case = Case.create(
    "lamp_request",
    area="living_room",
    entities=["light.lamp"],
    facts={"expected_state": "off"},
)
workflow = SentinelWorkflow(provider)
decision = workflow.review(case)
result = workflow.execute(
    case,
    decision,
    dispatch=lambda action: home_assistant_call(action),
    readback=lambda: "off",
)
```

A successful dispatch is not treated as success until the readback matches the expected state. A mismatch returns `reopen_case`, not a cheerful false confirmation.

## Safety boundary

- Jev is advisory, not execution authority.
- Unknown actions are denied.
- Sensitive actions need explicit approval.
- Every executed action should have a readback.
- Unavailable devices produce an uncertain result.
- Provider input is bounded and credential values are redacted.
- Shadow mode is the default for the Home Assistant service.

This project does not replace Home Assistant authentication, user permissions, alarm controls, or safety systems.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
pytest
```

The core tests run without a Home Assistant installation. The custom component is tested against Home Assistant's integration test matrix in CI before a public release. This initial checkout intentionally keeps the Home Assistant dependency out of the core package.

## Relationship to Jev Decisions

`jev-decisions` is the general Jev decision layer for Hermes and other agents. This repository is the Home Assistant adapter and closed loop execution boundary. The two projects should remain separate so the Home Assistant integration can be installed without importing private Hermes modules.

Jev is TypeSafe's model, and OpenRouter provides the API used here. This is an independent community project.
