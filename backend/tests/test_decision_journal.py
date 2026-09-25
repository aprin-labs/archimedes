"""The decision journal is an OBSERVER — issue #1575, build step 1.

Paper trading replays *the same* ``run_dsl_backtest`` the grader calls; that
"by construction" coupling is the whole correctness argument of
``services/paper_trading.py``. So the mechanism that surfaces paper decisions
must not be able to move a graded number — and that claim is a test here, not
a comment in the analyzer.

Hermetic: synthetic deterministic price data, no network, no DB, no Redis.
"""

from __future__ import annotations

import pytest
from archimedes.services.fusion_evaluator import run_dsl_backtest
from archimedes.services.strategy_dsl import FABER_2007_SPEC, validate_strategy_spec

# A spec that trades: SMA-200 tactical allocation on the synthetic series.
_TRADING_SPEC = validate_strategy_spec(FABER_2007_SPEC)

# A spec that can never enter: the entry condition is unsatisfiable on a
# strictly-positive price series, so the strategy evaluates every rebalance bar
# and acts on none of them.
_NEVER_ENTERS = validate_strategy_spec(
    {
        **FABER_2007_SPEC,
        "name": "never enters",
        "entry": {"lt": ["close", 0]},
        "exit": {"gt": ["close", 0]},
    }
)


def test_journal_is_off_by_default_and_absent_not_empty():
    """``None``, not ``[]``. "The journal was off" and "the strategy never
    traded" are different facts, and a writer that could not tell them apart
    would publish zero traces for a strategy that decided nothing and call it
    full coverage."""
    metrics = run_dsl_backtest(_TRADING_SPEC)
    assert metrics.decision_journal is None


def test_journal_records_the_orders_that_filled():
    metrics = run_dsl_backtest(_TRADING_SPEC, decision_journal=True)
    journal = metrics.decision_journal
    assert journal, "the SMA-200 spec trades on the synthetic series"
    # One journal entry per closed round trip leg; total_trades counts closed
    # ROUND TRIPS, so legs >= trades and both must be non-zero.
    assert len(journal) >= metrics.total_trades > 0

    for event in journal:
        assert event["side"] in {"buy", "sell"}
        # The decision is made on one bar and filled on the next open —
        # conflating them would misdate every trace.
        assert event["decided_on"] < event["filled_on"]
        assert (event["size"] > 0) == (event["side"] == "buy")
        assert event["price"] > 0


def test_journal_is_empty_for_a_spec_that_never_enters():
    """An empty list is the honest answer for a strategy that evaluated bars
    and acted on none: no order was placed, so the order observer sees
    nothing. This is also the v1 SKIP limitation made visible — those bars ARE
    decisions and they are not traced."""
    metrics = run_dsl_backtest(_NEVER_ENTERS, decision_journal=True)
    assert metrics.decision_journal == []
    assert metrics.total_trades == 0


def test_cash_and_position_bracket_every_leg():
    """The two sides of the portfolio delta come from the same executed leg,
    so they cannot disagree with the trade they bracket.

    This is the guard for a real bug found while building it: writing
    ``cash_before = cash_after + executed.value + comm`` looks symmetric but
    is wrong on a SELL, because backtrader reports a closing order's ``value``
    as the *opened* position's value (positive, at the entry price). The
    identity below is in terms of the signed ``size * price``, and it is
    checked on both directions.
    """
    journal = run_dsl_backtest(_TRADING_SPEC, decision_journal=True).decision_journal
    sides = {event["side"] for event in journal}
    assert sides == {"buy", "sell"}, "both directions must be exercised or this proves half of it"

    for event in journal:
        assert event["position_after"] - event["position_before"] == pytest.approx(event["size"])
        assert event["cash_before"] - event["cash_after"] == pytest.approx(
            event["size"] * event["price"] + event["commission"]
        )
        # A buy consumes cash, a sell releases it.
        if event["side"] == "buy":
            assert event["cash_after"] < event["cash_before"]
        else:
            assert event["cash_after"] > event["cash_before"]


def test_journal_is_a_no_op_on_every_graded_number():
    """G7. The same spec on the same feed, flag off vs on, must be identical.

    ADVERSARIAL CONTROL: ``test_an_analyzer_that_trades_would_fail_this``
    below builds the input that SHOULD break this claim — an analyzer that
    places an order — and shows the graded numbers move. Without that, "the
    journal is an observer" would be a property nothing measures.
    """
    off = run_dsl_backtest(_TRADING_SPEC)
    on = run_dsl_backtest(_TRADING_SPEC, decision_journal=True)

    assert on.equity_curve == off.equity_curve
    assert on.sharpe_ratio == off.sharpe_ratio
    assert on.sortino_ratio == off.sortino_ratio
    assert on.max_drawdown == off.max_drawdown
    assert on.total_trades == off.total_trades
    assert on.win_rate == off.win_rate
    assert on.backtest_start == off.backtest_start
    assert on.backtest_end == off.backtest_end


def test_a_meddling_analyzer_would_fail_this():
    """The adversarial half of G7: build the input that SHOULD fail and show
    it does.

    A *bad* analyzer — one that reaches through ``self.strategy`` into the
    broker — is bound in the journal's slot on the same run. If the graded
    numbers came out identical even then, the parity assertion above would be
    measuring nothing and "the journal is an observer" would be an unbacked
    prose claim.
    """
    import backtrader as bt
    from archimedes.services import fusion_evaluator

    class _MeddlingAnalyzer(bt.Analyzer):
        """NOT an observer: it moves money. This is the input that must fail."""

        def __init__(self):
            self._bars = 0

        def next(self):
            self._bars += 1
            if self._bars == 250:
                self.strategy.broker.add_cash(-50_000)

    baseline = run_dsl_backtest(_TRADING_SPEC)
    original = fusion_evaluator._DecisionJournalAnalyzer
    fusion_evaluator._DecisionJournalAnalyzer = _MeddlingAnalyzer
    try:
        meddled = run_dsl_backtest(_TRADING_SPEC, decision_journal=True)
    finally:
        fusion_evaluator._DecisionJournalAnalyzer = original

    assert meddled.equity_curve != baseline.equity_curve, (
        "an analyzer that touches the broker MUST move the graded numbers — if it does not, "
        "the no-op parity test above is not measuring the property it claims to"
    )
    assert meddled.sharpe_ratio != baseline.sharpe_ratio
