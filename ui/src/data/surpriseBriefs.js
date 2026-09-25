// Surprise Me brief bank for the Generate page (issue #1642).
//
// Replaces ui/src/data/exampleBriefs.js — three entries that were rendered as
// an always-visible "Examples — click to fill" list. That list is retired:
// nothing from this bank is shown until the user presses "Surprise me", and
// each press draws a different entry (see ./pickSurpriseBrief.js).
//
// ── What every entry must be ──────────────────────────────────────────────
//
// Shape of a good brief, unchanged from the file this replaces: name concrete
// assets or classes, a mechanism (momentum / vol-managed / carry / hedge /
// mean-reversion / rotation), and a goal. An entry that names only a goal
// ("make money safely") steers no universe and is not in here.
//
// `suggestedAssets` (optional) are display symbols from the supported universe
// (see assetUniverse.js) that the UI pre-selects when the brief is applied.
// Keep it to 1–5 symbols, and — for every entry added after this file was
// created — symbols the brief text itself names, so the pre-selection is
// never a claim the brief did not make. Both invariants are enforced by
// ui/test/surprise-briefs.test.js.
//
// ── What these entries are NOT ────────────────────────────────────────────
//
// **Only the first three have been run through the live pipeline.** They are
// the dogfood-proven entries carried over from exampleBriefs.js (5-brief
// bake-off, 2026-07-04, PR #875), and their per-entry status comments are
// preserved verbatim below.
//
// The other ~104 are CURATED COPY, not dogfood results. They were written to
// the quality bar above, machine-checked against the deterministic backend
// validator (`cheap_brief_reject` — backend/tests/test_surprise_briefs_quality.py)
// and against the supported asset universe, and read for finance literacy.
// **None of them has been generated live.** Do not describe this bank as
// "validated strategies", do not quote a pass rate for it, and do not add a
// `Status: DOGFOOD PROVEN` comment to an entry that has not actually been run.
// Passing `cheap_brief_reject` means "not obviously junk" — it is a floor, not
// a verdict; that function deliberately rejects only empty / too-short /
// keyboard-mash text (read its docstring in
// backend/archimedes/agents/generation_pipeline.py before reading more into it).

