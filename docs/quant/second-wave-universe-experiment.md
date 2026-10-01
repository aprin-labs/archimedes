# Universe Experiment: does a bigger universe rescue the second-wave strategies?

> **Status:** Findings note, 2026-06-11 (Önder, quant lane). **Historical — read at its
> vintage.** Every number below was measured on **2026-06-11**, against the library as it
> stood then (22 of 23 strategies) and against the **0.95 DSR bar and the library-sized
> `num_trials` convention that were current on that date**. The bar moved down in PR #901
> and back to `0.95` in [#1794](https://github.com/aprin-labs/archimedes/issues/1794), so
> the DSR column below is once again read against the live bar; the library-sized trial
> count, however, was reversed on 2026-07-09
> ([`../adr/num-trials-self-containment.md`](../adr/num-trials-self-containment.md)). The
> experiment's *conclusion* is unaffected — it turns on Sharpe ratios, not on the gate
> threshold — but three passages were corrected in place on 2026-08-31
> ([#1598](https://github.com/aprin-labs/archimedes/issues/1598)) and are marked where they sit.
> Companion to
> [`second-wave-multi-asset-strategies.md`](../plans/second-wave-multi-asset-strategies.md).
> **TL;DR:** No. All nine second-wave strategies are admitted as `CANDIDATE`
> (none clears the rigor gate). The natural hypothesis — "they fail only because
> the demo universe is too small (5 assets)" — was tested directly and is **false**.
> Expanding and re-composing the universe does **not** flip any verdict, and in
> several cases makes performance *worse*. The strategies genuinely underperform
> on real data after costs; the gate is doing its job. One legitimate calibration
> question (the DSR `num_trials` penalty) is split out to its own issue.

## Why this note exists

A fair challenge after the second wave landed: *"these are real, heavily-cited
papers (Jegadeesh-Titman, Engle-Granger, Avellaneda-Lee, …) — why is **every**
new strategy denied? Is the validation system too harsh, or is the 5-asset demo
universe unfair to them?"*

Good question, and testable. So we re-ran the four multi-asset strategies on
larger and more appropriate universes, using the **identical** rigor machinery
(DSR / PBO / OOS / gate) so the comparison is apples-to-apples.

## First: the gate is not a black hole

> **Retracted 2026-08-31 ([#1598](https://github.com/aprin-labs/archimedes/issues/1598)).**
> This paragraph stated how many library strategies clear the rigor gate and named them with
> their DSR p-values. `CLAUDE.md` forbids quoting a curated-library pass count anywhere, and
> for a concrete reason that post-dates this note: strategies reported as passing were later
> found to be grading equity-like return series (~18.5% annual vol) that arrived through a
> data-feed fallback in the backtest loader. **The corrected pass count is unestablished.**
> The p-values quoted here were additionally computed under the `num_trials = 22`
> convention, reversed on 2026-07-09, so they are not comparable to anything the gate
> returns today. The live rigor gate is the only authority; see the PASS/CANDIDATE badges in
> the app and `backend/archimedes/services/live_rigor_gate.py`.

The claim this section needs is weaker than a count and survives the retraction intact:
**the gate discriminates — it is not rejecting everything reflexively.** The evidence for
that is below and does not depend on any pass number. The library's strategies spread across
a wide range of outcomes on the same machinery, and the question this note answers is only
why *these nine* fail.

The dominant reason is blunt: **7 of the 9 new strategies posted a *negative*
Sharpe on real 2004–2026 data** — they lost money. No validation system should
bless a money-losing backtest, regardless of how cited the source paper is. The
only two non-negative new strategies are dual momentum (+0.10, too weak) and
risk parity (+0.35, the one genuine near-miss).

## The experiment

| Universe | Members | Rationale |
|---|---|---|
| **Original 5** | SPY, ^N225, GC=F, TLT, CL=F | the as-shipped demo universe (cross-asset, mixed markets) |
| **Diversified 10 ETF** | SPY, QQQ, IWM, EFA, EEM, TLT, IEF, GLD, XLE, XLF | bigger, US-listed (synchronous closes) |
| **9 sector ETFs** | XLB, XLE, XLF, XLI, XLK, XLP, XLU, XLV, XLY | a *homogeneous cross-section* — the fair test for ranking momentum / PCA |
| **8 asset classes** | SPY, EFA, EEM, TLT, IEF, GLD, DBC, VNQ | diversified low-correlation classes — the fair test for risk parity |
| **GEM 5** | SPY, EFA, EEM, TLT, GLD | relative + absolute momentum with a bond defensive leg |

All runs: 2004–2026, 10 bps transaction costs, same engine (`run_multi_backtest`),
same gate.

### Results (Sharpe ratio; **none** clears the gate in any column)

| Strategy | Original 5 | Diversified 10 | Sector 9 | Asset-class 8 | GEM 5 |
|---|---|---|---|---|---|
| Cross-sectional momentum | −0.21 | −0.24 | **−0.89** | — | — |
| Dual momentum | +0.10 | +0.14 | — | — | +0.15 |
| Risk parity | **+0.35** | +0.10 | — | −0.09 | — |
| PCA stat-arb | −0.33 | −1.22 | −1.53 | — | — |

## What this tells us

1. **Universe size is not the binding constraint.** Bigger did not help; the
   diversified-10 and sector-9 sets left every verdict unchanged or worse.
2. **Composition matters far more than count.** Risk parity's best result is on
   the *original 5* (+0.35), because that set is genuinely low-correlation
   (equities + gold + bonds + oil). The "bigger" 10-ETF set is mostly equities,
   so it is *less* diversified in risk terms and risk parity does worse (+0.10).
   Adding broad commodities (DBC) in the 8-asset set dragged it negative.
3. **Sector-rotation momentum has decayed.** On the textbook homogeneous sector
   cross-section it is the *worst* (−0.89): post-2009 momentum crashes plus
   monthly-rebalance costs. Consistent with McLean & Pontiff (2016) on
   post-publication factor decay.
4. **PCA needs scale we don't have.** Even on 9–10 names it over-trades
   (1700–1800 round-trips) and the factor hedge can't neutralize idiosyncratic
   risk at this scale → large drawdowns. AL's method is built for *hundreds* of
   names.
5. **The papers are not "wrong."** They report results at their intended scale
   (broad stock cross-sections, diversified pair portfolios, hundreds of names).
   Our small/medium-universe adaptations are honest demos, not those designs —
   "from a cited paper" and "survives DSR/PBO on this universe today after costs"
   are different claims. Surfacing that gap is the product (rigor-as-wedge).

**Conclusion:** keep the strategies as honest `CANDIDATE`s. Do **not** force a
larger universe onto them — for risk parity that would *degrade* the best
configuration we have. The expanded symbol set added to
`instruments.OPERATION_TO_SYMBOL` is useful infrastructure for *future* strategies
(and for these experiments), not a reason to rewrite the shipped ones.

## The one calibration caveat (own issue)

The single near-miss — risk parity on the original 5 assets (Sharpe +0.35,
max-DD 27%) — fails the DSR significance bar *only* because of how conservatively
we set `num_trials_in_selection`. Sweeping it on that strategy's real returns:

| `num_trials` | DSR p-value | vs. the 0.95 bar (live, and the bar of 2026-06-11) | vs. the lower bar PR #901 briefly used |
|---|---|---|---|
| 1–13 | 0.999 → 0.963 | **yes** | **yes** |
| 22 (full library) | 0.941 | no | **yes** |
| 50 | 0.896 | no | no |

> **Corrected 2026-08-31 ([#1598](https://github.com/aprin-labs/archimedes/issues/1598)).**
> The table shipped with a single pass/fail column computed against the 0.95 bar, and its
> verdicts went stale twice over. Both bars are kept side by side rather than the old one
> being overwritten: the p-values are the measurement and have not changed, but **what they
> clear has**. The `num_trials = 22` row, marked "no" at 0.941, cleared the lower bar PR
> #901 briefly used — the conclusion this section drew from that row did not survive it.
> **Updated 2026-09-03 (#1794):** the bar is 0.95 again and is now a single named constant,
> so the left-hand column is once more the live one and that row is "no" again. The
> whipsaw is precisely why the number is no longer written down in more than one place.
>
> **And the sweep itself is now moot for this strategy.** It asks what happens as the
> library-sized trial count varies; that convention was reversed on 2026-07-09 and
> `maillard_2010_risk_parity` is a curated single-paper implementation, so it is graded at
> `num_trials = 1` — the top row — with no deflation at all
> ([`../adr/num-trials-self-containment.md`](../adr/num-trials-self-containment.md),
> ratified 2026-08-31). Whether it clears the bar *today* is a question for the live gate on
> current data, not for this table: these p-values are 2026-06-11 measurements and the
> vintage-drift caveat in [`third-wave-retest.md`](third-wave-retest.md) applies.

**What the note argued at the time, preserved:** we then set `num_trials` = the full
library size (22), which penalized each strategy as if it were cherry-picked as the best of
22 independent trials. For an *individually-specified, paper-grounded* strategy that was
**not** data-mined from the library, that was argued to be too harsh — a genuine design
question about the gate's calibration, written up separately for a team decision rather
than changed unilaterally. **That decision was subsequently taken this way**
([#537](https://github.com/aprin-labs/archimedes/issues/537) → the self-containment ADR): the
library size no longer enters `num_trials` on any path. Note it was **not** what blocked
the other eight: they fail on performance, not on the penalty.
