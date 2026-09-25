# ─────────────────────────────────────────────────────────────────────────────
# AWS cost kill switch — budget thresholds, a fast billing tripwire, and a
# Lambda that turns the compute off when spend runs away unattended.
#
# ⚠️  AUTHORED OFFLINE — NOT yet `terraform plan`/`apply`-verified. Written
#     without applying anything; `terraform fmt -check` and `terraform validate`
#     were run locally and pass. Before applying, run `terraform plan` from
#     infra/ and read every line of it. Same posture as cloudwatch.tf's header.
#
#     Blast radius of an apply: all resources here are NEW (budget, SNS topic,
#     Lambda + role + log group, one CloudWatch alarm) with ONE exception —
#     `aws_sns_topic_policy.alerts` below brings the EXISTING archimedes-alerts
#     topic's access policy under management. That topic currently carries only
#     the implicit AWS default policy; the resource reproduces it verbatim and
#     adds a single statement letting AWS Budgets publish. Read that block's
#     comment before applying. Nothing else here modifies a live object.
#
#     `terraform init` is required before the first plan: this file introduces
#     the `hashicorp/archive` provider (main.tf) for zipping the Lambda source.
#
# Owner directive, 2026-08-31: "If costs spike hard and I'm not around, we need
# a mechanism to cut things off."
#
# WHY THIS EXISTS WHEN infra/scripts/setup-budgets.sh ALREADY RAN
# ---------------------------------------------------------------
# That script created two budgets out-of-band (archimedes-monthly-200,
# archimedes-tripwire-25 — both live, both unmanaged by Terraform) and a
# Bedrock-deny budget action. Every brake it installs is an *email* or a
# *permission removal on one service*. None of them stop a single running
# resource, and all of them assume a human reads the mail. The directive above
# is explicitly about the case where no human does.
#
# This file deliberately uses a DIFFERENT budget name so it neither collides
# with nor adopts those two. They stay as-is. See docs/runbooks/cost-kill-switch.md.
#
# THE HONEST LIMIT OF THIS MECHANISM
# ----------------------------------
# AWS billing data lags. Budget evaluation runs roughly every 8-12 hours;
# `AWS/Billing` EstimatedCharges publishes every ~6 hours. A spike therefore
# runs for hours before either brake can see it. **This bounds damage. It does
# not make overspend impossible, and nothing in AWS does.** Two layers here,
# fast one first:
#
#   1. CloudWatch alarm on AWS/Billing EstimatedCharges (~6h lag)   → notify + kill
#   2. AWS Budgets ACTUAL thresholds 50 / 80 / 120% (~8-12h lag)    → notify, notify, kill
#
# WHAT THE KILL SWITCH SACRIFICES
# -------------------------------
# Availability, and only availability. It scales the ECS backend to zero and
# stops the runner EC2 instance. It does not have — and by IAM cannot obtain —
# any permission against Aurora, ElastiCache, S3, EBS snapshots or DynamoDB.
# The site goes down; no data is at risk. That asymmetry is what makes an
# unattended automatic trigger acceptable at all.
#
# Note it also does not zero the bill: Aurora, ElastiCache, the ALB, NAT and
# the WAF keep billing while the compute is off (~$5/day of the ~$8/day
# baseline). This removes the *spike*, not the floor. Killing the floor means
# `terraform destroy`, which is a human decision, not a Lambda's.
# ─────────────────────────────────────────────────────────────────────────────

# ── Variables ────────────────────────────────────────────────────────────────

variable "cost_budget_monthly_limit" {
  description = <<-EOT
    Monthly account-wide cost budget in USD. Thresholds are percentages of it:
    50% and 80% notify, 120% fires the kill switch.

    Default 500 is set against measured spend, not a guess. Cost Explorer,
    read 2026-08-31: recurring run rate is ~$7.97/day (14 clean days,
    2026-08-15..29 excluding the 20th) = ~$243/month, and July 2026 closed at
    $241.41. The largest known benign one-off is the $274.00 Amazon Registrar
    domain renewal that landed on 2026-08-20; a renewal month therefore costs
    about $517, which is 103% of this budget — 80% notify fires, the 120% kill
    threshold ($600) does NOT. That headroom is the whole reason the default is
    500 rather than a tighter 300.
  EOT
  type        = number
  default     = 500
}

