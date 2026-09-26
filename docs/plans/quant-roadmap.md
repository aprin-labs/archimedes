# Quant Roadmap — from strategy count to rigor as the moat

> **Status:** Living roadmap for the portfolio-math / backtest-rigor lane (Önder),
> written 2026-06-11 after the second-wave build + universe experiment. Read with
> [`second-wave-multi-asset-strategies.md`](second-wave-multi-asset-strategies.md),
> [`second-wave-universe-experiment.md`](../quant/second-wave-universe-experiment.md), and
> the four-primitive admission gate in
> [`selection-bias-corrections-spec.md`](../specs/selection-bias-corrections-spec.md).
>
> **One-line thesis:** strategy *count* is now a vanity metric. The scarce,
> valuable things are (1) strategies that survive the rigor gate *honestly* and
> (2) verifiable proof that the rigor is real. Both are quant-lane work, and both
> are worth more than strategy #23.

## What the second wave taught us (the pivot)

The library was at 22 strategies when this was written; for the count today ask the tree
(`ls analytics-engine/strategies/*.py | wc -l`), not this doc. The two entries then read as
clearing the gate were `moreira_muir_2017_volatility_managed` and
`moskowitz_ooi_pedersen_2012_tsmom`, and all nine second-wave additions were `CANDIDATE`.
**Both of those readings predate the data-feed-fallback finding, so the pass count is
unestablished — do not quote a number here or anywhere else** ([`CLAUDE.md`](../../CLAUDE.md)
§ the hard constraint). The argument below does not depend on the count.
We then tested the obvious hypothesis — "they fail only because the demo universe
is too small" — directly, on larger and strategy-appropriate universes, and it is
**false**: bigger universes do not flip any verdict and often make things worse
(see the experiment doc). Seven of the nine simply have negative Sharpe after costs.

Three conclusions drive this roadmap:

1. **Adding single-instance, toy-scale implementations of cited papers is a dead
   end.** They fail honestly, which is correct, but it is not progress.
2. **The gate is robust, not punitive** — it passes 2 and lets a 3rd come within
   0.006. The `CANDIDATE`s are *receipts that the gate is real*, not embarrassments.
3. **The binding constraint is fidelity + cost realism + provenance, not count.**

## Guiding principle

> Optimize for *trustworthy* strategies and *verifiable* rigor, not for the size
> of the shelf. Every roadmap item below either (a) helps a strategy pass the gate
> *honestly*, or (b) makes the rigor itself provable to an outsider. Anything that
> only grows the count is deprioritized.

---

## Priority 1 — Close the calibration loop (now)

**Decide [#537](https://github.com/aprin-labs/archimedes/issues/537): what `num_trials`
should the DSR penalty use for individually-specified strategies?**

Today `num_trials_in_selection` = full library size (22), applied to every strategy
— i.e. each is penalized as if cherry-picked as the best of 22 trials. For a
paper-grounded strategy that was *not* data-mined, that is too harsh, and it is the
*only* thing blocking our best second-wave strategy (risk parity: Sharpe +0.35,
max-DD 27%, which clears the DSR bar at `num_trials ≤ 13`).

- **Recommendation:** provenance-based split — paper-grounded/curated strategies
  penalized by *variants actually tried*; fusion / library-selected strategies by
  the full count. Theoretically correct per López de Prado, and recovers one
  VALIDATED strategy without touching any threshold.
- **Owner/decision:** quant lane proposes; Dan signs off (shared `rigor_evaluator`).
- **Anti-goal:** do **not** lower the DSR p ≥ 0.95 bar — this is about the *N input*,
  not the bar.

## Priority 2 — Fidelity over fidelity-theater (the alpha question)

This attacks the root cause of "why does everything fail." Squarely backtest-math lane.

1. **Faithful-scale replication.** Implement papers at the scale they intend, not
   the demo scale:
   - Gatev 2006 as a *diversified portfolio of N pairs*, not one pair.
   - Jegadeesh-Titman as a *broad stock cross-section*, not 5 ETFs.
   - Avellaneda-Lee PCA on *dozens-to-hundreds* of names (its drawdown problem at
     N=5 is a scale artifact, documented on the passport).
   A few faithful replications will settle whether the alpha is real or decayed —
   and some may genuinely pass the gate.
2. **Shared transaction-cost + turnover model.** The Kalman blow-up (1174 trades,
   cost-bled to −1.47 Sharpe) showed execution realism is decisive. A reusable
   cost/turnover model + a turnover penalty, applied by every strategy, is the
   single highest-leverage backtest-engine addition. (Seed issue below.)
3. **Walk-forward parameter selection.** The gate checks OOS Sharpe; a proper
   walk-forward harness (parameters chosen out-of-sample, rolling) makes any future
   "pass" credible rather than lucky, and is the most defensible thing to build in
   this lane. It also directly feeds the PBO story.

## Priority 3 — Turn the gate into the product (the moat)

The differentiator vs every other AI-portfolio project is not the strategies — it
is that our rigor is *enforced and verifiable*.

1. **Risk pricing → actual allocation.** We compute Kelly fractions but do not yet
   size vault allocations from them. Fractional-Kelly sizing driven by the rigor
   metrics (and capped by risk profile) is core portfolio math and currently a gap.
2. **Anchor gate verdicts on-chain.** Hash the DSR/PBO/OOS verdict into the
   `ReasoningTraceRegistry` so "this strategy was honestly gated at time T" is
   verifiable by anyone, not merely asserted. Coordinate with Chuan (contract lane).
3. **Library-level PBO.** PBO is cohort-only today because fixtures store summary
   metrics, not return series. A daily-returns store unlocks true full-library
   overfit measurement (the honest, intended Bailey-et-al. CSCV) and is a known
   limitation in the current code.

---

## Explicit non-goals

- **Do not delete the `CANDIDATE`s.** They are proof the gate works — assets, not debt.
- **Do not add more single-instance toy strategies.** Fidelity > count.
- **Do not loosen the gate** to make the library look healthier. The honesty *is*
  the product (rigor-as-wedge).
- **Do not force a larger universe onto the shipped strategies** — for risk parity
  it degrades the best configuration we have (see the universe experiment).

## Sequencing

1. **#537 calibration decision** (recovers risk parity honestly) — now.
2. **Transaction-cost + turnover model** (reusable; unblocks credible re-tests).
3. **One faithful-scale replication** (e.g. Gatev portfolio-of-pairs) — proves the
   fidelity thesis end-to-end.
4. **Kelly-sized allocation** wired into vault construction.
5. **Walk-forward harness** + **library-level PBO** (the credibility upgrades).
6. **On-chain verdict anchoring** (with Chuan) — the verifiability capstone.

## Seed issues to file from this roadmap

- *Transaction-cost & turnover model for the backtest engine* (Priority 2.2).
- *Faithful-scale Gatev portfolio-of-pairs* (Priority 2.1) — the cleanest first
  fidelity replication.
- (#537 already filed for Priority 1.)
