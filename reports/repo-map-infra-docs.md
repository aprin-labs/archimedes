# Repo Map — INFRA / CI / TESTING / DOCS

Scout pass 2026-07-04. Scope: workflows, compose, infra TF, configs, scripts, docs.
No source edits. Findings ranked by severity.

> **Historical infrastructure snapshot.** Topology and security findings may be stale. Re-verify against current HEAD and live infrastructure before acting. Line cites are exact.

## Files Retrieved

- `.github/workflows/{quality-gate,deploy,contracts-test,import-guard,main-format-guard,release-tag,complexity-gate}.yml` — full CI surface
- `.github/dependabot.yml` — 5 ecosystems, weekly
- `pytest.ini`, `ruff.toml` — test/lint config
- `docker-compose.yml`, `docker-compose.production.yml`, `.env.example` — deploy topology + env contract
- `Makefile`, `dev.sh` — local dev entrypoints
- `tests/flows/*.py`, `tests/conftest.py` — top-level "e2e" flow suite
- `infra/cloudwatch.tf` (alarms+SNS), `infra/` tree (asg/aurora/alb/waf/elasticache)

---

## Ranked Findings

### 1. HIGH — `tests/flows/` e2e suite is (a) never run in CI and (b) vacuous stubs

Evidence:

- `pytest.ini` `testpaths = backend/tests` → root `pytest` never collects `tests/flows/`. No workflow references `tests/flows` or `pytest tests` (`grep` empty).
- Flow files are docstring-only method bodies with almost no assertions: `test_flow_2_amm_swap.py` has 5 `assert` across ~30 test methods (rest are empty `pass`-equivalent stubs — see lines 31–80). Flows 4/5/6 have 21/31/43 asserts but still large stub fractions.
- These claim to exercise the mandatory demo flows (AMM swap, reasoning traces, agent mgmt, marketplace) — the exact "claims must be true" surface CLAUDE.md #1-rules.
Impact: false sense of e2e coverage; the load-bearing user journeys have no executable guard.
Severity: HIGH.
Suggested issue: **"Wire `tests/flows/` into CI as a marked integration suite + fill stub bodies or delete"**
Clarify: keep as integration-marked (needs seeded DB/chain) or convert to hermetic mocked flows? Are these intentionally aspirational specs?

### 2. HIGH — Deploy targets a single hardcoded EC2 instance while infra provisions an ASG

Evidence: `deploy.yml` contained one explicit EC2 instance target (identifier redacted here); `infra/asg.tf` + `docker-compose.production.yml` described an autoscaling replica tier. Deploy only touched one box.
Impact: ASG replicas would not receive `git reset --hard origin/main` deploys → drift/stale replicas if ASG ever scales >1. Deploy model and infra model disagree.
Severity: HIGH (deploy risk / infra-vs-reality drift).
Suggested issue: **"Reconcile single-instance SSM deploy with ASG topology (CodeDeploy or per-instance fan-out, or document single-box reality)"**
Clarify: Is ASG actually live/desired-count>1, or is `docker-compose.production.yml`/ASG aspirational? CLAUDE.md T3.5 says Aurora/ElastiCache cutover still pending.

### 3. MED-HIGH — No SAST / secret-scanning as a *blocking* gate

Evidence: only `detect-secrets` (informational, `continue-on-error`, quality-gate supply-chain job) + pip-audit/npm-audit informational. No CodeQL, no gitleaks history scan, no blocking secret gate. CLAUDE.md flags "security ships with the product" as #6 primitive.
Impact: a newly committed secret or a critical CVE can merge freely; only a PR-comment nudge.
Severity: MED-HIGH.
Suggested issue: **"Add CodeQL + promote detect-secrets/pip-audit to blocking once CVE backlog (starlette/urllib3/pypdf) cleared"**
Clarify: acceptable to hard-block on the known open CVE backlog, or clear backlog first?

### 4. MED — Documented deploy-role security debt still open (OIDC trust + out-of-band IAM)

Evidence: `deploy.yml` header "KNOWN SECURITY DEBT" #2 (`archimedes-github-deploy`, SSM roles created via CLI not Terraform) and #3 ("VERIFY OIDC TRUST POLICY IS LOCKED TO THIS REPO" — without the `repo:a-apin/archimedes:*` condition, any repo's Actions can assume the role).
Impact: #3 is a real privilege-escalation vector if unverified; #2 = non-recoverable IAM state.
Severity: MED (HIGH if trust condition is actually absent — unverified).
Suggested issue: **"Verify + Terraform the GitHub OIDC deploy role trust policy (lock to repo/ref) and codify out-of-band IAM"**
Clarify: can scout get read-only AWS (`aws iam get-role --role-name archimedes-github-deploy`) to confirm the Condition block, or is that Dan-only?

### 5. MED — Hermetic-test discipline leaking: Makefile `pytest` deselects two "flaky" tests

Evidence: `Makefile` `pytest:` target `--deselect ...test_agent_status_redis_down_defaults --deselect ...test_advisor_redis_unavailable`. CLAUDE.md testing-conventions: "No skip-marks on flaky tests… fix the boundary mock." A deselect in the canonical make target is the same smell, and it means `make pytest` ≠ CI `pytest -m "not integration"` (CI does NOT deselect → these run in CI).
Impact: local/CI divergence (the exact "CI green ≠ local green is a bug" rule); masks a Redis-boundary mock gap.
Severity: MED.
Suggested issue: **"Fix Redis-down boundary mocks so make pytest drops the two --deselect flags"**

