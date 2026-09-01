"""Archimedes — FastAPI application entrypoint.

Minimal bootstrap for the hackathon MVP. All routes are wired to
chain services that read/write Arc smart contracts.
"""

import asyncio
import concurrent.futures
import faulthandler
import logging
import os

# #1632: the backend has died twice with a bare exit 139 (SIGSEGV) under RPC
# distress and left NOTHING in the logs — the crash context had to be inferred
# from the preceding lines. faulthandler dumps every thread's Python traceback
# to stderr on SIGSEGV/SIGFPE/SIGABRT/SIGBUS, which the ECS awslogs driver
# ships like any other stderr line, so the NEXT native crash names its frames.
# Enabled unconditionally: it costs nothing at rest and only speaks on faults.
faulthandler.enable()

# Load .env into os.environ at import time for modules that use os.getenv()
# (circle_signer, oracle_updater) — pydantic ChainSettings handles ARC_ vars itself.
from collections.abc import AsyncGenerator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

load_dotenv("../.env", override=True)  # Project root .env first (has real secrets)
load_dotenv(".env", override=False)  # Backend-local .env fills in any missing (no override)

# Load secrets from AWS SSM Parameter Store — PRODUCTION ONLY. Gated on
# PUBLIC_DOMAIN (same "is this production" signal used below for the docs gate
# and the EMAIL_ENCRYPTION_KEY fail-close) so a local run can never pull real
# prod secrets from SSM, even with ambient AWS creds and AWS_SSM_PATH_PREFIX
# resolving to a real path — issue #1044. Belt-and-suspenders with
# AWS_SSM_PATH_PREFIX defaulting blank in .env.example: this gate is what
# actually stops the fetch regardless of what that var resolves to. Must run
# BEFORE any service imports that read os.environ for API keys / secrets.
from archimedes.services.secrets_service import load_ssm_secrets

if os.getenv("PUBLIC_DOMAIN"):
    load_ssm_secrets()

# Shared rate limiter (Redis-backed, falls back to in-memory).
# Defined in a separate module to avoid circular imports with route modules.
from archimedes.api.account_auth import better_auth_session_middleware, require_current_user
from archimedes.api.account_usage_routes import account_usage_router
from archimedes.api.agent_manifest_routes import agent_manifest_router
from archimedes.api.api_key_routes import api_key_router
from archimedes.api.corpus_routes import corpus_router
from archimedes.api.explore_routes import explore_router
from archimedes.api.features_routes import features_router
from archimedes.api.generate_routes import generate_public_router, generate_router
from archimedes.api.leaderboard_routes import leaderboard_router
from archimedes.api.limiter import limiter
from archimedes.api.paper_routes import paper_router
from archimedes.api.payment_routes import payment_router

# FAIL-SOFT import: marketplace_routes → service → payments imports circlekit
# (the circle-titanoboa-sdk VCS dependency). If that dependency fails to IMPORT
# in the runtime image (installs fine in the builder but a runtime lib/module is
# missing in the slim final stage), the whole backend must NOT crash on boot —
# it degrades to "marketplace routes absent" (404). PR #958 prod incident:
# the first successful deploy crash-looped the backend on exactly this import.
try:
    from archimedes.api.marketplace_routes import marketplace_router
except Exception as _mkt_exc:
    logging.getLogger(__name__).error(
        "marketplace router unavailable — importing it failed (running WITHOUT marketplace): %s",
        _mkt_exc,
    )
    marketplace_router = None

# (the marketplace route registration was removed — hardcoded fees + invented math, Issue #381)
from archimedes.api.metrics_private_routes import metrics_private_router
from archimedes.api.metrics_routes import metrics_router
from archimedes.api.portfolio_routes import portfolio_router
from archimedes.api.proposals_routes import proposals_router
from archimedes.api.rigor_verify_routes import rigor_verify_router
from archimedes.api.risk_routes import risk_router
from archimedes.api.routes import (
    agent_router,
    assets_router,
    config_router,
    papers_router,
    regime_router,
    strategies_router,
    swap_router,
    traces_router,
    vaults_router,
)
from archimedes.api.selection_bias_routes import selection_bias_router
from archimedes.api.user_routes import user_router
from archimedes.api.wallet_routes import wallet_router
from archimedes.db import init_db

logger = logging.getLogger(__name__)


def _assert_marketplace_live_or_dry(marketplace_router_obj: object, payments_dry_run: bool) -> None:
    """FATAL when circlekit failed to import while PAYMENTS_DRY_RUN=false (#1240).

    The import above degrades on purpose (PR #958 prod incident) so a broken
    circlekit install can never crash-loop the whole backend — correct when
    PAYMENTS_DRY_RUN=true, since no real money is at stake and "marketplace
    absent" (routes 404) is an honest, visible degraded state. It stops being
    correct the moment an operator sets PAYMENTS_DRY_RUN=false: that is a
    deliberate signal real charging is wanted, and "the process boots fine,
    most routes 200, marketplace quietly 404s" is a silent trap for exactly
    that operator, not a safe degrade — see architectural-principles.md's
    fail-soft-is-wrong-for-anything-a-claim-depends-on rule. Pulled into its
    own function so the assertion is unit-testable without importing the
    whole app.

    KNOWN LIMITATION (#1240 follow-up, not fixed here): this gates on the
    GLOBAL PAYMENTS_DRY_RUN, which infra/ecs.tf pins to "true" in prod. The
    rail actually settling real money today is the generation paywall, which
    is live via the generation-scoped GENERATION_PAYMENTS_DRY_RUN="false"
    (the 2026-08-20 split, #1428) — a switch this assertion does not read. So
    in prod as configured this guard is INERT: it will not refuse to boot even
    though real value is moving. Widening it to "any rail is live" is a
    behavior change to the boot path and wants its own PR; recorded here so
    the guard is not mistaken for protection it does not currently give.
    """
    if marketplace_router_obj is None and not payments_dry_run:
        raise RuntimeError(
            "FATAL: circlekit failed to import (marketplace router unavailable, see "
            "the error logged above) while PAYMENTS_DRY_RUN=false. Refusing to boot "
            "with real-money charging intended but no charging capability available. "
            "Fix the circlekit install, or set PAYMENTS_DRY_RUN=true if dry-run is "
            "actually what's intended."
        )


_assert_marketplace_live_or_dry(
    marketplace_router,
    os.getenv("PAYMENTS_DRY_RUN", "true").lower() in ("1", "true", "yes"),
)


class GatewayChainMismatch(RuntimeError):
    """Raised ONLY by _assert_gateway_chain_matches_rpc, and only on a
    confirmed GATEWAY_CHAIN/RPC mismatch or an unresolvable GATEWAY_CHAIN
    name under PAYMENTS_DRY_RUN=false — never on a connectivity failure.

    A dedicated type (rather than a bare RuntimeError) so the lifespan
    startup wrapper can re-raise exactly this failure mode as fatal without
    also catching an unrelated RuntimeError from elsewhere in the same try
    block (e.g. get_chain_id() raising on a closed aiohttp session/event
    loop) — a review finding on #1240: a type-based `except RuntimeError`
    at the call site made ANY RuntimeError fatal, including ones that must
    fall through to the non-fatal "connectivity issue?" warning branch.
    Subclasses RuntimeError so existing `pytest.raises(RuntimeError, ...)`
    tests against this function keep working unchanged.
    """


async def _assert_gateway_chain_matches_rpc(
    chain_client_obj: object, gateway_chain: str, payments_dry_run: bool
) -> None:
    """FATAL when GATEWAY_CHAIN resolves to a chain_id the RPC we actually
    talk to doesn't report — but only when PAYMENTS_DRY_RUN=false (#1240).

    circlekit's ``CHAIN_ALIASES`` maps ``"mainnet"`` -> ``"ethereum"``, so a
    fat-fingered ``GATEWAY_CHAIN`` can silently resolve to a real, DIFFERENT
    chain than the one Arc RPC (and the vault reads/writes) actually target
    — Gateway payments would settle on a chain trades don't execute on, and
    ``GET /api/config/contracts``' "chain" field would be lying about which
    chain we're on. Fatal only under PAYMENTS_DRY_RUN=false (same
    conditioning as ``_assert_marketplace_live_or_dry`` above): a dry-run/
    testnet boot with a stale or experimental GATEWAY_CHAIN value must not
    crash-loop over it, just log loudly.

    A connectivity failure (``chain_client_obj.get_chain_id()`` raising) is
    the CALLER's problem, not this function's — chain_client owns its own
    retry/backoff, and "the RPC was briefly unreachable at boot" is a
    different failure mode than "we are pointed at the wrong chain". This
    function only ever raises ``GatewayChainMismatch`` (never a bare
    ``RuntimeError``) on a confirmed mismatch, or an unresolvable
    GATEWAY_CHAIN name (also a real config bug worth being loud about) — any
    OTHER exception (e.g. get_chain_id() raising for connectivity reasons)
    propagates as whatever type it naturally is, unmodified, precisely so
    the caller can tell the two failure modes apart by type.

    KNOWN LIMITATION (#1240 follow-up, not fixed here): see the same note on
    _assert_marketplace_live_or_dry — the ``payments_dry_run`` this receives
    is the GLOBAL switch, "true" in prod, so the fatal branch never fires
    there. It bites hardest on this assertion specifically: the live
    generation paywall quotes ``chain: gateway_chain()`` to real payers, so a
    fat-fingered GATEWAY_CHAIN is exactly the failure this function exists to
    catch, and today it would only be logged as a warning.
    """
    from circlekit.constants import get_chain_config

    try:
        expected_chain_id = get_chain_config(gateway_chain).chain_id
    except ValueError as exc:
        message = f"GATEWAY_CHAIN={gateway_chain!r} is not a chain circlekit recognizes: {exc}"
        if payments_dry_run:
            logger.warning("%s (PAYMENTS_DRY_RUN=true — not fatal, but fix before flipping it)", message)
            return
        raise GatewayChainMismatch(f"FATAL: {message} Refusing to boot with PAYMENTS_DRY_RUN=false.") from exc

    actual_chain_id = await chain_client_obj.get_chain_id()
    if expected_chain_id != actual_chain_id:
        message = (
            f"GATEWAY_CHAIN={gateway_chain!r} resolves to chain_id={expected_chain_id}, but the "
            f"configured RPC reports chain_id={actual_chain_id}. Gateway payments would settle "
            "on a different chain than trades execute on."
        )
        if payments_dry_run:
            logger.warning("%s (PAYMENTS_DRY_RUN=true — not fatal, but fix before flipping it)", message)
            return
        raise GatewayChainMismatch(f"FATAL: {message} Refusing to boot with PAYMENTS_DRY_RUN=false.")


# ── Docs gate: disable /docs and /openapi.json in production ──────────
# Default OFF when PUBLIC_DOMAIN is set (production). Override with
# ENABLE_API_DOCS=1 to re-enable in any environment.
_enable_docs = os.getenv("ENABLE_API_DOCS", "").lower() in ("1", "true", "yes")
_is_production = bool(os.getenv("PUBLIC_DOMAIN"))
if _is_production and not _enable_docs:
    _docs_url = None
    _openapi_url = None
else:
    _docs_url = "/docs"
    _openapi_url = "/openapi.json"


class _MarketplaceUnavailable(Exception):
    """Sentinel: the marketplace engine did not start, so rehydration is skipped."""


# ── Process-wide default thread pool (chosen, never inherited) ───────────────
#
# Every `asyncio.to_thread` / `run_in_executor(None, ...)` in the backend shares
# ONE pool. Until now nothing called `set_default_executor`, so its width was
# whatever CPython picked: `min(32, os.cpu_count() + 4)` — 5 on a 1-vCPU task,
# 6 on 2. That pool is what `asset_market_service`, `traces_routes`,
# `chat_routes`, `portfolio_routes` and `strategies_routes` block on, and it is
# also what the debate proposer fan-out (up to DEBATE_POOL_MAX = 10 concurrent
# LLM calls) occupies for the length of a generation. 10 > 6, so serving work
# queued behind generation with nothing in the code choosing that trade-off.
#
# The proposer threads are IO-bound (blocked on an LLM socket, holding no GIL),
# so the correct answer for them is a pool wide enough to absorb the fan-out
# plus serving headroom. The CPU-bound half — the backtests — is NOT solved by
# widening; it is moved off this pool entirely onto the bounded, dedicated pool
# in agents/debate_engine.py. The two changes are complements.
_DEFAULT_EXECUTOR_FLOOR = 16


def _default_executor_workers() -> int:
    """Explicit width for the process-wide default thread pool.

    Floor of 16 because one generation pipeline parks up to ``DEBATE_POOL_MAX``
    (10) IO-bound proposer threads here for minutes; anything at or below that
    leaves zero threads for request-serving blocking calls.
    ``SERVER_THREAD_POOL_WORKERS`` overrides for a box where that is wrong.
    """
    try:
        override = int(os.getenv("SERVER_THREAD_POOL_WORKERS", "0"))
    except ValueError:
        override = 0
    if override > 0:
        return min(64, override)
    return min(32, max(_DEFAULT_EXECUTOR_FLOOR, (os.cpu_count() or 1) * 4))


