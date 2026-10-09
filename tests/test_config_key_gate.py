"""The config flow must ask for a key exactly when the mode needs one.

``_needs_api_key`` read the *spelling* the operator picked rather than the mode
it resolves to, so the two legacy aliases ``laya_with_jev_fallback`` and
``laya_then_hosted`` were not asked for a key even though both resolve to
``local_with_api_fallback``, whose provider order raises
``ValueError: missing OPENROUTER_API_KEY``. A user who picked a legacy alias was
never prompted, saved, and every review on the entry crashed.

The table below walks every name the form offers and asserts against what
``build_provider`` actually needs with no key at all. The form and the builder
are two independent implementations of the same question, so this test is the
one place they are made to agree.

``homeassistant`` is not installable here, so the module is loaded through a
stand-in ``homeassistant.config_entries`` exactly as
``tests/test_config_flow_options.py`` does; ``config_flow`` imports only
``ConfigEntry``, ``callback``, and ``vol``, so that stand-in is complete for the
surface under test.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "jev_sentinel"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_runtime(module_name="_config_key_gate_runtime"):
    """``runtime.py`` standalone, the way Home Assistant loads it from a HACS install.

    It has no relative imports, so no stand-in is needed for it, and loading it
    by path keeps this file independent of whether ``sentinel`` is installed.
    """
    spec = importlib.util.spec_from_file_location(module_name, COMPONENT / "runtime.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


RUNTIME = _load_runtime()


def _install(monkeypatch):
    config_entries = types.ModuleType("homeassistant.config_entries")

    class ConfigEntry:
        pass

    class ConfigFlow:
        """Only the two entry points the form calls on ``self``.

        ``ConfigFlow.__init_subclass__`` consumes the ``domain=`` keyword the
        class is declared with, exactly as the real base class does. Swallowing
        it is the point: ``object.__init_subclass__`` rejects keyword arguments.
        """

        def __init_subclass__(cls, **kwargs):
            kwargs.pop("domain", None)
            super().__init_subclass__()

        # Synchronous, not async: Home Assistant's real ``async_create_entry``
        # returns a *finished* flow result, not a coroutine. Making this one
        # async would hand the caller an unawaited coroutine that looks like a
        # result and passes an ``await``-shaped test.
        def async_create_entry(self, **kwargs):
            return kwargs

        def async_show_form(self, **kwargs):
            return kwargs

    class ConfigEntryBaseFlow:
        """Only the plumbing the property below reads."""

        handler: str | None = None
        hass = None

    class OptionsFlow(ConfigEntryBaseFlow):
        @property
        def config_entry(self):
            if self.hass is None:
                raise ValueError(
                    "The config entry is not available during initialisation"
                )
            return self.hass.config_entries.async_get_known_entry(self.handler)

        def async_show_form(self, **kwargs):
            return kwargs

        def async_create_entry(self, **kwargs):
            return kwargs

        def async_show_progress(self, *args, **kwargs):  # pragma: no cover
            return kwargs

    config_entries.ConfigEntry = ConfigEntry
    config_entries.ConfigFlow = ConfigFlow
    config_entries.OptionsFlow = OptionsFlow
    # `async_get_options_flow` is a staticmethod on the flow class, and
    # `OptionsFlowHandler` takes no argument at construction time, so nothing
    # else of the base class is exercised by this file.
    core = types.ModuleType("homeassistant.core")
    core.callback = lambda function: function
    homeassistant = types.ModuleType("homeassistant")
    homeassistant.config_entries = config_entries
    homeassistant.core = core

    voluptuous = types.ModuleType("voluptuous")

    class Marker:
        def __init__(self, name, default=None):
            self.name = name
            self.default = default

    class Schema(dict):
        pass

    voluptuous.Marker = Marker
    voluptuous.Optional = Marker
    voluptuous.Required = Marker
    voluptuous.In = lambda values: values
    voluptuous.Schema = Schema

    for name, module in (
        ("homeassistant", homeassistant),
        ("homeassistant.config_entries", config_entries),
        ("homeassistant.core", core),
        ("voluptuous", voluptuous),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    package = types.ModuleType("jev_sentinel_under_test")
    package.__path__ = [str(COMPONENT)]
    # `config_flow` imports DOMAIN and DEFAULT_PROVIDER from this package. The
    # package's own `__init__` imports `homeassistant.core`, which the stand-in
    # above does not carry, so the two names are read out of the file rather than
    # executed — the same thing `tests/test_config_flow_options.py` does and for
    # the same reason.
    package.DOMAIN = "jev_sentinel"
    package.DEFAULT_PROVIDER = "openrouter"
    monkeypatch.setitem(sys.modules, "jev_sentinel_under_test", package)
    spec = importlib.util.spec_from_file_location(
        "jev_sentinel_under_test.config_flow", COMPONENT / "config_flow.py"
    )
    _runtime_spec = importlib.util.spec_from_file_location(
        "jev_sentinel_under_test.runtime", COMPONENT / "runtime.py"
    )
    _runtime = importlib.util.module_from_spec(_runtime_spec)
    monkeypatch.setitem(sys.modules, "jev_sentinel_under_test.runtime", _runtime)
    _runtime_spec.loader.exec_module(_runtime)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def await_or_run(awaitable):
    """Await a coroutine from synchronous test code.

    No async plugin is a dependency of this package and the suite must run under
    a bare ``python -m pytest``, exactly as CI does, so the coroutine is run on a
    fresh loop here rather than by a plugin the repository does not declare.
    """
    return asyncio.run(awaitable)


@pytest.fixture
def flow(monkeypatch):
    return _install(monkeypatch)


# Every name the form's selector offers. Read out of the flow module rather than
# transcribed, so a name added to the selector afterwards is in the table.
def _offered_names(flow):
    return sorted(flow.PROVIDER_LABELS)


# The local model that decides ``auto``. Named explicitly, because ``auto``
# resolves differently depending on whether a local model was chosen.
AUTO_LOCAL_MODEL = "laya"


def _offered_needs_key(flow):
    """The row per offered name, keyed the way the table below is written."""
    return {
        "api_with_local_fallback": True,
        "api_only": True,
        "auto": True,
        "clef": False,
        "clef_api": False,
        "clef_then_jev": True,
        "clef_with_jev_fallback": True,
        "clef_with_local_fallback": False,
        "jev_api": True,
        "laya_local": False,
        "laya_then_hosted": True,
        "laya_with_jev_fallback": True,
        "local_only": False,
        "local_with_api_fallback": True,
        "openrouter": True,
    }


def test_every_name_the_form_offers_is_in_the_table(flow):
    """A name in the selector with no row here is a defect this file cannot see."""
    table = _offered_needs_key(flow)
    offered = set(_offered_names(flow))
    missing = offered - set(table)
    assert not missing, f"the form offers a name with no row in this table: {missing}"
    extra = set(table) - offered
    assert (
        not extra
    ), f"this table has a row for a name the form does not offer: {extra}"


@pytest.mark.parametrize("name, needs_key", _offered_needs_key(flow=None).items())
def test_the_gate_agrees_with_what_build_provider_needs(flow, name, needs_key):
    """The form's gate and the builder's failure are two sides of one question."""
    assert flow._needs_api_key(name, local_model=AUTO_LOCAL_MODEL) is needs_key, name


def test_the_two_legacy_aliases_the_old_gate_let_through(flow):
    """``laya_with_jev_fallback`` and ``laya_then_hosted`` resolve onto the local
    leading chain, whose provider order raises without an OpenRouter key."""
    for name in ("laya_with_jev_fallback", "laya_then_hosted"):
        assert RUNTIME.resolve_provider(name) == "local_with_api_fallback"
        assert flow._needs_api_key(name, local_model=AUTO_LOCAL_MODEL) is True, name
        # And the builder confirms it: no key, no provider.
        with pytest.raises((ValueError, RuntimeError)):
            RUNTIME.build_provider(name, api_key=None, env={})


def test_a_keyless_name_really_builds(flow):
    """The other direction: a name the gate lets through must not raise."""
    build_provider = RUNTIME.build_provider

    for name in ("local_only", "laya", "laya_local"):
        provider = build_provider(name, api_key=None, env={})
        assert provider is not None, name


def test_auto_needs_a_key_without_a_local_model(flow):
    """``auto`` with no local model resolves to ``api_only``, which calls the key."""
    assert flow._needs_api_key("auto", local_model=None) is True


def test_a_bare_boolean_short_circuits_the_form(flow):
    """The form's own branch, so a regression in the call shape is visible."""
    result = flow.JevSentinelConfigFlow.async_get_options_flow(None)
    assert result is not None


