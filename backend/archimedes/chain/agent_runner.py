"""Strategy runner — paper-grounded strategies drive portfolio decisions.

This IS the intelligence layer. No heuristic regime detector, no equal-weight
guessing — each strategy's published signal rule (SMA200 cross, vol-targeting,
12-month momentum) is evaluated against live market data to produce allocation
signals. Signals are aggregated into target weights, then the executor
rebalances vaults and publishes reasoning trace hashes on-chain.

Run as a standalone process:
    python -m archimedes.chain.agent_runner

Env:
    AGENT_INTERVAL_SECONDS  — tick interval in seconds (default: 300 = 5 min)
    AGENT_DRY_RUN           — if "true", skip on-chain execution (default: false)
    AGENT_VAULT_ADDRESSES   — comma-separated vault addresses to manage
    AGENT_USDC_FLOOR        — minimum USDC allocation, 0.0–1.0 (default: 0.20)
    AGENT_LEASE_TTL_MS      — exactly-once lease TTL (default: 180000 = 3min)
    AGENT_LEASE_RENEW_S     — lease renewal interval (default: 50s, ~ttl/3)
    REVEAL_RECONCILE_MAX_ATTEMPTS  — retries before a dangling commitment goes
                              TERMINAL (default: 3)
    REVEAL_RECONCILE_SCAN_LIMIT    — newest traces scanned per tick, kept as a
                              migration backstop alongside the durable index
                              (default: 200)
    REVEAL_RECONCILE_MAX_PER_TICK  — reveals retried per tick (default: 5)
    REVEAL_RECONCILE_MAX_AGE_SECONDS — independent circuit breaker (#1353): a
                              record goes TERMINAL past this age since first
                              seen dangling regardless of its attempt counter,
                              so a compound failure (a broken save_trace write
                              path on top of a broken reveal) cannot retry
                              forever just because the persisted counter never
                              advanced (default: 86400 = 24h)

Exactly-once (#1043): this runner is a funds-adjacent singleton whose
rebalance is NOT idempotent (it issues absolute swap amounts, not deltas) —
two live copies rebalancing the same vault concurrently could double-trade
it. On startup it blocks until it holds a Redis lease (``RunnerLeaseGuard``);
a second copy simply waits. The lease is renewed on an INDEPENDENT
background schedule (never gated on tick completion — a single tick can run
minutes), and every on-chain write (trade execution, commit, reveal) is
gated on the lease still being held — if it's lost mid-tick, this runner
fails closed (skips the write, keeps retrying) rather than risking a
duplicate rebalance.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid
from datetime import UTC, datetime

from archimedes.chain.client import chain_client
from archimedes.chain.constants import MAX_MANAGEMENT_FEE_BPS, MAX_PERFORMANCE_FEE_BPS
from archimedes.chain.executor import InsufficientLiquidityError, chain_executor, compute_trade_id
from archimedes.chain.oracle_updater import OracleUpdater
from archimedes.chain.trace_publisher import trace_publisher
from archimedes.chain.v_check import VCheck
from archimedes.execution import core as execution_core
from archimedes.execution.venue import ChainVenue
from archimedes.interfaces.math import IRegimeDetector
from archimedes.models.portfolio import (
    Portfolio,
    RiskProfile,
    TargetAllocation,
    TradeOrder,
)
from archimedes.models.regime import EnsembleConsensus, RegimeClassification
from archimedes.models.trace import DecisionType, ReasoningTrace
from archimedes.services.gmm_regime_detector import GmmRegimeDetector
from archimedes.services.portfolio_constructor import PortfolioConstructor
from archimedes.services.redis_state import AgentStateStore
from archimedes.services.redis_state import is_dangling_reveal as _needs_reveal_reconciliation
from archimedes.services.runner_lease import RunnerLeaseGuard
from archimedes.services.source_tracker import build_consulted_hashes
from archimedes.services.strategy_provider import default_provider
from archimedes.services.strategy_signal_evaluator import (
    StrategySignals,
    strategy_evaluator,
)
from archimedes.services.vix_regime_detector import VixRegimeDetector

logger = logging.getLogger(__name__)

INTERVAL = int(os.getenv("AGENT_INTERVAL_SECONDS", "300"))
DRY_RUN = os.getenv("AGENT_DRY_RUN", "false").lower() == "true"
EXPLICIT_VAULTS = os.getenv("AGENT_VAULT_ADDRESSES", "")
USDC_FLOOR = float(os.getenv("AGENT_USDC_FLOOR", "0.20"))

# ─── Reveal reconciliation (#1276, audit G9) ─────────────────────────
#
# A tick that trades and commits but then FAILS to reveal (revert, RPC outage,
# lease lost during the claimedExecutionTime wait) leaves a DANGLING COMMITMENT:
# the money moved, the reasoning is persisted, the commit is anchored — but
# nothing on-chain ever proves the reasoning preceded the trade. Pre-#1276 no
# later tick ever came back for it, so that trade's reasoning was permanently
# unverifiable.
#
# A later retry is legitimate — not a fabrication — because
# ReasoningTraceRegistry.reveal() imposes NO upper window. Its five guards are:
#   revealBlock == 0 (unrevealed) | msg.sender == committer |
#   block.number > commitBlock | block.timestamp >= claimedExecutionTime |
#   keccak256(fullTraceContent) == contentHash
# Every one is monotone-satisfiable later: the block/timestamp bounds are LOWER
# bounds only, and the content bound is over immutable persisted bytes. So the
# same reveal that failed at T is still accepted at T+n — verified against
# contracts/src/ReasoningTraceRegistry.sol, not assumed.
#
# The two guards that CANNOT be satisfied later are the reason "terminal" exists
# as a state: a rotated signer key (msg.sender != committer) and canonical bytes
# that no longer re-derive to the committed hash are permanent, and so is having
# no on-chain commitment id at all (a pre-v1.5 publishTrace anchor). Those must
# surface as a countable, alertable state — never as a silent retry loop.
REVEAL_RECONCILE_MAX_ATTEMPTS = int(os.getenv("REVEAL_RECONCILE_MAX_ATTEMPTS", "3"))
REVEAL_RECONCILE_SCAN_LIMIT = int(os.getenv("REVEAL_RECONCILE_SCAN_LIMIT", "200"))
REVEAL_RECONCILE_MAX_PER_TICK = int(os.getenv("REVEAL_RECONCILE_MAX_PER_TICK", "5"))
# Compound-failure circuit breaker (#1353) — see the module docstring.
REVEAL_RECONCILE_MAX_AGE_SECONDS = int(os.getenv("REVEAL_RECONCILE_MAX_AGE_SECONDS", str(24 * 3600)))

_RECONCILE_PENDING = "pending"  # retried, failed, eligible for another attempt
_RECONCILE_TERMINAL = "terminal"  # given up on — LOUD, countable, never retried
_RECONCILE_DONE = "reconciled"  # a retry landed the reveal on-chain
_RECONCILE_FROM_CHAIN = "reconciled_from_chain"  # reveal was already on-chain; DB caught up
# Any state other than "pending" (and absent) closes the record to further scans.
_RECONCILE_CLOSED_STATES = frozenset({_RECONCILE_TERMINAL, _RECONCILE_DONE, _RECONCILE_FROM_CHAIN})

# Exactly-once lease (#1043)
RUNNER_NAME = "agent_runner"
LEASE_TTL_MS = int(os.getenv("AGENT_LEASE_TTL_MS", "180000"))  # 3 min
LEASE_RENEW_INTERVAL_S = int(os.getenv("AGENT_LEASE_RENEW_S", "50"))  # ~ttl/3

# Drift threshold for rebalance trigger. The constant and the reasoning behind
# it moved to ``execution.core.DRIFT_THRESHOLD`` (#1410) so the paper venue
# cannot run a different one; re-exported under the old name because this
# module's tests and comments refer to it.
_DRIFT_THRESHOLD = execution_core.DRIFT_THRESHOLD

# The exogenous market regime is detected each tick by VixRegimeDetector
# (issue #660) and written to KEY_REGIME. This is the value used only when that
# classification cannot be produced (snapshot fetch fails or lacks VIX/MA
# signals) — the regime then degrades honestly to "unknown". Distinct from the
# *endogenous* ensemble consensus (issue #659); traces carry both.
_MARKET_REGIME_UNKNOWN = "unknown"


def _compute_confidence(all_signals: list[StrategySignals]) -> float:
    """Compute dynamic confidence from signal consensus + magnitude.

    Combines two factors (Issue #359):
    1. Vote ratio: fraction of signals that are non-flat (directional)
    2. Signal strength: average weight magnitude across all directional signals
       (higher weights = stronger conviction = higher confidence)

    Formula: confidence = vote_ratio * (0.5 + 0.5 * avg_strength)
    Range: [0.0, 1.0] — varies naturally with signal composition.
    """
    total_signals = sum(len(ss.signals) for ss in all_signals)
    if total_signals == 0:
        return 0.5  # No data — neutral confidence

    directional = [s for ss in all_signals for s in ss.signals if s.signal.value != "flat"]
    flat_count = total_signals - len(directional)
    vote_ratio = 1.0 - (flat_count / total_signals)

    # Average magnitude of directional signals (weight represents conviction)
    if directional:
        avg_strength = sum(abs(s.weight) for s in directional) / len(directional)
        # Normalize: weights are typically 0.0-1.0, but clamp for safety
        avg_strength = min(avg_strength, 1.0)
    else:
        avg_strength = 0.0

    # Weight dispersion penalty: high disagreement in weights → lower confidence
    # std-dev of weights across all signals (not just directional)
    all_weights = [s.weight for ss in all_signals for s in ss.signals]
    if len(all_weights) >= 2:
        mean_w = sum(all_weights) / len(all_weights)
        variance = sum((w - mean_w) ** 2 for w in all_weights) / len(all_weights)
        dispersion = variance**0.5  # std dev, typically 0.0-0.3
        dispersion_penalty = min(dispersion * 2, 0.3)  # cap penalty at 0.3
    else:
        dispersion_penalty = 0.0

    # Final confidence: vote_ratio × strength_boost − dispersion_penalty
    raw = vote_ratio * (0.5 + 0.5 * avg_strength) - dispersion_penalty
    return max(0.05, min(0.99, round(raw, 4)))  # clamp + round to 4dp


def _paper_hashes_from_signals(all_signals: list[StrategySignals]) -> list[str]:
    """Build consulted-paper hash list (Xia § 4.3 Source Tracking) from strategy signals.

    Uses strategy_id as the content hash — it is a SHA-256 of paper + methodology,
    so it IS a stable content fingerprint even without a separate pdf_sha256.
    """
    papers = [{"arxiv_id": ss.paper_arxiv_id or ss.strategy_id, "content_hash": ss.strategy_id} for ss in all_signals]
    return build_consulted_hashes(papers)


# _needs_reveal_reconciliation is imported (aliased) from
# archimedes.services.redis_state.is_dangling_reveal above (#1353) — that
# module now owns the canonical predicate so it can be shared with the
# save_trace index-maintenance the durable dangling-index (see
# KEY_TRACE_RECONCILE_PENDING) is built on, without the two ever drifting
# into different ideas of "dangling". The name is kept module-private and
# unchanged here so every existing call site (and the reveal-reconciliation
# test suite, which imports it by this name) is untouched.


def _merge_dangling_candidates(index_records: list[dict], scan_records: list[dict]) -> list[dict]:
    """Union the durable-index and bounded-scan candidate sets, deduped by hash.

    The durable index (#1353, ``KEY_TRACE_RECONCILE_PENDING``) is the exact
    source of truth going forward — SMEMBERS never ages a member out
    regardless of trace volume. The bounded scan (#1276,
    ``list_recent_traces``) is kept as a one-cycle migration backstop: it
    still catches a record written dangling by code that predates this index
    (and so was never added to it) — the very next time ANYTHING writes that
    record via ``save_trace`` (including this pass's own retry), the index
    takes over for it for good.
    """
    seen: set[str] = set()
    merged: list[dict] = []
    for record in (*index_records, *scan_records):
        if not isinstance(record, dict):
            continue
        key = record.get("trace_hash") or record.get("id")
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(record)
    return merged


def _rebuild_committed_trace(record: dict) -> ReasoningTrace:
    """Re-derive the EXACT committed trace object from its persisted record.

    Mirrors ``GET /traces/{id}/canonical``: every ``_HASH_FIELDS`` member is
    restored from persistence and nothing else is, because ``canonical_json()``
    is computed over exactly those fields and the contract recomputes
    ``keccak256(fullTraceContent)`` against the committed hash.

    ``timestamp`` is deliberately left as the persisted ISO STRING rather than
    re-parsed to a datetime: ``canonical_json()`` only ``isoformat()``s
    datetimes, so passing the string through reproduces the committed bytes
    exactly and side-steps any parse/format round-trip difference — which would
    surface as an on-chain "Hash mismatch" revert (#903).
    """
    return ReasoningTrace(
        id=record["id"],
        vault_address=record["vault_address"],
        decision_type=DecisionType(record["decision_type"]),
        trigger=record["trigger"],
        timestamp=record["timestamp"],
        market_context=record.get("market_context", {}),
        portfolio_before=record.get("portfolio_before", {}),
        portfolio_after=record.get("portfolio_after", {}),
        reasoning=record.get("reasoning", ""),
        confidence=record.get("confidence", 0.0),
        trades_executed=record.get("trades_executed", []),
        strategies_referenced=record.get("strategies_referenced", []),
        consulted_paper_hashes=record.get("consulted_paper_hashes", []),
    )


def _norm_hash(value: str | None) -> str:
    """Normalize a hex hash for comparison (0x-prefix and case are not content)."""
    return (value or "").removeprefix("0x").lower()


def _is_already_revealed_error(exc: Exception) -> bool:
    """True when a reveal failure is the contract saying "Already revealed".

    That revert is not a failure at all — it means the reveal DID land and only
    our off-chain write of it was lost, so it routes to the reconcile-from-chain
    backfill instead of consuming a retry attempt.
    """
    return "already revealed" in str(exc).lower()


class StrategyRunner:
    """Paper-strategy-driven portfolio runner.

    One tick:
      1. Load strategies from LocalStrategyProvider
      2. Fetch live price histories (1y daily, from yfinance)
      3. Evaluate each strategy's signal rule against prices
      4. Aggregate signals into target portfolio weights
      5. For each managed vault:
         a. Read current portfolio from on-chain
         b. Diff current vs target → trades
         c. Execute if drift > threshold
         d. Publish reasoning trace hash on Arc
      6. Persist heartbeat + state to Redis
    """

    def __init__(self) -> None:
        self.provider = default_provider()
        self.state = AgentStateStore()
        # Oracle for market snapshots (VIX + S&P MAs) feeding regime detection.
        self.oracle = OracleUpdater()
        # Exogenous market-regime classifier (issues #660, #661). Data-driven
        # 4-component Gaussian Mixture (Hamilton 1989 / Ang & Bekaert 2002 in
        # spirit) that DROPS IN for the rule-based detector — but falls back to
        # VixRegimeDetector whenever no fitted gmm_model.pkl is present, history
        # is too short (<22 days), or VIX/price are missing. Until the offline
        # fit (scripts/fit_gmm_regime.py) produces an artifact, this behaves
        # exactly like the rule-based VIX/MA classifier. Kept SEPARATE from the
        # endogenous EnsembleConsensus signal (issue #659): the two are
        # complementary; the regime says what the market is doing, the consensus
        # how decisive our strategies are.
        self.regime_detector: IRegimeDetector = GmmRegimeDetector(fallback=VixRegimeDetector())
        # Position sizer (issue #662): throttles aggregated target weights by
        # the exogenous market regime AND the endogenous ensemble consensus.
        self.portfolio_constructor: PortfolioConstructor = PortfolioConstructor()
        self._synth_addrs = chain_client.settings.synth_addresses
        self._usdc_addr = chain_client.settings.usdc_address
        # The vault side of the #1410 executor boundary. Built from the SAME
        # snapshotted address maps this runner has always used, so target
        # construction resolves exactly the addresses it did before.
        self.venue = ChainVenue(
            usdc_address=self._usdc_addr,
            synth_addresses=self._synth_addrs,
        )
        self._known_vaults: set[str] = set()  # Vaults we've already seen
        # Dedup: track last reasoning per vault to avoid publishing identical traces
        self._last_reasoning: dict[str, str] = {}  # vault_address → reasoning text
        self._last_reasoning_count: dict[str, int] = {}  # vault_address → repeat count
        # Exactly-once lease (#1043). Wired by run() for the real process
        # loop; left None for direct/unit-test construction, in which case
        # on-chain writes are NOT gated (matches every pre-#1043 test's
        # expectations — the real gate only exists once run() wires a live
        # RunnerLeaseGuard, which is the only codepath where a second
        # concurrent copy of this process can actually exist).
        self.lease: RunnerLeaseGuard | None = None

    @property
    def _lease_ok(self) -> bool:
        """Fail-closed gate for every on-chain WRITE (#1043).

        True when no lease is wired (unit tests, direct construction) OR the
        wired lease currently believes it is held. False the instant a real
        lease is lost — the only way to get a wired-but-invalid lease is via
        the real ``run()`` loop, so this can never silently disable writes
        for a test that never opted into lease coordination.
        """
        return self.lease is None or self.lease.is_valid

    async def tick(self) -> None:
        """Run one full strategy evaluation cycle."""
        tick_id = uuid.uuid4().hex[:8]
        logger.info("═══ Strategy tick %s ═══", tick_id)

        try:
            # 0. Reveal reconciliation (#1276). Runs FIRST, under the same lease
            # this tick holds, and strictly BEFORE any new commit work: a
            # dangling commitment from an earlier tick is an already-executed
            # trade whose reasoning is not yet provable, which outranks a trade
            # that has not happened yet. Its own try/except is belt-and-braces
            # on top of the pass's internal guards — repairing yesterday's trace
            # must never be able to stop today's rebalance.
            try:
                await self._reconcile_dangling_reveals(tick_id)
            except Exception:
                logger.exception("[tick %s] Reveal reconciliation pass FAILED — continuing with the tick", tick_id)

            # 1. Load strategies
            strategies = self.provider.list_strategies()
            if not strategies:
                logger.warning("[tick %s] No strategies loaded — sleeping", tick_id)
                return

            logger.info(
                "[tick %s] Loaded %d strategies: %s",
                tick_id,
                len(strategies),
                ", ".join(s.paper_title[:30] for s in strategies),
            )

            # 2. Evaluate strategy signals against live market data
            synth_assets = [sym for sym, addr in self._synth_addrs.items() if addr]

            # Run signal evaluation in thread pool (yfinance is sync)
            all_signals: list[StrategySignals] = await asyncio.to_thread(
                strategy_evaluator.evaluate_strategies,
                strategies,
                synth_assets,
            )

            if not all_signals:
                logger.warning("[tick %s] No signals produced — sleeping", tick_id)
                return

            # Log each strategy's signals
            for ss in all_signals:
                logger.info(
                    "[tick %s] %s: %s",
                    tick_id,
                    ss.paper_title[:35],
                    " | ".join(f"{s.asset}={s.signal.value}({s.weight:.0%})" for s in ss.signals),
                )

            # 3. Aggregate signals into target weights
            target_weights = strategy_evaluator.aggregate_signals(
                all_signals,
                usdc_floor=USDC_FLOOR,
            )
            logger.info(
                "[tick %s] Target weights: %s",
                tick_id,
                " | ".join(f"{k}={v:.0%}" for k, v in target_weights.items()),
            )

            # Compute the ensemble consensus (how decisive the ensemble is).
            # NOTE: flat_pct is an *endogenous* ensemble-uncertainty signal — it
            # is NOT a market regime. We keep the flat_pct computation (it is a
            # useful consensus signal) but label it honestly as agent consensus
            # rather than overloading it onto the exogenous market regime (#659).
            flat_count = sum(1 for ss in all_signals for s in ss.signals if s.signal.value == "flat")
            total_count = sum(len(ss.signals) for ss in all_signals)
            consensus = EnsembleConsensus.from_signal_counts(flat_count, total_count)

            # Exogenous market regime (issue #660). Fetch a market snapshot
            # (VIX + S&P MAs) and classify it with the rule-based detector.
            # This is INDEPENDENT of the endogenous EnsembleConsensus above —
            # the regime says what the *market* is doing, the consensus says
            # how decisive *our strategies* are. Traces carry both.
            regime_classification, market_regime = await self._classify_market_regime(tick_id)

            # Persist the exogenous market regime under KEY_REGIME so the
            # /api/regime/current surface returns the *real* detector output
            # (it falls back to ensemble consensus only while this is absent —
            # see regime_routes.get_current_regime). Guarded: the classifier
            # returns None on snapshot-fetch failure or missing regime signals.
            if regime_classification is not None:
                await self.state.save_regime(regime_classification)

            # Persist the ensemble consensus under its OWN Redis key so it does
            # not shadow the market regime.
            await self.state.save_ensemble_consensus(consensus, all_signals)

            # 4. Build target allocations — throttle raw weights by the
            # exogenous market regime AND the endogenous ensemble consensus
            # (issue #662), then resolve symbols → token addresses.
            allocations = self.portfolio_constructor.construct(
                risk_profile=RiskProfile.MODERATE,
                strategies=strategies,
                backtest_results={},
                regime=regime_classification,
                ensemble_consensus=consensus,
                base_weights=target_weights,
            )
            targets = self._weights_to_targets({a.symbol: a.weight for a in allocations}, all_signals)

            # 5. Get managed vaults (polling VaultFactory discovers new vaults)
            vaults = await self._get_managed_vaults()

            # 5a. Discover new vaults from VaultFactory and add to state
            new_vaults = await self._discover_new_vaults()
            if new_vaults:
                logger.info(
                    "[tick %s] Discovered %d new vault(s): %s",
                    tick_id,
                    len(new_vaults),
                    ", ".join(v[:10] for v in new_vaults),
                )
                # Merge discovered vaults into the managed set
                vaults = list(set(vaults) | set(new_vaults))

            if not vaults:
                logger.warning("[tick %s] No vaults available — sleeping", tick_id)
                return

            logger.info("[tick %s] Managing %d vaults", tick_id, len(vaults))

            # 5b. Rebalancer decouple (Part A #1,
            # docs/CURATED-STRATEGY-DECOUPLE-AND-CONSOLIDATE-2026-07-08.md):
            # `all_signals` above is CURATED-ONLY (self.provider.list_strategies()).
            # The per-vault scoping below (Issue #307) filters `all_signals` down
            # to each vault's bound strategy_ids — a vault bound to a GENERATED
            # strategy_id has never appeared in `all_signals`, so it produced no
            # scoped signals and was silently skipped ("none produced signals").
            # Union every managed vault's bound ids (skipping legacy vaults with
            # no VaultMetadata — None), subtract the curated ids already
            # evaluated, and load+evaluate any remaining ids that carry a
            # persisted DSL `strategy_spec` in strategy_store. This does NOT
            # touch the curated global-ensemble weights/targets computed above —
            # it only enriches `signals_pool`, the SEPARATE list the per-vault
            # scoping below reads from. `all_signals` itself must stay
            # curated-only and immutable past this point: the legacy-vault
            # (no-VaultMetadata) path passes `all_signals` into _process_vault,
            # where it feeds _build_reasoning and every _publish_trace call —
            # extending it in place would let other users' generated-strategy
            # signals contaminate a legacy vault's published reasoning trace
            # (a claim-integrity violation, and a leak of private strategy ids
            # into someone else's on-chain-anchored provenance).
            # Fail-safe: a broken/invalid generated spec (bad JSON, DB error,
            # unexpected shape) must never break the curated tick or the
            # per-vault loop — logged and swallowed, never raised.
            #
            # Cache each vault's bound strategy_ids here so step 6's per-vault
            # loop below can reuse it instead of re-querying VaultMetadata a
            # second time per vault per tick (`_get_vault_strategy_ids` opens
            # its own DB session per call). Built unconditionally, ahead of
            # the try/except: `_get_vault_strategy_ids` already never raises
            # (its own internal try/except returns None on failure), so this
            # comprehension can't be short-circuited by the exception handler
            # below — every vault's entry is guaranteed to land in the cache.
            vault_strategy_ids_cache: dict[str, list[str] | None] = {
                vault_addr: self._get_vault_strategy_ids(vault_addr) for vault_addr in vaults
            }
            signals_pool: list[StrategySignals] = all_signals
            try:
                curated_ids = {ss.strategy_id for ss in all_signals}
                bound_ids: set[str] = set()
                for vault_ids in vault_strategy_ids_cache.values():
                    if vault_ids:
                        bound_ids.update(vault_ids)
                generated_ids = bound_ids - curated_ids
                if generated_ids:
                    generated_signals = await asyncio.to_thread(
                        self._load_generated_strategy_signals,
                        generated_ids,
                        synth_assets,
                    )
                    if generated_signals:
                        logger.info(
                            "[tick %s] Loaded %d generated-strategy signal set(s): %s",
                            tick_id,
                            len(generated_signals),
                            ", ".join(ss.strategy_id[:10] for ss in generated_signals),
                        )
                        signals_pool = all_signals + generated_signals  # new list — never mutate all_signals
            except Exception:
                logger.exception(
                    "[tick %s] Generated-strategy signal load failed — continuing with curated signals only",
                    tick_id,
                )

            # 6. Process each vault with per-vault strategy scoping (Issue #307)
            for vault_addr in vaults:
                try:
                    # Per-vault scoping: each vault executes only its selected
                    # strategies (looked up once, above, and cached for reuse here).
                    vault_strategy_ids = vault_strategy_ids_cache.get(vault_addr)

                    if vault_strategy_ids is None:
                        # Legacy vault (deployed before strategy-selection flow
                        # shipped, or via tooling that doesn't write VaultMetadata)
                        # — fall back to global consensus so existing vaults keep
                        # rebalancing. Vaults created via the UI's strategy-selection
                        # flow set strategy_ids and follow the scoped path below.
                        logger.info(
                            "[tick %s] Vault %s: no VaultMetadata (legacy) — using global consensus",
                            tick_id,
                            vault_addr[:10],
                        )
                        await self._process_vault(
                            vault_addr,
                            targets,
                            all_signals,
                            market_regime,
                            consensus,
                            tick_id,
                        )
                        continue

                    # Filter signals to this vault's strategies (from the
                    # generated-enriched pool, not curated-only all_signals)
                    scoped_signals = [ss for ss in signals_pool if ss.strategy_id in vault_strategy_ids]

                    if not scoped_signals:
                        logger.warning(
                            "[tick %s] Vault %s strategy_ids=%s — none produced signals, skipping",
                            tick_id,
                            vault_addr[:10],
                            vault_strategy_ids,
                        )
                        continue

                    # Aggregate scoped signals into per-vault target weights,
                    # then throttle by regime + ensemble consensus (issue #662).
                    vault_weights = strategy_evaluator.aggregate_signals(
                        scoped_signals,
                        usdc_floor=USDC_FLOOR,
                    )
                    vault_allocations = self.portfolio_constructor.construct(
                        risk_profile=RiskProfile.MODERATE,
                        strategies=strategies,
                        backtest_results={},
                        regime=regime_classification,
                        ensemble_consensus=consensus,
                        base_weights=vault_weights,
                    )
                    vault_targets = self._weights_to_targets(
                        {a.symbol: a.weight for a in vault_allocations}, scoped_signals
                    )

                    logger.info(
                        "[tick %s] Vault %s: scoped to %d/%d strategies → %s",
                        tick_id,
                        vault_addr[:10],
                        len(scoped_signals),
                        len(signals_pool),
                        " | ".join(f"{k}={v:.0%}" for k, v in vault_weights.items()),
                    )

                    await self._process_vault(
                        vault_addr,
                        vault_targets,
                        scoped_signals,
                        market_regime,
                        consensus,
                        tick_id,
                    )
                except Exception:
                    logger.exception(
                        "[tick %s] Error processing vault %s — continuing",
                        tick_id,
                        vault_addr,
                    )

            # 7. Heartbeat
            await self.state.save_heartbeat()

        except Exception:
            logger.exception("[tick %s] Strategy tick failed — will retry", tick_id)

    # ─── Exogenous market-regime classification (Issue #660) ─────────

    async def _classify_market_regime(self, tick_id: str) -> tuple[RegimeClassification | None, str]:
        """Fetch a market snapshot and classify the exogenous market regime.

        Returns ``(classification, regime_value)``. On any failure — snapshot
        fetch error, or a snapshot without enough regime signals (VIX / S&P
        MAs unavailable) — degrades gracefully to ``(None, "unknown")`` and
        logs a warning rather than crashing the tick.
        """
        try:
            snapshot = await self.oracle.fetch_market_snapshot()
        except Exception as e:
            logger.warning(
                "[tick %s] Market snapshot fetch failed (%s) — regime=unknown",
                tick_id,
                e,
            )
            return None, _MARKET_REGIME_UNKNOWN

        if not snapshot.has_regime_signals:
            logger.warning(
                "[tick %s] Snapshot missing regime signals (VIX/MA unavailable) — regime=unknown",
                tick_id,
            )
            return None, _MARKET_REGIME_UNKNOWN

        try:
            classification = self.regime_detector.classify(snapshot)
        except Exception as e:
            logger.warning(
                "[tick %s] Regime classification failed (%s) — regime=unknown",
                tick_id,
                e,
            )
            return None, _MARKET_REGIME_UNKNOWN

        logger.info(
            "[tick %s] Market regime: %s (confidence=%.2f, VIX=%.1f, changed=%s)",
            tick_id,
            classification.regime.value,
            classification.confidence,
            classification.signals.vix_level,
            classification.regime_changed,
        )
        return classification, classification.regime.value

    # ─── Per-vault strategy scoping (Issue #307) ─────────────────────

    def _get_vault_strategy_ids(self, vault_address: str) -> list[str] | None:
        """Load strategy_ids from VaultMetadata for a vault.

        Returns:
            list[str] — the user's selected strategies for this vault.
            None — if no metadata exists (vault deployed outside UI).
        """
        try:
            from archimedes.db import get_session
            from archimedes.models.chat import VaultMetadata

            with get_session() as session:
                # Casing fix (issue #1028): stored lowercase — see
                # vaults_routes.store_vault_metadata; vault_address here comes
                # from the on-chain (EIP-55 checksummed) vault list.
                meta = session.query(VaultMetadata).filter(VaultMetadata.vault_address == vault_address.lower()).first()
                if meta is None:
                    return None
                ids = meta.get_strategy_ids()
                return ids if ids else None
        except Exception as exc:
            logger.debug("_get_vault_strategy_ids(%s) failed: %s", vault_address[:10], exc)
            return None

    def _load_generated_strategy_signals(
        self, generated_ids: set[str], synth_assets: list[str]
    ) -> list[StrategySignals]:
        """Load + evaluate generated strategies bound to a vault (Part A #1).

        Queries ``strategy_store`` for the given ids, keeps only rows that
        carry a persisted DSL ``strategy_spec`` (a generated strategy
        persisted before that column existed, or one whose spec text is
        corrupt, has nothing executable — skipped, same as today's silent
        skip, just now scoped to exactly the ids that can't be evaluated),
        adapts each surviving row to a ``StrategyPassport`` via
        ``StrategyRecord.to_strategy_passport()`` (the same dataclass shape
        curated strategies flow through — no bespoke carrier needed), and runs
        them through the SAME ``strategy_evaluator.evaluate_strategies`` the
        curated strategies use. That evaluator already dispatches on
        ``strategy.strategy_spec`` into the DSL interpretation path
        (``_spec_signal``), which itself never raises on an invalid spec — it
        logs and returns a FLAT signal. Any exception raised here (DB error,
        malformed JSON, unexpected row shape) is caught PER RECORD so one bad
        row cannot suppress the other, evaluable, generated strategies; the
        caller (``tick()``) additionally wraps this whole call for
        belt-and-suspenders safety.

        Runs synchronously (DB query + yfinance-backed evaluator) — called via
        ``asyncio.to_thread`` from ``tick()``, matching the curated
        ``evaluate_strategies`` call above it.
        """
        from archimedes.db import get_session
        from archimedes.models.strategy_store import StrategyRecord

        gen_strategies = []
        with get_session() as session:
            records = (
                session.query(StrategyRecord)
                .filter(StrategyRecord.id.in_(generated_ids))
                .filter(StrategyRecord.strategy_spec.isnot(None))
                .all()
            )
            for record in records:
                try:
                    passport = record.to_strategy_passport()
                except Exception:
                    logger.warning(
                        "generated strategy %s: failed to adapt to StrategyPassport — skipping",
                        record.id,
                        exc_info=True,
                    )
                    continue
                if not passport.strategy_spec:
                    # Spec text parsed but was empty/falsy (e.g. "{}" or "null")
                    # — nothing executable, same honest skip as no spec at all.
                    continue
                gen_strategies.append(passport)

        if not gen_strategies:
            return []

        return strategy_evaluator.evaluate_strategies(gen_strategies, synth_assets)

    async def _vault_fee_cap_refusal(self, vault_address: str) -> str | None:
        """Fail-closed fee-cap gate for the agent's OWN state-changing call.

        Issue #1138 follow-up (flagged by Dan on PR #1139's review): the
        marketplace guard in ``marketplace_routes.py``/``vault_service.py``
        only covers the surfaces a human reaches (publish/subscribe/list/
        detail) — it never touched this loop, so an over-cap vault stayed
        fully tradeable by the agent, which is the actual state-changing
        path that triggers ``_accrueFees()`` in ``Vault.sol`` (rebalance()).
        Read once here, right before Phase 2 TRADE (not earlier in
        ``_process_vault``, since a no-drift tick never reaches this point
        anyway — no extra RPC cost on the common path).

        Returns a SKIP reason string when the vault must be refused, else
        None. Mirrors ``_lease_ok``'s fail-closed shape: unreadable fees
        refuse the same as over-cap fees, never silently proceed.
        """
        try:
            mgmt_fee_bps, perf_fee_bps = await chain_executor.get_vault_fee_bps(vault_address)
        except Exception as exc:
            return f"Could not verify on-chain fees for vault {vault_address[:10]}: {exc} — refusing (fail-closed)"
        if mgmt_fee_bps > MAX_MANAGEMENT_FEE_BPS or perf_fee_bps > MAX_PERFORMANCE_FEE_BPS:
            return (
                f"Vault {vault_address[:10]} fees exceed caps: "
                f"managementFeeBps={mgmt_fee_bps} (cap {MAX_MANAGEMENT_FEE_BPS}), "
                f"performanceFeeBps={perf_fee_bps} (cap {MAX_PERFORMANCE_FEE_BPS})"
            )
        return None

    async def _process_vault(
        self,
        vault_address: str,
        targets: list[TargetAllocation],
        all_signals: list[StrategySignals],
        market_regime: str,
        consensus: EnsembleConsensus,
        tick_id: str,
    ) -> None:
        """Process a single vault: read portfolio → diff → maybe trade → publish trace.

        ``market_regime`` is the exogenous regime (``"unknown"`` until a detector
        is wired); ``consensus`` is the endogenous ensemble consensus (#659).
        """
        logger.info("[tick %s] Processing vault %s", tick_id, vault_address[:10])

        # Read current portfolio
        try:
            portfolio = await chain_executor.read_portfolio(vault_address)
        except Exception as e:
            logger.warning(
                "[tick %s] Cannot read portfolio for %s: %s — skipping",
                tick_id,
                vault_address[:10],
                e,
            )
            return

        logger.info(
            "[tick %s] Vault %s: AUM=$%.2f, %d holdings",
            tick_id,
            vault_address[:10],
            portfolio.total_value_usdc,
            len(portfolio.holdings),
        )

        # Skip empty vaults (don't spam traces — just log)
        if portfolio.total_value_usdc <= 0 and not portfolio.holdings:
            logger.info("[tick %s] Vault %s is empty — needs deposit", tick_id, vault_address[:10])
            # Only publish a trace if we haven't already for this vault
            last_trace = await self.state.get_last_trace(vault_address)
            if not last_trace or last_trace.get("trigger") != "empty_vault":
                await self._publish_trace(
                    vault_address,
                    DecisionType.SKIP,
                    "empty_vault",
                    portfolio,
                    [],
                    all_signals,
                    market_regime,
                    consensus,
                    tick_id,
                    "Vault is empty — awaiting initial deposit.",
                )
            return

        # Set oracle addresses for NAV pricing (so totalAssets() prices all holdings)
        try:
            oracle_tokens = []
            oracle_addrs = []
            for t in targets:
                if t.weight > 0 and t.token_address:
                    symbol = t.symbol
                    if symbol == "USDC":
                        continue
                    oracle_addr = chain_client.settings.oracle_addresses.get(symbol)
                    if oracle_addr:
                        oracle_tokens.append(t.token_address)
                        oracle_addrs.append(oracle_addr)

            if oracle_tokens:
                await chain_executor.set_token_oracles(
                    vault_address,
                    oracle_tokens,
                    oracle_addrs,
                )
                logger.info(
                    "[tick %s] Set %d token oracles on vault %s",
                    tick_id,
                    len(oracle_tokens),
                    vault_address[:10],
                )
        except Exception as e:
            logger.warning(
                "[tick %s] Failed to set token oracles on %s: %s",
                tick_id,
                vault_address[:10],
                e,
            )

        # Set target allocations on the vault first (needed for rebalance)
        try:
            alloc_tokens = []
            alloc_weights = []
            for t in targets:
                if t.weight > 0 and t.token_address:
                    alloc_tokens.append(t.token_address)
                    alloc_weights.append(int(t.weight * 10000))  # BPS

            if alloc_tokens:
                # Normalize to exactly 10000 BPS
                total_bps = sum(alloc_weights)
                if total_bps > 0 and total_bps != 10000:
                    scale = 10000 / total_bps
                    alloc_weights = [int(round(w * scale)) for w in alloc_weights]
                    # Fix rounding residue
                    diff = 10000 - sum(alloc_weights)
                    if diff != 0 and alloc_weights:
                        alloc_weights[0] += diff

                await chain_executor.set_target_allocations(
                    vault_address,
                    alloc_tokens,
                    alloc_weights,
                )
                logger.info(
                    "[tick %s] Set target allocations on vault %s",
                    tick_id,
                    vault_address[:10],
                )
        except Exception as e:
            logger.warning(
                "[tick %s] Failed to set allocations on %s: %s",
                tick_id,
                vault_address[:10],
                e,
            )
            # Continue anyway — try rebalance with existing allocations

        trades = self._compute_trades(portfolio, targets)

        if not trades:
            logger.info("[tick %s] No drift — portfolio aligned with strategy signals", tick_id)
            # Deduplicate identical aligned decisions: when the portfolio stays
            # within drift thresholds tick after tick with the same reasoning,
            # re-anchoring the SKIP trace wastes gas and clutters the Reasoning
            # page. Increment a repeat counter and skip the publish instead.
            aligned_reasoning = self._build_reasoning(all_signals, market_regime, consensus, trades, portfolio)
            if self._last_reasoning.get(vault_address) == aligned_reasoning:
                count = self._last_reasoning_count.get(vault_address, 1) + 1
                self._last_reasoning_count[vault_address] = count
                logger.info(
                    "[tick %s] Vault %s: identical aligned decision (repeat #%d) — skipping trace publish",
                    tick_id,
                    vault_address[:10],
                    count,
                )
                return
            self._last_reasoning[vault_address] = aligned_reasoning
            self._last_reasoning_count[vault_address] = 1
            await self._publish_trace(
                vault_address,
                DecisionType.SKIP,
                "aligned",
                portfolio,
                [],
                all_signals,
                market_regime,
                consensus,
                tick_id,
                "Portfolio aligned with strategy signals. No rebalance needed.",
            )
            return

        # Log the trade plan
        logger.info(
            "[tick %s] REBALANCE vault %s: %d trades (market_regime=%s, consensus=%s)",
            tick_id,
            vault_address[:10],
            len(trades),
            market_regime,
            consensus.label.value,
        )
        for t in trades:
            logger.info(
                "[tick %s]   %s %s ~$%.0f",
                tick_id,
                t.direction.value,
                t.symbol,
                t.estimated_usdc_value,
            )

        # ── V_check — Xia et al. 2026 § 5 Reasoning I/O contract ───
        # Deterministic validity gate: if weights are invalid, SKIP the
        # entire rebalance regardless of LLM confidence.
        alloc_weights_bps: dict[str, int] = {}
        for t in targets:
            if t.weight > 0 and t.token_address:
                alloc_weights_bps[t.symbol] = int(round(t.weight * 10000))
        # Fix float→int rounding drift: individually rounded weights can
        # sum to 10001 or 9999 instead of 10000. Adjust the largest weight
        # to absorb the residual (max 1 BPS correction).
        if alloc_weights_bps:
            residual = sum(alloc_weights_bps.values()) - 10000
            if residual != 0:
                largest = max(alloc_weights_bps, key=alloc_weights_bps.get)
                alloc_weights_bps[largest] -= residual
        v_check = VCheck(weights_bps=alloc_weights_bps)
        v_result = v_check.run()
        if not v_result.passed:
            logger.warning(
                "[tick %s] V_check FAILED for vault %s: %s — skipping rebalance",
                tick_id,
                vault_address[:10],
                "; ".join(v_result.failures),
            )
            await self._publish_trace(
                vault_address,
                DecisionType.SKIP,
                "v_check_failed",
                portfolio,
                [],
                all_signals,
                market_regime,
                consensus,
                tick_id,
                f"V_check rejected: {'; '.join(v_result.failures)}",
            )
            return

        # ── Commit-Reveal Flow ────────────────────────────────────
        # Phase 0: SIZE — build the EXACT on-chain trade arrays (tokensIn,
        # amountsIn, tokensOut, amountsOut) BEFORE committing, and derive the
        # tradeId Vault.rebalance() will independently recompute from them
        # (keccak256(abi.encode(...)) — Vault.sol). This must happen before
        # the commit and the SAME arrays must be reused (not recomputed) for
        # execute_trades(): SELL-leg sizing reads a live oracle price, so two
        # independent builds could read two different prices and mint two
        # different tradeIds — the on-chain recompute would then miss the
        # commitment and rebalance() reverts (#588 / commit-before-trade guard).
        trade_arrays: tuple[list[str], list[int], list[str], list[int]] | None = None
        trade_id: bytes | None = None

        if not DRY_RUN:
            try:
                trade_arrays = await chain_executor.build_trade_arrays(vault_address, trades)
            except InsufficientLiquidityError as e:
                logger.warning(
                    "[tick %s] Vault %s: swap skipped — thin pool: %s; will retry next tick",
                    tick_id,
                    vault_address[:10],
                    e,
                )
                await self._publish_trace(
                    vault_address,
                    DecisionType.SKIP,
                    "insufficient_liquidity",
                    portfolio,
                    [],
                    all_signals,
                    market_regime,
                    consensus,
                    tick_id,
                    f"Swap skipped — thin pool: {e}",
                )
                return
            except Exception as e:
                logger.error(
                    "[tick %s] Trade sizing FAILED for %s: %s",
                    tick_id,
                    vault_address[:10],
                    e,
                )
                await self._publish_trace(
                    vault_address,
                    DecisionType.SKIP,
                    "execution_failed",
                    portfolio,
                    [],
                    all_signals,
                    market_regime,
                    consensus,
                    tick_id,
                    f"Trade sizing failed: {e}",
                    error=str(e),
                )
                return

            tokens_in, amounts_in, tokens_out, amounts_out = trade_arrays
            if not tokens_in and not tokens_out:
                # Mirrors #925: nothing left to swap once cash/USDC legs and
                # zero-value legs are filtered out. Skip before ever committing —
                # there is no trade to bind a tradeId to.
                logger.info(
                    "[tick %s] SKIP rebalance for %s: no non-cash legs after filtering",
                    tick_id,
                    vault_address[:10],
                )
                await self._publish_trace(
                    vault_address,
                    DecisionType.SKIP,
                    "no_tradeable_legs",
                    portfolio,
                    [],
                    all_signals,
                    market_regime,
                    consensus,
                    tick_id,
                    "No non-cash legs after filtering — nothing to trade.",
                )
                return

            trade_id = compute_trade_id(tokens_in, amounts_in, tokens_out, amounts_out)

        # Phase 1: COMMIT — compute hash and anchor on-chain BEFORE trade
        reasoning = self._build_reasoning(all_signals, market_regime, consensus, trades, portfolio)

        # A rebalance always publishes — trades are real actions that must be
        # anchored, so identical reasoning is never deduplicated here. Record
        # this decision so the next aligned (no-trade) tick dedups against the
        # most recently published reasoning (the no-trade branch above is the
        # only place identical decisions are skipped).
        self._last_reasoning[vault_address] = reasoning
        self._last_reasoning_count[vault_address] = 1

        commit_tx = None
        commit_block = None
        commit_trace_id: int | None = None

        # Build + commit the canonical trace ONCE. The same trace object is revealed
        # after settlement so the on-chain keccak256 binding holds. In dry-run we still
        # build it (no on-chain commit) so the reveal/persist path has a trace to use.
        (
            committed_trace,
            commit_trace_id,
            commit_tx,
            commit_block,
            claimed_execution_time,
            commit_reverted,
        ) = await self._commit_trace(
            vault_address,
            trades,
            all_signals,
            market_regime,
            consensus,
            tick_id,
            reasoning,
            portfolio,
            targets,
            trade_id,
        )

        # ── Commit-Reveal guard: if the registry supports commit-reveal but the
        # commit above did NOT land — either no tx was ever sent (commit_tx is
        # None) OR it was sent and CONFIRMED REVERTED on-chain (commit_reverted)
        # — submitting the trade anyway would revert on-chain post-#588-redeploy
        # ("no matching commitment") — wasting gas and leaving an unrevealed
        # commitment attempt. A confirmed revert still has a real commit_tx
        # (kept for the diagnostic trail), so commit_tx is None alone is NOT a
        # reliable "did the commit land" signal — commit_reverted is (#1095
        # review). Short-circuit with a SKIP trace instead of proceeding to
        # Phase 2. Gated on the SAME `not DRY_RUN` condition the commit path
        # uses above, so DRY_RUN (where trade_id/commit_tx are always None by
        # design) is unaffected. Registries that don't support commit-reveal
        # (legacy publishTrace-only) keep the old proceed-to-trade behavior —
        # this only blocks when commit-reveal IS supported and the commit failed.
        if not DRY_RUN and trace_publisher.supports_commit_reveal() and (commit_tx is None or commit_reverted):
            logger.warning(
                "[tick %s] Vault %s: commit-reveal supported but commit did not land "
                "(commit_tx=%s, reverted=%s) — skipping trade to avoid an on-chain "
                "revert against a missing commitment.",
                tick_id,
                vault_address[:10],
                commit_tx,
                commit_reverted,
            )
            await self._publish_trace(
                vault_address,
                DecisionType.SKIP,
                "commit_failed",
                portfolio,
                [],
                all_signals,
                market_regime,
                consensus,
                tick_id,
                "Commit-reveal supported but commit failed — skipping trade to avoid "
                "an on-chain revert against a missing commitment.",
                commit_tx=commit_tx,
                commit_block=commit_block,
            )
            return

        # Fee-cap guard (issue #1138 follow-up): refuse to submit the trade if
        # this vault's immutable on-chain fees exceed the #1129 caps, or if
        # they can't be read. rebalance() is what triggers Vault.sol's
        # _accrueFees() — gating it here closes the gap the marketplace-only
        # guard left open (see _vault_fee_cap_refusal docstring). Skipped
        # under DRY_RUN, matching the _lease_ok check right below: no real
        # write happens either way.
        if not DRY_RUN:
            fee_skip_reason = await self._vault_fee_cap_refusal(vault_address)
            if fee_skip_reason is not None:
                logger.error("[tick %s] %s", tick_id, fee_skip_reason)
                await self._publish_trace(
                    vault_address,
                    DecisionType.SKIP,
                    "fee_cap_exceeded",
                    portfolio,
                    [],
                    all_signals,
                    market_regime,
                    consensus,
                    tick_id,
                    fee_skip_reason,
                    commit_tx=commit_tx,
                    commit_block=commit_block,
                )
                return

        # Phase 2: TRADE — execute the rebalance, submitting the SAME arrays
        # (trade_arrays) the tradeId above was derived from — never recomputed.
        if DRY_RUN:
            logger.info("[tick %s] DRY RUN — skipping on-chain execution", tick_id)
            tx_hashes: list[str] = []
            trade_block = None
        elif not self._lease_ok:
            # #1043 fail-closed: the exactly-once lease is not currently held
            # (lost mid-tick, or never acquired) — this is NOT idempotent
            # (absolute swap amounts), so a second live copy trading the same
            # drift would double-execute. Skip the write, log loudly, and let
            # the next tick retry once the lease guard has re-acquired.
            logger.error(
                "[tick %s] LEASE NOT HELD — skipping trade execution for vault %s this cycle "
                "(fail-closed; rebalance is not idempotent)",
                tick_id,
                vault_address[:10],
            )
            await self._publish_trace(
                vault_address,
                DecisionType.SKIP,
                "lease_not_held",
                portfolio,
                [],
                all_signals,
                market_regime,
                consensus,
                tick_id,
                "Exactly-once lease not held — trade execution skipped this cycle (fail-closed).",
                commit_tx=commit_tx,
                commit_block=commit_block,
            )
            return
        else:
            try:
                tx_hashes = await chain_executor.execute_trades(vault_address, trades, trade_arrays=trade_arrays)
                # Get trade block number from first tx
                trade_block = None
                if tx_hashes:
                    try:
                        receipt = await chain_client.w3.eth.get_transaction_receipt(
                            chain_client.w3.to_bytes(hexstr=tx_hashes[0].removeprefix("0x"))
                        )
                        trade_block = receipt.blockNumber
                    except Exception:
                        logger.debug("trade receipt block lookup failed", exc_info=True)
                logger.info(
                    "[tick %s] Executed %d trades: %s",
                    tick_id,
                    len(tx_hashes),
                    [h[:16] for h in tx_hashes],
                )
                await self.state.save_last_rebalance(vault_address)
            except InsufficientLiquidityError as e:
                # Defensive fallback — liquidity was already validated in the SIZE
                # phase above with the same trade_arrays, so this should not
                # normally trigger when trade_arrays is provided to execute_trades.
                logger.warning(
                    "[tick %s] Vault %s: swap skipped — thin pool: %s; will retry next tick",
                    tick_id,
                    vault_address[:10],
                    e,
                )
                await self._publish_trace(
                    vault_address,
                    DecisionType.SKIP,
                    "insufficient_liquidity",
                    portfolio,
                    [],
                    all_signals,
                    market_regime,
                    consensus,
                    tick_id,
                    f"Swap skipped — thin pool: {e}",
                    commit_tx=commit_tx,
                    commit_block=commit_block,
                )
                return
            except Exception as e:
                logger.error(
                    "[tick %s] Trade execution FAILED for %s: %s",
                    tick_id,
                    vault_address[:10],
                    e,
                )
                await self._publish_trace(
                    vault_address,
                    DecisionType.SKIP,
                    "execution_failed",
                    portfolio,
                    [],
                    all_signals,
                    market_regime,
                    consensus,
                    tick_id,
                    f"Execution failed: {e}",
                    commit_tx=commit_tx,
                    commit_block=commit_block,
                    error=str(e),
                )
                return

        # Phase 3: REVEAL — pin public provenance + reveal the SAME committed trace
        await self._reveal_trace(
            committed_trace,
            commit_trace_id,
            tick_id,
            tx_hashes,
            commit_tx=commit_tx,
            commit_block=commit_block,
            trade_block=trade_block,
            claimed_execution_time=claimed_execution_time,
        )

    # ─── Signal → target allocation ───────────────────────────────

    def _weights_to_targets(
        self, weights: dict[str, float], all_signals: list[StrategySignals] | None = None
    ) -> list[TargetAllocation]:
        """Convert weight dict → TargetAllocation list.

        Delegates to the venue-independent core (#1410). The body that used to
        live here now runs for the paper venue too, which is the whole point —
        a copy kept here would be free to drift from the one paper trades on.
        """
        return execution_core.weights_to_targets(weights, all_signals, self.venue)

    # ─── Trade computation ─────────────────────────────────────────

    def _compute_trades(
        self,
        portfolio: Portfolio,
        targets: list[TargetAllocation],
    ) -> list[TradeOrder]:
        """Diff current portfolio vs target weights → trade list.

        Delegates to ``execution.core.compute_trades`` (#1410) — same body, same
        ``DRIFT_THRESHOLD``, now shared with the paper venue and, via the same
        import, the marketplace path (#1719).
        """
        return execution_core.compute_trades(portfolio, targets)

    # ─── Commit-Reveal Trace ────────────────────────────────────

    # How far in the future the committed execution is claimed to land. The
    # contract requires claimedExecutionTime > commit-block timestamp AND
    # reveal-block timestamp >= claimedExecutionTime, so the lead is the
    # minimum commit->reveal separation: bigger lead = stronger "content was
    # fixed well before reveal" proof, but the reveal phase must wait the
    # remainder of this window out (see _reveal_trace) — raising it stalls the
    # tick longer, it does NOT make reveals safer (#903). Must stay well under
    # AGENT_INTERVAL_SECONDS. Trades settle in seconds on Arc, so 60s already
    # upper-bounds real execution latency; override via env if that changes.
    _COMMIT_EXECUTION_LEAD_S = int(os.getenv("TRACE_COMMIT_EXECUTION_LEAD_S", "60"))

    # Clock-skew allowance added on top of claimedExecutionTime before the
    # reveal is submitted, so the reveal block's timestamp cannot land behind
    # the local clock and trip "Reveal before claimed execution".
    _REVEAL_SKEW_BUFFER_S = 3

    async def _commit_trace(
        self,
        vault_address: str,
        trades: list[TradeOrder],
        all_signals: list[StrategySignals],
        market_regime: str,
        consensus: EnsembleConsensus,
        tick_id: str,
        reasoning: str,
        portfolio: Portfolio,
        targets: list[TargetAllocation],
        trade_id: bytes | None,
    ) -> tuple[ReasoningTrace, int | None, str | None, int | None, int | None, bool]:
        """Commit phase: anchor the trace hash on-chain BEFORE the trade executes.

        Builds the canonical trace ONCE and commits its keccak256 via the registry's
        real ``commit()`` (with claimedExecutionTime and tradeId) so the same bytes
        can be revealed after settlement and the on-chain hash binding holds. There is
        no anchor of last resort: if the deployed registry has no ``commit()`` the tick
        anchors NOTHING and logs it loudly — the v1 ``publishTrace`` fallback was
        removed once the #588 redeploy landed (#714).

        Every hashed field — including ``portfolio_after`` — is FINAL here: mutating
        any of them after this point changes ``canonical_json()`` and the on-chain
        ``keccak256(fullTraceContent) == contentHash`` reveal check reverts with
        "Hash mismatch" (#903). ``portfolio_after`` therefore records the *intended*
        post-trade allocation (deterministic, known pre-trade); actual settlement tx
        hashes live in non-hashed fields and the off-chain record only.

        Args:
            trade_id: 32-byte tradeId (``executor.compute_trade_id``) derived from the
                EXACT arrays that will be submitted to ``execute_trades()`` for this
                rebalance — required on the real commit-reveal path (the caller
                computes it in the SIZE phase before calling this). Only ``None`` on
                the DRY_RUN path, where no on-chain commit is made at all.

        Returns (trace, on_chain_trace_id, commit_tx_hash, commit_block_number,
        claimed_execution_time, reverted). The trace is always returned so reveal()
        can submit the identical content; trace_id is None when commit-reveal is
        unavailable, and so are commit_tx/commit_block — nothing was anchored at all,
        rather than a v1 anchor standing in for a commitment (#714);
        claimed_execution_time is non-None only
        on the real commit-reveal path so the reveal phase knows how long to wait.
        ``reverted`` is True only on a CONFIRMED on-chain revert of the commit tx —
        a reverted commit still has a real commit_tx_hash (diagnostic trail), so
        callers gating Phase 2 trade execution MUST check ``reverted`` too, not
        just ``commit_tx is None`` (#1095 review).
        """
        trace = ReasoningTrace(
            id=str(uuid.uuid4()),
            vault_address=vault_address,
            decision_type=DecisionType.REBALANCE,
            trigger="strategy_signal_drift",
            timestamp=datetime.now(UTC),
            market_context={
                "regime": market_regime,
                "ensemble_consensus": consensus.label.value,
                "ensemble_flat_pct": round(consensus.flat_pct, 4),
                "strategy_count": len(all_signals),
            },
            portfolio_before={
                "vault": vault_address[:10],
                "aum_usdc": portfolio.total_value_usdc,
                "holdings": {
                    h.symbol: {"weight": f"{h.weight:.1%}", "value_usdc": h.value_usdc} for h in portfolio.holdings
                },
            },
            # Hashed field, so it must be knowable pre-trade: the target
            # allocation this rebalance intends to reach. Settlement tx hashes
            # are recorded post-trade in non-hashed fields (#903).
            portfolio_after={
                "intended": True,
                "target_weights": {t.symbol: round(t.weight, 6) for t in targets},
            },
            reasoning=reasoning,
            confidence=_compute_confidence(all_signals),
            trades_executed=[{"symbol": t.symbol, "direction": t.direction.value, "amount": t.amount} for t in trades],
            strategies_referenced=[ss.strategy_id for ss in all_signals],
            consulted_paper_hashes=_paper_hashes_from_signals(all_signals),
        )

        trace.compute_hash()
        logger.info("[tick %s] COMMIT hash: %s", tick_id, trace.trace_hash[:16])

        if DRY_RUN:
            logger.info("[tick %s] DRY RUN — skipping on-chain commit", tick_id)
            return trace, None, None, None, None, False

        if not self._lease_ok:
            # #1043 fail-closed: no provable exclusivity — do not anchor a
            # commit that a second live copy might also anchor. The trace is
            # still returned (built + hashed) so the caller's flow continues
            # normally; only the ON-CHAIN write is skipped.
            logger.error(
                "[tick %s] LEASE NOT HELD — skipping on-chain COMMIT this cycle "
                "(fail-closed; will retry once the lease is re-acquired)",
                tick_id,
            )
            return trace, None, None, None, None, False

        claimed_execution_time = int(datetime.now(UTC).timestamp()) + self._COMMIT_EXECUTION_LEAD_S
        intent_summary = f"{trace.decision_type.value}:{len(trades)}".encode()

        try:
            if trace_publisher.supports_commit_reveal():
                if trade_id is None:
                    # Invariant violation: the real commit-reveal path always
                    # supplies a trade_id (computed in the SIZE phase before this
                    # method is called on the non-DRY_RUN path). Fail loud rather
                    # than let trace_publisher.commit() reject an empty tradeId
                    # silently or bind a wrong one.
                    logger.error("[tick %s] COMMIT FAILED: trade_id missing on real commit-reveal path", tick_id)
                    return trace, None, None, None, None, False
                trace_id, commit_tx, commit_block, reverted = await trace_publisher.commit(
                    trace, claimed_execution_time, trade_id, intent_summary
                )
                if commit_tx and not reverted:
                    logger.info(
                        "[tick %s] COMMIT anchored (commit-reveal): id=%s tx=%s block=%s",
                        tick_id,
                        trace_id,
                        commit_tx[:16],
                        commit_block,
                    )
                elif commit_tx and reverted:
                    # A reverted commit carries a real tx hash but anchored
                    # NOTHING — logging it as anchored is the telemetry
                    # falsehood this path exists to kill (#1095).
                    logger.warning(
                        "[tick %s] COMMIT reverted on-chain (status=0): tx=%s block=%s",
                        tick_id,
                        commit_tx[:16],
                        commit_block,
                    )
                return trace, trace_id, commit_tx, commit_block, claimed_execution_time, reverted

            # No commit-reveal on the deployed registry. Until the #588 redeploy this
            # fell back to the v1 ``publishTrace`` anchor; that fallback is gone (#714).
            # The redeploy landed 2026-07-09, so reaching here means the cached ABI in
            # contracts/abis/ or the configured registry address is stale — a loud,
            # visible absence is the correct degraded state, not a v1 anchor whose tx
            # would then be persisted as ``commit_tx_hash`` while binding nothing
            # (CLAUDE.md § fail-soft is wrong for anything a claim depends on).
            logger.error(
                "[tick %s] COMMIT SKIPPED: deployed ReasoningTraceRegistry exposes no "
                "commit()/reveal() — nothing anchored this cycle. The registry is pre-v1.5; "
                "the #588 redeploy landed 2026-07-09, so check the cached ABI and the "
                "configured registry address.",
                tick_id,
            )
            return trace, None, None, None, None, False
        except Exception as e:
            logger.error("[tick %s] COMMIT FAILED: %s", tick_id, e)
            return trace, None, None, None, None, False

    async def _reveal_trace(
        self,
        trace: ReasoningTrace,
        trace_id: int | None,
        tick_id: str,
        tx_hashes: list[str] | None = None,
        commit_tx: str | None = None,
        commit_block: int | None = None,
        trade_block: int | None = None,
        claimed_execution_time: int | None = None,
    ) -> None:
        """Reveal phase: publish the SAME committed trace on-chain after settlement.

        ``trace`` is the exact object committed in the commit phase — we do NOT rebuild
        it, and we must NOT touch any field in ``_HASH_FIELDS`` (``portfolio_after``
        included: mutating it here was the #903 "Hash mismatch" revert). Only the
        non-hashed settlement annotations below are safe to set. Steps:
          1. Wait out the remainder of the claimedExecutionTime window — the contract
             requires reveal-block timestamp >= claimedExecutionTime, and trades settle
             in seconds on Arc, so an immediate reveal is structurally too early (#903).
          2. reveal(trace_id, storagePointer="", canonicalBytes) — hash-only. The
             registry's ``storagePointer`` is empty: we do not pin traces to a public
             gateway. The on-chain keccak256 is the integrity anchor; the full JSON
             lives in the off-chain store. Re-enabling a pin needs the owner to seed a
             live JWT in SSM and rebuild this path (``docs/adr/ipfs-pinning-not-live.md``).
        """
        # Annotate the (already-committed) trace with trade settlement data. These
        # fields are NOT in the hashed canonical set (_HASH_FIELDS), so adding them
        # does not change trace_hash — the commit binding stays intact.
        trace.commit_tx_hash = commit_tx
        trace.commit_block_number = commit_block
        trace.trade_tx_hash = tx_hashes[0] if tx_hashes else None
        trace.trade_block_number = trade_block

        logger.info("[tick %s] REVEAL hash: %s", tick_id, (trace.trace_hash or trace.compute_hash())[:16])

        reveal_tx = None
        reveal_block = None
        if not DRY_RUN:
            # Hash-only reveal: storagePointer is empty. We do not pin.
            # #1043: gated on the lease AFTER the claimedExecutionTime wait
            # (below) so a loss that happens DURING that wait — which can run
            # up to ~_COMMIT_EXECUTION_LEAD_S + skew seconds, independent of
            # the renewal loop's own clock — is caught before the write, not
            # just at the top of the tick.
            try:
                supports_commit_reveal = trace_publisher.supports_commit_reveal()
                if trace_id is not None and supports_commit_reveal:
                    if claimed_execution_time is not None:
                        wait_s = claimed_execution_time + self._REVEAL_SKEW_BUFFER_S - datetime.now(UTC).timestamp()
                        if wait_s > 0:
                            logger.info(
                                "[tick %s] REVEAL waiting %.0fs for claimedExecutionTime window",
                                tick_id,
                                wait_s,
                            )
                            await asyncio.sleep(wait_s)
                    if self._lease_ok:
                        reveal_tx, reveal_block = await trace_publisher.reveal(trace_id, trace, storage_pointer="")
                    else:
                        logger.error(
                            "[tick %s] LEASE NOT HELD — skipping on-chain REVEAL this cycle "
                            "(fail-closed; the commit stays unrevealed until a fresh cycle)",
                            tick_id,
                        )
                else:
                    # Nothing to reveal: either commit() never minted a trace id or the
                    # deployed registry predates commit/reveal. Until the #588 redeploy
                    # this anchored the canonical bytes via the v1 ``publishTrace`` path;
                    # that fallback is gone (#714). A v1 anchor reveals no commitment, so
                    # persisting its tx as ``reveal_tx_hash`` would report an unbound
                    # anchor as a completed reveal — and would set ``is_verified`` true
                    # for it. Leave the trace unanchored and say exactly why.
                    logger.error(
                        "[tick %s] REVEAL SKIPPED: no on-chain commitment to reveal "
                        "(trace_id=%s, commit_reveal_supported=%s) — trace persisted "
                        "off-chain only, unanchored",
                        tick_id,
                        trace_id,
                        supports_commit_reveal,
                    )
                if reveal_tx:
                    logger.info(
                        "[tick %s] REVEAL anchored: tx=%s block=%s (hash-only storagePointer)",
                        tick_id,
                        reveal_tx[:16],
                        reveal_block,
                    )
            except Exception as e:
                logger.error("[tick %s] REVEAL publish FAILED: %s", tick_id, e)

        # Persist off-chain with full temporal binding. storagePointer is empty.
        try:
            off_chain_data = {
                "id": trace.id,
                "vault_address": trace.vault_address,
                "decision_type": trace.decision_type.value,
                "trigger": trace.trigger,
                "timestamp": trace.timestamp.isoformat(),
                "market_context": trace.market_context,
                "portfolio_before": trace.portfolio_before,
                "portfolio_after": trace.portfolio_after,
                "reasoning": trace.reasoning,
                "confidence": trace.confidence,
                "trades_executed": trace.trades_executed,
                "strategies_referenced": trace.strategies_referenced,
                # Hashed field — persist it so /traces/{id}/canonical can rebuild
                # the exact committed bytes for external re-verification (#903).
                "consulted_paper_hashes": trace.consulted_paper_hashes,
                # Settlement txs live OUTSIDE the hashed set: they are only known
                # post-trade, and the committed canonical bytes are immutable (#903).
                "settlement_tx_hashes": tx_hashes or [],
                "trace_hash": trace.trace_hash,
                "arc_tx_hash": reveal_tx,
                "is_verified": reveal_tx is not None,
                # Registry storagePointer. Empty on the live path (#1526): we do not
                # pin. Historical records and on-chain backfill may still populate
                # this field; the name is leftover from the unused pin design.
                "ipfs_cid": None,
                # Temporal binding fields. The "chain" source — and any True binding —
                # require the REAL commit-reveal path: trace_id is non-None ONLY when
                # ReasoningTraceRegistry.commit() minted an on-chain id. Gating on
                # trace_id stops a non-commitment anchor's blocks from masquerading as a
                # verified temporal binding (#714 / AUDIT_2026-06-14 #3) — and since #714
                # removed the v1 publishTrace fallback, no such anchor is written here
                # any more; the gate stays because records persisted before it still are.
                "commit_tx_hash": commit_tx,
                "commit_block_number": commit_block,
                "reveal_tx_hash": reveal_tx,
                "reveal_block_number": reveal_block,
                "trade_tx_hash": tx_hashes[0] if tx_hashes else None,
                "trade_block_number": trade_block,
                # Reveal-reconciliation raw material (#1276). The registry's
                # own trace id and the committed claimedExecutionTime are what a
                # later tick needs to retry a failed reveal — without the id
                # there is no reveal(traceId, …) to make, and the dangling
                # commitment is unrecoverable. Persisted on EVERY reveal
                # attempt, success or failure, precisely because the failure is
                # the case that needs them.
                "onchain_trace_id": trace_id,
                "claimed_execution_time": claimed_execution_time,
                "temporal_binding_source": "chain" if trace_id is not None else "none",
                "temporal_binding_valid": (
                    trace_id is not None
                    and commit_block is not None
                    and trade_block is not None
                    and reveal_block is not None
                    and commit_block < trade_block <= reveal_block
                ),
            }
            await self.state.save_trace(off_chain_data)
        except Exception as e:
            logger.error("[tick %s] REVEAL Redis save FAILED: %s", tick_id, e)

    # ─── Reveal reconciliation (#1276, audit G9) ───────────────────

    async def _reconcile_dangling_reveals(self, tick_id: str) -> None:
        """Retry the reveal for commitments whose trade executed but never revealed.

        The repair pass for the failure mode #1275 pins: ``_reveal_trace``
        degrades honestly (loud ERROR, never-fabricated verification, dangling
        commit preserved) but is per-tick terminal on its own. This is what
        comes back for those records on a later tick.

        Ordering and safety properties, all deliberate:
          - runs at tick start, BEFORE new commit work, under the SAME lease
            (``_lease_ok``) the tick already holds — and re-checks it between
            records, because a lease lost mid-pass must stop the writes;
          - honours ``AGENT_DRY_RUN`` exactly as ``_reveal_trace`` does: a dry
            run performs NO chain interaction at all, so reconciliation can
            never become a back door around it;
          - checks the chain BEFORE transacting, so a reveal that landed but
            whose DB write was lost is backfilled rather than re-sent;
          - bounded: every real failure consumes one of
            ``REVEAL_RECONCILE_MAX_ATTEMPTS``, after which the record goes
            TERMINAL with a loud ERROR. Terminal is a state, not a deletion —
            the honest unverified trace stays exactly as #1275 persisted it.
          - the candidate set is the UNION of the durable index (#1353,
            ``list_dangling_reveal_traces`` — exact regardless of trace
            volume) and the bounded scan (#1276, ``list_recent_traces`` —
            kept as a one-cycle migration backstop for records written
            dangling before the index existed); either source failing is
            logged loudly but degrades to the other rather than aborting the
            whole pass, since a partial candidate set is still real work;
          - candidates are then cross-checked against the durable TERMINAL
            set (#1403 review, ``list_reveal_reconcile_terminal_hashes``) and
            dropped if present — this is what makes a record closed by
            ``mark_reveal_reconcile_terminal`` stay closed even on a tick
            where its own JSON blob failed to save and still reads "pending".
        """
        if DRY_RUN:
            logger.debug("[tick %s] DRY RUN — skipping reveal reconciliation (no chain reads, no writes)", tick_id)
            return

        if not self._lease_ok:
            logger.error(
                "[tick %s] LEASE NOT HELD — skipping reveal reconciliation this cycle "
                "(fail-closed; dangling commitments keep until the lease is re-acquired)",
                tick_id,
            )
            return

        try:
            indexed = await self.state.list_dangling_reveal_traces()
        except Exception as e:
            logger.error("[tick %s] Reveal reconciliation index read FAILED: %s", tick_id, e)
            indexed = []

        try:
            records = await self.state.list_recent_traces(REVEAL_RECONCILE_SCAN_LIMIT)
        except Exception as e:
            logger.error("[tick %s] Reveal reconciliation scan FAILED: %s", tick_id, e)
            records = []

        # Durable-terminal cross-check (#1403 review): ``mark_reveal_reconcile_terminal``
        # closes the index directly, independent of whether that record's own
        # JSON blob save (below, in ``_reconcile_terminal``) succeeded. A
        # candidate's blob can therefore still read "pending" here even
        # though it was already durably closed on an earlier tick — trust
        # the index over the blob, or the compound-failure breaker's verdict
        # would never actually stop the retries it computes.
        try:
            terminal_hashes = await self.state.list_reveal_reconcile_terminal_hashes()
        except Exception as e:
            logger.error("[tick %s] Reveal reconciliation terminal-index read FAILED: %s", tick_id, e)
            terminal_hashes = set()

        candidates = _merge_dangling_candidates(indexed or [], records or [])
        if terminal_hashes:
            candidates = [c for c in candidates if c.get("trace_hash") not in terminal_hashes]
        dangling = [r for r in candidates if _needs_reveal_reconciliation(r)]
        if not dangling:
            return

        logger.warning(
            "[tick %s] Reveal reconciliation: %d dangling commitment(s) with an executed trade — "
            "retrying up to %d this tick",
            tick_id,
            len(dangling),
            REVEAL_RECONCILE_MAX_PER_TICK,
        )

        for record in dangling[:REVEAL_RECONCILE_MAX_PER_TICK]:
            if not self._lease_ok:
                logger.error(
                    "[tick %s] LEASE LOST mid-reconciliation — stopping (fail-closed)",
                    tick_id,
                )
                return
            try:
                await self._reconcile_one_reveal(record, tick_id)
            except Exception:
                # One malformed record must never take the pass — or the tick — down.
                logger.exception(
                    "[tick %s] Reveal reconciliation errored for trace %s — continuing",
                    tick_id,
                    record.get("id"),
                )

    async def _reconcile_one_reveal(self, record: dict, tick_id: str) -> None:
        """Reconcile a single dangling commitment (see ``_reconcile_dangling_reveals``)."""
        trace_uuid = record.get("id")

        # The registry is 1-indexed (trace 0 is the empty sentinel), so 0, a
        # non-integer, or an absent id all mean the same thing: nothing to reveal.
        try:
            onchain_id = int(record["onchain_trace_id"])
        except (KeyError, TypeError, ValueError):
            onchain_id = 0

        # ── Permanently unrevealable #1: no on-chain commitment to reveal ──
        if onchain_id <= 0:
            await self._reconcile_terminal(
                record,
                tick_id,
                "no on-chain commitment id — the anchor was a publishTrace fallback "
                "(pre-v1.5 registry, #588) or predates commit-reveal persistence, so "
                "there is no commit() commitment for reveal() to consume",
            )
            return

        # ── Permanently unrevealable #2: the bytes no longer re-derive ──
        try:
            trace = _rebuild_committed_trace(record)
        except (KeyError, ValueError, TypeError) as e:
            await self._reconcile_terminal(
                record,
                tick_id,
                f"persisted record cannot be rebuilt into its committed trace ({e!r}) — "
                "the canonical bytes reveal() must submit are unrecoverable",
            )
            return
        rebuilt_hash = trace.compute_hash()
        if _norm_hash(rebuilt_hash) != _norm_hash(record.get("trace_hash")):
            await self._reconcile_terminal(
                record,
                tick_id,
                f"canonical bytes no longer re-derive to the committed hash "
                f"(rebuilt {_norm_hash(rebuilt_hash)[:16]} != committed "
                f"{_norm_hash(record.get('trace_hash'))[:16]}) — reveal() would revert 'Hash mismatch'",
            )
            return

        # ── Chain first: did the reveal actually land and only our write get lost? ──
        commitment = await trace_publisher.get_commitment(onchain_id)
        if commitment is not None and commitment.get("revealed"):
            await self._backfill_reveal_from_chain(record, commitment, onchain_id, tick_id)
            return
        if commitment is None:
            # Unreadable ≠ absent (see TracePublisher.get_commitment): treat as a
            # retryable failure so the attempt cap still bounds it.
            await self._reconcile_failure(
                record, tick_id, "on-chain commitment unreadable (registry read failed, or no such commitment)"
            )
            return

        # ── Permanently unrevealable #3: the signer that committed is no
        # longer the backend's confirmed signer (#1353, hardened by #1403
        # review) ──
        #
        # reveal() requires msg.sender == committer. After a key rotation the
        # OLD retry loop burned all REVEAL_RECONCILE_MAX_ATTEMPTS on "Not
        # committer" reverts before going terminal — wasted gas on a result
        # that can never change. Checking it here is a cheap, zero-attempt
        # short-circuit straight to terminal instead. Skipped (falls through
        # to the normal path) when the signer can't be CONFIRMED
        # (``backend_signer_address_confirmed`` returns None) or the
        # commitment carries no committer — never guess, only reject a
        # CONFIRMED mismatch, because terminal here is irreversible and a
        # false positive would permanently kill a perfectly recoverable
        # reveal.
        #
        # On the Circle path (the only prod signer) that confirmation is a
        # bounded read of GET /wallets/{WALLET_ID} asking Circle what the
        # signing wallet's address is (#1412) — NOT the hand-maintained
        # WALLET_ADDRESS mirror, which could drift stale and read as a
        # rotation that never happened. If Circle can't answer, the answer is
        # None and this check simply does not fire.
        configured_addr = await chain_executor.backend_signer_address_confirmed()
        committer = commitment.get("committer")
        if configured_addr and committer and str(committer).lower() != str(configured_addr).lower():
            await self._reconcile_terminal(
                record,
                tick_id,
                f"signer mismatch: reveal() requires msg.sender == committer ({committer}), but the "
                f"confirmed agent signer is {configured_addr} (key rotation since commit) — every "
                "future attempt would revert 'Not committer'",
            )
            return

        # The contract's one LOWER bound we can predict: reveal before
        # claimedExecutionTime reverts. Not a failed attempt — the window simply
        # has not opened yet — so it must not consume a retry.
        claimed = int(commitment.get("claimed_execution_time") or 0)
        now_ts = int(datetime.now(UTC).timestamp())
        if claimed > now_ts:
            logger.info(
                "[tick %s] Reveal reconciliation: trace %s still inside its claimedExecutionTime "
                "window (%ds left) — leaving pending, no attempt consumed",
                tick_id,
                trace_uuid,
                claimed - now_ts,
            )
            return

        # ── The retry itself, from the persisted canonical bytes ──
        # storage_pointer is NOT part of the hash binding, so an empty pointer
        # can never block verification. Reuse a historical pointer if the
        # record already has one; otherwise reveal hash-only (#1526).
        storage_pointer = record.get("ipfs_cid") or ""
        try:
            reveal_tx, reveal_block = await trace_publisher.reveal(onchain_id, trace, storage_pointer=storage_pointer)
        except Exception as e:
            if _is_already_revealed_error(e):
                fresh = await trace_publisher.get_commitment(onchain_id)
                if fresh is not None and fresh.get("revealed"):
                    await self._backfill_reveal_from_chain(record, fresh, onchain_id, tick_id)
                    return
            await self._reconcile_failure(record, tick_id, f"reveal raised: {e}")
            return

        if not reveal_tx:
            await self._reconcile_failure(record, tick_id, "reveal returned no tx hash")
            return

        self._apply_verified_reveal(record, reveal_tx, reveal_block)
        record["reveal_reconcile_state"] = _RECONCILE_DONE
        record["reveal_reconcile_resolved_at"] = datetime.now(UTC).isoformat()
        logger.info(
            "[tick %s] REVEAL RECONCILED: trace %s (vault %s) revealed on retry %d — tx=%s block=%s, "
            "temporal_binding_valid=%s",
            tick_id,
            trace_uuid,
            (record.get("vault_address") or "")[:10],
            int(record.get("reveal_reconcile_attempts") or 0) + 1,
            reveal_tx[:16],
            reveal_block,
            record.get("temporal_binding_valid"),
        )
        await self._persist_reconciled(record, tick_id)

    async def _backfill_reveal_from_chain(self, record: dict, commitment: dict, onchain_id: int, tick_id: str) -> None:
        """Record a reveal that is already on-chain but missing from our store.

        Restart-safety (#1276 constraint 5): the reveal tx can land and the
        process die before persisting it. The registry's ``revealBlock`` is set
        inside ``reveal()`` after its own hash check, so it is honest proof of
        verification — stronger, not weaker, than the tx hash we normally key
        ``is_verified`` off. The tx hash itself is recovered from the indexed
        ``TraceRevealed`` log when the node still serves it; when it does not,
        ``reveal_tx_hash`` stays None rather than being invented, and the
        ``reveal_source`` field says where the claim came from.
        """
        reveal_block = commitment.get("reveal_block")
        reveal_tx = await trace_publisher.find_reveal_tx(onchain_id, reveal_block)

        self._apply_verified_reveal(record, reveal_tx, reveal_block)
        record["reveal_reconcile_state"] = _RECONCILE_FROM_CHAIN
        record["reveal_reconcile_resolved_at"] = datetime.now(UTC).isoformat()
        record["reveal_source"] = "chain_commitment"
        if commitment.get("storage_pointer"):
            record["ipfs_cid"] = commitment["storage_pointer"]

        logger.warning(
            "[tick %s] REVEAL RECONCILED FROM CHAIN: trace %s (vault %s) was already revealed on-chain "
            "at block %s (tx=%s) — no new transaction sent; the off-chain record had lost it",
            tick_id,
            record.get("id"),
            (record.get("vault_address") or "")[:10],
            reveal_block,
            reveal_tx[:16] if reveal_tx else "unknown (log unavailable)",
        )
        await self._persist_reconciled(record, tick_id)

    def _apply_verified_reveal(self, record: dict, reveal_tx: str | None, reveal_block: int | None) -> None:
        """Write the reveal outcome onto the record, recomputing the binding honestly.

        ``temporal_binding_valid`` is re-derived from the SAME rule
        ``_reveal_trace`` uses — chain source, and commit < trade <= reveal —
        never assumed from the fact that a retry succeeded.
        """
        record["reveal_tx_hash"] = reveal_tx
        record["reveal_block_number"] = reveal_block
        if reveal_tx:
            record["arc_tx_hash"] = reveal_tx
        record["is_verified"] = True

        commit_block = record.get("commit_block_number")
        trade_block = record.get("trade_block_number")
        record["temporal_binding_valid"] = (
            record.get("temporal_binding_source") == "chain"
            and isinstance(commit_block, int)
            and isinstance(trade_block, int)
            and isinstance(reveal_block, int)
            and commit_block < trade_block <= reveal_block
        )

    async def _reveal_reconcile_first_seen(self, trace_hash: str | None, tick_id: str) -> datetime | None:
        """Read the durable first-seen-dangling marker; failure degrades to skipping the age guard.

        A read failure here must NOT block the normal attempt-cap path (that
        one still works off the in-memory ``record``) — this is a SECOND,
        independent bound, and losing it for one tick just means the
        compound-failure circuit breaker doesn't fire this cycle, not that
        reconciliation stops.
        """
        if not trace_hash:
            return None
        try:
            return await self.state.get_reveal_reconcile_first_seen(trace_hash)
        except Exception as e:
            logger.warning(
                "[tick %s] Reveal reconciliation first-seen read FAILED for %s: %s — age guard skipped this cycle",
                tick_id,
                trace_hash[:16],
                e,
            )
            return None

    async def _seed_missing_first_seen(self, trace_hash: str | None, tick_id: str) -> None:
        """Independently seed a missing first-seen marker (#1403 review, item 4).

        A migration-era dangling record — one that was already dangling
        before this index existed — has no first-seen marker, and normally
        only ``save_trace`` writes one, as a side effect of persisting the
        full trace JSON blob. If THAT blob write is the thing broken, the
        marker can never get seeded that way, so the max-age circuit breaker
        can never fire for exactly the compound failure it exists to close.

        Seeding here — a small, independent write, NOT gated on the blob
        save — gives such a record a clock (starting now) on its first
        reconciliation pass, regardless of whether the blob save this same
        tick succeeds. A write failure here is loud but never blocking: the
        normal attempt-cap path still works off the in-memory ``record``.
        """
        if not trace_hash:
            return
        try:
            await self.state.seed_reveal_reconcile_first_seen(trace_hash)
        except Exception as e:
            logger.warning(
                "[tick %s] Reveal reconciliation first-seen SEED FAILED for %s: %s — will retry seeding "
                "next cycle; age guard stays unbounded for this record until it succeeds",
                tick_id,
                trace_hash[:16],
                e,
            )

    async def _reconcile_failure(self, record: dict, tick_id: str, reason: str) -> None:
        """Count one failed retry; go terminal at the cap OR past the max age.

        The verification fields are pointedly NOT touched here — a failed retry
        leaves the #1275 honest-degradation contract exactly as it was
        (``is_verified`` False, reveal fields None, binding invalid, dangling
        commit preserved). Only the reconciliation bookkeeping moves.

        Two INDEPENDENT bounds gate terminal, not one (#1353, compound-failure
        finding on #1352): the attempt cap trusts ``reveal_reconcile_attempts``,
        a field inside the SAME JSON blob ``save_trace`` just tried (and may
        keep failing) to persist — a broken Redis write path on top of a
        broken reveal can leave that counter stuck below the cap forever. The
        max-age check reads a marker written directly to Redis (HSETNX, set
        once) the moment this record FIRST entered the dangling index, wholly
        independent of whether the counter's own persistence ever succeeds
        again — so this bound still fires even when the counter cannot be
        trusted.
        """
        attempts = int(record.get("reveal_reconcile_attempts") or 0) + 1
        record["reveal_reconcile_attempts"] = attempts
        record["reveal_reconcile_last_error"] = reason
        record["reveal_reconcile_last_attempt_at"] = datetime.now(UTC).isoformat()

        if attempts >= REVEAL_RECONCILE_MAX_ATTEMPTS:
            await self._reconcile_terminal(record, tick_id, reason)
            return

        first_seen = await self._reveal_reconcile_first_seen(record.get("trace_hash"), tick_id)
        if first_seen is not None:
            age_s = (datetime.now(UTC) - first_seen).total_seconds()
            if age_s > REVEAL_RECONCILE_MAX_AGE_SECONDS:
                await self._reconcile_terminal(
                    record,
                    tick_id,
                    f"{reason} — AND max age exceeded ({age_s:.0f}s > {REVEAL_RECONCILE_MAX_AGE_SECONDS}s since "
                    "first seen dangling), independent of the attempt counter (compound-failure guard, #1353)",
                )
                return
        else:
            # No marker at all (as opposed to a transient read failure) means
            # this record — likely migration-era — has never had a clock.
            # Seed one now, independently of whether this tick's blob save
            # below succeeds (#1403 review, item 4): otherwise a record whose
            # dangling-ness predates this index, combined with a blob write
            # path that stays broken, can never acquire a first-seen marker
            # and the age guard stays permanently unbounded for it.
            await self._seed_missing_first_seen(record.get("trace_hash"), tick_id)

        record["reveal_reconcile_state"] = _RECONCILE_PENDING
        logger.warning(
            "[tick %s] Reveal reconciliation attempt %d/%d FAILED for trace %s (vault %s): %s "
            "— still unverified, will retry next tick",
            tick_id,
            attempts,
            REVEAL_RECONCILE_MAX_ATTEMPTS,
            record.get("id"),
            (record.get("vault_address") or "")[:10],
            reason,
        )
        await self._persist_reconciled(record, tick_id)

    async def _reconcile_terminal(self, record: dict, tick_id: str, reason: str) -> None:
        """Give up on a commitment — LOUDLY, and once and for all.

        The failure mode this exists to prevent is an infinite silent retry
        loop. Terminal is a STATE, not a deletion: the trace stays persisted
        exactly as it was, honestly unverified, and becomes COUNTABLE via
        ``reveal_reconcile_state == "terminal"`` — which is also what stops
        ``_needs_reveal_reconciliation`` from ever picking it up again.
        Countable, not yet alerted on: the ERROR below is loud in the log and
        the count is published on ``/health``, but no metric filter or alarm
        consumes either (#1403 review — see
        ``AgentStateStore.get_reveal_reconcile_terminal_count``).

        The durable index close (#1403 review) runs FIRST and independently
        of the trace blob save below: ``mark_reveal_reconcile_terminal`` is
        three small ops (SADD/SREM/HDEL) on their own keys, not gated on the
        (larger) trace JSON's own ``SET`` succeeding. Without this, a broken
        write path specifically on the blob would recompute "should be
        terminal" correctly every tick without it ever sticking in Redis —
        so the record would still read "pending" on the next scan and the
        actual guarded ``reveal()`` call would keep firing, unbounded, exactly
        the compound failure the max-age breaker exists to close.
        """
        record["reveal_reconcile_state"] = _RECONCILE_TERMINAL
        record["reveal_reconcile_terminal_reason"] = reason
        record["reveal_reconcile_terminal_at"] = datetime.now(UTC).isoformat()
        record.setdefault("reveal_reconcile_attempts", 0)

        trace_hash = record.get("trace_hash")
        if trace_hash:
            try:
                await self.state.mark_reveal_reconcile_terminal(trace_hash)
            except Exception as e:
                logger.error(
                    "[tick %s] Durable terminal-index write FAILED for trace %s: %s — the trace "
                    "blob save below is the only remaining chance this transition sticks this tick",
                    tick_id,
                    record.get("id"),
                    e,
                )

        logger.error(
            "[tick %s] REVEAL RECONCILIATION TERMINAL — trace %s (hash %s, vault %s, commit %s) "
            "can no longer be made on-chain-verifiable after %d attempt(s): %s. Its executed trade "
            "%s stays honestly UNVERIFIED; this record is now countable as "
            "reveal_reconcile_state=terminal and will not be retried again.",
            tick_id,
            record.get("id"),
            _norm_hash(record.get("trace_hash"))[:16],
            (record.get("vault_address") or "")[:10],
            (record.get("commit_tx_hash") or "")[:16],
            int(record.get("reveal_reconcile_attempts") or 0),
            reason,
            (record.get("trade_tx_hash") or "")[:16],
        )
        await self._persist_reconciled(record, tick_id)

    async def _persist_reconciled(self, record: dict, tick_id: str) -> None:
        """Persist a reconciliation outcome; a store failure is loud, never fatal."""
        try:
            await self.state.save_trace(record)
        except Exception as e:
            logger.error(
                "[tick %s] Reveal reconciliation persist FAILED for trace %s: %s",
                tick_id,
                record.get("id"),
                e,
            )

    # ─── Reasoning trace (legacy) ──────────────────────────────────

    async def _publish_trace(
        self,
        vault_address: str,
        decision_type: DecisionType,
        trigger: str,
        portfolio: Portfolio,
        trades: list[TradeOrder],
        all_signals: list[StrategySignals],
        market_regime: str,
        consensus: EnsembleConsensus,
        tick_id: str,
        reasoning: str,
        tx_hashes: list[str] | None = None,
        error: str | None = None,
        commit_tx: str | None = None,
        commit_block: int | None = None,
    ) -> None:
        """Build and record a reasoning trace for a NO-TRADE decision.

        Every caller passes an empty trade list — this is the SKIP / error path. The
        trace is hashed and persisted off-chain only: with no trade there is no tradeId
        to bind, so neither ``commit()``/``reveal()`` nor the retired v1 ``publishTrace``
        anchor applies (#714). ``is_verified`` is therefore always False here; a trace
        that carries an on-chain claim comes from ``_commit_trace``/``_reveal_trace``.
        """
        trace = ReasoningTrace(
            id=str(uuid.uuid4()),
            vault_address=vault_address,
            decision_type=decision_type,
            trigger=trigger,
            timestamp=datetime.now(UTC),
            market_context={
                "regime": market_regime,
                "ensemble_consensus": consensus.label.value,
                "ensemble_flat_pct": round(consensus.flat_pct, 4),
                "strategy_count": len(all_signals),
                "signal_summary": {
                    ss.paper_title[:30]: {s.asset: f"{s.signal.value}({s.weight:.0%})" for s in ss.signals[:5]}
                    for ss in all_signals
                },
            },
            portfolio_before={
                "vault": vault_address[:10],
                "aum_usdc": portfolio.total_value_usdc,
                "holdings": {
                    h.symbol: {"weight": f"{h.weight:.1%}", "value_usdc": h.value_usdc} for h in portfolio.holdings
                },
            },
            portfolio_after={"tx_hashes": tx_hashes or []},
            reasoning=reasoning + (f" ERROR: {error}" if error else ""),
            confidence=_compute_confidence(all_signals),
            trades_executed=[{"symbol": t.symbol, "direction": t.direction.value, "amount": t.amount} for t in trades],
            strategies_referenced=[ss.strategy_id for ss in all_signals],
            consulted_paper_hashes=_paper_hashes_from_signals(all_signals),
        )

        trace.compute_hash()
        logger.info("[tick %s] Trace hash: %s", tick_id, trace.trace_hash[:16])

        # No-trade decisions are never anchored on-chain (#714). Every caller of this
        # method passes an empty trade list — it is the SKIP / error path — so there is
        # no tradeId for ``commit()`` to bind and nothing for ``Vault.executeTrade()``
        # to consume. Until the #588 redeploy this anchored the hash via the v1
        # ``publishTrace`` path; that is gone. Synthesising a tradeId to force the
        # commit-reveal path would write a commitment for a trade that never happens
        # and pin ``_pendingTrade[vault][tradeId]`` until something clears it — a
        # fabricated on-chain claim, which is precisely what this issue removes. The
        # trace is still persisted off-chain below, honestly unverified.
        logger.info("[tick %s] No-trade decision — recorded off-chain, not anchored", tick_id)

        # Persist off-chain to Redis (always, even in dry-run)
        try:
            off_chain_data = {
                "id": trace.id,
                "vault_address": trace.vault_address,
                "decision_type": trace.decision_type.value,
                "trigger": trace.trigger,
                "timestamp": trace.timestamp.isoformat(),
                "market_context": trace.market_context,
                "portfolio_before": trace.portfolio_before,
                "portfolio_after": trace.portfolio_after,
                "reasoning": trace.reasoning,
                "confidence": trace.confidence,
                "trades_executed": trace.trades_executed,
                "strategies_referenced": trace.strategies_referenced,
                "trace_hash": trace.trace_hash,
                # Never anchored (see above): the honest values are a null tx and an
                # unverified flag, not a v1 publishTrace hash standing in for one.
                "arc_tx_hash": None,
                "is_verified": False,
                # Commit-reveal fields (if available)
                "commit_tx_hash": commit_tx,
                "commit_block_number": commit_block,
            }
            await self.state.save_trace(off_chain_data)
        except Exception as e:
            logger.error("[tick %s] Trace Redis save FAILED: %s", tick_id, e)

    # ─── Reasoning builder ─────────────────────────────────────────

    def _build_reasoning(
        self,
        all_signals: list[StrategySignals],
        market_regime: str,
        consensus: EnsembleConsensus,
        trades: list[TradeOrder],
        portfolio: Portfolio | None = None,
    ) -> str:
        """Build human-readable reasoning from strategy signals.

        Includes portfolio context + trade specifics so each tick's reasoning
        is distinguishable even when the underlying strategy consensus is
        identical (addresses issue #334).

        Market regime (exogenous) and ensemble consensus (endogenous) are
        surfaced as distinct lines — the consensus is derived from strategy
        signals; the market regime is not (issue #659).
        """
        parts = [
            f"Market regime: {market_regime}",
            f"Ensemble consensus: {consensus.label.value} "
            f"(flat_pct={consensus.flat_pct:.0%}, derived from strategy signals)",
        ]
        for ss in all_signals:
            signal_strs = [f"{s.asset}={s.signal.value}" for s in ss.signals[:4]]
            parts.append(f"{ss.paper_title[:35]}: {', '.join(signal_strs)}")
        if portfolio and portfolio.holdings:
            top = sorted(portfolio.holdings, key=lambda h: h.value_usdc, reverse=True)[:3]
            parts.append("Portfolio: " + ", ".join(f"{h.symbol} {h.weight:.0%}" for h in top))
        if trades:
            parts.append("Trades: " + ", ".join(f"{t.direction.value} {t.amount:.2f} {t.symbol}" for t in trades[:4]))
        else:
            parts.append("No trades — within drift threshold")
        return ". ".join(parts)

    # ─── Vault discovery ───────────────────────────────────────────

    async def _get_managed_vaults(self) -> list[str]:
        """Get vault addresses this runner manages."""
        if EXPLICIT_VAULTS:
            return [v.strip() for v in EXPLICIT_VAULTS.split(",") if v.strip()]

        try:
            vaults = await chain_executor.get_all_vaults()
            if vaults:
                # Update known set
                self._known_vaults = set(vaults)
                return vaults
        except Exception as e:
            logger.warning("Cannot fetch vaults from factory: %s", e)
            return []

        # Auto-create default vault if none exist
        if DRY_RUN:
            logger.info("DRY RUN — would auto-create default vault")
            return []

        logger.info("No vaults found — auto-creating default Tier 1 vault…")
        try:
            # T0.2 — non-custodial: there is no user wallet at auto-create time, so
            # create_vault resolves the owner from ARC_VAULT_GOVERNANCE_ADDRESS (a
            # cold key DISTINCT from the agent). If that env var is unset, the
            # vault stays backend-owned and create_vault logs a loud warning — it
            # NEVER silently hands ownership to the agent. Set
            # ARC_VAULT_GOVERNANCE_ADDRESS in the runner's env to make
            # auto-created demo vaults non-custodial.
            vault_address = await chain_executor.create_vault(
                name="Archimedes Momentum",
                symbol="vMOMENTUM",
                management_fee_bps=100,
                performance_fee_bps=1500,
                agent_assisted=True,
                owner_wallet=None,  # → governance address, or warn-and-stay-backend-owned
            )
            logger.info("Default vault created at %s", vault_address)
            self._known_vaults.add(vault_address)
            return [vault_address]
        except Exception as e:
            logger.error("Failed to auto-create default vault: %s", e)
            return []

    async def _discover_new_vaults(self) -> list[str]:
        """Poll VaultFactory.getAllVaults() and return vaults not yet in _known_vaults.

        Called once per tick. Newly discovered vaults are added to _known_vaults
        so they won't be reported again. This allows user-created vaults (via
        CreateVaultModal on the frontend) to be picked up within one tick interval.
        """
        try:
            all_vaults = await chain_executor.get_all_vaults()
        except Exception as e:
            logger.debug("Cannot poll VaultFactory: %s", e)
            return []

        # If EXPLICIT_VAULTS is set, skip auto-discovery
        if EXPLICIT_VAULTS:
            return []

        current = {chain_client.to_checksum(v) for v in all_vaults}
        known = {chain_client.to_checksum(v) for v in self._known_vaults}
        new = current - known

        if new:
            for v in new:
                logger.info("Discovered new vault 0x%s…", v[:10])
            self._known_vaults = current
            return list(new)

        # Keep known_vaults in sync even if nothing new
        self._known_vaults = current
        return []


async def run() -> None:
    """Main strategy runner loop."""
    logger.info("Archimedes Strategy Runner starting")
    logger.info("  interval: %ds", INTERVAL)
    logger.info("  dry_run: %s", DRY_RUN)
    logger.info("  usdc_floor: %.0f%%", USDC_FLOOR * 100)
    logger.info("  chain_connected: %s", await chain_client.is_connected())

    # Exactly-once lease (#1043): block here until this process holds it — a
    # second live copy simply waits. Only wired onto the runner AFTER
    # acquisition, so tick() never sees a not-yet-acquired lease as "held".
    lease = RunnerLeaseGuard(RUNNER_NAME, LEASE_TTL_MS, LEASE_RENEW_INTERVAL_S)
    logger.info("Acquiring exactly-once lease (%s)…", RUNNER_NAME)
    await lease.acquire_forever()
    lease.start_renewal()
    lease.install_sigterm_release()

    runner = StrategyRunner()
    runner.lease = lease

    while True:
        await runner.tick()
        await runner.state.save_heartbeat()
        logger.info("Sleeping %ds until next tick", INTERVAL)
        await asyncio.sleep(INTERVAL)


if __name__ == "__main__":
    # Standalone entrypoint: load .env into os.environ so the SSOT per-synth
    # address resolution (chain/client.py reads ARC_<SYMBOL>_* via os.getenv) sees
    # .env overrides even when this runs as a bare `python -m archimedes.chain.
    # agent_runner` (no FastAPI main.load_dotenv, no docker env_file). Mirrors
    # main.py; override=False so an explicitly-exported env / docker env_file wins.
    # Lives under __main__ so importing this module in tests never loads .env.
    # (Copilot #765)
    from dotenv import load_dotenv

    # Root-logger config lives here for the same reason: an importable module
    # must not reconfigure its importer's root logger, and this one is imported
    # (by 7 test modules today, and by anything that reaches for
    # StrategyRunner — dev.sh's import check does). It ran at import time until
    # #1719. Nothing in the API process imports this module: the marketplace
    # adapter calls `execution.core.compute_trades` directly, so this is
    # hygiene, not a fix for a live import chain. Both prod entrypoints are
    # `python -m archimedes.chain.agent_runner` (docker-compose.yml,
    # infra/runner-user-data.sh), which still lands here, so the standalone
    # runner logs exactly as before.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )

    load_dotenv("../.env", override=False)
    load_dotenv(".env", override=False)
    asyncio.run(run())
