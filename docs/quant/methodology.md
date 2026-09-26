# Quantitative Methodology

> **Status:** Living reference for the math layer. Written 2026-06-12.
> **Author:** Önder Akkaya (quant / math lane).
> **Audience:** Teammates, judges, and any technically-literate reader who wants
> the *why* behind the numbers on a strategy passport.
> **Scope:** This document explains the statistics and the portfolio math that
> Archimedes runs. It is the conceptual companion to two operational specs that
> remain canonical where they overlap:
> - [`../specs/selection-bias-corrections-spec.md`](../specs/selection-bias-corrections-spec.md)
>   — the frozen DSR / PBO / walk-forward / look-ahead contract. **Thresholds and
>   formulas in this doc are consistent with that spec; if they ever drift, the
>   spec wins.**
> - [`../rigor-methods.md`](../rigor-methods.md) — the plain-English, judge-facing
>   version of the four-gate admission story.
>
> The implementing code is [`../../backend/archimedes/services/rigor_evaluator.py`](../../backend/archimedes/services/rigor_evaluator.py)
> (selection-bias controls + Kelly) and
> [`../../backend/archimedes/services/portfolio_optimizer.py`](../../backend/archimedes/services/portfolio_optimizer.py)
> (portfolio construction). Function names cited below are real and resolve in
> those files.

---

## 0. The thesis in one paragraph

A high backtest Sharpe ratio is the *easiest thing in finance to manufacture by
accident*. Search enough parameter combinations, or pick the best of enough
candidate strategies, and you will find something that looks brilliant on
history and is worthless out-of-sample. Archimedes' wedge is that it refuses to
report a raw Sharpe as if it were evidence. Every Tier-1 strategy is run through
statistical controls that hold the observed performance to a standard error robust to
non-normality and autocorrelation, and — **where a search of ours actually happened** —
deflate it by exactly the amount of luck that search injected. On the curated library
`num_trials = 1`, so there is no deflation and no multiple-testing correction; that caveat
is published on the Architecture page rather than buried. The resulting numbers — plus the
delta against what the source paper claimed — are surfaced openly, never hidden behind an
aggregate score. Note the distinction the product holds to throughout: the board-level
selection bias a user incurs choosing best-of-N is **disclosed, not corrected**. This is "rigor as the wedge": the curation protocol is
the product.

---

## Part I — Selection-bias corrections

The four-primitive admission gate is: **Deflated Sharpe Ratio**, **Probability of
Backtest Overfitting**, **walk-forward out-of-sample Sharpe**, and a **look-ahead
static audit**. A strategy is promoted `CANDIDATE → VALIDATED` only when all four
pass simultaneously (see [`admission-criteria.md`](admission-criteria.md)).

### 1. Deflated Sharpe Ratio (DSR)

**Reference:** Bailey, D. H., & López de Prado, M. (2014). *The Deflated Sharpe
Ratio: Correcting for Selection Bias, Backtest Overfitting and Non-Normality.*
Journal of Portfolio Management 40(5), 94–107.

**Implemented by:** `compute_dsr(daily_returns, num_trials, average_correlation)`
in `rigor_evaluator.py`, with the core formula factored into
`_dsr_from_stats(...)` for direct unit-testing against the spec's reference cases.

#### What it corrects

Two distinct biases in the ordinary Sharpe ratio:

1. **Multiple-testing inflation.** If you evaluate `N` candidate strategies and
   keep the best one, the maximum observed Sharpe is upward-biased *even when
   every strategy is pure noise*. The expected maximum of `N` independent
   standard-normal Sharpe estimates grows with `N`; reporting the winner's raw
   Sharpe without subtracting that expected-maximum baseline is the single most
   common way honest-looking backtests lie.
2. **Non-normality.** The textbook Sharpe significance test assumes i.i.d. normal
   returns. Real return series are skewed and fat-tailed; negative skew and excess
   kurtosis both *inflate* the apparent significance of a positive Sharpe. DSR
   widens the variance of the Sharpe estimator to account for both.

#### The formula and its intuition

Work in **per-bar** (un-annualized) Sharpe units; annualization is purely a
display transform. Given `T` per-bar returns, the per-bar Sharpe `ŜR`, skewness
`γ₃`, and **raw (Pearson) kurtosis** `γ₄` (γ₄ = 3 for a normal distribution —
*not* Fisher excess kurtosis), and `N` trials in the selection set:

