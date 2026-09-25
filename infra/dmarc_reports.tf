# ── DMARC aggregate-report inbox for ${var.domain_name} (#1504) ──────────────
#
# THE GAP THIS CLOSES. dns_email.tf already publishes
#
#   v=DMARC1; p=none; rua=mailto:dmarc-reports@archimedes-arc.com; fo=1
#
# and ses_inbound.tf already points the zone's MX at SES inbound. So the world
# is being TOLD to send us aggregate reports — but the active receipt rule set
# holds exactly one rule (privacy-inbox, recipients privacy@), so nothing
# matches `dmarc-reports@` and nothing is stored. Verified live 2026-09-03 with
# `aws ses describe-active-receipt-rule-set`. #1504's precondition ("no receipt
# rule delivers it — so no reports are being collected today") is exactly this
# file's absence.
#
# The failure mode is silent in both directions. Whatever SES does with a
# message whose recipient matches no rule, the outcome here is the same: nothing
# lands, and no failure surfaces in our account — any delivery error is handled
# by the reporting receiver, on their side, where we never see it. And an empty
# bucket looks identical to "nobody is spoofing us". That second reading is the
# dangerous one, and it is why scripts/dmarc_report_summary.py exits NON-ZERO
# when it parses zero reports instead of printing an empty all-clear table.
#
# WHAT THIS FILE DELIBERATELY DOES NOT DO. It does not touch
# aws_route53_record.dmarc. The policy stays at p=none. Moving to
# p=quarantine/p=reject is the rest of #1504 and is evidence-led — it needs a
# fortnight of the reports this file starts collecting, which by definition do
# not exist until this is applied. See docs/runbooks/dmarc-reports.md.
#
# ORDERING NOTE. The receipt RULE SET (aws_ses_receipt_rule_set.inbound), the
# active-rule-set binding, and the MX record all live in ses_inbound.tf and are
# reused here — a second rule set would not be active and would collect
# nothing. This file is separate from ses_inbound.tf because that file is
# written to be IMPORTED onto CLI-created resources; everything below is a
# genuine create.

locals {
  # Named once because the bucket policy has to spell the rule's ARN out as a
  # string literal: referencing aws_ses_receipt_rule.dmarc_reports from the
  # policy that the rule itself depends_on would be a dependency cycle.
  dmarc_receipt_rule_name = "dmarc-reports"

  # The key prefix the receipt rule writes under, named once because FOUR
  # things have to agree on it: the s3_action below, the weekly summary task's
  # DMARC_REPORTS_PREFIX, and the two statements of that task's read grant.
  # `aws_ses_receipt_rule.s3_action` is a SET in the provider schema and its
  # elements have no addressable index, so the other three cannot read the
  # value off the resource — they read it from here instead. A prefix mismatch
  # is invisible: the job lists nothing and reports "no reports received",
  # which is the false all-clear this whole file is built against.
  dmarc_object_key_prefix = "reports/"
}

# ── Bucket the reports land in ───────────────────────────────────────────────
#
# Account-suffixed because S3 bucket names are globally unique. Uses the live
# caller identity rather than a hardcoded number (alb.tf still carries the
# pre-migration account id in its bucket name) so this cannot be applied into
# the wrong account under a name that claims otherwise.
resource "aws_s3_bucket" "dmarc_reports" {
  bucket = "${var.project_name}-dmarc-reports-${data.aws_caller_identity.current.account_id}"

  tags = { Project = var.project_name }
}

