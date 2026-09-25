# Execution / Trading Agent Society — Spec (DRAFT)

> **Status:** DRAFT, 2026-06-28. Seed-and-refine artifact for T1.1's *second*
> agent society — the **Type‑2 / Execution** society that trades and rebalances the
> strategies users **deploy**. Distinct from the generation/debate society
> (`docs/specs/multi-agent-debate-spec.md`), which produces and rigor-gates
> *candidate* strategies. This spec describes what happens **after Deploy**: a
> deployed strategy's DSL "blueprint" is executed and kept in line with its own
> instructions, on-chain, under a hard custody boundary.
>
> **Scope fence (mirror of the generation spec's fence):** the generation society
> is **generation-only**; *this* society is **execution-only**. It NEVER authors a
> strategy, NEVER re-derives target weights from a brief, NEVER touches the rigor
> gate. It consumes a *frozen blueprint* and acts within it. The two societies meet
> at exactly one artifact: the **deployed-strategy blueprint** (§2).
>
> **Lineage to read together:**
> - [`docs/user-stories.md`](../user-stories.md) — the locked product spine (execute → monitor is this society's lane)
> - [issue #760](https://github.com/aprin-labs/archimedes/issues/760) — the "blueprint" concept this spec formalizes (Type‑2 Agent Readiness Dependency Map)
> - [`docs/specs/strategy-lifecycle-spec.md`](./strategy-lifecycle-spec.md) — `Deployed → Active → Completed`; this society drives those transitions
> - [`docs/specs/vault-semantics-spec.md`](./vault-semantics-spec.md) + [`docs/specs/commit-reveal-trace-spec.md`](./commit-reveal-trace-spec.md) — the on-chain settlement + provenance contracts
> - [`docs/specs/selection-bias-corrections-spec.md`](./selection-bias-corrections-spec.md) + [`docs/specs/xia-2026-protocols.md`](./xia-2026-protocols.md) — `V_check`, Source Tracking, Hierarchy of Truth (consumed here, enforced upstream)

---

## §0. The two societies, side by side

Archimedes runs **two** agent societies over one strategy library. They share data
(the library, the corpus, the regime detector, the trace registry) but are
operationally independent — different entry points, different cadence, different
authority.

```
┌──────────────────────────────────────┐        ┌──────────────────────────────────────┐
│  GENERATION / DEBATE SOCIETY (Type 1) │        │  EXECUTION / TRADING SOCIETY (Type 2) │
│  agents/generation_pipeline.py        │        │  chain/agent_runner.py  (StrategyRunner)│
│                                       │        │                                       │
│  brief → debate → N candidates →      │ DEPLOY │  blueprint → schedule → regime+drift  │
│  rigor gate → best → PERSIST          │ ─────▶ │  → V_check → commit → trade → reveal   │
│                                       │ (hand- │                                       │
│  authority: NONE on-chain.            │  off)  │  authority: rebalance-only. NEVER     │
│  writes StrategyRecord + Passport.    │        │  withdraw-to-platform. (PR #731)      │
│  cadence: per Generate request (SSE). │        │  cadence: per-tick loop (default 300s)│
└──────────────────────────────────────┘        └──────────────────────────────────────┘
        run_generation(...)                              StrategyRunner.tick()
        terminal event: "done"                           terminal: commit→trade→reveal
```

The hand-off is a **frozen artifact, not a function call**. The generation society
finishes (`run_generation` emits `done`, persists a `StrategyRecord` +
`StrategyPassport`); the user clicks Deploy; a vault is created; this society
*discovers* the deployed strategy on its next tick and begins executing it. No live
coupling — the societies can be redeployed independently.

**What already exists vs. what this spec adds.** The execution loop is **largely
built and live** today — `chain/agent_runner.py:StrategyRunner.tick()` is the real
per-tick cycle, `chain/executor.py:ChainExecutor.execute_trades()` builds the real
on-chain rebalance, the custody boundary (PR #731) and commit-before-trade (#589 /
#755) are enforced on-chain, and #783 killed the silent buy-and-hold fallback by
driving live signals from validated DSL specs (`services/strategy_signal_evaluator.py:_spec_signal`).
**This spec's job is to (a) name that machinery as a first-class "society," (b)
formalize the blueprint hand-off contract, and (c) define the agent roster + the
metered-compute tie-in as the build-out path** — not to invent the loop from scratch.

---

## §1. Purpose + scope

### In scope

1. **Consume a deployed strategy's blueprint** (§2) — the DSL spec, target weights,
   rebalance cadence/triggers, regime conditions, and provenance — and execute it
   per its own instructions.
2. **Decide when to rebalance** — read the live GMM regime + portfolio drift, and
   rebalance only when the strategy's blueprint says to (drift threshold crossed,
   cadence bar reached, or regime condition triggered).
3. **Gate every action deterministically** — `V_check` (Xia §5) on the target weights
   before any swap; refuse the rebalance if weights are invalid, regardless of any
   model's confidence.
4. **Build + submit the on-chain rebalance** — diff current vs. target, construct the
   swap legs, commit the reasoning trace *before* the trade (#589), execute, reveal.
5. **Drive lifecycle transitions** — `Deployed → Active` (first funded rebalance) and
   `Active → Completed` (window end), per the lifecycle spec.
6. **Be meterable** — every rebalance run is a discrete, attributable unit of compute
   that the nanopayment/metered-compute layer (§6) can price and freemium-cap.

### Explicitly out of scope (the fence)

- **Generating or re-deriving a strategy.** Target weights come from the blueprint /
  the strategy's own signal rule, never from a brief. (`PortfolioConstructor.construct`
  *throttles* blueprint weights by regime; it does not author them — its
  `_fallback_weights` path has no production caller.)
- **The rigor gate.** DSR/PBO/OOS/look-ahead run upstream in the generation society;
  this society trusts the `passes_rigor`/passport verdict it inherits and never
  re-runs it.
- **Withdraw / custody.** The agent has `onlyManager` (rebalance) authority on the
  `Vault`, never owner authority (oracles, slippage cap, pause, `setAgent`,
  withdraw-to-platform). See §4.

---

## §2. The hand-off contract — what a deployed strategy carries

The blueprint is the **single artifact** that crosses the society boundary. Issue
#760 frames it as the "agent behavior requirements" (its sections 1, 2, 4 — the
strategy library, the signal evaluators, and the runtime parameters); this spec pins
it to the **already-persisted** carriers so the two societies stay decoupled.

### 2.1 Where the blueprint lives today (the real carriers)

A deployed strategy is **already** persisted by the generation society's tail
(`generation_pipeline.py:_persist_candidate` → `upsert_strategy(...)` +
`ingest_passport(...)`). The execution society reads it back through the strategy
provider and the vault metadata — it does **not** receive a fresh object.

| Blueprint field | Type / carrier | Source of truth (verified symbol) | Consumed by (execution side) |
|---|---|---|---|
| **DSL spec** (`entry`/`exit`/`position_sizing`/…) | `dict` on `StrategyRecord.strategy_spec` | `validate_strategy_spec()` → `StrategySpec` (`services/strategy_dsl.py`); set at persist time | `strategy_signal_evaluator._spec_signal()` interprets it live (#783) |
| **Asset universe** | `list[str]` (tickers/synths) | `derive_asset_universe(brief.asset_classes)` overrides spec's `asset_universe` in fusion | `evaluate_strategies()` maps `asset_universe → synth` via `synth_map` |
| **Target weights** (per asset) | derived per-tick from the signal rule | `aggregate_signals()` averages per-asset votes, applies `usdc_floor` | `StrategyRunner.tick()` step 3 |
| **Rebalance cadence** | `rebalance_frequency ∈ {daily,weekly,monthly}` | `StrategySpec.rebalance_frequency` (DSL) | (cadence agent — §3; today the runner ticks every `AGENT_INTERVAL_SECONDS`) |
| **Rebalance trigger (drift)** | scalar threshold | `agent_runner._DRIFT_THRESHOLD = 0.15` (hardcoded today) | `_compute_trades()` skips legs with `abs(drift) < threshold` |
| **Regime conditions** | regime-aware throttle | `REGIME_MULTIPLIER` (`portfolio_constructor.py`) + per-asset regime bias | `PortfolioConstructor.construct(regime=…, ensemble_consensus=…)` |
| **Provenance** | `source_arxiv_ids`, `methodology_hash`, paper hashes | `StrategyPassport` (ingested at persist) | `_paper_hashes_from_signals()` → trace `consulted_paper_hashes` |
| **Per-vault scope** | `list[str]` strategy_ids | `VaultMetadata.get_strategy_ids()` | `agent_runner._get_vault_strategy_ids()` |

> **Drift to fix in v2 of this spec (#760 follow-up).** The blueprint's cadence and
> drift trigger are currently *implicit*: cadence collapses to the global tick
> interval and the drift threshold is a module constant, so a "monthly, 5%-drift"
> strategy and a "daily, 20%-drift" strategy are executed identically. Promoting
> `rebalance_frequency` + a per-strategy drift threshold to **honored** blueprint
> fields (read by the cadence/rebalance-decision agents in §3) is the headline
> behavioral upgrade this society needs. Until then, document the gap honestly — do
> not claim per-strategy cadence on stage.

### 2.2 Blueprint schema (the data format #760 asks for)

A **derived, read-only** projection assembled by the execution society at deploy-
discovery time — NOT a new persisted table (it is a view over the carriers above):

```jsonc
{
  "strategy_id": "cand_bull_a1b2",          // StrategyRecord.id
  "vault_address": "0x…",                    // the deployed vault
  "dsl_spec": { /* validated StrategySpec dict — entry/exit/sizing/… */ },
  "asset_universe": ["sSPY", "sQQQ"],        // resolved synths
  "rebalance": {
    "cadence": "monthly",                    // from rebalance_frequency  (OPEN: honor it — §3)
    "drift_threshold": 0.15,                 // per-strategy  (OPEN: currently global _DRIFT_THRESHOLD)
    "regime_aware": true                      // does PortfolioConstructor throttle apply
  },
  "provenance": {
    "source_arxiv_ids": ["0706.1497", "1704.03022"],
    "methodology_hash": "0x…",
    "passes_rigor": true,                    // inherited verdict — NOT re-checked here
    "admissible": true
  },
  "lifecycle": { "state": "Deployed", "window_start": null, "window_end": null }
}
```

**Invariant:** the execution society treats `dsl_spec` + `provenance` as **frozen**.
If the spec is invalid at execution time, the live path returns a **loud FLAT with a
reason** (`_spec_signal` on `DSLError`), never a silent buy-and-hold (#783) — the
strategy simply holds cash until a human re-deploys a corrected blueprint.

---

## §3. Agent roster

Four roles. Three are **deterministic gates / builders** (cheap, auditable, no LLM
budget); the regime/decision role reads the live GMM detector. This mirrors the
generation spec's "deterministic-critic budget trick" — keep the expensive reasoning
in the generation society; the execution society is mostly mechanical and provable.

| # | Agent | Job | Backed by (real symbol) | Determinism |
|---|---|---|---|---|
| 1 | **Scheduler / Cadence** | Decide *whether this tick is a decision point* for each deployed strategy (cadence bar reached? window open?). | today: the `StrategyRunner.run()` loop (`AGENT_INTERVAL_SECONDS`); **to build:** honor `rebalance_frequency` per strategy | deterministic |
| 2 | **Rebalance‑Decision** | Read live regime + portfolio drift; decide *what* the target should be and whether drift warrants a trade. | `GmmRegimeDetector.classify()/get_current_regime()` + `EnsembleConsensus` + `PortfolioConstructor.construct()` + `_compute_trades()` (`_DRIFT_THRESHOLD`) | deterministic (regime read is a model, the decision is rule-based) |
| 3 | **Risk / Guardrail** | Deterministic validity gate on the target weights; SKIP the whole rebalance on failure regardless of confidence. | `chain/v_check.py:VCheck(weights_bps=…).run()` + `ChainExecutor._validate_trade_liquidity()` (fail-closed on thin pools) | **fully deterministic** |
| 4 | **Execution** | Build the on-chain rebalance: commit trace → execute swaps → reveal; drive lifecycle transitions. | `chain/agent_runner.StrategyRunner._process_vault()` → `_commit_trace` → `chain_executor.execute_trades` → `_reveal_trace` | deterministic build; on-chain effects |

### 3.1 The tick (real control flow, `StrategyRunner.tick()`)

```
EVERY AGENT_INTERVAL_SECONDS (default 300):

  1. provider.list_strategies()                 → deployed blueprints
  2. strategy_evaluator.evaluate_strategies(...) → per-asset signals
       └─ DSL-spec strategies → _spec_signal (live↔backtest parity, #783)
       └─ legacy/curated      → keyword evaluator  (Faber/TSMOM/Vol-managed/…)
  3. aggregate_signals(usdc_floor)              → raw target weights
  4. classify_market_regime()                   → GMM (degrades → VixRegimeDetector)
     PortfolioConstructor.construct(regime, consensus, base_weights)  → throttled targets
  5. discover + scope vaults (VaultFactory poll; per-vault strategy_ids)
  6. FOR EACH vault:
       read_portfolio → _compute_trades (drift gate)
       if no trades  → SKIP trace (deduped)            ◀── agent #2 says "aligned"
       VCheck(weights_bps).run()                        ◀── agent #3 GUARDRAIL
         └─ fail → SKIP trace "v_check_failed", no trade
       _commit_trace(...)   (COMMIT hash on-chain, claimedExecutionTime)  ◀── #589
       execute_trades(...)  (real swaps; InsufficientLiquidityError → SKIP+retry)
       _reveal_trace(...)   (hash-only reveal of the SAME committed trace; empty storagePointer)  ◀── agent #4
  7. save_heartbeat()
```

Every branch already emits a **reasoning trace** (REBALANCE / SKIP with an explicit
trigger: `aligned`, `empty_vault`, `v_check_failed`, `insufficient_liquidity`,
`execution_failed`). That trace stream IS the execution society's "thinking out loud"
surface — the Type‑2 analogue of the generation society's SSE event vocabulary.

### 3.2 Regime read — honest degradation

The Rebalance‑Decision agent reads `GmmRegimeDetector`, which **falls back to the
rule-based `VixRegimeDetector`** whenever no fitted `gmm_model.pkl` exists, history
`< 22` ticks, or VIX/price are missing — the expected steady state until the offline
fit runs (`gmm_regime_health()` reports `degraded`). On total snapshot failure the
regime degrades to `"unknown"` and `PortfolioConstructor` applies the conservative
`REGIME_MULTIPLIER_NONE = 0.7`. The decision is never blocked by an absent model.

---

## §4. The custody boundary it MUST respect

This is the load-bearing constraint and the #1 product invariant
(`docs/architectural-principles.md` §3). **Owner ≠ agent.** Two enforcement layers,
both already live:

### 4.1 On-chain authority split (PR #731, `Vault.sol`)

- The agent address holds **`onlyManager`** authority: `rebalance()`,
  `setTargetAllocations()`, `setTokenOraclesFromRegistry()` (registry-allowlisted
  oracles only). That is the **entire** surface this society touches.
- The **owner** (the depositing user, or a governance cold key) holds **`onlyOwner`**:
  `setTokenOracles`, `setMaxSlippageBps`, `setAgent`, `pause`, `setAssetRegistry`,
  `setPlatformFeeRecipient`. The agent can **never** call these — verified by the
  `onlyOwner` modifier and the in-contract comment: *"the agent must not be able to
  redefine the oracles that feed the rebalance slippage floor … defeating the 'agent
  has rebalance-only authority' invariant."*
- **No withdraw-to-platform path exists.** `withdraw`/`redeem` burn the **caller's
  own** shares (or a spender with allowance); there is no function that lets the agent
  move user funds to a platform address. Non-custodial by construction.
- The handoff is applied at vault creation by `executor._apply_non_custodial_ownership`:
  `setAgent(backendSigner)` then `transferOwnership(user|governance)`. It **refuses to
  hand ownership to the agent address** and **fails loud** (warns, skips transfer)
  rather than minting a custodial vault when no distinct owner is resolvable
  (`ARC_VAULT_GOVERNANCE_ADDRESS`).

### 4.2 Commit‑before‑trade (#589 / #755)

Every swap is bound to a reasoning trace committed in an **earlier block**:

- `Vault.rebalance()` recomputes `tradeId = keccak256(abi.encode(tokensIn, amountsIn,
  tokensOut, amountsOut))` and calls `traceRegistry.executeTrade(tradeId)` — which
  **reverts** if no fresh commitment binds this `(vault, trade)`. A trade physically
  cannot settle without a prior commit. The constructor wires `traceRegistry` so a
  vault can never exist in a state where `rebalance()` skips the check (#755).
- The Python side honors this causally: `_process_vault` does
  `_commit_trace` (anchors the keccak256 with `claimedExecutionTime`, lead window
  `_COMMIT_EXECUTION_LEAD_S = 60`) **before** `execute_trades`, then `_reveal_trace`
  reveals the **same** committed `ReasoningTrace` object (settlement fields are added
  outside the hashed set, so the binding holds). `temporal_binding_valid` is persisted
  as `commit_block < trade_block`.

**Invariant the society must preserve in every new code path:** *no swap without a
prior committed trace bound to that exact trade*, and *no owner-only call from the
agent*. Any agent that builds a rebalance MUST route it through
`chain_executor.execute_trades` (which goes through `Vault.rebalance`) — never a
direct router swap that bypasses the registry check.

---

## §5. Where it plugs into existing code

| Concern | File / symbol | Role in this society |
|---|---|---|
| **Society entry point + loop** | `chain/agent_runner.py` → `StrategyRunner.tick()` / `run()` | the per-tick orchestrator; agents #1–#4 are stages within it |
| **Signal evaluation (blueprint → signals)** | `services/strategy_signal_evaluator.py` → `evaluate_strategies` / `_spec_signal` | DSL-spec interpretation (live↔backtest parity, #783); legacy keyword fallback for curated strategies |
| **Regime + drift sizing (agent #2)** | `services/portfolio_constructor.py` → `PortfolioConstructor.construct` / `compute_position_scale` | regime + ensemble-consensus throttle on blueprint weights (NOT weight authorship) |
| **Regime read** | `services/gmm_regime_detector.py` → `classify` / `get_current_regime` / `gmm_regime_health` | live regime; degrades to `VixRegimeDetector` |
| **Risk guardrail (agent #3)** | `chain/v_check.py` → `VCheck.run`; `chain/executor.py` → `_validate_trade_liquidity` | deterministic weight validity + fail-closed liquidity preflight |
| **Execution build (agent #4)** | `chain/executor.py` → `ChainExecutor.execute_trades` / `read_portfolio` / `create_vault` | builds + submits the `Vault.rebalance` tx (Circle signer or raw key); revert-aware via `_confirm_receipt` |
| **Trace commit/reveal** | `chain/agent_runner.py` → `_commit_trace` / `_reveal_trace`; `chain/trace_publisher.py` | commit-before-trade + hash-only reveal (empty `storagePointer`; no IPFS pin) |
| **Vault contract** | `contracts/src/Vault.sol` → `rebalance` (`onlyManager`), owner-only setters | the custody-enforcing settlement contract |
| **State / heartbeat** | `services/redis_state.py` → `AgentStateStore` | regime, ensemble consensus, heartbeat, per-vault last-rebalance, trace index |
| **Per-vault scope** | `models/chat.py:VaultMetadata` + `agent_runner._get_vault_strategy_ids` | restricts a vault to its owner-selected strategies |

The society is a **standalone process** (`python -m archimedes.chain.agent_runner`),
separate from the FastAPI app the generation society lives in — reinforcing the
operational independence in §0.

---

## §6. Metered‑compute / nanopayment tie‑in

Each **rebalance run is a discrete, attributable unit of compute** — exactly the
shape the nanopayment marketplace ([issue #713](https://github.com/aprin-labs/archimedes/issues/713),
x402 + Circle Gateway, sub-cent USDC) prices. This society is the natural metering
boundary.

### 6.1 What gets metered

- **Unit of metering = one `_process_vault` rebalance** that results in a real trade
  (`trigger="strategy_signal_drift"`, trace REVEALED). SKIPs (`aligned`,
  `v_check_failed`, …) are **free** — they cost no swap and produce no value, so they
  must not be billed (and the runner already dedups identical aligned SKIPs to avoid
  trace spam).
- **Provenance for billing is already on the trace:** `vault_address`, `trace_hash`,
  `trade_tx_hash`, `commit_block`/`trade_block`, `strategies_referenced`. A nanopayment
  charge can be bound to the same `tradeId`/`trace_hash` the on-chain commit uses, so
  the charge is *provably* tied to a real, committed rebalance.

### 6.2 Freemium cap + Lambda metering

- **Lambda-metered runs:** when the execution society runs per-vault rebalance
  evaluations as discrete invocations (a roadmap migration off the always-on tick
  loop), each invocation is a billable compute event — Lambda duration + a per-trade
  nanopayment. The always-on `StrategyRunner` loop is the demo path; the metered-
  Lambda path is the scale path. Keep `tick()` decomposable into per-vault units so a
  vault's rebalance can be lifted into its own invocation without rewriting the loop.
- **Freemium cap:** a free tier gets N agent-driven rebalances per window (or
  cadence-capped to e.g. monthly); beyond that, rebalances require a funded
  nanopayment / x402 settlement before `execute_trades` runs. The cap check is a new
  deterministic gate placed **between** the drift decision (agent #2) and the V_check
  (agent #3) — a "rebalances remaining?" guard that, on exhaustion, emits a SKIP trace
  (`trigger="freemium_cap_reached"`) instead of trading. It is a *spend* gate, distinct
  from the generation society's *model-entitlement* gate
  (`services/model_gate.py:enforce_model_entitlement`, which gates premium LLM models
  by wallet) — but both share the per-user spend-cap substrate.

> **Out of scope for the draft, named for v2:** the exact x402 challenge/settle
> handshake and the Gateway revenue split live in #713's spec; this society only needs
> to expose the **metering hook** (a billable-rebalance event with a trace-bound id)
> and the **cap gate** (a deterministic pre-trade check). Both are additive — they do
> not touch the custody or commit-before-trade invariants.

---

## §7. Phased build plan

The loop exists; the work is to **name it as a society, formalize the blueprint, and
honor the blueprint's cadence/trigger fields** — then add metering. Each phase ships
behind the existing `AGENT_DRY_RUN` switch so on-chain effects stay gated until proven.

### Phase 0 — Skeleton + blueprint projection (draft-first)
- [ ] Land **this spec** in `docs/specs/`; cross-link from the generation spec and #760.
- [ ] Implement the **read-only blueprint projection** (§2.2) as a pure view over
      `StrategyRecord` + `VaultMetadata` + `StrategyPassport` — *no new table*. A
      function `build_blueprint(strategy_id, vault_address) -> Blueprint` that the
      runner can log per deployed strategy at tick start.
- [ ] **Acceptance:** hermetic test asserting a deployed fusion strategy's persisted
      `strategy_spec` + arxiv ids round-trip into a `Blueprint` with `passes_rigor`
      inherited (not recomputed). `env -i … pytest backend/tests/test_blueprint.py -q` → `0 failed`.

### Phase 1 — Honor blueprint cadence + per-strategy drift (the #760 behavioral gap)
- [ ] Scheduler agent (#1): read `rebalance_frequency` per deployed strategy; a tick is
      a decision point for a strategy only when its cadence bar has elapsed since its
      last rebalance (`AgentStateStore` last-rebalance key already exists per vault).
- [ ] Rebalance‑Decision agent (#2): replace the global `_DRIFT_THRESHOLD` with a
      per-strategy threshold sourced from the blueprint (default 0.15 preserves current
      behavior).
- [ ] **Acceptance:** a "monthly" strategy does not trade on consecutive daily ticks
      with sub-threshold drift; a "daily" one does. Test against a fake clock + fixture
      portfolios. **Anti-goal:** do not weaken the existing drift skip or the dedup.

### Phase 2 — Make the roster explicit + the trace stream first-class
- [ ] Refactor `tick()`/`_process_vault` into the four named stages (§3) with a
      typed `RebalanceDecision` carrier between them (mirrors the generation society's
      `_CandidateResult`), so each agent is independently testable.
- [ ] Ensure every stage's SKIP/REBALANCE branch carries a structured `trigger` (most
      already do) and surface the trace stream as the Type‑2 "reasoning" view.
- [ ] **Acceptance:** unit tests per stage; the guardrail stage proven to SKIP on an
      invalid-weights fixture without ever reaching `execute_trades` (mock the executor,
      assert not called). **Anti-goal:** do not bypass `VCheck` or commit-before-trade.

### Phase 3 — Metering hook + freemium cap (additive)
- [ ] Emit a **billable-rebalance event** (trace-bound id) on every REVEALED trade; no
      event on SKIPs.
- [ ] Insert the deterministic **freemium-cap gate** between agents #2 and #3; on
      exhaustion emit `trigger="freemium_cap_reached"` SKIP.
- [ ] **Acceptance:** capped user's N+1th rebalance SKIPs (executor mock not called);
      under-cap rebalance proceeds. **Anti-goal:** never bill a SKIP; never let the cap
      gate touch the custody or commit-before-trade path.

### Phase 4 — Lambda metering (scale path, post-demo)
- [ ] Lift per-vault rebalance into a discrete invocation; wire Lambda duration + the
      per-trade nanopayment to #713's settlement. Always-on loop stays the demo path.

---

## §8. Open questions (own-before-bake)

1. **Per-strategy drift threshold source** — is it a blueprint/DSL field, a vault
   setting, or a strategy-class default? (Önder owns the turnover/cost tradeoff that
   sets it — see `transaction-cost-turnover-model.md`.) Do **not** bake a single
   global value into the spec beyond the current 0.15 default.
2. **Cadence vs. tick granularity** — a "monthly" cadence on a 300s tick means
   ~8,640 ticks between rebalances; confirm the last-rebalance key is the source of
   truth for "elapsed," not the loop counter.
3. **Multi-strategy vaults** — a vault scoped to several strategies aggregates their
   signals (`aggregate_signals`); does each contributing strategy's cadence apply, or
   the vault's? (Likely: rebalance when *any* scoped strategy's cadence + drift fire.)
4. **Metering unit under aggregation** — one charge per vault-rebalance, or per
   contributing strategy? (Billing simplicity argues per vault-rebalance.)
5. **Regime-condition triggers in the blueprint** — beyond the throttle, should a
   blueprint be able to say "go flat on CRISIS"? Today regime only *scales* exposure
   (`REGIME_MULTIPLIER[CRISIS]=0.1`); a hard regime exit is a blueprint capability to
   design, not assume.

---

## §9. Invariants checklist (every PR in this society must preserve)

- [ ] **Owner ≠ agent.** No agent code path calls an `onlyOwner` setter or any
      withdraw-to-platform function. (Grep the diff for `setTokenOracles(` without
      `FromRegistry`, `setMaxSlippageBps`, `setAgent`, `pause`, `transferOwnership`
      from agent paths.)
- [ ] **No swap without a prior committed trace** bound to that exact `tradeId`.
      Rebalances route through `chain_executor.execute_trades` → `Vault.rebalance`,
      never a direct router swap.
- [ ] **`V_check` is unconditional** — invalid weights SKIP, regardless of confidence.
- [ ] **Liquidity preflight fails closed** — a probe error treats the pool as thin.
- [ ] **No silent buy-and-hold** — an invalid/missing spec returns loud FLAT-with-reason
      (#783), never always-long.
- [ ] **SKIPs are free** — the metering hook never bills a non-trading tick.
- [ ] **Generation-only / execution-only fence holds** — this society never authors a
      strategy or runs the rigor gate.
