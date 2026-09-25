"""Shared helpers used by multiple per-resource routers.

Service singletons and mapping utilities that are imported by two or more
route modules live here so that each router stays self-contained.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from archimedes.chain.oracle_updater import OracleUpdater
from archimedes.services.asset_service import AssetService
from archimedes.services.config_service import ConfigService
from archimedes.services.strategy_provider import LocalStrategyProvider, default_provider
from archimedes.services.vault_service import VaultService

_logger = logging.getLogger(__name__)

# ── Service singletons ─────────────────────────────────────────
asset_svc = AssetService()
vault_svc = VaultService()
config_svc = ConfigService()
oracle = OracleUpdater()


@lru_cache(maxsize=1)
def strategy_provider() -> LocalStrategyProvider:
    """Lazily-constructed, cached strategy provider.

    Deliberately NOT built at module-import time. ``default_provider()``
    eagerly queries the ``strategy_backtest_fixtures`` table on
    construction (via ``LocalStrategyProvider.refresh()``); building it at
    import time races ``main.py``'s ``init_db()``, which only runs after
    all router modules (this one included) have already been imported.
    Deferring construction to first call means it happens after startup's
    ``init_db()`` has run — and, in prod, after a backfill script has
    populated the table and the process has restarted, so a single restart
    is sufficient rather than two. Call sites: ``strategy_provider().foo()``.
    """
    return default_provider()


def assert_strategy_visible(strategy_id: str, request) -> None:
    """Raise 404 unless the caller may learn that ``strategy_id`` EXISTS.

    THE **card-level** existence gate, shared by ``GET /api/strategies/{id}``
    and the strategy-scoped trace listing on ``/api/traces``. It asks
    ``is_strategy_visible``, so a published row is readable by anyone —
    correct, because both surfaces answer a card-level question (the detail
    route serves card content; "does id X exist" is card-level too).

    **Not for reasoning surfaces.** ``GET /{id}/returns``, ``GET /{id}/debate``
    and ``_spec_for_strategy`` gate on ``is_strategy_reasoning_visible``
    instead — publishing consents to sharing the result, not the derivation
    (#1557). Adding one of them to this helper's call sites re-opens that hole.
    Read the matrix in ``services/strategy_visibility.py`` before wiring a new
    surface to either one.

    Curated strategies (the provider path) are always public. A generated
    strategy is **404, never 403**, when the caller may not read it, so
    existence stays hidden either way; a 403 would confirm the id is real.

    It lives here rather than in each router because this codebase's
    characteristic defect is a rule being fixed in the one function the current
    ticket touches while sibling readers keep the old behaviour — the same
    reason ``is_strategy_visible`` refuses to be re-implemented at call sites.
    A new per-strategy surface that forgets this call is an authorization bug,
    and a *second* copy of the gate is the same bug with a longer fuse.

    Takes the raw ``Request`` because both identity sources are read off it:
    the SIWE-verified wallet (legacy rows) and the canonical Better Auth user
    (rows with ``owner_user_id``). ``is_strategy_visible`` decides which one
    counts — this function must not pre-empt that choice.
    """
    from fastapi import HTTPException

    from archimedes.api.account_auth import get_current_user
    from archimedes.api.auth_siwe import get_verified_wallet
    from archimedes.db import get_session
    from archimedes.models.strategy_store import StrategyRecord
    from archimedes.services.strategy_visibility import is_strategy_visible

    if strategy_provider().get_strategy(strategy_id) is not None:
        return

    with get_session() as session:
        row = session.query(StrategyRecord).filter_by(id=strategy_id).first()
        caller = get_verified_wallet(request)
        user = get_current_user(request)
        if not is_strategy_visible(row, caller, caller_user_id=user.id if user else None):
            raise HTTPException(status_code=404, detail="Strategy not found")


async def persist_trace_off_chain(trace) -> None:
    """Save a ReasoningTrace to Redis so it appears in /api/traces feed.

    Non-fatal -- failures are logged but don't break the caller.
    """
    try:
        from archimedes.services.redis_state import AgentStateStore

        state = AgentStateStore()
        try:
            await state.save_trace(
                {
                    "id": trace.id,
                    "vault_address": getattr(trace, "vault_address", "") or "",
                    "decision_type": (
                        trace.decision_type.value if hasattr(trace.decision_type, "value") else str(trace.decision_type)
                    ),
                    "trigger": getattr(trace, "trigger", "") or "",
                    "timestamp": (
                        trace.timestamp.isoformat() if hasattr(trace.timestamp, "isoformat") else str(trace.timestamp)
                    ),
                    "market_context": getattr(trace, "market_context", {}) or {},
                    "portfolio_before": getattr(trace, "portfolio_before", {}) or {},
                    "portfolio_after": getattr(trace, "portfolio_after", {}) or {},
                    "reasoning": getattr(trace, "reasoning", "") or "",
                    "confidence": getattr(trace, "confidence", 0.0) or 0.0,
                    "trades_executed": getattr(trace, "trades_executed", []) or [],
                    "strategies_referenced": getattr(trace, "strategies_referenced", []) or [],
                    # HASHED field (#1637). It was silently dropped here, so a
                    # trace persisted through this helper could never rebuild
                    # its own canonical bytes: `/canonical` would reconstruct it
                    # with an empty list and the recomputed hash would not match
                    # the one `compute_hash()` produced. This function has no
                    # callers today (see the module note), which is exactly why
                    # the omission survived — it is fixed here so wiring it
                    # later cannot inherit the trap, and so `/verify`'s new
                    # source-paper check reads a field that is actually there.
                    "consulted_paper_hashes": getattr(trace, "consulted_paper_hashes", []) or [],
                    "trace_hash": getattr(trace, "trace_hash", "") or "",
                    "arc_tx_hash": getattr(trace, "arc_tx_hash", None),
                    "is_verified": bool(getattr(trace, "arc_tx_hash", None)),
                }
            )
        finally:
            await state.close()
    except Exception as exc:
        _logger.warning("trace persistence failed (non-fatal): %s", exc)
