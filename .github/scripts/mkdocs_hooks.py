#!/usr/bin/env python3
"""Build hooks for the Archimedes docs site (issue #1381).

Both hooks exist for the same reason: **the published site's file layout is not
the repository's file layout.** `mkdocs.yml` sets `docs_dir: docs`, so mkdocs
only ever sees files under `docs/` and mounts that directory at the site root.
Two consequences, one per hook.

`on_files` — **mount the agent-generated `openwiki/` tree.** It lives at the
repository root (#1597), outside `docs_dir`, so mkdocs cannot see it at all and
the tree was published nowhere. This hook appends its markdown pages to the
build's file set with `src_dir` set to the repo root, which puts them at
`/openwiki/...` on the site. Nav entries in `mkdocs.yml` name them explicitly
rather than being generated here: a page written by an agent should be
*admitted* to the site by a person, and `backend/tests/test_docs_site.py`
fails when a new `openwiki/**.md` has no nav entry, which is what turns "should"
into "does".

`on_page_markdown` — **repoint links that cannot resolve inside the site, and
stamp provenance on agent-generated pages.** This repo's convention is "every
claim is a link to a file" (`docs/architecture.md`), so docs link out to
`../CLAUDE.md`, `backend/...`, `contracts/...`, `ui/...`. Those links are
correct on GitHub — where these docs are read day to day, and where
`docs-gate.yml`'s `docs_links.py` validates them against the real tree — but
mkdocs, scoped to `docs_dir`, cannot resolve any of them: 371 warnings on
`main` at the time of writing, every one of them a link that would 404 on the
published site. Rather than rewrite ~47 unique targets across dozens of files
to work around a generator limitation (the tradeoff the docs-site scaffold
declined, `docs/runbooks/docs-site-setup.md`), this rewrites them **at build
time** to `https://github.com/aprin-labs/archimedes/blob/main/<path>`, preserving
`#Lnn` line anchors. The committed markdown is untouched.

**The rewrite deliberately does not paper over real breakage.** A link whose
target lands *inside* `docs/` or `openwiki/` but names a file that does not
exist is left exactly as written, so mkdocs still warns and `--strict` still
fails the build. Only targets outside the site's own source trees become
GitHub URLs. See `test_docs_site.py::test_broken_in_site_link_is_left_for_strict_to_catch`.

`on_files` also enforces **default-deny publication** (issue #1751). Removing a
page from the nav does not unpublish it: mkdocs walks `docs_dir` and builds
every markdown file it finds, in the nav or not. That made the curated nav an
*allow-list of links* while the site itself stayed an allow-everything build,
so a new `docs/runbooks/*.md` or `docs/api/*.md` published the moment it was
committed and `exclude_docs` only caught the pages someone had already thought
of. This hook inverts it: a file under `docs_dir` (or the mounted `openwiki/`
tree) is kept only if the curated `nav:` names it, or `not_in_nav` allow-lists
it explicitly. Anything else is marked `InclusionLevel.EXCLUDED` — the same
state `exclude_docs` produces, so it is not built, not copied, and every link
into it is repointed at GitHub by the rewriter below — and logged at WARNING,
which `mkdocs build --strict` (the build command in
`.github/workflows/docs-site.yml`) fails on. `mkdocs.yml`'s
`validation.nav.omitted_files: warn` is the backstop for the case where this
hook is removed: mkdocs itself then fails the strict build on the same file.

`on_page_context` — **fix the edit pencil on the mounted wiki pages.** `edit_uri`
in `mkdocs.yml` is `edit/main/docs/`, which is right for every page that really
does live under `docs/`. The openwiki pages do not: they are mounted from
the repository root, so mkdocs built them an edit URL of
`…/edit/main/docs/openwiki/…` and all 14 pencils returned GitHub's 404. The fix
lives here rather than in the config because the alternative — `edit_uri: ""`
plus a computed URL for every page — moves every working link onto new code for
the sake of the 14 broken ones.

Importability: mkdocs is imported lazily inside `on_files` so this module can be
imported by the drift test in the backend unit suite, which does not install
mkdocs. Everything else here is stdlib plus `docs_links.py`'s regexes, reused
rather than re-derived (they already carry the nested-badge-link fix from #1262).
"""

