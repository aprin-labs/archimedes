"""The per-tick decision, with the venue factored out (#1410).

Lifted verbatim out of ``StrategyRunner`` — ``_weights_to_targets`` and
``_compute_trades`` are now one-line delegates to the functions here, so the
live vault path executes the same code it did before this module existed. The
only change is that the symbol→address resolution those functions used to read
off ``chain_client.settings`` now arrives through an
:class:`~archimedes.execution.venue.ExecutionVenue`.

Nothing in this module does I/O. That is what makes the parity claim testable:
the same signal fixture can be pushed through a chain venue and a paper venue in
one process, with no network and no database, and the target weights compared.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from archimedes.models.portfolio import (
    Portfolio,
    RiskProfile,
    TargetAllocation,
    TradeDirection,
    TradeOrder,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from archimedes.execution.venue import ExecutionVenue
    from archimedes.services.strategy_signal_evaluator import StrategySignals

logger = logging.getLogger(__name__)

# Drift threshold for rebalance trigger.
#
# CADENCE GATE FIRST, DRIFT THRESHOLD WITHIN THE OPEN WINDOW (divergence audit
# F3). These are two different gates on two different objects and they compose
# in this order:
#
#   1. CADENCE, per strategy, upstream of here. A DSL strategy declares
#      ``rebalance_frequency`` (daily / weekly / monthly). The live evaluator
#      honours it — strategy_signal_evaluator._replay_position_state only
#      re-decides entry/exit on cadence-eligible bars and HOLDS the position
#      through the rest, mirroring DSLStrategy._should_rebalance in the
#      backtest. A monthly strategy therefore emits the SAME per-asset weight on
#      every one of the ~288 ticks a day between its decision bars, so it
#      contributes no new drift here. Before that fix the live path never read
#      ``rebalance_frequency`` at all and a monthly spec was effectively re-run
#      every tick, with DRIFT_THRESHOLD as the only thing standing between it
#      and a trade.
#
#      Note the gate holds the strategy's VOTE rather than skipping it:
#      aggregate_signals averages every bound strategy's weight, so a strategy
#      that simply stopped emitting on off-cadence ticks would silently change
#      every OTHER co-bound strategy's effective allocation in the vault.
#
#   2. DRIFT, per account, below. Once the aggregated target weights are built,
#      a leg only trades when it has drifted at least this far from the target.
#      This is a cost/no-op filter on an ALREADY cadence-gated target — it does
#      not, and must not, decide WHEN a strategy is allowed to change its mind.
#      An account binding a daily strategy alongside a monthly one still
#      rebalances daily, because the daily strategy is due; that is correct.
#
# Shared by both venues on purpose. A paper venue with a looser threshold would
# trade where the vault would not, and the paper record would then describe a
# strategy the vault does not run.
DRIFT_THRESHOLD = 0.15


@dataclass(frozen=True)
class TickDecision:
    """One tick's complete decision — the unit a venue is asked to execute.

    Trades are NOT passed to a venue on their own, on purpose. A venue that
    persists what it executes has to be able to say which tick, which signals
    and which portfolio state produced each row (#1410: "each agent-driven
    paper trade records which signal/tick produced it"), and the reliable way
    to guarantee that is to make the provenance travel WITH the trades rather
    than alongside them where a caller can forget it.

    Venues that have nothing to persist simply read ``trades`` and ignore the
    rest — ``ChainVenue`` does exactly that, because the vault path anchors its
    tick provenance in the commit-reveal trace instead.
    """

    tick_id: str
    portfolio: Portfolio
    targets: list[TargetAllocation]
    trades: list[TradeOrder]
    signals: list[StrategySignals] = field(default_factory=list)
    decided_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def target_weight(self, symbol: str) -> float:
        """Target weight for ``symbol``; 0.0 when this tick targets none."""
        for t in self.targets:
            if t.symbol == symbol:
                return t.weight
        return 0.0

    def prior_weight(self, symbol: str) -> float:
        """Weight ``symbol`` held BEFORE this tick; 0.0 when it was not held."""
        return self.portfolio.weights_dict.get(symbol, 0.0)

    def signal_for(self, symbol: str) -> Any | None:
        """The ``AssetSignal`` behind ``symbol``, or None for an unvoted leg.

        A paper deployment carries exactly one spec, so at most one strategy
        votes on a symbol and "first match" is the only match. The USDC leg is
        the residual of the aggregation and no strategy votes on it — that is
        the None case, and it is recorded as None rather than attributed to
        whichever strategy happened to sort first.
        """
        for ss in self.signals:
            for sig in ss.signals:
                if sig.asset == symbol:
                    return sig
        return None


def weights_to_targets(
    weights: dict[str, float],
    all_signals: list[StrategySignals] | None,
    venue: ExecutionVenue,
) -> list[TargetAllocation]:
    """Convert weight dict → TargetAllocation list, resolved against ``venue``."""
    # Build symbol → strategy_ids map from signals
    symbol_strategies: dict[str, list[str]] = {}
    if all_signals:
        for ss in all_signals:
            for sig in ss.signals:
                symbol_strategies.setdefault(sig.asset, []).append(ss.strategy_id)

    return [
        TargetAllocation(
            symbol=symbol,
            token_address=venue.token_address(symbol),
            weight=weight,
            strategy_ids=symbol_strategies.get(symbol, []),
        )
        for symbol, weight in weights.items()
    ]


def compute_trades(
    portfolio: Portfolio,
    targets: list[TargetAllocation],
    *,
    drift_threshold: float = DRIFT_THRESHOLD,
) -> list[TradeOrder]:
    """Diff current portfolio vs target weights → trade list.

    Second of the two gates described at ``DRIFT_THRESHOLD``: the targets
    arriving here are already cadence-gated per strategy by the evaluator, so
    this is purely a cost filter on how far the account has drifted — never the
    thing that decides when a strategy may change its mind.
    """
    current_weights = portfolio.weights_dict
    target_map = {t.symbol: t for t in targets}

    # Holdings whose price couldn't be read report weight 0 BY CONSTRUCTION
    # (#1080) — the balance is real, the value is unknown. Trading on that fake
    # 0 would buy more of an unpriceable asset every tick (current 0 vs target
    # >0, forever) or size a blind sell. Skip.
    unpriced = {h.symbol for h in portfolio.holdings if not getattr(h, "priced", True)}

    trades: list[TradeOrder] = []
    all_symbols = set(target_map.keys()) | set(current_weights.keys())

    for sym in all_symbols:
        if sym in unpriced:
            logger.warning(
                "account %s: skipping trade for %s — price unavailable; holding weight "
                "is 0 by construction, not truth; refusing to size a trade against it (#1080)",
                portfolio.vault_address[:10],
                sym,
            )
            continue
        current_w = current_weights.get(sym, 0.0)
        target = target_map.get(sym)
        target_w = target.weight if target else 0.0
        token_addr = target.token_address if target else ""

        drift = target_w - current_w
        if abs(drift) < drift_threshold:
            continue

        usdc_value = abs(drift) * portfolio.total_value_usdc
        direction = TradeDirection.BUY if drift > 0 else TradeDirection.SELL

        trades.append(
            TradeOrder(
                symbol=sym,
                token_address=token_addr,
                direction=direction,
                amount=round(usdc_value, 6),
                estimated_usdc_value=round(usdc_value, 2),
            )
        )

    return trades


def targets_from_signals(
    all_signals: list[StrategySignals],
    *,
    venue: ExecutionVenue,
    constructor: Any,
    strategies: list | None = None,
    regime: Any = None,
    ensemble_consensus: Any = None,
    usdc_floor: float,
    risk_profile: RiskProfile = RiskProfile.MODERATE,
) -> tuple[dict[str, float], list[TargetAllocation]]:
    """Signals → aggregated weights → regime/consensus scaling → venue targets.

    Returns ``(raw_aggregated_weights, targets)``. The first is the PRE-scaling
    ensemble vote — useful in a tick log line, and returned rather than
    recomputed by callers who want it; the second is what to act on.

    The whole path from a signal fixture to a set of target weights, with the
    only venue-shaped thing being ``venue.token_address``. Swap the venue and
    the ``weight`` on every returned allocation must be bit-identical; only
    ``token_address`` may differ. That is the #1410 parity property, and
    ``test_execution_venue_parity`` asserts it here AND against
    ``StrategyRunner``'s own methods, which still compose these same three steps
    inline at their two call sites.
    """
    from archimedes.services.strategy_signal_evaluator import strategy_evaluator

    weights = strategy_evaluator.aggregate_signals(all_signals, usdc_floor=usdc_floor)
    allocations = constructor.construct(
        risk_profile=risk_profile,
        strategies=strategies or [],
        backtest_results={},
        regime=regime,
        ensemble_consensus=ensemble_consensus,
        base_weights=weights,
    )
    targets = weights_to_targets({a.symbol: a.weight for a in allocations}, all_signals, venue)
    return weights, targets
