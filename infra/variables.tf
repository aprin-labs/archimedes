variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "domain_name" {
  description = "Primary domain for the stack (ACM cert + Route 53 zone + CloudFront/ALB aliases). The hosted zone must already exist — auto-created when the domain is registered via Route 53 Domains."
  type        = string
  default     = "archimedes-arc.com"
}

variable "instance_type" {
  description = "EC2 instance type. t3.medium (4 GB) fixes the t3.small docker-build OOM (#439)."
  type        = string
  default     = "t3.medium"
}

variable "key_name" {
  description = "Name for the SSH key pair"
  type        = string
  default     = "archimedes-deploy-key"
}

variable "aurora_master_password" {
  description = "Master password for Aurora PostgreSQL. Set via TF_VAR_aurora_master_password env var."
  type        = string
  sensitive   = true
}

variable "project_name" {
  description = "Project name for tagging"
  type        = string
  default     = "archimedes"
}

variable "repo_url" {
  description = "GitHub repo HTTPS URL for cloning on the instance"
  type        = string
  default     = "https://github.com/aprin-labs/archimedes.git"
}

# AMI for the backend auto-scaling group (issue #155, OPTIONAL virality tier).
# Bake via infra/scripts/bake-backend-ami.sh, then set this to the resulting
# AMI id (or pass TF_VAR_backend_ami_id). Empty default keeps the var present
# without forcing a value when the ASG is not being applied. The launch
# template / ASG in asg.tf only become real on `terraform apply` — a plan with
# an empty value will simply error on the launch template until an AMI is set,
# which is the intended "supply the AMI to enable the tier" gate.
variable "backend_ami_id" {
  description = "Custom backend AMI id for the auto-scaling group launch template (issue #155). Set after baking via infra/scripts/bake-backend-ami.sh."
  type        = string
  default     = ""
}

# ── ECS Fargate (issue #1039) ──────────────────────────────────────────────

variable "ecs_backend_cpu" {
  description = "Fargate task-level vCPU units for the archimedes-backend task (nginx + backend containers, one task). 1024 = 1 vCPU. Must be a valid Fargate cpu/memory pairing — see https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-cpu-memory-error.html."
  type        = string
  default     = "1024"
}

variable "ecs_backend_memory" {
  description = "Fargate task-level memory (MiB) for the archimedes-backend task. Must pair validly with ecs_backend_cpu (1024 cpu allows 2048-8192 in 1024 steps)."
  type        = string
  default     = "3072"
}

variable "ecs_service_desired_count" {
  description = "Steady-state desired task count for the archimedes-backend ECS service. Terraform sets this once at bootstrap; day-to-day changes (CPU autoscaling, CI deploys) are ignored via lifecycle.ignore_changes on the service (see ecs.tf) so they're never reverted by a later apply."
  type        = number
  default     = 1
}

variable "ecs_service_min_count" {
  # 2, not 1 (owner decision 2026-08-31): a one-task fleet turns every deploy
  # into a brief 502 window (rollout gap) and every backend crash into a
  # user-facing outage — both bitten twice on 2026-08-31 (#1594, #1632).
  # At 2, rollouts overlap and a single crash is invisible. ~$15-18/mo.
  description = "Application Auto Scaling floor for the archimedes-backend service."
  type        = number
  default     = 2
}

variable "ecs_service_max_count" {
  description = "Application Auto Scaling ceiling for the archimedes-backend service. 4 mirrors the cost cap already used by the optional EC2 ASG tier (asg.tf) for this single-user, ~0.07 req/s workload."
  type        = number
  default     = 4
}

variable "ecs_autoscale_cpu_target" {
  description = "Target average CPU utilization (%) for the archimedes-backend service's target-tracking autoscaling policy."
  type        = number
  default     = 60
}

variable "backend_image_tag" {
  description = "Image tag Terraform registers in the initial archimedes-backend/archimedes-nginx task-definition revision (bootstrap only). Real deploys after that are CI registering a new task-definition revision (commit-SHA tag) and calling `aws ecs update-service --force-new-deployment` directly, NOT `terraform apply` — the service ignores task_definition/desired_count drift (see ecs.tf) so those out-of-band deploys are never reverted by a later plan/apply."
  type        = string
  default     = "latest"
}

