# ADR: Harden the strategy DSL; no Lean 4 on the emission path

> **Audience:** Archimedes team
> **Status:** **Accepted**
> **Date:** 2026-08-30 (amended same day: § Implementation status records item 3 landing)
> **Owner:** Dan Browne
> **Supersedes:** —
> **Superseded-by:** —
> **Question being decided:** The generator emits machine-readable strategies. Should that emission target become a formally verified language — specifically Lean 4 — or should the existing closed-enum JSON DSL be hardened in place?
> **Related:** [`strategy_dsl.py`](../../backend/archimedes/services/strategy_dsl.py), [`dsl_to_backtrader.py`](../../backend/archimedes/services/dsl_to_backtrader.py), [`strategy-dsl-spec.md`](../specs/strategy-dsl-spec.md), [`debate-society-sole-generation-pipeline.md`](debate-society-sole-generation-pipeline.md), [`backtrader-backtest-engine.md`](backtrader-backtest-engine.md), [`rigor-gate-unification.md`](rigor-gate-unification.md), [`future-strategy-language-reeval-issue.md`](../plans/future-strategy-language-reeval-issue.md).

## TL;DR

**No Lean 4 on the emission path. Harden the DSL we already have.** The safety property
people reach for Lean to get — *generated content cannot execute arbitrary code* — is
already structural here: the generator emits constrained JSON, and a fixed hand-written
interpreter turns it into a `backtrader.Strategy`. There is nothing to prove because there
is nothing to run. What the DSL is actually weak on is **honesty about its own limits**, and
that is fixed with four concrete pieces of work, not with a proof assistant. A sandboxed
execution path stays reserved for strategy shapes the DSL genuinely cannot express. Lean 4's
only plausible future role here is proving a small hand-written verifier core — never the
emitter's output.

## Context

### What the DSL is today

Real, live, and narrow. [`strategy_dsl.py`](../../backend/archimedes/services/strategy_dsl.py)
defines a closed-enum JSON grammar: five indicator stems (`sma`, `ema`, `rsi`,
`realized_vol`, `momentum`), four comparison operators, three logic operators, three
rebalance cadences, four declared position-sizing types. Conditions are a small tree
(`{"gt": ["close", "sma_200"]}`), validated node by node, and every unknown operator or
operand is a `DSLError`.
[`dsl_to_backtrader.interpret_spec()`](../../backend/archimedes/services/dsl_to_backtrader.py)
turns a validated spec into a `bt.Strategy` subclass via `type()` with closures — its
docstring says it plainly: *"No eval/exec/importlib."*

That is the whole security story, and it is a good one. **The generated artifact is data,
not code.** The interpreter is hand-written, fixed, reviewed, and in the tree. A malicious
or confused emission cannot do more than describe a strategy the interpreter already knows
how to build. No sandbox, no proof, no capability audit is required to establish that,
because the property is structural rather than argued.

The narrowness is equally real. The condition tree reads a **single price series** — every
operand resolves against `self.data`, so no condition can reference another asset. A
multi-symbol `asset_universe` is not a cross-sectional strategy; it is run as independent
per-symbol backtests and equal-weighted
([`fusion_evaluator.py`](../../backend/archimedes/services/fusion_evaluator.py) →
`run_dsl_backtest_portfolio`). Positions are long-flat. There is no ranking, no pair, no
short leg, no cross-asset signal.

### What the evaluation found about Lean 4

Four things, and any one of them would have been enough.

**Frontier models cannot reliably write Lean 4.** Public benchmark reporting at the time of
the decision put frontier-model success at producing correct Lean 4 proofs of
competition-grade statements in roughly the **4–11%** band. *[The specific benchmark
citation is not pinned in this record — unestablished, needs Dan.]* The number matters less
than its order of magnitude: a path where the best available models succeed one time in
ten to one time in twenty-five is not an emission path, it is a research project.

**And our emitter is not a frontier model.** The live generator runs
`amazon.nova-micro-v1:0` ([`glm-to-bedrock-llm-migration.md`](glm-to-bedrock-llm-migration.md)).
Whatever the frontier number is, ours is below it. Nova Micro emits the current JSON grammar
reliably because that grammar is small and closed; asking it for dependently-typed proof
terms is not the same task with a harder target, it is a different task it cannot do at all.