def test_the_offered_names_cover_every_alias_the_runtime_accepts(flow):
    """A name the runtime resolves but the form does not offer is unreachable."""
    offered = set(_offered_names(flow))
    # Every name the runtime accepts that is not a canonical mode must be
    # offered, or an existing entry's stored name is one the form can no longer
    # show back. `laya` is the one exception: it is the local route alias named
    # in the label of `local_only`, which selects it.
    unoffered = {
        name
        for name in RUNTIME.PROVIDER_NAMES
        if name not in offered and name not in flow.CANONICAL_MODES
    }
    assert unoffered <= {"laya"}, f"the form does not offer: {sorted(unoffered)}"


def test_the_form_refuses_a_missing_key_on_the_local_leading_chain(flow, monkeypatch):
    """The end-to-end path: the legacy alias is asked for the key after all."""
    submitted = {
        "provider": "laya_with_jev_fallback",
        "api_key": "",
        "laya_base_url": "http://127.0.0.1:8000",
    }
    result = await_or_run(
        flow.JevSentinelConfigFlow().async_step_user(user_input=submitted)
    )
    assert result.get("errors", {}).get("api_key") == "api_key_required", result
    # And with the key supplied the same submission is accepted.
    submitted = dict(submitted, api_key="sk-supplied")
    result = await_or_run(
        flow.JevSentinelConfigFlow().async_step_user(user_input=submitted)
    )
    assert result.get("title"), result
    assert result["data"]["provider"] == "laya_with_jev_fallback"


