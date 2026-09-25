"""Publication on docs.archimedes-arc.com is default-deny (#1751).

`#1816` curated the nav and listed ~90 internal pages in `exclude_docs`, which
made the *existing* leaks stop leaking. It did not change the rule underneath:
mkdocs walks `docs_dir` and builds every markdown file it finds, so the curated
nav was an allow-list of links while the site stayed an allow-everything build.
A deny-list only ever catches the files somebody already thought of — a NEW
`docs/runbooks/*.md` or `docs/api/*.md` published the day it was committed, with
no nav entry and no review, exactly as `/runbooks/cost-kill-switch/` and
`/api/admin-private/` did before the audit found them by hand.

So there are now three publication controls in `mkdocs.yml`, and a file must be
in one of them:

* **`nav:`** — publishes the page and lists it.
* **`not_in_nav:`** — publishes a file with no nav entry. Deliberately tiny and
  extension-scoped: the site's own assets, nothing that renders as a page.
* **`exclude_docs:`** — records that a file is internal; it is never built.

`.github/scripts/mkdocs_hooks.py::deny_unlisted` enforces that at build time by
marking anything in none of the three `InclusionLevel.EXCLUDED`. This module is
the other half: the build denies quietly and correctly, and these tests make the
same condition *loud*, on every PR, without needing mkdocs installed — so a page
added with no decision behind it fails CI here rather than being silently
dropped from a site nobody rebuilt yet.

Hermetic: reads committed YAML and the `docs/` tree off disk. No DB, Redis, RPC,
network or `.env`.
"""

from __future__ import annotations

import enum
import importlib.util
import re
import sys
import types
from pathlib import Path

import pathspec
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MKDOCS_YML = REPO_ROOT / "mkdocs.yml"
DOCS_DIR = REPO_ROOT / "docs"

#: mkdocs excludes these before any config is consulted
#: (`mkdocs/structure/files.py::_default_exclude`), so they are not the repo's
#: to classify. Mirrored here rather than assumed: if mkdocs ever stopped
#: excluding dotfiles, a `docs/.env` would publish and this module would be
#: the thing that failed to notice.
MKDOCS_DEFAULT_EXCLUDE = (".*", "/templates/")


def _load_hooks():
    """Import `.github/scripts/mkdocs_hooks.py` by path — a dot-directory is not a package."""
    path = REPO_ROOT / ".github" / "scripts" / "mkdocs_hooks.py"
    spec = importlib.util.spec_from_file_location("archimedes_mkdocs_hooks_deny", path)
    assert spec and spec.loader, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hooks = _load_hooks()


class _MkdocsLoader(yaml.SafeLoader):
    """`yaml.SafeLoader` that tolerates mkdocs' `!!python/name:` tags — see test_docs_site.py."""


_MkdocsLoader.add_multi_constructor("tag:yaml.org,2002:python/name:", lambda loader, suffix, node: suffix)


def _config() -> dict:
    return yaml.load(MKDOCS_YML.read_text(encoding="utf-8"), Loader=_MkdocsLoader)


def _spec(block: str | None) -> pathspec.GitIgnoreSpec:
    """Compile one gitignore-style config block the way mkdocs' `PathSpec` option does."""
    lines = [ln.strip() for ln in (block or "").splitlines()]
    return pathspec.GitIgnoreSpec.from_lines([ln for ln in lines if ln and not ln.startswith("#")])


def _docs_files() -> list[str]:
    """Every file under `docs/`, as the source URIs mkdocs would give them."""
    default_excluded = pathspec.GitIgnoreSpec.from_lines(MKDOCS_DEFAULT_EXCLUDE)
    return sorted(
        uri
        for uri in (p.relative_to(DOCS_DIR).as_posix() for p in DOCS_DIR.rglob("*") if p.is_file())
        if not default_excluded.match_file(uri)
    )


# ── the guard ────────────────────────────────────────────────────────────────────────