variable "cost_kill_switch_billing_threshold_usd" {
  description = <<-EOT
    Absolute month-to-date USD figure at which the AWS/Billing EstimatedCharges
    alarm trips. Defaults to 600 = 120% of the default 500 budget, i.e. the same
    dollar boundary the budget's kill threshold uses — deliberately the same
    point, reached ~6h sooner. Change both together or the fast tripwire stops
    being "the budget, earlier" and becomes a second, differently-calibrated
    switch nobody remembers the numbers for.
  EOT
  type        = number
  default     = 600
}

variable "cost_kill_switch_billing_alarm_arms_kill_switch" {
  description = <<-EOT
    Whether the EstimatedCharges alarm invokes the kill switch, or only notifies.

    Default true. Safe at the default threshold because that threshold is the
    same dollar boundary the budget's 120% kill already crosses — arming it buys
    hours, it does not add a new way to be shut down. Set false if you lower
    cost_kill_switch_billing_threshold_usd to a value a normal month can reach:
    EstimatedCharges is cumulative month-to-date, so a low absolute threshold
    trips every month around the same date, and an armed low threshold would
    take production down on schedule.
  EOT
  type        = bool
  default     = true
}

variable "cost_budget_time_period_start" {
  description = "Budget start month, YYYY-MM-DD_HH:MM. Pinned to a literal so successive plans are stable (an unset value defaults to 'now' and shows as drift)."
  type        = string
  default     = "2026-09-01_00:00"
}

# ── Data sources ─────────────────────────────────────────────────────────────
# `data.aws_caller_identity.current` already exists (ecs.tf). The partition is
# added here so the IAM ARNs below are written the way AWS documents them rather
# than with a hardcoded "aws" that quietly breaks in GovCloud/China partitions.

data "aws_partition" "current" {}

# ── Locals ───────────────────────────────────────────────────────────────────

locals {
  cost_kill_switch_name = "${var.project_name}-cost-kill-switch"

  # The AWS-managed default SNS access policy, as read live off
  # arn:aws:sns:us-east-1:037613907429:archimedes-alerts on 2026-08-31 (the topic
  # had no explicit policy — this IS what SNS synthesises). Reproduced verbatim
  # so bringing the topic under management is a no-op for every existing
  # publisher, in particular the CloudWatch alarms in cloudwatch.tf and
  # runner_ec2.tf, which publish as the account owner and are allowed by the
  # AWS:SourceOwner condition rather than by a service principal.
  sns_default_statement_actions = [
    "SNS:GetTopicAttributes",
    "SNS:SetTopicAttributes",
    "SNS:AddPermission",
    "SNS:RemovePermission",
    "SNS:DeleteTopic",
    "SNS:Subscribe",
    "SNS:ListSubscriptionsByTopic",
    "SNS:Publish",
  ]
}

# ── SNS: the kill-switch topic ───────────────────────────────────────────────
# Separate from aws_sns_topic.alerts on purpose. `alerts` fans out to a human
# inbox; this one fans out to a Lambda that turns production off. Anything
# subscribed here has consequences, so the subscriber list stays a list of one
# and is visible in a file whose name says what it does.

resource "aws_sns_topic" "cost_kill_switch" {
  name = local.cost_kill_switch_name
  tags = { Project = var.project_name }
}

