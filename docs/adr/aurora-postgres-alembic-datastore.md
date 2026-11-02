# ADR: Aurora PostgreSQL + Alembic as the datastore, ElastiCache Redis for ephemeral state

> **Audience:** Archimedes team
> **Status:** Accepted
> **Date:** 2026-07-28 (recorded; the datastore choice predates the ADR habit — original decision date [unestablished — needs Dan])
> **Owner:** Dan Browne
> **Supersedes:** —
> **Superseded-by:** —
> **Question being decided:** What is the system of record, how does its schema change, and what holds ephemeral state?
> **Related:** [`infra/aurora.tf`](https://github.com/aprin-labs/archimedes/blob/84c0fac4758e9572f34e4f70fe48e9d68c5acab0/infra/aurora.tf), [`infra/elasticache.tf`](https://github.com/aprin-labs/archimedes/blob/84c0fac4758e9572f34e4f70fe48e9d68c5acab0/infra/elasticache.tf), [`infra/ecs_migrate.tf`](https://github.com/aprin-labs/archimedes/blob/84c0fac4758e9572f34e4f70fe48e9d68c5acab0/infra/ecs_migrate.tf), [`.github/workflows/deploy.yml`](https://github.com/aprin-labs/archimedes/blob/84c0fac4758e9572f34e4f70fe48e9d68c5acab0/.github/workflows/deploy.yml), [`docs/database-architecture.md`](../database-architecture.md), [`ec2-to-ecs-fargate-cutover.md`](ec2-to-ecs-fargate-cutover.md).

## TL;DR

**Aurora PostgreSQL Serverless v2 (engine 18.3) is the system of record; Alembic is the
only way its schema changes; ElastiCache Redis 7.1 holds ephemeral state only.** Migrations
run as a one-off Fargate task *before* the new image is rolled out, so the schema is never
behind the code that expects it. Nothing durable lives in Redis.

## Context

The system's durable state is not incidental: strategies and proposals, backtest results
and daily return series, reasoning traces, the paper corpus and knowledge graph, vaults,
users/identity and the marketplace all need to survive a deploy and be queryable together
([`docs/architecture.md` § 1.8](../architecture.md)). Two properties dominated the choice:

1. **The rigor claims are relational and must be auditable.** A strategy passport joins a
   strategy to its papers, its backtests, its return series and its on-chain anchor. That
   is a join-heavy, integrity-constrained workload — the case for a relational store rather
   than a document store, and the case against keeping any of it in Redis.
2. **Two different lifetimes.** Regime state, in-flight generation traces and the job queue
   are ephemeral: losing them costs a re-run, not an audit failure. Mixing them into the
   system of record would make the durable store noisy and the backup story confusing.

The deploy model forced the migration question. Once the serving tier became a rolling
Fargate deployment ([`ec2-to-ecs-fargate-cutover.md`](ec2-to-ecs-fargate-cutover.md)), a
migration run *by the application at startup* would mean N tasks racing to migrate the same
schema, and a migration run *after* rollout would mean new code briefly hitting an old
schema.

## Decision

1. **Aurora PostgreSQL Serverless v2, engine version `18.3`, `engine_mode = "provisioned"`**
   ([`infra/aurora.tf:57-60`](https://github.com/aprin-labs/archimedes/blob/84c0fac4758e9572f34e4f70fe48e9d68c5acab0/infra/aurora.tf)) — Serverless v2 runs in provisioned
   mode with a `serverless_v2_scaling_configuration`, so the cluster scales with load
   without a fixed instance class. `allow_major_version_upgrade = true` and
   `apply_immediately = true` are both set deliberately: 16.4 → 18.3 is a direct upgrade
   target (no 17.x hop), major upgrades are rejected without the flag, and
   `apply_immediately` makes the change land when `terraform apply` runs instead of
   silently at some unplanned maintenance window — which is the only acceptable behaviour
   for a cluster backing live funds.
2. **Alembic is the sole schema-change mechanism, run as a one-off Fargate task before
   rollout.** [`infra/ecs_migrate.tf`](https://github.com/aprin-labs/archimedes/blob/84c0fac4758e9572f34e4f70fe48e9d68c5acab0/infra/ecs_migrate.tf) defines a dedicated
   single-container task definition (`python -m alembic upgrade head`); the `migrate` job in
   [`deploy.yml`](https://github.com/aprin-labs/archimedes/blob/84c0fac4758e9572f34e4f70fe48e9d68c5acab0/.github/workflows/deploy.yml) `aws ecs run-task`s it and waits for
   exit 0 before `deploy-ecs` force-redeploys the service. It reuses the same image, the
   same execution/task roles and the same SSM-backed secrets as the service — so the
   migration always runs with exactly the code and credentials that are about to serve.
   No application-startup migrations.
3. **ElastiCache Redis 7.1 is for ephemeral state only** —
   [`infra/elasticache.tf:55-57`](https://github.com/aprin-labs/archimedes/blob/84c0fac4758e9572f34e4f70fe48e9d68c5acab0/infra/elasticache.tf), described in the resource
   itself as "regime state, traces, job queue". Single node (`cache.t3.micro`,
   `num_cache_clusters = 1`); multi-AZ is treated as a cost upgrade, not a correctness
   requirement, precisely because nothing durable is stored there.

## Consequences

### Positive
- **Schema is never behind the code.** The gate is structural: `deploy-ecs` does not run
  if `migrate` fails, so a bad migration blocks the rollout rather than half-applying under
  a live service.
- **One migration runner, not N.** Rolling-deploy tasks never race to migrate.
- **The migrate task is correctly shaped.** No nginx sidecar, no health check, no
  `dependsOn` — it runs to completion. (Running migrations against the service task family
  would have booted the whole app and risked ECS killing the task mid-migration as
  unhealthy; see the Copilot review note in `ecs_migrate.tf`.)
- **Redis can be lost without data loss.** A cache flush costs recomputation.
- **Encryption at rest and in transit** are on for Redis (`at_rest_encryption_enabled`,
  `transit_encryption_enabled`).

### Negative / costs we accept
- **Migrations must be backward-compatible with the running code for the length of the
  rollout.** Because migrate runs *before* the new tasks are healthy, the old tasks are
  still serving against the new schema for the duration. Destructive migrations (dropping
  or renaming a column the old code reads) will break production during that window and
  must be split into expand/contract pairs. This is a real constraint on how migrations are
  written and it is not currently enforced by CI.
- **`apply_immediately = true` means a `terraform apply` can restart the cluster.** That is
  the intended trade (deliberate windows over surprise ones), but it makes `apply` an
  operational action, not a routine one.
- **No custom cluster parameter group.** Aurora moves the cluster onto the default
  `aurora-postgresql18` family on upgrade; there is no hand-managed parameter group to
  bump, which is simpler but means we do not control parameter defaults.
- **Single-node Redis is a single point of failure** for regime state and the job queue.
  Acceptable only because it is ephemeral; if anything durable is ever put there, this
  choice must be revisited.
- **Aurora Serverless v2 has a floor cost** and scales less cheaply than a small RDS
  instance at low traffic.

## Alternatives considered
- **Plain RDS PostgreSQL — rejected** for the scaling profile: generation and backtest
  bursts are spiky, and Serverless v2 absorbs them without a manual instance-class change.
- **Application-startup migrations (`alembic upgrade head` in the entrypoint) — rejected**:
  N concurrent tasks racing the same schema, and no way to fail the deploy cleanly.
- **A document store for strategies/traces — rejected**: the passport is a join, and the
  integrity constraints are the product.
- **Redis as a durable store — rejected**: the backup and audit story for anything
  rigor-related has to be the relational store.