def test_every_file_under_docs_is_published_or_excluded_on_purpose() -> None:
    """A file in neither the nav, `not_in_nav`, nor `exclude_docs` fails here.

    This is the test that turns "a new page publishes by accident" into "a new
    page needs a decision". It is deliberately a whole-tree sweep rather than a
    check on the diff: a page can also become unclassified because a nav row was
    renamed out from under it.
    """
    config = _config()
    nav = hooks.nav_source_paths(config.get("nav"))
    allowed = _spec(config.get("not_in_nav"))
    excluded = _spec(config.get("exclude_docs"))

    unclassified = [
        uri for uri in _docs_files() if uri not in nav and not allowed.match_file(uri) and not excluded.match_file(uri)
    ]
    assert not unclassified, (
        "these files under docs/ are in no nav entry, no `not_in_nav` pattern and no "
        "`exclude_docs` pattern, so nothing in mkdocs.yml says whether they should be "
        "public:\n  - " + "\n  - ".join(unclassified) + "\n\n"
        "The docs site is default-deny (#1751), so they are NOT on the site today — "
        "`.github/scripts/mkdocs_hooks.py::deny_unlisted` drops them and turns the "
        "`--strict` build red. Pick one, in the change that adds the file:\n"
        "  * a nav row in mkdocs.yml           — publish it, and list it;\n"
        "  * a `not_in_nav` pattern            — publish a site asset with no nav entry;\n"
        "  * an `exclude_docs` pattern         — it is internal; port it to the private\n"
        "                                        docs repo first (docs/CONVENTIONS.md\n"
        "                                        § Content routing)."
    )


def test_no_file_is_both_navigated_and_excluded() -> None:
    """A nav row pointing at an excluded file is a dead link mkdocs only whispers about.

    `mkdocs/structure/nav.py` logs that case at `min(logging.INFO, …)`, which
    `--strict` does not fail on — so the nav would render a link to a page the
    build deliberately did not write.
    """
    config = _config()
    excluded = _spec(config.get("exclude_docs"))
    contradictions = sorted(
        uri
        for uri in hooks.nav_source_paths(config.get("nav"))
        if not uri.startswith("openwiki/") and excluded.match_file(uri)
    )
    assert not contradictions, (
        "mkdocs.yml both lists these in the nav and excludes them from the build, so the "
        "nav renders a link to a page that was never written:\n  - " + "\n  - ".join(contradictions)
    )


def test_the_allow_list_cannot_admit_a_page() -> None:
    """`not_in_nav` is for the site's own assets, never for an unreviewed page.

    It is the one way onto the site that skips the curated nav, so a `**` glob
    here (`reference/**`, `runbooks/**`) would re-open exactly the hole
    default-deny closes: a future `docs/reference/internal-notes.md` would
    publish with no nav row and no reviewer. Every pattern therefore names a
    non-page file type, and this test is what holds that.
    """
    allowed = _spec(_config().get("not_in_nav"))
    pages = sorted(uri for uri in _docs_files() if uri.endswith(".md") and allowed.match_file(uri))
    assert not pages, (
        "`not_in_nav` in mkdocs.yml allow-lists markdown, which publishes a page with no nav "
        "entry:\n  - " + "\n  - ".join(pages) + "\n\n"
        "Give the page a nav row instead. If a page genuinely must be public and unlisted, "
        "that is a decision to argue for in review — change this test deliberately, not the glob."
    )


def test_the_allow_list_is_small_and_anchored() -> None:
    """Read the patterns themselves, not just what they happen to match today.

    `test_the_allow_list_cannot_admit_a_page` can only see the tree as it is now:
    `reference/**` matches no markdown today and would pass it, while quietly
    admitting every page anyone adds under `docs/reference/` tomorrow.
    """
    patterns = [ln.strip() for ln in (_config().get("not_in_nav") or "").splitlines() if ln.strip()]
    assert patterns, (
        "not_in_nav lost its patterns — the site's own assets (logo, favicon, diagrams) will stop publishing"
    )
    open_ended = [p for p in patterns if p.endswith(("/**", "/*", "**"))]
    assert not open_ended, (
        "a directory glob in `not_in_nav` publishes whatever lands in that directory next, with "
        "no nav row and no reviewer: " + ", ".join(open_ended) + ". Name the file types instead "
        "(`assets/*.svg`), which is what keeps this an allow-list of assets rather than a second "
        "docs tree nobody curates."
    )


# ── the mechanism ────────────────────────────────────────────────────────────────────
#
# The tests above check the *config*. These check that the hook actually denies,
# because a correct config in front of a hook that publishes everything is worth
# nothing. mkdocs is not installed in the backend unit-test job (it is a docs
# toolchain, not a runtime dependency), and `deny_unlisted` needs exactly two
# names from it, so they are stubbed here — and `test_the_inclusion_stub_matches_mkdocs`
# fails if that stub ever stops describing the real enum.


