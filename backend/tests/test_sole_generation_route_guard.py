"""Structural guard: the debate society is the ONLY generation route.

``docs/adr/debate-society-sole-generation-pipeline.md`` records the decision;
until 2026-08-31 the tree did not actually enforce it. A second live,
SIWE-gated, LLM-spending generation endpoint survived the Phase-3 cutover —
``POST /api/strategies/generate`` → ``_run_fusion_job``, gated on
``ARCHIMEDES_FUSION_ENABLED`` (a flag retired 2026-09-02, deck Q4) that was
set to ``true`` in every deployed environment anyway. An ADR without a test is
a comment.

Two independent guards, because one alone is evadable:

1. **Route table.** Walks the *live* FastAPI app's routes and asserts no
   generation route is mounted under ``/api/strategies``. A route added with
   any decorator, in any module, that ends up on the app is caught here.
2. **Import graph.** A route could enqueue fusion work without the literal
   path ``/generate``. So this walks the AST of every module under
   ``backend/archimedes/`` and asserts that the proposer entry points
   (``StrategyFusion`` / the retired ``default_fusion`` factory) are imported
   by exactly one module: ``agents/debate_engine.py``. Adding
   ``from archimedes.agents.strategy_fusion import StrategyFusion`` anywhere
   else — including a function-local import inside a route handler, which is
   exactly how the deleted route did it — fails this test.

Neither guard is tautological. Both were run against the pre-deletion tree and
both fail there: guard 1 finds ``POST /api/strategies/generate`` and
``GET /api/strategies/generate/{job_id}``; guard 2 finds the function-local
``from archimedes.agents.strategy_fusion import FusionBrief, default_fusion``
inside ``strategies_routes._run_fusion_job``. The demonstration is recorded in
the PR that added this file.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest
from fastapi import routing as fastapi_routing
from fastapi.routing import APIRoute

# The one module allowed to reach for the fusion proposer. The society IS the
# pipeline; fusion is a step inside it, not a route of its own.
_ALLOWED_PROPOSER_IMPORTERS = {"agents/debate_engine.py"}

# Entry points that construct or hand back a proposer. ``default_fusion`` no
# longer exists — it is listed so that re-adding the model-blind factory *and*
# calling it from a new module trips this guard rather than sliding back in.
_PROPOSER_NAMES = {"StrategyFusion", "default_fusion"}

_FUSION_MODULE = "archimedes.agents.strategy_fusion"

_PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "archimedes"


def _app_routes() -> list[tuple[str, frozenset[str]]]:
    """Flatten ``archimedes.main.app`` into (path, methods) pairs.

    Same walk as ``tests/test_api_docs_drift.py::_live_pairs`` and for the same
    reason: fastapi>=0.139 resolves ``include_router`` lazily, so ``app.routes``
    is a mix of real ``APIRoute`` objects and opaque ``_IncludedRouter``
    wrappers whose fully-prefixed children only appear via
    ``effective_route_contexts()``. Reading ``route.path`` off the top level
    would silently see ~6 of the ~250 routes — and a guard that inspects almost
    nothing passes for the wrong reason.
    """
    os.environ.setdefault("TESTING", "1")
    from archimedes.main import app

    included_router_cls = getattr(fastapi_routing, "_IncludedRouter", None)

    out: list[tuple[str, frozenset[str]]] = []
    for route in app.routes:
        if isinstance(route, APIRoute):
            out.append((route.path, frozenset(route.methods or ())))
        elif included_router_cls is not None and isinstance(route, included_router_cls):
            for ctx in route.effective_route_contexts():
                if isinstance(ctx.original_route, APIRoute):
                    out.append((ctx.path_format, frozenset(ctx.methods or ())))
    return out


def test_no_generation_route_is_mounted_under_api_strategies():
    """``/api/strategies`` hosts library reads and mutations — never generation."""
    offenders = [
        (path, sorted(methods))
        for path, methods in _app_routes()
        # "/api/strategies/generated" (the library LIST route) is a different
        # path and must survive: match the generate route and its sub-paths
        # only.
        if path == "/api/strategies/generate" or path.startswith("/api/strategies/generate/")
    ]
    assert offenders == [], (
        "A generation route is mounted under /api/strategies, which the sole-pipeline ADR forbids "
        f"(docs/adr/debate-society-sole-generation-pipeline.md). Found: {offenders}. "
        "Generation belongs to the debate society at POST /api/generate/start."
    )


def test_the_debate_route_is_actually_mounted():
    """Anti-vacuity partner for the guard above.

    If the app ever failed to mount its routers, the offender list would be
    empty for the wrong reason and the guard would pass while generation was
    entirely gone. Assert the surviving pipeline is really there.
    """
    paths = {path for path, _ in _app_routes()}
    assert "/api/generate/start" in paths, (
        "The debate society's generation route is missing — the route-table guard above "
        "would pass vacuously. Routers did not mount as expected."
    )


def _modules_importing_the_proposer() -> dict[str, set[str]]:
    """rel-path → the proposer names it imports, by AST (not by grep).

    AST rather than text search on purpose: ``evaluation/stockbench/adapter.py``
    mentions ``StrategyFusion.propose`` in a docstring and in a printed report
    line, and a grep-based guard would have to special-case it. An import is an
    unambiguous, comment-proof signal of a real call site.
    """
    found: dict[str, set[str]] = {}
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        rel = path.relative_to(_PACKAGE_ROOT).as_posix()
        if rel == "agents/strategy_fusion.py":  # the defining module itself
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover — a syntax error is another test's problem
            continue
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == _FUSION_MODULE:
                names |= {a.name for a in node.names} & _PROPOSER_NAMES
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == _FUSION_MODULE:
                        # `import archimedes.agents.strategy_fusion` — the whole
                        # module surface, proposer included.
                        names.add(alias.asname or alias.name)
        if names:
            found[rel] = names
    return found


def test_only_the_debate_engine_reaches_for_the_fusion_proposer():
    importers = _modules_importing_the_proposer()
    unexpected = {k: sorted(v) for k, v in importers.items() if k not in _ALLOWED_PROPOSER_IMPORTERS}
    assert unexpected == {}, (
        "A module outside the debate engine imports the fusion proposer, which means a second "
        f"generation path exists: {unexpected}. Fusion is a step inside the society "
        "(agents/debate_engine.py::_propose_pool), not a standalone runner. See "
        "docs/adr/debate-society-sole-generation-pipeline.md."
    )


def test_the_guard_above_can_actually_see_the_debate_engines_import():
    """Anti-vacuity partner: prove the AST walk finds the one legitimate importer.

    Without this, deleting or renaming ``debate_engine.py`` (or breaking the
    walk) would empty the map and make the guard pass while proving nothing.
    """
    importers = _modules_importing_the_proposer()
    assert "agents/debate_engine.py" in importers, (
        "The AST walk found no proposer import in agents/debate_engine.py — the guard above "
        f"is inspecting the wrong thing. Saw: {sorted(importers)}"
    )
    assert "StrategyFusion" in importers["agents/debate_engine.py"]


def test_the_deleted_bypass_symbols_are_gone_from_the_strategies_router():
    """The route handler and its background worker, by name."""
    from archimedes.api import strategies_routes

    for symbol in ("generate_strategy", "_run_fusion_job", "get_generation_job"):
        assert not hasattr(strategies_routes, symbol), (
            f"api/strategies_routes.{symbol} is back. That module hosts no generation route; "
            "the debate society at POST /api/generate/start is the sole pipeline."
        )


@pytest.mark.parametrize("survivor", ["/api/strategies/generated", "/api/generate/start"])
def test_the_deletion_did_not_take_the_neighbours_with_it(survivor):
    """The library LIST route and the debate route share a prefix with the
    deleted one. Both must still be mounted."""
    assert survivor in {path for path, _ in _app_routes()}
