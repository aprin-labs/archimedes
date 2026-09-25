# Strategy Library Reference

> **Status:** Living reference. Written 2026-06-12.
> **Author:** Önder Akkaya (quant / math lane).
> **Audience:** Anyone browsing the library who wants to know what each strategy
> captures, what regime it suits, and — honestly — how the v1 implementation
> diverges from the source paper.
> **Source of truth:** the strategy files under
> [`../../analytics-engine/strategies/`](../../analytics-engine/strategies/). The
> paper / author / regime fields below are transcribed from each file's
> `PAPER_*` and `REGIME_TAG` constants. Where a file's header documents a v1
> adaptation caveat, that caveat is reproduced here. For strategies whose full
> methodology is non-trivial, see the file header for the complete write-up.

This page documents the strategy files in the library (34 at the time of writing; count
them with `ls analytics-engine/strategies/*.py` — that command, not this number, is the
answer). Each carries an academic or practitioner anchor, a `REGIME_TAG`
(`bull` / `bear` / `regime_neutral`), and the honest paper-vs-implementation
delta. Admission to Tier 1 still requires passing the four-gate
[`admission-criteria.md`](admission-criteria.md); a paper anchor is necessary, not
sufficient. Which strategies pass all four gates is not recorded here — the live rigor gate is the only authority on which strategies currently pass; see the PASS/CANDIDATE badges in the app and `backend/archimedes/services/live_rigor_gate.py`; the rest are honest
`CANDIDATE`s with their failing gate shown openly.

