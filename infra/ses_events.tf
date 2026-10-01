# ── SES event feedback — the PUSH half of the bounce signal (#1804) ─────────
#
# THE DEFECT THIS CLOSES. Nothing in this account subscribed to SES delivery
# feedback: no `aws_sesv2_configuration_set`, no event destination, no topic
# (`ses_inbound.tf` next door is INBOUND mail for privacy@, a different thing
# entirely). A hard bounce therefore reached us only as an entry AWS silently
# added to the account suppression list — visible to an operator running
# `archimedes.scripts.ses_suppression list` and to nobody else, ever. The
# product's own view of that account was "emailVerified = false", which is the
# same value it holds for someone who simply has not clicked the link yet, so
# a fake or dead address is indistinguishable from an impatient human and is
# locked out of the free tier (`services/free_generations.py`) with no
# explanation and no path forward.
#
# THE LOOP THIS BUILDS.
#
#   SES send (auth container, ConfigurationSetName = the set below)
#        │
#        ├─ BOUNCE / COMPLAINT / REJECT / DELIVERY event
#        ▼
#   aws_sesv2_configuration_set_event_destination.feedback
#        ▼
#   aws_sns_topic.ses_events            (fan-out point; SES publishes here)
#        ▼
#   aws_sqs_queue.ses_events            (DURABLE — 14-day retention; the queue
#        │                               is what makes the consumer allowed to
#        │                               be periodic rather than always-on)
#        ▼
#   python -m archimedes.scripts.ses_events drain
#        ▼
#   auth_users.emailBouncedAt / emailBounceKind
#        ▼
#   signup + self-service resend refuse the address, with a typed reason
#        (auth/auth.js hooks.before)
#
# WHY SNS→SQS AND NOT SES→SQS DIRECTLY. SESv2 event destinations cannot target
# SQS; the supported destinations are SNS, EventBridge, Kinesis Firehose,
# CloudWatch and Pinpoint. SNS alone is not durable enough to be the whole
# answer — an SNS topic with no healthy subscriber DROPS the notification, so
# a consumer that is down (or, as here, one that runs periodically rather than
# continuously) would lose every bounce that arrived while it was not
# listening. The queue is the buffer that makes those losses impossible: a
# bounce sits in it until something deletes it, and a message that fails five
# receives lands in the DLQ instead of spinning forever.
#
# THE INVOCATION IS HERE TOO, at the bottom of this file: a dedicated
# single-container Fargate task definition (`ses-events-drain`, the shape
# `ecs_migrate.tf` documents for a one-off) invoked by an EventBridge
# Scheduler schedule every 15 minutes by default. Without it every resource
# above is built, wired, and still deaf — bounces would pile up in SQS and be
# deleted by the retention with nobody having read them. The queue's 14-day
# retention is what makes a PERIODIC drain honest rather than lossy: a bounce
# that arrives between ticks is late, never lost. Two alarms
# (`ApproximateAgeOfOldestMessage` on the queue, `ApproximateNumberOfMessages
# Visible` on the DLQ) are what stop the schedule itself from becoming the
# next unwatched link. See `docs/runbooks/ses-bounce-signal.md`.
#
# WHAT IS STILL DELIBERATELY NOT HERE. No per-event audit row: the consumer
# stamps the user and drops the `MessageId` / SES sub-type it parsed. That
# needs a table keyed on MessageId and is why #1804 stays open.
#
# data.aws_caller_identity.current is declared in ecs.tf and reused below, the
# same way ses_inbound.tf reuses it.

