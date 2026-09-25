# Doc register

> **status:** current
> **owner:** Dan Browne
> **updated:** 2026-09-02
> **superseded-by:** —

Every document in `docs/`, with its status, its owner and the date someone last
checked it against the running system. This is the *register*, not the front door —
the front door is [Archimedes documentation](index.md).

**A doc not listed here does not exist.** If you wrote something and it is not in this table, either add a row or delete the file. If a row is wrong, fix the row in the same commit as the doc.

`last-verified` is the date someone last checked the doc against the running system — not the date it was last edited. `—` means nobody has verified it since it was written; treat those claims as unproven.

Everything under [`archive/`](archive/) is historical by definition and is indexed separately in [`archive/README.md`](archive/README.md). Archived docs carry an `ARCHIVED` banner naming their replacement.

| Archived doc | Status | Owner | Archived | What it is |
|---|---|---|---|---|
| [`archive/deployment-runbook.md`](archive/deployment-runbook.md) | archived | Dan Browne | 2026-07-28 | EC2-era manual / break-glass AWS deploy runbook. **Do not execute** — it routes to an instance detached from the ALB target group. Kept for its incident history and diagrams. The Fargate-era replacement is unwritten; the gap is named in [`runbooks/README.md`](runbooks/README.md). |

`archive/` is **not published to the docs site** (`exclude_docs` in `mkdocs.yml`); the
link above resolves on GitHub, and on the site it is rewritten to the GitHub tree by
`.github/scripts/mkdocs_hooks.py`. The rest of this register links pages that are on
the site unless their row says otherwise.

Repo root: [`../README.md`](../README.md) · [`../SETUP.md`](../SETUP.md) · [`../CLAUDE.md`](../CLAUDE.md) · [`../AGENTS.md`](../AGENTS.md)

---

## The docs site itself

| Doc | Status | Owner | Last verified | What it is |
|---|---|---|---|---|
| [`index.md`](index.md) | current | Dan Browne | 2026-09-02 | The public front door of `docs.archimedes-arc.com`: the identity line, the three reader doors, what ships today with the endpoint printed beside each number, what the product does **not** do, and the two honesty artifacts. Every number on it is a live read, printed with the date it was read — the docs build stays hermetic. |
| [`doc-index.md`](doc-index.md) | current | Dan Browne | 2026-09-02 | This register. Enforced by `.github/scripts/docs_index.py`, which fails the docs gate when a `docs/**/*.md` file is listed neither here nor in a sub-index this file links to. |

## Architecture — start here

