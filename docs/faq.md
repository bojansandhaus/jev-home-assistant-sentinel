# FAQ and troubleshooting

## Does Sentinel give Jev control of my devices?

No. Jev returns a `Decision`. The Home Assistant integration emits that decision as `jev_sentinel_decision`; it does not call a device service. An external consumer must own approval and dispatch.

## What exactly does `review` do?

It creates a `Case` from the service data, redacts credential-shaped fields, sends the case and policy context to OpenRouter, receives a typed Jev answer, and fires an event containing the case and decision.

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

The Home Assistant runtime uses OpenRouter and `typesafe/jev-1.13`. The standalone core accepts a different `DecisionProvider`, so another model can implement `decide(state)` outside the integration adapter.

## Where does the OpenRouter key go?

Enter it in the config flow. Home Assistant stores it in the config entry. Do not place it in YAML or source control. Case fields whose keys contain `token`, `password`, `secret`, `api_key`, or `credential` are redacted before provider submission.

## Does the integration support automations?

Yes. Automations can call `review` and `verify`, then respond to `jev_sentinel_decision` or `jev_sentinel_verification`. The events do not create an active dispatch path.

## Is there a local-only decision mode?

There is no local Jev provider in v1.0.0. The local policy and verification functions can run without an OpenRouter request. The Home Assistant `review` service requires the configured provider.

## What does the `shadow` option do?

The config flow accepts `shadow`, defaulting to true. Current setup code does not read the saved option, and runtime `Decision` objects remain shadow decisions. Treat v1.0.0 as shadow-only.

## Why is the status sensor still `ready`?

The sensor starts at `ready` and updates after `jev_sentinel_decision` or `jev_sentinel_verification`. Confirm the integration entry is loaded and inspect the event bus for the expected event.

## Which actions does the core allow?

The public `Policy` allows `notify`, `ask_user`, `light.turn_on`, `light.turn_off`, `switch.turn_on`, `switch.turn_off`, and `climate.set_temperature`. It marks `lock.unlock`, `alarm_control_panel.alarm_disarm`, `cover.open_garage`, and `water_valve.close` as approval-required. Unknown actions are denied.

## Is this a security or emergency system?

No. Do not use it as certified safety equipment, alarm control, emergency automation, or access control. Home Assistant permissions and dedicated safety systems remain authoritative.

## How can I debug a provider failure?

Check the config entry, key availability, network access to `https://openrouter.ai/api/alpha/decisions`, and the returned provider shape. The runtime expects an `answers` object with `outcome`, `action`, and `confidence`. No decision event is evidence of a completed review.

## What is the shortest safe rollout?

Start with a harmless review and inspect the emitted event. Add a local approval gate. Dispatch one reversible action through your own consumer. Read the entity back. Call `verify`. Keep the case uncertain whenever the readback is missing or delayed.
