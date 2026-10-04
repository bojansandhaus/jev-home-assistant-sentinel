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
- Keep a hosted Jev key in the Home Assistant config entry. The local route stores nothing, and the Clef routes store nothing: their token and account id come from the environment.
- Prefer the local route when a case must not leave the host. That is `laya` alone, where Laya is the local System One decision model: both chains send the redacted case to a hosted API on a fallback, and the Clef routes send it there on every call.
- Treat provider confidence as a recommendation signal, not a sensor reading. On the shipped five level rubric it is an expected level rescaled onto 0 to 1, so adjacent values sit 0.25 apart.
- Never use Sentinel as certified safety equipment, alarm control, emergency automation, or access control.
- Keep approval and Home Assistant permissions authoritative.

## Cloudflare Clef route

Set `provider` to `clef`, or to its arrangement name `clef_api`, when you want the decision step to run on Cloudflare's hosted Clef model instead of hosted Jev or a local Laya server. Clef is a separate System One decision model rather than a Jev endpoint: it is a Cloudflare Workers AI model that answers the same typed questions in the same `answers` shape, so it joins the hosted routes rather than replacing one.

```yaml
# The Clef route in the config flow, either spelling
provider: clef              # or clef_api
clef_model: clef            # or clef-flash
```

Clef needs two environment variables, set in the Home Assistant environment before it starts:

```bash
export CLOUDFLARE_ACCOUNT_ID="your Cloudflare account id"
export CLOUDFLARE_API_TOKEN="a Cloudflare API token with Account > Workers AI > Read"
```

The account id is configuration and the token is the credential, but both are required before a request is made, and the error names whichever one is missing. The config flow does not ask for either, because a config entry is stored on disk in plain text. A stored OpenRouter key is never forwarded to Clef.

The same shape in the Python core:

```python
from sentinel import Case, SentinelWorkflow, build_provider

# Both Cloudflare variables come from the environment here too.
provider = build_provider("clef_api", clef_model="clef-flash")
decision = SentinelWorkflow(provider).review(case)
```

Clef ships two checkpoints of the one model, `clef` and `clef-flash`, selected by `clef_model`. The checkpoint changes the endpoint and the model string, not the shape of the answer, so it is a setting of the route rather than a second provider.

The route posts to `https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/@cf/cloudflare/{checkpoint}` and accepts either documented response shape: the bare model output with a top-level `answers` mapping, or Cloudflare's REST envelope with `success: true` and the answers under `result`. A `success: false` envelope raises with Cloudflare's own error code, so a rate limit or a quota problem is distinguishable from a malformed answer. An answer carrying a choice this integration never offered, or a confidence index outside the answer's own legend scale, is refused before it becomes a decision.

Clef accepts question ids built from letters, digits, `_`, `.`, and `-`, up to 100 characters, with at most 64 questions per request. Sentinel rewrites ids like `candidate:aaa` into that alphabet before the request and maps the answer back, so a caller never sees a renamed question.

**Privacy.** Clef is hosted, so the redacted case leaves the machine on every call, not only on a failure. Redaction runs before the request, so credential-shaped fields never leave the machine. Nothing about the case, the entity state, the account id, or the token reaches a log record: on failure only the exception class name is logged.

**This route has no live evidence.** No Cloudflare credential authorized for Workers AI was available on the verification machine, so the Clef tests use an injected transport and the wire details come from Cloudflare's published documentation. Run one review before relying on it.

## Clef then hosted Jev route

Set `provider` to `clef_then_jev`, or to `clef_with_jev_fallback`, to call Clef first and keep hosted Jev behind it. Both resolve to `local_with_api_fallback`: both of that chain's hops are hosted, so it is not one of the four modes, and the Clef lead is preserved from the stored name. The route needs both Clef variables and an OpenRouter API key. It is an explicit opt-in, and like the local-first chain it fails fast rather than degrading: it raises when Clef is not configured, and raises `no configured hosted provider behind Clef for the clef_then_jev route` when nothing is configured behind it.

```yaml
# The Clef-first chain in the config flow, either spelling
provider: clef_then_jev     # or clef_with_jev_fallback
api_key: your-openrouter-key
clef_model: clef-flash
```

