---
type: gate-contract
title: Tier-1 admission gate
description: The four-control admission contract and its thresholds, the CANDIDATE to VALIDATED promotion flow, the two principled exceptions, and what monitoring continues after a strategy is admitted.
tags: [rigor-gate, admission, dsr, pbo, walk-forward, look-ahead, promotion]
verified:
  - by: openwiki/0.4.3
    at: 2026-08-31T05:55:43.566Z
sources:
  - id: openwiki-source-f88b8fdeab2b23286a6aa730
    resource: repo://docs/quant/admission-criteria.md
  - id: openwiki-source-03a78d9aa62747d43f49c7bc
    resource: repo://docs/quant/methodology.md
generated: { by: "claude-code", at: "2026-08-31T05:55:43.566Z" }
---

# Tier-1 admission gate

> **STALE ON ONE NUMBER — regenerate.** This page was generated 2026-08-31 from the quant
> slice as it then stood. On 2026-09-03,
> [#1794](https://github.com/aprin-labs/archimedes/issues/1794) retired the lower DSR bar:
> the badge bar is **0.95**, defined once as `DSR_P_BADGE_MIN` in
> `backend/archimedes/services/rigor_profiles.py`, and the "the slice does not agree with
> itself" note below describes a disagreement that no longer exists. Every `0.90` on this
> page is the retired bar. The body is left as generated rather than hand-patched, so the
> `generated:` provenance above stays true; the next openwiki run supersedes it.

Tier-1 strategies get full agent autonomy and become eligible for vault deployment. The
bar to enter is the **four-primitive admission gate**: a strategy is admitted only when
**all four controls pass simultaneously**.

> **Authority order.** The thresholds below are transcribed from the quant slice, which
> itself states that the frozen selection-bias spec and the live implementation win
> wherever a doc drifts from them. Treat this page as a map of the contract, and the
> running gate as the authority on any individual verdict.

---

## The four controls

| # | Control | Threshold as stated in the slice |
|---|---|---|
| 1 | Deflated Sharpe Ratio | `dsr_p_value ≥ 0.90`, and not `None` |
| 2 | Probability of Backtest Overfitting | `pbo_score < 0.5`, and not `None` |
| 3a | Walk-forward OOS Sharpe — absolute floor | `oos_sharpe > 0`, and not `None` |
| 3b | Walk-forward OOS Sharpe — the cliff | `oos_sharpe / in_sample_sharpe ≥ 0.5` |
| 3c | CPCV path stability, when computable | `cpcv_positive_fraction ≥ 0.5` |
| 4 | Look-ahead static audit | `look_ahead_passed == True` |

**Read the DSR row carefully — the slice does not agree with itself about where 0.90 comes
from.** One document states 0.90 as the literal gate, recalibrated from 0.95. Another
states that 0.90 is the *strictest* level of a five-level strictness ladder whose live
values come from a rigor-profiles table, and that the gate reads the selected profile
rather than a literal. Both agree the badge bar is 0.90; they disagree on whether a single
threshold exists at all. See [`documented-conflicts`](documented-conflicts.md).

### What each threshold means

**DSR at 0.90** is one-sided 90% confidence that the excess Sharpe is positive under
standard errors robust to non-normality and autocorrelation. The slice is careful about how
to phrase this: say *"deflated-Sharpe evidence at the 0.90 level"* — a one-sided ~10% test,
real but materially weaker than a conventional 0.95 bar — and **not** "statistically
proven".

**On the curated library `num_trials = 1`, so DSR runs undeflated.** There was no search of
ours to charge for, and promoting a strategy into a larger library must not retroactively
move its score. A curated-path DSR is therefore *not* multiple-testing corrected, and
describing it as such is wrong.

**Disclosure is not correction.** The board-level selection bias a user incurs by picking
the best of N displayed strategies is *disclosed*, not *corrected*. Benjamini–Hochberg
helpers exist in the codebase with zero non-test callers — written down, unimplemented. Do
not describe this gate as correcting selection bias across the library.

**PBO below 0.5** is a statement about the *library's selection procedure*, not about one
strategy. `PBO ≥ 0.5` means the in-sample-optimal strategy is expected to underperform the
median strategy out-of-sample. One value is computed per analytics-engine run and attached
to every strategy in it.

**The OOS floor and cliff** are the economic test. A negative out-of-sample Sharpe can
never pass, regardless of in-sample strength; and the out-of-sample edge must retain at
least half the in-sample edge. The in-sample Sharpe in that ratio is computed on the first
70% slice only, so the denominator cannot be inflated by leaking held-out data into it.

**The look-ahead audit** must return zero warnings. Any flagged forward-data access blocks
admission until a reviewer resolves it, and a strategy that cannot be parsed also fails —
a `SyntaxError` returns `passed = False` rather than being skipped.

**CPCV is honest about being uncomputable.** When no real combinatorial out-of-sample
matrix is available the check reports `MISSING` and does **not** silently pass.

### One row that belongs to a different layer

The judge-facing summary lists a fourth row — total trades in backtest ≥ 10. That is an
**analytics-engine data-sufficiency precondition applied upstream** of the four statistical
gates, not a fifth statistical control: a strategy with too few trades cannot produce a
meaningful return series for the controls to grade in the first place.

---

## CANDIDATE → VALIDATED

```
              generate / ingest
                     │
                     ▼
              ┌───────────┐
              │ CANDIDATE │  in the library, numbers visible on its passport,
              └─────┬─────┘  NOT eligible for deployment or full autonomy
                    │
           run the four controls
                    │
          ┌─────────┴─────────┐
       passes_all == True?    │
          │ yes          no   │
          ▼                   ▼
    ┌───────────┐      stays CANDIDATE, with gate_details
    │ VALIDATED │      showing exactly which control failed
    └───────────┘
```

Four mechanics matter:

1. **Entry is as `CANDIDATE`.** In the library, visible, but not trusted for deployment.
2. **Promotion requires every gate to pass.** If any control returns `None` — insufficient
   or degenerate data — or misses its threshold, the strategy stays `CANDIDATE`.
3. **Failure is shown, not hidden.** Each control renders as `PASS (p=0.97)` /
   `FAIL (PBO=0.61, need < 0.5)` / `MISSING`, and the UI shows this for candidates and
   validated strategies alike. This is the design intent: rigor as transparency, not as a
   hidden score. Paper-claim deltas are surfaced alongside, never collapsed into an
   aggregate.
4. **Validation is a standing property, not a stamp.** Because PBO is library-level, adding
   or removing a strategy can change every member's PBO. A re-run that pushes a
   previously-passing strategy past a threshold **returns it to `CANDIDATE` automatically**.

**How many strategies currently pass is deliberately not written down.** The live rigor
gate is the only authority. The slice states this rule in three places and violates it in
three others — see [`documented-conflicts`](documented-conflicts.md).

---

## The two principled exceptions

The gate is a hard filter by default. Weakening a threshold is an explicit anti-goal. Two
situations warrant a documented, reviewer-approved exception instead.

**A. Genuine diversification against a marginally lower DSR.** A strategy that just misses
0.90 but is genuinely decorrelated from the validated set can be worth more to the
portfolio than a higher-DSR strategy duplicating an existing bet: it lowers portfolio
variance and raises the diversification ratio. Two things are then mandatory — the
decorrelation must be **real and measured** against the validated set, not asserted, and
the exception must be **recorded on the passport** so the lower DSR and the rationale are
both visible.

**B. An uncomputable CPCV on a short but sound series.** When the series is too short to
form a combinatorial out-of-sample matrix, CPCV reports `MISSING` rather than failing, and
the four core controls can still all pass. Promotion is permissible with the `MISSING`
explicitly noted and the strategy flagged for re-evaluation once more history accrues.

Both exceptions share one rule: **the number is never altered, the decision is documented,
and the caveat is visible on the passport.** An exception is a portfolio-construction
decision, not a relaxation of the statistic.

---

## After admission

Admission is the beginning of trust, not the end. Three monitoring axes continue:

- **Live versus backtest.** The whole point of the out-of-sample gate is to predict live
  behaviour, so live realised returns are checked against the backtest's expected
  distribution, with the Sharpe confidence band as the natural tolerance. Persistent drift
  outside that band is a re-evaluation and possible demotion trigger — consistent with
  McLean & Pontiff (2016), where published predictors lost about 26% of their return
  out-of-sample and about 58% post-publication.
- **Regime drift.** A strategy validated largely in one regime is at risk when the regime
  turns. The regime-conditional gamma multiplier is the live defence (see
  [`construction-and-sizing`](../portfolio/construction-and-sizing.md)); monitoring watches
  for the regime detector flipping and for the strategy's live Sharpe degrading
  specifically once its tagged regime ends.
- **Library re-coupling.** Every library mutation requires recomputing PBO and average
  pairwise correlation and re-running the gate for affected members. A strategy that no
  longer passes returns to `CANDIDATE`.
