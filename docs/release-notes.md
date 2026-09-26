# Release notes

## v1.2.1

Adds the local-failure circuit breaker, the three named arrangements, and category-only logging, porting the behaviour the DOGA fork shipped in its v1.3.0. The route surface is unchanged: hosted Jev, local Laya, and the opt-in chain are still the same three routes, and every one of them behaves as before until the breaker trips.

### Added

- A consecutive-local-failure circuit breaker on the local hop. Each local failure that qualified for the hosted fallback increments a counter; while the counter is at or below `LOCAL_FALLBACK_FAILURE_LIMIT`, which is 3, the hosted fallback is attempted; past 3 the fallback is suppressed, a warning is logged, and the local error is re-raised instead of being answered remotely. The counter is per process and resets on restart, and any successful local call resets it to zero, on the chained route and on the local-only route alike.
- The three named arrangements in DOGA's vocabulary: `jev_api`, `laya_local`, and `laya_with_jev_fallback`. They are offered in the Home Assistant config flow and exported from the package API as `MODE_NAMES`, `MODE_ALIASES`, `PROVIDER_MODES`, `resolve_provider`, and `provider_mode`.
- `LOCAL_FALLBACK_FAILURE_LIMIT`, `local_failure_count`, `reset_local_failures`, and `note_local_failure` in the package, and the same four names in the Home Assistant runtime bridge.
- Category-only logging on the decision path. A local failure logs the exception class name and nothing else.

### Changed

- `provider_order` and `build_provider` resolve one of the three arrangement names onto its canonical route before validating, so `jev_api` and `openrouter`, `laya_local` and `laya`, and `laya_with_jev_fallback` and `laya_then_hosted` behave identically. No other value is rewritten: `Laya` and `typesafe` are still rejected with `ValueError("invalid provider")` exactly as before.
- The config flow offers the three arrangements next to their canonical route aliases, and its key check runs against the resolved route, so `jev_api` and `laya_with_jev_fallback` require a key while `laya_local` does not.
- The hosted route, the local route, and `auto` are untouched. An existing config entry with no `provider` field still keeps the hosted route, and an entry that stored `openrouter` keeps it.
- Documentation covers the breaker, the named arrangements, and the benchmark evidence in the README, the reference, the integration guide, the FAQ, and the release checklist.

### Evaluation and limitations

- The headline quality evidence is the DOGA fork's 100-question, three-mode benchmark, run against DOGA v1.2.0 behaviour in a fresh process. On 100 authored, subjective labels, Laya local agreed with the labels on **goal 56/100 against Jev's 88/100**, **mode 41 against 68**, **stakes 37 against 67**, and **high-versus-low ambiguity 67 against 87**. Laya detected **no high-ambiguity labels at the 0.7 threshold** in 30 authored cases, against 21 of 30 for Jev. Both used a valid, well-formed answer in every case and injected a contract in all 100. Those labels are subjective and were authored before the Laya comparison, so this is a classifier agreement study, not a population accuracy estimate and not a final-answer quality study.
- The breaker bounds repeated remote egress after local errors, but it cannot detect a valid yet incorrect local judgment. A Laya answer that is wrong but well formed is a success: it is returned as the decision and it resets the counter. Switch to `jev_api`, or stay on `laya` where the case never leaves the machine, rather than treating the trip threshold as a quality gate.
- The hosted endpoint's acceptance of the five-level confidence rubric, and its own `score` scale, remain unverified. No hosted API key was available on the verification machine, so the hosted leg of the chained route is covered by unit tests with an injected transport only. Carried from v1.1.0 and v1.2.0.
- The counter is per process. A restart clears it, so a crash loop that restarts the process repeatedly would keep re-arming up to three fallbacks each time. It is also per copy: the package and the Home Assistant runtime bridge hold their own counter, because Home Assistant loads the bridge without installing the package.
- The plain local route never reaches the hosted provider, so the breaker only ever bounds egress on the chained route. On the local route the counter still resets on success, matching the hosted-fallback path.

### Verification

