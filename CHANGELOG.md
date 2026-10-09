# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

The full release notes, including the limitations and the verification performed,
are in [docs/release-notes.md](docs/release-notes.md).

## [1.6.0]

### Security

- **Services captured the first config entry, so a `local_only` entry still sent cases out.** Home Assistant registers a service once per domain, not once per config entry, but the registered handler was the first entry's closure and the second entry's handlers were discarded — so with two entries loaded every `jeve_sentinel.review` used the first entry's provider. A user who added a `local_only` entry beside a hosted one kept talking to the OpenRouter key stored in entry #1, sending household data out contrary to the second entry's intent. `async_unload_entry` also left the handler bound to the first entry after unloading it. Handlers now resolve their entry on every call from `hass.data[DOMAIN]`; `entry_id` names the entry, is optional with one entry loaded, and a call naming none while several are loaded is refused with `outcome: "unavailable"` rather than answered from whichever entry sits first.
- **A misconfigured provider crashed the service call and left the sensor stale.** `build_provider` raises for a mode that reaches hosted Jev with an empty key or a refused `laya_base_url`; the exception escaped the handler, no decision event fired, and `SentinelStatusSensor` kept its previous value — `ready` on a fresh install — so an operator watched a healthy-looking sensor while every review failed. The handler is wrapped, fires `jeve_sentinel_decision` with `outcome: "error"`, `error_type`, and `reason`, and the sensor reads `error` with those as attributes.
- **Policy was an action-name allowlist with no entity, area, or parameter scope.** `Policy.authorize(action)` took the action alone, so `light.turn_on` on `light.living_room` and the same action on `light.nursery_blackout` were indistinguishable, and `climate.set_temperature` was allowlisted with no temperature bounds — an AI decision of 40 C on a nursery climate in winter was authorized with no approval. `authorize` now takes `(action, entity_id, service_data)`; entities whose id carries a restricted segment (`nursery`, `baby`, `infant`, `child`, `blackout`, `incubator`, `medical`, `medication`, `aquarium`, `terrarium`, `vivarium`, `freezer`, `fridge`, `refrigerator`) need an approval, and `value_ranges` bounds action parameters with a `value_out_of_range` refusal checked before any approval. `user_approved` is an `Approval` record (`by`, `at`, `scope`, `expires_at`) or a mapping; an out-of-scope approval is `approval_out_of_scope`, an expired one is `approval_expired`, and the provenance is written into the authorization record.
- **`laya_base_url` was unvalidated free text, and household case data could go to an arbitrary remote host.** `laya_endpoint` rejected only a non-http(s) scheme, a missing hostname, and cleartext to a non-loopback host, so `https://evil.example.com` was accepted unchanged and every redacted-but-still-descriptive case was posted there. A base URL already carrying a path was joined, producing `.../v1/systemone/v1/systemone`. The local slot's host must now be loopback, a private address, or an mDNS `.local` name, with HTTPS required off loopback; a URL carrying a path, query, fragment, or embedded credentials is refused; the config form validates the same value through the same function and reports `laya_base_url_invalid`.
- **`runtime.py` carried no `execute()` and its `Policy` had no `approval_required` set.** The copy Home Assistant actually loads had no authorize step at all, and `lock.unlock` was reported there as "not allowlisted" rather than "approval required". Both are now in place and both copies answer identically.

### Fixed

- **The local-failure breaker was a process global, so one entry's dead local server denied every other entry its hosted fallback until restart.** The counter is keyed by the local endpoint, so each entry's hop carries its own count. `local_failure_count(scope)` reports what the breaker compares against the limit, `local_failure_count()` reports the process total as a diagnostic, `reset_local_failures(scope)` clears one hop, and `reset_local_failures()` clears every hop. The cooldown is unchanged and applies per hop.
- **The config-flow key gate read the stored spelling instead of the resolved mode,** so `laya_with_jev_fallback` and `laya_then_hosted` — both `local_with_api_fallback`, whose provider order raises `missing OPENROUTER_API_KEY` — were not asked for a key, and every review on such an entry crashed. `auto` is resolved from the same submission's `local_model`.
- **A false-positive in the segment split.** `_restricted_entity` split `light.nursery_room` on one separator at a time, which yields `light`, `nursery_room`, `light.nursery`, and `room` — never `nursery`. Both copies now split on `[^0-9a-z]+` at once. Found by a test written for this release.
- **`docs/reference.md` claimed the services are registered "once per loaded config entry,"** which is not something Home Assistant can do and not what the code did. The reference now documents entry selection, the two failure outcomes, the policy's three scopes and provenance record, the local slot's host rules, and the per-hop breaker.

