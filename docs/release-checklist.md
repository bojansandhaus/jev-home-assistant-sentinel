# Public release checklist

- [ ] Core tests pass in a clean environment.
- [ ] Home Assistant config component loads under the supported Home Assistant version.
- [ ] Service schema and event payloads are documented.
- [ ] No provider key, personal entity name, private path, or raw household history is present.
- [ ] Shadow mode remains the default.
- [ ] Every execution example includes deterministic readback.
- [ ] The four modes are documented: `api_only`, `local_only`, `api_with_local_fallback`, and `local_with_api_fallback`, with the two `_only` modes documented as single-provider routes whose failures are reported and never rerouted.
- [ ] Both fallback modes are documented as explicit opt-ins, and the pre-existing hosted and local routes are re-checked as unchanged.
- [ ] `local_model` is documented as the interchangeable local decision-model slot: it is free text with no allowlist, it defaults to `laya`, and the known local models that fit the slot are listed with a source link for the interchangeable-engine claim.
- [ ] The claim that no local model other than the default has been called live is stated wherever the interchangeable local slot is documented.
- [ ] The privacy consequence of each fallback mode is stated plainly wherever it is documented: a failed first attempt sends the redacted case to the other side, and redaction runs before the first attempt. `api_with_local_fallback` is a hosted-first mode, so its case leaves the machine on every call, which is stated as well.
- [ ] The score rubric is an ordered list of levels in every copy of it, and the confidence mapping is stated with its resolution.
- [ ] The local route was exercised against a real `laya-serve`, not a mock, and the observed outcome, action, and confidence are recorded in the release notes.
- [ ] The chained route was exercised against a real `laya-serve` for its local hop, and its hosted hop is reported as unit-test coverage only when no hosted key is available.
- [ ] GitHub metadata and CI are read back after publication.
- [ ] The local failure breaker is re-checked: three consecutive local failures fall back, the fourth is suppressed with no hosted call and the local error re-raised, a healthy local call resets the counter, and the counter is documented as per process and per copy, cleared by a restart.
- [ ] Every log call on the decision path was audited, file by file, and none can carry a case, entity state, or an answer. Only an exception class name is logged.
- [ ] The four canonical modes are offered in the config flow and exported from the package API, and every earlier name, including `jev_api`, `clef_api`, `laya_local`, `laya_with_jev_fallback`, and `clef_with_jev_fallback`, is offered as an alias that resolves to the same routing.
- [ ] Every mode name is identical across the provider rules, the config flow, `strings.json`, and `translations/en.json`, and no mode appears in the UI without existing in the code or the reverse.
- [ ] An explicitly supplied credential, including an empty one, is never topped up from the process environment, in every adapter that resolves a credential.
- [ ] The Clef checkpoint is offered as a field of the route, not as a provider name, and `clef-flash` cannot be selected as a route.
- [ ] Every environment variable a route reads is documented, and no credential is written to a config entry, a file, a log, or a release note.
- [ ] Any mode without a live verified call is reported as unit-test coverage only, with the reason stated plainly. This includes every local model other than the default.
- [ ] The DOGA 100-question benchmark numbers are cited as the headline quality evidence wherever local classifier quality is discussed, together with the limit that the breaker cannot detect a valid yet incorrect local judgment.
- [ ] The note that the hosted endpoint's acceptance of the five-level confidence rubric is unverified is still carried.
- [ ] No documentation writes the banned two-word phrase for the local classifier, and no document uses one vendor's member as the category name. The models are described as members of the System One decision model category, with Laya named as the local model and Jev as a hosted model.
- [ ] The category term is introduced once per document on first mention, as "System One decision model" or "typed decision model", and is linked to https://systemonemodels.org/guides/what-is-a-system-one-model/ .

The ecosystem index at https://systemonemodels.org/ catalogues further members of the category, both open-weight and hosted. This checklist names only the members this repository reaches.
- [ ] The repository topic tags are listed in the README and match the remote topics, so tags and docs agree.
- [ ] The honest limit is stated wherever the category is documented: membership and the shared wire contract are documented claims from those projects, not measurements made here, and no live call was made to any provider while this was written.
- [ ] The user-facing strings name the category, and `strings.json` and `translations/en.json` remain byte-identical, with no field added or removed and no Cloudflare credential field in the form.