def _install_default_executor(logger_: logging.Logger) -> concurrent.futures.ThreadPoolExecutor:
    """Bind an explicitly-sized pool as the running loop's default executor.

    Emits ONE INFO line carrying ``os.cpu_count()``, the width we chose, and the
    width CPython would have picked. That last number is the measurement: if
    ``cpu_count() + 4`` is at or below the 10-wide debate fan-out, executor
    exhaustion (not just GIL contention) was live on this task size.
    """
    cpu_count = os.cpu_count()
    width = _default_executor_workers()
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=width,
        thread_name_prefix="archimedes-default",
    )
    asyncio.get_running_loop().set_default_executor(executor)
    logger_.info(
        "startup: executor os.cpu_count()=%s default_executor_max_workers=%d "
        "(cpython_default_would_be=%d, override=SERVER_THREAD_POOL_WORKERS)",
        cpu_count,
        width,
        min(32, (cpu_count or 1) + 4),
    )
    return executor


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    """FastAPI lifespan context manager — startup before yield, shutdown after."""
    _logger = logging.getLogger("archimedes.startup")

    # ── STARTUP ──────────────────────────────────────────────────────────
    # 0. Choose the process-wide default executor before anything can use it.
    _app.state.default_executor = _install_default_executor(_logger)

    # 1. (removed) Rigor-gate backfill.
    #
    # This used to load every backtest_results row for every curated strategy
    # and then call evaluate_rigor_gate() to backfill DSR/PBO. Both halves were
    # wrong, and it was a primary cause of the 2026-08-19 OOM crash loop:
    #
    #   * The query was `.all()` over full rows. artifact_json and
    #     equity_curve_json are plain Text columns with no deferred loading
    #     (models/backtest_store.py), so every row's JSON blob was materialised
    #     — measured +1079 MB of RSS at 408 rows, strictly linear at ~2.6 MB
    #     per row, and rising daily because nothing dedupes that table.
    #   * Worse, `rows` was a local of THIS function, and lifespan() is an
    #     @asynccontextmanager that suspends at the `yield` below for the entire
    #     process lifetime. Python pins the frame, so those blobs were never
    #     collected — retained garbage for the life of the container, not a
    #     transient spike. `del rows; gc.collect()` in a probe returned 100% of it.
    #   * evaluate_rigor_gate() is a FastAPI route handler whose `strictness`
    #     parameter defaults to Query(DEFAULT_LEVEL). Called directly it stays a
    #     Query object, the whole cohort DSR/PBO computation runs (~14-20 s of
    #     one vCPU), and then StrategyRigorResult validation raises. It had
    #     therefore NEVER once succeeded since 2026-07-05 (commit 4cf8d59b) —
    #     46 days of burning that computation on every boot and discarding it.
    #
    # Nothing is lost by removing it. The rigor gate already runs where it
    # belongs: in the generation pipeline for generated strategies
    # (agents/generation_pipeline.py, via live_rigor_gate.verdict_from_returns),
    # and on demand for the curated library via GET /api/selection-bias/gate —
    # whose _compute() is the only production caller of update_rigor_gate_fields(),
    # i.e. the only thing that ever persisted these columns anyway.

    # 2. Seed papers table from manifest.jsonl (idempotent) — IN THE BACKGROUND.
    # This ran synchronously here until 2026-09-01, which was survivable at
    # 10,000 manifest rows and stopped being survivable at 18,907 (#1635): a
    # fresh prod task spent minutes bulk-seeding Aurora BEFORE binding, blew
    # the ALB health window, was killed mid-seed, and the replacement repeated
    # it — the rollout-crawl/502-flap incident of this date (failedTasks=2,
    # rollout budget exceeded). The seed is idempotent and every consumer
    # reads the papers TABLE (not the manifest), so nothing needs it to have
    # finished before the app serves: until it completes, the corpus is
    # merely yesterday's size, honestly reported by /health's corpus counts.
    # A worker thread, not a loop task: seed_from_manifest is synchronous
    # blocking DB work, and the whole point is keeping the serving loop free.
    def _seed_corpus_in_background() -> None:
        try:
            from archimedes.services.corpus_service import seed_from_manifest

            inserted = seed_from_manifest()
            if inserted > 0:
                _logger.info("startup: seeded %d new papers from manifest (background)", inserted)
            else:
                _logger.info("startup: corpus manifest already fully seeded (background check)")
        except Exception as exc:
            _logger.warning("startup: background corpus seed failed (non-fatal): %s", exc)

    import threading

    threading.Thread(target=_seed_corpus_in_background, name="corpus-seed", daemon=True).start()

    # 2a. Seed provider examples into strategy_store (idempotent, D1).
    # Each provider example gets a StrategyRecord keyed by its 32-char
    # provider id so the publish route can look it up. content_hash is a
    # domain-separated SHA-256 to avoid collision with real generated hashes.
    try:
        import hashlib
        import json

        from archimedes.db import get_session
        from archimedes.models.paper_assoc import paper_ref_to_assoc
        from archimedes.models.strategy_store import StrategyRecord
        from archimedes.services.strategy_provider import default_provider

        provider = default_provider()
        with get_session() as session:
            count = 0
            for s in provider.list_strategies():
                if session.query(StrategyRecord).filter_by(id=s.id).first():
                    continue
                content_hash = "0x" + hashlib.sha256(("example:" + s.id).encode()).hexdigest()
                # assoc/v1 (#1637) — same shape every other writer emits.
                papers_list = [paper_ref_to_assoc(p) for p in s.papers]
                record = StrategyRecord(
                    id=s.id,
                    content_hash=content_hash,
                    is_example=True,
                    generation_method="curated",
                    strategy_name=s.paper_title or s.id,
                    thesis=s.methodology_summary or "",
                    source_papers=json.dumps(papers_list),
                    asset_universe=json.dumps(s.asset_universe or []),
                    risk_profile=(s.risk_profiles[0] if s.risk_profiles else "moderate"),
                    status="live",
                    owner_wallet=None,
                )
                session.add(record)
                count += 1
            if count:
                session.commit()
                _logger.info("startup: seeded %d example strategies into strategy_store", count)
    except Exception as exc:
        _logger.warning("startup: example strategy seed failed (non-fatal): %s", exc)

    # 2b. Prime request-path caches BEFORE yield (#1713). Uvicorn is not
    # listening yet, so the ALB target cannot go healthy until the Library
    # page's cohort-returns + rigor caches are warm. Fail-soft: a timeout or
    # a raised warmup must not abort boot (the task serves cold, which is
    # the pre-#1713 behaviour, rather than never joining). The helper is
    # the indirection that keeps evaluate_rigor_gate / BacktestResultRecord
    # out of this frame — the 2026-08-19 OOM was this function pinning
    # artifact_json blobs at yield.
    try:
        from archimedes.services.request_path_warmup import arm_request_path_warmup

        await arm_request_path_warmup(_app)
        _logger.info("startup: request-path warmup armed")
    except Exception as exc:
        _logger.warning("startup: request-path warmup failed (non-fatal): %s", exc)

    # Both money-affecting switches FAIL SAFE (default to dry) and must be
    # turned on together and deliberately. Previously PAYMENTS_DRY_RUN
    # defaulted to "false" while PAPER_TRADING defaulted to "true", so an
    # out-of-the-box deploy mirrored no real trades yet charged real USDC —
    # the worst possible asymmetry. Charging real money now requires an
    # explicit PAYMENTS_DRY_RUN=false.
    #
    # Read BEFORE step 3's try block (not inside it) so it is unconditionally
    # bound going into step 3y below, regardless of whether step 3 itself
    # raises. It used to be assigned inside that try, after the MarketService
    # import and the AGENT_INTERVAL_SECONDS int() parse — either one raising
    # first (e.g. a non-numeric AGENT_INTERVAL_SECONDS) left this name
    # unbound, and step 3y's reference to it then raised a bare NameError
    # that its own `except Exception` swallowed as "connectivity issue?",
    # silently disabling the GATEWAY_CHAIN/RPC mismatch guard (#1240 review).
    payments_dry_run = os.getenv("PAYMENTS_DRY_RUN", "true").lower() in ("1", "true", "yes")

    # 3. Start the in-process marketplace engine (MarketService).
    # FAIL-SOFT: constructing the engine (or importing its deps, e.g. circlekit)
    # must NEVER take down the whole backend — a new subsystem crashing at
    # startup should degrade to "marketplace unavailable" (routes 503), not 502
    # the entire app. market stays None on failure and everything downstream
    # (rehydration, routes via _get_market, shutdown) guards on that.
    market = None
    try:
        from archimedes.marketplace.service import MarketService

        interval = int(os.getenv("AGENT_INTERVAL_SECONDS", "300"))
        paper_trading = os.getenv("PAPER_TRADING", "true").lower() in ("1", "true", "yes")
        market = MarketService(
            interval_seconds=interval, payments_dry_run=payments_dry_run, paper_trading=paper_trading
        )
        _app.state.market = market
        _logger.info(
            "marketplace engine started (interval=%ds, payments_dry_run=%s, paper_trading=%s)",
            interval,
            payments_dry_run,
            paper_trading,
        )
    except Exception as exc:
        _app.state.market = None
        _logger.error("startup: marketplace engine failed to start — running WITHOUT it (non-fatal): %s", exc)

    # 3y. Verify GATEWAY_CHAIN against the RPC we actually talk to (#1240).
    # Gated on marketplace_router (circlekit importable), not `market`
    # (MarketService construction) — generation_payment.py's paywall also
    # resolves GATEWAY_CHAIN independently of the ticking engine, so this
    # matters even when MarketService itself failed to start for some other
    # reason. A connectivity failure (RPC briefly unreachable at boot) is
    # logged and swallowed here — chain_client owns its own retry/backoff,
    # and that is a different failure mode than "pointed at the wrong
    # chain", which is the only thing _assert_gateway_chain_matches_rpc
    # itself treats as fatal (and only when PAYMENTS_DRY_RUN=false).
    if marketplace_router is not None:
        try:
            from archimedes.chain.client import chain_client
            from archimedes.marketplace.config import gateway_chain

            # Read through config.gateway_chain(), never a hand-rolled
            # os.getenv("GATEWAY_CHAIN", <the module default>): #1495 landed
            # that single accessor (and a structural test that forbids any
            # other reference to the default constant) precisely because a
            # hand-rolled getenv is how the revenue sweep silently stayed on
            # testnet. This boot check must resolve the chain the exact same
            # way the money paths do, or it verifies the wrong name.
            configured_chain = gateway_chain()
            await _assert_gateway_chain_matches_rpc(chain_client, configured_chain, payments_dry_run)
            _logger.info("startup: GATEWAY_CHAIN=%s verified against RPC", configured_chain)
        except GatewayChainMismatch:
            # Re-raise ONLY the dedicated mismatch type, by identity — not
            # "any RuntimeError". A plain RuntimeError from get_chain_id()
            # itself (e.g. a closed aiohttp session/event loop) must fall
            # through to the generic warning branch below instead of aborting
            # boot; catching RuntimeError broadly here used to make it fatal
            # regardless of PAYMENTS_DRY_RUN (#1240 review finding).
            raise
        except Exception as exc:
            _logger.warning("startup: GATEWAY_CHAIN/RPC verification skipped (connectivity issue?): %s", exc)

    # 3z. Register the system/agent addresses in controlled_wallets (issue
    # #1028, D1a/D3). Idempotent upsert — safe to run on every boot.
    #   - The trading-agent signer (chain_client.settings.agent_account): the
    #     SAME key both StrategyRunner (rebalance) and MarketService
    #     (createPool / on-chain settlement) sign with — one row covers both
    #     "agent_runner" and "the marketplace engine" from the issue's scope.
    #   - ARCHIMEDES_TREASURY_WALLET: the platform's revenue-split address
    #     (marketplace_routes.publish_strategy) — never a user identity.
    # Circle DCWs (publisher_settlement / subscriber_payment) are registered
    # per-wallet at provision time (marketplace_routes.py), not here — there's
    # no fixed set of those to enumerate at boot.
    try:
        from archimedes.chain.client import chain_client
        from archimedes.services.identity_events import register_controlled_wallet

        agent_account = chain_client.settings.agent_account
        if agent_account is not None:
            register_controlled_wallet(address=agent_account.address, wallet_class="trading_agent")

        treasury_wallet = os.getenv("ARCHIMEDES_TREASURY_WALLET", "").strip()
        if treasury_wallet:
            register_controlled_wallet(address=treasury_wallet, wallet_class="treasury")
    except Exception as exc:
        _logger.warning("startup: controlled-wallet registration failed (non-fatal): %s", exc)

    # 3a. Rehydrate running publishers from Postgres. Guarded on the engine
    # having started; the surrounding try/except is fail-soft regardless.
    try:
        if market is None:
            raise _MarketplaceUnavailable

        from archimedes.db import get_session
        from archimedes.marketplace.service import Subscriber
        from archimedes.models.marketplace import MarketplaceAgent

        with get_session() as session:
            publishers = (
                session.query(MarketplaceAgent)
                .filter(MarketplaceAgent.role == "publisher", MarketplaceAgent.status == "running")
                .all()
            )
            strategy_ids = [row.strategy_id for row in publishers]
            subscriber_rows = (
                session.query(MarketplaceAgent)
                .filter(
                    MarketplaceAgent.role == "subscriber",
                    MarketplaceAgent.status == "running",
                    MarketplaceAgent.strategy_id.in_(strategy_ids),
                )
                .all()
                if strategy_ids
                else []
            )

        # Group subscriber rows by strategy_id
        subscribers_by_strategy: dict[str, dict[str, Subscriber]] = {}
        for srow in subscriber_rows:
            if not srow.circle_wallet_id:
                _logger.warning(
                    "rehydrate: subscriber %s has NULL circle_wallet_id — marking inactive (fail closed, legacy row)",
                    srow.sub_id,
                )
                subscriber_active = False
            else:
                subscriber_active = not srow.halted
            subscribers_by_strategy.setdefault(srow.strategy_id, {})[srow.sub_id] = Subscriber(
                sub_id=srow.sub_id,
                pool_id=srow.pool_id,
                vault_address=srow.vault_address,
                ephemeral_wallet=srow.ephemeral_wallet,
                subscriber_wallet=srow.subscriber_wallet,
                active=subscriber_active,
                circle_wallet_id=srow.circle_wallet_id or "",
            )

        for row in publishers:
            subs = subscribers_by_strategy.get(row.strategy_id, {})
            if not row.gateway_seller_address:
                _logger.error(
                    "rehydrate: publisher %s has NULL gateway_seller_address — "
                    "skipping (fail closed, legacy row). Set a gateway_seller_address "
                    "on the publisher row before restarting.",
                    row.strategy_id,
                )
                continue
            await market.start_publisher(
                strategy_id=row.strategy_id,
                pool_id=row.pool_id,
                vault_address=row.vault_address,
                creator_wallet=row.creator_wallet,
                gateway_seller_address=row.gateway_seller_address,
                agent_wallet_id=row.agent_wallet_id or "",
                subscribers=subs,
            )
            _logger.info(
                "rehydrated publisher %s (vault=%s, %d subscribers from Postgres)",
                row.strategy_id,
                row.vault_address,
                len(subs),
            )
    except _MarketplaceUnavailable:
        _logger.info("startup: marketplace engine not running — skipping publisher rehydration")
    except Exception as exc:
        _logger.warning("startup: publisher rehydration failed (non-fatal): %s", exc)

    # 4. Arm the paper-trading advance scheduler. MUST live in this lifespan:
    # passing a custom lifespan= makes Starlette silently skip any
    # @app.on_event("startup") handlers, so anything not started here does
    # not start at all. Fail-soft: never blocks startup.
    #
    # There is deliberately NO backtest refresh here. A backtest is a frozen
    # artifact of evidence against a stated data window, produced once — at
    # generation for a generated strategy, by an explicit operator run for a
    # curated one (docs/runbooks/curated-backtests.md). The in-app
    # staleness/age-driven refresh loop that used to be armed on this line was
    # deleted with #1760: it re-ran the whole curated library in the serving
    # process at +180s on EVERY cold boot, pegged the 1-vCPU task, and got
    # tasks killed by their own container health check during the 2026-09-01
    # deploy. Do not reintroduce a clock or a boot hook here — the policy is
    # docs/adr/backtests-are-frozen-evidence.md and the guard is
    # backend/tests/test_backtests_are_frozen.py.
    try:
        # Paper-trading ledgers advance daily: one appended bar per deployment
        # per day, replayed on the graded engine (services/paper_trading.py).
        # MUST NOT be create_task(paper_advance_loop()) in this process — a C
        # abort in psycopg2/web3 on that tick (#1632) kills the interpreter
        # that serves /health. Isolation is arm_paper_advance_for_web_tier:
        # spawn a child, or refuse. It reads PAPER_ADVANCE_ENABLED itself, so
        # arming it here is unconditional.
        #
        # Hold a reference (RUF006): a fire-and-forget task can be garbage-
        # collected mid-run. Parked on app.state so it lives for the app's
        # lifetime instead of being reaped once this function returns.
        from archimedes.services.paper_trading import arm_paper_advance_for_web_tier

        _app.state.paper_advance_task = asyncio.create_task(arm_paper_advance_for_web_tier())
        _logger.info("startup: paper-trading advance scheduler armed (isolated child; in-process loop refused)")
        # Trace coverage is a claim the product makes ("auditable reasoning
        # behind every move"), so publishing being off is announced once at
        # boot rather than only discovered per-deployment (#1575 §7).
        from archimedes.services.paper_trace import publishing_enabled

        if not publishing_enabled():
            _logger.error(
                "startup: PAPER_TRACE_PUBLISH is OFF — paper deployments will make decisions with "
                "NO published reasoning trace. Every such decision is recorded as a durable gap and "
                "surfaced as trace_coverage.status='disabled'; the product's provenance claim does "
                "not hold while this is off."
            )
    except Exception as exc:
        _logger.warning("startup: paper-advance scheduler failed to arm (non-fatal): %s", exc)

    # Platform revenue sweep (services/revenue_sweep.py): opt-in Gateway →
    # DCW-token withdrawal loop. Money-switch convention: only the literal
    # REVENUE_SWEEP_ENABLED=true arms it; unset/anything-else stays off and
    # says so. Same RUF006 app.state reference rule as the loops above.
    try:
        from archimedes.services.revenue_sweep import revenue_sweep_loop, sweep_enabled

        if sweep_enabled():
            _app.state.revenue_sweep_task = asyncio.create_task(revenue_sweep_loop())
            _logger.info("startup: revenue sweep scheduler armed")
        else:
            _logger.info("startup: revenue sweep scheduler disabled (REVENUE_SWEEP_ENABLED != 'true')")
    except Exception as exc:
        _logger.warning("startup: revenue sweep failed to arm (non-fatal): %s", exc)

    yield  # ── app is now running ────────────────────────────────────────

    # ── SHUTDOWN ─────────────────────────────────────────────────────────
    # Stop the paper-advance arming task FIRST, because it owns a child
    # interpreter and a child is not reaped by this process's SIGTERM. Without
    # this cancel, a task draining out of a deploy leaves its paper-advance
    # child ticking against the same ledger rows the replacement task's child
    # is starting on. stop_paper_advance_task tolerates None and never raises.
    try:
        from archimedes.services.paper_trading import stop_paper_advance_task

        await stop_paper_advance_task(getattr(_app.state, "paper_advance_task", None))
    except Exception as exc:  # shutdown must not raise
        _logger.warning("shutdown: paper-advance task did not stop cleanly (%s: %s)", type(exc).__name__, exc)

    market = getattr(_app.state, "market", None)
    if market is not None:
        market._stop.set()
        strategy_ids = list(market.publishers.keys())
        if strategy_ids:
            await asyncio.gather(
                *(market.stop_publisher(sid) for sid in strategy_ids),
            )
        _logger.info("marketplace engine stopped")


