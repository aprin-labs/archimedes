# Archimedes — Repository Map (2026-07-14)

Top-level layout + the ~25 most load-bearing files. Same content as `file-tree.svg`.

```
archimedes/                              the public repo — github.com/aprin-labs/archimedes
├─ backend/archimedes/                   the Python monolith: API + agents + services + chain + marketplace
│  ├─ main.py                            app boot: routers, /health honesty flags, corpus seed
│  ├─ api/                               FastAPI route layer
│  │  ├─ auth_siwe.py                    EIP-4361 wallet auth → session cookie (humans and agents)
│  │  ├─ generate_routes.py              SSE strategy-generation jobs
│  │  ├─ vaults_routes.py                vault create/list — ownership always ends with the user
│  │  ├─ selection_bias_routes.py        the external rigor-gate API
│  │  ├─ marketplace_routes.py           publish · subscribe · earnings withdraw
│  │  └─ corpus_routes.py                real KB artifacts only — honest 503 until they exist
│  ├─ agents/                            generation
│  │  ├─ generation_pipeline.py          SSE orchestrator — the debate society is the sole path
│  │  ├─ debate_engine.py                proposers → critics → deterministic rigor → K=1 + rejects
│  │  └─ strategy_fusion.py              multi-paper fusion proposer + candidate retrieval
│  ├─ services/                          ~80 modules — the engine room; five that matter most:
│  │  ├─ llm_backend.py                  Bedrock Converse seam — Nova Micro · BYOK · local Ollama
│  │  ├─ paper_rag.py                    MiniLM semantic rerank (guardrails + TF-IDF fallback)
│  │  ├─ live_rigor_gate.py              four-state pass / fail / pending / degenerate badge — live, never cached
│  │  ├─ strategy_dsl.py                 the persisted, executable strategy spec
│  │  └─ runner_lease.py                 exactly-once Redis leases for funds-adjacent runners
│  ├─ chain/                             on-chain integration
│  │  ├─ executor.py                     rebalance execution — Circle DCW signer preferred
│  │  ├─ agent_runner.py                 the vault tick loop: commit → trade → reveal
│  │  ├─ oracle_runner.py                per-synth price pushes — lease-gated, fails closed
│  │  └─ trace_publisher.py              ReasoningTraceRegistry commit/reveal client
│  └─ marketplace/                       x402 nanopayment rail
│     ├─ payments.py                     402 → EIP-712 payment header → Circle Gateway settle
│     └─ settlement.py                   Gateway → wallet → PaymentSplitter sweep (dry-run guard)
├─ contracts/src/                        Solidity (Foundry) · 13 sources · ABIs cached in contracts/abis/
│  ├─ Vault.sol                          non-custodial ERC-4626 — commit-before-trade enforced on-chain
│  ├─ ReasoningTraceRegistry.sol         the commit/reveal provenance anchor
│  └─ PaymentSplitter.sol                90/10 creator/platform USDC split
├─ ui/src/                               React 19 + Vite + viem SPA (plain-CSS design system)
│  ├─ App.jsx · Layout.jsx               routes + sidebar: Generate · Library · Portfolio · Marketplace …
│  ├─ components/Generate.jsx            the primary action: brief → debate stream → passport
│  ├─ components/CreateVaultModal.jsx    client-signed deploy: createVault → setAgent (you sign everything)
│  ├─ components/Reasoning.jsx           trace viewer — recompute hash vs the on-chain anchor
│  └─ config.js                          chain 5042002 + the T3.2 contract-address set (2026-07-09)
├─ analytics-engine/                     backtrader backtest runner (own uv-managed env + tests)
│  └─ strategies/                        34 paper-grounded reference strategies
├─ infra/                                Terraform: ALB/WAF · Aurora · ElastiCache · ECR · ECS Fargate
│  ├─ ecs.tf                             the Fargate web tier + contract-address env (deploy SSOT)
│  └─ runbooks/                          ecs-fargate-cutover.md — the executed cutover playbook
├─ .github/workflows/                    deploy.yml: build-in-CI → ECR → migrate → Fargate redeploy
├─ docker-compose.yml                    local parity stack (localdb + runners profiles)
└─ docs/                                 living specs — user-stories.md is the canonical product spine
```

Submodules (reference only, not product code): `submodules/context-arc/` (Circle/Arc developer
docs — the canonical Arc integration reference), `submodules/KnowledgeBase/` (Dan's paper-analysis
pipeline — the KB-pipeline reference implementation), `submodules/Linus/` (orchestration priors).

The full component-by-component map with data flows and trust boundaries: `docs/architecture.md`.