variable "google_oauth_enabled" {
  description = "Inject Google OAuth client ID/secret from SSM into Better Auth. Enable only after both /archimedes/prod/GOOGLE_CLIENT_* parameters exist."
  type        = bool
  default     = false
}

variable "github_oauth_enabled" {
  description = "Inject GitHub OAuth client ID/secret from SSM into Better Auth. Enable only after both /archimedes/prod/GITHUB_CLIENT_* parameters exist."
  type        = bool
  default     = false
}

# ── Admission-control knobs (issue #1668) ──────────────────────────────────
# The three levers that bound how much concurrent work one Fargate task takes
# on. All three are read from the process environment by code that already
# ships, and all three were absent from infra/ecs.tf — so production ran on the
# code's os.getenv() fallbacks by accident rather than by decision. That is the
# exact config-drift class the generation-cap comment in ecs.tf already calls
# out in prose, and the one docs/adr/lambda-generation-offload.md recorded
# under § Consequences instead of patching.
#
# `type = string`, not `number`: ECS `environment` entries are string/string
# pairs and jsonencode would emit a bare JSON number, which the
# RegisterTaskDefinition API rejects (KeyValuePair.value is typed String). Same
# reason ecs_backend_cpu / ecs_backend_memory are strings.
#
# Defaults here are byte-identical to the os.getenv() fallbacks in the code, so
# this is pure plumbing: applying it changes no behaviour, it only makes the
# values visible and tunable without a code deploy. Retuning them is a separate
# change with separate review (issue #1668 anti-goal). The pairing is pinned by
# backend/tests/test_admission_knobs_drift.py, which reads both sides — the
# reader-facing source of each default is that test, not these comments.
#
# Each description names the READING FUNCTION rather than a line number: the
# line moves whenever anything above it is edited, and a stale citation is
# worse than none.

variable "generation_max_concurrent" {
  description = "GENERATION_MAX_CONCURRENT — how many strategy-generation pipelines may run at once inside ONE backend task. A generation averages ~65% of the task's vCPU for ~48s (measured 2026-08-20), so unbounded parallelism starves auth/SSE/the ALB health check and the task gets killed with every in-flight job. Floored at 1 by the code. Must match the os.getenv default in `_max_concurrent_generations()`, backend/archimedes/api/generate_routes.py."
  type        = string
  default     = "1"
}

variable "generation_max_queue" {
  description = "GENERATION_MAX_QUEUE — how many further generations may WAIT for a slot (the job stays `queued` and its SSE stream gets a job_queued event plus heartbeats). Beyond this /start refuses 429 BEFORE the payment gate, so nobody is charged for a slot that does not exist. 0 disables queueing. Must match the os.getenv default in `_max_queued_generations()`, backend/archimedes/api/generate_routes.py."
  type        = string
  default     = "10"
}

variable "debate_pool_max" {
  description = "DEBATE_POOL_MAX — how many of the regime×mechanism `_STEERS` (3 regimes × 6 mechanisms = 18 today) fan out as parallel proposer LLM calls in the multi-agent debate; the code clamps to [2, len(_STEERS)]. This is the debate's cost lever (docs/specs/multi-agent-debate-spec.md § 8): the deterministic critics and the backtests cost zero tokens, the proposer fan-out is the N× spend. Must match the os.getenv default in `_pool_max()`, backend/archimedes/agents/debate_engine.py."
  type        = string
  default     = "10"
}

# ── Config consolidation (issue #1039 P5) ──────────────────────────────────
# Public wallet addresses (not secrets — no private key material), but
# operator-specific and NOT baked into the image or a box `.env` file. Set at
# `terraform apply` time (TF_VAR_platform_admin_wallets /
# TF_VAR_archimedes_treasury_wallet) so they land as first-class, IaC-tracked
# ECS task-definition environment values — the same "config lives in one
# place, not a box file" goal SSM SecureStrings serve for the actual secrets
# below. Empty defaults keep both features off (no admin-wallet bypass, no
# marketplace publish) until Dan supplies real values, matching the
# ARCHIMEDES_TREASURY_WALLET default in `.env.example`.

