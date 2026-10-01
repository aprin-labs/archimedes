"""Real multi-year backtester for AI-generated portfolio strategies.

Closes the gap that left every generated strategy stuck at "Pending Backtest"
on the Library page. The analytics-engine's :mod:`engine.run_backtest` runs
single-asset backtrader Cerebro sims keyed on a hand-written ``bt.Strategy``
class — fine for the 6 curated example strategies (each ships its own class)
but wrong for generated output: the LLM emits ``{ticker: weight}`` maps + a
rebalance period, not Python strategy code.

This module fills that hole with a vanilla pandas/numpy simulator:

  1. ``market_data_provider.get_provider(seam="daily").get_daily_ohlcv`` (default
     yfinance; vendor-swappable + ``asset_daily_bars``-cached, #1218/#1282)
     for every ticker in ``weights``
  2. Wide-form close panel with strict inner-join on the business-day index
  3. Periodic rebalance with linear transaction costs (``tx_cost_bps``)
  4. Daily portfolio return series → Sharpe / Sortino / CAGR / max DD / Calmar
  5. The same daily-return series is fed into :mod:`rigor_evaluator` for
     DSR (Bailey & López de Prado 2014) and walk-forward OOS Sharpe — same
     primitives the curated strategies use, so generated and curated
     strategies are graded on the same scale.

Honest framing the UI inherits via the ``backtest_engine`` field:
this is a static-rebalance backtest of the agent's *allocation*. It does NOT
test regime-conditional signal logic — the agent does not emit signals, only
weights. Surfacing this as ``"portfolio-simulator-v1"`` lets the passport
distinguish it from the curated strategies' ``"backtrader"`` runs.

Owner: Dan (strategy engine); rigor primitives owned by Önder.
Spec:  docs/specs/selection-bias-corrections-spec.md (rigor gate unchanged).
"""

from __future__ import annotations

import hashlib
import inspect
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from archimedes.models.backtest import BacktestResult
from archimedes.services.rigor_evaluator import compute_dsr, compute_oos_sharpe

logger = logging.getLogger(__name__)


# Defaults tuned to give enough bars for a meaningful DSR (T ≥ 4 minimum;
# practical floor is ~252 bars / 1 year so the annualization isn't junk).
DEFAULT_LOOKBACK_YEARS = 7
DEFAULT_REBALANCE_DAYS = 21  # ~monthly
DEFAULT_INITIAL_CASH = 100_000.0
DEFAULT_TX_COST_BPS = 10  # round-trip; matches analytics-engine default
# Proportional slippage, mirroring analytics-engine's DEFAULT_COST_MODEL so
# every engine charges the same floor. Mirrored rather than imported because
# this module sits in the request path and the analytics-engine package is an
# optional install here (see run_backtests.py, which imports it lazily) — the
# same reason DEFAULT_TX_COST_BPS above is a mirrored constant. Drift is
# prevented by test_cost_parity.py, not by convention.
DEFAULT_SLIPPAGE_BPS = 5
ANNUALIZATION = 252
RF_DAILY = 0.05 / ANNUALIZATION  # 5% annual risk-free rate, daily equivalent
MIN_BARS_FOR_BACKTEST = 60  # ~3 months; refuse to backtest shorter windows


