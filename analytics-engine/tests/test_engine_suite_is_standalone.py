"""The engine suite must import with ONLY the engine's own dependencies (#1794 review).

The `engine-tests` CI job (.github/workflows/quality-gate.yml) installs exactly
`./analytics-engine`, pytest and pytest-socket — no backend, no FastAPI, no
pydantic. That is deliberate: it proves the engine is a standalone package, and it
is the reason a packaging break shows up as a red job rather than as a mystery in
production.

The #1794 fix needed the DSR badge bar, which is defined once in the *backend's*
`rigor_profiles`. The first attempt reached it with

    from archimedes.services.rigor_profiles import DSR_P_BADGE_MIN

on the reasoning that "rigor_profiles is dependency-free (dataclasses only), so
this costs nothing at import time". True of the leaf module; false of the package
Python must execute to reach it. `archimedes/services/__init__.py` re-exports
`agents.generation_pipeline` -> `api.generate_schemas` -> `pydantic`. In the
backend venv that is invisible. In the engine job it is
`ModuleNotFoundError: No module named 'pydantic'` at COLLECTION, taking
`test_curated_num_trials` and `test_library_pbo` down with it — 482 passing tests
to zero, on a required job.

The backend suite could not catch it: `pytest.ini`'s `norecursedirs` excludes
analytics-engine from the backend run, so both suites were green and the gate was
red. This module is the missing check, and it lives HERE because here is where the
dependency boundary actually exists.

`regen_buy_hold_fixture` now loads `rigor_profiles.py` by path with `importlib`,
which keeps the constant single-sourced (no second literal — see
`backend/tests/test_single_dsr_bar.py`) without dragging the package in.

Hermetic: imports pure Python and inspects `sys.modules`. No DB, no net, no files
beyond the two the loader reads.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

# Every backend-only package the engine venv does not have. `archimedes` is the
# backend distribution itself; the rest are what its `services/__init__` chain
# pulls in transitively.
_BACKEND_ONLY = ("archimedes", "archimedes.services", "pydantic", "fastapi", "sqlalchemy")

# The scripts the engine suite imports at module scope, directly or transitively.
# If a new one starts importing backend code, add it here.
_ENGINE_IMPORTED_SCRIPTS = ("regen_buy_hold_fixture", "regen_fixtures", "gen_daily_returns_store")


def test_the_scripts_the_suite_imports_pull_in_no_backend_package():
    """Import each script in a FRESH interpreter and assert the tree stayed clean.

    A fresh subprocess, not this process: by the time this test runs, conftest and
    sibling modules have already imported half the world, so an in-process check
    would pass on a tree that fails in CI. The subprocess reproduces collection.
    """
    probe = (
        "import sys\n"
        f"sys.path.insert(0, {str(_SCRIPTS)!r})\n"
        f"for name in {list(_ENGINE_IMPORTED_SCRIPTS)!r}:\n"
        "    __import__(name)\n"
        f"leaked = [m for m in {list(_BACKEND_ONLY)!r} if m in sys.modules]\n"
        "print('LEAKED:' + ','.join(leaked))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, (
        "importing the engine's fixture scripts in a clean interpreter failed. In CI "
        "this is a COLLECTION error, so the whole engine job reports zero tests:\n"
        f"{result.stdout}\n{result.stderr}"
    )
    leaked = [m for m in result.stdout.split("LEAKED:")[-1].strip().split(",") if m]
    assert not leaked, (
        "The engine's fixture scripts imported a backend-only package: "
        f"{leaked}. The engine-tests CI job installs ONLY ./analytics-engine, so this "
        "is ModuleNotFoundError at collection there even though the backend venv "
        "hides it. Reach backend constants by loading the leaf module with "
        "importlib.util.spec_from_file_location, never through the "
        "`archimedes.services` package — its __init__ imports the world."
    )


def test_the_badge_bar_is_read_from_the_backend_not_copied():
    """The constant is still single-sourced, and it is a real number."""
    import regen_buy_hold_fixture as rbhf

    backend_source = Path(__file__).parent.parent.parent / "backend" / "archimedes" / "services" / "rigor_profiles.py"
    assert backend_source.is_file(), (
        f"the one definition of the DSR bar is not at {backend_source} — the loader in "
        "regen_buy_hold_fixture is pointing at nothing"
    )
    assert isinstance(rbhf.DSR_P_BADGE_MIN, float)
    assert rbhf.DSR_P_BADGE_MIN > 0.0 and rbhf.DSR_P_BADGE_MIN <= 1.0

    # Read the number straight out of the backend file and require agreement. This
    # is what makes the loader's claim checkable rather than assumed: no literal is
    # written down here, so the assertion cannot go stale when the owner moves the bar.
    literal = [
        line.split("=", 1)[1].strip()
        for line in backend_source.read_text(encoding="utf-8").splitlines()
        if line.startswith("DSR_P_BADGE_MIN = ")
    ]
    assert len(literal) == 1, f"expected exactly one DSR_P_BADGE_MIN definition, found {literal}"
    defined = float(literal[0])
    assert defined == rbhf.DSR_P_BADGE_MIN, (
        f"regen_buy_hold_fixture loaded {rbhf.DSR_P_BADGE_MIN} but rigor_profiles.py "
        f"defines {literal[0]} — the loader is reading a stale or wrong file"
    )


@pytest.mark.parametrize("module", ["regen_buy_hold_fixture", "regen_fixtures"])
def test_the_constant_is_reachable_from_every_re_exporting_script(module: str):
    """`regen_fixtures` re-exports the bar; both names must be the same value."""
    import regen_buy_hold_fixture as rbhf

    imported = __import__(module)
    assert imported.DSR_P_BADGE_MIN == rbhf.DSR_P_BADGE_MIN