app = FastAPI(
    title="Archimedes",
    description="Research-grounded strategy generation with a visible rigor gate on Arc public testnet.",
    version="0.1.0",
    docs_url=_docs_url,
    openapi_url=_openapi_url,
    lifespan=lifespan,
)

# Wire rate limiter into the app state
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# Custom handler returns JSON 429 with rate-limit headers
@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):  # noqa: ARG001 — FastAPI exception_handler signature requires request
    """Return 429 JSON with X-RateLimit-* headers."""
    response = JSONResponse(
        status_code=429,
        content={"detail": "Rate limit exceeded. Please slow down and try again later."},
    )
    # slowapi populates these headers on the response via the extension point;
    # we ensure they're forwarded even when we override the handler.
    if hasattr(exc, "detail"):
        response.headers["X-RateLimit-Limit"] = str(getattr(exc, "limit", ""))
    return response


# Allow the Next.js frontend to call the API during development
# Production: restricted to PUBLIC_DOMAIN env var (Issue #178).
# Local dev: CORS_ORIGINS env var (defaults to localhost origins).
import os as _os

_cors_env_origins = _os.getenv("CORS_ORIGINS", "http://localhost:3000,http://localhost:80")
_public_domain = _os.getenv("PUBLIC_DOMAIN", "https://archimedes-arc.com")
_cors_origins = [o.strip() for o in _cors_env_origins.split(",") if o.strip()]
# In production (when PUBLIC_DOMAIN is set), restrict to that domain.
# Local dev keeps the CORS_ORIGINS list (localhost).
if _public_domain and _public_domain not in _cors_origins:
    _cors_origins.append(_public_domain)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-Wallet-Address",
        "X-Internal-Agent-Key",
        "X-Requested-With",
    ],
    max_age=600,
)

# ── Fail-closed: require EMAIL_ENCRYPTION_KEY in production ──────────
# Without this, services/email_crypto.py falls back to a hardcoded secret
# that anyone with repo access can use to decrypt stored emails.
if _is_production and not os.getenv("EMAIL_ENCRYPTION_KEY"):
    raise RuntimeError(
        "FATAL: EMAIL_ENCRYPTION_KEY must be set when PUBLIC_DOMAIN is configured. "
        'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(32))"'
    )


# ── Request body size limit middleware ────────────────────────────────
_MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MB


@app.middleware("http")
async def _limit_request_body(request: Request, call_next):
    """Reject request bodies larger than _MAX_BODY_BYTES (1 MB)."""
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > _MAX_BODY_BYTES:
        return JSONResponse(status_code=413, content={"detail": "Request body too large (max 1 MB)"})
    return await call_next(request)


# ── Timing middleware: per-request duration + slow-request log (issue #1436) ──
# The ALB reports p50 46ms against p99 16.8s, but its metrics are per-target,
# not per-route, so which endpoint owns the tail was not answerable from logs.
# Tags every response with X-Response-Time-Ms and logs the ones over
# SLOW_REQUEST_MS. SSE streams are exempt — they legitimately run 300s+, which
# is the same contamination that forced the latency alarm off p95 on 2026-08-21.
from archimedes.api.timing_middleware import timing_middleware

app.middleware("http")(timing_middleware)


# ── Telemetry middleware: human-vs-agent traction counter (issue #428) ──
# Registered AFTER _limit_request_body and BEFORE include_router. It classifies
# each request (Better Auth account → human; internal-key / bot-UA → agent), increments
# the matching Redis counter, and tags the response with X-Telemetry-Agent.
# Graceful-degrades on any error — telemetry never turns a request into a 5xx.
from archimedes.api.telemetry_middleware import telemetry_middleware

app.middleware("http")(telemetry_middleware)


# ── Visitor-id middleware: anonymous funnel attribution (issue #787) ────
# Registered LAST so it is the OUTERMOST middleware: it guarantees every request
# carries a stable anonymous `archimedes_vid` cookie and exposes it as
# request.state.visitor_id BEFORE any route or downstream middleware runs. This
# is what lets the conversion funnel (landed → generation_started →
# wallet_connected → vault_deployed) join stages for the same visitor. Fail-safe
# — never turns a request into a 5xx.
from archimedes.api.funnel_middleware import ensure_visitor_id_middleware

app.middleware("http")(ensure_visitor_id_middleware)

# Resolve Better Auth once per request. Protected route dependencies consume
# request.state.current_user; public routes keep working when no session exists.
app.middleware("http")(better_auth_session_middleware)


# Initialize database (creates any tables the ORM declares but migrations have
# not yet created — `vault_metadata`, `chat_messages`, …).
#
# THIS IS THE ONLY SCHEMA ASSERTION THE WEB PROCESS MAKES (#1818 P6). Until
# 2026-09-03 eleven request handlers also called `init_db()` so their
# transitional columns would exist before they read — `paper_routes` (5 sites),
# `selection_bias_routes` (2), `leaderboard_routes`, `strategies_routes` (2) and
# `services/live_rigor_gate`. Every one of them was reached from an `async def`
# endpoint with no threadpool hop, so ordinary API traffic issued
# `ALTER TABLE … ADD COLUMN IF NOT EXISTS` on the event loop: a page load could
# queue an AccessExclusiveLock behind a long transaction and put every later
# reader of that table behind it. That is the wedge shape of the 2026-09-03
# outage, still reachable after #1819 bounded the wait. Schema now belongs to
# the migrate task (`alembic upgrade head`) plus this one call, which runs
# before the first request is served.
#
# It stays at IMPORT rather than moving into `lifespan()`, and that is a
# deliberate non-change, not an oversight: P6 is about removing DDL from the
# REQUEST path, and relocating the boot call is a separate decision with its own
# ordering constraint (it has to precede the lifespan's manifest seed, which
# writes `papers`). What makes the import-time position acceptable today is
# #1819: every patch statement takes a 5s `lock_timeout` and the whole call a
# 10s budget, so a contended boot degrades with a WARNING instead of hanging for
# 91 minutes the way the replacement tasks did on 2026-09-03.
init_db()


