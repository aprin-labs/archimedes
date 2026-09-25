"""Live surfaces must name the canonical repository (#1434).

`ui/src/components/WalletConnect.jsx` pointed at `a-apin/archimedes-arcadia`
for months. GitHub redirects renamed repositories, so the link *worked* — which
is exactly why nobody noticed. `docs-gate.yml`'s link checker could not catch it
either: it verifies that links resolve, and a redirect resolves.

So the check has to be on the name, not on the response. Three stale spellings
exist now. Two came from the *repository* rename (`hackagora/archimedes-arcadia`
and `a-apin/archimedes-arcadia`). The third came from the **organisation**
rename of **2026-09-01**, `a-apin` → `aprin-labs`, which makes
`a-apin/archimedes` one more redirecting spelling rather than the canonical one.
All three redirect to `aprin-labs/archimedes` today, and a redirect is a
courtesy, not a guarantee — it breaks the moment any of the old names is
re-claimed.

Two scans live here, and they police different things.

The first polices the `archimedes-arcadia` substring, which covers both
*repository* spellings, across `LIVE_ROOTS`.

The second polices the *organisation* rename (#1758). Adding a bare `a-apin` to
`PRE_RENAME_NAMES` was never available: `a-apin.github.io` is quoted on purpose
by the docs-site guards (`test_docs_site.py`, `ui/test/docs-link.test.js`'s
`FORBIDDEN_HOSTS`, `docs-site/infra/main.tf`) to record the Pages host #1634
moved off, and the OIDC and deploy notes quote "a-apin -> aprin-labs" to explain
what the rename broke. So the second scan forbids the **repository pointer** and
leaves the *host* and the *rename narrative* alone.

#1756 widened *what counts as a pointer*. The first cut matched the literal
substring `github.com/a-apin/`, which is one of three shapes the same dead
coordinate takes:

* the browser URL — `https://github.com/a-apin/archimedes/issues/1756`
* the **SSH remote** — `git@github.com:a-apin/archimedes.git`, which a
  `git clone` line in a runbook carries and which the substring missed entirely
  because the separator is a colon
* the **bare coordinate** — `gh issue list --repo a-apin/archimedes`,
  `repo:a-apin/archimedes:*` in an OIDC trust policy, `a-apin/<any repo>` in
  prose. No `github.com` in front of it at all.

All three break the same way the redirect does, so the rule is now one regex
(`_PRE_RENAME_COORDINATE`) over `<old org>/<repo>` with a word boundary in
front. The boundary is what preserves the allow-list: in `a-apin.github.io` the
org name is followed by a dot, not a slash, so the *host* — including
`https://a-apin.github.io/archimedes/` — never matches, and "a-apin ->
aprin-labs" has no slash at all. `FORBIDDEN_POINTER_SHAPES` and
`ALLOWED_MENTION_SHAPES` run those claims through the real matcher, so the
prose above cannot drift away from the rule.

#1756 also put `openwiki/` — the published wiki tree — under both scans. It was
outside every root, and was carrying four live `github.com/a-apin/archimedes`
links at the time (`INSTRUCTIONS.md`, `rigor/documented-conflicts.md`).

`CANONICAL_REPO` and `CANONICAL_ORG` are **documentation**. They are the names
the failure messages point offenders at; no live surface is checked *against*
them. That is precisely how #1758 was found — reverting `CANONICAL_REPO` to the
pre-rename spelling left this file green, because a constant is a comment with a
colon in it. **The scans are the enforcement.** `test_the_canonical_names_agree`
keeps the two constants from contradicting each other, so the failure messages
cannot start pointing at a dead redirect — but it proves nothing about any file
in the repository. Only the scans do that.

**Historical documents are deliberately exempt.** `docs/archive/`,
`docs/handovers/` and `docs/audits/` record what was true at a point in time,
and CLAUDE.md is explicit that superseded history is not rewritten. A handover
saying "origin currently redirects to hackagora/archimedes-arcadia" was accurate
when written and should stay that way. The org scan extends the same exemption
to a **date in the filename** (`docs/plans/2026-08-30-*.md`,
`docs/decisions/tooling-adoptions-2026-08.md`), and caps how far that can go.
`submodules/` are separate repositories.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

CANONICAL_REPO = "aprin-labs/archimedes"

# Assembled at runtime so this file does not match its own scan.
PRE_RENAME_NAMES = ("archimedes-" + "arcadia",)

#: Trees whose contents describe the project as it is now. ``openwiki`` is the
#: published wiki tree and was outside every root until #1756.
LIVE_ROOTS = (
    "ui/src",
    "ui/public",
    "infra",
    "backend",
    "contracts",
    "docs/runbooks",
    "scripts",
    "openwiki",
)

#: Point-in-time records, and other people's repositories.
EXEMPT_PARTS = ("archive", "handovers", "audits", "submodules", "node_modules", "dist", ".git")

SCANNED_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".py", ".md", ".json", ".sh", ".tf", ".yml", ".yaml", ".sol"}


#: This file names the stale spellings in prose so its failure message can quote
#: them, so it must not police itself. Excluded by resolved path rather than by
#: filename, so renaming it cannot silently reintroduce the self-match.
_SELF = Path(__file__).resolve()


def _is_exempt(path: Path) -> bool:
    return any(part in EXEMPT_PARTS for part in path.parts)


def _live_files() -> list[Path]:
    seen: list[Path] = []
    for root in LIVE_ROOTS:
        base = REPO_ROOT / root
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if (
                path.is_file()
                and path.suffix in SCANNED_SUFFIXES
                and not _is_exempt(path.relative_to(REPO_ROOT))
                and path.resolve() != _SELF
            ):
                seen.append(path)
    # Root-level markdown (README.md, CLAUDE.md, SETUP.md …) describes the
    # project as it is now and is the first thing a reader opens.
    seen.extend(p for p in REPO_ROOT.glob("*.md") if p.is_file())
    return seen


def test_the_scan_reaches_the_files_it_is_policing() -> None:
    """Guard on the guard: a scan that matched nothing would pass vacuously."""
    scanned = {str(p.relative_to(REPO_ROOT)) for p in _live_files()}
    assert len(scanned) > 200, f"only {len(scanned)} files scanned — the walk is broken"
    for expected in (
        "ui/src/components/WalletConnect.jsx",
        "infra/README.md",
        "docs/runbooks/github-security-toggles.md",
        "CLAUDE.md",
        # The published wiki tree, brought under the scan by #1756.
        "openwiki/INSTRUCTIONS.md",
    ):
        assert expected in scanned, f"{expected} not scanned — the guard is blind"


def test_the_scan_does_not_reach_historical_records() -> None:
    """The exemption must actually exempt, or this test would fail the repo's own history."""
    scanned = {str(p.relative_to(REPO_ROOT)) for p in _live_files()}
    assert not [p for p in scanned if p.startswith(("docs/archive/", "docs/handovers/", "docs/audits/"))]


