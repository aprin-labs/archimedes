# How to Read a Backtest

> **Status:** Living reference. Written 2026-06-12.
> **Author:** Önder Akkaya (quant / math lane).
> **Audience:** Anyone reading a strategy passport who wants to know whether a
> backtest is *trustworthy*, not just whether the line goes up.
> **Companions:** [`methodology.md`](methodology.md) (the statistics in depth),
> [`admission-criteria.md`](admission-criteria.md) (the gate thresholds),
> [`../rigor-methods.md`](../rigor-methods.md) (the plain-English gate story).

A backtest is a *claim about the future stated in the language of the past*. The
single most important skill in quantitative finance is reading a backtest
**adversarially** — assuming it is overfit until it proves otherwise. This doc is a
field guide: the red flags that say "do not trust this," the green lights that say
"this might be real," and, for each, the metric or tool in our codebase that
detects it. Every detector named below is a real function in
[`../../backend/archimedes/services/rigor_evaluator.py`](../../backend/archimedes/services/rigor_evaluator.py)
or [`../../backend/archimedes/services/portfolio_optimizer.py`](../../backend/archimedes/services/portfolio_optimizer.py).

---

## The mindset: a backtest is a hypothesis, not a result

When you see a Sharpe of 2.0 on ten years of history, the correct first question is
**not** "how much money would that have made?" It is: *"How many strategies were
tried before this one was kept, and would it survive on data it has never seen?"*
A backtest earns trust by surviving attempts to break it — out-of-sample tests,
multiple-testing deflation, cost realism, and a check against economic logic. The
rest of this document is those attempts, organized as red flags and green lights.

---

## Red flags — reasons to distrust a backtest

### 1. The IS/OOS Sharpe gap (the cliff)

**The tell.** The strategy looks great in-sample and falls apart out-of-sample. If
a Sharpe of 1.8 in-sample collapses to 0.3 out-of-sample, the "edge" was a property
of the training data, not the market.

**What detects it.** `compute_oos_sharpe(daily_returns, train_fraction=0.70)`
computes the held-out final-30% Sharpe, and the gate enforces the **cliff check**
`OOS_Sharpe / IS_Sharpe ≥ 0.5` (with the IS Sharpe measured on the *first 70% only*
inside `run_rigor_gate`, so the ratio cannot be gamed by blending the slices).
`RigorGateResult.gate_details["oos_sharpe"]` renders the exact ratio as
`PASS (OOS/IS=0.71)` or `FAIL (OOS/IS=0.22, need ≥ 0.50)`. A ratio near or below
zero means the edge evaporated the moment the model stopped seeing the answers.

> **Deeper detector.** The single 70/30 hold-out can still let a lookback window
> straddle the split. `compute_cpcv_oos_sharpe(...)` runs Combinatorial Purged
> Cross-Validation over many paths and the gate requires
> `cpcv_positive_fraction ≥ 0.5` — the edge must hold OOS across a *majority* of
> held-out paths, not just one tail. It is honestly reported as `MISSING` until the
> analytics-engine supplies a real combinatorial OOS matrix.

### 2. Parameter sensitivity

**The tell.** Move the lookback from 200 days to 190 or 210 and the Sharpe halves.
A genuine signal is a *plateau* in parameter space; an overfit one is a needle.
Sensitive parameters mean the backtest found a coincidence, not a structure.

**What detects it.** This is the question **Probability of Backtest Overfitting**
answers at the library level. `compute_pbo(returns_matrix)` runs CSCV across
`C(16,8) = 12,870` symmetric IS/OOS splits and asks how often the in-sample winner
survives out-of-sample. **`PBO ≥ 0.5`** means the selection procedure systematically
picks strategies that underperform the OOS median — the hallmark of fitting noise.
At the single-strategy level, the **Deflated Sharpe Ratio** (`compute_dsr`) widens the
Sharpe's standard error for non-normality and autocorrelation, and — **where a candidate
pool exists** — additionally deflates by the expected best-of-`N`, so a Sharpe that only
looks good because many variants were tried gets pushed below the `p ≥ 0.95` bar. On the
curated library `num_trials = 1`: no deflation, no multiple-testing correction.

### 3. The unrealistically smooth equity curve / no slippage

**The tell.** A curve that climbs in a near-straight line with no drawdowns is a
warning, not a triumph. Either it ignored transaction costs and slippage, or it is
trading on look-ahead information. Real strategies have *jagged* equity curves and
real drawdowns.