# NOTE: startup work lives in lifespan() above. Do NOT add
# @app.on_event("startup") handlers here — with a custom lifespan= passed to
# FastAPI, Starlette silently skips them (verified on fastapi 0.138.1), so
# they would be dead code that LOOKS like it runs.


# Wire all routers
app.include_router(assets_router)
app.include_router(agent_manifest_router)
app.include_router(vaults_router)
app.include_router(strategies_router)
app.include_router(traces_router)
app.include_router(regime_router)
app.include_router(swap_router)
app.include_router(config_router)
app.include_router(agent_router)
app.include_router(corpus_router)
app.include_router(paper_router)
app.include_router(explore_router)
app.include_router(generate_router, dependencies=[Depends(require_current_user)])
app.include_router(generate_public_router)  # /quote only — public by design
if marketplace_router is not None:
    app.include_router(marketplace_router)
app.include_router(risk_router)
app.include_router(portfolio_router, dependencies=[Depends(require_current_user)])
app.include_router(selection_bias_router)
app.include_router(rigor_verify_router)
app.include_router(account_usage_router)
# Key management is session-gated inside the router (an API key cannot manage
# API keys — see account_auth.require_session_credential), so it is deliberately
# NOT wrapped in a router-level require_current_user: the stricter dependency
# already lives on each route and a second one here would only obscure it.
app.include_router(api_key_router)
app.include_router(payment_router)
app.include_router(papers_router)
app.include_router(user_router, dependencies=[Depends(require_current_user)])
app.include_router(wallet_router)
# Legacy SIWE router remains test-only so signature-verification regression
# tests exercise its reusable proof helpers without exposing wallet login live.
if os.getenv("TESTING"):
    from archimedes.api.auth_siwe import auth_router

    app.include_router(auth_router)
app.include_router(proposals_router, dependencies=[Depends(require_current_user)])
app.include_router(features_router)
app.include_router(metrics_router)
app.include_router(metrics_private_router)
app.include_router(leaderboard_router)


# ── Liveness responses must never be cached (issue #1520) ──────────────
# /health matched no CloudFront ordered_cache_behavior, so it fell through to
# the default one (infra/cloudfront.tf) whose `html` policy has default_ttl=60.
# With no Cache-Control from here, CloudFront cached it: measured `x-cache: Hit`
# with `age: 17` on the live site, and — worse — cached 504s coming back in
# 0.07s, far too fast to have reached the origin. A cached health check is not a
# health check: it keeps reporting "ok" for up to a minute after the origin
# starts failing, which is the plausible-substitute failure this codebase treats
# as the primary defect class. The CloudFront behaviour is the other half of the
# fix and needs a terraform apply; this half travels with the app, so it holds
# wherever the app is deployed and whoever sits in front of it.
_NO_STORE = "no-store, no-cache, must-revalidate, max-age=0"


def _no_store(response: Response) -> None:
    """Mark a liveness response uncacheable by any intermediary."""
    response.headers["Cache-Control"] = _NO_STORE
    response.headers["Pragma"] = "no-cache"


def _no_store_headers() -> dict[str, str]:
    """Same headers, for handlers that build their own Response object.

    A handler that returns a JSONResponse directly never touches the injected
    ``response``, so it needs these passed in explicitly — the AMM probe returns
    503s that way, and an uncacheable 200 with a cacheable 503 is the worse half
    to get wrong.
    """
    return {"Cache-Control": _NO_STORE, "Pragma": "no-cache"}


# ── /health's outbound budget (issue #1592) ───────────────────────────────
# INCIDENT 2026-08-31: the two outbound probes below were awaited with no
# deadline of their own. With the Arc RPC unreachable from inside the VPC they
# parked; /health blew the ALB's 5s check and ECS's container HEALTHCHECK; no
# new task ever turned healthy; the rollout wedged at 1/2 for its full 1200s
# budget while the serving task's event loop starved every other route. The
# same RPC answered in 0.1s from outside the VPC — nothing was slow, the calls
# were unbounded.
#
# Both probes now run CONCURRENTLY under hard budgets, so the bounded outbound
# section costs max(these two), not their sum. See services/health_cache.py for
# the last-known-value contract and archimedes/deadline.py (plus
# chain/client.py's BoundedAsyncHTTPProvider) for why a plain asyncio.wait_for
# was not enough.
#
# WHY 1.2s AND NOT THE 5s THE ALB ALLOWS. The budget has to cover the probe AND
# leave room for the rest of this handler, and the case that matters is a COLD
# task — the one whose failing check wedges a rollout. Measured on this
# handler's first call in a fresh process (corpus load, strategy-file scan,
# risk-data probe): ~0.5s of non-outbound work, ~0.15s once warm. 1.2 + 0.5 is
# comfortably inside the 2s the endpoint promises and the 5s the ALB and the
# ECS container HEALTHCHECK both cut at, and it is still ~12x the 0.1s the live
# Arc RPC answers in. Raising these numbers is how the promise gets lost, so
# the guard in backend/tests/test_health_always_answers.py asserts the 2s.
_CHAIN_PROBE_BUDGET_SECONDS = 1.2
# The outer backstop for the oracle probe. Deliberately LARGER than the inner
# budget below so the inner, better answer wins the race: oracle_health already
# knows how to report its own deadline overrun as an honest `probe_timeout`
# reason with the probed/universe counts intact. This outer bound exists only
# for stalls the probe's own wait_for cannot see — contract-loader
# construction, push-set derivation, or web3's uncancellable session-manager
# lock (the incident's actual shape).
_ORACLE_PROBE_BUDGET_SECONDS = 1.2
# Passed INTO oracle_health, replacing its default 1.5s. That default was sized
# against a /health that spent up to 3s on is_connected() before the probe even
# started; probes now run concurrently, so the oracle's slice is smaller and
# has to leave the outer backstop above room to be the backstop.
_ORACLE_INNER_BUDGET_SECONDS = 0.9
# Budget for the six LOCAL reads (corpus file, corpus DB rows, corpus meta,
# paper-RAG, GMM regime, risk data). Smaller than the chain/oracle budgets
# because none of these leaves the box: the corpus load is a file read, the two
# corpus-meta reads are single-row queries, and the three health functions are
# in-process state checks. Measured warm they are single-digit milliseconds.
#
# WHY THEY NEED A BUDGET AT ALL (#1594). "Local" is not "fast" when the box is
# the problem: an Aurora failover parks `get_paper_count`, a cold
# sentence-transformer import parks `paper_rag_health`, an EFS/S3-backed corpus
# file parks `load_corpus`. Measured on the live handler, /health was p50 1.19s
# but p95 17.03s / max 30s against an ALB check of timeout 10 x threshold 5 —
# five consecutive misses kill the task, and over 24h HealthyHostCount averaged
# 1.03 and touched 0. #1592 bounded the two OUTBOUND probes; these six ran
# unbounded and synchronously afterwards, which is where the p95 lived.
#
# They share this budget CONCURRENTLY with the two probes above, so the bounded
# section of this handler costs max(1.2, 0.8) = 1.2s, not the sum of eight.
_LOCAL_PROBE_BUDGET_SECONDS = 0.8

# One name per local probe, defined once because it is used three ways that MUST
# agree: the HealthProbeCache key, the `<name>_probe_*` payload prefix, and the
# payload field the trio describes. Drift between them would publish staleness
# fields that label a different reading than the one they sit next to.
_CORPUS_PROBE = "corpus"
_CORPUS_DB_PROBE = "corpus_db"
_CORPUS_META_PROBE = "corpus_meta"
_PAPER_RAG_PROBE = "paper_rag"
_REGIME_PROBE = "regime_detector"
_RISK_DATA_PROBE = "risk_data"

# The health probes get their OWN thread pool, not the loop's default executor.
# ``asyncio.to_thread`` would have been shorter and is wrong here: the default
# executor is also where asyncio runs ``getaddrinfo``, so abandoned health reads
# accumulating in it would eventually make DNS — for the DB, for Redis, for the
# RPC — queue behind a stuck corpus load. A liveness probe must not be able to
# damage the thing it reports on.
#
# 12 workers = two full checks' worth of the six probes. A read abandoned at its
# budget keeps its worker until it unwinds (see archimedes/deadline.py on the
# cost of abandonment), so the headroom is what stops ONE permanently-stuck read
# from starving its five healthy siblings on the very next check. If the pool
# does fill, ``run_in_executor`` queues without blocking and the queued probes
# report ``probe_timeout`` against their last-known values — degraded and
# labelled, never a stalled handler.
_HEALTH_PROBE_EXECUTOR = ThreadPoolExecutor(max_workers=12, thread_name_prefix="health-probe")


def _bounded_local_read(fn):
    """Adapt a BLOCKING local read into a factory ``HealthProbeCache.probe`` can bound.

    A worker thread, not a bare coroutine, and that is the entire point.
    ``asyncio.wait_for`` / ``run_with_deadline`` schedule their timeout as a
    callback ON THE EVENT LOOP, so neither can bound work that is *itself*
    blocking the loop — a budget denominated in loop time is not a budget when
    the loop is what stalled. Moving the call off the loop is what makes the
    deadline real: the loop stays free to fire the timeout and answer the ALB
    while the stalled read is still parked.

    Cost, stated plainly (mirrors archimedes/deadline.py's abandonment note): a
    read that blows its budget is ABANDONED, and its worker thread keeps running
    until it unwinds. That is only acceptable because every read passed here is
    READ-ONLY. Do not route a state-changing call through this helper.
    """
    return lambda: asyncio.get_running_loop().run_in_executor(_HEALTH_PROBE_EXECUTOR, fn)


def _cache_annotated(outcome, reason: str) -> str:
    """Fold a served-from-cache label into the ``*_reason`` field operators read.

    Same wording the oracle block below produces by hand: the sibling
    ``*_probe_state`` field already says ``stale_cached``, but the human-read
    reason string has to say it too or a past reading gets quoted as a present
    one.
    """
    if outcome.state == "stale_cached":
        return f"{outcome.reason}; last completed read {outcome.age_s}s ago: {reason}"
    return reason


def _probe_error_fields(prefix: str, exc: BaseException) -> dict[str, object]:
    """Staleness trio for a probe that RAISED — an error, never a timeout.

    Kept distinct from ``probe_timeout`` for the reason services/health_cache.py
    documents: collapsing them lets a broken probe hide behind "the network was
    slow".
    """
    return {
        f"{prefix}_probe_state": "probe_error",
        f"{prefix}_probe_age_s": None,
        f"{prefix}_probe_reason": f"{prefix} probe_error: {exc}",
    }


# The LLM backend probe. `make_llm_backend()` looks like pure construction and
# is not: on the ollama path — the local-mode default this budget exists for
# (#1044) — its `available` check is a SYNCHRONOUS `httpx.get({LLM_BASE_URL}
# /api/tags)` with a 3.0s timeout, i.e. longer than /health's entire 2s promise.
# Until this constant existed that call sat outside the cache and outside any
# deadline, running unwrapped inside the async handler: with LLM_PROVIDER=ollama
# and ollama down, every /health parked the whole event loop for 3s, on exactly
# the path the ECS container HEALTHCHECK and the ALB target-group check hammer.
# That is the #1592 incident's shape with a different dark endpoint, and the
# handler's own docstring ("every outbound probe is bounded") was false while it
# stood. Kept under the chain/oracle budgets because the honest local answer
# (`/api/tags` off loopback) returns in single-digit milliseconds — anything
# slower is a dark endpoint, not a busy one.
_LLM_PROBE_BUDGET_SECONDS = 1.0


def _paper_rag_read():
    """The paper-RAG probe's read.

    Module scope, not a closure inside ``health``, because ``/health`` and
    ``/health/ready`` both run it (#1818 P3) and they have to run the SAME read
    — two copies would drift and then disagree about the same task.

    Imported INSIDE the worker thread, not at module import, so a cold
    sentence-transformer import is inside the budget too — the import is the
    slow part on a fresh task. Tests keep patching
    ``services.paper_rag.paper_rag_health`` exactly as they do today.
    """
    from archimedes.services.paper_rag import paper_rag_health as _prag_health

    return _prag_health()


def _regime_read():
    """The GMM regime-detector probe's read (late import: see _paper_rag_read)."""
    from archimedes.services.gmm_regime_detector import gmm_regime_health

    return gmm_regime_health()


def _risk_data_read():
    """The risk-data probe's read (late import: see _paper_rag_read)."""
    from archimedes.api.risk_routes import risk_data_health

    return risk_data_health()