### Changed

- **CI no longer pins third-party actions to mutable refs, and the validation workflows run the test suite.** `home-assistant/actions/hassfest@master` and `hacs/action@main` were both pins that whoever can push to either repository can repoint; both are now full 40-hex commit SHAs, and both `hassfest.yaml` and `validate.yaml` run `python -m pytest`, so a merge that fails its own tests is not a validated one. A `needs: core` in a separate workflow file cannot work — GitHub's `needs` cannot cross workflow files, and this release's own first attempt failed at validation for exactly that reason.
- **`jeve_sentinel.verify` is renamed and documented as a caller-attested comparison.** Its fields were plain `text:` selectors with no entity selector, so `verify` accepted arbitrary caller-supplied strings as "expected"/"actual" with no link to real entity state. The service is now "Record a caller-attested readback comparison", and its description and the reference say plainly that the event attests two strings, not the house, and that a readback backed by real entity state is the caller's automation.
- **`hass.data[DOMAIN]` stores the entry object rather than a copy of its data,** so a handler resolving an entry by id gets the entry that carries `.data` and `.entry_id`.

### Added

- `tests/test_policy_scope.py`, 50 tests: the entity scope, the value ranges, the approval scope and expiry, the worked nursery-lamp and 40 C cases, the two-copy agreement over every action and entity combination, and the workflow reading its target out of the case.
- `tests/test_entry_scoped_services.py`, 20 tests: the misconfigured-provider event and the sensor's reading rule, the two-entry privacy case, an unnamed call refused rather than defaulted, unloading the first entry leaving no stale handler, services surviving while any entry remains, and the per-hop breaker in both copies.
- `tests/test_config_key_gate.py`, 27 tests: table-driven over every name the form offers and asserted against what `build_provider` needs with no key, both legacy aliases, the `auto` resolution, and three `laya_base_url` rejections through the form.

## [1.5.0]

### Security

- **A readback that returned nothing was reported as a successful readback.** `verifier.verify` compared its two arguments and closed the case on equality, so `verify(None, None)` returned `{'verified': True, 'status': 'matched', 'next_step': 'close_case'}`. A case built without an explicit `expected_state` had no verification requirement at all, and an entity renamed or removed between dispatch and readback made the readback `None` too. A device action was closed as successfully verified with zero state readback performed. Neither side being known is now reported as `no_expectation` or `unavailable`.
- **An async dispatcher was recorded as sent, and the device never moved.** `SentinelWorkflow.execute` accepted any callable, so a Home Assistant async service call returned a coroutine, was recorded `{'status': 'sent', 'result_type': 'coroutine'}`, was collected without being awaited, and the device never moved — while the readback closed the case as verified. Awaitable results are now refused as `unsupported_dispatch` with `dispatch_not_performed`. Exposure is documented integrators via `docs/integrations.md:66`; the shipped integration itself does not dispatch, which `docs/faq.md:5` already states.
- **Redaction missed nine credential spellings and every structured form.** `_key_is_secret` matched six words as substrings against a flattened key, so `private_key`, `access_key`, `key`, `passwd`, `passphrase`, `authorisation`, `cookie`, `clientid` and `bearer` carried their values to the provider and onto the event bus in cleartext. The string pattern required the colon to follow the word immediately, so every JSON form passed through, and `bearer` was absent from it entirely. Keys are now widened and matched on segments — `private_key` is a secret, `door_pin` is not — and the string branch allows a quoted value and masks `Bearer <token>`. A prose form was tried and removed: it also matched `the api key is stored in the vault`, which names no credential and which the suite already pinned as pass-through.

