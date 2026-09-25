"""The paper side of the executor boundary (#1410).

Same decision core as a vault, different ends: positions are the fold of an
append-only agent-trade ledger instead of an on-chain read, and "execution" is
appending to that ledger instead of submitting a swap.

WHAT THIS RUNS, PRECISELY. The execution engine's chain is *signal FSM →
aggregated target weights → diff against current positions → executed trades →
bookkeeping*. Every link of that chain runs here, on the one decision core,
against a real deployment, with no chain and no money. That is the whole
reason the paper venue exists: it is where the execution engine is deployed
and exercised as a product surface in its own right.

WHAT THIS IS NOT.

  * It is not vault validation, and was never intended to be. Two of the vault
    mechanic's behaviors structurally cannot fire here: this book never marks
    to market between ticks, so drift can only come from a signal change,
    never a price move — the price-drift half of a live rebalance is untested
    by construction; and paper always runs ``regime=None``, so
    regime-conditioned sizing never exercises a healthy detector. When the
    chain venue ships, it validates those on its own risk budget.
  * It is not the track record. ``paper_daily_returns`` stays the append-only
    ledger produced by the graded replay, and this venue never writes to it.
  * It is not a valuation. ``paper_marks`` stays the mark-to-market surface,
    and this venue never writes to it either.
  * Consequently the folded position here is a SIGNAL-STATE book, not a
    marked-to-market one — see ``PaperAgentTrade``'s docstring for the limit
    that follows and why closing it would mean inventing a second opinion
    about what a paper portfolio is worth.

WEIGHTS, NOT DOLLARS. ``PaperDeployment`` has no notional column, so the
portfolio this venue hands the decision core is an INDEX: ``total_value_usdc``
is 1.0 (deploy-time capital == 1.0, the same convention ``PaperMark`` uses) and
each holding's ``value_usdc`` is its portfolio fraction. Trade sizes therefore
come out of ``compute_trades`` as fractions of the book, which is exactly what
gets stored. Nothing here can render a dollar figure, because nothing here
knows one.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from archimedes.models.paper_store import PaperAgentTrade
from archimedes.models.portfolio import Portfolio, PortfolioHolding, TradeDirection

logger = logging.getLogger(__name__)

#: The book is an index: 1.0 == deploy-time capital. See the module docstring.
PAPER_INDEX_BASE = 1.0

#: Weights below this are treated as "not held" when the folded book is turned
#: back into holdings. Float noise from repeated rounding, not a position.
_DUST = 1e-9


class PaperVenue:
    """Executes a tick against one paper deployment's agent-trade ledger.

    Constructed per deployment per tick. Holds a live SQLAlchemy session; it
    flushes but never commits — the caller owns the transaction boundary, the
    same contract ``paper_trading.advance_deployment`` follows.
    """

    name = "paper"

    def __init__(self, session, spec_hash: str) -> None:
        self._session = session
        self._spec_hash = spec_hash

    def token_address(self, symbol: str) -> str:
        """Symbolic instrument id — every symbol is tradeable on paper.

        Never returns ``""``. The empty string is the protocol's "this venue
        cannot trade this symbol", which on paper is never true: there is no
        pool to be missing and no token to be undeployed. Returning one would
        put a leg on the decision that claims to be untradeable when it is not,
        and would land that claim on a stored trade row.
        """
        return f"paper:{symbol}"

    # ─── Positions ────────────────────────────────────────────────

    async def read_portfolio(self, account_id: str) -> Portfolio:
        """Fold this deployment's agent-trade ledger into current positions.

        Replayed from an all-cash start rather than read from a cached position
        blob, for the reason ``strategy_signal_evaluator`` derives position
        state by replay rather than persisting it: a pure function of an
        append-only ledger cannot be double-advanced by a re-run, cannot
        silently reset to "flat" on restart, and has nothing to reconcile.

        Each row records the weight the symbol was moved TO, so the fold is an
        assignment, not an accumulation — no cash-leg arithmetic, and therefore
        no way for rounding to leak into a phantom position. Symbols the tick
        did not trade keep the weight they had; the book can consequently sum to
        slightly less or more than 1.0, and that residual is REAL — it is
        exactly the sub-threshold drift the agent chose not to pay to correct,
        which a vault carries between rebalances too. It is deliberately not
        normalised away.

        Ordered by ``decided_at``, with the row id as a stable tiebreak. Two
        DIFFERENT ticks landing on the same timestamp would order arbitrarily —
        unreachable at this cadence (one tick per deployment per pass, one pass
        per day), and named here rather than defended against, because the
        defence would be a monotonic sequence column that buys nothing at a
        daily cadence. Within a single tick the order is irrelevant: each symbol
        appears at most once, and the fold is an assignment.
        """
        weights: dict[str, float] = {"USDC": PAPER_INDEX_BASE}
        for row in (
            self._session.query(PaperAgentTrade)
            .filter(PaperAgentTrade.deployment_id == account_id)
            .order_by(PaperAgentTrade.decided_at.asc(), PaperAgentTrade.id.asc())
        ):
            weights[row.symbol] = row.target_weight

        holdings = [
            PortfolioHolding(
                symbol=symbol,
                token_address=self.token_address(symbol),
                # Index units, not tokens and not dollars — there is no notional
                # to convert with. See the module docstring.
                amount=weight,
                value_usdc=weight * PAPER_INDEX_BASE,
                weight=weight,
                # Always True. `priced=False` is the chain path's #1080 signal
                # for "the oracle failed, this 0 is not a real 0"; on paper the
                # weight IS the stored fact, so there is no unread price to
                # misrepresent and claiming otherwise would suppress real trades.
                priced=True,
            )
            for symbol, weight in sorted(weights.items())
            if weight > _DUST
        ]

        return Portfolio(
            vault_address=f"paper:{account_id}",
            holdings=holdings,
            total_value_usdc=PAPER_INDEX_BASE,
        )

    # ─── Execution ────────────────────────────────────────────────

    async def execute_trades(self, account_id: str, decision) -> list[str]:
        """Append one row per trade, each naming the tick and signal behind it.

        Returns the ids of the rows written. A trade whose ``(deployment, tick,
        symbol)`` row already exists is SKIPPED rather than duplicated — the
        table's unique constraint is the backstop, this check is the cheap path,
        and together they make re-applying a tick a no-op instead of a doubled
        position (the same idempotent-append rule the rest of the paper tables
        follow).
        """
        if not decision.trades:
            return []

        existing = {
            row.symbol
            for row in self._session.query(PaperAgentTrade.symbol).filter(
                PaperAgentTrade.deployment_id == account_id,
                PaperAgentTrade.tick_id == decision.tick_id,
            )
        }

        decided_at = decision.decided_at or datetime.now(UTC)
        written: list[PaperAgentTrade] = []
        for trade in decision.trades:
            if trade.symbol in existing:
                logger.info(
                    "paper agent: deployment %s tick %s already has a %s row — skipping (idempotent re-apply)",
                    account_id,
                    decision.tick_id,
                    trade.symbol,
                )
                continue
            prior = decision.prior_weight(trade.symbol)
            target = decision.target_weight(trade.symbol)
            signal = decision.signal_for(trade.symbol)
            row = PaperAgentTrade(
                deployment_id=account_id,
                tick_id=decision.tick_id,
                decided_at=decided_at,
                symbol=trade.symbol,
                direction=trade.direction.value
                if isinstance(trade.direction, TradeDirection)
                else str(trade.direction),
                prior_weight=prior,
                target_weight=target,
                # Signed, and derived from the two stored weights so a row can
                # always be checked against itself.
                weight_delta=round(target - prior, 10),
                signal_strategy_id=getattr(signal, "strategy_id", None),
                signal_state=getattr(getattr(signal, "signal", None), "value", None),
                signal_reason=getattr(signal, "reason", None),
                spec_hash=self._spec_hash,
            )
            self._session.add(row)
            written.append(row)

        # Flush, never commit: the caller owns the transaction, so a later
        # failure in the same cycle rolls this back with everything else rather
        # than leaving a half-applied tick behind. Ids are read AFTER the flush —
        # `PaperAgentTrade.id` is a Python-side default applied at INSERT, so
        # reading it before this line returns None for every row.
        self._session.flush()
        return [row.id for row in written]
