"""Named chargeable/traceable stages of one marketplace tick.

Boundaries map 1:1 to the agent_runner.py evaluation/rebalance flow.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class TickStep(str, Enum):
    # ── publisher pipeline: one flat charge (action_count=1) per boundary ──
    LOAD_STRATEGY = "load_strategy"  # row 1
    EVALUATE_SIGNALS = "evaluate_signals"  # row 2
    AGGREGATE_WEIGHTS = "aggregate_weights"  # row 3
    ENSEMBLE_CONSENSUS = "ensemble_consensus"  # row 4
    REGIME_CLASSIFY = "regime_classify"  # row 5
    PERSIST_REGIME = "persist_regime"  # row 6
    PERSIST_CONSENSUS = "persist_consensus"  # row 7
    THROTTLE_WEIGHTS = "throttle_weights"  # row 8
    WEIGHTS_TO_TARGETS = "weights_to_targets"  # row 9
    READ_PORTFOLIO = "read_portfolio"  # row 13.1
    COMPUTE_TRADES = "compute_trades"  # row 13.5
    NO_DRIFT_DEDUP = "no_drift_dedup"  # row 13.6 (design halt)
    V_CHECK = "v_check"  # row 13.7 (design halt)
    # ── per-subscriber value delivery: action_count = len(trades) ──
    REBALANCE = "rebalance"  # row 13.13


# Canonical charge order for the publisher pipeline (REBALANCE excluded —
# it is per-subscriber and runs after the pipeline clears).
PIPELINE_STEPS: tuple[TickStep, ...] = (
    TickStep.LOAD_STRATEGY,
    TickStep.EVALUATE_SIGNALS,
    TickStep.AGGREGATE_WEIGHTS,
    TickStep.ENSEMBLE_CONSENSUS,
    TickStep.REGIME_CLASSIFY,
    TickStep.PERSIST_REGIME,
    TickStep.PERSIST_CONSENSUS,
    TickStep.THROTTLE_WEIGHTS,
    TickStep.WEIGHTS_TO_TARGETS,
    TickStep.READ_PORTFOLIO,
    TickStep.COMPUTE_TRADES,
    TickStep.NO_DRIFT_DEDUP,
    TickStep.V_CHECK,
)


class HaltSource(str, Enum):
    PUBLISHER = "publisher"  # step stopped the flow for everyone
    PAYMENT = "payment"  # this subscriber could not pay for a step
    EXECUTION = "execution"  # paid, but the on-chain mirror itself failed
    PAYMENTS_HALT = "payments_halt"  # #1240 kill switch active — no real charge moved, subscriber not deferred


@dataclass
class SubscriberTickRecord:
    sub_id: str
    strategy_id: str
    tick_id: str
    timestamp: datetime
    step_reached: TickStep
    halted: bool
    halt_source: HaltSource | None = None
    halt_reason: str | None = None
    charged: bool = False
    action_count: int | None = None
    trade_orders: list[dict] | None = None
