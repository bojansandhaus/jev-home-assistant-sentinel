"""`OptionsFlowHandler` must construct on a Home Assistant that owns `config_entry`.

Home Assistant 2025.6 exposed `OptionsFlow.config_entry` as a property whose
setter was marked `breaks_in_ha_version="2025.12"`. The setter was removed in
2025.12.0, and so was the constructor argument. In 2026.9.0 the base class takes
no arguments at all, and `config_entry` is a read-only property that returns the
entry named by `handler` and raises `ValueError("The config entry is not
available during initialisation")` while `hass` is None.

`OptionsFlowHandler.__init__` used to do `self.config_entry = config_entry`, and
`async_get_options_flow` used to pass the entry in, which raises

    AttributeError: property 'config_entry' of 'OptionsFlowHandler' object has
    no setter

the moment Home Assistant builds the options flow, which is what happens when a
user opens the options dialog. `hacs.json` declares `homeassistant: 2026.9.0`, so
every install the manifest admits hit it.

`homeassistant` is not a dependency of this package, so the real base class
cannot be imported here. The stand-in below is transcribed from the 2026.9.0
`homeassistant.config_entries.OptionsFlow`: no `__init__`, and `config_entry` as
a property with no setter that raises while `hass` is None. The tests below fail
on the pre-fix commit with the AttributeError above and pass once both the
assignment and the constructor argument are gone.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
COMPONENT = REPO / "custom_components" / "jev_sentinel"

# The relevant part of homeassistant.config_entries.OptionsFlow at 2026.9.0.
# `OptionsFlow` defines no `__init__`, so a subclass must accept no arguments,
# and `config_entry` is a read-only property resolved from `handler`.
_OPTIONS_FLOW_SOURCE = '''
class ConfigEntryBaseFlow:
    """Only the plumbing the property below reads."""

    handler: str | None = None
    # The real base class carries `hass`, which is None until Home Assistant
    # attaches the running instance. The property below reads it in that state.
    hass = None


class OptionsFlow(ConfigEntryBaseFlow):
    @property
    def _config_entry_id(self) -> str:
        if self.handler is None:
            raise ValueError(
                "The config entry id is not available during initialisation"
            )
        return self.handler

    @property
    def config_entry(self):
        """Return the config entry linked to the current options flow."""
        if self.hass is None:
            raise ValueError("The config entry is not available during initialisation")
        return self.hass.config_entries.async_get_known_entry(self._config_entry_id)

    def async_show_form(self, **kwargs):
        return kwargs

    def async_create_entry(self, **kwargs):
        return kwargs


class ConfigFlow:
    """The config flow base. Only the class object matters to the import."""

    def __init_subclass__(cls, **kwargs):
        # `JevSentinelConfigFlow` is declared with `domain=DOMAIN`, which the
        # real base class consumes to register the handler.
        cls.domain = kwargs.pop("domain", None)
        super().__init_subclass__(**kwargs)
'''


class _FakeConfigEntry:
    """Stands in for a ConfigEntry, for the factory to hand over."""

    def __init__(self, entry_id="entry-1"):
        self.entry_id = entry_id
        self.data = {}
        self.options = {}


def _constant_from_package_init(name: str) -> str:
    """Read one string constant out of the package `__init__.py` by parsing it.

    `config_flow.py` imports `DEFAULT_PROVIDER` and `DOMAIN` from there. That
    module also imports `homeassistant.config_entries.ConfigEntry` and
    `homeassistant.core`, which the stand-in below does not carry, so the two
    names are parsed rather than executed. Parsing rather than hardcoding keeps
    the values honest: a change to either constant is picked up here.
    """
    for node in ast.parse((COMPONENT / "__init__.py").read_text()).body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if any(
                isinstance(target, ast.Name) and target.id == name
                for target in node.targets
            ):
                return str(node.value.value)
    raise AssertionError(f"__init__.py declares no {name}")


def _install_stand_in(monkeypatch):
    """Put a stand-in `homeassistant.config_entries` and `voluptuous` in place."""
    config_entries = types.ModuleType("homeassistant.config_entries")
    exec(  # noqa: S102 - the source is a literal in this file
        compile(_OPTIONS_FLOW_SOURCE, "<config_entries stand-in>", "exec"),
        config_entries.__dict__,
    )
    config_entries.ConfigEntry = _FakeConfigEntry

    core = types.ModuleType("homeassistant.core")
    core.callback = lambda function: function

    homeassistant = types.ModuleType("homeassistant")
    homeassistant.config_entries = config_entries
    homeassistant.core = core

    # voluptuous is not installed in the isolated environment. The form builds its
    # schemas at call time and at import time, and only the names are needed.
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

    # The component's modules import each other relatively, so load them as a
    # package whose __path__ is the component directory.
    package = types.ModuleType("jev_sentinel_under_test")
    package.__path__ = [str(COMPONENT)]
    package.DOMAIN = _constant_from_package_init("DOMAIN")
    package.DEFAULT_PROVIDER = _constant_from_package_init("DEFAULT_PROVIDER")
    monkeypatch.setitem(sys.modules, "jev_sentinel_under_test", package)

    for name in ("runtime", "config_flow"):
        full = f"jev_sentinel_under_test.{name}"
        spec = importlib.util.spec_from_file_location(full, COMPONENT / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        # A module must be in sys.modules before it is executed: a dataclass reads
        # sys.modules[cls.__module__] while the decorator runs.
        monkeypatch.setitem(sys.modules, full, module)
        spec.loader.exec_module(module)

    return sys.modules["jev_sentinel_under_test.config_flow"]


@pytest.fixture
def flow(monkeypatch):
    return _install_stand_in(monkeypatch)


def test_options_handler_constructs_against_a_read_only_config_entry(flow):
    """The handler must not write to `config_entry`, and must take no argument.

    Fails on the pre-fix commit with `AttributeError: property 'config_entry' of
    'OptionsFlowHandler' object has no setter`, and again with
    `TypeError: OptionsFlowHandler() takes no arguments` if only the assignment
    is removed and the constructor argument is kept.
    """
    handler = flow.OptionsFlowHandler()
    assert isinstance(handler, flow.config_entries.OptionsFlow)
    # No instance attribute shadows the base class property.
    assert "config_entry" not in vars(handler)


def test_options_flow_factory_returns_a_handler(flow):
    """The factory is the caller whose call raised, so it is asserted too."""
    handler = flow.JevSentinelConfigFlow.async_get_options_flow(_FakeConfigEntry())
    assert isinstance(handler, flow.OptionsFlowHandler)
    assert "config_entry" not in vars(handler)


def test_the_entry_still_reaches_the_handler_through_the_base_class(flow):
    """The base class resolves the entry from `handler`, the entry id.

    This is the property of the real 2026.9.0 class transcribed into the
    stand-in, so it asserts the promise the fix has to keep: Home Assistant sets
    `handler` after the factory returns, and `config_entry` follows from it.
    """
    handler = flow.OptionsFlowHandler()
    with pytest.raises(ValueError):
        # `hass` is not set yet, exactly as when the flow is constructed.
        handler.config_entry

    handler.hass = types.SimpleNamespace(
        config_entries=types.SimpleNamespace(
            async_get_known_entry=lambda entry_id: _FakeConfigEntry(entry_id)
        )
    )
    handler.handler = "entry-42"
    assert handler.config_entry.entry_id == "entry-42"


def test_the_options_flow_source_never_writes_config_entry():
    """Read the source, so the guard holds even where the import cannot run.

    `homeassistant` is absent in the isolated environment, so the module is not
    always importable here. The parsed assignment is the defect: a write to
    `self.config_entry` in any method is what the removed setter used to allow.
    """
    tree = ast.parse((COMPONENT / "config_flow.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        else:
            continue
        for target in targets:
            for attribute in ast.walk(target):
                if (
                    isinstance(attribute, ast.Attribute)
                    and attribute.attr == "config_entry"
                    and isinstance(attribute.value, ast.Name)
                    and attribute.value.id == "self"
                ):
                    pytest.fail(
                        "config_flow.py assigns self.config_entry; on Home"
                        " Assistant 2025.12 and later that attribute is a"
                        " read-only property and the assignment raises"
                        " AttributeError"
                    )


def test_the_options_flow_handler_defines_no_constructor():
    """The base class takes no argument, so the handler must not either."""
    tree = ast.parse((COMPONENT / "config_flow.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "OptionsFlowHandler":
            assert not any(
                isinstance(child, ast.FunctionDef) and child.name == "__init__"
                for child in node.body
            ), (
                "OptionsFlowHandler defines __init__; OptionsFlow on Home"
                " Assistant 2025.12 and later accepts no constructor argument"
            )
