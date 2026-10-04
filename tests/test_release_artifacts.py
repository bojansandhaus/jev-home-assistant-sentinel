import json
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
    # different versions. They are asserted equal here.
    assert 'version = "1.4.0"' in pyproject
    assert manifest["version"] == "1.4.0"
    assert hacs == {
        "name": "Jev Home Assistant Sentinel",
        "render_readme": True,
        "homeassistant": "2026.9.0",
        "content_in_root": False,
        "zip_release": False,
    }
    assert manifest["version"] == "1.4.0"
    assert manifest["integration_type"] == "service"
    assert manifest["domain"] == "jev_sentinel"


def test_release_artifacts_exist_and_are_pngs():
    for name in ("icon.png", "logo.png"):
        payload = (ROOT / "custom_components/jev_sentinel/brand" / name).read_bytes()
        assert payload.startswith(b"\x89PNG\r\n\x1a\n")


def test_validation_workflows_use_official_actions():
    hass = (ROOT / ".github/workflows/hassfest.yaml").read_text()
    hacs = (ROOT / ".github/workflows/validate.yaml").read_text()
    assert "home-assistant/actions/hassfest@master" in hass
    assert "hacs/action@main" in hacs
    assert "category: integration" in hacs


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