- Full local suite: 57 passed, 3 skipped, with the three live tests gated behind `JEV_SENTINEL_LIVE_LAYA`. With the live server on `127.0.0.1:8123`: 60 passed.
- Formatting and static gate: `python -m black --check sentinel custom_components tests`, `python -m isort --check-only sentinel custom_components tests`, `python -m compileall -q sentinel custom_components tests`, `python -m json.tool` on `hacs.json` and the manifest, and `git diff --check` all pass.
- The breaker is proved with an injected synthetic transport: three consecutive local failures each fall through to the hosted hop, the fourth raises the local error with no hosted request, and the recorded attempt list shows only the local URL on that fourth call. A healthy local call resets a tripped counter to zero on both the chained and the local-only route. A weak and a confidently wrong local answer are both returned as they stand, over four consecutive calls, with no hosted attempt and a counter that stays at zero. The same suppression is asserted in both copies of the provider rules.
- The log audit is proved by capture: the failure warning contains the class name `RuntimeError` and never the message text, and the suppression warning never contains a case marker.
- Live against `laya-serve`, English checkpoint, CPU, on `127.0.0.1:8123`, 2026-09-26: a review through `sentinel.LayaJev` returned `outcome: notify`, `action: light.turn_off`, `confidence: 0.602025`; one through the Home Assistant runtime bridge returned `outcome: recommend`, `action: light.turn_off`, `confidence: 0.648825`; and one through the chained route answered from the local hop with `outcome: notify`, `action: light.turn_off`, `confidence: 0.566225`, `raw: {"model": "laya-rl-agent", "provider": "laya", "attempted": ["laya"]}`. These are the values from one recorded run; the local score varies between runs, so treat a single `confidence` as a sample and not a stable measurement.

## v1.2.0

Adds the third way to run the decision step, as an explicit opt-in. The first two are unchanged and still alternatives to each other: hosted Jev over an OpenRouter API key, or Laya on this machine with no API key. `laya_then_hosted` answers from the local server first and falls through to the hosted providers that have a key.

### Added

- `ChainedJev`, an ordered chain of named adapters that returns the answer from the first adapter that answers and records the hop that answered in `Decision.raw`.
- The `laya_then_hosted` route in the config flow, next to `openrouter` and `laya`, and in the package API through `CHAINED_PROVIDER`, `provider_order`, and `build_provider`.
- `is_fallback_trigger(exc)` and `FALLBACK_STATUS_CODES == frozenset({401, 403, 429})`. A fallback is taken on a transport error, a timeout, or an HTTP 401, 403, 429, or 5xx from the earlier hop. Any other reply, such as 400, 404, or 422, and any malformed answer, propagates unchanged.
- Decision attribution on the chained route: `raw.provider` names the hop that answered and `raw.attempted` lists the hops that were tried, in order.

### Changed

- `provider_order("laya_then_hosted")` resolves to `["laya", "openrouter"]` with a hosted key, and raises `ValueError("missing OPENROUTER_API_KEY for the laya_then_hosted route")` without one. The route fails fast rather than degrading into the local-only route under another name.
- `validate_fallback_order` still rejects every non-hosted name, and a route name is now covered explicitly: `("laya_then_hosted",)` raises `ValueError("invalid fallback_order")`, like `("laya",)`, because a chain is not a member of a chain.
- `build_provider` returns `ChainedJev` on the chained route. On that route `api_key` is the hosted key, and the local hop keeps its own optional `LAYA_API_KEY`.
- The plain hosted and plain local routes are unchanged. `raw` on those routes still carries only `{"model": ...}`, `auto` still never selects the local route, and an existing config entry without a `provider` field still keeps the hosted route.
- Documentation covers the route in the README, the reference, the integration guide, the FAQ, and the release checklist.

### Verification

