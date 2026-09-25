# Archimedes Infrastructure

## Local CLI tooling

The infra workflow needs three command-line tools. Two come from the `archimedes`
conda env (pinned in [`../environment.yml`](../environment.yml), so `conda env create`
gives everyone the same versions); the third is a Homebrew install because it is not
packaged on conda-forge.

| Tool | Version (verified) | How to install | Why |
| --- | --- | --- | --- |
| **Terraform** | 1.15.3 | conda env (`terraform>=1.10`) | IaC for the whole AWS stack (`infra/*.tf`). 1.10+ required for S3-native state locking (`use_lockfile=true`). |
| **AWS CLI v2** | 2.34.48 | conda env (`awscli>=2.15`) | Deploys, SSM sessions, and **`aws configure sso`** for IAM Identity Center. **v2 is required** — v1's SSO support is insufficient for the Identity Center login flow. |
| **AWS SAM CLI** | 1.162.1 | **Homebrew**: `brew install aws-sam-cli` (not on conda-forge) | Serverless build/deploy + `sam local` testing. Only needed if/when we add Lambda pieces — see note below. |

Run env-scoped tools with `conda run -n archimedes <cmd>` (or `conda activate archimedes`
first) so you are always on the pinned versions, not whatever is on the system PATH.

**On SAM's role:** the production stack is **ECS Fargate + Aurora + ALB, managed by
Terraform** (cut over from EC2 2026-07-09) — Terraform is the IaC backbone and SAM does
not replace it. SAM is
purpose-built for **Lambda / API Gateway serverless** apps and local Lambda emulation.
Treat it as *additive*: reach for it only when we introduce a concrete Lambda use-case
(e.g. an event-driven nanopayment-settlement hook, a scheduled job, or lightweight glue),
not as a second way to manage the core web tier. Until then it is installed-but-unused.

## Terraform State Backend (S3)

State is stored remotely in S3 with S3-native locking (Terraform 1.10+,
`use_lockfile = true`). The S3 bucket was created out-of-band via AWS
CLI (infrastructure-of-infrastructure — never changes, don't manage
with Terraform). No DynamoDB table needed.

### Bootstrap Commands (run once, already done)

```bash
# S3 bucket — versioned, encrypted, no public access
aws s3api create-bucket \
  --bucket archimedes-tfstate-037613907429 \
  --region us-east-1

aws s3api put-bucket-versioning \
  --bucket archimedes-tfstate-037613907429 \
  --versioning-configuration Status=Enabled

aws s3api put-bucket-encryption \
  --bucket archimedes-tfstate-037613907429 \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

aws s3api put-public-access-block \
  --bucket archimedes-tfstate-037613907429 \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

# Bucket policy: deny non-TLS + restrict to account only
aws s3api put-bucket-policy \
  --bucket archimedes-tfstate-037613907429 \
  --policy '{
    "Version": "2012-10-17",
    "Statement": [
      {"Sid":"DenyNonTLS","Effect":"Deny","Principal":"*","Action":"s3:*",
       "Resource":["arn:aws:s3:::archimedes-tfstate-037613907429","arn:aws:s3:::archimedes-tfstate-037613907429/*"],
       "Condition":{"Bool":{"aws:SecureTransport":"false"}}},
      {"Sid":"RestrictToAccount","Effect":"Deny","Principal":"*","Action":"s3:*",
       "Resource":["arn:aws:s3:::archimedes-tfstate-037613907429","arn:aws:s3:::archimedes-tfstate-037613907429/*"],
       "Condition":{"StringNotEquals":{"aws:PrincipalAccount":"037613907429"}}}
    ]}'
```

### Working with Terraform

**Prereqs (every session):** the S3 backend + provider use your SSO credentials,
so Terraform needs `AWS_PROFILE` exported — without it, it falls through to
EC2-instance-metadata and dies with *"No valid credential sources found / no EC2
IMDS role found."*