from __future__ import annotations

import logging
import posixpath
import sys
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from docs_links import BADGE_LINK, INLINE_LINK, REF_DEF, is_external, strip_code  # noqa: E402

#: Under the `mkdocs` logger on purpose: `mkdocs build --strict` counts WARNING and
#: above through a handler attached to `logging.getLogger("mkdocs")` (mkdocs/__main__.py),
#: so a logger outside that namespace would print and gate nothing.
log = logging.getLogger("mkdocs.hooks.mkdocs_hooks")

#: Repository root: <root>/.github/scripts/mkdocs_hooks.py -> parents[1] is <root>/.github.
REPO_ROOT = _SCRIPTS_DIR.parents[1]

#: Directory mkdocs mounts at the site root (must match `docs_dir` in mkdocs.yml).
DOCS_DIR = "docs"

#: Agent-generated wiki, mounted at /openwiki/ on the site by `on_files`.
WIKI_DIR = "openwiki"

#: Site URI of the human-written page that introduces the wiki section and carries
#: its provenance note. Source file: docs/agent-wiki.md.
PROVENANCE_PAGE = "agent-wiki.md"

_BLOB_BASE = "https://github.com/aprin-labs/archimedes/blob/main"
_TREE_BASE = "https://github.com/aprin-labs/archimedes/tree/main"
#: Base for `on_page_context`'s edit-pencil fix. Mirrors `edit_uri` in mkdocs.yml
#: minus its `docs/` prefix, which is exactly what is wrong for these pages.
_EDIT_BASE = "https://github.com/aprin-labs/archimedes/edit/main"

#: Stamped on every openwiki page by `add_provenance_banner`. A reader arriving from
#: a search engine lands on a leaf page, not on the section index, so the label has to
#: travel with the page. `{index}` is filled with a link back to PROVENANCE_PAGE.
_BANNER = """!!! warning "Agent-generated page — not written or line-edited by a person"

    OpenWiki produced this page from the repository itself. It is published
    without line-by-line human review, and it summarises documentation rather
    than reading the implementation. Where it disagrees with the hand-written
    docs, the frozen spec, or the running system, they win. Scope and
    provenance: [Agent-generated wiki]({index}).
"""


# ── openwiki tree ────────────────────────────────────────────────────────────────────


def wiki_pages(root: Path | str | None = None) -> list[Path]:
    """Absolute paths of the `openwiki/` markdown pages that belong on the site.

    Shared with `backend/tests/test_docs_site.py` so the nav-completeness test and
    the build agree on what "an openwiki page" is instead of each deciding
    separately. Dot-prefixed path parts are skipped: `openwiki/.claims/` holds the
    machine-checkable claim sidecars OpenWiki writes alongside each page, and
    `.last-update.json` its bookkeeping — neither is a page.
    """
    root = Path(root) if root is not None else REPO_ROOT
    wiki = root / WIKI_DIR
    if not wiki.is_dir():
        return []
    pages: list[Path] = []
    for path in sorted(wiki.rglob("*.md")):
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        pages.append(path)
    return pages


# ── default-deny publication (#1751) ─────────────────────────────────────────────────


def nav_source_paths(nav: Any) -> set[str]:
    """Every source path the curated `nav:` names, as mkdocs source URIs.

    The nav is a tree of strings, lists and one-key dicts. Only the leaves that
    address a file in the source tree count: an `- Label: https://…` row is a
    link off the site, and a site-absolute `/path` row addresses a built URL
    rather than a source file, so neither one publishes anything.

    Shared with `backend/tests/test_docs_default_deny.py` so the build and the
    guard read the nav the same way instead of each deciding separately — the
    failure mode this whole mechanism exists to prevent.
    """
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, str):
            if not is_external(node) and not node.startswith("/"):
                found.add(posixpath.normpath(node))
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)

    walk(nav)
    return found


