# Repo Map — Contracts / Chain / Provenance

Scope: `contracts/src`, `contracts/test`, `contracts/abis`, `backend/archimedes/chain`, provenance/vault specs.
Read-only scout from 2026-07-04. No source edited.

> **Historical security snapshot.** Findings may be resolved or invalid on current HEAD. Re-verify every claim before disclosure or remediation.

## TL;DR

Commit-before-trade + commit-reveal provenance is **fully implemented and tested in Solidity source**,
but is **NOT on the live path**: the cached ABIs (and, per code comments, the deployed bytecode) are the
**pre-v1.5** versions. Worse, even after a redeploy the **backend `commit()` call is the wrong arity
(4-arg, missing `tradeId`)** and no `tradeId` is computed, so commit-before-trade cannot work end-to-end.
Net: the "trace existed before the trade" / temporal-binding claim is currently unenforced on live path →
direct Tier‑0 claim-integrity violation.

---

## Ranked Findings

### 1. [CRITICAL — provenance truth / claim integrity] Source↔deploy↔ABI drift: commit-before-trade not live

- Evidence:
  - Source `commit/executeTrade/reveal/pendingTradeCommitment` implemented:
    `contracts/src/ReasoningTraceRegistry.sol:120-260`, iface `contracts/src/interfaces/IReasoningTraceRegistry.sol:78-140`.
  - Cached ABI is v1 only: `contracts/abis/ReasoningTraceRegistry.json` functions =
    `['getTraceById','getTraces','getTracesByVault','owner','publishTrace','renounceOwnership','traceCount','transferOwnership','verifyTrace']` — **no `commit`, `executeTrade`, `reveal`**.
  - `contracts/abis/Vault.json` constructor = 10 args (no `traceRegistry`); source `contracts/src/Vault.sol:139-166` constructor = **12 args incl `_traceRegistry`** (immutable, required).
  - `contracts/abis/VaultFactory.json` constructor = 5 args; source `contracts/src/VaultFactory.sol:36-48` = **6 args incl `_traceRegistry`**.
  - Backend confirms deployed contracts are old: `executor.py:create_vault` docstring — "deployed `VaultFactory.createVault` has a 5-arg selector … constructs the Vault with `Ownable(msg.sender)`"; `trace_publisher.supports_commit_reveal()` exists precisely because "deployed ABI may still be v1 … until the registry is redeployed (#588)".
- Impact: On live path `Vault.rebalance` (old bytecode) has **no `traceRegistry.executeTrade` gate** → trades settle with **no commit-before-trade**. `temporal_binding_source` degrades to `"none"`, `temporal_binding_valid=False` always (`agent_runner.py` off_chain_data). UI/pitch "commit-before-trade / trace existed before trade" is not backed by live path.
- Suggested issue title: `APIN - Chain - Redeploy v1.5 ReasoningTraceRegistry + Vault/VaultFactory and refresh cached ABIs (unblock commit-before-trade on live path)`

### 2. [CRITICAL — bug that breaks the primitive even after redeploy] Backend `commit()` arity + missing `tradeId`

- Evidence:
  - Source/iface `commit` is **5-arg**: `commit(address vault, bytes32 contentHash, uint64 claimedExecutionTime, bytes32 tradeId, bytes tradeIntentSummary)` (`IReasoningTraceRegistry.sol:98-104`). Foundry tests use it 5-arg (`contracts/test/Vault.t.sol:159-161`, `Registry.t.sol:462-471`).
  - Backend calls it **4-arg (no tradeId)**:
    - Circle path `abi_function="commit(address,bytes32,uint64,bytes)"` — `trace_publisher.py` (`commit`).
    - Raw path `registry.functions.commit(vault_addr, content_hash_bytes, claimed_execution_time, trade_intent_summary)` — 4 positional args.
  - `agent_runner.py:_commit_trace` never computes `tradeId = keccak256(abi.encode(tokensIn,amountsIn,tokensOut,amountsOut))`; it only builds `intent_summary`. Commit also runs **before** `executor.execute_trades` derives the final `tokensIn/amountsIn/...` arrays (decimal conversion + USDC-leg filtering in `executor.py:execute_trades`), so the exact `tradeId` the vault will recompute in `rebalance()` is not known at commit time.
