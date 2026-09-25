# Infrastructure Provider Analysis for Archimedes

> **Pricing checked:** 2026-08-16
> **Basis:** Current repository architecture and public list prices. This is a
> modeled estimate, not the actual AWS invoice, because Cost Explorer access was
> unavailable.

## Executive recommendation

**Do not migrate the complete production stack yet.**

- **AWS:** retain for production, funds-adjacent runners, Bedrock, managed
  PostgreSQL, IAM, WAF, and secrets.
- **Railway:** best candidate for staging, preview environments, and a low-cost
  non-custodial beta pilot.
- **Render:** best if the goal is replacing AWS operations with a conventional
  production PaaS, not primarily saving money.
- **Fly.io:** best for future isolated agent runtimes and geographically
  distributed stateless APIs.
- **Turso:** best for a read-heavy corpus/catalog replica or per-agent database,
  not as the current PostgreSQL replacement.

The immediate problem is not simply “AWS is expensive.” Archimedes has
production-style fixed infrastructure supporting a repository-stated design
workload of approximately **0.07 requests/second**.

## 1. What Archimedes actually runs

The current design contains:

- CloudFront → ALB → ECS Fargate.
- One x86 Fargate web task: **1 vCPU / 3 GB**, autoscaling 1–4.
- FastAPI, Better Auth, nginx/React, and SSE generation.
- Aurora PostgreSQL 18.3: **0.5–16 ACU**.
- Single-node ElastiCache Redis `cache.t3.micro`.
- Redis is not merely a cache: it holds traces, job state, rate limits, and
  singleton runner leases.
- Oracle and trading-agent processes must remain exactly-once because they can
  sign transactions.
- A detached `t3.medium` EC2 instance still runs the stranded runners and acts
  as rollback capacity.
- Two NAT instances, two WAF ACLs, ECR, EFS-related plans, CloudWatch, and Route
  53.
- Bedrock/Nova Micro through workload IAM.
- The future KB pipeline requires approximately 6 GB or more for models, but its
  scheduled Fargate implementation currently runs in skip/no-op mode.

These constraints make a simple “move the Docker container” comparison
misleading.

## 2. Estimated AWS cost

### Current modeled monthly floor

| Component | Approx. monthly |
| --- | ---: |
| Fargate web task, 1 vCPU / 3 GB | $39.30 |
| Aurora minimum 0.5 ACU | $43.80 |
| ElastiCache `cache.t3.micro` | $12.41 |
| ALB fixed charge | $16.43 |
| ALB public IPv4 addresses | $7.30 |
| Two `t4g.nano` NAT instances | $6.13 |
| NAT public IPv4 addresses | $7.30 |
| Two WAF ACLs + six rules | $16.00 |
| Detached `t3.medium`, EBS, and IPv4 | $35.62 |
| Route 53 | $0.50 |
| CloudWatch, ECR, S3, logs, and alarms | approximately $15–35 |
| **Modeled current total** | **approximately $200–220/month** |

This excludes:

- Bedrock token usage.
- High traffic or ALB LCU growth.
- Aurora ACU bursts and database I/O.
- Taxes and support.
- External RPC, Circle, Pinata, or other SaaS costs.

After the planned runner relocation and old EC2 decommission, the modeled floor
is approximately **$180–205/month**. The dedicated `t3.small` runner replaces
part, but not all, of the old EC2 cost.

### AWS remains the best fit for

- Production financial infrastructure.
- Transaction-signing singleton processes.
- Workload IAM rather than static AWS credentials.
- Bedrock with no cross-cloud data path.
- Managed relational durability, PITR, and network isolation.
- Fine-grained WAF, CloudWatch, and audit controls.
- Terraform-controlled infrastructure.

### AWS is poor for

- Very low-traffic prototypes.
- Teams that do not want an infrastructure operator.
- Workloads where fixed ALB, WAF, IPv4, and database floors dominate actual
  compute.

## 3. Render

### Relevant prices

- Pro workspace: **$25/month**, plus compute.
- Web/background services:
  - Starter: **$7**, 512 MB / 0.5 CPU.
  - Standard: **$25**, 2 GB / 1 CPU.
  - Pro: **$85**, 4 GB / 2 CPU.
- PostgreSQL:
  - Basic 1 GB: **$19**.
  - Pro 4 GB with HA: **$55**.
  - Storage: **$0.30/GB**.
- Redis-compatible Key Value:
  - Starter 256 MB: **$10**.
  - Standard 1 GB: **$32**.
