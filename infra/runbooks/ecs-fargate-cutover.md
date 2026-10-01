# ECS Fargate Cutover Runbook — Archimedes (issue #1039)

> **Status:** Authored 2026-07-06 (build-chunk 2/4 of #1039, `terraform-ecs`);
> updated 2026-07-07 (build-chunk 3/4, `migrate-config` — AURORA_MASTER_PASSWORD
> / EMAIL_ENCRYPTION_KEY wired as ECS secrets, PLATFORM_ADMIN_WALLETS /
> ARCHIMEDES_TREASURY_WALLET wired as ECS env, and the Alembic pre-rollout
> migrate stage added to `.github/workflows/deploy.yml`); reorganized
> 2026-07-07 (build-chunk 4/4, `docs-runbook` — the full ordered
> apply → seed → cutover → verify → decommission sequence below, with explicit
> zero-downtime and self-heal verification drills that were previously
> implied but not spelled out); updated again 2026-07-07 (build-chunk 5,
> `infra-hardening` — PR #1041 review comprehensiveness pass: B2 static
> migrate network config splits Phase 1's Stage 2 into 2a/2b with a mandatory
> migrate-then-verify gate between them; B3 mandatory baseline-stamp
> pre-flight; N1 NAT auto-recovery alarm; N2 dead-egress RPC timeout +
> chain_connected alarm; N3 NAT AMI-drift guard; a free S3 Gateway endpoint;
> C1 CI now redeploys Fargate itself; C2 corrects the rollback doc; C3 guards
> against a concurrent SSM deploy race; R1 reorders Phase 8's decommission
> gate and adds a NAT-outage scenario to the disaster-recovery runbook);
> updated again 2026-07-07 (build-chunk 6, `env-parity` — PR #1041
> correctness pass: closed two runtime/build env-parity gaps the Fargate task
> would otherwise have shipped without — `infra/ecs.tf`'s backend
> `environment` block was missing `LLM_PROVIDER`/`LLM_BEDROCK_MODEL`/
> `PRICE_SOURCE` (the prod box gets these from a box-local `.env` Fargate has
> no equivalent of), and `.github/workflows/deploy.yml`'s nginx build shipped
> with empty `VITE_*` build-args (no Circle client key baked into the served
> bundle); added the PRE-CUTOVER ENV-PARITY CHECKLIST as a hard gate before
> Phase 4; and locked in **Option C — hard cutover, no parallel-traffic
> soak** as the chosen cutover strategy, rewriting Phase 4 accordingly);
> updated 2026-07-08 (issue #1065 runner-relocation draft — **Phase 8's old
> gate (three ECS *services* named `archimedes-oracle`/`archimedes-agent`/
> `archimedes-kb-runner`) never matched what actually got built and is
> REPLACED below.** The shipped shape is a dedicated EC2 instance for
> oracle+agent (funds-adjacent exactly-once singletons — never a Fargate
> service with autoscaling, never an ASG) and a scheduled (not
> long-running) Fargate task for kb-runner. See `infra/runner_ec2.tf`,
> `infra/kb_runner.tf`, `infra/efs.tf`, and #1065 for the full IaC + the
> post-apply execution checklist).
> **Not yet applied, not yet drilled.** Written against `infra/ecr.tf` +
> `infra/ecs.tf` on the `dbrowneup/1039-fargate-infra` epic branch. No
> `terraform apply`, ALB cutover, or EC2 decommission has happened — those are
> Dan's AWS operations (see `CLAUDE.md` § AWS account access), not anything an
> agent runs. **Treat every command below as review-then-run, in order** —
> each phase assumes the previous one is done and verified.

Region: `us-east-1`. Account: `037613907429`, profile `ArchimedesDanAdmin`.

**The acceptance criteria this runbook exists to satisfy (issue #1039):** no
`docker build` on a serving host; zero ALB 5xx during a rolling deploy; a
killed task self-heals in <2 min with no human action; `aws ecs
execute-command` opens a shell and logs survive in CloudWatch; `alembic
upgrade head` runs pre-rollout; rollback = redeploy the prior image tag;
total spend stays within ±10% of ~$178/mo. Phases 5–7 below are the drills
that produce the evidence for criteria 2–4; do them once, deliberately,
before trusting the cutover.

---

## Phase 0 — Before you `terraform apply` this chunk: three blockers

This chunk (`ecr.tf` + `ecs.tf`) is structurally complete IaC, but three gaps
must close before the resulting ECS service will actually serve traffic
(each is called out in a `KNOWN GAP` comment at the top of `ecs.tf` too —
this is not new information, just gathered in one place):

1. **`nginx/nginx.conf`'s upstream is `server backend:8000 resolve;`** — a
   Docker-Compose bridge-network DNS name. ECS Fargate `awsvpc` tasks share
   one ENI across containers and talk via `localhost`, not container names.
   Fix: `server localhost:8000 resolve;` (small, but shared with the
   still-live EC2/compose path where `backend:8000` is still correct —
   needs either an environment-conditional template or to land at the same
   time the EC2 path is retired).
2. **Strategies/corpus/ABI paths are host bind mounts today**
   (`./analytics-engine/strategies`, `./data/corpus`, `./contracts/abis`,
   the `archimedes-corpus-artifact` named volume — see `docker-compose.yml`).
   Fargate has no host filesystem to bind-mount from, and
   `backend/Dockerfile`'s `COPY . /app` only copies the `backend/` build
   context — none of these are baked into the image. Needs either
   Dockerfile `COPY` additions or an EFS volume attached to the task.
3. **`DATABASE_URL` / `REDIS_URL` SSM parameters don't exist yet.** The task
   definition's `secrets` block resolves them from
   `/archimedes/prod/DATABASE_URL` and `/archimedes/prod/REDIS_URL`. Seed
   them the same way `AURORA_MASTER_PASSWORD` / `EMAIL_ENCRYPTION_KEY`
   already are:
   ```bash
   # secrets.env (NOT committed):
   #   DATABASE_URL=postgresql://archimedes:<password>@<aurora_endpoint>:5432/archimedes
   #   REDIS_URL=rediss://<redis_endpoint>:6379/0
   ./infra/scripts/seed-ssm-secrets.sh /path/to/secrets.env
   ```
   (`terraform output aurora_endpoint` / `redis_endpoint` give the host
   parts; `terraform output -raw database_url` gives the full string with
   password already substituted, if you'd rather copy it directly.)
   `AURORA_MASTER_PASSWORD` / `EMAIL_ENCRYPTION_KEY` themselves need **no**
   seeding step — build-chunk 3 (`migrate-config`) wired them as ECS
   `secrets` too, and both already exist live in SSM
   (`infra/scripts/setup-ssm-secrets.sh`, predating this epic).

Two more gaps are worth knowing about but do **not** block a launch (no seeding,
no code fix required for the service to come up healthy):

- **`platform_admin_wallets` / `archimedes_treasury_wallet` Terraform
  variables default to empty.** Build-chunk 3 wired `PLATFORM_ADMIN_WALLETS`
  / `ARCHIMEDES_TREASURY_WALLET` as plain (non-secret) ECS task-definition
  env, sourced from these two new `variables.tf` entries — set them before
  apply if you want the admin-wallet publish bypass / marketplace publish
  live on Fargate from day one:
  ```bash
  export TF_VAR_platform_admin_wallets="0xabc...,0xdef..."
  export TF_VAR_archimedes_treasury_wallet="0x123..."
  ```
  Left unset, both features stay off (matching `.env.example`'s own empty
  `ARCHIMEDES_TREASURY_WALLET` default) — a safe no-op, not a launch failure.
- **oracle/agent/kb-runner** (the background daemons) have no Fargate service
  in this chunk — they need their own (singleton, no-ALB) ECS services
  before the EC2 instance can actually be decommissioned (Phase 8 / #1039
  P6). Tracked as a residual, not silently dropped.

### Alembic pre-rollout migrate stage (#1039 P4, build-chunk 3; B2/B3 hardening, build-chunk 5)

`.github/workflows/deploy.yml` gained a `migrate` job (`needs: build-and-push`;
`deploy` now `needs: [build-and-push, migrate]`) that runs a one-off `aws ecs
run-task` against the **dedicated** migrate task definition
(`infra/ecs_migrate.tf`'s `aws_ecs_task_definition.migrate`, family
`archimedes-migrate`) — a single container, no nginx sidecar, no
healthCheck/dependsOn — but **only if `backend/alembic.ini` exists on the
checked-out ref**. (Originally this targeted the SERVICE family,
`archimedes-backend`, which bundles an nginx sidecar that `dependsOn` the
backend container being `HEALTHY`; since a migrate run overrides the backend
command away from serving HTTP, `/health` never resolves and ECS would kill
the task before Alembic finished — split out into its own family per the
PR #1041 Copilot review.) As of this PR, #1028 Phase A hasn't landed Alembic
yet (`backend/migrations/` is still hand-rolled timestamped `.sql` +
`archimedes.db.init_db()`'s idempotent `create_all` / `ADD COLUMN IF NOT
EXISTS` patches), so the job detects that and no-ops loudly (a clear log
line, exit 0) rather than either failing the pipeline on a stage with
nothing to do yet, or silently pretending to run a migration that doesn't
exist. The moment `backend/alembic.ini` lands on this branch, the step
activates itself — no further pipeline change needed.

**B2 — static network configuration, not `describe-services`.** The job
previously borrowed the LIVE ECS **service's** own `networkConfiguration` via
`aws ecs describe-services archimedes-backend`. That's a chicken-and-egg bug:
`describe-services` can only succeed once `aws_ecs_service.backend` already
exists, but the whole point of the migrate task is to run **before** that
service is created (a bare `terraform apply` creating the service also
instantly launches an un-migrated task from it). Fixed: the job now uses a
STATIC `ECS_MIGRATE_NETWORK_CONFIGURATION` literal, sourced from
`terraform output -raw ecs_migrate_network_configuration` (built from
`aws_subnet.private` + `aws_security_group.ecs_backend`, both of which exist
independently of the service) — same literal-constant pattern as
`ECS_CLUSTER` / `ECS_MIGRATE_TASK_FAMILY`. **This is why Phase 1 below applies
the cluster/task-defs/security-group in their own stage (2a) BEFORE the
service (2b), and requires a green migrate run in between.**

**B3 — MANDATORY: auto-stamp the already-populated prod DB.** Prod Aurora has
**no `alembic_version` table today** — its schema was built by
`init_db()`'s `create_all()` + hand-rolled `ADD COLUMN IF NOT EXISTS`
patches, never by Alembic. A bare `alembic upgrade head` against that
database fails immediately: it tries to `CREATE TABLE backtest_results` (the
baseline revision's first statement) against a table that already exists.
The migrate task's command is therefore **not** `python -m alembic upgrade
head` — it is `python -m archimedes.scripts.alembic_migrate_preflight`
(`backend/archimedes/scripts/alembic_migrate_preflight.py`), which:
1. Checks whether `alembic_version` is absent **and** `backtest_results`
   (the oldest pre-Alembic table) already exists.
2. If so, runs `alembic stamp af9c6a9376e4` (the baseline revision — see
   `backend/migrations/versions/af9c6a9376e4_baseline_schema.py`) first.
3. Then runs `alembic upgrade head` unconditionally.

This is idempotent — after the first successful run, `alembic_version`
exists, so every later run skips straight to step 3. **Do not run a bare
`alembic upgrade head` against prod by hand outside this task** — it will
fail on the baseline the same way. `backend/tests/scripts/
test_alembic_migrate_preflight.py` covers the decision logic hermetically
(sqlite, no Postgres/alembic needed) plus a full Postgres integration test
(`@pytest.mark.integration` — create_all() → stamp → upgrade head → assert
success) that self-activates once `backend/alembic.ini` lands.

---

## What `terraform apply` actually does — read this before running it

**Applying `ecs.tf` starts live traffic cutover, not just "provisioning."**
The moment `aws_ecs_service.backend` is created, ECS begins registering each
healthy Fargate task's ENI IP directly into `archimedes-backend-tg` — the
*same* target group the EC2 instance is already statically attached to via
`aws_lb_target_group_attachment.backend` in `alb.tf`. A target group with
multiple healthy targets round-robins across all of them. In practice:

- The instant the first Fargate task passes its health check, **some live
  production requests start being served by Fargate**, alongside the EC2 box,
  automatically, with no further action.
- This is actually a useful, low-effort blue/green mechanism (a natural
  canary) — but only if the Phase 0 blockers above are closed. If they
  aren't, Fargate tasks will register unhealthy (or never reach RUNNING) and
  the ALB simply keeps sending everything to the still-healthy EC2 target —
  i.e. the failure mode is "no-op," not "outage," as long as
  `deployment_circuit_breaker` and the ALB health check are both in place
  (they are, in `ecs.tf`/`alb.tf`).
- **This automatic blend is a brief, incidental side effect of Stage 2b's
  apply, not a deliberate soak window.** Under the chosen Option C cutover
  (see Phase 4), the gap between Stage 2b and the ALB swing is just "run
  Phase 3 + the pre-cutover env-parity checklist," not an open-ended period
  of blended traffic — don't read this section as license to leave both
  targets live for days.

---

## Phase 1 — Terraform apply order

Apply in **three stages**, not one big-bang `terraform apply` for the whole
chunk — **1, 2a, 2b, in that order, with a hard gate between 2a and 2b.**
Two independent reasons force the staging:

- `aws_ecs_service.backend` will try to launch tasks the moment it's created,
  and a task with nothing to pull from ECR fails to start (not fatal —
  `deployment_circuit_breaker` + the still-healthy EC2 target mean this is a
  no-op, not an outage — but it's noisy and wastes a launch cycle). Staging
  means the ECS service's first-ever launch attempt already has a real image
  to pull.
- **MANDATORY (issue #1039 B2/B3): the Alembic migrate task must run to
  exit 0, with `alembic current` verified `>=` the baseline revision
  (`af9c6a9376e4`), BEFORE the `terraform apply` that creates
  `aws_ecs_service.backend`.** Once the service exists, it immediately
  launches tasks against whatever schema state the database happens to be
  in — if that apply runs before a real migrate pass, the very first Fargate
  task can boot against a stale or (on prod Aurora specifically) an
  Alembic-untracked schema. `aws_ecs_service.backend` and the migrate task
  definition both live in what was previously a single "Stage 2" apply; it
  is now split into **Stage 2a** (everything the migrate task needs — cluster,
  task definitions, the `ecs_backend` security group, the CI deploy role's
  ECS permissions — but explicitly NOT the service) and **Stage 2b** (the
  service + autoscaling), with the migrate run + verification as the gate
  between them.

```bash
aws sso login --profile ArchimedesDanAdmin
export AWS_PROFILE=ArchimedesDanAdmin AWS_REGION=us-east-1
export TF_VAR_aurora_master_password="$(aws ssm get-parameter \
  --name /archimedes/prod/AURORA_MASTER_PASSWORD --with-decryption \
  --query Parameter.Value --output text)"
aws sts get-caller-identity   # smoke-test — confirms account 037613907429

cd infra/
terraform init
terraform plan   # scrutinize: should show ONLY new resources (ecr.tf, ecs.tf,
                  # ecs_migrate.tf, the alb.tf security-group egress addition)
                  # — zero destroys, zero changes to aws_lb.main /
                  # aws_lb_target_group.backend / aws_rds_cluster.main /
                  # aws_elasticache_replication_group.main.
```

**Stage 1 — ECR only** (repos + lifecycle policies; nothing that launches a task):

```bash
terraform apply -target=aws_ecr_repository.backend -target=aws_ecr_repository.nginx \
                 -target=aws_ecr_lifecycle_policy.backend -target=aws_ecr_lifecycle_policy.nginx
```

Then go to **Phase 2** and seed a real image before continuing — do not skip
ahead to Stage 2a with empty repos.

**Stage 2a — everything the migrate task needs, but NOT the service** (IAM
roles/policies, the ECS cluster, both task definitions, the `ecs_backend`
security group) — only after Phase 2 has pushed at least one image:

```bash
terraform plan    # re-diff — should show ecs.tf/ecs_migrate.tf/ec2_iam.tf-
                   # adjacent resources; ECR resources already applied read
                   # as unchanged
terraform apply \
  -target=aws_ecs_cluster.main \
  -target=aws_ecs_task_definition.backend \
  -target=aws_ecs_task_definition.migrate \
  -target=aws_security_group.ecs_backend \
  -target=aws_iam_role_policy.github_deploy_ecs
```

1. **Seed the SSM secrets** (Phase 0, blocker 3) if you haven't yet — the
   migrate task (and later, the service) will otherwise sit in a
   launch-failure loop on `DATABASE_URL`/`REDIS_URL` secret resolution.
2. **Land the nginx.conf + Dockerfile fixes** (Phase 0, blockers 1–2) before
   or immediately after this stage — they only affect the SERVICE containers
   (nginx + backend serving HTTP), not the single-container migrate task, so
   they don't block the migrate gate below, but land them before Stage 2b.
3. `terraform apply` (the `-target` list above). Expect: ECS cluster + both
   task definitions + `ecs_backend` security group created — **no service,
   no tasks launched yet.**
4. `terraform output -raw ecs_migrate_network_configuration` — copy the
   result into `.github/workflows/deploy.yml`'s `ECS_MIGRATE_NETWORK_CONFIGURATION`
   literal (replacing the `REPLACE_WITH_*` placeholders), commit, and push
   (or merge via PR — either way this needs a real commit; the migrate job
   fails closed with a clear `::error::` while the placeholders are still
   there).

**GATE — run the migrate task and verify BEFORE Stage 2b:**

```bash
# Preferred: let CI run it for real (needs backend/alembic.ini to already be
# on `main` — issue #1028 — and DEPLOY_ENABLED=true):
gh workflow run deploy.yml --ref main
gh run watch   # follow the `migrate` job specifically; it must exit 0

# Verify `alembic current` reports a revision — not just "the job exited 0"
# (a no-op skip due to a missing alembic.ini would also exit 0, silently
# proving nothing). Run a second one-off task overriding the command:
aws ecs run-task \
  --cluster archimedes-cluster \
  --task-definition archimedes-migrate \
  --launch-type FARGATE \
  --network-configuration "$(terraform output -raw ecs_migrate_network_configuration)" \
  --overrides '{"containerOverrides":[{"name":"migrate","command":["python","-m","alembic","current"]}]}' \
  --query 'tasks[0].taskArn' --output text
# then: aws ecs wait tasks-stopped --cluster archimedes-cluster --tasks <arn>
# then check CloudWatch Logs (/archimedes/app, ecs-migrate-* stream, this
# task's own stream) for the printed revision — it must be present (not
# empty) and must be the real Alembic head, not merely "some revision".
```

Do **not** proceed to Stage 2b until: the migrate job/task exited 0, AND the
`alembic current` check above shows a real, non-empty head revision. If
either check fails, stop — do not create the service against a schema you
haven't confirmed is current.

**Stage 2b — the service + autoscaling** (only after the gate above passes):

```bash
terraform plan    # re-diff — should now show ONLY aws_ecs_service.backend,
                   # aws_appautoscaling_target.backend,
                   # aws_appautoscaling_policy.backend_cpu, and the one
                   # alb.tf SG egress rule; everything from Stage 2a reads as
                   # unchanged
terraform apply
```

Expect: ECS service + autoscaling created, task(s) attempt to launch and pull
the image Phase 2 already seeded — now against a database whose schema is
already confirmed current.

---

## Phase 2 — Seed ECR with the first image

The ECS service (Stage 2b above) needs a real, boot-validated image sitting
in `archimedes-backend` / `archimedes-nginx` **before** it launches its first
task, or that first launch attempt is guaranteed to fail (empty repo → pull
error → circuit breaker trips → task never reaches steady state). Do this
right after Stage 1 (ECR-only apply) and before Stage 2a.

**Preferred path — let CI do it** (same `build-and-push` job build-chunk 1
wired; already boot-validates both images with a live `/health` curl before
pushing, so this seeds ECR with an image that's been smoke-tested, not just
built):

```bash
# Confirm the gate is on (should already be `true` — this is the live deploy
# mechanism today, see .github/workflows/deploy.yml's header comment):
gh variable list | grep DEPLOY_ENABLED

# Trigger the same pipeline that already runs on every push to main, without
# waiting for a real commit:
gh workflow run deploy.yml --ref main
gh run watch   # follow build-and-push; it fails closed with an explicit
               # ::error:: if the ECR repos it's pushing to don't exist yet
               # (i.e. if you run this before Stage 1's ECR apply)
```

This pushes both `archimedes-backend:<commit-sha>`, `archimedes-backend:latest`,
`archimedes-nginx:<commit-sha>`, and `archimedes-nginx:latest`. The `migrate`
and `deploy` jobs also run (gated on the same `DEPLOY_ENABLED` flag) after
`build-and-push` — `build-and-push` itself has already succeeded and pushed
the images by the time either of those runs, so this step's actual goal (seed
ECR) is achieved regardless of what happens next:
- If `backend/alembic.ini` isn't on `main` yet, `migrate` no-ops loudly and
  exits 0, same as always.
- If it IS on `main` but Stage 2a hasn't been applied yet (this phase, by
  definition, runs before Stage 2a), `migrate` now **fails closed** with a
  clear `::error::` (the `ECS_MIGRATE_NETWORK_CONFIGURATION` literal still
  has its `REPLACE_WITH_*` placeholders, or the migrate task definition
  doesn't exist yet) rather than silently passing — expected and fine at this
  point in the sequence, not a real incident.
- `deploy` only touches the still-untouched EC2 box regardless, so it's
  always safe to run this trigger before Stage 2a — the worst case is a red
  Actions run for a step that had nothing to verify yet, not any live-traffic
  effect.

**Fallback — push manually** (only if CI is down or you need an image before
merging to `main`; skips CI's own boot-validation, so treat this as a
break-glass path, not the normal one):

```bash
export AWS_PROFILE=ArchimedesDanAdmin AWS_REGION=us-east-1
ECR_REGISTRY=037613907429.dkr.ecr.us-east-1.amazonaws.com
aws ecr get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin "$ECR_REGISTRY"

docker build -t archimedes-backend:manual -f backend/Dockerfile .
docker tag archimedes-backend:manual "$ECR_REGISTRY/archimedes-backend:latest"
docker push "$ECR_REGISTRY/archimedes-backend:latest"

docker build -t archimedes-nginx:manual -f nginx/Dockerfile .
docker tag archimedes-nginx:manual "$ECR_REGISTRY/archimedes-nginx:latest"
docker push "$ECR_REGISTRY/archimedes-nginx:latest"
```

**Verify the seed landed** before moving to Stage 2a:

```bash
aws ecr describe-images --repository-name archimedes-backend \
  --query 'imageDetails[*].{tags:imageTags,pushed:imagePushedAt}' --output table
aws ecr describe-images --repository-name archimedes-nginx \
  --query 'imageDetails[*].{tags:imageTags,pushed:imagePushedAt}' --output table
```

---

## Phase 3 — Verify the ECS service comes up healthy

After Stage 2b's apply (which only happens after the migrate gate above passes):

```bash
# Service + task status
aws ecs describe-services --cluster "$(terraform output -raw ecs_cluster_name)" \
  --services "$(terraform output -raw ecs_service_name)" \
  --query 'services[0].{status:status,running:runningCount,desired:desiredCount,events:events[0:3]}'

# Target health — are Fargate task IPs showing healthy in the SAME target group
# the EC2 instance is attached to?
aws elbv2 describe-target-health \
  --target-group-arn "$(aws elbv2 describe-target-groups \
     --names archimedes-backend-tg --query 'TargetGroups[0].TargetGroupArn' --output text)"

# Smoke test through the ALB directly (bypasses CloudFront/DNS)
curl -sS -H "Host: archimedes-arc.com" "https://$(terraform output -raw alb_dns_name)/health"
```

Do not proceed to Phase 4 until `runningCount == desiredCount` and the
`describe-target-health` output shows the Fargate (`ip`-type) targets as
`healthy` — not just the pre-existing EC2 target.

---

## PRE-CUTOVER ENV-PARITY CHECKLIST — hard gate before Phase 4

**Added 2026-07-07 (build-chunk 6, `env-parity` — PR #1041 correctness pass).**
Phase 3's health check only proves the Fargate task's process is up and
answering `/health` — it says nothing about whether the task's *configuration*
is actually complete. This PR closed two real gaps found by exactly that
failure mode (`infra/ecs.tf`'s backend `environment` block was missing
`LLM_PROVIDER`/`LLM_BEDROCK_MODEL`/`PRICE_SOURCE`, all of which the prod EC2
box gets from a box-local `.env` Fargate has no equivalent of; and
`.github/workflows/deploy.yml`'s nginx build shipped with empty `VITE_*`
build-args, so the served bundle had no Circle client key). The checklist
below is the standing gate against the *next* one — **do not proceed to
Phase 4 until every check here passes.**

1. **Runtime env vars actually resolve on the REGISTERED task definition**
   (not just what's authored in `ecs.tf` — confirm what Terraform actually
   applied):
   ```bash
   aws ecs describe-task-definition --task-definition archimedes-backend \
     --query 'taskDefinition.containerDefinitions[?name==`backend`].environment | [0]' \
     --output json > /tmp/backend-env.json

   for kv in \
     'LLM_PROVIDER=bedrock_converse' \
     'LLM_BEDROCK_MODEL=amazon.nova-micro-v1:0' \
     'PRICE_SOURCE=cascade'; do
     name="${kv%%=*}"; want="${kv#*=}"
     got=$(jq -r --arg n "$name" '.[] | select(.name==$n) | .value' /tmp/backend-env.json)
     [ "$got" = "$want" ] && echo "OK: $name=$got" \
       || { echo "FAIL: $name resolved to '${got:-<missing>}', expected '$want'"; exit 1; }
   done

   # PUBLIC_DOMAIN is templated (https://${var.domain_name}), not a fixed
   # literal — assert non-empty and scheme-qualified (main.py's CORS + SIWE
   # checks both compare against a scheme-qualified origin):
   domain=$(jq -r '.[] | select(.name=="PUBLIC_DOMAIN") | .value' /tmp/backend-env.json)
   [[ "$domain" == https://* ]] && echo "OK: PUBLIC_DOMAIN=$domain" \
     || { echo "FAIL: PUBLIC_DOMAIN missing or not https://-qualified"; exit 1; }
   ```
2. **The four SSM secrets are wired in the task def AND actually exist in
   SSM** (a `secrets` entry whose `valueFrom` points at a parameter that
   doesn't exist fails at task LAUNCH, not at `describe-task-definition` —
   this check must hit SSM directly, not just read the task def back):
   ```bash
   aws ecs describe-task-definition --task-definition archimedes-backend \
     --query 'taskDefinition.containerDefinitions[?name==`backend`].secrets | [0]' \
     --output table

   for p in DATABASE_URL REDIS_URL EMAIL_ENCRYPTION_KEY AURORA_MASTER_PASSWORD; do
     aws ssm get-parameter --name "/archimedes/prod/$p" --query 'Parameter.Name' \
       --output text >/dev/null 2>&1 \
       && echo "OK: SSM /archimedes/prod/$p exists" \
       || { echo "FAIL: SSM /archimedes/prod/$p is MISSING — see Phase 0 blocker 3"; exit 1; }
   done
   ```
3. **The served nginx bundle actually has the Circle client key baked in.**
   This is invisible to `describe-task-definition` — `VITE_CIRCLE_CLIENT_KEY`
   is compiled into the static JS by Vite at BUILD time (`nginx/Dockerfile`'s
   `ui-build` stage), never read from a runtime env var or SSM secret. Verify
   the ECR image the task definition actually references was built with a
   real key:
   ```bash
   # Requires the real VITE_CIRCLE_CLIENT_KEY value (the same value set as the
   # GitHub repo secret — `gh secret list` only proves the secret EXISTS, it
   # never reveals the value; pull the real value from wherever Dan stored it,
   # e.g. 1Password, per README "Security notes").
   BUNDLE_JS=$(curl -sS https://archimedes-arc.com/ | grep -oE '/assets/index-[A-Za-z0-9]+\.js' | head -1)
   curl -sS "https://archimedes-arc.com${BUNDLE_JS}" | grep -c "<the real VITE_CIRCLE_CLIENT_KEY value>"
   # expect: >= 1 (the literal key string appears in the minified bundle). A
   # result of 0 means this image was built with an empty/placeholder key —
   # the GH repo secret is missing/misnamed, or was added AFTER this image was
   # already built (see deploy.yml's "Build nginx image" step) — rebuild +
   # re-push before proceeding.

   # No-secret-needed fallback (same practice docs/deployment-runbook.md § 4
   # already uses): load https://archimedes-arc.com in a browser and confirm
   # the "Connect Wallet" button renders with zero console errors — the
   # Circle SDK no-ops/throws silently on an empty key, so a rendering button
   # is strong (not airtight) evidence the key made it into the bundle.
   ```

If any check above fails, **stop** — fix the gap (env var, SSM parameter, or
rebuild the image with the missing build-arg) and re-verify before touching
the ALB target in Phase 4.

---

## Phase 4 — Cut over: swing the ALB target from EC2 to Fargate (Option C — hard cutover, no soak)

**Decided 2026-07-07: Option C.** Dan will NOT run a parallel-traffic soak.
Once Fargate is verified healthy (Phase 3) **and** the pre-cutover env-parity
checklist above is fully green, cut over immediately: flip the ALB target,
verify, and fix forward anything that surfaces — rather than blending traffic
for days first. The EC2 box stays running (not terminated) for one deploy
cycle afterward as a short, bounded rollback window (step 3 below, and
"Rollback" further down) — there is no open-ended blended-traffic period
beyond that.

1. Remove (or comment out, then `terraform apply`) `alb.tf`'s
   `aws_lb_target_group_attachment.backend` — this is the ONE line that
   detaches the EC2 instance from `archimedes-backend-tg`; ECS's own
   registrations are untouched by this change (they're managed by the ECS
   service, not by that Terraform resource).
   ```bash
   terraform plan   # should show exactly ONE resource destroyed:
                     # aws_lb_target_group_attachment.backend — nothing else
   terraform apply
   ```
2. Confirm 100% of target-group traffic is Fargate-only, right after the
   apply (Option C: no waiting period here):
   ```bash
   aws elbv2 describe-target-health \
     --target-group-arn "$(aws elbv2 describe-target-groups \
        --names archimedes-backend-tg --query 'TargetGroups[0].TargetGroupArn' --output text)"
   # expect: only `ip`-type (Fargate ENI) targets, zero `instance`-type entries
   ```
   Then immediately smoke-test the live domain end-to-end — not just
   `/health`: log in, open Generate, confirm the wallet-connect UI renders.
   This is the fastest way to catch anything the checklist above missed.
   (`deploy.yml`'s `deploy-ecs` job, issue #1039 C1, already redeploys
   Fargate on every push to `main` — no new CI step is needed at this point;
   the EC2 SSM `deploy` job is retired later, at Phase 8 step 2, once the box
   itself is gone.)
3. Leave the EC2 instance running, attached-to-nothing, for **one full
   deploy cycle** (not multiple days) — a short, bounded rollback window, not
   an open-ended soak. Only after that one cycle confirms Fargate is taking
   every deploy cleanly, proceed to Phase 8 (EC2 decommission). Don't
   terminate the instance in the same sitting as the ALB swing.

---

## Phase 5 — Verify zero-downtime during a rolling deploy

Acceptance criterion #2: **zero ALB 5xx during a rollout.** Exercise it
deliberately, don't just assume the `deployment_minimum_healthy_percent =
100` / `deployment_maximum_percent = 200` config (in `ecs.tf`) does the right
thing — measure it.

> **This did not hold in practice — see issue #1309.** A real deploy on
> 2026-08-20 showed the old target deregistered ~9s after the new task
> started, ~2.4 minutes before the new target registered healthy — a live
> violation of this criterion, read off the **ECS service event timeline**
> (`23:59:04 started 1 tasks` → `23:59:13 deregistered 1 targets`) recorded
> in the issue, and independently corroborated on 2026-08-30 by a `504` on
> `/health` at 90.4s during a deploy. It was **not** caught by the
> copy/paste probe this section used to print, and could not have been: that
> snippet sent `-H "Host: archimedes-arc.com"` to `https://$ALB_DNS/health`,
> and a `Host:` header is set *after* the TLS handshake — it changes neither
> the SNI nor the name the certificate is verified against, both of which
> come from the URL. Against the ALB's `archimedes-arc.com`-only listener
> certificate (`aws_acm_certificate.main`, `infra/alb.tf`) every such request
> fails at the TLS layer and reports `000`, so that loop could only ever have
> printed a total outage — on a healthy service just the same. The committed
> script below uses `--connect-to archimedes-arc.com:443:$ALB_DNS:443` so the
> connection still goes straight to the ALB (bypassing CloudFront) while SNI,
> certificate verification and `Host` all stay `archimedes-arc.com`, and it
> takes a preflight probe that must return `200` before it will trigger a
> deploy — so a probe that cannot reach the ALB fails as a broken
> measurement instead of as a fake outage.
>
> `infra/ecs.tf` gained a Docker `healthCheck` on the `nginx`
> container (previously the ONLY essential container without one, despite
> being the actual ALB target) as a defensible hardening — see that file's
> comment for the reasoning — but this was **not proven against a live
> deploy** (no AWS credentials in the environment that made the change). Run
> the script below across a real deploy to confirm the fix, and if it still
> fails, the next things to check in order: (1) whether the LIVE service's
> applied `deploymentConfiguration` / target group health-check settings
> actually match this file's current `infra/ecs.tf` / `infra/alb.tf` (config
> drift — `deploymentConfiguration` is not in the service's
> `lifecycle.ignore_changes`, so it only takes effect on the next `terraform
> apply`, which is a manual, Dan-only step per this runbook's own header);
> (2) `desiredCount=2` as the acceptance-criteria's own named "blunt
> alternative" (recurring Fargate cost — an infra spend commitment, Dan's
> call per `CLAUDE.md` § "When to ask before acting").

**Truth in labeling — this INSTRUMENTS the deploy window, it does not close
it.** The nginx `healthCheck` (`infra/ecs.tf`) gives the one container that
is actually the ALB target a health signal it never had, and the script below
makes the acceptance criterion a runnable measurement instead of prose. That
is the whole of it. Every knob that actually governs the gap the #1309
incident measured is **untouched by this change**: the backend target group
(`aws_lb_target_group.backend`, `infra/alb.tf`) still sets no
`deregistration_delay`, so it keeps the AWS default of 300s — and that value
governs how long a *draining* target holds in-flight connections, not when
deregistration begins, so it was never the lever here anyway;
`healthy_threshold = 2` × `interval = 30` is unchanged,
so a replacement target still needs ≥60s of passing checks before the ALB will
route to it; and `deployment_minimum_healthy_percent = 100` /
`deployment_maximum_percent = 200` (`infra/ecs.tf`) are unchanged. **Issue
#1309 stays open.** It closes on a measured, passing run of the script across
a real deploy — not on this merge.

**And it is INERT until someone applies it.** The `healthCheck` lives in
`aws_ecs_task_definition.backend`, so merging changes nothing about running
infrastructure by itself. Two things have to happen, in order: (1) a manual
`terraform apply` — Dan-only, per this runbook's own header — to register a
new task-definition revision that contains the check; and only then (2) CI
carries it forward, because `.github/workflows/deploy.yml`'s `deploy-ecs` job
clones the **currently registered** task definition (`aws ecs
describe-task-definition`, `deploy.yml:633`) and rewrites only the three image
fields — it never re-derives the definition from `infra/ecs.tf`. So a
merged-but-unapplied `healthCheck` is invisible to every subsequent deploy,
and a probe run before that apply is measuring the OLD behavior. Do not read
such a run — pass or fail — as evidence about this change.

**Both health endpoints in play are LIVENESS signals, not readiness signals.**
`/nginx-health` (`nginx/nginx.conf`) returns a literal `200 "ok"` straight from
nginx: it proves nginx is listening with a validly-rendered config, and
nothing whatsoever about backend or auth. `/health` (the chain-disconnected
branch of the handler in `backend/archimedes/main.py` — grep
`HEALTH_CHAIN_DISCONNECTED`, cited by name because line numbers drift)
returns **200 while degraded, by design** — when the Arc RPC is unreachable
it logs `HEALTH_CHAIN_DISCONNECTED` and still answers 200, deliberately, so a
transient RPC blip cannot cascade the whole ECS service down (#1039 N2). That
is a contract, not a bug — but it means a 200 from `/health`, whether observed
by the container check, the ALB target group, or this script's probe loop,
does **not** mean "ready to serve correct answers". A green run below proves
the criterion as written — "the ALB always had something answering 200" — not
"the service was fully functional throughout". If you need a readiness signal,
add an endpoint that fails closed; do not tighten these two.

```bash
cd infra   # requires the S3 backend already initialized, or pass --alb-dns/--cluster/--service explicitly
./scripts/verify-zero-downtime-deploy.sh
```

Takes a preflight probe and refuses to trigger anything unless it is a `200`
(a probe that cannot reach a healthy ALB would log nothing but non-200s and
then blame the rollout for them), then runs a 1 req/s probe loop against the
ALB directly (bypassing CloudFront, so it measures the target group, not the
CDN cache) for the whole rollout window, triggers a `force-new-deployment`,
waits for steady state, and exits non-zero with every offending log line if
any request was not a `200` — see the script's own header comment for flags
and what it does and does not prove. If `aws ecs wait services-stable` itself
gives up (the circuit-breaker-rollback case), the run does not die on that
exit code: it still stops the probe, scores the window, prints the log path,
and reports `INCONCLUSIVE` — never `PASS`. (This replaces the earlier copy/paste-bash version of this
procedure — same acceptance check, now a committed, reusable artifact
instead of prose that only exists if someone remembers to type it out.)

---

## Phase 6 — Verify self-heal (kill a task, time the recovery)

Acceptance criterion #3: **killing a task → ECS replaces it healthy in <2
min, no human action.**

```bash
CLUSTER="$(terraform output -raw ecs_cluster_name)"
SERVICE="$(terraform output -raw ecs_service_name)"

TASK_ID=$(aws ecs list-tasks --cluster "$CLUSTER" --service-name "$SERVICE" \
  --query 'taskArns[0]' --output text)
echo "Killing $TASK_ID at $(date -u +%H:%M:%S)"

aws ecs stop-task --cluster "$CLUSTER" --task "$TASK_ID" \
  --reason "manual self-heal drill (#1039 acceptance criterion 3)"

# The service's own desiredCount is the target to watch back up to — read it
# fresh rather than assuming it (autoscaling may have moved it since apply).
DESIRED=$(aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" \
  --query 'services[0].desiredCount' --output text)

# Poll until a NEW task (different ARN) is RUNNING and healthy — this is the
# measurement, not `aws ecs wait services-stable` alone (which can report
# stable on the surviving task before the replacement is actually healthy
# in the target group).
for i in $(seq 1 24); do
  RUNNING_TASKS=$(aws ecs list-tasks --cluster "$CLUSTER" --service-name "$SERVICE" \
    --desired-status RUNNING --query 'length(taskArns)' --output text)
  if [ "$RUNNING_TASKS" -ge "$DESIRED" ]; then
    echo "Back to desired RUNNING count ($DESIRED) at $(date -u +%H:%M:%S) (attempt $i)"
    break
  fi
  sleep 5
done

# Confirm the replacement is healthy in the target group too, not just RUNNING:
aws elbv2 describe-target-health \
  --target-group-arn "$(aws elbv2 describe-target-groups \
     --names archimedes-backend-tg --query 'TargetGroups[0].TargetGroupArn' --output text)"
```

**Verify:** the elapsed time between the `stop-task` timestamp and the
replacement task reaching `RUNNING` + `healthy` is under 2 minutes. If it
isn't, check `health_check_grace_period_seconds` (90s in `ecs.tf`) and the
ALB target group's own health-check interval/threshold in `alb.tf` before
concluding the criterion is unmet — both feed the total.

### NAT-kill drill (issue #1039 N1/N3 — self-heal isn't just an ECS-task story)

The task-kill drill above proves the ALB/service layer self-heals. The NAT
instances (`aws_instance.nat`, `vpc.tf`) are a SEPARATE single point of
failure for both private subnets' egress (ECR pulls, Bedrock, Arc RPC,
Aurora/ElastiCache client traffic) — drill their self-heal deliberately too,
don't just assume the `infra/cloudwatch.tf` `nat-status-check-failed` alarm
+ `ec2:recover` action wired for N1 works:

```bash
# No dedicated Terraform output for the NAT instance ids — look them up by
# their Name tag (vpc.tf: "${var.project_name}-nat-${az}").
NAT_ID=$(aws ec2 describe-instances --filters "Name=tag:Name,Values=archimedes-nat-*" "Name=instance-state-name,Values=running" \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)
echo "Stopping $NAT_ID at $(date -u +%H:%M:%S) — simulates a NAT-down outage"

aws ec2 stop-instances --instance-ids "$NAT_ID"
aws ec2 wait instance-stopped --instance-ids "$NAT_ID"

# Confirm the OTHER AZ's NAT keeps that AZ's private subnet alive (this is
# why there are two, one per AZ, not one shared) — tasks in the stopped AZ's
# subnet lose egress until this NAT is back; tasks in the other AZ's subnet
# are unaffected. Watch the archimedes-nat-status-check-failed-<n> alarm
# (infra/cloudwatch.tf, N1) transition to ALARM in the CloudWatch console or:
aws cloudwatch describe-alarms --alarm-names "archimedes-nat-status-check-failed-0" "archimedes-nat-status-check-failed-1" \
  --query 'MetricAlarms[*].{name:AlarmName,state:StateValue}'

# Recover: aws_instance.nat has no auto-restart on `stop` (only the
# StatusCheckFailed_System + ec2:recover path self-heals a HARDWARE-level
# failure, not an operator `stop-instances` — this drill exercises detection
# via the alarm, not automatic recovery from a stop). Manually restart to end
# the drill:
aws ec2 start-instances --instance-ids "$NAT_ID"
aws ec2 wait instance-running --instance-ids "$NAT_ID"
```

**Verify:** the corresponding `archimedes-nat-status-check-failed-<n>` alarm
transitions to `ALARM` within the alarm's own 2-minute evaluation window
(N1, `infra/cloudwatch.tf`) and pages the SNS topic. See
`infra/runbooks/disaster-recovery.md` § "NAT instance down" for the full
detect → recover playbook this drill exercises (added alongside this same
issue).

---

## Phase 7 — Verify ECS Exec (acceptance criterion #4)

```bash
CLUSTER="$(terraform output -raw ecs_cluster_name)"
SERVICE="$(terraform output -raw ecs_service_name)"
TASK_ID=$(aws ecs list-tasks --cluster "$CLUSTER" --service-name "$SERVICE" \
  --query 'taskArns[0]' --output text)

aws ecs execute-command --cluster "$CLUSTER" --task "$TASK_ID" \
  --container backend --interactive --command "/bin/sh"
```

Once inside, confirm logs are actually landing in CloudWatch (not just the
ECS Exec session I/O):

```bash
aws logs tail /archimedes/app --since 10m --follow
aws logs tail /archimedes/nginx --since 10m --follow
```

**Verify:** the shell opens, and a redeploy (Phase 5's `force-new-deployment`,
or a normal CI deploy) does not clear the log group — old tasks' log streams
persist alongside the new task's stream under the same group, satisfying
"logs survive a redeploy."

**Exercise the rollback acceptance criterion once, deliberately** while
you're in this verification pass: deploy a deliberately-broken task
definition revision (e.g. a bad image tag) and confirm
`deployment_circuit_breaker` auto-rolls-back without human action:

```bash
aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" \
  --query 'services[0].deployments'
```

---

## Phase 8 — Decommission the EC2 app instance

Only after Phase 4 is confirmed for at least one full deploy cycle with
Fargate carrying 100% of traffic. **Reordered (issue #1039 R1): the
runner-replacement gate is now step 1, and terminating the EC2 instance is
the LAST step — an earlier draft of this runbook put "terminate" first and
the runner check second, which is backwards. Do not terminate the box while
step 1's gate is still red.**

1. **HARD GATE — confirm the oracle/agent/kb-runner background daemons have
   somewhere to run, with a concrete verification command, BEFORE touching
   the EC2 instance at all.** **REPLACED 2026-07-08 (issue #1065):** the
   original version of this gate checked for three ECS *services* named
   `archimedes-oracle` / `archimedes-agent` / `archimedes-kb-runner` — that
   was always aspirational and doesn't match what actually got built.
   oracle + agent are funds-adjacent, exactly-once singletons
   (`services/runner_lease.py` is the app-layer Redis lease control) that
   live on their OWN dedicated EC2 instance (`infra/runner_ec2.tf`) —
   deliberately NOT an ECS service, NOT autoscaled, NOT an ASG (duplicating
   either process risks a double-signed on-chain tx). kb-runner is a
   scheduled (batch), not long-running, Fargate task
   (`infra/kb_runner.tf` + an EventBridge Scheduler schedule) — it has no
   `aws ecs describe-services` entry at all; it exists only as task
   invocations on its schedule. Decommissioning the old box while these
   still only run there (as `docker compose` services) would silently kill
   the vault/regime-state source of truth — this is still the one step in
   this runbook that is NOT just "detach and delete," just checked
   differently:

   ```bash
   # 1. Oracle + agent EC2 — must be running.
   aws ec2 describe-instances \
     --instance-ids "$(terraform output -raw runner_instance_id)" \
     --query 'Reservations[0].Instances[0].State.Name' --output text
   # Expect: running

   # 2. Oracle + agent — each completed >= 1 successful on-chain action in
   #    the last hour (price push / rebalance), not just "the process is up."
   aws logs tail "$(terraform output -raw runner_log_group_name)" \
     --since 1h --filter-pattern "oracle" | grep -i "price push complete" \
     || echo "GATE FAILS: no oracle price push in the last hour"
   aws logs tail "$(terraform output -raw runner_log_group_name)" \
     --since 1h --filter-pattern "agent" | grep -i "rebalance" \
     || echo "GATE FAILS: no agent rebalance activity in the last hour"

   # 3. kb-runner schedule — must be ENABLED, with >= 1 successful invocation.
   aws scheduler get-schedule --name "$(terraform output -raw kb_runner_schedule_name)" \
     --query 'State' --output text
   # Expect: ENABLED
   aws logs tail "$(terraform output -raw kb_runner_log_group_name)" --since 24h \
     | grep -i "manifest" || echo "GATE FAILS: no kb-runner manifest.json write logged in the last 24h"

   # 4. Both CloudWatch alarms show OK, not INSUFFICIENT_DATA.
   aws cloudwatch describe-alarms \
     --alarm-names archimedes-runner-ec2-status-check-failed archimedes-kb-runner-failed \
     --query 'MetricAlarms[].{Name:AlarmName,State:StateValue}' --output table
   ```

   **Gate passes only if:** the runner EC2 is `running`; both oracle and
   agent show real on-chain activity in the last hour (not just process
   liveness); the kb schedule is `ENABLED` with at least one completed
   invocation; and both alarms are `OK`. Any failure — STOP. Do not proceed
   to step 2 or 3. (Full step-by-step verification detail: issue #1065 Step
   3 of the execution checklist.)
2. Remove the now-dead `EC2_INSTANCE_ID` / SSM `deploy` job from `deploy.yml`
   entirely — the `deploy-ecs` job (issue #1039 C1) has independently
   redeployed Fargate on every push since before Phase 4 ran, so this step is
   purely deleting the now-unreachable EC2 code path, not changing behavior.
   **Done:** the SSM `deploy` job went in the #1039 fast-follow; the orphaned
   `EC2_INSTANCE_ID` env — which outlived its instance by twelve days after the
   2026-08-19 decommission — was deleted 2026-08-31.
   (Separately, once step 1's gate is green, `.github/workflows/deploy-runners.yml`'s
   `push` trigger can be uncommented — see that workflow's header — so runner
   deploys stop being a manual `workflow_dispatch`.)
3. **Only now, last:** terminate `aws_instance.archimedes` (remove from
   `main.tf`, `terraform apply`). Aurora/ElastiCache/ALB/WAF/CloudFront/
   Route 53 are all unaffected; this chunk never touched them. The NEW
   runner EC2 (`aws_instance.runner`, `infra/runner_ec2.tf`) is a SEPARATE
   resource and is not affected by this termination.

---

## Rollback (bad Fargate deploy, EC2 still attached — Phases 1–4 window only)

Because the EC2 target stays attached to `archimedes-backend-tg` throughout
Phases 1–4, the cheapest rollback during the cutover window is simply:
drain Fargate to zero tasks so the ALB serves 100% of traffic from the
still-healthy EC2 target, with no DNS change and no ALB reconfiguration.
Under the Option C hard cutover (Phase 4), this window is short and
bounded — Stage 2a/2b + Phase 3 verification + the pre-cutover env-parity
checklist, not a multi-day soak — but the mechanism below is exactly the
same regardless of how long that window ends up being.

**This MUST be done via the AWS CLI against the live service, NOT by
changing `var.ecs_service_desired_count` and re-running `terraform apply`**
(issue #1039 C2 — a real bug in an earlier draft of this runbook). The
service has `lifecycle { ignore_changes = [task_definition, desired_count]
}` (`infra/ecs.tf`) specifically so CI's deploys and autoscaling aren't
reverted by a later `terraform apply` — but that same guard means a
Terraform-side `desired_count` change is a **silent no-op** against the
running service. Setting `TF_VAR_ecs_service_desired_count=0` and applying
will NOT drain any tasks; you'll walk away believing you rolled back while
Fargate keeps serving. The only thing that actually changes the running
service's desired count is the CLI, direct against the service:

```bash
aws ecs update-service \
  --cluster "$(terraform output -raw ecs_cluster_name)" \
  --service "$(terraform output -raw ecs_service_name)" \
  --desired-count 0
```

To bring Fargate back afterward (once the bad revision is fixed), the same
CLI form with a positive count — again, not a Terraform apply:

```bash
aws ecs update-service \
  --cluster "$(terraform output -raw ecs_cluster_name)" \
  --service "$(terraform output -raw ecs_service_name)" \
  --desired-count 1
```

**After Phase 4 (EC2 detached), rollback is the standard ECS rollback**: the
`deployment_circuit_breaker` (Phase 7) already does this automatically for a
bad deployment; for a bad deploy that somehow reached steady state anyway,
redeploy the prior task-definition revision:

```bash
aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" \
  --task-definition "$(terraform output -raw ecs_task_definition_family):<prior-revision-number>"
```
