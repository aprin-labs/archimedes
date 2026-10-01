---
type: findings-summary
title: Measured results and their vintage
description: The three 2026-06-11 quant findings notes — library-level PBO, the cost-and-walk-forward re-test, and the universe experiment — with each headline number tied to its data vintage and the caveat that bounds it.
tags: [rigor, pbo, backtest, walk-forward, findings, data-vintage]
verified:
  - by: openwiki/0.4.3
    at: 2026-08-31T05:55:43.566Z
sources:
  - id: openwiki-source-7bbe81a7a83756c8ac62dc6e
    resource: repo://docs/quant/library-pbo.md
  - id: openwiki-source-0f74af6fbdba9344305572f1
    resource: repo://docs/quant/second-wave-universe-experiment.md
  - id: openwiki-source-0dce6a8f1e2a58d1ccdb4aad
    resource: repo://docs/quant/third-wave-retest.md
generated: { by: "claude-code", at: "2026-08-31T05:55:43.566Z" }
---

# Measured results and their vintage

> **STALE ON ONE NUMBER — regenerate.** This page was generated 2026-08-31 from the quant
> slice as it then stood. On 2026-09-03, [#1794](https://github.com/aprin-labs/archimedes/issues/1794) retired
> the lower DSR bar: the badge bar is **0.95**, defined once as `DSR_P_BADGE_MIN` in
> `backend/archimedes/services/rigor_profiles.py`. The measured numbers below are unaffected
> — they are diagnostics with a stamped vintage, not gate verdicts — but the calibration
> caveat's sentence about which bar is current asserted the *inverse* of the truth and was
> corrected in place; the rest of the body is as generated, so the `generated:` provenance
> above stays true. The next openwiki run supersedes this page.

Three findings notes in this slice report numbers that were actually computed, not
asserted. All three were run on **2026-06-11** against then-current market data. Every
number below is a *diagnostic from that run*, not a served value: none of them was
written back into `backtest_fixtures.json`, and none of them moved a gate verdict.

Read this page for what was measured. Read
[`admission-gate`](../rigor/admission-gate.md) for what the thresholds are, and
[`documented-conflicts`](../rigor/documented-conflicts.md) before quoting any of these
numbers as current — two of the three notes carry figures that later pages in the same
slice retract.

---

## 1. Library-level PBO = 0.047

The headline of the fourth-wave measurement: **library-level PBO (Bailey et al. 2014
CSCV) over 22 strategies = 0.047** — six-decimal value `0.046698`.

| Setting | Value |
|---|---|
| Selection set | 22 of the 23 library strategies |
| Joint window after calendar alignment | 2006-05-22 → 2026-04-30, 4709 trading days |
| Partitions `S` / splits `C(S, S/2)` | 16 / 12,870 |
| PBO | 0.046698 |
| Sensitivity across `S` = 8 / 12 / 16 | 0.043 / 0.022 / 0.047 |

In roughly 4.7% of the 12,870 combinatorial IS/OOS splits, the in-sample-best strategy
falls below the out-of-sample median. Against the `PBO < 0.5` gate that is a wide pass.

### Why a low PBO is not good news about the strategies

The note is explicit that this is the most misreadable number in the slice. **A low PBO
does not say the strategies are good — it says the ranking is stable.** Two-thirds of
the library has negative Sharpe on the joint window, and CSCV sees those failures lose
*consistently*, in-sample and out-of-sample alike, so the in-sample winner is almost
never a fluke that collapses out-of-sample. An overfit library — random strategies mined
until something sticks — would sit near PBO ≈ 0.5. This library fails honestly and
persistently, which CSCV correctly reads as "selection here is not the problem."

### Caveats that bound the number

- **Coverage is 22 of 23.** `capital_preservation_tbill` is excluded because its fixture
  models a synthetic T-bill yield, not a tradeable instrument run — the same exclusion
  the third-wave re-test makes.
- **Alignment costs history.** The joint window starts 2006-05-22 at the pair
  inceptions; roughly 900 days of 2004–2006 history is dropped for the strategies that
  have it. CSCV requires simultaneity, and this is the stated price.
- **Granularity.** With `N = 22` the OOS rank quantile takes only 22 discrete values.
  The note says to quote the **S-sensitivity range 0.022–0.047 as the error bar**, not
  the headline's six decimals.
- **Vintage drift is why this is add-only.** The legacy strategies' fixture-era series
  cannot be reproduced on current data; each daily-returns store file is stamped
  `data_vintage: 2026-06-11`.

The implementation is a deliberate mirror of the backend gate's `compute_pbo`, and a
parity test asserts exact-equal outputs so the two cannot drift silently.

### What is still undecided

Whether passports should surface the library-level PBO at all — as a `library_pbo` field
with its vintage, and on what refresh cadence — is recorded as an **open team decision**.
Because CSCV PBO is a property of the selection set, every library addition changes it,
which suits a value computed at evaluation time better than a frozen per-strategy
fixture entry. The served per-strategy PBO values remain the cohort-level ones.

---

## 2. The candidates fail on absent alpha, not on execution cost

The third-wave re-test pushed the candidates through a turnover-aware cost model and a
walk-forward parameter selector, at 10 bps per side on fresh 2004→2026 data, to separate
two hypotheses.

**Part A — the failures are alpha-absent, not cost-bled.** Every negative-net candidate
is *also negative gross*. Even at zero cost RSI-2 sits at −0.33, the pairs family at
−0.28 to −0.84, and the portfolio-of-pairs at −1.37. The "Kalman hypothesis" — that
execution cost was the binding constraint — is rejected outright: Kalman improves from
−1.47 to −0.75 gross (costs genuinely destroy 4.6%/yr at 22.9× turnover), and the
costless version still fails decisively. **No candidate's verdict flips even with free
execution.**

**Break-even cost is the cleanest single screen the re-test produced.** Survivors and
near-misses have wide implementability headroom — risk parity 1221 bps, Brock dual-MA
808, Moreira–Muir 361, TSMOM 214, dual momentum 116 — while the failures sit at ≤ 50 bps,
many near zero, meaning institutional-grade execution could not make them investable.

**Turnover sees what trade count cannot.** Moreira–Muir reports **0 closed trades** —
it holds one continuously-resized position, which backtrader's `TradeAnalyzer` never
counts as a round trip — yet turns over 1.07× equity per year and pays a real 0.21%/yr.
For resize-style strategies the `total_trades` field materially understates activity;
turnover is the honest activity metric.

**Part B — honest parameter selection rescues nothing.** Walk-forward over a trailing
1008-bar train and a 252-bar test, stitched:

| strategy | combos | folds | WF OOS Sharpe | default full-sample Sharpe |
|---|---|---|---|---|
| `faber_2007_sma200_timing` | 4 | 18 | +0.05 | +0.13 |
| `donchian_breakout` | 9 | 18 | +0.05 | −0.12 |
| `connors_alvarez_2009_rsi2` | 3 | 18 | −0.29 | −0.58 |
| `bollinger_2001_band_reversion` | 6 | 18 | −0.16 | −0.15 |
| `brock_1992_dual_ma_crossover` | 9 | 18 | +0.17 | +0.21 |
| `maillard_2010_risk_parity` | 3 | 16 | +0.14 | +0.35 |

No stitched OOS Sharpe comes near the gate. Donchian flips sign and RSI-2 halves its
losses, but "less bad" is not investable.

**Parameter instability is itself the overfitting signature.** The modal parameter choice
wins only about half the folds for most strategies (Faber 8/18, Bollinger 8/18, Brock
8/18) — the best parameter is regime-dependent, exactly the condition under which
in-sample selection overfits and PBO is the right alarm. RSI-2 is the stable exception
at 16/18 and still loses money.

**Risk parity's +0.35 is tempered honestly.** Walk-forward over lookback ∈ {42, 63, 126}
delivers +0.14 OOS against +0.35 for the fixed default of 63. The default was not mined —
it is the three-month convention the literature uses — but the +0.35 does benefit from
configuration luck an adaptive selector would not have captured.

---

## 3. A bigger universe does not rescue the second wave

The universe experiment tested the fair challenge that heavily-cited papers were failing
only because the shipped demo universe is five assets. It re-ran four multi-asset
strategies on larger and more appropriate universes through the identical rigor
machinery. **The answer is no**, and in several cases bigger is worse.

| Strategy | Original 5 | Diversified 10 | Sector 9 | Asset-class 8 | GEM 5 |
|---|---|---|---|---|---|
| Cross-sectional momentum | −0.21 | −0.24 | **−0.89** | — | — |
| Dual momentum | +0.10 | +0.14 | — | — | +0.15 |
| Risk parity | **+0.35** | +0.10 | — | −0.09 | — |
| PCA stat-arb | −0.33 | −1.22 | −1.53 | — | — |

None clears the gate in any column. Four conclusions the note draws:

- **Composition matters more than count.** Risk parity's best result is on the original
  five — genuinely low-correlation across equities, gold, bonds, and oil. The "bigger"
  ten-ETF set is mostly equities, so it is *less* diversified in risk terms; adding broad
  commodities dragged it negative.
- **Sector-rotation momentum has decayed.** On the textbook homogeneous sector
  cross-section it is worst at −0.89, consistent with McLean & Pontiff (2016) on
  post-publication factor decay.
- **PCA stat-arb needs scale this universe does not have.** At 9–10 names it over-trades
  (1700–1800 round trips) and the factor hedge cannot neutralise idiosyncratic risk;
  the method is built for hundreds of names.
- **The papers are not wrong.** They report results at their intended scale. The
  small-universe adaptations are honest demos, not those designs — "from a cited paper"
  and "survives DSR/PBO on this universe today after costs" are different claims.

The operative recommendation is to keep the strategies as honest candidates and *not*
force a larger universe onto them, since for risk parity that would degrade the best
configuration available.

### The one calibration caveat

The single near-miss — risk parity on the original five, Sharpe +0.35, max drawdown 27% —
fails the DSR bar only because of how conservatively `num_trials_in_selection` is set.
Sweeping it on that strategy's real returns gives DSR p-values of 0.999 → 0.963 for
`num_trials` 1–13, 0.941 at 22 (the full library), and 0.896 at 50.

Two warnings before reusing that table. First, its column header asks whether the value
passes **p ≥ 0.95** — which, after #1794, is once again the live badge bar
(`rigor_profiles.DSR_P_BADGE_MIN`), so the header needs no adjustment; the intermediate
lower bar it was written against is retired. Second, the note is emphatic that this
calibration question is *not* what blocks the other eight strategies — they fail on
performance, not on the penalty.

---

## How to use these numbers

- They are **diagnostics with a stamped vintage**, not the live gate. Current pass/fail
  comes from the live rigor gate, never from this page.
- Quote the PBO **range**, not the six-decimal headline.
- Do not read a low library PBO as evidence the strategies are good.
- Two of the three notes quote a strategy pass count or a `p ≥ 0.95` bar that later
  documents in the same slice retract. Check
  [`documented-conflicts`](../rigor/documented-conflicts.md) first.
