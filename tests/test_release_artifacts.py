import json
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_release_metadata_is_complete():
    hacs = json.loads((ROOT / "hacs.json").read_text())
    manifest = json.loads(
        (ROOT / "custom_components/jev_sentinel/manifest.json").read_text()
    )
    pyproject = (ROOT / "pyproject.toml").read_text()
    # Home Assistant reads the manifest version and the package reads the
    # pyproject one, so a release that bumps only one of them ships two
    # different versions. The invariant is that they agree, not any particular
    # number, so this parses both and compares them. It used to hardcode
    # "1.4.0", which made every version bump a three-way edit across two files
    # and a test, and had already been missed once.
    pyproject_version = re.search(r'^version = "([^"]+)"', pyproject, re.M)
    assert pyproject_version, "pyproject.toml declares no version"
    assert manifest["version"] == pyproject_version.group(1), (
        f"manifest says {manifest['version']} and pyproject says "
        f"{pyproject_version.group(1)}; a release must bump both together"
    )
    assert hacs == {
        "name": "Jev Home Assistant Sentinel",
        "render_readme": True,
        "homeassistant": "2026.9.0",
        "content_in_root": False,
        "zip_release": False,
    }
    assert manifest["integration_type"] == "service"
    assert manifest["domain"] == "jev_sentinel"


def test_release_artifacts_exist_and_are_pngs():
    for name in ("icon.png", "logo.png"):
        payload = (ROOT / "custom_components/jev_sentinel/brand" / name).read_bytes()
        assert payload.startswith(b"\x89PNG\r\n\x1a\n")


def test_validation_workflows_use_official_actions():
    """Third-party actions are pinned to a commit SHA, and the test gate runs.

    ``hassfest@master`` and ``hacs/action@main`` are mutable refs: anyone who
    can push to either repository changes what validates every pull request in
    this one, with no change in this repository's history. A full 40-hex SHA
    cannot be repointed. Neither workflow ran the test suite either, so a merge
    could be "validated" while its own tests were failing.
    """
    import re

    hass = (ROOT / ".github/workflows/hassfest.yaml").read_text()
    hacs = (ROOT / ".github/workflows/validate.yaml").read_text()
    # The actions are still the official ones, from the official accounts.
    assert "home-assistant/actions/hassfest@" in hass
    assert "hacs/action@" in hacs
    assert "category: integration" in hacs
    # Neither is pinned to a branch, a tag, or a partial SHA.
    for action, source in (
        ("home-assistant/actions/hassfest", hass),
        ("hacs/action", hacs),
    ):
        for ref in re.findall(rf"{re.escape(action)}@([^\s]+)", source):
            assert re.fullmatch(
                r"[0-9a-f]{40}", ref
            ), f"{action} is pinned to {ref!r}, which is not a commit SHA"
    for workflow in (hass, hacs):
        assert (
            "@main" not in workflow and "@master" not in workflow
        ), "a mutable default-branch pin survived"
    # Both validation workflows run the test suite themselves. A `needs: core`
    # here would not work: GitHub's `needs` cannot name a job in a different
    # workflow file, so the hassfest and HACS workflows failed at validation the
    # first time this release tried it. Running the suite in place is the only
    # form of that dependency these workflows can carry.
    for name, workflow in (("hassfest.yaml", hass), ("validate.yaml", hacs)):
        assert "python -m pytest" in workflow, name
        assert "setup-python" in workflow, name
    # And the core workflow still runs the suite itself.
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "python -m pytest" in ci, "the core workflow no longer runs the tests"
    for gate in (
        "python -m black --check sentinel custom_components tests",
        "python -m isort --check-only sentinel custom_components tests",
        "python -m compileall -q sentinel custom_components tests",
        "git diff --check",
    ):
        assert gate in ci, gate


