# Deployment — local vs production (one compose file, two topologies)

> **status:** reference
> **owner:** Dan Browne
> **updated:** 2026-08-31
> **superseded-by:** [`local-vs-prod.md`](local-vs-prod.md) (for the local-vs-production contract only)

> Written 2026-06-28, alongside the Aurora/ElastiCache cutover. Resolves the
> drift between "simple `docker compose up` locally" and "how it runs in prod."

> **⚠️ Correction, 2026-08-31 (issue #1044).** Everything below describing the
> **local** side, the `localdb` profile gate, and the nginx runtime-DNS fix is still
> accurate. Everything describing the **production** side is the retired EC2 era:
> production no longer runs `docker compose` at all. `deploy.yml` registers an ECS
> task definition and calls `ecs update-service`; `deploy-runners.yml` does
> `docker pull` + `systemctl restart` over SSM for the oracle/agent runners. The
> "Production deploy flow" and "One-time step on the live box" sections below are
> kept for their incident history, not as instructions — do not execute them. See
> [`adr/ec2-to-ecs-fargate-cutover.md`](adr/ec2-to-ecs-fargate-cutover.md) for the
> decision and [`local-vs-prod.md`](local-vs-prod.md) for the current contract.

## The two topologies

*(The right-hand column is the pre-Fargate topology — see the correction above.)*

| | Local dev | Production (single EC2 — **retired**) |
|---|---|---|
| Command | `docker compose up -d` | `docker compose up -d --force-recreate --remove-orphans` (via `deploy.yml`) |
| Data stores | **in-stack** `postgres` + `redis` containers | **managed** Aurora Serverless v2 + ElastiCache |
| Switch | `.env` has `COMPOSE_PROFILES=localdb` | `.env` omits `COMPOSE_PROFILES` |
| `DATABASE_URL` | `…@postgres:5432/…` | `…@archimedes-aurora…:5432/…?sslmode=require` |
| `REDIS_URL` | `redis://redis:6379/0` | `rediss://…elasticache…:6379/0` (TLS) |

**How one file serves both:** `postgres` and `redis` are gated behind
`profiles: ["localdb"]` in `docker-compose.yml`. The `localdb` profile is active
**only** when `COMPOSE_PROFILES=localdb` is in the `.env` (which `.env.example`
ships, so local dev is unchanged — `cp .env.example .env && docker compose up`
still brings up the full self-contained stack). Production's box-local `.env`
omits `COMPOSE_PROFILES`, so those two services never start and the app tier
(`backend`, `auth`, `oracle`, `agent`, `kb-runner`, `nginx`) runs against managed
Aurora + ElastiCache via the URLs in `.env`. The app services declare
`depends_on … { required: false }` so they boot even when the local DB/cache
isn't present (prod). *(Needs docker compose ≥ 2.20 for `required:` on
`depends_on`.)*

## Why this shape

Before this change, prod ran the **base** stack (in-stack postgres/redis) even
though the app `.env` already pointed at managed Aurora/ElastiCache after the
2026-06-28 cutover — so the box **double-ran** idle docker databases (paying for
managed *and* burning EC2 RAM on unused containers). Profile-gating makes the
managed-stores intent explicit and stops the waste, without forking the compose
file or changing the local workflow.

## nginx runtime DNS re-resolution (the recreate-502 fix)

`nginx/nginx.conf` previously declared `upstream backend_api { server
backend:8000; }`. nginx resolves that hostname to an IP **once at config-load**
and caches it. When `--force-recreate` gives the `backend` container a **new**
IP, nginx keeps proxying the dead IP → **every request 502s** until nginx is
restarted (this is exactly what happened during the cutover when the backend was
recreated without nginx). The fix adds a `resolver 127.0.0.11` (docker's
embedded DNS) + `zone` + the `resolve` parameter so nginx re-resolves `backend`
at **runtime** (every 10s) — a backend recreate now self-heals. A full
`deploy.yml` run recreates *all* services (nginx after backend), so it was
already safe; this fix additionally makes **partial** recreates (operational
fixes) safe.

## Production deploy flow (`deploy.yml`)

