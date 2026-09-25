---
type: known-issues
title: Conflicts inside the quant docs
description: Seven places where the quant documentation contradicts itself — stale thresholds, headings that disagree with the status lines beneath them, pass counts the same slice forbids quoting, and an out-of-sample Sharpe compared against a probability threshold.
tags: [documentation-drift, rigor-gate, dsr, contradictions, verification]
verified:
  - by: openwiki/0.4.3
    at: 2026-08-31T05:55:43.566Z
sources:
  - id: openwiki-source-f88b8fdeab2b23286a6aa730
    resource: repo://docs/quant/admission-criteria.md
  - id: openwiki-source-bcc51b9a099705e0822aa4c7
    resource: repo://docs/quant/backtest-interpretation.md
  - id: openwiki-source-7bbe81a7a83756c8ac62dc6e
    resource: repo://docs/quant/library-pbo.md
  - id: openwiki-source-03a78d9aa62747d43f49c7bc
    resource: repo://docs/quant/methodology.md
  - id: openwiki-source-aea1728d3a591b704463ef0e
    resource: repo://docs/quant/README.md
  - id: openwiki-source-0f74af6fbdba9344305572f1
    resource: repo://docs/quant/second-wave-universe-experiment.md
  - id: openwiki-source-6ce3a655f0a466a9e2cc4338
    resource: repo://docs/quant/strategy-library.md
  - id: openwiki-source-0dce6a8f1e2a58d1ccdb4aad
    resource: repo://docs/quant/third-wave-retest.md
generated: { by: "claude-code", at: "2026-08-31T05:55:43.566Z" }
---

# Conflicts inside the quant docs