@pytest.mark.parametrize("stale_name", PRE_RENAME_NAMES)
def test_no_live_surface_names_the_pre_rename_repository(stale_name: str) -> None:
    offenders: list[str] = []
    for path in _live_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if stale_name in text:
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, (
        f"{sorted(offenders)} still name '{stale_name}'. The canonical repository is "
        f"'{CANONICAL_REPO}'. GitHub's redirect makes the stale link work, so nothing "
        "else catches this — that redirect breaks if the old name is ever re-claimed."
    )


# ---------------------------------------------------------------------------
# The organisation rename, 2026-09-01 (#1758)
# ---------------------------------------------------------------------------

CANONICAL_ORG = "aprin-labs"

#: Assembled at runtime for the same reason as ``PRE_RENAME_NAMES``: this file
#: quotes the stale spellings in prose so its failure messages can, and a guard
#: that flags its own docstring is a guard nobody keeps.
PRE_RENAME_ORG = "a-" + "apin"

#: What is forbidden is the **repository coordinate**, not the organisation
#: name. ``<old org>.github.io`` is a *host* — the GitHub Pages origin #1634
#: moved off — and "old org -> new org" is a *rename narrative*; both are quoted
#: on purpose and stay green. ``<old org>/<repo>`` is a pointer that only
#: resolves while GitHub keeps redirecting, which is the thing that breaks.
#:
#: The slash is the whole distinction, and it is why one regex covers all three
#: shapes #1756 asked for: the HTTPS URL (``github.com/<old org>/…``), the SSH
#: remote (``github.com:<old org>/…`` — a *colon*, which the old substring check
#: could not see), and the bare coordinate with nothing in front of it at all
#: (``gh --repo <old org>/<repo>``, ``repo:<old org>/<repo>:*``). The lookbehind
#: keeps the match to a whole token, so a longer name ending in the old org's
#: spelling is not swept in; the required ``/`` right after the org name is what
#: lets ``<old org>.github.io`` — dot, not slash — through untouched.
_PRE_RENAME_COORDINATE = re.compile(rf"(?<![0-9A-Za-z._-]){re.escape(PRE_RENAME_ORG)}/[0-9A-Za-z._-]+")