def _is_under(abs_path: str, root: Path) -> bool:
    try:
        Path(abs_path).resolve().relative_to(root)
    except (OSError, ValueError):
        return False
    return True


def _effective_inclusion(file: Any, config: Any, inclusion_level: Any) -> Any:
    """What `mkdocs.structure.files.set_exclusions` would make of this file.

    Files mkdocs discovered under `docs_dir` already carry a level by the time a
    hook sees them. Files a hook *appended* — the mounted `openwiki/` tree — are
    still `UNDEFINED`, and mkdocs only resolves those after `on_files` returns
    (`mkdocs/commands/build.py`: "If plugins have added files but haven't set
    their inclusion level, calculate it again"). Mirroring that resolution here
    means one rule covers both, and in particular that an appended file matched
    by `exclude_docs` or `not_in_nav` is not reported as an unclassified page.
    """
    if file.inclusion is not inclusion_level.UNDEFINED:
        return file.inclusion
    for key, level in (
        ("exclude_docs", inclusion_level.EXCLUDED),
        ("draft_docs", inclusion_level.DRAFT),
        ("not_in_nav", inclusion_level.NOT_IN_NAV),
    ):
        spec = config.get(key)
        if spec is not None and spec.match_file(file.src_uri):
            return level
    return inclusion_level.INCLUDED


def deny_unlisted(files: Any, config: Any) -> list[str]:
    """Unpublish every `docs_dir`/`openwiki` file the curated nav does not name.

    This is the control that makes the nav an allow-list of *pages* rather than
    an allow-list of *links*. `exclude_docs` still works and still means what it
    said; it is now the place to record a deliberate "this is internal", not the
    only thing standing between a new file and the public site.

    Denial is `InclusionLevel.EXCLUDED` — exactly the state `exclude_docs`
    produces — so the rest of the build already handles it: the page is not
    rendered, a media file is not copied, and `published_uris` keeps every link
    into it out of the relative-link path so `rewrite_target` sends it to GitHub.

    Theme files are deliberately out of scope: `add_files_from_theme` runs before
    this hook, so the collection also holds mkdocs-material's CSS, JS and fonts
    (and `docs-site/overrides/404.html`), none of which is ever in the nav.

    Returns the denied source URIs, sorted — the return value is what
    `backend/tests/test_docs_default_deny.py` asserts on.
    """
    from mkdocs.exceptions import PluginError
    from mkdocs.structure.files import InclusionLevel

    nav_uris = nav_source_paths(config.get("nav"))
    if not nav_uris:
        # Without a nav, mkdocs generates one from the whole tree and every file
        # under docs/ publishes. Denying everything would be worse than useless
        # and denying nothing would be a lie, so the build stops.
        raise PluginError(
            "docs default-deny (#1751): mkdocs.yml has no curated `nav:`, so there is no "
            "allow-list to publish from. Restore the nav, or remove this hook deliberately "
            "and accept that every file under docs/ goes public."
        )

    roots = [Path(config["docs_dir"]).resolve(), (REPO_ROOT / WIKI_DIR).resolve()]
    denied: list[str] = []
    for file in files:
        abs_src = getattr(file, "abs_src_path", None)
        if not abs_src or not any(_is_under(abs_src, root) for root in roots):
            continue
        if _effective_inclusion(file, config, InclusionLevel) is not InclusionLevel.INCLUDED:
            continue
        if file.src_uri in nav_uris:
            continue
        file.inclusion = InclusionLevel.EXCLUDED
        denied.append(file.src_uri)

    denied.sort()
    if denied:
        log.warning(
            "docs default-deny (#1751): %d file(s) under docs/ are named by no nav entry and by no "
            "`not_in_nav` pattern, so this build does NOT publish them:\n  - %s\n"
            "Choose one, in the same change that adds the file: a nav row in mkdocs.yml publishes "
            "it; a `not_in_nav` pattern publishes it without a nav entry; an `exclude_docs` pattern "
            "records that it is internal. Until then it stays off the site and this build stays red "
            "under --strict.",
            len(denied),
            "\n  - ".join(denied),
        )
    return denied


