---
type: reference-catalog
title: The strategy shelf
description: The curated strategy library organised by sleeve, with each strategy's paper anchor, regime tag, and the v1 adaptation caveat that separates the paper's design from what is actually implemented.
tags: [strategy-library, sleeves, paper-anchor, regime-tag, adaptation-caveat]
verified:
  - by: openwiki/0.4.3
    at: 2026-08-31T05:55:43.566Z
sources:
  - id: openwiki-source-6ce3a655f0a466a9e2cc4338
    resource: repo://docs/quant/strategy-library.md
generated: { by: "claude-code", at: "2026-08-31T05:55:43.566Z" }
---

# The strategy shelf

The library is a set of strategy files, each carrying an academic or practitioner
anchor, a `REGIME_TAG` of `bull` / `bear` / `regime_neutral`, and — the part that matters
most — an honest statement of how the implementation diverges from the source paper.
The slice documents 34 strategy files at the time of writing and tells you to count them
yourself rather than trust the number.

**The source of truth is the strategy files, not this page and not the doc it summarises.**
The paper, author, and regime fields are transcribed from each file's `PAPER_*` and
`REGIME_TAG` constants; where a file's header documents an adaptation caveat, the caveat
is reproduced downstream.

**A paper anchor is necessary, not sufficient.** Admission to Tier 1 requires passing the
four-control gate described in [`admission-gate`](../rigor/admission-gate.md). Which
strategies currently pass is reported by the live rigor gate, never by a document — see
[`documented-conflicts`](../rigor/documented-conflicts.md), because this slice contains
stale pass markers that contradict their own status lines.

---

## The sleeves

### Momentum and trend-following

Momentum captures under-reaction and herding: winners keep winning over intermediate
horizons. Trend strategies are **bull-biased by construction** and carry crash risk at
trend reversals.

| Strategy | Anchor | Regime | The v1 divergence |
|---|---|---|---|
| Jegadeesh & Titman (1993) cross-sectional momentum | *Journal of Finance* 1993 | `bull` | The original is a cross-sectional long–short ranking; verify the live universe and ranking horizon against the paper. |
| Moskowitz, Ooi & Pedersen (2012) TSMOM | *JFE* 2012 | `bull` | The paper's diversified portfolio takes long **and** short positions across many assets; the implementation is long/flat on a narrower universe. |
| Antonacci (2014) dual momentum | SSRN 2042750 / JPM | `bull` | Relative momentum plus an absolute-momentum defensive switch to cash or bonds. |
| George & Hwang (2004) 52-week-high proximity | *Journal of Finance* 2004 | `bull` | The original is cross-sectional; the single-name proxy strips the cross-sectional component, so directional alpha is **lower than the paper claims**. |
| Appel (1979) MACD crossover | anchored on Brock, Lakonishok & LeBaron (1992) | `regime_neutral` | MACD has no peer-reviewed origin paper. Sullivan–Timmermann–White (1999) data-snooping caveat applies. |
| Brock, Lakonishok & LeBaron (1992) dual-MA crossover | *Journal of Finance* 47(5) | `bull` | The paper reports **conditional mean returns and t-statistics, not a tradeable Sharpe or CAGR**, so `paper_claimed_*` are null. |
| Donchian channel breakout | no peer-reviewed origin; anchored on Brock et al. (1992) | `bull` | Same data-snooping caveat; `paper_claimed_*` null. |
| Blitz, Huij & Martens (2011) residual momentum | *Journal of Empirical Finance* 2011 | `bull` | The paper covers a broad equity cross-section; the adaptation ranks a five-asset basket with a rolling single-factor OLS beta against SPY. With few assets the beta adjustment can **amplify** rather than reduce noise. Filename says `blitz_hanauer_2010`; the documented anchor is the 2011 paper. |
| Novy-Marx (2012) intermediate-horizon momentum | *JFE* 2012 | `bull` | The composite is 50% intermediate-horizon momentum plus 50% a **price-based quality score** — an Archimedes addition, not the fundamental quality of the QMJ literature. |

### Mean-reversion and pairs

These bet on convergence: a spread, ratio, or residual that has diverged should revert.
Built to be **regime-neutral**, because the bet is on the relationship rather than on
market direction.

- **Gatev, Goetzmann & Rouwenhorst (2006) pairs trading**, five implementations —
  four single specific pairs (EWA/EWC, GLD/SLV, KO/PEP, plus the generic distance pair)
  and one portfolio-of-pairs at the paper's own scale, re-forming pairs every six months.
  **The important caveat:** the paper's headline ~11% annualised excess return belongs
  to a diversified portfolio of many pairs, *not* a single pair. The single-pair files
  say explicitly that a lone pair is not comparable to that figure.
- **Engle & Granger (1987) cointegration pairs** — trades the cointegration residual
  rather than the price ratio, with an Ornstein–Uhlenbeck half-life for reversion.
  Engle–Granger is the econometric framework, not a trading paper; the trading
  application follows Vidyamurthy (2004).
- **Elliott, van der Hoek & Malcolm (2005) Kalman pairs** — models the hedge ratio as a
  time-varying state, so the spread adapts as the relationship drifts.
