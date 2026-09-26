# Archimedes — System Architecture Map

> **status:** current
> **owner:** Dan Browne
> **updated:** 2026-09-01
> **superseded-by:** —

> Identity and deploy topology amended for Better Auth account ownership (2026-08). (2026-07-14)

> **Amended 2026-08-31** for the 2026-08-30/31 merge train — the deltas are marked
> **(2026-08-31)** inline: the leaderboard's research/live-paper split (#1563), the
> ratified `num_trials` self-containment convention (#1560), the four-state `paper_rag`
> signal and what prod actually reports, and `deploy.yml`'s explicit rollout verdict
> (#1532/#1544). §9's disagreement list is re-scored in the same pass. Not re-audited this
> round: §§3–4 flows and the §1.7 contract census.

> Commissioned for the Architecture-page redesign. Every claim below is grounded in a file
> path in this repository. All paths are relative to the repository root unless noted. Facts from the 2026-07-14 merge train are marked **(PR #n, merged 2026-07-14)**; the only still-open PR is noted as such. Facts were
> verified open on 2026-07-14 (`gh pr view`). Where docs and code disagree, code wins and the
> disagreement is logged in §9.

---

## 0. One-screen summary

```
                     ┌──────────────────────────────────────────────────────────────┐
   USER / AGENT      │  React SPA (ui/) — Generate · Library · Portfolio · Reasoning │
   (Better Auth     │  Learnings · Explore · Leaderboard · Corpus · Marketplace     │
    account; optional└──────────────────────────┬───────────────────────────────────┘
    linked wallet)                              │ HTTPS · SSE · session cookie
                     ┌──────────────────────────▼───────────────────────────────────┐
   OFF-CHAIN         │  FastAPI (backend/archimedes/main.py)                        │
                     │   api/ routes ── agents/ debate society ── services/ rigor,  │
                     │   corpus RAG, DSL, regime ── marketplace/ x402 ── chain/     │
                     │   executor + runners (oracle · agent · kb)                   │
                     │  LLM: Bedrock Converse (Nova Micro default; BYOK; Ollama)    │
                     │  Stores: Aurora Postgres 18.3 · ElastiCache Redis · S3 · SSM │
                     └──────────────────────────┬───────────────────────────────────┘
                                                │ web3 (chain/client.py) · Circle DCW signer
                     ┌──────────────────────────▼───────────────────────────────────┐
   ON-CHAIN          │  Arc testnet (chain 5042002, USDC-as-gas) — 570 contracts    │
   (Arc testnet)     │  Vault/VaultFactory · ReasoningTraceRegistry (commit-reveal) │
                     │  StrategyRegistry · AMM Router/Pools · SyntheticFactory/     │
                     │  Tokens · per-synth PriceOracles · PaymentSplitter (90/10)   │
                     └──────────────────────────────────────────────────────────────┘
```

Product spine (canonical, [`docs/user-stories.md`](user-stories.md)): **generate → rigor-gate → (roadmap: execute → monitor) → explore**. Generate, rigor-gate, and explore are shipped. Vault execute/monitor is roadmap.

---

## 1. Layers and load-bearing components

### 1.1 UI layer — [`ui/`](../ui)

React 19 + Vite 8 + viem 2.5x, plain-CSS design system. Dark-first, light theme via
`data-theme` ([`ui/src/theme.js`](../ui/src/theme.js)); all tokens in `ui/src/App.css` (accent gold `#D4A853`,
canvas `#09090B`).

| Component | Path | Role |
|---|---|---|
| Router / page shell | [`ui/src/App.jsx`](../ui/src/App.jsx) | Path-based routes: landing, explore, leaderboard, corpus, architecture, generate, library, `strategy/:id`, portfolio, reasoning, learnings, marketplace, publish, subscriptions, insights, vault-detail |
| Sidebar nav | [`ui/src/navConfig.js`](../ui/src/navConfig.js) (data), [`ui/src/components/Layout.jsx`](../ui/src/components/Layout.jsx) (render) | **(2026-08-31, #1641)** Groups: Strategy (Explore/Corpus/Generate/Library), Position (Paper Trading/Reasoning/Leaderboard, plus flag-hidden Portfolio/Quant Lab/Learnings), Market (Marketplace/Publish/Subscriptions — flag-hidden), Ops (Insights/Account). Every entry is labelled — there is no ungrouped item. Architecture is **not** here: it is public-only (`/architecture`, #1370/#1400) |
| Generate surface | [`ui/src/components/Generate.jsx`](../ui/src/components/Generate.jsx), `GenerationStream.jsx`, `FusionResult.jsx`, `RejectedCandidates.jsx`, `RigorStrictnessControl.jsx`, `ModelCostPanel.jsx` | Brief input → SSE stream of debate progress → K=1 winner + considered-rejects + rigor verdict + model cost picker |
| Strategy passport | [`ui/src/components/StrategyPassport.jsx`](../ui/src/components/StrategyPassport.jsx) | Paper anchors, rigor verdict cards, backtest vs paper-claim deltas, trace verify |
| Vault deploy | [`ui/src/components/CreateVaultModal.jsx`](../ui/src/components/CreateVaultModal.jsx) (lines 155–195, 386–388), `DepositFlow.jsx` | **Client-side, user signs everything**: `createVault()` → `setAgent()` (2 sigs), then the 3-step approve → deposit → allocate flow |
| Portfolio / vaults | [`ui/src/components/Portfolio.jsx`](../ui/src/components/Portfolio.jsx), `VaultDetail.jsx` | On-chain reads via viem against `NEW_CONTRACTS.vaultFactory` |
| Trace viewer / verify | [`ui/src/components/Reasoning.jsx`](../ui/src/components/Reasoning.jsx) | Recompute keccak hash, check against `traceRegistry` on-chain |
| Marketplace | [`ui/src/components/MarketplacePage.jsx`](../ui/src/components/MarketplacePage.jsx), `PublishPage.jsx`, `SubscriptionsPage.jsx`, `StrategyDetailPage.jsx` | Publish / subscribe / earnings withdraw surfaces |
| Corpus | [`ui/src/components/CorpusExplorer.jsx`](../ui/src/components/CorpusExplorer.jsx), `CorpusGraph.jsx`, `CorpusKG.jsx` | Renders real KB artifacts; explicit empty state on 503 (pipeline not yet run) |
| Wallets | [`ui/src/components/WalletConnect.jsx`](../ui/src/components/WalletConnect.jsx), `WalletGate.jsx`, [`ui/src/circle-wallet.js`](../ui/src/circle-wallet.js) | EIP-6963 injected wallets + Circle passkey smart account |
| Chain config | [`ui/src/config.js`](../ui/src/config.js) (line 742) | Arc chain 5042002; `NEW_CONTRACTS` = the T3.2 2026-07-09 address set, converged with the backend env (the FE/BE split-brain is fixed by hand-sync; a runtime fetch from `/api/config/contracts` remains the durable fix). Full 281-synth universe comes from `GET /api/explore/assets`; only an 8-synth demo list is hardcoded |
| Architecture page | [`ui/src/components/Architecture.jsx`](../ui/src/components/Architecture.jsx) | **Rebuilt in PR #1192 (2026-07-28)** to the design in [`specs/architecture-page-design.md`](specs/architecture-page-design.md). §10 records the staleness that motivated the rebuild. |

### 1.2 API layer — [`backend/archimedes/api/`](../backend/archimedes/api) (FastAPI, wired in `backend/archimedes/main.py:468-492`)

| Router | Path | Role |
|---|---|---|
| Account auth | [`api/account_auth.py`](../backend/archimedes/api/account_auth.py), `auth/` | Better Auth sidecar owns email/password (+ optional OAuth) sessions; FastAPI resolves immutable canonical user IDs from cookies. Works headless — the agent-native auth path |
| Linked wallets | [`api/wallet_routes.py`](../backend/archimedes/api/wallet_routes.py) | Account-bound, single-use EIP-4361 proof (EOA; ERC-1271/6492 via [`api/_erc6492.py`](../backend/archimedes/api/_erc6492.py)); the wallet never creates a session |
| Generate | [`api/generate_routes.py`](../backend/archimedes/api/generate_routes.py) | SSE streaming generation jobs (Better Auth account-scoped; per-account + per-IP daily caps; linked wallet optional) |
| Vaults | [`api/vaults_routes.py`](../backend/archimedes/api/vaults_routes.py) (line ~275) | Agent-API deploy path: backend signer creates the vault, **transfers Ownable ownership to the user, pins the backend as rebalance-only agent**; vault metadata writes gated on the on-chain owner (line 376) |
| Rigor gate | [`api/selection_bias_routes.py`](../backend/archimedes/api/selection_bias_routes.py) | The external gate endpoint (`/api/selection-bias/gate/...`); strictness ladder |
| Marketplace | [`api/marketplace_routes.py`](../backend/archimedes/api/marketplace_routes.py) | `/api/marketplace/publish`, `/subscribe`, `/unsubscribe`, `/published`, `/my-published`, `/publish/{id}/withdraw`, `/my-subscriptions` |
| Corpus | [`api/corpus_routes.py`](../backend/archimedes/api/corpus_routes.py) | Honest 503 ("pipeline not yet run") until real KB artifacts exist — no metadata-synthesized graphs |
| Config | [`api/config_routes.py`](../backend/archimedes/api/config_routes.py) | `GET /api/config/contracts` — serves the contract addresses from the ECS task env ([`infra/ecs.tf`](../infra/ecs.tf)) |
| Agent manifest | [`api/agent_manifest_routes.py`](../backend/archimedes/api/agent_manifest_routes.py) | Agent-discoverability surface (`/api/agent/manifest`) per [`docs/agent-api.md`](agent-api.md) |
| Leaderboard | [`api/leaderboard_routes.py`](../backend/archimedes/api/leaderboard_routes.py) | **(2026-08-31, #1563)** Two boards, never mixed: `GET /api/leaderboard` is 100% backtest-era and stamps every row `performance_basis="backtest_research"`; `GET /api/leaderboard/live-paper` returns rows only for real deployments, and the "is this ledger real" test lives in exactly one place, `build_live_paper_leaderboard` |
| Others | `explore_routes.py` (281-synth universe), `portfolio_routes.py`, `traces_routes.py`, `regime_routes.py`, `proposals_routes.py` (owner-scoped), `user_routes.py`, `metrics_routes.py`, `risk_routes.py`, `swap_routes.py`, `papers_routes.py` | Product surfaces for the spine |
| Middleware | [`api/telemetry_middleware.py`](../backend/archimedes/api/telemetry_middleware.py), [`api/funnel_middleware.py`](../backend/archimedes/api/funnel_middleware.py), [`api/limiter.py`](../backend/archimedes/api/limiter.py), [`api/auth_guard.py`](../backend/archimedes/api/auth_guard.py) | Telemetry, visitor funnel, rate limits, auth |

`/health` + `/api/health` (`main.py:495-706`) expose honesty flags including `corpus_kg_built`
(`main.py:631,684`) and a `paper_rag` degraded signal (`/health/paper-rag`, `main.py:708`) —
the machine-readable claim-integrity surface the new page should read from.

### 1.3 Generation layer (the "debate society") — [`backend/archimedes/agents/`](../backend/archimedes/agents)

> Decision records: [`adr/debate-society-sole-generation-pipeline.md`](adr/debate-society-sole-generation-pipeline.md)
> (why there is one path and no fallback),
> [`adr/k1-generation-external-rigor-gate.md`](adr/k1-generation-external-rigor-gate.md) (K=1),
> [`adr/num-trials-self-containment.md`](adr/num-trials-self-containment.md) (what deflates a
> strategy's Sharpe).

| Component | Path | Role |
|---|---|---|
| Orchestrator | [`agents/generation_pipeline.py`](../backend/archimedes/agents/generation_pipeline.py) | SSE lifecycle: `job_queued → brief_validated → pipeline_selected("debate") → candidates_selected → candidate_drafted/evaluated → best_selected → trace_hashed → persisted → done`. Debate is the **sole** live pipeline (Phase 3, issue #834); no LLM or empty corpus → explicit `GENERATION_UNAVAILABLE`, never a silent fallback |
| Debate engine | [`agents/debate_engine.py`](../backend/archimedes/agents/debate_engine.py) | Proposer pool (fusion proposals across regime-biased evidence sets) → adversarial bull/bear round (never gates) → **C-rigor**: deterministic backtest of every survivor (`evaluate_fusion_spec`, 0 tokens) with `num_trials = _society_num_trials(pool_size)` — **(2026-08-31)** the strategy's *own* pool, not pool + library (decouple #2, ratified [ADR](adr/num-trials-self-containment.md), #1560) → **C-null**: must beat buy-and-hold net of cost or first-class **ABSTAIN** → K=1 winner + considered-rejects |
| Fusion proposer | [`agents/strategy_fusion.py`](../backend/archimedes/agents/strategy_fusion.py) | Multi-paper synthesis; `select_candidates` = the keyword/asset-class retrieval stage |
| Strategy Architect | `agents/strategy_architect.py` | **Removed (PR #1074, merged 2026-07-14)**; debate society is the sole generation path |
| num_trials self-containment | — | DSR multiple-testing count is self-contained per strategy (PR #1075, merged 2026-07-14; convention `self_contained_v2`) |
| Portfolio agent | [`agents/portfolio_agent.py`](../backend/archimedes/agents/portfolio_agent.py) | LLM portfolio advisor; disposition open (T1.1 residual) |

### 1.4 Services layer — [`backend/archimedes/services/`](../backend/archimedes/services) (the ~80-module core; load-bearing subset)

| Concern | Path | Role |
|---|---|---|
| LLM seam | [`services/llm_backend.py`](../backend/archimedes/services/llm_backend.py) | Multi-provider **Converse** backend: AWS Bedrock **Amazon Nova Micro** default; BYOK; local-Ollama single-user path. `response.model` is provenance-of-record |
| Corpus retrieval | [`services/paper_rag.py`](../backend/archimedes/services/paper_rag.py) | Stage-2 **query-time** rerank over the keyword-filtered candidates — nothing is read from a stored embedding index (none exists). `all-MiniLM-L6-v2` when the model loads in-process, lexical TF-IDF cosine otherwise. **(2026-08-31)** `paper_rag` has four states — `live` (a model object is loaded in *this* process) · `ready` (weights on disk, nothing has retrieved yet — deliberately **not** live, since presence on disk is not proof) · `degraded` (load attempted and failed) · `disabled`. Prod currently reports `ready`, and the two claims are published separately so neither can stand in for the other: `paper_rerank_model_live` is the process fact, `corpus_embedded_at_rest` the corpus one (false, derived from the schema — #1488, #778). 150-candidate cap, bounded embedding cache, `torch.set_num_threads(1)` (the #885 outage guardrails) |
| Corpus ingest | [`services/corpus_service.py`](../backend/archimedes/services/corpus_service.py), [`services/arxiv_corpus.py`](../backend/archimedes/services/arxiv_corpus.py), [`services/arxiv_pipeline.py`](../backend/archimedes/services/arxiv_pipeline.py) | JSONL manifest seed into Postgres (live count: `GET /health` `corpus_papers` / `corpus_db_count`) |
| KB pipeline | [`services/kb_runner.py`](../backend/archimedes/services/kb_runner.py), [`services/kb_artifacts.py`](../backend/archimedes/services/kb_artifacts.py) | Scheduled runner triggering SPECTER2 embeddings / HDBSCAN clusters / REBEL+SciSpacy KG builds (the "KB pipeline"); artifacts served by `corpus_routes` |
| Rigor gate | [`services/live_rigor_gate.py`](../backend/archimedes/services/live_rigor_gate.py) | **Single source of truth** for `passes_rigor_gate`: four-state pass/fail/pending/**degenerate**, computed live from persisted real returns — never a cached boolean (issue #821, the #1 rule) |
| Rigor math | [`services/rigor_evaluator.py`](../backend/archimedes/services/rigor_evaluator.py), [`services/_rigor_helpers.py`](../backend/archimedes/services/_rigor_helpers.py), [`services/rigor_profiles.py`](../backend/archimedes/services/rigor_profiles.py), [`services/rigor_cache.py`](../backend/archimedes/services/rigor_cache.py) | DSR, PBO, walk-forward OOS, look-ahead audit; strictness profiles |
| Strategy DSL | [`services/strategy_dsl.py`](../backend/archimedes/services/strategy_dsl.py), [`services/dsl_to_backtrader.py`](../backend/archimedes/services/dsl_to_backtrader.py), [`services/strategy_signal_evaluator.py`](../backend/archimedes/services/strategy_signal_evaluator.py) | The persisted, executable strategy spec; DSL → backtrader for evaluation; live signal evaluation for the rebalancer |
| Portfolio math | [`services/portfolio_constructor.py`](../backend/archimedes/services/portfolio_constructor.py), [`services/portfolio_optimizer.py`](../backend/archimedes/services/portfolio_optimizer.py), [`services/strategy_sizer.py`](../backend/archimedes/services/strategy_sizer.py) (Kelly), [`services/stress_engine.py`](../backend/archimedes/services/stress_engine.py) | Allocation, sizing, six-scenario stress |
| Regime | [`services/gmm_regime_detector.py`](../backend/archimedes/services/gmm_regime_detector.py) (+ `gmm_model.pkl`), [`services/vix_regime_detector.py`](../backend/archimedes/services/vix_regime_detector.py) | Market-context input to generation and rebalancing |
| Xia protocols | [`services/embargo_filter.py`](../backend/archimedes/services/embargo_filter.py) (Outcome Embargo), [`services/time_aware_retrieval.py`](../backend/archimedes/services/time_aware_retrieval.py), [`services/source_tracker.py`](../backend/archimedes/services/source_tracker.py), [`chain/v_check.py`](../backend/archimedes/chain/v_check.py) (V_check) | Enforced mechanisms, not guidelines ([`docs/specs/xia-2026-protocols.md`](specs/xia-2026-protocols.md)) |
| Exactly-once | [`services/runner_lease.py`](../backend/archimedes/services/runner_lease.py) | Redis lease guard — funds-adjacent runners are singletons |
| Monitoring | [`services/vault_monitor.py`](../backend/archimedes/services/vault_monitor.py), [`services/telemetry_store.py`](../backend/archimedes/services/telemetry_store.py) | Vault polling, telemetry. **No backtest refresh** — backtests are frozen evidence, produced once ([ADR](adr/backtests-are-frozen-evidence.md), #1760) |
| Secrets | [`services/secrets_service.py`](../backend/archimedes/services/secrets_service.py) | Pull-model: SSM Parameter Store SecureStrings at runtime |

### 1.5 Chain layer — [`backend/archimedes/chain/`](../backend/archimedes/chain)

| Component | Path | Role |
|---|---|---|
| RPC client | [`chain/client.py`](../backend/archimedes/chain/client.py) | web3 async client for Arc |
| Contract loader | [`chain/contracts.py`](../backend/archimedes/chain/contracts.py) | ABIs from [`contracts/abis/`](../contracts/abis) |
| Executor | [`chain/executor.py`](../backend/archimedes/chain/executor.py) | Reads portfolio state, executes rebalance trades, creates vaults. Signer: **Circle DCW preferred** ([`chain/circle_signer.py`](../backend/archimedes/chain/circle_signer.py)), raw `ARC_AGENT_PRIVATE_KEY` fallback |
| Trace publisher | [`chain/trace_publisher.py`](../backend/archimedes/chain/trace_publisher.py) | v1 `publishTrace` (legacy/SKIP path) + v1.5 **commit/reveal** temporal binding. Reveal is hash-only: empty `storagePointer`, no IPFS pin ([ADR](adr/ipfs-pinning-not-live.md)) |
| Agent runner | [`chain/agent_runner.py`](../backend/archimedes/chain/agent_runner.py) | The rebalance tick loop (default `AGENT_INTERVAL_SECONDS=300`); lease-gated exactly-once; per-vault strategy scoping; V_check gate (arithmetic weights-sum + max-concentration checks — [`chain/v_check.py`](../backend/archimedes/chain/v_check.py)); **commit → trade → reveal** ordering with a commit-guard (the `Phase 1: COMMIT` block onward). Generated-strategy vaults execute their persisted DSL spec (PR #1076, merged 2026-07-14) |
| Oracle runner | [`chain/oracle_runner.py`](../backend/archimedes/chain/oracle_runner.py), [`chain/oracle_updater.py`](../backend/archimedes/chain/oracle_updater.py) | Periodic price pushes to the per-synth `PriceOracle`s; lease-gated, fails closed |
| Marketplace publisher | [`chain/strategy_publisher.py`](../backend/archimedes/chain/strategy_publisher.py) | On-chain strategy registration (StrategyRegistry) |

### 1.6 Analytics engine — [`analytics-engine/`](../analytics-engine)

Separate uv-managed package; backtrader runner ([`analytics-engine/src/`](../analytics-engine/src)), **34 single-paper
strategies** in [`analytics-engine/strategies/`](../analytics-engine/strategies) (consolidation to ~6 honest multi-paper
strategies is a decided-but-not-executed plan — [`docs/audits/2026-07-09-curated-consolidation.md`](audits/2026-07-09-curated-consolidation.md)).
Backtesting engine choice: backtrader per [`docs/adr/backtrader-backtest-engine.md`](adr/backtrader-backtest-engine.md).

### 1.7 Contracts — [`contracts/src/`](../contracts/src) (Solidity + Foundry; deps as git submodules; `contracts-test.yml` CI)

Full hardened suite **redeployed 2026-07-09** on chain **5042002** (deployer
`0x03AaB3...4092`). The honest census, verifiable live at `GET /api/config/contracts`:
**12 Solidity sources** in [`contracts/src/`](../contracts/src) compile into **570 live protocol instances** —
**8 core singletons** (SyntheticFactory, AMMRouter, VaultFactory, AssetRegistry,
StrategyRegistry, PriceOracle, ReasoningTraceRegistry, PaymentSplitter) + **281 SyntheticToken
instances** (the tradable universe: crypto, equities, ETFs, FX) + **281 AMMPool instances**
(one USDC↔synth pool each, created on-chain by `AMMRouter.createPool`) — plus **user Vaults
minted on demand** via VaultFactory (0 on the fresh redeploy; grows with every deploy click).
USDC itself is Arc-native (`0x3600...0000`, Circle's, not ours). The earlier "289" headline
was core + synth tokens only; pools are factory-created children and belong in the count.
Addresses: [`infra/ecs.tf`](../infra/ecs.tf) env + [`ui/src/config.js`](../ui/src/config.js); SSOT endpoint above.

| Contract | Path | Role / trust property |
|---|---|---|
| Vault | [`contracts/src/Vault.sol`](../contracts/src/Vault.sol) | ERC-4626-style non-custodial container. `agent` has **rebalance-only** authority; `rebalance()` (line 416) enforces **commit-before-trade** (#589, line 422); token allowlist + oracle-floor slippage are `onlyOwner` (lines 521–531) so a compromised agent cannot leak value; `setAgent`/`pause`/fees all `onlyOwner` (614–655) |
| VaultFactory | [`contracts/src/VaultFactory.sol`](../contracts/src/VaultFactory.sol) | Vault deployment + enumeration (what the backend monitors) |
| ReasoningTraceRegistry | [`contracts/src/ReasoningTraceRegistry.sol`](../contracts/src/ReasoningTraceRegistry.sol) | v1 `publishTrace` + v1.5 `commit()`/`reveal()` with on-chain hash verification at reveal (spec: [`docs/specs/commit-reveal-trace-spec.md`](specs/commit-reveal-trace-spec.md)) |
| StrategyRegistry | [`contracts/src/StrategyRegistry.sol`](../contracts/src/StrategyRegistry.sol) | On-chain strategy registration for the marketplace |
| AssetRegistry | [`contracts/src/AssetRegistry.sol`](../contracts/src/AssetRegistry.sol) | Token allowlist source (coexists with StrategyRegistry by design) |
| PriceOracle | [`contracts/src/PriceOracle.sol`](../contracts/src/PriceOracle.sol) | Per-synth admin-push oracle (hardened per #724); fed by the Circle-signed oracle runner |
| AMMPool / AMMRouter | [`contracts/src/AMMPool.sol`](../contracts/src/AMMPool.sol), `AMMRouter.sol` | Swap venue for rebalances (oracle floor as slippage guard) |
| SyntheticFactory / SyntheticToken / SyntheticVault | `contracts/src/Synthetic*.sol` | The 281-synth universe machinery |
| PaymentSplitter | [`contracts/src/PaymentSplitter.sol`](../contracts/src/PaymentSplitter.sol) | **90/10 creator/platform** USDC split pools for marketplace payouts |

### 1.8 Data stores

> Decision record: [`adr/aurora-postgres-alembic-datastore.md`](adr/aurora-postgres-alembic-datastore.md)
> — Aurora Serverless v2 as the system of record, Alembic as the only schema-change
> mechanism, Redis as ephemeral state only.

| Store | Prod | Local | What lives there |
|---|---|---|---|
| Postgres | **Aurora PostgreSQL 18.3** ([`infra/aurora.tf`](../infra/aurora.tf)) | `postgres:18-alpine` ([`docker-compose.yml`](../docker-compose.yml), `localdb` profile) | Strategies + proposals ([`models/strategy_store.py`](../backend/archimedes/models/strategy_store.py), [`models/strategy_proposal.py`](../backend/archimedes/models/strategy_proposal.py)), backtests + daily returns ([`models/backtest_store.py`](../backend/archimedes/models/backtest_store.py), [`models/daily_returns_store.py`](../backend/archimedes/models/daily_returns_store.py)), traces ([`models/trace.py`](../backend/archimedes/models/trace.py)), corpus ([`models/corpus_store.py`](../backend/archimedes/models/corpus_store.py), [`models/kg.py`](../backend/archimedes/models/kg.py)), vaults, users/identity, marketplace |
| Redis | **ElastiCache** (TLS-required, [`infra/elasticache.tf`](../infra/elasticache.tf)) | `redis` service | SSE job event logs ([`services/redis_state.py`](../backend/archimedes/services/redis_state.py)), regime state, **runner leases** ([`services/runner_lease.py`](../backend/archimedes/services/runner_lease.py)), marketplace event log ([`marketplace/state.py`](../backend/archimedes/marketplace/state.py)) |
| S3 | KB artifacts ([`services/kb_artifacts.py`](../backend/archimedes/services/kb_artifacts.py)); backtest-results archive ([`scripts/archive_backtest_results.py`](../backend/archimedes/scripts/archive_backtest_results.py)) | — | Backtest/KB artifacts |
| KB artifact volume | named volume `archimedes-corpus-artifact`; **EFS (IaC merged, PR #1071; terraform apply pending — #1065)** | same volume | SPECTER2/HDBSCAN/REBEL artifacts |
| SSM Parameter Store | `/archimedes/prod/*` SecureStrings ([`infra/scripts/setup-ssm-secrets.sh`](../infra/scripts/setup-ssm-secrets.sh)) | `.env` | LLM keys, Circle creds, DB/Redis URLs — pull-model, nothing injected by CI |

### 1.9 Infra + CI/CD — [`infra/`](../infra), `.github/workflows/`

> Decision records: [`adr/ec2-to-ecs-fargate-cutover.md`](adr/ec2-to-ecs-fargate-cutover.md)
> (why Fargate, and what the cutover cost) and
> [`adr/build-on-deploy-main-only.md`](adr/build-on-deploy-main-only.md) (why every merge to
> `main` deploys).

See §7 for topology. Terraform: `vpc.tf`, `alb.tf`, `waf.tf`, `cloudfront.tf`, `aurora.tf`,
`elasticache.tf`, `ecr.tf`, `ecs.tf` (Fargate task: nginx :8080 + backend :8000 on one ENI),
`ecs_migrate.tf` (pre-rollout Alembic), `cloudwatch.tf`, legacy `asg.tf`/`ec2_iam.tf`.
CI: `deploy.yml` (build-in-CI → ECR → migrate → Fargate force-redeploy; OIDC, no long-lived
keys — **(2026-08-31, #1532/#1544)** it now polls the service's own `rolloutState` to an
explicit verdict against a named budget so "slow" and "broken" stop producing the same red,
probes live `/api/health` through CloudFront to assert the app *answers*, and keys the
CloudFront invalidation to that verdict), `deploy-runners.yml` (SSM → the runner EC2 +
kb-runner task-def), `docs-gate.yml`, `quality-gate.yml`, `contracts-test.yml` (forge),
`complexity-gate.yml`, `import-guard.yml`, `main-format-guard.yml`, `release-tag.yml`.
Runbook: [`infra/runbooks/ecs-fargate-cutover.md`](../infra/runbooks/ecs-fargate-cutover.md).

---

## 2. Flow — Generate (brief → rigor-gated strategy)

1. **Auth**: Better Auth account session (email/password or OAuth) → canonical user id; a wallet may be LINKED later via a single-use EIP-4361 proof ([`api/wallet_routes.py`](../backend/archimedes/api/wallet_routes.py)) but never logs in. Passkey users get a Circle smart account ([`ui/src/circle-wallet.js`](../ui/src/circle-wallet.js)); headless agents use the same endpoints ([`docs/agent-api.md`](agent-api.md), [`scripts/agent_journey.py`](../scripts/agent_journey.py)).
2. **Brief** → `POST /api/generate` ([`api/generate_routes.py`](../backend/archimedes/api/generate_routes.py)) → SSE job ([`agents/generation_pipeline.py`](../backend/archimedes/agents/generation_pipeline.py)), events persisted per-job in Redis.
3. **Retrieval**: keyword/asset-class pre-filter (`agents/strategy_fusion.py::select_candidates`) → query-time cosine rerank ([`services/paper_rag.py`](../backend/archimedes/services/paper_rag.py); MiniLM when the model loads, lexical TF-IDF in prod today) → top-N papers, embargo-filtered ([`services/embargo_filter.py`](../backend/archimedes/services/embargo_filter.py)) and age-decayed ([`services/time_aware_retrieval.py`](../backend/archimedes/services/time_aware_retrieval.py)). **Precheck: ≥2 corpus papers or `GENERATION_UNAVAILABLE`** — the absence of stored embeddings and of any KB artifact is the honest gap (issue #778).
4. **Debate society** ([`agents/debate_engine.py`](../backend/archimedes/agents/debate_engine.py)): proposer pool fans LLM fusion calls (model = user's cost-picker choice via [`services/llm_backend.py`](../backend/archimedes/services/llm_backend.py)) across regime-biased evidence sets → dedup by canonical spec hash → adversarial bull/bear transcript (surface, non-gating) → **C-rigor** deterministic backtest of every survivor → **C-null** vs buy-and-hold → **K=1 winner + considered-rejects**, or first-class ABSTAIN.
5. **Persist + hash**: winner and rejects content-hashed into `strategy_proposals` ([`models/strategy_proposal.py`](../backend/archimedes/models/strategy_proposal.py)); trace hashed; strategy record created ([`models/strategy_store.py`](../backend/archimedes/models/strategy_store.py)).
6. **External rigor gate**: the badge the user sees is computed live from persisted real returns ([`services/live_rigor_gate.py`](../backend/archimedes/services/live_rigor_gate.py) — four-state pass/fail/pending/degenerate) and served by [`api/selection_bias_routes.py`](../backend/archimedes/api/selection_bias_routes.py); the Deploy button gates on it. The gate runs **outside** the generator (architectural primitive 5, `CLAUDE.md:1015`).

## 3. Flow — Deploy + Rebalance (with commit-reveal)

**Deploy — two paths, both ending owner = user, agent = rebalance-only:**
- *UI path* ([`ui/src/components/CreateVaultModal.jsx`](../ui/src/components/CreateVaultModal.jsx)): user signs `createVault()` → `setAgent()` (2 wallet sigs, "You sign everything"), then the 3-step **approve → deposit → allocate** flow (`DepositFlow.jsx`) — 5 signatures total.
- *Agent-API path* (`api/vaults_routes.py:275`): backend signer creates the vault, **transfers Ownable ownership to the user**, pins the backend as agent — the headless-agent deploy.

**Rebalance loop** ([`chain/agent_runner.py`](../backend/archimedes/chain/agent_runner.py), default 300 s tick, Redis-lease singleton):
1. Read vault state **from chain** (ground truth — hierarchy of truth; the LLM narrative never overrides it).
2. Evaluate the strategy's signal rule / persisted DSL spec ([`services/strategy_signal_evaluator.py`](../backend/archimedes/services/strategy_signal_evaluator.py); generated-strategy DSL execution (PR #1076, merged 2026-07-14)) → target weights; drift + cost-benefit check.
3. **V_check** ([`chain/v_check.py`](../backend/archimedes/chain/v_check.py)) — deterministic arithmetic checks on the target weights dict (sum to 10000 BPS, max single-position concentration, and — only when a caller supplies `cost_benefit_bps`, which none do today — a minimum cost-benefit floor); any failing check aborts the rebalance.
4. Build the canonical trace and **commit** its keccak hash + trade-intent digest to `ReasoningTraceRegistry` ([`chain/trace_publisher.py`](../backend/archimedes/chain/trace_publisher.py)); the trade arrays used for the commit are reused verbatim for execution.
5. `Vault.rebalance(trades)` — **the contract itself reverts if no matching commitment exists** (#589, `contracts/src/Vault.sol:422`); swaps route through `AMMRouter` over per-synth pools with the oracle price floor bounding slippage.
6. **Reveal**: persist the canonical trace JSON off-chain and call `reveal(traceId, storagePointer="", content)` — the contract recomputes and verifies the hash. `storagePointer` is empty: we do not pin traces to IPFS ([ADR](adr/ipfs-pinning-not-live.md)). Commit block < trade block < reveal block is user-verifiable ([`ui/src/components/Reasoning.jsx`](../ui/src/components/Reasoning.jsx)).
7. Honest "hold" decisions are also traced. Runner liveness (whether the loop above is actually ticking on the deployed runner, not just defined in code) is surfaced live, not asserted in this doc — see [`/api/agent/status`](../backend/archimedes/api/agent_routes.py) (`alive`, from the Redis heartbeat) and § 7's deploy topology for where the runner lives today.

## 4. Flow — Marketplace (x402 nanopayments)

1. **Publish** (`POST /api/marketplace/publish`, `api/marketplace_routes.py:43`): strategy registered on-chain ([`chain/strategy_publisher.py`](../backend/archimedes/chain/strategy_publisher.py) → `StrategyRegistry`), a 90/10 `PaymentSplitter` pool created for the creator, publisher loop started ([`marketplace/service.py`](../backend/archimedes/marketplace/service.py) — in-process monolith, no per-agent containers).
2. **Subscribe** (`POST /api/marketplace/subscribe`): a Circle Developer-Controlled Wallet is auto-provisioned for the subscriber ([`marketplace/wallet_provisioner.py`](../backend/archimedes/marketplace/wallet_provisioner.py)); per-user spend caps on the subscribe path (PR #1099 — still a draft, the one unlanded piece).
3. **Charge per action** ([`marketplace/payments.py`](../backend/archimedes/marketplace/payments.py) — the only circlekit import): x402 flow = 402 payment-required → EIP-712 payment header signed with the subscriber's ephemeral key → Circle **Gateway facilitator** verifies + records the micropayment (sub-cent USDC).
4. **Settlement sweep** ([`marketplace/settlement.py`](../backend/archimedes/marketplace/settlement.py)): Stage A Gateway → agent wallet (threshold), Stage B wallet → `PaymentSplitter.depositToPool`, Stage C creator `withdraw` (the Withdraw button).
5. **Honesty state**: `PAYMENTS_DRY_RUN=true` in prod — the whole flow runs end-to-end **simulated**; real settlement is gated until fee custody migrates to non-custodial (issue #975). Vault principal is never touched by this flow; fee custody is **custodial-INTERIM** by explicit decision (merged #958).

## 5. Flow — Corpus (papers → retrievable knowledge)

1. **Seed**: committed JSONL manifest → Postgres (`services/corpus_service.py::seed_from_manifest`, boot-time in `main.py:139`). Live counts: `GET /health` `corpus_papers` / `corpus_db_count` — do not freeze a number here; the corpus probe can timeout.
2. **Seed writes text, and stops there**: metadata + abstracts land in Postgres; the `papers` schema carries no vector column, so there is nothing stored for a query to look up. Ranking is computed per request instead (§2.3). The ingest-time vectorisation described in [`docs/corpus-architecture.md`](corpus-architecture.md) is the target design, not the deployed one.
3. **KB pipeline** ([`services/kb_runner.py`](../backend/archimedes/services/kb_runner.py), own compose service / scheduled-Fargate target (PR #1071, merged; apply pending)): SPECTER2 embeddings, HDBSCAN clusters, REBEL+SciSpacy knowledge graph → artifacts → [`api/corpus_routes.py`](../backend/archimedes/api/corpus_routes.py). With no artifact, `/graph` raises **503 `kb_artifact_not_found`** and `/kg/*` returns **empty entity/relation sets** — neither synthesises from arXiv metadata (#201). `corpus_kg_built` flag in `/health`; both behaviours pinned by `backend/tests/test_corpus_claim_integrity.py`.
4. **Retrieval at generate time**: keyword filter → query-time rerank (§2.3).
5. **Honest state**: the `papers` table holds metadata + abstracts (live row count is `GET /health` `corpus_db_count`, not a number frozen in this file). There are **no embeddings** (no embedding column anywhere in the schema), `corpus_meta` = 0 rows, and `kg_entities`/`kg_relations` = 0/0 — so retrieval is **lexical**, and the KG/graph endpoints 503. What is missing is the artifact layer, not the papers (issue #778). Build decision: **HYBRID** — custom KB spine (Postgres + MiniLM) + optional Bedrock-KB retrieval bridge; Neptune ruled out; a MiniLM-only no-AWS local option exists.

## 6. Flow — Identity / accounts + linked wallets

1. Sign-up/sign-in against the Better Auth sidecar → session cookie → FastAPI resolves the canonical user id ([`api/account_auth.py`](../backend/archimedes/api/account_auth.py)). Wallet LINKING (optional, needed only for on-chain actions): `wallet_routes.py` challenge → wallet signs the single-use EIP-4361 message → verified (EOA; ERC-1271/6492) → wallet bound to the account. The wallet never creates a session.
2. Wallet-scoped authorization: proposals are owner-scoped ([`api/proposals_routes.py`](../backend/archimedes/api/proposals_routes.py), `owner_wallet` column); vault metadata writes verify the **on-chain Ownable owner** and fail closed on read failure (`api/vaults_routes.py:374-379`).
3. Same path for humans (MetaMask/Coinbase/passkey via EIP-6963 + Circle passkey) and headless agents ([`docs/agent-api.md`](agent-api.md), [`scripts/agent_journey.py`](../scripts/agent_journey.py), [`api/agent_manifest_routes.py`](../backend/archimedes/api/agent_manifest_routes.py)) — the agent-native thesis: one identity model, one API surface, no human-only path.

---

## 7. Deploy topology (live picture, 2026-07-14)

```
GitHub main ──deploy.yml (OIDC)──▶ ECR (backend + nginx images)
     │                                   │
     │ ecs_migrate task (alembic) ◀──────┤ force-redeploy
     ▼                                   ▼
CloudFront ──▶ WAF + ALB ──▶ ECS Fargate task [nginx:8080 → backend:8000, one ENI]
                                   │
                     ┌─────────────┼──────────────┐
                     ▼             ▼              ▼
              Aurora PG 18.3   ElastiCache    SSM Parameter Store
                               Redis (TLS)    (pull-model secrets)

Runners relocated (#1043, #1065; verified live 2026-08-18): oracle_runner + agent_runner
run on a dedicated `archimedes-runner` t3.small EC2 (stateful, exactly-once, Redis-lease
singleton — holds the scheduler lease); kb_runner runs as a scheduled ECS Fargate task
(`infra/kb_runner.tf`). Deploy path: `.github/workflows/deploy-runners.yml` (SSM
RunCommand pulls the fresh image + restarts the EC2 pair's systemd units; registers a new
`archimedes-kb-runner` task-definition revision for kb).
```

- Web tier cut over to Fargate 2026-07-09 (#1056–#1059) — decision and consequences in [`adr/ec2-to-ecs-fargate-cutover.md`](adr/ec2-to-ecs-fargate-cutover.md); the old web-tier EC2 box was stopped and snapshotted 2026-08-19 (`snap-02edf9e4a9ac7f201`) and its terraform removed (PR #1265, merged 2026-08-19) — Phase-8 decommission complete.
- Contract-address SSOT: Foundry broadcast → [`infra/ecs.tf`](../infra/ecs.tf) env (merged PR #1079) → `GET /api/config/contracts`; [`ui/src/config.js`](../ui/src/config.js) carries the matching hand-synced set (runtime fetch is the durable fix, not yet landed).
- Local dev parity: one [`docker-compose.yml`](../docker-compose.yml) with `localdb` + `runners` profiles mirrors prod.
- LLM: Bedrock in-region; BYOK and Ollama keep the single-user/local path AWS-optional.

## 8. Trust boundaries (what the page must state precisely)

| Boundary | Guarantee | Enforced at |
|---|---|---|
| **Non-custodial vaults** | Platform never holds vault principal. Vault owner = user (both deploy paths); agent authority = rebalance only; withdraw always user-signed | `contracts/src/Vault.sol:21,216,257`; `CreateVaultModal.jsx`; `vaults_routes.py:275` |
| **Agent cannot** | withdraw to platform, change the token allowlist or oracle floors, replace itself, pause, change fees, exceed slippage bounds | `Vault.sol:521-531` (onlyOwner allowlist rationale), `614-655` |
| **Agent can** | rebalance within the signed allocation universe, and only after an on-chain trace commitment exists | `Vault.sol:416-422` (#589 commit-before-trade) |
| **Provenance** | commit block < trade block < reveal block; reveal hash verified **on-chain**; anyone can recompute | `ReasoningTraceRegistry.sol`; [`chain/trace_publisher.py`](../backend/archimedes/chain/trace_publisher.py); `Reasoning.jsx` |
| **Oracle signer** | Circle DCW (API key + entity secret + WALLET_ID trio) pushes prices; role granted on-chain by the deployer; lease-gated, fails closed | [`chain/circle_signer.py`](../backend/archimedes/chain/circle_signer.py), [`chain/oracle_runner.py`](../backend/archimedes/chain/oracle_runner.py); secrets per [`docs/runbooks/t3.2-contract-redeploy.md`](runbooks/t3.2-contract-redeploy.md) |
| **Agent signer** | Circle DCW preferred; raw `ARC_AGENT_PRIVATE_KEY` fallback | [`chain/executor.py`](../backend/archimedes/chain/executor.py) |
| **Internal-agent key** | `INTERNAL_AGENT_API_KEY` shared secret for runner→backend internal endpoints | [`chain/agent_runner.py`](../backend/archimedes/chain/agent_runner.py) env; ecs secrets |
| **Hierarchy of truth** | Chain state outranks LLM narrative — the rebalance loop reads vault holdings from chain, never from LLM output; V_check separately caps any single position at 60% concentration and rejects a malformed weight set before the trade is committed | [`chain/v_check.py`](../backend/archimedes/chain/v_check.py), [`chain/agent_runner.py`](../backend/archimedes/chain/agent_runner.py) `read_portfolio` call |
| **Marketplace custody** | Subscription-fee custody is **custodial-INTERIM** (Circle Gateway wallet) while vault principal stays non-custodial; migration tracked #975; `PAYMENTS_DRY_RUN=true` until then | [`marketplace/settlement.py`](../backend/archimedes/marketplace/settlement.py), decision record #958 |
| **Exactly-once runners** | Funds-adjacent runners are Redis-lease singletons; every on-chain write gated on the live lease | [`services/runner_lease.py`](../backend/archimedes/services/runner_lease.py), runner docstrings |

## 9. Doc-vs-code disagreements found (flag for cleanup)

1. **[`CLAUDE.md`](../CLAUDE.md) § Tech Stack / Deployment** — ~~said the EC2/docker-compose stack "remains the accurate live picture"~~ **fixed 2026-07-14** (same branch as this map): Fargate/ALB/Aurora/ElastiCache is the live picture; the web-tier EC2 box itself was stopped, snapshotted, and its terraform removed 2026-08-19 (Phase-8 decommission complete, §7 above) — "11 contracts deployed" corrected to 12 sources / 570 live instances (T3.2 census above).
2. **[`docs/architectural-principles.md`](architectural-principles.md)** — "three top-level agents" mermaid + `services/portfolio_agent.py` path. Generation is debate-only now ([`agents/generation_pipeline.py`](../backend/archimedes/agents/generation_pipeline.py)); the file actually lives at [`agents/portfolio_agent.py`](../backend/archimedes/agents/portfolio_agent.py). The three-agent framing survived only as UI copy on the pre-#1192 Architecture page, and went away when that page was rebuilt (PR #1192, 2026-07-28).
3. **[`docs/specs/commit-reveal-trace-spec.md`](specs/commit-reveal-trace-spec.md)** — cites `backend/archimedes/services/trace_publisher.py`; actual: [`backend/archimedes/chain/trace_publisher.py`](../backend/archimedes/chain/trace_publisher.py). Spec status says "proposal / v1.5 hop"; commit-reveal is implemented and contract-enforced (`Vault.sol:422`).
4. **[`docs/user-stories.md`](user-stories.md)** — ~~"GLM-backed" MVP framing; live LLM is Bedrock/Nova Micro via the Converse seam~~ **fixed 2026-08-31** (same branch as this amendment), along with the stale "3-input fusion preview" surface, the "5 reference strategies (2 Tier-1)" count, and the on-the-fly graph/KG demo claim. Each correction is an inline dated note rather than a silent rewrite. Still true: the doc predates the marketplace/leaderboard surfaces, and it narrates vault execution in the present tense — flagged in its own front matter and in `CLAUDE.md` § Project as roadmap (#1469).
5. **`docs/design.md` §6** — vectorbt; superseded by backtrader (noted in [`CLAUDE.md`](../CLAUDE.md) itself, kept for history).
6. **[`docs/specs/ecosystem-design-spec.md`](specs/ecosystem-design-spec.md)** — `StrategyRegistry → AssetRegistry` replacement; in code both coexist intentionally (noted in `CLAUDE.md:404-406`).
7. **`.env.example`** — `LLM_PROVIDER=anthropic_compatible` default vs live `bedrock_converse` (tracked as roadmap T3.10).
8. **[`agents/strategy_fusion.py`](../backend/archimedes/agents/strategy_fusion.py) module docstring** — still describes fusion as "feature-flagged beside strategy_architect, default OFF" and GLM-backed; fusion proposals are now the heart of the sole (debate) pipeline and the architect was deleted (PR #1074, merged 2026-07-14). Docstring predates the pivot.
9. **[`docs/corpus-architecture.md`](corpus-architecture.md)** — Day-9 fusion-path framing, and it describes embeddings/clusters/KG as if built; retrieval reality is keyword → query-time rerank ([`services/paper_rag.py`](../backend/archimedes/services/paper_rag.py), lexical in prod) with the KB pipeline as an artifact layer that has not yet run (#778).
10. **[`ui/src/components/Architecture.jsx`](../ui/src/components/Architecture.jsx)** — the page being replaced; full staleness list in §10 and [`docs/handovers/2026-07-14-architecture-review.md`](handovers/2026-07-14-architecture-review.md).
11. **[`CLAUDE.md`](../CLAUDE.md) § Project** — ~~"executes and monitors them in non-custodial vaults on Arc with USDC settlement", present tense~~ **fixed 2026-08-31** (same branch): the contracts are deployed, but the deploy-a-vault journey is gated off every public surface by `ROADMAP_SURFACES_ENABLED` (off by default, #1266/#1354) and the blurb now says roadmap. The remaining public-copy scrub is #1469; the ADR carries a dated amendment saying the same thing ([`adr/non-custodial-vault-owner-agent.md`](adr/non-custodial-vault-owner-agent.md)).
12. **`.github/workflows/deploy.yml`** — ~~a top-level `EC2_INSTANCE_ID: i-01803d3abc271d39b` naming the single-box host decommissioned 2026-08-19~~ **fixed 2026-08-31** (same branch): no job had read it since the #1039 fast-follow retired the SSM `deploy` job, so it was dead env pointing at a dead box. The runners that *do* live on EC2 resolve their instance by name tag at runtime (`deploy-runners.yml`, [`infra/runner_ec2.tf`](../infra/runner_ec2.tf)) and never hardcode an id.

## 10. What the pre-#1192 Architecture page got wrong (headline items)

> **Updated 2026-07-28.** The page was rebuilt in **PR #1192** to the design in
> [`specs/architecture-page-design.md`](specs/architecture-page-design.md). The items below
> are the record of what motivated that rebuild — they are no longer live defects. #1123
> merged documentation only and shipped zero UI code; #1192 is the PR that built the page.

Itemized with evidence in [`docs/handovers/2026-07-14-architecture-review.md`](handovers/2026-07-14-architecture-review.md) §"What's stale". Headlines: the "3 top-level agents"
model (now: debate society + external rigor gate), "10 smart contracts" (now 12 Solidity
sources compiling into 570 live protocol instances on the fresh chain-5042002
deploy, per §1.7 and `GET /api/config/contracts`), "keyword/TF-IDF today"
(see the note below — that entry was over-corrected), "60s tick" (default 300 s), "4 wallet signatures" (2+3 client-signed
steps), publish-after trace anchoring (now commit-before-trade, contract-enforced), no
marketplace/x402, no SIWE/agent-native story, no leaderboard, and stat-card numbers hardcoded
in JSX where live endpoints exist (`/health`, `/api/config/contracts`, `/api/explore/assets`).

One item on that staleness list was corrected in the *wrong direction*. The "keyword/TF-IDF
today" entry was rewritten to present MiniLM reranking as a standing property of the corpus,
on the strength of `/health`'s `corpus_embedded` field. That field described the process, not
the corpus: it was `paper_rag == "live"`, i.e. whether sentence-transformers loaded in *this*
worker. The stored corpus is text either way, and prod serves the lexical path. #778 carries
the correction; `backend/tests/test_corpus_claim_integrity.py` pins it.

The field itself was the remaining hazard and is gone (#1488). `/health` now publishes
`paper_rerank_model_live` for the process-local fact and `corpus_embedded_at_rest` for the
corpus one, the latter derived from the ORM schema rather than declared, alongside
`rerank_candidate_cap` — the number of keyword candidates that actually reach the model.
`backend/tests/test_corpus_embedding_claims.py` fails if any `/health` field would let a
reader infer stored vectors from its name alone, and fails again if a stored-vector column
appears without the field being rewired to count it.

## 11. Stack at a glance

Relocated from `README.md` (2026-08-20) and corrected against the live system. Anything
that decays fast — contract counts, test totals — is deliberately a pointer, not a number.

| Layer | Technology |
| --- | --- |
| Backend | Python 3.12 · FastAPI · Uvicorn · SQLAlchemy |
| Frontend | React 19 + Vite 8 + UnoCSS + [viem](https://viem.sh/) |
| Datastore | Aurora PostgreSQL 18.3 + ElastiCache Redis (local dev: Postgres 16 + Redis 7 in compose) |
| LLM | `bedrock_converse` / `amazon.nova-micro-v1:0` ([ADR](adr/glm-to-bedrock-llm-migration.md)); BYOK and local-Ollama paths preserved behind the `LLM_*` env seam. Live value: `GET /health` → `llm_provider`, `llm_model`. |
| Backtesting | [backtrader](https://github.com/mementum/backtrader) ([ADR](adr/backtrader-backtest-engine.md)) |
| Generation | Multi-agent debate society — regime × mechanism steer grid, deterministic critics, K=1 ([spec](specs/multi-agent-debate-spec.md), [ADR](adr/debate-society-sole-generation-pipeline.md)) |
| Corpus retrieval | Keyword filter selects candidates; only that set is re-scored at request time across title + abstract — `all-MiniLM-L6-v2` when the model is loaded in-process, lexical TF-IDF when it is not. Nothing is precomputed (no vector column, no prebuilt index). Live scorer: `GET /health` → `paper_rag`, `paper_rag_reason`. Detail: [`corpus-architecture.md`](corpus-architecture.md). |
| Smart contracts | Solidity targeting Arc (EVM-compatible) + [Foundry](https://book.getfoundry.sh/). Census: `GET /api/config/contracts`. |
| On-chain | Circle SDK (Wallets, Gateway, CCTP) + viem on the UI side |
| Auth | Better Auth accounts/sessions; EIP-4361 only for optional wallet linking ([`security/auth-model.md`](security/auth-model.md)) |
| Deployment | ECS Fargate behind ALB/WAF (build-in-CI → ECR → Fargate) ([ADR](adr/ec2-to-ecs-fargate-cutover.md)); docker compose is the local dev mirror |