```python
provider = build_provider("clef_with_jev_fallback", api_key=hosted_key)
decision = SentinelWorkflow(provider).review(case)
print(decision.raw["provider"], decision.raw["attempted"])
```

The next hop is tried only on a transport error, a timeout, or an HTTP 401, 403, 429, or 5xx from the hop before it, so a request Clef rejected as invalid is not quietly retried on the other provider. `raw.provider` names the hop that answered and `raw.attempted` lists the hops that were tried.

The local failure circuit breaker guards the local hop only. It does not apply to a hosted hop leading to another hosted provider, so this chain falls through on every qualifying failure rather than after three: a Clef account that is persistently unavailable costs one failed request per review, which is what the plain hosted route already costs.

## The local decision-model slot

Set `provider` to `local_only` in the config flow when a local decision-model server runs on the same machine. The legacy names `laya` and `laya_local` select the same mode. The local route needs no API key, makes no outbound request, and replaces the hosted routes rather than joining them: it is a single provider with no fallback, and the default mode never selects it on its own initiative. `laya_base_url` defaults to `http://127.0.0.1:8000`, and plain HTTP is accepted for loopback hosts only.

The local provider name stays `laya`, but it is a **generic local decision-model slot** rather than a binding to one model. The `local_model` setting selects which local System One decision model answers and defaults to `laya`; it is free text, not a list, because a local model published after this release has to work by configuration alone.

```yaml
provider: local_only
laya_base_url: http://127.0.0.1:8000
local_model: laya
```

`laya_model` is kept as the deprecated spelling of the same setting. When both are present, `local_model` wins; an entry that stored only `laya_model` keeps calling that checkpoint.

### Pointing the local slot at a different engine

Changing engines is two configuration values and no code change: `laya_base_url` to the engine's server, and `local_model` to its engine or checkpoint name.

System One decision models known to fit the slot, because they publish the same `POST /v1/systemone` request shape and answer in the same `answers` shape:

