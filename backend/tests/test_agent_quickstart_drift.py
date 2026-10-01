"""``docs/agent-quickstart.md`` must describe routes that exist, with curls that match.

The quickstart is the one document written for an agent that has never seen this API and
will do exactly what the page says, in order. That makes a stale line there more expensive
than anywhere else in ``docs/``: the reader has no prior to correct against, spends a
request, gets a 404, and cannot tell "wrong path" from "endpoint down" — the #1293
dogfood finding, in its purest form.

Same guard shape as its two neighbours, extended one step:

- ``test_agent_discovery.py`` asserts the discovery documents' routes resolve against the
  live app. This file reuses that module's ``unresolved_routes`` helper and its single
  ``/api/auth/`` exemption rather than re-deriving either — the exemption stays narrow in
  one place, and widening it there is still the only way to disable both guards.
- ``test_api_docs_drift.py`` parses ``### METHOD /path`` headings out of ``docs/api/*.md``.
  The quickstart is a walkthrough, not a reference, so its routes live in a step table and
  in prose backticks instead of in headings — hence a different parser, same contract.

The step this file adds is **curl-vs-prose**: every ``curl`` on the page must call a route
the page also names. A worked example that quietly drifts from the table above it is the
failure mode a heading parser cannot see, and the one a copy-pasting reader hits first.

Hermetic: TESTING=1 import of ``archimedes.main`` (conftest sets it) plus in-memory regex
parsing of two committed markdown files. No DB / Redis / RPC / network.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.test_agent_discovery import _EXTERNALLY_SERVED_PREFIXES, _openapi_index, unresolved_routes

REPO_ROOT = Path(__file__).resolve().parents[2]
QUICKSTART = REPO_ROOT / "docs" / "agent-quickstart.md"
DOCS_INDEX = REPO_ROOT / "docs" / "doc-index.md"

# "GET /api/thing" inside a backtick span — the step table's cells and the prose both use
# that form, so one pattern covers the whole page.
_BACKTICKED_ROUTE_RE = re.compile(r"`(GET|POST|PUT|PATCH|DELETE) (/[^`\s]+)`")

# A fenced shell block. Only these are treated as executable examples.
_FENCE_RE = re.compile(r"```(?:bash|sh|console)\n(.*?)```", re.DOTALL)

# The URL a curl actually calls, and the method it calls it with.
_CURL_URL_RE = re.compile(r"\$BASE(/[^\s\\\"']*)")
_CURL_METHOD_RE = re.compile(r"-X\s+([A-Z]+)")


def _text() -> str:
    assert QUICKSTART.exists(), f"{QUICKSTART} is missing — the doc index promises it"
    return QUICKSTART.read_text(encoding="utf-8")


def _normalize(path: str) -> str:
    """Collapse every path parameter to ``{}`` so ``{job_id}`` and ``$JOB_ID`` compare.

    The page writes parameters two ways on purpose — ``{job_id}`` when naming the route,
    ``$JOB_ID`` when showing the command — and both are correct for their context. This is
    the only place the two spellings have to be reconciled.
    """
    segments = []
    for segment in path.split("/"):
        segments.append(
            "{}" if segment.startswith("$") or (segment.startswith("{") and segment.endswith("}")) else segment
        )
    return "/".join(segments)


def declared_routes(text: str) -> set[str]:
    """``{"METHOD /path", …}`` — every route the page names in prose or in its step table."""
    return {f"{method} {path}" for method, path in _BACKTICKED_ROUTE_RE.findall(text)}


def curl_calls(text: str) -> set[str]:
    """``{"METHOD /path", …}`` — every call the page's shell examples actually make."""
    calls: set[str] = set()
    for block in _FENCE_RE.findall(text):
        if "curl" not in block:
            continue
        method_match = _CURL_METHOD_RE.search(block)
        method = method_match.group(1) if method_match else "GET"
        for path in _CURL_URL_RE.findall(block):
            calls.add(f"{method} {path.split('?')[0]}")
    return calls


# ── the exemption is inherited, not re-declared ──────────────────────────────


def test_the_better_auth_exemption_is_the_shared_one():
    """Reusing the sibling's constant is the point: one place to widen, one place to audit."""
    assert _EXTERNALLY_SERVED_PREFIXES == ("/api/auth/",)


# ── (a) every route the page names exists on the app ─────────────────────────


