# Code Context — Scouting (no concrete task target given)

> **Historical snapshot — 2026-07-04.** Branch, status, issue, and file claims below describe that scout run only. Re-verify against current HEAD before acting.

## Findings

- **Git state:** on `main`, tracking `origin/main`, **working tree clean** — nothing to commit, no stash, no WIP. `main...origin/main` (may be a few behind; build-on-deploy fast-moving branch).
- **Local branch `dbrowneup/issue-589-commit-before-trade` exists but is 0 commits ahead of `main`** (empty `git log main..`). Not active WIP — stale/no divergence. Issue #589 = "commit trace before trade" (on-chain provenance causal-ordering, ties to commit-reveal spec).
- **No task target in parent prompt.** Cannot infer single scope. Repo is large, multi-surface (backend / frontend / contracts / docs / infra).
- **Recent merged work (last 15 commits)** clusters around: multi-paper strategy passport (#877/#872), Generate-page redesign (#875), MiniLM semantic retrieval flip off TF-IDF (#874/#878), automated in-app backtest refresh (#876), debate leaderboard return_series (#873), passkey SIWE ERC-1271/6492 smart-account verification (#869/#871). → Active themes: **strategy generation/rigor, semantic retrieval, passkey auth, backtest automation**.
- **Open issues (candidate scope if task targets these):**
  - #882 `[infra]` import_daily_returns.py unknown-stem clobber
  - #881 `[quant]` look_ahead_audit false-positives on `self.datas[N]`
  - #868 `[quant]` Rigor gate 0/31 root cause: degenerate series
  - #864 `[infra]` Backfill strategy_backtest_fixtures in prod
  - #857 `[quant]` Record `universe_source` on passport
  - #854 Dogfooding: Generate blocked for passkey users
  - #834 `[infra]` Feature-flag flip-list + dead-flag audit
  - #827 `[contracts]` Consolidate duplicate AggregatorV3 interface
  - #794/#775 `[oracle]` Chainlink/yfinance price cross-check
  - #788 `[agent-ui]` agent-facing API path
  - #778 `[corpus]` claim-integrity "10,000 papers" manifest-only
  - #777 `[infra]` Postgres 18.3 upgrade

## Constraints (from CLAUDE.md, load-bearing)

- **`main` = deploy branch.** Merge → CI build + auto-deploy to live EC2. Merge-commits only (`gh pr merge <n> --merge`). Short-lived per-owner branch `<handle>/<name>` → PR.
- **Contracts hold live funds:** any `contracts/src/*.sol` change needs **Dan approval + Bogdan (`mnemonik-dev`) two-eyes**. Ask before contract/infra/CI/`.env.example`/`environment.yml`/docker-compose edits.
- **Claims-must-be-true (#1 rule):** no fake rigor badge / cached boolean; live path must back every UI/pitch guarantee.
- **Tests hermetic:** no `.env`/live Redis/Postgres/RPC dependence. No `asyncio.get_event_loop().run_until_complete`; use `asyncio.run`/`async def`. Mock at boundaries. Per-module ≥85% coverage for new tests; repo gate `--cov-fail-under=60` (t2o2 PRs only).
- **Ruff:** line-length 120, rules `I,UP,B,SIM,RUF`. Hard gates: `ruff format --check .` + `ruff check --select E9,F63,F7,F40 .`.
- **Toolchain lives in `archimedes` conda env** (python/pytest/ruff AND node/npm). Not on base PATH: `export PATH="$(conda info --base)/envs/archimedes/bin:$PATH"`.
- **Shell is zsh** — no unquoted-var word-split; quote globs; write scripts to file for complex commands.
- **KB/corpus provenance:** graph surfaces MUST come from real KB pipeline output; do NOT reintroduce deleted `/api/papers/corpus/*` endpoints (#201). Corpus page uses `corpus_routes.py` (503 when no artifact).

## Likely integration points (by future surface)

- **Backend (FastAPI, Python 3.12):** `backend/archimedes/`
  - Routes: `api/*_routes.py` (36 route modules; big ones: `strategies_routes.py` 82K, `risk_routes.py`, `generate_routes.py`, `selection_bias_routes.py`, `vaults_routes.py`, `portfolio_routes.py`). App boots via `main.py`.
  - Services: `services/*` (rigor: `rigor_evaluator.py`, `live_rigor_gate.py`, `_rigor_helpers.py`; fusion: `fusion_evaluator.py`, `_fusion_helpers.py`; retrieval: `paper_rag.py`, `time_aware_retrieval.py`, `embargo_filter.py`; backtest: `portfolio_backtester.py` 49K, `backtest_scheduler.py`, `backtest_repository.py`; regime: `gmm_regime_detector.py`, `vix_regime_detector.py`).
  - Agents: `agents/generation_pipeline.py` (92K), `debate_engine.py`, `portfolio_agent.py`, `strategy_fusion.py`, `strategy_architect.py`.
  - Frozen interfaces: `interfaces/` (Dan owns `IStrategyProvider`). Models: `models/`. DB: `db.py`.
- **On-chain (`chain/`, Dan's lead / Bogdan reviews):** `agent_runner.py` (56K), `executor.py`, `oracle_updater.py`, `trace_publisher.py`, `provenance_publisher.py`, `strategy_publisher.py`, `circle_signer.py`, `client.py`, `pinata_client.py` (IPFS), `v_check.py`.
- **Frontend (React 19 + Vite + viem, `ui/src/`):** entry `App.jsx`/`main.jsx`; 50+ components. Key: `Generate.jsx`, `GenerationStream.jsx`, `StrategyPassport.jsx`, `Strategies.jsx` (40K), `Portfolio.jsx`, `RiskAnalysis.jsx`, `RigorExplainer.jsx`, `WalletConnect.jsx`, `DepositFlow.jsx`, `Leaderboard.jsx`, `CorpusExplorer.jsx`/`CorpusKG.jsx`. API client `ui/src/api.js`; auth `siwe.js`, `circle-wallet.js`, `circle-tx-executor.js`.
- **Contracts (`contracts/src/`, Foundry):** 11 deployed — `Vault.sol` (31K), `VaultFactory.sol`, `SyntheticVault.sol`, `SyntheticFactory.sol`, `SyntheticToken.sol`, `AMMPool.sol`, `AMMRouter.sol`, `AssetRegistry.sol`, `StrategyRegistry.sol`, `PriceOracle.sol` (24K), `ReasoningTraceRegistry.sol`. ABIs cached `contracts/abis/`. Interfaces `contracts/src/interfaces/`.
- **Docs:** `docs/` canonical; product spine `docs/user-stories.md`; specs `docs/specs/`. Roadmap `ARCHIMEDES-ROADMAP-v3.md` (root, uncommitted team artifact).

## Validation commands

```bash
export PATH="$(conda info --base)/envs/archimedes/bin:$PATH"   # env not on base PATH
pytest                                        # backend unit suite (pythonpath=backend, testpaths=backend/tests)
pytest -m "not integration"                   # CI hard-gate subset (no DB/Redis)
pytest --cov=archimedes --cov-report=term-missing
ruff format --check . && ruff check --select E9,F63,F7,F40 .   # hard gates
ruff check .                                  # informational broader lint
cd ui && npm ci && npm run lint               # frontend eslint (npm ci, not install)
cd analytics-engine && uv run pytest          # separate suite
make compile / make test                      # forge build / forge test -vv (contracts)
make up / make pytest / make routes           # docker stack + route inventory
```

Hermetic gate: `env -i HOME=$HOME PATH=$PATH PYTHONPATH=backend python -m pytest backend/tests/test_<mod>.py -q`.

## Start here

Depends on surface once task known:

- Backend/API → `backend/archimedes/main.py` then relevant `api/*_routes.py`.
- Strategy/rigor → `services/rigor_evaluator.py` + `agents/generation_pipeline.py`.
- Frontend → `ui/src/App.jsx` + `ui/src/api.js`.
- Contracts → `contracts/src/` + `docs/specs/`.
- Product intent → `docs/user-stories.md` + `ARCHIMEDES-ROADMAP-v3.md`.

## Clarification questions

1. **What is the actual task?** Parent prompt has no concrete target — cannot scope. Is it one of the open issues above (e.g. #868 rigor-gate 0/31, #854 passkey Generate-block, #881 look_ahead false-positive), or the dormant #589 commit-before-trade branch?
2. **Which surface** — backend / frontend / contracts / docs / infra? Determines review path + whether contract two-eyes gate applies.
3. **Is `dbrowneup/issue-589-commit-before-trade` intended scope?** Branch exists locally but has zero divergence from main — resume it or ignore?
4. **New branch vs continue existing?** If task warrants a PR, need a `<handle>/<name>` branch off fresh `main`.
5. **Any live-path / claim-integrity implication?** If touching rigor/provenance/non-custodial claims, #1 rule applies — need to confirm live-path backing.
