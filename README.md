<div align="center">

# Jev Home Assistant Sentinel

**A safety boundary for AI-assisted Home Assistant decisions.**

[![CI](https://github.com/bojansandhaus/jev-home-assistant-sentinel/actions/workflows/ci.yml/badge.svg)](https://github.com/bojansandhaus/jev-home-assistant-sentinel/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)
[![HACS](https://img.shields.io/badge/HACS-custom-orange.svg)](https://hacs.xyz/)

</div>

Jev Home Assistant Sentinel is a **Home Assistant integration** for **AI-assisted decisions** around physical devices. It sends a bounded case to Jev, applies a deterministic **policy check**, and records **state verification** separately from the command result. This **safety boundary** keeps recommendations advisory and makes the evidence visible.

There are three ways to run the decision step. **Jev over a hosted API key** sends the bounded case to OpenRouter. **Laya on this machine** scores it locally with no API key at all. Those two are alternatives rather than members of one chain: [Laya](https://github.com/NandhaKishorM/laya) is a separate local model, not a hosted Jev endpoint, so selecting it replaces the hosted route instead of extending it. **Laya first, with the hosted key as fallback** is the third route, an explicit opt-in that answers from the local server and falls through to the hosted key when the local server fails.

**Privacy note for the third route.** A failed local attempt sends the redacted case to a hosted API. Redaction runs before the first attempt and the same redacted case is used for both hops, so credential-shaped fields never leave the machine, but the case itself does leave the machine whenever the local server fails. The hosted route always leaves the machine, and the plain local route never does.

## Quick Start

1. Add this repository to HACS as a custom integration.
2. Restart Home Assistant and add **Jev Home Assistant Sentinel** from **Settings > Devices & services**.
3. Choose a provider: **Jev over the OpenRouter API key**, **Laya on this machine (no API key)** when a local `laya-serve` is running, or **Laya first, with the hosted key as fallback** when you want the local server tried first.
4. Call `jev_sentinel.review`. The v1.2.0 integration runs in shadow mode and emits a decision event. It does not dispatch a device action.

[![Add to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=bojansandhaus&repository=jev-home-assistant-sentinel&category=integration)

## What is this?

Sentinel is the Home Assistant boundary around a Jev recommendation. It packages the facts you choose, asks for one typed outcome, and exposes the result to a caller that owns approval and execution. The integration includes a provider-neutral Python core for applications that do not run inside Home Assistant.

## Why does it exist?

A service call can return without an exception while the target remains unavailable or in the wrong state. **Command sent** is an observation. **State confirmed** is evidence.

Consider a window left open while heating runs. Sentinel can review the bounded case containing the window event, the climate entity, the elapsed time, and the expected state. A caller can then choose whether to ask the user, dispatch an allowlisted action, and read the climate state back. A delayed or unavailable readback remains uncertain.

## What does it do?

- Builds bounded cases from event type, area, entities, and facts.
- Sends redacted case data to a hosted Jev key, to a local Laya server with no key, or to the local server first with the hosted key as a fallback.
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

The config flow asks for one of three providers. The hosted route and the local route are mutually exclusive: it is Jev over a hosted API key, or Laya locally with no API key at all. The third is an explicit opt-in chain that uses both.

| Provider | What it needs | Notes |
|---|---|---|
| `openrouter` (default) | An OpenRouter API key | Hosted Jev at `https://openrouter.ai/api/alpha/decisions`, model `typesafe/jev-1.13`. The key is stored in the Home Assistant config entry and passed to the runtime when `review` runs. |
| `laya` | A running local `laya-serve` | No API key and no outbound request. `laya_base_url` defaults to `http://127.0.0.1:8000` and `laya_model` to `convaiinnovations/laya`. |
| `laya_then_hosted` | A running local `laya-serve` **and** an OpenRouter API key | The chained route. `review` asks the local server first and, only when that attempt fails, sends the same redacted case to the hosted provider. |

Do not put a key in YAML, Git, issue reports, screenshots, or logs.

The local route replaces the hosted route rather than joining it. It is a single provider with no fallback, the default `auto` route never selects it, and the hosted provider order accepts only hosted names. Plain HTTP is accepted for `localhost`, `127.0.0.1`, and `::1` only, so household case data never travels over a cleartext remote link. The local route waits up to 120 seconds for a reply, because a CPU checkpoint takes seconds and a cold process takes longer.

The chained route is the only route that reaches both. Its order is the local server first, then every hosted provider that has a key, in the hosted order, which today is the OpenRouter key. It fails fast with `missing OPENROUTER_API_KEY for the laya_then_hosted route` when no hosted key is configured, because a chain with no fallback behind it would be the local route under another name. A hosted hop is tried only on a transport error, a timeout, or an HTTP 401, 403, 429, or 5xx from the local server. Any other reply, such as a 400 or 422, propagates unchanged, because a request the local server rejected as invalid would be rejected elsewhere too. The decision records which hop answered: `raw.provider` is `laya` or `openrouter`, and `raw.attempted` lists the hops that were tried.

**Privacy.** On the chained route, a failed local attempt sends the redacted case to a hosted API. The redaction runs before the first attempt, so the same redacted case goes to both hops and credential-shaped fields never leave the machine. Everything else in the case does leave the machine when the local server fails. If a case must never leave the machine, select `laya` alone.

The options flow exposes `shadow`, defaulting to `true`. In the current source, the option is collected but the runtime always returns shadow decisions and the Home Assistant review handler never dispatches actions. Treat this as a documented boundary until a consumer implements active execution.

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

The handler creates a case, calls the configured route, and fires `jev_sentinel_decision`. It does not call a Home Assistant device service.

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
from sentinel import Case, SentinelWorkflow, build_provider

case = Case.create(
    "lamp_request",
    area="living_room",
    entities=["light.lamp"],
    facts={"expected_state": "off"},
)

# A hosted key, a local laya-serve, or the chained route, chosen by configuration.
provider = build_provider("auto", api_key=hosted_key)
# The explicit chain: the local server first, the hosted key behind it.
provider = build_provider("laya_then_hosted", api_key=hosted_key)
decision = SentinelWorkflow(provider).review(case)
```

The core accepts any provider with `decide(state) -> Decision`. Its `execute` method can receive a dispatcher and readback callable, applies `Policy`, and returns separate authorization, dispatch, and verification records.

`build_provider` and `provider_order` are the route rules: `provider_order("laya")` returns exactly `["laya"]`, `provider_order("auto")` never returns the local route, `provider_order("laya_then_hosted")` returns `["laya", "openrouter"]` and raises `ValueError("missing OPENROUTER_API_KEY for the laya_then_hosted route")` without a hosted key, and `validate_fallback_order` rejects a local member or a route name.

## Frequently asked questions

### Does this give an AI model control of my house?

No. The Home Assistant review handler only emits a recommendation event. It does not execute a device action. The core policy still requires a caller to dispatch an authorized action.

### What happens if the device is unavailable?

`verify` returns `unavailable`, `verified: false`, and `notify_and_retry`. A successful service-call return does not count as state confirmation.

### Can I use a different decision model than Jev?

The integration ships three routes. Hosted Jev over the OpenRouter key, and Laya on this machine with no key, are alternatives. The third, `laya_then_hosted`, answers from Laya first and falls through to hosted Jev. The provider-neutral core also accepts another implementation of `DecisionProvider`.

### Where is my API key stored?

The config flow stores it in the Home Assistant config entry. The provider request uses an authorization header. Redaction masks credential-shaped case fields and matching credential text before provider submission and decision event emission.

### Does it work with automations?

Yes. An automation can call `jev_sentinel.review` or `jev_sentinel.verify` and listen for the resulting event. A separate consumer must decide whether to approve or dispatch anything.

### What if the readback is delayed?

Keep the result uncertain until a later readback supplies the expected value. The current `verify` service compares the values supplied in the call; it does not poll an entity.

### How do I know the action actually happened?

Read the target entity after dispatch and compare its value with the expected state. `status: matched` means the supplied values matched. It does not prove that the caller read the correct entity.

### Can I run this without OpenRouter?

Yes. Select the Laya provider and run `laya-serve` on the same machine. The local route needs no key and makes no outbound request. The standalone policy and verification code also run without any provider request.

```bash
python -m pip install laya
LAYA_HOST=127.0.0.1 LAYA_PORT=8000 LAYA_MODELS=english laya-serve
```

Home Assistant listens on 8123 itself, so bind `laya-serve` to another port and set `laya_base_url` to match.

### Is there a local-only mode?

Yes, since v1.1.0, and it replaces the hosted route rather than joining it: `provider_order("laya")` returns exactly one provider, and the default route never selects the local one on its own initiative.

### What happens to my case data when the local server fails?

On the chained route, the redacted case goes to a hosted API on the fallback. On the plain local route there is no fallback, so a local failure is just a failure and the case never leaves the machine. If a case must never leave the machine, use `laya` alone. On every route the redaction runs first, so credential-shaped fields never leave the machine at all.

## Documentation and links

- [Technical reference](docs/reference.md)
- [Integration guide](docs/integrations.md)
- [FAQ and troubleshooting](docs/faq.md)
- [v1.2.0 release notes](docs/release-notes.md)
- [Contributing](CONTRIBUTING.md)
- [MIT license](LICENSE)
- [Jev Decisions reference project](https://github.com/bojansandhaus/jev-decisions)
