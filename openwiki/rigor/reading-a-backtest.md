---
type: field-guide
title: Reading a backtest adversarially
description: The six red flags and five green lights for judging a backtest, each mapped to the detector that catches it, plus the six-step order in which to scan a strategy passport.
tags: [backtest, red-flags, overfitting, passport, diagnostics]
verified:
  - by: openwiki/0.4.3
    at: 2026-08-31T05:55:43.566Z
sources:
  - id: openwiki-source-bcc51b9a099705e0822aa4c7
    resource: repo://docs/quant/backtest-interpretation.md
generated: { by: "claude-code", at: "2026-08-31T05:55:43.566Z" }
---

# Reading a backtest adversarially

> **STALE ON ONE NUMBER — regenerate.** This page was generated 2026-08-31 from the quant
> slice as it then stood. On 2026-09-03, [#1794](https://github.com/aprin-labs/archimedes/issues/1794) retired
> the lower DSR bar: the badge bar is **0.95**, defined once as `DSR_P_BADGE_MIN` in
> `backend/archimedes/services/rigor_profiles.py`. Every `0.90` and every "90% one-sided
> confidence" below is the retired bar — step 2 of the one-screen read included. The body
> is otherwise left as generated rather than hand-patched, so the `generated:` provenance
> above stays true; the one exception is the closing note, which asserted the *inverse* of
> the truth (it called 0.95 "retired") and was corrected in place rather than left to
> mislead. The next openwiki run supersedes this page.

A backtest is **a claim about the future stated in the language of the past**. The
operative skill is reading one adversarially — assuming it is overfit until it proves
otherwise.

When you see a Sharpe of 2.0 on ten years of history, the correct first question is *not*
"how much money would that have made?" It is: **how many strategies were tried before this
one was kept, and would it survive on data it has never seen?** A backtest earns trust by
surviving attempts to break it.

---

## Six red flags

### 1. The IS/OOS cliff

**The tell.** Great in-sample, falls apart out-of-sample. A Sharpe of 1.8 collapsing to 0.3
means the edge was a property of the training data, not of the market.

**What catches it.** The held-out final-30% Sharpe, with the cliff check `OOS/IS ≥ 0.5`
enforced against an in-sample Sharpe measured on the *first 70% only*, so the ratio cannot
be gamed by blending the slices. The gate renders the exact ratio — `PASS (OOS/IS=0.71)` or
`FAIL (OOS/IS=0.22, need ≥ 0.50)`. The deeper detector is CPCV, which requires the edge to
hold across a *majority* of held-out paths rather than one, and reports `MISSING` until a
real combinatorial matrix exists.

### 2. Parameter sensitivity

**The tell.** Move the lookback from 200 to 190 or 210 and the Sharpe halves. A genuine
signal is a **plateau** in parameter space; an overfit one is a needle.

**What catches it.** At library level, PBO across 12,870 symmetric splits asks how often the
in-sample winner survives out-of-sample. At strategy level, DSR widens the Sharpe's standard
error for non-normality and autocorrelation and — *where a candidate pool exists* —
additionally deflates by the expected best-of-N. On the curated library `num_trials = 1`:
no deflation, no multiple-testing correction.

### 3. The unrealistically smooth curve

**The tell.** A near-straight climb with no drawdowns is a warning, not a triumph. Either
costs and slippage were ignored, or the strategy is trading on information it could not have
had. Real strategies have jagged curves and real drawdowns.

**What catches it.** The look-ahead audit flags forward-data access — negative pandas shifts,
positive feed indexing, forecast-named calls. The analytics engine separately rejects
close-on-close and close-on-open broker configurations that leak a bar's own close into its
own decision. And the closed-form expected one-year maximum drawdown gives a reference: a
curve with materially *smaller* realised drawdowns than its own μ/σ implies is suspicious.

### 4. Concentration risk

**The tell.** The whole return comes from one asset, one position, or one lucky year. A
"diversified" portfolio that is diversified in name only.

**What catches it.** The Euler risk decomposition surfaces each asset's marginal
contribution to portfolio variance — "NVDA contributes 61% of portfolio variance". The
optimizer defends structurally with per-asset weight caps, and pairwise-correlation
reporting exposes apparent diversification across correlated names.

### 5. Regime-selection turnover

**The tell.** The strategy "works" because it happened to be long through one bull regime,
or it flips so often that the backtest is really a bet on one historical sequence of
regimes.

**What catches it.** Regime tagging labels a strategy that only earns its Sharpe in one
regime, and the regime-conditional gamma multiplier is the sizing defence that stops such a
strategy from being sized as if its regime will persist.

### 6. Correlation clustering

**The tell.** Five "different" strategies that are 0.9-correlated are one strategy with five
names — and both the apparent diversification and the apparent multiple-testing breadth are
illusions.

**What catches it.** Average pairwise correlation feeds the DSR's effective-N correction, so
highly correlated trials are correctly counted as *fewer independent tests*; correlation-pair
reporting lists the worst offenders; and covariance shrinkage keeps a near-singular
correlation matrix from blowing up the optimizer.

---

## Five green lights

1. **Consistent rolling Sharpe.** The edge is earned repeatedly, not once. CPCV's positive
   fraction measures exactly this, and the Lo (2002) confidence band lets "consistent" be
   read against the estimate's actual error bars.
2. **Low parameter sensitivity.** The formal version of a plateau: a low PBO means the
   strategy keeps winning across many different IS/OOS partitions, so it is not sensitive to
   which slice of history it was tuned on.
3. **Realistic transaction costs.** The Sharpe is reported *after* commissions and slippage
   and still clears the bar. Costs hit high-turnover strategies hardest, so surviving them
   indicates a capturable edge rather than a paper one.
4. **Documented economic intuition.** There is a reason the edge should exist — a behavioural
   bias, a risk premium, a structural friction — stated *before* the backtest rather than
   reverse-engineered after. A strong backtest with no economic story is more likely a
   data-mining artefact than a weak backtest with a sound thesis.
5. **Peer-reviewed backing, held to its actual strength.** A *Journal of Finance* paper is a
   stronger anchor than a practitioner book, and where the academic anchor is a *related*
   paper rather than the strategy's literal origin, the file says so. McLean & Pontiff (2016)
   is the sobering backdrop: published predictors lost roughly 26% of their return
   out-of-sample and about 58% post-publication — which is why peer review is **necessary but
   not sufficient**.

---

## The one-screen read

Scan a passport in this order:

1. **Gate details** — are all four `PASS`? A `FAIL` tells you which failure mode tripped; a
   `MISSING` tells you a check could not be computed.
2. **DSR p-value** — is it ≥ 0.90? If not, the excess Sharpe is not positive at 90%
   one-sided confidence under robust standard errors, and the headline Sharpe is unproven.
   Note what this does *not* say on the curated path: `num_trials = 1` there, so no
   multiple-testing deflation was applied.
3. **PBO** — is it < 0.5? If not, the *library's selection* is overfit and this strategy's
   apparent edge is suspect by association.
4. **OOS/IS ratio** — is it ≥ 0.5, and is the OOS Sharpe positive? The economic survival
   test.
5. **Paper-claim delta** — how far below the paper's claim is the measured performance, and
   is the divergence explained (price proxy, single-name adaptation, cost-aware)?
6. **Risk decomposition** — is the variance concentrated in one name? Is the library secretly
   one correlated bet?

A backtest surviving all six is **not guaranteed to work — nothing is**. It has survived the
attempts that catch the vast majority of false positives, and stating exactly that, rather
than more, is the point of the protocol.

---

> **Before quoting a threshold from this page**, check `rigor_profiles.DSR_P_BADGE_MIN` —
> it is the one definition of the DSR bar. The 0.90 bar in step 2 is the RETIRED one; the
> live bar is 0.95, and the disagreement across the slice that
> [`documented-conflicts`](documented-conflicts.md) recorded was resolved by #1794 in favour
> of 0.95.