- Impact: Against a v1.5 registry the commit call fails ABI encoding → falls to `publishTrace` → `Vault.rebalance.executeTrade(tradeId)` finds **no matching commitment → reverts** (`Vault.sol:rebalance`, tested `Vault.t.sol:1346 test_revert_rebalance_without_commit`). Commit-before-trade is unshippable until the backend computes the exact `tradeId` and commits it *before* executing that identical trade.
- Suggested issue title: `APIN - Chain - Wire tradeId through commit→execute so commit-before-trade works end-to-end (backend commit() is 4-arg, no tradeId)`

### 3. [HIGH — architecture/ordering] Commit precedes final trade-array construction

- Evidence: `agent_runner._process_vault` computes `trades` (symbol/USD level), then `_commit_trace(...)`, then `chain_executor.execute_trades(vault, trades)`. The on-chain `tradeId` binds the **raw** rebalance arrays (`keccak256(abi.encode(tokensIn,amountsIn,tokensOut,amountsOut))`, `Vault.sol:rebalance`), which are only formed inside `execute_trades` (BUY→`int(amount*1e6)`, SELL→oracle-derived raw, USDC legs dropped `executor.py:execute_trades`).
- Impact: No single source builds the exact arrays, hashes them, commits, then executes them. Refactor needed: a "plan → tradeId → commit → execute(plan)" pipeline (executor exposes the built arrays; agent_runner commits their hash).
- Suggested issue title: `APIN - Chain - Refactor rebalance to build trade arrays once, commit their tradeId, then execute the identical plan`

### 4. [MEDIUM — provenance doc/behavior mismatch] `verifyTrace` NatSpec says SHA-256, code is keccak256

- Evidence: `IReasoningTraceRegistry.sol` `verifyTrace` doc "True if SHA-256(fullTrace) matches"; implementation `ReasoningTraceRegistry.sol:verifyTrace` uses `keccak256(fullTrace)`. All backend hashing (`trace.compute_hash`, `bytes.fromhex(...)`) assumes keccak256.
- Impact: Doc-level provenance ambiguity for external verifiers; low code risk but this is exactly the "claims must be true" surface.
- Suggested issue title: `APIN - Docs - Fix ReasoningTraceRegistry NatSpec: verifyTrace uses keccak256, not SHA-256`

### 5. [MEDIUM — address/config drift] Synth address map is transitional / partial pending T3.2 redeploy

- Evidence: `client.py:_SYNTH_DEFAULTS`/`_ORACLE_DEFAULTS` hold only `sSPY`,`sBTC`; all other SSOT synths resolve **only** from `ARC_<SYMBOL>_ADDRESS` env or are excluded. Several `""` empty defaults for `amm_router_address`, `vault_factory_address`, `reasoning_trace_registry_address`, etc. Per-synth overrides read from `os.environ` (not pydantic env_file) — only work if `load_dotenv` ran (documented, but a fragile invariant for new entrypoints).
- Impact: A redeploy that forgets any `ARC_*` env var silently drops that synth/contract from the live path (empty address → excluded), not a loud error. Post-redeploy the transitional defaults become stale/wrong addresses if env not set.
- Suggested issue title: `APIN - Chain - Fail-loud on missing core contract addresses after T3.2 redeploy; regen ARC_* env from deploy output`

### 6. [MEDIUM — deploy tooling] No Foundry `Deploy.s.sol`; only `deploy_contracts.py`

- Evidence: CLAUDE.md + client.py docstring reference `Deploy.s.sol`, but `find` shows no `Deploy*.s.sol`; only `backend/archimedes/scripts/deploy_contracts.py`. Need to confirm this script deploys the **new** 6-arg VaultFactory / 12-arg Vault constructors and the v1.5 registry, and re-exports ABIs to `contracts/abis/`.
- Impact: If the deploy path doesn't construct with `_traceRegistry` and re-copy ABIs, finding #1 recurs on every redeploy.
- Suggested issue title: `APIN - Infra - Verify deploy_contracts.py deploys v1.5 constructors + refreshes contracts/abis/ (ABI regen step)`