variable "platform_admin_wallets" {
  description = "Space/comma-separated wallet addresses allowed to publish `is_example` strategies to the marketplace (backend/archimedes/models/strategy_generators.py:wallet_can_publish; issue #1037). Also EVIDENCE for the admin dashboard gate since #1648: an account is admin when any of its OWN linked wallets is listed. Public addresses, not secrets."
  type        = string
  default     = ""
}

variable "platform_admin_accounts" {
  description = "Space/comma-separated canonical account identifiers (Better Auth `auth_users.id` values and/or emails) granted the admin cost/ops dashboard (backend/archimedes/services/platform_admin.py; issue #1648). The account-keyed allowlist: unlike `platform_admin_wallets` it survives a wallet unlink and needs no database read, so it is the break-glass during a datastore incident. Derive the value with backend/scripts/derive_platform_admin_accounts.py. Not secrets, but account-identifying — same `TF_VAR_` drift gotcha as the wallet list: re-pass it on every apply or it silently empties."
  type        = string
  default     = ""
}

variable "archimedes_treasury_wallet" {
  description = "Platform revenue-share wallet address (marketplace publish 10% split, PR #958). Public address, not a secret. Publishing 503s (backend/archimedes/api/marketplace_routes.py) until this is set."
  type        = string
  default     = ""
}

variable "generation_payment_recipient" {
  description = "Platform wallet address that receives x402 generation payments (flip-list #834). Public address, not a secret. Defaults to the platform generation-revenue DCW (Circle Console, wallet ID af3e1cf6-76a3-55db-911a-b356860058e4) so a terraform apply without the TF_VAR cannot silently un-configure the paywall; override via TF_VAR_generation_payment_recipient only to rotate the wallet."
  type        = string
  default     = "0xffa7abba5f17cb8471ebf150bf808bd6fb8856c1"
}

# ── Runner relocation (issue #1065 / #1043) ─────────────────────────────────
# Draft IaC — Dan applies POST-T3.2. See infra/runner_ec2.tf, infra/kb_runner.tf,
# infra/efs.tf, and the PR body for the full architecture + caveats.

variable "runner_instance_type" {
  description = "EC2 instance type for the dedicated oracle+agent runner box (issue #1065 decision #1 — a single instance, never an ASG). t3.small is generous headroom for two lightweight async Python loops; bump if the agent's regime/backtest compute proves heavier in practice."
  type        = string
  default     = "t3.small"
}

variable "kb_runner_cpu" {
  description = "Fargate task-level vCPU units for the scheduled kb-runner task (infra/kb_runner.tf). 1024 = 1 vCPU. Sized for the current skip-mode default (KB_PIPELINE_ENABLED unset) — bump substantially (and add GPU-appropriate compute, out of scope for Fargate) when the real ~6 GB SPECTER2/REBEL/SciSpacy pipeline is enabled."
  type        = string
  default     = "1024"
}

variable "kb_runner_memory" {
  description = "Fargate task-level memory (MiB) for the scheduled kb-runner task. Must pair validly with kb_runner_cpu — see https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-cpu-memory-error.html."
  type        = string
  default     = "4096"
}

variable "kb_runner_schedule_expression" {
  description = "EventBridge Scheduler rate/cron expression for the kb-runner scheduled Fargate task (infra/kb_runner.tf's aws_scheduler_schedule). Daily is a conservative default for a batch job that currently no-ops to a 'skipped' manifest.json (KB_PIPELINE_ENABLED unset) — tighten or loosen once the real pipeline is live and its actual runtime/cost is known."
  type        = string
  default     = "rate(1 day)"
}

variable "privacy_inbox_email" {
  description = <<-EOT
    Destination for mail sent to privacy@<domain> (SNS email subscription).

    Empty by default and left empty in the repo ON PURPOSE: the live endpoint
    is a personal inbox, and a personal address does not belong in a public
    repository. Set it in infra/terraform.tfvars (gitignored) to bring the
    existing subscription under management. Left unset, the subscription
    resource is not created and the live one stays unmanaged — the status quo,
    not a regression.

    Mirrors the alarm_email pattern in cloudwatch.tf.
  EOT
  type        = string
  default     = ""
}

# ── Email authentication (#1462) ─────────────────────────────────────────────

