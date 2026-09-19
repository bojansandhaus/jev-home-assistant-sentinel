import json
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_release_metadata_is_complete():
    hacs = json.loads((ROOT / "hacs.json").read_text())
    manifest = json.loads((ROOT / "custom_components/jev_sentinel/manifest.json").read_text())
    assert hacs == {
        "name": "Jev Home Assistant Sentinel",
        "render_readme": True,
        "homeassistant": "2026.9.0",
        "content_in_root": False,
        "zip_release": False,
    }
    assert manifest["version"] == "1.0.0"
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


def test_service_schema_uses_structured_readback_values():
    schema = (ROOT / "custom_components/jev_sentinel/services.yaml").read_text()
    assert "object: {}" in schema
    assert "jev_sentinel_verification" not in schema
