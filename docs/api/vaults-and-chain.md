# Vaults & On-Chain API

> **status:** current
> **owner:** Dan Browne
> **updated:** 2026-08-31

The marketplace's on-chain surface: vault discovery and creation, off-chain
vault metadata (display name, strategy bindings), reasoning-trace publish/
verify, the AMM swap preview, deployed contract addresses, and the health/
root endpoints operators and CI probe. Every route here was checked directly
against its router source (`vaults_routes.py`, `traces_routes.py`,
`swap_routes.py`, `config_routes.py`, `main.py`) — no undocumented route was
found in their scope.

**Vaults are non-custodial by design.** `POST /api/vaults/create` deploys the
vault with the backend signer, then transfers on-chain `Ownable` ownership to
the caller's verified linked wallet and pins the backend only as the
rebalance-only agent — so `owner == user` and `agent == backend`. A
compromised backend/agent key can rebalance but cannot re-point the oracle,
widen slippage, pause, or otherwise drain the vault.

**Server-side rigor enforcement, not just a UI gate.** Every path that binds
a strategy to a vault — `POST /api/vaults/create`, `POST
/api/vaults/metadata` — re-checks each strategy against the live rigor gate
at the caller's chosen `strictness_level` **before** spending gas or
persisting the link, and refuses (`422`) any strategy that hasn't passed at
that level. The always-on correctness floors (look-ahead audit, positive OOS,
DSR ≥ 0.50) hold at every strictness level — a caller can trade statistical
confidence for breadth, never bypass the floor entirely. This exists because
the frontend's own Deploy gate is defense-in-depth, not the guarantee: a
direct API caller must hit the same wall a UI user does.

**Auth model.** Reads (`GET /api/vaults/`, `GET /api/vaults/{address}`, `GET
/api/vaults/{address}/health`, `GET /api/vaults/{address}/metadata`, and
everything under `/api/traces/`, `/api/swap/`, `/api/config/`, and the health/
root endpoints) are anonymous. Anything that spends gas, writes vault
metadata, or derives allocations (`POST /api/vaults/create`, `POST
/api/vaults/metadata`, `POST /api/vaults/{address}/derive-allocations`)
requires a Better Auth account session **and** a verified linked wallet
(`require_linked_wallet`). `POST /api/traces/publish` is `internal-key`
(`X-Internal-Agent-Key`, `hmac.compare_digest` against
`INTERNAL_AGENT_API_KEY` — fails closed if that env var is unset). Examples
needing a session assume an authenticated cookie jar at `/tmp/session.jar`.

## Vaults

### GET /api/vaults/
List vaults for the marketplace leaderboard. | **Auth**: anonymous

Request: query `tier: int(1..2)|null, sort_by: "aum"|"return_24h"|"return_7d"|"sharpe"|"created_at" = "aum", order: "asc"|"desc" = "desc", limit: int(1..100)=20, offset: int(>=0)=0`.
Response (`VaultListResponse`): `{vaults: [VaultSummaryResponse{address, name, symbol, tier, creator, aum_usdc, share_price, return_24h/7d/30d/inception: float|null, returns_source: "oracle_baseline"|"unavailable", sharpe_ratio, management_fee_pct, performance_fee_pct, is_agent_assisted, depositors, last_rebalance, created_at}], total: int}`. Return fields are `null` unless backed by a real oracle-price baseline comparison — `returns_source` names the provenance honestly rather than defaulting to `0`.
Errors: none explicit.

```bash
curl -s "https://archimedes-arc.com/api/vaults/?tier=1&sort_by=aum&limit=20"
```

### POST /api/vaults/create
Deploy a new vault on Arc via `VaultFactory`. | **Auth**: linked-wallet |
**Flags**: rate limit `5/minute` (disabled under `TESTING`)

