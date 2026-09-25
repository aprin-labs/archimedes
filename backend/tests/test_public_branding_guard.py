"""Retired branding must not return to public surfaces (Dan, 2026-09-01).

Why this exists
---------------
The 2026-08-31 README refresh faithfully re-introduced "Linus for quantitative
finance" because CLAUDE.md still carried it as the product identity — stale
branding upstream propagated downstream through an agent doing exactly what it
was told. The owner's call: product-analogy branding ("Linus for X", "Elicit
for Y") is retired and **banned on every public surface**; competitive comps
live in the private docs repo only. A grep guard is the cheapest thing that
makes the ban self-enforcing instead of one more fact a future refresh can
silently lose.

Scope
-----
Public identity surfaces: the root markdown files, docs/ (excluding archive/,
handovers/, and runbooks/ — those record history and may quote the old brand),
openwiki/, the UI source and public assets, the distributed CLI/MCP packages, and
the company one-pager.

Two classes of surface are easy to miss and are pinned by
``test_scan_covers_the_syndicated_surfaces`` below, because branding leaves the
repo through them without ever appearing in ui/src:

* ``ui/index.html`` and ``ui/public/site.webmanifest`` — the ``<title>``,
  ``description``, ``og:*`` and ``twitter:*`` copy is what search results, link
  unfurls and installed-PWA chrome actually show.
* ``cli/`` and ``mcp-server/`` in full — their ``README.md`` and pyproject
  ``description`` are the package pages on PyPI, not just repo files.

The banned list is small on purpose: patterns land here when the owner retires
them, not speculatively.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# Each entry: (compiled pattern, why it is banned).
BANNED = [
    (
        re.compile(r"Linus\s+for", re.IGNORECASE),
        'product-analogy branding retired 2026-09-01 ("Linus for quantitative finance")',
    ),
    (
        re.compile(r"Elicit\s+for", re.IGNORECASE),
        "product-analogy branding is banned on public surfaces (comps live in the private docs repo)",
    ),
    (
        re.compile(r"world is your portfolio", re.IGNORECASE),
        'retired tagline clause 2026-09-01 (read as a trading-app promise; replaced by "portfolio strategy, under scrutiny")',
    ),
]

# Whole package dirs, not just src/ — a package's README and pyproject description
# are its public page on PyPI.
PUBLIC_ROOTS = [
    "docs",
    "openwiki",
    "ui/src",
    "ui/public",
    "cli",
    "mcp-server",
    "company-site",
]
# ui/ itself is not a root (package-lock.json is megabytes of noise), so the two
# syndicated files in it are named directly.
PUBLIC_FILES = [
    "README.md",
    "CLAUDE.md",
    "AGENTS.md",
    "SETUP.md",
    "ui/index.html",
    "ui/README.md",
]

# History is allowed to quote the old brand; identity surfaces are not.
EXCLUDED_PARTS = {
    "archive",
    "handovers",
    "runbooks",
    "node_modules",
    "__pycache__",
    "dist",
    "build",
    ".venv",
}

TEXT_SUFFIXES = {
    ".md",
    ".txt",
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".json",
    ".html",
    ".yml",
    ".yaml",
    ".toml",
    ".webmanifest",
}


def _public_files():
    for name in PUBLIC_FILES:
        p = REPO / name
        if p.is_file():
            yield p
    for root in PUBLIC_ROOTS:
        base = REPO / root
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if not p.is_file() or p.suffix not in TEXT_SUFFIXES:
                continue
            parts = p.relative_to(REPO).parts
            if EXCLUDED_PARTS.intersection(parts):
                continue
            if any(part.endswith(".egg-info") for part in parts):
                continue
            # This guard quotes the banned phrases in its own docstring.
            if p.name == "test_public_branding_guard.py":
                continue
            yield p


def test_no_retired_branding_on_public_surfaces():
    hits: list[str] = []
    for path in _public_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for pattern, why in BANNED:
                if pattern.search(line):
                    rel = path.relative_to(REPO)
                    hits.append(f"{rel}:{lineno}: {line.strip()[:100]}  [{why}]")
    assert not hits, (
        "Retired branding found on public surfaces — the owner banned these "
        "framings (see CLAUDE.md § Project). Remove or rewrite:\n" + "\n".join(hits)
    )


def test_the_guard_actually_rejects_the_banned_phrase(tmp_path):
    """The pattern list must fire on the exact line that shipped in the incident."""
    incident_line = "**Linus for quantitative finance.** Archimedes is a single-user agent"
    assert any(p.search(incident_line) for p, _ in BANNED)
    tagline = "The world is your portfolio."
    assert any(p.search(tagline) for p, _ in BANNED)


def test_scan_covers_the_syndicated_surfaces():
    """Roots must keep covering the files branding actually escapes through.

    The scan started at ui/src + cli/src + mcp-server/src, which missed every
    surface below: the ``<title>``/``og:``/``twitter:`` copy that search results
    and link unfurls render, the PWA manifest, and the two package pages
    published to PyPI. Narrowing a root back down is a silent regression, so the
    coverage is asserted rather than assumed.
    """
    scanned = {str(p.relative_to(REPO)) for p in _public_files()}
    required = {
        "README.md",
        "CLAUDE.md",
        "ui/index.html",
        "ui/public/site.webmanifest",
        "ui/src/components/Architecture.jsx",
        "ui/src/paperCopy.js",
        "cli/README.md",
        "cli/pyproject.toml",
        "mcp-server/README.md",
        "mcp-server/pyproject.toml",
        "company-site/index.html",
    }
    missing = sorted(required - scanned)
    assert not missing, f"public surfaces dropped out of the branding scan: {missing}"