### 7. [LOW-MED — test coverage gap] Solidity commit-before-trade well-tested; backend path is not

- Evidence: Solidity coverage is strong — `Registry.t.sol` (commit/reveal happy path, hash mismatch, same-block, empty tradeId, single-use) and `Vault.t.sol:1346-1396` (`test_revert_rebalance_without_commit`, `test_rebalance_commit_is_single_use`, `test_revert_rebalance_commit_for_different_trade`). No backend test asserts the agent computes the matching `tradeId` and that commit→execute round-trips (because it currently can't — see #2).
- Impact: The end-to-end provenance guarantee has zero backend integration coverage; the arity bug (#2) would be caught by one hermetic test mocking the chain boundary.
- Suggested issue title: `APIN - Tests - Add backend commit→execute round-trip test asserting tradeId binding (mock registry+vault at chain boundary)`

### 8. [LOW — duplicate/overlapping surfaces] Two registries + partial interface duplication

- Evidence: `AssetRegistry.sol` + `StrategyRegistry.sol` coexist (CLAUDE.md notes intentional). `src/generated/SyntheticUniverse.sol` is a generated contract not in the "11 deployed" list — confirm it's referenced/deployed or dead. Interfaces mirror concretes 1:1 under `src/interfaces/` (fine), but cached `contracts/abis/` ships both `Vault.json` and `IVault.json` etc. — drift risk if only one is regenerated.
- Impact: Minor; mostly a "which ABI does the loader read" clarity issue (`contracts.py` ContractLoader — worth confirming it loads concrete `Vault.json`, so ABI staleness in #1 directly bites it).
- Suggested issue title: `APIN - Chain - Audit ContractLoader ABI sources + retire/confirm generated SyntheticUniverse.sol`

---

## Live-funds risk notes (mostly positive — recent audits landed)

- Vault non-custodial handoff is real in code: `executor._apply_non_custodial_ownership` does `setAgent(backend)` + `transferOwnership(user/gov)`, refuses to leave owner==agent (loud warn). Owner-only guards on oracle/slippage/pause setters (`Vault.sol:setTokenOracles onlyOwner`, `setMaxSlippageBps` cap 500bps). Inflation-attack dead-shares guard + CEI allowance checks present and tested (`Vault.t.sol:354, 265-301`).
- Reverted-tx handling is correct: `_confirm_receipt` raises `TradeReverted/VaultCreationReverted` on `status==0` (prevents recording a trace for a failed trade).
- Residual: `create_vault` Circle path falls back to `getVaults()[-1]` when `VaultCreated` event not decoded — logged as warning but could still return a wrong vault under concurrent creation. Worth a follow-up.

## Start Here

Open `backend/archimedes/chain/trace_publisher.py` (`commit`) next to
`contracts/src/interfaces/IReasoningTraceRegistry.sol:98-104` — the 4-arg-vs-5-arg `tradeId`
mismatch is the single change that gates whether commit-before-trade can ever be true on the live path.
Then `backend/archimedes/chain/agent_runner.py:_process_vault`/`_commit_trace` for the commit-before-execute ordering.

## Clarification Questions

1. Is the #588 redeploy of v1.5 registry + new Vault/VaultFactory already scheduled, or should the backend
   `commit()` fix (#2) land first behind the `supports_commit_reveal()` guard?
2. Does `deploy_contracts.py` construct VaultFactory with `_traceRegistry` and re-export ABIs to
   `contracts/abis/`, or is ABI regen a separate manual step? (Determines if #1 recurs each deploy.)
3. Should missing core `ARC_*` contract addresses fail-loud at boot (vs the current silent-exclude), given
   the security-ships-with-product value in CLAUDE.md?
4. Is `src/generated/SyntheticUniverse.sol` deployed / on the live path, or dead generated code?
5. Confirm the intended hash primitive is keccak256 everywhere (finding #4) — any external verifier docs
   promising SHA-256?