def _fetch_price_panel(symbols: list[str], start: str, end: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Fetch close prices and volumes for ``symbols`` and inner-join on the date index.

    Missing data is a critical lookahead vector. If a symbol halted or wasn't
    public yet, forward-filling prices leaks the "survival" fact to the
    simulator. We strictly inner-join: the panel only contains days where
    EVERY requested symbol traded.

    Fetches via the market-data provider seam
    (``get_provider(seam="daily").get_daily_ohlcv``, #1218/#1282/#1798)
    rather than analytics-engine's ``fetch_ohlcv`` directly, so
    this — the GENERATION path's portfolio-weights backtester — honors
    ``MARKET_DATA_DAILY_PROVIDER`` and reads/writes the ``asset_daily_bars`` cache
    the same way the request-path call sites do, instead of re-hitting the
    vendor on every backtest re-run.
    """
    from archimedes.services.market_data_provider import get_provider

    provider = get_provider(seam="daily")
    closes: dict[str, pd.Series] = {}
    volumes: dict[str, pd.Series] = {}
    for sym in symbols:
        try:
            df = provider.get_daily_ohlcv(sym, start, end)
            if not df.empty and "Close" in df.columns and "Volume" in df.columns:
                closes[sym] = df["Close"]
                volumes[sym] = df["Volume"]
        except Exception:
            logger.debug("price/volume frame load failed for a symbol", exc_info=True)

    close_panel = pd.DataFrame(closes).dropna()
    volume_panel = pd.DataFrame(volumes).dropna()

    if close_panel.empty or volume_panel.empty:
        raise ValueError(f"Insufficient overlapping history for {symbols}")

    common_idx = close_panel.index.intersection(volume_panel.index)
    if common_idx.empty:
        raise ValueError(f"Insufficient overlapping history for {symbols}")

    panel = close_panel.loc[common_idx]
    if len(panel) < MIN_BARS_FOR_BACKTEST:
        raise ValueError(
            f"Insufficient overlapping history: only {len(panel)} bars across {symbols} (min {MIN_BARS_FOR_BACKTEST})"
        )

    return close_panel.loc[common_idx], volume_panel.loc[common_idx]


def _simulate_portfolio(
    panel: pd.DataFrame,
    volume_panel: pd.DataFrame,
    target_weights: dict[str, float] | pd.DataFrame,
    *,
    rebalance_days: int,
    initial_cash: float,
    tx_cost_bps: int = 10,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
    gamma: float = 0.1,  # Almgren square-root impact coefficient — see note below
) -> tuple[list[float], list[float]]:
    """Run a periodic-rebalance simulation; returns ``(daily_returns, equity_curve)``.

    Implementation notes:
      - Held weights drift between rebalance bars with realized returns.
      - Uses the Almgren square-root market impact function:
        Impact = gamma * sigma * Q * sqrt(Q / ADV)
      - ``gamma`` is the dimensionless permanent-impact coefficient from the
        Almgren-Chriss (2000) optimal-execution framework, refined to the
        empirical square-root law in Almgren et al. (2005), "Direct Estimation
        of Equity Market Impact" (Risk). Calibrated estimates sit around
        0.1-1.0 depending on venue and asset class; we default to a deliberately
        conservative 0.1 so the impact haircut is honest-but-not-punitive. It is
        a tunable parameter, not a fitted constant — surface it per-asset once we
        have venue-specific ADV/impact calibration data.
      - Proportional transaction costs (``tx_cost_bps``) are additive to Almgren
        impact. ``tx_cost_bps`` is a **one-way** (per-leg) rate in basis points,
        and the linear cost is charged on the two-sided turnover ``sum(|Δw|)`` at
        each rebalance. A rebalance moves weight out of some positions and into
        others, so ``sum(|Δw|)`` counts both the sell leg and the buy leg; the
        model therefore charges ``tx_cost_bps`` on **both legs — i.e. round-trip
        per rebalance**. Concretely, shifting a fraction ``f`` of the book from one
        holding to another has ``sum(|Δw|) = 2f`` (``f`` sold + ``f`` bought), so
        the realized drag is ``2 * f * tx_cost_bps / 10_000``. This is intentional
        and conservative (it slightly overstates cost / understates CAGR rather
        than the reverse); callers who want to model a one-way-only fill should
        pass half the round-trip bps. See the ``linear_cost_dollars`` line below.
      - ``slippage_bps`` is charged on the same two-sided turnover and exists so
        this engine's cost FLOOR matches the backtrader engines, which apply
        ``set_slippage_perc`` on top of commission. The Almgren impact term sits
        on top of that shared floor, so this engine is stricter than the others
        and never looser — which is the only direction that keeps a
        cross-engine ranking defensible.
      - No leverage. Negative weights are clamped to zero at normalization
        (long-only enforcement matches the agent's actual output contract).
    """
    syms = list(panel.columns)
    n_bars = len(panel)

    returns_df = panel.pct_change().fillna(0.0)

    # Pre-compute rolling metrics for Almgren impact (must be shift(1) to avoid look-ahead bias)
    rolling_window = 21
    sigma_df = returns_df.rolling(window=rolling_window, min_periods=1).std().shift(1).fillna(0.0)
    dollar_volume_df = (volume_panel * panel).fillna(0.0)
    adv_df = dollar_volume_df.rolling(window=rolling_window, min_periods=1).mean().shift(1).fillna(0.0)

    # ── Temporal (t-1) data alignment execution layer guard ──
    if isinstance(target_weights, pd.DataFrame):
        dynamic_targets = target_weights.reindex(index=panel.index, columns=syms).clip(lower=0.0).fillna(0.0)

        if dynamic_targets.empty:
            raise ValueError("Dynamic target weights DataFrame cannot be empty.")

        # The loop inherently provides a T-1 execution guard: 'held' state
        # from the end of i-1 is applied to the return of bar i.
        # Thus, signals computed at close(t) are executed at close(t),
        # earning returns over t+1. No double-shift is needed.

        # Row-wise L1 normalization
        row_sums = dynamic_targets.sum(axis=1)
        dynamic_targets = dynamic_targets.div(row_sums.where(row_sums > 0, 1.0), axis=0)
        is_dynamic = True
        dynamic_targets_arr = dynamic_targets.to_numpy()
    else:
        # Build canonical static target weights
        raw = pd.Series({s: max(target_weights.get(s, 0.0), 0.0) for s in syms})
        total = float(raw.sum())
        if total <= 0:
            raise ValueError("All weights non-positive after symbol alignment")
        static_target = raw / total
        is_dynamic = False
        static_target_arr = static_target.to_numpy()

    # Extract NumPy arrays for high-performance iteration
    returns_arr = returns_df.to_numpy()
    sigma_arr = sigma_df.to_numpy()
    adv_arr = adv_df.to_numpy()

    held = dynamic_targets_arr[0].copy() if is_dynamic else static_target_arr.copy()

    portfolio_returns: list[float] = []
    equity = float(initial_cash)
    equity_curve: list[float] = []

    for i in range(n_bars):
        # The portfolio return for day i is generated by the weights held at
        # the end of day i-1, perfectly aligned with the t-1 execution guard.
        r_t = float(np.sum(returns_arr[i] * held))

        # Calculate pre-cost equity at end of day
        post_r_t_equity = max(0.0, equity * (1.0 + r_t))

        # Rebalance drift
        drifted = held * (1.0 + returns_arr[i])
        drifted_sum = np.sum(drifted)
        if drifted_sum > 0:
            drifted /= drifted_sum
        else:
            drifted = np.zeros_like(held)

        target = dynamic_targets_arr[i].copy() if is_dynamic else static_target_arr.copy()

        # If dynamic, we must rebalance every bar to track the signals.
        should_rebalance = (i > 0) if is_dynamic else (i > 0 and i % rebalance_days == 0)

        if post_r_t_equity <= 0.0:
            # Bankruptcy!
            r_t = -1.0  # 100% loss
            held = np.zeros_like(held)
            post_r_t_equity = 0.0
        elif should_rebalance:
            delta_w = np.abs(drifted - target)
            # Use pre-return equity (consistent with cost_fraction denominator below)
            q_j = delta_w * equity

            # Avoid division by zero in ADV, penalize illiquidity heavily
            safe_adv = np.where(adv_arr[i] > 0, adv_arr[i], 1.0)

            # Impact Cost = sum(gamma * sigma_j * Q_j * sqrt(Q_j / ADV_j))
            # + linear bps cost
            impact_dollars = np.sum(gamma * sigma_arr[i] * q_j * np.sqrt(q_j / safe_adv))
            # `tx_cost_bps` is a one-way (per-leg) rate. `delta_w = |drifted - target|`
            # is two-sided turnover: each rebalance sells `f` of the book and buys `f`
            # elsewhere, so `sum(delta_w)` counts both legs and this charges the bps
            # on both — i.e. round-trip per rebalance. Intentional and conservative;
            # documented on the function docstring and pinned by
            # test_linear_cost_is_round_trip_on_turnover. Do not "halve" this without
            # updating that test and the round-trip convention across the codebase.
            linear_cost_dollars = np.sum(delta_w) * equity * (tx_cost_bps / 10_000.0)
            # Proportional slippage, charged on the same two-sided turnover as
            # the linear cost. This exists so the cost FLOOR here matches the
            # backtrader engines, which apply set_slippage_perc on top of
            # commission. Without it this engine charged commission-only while
            # the curated library was charged commission plus slippage, and the
            # two sets of numbers were being ranked against each other.
            # The Almgren impact term above stays on top of the floor, which
            # makes this engine stricter than the others and never looser.
            slippage_dollars = np.sum(delta_w) * equity * (slippage_bps / 10_000.0)

            total_cost_dollars = impact_dollars + linear_cost_dollars + slippage_dollars

            cost_fraction = total_cost_dollars / equity if equity > 0 else 0.0
            r_t -= cost_fraction
            post_r_t_equity = max(0.0, equity * (1.0 + r_t))
            held = target.copy()
        else:
            held = drifted

        portfolio_returns.append(r_t)
        equity_curve.append(post_r_t_equity)
        equity = post_r_t_equity

    return portfolio_returns, equity_curve


def _annualized_metrics(daily_returns: list[float], equity_curve: list[float]) -> dict[str, float]:
    """Annualized Sharpe / Sortino / CAGR / max DD / Calmar from a daily return series."""
    arr = np.asarray(daily_returns, dtype=float)
    T = len(arr)
    if T < 2:
        return {
            "sharpe_ratio": 0.0,
            "sortino_ratio": 0.0,
            "cagr": 0.0,
            "max_drawdown": 0.0,
            "calmar_ratio": 0.0,
        }

    mu = float(arr.mean())
    sigma = float(arr.std(ddof=1))
    sharpe = ((mu - RF_DAILY) / sigma) * np.sqrt(ANNUALIZATION) if sigma > 0 else 0.0

    downside = arr[arr < 0]
    # Sortino denominator = RMS of the NEGATIVE returns only, i.e. the divisor is
    # the COUNT of downside observations, not the total N. This is a deliberate,
    # non-standard convention ("downside-frequency-weighted"): the textbook
    # Sortino & Price (1994) target-downside-deviation divides by total N (treating
    # non-negative returns as zero deviation), which gives a larger denominator and
    # a smaller ratio when downside days are sparse. We keep the count-of-negatives
    # form so the metric matches analytics-engine/engine.py::_compute_sortino exactly
    # (pinned there by test_sortino_rms_of_negatives_convention, and here by
    # test_sortino_uses_rms_of_negatives_convention). Sortino is a reported metric
    # only — it does NOT feed the rigor gate — so the convention has no bearing on
    # strategy admission (#952).
    down_rms = float(np.sqrt(np.mean(downside**2))) if len(downside) > 0 else 0.0
    sortino = ((mu - RF_DAILY) / down_rms) * np.sqrt(ANNUALIZATION) if down_rms > 0 else 0.0

    eq = np.asarray(equity_curve, dtype=float)
    cagr = float((eq[-1] / eq[0]) ** (ANNUALIZATION / T) - 1.0) if eq[0] > 0 else 0.0

    drawdown = (eq / np.maximum.accumulate(eq)) - 1.0
    max_dd = float(-drawdown.min())

    calmar = float(cagr / max_dd) if max_dd > 0 else 0.0

    return {
        "sharpe_ratio": float(sharpe),
        "sortino_ratio": float(sortino),
        "cagr": cagr,
        "max_drawdown": max_dd,
        "calmar_ratio": calmar,
    }


def _correlation_to_benchmark(daily_returns: list[float], benchmark_returns: list[float]) -> float | None:
    """Pearson correlation between two return series, robust to unequal length.

    Returns None — not a fabricated 0.0 — on either degenerate branch (too few
    overlapping observations, or a zero-variance series that makes Pearson's r
    undefined). 0.0 would assert "uncorrelated", a claim nothing measured
    (#1242 review; the caller-side version of this fix already stops
    coercing a missing/failed correlation to 0.0 — this closes the same gap
    inside the helper it calls).
    """
    n = min(len(daily_returns), len(benchmark_returns))
    if n < 2:
        return None
    a = np.asarray(daily_returns[:n], dtype=float)
    b = np.asarray(benchmark_returns[:n], dtype=float)
    if a.std() == 0 or b.std() == 0:
        return None
    return float(np.corrcoef(a, b)[0, 1])


def _module_hash() -> str:
    """SHA-256 of the simulator's source — for replay verification on the artifact."""
    try:
        src = inspect.getsource(_simulate_portfolio) + inspect.getsource(_annualized_metrics)
        return hashlib.sha256(src.encode("utf-8")).hexdigest()
    except (OSError, TypeError):
        return ""


def _panel_hash(panel: pd.DataFrame) -> str:
    """SHA-256 of the price panel content — lets a replayer verify same data."""
    return hashlib.sha256(panel.to_csv(index=True).encode("utf-8")).hexdigest()


def backtest_portfolio(
    *,
    strategy_id: str,
    weights: dict[str, float] | pd.DataFrame,
    start_date: str | None = None,
    end_date: str | None = None,
    rebalance_days: int = DEFAULT_REBALANCE_DAYS,
    initial_cash: float = DEFAULT_INITIAL_CASH,
    tx_cost_bps: int = DEFAULT_TX_COST_BPS,
    num_trials_for_dsr: int = 1,
    paper_claimed_sharpe: float | None = None,
    paper_title: str | None = None,
) -> tuple[BacktestResult, dict[str, Any]]:
    """Backtest a static-weight portfolio over real multi-year market data.

    Args:
        strategy_id: FK back to the StrategyPassport row.
        weights: ``{ticker: weight}`` or DataFrame for dynamic rebalancing.
        start_date: ISO date for backtest start. Defaults to ``end - DEFAULT_LOOKBACK_YEARS``.
        end_date: ISO date for backtest end. Defaults to today (UTC).
        rebalance_days: Periodic rebalance interval in trading days.
        initial_cash: Starting equity.
        tx_cost_bps: Round-trip cost in basis points (10 = 0.10%, matches default).
        num_trials_for_dsr: Multiple-testing correction; pass the strategy's OWN
            selection-set size (e.g. its N-candidate generation pool) for a
            meaningful DSR — never the curated library's count (decouple #2).
            Default 1 = no correction (a self-contained, single-trial grade).
        paper_claimed_sharpe: If set, recorded for paper-vs-actual delta display.
        paper_title: Optional human-readable title for the artifact metadata.

    Returns:
        ``(BacktestResult, artifact_dict)`` — the dataclass for DB persistence,
        the dict for the JSON ``artifact_json`` blob (mirrors analytics-engine
        artifact shape so the existing rigor-gate consumers stay generic).

    Raises:
        ValueError: insufficient overlap (< MIN_BARS_FOR_BACKTEST) or all weights
            zero after symbol alignment. Callers should treat these as
            non-fatal: a failed backtest leaves the placeholder in place.
    """
    end_iso = end_date or datetime.now(UTC).date().isoformat()
    if start_date is None:
        start_dt = datetime.fromisoformat(end_iso) - timedelta(days=365 * DEFAULT_LOOKBACK_YEARS)
        start_iso = start_dt.date().isoformat()
    else:
        start_iso = start_date

    if isinstance(weights, pd.DataFrame):
        active_weights = weights
        symbols = list(weights.columns)
    else:
        active_weights = {sym: float(w) for sym, w in (weights or {}).items() if w and w > 0 and sym}
        symbols = list(active_weights.keys())

    if not symbols:
        raise ValueError(f"No positive weights for strategy {strategy_id}")

    panel, volume_panel = _fetch_price_panel(symbols, start_iso, end_iso)
    daily_returns, equity_curve = _simulate_portfolio(
        panel=panel,
        volume_panel=volume_panel,
        target_weights=active_weights,
        rebalance_days=rebalance_days,
        initial_cash=initial_cash,
        tx_cost_bps=tx_cost_bps,
    )

    core = _annualized_metrics(daily_returns, equity_curve)

    # ── Rigor primitives — same evaluator the curated strategies use ──
    deflated_sharpe, dsr_p_value = compute_dsr(daily_returns, num_trials_for_dsr)
    oos_sharpe = compute_oos_sharpe(daily_returns)
    # The rebalancer mechanics are structurally look-ahead-free (t-1 held weights
    # earn t returns). However, the LLM-generated weight matrix is not audited —
    # weights encoding future information (e.g. allocating to last quarter's winners)
    # cannot be detected here. This flag reflects mechanical correctness only.
    look_ahead_passed = True

    # ── SPY correlation (diversification signal) ──
    # None means "not measured" — coercing a missing/failed correlation to 0.0
    # asserts "uncorrelated to SPY", a claim nothing measured (#1242 review).
    correlation_to_spy: float | None = None
    try:
        spy_panel, _ = _fetch_price_panel(["SPY"], start_iso, end_iso)
        spy_full = spy_panel["SPY"].pct_change().fillna(0.0)
        # Align to the portfolio's panel index (inner-join already applied)
        common = panel.index.intersection(spy_panel.index)
        if len(common) >= 2:
            spy_aligned = spy_full.loc[common].tolist()
            port_series = pd.Series(daily_returns, index=panel.index)
            port_aligned = port_series.loc[common].tolist()
            correlation_to_spy = _correlation_to_benchmark(port_aligned, spy_aligned)
    except (ValueError, KeyError) as exc:
        logger.debug("SPY correlation skipped: %s", exc)

    n_obs_daily = len(daily_returns)
    # Number of rebalance events ≈ trades; pure buy-and-hold (no rebalance bars
    # crossed) reports 0 which is honest.
    rebalance_events = max(0, n_obs_daily // rebalance_days - 1)

    result = BacktestResult(
        strategy_id=strategy_id,
        sharpe_ratio=core["sharpe_ratio"],
        sortino_ratio=core["sortino_ratio"],
        max_drawdown=core["max_drawdown"],
        cagr=core["cagr"],
        calmar_ratio=core["calmar_ratio"],
        win_rate=0.0,  # No closed trades in static rebalance; honest 0
        profit_factor=0.0,
        total_trades=rebalance_events,
        avg_holding_period_days=float(rebalance_days),
        correlation_to_spy=correlation_to_spy,
        correlation_to_btc=None,  # not computed for portfolio sim — None, not a fabricated 0.0
        equity_curve=equity_curve,
        monthly_returns=[],
        backtest_start=date.fromisoformat(start_iso),
        backtest_end=date.fromisoformat(end_iso),
        paper_claimed_sharpe=paper_claimed_sharpe,
        paper_claimed_cagr=None,
        paper_claimed_max_dd=None,
        deflated_sharpe_ratio=deflated_sharpe,
        dsr_p_value=dsr_p_value,
        num_trials_in_selection=num_trials_for_dsr,
        pbo_score=None,  # library-level metric; a follow-up scheduler can refresh
        out_of_sample_sharpe=oos_sharpe,
        walk_forward_train_fraction=0.70,
        look_ahead_audit_passed=look_ahead_passed,
        backtest_engine="portfolio-simulator-v1",
        backtest_code_hash=_module_hash(),
        transaction_cost_bps=tx_cost_bps,
        # Cost basis this row was actually charged, so a reader comparing it
        # against engine A/C rows can tell whether they're comparable (#1242
        # review: this used to stop at the artifact dict, never reach the
        # persisted BacktestResult / DB column it was added for).
        cost_model_id=f"cm1:d{tx_cost_bps:g}:s{DEFAULT_SLIPPAGE_BPS:g}+almgren",
        look_ahead_audit_source="static_rebalance_no_signal_shift",
    )

    artifact = {
        "run_id": f"gen-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{strategy_id[:8]}",
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "operations": symbols,
        "strategy": {
            "path": f"generated/{strategy_id}",
            "class_name": "GeneratedStaticRebalance",
            "backtest_code_hash": result.backtest_code_hash,
            "paper_arxiv_id": None,
            "paper_title": paper_title or f"Generated Strategy {strategy_id[:8]}",
            "methodology_hash": None,
            "paper_claimed_sharpe": paper_claimed_sharpe,
            "paper_claimed_cagr": None,
            "paper_claimed_max_dd": None,
        },
        "assumptions": {
            "start": start_iso,
            "end": end_iso,
            "transaction_cost_bps": tx_cost_bps,
            # Was reporting tx_cost_bps here, which claimed a slippage figure
            # this engine did not actually charge and made the row look
            # cost-comparable to the backtrader engines when it wasn't.
            "slippage_bps": DEFAULT_SLIPPAGE_BPS,
            "cost_model_id": f"cm1:d{tx_cost_bps:g}:s{DEFAULT_SLIPPAGE_BPS:g}+almgren",
            "lookahead_guard": "static_rebalance_no_signal_shift",
            "walk_forward_split": 0.70,
            "data_source": "yfinance",
            "backtest_engine": "portfolio-simulator-v1",
            "rebalance_days": rebalance_days,
            "weights": active_weights,
        },
        "results": [
            {
                "operation": "PORTFOLIO",
                "symbol": "PORTFOLIO",
                "metrics": {
                    **core,
                    "daily_returns": daily_returns,
                    "equity_curve": equity_curve,
                    "deflated_sharpe_ratio": deflated_sharpe,
                    "dsr_p_value": dsr_p_value,
                    "out_of_sample_sharpe": oos_sharpe,
                    "correlation_to_spy": correlation_to_spy,
                    "num_bars": n_obs_daily,
                    "rebalance_events": rebalance_events,
                },
            }
        ],
        "data_hashes": [_panel_hash(panel)],
        "integrity_flags": {
            "lookahead_audit_passed": look_ahead_passed,
            "survivorship_bias_mitigated": False,  # yfinance ticker list is current-roster
            "paper_claim_comparison_applied": paper_claimed_sharpe is not None,
        },
    }
    return result, artifact


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 2.2 additions — Monte Carlo robustness, sensitivity sweep, walk-forward
# ═══════════════════════════════════════════════════════════════════════════════


def sensitivity_sweep(
    *,
    strategy_id: str,
    weights: dict[str, float],
    param_grid: dict[str, list[Any]],
    start_date: str | None = None,
    end_date: str | None = None,
    metric: str = "sharpe_ratio",
    n_workers: int = 1,
) -> dict[str, Any]:
    """Run ``backtest_portfolio`` across a grid of parameter combinations.

    Tests how sensitive the strategy's ``metric`` is to changes in backtesting
    parameters (``rebalance_days``, ``tx_cost_bps``, ``initial_cash``, etc.).
    A robust strategy should maintain acceptable performance across the full
    grid; sharp performance cliffs signal over-fitted parameter choices.

    Args:
        strategy_id: Strategy identifier (passed to each ``backtest_portfolio`` call).
        weights: Static target weights.
        param_grid: ``{param_name: [value1, value2, ...]}`` — all combinations
            are expanded. Only the params accepted by ``backtest_portfolio``
            (``rebalance_days``, ``tx_cost_bps``, ``start_date``, ``end_date``)
            are forwarded; unknown keys are silently ignored.
        start_date: ISO date for all runs. Defaults to ``backtest_portfolio`` default.
        end_date: ISO date for all runs. Defaults to today.
        metric: Which scalar metric to summarise (``"sharpe_ratio"`` default).
        n_workers: Parallel workers (ProcessPoolExecutor). Default 1 (sequential).
            Network I/O dominates, so >1 rarely helps in practice but is exposed
            for test environments with a pre-populated cache.

    Returns:
        dict with keys:
            ``"grid"`` — list of ``{params, metric_value}`` dicts for all cells
            ``"metric"`` — name of the metric being tracked
            ``"best_params"`` — parameter combination with the highest metric value
            ``"worst_params"`` — combination with the lowest metric value
            ``"metric_mean"`` — mean metric across the grid
            ``"metric_std"`` — std of metric across the grid
            ``"metric_range"`` — [min, max]
            ``"sensitivity_ratio"`` — (max − min) / |mean|; > 0.5 flags instability

    Raises:
        ValueError: if ``param_grid`` is empty or yields zero combinations.
    """
    from concurrent.futures import ProcessPoolExecutor, as_completed
    from itertools import product

    _ALLOWED_PARAMS = {"rebalance_days", "tx_cost_bps", "start_date", "end_date", "initial_cash"}
    grid_params = {k: v for k, v in param_grid.items() if k in _ALLOWED_PARAMS}
    if not grid_params:
        raise ValueError(f"param_grid must contain at least one of {_ALLOWED_PARAMS}")

    param_names = list(grid_params.keys())
    param_values = list(grid_params.values())
    combinations = list(product(*param_values))

    if not combinations:
        raise ValueError("param_grid produced zero combinations")

    # Defense-in-depth compute cap (audit 2026-06-14). The HTTP schema bounds
    # each range to 25, but this service is callable directly — refuse a grid
    # whose Cartesian product would schedule an unreasonable number of
    # backtests rather than pinning the worker pool.
    _MAX_COMBINATIONS = 625
    if len(combinations) > _MAX_COMBINATIONS:
        raise ValueError(
            f"param_grid yields {len(combinations)} combinations, exceeding the "
            f"{_MAX_COMBINATIONS}-cell sweep limit; reduce the parameter ranges."
        )

    def _run_one(combo: tuple) -> dict[str, Any]:
        kw: dict[str, Any] = dict(zip(param_names, combo, strict=True))
        base = {
            "strategy_id": strategy_id,
            "weights": weights,
            "start_date": start_date,
            "end_date": end_date,
        }
        base.update({k: v for k, v in kw.items() if k in _ALLOWED_PARAMS})
        try:
            result, _ = backtest_portfolio(**base)  # type: ignore[arg-type]
            value = float(getattr(result, metric, 0.0) or 0.0)
        except Exception as exc:
            logger.debug("sensitivity_sweep cell failed %s: %s", kw, exc)
            value = float("nan")
        return {"params": kw, metric: value}

    if n_workers > 1:
        cells: list[dict[str, Any]] = []
        with ProcessPoolExecutor(max_workers=n_workers) as pool:
            futures = {pool.submit(_run_one, combo): combo for combo in combinations}
            for fut in as_completed(futures):
                cells.append(fut.result())
    else:
        cells = [_run_one(combo) for combo in combinations]

    values = [c[metric] for c in cells if not (isinstance(c[metric], float) and c[metric] != c[metric])]
    if not values:
        return {
            "grid": cells,
            "metric": metric,
            "best_params": None,
            "worst_params": None,
            "metric_mean": float("nan"),
            "metric_std": float("nan"),
            "metric_range": [float("nan"), float("nan")],
            "sensitivity_ratio": float("nan"),
        }

    best = max(cells, key=lambda c: c[metric] if c[metric] == c[metric] else float("-inf"))
    worst = min(cells, key=lambda c: c[metric] if c[metric] == c[metric] else float("inf"))
    arr = np.asarray(values)
    mean_v = float(arr.mean())
    std_v = float(arr.std())
    min_v, max_v = float(arr.min()), float(arr.max())
    sensitivity_ratio = float((max_v - min_v) / abs(mean_v)) if abs(mean_v) > 1e-10 else float("inf")

    return {
        "grid": cells,
        "metric": metric,
        "best_params": best["params"],
        "worst_params": worst["params"],
        "metric_mean": mean_v,
        "metric_std": std_v,
        "metric_range": [min_v, max_v],
        "sensitivity_ratio": sensitivity_ratio,
    }