# ─────────────────────────── READINESS (#1818 P3) ───────────────────────────
#
# INCIDENT 2026-09-03. Two paper-advance children ran ``init_db()``'s DDL
# patches concurrently; one child's ``AccessExclusiveLock`` request queued
# behind its sibling's open cycle transaction, and every later reader of
# ``papers`` / ``strategy_store`` queued behind THAT. From 03:32Z to 13:28Z
# ``/health`` answered 200 in ~1.05s on both tasks — correctly, by its own
# contract: it reports what it knows, and what it knew was a set of
# ``stale_cached`` readings, each honestly labelled, whose age reached 35,797s.
# ECS and the ALB stayed green for ten hours over a fleet that could not read
# its own database. Liveness was TRUE the whole time (the loop was turning).
# Readiness was false and was represented nowhere, so nothing could act on it.
#
# This is the missing half, and it is deliberately a SECOND endpoint rather
# than a status code on the first:
#
#   /health        ALB target-group check (infra/alb.tf: path /health,
#                  matcher "200") + the human/monitor payload. UNCHANGED —
#                  still always 200, still carrying the labelled stale state,
#                  now also publishing the readiness verdict as data. The ALB
#                  check acts on EVERY target at once, so a shared transient
#                  (an Aurora failover, an RPC blip) must not be able to route
#                  through it; that is the N2 argument in the chain block below
#                  and it is unchanged here.
#   /health/ready  the CONTAINER health check (backend/Dockerfile HEALTHCHECK,
#                  mirrored in infra/ecs.tf so the ECS agent can see it). 503
#                  when this task has been serving cached DB readings for
#                  longer than HEALTH_STALE_UNREADY_S.
#
# Why the container check and not the ALB check. Failing the ALB check pulls a
# target out of rotation IMMEDIATELY, and a shared cause pulls all of them at
# once — that is literally the 13:29Z line of the incident (HealthyHostCount=0,
# 504s to the only user of the day). Failing the container check hands the task
# to the ECS scheduler, which is bound by
# `deployment_minimum_healthy_percent = 100` (infra/ecs.tf): it brings a
# replacement up and healthy BEFORE draining the wedged one. Same verdict, and
# the response to it is proportionate instead of fleet-wide.
#
# The two consumers therefore keep their existing semantics and only the
# container check gains the new verdict.

# The probes whose staleness means "this task can no longer read its database".
# Four are literal DB reads: ``count_corpus_papers`` and ``get_paper_count`` are
# scalar COUNTs on ``papers`` — the very table the incident's lock chain wedged —
# ``get_corpus_meta`` is a single-row read, and ``risk_data_health`` goes through
# the strategy provider's persisted-backtest load. ``paper_rag_health`` is NOT a
# DB read (process state plus a weights stat) and is in the set anyway, on the
# incident's own evidence: it went ``stale_cached`` in the same 03:32:08-03:32:19
# window as the other three, because the shared probe executor filled with
# blocked DB reads — so its staleness is a second, independent witness to the
# same wedge.
#
# ``regime_detector`` and the two outbound probes (chain, oracle) are NOT here.
# A dark Arc RPC is not a reason to destroy a task that is serving pages, and
# the GMM probe reads a fitted artifact off disk — neither one says anything
# about whether this process can still reach Postgres.
#
# The value is each probe's ``absent`` — what /health reports when the probe
# misses AND nothing was ever cached. Restated here (identically) rather than
# left to the two call sites to keep in step.
#
# Note what the reason is NOT. ``absent`` never enters the shared memo, so a
# divergent one could not leak from one endpoint to the other through it:
# ``HealthProbeCache.probe`` RETURNS ``absent`` on a miss with nothing stored,
# and only a COMPLETED read reaches ``self._entries`` (services/health_cache.py).
# The reason is plainer, and it is about what a reader is told rather than about
# cache state: two endpoints answering the same question must publish the same
# "we do not know" value, and neither of them may publish an optimistic one.
_READINESS_ABSENT: dict[str, object] = {
    _CORPUS_PROBE: 0,
    _CORPUS_DB_PROBE: 0,
    _CORPUS_META_PROBE: None,
    _PAPER_RAG_PROBE: None,
    _RISK_DATA_PROBE: None,
}
_READINESS_PROBES = tuple(_READINESS_ABSENT)

# 900s = 30 consecutive misses of the ALB's 30s /health poll. Sized to be
# unreachable by anything transient: every probe in the set above is a bounded
# LOCAL read with a 0.8s budget (_LOCAL_PROBE_BUDGET_SECONDS), so a quarter of
# an hour of them never once completing is not a slow dependency — it is a task
# that has stopped being able to read. In the incident the last completed reads
# were at ~03:32Z, so this bar would have been passed at ~03:47Z — 9h41m before
# the first user request of the day at 13:28Z, and ~11h15m before the OOM kill
# that actually broke the wedge.
_DEFAULT_STALE_UNREADY_SECONDS = 900.0

_STALE_UNREADY_ENV = "HEALTH_STALE_UNREADY_S"


def _stale_unready_threshold_s() -> float:
    """Seconds of continuous ``stale_cached`` before this task calls itself unready.

    Read per request, not memoised at import, so the value follows a task
    restart with a different env instead of requiring a build.

    ``HEALTH_STALE_UNREADY_S=0`` (or negative) DISABLES the rule: the endpoint
    then always answers 200 and reports the same labelled states /health does.
    That escape hatch is not decoration. The cost of getting this threshold
    wrong is a task replaced once per threshold-length interval, forever, and an
    operator has to be able to stop that with an env change and a restart rather
    than a code change and an image build. Setting it on the live task
    definition does exactly that and holds until the next deploy; because
    ``.github/scripts/ecs_rewrite_task_def.py`` re-pins the name on every deploy
    (#1799 — terraform no longer writes container settings at all), the DURABLE
    pull-back is ``HEALTH_STALE_UNREADY_VALUE = "0"`` in that script. A value
    that will not parse falls back to the default and says so — silently
    disabling a safety rule because someone typo'd a number is the worse
    failure.
    """
    raw = os.getenv(_STALE_UNREADY_ENV, "").strip()
    if not raw:
        return _DEFAULT_STALE_UNREADY_SECONDS
    try:
        parsed = float(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not a number — falling back to the %.0fs default",
            _STALE_UNREADY_ENV,
            raw,
            _DEFAULT_STALE_UNREADY_SECONDS,
        )
        return _DEFAULT_STALE_UNREADY_SECONDS
    return max(0.0, parsed)


def _stale_unready_probes(outcomes: dict[str, object], threshold_s: float) -> list[tuple[str, float]]:
    """``(name, age_s)`` for every readiness probe serving a reading older than the threshold.

    ANY of them, not all of them. These five share one database and one probe
    executor, so in the incident they did go stale together — but the rule has
    to fire when only the DB-backed half does. Requiring unanimity would let a
    wedged database hide behind ``paper_rag``, the one member of the set that
    can keep answering with Postgres gone.

    The cost of ANY, stated plainly: one permanently stuck probe replaces this
    task roughly once per threshold interval, forever. That is loud in the ECS
    event log, every replacement logs HEALTH_READINESS_STALE naming the probe,
    and an operator stops it with ``HEALTH_STALE_UNREADY_S=0``. The cost of ALL
    is another ten-hour outage nobody is told about.

    ``risk_data`` is the member of the set whose cost GROWS WITH THE DATA, and
    it is named here so nobody has to rediscover it during an incident. Four of
    the five are scalar COUNTs or a single-row read; ``risk_data_health()``
    (api/risk_routes.py) is ``list_strategies()`` followed by one
    ``get_backtest_result`` per strategy — an N+1 — under the same shared 0.8s
    ``_LOCAL_PROBE_BUDGET_SECONDS``. It also catches every exception internally,
    so it can never be ``probe_error``: past the budget it goes ``stale_cached``
    and STAYS there, its age growing without bound, and this rule then replaces
    every backend task about once per threshold interval on a fleet that is
    serving fine. Prod as of 2026-09-03 is nowhere near it (34 strategies, 30
    persisted backtests, the probe ``live``), so this is a tripwire and not a
    defect today.

    The tripwire: ``risk_data_probe_state`` on /health sitting persistently at
    ``stale_cached`` while the other four stay ``live``. That shape is the N+1
    outgrowing its budget, NOT a wedged database — the fix is to bound or drop
    the per-strategy read, not to raise the threshold. If it has to be stopped
    before then, ``risk_data`` is the one member of ``_READINESS_PROBES`` that
    can be removed without weakening the incident's own coverage; the other four
    read the exact tables (``papers``, ``strategy_store``) the DDL wedged.

    Only ``stale_cached`` counts, and that is a deliberate line:

    * ``probe_timeout`` — the probe missed and nothing was EVER cached. That is
      the COLD-START shape (the cache is process-local and empty at boot), and
      replacing a task for not yet having completed its first read turns a slow
      boot into a replacement loop. A task that has never read is handled by
      the container check's ``startPeriod``, not by this rule.
    * ``probe_error`` — the probe RAISED. That is a defect in the probe, not a
      verdict about the database (services/health_cache.py draws the same line
      for the same reason), and a broken probe must not be able to take the
      fleet down.
    * ``live`` — a completed read. Whatever else is wrong, this task can read.

    ``threshold_s <= 0`` disables the rule entirely (see
    ``_stale_unready_threshold_s``).
    """
    if threshold_s <= 0:
        return []
    stale: list[tuple[str, float]] = []
    for name in _READINESS_PROBES:
        outcome = outcomes.get(name)
        if isinstance(outcome, BaseException) or outcome is None:
            continue
        if getattr(outcome, "state", None) != "stale_cached":
            continue
        age_s = getattr(outcome, "age_s", None)
        if age_s is None or age_s <= threshold_s:
            continue
        stale.append((name, float(age_s)))
    return stale


async def _readiness_probe_outcomes() -> dict[str, object]:
    """Run exactly the readiness probes, through the cache ``/health`` already uses.

    This endpoint drives its OWN probes rather than reading the cache another
    endpoint fills. A read-only readiness check has the mirror-image failure of
    the incident: with nobody polling /health the recorded ages keep growing
    while nothing is ever attempted, and a perfectly healthy idle task gets
    replaced for being idle.

    Same singleton cache, same budgets, same ``absent`` values as /health, so
    the two endpoints can never disagree about how old a reading is, and this
    one inherits /health's "answers, never waits" contract (#1592/#1594): the
    whole set costs ``max(budget)``, not ``sum(budget)``.
    """
    from archimedes.services.corpus_service import count_corpus_papers, get_corpus_meta, get_paper_count
    from archimedes.services.health_cache import health_probe_cache

    # Imported here, not at module scope, so the tests that patch these module
    # attributes keep working — same reason the three ``*_read`` helpers above
    # import inside their own bodies.
    reads = {
        _CORPUS_PROBE: count_corpus_papers,
        _CORPUS_DB_PROBE: get_paper_count,
        _CORPUS_META_PROBE: get_corpus_meta,
        _PAPER_RAG_PROBE: _paper_rag_read,
        _RISK_DATA_PROBE: _risk_data_read,
    }
    # Iterated and zipped back over the SAME sequence, so reordering
    # _READINESS_PROBES can never silently attach one probe's age to another
    # probe's name — which would be a lie of exactly the kind this endpoint is
    # here to stop.
    outcomes = await asyncio.gather(
        *(
            health_probe_cache.probe(
                name,
                _bounded_local_read(reads[name]),
                budget_seconds=_LOCAL_PROBE_BUDGET_SECONDS,
                absent=_READINESS_ABSENT[name],
            )
            for name in _READINESS_PROBES
        ),
        return_exceptions=True,
    )
    return dict(zip(_READINESS_PROBES, outcomes, strict=True))


def _readiness_probe_payload(outcomes: dict[str, object]) -> dict[str, dict[str, object]]:
    """Per-probe ``state``/``age_s``/``reason``, so a 503 says WHICH read went dark."""
    payload: dict[str, dict[str, object]] = {}
    for name in _READINESS_PROBES:
        outcome = outcomes.get(name)
        if isinstance(outcome, BaseException):
            payload[name] = {
                "state": "probe_error",
                "age_s": None,
                "reason": f"{name} probe_error: {outcome}",
            }
            continue
        payload[name] = {
            "state": getattr(outcome, "state", "probe_error"),
            "age_s": getattr(outcome, "age_s", None),
            "reason": getattr(outcome, "reason", ""),
        }
    return payload


