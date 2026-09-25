---
type: method-reference
title: Selection-bias controls
description: The mathematics behind each rigor control — DSR with its raw-kurtosis convention and equicorrelated-E[max] correction, PBO via CSCV, walk-forward and CPCV, the look-ahead audit, FDR versus FWER, and the circular block bootstrap — with what is implemented separated from what is only written down.
tags: [dsr, pbo, cscv, cpcv, look-ahead, fdr, bootstrap, methodology]
verified:
  - by: openwiki/0.4.3
    at: 2026-08-31T05:55:43.566Z
sources:
  - id: openwiki-source-03a78d9aa62747d43f49c7bc
    resource: repo://docs/quant/methodology.md
generated: { by: "claude-code", at: "2026-08-31T05:55:43.566Z" }
---

# Selection-bias controls

**A high backtest Sharpe is the easiest thing in finance to manufacture by accident.**
Search enough parameter combinations, or pick the best of enough candidates, and you will
find something that looks brilliant on history and is worthless out-of-sample. These four
controls are the refusal to report a raw Sharpe as if it were evidence.

[`admission-gate`](admission-gate.md) has the thresholds and the promotion flow. This page
has the mechanisms.

---

## 1. Deflated Sharpe Ratio

**Bailey & López de Prado (2014).** DSR corrects two distinct biases at once:

1. **Multiple-testing inflation.** Evaluate `N` candidates and keep the best, and the
   maximum observed Sharpe is upward-biased **even when every candidate is pure noise**.
   Reporting the winner's raw Sharpe without subtracting that expected-maximum baseline is
   the single most common way honest-looking backtests lie.
2. **Non-normality.** The textbook Sharpe test assumes i.i.d. normal returns. Real returns
   are skewed and fat-tailed, and both negative skew and excess kurtosis *inflate* the
   apparent significance of a positive Sharpe.

### The formula

Working in per-bar Sharpe units — annualisation is purely a display transform — with `T`
returns, per-bar Sharpe `ŜR`, skewness `γ₃`, raw kurtosis `γ₄`, and `N` trials:

```
E[max_N] = (1 − γ_E)·Φ⁻¹(1 − 1/N) + γ_E·Φ⁻¹(1 − 1/(N·e))
SR_zero  = √(1/(T−1)) · E[max_N]
z        = (ŜR − SR_zero)·√(T−1) / √(1 − γ₃·ŜR + ((γ₄ − 1)/4)·ŜR²)
DSR      = Φ(z)
```

Read left to right: `SR_zero` is **the Sharpe you would expect by luck alone** after picking
the best of `N` trials, and it climbs as `N` grows. `ŜR − SR_zero` is the excess over that
luck baseline. The denominator is the non-normality correction — negative skew and fat tails
both enlarge it, shrinking `z` and lowering DSR, which is the wanted behaviour because
fat-tailed strategies hide crash risk. `DSR = Φ(z)` is a probability in `[0, 1]`.

> **The kurtosis convention is load-bearing.** The `(γ₄ − 1)/4` coefficient is derived for
> **raw (Pearson) kurtosis**, where a normal distribution gives γ₄ = 3 — *not* Fisher excess
> kurtosis. Passing excess kurtosis instead would bias the denominator by a constant
> `(3/4)·ŜR²` and skew every DSR produced.

### Sample size and the `N = 1` case

DSR needs at least **`T ≥ 4` bars** to form skew and kurtosis; below that, on a degenerate
constant series, or on a non-positive denominator it returns nothing. But **the floor is not
the power requirement**: a borderline strategy needs years of daily data before `z` separates
from the null, and the spec's reference cases use 504 / 1260 / 2520 bars.

At `num_trials = 1`, `E[max_N] = 0` and **no correction is applied** — there was no
selection, so there is nothing to deflate.

### Correlated trials shrink `E[max]`, not `N`

A parameter sweep whose variants move together is not `N` *independent* tests. Under an
equicorrelation model the factor common to every trial passes straight through the
maximum, so the expected best-of-N null is the independent-trial value scaled by
`√(1−ρ̄)`:

```
E[max of N equicorrelated] = √(1−ρ̄) · E[max of N iid]
```

At `ρ̄ = 0` the full multiple-testing penalty applies; at `ρ̄ = 1` all variants collapse to a
single test and no deflation occurs. Negative (diversifying) correlations are **clamped to
0.0** — a conservative "no penalty relief" default. Unlike an effective-trial-count form,
this keeps growing with `N`: a 10,000-variant sweep is charged more than a 64-variant one.

> This section described an effective-trial-count form `N_eff = N / (1 + (N − 1)·ρ̄)` with a
> `max(2, N_eff)` taper when it was generated on 2026-08-31 from
> `docs/quant/methodology.md`, which was itself stale. See #1558/#1559 — that form was the
> Kish design effect, not an expectation of a maximum, and it under-deflated by 2×–10×.

### What it returns

Two values: an *annualised* corrected Sharpe (positive means the multiple-testing bar is
cleared), and the p-value that is the actual gate quantity. A companion routine returns the
Lo (2002) confidence interval for an annualised Sharpe, which is useful context for how wide
the estimate's error bars really are.

---

## 2. Probability of Backtest Overfitting

**Bailey, Borwein, López de Prado & Zhu (2014).** The question is different from DSR's:

> "If we had picked this strategy on a *different* slice of history, would it still beat its
> peers on the remainder?"

DSR corrects the winner's own Sharpe. **PBO instead asks whether the selection procedure is
itself overfit** — a property of the whole library, not of one strategy.

### CSCV

1. Stack the `N` strategies' aligned daily returns into a `(T, N)` matrix.
2. Partition `T` rows into `S` equal time blocks (`S = 16` is the paper's default; must be
   even).
