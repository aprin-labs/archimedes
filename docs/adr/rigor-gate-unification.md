# ADR: Rigor-gate unification (single source of selection-bias truth)

> **Audience:** Archimedes team (decision owner: Dan; rigor lane: Önder)
> **Status:** Accepted
> **Date:** 2026-06-26 (recorded; decision date [unestablished — needs Dan])
> **Owner:** Dan Browne (rigor lane: Önder Akkaya)
> **Supersedes:** —
> **Superseded-by:** —
> **Question being decided:** Should every Tier-1 strategy pass the four selection-bias controls via ONE authoritative gate path, or are different gate definitions (a fast one for the library list, a strict one for the passport) acceptable?
> **Related:** [PR #710](https://github.com/aprin-labs/archimedes/pull/710) (full-tree technical audit), [`docs/specs/selection-bias-corrections-spec.md`](../specs/selection-bias-corrections-spec.md), `backend/archimedes/services/rigor_evaluator.py` (`run_rigor_gate`).

## TL;DR

**Every Tier-1 strategy passes the SAME four selection-bias controls — Deflated Sharpe Ratio (DSR), Probability of Backtest Overfitting (PBO), walk-forward OOS Sharpe, look-ahead audit — via ONE authoritative rigor gate.** The gate is unified (one code path, one set of thresholds, one verdict per strategy) and surfaced honestly in the passport (each control shown as its own field, not hidden behind an aggregate score). Failed-rigor strategies are visible failures, not silently dropped. The motivation: Bogdan's PR #710 audit found an earlier implementation had a *fake-strict* rigor badge — the passport claimed rigor but the gate was lenient/incomplete, violating the "claims must be true on the live path" rule.

## Context

The selection-bias spec (Bailey & López de Prado 2014; Bailey, Borwein, López de Prado & Zhu 2014) names four orthogonal controls that separate credible published alpha from curve-fit artifacts: DSR (p-value ≥ 0.95), PBO via CSCV (< 0.5), walk-forward OOS Sharpe (no in/out-of-sample cliff), and a look-ahead static audit. The pitch claims Tier-1 strategies survive all four — the Day-3 red-team insight that became the wedge. The risk the audit found: two divergent gate paths (a fast lenient one for the list view, a stricter one for detail) let the "✓ Rigor Gate Passed" badge display for strategies that passed the lenient path but not the strict one.

## Decision

**One unified `run_rigor_gate()` computes all four controls, records each independently, and emits a single boolean verdict** (`passes_all = DSR p≥0.95 AND PBO<0.5 AND OOS/IS≥0.5 AND look-ahead PASS`). Every read — library list, passport detail, on-chain anchor eligibility — consults the same verdict.

- **Honest surfacing:** the passport returns all four results as separate fields (with the actual numbers), not an aggregate. Paper-claim deltas are shown, never hidden.
- **Failure visibility:** failed strategies persist with `verdict='rigor_fail'` and are queryable, not silently dropped — defeating the "keep generating until one passes by chance" dynamic.
- **On-chain eligibility:** a strategy is anchored to the `StrategyRegistry` iff `passes_rigor_gate=true`. Fail-closed: a missing/NaN metric fails its criterion rather than silently passing.

## Consequences

### Positive
- **"Claims must be true on the live path" is load-bearing** — the gate behaves identically whether tested locally, rendered on the site, or audited on-chain.
- **Complete audit trail** — recompute the four metrics off-chain, read the hashes on-chain, cross-check. The loop is closed and third-party-verifiable.
- **Falsifiable** — any reader can check `dsr_p_value` against 0.95; no hiding behind aggregate scores.

### Negative / costs we accept
- **Four fixed thresholds to defend** — if research consensus shifts, they must be re-evaluated in lockstep (no per-strategy exceptions).
- **PBO is library-wide**, so adding a strategy recomputes the selection-set PBO; the passport records `num_trials` so the computation stays reproducible.
- **Synchronous gate latency** — the generator waits for the gate; acceptable for the demo (the user reviews the passport anyway).

## Alternatives considered
- **Two-tier rigor (fast list gate + strict detail gate) — rejected:** this is exactly what produced the fake-strict badge. Two paths = two truths.
- **Adaptive thresholds (scale with library size N) — rejected** for auditability: a moving target erodes "it passed" credibility.
- **Lazy PBO (defer to deploy-time) — rejected:** the user should see the full verdict before deciding, not have the gate fail at deploy.

## Ratification

Adopted; the fake-strict bug flagged by the PR #710 audit is closed and the gate is unified. (See also [k1-generation-external-rigor-gate](k1-generation-external-rigor-gate.md) — the gate runs *external* to the K=1 generator.)
