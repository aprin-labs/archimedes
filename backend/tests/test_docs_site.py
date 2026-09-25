"""The docs site must publish what the repository actually contains (#1381).

`mkdocs.yml` mounts `docs/` at the site root and, via
`.github/scripts/mkdocs_hooks.py`, the agent-generated `openwiki/` tree at
`/openwiki/`. Three ways that arrangement rots, each with a test here:

1. **A nav entry outgrows its file.** `mkdocs build --strict` catches this in the
   docs-site workflow, but that workflow is path-filtered — a PR that renames a
   doc without touching `mkdocs.yml`'s paths never runs it. This suite runs on
   every PR.
2. **A regenerated wiki adds a page nobody publishes.** `openwiki/**` nav entries
   are listed by hand *on purpose*: auto-generating them would put new
   agent-written pages in front of readers with no person in the loop. The cost
   of that choice is that a new page is silently unpublished unless something
   fails, which is `test_every_openwiki_page_is_in_the_nav`.
3. **The provenance label quietly disappears.** An agent-generated section that
   is not labelled as agent-generated is worse than not publishing it.

Also guarded: the workflow's `paths:` filter (a wiki regeneration that does not
trigger the build never reaches the site) and its `--strict` flag (dropping it
turns a broken-link failure back into a silent warning).

And, since #1634, **where** it is served from: our own S3 + CloudFront
(`docs-site/infra/main.tf`), not GitHub Pages. That half has its own way of
rotting — a workflow that builds but publishes nowhere, a terraform root no
gate parses, a runbook still describing console steps that do nothing — so the
last section here checks the workflow, the terraform and `infra-gate.yml`
against each other rather than each on its own.

Hermetic: reads committed YAML and markdown off disk. No DB, Redis, RPC,
network, or `.env`. `yaml` is available in the CI unit-test image because
`backend/requirements.txt` pins `uvicorn[standard]`, whose `standard` extra
requires `pyyaml>=5.1`.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MKDOCS_YML = REPO_ROOT / "mkdocs.yml"
DOCS_INDEX = REPO_ROOT / "docs" / "doc-index.md"
DOCS_SITE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "docs-site.yml"
INFRA_GATE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "infra-gate.yml"
DOCS_SITE_RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "docs-site-setup.md"

#: The terraform root that serves the site, and the bucket it creates (#1634).
#: Written down here so the workflow, the terraform and the gate can be checked
#: against one another rather than each being read on its own.
DOCS_SITE_TF_ROOT = "docs-site/infra"
DOCS_SITE_TF = REPO_ROOT / "docs-site" / "infra" / "main.tf"
DOCS_BUCKET = "archimedes-docs-site-037613907429"

#: Human-written index of the agent-generated section, and the nav label above it.
PROVENANCE_DOC = "agent-wiki.md"
WIKI_SECTION = "Agent-generated wiki"


def _load_hooks():
    """Import `.github/scripts/mkdocs_hooks.py` by path — a dot-directory is not a package."""
    path = REPO_ROOT / ".github" / "scripts" / "mkdocs_hooks.py"
    spec = importlib.util.spec_from_file_location("archimedes_mkdocs_hooks", path)
    assert spec and spec.loader, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hooks = _load_hooks()


class _MkdocsLoader(yaml.SafeLoader):
    """`yaml.SafeLoader` that tolerates mkdocs' `!!python/name:` tags.

    `mkdocs.yml` names the mermaid fence formatter the way mkdocs-material
    documents it — `format: !!python/name:pymdownx.superfences.fence_code_format`
    — and `yaml.safe_load` refuses to construct that tag (`ConstructorError`),
    which would make every test in this module fail on a config mkdocs itself
    reads fine. Resolving the tag to its *name* is enough here: nothing in this
    suite calls the formatter, and a SafeLoader that imports arbitrary dotted
    paths would be the unsafe loader wearing a different hat.
    """


_MkdocsLoader.add_multi_constructor("tag:yaml.org,2002:python/name:", lambda loader, suffix, node: suffix)


def _mkdocs_config() -> dict:
    return yaml.load(MKDOCS_YML.read_text(encoding="utf-8"), Loader=_MkdocsLoader)


def _nav_targets(node) -> list[str]:
    """Every page path in the nav tree, in document order."""
    if isinstance(node, str):
        return [node]
    if isinstance(node, list):
        return [t for item in node for t in _nav_targets(item)]
    if isinstance(node, dict):
        return [t for value in node.values() for t in _nav_targets(value)]
    return []


def _wiki_section(nav) -> list:
    for item in nav:
        if isinstance(item, dict) and WIKI_SECTION in item:
            return item[WIKI_SECTION]
    raise AssertionError(f"mkdocs.yml nav has no '{WIKI_SECTION}' section — the openwiki tree is unpublished again.")


def _source_path(nav_target: str) -> Path:
    """Repository path a nav target names.

    `docs_dir` is `docs/`, so a bare target is relative to it; the hook mounts
    `openwiki/` from the repository root, so those targets are already
    repo-relative.
    """
    if nav_target.startswith(hooks.WIKI_DIR + "/"):
        return REPO_ROOT / nav_target
    return REPO_ROOT / "docs" / nav_target


# ── nav ↔ tree ───────────────────────────────────────────────────────────────────────


def test_every_nav_target_exists() -> None:
    missing = [t for t in _nav_targets(_mkdocs_config()["nav"]) if not _source_path(t).is_file()]
    assert not missing, (
        "mkdocs.yml nav names files that do not exist — the site build (--strict) will fail:\n  " + "\n  ".join(missing)
    )


#: Repo docs that are engineering reference and must NOT be published (#1751 —
#: publication is default-deny; on `main` the deny half is mkdocs' own
#: `exclude_docs`). Each one still carries a row in docs/README.md, because the
#: docs gate's index check is about the REPOSITORY index, not the site.
INTERNAL_ONLY_DOCS = (
    "specs/assoc-v1-spec.md",
    "specs/mnemonik-integration-scoping.md",
)


def _exclude_patterns() -> list[str]:
    """`exclude_docs` as a list of patterns. mkdocs accepts a block string.

    Gitignore-style `#` lines are comments and are dropped — a pattern that is
    really a comment matches nothing, which is the failure mode this whole
    check exists to notice.
    """
    raw = _mkdocs_config().get("exclude_docs") or ""
    lines = raw.splitlines() if isinstance(raw, str) else [str(x) for x in raw]
    return [s for line in lines if (s := line.strip()) and not s.startswith("#")]


def _publication_violations(excluded: set[str], navigated: set[str]) -> list[str]:
    """Which internal docs would reach the public site under this config.

    Two ways, and either one alone fails open in a different direction: mkdocs
    walks `docs_dir` and builds every file it finds regardless of the nav, so a
    nav-less page is still published at its URL; and a nav entry naming an
    excluded file is a hard build error rather than a silent no-op.
    """
    problems = []
    for rel in INTERNAL_ONLY_DOCS:
        if rel not in excluded:
            problems.append(f"{rel}: not in exclude_docs — mkdocs builds it and publishes it at its URL")
        if rel in navigated:
            problems.append(f"{rel}: named in the nav while excluded from the build — mkdocs fails the build")
    return problems


def test_internal_specs_are_excluded_from_the_site() -> None:
    for rel in INTERNAL_ONLY_DOCS:
        assert (REPO_ROOT / "docs" / rel).is_file(), f"{rel} is listed as internal but does not exist"

    problems = _publication_violations(set(_exclude_patterns()), set(_nav_targets(_mkdocs_config()["nav"])))
    assert not problems, "internal docs would be published:\n  " + "\n  ".join(problems)


def test_the_publication_guard_fires_on_both_failure_modes() -> None:
    """GUARD's adversarial companion: the predicate above must be able to fail.

    The same function, given a config that drops the exclusion, and one that
    also names the doc in the nav.
    """
    rel = INTERNAL_ONLY_DOCS[0]
    dropped = _publication_violations(excluded={"something/else.md"}, navigated=set())
    assert [p for p in dropped if p.startswith(f"{rel}:") and "not in exclude_docs" in p], dropped

    both = _publication_violations(excluded={"something/else.md"}, navigated={rel})
    assert len([p for p in both if p.startswith(f"{rel}:")]) == 2, both

    # …and the real config is clean, so a green result above is not vacuous.
    assert _publication_violations(set(_exclude_patterns()), set(_nav_targets(_mkdocs_config()["nav"]))) == []


def test_every_openwiki_page_is_in_the_nav() -> None:
    navigated = set(_nav_targets(_mkdocs_config()["nav"]))
    on_disk = {p.relative_to(REPO_ROOT).as_posix() for p in hooks.wiki_pages(REPO_ROOT)}
    unpublished = sorted(on_disk - navigated)
    assert not unpublished, (
        "openwiki pages exist but are in no nav section, so the site does not serve them:\n  "
        + "\n  ".join(unpublished)
        + f"\n\nAdd each one under the '{WIKI_SECTION}' section of mkdocs.yml, in the same change "
        "that adds the page. The list is hand-maintained on purpose: an agent-written page "
        "should be admitted to the site by a person."
    )


def test_openwiki_pages_are_the_only_thing_in_that_section() -> None:
    """The section index is the one hand-written page there; everything else is generated."""
    targets = _nav_targets(_wiki_section(_mkdocs_config()["nav"]))
    strays = [t for t in targets[1:] if not t.startswith(hooks.WIKI_DIR + "/")]
    assert targets[0] == PROVENANCE_DOC, (
        f"the first entry under '{WIKI_SECTION}' must be {PROVENANCE_DOC} — it is the section "
        f"index a reader lands on, and the page that carries the provenance note; got {targets[0]!r}"
    )
    assert not strays, (
        f"'{WIKI_SECTION}' is labelled agent-generated, so only openwiki/ pages belong under it; "
        f"hand-written docs listed there would inherit a label that is false for them: {strays}"
    )


# ── provenance ───────────────────────────────────────────────────────────────────────


def test_section_index_states_the_provenance() -> None:
    text = (REPO_ROOT / "docs" / PROVENANCE_DOC).read_text(encoding="utf-8")
    required = {
        "names the generator": "OpenWiki",
        "says an agent wrote it": "written by an AI agent",
        "says it was not line-reviewed": "not reviewed line by line",
        "names the read boundary": ".openwikiignore",
        "says the hand-written docs win": "they win",
    }
    missing = sorted(f"{why} ({needle!r})" for why, needle in required.items() if needle not in text)
    assert not missing, f"docs/{PROVENANCE_DOC} no longer states:\n  " + "\n  ".join(missing)


def test_every_wiki_page_gets_the_provenance_banner() -> None:
    """The banner travels with the page — search engines land readers on leaves."""
    for page in hooks.wiki_pages(REPO_ROOT):
        site_uri = page.relative_to(REPO_ROOT).as_posix()
        out = hooks.add_provenance_banner(page.read_text(encoding="utf-8"), site_uri)
        assert "Agent-generated page" in out, f"{site_uri} did not receive the banner"
        assert f"]({'../' * site_uri.count('/')}{PROVENANCE_DOC})" in out, (
            f"{site_uri}'s banner does not link back to the section index at the right depth"
        )


def test_section_index_is_in_the_docs_index() -> None:
    """docs/doc-index.md opens 'A doc not listed here does not exist.'"""
    assert f"({PROVENANCE_DOC})" in DOCS_INDEX.read_text(encoding="utf-8"), (
        f"docs/{PROVENANCE_DOC} has no row in docs/doc-index.md"
    )


# ── the workflow that publishes it ───────────────────────────────────────────────────


def _executable(job_name: str) -> str:
    """Everything a job actually *runs*, with YAML and shell comments stripped out.

    A raw `"aws s3 sync" in DOCS_SITE_WORKFLOW.read_text()` is not a guard: this
    workflow explains its own steps in prose, so the phrase is in a comment two
    lines above the command, and deleting the command leaves the assertion
    passing. (Demonstrated on this branch: removing the sync step's command left
    the whole module green until this helper existed.) Only `run:` bodies, `env:`
    values and `with:` values count as executed, and `#` lines inside a `run:`
    block scalar are dropped for the same reason.

    Note the asymmetry with `test_nothing_still_points_at_github_pages`, which
    deliberately scans the raw text: there, a leftover *mention* of
    `deploy-pages` is itself the defect, so prose has to be in scope.
    """
    workflow = yaml.safe_load(DOCS_SITE_WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"][job_name]
    parts: list[str] = [str(v) for v in (job.get("env") or {}).values()]
    for step in job["steps"]:
        parts.extend(str(v) for v in (step.get("with") or {}).values())
        parts.extend(line for line in str(step.get("run", "")).splitlines() if not line.lstrip().startswith("#"))
    return "\n".join(parts)


def test_workflow_rebuilds_the_site_when_the_wiki_changes() -> None:
    workflow = yaml.safe_load(DOCS_SITE_WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML parses the unquoted key `on:` as the boolean True (YAML 1.1); GitHub
    # reads it as the string. Accept whichever this PyYAML produced.
    triggers = workflow.get("on", workflow.get(True))
    for event in ("push", "pull_request"):
        paths = triggers[event]["paths"]
        assert f"{hooks.WIKI_DIR}/**" in paths, (
            f"docs-site.yml's {event} paths filter does not include '{hooks.WIKI_DIR}/**' — a "
            "regenerated wiki would merge and never reach the site."
        )
        assert ".github/scripts/mkdocs_hooks.py" in paths, (
            f"docs-site.yml's {event} paths filter does not include the build hook — changing how "
            "the site is assembled would not rebuild it."
        )


def test_workflow_builds_strict() -> None:
    assert "mkdocs build --strict" in _executable("build"), (
        "docs-site.yml no longer builds with --strict. Without it a link into docs/ or openwiki/ "
        "that names a missing file is a warning nobody reads instead of a failed build."
    )


# ── where it is served from (#1634) ──────────────────────────────────────────────────
#
# Dan's hosting call, recorded on #1634 on 2026-08-31: our own S3 + CloudFront
# (``docs-site/infra``), not GitHub Pages. The three tests below keep that call
# from silently reverting — the failure mode is not a broken build but a docs
# push that publishes nowhere, or publishes to a host we do not control.


def test_nothing_still_points_at_github_pages() -> None:
    """The Pages plumbing is gone from both the workflow and the runbook.

    Leaving any of it behind is how the migration half-reverts: a `deploy-pages`
    step that errors on every docs push, a `DOCS_SITE_ENABLED` variable nobody
    can flip to anything useful, or a runbook that still tells the next operator
    to point DNS at `a-apin.github.io`.
    """
    for path in (DOCS_SITE_WORKFLOW, DOCS_SITE_RUNBOOK):
        text = path.read_text(encoding="utf-8")
        for token in ("deploy-pages", "upload-pages-artifact", "DOCS_SITE_ENABLED", "a-apin.github.io"):
            assert token not in text, (
                f"{path.relative_to(REPO_ROOT)} still names {token!r}. The docs site is served from "
                "docs-site/infra (S3 + CloudFront), not GitHub Pages — see issue #1634."
            )


def test_workflow_publishes_to_the_docs_bucket_and_invalidates_the_edge() -> None:
    """A sync with no invalidation looks green and serves the previous build.

    CloudFront fronts the bucket with the AWS managed CachingOptimized policy, so
    an `s3 sync` on its own leaves the old pages at the edge for hours. Both
    halves have to be in the workflow, against the bucket terraform actually
    creates.
    """
    script = _executable("deploy")
    assert DOCS_BUCKET in script, (
        f"docs-site.yml's deploy job does not name the docs bucket ({DOCS_BUCKET}) that "
        "docs-site/infra/main.tf creates."
    )
    assert "aws s3 sync" in script, "docs-site.yml no longer syncs the built site to S3 — nothing publishes."
    assert "create-invalidation" in script, (
        "docs-site.yml syncs to S3 but never invalidates CloudFront: the edge would keep serving the "
        "previous build while the workflow reported success."
    )
    assert DOCS_BUCKET in DOCS_SITE_TF.read_text(encoding="utf-8"), (
        f"docs-site/infra/main.tf no longer creates {DOCS_BUCKET}, but docs-site.yml still syncs to it."
    )


def test_infra_gate_covers_the_docs_site_root() -> None:
    """A terraform root nothing parses is a root that breaks at `apply`.

    infra-gate.yml needs the new root in BOTH places: the `paths:` filter (or the
    job never fires on the PR that breaks it) and `matrix.dir` (or the run is
    green having parsed two other roots).
    """
    gate = yaml.safe_load(INFRA_GATE_WORKFLOW.read_text(encoding="utf-8"))
    triggers = gate.get("on", gate.get(True))  # PyYAML reads the bare `on:` key as True
    assert f"{DOCS_SITE_TF_ROOT}/**" in triggers["pull_request"]["paths"], (
        f"infra-gate.yml's paths filter does not include '{DOCS_SITE_TF_ROOT}/**' — a PR that breaks "
        "the docs-site terraform would not run the gate that parses it."
    )
    dirs = gate["jobs"]["infra-gate"]["strategy"]["matrix"]["dir"]
    assert DOCS_SITE_TF_ROOT in dirs, (
        f"infra-gate.yml's matrix does not include '{DOCS_SITE_TF_ROOT}' — the gate would report green "
        "having formatted and validated only the other roots."
    )


# ── the link rewriter ────────────────────────────────────────────────────────────────

# A stand-in for the mkdocs file set: what the site actually serves.
KNOWN = {"README.md", "architecture.md", "agent-wiki.md", "adr/README.md", "openwiki/quickstart.md"}


def test_out_of_docs_link_becomes_a_github_url() -> None:
    """`../CLAUDE.md` is correct on GitHub and 404s on the site. Repoint it, keep the anchor."""
    assert (
        hooks.rewrite_target("../backend/archimedes/main.py#L203", "docs/architecture.md", "architecture.md", KNOWN)
        == "https://github.com/aprin-labs/archimedes/blob/main/backend/archimedes/main.py#L203"
    )


def test_in_site_link_that_already_resolves_is_untouched() -> None:
    assert hooks.rewrite_target("adr/README.md", "docs/architecture.md", "architecture.md", KNOWN) is None


def test_wiki_page_link_into_docs_is_repointed_inside_the_site() -> None:
    """openwiki/ sits beside docs/ in the repo but under it on the site."""
    assert (
        hooks.rewrite_target("../docs/architecture.md", "openwiki/INSTRUCTIONS.md", "openwiki/INSTRUCTIONS.md", KNOWN)
        == "../architecture.md"
    )


def test_broken_in_site_link_is_left_for_strict_to_catch() -> None:
    """The guard: a link naming a file that exists nowhere must NOT be laundered.

    Rewriting it to a GitHub URL would turn a broken link into a link that looks
    fine and 404s, and would silence the `--strict` build failure that is the
    only thing checking it.
    """
    for target, repo_uri, site_uri in (
        ("no-such-doc.md", "docs/architecture.md", "architecture.md"),
        ("../openwiki/no-such-page.md", "docs/agent-wiki.md", "agent-wiki.md"),
    ):
        assert hooks.rewrite_target(target, repo_uri, site_uri, KNOWN) is None, (
            f"{target!r} names no file in the repository; leaving it alone is what makes "
            "`mkdocs build --strict` fail on it."
        )


def test_unpublished_repo_file_under_the_wiki_becomes_a_github_url() -> None:
    """`openwiki/.claims/` and `.last-update.json` are real files the site does not serve."""
    assert (
        hooks.rewrite_target("../openwiki/.last-update.json", "docs/agent-wiki.md", "agent-wiki.md", KNOWN)
        == "https://github.com/aprin-labs/archimedes/blob/main/openwiki/.last-update.json"
    )


def test_links_inside_code_blocks_are_not_rewritten() -> None:
    markdown = "See [main](../backend/archimedes/main.py).\n\n```\n[main](../backend/archimedes/main.py)\n```\n"
    out = hooks.rewrite_links(markdown, "docs/architecture.md", "architecture.md", KNOWN)
    assert out.count("https://github.com/") == 1, "a path shown inside a fenced block is an illustration, not a link"


def test_the_wiki_edit_pencil_points_at_the_real_repo_path() -> None:
    """`edit_uri` is `edit/main/docs/`; the wiki is mounted from the repo ROOT.

    So mkdocs built every openwiki pencil as `…/edit/main/docs/openwiki/…`, which
    is a path that has never existed — all 14 returned GitHub's 404. The hook's
    `on_page_context` replaces the URL for those pages and leaves every other
    page's alone.
    """
    url = hooks.wiki_edit_url("openwiki/rigor/admission-gate.md")
    assert url == "https://github.com/aprin-labs/archimedes/edit/main/openwiki/rigor/admission-gate.md"
    assert "/docs/openwiki/" not in url, "the docs/ prefix is exactly the bug this fixes"
    assert hooks.wiki_edit_url("architecture.md") is None, (
        "a page that really is under docs/ must keep the edit URL mkdocs computed for it"
    )
    assert hooks.wiki_edit_url("openwikinotreally/page.md") is None, (
        "prefix match on the directory name, not on the string"
    )


def test_wiki_pages_excludes_the_claim_sidecars() -> None:
    pages = {p.relative_to(REPO_ROOT).as_posix() for p in hooks.wiki_pages(REPO_ROOT)}
    assert pages, "openwiki/ has no pages — did the tree move?"
    assert not [p for p in pages if "/." in p or p.startswith(".")], (
        f"wiki_pages() picked up dot-prefixed bookkeeping files: {sorted(pages)}"
    )
