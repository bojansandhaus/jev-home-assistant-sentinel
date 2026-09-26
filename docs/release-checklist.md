# Public release checklist

- [ ] Core tests pass in a clean environment.
- [ ] Home Assistant config component loads under the supported Home Assistant version.
- [ ] Service schema and event payloads are documented.
- [ ] No provider key, personal entity name, private path, or raw household history is present.
- [ ] Shadow mode remains the default.
- [ ] Every execution example includes deterministic readback.
- [ ] Both provider routes are documented as alternatives: hosted Jev over an API key, or Laya locally with no key.
- [ ] The third route, `laya_then_hosted`, is documented as an explicit opt-in, and the existing hosted and local routes are re-checked as unchanged.
- [ ] The privacy consequence of the chained route is stated plainly wherever the route is documented: a failed local attempt sends the redacted case to a hosted API, and redaction runs before the first attempt.
- [ ] The score rubric is an ordered list of levels in every copy of it, and the confidence mapping is stated with its resolution.
- [ ] The local route was exercised against a real `laya-serve`, not a mock, and the observed outcome, action, and confidence are recorded in the release notes.
- [ ] The chained route was exercised against a real `laya-serve` for its local hop, and its hosted hop is reported as unit-test coverage only when no hosted key is available.
- [ ] GitHub metadata and CI are read back after publication.