- The full suite passes, including new tests for route resolution with one hosted key, with an extra `TYPESAFE_API_KEY` present, and with none; for a local success that never reaches the hosted hop; for a local failure that falls through and records `raw.provider: openrouter`; for the unchanged hosted and local routes; and for drift between the two copies of the provider rules.
- The fallback is proved with an injected synthetic transport that fails on the local URL and records the hosted attempt. The covered triggers are connection refused, a DNS failure, a timeout, and HTTP 401, 403, 429, 500, and 503. An HTTP 422 does not fall through, and the error from the last hop propagates when every hop fails.
- Live against `laya-serve` 0.3.20, English checkpoint, CPU, on `127.0.0.1:8123`, 2026-09-26: a review through `sentinel.LayaJev` returned `outcome: notify`, `action: light.turn_off`, `confidence: 0.573825`; one through the Home Assistant runtime bridge returned `outcome: recommend`, `action: light.turn_off`, `confidence: 0.63925`; and one through the new chained route answered from the local hop with `outcome: notify`, `action: light.turn_off`, `confidence: 0.53485`, `raw: {"model": "laya-rl-agent", "provider": "laya", "attempted": ["laya"]}`. Those are the values from one recorded run; the local score varies between runs, so treat a single `confidence` as a sample and not a stable measurement.
- Live fallback probe: with the chained route pointed at a port where nothing listens, the real local hop raised connection refused, the hosted hop was attempted, and the real hosted endpoint answered HTTP 401 to a synthetic key. That shows the handoff fires over real sockets, and that the hosted leg cannot succeed on this machine.

### Known limitations

- The hosted leg of the chained route is covered by unit tests only. No hosted API key was available on the verification machine, so the hosted hop was never observed answering. The live probe above shows the hosted endpoint is reached and refuses a synthetic key.
- The hosted endpoint's acceptance of the five level list rubric, and its own `score` scale, remain unverified. This is carried over from v1.1.0.
- **Privacy.** On the chained route a failed local attempt sends the redacted case to a hosted API. Redaction runs before the first attempt, so both hops receive the same redacted case and credential-shaped fields never leave the machine, but the rest of the case does leave the machine when the local server fails. `laya` remains the only route that never leaves the machine.
- The route carries no cooldown and no retry: each hop is tried once, in order. This repository has never had a cooldown and none was added, so the timing behaviour of the two existing routes is unchanged.
- The chain follows the repository's hosted order, which has one member, `openrouter`, serving the TypeSafe `typesafe/jev-1.13` Jev model. A direct TypeSafe endpoint is still not implemented, and a `TYPESAFE_API_KEY` adds no hop.

### Distribution note

The repository can be installed through HACS as a custom repository. HACS default-list submission and remote metadata changes are distribution operations outside these release notes.

## v1.1.0

Adds a second route and fixes the score rubric behind it. There are now exactly two mutually exclusive ways to run the decision step: Jev over a hosted API key, or Laya running locally with no API key at all. The local route replaces the hosted route for a profile. It does not extend it.

### Added

- `LayaJev`, a provider that reaches a local `laya-serve` process over the same Decisions wire contract. It needs no credential, and it omits the `Authorization` header entirely rather than sending an empty one.
- Provider selection in the config flow: `openrouter` (hosted, API key) or `laya` (local, no key). An existing config entry without a `provider` field keeps the hosted route.
- `sentinel.providers` with `provider_order`, `validate_fallback_order`, and `build_provider`. The Home Assistant runtime bridge carries the same three functions so it stays installable without the package.
- A loopback rule for the local base URL. Plain HTTP is accepted for `localhost`, `127.0.0.1`, and `::1` only; any other host needs HTTPS, which keeps household case data off cleartext remote links.
- Local route defaults: `http://127.0.0.1:8000`, path `/v1/systemone`, model `convaiinnovations/laya`, timeout 120 s. The hosted 30 s default is too short for a cold local server.

### Changed