**Updated 2026-07-06 (issue #1039, P1 — "stop the bleeding"); updated
2026-07-07 (#1039 P4, build-chunk 3 — Alembic migrate stage).** Building the
image on the serving host (`docker compose build`) was the root cause of 3
production outages on 2026-07-06, all OOM: a build on top of a running stack
overruns the 2 vCPU / 4 GB box, the OOM killer takes out the SSM agent +
backend, and there's no registry to roll back to. The pipeline is now
build-in-CI → push to ECR → migrate → pull-on-box:

1. Push to `main` → (gated on `DEPLOY_ENABLED=true`) → the `build-and-push`
   job builds backend (`backend/Dockerfile`), Better Auth (`auth/Dockerfile`),
   and nginx/frontend (`nginx/Dockerfile`) images on the **CI runner**, boots them in a throwaway
   container and curls `/health` (nginx through its full reverse-proxy path,
   including runtime DNS resolution to a `backend` container) — a broken
   image is caught **here**, before it ever reaches ECR or the serving host —
   then tags each `<commit-sha>` and `latest` and pushes to ECR
   (`archimedes-backend`, `archimedes-auth`, `archimedes-nginx`).
2. The `migrate` job (needs: `build-and-push`) runs `alembic upgrade head` as
   a one-off ECS Fargate task, **before** `deploy` — the ordered pre-rollout
   migrate stage from #1039 P4 (shared with #1028 Phase A). As of this PR,
   Alembic hasn't landed yet (`backend/migrations/` is still hand-rolled
   `.sql` + `archimedes.db.init_db()`'s idempotent patches), so the job
   detects `backend/alembic.ini`'s absence and no-ops loudly rather than
   failing or faking a migration — see the job's own comments in `deploy.yml`
   and `infra/runbooks/ecs-fargate-cutover.md` § "Alembic pre-rollout migrate
   stage" for the full mechanics.
3. The `deploy` job (needs: `[build-and-push, migrate]`) OIDCs into AWS and
   SSMs the EC2: `git reset --hard origin/main` (the gitignored `.env` is
   **untouched**, so the managed-store URLs persist), sets
   `ECR_REGISTRY`/`IMAGE_TAG` in `.env`, logs the box in to ECR via its own
   instance role, then `docker compose pull && docker compose up -d
   --force-recreate --remove-orphans` — **no `docker build` ever runs on the
   serving host** — then seed/hydrate/AMM bootstrap + a `/health` check.

`docker-compose.yml`'s app-tier services (`nginx`, `auth`, `backend`, `oracle`,
`agent`, `kb-runner`) now carry an explicit `image:` (in addition to the
existing `build:`, unchanged for local dev) pointing at the ECR-hosted tag —
see the comment block at the top of that file.

**Known residual (not closed by this PR — tracked as issue #1039 chunk 2):**
the EC2 instance role (`archimedes-backend-role`) does not yet have ECR pull
permissions (`ecr:GetAuthorizationToken`, `ecr:BatchGetImage`,
`ecr:GetDownloadUrl`, `ecr:BatchCheckLayerAvailability`), and the box does not
yet have `awscli` installed. Until both land (Terraform in `infra/ec2_iam.tf`
+ `infra/README.md`), the deploy step's `docker login`/`docker compose pull`
fails closed with an explicit `ERROR_ECR_LOGIN_FAILED` / `ERROR_ECR_PULL_FAILED`
message rather than silently falling back to a local build.

## One-time step on the live box (after merging this change)

The live EC2 currently still has the idle `postgres`/`redis` containers running
(left intact as the cutover rollback). After this PR merges and you're confident
the managed stores are stable, reclaim the resources.

**These are two separate steps. The second command runs _inside_ the interactive
SSM session, on the EC2 — do NOT run `docker compose stop` on your laptop.**

1. Open an interactive shell on the live box (substitute the real instance id /
   region — the current values are in the `prod-infra-reality` notes / `CLAUDE.md`
   AWS section):

   ```bash
   aws ssm start-session --target <INSTANCE_ID> --region <REGION>
   ```

2. **Now at the EC2 shell prompt** (the prompt changes to the instance's
   hostname), stop the idle containers:

   ```bash
   cd /opt/archimedes && docker compose stop postgres redis   # or: docker rm -f
   ```

(The `pgdata` volume persists — data isn't deleted — but once the cutover is
trusted it's just dead weight. Keep it until then as the rollback path.)

Better Auth migration, rollout, smoke checks, and non-destructive rollback are
specified in [`account-authentication.md`](account-authentication.md).

## ⚠️ Merge note

`DEPLOY_ENABLED=true` — **merging this auto-deploys.** Merge in a controlled
window with someone watching the deploy, **not** right before a demo/relaunch.
The nginx-resolver part is low-risk; the profile-gating changes which services
prod starts — validate the `/health` 200 after deploy and have the rollback
(revert + redeploy) ready.