resource "aws_sns_topic_policy" "cost_kill_switch" {
  arn = aws_sns_topic.cost_kill_switch.arn

  policy = <<-JSON
    {
      "Version": "2012-10-17",
      "Id": "archimedes-cost-kill-switch-policy",
      "Statement": [
        {
          "Sid": "AccountOwnerFullAccess",
          "Effect": "Allow",
          "Principal": { "AWS": "*" },
          "Action": ${jsonencode(local.sns_default_statement_actions)},
          "Resource": "${aws_sns_topic.cost_kill_switch.arn}",
          "Condition": {
            "StringEquals": { "AWS:SourceOwner": "${data.aws_caller_identity.current.account_id}" }
          }
        },
        {
          "Sid": "AllowBudgetsPublish",
          "Effect": "Allow",
          "Principal": { "Service": "budgets.amazonaws.com" },
          "Action": "SNS:Publish",
          "Resource": "${aws_sns_topic.cost_kill_switch.arn}",
          "Condition": {
            "StringEquals": { "aws:SourceAccount": "${data.aws_caller_identity.current.account_id}" }
          }
        }
      ]
    }
  JSON
}

# ── SNS: bring the EXISTING alerts topic policy under management ─────────────
#
# ⚠️  THE ONE RESOURCE IN THIS FILE THAT TOUCHES A LIVE OBJECT.
#
# AWS Budgets publishes as the `budgets.amazonaws.com` service principal. The
# archimedes-alerts topic's implicit default policy allows only the account
# owner (Principal AWS:* gated on AWS:SourceOwner), so a budget notification
# pointed at it is silently dropped — no error at apply time, no email, and the
# 50%/80% rungs of this ladder would simply never be heard. Fixing that means
# writing an explicit policy, and writing an explicit policy replaces the
# implicit default wholesale.
#
# Statement 1 below is therefore the default policy reproduced exactly as read
# off the live topic on 2026-08-31 (see local.sns_default_statement_actions).
# Statement 2 is the only addition. Existing publishers — every alarm in
# cloudwatch.tf and runner_ec2.tf — are covered by statement 1, unchanged.
#
# If you would rather not have Terraform own this policy, delete this resource
# and add `subscriber_email_addresses = [var.alarm_email]` to the 50% and 80%
# notification blocks instead. You lose the single-fan-out property (the topic's
# other subscribers stop hearing budget alerts) and gain one fewer live object
# in the plan. That trade was made the other way here because the budget rungs
# are worthless if nobody receives them.

resource "aws_sns_topic_policy" "alerts" {
  arn = aws_sns_topic.alerts.arn

  policy = <<-JSON
    {
      "Version": "2012-10-17",
      "Id": "archimedes-alerts-policy",
      "Statement": [
        {
          "Sid": "AccountOwnerFullAccess",
          "Effect": "Allow",
          "Principal": { "AWS": "*" },
          "Action": ${jsonencode(local.sns_default_statement_actions)},
          "Resource": "${aws_sns_topic.alerts.arn}",
          "Condition": {
            "StringEquals": { "AWS:SourceOwner": "${data.aws_caller_identity.current.account_id}" }
          }
        },
        {
          "Sid": "AllowBudgetsPublish",
          "Effect": "Allow",
          "Principal": { "Service": "budgets.amazonaws.com" },
          "Action": "SNS:Publish",
          "Resource": "${aws_sns_topic.alerts.arn}",
          "Condition": {
            "StringEquals": { "aws:SourceAccount": "${data.aws_caller_identity.current.account_id}" }
          }
        }
      ]
    }
  JSON
}

# ── AWS Budgets: the three-rung ladder ───────────────────────────────────────
# 50% -> notify, 80% -> notify, 120% -> KILL.
#
# All three are ACTUAL, not FORECASTED. A forecast-driven kill switch would
# shut production down over a projection, and AWS's forecast is noisy in the
# first days of a month on an account this small. Forecast alerting already
# exists on the script-created archimedes-monthly-200 budget; that is the right
# home for it, because it only sends mail.
#
# Named distinctly from the two script-created budgets so `terraform apply`
# neither collides with nor silently adopts them.

