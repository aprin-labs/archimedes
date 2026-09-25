# Interpreter Unification — collapsing the two DSL interpreters into one

> **status:** draft
> **owner:** Dan Browne
> **updated:** 2026-08-30
> **superseded-by:** —

**Design only. No engine code is proposed for this commit** — this document is the
inventory, the architecture call, and the migration ratchet. Every behavioural claim
below is either cited to `file:line` or marked with the evidence that produced it.

---

## 0. The problem, stated exactly

One strategy language (`backend/archimedes/services/strategy_dsl.py`) has **two
interpreters**:

| | **Interpreter A** | **Interpreter B** |
|---|---|---|
| Entry point | `dsl_to_backtrader.interpret_spec` (`dsl_to_backtrader.py:114-242`) | `strategy_signal_evaluator._spec_signal` → `_replay_position_state` (`strategy_signal_evaluator.py:1153-1370`) |
| Engine | backtrader `Strategy` inside a full `Cerebro` + broker | pure Python / numpy / pandas, **no backtrader** |
| Consumers | rigor gate + published metrics (`fusion_evaluator.run_dsl_backtest:207-325`), paper ledger replay (`paper_trading.replay_spec:69-106`) | agent tick (`chain/agent_runner.py:385-390`), marketplace tick (`marketplace/service.py:449-458`) |
| Costs | commission 10 bps + slippage 5 bps (`fusion_evaluator.py:159-168, 250-252`) | **none** |
| Fills | broker fills at next bar's open | none — emits a target weight |
| Output | equity curve, Sharpe, DSR/PBO inputs, trade stats | `AssetSignal(signal, weight)` |
| Shared code | `_eval_condition`, `rebalance_period_bars` — that is all (`strategy_signal_evaluator.py:1173`) | ditto |

`paper_store.py:1-8` states the consequence in the repo's own words: a paper deployment
is a forward run of *the backtest engine's semantics* — **"NOT the live signal evaluator:
the divergence audit (F2/F3) established it grades a different strategy."**

Parity is held today only by **best-effort tests** — `backend/tests/test_interpreter_parity.py`.
That file is unusually honest about its own scope (`test_interpreter_parity.py:12-53`): it
pins some things equal, some convergent, one asymmetric, and explicitly declares two
classes of divergence **out of scope**. It is a tether between two implementations, not a
proof that there is one.

```
                    ┌──────────────────── ONE DSL ────────────────────┐
                    │  strategy_dsl.validate_strategy_spec            │
                    └───────────────┬─────────────────┬───────────────┘
                                    │                 │
                  ┌─────────────────▼──────┐   ┌──────▼──────────────────┐
                  │ A · interpret_spec     │   │ B · _replay_position_   │
                  │   backtrader Strategy  │   │     state / _spec_signal│
                  │   + Cerebro + broker   │   │   pure-python FSM       │
                  └───────┬────────┬───────┘   └──────────┬──────────────┘
                          │        │                      │
              rigor gate ─┘        └─ paper ledger        └─ agent tick / marketplace
              (PUBLISHED numbers)     (user track record)    (REAL vault trades)
                          ╲                                 ╱
                           ╲     ~ best-effort tests ~     ╱
                            ╲── test_interpreter_parity ──╱
```

The three surfaces that matter most to the product — the number we publish, the track
record we show, and the trade we actually place — do not all come from the same code.

---

## 1. Divergence inventory

Evidence key — **M** = measured on this branch (script + output in §1.9); **C** = read
directly from the cited code; **R** = already documented in-repo.

