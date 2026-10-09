"""The service handlers, the sensor, and the local-failure breaker.

``custom_components/jev_sentinel/__init__.py`` imports ``homeassistant.core``,
which is not installable here (PEP 668 on this machine) and is not a dependency
of this package. The stand-in below carries the whole surface the module uses —
``HomeAssistant``, ``ServiceCall``, ``ConfigEntry``, a bus that records what was
fired, an executor job, and a service registry that refuses a double
registration — so the handlers are exercised against the real module rather than
a paraphrase of it. Tests that cannot run against a paraphrase are the point:
the two defects these cover were both invisible in every existing test.

1. A provider misconfiguration crashed the service call, fired nothing, and left
   the sensor showing ``ready`` — its initial value, which it kept for the life
   of the process. An operator watching it saw a healthy integration while every
   review on it was raising.

2. Services are registered once per domain, but the handlers captured the FIRST
   config entry. With a second entry loaded, every ``review`` used entry #1's
   provider: a user who added a ``local_only`` entry beside a hosted one kept
   sending household cases out over the OpenRouter key stored in entry #1,
   contrary to the second entry's intent. ``async_unload_entry`` then left the
   registered handler bound to the entry it had captured.

3. The local-failure breaker was one module-global counter, so one entry's dead
   local server suppressed the hosted fallback of every other entry for the
   whole process.
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

DOMAIN = "jev_sentinel"


class _Bus:
    """Records fired events instead of handing them to Home Assistant."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def async_fire(self, event_type, event_data=None):
        self.events.append((event_type, dict(event_data or {})))

    def async_listen(self, event_type, handler):
        return lambda: None


class _Service:
    """One registered service, and the handler it holds."""

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[dict] = []

    async def __call__(self, call):
        self.calls.append(dict(call.data))
        return await self.handler(call)


class _Registry:
    """Mirrors ``has_service``/``async_register``/``async_remove``."""

    def __init__(self):
        self.services: dict[tuple[str, str], _Service] = {}

    def has_service(self, domain, name):
        return (domain, name) in self.services

    def async_register(self, domain, name, handler):
        assert not self.has_service(domain, name), "double registration"
        self.services[(domain, name)] = _Service(handler)

    def async_remove(self, domain, name):
        self.services.pop((domain, name), None)


class _Call:
    def __init__(self, data):
        self.data = dict(data)


class _Entry:
    def __init__(self, entry_id, data=None):
        self.entry_id = entry_id
        self.data = dict(data or {})
        self.options = {}


class _FakeHass:
    """The subset of ``HomeAssistant`` this module touches."""

    def __init__(self):
        self.data: dict = {}
        self.bus = _Bus()
        self.services = _Registry()
        self.config_entries = types.SimpleNamespace(
            async_forward_entry_setups=self._forward,
            async_unload_platforms=self._unload_platforms,
            async_get_entry=self._get_entry,
        )
        self.entries: dict[str, _Entry] = {}
        self.executor_calls: list[tuple] = []

    # Home Assistant calls these with the entry; the stand-in just records that
    # the platform setup ran, which is all the handlers depend on.
    async def _forward(self, entry, platforms):
        self.forwarded.append(entry.entry_id)

    async def _unload_platforms(self, entry, platforms):
        self.unloaded.append(entry.entry_id)
        return True

    def _get_entry(self, entry_id):
        return self.entries.get(entry_id)

    async def async_add_executor_job(self, func, *args):
        self.executor_calls.append((func, args))
        return func(*args)

    forwarded: list
    unloaded: list

    def __attrs_post_init__(self):  # pragma: no cover - not a dataclass
        pass