resource "aws_budgets_budget" "cost_kill_switch" {
  name              = "${var.project_name}-cost-kill-switch-monthly"
  budget_type       = "COST"
  time_unit         = "MONTHLY"
  limit_amount      = format("%.2f", var.cost_budget_monthly_limit)
  limit_unit        = "USD"
  time_period_start = var.cost_budget_time_period_start

  # Rung 1 — informational. At the measured ~$243/month baseline this lands
  # around the 30th of a normal month, so hearing it late in the month is the
  # expected, healthy signal; hearing it on the 10th is the anomaly.
  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 50
    threshold_type            = "PERCENTAGE"
    notification_type         = "ACTUAL"
    subscriber_sns_topic_arns = [aws_sns_topic.alerts.arn]
  }

  # Rung 2 — this should not happen without a reason you can name.
  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 80
    threshold_type            = "PERCENTAGE"
    notification_type         = "ACTUAL"
    subscriber_sns_topic_arns = [aws_sns_topic.alerts.arn]
  }

  # Rung 3 — the human "budget exceeded" signal, one step before the kill.
  # Exists because the API constraint below forces the kill rung to carry the
  # Lambda topic alone, so this is where a person hears it through the same
  # channel as rungs 1 and 2.
  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 100
    threshold_type            = "PERCENTAGE"
    notification_type         = "ACTUAL"
    subscriber_sns_topic_arns = [aws_sns_topic.alerts.arn]
  }

  # Rung 4 — KILL. ONE subscriber, and that is an AWS API constraint, not a
  # choice: CreateNotification rejects a second SNS subscriber per notification
  # (CreationLimitExceededException, learned on the 2026-09-01 first apply).
  # The "a broken Lambda cannot mean silence" property this rung used to carry
  # via a second topic lives elsewhere now, twice over: the 100% rung above
  # mails a human before this fires, and the EstimatedCharges tripwire
  # (billing_estimated_charges_high) fires BOTH topics at the same dollar
  # boundary — CloudWatch alarms, unlike Budgets notifications, allow it.
  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 120
    threshold_type            = "PERCENTAGE"
    notification_type         = "ACTUAL"
    subscriber_sns_topic_arns = [aws_sns_topic.cost_kill_switch.arn]
  }
}

# ── CloudWatch: the fast tripwire ────────────────────────────────────────────
#
# AWS/Billing EstimatedCharges is month-to-date cumulative and is published
# ONLY to us-east-1 (the provider's region — main.tf). It refreshes roughly
# every 6 hours, versus ~8-12 hours for budget evaluation, which is the entire
# reason this alarm exists alongside the budget: same boundary, sooner.
#
# period = 21600 (6h) matches the publish cadence. A shorter period would spend
# most of its time in INSUFFICIENT_DATA.
#
# treat_missing_data = "notBreaching" is load-bearing and is the opposite of the
# choice runner_ec2.tf's status-check alarm makes. There, missing data means the
# box is gone and should page. Here, missing data means AWS has not published a
# billing datapoint yet — routine at month rollover — and "breaching" would
# shut production down every 1st of the month.
#
# Because the metric resets at month rollover, the alarm returns to OK on its
# own and re-arms. Within a month it fires once: alarm actions run on state
# TRANSITION, so a bill that stays above the threshold does not re-invoke the
# Lambda every 6 hours.

resource "aws_cloudwatch_metric_alarm" "billing_estimated_charges_high" {
  alarm_name        = "${var.project_name}-billing-estimated-charges-high"
  alarm_description = "Month-to-date AWS charges exceeded USD ${var.cost_kill_switch_billing_threshold_usd}. Fast tripwire ahead of the ~8-12h budget evaluation; ~6h billing-metric lag still applies. See docs/runbooks/cost-kill-switch.md."

  namespace   = "AWS/Billing"
  metric_name = "EstimatedCharges"
  statistic   = "Maximum"
  dimensions  = { Currency = "USD" }

  comparison_operator = "GreaterThanThreshold"
  threshold           = var.cost_kill_switch_billing_threshold_usd
  period              = 21600
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"

  alarm_actions = var.cost_kill_switch_billing_alarm_arms_kill_switch ? [
    aws_sns_topic.alerts.arn,
    aws_sns_topic.cost_kill_switch.arn,
    ] : [
    aws_sns_topic.alerts.arn,
  ]
  ok_actions = [aws_sns_topic.alerts.arn]

  tags = { Project = var.project_name }
}