> **A note on library size, added 2026-08-31 ([#1598](https://github.com/aprin-labs/archimedes/issues/1598)).**
> The two findings notes in this directory — [`library-pbo.md`](library-pbo.md) and
> [`second-wave-universe-experiment.md`](second-wave-universe-experiment.md) — were both run
> on **2026-06-11 over 22 of the 23 strategies that existed then**. The shelf has grown
> since. Those notes are correct at their vintage and are not restated here; a PBO or
> universe figure quoted from them is a 22-strategy figure, not a figure about the shelf you
> are reading now.

> **Corrected 2026-08-31 (#1598).** This paragraph used to explain Faber's failure by
> calling 0.612 a walk-forward out-of-sample Sharpe and comparing it to the DSR gate. Both
> halves were wrong. **0.612 is a DSR p-value** — the live pull recorded in
> [`../analysis/faber-dsr-finding.md`](../analysis/faber-dsr-finding.md) (2026-05-27) puts
> Faber's `dsr_p_value` at 0.612 and its out-of-sample Sharpe at **0.930**. And **the DSR
> bar is not an out-of-sample-Sharpe threshold at all**: it is the level-1 DSR p-value bar,
> a probability in `[0, 1]`. The out-of-sample Sharpe has two thresholds of its own — an
> always-on floor of `> 0` and a cliff of `OOS/IS ≥ 0.5` — and 0.930 clears both
> comfortably. Faber's recorded failure is on **criterion 1**, `dsr_p_value` against the
> DSR bar; the verdict is unchanged, the reason was misattributed. As with every strategy
> on this page, the current verdict comes from the live gate, not from this file.

A recurring honesty theme: many files set `paper_claimed_*` to **null** when the
source reports win-rate / t-statistic / conditional-mean tables rather than a
mechanical Sharpe or CAGR (common for the technical-rule and book-sourced
strategies). In those cases the only performance claim we stand behind is the
post-gate one measured on our own data.

---

## Sleeve: Momentum / trend-following

Momentum captures under-reaction and herding — winners keep winning over
intermediate horizons. Trend strategies are **bull-biased** by construction and
carry crash risk at trend reversals.

### Jegadeesh & Titman (1993) — cross-sectional momentum
- **File:** `jegadeesh_titman_1993_cross_sectional_momentum.py`
- **Paper:** *Returns to Buying Winners and Selling Losers: Implications for Stock
  Market Efficiency*, Journal of Finance, 1993.
- **Anomaly:** stocks ranked on prior 3–12 month returns continue to outperform/
  underperform over the next 3–12 months — the canonical academic documentation of
  the momentum effect.
- **Regime:** `bull` (momentum is a trend phenomenon).
- **v1 caveat:** the original is a cross-sectional long–short ranking; verify the
  live universe and ranking horizon against the paper. *(See file header.)*

### Moskowitz, Ooi & Pedersen (2012) — Time Series Momentum (TSMOM)
- **File:** `moskowitz_ooi_pedersen_2012_tsmom.py`
- **Paper:** *Time Series Momentum*, Journal of Financial Economics, 2012.
- **Anomaly:** an asset's own past 12-month return predicts its next-month return —
  go long if the trailing return `r_{t-12,t}` is positive, flat/short otherwise.
- **Regime:** `bull`.
- **v1 caveat:** the paper's diversified portfolio takes **long *and* short**
  positions across many assets; the v1 adaptation is long/flat on a narrower
  universe, so the paper-claimed-vs-actual delta is surfaced in the methodology
  block. *(See file header.)*
- **Status:** per the live rigor gate (see
  [`../analysis/faber-dsr-finding.md`](../analysis/faber-dsr-finding.md)).

### Antonacci (2014) — Dual Momentum
- **File:** `antonacci_2014_dual_momentum.py`
- **Paper:** *Risk Premia Harvesting Through Dual Momentum* (SSRN 2042750; JPM
  Vol. 42 No. 1), 2014.
- **Anomaly:** combines **relative** momentum (pick the stronger asset) with
  **absolute** momentum (defensive switch to cash/bonds when trailing return is
  negative) — trend-following with a downside switch.
- **Regime:** `bull` (defends in bear).

### George & Hwang (2004) — 52-Week High proximity momentum
- **File:** `george_hwang_2004_52w_high.py`
- **Paper:** *The 52-Week High and Momentum Investing*, Journal of Finance, 2004.
- **Anomaly:** proximity of price to its 52-week high predicts continued returns —
  a nearness-to-high signal rather than a raw-return rank.
- **Regime:** `bull`.
- **v1 caveat:** the original is **cross-sectional** (rank all stocks by
  high-proximity); the v1 single-name proxy strips the cross-sectional component, so
  the directional alpha is **lower than the paper claims** and the divergence is
  disclosed. *(See file header.)*

### Appel (1979) — MACD signal-line crossover
- **File:** `appel_1979_macd.py`
- **Paper:** MACD trading method (Appel 1979); academic MA-rule evidence in **Brock,
  Lakonishok & LeBaron (1992)**, Journal of Finance 47(5).
- **Anomaly:** smoothed momentum via the convergence/divergence of two EMAs; trade
  the signal-line crossover.
- **Regime:** `regime_neutral`.
- **v1 caveat:** MACD has no peer-reviewed origin paper, so the academic anchor is
  Brock et al. (1992)'s MA-rule tests. **Data-snooping caveat** (Sullivan,
  Timmermann & White 1999) applies. *(See file header.)*

### Brock, Lakonishok & LeBaron (1992) — dual MA crossover (golden cross)
- **File:** `brock_1992_dual_ma_crossover.py`
- **Paper:** *Simple Technical Trading Rules and the Stochastic Properties of Stock
  Returns*, Journal of Finance 47(5):1731–1764, 1992.
- **Anomaly:** long/short on a fast/slow moving-average crossover; the paper's
  rigorous test of the MA-rule family on the DJIA 1897–1986 found buy-signals carry
  predictive content.
- **Regime:** `bull`.
- **v1 caveat:** the paper reports **conditional mean returns and t-statistics, not
  a tradeable Sharpe or CAGR**, so `paper_claimed_*` are null. *(See file header.)*

### Donchian — channel breakout (Four-Week Rule)
- **File:** `donchian_breakout.py`
- **Paper:** Donchian's breakout rule; academic anchor again **Brock, Lakonishok &
  LeBaron (1992)**.
- **Anomaly:** enter on a breakout above the N-day high (and exit on the symmetric
  low) — the classic trend-capture mechanism.
- **Regime:** `bull`.
- **v1 caveat:** no peer-reviewed origin; anchored on Brock et al. (1992).
  Data-snooping caveat (Sullivan, Timmermann & White 1999) applies; the performance
  claim we stand behind is the post-gate one, `paper_claimed_*` null. *(See file
  header.)*

### Blitz, Huij & Martens (2011) — residual momentum
- **File:** `blitz_hanauer_2010_rmom.py`
- **Paper:** *Residual Momentum*, Journal of Empirical Finance, 2011.
- **Anomaly:** standard momentum conflates alpha with market-beta exposure; rank
  assets instead by their **beta-adjusted** formation-window return (the residual
  after subtracting `beta × market_return`). The paper reports a higher Sharpe than
  total-return momentum, lower correlation with conventional momentum factors.
- **Regime:** `bull`.
- **v1 caveat:** the paper's results are for a broad equity cross-section; the v1
  adaptation ranks a 5-asset basket with a rolling single-factor OLS beta vs
  `datas[0]` (SPY), so `paper_claimed_*` are null. With few assets the beta
  adjustment can amplify rather than reduce noise. Note the filename says
  `blitz_hanauer_2010` but the documented anchor is Blitz, Huij & Martens (2011).
  *(See file header.)*

### Novy-Marx (2012) — intermediate-horizon "quality" momentum
- **File:** `novy_marx_2012_quality_momentum.py`
- **Paper:** *Is Momentum Really Momentum?*, Journal of Financial Economics, 2012.
- **Anomaly:** the months-7–12 formation window (skipping recent months) drives most
  of the standard 12-1 momentum signal; short-horizon continuation is largely noise.
- **Regime:** `bull` (quality leg partially defends in bear).
- **v1 caveat:** the composite is 50% intermediate-horizon momentum + 50% a
  **price-based quality score** (information ratio over 63 bars) — the quality
  filter is an Archimedes addition layered on the paper's refined momentum window,
  not the fundamental quality of the AQR QMJ paper. US-stock cross-section results
  don't transfer to the 5-asset basket; `paper_claimed_*` null. *(See file header.)*

---

## Sleeve: Mean-reversion / pairs (relative-value, market-neutral)

These strategies bet on convergence — a spread, ratio, or residual that has
diverged should revert. Built to be **regime-neutral** because the bet is on the
*relationship*, not market direction.

### Gatev, Goetzmann & Rouwenhorst (2006) — pairs trading (distance method)
- **Files:** `gatev_2006_pairs_distance.py`, `gatev_2006_pairs_ewa_ewc.py`,
  `gatev_2006_pairs_gld_slv.py`, `gatev_2006_pairs_ko_pep.py`,
  `gatev_2006_portfolio_of_pairs.py`
- **Paper:** *Pairs Trading: Performance of a Relative-Value Arbitrage Rule*, Review
  of Financial Studies 19, 2006.
- **Anomaly:** match securities by minimum sum-of-squared normalized-price distance;
  when a matched pair's normalized prices diverge beyond a threshold, short the
  winner / long the loser and hold to convergence.
- **Regime:** `regime_neutral`.
- **Five implementations:** four are **single specific pairs** (EWA/EWC country
  ETFs, GLD/SLV gold-silver ratio, KO/PEP consumer staples, plus the generic
  distance pair) and one is the **portfolio-of-pairs** faithful-scale method.
- **v1 caveat (important):** the paper's headline ~11% annualized excess return
  belongs to a **diversified portfolio of many pairs**, *not* a single pair. The
  `gatev_2006_portfolio_of_pairs.py` file reproduces the paper's actual design at
  the paper's actual scale (re-form pairs every 6 months); the single-pair files
  explicitly note that a lone pair is **not comparable** to the paper's diversified
  ~11%. *(See file headers.)*

### Engle & Granger (1987) — cointegration pairs
- **File:** `engle_granger_1987_cointegration_pairs.py`
- **Paper:** *Co-integration and Error Correction: Representation, Estimation, and
  Testing*, Econometrica, 1987 (with Vidyamurthy 2004 for the pairs application).
- **Anomaly:** where the distance method trades the *price ratio*, this trades the
  **cointegration residual** — fit a cointegrating relationship, model the spread's
  reversion with an Ornstein–Uhlenbeck half-life, and trade deviations.
- **Regime:** `regime_neutral` (relative-value by design).
- **v1 caveat:** Engle–Granger is the econometric *framework*, not a trading paper;
  the trading application follows Vidyamurthy (2004). *(See file header.)*

### Elliott, van der Hoek & Malcolm (2005) — Kalman-filter dynamic-hedge pairs
- **File:** `elliott_2005_kalman_pairs.py`
- **Paper:** *Pairs Trading*, Quantitative Finance, 2005.
- **Anomaly:** model the hedge ratio as a **time-varying state** estimated by a
  Kalman filter, so the pair's spread adapts as the relationship drifts — more
  robust than a static OLS hedge.
- **Regime:** `regime_neutral`.

### Avellaneda & Lee (2010) — PCA / eigenportfolio statistical arbitrage
- **File:** `avellaneda_lee_2010_pca_statarb.py`
- **Paper:** *Statistical Arbitrage in the U.S. Equities Market*, Quantitative
  Finance, 2010.
- **Anomaly:** decompose returns into principal-component (eigenportfolio) factors;
  trade the mean-reverting **residual** of each name against its factor exposure —
  market-neutral residual reversion.
- **Regime:** `regime_neutral` (market-neutral by construction).
- **v1 caveat:** the PCA-residual approach reads from every `self.datas[i]` each
  bar; verify the live universe matches the paper's residual construction. *(See
  file header.)*

### Bollinger (2001) — lower-band mean reversion
- **File:** `bollinger_2001_band_reversion.py`
- **Paper:** *Bollinger on Bollinger Bands* (McGraw-Hill book), 2001.
- **Anomaly:** buy when price touches the lower band (volatility-scaled distance from
  a moving average), expecting reversion to the mean.
- **Regime:** `regime_neutral`.
- **v1 caveat:** the authoritative reference is a **book, not a peer-reviewed
  paper**, and it describes the bands qualitatively without a mechanical Sharpe/CAGR;
  bands widen in volatile regimes and contract in calm ones, so a fixed-distance
  rule behaves differently across regimes. `paper_claimed_*` null. *(See file
  header.)*

### Connors & Alvarez (2009) — RSI(2) mean reversion
- **File:** `connors_alvarez_2009_rsi2.py`
- **Paper:** *Short Term Trading Strategies That Work* (book), 2009.
- **Anomaly:** short-term mean reversion using a 2-period RSI, entered only inside a
  longer-term uptrend filter.
- **Regime:** `regime_neutral` (mean reversion inside an uptrend filter).
- **v1 caveat:** book source, not peer-reviewed; reports **win-rate / average-gain
  tables** rather than a clean Sharpe/CAGR, so `paper_claimed_*` are null. *(See
  file header.)*

---

## Sleeve: Value / yield

### Fama & French (1988) — dividend-yield predictability
- **File:** `low_tan_wermers_2004_dividend_yield.py`
- **Paper:** *Dividend Yields and Expected Stock Returns*, Journal of Financial
  Economics, 1988.
- **Anomaly:** the dividend yield (D/P) of stocks predicts subsequent returns — a
  value/yield tilt.
- **Regime:** `bear` (value/defensive tilt).
- **v1 caveat:** the live implementation is a **high-D/P-sort proxy**, *not a true
  dividend sort*; the file name references Low, Tan & Wermers (2004) but the
  documented anchor and methodology are the Fama–French (1988) D/P study. Treat as a
  yield-tilt proxy. *(See file header.)*

### Asness, Moskowitz & Pedersen (2013) — cross-asset value
- **File:** `asness_moskowitz_2013_value.py`
- **Paper:** *Value and Momentum Everywhere*, Journal of Finance, 2013.
- **Anomaly:** value and momentum premia exist across eight asset classes (equities,
  indices, bonds, currencies, commodities); for asset classes, "cheap" is proxied by
  long-horizon return reversal. The paper reports a value-factor Sharpe of ~0.6
  averaged across classes — and that value and momentum are **negatively
  correlated**, making them natural complements.
- **Regime:** `bear` (value mean-reverts as expensive assets de-rate).
- **v1 caveat:** without fundamental ratios, cheapness is a **price-based proxy**
  (negative deviation of price from its rolling mean) per the paper's Appendix
  Table AI proxies — cheapness relative to recent history, not intrinsic value.
  Universe is 5 assets vs the paper's 40+ markets; `paper_claimed_*` null. *(See
  file header.)*

