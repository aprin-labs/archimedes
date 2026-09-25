---
type: routing-map
title: Quickstart — the rigor and quant layer
description: Task-routing map for the quant slice — where to go for a threshold, a strategy claim, or a measured number, what this wiki does not cover, and the claims never to make.
tags: [quickstart, routing, rigor, scope, guardrails]
verified:
  - by: openwiki/0.4.3
    at: 2026-08-31T05:55:43.566Z
sources:
  - id: openwiki-source-f88b8fdeab2b23286a6aa730
    resource: repo://docs/quant/admission-criteria.md
  - id: openwiki-source-03a78d9aa62747d43f49c7bc
    resource: repo://docs/quant/methodology.md
  - id: openwiki-source-aea1728d3a591b704463ef0e
    resource: repo://docs/quant/README.md
  - id: openwiki-source-6ce3a655f0a466a9e2cc4338
    resource: repo://docs/quant/strategy-library.md
generated: { by: "claude-code", at: "2026-08-31T05:55:43.566Z" }
---

# Quickstart — the rigor and quant layer

This wiki covers **one slice of the repository: `docs/quant/`**, the quantitative
methodology and rigor layer. The generation run could not read backend code, contracts, UI,
or any other doc tree — the read boundary in `.openwikiignore` is an allow-list that
excludes everything else.

**What that means for how you read these pages.** Every claim here is grounded in a
*document*, not in an implementation. A doc asserting a threshold is evidence that the doc
asserts it, not that the code enforces it. The slice itself says the frozen spec and the
live implementation win wherever they and a doc disagree. For any current verdict, go to
the running system.

---

## Where to go

| If you need… | Go to |
|---|---|
| A gate threshold, or whether a strategy can be promoted | [`rigor/admission-gate`](rigor/admission-gate.md) |
| The math behind DSR, PBO, CPCV, or the look-ahead audit | [`rigor/selection-bias-controls`](rigor/selection-bias-controls.md) |
| To judge whether a backtest is trustworthy | [`rigor/reading-a-backtest`](rigor/reading-a-backtest.md) |
| What a strategy is, its paper anchor, and how the implementation diverges | [`library/strategy-shelf`](library/strategy-shelf.md) |
| How weights and position sizes are produced | [`portfolio/construction-and-sizing`](portfolio/construction-and-sizing.md) |
| A measured number — PBO, cost, walk-forward, universe results | [`findings/measured-results`](findings/measured-results.md) |
| **To check whether the docs contradict each other before you quote one** | [`rigor/documented-conflicts`](rigor/documented-conflicts.md) |

**Read `documented-conflicts` first if you are about to state a threshold, a pass/fail, or a
library size.** The slice disagrees with itself in seven places, including on where the DSR
bar comes from and on how many strategies exist.

---

## The four controls, in one line each

A strategy is admitted only when **all four pass simultaneously**:

1. **Deflated Sharpe Ratio** — is the excess Sharpe positive under robust standard errors,
   and after deflating for the search that found it?
2. **Probability of Backtest Overfitting** — is the *library's selection procedure* picking
   winners that survive out-of-sample?
3. **Walk-forward out-of-sample Sharpe** — is the edge positive on held-out data, and does
   it retain at least half the in-sample edge?
4. **Look-ahead static audit** — does the strategy source touch data it could not have known
   at decision time?

Failure is rendered, not hidden: each control shows as `PASS` with its value, `FAIL` with
the value and threshold, or `MISSING` when it could not be computed.

---

## Three claims never to make

**1. Never state how many strategies pass the gate.** The live gate is the only authority,
and a number written into a document is stale the moment the library changes. Two documents
in this slice state a count anyway; both are dated 2026-06-11 and at least one of the pass
claims they rest on has since been retracted.

**2. Never describe the curated path as multiple-testing corrected.** On the curated library
`num_trials = 1`, so the expected best-of-N term is zero and **DSR runs undeflated**. The
board-level bias a user incurs picking the best of N displayed strategies is **disclosed,
not corrected** — the Benjamini–Hochberg helpers have zero non-test callers.

**3. Never write vaults or on-chain execution in the present tense.** The vault contracts
exist and are deployed, but the deploy-a-vault journey is gated off every public surface
behind a feature flag. Executing and monitoring strategies in non-custodial vaults is
**roadmap, not shipped product**.

A fourth, softer rule from the slice: say *"deflated-Sharpe evidence at the 95% one-sided
level"* — real evidence on a short return history — never "statistically proven". Quote the
bar from `rigor_profiles.DSR_P_BADGE_MIN`, the only place it is written down (#1794).

---

## What this wiki does not cover

- **The implementing code.** The rigor evaluator, the portfolio optimizer, and the rigor
  profiles table are all outside the read boundary. Every statement about what a function
  does is a statement about what a doc says it does.
- **Live status of anything.** No pass/fail, no current library size, no served values.
- **The rest of the repository** — the API surface, the generation pipeline, the contracts,
  the infrastructure. Widen the boundary one slice at a time and record what each slice
  costs, per the tooling-adoption standard this run was produced under.
