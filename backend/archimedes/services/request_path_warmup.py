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

WARMUP_BUDGET_SECONDS = 60.0


class RequestPathWarmupTimeout(Exception):
    """Warmup exceeded the boot budget. The process must not listen cold."""


def warmup_enabled() -> bool:
    if os.getenv("TESTING"):
        return False
    return os.getenv("REQUEST_PATH_WARMUP", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


async def arm_request_path_warmup(app: Any) -> dict[str, bool]:
    if not warmup_enabled():
        logger.info("request-path warmup skipped")
        return {}
    try:
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
