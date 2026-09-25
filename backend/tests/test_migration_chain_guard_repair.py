"""The migration chain guard must let the repair of a forked main through.

2026-09-04: #1844 and #1845 each added a migration on ``e6b2a19c4d70``; each
PR's CI saw one head, main ended with two, and the deploy of main failed in
boot validation. The repair PR (#1847) re-points one migration onto the other
— and the guard, seeing two heads on main, refused to validate it: the fix for
a forked main was unmergeable under the guard whose job is to prevent forks.

These tests build real git repositories in ``tmp_path`` and run the guard the
way CI runs it (subprocess, ``--base-ref``). Hermetic: no network, no alembic.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD = REPO_ROOT / ".github" / "scripts" / "migration_chain_guard.py"
VERSIONS = "backend/migrations/versions"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def _migration(rev: str, down: str | None) -> str:
    down_s = "None" if down is None else f'"{down}"'
    return f'"""m {rev}"""\nrevision = "{rev}"\ndown_revision = {down_s}\n'


def _write(repo: Path, rev: str, down: str | None) -> None:
    d = repo / VERSIONS
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rev}_m.py").write_text(_migration(rev, down), encoding="utf-8")


def _commit(repo: Path, msg: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", msg)


@pytest.fixture
def forked_main(tmp_path: Path) -> Path:
    """A repo whose ``main`` carries two heads: aaa1 and bbb1 both on base0."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, "base0", None)
    _write(repo, "aaa1", "base0")
    _write(repo, "bbb1", "base0")
    _commit(repo, "main with two heads")
    _git(repo, "checkout", "-q", "-b", "pr")
    return repo


def _run_guard(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GUARD), "--base-ref", "main"],
        cwd=repo,
        capture_output=True,
        text=True,
    )


def test_the_fixture_main_really_has_two_heads(forked_main: Path):
    _write(forked_main, "ccc1", "aaa1")
    _commit(forked_main, "touch so the guard looks")
    out = _run_guard(forked_main)
    assert out.returncode == 1
    assert "doesn't have exactly one head" in out.stdout


def test_a_repair_that_re_points_onto_one_head_passes(forked_main: Path):
    _write(forked_main, "bbb1", "aaa1")  # the #1847 shape
    _commit(forked_main, "repair")
    out = _run_guard(forked_main)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "Repair accepted" in out.stdout


def test_a_repair_that_also_adds_a_migration_still_fails(forked_main: Path):
    _write(forked_main, "bbb1", "aaa1")
    _write(forked_main, "ccc1", "bbb1")
    _commit(forked_main, "repair plus a new migration")
    out = _run_guard(forked_main)
    assert out.returncode == 1
    assert "Repair accepted" not in out.stdout


def test_a_third_head_on_a_forked_main_still_fails(forked_main: Path):
    _write(forked_main, "ccc1", "base0")
    _commit(forked_main, "third head")
    out = _run_guard(forked_main)
    assert out.returncode == 1


def test_a_repair_that_drops_a_revision_still_fails(forked_main: Path):
    (forked_main / VERSIONS / "bbb1_m.py").unlink()
    # A deletion alone is filtered out of the guard's diff (ACMR); make the
    # surviving head's file actually change so the guard looks at the chain.
    f = forked_main / VERSIONS / "aaa1_m.py"
    f.write_text(f.read_text(encoding="utf-8").replace('"""m aaa1"""', '"""m aaa1 (touched)"""'), encoding="utf-8")
    _commit(forked_main, "drop a head instead of re-pointing it")
    out = _run_guard(forked_main)
    assert out.returncode == 1


def test_a_single_head_main_is_unaffected(tmp_path: Path):
    repo = tmp_path / "clean"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, "base0", None)
    _write(repo, "aaa1", "base0")
    _commit(repo, "clean main")
    _git(repo, "checkout", "-q", "-b", "pr")
    _write(repo, "bbb1", "aaa1")
    _commit(repo, "extends the head")
    out = _run_guard(repo)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "cleanly" in out.stdout