**The numeric ecosystem is pre-production.** Lean 4's strength is pure mathematics. What a
strategy needs proved is about floating-point series, rolling windows, and sample
boundaries. Bridging real-number reasoning to the `float64` a backtest actually computes on
is an open problem in that ecosystem, not a library call.

**There is no clean Python bridge.** The entire evaluation stack — backtrader, the rigor
gate, the passport — is Python
([`backtrader-backtest-engine.md`](backtrader-backtest-engine.md)). A Lean 4 emission would
need extraction or an FFI to become something backtrader can run, and that translation step
is unverified by construction. A proof about the Lean artifact would say nothing about the
Python that actually produced the published Sharpe. The proof and the numbers would be about
different objects.

### So what is the actual problem?

Not safety. **Honesty.** The audit that prompted this decision found the DSL claiming more
than it delivers, in four specific places — each one a "claims must be true" violation of
exactly the shape [`rigor-gate-unification.md`](rigor-gate-unification.md) exists to prevent:

- **`look_ahead_safe` is self-declared.** The validator checks only that the field is a
  boolean and that its value is `true` — a spec asserting its own innocence is admitted on
  that assertion. The pipeline is honest about this downstream
  (`look_ahead_audit_source="self_attested"` in
  [`generation_pipeline.py`](../../backend/archimedes/agents/generation_pipeline.py), and the
  distinct `look_ahead_label` in
  [`strategies_routes.py`](../../backend/archimedes/api/strategies_routes.py)), which is
  correct labelling of a weak signal but does not make the signal strong.
  **(Closed — see § Implementation status. This paragraph is the diagnosis as
  found, kept as the record of what was wrong; the state it describes no longer
  ships.)**
- **Two of the four position-sizing types are not implemented.**
  `POSITION_SIZING_TYPES` advertises `full_invested_when_in_market`, `equal_weight`,
  `inverse_vol`, `volatility_target`. `_enter_position` implements the first and the last;
  `equal_weight` falls through to the `else` branch and full-invests, and `inverse_vol` has
  no branch at all and does the same. A passport can therefore record `inverse_vol` over a
  run that was fully invested.
- **`realized_vol` validates but blows up at backtest time.** It is in `INDICATOR_NAMES`, so
  `realized_vol_20` parses, and `interpret_spec` returns a strategy class without complaint.
  The failure lands later: indicators are wired in the generated class's `__init__`, so
  `_make_indicator` raises `DSLError("unsupported indicator: realized_vol")` when backtrader
  instantiates the strategy inside `cerebro.run()`. A validation-time rejection has become a
  run-time crash. This is currently worked around rather than fixed — the debate engine
  carries a `_CONFORMANT_INDICATORS` filter
  ([`debate_engine.py`](../../backend/archimedes/agents/debate_engine.py)) that drops such
  specs from the pool before they reach the evaluator. (That filter's comment locates the
  raise "inside `interpret_spec`"; the effect is as described but the location is one call
  later — worth correcting when the filter is retired.)
- **The spec doc describes a schema that does not exist.**
  [`strategy-dsl-spec.md`](../specs/strategy-dsl-spec.md)'s "Schema" and "Closed-enum
  vocabulary" sections document an older grammar —
  `{indicator, operator, threshold, secondary_indicator}`, indicators `macd_line` /
  `bb_upper` / `atr`, `position_sizing` as a bare string `full_capital` / `kelly` /
  `atr_sized`, a `params.period` block. None of it is accepted by the live validator. Only
  the later "Parameter variants" section reflects what actually ships.

None of those four is a problem a proof assistant solves. Three are missing
implementations and one is a stale document.

## Alternatives considered

**A note on the name, because it caused real confusion in the evaluation.** Two unrelated
things are called "LEAN":

- **Lean 4** — a dependently-typed programming language and interactive theorem prover
  (Mathlib, `sorry`, proof terms). This is what "formally verified strategies" meant.
- **QuantConnect LEAN** — an open-source algorithmic-trading backtesting and live-trading
  engine, C# core with a Python API. It proves nothing about anything; it is a competitor to
  backtrader.