def test_translations_offer_every_mode_the_code_accepts():
    """A mode in the code but not in the form, or the reverse, is a defect."""
    import ast

    flow = (ROOT / "custom_components/jev_sentinel/config_flow.py").read_text()
    strings = json.loads(
        (ROOT / "custom_components/jev_sentinel/strings.json").read_text()
    )
    translations = json.loads(
        (ROOT / "custom_components/jev_sentinel/translations/en.json").read_text()
    )
    # Home Assistant serves the form from the translations, so the two files must
    # be the same text or a user's form and a user's HA UI disagree.
    assert strings == translations
    data = strings["config"]["step"]["user"]["data"]
    # Every field the form declares has a label, and the labels the user reads
    # are the field names the runtime reads.
    runtime = (ROOT / "custom_components/jev_sentinel/runtime.py").read_text()
    assert 'LOCAL_MODEL_FIELD = "local_model"' in runtime
    assert "local_model" in data
    assert "laya_model" in data, "the deprecated field still needs a label"
    assert "clef_model" in data
    # The four canonical mode names and every alias are named in the prose the
    # user reads, because the selector's own labels are assembled in the flow.
    description = strings["config"]["step"]["user"]["description"]
    for mode in (
        "api_with_local_fallback",
        "api_only",
        "local_only",
        "local_with_api_fallback",
    ):
        assert mode in description, mode
    for alias in (
        "jev_api",
        "laya_local",
        "laya_with_jev_fallback",
        "clef_api",
        "clef_with_jev_fallback",
    ):
        assert alias in description, alias
    # Home Assistant's hassfest rejects a URL in any user-facing string and asks
    # for a description placeholder instead. The category link belongs in the
    # documentation, so this asserts the form text never carries one. It failed
    # CI once already, when the link was added to this description.
    for section in ("user", "data", "data_description"):
        block = strings["config"]["step"].get(section, {})
        for key, value in block.items():
            if isinstance(value, str):
                assert (
                    "http://" not in value and "https://" not in value
                ), f"config.step.user.{section}.{key} carries a URL, which hassfest rejects"
    # The error keys the flow raises must exist in the translations.
    errors = strings["config"]["error"]
    assert "api_key_required" in errors
    assert "local_model_invalid" in errors
    for key in errors:
        assert key in flow, key
    # The form is never asked for a Cloudflare credential: the two variable names
    # appear in the prose that says where they come from, never as a field. This
    # reads the parsed schema rather than matching wrapped lines of text.
    # A field may be declared as a literal or as a shared constant, so both
    # spellings are read and the constant is resolved from the runtime.
    form_fields, form_constants = set(), set()
    for node in ast.walk(ast.parse(flow)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"Optional", "Required"}
            and node.args
        ):
            continue
        argument = node.args[0]
        if isinstance(argument, ast.Constant):
            form_fields.add(argument.value)
        elif isinstance(argument, ast.Name):
            form_constants.add(argument.id)
    assert "LOCAL_MODEL_FIELD" in form_constants, form_constants
    assert "CLEF_CHECKPOINT_FIELD" in form_constants, form_constants
    runtime = (ROOT / "custom_components/jev_sentinel/runtime.py").read_text()
    # The shared constants are where the spelling is decided, so the form and
    # the entry builder cannot drift apart on it.
    assert 'LOCAL_MODEL_FIELD = "local_model"' in runtime
    assert 'CLEF_CHECKPOINT_FIELD = "clef_model"' in runtime
    for field in ("provider", "api_key", "laya_base_url", "laya_model"):
        assert field in form_fields, field
    for variable in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"):
        assert variable not in form_fields, variable
    # The form's selector carries all four canonical modes and every alias.
    for mode in (
        "api_with_local_fallback",
        "api_only",
        "local_only",
        "local_with_api_fallback",
        "jev_api",
        "laya_local",
        "laya_with_jev_fallback",
        "clef_api",
        "clef_with_jev_fallback",
        "clef_with_local_fallback",
        "auto",
        "laya_then_hosted",
        "clef_then_jev",
    ):
        assert mode in flow, mode


def test_service_schema_uses_structured_readback_values():
    schema = (ROOT / "custom_components/jev_sentinel/services.yaml").read_text()
    assert "entity:" in schema
    assert "multiple: true" in schema
    assert "jev_sentinel_verification" not in schema
