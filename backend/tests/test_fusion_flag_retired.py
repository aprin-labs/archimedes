"""Reader-ban guard: the generation pipeline has no fusion switch (deck Q4).

``ARCHIMEDES_FUSION_ENABLED`` was retired on 2026-09-02. It was never a lever:
the debate society is the sole generation pipeline
(``docs/adr/debate-society-sole-generation-pipeline.md``) and every proposer
routes through ``StrategyFusion.propose()``
(``docs/adr/fusion-primary-generation.md``), so the OFF branch returned a
``disabled`` sentinel and made Generate silently produce nothing. Every
deployed environment pinned it ``true``; the only state the switch could
reach that prod did not already occupy was "broken".

This guard is grep-based on purpose. An import-based check ("``fusion_enabled``
is not importable") would pass the moment someone spells the switch inline as
``os.getenv("ARCHIMEDES_FUSION_ENABLED")`` inside ``propose``, which is exactly
the reintroduction worth blocking. So it bans the **name** across runtime
source and across the deploy-config surface that injects it, and it bans the
generic shape of a fusion switch in the two files that carry the generation
path.

Hermetic: reads committed files off disk and matches regexes in memory. No
env, no network, no DB, no app import.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

FLAG = "ARCHIMEDES_FUSION_ENABLED"

# Runtime source roots (same set the flip-list drift guard walks) plus the
# deploy-config files that can *inject* an env var without any reader.
_PY_ROOTS = (
    "backend/archimedes",
    "scripts",
    "cli",
    "analytics-engine/src",
    "infra/lambda",
    "mcp-server/src",
)
_JS_ROOTS = ("ui/src", "auth")
_CONFIG_GLOBS = (
    "infra/**/*.tf",
    "infra/**/*env*.txt",  # spike-1411/function-env.txt — deploy.sh injects it verbatim
    "docker-compose*.yml",
    ".env.example",
    "ui/.env.example",
)

# The two files that carry the generation path. A switch here is the failure
# this guard exists to prevent, whatever it is named.
_GENERATION_FILES = (
    "backend/archimedes/agents/strategy_fusion.py",
    "backend/archimedes/agents/generation_pipeline.py",
)
_SWITCH_SHAPE_RE = re.compile(
    r'os\.(?:getenv|environ\.get)\(\s*"[A-Z0-9_]*FUSION[A-Z0-9_]*(?:ENABLED|DISABLED)"'
    r"|def\s+fusion_enabled\b"
    r"|def\s+fusion_disabled\b"
)

# ``FUSION_SEMANTIC_RETRIEVAL`` (paper_rag's MiniLM rerank switch) is a
# different, live flag and must NOT be caught: it does not match the shape
# above, and this guard never bans the substring "FUSION" on its own.


def _iter_files(root: Path):
    """Yield (path, repo-relative posix path) for every scanned non-test file."""
    seen: set[Path] = set()
    for pattern, roots in ((("*.py"), _PY_ROOTS), (("*.js"), _JS_ROOTS), (("*.jsx"), _JS_ROOTS)):
        for directory in roots:
            base = root / directory
            if not base.is_dir():
                continue
            for path in sorted(base.rglob(pattern)):
                rel = path.relative_to(root).as_posix()
                if "/tests/" in f"/{rel}" or "/test/" in f"/{rel}" or "node_modules" in rel:
                    continue
                if path not in seen:
                    seen.add(path)
                    yield path, rel
    for glob in _CONFIG_GLOBS:
        for path in sorted(root.glob(glob)):
            if path.is_file() and path not in seen:
                seen.add(path)
                yield path, path.relative_to(root).as_posix()


def _executable_lines(rel: str, text: str):
    """Yield (lineno, code) for lines that are not prose.

    Prose keeps the name deliberately: the ADRs, the retired-flag row and the
    module docstring that says *why* there is no switch are what stop it being
    re-added — the same call `debate_engine.py` made for
    ``ARCHIMEDES_DEBATE_ENABLED``. What must not survive is a line that can
    *act*: an env read, an env-name constant, or a deploy-config injection. So
    Python comments and triple-quoted blocks are skipped, and everything else —
    including a bare ``_FLAG = "ARCHIMEDES_FUSION_ENABLED"`` constant — counts.
    Config files get no exemption at all: a commented-out ``.env.example``
    entry reads as "a switch you still need to set", which is how
    ``X402_WEBHOOK_SECRET`` survived its own removal.
    """
    is_py = rel.endswith(".py")
    is_js = rel.endswith((".js", ".jsx"))
    block_quote = ""
    for i, line in enumerate(text.splitlines(), 1):
        if is_py:
            stripped = line.lstrip()
            if block_quote:
                if block_quote in line:
                    block_quote = ""
                continue
            one_line_docstring = False
            for quote in ('"' * 3, "'" * 3):
                if not stripped.startswith(quote):
                    continue
                if stripped.count(quote) == 1:
                    block_quote = quote
                else:
                    one_line_docstring = True  # `"""one line."""`
                break
            if block_quote or one_line_docstring:
                continue
            code = line.split("#", 1)[0]
        elif is_js:
            code = line.split("//", 1)[0]
        else:
            code = line
        yield i, code


def flag_residue(root: Path) -> dict[str, list[int]]:
    """Map each file with an *actionable* mention of the retired flag to its lines."""
    residue: dict[str, list[int]] = {}
    for path, rel in _iter_files(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        hits = [i for i, code in _executable_lines(rel, text) if FLAG in code]
        if hits:
            residue[rel] = hits
    return residue


def generation_switches(root: Path) -> dict[str, list[int]]:
    """Map each generation-path file with a fusion-switch shape to its lines."""
    found: dict[str, list[int]] = {}
    for rel in _GENERATION_FILES:
        path = root / rel
        if not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        hits = [i for i, line in enumerate(lines, 1) if _SWITCH_SHAPE_RE.search(line)]
        if hits:
            found[rel] = hits
    return found


# ── The guard ────────────────────────────────────────────────────────────────


def test_scan_is_not_vacuous() -> None:
    """A scanner that walks nothing would pass forever."""
    scanned = {rel for _, rel in _iter_files(REPO_ROOT)}
    for anchor in (
        "backend/archimedes/agents/strategy_fusion.py",
        "backend/archimedes/agents/generation_pipeline.py",
        "backend/archimedes/main.py",
        "infra/ecs.tf",
        "docker-compose.yml",
    ):
        assert anchor in scanned, f"the retired-flag scan stopped covering {anchor}"


def test_the_retired_flag_has_no_reader_and_no_injection_site() -> None:
    residue = flag_residue(REPO_ROOT)
    assert not residue, (
        f"{FLAG} was retired on 2026-09-02 (deck Q4) and must not come back. "
        "Fusion is the unconditional sole generation path; the OFF branch could "
        "only make Generate silently return nothing. A name surviving in "
        "terraform / compose / .env.example is just as bad as a reader — it is "
        "how a dead switch gets re-wired.\n"
        + "\n".join(f"  {rel}: lines {lines}" for rel, lines in sorted(residue.items()))
    )


def test_the_deploy_strips_the_stale_pin_off_the_live_task_definition() -> None:
    """The repo is clean; the LIVE task definition is not (#1824).

    #1811 took the reader, the OFF branch, the ``infra/ecs.tf`` pin and both
    compose defaults in one commit — which the guard above proves. It could not
    take the entry on the **registered** task definition, because ``deploy.yml``
    clones the live revision and never applies terraform. So
    ``ARCHIMEDES_FUSION_ENABLED=true`` has ridden forward on the backend
    container ever since with nothing on the far end: the exact INERT shape
    #1824 catalogued, and the exact shape ``BACKTEST_REFRESH_ENABLED`` had.

    The fix is the deploy-path strip, not an operator ritual — #1824's standing
    rule ("a flag with no reader is deleted the same week its reader dies; when
    the pin lives in a live task definition the repo cannot edit, in the
    deploy-path strip that removes it"). Behaviourally this is a no-op by
    construction: the assertions above prove nothing reads the name, so
    deleting it from the shipped revision cannot change what the backend does.

    Mutation this catches: dropping the name from ``RETIRED_BACKEND_ENV``, which
    would leave the pin cloning forward invisibly again.
    """
    from tests.test_ecs_paper_advance_deploy_pin import _backend_env, _last_good_task_def, _load_rewrite, _rewrite

    mod = _load_rewrite()
    assert FLAG in mod.RETIRED_BACKEND_ENV, (
        f"{FLAG} came off .github/scripts/ecs_rewrite_task_def.py's "
        "RETIRED_BACKEND_ENV. That tuple is the only thing that takes the stale "
        "pin off the live task definition — the repo has been clean since "
        "2026-09-02, but every CI deploy clones the registered revision, so "
        "without this entry the retired name ships forever."
    )

    task_def = _last_good_task_def()
    backend = next(c for c in task_def["containerDefinitions"] if c["name"] == "backend")
    backend["environment"].append({"name": FLAG, "value": "true"})
    assert FLAG not in _backend_env(_rewrite(task_def))


def test_the_generation_path_has_no_fusion_switch_under_any_name() -> None:
    """Renaming the flag is the obvious way around the name ban above."""
    switches = generation_switches(REPO_ROOT)
    assert not switches, (
        "A fusion on/off switch reappeared on the generation path. Fusion is "
        "unconditional (deck Q4, 2026-09-02) — see the module docstring in "
        "backend/archimedes/agents/strategy_fusion.py.\n"
        + "\n".join(f"  {rel}: lines {lines}" for rel, lines in sorted(switches.items()))
    )


# ── Adversarial: the guard must be shown to reject ───────────────────────────


@pytest.fixture()
def synthetic_tree(tmp_path: Path) -> Path:
    """A clean tree shaped like the real one: fusion present, switch absent."""
    agents = tmp_path / "backend" / "archimedes" / "agents"
    agents.mkdir(parents=True)
    (agents / "strategy_fusion.py").write_text(
        'import os\n\n\ndef propose():\n    return os.getenv("ARCHIMEDES_CORPUS_MANIFEST")\n',
        encoding="utf-8",
    )
    (agents / "generation_pipeline.py").write_text("def run():\n    return propose()\n", encoding="utf-8")
    (tmp_path / "infra").mkdir()
    (tmp_path / "infra" / "ecs.tf").write_text(
        '        { name = "APP_ENV", value = "production" },\n', encoding="utf-8"
    )
    return tmp_path


def test_guard_passes_on_a_clean_synthetic_tree(synthetic_tree: Path) -> None:
    assert flag_residue(synthetic_tree) == {}
    assert generation_switches(synthetic_tree) == {}


def test_guard_rejects_a_reintroduced_reader(synthetic_tree: Path) -> None:
    target = synthetic_tree / "backend" / "archimedes" / "agents" / "strategy_fusion.py"
    target.write_text(
        "import os\n\n\ndef fusion_enabled() -> bool:\n"
        '    return os.getenv("ARCHIMEDES_FUSION_ENABLED", "").lower() == "true"\n',
        encoding="utf-8",
    )
    assert flag_residue(synthetic_tree) == {"backend/archimedes/agents/strategy_fusion.py": [5]}
    assert generation_switches(synthetic_tree) == {"backend/archimedes/agents/strategy_fusion.py": [4, 5]}


def test_guard_rejects_a_terraform_only_reintroduction(synthetic_tree: Path) -> None:
    """The injection half: no reader, but the deploy pins the name again."""
    (synthetic_tree / "infra" / "ecs.tf").write_text(
        '        { name = "ARCHIMEDES_FUSION_ENABLED", value = "true" },\n', encoding="utf-8"
    )
    assert flag_residue(synthetic_tree) == {"infra/ecs.tf": [1]}


def test_guard_rejects_a_renamed_switch(synthetic_tree: Path) -> None:
    """The rename dodge: a new name, the same lever."""
    target = synthetic_tree / "backend" / "archimedes" / "agents" / "generation_pipeline.py"
    target.write_text(
        "import os\n\n\ndef run():\n"
        '    if os.getenv("FUSION_PRIMARY_ENABLED", "") != "true":\n'
        "        return []\n"
        "    return propose()\n",
        encoding="utf-8",
    )
    assert flag_residue(synthetic_tree) == {}, "the new name is not the banned literal"
    assert generation_switches(synthetic_tree) == {"backend/archimedes/agents/generation_pipeline.py": [5]}


def test_guard_does_not_ban_the_live_semantic_retrieval_flag(synthetic_tree: Path) -> None:
    """FUSION_SEMANTIC_RETRIEVAL is a different, live flag — must not fire."""
    target = synthetic_tree / "backend" / "archimedes" / "agents" / "strategy_fusion.py"
    target.write_text(
        'import os\n\n\ndef rerank() -> bool:\n    return os.getenv("FUSION_SEMANTIC_RETRIEVAL", "true") == "true"\n',
        encoding="utf-8",
    )
    assert flag_residue(synthetic_tree) == {}
    assert generation_switches(synthetic_tree) == {}


def test_guard_tolerates_prose_that_explains_the_retirement(synthetic_tree: Path) -> None:
    """A docstring naming the retired flag is the record, not residue.

    Deleting it is how a retired flag gets re-added by someone who never knew
    it existed — the reason § DEAD / RETIRED is exempt from the flip-list's own
    reverse guard.
    """
    target = synthetic_tree / "backend" / "archimedes" / "agents" / "strategy_fusion.py"
    target.write_text(
        '"""Fusion is unconditional; ARCHIMEDES_FUSION_ENABLED was retired 2026-09-02."""\n'
        "\n"
        "# Do not reintroduce ARCHIMEDES_FUSION_ENABLED.\n"
        "def propose():\n"
        "    return []\n",
        encoding="utf-8",
    )
    assert flag_residue(synthetic_tree) == {}
    assert generation_switches(synthetic_tree) == {}


def test_guard_rejects_an_env_name_constant(synthetic_tree: Path) -> None:
    """The indirection dodge: the literal in a constant, the read through it."""
    target = synthetic_tree / "backend" / "archimedes" / "agents" / "generation_pipeline.py"
    target.write_text(
        "import os\n"
        "\n"
        '_FLAG = "ARCHIMEDES_FUSION_ENABLED"\n'
        "\n"
        "\n"
        "def run():\n"
        '    return [] if os.getenv(_FLAG) != "true" else propose()\n',
        encoding="utf-8",
    )
    assert flag_residue(synthetic_tree) == {"backend/archimedes/agents/generation_pipeline.py": [3]}