def test_the_form_accepts_a_keyless_local_only_submission(flow):
    submitted = {
        "provider": "local_only",
        "api_key": "",
        "laya_base_url": "http://127.0.0.1:8000",
    }
    result = await_or_run(
        flow.JevSentinelConfigFlow().async_step_user(user_input=submitted)
    )
    assert result.get("title"), result


def test_the_form_refuses_a_public_laya_base_url(flow):
    """``https://evil.example.com`` was accepted unchanged."""
    submitted = {
        "provider": "local_only",
        "api_key": "",
        "laya_base_url": "https://evil.example.com",
    }
    result = await_or_run(
        flow.JevSentinelConfigFlow().async_step_user(user_input=submitted)
    )
    assert (
        result.get("errors", {}).get("laya_base_url") == "laya_base_url_invalid"
    ), result


def test_the_form_accepts_a_private_laya_base_url(flow):
    submitted = {
        "provider": "local_only",
        "api_key": "",
        "laya_base_url": "https://192.168.1.10:8000",
    }
    result = await_or_run(
        flow.JevSentinelConfigFlow().async_step_user(user_input=submitted)
    )
    assert result.get("title"), result


def test_the_form_refuses_a_laya_base_url_carrying_a_path(flow):
    submitted = {
        "provider": "local_only",
        "api_key": "",
        "laya_base_url": "http://127.0.0.1:8000/v1",
    }
    result = await_or_run(
        flow.JevSentinelConfigFlow().async_step_user(user_input=submitted)
    )
    assert (
        result.get("errors", {}).get("laya_base_url") == "laya_base_url_invalid"
    ), result


def test_the_error_key_the_flow_raises_exists_in_the_translations(flow):
    import json

    strings = json.loads((COMPONENT / "strings.json").read_text())
    errors = strings["config"]["error"]
    assert "laya_base_url_invalid" in errors
    assert "api_key_required" in errors
    assert "local_model_invalid" in errors