They share four letters and nothing else. Both were on the table and they are scored
separately below.

**Lean 4 itself is not a row in the matrix**, because it did not survive screening. The
matrix scores *emission targets* — things the generator can produce that end in a graded
backtest. Lean 4 has no execution path to a backtest at all without an extraction step that
would itself be unverified, so there was nothing to score. That is the finding, not an
omission.

Each option is scored 1–5 on eight criteria; 40 is the maximum.

| Criterion | Harden DSL | Restricted Python sandbox | Checked AST subset | QuantConnect LEAN |
|---|---|---|---|---|
| Expressiveness for strategies we actually want | 2 | 5 | 4 | 5 |
| Safety of the execution boundary | **5** | 3 | 4 | 3 |
| Fit with the live emitter (Nova Micro) | **5** | 4 | 3 | 3 |
| Time to a state we can describe honestly | **5** | 3 | 2 | 1 |
| Verifiability of the no-lookahead property | 4 | 3 | 4 | 3 |
| Numeric / financial ecosystem maturity | 4 | 5 | 4 | 5 |
| Integration cost against backtrader + the rigor gate | **5** | 5 | 4 | 2 |
| Ongoing maintenance burden | 3 | 4 | 2 | 4 |
| **Total** | **33** | **32** | **27** | **26** |

Also screened and scored below the cut in the same evaluation: Composer-style
declarative JSON **26** (a UX pattern with no open spec or parser to adopt),
Pine Script **17** and MQL5 **13** (retail/vendor-hosted DSLs with no path
into the backtrader-based rigor gate). Recorded so the ranking is complete
and no rejected option invites relitigation as an oversight.

**1. Harden the existing DSL — 33. Chosen.** Loses badly on expressiveness and that is the
honest cost. Wins everywhere else, and wins the one criterion that decided it: it is the
only option that reaches a state we can describe truthfully in days rather than months,
without giving up the structural safety property we already have for free.

**2. Restricted Python sandbox — 32.** The closest call, and it is close for good reasons:
maximum expressiveness, native fit with backtrader, an emitter that writes Python far better
than it writes anything else. It loses on the execution boundary. Today "generated content
cannot execute code" is a *structural* fact; under a sandbox it becomes an *argued* one,
resting on a containment configuration that has to be right and stay right. Trading a
structural guarantee for a configured one, to buy expressiveness we do not yet have a
strategy demanding, is the wrong trade **now**. It is not the wrong trade forever — see the
decision below.

**3. Checked AST subset — 27.** Generate Python, parse it, walk the AST against a whitelist,
execute what passes. Sounds like the best of both and is the worst of both: still executing
generated code, so the boundary is argued rather than structural, but with a whitelist that
must be exhaustively correct against the entire Python grammar forever. Every language
feature is a potential hole and the maintenance burden never ends. It is a worse DSL and a
worse sandbox at the same time.

**4. QuantConnect LEAN engine — 26.** A mature, genuinely capable engine. Adopting it means
replacing backtrader — the engine the rigor gate, the passport schema, and the
`backtest_results.backtest_engine` provenance column are all built around
([`backtrader-backtest-engine.md`](backtrader-backtest-engine.md)) — with a C#-cored,
Docker-deployed system carrying its own data model and its own cost assumptions. That is a
platform migration, and it answers no question we are currently stuck on. Recorded as
considered and declined; not re-opened here.

## Decision

**Harden the DSL. Four pieces of work, all of which close a gap between what the DSL claims
and what it does.**

1. **Time bounds become part of the spec.** A spec must declare the evaluation window it is
   meant for, and indicator periods must be bounded against the sample rather than against a
   constant. Today `_parse_indicator_operand` accepts any period in `1..10_000` — around
   forty years of daily bars, longer than the 5,560-bar SPY fixture the DSL is verified
   against — so a spec can validate with a warmup that swallows its entire sample and still
   be graded. Relatedly, the cadence table is trading-day proxies (`weekly` = 5 bars,
   `monthly` = 21 bars), not calendar dates; that is a defensible choice but it must be
   stated rather than discovered.