@app.get("/health/ready")
@app.get("/api/health/ready")
@limiter.exempt
async def health_ready(response: Response):
    """READINESS — 503 when this task has been reading from cache too long (#1818 P3).

    Wired to the CONTAINER health check (backend/Dockerfile ``HEALTHCHECK``,
    mirrored in ``infra/ecs.tf``), never to the ALB target group. See the
    READINESS block above for why those two consumers are kept apart.

    Liveness is unchanged and is still ``/health``'s job: this endpoint answers
    under the same bounded-probe contract, so "the process is alive" is proved
    by it returning at all, and 503 is a statement about READINESS only —
    "alive, and unable to read its database since ``age_s`` seconds ago".
    """
    _no_store(response)

    outcomes = await _readiness_probe_outcomes()
    threshold_s = _stale_unready_threshold_s()
    stale = _stale_unready_probes(outcomes, threshold_s)

    if stale:
        # 503, not 500: the task is alive and answering, it just must not be
        # kept in service. Also a loud, greppable literal for a CloudWatch
        # metric filter, in the shape of HEALTH_CHAIN_DISCONNECTED and
        # HEALTH_ORACLE_STALE in the /health handler below. (The alarm itself is
        # #1818 P5; this is the log line it will key on.)
        response.status_code = 503
        logger.error(
            "HEALTH_READINESS_STALE: unready — %s stale beyond %.0fs; container health check fails",
            ", ".join(f"{name}={age_s:.0f}s" for name, age_s in stale),
            threshold_s,
        )

    return {
        "ready": not stale,
        "service": "archimedes-backend",
        "version": os.getenv("ARCHIMEDES_GIT_SHA", "dev"),
        # 0.0 means the rule is switched off by env; publishing it is how an
        # operator sees that a 200 here is a verdict and not a switched-off rule.
        "stale_unready_threshold_s": threshold_s,
        "stale_probes": [{"probe": name, "age_s": age_s} for name, age_s in stale],
        "probes": _readiness_probe_payload(outcomes),
    }


