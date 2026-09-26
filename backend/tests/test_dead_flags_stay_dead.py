"""Reader-ban guard: the eight flags #1824 classified DEAD stay dead.

#1824's inventory scanned every flag surface in the tree and found eight names
with **no reader and no pin** — documentation residue only:

    DOCS_SITE_ENABLED · X402_WEBHOOK_SECRET · ARCHIMEDES_X402_ENABLED
    MAX_USDC_PER_DAY · ARCHIMEDES_DEBATE_ENABLED · REQUIRE_SIWE_FOR_GENERATION
    ARCHIMEDES_TRACE_PIN_ENABLED · the #1526 pin-JWT name (see :data:`_PIN_JWT`)

Nothing had to be deleted for them, which is exactly why they need a guard: a
retirement that consists of "we checked, it is gone" decays the moment someone
who never read the flip-list adds the name back. The flip-list's own drift
guard cannot catch that — its § DEAD / RETIRED section is *exempt* from the
reverse check by design (the record of a retired name is what stops it being
re-added), so a dead name gaining a reader, or a dead name being promoted back
onto an actionable table, both pass today. This closes that hole.

**What counts as residue, per surface.** The distinction is the one
``test_fusion_flag_retired.py`` already draws for ``ARCHIMEDES_FUSION_ENABLED``,
and this module reuses its prose-skipping scanner rather than restating it:

* **Source roots** — prose is exempt, actionable lines are not. A docstring
  saying *why* ``_debate_can_run`` has no flag check is the anti-re-add record
  (``agents/debate_engine.py`` keeps two on purpose, and the flip-list row says
  so). An ``os.getenv`` call, or a bare ``_FLAG = "…"`` constant read through,
  is a flag coming back.
* **Deploy-config surfaces** — **no exemption at all**, comments included:
  ``infra/``, ``.github/scripts``, ``.github/workflows``, ``docker-compose*.yml``
  and all three ``.env.example`` templates (root, ``ui/``, ``backend/``). A
  commented-out ``.env.example`` entry reads as "a secret you still need to
  generate", which is how ``X402_WEBHOOK_SECRET`` outlived its own removal by
  months. An injected name with no reader is the ``BACKTEST_REFRESH_ENABLED``
  failure: it rides forward on every task-def clone with nothing on the far end.
  ``.github/workflows`` earns its place because it is the *only* surface
  ``DOCS_SITE_ENABLED`` ever lived on (#1824's scan table: ``vars.*``, 2 flags);
  ``backend/.env.example`` because it is a live 55-line template the runbooks
  hand to operators, so a name reappearing there is a name someone goes and
  sets; and ``docker-compose*.yml`` because #1811 had to delete two compose
  defaults to retire one flag, and ``test_fusion_flag_retired.py`` has covered
  that surface for its own name since. The first cut of this guard omitted all
  three, and appending ``# X402_WEBHOOK_SECRET=`` to ``backend/.env.example``
  — the exact commented-template shape cited above — left it green.
* **The flip-list itself** — a dead name may be named only in a *record*
  section (§ DEAD / RETIRED, § Audit findings). On an actionable table it is a
  row telling someone under pressure to flip a name nothing reads.

``ARCHIMEDES_FUSION_ENABLED`` is deliberately NOT on this list. It has its own,
stricter guard (``test_fusion_flag_retired.py``, which also bans a *renamed*
fusion switch on the generation path), and as of #1824 its name appears in
``.github/scripts/ecs_rewrite_task_def.py``'s ``RETIRED_BACKEND_ENV`` on
purpose — that is the line that strips the stale pin off the live task
definition, and banning it here would forbid its own removal. The four
``BACKTEST_REFRESH_*`` names are off this list for the same reason (#1797).

Hermetic: reads committed files off disk and matches substrings in memory. No
env, no network, no DB, no app import.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# The prose/actionable split is one rule with one implementation. Importing it
# rather than re-spelling it means a fix to the docstring scanner reaches both
# guards; if that module is ever deleted, this import fails loudly instead of
# this guard silently loosening.
from tests.test_fusion_flag_retired import _executable_lines

REPO_ROOT = Path(__file__).resolve().parents[2]
FLIPLIST = REPO_ROOT / "docs" / "operations" / "feature-flag-fliplist.md"

#: The #1526 pin-JWT env name, assembled rather than spelled — the same trick
#: ``test_ipfs_pinning_absent.py`` plays, for the same reason. That guard bans
#: the vendor acronym *anywhere* under ``backend/`` and ``infra/``, tests
#: included, so a file that wrote the name out would fail the stricter guard
#: next door. Splitting it is the established convention here, not a dodge: the
#: ban stays whole, and this module still covers the surfaces that guard does
#: not walk (``.github/scripts``, the ``.env.example`` templates, the other
#: source roots, and the flip-list's actionable sections).
_PIN_JWT = "PIN" + "ATA_JWT"

#: #1824 § DEAD (8) — "no reader, no pin; documentation residue only".
DEAD_FLAGS = (
    "ARCHIMEDES_DEBATE_ENABLED",
    "ARCHIMEDES_TRACE_PIN_ENABLED",
    "ARCHIMEDES_X402_ENABLED",
    "DOCS_SITE_ENABLED",
    "MAX_USDC_PER_DAY",
    _PIN_JWT,
    "REQUIRE_SIWE_FOR_GENERATION",
    "X402_WEBHOOK_SECRET",
)

# Runtime source. Prose exempt, everything that can act is not. ``backend/archimedes``
# is the surface #1824 names; the rest are the other roots the flip-list's own
# discovery walks, included because scanning them is free and a dead flag is no
# more welcome in ``scripts/`` than in a service module.
_SOURCE_ROOTS = ("backend/archimedes", "scripts", "cli", "ui/src", "auth")
_SOURCE_SUFFIXES = (".py", ".js", ".jsx")

# Deploy config. NO exemption — a commented-out injection is still an injection
# site. ``infra`` is 50-odd text files; walking all of them beats a suffix list
# that a new ``.hcl`` or ``.env`` quietly falls out of. ``.github/workflows``
# gets the same whole-directory treatment: a workflow injects a name through
# ``env:``, ``vars.*``, a ``--build-arg`` or a heredoc, and no suffix or key
# list survives all four.
_CONFIG_ROOTS = ("infra", ".github/scripts", ".github/workflows")
_CONFIG_FILES = (".env.example", "ui/.env.example", "backend/.env.example")
#: Injection sites that sit at the repo root rather than under a scanned root.
#: Kept in step with ``test_fusion_flag_retired._CONFIG_GLOBS`` by
#: :func:`test_the_config_scan_is_a_superset_of_the_sibling_guards_surface`,
#: which fails loudly if that guard ever widens past this one.
_CONFIG_GLOBS = ("docker-compose*.yml",)
_SKIP_DIRS = frozenset({".terraform", "node_modules", "__pycache__", ".git"})

# Flip-list sections where naming a dead flag is the point. Everything else on
# that page — including a section nobody has written yet — is actionable until
# someone argues otherwise here, which is the direction that fails safe.
_RECORD_HEADINGS = ("## DEAD / RETIRED", "## Audit findings")


# ── Scanners ─────────────────────────────────────────────────────────────────


def _iter_source(root: Path):
    """Yield (path, repo-relative posix path) for every scanned non-test source file."""
    for directory in _SOURCE_ROOTS:
        base = root / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix not in _SOURCE_SUFFIXES:
                continue
            rel = path.relative_to(root).as_posix()
            if _SKIP_DIRS & set(rel.split("/")):
                continue
            if "/tests/" in f"/{rel}" or "/test/" in f"/{rel}":
                continue
            yield path, rel


def _keep(root: Path, path: Path, found: dict[str, Path]) -> None:
    """Record ``path`` under its repo-relative key unless it is skipped."""
    if not path.is_file():
        return
    rel = path.relative_to(root).as_posix()
    if _SKIP_DIRS & set(rel.split("/")):
        return
    found.setdefault(rel, path)


def _iter_config(root: Path):
    """Yield (path, repo-relative posix path) for every deploy-config file.

    Three passes, because the injection surface is not one shape: whole
    directories, named files, and repo-root globs. De-duplicated by relative
    path so a file reachable two ways is scanned once and residue line lists
    stay honest, and yielded in sorted order so failures read the same twice.
    """
    found: dict[str, Path] = {}
    for directory in _CONFIG_ROOTS:
        base = root / directory
        if base.is_dir():
            for path in base.rglob("*"):
                _keep(root, path, found)
    for name in _CONFIG_FILES:
        _keep(root, root / name, found)
    for glob in _CONFIG_GLOBS:
        for path in root.glob(glob):
            _keep(root, path, found)
    for rel in sorted(found):
        yield found[rel], rel


def source_residue(root: Path) -> dict[str, list[str]]:
    """Map each dead flag to the ``path:line`` of every *actionable* mention."""
    residue: dict[str, list[str]] = {}
    for path, rel in _iter_source(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, code in _executable_lines(rel, text):
            for flag in DEAD_FLAGS:
                if flag in code:
                    residue.setdefault(flag, []).append(f"{rel}:{lineno}")
    return residue


def config_residue(root: Path) -> dict[str, list[str]]:
    """Map each dead flag to the ``path:line`` of every mention, comments included."""
    residue: dict[str, list[str]] = {}
    for path, rel in _iter_config(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # a binary artefact cannot inject an env var
        for lineno, line in enumerate(text.splitlines(), 1):
            for flag in DEAD_FLAGS:
                if flag in line:
                    residue.setdefault(flag, []).append(f"{rel}:{lineno}")
    return residue


def fliplist_residue(doc: Path) -> dict[str, list[str]]:
    """Map each dead flag to ``line (heading)`` for mentions outside the record sections."""
    residue: dict[str, list[str]] = {}
    heading = "(preamble)"
    for lineno, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
        if line.startswith("## "):
            heading = line.strip()
            continue
        if any(heading.startswith(record) for record in _RECORD_HEADINGS):
            continue
        for flag in DEAD_FLAGS:
            if flag in line:
                residue.setdefault(flag, []).append(f"line {lineno} ({heading})")
    return residue


def fliplist_record_rows(doc: Path) -> set[str]:
    """The dead flags carrying a **table row** in § DEAD / RETIRED.

    Table rows only — prose in that section does not count. The first draft of
    this scanner accepted any line, and the paragraph directly above the table
    (which lists all eight names while explaining this guard) satisfied it, so
    deleting a row stayed green. The record is the row: it is the cell that
    carries where the name still appears and which PR killed it.
    """
    named: set[str] = set()
    heading = ""
    for line in doc.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            heading = line.strip()
            continue
        if not heading.startswith("## DEAD / RETIRED") or not line.lstrip().startswith("|"):
            continue
        named.update(flag for flag in DEAD_FLAGS if flag in line)
    return named


# ── Non-vacuity: a scanner that walks nothing passes forever ─────────────────


def test_the_source_scan_covers_the_runtime_roots() -> None:
    scanned = {rel for _, rel in _iter_source(REPO_ROOT)}
    for anchor in (
        "backend/archimedes/main.py",
        "backend/archimedes/agents/debate_engine.py",
        "ui/src/featureFlags.js",
    ):
        assert anchor in scanned, f"the dead-flag source scan stopped covering {anchor}"
    assert len(scanned) >= 100, f"source scan collapsed to {len(scanned)} files"


def test_the_config_scan_covers_the_injection_sites() -> None:
    scanned = {rel for _, rel in _iter_config(REPO_ROOT)}
    for anchor in (
        "infra/ecs.tf",
        "infra/cost_kill_switch.tf",
        ".github/scripts/ecs_rewrite_task_def.py",
        ".github/workflows/deploy.yml",
        ".github/workflows/docs-site.yml",
        "docker-compose.yml",
        ".env.example",
        "ui/.env.example",
        "backend/.env.example",
    ):
        assert anchor in scanned, f"the dead-flag config scan stopped covering {anchor}"


def test_the_config_scan_is_a_superset_of_the_sibling_guards_surface() -> None:
    """This guard must never cover *less* config than the one it inherits from.

    ``test_fusion_flag_retired.py`` bans one name across its own
    ``_CONFIG_GLOBS``. Eight names deserve at least the same reach, and the
    first cut of this module did not have it — it omitted ``docker-compose*``
    entirely. Comparing the resolved file sets (not the glob strings) means a
    glob added *there* fails *here* until it is covered, rather than silently
    leaving these eight with a narrower net than the one flag next door.
    """
    from tests.test_fusion_flag_retired import _CONFIG_GLOBS as _SIBLING_GLOBS

    sibling = {
        path.relative_to(REPO_ROOT).as_posix()
        for glob in _SIBLING_GLOBS
        for path in REPO_ROOT.glob(glob)
        if path.is_file() and not _SKIP_DIRS & set(path.relative_to(REPO_ROOT).parts)
    }
    assert sibling, "the sibling guard's config globs resolved to nothing — it moved or was renamed"
    uncovered = sorted(sibling - {rel for _, rel in _iter_config(REPO_ROOT)})
    assert not uncovered, (
        "test_fusion_flag_retired.py scans deploy-config files this guard does "
        f"not: {uncovered}. One retired flag is watched on those surfaces and "
        "the eight DEAD names are not. Add the surface to _CONFIG_ROOTS / "
        "_CONFIG_FILES / _CONFIG_GLOBS above."
    )


def test_the_fliplist_scan_reads_a_page_with_sections() -> None:
    """A doc whose headings stopped parsing would exempt everything silently."""
    assert FLIPLIST.is_file(), FLIPLIST
    text = FLIPLIST.read_text(encoding="utf-8")
    assert "## DEAD / RETIRED" in text, "the record section this guard exempts is gone"
    assert "## LIVE" in text, "the actionable section this guard protects is gone"


# ── The guard ────────────────────────────────────────────────────────────────


def test_no_dead_flag_has_a_reader_in_the_source_tree() -> None:
    residue = source_residue(REPO_ROOT)
    assert not residue, (
        "Flags #1824 retired as DEAD have an actionable mention again — an env "
        "read, or an env-name constant something reads through. Each of these "
        "has had no reader and no pin since the PR named in "
        "docs/operations/feature-flag-fliplist.md § DEAD / RETIRED; re-adding "
        "one means re-deciding it there first, with a row on an actionable "
        "table and a pin in infra/ecs.tf. Prose explaining a retirement is "
        "exempt and is not what tripped this.\n"
        + "\n".join(f"  {flag}: {sites}" for flag, sites in sorted(residue.items()))
    )


def test_no_dead_flag_is_injected_by_the_deploy_config() -> None:
    residue = config_residue(REPO_ROOT)
    assert not residue, (
        "Flags #1824 retired as DEAD appear in deploy config again. An injected "
        "name with no reader is not harmless: it rides forward on every "
        "task-definition clone and reads as a switch someone still has to set "
        "— the BACKTEST_REFRESH_ENABLED and X402_WEBHOOK_SECRET failures both "
        "had exactly this shape. Commented-out lines count, deliberately.\n"
        + "\n".join(f"  {flag}: {sites}" for flag, sites in sorted(residue.items()))
    )


def test_no_dead_flag_sits_on_an_actionable_flip_list_row() -> None:
    residue = fliplist_residue(FLIPLIST)
    assert not residue, (
        "Flags #1824 retired as DEAD are named outside the record sections of "
        f"{FLIPLIST.relative_to(REPO_ROOT)}. § DEAD / RETIRED is exempt from "
        "that page's own reverse guard, so a dead name promoted back onto § "
        "LIVE / § FLIP-AT-LAUNCH / § Deployed knobs passes every other check "
        "and tells the next operator to flip something nothing reads.\n"
        + "\n".join(f"  {flag}: {sites}" for flag, sites in sorted(residue.items()))
    )


def test_every_dead_flag_still_has_its_record_row() -> None:
    """The positive half: deleting the record is how a retired flag comes back.

    The flip-list's § Removing a flag says "move the row, do not delete it".
    Nothing enforced that for a name already retired — the reverse guard exempts
    the section, so emptying it is invisible. This is the check that makes the
    exemption safe.
    """
    named = fliplist_record_rows(FLIPLIST)
    missing = sorted(set(DEAD_FLAGS) - named)
    assert not missing, (
        "These retired flags lost their § DEAD / RETIRED row on "
        f"{FLIPLIST.relative_to(REPO_ROOT)}: {missing}. That row is the whole "
        "anti-re-add record — it is why the section is exempt from the reverse "
        "drift guard. Restore it rather than deleting this assertion."
    )


# ── Adversarial: the guard must be shown to reject ───────────────────────────


@pytest.fixture()
def synthetic_tree(tmp_path: Path) -> Path:
    """A clean tree shaped like the real one: the dead names nowhere actionable."""
    services = tmp_path / "backend" / "archimedes" / "services"
    services.mkdir(parents=True)
    (services / "widget.py").write_text(
        'import os\n\n\ndef enabled() -> bool:\n    return os.getenv("PAYMENTS_DRY_RUN", "true") == "true"\n',
        encoding="utf-8",
    )
    (tmp_path / "infra").mkdir()
    (tmp_path / "infra" / "ecs.tf").write_text(
        '        { name = "APP_ENV", value = "production" },\n', encoding="utf-8"
    )
    scripts = tmp_path / ".github" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "ecs_rewrite_task_def.py").write_text(
        'RETIRED_BACKEND_ENV = ("BACKTEST_REFRESH_ENABLED",)\n', encoding="utf-8"
    )
    (tmp_path / ".env.example").write_text("APP_ENV=development\n", encoding="utf-8")
    backend = tmp_path / "backend"
    (backend / ".env.example").write_text(
        "# Archimedes Backend — Environment Variables\nARC_CHAIN_ID=13068200\n", encoding="utf-8"
    )
    (tmp_path / "docker-compose.yml").write_text(
        'services:\n  backend:\n    environment:\n      APP_ENV: "development"\n', encoding="utf-8"
    )
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "deploy.yml").write_text('name: Deploy\nenv:\n  APP_ENV: "production"\n', encoding="utf-8")
    return tmp_path


def _doc(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "fliplist.md"
    path.write_text(body, encoding="utf-8")
    return path


_FULL_RECORD = "\n".join(f"| `{flag}` | nowhere | **RETIRED**. |" for flag in DEAD_FLAGS)


def test_guard_passes_on_a_clean_synthetic_tree(synthetic_tree: Path) -> None:
    assert source_residue(synthetic_tree) == {}
    assert config_residue(synthetic_tree) == {}
    doc = _doc(synthetic_tree, f"## LIVE\n\n| `PAYMENTS_DRY_RUN` | x |\n\n## DEAD / RETIRED\n\n{_FULL_RECORD}\n")
    assert fliplist_residue(doc) == {}
    assert fliplist_record_rows(doc) == set(DEAD_FLAGS)


def test_guard_rejects_a_reintroduced_reader(synthetic_tree: Path) -> None:
    target = synthetic_tree / "backend" / "archimedes" / "services" / "widget.py"
    target.write_text(
        "import os\n\n\ndef siwe_required() -> bool:\n"
        '    return os.getenv("REQUIRE_SIWE_FOR_GENERATION", "") == "true"\n',
        encoding="utf-8",
    )
    assert source_residue(synthetic_tree) == {
        "REQUIRE_SIWE_FOR_GENERATION": ["backend/archimedes/services/widget.py:5"]
    }


def test_guard_rejects_an_env_name_constant(synthetic_tree: Path) -> None:
    """The indirection dodge: the literal in a constant, the read through it."""
    target = synthetic_tree / "backend" / "archimedes" / "services" / "widget.py"
    target.write_text(
        'import os\n\n_FLAG = "ARCHIMEDES_DEBATE_ENABLED"\n\n\ndef on() -> bool:\n    return bool(os.getenv(_FLAG))\n',
        encoding="utf-8",
    )
    assert source_residue(synthetic_tree) == {"ARCHIMEDES_DEBATE_ENABLED": ["backend/archimedes/services/widget.py:3"]}


def test_guard_tolerates_prose_that_explains_the_retirement(synthetic_tree: Path) -> None:
    """The two ``debate_engine.py`` doc-comments are the record, not residue."""
    target = synthetic_tree / "backend" / "archimedes" / "services" / "widget.py"
    target.write_text(
        '"""The ARCHIMEDES_DEBATE_ENABLED flag is retired; the society is unconditional."""\n'
        "\n"
        "# Do not reintroduce REQUIRE_SIWE_FOR_GENERATION: generation auth is unconditional.\n"
        "def run():\n"
        "    return []\n",
        encoding="utf-8",
    )
    assert source_residue(synthetic_tree) == {}


def test_guard_rejects_a_terraform_only_reintroduction(synthetic_tree: Path) -> None:
    """The injection half: no reader, but the deploy pins the name again."""
    (synthetic_tree / "infra" / "ecs.tf").write_text(
        '        { name = "ARCHIMEDES_X402_ENABLED", value = "true" },\n', encoding="utf-8"
    )
    assert config_residue(synthetic_tree) == {"ARCHIMEDES_X402_ENABLED": ["infra/ecs.tf:1"]}


def test_guard_rejects_a_commented_out_env_example_entry(synthetic_tree: Path) -> None:
    """A commented template line reads as "a secret you still need to generate"."""
    (synthetic_tree / ".env.example").write_text("APP_ENV=development\n# X402_WEBHOOK_SECRET=\n", encoding="utf-8")
    assert config_residue(synthetic_tree) == {"X402_WEBHOOK_SECRET": [".env.example:2"]}


def test_guard_rejects_a_backend_env_example_entry(synthetic_tree: Path) -> None:
    """The flagship scenario, on the template the guard first forgot.

    ``backend/.env.example`` is a live 55-line template that
    ``docs/runbooks/t3.2-contract-redeploy.md`` hands to whoever redeploys. A
    commented ``# X402_WEBHOOK_SECRET=`` there is *the* shape this module's
    docstring cites — "a secret you still need to generate" — and until this
    surface was added the guard read straight past it.
    """
    (synthetic_tree / "backend" / ".env.example").write_text(
        "ARC_CHAIN_ID=13068200\n# X402_WEBHOOK_SECRET=\nMAX_USDC_PER_DAY=500\n", encoding="utf-8"
    )
    assert config_residue(synthetic_tree) == {
        "MAX_USDC_PER_DAY": ["backend/.env.example:3"],
        "X402_WEBHOOK_SECRET": ["backend/.env.example:2"],
    }


def test_guard_rejects_a_compose_default(synthetic_tree: Path) -> None:
    """#1811 had to delete two compose defaults to retire one flag.

    A compose ``environment:`` entry is an injection site with no reader on the
    far end — the same shape as an ``ecs.tf`` pin, on the surface developers
    actually run locally. ``test_fusion_flag_retired.py`` has covered it for
    ``ARCHIMEDES_FUSION_ENABLED`` since 2026-09-02; the eight get it now too.
    """
    (synthetic_tree / "docker-compose.yml").write_text(
        'services:\n  backend:\n    environment:\n      X402_WEBHOOK_SECRET: "abc"\n', encoding="utf-8"
    )
    assert config_residue(synthetic_tree) == {"X402_WEBHOOK_SECRET": ["docker-compose.yml:4"]}


def test_guard_rejects_a_workflow_env_entry(synthetic_tree: Path) -> None:
    """The only surface ``DOCS_SITE_ENABLED`` ever lived on.

    #1824's scan table lists ``vars.*`` in ``.github/workflows`` as a flag
    surface in its own right, and ``DOCS_SITE_ENABLED`` never appeared anywhere
    else — so a guard that skipped workflows could not have caught that one of
    the eight coming back at all. Both spellings count: a ``vars.`` reference
    and a plain ``env:`` pin are the same injection.
    """
    (synthetic_tree / ".github" / "workflows" / "deploy.yml").write_text(
        "name: Deploy\n"
        "env:\n"
        '  MAX_USDC_PER_DAY: "500"\n'
        "jobs:\n"
        "  publish:\n"
        "    if: vars.DOCS_SITE_ENABLED == 'true'\n",
        encoding="utf-8",
    )
    assert config_residue(synthetic_tree) == {
        "DOCS_SITE_ENABLED": [".github/workflows/deploy.yml:6"],
        "MAX_USDC_PER_DAY": [".github/workflows/deploy.yml:3"],
    }


def test_guard_rejects_a_dead_name_on_the_deploy_rewrite(synthetic_tree: Path) -> None:
    """Even a comment in ``.github/scripts`` counts: that path writes the task-def.

    Spelled through :data:`_PIN_JWT` rather than written out, because this is
    the surface ``test_ipfs_pinning_absent.py`` does NOT walk — it stops at
    ``backend/`` and ``infra/`` — so this assertion is the one that would catch
    the pin JWT creeping back in via the deploy rewrite.
    """
    (synthetic_tree / ".github" / "scripts" / "ecs_rewrite_task_def.py").write_text(
        f"# {_PIN_JWT} is seeded from SSM\nRETIRED_BACKEND_ENV = ()\n", encoding="utf-8"
    )
    assert config_residue(synthetic_tree) == {_PIN_JWT: [".github/scripts/ecs_rewrite_task_def.py:1"]}


def test_guard_rejects_a_dead_row_promoted_to_an_actionable_table(synthetic_tree: Path) -> None:
    doc = _doc(
        synthetic_tree,
        f"## LIVE\n\n| `DOCS_SITE_ENABLED` | somewhere | flip it |\n\n## DEAD / RETIRED\n\n{_FULL_RECORD}\n",
    )
    assert fliplist_residue(doc) == {"DOCS_SITE_ENABLED": ["line 3 (## LIVE)"]}


def test_guard_rejects_a_dead_name_in_a_brand_new_section(synthetic_tree: Path) -> None:
    """A section nobody has classified is actionable until someone argues otherwise."""
    doc = _doc(
        synthetic_tree,
        f"## DEAD / RETIRED\n\n{_FULL_RECORD}\n\n## Provisional\n\n| `MAX_USDC_PER_DAY` | tbd |\n",
    )
    assert list(fliplist_residue(doc)) == ["MAX_USDC_PER_DAY"]


def test_guard_rejects_an_emptied_record_section(synthetic_tree: Path) -> None:
    """Deleting the row is how a retired flag comes back through the exempt door."""
    doc = _doc(synthetic_tree, "## LIVE\n\n| `PAYMENTS_DRY_RUN` | x |\n\n## DEAD / RETIRED\n\n(none)\n")
    assert fliplist_record_rows(doc) == set()


def test_prose_in_the_record_section_does_not_stand_in_for_a_row(synthetic_tree: Path) -> None:
    """The mutation that caught the first draft of this scanner.

    The paragraph introducing § DEAD / RETIRED names all eight flags while
    explaining this guard. Counting any line made it satisfy the completeness
    check on its own, so deleting a row stayed green — the guard would have
    been vacuous against the exact failure it exists to catch.
    """
    prose = "This guard covers " + ", ".join(f"`{flag}`" for flag in DEAD_FLAGS) + ".\n"
    rows_but_one = "\n".join(f"| `{flag}` | nowhere | **RETIRED**. |" for flag in DEAD_FLAGS[1:])
    doc = _doc(synthetic_tree, f"## DEAD / RETIRED\n\n{prose}\n{rows_but_one}\n")
    assert fliplist_record_rows(doc) == set(DEAD_FLAGS[1:])
    assert DEAD_FLAGS[0] not in fliplist_record_rows(doc)


def test_guard_does_not_ban_a_live_flag_that_shares_a_prefix(synthetic_tree: Path) -> None:
    """``ARCHIMEDES_FUSION_REAL_DATA`` and friends must not be caught by an X402/DEBATE scan."""
    target = synthetic_tree / "backend" / "archimedes" / "services" / "widget.py"
    target.write_text(
        'import os\n\n\ndef real_data() -> bool:\n    return os.getenv("ARCHIMEDES_FUSION_REAL_DATA", "1") == "1"\n',
        encoding="utf-8",
    )
    (synthetic_tree / "infra" / "ecs.tf").write_text(
        '        { name = "DEBATE_POOL_MAX", value = "10" },\n', encoding="utf-8"
    )
    assert source_residue(synthetic_tree) == {}
    assert config_residue(synthetic_tree) == {}