---

## Sleeve: Quality / defensive

### Arnott, Harvey, Kalesnik & Linnainmaa (2019) — defensive quality
- **File:** `arnott_2019_defensive_quality.py`
- **Paper:** *Alice's Adventures in Factorland: Three Blunders That Plague Factor
  Investing*, Journal of Portfolio Management, 2019.
- **Anomaly:** a defensive/quality tilt informed by the paper's catalogue of naive
  factor-investing blunders (using the market as a beta reference rather than
  chasing raw factor backtests).
- **Regime:** `bear` (defensive).
- **v1 caveat:** the paper is a *critique-and-prescription* on factor investing, not
  a single mechanical strategy; the implementation operationalizes its defensive
  prescription. *(See file header.)*

### Ang, Hodrick, Xing & Zhang (2006) — low idiosyncratic volatility
- **File:** `ang_hodrick_2006_low_idiovol.py`
- **Paper:** *The Cross-Section of Volatility and Expected Returns*, Journal of
  Finance, 2006.
- **Anomaly:** stocks with high idiosyncratic volatility earn anomalously **low**
  subsequent returns — the paper reports a −1.06%/month value-weighted quintile
  spread on US stocks 1963–2000, contradicting the risk-return tradeoff. Long
  low-idiovol, short high-idiovol.