# ── Lambda: the kill switch itself ───────────────────────────────────────────

data "archive_file" "cost_kill_switch" {
  type        = "zip"
  source_file = "${path.module}/lambda/cost_kill_switch/index.py"
  output_path = "${path.module}/.build/cost-kill-switch.zip"
}

# Created explicitly rather than left to Lambda's implicit-on-first-invoke
# creation, so the execution role's logs:* statement can be pinned to this exact
# log group ARN instead of a wildcard.
resource "aws_cloudwatch_log_group" "cost_kill_switch" {
  name              = "/aws/lambda/${local.cost_kill_switch_name}"
  retention_in_days = 90 # matches every other log group in this stack
  tags              = { Project = var.project_name }
}

resource "aws_iam_role" "cost_kill_switch" {
  name = "${local.cost_kill_switch_name}-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = { Project = var.project_name }
}

# The permission surface, in full. Read it as the real specification of what the
# kill switch can do — the Python in lambda/cost_kill_switch/index.py can be
# rewritten, this cannot be exceeded.
#
# Deliberately NOT attached: AWSLambdaBasicExecutionRole (its logs:* is
# account-wide), and anything at all touching rds:, elasticache:, s3:,
# dynamodb:, backup: or kms:. There is no Delete*, Terminate*, Destroy* or
# Modify*-a-datastore action anywhere below, and
# backend/tests/test_cost_kill_switch_guards.py fails the build if one appears.
#
# ONE statement uses "Resource": "*" — AutoScalingPinToZero. Application Auto
# Scaling defines no IAM resource types at all, so RegisterScalableTarget and
# DescribeScalableTargets can only be written against "*"; there is no ARN to
# name and no documented condition key that reliably narrows it. Writing a
# speculative Condition here would be worse than the wildcard: an unsupported
# condition key is absent from the request context, the StringEquals fails
# closed, and the kill switch silently loses the one call that makes the rest of
# it stick. The wildcard is bounded instead by the two enumerated actions, and
# the guard test allows it for this Sid ONLY and asserts the action list is
# exactly those two.

resource "aws_iam_role_policy" "cost_kill_switch" {
  name = local.cost_kill_switch_name
  role = aws_iam_role.cost_kill_switch.id

  policy = <<-JSON
    {
      "Version": "2012-10-17",
      "Statement": [
        {
          "Sid": "AutoScalingPinToZero",
          "Effect": "Allow",
          "Action": [
            "application-autoscaling:DescribeScalableTargets",
            "application-autoscaling:RegisterScalableTarget"
          ],
          "Resource": "*"
        },
        {
          "Sid": "EcsScaleServiceToZero",
          "Effect": "Allow",
          "Action": [
            "ecs:DescribeServices",
            "ecs:UpdateService"
          ],
          "Resource": "arn:${data.aws_partition.current.partition}:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:service/${aws_ecs_cluster.main.name}/${aws_ecs_service.backend.name}"
        },
        {
          "Sid": "Ec2StopRunnerOnly",
          "Effect": "Allow",
          "Action": [
            "ec2:StopInstances"
          ],
          "Resource": "arn:${data.aws_partition.current.partition}:ec2:${var.aws_region}:${data.aws_caller_identity.current.account_id}:instance/${aws_instance.runner.id}"
        },
        {
          "Sid": "PublishWhatIDid",
          "Effect": "Allow",
          "Action": [
            "sns:Publish"
          ],
          "Resource": "${aws_sns_topic.alerts.arn}"
        },
        {
          "Sid": "OwnLogsOnly",
          "Effect": "Allow",
          "Action": [
            "logs:CreateLogStream",
            "logs:PutLogEvents"
          ],
          "Resource": "${aws_cloudwatch_log_group.cost_kill_switch.arn}:*"
        }
      ]
    }
  JSON
}