class _InclusionLevel(enum.Enum):
    """Stand-in for `mkdocs.structure.files.InclusionLevel` (values pinned by a test below)."""

    EXCLUDED = -3
    DRAFT = -2
    NOT_IN_NAV = -1
    UNDEFINED = 0
    INCLUDED = 1


class _PluginError(Exception):
    """Stand-in for `mkdocs.exceptions.PluginError`."""


class _File:
    """The three `mkdocs.structure.files.File` attributes `deny_unlisted` reads."""

    def __init__(self, abs_src_path: Path, src_uri: str, inclusion: _InclusionLevel = _InclusionLevel.UNDEFINED):
        self.abs_src_path = str(abs_src_path)
        self.src_uri = src_uri
        self.inclusion = inclusion


def _file_from_mkdocs_signature(path: str, src_dir: str, dest_dir: str, use_directory_urls: bool) -> _File:
    """`mkdocs.structure.files.File(path, src_dir, dest_dir, use_directory_urls)`.

    `on_files` constructs these for the mounted `openwiki/` tree; only the first
    two arguments decide what `deny_unlisted` sees.
    """
    del dest_dir, use_directory_urls
    return _File(Path(src_dir) / path, path)


@pytest.fixture
def stub_mkdocs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put the two names `deny_unlisted` lazily imports into `sys.modules`."""
    pkg = types.ModuleType("mkdocs")
    pkg.__path__ = []  # type: ignore[attr-defined]
    structure = types.ModuleType("mkdocs.structure")
    structure.__path__ = []  # type: ignore[attr-defined]
    files_mod = types.ModuleType("mkdocs.structure.files")
    files_mod.InclusionLevel = _InclusionLevel  # type: ignore[attr-defined]
    files_mod.File = _file_from_mkdocs_signature  # type: ignore[attr-defined]
    exceptions = types.ModuleType("mkdocs.exceptions")
    exceptions.PluginError = _PluginError  # type: ignore[attr-defined]
    for name, module in (
        ("mkdocs", pkg),
        ("mkdocs.structure", structure),
        ("mkdocs.structure.files", files_mod),
        ("mkdocs.exceptions", exceptions),
    ):
        monkeypatch.setitem(sys.modules, name, module)


def _config_for(tmp_docs: Path, nav: list, **blocks: str) -> dict:
    config = {"docs_dir": str(tmp_docs), "nav": nav}
    for key, block in blocks.items():
        config[key] = _spec(block)
    return config


@pytest.mark.usefixtures("stub_mkdocs")
def test_a_page_the_nav_does_not_name_is_denied(tmp_path: Path) -> None:
    """The one behaviour the issue asks for: a new page under docs/ does not publish."""
    listed = _File(tmp_path / "index.md", "index.md")
    new_runbook = _File(tmp_path / "runbooks" / "new-thing.md", "runbooks/new-thing.md")
    files = [listed, new_runbook]

    denied = hooks.deny_unlisted(files, _config_for(tmp_path, [{"Home": "index.md"}]))

    assert denied == ["runbooks/new-thing.md"]
    assert new_runbook.inclusion is _InclusionLevel.EXCLUDED, "the unlisted page is still in the build"
    assert listed.inclusion is _InclusionLevel.UNDEFINED, "a page the nav names must be left alone"


@pytest.mark.usefixtures("stub_mkdocs")
def test_an_allow_listed_asset_survives(tmp_path: Path) -> None:
    logo = _File(tmp_path / "assets" / "logo.svg", "assets/logo.svg")
    stray = _File(tmp_path / "assets" / "credentials.json", "assets/credentials.json")

    denied = hooks.deny_unlisted(
        [logo, stray], _config_for(tmp_path, [{"Home": "index.md"}], not_in_nav="assets/*.svg")
    )

    assert denied == ["assets/credentials.json"]
    assert logo.inclusion is _InclusionLevel.UNDEFINED


@pytest.mark.usefixtures("stub_mkdocs")
def test_an_already_excluded_page_is_not_reported_twice(tmp_path: Path) -> None:
    """`exclude_docs` is a decision, not an omission — it must not show up as unclassified."""
    internal = _File(tmp_path / "team.md", "team.md", _InclusionLevel.EXCLUDED)
    appended = _File(tmp_path / "sprint" / "card.md", "sprint/card.md")

    denied = hooks.deny_unlisted([internal, appended], _config_for(tmp_path, ["index.md"], exclude_docs="sprint/**"))

    assert denied == []


@pytest.mark.usefixtures("stub_mkdocs")
def test_theme_files_are_out_of_scope(tmp_path: Path) -> None:
    """`add_files_from_theme` runs first, so the collection also holds material's CSS/JS."""
    docs = tmp_path / "docs"
    docs.mkdir()
    theme_asset = _File(tmp_path / "theme" / "assets" / "bundle.css", "assets/bundle.css")

    denied = hooks.deny_unlisted([theme_asset], _config_for(docs, ["index.md"]))

    assert denied == [], "denying theme files would strip the site's own stylesheet"
    assert theme_asset.inclusion is _InclusionLevel.UNDEFINED