export const SURPRISE_BRIEFS = [
  // ── Dogfood-proven carry-overs from exampleBriefs.js (#872 / PR #875) ──
  {
    id: 'momentum-quality-gold-usdc',
    label: 'Momentum + quality + gold hedge, vol-managed sizing',
    brief:
      'blend momentum, quality and a gold hedge across major ETFs with volatility-managed sizing for idle USDC',
    suggestedAssets: ['SPY', 'QQQ', 'GLD', 'IWM', 'VTV'],
    // Status: DOGFOOD PROVEN — dsr_p 0.999; strongest alpha signal of the bake-off.
  },
  {
    id: 'crypto-trend-treasury-rotation',
    label: 'BTC/ETH trend with defensive treasury rotation',
    brief:
      'trend-following on BTC and ETH with a defensive rotation into treasuries when volatility spikes',
    suggestedAssets: ['BTC', 'ETH', 'IEF', 'SHY'],
    // Status: DOGFOOD PROVEN — dsr_p 0.938, PBO 0.19; best-balanced of the bake-off.
  },
  {
    id: 'low-vol-income-preservation',
    label: 'Low-volatility income, capital preservation',
    brief:
      'low-volatility income portfolio from SPY TLT and SCHD focused on capital preservation',
    suggestedAssets: ['SPY', 'TLT', 'SCHD'],
    // Status: DOGFOOD PROVEN — cleanest overfitting profile (PBO 0.27) of the bake-off.
  },

  // ── US equity factors ────────────────────────────────────────────────────
  {
    id: 'value-momentum-sleeve',
    label: 'Value core with a momentum sleeve',
    brief:
      'combine a value tilt from VTV and IWD with a momentum sleeve from MTUM, rebalanced monthly, targeting steady excess return over SPY',
    suggestedAssets: ['VTV', 'IWD', 'MTUM', 'SPY'],
  },
  {
    id: 'size-quality-smallcap',
    label: 'Quality-screened small caps',
    brief:
      'small-cap exposure through IJR and VB screened for quality with QUAL, sized down as drawdown deepens, aiming to capture the size premium without the full small-cap volatility',
    suggestedAssets: ['IJR', 'VB', 'QUAL'],
  },
  {
    id: 'minvol-core-defensive',
    label: 'Minimum-volatility US core',
    brief:
      'core US equity from USMV and SPLV with volatility-managed sizing, goal is equity-like return with a shallower maximum drawdown than SPY',
    suggestedAssets: ['USMV', 'SPLV', 'SPY'],
  },
  {
    id: 'equal-weight-concentration-hedge',
    label: 'Equal weight against mega-cap concentration',
    brief:
      'hold RSP equal-weight instead of SPY to dilute mega-cap concentration, with a QQQ overlay added only when market breadth improves, targeting steadier compounding',
    suggestedAssets: ['RSP', 'SPY', 'QQQ'],
  },
  {
    id: 'growth-value-regime-switch',
    label: 'Growth/value regime switch',
    brief:
      'rotate between VUG and VTV on a trend and rate-momentum signal, aiming to hold the winning style rather than a static blend of both',
    suggestedAssets: ['VUG', 'VTV'],
  },
  {
    id: 'dividend-growth-compounding',
    label: 'Dividend growth, low turnover',
    brief:
      'dividend-growth compounding from VIG DGRO and NOBL with a quality screen, goal is rising income with lower turnover than a high-yield screen',
    suggestedAssets: ['VIG', 'DGRO', 'NOBL'],
  },
  {
    id: 'mid-cap-momentum-bridge',
    label: 'Mid-cap momentum with a trend stop',
    brief:
      'mid-cap momentum from IJH and MDY with a cash buffer whenever the 200-day trend breaks, targeting the mid-cap growth premium with a defined downside stop',
    suggestedAssets: ['IJH', 'MDY'],
  },
  {
    id: 'total-market-trend-overlay',
    label: 'Total market with a trend overlay',
    brief:
      'hold VTI as the core and shift into SHY when the 12-month trend turns negative, a simple trend overlay aimed at cutting bear-market drawdown',
    suggestedAssets: ['VTI', 'SHY'],
  },
  {
    id: 'mean-reversion-oversold-index',
    label: 'Short-horizon index mean reversion',
    brief:
      'short-horizon mean reversion on SPY and QQQ after oversold stretches, with volatility-scaled position sizing and a hard drawdown stop',
    suggestedAssets: ['SPY', 'QQQ'],
  },
  {
    id: 'low-beta-duration-blend',
    label: 'Low-beta equity plus intermediate duration',
    brief:
      'blend low-beta equity from USMV with intermediate treasuries in IEF, targeting a smoother equity curve than a static sixty-forty portfolio',
    suggestedAssets: ['USMV', 'IEF'],
  },
  {
    id: 'quality-profitability-screen',
    label: 'Profitability-screened US equity',
    brief:
      'own QUAL and VLUE together with a profitability screen and quarterly rebalancing, goal is factor exposure that survives a value drawdown',
    suggestedAssets: ['QUAL', 'VLUE'],
  },
  {
    id: 'broad-core-drift-bands',
    label: 'Two-fund core on drift bands',
    brief:
      'hold ITOT and IUSB as a two-fund core rebalanced on drift bands rather than a calendar, aiming for low-cost compounding with minimal turnover',
    suggestedAssets: ['ITOT', 'IUSB'],
  },

  // ── Sector and thematic rotation ─────────────────────────────────────────
  {
    id: 'sector-momentum-rotation',
    label: 'US sector momentum rotation',
    brief:
      'rotate monthly into the strongest of the US sector ETFs such as XLK XLE and XLF on six-month momentum, falling back to XLU when nothing is trending, aiming for relative-strength returns with a defensive fallback',
    suggestedAssets: ['XLK', 'XLE', 'XLF', 'XLU'],
  },
  {
    id: 'energy-inflation-hedge',
    label: 'Energy and commodities as an inflation hedge',
    brief:
      'hedge inflation with XLE and XOP alongside a broad commodity sleeve in DBC, goal is to protect real purchasing power when inflation surprises to the upside',
    suggestedAssets: ['XLE', 'XOP', 'DBC'],
  },
  {
    id: 'semis-cycle-trend',
    label: 'Semiconductor cycle, trend-gated',
    brief:
      'trend-following on SMH and SOXX with a volatility target that cuts exposure when the semiconductor cycle rolls over, targeting cyclical upside inside a defined risk budget',
    suggestedAssets: ['SMH', 'SOXX'],
  },
  {
    id: 'defensive-staples-utilities',
    label: 'Defensive sectors for a slowdown',
    brief:
      'defensive core of XLP XLV and XLU sized up when the yield curve inverts, aiming for capital preservation through an economic slowdown',
    suggestedAssets: ['XLP', 'XLV', 'XLU'],
  },
  {
    id: 'financials-rates-beta',
    label: 'Financials as rate beta, duration-hedged',
    brief:
      'express a rising-rate view through XLF and KRE hedged with a duration sleeve in TLT, goal is to earn the rate beta without an outright macro bet',
    suggestedAssets: ['XLF', 'KRE', 'TLT'],
  },
  {
    id: 'real-estate-rate-sensitivity',
    label: 'REITs, only when real yields fall',
    brief:
      'own VNQ and SCHH only while real yields are falling and hold BIL otherwise, targeting REIT income while avoiding the rate-shock drawdowns',
    suggestedAssets: ['VNQ', 'SCHH', 'BIL'],
  },
  {
    id: 'clean-energy-thematic-risk',
    label: 'Clean energy with a hard risk budget',
    brief:
      'thematic exposure to ICLN TAN and LIT with volatility-managed sizing and a strict drawdown budget, aiming to hold a high-beta theme without ruin risk',
    suggestedAssets: ['ICLN', 'TAN', 'LIT'],
  },
  {
    id: 'cybersecurity-software-momentum',
    label: 'Software and cybersecurity momentum',
    brief:
      'momentum across CIBR HACK and IGV rebalanced quarterly, goal is durable software-spending exposure with a trend filter to sidestep multiple compression',
    suggestedAssets: ['CIBR', 'HACK', 'IGV'],
  },
  {
    id: 'biotech-barbell',
    label: 'Biotech barbell against short treasuries',
    brief:
      'barbell biotech beta from XBI and IBB against short treasuries in SHV, aiming to keep optionality on innovation while capping the drawdown',
    suggestedAssets: ['XBI', 'IBB', 'SHV'],
  },
  {
    id: 'infrastructure-capex-cycle',
    label: 'Infrastructure and construction capex',
    brief:
      'own PAVE IFRA and ITB as an infrastructure and construction capex basket behind a trend filter, targeting the fiscal-spending cycle with a defined exit',
    suggestedAssets: ['PAVE', 'IFRA', 'ITB'],
  },
  {
    id: 'transport-cyclical-credit-gate',
    label: 'Cyclicals gated by credit spreads',
    brief:
      'use JETS and XLI as a cyclical risk-on sleeve gated by a credit-spread signal, moving to AGG when spreads widen, aiming to be cyclical only when credit agrees',
    suggestedAssets: ['JETS', 'XLI', 'AGG'],
  },
  {
    id: 'robotics-automation-theme',
    label: 'Robotics and automation, vol-capped',
    brief:
      'thematic allocation to BOTZ ROBO and IGV capped by realized volatility, goal is exposure to automation capex with position sizes that respect the theme volatility',
    suggestedAssets: ['BOTZ', 'ROBO', 'IGV'],
  },
  {
    id: 'homebuilders-rate-cycle',
    label: 'Homebuilders against the rate cycle',
    brief:
      'trade ITB against a duration signal from IEF, going long builders only when rates are falling, targeting the housing cycle with an explicit rate hedge',
    suggestedAssets: ['ITB', 'IEF'],
  },

  // ── International and emerging markets ───────────────────────────────────
  {
    id: 'developed-ex-us-momentum',
    label: 'Developed ex-US, only when it leads',
    brief:
      'momentum rotation across EFA VEA and EWJ measured against SPY, goal is to add developed-market diversification only while it is actually outperforming',
    suggestedAssets: ['EFA', 'VEA', 'EWJ', 'SPY'],
  },
  {
    id: 'em-vol-scaled-dollar-filter',
    label: 'EM equity with a dollar filter',
    brief:
      'emerging-market equity from EEM and IEMG sized by realized volatility behind a dollar-strength filter, targeting EM upside without the dollar-shock drawdowns',
    suggestedAssets: ['EEM', 'IEMG'],
  },
  {
    id: 'china-tech-tactical',
    label: 'China tech, tactical with a hard stop',
    brief:
      'tactical exposure to KWEB MCHI and FXI with a hard stop and volatility-scaled sizing, aiming to trade the policy cycle rather than hold through it',
    suggestedAssets: ['KWEB', 'MCHI', 'FXI'],
  },
  {
    id: 'india-structural-growth',
    label: 'India as a structural growth sleeve',
    brief:
      'structural allocation to INDA and EPI rebalanced against ACWI with a currency-volatility filter, goal is long-horizon growth exposure with controlled drawdown',
    suggestedAssets: ['INDA', 'EPI', 'ACWI'],
  },
  {
    id: 'latam-terms-of-trade',
    label: 'Latin America on terms of trade',
    brief:
      'link a Latin America sleeve of EWZ ILF and EWW to a copper and oil trend signal, aiming to hold the region only while its commodity terms of trade improve',
    suggestedAssets: ['EWZ', 'ILF', 'EWW'],
  },
  {
    id: 'japan-yen-aware-equity',
    label: 'Japan equity, currency-aware',
    brief:
      'Japanese equity through EWJ and DXJ selected on the yen trend, targeting the local equity return without the currency drag',
    suggestedAssets: ['EWJ', 'DXJ'],
  },
  {
    id: 'europe-country-dispersion',
    label: 'Intra-Europe country dispersion',
    brief:
      'rotate among EZU EWG EWU and EWQ on relative value and momentum benchmarked to EFA, aiming for intra-Europe dispersion rather than a single country bet',
    suggestedAssets: ['EZU', 'EWG', 'EWU', 'EWQ'],
  },
  {
    id: 'frontier-satellite-capped',
    label: 'Frontier satellite, risk-capped',
    brief:
      'a small satellite in TUR EPOL and ECH capped at a strict share of portfolio risk with volatility targeting, goal is diversification from a low-correlation sleeve',
    suggestedAssets: ['TUR', 'EPOL', 'ECH'],
  },
  {
    id: 'global-all-weather-core',
    label: 'Genuinely global low-turnover core',
    brief:
      'global equity core from VT and VXUS paired with BNDX and rebalanced on drift bands, aiming for a genuinely global long-horizon portfolio with low turnover',
    suggestedAssets: ['VT', 'VXUS', 'BNDX'],
  },
  {
    id: 'asean-growth-basket',
    label: 'Southeast Asia, trend-weighted',
    brief:
      'basket of EWS EWM THD and VNM weighted by trend strength, targeting Southeast Asian growth with a momentum filter to avoid value traps',
    suggestedAssets: ['EWS', 'EWM', 'THD', 'VNM'],
  },
  {
    id: 'korea-taiwan-tech-cycle',
    label: 'Korea and Taiwan as the tech cycle',
    brief:
      'express the global technology hardware cycle through EWY and EWT with a SMH confirmation signal, goal is cyclical semiconductor exposure at index-level liquidity',
    suggestedAssets: ['EWY', 'EWT', 'SMH'],
  },
  {
    id: 'commodity-exporters-basket',
    label: 'Commodity-exporter equity basket',
    brief:
      'own EWC EWA and EZA as a commodity-exporter equity basket driven by a broad commodity trend in GSG, targeting resource-cycle upside through equities',
    suggestedAssets: ['EWC', 'EWA', 'EZA', 'GSG'],
  },

  // ── Crypto ───────────────────────────────────────────────────────────────
  {
    id: 'btc-eth-vol-target',
    label: 'Volatility-targeted BTC and ETH core',
    brief:
      'volatility-targeted BTC and ETH core that scales exposure down as realized volatility rises, goal is crypto upside at a risk level a treasury allocator can hold',
    suggestedAssets: ['BTC', 'ETH'],
  },
  {
    id: 'l1-relative-strength',
    label: 'Layer-one relative strength, BTC-gated',
    brief:
      'relative-strength rotation across SOL AVAX NEAR and SUI holding only the strongest two, gated by a BTC trend filter, aiming to capture layer-one dispersion without permanent altcoin beta',
    suggestedAssets: ['SOL', 'AVAX', 'NEAR', 'SUI'],
  },
  {
    id: 'defi-bluechip-basket',
    label: 'Blue-chip DeFi with a stable fallback',
    brief:
      'blue-chip DeFi basket of UNI AAVE and LDO sized by liquidity and volatility, with a stablecoin fallback whenever the sector trend breaks',
    suggestedAssets: ['UNI', 'AAVE', 'LDO'],
  },
  {
    id: 'crypto-stable-barbell',
    label: 'Stablecoin barbell with a crypto sleeve',
    brief:
      'barbell idle USDC against a small BTC and ETH sleeve rebalanced on drift bands, goal is to earn crypto upside while most of the balance stays in stable value',
    suggestedAssets: ['BTC', 'ETH'],
  },
  {
    id: 'btc-gold-debasement',
    label: 'BTC and gold as a debasement pair',
    brief:
      'pair BTC with GLD as a monetary-debasement hedge weighted by inverse volatility, aiming for a store-of-value sleeve that does not depend on one of them being right',
    suggestedAssets: ['BTC', 'GLD'],
  },
  {
    id: 'eth-yield-trend-filter',
    label: 'ETH with a staking-yield tilt',
    brief:
      'ETH-centric allocation with a staking-yield tilt and a trend filter that rotates into short treasuries in SHY when momentum turns negative',
    suggestedAssets: ['ETH', 'SHY'],
  },
  {
    id: 'crypto-momentum-drawdown-stop',
    label: 'Cross-sectional crypto momentum',
    brief:
      'cross-sectional momentum across BTC ETH SOL and LINK with a portfolio-level drawdown stop, aiming to run winners while capping the peak-to-trough loss',
    suggestedAssets: ['BTC', 'ETH', 'SOL', 'LINK'],
  },
  {
    id: 'compute-data-token-theme',
    label: 'Decentralised compute and data theme',
    brief:
      'thematic sleeve in RENDER FET and GRT capped by volatility and rebalanced monthly, goal is exposure to decentralised compute and data demand inside a hard risk budget',
    suggestedAssets: ['RENDER', 'FET', 'GRT'],
  },
  {
    id: 'rwa-tokenised-yield',
    label: 'Tokenised real-world-asset yield',
    brief:
      'allocate to ONDO alongside short-duration treasuries in BIL as a tokenised real-world-asset sleeve, targeting yield with a conventional backstop',
    suggestedAssets: ['ONDO', 'BIL'],
  },
  {
    id: 'crypto-basis-carry',
    label: 'Spot-versus-funding basis carry',
    brief:
      'harvest perpetual funding carry on BTC and ETH while holding the spot leg, with a hard deleverage rule when funding flips negative, goal is a market-neutral yield',
    suggestedAssets: ['BTC', 'ETH'],
  },
  {
    id: 'payments-token-utility',
    label: 'Settlement-rail token basket',
    brief:
      'utility-payments basket of XRP XLM and LTC behind a trend filter, aiming for exposure to settlement-rail adoption rather than undirected speculative beta',
    suggestedAssets: ['XRP', 'XLM', 'LTC'],
  },
  {
    id: 'modular-l2-rotation',
    label: 'Layer-two rotation, ETH-hedged',
    brief:
      'rotate across ARB OP and TIA on relative strength with an ETH beta hedge, targeting layer-two share shifts rather than directional crypto risk',
    suggestedAssets: ['ARB', 'OP', 'TIA', 'ETH'],
  },
  {
    id: 'crypto-equity-correlation-guard',
    label: 'Crypto sized by its equity correlation',
    brief:
      'hold BTC alongside SPY and cut the crypto sleeve whenever their rolling correlation rises above its trailing average, aiming to keep the diversification the position was bought for',
    suggestedAssets: ['BTC', 'SPY'],
  },
  {
    id: 'altcoin-mean-reversion',
    label: 'Altcoin mean reversion, stable default',
    brief:
      'short-horizon mean reversion on DOT ATOM and INJ after oversold stretches, with volatility-scaled sizing and a stablecoin default, goal is to trade dislocations rather than trends',
    suggestedAssets: ['DOT', 'ATOM', 'INJ'],
  },
  {
    id: 'majors-only-crypto-core',
    label: 'Majors-only crypto core',
    brief:
      'majors-only crypto core of BTC ETH and SOL rebalanced to fixed risk weights each month, goal is simple liquid beta with no thematic or long-tail exposure',
    suggestedAssets: ['BTC', 'ETH', 'SOL'],
  },
  {
    id: 'crypto-vol-regime-cash',
    label: 'Crypto held only in calm regimes',
    brief:
      'hold BTC and ETH only while realized volatility sits below its trailing median and hold BIL otherwise, targeting participation in calm regimes and absence in violent ones',
    suggestedAssets: ['BTC', 'ETH', 'BIL'],
  },

  // ── FX and macro ─────────────────────────────────────────────────────────
  {
    id: 'g10-carry-vol-filter',
    label: 'G10 carry with a volatility filter',
    brief:
      'G10 carry across AUD/USD NZD/USD and USD/JPY with a volatility filter that cuts the book when currency volatility spikes, aiming for carry income without the crash risk',
    suggestedAssets: ['AUD/USD', 'NZD/USD', 'USD/JPY'],
  },
  {
    id: 'dollar-trend-macro',
    label: 'Dollar trend as a macro sleeve',
    brief:
      'trend-following the dollar through EUR/USD USD/JPY and GBP/USD, goal is a low-correlation macro sleeve to sit alongside an equity core',
    suggestedAssets: ['EUR/USD', 'USD/JPY', 'GBP/USD'],
  },
  {
    id: 'em-fx-selective-carry',
    label: 'Selective EM carry with hard stops',
    brief:
      'selective emerging-market carry in USD/MXN USD/BRL and USD/ZAR with a hard stop on each leg, targeting yield inside an explicit tail-risk budget',
    suggestedAssets: ['USD/MXN', 'USD/BRL', 'USD/ZAR'],
  },
  {
    id: 'yen-risk-off-overlay',
    label: 'Yen overlay on an equity core',
    brief:
      'use USD/JPY and AUD/JPY as a risk-off overlay on an SPY core, sizing the yen leg up as equity volatility rises, aiming to cheapen the cost of hedging',
    suggestedAssets: ['USD/JPY', 'AUD/JPY', 'SPY'],
  },
  {
    id: 'commodity-currency-cycle',
    label: 'Commodity currencies on the metals cycle',
    brief:
      'trade AUD/USD and USD/CAD against a copper and oil trend signal, goal is to express the commodity cycle through currencies rather than futures',
    suggestedAssets: ['AUD/USD', 'USD/CAD'],
  },
  {
    id: 'eur-cross-mean-reversion',
    label: 'European cross mean reversion',
    brief:
      'mean reversion on EUR/CHF and EUR/GBP inside their trailing ranges with tight risk limits, aiming for a low-beta return stream uncorrelated with equities',
    suggestedAssets: ['EUR/CHF', 'EUR/GBP'],
  },
  {
    id: 'asia-fx-growth-proxy',
    label: 'Asian FX as a growth proxy',
    brief:
      'express Asian growth through USD/KRW USD/SGD and USD/INR with volatility-scaled sizing, targeting a diversifier that is not equity beta in disguise',
    suggestedAssets: ['USD/KRW', 'USD/SGD', 'USD/INR'],
  },
  {
    id: 'fx-hedged-international-equity',
    label: 'International equity, currency-hedged',
    brief:
      'hold EFA and hedge the currency using a trend signal on EUR/USD, goal is to keep the international equity return and drop the unrewarded currency volatility',
    suggestedAssets: ['EFA', 'EUR/USD'],
  },
  {
    id: 'nordic-carry-pair',
    label: 'Nordic carry pair, range-limited',
    brief:
      'trade EUR/NOK and EUR/SEK on carry and range signals with a strict per-leg loss limit, aiming for a small uncorrelated income sleeve',
    suggestedAssets: ['EUR/NOK', 'EUR/SEK'],
  },

  // ── Metals and commodities ───────────────────────────────────────────────
  {
    id: 'gold-real-rate-signal',
    label: 'Gold, only when real rates fall',
    brief:
      'own GLD and IAU only while real rates are falling and hold BIL otherwise, aiming for the gold return without the long flat stretches',
    suggestedAssets: ['GLD', 'IAU', 'BIL'],
  },
  {
    id: 'gold-miners-levered-gold',
    label: 'Gold miners as budgeted leverage',
    brief:
      'gold-miner exposure through GDX and GDXJ sized against a GLD core with volatility targeting, goal is levered gold upside with the drawdown explicitly budgeted',
    suggestedAssets: ['GDX', 'GDXJ', 'GLD'],
  },
  {
    id: 'silver-monetary-industrial',
    label: 'Silver as monetary and industrial metal',
    brief:
      'blend SLV with SIL and a copper sleeve in COPX, aiming to capture both the monetary and the industrial legs of the precious-metals complex',
    suggestedAssets: ['SLV', 'SIL', 'COPX'],
  },
  {
    id: 'copper-electrification',
    label: 'Copper as an electrification proxy',
    brief:
      'own COPX and XME behind a trend filter as an electrification and grid-capex proxy with a cash fallback, targeting the structural copper deficit without holding through the cycle trough',
    suggestedAssets: ['COPX', 'XME'],
  },
  {
    id: 'broad-commodity-inflation',
    label: 'Broad commodities measured in real terms',
    brief:
      'broad commodity exposure through DBC PDBC and GSG rebalanced against TIP, goal is an inflation hedge judged on real return rather than nominal',
    suggestedAssets: ['DBC', 'PDBC', 'GSG', 'TIP'],
  },
  {
    id: 'energy-term-structure',
    label: 'Crude on term structure and trend',
    brief:
      'trade USO and BNO on term-structure and trend signals with a strict stop, aiming to earn roll yield while the curve is backwardated and stay flat when it is not',
    suggestedAssets: ['USO', 'BNO'],
  },
  {
    id: 'agriculture-supply-shock',
    label: 'Agriculture as supply-shock convexity',
    brief:
      'agricultural basket of CORN WEAT SOYB and DBA with volatility-scaled sizing, targeting supply-shock convexity as a diversifier from financial assets',
    suggestedAssets: ['CORN', 'WEAT', 'SOYB', 'DBA'],
  },
  {
    id: 'natgas-seasonality',
    label: 'Natural gas seasonality, trend-confirmed',
    brief:
      'seasonal exposure to UNG with a trend confirmation and a hard risk cap, goal is to trade the winter demand pattern without carrying contango all year',
    suggestedAssets: ['UNG'],
  },
  {
    id: 'uranium-nuclear-buildout',
    label: 'Uranium buildout with a defined exit',
    brief:
      'own URA behind a trend filter as a nuclear-buildout proxy with a treasury fallback in SHY, aiming for thematic upside and a defined exit',
    suggestedAssets: ['URA', 'SHY'],
  },
  {
    id: 'metals-risk-share-diversifier',
    label: 'Precious metals sized by risk share',
    brief:
      'blend GLD PPLT and PALL as a precious-metals sleeve sized to a fixed share of portfolio risk alongside SPY, targeting diversification measured in risk rather than dollars',
    suggestedAssets: ['GLD', 'PPLT', 'PALL', 'SPY'],
  },
  {
    id: 'industrial-metals-cycle',
    label: 'Industrial metals on the growth cycle',
    brief:
      'hold DBB and XME while global growth momentum is positive and rotate to SHV when it turns, goal is a cyclical metals sleeve with a systematic off switch',
    suggestedAssets: ['DBB', 'XME', 'SHV'],
  },
  {
    id: 'timber-soft-commodity-mix',
    label: 'Timber and softs as a real-asset mix',
    brief:
      'combine WOOD and MOO with a DBA sleeve as a real-asset basket rebalanced quarterly, targeting inflation-linked return from outside the energy complex',
    suggestedAssets: ['WOOD', 'MOO', 'DBA'],
  },

  // ── Fixed income and credit ──────────────────────────────────────────────
  {
    id: 'duration-barbell',
    label: 'Duration barbell on rate momentum',
    brief:
      'barbell TLT against BIL with the duration weight set by a rate-momentum signal, goal is to own duration while it is being rewarded and stay short when it is not',
    suggestedAssets: ['TLT', 'BIL'],
  },
  {
    id: 'credit-quality-rotation',
    label: 'Credit taken only when it pays',
    brief:
      'rotate between LQD and HYG on credit-spread momentum with a GOVT fallback, aiming to take credit risk only while it is being paid for',
    suggestedAssets: ['LQD', 'HYG', 'GOVT'],
  },
  {
    id: 'floating-rate-carry',
    label: 'Floating-rate carry, spread-stopped',
    brief:
      'floating-rate carry from BKLN and FLOT with a credit-spread stop, targeting income insulated from rate volatility',
    suggestedAssets: ['BKLN', 'FLOT'],
  },
  {
    id: 'breakeven-aware-inflation-ladder',
    label: 'Inflation protection priced on breakevens',
    brief:
      'ladder TIP and VTIP against nominal treasuries in IEF with the weight set by the breakeven spread, goal is to hold inflation protection only while it is cheap',
    suggestedAssets: ['TIP', 'VTIP', 'IEF'],
  },
  {
    id: 'municipal-tax-efficient-income',
    label: 'Municipal income with a duration cap',
    brief:
      'tax-efficient income from MUB and VTEB under a duration cap, aiming for stable after-tax yield with a shallow drawdown',
    suggestedAssets: ['MUB', 'VTEB'],
  },
  {
    id: 'em-debt-dollar-gated',
    label: 'EM debt carry, dollar-gated',
    brief:
      'emerging-market debt carry from EMB and EMLC gated by a dollar-trend filter, targeting hard-currency and local-currency yield with a currency-shock guard',
    suggestedAssets: ['EMB', 'EMLC'],
  },
  {
    id: 'fallen-angel-credit',
    label: 'High-yield carry with a systematic exit',
    brief:
      'own ANGL and JNK while credit momentum is positive and rotate to SHY when it turns, aiming for high-yield carry with a systematic exit',
    suggestedAssets: ['ANGL', 'JNK', 'SHY'],
  },
  {
    id: 'mortgage-spread-carry',
    label: 'Mortgage and corporate spread carry',
    brief:
      'carry sleeve in MBB and VCIT measured against GOVT and sized on the spread level, goal is to be paid for prepayment and credit risk rather than simply assume it',
    suggestedAssets: ['MBB', 'VCIT', 'GOVT'],
  },
  {
    id: 'short-duration-cash-plus',
    label: 'Cash-plus ladder for idle USDC',
    brief:
      'cash-plus ladder from BIL SHV and MINT for idle USDC, targeting money-market-like stability with a modest yield pickup',
    suggestedAssets: ['BIL', 'SHV', 'MINT'],
  },
  {
    id: 'curve-steepener-expression',
    label: 'Curve steepener with no net duration',
    brief:
      'express a curve steepener with VGSH against VGLT rebalanced on the two-year versus ten-year spread, aiming for a rates view with no net duration',
    suggestedAssets: ['VGSH', 'VGLT'],
  },
  {
    id: 'global-bond-diversification',
    label: 'Globally diversified defensive sleeve',
    brief:
      'diversify a bond core with BNDX and BWX alongside AGG in a currency-aware way, goal is to reduce single-country rate risk in the defensive sleeve',
    suggestedAssets: ['BNDX', 'BWX', 'AGG'],
  },
  {
    id: 'preferred-income-beta-aware',
    label: 'Preferreds with their equity beta priced',
    brief:
      'income from PFF sized against its measured equity beta with an HYG cross-check, aiming for high current yield with the hidden equity risk made explicit',
    suggestedAssets: ['PFF', 'HYG'],
  },
  {
    id: 'intermediate-core-ballast',
    label: 'Intermediate treasury ballast',
    brief:
      'hold VGIT and IEF as intermediate treasury ballast beside an equity core, sized so the bond sleeve carries a target share of total portfolio risk',
    suggestedAssets: ['VGIT', 'IEF'],
  },

  // ── Multi-asset and risk parity ──────────────────────────────────────────
  {
    id: 'risk-parity-four-quadrant',
    label: 'Four-quadrant risk parity',
    brief:
      'risk parity across SPY TLT GLD and DBC with volatility targeting, goal is a portfolio that does not depend on one macro regime being right',
    suggestedAssets: ['SPY', 'TLT', 'GLD', 'DBC'],
  },
  {
    id: 'sixty-forty-modernised',
    label: 'A modernised sixty-forty',
    brief:
      'modernise a sixty-forty by replacing part of AGG with TIP and part of SPY with USMV, targeting the same return with less rate and drawdown risk',
    suggestedAssets: ['SPY', 'AGG', 'TIP', 'USMV'],
  },
  {
    id: 'trend-plus-carry-multi-asset',
    label: 'Cross-asset trend plus carry',
    brief:
      'combine cross-asset trend on SPY TLT GLD and DBC with a carry sleeve, aiming for two weakly correlated return sources inside one book',
    suggestedAssets: ['SPY', 'TLT', 'GLD', 'DBC'],
  },
  {
    id: 'all-weather-inflation-aware',
    label: 'All-weather with an inflation tilt',
    brief:
      'all-weather allocation across VTI VGLT GLD and PDBC with an inflation-regime tilt, goal is stable real return across growth and inflation surprises',
    suggestedAssets: ['VTI', 'VGLT', 'GLD', 'PDBC'],
  },
  {
    id: 'endowment-style-diversified',
    label: 'Endowment-style breadth, annual rebalance',
    brief:
      'endowment-style mix of VT VNQ DBC and BND rebalanced annually on drift bands, targeting long-horizon growth with genuine asset-class breadth',
    suggestedAssets: ['VT', 'VNQ', 'DBC', 'BND'],
  },
  {
    id: 'drawdown-controlled-growth',
    label: 'Global growth under a drawdown cap',
    brief:
      'growth portfolio from VTI and VXUS with a portfolio-level drawdown control that shifts into SHY, aiming to keep the equity premium while capping the worst year',
    suggestedAssets: ['VTI', 'VXUS', 'SHY'],
  },
  {
    id: 'macro-regime-switch',
    label: 'One position per macro regime',
    brief:
      'switch between a risk-on sleeve of QQQ and EEM and a risk-off sleeve of TLT and GLD on a growth and inflation signal, goal is one position per regime rather than a static blend',
    suggestedAssets: ['QQQ', 'EEM', 'TLT', 'GLD'],
  },
  {
    id: 'correlation-aware-diversifier',
    label: 'Weights set by rolling correlation',
    brief:
      'build a diversified book from SPY IEF GLD and BTC with weights set by rolling correlation rather than fixed percentages, aiming for diversification that survives a correlation shock',
    suggestedAssets: ['SPY', 'IEF', 'GLD', 'BTC'],
  },
  {
    id: 'permanent-portfolio-updated',
    label: 'Permanent portfolio, wide bands',
    brief:
      'update the permanent portfolio with VTI VGLT GLD and BIL rebalanced on wide drift bands, targeting resilience with very low turnover',
    suggestedAssets: ['VTI', 'VGLT', 'GLD', 'BIL'],
  },
  {
    id: 'minimum-variance-multi-asset',
    label: 'Minimum variance with covariance shrinkage',
    brief:
      'minimum-variance allocation across SPY EFA AGG and GLD with shrinkage applied to the covariance estimate, goal is the lowest-volatility portfolio that still earns a real return',
    suggestedAssets: ['SPY', 'EFA', 'AGG', 'GLD'],
  },
  {
    id: 'cash-plus-satellite',
    label: 'Cash core with capped satellites',
    brief:
      'hold a BIL core with satellite sleeves in QQQ and BTC capped by a strict risk budget, aiming for asymmetric upside on idle USDC without risking the principal base',
    suggestedAssets: ['BIL', 'QQQ', 'BTC'],
  },
  {
    id: 'rebalancing-premium-harvest',
    label: 'Harvest the rebalancing premium',
    brief:
      'harvest the rebalancing premium between SPY GLD and TLT using volatility-scaled drift bands, goal is return from rebalancing itself rather than from a directional call',
    suggestedAssets: ['SPY', 'GLD', 'TLT'],
  },
  {
    id: 'equity-bond-correlation-regime',
    label: 'Ballast chosen by the stock-bond correlation',
    brief:
      'choose between IEF and GLD as the ballast for an SPY core based on the rolling stock-bond correlation, aiming to keep a hedge that is actually hedging',
    suggestedAssets: ['SPY', 'IEF', 'GLD'],
  },
  {
    id: 'inverse-volatility-weighting',
    label: 'Inverse-volatility multi-asset weights',
    brief:
      'weight VTI VEA EMB and GLD by inverse realized volatility with a monthly rebalance, targeting equal risk contribution from each sleeve',
    suggestedAssets: ['VTI', 'VEA', 'EMB', 'GLD'],
  },

  // ── Volatility and tail risk ─────────────────────────────────────────────
  {
    id: 'tail-hedge-budgeted',
    label: 'Budgeted long-volatility tail hedge',
    brief:
      'hold SPY with a budgeted long-volatility sleeve in VXX capped at a small share of the portfolio, aiming to cut the worst drawdown at a known annual cost',
    suggestedAssets: ['SPY', 'VXX'],
  },
  {
    id: 'short-vol-carry-guarded',
    label: 'Variance premium with a hard cap',
    brief:
      'harvest the variance risk premium through SVXY under a hard exposure cap and a SPY trend gate, goal is to earn volatility carry without an uncapped tail',
    suggestedAssets: ['SVXY', 'SPY'],
  },
  {
    id: 'vol-target-equity-core',
    label: 'Stable risk, not stable weight',
    brief:
      'volatility-targeted SPY core that scales exposure with realized volatility, targeting a stable risk level rather than a stable portfolio weight',
    suggestedAssets: ['SPY'],
  },
  {
    id: 'crisis-alpha-trend',
    label: 'Crisis alpha sized as insurance',
    brief:
      'crisis-alpha sleeve built from cross-asset trend on TLT GLD and USD/JPY intended to gain when equities fall, sized as insurance rather than as a return driver',
    suggestedAssets: ['TLT', 'GLD', 'USD/JPY'],
  },
  {
    id: 'sector-vol-dispersion',
    label: 'Sector volatility dispersion',
    brief:
      'trade the dispersion between XLK and XLU realized volatility around a market-neutral equity core, aiming for a return stream uncorrelated with market direction',
    suggestedAssets: ['XLK', 'XLU'],
  },
  {
    id: 'drawdown-triggered-derisk',
    label: 'Rules-based de-risking in a bear market',
    brief:
      'systematically de-risk from QQQ into SHV when a rolling drawdown threshold is breached and re-risk on a trend confirmation, goal is a rules-based response to a bear market',
    suggestedAssets: ['QQQ', 'SHV'],
  },
  {
    id: 'convexity-barbell-cash',
    label: 'Cash base with a convex sleeve',
    brief:
      'barbell a large BIL cash base against a small convex sleeve in UVXY, targeting positive skew rather than the highest expected return',
    suggestedAssets: ['BIL', 'UVXY'],
  },
  {
    id: 'volatility-regime-allocation',
    label: 'Allocation set by the volatility regime',
    brief:
      'set the split between SPY and IEF from the prevailing volatility regime rather than a fixed weight, aiming to hold less equity precisely when equity risk is expensive',
    suggestedAssets: ['SPY', 'IEF'],
  },

  // ── Income and capital preservation ──────────────────────────────────────
  {
    id: 'retirement-income-glidepath',
    label: 'Income glidepath that shortens duration',
    brief:
      'income-first allocation from SCHD VYM and VCSH on a glidepath that shortens duration over time, targeting a durable withdrawal rate',
    suggestedAssets: ['SCHD', 'VYM', 'VCSH'],
  },
  {
    id: 'dividend-plus-vol-overlay',
    label: 'Dividend core with a volatility overlay',
    brief:
      'income from a dividend core in DGRO and VIG combined with a short-volatility overlay, aiming for higher current yield than the underlying at a similar drawdown',
    suggestedAssets: ['DGRO', 'VIG'],
  },
  {
    id: 'capital-preservation-usdc',
    label: 'Preservation first for idle USDC',
    brief:
      'capital preservation for idle USDC using SHV BIL and MINT under a strict drawdown limit, goal is stability first and yield second',
    suggestedAssets: ['SHV', 'BIL', 'MINT'],
  },
  {
    id: 'inflation-aware-income',
    label: 'Income that clears inflation',
    brief:
      'income portfolio from VYM TIP and VNQ weighted to hold real yield above inflation, targeting purchasing-power-protected cash flow',
    suggestedAssets: ['VYM', 'TIP', 'VNQ'],
  },
  {
    id: 'conservative-balanced-usdc',
    label: 'Conservative balanced allocation',
    brief:
      'conservative balanced allocation of USMV AGG and GLD for idle USDC, aiming for a smoother path than equities with more return than cash',
    suggestedAssets: ['USMV', 'AGG', 'GLD'],
  },
  {
    id: 'yield-with-liquidity-floor',
    label: 'Yield under a hard liquidity floor',
    brief:
      'yield-seeking sleeve in LQD and PFF constrained by a hard liquidity floor held in BIL, goal is income that can be exited without a fire sale',
    suggestedAssets: ['LQD', 'PFF', 'BIL'],
  },
  {
    id: 'global-income-diversified',
    label: 'Globally diversified income',
    brief:
      'diversified global income from VYM VXUS and EMB under a currency and credit risk cap, targeting yield that is not concentrated in one market',
    suggestedAssets: ['VYM', 'VXUS', 'EMB'],
  },
  {
    id: 'low-turnover-tax-aware',
    label: 'After-tax compounding, wide bands',
    brief:
      'low-turnover tax-aware core of ITOT and MUB with wide rebalancing bands, aiming to maximise after-tax compounding rather than pre-tax return',
    suggestedAssets: ['ITOT', 'MUB'],
  },
  {
    id: 'laddered-treasury-growth-kicker',
    label: 'Treasury ladder with a growth kicker',
    brief:
      'ladder SHY IEF and VGIT beside a small SPY sleeve, targeting predictable income with a modest growth kicker',
    suggestedAssets: ['SHY', 'IEF', 'VGIT', 'SPY'],
  },
  {
    id: 'drawdown-capped-income',
    label: 'Income with a credit drawdown cap',
    brief:
      'income from SCHD and HYG under a portfolio drawdown cap that rotates into GOVT, goal is yield that does not surrender its own coupon in a credit selloff',
    suggestedAssets: ['SCHD', 'HYG', 'GOVT'],
  },
  {
    id: 'municipal-plus-equity-sleeve',
    label: 'Municipal core with an equity sleeve',
    brief:
      'municipal core in VTEB with a small VOO equity sleeve rebalanced on drift bands, targeting tax-aware income with a measured growth allocation',
    suggestedAssets: ['VTEB', 'VOO'],
  },
  {
    id: 'stable-value-with-gold-floor',
    label: 'Stable value with a gold floor',
    brief:
      'hold SHV as stable value with a fixed GLD floor to hedge currency debasement, goal is preservation in both nominal and real terms',
    suggestedAssets: ['SHV', 'GLD'],
  },
]

export default SURPRISE_BRIEFS
