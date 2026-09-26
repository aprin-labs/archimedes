"""Generate backtest_fixtures.json entries for the NEW strategies (add-only).

Fetches real market data via yfinance, runs each new strategy's backtest
(single-asset via ``run_backtest``; pairs via ``run_pairs_backtest``), then
computes the full rigor-metric suite — DSR, OOS Sharpe, Kelly, and a
cohort-level PBO — and writes the entries.

SCOPE — add-only + idempotent (decided 2026-06-11):
    ``NEW_SINGLE_SPECS`` / ``NEW_PAIR_SPECS`` are a *cumulative catalog* recording
    how every non-legacy fixture entry was generated. A given run only
    (re)backtests the stems NOT already present in ``backtest_fixtures.json``, so
    re-running after a previous ``--write`` is a no-op and earlier entries are
    never recomputed. The legacy fixture entries are likewise left byte-for-byte
    untouched, because re-backtesting them on current data does not reproduce
    their stored metrics (data-vintage drift; and capital_preservation_tbill
    models a T-bill yield, not a TLT buy-hold). Overwriting live published metrics
    with drifted numbers would be a silent, hard-to-reverse regression.

PBO caveat:
    PBO (Bailey et al. 2014 CSCV) is a library-level metric requiring every
    strategy's daily-return series simultaneously. Since we cannot faithfully
    reproduce the legacy return series, PBO here is computed over the NEW
    cohort only and is reported as such. It is a cohort-overfit signal, not the
    full-library value.

    ``num_trials_in_selection`` is set to 1 for every entry (V1 fix,
    num_trials-provenance audit 2026-08-03): each strategy here is a
    hand-implemented published paper, not the output of a search of ours, so
    there is no candidate pool to deflate against — never the curated
    library's size (Dan's decision, 2026-07-27). A PREVIOUS version of this
    script stamped the post-add library size into every new entry instead;
    that was the cross-strategy coupling the decision forbids outside
    Leaderboard/Marketplace, not a genuine per-strategy trial count.

DSR math (compute_dsr / compute_oos_sharpe / compute_kelly) is imported from
``regen_buy_hold_fixture`` so there is a single source for the rigor formulas
(Önder's lane). PBO is ported here from backend ``rigor_evaluator.compute_pbo``.

Usage:
    cd analytics-engine
    uv run python scripts/regen_fixtures.py          # dry-run (print only)
    uv run python scripts/regen_fixtures.py --write   # write new entries
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "strategies"))
sys.path.insert(0, str(Path(__file__).parent))  # for importing regen_buy_hold_fixture

from archimedes_analytics_engine.data import fetch_ohlcv
from archimedes_analytics_engine.engine import run_backtest, run_multi_backtest, run_pairs_backtest
from archimedes_analytics_engine.strategy_loader import load_strategy

# Single source of truth for the DSR / OOS / Kelly formulas (Önder's lane).
from regen_buy_hold_fixture import (  # re-exported: the ONE DSR bar (#1794)
    DSR_P_BADGE_MIN,
    compute_dsr,
    compute_kelly,
    compute_oos_sharpe,
)

# ── Config ────────────────────────────────────────────────────────────────────
BACKTEST_START = "2004-01-02"
BACKTEST_END = "2026-05-01"  # yfinance end is exclusive; includes bars through 2026-04-30
INITIAL_CASH = 100_000.0
_FIXTURE_PATH = ROOT / "strategies" / "backtest_fixtures.json"
_STRATEGIES_DIR = ROOT / "strategies"

# Representative backtest instrument(s) per new strategy. Single-asset strategies
# use SPY for comparability with the legacy SPY-based entries; the pairs strategy
# uses its GLD/GDX demo pair.
NEW_SINGLE_SPECS: list[dict] = [
    {"stem": "connors_alvarez_2009_rsi2", "symbol": "SPY", "tx_cost_bps": 10},
    {"stem": "bollinger_2001_band_reversion", "symbol": "SPY", "tx_cost_bps": 10},
    {"stem": "donchian_breakout", "symbol": "SPY", "tx_cost_bps": 10},
    {"stem": "appel_1979_macd", "symbol": "SPY", "tx_cost_bps": 10},
    {"stem": "brock_1992_dual_ma_crossover", "symbol": "SPY", "tx_cost_bps": 10},
    {"stem": "ariel_1987_turn_of_month", "symbol": "SPY", "tx_cost_bps": 10},
]
NEW_PAIR_SPECS: list[dict] = [
    {"stem": "gatev_2006_pairs_distance", "pair": ("GLD", "GDX"), "tx_cost_bps": 10},
    # Phase 1.3 economic pairs (2026-06-11): same Gatev distance method, new pairs.
    {"stem": "gatev_2006_pairs_ko_pep", "pair": ("KO", "PEP"), "tx_cost_bps": 10},
    {"stem": "gatev_2006_pairs_ewa_ewc", "pair": ("EWA", "EWC"), "tx_cost_bps": 10},
    {"stem": "gatev_2006_pairs_gld_slv", "pair": ("GLD", "SLV"), "tx_cost_bps": 10},
    # Phase 1.1/1.2 (2026-06-11): proper stat-arb on the existing 2-feed engine.
    {"stem": "engle_granger_1987_cointegration_pairs", "pair": ("EWA", "EWC"), "tx_cost_bps": 10},
    {"stem": "elliott_2005_kalman_pairs", "pair": ("GLD", "GDX"), "tx_cost_bps": 10},
]
# N-asset (cross-sectional / portfolio) specs — run via run_multi_backtest on the
# 5-operation universe. ``symbols`` are yfinance tickers AND the per-feed names
# passed to the engine (so e.g. DualMomentum can find its "TLT" defensive leg).
_UNIVERSE_5 = ["SPY", "^N225", "GC=F", "TLT", "CL=F"]  # SPY / NIKKEI / GOLD / TREASURY / OIL
NEW_MULTI_SPECS: list[dict] = [
    # Phase 2 (2026-06-11): cross-sectional & portfolio strategies (need N feeds).
    {"stem": "jegadeesh_titman_1993_cross_sectional_momentum", "symbols": _UNIVERSE_5, "tx_cost_bps": 10},
    {"stem": "antonacci_2014_dual_momentum", "symbols": _UNIVERSE_5, "tx_cost_bps": 10},
    {"stem": "maillard_2010_risk_parity", "symbols": _UNIVERSE_5, "tx_cost_bps": 10},
    # Phase 3 (2026-06-11): PCA / eigenportfolio stat-arb (N-feed + heavier math).
    {"stem": "avellaneda_lee_2010_pca_statarb", "symbols": _UNIVERSE_5, "tx_cost_bps": 10},
    # Third wave (2026-06-11): faithful-scale Gatev — formation/trading cycle over
    # the top-20 SSD pairs of a 26-ETF universe (joined window starts ~2006 at the
    # latest inception: SLV 2006-04 / GDX 2006-05 / DBC 2006-02).
    {
        "stem": "gatev_2006_portfolio_of_pairs",
        "symbols": [
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
        ],
        "tx_cost_bps": 10,
    },
]


# ── PBO (CSCV) — single source: archimedes_analytics_engine.pbo ───────────────
# (Moved there 2026-06-11 for the library-PBO work; it remains a line-for-line
# mirror of backend rigor_evaluator.compute_pbo, parity-tested in backend/tests.)
from archimedes_analytics_engine.pbo import compute_pbo

# ── Rigor gate (full criteria incl. paper-claim check) ─────────────────────────


def _compute_passes_rigor_gate(
    *,
    passes_validation: bool,
    dsr: float | None,
    dsr_p: float | None,
    num_trials: int | None,
    full_sharpe: float | None,
    oos_sharpe: float | None,
    pbo_score: float | None,
    look_ahead_passed: bool | None,
    sharpe_vs_paper: float | None,
) -> bool:
    """Mirror BacktestResult.passes_rigor_gate criteria 1-7."""
    if not passes_validation:
        return False
    if dsr is None or dsr_p is None or num_trials is None:
        return False
    if dsr_p < DSR_P_BADGE_MIN:
        return False
    if pbo_score is None or pbo_score >= 0.5:
        return False
    if look_ahead_passed is not True:
        return False
    if oos_sharpe is None:
        return False
    if full_sharpe is not None and full_sharpe > 0 and oos_sharpe / full_sharpe < 0.5:
        return False
    # Criterion 7: paper-claim delta (only when a paper Sharpe is declared).
    return not (sharpe_vs_paper is not None and sharpe_vs_paper < 0.5)


# ── Helpers ────────────────────────────────────────────────────────────────────


def _correlation_to(daily_returns: list[float], benchmark_returns: list[float]) -> float | None:
    n = min(len(daily_returns), len(benchmark_returns))
    if n < 3:
        return None
    a = np.asarray(daily_returns[-n:], dtype=float)
    b = np.asarray(benchmark_returns[-n:], dtype=float)
    if float(np.ptp(a)) == 0.0 or float(np.ptp(b)) == 0.0:
        return None
    return round(float(np.corrcoef(a, b)[0, 1]), 6)


def _passes_validation(result) -> bool:
    return (
        result.sharpe_ratio is not None
        and result.sharpe_ratio > 0.5
        and result.max_drawdown_pct is not None
        and (result.max_drawdown_pct / 100.0) < 0.5
        and result.cagr is not None
        and result.cagr < 10.0
        and (result.total_trades < 2 or result.total_trades >= 10)
    )


def _build_entry(result, *, metadata: dict, num_trials: int, corr_spy: float | None, tx_cost_bps: int) -> dict:
    daily = result.daily_returns
    dsr, dsr_p = compute_dsr(daily, num_trials)
    oos = compute_oos_sharpe(daily)
    kelly = compute_kelly(daily)

    max_dd_frac = (result.max_drawdown_pct / 100.0) if result.max_drawdown_pct is not None else None
    calmar = None
    if result.cagr is not None and max_dd_frac and max_dd_frac > 0:
        calmar = round(result.cagr / max_dd_frac, 7)

    # strategy_loader lowercases module-constant keys (PAPER_CLAIMED_SHARPE -> paper_claimed_sharpe).
    paper_sharpe = metadata.get("paper_claimed_sharpe")
    sharpe_vs_paper = None
    if paper_sharpe and result.sharpe_ratio is not None and paper_sharpe != 0:
        sharpe_vs_paper = result.sharpe_ratio / paper_sharpe

    return {
        "n_obs_daily": len(daily),
        "sharpe_ratio": round(result.sharpe_ratio, 10) if result.sharpe_ratio is not None else None,
        "sortino_ratio": round(result.sortino_ratio, 10) if result.sortino_ratio is not None else None,
        "max_drawdown": round(max_dd_frac, 16) if max_dd_frac is not None else None,
        "cagr": round(result.cagr, 16) if result.cagr is not None else None,
        "calmar_ratio": calmar,
        "win_rate": result.win_rate,
        "profit_factor": result.profit_factor,
        "total_trades": result.total_trades,
        "avg_holding_period_days": result.avg_holding_period_days,
        "correlation_to_spy": corr_spy,
        "correlation_to_btc": None,
        "out_of_sample_sharpe": oos,
        "look_ahead_audit_passed": result.look_ahead_audit_passed,
        "backtest_engine": "backtrader",
        "transaction_cost_bps": tx_cost_bps,
        "backtest_start": result.backtest_start,
        "backtest_end": result.backtest_end,
        "paper_claimed_sharpe": metadata.get("paper_claimed_sharpe"),
        "paper_claimed_cagr": metadata.get("paper_claimed_cagr"),
        "paper_claimed_max_dd": metadata.get("paper_claimed_max_dd"),
        "deflated_sharpe_ratio": dsr,
        "dsr_p_value": dsr_p,
        # Sharpe convention used for the DSR above. "excess" subtracts the daily
        # risk-free rate (#547); legacy frozen entries are tagged "raw".
        "dsr_convention": "excess",
        "num_trials_in_selection": num_trials,
        "pbo_score": None,  # filled in after cohort PBO is computed
        "passes_rigor_gate": _compute_passes_rigor_gate(
            passes_validation=_passes_validation(result),
            dsr=dsr,
            dsr_p=dsr_p,
            num_trials=num_trials,
            full_sharpe=result.sharpe_ratio,
            oos_sharpe=oos,
            pbo_score=None,  # provisional; re-evaluated after PBO below
            look_ahead_passed=result.look_ahead_audit_passed,
            sharpe_vs_paper=sharpe_vs_paper,
        ),
        "kelly_fraction": kelly,
    }


def curated_num_trials(existing_fixture_count: int, new_entry_count: int) -> int:
    """``num_trials_in_selection`` for every curated fixture entry this script
    writes (V1 fix, num_trials-provenance audit 2026-08-03).

    ALWAYS 1: every strategy backtested here is a hand-implemented published
    paper, not the output of a search of ours, so there is no candidate pool
    to deflate against — never the curated library's size (Dan's decision,
    2026-07-27: "For a hand-curated implementation of one published paper
    there is no search of ours, so num_trials = 1"). The arguments exist only
    so this function's contract is independently unit-testable against the
    PREVIOUS (buggy) formula it replaces — ``existing_fixture_count +
    new_entry_count`` (the post-add library size) — without needing to run
    the whole fetch-and-backtest pipeline; the fix is that this now ignores
    both.
    """
    return 1


# ── Main ──────────────────────────────────────────────────────────────────────


def main(write: bool = False) -> None:
    try:
        fixtures = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise SystemExit(
            f"Cannot read fixture file {_FIXTURE_PATH}: {exc}\n"
            "backtest_fixtures.json is no longer committed (DB migration) — seed a local "
            "snapshot first: conda run -n archimedes python "
            "backend/scripts/export_backtest_fixtures.py"
        ) from exc

    # Add-only + idempotent: the spec lists are a cumulative catalog of how every
    # fixture entry was generated, but a given run only (re)backtests the stems not
    # already present in the fixture file. Re-running after a previous --write is a
    # no-op. The hard collision guard at the end is the final safety net.
    pending_single = [s for s in NEW_SINGLE_SPECS if s["stem"] not in fixtures]
    pending_pair = [s for s in NEW_PAIR_SPECS if s["stem"] not in fixtures]
    pending_multi = [s for s in NEW_MULTI_SPECS if s["stem"] not in fixtures]
    skipped = [s["stem"] for s in (*NEW_SINGLE_SPECS, *NEW_PAIR_SPECS, *NEW_MULTI_SPECS) if s["stem"] in fixtures]
    if skipped:
        print(f"Add-only: skipping {len(skipped)} stem(s) already in fixtures: {sorted(skipped)}")
    if not pending_single and not pending_pair and not pending_multi:
        print("Nothing to add — all catalog specs are already in the fixture file.")
        return

    # num_trials_in_selection = 1 for every curated fixture entry this script
    # writes (V1 fix, num_trials-provenance audit 2026-08-03). Every strategy
    # backtested here is a hand-implemented published paper — the fixture
    # generation process is not itself a search, so there is nothing to deflate
    # against (Dan's decision, 2026-07-27: "For a hand-curated implementation
    # of one published paper there is no search of ours, so num_trials = 1").
    #
    # The PREVIOUS convention below stamped the count of every curated fixture
    # in the file (existing + newly-added) into EACH new entry's
    # num_trials_in_selection, deflating each individually-authored strategy by
    # how many OTHER strategies happen to sit in the library — exactly the
    # cross-strategy coupling the decision forbids outside Leaderboard/
    # Marketplace, and the same library-size-into-num_trials shape as the
    # num_trials ∈ {25, 4} buckets observed live in production
    # `backtest_results` rows tagged backtest_engine='backtrader' (this script
    # writes to a separate table, `strategy_backtest_fixtures`, not
    # `backtest_results` — the two were never mechanically linked — but it is
    # the same anti-pattern, and fixing it here closes off the one remaining
    # place in the codebase that still manufactures a library-size num_trials
    # value for a curated entry):
    #   num_trials = len(fixtures) + len(pending_single) + len(pending_pair) + len(pending_multi)
    num_trials = curated_num_trials(len(fixtures), len(pending_single) + len(pending_pair) + len(pending_multi))
    print(f"num_trials_in_selection for every new entry: {num_trials} (curated = no search of ours)")

    print(f"Fetching SPY benchmark {BACKTEST_START} → {BACKTEST_END}…")
    spy = fetch_ohlcv("SPY", BACKTEST_START, BACKTEST_END)
    spy_returns = [(float(spy["Close"].iloc[i]) / float(spy["Close"].iloc[i - 1])) - 1.0 for i in range(1, len(spy))]

    new_entries: dict[str, dict] = {}
    cohort_returns: dict[str, list[float]] = {}

    for spec in pending_single:
        stem = spec["stem"]
        bundle = load_strategy(_STRATEGIES_DIR / f"{stem}.py")
        print(f"Backtesting {stem} on {spec['symbol']}…")
        prices = fetch_ohlcv(spec["symbol"], BACKTEST_START, BACKTEST_END)
        result = run_backtest(
            prices, strategy_cls=bundle.cls, initial_cash=INITIAL_CASH, transaction_cost_bps=spec["tx_cost_bps"]
        )
        corr = _correlation_to(result.daily_returns, spy_returns)
        new_entries[stem] = _build_entry(
            result, metadata=bundle.metadata, num_trials=num_trials, corr_spy=corr, tx_cost_bps=spec["tx_cost_bps"]
        )
        cohort_returns[stem] = result.daily_returns
        print(
            f"  sharpe={result.sharpe_ratio:+.4f} cagr={result.cagr:+.4f} dd={result.max_drawdown_pct:.1f}% trades={result.total_trades} corr_spy={corr}"
        )

    for spec in pending_pair:
        stem = spec["stem"]
        sym_a, sym_b = spec["pair"]
        bundle = load_strategy(_STRATEGIES_DIR / f"{stem}.py")
        print(f"Backtesting {stem} on {sym_a}/{sym_b}…")
        pa = fetch_ohlcv(sym_a, BACKTEST_START, BACKTEST_END)
        pb = fetch_ohlcv(sym_b, BACKTEST_START, BACKTEST_END)
        result = run_pairs_backtest(
            pa,
            pb,
            strategy_cls=bundle.cls,
            initial_cash=INITIAL_CASH,
            name_a=sym_a,
            name_b=sym_b,
            transaction_cost_bps=spec["tx_cost_bps"],
        )
        corr = _correlation_to(result.daily_returns, spy_returns)
        new_entries[stem] = _build_entry(
            result, metadata=bundle.metadata, num_trials=num_trials, corr_spy=corr, tx_cost_bps=spec["tx_cost_bps"]
        )
        cohort_returns[stem] = result.daily_returns
        print(
            f"  sharpe={result.sharpe_ratio:+.4f} cagr={result.cagr:+.4f} dd={result.max_drawdown_pct:.1f}% trades={result.total_trades} corr_spy={corr}"
        )

    for spec in pending_multi:
        stem = spec["stem"]
        symbols = spec["symbols"]
        bundle = load_strategy(_STRATEGIES_DIR / f"{stem}.py")
        print(f"Backtesting {stem} on universe {symbols}…")
        prices_list = [fetch_ohlcv(sym, BACKTEST_START, BACKTEST_END) for sym in symbols]
        result = run_multi_backtest(
            prices_list,
            strategy_cls=bundle.cls,
            initial_cash=INITIAL_CASH,
            names=symbols,
            transaction_cost_bps=spec["tx_cost_bps"],
        )
        corr = _correlation_to(result.daily_returns, spy_returns)
        new_entries[stem] = _build_entry(
            result, metadata=bundle.metadata, num_trials=num_trials, corr_spy=corr, tx_cost_bps=spec["tx_cost_bps"]
        )
        cohort_returns[stem] = result.daily_returns
        print(
            f"  sharpe={result.sharpe_ratio:+.4f} cagr={result.cagr:+.4f} dd={result.max_drawdown_pct:.1f}% trades={result.total_trades} corr_spy={corr}"
        )

    # Cohort-level PBO across the new strategies, then re-evaluate the gate with it.
    print("\nComputing cohort PBO across new strategies (CSCV)…")
    pbo_by_stem = compute_pbo(cohort_returns)
    cohort_pbo = next(iter(pbo_by_stem.values())) if pbo_by_stem else None
    print(f"  cohort PBO = {cohort_pbo}  (NOTE: new-cohort only, not full-library; see module docstring)")

    for stem, entry in new_entries.items():
        entry["pbo_score"] = pbo_by_stem.get(stem)
        paper_sharpe = entry["paper_claimed_sharpe"]
        sharpe_vs_paper = (
            entry["sharpe_ratio"] / paper_sharpe
            if (paper_sharpe and entry["sharpe_ratio"] is not None and paper_sharpe != 0)
            else None
        )
        entry["passes_rigor_gate"] = _compute_passes_rigor_gate(
            passes_validation=(
                entry["sharpe_ratio"] is not None
                and entry["sharpe_ratio"] > 0.5
                and entry["max_drawdown"] is not None
                and entry["max_drawdown"] < 0.5
                and entry["cagr"] is not None
                and entry["cagr"] < 10.0
                and (entry["total_trades"] < 2 or entry["total_trades"] >= 10)
            ),
            dsr=entry["deflated_sharpe_ratio"],
            dsr_p=entry["dsr_p_value"],
            num_trials=entry["num_trials_in_selection"],
            full_sharpe=entry["sharpe_ratio"],
            oos_sharpe=entry["out_of_sample_sharpe"],
            pbo_score=entry["pbo_score"],
            look_ahead_passed=entry["look_ahead_audit_passed"],
            sharpe_vs_paper=sharpe_vs_paper,
        )

    print("\n── New fixture entries ────────────────────────────────")
    print(json.dumps(new_entries, indent=2))
    print("\n── Rigor-gate summary ─────────────────────────────────")
    for stem, entry in new_entries.items():
        print(
            f"  {stem:34s} sharpe={entry['sharpe_ratio']:+.3f} "
            f"DSR_p={entry['dsr_p_value']} PBO={entry['pbo_score']} "
            f"passes_gate={entry['passes_rigor_gate']}"
        )

    # Sanity: refuse to overwrite an existing entry.
    collisions = set(new_entries) & set(fixtures)
    if collisions:
        raise SystemExit(f"Refusing to overwrite existing fixture entries: {sorted(collisions)}")

    if write:
        fixtures.update(new_entries)
        _FIXTURE_PATH.write_text(json.dumps(fixtures, indent=2) + "\n", encoding="utf-8")
        print(f"\n✓ Added {len(new_entries)} entries to {_FIXTURE_PATH} (legacy entries untouched)")
    else:
        print("\n(dry-run — pass --write to add these entries; legacy entries are never modified)")


if __name__ == "__main__":
    main(write="--write" in sys.argv)