### Fixed

- **All three fixes land in both copies of the safety layer.** `sentinel/` and `custom_components/jev_sentinel/runtime.py` each carry their own verifier and redactor; the shipped copy is the one Home Assistant loads. A regression test asserting the two agree failed the moment the first copy was fixed, which is the failure mode `tests/test_safety_boundaries.py:66` exists to guard. The new tests run against both.

### Added

- `tests/test_fail_closed_regressions.py`, 33 tests: the verifier's four states and the shipped copy, the async refusal with the coroutine closed, the synchronous and raising dispatchers still behaving, twelve credential spellings, four credential string forms, six household keys and one prose sentence left alone, all against both implementations.

### Known issues

- `runtime.py`'s `Policy` carries no `approval_required` set, so `lock.unlock` is reported there as "not allowlisted" rather than "approval required" — the same refusal under a different reason code. A further divergence between the two copies of the layer.

## [1.4.0]

### Added

- **Four decision modes**, replacing the five route names: `api_with_local_fallback`,
  `api_only`, `local_only`, and `local_with_api_fallback`. Each names which side leads and
  whether the other side is a fallback. The two `_only` modes are single-provider routes
  whose failures are reported and never rerouted; the two fallback modes are
  two-provider chains using the existing cooldown, trigger, and breaker machinery.
- **`api_with_local_fallback`**, a hosted-first chain that did not exist before. It
  resolves to `["openrouter", "laya"]`, and the local hop is reached only when the hosted
  attempt fails on a configured trigger.
- **`clef_with_local_fallback`**, the Clef-pinned form of the hosted-first mode, resolving
  to `["clef", "laya"]`.
- **The `local_model` setting**, defaulting to `laya`, which selects which local decision
  model answers. The provider name in configuration stays `laya`; the value is the
  checkpoint or engine name sent to the local server, so a different local model is selected
  by configuration alone, with no code change and no new provider name.
- **`local_checkpoint`, `local_model_configured`, `resolve_auto_mode`,
  `accepted_provider_names`, and `pins_clef`**, in the package and in the Home Assistant
  runtime bridge, alongside the mode constants `API_WITH_LOCAL_FALLBACK`, `API_ONLY`,
  `LOCAL_ONLY`, `LOCAL_WITH_API_FALLBACK`, `CANONICAL_MODES`, `FALLBACK_MODES`,
  `SINGLE_PROVIDER_MODES`, `CLEF_PINNED_NAMES`, `CASE_INSENSITIVE_ALIASES`, `LOCAL_MODEL`,
  and `LOCAL_MODEL_FIELD`.
- **`local_model` in the Home Assistant config flow**, as free text with its own
  translation label, validated before the entry is stored.
