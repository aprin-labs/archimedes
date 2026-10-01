# ADR: Move the serving tier from a single EC2 box to ECS Fargate

> **Audience:** Archimedes team
> **Status:** Accepted
> **Date:** 2026-07-09 (executed; decided in [#1039](https://github.com/aprin-labs/archimedes/issues/1039))
> **Owner:** Dan Browne
> **Supersedes:** —
> **Superseded-by:** —
> **Question being decided:** Does the backend keep running as docker-compose on one long-lived EC2 instance, or move to a managed container service?
> **Related:** [`infra/ecs.tf`](../../infra/ecs.tf), [`infra/ecs_migrate.tf`](../../infra/ecs_migrate.tf), [`infra/alb.tf`](../../infra/alb.tf), [`.github/workflows/deploy.yml`](../../.github/workflows/deploy.yml), [`infra/runbooks/ecs-fargate-cutover.md`](../../infra/runbooks/ecs-fargate-cutover.md), [`docs/architecture.md` § 7](../architecture.md).

## TL;DR

**The web tier is an ECS Fargate service behind the existing ALB.** One task runs the same
two containers that used to share the EC2 box — nginx on `:8080` (the ALB target) and the
FastAPI backend on `:8000` — in `awsvpc` network mode, so they share one ENI and talk over
`localhost`. Images are built in CI and pushed to ECR; Alembic runs as a one-off Fargate
task before rollout; deploy is a `force-new-deployment`. **No `aws ssm send-command`
remains in the deploy path.** The old EC2 instance is detached from the target group but
still running as a rollback window.

## Context

Until 2026-07, production was one EC2 instance running `docker-compose`, and deploying
meant `aws ssm send-command` into that box to `git pull` and rebuild images **on the
serving host**. Three properties of that arrangement were the problem:

1. **Build and serve shared one machine's RAM.** A `docker build` on the serving host
   competes with the process serving traffic. This is what produced the OOM outage chain
   on **2026-07-06** ([#1001](https://github.com/aprin-labs/archimedes/issues/1001)) — the
   proximate trigger for the whole cutover. The first fix landed in the same issue thread:
   build in CI, push to ECR, stop building images on the serving host
   (commit `d62e449`-lineage, "[infra] Build in CI, push to ECR, and stop building images
   on the serving host (#1039 P1)", 2026-07-06).
2. **Deploy was imperative and racy.** Two merges landing close together could run two
   SSM builds against the same box; a guard for that double-deploy race had to be written
   (`7325845`, "[infra] Guard the SSM EC2 deploy against a concurrent double-deploy race
   (#1039 C3)") — a patch on a mechanism that shouldn't need one.
3. **The serving host was a pet.** State lived in its git checkout and its bind mounts, so
   the host could not be replaced without ceremony, and a host failure was an outage rather
   than a task restart.

Fargate was chosen over an EC2 ASG behind the same ALB (`infra/asg.tf` exists and is now
legacy) because the ASG keeps the host as a unit of management — patching, AMIs, user-data
drift — for a workload that is one stateless container pair. Kubernetes was not considered
proportionate for a single service at this size.

## Decision

**Run the web tier as an ECS Fargate service, and make deploy declarative.**

1. **One task, two containers, one ENI** ([`infra/ecs.tf`](../../infra/ecs.tf)). `awsvpc`
   mode gives nginx and backend the same network namespace, so nginx's upstream is
   `localhost:8000` — the same shape as two processes on one host, which is what made this
   a low-semantic-change move rather than a re-architecture.
2. **The ALB, listener and target group are reused unchanged** from
   [`infra/alb.tf`](../../infra/alb.tf). The target group was already `target_type = ip`
   with a `GET /health` check, which is exactly what Fargate registers. No `alb.tf`
   resource was touched by the cutover.
3. **Deploy is three jobs** ([`.github/workflows/deploy.yml`](../../.github/workflows/deploy.yml)):
   `build-and-push` (CI builds, pushes to ECR) → `migrate` (`aws ecs run-task` on the
   dedicated migrate task definition, line ~470) → `deploy-ecs`
   (`aws ecs update-service ... --force-new-deployment`, line ~596). Auth is OIDC; there
   are no long-lived AWS keys in Actions, and **zero `aws ssm send-command` calls**.
4. **Migrations run as a separate single-container task definition**
   ([`infra/ecs_migrate.tf`](../../infra/ecs_migrate.tf)), not as a command override on the
   service task-def. Reason, from the Copilot review on PR #1041: the service family
   defines an nginx sidecar with `dependsOn { backend, HEALTHY }`, and a migrating backend
   never serves `/health`, so ECS would kill the task mid-migration. The migrate task has
   no sidecar, no health check, no `dependsOn`; it runs to completion and stops.
5. **The image became self-contained.** `backend/Dockerfile`'s `COPY . /app` only covered
   the `backend/` build context, while strategies, corpus and ABIs arrived on EC2 via
   docker-compose host bind mounts. Fargate has no host filesystem to bind-mount from, so
   those assets are baked into the image (`ab87fdb`, "[infra] Self-contained backend image:
   bake file-backed assets for Fargate (#1039)"), with the corpus PDFs excluded from the
   root build context (`4504589`).

**Execution:** PRs [#1056](https://github.com/aprin-labs/archimedes/pull/1056)–[#1059](https://github.com/aprin-labs/archimedes/pull/1059),
2026-07-08/09. #1041 landed the Terraform; #1056 detached EC2 from the target group
(`6689412`, "Phase 4 cutover"); #1057 fixed the migrate task's network configuration;
#1058 added the version stamp + rich-health CI gate and retired the EC2 deploy path
(`d62e449`); #1059 made the Aurora 18.3 upgrade safe (`7cb1bc4`).

## Consequences

### Positive
- **Build no longer competes with serve.** The #1001 failure mode is structurally gone,
  not mitigated — CI builds, the task only runs.
- **Deploy is declarative and idempotent.** `force-new-deployment` with a rolling
  replacement, gated on a health check, replaces an imperative remote shell command. The
  double-deploy race guard is no longer load-bearing.
- **Rollback is a task-definition revision**, not a rebuild.
- **No long-lived AWS credentials in CI** (OIDC), and secrets resolve from SSM at task
  start rather than being fetched by a boot script.

### Negative / costs we accept — recorded honestly
- **The old EC2 box is detached but still running.** It is out of the ALB target group and
  serves no traffic, but it is not terminated: it is the rollback window. Phase-8
  decommission is still pending. Until it is torn down we are paying for it and it remains
  a live host with production credentials on it.
- **The background runners were stranded on that box.** The oracle updater, the agent
  runner and the KB pipeline all ran as docker-compose services on EC2 and were not part
  of the web-tier task. After the cutover they had **no deploy path**. Relocation IaC
  (oracle + agent → a small dedicated EC2, KB → a scheduled Fargate task, EFS for corpus
  artifacts) was written as PR [#1071](https://github.com/aprin-labs/archimedes/pull/1071)
  (merged 2026-07-14, `bb0c345`/`0f1d8cb`) and **applied 2026-07-28**. This was a
  foreseeable consequence of cutting over the web tier alone and it cost ~3 weeks of the
  runners having no deployment story; a future cutover should enumerate every
  docker-compose service, not just the ALB-fronted ones.
- **Two config paths existed during the transition.** `nginx.conf`'s upstream is correct
  as `backend:8000` under docker-compose's bridge network and wrong under `awsvpc`; that
  divergence had to be carried until the EC2 path was retired.
- **Fargate has no host filesystem.** Anything that was a bind mount is now either baked
  into the image (and therefore only updatable by a deploy) or on EFS.
- **Cold-start and per-task cost** are worse than a warm long-lived box for a
  low-traffic service. Accepted in exchange for the operational properties above.

## Alternatives considered
- **Keep EC2, only move the build to CI — rejected as insufficient.** It fixes #1001's
  proximate cause (and was in fact shipped first, as P1) but leaves the pet host, the
  imperative SSM deploy and the race guard in place.
- **EC2 Auto Scaling Group behind the same ALB — rejected.** Keeps host management for a
  stateless container workload; the ALB reuse benefit is identical either way.
- **EKS / Kubernetes — rejected as disproportionate** for one service.