Request (`VaultCreateRequest`): `{name: str(1..64), symbol: str(1..16), management_fee_bps: int=0, performance_fee_bps: int(0..3000)=0, agent_assisted: bool=true, strategy_ids: [str]=[], strictness_level: int(1..5)=1}`.
Response (`VaultCreateResponse`): `{vault_address: str, strategy_ids: [str]}`.
Errors: `422` — a bound strategy fails the rigor gate at `strictness_level` (server-side enforcement, see above); `503` — chain executor unavailable; `500` `Vault deployment failed` (generic — the raw chain/DB exception is never echoed to the client).

```bash
curl -s -X POST https://archimedes-arc.com/api/vaults/create \
  -b /tmp/session.jar -H "Content-Type: application/json" \
  -d '{"name": "Momentum Vault", "symbol": "MOMV", "strategy_ids": ["<strategy_id>"], "strictness_level": 1}'
```

### GET /api/vaults/{address}/health
Vault health snapshot — agent liveness, rebalance staleness, AUM trend. |
**Auth**: anonymous

Request: path `address`.
Response: `{vault_address, agent_alive: bool, last_heartbeat: str|null, last_rebalance: str|null, rebalance_age_seconds: float|null, aum_trend_pct: float, snapshot_count: int, latest_snapshot: dict|null, sharpe_drift: {available: bool, reason: str}, recent_events: [dict]}`. `agent_alive` is `true` only when the agent's Redis heartbeat is under 10 minutes old. `sharpe_drift.available` is currently always `false` (`reason: "baseline_backtest_sharpe_unavailable"`) — a strategy-specific backtest baseline isn't wired in yet, so drift is honestly reported as unavailable rather than computed from a stand-in value.
Errors: none explicit — degrades to empty/default fields on a backing-store failure.

```bash
curl -s https://archimedes-arc.com/api/vaults/<address>/health
```

### GET /api/vaults/{address}
Full vault detail page data — holdings, target allocations, equity curve,
recent traces. | **Auth**: anonymous

