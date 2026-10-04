# FAQ and troubleshooting

## Does Sentinel give Jev control of my devices?

No. Jev returns a `Decision`. The Home Assistant integration emits that decision as `jev_sentinel_decision`; it does not call a device service. An external consumer must own approval and dispatch.

## What exactly does `review` do?

It creates a `Case` from the service data, redacts credential-shaped fields, sends the case and policy context to the configured route, receives a typed Jev answer, and fires an event containing the case and decision.

```yaml
service: jev_sentinel.review
data:
  event_type: manual_review
  area: kitchen
  entities:
    - light.kitchen
  facts:
    expected_state: "off"
```

## What exactly does `verify` do?

It compares the `expected` and `actual` values supplied to the service. It returns `matched` when they are equal, `mismatch` when they differ, and `unavailable` when `available: false`.

```yaml
service: jev_sentinel.verify
data:
  expected: "off"
  actual: "on"
  available: true
```

This produces `verified: false`, `status: mismatch`, and `next_step: reopen_case`.

## Does a successful service call prove that the device changed?

No. A service-call return is dispatch evidence. Read the target state and pass that observation to `verify`. A matching comparison proves only that the supplied values match.

## What happens when a target is unavailable?

`verify` returns `status: unavailable`, `verified: false`, and `next_step: notify_and_retry`. The caller decides when to retry. Sentinel does not poll.

## Can I use a local model other than Laya?

Yes, by configuration alone. The local provider name stays `laya`, and the `local_model` setting selects which local System One decision model answers, defaulting to `laya`. It is free text rather than a list, so a model published after this release works by naming it, with no code change and no new provider name.