variable "google_site_verification" {
  description = <<-EOT
    The Google Search Console verification token already published in the apex
    TXT record. It is carried here because Route 53 keeps one record set per
    (name, type): the SPF string has to share the apex TXT with it, so both
    values must be written in the same resource. Dropping this un-verifies
    Search Console. A published DNS value, not a secret.
  EOT
  type        = string
  default     = "google-site-verification=nHeZsrl8SxRsJeKWIQx0kaSQkHOlzPDdfRZU_ZCUqk8"
}

variable "dmarc_rua_address" {
  description = <<-EOT
    Mailbox for DMARC aggregate reports (the rua= tag).

    Read by TWO resources and they must agree: aws_route53_record.dmarc below
    publishes it to the world, and aws_ses_receipt_rule.dmarc_reports
    (dmarc_reports.tf, #1504) is what makes mail to it actually land somewhere.
    Changing this in one place only means reporters send to an address with no
    rule behind it. Nothing is stored, and no failure surfaces on our side —
    any delivery error is the reporting receiver's to handle, where we never
    see it. The symptom is an empty bucket, which is indistinguishable from
    "nobody is spoofing us".

    Procedure and interpretation: docs/runbooks/dmarc-reports.md.
  EOT
  type        = string
  default     = "dmarc-reports@archimedes-arc.com"
}

variable "public_trace_vaults" {
  description = "Comma-separated house/demo vault addresses whose ownerless reasoning traces form the public proof surface (backend/archimedes/services/trace_visibility.py, #1556). Public on-chain addresses, not secrets. The default is the five house vaults observed in the live trace store on 2026-08-31; ownerless traces from any other vault go private once this is applied."
  type        = string
  default     = "0x88F284e6667947d66949528dB209b2a50bf2f612,0x99120A79f54F83f6729E1E1e2B1f536952BF3574,0x9d4530e874D712d3F0f65c49F9355403bf232e66,0xA3b077e16C208cD794581db46b559FDC9619ada7,0xcdd47c6D16a206f2C69B6D533Ac98b56Db3CeF52"
}

# ── SES bounce/complaint drain (#1804, infra/ses_events.tf) ─────────────────

variable "ses_events_drain_cpu" {
  description = "Fargate task-level vCPU units for the scheduled ses-events-drain task (infra/ses_events.tf). 256 = 0.25 vCPU, the Fargate minimum — the job reads a small SQS batch and writes one UPDATE per bounce, so this is sized for the boto3 + SQLAlchemy import cost, not for the work."
  type        = string
  default     = "256"
}

variable "ses_events_drain_memory" {
  description = "Fargate task-level memory (MiB) for the scheduled ses-events-drain task. Must pair validly with ses_events_drain_cpu (256 cpu allows 512, 1024 or 2048) — see https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-cpu-memory-error.html."
  type        = string
  default     = "1024"
}

variable "ses_events_drain_schedule_expression" {
  description = "EventBridge Scheduler rate/cron expression for the SES bounce/complaint drain (infra/ses_events.tf's aws_scheduler_schedule.ses_events_drain). Every 15 minutes is the latency a person retrying a signup would notice; the queue's 14-day retention means a looser interval loses nothing, it only makes the refusal later. Set to a longer rate to cut cost, not to `null` — there is no 'off' here short of setting the schedule's state, and a drain that never runs is the defect #1804 opened against."
  type        = string
  default     = "rate(15 minutes)"
}

# ── DMARC weekly summary (#1504, infra/dmarc_reports.tf) ────────────────────

variable "dmarc_summary_cpu" {
  description = "Fargate task-level vCPU units for the weekly DMARC summary task (infra/dmarc_reports.tf). 256 = 0.25 vCPU, the Fargate minimum — the job lists a fortnight of a few-KB objects, parses them in memory and sends one email, so this is sized for the boto3 import cost, not for the work."
  type        = string
  default     = "256"
}

variable "dmarc_summary_memory" {
  description = "Fargate task-level memory (MiB) for the weekly DMARC summary task. Must pair validly with dmarc_summary_cpu (256 cpu allows 512, 1024 or 2048) — see https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-cpu-memory-error.html. 1024 matches the ses-events-drain task rather than being measured; the parser refuses to decompress past 64 MB (archimedes/scripts/dmarc_reports.py's MAX_UNCOMPRESSED_BYTES) so a hostile report cannot make the job want more."
  type        = string
  default     = "1024"
}

variable "dmarc_summary_schedule_expression" {
  description = <<-EOT
    EventBridge Scheduler expression for the weekly DMARC aggregate-report
    summary (infra/dmarc_reports.tf's aws_scheduler_schedule.dmarc_weekly_summary).

    Monday 13:00 UTC. Monday because a new source IP claiming to send as this
    domain is something to act on during a working week; 13:00 UTC because it
    is inside a waking day on both sides of the Atlantic.

    WEEKLY IS NOT DECORATION. The owner asked for a weekly summary
    (#1504, 2026-09-03), and the message's ARRIVAL is what tells him the
    collection path is alive — a quiet week still sends a "no reports received"
    line, precisely so silence stays meaningful. A summary that quietly became
    hourly is a summary nobody reads, and one that became monthly is a
    fortnight of evidence discovered too late. So
    backend/tests/test_dmarc_summary_wiring.py fails unless this expression is
    a cron naming exactly ONE weekday: tighten it to daily during the ramp by
    changing that guard deliberately, not by editing this default past it.

    There is no "off" here short of the schedule's own `state`.
  EOT
  type        = string
  default     = "cron(0 13 ? * MON *)"
}

# ── Outage paging (issue #1818 P5) ──────────────────────────────────────────

variable "owner_alert_email" {
  description = <<-EOT
    The address the owner will ACTUALLY SEE a CloudWatch alarm at. Subscribed
    to the `archimedes-alerts` SNS topic (infra/cloudwatch.tf) alongside — and
    required to differ from — `alarm_email`.

    WHY A SECOND ADDRESS, MEASURED. Issue #1818 P5 reads "there was no alarm".
    The account says otherwise: on 2026-09-03, during a 94-minute outage,
    `archimedes-alb-unhealthy-hosts` went to ALARM at 13:38:46Z,
    `archimedes-alb-5xx-rate-high` at 13:39:16Z, and this topic delivered SIX
    notifications with zero failures (AWS/SNS NumberOfNotificationsDelivered /
    NumberOfNotificationsFailed). The owner still found out by loading the
    site. The gap is not a missing alarm and not a missing subscription — it
    is that a delivered email did not reach anyone's attention. Terraform
    cannot fix that; the only thing it can do is force the question to be
    answered, by making the destination a required input and refusing to let
    it be a duplicate of the mailbox that already failed.

    NO DEFAULT — DELIBERATE. Apart from `aurora_master_password` this is the
    only defaultless variable in the file. A default would let an apply
    succeed without anyone deciding where a page goes, which is the class of
    silence #1818 is about. Pick a channel you will see: a mailbox you
    actually watch, an SMS gateway, a PagerDuty/Opsgenie intake address.

    MUST DIFFER FROM `alarm_email`, enforced by a `precondition` on
    `aws_sns_topic_subscription.owner_alerts_email`. SNS keys a subscription
    by (topic, protocol, endpoint), so the same address in both variables
    would give two Terraform resources one SubscriptionArn — and an apply that
    dropped either would unsubscribe the other, leaving the topic with no
    subscriber while state claimed otherwise.

    NOT a personal address in this repo: the repository is public. Set it in
    infra/terraform.tfvars (gitignored) — see infra/README.md § "Operational
    variables" and infra/terraform.tfvars.example.

    AWS emails a confirmation link on the first apply; the subscription pages
    nobody until it is clicked, and an unconfirmed subscription is
    indistinguishable from a confirmed one on Terraform's side. Confirm it,
    then run the alarm drill in infra/runbooks/disaster-recovery.md § Drills —
    "the mail arrives AND I notice it" is the only assertion that matters here
    and it is the one 2026-09-03 falsified.
  EOT
  type        = string

  validation {
    # Rejects "" along with anything else not shaped like an address. A
    # `count` gate would be the wrong tool: an empty value would apply cleanly
    # and page nobody, which is the shape being designed out.
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.owner_alert_email))
    error_message = "owner_alert_email must be a real email address, and one you will actually see — issue #1818 P5's measured finding is that six alarm emails were delivered on 2026-09-03 and still did not reach the owner. Set it in infra/terraform.tfvars."
  }
}