- **Regime:** `bear` (low-vol anomaly strongest in defensive/risk-off regimes).
- **v1 caveat:** residuals come from a rolling **single-factor CAPM** vs `datas[0]`
  (SPY), not the paper's Fama–French three-factor model, so the idiovol estimate is
  noisier. The paper's −1.06%/month is context for a broad stock cross-section, not
  a benchmark for the 5-asset basket; `paper_claimed_*` null. Short-selling costs
  are not modelled. *(See file header.)*

### Frazzini & Pedersen (2014) — Betting Against Beta (BAB)
- **File:** `frazzini_pedersen_2014_bab.py`
- **Paper:** *Betting Against Beta*, Journal of Financial Economics, 2014.
- **Anomaly:** the Security Market Line is empirically flat or inverted —
  leverage-constrained investors bid up high-beta assets, compressing their
  risk-adjusted returns. Long low-beta (levered to unit beta), short high-beta
  (delevered).
- **Regime:** `bear` (low-beta legs — Treasuries, gold — give flight-to-quality
  exposure when high-beta assets draw down).
- **v1 caveat:** ranks the 5-asset basket on a rolling 63-day OLS beta vs SPY with
  equal weight in each leg — **no explicit leverage rescaling to unit beta** is
  applied; only the long-low/short-high sign survives. Broad US-stock results don't
  transfer; `paper_claimed_*` null, and with so few assets each leg may hold only
  one or two names. *(See file header.)*

