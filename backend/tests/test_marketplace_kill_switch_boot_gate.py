"""End-to-end guard test for issue #1240's first startup assertion:
``archimedes.main`` must refuse to finish importing (i.e. the process must
never reach a state where it could serve traffic) when circlekit fails to
import AND PAYMENTS_DRY_RUN=false.

Pattern copied from ``test_main_ssm_prod_gate.py``: runs the import in a
**subprocess** because the assertion lives in top-level module code that
only executes once per interpreter, and every other test file in this suite
already imports ``archimedes.main`` — re-exercising that import path
in-process would depend on cache/reload ordering across the whole test
session instead of being hermetic. ``sys.modules["circlekit"] = None`` is
the documented Python mechanism for forcing every subsequent
``import circlekit`` (or ``from circlekit import ...``) to raise
``ImportError`` — it reproduces the PR #958 runtime-image failure mode
(installs fine in the builder, missing at runtime) without needing to
actually uninstall the package.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from textwrap import dedent

_BACKEND_DIR = Path(__file__).resolve().parents[1]

_HARNESS = dedent(
    """
    import sys

    # Force every `import circlekit` / `from circlekit import ...` to raise
    # ImportError, reproducing "installs in the builder, missing at runtime"
    # (PR #958) without needing to actually uninstall the package.
    sys.modules["circlekit"] = None

    # Neutralize load_dotenv BEFORE archimedes.main runs its own
    # load_dotenv("../.env", override=True) — a populated repo-root .env
    # could set PAYMENTS_DRY_RUN and silently override this test's env
    # (pattern: test_main_ssm_prod_gate.py).
    import dotenv as _dotenv
    _dotenv.load_dotenv = lambda *_a, **_kw: False

    try:
        import archimedes.main  # noqa: F401  (import-time side effect under test)
    except RuntimeError as exc:
        print("FATAL:" + str(exc))
    else:
        print("BOOTED")
    """
)


def _run(env: dict[str, str]) -> str:
    result = subprocess.run(
        [sys.executable, "-c", _HARNESS],
        cwd=_BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"subprocess itself crashed (rc={result.returncode}) instead of the harness catching "
        f"RuntimeError\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result.stdout.strip().splitlines()[-1]


def _base_env() -> dict[str, str]:
    """Whitelist-only env (testing conventions; pattern:
    test_security_hardening._clean_subprocess_env / test_main_ssm_prod_gate).
    PUBLIC_DOMAIN is deliberately absent — this test is only about the
    circlekit/PAYMENTS_DRY_RUN gate, not the separate SSM/production gate."""
    import os

    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "PYTHONPATH": str(_BACKEND_DIR),
        "DATABASE_URL": "sqlite:////tmp/archimedes-mkt-kill-switch-gate-subprocess.db",
    }


def test_broken_circlekit_plus_real_money_refuses_to_boot():
    """The #1240 scenario: circlekit unimportable + PAYMENTS_DRY_RUN=false
    must not silently degrade to 'marketplace absent, everything else 200s'
    — the process must refuse to finish starting."""
    env = _base_env()
    env["PAYMENTS_DRY_RUN"] = "false"
    out = _run(env)
    assert out.startswith("FATAL:"), f"expected a FATAL refusal to boot, got: {out!r}"
    assert "circlekit failed to import" in out


def test_broken_circlekit_with_dry_run_still_boots():
    """Adversarial companion: the SAME broken-circlekit shim, but
    PAYMENTS_DRY_RUN left at its safe default (true) — this is the
    pre-existing, still-correct PR #958 behavior (degrade, don't crash) and
    must NOT be broken by the new assertion. Proves the FATAL above is
    actually conditioned on payments_dry_run, not on circlekit alone."""
    env = _base_env()
    env.pop("PAYMENTS_DRY_RUN", None)  # unset -> defaults to "true" (safe)
    out = _run(env)
    assert out == "BOOTED", f"expected a normal, degraded boot, got: {out!r}"
