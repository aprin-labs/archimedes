"""Fusion evaluator helper functions — stat computation, data feeds, and quality scoring.

Extracted from fusion_evaluator.py to reduce file size and improve modularity.
Contains pure functions for:
- Equity curve and return series computation
- Risk metrics (Sharpe, Sortino, max drawdown)
- Fusion quality scoring across six dimensions
- Data feed setup and backtrader analyzer bindings
"""

from __future__ import annotations

import logging
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Equity curve and return extraction ─────────────────────────────────


def _extract_daily_returns(strat: Any) -> list[float]:
    """Extract daily return series from the TimeReturn analyzer."""
    try:
        tr = strat.analyzers.timereturn.get_analysis()
        return list(tr.values())
    except (AttributeError, KeyError):
        return []


def _extract_analyzer_sharpe(strat: Any) -> float:
    """Extract Sharpe ratio from the SharpeRatio analyzer (rf=0, annualized)."""
    try:
        raw = strat.analyzers.sharpe.get_analysis().get("sharperatio")
        return float(raw) if raw is not None else 0.0
    except (AttributeError, KeyError, TypeError):
        return 0.0


def _extract_analyzer_drawdown(strat: Any) -> float:
    """Extract max drawdown (decimal) from the DrawDown analyzer."""
    try:
        dd_analysis = strat.analyzers.drawdown.get_analysis()
        max_dd_pct = dd_analysis.get("max", {}).get("drawdown", 0.0)
        return float(max_dd_pct) / 100.0
    except (AttributeError, KeyError, TypeError):
        return 0.0


def _build_equity_curve(daily_returns: list[float], initial_cash: float) -> list[float]:
    """Build an equity curve from a series of daily returns."""
    if not daily_returns:
        return [initial_cash]
    curve = [initial_cash]
    for ret in daily_returns:
        curve.append(curve[-1] * (1 + ret))
    return curve


def _extract_equity_curve(strat: Any, initial_cash: float) -> list[float]:
    """Extract equity curve from a completed strategy run."""
    # Use the analyzer if available, otherwise synthesize from broker value
    try:
        for _i in range(len(strat.data)):
            # Approximate by replaying — in practice cerebro.run() doesn't keep history
            pass
    except Exception:
        logger.debug("fusion equity replay failed", exc_info=True)
    # Cerebro doesn't easily expose per-bar equity after run()
    # Return a simplified curve based on initial → final
    final = float(strat.broker.getvalue())
    n_bars = max(1, len(strat.data))
    # Linear interpolation as approximation (real curve would need observer)
    return [initial_cash + (final - initial_cash) * i / n_bars for i in range(n_bars + 1)]


def _compute_monthly_returns(equity_curve: list[float]) -> list[float]:
    """Compute approximate monthly returns from daily equity curve."""
    if len(equity_curve) < 22:
        return []
    returns = []
    for i in range(21, len(equity_curve), 21):
        start = equity_curve[i - 21]
        end = equity_curve[i]
        if start > 0:
            returns.append((end - start) / start)
    return returns


def equity_curve_to_daily_returns(equity_curve: list[float]) -> list[float]:
    """Convert an equity curve to a per-bar return series.

    Shared helper used by both debate_engine and fusion_evaluator to ensure
    identical denominator-guard logic (divides only when the previous bar is
    strictly positive) across every code path that persists return_series for
    the live gate.

    Returns an empty list when the curve has fewer than two bars.
    """
    return [
        (equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1]
        for i in range(1, len(equity_curve))
        if equity_curve[i - 1] > 0
    ]


# ── Risk metrics ──────────────────────────────────────────────────────


def _annualized_sharpe(daily_returns: list[float], rf_annual: float = 0.05) -> float:
    """Compute annualized Sharpe ratio from daily returns.

    Args:
        daily_returns: List of daily return decimal values (e.g., 0.01 for +1%).
        rf_annual: Annual risk-free rate (default 0.05 = 5%); rf=0 for headline display.

    Returns:
        Annualized Sharpe ratio (252 trading days/year).
    """
    if not daily_returns:
        return 0.0
    mean_ret = sum(daily_returns) / len(daily_returns)
    var = sum((r - mean_ret) ** 2 for r in daily_returns) / len(daily_returns)
    std = var**0.5 if var > 0 else 0.0
    if std == 0:
        return 0.0
    daily_rf = rf_annual / 252
    return (mean_ret - daily_rf) / std * (252**0.5)