**What detects it.** Two checks. First, the **look-ahead static audit**
(`look_ahead_audit(strategy_code)`) parses the strategy source and flags
forward-data access — negative pandas shifts (`shift(-1)`), positive feed indexing
(`data[+N]`), and forecast-named calls — any of which can manufacture an
implausibly smooth curve. Second, the analytics-engine's broker-level check rejects
`coc`/`coo` (close-on-close / close-on-open) configurations that leak the bar's own
close into its decision. On the cost side, a strategy whose Sharpe survives only at
zero cost should be read against the realistic-cost requirement in the green-lights
section. The closed-form `expected_max_drawdown_1y(μ, σ)` gives the *expected* worst
drawdown for a given μ/σ — an equity curve with materially smaller realized
drawdowns than that is suspicious.

### 4. Concentration risk

**The tell.** The whole return comes from one asset, one position, or one lucky
year. The "diversified" portfolio is diversified in name only — a single name
drives the variance.

**What detects it.** `kelly_risk_decomposition(...)` computes each asset's
**marginal contribution to portfolio variance** via the Euler decomposition
`MCᵢ = wᵢ·(Σw)ᵢ / σ²ₚ` (sums to 1), surfacing lines like "NVDA contributes 61% of
portfolio variance." The optimizer itself defends against this with **per-asset
weight caps** (`_CAP_DEFAULT = 0.40`, `_CAP_HYPER = 0.60` in `portfolio_optimizer.py`)
so no single synth can dominate, and `correlation_pairs(...)` surfaces the
highest-magnitude pairwise correlations so apparent diversification across
correlated names is visible.

### 5. Regime-selection turnover

**The tell.** The strategy only "works" because it happened to be long through one
bull regime, or it flips positions so often that the backtest is really a bet on a
specific historical sequence of regimes. High turnover that aligns suspiciously well
with past regime boundaries is curve-fitting to history's particular path.

**What detects it.** Two surfaces. The strategy library tags each strategy with a
`REGIME_TAG` (`bull` / `bear` / `regime_neutral`) so a strategy that only earns its
Sharpe in one regime is labeled as such (see [`strategy-library.md`](strategy-library.md)).
And the **regime-conditional γ multiplier** (`REGIME_GAMMA_MULTIPLIER` in
`portfolio_optimizer.py`, grounded in Ang & Bekaert 2002) is precisely the mechanism
that prevents a single-regime strategy from being sized as if its regime will
persist: in `risk_off`/`crisis` the optimizer's effective risk aversion rises (2×/4×),
pulling toward minimum-variance rather than chasing a regime-specific edge.

### 6. Correlation clustering

**The tell.** The library's strategies all secretly bet on the same thing. Five
"different" strategies that are 0.9-correlated are one strategy with five names —
and the apparent diversification (and apparent multiple-testing breadth) is an
illusion.

