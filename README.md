<div align="center">

# Jev Home Assistant Sentinel

**A safety boundary for AI-assisted Home Assistant decisions.**

[![CI](https://github.com/bojansandhaus/jev-home-assistant-sentinel/actions/workflows/ci.yml/badge.svg)](https://github.com/bojansandhaus/jev-home-assistant-sentinel/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)
[![HACS](https://img.shields.io/badge/HACS-custom-orange.svg)](https://hacs.xyz/)

</div>

Jev Home Assistant Sentinel is a **Home Assistant integration** for **AI-assisted decisions** around physical devices. It sends a bounded case to Jev, applies a deterministic **policy check**, and records **state verification** separately from the command result. This **safety boundary** keeps recommendations advisory and makes the evidence visible.

## What answers the decision step

A **System One decision model**, also written *typed decision model*, is what answers the decision step: a model that returns typed values, each carrying a probability, rather than prose. [TypeSafe coined the category alongside Jev on 15 September 2026](https://systemonemodels.org/guides/what-is-a-system-one-model/), and it is written System 1 as well. **Jev is one member of the category, not the name of it.**

This integration can reach these members:

| Model | Where it runs | Weights | How you select it |
|---|---|---|---|
| **Jev** | hosted, TypeSafe or OpenRouter | closed | `api_only`, or the hosted hop of either fallback mode |
| **Clef** and **Clef Flash** | hosted, Cloudflare Workers AI | closed | `clef`, `clef_then_jev`, `clef_with_local_fallback`, with `clef_model` choosing the checkpoint |
| **Laya** | local, on this machine | open | `local_only`, the local hop of either fallback mode, and the default for `local_model` |
| **Kev** | wherever you host it | open, 0.8B to 27B on Qwen3.5 and Qwen3.8 bases | `local_model: kev` in the local slot |
| **Tev1** | wherever you host it | open, Together AI, Qwen3.5-based | `local_model: tev1` in the local slot |

## The four decision modes

Configuration exposes exactly four decision modes. Each names which side leads and whether the other side is a fallback:

| Mode | Leads | Fallback | Meaning |
|---|---|---|---|
| `api_with_local_fallback` | hosted API | local | Call the hosted API; if it fails on a configured trigger, call the local model. |
| `api_only` | hosted API | none | Call the hosted API and nothing else. A failure is reported, never rerouted. |
| `local_only` | local | none | Call the local model and nothing else. A failure is reported, never rerouted. |
| `local_with_api_fallback` | local | hosted API | Call the local model; if it fails on a configured trigger, call the hosted API. |

The two `_only` modes are single-provider routes: no chain, no fallback, no cooldown list beyond the one provider. The two `_with_..._fallback` modes are two-provider chains and use the existing cooldown, trigger, and breaker machinery unchanged.

**The local slot is generic.** The provider name in configuration stays `laya`, and a new **`local_model`** setting selects which local decision model answers. It defaults to `laya`. The value is the checkpoint or engine name sent to the local server, so a different local model is selected by **configuration alone, with no code change and no new provider name**, and it is deliberately not validated against a list of names. Pointing `laya_base_url` at another engine's server is also a configuration change. System One decision models known to fit the slot, because they publish the same `/v1/systemone` request shape: `laya` (also `laya-multilingual` and `laya-typed-decisions`), `kev` (open weights, 0.8B to 27B on Qwen3.5 and Qwen3.8 bases, also published as `kev-0.8b`), `tev1` ([Together AI](https://github.com/togethercomputer/tev1), Qwen3.5-based, open weights; `Tev1-4B` and `Tev1-0.8B`), and `jeff-qwen3.5-0.8b` and `jeff-gemma4-e2b`. See [chaitin/Decis](https://github.com/chaitin/Decis), which serves Laya, Kev, and the jeff family behind one `/v1/systemone` endpoint, one Docker image per engine, and where swapping `base_url` is the whole migration.

**Which hosted model answers the API side** is still its own choice and is not renamed: hosted Jev over an OpenRouter API key, or [Cloudflare Clef](https://developers.cloudflare.com/workers-ai/models/clef/) over a Workers AI account and API token read from the environment. Both remain individually selectable and remain the members of the hosted fallback order.

**Every name earlier releases used still works.** `jev_api`, `laya_local`, `laya_with_jev_fallback`, `clef_api`, and `clef_with_jev_fallback` are kept as aliases, as are the canonical route names `openrouter`, `clef`, `laya`, `laya_then_hosted`, and `clef_then_jev`, so an existing config entry keeps the routing it stored. An alias resolves onto its canonical mode before it reaches a chain, a diagnostic, a log line, or a URL, so no alias string ever appears in observable output. The full table is in [the reference](docs/reference.md).

**Clef ships two checkpoints of one model**, `clef` and `clef-flash`. The checkpoint is a setting of the route, selected by the `clef_model` field, not a second provider name.

**Privacy note for the two chained routes.** A failed first attempt sends the redacted case to a hosted API. Redaction runs before the first attempt and the same redacted case is used for every hop, so credential-shaped fields never leave the machine, but the case itself does leave the machine whenever the first provider fails. The single-provider hosted routes always leave the machine, and the plain local route never does.

## Quick Start

1. Add this repository to HACS as a custom integration.
2. Restart Home Assistant and add **Jev Home Assistant Sentinel** from **Settings > Devices & services**.
3. Choose one of the four modes: **`api_only`** (hosted API alone, e.g. Jev over the OpenRouter API key or Cloudflare Clef over the environment), **`local_only`** (a local decision model on this machine, no API key), **`api_with_local_fallback`** (the hosted API first, the local model behind it), or **`local_with_api_fallback`** (the local model first, the hosted API behind it). Set **`local_model`** to the engine you run locally, and **`laya_base_url`** to its server.
4. Call `jev_sentinel.review`. The v1.4.0 integration runs in shadow mode and emits a decision event. It does not dispatch a device action.

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

The config flow asks for one of the four modes, plus the settings that mode needs. `local_model` is free text rather than a list, so a local model published after this release works by naming it.

| Mode | Legacy aliases | What it needs | Notes |
|---|---|---|---|
| `api_only` (default) | `openrouter`, `jev_api`, `clef`, `clef_api` | An OpenRouter API key, or the two Cloudflare variables for Clef | The hosted API alone. Hosted Jev runs at `https://openrouter.ai/api/alpha/decisions` with model `typesafe/jev-1.13`; Clef runs at `https://api.cloudflare.com/client/v4/accounts/<account>/ai/run/@cf/cloudflare/<checkpoint>`, where `clef_model` selects `clef` (default) or `clef-flash`. The key is stored in the config entry and passed to the runtime when `review` runs. |
| `local_only` | `laya`, `laya_local` | A running local decision-model server | No API key and no outbound request. `laya_base_url` defaults to `http://127.0.0.1:8000` and `local_model` to `laya`. |
| `api_with_local_fallback` | `clef_with_local_fallback` | A hosted credential **and** a running local server | The hosted API leads. `review` asks the hosted provider first and, only when that attempt fails, sends the same redacted case to the local server. |
| `local_with_api_fallback` | `laya_then_hosted`, `laya_with_jev_fallback`, `clef_then_jev`, `clef_with_jev_fallback` | A running local server **and** an OpenRouter API key | The local-first chain. `review` asks the local server first and, only when that attempt fails, sends the same redacted case to the hosted provider, up to three consecutive local failures in one process. |

Every alias selects the same routing as the mode it resolves to, so a configuration that worked before v1.4.0 makes the same routing decision after it. The config flow offers the canonical mode first and every alias beneath it, and an entry that stored any earlier name keeps it. An entry created before v1.1.0 has no `provider` field and keeps the hosted route, so no migration is needed.

`clef_then_jev` is a chain between two hosted providers rather than one of the four modes, because both of its hops are hosted. It resolves to `local_with_api_fallback` so an entry storing it keeps routing the same way: Clef first, hosted Jev behind it.

Do not put a key in YAML, Git, issue reports, screenshots, or logs.

**Clef credentials come from the environment, not from this form.** The config flow does not ask for the Cloudflare token or account id, because a Home Assistant config entry is written to disk in plain text. Set both variables in the Home Assistant environment before starting it:

```bash
export CLOUDFLARE_ACCOUNT_ID="your Cloudflare account id"
export CLOUDFLARE_API_TOKEN="a Cloudflare API token with Account > Workers AI > Read"
```

The account id is configuration and the token is the credential, but both are required before a request is made and the failure message names whichever one is missing. A selection that would route to Clef without them is refused before any network call, naming the variables, rather than degrading into another provider. The token needs only Workers AI read access. Clef is a hosted route, so the redacted case leaves the machine on every call, not only on a failure.

`local_only` is a single provider with no fallback, the default mode never selects it on its own initiative, and the hosted provider order accepts only hosted names. Plain HTTP is accepted for `localhost`, `127.0.0.1`, and `::1` only, so household case data never travels over a cleartext remote link. The local route waits up to 120 seconds for a reply, because a CPU checkpoint takes seconds and a cold process takes longer.

The two fallback modes are the only modes that reach more than one provider. `local_with_api_fallback` orders the local server first, then every hosted provider that is configured, which by default is the OpenRouter key. It fails fast with `missing OPENROUTER_API_KEY for the local_with_api_fallback mode` when no hosted provider is configured, because a chain with no fallback behind it would be the local route under another name. `api_with_local_fallback` orders the configured hosted providers first and the local server behind them, and fails fast when no hosted provider is configured, on the same reasoning: a hosted-first mode with nothing to lead with would answer from the local slot under a misleading name. A next hop is tried only on a transport error, a timeout, or an HTTP 401, 403, 429, or 5xx from the hop before it. Any other reply, such as a 400 or 422, propagates unchanged, because a request one provider rejected as invalid would be rejected elsewhere too. The decision records which hop answered: `raw.provider` names it, and `raw.attempted` lists the hops that were tried.

**Circuit breaker.** A local server that stays down must not turn every household case into remote traffic, so the chain whose local hop leads counts consecutive local failures. The first three each fall through to the hosted hop. Past three, the fallback is suppressed, a warning is logged, and the local error is raised instead of the case being answered remotely. A successful local call resets the counter to zero, and the counter is per process, so it clears on restart. Only the exception class name is ever logged: no case content, entity state, answer, account id, or token reaches a log record. The breaker guards the local hop only; the Clef-first chain is bounded by the hosted order rather than by a counter.

**Quality evidence.** The local route's accuracy is measured by the DOGA fork's 100-question, three-mode benchmark. Against 100 authored, subjective labels, Laya local agreed on 56 goals and Jev on 88, on mode 41 against 68, on stakes 37 against 67, and on high-versus-low ambiguity at the 0.7 threshold 67 against 87. Laya detected none of the 30 authored high-ambiguity labels at 0.7. Those labels are subjective and were written before the Laya comparison, so treat this as a classifier agreement study and keep `jev_api` as the default while the local checkpoint is uncalibrated.

**Privacy.** On the two fallback modes, a failed first attempt sends the redacted case to the other side. The redaction runs before the first attempt, so the same redacted case goes to every hop and credential-shaped fields never leave the machine. Everything else in the case does leave the machine when the first provider fails, and on the Clef routes it leaves the machine on every call. If a case must never leave the machine, select `laya` alone. The breaker limits how long a local outage can keep doing that, but it cannot tell that a local answer was wrong.

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

# One of the four modes.
provider = build_provider("api_only", api_key=hosted_key)
provider = build_provider("local_only", local_model="kev")
provider = build_provider("api_with_local_fallback", api_key=hosted_key)
provider = build_provider("local_with_api_fallback", api_key=hosted_key)
# Clef alone. Its token and account id come from CLOUDFLARE_API_TOKEN and
# CLOUDFLARE_ACCOUNT_ID in the environment, never from the config entry.
provider = build_provider("clef_api", clef_model="clef-flash")
decision = SentinelWorkflow(provider).review(case)
```

The core accepts any provider with `decide(state) -> Decision`. Its `execute` method can receive a dispatcher and readback callable, applies `Policy`, and returns separate authorization, dispatch, and verification records.

`build_provider` and `provider_order` are the mode rules: `provider_order("local_only")` returns exactly `["laya"]`, `provider_order("api_only")` returns one configured hosted provider and never the local slot, `provider_order("local_with_api_fallback")` returns `["laya", "openrouter"]` and raises `ValueError("missing OPENROUTER_API_KEY for the local_with_api_fallback mode")` without a hosted key, `provider_order("api_with_local_fallback")` returns `["openrouter", "laya"]`, and `validate_fallback_order` rejects a local member, a mode name, or a `local_model` value.

## Frequently asked questions

### Does this give an AI model control of my house?

No. The Home Assistant review handler only emits a recommendation event. It does not execute a device action. The core policy still requires a caller to dispatch an authorized action.

### What happens if the device is unavailable?

`verify` returns `unavailable`, `verified: false`, and `notify_and_retry`. A successful service-call return does not count as state confirmation.

### Can I use a different decision model than Jev?

The integration ships four modes: `api_only`, `local_only`, `api_with_local_fallback`, and `local_with_api_fallback`. The local side is not bound to Laya: set `local_model` to `kev`, `tev1`, or a `jeff` checkpoint, or to any other System One decision model that publishes the same `/v1/systemone` request shape, and it works by configuration alone with no code change. Point `laya_base_url` at that engine's server. See [chaitin/Decis](https://github.com/chaitin/Decis) for one endpoint serving Laya, Kev, and the jeff family behind one Docker image per engine. The provider-neutral core also accepts another implementation of `DecisionProvider`.

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

Yes. Select `local_only` and run a local decision-model server on the same machine. The local route needs no key and makes no outbound request. The standalone policy and verification code also run without any provider request.

```bash
python -m pip install laya
LAYA_HOST=127.0.0.1 LAYA_PORT=8000 LAYA_MODELS=english laya-serve
```

Home Assistant listens on 8123 itself, so bind the server to another port and set `laya_base_url` to match. To run a different engine instead, start that engine's server and set `local_model` to its engine or checkpoint name. For example, with a Decis image serving Kev:

```yaml
provider: local_only
laya_base_url: http://127.0.0.1:8000
local_model: kev
```

No code change is needed, and `local_model` is not restricted to a list of names.

### Is there a local-only mode?

Yes, since v1.1.0, and it replaces the hosted route rather than joining it: `provider_order("local_only")` returns exactly one provider, and the default never selects the local one on its own initiative. Since v1.4.0 the mode is named `local_only`, with `laya` and `laya_local` kept as aliases, and `local_model` selects the engine.

### What happens to my case data when the local server fails?

On a fallback mode, the redacted case goes to the other side when the first attempt fails. On `local_only` there is no fallback, so a local failure is just a failure and the case never leaves the machine. If a case must never leave the machine, use `local_only` alone. Note that `api_with_local_fallback` is a hosted-first mode, so its case leaves the machine on every call while the hosted provider answers; use it only when leaving the machine is acceptable. On every route the redaction runs first, so credential-shaped fields never leave the machine at all.

## Repository topics

This repository carries the topic tags that name the same taxonomy as the table above, so the tags and these docs agree:

`ai`, `clef`, `cloudflare`, `custom-integration`, `decision-model`, `hacs`, `hacs-integration`, `home-assistant`, `jev`, `kev`, `laya`, `openrouter`, `safety`, `sentinel`, `system-one`, `tev1`.

`system-one` and `decision-model` are the category, `jev`, `clef`, `kev`, `laya`, and `tev1` are members of it, and `cloudflare` and `openrouter` name where two of them are hosted.

## Documentation and links

- [Technical reference](docs/reference.md)
- [Integration guide](docs/integrations.md)
- [FAQ and troubleshooting](docs/faq.md)
- [v1.4.0 release notes](docs/release-notes.md)
- [Changelog](CHANGELOG.md)
- [Contributing](CONTRIBUTING.md)
- [MIT license](LICENSE)
- [Jev Decisions reference project](https://github.com/bojansandhaus/jev-decisions)
- [What is a System One model](https://systemonemodels.org/guides/what-is-a-system-one-model/)
- [System One ecosystem index](https://systemonemodels.org/)

Other members catalogued in the same index: **CLM** and **GLiNER2.5-Decide** (open weights), plus hosted **d1** (Liquid AI), **Mercury Decide** (Inception, free on OpenRouter), **Solar Decide** (Upstage), **pplx-decider** (Perplexity), **Span-01** (Respan), **Decider 1** (meraGPT), and the **OpenAI Decisions API**.