2. **Real sizing implementations, or a smaller enum.** `equal_weight` and `inverse_vol` get
   implemented, or they come out of `POSITION_SIZING_TYPES`. Both are acceptable; silently
   full-investing under another label is not. The same rule settles `realized_vol`:
   implement it in `_make_indicator` or remove it from `INDICATOR_NAMES`, and retire the
   `_CONFORMANT_INDICATORS` filter that exists only to route around the mismatch.
3. **A derived no-lookahead check replaces the self-declared boolean.** This is the item that
   makes the whole decision coherent. Because the grammar is closed and the interpreter is
   fixed, the property is *decidable from the spec* — every operand either reads the current
   bar or an indicator over a bounded trailing window, and the interpreter is the only thing
   that ever evaluates them. So compute it: walk the condition tree, confirm every operand
   resolves to a value available at decision time, and emit a **derived** verdict.
   `look_ahead_safe` stops being an input the emitter asserts and becomes an output the
   validator computes. That is the formal-verification benefit people wanted from Lean,
   obtained by keeping the language small enough that a hand-written checker is sufficient.
4. **An honest spec doc.** [`strategy-dsl-spec.md`](../specs/strategy-dsl-spec.md) gets
   rewritten to the grammar that actually ships, including the limits: single price series,
   long-flat only, no cross-asset conditions, multi-symbol universes evaluated as
   equal-weighted independent runs.

**The sandbox path is reserved, not rejected.** It scored one point behind. When a strategy
shape we genuinely want cannot be expressed — a long/short pair, a cross-sectional rank, a
signal reading two series at once — the answer is the restricted sandbox, entered
deliberately for that reason and with the boundary argued explicitly. It is not the answer
to "the DSL is a bit awkward here."

**Lean 4's only future role is proving a hand-written verifier core.** If the no-lookahead
checker in item 3 becomes load-bearing enough that we want a proof of *it* — a few hundred
lines of hand-written Python over a closed grammar, a genuinely tractable target — that is a
reasonable use of a theorem prover. Proving properties of a small artifact a human wrote is
the thing Lean is actually good at. Having an LLM emit Lean is not, and that door is closed.

## Implementation status

Recorded here so the decision above and the shipped code cannot drift apart. This
section reports what landed; it does not reopen the decision.

**Item 3 (derived no-lookahead check) — LANDED, 2026-08-30.**
[`services/dsl_lookahead_audit.py`](../../backend/archimedes/services/dsl_lookahead_audit.py).
It went further than the item described. The item proposed walking the condition
tree to confirm every operand resolves to a value available at decision time; the
shipped audit does that *and* proves the thing that walk quietly assumes — that the
interpreter evaluating those operands never reads forward:

1. **The interpreter is audited, once, by AST.** `verify_interpreter_surface`
   parses [`dsl_to_backtrader.py`](../../backend/archimedes/services/dsl_to_backtrader.py)
   and classifies every read of a backtrader line, proving each carries a bar
   offset ≤ 0. An offset it cannot *prove* non-positive is treated exactly like
   one it disproved — the only accepted proofs are a non-negative integer
   literal, a `range()`-bound loop variable, and a parameter the enclosing
   function guards (`if period < 1: raise`, which is why that guard in
   `_make_indicator` is load-bearing rather than defensive). A read site with
   zero matches is a *broken verifier*, not a clean one, and fails.
2. **The spec is proven to stay inside that audited surface.**
   `audit_dsl_strategy` checks every indicator, operator, operand, sizing type,
   cadence and parameter-variant period against a surface pinned in the audit
   module — deliberately *not* imported from `strategy_dsl`, so widening a DSL
   enum without re-auditing the interpreter makes specs using the new construct
   FAIL rather than inherit a pass they never earned.
3. **Broker execution timing is charged on the real run.**
   cheat-on-close / cheat-on-open must be off, read on both sides of
   `cerebro.run()` because a strategy can set it mid-run.

The verdict is four-state — `pass` / `fail` / `pending` / `degenerate`, the same
words the rest of the rigor stack uses for a verdict that may not exist — and
**only `pass` clears the gate's LEAK criterion**. `pending` means nothing was
audited (no validated spec, or no broker execution-timing check for the run);
`degenerate` means the audit ran and its own instrument gave no reading (the
interpreter source was unreadable, or the AST pass matched zero read sites, which
proves nothing rather than proving cleanliness).

