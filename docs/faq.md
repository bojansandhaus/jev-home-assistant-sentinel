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

## Can I use another model?

The integration offers three routes. Hosted Jev runs over the OpenRouter key with `typesafe/jev-1.13`, and Laya runs locally with no key; those two are alternatives rather than members of one chain. The third, `laya_then_hosted`, is an explicit opt-in chain that answers from Laya first and falls through to hosted Jev. Choose one in the config flow. The standalone core also accepts any other `DecisionProvider`, so a custom provider can implement `decide(state)` outside the integration adapter.

## Where does the API key go?

Enter it in the config flow when you select the OpenRouter route. Home Assistant stores it in the config entry. Do not place it in YAML or source control. There is no key to store on the Laya route. Case fields whose keys contain `token`, `password`, `secret`, `api_key`, or `credential` are redacted before provider submission, and the same redaction runs again before the decision event is fired.

## Does the integration support automations?

Yes. Automations can call `review` and `verify`, then respond to `jev_sentinel_decision` or `jev_sentinel_verification`. The events do not create an active dispatch path.

## Is there a local-only decision mode?

Yes, since v1.1.0. Select `laya` in the config flow and run `laya-serve` on the same machine. The local route needs no API key and sends no request off the host, and it replaces the hosted route rather than extending it. The local policy and verification functions already ran without a provider request before that.

```bash
python -m pip install laya
LAYA_HOST=127.0.0.1 LAYA_PORT=8000 LAYA_MODELS=english laya-serve
```

Then set `provider: laya` with `laya_base_url: http://127.0.0.1:8000`. The first load takes 25 to 35 seconds, a review takes a few seconds on CPU, and the adapter waits up to 120 seconds. Home Assistant itself listens on 8123, so do not bind the server there.

## Is there a fallback route that uses the local server first?

Yes, since v1.2.0. Select `laya_then_hosted` and supply the hosted key. A review asks the local server first and, only when that attempt fails, sends the same redacted case to the hosted provider. The route is an explicit opt-in: the hosted route and the `laya` route behave exactly as they did before, an existing config entry without a `provider` field keeps the hosted route, and no other route reaches the local server. A hosted hop is tried only on a transport error, a timeout, or an HTTP 401, 403, 429, or 5xx from the local server; any other reply propagates unchanged. Without a hosted key the route fails fast with `missing OPENROUTER_API_KEY for the laya_then_hosted route`.

### What happens to my case data on that route?

On the chained route, a failed local attempt sends the redacted case to a hosted API. Redaction runs before the first attempt, so both hops receive the same redacted case and credential-shaped fields never leave the machine. The rest of the case does leave the machine whenever the local server fails. If a case must never leave the machine, use the `laya` route alone, which has no fallback.

## How much should I trust the reported confidence?

Treat it as a hint. The confidence question is a five level score rubric, and `confidence` is the model's expected level on that rubric rescaled onto 0 to 1, so adjacent levels sit 0.25 apart. It is a quantized ordinal estimate, not a calibrated probability, and nothing in this repository compares it against a threshold. Policy authorization looks at the action name, not the confidence value. See [Confidence scale](reference.md#confidence-scale) for the exact mapping and its limits.

## What does the `shadow` option do?

The config flow accepts `shadow`, defaulting to true. Current setup code does not read the saved option, and runtime `Decision` objects remain shadow decisions. Treat v1.2.0 as shadow-only.

## Why is the status sensor still `ready`?

The sensor starts at `ready` and updates after `jev_sentinel_decision` or `jev_sentinel_verification`. Confirm the integration entry is loaded and inspect the event bus for the expected event.

## Which actions does the core allow?

The public `Policy` allows `notify`, `ask_user`, `light.turn_on`, `light.turn_off`, `switch.turn_on`, `switch.turn_off`, and `climate.set_temperature`. It marks `lock.unlock`, `alarm_control_panel.alarm_disarm`, `cover.open_garage`, and `water_valve.close` as approval-required. Unknown actions are denied.

## Is this a security or emergency system?

No. Do not use it as certified safety equipment, alarm control, emergency automation, or access control. Home Assistant permissions and dedicated safety systems remain authoritative.

## How can I debug a provider failure?

Check the config entry, the configured route, key availability, network access to `https://openrouter.ai/api/alpha/decisions`, and the returned provider shape. The runtime expects an `answers` object with `outcome`, `action`, and `confidence`. No decision event is evidence of a completed review.

On the Laya route, check that `laya-serve` is running and that `laya_base_url` matches the port you bound it to. A connection refused means the server is not up or is on another port. An HTTP 422 carrying `a score question takes 'criteria' as a list of level descriptions` means the server rejected the rubric, which should not happen on v1.1.0 or later. An HTTP 404 with `{"detail":"Not Found"}` means something other than Laya answered on that port.

On the `laya_then_hosted` route, the first failure to check is a missing hosted key: the route fails fast with `missing OPENROUTER_API_KEY for the laya_then_hosted route`. When a review completes, `raw.provider` names the hop that answered and `raw.attempted` lists the hops that were tried, so a review answered by `openrouter` with `attempted: ["laya", "openrouter"]` means the local hop failed first. The hosted hop itself is covered by unit tests with an injected transport in this release; the live evidence for the route is its local hop.

## What is the shortest safe rollout?

Start with a harmless review and inspect the emitted event. Add a local approval gate. Dispatch one reversible action through your own consumer. Read the entity back. Call `verify`. Keep the case uncertain whenever the readback is missing or delayed.