@pytest.mark.usefixtures("stub_mkdocs")
def test_a_missing_nav_stops_the_build(tmp_path: Path) -> None:
    """No nav means no allow-list. Denying everything and denying nothing are both wrong."""
    with pytest.raises(_PluginError, match="no curated"):
        hooks.deny_unlisted([_File(tmp_path / "index.md", "index.md")], {"docs_dir": str(tmp_path), "nav": None})


def test_the_inclusion_stub_matches_mkdocs() -> None:
    """Where mkdocs IS installed, the stub above must describe the real enum."""
    real = pytest.importorskip("mkdocs.structure.files").InclusionLevel
    assert {m.name: m.value for m in real} == {m.name: m.value for m in _InclusionLevel}


# ── wiring ───────────────────────────────────────────────────────────────────────────


def test_mkdocs_yml_still_loads_the_hook() -> None:
    """Default-deny lives in the hook; a config that does not load it publishes everything."""
    assert ".github/scripts/mkdocs_hooks.py" in (_config().get("hooks") or []), (
        "mkdocs.yml no longer loads .github/scripts/mkdocs_hooks.py — without it every file under "
        "docs/ publishes again, nav or no nav (#1751)"
    )


@pytest.mark.usefixtures("stub_mkdocs")
def test_on_files_denies(tmp_path: Path) -> None:
    """mkdocs only ever calls `on_files`; a `deny_unlisted` nothing calls is decoration.

    Driven through the real entry point rather than asserted on its source, so
    commenting the call out — or moving it somewhere it cannot run — fails here.
    """
    listed = _File(tmp_path / "index.md", "index.md")
    unlisted = _File(tmp_path / "runbooks" / "new-thing.md", "runbooks/new-thing.md")
    files = [listed, unlisted]
    config = {
        "docs_dir": str(tmp_path),
        "site_dir": str(tmp_path / "site"),
        "use_directory_urls": True,
        "nav": [{"Home": "index.md"}],
    }

    hooks.on_files(files, config)

    assert unlisted.inclusion is _InclusionLevel.EXCLUDED, (
        "on_files returned without denying an unlisted page — the docs site is back to publishing "
        "every file under docs/, nav or no nav (#1751)"
    )
    assert listed.inclusion is _InclusionLevel.UNDEFINED


def test_strict_build_still_fails_on_warnings() -> None:
    """The hook denies AND warns; the warning only gates because the workflow is --strict.

    Read off the build command itself: a workflow that still *mentions* `--strict`
    in a comment while running `--no-strict` would satisfy a substring check and
    gate nothing.
    """
    workflow = (REPO_ROOT / ".github" / "workflows" / "docs-site.yml").read_text(encoding="utf-8")
    commands = re.findall(r"^\s*run:\s*(mkdocs build.*)$", workflow, flags=re.MULTILINE)
    assert commands, "no `run: mkdocs build …` step in docs-site.yml — the site is not built in CI at all"
    not_strict = [c for c in commands if "--strict" not in c.replace("--no-strict", "")]
    assert not not_strict, (
        "the docs-site build no longer runs with --strict, so `deny_unlisted`'s warning about an "
        "unclassified page fails nothing (and neither does validation.nav.omitted_files): " + "; ".join(not_strict)
    )


def test_omitted_files_validation_is_the_backstop() -> None:
    """If the hook is ever removed, mkdocs' own check must still fail the strict build."""
    validation = _config().get("validation") or {}
    assert validation.get("nav", {}).get("omitted_files") == "warn", (
        "mkdocs.yml's validation.nav.omitted_files must be `warn` — mkdocs' default is `info`, "
        "which --strict ignores, so a page in no nav would once again fail nothing (#1751)"
    )