@app.get("/health")
@app.get("/api/health")
@limiter.exempt
async def health(response: Response):
    """Health check — used by Docker healthcheck and CI/CD.

    Reports corpus state so silent degradation is visible.

    **This endpoint reports what we know; it does not go and find out.** Every
    probe is bounded and falls back to its last-known value, labelled with age
    and reason — the two outbound ones since #1592, the six local ones since
    #1594, and the LLM backend since #1044 (`make_llm_backend()` looked like
    construction and was in fact a blocking outbound call on the ollama path).
    No field's MEANING changed — only how long the handler is willing to wait
    to compute it.
    """
    _no_store(response)

    from archimedes.chain.client import chain_client
    from archimedes.services.corpus_service import count_corpus_papers, get_corpus_meta, get_paper_count
    from archimedes.services.health_cache import health_probe_cache

    async def _oracle_probe():
        # Imported at call time, not module scope, so tests keep patching
        # ``services.oracle_health.oracle_health`` the way they already do.
        from archimedes.services.oracle_health import oracle_health as _oracle_health_probe

        return await _oracle_health_probe(budget_seconds=_ORACLE_INNER_BUDGET_SECONDS)

    def _llm_read() -> tuple[bool, str, str, str | None]:
        """Resolve the LLM backend off the loop, on the dedicated probe pool (#1044).

        Returns plain data — ``(available, backend_label, reason, model_id)`` —
        rather than the backend object, because this value gets CACHED as
        /health's last-known reading and a live client held across requests
        would be a connection, not a measurement. Runs through
        ``_bounded_local_read`` for the same reason the six local reads do: the
        resolve is synchronous and blocking (on the ollama path it is a real
        ``httpx.get`` with a 3s timeout), and a deadline cannot fire on a loop
        that is not turning. NOT ``asyncio.to_thread`` — the default executor
        is where ``getaddrinfo`` lives (see _HEALTH_PROBE_EXECUTOR above).
        """
        # Imported inside the worker thread so tests keep patching
        # ``services.llm_backend.make_llm_backend`` the way they already do.
        from archimedes.services.llm_backend import make_llm_backend as _make

        backend = _make()
        available = bool(getattr(backend, "available", False))
        label = "live" if available else str(getattr(backend, "model_id", "unavailable"))
        reason = "" if available else str(getattr(backend, "unavailable_reason", "") or "")
        return available, label, reason, getattr(backend, "model_id", None)

    # Concurrent + bounded. return_exceptions keeps one broken probe from
    # taking the others down: a raised probe is still a health verdict, it is
    # just a "we could not read it" one, and that is what gets reported.
    #
    # ALL NINE reads live in this one gather (#1594, the LLM probe via #1044).
    # The six local ones used to run one after another, unbounded, below the
    # chain/oracle block — which is why #1592 read correct and measured wrong:
    # the outbound calls were bounded and the handler still went to 17s p95.
    # Adding them here costs max(budget), not sum(budget), and every one of
    # them now reports its own freshness instead of stalling the endpoint that
    # reports everything else.
    (
        chain_outcome,
        oracle_outcome,
        corpus_outcome,
        corpus_db_outcome,
        corpus_meta_outcome,
        paper_rag_outcome,
        regime_outcome,
        risk_data_outcome,
        llm_outcome,
    ) = await asyncio.gather(
        health_probe_cache.probe(
            "chain_connected",
            chain_client.is_connected,
            budget_seconds=_CHAIN_PROBE_BUDGET_SECONDS,
            absent=False,
        ),
        health_probe_cache.probe(
            "oracle_health",
            _oracle_probe,
            budget_seconds=_ORACLE_PROBE_BUDGET_SECONDS,
            absent=None,
        ),
        # `absent=[]` renders corpus_papers: 0 — a plausible-looking number, and
        # the reason every one of these carries a `*_probe_state`: the state
        # field is what separates "we read zero" from "we could not read".
        # A COUNT, never load_corpus: the full 18k-row ORM load here is what
        # killed prod rev 214 — abandoned cold-boot probes piled concurrent
        # loads into the executor until two raced in SQLAlchemy session
        # teardown and aborted the interpreter (#1632). A scalar count holds
        # no ORM state to race; load_corpus itself is additionally serialized
        # behind _CORPUS_LOAD_LOCK for its real (generation) callers.
        health_probe_cache.probe(
            _CORPUS_PROBE,
            _bounded_local_read(count_corpus_papers),
            budget_seconds=_LOCAL_PROBE_BUDGET_SECONDS,
            absent=0,
        ),
        health_probe_cache.probe(
            _CORPUS_DB_PROBE,
            _bounded_local_read(get_paper_count),
            budget_seconds=_LOCAL_PROBE_BUDGET_SECONDS,
            absent=0,
        ),
        health_probe_cache.probe(
            _CORPUS_META_PROBE,
            _bounded_local_read(get_corpus_meta),
            budget_seconds=_LOCAL_PROBE_BUDGET_SECONDS,
            absent=None,
        ),
        health_probe_cache.probe(
            _PAPER_RAG_PROBE,
            _bounded_local_read(_paper_rag_read),
            budget_seconds=_LOCAL_PROBE_BUDGET_SECONDS,
            absent=None,
        ),
        health_probe_cache.probe(
            _REGIME_PROBE,
            _bounded_local_read(_regime_read),
            budget_seconds=_LOCAL_PROBE_BUDGET_SECONDS,
            absent=None,
        ),
        health_probe_cache.probe(
            _RISK_DATA_PROBE,
            _bounded_local_read(_risk_data_read),
            budget_seconds=_LOCAL_PROBE_BUDGET_SECONDS,
            absent=None,
        ),
        health_probe_cache.probe(
            "llm_backend",
            _bounded_local_read(_llm_read),
            budget_seconds=_LLM_PROBE_BUDGET_SECONDS,
            # "We could not read it" for a capability flag is FALSE. An
            # optimistic absent value here would rebuild the exact lie #1044
            # set out to kill — /health claiming an LLM it has never reached.
            absent=(False, "unavailable", "llm probe never completed", None),
        ),
        return_exceptions=True,
    )

    # ── chain connectivity ───────────────────────────────────────────────
    chain_probe_fields: dict[str, object] = {}
    chain_probe_live = False
    if isinstance(chain_outcome, BaseException):
        # ChainClient.is_connected() already maps every chain-side failure to
        # False, so an exception here is a defect in the probe plumbing rather
        # than a verdict about the chain. Report it as its own state instead of
        # letting it masquerade as either a live reading or a timeout.
        logger.warning(
            "chain health probe raised %s — reporting chain_connected=false",
            type(chain_outcome).__name__,
        )
        connected = False
        chain_probe_fields = {
            "chain_probe_state": "probe_error",
            "chain_probe_age_s": None,
            "chain_probe_reason": f"chain probe_error: {chain_outcome}",
        }
    else:
        connected = bool(chain_outcome.value)
        chain_probe_live = chain_outcome.is_live
        chain_probe_fields = chain_outcome.payload_fields("chain")

    if not connected:
        # N2 (infra #1039): /health's status code is deliberately unchanged (still
        # 200 — see the module docstring above and infra/runbooks) so a transient
        # Arc RPC blip can't cascade the whole ECS service down. But "degraded but
        # HTTP 200" is silent by default, so log loudly here — a CloudWatch Logs
        # metric filter on this exact string (infra/cloudwatch.tf) turns repeated
        # occurrences into a paging alarm without touching the response contract.
        logger.warning("HEALTH_CHAIN_DISCONNECTED: chain_connected=false (Arc RPC unreachable or timed out)")

    # ── corpus file load ─────────────────────────────────────────────────
    # Previously a bare `corpus = load_corpus()`: an unbounded read whose only
    # failure mode was a 500 from the endpoint that exists to report failures.
    # Now bounded, and a raise is reported as `probe_error` rather than losing
    # every other field on the page.
    corpus_probe_fields: dict[str, object] = {}
    if isinstance(corpus_outcome, BaseException):
        logger.warning("corpus count raised %s — reporting 0 papers", type(corpus_outcome).__name__)
        corpus_count: int = 0
        corpus_probe_fields = _probe_error_fields(_CORPUS_PROBE, corpus_outcome)
    else:
        corpus_count = int(corpus_outcome.value or 0)
        corpus_probe_fields = corpus_outcome.payload_fields(_CORPUS_PROBE)

    # ── LLM backend ──────────────────────────────────────────────────────
    llm_provider = os.getenv("LLM_PROVIDER", "auto")
    llm_probe_fields: dict[str, object] = {}
    if isinstance(llm_outcome, BaseException):
        # Same treatment as the chain probe above: a raised probe is a defect in
        # the plumbing, not a verdict about the backend, so it gets its own
        # state instead of masquerading as either a reading or a timeout.
        logger.warning(
            "llm health probe raised %s — reporting llm_available=false",
            type(llm_outcome).__name__,
        )
        is_available, llm_backend, llm_reason, llm_model = (
            False,
            "unavailable",
            f"llm probe_error: {llm_outcome}",
            None,
        )
        llm_probe_fields = {
            "llm_probe_state": "probe_error",
            "llm_probe_age_s": None,
            "llm_probe_reason": llm_reason,
        }
    else:
        is_available, llm_backend, llm_reason, llm_model = llm_outcome.value
        llm_probe_fields = llm_outcome.payload_fields("llm")
        if llm_outcome.state == "stale_cached":
            # A served-from-cache reading has to say so in the field an operator
            # actually reads, not only in the sibling llm_probe_* fields. Exact
            # shape of the oracle_reason composition below (main.py:1089).
            llm_reason = (
                f"{llm_outcome.reason}; last completed read {llm_outcome.age_s}s ago: {llm_reason or 'available'}"
            )
        elif not llm_outcome.is_live:
            # probe_timeout with nothing ever cached. There is no prior reading,
            # so nothing is quoted as one — the timeout IS the whole reason.
            llm_reason = llm_outcome.reason

    # DB-backed corpus diagnostics. Two probes, not one try block: a paper-count
    # query that answers and a corpus-meta query that stalls are two different
    # facts, and folding them together lost the distinction.
    db_count = 0
    corpus_db_probe_fields: dict[str, object] = {}
    if isinstance(corpus_db_outcome, BaseException):
        logger.debug("corpus paper-count read failed", exc_info=corpus_db_outcome)
        corpus_db_probe_fields = _probe_error_fields(_CORPUS_DB_PROBE, corpus_db_outcome)
    else:
        db_count = corpus_db_outcome.value or 0
        corpus_db_probe_fields = corpus_db_outcome.payload_fields(_CORPUS_DB_PROBE)

    corpus_source = "file"
    corpus_last_intake = None
    artifact_hash = None
    corpus_meta_probe_fields: dict[str, object] = {}
    if isinstance(corpus_meta_outcome, BaseException):
        logger.debug("corpus meta read failed", exc_info=corpus_meta_outcome)
        corpus_meta_probe_fields = _probe_error_fields(_CORPUS_META_PROBE, corpus_meta_outcome)
    else:
        meta = corpus_meta_outcome.value
        corpus_meta_probe_fields = corpus_meta_outcome.payload_fields(_CORPUS_META_PROBE)
        if meta:
            corpus_source = meta.get("source", "unknown")
            corpus_last_intake = meta.get("last_intake_at")
            artifact_hash = meta.get("artifact_hash")

    # Paper RAG health (semantic retrieval). "unknown" is reserved for a probe
    # that never completed — it must not collapse into "disabled", which is a
    # real, deliberately-configured state.
    paper_rag_status = "disabled"
    paper_rag_reason = ""
    paper_rag_probe_fields: dict[str, object] = {}
    if isinstance(paper_rag_outcome, BaseException):
        paper_rag_reason = "import failed"
        paper_rag_probe_fields = _probe_error_fields(_PAPER_RAG_PROBE, paper_rag_outcome)
    else:
        paper_rag_probe_fields = paper_rag_outcome.payload_fields(_PAPER_RAG_PROBE)
        _diag = paper_rag_outcome.value
        if _diag is None:
            paper_rag_status = "unknown"
            paper_rag_reason = paper_rag_outcome.reason
        else:
            paper_rag_status = _diag.status
            paper_rag_reason = _cache_annotated(paper_rag_outcome, _diag.reason)

    # GMM regime-detector health (T0.5 — loud fallback telemetry).
    # "degraded" => no fitted artifact, rule-based VixRegimeDetector fallback
    # active. Surfaced so rule-based regime calls aren't presented as data-driven.
    regime_detector_status = "unknown"
    regime_detector_reason = ""
    regime_detector_probe_fields: dict[str, object] = {}
    if isinstance(regime_outcome, BaseException):
        regime_detector_reason = "import failed"
        regime_detector_probe_fields = _probe_error_fields(_REGIME_PROBE, regime_outcome)
    else:
        regime_detector_probe_fields = regime_outcome.payload_fields(_REGIME_PROBE)
        _gmm_diag = regime_outcome.value
        if _gmm_diag is None:
            regime_detector_reason = regime_outcome.reason
        else:
            regime_detector_status = _gmm_diag.status
            regime_detector_reason = _cache_annotated(regime_outcome, _gmm_diag.reason)

    # Risk-analysis data health (T0.5 — loud fallback telemetry).
    # "mock" => no persisted backtest equity curves, so the Risk UI renders
    # placeholder mockReturns. Surfaced so mock tail-risk isn't presented as real.
    risk_data_status = "unknown"
    risk_data_reason = ""
    risk_data_probe_fields: dict[str, object] = {}
    if isinstance(risk_data_outcome, BaseException):
        risk_data_reason = "import failed"
        risk_data_probe_fields = _probe_error_fields(_RISK_DATA_PROBE, risk_data_outcome)
    else:
        risk_data_probe_fields = risk_data_outcome.payload_fields(_RISK_DATA_PROBE)
        _risk_diag = risk_data_outcome.value
        if _risk_diag is None:
            risk_data_reason = risk_data_outcome.reason
        else:
            risk_data_status = _risk_diag.status
            risk_data_reason = _cache_annotated(risk_data_outcome, _risk_diag.reason)

    # Oracle-freshness health (issue #1371 — isFresh()/lastUpdated() had zero
    # backend callers; every deployed PriceOracle has been stale since the
    # T3.2 redeploy with nothing reporting it). oracle_fresh is true only if
    # EVERY probed oracle is fresh; oracle_probed_count/oracle_universe_count
    # are always both present so a 2-of-281-fresh push set can never be read
    # as "oracles are healthy" system-wide. A chain-read failure reports
    # oracle_fresh=false with an explicit marker (never fail-soft "assume
    # fresh") — see services/oracle_health.py's module docstring.
    #
    # The probe itself ran CONCURRENTLY with the chain check at the top of this
    # handler, under its own hard budget (#1592). What is left here is only the
    # rendering of whatever that bounded probe produced — three cases, and none
    # of them may report "fresh" without a completed read behind it.
    oracle_fresh = False
    oracle_oldest_age_s: int | None = None
    oracle_probed_count = 0
    oracle_universe_count = 0
    oracle_probe_fields: dict[str, object] = {}
    # oracle_reason needs no initializer: every branch below assigns it
    # unconditionally before any use.
    if isinstance(oracle_outcome, BaseException):
        # The probe raised. Same wording as before this change, so the existing
        # `probe_error` contract (and the test that asserts it) is untouched.
        oracle_reason = f"oracle_health probe_error: {oracle_outcome}"
        oracle_probe_fields = {
            "oracle_probe_state": "probe_error",
            "oracle_probe_age_s": None,
            "oracle_probe_reason": oracle_reason,
        }
    else:
        oracle_probe_fields = oracle_outcome.payload_fields("oracle")
        _oracle_diag = oracle_outcome.value
        if _oracle_diag is None:
            # probe_timeout with nothing ever cached: there is no reading to
            # report. oracle_fresh stays False and the counts stay 0 — a loud
            # absence, never a fabricated "fresh" or a borrowed count.
            oracle_reason = oracle_outcome.reason
        else:
            oracle_fresh = _oracle_diag.oracle_fresh
            oracle_oldest_age_s = _oracle_diag.oracle_oldest_age_s
            oracle_probed_count = _oracle_diag.oracle_probed_count
            oracle_universe_count = _oracle_diag.oracle_universe_count
            oracle_reason = _oracle_diag.reason
            if oracle_outcome.state == "stale_cached":
                # Served from cache. The values above are a real past reading,
                # so they keep their meaning — but the reason string has to say
                # out loud that they are not current, because oracle_reason is
                # the field an operator actually reads.
                oracle_reason = (
                    f"{oracle_outcome.reason}; last completed read {oracle_outcome.age_s}s ago: {oracle_reason}"
                )

    if not oracle_fresh:
        # Loud, greppable marker — infra/cloudwatch.tf's metric filter keys off
        # this exact literal (mirrors HEALTH_CHAIN_DISCONNECTED at :545-553).
        # /health's HTTP status stays 200 for the same ALB/ECS reason documented
        # there; this is what makes the degraded state page a human instead of
        # sitting silently in a JSON body nobody is reading.
        logger.warning(
            "HEALTH_ORACLE_STALE: oracle_fresh=false oldest_age_s=%s probed=%d/%d (%s)",
            oracle_oldest_age_s,
            oracle_probed_count,
            oracle_universe_count,
            oracle_reason,
        )

    # Strategy-library presence (issue #1039). count_strategy_files() is a cheap
    # directory file count (NO provider construction → no filesystem refresh, DB
    # backtest load, or unified-table sync side effect — /health is hit by the ALB
    # every 30s). 0 here means the curated library is missing from the image — the
    # exact regression that shipped a strategy-less Fargate build (→ risk_data=mock,
    # empty Explore). CI asserts > 0.
    strategy_count = 0
    try:
        from archimedes.services.strategy_provider import count_strategy_files

        strategy_count = count_strategy_files()
    except Exception:
        logger.debug("strategy count read failed", exc_info=True)

    # Human-vs-agent traction counts (issue #428). Fail-safe: get_counts
    # returns (0, 0) when Redis is unreachable, so /health never degrades on it.
    # NOTE: these are cumulative per-request tallies (site traffic, NOT users).
    human_count = 0
    agent_count = 0
    try:
        from archimedes.services.telemetry_store import TelemetryStore

        _store = TelemetryStore()
        try:
            human_count, agent_count = await _store.get_counts()
        finally:
            await _store.close()
    except Exception:
        logger.debug("telemetry counts read failed", exc_info=True)

    # Distinct real users = canonical Better Auth accounts. Surfaced
    # next to the request tallies so no doc/monitor can conflate traffic with
    # users. Fail-safe: returns 0 on any DB error.
    real_users = 0
    try:
        from archimedes.services.user_stats import get_distinct_user_count

        real_users = get_distinct_user_count()
    except Exception:
        logger.debug("real_users count read failed", exc_info=True)

    # Claim-integrity: corpus honesty fields (issue #778).
    # The `corpus_papers` / `corpus_db_count` counts above are *metadata records*
    # seeded from the JSONL manifest into the `papers` table — they do NOT imply
    # the corpus has been embedded, clustered, or graphed. These three fields make
    # the real state machine-readable so no surface can present manifest metadata
    # as "embedded / knowledge-graphed". Each is driven from actual state, never a
    # constant:
    #   paper_rerank_model_live — a sentence-transformer object is loaded IN THIS
    #                             PROCESS (paper_rag == "live"). "ready" (weights on
    #                             disk, nothing has retrieved yet) is deliberately not
    #                             live: presence on disk is not proof. Flips to true on
    #                             the first real retrieval, and back to false on restart.
    #   corpus_embedded_at_rest — whether STORED vectors exist, derived from the schema.
    #                             This was previously published as `corpus_embedded` with
    #                             the value of the field above it (#1488): the name
    #                             asserted a property of the corpus while the value
    #                             measured a property of the process, so one retrieval
    #                             made /api/health say the 10k corpus was embedded. It
    #                             was never embedded; retrieval is a keyword filter plus
    #                             a query-time rerank of at most `rerank_candidate_cap`
    #                             candidates, and everything past that cap is appended at
    #                             score 0.0. Both fields ship because the pair is what
    #                             makes the absence legible — a lone false reads as an
    #                             outage, and a lone true reads as the claim we must not
    #                             make. Same reason `corpus_kg_built: false` sits beside
    #                             `corpus_kg_entities: 0`.
    #   corpus_kg_built         — at least one KG entity exists (REBEL/SciSpacy output)
    #   corpus_artifact_present — a real KB-pipeline artifact (S3/local manifest) exists
    paper_rerank_model_live = paper_rag_status == "live"
    # Reporting "not embedded" on a failed read is the only safe direction here:
    # the sole way this field can do harm is by claiming vectors that are absent.
    corpus_embedded_at_rest = False
    corpus_embedded_at_rest_reason = "probe failed"
    rerank_cap = 0
    try:
        from archimedes.services.paper_rag import corpus_embedding_at_rest, rerank_candidate_cap

        _at_rest = corpus_embedding_at_rest()
        corpus_embedded_at_rest = _at_rest.embedded_at_rest
        corpus_embedded_at_rest_reason = _at_rest.reason
        rerank_cap = rerank_candidate_cap()
    except Exception as exc:
        logger.warning("corpus_embedding_at_rest read failed (%s) — reporting not-embedded", type(exc).__name__)
        corpus_embedded_at_rest_reason = f"probe failed ({type(exc).__name__})"
    kg_entity_count = 0
    kg_relation_count = 0
    try:
        from sqlalchemy import func

        from archimedes.db import get_session
        from archimedes.models.kg import KGEntity, KGRelation

        with get_session() as session:
            kg_entity_count = session.query(func.count(KGEntity.id)).scalar() or 0
            kg_relation_count = session.query(func.count(KGRelation.id)).scalar() or 0
    except Exception:
        logger.debug("kg counts read failed", exc_info=True)
    corpus_kg_built = kg_entity_count > 0

    corpus_artifact_present = False
    try:
        from archimedes.services.kb_artifacts import ArtifactNotFound, load_manifest

        try:
            load_manifest()
            corpus_artifact_present = True
        except ArtifactNotFound:
            corpus_artifact_present = False
    except Exception:
        logger.debug("kb artifact probe failed", exc_info=True)

    # Reveal-reconciliation observability (issue #1353, hardening #1352's
    # audit-G9 pass). Both counts come off the durable index (SCARD is O(1),
    # not a scan). Fail-safe like every other Redis-backed field on this
    # endpoint: a Redis outage reports 0 rather than 500ing the whole health
    # check.
    #
    # HOW TO READ "terminal" (#1403 review): it is a CUMULATIVE lifetime
    # counter, not a level. Members are never removed from the terminal set,
    # so the number only ever goes up for the life of the Redis keyspace and
    # one historical give-up pins it above zero permanently. Watch the RATE OF
    # INCREASE between samples; a static threshold on the value would fire once
    # and stay fired. "pending" is the true level gauge — it goes up and down
    # as commitments dangle and resolve.
    #
    # WHAT IS AND ISN'T WIRED (#1403 review): this publishes the SURFACE only.
    # No alerting consumes these two fields — infra/cloudwatch.tf has no metric
    # filter and no alarm over them, and unlike the HEALTH_CHAIN_DISCONNECTED
    # and HEALTH_ORACLE_STALE literals this same handler logs elsewhere,
    # nothing here emits a greppable literal a metric filter could key on
    # (those two are the repo's only working log-literal ->
    # aws_cloudwatch_log_metric_filter -> alarm pairs). Calling either field
    # "alertable" would overstate what exists today: they are readable, not
    # paging.
    #
    # MIGRATION CAVEAT (#1403 review): ``reveal_reconcile_pending`` under-counts
    # any dangling record written before this index existed and not yet
    # re-saved through the new ``save_trace`` path — it is SCARD of a set only
    # that path populates, so a pre-index dangling record is invisible here
    # until something re-saves it (the reconciliation pass's own retry does
    # this the first time it touches one, same one-cycle migration window the
    # bounded-scan backstop covers). This gauge can therefore read 0 at deploy
    # time while real dangling reveals are outstanding; it becomes accurate
    # once every pre-existing dangling record has been touched once.
    reveal_reconcile_pending = 0
    reveal_reconcile_terminal = 0
    try:
        from archimedes.services.redis_state import AgentStateStore

        _reconcile_store = AgentStateStore()
        try:
            reveal_reconcile_pending = await _reconcile_store.get_reveal_reconcile_pending_count()
            reveal_reconcile_terminal = await _reconcile_store.get_reveal_reconcile_terminal_count()
        finally:
            await _reconcile_store.close()
    except Exception:
        logger.debug("reveal reconciliation counts read failed", exc_info=True)

    # Readiness verdict (#1818 P3), published as DATA and never as this
    # endpoint's status code. /health is the ALB's check and stays 200 by
    # contract; these three fields are how an operator reading the body — or a
    # monitor scraping it — sees the same verdict the container health check at
    # /health/ready is acting on, computed from the very outcomes above rather
    # than from a second set of probes. See the READINESS block above main's
    # /health handler for why the status code lives on the other endpoint.
    #
    # Wrapped, like the reveal-reconcile read above it, because THIS endpoint's
    # contract is that it always answers 200 (infra/alb.tf ``matcher = "200"``,
    # pinned by test_health_always_answers.py). An exception escaping here would
    # turn the ALB's check into a 500 and drain every target at once — the exact
    # fleet-wide failure this PR avoided by putting the 503 on /health/ready.
    # A reporting field must not be able to outrank the endpoint reporting it.
    #
    # ``ready`` is None, not True, when the verdict could not be computed: a
    # display failure must not publish an optimistic verdict (same rule as the
    # ``absent`` values above). /health/ready is the authority either way — it
    # computes this independently and is what the scheduler acts on.
    _readiness_threshold_s: float | None = None
    _stale_unready: list[tuple[str, float]] = []
    _readiness_known = False
    try:
        _readiness_threshold_s = _stale_unready_threshold_s()
        _stale_unready = _stale_unready_probes(
            {
                _CORPUS_PROBE: corpus_outcome,
                _CORPUS_DB_PROBE: corpus_db_outcome,
                _CORPUS_META_PROBE: corpus_meta_outcome,
                _PAPER_RAG_PROBE: paper_rag_outcome,
                _RISK_DATA_PROBE: risk_data_outcome,
            },
            _readiness_threshold_s,
        )
        _readiness_known = True
    except Exception:
        _readiness_threshold_s = None
        _stale_unready = []
        logger.debug("readiness verdict for /health failed", exc_info=True)

    return {
        # "ok" requires BOTH a connected chain and a LIVE reading of it (#1592).
        # A cached `connected: true` served because the fresh probe timed out is
        # not evidence of a connected chain, and reporting "ok" off it would be
        # exactly the plausible-substitute failure this codebase treats as its
        # primary defect class. This can only ever widen "degraded" — it never
        # calls something healthy that was previously degraded — and the HTTP
        # status stays 200 for the ALB/ECS reason documented above.
        "status": "ok" if connected and chain_probe_live else "degraded",
        "service": "archimedes-backend",
        # Build provenance (issue #1039): the git SHA stamped in at image-build
        # time (deploy.yml --build-arg GIT_SHA). "Which code is live?" in one glance
        # — the question that turned the Fargate cutover into an hour of forensics.
        "version": os.getenv("ARCHIMEDES_GIT_SHA", "dev"),
        "chain_connected": connected,
        # Bounded-probe provenance for chain_connected (#1592). `chain_probe_state`
        # is always present ("live" | "stale_cached" | "probe_timeout" |
        # "probe_error"); `chain_probe_age_s` + `chain_probe_reason` appear ONLY
        # when the fresh probe missed, so their presence IS the signal and their
        # absence is the all-clear. Same shape for the oracle block below.
        **chain_probe_fields,
        # human_count / agent_count are cumulative per-request tallies (site
        # traffic, NOT users). real_users is the honest distinct-user count.
        "human_count": human_count,
        "agent_count": agent_count,
        "real_users": real_users,
        "corpus_papers": corpus_count,
        "corpus_db_count": db_count,
        "corpus_source": corpus_source,
        "corpus_last_intake": corpus_last_intake,
        "artifact_hash": artifact_hash,
        # Bounded-probe provenance for the six LOCAL reads (#1594), same shape
        # and same rules as the chain/oracle blocks: `*_probe_state` always
        # present, `*_probe_age_s` + `*_probe_reason` present ONLY when the
        # fresh read missed. Without these, `corpus_papers: 0` and
        # `regime_detector: "unknown"` are indistinguishable from real readings.
        **corpus_probe_fields,
        **corpus_db_probe_fields,
        **corpus_meta_probe_fields,
        # Claim-integrity honesty fields (issue #778). The counts above are
        # manifest-seeded *metadata records*; these say what has actually been
        # built on top of them. New keys only — existing keys are unchanged so
        # current UI/monitoring consumers don't break.
        "paper_rerank_model_live": paper_rerank_model_live,
        "corpus_embedded_at_rest": corpus_embedded_at_rest,
        "corpus_embedded_at_rest_reason": corpus_embedded_at_rest_reason,
        "rerank_candidate_cap": rerank_cap,
        "corpus_kg_built": corpus_kg_built,
        "corpus_kg_entities": kg_entity_count,
        "corpus_kg_relations": kg_relation_count,
        "corpus_artifact_present": corpus_artifact_present,
        # `fusion_enabled` was published here until 2026-09-03. It was dropped
        # by owner decision (deck Q4 follow-up): with ARCHIMEDES_FUSION_ENABLED
        # retired, the key could only ever be the literal `True`, and a constant
        # dressed as a health signal is exactly the claim-integrity problem the
        # fields above exist to avoid. No consumer was found — the field set is
        # asserted absent in backend/tests/test_health_always_answers.py.
        "llm_provider": llm_provider,
        "llm_backend": llm_backend,
        "llm_model": llm_model,
        "llm_available": is_available,
        # Why the backend is not live, in a sentence an operator can act on
        # ("ollama unreachable at …", "LLM_MODEL is unset …", "… is not pulled
        # (run `ollama pull llama3.1`)"). Empty string when it IS live — a bool
        # alone cannot tell those three apart, which is how a local ollama
        # misconfiguration used to read as a mystery (#1044). Companion to the
        # oracle_reason / paper_rag_reason / corpus_embedded_at_rest_reason
        # pattern already in this payload.
        "llm_reason": llm_reason,
        **llm_probe_fields,
        "llm_has_api_key": bool(os.getenv("LLM_API_KEY") or os.getenv("ANTHROPIC_API_KEY")),
        "llm_has_auth_token": bool(os.getenv("LLM_AUTH_TOKEN") or os.getenv("ANTHROPIC_AUTH_TOKEN")),
        "llm_has_base_url": bool(os.getenv("LLM_BASE_URL") or os.getenv("ANTHROPIC_BASE_URL")),
        "paper_rag": paper_rag_status,
        "paper_rag_reason": paper_rag_reason,
        **paper_rag_probe_fields,
        "regime_detector": regime_detector_status,
        "regime_detector_reason": regime_detector_reason,
        **regime_detector_probe_fields,
        "risk_data": risk_data_status,
        "risk_data_reason": risk_data_reason,
        **risk_data_probe_fields,
        # Oracle-freshness health (issue #1371). oracle_fresh is true only when
        # EVERY probed oracle is fresh — oracle_probed_count/oracle_universe_count
        # are always both present so a fully-fresh push set is never read as
        # "the oracle subsystem is healthy" when the push set is a small
        # fraction of the deployed universe. See services/oracle_health.py.
        "oracle_fresh": oracle_fresh,
        "oracle_oldest_age_s": oracle_oldest_age_s,
        "oracle_probed_count": oracle_probed_count,
        "oracle_universe_count": oracle_universe_count,
        "oracle_reason": oracle_reason,
        **oracle_probe_fields,
        # Strategy-library presence (issue #1039) — 0 means the image is missing
        # analytics-engine/strategies (the Fargate-cutover regression). CI gates on > 0.
        "strategy_count": strategy_count,
        # Reveal-reconciliation gauges (issue #1353). "pending" = a level:
        # commitments currently dangling and awaiting a retry, up and down.
        # "terminal" = a CUMULATIVE lifetime counter of permanent give-ups
        # (never decreases — alert on its rate of increase, not its value).
        # Nothing consumes either field yet; see the block above the return.
        "reveal_reconcile_pending": reveal_reconcile_pending,
        "reveal_reconcile_terminal": reveal_reconcile_terminal,
        # Readiness (#1818 P3). `ready: false` means the DB-backed probes above
        # have been serving cached readings for longer than
        # stale_unready_threshold_s, i.e. this task is alive but can no longer
        # read — the state that sat behind a green /health for ten hours on
        # 2026-09-03. The STATUS CODE here is deliberately still 200; the 503
        # that gets the task replaced is /health/ready's, which the container
        # health check polls. `stale_unready_threshold_s: 0` means the rule is
        # switched off by env, so `ready: true` under it is not a verdict.
        # `ready: null` (with a null threshold) means the verdict could not be
        # computed on this poll at all — never read that as ready.
        "ready": (not _stale_unready) if _readiness_known else None,
        "stale_unready_probes": [{"probe": name, "age_s": age_s} for name, age_s in _stale_unready],
        "stale_unready_threshold_s": _readiness_threshold_s,
    }


