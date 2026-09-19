# Release notes

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