# ── Configuration set ────────────────────────────────────────────────────────
#
# A configuration set is the ONLY way to get per-message event feedback out of
# SES, and it applies only to sends that name it. `auth/mailer.js` passes
# `ConfigurationSetName` from the `SES_CONFIGURATION_SET` environment variable
# (ecs.tf, auth container) — the name below and that env value must be the same
# string or events are published for nothing. That pairing is a guard, not a
# convention: backend/tests/test_ses_event_wiring.py reads both files and fails
# when they drift.
#
# The mailer treats an unset/blank variable as "send without a configuration
# set", i.e. exactly today's behaviour — so this file landing before the
# `terraform apply` that sets the variable degrades to the status quo rather
# than to a broken send path.
resource "aws_sesv2_configuration_set" "mail" {
  configuration_set_name = "${var.project_name}-mail"

  reputation_options {
    # Per-configuration-set bounce/complaint rate metrics in CloudWatch. Free,
    # and the only way to see the rate that governs whether AWS keeps letting
    # us send at all — the account-level number alone cannot tell transactional
    # verification mail apart from anything else we ever send.
    reputation_metrics_enabled = true
  }

  sending_options {
    # Explicit, not inherited: this is the switch AWS itself flips to pause a
    # set that is bouncing badly, so its value belongs in the code rather than
    # in whatever the console last did.
    sending_enabled = true
  }

  tags = { Project = var.project_name }
}

# ── SNS topic SES publishes events to ───────────────────────────────────────

resource "aws_sns_topic" "ses_events" {
  name = "${var.project_name}-ses-events"
  tags = { Project = var.project_name }
}

resource "aws_sns_topic_policy" "ses_events" {
  arn = aws_sns_topic.ses_events.arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowSESPublish"
        Effect    = "Allow"
        Principal = { Service = "ses.amazonaws.com" }
        Action    = "sns:Publish"
        Resource  = aws_sns_topic.ses_events.arn
        # Same scoping ses_inbound.tf's privacy topic carries: another SES
        # tenant in another account must not be able to publish into a topic
        # whose messages we treat as authoritative about our own users.
        Condition = {
          StringEquals = {
            "AWS:SourceAccount" = data.aws_caller_identity.current.account_id
          }
        }
      },
    ]
  })
}

resource "aws_sesv2_configuration_set_event_destination" "feedback" {
  configuration_set_name = aws_sesv2_configuration_set.mail.configuration_set_name
  event_destination_name = "${var.project_name}-ses-events-sns"

  event_destination {
    enabled = true

    # BOUNCE and COMPLAINT are the two that stamp a user (a permanent bounce
    # means the mailbox does not exist; a complaint means a human told their
    # provider to stop us). REJECT and DELIVERY are carried because they are
    # the control group: REJECT is SES refusing the message outright (virus,
    # blocked content) and DELIVERY is the success case, and without them in
    # the same stream there is no way to tell "no bounce arrived" from "no
    # events are flowing at all". The consumer records neither as a bounce —
    # see `scripts/ses_events.py`'s event table.
    #
    # Deliberately NOT OPEN/CLICK: those need SES to rewrite links and embed a
    # tracking pixel in transactional mail people are asked to trust. No
    # product question here is worth that.
    matching_event_types = ["BOUNCE", "COMPLAINT", "REJECT", "DELIVERY"]

    sns_destination {
      topic_arn = aws_sns_topic.ses_events.arn
    }
  }
}

# ── SQS — the durable buffer ────────────────────────────────────────────────

resource "aws_sqs_queue" "ses_events_dlq" {
  name = "${var.project_name}-ses-events-dlq"

  # Maximum SQS allows. A message here is a bounce the consumer could not
  # parse or could not write; the whole point is that it is still readable
  # days later when somebody notices.
  message_retention_seconds = 1209600

  sqs_managed_sse_enabled = true

  tags = { Project = var.project_name }
}

resource "aws_sqs_queue" "ses_events" {
  name = "${var.project_name}-ses-events"

  # 14 days. This number is what licenses a periodic consumer: a bounce that
  # arrives between drains is late, not lost.
  message_retention_seconds = 1209600

  # Longer than any plausible drain of a single batch (the consumer processes
  # at most a few hundred messages and writes one row each) so a message is
  # never handed to a second reader while the first is still working on it.
  visibility_timeout_seconds = 300

  sqs_managed_sse_enabled = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.ses_events_dlq.arn
    # Five attempts, then the DLQ. A message that fails repeatedly is a bug in
    # the parser or a schema change at AWS — it must stop being retried and
    # start being visible, rather than blocking the head of the queue forever.
    maxReceiveCount = 5
  })

  tags = { Project = var.project_name }
}

