# Fail-soft by design: a broken cache step must not wedge boot.
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
200 once we listen — we simply do not listen until warmup finishes (or
times out / fails-soft). A hung warmup must not pin boot: the budget below
is inside the ALB grace window.

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

# Sized off the measured first-hit cost (12–24s) with headroom, and strictly
# inside alb.tf's 90s health-check grace. Exceeding this still boots — the
# task serves cold rather than never joining the target group.
WARMUP_BUDGET_SECONDS = 60.0


def warmup_enabled() -> bool:
    """Warmup is on in production, off under pytest, kill-switchable.

    ``TESTING``: the suite drives ``lifespan`` in several files; running the
    real cohort gate at import-time would make those tests pay the Library
    page's compute and would hit the network for Explore. The unit tests
    for this module call ``_prime`` / ``arm_request_path_warmup`` directly.

    ``REQUEST_PATH_WARMUP=0``: emergency kill switch. A broken warmup must
    not be able to wedge every deploy; flipping this serves cold (the
    pre-#1713 behaviour) without a rollback.
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
    """Prime request-path caches, fail-soft, never raise out to lifespan.

    Returns a step → succeeded map. Explore is reported as armed (the task
    is scheduled), not as completed — awaiting it would re-introduce the
    57s rebuild into the boot critical path.
    """
    if not warmup_enabled():
        logger.info("request-path warmup skipped")
        return {}
    try:
        return await asyncio.wait_for(_prime(app), timeout=WARMUP_BUDGET_SECONDS)
    except TimeoutError:
        logger.warning(
            "request-path warmup timed out after %.0fs — task will serve with cold caches",
            WARMUP_BUDGET_SECONDS,
        )
        return {}
    except Exception as exc:
        logger.warning("request-path warmup failed (non-fatal): %s", exc)
        return {}


async def _prime(app: Any) -> dict[str, bool]:
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

        await evaluate_rigor_gate(strictness=DEFAULT_LEVEL)
        warmed["selection_bias_gate"] = True
    except Exception as exc:
        logger.warning("request-path warmup: selection-bias gate cache failed: %s", exc)

    try:
        from archimedes.services.asset_market_service import asset_market_service

        app.state.explore_warmup_task = asyncio.create_task(
            _warm_explore(asset_market_service),
            name="explore-assets-warmup",
        )
        warmed["explore_assets_armed"] = True
    except Exception as exc:
        logger.warning("request-path warmup: explore-assets arm failed: %s", exc)

    logger.info("request-path warmup: %s", warmed)
    return warmed


async def _warm_explore(svc: Any) -> None:
    try:
        await svc.list_assets()
        logger.info("request-path warmup: explore-assets cache primed")
    except Exception as exc:
        logger.warning("request-path warmup: explore-assets failed: %s", exc)