def _annualized_sortino(daily_returns: list[float], rf_annual: float = 0.05) -> float:
    """Compute annualized Sortino ratio from daily returns.

    Args:
        daily_returns: List of daily return decimal values (e.g., 0.01 for +1%).
        rf_annual: Annual risk-free rate (default 0.05 = 5%); rf=0 for headline display.

    Returns:
        Annualized Sortino ratio (considers only downside volatility).
    """
    if not daily_returns:
        return 0.0
    mean_ret = sum(daily_returns) / len(daily_returns)
    daily_rf = rf_annual / 252
    downside = [min(r - daily_rf, 0) ** 2 for r in daily_returns]
    ds_std = (sum(downside) / len(downside)) ** 0.5 if downside else 0.0
    if ds_std == 0:
        return 0.0
    return (mean_ret - daily_rf) / ds_std * (252**0.5)


def _max_drawdown(equity_curve: list[float]) -> float:
    """Compute maximum drawdown from an equity curve.

    Args:
        equity_curve: List of equity values over time (e.g., [100000, 101000, 99000]).

    Returns:
        Maximum drawdown as a decimal (e.g., 0.05 = 5% max peak-to-trough loss).
    """
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for v in equity_curve:
        if v > peak:
            peak = v
        dd = (peak - v) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    return max_dd


# ── Backtrader analyzers and data feeds ───────────────────────────────


def _synthetic_data() -> Any:
    """Generate synthetic SPY-like daily data for testing (2004-2026).

    Returns:
        A backtrader GenericCSVData feed with deterministic synthetic prices.
    """
    import random

    import backtrader as bt

    random.seed(42)
    rows = []
    price = 100.0
    d = date(2004, 1, 2)
    end = date(2026, 4, 30)

    while d <= end:
        daily_ret = random.gauss(0.0003, 0.012)
        price *= 1 + daily_ret
        rows.append(f"{d.isoformat()},{price:.4f},{price * 1.001:.4f},{price * 0.999:.4f},{price:.4f},1000000")
        d += timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)

    csv_text = "\n".join(rows)
    # delete=False is required: backtrader reads the file from disk in the
    # GenericCSVData(dataname=tmp.name) call below. A context manager would
    # close+delete it before backtrader can open it.
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False)  # noqa: SIM115
    tmp.write(csv_text)
    tmp.close()

    return bt.feeds.GenericCSVData(
        dataname=tmp.name,
        dtformat=("%Y-%m-%d"),
        datetime=0,
        open=1,
        high=2,
        low=3,
        close=4,
        volume=5,
        openinterest=-1,
    )


def _csv_data_feed(csv_path: Path) -> Any:
    """Build a GenericCSVData feed from a CSV file on disk.

    Column layout matches _synthetic_data(): datetime=0, open=1, high=2,
    low=3, close=4, volume=5, openinterest=-1.

    Args:
        csv_path: Path to CSV file with OHLCV data (one-row-per-day).

    Returns:
        A backtrader GenericCSVData feed.
    """
    import backtrader as bt

    return bt.feeds.GenericCSVData(
        dataname=str(csv_path),
        dtformat=("%Y-%m-%d"),
        datetime=0,
        open=1,
        high=2,
        low=3,
        close=4,
        volume=5,
        openinterest=-1,
    )


