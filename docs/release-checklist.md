# Public release checklist

- [ ] Core tests pass in a clean environment.
- [ ] Home Assistant config component loads under the supported Home Assistant version.
- [ ] Service schema and event payloads are documented.
- [ ] No provider key, personal entity name, private path, or raw household history is present.
- [ ] Shadow mode remains the default.
- [ ] Every execution example includes deterministic readback.
- [ ] Both provider routes are documented as alternatives: hosted Jev over an API key, or Laya locally with no key.
- [ ] The score rubric is an ordered list of levels in every copy of it, and the confidence mapping is stated with its resolution.
- [ ] The local route was exercised against a real `laya-serve`, not a mock, and the observed outcome, action, and confidence are recorded in the release notes.
- [ ] GitHub metadata and CI are read back after publication.
