<div align="center">

# Jev Home Assistant Sentinel

**A safety boundary for Home Assistant decisions.**

Jev interprets a bounded case. Sentinel applies policy. Home Assistant performs an allowed action. Sentinel reads the state back.

[![CI](https://github.com/bojansandhaus/jev-home-assistant-sentinel/actions/workflows/ci.yml/badge.svg)](https://github.com/bojansandhaus/jev-home-assistant-sentinel/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

</div>

## The short answer

Jev Home Assistant Sentinel is a Home Assistant integration for AI assisted decisions with explicit policy checks and deterministic state verification.

It can review an event such as a window left open while heating runs, recommend a safe response, and report whether the resulting Home Assistant state actually matches the expectation.

It does not give an AI model unrestricted control of your house.

## Why it exists

The usual command flow ends with a green light and the words **command sent**. Physical systems do not work that way. A service call can succeed while a device remains unavailable, a target can resolve to the wrong entity, and a lock can report an unknown state after the network disappears.

Sentinel carries the command across the gap between intention and evidence:

```text
Natural language or sensor event
              ↓
        Bounded Sentinel case
              ↓
        Typed Jev recommendation
              ↓
       Deterministic policy check
              ↓
     Home Assistant service call
              ↓
         State readback
              ↓
  confirmed, failed, or uncertain result
```

The distinction matters. **Command sent** is an observation. **State confirmed** is evidence.

## What it does

- Sends bounded case state to Jev through OpenRouter.
- Redacts credential shaped fields before a provider request.
- Keeps Jev advisory rather than granting it execution authority.
- Allows only explicitly listed actions.
- Leaves sensitive actions behind an approval gate.
- Separates dispatch success from device state confirmation.
- Reopens the case when readback contradicts the expected state.
- Reports unavailable devices as uncertain rather than successful.
- Exposes Home Assistant services for review and verification.
- Provides a status sensor for the latest decision outcome.
- Includes a provider neutral Python core for agents and local tools.

The Home Assistant review service runs in shadow mode. It creates a typed recommendation and fires an event. It does not silently operate a device.

## Install in Home Assistant

Download or clone this repository. Copy `custom_components/jev_sentinel` into the `custom_components` directory of your Home Assistant configuration:

```text
/config/custom_components/jev_sentinel/
```

Restart Home Assistant. Open **Settings**, **Devices and services**, choose **Add integration**, and search for **Jev Home Assistant Sentinel**.

The config flow stores the OpenRouter key in the Home Assistant config entry. Keep keys out of YAML, Git, screenshots, issue reports, and logs.

The integration carries its small runtime bridge inside the custom component. It does not require a private Hermes installation or a package import from the source checkout.

## Review a case

Call the Home Assistant service with the smallest useful set of facts:

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

The integration emits `jev_sentinel_decision`. A consumer can apply local policy, ask the user for approval, and then dispatch an allowlisted Home Assistant action.

## Verify a result

Use the verification service when a command has a known expected state and a readback value:

```yaml
service: jev_sentinel.verify
data:
  expected: "off"
  actual: "off"
  available: true
```

A matching readback produces `matched` and `close_case`. A mismatch produces `mismatch` and `reopen_case`. An unavailable target produces `unavailable` and `notify_and_retry`.

The service never turns an unavailable device into a success merely because the original command returned without an exception.

## Python core

The core package works without Home Assistant:

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

A successful result contains separate records for the decision, authorization, dispatch, and verification. That structure makes the path inspectable and testable.

## Safety boundary

Jev can recommend. It cannot authorize itself.

Sentinel denies unknown actions, keeps security sensitive operations behind approval, and requires a caller supplied readback for executed actions. Home Assistant authentication, user permissions, alarm controls, device safety rules, and emergency systems remain authoritative.

The default policy allows a small set of reversible light, switch, climate, notification, and user question actions. It does not include arbitrary service calls, door unlocking, alarm disarming, or garage control.

## Who is this for?

**Home Assistant users** who want AI assisted triage without handing an agent the keys to every entity.

**Automation authors** who need a typed recommendation before a workflow acts.

**AI agent developers** who need a provider neutral policy and verification boundary around physical world actions.

**Researchers and maintainers** who want observable outcomes instead of logs that only say a tool returned successfully.

## What is Jev?

Jev is TypeSafe's focused decision model. This project calls Jev through OpenRouter using bounded state and typed questions. Jev supplies a recommendation. It does not supply the final authority for a physical action.

This repository is independent of TypeSafe, OpenRouter, and Hermes. The companion project [jev-decisions](https://github.com/bojansandhaus/jev-decisions) provides the general Jev decision layer for Hermes and other agents. Jev Home Assistant Sentinel supplies the Home Assistant boundary.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest
python -m compileall -q sentinel custom_components tests
```

The core suite runs without a Home Assistant installation. The custom component has a self contained runtime bridge, and its files are syntax checked in the build. Live Home Assistant validation should be performed in a test instance before enabling automation.

## Frequently asked questions

### Does Sentinel control Home Assistant automatically?

No. The review service is advisory and shadow only. A separate caller must apply local policy, approval, dispatch, and verification rules.

### Does Sentinel replace Home Assistant Assist?

No. Assist can remain the natural language front door. Sentinel can sit behind it as the typed decision, policy, and readback boundary.

### Does Sentinel send my whole Home Assistant history to OpenRouter?

No. The integration sends a bounded case. Credential shaped fields are redacted. The caller decides which entity facts belong in that case.

### What happens when a device is unavailable?

The result is uncertain. Sentinel reports the missing evidence and recommends notification or retry rather than claiming success.

### Can I use the core without Home Assistant?

Yes. The `sentinel` package accepts a decision provider, an action dispatcher, and a readback function. Home Assistant is one adapter, not a requirement of the policy contract.

### Is this a security system?

No. It is a decision and verification boundary. Do not use it as a substitute for certified safety equipment, alarm controls, emergency automation, or access control.

## License

MIT. See [LICENSE](LICENSE).