- Cron jobs: from **$1/month**, usage prorated.
- Persistent disk: **$0.25/GB**.
- Pro bandwidth: 25 GB included, then **$0.15/GB**.

### Archimedes estimate

| Configuration | Approx. monthly |
| --- | ---: |
| Lean: 2 GB web, basic PostgreSQL, two small workers | **$105–115** |
| Safer: 4 GB web, Pro PostgreSQL, Redis, workers | **$195–210** |
| Future 8 GB KB job running four hours/day | add approximately **$29** |

The lean configuration risks memory pressure because the current ECS task
intentionally has 3 GB for the Python model, auth, and nginx. Render becomes
approximately as expensive as AWS once production database and application
sizing are used.

### Best actual use case

**A production PaaS migration where reduced operations are worth more than raw
savings.**

Render provides the cleanest replacement for web services, background workers,
cron, PostgreSQL, and Redis in one vendor.

### Main concerns

- Bedrock would need cross-cloud AWS authentication, likely introducing static
  credentials or another identity mechanism.
- The current three-container task must become separate Render services or a
  supervised multi-process container.
- Production sizing is not substantially cheaper than optimized AWS.
- Exactly-once runners must not be deployed with overlapping old/new instances.

**Verdict:** safest full-PaaS option, but not the cost winner.

## 4. Railway

### Relevant prices

- Hobby: **$5 minimum usage**, including $5 usage.
- Pro: **$20 minimum usage**, including $20 usage.
- Memory: **$0.00000386/GB-second**, approximately **$10/average GB-month**.
- CPU: **$0.00000772/vCPU-second**, approximately **$20/average vCPU-month**.
- Volumes: approximately **$0.156/GB-month**.
- Egress: **$0.05/GB**.
- Object storage: **$0.015/GB-month**, free egress.

### Archimedes estimate

Assuming the web process averages 2–3 GB, low CPU usage, small runners, 0.5–1 GB
PostgreSQL, and 0.25–0.5 GB Redis:

- **Lean full stack:** approximately **$45–75/month**.
- With a real KB batch workload and more database capacity: **$65–95/month**.
- With a self-operated HA PostgreSQL cluster: approximately **$70–110+**.

These are usage-sensitive estimates. A two-week deployment is needed to
determine actual Python memory and CPU consumption.

### Important database limitation

Railway’s PostgreSQL and Redis offerings are containers created from templates
with attached volumes. Railway’s documentation explicitly calls the templates
**unmanaged**. Backups are available, and PostgreSQL can be converted to a
Patroni/etcd/HAProxy HA configuration, but the team owns that architecture and
maintenance.

A 99.99% Pro platform availability target does not make a single PostgreSQL
container highly available.

### Best actual use case

**Staging, previews, and a low-cost beta environment.**

It fits the monorepo, Docker services, cron jobs, and private networking while
billing for actual consumption. It is the best place to test whether the
application genuinely needs its AWS allocations.

### Main concerns

- Database reliability is weaker unless the team operates its own HA cluster.
- Bedrock and AWS secret access become cross-cloud.
- Funds-adjacent workers require careful replica and deployment controls.
- The cost advantage declines if several replicas constantly consume their
  limits.

**Verdict:** cheapest credible beta option and the recommended pilot platform.

## 5. Fly.io

### Relevant IAD pricing

- `shared-cpu-1x`, 2 GB: approximately **$10.70/month**.
- `shared-cpu-2x`, 4 GB: approximately **$21.40/month**.
- Persistent volumes: **$0.15/GB-month**.
- North America egress: **$0.02/GB**.
- Shared Anycast IPv4 and the first ten certificates: free.
- Managed PostgreSQL:
  - Basic, 1 GB, HA/backups/pooling: **$38/month**.
  - Storage: **$0.28/GB-month**.
- Standard support: **$29/month**.
- Upstash Redis:
  - Free: 256 MB / 500K commands.
  - Fixed 250 MB: **$10/month**.
  - Production SLA, Multi-AZ HA, and encryption at rest:
    **+$200/database/month**.

### Archimedes estimate

- Web machine: approximately $21.
- Auth machine or additional process: $3–6.
- Singleton runner: approximately $11.
- Managed PostgreSQL plus 10 GB: approximately $41.
- Basic Upstash: $10.
- Storage/egress: low single digits.

Total:

- **Lean:** approximately **$85–105/month**.
- With support and future KB batch compute: **$115–135/month**.
- With Upstash’s production pack: **$285+**.

