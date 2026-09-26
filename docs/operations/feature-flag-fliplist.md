# Feature-flag flip-list — the go-live checklist

> **status:** current
> **owner:** Dan Browne
> **updated:** 2026-09-03
> **superseded-by:** —

**Scope:** every feature flag in the tree, what it gates, what the committed
deployment config sets it to, and — for the ones still dark — who flips it and
what has to be true first. Tracking issue: [#834](https://github.com/aprin-labs/archimedes/issues/834).

**Why this is a file and not just an issue comment.** #834 has drifted twice.
The 2026-07-05 reconciliation found four live flags missing from the issue; the
2026-08-20 pass found a fifth — `AGENT_DRY_RUN`, the live-signal switch that
decides whether the agent signs real transactions. A checklist that silently
omits a money switch is worse than no checklist, because it is read as complete.
So this page is **enforced**: [`backend/tests/test_feature_flag_fliplist_drift.py`](../../backend/tests/test_feature_flag_fliplist_drift.py)
re-derives the flag inventory from the source tree on every CI run.

**The guard runs in both directions** (the reverse half added 2026-08-31, after
the same night's merge train landed five flags this page had no row for):

- **Forward** — every flag discovered in the tree must appear *somewhere* on
  this page. Extra rows are allowed and encouraged: retired flags, mode
  selectors, and companion tunables are context a reader needs and a scanner
  cannot infer.
- **Reverse** — every flag named in the two **actionable** tables (§ LIVE and
  § FLIP-AT-LAUNCH) must still have a **reader**. A checklist row telling
  someone to flip a name nothing reads is the mirror-image failure: they flip
  it, nothing happens, and the page has lied about being the go-live list.
  Surviving in `.env.example`, a compose file, or terraform does *not* count —
  that is an injection site, and a flag that is only injected is dead. Its row
  belongs in § DEAD / RETIRED, which is exempt, along with § Deployed knobs
  (knobs, not flips). Names listed as *companions* beside a flag (e.g.
  `PREMIUM_MODELS_ALLOWLIST`) are held to the weaker "still exists" bar.

**Reader citations name the reading function, never a line number.** Line
numbers moved under five of this page's rows in a single night's merges
(2026-08-30 → 31), and a stale citation is worse than none — it sends a reader
to an unrelated line and looks authoritative doing it. Same convention, same
reason, as the comment at the top of [`infra/variables.tf`](../../infra/variables.tf).

---

## How to read the "deployed value" column

There is no single place that holds prod's flag values, so each row names the
layer that actually decides:

| Layer | File | Applies to |
|---|---|---|
| ECS task definition | [`infra/ecs.tf`](../../infra/ecs.tf) | The Fargate web/API tier — the live production backend. A change here is a `terraform apply` (new task-def revision + service roll), no image rebuild. **Does not ship on a GitHub deploy.** `deploy.yml` clones the currently registered task definition and retags images; it does not apply terraform. |
| CI task-def rewrite | [`.github/workflows/deploy.yml`](../../.github/workflows/deploy.yml) via [`.github/scripts/ecs_rewrite_task_def.py`](../../.github/scripts/ecs_rewrite_task_def.py) | The path that actually ships. Clones the live task-def, retags images, and can pin env that terraform has not yet applied. `PAPER_ADVANCE_ENABLED` is injected here — `"true"` since 2026-09-01 (#1778) — so a deploy cannot leave the tick to whatever value a cloned last-good revision happened to carry. It also points the backend container's `healthCheck.command` at `/health/ready` and pins `HEALTH_STALE_UNREADY_S=900` (#1818 P3) — since [#1799](https://github.com/aprin-labs/archimedes/issues/1799) put `ignore_changes = [container_definitions]` on `aws_ecs_task_definition.backend`, this script is the ONLY writer of anything inside `containerDefinitions`; the `infra/ecs.tf` lines are documented twins. terraform apply is still required for other `ecs.tf` drift outside `container_definitions`; these pins must not depend on it. |
| Lambda environment | [`infra/cost_kill_switch.tf`](../../infra/cost_kill_switch.tf) and friends | The out-of-band Lambdas (cost kill switch, deploy drift). Terraform spells these as bare `variables { X = "..." }` assignments rather than the ECS `{ name, value }` list — a shape that hid `COST_KILL_SWITCH_DRY_RUN` from this page's guard until 2026-08-31. |
| SSM Parameter Store | seeded by [`infra/scripts/setup-ssm-secrets.sh`](../../infra/scripts/setup-ssm-secrets.sh) | Secrets and the agent-runner switches. Not readable from this repo — verify with `aws ssm get-parameter`. |
| Compose | [`docker-compose.yml`](../../docker-compose.yml) / [`docker-compose.production.yml`](../../docker-compose.production.yml) | Local dev and the legacy EC2 box. |
| Code default | the `os.getenv(...)` call itself | Anything not set by a layer above. Most flags fall through to here — the code default *is* the production value. |
| GitHub repo variable | `vars.*` in `.github/workflows/` | CI/CD gates. Not readable from the tree — `gh variable list`. |
| Vite build arg | [`nginx/Dockerfile`](../../nginx/Dockerfile) | Frontend flags. Baked into the bundle at build time. **See the finding in § Audit findings — most UI flags have no build-arg wired, so they are pinned at their code default in every deployed image.** |

Where a row says *unset → code default*, that is a claim about the committed
config, verified by grep against `infra/`, `docker-compose*.yml`, and
`.github/workflows/`. It is **not** a claim about a hand-edited `/opt/archimedes/.env`
on the legacy box; that file is not in version control and cannot be verified
from here.

---

## LIVE — flags gating something today

| Flag | Reader | Deployed value | What it gates |
|---|---|---|---|
| `GENERATION_PAYMENT_REQUIRED` | `payment_required()` — [`services/generation_payment.py`](../../backend/archimedes/services/generation_payment.py) | `"true"` in `infra/ecs.tf` | The x402 paywall + wallet-link precondition on `POST /api/generate/start`. **Flipped on 2026-08-20** at `GENERATION_PRICE_USD="2.00"`, recipient `GENERATION_PAYMENT_RECIPIENT` (TF var, #1414). Only the literal `"true"` enables. Since #1643 the free allowance sits *above* this gate — the first `FREE_GENERATIONS_PER_ACCOUNT` runs on a verified account never reach it. |
| `GENERATION_PAYMENTS_DRY_RUN` | `_payments_dry_run()` — [`services/generation_payment.py`](../../backend/archimedes/services/generation_payment.py) | `"false"` in `infra/ecs.tf` | The generation-scoped settlement switch (#1428 split). `"false"` = the generation rail settles for real on the caller-signed metered-API path. Unset inherits `PAYMENTS_DRY_RUN`. |
| `PAPER_TRADING` | the `paper_trading` startup read in [`main.py`](../../backend/archimedes/main.py) (marketplace-engine construction) | `"true"` in `infra/ecs.tf` | Marketplace engine executes simulated fills instead of on-chain trades. |
| `PAPER_ADVANCE_ENABLED` | `advance_enabled()` — [`services/paper_trading.py`](../../backend/archimedes/services/paper_trading.py), checked once per tick inside `paper_advance_loop` | **pinned `"true"` in `infra/ecs.tf` AND injected `"true"` by the CI task-def rewrite** (armed 2026-09-01, [#1778](https://github.com/aprin-labs/archimedes/pull/1778)) — **the code default stays `"false"`**: two of the three sites moved, deliberately. Unset must still not tick, because task-def :211 (#1725 image) cloned last-good, the name was absent, the then-ON code default started the tick, and `/health` 502'd at `PAPER_ADVANCE_STARTUP_DELAY_S`. The CI rewrite is the path that ships; the terraform line is its documentation twin, kept equal so a future apply cannot silently disagree; the code default is the last line if both pins go missing. | **Break-glass kill switch, currently ARMED — the tick runs ([#1632](https://github.com/aprin-labs/archimedes/issues/1632) lift).** Gates the daily paper-advance tick. Off means **paper ledgers do not advance**: track records freeze and the deployment payload keeps serving its last mark. That is a live product claim suspended, not a dormant switch — which is why it was pulled on 2026-08-31, when web-tier tasks were dying with `Fatal Python error: Aborted` and this tick was the suspect. **The cause that was actually found is a different one:** two `/health` corpus-probe threads racing in SQLAlchemy session teardown inside `load_corpus`, caught by faulthandler on prod rev 214 *with this flag off*, fixed by [#1740](https://github.com/aprin-labs/archimedes/pull/1740) (the dual-OpenSSL hypothesis was falsified by the same traceback). The replay's own OHLCV cache write in [`services/market_data_provider.py`](../../backend/archimedes/services/market_data_provider.py) — the frame the first attribution named — was never the proven cause and was never cleared either, so arming this flag is **not** a claim that the tick's frame is clean. What makes arming defensible is the #1728 process boundary: the web process **refuses to run `paper_advance_loop` in-process** (`arm_paper_advance_for_web_tier`) and spawns a child interpreter, so a residual C-level abort kills the child while `/health`, in the parent, keeps answering. One ticker per fleet is enforced inside that child by a Postgres advisory lock around the cycle (#1778), and the lifespan cancels the arming task at shutdown so a draining task's child stops. **To pull it back:** set `PAPER_ADVANCE_VALUE = "false"` in [`.github/scripts/ecs_rewrite_task_def.py`](../../.github/scripts/ecs_rewrite_task_def.py) *and* the twin line in [`infra/ecs.tf`](../../infra/ecs.tf), then deploy — a terraform apply alone is overwritten by the next CI deploy. Only the falsy literals (`0`/`false`/`no`/`off`) disable; only an explicit non-falsy value enables; unset falls through to the code default `"false"`. |
| `FEATURE_QUANT` | `resolve_feature_flags()` — [`feature_flags.py`](../../backend/archimedes/feature_flags.py) | `"false"` in `infra/ecs.tf`; unset elsewhere → ON outside production (`APP_ENV != prod`) | The quant lab surfaces: `require_quant_feature()` on [`api/risk_routes.py`](../../backend/archimedes/api/risk_routes.py) and [`api/portfolio_routes.py`](../../backend/archimedes/api/portfolio_routes.py) 404s them when off. `GET /api/features` reports the resolved value to the UI ([`ui/src/features.js`](../../ui/src/features.js)). Deliberately off in prod. |
| `ENABLE_API_DOCS` | the module-level `_enable_docs` gate in [`main.py`](../../backend/archimedes/main.py) | unset → code default (off) | Re-enables `/docs` + `/openapi.json`, which are suppressed whenever `PUBLIC_DOMAIN` is set. Leave unset in prod. |
| `PAYMENTS_HALT` | `payments_halted()` — [`marketplace/config.py`](../../backend/archimedes/marketplace/config.py) | unset → code default `"false"` (not halted) | **Break-glass kill switch** for marketplace money movement. Distinct from `PAYMENTS_DRY_RUN` (posture) and `PAPER_TRADING` (execution) — this is the "stop now" lever. |
| `COST_KILL_SWITCH_DRY_RUN` | the module-level `DRY_RUN` constant in [`infra/lambda/cost_kill_switch/index.py`](../../infra/lambda/cost_kill_switch/index.py) | **pinned `"false"`** in [`infra/cost_kill_switch.tf`](../../infra/cost_kill_switch.tf) | **Break-glass kill switch, armed** (#1649). `"true"` puts the AWS budget-ladder Lambda in rehearsal mode: it still alarms and still notifies, but never scales the ECS service to zero or stops the runner. **Looks inert, is not** — a kill switch left in rehearsal is a kill switch that does not exist, and nobody notices until the month it mattered. The pin is guarded in both directions by [`backend/tests/test_cost_kill_switch_guards.py`](../../backend/tests/test_cost_kill_switch_guards.py). Procedure: [`docs/runbooks/cost-kill-switch.md`](../runbooks/cost-kill-switch.md). |
| `ARCHIMEDES_FUSION_REAL_DATA` | `real_data_enabled()` — [`services/fusion_market_data.py`](../../backend/archimedes/services/fusion_market_data.py) | unset → code default ON | Kill switch for live market-data fetch inside fusion; `0`/`false`/`no`/`off` falls back to stubs. |
| `FUSION_SEMANTIC_RETRIEVAL` | `_semantic_enabled()` — [`services/paper_rag.py`](../../backend/archimedes/services/paper_rag.py) | unset → code default `"true"` | Reranking in fusion candidate selection. Note: the corpus has **no embedding column** — retrieval is lexical (#778 open), so this flag does not turn on semantic search. |
| `REQUEST_PATH_WARMUP` | `warmup_enabled()` — [`services/request_path_warmup.py`](../../backend/archimedes/services/request_path_warmup.py) | unset → code default `"true"` (ON). Not in `infra/ecs.tf`; not on the CI rewrite pin. Only the falsy literals (`0`/`false`/`no`/`off`) disable. Forced off under `TESTING`. | **LIVE on the warmup path ([#1713](https://github.com/aprin-labs/archimedes/issues/1713)).** Primes cohort daily returns, the Library list's stored-passport read, and the selection-bias gate rigor memo in the FastAPI lifespan before `yield`, so a new Fargate task cannot become an ALB target with cold caches. The list path no longer runs a live gate (verdict-of-record); the remaining live-gate cold path is `/api/selection-bias/gate`. Off is an emergency kill switch: `REQUEST_PATH_WARMUP=0` serves cold (the pre-#1713 behaviour) without a rollback. Not FLIP-AT-LAUNCH — production wants it ON. A timed-out warmup refuses to listen rather than joining cold. |
| `PAPER_TRACE_PUBLISH` | `publishing_enabled()` — [`services/paper_trace.py`](../../backend/archimedes/services/paper_trace.py) (env name held in `_PUBLISH_ENV`) | unset → code default ON | Paper-trace publishing. Turning it off is loud by design: the decision still gets a durable `disabled` row and `trace_coverage.status` reports `disabled`. |
| `VITE_GENERATION_QUOTE_ENABLED` | `GENERATION_QUOTE_ENABLED` — [`ui/src/featureFlags.js`](../../ui/src/featureFlags.js) | no build arg → code default ON (only the literal `"false"` suppresses) | The upfront cost-quote + x402 paywall step on the Generate page (#1296). |
| `DEPLOY_ENABLED` | [`.github/workflows/deploy.yml`](../../.github/workflows/deploy.yml) | GitHub repo variable — `gh variable list` | Gates the build-and-push / migrate / deploy-ecs jobs. Must be `"true"` for a merge to `main` to reach production. |
| `GENERATION_PIPELINE_FIXTURE` | `_llm_available()` and the fixture branches in [`agents/generation_pipeline.py`](../../backend/archimedes/agents/generation_pipeline.py) | unset everywhere | **Test-only.** Forces the deterministic fixture pipeline (no LLM, no network). Must never be set in any deployed environment — setting it would serve canned strategies as real output. |
| `GENERATION_PIPELINE_SKIP_BACKTEST` | the same backtest-leg branch in [`agents/generation_pipeline.py`](../../backend/archimedes/agents/generation_pipeline.py) | unset everywhere | **Test-only.** Skips the backtest leg for hermetic tests. Same warning as above: a generated strategy with no backtest cannot be gated. |
| `BOOTSTRAP_ALLOW_LEGACY_VAULTS` | [`scripts/bootstrap_vaults.py`](../../backend/archimedes/scripts/bootstrap_vaults.py) | unset (operator-supplied, `=1`) | Operator escape hatch in the one-shot vault bootstrap script — lets it proceed against pre-redeploy vault addresses. Not a runtime flag. |
| `ALLOW_LEGACY_HTTPS_SETUP` | [`infra/scripts/setup-https.sh`](../../infra/scripts/setup-https.sh) | unset (operator-supplied, `=1`) | Refuse-by-default guard on the **deprecated** in-instance certbot script. Live TLS is CloudFront + ACM; the script is retained for reference only. The flag exists so a stray `bash -s` cannot fire it. Do not set it against prod. |

---

## FLIP-AT-LAUNCH — the checklist

Each row is a flip someone has to perform, with the precondition that makes it
safe. **Owner is Dan** unless stated; nothing here flips in a drive-by PR.

| # | Flag | Current | Flip mechanics | Preconditions before flipping |
|---|---|---|---|---|
| 1 | `AGENT_DRY_RUN` | `"true"` in SSM (`/archimedes/prod/AGENT_DRY_RUN`, SecureString) | SSM parameter edit + agent-runner restart | Dan's explicit live-money go-ahead, after interpreter-parity (F2/F3) is merged and deployed so live behaviour matches the backtests being trusted. **Danger:** [`chain/agent_runner.py`](../../backend/archimedes/chain/agent_runner.py)'s module-level `DRY_RUN` resolves an *unset* value to `"false"` = LIVE signing. `setup-ssm-secrets.sh` refuses to `--apply` without an explicit recognised value for exactly this reason — do not "fix" that refusal. |
| 2 | `PAYMENTS_DRY_RUN` | `"true"` in `infra/ecs.tf` | task-definition env → `terraform apply` | [#975](https://github.com/aprin-labs/archimedes/issues/975) (non-custodial fee-custody migration). The custodial-INTERIM decision holds it at `"true"`; the generation rail already settles for real via `GENERATION_PAYMENTS_DRY_RUN="false"`, which is the whole scope the ecs.tf comment permits. Marketplace sweeps/withdraws stay blocked. |
| 3 | `EMAIL_VERIFICATION_ENFORCED` | `"false"` in `infra/ecs.tf` (auth container) | task-definition env → `terraform apply`, no image change | **The old precondition is met — SES production access is live.** The pre-flip audit ([#1695](https://github.com/aprin-labs/archimedes/pull/1695), closing #1693) replaced it with a named list; runbook [`docs/runbooks/email-verification-validation.md`](../runbooks/email-verification-validation.md). Still open: **EV-4** (a completed password reset proves mailbox control but does not set `email_verified`, so enforcement 403s the user who just reset — owner decision, not a bug), **EV-5** (no SES bounce/complaint telemetry, and production access is *kept* on bounce < 5% / complaint < 0.1%). **EV-6** (an agent cannot click an inbox link) was the flip blocker and is *plausibly closed* by #1698's scoped API keys + #1705's CLI, which authenticate without the gated `sign-in/email` handler — **Dan to confirm** before flipping. EV-1 (global rate-limit buckets) closed via #1691/#1709. Reader: `emailVerificationEnforced()` — [`auth/auth.js`](../../auth/auth.js); only the literal `'true'` enforces, and it gates exactly one line, the credential sign-in handler. Second-order effect: `free_generations.locked_reason()` already refuses the free allowance to an unverified account, so verification is load-bearing for the free path *today*, flag or no flag. |
| 4 | `REVENUE_SWEEP_ENABLED` | not set in `infra/ecs.tf` → code default off | add `{ name = "REVENUE_SWEEP_ENABLED", value = "true" }` to ecs.tf → `terraform apply` | The recipient DCW must hold gas — **USDC is gas on Arc** and a freshly created DCW has none, so the first sweep fails without funding. `REVENUE_WALLET_ID` is already pinned in ecs.tf. Reader: `sweep_enabled()` — [`services/revenue_sweep.py`](../../backend/archimedes/services/revenue_sweep.py), started from the revenue-sweep block in [`main.py`](../../backend/archimedes/main.py)'s startup. Only the literal `"true"` enables. |
| 5 | `PAPER_TRACE_ANCHOR` | unset → code default OFF | env value wherever the paper-trace worker runs | On-chain `reveal()` writes `portfolio_before`/`portfolio_after` — the user's holdings — **permanently**. Two independent gates by design (this flag *and* per-deployment `PaperDeployment.anchor_traces`); do not collapse them. Nothing on-chain can be recalled, so consent is forward-only. Reader: `anchoring_enabled()` — [`services/paper_trace.py`](../../backend/archimedes/services/paper_trace.py). |
| 6 | `KB_PIPELINE_ENABLED` | unset (deliberately, per [`infra/kb_runner.tf`](../../infra/kb_runner.tf)) | env on the scheduled kb-runner task | [#778](https://github.com/aprin-labs/archimedes/issues/778) / #1090 / #1092. Setting it today does **not** produce a KB: [`scripts/run_kb_pipeline.py`](../../backend/archimedes/scripts/run_kb_pipeline.py) raises "the full pipeline invocation is not yet wired". The task is also sized for skip-mode (see `variables.tf`) — flipping without resizing is a second failure. |
| 7 | `PREMIUM_MODELS_ENABLED` (+ `PREMIUM_MODELS_ALLOWLIST`) | unset → off | env value | AWS Bedrock Anthropic use-case activation (a console action, no PR). **Explicitly deprioritised by Dan 2026-07-04** — not a near-term flip. Reader: `_premium_globally_enabled()` — [`services/model_gate.py`](../../backend/archimedes/services/model_gate.py); the allowlist is an independent per-wallet path that works with the global flag off. |
| 8 | `VITE_ROADMAP_SURFACES` | `false` in `ui/.env.example`; **no build arg in `nginx/Dockerfile`** → pinned OFF in every built image | see audit finding A1 — needs a Dockerfile `ARG`/`ENV` + a `--build-arg` in `deploy.yml` before it can be flipped at all | [#1266](https://github.com/aprin-labs/archimedes/issues/1266) — vaults (Portfolio + vault-detail), Marketplace (+ market-strategy), Publish, Subscriptions, Learnings return to scope. **Load-bearing while off:** CLAUDE.md's roadmap-tense rule and [`ui/test/roadmap-copy.test.js`](../../ui/test/roadmap-copy.test.js) both depend on this gate holding those surfaces out of the shipped nav. Not a dead flag — a claim-integrity gate. |
| 9 | `VITE_KNOWLEDGE_GRAPH_TAB` | `false` in `ui/.env.example`; **no build arg** → pinned OFF in every built image | same as row 8 | #1090 (KB pipeline artifact) + #1092 (Postgres backfill). `kg_entities`/`kg_relations` are 0 rows today, so the tab would offer an empty capability. |
| 10 | `RUNNER_DEPLOY_ENABLED` | GitHub repo variable, unset → both jobs skip | repo variable → `true` | The runner infrastructure must exist first (#1065 step 2 `terraform apply` + step 3 on-chain verify). [`deploy-runners.yml`](../../.github/workflows/deploy-runners.yml) has a third layer of runtime existence checks, so an early flip no-ops loudly rather than erroring — but it is still an early flip. |
| 11 | `TF_DRIFT_ENABLED` | GitHub repo variable, unset → the [`terraform-drift.yml`](../../.github/workflows/terraform-drift.yml) job skips | repo variable → `true` | Not tied to launch — tied to one AWS operation. The read-only plan role must exist first (`infra/scripts/setup-github-plan-role.sh --apply`), plus the `TF_PLAN_ROLE_ARN` variable and the `TF_VAR_ALARM_EMAIL` secret. Flipping early makes the job red on every `infra/**` PR at the OIDC step, which is how a gate gets ignored. The gate is advisory and never a required check. Full procedure: [`../runbooks/terraform-apply-and-task-definition-ownership.md`](../runbooks/terraform-apply-and-task-definition-ownership.md). |

---

## Deployed knobs — a value someone chose, not a switch someone flips

Not flips, and deliberately outside the reverse guard's actionable scope. They
are here because the 2026-08-30/31 merge train pinned four of them into the prod
task definition for the first time, and **a number a human wrote into `ecs.tf`
is a decision on the deploy path** — which is the same reason a flag belongs on
this page. Each names the value that turns the behaviour *off*, because that is
the part an operator reaches for under pressure.

| Knob | Reader | Deployed value + provenance | Off-value / clamp |
|---|---|---|---|
| `GENERATION_MAX_CONCURRENT` | `_max_concurrent_generations()` — [`api/generate_routes.py`](../../backend/archimedes/api/generate_routes.py) | `var.generation_max_concurrent` = `"1"` (`infra/ecs.tf` ← [`infra/variables.tf`](../../infra/variables.tf), #1686) | Floored at 1 — **cannot be disabled**. A generation averages ~65% of the task's vCPU for ~48 s, so unbounded parallelism starves auth/SSE/the ALB health check. |
| `GENERATION_MAX_QUEUE` | `_max_queued_generations()` — same file | `var.generation_max_queue` = `"10"` (#1686) | `0` disables queueing — `/start` then refuses 429 immediately, *before* the payment gate, so nobody is charged for a slot that does not exist. |
| `DEBATE_POOL_MAX` | `_pool_max()` — [`agents/debate_engine.py`](../../backend/archimedes/agents/debate_engine.py) | `var.debate_pool_max` = `"10"` (#1686) | Clamped to `[2, len(_STEERS)]` (18 today) — **cannot be disabled**. This is the debate's cost lever: the critics and backtests cost zero tokens, the proposer fan-out is the N× spend. |
| `GENERATION_TIMEOUT_SECONDS` | `_generation_timeout_seconds()` — [`api/generate_routes.py`](../../backend/archimedes/api/generate_routes.py) | `"300"` literal in `infra/ecs.tf` (#1692); code default 600 | **Fail-soft, so mistypes are silent:** `"300s"`, `"5m"`, `"0"` and `"-1"` all fall back to 600 without complaint. `inf` is the one intended escape hatch. Keep it a bare positive number; pinned by [`test_ecs_generation_timeout.py`](../../backend/tests/test_ecs_generation_timeout.py). |
| `GENERATION_DAILY_CAP_PER_USER` / `_PER_IP` | `user_daily_cap()` / `ip_daily_cap()` — [`services/generation_quota.py`](../../backend/archimedes/services/generation_quota.py) | `"100"` / `"200"` in `infra/ecs.tf` | `<= 0` disables that layer. Both layers must pass. |
| `PUBLIC_TRACE_VAULTS` | `trace_visibility.py`'s `_PUBLIC_VAULTS_ENV`, falling back to `AGENT_VAULT_ADDRESSES` — [`services/trace_visibility.py`](../../backend/archimedes/services/trace_visibility.py) | `var.public_trace_vaults` in `infra/ecs.tf`; empty default → **unarmed** | Unarmed is the *wider* setting: every unowned trace is house-public. Arming it narrows visibility to the listed vaults. Counter-intuitive direction — check it before assuming an empty value is the safe one. |
| `FREE_GENERATIONS_PER_ACCOUNT` | `allowance()` — [`services/free_generations.py`](../../backend/archimedes/services/free_generations.py) | `"3"` pinned on **both** task-definition paths (owner, 2026-09-02, deck Q3): the literal in `infra/ecs.tf` and `FREE_GENERATIONS_VALUE` in [`.github/scripts/ecs_rewrite_task_def.py`](../../.github/scripts/ecs_rewrite_task_def.py), which is the one that actually ships (`deploy.yml` clones the live task-def and never applies terraform). Equal to the code default, so the pin was plumbing-only. Change **both** or the next CI deploy overwrites you. Guards: [`test_ecs_free_generations_pin.py`](../../backend/tests/test_ecs_free_generations_pin.py), [`test_ecs_backend_secrets.py`](../../backend/tests/test_ecs_backend_secrets.py). Closes A5. | `<= 0` disables the free path entirely, and does so *without dangling a carrot* — `locked_reason()` returns `None` rather than a lock reason. Sits above `GENERATION_PAYMENT_REQUIRED`: the allowance is spendable only on a **verified** account (`LOCK_EMAIL_UNVERIFIED`). #1643/#1658. |
| `DEBATE_BACKTEST_WORKERS` | `_backtest_max_workers()` — [`agents/debate_engine.py`](../../backend/archimedes/agents/debate_engine.py) | not deployed → code default `2` | Clamped `[1, 2]`. **The clamp is the point:** the knob can make the dedicated backtest pool narrower, never wider, so an operator typo cannot reintroduce the GIL-contention fan-out #1689 removed. |
| `SERVER_THREAD_POOL_WORKERS` | `_default_executor_workers()` — [`main.py`](../../backend/archimedes/main.py) | not deployed → `0` = auto | `0`/unset picks `min(32, max(16, cpu_count*4))`; a positive value overrides, capped at 64. Floor of 16 exists because one generation parks up to `DEBATE_POOL_MAX` IO-bound proposer threads on this pool (#1689). |
| `TIINGO_MIN_REQUEST_INTERVAL_S` | `_tiingo_min_request_interval_s()` — [`services/market_data_provider.py`](../../backend/archimedes/services/market_data_provider.py) | commented out in `.env.example` → code default `1.1` s | `0` disables client-side pacing entirely, leaving only Tiingo's own HTTP 429. Negative or unparseable values log a warning and fall back to 1.1 (#1627). |

---

## DEAD / RETIRED — no reader in the tree

Exempt from the reverse guard: naming a name that is *gone* is what this
section is for, and forcing it out would delete the history that stops a
retired flag being re-added.

**That exemption is now paid for, not assumed** ([#1824](https://github.com/aprin-labs/archimedes/issues/1824)).
It left two holes the drift guard cannot see: a dead name gaining a reader, and
a dead name being promoted back onto an actionable table — both pass every
other check on this page. [`backend/tests/test_dead_flags_stay_dead.py`](../../backend/tests/test_dead_flags_stay_dead.py)
closes them for the eight names #1824 classified DEAD (`DOCS_SITE_ENABLED`,
`X402_WEBHOOK_SECRET`, `ARCHIMEDES_X402_ENABLED`, `MAX_USDC_PER_DAY`,
`ARCHIMEDES_DEBATE_ENABLED`, `REQUIRE_SIWE_FOR_GENERATION`,
`ARCHIMEDES_TRACE_PIN_ENABLED`, `PINATA_JWT`). It goes red on:

- an env read or an env-name constant under the runtime source roots — **prose
  is exempt**, so `debate_engine.py`'s two doc-comments are the record they were
  always meant to be, and only a line that can *act* trips it;
- **any** mention on a deploy-config surface — `infra/`, `.github/scripts`,
  `.github/workflows`, `docker-compose*.yml` and all three `.env.example`
  templates (root, `ui/`, `backend/`) — commented-out lines included, because a
  commented template entry reads as "a secret you still need to generate",
  which is how `X402_WEBHOOK_SECRET` outlived its own removal. `.github/workflows`
  is on that list because it is the only surface `DOCS_SITE_ENABLED` ever lived
  on, and a companion assertion keeps the set a superset of the one
  `test_fusion_flag_retired.py` watches for its own name;
- a mention anywhere on this page outside § DEAD / RETIRED and § Audit
  findings — a section nobody has classified counts as actionable;
- **an emptied row.** The positive half: deleting a name from the table below is
  how a retired flag comes back through the exempt door, so all eight must still
  be named here.

`ARCHIMEDES_FUSION_ENABLED` and the four `BACKTEST_REFRESH_*` names are held by
their own, stricter guards instead ([`test_fusion_flag_retired.py`](../../backend/tests/test_fusion_flag_retired.py),
which also bans a *renamed* fusion switch on the generation path, and
[`test_ecs_paper_advance_deploy_pin.py`](../../backend/tests/test_ecs_paper_advance_deploy_pin.py)) —
all five are named in `.github/scripts/ecs_rewrite_task_def.py` on purpose, as
the lines that strip them.

| Name | Where it still appears | Status |
|---|---|---|
| `DOCS_SITE_ENABLED` | nowhere | **RETIRED** by [#1634](https://github.com/aprin-labs/archimedes/issues/1634). It gated a GitHub Pages deploy that never went live. The docs site is now served from our own S3 + CloudFront ([`docs-site/infra/main.tf`](../../docs-site/infra/main.tf)) and the publish job has no variable gate: the bucket's existence is the gate, so an unapplied stack prints a NO-OP notice instead of needing someone to remember a switch. Procedure: [`docs/runbooks/docs-site-setup.md`](../runbooks/docs-site-setup.md). If the variable was ever created in repo Settings, delete it — it now reads as a live switch that does nothing. |
| `X402_WEBHOOK_SECRET` | nowhere | **REMOVED 2026-08-31** by this page's own audit. It was an HMAC secret for `POST /api/marketplace/payment-webhook` — a route that was never implemented (`backend/tests/api/test_marketplace_routes.py` says so in a comment) and never will be: the marketplace shipped via #958's x402 rail, not a gateway callback. It survived only as a commented-out `.env.example` entry, which read as "a secret you still need to generate". |
| `ARCHIMEDES_X402_ENABLED`, `MAX_USDC_PER_DAY` | nowhere | **GONE.** The marketplace shipped via #958, not #749/#824 (both closed unmerged). Superseded by `PAYMENTS_DRY_RUN`. |
| `ARCHIMEDES_DEBATE_ENABLED` | two explanatory doc-comments in [`agents/debate_engine.py`](../../backend/archimedes/agents/debate_engine.py) | **RETIRED** by #880; the last executable residue — two `monkeypatch.delenv` calls clearing a variable nothing can read — was removed 2026-08-31. The surviving mentions are prose that says *why* `_debate_can_run` has no flag check, which is the thing that stops it being re-added. The society is the unconditional sole pipeline; `_pick_pipeline` returns `debate` regardless. |
| `ARCHIMEDES_FUSION_ENABLED` | nowhere — the reader, the OFF branch, the `ecs.tf` pin, both compose defaults and `infra/spike-1411/function-env.txt` all went in the same commit | **RETIRED 2026-09-02** (deck Q4). It was never a lever. The debate society is the sole generation pipeline ([`adr/debate-society-sole-generation-pipeline.md`](../adr/debate-society-sole-generation-pipeline.md)) and every proposer routes through `StrategyFusion.propose()` ([`adr/fusion-primary-generation.md`](../adr/fusion-primary-generation.md)), so OFF returned a `disabled` sentinel and Generate silently produced nothing; every deployed environment pinned it `"true"` while the *code* default was OFF — finding A3's footgun. Fusion is now unconditional and there is no switch to flip. [`backend/tests/test_fusion_flag_retired.py`](../../backend/tests/test_fusion_flag_retired.py) fails if the name, or a renamed fusion switch on the generation path, comes back. `/health` published a companion `fusion_enabled` field; it was **dropped on 2026-09-03** by owner decision rather than frozen at a constant `true` — a search of backend, ui, cli, mcp-server, docs, scripts and tests found no consumer beyond the contract test's own pinned field set, and a constant wearing a health signal's clothes is the claim-integrity failure the neighbouring `/health` fields exist to avoid. `test_health_always_answers.py::TestNoReportedFieldWasDropped::test_fusion_enabled_is_not_published` now pins the key ABSENT. **No operator action — the deploy strips the pin** ([#1824](https://github.com/aprin-labs/archimedes/issues/1824), 2026-09-03). The residue this row used to describe was real: CI deploys clone the live task definition, which still carries `ARCHIMEDES_FUSION_ENABLED=true` on the `backend` container, so the now-inert name rode forward on every revision. It is now the fifth entry in `RETIRED_BACKEND_ENV` in [`.github/scripts/ecs_rewrite_task_def.py`](../../.github/scripts/ecs_rewrite_task_def.py) — the same treatment `BACKTEST_REFRESH_ENABLED` got, and the option #1797 named — so **the first deploy after this merge registers the revision that drops it**, with no `terraform apply` and no operator ritual. Stripping it is a no-op on behaviour by construction: the reader-ban above proves nothing reads the name. Guarded by `test_fusion_flag_retired.py::test_the_deploy_strips_the_stale_pin_off_the_live_task_definition`. |
| `REQUIRE_SIWE_FOR_GENERATION` | test docstrings, and one live assertion in `test_local_mode_contract.py` that the name does **not** reappear | **DELETED** by #1300. The flag, `gate_generation`, and `_generation_auth_required` are gone; generation/chat auth is unconditional. Setting it is a no-op. |
| `ARCHIMEDES_TRACE_PIN_ENABLED` | [`docs/specs/ipfs-reasoning-traces-design-note.md`](../specs/ipfs-reasoning-traces-design-note.md) only | **NEVER IMPLEMENTED.** A proposed flag in a design note, not code. Named here so a reader who greps the docs does not go looking for the gate. |
| `PINATA_JWT` | historical docs + this row | **REMOVED 2026-09-01** (#1526). The pin client shipped as code but the JWT was never in the backend Fargate `secrets{}` block, so prod never pinned. Outcome (b): delete the client rather than half-wire a secret nobody can seed here. Re-enabling is an owner action (SSM + `ecs.tf` + rebuilt client) — [`adr/ipfs-pinning-not-live.md`](../adr/ipfs-pinning-not-live.md). |
| `BACKTEST_REFRESH_ENABLED`, `BACKTEST_REFRESH_INTERVAL_HOURS`, `BACKTEST_MAX_AGE_HOURS`, `BACKTEST_REFRESH_STARTUP_DELAY_S` | **the live ECS task definition** (`archimedes-backend:216`+ still carries `BACKTEST_REFRESH_ENABLED=false` on the `backend` container), plus historical prose in [`../sprint/a6-rerun.md`](../sprint/a6-rerun.md) and [`../sprint/cluster-0-unblock.md`](../sprint/cluster-0-unblock.md) | **RETIRED 2026-09-01** by [#1760](https://github.com/aprin-labs/archimedes/issues/1760). They were the knobs of `services/backtest_scheduler.py`, whose boot-time + age-driven refresh re-ran the whole curated library **in the web process** at +180 s on every cold boot, pegged the 1-vCPU task (`CpuUtilized` 972/1024) and got tasks killed by their own 5 s container health check during the 2026-09-01 deploy. The module and its lifespan wiring are deleted; a curated backtest is now produced only by an explicit operator run ([`../runbooks/curated-backtests.md`](../runbooks/curated-backtests.md)), and a generated one exactly once at generation. Policy: [`../adr/backtests-are-frozen-evidence.md`](../adr/backtests-are-frozen-evidence.md). **No operator action — the deploy strips the pin.** The `=false` on the task definition was the 2026-09-01 mitigation (task-def 216 = 215 + this env) and it worked, but nothing reads the name any more, and CI deploys **clone the live task-def** forward. As of this PR, `.github/scripts/ecs_rewrite_task_def.py` deletes every name in its `RETIRED_BACKEND_ENV` tuple (all four of these) from the `backend` container before registering, logging what it removed; **the first deploy after this merge registers the revision that drops them**, and every deploy after that is a no-op. The strip is guarded by `backend/tests/test_ecs_paper_advance_deploy_pin.py::TestRewriteStripsRetiredBackendEnv`, which also fails if any of these names gains a reader under `backend/archimedes` while it is still on the retired tuple. They remain absent from `infra/ecs.tf`. |
| `chainlink_covered` / `chainlink_covered_synths()` | two prose mentions only: a docstring in [`backend/archimedes/scripts/generate_universe.py`](../../backend/archimedes/scripts/generate_universe.py) ("replaces the misleading `chainlink_covered`") and a historical comment in [`backend/tests/test_asset_universe_doc.py`](../../backend/tests/test_asset_universe_doc.py) | **ALREADY REMOVED.** Not a flag; #834's audit section listed the `universe.py` `SyntheticSpec` field as ready for cleanup, and re-grepping on 2026-08-31 finds it gone from [`backend/archimedes/universe.py`](../../backend/archimedes/universe.py) entirely — both surviving mentions are comments naming it as the *superseded* schema. Strike the action item. |

### Looks dead, is load-bearing — do not remove

Every name here has an unreachable-looking branch, a permanently-one-valued
pin, or no deployment at all. Each one is deliberate.

| Name | Why it survives the audit |
|---|---|
| `COST_KILL_SWITCH_DRY_RUN` | Permanently pinned `"false"`, so the `True` branch never executes in prod — which is exactly the state a kill switch is supposed to be in. Removing the flag removes the rehearsal mode the runbook depends on for testing the ladder without scaling prod to zero. |
| `PAYMENTS_HALT` | Never set anywhere. It is a break-glass lever whose whole value is being unset until the day it is not. |
| `AGENT_DRY_RUN` | Reads as a stale `"true"` in SSM. It is *the* live-money switch, and its unset value is LIVE — the most dangerous name on this page. |
| `ROADMAP_SURFACES_ENABLED` / `VITE_ROADMAP_SURFACES` | Permanently `false` in every built image, which makes it look like a constant. It is the gate that keeps not-yet-shipped vault/marketplace surfaces out of the product, and CLAUDE.md's claims-must-be-true rule plus `ui/test/roadmap-copy.test.js` both lean on it. |
| `GENERATION_PIPELINE_FIXTURE` / `GENERATION_PIPELINE_SKIP_BACKTEST` | Unset in every deployed environment. They are the hermetic-test escape hatches; deleting them costs the suite its no-LLM path. |
| `ALLOW_LEGACY_HTTPS_SETUP` / `BOOTSTRAP_ALLOW_LEGACY_VAULTS` | Refuse-by-default guards on scripts that must not fire accidentally. The flag *is* the guard — removing it makes the script runnable, which is the failure it was added to prevent. |

---

## Not flags — companions, mode selectors, and tunables

These are read by the same code paths and get confused for flags. They are
documented here so the flip rows above make sense; the drift guard does not
require them.

| Name | Kind | Notes |
|---|---|---|
| `PRICE_SOURCE` | mode selector | `"cascade"` in `infra/ecs.tf` (flipped 2026-07-04, Pyth live). Code default stays `"yfinance"` for local/CI safety. Reader: `price_source_mode()` — [`services/price_source.py`](../../backend/archimedes/services/price_source.py). |
| `PRICE_CROSSCHECK_BAND_BPS` / `PRICE_CROSSCHECK_MAX_STALENESS_SECONDS` | tunable | 5000 bps (50%) / 345600 s (4 d). Only bite while `PRICE_SOURCE=cascade`. Band tuning owned by Önder. |
| `MARKET_DATA_PROVIDER` | mode selector | Default `"yfinance"`. Since #1798 this names the **intraday/history** seam's vendor — the live oracle push, the paper-marks loop, the #775 cross-check and the Explore history modal — and it is also the daily seam's fallback when `MARKET_DATA_DAILY_PROVIDER` is unset (back-compat: the pre-#1798 single-variable flip still moves daily bars). Setting it to `tiingo` does **not** move intraday: Tiingo declares the daily seam only (`_VENDOR_SEAMS`), so the intraday seam logs the substitution and stays on yfinance. Reader: `provider_name("intraday")` — [`services/market_data_provider.py`](../../backend/archimedes/services/market_data_provider.py). Unset in `infra/ecs.tf` — the code default IS the prod value. |
| `MARKET_DATA_DAILY_PROVIDER` | mode selector | **The Tiingo cutover switch (#1798).** Default: unset → falls back to `MARKET_DATA_PROVIDER` → `"yfinance"`, so shipping it changed nothing. Set to `tiingo` to move DAILY BARS — strategy signal evaluation (marketplace ticks), the generation-path fusion panel and the portfolio backtester — onto Tiingo while every intraday/history surface stays on yfinance. Requires `TIINGO_API_TOKEN` (it refuses loudly rather than falling back) and starts with a cold `asset_daily_bars` cache, per the per-vendor `source` filter. Reader: `provider_name("daily")` — [`services/market_data_provider.py`](../../backend/archimedes/services/market_data_provider.py). Per-feature table + rationale: [`docs/adr/market-data-sourcing.md`](../adr/market-data-sourcing.md) § Amendment. Flip owner: Dan, after a proof pull on this seam — run [`scripts/verify_market_data.py`](../../scripts/verify_market_data.py) per [`docs/runbooks/market-data-provider-proof.md`](../runbooks/market-data-provider-proof.md) (fixed ten-bar window, prints seam + vendor + first/last bar, exits non-zero on a wrong answer); the same read done by hand from inside a task is in [`docs/adr/market-data-sourcing.md`](../adr/market-data-sourcing.md) § Proving the flip. Setting the *wider* `MARKET_DATA_PROVIDER` flips this seam too, by the back door — both names are in `test_ecs_backend_secrets.py`'s `FORBIDDEN_WHY`, so neither can reach `infra/ecs.tf` without an edit that states the intent. **Does not reach analytics-engine's standalone CLI seam**, which registers no Tiingo adapter. |
| `ORACLE_CRYPTO_SOURCE` | mode selector | Default `"coingecko"` (CoinGecko primary, `MARKET_DATA_PROVIDER` seam as fallback). Also `coingecko_only` / `provider` / `provider_only`. Unset in `infra/*` — the default IS the pre-#1710 happy path. Reader: [`chain/oracle_updater.py`](../../backend/archimedes/chain/oracle_updater.py) (`_crypto_source_order`). **Gotcha (rewritten by #1798):** this leg asks the *intraday* seam, which Tiingo does not serve, so `MARKET_DATA_PROVIDER=tiingo` no longer breaks it — the seam resolves to yfinance with a logged substitution and `provider`/`provider_only` keep pricing from there. Before #1798 the same setting raised `NotImplementedError` and dropped the symbol. |
| `GENERATION_PRICE_USD` | tunable | `"2.00"` in `infra/ecs.tf`. Flat `flat_v1` pricing until #1217's measured budget replaces it behind the same `quote()` seam. |
| `GENERATION_PAYMENT_RECIPIENT` | required config | Terraform variable (#1414). Flag-on with no recipient is a deliberate 503, never a free pass. |
| `PLATFORM_ADMIN_WALLETS` | allowlist | Terraform variable. Gates `/api/metrics/private/*` — as of #1648 as EVIDENCE: an account is admin when any of its OWN linked wallets is listed, not when the request's `X-Wallet-Address` header names one. **Drift gotcha:** re-pass `TF_VAR_platform_admin_wallets` on every apply or it silently empties. |
| `PLATFORM_ADMIN_ACCOUNTS` | allowlist | Terraform variable (#1648). The account-keyed half of the same gate: canonical `auth_users.id` values and/or emails. Answered with no database read, so it is the break-glass while the datastore is down. Derive it from the wallet list with `backend/scripts/derive_platform_admin_accounts.py`. **Same drift gotcha** as the wallet list. |
| `PUBLIC_DOMAIN` | environment discriminator | Set = production. Used as the production switch by the docs gate and the SIWE cookie policy — not a flag, but it changes behaviour like one. |
| `APP_ENV` | environment discriminator | Feeds `FEATURE_QUANT`'s production default. |
| `AWS_SSM_PATH_PREFIX` | environment discriminator | Blank by default since #1682 so a local run with ambient AWS creds cannot pull prod secrets. Not a flag; behaves like one on the wrong box. |
| `TESTING` | test harness | Forces hermetic paths (the fixture generation pipeline). Never set in a deployed environment. It used to also disable the backtest refresh loop; that loop is gone (#1760), so the "hermetic tests never hit yfinance" property now holds structurally rather than by a flag check. |
| `PAPER_TRACE_BACKFILL_MAX` | tunable | Default 500. |
| `REVENUE_SWEEP_INTERVAL_S` / `REVENUE_SWEEP_MIN_USDC` | tunable | Only consulted once `REVENUE_SWEEP_ENABLED=true`. |

---

## Audit findings

Findings A1–A4 are from the 2026-08-31 first pass; A5–A7 from the second pass
the same night, after the merge train.

**A1 — the three UI flags cannot be flipped in a deployed build.**
[`nginx/Dockerfile`](../../nginx/Dockerfile) declares build args for exactly
three Vite vars — `VITE_CIRCLE_CLIENT_KEY`, `VITE_CIRCLE_CLIENT_URL`,
`VITE_API_BASE` — and [`deploy.yml`](../../.github/workflows/deploy.yml) passes
the same three. `VITE_ROADMAP_SURFACES`, `VITE_KNOWLEDGE_GRAPH_TAB`, and
`VITE_GENERATION_QUOTE_ENABLED` have **no** `ARG`, so Vite sees them unset in
every image and each resolves to its code default (OFF, OFF, ON). Setting them
in `ui/.env` works for `npm run dev` and nowhere else. Flip rows 8 and 9 above
are therefore *code changes*, not config flips, until the build args are wired.

**A2 — `infra/ecs.tf`'s `GENERATION_PAYMENT_REQUIRED` comment is stale.**
The comment above the entry still reads "flag stays `"false"` until Dan flips it
deliberately"; the value below it is `"true"` (flipped 2026-08-20). The value is
correct and the comment is not. Left as-is here — this page is not the place to
edit terraform — but a reader trusting the comment would draw the wrong
conclusion about whether the paywall is live.

**A3 — `ARCHIMEDES_FUSION_ENABLED` — absent from `.env.example`, now retired entirely.**
`docker-compose.yml` defaulted it `:-true` and `infra/ecs.tf` set `"true"`, so
every real path was safe. The gap only bit a non-compose `python -m uvicorn`
run, which defaulted the flag OFF and got a silently empty Generate. Named
in #834 since 2026-07-07. **CLOSED 2026-09-02 (deck Q4)** by the second of the
two candidate fixes — not "add the line to `.env.example`" but "retire the
gate". The flag is gone from the code and from every injection site, so there
is no longer a default that can be wrong; the row moved to § DEAD / RETIRED.

**A4 — superseded: one dead flag was removable, and was removed.** The original
finding said no dead flag could be removed because every zero-reader name lived
in `.env.example`, which open PR #1595 was rewriting. #1595 merged on
2026-08-31, so the collision risk is gone, and `X402_WEBHOOK_SECRET` was removed
in the same PR as this revision. The remaining zero-reader `.env.example`
entries are contract addresses and credentials, not flags — out of scope for
#834, and each is a legitimate template line rather than dead config.

**A5 — RESOLVED (2026-09-02): `FREE_GENERATIONS_PER_ACCOUNT` is now pinned on
both task-definition paths.** The finding as written stands below; the fix
landed as the literal `{ name = "FREE_GENERATIONS_PER_ACCOUNT", value = "3" }`
in `infra/ecs.tf` **and** `FREE_GENERATIONS_VALUE = "3"` in
`.github/scripts/ecs_rewrite_task_def.py`. Both, not either: ecs.tf is the
declared baseline but is drifted and is not applied by `deploy.yml`, while the
rewrite script is the path that registers every revision. That is the same
two-path treatment `PAPER_ADVANCE_ENABLED` already has, for the same reason
(task-def :211). The value equals the code default, so nothing about what prod
serves changed — only whether it was decided. Original finding:

**A5 — `FREE_GENERATIONS_PER_ACCOUNT` is not in the task definition.**
It is read by `free_generations.allowance()` with a code default of 3 and is set
in `.env.example` — but **nowhere in `infra/ecs.tf`**, so prod grants three free
generations per account by *accident of a code default*, not by a decision
anyone applied. This is the same config-drift failure `GENERATION_DAILY_CAP_*`
was explicitly plumbed to avoid (see the comment above those entries) and that
#1686 and #1692 fixed for the admission knobs and the timeout. It is also the
one knob on this page that gives away paid product. Fix: add
`{ name = "FREE_GENERATIONS_PER_ACCOUNT", value = "3" }` to ecs.tf, matching the
code default so the apply is plumbing-only. **Not done here** — this page is not
the place to edit terraform, and a value change wants its own review.
*(Done separately, 2026-09-02 — see the resolution note above.)*

**A6 — five rows' reader citations had drifted in one night.**
`ARCHIMEDES_FUSION_ENABLED`, `GENERATION_PAYMENT_REQUIRED`,
`GENERATION_PAYMENTS_DRY_RUN`, `PAPER_TRADING`, `ENABLE_API_DOCS`,
`REVENUE_SWEEP_ENABLED`'s starter and both `generation_pipeline.py` citations
pointed at lines that had moved between 2026-08-30 and 2026-08-31. Every
citation on this page now names the **reading function** instead, which does not
move when code above it is edited. Adopted from the convention already written
at the top of `infra/variables.tf`.

**A7 — the guard had two blind spots, both closed.** `COST_KILL_SWITCH_DRY_RUN`
— an armed kill switch — was invisible because `infra/lambda/` was not a scanned
Python root and Terraform's Lambda `variables { X = "..." }` shape is not the
ECS `{ name, value }` shape. And the four admission/timeout knobs #1686/#1692
pinned into `ecs.tf` were invisible because the scanner deliberately ignores
numeric reads. Both are fixed: `infra/lambda` and `mcp-server/src` are scanned
roots, the Lambda assignment shape is matched, and a numeric read now counts
**if and only if** the same name is also pinned in `infra/ecs.tf`. Bare numeric
reads still do not count — 39 names in this tree are read that way, mostly RPC
timeouts and lease TTLs, and dragging them in would bury the checklist.

---

## Adding a flag

1. Read it in one place, behind a named function (`sweep_enabled()`,
   `publishing_enabled()`), not inline at every call site. The drift guard cites
   that function by name, so it must exist.
2. **Only the literal `"true"` enables a money switch.** An unset money switch
   must mean OFF. `AGENT_DRY_RUN` is the counter-example the repo already paid
   for: it defaults to `"false"` = LIVE, which is why the SSM seeding script has
   a hand-written refusal around it.
3. Set it explicitly in [`infra/ecs.tf`](../../infra/ecs.tf). A flag whose prod
   value is an accident of a code default is the failure mode the 2026-08-16
   money-switch pinning comment describes — and finding A5 above is a live
   instance of it.
4. **Add a row here in the same PR.** CI fails otherwise —
   [`backend/tests/test_feature_flag_fliplist_drift.py`](../../backend/tests/test_feature_flag_fliplist_drift.py)
   re-derives the inventory and will name your flag.

## Removing a flag

1. **Removal is one commit, with the evidence in the message** — the `git log
   -S<NAME>` line that shows why it was added, and the grep that shows nothing
   reads it now.
2. **Delete the dead branch too.** A flag whose reader is gone but whose `if`
   remains is still a flag; a `monkeypatch.delenv` for a name nothing reads is
   still residue.
3. **Move the row, do not delete it.** A removed flag belongs in § DEAD /
   RETIRED with the reason, which is what stops it being re-added. Deleting the
   reader while leaving the row on an actionable table now fails CI in the
   reverse direction — including the half-deletion where the `.env.example`
   line survives the code.
4. **When in doubt, keep it and say why** in § Looks dead, is load-bearing.
   Kill switches, refuse-by-default guards, and permanently-one-valued pins all
   look dead by construction; that is the shape of a lever you hope never to
   pull.
5. **A flag with no reader is deleted the same week its reader dies**
   ([#1824](https://github.com/aprin-labs/archimedes/issues/1824)'s standing
   rule). The pin, the `.env.example` line, the compose default and the row all
   move in the same commit as the reader. When the pin lives in a **live task
   definition the repo cannot edit**, the removal is the deploy-path strip that
   drops it — `RETIRED_BACKEND_ENV` in
   [`.github/scripts/ecs_rewrite_task_def.py`](../../.github/scripts/ecs_rewrite_task_def.py),
   never an operator ritual somebody has to remember. Every name that has
   outlived its reader by more than a week is on § DEAD / RETIRED with the
   evidence; read those rows before adding a ninth.
