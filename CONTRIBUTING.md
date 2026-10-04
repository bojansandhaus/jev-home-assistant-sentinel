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

The live local route is opt in, because CI has no model server. Start `laya-serve` and point the test at it:

```bash
JEV_SENTINEL_LIVE_LAYA=http://127.0.0.1:8000 JEV_SENTINEL_LIVE_LAYA_MODEL=english python -m pytest tests/test_laya_provider.py -v
```

Those two tests make a real request through the package adapter and through the Home Assistant runtime bridge. They skip when the variable is unset, so a mock never stands in for a live answer.

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

The provider contract exists twice: in `sentinel/` and in `custom_components/jev_sentinel/runtime.py`, because Home Assistant installs the component without the package. Change both, and let `tests/test_laya_provider.py` prove they still agree on the rubric, the mode resolutions and provider orders, the local-model acceptance and refusal, and the confidence mapping. Provider changes must keep the two single-provider modes single-provider: `api_only` and `local_only` return one provider each, `auto` never selects the local slot on its own initiative, and the hosted order keeps rejecting it. The two fallback modes are the only modes that reach both sides, and each fails fast when it has no usable provider for the side it promises.

There are four decision modes, and a mode name must be identical across the provider rules, the config flow, `strings.json`, and `translations/en.json`. A name in the UI that does not exist in the code, or the reverse, is a defect; `tests/test_release_artifacts.py` parses the form's schema and asserts it.

The models that answer the decision step are **System One decision models**, also written *typed decision model*: a model that returns typed values, each carrying a probability, rather than prose. See [what is a System One model](https://systemonemodels.org/guides/what-is-a-system-one-model/) and the ecosystem index at https://systemonemodels.org/. Jev is one member of that category, not the name of it, so never document the category by one vendor's member and never write "Jev-like model" as a category name.

The local provider is a generic local decision-model slot named `laya`, and `local_model` selects which System One decision model answers. Do not add an allowlist of model names and do not add a provider name per engine: the slot is interchangeable by configuration, so a local model published after this release must work with no code change. Refuse only a value that cannot be a name.

An explicitly supplied credential, including an empty one, is never topped up from the process environment, in every adapter that resolves one. An empty credential is a missing credential. A constructor that falls through to `os.environ` on an empty value leaks the machine's real key into whatever the caller was trying to keep out.

## Documentation rules

Document what the source does. Name the System One decision model category once per document, on first mention, with its link. Include exact YAML and JSON shapes where users copy them. State source discrepancies plainly. Keep the README's installation path and safety boundary current.
