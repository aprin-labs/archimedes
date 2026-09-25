"""Venue-independent strategy execution: one decision core, two venues (#1410).

The agent's per-tick decision — *read signals → aggregate into target weights →
diff against current positions → produce trades* — has nothing venue-specific in
it. Only the two ends do: where "current positions" is read from, and where the
resulting trades are sent. This package draws that line explicitly.

  ``core``        the decision itself. Pure functions over signals, a portfolio
                  and a symbol→address resolver. No chain client, no DB, no I/O.
  ``venue``       the :class:`~archimedes.execution.venue.ExecutionVenue`
                  protocol (the two ends) and ``ChainVenue``, the vault side.
  ``paper_venue`` ``PaperVenue`` — the same decision, executed against a paper
                  deployment's own append-only agent-trade ledger.

WHY THIS EXISTS. Archimedes is two engines: a strategy GENERATION engine (the
debate society behind the rigor gate) and a strategy EXECUTION engine — the
agent's signal → target-weights → rebalance loop. Paper trading is where the
execution engine deploys and runs for real: same decision core, real
deployments, an append-only trade ledger, no chain and no money. It is the
execution engine's product surface, not a rehearsal for something else.
Vaults are roadmap; when the chain venue goes live it becomes a second venue
behind the same protocol and gets its own validation on its own risk budget.
Paper trading is not, and was never intended to be, vault validation — a green
paper run proves the execution engine, not the vault mechanic.

WHAT THIS DELIBERATELY DOES NOT DO (#1410 anti-goals, all three):

  * ``ChainExecutor``'s live behavior is untouched. ``ChainVenue`` is a thin
    adapter *over* it, and ``StrategyRunner`` still calls ``chain_executor``
    directly — migrating the live vault path onto the protocol is a separate
    change with its own risk budget, and doing it here would have meant the
    paper work rode on a live-money refactor.
  * No new process, daemon or container. ``PaperVenue`` is driven from the
    existing ``paper_advance_loop`` in the web tier.
  * Paper-trading VALUATION math is untouched. ``paper_daily_returns`` stays
    the append-only track record produced by the graded replay, and
    ``paper_marks`` stays the mark-to-market surface. The agent venue writes to
    neither; it owns one new table of its own. See ``paper_venue`` for the
    consequence of that separation, stated as a known limit rather than hidden.
"""

from __future__ import annotations

from archimedes.execution.core import (
    DRIFT_THRESHOLD,
    TickDecision,
    compute_trades,
    targets_from_signals,
    weights_to_targets,
)
from archimedes.execution.venue import ChainVenue, ExecutionVenue

__all__ = [
    "DRIFT_THRESHOLD",
    "ChainVenue",
    "ExecutionVenue",
    "TickDecision",
    "compute_trades",
    "targets_from_signals",
    "weights_to_targets",
]
