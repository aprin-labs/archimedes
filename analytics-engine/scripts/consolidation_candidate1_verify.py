"""Curated-consolidation verification — Part B (2026-07-09).

One-off, honest verification script for the curated-example consolidation
work (docs/CURATED-STRATEGY-DECOUPLE-AND-CONSOLIDATE-2026-07-08.md, Part B).
It does THREE things, all on REAL market data (yfinance), never synthetic:

  1. Closes the data gaps the doc flagged: AM-value (asness_moskowitz_2013_value),
     BAB (frazzini_pedersen_2014_bab), Arnott (arnott_2019_defensive_quality) have
     never actually been backtested (BACKTEST_SHARPE=None, no fixture entry) even
     though they are fully ``run_multi_backtest``-compatible on the existing
     5-asset universe used by antonacci/maillard/moskowitz-tsmom/george-hwang.
     This script runs them for real, using the SAME spec pattern
     ``analytics-engine/scripts/regen_fixtures.py`` already uses for that
     universe (no new methodology invented).

  2. Diagnoses the Gatev/Engle-Granger negative-Sharpe puzzle by comparing NET
     sharpe_ratio (rf=5%/yr, costs included) against gross_sharpe_ratio (rf=5%/yr,
     costs added back) and cost_drag_annual_pct — isolating whether the negative
     number is transaction-cost drag or the 5%/yr excess-return convention
     applied to a strategy earning near-zero nominal return (i.e. a genuine,
     non-buggy result).

  3. Builds candidate #1 (Antonacci dual-momentum + Maillard risk-parity +
     capital_preservation_tbill, equal-weight 1/3 each, date-aligned, DAILY
     rebalance to fixed weights) and runs it through the REAL production rigor
     gate (``rigor_evaluator.run_rigor_gate``) with an honestly-computed CSCV
     PBO over a real, date-aligned multi-strategy cohort (N >= 10, clearing
     ``MIN_LIBRARY_N_FOR_PBO_GATING``).

NEVER tunes parameters to pass. No weight in the fused blend is chosen to beat
the gate — it is a flat 1/3 split, the least-tunable choice available.

Reproducibility caveat (PR #1078 review, 2026-07-12): every leg except the
T-bill one reproduces bit-exactly across runs — the pipeline itself is
deterministic (CSCV uses exhaustive ``itertools.combinations``; no RNG
anywhere). The T-bill leg alone uses dividend-adjusted BIL
(``auto_adjust=True``), and Yahoo recomputes the backward adjustment on every
query, so bit-exact reproduction of that leg (and, downstream, the fused
series) is NOT guaranteed between fetches. Observed drift is in the 5th-6th
significant digit; the decisive gate numbers (``deflated_sharpe``,
``dsr_p_value``) were bit-identical across three independent runs days apart.

Usage:
    cd analytics-engine
    conda run -n archimedes python scripts/consolidation_candidate1_verify.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "strategies"))

BACKEND_ROOT = ROOT.parent / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

import yfinance as yf
from archimedes_analytics_engine.data import _MAX_RETRIES, _RETRY_DELAY_S, fetch_ohlcv, normalize_ohlcv
from archimedes_analytics_engine.engine import (
    run_backtest,
    run_multi_backtest,
    run_pairs_backtest,
)
from archimedes_analytics_engine.pbo import compute_pbo
from archimedes_analytics_engine.strategy_loader import load_strategy


def fetch_ohlcv_div_adjusted(symbol: str, start: str, end: str):
    """Dividend/split-adjusted OHLCV — for the T-bill leg only.

    ``fetch_ohlcv`` (the codebase default, ``auto_adjust=False``) is correct
    for price-appreciation instruments, but BIL (the T-bill ETF proxy for
    ``capital_preservation_tbill``) returns essentially ALL of its yield as
    distributions, not price appreciation. Verified empirically before using
    this: BIL raw Close 2007-05-30..2026-04-30 total return = +0.04% (CAGR
    ~0.002%/yr — a data artifact, not the real T-bill return); BIL dividend-
    adjusted Close over the same window = +28.7% total (CAGR ~1.3%/yr, the
    plausible real T-bill total return across a window spanning two ZIRP
    periods). Backtesting a distributions-only instrument on non-adjusted
    Close silently discards its entire return mechanism. This is scoped to
    the T-bill leg only — it does not change the shared ``fetch_ohlcv`` used
    by every other (price-return-dominated) strategy in the library, which is
    a separate, wider decision for Dan/Önder, not made unilaterally here.

    Retry/backoff mirrors ``fetch_ohlcv`` (same constants) — BIL is the one
    series in this script empirically shown to vary between fetches, so it
    should not also be the one fetch with no transient-failure defense
    (PR #1078 review finding #2).

    Reproducibility note: ``auto_adjust=True`` series are recomputed by Yahoo
    backward through history on each query — bit-exact reproduction of this
    leg between runs is not guaranteed (see module docstring).
    """
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            data = yf.download(symbol, start=start, end=end, auto_adjust=True, progress=False)
        except Exception as exc:  # transient network/yfinance error — retry with backoff
            last_exc = exc
            if attempt == _MAX_RETRIES:
                break
            _log(f"fetch_ohlcv_div_adjusted({symbol}) attempt {attempt} failed: {exc} — retrying")
            time.sleep(_RETRY_DELAY_S * attempt)
            continue
        if data.empty:
            if attempt == _MAX_RETRIES:
                raise ValueError(f"No data returned for symbol={symbol} in range {start}..{end}")
            _log(f"fetch_ohlcv_div_adjusted({symbol}) returned empty — retrying (attempt {attempt})")
            time.sleep(_RETRY_DELAY_S * attempt)
            continue
        return normalize_ohlcv(data, symbol=symbol)
    raise RuntimeError(f"yfinance download failed for {symbol} after {_MAX_RETRIES} attempts") from last_exc


BACKTEST_START = "2004-01-02"
BACKTEST_END = "2026-05-01"  # yfinance end is exclusive; includes bars through 2026-04-30
INITIAL_CASH = 100_000.0
TX_COST_BPS = 10
STRATEGIES_DIR = ROOT / "strategies"

UNIVERSE_5 = ["SPY", "^N225", "GC=F", "TLT", "CL=F"]
UNIVERSE_26 = [
    "SPY",
    "IVV",
    "QQQ",
    "IWM",
    "EFA",
    "EEM",
    "EWA",
    "EWC",
    "GLD",
    "GDX",
    "SLV",
    "KO",
    "PEP",
    "TLT",
    "IEF",
    "DBC",
    "VNQ",
    "XLB",
    "XLE",
    "XLF",
    "XLI",
    "XLK",
    "XLP",
    "XLU",
    "XLV",
    "XLY",
]

ALL_SYMBOLS = sorted(set(UNIVERSE_5) | set(UNIVERSE_26) | {"BIL", "EWA", "EWC"})


def _log(msg: str) -> None:
    print(msg, flush=True)


def fetch_all(symbols: list[str]) -> dict[str, object]:
    out = {}
    for s in symbols:
        _log(f"fetch_ohlcv({s})...")
        out[s] = fetch_ohlcv(s, BACKTEST_START, BACKTEST_END)
    return out


def _correlation_to(map_a: dict[str, float], map_b: dict[str, float]) -> float | None:
    """Pearson correlation over date-ALIGNED return pairs.

    Takes date->return maps (ISO-date keys, the ``daily_return_dates``
    convention) and intersects the two calendars before correlating. The
    previous positional last-N truncation mismatched up to 12.4% of pairs
    when the series had different trading calendars (PR #1078 review
    finding #3) — the engine explicitly supports differing calendars, so
    alignment must be by date, never by position.
    """
    common = sorted(set(map_a) & set(map_b))
    if len(common) < 3:
        return None
    aa = np.asarray([map_a[d] for d in common], dtype=float)
    bb = np.asarray([map_b[d] for d in common], dtype=float)
    if float(np.ptp(aa)) == 0.0 or float(np.ptp(bb)) == 0.0:
        return None
    return round(float(np.corrcoef(aa, bb)[0, 1]), 6)


def _date_return_map(result) -> dict[str, float]:
    return dict(zip(result.daily_return_dates, result.daily_returns, strict=True))


def summarize(stem: str, result) -> dict:
    return {
        "stem": stem,
        "sharpe": result.sharpe_ratio,
        "gross_sharpe": result.gross_sharpe_ratio,
        "cagr": result.cagr,
        "max_dd_pct": result.max_drawdown_pct,
        "trades": result.total_trades,
        "n_obs": len(result.daily_returns),
        "cost_drag_annual_pct": result.cost_drag_annual_pct,
        "turnover_annualized": result.turnover_annualized,
        "look_ahead_passed": result.look_ahead_audit_passed,
        "start": result.backtest_start,
        "end": result.backtest_end,
    }


def main() -> None:
    prices = fetch_all(ALL_SYMBOLS)
    spy_close = prices["SPY"]["Close"]
    spy_ret_map = {
        spy_close.index[i].isoformat()[:10]: (float(spy_close.iloc[i]) / float(spy_close.iloc[i - 1])) - 1.0
        for i in range(1, len(spy_close))
    }

    results: dict[str, object] = {}

    # ---------------------------------------------------------------------
    # 1. Candidate #1 legs (Antonacci, Maillard) + PBO-cohort widener on the
    #    SAME 5-asset universe (identical common_index across all of these).
    # ---------------------------------------------------------------------
    multi5_stems = [
        "antonacci_2014_dual_momentum",
        "maillard_2010_risk_parity",
        "moskowitz_ooi_pedersen_2012_tsmom",
        "george_hwang_2004_52w_high",
        "avellaneda_lee_2010_pca_statarb",
        "jegadeesh_titman_1993_cross_sectional_momentum",
        # Data-gap closers (doc Part B): never actually backtested before.
        "asness_moskowitz_2013_value",
        "frazzini_pedersen_2014_bab",
        "arnott_2019_defensive_quality",
    ]
    prices_list_5 = [prices[s] for s in UNIVERSE_5]
    for stem in multi5_stems:
        _log(f"\n=== run_multi_backtest[5U]: {stem} ===")
        bundle = load_strategy(STRATEGIES_DIR / f"{stem}.py")
        result = run_multi_backtest(
            prices_list_5,
            strategy_cls=bundle.cls,
            initial_cash=INITIAL_CASH,
            names=UNIVERSE_5,
            transaction_cost_bps=TX_COST_BPS,
        )
        results[stem] = result
        _log(json.dumps(summarize(stem, result), default=str, indent=2))

    # ---------------------------------------------------------------------
    # 2. Candidate #1 leg: capital_preservation_tbill (BIL, single-asset).
    #    Uses dividend-adjusted BIL (see fetch_ohlcv_div_adjusted docstring) —
    #    NOT prices["BIL"] (raw Close), which would silently backtest a flat
    #    line for a distributions-only instrument.
    # ---------------------------------------------------------------------
    _log("\n=== run_backtest: capital_preservation_tbill (BIL, dividend-adjusted) ===")
    bil_adj = fetch_ohlcv_div_adjusted("BIL", BACKTEST_START, BACKTEST_END)
    bundle = load_strategy(STRATEGIES_DIR / "capital_preservation_tbill.py")
    tbill_result = run_backtest(
        bil_adj, strategy_cls=bundle.cls, initial_cash=INITIAL_CASH, transaction_cost_bps=TX_COST_BPS
    )
    results["capital_preservation_tbill"] = tbill_result
    _log(json.dumps(summarize("capital_preservation_tbill", tbill_result), default=str, indent=2))

    # For comparison / transparency: also report the (wrong) raw-Close run so
    # the report can show the delta honestly rather than silently swapping.
    _log("\n=== run_backtest: capital_preservation_tbill (BIL, RAW close — for comparison only) ===")
    bundle_raw = load_strategy(STRATEGIES_DIR / "capital_preservation_tbill.py")
    tbill_result_raw = run_backtest(
        prices["BIL"], strategy_cls=bundle_raw.cls, initial_cash=INITIAL_CASH, transaction_cost_bps=TX_COST_BPS
    )
    _log(
        json.dumps(summarize("capital_preservation_tbill_RAW_for_comparison", tbill_result_raw), default=str, indent=2)
    )

    # ---------------------------------------------------------------------
    # 3. PBO-cohort widener: SPY single-asset strategies.
    # ---------------------------------------------------------------------
    spy_single_stems = [
        "faber_2007_sma200_timing",
        "moreira_muir_2017_volatility_managed",
        "ariel_1987_turn_of_month",
        "appel_1979_macd",
        "bollinger_2001_band_reversion",
    ]
    for stem in spy_single_stems:
        _log(f"\n=== run_backtest[SPY]: {stem} ===")
        bundle = load_strategy(STRATEGIES_DIR / f"{stem}.py")
        result = run_backtest(
            prices["SPY"], strategy_cls=bundle.cls, initial_cash=INITIAL_CASH, transaction_cost_bps=TX_COST_BPS
        )
        results[stem] = result
        corr = _correlation_to(_date_return_map(result), spy_ret_map)
        s = summarize(stem, result)
        s["corr_spy"] = corr
        _log(json.dumps(s, default=str, indent=2))

    # ---------------------------------------------------------------------
    # 4. Hedge-leg negative-Sharpe diagnostic: Engle-Granger (EWA/EWC pair)
    #    and Gatev portfolio-of-pairs (26-ETF universe).
    # ---------------------------------------------------------------------
    _log("\n=== run_pairs_backtest: engle_granger_1987_cointegration_pairs (EWA/EWC) ===")
    bundle = load_strategy(STRATEGIES_DIR / "engle_granger_1987_cointegration_pairs.py")
    eg_result = run_pairs_backtest(
        prices["EWA"],
        prices["EWC"],
        strategy_cls=bundle.cls,
        initial_cash=INITIAL_CASH,
        name_a="EWA",
        name_b="EWC",
        transaction_cost_bps=TX_COST_BPS,
    )
    results["engle_granger_1987_cointegration_pairs"] = eg_result
    _log(json.dumps(summarize("engle_granger_1987_cointegration_pairs", eg_result), default=str, indent=2))

    _log("\n=== run_multi_backtest[26U]: gatev_2006_portfolio_of_pairs ===")
    bundle = load_strategy(STRATEGIES_DIR / "gatev_2006_portfolio_of_pairs.py")
    prices_list_26 = [prices[s] for s in UNIVERSE_26]
    gatev_result = run_multi_backtest(
        prices_list_26,
        strategy_cls=bundle.cls,
        initial_cash=INITIAL_CASH,
        names=UNIVERSE_26,
        transaction_cost_bps=TX_COST_BPS,
    )
    results["gatev_2006_portfolio_of_pairs"] = gatev_result
    _log(json.dumps(summarize("gatev_2006_portfolio_of_pairs", gatev_result), default=str, indent=2))

    # rf=0 vs rf=5%/yr diagnostic — isolate the risk-free-hurdle effect from
    # cost drag on the two hedge legs' deeply negative net Sharpe.
    _ANN = 252
    for stem, result in (
        ("engle_granger_1987_cointegration_pairs", eg_result),
        ("gatev_2006_portfolio_of_pairs", gatev_result),
    ):
        dr = np.asarray(result.daily_returns, dtype=float)
        mu, sigma = float(dr.mean()), float(dr.std(ddof=1))
        sharpe_rf0 = (mu / sigma) * np.sqrt(_ANN) if sigma > 0 else None
        _log(
            f"\n[diagnostic] {stem}: net_sharpe(rf=5%)={result.sharpe_ratio:+.4f} "
            f"gross_sharpe(rf=5%,no-cost)={result.gross_sharpe_ratio}  "
            f"raw_sharpe(rf=0)={sharpe_rf0:+.4f}  "
            f"cost_drag_annual_pct={result.cost_drag_annual_pct}  "
            f"cagr={result.cagr:+.4%}  trades={result.total_trades}  turnover={result.turnover_annualized}"
        )

    # ---------------------------------------------------------------------
    # 5. Build candidate #1 fused series: equal-weight (1/3 each) blend of
    #    Antonacci + Maillard + T-Bill, date-aligned via daily_return_dates.
    #    Flat weights — nothing tuned.
    # ---------------------------------------------------------------------
    antonacci_result = results["antonacci_2014_dual_momentum"]
    maillard_result = results["maillard_2010_risk_parity"]

    m_a = _date_return_map(antonacci_result)
    m_m = _date_return_map(maillard_result)
    m_t = _date_return_map(tbill_result)
    fused_dates = sorted(set(m_a) & set(m_m) & set(m_t))
    _log(
        f"\nFused candidate #1 date alignment: antonacci={len(m_a)} maillard={len(m_m)} "
        f"tbill={len(m_t)} -> common={len(fused_dates)}"
    )
    fused_returns = [(m_a[d] + m_m[d] + m_t[d]) / 3.0 for d in fused_dates]

    # ---------------------------------------------------------------------
    # 6. Real CSCV PBO over a wide, date-aligned cohort (N >= 10).
    # ---------------------------------------------------------------------
    cohort_stems = [
        "antonacci_2014_dual_momentum",
        "maillard_2010_risk_parity",
        "moskowitz_ooi_pedersen_2012_tsmom",
        "george_hwang_2004_52w_high",
        "avellaneda_lee_2010_pca_statarb",
        "jegadeesh_titman_1993_cross_sectional_momentum",
        "capital_preservation_tbill",
        "faber_2007_sma200_timing",
        "moreira_muir_2017_volatility_managed",
        "ariel_1987_turn_of_month",
        "appel_1979_macd",
        "bollinger_2001_band_reversion",
        "engle_granger_1987_cointegration_pairs",
        "gatev_2006_portfolio_of_pairs",
    ]
    cohort_maps = {stem: _date_return_map(results[stem]) for stem in cohort_stems}
    cohort_maps["candidate1_fused"] = dict(zip(fused_dates, fused_returns, strict=True))

    common_dates = set(fused_dates)
    for m in cohort_maps.values():
        common_dates &= set(m.keys())
    common_dates = sorted(common_dates)
    _log(
        f"\nPBO cohort common date range: n={len(common_dates)} "
        f"[{common_dates[0] if common_dates else None} .. {common_dates[-1] if common_dates else None}]"
    )

    cohort_returns = {stem: [m[d] for d in common_dates] for stem, m in cohort_maps.items()}
    pbo_map = compute_pbo(cohort_returns)
    fused_pbo = pbo_map.get("candidate1_fused")
    _log(f"\nCohort CSCV PBO (library-level, same value for all N={len(cohort_returns)} members) = {fused_pbo}")
    _log(f"Per-member (all share the same PBO value by construction): {json.dumps(pbo_map, indent=2)}")

    # ---------------------------------------------------------------------
    # 7. Run the REAL production rigor gate on the fused candidate.
    # ---------------------------------------------------------------------
    from archimedes.services.rigor_evaluator import run_rigor_gate

    # num_trials = 1 (audit 2026-08-03, V2): candidate #1 is a hand-fused blend
    # of THREE hand-implemented published papers (Antonacci/Maillard/T-bill),
    # combined with a fixed, untuned 1/3 weight — there is no search of ours
    # here, so its selection-set size is 1, exactly like any other curated
    # strategy (Dan's decision, 2026-07-27: "For a hand-curated implementation
    # of one published paper there is no search of ours, so num_trials = 1").
    # The PREVIOUS convention (`len(STRATEGIES_DIR.glob("*.py"))` == the
    # curated library's file count, 34 as of this fix) deflated this candidate
    # by the WHOLE library's size, which is exactly the cohort-deflation
    # pattern the decision retires (mirrors V1's regen_fixtures.py bug, and
    # decouple #2 for the live selection-bias routes) — it was never this
    # candidate's own trial count. Restated at num_trials=1 the verdict is
    # STILL a FAIL at the badge/strictest level (dsr_p_value rises from 0.0198
    # to 0.5997 — the far more lenient number — but it stays under
    # rigor_profiles.DSR_P_BADGE_MIN; min_passing_level moves from None to 5,
    # i.e. it only clears the loosest/Speculative rung, never the strictest
    # one) — the corrected number changes, the conclusion does not. See
    # docs/CURATED-STRATEGY-DECOUPLE-AND-CONSOLIDATE-2026-07-08.md and the
    # 2026-08-03 num_trials-provenance audit for the full restatement.
    CANDIDATE1_NUM_TRIALS = 1
    look_ahead_ok = (
        all(results[s].look_ahead_audit_passed for s in ("antonacci_2014_dual_momentum", "maillard_2010_risk_parity"))
        and tbill_result.look_ahead_audit_passed
    )

    verdict = run_rigor_gate(
        strategy_id="candidate1_fused_tactical_riskparity_cashfloor",
        daily_returns=fused_returns,
        num_trials=CANDIDATE1_NUM_TRIALS,
        look_ahead_audit_passed=look_ahead_ok,
        library_pbo=fused_pbo,
        pbo_library_size=len(cohort_returns),
    )

    _log("\n" + "=" * 78)
    _log("CANDIDATE #1 — Antonacci + Maillard + T-Bill (equal-weight 1/3, real data)")
    _log("=" * 78)
    _log(f"n_obs (fused, date-aligned)   = {len(fused_returns)}")
    _log(f"date range                    = {fused_dates[0]} .. {fused_dates[-1]}")
    _log(f"look_ahead_audit_passed       = {look_ahead_ok}")
    _log(f"num_trials (self-contained)   = {CANDIDATE1_NUM_TRIALS}")
    _log(f"deflated_sharpe               = {verdict.deflated_sharpe}")
    _log(f"dsr_p_value (HAC)             = {verdict.dsr_p_value}")
    _log(f"dsr_p_value (IID, advisory)   = {verdict.dsr_p_value_iid}")
    _log(f"oos_sharpe                    = {verdict.oos_sharpe}")
    _log(f"in_sample_sharpe              = {verdict.in_sample_sharpe}")
    _log(f"pbo_score (cohort N={verdict.pbo_library_size})       = {verdict.pbo_score}")
    _log(f"blocked_by_floor              = {verdict.blocked_by_floor}")
    _log(f"passes_all (badge, level 1)   = {verdict.passes_all}")
    _log(f"min_passing_level             = {verdict.min_passing_level}")
    _log("gate_details:")
    for k, v in verdict.gate_details.items():
        _log(f"  {k:20s} {v}")

    # Also report the fused candidate's plain (undeflated) performance stats
    # for context (Sharpe rf=5%, CAGR, max DD) via a synthetic BacktestResult-
    # style summary computed directly off the equity curve.
    eq = [INITIAL_CASH]
    for r in fused_returns:
        eq.append(eq[-1] * (1 + r))
    total_return = eq[-1] / eq[0] - 1.0
    years = len(fused_returns) / 252.0
    cagr = (1 + total_return) ** (1 / years) - 1 if years > 0 else None
    running_max = np.maximum.accumulate(eq)
    dd = [(running_max[i] - eq[i]) / running_max[i] for i in range(len(eq))]
    max_dd = max(dd)
    arr = np.asarray(fused_returns, dtype=float)
    mu, sigma = float(arr.mean()), float(arr.std(ddof=1))
    rf_daily = 0.05 / 252
    sharpe_rf5 = ((mu - rf_daily) / sigma) * np.sqrt(252) if sigma > 0 else None
    _log(f"\nfused CAGR                    = {cagr:+.4%}")
    _log(f"fused max_drawdown            = {max_dd:.4%}")
    _log(f"fused Sharpe (rf=5%/yr)       = {sharpe_rf5:+.4f}")
    corr_spy_fused = _correlation_to(dict(zip(fused_dates, fused_returns, strict=True)), spy_ret_map)
    _log(f"fused correlation_to_spy      = {corr_spy_fused}")

    # ---------------------------------------------------------------------
    # 8. Extra honesty context (not required for the pass/fail verdict, but
    #    directly explains it): each leg's OWN rigor-gate verdict under the
    #    exact same num_trials/PBO cohort convention, plus a 2-leg (no cash
    #    floor) variant, so the report can say WHY the 3-leg blend fails,
    #    not just THAT it fails.
    # ---------------------------------------------------------------------
    _log("\n" + "=" * 78)
    _log("SUPPLEMENTARY — individual-leg verdicts + 2-leg (no cash-floor) variant")
    _log("=" * 78)

    def _leg_verdict(label: str, returns: list[float], look_ahead_passed: bool) -> None:
        # look_ahead_passed threads the REAL per-leg audit flag through —
        # previously hardcoded True, which would have silently misreported a
        # failing leg (PR #1078 review finding #4; latent, never triggered).
        v = run_rigor_gate(
            strategy_id=label,
            daily_returns=returns,
            num_trials=CANDIDATE1_NUM_TRIALS,
            look_ahead_audit_passed=look_ahead_passed,
            library_pbo=fused_pbo,
            pbo_library_size=len(cohort_returns),
        )
        arr = np.asarray(returns, dtype=float)
        mu, sigma = float(arr.mean()), float(arr.std(ddof=1))
        sharpe5 = ((mu - 0.05 / 252) / sigma) * np.sqrt(252) if sigma > 0 else None
        _log(
            f"{label:45s} n={len(returns):5d}  sharpe(rf5%)={sharpe5:+.4f}  "
            f"DSR_p={v.dsr_p_value:.4f}  OOS={v.oos_sharpe:+.4f}  IS={v.in_sample_sharpe:+.4f}  "
            f"look_ahead={look_ahead_passed}  blocked_by_floor={v.blocked_by_floor}  passes_all={v.passes_all}"
        )

    la_a = bool(antonacci_result.look_ahead_audit_passed)
    la_m = bool(maillard_result.look_ahead_audit_passed)
    la_t = bool(tbill_result.look_ahead_audit_passed)

    # Date-aligned per-leg SPY correlations (native windows). The fused blend's
    # aligned corr came out materially different from the misaligned value
    # (+0.21 vs -0.009), so the per-leg "low/negative correlation" claims in
    # the report must be recomputed with aligned calendars too.
    corr_a = _correlation_to(m_a, spy_ret_map)
    corr_m = _correlation_to(m_m, spy_ret_map)
    corr_t = _correlation_to(m_t, spy_ret_map)
    _log(
        f"\nleg correlation_to_spy (date-aligned, native windows): antonacci={corr_a} maillard={corr_m} tbill={corr_t}"
    )

    # Individual legs, restricted to the SAME common date window as the fused
    # candidate (2007-05-30..2026-04-30) for a true apples-to-apples comparison
    # — NOT each leg's own full native window.
    _leg_verdict("antonacci_ALONE_2007_2026window", [m_a[d] for d in fused_dates], la_a)
    _leg_verdict("maillard_ALONE_2007_2026window", [m_m[d] for d in fused_dates], la_m)
    _leg_verdict("tbill_ALONE_2007_2026window", [m_t[d] for d in fused_dates], la_t)

    # Apples-to-apples 2-leg row: SAME window as the fused candidate
    # (m_a ∩ m_m ∩ m_t — BIL-capped 2007-05-30..2026-04-30) even though this
    # variant holds no BIL; comparability against the 3-leg blend is the whole
    # point of the row. The previous m_a ∩ m_m window silently included ~3.2
    # extra years (2004..2007, pre-GFC bull) — PR #1078 review finding #1.
    two_leg_returns = [(m_a[d] + m_m[d]) / 2.0 for d in fused_dates]
    _leg_verdict("antonacci_plus_maillard_NO_cashfloor_50_50", two_leg_returns, la_a and la_m)

    # Context-only row on the 2-leg blend's own native window (2004..2026,
    # ~3.2y longer). Labeled as such — NOT comparable to the 3-leg candidate.
    two_leg_native_dates = sorted(set(m_a) & set(m_m))
    two_leg_native_returns = [(m_a[d] + m_m[d]) / 2.0 for d in two_leg_native_dates]
    _leg_verdict("antonacci_plus_maillard_NATIVE_2004_2026", two_leg_native_returns, la_a and la_m)

    _log("\n" + "=" * 78)
    _log("ALL RESULTS (real data, for the report) — machine-readable dump below")
    _log("=" * 78)
    dump = {stem: summarize(stem, r) for stem, r in results.items()}
    dump["candidate1_fused"] = {
        "n_obs": len(fused_returns),
        "cagr": cagr,
        "max_dd": max_dd,
        "sharpe_rf5": sharpe_rf5,
        "corr_spy": corr_spy_fused,
        "dsr": verdict.deflated_sharpe,
        "dsr_p_value": verdict.dsr_p_value,
        "oos_sharpe": verdict.oos_sharpe,
        "in_sample_sharpe": verdict.in_sample_sharpe,
        "pbo_score": verdict.pbo_score,
        "pbo_library_size": verdict.pbo_library_size,
        "num_trials": CANDIDATE1_NUM_TRIALS,
        "passes_all": verdict.passes_all,
        "min_passing_level": verdict.min_passing_level,
        "blocked_by_floor": verdict.blocked_by_floor,
    }
    dump["leg_correlations_to_spy_date_aligned"] = {
        "antonacci_2014_dual_momentum": corr_a,
        "maillard_2010_risk_parity": corr_m,
        "capital_preservation_tbill": corr_t,
    }
    out_path = ROOT / "scripts" / "_consolidation_candidate1_verify_output.json"
    out_path.write_text(json.dumps(dump, indent=2, default=str))
    _log(f"\nWrote machine-readable dump to {out_path}")


if __name__ == "__main__":
    main()