- **The System One decision model category, named in the documentation.** The models this
  integration reaches are described as members of one category rather than by one vendor's
  member: Jev (hosted, TypeSafe or OpenRouter, closed weights), Clef and Clef Flash (hosted,
  Cloudflare Workers AI), and Laya (local, open weights, the `local_model` default), plus
  whatever other local pre-deterministic routing model is configured. See the
  [System One ecosystem index](https://systemonemodels.org/) and
  [what is a System One model](https://systemonemodels.org/guides/what-is-a-system-one-model/).

  Jev is one member of the category, not the name of it, and the phrase naming one member as
  the category is gone from the docs, from `strings.json`, and from both copies of the provider
  rules.

### Changed

- **The mode vocabulary.** `MODE_NAMES` is the four canonical names, and `MODE_ALIASES`
  and `PROVIDER_MODES` are total over each other.
- **`laya_model` is deprecated in favour of `local_model`** and is kept working. An entry
  that stored only `laya_model` keeps calling that checkpoint; `local_model` takes
  precedence when both are present.
- **`auto` is now dynamic.** It resolves to `api_with_local_fallback` when a `local_model`
  has been explicitly configured and to `api_only` otherwise, which is what it meant
  before. It never selects the local slot on its own initiative.
- **Failure messages name the canonical mode**, so the local-first chain failure is
  `missing OPENROUTER_API_KEY for the local_with_api_fallback mode`. The variable that is
  missing is unchanged, and no alias string appears in observable output.
- **`provider_mode` and `provider_order` name every accepted mode in their refusal**, which
  still begins with `invalid provider`.
- **`clef_then_jev` resolves to `local_with_api_fallback`**, with the Clef lead preserved
  from the stored spelling, because both of its hops are hosted and the mode name alone
  cannot express which hosted provider leads.
- **An explicitly supplied credential, including an empty one, is never topped up from the
  process environment.** This closes a leak in `OpenRouterJev`, where `OpenRouterJev("")`
  previously fell back to `os.environ["OPENROUTER_API_KEY"]` and would have sent the
  machine's real key to a hosted endpoint. `ClefJev` and `LayaJev` already behaved this way.
- **`strings.json` and `translations/en.json`** describe the four modes, `local_model`, its
  precedence over `laya_model`, and its validation failure. The config-flow description now also
  names the category, its members, and the ecosystem index, and the `local_model` and
  `laya_model` labels name the category too. The two files are asserted byte-identical. The form
  still asks for no Cloudflare credential, and no field was added or removed.
- **Version `1.4.0`** in `pyproject.toml` and in `custom_components/jev_sentinel/manifest.json`,
  asserted equal so a release cannot ship two versions.

### Compatibility

Every name earlier releases used is kept as an alias and produces the same routing, so a
configuration that worked before this change produces the same routing decision after it:

| Earlier name | Now resolves to |
|---|---|
| `openrouter`, `jev_api` | `api_only` |
| `clef`, `clef_api` | `api_only` |
| `laya`, `laya_local` | `local_only` |
| `laya_then_hosted`, `laya_with_jev_fallback` | `local_with_api_fallback` |
| `clef_then_jev`, `clef_with_jev_fallback` | `local_with_api_fallback` |

No default mode was flipped, no setting was removed, and no existing test was weakened or
deleted. An entry created before v1.1.0 keeps the hosted route, one created before v1.3.0
keeps the default Clef checkpoint, and one created before v1.4.0 needs no `local_model`,
because its absence means `laya`.

### Notes

- **No local model other than the default has been called live.** The only verified live
  behaviour is the Laya route recorded in the release notes, captured against a real
  `laya-serve` on 2026-09-26. No local model server was running during this work and no
  credential on the verification machine is authorized for Cloudflare Workers AI, so every
  provider test is mocked or socket-captured.
- **`local_model` is not validated against a list of model names**, because the whole point
  is interchangeability: a local model published after this release must work by naming it.
  Only a value that cannot be a name is refused, with `invalid local_model`.
- **Membership of the category and the shared `/v1/systemone` request shape are documented
  claims from those projects, not measurements made here.** No live call was made to any hosted
  provider while this was written, and no local model server was running, so no local model other
  than the default has been called live.

## [1.3.0]

### Added

- `ClefJev`, a hosted decision model reached over Cloudflare Workers AI, answering the same
  typed questions in the same `answers` shape. Unlike the local route it joins the hosted
  routes: it can be selected on its own, sit in a fallback order, and lead a chain.
- The `clef` route, the `clef_then_jev` chain, and the arrangement names `clef_api` and
  `clef_with_jev_fallback`. Clef needs both `CLOUDFLARE_API_TOKEN` and
  `CLOUDFLARE_ACCOUNT_ID` and fails fast without either, naming the variable.
- Two checkpoints of one model, `clef` and `clef-flash`, selected by the `clef_model` field,
  so the checkpoint is a setting of the route rather than a provider name.
- `validate_answers`, `validate_score_answer`, and `score_index`, one typed answer
  validation shared by every route.
- Question id sanitisation through `clef_question_ids` and `clef_answers`, and both
  documented Cloudflare response envelopes.

### Changed

- `HOSTED_PROVIDERS` is `("openrouter", "clef")` and a provider counts as configured only
  when every variable it needs is present.
- `ChainedJev` falls through from a hosted hop to the next hop on the documented triggers.
- Clef credentials are read from the environment and are never stored in a config entry,
  because a config entry is written to disk in plain text. The config flow does not ask for
  them and does not forward the stored OpenRouter key to a Clef route.

### Notes

- **No live Clef call was made, and none was possible.** Every candidate token was refused
  with HTTP 401, so the route is covered by unit tests with an injected transport only.

## [1.2.1]

### Added

- A consecutive-local-failure circuit breaker on the local hop, so a local server that stays
  down cannot quietly turn every household case into remote traffic. Three consecutive
  failures that qualify for the hosted fallback still fall through; the fourth is suppressed
  and the local error is raised instead.
- The named arrangements `jev_api`, `laya_local`, and `laya_with_jev_fallback`, exported as
  `MODE_NAMES`, `MODE_ALIASES`, `PROVIDER_MODES`, `resolve_provider`, and `provider_mode`.
- Category-only logging on the decision path: a local failure logs the exception class name
  and nothing else.

### Notes

- The hosted, local, and `auto` routes are unchanged. The default route still never selects
  the local one on its own initiative.

## [1.2.0]

### Added

- `ChainedJev`, an ordered chain of named adapters that returns the answer from the first
  that answers and records the hop that answered in `Decision.raw`.
- The `laya_then_hosted` route, an explicit opt-in that answers from the local server first
  and falls through to the hosted providers that have a key.
- `is_fallback_trigger(exc)` and `FALLBACK_STATUS_CODES == frozenset({401, 403, 429})`.

### Notes

- The chain's hosted leg is covered by unit tests only. No hosted API key was available on
  the verification machine.

## [1.1.0]

### Added

- `LayaJev`, a provider that reaches a local `laya-serve` process over the same Decisions
  wire contract. It needs no credential and omits the `Authorization` header entirely.
- Provider selection in the config flow: `openrouter` or `laya`. An existing config entry
  without a `provider` field keeps the hosted route.
- A loopback rule for the local base URL: plain HTTP is accepted for `localhost`,
  `127.0.0.1`, and `::1` only.

### Changed

- The `confidence` question is a `score` question whose rubric is an ordered list of level
  descriptions, index 0 first. `Decision.confidence` is the score answer's expected legend
  index rescaled onto 0 to 1.

### Notes

- Live against `laya-serve` 0.3.20 on `127.0.0.1:8123`, 2026-09-26: a review through the
  package adapter returned `outcome: notify`, `action: light.turn_off`,
  `confidence: 0.57655`.

## [1.0.0]

### Added

- `jev_sentinel.review` for bounded case review and `jev_sentinel.verify` for deterministic
  expected-versus-actual comparisons.
- Shadow decisions with `shadow: true`, credential-shaped field redaction before provider
  submission, an allowlist and approval set in the provider-neutral policy core, and
  readback verification with matched, mismatch, unavailable, and failure paths.
- The `jev_sentinel_decision` and `jev_sentinel_verification` events, a status sensor, and a
  provider-neutral Python core for agents and local tools.

### Notes

- This release runs in shadow mode only. The Home Assistant integration does not dispatch an
  action after a recommendation.

## 1.4.1 - 2026-10-05

Two safety defects found by independent review, plus a cross-copy drift that had
allowed them. `tests/test_safety_boundaries.py` (41 tests) fails against v1.4.0.

### Fixed

- **Credential redaction missed three spellings in the Home Assistant copy.**
  `runtime.redact()` matched a literal substring list against a lowercased key, so
  `api_key` was redacted while `api-key`, `apiKey`, `api key` and `Authorization`
  were not. A secret stored under one of those names was sent to the provider in
  cleartext, because that redacted payload is what goes on the wire. Both copies
  now normalise case and separators before matching, and both use the same rule,
  so they can no longer diverge.
- **The rubric boundary only existed on the Clef route.** `OpenRouterJev.decide`
  and `LayaJev.decide` went straight from `json.loads` to `decision_from_body`
  with no `validate_answers` call, in both the package and the runtime copy. An
  off-rubric answer was accepted at whatever confidence it claimed:
  `outcome: TOTALLY_MADE_UP_OUTCOME` with `action: cover.open_garage` became a
  `Decision` at full confidence, and `confidence_from_score` silently clamped an
  out-of-range 99 to 1.0 instead of refusing it. `docs/reference.md` already
  promised answers are refused the same way whatever answered; now they are.
  Every route validates.
- **`Authorization` is now redacted as a key**, not only in text.

### Changed

- `runtime.redact()` imports the predicate from `sentinel.redaction` instead of
  keeping a second, weaker copy of the rule.

### Not changed, deliberately

- The local-failure breaker is still a module global with no cooldown, so one
  entry's dead local server can deny another entry its fallback until restart.
  That is a real defect and the fix needs a decision about per-entry keying that
  belongs in its own change with its own tests. It is recorded here rather than
  silently patched.
- `auto` mode still resolves to `api_with_local_fallback` when the config form
  injects its `local_model` default, so a user who selected `auto` and touched
  nothing gets a local hop they did not ask for. Same reasoning: a behaviour
  change, not a typo, and it needs its own release note.


## Unreleased

Two install-breaking defects, both shipped because no test covered the install
layout or the Home Assistant options-flow contract.

### Fixed

- **The integration could not load from a HACS install at all.**
  `custom_components/jev_sentinel/runtime.py` imported
  `sentinel.redaction._key_is_secret`, and `sentinel/` is declared in
  `pyproject.toml` as `packages = ["sentinel"]`, so it exists only in a pip
  install or a source checkout. `hacs.json` sets `content_in_root: false`, so
  HACS copies only `custom_components/jev_sentinel` and `sentinel/` never
  arrives. Every install raised `ModuleNotFoundError: No module named
  'sentinel'` at module load. The regression came from 1.4.1, which replaced a
  local helper with the cross-package import. `runtime.py` is self contained
  again: it imports nothing outside the standard library and `homeassistant.*`,
  and its copy of `_key_is_secret` handles `Mapping` rather than `dict` so it
  cannot drift from the package copy on that.
- **The options dialog raised on every supported install.**
  `OptionsFlowHandler.__init__` assigned `self.config_entry`. Home Assistant
  2025.6 marked the setter `breaks_in_ha_version="2025.12"` and 2025.12.0
  removed it, so on 2026.2.0 and 2026.9.0 the assignment raises
  `AttributeError: property 'config_entry' of 'OptionsFlowHandler' object has no
  setter` the moment Home Assistant builds the options flow. `hacs.json`
  declares `homeassistant: 2026.9.0`, so every install the manifest admits hit
  it. `__init__` is gone and `async_get_options_flow` builds the handler with no
  argument, which is what the 2026.9.0 base class accepts; `config_entry` is
  resolved by the base class from `handler`.

### Added

- `tests/test_hacs_install.py`, which copies the component alone into a temporary
  directory and imports it under `python -I`, so the guarantee holds without
  `pip install -e '.[test]'` having put `sentinel` on the path.
- `tests/test_config_flow_options.py`, which constructs `OptionsFlowHandler`
  against a stand-in base class carrying the real 2026.9.0 `config_entry`
  property.
- A `hacs-install-boundary` CI job that runs both without installing the package.

### Changed

- `tests/test_safety_boundaries.py` narrows six bare `pytest.raises(Exception)`
  to `ValueError`, which is what the validator raises. A blind `Exception` also
  passes on the `AttributeError` a broken transport stub produces, which is the
  failure mode this file already had once. `test_an_out_of_range_score_is_refused_not_clamped`
  also used a payload that was refused for a missing answer rather than for the
  out-of-range index it claims to test; it now sends a complete answer set with
  only the score out of range, and asserts the refusal names the index.