| `local_model` value | Model |
|---|---|
| `laya`, `laya-multilingual`, `laya-typed-decisions` | [Laya](https://github.com/NandhaKishorM/laya) (Convai Innovations, local, open weights) |
| your own engine or checkpoint name | Laya or other pre-deterministic routing models: any local engine publishing the same `POST /v1/systemone` request shape and answering in the same `answers` shape |

[chaitin/Decis](https://github.com/chaitin/Decis) is the reference for the interchangeable-engine claim: it serves several such engines behind one `/v1/systemone` endpoint, with one Docker image per engine, and swapping `base_url` is the whole migration.

A worked example, one Decis image per engine on loopback:

```bash
# An engine of your choice, on loopback, on a port Home Assistant is not using.
docker run --rm -p 127.0.0.1:8000:8000 ghcr.io/chaitin/decis:laya
```

```yaml
provider: local_only
laya_base_url: http://127.0.0.1:8000
local_model: laya        # or any other local engine or checkpoint name
```

Swap the image and change one line:

```yaml
provider: local_only
laya_base_url: http://127.0.0.1:8000
local_model: your-local-engine
```

Two boundaries on this claim, stated plainly:

- **No local model other than the default has been called live.** `local_model` is proved with an injected transport and a payload capture, which shows what the request asks for; it says nothing about whether another engine answers that shape. The only verified live behaviour is the Laya route recorded in the release notes, captured against a real `laya-serve` on 2026-09-26. Run one review against the engine you configure and read it as a smoke test.
- **A name that cannot be a name is refused.** An empty or whitespace-only value, a value that is not text, and a value carrying a control character, a quote, a backslash, whitespace, or the URL characters `?`, `#`, and `&` are all rejected with `invalid local_model` at build time, and the config form reports `local_model_invalid` before the entry is stored. Characters that would corrupt a path segment are refused rather than silently percent-encoded, so an engine is never misaddressed. There is no allowlist, so a model published tomorrow works tonight.

```bash
python -m pip install laya
LAYA_HOST=127.0.0.1 LAYA_PORT=8000 LAYA_MODELS=english laya-serve
```

The first load takes 25 to 35 seconds, one review takes a few seconds on CPU, and the adapter waits up to 120 seconds. Home Assistant listens on 8123 itself, so bind the server elsewhere and set `laya_base_url` to that port. Keep the server on loopback, because a `laya-serve` started without `LAYA_API_KEY` accepts any request on the host.

## Local first, hosted API behind it

Set `provider` to `local_with_api_fallback`, or to either of its earlier names `laya_then_hosted` and `laya_with_jev_fallback`, to answer from the local model first and keep a hosted key behind it. For the Clef-first chain, see the Clef sections above. The mode needs both a running local decision-model server and an OpenRouter API key. It is an explicit opt-in: the hosted routes and the local-only route behave exactly as they did before, an existing config entry without a `provider` field keeps the hosted route, and no other mode reaches the local server.

## Hosted API first, local model behind it

Set `provider` to `api_with_local_fallback`, or to its Clef-pinned name `clef_with_local_fallback`, to answer from the hosted API first and keep the local model behind it. This is the one arrangement v1.3.0 did not have, because both of its chains were local-first or hosted-only. It needs a hosted credential **and** a running local server.

```yaml
provider: api_with_local_fallback
api_key: your-openrouter-key
laya_base_url: http://127.0.0.1:8000
local_model: laya
```

With the hosted API leading, the redacted case leaves the machine on every call while the hosted provider answers, exactly as it does on `api_only`. The local hop is reached only when the hosted attempt fails on a configured trigger. If the case must never leave the machine, use `local_only` alone.

The mode fails fast when no hosted provider is configured, with `missing OPENROUTER_API_KEY for the api_with_local_fallback mode`, rather than quietly answering from the local slot under a hosted-first mode's name.

The four modes are `api_only`, `local_only`, `api_with_local_fallback`, and `local_with_api_fallback`. Every name earlier releases used remains an alias and selects the same routing: `openrouter`, `jev_api`, `clef`, and `clef_api` are `api_only`; `laya` and `laya_local` are `local_only`; `laya_then_hosted`, `laya_with_jev_fallback`, `clef_then_jev`, and `clef_with_jev_fallback` are `local_with_api_fallback`. The complete table is in [the reference](reference.md#the-alias-table).

```yaml
# The local-first mode in the config flow, any of its three names
provider: local_with_api_fallback   # or laya_then_hosted / laya_with_jev_fallback
api_key: your-openrouter-key
laya_base_url: http://127.0.0.1:8123
local_model: laya
```

The same shape in the Python core:

```python
from sentinel import Case, SentinelWorkflow, build_provider

provider = build_provider("local_with_api_fallback", api_key=hosted_key)
# An earlier name selects the same routing.
provider = build_provider("laya_with_jev_fallback", api_key=hosted_key)
# The local model is configuration, not a new provider.
provider = build_provider("local_with_api_fallback", api_key=hosted_key,
                          local_model="your-local-engine")
decision = SentinelWorkflow(provider).review(case)
print(decision.raw["provider"], decision.raw["attempted"])
```

A hosted hop is tried only on a transport error, a timeout, or an HTTP 401, 403, 429, or 5xx from the local server. Any other local reply propagates, so a request the local server rejected as invalid is not quietly retried on the hosted side. The decision records the hop that answered in `raw.provider` and the hops that were tried in `raw.attempted`. That rule is the same on the Clef-first chain, where the hop before the fallback is hosted rather than local.

### Local failure circuit breaker

A local server that stays down must not send every case to a hosted API for as long as it stays down. The route counts consecutive local failures that qualified for the fallback:

| Consecutive local failures | What happens |
|---|---|
| 1, 2, or 3 | The hosted hop is tried, and the decision records `raw.provider: openrouter`. |
| 4 and beyond | The fallback is suppressed, a warning is logged, and the local error is raised. No hosted request is made. |
| Any successful local call | The counter resets to zero. |

The counter is per process and resets on restart. Only the exception class name is logged, never the case, the entity state, or the answer. The breaker bounds repeated remote egress; it cannot detect a local answer that is well formed but wrong. If a case must never leave the machine, use `laya` alone: on that route the counter still resets on success, but there is no hosted hop to reach.

**Privacy.** On this route a failed local attempt sends the redacted case to a hosted API. Sentinel redacts before the first attempt, so every hop receives the same redacted case and credential-shaped fields never leave the machine. The rest of the case does leave the machine when the local server fails. If a case must not leave the machine, use the `laya` route alone: `laya_then_hosted` is the one route that can reach both.

Without a hosted key the route fails fast with `missing OPENROUTER_API_KEY for the laya_then_hosted route` instead of running as a local-only route under a different name.

### Local classifier quality

The headline evidence for the local route is the DOGA fork's 100-question, three-mode benchmark. Against 100 authored, subjective labels, Laya local agreed with 56 goal labels against 88 for Jev, mode 41 against 68, stakes 37 against 67, and high-versus-low ambiguity at the 0.7 threshold 67 against 87. Laya detected none of the 30 authored high-ambiguity labels at 0.7, against 21 of 30 for Jev. The labels are subjective and predate the Laya comparison, so keep `jev_api` as the default and read the numbers as agreement over one authored set.

## Troubleshooting

| Symptom | Check |
|---|---|
| `review` fails before an event | Confirm the config entry mode matches what is running. On the OpenRouter route, check the key and reachability of `https://openrouter.ai/api/alpha/decisions`. On the local route, check that the decision-model server is up and that `laya_base_url` matches its port and `local_model` names the engine it serves. On either fallback mode, check both sides. On a Clef route, check that `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` are set in the Home Assistant environment, that they were set before it started, and that the token has `Account > Workers AI > Read`. |
| `local_with_api_fallback` raises `missing OPENROUTER_API_KEY for the local_with_api_fallback mode` | The chained route has no hosted key to fall back to. Add the key to the config entry; the route never runs as a local-only route under another name. |
| A fallback-mode review answered with `raw.provider: openrouter` | The local hop failed and the case went to the hosted provider. Check that `laya-serve` is up and that `laya_base_url` matches its port; `raw.attempted` lists the hops that were tried. |
| The chained route raised the local error instead of answering | The breaker has tripped after three consecutive local failures in this process. Fix the local server; one successful local call resets the counter, and restarting the process clears it. |
| Decision event exists but no device changes | This is expected in v1.3.0. The integration is shadow-only. |
| Verification says `mismatch` | Compare the actual value with the expected value and confirm the readback targeted the correct entity. |
| Verification says `unavailable` | Keep the case open and notify or retry after the target becomes available. |
| Status sensor remains `ready` | Confirm that `jev_sentinel_decision` was emitted and the sensor config entry is loaded. |
| Laya route returns connection refused | The server is not running, or it is bound to another port. Home Assistant uses 8123 itself. |
| Laya route returns 404 with `{"detail":"Not Found"}` | Something other than `laya-serve` answered on that port. |
| Laya route returns 422 naming the score criteria | The server rejected the confidence rubric. v1.1.0 and later send the required ordered list, so recheck the adapter version in use. |
| A Clef review raises `CLOUDFLARE_ACCOUNT_ID is required for live Clef decisions` | The account id is not set in the Home Assistant environment, or the route was selected before it was exported. Restart Home Assistant after setting it. |
| A Clef review raises `CLOUDFLARE_API_TOKEN is required for live Clef decisions` | The token is not set. The account id alone is not enough: both are required, and neither is read from the config entry. |
| A Clef review raises `Cloudflare Clef request failed with Cloudflare code ...` | Cloudflare rejected the request. The code is Cloudflare's own, commonly an authentication, permission, or rate-limit problem. Check that the token is scoped to `Account > Workers AI > Read` on the account named by `CLOUDFLARE_ACCOUNT_ID`. |
| A Clef review raises `invalid Clef checkpoint: clef, clef-flash` | The stored `clef_model` is not one of the two shipped checkpoints. Clear the field to use the default. |
| A Clef review raises `invalid Cloudflare Clef response: unknown choice for ...` | Clef returned a value the rubric never offered. The answer is refused rather than passed through, so no decision is emitted. |
| A Clef review raises `invalid Cloudflare Clef response: ... outside the 0 to 4 legend scale` | The confidence index fell outside the answer's own legend. It is refused rather than clamped, because a clamped index would misstate the model's position. |
| A chained review answered with `raw.provider: openrouter` on the Clef chain | Clef was unreachable, refused, rate limited, or broken, and the case went to hosted Jev. `raw.attempted` lists the hops that were tried. |