Known to fit the slot, because they publish the same `POST /v1/systemone` request shape: `laya` (also `laya-multilingual` and `laya-typed-decisions`), and any other local pre-deterministic routing model serving that endpoint. [chaitin/Decis](https://github.com/chaitin/Decis) is the reference for the claim: it serves several such engines behind one endpoint, one Docker image per engine, where swapping `base_url` is the whole migration.

Two settings do it: `laya_base_url` to the engine's server and `local_model` to its engine or checkpoint name. A value that cannot be a name is refused: empty, whitespace-only, not text, or carrying a character that would corrupt a URL path segment or a JSON string, which includes the URL characters `?`, `#`, and `&`, a quote, and a backslash. Those are refused rather than silently escaped, so an engine is never misaddressed.

**No local model other than Laya has been called live.** The slot is proved with an injected transport and a payload capture, which shows what the request asks for but says nothing about whether another engine answers that shape. The only verified live behaviour is the Laya route in the release notes, captured against a real `laya-serve` on 2026-09-26. Run one review and read it as a smoke test.

## Can I use another model?

The integration offers four decision modes. `api_only` calls the hosted API and nothing else, so a failure is reported and never rerouted; it runs either hosted Jev over the OpenRouter key with `typesafe/jev-1.13`, or Cloudflare Clef, a separate System One decision model hosted on Workers AI that answers the same typed questions, over a Workers AI account and token from the environment. `local_only` calls the local decision model alone with no key and no fallback. `api_with_local_fallback` calls the hosted API first and falls through to the local model, and `local_with_api_fallback` does the reverse: the local model first, the hosted API behind it. The two `_only` modes are single-provider routes; the two fallback modes are two-provider chains.

Every name earlier releases used is kept as an alias and selects the same routing: `openrouter`, `jev_api`, `clef`, and `clef_api` are `api_only`; `laya` and `laya_local` are `local_only`; `laya_then_hosted`, `laya_with_jev_fallback`, `clef_then_jev`, and `clef_with_jev_fallback` are `local_with_api_fallback`; `clef_with_local_fallback` is `api_with_local_fallback` with Clef leading. The standalone core also accepts any other `DecisionProvider`, so a custom provider can implement `decide(state)` outside the integration adapter.

## What does the Cloudflare Clef route need?

Two environment variables, and nothing in the config flow: `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN`. The account id is configuration and the token is the credential, but both are required before a request is made, and the error names whichever one is missing. The token needs `Account > Workers AI > Read`.

```bash
export CLOUDFLARE_ACCOUNT_ID="your Cloudflare account id"
export CLOUDFLARE_API_TOKEN="a Cloudflare API token with Account > Workers AI > Read"
```

Set them in the Home Assistant environment, for example through the container's environment settings or the `homeassistant` service unit, then restart Home Assistant. The form deliberately does not ask for them, because a config entry is stored on disk in plain text.

Clef ships two checkpoints of the one model, `clef` and `clef-flash`, selected with the `clef_model` field. The checkpoint changes the model and the endpoint, never the shape of the answer, so `clef-flash` is not a separate provider and cannot be selected as one.

## Has the Clef route been run against the real API?

No. No Cloudflare credential authorized for Workers AI was available when this was written: every candidate token was refused with HTTP 401. The Clef route is covered by unit tests with an injected transport, and its request shape, response envelopes, error codes, and question-id rules come from Cloudflare's published documentation rather than from an observed reply. Run one review before you rely on it, and read that first review as a smoke test rather than as evidence that Clef agrees with hosted Jev.

## What happens if Clef rejects a request?

A Cloudflare error envelope with `success: false` raises with Cloudflare's own error code in the message, so a rate limit or a model quota problem is distinguishable from a malformed answer. An HTTP error keeps its status, which is what lets a chain fall through to its next hop on a refusal, an outage, or a rate limit. An answer carrying a choice this integration never offered, or a confidence index outside the answer's own legend scale, is refused before it becomes a decision event. Only the exception class name is logged: not the case, not the entity state, not the account id, not the token.

## How good is the local classifier?

Measured, not assumed. On the DOGA fork's 100-question, three-mode benchmark against 100 authored, subjective labels, Laya local agreed on 56 goal labels against 88 for Jev, mode 41 against 68, stakes 37 against 67, and high-versus-low ambiguity at the 0.7 threshold 67 against 87, detecting none of the 30 authored high-ambiguity labels. The labels are subjective and predate the Laya comparison, so keep `api_only` as the default while the local checkpoint is uncalibrated and read the numbers as agreement over one authored set rather than accuracy on your cases.

## Where does the API key go?

Enter it in the config flow when you select the OpenRouter route. Home Assistant stores it in the config entry. Do not place it in YAML or source control. There is no key to store on the Laya route, and the Clef routes keep nothing in the config entry at all: their token and account id come from `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` in the environment, and the stored OpenRouter key is never forwarded to Clef. Case fields whose keys contain `token`, `password`, `secret`, `api_key`, or `credential` are redacted before provider submission, and the same redaction runs again before the decision event is fired.

## Does the integration support automations?

Yes. Automations can call `review` and `verify`, then respond to `jev_sentinel_decision` or `jev_sentinel_verification`. The events do not create an active dispatch path.

## Is there a local-only decision mode?

Yes, since v1.1.0. Select `local_only` in the config flow, or either of its earlier names `laya` and `laya_local`, and run a local System One decision model server on the same machine. The local route needs no API key and sends no request off the host, and it replaces the hosted routes rather than extending them. The local policy and verification functions already ran without a provider request before that.

```bash
python -m pip install laya
LAYA_HOST=127.0.0.1 LAYA_PORT=8000 LAYA_MODELS=english laya-serve
```

Then set `provider: laya` with `laya_base_url: http://127.0.0.1:8000`. The first load takes 25 to 35 seconds, a review takes a few seconds on CPU, and the adapter waits up to 120 seconds. Home Assistant itself listens on 8123, so do not bind the server there.

## Is there a fallback route that uses the local server first?

Yes, since v1.2.0. Select `local_with_api_fallback`, or either of its earlier names `laya_then_hosted` and `laya_with_jev_fallback`, and supply the hosted key. A review asks the local model first and, only when that attempt fails, sends the same redacted case to the hosted API. The mode is an explicit opt-in: the hosted routes and the local-only route behave exactly as they did before, an existing config entry without a `provider` field keeps the hosted route, and no other mode reaches the local server. `api_with_local_fallback`, new in v1.4.0, is the reverse: the hosted API first and the local model behind it.

The Clef-first chain, `clef_then_jev` and `clef_with_jev_fallback`, works the same way with Clef in place of the local model: Clef first, hosted Jev behind it. A next hop is tried only on a transport error, a timeout, or an HTTP 401, 403, 429, or 5xx; any other reply propagates unchanged. The local failure breaker guards the local hop only, so a Clef-first chain falls through on every qualifying failure rather than after three. Without a hosted key the mode fails fast with `missing OPENROUTER_API_KEY for the local_with_api_fallback mode`, naming the variable rather than degrading into the local-only route under another name.

### What if the local server stays down?

The route counts consecutive local failures. The first three each fall through to the hosted hop. The fourth, and every one after it in the same process, suppresses the fallback: the local error is raised, no hosted request is made, and a warning is logged. The warning carries the exception class name only, never the case, the entity state, or the answer. A successful local call resets the counter to zero, and a restart clears it. The breaker bounds repeated remote egress; it cannot tell that a local answer was well formed but wrong.

### What happens to my case data on that route?

On a chained route, a failed first attempt sends the redacted case to a hosted API. Redaction runs before the first attempt, so every hop receives the same redacted case and credential-shaped fields never leave the machine. The rest of the case does leave the machine whenever the first provider fails. On the Clef routes the redacted case leaves the machine on every call, not only on a failure, because Clef is hosted. If a case must never leave the machine, use the `laya` route alone, which has no fallback.

## How much should I trust the reported confidence?

Treat it as a hint. The confidence question is a five level score rubric, and `confidence` is the model's expected level on that rubric rescaled onto 0 to 1, so adjacent levels sit 0.25 apart. It is a quantized ordinal estimate, not a calibrated probability, and nothing in this repository compares it against a threshold. Policy authorization looks at the action name, not the confidence value. See [Confidence scale](reference.md#confidence-scale) for the exact mapping and its limits.

## What does the `shadow` option do?

The config flow accepts `shadow`, defaulting to true. Current setup code does not read the saved option, and runtime `Decision` objects remain shadow decisions. Treat v1.2.1 as shadow-only.

## Why is the status sensor still `ready`?

The sensor starts at `ready` and updates after `jev_sentinel_decision` or `jev_sentinel_verification`. Confirm the integration entry is loaded and inspect the event bus for the expected event.

## Which actions does the core allow?

The public `Policy` allows `notify`, `ask_user`, `light.turn_on`, `light.turn_off`, `switch.turn_on`, `switch.turn_off`, and `climate.set_temperature`. It marks `lock.unlock`, `alarm_control_panel.alarm_disarm`, `cover.open_garage`, and `water_valve.close` as approval-required. Unknown actions are denied.

## Is this a security or emergency system?

No. Do not use it as certified safety equipment, alarm control, emergency automation, or access control. Home Assistant permissions and dedicated safety systems remain authoritative.

## How can I debug a provider failure?

Check the config entry, the configured route, key availability, network access to `https://openrouter.ai/api/alpha/decisions`, and, on a Clef route, that both `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` are set in the Home Assistant environment. Then check the returned provider shape. The runtime expects an `answers` object with `outcome`, `action`, and `confidence`. No decision event is evidence of a completed review.

On the Laya route, check that `laya-serve` is running and that `laya_base_url` matches the port you bound it to. A connection refused means the server is not up or is on another port. An HTTP 422 carrying `a score question takes 'criteria' as a list of level descriptions` means the server rejected the rubric, which should not happen on v1.1.0 or later. An HTTP 404 with `{"detail":"Not Found"}` means something other than Laya answered on that port.

On `local_with_api_fallback`, the first failure to check is a missing hosted key: the mode fails fast with `missing OPENROUTER_API_KEY for the local_with_api_fallback mode`. When a review completes, `raw.provider` names the hop that answered and `raw.attempted` lists the hops that were tried, so a review answered by `openrouter` with `attempted: ["laya", "openrouter"]` means the local hop failed first. After three consecutive local failures in one process, the fallback is suppressed and the local error is raised instead; the log warning names the exception class and nothing else, and a single successful local call, or a restart, clears the counter. The hosted hop itself is covered by unit tests with an injected transport in this release; the live evidence for the route is its local hop.

## What is the shortest safe rollout?

Start with a harmless review and inspect the emitted event. Add a local approval gate. Dispatch one reversible action through your own consumer. Read the entity back. Call `verify`. Keep the case uncertain whenever the readback is missing or delayed.