> **Sequencing note, added post-hoc 2026-08-30.** A same-day sibling branch,
> `dbrowneup/dsl-hardening-part1` (`c9b88e2b`, "make the DSL's enum surface honest: real
> equal_weight/inverse_vol, realized_vol, audible rejections"), is already rewriting four
> of the five files this doc's own Appendix file map lists for Interpreter A —
> `dsl_to_backtrader.py`, `strategy_dsl.py`, `fusion_evaluator.py`, `paper_trading.py` —
> ahead of and independently from the Step 0–8 ratchet in §3. It was not sequenced against
> this plan (neither branch references the other) and has not merged to `main` as of this
> writing, so the table below still describes `main`'s current behaviour; the three rows
> marked **in-flight** are where that sibling branch changes the picture once it lands.
> Specifics:
>
> * **D11 closes, but by hand-duplication, not by the shared core this doc argues for.**
>   The sibling adds `RealizedVolAnnualized`, a `bt.Indicator` that reproduces the live
>   evaluator's `prices.pct_change().tail(N).std() * sqrt(252)` bit-for-bit (sample std,
>   ddof=1) — a second, independent implementation of the same formula, not an extraction
>   into one. It does move the parity suite's D11/F6 pin from asymmetric to pinned-equal,
>   so it satisfies the letter of the §3 ratchet rule ("retire a divergence, move a line in
>   the docstring") without going through `core.indicators`. Whichever step eventually
>   extracts indicators (§2's Option (b) cost discussion, b1/b2) inherits a **third**
>   `realized_vol` implementation to reconcile, not a second.
> * **It adds two real sizing types Step 3 does not scope.** `equal_weight` and
>   `inverse_vol` previously fell through to full-invest on the A side (dead enum members);
>   the sibling gives both real behaviour — a per-slot `1/N` weight via a new
>   `universe_slots` parameter, with `inverse_vol` scaling that slot by
>   `reference_vol_annual / realized_vol` through the *same* `_sizing_realized_vol` helper
>   and the *same* `_VOL_SCALE_CAP = 2.0` that `volatility_target` uses. Step 3 below was
>   scoped as "four decisions about `volatility_target`" — window, statistic, cap, timing —
>   before this landed. It now inherits `equal_weight`/`inverse_vol` too, since they share
>   the exact formula D3/D4 are about.
> * **The cap is applied to the scale factor, not to the final weight — "clamping scale
>   pre-slot."** `inverse_vol`'s multiplier (`reference_vol / realized_vol`, capped at
>   2.0×) is computed and clamped *before* it is multiplied into the per-slot weight, so a
>   small slot (say 1/10th of the universe) times a capped 2.0× can still land well under
>   1.0 while a near-full slot at the same cap can approach or exceed it. Whether the cap
>   should instead bound the *final* target weight is a live-side design question, not
>   answered by the A-side code alone.
> * **Timing inherits D4 unexamined.** `inverse_vol`, like `volatility_target`, is only
>   reachable from `_enter_position()`'s not-in-market branch — sized once at entry, frozen
>   until exit. The D4 call ("entry-only vs per-decision-bar", Step 3) now has to be made
>   for `inverse_vol` too, and there is no live-side twin yet to compare against: as of this
>   commit `strategy_signal_evaluator.py` still treats `equal_weight` and `inverse_vol` as
>   plain full-exposure-when-in-market (`:1361-1362`, "inverse_vol cross-asset sizing is an
>   aggregation concern"). The sibling's own commit message flags a live-side sizing-parity
>   change as a possible follow-up without committing to one — genuinely open, not settled.
>
> None of this changes the recommendation in §2 or the step ordering in §3; it changes what
> "done" means for Step 3 once that sibling branch merges, and it means D3/D4/D11 below are
> mid-flight rather than static facts about `main`.

| # | Divergence | A | B | Pinned? | Ev |
|---|---|---|---|---|---|
| D1 | Transaction costs | 10 bps commission + 5 bps slippage on every fill | **none at all** | no | C |
| D2 | Execution timing | order on bar *t*, fill at *t+1* open | target weight *as of bar t*, acted on same tick | test-local fudge | C/R |
| D3 | Vol-target formula | 20-bar **RMS** of returns, cap **2.0×** | 22-bar **sample stdev**, cap **1.0×** | "deliberately out" · **in-flight, scope widened** | C |
| D4 | Vol-target *timing* | sized **once at entry**, frozen until exit | **recomputed every decision bar** | **no** · **in-flight, scope widened** | **M** |
| D5 | Exposure & rounding | `int(cash × 0.99 / price)` — 1% drag + floor | `weight = 1.0` exactly | no | C |
| D6 | OHLV operands | real `open/high/low/volume` from the feed | **`open=high=low=close`, `volume=0.0`** | **no** | **M** |
| D7 | Missing data | fail-closed (`PaperReplayError`); NaN poisons bt indicators | fail-soft: gaps compressed, empty tickers dropped silently | no | C |
| D8 | Portfolio construction | independent dollar sleeves, equity-weighted | cross-strategy vote average, renormalised to `1 − usdc_floor` | no | C |
| D9 | Cadence **phase anchor** | first post-warm-up bar of a **fixed** window | first post-warm-up bar of a **rolling 2y** window → phase drifts 1 bar/day | known limit | R |
| D10 | Position membership | sees the true entry bar | entry older than the window's left edge is invisible | known limit | R |
| D11 | `realized_vol` | **raises `DSLError`** | computes | pinned asymmetric · **in-flight, closing by duplication** | R |
| D12 | EMA seeding | SMA seed | first-value `ewm` seed | pinned convergent | R |
| D13 | Spec-less strategies | no counterpart | `_get_evaluator` → **always-long buy-and-hold** | no | C |
| D14 | Variant grid | `interpret_variant` expands a parameter grid | no twin — base spec only | no | C |

Detail on the ones that are neither pinned nor documented follows.

### 1.1 D1 — costs exist on one side only

`run_dsl_backtest` charges the broker (`fusion_evaluator.py:250-252`):

```python
cerebro.broker.setcommission(commission=tx_cost_bps / 10_000)   # _DEFAULT_TX_BPS = 10
if slippage_bps > 0:
    cerebro.broker.set_slippage_perc(perc=slippage_bps / 10_000) # DEFAULT_SLIPPAGE_BPS = 5
```

B has no broker and no cost model. The nearest thing on the live side is
`_DRIFT_THRESHOLD = 0.15` (`chain/agent_runner.py:155`), and its own comment is careful
to say what it is *not*: "a cost/no-op filter on an ALREADY cadence-gated target — it does
not, and must not, decide WHEN a strategy is allowed to change its mind"
(`agent_runner.py:149-154`). A 15% no-trade band and a 15 bps round-trip cost are
different objects: the band **suppresses trades the graded run takes at full cost**, and
charges nothing for the trades it does let through.

Consequence: a graded Sharpe and a live-realised return are not comparable quantities, and
nothing in the code or the UI says so.

### 1.2 D2 — execution offset is currently a test-local convention

`test_interpreter_parity.py:32-36` states it plainly: backtrader "submits the order on the
decision bar and the broker fills it at the next bar's open", so the helpers compare
`bt_path[t + 1]` against the live decision at bar `t` (`:266-267`, `:332`, `:376`). The
docstring calls this "an execution convention, not an interpreter divergence" — correct,
but it lives *only* in a test helper. Neither interpreter declares an execution model, so
nothing stops one side's convention from changing without the other's.

The same fixture also had to dial `exposure_fraction` down to 0.5 to stop **margin
rejections** from corrupting the comparison (`test_interpreter_parity.py:270-274`) — i.e.
A's observable position depends on broker state that B has no concept of.

### 1.3 D3 + D4 — position sizing diverges in *formula* and in *when it is applied*

Formula (`dsl_to_backtrader.py:216-232` vs `strategy_signal_evaluator.py:1343-1359`):

| | A | B |
|---|---|---|
| window | 20 bars | 22 bars |
| statistic | `(Σr²/n)^0.5` — RMS, **not mean-centred** | `pct_change().tail(22).std()` — sample stdev |
| cap | `min(target/realized, **2.0**)` — leverage allowed | `min(annual_pct/realized, **1.0**)` — never levered |
| insufficient data | `len(self) > 20` else **full invest** | falls through the `max_period + 1` guard → FLAT |

`test_interpreter_parity.py:37-41` declares this "DELIBERATELY OUT".

**D4 is the part that note does not cover, and it is larger.** A only calls
`_enter_position()` from the `not in_market` branch (`dsl_to_backtrader.py:199-204`), so a
vol-targeted position is sized **once, at entry, and never re-scaled** until it exits. B
recomputes the weight on **every decision bar while in market**
(`strategy_signal_evaluator.py:1337-1359`). Measured on a regime-change fixture (calm
first half, violent second half, §1.9):

```
A: 177 in-market bars, 14 distinct share counts — and all 14 changes are RE-ENTRIES:
   (bar 23, 9881) (bar 164, 1186) (bar 176, 985) (bar 187, 1004) (bar 198, 1024) ...
B weight per bar: 60→1.0  100→1.0  140→1.0  180→0.0  220→0.209  260→0.0  295→0.0
```

At bar 220 the backtest is holding a share count fixed since its last entry while the live
path targets 20.9% of the vault. These are not the same strategy.

### 1.4 D6 — B substitutes `close` for every other price field

The DSL grammar admits five price operands (`strategy_dsl.py:57`):

```python
_PRICE_OPERANDS = frozenset({"close", "open", "high", "low", "volume"})
```

A supplies all five from the feed (`dsl_to_backtrader.py:156-163`). B fabricates them
(`strategy_signal_evaluator.py:1197-1203`):

```python
bar_values = {"close": px, "open": px, "high": px, "low": px, "volume": 0.0}
```

Measured (§1.9), same spec, same series, both interpreters:

```
[entry/exit on `high`]      disagreeing bars: 39 / 279   (e.g. bar 158: A=long, B=flat)
[entry gated on `volume`]   A(bar 298)=long   B(bar 298)=FLAT   AGREE = False
```

The `volume` case is the worse one and it is not a fixture artefact: `{"gt": ["volume", N]}`
is **unconditionally false** on the live path for every positive `N`, so any spec carrying
a volume filter is graded as a strategy that trades and deployed as a strategy that never
does. Nothing in validation, in the interpreters, or in the parity suite catches it.

Root cause is structural, not a bug to patch: the live fetch is
`get_daily_close_batch` (`strategy_signal_evaluator.py:615, 666`) — **closes only**. B
*cannot* honour OHLV without a new vendor call.

### 1.5 D7 — opposite dispositions to the same missing-data condition

* A / paper: `replay_spec` raises `PaperReplayError` when the panel is unavailable
  (`paper_trading.py:80-82`), and `_sleeve_dated_returns` raises on any length mismatch
  rather than mis-dating a row (`paper_trading.py:63-66`). Fail-closed.
* B: `_fetch_price_histories` silently omits failed/empty tickers
  (`strategy_signal_evaluator.py:676-682`), and `evaluate_strategies` skips assets with no
  prices (`:1550-1553`). A vendor gap becomes *"this asset cast no vote"* — which, because
  `aggregate_signals` renormalises over whatever votes arrived (`:1647-1655`), silently
  **reweights every other asset in the vault**.
* Inside the indicators, B compresses gaps: `_rsi_wilder_series` drops NaN moves via
  `kept = np.flatnonzero(~np.isnan(diff))` (`:1028`), while backtrader propagates NaN
  through the Wilder recursion indefinitely. The comment at `:1025-1027` says this exists
  so "a gap in the series shifts both implementations identically" — that is true of B's
  own scalar/series pair, not of A.

This is the repo's own fail-soft principle inverted: the *graded* side is loud and the
*trading* side is quiet, when the trading side is the one moving money.

### 1.6 D8 — the two sides build different portfolios even when every signal agrees

* A / paper (`paper_trading.py:84-106`): `run_dsl_backtest` takes **one feed**, so each
  symbol runs as an independently-capitalised sleeve; the portfolio return is the
  equity-weighted total-equity ratio across sleeves. Fully invested per sleeve.
* B (`strategy_signal_evaluator.aggregate_signals:1620-1661`): per-asset weights are
  **averaged across strategies**, then renormalised to `1 − usdc_floor` with
  `usdc_floor = 0.20` by default. The vault is therefore capped at 80% invested, and a
  strategy long on one asset and a strategy long on five produce entirely different
  per-asset weights than the sleeve model.

Even with D1–D7 all fixed, the graded curve and the live vault would still be different
portfolios. This is the largest divergence in the inventory and it is entirely outside the
parity suite's scope.

### 1.7 D9 / D10 — window anchoring (documented, unfixed)

Both are written down at `strategy_signal_evaluator.py:1121-1140` and echoed in the parity
suite's out-of-scope note (`test_interpreter_parity.py:42-48`):

* **Phase (D9):** the cadence grid is anchored to the first post-warm-up bar of the window.
  A's window is fixed; B's is a rolling 2-year fetch, so the grid shifts one bar per
  trading day. A monthly strategy re-decides "roughly every 21 bars, but not on a fixed
  calendar date". The parity suite cannot see this because it feeds both sides the
  identical fixed series.
* **Membership (D10):** a position whose true entry bar has aged out of the rolling window
  reads FLAT live and LONG in the backtest. Guarded by a `created_at`-vs-window **warning
  only** (`strategy_signal_evaluator.py:1506-1538`). Unreachable today; a warning is not a
  fix.

### 1.8 D13 / D14 — surfaces with no counterpart at all

* **D13:** `_get_evaluator` falls back to `_buy_hold_signal` — always long, weight 1.0 —
  for any strategy without a spec (`strategy_signal_evaluator.py:1396-1406`). The module
  header claims this path "kills the silent buy-and-hold fallback" (`:906-915`); it kills
  it *only for spec-carrying strategies*. A spec-less strategy is fully invested live with
  no graded counterpart whatsoever.
* **D14:** `interpret_variant` (`dsl_to_backtrader.py:245-294`) expands a parameter grid on
  the A side; B has no twin and always interprets the base spec. If a published number ever
  comes from a winning grid variant, the live path is trading a different parameterisation
  than the one that was graded. **Verify before Step 3** which spec is persisted to
  `strategy_spec`.

### 1.9 Reproducing the measurements

Both measured results above come from short scripts run against this branch with
`/opt/homebrew/Caskroom/mambaforge/base/envs/archimedes/bin/python`. They construct a
deterministic price series, run the real `interpret_spec` class inside a real `Cerebro`
(subclassed to record `self.position.size > 0` at the top of `next()`, exactly as
`test_interpreter_parity._bt_position_path` does), and call the real `_spec_signal` on each
growing prefix. No mocks. The fixtures are:

* **OHLV:** 300 bars, `High = Close × 1.04`, `Volume = 5e6`, specs
  `{"gt": ["high", "sma_20"]}` and `{"and": [{"gt": ["close","sma_20"]}, {"gt": ["volume", 1_000_000]}]}`.
* **Vol-target:** 300 bars, ±0.2%/±0.12% zig for bars 0-149 then ±3.0%/±2.9% for bars
  150-299, spec `position_sizing = {"type": "volatility_target", "annual_pct": 0.10}`.

These belong in `test_interpreter_parity.py` as asserted asymmetries — that is Step 0.

---

## 2. Target architecture

### Option (a) — B replays through A (backtrader in the tick loop)

**Measured cost.** Same spec (`close > sma_200 AND rsi_14 < 70`), 504 bars (the live 2-year
fetch window), warm caches, on the dev box (Apple silicon, far faster than the runner):

```
A (interpret_spec + full Cerebro run):  45.2  ms per (strategy, asset)
B (_spec_signal FSM replay):             0.71 ms per (strategy, asset)
ratio A/B = 64x
```

Fan-out against the tick budget (`AGENT_INTERVAL_SECONDS = 300`, `agent_runner.py:81`):

| scale | A | B |
|---|---|---|
| 5 strategies × 20 assets | 4.5 s | 0.07 s |
| 10 × 60 | 27.1 s | 0.43 s |
| 10 × 340 (`len(GLOBAL_ASSETS) == 340`) | 122.1 s | 1.92 s |

**Runner box constraints.** The oracle **and** the agent run on one `t3.small`
(`infra/variables.tf:145-149` — "a single instance, never an ASG"), a **burstable** 2-vCPU
instance whose sustained baseline is a fraction of full CPU. The dev-box numbers above
therefore *understate* the runner cost, and the runner must also serve the oracle loop
inside the same 300 s. At the 10×340 scale the tick would not finish before the next one
starts, and CPU-credit exhaustion is a slow, silent degradation rather than a clean failure.

**It also does not fix the divergences that matter.** D1 (costs), D2 (fills), D5
(rounding), D8 (portfolio construction) live *outside* `interpret_spec` — in the Cerebro
broker and in `aggregate_signals`. Replaying through A buys A's *signal* semantics and
drags a broker simulation into a path whose job is to emit a target weight, not a fill.
Worse, it makes live signals depend on broker margin behaviour — the exact artefact the
parity fixture had to suppress with `exposure_fraction=0.5`
(`test_interpreter_parity.py:270-274`).

Finally it inverts a deliberate design decision: `strategy_signal_evaluator.py:38-43`
imports `_eval_condition` **lazily** specifically "so this module does not pull backtrader
into the API/runner import chain".

**Verdict: rejected for the tick loop.** Note the nuance — A-in-the-loop is already the
right answer for the **paper ledger** (`paper_trading.replay_spec` does exactly this) and
for any nightly re-grade. The objection is to a 300 s trading loop, not to A itself.

### Option (b) — one shared pure decision core, two adapters ✅ **recommended**

Extract `backend/archimedes/services/strategy_core/` — pure Python + numpy/pandas, **no
backtrader import** — owning:

```
strategy_core/
  indicators.py   # one definition per indicator, scalar + whole-series forms
                  # (today split across dsl_to_backtrader._make_indicator and
                  #  strategy_signal_evaluator._compute_indicator_{value,series})
  cadence.py      # rebalance_period_bars  ← already single-sourced, just moves
  fsm.py          # step(state, bar_values, cadence_eligible) -> state
                  # the ONE position FSM
  sizing.py       # target_exposure(window, position_sizing) -> float
  execution.py    # the declared execution model (fill offset, cost model id)
```

and two thin adapters:

* **A's adapter** — `DSLStrategy.next()` builds `bar_values` from the backtrader lines and
  calls `core.fsm.step()`, then translates the returned exposure into an order. backtrader
  keeps doing what it is good at: the broker, fills, costs, trade stats, equity curve.
* **B's adapter** — `_replay_position_state` becomes a loop over `core.fsm.step()` fed from
  the pandas series.

**Why this is the right shape.** The inventory splits cleanly into two classes:

| class | divergences | fixed by a shared core? |
|---|---|---|
| **decision** — what the strategy *decides* | D3, D4, D6, D9, D10, D11, D12, D13 | **yes, by construction** |
| **execution / portfolio** — what happens *after* | D1, D2, D5, D7, D8, D14 | **no** — must become explicit, named, versioned parameters on both sides |

Option (b) is the only option that *separates* those classes instead of pretending one does
not exist. Option (a) merges them by fiat and inherits a broker it does not want; option (c)
merges them by deleting the broker.

Two further arguments:

1. **The parity suite changes species.** Today it is a tether between two implementations —
   valuable, but it can only ever test the cases someone thought to write. After (b) it
   becomes a *conformance test on one implementation with two adapters*, plus a small
   residual set for the things that genuinely differ (fills, costs). Whole classes of
   future F1/F5-style drift stop being possible rather than being caught.
2. **Every intermediate state is shippable.** Each extraction can land alone with the parity
   suite green. That is what makes the migration in §3 a ratchet rather than a big-bang.

**Cost of (b), honestly:** the indicator extraction is the awkward part. A currently uses
lazily-evaluated backtrader line objects (`bt.indicators.SimpleMovingAverage`, etc. —
`dsl_to_backtrader.py:70-88`), not pandas. Two sub-options:

* **(b1, default)** A keeps bt indicator objects; the core owns the pandas implementations
  and is the *specification*; the parity suite pins them per-bar (it already does for
  `sma`/`rsi`/`momentum` — `test_interpreter_parity.py:150-166`). Cheap, preserves A's
  streaming feed compatibility, leaves one residual class of drift pinned-not-eliminated.
* **(b2)** A precomputes indicator series once in `__init__` from the pandas frame the feed
  was built from (`fusion_market_data.feed_factory(frame)` always has one) and indexes them
  by bar. Eliminates the residual class entirely, but couples A to pandas-backed feeds.

**Take (b1) now; keep (b2) as a later step.** The FSM and the sizing function — not the
indicators — are where the unpinned divergences (D4) actually live.

### Option (c) — A adopts B (delete backtrader from the graded path)

Rejected. Its one genuine merit is real and should be stated: it removes the 64× cost
asymmetry and makes the tick path and the grader literally the same code. It still loses:

1. **It relitigates an accepted ADR.** `docs/adr/backtrader-backtest-engine.md` fixes
   backtrader as the engine. Changing that needs a superseding ADR, not a plan doc.
2. **B has no broker, and the graded outputs need one.** `run_dsl_backtest` reads
   `_TradeStatsAnalyzer` and `_EquityCurveAnalyzer` for win rate, average holding period,
   and the per-bar equity curve that feeds the DSR/PBO gate (`fusion_evaluator.py:254-306`).
   Option (c) means reimplementing a broker to replace a mature one — with commissions,
   slippage, margin, and order lifecycle — on the side of the system where being wrong
   means publishing a false number.
3. **It re-grades everything at once.** Every published passport, every paper ledger row,
   and every open deployment was produced by A. Swapping the graded engine takes the entire
   risk of §5 in a single step, deliberately, with no ratchet.
4. **It moves the wrong way on rigor.** B is the side with **no cost model**. "Claims must
   be true" means the graded number should be the pessimistic one; adopting B makes the
   published number more flattering by construction.

---

## 3. Migration — the parity suite as ratchet

**Ratchet rule.** Every step must (i) leave `pytest backend/tests/test_interpreter_parity.py`
green, and (ii) **retire a divergence** — move a line out of the docstring's
"DELIBERATELY OUT" / "KNOWN LIMIT" sections into a pinned assertion, or add a new pinned
case. A step that changes code without moving a line in that docstring has not earned its
merge.

Sizing: **S** ≤ 1 day, contained, cannot change a graded number · **M** a few days, touches
both interpreters, *may* change a graded number · **L** a week+, touches the portfolio or
the ledger, *will* change graded numbers.

| Step | Size | What | Retires | Re-grades? |
|---|---|---|---|---|
| 0 | **S** | Give the ratchet teeth | — | no |
| 1 | **S** | Close the OHLV hole | D6 | no |
| 2 | **M** | Extract `core.fsm` | two FSMs | no |
| 3 | **M** | Extract `core.sizing` | D3, D4 † | **yes** |
| 4 | **M** | Declare the execution model | D2, D5 | no |
| 5 | **S** | Close the spec-less / variant gaps | D13, D14 | no |
| 6 | **L** | Reconcile portfolio construction | D8 | **yes** |
| 7 | **L** | Cost model on the live side (or disclose) | D1 | no (discloses) |
| 8 | **L** | Anchor the window at inception | D9, D10, part of D7 | **yes** |

† scope widened post-hoc by an in-flight sibling branch — see the sequencing note in §1
and the in-flight note under Step 3 below.

### Step 0 (S) — make the inventory executable

Add every currently-unpinned divergence to `test_interpreter_parity.py` as an **asserted
asymmetry**, using the pattern the file already has for `realized_vol` (`:417-431`): assert
that the two sides *disagree*, with a docstring saying which step is going to make them
agree. Port the two §1.9 fixtures verbatim.

Nothing changes behaviour. What changes is that from here on, *closing* a divergence
**breaks a test** — which is exactly the signal a ratchet needs, and the reason the
inversion is worth the small oddity of a test that asserts a bug.

*Guard demonstration:* the volume fixture must fail if `_replay_position_state` is changed
to pass real volume through; the `high` fixture must fail if `bar_values` stops substituting
close.

### Step 1 (S) — close the OHLV hole

Decide the disposition, do not paper over it. **Recommendation: restrict the grammar.**
Remove `open`, `high`, `low`, `volume` from `_PRICE_OPERANDS` (`strategy_dsl.py:57`) and
have `validate_strategy_spec` reject them, because the live data path physically cannot
supply them (`get_daily_close_batch`). One closed-enum line plus a validator test.

*Guard demonstration:* a spec containing `{"gt": ["volume", 1_000_000]}` must raise
`DSLError`; a spec containing `{"gt": ["high", "sma_20"]}` must raise. Show both failing
before the change (they validate cleanly today — that is how the §1.4 measurement was
possible) and rejecting after.

Migration note: any persisted spec already referencing an OHLV operand must be found and
its strategy marked, **not** silently re-validated. Search `strategy_spec` before merging.

### Step 2 (M) — extract `core.fsm`

`step(state, bar_values, cadence_eligible) -> state`, consumed by both
`DSLStrategy.next()` (`dsl_to_backtrader.py:189-204`) and `_replay_position_state`
(`strategy_signal_evaluator.py:1189-1217`). Pure refactor: the two implementations are
already bar-for-bar equivalent (the cadence algebra checks out — A's counter seeded to
`period − 1` and incremented per post-warm-up bar is identical to B's
`(t − max_period) % period_bars`), so this step should move zero numbers.

Retires the *category*: F2 and F3 were both "two FSMs drifted". After this there is one.

*Guard demonstration:* mutate the extracted `step` (e.g. test exit while flat) and show
`test_stateful_dead_zone_position_parity` and `test_monthly_cadence_decision_parity`
**both** fail — proving the shared core is genuinely on both paths and not merely imported.

### Step 3 (M) — extract `core.sizing` ⚠️ this is the step that re-grades

One `target_exposure(window, position_sizing) -> float`. Four decisions must be made
explicitly and recorded in this doc when they are:

1. window: 20 or 22 bars
2. statistic: RMS or sample stdev
3. cap: 2.0× (leverage) or 1.0× (no leverage)
4. **timing: entry-only (A) or per-decision-bar (B)** ← the D4 call, and the one with the
   largest effect

**In-flight scope note (see the sequencing note in §1).** These four decisions were scoped
against `volatility_target` alone. `dbrowneup/dsl-hardening-part1` (not merged to `main` as
of this writing) gives `equal_weight` and `inverse_vol` real per-slot behaviour on the A
side, and `inverse_vol` reuses this exact sizing formula and cap. If that branch lands
before this step is executed, `target_exposure` must cover every sizing type that shares
the realized-vol/cap math — `volatility_target` and `inverse_vol`, not just the one this
step was originally scoped against — and the live-side twin for `equal_weight`/`inverse_vol`
does not exist yet (`strategy_signal_evaluator.py:1361-1362` still treats both as plain
full-exposure-when-in-market), so "Retires D3, D4" in the table above is optimistic until
that twin is written too.

Recommendation: **22-bar sample stdev, cap 1.0×, per-decision-bar.** Rationale — sample
stdev is the standard estimator and matches `_vol_managed_signal`'s published lineage
(`strategy_signal_evaluator.py:740-798`); a 2.0× cap silently levers a vault and nothing in
the product promises leverage; and entry-only sizing means a vol-*targeted* strategy stops
targeting vol the moment it enters, which contradicts the paper it claims to implement.
**Önder owns this call** — it is portfolio math, not plumbing.

Promotes `test_interpreter_parity.py:37-41` ("DELIBERATELY OUT: volatility_target SIZING
parity") to a pinned-equal test.

*Guard demonstration:* the §1.3 regime-change fixture, asserting A and B produce the same
exposure at bars 60/100/140/180/220/260/295 — and failing on today's code, where they
produce `9881 shares frozen` vs `[1.0, 1.0, 1.0, 0.0, 0.209, 0.0, 0.0]`.

### Step 4 (M) — declare the execution model

Promote the `bt_path[t + 1]` convention out of the test helpers
(`test_interpreter_parity.py:32-36, 266-267`) into `core.execution`: a named, versioned
model (`fill_at_next_open` vs `decide_at_close`) that both adapters read. The parity suite
then *reads the declared offset* instead of hard-coding `+1`, and D5's
`exposure_fraction`/integer-rounding behaviour becomes a declared property of the A adapter
rather than an unexamined constant.

*Guard demonstration:* change the declared model and show the parity helpers shift their
comparison index accordingly — and that a mismatched declaration fails loudly rather than
comparing bar `t` to bar `t`.

### Step 5 (S) — close the spec-less and variant gaps

* **D13:** make `_get_evaluator`'s default an explicit FLAT with a loud reason, not
  buy-and-hold (`strategy_signal_evaluator.py:1396-1406`). Fail-soft is wrong here: an
  always-long default is a *plausible substitute* for a signal, which is precisely the
  failure mode `docs/architectural-principles.md` § fail-soft names.
* **D14:** confirm which spec is persisted after a variant grid run
  (`fusion_evaluator.run_dsl_backtest_variants:328-393`). If the winning variant's
  parameters are not what lands in `strategy_spec`, that is a claim-integrity bug that
  outranks the rest of this plan — file it separately and fix it first.

### Step 6 (L) — reconcile portfolio construction

`paper_trading.replay_spec`'s dollar-sleeve model (`:84-106`) vs
`aggregate_signals`' vote-average + `usdc_floor` (`:1620-1661`). Nothing above depends on
this, which is why it is last among the semantic steps. Needs a decision on whether the
**graded** number should model the `usdc_floor` cash drag the live vault actually carries —
if it should, every published number changes.

### Step 7 (L) — cost model on the live side, or an honest disclosure

Either give the live path a cost model (`core.execution` already holds the cost-model id,
and `DEFAULT_COST_MODEL_ID` at `fusion_evaluator.py:174` is the existing precedent), or
state on every surface that compares them that live target weights are **gross of costs**
and the graded curve is **net**. The disclosure is cheap and must ship regardless — the
model can follow.

### Step 8 (L) — anchor the window at strategy inception

Replace B's rolling 2-year fetch with a fetch anchored at the strategy's `created_at`.
Retires D9 (phase drift) and D10 (invisible entries), and lets the `created_at`-vs-window
warning (`strategy_signal_evaluator.py:1506-1538`) be **deleted** rather than left standing
as a permanent caveat. Also removes the fixed-vs-rolling asymmetry underneath part of D7.

Expensive: changes the fetch shape, the provider cache's access pattern, and the per-tick
work (a five-year-old strategy replays five years of bars every tick). Measure before
committing — at B's 0.71 ms / 504 bars, a 5× window is still well inside budget, but that
should be a measurement and not an assumption.

---

## 4. How the intraday-marks design rides the unified core

**Correction, 2026-08-30 (post-hoc).** This section originally claimed
`docs/plans/2026-08-30-intraday-paper-trading.md` "did not exist in this branch at the
time of writing." That was true of this branch's own tree — it forked from `58e337dc`,
before the sibling doc landed — but false as a claim about the project: the sibling doc
merged to `main` at `d15afbe1` (2026-08-30 15:22:41), **eleven minutes before** this
plan's own commit (`797877be`, 15:33:43). It existed the whole time; this branch was
simply stale against `main`, and the "checked at `docs/plans/`" verification checked the
wrong tree. The contract below was therefore written blind rather than derived from the
actual design. It is corrected below, and §4.1 cross-references the two documents now
that the sibling's 673 lines are available to read.

**The one thing that must not happen.** Today, decision cadence and mark cadence are the
same object: `_replay_position_state` iterates one series and both *decides* on a bar and
*reads price* from it (`strategy_signal_evaluator.py:1189-1217`), and
`paper_trading._sleeve_dated_returns` derives one return per bar from the equity curve
(`:47-66`). If intraday marks arrive as **more rows in the same series**, every intraday
tick becomes a decision bar and a `daily` spec silently becomes an intraday spec — F3
recreated at a finer grain, with `rebalance_period_bars`' "trading-day proxies"
(`dsl_to_backtrader.py:99-108`) quietly redefined to mean 5-minute bars.

**The contract:**

1. **Two series, named separately.** `core` takes a **decision series** (daily bars, the
   graded grain) and, optionally, a **mark series** (intraday quotes via
   `market_data_provider.get_intraday_quotes_batch`, `:127`). `core.fsm.step` consumes only
   the decision series. Marking is a separate function that takes *the position the
   decision series produced* and a price.
2. **`rebalance_period_bars` stays denominated in decision bars forever.** `weekly = 5`,
   `monthly = 21` are trading-day proxies (`dsl_to_backtrader.py:105-108`); an intraday
   grain must never be counted into that modulus. Assert it: a test that runs the same spec
   with an intraday mark series and shows `decision_count` unchanged.
3. **An intraday mark is not a `PaperDailyReturn`.** That table is append-only by law with
   `UniqueConstraint(deployment_id, date)` (`paper_store.py:68-84`) and its docstring calls
   the ledger "a user-facing track record". An intraday mark is a *display* quantity, not a
   settled daily return — it belongs in a separate table or an ephemeral cache, never as a
   row that later gets superseded by the day's real close.
4. **Intraday marks must not enter drift detection.** `advance_deployment` flags drift when
   a fresh replay disagrees with a written row beyond `_DRIFT_EPS = 1e-9`
   (`paper_trading.py:154-178`). Feed intraday marks into that comparison and every
   deployment drift-flags every day, destroying the signal that exists to catch real
   restatements.
5. **The mark path must state its cost treatment.** A mid-day mark of an open position is
   gross of the costs the daily replay will charge on any trade it triggers. If the UI shows
   an intraday P&L beside a graded return, they are different quantities — see Step 7.

Under option (b) all five fall out of the architecture: `core.fsm` is fed the decision
series, `core.execution` names the cost treatment, and the mark path is a new consumer of
the core rather than a new *interpreter* of the DSL. Under option (a) or (c) the intraday
path would be a third interpreter, and this document would be written again in six months.

### 4.1 Cross-reference against the landed sibling doc

Checked against the actual 673-line document rather than assumed. It was written without
reference to this one — neither cites the other — and independently arrived at compatible
content for four of the five contract points above:

| This doc's rule | Sibling doc's actual v1 design | Compatible? |
|---|---|---|
| **1.** Two series, named separately — `core.fsm.step` consumes only the decision series | v1 marks a *cached position set* from the daily `replay_spec`; the marks loop "does **not** call `run_dsl_backtest`, does **not** call `_eval_condition`, and does **not** consult `rebalance_frequency`" (sibling §4.1) | **yes** |
| **2.** `rebalance_period_bars` stays denominated in decision bars forever | Sibling §1.3 "Landmine 1" independently names the identical bar-vs-calendar-time hazard in `_REBALANCE_PERIOD_BARS` and defers touching it to a v2 ADR | **yes** |
| **3.** An intraday mark is not a `PaperDailyReturn` — a separate table, never a superseding row | New `paper_marks` table (sibling §3.1), its own docstring says "**NOT the track record**"; `paper_daily_returns` stays append-only, untouched | **yes** |
| **4.** Intraday marks must not enter drift detection | Sibling §4.3: "`replay_spec`, the ledger append-only law, and drift detection are untouched." True by construction — the marks loop has no write path into `advance_deployment` — but not stated as an explicit rule the way this doc states it | **yes, by construction** |
| **5.** The mark path must state its cost treatment | Handled as *staleness* honesty only (`is_delayed`, sibling §2.4) — nothing in the sibling's five honesty rules says a mark is gross of the trade cost a same-day rebalance would incur | **gap — not reconciled** |

Four of five needed no reconciliation because the sibling doc reached the same place on its
own evidence, independently, from the code rather than from this document. The fifth is a
real gap: `is_delayed` answers *when* a price was observed, not *whether the number shown
already prices in costs it hasn't charged yet* — the same gross/net ambiguity §5.3 below
flags between the graded curve and the live vault, now with a third quantity (the intraday
mark) in the confusable set. Concretely: the sibling doc's ledger-card copy (`paperCopy.js`,
its §5.1) should read something like "unsettled — gross of any trade this position would
trigger" beside the as-of time, not just "delayed." File this as review feedback on that
design's implementing issue when one exists (its own §7 notes none does yet); it does not
block either doc today.

---

## 5. Risks

### 5.1 The gate re-grades existing strategies differently — the headline risk

Steps 3, 6 and 8 change what a strategy's equity curve is, therefore its Sharpe, therefore
its DSR and PBO, therefore **its verdict**. Some currently-passing strategies will fail and
some currently-failing ones will pass. That is not a bug — it is the correct consequence of
fixing a divergence — but it is a claim change, and the claims rule governs it.

**Detection — a re-grade diff, mandatory before any of those three steps merges.** Replay
every persisted strategy through the old and the new core and emit a per-strategy table:

```
strategy_id · name · sharpe(old→new) · dsr(old→new) · pbo(old→new) · verdict(old→new)
```

Merge criterion is *not* "no verdicts changed" — that would forbid ever fixing anything.
It is: **every changed verdict is explained by the specific divergence the step retired.**
A verdict that moves for an unexplained reason means the step did more than it claimed.

**Attribution — stamp a core version, the way costs already are.** `DEFAULT_COST_MODEL_ID
= f"cm1:d{_DEFAULT_TX_BPS:g}:s{DEFAULT_SLIPPAGE_BPS:g}"` (`fusion_evaluator.py:174`) is the
existing pattern. Add an interpreter/core version id to every backtest row and every paper
deployment. Without it, a re-grade is **indistinguishable from a data restatement** — and
that ambiguity is not hypothetical: it is exactly what `advance_deployment`'s warning
already has to admit it cannot resolve (`paper_trading.py:166-178`):

> "Two known causes produce this identical signature: (1) upstream data restatement … or
> (2) a change to the GRADED path's own replay behavior between runs … This log cannot
> distinguish the two."

A core-version stamp turns that sentence into a solved problem.

**Disclosure — the paper ledger is the sharp edge.** The ledger is append-only by law, so a
core change moves `replay_spec`'s output for **already-written dates** and
`drift_detected_at` fires on every open deployment simultaneously. Correct detection,
terrible disclosure: the user sees "your track record drifted" with no cause. Required
mitigation:

* stamp the core version on the deployment **at deploy time**;
* when a replay crosses a core-version boundary, say so explicitly in the UI copy — *"the
  engine grading this deployment changed on ⟨date⟩; rows before that date were graded by
  ⟨v1⟩"* — instead of the generic drift flag;
* never rewrite the pre-boundary rows. The append-only law is the product.

**Per the claims rule:** do not re-publish a verdict under the new core without re-running
the gate, and do not quote a pass count before or after (CLAUDE.md's standing corollary —
the corrected count is "unestablished"). If a verdict flips, the honest surface is a visible
`re-graded` state with both numbers, not a silent overwrite.

### 5.2 A shared core makes both sides fail together

Today a bug in B degrades live signals while published numbers stay intact — the duplication
is accidental redundancy. After unification, one bad core change corrupts the graded number,
the track record, and the live trade at once. The parity suite cannot catch this: both sides
would be wrong *identically*, and every parity assertion would pass.

**Mitigation:** the parity suite must stop being the only guard. Add a **golden-numbers
regression** — a handful of specs with Sharpe / CAGR / max-DD pinned to 4 dp against a fixed
series — so a semantic change to the core cannot land silently even when it is
self-consistent. Land this in **Step 0**, before the first extraction, not after.

### 5.3 "Unified" will be read as "comparable"

The moment this ships, everyone — including us — will assume live and graded returns are now
the same quantity. Until Step 7 they are not: the graded curve is **net** of 10 bps
commission and 5 bps slippage, the live target weights are **gross of everything**, and the
live vault also carries a 20% `usdc_floor` the graded run does not model. Say this in the
PR body, in the doc, and on any surface that puts the two numbers near each other. This is
a disclosure obligation, not a caveat.

### 5.4 Steps 3 and 6 need Önder, not just a merge

Window length, estimator, cap, sizing cadence, and whether the graded number should model
the vault's cash drag are **portfolio-math decisions**. The lead/coverage table makes Önder
the reviewer of record for backtest and portfolio math. Executing the extraction without
that call is how a plumbing PR silently becomes a quant decision.

### 5.5 The parity suite's own fixtures are load-bearing

`test_interpreter_parity.py` carries anti-vacuity assertions throughout (`:144`, `:192`,
`:343-356`, `:386-402`) precisely because a parity loop that compares nothing proves
nothing. Every step above adds fixtures; each must carry the same anti-vacuity guard, and
each new "pinned equal" case must be shown to **fail on the pre-step code**. A parity test
that passes both before and after the fix is not a ratchet tooth.

---

## Appendix — file map

| File | Role |
|---|---|
| `backend/archimedes/services/strategy_dsl.py` | the language: closed enums, validator, `StrategySpec` |
| `backend/archimedes/services/dsl_to_backtrader.py` | **Interpreter A** — spec → `bt.Strategy` |
| `backend/archimedes/services/fusion_evaluator.py` | A's harness — Cerebro, broker, costs, metrics |
| `backend/archimedes/services/paper_trading.py` | A's forward run — replay, append, drift |
| `backend/archimedes/models/paper_store.py` | the append-only ledger and its law |
| `backend/archimedes/services/strategy_signal_evaluator.py` | **Interpreter B** — live FSM, indicators, aggregation |
| `backend/archimedes/chain/agent_runner.py` | B's consumer — tick loop, cadence/drift gate composition |
| `backend/archimedes/marketplace/service.py` | B's second consumer |
| `backend/tests/test_interpreter_parity.py` | the tether — and the ratchet this plan uses |
