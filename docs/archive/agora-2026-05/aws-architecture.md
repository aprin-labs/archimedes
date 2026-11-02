> **ARCHIVED 2026-07-28 — historical. Current: [`docs/architecture.md`](../../architecture.md)**
>
> Describes a single EC2 box with no ALB, no managed database, no managed Redis and no WAF. All four exist: ECS Fargate behind an ALB, CloudFront + WAF in front, Aurora PostgreSQL 18.3 and ElastiCache Redis 7.1.

# AWS Architecture — Archimedes

> **Status:** Living doc. Written 2026-05-28. Reflects actual deployed state;
> planned-but-not-applied Terraform resources are clearly marked.
>
> **AWS Account:** `037613907429` (root), region `us-east-1` (N. Virginia).
> **Account owner:** Chuan (team lead, CTO @ Gyld Finance). This is a shared
> account also running Kaleidoscope and Lighthouse workloads — only
> Archimedes-specific resources are inventoried here.
>
> **Cost context:** Dan is personally funding infra. Hard cap: **$150/month**,
> prefer less. The planned Aurora/ALB/WAF stack pushes toward the cap; migration
> to ECS or lighter alternatives is analyzed in §3.

---

## 1. Current Deployed State (as of 2026-05-28)

### 1.1 What's live RIGHT NOW

The production stack runs on a single EC2 instance with Docker Compose.
All services (backend, postgres, redis, nginx, oracle, agent, kb-runner) are
containers on one host. There is no ALB, no managed DB, no managed Redis, no WAF.

```
Internet → Route 53 → Elastic IP → EC2 (nginx:80/443) → Docker network
                                                         ├── backend:8000 (FastAPI)
                                                         ├── postgres:5432
                                                         ├── redis:6379
                                                         ├── oracle
                                                         ├── agent
                                                         └── kb-runner
```

### 1.2 Resource Inventory

| Resource | ID / Name | Purpose | Spec | Monthly Cost (est.) |
|---|---|---|---|---|
| **EC2 Instance** | `i-0987f70a131ed3ab1` (`archimedes-server`) | Docker host, runs entire stack | t3.small (2 vCPU, 2 GB RAM, 20 GB gp3) | ~$17.28 compute + $1.60 EBS = **$18.88** |
| **Elastic IP** | `eipalloc-0de7ade1e4d96e097` → `16.61.56.158` | Stable public IP (survives stop/start) | Attached to EC2 | **$0** (free while attached) |
| **Security Group** | `sg-022a1abad15a9b988` (`archimedes-sg`) | Ingress: SSH(22)/HTTP(80)/HTTPS(443) from 0.0.0.0/0 + port 80 from 10.0.0.0/23. Egress: all | — | **$0** |
| **Route 53 Hosted Zone** | `Z03812612E5OLGK1YGZSR` | `archimedes-arc.com.` DNS | A record → `16.61.56.158` (TTL 60) | **$0.50** |
| **ACM Certificate** | `arn:aws:acm:us-east-1::certificate/9ba0da81...` | TLS cert for `archimedes-arc.com` | DNS-validated, ISSUED | **$0** |
| **S3: Corpus Artifacts** | `archimedes-corpus-artifacts-prod` | KB pipeline output (empty) | SSE-S3, versioned | **~$0** |
| **S3: Paper PDFs** | `archimedes-paper-pdfs-prod` | Ingested paper PDFs (empty) | SSE-S3 | **~$0** |
| **S3: TF State** | `archimedes-tfstate-037613907429` | Terraform remote state | Versioned, SSE-S3, S3-native locking | **~$0** |
| **DynamoDB** | `archimedes-papers-index` | Paper metadata index | PAY_PER_REQUEST, 0 items | **~$0** |
| **SSM Parameters** | `/archimedes/prod/*` (5 params) | Secrets: ANTHROPIC_AUTH_TOKEN, CIRCLE_API_KEY, CIRCLE_ENTITY_SECRET, DATABASE_URL, REDIS_URL | SecureString, KMS-encrypted | **$0.25** |
| **IAM Role: Backend** | `archimedes-backend-role` | EC2 instance profile: S3 corpus r/w, DynamoDB papers r/w, SSM params read, KMS decrypt | Inline policy | **$0** |
| **IAM Role: Deploy** | `archimedes-github-deploy` | GitHub Actions OIDC: SSM SendCommand to instance, GetCommandInvocation, DescribeInstanceInformation | Inline policy | **$0** |
| **Instance Profile** | `archimedes-backend-profile` | Attached to EC2, assumes backend role | — | **$0** |
| **Key Pair** | `archimedes-deploy-key` (ED25519) | SSH access (emergency/SSM fallback) | TF-managed, private key in TF state | **$0** |