- The `confidence` question is a `score` question, and a score rubric is an ordered list of level descriptions with index 0 first. The previous `{"min": 0, "max": 1}` mapping is not a valid score rubric: a local Laya server rejects it with `question 'confidence': a score question takes 'criteria' as a list of level descriptions, index 0 first`. The rubric is now a five level list, in both `sentinel/jev.py` and `custom_components/jev_sentinel/runtime.py`.
- `Decision.confidence` is the score answer's expected legend index rescaled onto 0 to 1. Level 0 maps to 0.0 and the last level maps to 1.0, so adjacent levels sit 0.25 apart on a five level rubric. The value is a quantized ordinal estimate, not a calibrated probability, and the raw index stays recoverable by multiplying by four. Nothing in this repository compares confidence against a threshold, so no policy decision changes with the scale.
- The two copies of the rubric are now byte-identical, and a test fails on drift between them.
- The `outcome` and `action` question wording in the Home Assistant bridge is aligned with the package copy.

### Verification

Live against `laya-serve` 0.3.20, English checkpoint, CPU, on `127.0.0.1:8123`, 2026-09-26:

- One review through `sentinel.LayaJev` and one through the Home Assistant runtime bridge, both against the real server. First: `outcome: notify`, `action: light.turn_off`, `confidence: 0.57655`, raw model `laya-rl-agent`. Second: `outcome: recommend`, `action: light.turn_off`, `confidence: 0.6124`. Latency was about 2.5 s per call.
- The rejected dict rubric returns HTTP 422 with the message quoted above. The five level list rubric returns HTTP 200 with `score: 2.011`, a `legend` of `"0"` to `"4"`, and probabilities that sum to about 1.0.

### Known limitations

- Local scoring accuracy on real Home Assistant cases is unmeasured. The upstream project reports weak ordinal scoring on its base checkpoints, so treat a local `confidence` as a hint, not a measurement.
- The hosted endpoint's acceptance of the five level list rubric was not re-verified in this pass, because no hosted API key was available on the verification machine. The hosted answer's `score` scale is likewise unverified. The mapping above is applied on both routes so the meaning of `confidence` stays identical, but the hosted side needs one live review to confirm it.
- A local review is bounded by the server, not by Sentinel. A `laya-serve` started without `LAYA_API_KEY` accepts any request on the host, so keep it on loopback.

### Distribution note

The repository can be installed through HACS as a custom repository. HACS default-list submission and remote metadata changes are distribution operations outside these release notes.

## v1.0.0

Jev Home Assistant Sentinel provides a safety boundary for Home Assistant decisions. It keeps the recommendation, the authorization, the command, and the observed state in separate records.

### Included

- `jev_sentinel.review` for bounded case review through OpenRouter.
- `jev_sentinel.verify` for deterministic expected-versus-actual comparisons.
- Shadow decisions with `shadow: true`.
- OpenRouter adapter for `typesafe/jev-1.13`.
- Credential-shaped field redaction before provider submission.
- An allowlist and approval set in the provider-neutral policy core.
- Approval-gate support in `SentinelWorkflow.execute`.
- Readback verification with matched, mismatch, unavailable, and failure paths.
- `jev_sentinel_decision` and `jev_sentinel_verification` events.
- A status sensor for the latest decision outcome.
- A provider-neutral Python core for agents and local tools.

### Install

Install the repository as a HACS custom repository, choose **Integration**, restart Home Assistant, and add the integration from **Settings > Devices & services**. Manual installation is also supported by copying `custom_components/jev_sentinel` into the Home Assistant configuration directory.

### Requirements

- Home Assistant 2026.9 or newer as the project target.
- An OpenRouter API key for the Home Assistant `review` service.
- Python 3.10 or newer for the standalone package and test suite.

### Known limitations

This release runs in shadow mode only. The Home Assistant integration does not dispatch an action after a recommendation. It emits an event for a separate consumer to inspect, approve, dispatch, and verify. There is no built-in polling, delayed readback, durable decision ledger, local decision provider, or active execution consumer.

The config flow exposes a `shadow` option, but the adapter remains shadow-only regardless of its value. The runtime always marks decisions as shadow decisions. This is a source-level limitation of v1.0.0.

### Future scope

Future work may add an active execution consumer with explicit audit and retry rules, a local decision provider, and an expanded policy engine. Those features require new source and new validation. They are not part of v1.0.0.

### Distribution note

The repository can be installed through HACS as a custom repository immediately. HACS default-list submission, a GitHub release, and remote metadata changes are distribution operations outside these local release notes.
