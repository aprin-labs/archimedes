"""Every `exclude_docs` pattern in mkdocs.yml must match at least one real file.

Why: mkdocs' `exclude_docs` uses gitignore semantics, and a pattern that
matches nothing is silent — `mkdocs build --strict` stays green while the
page it meant to hide keeps publishing. That already happened once (the
audit's block carried trailing `# comments` inside the patterns, so `team.md`
was still live). This guard turns a dead pattern into a red test.

`pathspec` is imported outright rather than through `pytest.importorskip`. It is
the library mkdocs itself matches these patterns with (`mkdocs/structure/files.py`
compiles `exclude_docs` into a `pathspec.GitIgnoreSpec`), so it is the only way
for this test to mean the same thing the build means — and it was NOT installed
in the backend unit-test job (`pip show pathspec` → required by mkdocs and mypy,
neither of which that job installs), so every case here silently skipped in CI.
`backend/tests/test_env_requirements_alignment.py` names that failure mode: "a
guard that can only run when an undeclared transitive import happens to be
present is not a guard". It is a declared test dependency now — `environment.yml`
and the two `pip install` lines in `.github/workflows/quality-gate.yml`.
"""

from __future__ import annotations

from pathlib import Path

import pathspec
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MKDOCS = REPO_ROOT / "mkdocs.yml"
DOCS = REPO_ROOT / "docs"


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_multi_constructor("tag:yaml.org,2002:python/name:", lambda loader, suffix, node: suffix)


def _exclude_patterns() -> list[str]:
    cfg = yaml.load(MKDOCS.read_text(encoding="utf-8"), Loader=_Loader)
    raw = cfg.get("exclude_docs") or ""
    return [ln.strip() for ln in raw.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def test_the_block_is_not_empty():
    assert len(_exclude_patterns()) >= 10, "exclude_docs lost its patterns"


@pytest.mark.parametrize("pattern", _exclude_patterns())
def test_every_exclude_pattern_matches_a_real_file(pattern: str):
    spec = pathspec.GitIgnoreSpec.from_lines([pattern])
    files = [str(p.relative_to(DOCS)) for p in DOCS.rglob("*") if p.is_file()]
    hits = [f for f in files if spec.match_file(f)]
    assert hits, (
        f"exclude_docs pattern {pattern!r} matches no file under docs/ — a dead pattern is silent "
        "(the page it meant to hide still publishes). Fix the pattern or delete the line."
    )