> **STALE ON ONE NUMBER — regenerate.** This page was generated 2026-08-31 and is the
> *evidence record* of what that run found, so it deliberately reproduces the strings the
> quant-docs guards forbid. On 2026-09-03, [#1794](https://github.com/aprin-labs/archimedes/issues/1794)
> retired the lower DSR bar: the badge bar is **0.95**, defined once as `DSR_P_BADGE_MIN` in
> `backend/archimedes/services/rigor_profiles.py`. Every `0.90` below is the retired bar,
> and conflicts 1 and 3 — both of which turn on which bar is live — were resolved by that
> call. The body is left exactly as generated, so the `generated:` provenance above stays
> true and the record of what the run saw is not rewritten after the fact. The next openwiki
> run supersedes this page.

> ## ⚠ RESOLVED 2026-08-31 — hand-annotated, not regenerated
>
> *This block is a human edit to a generated page, added by
> [#1598](https://github.com/aprin-labs/archimedes/issues/1598). Everything below it is the
> generator's output, preserved verbatim as the evidence record of what the 2026-08-31
> 05:55 run found.*
>
> **All seven conflicts below have been reconciled in `docs/quant/`.** Each was resolved
> against the live code and the ratified ADR
> [`num-trials-self-containment.md`](../../docs/adr/num-trials-self-containment.md) — the
> source-of-truth order is **live code > ratified ADR > newer doc** — and each fix is
> annotated and dated where it lands in the source doc. In summary:
>
> | # | Resolution |
> |---|---|
> | 1 | **Methodology was right.** `passes_all` reads a `rigor_profiles._PROFILES` row, never a literal; 0.90 is the level-1 (Conservative) `dsr_p_min` and level 1 *is* the badge bar. `admission-criteria.md`'s table is relabelled as that rung and the always-on floors are separated from the adjustable ones. |
> | 2 | **`num_trials = 1` on the curated path** — hard-coded at `selection_bias_routes.py:419`, generated path uses the debate pool size. `len(strategy_library)` was the #770 convention, reversed 2026-07-09. The promotion flow is corrected. |
> | 3 | The sweep table now shows **both bars side by side**; the `num_trials = 22` row at p = 0.941 clears 0.90. The sweep is additionally moot for a curated strategy graded at `num_trials = 1`. |
> | 4 | The three ✅/❌ headings are **removed**; the stranded Faber sentence is gone from the Moreira–Muir entry; a standing "a heading is not a verdict" rule was added. |
> | 5 | **0.612 is a DSR p-value, not an OOS Sharpe.** Faber's OOS Sharpe on the same pull is 0.930, which clears both OOS thresholds (`> 0`, `OOS/IS ≥ 0.5`). The verdict stands; the reason was misattributed. |
> | 6 | The two findings notes are **vintage-stamped** as 2026-06-11 / 22-of-23 measurements; the shelf's live-count instruction stands and is now the only count. |
> | 7 | Both pass counts **retracted in place** — `CLAUDE.md`'s hard rule; the corrected count is **unestablished**. |
>
> **The machine claims file `openwiki/.claims/rigor/documented-conflicts.json` was NOT
> updated and is now stale.** Its evidence anchors are content-addressed
> (`repo-lines-v1:sha256:…`) over line ranges of the very files this PR changed, and its
> `pageVersion` is a sha256 of this page. Hand-editing those hashes would produce a file
> that *looks* verified and is not — worse than a stale one. It needs an OpenWiki
> regeneration run, tracked as follow-up on #1598.
>
> **The "not resolvable from docs" note below is now answered, and its lesson is the
> durable one:** items 1, 2 and 5 needed the implementing code, which the run's
> `.openwikiignore` allow-list deliberately excluded. A doc-only slice can find that two
> documents disagree; it cannot find that both are wrong. See Önder's eighth conflict on
> #1598 — a stale `N_eff` formula that every doc in the slice reproduced *consistently*,
> and which only a check against the code could catch (fixed separately in
> [#1614](https://github.com/aprin-labs/archimedes/pull/1614)).

This page exists so a reader does not resolve a contradiction by trusting whichever page
they happened to open first. Everything below is a disagreement **inside this slice**,
found by reading all eight documents together.

None of these is resolved here. Two of them cannot be resolved from documentation at all —
they need the running system or the implementing code, both of which sit outside the
boundary this wiki was generated within.

---

## 1. Is 0.90 a literal threshold or the top rung of a ladder?

**The disagreement.** The admission document states the DSR bar as a literal gate value:
`dsr_p_value ≥ 0.90`, recalibrated from 0.95 in PR #901, and presents the four thresholds
as "transcribed from `passes_all`, not invented". The methodology document says something
structurally different: 0.90 is the threshold **at the strictest level (1, Conservative —
the badge bar)**, loosening down a five-level strictness ladder to a floor at level 5, with
the live values living in a rigor-profiles table, and it states that the gate "reads the
selected profile, never a literal".

**Why it matters.** These are not two phrasings of one rule. One says there is a single
number; the other says the number depends on which profile is selected. An agent that reads
only the admission page will assert a fixed 0.90 bar for every evaluation.

**Not resolvable from docs.** The profiles table is source code outside this slice.

## 2. The same document gives `num_trials` two different values

**The disagreement.** Within the admission document, the threshold section says that on the
curated library `num_trials = 1`, so no deflation is applied. The promotion-flow section
directly below says the caller passes `num_trials = len(strategy_library)`.

**Why it matters.** This is the difference between a DSR that is multiple-testing corrected
and one that is not — the single most load-bearing caveat in the whole rigor story. Both
statements appear on one page, roughly a hundred lines apart.

The likely reconciliation is that the two describe different paths — generated versus
curated — but neither passage says so, and the promotion flow is written as the general
case.

## 3. A stale `p ≥ 0.95` bar survives in the universe experiment

**The disagreement.** Two documents carry dated corrections noting that a `≥ 0.95` figure
was wrong and that 0.90 has been the bar since PR #901. The universe experiment's
`num_trials` sweep table was not corrected: its column header still asks **"passes
p ≥ 0.95?"**, and its yes/no answers are computed against that bar.

**Why it matters.** Read against the current 0.90 bar, that table's verdicts change. The row
at `num_trials = 22` shows p = 0.941 marked "no" — but 0.941 clears 0.90.

## 4. Three headings contradict the status lines beneath them

The strategy shelf marks pass/fail in section headings while its status lines defer to the
live gate:

| Strategy | Heading says | Status line says |
|---|---|---|
| Moskowitz, Ooi & Pedersen (2012) TSMOM | ✅ *passes the gate* | "per the live rigor gate" |
| Moreira & Muir (2017) volatility-managed | ✅ *passes the gate* | `CANDIDATE` — **fails admission** |
| Faber (2007) SMA-200 timing | ❌ *fails the gate* | "per the live rigor gate" |

The Moreira & Muir row is the sharpest: a heading claiming a pass sits directly above a
status line claiming failure. That status line then continues "The earlier claim that Faber
passed was wrong" — Faber text stranded under a different strategy, which is the signature
of a partially-applied edit.

**Why it matters.** The same document states twice that which strategies pass "is not
recorded here" because the live gate is the only authority. The headings break that rule,
and a reader skimming headings gets the opposite answer from a reader reading status lines.

## 5. An out-of-sample Sharpe is compared against a probability threshold

**The disagreement.** Two passages say a strategy fails because "its walk-forward OOS Sharpe
ratio is 0.612, under the 0.90 gate".

**Why this cannot be right as written.** 0.90 is the **DSR p-value** threshold — a
probability in `[0, 1]`. The out-of-sample Sharpe has two thresholds of its own, and neither
is 0.90: an absolute floor of `> 0` and a cliff ratio of `OOS/IS ≥ 0.5`. An OOS Sharpe of
0.612 clears the floor comfortably. Whatever the real failure reason is, the sentence as
written compares a Sharpe ratio to a probability.

**Why it matters.** It is the only failure explanation the shelf gives, it appears twice,
and it teaches a wrong mental model of which threshold governs which statistic.

## 6. Three different library sizes, none reconciled

| Document | Count |
|---|---|
| Strategy shelf | 34 strategy files "at the time of writing" |
| Library-PBO findings | 22 of 23 strategies |
| Universe experiment | "the full 22-strategy library" |

The two findings notes agree with each other and were both run on 2026-06-11; the shelf is a
later, larger count. Nothing in the slice says the findings numbers were computed on a
smaller library than the one now shipped, which is exactly what a reader needs to know
before quoting a PBO figure as current.

**Partially self-defending.** The shelf tells you to count the files yourself rather than
trust its number — good practice, and the only such instruction in the slice.

## 7. Pass counts that the same slice forbids quoting

Three documents state the rule that the live gate is the only authority on pass/fail. Two
others state a count anyway:

- The universe experiment: "Of the full 22-strategy library, **two pass** the rigor gate
  today" — naming both, with DSR p-values of 0.995 and 0.976, and a third within 0.006.
- The third-wave re-test refers to "the two gate-passers" and to Faber having LIVE status.

Both are dated 2026-06-11 and were true observations at that vintage. The rule they violate
was written to stop exactly this figure from being carried forward as current, and the
shelf has since retracted at least one of the pass claims those notes rest on.

---

## One non-conflict worth knowing

The methodology document retracts one of its own claims rather than contradicting another
page: the flat 5% risk-free constant is now only a fallback, and because Kelly and the
portfolio optimizer still take the flat constant while the rigor statistics can be graded
against the historical T-bill series, **"every excess-return figure is computed on the same
basis" is stated to be no longer accurate**. That is a documented non-uniformity, not a
drift — it is worth carrying alongside these conflicts because it has the same practical
effect: two numbers that look comparable may not be.

---

## How to use this page

- **Never resolve one of these by picking a document.** For anything about a current
  verdict, threshold selection, or pass count, the live gate and the implementing code are
  the authority.
- Items 1, 2, and 5 need someone with access to the implementation to settle.
- Items 3, 4, 6, and 7 are documentation fixes: a stale table header, three headings, a
  count reconciliation, and two dated pass counts that need vintage stamps or removal.

An index gap sits alongside these: the slice's own README describes itself as "these four
docs" and tables four of them, while the directory holds eight. The three findings notes —
the ones carrying the numbers most likely to be quoted — are not in that table.