```bash
aws sso login --profile ArchimedesDanAdmin           # refresh the SSO session
export AWS_PROFILE=ArchimedesDanAdmin AWS_REGION=us-east-1
aws sts get-caller-identity                          # smoke-test (account 037613907429)
```

**The Aurora master password** is required by the config (`var.aurora_master_password`).
It lives in SSM as a SecureString — pipe it straight into the TF var so it never
prints to screen or shell history:

```bash
export TF_VAR_aurora_master_password="$(aws ssm get-parameter \
  --name /archimedes/prod/AURORA_MASTER_PASSWORD \
  --with-decryption --query Parameter.Value --output text)"
```

**The core loop:**

```bash
cd infra/                # the LIVE stack (NOT infra/terraform/ — that's a separate, unwired module)
terraform init           # downloads providers, connects to the S3 backend
./apply.sh               # ALWAYS preview first — see "Operational variables" below
./apply.sh --apply       # applies after the same guarded preflight; prompts for confirmation
```

`terraform plan` / `terraform apply` still work directly, but `./apply.sh` is the
recommended entry point — see the next section for what it checks and why.

**If a plan/apply was interrupted (Ctrl-C), the S3 state lock can go stale** —
you'll see `Error acquiring the state lock … PreconditionFailed`. Clear it (only
when you're sure no other terraform is actually running):

```bash
terraform force-unlock <LOCK_ID>     # the ID is printed in the error
```

**Adopting a live-but-unmanaged resource (drift) — import, don't recreate.** If a
resource exists in AWS but Terraform wants to *create* it (→ would error
`…AlreadyExists`), import it into state first. Example (the private-subnet NAT routes):

```bash
terraform import 'aws_route.private_nat[0]' rtb-<id>_0.0.0.0/0
terraform import 'aws_route.private_nat[1]' rtb-<id>_0.0.0.0/0
```

