"""A HACS install gets one directory, and the component must load from it.

`hacs.json` sets `content_in_root: false`, so a HACS install copies only
`custom_components/jev_sentinel` into `config/custom_components/`. The
repository's `sentinel/` package is declared in `pyproject.toml` as
`packages = ["sentinel"]`, so it exists only in a pip install or a source
checkout and never arrives through HACS.

Commit 3ff0926 replaced the local `_key_is_secret` helper in
`custom_components/jev_sentinel/runtime.py` with
`from sentinel.redaction import _key_is_secret`. Every HACS install then failed
at module load with `ModuleNotFoundError: No module named 'sentinel'`, and the
integration could not be added at all.

The ordinary suite cannot see this. The other test modules do
`sys.path.insert(0, REPO)`, and CI installs `pip install -e '.[test]'`, which
makes `sentinel` importable from anywhere. So the test below builds the
isolation itself: it copies the component alone into a temporary directory, runs
a subprocess with `-I -S` (isolated mode with no site-packages, so neither the
script's directory nor an editable install of this package is on the path), and
asserts the import works there. It does not depend on how pytest was invoked.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMPONENT = REPO / "custom_components" / "jev_sentinel"

# `runtime.py` is the module that failed, and it is the only module in the
# component whose imports are entirely standard library. It is loaded from its
# file under a private name: importing it as `custom_components.jev_sentinel`
# would first run the package `__init__.py`, which imports `homeassistant`, and
# that is not a dependency of this package (manifest.json declares
# `"requirements": []`). The assertion is that the module body executes with only
# what a HACS install ships on the path.
_ISOLATION_SCRIPT = """
import importlib.util
import sys

# If the dev package is reachable here the isolation is wrong and this run must
# not be reported as a pass, because a pass would prove nothing.
if importlib.util.find_spec("sentinel") is not None:
    print("the sentinel package is importable here; the isolation is wrong")
    sys.exit(3)

spec = importlib.util.spec_from_file_location(
    "jev_sentinel_runtime_under_test", PACKAGE + "/runtime.py"
)
module = importlib.util.module_from_spec(spec)
# Register before executing: a dataclass reads sys.modules[cls.__module__]
# while its decorator runs, so an unregistered module fails on `None`.
sys.modules[spec.name] = module
spec.loader.exec_module(module)
print("imports OK")
"""


def test_component_imports_without_repo_root_on_path(tmp_path):
    """The component loads from the only directory a HACS install provides.

    Fails on the pre-fix commit with `ModuleNotFoundError: No module named
    'sentinel'` raised from the import line of `runtime.py`.
    """
    target = tmp_path / "custom_components" / "jev_sentinel"
    target.parent.mkdir(parents=True)
    shutil.copytree(COMPONENT, target, ignore=shutil.ignore_patterns("__pycache__"))
    script = tmp_path / "isolated_import.py"
    script.write_text(f"PACKAGE = {str(target)!r}\n" + _ISOLATION_SCRIPT)

    # `-I` drops the script's directory and PYTHONPATH. `-S` drops
    # site-packages, which is where `pip install -e .` puts `sentinel`: without
    # it the editable install leaves the package importable and this test fails
    # for a developer who followed the CONTRIBUTING setup, which is most of them.
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(script)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode != 3, result.stdout + result.stderr
    assert result.returncode == 0, (
        "the component does not import from a HACS install layout:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "imports OK" in result.stdout, result.stdout


def test_the_component_imports_nothing_a_hacs_install_does_not_ship():
    """No module may import a dependency the copied directory does not carry.

    `sentinel` is the case that broke, and it is invisible to the ordinary suite
    because `pip install -e '.[test]'` provides it. A relative import stays
    inside the copied directory, so it is fine. `homeassistant` is supplied by
    Home Assistant itself, and `voluptuous` by the manifest's own requirements
    when it is not bundled, so both are allowed. Anything else is a third party
    name that only the repository's own package provides.
    """
    allowed = {"homeassistant", "voluptuous"}
    for name in ("runtime.py", "config_flow.py", "sensor.py", "__init__.py"):
        tree = ast.parse((COMPONENT / name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    continue
                modules = [node.module or ""]
            else:
                continue
            for module in modules:
                root = module.split(".", 1)[0]
                assert (
                    root in sys.stdlib_module_names or root in allowed
                ), f"{name} imports {module!r}, which a HACS install does not ship"
