# Selection-Bias Corrections — Implementation Spec

> **Date:** 2026-05-13 (Day 3)
> **Owner:** Önder (math + statistics)
> **Consumers:** Dan (strategy validation gate), Daniel R. (analytics-engine
> output), Daniel S. (passport UI), Chuan (orchestrator's rigor gate)
> **Status:** Draft — ready for Önder review and implementation
> **Prerequisite reading:** ``../agora_project_analysis.md`` (routed to the private docs repo, 2026-08-19)
> § 5.3, [`./strategy-passport-spec.md`](./strategy-passport-spec.md),
> [`../architectural-principles.md`](../architectural-principles.md)

## Why this exists

The strongest red-team critique of an LLM-driven strategy-extraction pipeline
is **multiple-testing inflation**. If we evaluate N candidate strategies on
historical data and pick the top K by backtest Sharpe, we are running an
N-way selection experiment without selection-bias control. McLean & Pontiff
(2016) showed published cross-sectional predictors lost ~26% of return
out-of-sample and ~58% post-publication; Bailey, Borwein, López de Prado &
Zhu (2014) demonstrated that under realistic multiple-testing conditions
the in-sample-optimal strategy frequently does not even dominate the median
out-of-sample. Either failure mode produces a "validated" strategy that
fails in production.

**Archimedes' wedge against the 96 other AI-portfolio submissions at the
last HackMoney is that we apply the textbook corrections.** This spec
defines the contract.

## What this spec covers

Three corrections, each populating specific fields on
[`backend/archimedes/models/backtest.py`](../../backend/archimedes/models/backtest.py)
`BacktestResult`:

1. **Deflated Sharpe Ratio (DSR)** — excess Sharpe tested at 95% one-sided
   confidence under standard errors robust to non-normality and
   autocorrelation, deflated by the expected best-of-`N` **only where a
   candidate pool exists** (Bailey & López de Prado 2014). On the curated
   library `num_trials = 1` and DSR runs undeflated — no multiple-testing
   correction on that path. Board-level selection bias is *disclosed*, not
   corrected: the Benjamini–Hochberg helpers at `_rigor_helpers.py:1199` have
   zero non-test callers.
2. **Probability of Backtest Overfitting (PBO)** — CSCV-framework probability
   that the in-sample-optimal strategy underperforms the median out-of-
   sample (Bailey, Borwein, López de Prado & Zhu 2014).
3. **Walk-forward OOS Sharpe** — held-out-of-sample slice metric, separate
   from in-sample. The analytics-engine already exposes `walk_forward_split`
   and `out_of_sample_sharpe` slots; this spec defines how they're populated.

The fourth column, `look_ahead_audit_passed`, is a static-analysis check
already wired by Daniel R.'s engine — covered briefly at the end for
completeness.

## 1. Deflated Sharpe Ratio (DSR)

**Reference:** Bailey, D. H., & López de Prado, M. (2014). The Deflated
Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting and
Non-Normality. *Journal of Portfolio Management* 40(5), 94–107.

### Inputs

| Field | Source | Notes |
|---|---|---|
| `daily_returns: list[float]` | analytics-engine `BacktestResult.daily_returns` | Already populated |
| `num_trials: int` | caller (orchestrator) | See § 1.3 for sourcing rules |
| `annualization: int` | constant `252` | Daily bars assumption |

### Formula

Convention: `SR_hat` is the **per-bar** Sharpe ratio (un-annualized). If the
caller carries an annualized Sharpe, divide by `sqrt(annualization_factor)`
before computing DSR (annualization is purely a display transform). Skewness
`gamma_3` and **raw (Pearson) kurtosis** `gamma_4` (γ₄ = 3 for normal) are
likewise computed on the per-bar return series. Note: the `(γ₄ − 1)/4` coefficient
in eq. 8 below is derived for the raw-kurtosis convention; passing Fisher
*excess* kurtosis would bias the denominator by a constant `(3/4)·ŜR²`.

Given `T` per-bar returns, the per-bar Sharpe `SR_hat`, and `N` independent
trials in the selection set, the DSR is the probability that the true Sharpe
exceeds zero:

```
gamma_E = 0.5772156649        # Euler-Mascheroni
Phi_inv = standard normal inverse CDF (scipy.stats.norm.ppf)

# Bailey-López de Prado (2014) approximation for E[max_N] across N iid
# normal Sharpe estimates with unit variance:
E_max_N = (1 - gamma_E) * Phi_inv(1 - 1/N)
        + gamma_E       * Phi_inv(1 - 1/(N * e))

# Per-bar SR_hat has variance 1/(T - 1) under the iid normal null, so the
# expected best-of-N under the null is scaled by sqrt(1/(T - 1)):
SR_zero = sqrt(1 / (T - 1)) * E_max_N

# Standard normal CDF of the variance-corrected z statistic:
DSR = Phi( (SR_hat - SR_zero) * sqrt(T - 1)
         / sqrt(1 - gamma_3 * SR_hat + ((gamma_4 - 1) / 4) * SR_hat^2) )
```

`dsr_p_value` is the resulting probability (0 to 1). Higher = more confident
the true Sharpe is positive after correcting for the N-way selection.

### Outputs

Populate on `BacktestResult`:

- `deflated_sharpe_ratio: float` — the corrected Sharpe value (Sharpe units,
  not probability)
- `dsr_p_value: float` — probability the true Sharpe > 0
- `num_trials_in_selection: int` — the N value used

### Sourcing N (`num_trials_in_selection`)

For v1, `N` is the number of distinct strategies evaluated by the analytics-
engine in the same "selection round" — i.e. the size of the curated library
at evaluation time. The orchestrator passes this in as a single integer.

**Concretely:** when Önder's `evaluate(strategy, ...)` is called by the
orchestrator, the orchestrator also passes
`num_trials = len(strategy_provider.list_strategies())`. The default in the
absence of context is `1` (no correction), but a warning is logged if N=1
when more than one strategy exists in the library.

For LLM-extracted candidates (the arxiv pipeline demo), `N` should reflect
the candidate pool size from the most recent extraction pass, not the
library size — the LLM tried K methodology variants and we picked the best.
This will be wired in T5 (post-hackathon if not reached).

#### Addendum (#770) — the agentic society path: N + library_size

The agentic society (PR #766) generates **N candidates** (`n_candidates`), backtests
**all** of them, rigor-gates **all**, and keeps the **best**. Selection-from-N is itself
a multiple-testing search distinct from the library context the winner then joins, so the
effective trial count on the society path is the **sum**:

```
num_trials = n_candidates + library_size            # the chosen formula (A)
```

**Rationale.** The survivor was chosen through two independent selection layers — it beat
`n_candidates - 1` siblings in the society round *and* it is being promoted into a library
of `library_size` prior strategies. Both are searches over which the maximum was taken, so
both inflate the observed Sharpe under the Bailey & López de Prado (2014) best-of-N null;
the additive count is the conservative, defensible deflation. It can only make the gate
**stricter** (anti-goal compliant): with `n_candidates ≥ 1` it is always `≥ library_size`,
the prior (under-deflating) value.

**Why not correlation-adjusted deflation here.** *(#1558/#1559, 2026-08-31: this
paragraph originally named `N_eff = N / (1 + (N-1)ρ̄)` as the correlation refinement.
That form — the Kish design effect — was never the correlated `E[max]`; the shipped
correction scales the independent-trial `E[max]` by `√(1−ρ̄)`. The reasoning below is
unaffected: it turns on shipping the additive count first, not on which correlation
form the refinement uses.)* `compute_dsr` already accepts an
`average_correlation` and can deflate by scaling `E[max]` by `√(1−ρ̄)`
— the principled refinement when candidates are correlated
(same brief, overlapping universes). We deliberately ship the **simple additive count
first** (smaller blast radius, strictly conservative) and leave the candidate-pool
correlation correction as a follow-up; over-deflation (treating correlated candidates as
independent) only *tightens* the gate, never loosens it, so the simple form is safe to ship.

**Scope note.** *(Superseded by the #1075 addendum below, including for the gate route:
`/api/selection-bias/gate` now grades curated strategies at `num_trials = 1`, not
`library_size` — kept verbatim for history.)* This additive correction applies to the
**live society generation path** (`agents/generation_pipeline.py`). The
`/api/selection-bias/gate` route grades the **existing persisted library**, where the
selection set is the library itself — there is no fresh candidate pool, so that route
keeps `num_trials = library_size`.

> **Owner sign-off:** the choice of formula (additive A vs. effective-N B) is Önder's
> statistics call. This addendum records approach **A** as the shipped default pending his
> review; promoting to B is a follow-up if candidate-pool over-deflation proves material.
> *(Historical: formula A is superseded — see [`../adr/num-trials-self-containment.md`](../adr/num-trials-self-containment.md).
> The pending-review state carried forward and is still open.)*

#### Addendum (#1075, 2026-07-14) — decouple #2: num_trials is self-contained — SUPERSEDES the `+ library_size` term above

> **Decision record: [`../adr/num-trials-self-containment.md`](../adr/num-trials-self-containment.md).**
> That ADR is authoritative for *why* and for the decision's status; this section stays as
> the mechanical spec of *what the code computes*. Do not re-argue the convention here.
>
> **Correction to the sign-off claim below:** the merge of PR #1075 is **not** a quant
> sign-off. Two live code comments still ask for one — `generation_pipeline.py:~657`
> ("Needs Önder's sign-off (portfolio math) — it changes DSR p-values") and
> `selection_bias_routes.py:~310`. **It has never been obtained.** The ADR is therefore
> marked `Accepted, pending quant sign-off`.

PR #1075 deliberately reverses formula (A)'s `+ library_size` term. This is the one
documented exception to #770's "only stricter" anti-goal, reviewed as such — the
loosening is the point, not a side effect.

```
num_trials = n_candidates      # society/fusion generation paths (_society_num_trials(N))
num_trials = 1                 # curated single-methodology serving paths
```

**Rationale.** Bailey & López de Prado's deflation corrects for the *search that
produced the reported Sharpe*. The society round genuinely searched `n_candidates`
variants — that term stays. But a strategy does not become more overfit because the
shelf it later joins grows: `library_size` is a property of the library, not of the
trial that produced the strategy. Under formula (A) the *same* strategy's DSR verdict
changed as unrelated strategies were added — the gate verdict was not a stable
property of the strategy. Cross-library selection bias ("the shelf displays its best")
is real but is a *ranking/display* correction belonging to library-level PBO (which is
unchanged), not a per-strategy admission correction.

**Effect direction.** `num_trials` can only shrink relative to formula (A)
(`N + library ≥ N ≥ 1`), so deflation strictly weakens and pass rates can rise — an
acknowledged, reviewed loosening, stated here so nobody reads the old addendum as
current.

**Methodology versioning.** `rigor_verdict` JSON blobs written after this change carry
`"num_trials_convention": "self_contained_v2"`. Blobs without the key were computed
under formula (A) (or the pre-#770 bare library-size convention) and are **not**
directly comparable to post-change numbers. Whether to backfill/recompute stored
verdicts or surface the distinction in the UI is an open follow-up (merge-board
2026-07-14).

#### Addendum (#822) — approach B ships alongside A: real ρ̄ feeds the same `num_trials`

Approach A's additive `num_trials = n_candidates + library_size` stays the trial *count* —
this addendum does not change it. What it fixes is the `average_correlation` argument A
shipped with: `0.0`, i.e. treating all `n_candidates` society candidates as mutually
independent trials. They are generated from the **same user brief over overlapping
universes**, so in practice they are typically strongly correlated (ρ̄ ≈ 0.5–0.9), and
feeding ρ̄ = 0 over-deflates `E[max_N]` — over-stating the gate's strictness and risking
rejection of genuinely-skilled strategies whose correlated siblings get counted as
independent searches they aren't.

**What ships.** The live society path (`agents/generation_pipeline.py`) estimates ρ̄ from
the candidate pool's own return series via `compute_average_pairwise_correlation` (no
change to that function or to `compute_dsr`'s correlation term — both are reused as-is;
that term was the `N_eff` form when this addendum was written and is `√(1−ρ̄)` as of #1559) and
feeds it into the same `compute_dsr` call each candidate's DSR was already computed with,
at the same `num_trials`. `_patch_dsr_with_pool_correlation` runs once every candidate's
return series is known (mirroring the existing `_patch_pbo` two-pass shape: DSR is first
computed per-candidate at ρ̄=0 during the generation loop, then re-deflated with the pool's
real ρ̄ once the full pool exists).

**Fallback stays approach A.** With fewer than two candidate return series available (a
single-candidate generation, or every series too short to correlate), ρ̄ cannot be
estimated and the verdict is left exactly as approach A computed it — ρ̄=0, unchanged. The
same applies to fusion/debate candidates (`has_real_rigor=True`): they carry a real DSR
from their own CSCV evaluator over a parameter-variant grid, not the buy-and-hold return
series this correlation estimate is scoped to, and are excluded from the pool the same way
`_patch_pbo` already excludes them from cross-candidate PBO.

**Why this can only relax, never loosen past the no-correction floor.** *(#1559: argued
here from `1 ≤ N_eff ≤ N`; the conclusion is unchanged under the shipped form, but the
premise is now `0 ≤ √(1−ρ̄) ≤ 1`, which shrinks `E[max]` by at most a factor of one.)*
`√(1−ρ̄)` satisfies `0 ≤ √(1−ρ̄) ≤ 1` for any `ρ̄ ∈ [0, 1]`, so the corrected DSR p-value sits
between the ρ̄=0 (approach A) value and the value at `num_trials=1` (no multiple-testing
penalty at all) — it never exceeds the latter, so the gate is never made looser than the
IID-Sharpe baseline. At ρ̄→1 (candidates fully collapse to one effective trial) the
corrected p-value converges to exactly the `num_trials=1` floor, never past it.

**Scope.** Same as A: this applies to the live society generation path only. The
`/api/selection-bias/gate` route (grading the persisted library) is untouched by this
addendum — it already estimates a library-wide ρ̄ via `verdicts_for_strategies` in
`live_rigor_gate.py`, a separate, pre-existing mechanism this issue doesn't touch.

## 2. Probability of Backtest Overfitting (PBO)

**Reference:** Bailey, D. H., Borwein, J., López de Prado, M., & Zhu, J.
(2014). The Probability of Backtest Overfitting. SSRN 2326253.

### Inputs

| Field | Source | Notes |
|---|---|---|
| `returns_matrix: dict[str, list[float]]` | orchestrator | Map of `strategy_id → daily returns` for all N candidate strategies aligned on the same T dates. The evaluator converts this to an internal `np.ndarray` of shape `(T, N)` (column order pinned by sorted `strategy_id`) before running CSCV — the dict-keyed input is what the orchestrator naturally carries and preserves the strategy-id mapping needed for the `dict[str, float]` return value. |
| `S: int` | constant `16` | Number of CSCV partitions; 16 is the paper's recommended default |
| `selection_metric: callable` | default `sharpe_ratio` | The function used to pick the "best" strategy |

### Algorithm (CSCV — Combinatorially Symmetric Cross-Validation)

1. Partition the T-day return matrix into `S` equal-size submatrices along
   the time axis.
2. Enumerate every combination `C(S, S/2)` of S/2 submatrices forming the
   in-sample set; the complement is out-of-sample.
3. For each split, rank all N strategies in-sample by `selection_metric`,
   identify the strategy with the highest in-sample rank, then look up its
   out-of-sample rank.
4. The relative OOS rank `omega = rank_OOS / N` produces the logit
   `lambda = log( omega / (1 - omega) )`.
5. `PBO = P(lambda <= 0)` — the fraction of splits in which the in-sample-
   best strategy underperforms the OOS median.

### Outputs

Populate on `BacktestResult`:

- `pbo_score: float` — probability of backtest overfitting (0 to 1, lower
  is better)

A `pbo_score >= 0.5` means the in-sample-optimal strategy is expected to
underperform the median strategy out-of-sample — the strategy fails the
rigor gate.

### Computation cadence

PBO is a **library-level** metric, not a per-strategy metric. Compute once
per analytics-engine run across all strategies in the library, then attach
the same `pbo_score` to each strategy's `BacktestResult` from that run.
Re-compute when the library changes.

### Addendum (#819) — a statistical-power floor on PBO gating at small N

CSCV is a faithful estimator regardless of N — the mechanics in `compute_pbo`
are unchanged by this addendum. The concern is narrower: at our library size
today (N ~ 4-6), is a `pbo_score >= 0.5` verdict a reliable enough signal to
gate Tier-1 admission on, or is it mostly noise dressed up as a number?

**The granularity problem.** Each CSCV split ranks the IS-optimal strategy
against the OTHER N-1 members and produces one of only N possible values of
`omega = rank_OOS / N`. At N = 4, `omega` in {0.25, 0.5, 0.75, 1.0} — and
0.5 (the PBO decision boundary, `logit(omega) = 0`) is one of only four
possible outcomes, landed on exactly whenever `rank_OOS = N/2` (true for
every even N). A statistic with four possible readings per split is not
finely resolving "genuinely overfit" from "noise," and PBO inherits that
coarseness in aggregate.

**The library-coupling problem — the one that matters for gating.** PBO's
rank comparison is *relative to whoever else is in the library*. Removing or
adding one strategy re-ranks every OOS split for every remaining member. The
fraction of the comparison set that one member represents is exactly `1/N`
— at N=4 a single addition/removal changes 25% of the field; at N=10, 10%;
at N=20, 5%. This is a clean, N-only argument, independent of any specific
data: **the smaller the library, the more a single unrelated member's
presence or absence can swing everyone else's PBO verdict** — precisely the
"adding/removing one neighbor flips a verdict" failure mode this issue
raises, and it doesn't require simulation to see why it's structural.

**Simulation (corroborating, not the sole basis — see caveat below).**
Leave-one-out instability: build an N-strategy pool of IID synthetic daily
returns (T=500 bars, no true skill difference — the hardest case, since any
apparent "best" strategy is pure noise), compute the library PBO verdict
(`< 0.5` or not), remove one randomly-chosen member, recompute, and check
whether the verdict flipped. Repeated at N in {4, 6, 8, 10, 12, 16}, 10
trials each (a real run of `compute_pbo` at S=16 partitions costs ~2.2s
regardless of N — dominated by the `C(16,8)=12,870`-split Python loop, not
array width — so this is a deliberately small Monte Carlo run, not a
high-power one):

| N | leave-one-out flip rate | power (planted-edge, correctly PBO < 0.5) |
|---|---|---|
| 4 | 0.20 | 0.60 |
| 6 | 0.40 | 0.60 |
| 8 | 0.20 | 0.20 |
| 10 | 0.10 | 0.60 |
| 12 | 0.10 | 0.40 |
| 16 | 0.10 | 0.80 |

The flip rate is noisy at small N (as expected from only 10 trials) but
settles at its floor of ~0.10 from N=10 onward and never improves further
through N=16 — matching the exact `1/N = 10%` argument above almost exactly.
Power is too noisy at this trial count to read a precise curve from, but
nothing in it contradicts N=10 being a reasonable cutover point, and the
qualitative floor value it would suggest (somewhere in "high single digits
to low teens") lines up with the exact argument.

**Floor: N = 10.** Chosen where the leave-one-out flip rate first reaches
its stable floor (matching `1/N <= 10%` from the structural argument, which
holds regardless of simulation noise) — not from the simulation's power
column alone, which this trial count can't resolve precisely. Below N = 10,
`gate_details["pbo"]` reports `NOT_RUN (N=<n> below the CSCV power floor of
10)` and criterion 4 is skipped (neither required nor checked) rather than
gating on an underpowered statistic; PBO is still computed and shown
alongside as advisory. At or above N = 10, gating is unchanged from today.

**Reproduce:** the floor's behavioral contract is pinned by the
`TestPboPowerFloor` cases in `backend/tests/test_rigor_evaluator.py`
(below `MIN_LIBRARY_N_FOR_PBO_GATING` → criterion 4 reports `NOT_RUN` and
never gates; at/above → gating unchanged). The N = 10 threshold itself came
from a one-off power simulation that is not a maintained artifact; the
structural `1/N <= 10%` argument above is the durable justification, and
issue #819 records the derivation discussion.

## 3. Walk-forward Out-of-Sample Sharpe

The analytics-engine already declares `walk_forward_split` (train fraction,
default 0.70) and `out_of_sample_sharpe` on its result dataclass. Önder's
`IBacktestEvaluator` should:

1. Split `daily_returns` by `walk_forward_train_fraction` along the time
   axis. No shuffling.
2. Run the strategy logic over the **train** slice for any
   parameter-tuning the strategy supports (v1 strategies are
   non-parameterized so this is a no-op).
3. Apply the chosen parameters to the **test** slice and compute Sharpe
   over the test slice alone.
4. Populate `out_of_sample_sharpe` with the test-slice Sharpe.

The rigor gate requires `out_of_sample_sharpe / sharpe_ratio >= 0.5` —
i.e. the OOS Sharpe must be at least half the in-sample Sharpe.

## 4. Look-ahead audit

`look_ahead_audit_passed: bool` is already set by Daniel R.'s engine via
[`analytics-engine/.../engine.py`](../../analytics-engine/src/archimedes_analytics_engine/engine.py)
`_lookahead_audit_passed()`, which checks that the broker is not configured
with `coc` (close-on-close) or `coo` (close-on-open). That covers the
backtrader-level look-ahead vector; if you add additional static checks
(e.g. AST analysis of the strategy file for forward-bar references) wire
the result into the same field.

## 5. Strictness ladder (per-user deploy levels)

The four controls above define **one** verdict, but not every reader wants the
same bar. A user with a high risk tolerance may want to deploy a strategy whose
edge is *probable but not highly certain*; a conservative user wants only the
statistically strongest. We expose this as a **per-user strictness level 1–5**
(1 = Conservative, 5 = Speculative) implemented in
[`backend/archimedes/services/rigor_profiles.py`](../../backend/archimedes/services/rigor_profiles.py).

The critical distinction: only some checks are risk-tolerance knobs.

**Strictness-adjustable thresholds** (the slider moves these):

| Level | Label | DSR p ≥ | PBO < | OOS/IS ≥ |
| ----- | ----------- | ------- | ----- | -------- |
| 1 | Conservative | 0.95 | 0.50 | 0.50 |
| 2 | Balanced | 0.80 | 0.55 | 0.45 |
| 3 | Moderate | 0.70 | 0.60 | 0.40 |
| 4 | Aggressive | 0.60 | 0.65 | 0.35 |
| 5 | Speculative | 0.50 | 0.70 | 0.30 |

> The level-1 DSR bar is **0.95** — `rigor_profiles.DSR_P_BADGE_MIN`, the one place the
> number is written down (#1794, owner call 2026-09-03; PR #901's lower bar is retired).
> Thresholds relax monotonically with
> level, so "passes at level L" is monotonic in L and a well-defined
> `min_passing_level` exists (`RigorGateResult.min_passing_level`).

**Always-on floors** (identical at *every* level — never bypassable):

- the **look-ahead audit must pass** — a look-ahead-biased backtest is a lie
  about the past, not a bolder bet on the future; it is a *correctness* failure,
  not a risk preference;
- the **out-of-sample Sharpe must be > 0** — a strategy that loses money
  out-of-sample is broken, not "riskier";
- the **DSR p-value must be ≥ 0.50** (`DSR_P_FLOOR`) — below this the deflated
  Sharpe is worse than a coin flip; at level 5 the adjustable DSR bar collapses
  onto this floor by design;
- when a CPCV combinatorial matrix exists, the edge must hold on a **majority of
  held-out paths**.

This is what makes *"you can never fully bypass the rigor gate"* literally true:
even at level 5 a look-ahead-biased / OOS-negative / worse-than-coin-flip
strategy is refused. `RigorGateResult.blocked_by_floor` distinguishes "blocked
by a floor" (never deployable) from "too statistically weak for the loosest
adjustable thresholds" (`min_passing_level is None and not blocked_by_floor`).

**Badge integrity.** The global **Archimedes Verified 🏆** badge and the
persisted `passes_rigor_gate` boolean are **always evaluated at level 1** and
never move with a user's slider — otherwise one user's risk appetite would
rewrite a claim every other visitor sees, violating the #1 "claims must be true"
rule. The badge is a stable global claim; the slider is a private deploy
preference.

**Enforcement.** The strictness is enforced server-side, re-evaluated live from
persisted returns over the whole-library cohort — a non-UI caller cannot route
around it:

- `POST /api/vaults/create` and `POST /api/vaults/metadata` (the client-signed
  path's choke point) both call `_assert_strategies_pass_rigor(ids, level)`;
- allocation sizing (`derive_vault_allocations` → `size_strategies`) only sizes a
  strategy that passes at the user's level, so a strategy deployed at level L is
  not silently zeroed by the stricter level-1 badge check.

The whole ladder + floors are disclosed at `GET /api/selection-bias/strictness-ladder`
so the frontend renders labels/thresholds from one source of truth. The
`GET /api/selection-bias/gate?strictness=L` route reports each strategy's verdict
at level L plus its `min_passing_level`.

**Scope note (v1).** Curated library strategies (real returns + source) are
re-graded live at any level. *Generated* strategies remain badge-gated for
deployment in v1 — but **not** for the reason this note used to give.

The reason recorded here through 2026-08-30 was that a generated strategy's
look-ahead provenance was "a closed-DSL self-attestation rather than the AST
audit the live re-grade runs". That is no longer true.
[`dsl_lookahead_audit.py`](../../backend/archimedes/services/dsl_lookahead_audit.py)
replaced the self-declared `look_ahead_safe` boolean with a structural proof in
three parts: an AST pass over the DSL interpreter proving every bar-indexed read
carries an offset ≤ 0 (bar *t* or earlier); a walk of the validated spec proving
it uses nothing outside that audited surface; and the broker
cheat-on-close/cheat-on-open check charged on the real `cerebro` the backtest
ran. The verdict is four-state — `pass` / `fail` / `pending` (nothing was
audited) / `degenerate` (the audit ran and could not decide) — and **only `pass`
clears the LEAK criterion**. The LLM's own boolean does not survive in any form:
`look_ahead_safe` is deleted from the DSL schema, the `StrategySpec` dataclass
and the generation prompt, so there is no declaration left to read back.

Two consequences worth stating plainly, because they pull in opposite
directions and both are deliberate:

- **Deployability is fail-closed.** `pending` and `degenerate` block the gate
  exactly as hard as `fail`. An audit that reached no verdict is not evidence.
- **Rendering is honest.** Those same states must be *shown* as "not audited" /
  "undecidable", never as a failure, because nothing found a leak — nothing
  concluded. The four-state `look_ahead_status` is the single vocabulary every
  surface reads (there is no second render vocabulary to keep in sync), and
  `gate_details["look_ahead"]` renders those legs as
  `NOT_RUN (…) — blocks admission (fail-closed)` /
  `DEGENERATE (…) — blocks admission (fail-closed)` rather than `FAIL`.
- **Persisted rows get the same treatment.** `GET /api/selection-bias/gate/{id}`
  for a generated strategy grades stored columns, not a live spec, so it reads
  `look_ahead_audit_source` alongside the boolean: a row whose provenance is the
  retired `self_attested` (the boolean *was* the LLM's declaration) or
  `dsl_audit_not_run` is `pending`, not a pass — a stored `True` from the old
  self-attested path no longer deploys anything.

What still keeps generated strategies badge-gated is the remaining wiring: the
live *per-level* re-grade path takes curated strategies only. That is a
follow-up, and it is now the whole of the reason.

## 6. Board-level Benjamini-Hochberg FDR correction (#1185, 2026-08-20)

**The gap this closes.** Sections 1–5 above disclose board-level selection
bias — the strictness ladder, the passport's PBO/DSR numbers, and the
"num_trials = 1" per-strategy convention (§1.3 addendum, superseded by
[`../adr/num-trials-self-containment.md`](../adr/num-trials-self-containment.md))
all tell a user *that* browsing a 34-strategy leaderboard and deploying the
top pick is a stronger claim than one strategy graded on its own merits. None
of them *correct* for it. A Benjamini-Hochberg FDR correction
(`_rigor_helpers.benjamini_hochberg_fdr`) existed in the codebase for this
exact purpose but had zero non-test callers — and was in fact deleted as dead
code on 2026-08-18 (Tranche-1 excision, PR #1259 / commit `c02d5fad`) three
weeks *after* #1185 had already asked for it to be wired in instead. This
section restores it and records the scope decision the deletion sidestepped.

**The scope decision (issue #1185's first acceptance criterion, answered
explicitly).** `rigor_evaluator.compute_board_level_fdr` is **ADVISORY /
annotation only** — it computes a real BH-FDR-adjusted significance figure
across the current leaderboard cohort's DSR p-values and surfaces it
(`GET /api/leaderboard` → `LeaderboardResponse.board_level_fdr` + per-row
`LeaderboardEntry.board_fdr_significant` / `board_fdr_adjusted_p` /
`board_fdr_confidence` — **relocated there from the per-strategy gate by
#1564, see §6.1**), but it is **not** wired into `passes_all` /
`blocked_by_floor` at any strictness level, and it is not an input to the
leaderboard's `conviction_score` either. Three reasons, in order of
weight:

1. **Admission-policy calls belong to the rigor lane (Dan/Önder), not to an
   issue implementation.** This mirrors the precedent already set for this
   module's other cohort-shaped diagnostics — IID assumption violation (#621)
   and regime-robustness are both computed, surfaced, and explicitly kept
   OUT of `passes_all` for the same reason (see `run_rigor_gate`'s own
   comments in `rigor_evaluator.py`).
2. **Gating on it would reintroduce library-coupling.** A strategy's deploy
   status would move purely because *other, unrelated* strategies were added
   to or removed from the board — exactly the "library-coupled verdict"
   problem `num-trials-self-containment.md` deliberately rejected for
   `num_trials` (PR #1075). Silently reintroducing that coupling via a
   different statistic would cut against a decision this codebase already
   made on purpose.
3. **No quant sign-off exists for a new gate.** §1.3's addendum records that
   PR #1075's `num_trials` convention is still `Accepted, pending quant
   sign-off` — a second, unreviewed statistical gate stacked on top before
   the first is signed off is not a call an issue implementation should make
   unilaterally. Promoting board-level FDR to an actual gate is a legitimate
   follow-up; it needs its own explicit decision from Önder/Dan.

**Mechanics.** `RigorGateResult.dsr_p_value` uses the `P(true SR > 0)`
convention (HIGH = confident) — the OPPOSITE of the classical p-value
`benjamini_hochberg_fdr` expects (LOW = significant). `compute_board_level_fdr`
converts via `classical_p = 1 - dsr_p_value` before calling BH-FDR, then
reports back in the `dsr_p_value` convention (`board_fdr_confidence = 1 -
board_fdr_adjusted_p`). The converted classical p-value is floored at
`rigor_evaluator._CLASSICAL_P_FLOOR` (`1e-6`) before BH-FDR runs: a
confidently-positive series can round `dsr_p_value` to exactly `1.0`, which
without the floor would convert to an impossible `classical_p == 0.0` (and
`board_fdr_confidence == 1.0`) — not a hypothetical, the strong-series case in
`TestBoardLevelFdrWiring` hits it directly. Strategies with no finite
`dsr_p_value` (no backtest data, degenerate series) are excluded from the
correction and omitted from the result — never assigned a fabricated verdict,
matching every other MISSING convention in this gate. Default `fdr_level =
rigor_evaluator.DEFAULT_BOARD_FDR_LEVEL` (`0.05`), independently chosen (BH
convention) — NOT derived from the DSR badge's `dsr_p_min`. Computed fresh on
every board request over the exact cohort being served, so a rigor-cache hit
can never serve a stale board composition's correction.

### 6.1 Placement and rendering (#1564, 2026-08-31)

**Owner decision (Dan).** *The strategy passport carries only information about
the strategy itself; the Leaderboard is the one cross-strategy surface.
Relational metrics must not ride per-strategy responses.* Board-level FDR is
relational by construction — the same strategy's `board_fdr_significant` flips
as unrelated strategies join or leave the cohort, which is the library-coupled
verdict [`../adr/num-trials-self-containment.md`](../adr/num-trials-self-containment.md)
Decision #3 already rejected.

**Where it lives now.** `GET /api/leaderboard` only. `GET
/api/selection-bias/gate` and `GET /api/selection-bias/gate/{id}` carry no
`board_fdr` or `board_level_fdr` key —
`test_selection_bias_routes.py::TestBoardFdrStaysOffThePerStrategyGate` scans
both the response models and a live response shape and fails if one reappears.

**Cache.** No new scheduler and no cache of its own. The `dsr_p_value`s it
corrects arrive on already-built `StrategyResponse` objects, which since
[#1746](https://github.com/aprin-labs/archimedes/issues/1746) / PR-B are READ off
each strategy's stored grade on `strategy_passports` (one batched query, no gate
run) rather than recomputed per request; the BH itself (pure numpy over a few
hundred floats) recomputes per request so the correction always matches the
cohort actually served. Note what that makes true: every p-value in the cohort
was produced by a gate run whose version the row records, so a board mixing two
gate vintages is visible (`gate_version`) rather than silently averaged over.

**Cohort = the whole board, before filters and before `limit`.** Load-bearing:
BH's adjusted p is `p_(k)·m/k`, so a smaller *m* makes every row look *more*
significant. If *m* tracked the filtered/paged view, a reader could narrow
`regime_tag` or shrink `limit` until a row went significant — manufacturing the
exact selection effect the correction exists to price in. Pinned by
`test_leaderboard_board_fdr.py::test_correction_is_invariant_to_limit` /
`..._to_regime_and_min_rigor_filters`, each with an anti-vacuity test proving
the shrink really would flip the verdict.

**Rendering (the other half of the owner's call: "render it honestly, as best
we can").** The research board gets a `Board FDR` column plus a board-level
line. When nothing clears — the state prod is in, per Önder's pull on #1555 —
it reads *"Not yet distinguishable from selection noise at board level."* A row
with no finite `dsr_p_value` was never corrected and renders an **em-dash**,
never a verdict; `None` is not `False`. Guarded in
`ui/test/leaderboard-board-fdr.test.js`.

**`library_pbo` STAYS on the per-strategy result** (the call #1564 item 3
asked for; it was flagged as the same impurity class and is not one). It is
byte-identical for every strategy in a selection set, so it is a *disclosure*,
not a per-strategy verdict — nothing about strategy A's passport changes
because of strategy B. And it is the scope label for a number the same
response already shows: on the curated path `pbo_score` comes from
`compute_pbo`, which assigns one library-wide CSCV value to every strategy.
Removing `library_pbo` would delete the label and leave the labelled number —
strictly less honest, and it would purify nothing. Pinned by
`test_selection_bias_routes.py::TestLibraryPboStaysOnThePassport`.

## API surface

Önder's `IBacktestEvaluator.evaluate` signature already takes the strategy
and price data. Extend it once to accept `num_trials`:

```python
def evaluate(
    self,
    strategy: Strategy,
    price_data: dict[str, list[float]],
    start_date: str | None = None,
    end_date: str | None = None,
    num_trials: int = 1,  # NEW — for DSR multiple-testing correction
) -> BacktestResult: ...
```

A new method for library-level PBO:

```python
def compute_pbo(
    self,
    returns_matrix: dict[str, list[float]],  # strategy_id -> daily returns
    s_partitions: int = 16,
) -> dict[str, float]:  # strategy_id -> pbo_score (all identical)
    """Compute PBO across the full strategy library."""
```

## Acceptance criteria for v1

- [ ] `BacktestResult.deflated_sharpe_ratio` and `dsr_p_value` populated for
      every backtest run with `num_trials > 1`.
- [ ] `num_trials_in_selection` recorded so the correction is reproducible.
- [ ] `pbo_score` computed once per library run and attached to every
      strategy's result from that run.
- [ ] `out_of_sample_sharpe` populated per strategy via walk-forward split.
- [ ] `BacktestResult.passes_rigor_gate` returns `True` only when all four
      controls are present and pass their thresholds.
- [ ] Unit tests: a hand-constructed return series with known properties
      reproduces a DSR and PBO matching reference values (within tolerance).

## Numerical sanity-check examples (for unit test seed)

All three cases use the per-bar convention: `SR_per_bar = SR_annualized /
sqrt(252)` for daily bars, and skew / **raw (Pearson) kurtosis** (γ₄ = 3 for
normal) computed on the per-bar series. Reference values below were computed
against `scipy.stats.norm.ppf` and `norm.cdf`; pin the unit tests to these to
catch implementation drift.

| Case | `SR_ann` | `T` | `skew` | `raw_kurt` | `N` | `SR_zero` (per-bar) | `z` | `dsr_p_value` |
|---|---|---|---|---|---|---|---|---|
| A — strong | 1.8 | 2520 | −0.4 | 6.2  | 10   | 0.0314 |  3.994 | ~1.0000 |
| B — borderline | 0.9 | 1260 | −0.2 | 5.0  | 20   | 0.0536 |  0.110 | ~0.5439 |
| C — failure | 0.3 | 504  |  0.0 | 3.0  | 1000 | 0.1451 | −2.831 | ~0.0023 |

Case A is the slam-dunk: a long, smooth backtest with a small library.
Case B is the "credibly positive but not at the 95% bar" boundary used to
exercise the gate threshold. Case C is the multiple-testing failure mode —
a weak Sharpe pulled out of a thousand-trial selection should *not* clear
the gate, even with arbitrarily clean residuals.

## What this spec deliberately does not specify

- Exact numerical-library choice (scipy vs. numpy vs. statsmodels) — Önder
  picks.
- Specific test fixtures or property-based test setup — Önder owns.
- The PBO `S` parameter beyond the default `16` — known-good per the paper.
- Encryption / privacy of trial counts (a v2 concern; v1 is fully public).

## References

Bailey, D. H., & López de Prado, M. (2014). The Deflated Sharpe Ratio:
Correcting for Selection Bias, Backtest Overfitting and Non-Normality.
*Journal of Portfolio Management* 40(5), 94–107.
<https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551>

Bailey, D. H., Borwein, J., López de Prado, M., & Zhu, J. (2014). The
Probability of Backtest Overfitting. SSRN 2326253.
<https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253>

McLean, R. D., & Pontiff, J. (2016). Does Academic Research Destroy Stock
Return Predictability? *Journal of Finance* 71(1), 5–32.
<https://onlinelibrary.wiley.com/doi/abs/10.1111/jofi.12365>