resource "aws_sqs_queue_policy" "ses_events" {
  queue_url = aws_sqs_queue.ses_events.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowSNSDeliveryFromSesEventsTopic"
        Effect    = "Allow"
        Principal = { Service = "sns.amazonaws.com" }
        Action    = "sqs:SendMessage"
        Resource  = aws_sqs_queue.ses_events.arn
        # Scoped to THIS topic, not to SNS as a service: without the condition
        # any SNS topic in any account could push messages the consumer would
        # treat as SES telling it a user's address is dead.
        Condition = {
          ArnEquals = { "aws:SourceArn" = aws_sns_topic.ses_events.arn }
        }
      },
    ]
  })
}

resource "aws_sns_topic_subscription" "ses_events_sqs" {
  topic_arn = aws_sns_topic.ses_events.arn
  protocol  = "sqs"
  endpoint  = aws_sqs_queue.ses_events.arn

  # Left at the default (false) on purpose: with the SNS envelope intact the
  # message carries its own TopicArn and Timestamp, which is what makes a
  # queue message self-describing when it lands in the DLQ. The consumer
  # unwraps the envelope and also accepts a bare SES event, so flipping this
  # later does not break it (backend/tests/test_ses_bounce_consumer.py covers
  # both shapes).
  raw_message_delivery = false
}

# ── IAM — the consumer's read/delete grant ──────────────────────────────────
#
# On the ECS *task* role (ecs.tf), which is the identity the backend image runs
# under wherever it runs: `aws ecs execute-command` into the running service
# task, a `run-task` override, or a future scheduled task all inherit it. No
# `sqs:SendMessage` — only SNS writes to this queue — and no access to any
# other queue.
resource "aws_iam_role_policy" "ecs_task_ses_events_queue" {
  name = "archimedes-ecs-ses-events-queue"
  role = aws_iam_role.ecs_task.id # ecs.tf

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "DrainSesEventQueue"
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
          "sqs:GetQueueUrl",
        ]
        Resource = aws_sqs_queue.ses_events.arn
      },
    ]
  })
}

# ── THE INVOCATION — a scheduled Fargate one-off, not a service ─────────────
#
# WHY THIS EXISTS AT ALL. Everything above is the push half; the queue it
# fills is durable and the consumer (`archimedes.scripts.ses_events drain`) is
# runnable. Neither fact stamps a single user. Without something that calls
# the consumer on a clock, bounces accumulate in SQS and are deleted by the
# 14-day retention with nobody having read them — the loop is built, wired,
# and still deaf, which is the same product state #1804 opened against, only
# with more terraform in it. The issue names the shape directly: "a small
# consumer (scheduled ECS task or the auth service polling)".
#
# WHY A DEDICATED TASK DEFINITION AND NOT A COMMAND OVERRIDE ON THE SERVICE
# FAMILY. `aws_ecs_task_definition.backend` (ecs.tf) defines THREE containers
# — backend, auth, and an nginx sidecar with `dependsOn { containerName =
# "backend", condition = "HEALTHY" }`. Running a one-off against that family
# boots all three just to read a queue, and worse: with the backend
# container's command overridden to the drain it never serves HTTP, its
# `healthCheck` never turns HEALTHY, nginx's `dependsOn` is never satisfied,
# and ECS is liable to kill the task before the drain finishes. That is the
# same reasoning — and the same conclusion — as `infra/ecs_migrate.tf`, whose
# single-container shape this copies.
#
# WHY A SCHEDULE AND NOT AN SQS EVENT SOURCE. Fargate has no native SQS
# trigger; the poll-driven options are a Lambda (a second runtime, a second
# image, a second dependency set for one file that already runs in the backend
# image) or a long-lived poller (a new always-on compute unit and a new
# singleton to reason about). A batch drain every few minutes costs one short
# task per invocation and is exactly what a 14-day-retention queue is for:
# latency here is measured against a human eventually retrying a signup, not
# against a trade.