def test_the_quickstart_names_the_routes_the_journey_needs():
    """A cheap sanity floor before the resolution check: the spine must be on the page.

    Without this, deleting the whole step table would leave the guard below vacuously
    green — an empty set has no unresolved members.
    """
    declared = declared_routes(_text())
    for route in (
        "GET /api/agent/manifest",
        "GET /api/generate/quote",
        "POST /api/auth/sign-in/email",
        "GET /api/auth/get-session",
        "POST /api/generate/start",
        "GET /api/generate/jobs/{job_id}/candidates",
        "GET /api/strategies/{strategy_id}",
        "POST /api/paper/deployments",
        "GET /api/paper/deployments/{deployment_id}",
    ):
        assert route in declared, f"docs/agent-quickstart.md no longer names {route}"


def test_every_route_the_quickstart_names_resolves_against_the_live_openapi():
    """The docs cannot promise a route archimedes.main does not serve."""
    declared = sorted(declared_routes(_text()))
    assert declared, "parsed no routes at all — the parser or the page changed shape"
    unresolved = unresolved_routes(declared, _openapi_index())
    assert unresolved == [], (
        "docs/agent-quickstart.md names routes that do NOT exist on the running app — an "
        "agent following this page would 404 with no way to diagnose it:\n  " + "\n  ".join(unresolved)
    )


# ── (b) the worked examples match the routes the page names ──────────────────


def test_every_curl_calls_a_route_the_page_also_names():
    """A copy-pasteable command that drifts from its own documentation is the worst case.

    Compared after normalizing parameters, so ``$JOB_ID`` in the command matches
    ``{job_id}`` in the table — the difference this page makes on purpose.
    """
    text = _text()
    declared = {_normalize(route) for route in declared_routes(text)}
    calls = {_normalize(call) for call in curl_calls(text)}

    assert calls, "parsed no curl commands — the parser or the page changed shape"
    undocumented = sorted(calls - declared)
    assert not undocumented, (
        "docs/agent-quickstart.md runs curls against routes it never names in its step "
        "table or prose (add the route to the page, or fix the command):\n  " + "\n  ".join(undocumented)
    )


def test_every_curl_target_also_resolves_against_the_live_openapi():
    """The commands are checked against the app directly, not only against the prose.

    Belt and braces on purpose: the check above would stay green if a route were wrong in
    the table AND in the command in the same way, which is exactly how a bad rename lands.
    """
    text = _text()
    # Recover the parameter spelling from the declared set so a curl's ``$JOB_ID`` can be
    # resolved: the OpenAPI index is keyed by ``{job_id}``, and only the page knows which
    # declared route a given command corresponds to.
    by_normalized = {_normalize(route): route for route in declared_routes(text)}
    targets = [by_normalized[key] for call in curl_calls(text) if (key := _normalize(call)) in by_normalized]

    assert targets, "no curl target could be mapped to a declared route"
    assert unresolved_routes(targets, _openapi_index()) == []


# ── the doc index ────────────────────────────────────────────────────────────


def test_the_quickstart_is_listed_in_the_docs_index():
    """CLAUDE.md's rule, asserted: "a doc not listed there does not exist"."""
    assert "agent-quickstart.md" in DOCS_INDEX.read_text(encoding="utf-8"), (
        "docs/agent-quickstart.md has no row in docs/doc-index.md — add one in the same commit."
    )


# ── the rigor verdict this page teaches (#1746 PR-B) ─────────────────────────
#
# The page's own paragraph says "every route serves that stored verdict", and
# the four-state table eight lines above it said the opposite — `pass` meant
# "the live gate passed" and `pending` meant "no real persisted returns yet".
# The second is the one with teeth: between a deploy of #1746 PR-B and the
# `grade_curated` run (docs/runbooks/curated-backtests.md § "Grading on its
# own"), every curated strategy reads `pending` WITH real persisted returns, so
# an agent following that row concludes the backtest does not exist.


def test_the_page_does_not_promise_a_gate_run_on_read():
    """MUTATION: put "step 9 is the live gate the server enforces" back."""
    text = _text().lower()
    for phrase in ("live gate", "live verdict"):
        assert phrase not in text, (
            f"docs/agent-quickstart.md still says {phrase!r}. Every strategy route serves the "
            "STORED verdict of record and runs no gate — docs/adr/rigor-verdict-of-record.md."
        )


def test_the_pending_row_names_the_ungraded_case():
    """``pending`` is "nobody graded this", not "there is no backtest".

    MUTATION: restore "No real persisted returns yet; the gate could not run".
    """
    rows = [line for line in _text().splitlines() if line.startswith("| `pending`")]
    assert len(rows) == 1, f"expected exactly one `pending` row in the four-state table, found {len(rows)}"
    row = rows[0].lower()
    assert "grading job" in row, (
        f"the `pending` row does not name the ungraded case: {rows[0]!r}. A curated strategy with "
        "real persisted returns reads `pending` until the grading job has run over them, and an "
        "agent told `pending` means 'no returns yet' will read that as a broken backtest (#1184)."
    )
