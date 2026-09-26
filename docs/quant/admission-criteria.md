# Tier-1 Strategy Admission

> **Status:** Living reference. Written 2026-06-12.
> **Author:** Önder Akkaya (quant / math lane).
> **Audience:** Anyone deciding whether a strategy belongs in the
> Archimedes-Verified (Tier 1) library, and anyone auditing why a given strategy
> passed or failed.
> **Canonical sources this doc must stay consistent with:**
> [`../specs/selection-bias-corrections-spec.md`](../specs/selection-bias-corrections-spec.md)
> (the frozen control contract) and the live implementation
> [`../../backend/archimedes/services/rigor_evaluator.py`](../../backend/archimedes/services/rigor_evaluator.py)
> (`RigorGateResult.passes_all`) plus the threshold table it reads,
> [`../../backend/archimedes/services/rigor_profiles.py`](../../backend/archimedes/services/rigor_profiles.py).
> Where this doc and those drift, **the spec and the code win** — the thresholds below are
> transcribed from `passes_all` and `_PROFILES`, not invented.
>
> **Reconciled 2026-08-31 ([#1598](https://github.com/aprin-labs/archimedes/issues/1598)):**
> against the live code and [`../adr/num-trials-self-containment.md`](../adr/num-trials-self-containment.md).
> Two corrections landed — the threshold table is the **level-1 row of a five-level
> strictness ladder**, not a set of literals (see below), and the promotion flow no longer
> deflates by the library size, a convention reversed on 2026-07-09.

Tier 1 ("Archimedes Verified 🏆") strategies get full agent autonomy and are
eligible for live vault deployment. The bar to enter is the **four-primitive
admission gate**. This doc states each control's threshold, the promotion flow, the
principled exceptions, and what monitoring continues *after* admission.

---

## The four controls and their thresholds

A strategy is admitted only when **all four** controls pass simultaneously. These
are exactly the conditions checked in `RigorGateResult.passes_all`:

| # | Control | Function | Threshold at level 1 (Conservative — the Tier-1 bar) |
|---|---|---|---|
| 1 | Deflated Sharpe Ratio | `compute_dsr` | `dsr_p_value ≥ 0.95` (and not `None`) — `rigor_profiles.DSR_P_BADGE_MIN`, the one definition of the bar (#1794) |
| 2 | Probability of Backtest Overfitting | `compute_pbo` | `pbo_score < 0.5` (and not `None`) |
| 3a | Walk-forward OOS Sharpe — absolute floor | `compute_oos_sharpe` | `oos_sharpe > 0` (and not `None`) |
| 3b | Walk-forward OOS Sharpe — the cliff | `compute_oos_sharpe` | `oos_sharpe / in_sample_sharpe ≥ 0.5` |
| 3c | CPCV path stability (when computed) | `compute_cpcv_oos_sharpe` | `cpcv_positive_fraction ≥ 0.5` |
| 4 | Look-ahead static audit | `look_ahead_audit` | `look_ahead_passed == True` |

### These are one rung of a ladder, not literals

**`passes_all` reads a profile; it never compares against a hard-coded number.** The
numbers above are the `level = 1` ("Conservative") row of the five-level strictness ladder
in [`rigor_profiles.py`](../../backend/archimedes/services/rigor_profiles.py) `_PROFILES`.
They are the right numbers for *this* document because level 1 **is** the Tier-1
"Archimedes Verified 🏆" bar and the badge is always evaluated at `STRICTEST_LEVEL` — a
user's personal deployment strictness can admit weaker strategies into their own vaults but
never rewrites the global badge. Read [`methodology.md`](methodology.md) §1 for the ladder
itself; it and this page now agree.

Three of the six rows move with the level and three do not:

| Row | Moves with strictness? | Level-1 → level-5 |
|---|---|---|
| 1 — `dsr_p_min` | **yes** (`dsr_p_min`) | `0.95 → 0.50` |
| 2 — `pbo_max` | **yes** (`pbo_max`) | `0.50 → 0.70` |
| 3b — OOS/IS cliff | **yes** (`oos_is_ratio_min`) | `0.50 → 0.30` |
| 3a — OOS absolute floor | no — always-on correctness floor (`OOS_ABS_FLOOR = 0.0`) | `> 0` at every level |
| 3c — CPCV majority | no (`CPCV_MIN_POSITIVE_FRACTION = 0.5`) | `≥ 0.5` at every level |
| 4 — look-ahead audit | no | `PASS` at every level |

A fourth always-on floor sits underneath row 1: `DSR_P_FLOOR = 0.50`, which no level
bypasses (at level 5 `dsr_p_min` collapses onto it by design). *Losing money out of sample
is broken, not "riskier"* — that is the line between a risk-tolerance knob and a
correctness floor, and it is why "you can never fully bypass the rigor gate" is true on the
live path.

Notes on each threshold, with the *why* behind the number:

### 1. DSR p-value ≥ 0.95

The published statement of the gate — the wording the app carries on the Architecture
page, which these thresholds must match:

> Over 20+ years of backtested returns net of realistic commission, a strategy's excess Sharpe
> must be positive at 95% one-sided confidence under standard errors robust to non-normality
> and autocorrelation, and must stay positive on a 30% chronological holdout. On the generated
> path, the Sharpe is additionally deflated against that strategy's own candidate pool.
>
> **On the curated library, `num_trials=1` — DSR runs undeflated (no multiple-testing
> correction).**

The Deflated Sharpe Ratio (Bailey & López de Prado 2014) returns a probability that the
excess Sharpe is positive under standard errors robust to non-normality and
autocorrelation, *and*, where a candidate pool exists, after deflating by the expected
best-of-`N` under the null. Admission requires **95% one-sided confidence**. PR #901
briefly lowered the bar; [#1794](https://github.com/aprin-labs/archimedes/issues/1794)
retired that path on 2026-09-03 (owner call) because the Generate pipeline and every public
rigor page had gone on quoting 95% throughout — two bars, and which one graded you depended
on which code path reached you. The number now exists in exactly one place,
`rigor_profiles.DSR_P_BADGE_MIN`, which the ladder's level-1 row *is*.

**What `N` is, and is not.** On the **generated** path `num_trials` is that strategy's own
candidate pool — specifically the debate's own assembled pool, `pool_size = len(pool)`, the
search we actually ran. On the **curated library** `num_trials = 1`, so `E[max_N] = 0` and
**no deflation is applied**: there was no search of ours to charge for, and promoting a
strategy into a larger library must not retroactively move its score. The value is
hard-coded on the curated serving path
([`selection_bias_routes.py:419`](../../backend/archimedes/api/selection_bias_routes.py)),
not read from any stored field, and `rigor_evaluator.py` logs the undeflated case verbatim.
The ratified decision record is
[`../adr/num-trials-self-containment.md`](../adr/num-trials-self-containment.md)
(Accepted, ratified 2026-08-31).

`average_correlation` is the correlation between *trials*, and on the curated path it is
inert by construction: `num_trials = 1` leaves nothing to deflate, and
`assert_self_contained_cohort_correlation` fails loudly if a future edit ever pairs a
cohort-wide correlation with `num_trials > 1` there. Where a pool does exist, correlation
enters the expectation-of-maximum term; [`methodology.md`](methodology.md) §1 is the
authority on the functional form and its direction.

**Disclosure is not correction *inside this gate*.** The board-level selection bias a user
incurs by picking the best of N displayed strategies is not corrected by any of the four
controls above — do not describe this gate as correcting selection bias across the library.
*(Corrected 2026-08-31, #1598.)* An earlier revision of this paragraph said the
Benjamini–Hochberg helpers had "zero non-test callers — written down, unimplemented."
That has not been true since #1185: `benjamini_hochberg_fdr` is called by
`compute_board_level_fdr`
([`rigor_evaluator.py:486`](../../backend/archimedes/services/rigor_evaluator.py)), whose
`board_fdr_significant` / `board_fdr_adjusted_p` / `board_fdr_confidence` fields ship on
every `GET /api/leaderboard` row (#1564 moved them there off the per-strategy gate, which
is why they are correctly absent from `passes_all`). The board-level correction is
**computed and served — it is simply not a gate criterion**, and per the ADR it currently
disagrees with the per-strategy gate on every strategy. See the ADR's "Consequences"
section before quoting the badge as a public claim.

### 2. PBO < 0.5

The Probability of Backtest Overfitting (Bailey, Borwein, López de Prado & Zhu
2014) is the CSCV-estimated fraction of IS/OOS splits in which the in-sample winner
underperforms the OOS median. **`PBO ≥ 0.5` means the in-sample-optimal strategy is
expected to underperform the median strategy out-of-sample** — the strategy (more
precisely, the *library's selection procedure*) fails the gate. This matches the
spec exactly: "A `pbo_score >= 0.5` means the in-sample-optimal strategy is expected
to underperform the median strategy out-of-sample — the strategy fails the rigor
gate." PBO is library-level: one value per analytics-engine run, recomputed when the
library changes.

### 3. Walk-forward OOS Sharpe — floor + cliff

Two sub-checks. The **absolute floor** (`oos_sharpe > 0`) means a negative
out-of-sample Sharpe can never pass, no matter how strong the in-sample result. The
**cliff check** (`oos_sharpe / in_sample_sharpe ≥ 0.5`) requires the out-of-sample
edge to retain at least half the in-sample edge — equivalently, **the OOS-to-IS
Sharpe degradation must be no worse than ~50%**. This is consistent with the
Bailey–López de Prado finding that overfit strategies cliff hard out-of-sample; a
≥50%-retained edge is the line between "degraded but real" and "memorized." The IS
Sharpe is computed on the first-70% slice only (see `run_rigor_gate`), so the ratio
cannot be inflated by leaking OOS data into the denominator. When a real
combinatorial OOS matrix is available, the CPCV path-stability check
(`cpcv_positive_fraction ≥ 0.5`) is an additional requirement; until then it is
honestly reported as `MISSING` and does not silently pass.

### 4. Look-ahead audit PASS

The static AST audit (`look_ahead_audit`) must return `passed = True` (zero
warnings). Any flagged forward-data access — negative pandas shift, positive feed
index, forecast-named call, or an ambiguous negative subscript — blocks admission
until a reviewer resolves it. A strategy that cannot be parsed also fails (a
`SyntaxError` returns `passed = False`).

> **Cross-reference to `rigor-methods.md`.** The judge-facing summary in
> [`../rigor-methods.md`](../rigor-methods.md) lists a fourth row "Total trades in
> backtest ≥ 10 (avoid sparse-trade illusions)." That is an *analytics-engine
> data-sufficiency* precondition applied upstream of the four statistical gates
> here — a strategy with too few trades cannot produce a meaningful return series in
> the first place. The four controls above are the statistical gate enforced in
> `RigorGateResult.passes_all`; the trade-count minimum is the data-quality
> prerequisite that must hold before those controls are even computed.

---

## The CANDIDATE → VALIDATED promotion flow

Every strategy carries a status. Promotion is gated; demotion is automatic on
re-evaluation failure.

```
                 generate / ingest
                        │
                        ▼
                  ┌───────────┐
                  │ CANDIDATE │   ← admitted to the library, NOT yet trusted
                  └─────┬─────┘      for live deployment or full agent autonomy
                        │
          run_rigor_gate(strategy_id, daily_returns,
            num_trials=1 (curated) | pool_size (generated),
            pbo_scores=…, strategy_code=…,
            average_correlation=…)
                        │
            ┌───────────┴────────────┐
            │  RigorGateResult         │
            │  .passes_all == True?    │
            └───────────┬────────────┘
                 yes ▼          ▼ no
            ┌───────────┐   stays CANDIDATE
            │ VALIDATED │   (gate_details shows exactly which
            └───────────┘    control failed: FAIL / MISSING)
```

Mechanics:

1. **A strategy enters as `CANDIDATE`.** It is in the library, its numbers are
   visible on its passport, but it is *not* eligible for live deployment or full
   agent autonomy.
2. **The gate runs via `run_rigor_gate(...)`**, which orchestrates all four controls
   and returns a `RigorGateResult`. The caller passes the pre-computed library-level
   `pbo_scores`, the strategy source for the look-ahead audit, and a `num_trials`
   **that depends on which path produced the strategy**: `1` on the curated serving
   path, and that strategy's own debate pool size on the generated path.
   *(Corrected 2026-08-31, #1598.)* This step used to pass the strategy library's own
   length as the trial count. That was the #770 convention, **reversed on
   2026-07-09** (`371a908` + `c8e0436`) and superseded by
   [`../adr/num-trials-self-containment.md`](../adr/num-trials-self-containment.md)
   — a library-sized trial count made a strategy's p-value move when an unrelated
   strategy was curated in, which is a property of the catalogue rather than of the
   strategy. Verdicts carry `"num_trials_convention": "self_contained_v2"` so
   pre- and post-reversal numbers are distinguishable; **do not compare pass rates
   across that boundary.**
3. **Promotion to `VALIDATED` requires `passes_all == True`.** Every gate must pass.
   If any returns `None` (insufficient/degenerate data) or fails its threshold, the
   strategy stays `CANDIDATE`.
4. **Transparency, not a black box.** `RigorGateResult.gate_details` renders each
   control as `PASS (p=0.97)` / `FAIL (PBO=0.61, need < 0.5)` / `MISSING`. The UI
   shows this for *candidates and validated strategies alike* — a failing strategy
   shows exactly which gate it tripped. This is the design intent: rigor as
   transparency, not as a hidden score. Paper-claim deltas are surfaced alongside,
   never collapsed into an aggregate.
5. **Re-evaluation can demote.** Because PBO is library-level, adding or removing a
   strategy can change every member's PBO; a re-run that pushes a previously-passing
   strategy's PBO `≥ 0.5` (or any other gate below threshold) returns it to
   `CANDIDATE`. Validation is a *standing* property, not a one-time stamp.

How many of the library's strategies pass all four gates is deliberately not written
down here — the live rigor gate is the only authority on which strategies currently pass; see the PASS/CANDIDATE badges in the app and `backend/archimedes/services/live_rigor_gate.py`.
The rest remain honest CANDIDATEs with their failing gate shown openly. (This
paragraph previously named Faber 2007 as a passing strategy, contradicting
[`../analysis/faber-dsr-finding.md`](../analysis/faber-dsr-finding.md) and
[`../rigor-methods.md`](../rigor-methods.md), which both record it as failing.)

---

## Principled exceptions

The gate is a hard filter by default. But two situations warrant a *documented,
reviewer-approved* exception — never a silent threshold weakening (weakening
thresholds is an explicit anti-goal).

### A. Diversification benefit vs. a marginally lower DSR

A strategy that *just* misses the DSR bar but is **genuinely
decorrelated** from the rest of the validated set can be more valuable to the
portfolio than a higher-DSR strategy that duplicates an existing bet. The portfolio
math is the justification: adding a low-correlation sleeve lowers portfolio variance
(its marginal contribution to variance, `kelly_risk_decomposition`, is small) and
raises the diversification ratio, even at a modestly lower standalone Sharpe.

This ties directly to the **fusion-quality** concept: the library is judged not as a
bag of independent strategies but as a *constructable portfolio*. A candidate's value
includes its incremental diversification, measurable via
`compute_average_pairwise_correlation(...)` against the validated set. When a
reviewer grants this exception, two things are mandatory: (1) the decorrelation must
be *real and measured* (low `ρ̄` against the validated set, not asserted), and (2)
the exception is recorded on the passport so the lower DSR and the diversification
rationale are both visible. *(Corrected 2026-08-31, #1598:)* this paragraph used to add
that "the DSR's own effective-N machinery already encodes part of this logic — correlated
trials are penalized harder, decorrelated ones less." It does not. `average_correlation` is
the correlation among a strategy's **own trials**, not its correlation against the
validated set, and on the curated path it is inert (`num_trials = 1`). Nothing in the DSR
rewards portfolio-level decorrelation — that is exactly why this exception has to be a
documented reviewer decision rather than something the statistic absorbs.

> An exception is a documented portfolio-construction decision, not a relaxation of
> the statistic. The DSR number shown does not change; what changes is the *admission
> decision*, with the reason attached.

### B. CPCV / data-sufficiency `MISSING` on a short but sound series

When a strategy's series is too short to form a combinatorial OOS matrix, the CPCV
check reports `MISSING` rather than failing. The four core controls (DSR, PBO,
single-holdout OOS, look-ahead) can still all pass. Promotion is permissible on the
core four, with the `MISSING` CPCV explicitly noted — and the strategy is flagged for
CPCV re-evaluation once more history accrues. This is *not* a weakened threshold; it
is honest reporting of an uncomputable check, with a follow-up obligation.

Both exceptions share a rule: **the number is never altered, the decision is
documented, and the caveat is visible on the passport.**

---

## Post-admission monitoring

Admission is the beginning of trust, not the end. A VALIDATED strategy is monitored
on three axes:

### 1. Live-vs-backtest tracking

The whole point of the OOS gate is to predict live behavior; we then *check that
prediction*. Live realized returns are compared against the backtest's expected
distribution. The natural tolerance is the Sharpe confidence band from
`compute_sharpe_ci(...)` (Lo 2002): if live Sharpe drifts persistently outside the
backtest's CI, the edge is decaying — consistent with McLean & Pontiff (2016)'s
finding that published predictors lose ~26% of their return out-of-sample and ~58%
post-publication. A strategy whose live performance falls materially below its
backtest band is a re-evaluation (and possible demotion) trigger.

### 2. Regime drift

Each strategy carries a `REGIME_TAG` (`bull` / `bear` / `regime_neutral`). A
strategy validated largely in one regime is at risk when the regime changes. The
**regime-conditional γ multiplier** (`REGIME_GAMMA_MULTIPLIER`, Ang & Bekaert 2002)
is the live defense: in `risk_off`/`crisis` the optimizer's effective risk aversion
rises (2×/4×), pulling allocations toward minimum-variance so a single-regime
strategy is not sized as if its favorable regime will persist. Monitoring watches for
the regime detector flipping and for the strategy's live Sharpe degrading
specifically when its tagged regime ends.

### 3. Library re-coupling (PBO recompute)

Because PBO is library-level, the validated set must be re-evaluated whenever the
library changes. Adding a new strategy that is highly correlated with the existing
set can raise PBO across the board; removing
a strategy can change every neighbor's verdict. The discipline: **recompute
`compute_pbo(...)` and `compute_average_pairwise_correlation(...)` on every library
mutation**, and re-run `run_rigor_gate(...)` for affected members. A strategy that
no longer passes returns to `CANDIDATE` automatically.

---

## Summary

- Admission = all four controls pass in `RigorGateResult.passes_all` **at strictness
  level 1**, the badge rung: DSR `p ≥ 0.95`, PBO `< 0.5`, OOS Sharpe `> 0` and
  OOS/IS `≥ 0.5` (plus CPCV `positive_fraction ≥ 0.5` when computable), look-ahead
  `PASS`. Three of those move down the ladder for a user's own deployment strictness;
  the OOS floor, the CPCV majority, the look-ahead audit and `DSR_P_FLOOR = 0.50`
  do not. The Tier-1 badge is always level 1.
- `num_trials` is **self-contained**: `1` on the curated library, the strategy's own
  debate pool size on the generated path — never the library size.
- Promotion is `CANDIDATE → VALIDATED`; failures stay `CANDIDATE` with the failing
  gate shown openly; re-evaluation can demote.
- Exceptions are documented portfolio-construction decisions (genuine
  diversification benefit; `MISSING` uncomputable checks) — never silent threshold
  weakening.
- Monitoring continues post-admission: live-vs-backtest tracking against the Sharpe
  CI, regime drift, and PBO recompute on every library change.
