<div align="center">

# Jev Home Assistant Sentinel

**A safety boundary for AI-assisted Home Assistant decisions.**

[![CI](https://github.com/bojansandhaus/jev-home-assistant-sentinel/actions/workflows/ci.yml/badge.svg)](https://github.com/bojansandhaus/jev-home-assistant-sentinel/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)
[![HACS](https://img.shields.io/badge/HACS-custom-orange.svg)](https://hacs.xyz/)

</div>

Jev Home Assistant Sentinel is a **Home Assistant integration** for **AI-assisted decisions** around physical devices. It sends a bounded case to Jev, applies a deterministic **policy check**, and records **state verification** separately from the command result. This **safety boundary** keeps recommendations advisory and makes the evidence visible.

There are five ways to run the decision step, and each has a name. **`jev_api`** sends the bounded case to OpenRouter over a hosted API key. **`clef_api`** sends it to [Cloudflare Clef](https://developers.cloudflare.com/workers-ai/models/clef/) instead, over a Workers AI account and API token read from the environment. **`laya_local`** scores it on this machine with no API key at all. **`laya_with_jev_fallback`** and **`clef_with_jev_fallback`** are the two explicit opt-in chains: each answers from its first provider and falls through to hosted Jev when that provider fails.

[Laya](https://github.com/NandhaKishorM/laya) is a separate local model, not a hosted Jev endpoint, so selecting it replaces the hosted routes instead of extending them. Clef is the opposite case: it is a hosted decision model that answers the same typed questions in the same shape, so it joins the hosted routes and can lead a chain rather than replacing one.

The wording matches the DOGA fork, which ships the same five arrangements. The canonical route names `openrouter`, `clef`, `laya`, `laya_then_hosted`, and `clef_then_jev` are accepted as aliases, so an existing config entry keeps the route it stored.

**Clef ships two checkpoints of one model**, `clef` and `clef-flash`. The checkpoint is a setting of the route, selected by the `clef_model` field, not a second provider name.

**Privacy note for the two chained routes.** A failed first attempt sends the redacted case to a hosted API. Redaction runs before the first attempt and the same redacted case is used for every hop, so credential-shaped fields never leave the machine, but the case itself does leave the machine whenever the first provider fails. The single-provider hosted routes always leave the machine, and the plain local route never does.

## Quick Start

1. Add this repository to HACS as a custom integration.
2. Restart Home Assistant and add **Jev Home Assistant Sentinel** from **Settings > Devices & services**.
3. Choose a route: **`jev_api`** (Jev over the OpenRouter API key), **`clef_api`** (Cloudflare Clef over the environment), **`laya_local`** (Laya on this machine, no API key) when a local `laya-serve` is running, **`laya_with_jev_fallback`** (Laya first, the hosted key behind it), or **`clef_with_jev_fallback`** (Clef first, hosted Jev behind it).
4. Call `jev_sentinel.review`. The v1.3.0 integration runs in shadow mode and emits a decision event. It does not dispatch a device action.

[![Add to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=bojansandhaus&repository=jev-home-assistant-sentinel&category=integration)

## What is this?

Sentinel is the Home Assistant boundary around a Jev recommendation. It packages the facts you choose, asks for one typed outcome, and exposes the result to a caller that owns approval and execution. The integration includes a provider-neutral Python core for applications that do not run inside Home Assistant.

## Why does it exist?

A service call can return without an exception while the target remains unavailable or in the wrong state. **Command sent** is an observation. **State confirmed** is evidence.

Consider a window left open while heating runs. Sentinel can review the bounded case containing the window event, the climate entity, the elapsed time, and the expected state. A caller can then choose whether to ask the user, dispatch an allowlisted action, and read the climate state back. A delayed or unavailable readback remains uncertain.

## What does it do?

- Builds bounded cases from event type, area, entities, and facts.
- Sends redacted case data to a hosted Jev key, to Cloudflare Clef, to a local Laya server with no key, or to either chain in turn.
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

The config flow asks for one of five providers. The local route and the hosted routes are mutually exclusive: it is Laya locally with no API key at all, or one of the hosted routes. The last two are explicit opt-in chains that use two providers.

| Route | Arrangement name | What it needs | Notes |
|---|---|---|---|
| `openrouter` (default) | `jev_api` | An OpenRouter API key | Hosted Jev at `https://openrouter.ai/api/alpha/decisions`, model `typesafe/jev-1.13`. The key is stored in the Home Assistant config entry and passed to the runtime when `review` runs. |
| `clef` | `clef_api` | `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` in the environment | Cloudflare Clef at `https://api.cloudflare.com/client/v4/accounts/<account>/ai/run/@cf/cloudflare/<checkpoint>`. `clef_model` selects `clef` (default) or `clef-flash`. |
| `laya` | `laya_local` | A running local `laya-serve` | No API key and no outbound request. `laya_base_url` defaults to `http://127.0.0.1:8000` and `laya_model` to `convaiinnovations/laya`. |
| `laya_then_hosted` | `laya_with_jev_fallback` | A running local `laya-serve` **and** an OpenRouter API key | The local-first chain. `review` asks the local server first and, only when that attempt fails, sends the same redacted case to the hosted provider, up to three consecutive local failures in one process. |
| `clef_then_jev` | `clef_with_jev_fallback` | `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` **and** an OpenRouter API key | The Clef-first chain. `review` asks Clef first and, only when that attempt fails, sends the same redacted case to hosted Jev. |

Either spelling selects the same route. The config flow offers the arrangement name next to its canonical alias, and an entry that stored a canonical name keeps it. An entry created before v1.1.0 has no `provider` field and keeps the hosted route, so no migration is needed.

Do not put a key in YAML, Git, issue reports, screenshots, or logs.

**Clef credentials come from the environment, not from this form.** The config flow does not ask for the Cloudflare token or account id, because a Home Assistant config entry is written to disk in plain text. Set both variables in the Home Assistant environment before starting it:

```bash
export CLOUDFLARE_ACCOUNT_ID="your Cloudflare account id"
export CLOUDFLARE_API_TOKEN="a Cloudflare API token with Account > Workers AI > Read"
```

The account id is configuration and the token is the credential, but both are required before a request is made and the failure message names whichever one is missing. A selection that would route to Clef without them is refused before any network call, naming the variables, rather than degrading into another provider. The token needs only Workers AI read access. Clef is a hosted route, so the redacted case leaves the machine on every call, not only on a failure.

The local route replaces the hosted routes rather than joining them. It is a single provider with no fallback, the default `auto` route never selects it, and the hosted provider order accepts only hosted names. Plain HTTP is accepted for `localhost`, `127.0.0.1`, and `::1` only, so household case data never travels over a cleartext remote link. The local route waits up to 120 seconds for a reply, because a CPU checkpoint takes seconds and a cold process takes longer.

The two chained routes are the only routes that reach more than one provider. `laya_then_hosted` orders the local server first, then every hosted provider that is configured, which by default is the OpenRouter key. It fails fast with `missing OPENROUTER_API_KEY for the laya_then_hosted route` when no hosted provider is configured, because a chain with no fallback behind it would be the local route under another name. `clef_then_jev` orders Clef first and hosted Jev behind it, and fails fast when Clef is not configured or when nothing is configured behind it, on the same reasoning. A next hop is tried only on a transport error, a timeout, or an HTTP 401, 403, 429, or 5xx from the hop before it. Any other reply, such as a 400 or 422, propagates unchanged, because a request one provider rejected as invalid would be rejected elsewhere too. The decision records which hop answered: `raw.provider` names it, and `raw.attempted` lists the hops that were tried.

**Circuit breaker.** A local server that stays down must not turn every household case into remote traffic, so the local-first chain counts consecutive local failures. The first three each fall through to the hosted hop. Past three, the fallback is suppressed, a warning is logged, and the local error is raised instead of the case being answered remotely. A successful local call resets the counter to zero, and the counter is per process, so it clears on restart. Only the exception class name is ever logged: no case content, entity state, answer, account id, or token reaches a log record. The breaker guards the local hop only; the Clef-first chain is bounded by the hosted order rather than by a counter.

**Quality evidence.** The local route's accuracy is measured by the DOGA fork's 100-question, three-mode benchmark. Against 100 authored, subjective labels, Laya local agreed on 56 goals and Jev on 88, on mode 41 against 68, on stakes 37 against 67, and on high-versus-low ambiguity at the 0.7 threshold 67 against 87. Laya detected none of the 30 authored high-ambiguity labels at 0.7. Those labels are subjective and were written before the Laya comparison, so treat this as a classifier agreement study and keep `jev_api` as the default while the local checkpoint is uncalibrated.

**Privacy.** On the chained routes, a failed first attempt sends the redacted case to a hosted API. The redaction runs before the first attempt, so the same redacted case goes to every hop and credential-shaped fields never leave the machine. Everything else in the case does leave the machine when the first provider fails, and on the Clef routes it leaves the machine on every call. If a case must never leave the machine, select `laya` alone. The breaker limits how long a local outage can keep doing that, but it cannot tell that a local answer was wrong.

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

# A hosted key, a local laya-serve, Cloudflare Clef, or a chain, by configuration.
provider = build_provider("auto", api_key=hosted_key)
# The explicit chains: the local server first, or Clef first, hosted key behind.
provider = build_provider("laya_then_hosted", api_key=hosted_key)
provider = build_provider("clef_then_jev", api_key=hosted_key)
# Clef alone. Its token and account id come from CLOUDFLARE_API_TOKEN and
# CLOUDFLARE_ACCOUNT_ID in the environment, never from the config entry.
provider = build_provider("clef_api", clef_model="clef-flash")
decision = SentinelWorkflow(provider).review(case)
```

The core accepts any provider with `decide(state) -> Decision`. Its `execute` method can receive a dispatcher and readback callable, applies `Policy`, and returns separate authorization, dispatch, and verification records.

`build_provider` and `provider_order` are the route rules: `provider_order("laya")` returns exactly `["laya"]`, `provider_order("auto")` never returns the local route, `provider_order("laya_then_hosted")` returns `["laya", "openrouter"]` and raises `ValueError("missing OPENROUTER_API_KEY for the laya_then_hosted route")` without a hosted key, `provider_order("clef")` returns `["clef"]` and raises `ValueError("missing CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID")` without both Clef variables, and `validate_fallback_order` rejects a local member or a route name.

## Frequently asked questions

### Does this give an AI model control of my house?

No. The Home Assistant review handler only emits a recommendation event. It does not execute a device action. The core policy still requires a caller to dispatch an authorized action.

### What happens if the device is unavailable?

`verify` returns `unavailable`, `verified: false`, and `notify_and_retry`. A successful service-call return does not count as state confirmation.

### Can I use a different decision model than Jev?

The integration ships five routes. `jev_api` (hosted Jev over the OpenRouter key), `clef_api` (Cloudflare Clef over the environment), and `laya_local` (Laya on this machine with no key) are alternatives. `laya_with_jev_fallback` and `clef_with_jev_fallback` answer from the first provider and fall through to hosted Jev. The provider-neutral core also accepts another implementation of `DecisionProvider`.

### What does the Cloudflare Clef route need?

Two environment variables: `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN`. The account id is configuration and the token is the credential, but both are required, and the error names whichever is missing before any request is made. The token needs `Account > Workers AI > Read`. The config flow does not ask for either, because a config entry is stored on disk in plain text.

Clef ships two checkpoints of the one model: `clef` and `clef-flash`, selected by the `clef_model` field. Both answer the same typed questions, so the checkpoint changes the model and the endpoint, never the shape of the answer.

### What happens if Clef rejects the request?

A Cloudflare error envelope carrying `success: false` raises with Cloudflare's own error code in the message, so a rate limit or a model quota problem is distinguishable from a malformed answer. An HTTP error keeps its status, which is what lets the `clef_with_jev_fallback` chain recognise a refusal or an outage and fall through to hosted Jev. An unknown choice, or a score index outside the answer's own legend scale, is refused before it becomes a decision.

### What happens if the local server stays down?

On `laya_with_jev_fallback`, the first three consecutive local failures each fall through to the hosted key. After that the fallback is suppressed for the rest of the process: the local error is raised, no hosted request is made, and a warning carrying only the exception class name is logged. Bring the local server back and one successful local call resets the counter. Nothing about the case, the entity state, the Cloudflare account, or any token is ever written to a log record.

### How good is the local classifier?

Measured, not assumed: on the DOGA fork's 100-question benchmark, against 100 authored and subjective labels, Laya local agreed on 56 goal labels against Jev's 88, on mode 41 against 68, on stakes 37 against 67, and on ambiguity at the 0.7 threshold 67 against 87, detecting none of the 30 authored high-ambiguity labels. Keep `jev_api` as the default while that checkpoint is uncalibrated, and read the numbers as agreement over one authored set rather than accuracy on your cases.

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
- [v1.3.0 release notes](docs/release-notes.md)
- [Contributing](CONTRIBUTING.md)
- [MIT license](LICENSE)
- [Jev Decisions reference project](https://github.com/bojansandhaus/jev-decisions)