Request: path `address`.
Response (`VaultDetailResponse`): `{address, name, symbol, tier, creator, aum_usdc, share_price, is_agent_assisted, management_fee_pct, performance_fee_pct, high_water_mark, holdings: [VaultHolding{symbol, token_address, amount, value_usdc, weight_pct}], target_allocations: [VaultHolding], return_24h/7d/30d/inception: float|null, returns_source: "oracle_baseline"|"unavailable", sharpe_ratio, max_drawdown, equity_curve: [PricePoint], strategy_ids: [str], current_regime: str|null, recent_traces: [TraceResponse], depositors, last_rebalance, created_at}`.
Errors: `404` `Vault not found` — unknown address. `400`/`502` — the vault exists but its **fee guard** refuses it (issue #1138): `400` when fees are verifiably over-cap (the reason names the values), `502` when fees can't be verified on-chain (fail-closed rather than silently trusting an unverifiable number).

```bash
curl -s https://archimedes-arc.com/api/vaults/<address>
```

### POST /api/vaults/metadata
Store off-chain vault metadata (display name, strategy bindings) — the
client-signed deploy path's choke point. | **Auth**: linked-wallet | **Flags**:
rate limit `10/minute` (disabled under `TESTING`)

Request (`VaultMetadataRequest`): `{vault_address: "0x"+40hex, name: str(<=64)="", symbol: str(<=16)="", creator_address: str="", strategy_ids: [str]=[], strictness_level: int(1..5)=1}`.
Response (`VaultMetadataResponse`): `{vault_address, name, symbol, creator_address, strategy_ids, created_at: str|null}`.
Errors: `403` "Only the vault's on-chain owner may edit its metadata." — caller's linked wallet isn't the vault's actual on-chain `Ownable` owner (closes an IDOR, #916: metadata ownership is read from the contract, never "whoever wrote first"); `503` — on-chain owner unreadable, fails closed rather than letting an unverifiable caller claim the vault; `409` "Vault metadata belongs to another account" — a different account already owns this metadata row; `422` — any `strategy_ids` entry fails the rigor gate at `strictness_level`; `500` `Vault metadata update failed` (generic).

```bash
curl -s -X POST https://archimedes-arc.com/api/vaults/metadata \
  -b /tmp/session.jar -H "Content-Type: application/json" \
  -d '{"vault_address": "0x1234567890123456789012345678901234567890", "name": "Momentum Vault", "strategy_ids": ["<strategy_id>"], "strictness_level": 1}'
```

### GET /api/vaults/{address}/metadata
Get off-chain vault metadata. | **Auth**: anonymous

Request: path `address`.
Response (`VaultMetadataResponse`): same shape as the `POST` above.
Errors: `404` "No metadata for this vault" — no metadata row exists yet.

```bash
curl -s https://archimedes-arc.com/api/vaults/<address>/metadata
```

### POST /api/vaults/{address}/derive-allocations
**(roadmap-hidden — the API is live and enforced; no frontend surface calls
it. A repo-wide search of `ui/src` finds zero call sites for
`derive-allocations`.)** Derive Kelly-sized target allocations from selected
strategies — returns the derived weights for the UI to submit as an
on-chain `setTargetAllocations` tx via the user's own wallet; does **not**
execute on-chain itself. | **Auth**: linked-wallet | **Flags**: rate limit
`20/minute` (disabled under `TESTING`)

Request (`SetAllocationsRequest`): `{strategy_ids: [str]=[], usdc_floor_pct: float(0..80)=20.0, risk_profile: "fixed_income"|"conservative"|"moderate"|"aggressive"|"hyper_risky"="moderate", strictness_level: int(1..5)=1}`.
Response (`SetAllocationsResponse`): `{allocations: [{symbol, token_address, weight_bps}], total_bps: int (should equal 10000), strategy_count: int, risk_profile: str, sized_strategies: dict[str,float] (per-strategy capital fraction = passport half-Kelly × profile multiplier), excluded_strategy_ids: [str] (selected strategies sized to zero — rigor-gate fail or no stored kelly_fraction)}`. With no strategies selected, allocations fall back to an even USDC-floor + synthetic-universe split.
Errors: none explicit beyond the linked-wallet gate — a strategy that doesn't clear `strictness_level` sizes to zero (`excluded_strategy_ids`) rather than erroring the whole request.

```bash
curl -s -X POST https://archimedes-arc.com/api/vaults/<address>/derive-allocations \
  -b /tmp/session.jar -H "Content-Type: application/json" \
  -d '{"strategy_ids": ["<strategy_id>"], "risk_profile": "moderate", "strictness_level": 1}'
```

## Reasoning traces

### GET /api/traces/
List reasoning traces — merges off-chain (Redis) metadata with on-chain IDs,
falling back to an on-chain-only listing when Redis is unavailable. |
**Auth**: anonymous

Request: query `vault_address: str|null, decision_type: "construction"|"rebalance"|"rotation"|"regime_change"|"skip"|null, limit: int(1..100)=20, offset: int(>=0)=0`.
Response (`TraceListResponse`): `{traces: [TraceResponse], total: int}`. `TraceResponse` carries `id, vault_address, decision_type, trigger, timestamp, reasoning, confidence, trace_hash, arc_tx_hash: str|null, is_verified: bool, regime_at_decision, trades_executed, strategies_referenced, commit_tx_hash/commit_block_number, reveal_tx_hash/reveal_block_number, trade_tx_hash/trade_block_number, temporal_binding_valid: bool|null, temporal_binding_source: str="none"`. On the on-chain-only fallback path (Redis unavailable), a trace's off-chain-only `decision_type` cannot be recovered from the registry alone — the served value is the literal string `"unknown"` (#1356: never fabricate `"rebalance"` as a guess), outside the filterable enum above.
Errors: none explicit — a Redis outage degrades to the on-chain-only path rather than erroring.

```bash
curl -s "https://archimedes-arc.com/api/traces/?vault_address=<address>&limit=20"
```

### GET /api/traces/{trace_id}
Get a single reasoning trace by ID (off-chain UUID or on-chain integer ID). |
**Auth**: anonymous

Request: path `trace_id`.
Response: `TraceResponse` (same shape as the list item).
Errors: `404` `Trace not found` — unknown ID, in either off-chain or on-chain form.

```bash
curl -s https://archimedes-arc.com/api/traces/<trace_id>
```

### POST /api/traces/publish
Publish a reasoning trace — computes its hash, anchors it on Arc, persists it
off-chain. Internal-only, called by the agent runner. | **Auth**:
internal-key

Request (`TracePublishRequest`): `{vault_address: str, decision_type: "construction"|"rebalance"|"rotation"|"regime_change"|"skip"="construction", trigger: str="manual", reasoning: str="", confidence: float=0.0, market_context: dict={}, portfolio_before: dict={}, portfolio_after: dict={}, trades_executed: [dict]=[], strategies_referenced: [str]=[]}`.
Response (`TracePublishResponse`): `{id: str (uuid), trace_hash: str (keccak256), arc_tx_hash: str|null, is_anchored: bool, timestamp, vault_address, decision_type}`.
Errors: `400` — invalid `decision_type`. `403` `Forbidden` — missing/wrong `X-Internal-Agent-Key`. `503` — off-chain persistence failed; the response names whether the on-chain anchor still landed (`"Trace anchored on-chain (tx {hash}) but off-chain persistence failed — retry publish."`) so a retry doesn't silently produce a trace with an anchor that can never be re-verified against its own content.

```bash
curl -s -X POST https://archimedes-arc.com/api/traces/publish \
  -H "X-Internal-Agent-Key: $INTERNAL_AGENT_API_KEY" -H "Content-Type: application/json" \
  -d '{"vault_address": "<address>", "decision_type": "rebalance", "reasoning": "Rotated into momentum sleeve.", "confidence": 0.82}'
```

### GET /api/traces/{trace_id}/verify
Verify a reasoning trace against its on-chain anchor. | **Auth**: anonymous
| **Flags**: rate-limit exempt

Request: path `trace_id`.
Response (`TraceVerifyResponse`): `{trace_id: int, trace_hash, is_verified: bool, verification_mode: "hash_matched"|"anchored_only"|"failed", agent, vault, on_chain_timestamp: int, details: str, temporal_binding_valid: bool|null, commit_block_number, trade_block_number, reveal_block_number}`. `verification_mode` names which of the three actually happened: `hash_matched` — the off-chain `trace_hash` was re-fetched from the on-chain receipt and compared byte-for-byte; `anchored_only` — the store was reachable but had no off-chain record for this id, so only the on-chain anchor itself was confirmed (zero hashes compared); `failed` — mismatch, missing receipt, or never anchored. No default — every response names one of the three, so a caller can't mistake "nothing was compared" for a real hash match.
Errors: `404` `Trace not found` — unknown ID. `503` "Trace store temporarily unavailable — retry verification." — Redis unreachable; deliberately distinguished from `404` so a caller retries instead of concluding the trace doesn't exist, and from `anchored_only` so a store outage can never be reported as a verification result (mirrors `/canonical`'s `503` below).