#: The shapes that must be caught, and the mentions that must survive. These are
#: *inputs to the real matcher* (``test_the_matcher_catches_every_forbidden_shape``
#: and ``test_the_matcher_lets_every_deliberate_mention_through``), not prose:
#: #1758's whole lesson is that a constant no assertion reads is a comment with a
#: colon in it. Assembled from ``PRE_RENAME_ORG`` so this file does not match its
#: own scan by hand-spelling the stale org.
FORBIDDEN_POINTER_SHAPES = (
    f"see https://github.com/{PRE_RENAME_ORG}/archimedes/issues/1756 for context",
    f"git clone git@github.com:{PRE_RENAME_ORG}/archimedes.git",
    f"gh issue list --repo {PRE_RENAME_ORG}/archimedes",
    f"the wiki lives at {PRE_RENAME_ORG}/openwiki these days",
    f'"token.actions.githubusercontent.com:sub": "repo:{PRE_RENAME_ORG}/archimedes:*"',
)

#: Deliberate, correct mentions. The Pages host #1634 moved off, that host with a
#: path on it, the rename narrative in both spellings the repo uses, and the
#: canonical coordinate itself.
ALLOWED_MENTION_SHAPES = (
    f"FORBIDDEN_HOSTS = [{PRE_RENAME_ORG}.github.io]",
    f"docs was never a CNAME to https://{PRE_RENAME_ORG}.github.io/archimedes/",
    f"after the org rename {PRE_RENAME_ORG} -> {CANONICAL_ORG}, every deploy broke",
    f"On 2026-09-01 the `{PRE_RENAME_ORG}` → `{CANONICAL_ORG}` rename landed",
    f"https://github.com/{CANONICAL_REPO}/issues/1756",
)

#: Files handed to a reader, or to a package manager, as current truth.
ORG_LIVE_FILES = ("README.md", "SETUP.md", "CLAUDE.md", "AGENTS.md")

#: Trees that ship: the app, the two published packages, the docs tree, and the
#: published wiki (``openwiki``, added by #1756 — it was outside every root).
ORG_LIVE_ROOTS = (
    "ui/src",
    "ui/public",
    "backend/archimedes",
    "cli",
    "mcp-server",
    "docs",
    "openwiki",
)

ORG_EXEMPT_PARTS = ("archive", "handovers", "audits", "submodules", "node_modules", "dist", "build", ".git")

#: Wider than ``SCANNED_SUFFIXES``: ``cli/pyproject.toml`` and
#: ``mcp-server/pyproject.toml`` carry the ``Repository =`` URL that PyPI shows,
#: and ``ui/public`` ships ``llms.txt`` / ``sitemap.xml`` / ``site.webmanifest``.
ORG_SCANNED_SUFFIXES = SCANNED_SUFFIXES | {".toml", ".txt", ".html", ".css", ".xml", ".webmanifest"}

#: A date in the filename marks a point-in-time record — a plan written on a
#: day, a decision log for a month. Same principle as ``docs/archive/``: it was
#: accurate when written, and CLAUDE.md is explicit that history is not
#: rewritten. ``test_the_org_scan_exempts_only_point_in_time_records`` keeps
#: this from becoming the loophole.
_DATED_RECORD = re.compile(r"(?<!\d)\d{4}-\d{2}(?:-\d{2})?(?!\d)")