**Estimated current monthly total: ~$19.63**

### 1.3 On-instance security (Docker layer)

| Measure | Detail |
|---|---|
| **TLS termination** | Nginx with Let's Encrypt cert (not the ACM cert — ACM is for the future ALB). Auto-renewed via certbot. |
| **Security headers** | HSTS (`max-age=31536000; includeSubDomains`), `X-Frame-Options: DENY`, set by nginx. |
| **Port exposure** | Only 22/80/443 open in SG. Backend (8000), Postgres (5432), Redis (6379) are Docker-internal only. |
| **Secrets** | Loaded from SSM Parameter Store at deploy time, injected into containers via `docker compose` env vars. Not in `.env` on disk (SSM fetch at startup). |
| **SSH key** | ED25519, rotated 2026-05-26 (old key revoked). Private key in TF state (S3-encrypted); follow-up to move to Secrets Manager. |
| **Swap** | 2 GB swap file (`/swapfile`), persisted in `/etc/fstab`. Added 2026-05-28 after OOM hang. |
| **Docker build cache** | Pruned to 0 (was 4.7 GB). `docker builder prune -af` needed in deploy script going forward. |

### 1.4 Deploy pipeline

```
GitHub push to main
  → GitHub Actions (deploy.yml)
    → Assume archimedes-github-deploy role (OIDC)
      → SSM SendCommand (AWS-RunShellScript)
        → docker compose build + up on EC2
```

