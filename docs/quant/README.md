# Quantitative Methodology Docs

> The math layer of Archimedes, written for teammates, judges, and anyone reading a
> strategy passport. Author: Önder Akkaya (quant / math lane). Written 2026-06-12.

This directory holds **four living references** and **four dated findings notes**. The
references explain the statistics and portfolio math behind the
rigor-as-the-wedge story. They are the conceptual companions to the canonical
operational spec [`../specs/selection-bias-corrections-spec.md`](../specs/selection-bias-corrections-spec.md)
and the judge-facing summary [`../rigor-methods.md`](../rigor-methods.md); where
thresholds or formulas overlap, the spec and the code
([`../../backend/archimedes/services/rigor_evaluator.py`](../../backend/archimedes/services/rigor_evaluator.py),
[`../../backend/archimedes/services/portfolio_optimizer.py`](../../backend/archimedes/services/portfolio_optimizer.py))
are authoritative.

| Doc | What it covers |
|---|---|
| [`methodology.md`](methodology.md) | The full quantitative methodology — selection-bias corrections (DSR, PBO/CSCV, walk-forward OOS, look-ahead, FDR vs FWER, circular block bootstrap) and portfolio construction (MVO, GMV, Max-Sharpe, Kelly, HRP, Black–Litterman, Ledoit–Wolf shrinkage). |
| [`backtest-interpretation.md`](backtest-interpretation.md) | How to read a backtest adversarially — the red flags (IS/OOS cliff, parameter sensitivity, smooth curves, concentration, regime turnover, correlation clustering) and green lights, each mapped to the detector in our codebase. |
| [`admission-criteria.md`](admission-criteria.md) | The four-gate Tier-1 admission contract, the CANDIDATE → VALIDATED flow, principled exceptions, and post-admission monitoring. |
| [`strategy-library.md`](strategy-library.md) | A per-strategy reference for the strategy files in `analytics-engine/strategies/`, grouped by sleeve, with the honest paper-vs-v1 adaptation caveats. |

**Read in this order** if you are new: `methodology.md` → `backtest-interpretation.md`
→ `admission-criteria.md` → `strategy-library.md`.

## Findings notes — dated, historical, not living references

*(Table added 2026-08-31, [#1598](https://github.com/aprin-labs/archimedes/issues/1598): these
four were in the directory but not in this index, and they carry the numbers most likely to
be quoted.)* **Every one of them is a measurement at a stated vintage, not a current
statement.** Each was run against the library, gate threshold, and `num_trials` convention
in force on its date — all three have changed since. Read the status banner at the top of
each note before quoting a figure out of it, and never quote a pass count from one: per
`CLAUDE.md` the corrected curated-library pass count is **unestablished**, and the live
rigor gate is the only authority on which strategies clear it.

| Note | Vintage | What it measured |
|---|---|---|
| [`library-pbo.md`](library-pbo.md) | 2026-06-11 | Library-level CSCV PBO over 22 of the 23 strategies then in the library. The headline is a 22-strategy figure; CSCV PBO changes with every library addition. |
| [`third-wave-retest.md`](third-wave-retest.md) | 2026-06-11 | The CANDIDATEs through the turnover-aware cost model and the walk-forward parameter selector: are the failures cost-bled or alpha-absent? |
| [`second-wave-universe-experiment.md`](second-wave-universe-experiment.md) | 2026-06-11 | Whether a larger or better-composed universe rescues the second-wave strategies. Its `num_trials` sweep predates the self-containment reversal. |
| [`../analysis/faber-dsr-finding.md`](../analysis/faber-dsr-finding.md) | 2026-05-27 | Why Faber 2007 fails on DSR (p = 0.612) despite a raw Sharpe near TSMOM's — the clearest worked example of DSR beating raw Sharpe as an admission test. Lives in `analysis/`, listed here because the quant docs cite it constantly. |

Two conventions changed under all four notes and are the usual reason a number in them does
not reproduce: the DSR bar moved down in PR #901 and back to `0.95` in #1794 (where it is
now a single named constant), and `num_trials`
stopped counting the library size on 2026-07-09
([`../adr/num-trials-self-containment.md`](../adr/num-trials-self-containment.md), ratified
2026-08-31). Fixture-era series also do not reproduce on current data (yfinance vintage
drift) — which is why the fixture file is add-only.
