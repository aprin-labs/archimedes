# Writing a brief

> **status:** current
> **owner:** Dan Browne
> **updated:** 2026-09-02
> **superseded-by:** —

The long-form guide to prompting the Generate page. The app itself carries a one-line hint
and a link here; this is the version with the reasoning, the failure modes, and the worked
examples. If you are looking for *what the endpoint accepts*, that is
[`api/generation.md`](api/generation.md); if you are looking for *what a brief may not
contain* and why yours was refused, that is [`brief-guidelines.md`](brief-guidelines.md).
This doc is about what to type.

## 1. The three parts of a brief that works

A brief is free text. It is read by an LLM validator, then by the retrieval and fusion
stages that pick which q-fin papers the strategy is built from. Text that names nothing
concrete gives those stages nothing to retrieve against, and the result is a generic
portfolio that happens to be legal rather than one that answers your question.

Every brief in the in-app bank (`ui/src/data/surpriseBriefs.js`) names three things:

| Part | What it does | Weak | Strong |
| --- | --- | --- | --- |
| **Assets or classes** | Steers the universe the backtest runs on | "some stocks" | "SPY, TLT and SCHD"; "US sector ETFs"; "G10 currency pairs" |
| **A mechanism** | Steers which papers get retrieved | "make money" | "momentum", "volatility-managed sizing", "carry", "mean reversion", "trend-following", "risk parity" |
| **A goal** | Steers the objective the sizing stage optimises for | "good returns" | "capital preservation"; "income above inflation"; "equity-like return with a shallower drawdown" |

Read as a sentence:

> low-volatility income portfolio from SPY TLT and SCHD focused on capital preservation

Assets (`SPY TLT SCHD`), mechanism (`low-volatility income`), goal (`capital
preservation`). That is the whole shape.

## 2. What the system does with each part

- **Assets** are resolved against the supported universe
  ([`asset-universe.md`](asset-universe.md), and the picker's own copy at
  `ui/src/data/assetUniverse.js`). Naming symbols in the brief is not the same as selecting
  them under *Advanced options* — the picker is a hard constraint on the universe, the brief
  is a signal. Use the picker when you mean "only these"; use the brief when you mean "this
  kind of thing".
- **Mechanism** words are what the corpus retrieval matches on. Retrieval is a keyword /
  asset-class pre-filter followed by a query-time rerank, and **there is no stored embedding
  index** — prod serves the lexical TF-IDF path
  ([`architecture.md`](architecture.md) § Corpus retrieval, issue #778). So the words you
  choose are matched close to literally: "vol-managed" and "volatility-managed" are not
  interchangeable to it. Prefer the term the literature uses.
- **Goal** language reaches the sizing and rigor stages. "Capital preservation" and
  "maximise return" produce genuinely different position sizes from the same candidate
  method.

## 3. Failure modes, in the order people hit them

1. **Too vague to retrieve.** "I want to make money with low risk" names no asset, no
   mechanism, and a goal so generic it constrains nothing. It will usually generate — and
   the output will be forgettable.
2. **A goal with no mechanism.** "Beat the S&P with less risk" states an outcome, not a
   method. Add the *how*: "…using a momentum screen with volatility-managed sizing".
3. **An asset list with no thesis.** "SPY QQQ IWM GLD TLT BTC" is a universe, not a brief.
   Say what you want done with them.
4. **Asking for a specific number.** "Get me a Sharpe above 2" is not a mechanism, and the
   rigor gate exists precisely to stop that from being an instruction. Ask for a property
   (drawdown-capped, income-first, market-neutral), not a metric target.
5. **Off-topic or adversarial text.** Two different things now, since #1801. *Adversarial*
   text — instructions aimed at the model, links, code fences, encoded blobs, forged JSON
   replies — and obviously-junk text — empty, a few characters, keyboard mash, over 600
   characters — are caught before the paywall and for free, by the deterministic screen
   (`backend/archimedes/services/brief_screen.py`); the full rule list and its reason codes
   are [`brief-guidelines.md`](brief-guidelines.md). *Off-topic but grammatical* text (a
   recipe) still reaches the model validator and is rejected after the credit is spent.
   The screen stays deliberately permissive about *unfamiliar* vocabulary: "muni ladder",
   "XIU.TO" and non-English text all pass it.

## 4. Length and specificity

One or two sentences. The bank entries run roughly 90–200 characters, and that is a
deliberate ceiling rather than an accident: past a couple of clauses, briefs start
specifying implementation details ("rebalance on the third Friday") that the pipeline
either ignores or, worse, over-fits toward. State the intent and let the fusion stage
choose the mechanics.

The **hard** limit is 600 characters, enforced by the request schema, by the screen, and by
the textarea's own counter — roughly three times the longest bank entry, so it bites only
on a paste. See [`brief-guidelines.md` § 2](brief-guidelines.md).

## 5. Worked upgrades

| Before | Why it is weak | After |
| --- | --- | --- |
| "crypto portfolio" | no mechanism, no goal | "volatility-targeted BTC and ETH core that scales exposure down as realized volatility rises, goal is crypto upside at a risk level a treasury allocator can hold" |
| "safe income" | no assets, no mechanism | "cash-plus ladder from BIL SHV and MINT for idle USDC, targeting money-market-like stability with a modest yield pickup" |
| "gold" | one asset, nothing else | "own GLD and IAU only while real rates are falling and hold BIL otherwise, aiming for the gold return without the long flat stretches" |
| "sector rotation" | mechanism only | "rotate monthly into the strongest of the US sector ETFs such as XLK XLE and XLF on six-month momentum, falling back to XLU when nothing is trending" |

## 6. The Surprise Me bank

The Generate page has no example list. Pressing **Surprise me** fills the box with one
entry drawn from `ui/src/data/surpriseBriefs.js` (124 entries as of 2026-08-31), and
consecutive presses never repeat.

Two honest notes about that bank, because it is easy to over-read:

- **Only three entries have been run through the live pipeline** — the dogfood-proven
  carry-overs from the 2026-07-04 bake-off ([PR #875](https://github.com/aprin-labs/archimedes/pull/875)).
  Their per-entry status comments say so in the file.
- **The rest are curated copy.** They are written to the shape in § 1, machine-checked
  against `cheap_brief_reject` (`backend/tests/test_surprise_briefs_quality.py`) and against
  the supported asset universe (`ui/test/surprise-briefs.test.js`), and read for finance
  literacy. Passing those checks means "well-formed", not "profitable" and not "will clear
  the rigor gate". Nothing in the bank is a recommendation, and none of it is a backtest
  result.

Adding an entry: match the shape in § 1, keep `suggestedAssets` to symbols the brief text
itself names, and run both tests above. Do not add a `DOGFOOD PROVEN` comment to an entry
that has not actually been generated live.