# NOTE ON CONCURRENCY: `reserved_concurrent_executions` is deliberately NOT set.
# This account's Lambda ConcurrentExecutions limit is 10 (checked 2026-08-31),
# and AWS refuses any reservation that would leave fewer than 100 unreserved —
# so setting it, however sensible it looks, makes `terraform apply` fail. The
# duplicate-invocation concern it would have addressed is handled instead by the
# handler being idempotent: the autoscaling and ECS steps read current state and
# skip the write; the EC2 step re-issues StopInstances and lets AWS no-op it
# (deliberate — reading first would force the EC2 IAM statement to "*"; see the
# IDEMPOTENT section of the Lambda docstring).

resource "aws_lambda_function" "cost_kill_switch" {
  function_name = local.cost_kill_switch_name
  description   = "Scales the backend to zero and stops the runner when spend crosses the kill threshold. Never touches a data store."
  role          = aws_iam_role.cost_kill_switch.arn
  handler       = "index.lambda_handler"
  runtime       = "python3.12"
  architectures = ["arm64"]

  filename         = data.archive_file.cost_kill_switch.output_path
  source_code_hash = data.archive_file.cost_kill_switch.output_base64sha256

  # 60s is generous: three API calls and an SNS publish. It exists so a single
  # throttled retry inside botocore cannot time the whole thing out.
  timeout     = 60
  memory_size = 256

  environment {
    variables = {
      ECS_CLUSTER                 = aws_ecs_cluster.main.name
      ECS_SERVICE                 = aws_ecs_service.backend.name
      SCALABLE_TARGET_RESOURCE_ID = aws_appautoscaling_target.backend.resource_id
      RUNNER_INSTANCE_ID          = aws_instance.runner.id
      NOTIFY_TOPIC_ARN            = aws_sns_topic.alerts.arn

      # Echoed verbatim into the recovery command in the SNS message, so the
      # operator does not have to look the steady-state numbers up mid-incident.
      RESTORE_MIN_CAPACITY  = tostring(var.ecs_service_min_count)
      RESTORE_MAX_CAPACITY  = tostring(var.ecs_service_max_count)
      RESTORE_DESIRED_COUNT = tostring(var.ecs_service_desired_count)

      # Pinned "false". A kill switch left in rehearsal mode is a kill switch
      # that does not exist, and nobody notices until the month it mattered —
      # backend/tests/test_cost_kill_switch_guards.py fails if this changes.
      COST_KILL_SWITCH_DRY_RUN = "false"
    }
  }

  depends_on = [aws_cloudwatch_log_group.cost_kill_switch]

  tags = { Project = var.project_name }
}

resource "aws_lambda_permission" "cost_kill_switch_sns" {
  statement_id  = "AllowExecutionFromCostKillSwitchTopic"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.cost_kill_switch.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.cost_kill_switch.arn
}

resource "aws_sns_topic_subscription" "cost_kill_switch_lambda" {
  topic_arn = aws_sns_topic.cost_kill_switch.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.cost_kill_switch.arn
}

# ── Outputs ──────────────────────────────────────────────────────────────────

output "cost_kill_switch_function_name" {
  description = "Lambda to invoke for a live rehearsal (see docs/runbooks/cost-kill-switch.md)."
  value       = aws_lambda_function.cost_kill_switch.function_name
}

output "cost_kill_switch_topic_arn" {
  description = "SNS topic whose only subscriber is the kill-switch Lambda. Publishing here fires it."
  value       = aws_sns_topic.cost_kill_switch.arn
}

output "cost_kill_switch_recovery_command" {
  description = "The one command that undoes a kill-switch fire. Also embedded in the SNS notification the Lambda publishes."
  value = join(" && ", [
    "aws application-autoscaling register-scalable-target --service-namespace ecs --resource-id ${aws_appautoscaling_target.backend.resource_id} --scalable-dimension ecs:service:DesiredCount --min-capacity ${var.ecs_service_min_count} --max-capacity ${var.ecs_service_max_count}",
    "aws ecs update-service --cluster ${aws_ecs_cluster.main.name} --service ${aws_ecs_service.backend.name} --desired-count ${var.ecs_service_desired_count}",
    "aws ec2 start-instances --instance-ids ${aws_instance.runner.id}",
  ])
}
