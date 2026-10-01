"""The ungated ``/app`` locations in nginx must be exactly ``ANON_APP_PAGES``.

Owner's call on #1753 (narrowing #1194 revision d): a signed-out
visitor may browse **Explore and Corpus only**. Library, the leaderboard and
the strategy passport are gated.

That gate has two halves that have to agree, and until now the agreement was
asserted only by a comment in each file ("Must stay in lockstep with…"). A
comment cannot fail. Both failure directions are real and neither is loud:

* **nginx open, SPA closed** — nginx serves the shell to an anonymous request,
  and the client then bounces to ``/sign-in``. Cosmetically survivable, but it
  is a *pre-auth* answer that differs from every other gated ``/app`` path, i.e.
  the same existence-oracle shape #1437 removed for ``/app/insights``.
* **nginx closed, SPA open** — the fail-closed direction for content, but the
  page is simply unreachable on a cold load while the SPA still advertises it.

So this test derives one side from the other instead of holding a third copy of
the list: it parses ``ANON_APP_PAGES`` and ``APP_PATHS`` out of
``ui/src/routes.js`` and requires the set of ungated ``/app`` ``location``
blocks in ``nginx/nginx.conf`` to equal what those two imply. Adding a page to
one file and not the other is a failure, in both directions, naming the side
that is missing it.

MUTATION (the one this change is about): re-add

    location ^~ /app/leaderboard { ... }

to ``nginx/nginx.conf`` → ``test_the_ungated_app_locations_are_exactly_the_anonymous_pages``
goes red naming ``/app/leaderboard`` as ungated at the edge but not anonymous in
the SPA. Deleting ``'corpus'`` from ``ANON_APP_PAGES`` fails the same test from
the other side.

Parsing, not importing: the same text-inspection idiom as this suite's other
config guards (``test_local_setup_contract.py``, ``test_breadcrumbs.py``). It
needs no bundler, and it needs ``routes.js`` to export nothing it does not
already export.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NGINX_CONF = ROOT / "nginx" / "nginx.conf"
ROUTES_JS = ROOT / "ui" / "src" / "routes.js"

# Page ids that have no static path in APP_PATHS (deep routes such as
# `strategy`, which lives at /app/strategy/<id>). If one of these is ever
# anonymous again it needs its own nginx prefix location, so the derivation
# below must not silently ignore it — it maps to an explicit /app/<id> prefix.
_DEEP_ROUTE_PREFIXES = {"strategy": "/app/strategy"}


def _app_paths() -> dict[str, str]:
    """``APP_PATHS`` from routes.js as ``{path: page}``."""
    block = re.search(r"const\s+APP_PATHS\s*=\s*\{(.*?)\n\}", ROUTES_JS.read_text(encoding="utf-8"), re.DOTALL)
    assert block, "could not find `const APP_PATHS = { ... }` in ui/src/routes.js"
    pairs = re.findall(r"['\"]([^'\"]+)['\"]\s*:\s*['\"]([^'\"]+)['\"]", block.group(1))
    assert pairs, "APP_PATHS parsed to zero entries — the parser has drifted from routes.js"
    return dict(pairs)


def _anon_app_pages() -> set[str]:
    """``ANON_APP_PAGES`` from routes.js."""
    block = re.search(
        r"const\s+ANON_APP_PAGES\s*=\s*new Set\(\[([^\]]*)\]\)",
        ROUTES_JS.read_text(encoding="utf-8"),
    )
    assert block, "could not find `const ANON_APP_PAGES = new Set([...])` in ui/src/routes.js"
    return set(re.findall(r"['\"]([^'\"]+)['\"]", block.group(1)))


def _location_blocks() -> dict[str, str]:
    """Map ``location <selector>`` → block body, brace-balanced."""
    conf = NGINX_CONF.read_text(encoding="utf-8")
    blocks: dict[str, str] = {}
    for m in re.finditer(r"^\s*location\s+([^{]+?)\s*\{", conf, re.M):
        depth, i = 0, m.end() - 1
        while i < len(conf):
            if conf[i] == "{":
                depth += 1
            elif conf[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        blocks[m.group(1).strip()] = conf[m.end() : i]
    return blocks


def _ungated_app_paths() -> set[str]:
    """Paths under ``/app`` that nginx serves with no ``auth_request``.

    Selector syntax is stripped to the bare path so both halves of the
    comparison speak in URLs: ``= /app`` → ``/app``, ``^~ /app/explore`` →
    ``/app/explore``. Named locations (``@sign_in``) and regex locations
    (``~``) are not prefix carve-outs and are excluded — a regex location under
    /app would be a different shape of change and should not slip through this
    guard silently, so it is asserted against below.
    """
    ungated: set[str] = set()
    for selector, body in _location_blocks().items():
        if selector.startswith("@"):
            continue
        assert not selector.startswith(("~", "~*")), (
            f"regex location {selector!r} — this guard only understands exact (`=`) and "
            "prefix (`^~`, bare) locations; a regex location under /app needs review, not a parser tweak"
        )
        path = re.sub(r"^(=|\^~)\s*", "", selector).strip()
        if not (path == "/app" or path.startswith("/app/")):
            continue
        if "auth_request /_auth_session" in body:
            continue
        ungated.add(path)
    return ungated


def _expected_ungated_app_paths() -> set[str]:
    """What ``ANON_APP_PAGES`` implies nginx must leave ungated."""
    anon = _anon_app_pages()
    app_paths = _app_paths()
    expected: set[str] = set()
    for path, page in app_paths.items():
        if page in anon:
            expected.add(path)
    for page in anon:
        if page not in app_paths.values():
            assert page in _DEEP_ROUTE_PREFIXES, (
                f"ANON_APP_PAGES names {page!r}, which has neither an APP_PATHS entry nor a known "
                "deep-route prefix — this guard cannot tell which nginx location should be open for it"
            )
            expected.add(_DEEP_ROUTE_PREFIXES[page])
    return expected


def test_the_ungated_app_locations_are_exactly_the_anonymous_pages() -> None:
    """The lockstep the two files' comments promise, enforced."""
    actual = _ungated_app_paths()
    expected = _expected_ungated_app_paths()

    open_at_edge_only = sorted(actual - expected)
    assert not open_at_edge_only, (
        "nginx/nginx.conf serves these /app paths with no auth_request, but ui/src/routes.js's "
        f"ANON_APP_PAGES does not mark them anonymous: {open_at_edge_only}. Signed-out visitors "
        "browse Explore and Corpus only (#1753) — either the carve-out is stale and should be "
        "deleted, or the owner re-opened the page and ANON_APP_PAGES has to say so too."
    )
    anon_in_spa_only = sorted(expected - actual)
    assert not anon_in_spa_only, (
        "ui/src/routes.js marks these pages anonymous-OK but nginx gates them, so a cold load "
        f"302s to /sign-in before the SPA ever runs: {anon_in_spa_only}. Add the matching "
        "ungated `location` to nginx/nginx.conf, or drop the page from ANON_APP_PAGES."
    )