```
γ_E   = 0.5772156649                      # Euler–Mascheroni constant
Φ⁻¹   = standard-normal inverse CDF        # scipy.stats.norm.ppf
Φ     = standard-normal CDF                # scipy.stats.norm.cdf

# Expected maximum of N i.i.d. standard-normal Sharpe estimates
# (Bailey–López de Prado two-quantile approximation):
E[max_N] = (1 − γ_E)·Φ⁻¹(1 − 1/N) + γ_E·Φ⁻¹(1 − 1/(N·e))

# Per-bar SR has variance 1/(T−1) under the i.i.d.-normal null,
# so the expected best-of-N null Sharpe scales by √(1/(T−1)):
SR_zero  = √(1/(T−1)) · E[max_N]

# Variance-corrected z-statistic (Bailey–LdP eq. 8):
z   = (ŜR − SR_zero)·√(T−1) / √(1 − γ₃·ŜR + ((γ₄ − 1)/4)·ŜR²)
DSR = Φ(z)
```

Read it left to right:

- `SR_zero` is **the Sharpe you would expect to see by luck alone** after picking
  the best of `N` trials. The more strategies you searched (`N` ↑), the higher
  this bar climbs.
- `ŜR − SR_zero` is the strategy's *excess over the luck baseline*. If your
  observed Sharpe barely beats what `N`-way search produces from noise, this is
  near zero and the strategy will not clear the gate.
- The denominator `√(1 − γ₃·ŜR + ((γ₄−1)/4)·ŜR²)` is the non-normality
  correction. Negative skew (`γ₃ < 0`) and fat tails (`γ₄ > 3`) both *enlarge* it,
  shrinking `z` and lowering the DSR — exactly the behavior we want, because
  fat-tailed strategies hide crash risk.
- `DSR = Φ(z)` is a **probability in `[0, 1]`** — the model's confidence that the
  true Sharpe is positive after both corrections. This is what the UI shows as the
  DSR p-value.

> **Convention warning (load-bearing).** The `(γ₄ − 1)/4` coefficient is derived
> for the *raw-kurtosis* convention. The code calls
> `scipy.stats.kurtosis(arr, fisher=False)` deliberately. Passing Fisher *excess*
> kurtosis would bias the denominator by a constant `(3/4)·ŜR²` and skew every
> DSR. This is documented inline in `compute_dsr`.

#### When it applies and sample-size requirements

- DSR needs at least **`T ≥ 4` bars** to form skew/kurtosis; `compute_dsr` returns
  `(None, None)` below that, on a degenerate constant series, or on a non-positive
  denominator. Statistical power, however, requires far more than the floor — a
  borderline strategy needs years of daily data before `z` separates from the
  null. The spec's reference cases use `T` of 504 / 1260 / 2520 bars (2–10 years).
- `num_trials = 1` applies **no correction** (`E[max_N] = 0`): there was no
  selection, so there is nothing to deflate. The orchestrator passes
  `N = len(strategy_library)` so the correction is meaningful, and a warning is
  logged when `N = 1` while the library holds more than one strategy.

#### Correlated trials shrink `E[max]`, they do not shrink `N`

A parameter sweep whose variants move together does **not** constitute `N`
*independent* tests. `compute_dsr` accepts an `average_correlation` argument and
shrinks the expected best-of-N null directly. For `N` standard normals with
equicorrelation `ρ̄ ≥ 0`, the one-factor representation
`Xᵢ = √ρ̄·Z + √(1−ρ̄)·εᵢ` gives each `Xᵢ` unit variance and pairwise correlation
`ρ̄`, and the term `Z` common to all of them factors straight out of the maximum:

```
max_i Xᵢ    = √ρ̄·Z + √(1−ρ̄)·max_i εᵢ
E[max_i Xᵢ] = √(1−ρ̄) · E[max of N iid]
```

So the correlated `E[max]` is exactly the independent one scaled by `√(1−ρ̄)`.
This is Bailey–López de Prado's own `√(V[{SRₙ}])` term specialised to
equicorrelation. At `ρ̄ = 0` the full multiple-testing penalty applies; at
`ρ̄ = 1` all variants are one test and there is nothing to deflate. The helper
`compute_average_pairwise_correlation(...)` computes `ρ̄` from the library's
return matrix and clamps negative (diversifying) correlations to `0.0` — a
conservative "no penalty relief" default.

