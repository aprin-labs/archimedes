# Paper-Trading Reasoning Traces

> **status:** draft
> **owner:** Dan Browne
> **updated:** 2026-08-30
> **superseded-by:** —
>
> Addresses [#1575](https://github.com/a-apin/archimedes/issues/1575).
> **Prerequisite reading:** [`../specs/commit-reveal-trace-spec.md`](../specs/commit-reveal-trace-spec.md) ·
> [`2026-08-30-intraday-paper-trading.md`](2026-08-30-intraday-paper-trading.md) §1 ·
> `services/trace_visibility.py` (#1556) · `services/paper_trading.py`.

## The gap, stated plainly

The product's central claim is *auditable reasoning behind every move*. Today the house
agent honours it: `chain/agent_runner.py` builds a `ReasoningTrace`, hashes it,
commit-anchors it before the trade, reveals after, and persists it through
`AgentStateStore.save_trace`. Paper trading — **the surface users actually touch** —
honours none of it. `services/paper_trading.py` re-runs the graded engine and appends rows
to `paper_daily_returns`. That is the entire artifact. A user's paper deployment moves
(paper) money and leaves no hashed, owner-stamped, verifiable record of *why*.

The fix is not a second trace system. It is **one new producer feeding the existing
choke point**, so that everything already built around that choke point — the #1556
ownership stamp, the #1569 passport panel, `/verify`, `/canonical`, the tamper detection —
applies to paper traces for free.

---

## Contents

1. [Where a paper decision is born](#1-where-a-paper-decision-is-born)
2. [The trace body, next to the house agent's](#2-the-trace-body-next-to-the-house-agents)
3. [Ordering: the paper analogue of commit-before-trade](#3-ordering-the-paper-analogue-of-commit-before-trade)
4. [Owner stamping (#1556) — and the sentinel landmine](#4-owner-stamping-1556--and-the-sentinel-landmine)
5. [Reachability (#1569): `decision_type` + `strategies_referenced`](#5-reachability-1569-decision_type--strategies_referenced)
6. [On-chain anchoring is default OFF](#6-on-chain-anchoring-is-default-off)
7. [Loud failure for a decision that does not publish](#7-loud-failure-for-a-decision-that-does-not-publish)
8. [Adversarial guards](#8-adversarial-guards)
9. [Build plan](#9-build-plan)
10. [Anti-goals, sequencing, out-of-scope](#10-anti-goals-sequencing-out-of-scope)

---

## 1. Where a paper decision is born

### 1.1 The settle path today

```
paper_advance_loop()  →  advance_all(session)  →  advance_deployment(session, dep)
                                                    │
                                                    ├─ replay_spec(spec_json, deployed_at)
                                                    │     └─ per sleeve: run_dsl_backtest(...)
                                                    │          → BacktestMetrics.equity_curve
                                                    ├─ diff against existing ledger rows
                                                    ├─ append NEW dates only (append-only law)
                                                    └─ count drift, never repair
```

The replay is a **full re-run from the deploy date on every settle** — deliberately, because
the backtest engine is a position FSM with no serialisable state (`paper_trading.py` module
docstring). Everything below has to be idempotent against that.

### 1.2 The boundary itself

Inside `dsl_to_backtrader.interpret_spec`, `next()` is where a paper strategy decides:

```python
def next(self) -> None:
    if len(self) <= self._warmup:          # still warming up — not a decision
        return
    if not self._should_rebalance():        # not a rebalance bar — not a decision
        return
    bar_values = self._bar_values()
    in_market = self.position.size > 0
    if not in_market:
        if _eval_condition(entry_cond, bar_values):
            self._enter_position()          # ← DECISION: enter
    else:
        if _eval_condition(exit_cond, bar_values):
            self.close()                    # ← DECISION: exit
```

**A paper decision is born at a rebalance-eligible bar on which the entry or exit condition
fired.** That is the settle-path rebalance boundary, and it is the only place in the paper
system where the position set changes.

### 1.3 What is explicitly *not* a decision

- **Intraday marks (#1568) never decide.** The marks path re-prices the basket the daily
  replay last established; it calls no `run_dsl_backtest`, no `_eval_condition`, no
  `rebalance_frequency`. Emitting a trace from a mark would assert a decision that did not
  happen — the exact fabrication class this repo exists to oppose. Marks produce **zero**
  traces, and the guard for that is a grep in §8 (G6).
- **A ledger append is not a decision.** `paper_daily_returns` gets a row every trading
  day; the strategy decides far less often (`weekly` = 5 bars, `monthly` = 21). Tracing per
  ledger row would manufacture ~21 fake "decisions" per real one.
- **Warmup bars are not decisions.** `len(self) <= self._warmup` returns before any
  evaluation happens.

### 1.4 Surfacing it without touching graded semantics

The replay must not change. `paper_trading.py`'s whole correctness argument is that it calls
*the same* `run_dsl_backtest` the grader calls; a semantic change there moves every open
deployment's history (that is literally what the drift log warns about).

So the decision journal is an **observer**, not a hook: a new backtrader `Analyzer`.
Analyzers receive `notify_order` and cannot influence the run.

```python
class _DecisionJournal(bt.Analyzer):
    """Dated record of every order the interpreted strategy actually placed.

    Observer-only. `order.created.dt` is the bar the strategy DECIDED on;
    `order.executed.dt` is the bar the fill landed (backtrader fills at the
    next bar's open unless cheat-on-close). Both are recorded because
    conflating them would misdate the decision.
    """
    def start(self):
        self._events: list[dict] = []

    def notify_order(self, order):
        if order.status != order.Completed:
            return
        self._events.append({
            "decided_on": bt.num2date(order.created.dt).date(),
            "filled_on": bt.num2date(order.executed.dt).date(),
            "side": "buy" if order.isbuy() else "sell",
            "size": float(order.executed.size),
            "price": float(order.executed.price),
            "value": float(order.executed.value),
            "commission": float(order.executed.comm),
        })

    def get_analysis(self):
        return {"events": list(self._events)}
```

Wired behind an opt-in flag so the graded path is byte-identical when off:

```python
run_dsl_backtest(spec, ..., decision_journal: bool = False) -> BacktestMetrics
# BacktestMetrics gains: decision_journal: list[dict] | None = None   (default None)
```

`BacktestMetrics` is a frozen dataclass; a trailing optional field with a `None` default is
additive. **The no-op claim is a test, not a comment** (§8 G7): the same spec run with the
flag off and on must produce identical `equity_curve`, `sharpe_ratio`, and `total_trades`.

**What this deliberately does not capture: SKIP.** A rebalance-eligible bar where the
condition did *not* fire is a real decision (`DecisionType.SKIP` exists for it) and produces
no order, so an order observer cannot see it. v1 traces **acted** decisions only, and says
so at the surface — the deployment payload reports `decisions_traced` alongside
`decision_kinds: ["rebalance"]` and the UI copy reads "trades traced; no-trade bars are not
yet traced". Inferring skips by re-deriving `_should_rebalance()` outside the engine would
be a second cadence implementation that can silently disagree with the first — precisely the
audit-F3 shape. Skip tracing is a follow-up that adds an in-engine journal hook, with the
no-op proof that requires.

### 1.5 One trace per (deployment, decision date)

The universe runs as independent dollar sleeves, so a single decision date can carry several
symbol legs. The user-visible unit of "a move" is the **date**, not the leg. Therefore:

- **Decision key = `(deployment_id, decided_on)`**, unique.
- Legs land in `trades_executed` (one entry per symbol) and in `portfolio_before/after`.
- Acceptance ("advancing one settle produces exactly one trace") is then a property of the
  key, not of the fixture.

### 1.6 Idempotency, and the first-settle backfill

The replay re-derives *every* historical decision on *every* settle. Without a key, a
deployment would republish its whole decision history daily. The `paper_decision_traces`
table (§9.3) makes the key durable: a decision date that already has a row publishes
nothing.

Two consequences to state honestly:

- **The first settle after this ships is a backfill.** An existing deployment gets traces
  for all its past decisions at once. Bounded by `PAPER_TRACE_BACKFILL_MAX` (default 500)
  per deployment per settle, logged at INFO with the count.
- **A backfilled trace is self-labelling.** `timestamp` is the *decision bar's* date (the
  honest decision time, and a hashed field), while `market_context.trace_provenance` is
  `"settle"` or `"backfill"` — also hashed. A backfilled trace can therefore never be
  laundered into a real-time one: stripping the label breaks the hash. This matters because
  the commit-reveal spec's entire threat model is post-hoc trace construction; a paper trace
  written after the fact must admit it.
- **Re-replay disagreement is drift, not repair.** If a re-run produces a different decision
  for a date that already has a published trace, the trace is **not** rewritten (the hash is
  the point). It is counted, logged with the same two-causes language `advance_deployment`
  already uses, and stamped `trace_drift_at` on the deployment.

---

## 2. The trace body, next to the house agent's

Same `ReasoningTrace` dataclass, same `_HASH_FIELDS`, same `canonical_json()` → keccak.
**No schema fork.** What differs is what honestly fills each field.

| Field | House agent (`agent_runner`) | Paper (this design) | Hashed? |
|---|---|---|---|
| `id` | `uuid4()` | `uuid4()` | ✅ |
| `vault_address` | the real vault | `""` — there is no vault (§4) | ✅ |
| `decision_type` | `REBALANCE` / `SKIP` / … | `REBALANCE` (§5) | ✅ |
| `trigger` | `"scheduled_tick"`, `"empty_vault"`, … | `"paper_settle"` | ✅ |
| `timestamp` | wall clock at decision | **the decision bar's date, 00:00 UTC** | ✅ |
| `market_context` | regime, ensemble consensus, signal summary | `venue: "paper"`, `deployment_id`, `strategy_id`, `decided_on`, `filled_on`, `rebalance_frequency`, `asset_universe`, `source_arxiv_ids`, `spec_sha256`, `trace_provenance` | ✅ |
| `portfolio_before` | AUM + holdings + weights | **the whole deployment**: every sleeve's `{size, price, value}` and summed `cash`, immediately before the fill (§2.3) | ✅ |
| `portfolio_after` | intended post-trade allocation | the whole deployment, immediately after (§2.3) | ✅ |
| `reasoning` | LLM-generated | **spec-derived and deterministic** (§2.1) | ✅ |
| `confidence` | `_compute_confidence(all_signals)` | `0.0` — no calibrated source (§2.2) | ✅ |
| `trades_executed` | `{symbol, direction, amount}` per trade | `{symbol, side, size, price, value, commission}` per leg | ✅ |
| `strategies_referenced` | `[ss.strategy_id for ss in all_signals]` | `[dep.strategy_id]` — exactly one, exact match (§5) | ✅ |
| `consulted_paper_hashes` | `arxiv_id:content_hash` from signals | only when a content hash resolves; else empty (§2.4) | ✅ |
| `trace_hash` | keccak of canonical JSON | identical computation | — |
| `arc_tx_hash` / commit / reveal / trade tx | populated by commit-reveal | **`None`** — nothing is anchored by default (§6) | ❌ |
| `is_verified` | true only on a real reveal | **`False`** | ❌ |
| `owner_user_id` / `owner_wallet` | resolved from the vault by `save_trace` | **set explicitly from the deployment** (§4) | ❌ |

The precedent for an honestly-unanchored record already exists: `agent_runner`'s no-trade
path (`_record_no_trade_trace`) hashes and persists with `arc_tx_hash=None`,
`is_verified=False`, and a log line saying exactly why nothing was anchored. Paper traces are
that same shape, for a different reason.

**Paper-ness is inside the hash.** `trigger` and `market_context` are both hashed fields, so
`venue: "paper"` cannot be stripped from a stored trace without breaking `/verify`. A paper
trace cannot be laundered into a live one.

### 2.1 `reasoning` is derived, never generated

There is no LLM in the paper settle path and there must not be one: an LLM sentence written
at settle time is a *post-hoc rationalisation of a decision a deterministic engine already
made*, which is the precise attack the commit-reveal spec is about. The paper trace's
reasoning is rendered from the snapshotted spec and the bar values that actually fired:

```
Rebalance (paper) for strategy 4f2b91c0aa3e1d55 on 2026-07-14.
Spec: "SMA-200 Tactical Allocation" — rebalance_frequency=monthly, universe=[SPY].
Entry condition {"gt": ["close", "sma_200"]} evaluated TRUE at close=548.21, sma_200=531.07.
Action: enter SPY, 182 shares @ 548.21 (full_invested_when_in_market, exposure 1.0).
Decided on the 2026-07-14 bar; filled on the 2026-07-15 open.
Graded spec snapshot sha256=…; no LLM produced this text.
```

That last clause is not decoration. It is the difference between a claim we can defend and
one we cannot.

### 2.2 `confidence` stays 0.0

Same rule `construction_trace.py` already follows: there is no calibrated source for a
confidence number on this path, and inventing one contradicts the selection-bias thesis the
rigor gate exists to enforce. The absence is stated in `expected_outcome`, not filled with a
plausible float.

### 2.3 `portfolio_before` / `portfolio_after` are DEPLOYMENT-scoped, not leg-scoped

A deployment runs its universe as **N independent dollar sleeves** (`paper_trading._replay`,
faithful to the graded path), each opening with `fusion_evaluator._DEFAULT_CASH`. So a
2-sleeve deployment holds **2 × the sleeve capital**, and on any given decision date only
*some* of its sleeves trade.

Deriving the two snapshots from the decision's legs — the obvious reading, since the legs
carry `cash_before`/`cash_after` and `position_before`/`position_after` — gets both halves
wrong:

- **Cash is understated by the sleeves that did not trade.** A 2-sleeve deployment tracing
  one sleeve's decision reported `cash: 100_000`-scale numbers for a deployment holding
  `200_000`, and would report the same figure at 2 sleeves or at 20.
- **Untraded symbols are absent from `holdings` entirely**, so a reader cannot distinguish
  "held nothing" from "was not looked at".

Both fields are **hashed**, and on the opt-in anchoring path (§6) `reveal()` writes them
on-chain *permanently*. A wrong portfolio is worse than no portfolio: it is a false
statement with a keccak over it.

So `paper_trace.deployment_portfolio()` builds the snapshot over **every sleeve**, and it is
built in `_replay` — the only place that has all the sleeves — rather than inside
`build_paper_trace`, which only ever sees one date's legs. `build_paper_trace` therefore
**requires** both snapshots instead of deriving them; a caller that supplies only legs is
rejected at the seam rather than publishing a half-formed portfolio.

| sleeve on the decision date | `size` / `cash` | mark |
|---|---|---|
| traded | the bracketing legs' own `position_*` / `cash_*` — first fill of the date for `before`, last for `after`, so several fills on one bar still bracket the whole date | that leg's fill price |
| traded earlier, quiet today | carried forward from its most recent earlier fill | that sleeve's close on the decision bar (as-of the last bar ≤ it) |
| never traded | flat, holding its full opening sleeve capital | the decision bar's close, else `None` |

Two consequences worth stating:

- The mark for an untraded sleeve is `None` when no close and no prior fill exist — **an
  absence, never a `0.0`**, which would read as "this position is worthless" inside a hashed
  field. Same rule as `confidence` (§2.2) and `consulted_paper_hashes` (§2.4).
- `deployment_portfolio` is handed each sleeve's **complete** leg history, pre-deploy dates
  included. A sleeve's state on a decision date is the sum of every fill before it, and the
  replay starts at the feed's first bar — the same window the graded numbers are computed
  over — not at `deployed_at`.

### 2.4 `consulted_paper_hashes` is empty unless a hash resolves

The field's contract is `"arxiv_id:content_hash"`. The snapshotted spec carries
`source_arxiv_ids` but no content hash, and the prod corpus has `corpus_meta = 0`, so for
most deployments no content hash is resolvable. Emitting `"2301.00001:"` would be a
half-formed value that reads as provenance. Design: populate the list **only** for ids whose
content hash resolves from the corpus store; record the bare ids in
`market_context.source_arxiv_ids` regardless. Absence is visible; fabrication is not.

---

## 3. Ordering: the paper analogue of commit-before-trade

The commit-reveal spec's ordering (hash → commit → trade → reveal) exists so the agent cannot
tailor reasoning to an outcome. Paper has no broadcast, but it has an equivalent
irreversible moment: **the ledger row becoming part of the user's track record.** The ledger
is append-only and is never rewritten; once a return is admitted, it is the record.

So the settle order is:

```
1. replay              → dated returns + decision journal
2. compute the delta   → which dates are NEW to the ledger
3. for each new decision date without a trace row:
       build trace → compute_hash() → save_trace()  → INSERT paper_decision_traces
4. append the new paper_daily_returns rows
5. flush / commit
```

The reasoning is recorded **before** the return it explains is admitted. That is the strongest
temporal claim paper can honestly make, and it is worth exactly as much as it sounds:
off-chain ordering inside one process, not a block-number proof. The UI copy says so.

**Step 3 must not fail the settle.** A Redis outage cannot be allowed to freeze every user's
paper ledger — the ledger is the honest number of record and it keeps advancing. But per the
fail-soft rule, the *absence* is loud and durable: the failure is written to Postgres in the
same transaction as the ledger rows (§7), so the gap survives the process that caused it.

**Cost.** Step 3 is one keccak (microseconds) plus one Redis `SET` per *decision*, not per
bar. A monthly-rebalance strategy makes ~12 decisions a year. The settle path is dominated by
`run_dsl_backtest`; this is noise against it.

---

## 4. Owner stamping (#1556) — and the sentinel landmine

`save_trace` is the single write choke point and stamps ownership on the way in. It resolves
the owner **from the vault address** — and a paper deployment has no vault.

The escape hatch is already documented in `save_trace`: *"A caller that already knows the
owner sets `owner_user_id`/`owner_wallet` itself; the presence of either key suppresses the
lookup."* The paper writer takes that path, exactly as the generation-trace writer does:

```python
await state.save_trace({
    ...,
    "vault_address": "",                    # there is no vault
    "owner_user_id": dep.owner_user_id,     # suppresses the vault lookup
    "owner_wallet":  dep.owner_wallet,      # even when both are None
})
```

`PaperDeployment` carries both columns (`owner_user_id`, `owner_wallet`) for exactly the
canonical-identity transition this needs, so the stamp is a direct copy — no lookup, no DB
round-trip at read time, and a Postgres outage cannot downgrade a private paper trace to a
public one.

### The sentinel is a real landmine — do not use it

`construction_trace.py` uses `UNBOUND_VAULT = "0x0000…0000"` for "no vault yet". **Do not
reuse it here.** Trace the read gate:

`is_trace_visible` → no `owner_user_id`, no `owner_wallet` → falls through to
`is_public_trace_vault(vault_address)` → address is non-blank → `PUBLIC_TRACE_VAULTS` is
**unset** → returns `True` and logs one warning. (Unset is the live state, not an
assumption: `grep -rn PUBLIC_TRACE_VAULTS` finds it in no `.env.example`, no `infra/`
terraform, and no compose file — nothing in this tree arms the allowlist.)

So an unstamped paper trace on the zero-address sentinel is **world-readable**, holdings and
all. An unstamped paper trace on `vault_address=""` is not: `is_public_trace_vault` returns
`False` for a blank address by explicit design ("An ownerless body with no vault behind it is
not a house artifact"). Blank is fail-closed twice over — the stamp, and then the blank-vault
floor if the stamp is ever missing. G3 in §8 demonstrates the difference rather than
asserting it.

### Fail closed on an unownable deployment

A `PaperDeployment` with **both** ownership columns null cannot be gated. Such a deployment
does not get a trace: publishing is skipped, the decision is recorded `status="unowned"` in
`paper_decision_traces`, and the settle logs an ERROR naming the deployment id. A trace we
cannot scope is worse than no trace, and a silent skip is worse than both.

---

## 5. Reachability (#1569): `decision_type` + `strategies_referenced`

#1569 makes traces reachable from the strategy passport via `GET /api/traces/?strategy_id=`.
Its matcher is deliberately narrow:

```python
STRATEGY_REFERENCE_DECISION_TYPES = frozenset({"rebalance", "rotation", "regime_change", "skip"})

def trace_references_strategy(trace: dict, strategy_id: str) -> bool:
    if trace.get("decision_type") not in STRATEGY_REFERENCE_DECISION_TYPES:
        return False
    refs = trace.get("strategies_referenced")
    if isinstance(refs, str):
        return refs == strategy_id          # WHOLE string, never substring
    if isinstance(refs, list | tuple | set | frozenset):
        return any(isinstance(r, str) and r == strategy_id for r in refs)
    return False
```

Two hard constraints fall straight out, and both are why the design conforms rather than
extends:

1. **`decision_type` must be `"rebalance"`.** A new `"paper"` or `"paper_rebalance"` value
   would be rejected twice over — by `list_traces`' `decision_type` regex
   (`^(construction|rebalance|rotation|regime_change|skip)$`) and by the frozenset above,
   which fails **silently**: the passport would render "no traces for this strategy" while
   traces existed. Silent unreachability on the provenance surface is the worst available
   outcome. `#1575`'s own anti-goal ("no schema fork of the trace record") points the same
   way. The venue is disclosed by `trigger` and `market_context.venue`, both hashed (§2).
2. **`strategies_referenced` must be `[dep.strategy_id]` exactly.** The match is exact
   string equality; `strategy_store.id` is `content_hash[:16]`, and the deployment already
   carries it as an FK. One element, no prefixes, no composite anchors — the two
   non-conforming writers the constant documents (arXiv ids and paper anchors on
   `construction` traces) are the cautionary example.

The read path then works with no new code: `assert_strategy_visible` answers *may you know
this strategy exists*, `can_read_trace` answers *may you read this row*, both run, and the
owner sees their paper trace on the passport while an anonymous caller gets a filtered list
and a 404 on the detail route.

**Sequencing.** `trace_references_strategy` and the `?strategy_id=` filter live on
`dbrowneup/user-reachable-traces` (#1569), not on `main`. The acceptance criterion
`GET /api/traces/?strategy_id=<sid>` therefore **depends on #1569 landing first**. Until it
does, paper traces are reachable through the owner-scoped `GET /api/traces/` and by id. The
build plan (§9) puts the conformance assertion in a test that pins the constant by import,
so if the frozenset ever changes shape the paper pipeline fails loudly rather than going
quietly unreachable.

**One UI obligation.** #1569's panel renders traces beside on-chain ones. A never-anchored
paper trace must render as its own honest state via `anchorState()` — "not anchored (paper)",
never "anchor pending", which asserts a registry write that was never attempted.

---

## 6. On-chain anchoring is default OFF

`PAPER_TRACE_ANCHOR` defaults to `false`. The existing anchor path is left untouched and
unreachable from the paper writer unless the flag is on **and** the deployment opted in.
Three arguments, in order of weight.

### 6.1 Consent — the decisive one

The commit-reveal `reveal()` call carries `fullTraceContent` **bytes on-chain** so the
contract can recompute the hash. For a paper trace that content is
`portfolio_before` / `portfolio_after` — the user's holdings — plus their strategy's spec
context. #1556 exists because *"the first user-owned vault that publishes a trace publishes
its full portfolio and reasoning to the internet."* Anchoring a paper trace by default does
exactly that, on purpose, **irreversibly**, for a simulation the user ran privately. Building
an ownership gate in one issue and defeating it by default in the next is not a coherent
posture.

Consent must be explicit, per deployment, and revocable-going-forward only (nothing already
on-chain can be recalled — the UI copy must say that before the user opts in). Hence
`PaperDeployment.anchor_traces` (default `false`), never a global switch.

### 6.2 Cost — real, and it scales with the wrong thing

Per the spec's own estimate, ~$0.01 per anchor, ~$0.02 for a commit+reveal pair. That was
sized for *"~20 agent decisions across 4 Tier-1 vaults during the demo window"*. Paper
decisions scale with **users × strategies × universe size × cadence**, and the house pays the
gas. A daily-rebalance deployment is ~250 decisions/year; 100 deployments is ~$500/year of
USDC gas to notarise simulated trades — and USDC *is* gas on Arc, from a DCW that has to be
funded. The spend is not the blocker on its own; spending it on the one class of decision
where nothing is at stake is.

### 6.3 It does not buy the property paper needs

| Property | Off-chain keccak (what paper gets) | On-chain anchor (what it would add) |
|---|---|---|
| Owner can verify the body was not edited | ✅ `/verify` re-derives the hash | — |
| Canonical bytes reproducible by anyone with the row | ✅ `/canonical` | — |
| Tamper by a *third party* detectable | ✅ | — |
| Tamper by *us* detectable | ❌ | ✅ |
| Third party can check the trace preceded the trade | ❌ | ✅ (`commitBlock < tradeBlock < revealBlock`) |
| Publishes the user's holdings permanently | — | ✅ (unwanted) |

The rows the anchor wins matter when *someone else's money* is at stake and the operator is
the adversary. In paper trading there is no counterparty, no capital, and no settlement. The
claim paper trading can honestly make is **"hashed and owner-verifiable"** — and the UI must
say that and not "on-chain proven". Anchoring is available for the user who wants a public,
permanent record of a paper track record (a real use case for a strategy author building
reputation) — as an opt-in, which is what it is.

### 6.4 What "on" looks like

Both gates true (`PAPER_TRACE_ANCHOR=true` **and** `dep.anchor_traces`) →
`trace_publisher.commit()` before the ledger append, `reveal()` after, populating the same
commit/reveal/temporal-binding fields the house agent writes, with the same rule: a failed
commit anchors **nothing** and says so, never a fallback anchor standing in for a commitment
(#714). No new contract surface, no new code path — the existing publisher, reached from a
second producer.

---

## 7. Loud failure for a decision that does not publish

The rule: *fail-soft is wrong for anything a claim depends on.* Trace coverage is exactly
such a claim. The failure states and what each does:

| State | Cause | Loud where |
|---|---|---|
| `published` | normal | — |
| `failed` | Redis down, save raised | row `status="failed"` + `error`; deployment `trace_gap_at` stamped; settle logs WARNING with the decision key; `advance_all` returns `trace_failed: n`; API reports it |
| `unowned` | deployment has neither owner column | row `status="unowned"`; settle logs **ERROR** with the deployment id (§4) |
| `disabled` | `PAPER_TRACE_PUBLISH=off` | row `status="disabled"`; settle logs WARNING **naming the env var**; boot logs ERROR once; API `trace_coverage.status = "disabled"`; UI renders the gap |

Three mechanisms make the absence visible rather than merely logged:

**1. Coverage is an accounting identity, checked.**

```python
decisions_detected == published + failed + unowned + disabled
```

asserted at the end of every `advance_deployment`. A mismatch means a decision fell out of
the pipeline without being counted, which is the failure mode that produces a silent zero.
It raises.

**2. The gap is durable and API-visible.** `paper_decision_traces` rows live in Postgres and
are written in the same transaction as the ledger rows they accompany, so a gap survives the
process that caused it. `GET /api/paper/deployments/{id}` gains:

```json
"trace_coverage": {
  "status": "ok" | "gap" | "disabled",
  "decisions": 14, "published": 12, "failed": 2, "unowned": 0, "disabled": 0,
  "first_gap_at": "2026-08-21", "kinds": ["rebalance"]
}
```

`status` is never derived from a bare `published > 0`. A deployment with any non-published
decision reports `"gap"`, and `PaperTrading.jsx` renders "2 of 14 decisions have no published
trace" — not a hidden discrepancy, and not a blank panel.

**3. Retry, because a gap should close itself.** The next settle re-attempts every
`failed`/`disabled` row (the decision key makes this idempotent), so a Redis blip heals on
the following tick instead of leaving a permanent hole. `unowned` never retries — it is a
data problem, not a transient one.

**What is deliberately *not* done:** the settle is not aborted on publish failure. The ledger
is the honest number of record and freezing every user's paper history behind a Redis outage
trades a visible gap for an invisible stall. The gap is the correct degraded state *because*
it is loud.

---

## 8. Adversarial guards

House rule: build the input that *should* fail, show it fails, before pushing. Every guard
below ships with a revert-demo in the PR body.

- **G1 — publishing disabled is loud.** Seed a deployment, advance one settle with
  `PAPER_TRACE_PUBLISH=off`. Assert: `trace_coverage.status == "disabled"`, `disabled >= 1`,
  a WARNING naming `PAPER_TRACE_PUBLISH` was emitted, and the deployment payload carries the
  gap. *Adversarial input:* the same run with the coverage accounting reverted to
  `published > 0` reports `"ok"` with zero traces — the test must fail.
- **G2 — a tampered trace does not re-derive.** Store a trace, flip one character of
  `reasoning`, assert `/verify` reports a hash mismatch. *Control (non-tautological):*
  mutating a **non-hashed** field (`arc_tx_hash`) must leave the hash valid — proving the
  test measures the hashed set and not "any write breaks it".
- **G3 — non-owner reads are blocked, and the sentinel is why blank wins.** Owner reads the
  trace by id and in the list; a different signed-in user gets 404 and an empty list;
  anonymous gets 404. *Adversarial input:* the identical row rewritten with
  `vault_address = UNBOUND_VAULT` and the owner stamp removed **is** readable anonymously
  with `PUBLIC_TRACE_VAULTS` unset — the leaks-without-the-gate control that makes G3 mean
  something (reuses `test_traces_ownership_gate.py` helpers per #1556's shape).
- **G4 — idempotency.** Advance the same deployment three times; assert exactly one trace per
  decision key. *Adversarial input:* drop the unique constraint and re-run → duplicates
  appear and the test fails.
- **G5 — reachability conformance.** Assert `trace_references_strategy(record, sid) is True`
  for a produced paper record, by **importing** `STRATEGY_REFERENCE_DECISION_TYPES` rather
  than hard-coding `"rebalance"`. *Adversarial input:* the same record with
  `decision_type="paper_rebalance"` returns `False` — pinning why the conforming value is not
  cosmetic. (Skipped with a reason until #1569 lands.)
- **G6 — marks never trace.** `grep` the marks module for `save_trace` / `ReasoningTrace` /
  `paper_decision_traces` → no hits; plus a behavioural test that a mark cycle produces zero
  new traces. Mirrors #1568's grep-verified anti-goal. (Lands with #1568; until then the grep
  runs against the settle path only.)
- **G7 — the journal is a no-op.** Same spec, same feed: `decision_journal=False` and
  `=True` must produce identical `equity_curve`, `sharpe_ratio`, `total_trades`. *Adversarial
  input:* an analyzer that places an order fails it.
- **G8 — no LLM on the settle path.** `grep` the paper trace builder for the LLM client
  imports → no hits. A prose claim ("deterministic, no LLM") that nothing enforces is the
  same defect, harder to grep for.

---

## 9. Build plan

Numbered, in dependency order. Each step is independently testable; steps 1–6 are one PR,
7–8 the follow-up.

**1. Decision journal analyzer (observer-only).**
`backend/archimedes/services/_fusion_helpers.py` — add `_DecisionJournal(bt.Analyzer)` per
§1.4; return it from `_build_analyzers()` and bind `_DecisionJournalAnalyzer` at module
level alongside the two existing ones.
`backend/archimedes/services/fusion_evaluator.py` — add `decision_journal: bool = False` to
`run_dsl_backtest`; add `decision_journal: list[dict] | None = None` to `BacktestMetrics`;
attach the analyzer and populate the field only when the flag is on.
*Tests* (`backend/tests/test_decision_journal.py`): journal populated for a spec that trades;
empty list for a spec that never enters; **G7** no-op parity.

**2. Replay surfaces dated decisions.**
`backend/archimedes/services/paper_trading.py` — `_sleeve_dated_returns` gains a sibling
`_sleeve_decisions(spec, sym, factory)`; new
`replay_decisions(spec_dict, deployed_at) -> dict[date, list[dict]]` grouping legs by
`decided_on`, filtered to `>= deployed_at`. `replay_spec` is untouched.
*Tests:* legs grouped by date; dates before `deployed_at` excluded; a multi-symbol universe
yields one entry per date with both legs.

**3. Migration + model: `paper_decision_traces`.**
`backend/migrations/versions/d7c41f9b2e58_paper_decision_traces.py`, `down_revision =
"85ca5310b7a1"` (verified the single head at write time with the real script directory;
`alembic heads` now returns `d7c41f9b2e58` alone).
Columns: `id`, `deployment_id` FK→`paper_deployments.id` ON DELETE CASCADE (matching the
ledger's own cascade), `decision_date` (Date), `trace_id`, `trace_hash`, `status`
(`published|failed|unowned|disabled`), `provenance` (`settle|backfill`, the value hashed
into the published trace — without it every re-replay reads as drift, because the
provenance label is inside the hash), `error` (Text, nullable), `created_at`, `updated_at`.
`UniqueConstraint(deployment_id, decision_date)` — the idempotency key.
`PaperDeployment` gains `anchor_traces` (Boolean, default `false`), `trace_gap_at`,
`trace_drift_at` (DateTime, nullable).
`backend/archimedes/models/paper_store.py` — `PaperDecisionTrace` model.
*Tests:* migration up/down; `alembic heads` returns exactly one; unique constraint rejects a
duplicate key; cascade removes rows with the deployment.

**4. The trace builder (pure, no I/O).**
New `backend/archimedes/services/paper_trace.py`, shaped after `construction_trace.py`
(stops at the hash, never touches the chain or Redis):
`build_paper_trace(dep, spec, decision_date, legs, before, after) -> ReasoningTrace`,
`_render_reasoning(...)` (§2.1), `_market_context(...)`, `_paper_hashes(spec)` (§2.4).
*Tests* (`backend/tests/services/test_paper_trace.py`): field-by-field against §2's table;
`decision_type is DecisionType.REBALANCE`; `vault_address == ""`; `confidence == 0.0`;
`consulted_paper_hashes == []` with no resolvable content hash; hash is stable across two
builds of the same decision and changes when a hashed field changes; **G8** no-LLM grep.

**5. The publisher.**
`backend/archimedes/services/paper_trace.py` —
`publish_paper_trace(session, dep, trace) -> str` (status): explicit owner stamp per §4,
`save_trace` through the choke point, `paper_decision_traces` row, anchoring only when both
gates are on (§6.4).
Config in `backend/archimedes/config.py` + `.env.example`: `PAPER_TRACE_PUBLISH` (default
`on`), `PAPER_TRACE_ANCHOR` (default `false`), `PAPER_TRACE_BACKFILL_MAX` (default `500`).
Boot check in `main.py`: ERROR once if `PAPER_TRACE_PUBLISH` is off.
*Tests:* owner stamp copied verbatim from the deployment; both-null deployment →
`status="unowned"` + ERROR; `save_trace` raising → `status="failed"` + the settle survives;
anchor path not reached with either gate off.

**6. Wire the settle path.**
`services/paper_trading.py` — `advance_deployment` gains the §3 ordering, the coverage
identity assert, the retry of `failed`/`disabled` rows, the backfill bound, and drift
stamping; its return dict gains `decisions`, `traces_published`, `trace_failed`.
`advance_all` sums them.
`backend/archimedes/api/paper_routes.py` — `deployment_summary` gains `trace_coverage` (§7).
*Tests* (`backend/tests/test_paper_trace_pipeline.py` — the issue's named acceptance file):
one settle on a seeded deployment produces exactly one trace; the trace round-trips
`/verify`; **G1** disabled-is-loud + revert-demo; **G2** tamper + non-hashed control; **G3**
owner/non-owner/anonymous + the sentinel leaks-without-the-gate control; **G4** idempotency
across three advances; **G5** `trace_references_strategy` conformance (import the constant);
coverage identity holds across every failure state.

**7. UI (follow-up PR).**
`ui/src/components/PaperTrading.jsx` — render `trace_coverage` including the gap and the
"trades traced; no-trade bars are not yet traced" disclosure; link each decision to its
trace. `ui/src/trace-binding.js` `anchorState()` — an honest "not anchored (paper)" state,
never "anchor pending" (§5). Opt-in anchoring toggle with the irreversibility copy (§6.1).
*Tests* (`ui/test/`): the gap string renders; the paper trace does not render as pending;
the disclosure copy is pinned so it cannot silently rot (#1568's pattern).

**8. Follow-ups, tracked not shipped.**
SKIP tracing via an in-engine journal hook with its own no-op proof (§1.4) · #1569's panel
labelling paper vs on-chain · the marks-never-trace grep re-pointed at #1568's module once it
lands (G6).

---

## 10. Anti-goals, sequencing, out-of-scope

**Anti-goals (from #1575, all enforced by a check above):**

- No live-execution path changes. `chain/agent_runner.py`, `chain/executor.py`, and the
  contracts are untouched; the only shared code is the existing `save_trace` and (opt-in)
  `trace_publisher`.
- No schema fork of the trace record. Same `ReasoningTrace`, same `_HASH_FIELDS`, same
  canonical JSON, same keccak. §5 is the reason this is a constraint and not a preference.
- No new on-chain writes by default. §6, two independent gates.
- Do not slow the settle unboundedly. One keccak + one Redis `SET` per *decision*; the
  bounded backfill; publish failure never blocks the ledger.
- Marks never decide, and never trace. §1.3, G6.

**Sequencing:** the `?strategy_id=` acceptance depends on #1569; G6's grep depends on #1568.
Neither blocks steps 1–6, and both have their conformance pinned by test rather than by
prose.

**Out of scope:** intraday signal evaluation (v2, needs an ADR — see the intraday plan §1.3's
three landmines); position-vector marking; a paper→live promotion path; any change to
`PAYMENTS_DRY_RUN` or the custody posture.