#: Deliberate mentions of the pre-rename org that must stay green. These are the
#: reason ``PRE_RENAME_NAMES`` could not simply grow a bare org entry (#1758).
DELIBERATE_ORG_MENTIONS = (
    # The Pages host #1634 moved off, quoted by the two docs-site guards.
    "backend/tests/test_docs_site.py",
    "ui/test/docs-link.test.js",
    "docs-site/infra/main.tf",
    # Incident narrative: the rename itself, and what it broke.
    "docs/runbooks/docs-site-setup.md",
    "infra/scripts/setup-github-oidc.sh",
    ".github/workflows/deploy.yml",
)


def _is_point_in_time(relative: Path) -> bool:
    return any(part in ORG_EXEMPT_PARTS for part in relative.parts) or bool(_DATED_RECORD.search(relative.name))


def _org_walk(*, apply_exemptions: bool) -> list[Path]:
    found: list[Path] = [REPO_ROOT / name for name in ORG_LIVE_FILES if (REPO_ROOT / name).is_file()]
    for root in ORG_LIVE_ROOTS:
        base = REPO_ROOT / root
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in ORG_SCANNED_SUFFIXES:
                continue
            relative = path.relative_to(REPO_ROOT)
            if apply_exemptions and _is_point_in_time(relative):
                continue
            found.append(path)
    return found


def _org_live_files() -> list[Path]:
    return _org_walk(apply_exemptions=True)


def _points_at_pre_rename_repo(line: str) -> bool:
    """The rule, in one place, so every test below is proving the same thing."""
    return bool(_PRE_RENAME_COORDINATE.search(line))