def on_files(files: Any, config: Any) -> Any:
    """Mount the `openwiki/` tree, then deny every file the curated nav does not name."""
    # Lazy so this module imports without mkdocs — see the module docstring.
    from mkdocs.structure.files import File

    for path in wiki_pages(REPO_ROOT):
        files.append(
            File(
                path.relative_to(REPO_ROOT).as_posix(),
                str(REPO_ROOT),
                config["site_dir"],
                config["use_directory_urls"],
            )
        )
    deny_unlisted(files, config)
    return files


# ── link rewriting ───────────────────────────────────────────────────────────────────


def _site_uri_for(repo_path: str) -> str | None:
    """Site URI a repo-relative path would have, or None if it is not on the site."""
    if repo_path == DOCS_DIR:
        return ""
    if repo_path.startswith(DOCS_DIR + "/"):
        return repo_path[len(DOCS_DIR) + 1 :]
    if repo_path == WIKI_DIR or repo_path.startswith(WIKI_DIR + "/"):
        return repo_path
    return None


def _resolves(site_uri: str, known: set[str]) -> str | None:
    """Return the file `site_uri` addresses, following directory -> index/README."""
    for candidate in (site_uri, posixpath.join(site_uri, "index.md"), posixpath.join(site_uri, "README.md")):
        if candidate in known:
            return candidate
    return None


def rewrite_target(target: str, page_repo_uri: str, page_site_uri: str, known: set[str]) -> str | None:
    """Replacement for one link target, or None to leave it exactly as written.

    `page_repo_uri` is the page's path in the repository (`docs/architecture.md`,
    `openwiki/quickstart.md`); `page_site_uri` is its path in the mkdocs source
    tree (`architecture.md`, `openwiki/quickstart.md`). They differ for `docs/**`,
    which is why a link authored against the repo layout can be unresolvable
    against the site layout even though it is perfectly correct.
    """
    if is_external(target) or target.startswith(("#", "/")):
        return None
    path_part, sep, frag = target.partition("#")
    if not path_part:
        return None  # pure in-page anchor

    page_site_dir = posixpath.dirname(page_site_uri)

    # 1. mkdocs can already resolve it against the site tree — leave it alone.
    if _resolves(posixpath.normpath(posixpath.join(page_site_dir, path_part)), known):
        return None

    # 2. Resolve it the way the author wrote it: against the repository tree.
    repo_target = posixpath.normpath(posixpath.join(posixpath.dirname(page_repo_uri), path_part))
    if repo_target.startswith(".."):
        return None  # escapes the repository; nothing sensible to point at

    site_uri = _site_uri_for(repo_target)
    if site_uri is not None:
        found = _resolves(site_uri, known)
        if found is not None:
            new_path = posixpath.relpath(found, page_site_dir or ".")
            return new_path + sep + frag
        if not (REPO_ROOT / repo_target).exists():
            # Inside docs/ or openwiki/ and not a file anywhere: genuinely broken.
            # Leave it exactly as written so mkdocs warns and --strict fails. This
            # branch is the whole reason --strict is worth running.
            return None
        # Exists in the repository but the site does not publish it — the
        # openwiki/.claims/ evidence sidecars and .last-update.json, which
        # `wiki_pages` deliberately excludes. Fall through to a GitHub link.

    # 3. A repository path the site does not serve: link out to GitHub. Whether it
    # exists is not this build's gate — docs-gate.yml's docs_links.py checks every
    # docs/** link against the real tree, including the two into submodules/ that
    # only resolve in a recursive checkout.
    base = _TREE_BASE if (REPO_ROOT / repo_target).is_dir() else _BLOB_BASE
    return f"{base}/{repo_target}{sep}{frag}"


