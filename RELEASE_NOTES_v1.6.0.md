# v1.6.0

Six findings from an independent review of the integration, in priority order.
Two of them are the ones that matter most: a `local_only` entry could still send
household cases out over the first entry's hosted key, and an operator watching
the status sensor could see `ready` while every review on that entry was
failing.

| | v1.5.0 | v1.6.0 |
| --- | --- | --- |
| test suite | 251 passed, 3 skipped | **351 passed, 3 skipped** |

---

## 1. Services captured the first config entry, so a `local_only` entry still sent cases out

Home Assistant registers a service **once per domain**, not once per config
entry. The handler `custom_components/jev_sentinel/__init__.py` registered was
the first entry's closure, and the second entry's handlers were silently
discarded: with two entries loaded, every `jeve_sentinel.review` call used the
first entry's provider.

A user who added a `local_only` entry beside a hosted one kept talking to the
OpenRouter key stored in entry #1. Household data left the house contrary to the
second entry's intent, which is the property this repository exists to protect.

Worse, `async_unload_entry` only removed the services when `hass.data[DOMAIN]`
was empty, so unloading the *first* entry left the registered handler bound to
the entry it had captured — an unloaded entry.

**What changed.** The handlers are registered once and resolve their entry on
every call, reading `hass.data[DOMAIN]` at call time instead of closing over an
entry. A call names `entry_id`; with exactly one entry loaded the field is
optional, and with several loaded a call that names none is **refused** rather
than answered from whichever entry happens to sit first, because a silent
default is the same misrouting wearing a different hat. The refusal fires
`jeve_sentinel_decision` with `outcome: "unavailable"`.

`docs/reference.md` said the services are registered "once per loaded config
entry", which is not a thing Home Assistant can do and is not what the code did.
It now says what is true.

## 2. A misconfigured provider crashed the service call and left the sensor stale

`_provider_for(entry)` calls `build_provider`, which raises for a misconfigured
entry — a mode that reaches hosted Jev with an empty key, or a `laya_base_url`
the policy refuses. The exception escaped the handler: no
`jeve_sentinel_decision` event fired, and `SentinelStatusSensor` kept its
previous value, which on a fresh install is `ready`.

An operator watching the sensor saw `ready` while every review on that entry was
failing.

**What changed.** The handler is wrapped. A failure fires
`jeve_sentinel_decision` with `{"outcome": "error", "error_type", "reason"}`, so
the sensor — which reads `decision.outcome` and nothing else — reads `error` and
its attributes carry the reason. The same shape covers a call that names no
entry while several are loaded, with `outcome: "unavailable"`. The local-failure
breaker path is covered by the same wrapper, because its re-raised local error
reached the caller exactly the same way.

## 3. The config-flow key gate omitted the legacy fallback aliases

`_needs_api_key` read the *spelling* the operator picked rather than the mode it
resolves to. `laya_with_jev_fallback` and `laya_then_hosted` both resolve to
`local_with_api_fallback`, whose `provider_order` raises
`ValueError: missing OPENROUTER_API_KEY` — but neither was asked for a key. A
user who picked a legacy alias was never prompted, saved, and every review on
the entry crashed per finding 2.

**What changed.** The gate is keyed off `resolve_provider(stored)`. `auto`, which
is dynamic, is resolved from the same submission's `local_model` through
`resolve_auto_mode`. `tests/test_config_key_gate.py` is table-driven over every
name the form offers, and asserts against what `build_provider` actually needs
with no key at all: the form and the builder are two implementations of one
question, and this is the one place they are made to agree.

## 4. Policy was an action-name allowlist with no entity, area, or parameter scope

`Policy.authorize(action)` took the action string alone. It could not distinguish
`light.turn_on` on `light.living_room` from the same action on a nursery
blackout lamp. `climate.set_temperature` was allowlisted with **no temperature
bounds at all**: an AI decision of 40 °C on `climate.nursery` in winter was
authorized with no approval. And `user_approved` was a bare boolean with no
provenance, no expiry, and no caller in the repository setting it `True`.

**What changed.** `authorize` takes `(action, entity_id, service_data,
*, user_approved=None)` and answers for a whole request:

- **Entity scope.** An entity whose id carries a segment matching
  `restricted_entity_patterns` — `nursery`, `baby`, `infant`, `child`,
  `blackout`, `incubator`, `medical`, `medication`, `aquarium`, `terrarium`,
  `vivarium`, `freezer`, `fridge`, `refrigerator` — needs an approval, so the
  nursery lamp is not moved by a recommendation. The match is on whole segments,
  split on every separator at once, so `blackout` is not reached by `out` and
  `light.living_room` is never refused. A caller may replace the set; an empty
  set disables the check.
- **Value scope.** `value_ranges` bounds the parameters of an action. A
  `climate.set_temperature` outside 5–30 °C is refused with
  `value_out_of_range`, *before* any approval is consulted, because an
  out-of-range value names a request nobody should make and an approval does not
  make it one somebody should.
- **Provenance.** `user_approved` accepts an `Approval` record carrying `by`,
  `at`, `scope`, and `expires_at`, or a mapping with those keys, or a bare
  boolean for backwards compatibility. An approval that does not name the
  request it is offered for is `approval_out_of_scope`; an expired one is
  `approval_expired`, and an unparseable expiry is not a permissive one. The
  record is written into the authorization result, so the event bus says who
  approved what and when.