3. Enumerate **every** way to choose `S/2` blocks as in-sample — `C(16, 8) = 12,870`
   symmetric splits.
4. Per split: rank strategies by in-sample Sharpe, take the in-sample winner, look up *its*
   out-of-sample rank.
5. Convert to a relative rank `ω` and form the logit `λ = log(ω / (1 − ω))`.
6. **`PBO = P(λ ≤ 0)`** — the fraction of splits where the in-sample winner landed in the
   bottom half out-of-sample.

`PBO = 0` means the in-sample winner also won out-of-sample every time. `PBO = 0.5` is a coin
flip and is the failure threshold.

### Three limitations documented in the implementation

- **Library-level coupling.** The same value attaches to every strategy in a run, so a
  strategy's PBO verdict shifts when a *neighbour* is added or removed. Inherent to CSCV, not
  a bug — read PBO as a library-overfit signal, never a per-strategy score.
- **Coarse out-of-sample rank.** With small `N` the relative rank takes few discrete values,
  so PBO is granular. It sharpens as the library grows.
- **Trailing-bar truncation.** Equal-length blocks mean up to `S − 1` trailing bars are
  dropped — negligible on multi-year series.

---

## 3. Walk-forward out-of-sample and CPCV

DSR and PBO are *statistical* tests. The walk-forward Sharpe is an **economic** one: split
the series chronologically with no shuffling, train on the first 70%, and compute the Sharpe
on the held-out final 30% alone. Two checks apply — an absolute floor, and a cliff check
requiring the out-of-sample edge to retain at least half the in-sample edge, with the
in-sample Sharpe taken from the first-70% slice only so the denominator cannot be inflated.

**The honest limitation.** This is a *single chronological hold-out*, not a rolling
walk-forward re-estimation. There is no per-window refit and **no purge or embargo gap at the
boundary**, so a lookback indicator's state — an SMA-200 or a 252-day momentum window — can
straddle the split.

**The principled upgrade** is Combinatorial Purged Cross-Validation, which assembles many
continuous backtest paths from purged splits and measures path-to-path stability. Crucially,
**CPCV is mathematically invalid on a single static one-dimensional return series** — it
would generate identical paths — so it returns nothing unless a real two-dimensional
combinatorial matrix is supplied, and the gate reports `MISSING` rather than silently
passing.

---

## 4. Look-ahead static audit

The cheapest way to fake alpha is to let the strategy peek at data it could not have known at
decision time. The audit parses the strategy source with Python's `ast` module and flags four
things:

1. Calls to functions whose names suggest forecasting or peeking.
2. **Negative pandas shifts** (`shift(-1)`), which pull future rows backward.
3. **Positive integer indexing into a data feed** (`data[+N]`), referencing future bars.
4. **Negative subscripts**, flagged *for manual review* rather than failed outright — `[-N]`
   is safe in backtrader (N bars ago) but unsafe in pandas (last row = future data), and the
   audit cannot resolve the calling context, so it surfaces the ambiguity instead of guessing.

It passes only on zero warnings. This complements a separate broker-level check that rejects
close-on-close and close-on-open configurations.

---

## 5. FDR versus FWER — the frame, not the gate

Two regimes control multiple testing family-wide, and they answer different questions.

| Use FWER (Bonferroni) when… | Use FDR (Benjamini–Hochberg) when… |
|---|---|
| Any single false admission is expensive (live capital) | You are *screening* a large candidate pool and a few false leads are acceptable |
| `m` is small | `m` is large |
| You need a guarantee on *any* error | You can tolerate a known expected *proportion* of errors |

Bonferroni controls the probability of at least one false positive by testing each of `m`
hypotheses at `α/m` — conservative: with `m = 100` and `α = 0.05`, each must clear
`p ≤ 0.0005`. Benjamini–Hochberg instead controls the expected *proportion* of false
positives among rejections and is more powerful.

**For this system the per-strategy DSR is the binding gate**, since it already encodes
multiple testing through the `N`-trial deflation. FDR and FWER are the right frame for the
library-*screening* question, and FDR is generally preferred at that stage.

> **Written down, not wired in.** The Benjamini–Hochberg helpers have zero non-test callers.
> Board-level selection bias is **disclosed, not corrected** — do not describe this system as
> correcting selection bias across the library.

---

## 6. Circular block bootstrap

When a **distribution-free** significance statement is wanted for a metric on serially
correlated returns, the naive i.i.d. bootstrap is wrong: it destroys autocorrelation and
volatility clustering. The circular block bootstrap (Politis & Romano 1992) wraps the series
into a circle so the last bar's "next" is the first, resamples contiguous blocks of length
`b` to preserve short-range dependence, and rebuilds the metric's empirical distribution
across resamples.

Block length should grow with the autocorrelation horizon (rule of thumb `b ∝ T^{1/3}`), and
the circular variant gives every observation equal resampling weight where the plain block
bootstrap under-weights the tails. This is the right tool for a Monte-Carlo confidence band,
as against the closed-form Lo (2002) standard error.

---

## The disclosure rules that go with the math

Two rules are described as architectural rather than optional:

- **Paper-claim deltas are surfaced, not hidden.** Every passport shows the source paper's
  claimed Sharpe and CAGR alongside the measured post-gate numbers. When the adaptation
  underperforms the paper — which it usually does — the delta is shown openly, and the four
  gate numbers are never collapsed into a single marketing score.
- **Price-based proxies are disclosed.** Several library strategies are price-only
  adaptations of designs that originally used cross-sectional or fundamental data. Where the
  implementation diverges from the paper's universe or data the strategy file says so, the
  paper-claimed fields are set to null when the paper reports no mechanical Sharpe or CAGR,
  and the only performance claim stood behind is the post-gate one.