### Asness, Frazzini & Pedersen (2019) — Quality Minus Junk (price-based proxy)
- **File:** `novy_marx_2013_qmj.py`
- **Paper:** *Quality Minus Junk*, Review of Accounting Studies, 2019.
- **Anomaly:** a composite quality score (profitability, safety, growth, payout from
  accounting fundamentals) earns a Sharpe of ~1.0 long-quality/short-junk on US
  stocks 1956–2012, largely independent of market, value, and momentum factors.
- **Regime:** `regime_neutral` (quality premium persistent across regimes; strongest
  when junk sells off).
- **v1 caveat:** the engine has no fundamental data, so the four accounting
  dimensions are replaced by a single **price-based information-ratio proxy**
  (mean/std of daily returns) capturing only the profitability + safety dimensions —
  a loose approximation, not a replication; `paper_claimed_*` null. Note the
  filename says `novy_marx_2013` but the documented anchor is Asness, Frazzini &
  Pedersen (2019). *(See file header.)*

---

## Sleeve: Risk-parity / allocation / volatility management

These are **allocation overlays** — how to size and balance, not what single
anomaly to chase.

### Maillard, Roncalli & Teïletche (2010) — risk parity / inverse-volatility
- **File:** `maillard_2010_risk_parity.py`
- **Paper:** *The Properties of Equally Weighted Risk Contribution Portfolios*,
  Journal of Portfolio Management, 2010.
- **Anomaly:** size positions so each contributes **equal risk** (equal-risk
  contribution / inverse-volatility), rather than equal capital — a diversification
  overlay designed to hold across regimes.
- **Regime:** `regime_neutral`.
- **Relation to the optimizer:** this is the strategy-level cousin of the
  Hierarchical Risk Parity objective documented in
  [`methodology.md`](methodology.md) §11.

### Moreira & Muir (2017) — volatility-managed portfolios
- **File:** `moreira_muir_2017_volatility_managed.py`
- **Paper:** *Volatility-Managed Portfolios*, Journal of Finance, 2017.
- **Anomaly:** scale exposure **inversely to recent realized volatility** — contract
  in high-vol regimes, expand in calm ones. Reduces drawdowns and improves Sharpe
  versus a static-exposure baseline; exploits the weak/negative relationship between
  current volatility and next-period return.