@app.get("/health/paper-rag")
@limiter.exempt
async def health_paper_rag(response: Response):
    """Dedicated paper-rag health endpoint."""
    _no_store(response)
    from archimedes.services.paper_rag import paper_rag_health

    # probe=True: this endpoint exists to PROVE the model loads, so it is the
    # one place allowed to pay the ~521 MB load cost. The ALB-polled /health
    # deliberately does not (see paper_rag_health's docstring).
    diag = paper_rag_health(probe=True)
    return {
        "paper_rag": diag.status,
        "reason": diag.reason,
    }


@app.get("/health/amm")
@app.get("/api/health/amm")
@limiter.exempt
async def health_amm(response: Response):
    """AMM pool liquidity health — per-pool status for operator/judge probes.

    Returns 200 with pool list when pools exist, or 503 with an explicit
    status message when they haven't been initialized. Never returns 404.
    """
    from fastapi.responses import JSONResponse

    from archimedes.chain.client import chain_client

    _no_store(response)
    try:
        connected = await chain_client.is_connected()
        if not connected:
            return JSONResponse(
                status_code=503,
                content={"status": "chain_disconnected", "reason": "Cannot reach Arc RPC"},
                headers=_no_store_headers(),
            )

        from archimedes.chain.contracts import get_contract_loader

        loader = get_contract_loader()
        router = loader.amm_router

        # getAllPools() returns list of pool addresses
        pool_addresses = await router.functions.getAllPools().call()

        if not pool_addresses:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "amm_pools_not_initialized",
                    "reason": "No AMM pools exist yet. Run bootstrap_vaults to create pools.",
                    "pools": [],
                },
                headers=_no_store_headers(),
            )

        # For each pool, read basic state
        pools = []
        for addr in pool_addresses:
            pool_info = {"address": addr}
            try:
                pool_contract = loader.amm_pool(addr)
                # ABI uses UniswapV2-style names: token0/token1/reserve0/reserve1
                t0 = await pool_contract.functions.token0().call()
                t1 = await pool_contract.functions.token1().call()
                r0 = await pool_contract.functions.reserve0().call()
                r1 = await pool_contract.functions.reserve1().call()
                pool_info.update(
                    {
                        "token0": t0,
                        "token1": t1,
                        "reserve0": r0,
                        "reserve1": r1,
                    }
                )
            except Exception as exc:
                # Log full detail server-side only — the exception text (which
                # can include RPC/contract internals) must not flow into this
                # public health-check response (CodeQL py/stack-trace-exposure, #9).
                logging.getLogger(__name__).warning(
                    "AMM health check: failed to read pool state for %s", addr, exc_info=exc
                )
                pool_info["error"] = "failed to read pool state — see server logs"
            pools.append(pool_info)

        return {
            "status": "ok",
            "pool_count": len(pools),
            "pools": pools,
        }

    except Exception:
        logging.getLogger(__name__).exception("AMM health check failed")
        return JSONResponse(
            status_code=503,
            content={
                "status": "amm_health_check_failed",
                "reason": "AMM health check failed — see server logs.",
            },
            headers=_no_store_headers(),
        )


@app.get("/")
@limiter.exempt
async def root():
    return {
        "name": "Archimedes",
        "tagline": "Research-grounded strategy generation with a visible rigor gate on Arc public testnet.",
        "docs": _docs_url or "disabled (production)",
        # Agent-discoverability pointers (additive, backwards-compatible — see /llms.txt
        # and docs/agent-api.md for the full agent-facing contract).
        "llms_txt": "/llms.txt",
        "agent_manifest": "/api/agent/manifest",
        "agent_docs": "see /llms.txt",
    }
