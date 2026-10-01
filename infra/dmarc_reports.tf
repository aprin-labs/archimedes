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
    object_key_prefix = "reports/"
    position          = 1
  }

  # SES validates the write at rule-CREATE time: without the policy already in
  # place the create fails with "Could not write to bucket". Terraform has no
  # way to infer this ordering from the arguments above, because the rule
  # references the bucket, not the policy.
  depends_on = [aws_s3_bucket_policy.dmarc_reports]
}