def _repo_pointer_offenders(paths: list[Path]) -> list[str]:
    """Report ``path:line`` for every line pointing at the pre-rename org's repo."""
    offenders: list[str] = []
    for path in paths:
        if path.resolve() == _SELF:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if _points_at_pre_rename_repo(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
    return offenders


def test_the_org_scan_reaches_the_live_surfaces() -> None:
    """Guard on the guard: a walk that reached nothing would pass vacuously."""
    scanned = {str(p.relative_to(REPO_ROOT)) for p in _org_live_files()}
    assert len(scanned) > 400, f"only {len(scanned)} files scanned — the walk is broken"
    for expected in (
        "README.md",
        "SETUP.md",
        "CLAUDE.md",
        "AGENTS.md",
        # The published packages' metadata: this is what PyPI renders.
        "cli/pyproject.toml",
        "cli/README.md",
        "mcp-server/pyproject.toml",
        "mcp-server/README.md",
        # The surface that actually rotted in #1434.
        "ui/src/components/WalletConnect.jsx",
        "backend/archimedes/main.py",
        # A live doc that quotes the rename narrative and must stay green.
        "docs/runbooks/docs-site-setup.md",
        # The published wiki: outside every root until #1756, and it was
        # carrying four live pre-rename links when that was noticed.
        "openwiki/INSTRUCTIONS.md",
        "openwiki/rigor/documented-conflicts.md",
    ):
        assert expected in scanned, f"{expected} not scanned — the guard is blind"


def test_the_org_scan_exempts_only_point_in_time_records() -> None:
    """The date exemption must stay a footnote, not a way to park a stale link."""
    scanned = {p.relative_to(REPO_ROOT) for p in _org_live_files()}
    everything = {p.relative_to(REPO_ROOT) for p in _org_walk(apply_exemptions=False)}
    skipped = everything - scanned
    assert skipped, "nothing was exempted — the point-in-time filter is not running"
    for relative in skipped:
        assert _is_point_in_time(relative), f"{relative} was dropped for no stated reason"

    dated_only = {r for r in skipped if not any(part in ORG_EXEMPT_PARTS for part in r.parts)}
    assert len(dated_only) * 10 < len(scanned), (
        f"{len(dated_only)} files are exempt on a dated filename alone against {len(scanned)} scanned. "
        "The exemption is meant for a handful of plans and decision logs; at this ratio it is the loophole."
    )


def test_no_live_surface_points_at_the_pre_rename_org() -> None:
    """#1758: the org rename must be enforced, not merely swept.

    ``CANONICAL_REPO``/``CANONICAL_ORG`` are documentation — no assertion reads
    them, which is exactly why reverting ``CANONICAL_REPO`` left this file green
    and got #1758 filed. *This scan* is the enforcement.
    """
    offenders = _repo_pointer_offenders(_org_live_files())
    assert not offenders, (
        f"{sorted(offenders)} name a repository under '{PRE_RENAME_ORG}/' — as an https:// link, an "
        f"SSH remote (github.com:{PRE_RENAME_ORG}/…), or a bare coordinate. The canonical repository "
        f"is '{CANONICAL_REPO}' under the '{CANONICAL_ORG}' organisation. GitHub's redirect makes the "
        "stale pointer work today, so nothing else catches this — and the redirect dies the moment "
        f"'{PRE_RENAME_ORG}' is re-claimed by anyone. (The *host* '{PRE_RENAME_ORG}.github.io' and the "
        f"'{PRE_RENAME_ORG} -> {CANONICAL_ORG}' rename narrative are deliberate and do not trip this.)"
    )


@pytest.mark.parametrize("shape", FORBIDDEN_POINTER_SHAPES)
def test_the_matcher_catches_every_forbidden_shape(shape: str) -> None:
    """#1756: the same dead coordinate wears three different clothes.

    The first cut of this guard matched the literal substring
    ``github.com/<old org>/``. An SSH remote uses a colon, and a
    ``gh --repo``/OIDC-``sub`` coordinate has no host in front of it at all —
    both walked straight past. This runs each shape through the *live* matcher,
    so widening the rule is a claim the suite checks rather than one the
    docstring makes.
    """
    assert _points_at_pre_rename_repo(shape), f"{shape!r} is a live pointer at the pre-rename org and was not caught"


@pytest.mark.parametrize("shape", ALLOWED_MENTION_SHAPES)
def test_the_matcher_lets_every_deliberate_mention_through(shape: str) -> None:
    """The widened rule must not swallow the allow-list it was built around.

    ``<old org>.github.io`` is the Pages host #1634 moved off and is quoted on
    purpose; "old org -> new org" is the rename narrative the OIDC and deploy
    notes need. A rule that flagged either would be reverted within a day, which
    is the failure mode a bare-substring widening walks into.
    """
    assert not _points_at_pre_rename_repo(shape), f"{shape!r} is a deliberate mention and must stay green"


@pytest.mark.parametrize("keep", DELIBERATE_ORG_MENTIONS)
def test_deliberate_pre_rename_org_mentions_stay_green(keep: str) -> None:
    """The host and the rename narrative are on purpose — the rule must let them through.

    This is why #1758 was not a one-line addition to ``PRE_RENAME_NAMES``. Each
    file here is checked with the *same* offender function the live scan uses,
    so the pass is earned by the host-vs-repo distinction rather than by the
    file happening to sit outside the scanned roots.
    """
    path = REPO_ROOT / keep
    assert path.is_file(), f"{keep} is gone — update DELIBERATE_ORG_MENTIONS or restore the file"
    assert PRE_RENAME_ORG in path.read_text(encoding="utf-8"), (
        f"{keep} no longer mentions '{PRE_RENAME_ORG}'. If the mention was deliberately removed, "
        "drop it from DELIBERATE_ORG_MENTIONS; otherwise this guard is no longer proving anything."
    )
    assert not _repo_pointer_offenders([path]), f"{keep} is a repo pointer, not a host or a narrative"


def test_the_canonical_names_agree() -> None:
    """The constants are documentation, but they may not be *wrong* documentation.

    #1758's opening symptom: reverting `CANONICAL_REPO` to the pre-rename
    spelling left this file green, because the failure messages were the only
    reader. A guard that points offenders at a dead redirect is worse than one
    that points at nothing.
    """
    assert CANONICAL_REPO.startswith(f"{CANONICAL_ORG}/"), (
        f"CANONICAL_REPO ({CANONICAL_REPO!r}) is not under CANONICAL_ORG ({CANONICAL_ORG!r})"
    )
    assert PRE_RENAME_ORG not in CANONICAL_REPO, f"CANONICAL_REPO ({CANONICAL_REPO!r}) still names the pre-rename org"
    for stale in PRE_RENAME_NAMES:
        assert stale not in CANONICAL_REPO, f"CANONICAL_REPO ({CANONICAL_REPO!r}) names a pre-rename repository"