On-chain verification is **O(1)**: the handler fetches the receipt for the
trace's cached `arc_tx_hash` and decodes the `TracePublished` event directly,
rather than the prior O(N) `getTracesByVault → getTraceById` scan that
returned 504 on vaults with 40+ traces.

```bash
curl -s https://archimedes-arc.com/api/traces/<trace_id>/verify
```

### GET /api/traces/{trace_id}/canonical
Get the canonical JSON used to compute a trace's hash — the exact bytes a
verifier re-hashes to check `trace_hash`. | **Auth**: anonymous

Request: path `trace_id`.
Response: `text/plain` (raw canonical JSON string; not wrapped in an envelope).
Errors: `404` `Trace not found` — unknown ID. `503` "Trace store temporarily unavailable — retry." — Redis unreachable; deliberately distinguished from `404` so a hash-reverification consumer retries instead of concluding the trace doesn't exist.

```bash
curl -s https://archimedes-arc.com/api/traces/<trace_id>/canonical
```

## Swap (AMM)

**(roadmap-hidden — both routes below are live and enforced; no frontend
surface calls them. A repo-wide search of `ui/src` finds no Swap/Exchange
component and zero call sites for `/api/swap/*`.)**

### GET /api/swap/quote
Preview a swap via the AMM router before the user signs anything. |
**Auth**: anonymous | **Flags**: rate limit `30/minute` (disabled under
`TESTING`)