| Doc | Status | Owner | Last verified | What it is |
|---|---|---|---|---|
| [`architecture.md`](architecture.md) | current | Dan Browne | 2026-09-01 | System architecture map. ECS Fargate + ALB + CloudFront + WAF, Aurora PostgreSQL 18.3, ElastiCache Redis 7.1. Every claim is a link to a file. Amended 2026-09-01: reveal is hash-only, no IPFS pin (#1526). |
| [`reference/file-tree.md`](reference/file-tree.md) | reference | Dan Browne | 2026-07-14 | Repository map generated alongside the architecture map. |
| [`reference/flow-diagram.mmd`](reference/flow-diagram.mmd) | reference | Dan Browne | 2026-07-14 | Request/generation flow, Mermaid source (`flow-diagram.svg`, `file-tree.svg` render it). |
| [`database-architecture.md`](database-architecture.md) | current | Dan Browne | 2026-08-31 | Data stores, schemas, migration posture. The § 2.3 table list is a 2026-06-28 cutover inventory and has drifted ~15 tables — `backend/archimedes/db.py` is the live source; see `database-relations.md` for FKs and deletion policy. |
| [`database-relations.md`](database-relations.md) | current | Dan Browne | 2026-08-31 | Identity/ownership/money-table relational structure: the schema-relations audit (corrections + gap found), the Phase 1 indices + FKs from [PR #1438](https://github.com/aprin-labs/archimedes/pull/1438) — **merged 2026-08-31**, with #1429 reconciling the account-deletion policy — the target ERD, and the Phase 2 proposal (G1 shipped; the rest not built). |
| [`deployment.md`](deployment.md) | reference | Dan Browne | 2026-08-31 | Compose topology from one file: the `localdb` profile gate and the nginx runtime-DNS fix, both still accurate. Its **production** column is pre-Fargate (EC2 + `docker compose pull`) and is superseded by `local-vs-prod.md`. |
| [`local-vs-prod.md`](local-vs-prod.md) | current | Dan Browne | 2026-08-31 | The deployment contract (#1044): the row-by-row local/production selector table with the variable that decides each one, the four config leaks and where each stands, the recorded "no local auth bypass" decision, the fresh-clone ollama smoke path, and `make check-local`. |
| [`architectural-principles.md`](architectural-principles.md) | current | Dan Browne | 2026-07-28 | The four primitives the product is built to defend. |
| [`anti-features.md`](anti-features.md) | current | Dan Browne | 2026-07-28 | What Archimedes deliberately does not build. |

## API reference

| Doc | Status | Owner | Last verified | What it is |
|---|---|---|---|---|
| [`api/README.md`](api/README.md) | current | Dan Browne | 2026-08-20 | Index of the API reference: per-surface docs, the auth-model overview table, and the `/docs` (Swagger) production-gate note. |
| [`api/auth-and-accounts.md`](api/auth-and-accounts.md) | current | Dan Browne | 2026-08-20 | The Better Auth sidecar (`/api/auth/*`): email/password + OAuth, session lookup, email verification. Amended 2026-09-01 with `GET /api/auth/verification-status` — the delivery-state endpoint that replaced the resend button's eternal `200 {status:true}` (#1748). |
| [`api/wallets.md`](api/wallets.md) | current | Dan Browne | 2026-08-20 | `/api/wallets/*` — EIP-4361 wallet-link challenge/verify. |
| [`api/generation.md`](api/generation.md) | current | Dan Browne | 2026-08-20 | `/api/generate/*` — the debate-society generation pipeline, its x402 payment gate, and daily quotas. |
| [`api/strategies-and-rigor.md`](api/strategies-and-rigor.md) | current | Dan Browne | 2026-08-20 | `/api/strategies/*` and `/api/selection-bias/*` — the strategy library, portfolio advisor, stress testing, and the rigor gate. |
| [`api/paper-trading.md`](api/paper-trading.md) | current | Dan Browne | 2026-08-20 | `/api/paper/*` — deploy a strategy to an append-only, never-rewritten forward-return ledger. |
| [`api/vaults-and-chain.md`](api/vaults-and-chain.md) | current | Dan Browne | 2026-08-20 | `/api/vaults/*`, `/api/traces/*`, `/api/swap/*`, `/api/config/contracts`, and the health/root endpoints. |
| [`api/leaderboard-and-metrics.md`](api/leaderboard-and-metrics.md) | current | Dan Browne | 2026-08-20 | `/api/leaderboard` and the public, PII-free `/api/metrics/*` traction surface. |
| [`api/admin-private.md`](api/admin-private.md) | current | Dan Browne | 2026-08-31 | `/api/metrics/private/*` — the platform-admin-gated cost/ops dashboard (incl. the measured `$/generation`) and per-wallet identity roster. |
| [`api-surface-status.md`](api-surface-status.md) | current | Dan Browne | 2026-08-20 | Census of every router `backend/archimedes/main.py` registers: prefix, auth model, status, and whether a detailed doc above covers it (14/30 do). Backed by a completeness test that fails CI if a registered router has no row. |

## Product

| Doc | Status | Owner | Last verified | What it is |
|---|---|---|---|---|
| [`user-stories.md`](user-stories.md) | current | Dan Browne | 2026-08-31 | The locked product spine. Canonical statement of what the product is. Re-verified against `/api/health` 2026-08-31; the Day-9 body carries dated inline corrections (fusion-preview surface, "GLM-backed", library size, KG demo claim) and reads vault execution in the present tense, which is roadmap (#1469). |
| [`agent-api.md`](agent-api.md) | current | Dan Browne | — | Driving the full journey programmatically; the agent-native surface. |
| [`specs/agent-native-onboarding-spec.md`](specs/agent-native-onboarding-spec.md) | draft | Dan Browne | 2026-08-31 | How a CLI / agent-skill / (possible) MCP caller creates an account, links a wallet, and pays — the **deltas** from `agent-quickstart.md`, not a second copy of it. Records the six deltas that make the agent path different, the classifier rule that makes logged-in agents unmeasurable as agents, the reconciliation with the 3-free-generation gate ([#1643](https://github.com/aprin-labs/archimedes/issues/1643)), and six owner decisions D1–D6 — **all closed 2026-08-31/09-01** (recorded as PR comments on #1653); doc updates to `current` when the decision rows are folded in. |
| [`agent-quickstart.md`](agent-quickstart.md) | current | Dan Browne | 2026-08-31 | Zero to paper-traded for an external agent: eleven steps, exact response shapes, and an error table (401/402/409/422/429). Includes the live x402 paywall — production charges $2.00 USDC per generation, so steps 6a–6b link a wallet and pay. Narrower than `agent-api.md` on purpose: no vault, no capital deployed. Route strings and worked commands are drift-guarded by `backend/tests/test_agent_quickstart_drift.py`. Carries the MCP-client section (2026-08-31) pointing at `../mcp-server/README.md`. |
| [`claims-ledger.md`](claims-ledger.md) | current | Dan Browne | 2026-09-03 | Every public claim — Landing, `/security`, `README.md`, `agent-quickstart.md`, `Architecture.jsx`, `index.html` meta, `llms.txt`, `agent.json`, `user-stories.md`, paper trading — with a per-claim verdict (`TRUE` / `CHANGED` / `RETRACTED` / `OVER-CLAIMED` / `PENDING ADR MERGE`) and the file:line that backs it. Records what #1469's rebrand retracted, the open over-claims the scrub did not reach, and the market-data position. Citations are enforced by `backend/tests/test_claims_ledger.py`. |
| [`../mcp-server/README.md`](../mcp-server/README.md) | current | Dan Browne | 2026-08-31 | The MCP server — nine tools over the public HTTP API, for agents that call tools instead of `curl`. Lives outside `docs/` because it is a distribution's own README (as `cli/README.md` is) and ships in its sdist; indexed here because it is an agent-facing product surface and this index is where those are found. Deliberately thin: no business logic, no DB/Redis, no wallet key, and every route it calls is pinned to the live app — resolution **and** each tool's credential label — by `backend/tests/test_mcp_contract_drift.py`. Ships decision **D2** of the agent-native onboarding spec ([PR #1653](https://github.com/aprin-labs/archimedes/pull/1653) — that doc is not on `main` yet, so this row links the PR rather than a path that does not resolve here). |
| [`asset-universe.md`](asset-universe.md) | current | Dan Browne | — | Tradable universe and how it is assembled. |
| [`brief-guidelines.md`](brief-guidelines.md) | current | Dan Browne | 2026-09-03 | The RULES page for the Generate brief (#1801) — the 600-character bound, the reason-code vocabulary the deterministic screen returns (`shape.*`, `lang.*`, `inject.*`, `struct.*`, `pii.*`), what is explicitly still allowed and why the near-misses matter, how model text re-entering a prompt is omitted rather than rewritten and how third-party paper titles are quoted as data, and what a `BRIEF_UNVALIDATED` refusal means. Companion to `writing-a-brief.md`, which is the tutorial; this one is what trips. |
| [`writing-a-brief.md`](writing-a-brief.md) | current | Dan Browne | 2026-08-31 | The long-form prompting guide for the Generate page — the three parts of a brief that works, what the pipeline does with each, the five failure modes, and worked upgrades. Moved out of `Generate.jsx` by #1642, which leaves a one-line hint plus a link to this page. Also documents the Surprise Me bank (`ui/src/data/surpriseBriefs.js`) and the honest limits of what its machine checks prove. |
| [`archive/demo-script-lepton.md`](archive/demo-script-lepton.md) | archived | Dan Browne | 2026-09-01 | Lepton-era demo script (June 2026); carries retired branding, kept as history. |

## Quant and rigor — the math layer

| Doc | Status | Owner | Last verified | What it is |
|---|---|---|---|---|
| [`quant/README.md`](quant/README.md) | current | Önder Akkaya | 2026-08-31 | Index for the quant docs. Read this before any strategy claim. Now indexes the four dated findings notes as well as the four living references — it previously described itself as "these four docs" while the directory held eight (#1598). |
| [`quant/methodology.md`](quant/methodology.md) | current | Önder Akkaya | 2026-09-03 | The math layer end to end. DSR gate threshold is `0.95` and has exactly one definition, `rigor_profiles.DSR_P_BADGE_MIN` (#1794, 2026-09-03). |
| [`quant/admission-criteria.md`](quant/admission-criteria.md) | current | Önder Akkaya | 2026-08-31 | Tier-1 admission. DSR badge threshold is 0.95 — stated as the level-1 row of the `rigor_profiles` strictness ladder, which is `DSR_P_BADGE_MIN` itself (#1794), and the promotion flow no longer passes the library's length as the trial count, a convention reversed on 2026-07-09 (#1598). |
| [`quant/backtest-interpretation.md`](quant/backtest-interpretation.md) | current | Önder Akkaya | 2026-08-31 | How to read a backtest without fooling yourself. The doc's two DSR thresholds disagreed with each other and then with the code; both now read the one bar, `DSR_P_BADGE_MIN` (#1794, 2026-09-03). |
| [`quant/strategy-library.md`](quant/strategy-library.md) | current | Önder Akkaya | 2026-08-31 | Curated library reference. Pass/fail status is whatever the live gate returns, not a number in a doc — three headings that carried ✅/❌ verdicts contradicting the status lines beneath them were removed, and Faber's 0.612 was corrected from an "OOS Sharpe" to the DSR p-value it actually is (#1598). |
| [`quant/library-pbo.md`](quant/library-pbo.md) | findings | Önder Akkaya | 2026-08-31 | Library-level PBO findings (fourth wave), measured 2026-06-11. Stamped historical and its pass-count phrasing retracted 2026-08-31 (#1598): the 0.047 headline is a 22-strategy figure and CSCV PBO moves with every library addition. |
| [`quant/third-wave-retest.md`](quant/third-wave-retest.md) | findings | Önder Akkaya | 2026-08-31 | Third-wave candidates through the cost model and walk-forward, measured 2026-06-11. Stamped historical and its two pass-count phrasings retracted 2026-08-31 (#1598). |
| [`quant/second-wave-universe-experiment.md`](quant/second-wave-universe-experiment.md) | findings | Önder Akkaya | 2026-08-31 | Does a bigger universe rescue the second-wave strategies — measured 2026-06-11. Stamped historical 2026-08-31 (#1598): the pass count is retracted, and the `num_trials` sweep shows both bars side by side. #1794 restored `0.95`, so the column it was originally computed against is the live one again. |
| [`rigor-methods.md`](rigor-methods.md) | current | Önder Akkaya | 2026-07-28 | The four gates: DSR, PBO, walk-forward OOS, look-ahead audit. |
| [`analysis/faber-dsr-finding.md`](analysis/faber-dsr-finding.md) | findings | Önder Akkaya | — | Why Faber 2007 fails the gate, and why that is the correct outcome. |
| [`analysis/insights-analytics-gap-2026-08-31.md`](analysis/insights-analytics-gap-2026-08-31.md) | findings | Dan Browne | 2026-08-31 | Which Insights tiles are really measured, and the one structural gap (`settled_volume_usd`, blocked on unwritten `amount_usdc` + #975). Companion to the #1648 admin-gate fix. |
| [`benchmarks/stockbench-results.md`](benchmarks/stockbench-results.md) | findings | Önder Akkaya | — | StockBench evaluation results (`stockbench-results.json`, `stockbench-vs-baselines.png`). |
| [`specs/selection-bias-corrections-spec.md`](specs/selection-bias-corrections-spec.md) | spec | Önder Akkaya | 2026-07-28 | DSR + PBO + walk-forward + look-ahead audit math and thresholds. |
| [`specs/transaction-cost-turnover-model.md`](specs/transaction-cost-turnover-model.md) | shipped | Önder Akkaya | 2026-06-11 | Transaction-cost and turnover model in the analytics engine. |
| [`cited-literature.md`](cited-literature.md) | current | Dan Browne | 2026-08-20 | The five load-bearing papers behind the gate, including the two that are cited against us. |

## Corpus and generation

| Doc | Status | Owner | Last verified | What it is |
|---|---|---|---|---|
| [`generation-cost-instrumentation.md`](generation-cost-instrumentation.md) | current | Dan Browne | 2026-08-31 | What one generation actually consumes: per-job token counts, per-stage wall/CPU seconds, peak RSS, row writes — plus the measured `$/generation` those counts price to on the admin-only cost endpoint. The customer-facing quote seam stays `flat_v1`. |
| [`corpus-architecture.md`](corpus-architecture.md) | target-state | Dan Browne | 2026-09-01 | arXiv preprints (not peer-reviewed), metadata + abstracts only. Live count: `GET /health` `corpus_papers` / `corpus_db_count` — do not freeze a number. **Describes embeddings/clusters/KG as built; in prod none of the three exist** (#778). Selection is a **keyword filter** and only that candidate set is re-scored at request time — nothing is precomputed; `/health` `paper_rag` names the live scorer, and the graph/KG endpoints 503 or return empty. |
| [`specs/multi-agent-debate-spec.md`](specs/multi-agent-debate-spec.md) | shipped | Dan Browne | 2026-07-28 | The debate society — the sole generation pipeline. |
| [`specs/prompt-inventory.md`](specs/prompt-inventory.md) | current | Dan Browne | 2026-09-03 | Every prompt this tree sends a model, rendered from the `agents/prompts.py` registry by `scripts/gen_prompt_inventory.py`. Edit the registry, not this file. |
| [`specs/strategy-fusion-spec.md`](specs/strategy-fusion-spec.md) | shipped | Dan Browne | 2026-09-01 | Multi-paper synthesis feeding the debate proposals. Future public-storage pin is owner-gated ([ADR](adr/ipfs-pinning-not-live.md)). |
| [`specs/strategy-passport-spec.md`](specs/strategy-passport-spec.md) | shipped | Dan Browne | — | Paper-grounding contract carried by every strategy. |
| [`specs/assoc-v1-spec.md`](specs/assoc-v1-spec.md) | current | Dan Browne | 2026-09-03 | `assoc/v1` — the one shape a paper→strategy association takes, the identity-only projection the content hash may see, the merge-not-delete rule for passport refs, and what a `papers_verified` green check does and does not mean. **Internal: excluded from the published docs site** (`mkdocs.yml` `exclude_docs`). |
| [`specs/strategy-dsl-spec.md`](specs/strategy-dsl-spec.md) | spec | Dan Browne | — | Strategy DSL. |
| [`specs/strategy-lifecycle-spec.md`](specs/strategy-lifecycle-spec.md) | spec | Dan Browne | — | Draft → generated → published lifecycle. |
| [`specs/generation-streaming-spec.md`](specs/generation-streaming-spec.md) | spec | Dan Browne | — | SSE contract for the Generate page. |
| [`specs/generation-quote-contract.md`](specs/generation-quote-contract.md) | spec | Dan Browne | — | Public cost quote + 409/402 x402 paywall on `/api/generate/start`, ratified in #1296. Frontend ships behind `VITE_GENERATION_QUOTE_ENABLED`. |
| [`specs/kb-integration-spec.md`](specs/kb-integration-spec.md) | spec | Dan Browne | — | KnowledgeBase submodule integration. |
| [`specs/page-roles-spec.md`](specs/page-roles-spec.md) | spec | Dan Browne | — | What each page is for. |
| [`specs/component-interfaces-spec.md`](specs/component-interfaces-spec.md) | spec | Dan Browne | — | Component interfaces and the team work split. |
| [`specs/ecosystem-design-spec.md`](specs/ecosystem-design-spec.md) | spec | Dan Browne | — | Marketplace/ecosystem design. Marketplace ships behind `PAYMENTS_DRY_RUN`. |
| [`specs/xia-2026-protocols.md`](specs/xia-2026-protocols.md) | spec | Dan Browne | — | Xia et al. 2026 named protocols as implemented here. |
| [`specs/architecture-page-design.md`](specs/architecture-page-design.md) | implemented | Dan Browne | 2026-07-28 | Design for the Architecture page, **implemented in PR #1192**. Should leave `specs/` for `architecture/` or `archive/` once #1192 merges, per `CONVENTIONS.md` § 1. |
| [`diagrams/strategy-passport-architecture.md`](diagrams/strategy-passport-architecture.md) | reference | Dan Browne | — | Passport architecture diagram + reference. |
| [`bedrock-model-cost-comparison.md`](bedrock-model-cost-comparison.md) | reference | Dan Browne | — | Bedrock model costs, us-east-1 on-demand. LLM is `bedrock_converse` / `amazon.nova-micro-v1:0`. |
| [`cost-estimates/generate-llm-costs.md`](cost-estimates/generate-llm-costs.md) | reference | Dan Browne | — | Per-generation LLM cost estimate. |
| [`infrastructure-provider-analysis.md`](infrastructure-provider-analysis.md) | reference | Daniel Reis | 2026-08-16 | Modeled infra cost comparison across providers (list prices, not invoice). |

## On-chain and Arc

| Doc | Status | Owner | Last verified | What it is |
|---|---|---|---|---|
| [`arc-integration.md`](arc-integration.md) | current | Dan Browne | 2026-07-28 | Arc testnet reference and Circle integration. |
| [`specs/vault-semantics-spec.md`](specs/vault-semantics-spec.md) | spec | Dan Browne | 2026-07-28 | Vault lifecycle and trade-window semantics. |
| [`specs/commit-reveal-trace-spec.md`](specs/commit-reveal-trace-spec.md) | spec | Dan Browne | 2026-09-01 | Commit-before-trade reasoning-trace anchoring. Live `storagePointer` is empty (hash-only); IPFS pinning is not live ([ADR](adr/ipfs-pinning-not-live.md)). Contract review: Bogdan Sivochkin. |
| [`specs/ipfs-reasoning-traces-design-note.md`](specs/ipfs-reasoning-traces-design-note.md) | archived (superseded) | Dan Browne | 2026-09-01 | Proposed IPFS pinning via Pinata. Superseded by [`adr/ipfs-pinning-not-live.md`](adr/ipfs-pinning-not-live.md): the pin client was never wired into prod and is now removed. |
| [`specs/mnemonik-integration-scoping.md`](specs/mnemonik-integration-scoping.md) | current (internal — not on the docs site) | Dan Browne | 2026-09-03 | #714 sub-task C verdict: adopt the protocol math, reimplement natively on Arc, take no Mnemonik dependency. Internal: excluded from the site build (`mkdocs.yml` `exclude_docs`). |
| [`specs/execution-trading-agent-society-spec.md`](specs/execution-trading-agent-society-spec.md) | draft | Dan Browne | 2026-06-28 | Execution/trading agent society. Seed-and-refine draft. |

## Security

| Doc | Status | Owner | Last verified | What it is |
|---|---|---|---|---|
| [`security/auth-model.md`](security/auth-model.md) | current — open gap | Dan Browne | — | What authentication is actually enforced and the known testnet gap. Read before exposing anything. |
| [`runbooks/github-security-toggles.md`](runbooks/github-security-toggles.md) | runbook | Dan Browne | — | Repository security settings. |

## Runbooks and operations

| Doc | Status | Owner | Last verified | What it is |
|---|---|---|---|---|
| [`runbooks/README.md`](runbooks/README.md) | current | Dan Browne | 2026-07-28 | Index of every runbook, and an explicit list of the runbooks that do **not** exist yet — including the missing Fargate break-glass procedure. |
| [`runbooks/operations.md`](runbooks/operations.md) | current | Dan Browne | 2026-07-28 | Run the stack, RPC deep-dive, LLM backends, security notes. |
| [`runbooks/arc-testnet-e2e.md`](runbooks/arc-testnet-e2e.md) | runbook | Dan Browne | — | End-to-end testnet smoke test. |
| [`runbooks/arc-testnet-e2e-evidence.md`](runbooks/arc-testnet-e2e-evidence.md) | evidence | Önder Akkaya | 2026-05-26 | Replayable on-chain evidence for SPEC-1. |
| [`runbooks/spec-1-walkthrough.md`](runbooks/spec-1-walkthrough.md) | runbook | Dan Browne | — | SPEC-1 user-journey walkthrough. |
| [`runbooks/t3.2-contract-redeploy.md`](runbooks/t3.2-contract-redeploy.md) | runbook | Dan Browne | — | Contract redeploy procedure and secret handling. |
| [`runbooks/docs-site-setup.md`](runbooks/docs-site-setup.md) | runbook | Dan Browne | 2026-08-31 | Docs site at `docs.archimedes-arc.com` on our own S3 + CloudFront (#1634): apply `docs-site/infra`, publish, invalidate, roll back, local preview, and what `mkdocs build --strict` guards. |
| [`operations/feature-flag-fliplist.md`](operations/feature-flag-fliplist.md) | current | Dan Browne | 2026-08-31 | The go-live checklist (#834): every feature flag in the tree, classified LIVE / FLIP-AT-LAUNCH / DEAD, with its deployed value, its reader, and the precondition for flipping it. Enforced in both directions — `backend/tests/test_feature_flag_fliplist_drift.py` re-derives the inventory and fails CI on any flag with no row, and on any actionable row whose flag no longer has a reader. |
| [`runbooks/backtest-results-retention.md`](runbooks/backtest-results-retention.md) | runbook | Dan Browne | 2026-08-30 | `backtest_results` archive-then-prune procedure (v8 Lane 3.1): keep policy, `archive_backtest_results.py`'s `--plan`/`--archive`/`--prune` flags, the manifest-verification guard, and the post-prune VACUUM step. |
| [`runbooks/erc8004-identity-registration.md`](runbooks/erc8004-identity-registration.md) | runbook | Dan Browne | 2026-08-31 | Minting the ERC-8004 agent identity on Arc (#1527): the live-verified registry facts, `--plan`/`--verify`/`--execute`, the Circle-signed owner step, and why `ERC8004_AGENT_ID` points at a token rather than making a claim. |
| [`runbooks/runner-ec2-wedge.md`](runbooks/runner-ec2-wedge.md) | runbook | Dan Browne | 2026-08-31 | `archimedes-runner` wedge (#1402): the impaired-instance-check + dead-SSM-agent signature, read-only diagnosis commands, the recovery ladder, what the new `ec2:reboot` alarm automates, and the operator-only steps (`terraform apply`, the one-time live reboot test). |
| [`runbooks/cloudfront-cache-behaviour-apply.md`](runbooks/cloudfront-cache-behaviour-apply.md) | runbook | Dan Browne | 2026-09-03 | Applying a CloudFront cache-behaviour change: why merging `infra/cloudfront.tf` changes nothing on its own, the exact plan to expect for #1768 (applied) and #1776 (+7 static-asset behaviours on `aws_cloudfront_distribution.main`, nothing else), the mid-list-insert rendering caveat with a real rendered plan, when an invalidation is and is not needed, the `age`/`x-cache` verification, and the rollbacks. |
| [`runbooks/email-verification-validation.md`](runbooks/email-verification-validation.md) | runbook | Dan Browne | 2026-08-31 | Live validation of signup verification + password reset before `EMAIL_VERIFICATION_ENFORCED` flips: pre-flight AWS/DNS/log checks, the local console-mailer rehearsal, the sandbox-safe production test, the reset rehearsal (including session revocation and link replay), the flip's exact blast radius and rollback, six audit findings that gate it, and the SES sandbox-vs-production gotchas. |
| [`runbooks/ses-suppression.md`](runbooks/ses-suppression.md) | runbook | Dan Browne | 2026-09-01 | The AWS **account-level** SES suppression list (#1748 item 4): why a suppressed address makes `SendEmail` succeed and silently drop the message, how to inspect the list read-only, the three conditions that must all hold before an address comes off, and why there is deliberately no bulk clear. Driven by `backend/archimedes/scripts/ses_suppression.py` — dry-run by default, `--apply` to act. |
| [`runbooks/dmarc-reports.md`](runbooks/dmarc-reports.md) | runbook | Dan Browne | 2026-09-03 | The DMARC aggregate-report inbox (#1504): the receipt-rule → private-S3 wiring that makes reports arrive at all, how to read a per-source-IP pass/fail table, why `policy_evaluated` and not `auth_results` is DMARC, the four conditions that gate moving off `p=none`, the `quarantine`→`reject` ramp and how to roll it back, and the diagnosis ladder for silence. Driven by `scripts/dmarc_report_summary.py` — exits non-zero rather than printing an empty all-clear table. |
| [`runbooks/curated-backtests.md`](runbooks/curated-backtests.md) | runbook | Dan Browne | 2026-09-01 | Producing curated backtest rows with `run_backtests.py` (#1760): the three triggers that justify a run (a strategy file changed, a data-quality fix, an owner decision — never a calendar), the one-off Fargate `run-task` invocation and its waiter caveat, how to read the summary line, and the four constraints this must never break (no clock, no boot hook, never in the serving process, never for generated strategies). |
| [`runbooks/market-data-provider-proof.md`](runbooks/market-data-provider-proof.md) | runbook | Dan Browne | 2026-09-01 | Getting `TIINGO_API_TOKEN` onto the running backend container (#1798): why the SSM parameter existed for a day and a half with nothing reading it, the two registrars for the task definition (#1799) and which one actually ships, the `verify_market_data.py` proof pull and what to paste on the issue, and why flipping the daily seam (`MARKET_DATA_DAILY_PROVIDER`, #1798) is a separate owner call — and why setting the wider `MARKET_DATA_PROVIDER` instead moves daily bars by the back door. |
| [`incidents/2026-09-03-paper-advance-ddl-wedge.md`](incidents/2026-09-03-paper-advance-ddl-wedge.md) | current | Dan Browne | 2026-09-03 | The 2026-09-03 P0 (#1818): the paper-advance child ran `init_db()` DDL every cycle in both ECS tasks, wedging Postgres on a lock chain PostgreSQL could not see. Full timeline, the six-part mechanism, what the P1 PR fixes, and what it deliberately leaves to P2–P6 (including P6: `init_db()` still runs on the serving path). |

## Decisions (ADRs)

| Doc | Status | Owner | Last verified | What it is |
|---|---|---|---|---|
| [`adr/README.md`](adr/README.md) | current | Dan Browne | 2026-09-01 | ADR index and status vocabulary. All twenty-four records are listed there. |
| [`adr/unlicense-public-domain.md`](adr/unlicense-public-domain.md) | accepted | Dan Browne | initial commit | The Unlicense as a public-domain dedication, and its ownership/contributor consequences. |
| [`adr/arc-settlement-chain.md`](adr/arc-settlement-chain.md) | accepted | Dan Browne | 2026-05-13 | Arc testnet 5042002; USDC as settlement asset and native gas token. |
| [`adr/two-tier-marketplace.md`](adr/two-tier-marketplace.md) | accepted | Dan Browne | 2026-05-13 | Verified / Community tiers; rigor as the wedge. |
| [`adr/backtrader-backtest-engine.md`](adr/backtrader-backtest-engine.md) | accepted | Dan Browne | 2026-05-13 | backtrader chosen as the v1 backtest engine. |
| [`adr/build-on-deploy-main-only.md`](adr/build-on-deploy-main-only.md) | accepted | Dan Browne | 2026-05-18 | Build-on-deploy, main-only branch model. |
| [`adr/portfolio-constructor-decision-tree.md`](adr/portfolio-constructor-decision-tree.md) | accepted | Önder Akkaya | 2026-05-22 | Which constructor runs when. |
| [`adr/k1-generation-external-rigor-gate.md`](adr/k1-generation-external-rigor-gate.md) | accepted | Dan Browne | 2026-05-23 | K=1 generation with an externalised rigor gate. |
| [`adr/aws-account-migration.md`](adr/aws-account-migration.md) | accepted | Dan Browne | 2026-06-24 | Production moved to account 037613907429 / us-east-1. |
| [`adr/generation-payment-credit-not-refund.md`](adr/generation-payment-credit-not-refund.md) | accepted | Önder Akkaya | 2026-08-29 | An undelivered generation is repaid as a durable credit, never a refund; the claim is taken before the money moves. |
| [`adr/glm-to-bedrock-llm-migration.md`](adr/glm-to-bedrock-llm-migration.md) | accepted | Dan Browne | 2026-06-24 | GLM → Bedrock. |
| [`adr/non-custodial-vault-owner-agent.md`](adr/non-custodial-vault-owner-agent.md) | accepted | Dan Browne | 2026-06-26 | Owner ≠ agent; non-custodial vaults. **Amended 2026-08-31** (product framing only, decision unchanged): the contracts are deployed; the user-facing vault journey is roadmap, gated off public surfaces (#1266/#1354/#1469). |
| [`adr/portfolio-constructor-consolidation.md`](adr/portfolio-constructor-consolidation.md) | accepted | Önder Akkaya | 2026-06-26 | Legacy constructor paths retired; dual-signal sizer activated. |
| [`adr/rigor-gate-unification.md`](adr/rigor-gate-unification.md) | accepted | Dan Browne | 2026-06-26 | One source of selection-bias truth. |
| [`adr/fusion-primary-generation.md`](adr/fusion-primary-generation.md) | superseded | Dan Browne | 2026-06-26 | Fusion-primary routing. Superseded by the debate-society record (2026-07-09). |
| [`adr/chainlink-primary-oracle.md`](adr/chainlink-primary-oracle.md) | accepted | Dan Browne | 2026-07-01 | Chainlink-primary oracles with a thin, bounded admin fallback. |
| [`adr/ec2-to-ecs-fargate-cutover.md`](adr/ec2-to-ecs-fargate-cutover.md) | accepted | Dan Browne | 2026-07-09 | Serving tier moved from one EC2 box to ECS Fargate behind the existing ALB. |
| [`adr/debate-society-sole-generation-pipeline.md`](adr/debate-society-sole-generation-pipeline.md) | accepted | Dan Browne | 2026-07-09 | The debate society is the only generation path; no fallback. |
| [`adr/num-trials-self-containment.md`](adr/num-trials-self-containment.md) | accepted (ratified 2026-08-31; option 1 board FDR 2026-09-01) | Dan Browne | 2026-09-01 | DSR trial count depends only on the strategy's own search; curated strategies grade at num_trials = 1. Board FDR is ranking-surface via #1580: advisory, never flips `passes_all`, not on the passport. |
| [`adr/aurora-postgres-alembic-datastore.md`](adr/aurora-postgres-alembic-datastore.md) | accepted | Dan Browne | 2026-07-28 | Aurora Serverless v2 (18.3) + Alembic; Redis 7.1 ephemeral-only. |
| [`adr/strategy-dsl-hardening-over-lean4.md`](adr/strategy-dsl-hardening-over-lean4.md) | accepted | Dan Browne | 2026-08-30 | No Lean 4 on the emission path; harden the existing closed-enum DSL instead. Sandbox reserved for inexpressible shapes; languages re-evaluated only on a trigger. |
| [`adr/market-data-sourcing.md`](adr/market-data-sourcing.md) | accepted | Dan Browne | 2026-08-31 | Market data is sourced **by surface**: Tiingo (Free tier, for testing) for backtesting and paid analysis; yfinance for the free, ungated Explore viewer, which sells and redistributes nothing. Flags a **Tiingo commercial plan as a mainnet prerequisite**, and records that the split is reversible by build — a full vendor swap is a config + adapter change, not surgery (#1218). |
| [`adr/lambda-generation-offload.md`](adr/lambda-generation-offload.md) | proposed (verdict: defer) | Dan Browne | 2026-08-30 | Measured spike (#1411): a real Lambda container built from the production backend image reaches Redis/Aurora/Bedrock/MiniLM from inside the VPC, but cold start is 13.6 s steady-state and 51 s after a deploy. Defers the lane; adopts the lane-agnostic worker entrypoint and the measured-cost model, and records why the quote seam is `_price()` rather than `quote()`. |
| [`adr/ipfs-pinning-not-live.md`](adr/ipfs-pinning-not-live.md) | accepted | Dan Browne | 2026-09-01 | Reveal is hash-only (empty `storagePointer`). The Pinata pin path is removed, not half-wired — `PINATA_JWT` was never in prod ECS secrets (#1526). |
| [`adr/backtests-are-frozen-evidence.md`](adr/backtests-are-frozen-evidence.md) | accepted | Dan Browne | 2026-09-01 | A backtest is a one-time artifact with a stated data window, never revisited on a clock. Generated strategies are backtested once at generation; curated ones only when their code changes or by explicit operator action. No periodic or boot-time refresh anywhere; forward performance is the paper-trading ledger's job. Retires the in-app refresh loop that killed ECS tasks on 2026-09-01 (#1760). |
| [`adr/rigor-verdict-of-record.md`](adr/rigor-verdict-of-record.md) | accepted | Dan Browne | 2026-09-01 | Generation, backtesting and grading are one-time events: a strategy is graded **once, at backtest time**, by the real gate, and the verdict is persisted on its passport with `graded_at` / `gate_version` / `cohort_n`. Every surface reads the stored verdict; a re-grade is an explicit, versioned event, never a recompute on read. Supersedes #868's read-time premise; #821's rule survives tightened (#1746/#1747). |

## Plans and roadmaps (intent, not state)

| Doc | Status | Owner | Last verified | What it is |
|---|---|---|---|---|
| [`account-authentication.md`](account-authentication.md) | runbook | Daniel Reis | 2026-08 | Better Auth deploy runbook: secrets, ECR, rollback (#1194); account linking, explicit link/unlink (#1420 follow-up; implicit auto-link stays off); account management — email/password change, session revocation, deletion (#1367). |
| [`plans/2026-07-28-account-auth-app-boundary.md`](plans/2026-07-28-account-auth-app-boundary.md) | plan | Daniel Reis | 2026-07-28 | The #1194 account-auth boundary plan. |
| [`plans/2026-08-15-core-app-visual-refresh.md`](plans/2026-08-15-core-app-visual-refresh.md) | plan | Daniel Reis | 2026-08-15 | Core-app visual refresh plan. |
| [`plans/2026-08-22-calm-precision-rebrand.md`](plans/2026-08-22-calm-precision-rebrand.md) | plan | Daniel Reis | 2026-08-22 | Calm-precision rebrand plan (PR #1469). |
| [`plans/2026-08-23-phantom-inspired-public-landing.md`](plans/2026-08-23-phantom-inspired-public-landing.md) | plan | Daniel Reis | 2026-08-23 | Public landing redesign plan (PR #1469). |
| [`plans/2026-08-23-security-posture-page.md`](plans/2026-08-23-security-posture-page.md) | plan | Daniel Reis | 2026-08-23 | Static /security posture page plan (PR #1469). |
| [`plans/2026-08-30-relations-phase2.md`](plans/2026-08-30-relations-phase2.md) | plan | Dan Browne | 2026-08-30 | Relations Phase 2 (builds on PR #1438): passport ↔ proposal ↔ user entity graph, FK/index plan with orphan-audit SQL, dead-column drops, `brief_intent` promotion, sizing and ordering. |
| [`plans/2026-08-30-intraday-paper-trading.md`](plans/2026-08-30-intraday-paper-trading.md) | draft | Dan Browne | 2026-08-30 | Intraday paper trading (v8 Lane 3.5). Recommends 15-min mark-to-market on open positions (`paper_marks` + retention + a runner-box loop) and defers intraday *signal* evaluation to v2 behind an ADR — bar-counted rebalance cadence and indicator warmup would silently change strategy semantics. |
| [`plans/2026-08-30-interpreter-unification.md`](plans/2026-08-30-interpreter-unification.md) | draft | Dan Browne | 2026-08-30 | Collapsing the two DSL interpreters (backtrader backtest vs live signal FSM) into one shared decision core: divergence inventory, architecture options, and a migration ratcheted on the parity suite. Cross-references the intraday-paper-trading plan (§4.1). |
| [`plans/2026-08-30-paper-trading-reasoning-traces.md`](plans/2026-08-30-paper-trading-reasoning-traces.md) | draft | Dan Browne | 2026-08-30 | Wiring paper-trading decisions into the commit-reveal trace pipeline (#1575). Where a paper decision is born (the settle-path rebalance boundary — marks never decide), the trace body next to the house agent's, owner stamping via #1556 (and why the zero-address sentinel would leak), passport reachability via #1569's `trace_references_strategy`, why on-chain anchoring is default OFF (consent + cost), the loud-failure design for an unpublished decision, and a numbered build plan. |
| [`plans/quant-roadmap.md`](plans/quant-roadmap.md) | plan | Önder Akkaya | — | The portfolio-math and backtest-rigor lane. |
| [`plans/spine-plus-v2-plan.md`](plans/spine-plus-v2-plan.md) | plan | Dan Browne | — | Spine+ v2 phase plan. |
| [`plans/second-wave-multi-asset-strategies.md`](plans/second-wave-multi-asset-strategies.md) | plan | Önder Akkaya | 2026-06-11 | Second-wave multi-asset strategies. |
| [`plans/paper-replication-spec.md`](plans/paper-replication-spec.md) | plan | Önder Akkaya | — | Replication and original-extension workflow. |
| [`plans/future-strategy-language-reeval-issue.md`](plans/future-strategy-language-reeval-issue.md) | draft issue | Dan Browne | 2026-08-30 | Unfiled Future Plans issue body: the trigger conditions under which the emission-language decision re-opens. Dormant by design — do not close for inactivity. |

## Sprint session cards (current)

| Doc | Status | Owner | Last verified | What it is |
|---|---|---|---|---|
| [`sprint/README.md`](sprint/README.md) | current | Önder Akkaya | 2026-08-21 | Index of per-session working cards for the Arc-mainnet sprint, plus the per-card completion status and the corrections to the cards' own claims. |

## Audits and findings

| Doc | Status | Owner | Last verified | What it is |
|---|---|---|---|---|
| [`audits/2026-06-14-full-tree-audit.md`](audits/2026-06-14-full-tree-audit.md) | audit — lineage head | Dan Browne | 2026-06-14 | Full-repo audit. Carries the only resolution ledger; supersedes the earlier audit chain. |
| [`audits/2026-06-14-gpt-oss-findings-verified.md`](audits/2026-06-14-gpt-oss-findings-verified.md) | audit | Dan Browne | 2026-07-28 | GPT-OSS findings, verified. |
| [`audits/2026-06-13-Onder-findings.md`](audits/2026-06-13-Onder-findings.md) | audit | Önder Akkaya | 2026-06-13 | Resilience and stress-test report. |
| [`audits/2026-07-09-curated-consolidation.md`](audits/2026-07-09-curated-consolidation.md) | audit | Önder Akkaya | 2026-07-09 | Curated-example consolidation build and verification. |
| [`audits/merge-handoff-2026-06-10.md`](audits/merge-handoff-2026-06-10.md) | historical log | Dan Browne | 2026-06-10 | Merge handoff for the 2026-06-10 remediation PRs. |
| [`audits/rigor-gate-fixes.md`](audits/rigor-gate-fixes.md) | needs owner | Önder Akkaya | — | A bare patch list with no findings, owner or dates. Should be tracked issues, then deleted. |

## Handovers and session logs (historical — not current state)

| Doc | Status | Owner | Last verified | What it is |
|---|---|---|---|---|
| [`handovers/2026-07-14-architecture-review.md`](handovers/2026-07-14-architecture-review.md) | historical log | Dan Browne | 2026-07-28 | Architecture-page redesign summary; evidence behind `architecture.md` §10. Its "what's stale" section describes a page that no longer exists — the redesign was **implemented in PR #1192**. |
| [`handovers/second-wave-handover.md`](handovers/second-wave-handover.md) | historical log | Önder Akkaya | — | Second-wave brief. |
| [`handovers/third-wave-handover.md`](handovers/third-wave-handover.md) | historical log | Önder Akkaya | — | Third-wave (fidelity) brief. |
| [`handovers/fourth-wave-handover.md`](handovers/fourth-wave-handover.md) | historical log | Önder Akkaya | — | Fourth-wave (gate-as-product) brief. |
| [`handovers/phase8-9-landing-and-fusion-spec.md`](handovers/phase8-9-landing-and-fusion-spec.md) | historical log | Dan Browne | 2026-05-24 | Phase 8/9 end-of-context session log. Named like a spec; it is not one. |

## Research and prompts

| Doc | Status | Owner | Last verified | What it is |
|---|---|---|---|---|
| [`research/README.md`](research/README.md) | reference | Dan Browne | — | Research artifacts index. |
| [`research/linus-archimedes-comparison.md`](research/linus-archimedes-comparison.md) | reference | Dan Browne | — | Bidirectional architecture comparison with Linus. |
| [`research/archimedes-to-linus-portbacks.md`](research/archimedes-to-linus-portbacks.md) | reference | Dan Browne | — | What Archimedes sends back to Linus. |
| [`prompts/quant-audit-prompt.md`](prompts/quant-audit-prompt.md) | template | Önder Akkaya | — | LLM prompt template for a quant audit. Not an audit. |
| [`prompts/agentic-issue-skeleton.md`](prompts/agentic-issue-skeleton.md) | template | Dan Browne | 2026-07-28 | Copy-paste skeleton for a judge-grade issue spec dispatched to the agentic system. |

---

## Working with the repo and the team

| Doc | Status | Owner | Last verified | What it is |
|---|---|---|---|---|
| [`CONVENTIONS.md`](CONVENTIONS.md) | current | Dan Browne | 2026-07-28 | Where a new doc goes, how it is named, its front-matter, and the ADR lifecycle. Read before adding any file. |
| [`team.md`](team.md) | current | Dan Browne | 2026-07-28 | Roster, lanes, review coverage, timezones, sync window. Extracted from `CLAUDE.md`. |
| [`agent-gotchas.md`](agent-gotchas.md) | current | Dan Browne | 2026-07-28 | Character-limited message surfaces (`wc -m`, not `wc -c`) and zsh quoting traps. Both were paid for. |
| [`agent-operations.md`](agent-operations.md) | current | Dan Browne | 2026-08-31 | How this team dispatches AI agents: spec-driven execution mechanics (acceptance criteria, anti-goals, the pre-close verification gate, verify-your-own-audit-claims), parallel fan-out discipline (canary-first, worktree isolation, as-you-go cleanup), and agent-as-proxy authorization with its two never-proxy exceptions. Extracted from [`../CLAUDE.md`](../CLAUDE.md) 2026-08-31; the session file keeps the one-line rules and points here. |
| [`testing-conventions.md`](testing-conventions.md) | current | Dan Browne | 2026-08-31 | The hermetic-test rules: the `env -i` gate, the forbidden `asyncio.get_event_loop()` pattern, the subprocess-env recipe, boundary-mock precedents with file references, coverage targets, and why flaky tests are never skip-marked. Extracted from [`../CLAUDE.md`](../CLAUDE.md) § Testing conventions 2026-08-31. Command reference lives in [`../SETUP.md`](../SETUP.md). |
| [`submodules.md`](submodules.md) | current | Dan Browne | 2026-07-28 | The three submodules, what each is for, and the sticky-config one-liner a fresh clone needs. |
| [`agent-wiki.md`](agent-wiki.md) | current | Dan Browne | 2026-08-31 | Provenance note for the agent-generated wiki (`openwiki/`, landed in [#1597](https://github.com/aprin-labs/archimedes/pull/1597)) that the docs site publishes as its own nav section: what generated it (OpenWiki 0.4.3, coding-agent mode, no provider spend), the `docs/quant/`-only read boundary, that its claims are grounded in *documents* rather than in code, and that the pages are **not** line-by-line human-reviewed. Read this before citing anything under `/openwiki/`. |
| [`decisions/tooling-adoptions-2026-08.md`](decisions/tooling-adoptions-2026-08.md) | current | Dan Browne | 2026-08-31 | Register of developer tooling adopted in August 2026 and what each one cost and produced. Holds the standard "run it against a real corpus slice and record what it cost and what it produced, or remove it — installed is not a resting state," plus the OpenWiki row: the Bedrock blocker (Anthropic use-case form not submitted for the account), the `docs/quant/` run, and its verdict. **Placement is off-convention** — a closed-off decision belongs in [`adr/`](adr/README.md); this file sits at the path it was named at, and folding it into an ADR is open follow-up. |

---

## Conventions

- **No dates in top-level filenames.** Dated files belong in `audits/`, `handovers/` or `archive/`, named `YYYY-MM-DD-slug.md`.
- **A decision is stated once, in one ADR.** Prose docs link to the record; they do not
  restate or re-argue it. If a spec disagrees with an `Accepted` ADR, the spec is wrong —
  open a superseding ADR, do not patch the prose.
- **One kind per directory.** `specs/` holds specs. Session logs go to `handovers/`, roadmaps to `plans/`, findings to `quant/`, decisions to `adr/`, prompt templates to `prompts/`.
- **Never state a curated-library pass count in a doc.** Strategy pass/fail is whatever the live rigor gate returns; a number written here goes stale silently and has been wrong before. Point readers at the gate.
- **Contract counts come from `GET /api/config/contracts`**, not from prose. The tree currently holds 12 Solidity sources / 22 `.sol` files / 21 ABIs.
- **Archiving is a move plus a banner**, not a deletion: `> **ARCHIVED <date> — historical. Current: <path>**`.