def test_the_anonymous_set_is_explore_and_corpus() -> None:
    """Anti-vacuity + the owner's decision, pinned on the SPA side.

    The lockstep test above is symmetric: it stays green if BOTH files open a
    page, which is exactly how a gating decision gets quietly reversed. This
    row is the decision itself (#1753) and only the owner may
    change it.
    """
    assert _anon_app_pages() == {"explore", "corpus"}, _anon_app_pages()


@pytest.mark.parametrize("path", ["/app/leaderboard", "/app/library", "/app/strategy", "/app/generate"])
def test_the_gated_pages_have_no_ungated_location(path: str) -> None:
    """Named explicitly, so the failure reads as a product regression.

    ``/app/leaderboard`` and ``/app/strategy`` are here because they WERE
    carve-outs (#1194 rev d) and stop being ones with #1753 — the paths a
    revert or a stale rebase would most plausibly bring back.
    """
    assert path not in _ungated_app_paths(), (
        f"{path} is served without auth_request — it is a gated page (#1753) and an anonymous "
        "request for it must get the same @sign_in 302 as any other gated /app path"
    )


def test_every_ungated_app_location_still_serves_the_spa_shell() -> None:
    """A carve-out that does not fall back to index.html is a 404, not a page."""
    for selector, body in _location_blocks().items():
        path = re.sub(r"^(=|\^~)\s*", "", selector).strip()
        if not (path == "/app" or path.startswith("/app/")):
            continue
        if "auth_request /_auth_session" in body:
            continue
        assert "try_files $uri $uri/ /index.html;" in body, (
            f"the ungated {selector!r} location has no SPA fallback — an anonymous visitor gets a 404"
        )