The segment split is the one implementation detail worth naming: splitting on
one separator at a time gives `light.nursery_room` the segments `light`,
`nursery_room`, `light.nursery`, and `room` — never `nursery`, which is the one
the policy is looking for. A test caught it, and both copies split on
`[^0-9a-z]+` at once now.

`SentinelWorkflow.execute` reads `entity_id` and `service_data` out of
`case.facts`, falling back to `decision.raw`, so a decision that names an entity
is authorized for that entity. **`runtime.py` carried no `execute` at all** and
its own `Policy` had no `approval_required` set, so the copy Home Assistant
actually loads had no authorize step; both are now there, and both copies answer
identically.

## 5. `laya_base_url` was unvalidated free text, and household case data could go anywhere

`laya_endpoint` rejected only a non-http(s) scheme, a missing hostname, and
cleartext to a non-loopback host. So `https://evil.example.com` was accepted
unchanged, and every redacted-but-still-descriptive case — area, entity ids,
`window_open_minutes`, `heating_state` — was posted there. There was no loopback
requirement and no allowlist.

Separately, a `base_url` that already carried a path was joined to the endpoint
path, producing `http://127.0.0.1:8000/v1/systemone/v1/systemone` — a URL no
server answers, which reads in the log as a working endpoint.

**What changed.** The local slot's host must be loopback or a private address on
the household's own network, with HTTPS required for anything that is not
loopback. An mDNS `.local` name is allowed, because it resolves on the local
network and nowhere else. A **public** remote host is refused — that is the
finding — while a private address stays allowed over HTTPS for the household
that runs its local server on another box on its own network. A URL carrying a
path, a query, a fragment, or embedded credentials is refused, with a message
that names the doubling it would have produced; credentials belong in
`LAYA_API_KEY`, not in a URL that becomes a log line.

The config flow validates the same value through the same function, so the
operator gets `laya_base_url_invalid` in the form rather than a crash at the
first review. The new error key exists in `strings.json` and
`translations/en.json`, which stay identical.

## 6. CI pinned mutable refs and never ran the tests

`.github/workflows/hassfest.yaml` used `home-assistant/actions/hassfest@master`
and `validate.yaml` used `hacs/action@main`. Both are mutable refs: anyone who
can push to either repository changes what validates every pull request here,
with no change in this repository's history. Neither workflow ran the test suite
either, so a merge could be "validated" while its own tests were failing.

**What changed.** Both actions are pinned to full 40-hex commit SHAs and both
validation jobs now declare `needs: core`, so a release that has not passed its
own tests is not a validated one. `ci.yml` already ran `pytest`; it continues to,
alongside `black`, `isort`, `compileall`, `json.tool`, and `git diff --check`.

`tests/test_release_artifacts.py` asserted the *old* pinned strings
(`hassfest@master`, `hacs/action@main`), which is the assertion that made pinning
look like a regression. It now asserts the official accounts, a full SHA, the
absence of a branch or tag pin, the `needs: core` dependency, and that the core
workflow still runs the suite. That is the same property, strengthened, not a
test weakened to make a change pass.

Separately, `tests/test_release_artifacts.py::test_service_schema_uses_structured_readback_values`
asserted `"entity:"` and `"multiple: true"` appear in `services.yaml` — and they
do, but from `review.entities`. The `verify` readback fields were plain
`text:` selectors with no entity selector, so `verify` accepted arbitrary
caller-supplied strings as "expected"/"actual" with no link to real entity
state. **The service is renamed** to "Record a caller-attested readback
comparison", and its description and `docs/reference.md` now say plainly that
this is a caller-attested comparison, not a readback: the event attests two
strings, not the house, and a readback backed by real entity state is the
caller's automation.

---

## Both copies, every fix

The safety layer exists twice: `sentinel/`, the pip package, and a 2,129-line
duplicate inside `custom_components/jev_sentinel/runtime.py`, which is the copy
Home Assistant actually loads from a HACS install.

Every fix above lands in **both**, and the property that caught the v1.5.0
redaction drift is now asserted for the policy, the breaker signatures, and the
local-slot URL rules: the two copies must answer **identically**, not merely both
answer. `tests/test_policy_scope.py` parametrizes every action, entity, and
service-data combination through both copies and compares the whole returned
record, timestamp aside. That is the assertion that caught the last one.

The local-failure breaker was also a **process global**, so one entry's dead
local server denied every other entry its hosted fallback until restart. It is
now keyed by the local endpoint, so each entry's hop carries its own count; the
cooldown is unchanged and applies per hop.

### Known issues

- `_target` reads `entity_id` and `service_data` from `case.facts` first and
  `decision.raw` second. Neither source is trusted — the policy decides for
  itself — but a provider that answers with an entity in `raw` gets it scoped,
  which is the intended direction.
- `value_ranges` covers `climate.set_temperature` only. `light.turn_on`'s
  `brightness` and any other action's parameters are not bounded; the range
  table is where a bound for a new parameter belongs.
- The `verify` service still takes free text. The rename says so; only a schema
  change against real entity state would fix it, and that is a breaking change
  for every automation already calling it.