### Best actual use case

**Isolated per-user agents, ephemeral sandboxes, and global stateless APIs.**

Fly Machines are particularly attractive if Archimedes later runs one isolated
agent runtime per subscriber and stops idle machines. It also handles
long-running worker processes better than a request-only serverless platform.

### Main concerns

- More operational responsibility than Render or Railway.
- Global web replicas do not help much while PostgreSQL and signing workers
  remain single-region.
- Fly volumes are local, so corpus artifacts should move to object storage.
- Production Redis assurance is expensive.
- Bedrock again becomes cross-cloud.

**Verdict:** strong future agent-runtime platform, not the best current
full-stack migration.

## 6. Turso

### Relevant prices

| Plan | Monthly | Annual-billed effective |
| --- | ---: | ---: |
| Free | $0 | $0 |
| Developer | $5.99 | $4.99/month |
| Scaler | $29 | $24.92/month |
| Pro | $499 | $416.58/month |

Free includes 5 GB, 500M rows read, and 10M rows written. Scaler includes 24 GB,
100B reads, 100M writes, and 30-day PITR.

### Why it cannot replace Aurora directly

Turso is SQLite-compatible. The current application uses:

- Synchronous SQLAlchemy and `psycopg2`.
- PostgreSQL Alembic migrations and behavior.
- A Better Auth sidecar using Node’s `pg.Pool`.
- PostgreSQL as the shared transactional source of truth.
- Redis-specific queues, leases, TTLs, and atomic counters.

Replacing Aurora with Turso would therefore be an application and schema
migration, not a connection-string change.

### Best actual use case

- Read-only public corpus/catalog replica.
- Edge-local strategy marketplace reads.
- Per-agent memory or one database per autonomous agent.
- Embedded replicas for local/offline agents.

**Verdict:** excellent specialized database, but no role in the critical write
path today.

## 7. Final decision

| Requirement | Best choice |
| --- | --- |
| Current production and funds-adjacent execution | **AWS** |
| Cheapest staging/preview environment | **Railway** |
| Simplest production PaaS | **Render** |
| Global APIs and isolated agent runtimes | **Fly.io** |
| Edge read model or per-agent database | **Turso** |

If forced to move the whole beta today, the preferred option would be
**Railway Pro**, but meaningful funds should not depend on its default
single-container PostgreSQL setup.

## 8. Recommended next steps

1. Export 30–60 days of AWS Cost Explorer usage by service and usage type.
2. Complete runner relocation, verify exactly-once behavior, and then
   decommission the old EC2 instance.
3. Consolidate the two WAF layers if the same protection can be maintained and
   direct-origin bypass is closed.
4. Test ARM64 Fargate; it could reduce web compute by approximately 20%, subject
   to PyTorch/model compatibility.
5. Consider ElastiCache Valkey on the equivalent node; current AWS pricing is
   approximately 20% below Redis.
6. Review whether seven CloudWatch dashboards and 90-day log retention are
   justified.
7. Deploy a **Railway staging environment for 14 days**, without signing keys or
   production secrets, and measure:
   - Peak and average memory.
   - CPU consumption.
   - SSE stability.
   - Deployment overlap.
   - Database restore.
   - Redis lease correctness.
   - Actual monthly projection.
8. Reconsider Aurora only after defining the required RPO/RTO. A smaller
   single-AZ RDS instance could save approximately $25–30/month but reduces
   resilience.

## Primary pricing sources

- [Render pricing](https://render.com/pricing)
- [Railway pricing](https://railway.com/pricing)
- [Railway database reference](https://docs.railway.com/databases/reference)
- [Railway volume backups](https://docs.railway.com/volumes/backups)
- [Turso pricing](https://turso.tech/pricing)
- [Fly.io resource pricing](https://fly.io/docs/about/pricing/)
- [Fly.io Managed PostgreSQL](https://fly.io/docs/mpg/)
- [Upstash Redis pricing](https://upstash.com/pricing/redis)
- [AWS Fargate pricing](https://aws.amazon.com/fargate/pricing/)
- [AWS Aurora pricing](https://aws.amazon.com/rds/aurora/pricing/)
- [AWS load balancer pricing](https://aws.amazon.com/elasticloadbalancing/pricing/)
- [AWS WAF pricing](https://aws.amazon.com/waf/pricing/)
- [AWS VPC and IPv4 pricing](https://aws.amazon.com/vpc/pricing/)
