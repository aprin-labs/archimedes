# Third-wave re-test: the CANDIDATEs through the cost model + walk-forward

> **Status:** Findings note, 2026-06-11 (Önder, quant lane). **Historical — read at its
> vintage.** Measured on 2026-06-11 over the 22-of-23-strategy library of that date; the
> shelf has grown since (count it with `ls analytics-engine/strategies/*.py`). Two pass-count
> phrasings were retracted in place on 2026-08-31
> ([#1598](https://github.com/aprin-labs/archimedes/issues/1598)); the diagnostics themselves
> are untouched. Item 4 of the
> third wave (see [`third-wave-handover.md`](../handovers/third-wave-handover.md) §11).
> Companion to [`transaction-cost-turnover-model.md`](../specs/transaction-cost-turnover-model.md)
> (item 1) and the walk-forward harness (item 3, `walk_forward.py`).
> Reproduce with `cd analytics-engine && uv run python scripts/retest_candidates.py`
> (read-only — no fixture is written; the add-only law stands).

## Why this note exists

The first three third-wave items built measurement machinery: a turnover-aware
cost model (gross-vs-net Sharpe, break-even cost), a faithful-scale Gatev
replication, and a walk-forward parameter selector. This note runs the library
through that machinery and answers two questions per CANDIDATE:

1. **Is the failure execution-cost-driven or alpha-absent?** The cost model
   separates the two: a strategy with a healthy *gross* Sharpe that dies net
   of costs is cost-bled (potentially rescuable by turnover control); one that
   is negative *before* costs has no alpha to rescue.
2. **Would honest parameter selection have rescued it?** The walk-forward
   harness picks each strategy's parameters on a trailing 4-year train window
   and evaluates on the next unseen year, rolling — the stitched OOS Sharpe is
   what a non-overfit deployment of that strategy family would actually have
   earned. Grids are small and paper-plausible (the values the papers
   themselves discuss), and `n_param_combos` is recorded per run.

Method notes: fresh runs on current yfinance data (2004→2026 window; the
joined window per strategy follows its instruments' inceptions), 10 bps/side.
Legacy fixture entries are known not to reproduce on current data (vintage
drift), so these are diagnostics, never written back to fixtures.
`capital_preservation_tbill` is skipped (it models a T-bill yield, not a
tradeable instrument run); `pipeline_buy_hold` is skipped as the trivial
baseline. Moreira-Muir, MOP TSMOM and Faber are included as reference rows.
*(Retracted 2026-08-31, [#1598](https://github.com/aprin-labs/archimedes/issues/1598): this
sentence used to label the first two by how many of them cleared the gate, and to give
Faber a LIVE status. Both are pass/fail claims a doc must not make — `CLAUDE.md` forbids
quoting a curated-library pass count anywhere, and the corrected count is
**unestablished**. They are reference rows because they were the shelf's strongest
performers at this vintage, which is all this note needs them for. The live rigor gate is
the only authority on status.)*

## Part A — cost/turnover diagnosis (fresh runs, current data, 10 bps/side)

Reference rows first (Faber, the 52-week-high legacy, Moreira-Muir, MOP TSMOM),
then the CANDIDATEs. "Gross" adds back commissions only (not
slippage); "break-even" is the per-side cost at which the gross CAGR is fully
consumed — the implementability headroom.

| strategy | net Sharpe | gross Sharpe | turnover ×/yr | cost drag %/yr | break-even bps | trades |
|---|---|---|---|---|---|---|
| faber_2007_sma200_timing | +0.13 | +0.19 | 2.98 | 0.60 | 109.7 | 72 |
| george_hwang_2004_52w_high | +0.09 | +0.13 | 2.17 | 0.43 | 134.7 | 51 |
| moreira_muir_2017_volatility_managed | +0.25 | +0.26 | 1.07 | 0.21 | 360.7 | 0 |
| moskowitz_ooi_pedersen_2012_tsmom | +0.15 | +0.17 | 1.52 | 0.30 | 214.4 | 36 |
| connors_alvarez_2009_rsi2 | −0.58 | −0.33 | 7.70 | 1.54 | 18.0 | 170 |
| bollinger_2001_band_reversion | −0.15 | −0.08 | 4.43 | 0.89 | 35.0 | 99 |
| donchian_breakout | −0.12 | −0.02 | 4.50 | 0.90 | 48.4 | 99 |
| appel_1979_macd | −0.30 | −0.09 | 10.41 | 2.08 | 16.6 | 234 |
| brock_1992_dual_ma_crossover | +0.21 | +0.22 | 0.44 | 0.09 | 808.2 | 10 |
| ariel_1987_turn_of_month | −0.32 | −0.05 | 11.60 | 2.32 | 17.9 | 260 |
| gatev_2006_pairs_distance | −0.39 | −0.37 | 1.15 | 0.23 | 21.9 | 44 |
| gatev_2006_pairs_ko_pep | −0.75 | −0.70 | 1.28 | 0.26 | 46.9 | 56 |
| gatev_2006_pairs_ewa_ewc | −0.79 | −0.74 | 1.29 | 0.26 | 32.1 | 56 |
| gatev_2006_pairs_gld_slv | −0.30 | −0.28 | 1.22 | 0.24 | 0.0 | 46 |
| engle_granger_1987_cointegration_pairs | −0.92 | −0.84 | 2.21 | 0.44 | 13.4 | 102 |
| elliott_2005_kalman_pairs | −1.47 | −0.75 | 22.90 | 4.58 | 0.3 | 1174 |
| jegadeesh_titman_1993_cross_sectional_momentum | −0.21 | −0.19 | 1.87 | 0.37 | 0.0 | 141 |
| antonacci_2014_dual_momentum | +0.10 | +0.12 | 2.09 | 0.42 | 115.7 | 49 |
| maillard_2010_risk_parity | +0.35 | +0.36 | 0.35 | 0.07 | 1221.1 | 1 |
| avellaneda_lee_2010_pca_statarb | −0.33 | n/a¹ | 19.91 | 3.98 | n/a¹ | 935 |
| gatev_2006_portfolio_of_pairs | −1.58 | −1.37 | 3.99 | 0.80 | 0.0 | 983 |

¹ PCA's equity curve goes *negative* (minimum −$46k on $100k initial; 130%
drawdown) — gross-return reconstruction is mathematically undefined once
equity ≤ 0, and the engine reports `None` rather than a fabricated number.
That a "market-neutral" strategy can lose more than its capital at N=5 is
itself the finding (already documented on its passport).

### Part A findings

1. **The failures are alpha-absent, not cost-bled.** Every negative-net
   CANDIDATE is *also negative gross*. Even at zero cost, RSI-2 sits at −0.33,
   the pairs family at −0.28…−0.84, the portfolio-of-pairs at −1.37. The
   "Kalman hypothesis" — that execution costs were the binding constraint —
   is **rejected**: Kalman improves from −1.47 to −0.75 gross (costs *do*
   destroy 4.6%/yr at 22.9× turnover), but the costless version still fails
   decisively. No CANDIDATE's verdict would flip even with free execution.
2. **The gate implicitly selects for implementability.** The survivors and
   near-misses all have wide break-even headroom: risk parity 1221 bps, Brock
   dual-MA 808, Moreira-Muir 361, TSMOM 214, dual momentum 116. The failures
   sit at ≤ 50 bps — many at ~0 — meaning even institutional-grade execution
   could not make them investable. Break-even cost is the cleanest single
   screening number this re-test produced.
3. **Turnover sees what trade-count cannot.** Moreira-Muir reports **0
   closed trades** (it holds one continuously-resized position, which
   backtrader's TradeAnalyzer never counts as a round trip) yet turns over
   1.07× equity per year, paying a real 0.21%/yr. The fixture-level
   `total_trades` field materially understates activity for resize-style
   strategies; turnover is the honest activity metric.

## Part B — walk-forward parameter selection (train 1008 bars / test 252, OOS stitched)

| strategy | combos | folds | WF OOS Sharpe | default full-sample Sharpe | chosen params (mode) |
|---|---|---|---|---|---|
| faber_2007_sma200_timing | 4 | 18 | +0.05 | +0.13 | sma_period=210 (8/18 folds) |
| donchian_breakout | 9 | 18 | +0.05 | −0.12 | entry=20, exit=50 (12/18 folds) |
| connors_alvarez_2009_rsi2 | 3 | 18 | −0.29 | −0.58 | rsi_entry=15 (16/18 folds) |
| bollinger_2001_band_reversion | 6 | 18 | −0.16 | −0.15 | period=20, dev=2.0 (8/18 folds) |
| brock_1992_dual_ma_crossover | 9 | 18 | +0.17 | +0.21 | fast=5, slow=200 (8/18 folds) |
| maillard_2010_risk_parity | 3 | 16 | +0.14 | +0.35 | lookback=42 (9/16 folds) |

### Part B findings

1. **Honest parameter selection rescues nothing.** No strategy's stitched
   OOS Sharpe comes anywhere near the gate. Donchian flips sign (−0.12 →
   +0.05) and RSI-2 halves its losses (−0.58 → −0.29), but "less bad" is not
   investable. The gate's verdicts are hereby confirmed by an independent
   method: these families fail not because we fixed the wrong parameter but
   because the underlying edge is not there on 2004–2026 data.
2. **Parameter instability is the overfitting signature.** The modal choice
   wins only ~half the folds for most strategies (Faber 8/18, Bollinger 8/18,
   Brock 8/18) — the "best" parameter is regime-dependent, which is exactly
   the condition under which in-sample selection overfits and PBO is the
   right alarm. RSI-2 is the exception (16/18 stable) and still loses.
3. **Risk parity's +0.35 is tempered, honestly.** Walk-forward over
   lookback ∈ {42, 63, 126} delivers +0.14 OOS vs +0.35 for the fixed
   default (63). Read precisely: the default was *not* mined (it is the
   3-month convention the literature uses), but the +0.35 does benefit from
   configuration luck that an adaptive selector would not have captured.
   This belongs in the [#537](https://github.com/aprin-labs/archimedes/issues/537)
   discussion as evidence on BOTH sides: `n_param_combos` here was 3 (not
   22), supporting the provenance-based `num_trials` split — while the
   +0.14 OOS suggests the recovered strategy should be sized conservatively.

## Conclusions

- **No CANDIDATE is promoted by this re-test, and that is the result.** The
  second-wave conclusion ("the gate is robust, not punitive") now has two
  independent confirmations: zero-cost re-runs don't flip any verdict, and
  honest OOS parameter selection doesn't either.
- **The cost model changes how we *read* strategies, not which ones pass:**
  break-even headroom cleanly separates the investable shelf (≥ 100 bps) from
  the uninvestable one (≤ 50 bps), and turnover exposes activity that
  trade-count misses.
- **What would actually move the needle** (carried forward on the roadmap):
  Kelly-sized allocation from the rigor metrics (Priority 3.1), library-level
  PBO from a daily-returns store (3.3), and the #537 `num_trials` decision —
  the one open item where a strategy's verdict legitimately depends on a
  policy choice rather than on the data.

## Non-goals honored

- No fixture entry was written or modified (`backtest_fixtures.json`
  untouched; verify with `git diff`).
- No gate logic, thresholds, or `num_trials` policy changed — #537 receives
  evidence, not a unilateral decision.
- No parameter from Part B was promoted into any strategy file's defaults;
  that would be in-sample selection wearing a costume.

