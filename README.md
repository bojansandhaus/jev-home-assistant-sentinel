<div align="center">

# Jev Home Assistant Sentinel

**A safety boundary for AI-assisted Home Assistant decisions.**

[![CI](https://github.com/bojansandhaus/jev-home-assistant-sentinel/actions/workflows/ci.yml/badge.svg)](https://github.com/bojansandhaus/jev-home-assistant-sentinel/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)
[![HACS](https://img.shields.io/badge/HACS-custom-orange.svg)](https://hacs.xyz/)

</div>

Jev Home Assistant Sentinel is a **Home Assistant integration** for **AI-assisted decisions** around physical devices. It sends a bounded case to Jev through OpenRouter, applies a deterministic **policy check**, and records **state verification** separately from the command result. This **safety boundary** keeps recommendations advisory and makes the evidence visible.

## Quick Start

1. Add this repository to HACS as a custom integration.
2. Restart Home Assistant and add **Jev Home Assistant Sentinel** from **Settings > Devices & services**.
3. Enter an OpenRouter API key in the config flow.
4. Call `jev_sentinel.review`. The v1.0.0 integration runs in shadow mode and emits a decision event. It does not dispatch a device action.

[![Add to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=bojansandhaus&repository=jev-home-assistant-sentinel&category=integration)

## What is this?

Sentinel is the Home Assistant boundary around a Jev recommendation. It packages the facts you choose, asks for one typed outcome, and exposes the result to a caller that owns approval and execution. The integration includes a provider-neutral Python core for applications that do not run inside Home Assistant.

## Why does it exist?

A service call can return without an exception while the target remains unavailable or in the wrong state. **Command sent** is an observation. **State confirmed** is evidence.

Consider a window left open while heating runs. Sentinel can review the bounded case containing the window event, the climate entity, the elapsed time, and the expected state. A caller can then choose whether to ask the user, dispatch an allowlisted action, and read the climate state back. A delayed or unavailable readback remains uncertain.

## What does it do?

- Builds bounded cases from event type, area, entities, and facts.
- Sends redacted case data to Jev through OpenRouter.
- Returns a typed outcome, action, confidence, and shadow flag.
- Checks actions against an explicit allowlist and approval set in the Python core.
- Separates provider response, authorization, dispatch, and verification records.
- Reports matched, mismatched, unavailable, and failed readbacks.
- Emits `jev_sentinel_decision` and `jev_sentinel_verification` events.
- Exposes a status sensor for the latest decision outcome.

## How does it work?

```text
natural language or sensor event
            ↓
        bounded case
            ↓
   typed Jev recommendation
            ↓
   policy context for a caller
            ↓
   advisory decision event
            ↓
 external approval, dispatch, and readback
            ↓
confirmed, failed, or uncertain result
```

The Home Assistant integration is shadow-only. It stops after review and event emission. It supplies policy context but does not apply authorization, dispatch a Home Assistant service, or read state back. A consumer must own those steps. The standalone `SentinelWorkflow.execute` method exposes the complete provider-neutral contract.

## Installation

### HACS custom repository

Use the quick-add badge above, or open HACS and choose **⋮ > Custom repositories**. Enter:

```text
https://github.com/bojansandhaus/jev-home-assistant-sentinel
```

Choose **Integration**, install it, and restart Home Assistant. Add the integration from **Settings > Devices & services**.

### Manual

Copy `custom_components/jev_sentinel` into the `custom_components` directory of your Home Assistant configuration:

```bash
mkdir -p /config/custom_components
cp -R custom_components/jev_sentinel /config/custom_components/
```

Restart Home Assistant, then add the integration through the UI.

## Configuration

The config flow requires an OpenRouter API key. The key is stored in the Home Assistant config entry and passed to the runtime when `review` runs. Do not put it in YAML, Git, issue reports, screenshots, or logs.

The options flow exposes `shadow`, defaulting to `true`. In the current source, the option is collected but the runtime always returns shadow decisions and the Home Assistant review handler never dispatches actions. Treat this as a documented v1 boundary until a consumer implements active execution.

## Usage

### Review a case

`review` requires `event_type`. `area`, `entities`, `facts`, and `requested_action` are optional.

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
  requested_action: climate.set_temperature
```

The handler creates a case, calls Jev through OpenRouter, and fires `jev_sentinel_decision`. It does not call a Home Assistant device service.

### Record a verification comparison

```yaml
service: jev_sentinel.verify
data:
  expected: "off"
  actual: "off"
  available: true
```

A match emits `jev_sentinel_verification` with `status: matched`, `verified: true`, and `next_step: close_case`. A mismatch reopens the case. An unavailable target emits `status: unavailable` and `next_step: notify_and_retry`.

### Use the Python core

```python
from sentinel import Case, SentinelWorkflow

case = Case.create(
    "lamp_request",
    area="living_room",
    entities=["light.lamp"],
    facts={"expected_state": "off"},
)
decision = SentinelWorkflow(provider).review(case)
```

The core accepts any provider with `decide(state) -> Decision`. Its `execute` method can receive a dispatcher and readback callable, applies `Policy`, and returns separate authorization, dispatch, and verification records.

## Frequently asked questions

### Does this give an AI model control of my house?

No. The Home Assistant review handler only emits a recommendation event. It does not execute a device action. The core policy still requires a caller to dispatch an authorized action.

### What happens if the device is unavailable?

`verify` returns `unavailable`, `verified: false`, and `notify_and_retry`. A successful service-call return does not count as state confirmation.

### Can I use a different decision model than Jev?

The Home Assistant adapter is fixed to the OpenRouter endpoint and `typesafe/jev-1.13`. The provider-neutral core accepts another implementation of `DecisionProvider`.

### Where is my API key stored?

The config flow stores it in the Home Assistant config entry. The provider request uses an authorization header. Redaction masks credential-shaped case fields and matching credential text before provider submission and decision event emission.

### Does it work with automations?

Yes. An automation can call `jev_sentinel.review` or `jev_sentinel.verify` and listen for the resulting event. A separate consumer must decide whether to approve or dispatch anything.

### What if the readback is delayed?

Keep the result uncertain until a later readback supplies the expected value. The current `verify` service compares the values supplied in the call; it does not poll an entity.

### How do I know the action actually happened?

Read the target entity after dispatch and compare its value with the expected state. `status: matched` means the supplied values matched. It does not prove that the caller read the correct entity.

### Can I run this without OpenRouter?

You can use the standalone policy and verification code without a provider request. The Home Assistant `review` service requires a configured OpenRouter key.

### Is there a local-only mode?

There is no local decision model in v1.0.0. Local verification and the provider-neutral core work without OpenRouter, while Jev review does not.

## Documentation and links

- [Technical reference](docs/reference.md)
- [Integration guide](docs/integrations.md)
- [FAQ and troubleshooting](docs/faq.md)
- [v1.0.0 release notes](docs/release-notes.md)
- [Contributing](CONTRIBUTING.md)
- [MIT license](LICENSE)
- [Jev Decisions reference project](https://github.com/bojansandhaus/jev-decisions)
