# Rigor Methods — How Archimedes Stress-Tests Every Strategy

> **Status:** Shipped — all four gates (DSR, PBO, walk-forward OOS, look-ahead audit) are live in [`services/rigor_evaluator.py`](../backend/archimedes/services/rigor_evaluator.py) (canonical) and gate every Tier-1 strategy. How many strategies pass all four is not recorded in this document — the live rigor gate is the only authority on which strategies currently pass; see the PASS/CANDIDATE badges in the app and `backend/archimedes/services/live_rigor_gate.py`.
> Faber 2007 does *not* pass — see [`analysis/faber-dsr-finding.md`](analysis/faber-dsr-finding.md).
>
> **Audience:** Judges, team members, and anyone reading a strategy passport who is not a quant.
> **Author:** Önder Akkaya (Lead Quant)
> **Date:** 2026-05-19

Every Tier-1 strategy in Archimedes must pass four quantitative gates before it can be
promoted from `candidate` → `validated`. This page explains each one in plain English,
why it matters, and what the number shown in the UI actually means.

---

## Why this matters at all

Most AI finance tools backtest a strategy, see a high Sharpe ratio, and call it done.
The problem: a high Sharpe on historical data is easy to manufacture accidentally.
You can search through hundreds of parameter combinations — moving average windows,
lookback periods, thresholds — until one "works." If you do that, the backtest has
essentially memorized the past rather than discovered a real edge. The strategy will
likely fail going forward.

This is called **overfitting**, and academic research quantifies how common it is.
Bailey et al. (2015, "The Probability of Backtest Overfitting") showed that for a
strategy with 45 trials, over half of seemingly-positive backtests are pure luck.

Archimedes surfaces three metrics — DSR, PBO, and OOS Sharpe — specifically to catch
this. If a strategy looks good on all three, the Sharpe ratio is not an accident.

---

## 1. Deflated Sharpe Ratio (DSR)

**What the gate actually states.** This is the wording the app publishes on the
Architecture page, and it is the only defensible form — docs match the app, not the other
way round:

> Over 20+ years of backtested returns net of realistic commission, a strategy's excess Sharpe
> must be positive at 95% one-sided confidence under standard errors robust to non-normality
> and autocorrelation, and must stay positive on a 30% chronological holdout. On the generated
> path, the Sharpe is additionally deflated against that strategy's own candidate pool.
>
> **On the curated library, `num_trials=1` — DSR runs undeflated (no multiple-testing
> correction).**

**The question it answers:** "Once the standard errors account for the fat tails and
autocorrelation in these returns, is the excess Sharpe distinguishable from zero?"

**The idea in one sentence:** A raw Sharpe ratio is misleading when returns are skewed,
kurtotic or autocorrelated; the Deflated Sharpe Ratio widens the standard error to
account for that, and — *where a search actually happened* — additionally deflates by the
expected best-of-`N` under the null.