resource "aws_s3_bucket_public_access_block" "dmarc_reports" {
  bucket = aws_s3_bucket.dmarc_reports.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# SSE-S3 (AES256), NOT SSE-KMS. An SES receipt rule can write to a KMS-encrypted
# bucket only if the rule itself is given the key (s3_action.kms_key_arn) and
# the key policy admits SES; getting that wrong fails the delivery rather than
# the apply. Aggregate reports are DNS-derived telemetry about mail we sent in
# public — the sensitivity here is "not world-readable", which the public-access
# block above provides.
resource "aws_s3_bucket_server_side_encryption_configuration" "dmarc_reports" {
  bucket = aws_s3_bucket.dmarc_reports.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# 180 days. The decision these reports feed (p=none → quarantine → reject) reads
# a fortnight at a time; six months keeps a full ramp plus the before/after
# window for the PR that finally moves the policy, and keeps the bucket from
# growing without bound afterwards. Reports are a few KB each — this is a
# retention statement, not a cost control.
resource "aws_s3_bucket_lifecycle_configuration" "dmarc_reports" {
  bucket = aws_s3_bucket.dmarc_reports.id

  rule {
    id     = "expire-aggregate-reports"
    status = "Enabled"

    filter {}

    expiration {
      days = 180
    }
  }
}

# The grant SES needs to write, and nothing else.
#
# Both conditions matter and they are not redundant: SourceAccount stops another
# AWS account's SES from writing into our bucket, and SourceArn narrows the
# grant to this one receipt rule rather than every rule we ever add. Those two
# ARE the boundary here — not the object key.
#
# WHY THE RESOURCE IS THE WHOLE BUCKET AND NOT `/reports/*`. That is the policy
# AWS documents for this exact action (SES developer guide, "Give SES permission
# to write to an S3 bucket": Resource "arn:aws:s3:::bucket/*", the same two
# conditions). SES validates the write when the receipt rule is CREATED, not on
# the first message, and the documented grant is bucket-wide — a prefix-scoped
# Resource risks failing that validation with "Could not write to bucket" at the
# owner's apply, for no gain: the only principal admitted is SES, only from this
# account, only acting as this one rule, and that rule is configured below to
# write under reports/ and nowhere else. alb.tf's log-delivery grant has the
# same bucket-wide shape.
#
# DenyNonTLS also mirrors alb.tf, where it is live and does not block AWS's own
# log delivery. SES's PutObject is likewise an HTTPS call, so this should be
# inert — but that is reasoning, not a live observation on THIS write path, so
# the runbook makes it step 5 of the "no reports are arriving" ladder.
resource "aws_s3_bucket_policy" "dmarc_reports" {
  bucket = aws_s3_bucket.dmarc_reports.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowSESReceiptRulePut"
        Effect    = "Allow"
        Principal = { Service = "ses.amazonaws.com" }
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.dmarc_reports.arn}/*"
        Condition = {
          StringEquals = {
            "AWS:SourceAccount" = data.aws_caller_identity.current.account_id
          }
          ArnLike = {
            "AWS:SourceArn" = "arn:aws:ses:${var.aws_region}:${data.aws_caller_identity.current.account_id}:receipt-rule-set/${aws_ses_receipt_rule_set.inbound.rule_set_name}:receipt-rule/${local.dmarc_receipt_rule_name}"
          }
        }
      },
      {
        Sid       = "DenyNonTLS"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.dmarc_reports.arn,
          "${aws_s3_bucket.dmarc_reports.arn}/*",
        ]
        Condition = {
          Bool = { "aws:SecureTransport" = "false" }
        }
      },
    ]
  })
}

# ── The receipt rule ─────────────────────────────────────────────────────────
#
# `after` pins this rule behind the existing privacy-inbox rule. Without it
# Terraform would insert at position 1 and reorder the live rule set on every
# apply. Order is not a correctness concern here — the two rules' `recipients`
# are disjoint and neither carries a stop_action, so SES applies whichever
# matches — but a rule set that churns its order on every plan is noise nobody
# should have to read past.
#
# tls_policy = "Optional", same call as the privacy inbox and for a sharper
# reason: "Require" bounces a sender whose TLS we do not like, and the senders
# here are Google, Yahoo, Microsoft and a long tail of smaller receivers whose
# report-generating MTAs we do not control. A bounced report is a hole in the
# evidence that this issue exists to collect.
#
# scan_enabled adds SES's spam/virus verdict HEADERS to the stored message. It
# does not drop anything on its own (that would need a stop_action keyed on the
# verdict, which is deliberately absent) — the headers are just there if a
# report ever looks forged.
resource "aws_ses_receipt_rule" "dmarc_reports" {
  name          = local.dmarc_receipt_rule_name
  rule_set_name = aws_ses_receipt_rule_set.inbound.rule_set_name
  recipients    = [var.dmarc_rua_address]
  enabled       = true
  scan_enabled  = true
  tls_policy    = "Optional"
  after         = aws_ses_receipt_rule.privacy_inbox.name

  s3_action {
    bucket_name       = aws_s3_bucket.dmarc_reports.id
    object_key_prefix = local.dmarc_object_key_prefix
    position          = 1
  }

  # SES validates the write at rule-CREATE time: without the policy already in
  # place the create fails with "Could not write to bucket". Terraform has no
  # way to infer this ordering from the arguments above, because the rule
  # references the bucket, not the policy.
  depends_on = [aws_s3_bucket_policy.dmarc_reports]
}

# ── THE WEEKLY SUMMARY — what actually reads the bucket (#1504) ─────────────
#
# WHY IT EXISTS. Everything above collects. Nothing above LOOKS. The owner's
# call on 2026-09-03 is explicit: "the aggregate-report parser runs on a
# schedule and posts a weekly summary (per-source pass/fail table) — scheduled
# job + a summary destination"; on 2026-09-04 the destination was settled as
# email to the alert address, sent from no-reply@ through the SES identity the
# verification mail already uses.
#
# The decision this feeds is #1504's own — moving _dmarc off p=none — and it
# needs a fortnight of enumerated sources. A fortnight only accumulates if
# somebody reads weekly; discovering the backlog on the day you want to change
# the policy is how you end up guessing.
#
# THE MESSAGE IS THE HEARTBEAT. A quiet week still sends: the job mails a
# one-line "no reports received" and exits 0. An empty bucket looks exactly
# like an un-spoofed domain, and a job that stays silent on a quiet week looks
# exactly like a job that stopped running, a task role that lost its S3 grant,
# or a schedule somebody disabled. So the ARRIVAL of the Monday mail is the
# signal that this whole file is alive, and its ABSENCE is the alarm. That is
# why there is no CloudWatch alarm here and why ses_events.tf's two alarms are
# not mirrored: the thing being watched there is a queue nobody looks at, and
# the thing being watched here is an email a person reads. A second, weaker
# monitor on task exit code would be one more thing to mute. The residual —
# a send that fails leaves only a non-zero task exit in the log group — is
# written down in docs/runbooks/dmarc-reports.md § 'The weekly summary'.
#
# SHAPE: a dedicated single-container Fargate task definition invoked by an
# EventBridge Scheduler schedule, copied from aws_ecs_task_definition
# .ses_events_drain (infra/ses_events.tf) and, before it, infra/ecs_migrate.tf.
# NOT a command override on aws_ecs_task_definition.backend: that family runs
# three containers, and its nginx sidecar `dependsOn` the backend being HEALTHY
# — with the backend command overridden to a batch job that never serves HTTP,
# the dependency is never satisfied and ECS is liable to kill the task
# mid-summary.
#
# NO NEW IMAGE. `python -m archimedes.scripts.dmarc_weekly_summary` lives in
# the backend package, which is exactly why the PARSER moved there too
# (archimedes/scripts/dmarc_reports.py): backend/Dockerfile copies `backend/`
# and nothing else, so the repo's top-level scripts/ directory — where
# dmarc_report_summary.py lives — is not in this image at all. One parser, two
# callers, one answer about whether the domain is being spoofed.

resource "aws_ecs_task_definition" "dmarc_weekly_summary" {
  family                   = "${var.project_name}-dmarc-weekly-summary"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.dmarc_summary_cpu
  memory                   = var.dmarc_summary_memory

  # Both reused from ecs.tf. The TASK role is the identity that reads the
  # bucket and calls SES; the EXECUTION role pulls the image and writes the log
  # stream. No new roles — see the two grants below for what the task role
  # gains and what it already had.
  execution_role_arn = aws_iam_role.ecs_task_execution.arn
  task_role_arn      = aws_iam_role.ecs_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([
    {
      name = "dmarc-weekly-summary"
      # APPLY ORDERING. This is `archimedes-backend:latest`, which is whatever
      # CI last pushed from main — NOT this branch. The module named below
      # ships with the same PR as this resource, so applying before the branch
      # is merged and `deploy.yml` has pushed a new `:latest` gives the first
      # scheduled task a `ModuleNotFoundError`, no mail, and — by the no-alarm
      # design above — nothing but a task exit code to say so. Merge, let CI
      # push, apply, then prove it with `--dry-run` or a manual `aws ecs
      # run-task` rather than waiting for a Monday. Written down in
      # docs/runbooks/dmarc-reports.md § 'The weekly summary'.
      image     = "${aws_ecr_repository.backend.repository_url}:${var.backend_image_tag}"
      essential = true

      # Run-to-completion: no portMappings, no healthCheck, no dependsOn. The
      # job lists two weeks of objects, parses them, sends one message and
      # exits — 0 sent, 2 misconfigured, 3 could not read the bucket, 4 could
      # not send. 3 and 4 are deliberately distinct from 0 and from each other:
      # "I could not look" must never render as "I looked and found nothing".
      command = ["python", "-m", "archimedes.scripts.dmarc_weekly_summary"]

      environment = [
        { name = "AWS_REGION", value = var.aws_region },
        # Off the bucket resource, never a literal. A wrong bucket name here
        # produces an empty listing, which the job would report as "no reports
        # received" — the exact false all-clear this issue is about.
        { name = "DMARC_REPORTS_BUCKET", value = aws_s3_bucket.dmarc_reports.id },
        # Must match the receipt rule's own object_key_prefix above; a prefix
        # mismatch reads as an empty bucket for the same reason.
        { name = "DMARC_REPORTS_PREFIX", value = local.dmarc_object_key_prefix },
        # THE DESTINATION. var.owner_alert_email is the address #1818 P5
        # established the owner actually reads — six alarm mails were delivered
        # to `alarm_email` during the 2026-09-03 outage and still did not reach
        # him. A summary sent to an address nobody opens is the same silence
        # this file exists to break, one step further along.
        { name = "DMARC_SUMMARY_TO", value = var.owner_alert_email },
        # The verified domain identity the verification and reset mail already
        # goes out as, spelled the same way infra/ecs.tf's auth container
        # spells it. A different sender would need a second SES identity and a
        # second IAM grant, and would be a second thing to keep aligned in DNS.
        { name = "EMAIL_SENDER", value = "no-reply@${var.domain_name}" },
      ]

      # NO SECRETS BLOCK, deliberately unlike ecs_migrate.tf and the
      # ses-events-drain task. Those write to Aurora and therefore need
      # DATABASE_URL. This job touches no database: it reads S3, parses XML in
      # memory, and calls SES. `archimedes.scripts.dmarc_weekly_summary` imports
      # only the standard library, boto3 and
      # `archimedes.scripts.dmarc_reports`, and `archimedes/scripts/__init__.py`
      # is empty — so nothing on that import path resolves DATABASE_URL at
      # import time the way `archimedes.services` would.
      # backend/tests/test_dmarc_summary_wiring.py pins that import surface, so
      # a future import of `archimedes.services.*` fails a test here rather
      # than crash-looping a Monday-morning task in production.

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.app.name # cloudwatch.tf
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "dmarc-weekly-summary"
        }
      }
    }
  ])

  tags = { Project = var.project_name }
}

# ── IAM — the two grants the job needs, and where each comes from ───────────
#
# READ THE BUCKET: added here, on the shared ECS task role (ecs.tf), the same
# way infra/ses_events.tf adds its queue grant. Scoped to this one bucket and
# to reading only — the job never writes, and never deletes, so the 180-day
# lifecycle rule above stays the only thing that removes a report.
#
# SEND THE MAIL: NOT added here. aws_iam_role_policy.ecs_task_ses_send
# (infra/ecs.tf) already grants this role `ses:SendEmail` on
# `identity/${var.domain_name}`, which is precisely the identity this job sends
# as — that grant is what makes reusing no-reply@ free rather than a second
# thing to provision. A duplicate statement here would be redundant IAM that
# drifts. backend/tests/test_dmarc_summary_wiring.py asserts the ecs.tf grant
# still exists and still names the identity, so deleting it there fails a test
# rather than silently turning the Monday mail off.
resource "aws_iam_role_policy" "ecs_task_dmarc_reports_read" {
  name = "archimedes-ecs-dmarc-reports-read"
  role = aws_iam_role.ecs_task.id # ecs.tf

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ListDmarcReportsBucket"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.dmarc_reports.arn
        # ListBucket is granted on the BUCKET arn and filtered by prefix here,
        # because s3:prefix is the only way to scope a listing — an object-arn
        # Resource does not restrict it.
        Condition = {
          StringLike = { "s3:prefix" = ["${local.dmarc_object_key_prefix}*"] }
        }
      },
      {
        Sid      = "ReadDmarcReports"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.dmarc_reports.arn}/${local.dmarc_object_key_prefix}*"
      },
    ]
  })
}

# ── IAM — EventBridge Scheduler's own role ──────────────────────────────────
# Same shape as aws_iam_role.ses_events_scheduler (infra/ses_events.tf), scoped
# to this one task family and this one cluster.

resource "aws_iam_role" "dmarc_summary_scheduler" {
  name = "archimedes-dmarc-summary-scheduler-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
      }
    }]
  })
  tags = { Project = var.project_name }
}

resource "aws_iam_role_policy" "dmarc_summary_scheduler_run_task" {
  name = "archimedes-dmarc-summary-scheduler-run-task"
  role = aws_iam_role.dmarc_summary_scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "RunDmarcWeeklySummaryTask"
        Effect = "Allow"
        Action = ["ecs:RunTask"]
        # Family-wildcard (revision-less) ARN, matching the schedule target
        # below — the grant stays valid across every future revision without a
        # terraform change.
        Resource = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task-definition/${aws_ecs_task_definition.dmarc_weekly_summary.family}:*"
        Condition = {
          ArnLike = { "ecs:cluster" = aws_ecs_cluster.main.arn } # ecs.tf
        }
      },
      {
        Sid      = "PassDmarcWeeklySummaryRoles"
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = [aws_iam_role.ecs_task_execution.arn, aws_iam_role.ecs_task.arn] # ecs.tf
        Condition = {
          StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" }
        }
      }
    ]
  })
}

# ── Schedule ────────────────────────────────────────────────────────────────
#
# Monday 13:00 UTC, weekly (var.dmarc_summary_schedule_expression). Monday
# because the summary is a thing to act on during a working week, not something
# to land on a Friday evening and be read on Monday anyway; 13:00 UTC because
# it is the morning in the Americas and the afternoon in Europe, so it is
# inside a waking day for everyone who might have to answer for a new source.
#
# `task_definition_arn` is the REVISION-LESS family ARN on purpose, exactly as
# aws_scheduler_schedule.ses_events_drain does it: it resolves to the latest
# ACTIVE revision at invocation, so a CI-registered revision is picked up
# without a schedule update. Pinning terraform's own `.arn` would freeze the
# schedule on whatever revision this apply happens to create.
#
# COST: 52 invocations a year. Fargate bills a 1-minute minimum, so this is
# 52 minutes/year at 0.25 vCPU + 1 GB ≈ $0.01, plus a handful of S3 GETs and 52
# SES messages (inside the free tier for mail sent from an AWS-hosted app). The
# interval is a variable so the owner can make it daily during the ramp without
# a code change — but it is not free-form: the guard in
# backend/tests/test_dmarc_summary_wiring.py fails if the expression stops
# naming exactly one weekday, because "weekly" is the cadence the owner asked
# for and a summary that silently became hourly is a summary nobody reads.
#
# NOT `flexible_time_window`: OFF keeps the arrival time predictable, which
# matters more here than anywhere else in this repo — the whole design leans on
# a person noticing that the Monday mail did not come.

resource "aws_scheduler_schedule" "dmarc_weekly_summary" {
  name                         = "${var.project_name}-dmarc-weekly-summary"
  schedule_expression          = var.dmarc_summary_schedule_expression
  schedule_expression_timezone = "UTC"
  state                        = "ENABLED"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_ecs_cluster.main.arn # ecs.tf
    role_arn = aws_iam_role.dmarc_summary_scheduler.arn

    ecs_parameters {
      task_definition_arn = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task-definition/${aws_ecs_task_definition.dmarc_weekly_summary.family}"
      launch_type         = "FARGATE"

      network_configuration {
        # Private subnets + the backend security group, the same static pair
        # ecs_migrate.tf and ses_events.tf use for their one-offs. Egress to
        # S3, SES, ECR and CloudWatch; no ingress is needed by a task that
        # never accepts a connection.
        subnets          = aws_subnet.private[*].id # vpc.tf
        security_groups  = [aws_security_group.ecs_backend.id]
        assign_public_ip = false
      }
    }

    retry_policy {
      # One retry. Nothing is lost by giving up: the reports stay in the bucket
      # for 180 days and next Monday's window overlaps this one by nothing —
      # but a missed week is a visible gap in the owner's inbox, which is the
      # point. A retry storm on a broken send would not make it more visible.
      maximum_retry_attempts = 1
    }
  }
}