resource "aws_ecs_task_definition" "ses_events_drain" {
  family                   = "${var.project_name}-ses-events-drain"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.ses_events_drain_cpu
  memory                   = var.ses_events_drain_memory

  # Both reused from ecs.tf. The TASK role is the one that matters here: it
  # already carries the queue grant added above
  # (aws_iam_role_policy.ecs_task_ses_events_queue), so this task needs no new
  # IAM of its own. The EXECUTION role is what resolves the SSM secrets below
  # and writes the log stream.
  execution_role_arn = aws_iam_role.ecs_task_execution.arn
  task_role_arn      = aws_iam_role.ecs_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([
    {
      name      = "ses-events-drain"
      image     = "${aws_ecr_repository.backend.repository_url}:${var.backend_image_tag}"
      essential = true

      # Run-to-completion: no portMappings, no healthCheck, no dependsOn. The
      # drain reads at most --max-messages, writes its stamps, deletes what it
      # processed and exits (0 success, 2 configuration/AWS failure — and a
      # failed AWS call is deliberately NOT rendered as "the queue was empty";
      # see the module docstring).
      command = ["python", "-m", "archimedes.scripts.ses_events", "drain"]

      environment = [
        { name = "AWS_REGION", value = var.aws_region },
        { name = "AWS_SSM_PATH_PREFIX", value = "/archimedes/prod/" },
        # The one variable the drain actually requires: without it the command
        # exits 2 rather than silently reporting an empty queue. Taken off the
        # queue resource itself, never a literal — the same one-name-one-place
        # rule the configuration set follows, pinned by
        # backend/tests/test_ses_event_wiring.py.
        { name = "SES_EVENTS_QUEUE_URL", value = aws_sqs_queue.ses_events.id },
      ]

      # DATABASE_URL is what the stamp is written through (`archimedes.db`).
      # The other three mirror ecs_migrate.tf's set exactly, for the same
      # stated reason: the task is then self-sufficient against this image
      # without depending on which import path the entrypoint happens to
      # exercise, and there is one answer — not a second, drifting one — to
      # "what secrets does this image need to boot".
      secrets = [
        { name = "DATABASE_URL", valueFrom = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/archimedes/prod/DATABASE_URL" },
        { name = "REDIS_URL", valueFrom = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/archimedes/prod/REDIS_URL" },
        { name = "AURORA_MASTER_PASSWORD", valueFrom = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/archimedes/prod/AURORA_MASTER_PASSWORD" },
        { name = "EMAIL_ENCRYPTION_KEY", valueFrom = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/archimedes/prod/EMAIL_ENCRYPTION_KEY" }
      ]

      # Shared /archimedes/app group with its own stream prefix, exactly as
      # ecs_migrate.tf does — a drain run is findable without provisioning
      # another log group.
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.app.name # cloudwatch.tf
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "ses-events-drain"
        }
      }
    }
  ])

  tags = { Project = var.project_name }
}

# ── IAM — EventBridge Scheduler's own role (assumes into ecs:RunTask) ───────
# Same shape as aws_iam_role.kb_scheduler (kb_runner.tf), scoped to this one
# task family and this one cluster.

resource "aws_iam_role" "ses_events_scheduler" {
  name = "archimedes-ses-events-scheduler-role"
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

resource "aws_iam_role_policy" "ses_events_scheduler_run_task" {
  name = "archimedes-ses-events-scheduler-run-task"
  role = aws_iam_role.ses_events_scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "RunSesEventsDrainTask"
        Effect = "Allow"
        Action = ["ecs:RunTask"]
        # Family-wildcard (revision-less) ARN, matching the schedule target
        # below — the grant stays valid across every future revision without a
        # terraform change.
        Resource = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task-definition/${aws_ecs_task_definition.ses_events_drain.family}:*"
        Condition = {
          ArnLike = { "ecs:cluster" = aws_ecs_cluster.main.arn } # ecs.tf
        }
      },
      {
        Sid      = "PassSesEventsDrainRoles"
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
# `task_definition_arn` is the REVISION-LESS family ARN on purpose (same
# reasoning, and the same verify-at-apply-time caveat, as
# aws_scheduler_schedule.kb_runner): it resolves to the latest ACTIVE revision
# at invocation, so a CI-registered revision is picked up with no schedule
# update. Pinning terraform's own `.arn` would freeze the schedule on whatever
# revision this apply happens to create.
#
# COST, stated rather than waved at: at the default rate(15 minutes) that is
# 96 invocations/day. Fargate bills a 1-minute minimum, so the floor is
# 96 min/day ≈ 48 h/month at 0.25 vCPU + 1 GB
# (48 × (0.25 × $0.04048 + 1 × $0.004445)) ≈ **$0.70/month** in us-east-1
# on-demand pricing, plus SQS requests well inside the free tier. The interval
# is a variable precisely so this is the owner's dial, not a constant.
#
# NOT `flexible_time_window`: a drain that runs a few minutes late is fine,
# but OFF keeps the invocation times predictable when reading the log group.

resource "aws_scheduler_schedule" "ses_events_drain" {
  name                = "${var.project_name}-ses-events-drain"
  schedule_expression = var.ses_events_drain_schedule_expression
  state               = "ENABLED"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_ecs_cluster.main.arn # ecs.tf
    role_arn = aws_iam_role.ses_events_scheduler.arn

    ecs_parameters {
      task_definition_arn = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task-definition/${aws_ecs_task_definition.ses_events_drain.family}"
      launch_type         = "FARGATE"

      network_configuration {
        # Private subnets + the backend security group: the same static pair
        # ecs_migrate.tf documents for its own one-off (egress to Aurora, SSM,
        # ECR, CloudWatch and the SQS endpoint; no ingress is needed by a task
        # that never accepts a connection).
        subnets          = aws_subnet.private[*].id # vpc.tf
        security_groups  = [aws_security_group.ecs_backend.id]
        assign_public_ip = false
      }
    }

    retry_policy {
      # One retry, then wait for the next tick. Nothing is lost by giving up:
      # an undeleted message stays visible in the queue for 14 days, so the
      # cost of a skipped invocation is latency, not data.
      maximum_retry_attempts = 1
    }
  }
}

# ── The two alarms that make a DEAF loop visible ────────────────────────────
#
# The whole failure class #1804 is about is silence that looks like health, so
# the schedule above must not be the last unwatched link. Both alarms are on
# queue metrics SQS publishes for free — no metric filter, no log parsing, no
# second alerting path — and both route to cloudwatch.tf's existing topic.

resource "aws_cloudwatch_metric_alarm" "ses_events_not_being_drained" {
  alarm_name        = "archimedes-ses-events-not-drained"
  alarm_description = "The oldest message in the SES bounce/complaint queue has been waiting over an hour: the scheduled drain (infra/ses_events.tf) is not running, is failing, or has lost its queue grant. Bounces are still safe (14-day retention) but nothing is being stamped, so a dead address once again looks exactly like an impatient human."

  namespace   = "AWS/SQS"
  metric_name = "ApproximateAgeOfOldestMessage"
  dimensions  = { QueueName = aws_sqs_queue.ses_events.name }
  statistic   = "Maximum"

  comparison_operator = "GreaterThanThreshold"
  threshold           = 3600 # seconds; four missed 15-minute ticks, with room
  period              = 300
  evaluation_periods  = 2

  # An EMPTY queue publishes no ApproximateAgeOfOldestMessage datapoints at
  # all, and an empty queue is the healthy steady state here — treating
  # missing data as breaching would make this alarm scream during every quiet
  # week, which is how alarms get muted.
  treat_missing_data = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn] # cloudwatch.tf
  ok_actions    = [aws_sns_topic.alerts.arn]
  tags          = { Project = var.project_name }
}

resource "aws_cloudwatch_metric_alarm" "ses_events_dlq_not_empty" {
  alarm_name        = "archimedes-ses-events-dlq-not-empty"
  alarm_description = "A message failed five drain attempts and landed in the SES events dead-letter queue — almost always the consumer's parser behind a change to the SES event schema. `aws sqs receive-message --queue-url $(terraform output -raw ses_events_dlq_url)` to read it; see docs/runbooks/ses-bounce-signal.md."

  namespace   = "AWS/SQS"
  metric_name = "ApproximateNumberOfMessagesVisible"
  dimensions  = { QueueName = aws_sqs_queue.ses_events_dlq.name }
  statistic   = "Maximum"

  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  period              = 300
  evaluation_periods  = 1

  treat_missing_data = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn] # cloudwatch.tf
  ok_actions    = [aws_sns_topic.alerts.arn]
  tags          = { Project = var.project_name }
}
