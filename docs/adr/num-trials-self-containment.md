# ADR: `num_trials` is self-contained — a strategy's trial count depends only on that strategy

> **Audience:** Archimedes team
> **Status:** **Accepted** — ratified 2026-08-31 by Önder Akkaya (portfolio math), [#1555](https://github.com/aprin-labs/archimedes/issues/1555) outcome 3; see "Ratification" below
> **Date:** 2026-07-09 (reversal shipped); spec addendum 2026-07-14; amended 2026-08-19 (fixture-leak class); ratified + four corrections folded in 2026-08-31 (#1555); amended 2026-09-01 (#1654 option 1)
> **Owner:** Dan Browne (quant reviewer of record: Önder Akkaya)
> **Supersedes:** the `N + library_size` DSR convention from [#770](https://github.com/aprin-labs/archimedes/issues/770) / #811 / [#820](https://github.com/aprin-labs/archimedes/issues/820)
> **Superseded-by:** —
> **Question being decided:** What is the multiple-testing trial count `num_trials` that deflates a strategy's Sharpe ratio — does the size of the strategy library enter it?
> **Related:** [`backend/archimedes/agents/generation_pipeline.py:645-660`](../../backend/archimedes/agents/generation_pipeline.py) (`_society_num_trials`), [`backend/archimedes/api/selection_bias_routes.py:300-315`](../../backend/archimedes/api/selection_bias_routes.py), [`docs/specs/selection-bias-corrections-spec.md` § 1.3](../specs/selection-bias-corrections-spec.md) (addendum #1075), [`rigor-gate-unification.md`](rigor-gate-unification.md).

## TL;DR

**DSR deflation happens inside the debate society, and `num_trials` is the candidate pool
*that search actually considered* — never `N + library_size`.** A curated single-paper
implementation is graded at `num_trials = 1`, because there is no search of ours to deflate:
the paper's headline configuration is the only configuration we tried. **Cross-strategy
comparison belongs on the Leaderboard and the Marketplace, not in the per-strategy gate.**
Promoting a strategy into a bigger library must not retroactively change its Deflated
Sharpe. One deliberate exception is named below: PBO (criterion 4) is cohort-level by
construction and stays in the gate — the self-containment claim is about `num_trials`,
not the whole verdict (see "The PBO carve-out").

## Context

The Deflated Sharpe Ratio (Bailey & López de Prado) corrects a Sharpe ratio for
multiple-testing: if you tried `num_trials` configurations and reported the best, the
expected maximum Sharpe under the null is inflated, and DSR deflates by exactly that
expectation-of-maximum term. The whole question is therefore **what counts as a trial**.

The answer drifted three times:

| When | Commit / PR | Convention |
|---|---|---|
| 2026-06-29 | `737b3117` ([#770](https://github.com/aprin-labs/archimedes/issues/770)) | `num_trials = n_candidates + library_size` on the society path — the library treated as a second selection layer |
| 2026-07-03 | `a47edde` ([#820](https://github.com/aprin-labs/archimedes/issues/820)) | the same additive formula unified across the live and fusion generation paths |
| — | `dfa8fc1` | DSR deflation made to fire on *every* rigor-gate path (and the PBO floor documented honestly) |
| **2026-07-09** | **`371a908` + `c8e0436`** | **reversed** — self-contained trial count, core sites then the remaining sites with green tests |
| 2026-07-14 | `b4481b8` (spec addendum #1075) | the spec addendum superseding the #770 formula, plus a verdict methodology marker |

The `+ library_size` convention has a defect that is easiest to see as a thought
experiment: **curating one more strategy into the library retroactively lowers the DSR of
every strategy already in it.** Nothing about an existing strategy changed — not its
returns, not its methodology, not the search that produced it — yet its p-value moved. A
rigor verdict that mutates when an unrelated strategy is added is not a property of the
strategy; it is a property of the catalogue. It also means a strategy's passport is not
reproducible from the strategy's own artifacts, which contradicts the externally-verifiable
provenance commitment in [`k1-generation-external-rigor-gate.md`](k1-generation-external-rigor-gate.md).

The second half of the problem is the curated library. A curated implementation of a
published paper is not the output of *our* search — we did not try 40 configurations and
report the best; we implemented the configuration the paper published. Deflating it by the
size of our library charges it for a search that never happened.

The unification of the rigor gate ([`rigor-gate-unification.md`](rigor-gate-unification.md))
made this decidable: with one gate path there is exactly one place the trial count is
sourced, and with one generation path
([`debate-society-sole-generation-pipeline.md`](debate-society-sole-generation-pipeline.md))
"the pool the search considered" is well-defined.

## Decision

**`num_trials` is self-contained: it counts only the search that produced *this* strategy.**

1. **Deflation happens inside the debate society**, where the search happens.
   `_society_num_trials(selection_pool_size)` returns `max(1, selection_pool_size)` —
   the N generated candidates the winner was chosen from, **not** `N + library_size`
   ([`generation_pipeline.py:645-660`](../../backend/archimedes/agents/generation_pipeline.py)).
   *(Precision added at ratification, 2026-08-31:)* the count that actually reaches DSR is
   the debate's own assembled pool, `pool_size = len(pool)` (`debate_engine.py:695`) — not
   the user-requested `n_candidates`, which earlier drafts of this ADR named. The pool the
   search actually considered is the right count, and it is what was ratified.
   Candidates carry their own `dsr_num_trials` and are re-scored at the same count
   (`generation_pipeline.py:484,514-516`), so a re-computation reproduces the original
   verdict.
2. **Curated single-paper implementations are graded at `num_trials = 1`**
   ([`selection_bias_routes.py:~312`](../../backend/archimedes/api/selection_bias_routes.py)).
   With `num_trials = 1` the expectation-of-maximum term collapses and the strategy is
   judged purely on its own return series — which is the honest description of what a
   single-paper implementation is.
3. **Cross-strategy comparison belongs on the Leaderboard and the Marketplace, not in the
   per-strategy gate.** "Is this strategy statistically sound on its own evidence?" and "is
   this strategy better than the other 40?" are different questions with different
   audiences. Conflating them made the gate answer neither well. The gate answers the first;
   ranking answers the second.
4. **The convention is named in the payload.** Gate output carries
   `"num_trials_convention": "self_contained_v2"`
   ([`generation_pipeline.py:394-395`](../../backend/archimedes/agents/generation_pipeline.py)),
   so a passport records which convention produced its verdict and old and new verdicts are
   distinguishable rather than silently mixed.

### The PBO carve-out — what the headline does not claim (2026-08-31)

Decision #3 reads as an absolute, and for `num_trials`/DSR it is. It is **not** true of the
whole verdict: criterion 4 is PBO, and PBO is a library-level metric by construction (CSCV
— `compute_pbo`'s own docstring says adding or removing a neighbour can flip it). Gating is
suppressed below `MIN_LIBRARY_N_FOR_PBO_GATING = 10`
([`rigor_evaluator.py:570`](../../backend/archimedes/services/rigor_evaluator.py)) and the
gate currently grades 34 strategies, so cohort PBO is a **live** gating criterion today:
curating a 35th strategy can move an existing strategy's `passes_all` through PBO — the
retroactivity this ADR removes from `num_trials`, arriving through a different criterion.
That is accepted, not scheduled away: a self-contained PBO is not a thing. This is why the
headline claim is "`num_trials` is self-contained," deliberately not "the gate is."
5. **Correlation enters through the equicorrelated E[max], not an effective trial count**
   *(corrected 2026-08-31).* This item originally recorded `N_eff = N / (1 + (N-1)ρ̄)` as
   the correlation adjustment. [#1558](https://github.com/aprin-labs/archimedes/issues/1558)
   showed that form is the Kish design effect — the wrong functional form for an
   expectation of a maximum: it saturates in N and under-deflates, admitting pure noise at
   up to ~29% against a nominal 10% under a zero-Sharpe null.
   [#1559](https://github.com/aprin-labs/archimedes/pull/1559) (merged 2026-08-31) replaced it
   with the exact one-factor result `E[max] = √(1−ρ̄) · E[max of N iid]`. The decision
   boundary is unchanged either way: self-containment decides *what N counts*; how ρ̄
   enters is the DSR implementation's concern, and ratifying this ADR does not bless it
   (nor did the bug invalidate the convention).

## Consequences

### Positive
- **A strategy's rigor verdict is a property of the strategy.** It does not move when the
  library grows, so a passport is reproducible from the strategy's own artifacts — which is
  what "externally verifiable" was supposed to mean.
- **Curated strategies are graded for the search we actually did**, which is none.
- **The gate has one job.** Selection-bias soundness, per strategy. Ranking moved to where
  ranking belongs.
- **Verdicts are labelled by convention**, so the reversal is auditable rather than a silent
  re-grade.

### Negative / costs we accept — recorded honestly
- **This is a loosening, and it moves in the direction that flatters us.** `num_trials` can
  only shrink relative to formula (A), so DSR p-values can only improve. A reviewer is
  entitled to be suspicious of a rigor change that raises pass rates, and the code comments
  say so explicitly ("it raises curated pass rates by removing a cross-strategy penalty").
  The argument for it is a correctness argument, not a results argument — but the results
  moved, and that must not be buried.
- **No curated-library pass count is stated here or anywhere in the docs.** Three
  strategies previously reported as passing were found to be grading equity-like return
  series (~18.5% annual vol) that arrived through a data-feed fallback in the backtest
  loader. The pass rate on corrected data is not established. Any number in a document is a
  claim we cannot currently support; the live rigor gate is the only answer.
- **We give up a real signal.** There genuinely is a portfolio-level multiple-testing
  problem: selecting from a 40-strategy library *is* a selection event for the user doing
  the selecting. This decision moves that concern to the Leaderboard/Marketplace surface.
  *(Reframed twice on 2026-08-31 — second time on prod evidence.)* The correction
  **exists, is served, and is answering**: a board-level Benjamini–Hochberg FDR
  ([`compute_board_level_fdr`, `rigor_evaluator.py:405`](../../backend/archimedes/services/rigor_evaluator.py))
  ships `board_fdr_significant` / `board_fdr_adjusted_p` / `board_fdr_confidence` per
  row plus `n_tested` / `n_significant` as a top-level key on every
  `/api/leaderboard` response (it rode `/api/selection-bias/gate` until #1564 moved it —
  see below), with BH the right method under the positive
  dependence of strategies sharing a universe. And it **currently disagrees with the
  per-strategy gate on every strategy**: pulled from prod 2026-08-30, the minimum
  `board_fdr_adjusted_p` across the whole board is **0.319** — nothing clears a
  board-level FDR threshold at any conventional level, including every strategy that
  passes the per-strategy gate (adjusted p 0.319 / 0.319 / 0.536, all
  `board_fdr_significant: false`). That is not a contradiction in the math — the gate
  answers "sound on its own evidence," the board FDR answers "distinguishable from the
  field's multiple testing," and both can be true. **The product decision is recorded
  and the wiring has shipped** — option 1 on
  [#1654](https://github.com/aprin-labs/archimedes/issues/1654) (2026-09-01 amendment below);
  remaining work is none unless a new ranking surface shows an unqualified badge.
  (Evidence: [Önder's prod pull](https://github.com/aprin-labs/archimedes/issues/1555#issuecomment-5471987448),
  #1555 thread; no pass count quoted, per the standing rule — the point stands on the
  adjusted p-values, a property of the correction rather than of the return data.)
  **Both halves decided by the owner and shipped in
  [#1564](https://github.com/aprin-labs/archimedes/issues/1564) (2026-08-31).**
  *Placement:* the passport carries only per-strategy information; the Leaderboard is the
  one cross-strategy surface. The `board_fdr_*` fields moved off the per-strategy gate
  response onto `GET /api/leaderboard`, riding its existing cache semantics — which brings
  the API shape into line with Decision #3 itself. `library_pbo` **stays** on the passport:
  it is byte-identical across the selection set, so it is a disclosure rather than a
  per-strategy verdict, and it is the scope label for the `pbo_score` the same response
  already shows (rationale + guards in
  [`../specs/selection-bias-corrections-spec.md`](../specs/selection-bias-corrections-spec.md) §6.1).
  *Rendering:* the board says it plainly — a per-row column plus, while nothing clears,
  "not yet distinguishable from selection noise at board level". An uncorrected row renders
  an em-dash, never a verdict. The forward experiment that could eventually clear it is the
  live paper ledger ([#1563](https://github.com/aprin-labs/archimedes/issues/1563)).
- **Two conventions exist in the historical record.** Verdicts computed before 2026-07-09
  used formula (A); `num_trials_convention` distinguishes them, but any longitudinal
  comparison of pass rates across that boundary is invalid.

## Ratification — signed off 2026-08-31

**Önder Akkaya ratified both legs on 2026-08-31** — the society pool-size count and the
curated `num_trials = 1` — as outcome 3 of
[#1555](https://github.com/aprin-labs/archimedes/issues/1555)
([the review](https://github.com/aprin-labs/archimedes/issues/1555#issuecomment-5471788506)),
after reading the live code rather than this document's description of it. Status is plain
**Accepted**. The two code comments that named the sign-off as required
(`generation_pipeline.py`, `selection_bias_routes.py`) were updated in the same commit as
this stamp.

What the review affirmed, in its own terms:

- **Self-containment**, on a stronger form of the retroactivity argument than this ADR
  carried: under `N + library_size` a p-value is not recomputable from the strategy's own
  artifacts, which makes the passport unverifiable by anyone outside the system — the
  property [`k1-generation-external-rigor-gate`](k1-generation-external-rigor-gate.md)
  is built on. *"A verdict you cannot reproduce from what you published is not provenance."*
- **Curated `num_trials = 1`** as the honest floor, with an explicit anti-goal: do not
  later adopt "deflate by the paper's reported configuration count" without a source for
  the count.
- **The loosening** judged the removal of a penalty charged for a search nobody ran — a
  correctness consequence, not grade inflation.

An earlier revision of this section called the missing sign-off "the single largest
outstanding rigor risk in the tree." That sentence was removed at the reviewer's request as
part of the stamp: while it stood, a larger and undocumented error sat in the same
statistic — the DSR's correlated-trials correction used the wrong functional form
([#1558](https://github.com/aprin-labs/archimedes/issues/1558), fixed in
[#1559](https://github.com/aprin-labs/archimedes/pull/1559)), admitting noise at up to ~29%
against a nominal 10%. The general lesson is recorded here because it is the same class as
this ADR's 2026-08-19 amendment: **provenance is not correctness, and endpoint tests are
not a guard** — both the removed `√(1−ρ)` factor and the wrong `N_eff` form satisfied the
ρ=0 endpoint, the ρ=1 endpoint, and monotonicity between them, so the suite could not tell
a formula off by 10× from the right one.

## Alternatives considered
- **`N + library_size` (formula A, #770) — rejected**, and reversed. Makes a strategy's
  p-value a function of unrelated strategies; breaks reproducibility of a passport.
- **`num_trials = library_size` for the curated serving path — rejected** for the same
  reason: it charges a single-paper implementation for a search we did not run.
- **Deflate curated strategies by the paper's own reported configuration count — considered,
  not adopted.** Defensible in principle (the *authors'* search is a real multiple-testing
  event), but the count is rarely reported and would be a guess. `num_trials = 1` is the
  honest floor; the residual author-side selection bias is a known, unmodelled term.
- **Keep a portfolio-level correction inside the gate — rejected** as answering the wrong
  question at the wrong surface; the ranking-surface disclosure is recorded as option 1
  of [#1654](https://github.com/aprin-labs/archimedes/issues/1654) (2026-09-01 amendment).

## Amendment (2026-08-19): the fixture-leak class

The convention marker (Decision #4) protects verdicts computed *by the gate*. The
2026-08 test-suite/chaff audit found a failure class it does not protect against:
**surfaces that serve rigor values from the frozen strategy fixtures instead of a live
gate run.** The fixture fields (`Strategy.dsr_p_value`, `num_trials_in_selection`,
`pbo_score`, `deflated_sharpe_ratio`, `out_of_sample_sharpe`, and the
`passes_rigor_gate` boolean) are stored values the live gate never rewrites — most
predate the 2026-07-09 reversal, so a surface serving them next to a live badge
**silently mixes conventions**, exactly what the marker exists to prevent, without ever
touching the marker.

Live instance (audit finding M1, fixed in
[PR #1272](https://github.com/aprin-labs/archimedes/pull/1272)): `/api/strategies/advisor`
served five fixture statistics — including a fixture-era `num_trials_in_selection` —
with only the badge live-corrected, so its numbers could disagree with
`GET /api/selection-bias/gate` for the same strategy at the same moment. The sibling
finding (M2, same PR) was the *sentinel* variant: readers presenting the fail-closed
in-memory `passes_rigor_gate = False` (#821) to LLM prompts as if it were a verdict.

**Rule this amendment records:** a served rigor statistic or verdict must come from a
live `run_rigor_gate` computation (directly or via the honest memoized batch in
`rigor_cache` — a cache hit serves exactly what the miss would have computed). Fixture
fields may appear only as the explicitly-documented fallback when the live gate cannot
run for a strategy (#868 contract), and the fail-closed sentinel is never a verdict —
downstream consumers render "pending"/omission, not "fail". The leaderboard (#868), the
library badge (#821), the advisor, the portfolio LLM prompt, and vault chat (#1272) now
all comply; any new consumer of these fields starts from this rule.

## Amendment (2026-08-31): ratification stamp + four corrections

Ratified as recorded under "Ratification." Four corrections were folded into the sections
they touch in the same commit — at the reviewer's request, rather than appended
out-of-line — and each edit is dated where it lands:

1. **The "single largest outstanding rigor risk" framing removed** (Ratification): the
   risk ranking was wrong while #1558 sat undocumented in the same statistic for eleven
   weeks.
2. **The headline narrowed to `num_trials`** (title, TL;DR) and the live PBO cohort
   coupling named explicitly (the carve-out under the Decision list) — the gate contains
   one library-level criterion by construction, and this ADR must not be citable for a
   property the gate does not have.
3. **The Leaderboard/Marketplace "open gap" reframed** (Consequences): board-level BH FDR
   is computed and served — and, per the reviewer's post-ratification prod pull, currently
   disagrees with the per-strategy gate on every strategy (min adjusted p 0.319
   board-wide). The open item *as of this stamp* was the product decision on what the
   ranking surface says, made before the badge is leaned on publicly — not a spec, and no
   longer merely wiring. *(Closed 2026-09-01: option 1 recorded on #1654; see the
   amendment below.)*
4. **Decision #5 rewritten**: it cited the `N_eff` form that #1558 showed to be the wrong
   functional form and #1559 removed. Found during the stamp, not in the review — the
   document claimed the code did something it no longer does. (A matching stale comment in
   `selection_bias_routes.py` was corrected in the same commit.)

## Amendment (2026-09-01): #1654 records option 1 — board FDR on the ranking surface

[#1654](https://github.com/aprin-labs/archimedes/issues/1654) asked what the ranking surface
shows when board-level Benjamini–Hochberg FDR disagrees with the per-strategy gate on
every passing strategy. **Option 1 is chosen.** Surface `board_fdr_*` on the ranking
surface alongside the per-strategy badge, with the two-questions explanation: the gate
answers "is this strategy statistically sound on its own evidence?"; board FDR answers
"is this strategy distinguishable from the field's multiple testing?" They can both be
true. Board FDR is **advisory** and **never flips the badge** (`passes_all`). The
passport stays out — Decision #3: the passport carries only per-strategy information.

This is not a new call. Owner decision on
[#1564](https://github.com/aprin-labs/archimedes/issues/1564) (comment 2026-08-31) plus
[PR #1580](https://github.com/aprin-labs/archimedes/pull/1580) already moved the board FDR
block onto `GET /api/leaderboard` and rendered it in
[`ui/src/components/Leaderboard.jsx`](../../ui/src/components/Leaderboard.jsx) with the
sentence "Not yet distinguishable from selection noise at board level." An uncorrected
row is an em-dash, never a verdict. `ui/test/leaderboard-board-fdr.test.js` pins that
copy. The #1654 filing that no `ui/src/` file referenced `board_fdr` is stale against
that wiring.

**Decision recorded, wiring shipped.** Remaining work is none unless a new ranking
surface shows an unqualified badge. The BH correction is unchanged; `compute_board_level_fdr`
is not muted. No curated-library pass rate is quoted.
