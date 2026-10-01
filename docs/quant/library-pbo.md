# Library-level PBO — findings (fourth wave, task 1)

> **Author:** Önder, 2026-06-11. **Lineage:** roadmap Priority 3.3 in
> [`quant-roadmap.md`](../plans/quant-roadmap.md); task 1 of
> [`fourth-wave-handover.md`](../handovers/fourth-wave-handover.md); evidence base in
> [`third-wave-retest.md`](third-wave-retest.md).
>
> **Status of these numbers:** **Historical — read at their vintage.** A parallel
> diagnostic run on **2026-06-11**, over **22 of the 23 strategies in the library on that
> date**. The shelf has grown since (count it with `ls analytics-engine/strategies/*.py`);
> the 0.047 headline is a 22-strategy figure, and because CSCV PBO is a property of the
> selection set, **every library addition changes it** — do not quote this number as the
> library PBO of a larger shelf. One pass-count phrasing was retracted in place on
> 2026-08-31 ([#1598](https://github.com/aprin-labs/archimedes/issues/1598)). These numbers do
> **not** replace the per-cohort `pbo_score` values
> in `backtest_fixtures.json` (different data vintage, different selection
> set; the fixture file is add-only). Whether/how to promote library PBO into
> the served passports is an open team decision — see the follow-up issue.

## The headline number

**Library-level PBO (Bailey et al. 2014 CSCV) over 22 strategies = 0.047.**

| Setting | Value |
| --- | --- |
| Selection set | 22 of the 23 library strategies (see Coverage) |
| Joint window (after calendar alignment) | 2006-05-22 → 2026-04-30, 4709 trading days |
| Partitions S / splits C(S, S/2) | 16 / 12 870 |
| **PBO** | **0.046698** |
| Sensitivity: S = 8 / 12 / 16 | 0.043 / 0.022 / 0.047 |

Interpretation: in only ~4.7% of the 12 870 combinatorial in-sample/out-of-sample
splits does the in-sample-best strategy fall below the out-of-sample median.
**The library is not selection-overfit** — picking the backtest winner from this
shelf is informative, not noise-mining.

## Why PBO is low when most strategies fail — read this before celebrating

A low PBO does *not* say the strategies are good. It says the *ranking* is
stable. Two-thirds of this library has negative Sharpe on the joint window, and
the CSCV sees those failures lose **consistently** — in-sample and
out-of-sample alike — so the in-sample winner is almost never a fluke that
collapses out-of-sample. This is the same conclusion the third-wave re-test
reached from a different angle: the CANDIDATEs are **alpha-absent, not
overfit** (every negative-net CANDIDATE is negative *gross*, and walk-forward
parameter selection rescues nothing). An overfit library — random strategies
mined until something sticks — would sit near PBO ≈ 0.5 (or higher with
anti-persistence). Ours fails honestly and persistently, which CSCV correctly
reads as "selection here is not the problem."

The flip side is worth stating: PBO measures the **selection process**, not the
shelf quality. The gate's other criteria (DSR, OOS Sharpe, validation) carry
the "is the strategy any good" burden; PBO < 0.5 (criterion 4) guards the
"did we fool ourselves by picking the best of many" failure mode. On the
full-library measurement, that guard passes with a wide margin.

## Per-strategy diagnostics (S = 16, 12 870 splits)

How often each strategy is the in-sample best, and where it ranks
out-of-sample when it is (rank quantile: 1.0 = OOS best, 0.0 = OOS worst).
Joint-window Sharpe is the engine-convention Sharpe on the aligned 4709-day
window — it differs from fixture values (different window + vintage).

| strategy | joint-window Sharpe | IS-best in splits | median OOS rank quantile when best |
|---|---|---|---|
| maillard_2010_risk_parity | +0.285 | 3484 | 0.77 |
| moreira_muir_2017_volatility_managed | +0.277 | 1064 | 0.82 |
| pipeline_buy_hold | +0.266 | 3596 | 0.77 |
| brock_1992_dual_ma_crossover | +0.261 | 2878 | 0.77 |
| moskowitz_ooi_pedersen_2012_tsmom | +0.205 | 357 | 0.73 |
| faber_2007_sma200_timing | +0.125 | 7 | 0.64 |
| antonacci_2014_dual_momentum | +0.100 | 1399 | 0.55 |
| george_hwang_2004_52w_high | +0.099 | 36 | 0.59 |
| donchian_breakout | −0.008 | 49 | 0.55 |
| bollinger_2001_band_reversion | −0.145 | 0 | — |
| appel_1979_macd | −0.210 | 0 | — |
| jegadeesh_titman_1993_cross_sectional_momentum | −0.221 | 0 | — |
| avellaneda_lee_2010_pca_statarb | −0.354 | 0 | — |
| ariel_1987_turn_of_month | −0.357 | 0 | — |
| gatev_2006_pairs_distance | −0.391 | 0 | — |
| gatev_2006_pairs_gld_slv | −0.439 | 0 | — |
| connors_alvarez_2009_rsi2 | −0.600 | 0 | — |
| gatev_2006_pairs_ko_pep | −0.662 | 0 | — |
| gatev_2006_pairs_ewa_ewc | −0.735 | 0 | — |
| engle_granger_1987_cointegration_pairs | −0.828 | 0 | — |
| elliott_2005_kalman_pairs | −1.470 | 0 | — |
| gatev_2006_portfolio_of_pairs | −1.538 | 0 | — |

Notable details:

- **The 13 negative-Sharpe strategies are never the in-sample best** in any of
  the 12 870 splits — the CANDIDATE failures are unambiguous at every horizon,
  not artifacts of one unlucky window.
- **Risk parity and buy-and-hold dominate selection** (3484 + 3596 splits) and
  both hold a ~0.77 median OOS rank quantile when selected — that, together with
  `moreira_muir`, `tsmom` and risk parity (the #537 near-miss) holding the top
  joint-window Sharpes, is what a *stable* top shelf looks like.
  *(Retracted 2026-08-31, [#1598](https://github.com/aprin-labs/archimedes/issues/1598): this
  bullet used to identify those strategies by how many of them cleared the gate.
  `CLAUDE.md` forbids quoting a curated-library pass count anywhere; the corrected count is
  **unestablished**, and the ranking stability this bullet is actually about does not depend
  on one. The live rigor gate is the only authority on pass/fail.)*
- Those strategies' fixture-era `pbo_score` was 0.39 (legacy cohort of 4–6);
  the new-cohort entries carry 0.006–0.30. The full-library 0.047 sits inside
  that band but is now measured the way Bailey et al. intend: one selection
  set, simultaneous dated series, honest calendar alignment.

## How the measurement works (what's new)

1. **Daily-returns store** — `analytics-engine/strategies/daily_returns/<stem>.json`,
   one file per strategy: dated daily-return series from a fresh run in the
   strategy's fixture configuration (same spec catalog as `regen_fixtures.py`,
   plus the 4 legacy SPY stems re-run as in `retest_candidates.py`, plus the
   buy-and-hold baseline). Generated by `scripts/gen_daily_returns_store.py`
   — **add-only + idempotent**: existing files are never regenerated or
   overwritten, same law as the fixture file.
2. **Calendar alignment** — `scripts/compute_library_pbo.py` inner-joins the
   series on ISO dates before building the CSCV matrix (the strategies trade
   different calendars: ^N225 vs SPY vs ~2006-start joined pair windows).
   `BacktestResult` now carries `daily_return_dates` (1:1 with
   `daily_returns`) to make this possible.
3. **One formula, parity-tested** — the CSCV implementation lives in
   `archimedes_analytics_engine/pbo.py`, a deliberate mirror of backend
   `rigor_evaluator.compute_pbo`; `backend/tests/test_pbo_parity.py` asserts
   exact-equal outputs so the two can never drift silently.

### Coverage and honesty caveats

- **22 of 23 strategies.** `capital_preservation_tbill` is excluded — its
  fixture models a synthetic T-bill yield, not a tradeable instrument run
  (same exclusion as the third-wave re-test).
- **Fresh measurement, current data.** The legacy strategies' fixture-era
  series cannot be reproduced (yfinance vintage drift — the reason fixtures
  are add-only). Each store file is stamped `data_vintage: 2026-06-11`.
- **Alignment cost.** The joint window starts 2006-05-22 (GDX/SLV-era pair
  inceptions); 2004–2006 history (~900 days) is dropped for the strategies
  that have it. The CSCV requires simultaneity; this is the honest price.
- **CSCV granularity.** With N = 22 the OOS rank quantile takes 22 discrete
  values; PBO is granular at the third decimal. S-sensitivity (0.022–0.047)
  is the right error bar to quote, not the headline's six decimals.

## Reproduce

```bash
cd analytics-engine
uv run python scripts/gen_daily_returns_store.py --write   # no-op if store exists (add-only)
uv run python scripts/compute_library_pbo.py
uv run pytest                                              # incl. tests/test_library_pbo.py
# parity with the backend gate implementation:
PYTHONPATH=backend /tmp/abe/bin/python -m pytest backend/tests/test_pbo_parity.py -q
```

## What this does NOT change (and the open decision)

- No `pbo_score` in `backtest_fixtures.json` was touched; no gate verdict
  moved. The served per-strategy PBO values remain the cohort-level ones,
  disclosed as such in the fixture-generation docstrings.
- **Open team decision** (tracked in the follow-up issue): whether passports
  should additionally surface the library-level PBO (e.g. as a
  `library_pbo` field with its vintage), and on what refresh cadence —
  since CSCV PBO is a property of the selection set, every library addition
  changes it, which fits a "computed at evaluation time" surface better than
  a frozen per-strategy fixture value.