def rewrite_links(markdown: str, page_repo_uri: str, page_site_uri: str, known: set[str]) -> str:
    """Apply `rewrite_target` to every link target in `markdown` outside code."""
    # Matches are found in a code-blanked copy (same length, so offsets carry over)
    # and applied to the original: a path inside a fenced block is an illustration,
    # not a link.
    blanked = strip_code(markdown)
    spans: dict[tuple[int, int], str] = {}
    for pattern in (INLINE_LINK, BADGE_LINK, REF_DEF):
        for match in pattern.finditer(blanked):
            spans[match.span(1)] = match.group(1).strip()

    edits = []
    for (start, end), target in spans.items():
        replacement = rewrite_target(target, page_repo_uri, page_site_uri, known)
        if replacement is not None and replacement != target:
            edits.append((start, end, replacement))

    out = markdown
    for start, end, replacement in sorted(edits, reverse=True):
        out = out[:start] + replacement + out[end:]
    return out


def published_uris(files: Any) -> set[str]:
    """The source URIs the built site actually serves.

    `files` still carries the pages `exclude_docs` removed — mkdocs *marks* a
    file's inclusion level rather than dropping it — so a plain
    `{f.src_uri for f in files}` counts an excluded page as "on the site". Every
    link INTO one would then be left as a relative link, which resolves nowhere
    once the page is gone: mkdocs reports that at INFO, so `--strict` stays green
    while the published site grows dead links.

    Filtering here is what lets a page be unpublished without a tree-wide link
    scrub. A link to an excluded page falls through `rewrite_target`'s step 3 and
    becomes the GitHub blob URL for the file, which is where the content now is.

    `inclusion` is mkdocs >= 1.6 (`InclusionLevel`); the getattr keeps this module
    importable, and the build honest-by-default, on anything older.
    """
    out: set[str] = set()
    for f in files:
        inclusion = getattr(f, "inclusion", None)
        if inclusion is not None and not inclusion.is_included():
            continue
        out.add(f.src_uri)
    return out


def add_provenance_banner(markdown: str, page_site_uri: str) -> str:
    """Insert the agent-generated banner just below an openwiki page's first heading."""
    index_link = posixpath.relpath(PROVENANCE_PAGE, posixpath.dirname(page_site_uri) or ".")
    banner = _BANNER.format(index=index_link)

    lines = markdown.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("# "):
            # Below the H1 so the page still opens with its own title.
            return "\n".join([*lines[: i + 1], "", banner, *lines[i + 1 :]])
    return banner + "\n" + markdown


# `config` is unused but the name is load-bearing: mkdocs dispatches plugin events
# by keyword (`method(item, **kwargs)`), so renaming or dropping it breaks the call.
def on_page_markdown(markdown: str, page: Any, config: Any, files: Any) -> str:  # noqa: ARG001
    known = published_uris(files)
    page_repo_uri = Path(page.file.abs_src_path).resolve().relative_to(REPO_ROOT).as_posix()
    out = rewrite_links(markdown, page_repo_uri, page.file.src_uri, known)
    if page_repo_uri.startswith(WIKI_DIR + "/"):
        out = add_provenance_banner(out, page.file.src_uri)
    return out


# ── edit pencil ──────────────────────────────────────────────────────────────────────


def wiki_edit_url(src_uri: str) -> str | None:
    """GitHub edit URL for a mounted `openwiki/` page, or None for anything else.

    Separated from the hook so `backend/tests/test_docs_site.py` can assert the
    URL without standing up a mkdocs build.
    """
    if src_uri != WIKI_DIR and not src_uri.startswith(WIKI_DIR + "/"):
        return None
    return f"{_EDIT_BASE}/{src_uri}"


# `config` and `nav` are unused but the names are load-bearing: mkdocs dispatches
# plugin events by keyword (`method(item, **kwargs)`).
def on_page_context(context: Any, page: Any, config: Any, nav: Any) -> Any:  # noqa: ARG001
    """Repoint the edit pencil for pages whose source is not under `docs_dir`."""
    fixed = wiki_edit_url(page.file.src_uri)
    if fixed is not None:
        page.edit_url = fixed
    return context