Request: query `token_in: str (address), token_out: str (address), amount_in: float(>0)`.
Response (`SwapQuoteResponse`): `{token_in, token_out, amount_in, amount_out, price_impact_pct, fee_pct: 0.3, min_amount_out}`.
Errors: `400` "Quote failed — check the token pair and amount." — any chain/web3 failure is collapsed to this generic message; the raw exception (which can leak RPC internals, contract addresses, or revert reasons) is logged server-side only, never echoed to the client.

```bash
curl -s "https://archimedes-arc.com/api/swap/quote?token_in=<usdc_address>&token_out=<synth_address>&amount_in=100"
```

### GET /api/swap/pools
List AMM pools and reserves. | **Auth**: anonymous

Request: none.
Response (`PoolListResponse`): `{pools: [PoolResponse{address, token0, token1, symbol0, symbol1, reserve0, reserve1, tvl_usdc, volume_24h_usdc: 0.0, fee_pct, apr_pct: null, total_supply}], total: int}`. `volume_24h_usdc` is always `0.0` (not wired) and `apr_pct` is always `null` — both honest placeholders, not computed estimates.
Errors: none explicit — a per-pool read failure silently drops that pool from the list rather than failing the whole request.

```bash
curl -s https://archimedes-arc.com/api/swap/pools
```

## Config

### GET /api/config/contracts
All deployed contract addresses — what the frontend (or any direct on-chain
caller) needs to call the chain itself. | **Auth**: anonymous

