# Contributing

Contributions should keep the boundary legible: a recommendation is separate from authorization, a dispatched command is separate from readback, and unknown evidence stays unknown.

## Report an issue

Open a GitHub issue with:

- a short, specific title;
- the repository revision and Home Assistant version;
- the service, event, or Python method involved;
- a minimal reproduction;
- expected and observed behavior;
- sanitized logs or tracebacks.

Remove API keys, tokens, personal data, entity details that identify your home, and private provider responses. Use the repository's security reporting path for a suspected vulnerability rather than publishing sensitive details in an issue.

## Pull requests

1. Fork the repository and create a focused branch.
2. Add or update tests for behavior changes.
3. Keep public schemas, service examples, and documentation aligned with source.
4. Run the local checks below.
5. Open a pull request that states the user-visible change, the verification performed, and any known limitation.

Do not claim active execution when the change only emits a recommendation event. Do not treat a successful dispatch as verified state.

## Local setup

The project targets Python 3.10 or newer.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
```

## Required local checks

Run the repository test suite and syntax checks:

```bash
python -m pytest
python -m compileall -q sentinel custom_components tests
python -m json.tool custom_components/jev_sentinel/manifest.json >/dev/null
python -m json.tool hacs.json >/dev/null
```

The test extra installs the required formatters in the isolated environment. Run:

```bash
python -m black --check sentinel custom_components tests
python -m isort --check-only sentinel custom_components tests
```

Type hints are expected for new public Python interfaces. Keep imports ordered and formatting compatible with Black and isort.

## Hassfest and HACS validation

The authoritative checks run in GitHub Actions:

- `.github/workflows/hassfest.yaml` runs `home-assistant/actions/hassfest@master`.
- `.github/workflows/validate.yaml` runs `hacs/action@main` with `category: integration`.

The authoritative Hassfest and HACS checks run in GitHub Actions. Locally, validate the same repository metadata before pushing:

```bash
python -m json.tool hacs.json >/dev/null
python -m json.tool custom_components/jev_sentinel/manifest.json >/dev/null
python -m compileall -q sentinel custom_components tests
```

Do not report Hassfest or HACS as passed until their official action output exists. Docker is optional locally; it is not required for the local Python validator commands above.

## Quality bar

Every pull request must pass:

- the Python test suite;
- Black and isort checks;
- JSON validation for integration metadata;
- Hassfest validation;
- HACS validation;
- documentation review for exact service names, fields, event names, and limitations.

A change that adds active execution must include an explicit approval model, allowlist behavior, dispatch failure handling, readback behavior, and tests for uncertainty. A change that alters the provider payload must update the reference documentation and redaction tests.

## Documentation rules

Document what the source does. Include exact YAML and JSON shapes where users copy them. State source discrepancies plainly. Keep the README's installation path and safety boundary current.
