"""#1632: the entrypoint must arm faulthandler.

The backend died twice with a bare exit 139 and no trace (issue #1632); the
one-line defense is ``faulthandler.enable()`` at import time in ``main.py``.

Asserted in a SUBPROCESS, not in-process: pytest ships a built-in faulthandler
plugin that arms the test process itself, so an in-process
``faulthandler.is_enabled()`` check is true with the fix deleted — a vacuous
guard (caught by this file's own revert-demo during #1632's development). The
subprocess first proves faulthandler is NOT pre-armed in a bare interpreter,
then imports the app and asserts the import armed it — so the pass can only
come from ``main.py``.

Uses the repo's clean-subprocess idiom (whitelist env, no .env leak) — see
test_security_hardening.py for the precedent.
"""

import os
import subprocess
import sys
from pathlib import Path

_BACKEND_DIR = str(Path(__file__).resolve().parent.parent)

_PROBE = (
    "import faulthandler\n"
    "assert not faulthandler.is_enabled(), 'pre-armed: this probe proves nothing'\n"
    "import archimedes.main\n"
    "assert faulthandler.is_enabled(), 'importing archimedes.main did not arm faulthandler (#1632)'\n"
    "print('ARMED-BY-IMPORT')\n"
)


def test_importing_main_arms_faulthandler_in_a_bare_interpreter():
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=_BACKEND_DIR,
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "PYTHONPATH": _BACKEND_DIR,
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"probe failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    assert "ARMED-BY-IMPORT" in result.stdout