def _install(monkeypatch, module_name="jev_sentinel_services_under_test"):
    """Load the real integration module against the stand-in Home Assistant."""
    ha = types.ModuleType("homeassistant")
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = _FakeHass
    core.ServiceCall = _Call

    config_entries = types.ModuleType("homeassistant.config_entries")

    class ConfigEntry:
        pass

    config_entries.ConfigEntry = ConfigEntry
    ha.core = core
    ha.config_entries = config_entries
    for name, module in (
        ("homeassistant", ha),
        ("homeassistant.core", core),
        ("homeassistant.config_entries", config_entries),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    package = types.ModuleType(module_name)
    package.__path__ = [str(COMPONENT)]
    monkeypatch.setitem(sys.modules, module_name, package)
    spec = importlib.util.spec_from_file_location(
        f"{module_name}.__init__", COMPONENT / "__init__.py"
    )
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: the module imports `.runtime`, and a module must
    # be in sys.modules before it is executed.
    monkeypatch.setitem(sys.modules, f"{module_name}.__init__", module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def integration(monkeypatch):
    return _install(monkeypatch)


@pytest.fixture
def hass():
    instance = _FakeHass()
    instance.forwarded = []
    instance.unloaded = []
    return instance


def _entry(integration, hass, entry_id="entry-1", **data):
    entry = _Entry(entry_id, data)
    hass.entries[entry_id] = entry
    return entry


async def _setup(integration, hass, *entries):
    for entry in entries:
        await integration.async_setup_entry(hass, entry)
    return hass


def _decision_event(hass):
    """Every review-shaped event fired, and the last one."""
    review_events = [
        payload for kind, payload in hass.bus.events if kind == "jev_sentinel_decision"
    ]
    return review_events


# ---------------------------------------------------------------------------
# 1. a misconfigured provider must not crash the call or hide the failure
# ---------------------------------------------------------------------------


def test_a_misconfigured_provider_fires_an_error_event(integration, hass):
    return asyncio.run(
        _test_a_misconfigured_provider_fires_an_error_event(integration, hass)
    )


async def _test_a_misconfigured_provider_fires_an_error_event(integration, hass):
    """``local_with_api_fallback`` with no key raises from ``build_provider``.

    The exception used to escape the handler, so the call raised into Home
    Assistant's log, no decision event was fired, and the sensor kept its
    previous value — ``ready`` on a fresh install.
    """
    bad = _entry(
        integration, hass, "bad", provider="laya_with_jev_fallback", api_key=""
    )
    await _setup(integration, hass, bad)
    service = hass.services.services[(DOMAIN, "review")]
    # The call completes; it is the review that failed.
    await service(_Call({"event_type": "manual_review"}))
    events = _decision_event(hass)
    assert len(events) == 1, events
    payload = events[-1]
    assert payload["decision"]["outcome"] == "error", payload
    # The sensor reads ``decision.outcome`` and nothing else, so this is what
    # moves it off ``ready``.
    assert payload["decision"]["error_type"] == "ValueError", payload
    assert "case" in payload, "the event still carries the case it reviewed"


def test_a_misconfigured_provider_moves_the_sensor_off_ready(integration, hass):
    return asyncio.run(
        _test_a_misconfigured_provider_moves_the_sensor_off_ready(integration, hass)
    )


async def _test_a_misconfigured_provider_moves_the_sensor_off_ready(integration, hass):
    """What the operator sees, asserted on the sensor's own reading rule.

    ``sensor.py`` cannot be imported here without Home Assistant, so the rule is
    restated here. It is the expression
    ``SentinelStatusSensor._decision_received`` evaluates, and a change to that
    expression has to change this assertion with it.
    """
    bad = _entry(integration, hass, "bad", provider="local_only", laya_base_url="")
    await _setup(integration, hass, bad)
    service = hass.services.services[(DOMAIN, "review")]
    await service(_Call({"event_type": "manual_review"}))
    payload = _decision_event(hass)[-1]
    # This is exactly the expression ``SentinelStatusSensor._decision_received``
    # evaluates, so the sensor's value is the assertion rather than a restatement.
    native_value = payload.get("decision", {}).get("outcome", "unknown")
    assert native_value == "error"
    assert native_value != "ready"


def test_an_unresolvable_call_reports_unavailable_not_error(integration, hass):
    return asyncio.run(
        _test_an_unresolvable_call_reports_unavailable_not_error(integration, hass)
    )


async def _test_an_unresolvable_call_reports_unavailable_not_error(integration, hass):
    """No entry could answer, so there is no case review to have failed."""
    # Two entries, both still loaded, and the call names neither.
    await _setup(
        integration,
        hass,
        _entry(integration, hass, "first", provider="local_only"),
        _entry(integration, hass, "second", provider="local_only"),
    )
    service = hass.services.services[(DOMAIN, "review")]
    await service(_Call({"event_type": "manual_review"}))
    payload = _decision_event(hass)[-1]
    assert payload["decision"]["outcome"] == "unavailable", payload
    assert payload["case"]["event_type"] == "manual_review"
    assert "2 config entries" in payload["decision"]["reason"], payload


def test_a_successful_review_still_fires_the_case_and_decision(integration, hass):
    return asyncio.run(
        _test_a_successful_review_still_fires_the_case_and_decision(integration, hass)
    )


async def _test_a_successful_review_still_fires_the_case_and_decision(
    integration, hass
):
    """The error path must not replace the working one."""
    entry = _entry(integration, hass, "ok", provider="local_only")
    await _setup(integration, hass, entry)

    class _Decides:
        """A provider that answers, standing in for the local decision model."""

        def decide(self, state):
            from sentinel.models import Decision

            return Decision("notify", "nothing to do", action="notify")

    built = integration._provider_for(entry)

    class _Local:
        decide = _Decides().decide
        endpoint = "http://127.0.0.1:8000/v1/systemone"

    # The provider is replaced at the module level so no request is made, which
    # is what the no-network rule in this repository's other tests asserts.
    integration._provider_for = lambda entry: _Local()
    service = hass.services.services[(DOMAIN, "review")]
    await service(_Call({"event_type": "manual_review"}))
    payload = _decision_event(hass)[-1]
    assert payload["decision"]["outcome"] == "notify", payload
    assert payload["case"]["event_type"] == "manual_review"


# ---------------------------------------------------------------------------
# 2. every call resolves the entry it is for
# ---------------------------------------------------------------------------


def test_the_second_entrys_provider_answers_not_the_firsts(integration, hass):
    return asyncio.run(
        _test_the_second_entrys_provider_answers_not_the_firsts(integration, hass)
    )


async def _test_the_second_entrys_provider_answers_not_the_firsts(integration, hass):
    """The privacy finding: a ``local_only`` entry beside a hosted one.

    Both entries are loaded, both handlers registered once, and the call must
    reach the entry that names the local-only mode. Before the fix the first
    entry's closures were captured and the second entry's were discarded, so
    this call went out over entry #1's OpenRouter key.
    """
    hosted = _entry(integration, hass, "hosted", provider="auto", api_key="sk-first")
    local = _entry(integration, hass, "local", provider="local_only")
    await _setup(integration, hass, hosted, local)
    seen: list[str] = []
    integration._provider_for = lambda entry: seen.append(entry.entry_id) or _Local()
    service = hass.services.services[(DOMAIN, "review")]
    await service(_Call({"event_type": "manual_review", "entry_id": "local"}))
    assert seen == ["local"], seen
    await service(_Call({"event_type": "manual_review", "entry_id": "hosted"}))
    assert seen == ["local", "hosted"], seen


def test_an_unnamed_call_is_refused_rather_than_answered_from_entry_one(
    integration, hass
):
    return asyncio.run(
        _test_an_unnamed_call_is_refused_rather_than_answered_from_entry_one(
            integration, hass
        )
    )


async def _test_an_unnamed_call_is_refused_rather_than_answered_from_entry_one(
    integration, hass
):
    """One entry needs no naming. Two entries, and no naming, is not a default.

    Answering it from whichever entry happens to be first in the mapping is the
    misrouting this whole change exists to stop, wearing a different hat.
    """
    hosted = _entry(integration, hass, "hosted", provider="auto", api_key="sk-first")
    local = _entry(integration, hass, "local", provider="local_only")
    await _setup(integration, hass, hosted, local)
    seen: list[str] = []
    integration._provider_for = lambda entry: seen.append(entry.entry_id) or _Local()
    service = hass.services.services[(DOMAIN, "review")]
    await service(_Call({"event_type": "manual_review"}))
    assert seen == [], seen
    payload = _decision_event(hass)[-1]
    assert payload["decision"]["outcome"] == "unavailable", payload
    assert "entry_id" in payload["decision"]["reason"], payload


def test_a_single_entry_still_needs_no_naming(integration, hass):
    return asyncio.run(_test_a_single_entry_still_needs_no_naming(integration, hass))


async def _test_a_single_entry_still_needs_no_naming(integration, hass):
    """The ordinary household's one-entry install is unchanged."""
    only = _entry(integration, hass, "only", provider="local_only")
    await _setup(integration, hass, only)
    seen: list[str] = []
    integration._provider_for = lambda entry: seen.append(entry.entry_id) or _Local()
    service = hass.services.services[(DOMAIN, "review")]
    await service(_Call({"event_type": "manual_review"}))
    assert seen == ["only"], seen


def test_unloading_the_captured_entry_leaves_no_stale_handler(integration, hass):
    return asyncio.run(
        _test_unloading_the_captured_entry_leaves_no_stale_handler(integration, hass)
    )


async def _test_unloading_the_captured_entry_leaves_no_stale_handler(integration, hass):
    """``async_unload_entry`` used to leave the handler bound to the unloaded entry.

    The handler resolved its entry on every call, because it now reads
    ``hass.data`` at call time instead of closing over an entry.
    """
    hosted = _entry(integration, hass, "hosted", provider="auto", api_key="sk-first")
    local = _entry(integration, hass, "local", provider="local_only")
    await _setup(integration, hass, hosted, local)
    # The first entry is unloaded, so its handler must not answer for it.
    await integration.async_unload_entry(hass, hosted)
    seen: list[str] = []
    integration._provider_for = lambda entry: seen.append(entry.entry_id) or _Local()
    service = hass.services.services[(DOMAIN, "review")]
    await service(_Call({"event_type": "manual_review", "entry_id": "hosted"}))
    assert seen == [], seen
    payload = _decision_event(hass)[-1]
    assert payload["decision"]["outcome"] == "unavailable", payload
    # Its data is gone, so nothing later can find it either.
    assert "hosted" not in (hass.data.get(DOMAIN) or {})
    # And the surviving entry still answers.
    await service(_Call({"event_type": "manual_review", "entry_id": "local"}))
    assert seen == ["local"], seen


def test_services_are_removed_with_the_last_entry(integration, hass):
    return asyncio.run(
        _test_services_are_removed_with_the_last_entry(integration, hass)
    )


async def _test_services_are_removed_with_the_last_entry(integration, hass):
    only = _entry(integration, hass, "only", provider="local_only")
    await _setup(integration, hass, only)
    assert (DOMAIN, "review") in hass.services.services
    await integration.async_unload_entry(hass, only)
    assert (DOMAIN, "review") not in hass.services.services
    assert (DOMAIN, "verify") not in hass.services.services
    assert DOMAIN not in hass.data


def test_services_survive_while_any_entry_remains(integration, hass):
    return asyncio.run(
        _test_services_survive_while_any_entry_remains(integration, hass)
    )


async def _test_services_survive_while_any_entry_remains(integration, hass):
    first = _entry(integration, hass, "first", provider="local_only")
    second = _entry(integration, hass, "second", provider="local_only")
    await _setup(integration, hass, first, second)
    await integration.async_unload_entry(hass, first)
    # The surviving entry still needs a handler, and a real Home Assistant
    # refuses a second registration, so the handler must not have been removed.
    assert (DOMAIN, "review") in hass.services.services
    assert (DOMAIN, "verify") in hass.services.services


# ---------------------------------------------------------------------------
# the verification event carries the entry it was attributed to
# ---------------------------------------------------------------------------


def test_the_verification_service_records_its_entry(integration, hass):
    return asyncio.run(
        _test_the_verification_service_records_its_entry(integration, hass)
    )


async def _test_the_verification_service_records_its_entry(integration, hass):
    only = _entry(integration, hass, "only", provider="local_only")
    await _setup(integration, hass, only)
    service = hass.services.services[(DOMAIN, "verify")]
    await service(_Call({"expected": "off", "actual": "off"}))
    assert hass.bus.events[-1][0] == "jev_sentinel_verification"
    assert hass.bus.events[-1][1]["verified"] is True
    assert hass.bus.events[-1][1]["entry_id"] == "only"


def test_a_mismatch_is_still_a_mismatch(integration, hass):
    return asyncio.run(_test_a_mismatch_is_still_a_mismatch(integration, hass))


async def _test_a_mismatch_is_still_a_mismatch(integration, hass):
    only = _entry(integration, hass, "only", provider="local_only")
    await _setup(integration, hass, only)
    service = hass.services.services[(DOMAIN, "verify")]
    await service(_Call({"expected": "off", "actual": "on"}))
    payload = hass.bus.events[-1][1]
    assert payload["verified"] is False
    assert payload["status"] == "mismatch"


# ---------------------------------------------------------------------------
# 3. the breaker is per local hop, not a process global
# ---------------------------------------------------------------------------


def _load_sentinel_jev(module_name="_entry_scoped_jev"):
    """``sentinel.jev`` through its package, so relative imports resolve."""
    if module_name in sys.modules:
        return sys.modules[module_name]
    import sentinel.jev  # noqa: PLC0415 - deferred, and the name is set below

    module = importlib.import_module("sentinel.jev")
    sys.modules[module_name] = module
    return module


def _load_runtime(module_name="_entry_scoped_runtime"):
    """``runtime.py`` standalone: it has no relative imports to resolve.

    It is loaded by file, exactly as Home Assistant loads it from a HACS
    install, and registered under two names so a parametrized test can load it
    twice without the second load reusing the first module.
    """
    spec = importlib.util.spec_from_file_location(
        module_name, ROOT / "custom_components" / "jev_sentinel" / "runtime.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: the module declares dataclasses with forward
    # references, and the dataclass machinery resolves them through
    # `sys.modules[cls.__module__]`, which is not yet set for a module that has
    # only been constructed.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("copy", ["package", "runtime"])
def test_one_entrys_dead_local_server_does_not_deny_the_other(copy):
    """The shared-breaker finding.

    With two config entries, three failures on the first entry's dead local
    server used to suppress the second entry's hosted fallback for the whole
    process. The breaker is now keyed by the local endpoint, so each entry's
    local hop carries its own count.
    """
    if copy == "package":
        module = _load_sentinel_jev()
    else:
        module = _load_runtime()
    module.reset_local_failures()
    dead = "http://127.0.0.1:9000/v1/systemone"
    alive = "http://127.0.0.1:8000/v1/systemone"
    for _ in range(module.LOCAL_FALLBACK_FAILURE_LIMIT + 1):
        module.note_local_failure(RuntimeError("dead"), dead)
    assert module.local_failure_count(dead) > module.LOCAL_FALLBACK_FAILURE_LIMIT
    # The healthy hop is untouched, so its fallback is still allowed.
    assert module.local_failure_count(alive) == 0
    assert module.note_local_failure(RuntimeError("blip"), alive) is True


@pytest.mark.parametrize("copy", ["package", "runtime"])
def test_a_success_on_one_hop_resets_only_that_hop(copy):
    if copy == "package":
        module = _load_sentinel_jev()
    else:
        module = _load_runtime()
    module.reset_local_failures()
    dead = "http://127.0.0.1:9000/v1/systemone"
    alive = "http://127.0.0.1:8000/v1/systemone"
    module.note_local_failure(RuntimeError("dead"), dead)
    module.note_local_failure(RuntimeError("blip"), alive)
    module.reset_local_failures(alive)
    assert module.local_failure_count(alive) == 0
    assert module.local_failure_count(dead) == 1
    module.reset_local_failures()
    assert module.local_failure_count() == 0


@pytest.mark.parametrize("copy", ["package", "runtime"])
def test_the_breaker_still_decays_within_one_hop(copy, monkeypatch):
    """The cooldown property is unchanged, only its scope is narrower."""
    if copy == "package":
        module = _load_sentinel_jev()
    else:
        module = _load_runtime()
    module.reset_local_failures()
    clock = {"now": 1_000.0}
    monkeypatch.setattr(module.time, "monotonic", lambda: clock["now"])
    scope = "http://127.0.0.1:8000/v1/systemone"
    for _ in range(module.LOCAL_FALLBACK_FAILURE_LIMIT):
        assert module.note_local_failure(RuntimeError("blip"), scope) is True
    clock["now"] += module.LOCAL_FALLBACK_COOLDOWN_SECONDS + 1
    assert module.local_failure_count(scope) == 0
    assert module.note_local_failure(RuntimeError("blip"), scope) is True


@pytest.mark.parametrize("copy", ["package", "runtime"])
def test_the_breaker_is_still_tripped_at_the_limit(copy):
    """The suppression itself must survive the scoping change."""
    if copy == "package":
        module = _load_sentinel_jev()
    else:
        module = _load_runtime()
    module.reset_local_failures()
    scope = "http://127.0.0.1:8000/v1/systemone"
    results = [
        module.note_local_failure(RuntimeError("blip"), scope)
        for _ in range(module.LOCAL_FALLBACK_FAILURE_LIMIT + 1)
    ]
    assert results == [True, True, True, False]
    module.reset_local_failures()
