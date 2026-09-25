---
type: subsystem-explanation
title: Portfolio construction and position sizing
description: How a risk profile becomes portfolio weights — the mean-variance family, Kelly sizing and its gamma mapping, the regime-conditional gamma multiplier, covariance shrinkage, and the risk-attribution primitives — separating what the optimizer routes today from what is documented as intended.
tags: [portfolio, optimizer, kelly, risk-aversion, regime, covariance-shrinkage]
verified:
  - by: openwiki/0.4.3
    at: 2026-08-31T05:55:43.566Z
sources:
  - id: openwiki-source-03a78d9aa62747d43f49c7bc
    resource: repo://docs/quant/methodology.md
generated: { by: "claude-code", at: "2026-08-31T05:55:43.566Z" }
---

# Portfolio construction and position sizing

The rigor gate decides *what is allowed into the library*. This is the other half: how the
allowed set becomes weights. The mapping is driven by a vault **risk profile** — one of
`FIXED_INCOME`, `CONSERVATIVE`, `MODERATE`, `AGGRESSIVE`, `HYPER_RISKY` — and each profile
selects an objective.

> **Scope note.** This page summarises the quant slice's account of the optimizer. The
> implementing code is outside the slice this wiki was generated from, so treat every
> statement here as *what the methodology document specifies*, not as verified behaviour of
> the running system. The slice itself says the spec and the code win wherever they drift.

---

## The mean-variance family

A portfolio is a point in expected-return / variance space, and for any target return
there is a minimum-variance weight vector. Sweeping the target traces the **efficient
frontier**, solved as a sequence of `min wᵀΣw  s.t. wᵀμ = target, 1ᵀw = 1` problems via
SLSQP.

Every objective below is solved on the **long-only unit simplex** with a per-asset weight
cap — default 0.40, raised to 0.60 for the hyper-risky profile — to prevent degenerate
corner solutions, and falls back to equal weight when SLSQP fails or history is shorter
than 20 bars.

| Risk profile | Objective |
|---|---|
| `CONSERVATIVE` | Global Minimum Variance |
| `MODERATE` / `AGGRESSIVE` | Max Sharpe (tangency) |
| `HYPER_RISKY` | Max expected return (LP) |

**Global Minimum Variance** minimises `wᵀΣw` subject to the simplex and cap constraints.
It serves the conservative profile because it is **the most estimation-robust objective
available**: it depends only on the covariance matrix and never on the notoriously noisy
expected-return vector. When you cannot trust your return forecasts, minimise variance.

**Max-Sharpe** finds the tangency portfolio — the point where a ray from the risk-free
rate touches the frontier. It is more sensitive to estimation error in `μ` than GMV, which
is exactly why the covariance is shrunk and, on the Kelly path, `μ` is shrunk too.

---

## Kelly sizing

Kelly asks what fraction of capital maximises long-run log growth. For a continuous return
stream the single-asset full-Kelly fraction is `f* = (μ_ann − rf_ann) / σ²_ann`.

- **Full Kelly is treated as an academic reference only.** It maximises growth but is
  acutely sensitive to estimation error in `μ`, and over-betting can be catastrophic.
- **Half-Kelly is the default.** It gives up a small amount of long-run growth for a large
  reduction in drawdown volatility — the standard risk-management convention.
- Kelly is defined on **excess** returns; using gross returns inflates every allocation by
  `rf/σ²`. The output is clipped to `[0, 1]` (no leverage), and a non-positive excess
  return returns exactly zero.

### Kelly expressed as risk aversion

In the multi-asset path Kelly becomes a risk-aversion-parameterised mean-variance
objective: `maximize wᵀ(μ − rf) − ½·γ·wᵀΣw` subject to a per-asset cap and a budget
constraint. `γ = 2` reproduces half-Kelly; `γ → 0` approaches full Kelly; `γ → ∞`
collapses to minimum-variance.

| Profile | Baseline γ | Reading |
|---|---|---|
| `fixed_income` | 12.0 | Extreme preservation; minvar-dominated |
| `conservative` | 6.0 | Capital preservation first |
| `moderate` | 3.0 | Balanced |
| `aggressive` | 2.0 | Half-Kelly; accepts drawdown |
| `hyper_risky` | 1.5 | Near-full-Kelly |

