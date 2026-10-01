"""``docs/api/*.md`` must stay in sync with the LIVE FastAPI route table.

House promise-checked-against-the-truth pattern (cf. ``test_asset_universe_doc.py``'s
SSOT-vs-doc contract): parse every ``### METHOD /path`` heading across ``docs/api/*.md``
into a DOCUMENTED set, walk ``archimedes.main``'s real route table into a LIVE set, and
assert both directions:

  (a) every documented (method, path) exists live — the docs can't promise a route the
      app doesn't actually serve (docs can't drift *ahead* of the app).
  (b) every live route is either documented, or listed in the frozen ``_UNDOCUMENTED``
      allowlist below with a one-line reason — a new route can't silently ship undocumented
      (the app can't drift *ahead* of the docs either).

``_SIDECAR_ONLY_DOCS`` is the mirror-image exemption for (a): ``auth-and-accounts.md``
documents the Better Auth Node sidecar's own HTTP contract (``auth/auth.js`` on port 3000,
proxied by nginx) — those headings are real API surface, just never mounted as
``archimedes.main`` FastAPI routes, so they can never appear in the live set no matter how
faithful the docs are. Frozen with the same one-line-reason-per-entry discipline as
``_UNDOCUMENTED`` so a *new* heading in that file still has to earn its way onto the list
instead of silently exempting itself.

Hermetic: TESTING=1 import of ``archimedes.main`` (conftest sets it before any archimedes
import) plus in-memory regex parsing of the committed docs. No DB / Redis / RPC / network.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import fastapi.routing as fastapi_routing
from fastapi.routing import APIRoute

# Pair = (HTTP method, path), e.g. ("GET", "/api/vaults/{address}/chat").
Pair = tuple[str, str]

_IGNORED_METHODS = {"HEAD", "OPTIONS"}

# ── docs → DOCUMENTED set ────────────────────────────────────────────────────────────

# A "### " heading line; captures everything after the marker so a single heading like
# "### GET /api/health (and GET /health)" can yield more than one pair below.
_HEADING_RE = re.compile(r"^### (.+)$", re.MULTILINE)
# One METHOD + path token inside a heading. Path stops at whitespace or a closing paren
# so "(and GET /health)" parses as a second, separate pair rather than swallowing the
# paren into the path.
_METHOD_PATH_RE = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/[^\s)]*)")


def _repo_root() -> Path:
    import archimedes  # backend/archimedes/__init__.py → parents: archimedes, backend, <repo>

    return Path(archimedes.__file__).resolve().parents[2]


def _docs_api_dir() -> Path:
    return _repo_root() / "docs" / "api"


def _documented_pairs() -> dict[Pair, list[str]]:
    """(method, path) -> sorted list of doc filenames whose heading(s) claim it."""
    docs_dir = _docs_api_dir()
    pairs: dict[Pair, list[str]] = {}
    for md in sorted(docs_dir.glob("*.md")):
        text = md.read_text(encoding="utf-8")
        for heading in _HEADING_RE.findall(text):
            for method, raw_path in _METHOD_PATH_RE.findall(heading):
                path = raw_path.rstrip(").,;:")
                pairs.setdefault((method, path), []).append(md.name)
    return pairs


# ── live app → LIVE set ──────────────────────────────────────────────────────────────


def _live_pairs() -> set[Pair]:
    """Flatten archimedes.main's real route table into (method, path) pairs.

    fastapi>=0.139 (see backend/requirements.txt) resolves ``app.include_router(...)``
    lazily: top-level ``app.routes`` holds a mix of real ``APIRoute`` objects (routes
    added directly via ``@app.get`` etc.) and opaque ``_IncludedRouter`` wrappers for
    every ``include_router`` call, which only expose their *effective* (fully-prefixed,
    dependency-merged) routes through ``effective_route_contexts()``. Walk both shapes so
    this reflects the exact route table FastAPI would actually dispatch against — no
    guessing at prefixes by re-deriving them from router source. Falls back to a flat
    ``isinstance(route, APIRoute)`` scan on older fastapi where ``include_router`` eagerly
    flattens sub-routes into real ``APIRoute`` objects and ``_IncludedRouter`` doesn't
    exist at all.
    """
    os.environ.setdefault("TESTING", "1")
    from archimedes.main import app

    included_router_cls = getattr(fastapi_routing, "_IncludedRouter", None)

    def walk(routes) -> list[tuple[set[str], str]]:
        found: list[tuple[set[str], str]] = []
        for route in routes:
            if isinstance(route, APIRoute):
                found.append((route.methods, route.path))
            elif included_router_cls is not None and isinstance(route, included_router_cls):
                for ctx in route.effective_route_contexts():
                    if isinstance(ctx.original_route, APIRoute):
                        found.append((ctx.methods, ctx.path_format))
        return found

    pairs: set[Pair] = set()
    for methods, path in walk(app.routes):
        for method in methods:
            if method not in _IGNORED_METHODS:
                pairs.add((method, path))
    return pairs


# ── frozen exemptions, one-line reason per entry ─────────────────────────────────────

# Direction (a) exemption: docs/api/auth-and-accounts.md documents the colocated Better
# Auth Node sidecar's own HTTP contract (auth/auth.js, port 3000 — nginx proxies
# /api/auth/ straight to it, never through FastAPI). Real, load-bearing API surface, but
# it will never appear in archimedes.main's route table under any circumstance.
_SIDECAR_ONLY_DOCS: dict[Pair, str] = {
    ("POST", "/api/auth/sign-up/email"): "Better Auth Node sidecar route, not a FastAPI route (auth-and-accounts.md)",
    ("POST", "/api/auth/sign-in/email"): "Better Auth Node sidecar route, not a FastAPI route (auth-and-accounts.md)",
    ("POST", "/api/auth/sign-out"): "Better Auth Node sidecar route, not a FastAPI route (auth-and-accounts.md)",
    ("GET", "/api/auth/get-session"): "Better Auth Node sidecar route, not a FastAPI route (auth-and-accounts.md)",
    ("GET", "/api/auth/verify-email"): "Better Auth Node sidecar route, not a FastAPI route (auth-and-accounts.md)",
    (
        "GET",
        "/api/auth/verification-status",
    ): "auth sidecar's own route (auth/server.js, #1748) — not a FastAPI route (auth-and-accounts.md)",
    ("GET", "/api/auth/providers"): "Better Auth Node sidecar route, not a FastAPI route (auth-and-accounts.md)",
    ("POST", "/api/auth/sign-in/social"): "Better Auth Node sidecar route, not a FastAPI route (auth-and-accounts.md)",
    (
        "GET",
        "/api/auth/callback/{provider}",
    ): "Better Auth Node sidecar route, not a FastAPI route (auth-and-accounts.md)",
}

# Direction (b) exemption: real archimedes.main FastAPI routes that are deliberately not
# (yet) covered by docs/api/*.md. Each reason is honest about *why* — either the route
# belongs to a capability area documented elsewhere (docs/agent-api.md's own drift guard
# is backend/tests/test_agent_discovery.py), it's an internal service-to-service route
# never reachable from the browser UI, or it's a real gap in docs/api/ coverage that
# hasn't been written yet. A route leaving this list because its doc landed is a welcome
# diff; a route silently disappearing from *and* the docs is exactly what this test
# guards against.
_UNDOCUMENTED: dict[Pair, str] = {
    # legacy SIWE router (auth_siwe.py) — TESTING-only, main.py mounts it only under
    # TESTING so signature-verification tests can exercise its proof helpers; 404s in
    # every real environment. Shares the /api/auth prefix with the Better Auth sidecar
    # by coincidence of URL space, not identity — auth-and-accounts.md explicitly scopes
    # itself to the sidecar and says this capability area "is not documented here".
    ("GET", "/api/auth/nonce"): "legacy SIWE router, TESTING-only, out of scope per auth-and-accounts.md",
    ("GET", "/api/auth/session"): "legacy SIWE router, TESTING-only, out of scope per auth-and-accounts.md",
    ("POST", "/api/auth/verify"): "legacy SIWE router, TESTING-only, out of scope per auth-and-accounts.md",
    ("POST", "/api/auth/logout"): "legacy SIWE router, TESTING-only, out of scope per auth-and-accounts.md",
    # internal-key gated (X-Internal-Agent-Key), agent-runner-only — never called from
    # the browser UI, fails closed if INTERNAL_AGENT_API_KEY is unset.
    (
        "POST",
        "/api/agent/bootstrap-liquidity",
    ): "internal-key gated (X-Internal-Agent-Key); agent-runner-only, never browser-facing",
    # agent_manifest_routes.py — fully documented at docs/agent-api.md, which is a
    # separate written surface with its own drift guard
    # (backend/tests/test_agent_discovery.py asserts every route string there resolves
    # against the running app), not docs/api/.
    # (POST /api/rigor/verify came OFF this list with #1803 — it now has its own
    # `### POST /api/rigor/verify` section in docs/api/strategies-and-rigor.md, which
    # is what documents the strict input contract and its rejection codes.)
    (
        "GET",
        "/api/agent/manifest",
    ): "documented at docs/agent-api.md, guarded by test_agent_discovery.py, not docs/api/",
    # agent_routes.py status/health introspection — internal system health, distinct
    # concept from agent_manifest_routes.py's external-agent-facing /manifest.
    ("GET", "/api/agent/status"): "agent-runner status introspection; not yet in docs/api/",
    ("GET", "/api/agent/circle-status"): "Circle integration status probe; not yet in docs/api/",
    (
        "GET",
        "/api/agent/health/amm",
    ): "AMM health probe (agent_routes.py twin of /api/health/amm); not yet in docs/api/",
    # corpus_routes.py — KB pipeline / knowledge-graph introspection surface.
    ("GET", "/api/corpus/runner-state"): "KB pipeline phase/manifest introspection; not yet in docs/api/",
    ("GET", "/api/corpus/overview"): "corpus aggregate stats; not yet in docs/api/",
    ("GET", "/api/corpus/graph"): "SPECTER2-similarity scatter data; not yet in docs/api/",
    ("GET", "/api/corpus/kg/entities"): "knowledge-graph entity search; not yet in docs/api/",
    ("GET", "/api/corpus/kg/entity/{entity_id}"): "knowledge-graph entity detail; not yet in docs/api/",
    ("GET", "/api/corpus/kg/paper/{arxiv_id}"): "knowledge-graph triples for one paper; not yet in docs/api/",
    # papers_routes.py — paper-corpus browser, distinct from corpus_routes.py's KG surface.
    ("GET", "/api/papers/"): "paper-corpus browser list; not yet in docs/api/",
    ("GET", "/api/papers/{arxiv_id}"): "single-paper browser detail; not yet in docs/api/",
    # explore_routes.py — read-only Explore-page asset discovery (page-roles-spec.md).
    ("GET", "/api/explore/assets"): "Explore-page asset list; not yet in docs/api/",
    ("GET", "/api/explore/assets/{symbol}/history"): "Explore-page asset history; not yet in docs/api/",
    # assets_routes.py — asset-universe list + price history.
    ("GET", "/api/assets/"): "asset-universe list; not yet in docs/api/",
    ("GET", "/api/assets/{symbol}/history"): "asset price history; not yet in docs/api/",
    # features_routes.py — public feature-flag introspection.
    ("GET", "/api/features"): "public feature-flag introspection; not yet in docs/api/",
    # regime_routes.py.
    ("GET", "/api/regime/current"): "current market-regime read; not yet in docs/api/",
    ("GET", "/api/regime/transitions"): "regime transition history; not yet in docs/api/",
    # risk_routes.py.
    ("GET", "/api/risk/profiles"): "risk-profile bands; not yet in docs/api/",
    ("GET", "/api/risk/portfolio"): "portfolio risk summary; not yet in docs/api/",
    ("GET", "/api/risk/cvar"): "portfolio CVaR (quant-feature gated); not yet in docs/api/",
    ("GET", "/api/risk/greeks"): "portfolio options-Greeks proxy (quant-feature gated); not yet in docs/api/",
    # portfolio_routes.py — thin HTTP layer over services/portfolio_optimizer.py.
    ("POST", "/api/portfolio/optimize"): "MVO/HRP/BL optimizer (quant-feature gated); not yet in docs/api/",
    ("POST", "/api/portfolio/parameter-sweep"): "optimizer parameter sweep (quant-feature gated); not yet in docs/api/",
    ("POST", "/api/portfolio/scenario-analysis"): "optimizer scenario analysis; not yet in docs/api/",
    # proposals_routes.py — account-session gated, owner-scoped strategy_proposals reads.
    ("GET", "/api/proposals/"): "owner-scoped generation-proposal history; not yet in docs/api/",
    ("GET", "/api/proposals/{generation_id}/siblings"): "sibling-candidate comparison; not yet in docs/api/",
    # account_usage_routes.py — the CLI's `meter` command backend.
    ("GET", "/api/account/usage"): "CLI `meter` command backend, generation-quota usage; not yet in docs/api/",
    # user_routes.py — legacy wallet-keyed profile, distinct from the canonical Better
    # Auth account identity documented in auth-and-accounts.md.
    ("GET", "/api/user/profile/{wallet}"): "legacy wallet-keyed profile read; not yet in docs/api/",
    ("POST", "/api/user/profile"): "legacy wallet-keyed profile write; not yet in docs/api/",
    # marketplace_routes.py — publish/subscribe CRUD (linked-wallet gated except the two
    # public browse GETs). README.md's index already promises a "Trading & marketplace"
    # section; this is the concrete gap in that promise, not a permanent exemption.
    ("POST", "/api/marketplace/publish"): "publish a strategy to the marketplace; not yet in docs/api/",
    ("POST", "/api/marketplace/subscribe"): "subscribe to a published strategy; not yet in docs/api/",
    (
        "DELETE",
        "/api/marketplace/subscribe/{strategy_id}",
    ): "unsubscribe from a published strategy; not yet in docs/api/",
    ("DELETE", "/api/marketplace/publish/{strategy_id}"): "stop publishing a strategy; not yet in docs/api/",
    ("GET", "/api/marketplace/published"): "public published-strategy list; not yet in docs/api/",
    ("GET", "/api/marketplace/published/{strategy_id}"): "public published-strategy detail; not yet in docs/api/",
    ("GET", "/api/marketplace/my-published"): "caller's own published strategies; not yet in docs/api/",
    ("GET", "/api/marketplace/my-subscriptions"): "caller's own subscriptions; not yet in docs/api/",
    ("POST", "/api/marketplace/publish/{strategy_id}/withdraw"): "withdraw publisher earnings; not yet in docs/api/",
}


def _fmt(pairs) -> str:
    return ", ".join(f"{m} {p}" for m, p in sorted(pairs))


# ── the two directions ───────────────────────────────────────────────────────────────


def test_every_documented_route_exists_live() -> None:
    """(a) docs/api/*.md can't promise a route archimedes.main doesn't actually serve."""
    documented = _documented_pairs()
    live = _live_pairs()

    stale = {pair: files for pair, files in documented.items() if pair not in live and pair not in _SIDECAR_ONLY_DOCS}
    assert not stale, (
        "docs/api/*.md documents routes that do NOT exist in the live archimedes.main "
        "route table (docs have drifted ahead of the app — fix the doc or the route):\n"
        + "\n".join(f"  {m} {p}  (in {', '.join(files)})" for (m, p), files in sorted(stale.items()))
    )


def test_every_live_route_is_documented_or_allowlisted() -> None:
    """(b) a live archimedes.main route can't ship silently undocumented."""
    documented = _documented_pairs()
    live = _live_pairs()

    undocumented = live - set(documented) - set(_UNDOCUMENTED)
    assert not undocumented, (
        "archimedes.main serves routes that are neither documented in docs/api/*.md nor "
        "listed in test_api_docs_drift._UNDOCUMENTED (add a doc heading, or add an "
        "allowlist entry with a one-line reason):\n  " + _fmt(undocumented)
    )


def test_allowlists_do_not_accumulate_dead_entries() -> None:
    """Every frozen exemption must still describe something real, or it's just noise.

    A ``_SIDECAR_ONLY_DOCS`` entry that stops being a documented heading, or an
    ``_UNDOCUMENTED`` entry for a route that got removed (or got its own doc heading),
    should be deleted from the allowlist in the same commit — otherwise the allowlist
    silently stops meaning anything and nobody notices when it does.
    """
    documented = _documented_pairs()
    live = _live_pairs()

    dead_sidecar_entries = set(_SIDECAR_ONLY_DOCS) - set(documented)
    assert not dead_sidecar_entries, (
        "_SIDECAR_ONLY_DOCS lists heading(s) no longer present in docs/api/*.md — "
        "remove the stale allowlist entry:\n  " + _fmt(dead_sidecar_entries)
    )

    dead_undocumented_entries = set(_UNDOCUMENTED) - live
    assert not dead_undocumented_entries, (
        "_UNDOCUMENTED lists route(s) no longer live in archimedes.main — remove the "
        "stale allowlist entry:\n  " + _fmt(dead_undocumented_entries)
    )