**What detects it.** `compute_average_pairwise_correlation(...)` computes the mean
off-diagonal correlation across a set of return series, and it feeds the DSR's
correlated-trials correction, which scales the expected best-of-N null Sharpe by
`√(1−ρ̄)` (#1559): if the "trials" are highly correlated, the deflation correctly
prices them as fewer distinct chances to get lucky.
On the construction side, `correlation_pairs(...)` lists the top correlation pairs,
and the Ledoit–Wolf shrinkage (`ledoit_wolf_shrinkage(...)`) keeps a
near-singular correlation matrix from blowing up the optimizer.

---

## Green lights — reasons to trust a backtest

### 1. Consistent rolling Sharpe

**The tell of quality.** The Sharpe is roughly stable across rolling windows and
across regimes, not concentrated in one lucky stretch. The strategy earns its return
*repeatedly*, not once.

**What supports it.** The CPCV path stability check
(`compute_cpcv_oos_sharpe → positive_fraction`) measures exactly this: the fraction
of out-of-sample paths on which the edge is positive. A `cpcv_positive_fraction`
well above the 0.5 gate floor is the multi-path version of "consistent across
windows." `compute_sharpe_ci(...)` (Lo 2002) gives the Sharpe's confidence band, so
"consistent" can be read against the estimate's actual error bars.

### 2. Low parameter sensitivity

**The tell of quality.** Small changes to lookbacks/thresholds barely move the
result — the strategy sits on a *plateau*, not a needle. This is the direct opposite
of red-flag #2.

**What supports it.** A low PBO from `compute_pbo(...)` is the formal version: if the
strategy keeps winning across `C(16,8)` different IS/OOS partitions, it is not
sensitive to which slice of history it was tuned on. A high DSR p-value
(`compute_dsr → dsr_p_value ≥ 0.95` at the strictest level) confirms the Sharpe survives
multiple-testing deflation. **Corrected 2026-09-03 (#1794):** this doc's two DSR thresholds
disagreed with each other, and then with the code, twice. The bar is now defined once, as
`DSR_P_BADGE_MIN` in
[`rigor_profiles.py`](../../backend/archimedes/services/rigor_profiles.py), and the
level-1 profile row is that constant.

### 3. Realistic transaction costs

**The tell of quality.** The Sharpe is reported *after* commissions and slippage,
and the strategy still clears the bar. Costs hit high-turnover strategies hardest,
so a strategy that survives realistic costs is reporting a real, capturable edge —
not a paper one.

**What supports it.** The walk-forward OOS Sharpe (`compute_oos_sharpe`) and the
gate's **absolute floor** (`oos_sharpe > 0`) are computed on the *net* return series
the analytics-engine produces. The honest framing is reinforced by the
paper-claim delta: where the live, cost-aware adaptation underperforms the paper's
gross claim, the passport shows the gap rather than hiding it.

### 4. Documented economic intuition

**The tell of quality.** There is a *reason* the edge should exist — a behavioral
bias, a risk premium, a structural friction — stated before the backtest, not
reverse-engineered after. A strategy with a strong backtest and no economic story is
more likely a data-mining artifact than one with a weak backtest and a sound thesis.

**What supports it.** Every library strategy file carries a methodology docstring
that states the anomaly and its economic mechanism (momentum as
under-reaction/herding; pairs as relative-value mean reversion; volatility
management as the negative vol-risk-premium tilt). The strategy passport renders this
alongside the numbers — and where the live implementation is a *proxy* for the
paper's design, the file's header discloses the divergence (see the v1 adaptation
caveats in [`strategy-library.md`](strategy-library.md)).

### 5. Peer-reviewed backing

**The tell of quality.** The strategy traces to a published, citable paper — ideally
one whose results have themselves survived replication and post-publication scrutiny.

**What supports it.** Each strategy file records `PAPER_TITLE`, `PAPER_AUTHORS`,
`PAPER_VENUE`, `PAPER_YEAR`, and `PAPER_DOI`. We are honest about the strength of the
anchor: a *Journal of Finance* paper (e.g. Jegadeesh & Titman 1993, Moreira & Muir
2017) is a stronger backing than a practitioner book (e.g. Bollinger 2001, Connors &
Alvarez 2009), and where the academic anchor is a *related* paper rather than the
strategy's literal origin (e.g. anchoring a moving-average rule on Brock,
Lakonishok & LeBaron 1992) the file says so. McLean & Pontiff (2016) is the
sobering backdrop: published predictors lost ~26% of their return out-of-sample and
~58% post-publication, which is *why* peer-reviewed backing is necessary but not
sufficient — it must still clear the four gates on our own data.

---

## Putting it together: the one-screen read

When you open a strategy passport, scan in this order:

1. **`gate_details`** — are all four gates `PASS`? Any `FAIL` tells you *which*
   failure mode tripped; any `MISSING` tells you a check could not be computed
   (usually too little data, or CPCV without a combinatorial matrix).
2. **DSR p-value** — is it `≥ 0.95`? If not, the excess Sharpe is not positive at 95%
   one-sided confidence under robust standard errors; treat the headline Sharpe as
   unproven. Note what this does *not* say on the curated path: `num_trials = 1` there,
   so no multiple-testing deflation was applied.
3. **PBO** — is it `< 0.5`? If not, the *library's selection* is overfit, and this
   strategy's apparent edge is suspect by association.
4. **OOS/IS ratio** — is it `≥ 0.5`, and is the OOS Sharpe positive? This is the
   economic survival test.
5. **Paper-claim delta** — how far below the paper's claim is our measured
   performance, and is the divergence explained (price-proxy, single-name
   adaptation, cost-aware)?
6. **Risk decomposition** — is the variance concentrated in one name? Is the library
   secretly one correlated bet?

A backtest that survives all six reads is not *guaranteed* to work — nothing is —
but it has survived the attempts that catch the vast majority of false positives.
That is the most any honest backtest can claim, and stating exactly that is the
point of the whole protocol.
