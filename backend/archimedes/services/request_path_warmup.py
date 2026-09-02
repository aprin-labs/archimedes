# Fail-soft by design for per-step errors: a broken cache step must not
# wedge boot. A *timeout* is the opposite — that is the #1713 bug, and it
# raises RequestPathWarmupTimeout so the process never listens cold.
# ruff: noqa: BLE001
"""Prime the request-path caches before a new task is an ALB target (#1713).

Every deploy used to ship a cold fleet: the first hits after a roll paid
12–13s on ``GET /api/strategies/`` and up to 24s on
``GET /api/selection-bias/gate`` while ``get_all_daily_returns`` decoded
artifact blobs and ``rigor_cache`` filled. Warm Library is 0.75–0.85s. With
near-continuous deploys the fleet was perpetually cold — which is most of
what CloudWatch showed as 'unhealthy' — and the post-rollout answer probe
(5 consecutive <5s) failed whole deploys on single cold samples.

``arm_request_path_warmup`` runs FROM the FastAPI lifespan BEFORE ``yield``,
so uvicorn is not listening and the ALB target cannot be healthy until the
caches the Library page actually reads are populated:

* cohort daily returns (process-local memo in ``backtest_repository``)
* the ``strategies_list:`` ``rigor_cache`` entry
* the ``selection_bias_gate:`` ``rigor_cache`` entry (and the shared Redis
  layer, when configured)
* explore-assets, kicked off but not awaited (oracle + yfinance can take
  up to ~57s; blocking boot on that would blow the ALB 90s grace window)

Anti-goals from the issue: do not loosen the 5s answer-probe bar; do not
mark the task healthy before the app can actually serve. ``/health`` stays
200 once we listen — we simply do not listen until warmup finishes. A
timed-out warmup must **not** become an ALB-ready target: the helper
raises :class:`RequestPathWarmupTimeout` and lifespan does not catch it,
so uvicorn never binds.

The primes themselves are synchronous (blob decode, cohort DSR/PBO). They
run in a worker thread so ``asyncio.wait_for`` can actually fire; wrapping
the sync calls in ``wait_for`` on the event loop cannot interrupt them.
Cancelling ``to_thread`` does not kill the worker, but the decision not
to listen fires on the budget and the process exits with the thread.

The lifespan MUST NOT call ``evaluate_rigor_gate`` itself or read
``BacktestResultRecord`` (2026-08-19 OOM: the lifespan frame is pinned at
yield and used to retain every artifact_json blob). This module is the
indirection that keeps those names out of ``lifespan``'s source; the
decoded float series live in the cohort-returns cache, not in the
lifespan frame.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Sized off the measured first-hit cost (12–24s sequential, up to ~42s for
# blob decode + strategies-list rigor + selection-bias gate) with headroom.
# Must fit inside the backend container healthCheck startPeriod that
# ``ecs_rewrite_task_def.py`` pins on every CI deploy (90s, matching
# alb.tf / ecs.tf health_check_grace_period_seconds). A cloned live
# task-def still carries startPeriod=30 until that pin; do not raise this
# above the pinned startPeriod.
WARMUP_BUDGET_SECONDS = 60.0


class RequestPathWarmupTimeout(Exception):
    """Warmup exceeded the boot budget. The process must not listen cold."""


def warmup_enabled() -> bool:
    """Warmup is on in production, off under pytest, kill-switchable.

    ``TESTING``: the suite drives ``lifespan`` in several files; running the
    real cohort gate at import-time would make those tests pay the Library
    page's compute and would hit the network for Explore. The unit tests
    for this module call ``_prime_sync`` / ``arm_request_path_warmup``
    directly.

    ``REQUEST_PATH_WARMUP=0``: emergency kill switch. A broken warmup must
    not be able to wedge every deploy; flipping this serves cold (the
    pre-#1713 behaviour) without a rollback. Explicit, and the only
    remaining path that listens without priming.
    """
    if os.getenv("TESTING"):
        return False
    return os.getenv("REQUEST_PATH_WARMUP", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


async def arm_request_path_warmup(app: Any) -> dict[str, bool]:
    """Prime request-path caches before listen. Timeout aborts boot.

    Returns a step → succeeded map. Explore is reported as armed (the task
    is scheduled), not as completed — awaiting it would re-introduce the
    57s rebuild into the boot critical path.

    Per-step failures fail-soft (an empty CI library still boots). Exhausting
    ``WARMUP_BUDGET_SECONDS`` raises :class:`RequestPathWarmupTimeout` —
    uvicorn must not bind, and ECS must not get a healthy target.
    """
    if not warmup_enabled():
        logger.info("request-path warmup skipped")
        return {}
    try:
        # to_thread is load-bearing: wait_for cannot interrupt a sync prime
        # running on the event loop. Cancelling the wrapper does not kill
        # the worker thread; the timeout still decides not to listen.
        warmed = await asyncio.wait_for(
            asyncio.to_thread(_prime_sync),
            timeout=WARMUP_BUDGET_SECONDS,
        )
    except TimeoutError as exc:
        logger.error(
            "request-path warmup timed out after %.0fs — refusing to listen with cold caches",
            WARMUP_BUDGET_SECONDS,
        )
        raise RequestPathWarmupTimeout(
            f"request-path warmup exceeded {WARMUP_BUDGET_SECONDS:.0f}s — refusing to listen cold"
        ) from exc
    except Exception as exc:
        logger.warning("request-path warmup failed (non-fatal): %s", exc)
        return {}

    _arm_explore(app, warmed)
    return warmed


def _prime_sync() -> dict[str, bool]:
    """Run the Library-path primes. Synchronous: blob decode + rigor.

    Called from a worker thread so the boot budget can fire. Must not
    touch ``app.state`` or ``asyncio.create_task`` — those belong on the
    serving loop after this returns.
    """
    warmed = {
        "cohort_returns": False,
        "strategies_list": False,
        "selection_bias_gate": False,
        "explore_assets_armed": False,
    }
    library: list[Any] = []
    try:
        from archimedes.api.strategies_routes import strategy_provider

        library = strategy_provider().list_strategies()
    except Exception as exc:
        logger.warning("request-path warmup: strategy library unavailable: %s", exc)
        return warmed

    try:
        from archimedes.db import get_session, init_db
        from archimedes.services.backtest_repository import get_all_daily_returns

        init_db()
        with get_session() as session:
            get_all_daily_returns(session, [s.id for s in library])
        warmed["cohort_returns"] = True
    except Exception as exc:
        logger.warning("request-path warmup: cohort returns failed: %s", exc)

    try:
        from archimedes.api.strategies_routes import _live_rigor_results_for_strategies

        _live_rigor_results_for_strategies(library)
        warmed["strategies_list"] = True
    except Exception as exc:
        logger.warning("request-path warmup: strategies_list rigor cache failed: %s", exc)

    try:
        from archimedes.api.selection_bias_routes import DEFAULT_LEVEL, evaluate_rigor_gate

        # evaluate_rigor_gate is an async route handler whose work is sync.
        # This function runs in a worker thread (no running loop), so
        # asyncio.run is the production path. Do not call this on the
        # uvicorn loop — use arm_request_path_warmup / to_thread.
        asyncio.run(evaluate_rigor_gate(strictness=DEFAULT_LEVEL))
        warmed["selection_bias_gate"] = True
    except Exception as exc:
        logger.warning("request-path warmup: selection-bias gate cache failed: %s", exc)

    logger.info("request-path warmup: %s", warmed)
    return warmed


def _arm_explore(app: Any, warmed: dict[str, bool]) -> None:
    """Fire-and-forget explore warmup on the serving loop. Best-effort."""
    try:
        from archimedes.services.asset_market_service import asset_market_service

        app.state.explore_warmup_task = asyncio.create_task(
            _warm_explore(asset_market_service),
            name="explore-assets-warmup",
        )
        warmed["explore_assets_armed"] = True
    except Exception as exc:
        logger.warning("request-path warmup: explore-assets arm failed: %s", exc)


async def _prime(app: Any) -> dict[str, bool]:
    """Test helper: the same primes as boot, without the budget.

    Production boot uses :func:`arm_request_path_warmup` so a hung sync
    prime is interruptible. Tests that assert cache contents call this.
    """
    warmed = await asyncio.to_thread(_prime_sync)
    _arm_explore(app, warmed)
    return warmed


async def _warm_explore(svc: Any) -> None:
    try:
        await svc.list_assets()
        logger.info("request-path warmup: explore-assets cache primed")
    except Exception as exc:
        logger.warning("request-path warmup: explore-assets failed: %s", exc)