`look_ahead_safe` is **removed from the DSL entirely** — from `REQUIRED_FIELDS`,
from `StrategySpec`, from `to_dict`, and from the generation prompt. It is not
demoted to a recorded field: an emitter's assertion about its own output is not
provenance worth carrying, and keeping it invited exactly the "read it back as if
it were a check" mistake the diagnosis describes. Since the attribute does not
exist, consulting a declaration is an `AttributeError`, not a silent false claim.
That is the "input the emitter asserts becomes an output the validator computes"
the item asked for, with the input deleted rather than demoted.
`look_ahead_audit_source` is derived — `dsl_structural_audit` when the audit
concluded either way, `dsl_audit_not_run` when it did not — and the hardcoded
`self_attested` the diagnosis cites is retired: never written again, and no
longer honoured on read (a stored `True` beside it is a claim, not a
measurement, so `verdict_from_persisted_row` reads such a row as `pending`).

Two properties are separate on purpose and must stay that way: **deployability is
fail-closed** (`pending` and `degenerate` block exactly as hard as `fail` — an
audit that reached no verdict is not evidence), while **rendering is honest**
(neither is ever shown as a failure, because nothing found a leak). There is one
vocabulary for both surfaces and the gate: the four-state `look_ahead_status`,
plus `blocks_deploy` for the axis a status word cannot carry.

**Not proven, stated rather than hidden.** backtrader's own `SimpleMovingAverage`
/ `ExponentialMovingAverage` / `RSI` are library code: the audit proves the
interpreter *feeds* them only bar-*t*-and-earlier lines and *reads* their output
at offset 0; it does not re-derive backtrader's internal causality. Data-feed
time alignment is a provenance question handled by the evaluator's `data_source`
leg, not here.

Items 1, 2 and 4 are unchanged by this and remain as written above.

## Consequences

**Good.** The structural safety property survives untouched: generated content stays data,
the interpreter stays hand-written, `eval`/`exec` stay absent. Every claim the DSL makes
becomes one the code keeps, which is the repo's first rule applied to the generation path.
The no-lookahead signal is upgraded from an emitter's self-assertion to a derived fact, which
strengthens the rigor gate's most-questioned input without touching the gate. And the work is
small and local — no engine migration, no new runtime, no dependency.

**The cost, stated plainly.** The DSL stays narrow, deliberately. Long/short, cross-sectional,
and multi-leg strategies remain inexpressible, and that ceiling is now a recorded decision
rather than an accident. Some fraction of the strategies a paper corpus can motivate cannot be
emitted at all — the pipeline will keep declining them, and it should keep declining them
loudly rather than emitting a degraded approximation under the paper's name.

**Between now and the hardening landing, three things are still not true.** `inverse_vol` and
`equal_weight` passports describe sizing that did not happen; `realized_vol` specs are
filtered out by a workaround rather than supported or rejected at the source; and the spec doc
misdescribes the schema. Those are named here so nobody has to rediscover them, and none of
them should be cited as working until the corresponding item ships. (Item 3 has
since shipped — see § Implementation status. These three are items 1, 2 and 4 and
are still open. `realized_vol` is now at least *rejected loudly*: it sits in the
DSL enum with no interpreter branch, so the structural audit fails any spec naming
it rather than letting it through — a rejection, not the implementation item 2
asks for.)

**What this does not decide.** Whether the sandbox eventually gets built, and what its
containment boundary looks like. That is a separate decision with a separate ADR, triggered by
a real strategy shape rather than by discomfort with the grammar.

## Re-evaluation

Deliberately not "never." The trigger conditions, the evidence to gather, and the shape of
the re-evaluation are drafted as a Future Plans issue:
[`future-strategy-language-reeval-issue.md`](../plans/future-strategy-language-reeval-issue.md).
In short: revisit when property tests stop catching real violations, when a cross-asset
grammar starts pushing the DSL toward general-purpose, or when LLM-Lean writability moves by
an order of magnitude. Until one of those fires, this record is in force and the language
question is closed.