> **Route-table gotcha (load-bearing).** Terraform forbids mixing an **inline
> `route {}` block** on `aws_route_table` with **standalone `aws_route` resources**
> on the same table — the inline block claims the whole route set and will silently
> DELETE routes managed by `aws_route` (this once tried to delete the VPC-peering
> return route and would have severed app↔Aurora/Redis — see PR #836). The private
> route tables therefore use **all standalone `aws_route`** (NAT + peering); do not
> add inline `route {}` blocks to them.

> **`user_data` edits reboot the box.** Changing the EC2 `user_data` (e.g. editing
> `user-data.sh`) makes Terraform **stop/start** the instance (AWS can't modify
> user_data on a running instance) → a ~1–2 min outage. The EIP keeps the IP and
> `restart: unless-stopped` brings the app back, but the *running* instance never
> needs a user_data refresh (it only runs at first boot). To apply other changes
> without the reboot, `-target` them; to stop Terraform proposing the reboot for a
> bootstrap-script edit, add `lifecycle { ignore_changes = [user_data] }`.

### Operational variables — always apply via terraform.tfvars, never a bare TF_VAR_* export

**The landmine:** `infra/` ships no `terraform.tfvars`. Every operationally-significant
variable in [`variables.tf`](variables.tf) defaults to the *feature-off* value (`false` /
`""`). Setting one only via a one-off `TF_VAR_*` shell export — the pattern this README
used to document exclusively, e.g. for `aurora_master_password` above — is one bare
`terraform apply` away from silently reverting it: a different shell, a later session, a
teammate who doesn't know the export was ever needed. For the OAuth flags that strips the
whole `secrets{}` block for that provider out of the auth task definition
([`ecs.tf`](ecs.tf) lines 662-665 / 666-669); for the wallet variables it blanks a live env
value ([`ecs.tf`](ecs.tf) lines 589-590). No error, no destructive-looking plan line — just
a quiet, successful apply that undoes prod config.

**The rule:** copy [`terraform.tfvars.example`](terraform.tfvars.example) to
`terraform.tfvars` (gitignored — the real file with real values is never committed) and
keep it current with whatever is actually live in prod. Terraform auto-loads
`terraform.tfvars` from the working directory on every plan/apply — no `-var-file` flag
needed — so once it's maintained, a bare `terraform apply` stops being a trap.
`aurora_master_password` stays on the existing SSM-piped
`TF_VAR_aurora_master_password=$(aws ssm get-parameter ...)` export documented above — it's
a true secret (`sensitive = true`, no default) and belongs in SSM, not a local file, even a
gitignored one.

**`./apply.sh` is the entry point that enforces this rule** — run it instead of a bare
`terraform plan` / `terraform apply`. It always operates on `infra/` regardless of the
caller's cwd, and before touching Terraform it: (1) requires `terraform.tfvars` to exist,
pointing here if it doesn't; (2) re-derives the operational-variable list from
`terraform.tfvars.example` at runtime (so a newly added variable can't silently drift out
of the check) and warns loudly, by name, on anything missing or an empty string — pass
`--allow-empty <var>` (repeatable) to proceed with a specific one intentionally unset, or
it refuses; (3) refuses if any `TF_VAR_*` env var is currently set that would shadow a
`terraform.tfvars` entry — the exact landmine this section describes; (4) confirms `aws sts
get-caller-identity` succeeds and resolves to account `037613907429`, guarding against an
apply run under the wrong `AWS_PROFILE`. It then runs `terraform plan` by default;
`--apply` runs `terraform apply` (still prompting for confirmation unless `--yes` is also
given); any other args pass through to terraform. See the script's own header comment for
full usage.

Known operationally-set variables as of 2026-08-20 — re-grep `variables.tf` for new
`var.*` conditionals in `ecs.tf` any time a new operational flag is added:

| Variable | Declared | Gates | Default | Status |
| --- | --- | --- | --- | --- |
| `google_oauth_enabled` | `variables.tf:100` | Google OAuth `secrets{}` block — `ecs.tf:662-665` | `false` | Live-set in prod via a bare `TF_VAR_*` export — verify the current value before any apply; not captured in a tfvars file before this PR. |
| `github_oauth_enabled` | `variables.tf:106` | GitHub OAuth `secrets{}` block — `ecs.tf:666-669` | `false` | Same as above. |
| `platform_admin_wallets` | `variables.tf:123` | `PLATFORM_ADMIN_WALLETS` env — `ecs.tf:589` | `""` | **Confirmed:** Dan applied a real value via `TF_VAR_platform_admin_wallets` on 2026-08-20. Not captured anywhere durable — the next bare apply reverts it to `""` and silently removes the admin-wallet publish bypass. |
| `platform_admin_accounts` | `variables.tf` (added #1648) | `PLATFORM_ADMIN_ACCOUNTS` env — `ecs.tf` | `""` | Same landmine class as `platform_admin_wallets` directly above. Empty is safe *today* (the wallet list still grants admin on its own), but once admin is pinned to accounts a bare apply silently locks the owner out of `/api/metrics/private/*`. |
| `archimedes_treasury_wallet` | `variables.tf:129` | `ARCHIMEDES_TREASURY_WALLET` env — `ecs.tf:590` | `""` | Same landmine class and same `ecs.tf` gating pattern as `platform_admin_wallets`. This PR did **not** confirm whether a real value is currently applied in prod — check with Dan before any apply that could touch it. |
| `owner_alert_email` | `variables.tf` (added #1818 P5) | A second SNS email subscription — `cloudwatch.tf`'s `aws_sns_topic_subscription.owner_alerts_email` | **none** | **REQUIRED — `terraform plan` errors until it is set**, and a `precondition` also fails the plan if it equals `alarm_email`. Not landmine-shaped on purpose. #1818 P5 says "there was no alarm"; the account says two alarms fired on 2026-09-03 (13:38:46Z, 13:39:16Z) and the topic delivered six emails with zero failures — to `alarm_email` — during a 94-minute outage the owner then found by loading the site. So this variable names a channel he will actually see. Public repo ⇒ the real address lives only in your gitignored `terraform.tfvars`. AWS emails a confirmation link; it pages nobody until clicked. |
| `alarm_email` | `cloudwatch.tf:17` | The pre-existing SNS email subscription — `cloudwatch.tf:33-36` | `""` | **CONFIRMED applied and working (2026-09-03): a real address is subscribed, confirmed, and delivering.** It is not captured in any tfvars, so this row is now the sharpest instance of this section's landmine — one bare apply without `TF_VAR_alarm_email` unsubscribes the only subscription this topic has. Get the value from Dan and put it in `terraform.tfvars`. |

### Admin Access (SSM Session Manager)

Once VPC migration is complete, admin access is via AWS SSM:

```bash
# Terminal session (replaces SSH)
aws ssm start-session --target i-<instance-id> --region us-east-1

# Port forwarding to Aurora (database access from laptop)
aws ssm start-session \
  --target i-<instance-id> \
  --region us-east-1 \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters host=<aurora-endpoint>,portNumber=5432,localPortNumber=5432
```

## Branch Protection (`main`)

`main` is build-on-deploy: every push auto-deploys to the live EC2 host. The protection
ruleset is codified in [`scripts/setup-branch-protection.sh`](../scripts/setup-branch-protection.sh)
so an admin can apply or audit it declaratively (audit #10 / issues #519, #526).

```bash
./scripts/setup-branch-protection.sh            # dry-run: print the payload, apply nothing
./scripts/setup-branch-protection.sh --apply    # apply (needs repo admin)
./scripts/setup-branch-protection.sh --verify    # print the currently-applied protection
# or, raw:  gh api repos/aprin-labs/archimedes/branches/main/protection
```

What it enforces: the two hard-block CI checks (`Backend — unit tests`, `Ruff — format +
critical lint rules`), 1 approving review, no force-push, no branch deletion, and
`required_linear_history: false` (we are **merge-commits-only** — linear history would
force squash/rebase). The informational checks (lint-report, complexity) stay non-required.

**Build-on-deploy tradeoff:** the script ships with `enforce_admins=false` so repo admins
(including the `t2o2` agentic user) keep their direct-push path while non-admins are gated.
**Stale as of 2026-08-03:** `t2o2` is dormant, so this exemption now only benefits human
admins. See the REVISIT note in `scripts/setup-branch-protection.sh`.
Flipping `ENFORCE_ADMINS=true` gates everyone but forces the agentic system onto PRs — that
is a team decision (Chuan, as repo admin, owns it).

## Monitoring & Disaster Recovery

- **`cloudwatch.tf`** — SNS alert topic + alarms (ALB 5xx / unhealthy hosts /
  p95 latency, ECS service CPU + memory, Aurora CPU / memory / connections, NAT,
  Redis, WAF, runner liveness, deploy drift) + three dashboards. Additive:
  `terraform apply` only *creates* new CloudWatch objects, it does not touch the
  existing ALB/ECS/Aurora/WAF resources.
- **Paging is now a required input, not a setting.** `owner_alert_email` has no
  default and `terraform plan` errors until it is set — see the operational
  variables table above. The reason is not the one #1818 P5 states: on
  2026-09-03 two alarms *did* fire (13:38:46Z, 13:39:16Z) and the topic *did*
  deliver six emails with zero failures, and the owner still found the
  94-minute outage by loading the site. What failed was attention, not
  plumbing, so the variable forces a deliberate choice of a channel that will
  be seen — and a `precondition` refuses the plan if it duplicates the mailbox
  that already failed. After the first apply, **click the confirmation link AWS
  emails** — an unconfirmed subscription is indistinguishable, from Terraform's
  side, from a working one — and then run the alarm drill in
  `runbooks/disaster-recovery.md` § Drills. The alarms added alongside it make
  a repeat of 2026-09-03 detectable at ~03:48 instead of 13:38:46.
- **Applying the alarms on their own:** the exact `-target` list and the
  expected `Plan: 5 to add, 1 to change, 0 to destroy` are in
  `runbooks/disaster-recovery.md` § "2026-09-03 alarm set" → Applying them. If
  that plan mentions `aws_ecs_task_definition.backend`, **stop** — an
  observability apply is about to replace the task definition (measured
  2026-09-03: `PAPER_ADVANCE_ENABLED "false" -> "true"`, which re-arms the loop
  that caused the outage). The runbook explains the dependency edge.
- **`runbooks/disaster-recovery.md`** — RTO/RPO targets, per-scenario response
  (host loss, DB corruption, WAF lockout), restore-order, and a drill checklist.
- **`runbooks/aurora-backup-restore.md`** — exact PITR / snapshot-restore CLI
  (Aurora `backup_retention_period = 7` ⇒ 7-day PITR window already on).
- **`runbooks/waf-rules-reference.md`** — what each `waf.tf` rule does and the
  count→block promotion workflow.

> These runbooks are **authored, not drilled.** Run a game-day (see the DR
> drill checklist) before trusting the measured RTO/RPO.

## ECS Fargate (issue #1039)

- **`ecr.tf`** — the two private ECR repos CI (build-chunk 1) already pushes
  to (`archimedes-backend`, `archimedes-nginx`), each with a lifecycle policy
  (expire untagged after 1 day; keep the last 15 tagged images).
- **`ecs.tf`** — ECS cluster, the `archimedes-backend` task definition
  (nginx + backend, one Fargate task, `awsvpc` mode) and service (registered
  into the **existing** `archimedes-backend-tg` target group via a data
  source — `alb.tf` itself is untouched except one additive security-group
  egress rule), CPU-based Application Auto Scaling (min/max via
  `ecs_service_min_count` / `ecs_service_max_count`), and the task
  execution/task IAM roles + an additive inline policy on the existing
  out-of-band `archimedes-github-deploy` CI role.
- **`runbooks/ecs-fargate-cutover.md`** — the full ordered operator runbook:
  the three blockers that must close before this actually serves traffic;
  what `terraform apply` does the instant it runs (starts live blue/green
  traffic against the same target group — read before applying); a staged
  apply order (ECR first, then seed a real image, then the ECS
  cluster/service); how to seed ECR with the first image (CI trigger or a
  manual break-glass push); swinging the ALB target from the EC2 instance to
  the ECS service; dedicated verification drills for zero-downtime rollouts,
  self-heal (kill a task, time the <2 min recovery), and `ecs
  execute-command`; and the EC2 decommission sequence (gated on the
  oracle/agent/kb-runner daemons getting their own Fargate home first).

> **Authored 2026-07-06, not yet `terraform plan`-verified against live AWS
> (no credentials in this environment) — `terraform validate`/`fmt` only.**
> Same review-then-apply posture as the rest of this section.

## Per-deploy artifacts

- **`deploy_output.json`** (repo root) is written by
  [`backend/archimedes/scripts/deploy_contracts.py`](../backend/archimedes/scripts/deploy_contracts.py)
  on every contract deploy and goes stale the moment any contract is
  redeployed — it is gitignored and untracked (audit 06-14 finding I3: the
  previously-tracked copy's `synthTokens`/`synthOracles`/`vaults` addresses
  no longer matched the live deploy). It is per-deploy operator output, not a
  source of truth. The authoritative current addresses live in
  [`ui/src/config.js`](../ui/src/config.js) (frontend) and
  [`backend/archimedes/chain/client.py`](../backend/archimedes/chain/client.py)
  (backend) — update both of those after any redeploy.

## Security Notes

- **No `.pem` files in git.** `infra/*.pem` is in `.gitignore`.
- **No Terraform state in git.** State is in S3; local files are gitignored.
- **SSH keys are rotated.** The key committed in early repo history was revoked
  on 2026-05-26. The current key exists only in GitHub Secrets + local machine.
- **Port 22 will be removed** once SSM Session Manager is live.