def _build_analyzers():
    """Wire backtrader analyzer classes for equity-curve and trade-stats capture.

    Returns:
        A tuple (_EquityCurve, _TradeStats, _DecisionJournal) of backtrader
        Analyzer subclasses, ready to be passed to cerebro.addanalyzer().

    Note:
        Called once at module load time to defer backtrader import; Analyzer
        classes are then cached module-wide for use in backtests.
    """
    import backtrader as bt

    class _EquityCurve(bt.Analyzer):
        def start(self):
            self._values: list[float] = []
            # First/last bar dates of the feed actually consumed by this run.
            # Captured here rather than passed in: the caller's notion of the
            # requested window and the bars backtrader really iterated can
            # differ (short feeds, warmup trimming), and the persisted row has
            # to describe the run that happened. See fusion_evaluator's
            # backtest_start/backtest_end stamping.
            self._first_date = None
            self._last_date = None

        def next(self):
            self._values.append(float(self.strategy.broker.getvalue()))
            bar_date = self._current_bar_date()
            if bar_date is not None:
                if self._first_date is None:
                    self._first_date = bar_date
                self._last_date = bar_date

        def _current_bar_date(self):
            try:
                return self.strategy.datas[0].datetime.date(0)
            except (AttributeError, IndexError, ValueError, TypeError):
                # A feed without a usable datetime line yields no date rather
                # than aborting the run; the caller then persists None, which
                # is the honest answer.
                return None

        def stop(self):
            # Capture the final value once after the last bar so the curve
            # spans the full backtest including the closing mark.
            final = float(self.strategy.broker.getvalue())
            if not self._values or self._values[-1] != final:
                self._values.append(final)

        def get_analysis(self):
            return {
                "values": list(self._values),
                "first_bar_date": self._first_date,
                "last_bar_date": self._last_date,
            }

    class _TradeStats(bt.Analyzer):
        """Trade-level stats: total trades, win rate, average holding period."""

        def start(self):
            self._closed_pnls: list[float] = []
            self._holding_periods_bars: list[int] = []

        def notify_trade(self, trade):
            if trade.isclosed:
                self._closed_pnls.append(float(trade.pnlcomm))
                self._holding_periods_bars.append(int(trade.barlen))

        def get_analysis(self):
            n = len(self._closed_pnls)
            wins = sum(1 for p in self._closed_pnls if p > 0)
            win_rate = wins / n if n > 0 else 0.0
            avg_hold = sum(self._holding_periods_bars) / n if n > 0 else 0.0
            return {
                "total_trades": n,
                "win_rate": win_rate,
                # Bars are roughly daily for our seed strategies → days.
                "avg_holding_period_days": avg_hold,
            }

    class _DecisionJournal(bt.Analyzer):
        """Dated record of every order the interpreted strategy actually placed.

        OBSERVER-ONLY, and that is the whole point (#1575 §1.4). Paper trading
        replays *the same* ``run_dsl_backtest`` the grader calls, so anything
        that could influence the run would move every open deployment's
        recorded history. An Analyzer receives ``notify_order`` and cannot
        place orders or touch the broker, so binding it is a no-op on the
        graded numbers — pinned by a parity test rather than asserted here
        (``test_decision_journal.py::test_journal_is_a_no_op``).

        ``order.created.dt`` is the bar the strategy DECIDED on;
        ``order.executed.dt`` is the bar the fill landed (backtrader fills at
        the next bar's open unless cheat-on-close). Both are recorded because
        conflating them would misdate the decision, and the decision date is
        what the trace is keyed and timestamped on.

        The cash/position pair is captured at fill time and the "before" side
        derived from the executed leg rather than sampled separately, so the
        two sides of the portfolio delta cannot disagree with the leg that
        produced them:

            cash_before = cash_after + size * price + commission

        ``size`` is signed (negative on a sell), so the one identity covers
        both directions. It is deliberately NOT written in terms of
        ``order.executed.value``: for a CLOSING order backtrader reports
        ``value`` as the *opened* position's value — positive, and at the
        entry price — so ``cash_after + value + comm`` overstates a sell's
        pre-trade cash by roughly the whole position. Verified against a real
        run rather than read off the docs.
        """

        def start(self):
            self._events: list[dict] = []

        def notify_order(self, order):
            if order.status != order.Completed:
                return
            size = float(order.executed.size)
            price = float(order.executed.price)
            comm = float(order.executed.comm)
            cash_after = float(self.strategy.broker.getcash())
            position_after = float(self.strategy.position.size)
            self._events.append(
                {
                    "decided_on": bt.num2date(order.created.dt).date(),
                    "filled_on": bt.num2date(order.executed.dt).date(),
                    "side": "buy" if order.isbuy() else "sell",
                    "size": size,
                    "price": price,
                    # Signed notional of THIS leg, which is what the cash delta
                    # is made of. See the docstring for why executed.value is
                    # not the right number here.
                    "value": size * price,
                    "commission": comm,
                    "cash_after": cash_after,
                    "cash_before": cash_after + size * price + comm,
                    "position_after": position_after,
                    "position_before": position_after - size,
                }
            )

        def get_analysis(self):
            return {"events": list(self._events)}

    return _EquityCurve, _TradeStats, _DecisionJournal


# ── Module-level analyzer instantiation ────────────────────────────────
# Resolve the real analyzer classes once and bind module-level so
# cerebro.addanalyzer(_EquityCurveAnalyzer, ...) works.
# Done lazily inside a function so module import doesn't pull
# backtrader (matters for environments where backtrader is optional).
_EquityCurveAnalyzer, _TradeStatsAnalyzer, _DecisionJournalAnalyzer = _build_analyzers()