No ECR registry — images are built on-instance. The deploy script runs inside
SSM, which means it cannot self-recover from an OS-level hang (issue #439).

### 1.5 DNS architecture

```
archimedes-arc.com.  →  A record  →  16.61.56.158 (Elastic IP)
                                        → i-0987f70a131ed3ab1 (t3.small)
```

Route 53 hosted zone with NS delegation from registrar. ACM certificate in
us-east-1 (DNS-validated via Route 53 TXT record). The Let's Encrypt cert on
the instance is what's actually serving TLS today; the ACM cert is pre-staged
for the ALB migration.

---

## 2. Planned Infrastructure (Terraform-defined, NOT yet applied)

The following resources are defined in `infra/` Terraform files but **have not
been `terraform apply`-ed**. They represent the target production architecture.

```
Internet → Route 53 → ALB (HTTPS, WAF) → EC2 (private subnet, port 80)
                                           Aurora PostgreSQL (private subnet)
                                           ElastiCache Redis (private subnet)
```

### 2.1 Planned resource inventory

| Resource | Terraform File | Spec | Monthly Cost (est.) |
|---|---|---|---|
| **VPC** | `vpc.tf` | 10.0.0.0/16, DNS support enabled | **$0** |
| **Internet Gateway** | `vpc.tf` | For public subnets | **$0** |
| **2× Public Subnets** | `vpc.tf` | 10.0.0.0/24, 10.0.1.0/24 (first 2 available AZs) | **$0** |
| **2× Private Subnets** | `vpc.tf` | 10.0.10.0/24, 10.0.11.0/24 (first 2 available AZs) | **$0** |
| **2× NAT Instances** | `vpc.tf` | fck-nat on t4g.nano, one per AZ | **~$7.88** |
| **VPC Peering** | `aurora.tf` | Default VPC ↔ new VPC (transitional) | **$0** + data transfer |
| **Aurora Serverless v2** | `aurora.tf` | PostgreSQL 16.4, 0.5–16 ACU, encrypted, 7-day backups | **~$43.80** (min 0.5 ACU × $0.12/hr × 730) |
| **ElastiCache Redis** | `elasticache.tf` | cache.t3.micro, Redis 7.1, encrypted at rest + in transit | **~$12.41** ($0.017/hr × 730) |
| **ALB** | `alb.tf` | HTTPS (TLS 1.3), idle_timeout 300s, access logs → S3, deletion protection | **~$16.43** ($0.0225/hr × 730) + LCU charges (~$3) |
| **ALB Logs S3** | `alb.tf` | `archimedes-alb-logs-*`, 30-day lifecycle, TLS-only bucket policy | **~$0** |
| **WAF v2** | `waf.tf` | Rate limit (1000/5min/IP), AWS Managed Rules (Core, Bad Inputs, IP Reputation, SQLi) | **~$5.00** + $0.60/million req + $1.20/million for managed rules |
| **ACM Certificate** | `alb.tf` | DNS-validated for ALB (us-east-1, already issued) | **$0** |
| **Route 53 Alias** | `alb.tf` | A record → ALB (replaces direct EC2 A record) | **$0** |

**Estimated planned monthly total: ~$19.63 (current) + ~$88.52 = ~$108.15**

This is within the $150 cap but leaves very little headroom. The Aurora
Serverless v2 at $43.80/mo is the single largest line item.

### 2.2 Security architecture (planned)

| Layer | Measure |
|---|---|
| **Edge** | WAF v2 with rate limiting (1000 req/5 min/IP) + AWS Managed Rules. Core and SQLi rules in COUNT mode initially (LLM prompts false-positive on SQL-like words). IP Reputation in BLOCK mode immediately. |
| **TLS** | ALB terminates TLS 1.3 with ACM certificate. HTTP → HTTPS 301 redirect. Backend receives unencrypted traffic from ALB on port 80 (same host). |
| **Network isolation** | EC2, Aurora, and Redis in private subnets (no public IPs). ALB in public subnets. NAT instances (fck-nat) for outbound from private subnets. VPC peering to default VPC is transitional — removed when EC2 moves. |
| **Database** | Aurora encrypted at rest. IAM DB auth available. Security group allows only EC2 SG. 7-day backup retention. |
| **Cache** | ElastiCache encrypted at rest + in transit. Security group allows only EC2 SG. |
| **Secrets** | SSM Parameter Store (SecureString, KMS-encrypted) for app secrets. Aurora master password in TF_VAR (stored in S3 state — follow-up to move to Secrets Manager at runtime). |
| **IAM** | Least-privilege: backend role (S3+DynamoDB+SSM+KMS), deploy role (SSM SendCommand to specific instance). No wildcard resources. |
| **ALB** | Deletion protection enabled. `drop_invalid_header_fields = true`. Access logs to S3 (30-day retention). |
| **S3** | All buckets: SSE-S3 encryption, TLS-only bucket policy (deny non-TLS), public access blocked. TF state bucket additionally versioned with S3-native locking. |

### 2.3 WAF rule strategy

| Rule | Priority | Mode | Rationale |
|---|---|---|---|
| Rate limit (1000/5min/IP) | 1 | **BLOCK** | Prevents brute-force / abuse. Generous enough for normal use. |
| AWSManagedRulesCommonRuleSet | 10 | **COUNT** | LLM endpoints will false-positive. Observe 24-48h, then flip to BLOCK. |
| AWSManagedRulesKnownBadInputsRuleSet | 20 | **COUNT** | Same — observe before blocking. |
| AWSManagedRulesAmazonIpReputationList | 30 | **BLOCK** | Known-bad IPs can be blocked immediately. |
| AWSManagedRulesSQLiRuleSet | 40 | **COUNT** | "select top strategies" prompts trip this. Observe first. |

### 2.4 What's NOT in Terraform (flagged for state import or manual docs)

| Resource | How it was created | Action needed |
|---|---|---|
| Elastic IP (16.61.56.158) | AWS CLI (emergency, 2026-05-28) | Import into TF or add `aws_eip` resource |
| ACM cert (us-east-1) | AWS Console (pre-TF) | Already referenced in `alb.tf`, will be managed by TF on apply |
| Route 53 A record (current) | AWS CLI | Will be replaced by ALB Alias record on ALB migration |
| Swap file on EC2 | SSM command | Should be in `user-data.sh` for persistence |
| Docker build cache prune | Manual SSM | Should be in deploy script |
| SSM Parameters (5 secrets) | Manual | Import into TF or keep as out-of-band secrets management |
| DynamoDB table | Manual or backend init | Import into TF |
| `archimedes-dan-browne-credentials` secret | AWS Console | Temporary — should be deleted after Dan retrieves |

---

## 3. Cost-Benefit Analysis vs. Alternatives

**Hard constraint:** $150/month absolute cap. Dan is personally funding.
Current actual spend: ~$20/month. Planned full stack: ~$108/month.

### 3.1 Option comparison

| | **Current: EC2 + Docker Compose** | **A. ECS Fargate** | **B. ECS on EC2** | **C. App Runner** | **D. Lightsail Containers** |
|---|---|---|---|---|---|
| **Monthly cost** | ~$20 | ~$35–55 | ~$25–40 | ~$30–50 | ~$20–30 |
| **Compute** | t3.small (2 GB RAM) | 3× 0.5 vCPU/1 GB tasks | 1× t3.medium (4 GB) | 1 vCPU/2 GB | 2× 512 MB containers |
| **Managed DB** | ❌ (Docker Postgres) | Aurora or RDS (extra) | Aurora or RDS (extra) | RDS proxy (extra) | ❌ |
| **Managed Redis** | ❌ (Docker Redis) | ElastiCache (extra) | ElastiCache (extra) | ❌ | ❌ |
| **ALB/WAF** | ❌ | Built-in ALB | Built-in ALB | Built-in | ❌ |
| **Deploy ergonomics** | SSM → build on host | ECR push → task def update | ECR push → task def update | Git push / ECR | Manual / limited CI |
| **Observability** | Manual (SSM exec) | CloudWatch built-in | CloudWatch built-in | CloudWatch built-in | Basic |
| **Blast radius of bad deploy** | Entire host hangs (OOM) | Task dies, new task replaces | Same as current (single host) | App Runner rolls back | Same as current |
| **Migration effort** | — (current state) | 2–3 days: Dockerfile hardening, ECR, task definitions, ALB config, CI rewrite | 1–2 days: ECR, ECS cluster, simpler than Fargate | 1 day per service: limited to web services | 1 day: minimal infra |
| **Security delta** | TLS on instance, no WAF, no managed DB encryption | TLS at ALB, WAF attachable, Aurora encrypted, IAM task roles | Same as Fargate | TLS built-in, limited WAF | No WAF, no managed DB |
| **Swap / OOM risk** | ❌ (caused outage, now mitigated with swap) | ✅ Fargate manages memory, task killed cleanly | Same as current (single host) | ✅ Managed | Same as current |
| **State in Terraform** | Partial | Full | Full | Partial | ❌ |

### 3.2 Detailed cost estimates

#### Option A: ECS Fargate

| Component | Spec | Monthly Cost |
|---|---|---|
| 3× Fargate tasks (backend, oracle, agent) | 0.5 vCPU / 1 GB each | 3 × ($0.04048/vCPU-hr × 0.5 + $0.004445/GB-hr × 1) × 730 = **$48.96** |
| Postgres | Aurora Serverless v2 (0.5 ACU min) | **$43.80** |
| Redis | ElastiCache cache.t3.micro | **$12.41** |
| ALB | 1 ALB + minimal LCU | **$19.43** |
| WAF | 1 ACL + managed rules | **$5.00** |
| ECR | 3 repos, <1 GB | **~$1.00** |
| **Total** | | **~$130.60** |

**Answer to the specific question: ECS Fargate compute alone for 3 lightweight
tasks costs ~$49/month — YES, under $50, but the full stack with Aurora + ALB
pushes to $130/month.** If we substitute RDS t4g.micro for Aurora, we save ~$25
(RDS t4g.micro ≈ $18/mo vs Aurora $44/mo), bringing the total to ~$105.

To get under $50/month total on Fargate, we'd need to:
- Drop managed DB (keep Docker Postgres on a persistent ECS task or t4g.micro RDS)
- Drop managed Redis (keep in-memory or skip)
- Drop ALB/WAF (use Fargate's public IP directly with ACM cert)

That defeats the security architecture. **Fargate makes sense as the compute
layer, but the full managed stack at $130/mo is near the cap.**

#### Option B: ECS on EC2

| Component | Spec | Monthly Cost |
|---|---|---|
| 1× t3.medium EC2 | 4 GB RAM, runs ECS agent + all tasks | **$34.56** |
| Aurora Serverless v2 | Same as above | **$43.80** |
| ElastiCache | Same as above | **$12.41** |
| ALB + WAF | Same as above | **$24.43** |
| **Total** | | **~$115.20** |

Saves ~$15 vs Fargate (no per-task vCPU tax) but reintroduces single-host
failure risk. The t3.medium has 4 GB RAM which makes OOM much less likely.
Best value if staying with EC2.

#### Option C: App Runner

App Runner supports only web services (HTTP). Our oracle and agent containers
don't serve HTTP — they're workers. Would need to split: App Runner for backend,
separate compute for workers. Adds complexity without clear benefit over ECS.

#### Option D: Lightsail Containers

Too limited: no managed DB, no WAF, no ALB. Only suitable for the frontend +
API. Doesn't meet the security requirements. Skip.

### 3.3 Recommendation

**For the hackathon timeline (next ~2 weeks): stay on EC2 + Docker Compose.**

Rationale:
1. It works. The site is up. Migration is a 2–3 day distraction.
2. Current cost (~$20/mo) is the cheapest option by far.
3. The OOM issue is mitigated (swap + build cache pruning).
4. The security posture is adequate for hackathon-scale traffic.

**For post-hackathon (if the project continues): migrate to ECS on EC2 with the
planned Aurora + ALB + WAF stack.** Estimated cost: ~$115/month. This is the
sweet spot — managed DB/Redis/ALB/WAF for security, ECS for deploy ergonomics,
single t3.medium for cost efficiency.

**Key decision point:** The Aurora Serverless v2 at $43.80/month is 40% of the
budget. If the corpus pipeline never reaches production scale, a `db.t4g.micro`
RDS instance at ~$18/month is the pragmatic downgrade (saves $26/mo, loses
auto-scaling, fine for our workload).

---

## 4. Operational Runbook

### 4.1 EC2 hung / site down (happened 2026-05-28)

```bash
# 1. Check instance status
aws ec2 describe-instance-status --instance-ids i-0987f70a131ed3ab1 --region us-east-1

# 2. If InstanceStatus = impaired:
aws ec2 reboot-instances --instance-ids i-0987f70a131ed3ab1 --region us-east-1
# Wait 2 min, recheck. If still impaired:

# 3. Stop → Start (fresh host)
aws ec2 stop-instances --instance-ids i-0987f70a131ed3ab1 --region us-east-1
# Wait for 'stopped' state
aws ec2 start-instances --instance-ids i-0987f70a131ed3ab1 --region us-east-1
# Wait for InstanceStatus = ok

# 4. DNS update (EIP should prevent this, but verify)
dig +short archimedes-arc.com
# Should match the Elastic IP (16.61.56.158)

# 5. Verify site
curl -sI https://archimedes-arc.com/ | head -5
curl -s https://archimedes-arc.com/api/health
```

### 4.2 Check Docker health

```bash
aws ssm send-command \
  --instance-ids i-0987f70a131ed3ab1 \
  --region us-east-1 \
  --document-name AWS-RunShellScript \
  --parameters '{"commands":["sudo docker ps --format \"table {{.Names}}\t{{.Status}}\"","free -h","df -h /","sudo docker system df"]}' \
  --timeout-seconds 30
```

### 4.3 Prune Docker build cache (do monthly or after OOM)

```bash
aws ssm send-command \
  --instance-ids i-0987f70a131ed3ab1 \
  --region us-east-1 \
  --document-name AWS-RunShellScript \
  --parameters '{"commands":["sudo docker builder prune -af"]}' \
  --timeout-seconds 120
```

### 4.4 Trigger deploy

```bash
gh workflow run deploy.yml --ref main
```

---

## 5. Known Issues and Follow-ups

| Issue | Severity | Status | GitHub Issue |
|---|---|---|---|
| EC2 OOM during `docker compose build --no-cache` | High | Mitigated (swap + cache prune) | [#439](https://github.com/a-apin/archimedes-arcadia/issues/439) |
| Elastic IP not in Terraform | Medium | Out-of-band (AWS CLI) | Needs import |
| Swap not in user-data.sh | Medium | Manual (won't survive instance replace) | Needs TF fix |
| SSH key private key in TF state | Medium | Accepted (S3-encrypted, account-scoped) | Follow-up: Secrets Manager |
| Aurora master password in TF state | Medium | Accepted (same as above) | Follow-up: runtime fetch |
| No CloudWatch memory/disk alarms | Medium | No alarms configured | Needs CW alarm TF resource |
| Deploy builds on-instance (OOM risk) | Medium | Accepted for hackathon | Post-hackathon: ECR |
| WAF managed rules in COUNT mode | Low | Intentional (observation period) | Flip to BLOCK after 48h |
| `archimedes-dan-browne-credentials` secret | Low | Staged for Dan's one-time retrieval | Delete after retrieval |
| ACM cert in us-east-1, ALB in us-east-1 | Info | With the ALB in us-east-1, a single us-east-1 ACM cert serves both the regional ALB and CloudFront (which requires us-east-1); a separate cert is optional (for isolation), not required. | Verify on apply |

---

_Cross-references: [CLAUDE.md § AWS account access](../../../CLAUDE.md), [infra/README.md](https://github.com/aprin-labs/archimedes/blob/84c0fac4758e9572f34e4f70fe48e9d68c5acab0/infra/README.md), [issue #439](https://github.com/a-apin/archimedes-arcadia/issues/439)_