- **Regime:** `bear` (outperforms in bear/high-vol regimes).
- **Status:** per the live rigor gate.
  *(Corrected 2026-08-31, [#1598](https://github.com/aprin-labs/archimedes/issues/1598).)*
  This line used to assert a `CANDIDATE` verdict, explain it with 0.612 as a walk-forward
  out-of-sample Sharpe, and then retract a claim about **Faber**. Every clause of it
  belonged to a different strategy — 0.612 is Faber's DSR p-value — and the Faber sentence
  was stranded here by a partially-applied edit. It sat directly under a heading that
  claimed this strategy passes. All of it is gone; the live gate answers.
- **v1 note:** the volatility signal is a rolling realized-volatility estimate (the
  paper uses one-month realized vol). *(See file header.)*

### Faber (2007) — SMA200 tactical asset allocation
- **File:** `faber_2007_sma200_timing.py`
- **Paper:** *A Quantitative Approach to Tactical Asset Allocation*, Journal of
  Wealth Management, 2007.
- **Anomaly:** hold the asset while price is above its 200-day SMA; move to cash
  (T-bills) when it falls below — a simple trend filter that cuts drawdowns.
- **Regime:** `bull` (works in trending markets).
- **Status:** per the live rigor gate.

---

## Sleeve: Seasonality

### Ariel (1987) — turn-of-the-month effect
- **File:** `ariel_1987_turn_of_month.py`
- **Paper:** *A Monthly Effect in Stock Returns*, Journal of Financial Economics,
  1987 (corroborated by Lakonishok & Smidt 1988).
- **Anomaly:** essentially all of the market's cumulative gain over 1963–1981 accrued
  in the first half of trading months; trade the turn-of-month window.
- **Regime:** `regime_neutral`.
- **v1 caveat:** a calendar-window strategy; the file notes `paper_claimed_*` are
  null (the paper reports the effect, not a tradeable Sharpe). *(See file header.)*

---

## Sleeve: Baseline / capital preservation

These are not alpha bets — they are the references everything else is measured
against, and the defensive floor.

### Buy-and-Hold baseline
- **File:** `pipeline_buy_hold.py`
- **Anchor:** internal baseline (no paper).
- **Role:** the long-only SPY buy-and-hold benchmark against which paper-grounded
  strategies are compared. Backtest results are **real**, regenerated via
  `scripts/regen_buy_hold_fixture.py` over 2004-01-02 → 2026-04-30 (includes the
  2008–09 crisis and the 2022 correction).
- **Regime:** `bull` (long-only, bull-biased).

### Capital Preservation — T-Bill / USYC allocation
- **File:** `capital_preservation_tbill.py`
- **Anchor:** internal (no paper); a short-duration Treasury / USYC proxy.
- **Role:** the defensive baseline that works in any regime — the destination the
  trend filters (Faber, Antonacci's absolute-momentum switch) rotate *into* when
  risk-off. Maps naturally to the `fixed_income` / `conservative` risk profiles.
- **Regime:** `regime_neutral` (defensive baseline).

---

## How to read this library

- **Paper anchor strength varies and is disclosed.** Journal-of-Finance/JFE papers
  (Jegadeesh–Titman, Moreira–Muir, Gatev et al., George–Hwang, Fama–French) are
  stronger anchors than practitioner books (Bollinger, Connors–Alvarez) or
  rules anchored on a *related* academic test (MACD/Donchian → Brock et al. 1992).
- **`paper_claimed_*` null ≠ a weak strategy.** It means the source reported
  t-stats/win-rates rather than a mechanical Sharpe/CAGR; the number we stand behind
  is always the post-gate one on our own data.
- **A paper anchor does not equal Tier-1 admission.** Which strategies pass all four gates is
  reported by the live gate, not by this file.
  Everything that has not passed is an honest `CANDIDATE` with its failing gate visible. See [`admission-criteria.md`](admission-criteria.md).
  One strategy has a standing written analysis of *why* it fails —
  [`../analysis/faber-dsr-finding.md`](../analysis/faber-dsr-finding.md), on a 2026-05-27
  pull — which is a record of that finding, not a statement of Faber's status today.
- **A heading is not a verdict.** *(Rule added 2026-08-31, #1598.)* Section headings on
  this page carry the paper and the strategy name only. Three of them used to carry a
  pass/fail marker, and all three disagreed with the status line directly beneath them —
  including a green tick sitting on top of "fails admission". Pass/fail lives in the live
  gate and nowhere on this page; if you are tempted to add a marker to a heading, add a
  link to the badge instead.
- **Regime tags drive sizing, not just labeling.** A `bull`-tagged strategy is sized
  down by the regime-conditional γ multiplier in `risk_off`/`crisis` regimes (see
  [`methodology.md`](methodology.md) §10).
