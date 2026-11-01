"""Guard: no test module binds ``unittest.mock.call`` at module scope.

pytest 9.2.0 unwraps the objects it finds at module level while collecting.
``mock.call`` answers every attribute lookup, ``__wrapped__`` included, with a
brand-new ``_Call``, so that unwrap never ends: collecting one such module grew
to 2 GB in a second. It got CI's backend job cancelled (the runner ran out of
memory) and took down a developer laptop running the suite (2026-11-01).
``from unittest import mock`` and ``mock.call(...)`` is safe, because the module
object, not ``call``, is what sits at module scope.
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOTS = [TESTS, TESTS.parents[1] / "analytics-engine", TESTS.parents[1] / "cli", TESTS.parents[1] / "mcp-server"]
BANNED = {"call", "_Call"}


def _module_level_mock_call_imports(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines = []
    for node in tree.body:  # module scope only; imports inside functions are fine
        if isinstance(node, ast.ImportFrom) and node.module in {"unittest.mock", "mock"}:
            if any(alias.name in BANNED for alias in node.names):
                lines.append(node.lineno)
    return lines


def _python_test_files():
    for root in ROOTS:
        if root.is_dir():
            yield from (p for p in root.rglob("*.py") if "node_modules" not in p.parts and ".venv" not in p.parts)


def test_no_test_module_imports_mock_call_at_module_scope():
    offenders = [f"{p}:{n}" for p in _python_test_files() for n in _module_level_mock_call_imports(p)]
    assert not offenders, (
        "module-level `from unittest.mock import call` makes pytest 9.2 collection run away in memory; "
        "use `from unittest import mock` and `mock.call(...)` instead: " + ", ".join(offenders)
    )


def test_the_guard_sees_the_pattern(tmp_path):
    bad = tmp_path / "test_bad.py"
    bad.write_text("from unittest.mock import AsyncMock, call\n\n\ndef test_x():\n    assert call\n", encoding="utf-8")
    good = tmp_path / "test_good.py"
    good.write_text("from unittest import mock\n\n\ndef test_x():\n    assert mock.call\n", encoding="utf-8")
    inner = tmp_path / "test_inner.py"
    inner.write_text("def test_x():\n    from unittest.mock import call\n    assert call\n", encoding="utf-8")
    assert _module_level_mock_call_imports(bad) == [1]
    assert _module_level_mock_call_imports(good) == []
    assert _module_level_mock_call_imports(inner) == []