### The regime multiplier

When a live regime is supplied, effective risk aversion becomes
`γ_eff = γ_profile × REGIME_GAMMA_MULTIPLIER[regime]`, with multipliers of 1.0× for
`risk_on` and `transition`, **2.0× for `risk_off`, and 4.0× for `crisis`**.

The consequence is concrete: a `moderate` investor in a `crisis` regime operates at
`γ = 12.0` — the same risk aversion as `fixed_income` in normal times — **without the user
changing their declared profile**. This is the mechanism that stops a single-regime
strategy from being sized as though its favourable regime will persist.

The multipliers are explicitly documented as **an engineering judgment, not a claim of
optimality**: research papers sometimes use 6–10× in tail regimes, and the smaller values
are chosen deliberately to avoid whipsawing allocations on every regime flip.

---

## Estimation robustness

**Ledoit–Wolf covariance shrinkage** is the production estimator. It shrinks the sample
covariance toward a scaled-identity target, `Σ* = δ·μ·I + (1 − δ)·S`, where the intensity
`δ*` is derived **analytically from the data rather than hand-tuned**: a short, noisy
sample shrinks hard; a long, clean one barely shrinks. A fixed-intensity diagonal fallback
at α = 0.10 runs only when the analytic estimator cannot. Shrinkage is the practical face
of robust optimisation — it keeps the optimizer's output stable under the estimation error
that plagues naive mean-variance.

**Black–Litterman** is documented as the principled answer to mean-variance's real
weakness: small changes in `μ` produce enormous swings in weights. It starts from
market-implied equilibrium returns as a Bayesian prior and blends in explicit views with
stated confidences. The slice maps this onto the existing `mu_override` + `mu_shrinkage`
blending, which shrinks paper-extrapolated `μ` toward each asset's own sample mean at a
default 0.5 blend — so a single strategy's claimed CAGR is not promised across every asset
it voted for.

**Hierarchical Risk Parity** avoids Markowitz's worst failure mode — inverting an
ill-conditioned covariance matrix, where a near-singular `Σ` turns estimation noise into
wild corner weights. HRP never inverts `Σ`; it clusters the correlation matrix into a tree,
quasi-diagonalises, and allocates top-down by recursive bisection.

> **Documented, not routed.** The slice carries an explicit status note: the production
> optimizer today routes the five profiles through GMV / max-Sharpe / max-return with
> Ledoit–Wolf shrinkage. **HRP is documented as the principled diversification objective
> and the intended path for many-asset, high-correlation baskets** — not as what runs.

---

## Making the output legible

Two reporting primitives exist so the optimizer is not a black box:

- **Risk decomposition** — per-asset marginal contribution to portfolio variance via the
  Euler decomposition `MCᵢ = wᵢ·(Σw)ᵢ / σ²ₚ`, which sums to 1. This is what lets the UI say
  "GLD contributes 22% of portfolio variance", and it is the detector for concentration
  risk described in [`reading-a-backtest`](../rigor/reading-a-backtest.md).
- **Closed-form risk figures** — a one-year expected maximum drawdown using the
  Magdon-Ismail & Atiya (2004) approximation (a real improvement on the naive 2σ
  heuristic), and a parametric normal 95% VaR. Both return positive decimals and are
  explainable to a non-quant.

---

## The risk-free-rate seam

There is a live inconsistency worth knowing before quoting any excess-return figure. The
flat `_RF_ANNUAL = 0.05` is now the **fallback** convention only: DSR, out-of-sample
Sharpe, and in-sample Sharpe can instead be graded against the actual historical
three-month T-bill series when a caller threads dates through, with the choice disclosed
per result via an `rf_convention` field.

**Not every consumer moved together.** Kelly and the portfolio optimizer still take only
the flat constant. The slice states the consequence plainly: "every excess-return figure is
computed on the same basis" is **no longer accurate as a blanket claim**, because the
gate's Sharpe figures can diverge from Kelly's and the optimizer's basis whenever the
former are graded on the historical series.