**What the number means:**
- DSR is displayed as the **p-value** of the test (range 0–1).
- A p-value ≥ 0.95 clears the bar: the excess Sharpe is positive at 95% one-sided
  confidence under non-normality- and autocorrelation-robust standard errors. That
  threshold has one definition in the codebase — `DSR_P_BADGE_MIN` in
  `backend/archimedes/services/rigor_profiles.py` — and the four-primitive table below
  quotes it, not a second copy (#1794).
- Below 0.95 means the strategy has not cleared the bar — it may still be a good
  strategy, but we cannot distinguish it from noise with this sample.

**Where the deflation does and does not apply.** On the **generated** path the Sharpe is
deflated against that strategy's own candidate pool — the search we ran is the search we
pay for. On the **curated library** `num_trials = 1`, so `E[max_N] = 0` and DSR runs
**undeflated**: there was no search of ours to charge for, and a published strategy's
score must not change because the library around it grew (see
[`adr/num-trials-self-containment.md`](adr/num-trials-self-containment.md)).
`rigor_evaluator.py` logs this verbatim when it fires. Do not describe the curated gate as
correcting for multiple testing; on that path it does not.

**Disclosure is not correction, on the curated per-strategy gate.** The product *discloses*
the board-level selection bias a user incurs by choosing the best of N displayed strategies;
it does not *correct* the individual strategy's DSR for it. Benjamini–Hochberg helpers exist
in [`_rigor_helpers.py:1271`](../backend/archimedes/services/_rigor_helpers.py) (`benjamini_hochberg_fdr`)
and, as of #1185, DO have a live non-test caller —
[`compute_board_level_fdr`](../backend/archimedes/services/rigor_evaluator.py) — but it is
deliberately **ADVISORY/annotation only**, not wired into `RigorGateResult.passes_all` or
any strictness threshold (see that function's own docstring for the scope rationale). So
the correction above still holds for the thing it was written about — no per-strategy
`passes_all` verdict moves because of a board-level FDR adjustment — but "zero non-test
callers" is no longer accurate as a blanket statement; correct as of 2026-08-21.

**Where the correction is served, as of #1564 (2026-08-31).** On `GET /api/leaderboard`,
and nowhere else. It used to ride the per-strategy gate response
(`StrategyRigorResult.board_fdr_*` + a top-level `board_level_fdr`); the owner decision is
that the strategy passport carries only information about the strategy itself, and the
leaderboard is the one cross-strategy surface. `GET /api/selection-bias/gate*` now carries
no `board_fdr` key at all — guarded by
`test_selection_bias_routes.py::TestBoardFdrStaysOffThePerStrategyGate`, which fails if one
reappears. The board renders it: a per-row column plus the honest board-level line ("not
yet distinguishable from selection noise at board level" when nothing clears, which is the
state prod is in). An uncorrected row — no finite `dsr_p_value` — renders an em-dash, never
a verdict.

**Why it's better than raw Sharpe:** The standard Sharpe assumes returns are normally
distributed, serially independent, and that you only ran one backtest. None of those is
reliably true. DSR relaxes the first two always, and the third when a candidate pool
exists (Bailey & López de Prado 2014).

---

## 1a. The risk-free rate behind "excess" (issue #1409)

**What changed.** Every "excess Sharpe" above (DSR, walk-forward OOS, in-sample) is
computed against a risk-free rate. Through 2026-08-20 that rate was a flat 5%/year
constant for every strategy, every backtest window, forever. As of #1409 the mechanism
supports grading against the **actual historical 3-month U.S. Treasury bill rate** (FRED
series `DGS3MO`), aligned to each backtest's own per-bar dates — the universal
convention for a USD Sharpe ratio (Sharpe 1994) and the rate Bailey & López de Prado
(2014) implicitly assume when they define the Deflated Sharpe Ratio on excess returns.
**It is not yet the *live default for `run_rigor_gate`***: `run_rigor_gate`'s `dates`
parameter is optional, and as of this PR none of its four production call sites
(`selection_bias_routes.py` ×2, `strategies_routes.py`, `live_rigor_gate.py`) threads
per-bar dates through yet — see "Why production callers still show
`excess_flat_fallback` today" in the PR that shipped this section. Every live **gate
verdict** today still takes the flat-rate path and honestly discloses it via
`rf_convention` below; wiring real per-bar dates into those four callers is tracked
follow-up work, not implied by "the mechanism exists."

**One thing already runs on the real series today, disclosed via its own
`rf_convention`, precisely because it already has real per-bar dates and did not need new
plumbing:**
- `POST /api/rigor/verify` (the CLI's `archimedes verify` backend, #1305) — unlike
  `run_rigor_gate`'s callers, this endpoint's request schema already carries real
  per-bar dates on every call (the CLI builds them from the returns CSV), so no schema
  change was needed to wire it. Disclosed via `RigorVerifyResponse.rf_convention`. This
  is the ONE genuinely new live behavior this PR turns on — a deliberate design choice
  (issue #1409's item 3), not an oversight.

**2026-08-21 round-4 correction.** An earlier draft of this PR also threaded dates
**unconditionally** into `compute_library_pbo` (the library-wide, display-only CSCV PBO,
#546), making it a second live-series exception with no explicit flip-the-switch decision
and no before/after value in the re-grade delta table below. That is now gated behind an
opt-in `use_tbill_series` parameter (default `False`) on `compute_library_pbo` /
`compute_library_pbo_rf_convention`, matching every other not-yet-wired call site in this
file — `_cached_library_pbo` (`selection_bias_routes.py`) calls it at the default, so
`LibraryPbo.rf_convention` is `excess_flat_fallback` today, byte-identical to every
pre-#1409 grade. The mechanism is built and tested (see
`test_matches_the_convention_compute_library_pbo_actually_used`,
`test_rigor_evaluator.py`), ready to flip on the moment product/quant decides to — the
same "wired but not fed" pattern this file already uses for `cv_returns_matrix`/CPCV.

**Disclosed no-dates holdouts.** Two callers of the shared Sharpe helpers stay on the flat
5% convention regardless of whether the gate around them threads dates, by design —
neither is in this issue's scope, and mixing conventions inside one grade only happens
where explicitly noted:
- **Kelly fraction** (`compute_kelly_fraction`, `_rigor_helpers.py`) and the **MVO
  portfolio optimizer** — both take only the flat `rf_annual` constant (see
  `docs/quant/methodology.md`'s risk-free-rate section). Kelly sizing and the optimizer are
  downstream of the gate verdict, not part of the DSR/OOS/IS admission checks this issue
  scoped.
- **`regime_conditional_sharpe`** (reached via `regime_robustness_score`, which
  `run_rigor_gate` DOES call and surface on every gate result long enough to classify more
  than one volatility regime) has no `dates` parameter and always computes on the flat
  5% rate — **this one IS live**, so a grade that threads `dates` mixes a T-bill-series
  DSR/OOS/IS with flat-5% per-regime Sharpes inside the SAME `RigorGateResult`. Advisory
  only (never gates pass/fail), but disclose it here so a reader of `_annualized_sharpe_arr`'s
  docstring (`_rigor_helpers.py`) finds this list, not a dangling citation.
- **`compute_cpcv_oos_sharpe`** also takes no `dates` — out of scope, and moot today since
  it has no production caller (CPCV is reported `NOT_RUN` until a real combinatorial OOS
  matrix is wired in).

**Why a flat constant was wrong.** The 3-month T-bill has ranged from roughly 0.00%
(the 2008–2015 near-zero-rate era, mean 0.25% over the vendored series) to double
digits in the early 1980s (mean 8.27% for 1981–1990, publishing above 5% on every
single day that decade). Across the vendored series' full history, 38.6% of all
published days sit *above* 5%. So a flat 5% constant does not mis-grade in one
direction: it **over-subtracts** (makes the gate too strict) in low-rate windows like
2008–2015 — a strategy backtested through that era was having ~5% subtracted when the
true opportunity cost of cash was closer to 0.25% — and it **under-subtracts** (makes
the gate too lenient) in high-rate windows like the 1980s, where the true rate ran
several points above the flat constant it replaced. The gate was **mis-graded in both
directions**, not uniformly stricter.

**Display Sharpe is unaffected.** This change touches the **gate's excess-return
metrics only** (DSR, OOS Sharpe, in-sample Sharpe) — the passport's **display** Sharpe
stays raw (`rf = 0`), per the gate-excess/display-raw split documented in §1 above
(audit 2026-06-13 #8). Nothing about *what* is disclosed to a user changed, only the
rf input the gate's own pass/fail arithmetic uses.

**Alignment rule.** Per backtest bar date: an exact match uses that date's published
rate; a weekend or market holiday forward-fills from the most recent prior published
date (a Saturday resolves to the preceding Friday's rate); a date up to 14 calendar
days past the vendored series' last published date also forward-fills the same way.
Beyond that grace window the **whole grade** falls back to the flat convention — never
a silent partial substitution.

**Disclosure — `rf_convention`.** Every `RigorGateResult` (the live gate's own object),
`RigorVerifyResponse`, and `LibraryPbo` carries an `rf_convention` field, riding the same
payload path `dsr_convention` already does:
- `excess_tbill_series` — the historical T-bill series was used, per-window aligned.
- `excess_flat_fallback` — the flat rate was used (no date index was available for
  that grade, or the window fell outside the vendored series' coverage). This is a
  **disclosed** fallback state, not a silent one — it is always logged and always
  visible on the result, per this repo's fail-soft principle
  ([`architectural-principles.md`](architectural-principles.md) § fail-soft).
- `MISSING` — no excess-return metric was computed at all (e.g. a too-short or
  degenerate return series), so there is no computation to attribute a convention to;
  distinct from `excess_flat_fallback`, which means a flat-rate computation genuinely ran.

**Scope of that guarantee.** This is a promise about the LIVE gate's own objects, not
every historical persisted summary. Pre-#1409 served/imported fixture rows — the
`StrategyBacktestFixture.dsr_convention` column
(`backend/archimedes/models/backtest_fixtures_store.py`) and the
`analytics-engine/scripts/regen_fixtures.py` / `regen_buy_hold_fixture.py` generators that
populate it — still emit `dsr_convention` with no `rf_convention` companion at all; see
"Deliberately scoped-out `dsr_convention` payload paths" in the PR that shipped this
section for why that gap was disclosed rather than silently left out.

**Where the series lives.** Vendored (not fetched at grade time — determinism is
load-bearing for the same reason the commit-reveal provenance posture is):
[`backend/archimedes/data/rf/DGS3MO.csv`](../backend/archimedes/data/rf/DGS3MO.csv).
Refresh it with `python scripts/refresh_rf_series.py`; see
[`rf_series.py`](../backend/archimedes/services/rf_series.py) for the loader and the
forward-fill / fallback logic in full.

---

## 2. Probability of Backtest Overfitting (PBO)

**The question it answers:** "If we had used a different slice of history to pick this
strategy, would it still look good on the remainder?"

**The idea in one sentence:** Split the historical data into 16 equal chunks, try all
possible ways to divide those chunks into in-sample and out-of-sample, and count how
often the in-sample winner also wins out-of-sample.

**What the number means:**
- PBO is the fraction of splits where the in-sample winner loses out-of-sample.
- PBO = 0% means the strategy won out-of-sample every time (no overfitting detected).
- PBO = 50% means it's a coin flip — essentially random.
- Archimedes requires PBO < 50% for the Tier-1 gate. Lower is better.

**Why it matters:** A genuinely predictive strategy should win on any slice of data,
not just the one it was tuned on. The CSCV method (Bailey et al. 2014) formalizes this
intuition with C(16,8) = 12,870 different IS/OOS splits.

---

## 3. Out-of-Sample Sharpe (Walk-Forward)

**The question it answers:** "Does the strategy still work on data it has never seen?"

**The idea in one sentence:** Train on the first 70% of history, test on the final 30%
without touching the parameters — chronological order preserved, no peeking.

**What the number means:**
- The OOS Sharpe is just the Sharpe ratio computed on the last 30% of the data.
- Archimedes requires OOS Sharpe ≥ 50% of the in-sample Sharpe.
- A ratio near 1.0 means the strategy held up perfectly out-of-sample.
- A ratio near 0 or negative means the edge evaporated the moment the model stopped
  seeing the training data — a textbook overfit.

**Why it complements DSR/PBO:** DSR and PBO are statistical tests; OOS Sharpe is an
economic test. A strategy can pass the statistics but fail if the Sharpe drops 90%
out-of-sample. The trio together catches more failure modes than any one alone.

---

## 4. Kelly Fraction (f*)

**The question it answers:** "What fraction of your capital should you bet on this
strategy, assuming you want to maximize long-run growth without going broke?"

**The idea in one sentence:** Kelly (1956) derived the mathematically optimal bet size
for repeated positive-expectancy bets; applying it to a continuous return stream gives
the maximum-growth position size.

**Formula used:**
```
f* = 0.5 × (annualized_excess_return) / (annualized_variance)
```

The 0.5 multiplier is **half-Kelly** — a standard risk-management convention because
full Kelly is very aggressive and highly sensitive to parameter estimation error.

**What the number means:**
- Kelly f* = 1.0 means: bet up to your full account (already half-Kelly capped).
- Kelly f* = 0.5 means: deploy 50% of capital into this strategy.
- Kelly f* = 0.0 means: the strategy has negative excess return — do not allocate.
- Values are clipped to [0, 1] since we do not use leverage.

**How it's used:** Kelly fractions drive the MVO (Mean-Variance Optimization)
portfolio construction step. Strategies with higher Kelly fractions receive larger
allocations in the optimizer, subject to diversification and drawdown constraints.

---

## 5. Regime-conditional risk aversion (γ scaling)

**The question it answers:** "Should my portfolio be more defensive when markets are
stressed, even if my declared risk profile hasn't changed?"

**The idea in one sentence:** Your stated risk profile (moderate, aggressive, etc.)
should determine how you invest in *normal times* — but when the regime is stressed,
the optimizer's risk aversion should rise automatically, pulling allocations toward
minimum-variance without requiring you to change your declared preference.

**The math:** The Kelly/MVO objective is `max wᵀ(μ−rf) − ½γwᵀΣw`. The parameter γ
is the risk-aversion coefficient. We map risk profiles to γ baseline values, then
multiply by a regime-conditional factor:

| Risk profile   | Baseline γ | Description |
|---|---|---|
| `fixed_income` | 12.0 | Extreme preservation; dominated by minimum-variance |
| `conservative` | 6.0  | Capital preservation first |
| `moderate`     | 3.0  | Balanced growth and protection |
| `aggressive`   | 2.0  | Growth-oriented, accepts drawdown |
| `hyper_risky`  | 1.5  | Near-full-Kelly; maximum growth intent |

These baseline γ values are then scaled by a **regime multiplier**:

| Regime       | Multiplier | Effective interpretation |
|---|---|---|
| `risk_on`    | 1.0×       | Declared profile is appropriate; full allocation budget |
| `transition` | 1.0×       | Uncertain regime; do not overreact |
| `risk_off`   | 2.0×       | Double effective γ ≈ halve effective Kelly fraction |
| `crisis`     | 4.0×       | Quadruple effective γ ≈ minimum-variance leaning |

So a `moderate` investor in a `risk_off` regime has effective γ = 3.0 × 2.0 = 6.0 —
the same as a `conservative` investor in a normal regime. In a `crisis` regime that
same moderate investor operates at γ = 12.0 — equivalent to `fixed_income`.

**The research grounding:** Ang & Bekaert (2002, "International Asset Allocation With
Regime Shifts", *Review of Financial Studies*) demonstrated that regime-conditioned
portfolio weights strictly dominate static weights across a range of γ specifications.
Their two-state Markov-switching model produces exactly this pattern: risk aversion
should be higher in the bear/crisis state than the bull/calm state, and the magnitude
of the increase is large enough to matter for allocation decisions.

**Calibration note:** The multipliers here (1×/2×/4×) are deliberately conservative
relative to the research-paper values (which sometimes go 6–10× in tail regimes) to
avoid producing wildly different allocations every time the regime detector flips.
This is an engineering judgment, not a claim of optimality — it reflects the
hackathon-stage calibration status of the regime detector.

**What the number means in the UI:** The Portfolio Advisor's allocation table shows
the effective γ applied to each recommendation. When the regime is `risk_off` or
`crisis`, the advisor notes the multiplier applied and which paper it cites.

---

## The four-primitive admission gate

A strategy is promoted to `validated` only if all four conditions hold simultaneously:

| Condition | Threshold |
|---|---|
| DSR p-value | ≥ 0.95 |
| PBO | < 0.50 |
| OOS Sharpe / IS Sharpe | ≥ 0.50 |
| Total trades in backtest | ≥ 10 (avoid sparse-trade illusions) |

The UI displays each number openly for every strategy — including candidates that
have not yet passed. If a strategy fails a gate, you can see exactly which one and
why. This is the design: rigor as transparency, not as a hidden score.

---

## References

- Ang, A., Bekaert, G. (2002). "International Asset Allocation With Regime Shifts."
  *Review of Financial Studies*, 15(4), 1137–1187. *(Regime-conditional γ scaling — §5)*
- Bailey, D.H., Borwein, J., López de Prado, M., Zhu, Q.J. (2014). "Pseudo-Mathematics
  and Financial Charlatanism: The Effects of Backtest Overfitting on Out-of-Sample
  Performance." *Notices of the AMS*, 61(5), 458–471.
- Bailey, D.H., López de Prado, M. (2014). "The Deflated Sharpe Ratio: Correcting for
  Selection Bias, Backtest Overfitting and Non-Normality." *Journal of Portfolio
  Management*, 40(5), 94–107.
- Kelly, J.L. (1956). "A New Interpretation of Information Rate." *Bell System Technical
  Journal*, 35(4), 917–926.
- McLean, R.D., Pontiff, J. (2016). "Does Academic Research Destroy Stock Return
  Predictability?" *Journal of Finance*, 71(1), 5–32.
- Sharpe, W.F. (1994). "The Sharpe Ratio." *Journal of Portfolio Management*, 21(1),
  49–58. *(Defines the ratio on excess returns over the risk-free rate — the basis for
  the historical T-bill series in §1a.)*