### 6. MED — Node version mismatch: docs say v26, CI pins 22

Evidence: `CLAUDE.md`/`README.md` line 235 `node --version → v26.x ; npm 11.x`; `quality-gate.yml` lines 75,211 `node-version: "22"`. eslint + `npm ci` run under 22 in CI, dev under 26.
Impact: lint/build behavior can differ dev-vs-CI; lockfile/engine drift risk.
Severity: MED.
Suggested issue: **"Align Node version across CI (setup-node), conda env, and docs"**
Clarify: which is the intended pin — 22 (CI) or 26 (env)?

### 7. LOW-MED — Env-contract / doc drift (`.env.example` vs CLAUDE.md vs ruff)

Evidence:

- CLAUDE.md says "`.env.example` still defaults `LLM_PROVIDER=anthropic_compatible` (stale, T3.10)" — but `.env.example` **already** defaults `LLM_PROVIDER=bedrock_converse` (line ~"Default matches production"). Doc is stale about its own fix.
- CLAUDE.md CI table + ruff §: "blocking subset … `--select E9,F63,F7,F40`; next candidate is F82" — but `quality-gate.yml` ruff-gate **already** runs `--select E9,F63,F7,F40,F82`. Comment in same file still lists the old set.
Impact: parallel Claude sessions read stale contract; low functional risk.
Severity: LOW-MED.
Suggested issue: **"Refresh CLAUDE.md CI/env drift (F82 already blocking; LLM_PROVIDER default already bedrock_converse)"**

### 8. LOW — Human PRs have no coverage floor; agent-only gate

Evidence: `quality-gate.yml` `coverage-gate` job `if: github.event.pull_request.user.login == 't2o2'`. Human/other-agent PRs skip the ≥60% floor entirely.
Severity: LOW (intentional per docs, but a blind spot as non-t2o2 agents drive more of the build).
Suggested issue: **"Extend coverage floor to all PRs (informational for non-python)"**

### 9. LOW — Backend service has no healthcheck in local `docker-compose.yml`

Evidence: `docker-compose.yml` backend service: no `healthcheck` block (oracle/agent explicitly `disable: true`); only `docker-compose.production.yml` backend has one. nginx `depends_on: backend: condition: service_started` (not `_healthy`).
Impact: local nginx can route to a not-yet-ready backend; deploy relies on the workflow's own 24×5s health poll instead.
Severity: LOW.
Suggested issue: **"Add backend healthcheck + service_healthy gate to docker-compose.yml (mirror production file)"**

### 10. LOW — Cosmetic deploy poll-loop label bug

Evidence: `deploy.yml` line 171 `echo "Status: $STATUS (attempt $i/60)"` inside a `seq 1 180` loop → misreports "/60".
Severity: LOW (log-only).
Suggested issue: **"Fix deploy poll-loop attempt label (180 not 60)"**

### 11. LOW — CloudWatch alarms exist but observability breadth unverified

Evidence: `infra/cloudwatch.tf` has SNS topic + 5 alarms (ec2_cpu, ec2_status_check, alb_5xx, alb_unhealthy_hosts, alb_latency) with email subscription (`var.alarm_email`). No app-level/business alarms (deploy failure, LLM fallback rate, oracle staleness) surfaced in TF.
Note: CLAUDE.md calls "loud fallback telemetry" a Tier-0 requirement; `/health` degradation is app-side, not alarmed.
Severity: LOW.
Suggested issue: **"Add app-signal CloudWatch alarms (LLM-fallback rate, oracle push staleness, deploy failure)"**
Clarify: is `var.alarm_email` populated in prod tfvars? (unverified)

---

## Architecture (how CI/deploy connect)

- **PR gates (blocking):** `quality-gate` backend-tests (`pytest -m "not integration"`, pip-installed, hermetic — no docker) + `ruff-gate` (format + E9,F63,F7,F40,F82). `import-guard` (F821 + compileall + deptry, contract-scoped paths). `contracts-test` (forge build+test, paths-filtered to `contracts/**`).
- **PR gates (informational):** lint-report (ruff/eslint table), supply-chain (pip-audit/npm-audit/detect-secrets table), complexity-gate.
- **Push→main:** `main-format-guard` (self-heals format, commits `[skip ci]`), `release-tag` (semver from PR-title end-anchor), `deploy` (OIDC→AWS→SSM SendCommand→single EC2 `git reset --hard` + `docker compose up`; gated by repo var `DEPLOY_ENABLED`).
- **Compose duality:** one `docker-compose.yml` serves local (profile `localdb` starts pg+redis) and single-box prod; `docker-compose.production.yml` is the stateless ASG replica unit (app+nginx only, managed Aurora/ElastiCache). Deploy workflow uses the **default** compose file on the single instance.
- **Env contract:** `.env.example` is the source of truth; prod secrets pulled from SSM Parameter Store `/archimedes/prod/*` via EC2 instance role (pull-model, no CI injection).

## Start Here

Open `.github/workflows/deploy.yml` first — it encodes the deploy model, the single-instance-vs-ASG tension (Finding 2), and the three self-documented security-debt items (Finding 4). It's the highest-leverage file for both deploy risk and security posture.

## Supervisor coordination

Not blocked. Findings 2 and 4 carry open clarifications that need Dan (infra/AWS owner) — flagged inline, not escalated (scout is read-only, no decision needed to complete the map).