Request: none.
Response (`ContractAddressesResponse`): `{usdc, synthetic_factory, amm_router, vault_factory, reasoning_trace_registry, asset_registry, price_oracle, synthetics: dict[str,str] (symbol -> address), pools: dict[str,str]|null (pair -> address), vaults: dict[str,str]|null (symbol -> address), chain_id: int, rpc_url: str}`. `pools`/`vaults` are `null` when the on-chain read failed (RPC error) — distinct from `{}`, which means the chain was read and genuinely reports zero. A `null` must render as a failed/unread state, never as a measured zero (#1356).
Errors: none explicit.

**Contract counts come from this endpoint, not from prose** — per
`docs/CONVENTIONS.md`, a stale count in a doc is worse than no count.

```bash
curl -s https://archimedes-arc.com/api/config/contracts
```

## Health and root

### GET /api/health (and GET /health)
Primary health check — Docker healthcheck, ALB target-group health, CI/CD. |
**Auth**: anonymous | **Flags**: rate-limit exempt

Request: none.
Response: `{status: "ok"|"degraded", service: "archimedes-backend", version: str (git SHA), chain_connected: bool, human_count, agent_count, real_users, corpus_papers, corpus_db_count, corpus_source, corpus_last_intake, artifact_hash, paper_rerank_model_live: bool, corpus_embedded_at_rest: bool, corpus_embedded_at_rest_reason: str, rerank_candidate_cap: int, corpus_kg_built: bool, corpus_kg_entities: int, corpus_kg_relations: int, corpus_artifact_present: bool, llm_provider, llm_backend, llm_model, llm_available, llm_reason: str, llm_has_api_key, llm_has_auth_token, llm_has_base_url, paper_rag: str, paper_rag_reason, regime_detector: str, regime_detector_reason, risk_data: str, risk_data_reason, strategy_count: int, ready: bool|null, stale_unready_probes: [{probe: str, age_s: float}], stale_unready_threshold_s: float|null}`. `status` is deliberately still `"ok"` (HTTP 200) even when `chain_connected: false` — a transient Arc RPC blip must not cascade the whole ECS service down; the disconnect is instead logged loudly (`HEALTH_CHAIN_DISCONNECTED`) for a CloudWatch metric-filter alarm. `paper_rerank_model_live`/`corpus_embedded_at_rest`/`corpus_kg_built`/`corpus_kg_relations`/`corpus_artifact_present` are claim-integrity fields (issues #778, #1488) — they report what has actually been built on top of the manifest-seeded `papers` table, never a constant; today none of embeddings, KG, or a pipeline artifact exist in prod, so this endpoint reports that honestly rather than implying semantic retrieval that isn't there. `paper_rerank_model_live` is process-local (a model object is loaded in THIS worker) and is deliberately separate from `corpus_embedded_at_rest`, which is derived from the ORM schema and is false because no stored-vector column exists; `rerank_candidate_cap` is published because candidates past it are appended at score 0.0 and never scored. `fusion_enabled: bool` was published here until 2026-09-03; it was dropped (deck Q4 follow-up) once `ARCHIMEDES_FUSION_ENABLED` was retired and the value could only be a constant `true` — no consumer was found, and `test_health_always_answers.py::TestNoReportedFieldWasDropped::test_fusion_enabled_is_not_published` now pins it absent.

**Bounded-probe fields (#1592, extended to the LLM at #1044).** Three components are read through an outbound probe with its own deadline — `chain`, `oracle`, and `llm` — and each publishes `{prefix}_probe_state: "live"|"stale_cached"|"probe_timeout"|"probe_error"`, **always present**. `{prefix}_probe_age_s: float|null` and `{prefix}_probe_reason: str` appear **only when the fresh probe missed**, so their presence is itself the alarm and their absence is the all-clear; on `probe_timeout` the age is `null` because no reading was ever taken and reporting one would be an invention. A `stale_cached` value is a real prior measurement served with its age attached — never a fresh reading, and never silently. `llm_reason` is the LLM's substantive companion to `oracle_reason`: `""` when the backend is live, otherwise a sentence naming the actual cause (`ollama unreachable at …`, `LLM_MODEL is unset …`, `… is not pulled (run \`ollama pull llama3.1\`)`), because `llm_available: false` alone cannot distinguish them.

**Readiness fields (#1818 P3).** `ready: false` means the DB-backed probes (`corpus`, `corpus_db`, `corpus_meta`, `paper_rag`, `risk_data`) have been serving `stale_cached` readings for longer than `stale_unready_threshold_s` (env `HEALTH_STALE_UNREADY_S`, default 900) — i.e. this task is alive but can no longer read its database, the state that sat behind a green `/health` for ten hours during the 2026-09-03 outage. **This endpoint's status code does not change**: it is the ALB target-group check and stays 200 for the same reason `status` does. The 503 that gets a wedged task replaced belongs to `/health/ready` below, which the container health check polls. `stale_unready_threshold_s: 0` means the rule is switched off by env, so `ready: true` under it is not a verdict. `ready: null` (with `stale_unready_threshold_s: null`) means the verdict could not be computed on this poll at all — the three readiness fields are reporting only, and are computed inside a `try` so that a failure here cannot turn the ALB's check into a 500 and drain every target at once; read `null` as "unknown", never as ready, and ask `/health/ready`, which computes the verdict independently and is what the container check acts on.
Errors: none — always 200 (fail-soft by design; see `status` above).

```bash
curl -s https://archimedes-arc.com/api/health
```

### GET /api/health/ready (and GET /health/ready)
Readiness probe for the **container** health check (`backend/Dockerfile`
`HEALTHCHECK`, mirrored in `infra/ecs.tf`) — never for the ALB target group. |
**Auth**: anonymous | **Flags**: rate-limit exempt

Request: none.
Response: `{ready: bool, service: str, version: str (git SHA), stale_unready_threshold_s: float, stale_probes: [{probe: str, age_s: float}], probes: {<name>: {state: "live"|"stale_cached"|"probe_timeout"|"probe_error", age_s: float|null, reason: str}}}`.
Runs exactly the five DB-backed `/health` probes (`corpus`, `corpus_db`, `corpus_meta`, `paper_rag`, `risk_data`) through the same bounded-probe cache and the same 0.8 s budget, so it inherits `/health`'s "answers, never waits" contract and the two endpoints can never disagree about how old a reading is.
Errors: `503` with the same body and `ready: false` — **any** of those probes has been `stale_cached` for longer than `HEALTH_STALE_UNREADY_S` (default 900 s). Liveness is unchanged and is still `/health`'s job: a 503 here means "alive, and unable to read its database", and the container check failing replaces **one** task while the ALB keeps every other target in service. A probe that has never completed (`probe_timeout`, cold start) and a probe that raised (`probe_error`) are deliberately **not** unready — the rule fires on a task that could read and stopped, never on one that has not read yet. Set `HEALTH_STALE_UNREADY_S=0` to disable the rule. A console edit to the live task definition works immediately but only until the next deploy, because the pipeline rewrite (`.github/scripts/ecs_rewrite_task_def.py`) re-pins the name to `900` on every deploy; the durable pull-back is `HEALTH_STALE_UNREADY_VALUE = "0"` in that script.

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://archimedes-arc.com/health/ready
```

### GET /health/paper-rag
Dedicated paper-RAG health probe — the one endpoint allowed to pay the
~521 MB model-load cost (the ALB-polled `/health` deliberately does not). |
**Auth**: anonymous | **Flags**: rate-limit exempt

Request: none.
Response: `{paper_rag: str, reason: str}`.
Errors: none.

```bash
curl -s https://archimedes-arc.com/health/paper-rag
```

### GET /api/health/amm (and GET /health/amm)
AMM pool liquidity health — per-pool status for operator/judge probes. |
**Auth**: anonymous | **Flags**: rate-limit exempt

Request: none.
Response: `{status: "ok", pool_count: int, pools: [{address, token0, token1, reserve0, reserve1} | {address, error: str}]}` on success.
Errors: `503` `{status: "chain_disconnected", reason: "Cannot reach Arc RPC"}` — Arc RPC unreachable. `503` `{status: "amm_pools_not_initialized", reason: "...", pools: []}` — no pools exist yet. `503` `{status: "amm_health_check_failed", reason: "..."}` — any other failure; the raw exception is logged server-side only (never echoed) and this endpoint **never returns 404**.

```bash
curl -s https://archimedes-arc.com/api/health/amm
```

### GET /
Root — service identity + agent-discoverability pointers. | **Auth**:
anonymous | **Flags**: rate-limit exempt

Request: none.
Response: `{name: "Archimedes", tagline: str, docs: "/docs"|"disabled (production)", llms_txt: "/llms.txt", agent_manifest: "/api/agent/manifest", agent_docs: "see /llms.txt"}`. `docs` reports `"disabled (production)"` whenever `/docs` and `/openapi.json` are gated off — see [`README.md`](README.md) for the exact `PUBLIC_DOMAIN` / `ENABLE_API_DOCS` rule.
Errors: none.

```bash
curl -s https://archimedes-arc.com/
```