> **Superseded (#1558 / #1559, 2026-08-31).** This section previously documented
> an effective-trial-count form `N_eff = N / (1 + (N − 1)·ρ̄)`, attributed to
> Cheverud 2001 / Nyholt 2004, evaluated at `max(2, N_eff)` with a linear taper
> across `N_eff ∈ [1, 2]`. That is the Kish design effect — an effective sample
> size for a *mean*, not an expectation of a *maximum*. It under-deflated by
> 2×–10× across `ρ̄ ∈ [0.3, 0.9]`, and it was wrong in shape as well as scale:
> `N_eff → 1/ρ̄` as `N` grows, so the penalty saturated and a 10,000-variant
> sweep drew the same deflation as a 64-variant one. The formula, the citation,
> and the taper are all gone from the code; none of them should be reintroduced.

#### Outputs

`compute_dsr` returns `(deflated_sharpe_ratio, dsr_p_value)`:
- `deflated_sharpe_ratio` — `(ŜR − SR_zero)·√252`, the *annualized* corrected
  Sharpe in Sharpe units (positive ⇒ clears the multiple-testing bar).
- `dsr_p_value` — `Φ(z) ∈ [0, 1]`, the gate quantity. **Gate threshold at the strictest
  level (1, Conservative — the badge bar): ≥ 0.95**, loosening down the strictness ladder
  to `DSR_P_FLOOR` at level 5. The live values are the `dsr_p_min` fields of
  `_PROFILES` in [`rigor_profiles.py`](../../backend/archimedes/services/rigor_profiles.py);
  `RigorGateResult.passes_all` reads the selected profile, never a literal, and the level-1
  row *is* `DSR_P_BADGE_MIN` rather than a copy of its value.
  **Corrected 2026-09-03 (#1794):** between PR #901 and #1794 this line quoted a lower bar
  than the Generate pipeline and the public rigor pages did, so the product graded against
  two bars at once. There is now one. Say "deflated-Sharpe evidence at the 95% one-sided
  level" — real evidence on a short return history, not "statistically proven".

A companion `compute_sharpe_ci(...)` returns the Lo (2002) confidence interval for
an annualized Sharpe under i.i.d. daily returns — useful context for how wide the
Sharpe estimate's error bars actually are.

---

### 2. Probability of Backtest Overfitting (PBO) via CSCV

**Reference:** Bailey, D. H., Borwein, J., López de Prado, M., & Zhu, J. (2014).
*The Probability of Backtest Overfitting.* SSRN 2326253. See also Bailey et al.
(2014), *Pseudo-Mathematics and Financial Charlatanism*, Notices of the AMS
61(5), 458–471, for the accessible exposition.

**Implemented by:** `compute_pbo(returns_matrix, s_partitions=16)` in
`rigor_evaluator.py`.

#### The question PBO answers

> "If we had picked this strategy on a *different* slice of history, would it still
> beat its peers on the remainder?"

DSR corrects the *winner's own* Sharpe. PBO instead asks whether the **selection
procedure** — picking the in-sample best from a library — is itself overfit. It is
a property of the whole library, not of one strategy.

#### CSCV — Combinatorially Symmetric Cross-Validation

1. Stack the `N` strategies' aligned daily returns into a `(T, N)` matrix.
2. Partition the `T` rows into `S` equal time-blocks (`S = 16` is the paper's
   recommended default; must be even).
3. Enumerate **every** way to choose `S/2` blocks as in-sample (IS); the
   complementary `S/2` blocks are out-of-sample (OOS). That is `C(16, 8) = 12,870`
   distinct symmetric splits.
4. For each split: rank the `N` strategies by Sharpe in-sample, take the
   **in-sample winner**, and look up *its* rank out-of-sample.
5. Convert the winner's OOS rank to a relative rank `ω = rank_OOS / N` and form the
   logit `λ = log(ω / (1 − ω))`.
6. **`PBO = P(λ ≤ 0)`** — the fraction of splits in which the in-sample winner
   landed in the *bottom half* out-of-sample.

#### Interpretation

- `PBO = 0` — the IS winner also won OOS in every split: no overfitting detected.
- `PBO = 0.5` — a coin flip: the IS-best strategy is no better than the median OOS
  strategy. **This is the failure threshold.**
- `PBO ≥ 0.5` means the selection procedure systematically picks strategies that
  underperform the OOS median — the library is overfit. **Gate threshold: PBO < 0.5.**

#### Honest caveats baked into the implementation

`compute_pbo`'s docstring documents three real limitations that matter for a small
library (today `N ≈ 4–6`):

- **Library-level coupling.** PBO is a property of the *selection set*; the same
  value is attached to every strategy in the run. A strategy's PBO verdict
  therefore shifts when you add or remove a *neighbor*. This is inherent to CSCV,
  not a bug — read PBO as a *library-overfit signal*, not a per-strategy score.
- **Coarse OOS rank.** With small `N`, `ω = rank_OOS / N` takes only a few discrete
  values, so `λ` and hence PBO are granular. The estimate sharpens as the library
  grows.
- **Trailing-bar truncation.** `rows_per_block = T // S` drops up to `S − 1`
  trailing bars so every block is equal-length — negligible for multi-year series.

PBO is a **library-level** metric: compute it once per analytics-engine run and
attach the same score to each strategy's `BacktestResult` from that run; recompute
when the library changes.

---

### 3. Walk-forward out-of-sample Sharpe and the IS/OOS cliff

**Implemented by:** `compute_oos_sharpe(daily_returns, train_fraction=0.70)` in
`rigor_evaluator.py`.

DSR and PBO are *statistical* tests. The walk-forward OOS Sharpe is an *economic*
one: split the return series chronologically (no shuffling), reserve the first
70% as in-sample, and compute the Sharpe on the held-out final 30% alone. The gate
applies two checks:

- **Absolute floor:** OOS Sharpe must be `> 0`. A negative OOS Sharpe can never
  pass, regardless of how strong the in-sample Sharpe was.
- **The cliff check:** `OOS_Sharpe / IS_Sharpe ≥ 0.5`. The out-of-sample edge must
  be at least half the in-sample edge. A strategy whose Sharpe *cliffs* the moment
  it stops seeing training data has memorized the past, not discovered a signal.

The in-sample Sharpe used for the ratio is computed on the **first 70% slice
only** (`run_rigor_gate` derives this when not supplied) — blending IS+OOS into the
denominator would make the ratio trivially easy to pass.

#### Honest limitation and the principled upgrade

`compute_oos_sharpe` is a **single chronological hold-out**, not a rolling
walk-forward re-estimation. There is no per-window refit and no purge/embargo gap
at the train/test boundary, so a lookback indicator's state (e.g. an SMA-200 or
TSMOM-252 window) can straddle the split. The principled upgrade is **Combinatorial
Purged Cross-Validation** (López de Prado, *Advances in Financial Machine
Learning*, ch. 12), implemented as `compute_cpcv_oos_sharpe(...)`. CPCV assembles
`C(N−1, k−1)` continuous backtest paths from `C(N, k)` purged splits and measures
path-to-path stability (mean OOS Sharpe + fraction of paths with positive OOS
Sharpe). Crucially, **CPCV is mathematically invalid on a single static 1-D return
series** (it would generate identical paths), so the function returns `None` unless
the analytics-engine supplies a real 2-D combinatorial OOS matrix — and the gate
reports the CPCV check as `MISSING` rather than silently passing. When present, the
gate additionally requires `cpcv_positive_fraction ≥ 0.5`.

---

### 4. Look-ahead static audit

**Implemented by:** `look_ahead_audit(strategy_code)` in `rigor_evaluator.py`.

The cheapest way to fake alpha is to let the strategy peek at data it could not
have known at decision time. The audit parses the strategy source with Python's
`ast` module and flags:

1. Calls to functions whose names suggest forecasting/peeking (`future`,
   `forecast`, `predict`, `peek`, `lookahead`, `look_ahead`).
2. Negative pandas shifts (`shift(-1)`), which pull future rows backward.
3. Positive integer indexing into a data feed (`data[+N]`), which references
   future bars.
4. Negative subscripts, flagged for manual review because `[-N]` is *safe* in
   backtrader (N bars ago) but *unsafe* in pandas (last row = future data) — the
   audit cannot resolve the calling context, so it surfaces the ambiguity rather
   than guessing.

It returns `(passed, warnings)`; `passed` is `True` only when zero warnings fire.
This complements the analytics-engine's own broker-level check that rejects
`coc` (close-on-close) / `coo` (close-on-open) configurations.

---

### 5. Multiple-testing control: FDR vs FWER

When the library grows and we want a *family-wide* significance statement across
many strategy p-values (rather than the per-strategy DSR), two control regimes
apply. They answer different questions, and choosing the wrong one is itself a
methodological error.

#### Bonferroni — Family-Wise Error Rate (FWER)

**Reference:** Bonferroni (1936); see Holm (1979) for the uniformly more powerful
step-down variant.

Controls `P(at least one false positive) ≤ α` by testing each of `m` hypotheses at
`α/m`. It is **conservative**: with `m = 100` strategies and `α = 0.05`, each must
clear `p ≤ 0.0005`. Use FWER when *any* single false admission is costly — e.g.
deploying real capital where one curve-fit strategy in the live set is unacceptable.

#### Benjamini–Hochberg — False Discovery Rate (FDR)

**Reference:** Benjamini, Y., & Hochberg, Y. (1995). *Controlling the False
Discovery Rate: A Practical and Powerful Approach to Multiple Testing.* JRSS-B
57(1), 289–300.

Controls the **expected proportion of false positives among the rejections**.
Procedure: sort the `m` p-values ascending `p₍₁₎ ≤ … ≤ p₍ₘ₎`; find the largest `k`
with `p₍ₖ₎ ≤ (k/m)·α`; reject hypotheses `1…k`. FDR is **more powerful** than
Bonferroni — it tolerates a controlled fraction of false discoveries in exchange
for finding more true ones.

#### When to prefer FDR over FWER

| Use FWER (Bonferroni) when… | Use FDR (Benjamini–Hochberg) when… |
|---|---|
| Any single false admission is expensive (live capital, one bad strategy poisons the vault) | You are *screening* a large candidate pool and a few false leads are acceptable |
| `m` is small | `m` is large (dozens+ of candidates) |
| You need a guarantee on *any* error | You can tolerate a known *expected proportion* of errors |

For Archimedes, the **per-strategy DSR is the binding gate** (it already encodes
multiple-testing via the `N`-trial deflation). FDR/FWER are the right frame for the
*library-screening* question — "of the candidates we are about to promote, what
fraction are likely false?" — and FDR is generally preferred at the screening stage
because the candidate pool is large and a missed true strategy costs more than a
controlled trickle of false leads that the DSR gate then filters individually.

---

### 6. Monte Carlo significance via the circular block bootstrap

**Reference:** Politis, D. N., & Romano, J. P. (1992). *A Circular Block-Resampling
Procedure for Stationary Data.* (And the stationary bootstrap, Politis & Romano
1994.)

A parametric significance test assumes a return distribution. When we want a
**distribution-free** significance statement for a metric (Sharpe, CAGR, max
drawdown) on serially-correlated returns, we bootstrap. The naive i.i.d. bootstrap
is wrong for financial returns because it destroys autocorrelation and volatility
clustering. The **circular block bootstrap** fixes this:

1. Wrap the return series into a circle (so the last bar's "next" is the first).
2. Resample contiguous **blocks** of length `b` (preserving short-range serial
   dependence within each block) until a synthetic series of length `T` is built.
3. Recompute the metric on each resample to build its null/empirical distribution.
4. Read significance as the resampled metric's percentile (e.g. one-sided p =
   fraction of resamples with metric ≤ 0 for a positive-Sharpe test).

Block length `b` should grow with the autocorrelation horizon (rule of thumb
`b ∝ T^{1/3}`). The circular variant gives every observation equal resampling
weight (the plain block bootstrap under-weights the tails). This is the right tool
when we report a Monte-Carlo confidence band on a backtest statistic rather than
relying on the closed-form Lo (2002) Sharpe standard error.

---

## Part II — Portfolio construction

The optimizer maps each vault **risk profile** to an objective. The five profiles
are defined in
[`../../backend/archimedes/models/portfolio.py`](../../backend/archimedes/models/portfolio.py)
(`RiskProfile`: `FIXED_INCOME`, `CONSERVATIVE`, `MODERATE`, `AGGRESSIVE`,
`HYPER_RISKY`). Construction lives in `portfolio_optimizer.py`.

### 7. Mean-Variance Optimization (Markowitz)

**Reference:** Markowitz, H. (1952). *Portfolio Selection.* Journal of Finance
7(1), 77–91.

The foundation: a portfolio is a point in (expected-return, variance) space, and
for any target return there is a *minimum-variance* weight vector. Sweeping the
target return traces the **efficient frontier** — implemented by
`compute_efficient_frontier(...)`, which solves a sequence of
`min wᵀΣw  s.t. wᵀμ = target, 1ᵀw = 1` problems via SLSQP. All objectives below
are solved on the **long-only unit simplex** with a per-asset weight cap (default
0.40, raised to 0.60 for the hyper-risky profile) to prevent degenerate
corner solutions, falling back to equal weight when SLSQP fails or history is too
short (`< 20` bars).

| Risk profile | Objective | Function |
|---|---|---|
| `CONSERVATIVE` | Global Minimum Variance | `_gmv(...)` |
| `MODERATE` / `AGGRESSIVE` | Max Sharpe (tangency) | `_max_sharpe(...)` |
| `HYPER_RISKY` | Max Expected Return (LP) | `_max_expected_return(...)` |

### 8. Global Minimum Variance (GMV)

`min wᵀΣw  s.t.  1ᵀw = 1,  0 ≤ wᵢ ≤ cap`. The single point on the frontier with the
lowest variance, ignoring expected returns entirely. **Serves the conservative
profile** because it is the most estimation-robust objective — it depends only on
the covariance matrix `Σ`, never on the notoriously noisy expected-return vector
`μ`. When you cannot trust your return forecasts, minimize variance.

### 9. Max-Sharpe (tangency portfolio)

`max (μ − rf)ᵀw / √(wᵀΣw)`. The point where a ray from the risk-free rate is
tangent to the efficient frontier — the **highest risk-adjusted return**
portfolio. **Serves the moderate and aggressive profiles** (aggressive applies a
looser USYC floor upstream). More sensitive to `μ` estimation error than GMV, which
is why we shrink the covariance (§13) and, in the Kelly path, shrink `μ` too.

### 10. Kelly criterion sizing

**References:** Kelly, J. L. (1956). *A New Interpretation of Information Rate.*
Bell System Technical Journal 35(4), 917–926. Bell, R., & Cover, T. M. (1980).
*Competitive Optimality of Logarithmic Investment.* Mathematics of Operations
Research 5(2), 161–166.

Kelly answers: *what fraction of capital maximizes long-run (log) growth?* For a
continuous return stream the single-asset full-Kelly fraction is:

```
f* = (μ_ann − rf_ann) / σ²_ann
```

implemented in `compute_kelly_fraction(daily_returns, rf_annual=0.05,
fractional=0.5)`.

- **Full Kelly** (`fractional=1.0`) maximizes growth but is *too aggressive in
  practice*: it is acutely sensitive to estimation error in `μ`, and over-betting
  it can be catastrophic. We treat full Kelly as an academic reference only.
- **Half-Kelly** (`fractional=0.5`, the default) sacrifices a small amount of
  long-run growth for a large reduction in drawdown volatility — the standard
  risk-management convention. Kelly is defined on **excess** returns; using gross
  returns inflates every allocation by `rf/σ²`. The output is clipped to `[0, 1]`
  (no leverage); a non-positive excess return returns `0.0`.

#### γ-mapped risk aversion (Kelly as mean-variance)

In the portfolio (multi-asset) path, Kelly is expressed as the
risk-aversion-parameterized mean-variance objective solved by
`kelly_optimize_from_prices(...)`:

```
maximize   wᵀ(μ − rf) − ½·γ·wᵀΣw
subject to 0 ≤ wᵢ ≤ max_weight,   Σ wᵢ ≤ synth_budget
```

`γ` is mapped from the risk profile via the `RISK_AVERSION` table. **`γ = 2`
reproduces half-Kelly** (Bell & Cover 1980); `γ → 0` approaches full Kelly;
`γ → ∞` collapses to minimum-variance:

| Profile | Baseline γ | Reading |
|---|---|---|
| `fixed_income` | 12.0 | Extreme preservation; minvar-dominated |
| `conservative` | 6.0 | Capital preservation first |
| `moderate` | 3.0 | Balanced |
| `aggressive` | 2.0 | Half-Kelly; accepts drawdown |
| `hyper_risky` | 1.5 | Near-full-Kelly |

#### Regime-conditional γ scaling

**Reference:** Ang, A., & Bekaert, G. (2002). *International Asset Allocation With
Regime Shifts.* Review of Financial Studies 15(4), 1137–1187.

When a live regime is supplied, the effective risk aversion is
`γ_eff = γ_profile × REGIME_GAMMA_MULTIPLIER[regime]`
(`risk_on` 1.0× · `transition` 1.0× · `risk_off` 2.0× · `crisis` 4.0×). Ang &
Bekaert show regime-conditioned weights strictly dominate static weights: risk
aversion *should* rise in stressed states. So a `moderate` investor in a `crisis`
regime operates at `γ = 12.0`, equivalent to `fixed_income` in normal times — more
defensive without the user changing their declared profile. The multipliers are
deliberately conservative (research papers sometimes use 6–10× in tail regimes) to
avoid whipsawing allocations on every regime flip; this is documented as an
engineering judgment, not a claim of optimality.

### 11. Hierarchical Risk Parity (HRP)

**Reference:** López de Prado, M. (2016). *Building Diversified Portfolios that
Outperform Out of Sample.* Journal of Portfolio Management 42(4), 59–69.

HRP avoids the single worst failure mode of Markowitz: **inverting an
ill-conditioned covariance matrix.** Quadratic optimizers must invert `Σ`, and when
assets are highly correlated (so `Σ` is near-singular) the inverse amplifies
estimation noise into wild, unstable corner weights. HRP never inverts `Σ`.
Instead it (1) builds a hierarchical *tree* of assets by clustering the correlation
matrix, (2) reorders `Σ` so similar assets are adjacent (**quasi-diagonalization**),
and (3) allocates capital top-down by **recursive bisection**, splitting each
cluster's budget in inverse proportion to its sub-clusters' variance. The result is
more stable out-of-sample than Markowitz and degrades gracefully as asset count
grows. **Maps to risk-balanced / diversification-seeking mandates** — the same
intent the inverse-volatility risk-parity sleeve serves (Maillard, Roncalli &
Teïletche 2010, implemented as a strategy in the library).

> *Status note:* the production optimizer today routes the five profiles through
> GMV / Max-Sharpe / Max-Return (above) with Ledoit–Wolf covariance shrinkage;
> HRP is documented here as the principled diversification objective and the
> intended path for many-asset, high-correlation baskets.

### 12. Black–Litterman

**Reference:** He, G., & Litterman, R. (1999). *The Intuition Behind
Black–Litterman Model Portfolios.* Goldman Sachs Investment Management. (Original:
Black & Litterman 1992.)

Markowitz's Achilles heel is that small changes in the `μ` estimate produce
enormous swings in weights. Black–Litterman fixes this by **starting from the
market-implied equilibrium returns** (reverse-optimized from market-cap weights via
`Π = δ·Σ·w_mkt`) as a Bayesian *prior*, then blending in the investor's explicit
**views** (with stated confidences) to form a posterior `μ`. The posterior is far
more stable than raw historical means, so the optimized weights are well-behaved
and tilt *gently* toward views rather than over-reacting. In Archimedes' frame, the
**paper-grounded expected returns** (a strategy's claimed edge) are natural
"views," and the market equilibrium is the prior — a clean mapping onto the
`mu_override` + `mu_shrinkage` blending already in `kelly_optimize_from_prices`,
which shrinks paper-extrapolated `μ` toward each asset's own sample mean (default
0.5 blend) so a single strategy's CAGR does not get promised across every asset it
voted for.

### 13. Robust optimization and covariance shrinkage

**Reference:** Ledoit, O., & Wolf, M. (2004). *A Well-Conditioned Estimator for
Large-Dimensional Covariance Matrices.* Journal of Multivariate Analysis 88(2),
365–411.

Every objective that touches `Σ` is only as good as the `Σ` estimate. The sample
covariance is the MLE but is badly conditioned — often singular — when the asset
count is not small relative to the number of observations. `ledoit_wolf_shrinkage(...)`
shrinks the sample covariance `S` toward a scaled-identity target `F = μ·I`:

```
Σ* = δ·μ·I + (1 − δ)·S,    δ* = b²/d² ∈ [0, 1]
```

where `δ*` is derived *analytically from the data* (not hand-tuned): a short, noisy
sample shrinks hard toward the target; a long, clean one barely shrinks. This is
the production estimator in `_build_mu_sigma_from_prices(...)`, with a
fixed-intensity diagonal fallback (`_shrink_cov`, α = 0.10) only when the analytic
estimator cannot run. Shrinkage is the practical face of **robust optimization**:
it makes the optimizer's output stable under the estimation error that plagues
naive MVO.

### 14. Risk attribution and reporting

Two reporting primitives make the optimizer's output legible:

- `kelly_risk_decomposition(...)` — per-asset **marginal contribution to portfolio
  variance** via the Euler decomposition `MCᵢ = wᵢ·(Σw)ᵢ / σ²ₚ` (sums to 1). Lets
  the UI say "GLD contributes 22% of portfolio variance."
- `expected_max_drawdown_1y(μ, σ)` and `value_at_risk_95_1y(μ, σ)` — closed-form
  risk figures. The drawdown uses the Magdon-Ismail & Atiya (2004) approximation
  (a real improvement on the naive `2σ` heuristic); the VaR is the parametric
  normal `−(μ − 1.645σ)`. Both return positive decimals and are explainable to a
  non-quant ("5% chance you lose at least this much in a year, assuming normal
  returns").

---

## Part III — Honest framing (non-negotiable)

Rigor that hides its own caveats is theater. Two disclosure rules are
architectural, not optional:

1. **Paper-claim deltas are surfaced, not hidden.** Every strategy passport shows
   the source paper's *claimed* Sharpe/CAGR alongside our *measured* post-gate
   numbers. When our adaptation underperforms the paper (it usually does — see the
   v1 adaptation caveats in [`strategy-library.md`](strategy-library.md)), the
   delta is shown openly. We never collapse the four gate numbers into a single
   marketing score. This is the design intent of `RigorGateResult.gate_details`,
   which renders each gate as `PASS` / `FAIL` / `MISSING` with the actual value.
2. **Price-based proxies are disclosed.** Several library strategies are *price-only
   adaptations* of strategies that originally used cross-sectional or
   fundamental data (e.g. a single-name 52-week-high proxy for a cross-sectional
   sort; an SPY-yield proxy for a true dividend-yield sort). Where the live
   implementation diverges from the paper's universe or data, the strategy file's
   header says so, the `paper_claimed_*` fields are set to `null` when the paper
   reports no mechanical Sharpe/CAGR, and the only performance claim we stand
   behind is the post-gate one measured on our own data.

**Risk-free rate (updated 2026-08-20, issue #1409):** `_RF_ANNUAL = 0.05` is now the
**fallback** convention only, not the sole rate — DSR, OOS Sharpe, and in-sample Sharpe
can instead be graded against the actual historical 3-month U.S. Treasury bill series
(FRED `DGS3MO`), per-bar aligned, when a caller threads dates through; the choice is
disclosed per result via `rf_convention`. See
[`rigor-methods.md` §1a](../rigor-methods.md#1a-the-risk-free-rate-behind-excess-issue-1409)
for the full methodology, including which live callers do and don't thread dates today.
**Not every consumer moved together** — Kelly (`compute_kelly_fraction`, `rf_annual:
float = 0.05` above) and the portfolio optimizer still take only the flat constant, so
"every excess-return figure is computed on the same basis" is no longer accurate as a
blanket claim: DSR/OOS/in-sample Sharpe can diverge from Kelly/the optimizer's basis
whenever the former are graded on the historical series.

---

## References

- Ang, A., & Bekaert, G. (2002). International Asset Allocation With Regime Shifts.
  *Review of Financial Studies* 15(4), 1137–1187.
- Bailey, D. H., Borwein, J., López de Prado, M., & Zhu, J. (2014). The Probability
  of Backtest Overfitting. *SSRN 2326253*.
- Bailey, D. H., Borwein, J., López de Prado, M., & Zhu, J. (2014).
  Pseudo-Mathematics and Financial Charlatanism. *Notices of the AMS* 61(5),
  458–471.
- Bailey, D. H., & López de Prado, M. (2014). The Deflated Sharpe Ratio. *Journal
  of Portfolio Management* 40(5), 94–107.
- Bell, R., & Cover, T. M. (1980). Competitive Optimality of Logarithmic
  Investment. *Mathematics of Operations Research* 5(2), 161–166.
- Benjamini, Y., & Hochberg, Y. (1995). Controlling the False Discovery Rate.
  *Journal of the Royal Statistical Society B* 57(1), 289–300.
- Black, F., & Litterman, R. (1992). Global Portfolio Optimization. *Financial
  Analysts Journal* 48(5), 28–43. (See He & Litterman 1999 for the intuition.)
- Cheverud, J. M. (2001). A simple correction for multiple comparisons in interval
  mapping genome scans. *Heredity* 87, 52–58.
- He, G., & Litterman, R. (1999). The Intuition Behind Black–Litterman Model
  Portfolios. *Goldman Sachs Investment Management*.
- Holm, S. (1979). A Simple Sequentially Rejective Multiple Test Procedure.
  *Scandinavian Journal of Statistics* 6(2), 65–70.
- Kelly, J. L. (1956). A New Interpretation of Information Rate. *Bell System
  Technical Journal* 35(4), 917–926.
- Ledoit, O., & Wolf, M. (2004). A Well-Conditioned Estimator for
  Large-Dimensional Covariance Matrices. *Journal of Multivariate Analysis* 88(2),
  365–411.
- Lo, A. W. (2002). The Statistics of Sharpe Ratios. *Financial Analysts Journal*
  58(4), 36–52.
- López de Prado, M. (2016). Building Diversified Portfolios that Outperform Out of
  Sample. *Journal of Portfolio Management* 42(4), 59–69.
- López de Prado, M. (2018). *Advances in Financial Machine Learning.* Wiley.
  (CPCV — ch. 12.)
- Magdon-Ismail, M., & Atiya, A. (2004). Maximum Drawdown. *Wilmott Magazine*.
- Markowitz, H. (1952). Portfolio Selection. *Journal of Finance* 7(1), 77–91.
- McLean, R. D., & Pontiff, J. (2016). Does Academic Research Destroy Stock Return
  Predictability? *Journal of Finance* 71(1), 5–32.
- Nyholt, D. R. (2004). A Simple Correction for Multiple Testing for SNPs in
  Linkage Disequilibrium. *American Journal of Human Genetics* 74(4), 765–769.
- Politis, D. N., & Romano, J. P. (1992). A Circular Block-Resampling Procedure for
  Stationary Data. In *Exploring the Limits of Bootstrap*, Wiley.
- Politis, D. N., & Romano, J. P. (1994). The Stationary Bootstrap. *Journal of the
  American Statistical Association* 89(428), 1303–1313.