- **Avellaneda & Lee (2010) PCA statistical arbitrage** — trades the mean-reverting
  residual of each name against its eigenportfolio factor exposure.
- **Bollinger (2001) lower-band reversion** — the reference is a **book, not a
  peer-reviewed paper**, and describes the bands qualitatively; `paper_claimed_*` null.
- **Connors & Alvarez (2009) RSI(2)** — book source reporting win-rate and average-gain
  tables rather than a Sharpe; `paper_claimed_*` null.

### Value and yield

- **Fama & French (1988) dividend-yield predictability** — implemented as a
  **high-D/P-sort proxy, not a true dividend sort**. The filename references Low, Tan &
  Wermers (2004) while the documented anchor is the Fama–French (1988) D/P study.
  Regime `bear`.
- **Asness, Moskowitz & Pedersen (2013) cross-asset value** — without fundamental ratios,
  cheapness is a **price-based proxy** (negative deviation of price from its rolling
  mean), which is cheapness relative to recent history rather than intrinsic value. Five
  assets against the paper's 40+ markets; `paper_claimed_*` null. Regime `bear`.

### Quality and defensive

- **Arnott, Harvey, Kalesnik & Linnainmaa (2019) defensive quality** — the paper is a
  critique-and-prescription on factor investing, not a single mechanical strategy; the
  implementation operationalises its defensive prescription. Regime `bear`.
- **Ang, Hodrick, Xing & Zhang (2006) low idiosyncratic volatility** — residuals come
  from a rolling **single-factor CAPM** against SPY, not the paper's three-factor model,
  so the idiovol estimate is noisier. The paper's −1.06%/month quintile spread is context
  for a broad cross-section, not a benchmark for a five-asset basket. Short-selling costs
  are not modelled. Regime `bear`.
- **Frazzini & Pedersen (2014) Betting Against Beta** — ranks the basket on rolling
  63-day OLS beta with equal weight per leg, but **no leverage rescaling to unit beta is
  applied**; only the long-low / short-high sign survives. With so few assets each leg may
  hold one or two names. Regime `bear`.
- **Asness, Frazzini & Pedersen (2019) Quality Minus Junk** — the engine has no
  fundamental data, so four accounting dimensions collapse into a single **price-based
  information-ratio proxy** capturing profitability and safety only. A loose
  approximation, not a replication. Filename says `novy_marx_2013`; the documented anchor
  is the 2019 paper. Regime `regime_neutral`.

### Risk-parity, allocation, volatility management

These are **allocation overlays** — how to size and balance, not which anomaly to chase.

- **Maillard, Roncalli & Teïletche (2010) risk parity** — equal risk contribution rather
  than equal capital. The strategy-level cousin of the Hierarchical Risk Parity objective
  in [`construction-and-sizing`](../portfolio/construction-and-sizing.md).
- **Moreira & Muir (2017) volatility-managed portfolios** — scale exposure inversely to
  recent realised volatility. Regime `bear`.
- **Faber (2007) SMA-200 tactical allocation** — hold while price is above its 200-day
  SMA, rotate to T-bills below. Regime `bull`.

### Seasonality

- **Ariel (1987) turn-of-the-month** — nearly all of the market's 1963–1981 cumulative
  gain accrued in the first half of trading months. The paper reports the effect, not a
  tradeable Sharpe; `paper_claimed_*` null. Regime `regime_neutral`.

### Baselines

Not alpha bets — the references everything else is measured against.

- **Buy-and-hold** (`pipeline_buy_hold.py`) — the long-only SPY benchmark. Its backtest
  results are real, regenerated over 2004-01-02 → 2026-04-30, so the window includes both
  the 2008–09 crisis and the 2022 correction.
- **Capital preservation** (`capital_preservation_tbill.py`) — a short-duration Treasury
  proxy and the defensive destination the trend filters rotate into. It maps to the
  `fixed_income` and `conservative` risk profiles, and it is the strategy excluded from
  the library-PBO measurement because it models a yield rather than a tradeable
  instrument run (see [`measured-results`](../findings/measured-results.md)).

---

## Reading the shelf without being misled

**A null `paper_claimed_*` is not a weak strategy.** It means the source reported
t-statistics, win rates, or conditional-mean tables instead of a mechanical Sharpe or
CAGR — common for the technical-rule and book-sourced entries. In those cases the only
performance claim to stand behind is the post-gate one measured on internal data.

**Anchor strength varies and is disclosed.** *Journal of Finance* and *JFE* papers are
stronger anchors than practitioner books, which are in turn stronger than rules anchored
on a merely *related* academic test — MACD and Donchian both point at Brock et al. (1992)
rather than at an origin paper of their own.

**Regime tags drive sizing, not just labelling.** A `bull`-tagged strategy is sized down
by the regime-conditional gamma multiplier in `risk_off` and `crisis` regimes; see
[`construction-and-sizing`](../portfolio/construction-and-sizing.md).

**Do not take a pass or fail marker from this shelf.** The slice's own rule is that the
live gate is the only authority, and the slice violates that rule in three places —
enumerated in [`documented-conflicts`](../rigor/documented-conflicts.md).
