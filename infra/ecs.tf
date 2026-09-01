# ── ECS Fargate — the archimedes-backend service on the EXISTING ALB ─────────
#
# Issue #1039 (P2 foundation + P3 Fargate service). Runs the same two
# containers that today share one EC2 box via docker-compose.yml
# (nginx reverse-proxy + backend FastAPI), as ONE Fargate task: awsvpc network
# mode gives both containers the same ENI, so they talk over `localhost` exactly
# like two processes on one host — nginx on :8080 (the ALB target), backend on
# :8000 (nginx's upstream). The ALB, its listener, and its target group
# (`archimedes-backend-tg`, target_type=ip, health check GET /health) are
# ALL reused unchanged from alb.tf — Fargate registers each task's ENI IP
# directly on container_port=8080, which is exactly what an `ip`-type target
# group already expects. No alb.tf resource is touched.
#
# ══════════════════════════════════════════════════════════════════════════
# KNOWN GAP — MUST FIX BEFORE THIS SERVICE CAN ACTUALLY BOOT (not faked here):
# ══════════════════════════════════════════════════════════════════════════
# 1. nginx/nginx.conf's upstream is `server backend:8000 resolve;`, relying on
#    Docker Compose's per-container DNS name on its user-defined bridge
#    network. ECS Fargate awsvpc tasks have NO such inter-container DNS —
#    containers in the same task share one ENI and reach each other via
#    `localhost`/127.0.0.1, not by container name. Left as-is, nginx's
#    resolver (127.0.0.11, Docker's embedded DNS) doesn't exist in this
#    network mode and every request 502s. Fix is a one-line nginx.conf change
#    (`server localhost:8000 resolve;` or drop `resolve` entirely since
#    localhost never changes) — deliberately NOT made in this Terraform-only
#    chunk: nginx.conf is shared with docker-compose's bridge-network EC2 path
#    where `backend:8000` is still correct, so the fix needs either an
#    environment-conditional template or to land alongside the actual cutover
#    (when the EC2 path is retired and only one value is ever needed again).
# 2. `ARCHIMEDES_STRATEGIES_DIR`, `ARCHIMEDES_CORPUS_MANIFEST`, and
#    `KB_ARTIFACT_DIR` (env vars below) point at paths that today are
#    populated via docker-compose HOST BIND MOUNTS
#    (./analytics-engine/strategies, ./data/corpus, ./contracts/abis, the
#    archimedes-corpus-artifact named volume) from the EC2 box's git
#    checkout — see docker-compose.yml. Fargate has no host filesystem to
#    bind-mount from. `backend/Dockerfile`'s `COPY . /app` only copies the
#    `backend/` build context, so none of these repo-root sibling directories
#    are baked into the image today. Until backend/Dockerfile COPYs them in
#    (or an EFS volume is attached to this task), the backend container will
#    boot but fail to find strategies/corpus data at these paths.
# 3. RESOLVED (2026-07-08): `DATABASE_URL` / `REDIS_URL` (below, via ECS
#    `secrets`) resolve from SSM parameters `/archimedes/prod/DATABASE_URL`
#    and `/archimedes/prod/REDIS_URL` — both now exist, seeded the same way
#    AURORA_MASTER_PASSWORD / EMAIL_ENCRYPTION_KEY already were (via
#    `infra/scripts/seed-ssm-secrets.sh` — operator step, see
#    infra/runbooks/ecs-fargate-cutover.md). All eight secrets in the
#    `secrets` block below — DATABASE_URL, REDIS_URL,
#    AURORA_MASTER_PASSWORD, EMAIL_ENCRYPTION_KEY, the CIRCLE_API_KEY /
#    CIRCLE_ENTITY_SECRET / WALLET_ID trio added by #1463, and
#    TIINGO_API_TOKEN added by #1798 — are live in SSM
#    under /archimedes/prod/ and resolve without further action. Keep this
#    count and list in step with the block; the guard in
#    backend/tests/test_ecs_backend_secrets.py pins the membership.
# 4. `oracle_runner` / `agent_runner` / `kb_runner` (docker-compose services
#    `oracle`, `agent`, `kb-runner`) are NOT covered by this file. They are
#    singleton background daemons, not ALB-fronted request handlers, and
#    #1039's explicit scope for this chunk is "task definition for backend"
#    (singular, the ALB-fronted service). They need their own Fargate
#    service(s) (no load balancer, desired_count=1, no autoscaling — the
#    issue's own anti-goal for the EC2 ASG tier applies equally here: these
#    loops are the source of truth for vault/regime state and must not be
#    duplicated) before the EC2 instance can actually be decommissioned
#    (P6) — tracked as a residual for a later chunk, not silently dropped.
# ══════════════════════════════════════════════════════════════════════════
#
# APPLYING THIS FILE STARTS LIVE TRAFFIC CUTOVER: as soon as `aws_ecs_service`
# is created, ECS begins registering healthy Fargate task IPs into the SAME
# target group the EC2 instance is statically attached to (alb.tf's
# `aws_lb_target_group_attachment.backend`) — the ALB round-robins across
# BOTH immediately. See infra/runbooks/ecs-fargate-cutover.md before applying.

data "aws_caller_identity" "current" {}

# The existing target group, reused by NAME (not the `aws_lb_target_group.backend`
# resource reference in alb.tf, even though it lives in this same state/root
# module) — deliberately decoupled via a data source so this file reads as
# "attach to whatever `archimedes-backend-tg` already is" rather than
# depending on alb.tf's resource graph. It already exists live; reuse is exact.
data "aws_lb_target_group" "backend" {
  name = "${var.project_name}-backend-tg"
}

# ── ECS Cluster ────────────────────────────────────────────────────────────

resource "aws_ecs_cluster" "main" {
  name = "${var.project_name}-cluster"

  # Container Insights: per-service/per-task CPU/mem/network dashboards.
  # Modest extra CloudWatch cost for a single-service, ~0.07 req/s workload —
  # worth it since infra/cloudwatch.tf has no ECS-specific dashboard today.
  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  # DEFAULT = ECS Exec session I/O is logged via each container's own awslogs
  # configuration (see the two log groups below) — no separate log group or
  # KMS key needed to satisfy "aws ecs execute-command ... logs survive."
  configuration {
    execute_command_configuration {
      logging = "DEFAULT"
    }
  }

  tags = { Project = var.project_name }
}

# ── CloudWatch Logs — reuse the two log groups already provisioned ─────────
# infra/cloudwatch.tf (AUDIT I8) already created `/archimedes/app` and
# `/archimedes/nginx` at 90-day retention, pre-provisioned but currently
# unused (nothing ships to them — today's container logs are EC2-local,
# per #1039's own problem statement). They are exactly the right shape for
# the two ECS containers; reused here rather than creating a second,
# redundant pair of log groups. See aws_cloudwatch_log_group.app /
# aws_cloudwatch_log_group.nginx in cloudwatch.tf.

# ── Security group — ECS tasks (nginx ingress from ALB only) ───────────────
# backend is reached by nginx over localhost inside the same task (awsvpc
# shares one ENI across containers) — no SG rule needed for that hop at all.

resource "aws_security_group" "ecs_backend" {
  name        = "${var.project_name}-ecs-backend-sg"
  description = "ECS Fargate backend task (nginx + backend) - HTTP from ALB only"
  vpc_id      = aws_vpc.main.id

  # CIDR-restricted to the ALB's OWN public subnets (10.0.0.0/24,
  # 10.0.1.0/24 — aws_subnet.public in vpc.tf), NOT the whole VPC CIDR
  # (10.0.0.0/16, which also covers the private subnets Aurora/ElastiCache/
  # this same task sit in and is far wider than the ALB needs) and NOT a
  # `security_groups = [aws_security_group.alb.id]` reference: the ALB SG's
  # egress rule below already references THIS security group's id, and a
  # reciprocal reference the other way round is a dependency cycle Terraform
  # can't resolve ("Cycle: aws_security_group.ecs_backend,
  # aws_security_group.alb"). Referencing `aws_subnet.public[*].cidr_block`
  # directly (PR #1041 Copilot review) tightens the rule to exactly where the
  # ALB's ENIs live without introducing that cycle — subnet resources have no
  # dependency on either security group. Same one-directional
  # avoid-the-SG-cycle rationale main.tf's `aws_security_group.archimedes`
  # documents for its own CIDR-based rule.
  ingress {
    description = "HTTP from ALB (nginx container port), restricted to the ALB public subnet CIDRs"
    from_port   = 8080
    to_port     = 8080
    protocol    = "tcp"
    cidr_blocks = aws_subnet.public[*].cidr_block
  }

  egress {
    description = "All outbound (Aurora, ElastiCache, Arc RPC, Bedrock, ECR, CloudWatch Logs)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name    = "${var.project_name}-ecs-backend-sg"
    Project = var.project_name
  }
}

# alb.tf's aws_security_group.alb egress only allows the ALB to reach the EC2
# SG on port 80 (the pre-Fargate world). This is the one necessary, minimal
# addition needed for the ALB to reach the new Fargate targets on their
# actual container port (8080) — added as a SECOND inline egress block on the
# SAME resource declaration (see alb.tf), not a standalone aws_security_group_rule:
# aws_security_group with inline ingress/egress blocks is authoritative for ALL
# rules on that SG, and mixing it with standalone aws_security_group_rule
# resources causes Terraform to fight itself and delete "unmanaged" rules on
# every apply (the same footgun vpc.tf already documents for route tables).

# ── IAM — ECS task EXECUTION role (used by the ECS agent, not app code) ────
# Pulls images from ECR, ships container stdout/stderr to CloudWatch Logs, and
# resolves the `secrets` block below (SSM SecureString → env var) at launch.

resource "aws_iam_role" "ecs_task_execution" {
  name = "archimedes-ecs-task-execution-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = { Project = var.project_name }
}

# AWS-managed: ecr:GetAuthorizationToken/BatchGetImage/GetDownloadUrlForLayer/
# BatchCheckLayerAvailability + logs:CreateLogStream/PutLogEvents.
resource "aws_iam_role_policy_attachment" "ecs_task_execution_managed" {
  role       = aws_iam_role.ecs_task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Resolve the `secrets` block's SSM parameters (see task definition below) at
# container launch. Scoped identically to ec2_iam.tf's existing
# `ec2_ssm_params` policy for the same prefix. The Resource is a PREFIX
# WILDCARD, not an enumeration: adding a parameter under
# /archimedes/prod/ to the task definition's `secrets` needs no change here
# (that is why #1463's three additions below — CIRCLE_API_KEY,
# CIRCLE_ENTITY_SECRET, WALLET_ID — carry no IAM diff, and neither does
# #1798's TIINGO_API_TOKEN).
resource "aws_iam_role_policy" "ecs_task_execution_ssm_secrets" {
  name = "archimedes-ecs-execution-ssm-read"
  role = aws_iam_role.ecs_task_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadAppSecretsForTaskLaunch"
        Effect   = "Allow"
        Action   = ["ssm:GetParameters", "ssm:GetParameter"]
        Resource = "arn:aws:ssm:*:*:parameter/archimedes/prod/*"
      },
      {
        Sid      = "DecryptSecureStringViaSSM"
        Effect   = "Allow"
        Action   = "kms:Decrypt"
        Resource = "*"
        Condition = {
          StringEquals = { "kms:ViaService" = "ssm.${var.aws_region}.amazonaws.com" }
        }
      }
    ]
  })
}

# ── IAM — ECS task ROLE (used by application code inside the container) ────
# Mirrors ec2_iam.tf's EC2 instance role almost exactly — this is the same
# app code (services/secrets_service.load_ssm_secrets, the Bedrock LLM
# backends) running under ECS task credentials instead of EC2 instance
# credentials. Plus ssmmessages:* for `aws ecs execute-command` (the SSM
# Session Manager channel equivalent of the box's existing SSM access) and
# scoped CloudWatch Logs write for exec session output.

resource "aws_iam_role" "ecs_task" {
  name = "archimedes-ecs-task-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = { Project = var.project_name }
}

resource "aws_iam_role_policy" "ecs_task_ssm_params" {
  name = "archimedes-ecs-task-ssm-read"
  role = aws_iam_role.ecs_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadAppSecrets"
        Effect = "Allow"
        Action = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"]
        # BOTH ARNs are required. ssm:GetParametersByPath authorizes against the PATH
        # itself (".../parameter/archimedes/prod"), which the "/*" child pattern does
        # NOT match — granting only the child pattern yields:
        #   AccessDeniedException ... not authorized to perform: ssm:GetParametersByPath
        #   on resource: arn:aws:ssm:us-east-1:<acct>:parameter/archimedes/prod
        # The child ARN is still needed for GetParameter/GetParameters on individual keys.
        Resource = [
          "arn:aws:ssm:*:*:parameter/archimedes/prod",
          "arn:aws:ssm:*:*:parameter/archimedes/prod/*",
        ]
      },
      {
        Sid      = "DecryptSecureStringViaSSM"
        Effect   = "Allow"
        Action   = "kms:Decrypt"
        Resource = "*"
        Condition = {
          StringEquals = { "kms:ViaService" = "ssm.${var.aws_region}.amazonaws.com" }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy" "ecs_task_bedrock_invoke" {
  name = "archimedes-ecs-bedrock-invoke"
  role = aws_iam_role.ecs_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "InvokeBedrockFoundationModels"
        Effect = "Allow"
        Action = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        Resource = [
          "arn:aws:bedrock:*::foundation-model/*",
          "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/*"
        ]
      }
    ]
  })
}

# Verification email (auth container). Scoped to the one verified domain
# identity — the task can send AS this domain and nothing else. The identity
# itself (domain + DKIM CNAMEs in Route53) is managed outside terraform for
# now; the production-access (sandbox-exit) request is account-level.
resource "aws_iam_role_policy" "ecs_task_ses_send" {
  name = "archimedes-ecs-ses-send"
  role = aws_iam_role.ecs_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "SendVerificationEmailAsDomainIdentity"
        Effect = "Allow"
        Action = ["ses:SendEmail", "ses:SendRawEmail"]
        Resource = [
          "arn:aws:ses:${var.aws_region}:${data.aws_caller_identity.current.account_id}:identity/${var.domain_name}",
          # #1804: naming a configuration set on SendEmail requires permission
          # on the CONFIGURATION SET as well as on the identity — AWS evaluates
          # both resources for the one call. Without this entry every send from
          # the auth container starts failing AccessDeniedException the moment
          # SES_CONFIGURATION_SET is set, i.e. this line is what keeps turning
          # the bounce signal on from turning verification mail off.
          "arn:aws:ses:${var.aws_region}:${data.aws_caller_identity.current.account_id}:configuration-set/${aws_sesv2_configuration_set.mail.configuration_set_name}",
        ]
      },
      # #1748 item 2: GET /api/auth/verification-status asks SES whether the
      # caller's own address is on the account suppression list, because
      # SendEmail SUCCEEDS for a suppressed address (it returns a MessageId and
      # the message is then dropped) — so without this lookup the resend button
      # can only ever answer "requested", forever.
      #
      # Resource "*" is not laziness: the suppression list is an ACCOUNT-level
      # resource in SESv2 and has no per-identity ARN to scope to, unlike the
      # send statement above. Read-only, one action, and the only thing it can
      # reveal is whether an address AWS already refuses to mail is suppressed.
      # Without it the lookup fails AccessDeniedException, which auth/
      # suppression.js reports as "we could not look" — never as "not
      # suppressed" — so a missing grant degrades to an honest unknown rather
      # than a false all-clear.
      {
        Sid      = "ReadAccountSuppressionListForDeliveryFeedback"
        Effect   = "Allow"
        Action   = ["ses:GetSuppressedDestination"]
        Resource = "*"
      }
    ]
  })
}

# ECS Exec ("aws ecs execute-command" — the SSM-session equivalent for a
# Fargate task; no inbound port, no SSH). Required permissions per AWS docs:
# https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ecs-exec.html
resource "aws_iam_role_policy" "ecs_task_exec_command" {
  name = "archimedes-ecs-exec-command"
  role = aws_iam_role.ecs_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "EcsExecSsmChannel"
        Effect = "Allow"
        Action = [
          "ssmmessages:CreateControlChannel",
          "ssmmessages:CreateDataChannel",
          "ssmmessages:OpenControlChannel",
          "ssmmessages:OpenDataChannel"
        ]
        Resource = "*"
      },
      {
        # logs:DescribeLogGroups is a LIST action and does NOT support
        # resource-level permissions (see the CloudWatch Logs section of the
        # AWS IAM "Actions, resources, and condition keys" reference) — it
        # must be Resource = "*" or the grant is silently ineffective. Kept
        # in its own statement, separate from the log-STREAM actions below
        # which DO support (and are correctly scoped to) specific log-group
        # ARNs. (PR #1041 Copilot review.)
        Sid      = "EcsExecDescribeLogGroups"
        Effect   = "Allow"
        Action   = ["logs:DescribeLogGroups"]
        Resource = "*"
      },
      {
        Sid    = "EcsExecSessionLogging"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:DescribeLogStreams",
          "logs:PutLogEvents"
        ]
        Resource = [
          aws_cloudwatch_log_group.app.arn,
          "${aws_cloudwatch_log_group.app.arn}:*",
          aws_cloudwatch_log_group.nginx.arn,
          "${aws_cloudwatch_log_group.nginx.arn}:*"
        ]
      }
    ]
  })
}

# ── IAM — extend the EXISTING CI deploy role with ECS deploy permissions ───
# `archimedes-github-deploy` is deliberately NOT Terraform-managed (created
# out-of-band by infra/scripts/setup-github-oidc.sh — see that script's own
# "tighten once ECS exists" comment). That script, not Terraform, also owns the
# role's TRUST policy: after any org/repo rename or transfer it must be re-run
# (`--apply`) or every deploy fails at "Configure AWS credentials (OIDC)" —
# incident 2026-09-01, written up in that script's header.
#
# This attaches a SEPARATE, additively-named inline policy
# ("archimedes-ecs-deploy", distinct from the script's own "archimedes-deploy"
# policy) so Terraform and the script each own a disjoint policy name on the
# same role — no state fights, no clobbering.
#
# NOT wired into any CI workflow step yet (deploy.yml still only builds/pushes
# to ECR — the box-pull path from build-chunk 1). This is the IAM
# groundwork a later chunk's `aws ecs register-task-definition` +
# `aws ecs update-service --force-new-deployment` deploy step needs; landing
# it now means that later chunk needs zero IAM changes of its own.

data "aws_iam_role" "github_deploy" {
  name = "archimedes-github-deploy"
}

resource "aws_iam_role_policy" "github_deploy_ecs" {
  name = "archimedes-ecs-deploy"
  role = data.aws_iam_role.github_deploy.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "EcsDeploy"
        Effect = "Allow"
        Action = [
          "ecs:RegisterTaskDefinition",
          "ecs:DeregisterTaskDefinition",
          "ecs:DescribeTaskDefinition",
          "ecs:DescribeServices",
          "ecs:DescribeTasks",
          "ecs:ListTasks",
          "ecs:UpdateService",
          # RunTask/StopTask: the dedicated Alembic one-off migrate task
          # (infra/ecs_migrate.tf's `aws_ecs_task_definition.migrate`, #1039
          # P4, coupled to #1028 Phase A) runs this same way — pre-rollout,
          # before new tasks serve traffic. `Resource = "*"` already covers
          # that family (and the service family below); no separate grant
          # needed when the migrate task-def was split out from the service
          # one (PR #1041 Copilot review).
          "ecs:RunTask",
          "ecs:StopTask"
        ]
        Resource = "*"
      },
      {
        Sid    = "PassEcsRoles"
        Effect = "Allow"
        Action = "iam:PassRole"
        # Same two roles infra/ecs_migrate.tf's task definition reuses
        # (execution_role_arn / task_role_arn) — no additional PassRole grant
        # needed for the dedicated migrate task-def.
        Resource = [
          aws_iam_role.ecs_task_execution.arn,
          aws_iam_role.ecs_task.arn
        ]
        Condition = {
          StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" }
        }
      }
    ]
  })
}

# ── Task definition — nginx + backend, one Fargate task ────────────────────

resource "aws_ecs_task_definition" "backend" {
  family                   = "${var.project_name}-backend"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.ecs_backend_cpu
  memory                   = var.ecs_backend_memory
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  # ── EFS companion mount (issue #1065 decision #3 / infra/efs.tf) ─────────
  # Shared KB corpus-artifact storage — the backend's read side of the SAME
  # EFS access point the kb-runner scheduled task (infra/kb_runner.tf) writes
  # into, replacing docker-compose.yml's single named volume
  # (`archimedes-corpus-artifact`, mounted at this same
  # `/app/data/corpus-artifact` path there too). Adding this volume block +
  # the "backend" container's mountPoints entry below is the ONE additive,
  # in-place update this PR makes to an existing resource (new task-def
  # revision; aws_ecs_service.backend keeps `lifecycle.ignore_changes =
  # [task_definition]`, so this revision does NOT auto-roll the live
  # service — a human/CI still points the service at it explicitly, same as
  # any other deploy).
  volume {
    name = "corpus-artifact"

    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.corpus_artifact.id # efs.tf
      transit_encryption = "ENABLED"

      authorization_config {
        access_point_id = aws_efs_access_point.corpus_artifact.id # efs.tf
        iam             = "ENABLED"
      }
    }
  }

  container_definitions = jsonencode([
    {
      name      = "backend"
      image     = "${aws_ecr_repository.backend.repository_url}:${var.backend_image_tag}"
      essential = true

      portMappings = [
        { containerPort = 8000, protocol = "tcp" }
      ]

      # Backend/Dockerfile pre-creates + chowns this exact path to the
      # nonroot (uid 1001) user, matching the EFS access point's posix_user
      # (efs.tf) — the KB pipeline's read side (kb-runner writes the SAME
      # underlying files via infra/kb_runner.tf's mount at
      # /srv/corpus-artifact). KB_ARTIFACT_DIR (env, below) already points
      # here; only the volume was previously missing.
      mountPoints = [
        { sourceVolume = "corpus-artifact", containerPath = "/app/data/corpus-artifact", readOnly = false }
      ]

      # Mirrors backend/Dockerfile's own HEALTHCHECK exactly. Declaring it
      # here (not just relying on the image's Dockerfile HEALTHCHECK) is what
      # lets ECS track this container's health for nginx's `dependsOn
      # condition = HEALTHY` below — an image-only HEALTHCHECK isn't visible
      # to the ECS agent for container-dependency purposes.
      #
      # /health/ready, NOT /health (#1818 P3). This is the READINESS probe: it
      # answers 200 while the task can still read its database and 503 once the
      # DB-backed /health probes have been serving cached readings for longer
      # than HEALTH_STALE_UNREADY_S (default 900s). On 2026-09-03 two
      # paper-advance children wedged Postgres on a DDL lock chain and /health
      # went on answering 200 in ~1.05s from `stale_cached` values for TEN
      # HOURS — liveness was true, readiness was represented nowhere, and ECS
      # had nothing to act on until an OOM kill broke the wedge.
      #
      # Why here and not on the ALB target group: failing infra/alb.tf's check
      # pulls a target out of rotation IMMEDIATELY and a shared cause pulls all
      # of them at once (the incident's 13:29Z line: HealthyHostCount=0, 504s).
      # Failing THIS check hands the task to the ECS scheduler, which is bound
      # by `deployment_minimum_healthy_percent = 100` below — it brings a
      # replacement up healthy before draining the wedged one. So the target
      # group keeps polling /health (still unconditionally 200 — the N2 argument
      # in main.py's chain block) and only the container check acts on
      # staleness. 3 retries x 30s => ~90s of continuous 503 before ECS acts.
      #
      # THIS FILE IS NOT WHAT SHIPS IT. deploy.yml clones the LIVE revision and
      # .github/scripts/ecs_rewrite_task_def.py rewrites the clone; it does not
      # apply terraform. That is true TODAY, on its own: a healthCheck that
      # exists only here is not live until somebody applies, and a targeted
      # apply of this block is neither needed nor safe (it would carry the whole
      # accumulated container drift with it). #1799 (PR #1833, still OPEN as of
      # 2026-09-03) would add `lifecycle { ignore_changes =
      # [container_definitions] }` to this resource and make terraform stop
      # writing container settings altogether — that STRENGTHENS the argument
      # below but nothing here depends on it landing. The effective writer is
      # ecs_rewrite_task_def.READINESS_HEALTH_CHECK_COMMAND; the lines below are
      # the documented twin, kept in step by
      # backend/tests/test_ecs_readiness_deploy_pin.py.
      healthCheck = {
        command     = ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8000/health/ready')\" || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        # 90s: must cover request-path warmup (#1713, 60s budget) so ECS
        # ignores /health failures while uvicorn is not yet listening.
        # deploy.yml clones the live task-def and does not apply this file;
        # ecs_rewrite_task_def.py pins the same startPeriod on every CI
        # deploy so a clone of the 30s live revision cannot leave the
        # budget outside the grace window. Do not drop this below the
        # warmup budget. Do not terraform-noop deployment_minimum_healthy_percent.
        startPeriod = 90
      }

      environment = [
        { name = "AWS_REGION", value = var.aws_region },
        { name = "AWS_SSM_PATH_PREFIX", value = "/archimedes/prod/" },
        # SES bounce/complaint feedback queue (#1804, infra/ses_events.tf).
        # This copy is the OPERATOR path: the scheduled drain runs in its own
        # `archimedes-ses-events-drain` task definition (ses_events.tf), and
        # this variable is what makes `aws ecs execute-command` into a running
        # service task able to run the same drain by hand without anyone
        # pasting a queue URL. Nothing in the serving path reads it and no
        # always-on loop in this container consumes the queue, so its presence
        # costs the web tier nothing. The task role's queue grant is
        # aws_iam_role_policy.ecs_task_ses_events_queue, shared by both.
        { name = "SES_EVENTS_QUEUE_URL", value = aws_sqs_queue.ses_events.id },
        # PUBLIC_DOMAIN includes scheme because CORS and wallet-link URI/domain
        # bindings compare scheme-qualified origins. var.domain_name is bare host.
        { name = "PUBLIC_DOMAIN", value = "https://${var.domain_name}" },
        { name = "BETTER_AUTH_INTERNAL_URL", value = "http://127.0.0.1:3000" },
        { name = "APP_ENV", value = "production" },
        # Readiness threshold for the /health/ready container check above
        # (#1818 P3). Stated here at its code default rather than left implicit
        # so the knob is visible where the check that uses it is: seconds of
        # continuous `stale_cached` DB probes before this task reports 503 and
        # ECS replaces it.
        #
        # Twin of ecs_rewrite_task_def.HEALTH_STALE_UNREADY_VALUE, which is what
        # actually ships it (see the healthCheck note above, #1799). To pull the
        # rule back: "0" on the live revision disables it immediately and holds
        # until the next deploy; the DURABLE pull-back is "0" in BOTH places,
        # because the pipeline re-pins this name on every deploy.
        { name = "HEALTH_STALE_UNREADY_S", value = "900" },
        { name = "FEATURE_QUANT", value = "false" },
        # Runtime env-parity fix (PR #1041 correctness pass, 2026-07-07): the
        # prod EC2 box sets these three via docker-compose's `env_file: .env`
        # (docker-compose.yml's `backend` service) — a box-local, gitignored
        # file this ECS task definition has NO equivalent of. Verified missing
        # from both this `environment` block AND from SSM (`/archimedes/prod/`
        # today holds only AURORA_MASTER_PASSWORD + EMAIL_ENCRYPTION_KEY) —
        # left unset, `services/llm_backend.py` falls through to "no LLM
        # backend configured" (every LLM-backed route fails) and
        # `services/price_source.py` silently reverts to its `yfinance`-only
        # default instead of the live Pyth→yfinance→admin cascade. Not
        # secrets — same non-secret-config precedent as the ARC_* contract
        # addresses and PUBLIC_DOMAIN above — so hardcoded here rather than
        # routed through SSM. See infra/runbooks/ecs-fargate-cutover.md's
        # pre-cutover env-parity checklist for the verification command.
        { name = "LLM_PROVIDER", value = "bedrock_converse" },
        { name = "LLM_BEDROCK_MODEL", value = "amazon.nova-micro-v1:0" },
        { name = "PRICE_SOURCE", value = "cascade" },
        # Money switches — pinned explicitly (2026-08-16). Both were unset here,
        # in both compose files, and in setup-ssm-secrets.sh, so prod's values
        # were an accident of main.py:203-204's defaults rather than a decision.
        # An unset money switch is the bug regardless of which way it points.
        # These values change ONLY alongside the written mainnet scope decision
        # (payment-rail-only; PAYMENTS_DRY_RUN=false is scoped to the
        # caller-signed metered-API path, never the DCW browser subscribe flow,
        # which stays blocked on #975). Do not flip either one in a drive-by PR.
        { name = "PAYMENTS_DRY_RUN", value = "true" },
        # Generation-scoped dry-run split (2026-08-20, Dan's testnet-paywall
        # decision): the generation rail settles for real (caller-signed
        # metered-API path — exactly the scope the block above permits) while
        # the global stays dry, keeping marketplace sweeps/withdraws blocked
        # on #975. Reads in services/generation_payment.py; unset inherits
        # PAYMENTS_DRY_RUN.
        { name = "GENERATION_PAYMENTS_DRY_RUN", value = "false" },
        { name = "PAPER_TRADING", value = "true" },
        # ------------------------------------------------------------------
        # PAPER-ADVANCE TICK: ARMED (2026-09-01, #1778 — the #1632 lift).
        #
        # "true" means paper ledgers advance again: one appended bar per active
        # deployment per day, replayed on the graded engine. That is a live
        # product claim — a track record that moves with time — switched back
        # on, not a dormant knob. It was pulled on 2026-08-31 when web-tier
        # tasks were dying with "Fatal Python error: Aborted" and this tick was
        # the suspect.
        #
        # WHY IT IS DEFENSIBLE TO ARM, stated exactly. The tick runs ONLY in a
        # child interpreter: arm_paper_advance_for_web_tier (#1728) refuses to
        # schedule paper_advance_loop in the web process and spawns
        # `python -m archimedes.services.paper_trading` instead. A C-level
        # abort on the tick therefore kills that child, and /health — which
        # lives in the parent — keeps answering. The process boundary is the
        # whole protection.
        #
        # It is NOT a claim that the tick's own frame is clean. #1632's
        # faulthandler mechanism turned out to be two /health corpus-probe
        # threads racing in SQLAlchemy session teardown inside load_corpus,
        # caught on prod rev 214 WITH THIS FLAG OFF and fixed by #1740 (the
        # dual-OpenSSL hypothesis was falsified by the same traceback). The
        # replay's own OHLCV cache write in services/market_data_provider.py
        # was never the proven cause and was never cleared either.
        #
        # THIS FILE IS NOT THE PATH THAT SHIPS. deploy.yml clones the currently
        # registered task definition and retags images; it does not apply
        # terraform. The load-bearing pin is
        # .github/scripts/ecs_rewrite_task_def.py, invoked from deploy.yml.
        # This line is its documentation twin — keep the two equal so a future
        # terraform apply cannot silently disagree with what CI ships.
        #
        # TO PULL IT BACK (break glass — no terraform apply required):
        #   1. PAPER_ADVANCE_VALUE = "false" in
        #      .github/scripts/ecs_rewrite_task_def.py. That is the pin the
        #      next deploy actually registers.
        #   2. This line to "false", so the twin agrees.
        #   3. Deploy. Editing only this line is overwritten by the next CI
        #      deploy, and a terraform apply alone is overwritten the same way.
        # The cost while pulled, stated plainly: paper ledgers DO NOT ADVANCE.
        # Track records freeze and the deployment payload keeps serving its
        # last mark. That is a real product claim suspended, not a free win.
        #
        # Reader: advance_enabled() in services/paper_trading.py; row in
        # docs/operations/feature-flag-fliplist.md; guard in
        # backend/tests/services/test_paper_advance_kill_switch.py.
        # ------------------------------------------------------------------
        { name = "PAPER_ADVANCE_ENABLED", value = "true" },
        # Daily generation caps (services/generation_quota.py, #1194 rev a).
        # Plumbed here EXPLICITLY: a cap that silently falls back to its
        # code default because it was never added to the task definition is a
        # config-drift failure this file has already had (see KNOWN GAP list).
        # Not secrets. Both layers must pass; <= 0 disables a layer.
        { name = "GENERATION_DAILY_CAP_PER_USER", value = "100" },
        { name = "GENERATION_DAILY_CAP_PER_IP", value = "200" },
        # Admission control (#1668) — the same config-drift failure the caps
        # above were plumbed to avoid, found again by grep: all three are read
        # from the environment by shipped code (generate_routes.py's
        # `_max_concurrent_generations()` / `_max_queued_generations()` and
        # debate_engine.py's `_pool_max()`) and none of them were in this file,
        # so prod ran on the os.getenv() fallbacks by accident. Wired from
        # variables.tf, whose defaults are byte-identical to those fallbacks —
        # plumbing only, no behaviour change on apply. Retuning any of them is
        # a separate, separately-reviewed change (#1668 anti-goal).
        # backend/tests/test_admission_knobs_drift.py reads both sides and
        # fails if they ever diverge, in either direction.
        { name = "GENERATION_MAX_CONCURRENT", value = var.generation_max_concurrent },
        { name = "GENERATION_MAX_QUEUE", value = var.generation_max_queue },
        { name = "DEBATE_POOL_MAX", value = var.debate_pool_max },
        # Hard ceiling on one generation run. Read at
        # backend/archimedes/api/generate_routes.py:162, whose code default is
        # 600 s — and this name was NEVER in this task definition, so prod ran
        # that 600 s default by accident rather than by decision. Flagged by
        # the 2026-08-31 Javi re-grade, which reported a prod generation hang
        # of roughly 420 s: a ceiling looser than the hang it exists to bound
        # is not a ceiling. (The ~420 s figure is the re-grade's; it is not
        # reproduced anywhere in this repo — carried here as attribution.)
        #
        # 300 s is the deliberate tightening. Headroom is ample: a single
        # generation averages ~48 s (measured 2026-08-20, cited in the
        # admission-control note in generate_routes.py), so this is ~6x the
        # measured run. Past it, `_run_with_cleanup`'s `asyncio.wait_for`
        # ends the job `error` with an honest "generation exceeded the
        # 300-second limit", restores the payer's credit via
        # `_release_credit_if_undelivered`, and frees the generation-gate slot.
        #
        # Keep this a bare positive number. `_generation_timeout_seconds()`
        # is deliberately fail-soft — "300s", "5m", "0" or "-1" all fall back
        # to 600 silently — so a mistyped value reinstates exactly the drift
        # this line removes. Guarded by
        # backend/tests/test_ecs_generation_timeout.py.
        { name = "GENERATION_TIMEOUT_SECONDS", value = "300" },
        # Generation payment gate (flip-list #834): flag stays "false" until
        # Dan flips it deliberately — and GENERATION_PAYMENT_RECIPIENT (the
        # platform wallet that receives x402 settlements) MUST be set first;
        # flag-on with no recipient is a deliberate 503, never a free pass.
        # The recipient is a TF_VAR (same pattern as PLATFORM_ADMIN_WALLETS
        # below): supplied at apply time once the platform DCW exists, so the
        # flip needs no code change.
        { name = "GENERATION_PAYMENT_REQUIRED", value = "true" },
        # Free allowance ABOVE the paywall (#1643): the first N generations on
        # a VERIFIED account never reach GENERATION_PAYMENT_REQUIRED. Pinned
        # here at the code default (free_generations.DEFAULT_ALLOWANCE = 3) so
        # the number prod gives away is a decision someone applied rather than
        # an accident of a code default — finding A5 on the flip-list, and the
        # same drift GENERATION_DAILY_CAP_* and GENERATION_TIMEOUT_SECONDS
        # above were plumbed to remove. This is the one knob on that page that
        # gives away paid product, so it does not get to be implicit.
        #
        # TWIN, and the one that actually ships: FREE_GENERATIONS_VALUE in
        # .github/scripts/ecs_rewrite_task_def.py. deploy.yml clones the live
        # task definition and does not apply terraform, so this line alone is
        # not live. Change BOTH or the next CI deploy overwrites you.
        #
        # `<= 0` disables the free path entirely and restores the pre-#1643
        # wallet-gate-on-first-call behaviour; a non-integer falls back to 3
        # with a warning, so keep it a bare integer.
        # Reader: allowance() in services/free_generations.py. Guards:
        # backend/tests/test_ecs_backend_secrets.py (this line) and
        # backend/tests/test_ecs_free_generations_pin.py (both paths).
        { name = "FREE_GENERATIONS_PER_ACCOUNT", value = "3" },
        # $2.00/generation (Dan, 2026-08-20): the testnet faucet drips $20
        # per 2h cooldown, so one drip = a clean 10 generations — and $2 sits
        # inside the 10x-margin-over-measured-cost pricing direction (private
        # docs repo, business-model refresh §3a).
        { name = "GENERATION_PRICE_USD", value = "2.00" },
        { name = "GENERATION_PAYMENT_RECIPIENT", value = var.generation_payment_recipient },
        # The recipient DCW's Circle wallet id — an identifier (the API key +
        # entity secret in SSM are the credentials), needed so the revenue
        # sweep (services/revenue_sweep.py) can sign Gateway withdrawals.
        # Sweep stays OFF until REVENUE_SWEEP_ENABLED=true is added here.
        { name = "REVENUE_WALLET_ID", value = "af3e1cf6-76a3-55db-911a-b356860058e4" },
        # KNOWN GAP #2 (see file header): these three paths have no Fargate
        # equivalent of the docker-compose host bind mount yet.
        { name = "ARCHIMEDES_STRATEGIES_DIR", value = "/app/analytics-engine/strategies" },
        { name = "ARCHIMEDES_CORPUS_MANIFEST", value = "/app/data/corpus/manifest.jsonl" },
        { name = "KB_ARTIFACT_DIR", value = "/app/data/corpus-artifact" },
        # Backtest run artifacts (scripts/run_backtests.py). The packaged
        # analytics-engine/artifacts dir is NOT writable by the nonroot task
        # user, which killed every scheduled backtest refresh at the artifact
        # write and froze the leaderboard at its 2026-07-01 rows. Task-local
        # scratch is the deliberate choice: the durable record is the DB row's
        # artifact_json, not the file.
        { name = "ARCHIMEDES_ARTIFACT_DIR", value = "/tmp/archimedes-artifacts" },
        # Deployed Arc testnet contract addresses — T3.2 redeploy 2026-07-09
        # (deployer 0x03AaB3C91873f0Ec606c1Ed7528FE4CDCD6a4092, chain 5042002).
        # One authoritative set; converges the prior FE/BE split-brain. Not secrets.
        { name = "ARC_AMM_ROUTER_ADDRESS", value = "0x03df6c79f4e573ce793cdaa187719d1f15df24dc" },
        { name = "ARC_VAULT_FACTORY_ADDRESS", value = "0x404d18a906abbdf7bed5cf798c1dc5399c563987" },
        { name = "ARC_REASONING_TRACE_REGISTRY_ADDRESS", value = "0x9d81bfbfadb683cf77acda480e94e64088012847" },
        { name = "ARC_ASSET_REGISTRY_ADDRESS", value = "0x32c28231202626fbd8cf87caf42b01d973dc96c8" },
        { name = "ARC_SYNTHETIC_FACTORY_ADDRESS", value = "0x295b176aecc3be722827c49827794ee7ee707815" },
        # Marketplace contracts (publish path) — T3.2.
        { name = "ARC_STRATEGY_REGISTRY_ADDRESS", value = "0x283a2E42a06bb9BBA5e6613957C473D8AE7d4219" },
        { name = "ARC_PAYMENT_SPLITTER_ADDRESS", value = "0x69697D64e6ABD4dd7febc4dB7F017e9Cf4a9A1a7" },
        # Per-synth token/oracle addresses are NOT set here: the backend resolves all
        # 281 from the committed deploy-address SSOT baked into the image
        # (backend/archimedes/data/synthetic_addresses.json, regenerated per redeploy by
        # scripts/gen_synthetic_addresses.py). ARC_<SYMBOL>_ADDRESS env still overrides
        # any single synth if ever needed, but the whole set no longer needs pinning here.
        # Config consolidation (#1039 P5): public wallet addresses, sourced
        # from Terraform variables (TF_VAR_*, see variables.tf) rather than
        # hardcoded here or read off a box .env file. Empty defaults disable
        # the admin-wallet publish bypass / marketplace publish respectively
        # until Dan supplies real values.
        { name = "PLATFORM_ADMIN_WALLETS", value = var.platform_admin_wallets },
        # The account-keyed half of the admin gate (#1648). Empty is the safe
        # default: PLATFORM_ADMIN_WALLETS above keeps granting admin on its own
        # (as evidence), so an unset value changes nothing — it only forgoes the
        # database-independent break-glass path.
        { name = "PLATFORM_ADMIN_ACCOUNTS", value = var.platform_admin_accounts },
        { name = "ARCHIMEDES_TREASURY_WALLET", value = var.archimedes_treasury_wallet },
        # Arms the #1556 trace-visibility floor: ownerless trace rows are served
        # publicly ONLY for these house vaults; every other ownerless row goes
        # private. Unset, the floor is unarmed (every ownerless row is presumed
        # house content — WARNING-logged). Public on-chain addresses, not
        # secrets — same reasoning as PLATFORM_ADMIN_WALLETS above. Terraform
        # owns this so a bare apply can never silently revert it (the
        # infra/README PLATFORM_ADMIN_WALLETS lesson).
        { name = "PUBLIC_TRACE_VAULTS", value = var.public_trace_vaults }
      ]

      # KNOWN GAP #3 (see file header) — RESOLVED (2026-07-08): AURORA_MASTER_PASSWORD
      # and EMAIL_ENCRYPTION_KEY were already live in SSM
      # (infra/scripts/setup-ssm-secrets.sh, predating this chunk).
      # DATABASE_URL / REDIS_URL were seeded the same way via
      # infra/scripts/seed-ssm-secrets.sh (operator step, documented in
      # infra/runbooks/ecs-fargate-cutover.md) and now also exist live in SSM.
      # TIINGO_API_TOKEN was seeded by the owner on 2026-08-31 (SecureString,
      # same path convention) and is wired here by #1798.
      # Eight entries follow — DATABASE_URL, REDIS_URL, AURORA_MASTER_PASSWORD,
      # EMAIL_ENCRYPTION_KEY, CIRCLE_API_KEY, CIRCLE_ENTITY_SECRET, WALLET_ID,
      # TIINGO_API_TOKEN — and each resolves at task launch with no outstanding gap.
      secrets = [
        { name = "DATABASE_URL", valueFrom = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/archimedes/prod/DATABASE_URL" },
        { name = "REDIS_URL", valueFrom = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/archimedes/prod/REDIS_URL" },
        # Already live in SSM (setup-ssm-secrets.sh, predates #1039) — no
        # seeding step needed for these two, unlike DATABASE_URL/REDIS_URL
        # above. Resolved by the execution role at task launch, same as the
        # box's runtime `secrets_service.load_ssm_secrets()` fetch today, but
        # deterministic: the app process never starts without them, rather
        # than depending on a best-effort in-process SSM fetch.
        { name = "AURORA_MASTER_PASSWORD", valueFrom = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/archimedes/prod/AURORA_MASTER_PASSWORD" },
        { name = "EMAIL_ENCRYPTION_KEY", valueFrom = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/archimedes/prod/EMAIL_ENCRYPTION_KEY" },
        # Circle developer-controlled wallet credentials (#1463) — a TRIO, not
        # a pair. The backend reads all three off the process environment and
        # each consumer gates on its own subset:
        #   chain/circle_signer.py  is_configured = API_KEY and ENTITY_SECRET
        #                           and WALLET_ID; execute_contract raises
        #                           "Circle credentials not configured
        #                           (CIRCLE_API_KEY / CIRCLE_ENTITY_SECRET /
        #                           WALLET_ID)" if any one is blank
        #   chain/oracle_updater.py same trio, same message
        #   services/circle_service.py  API_KEY + WALLET_ID
        #   marketplace/wallet_provisioner.py  API_KEY + ENTITY_SECRET
        # So WALLET_ID is load-bearing, not decorative: shipping only the two
        # CIRCLE_* names leaves the signer and the oracle updater failing with
        # the exact error this change exists to remove. Without these three
        # entries the Fargate task starts fine and every Circle-signed path
        # (agent trade execution, oracle updates, wallet provisioning, the
        # revenue sweep) fails at call time instead of at boot — the fail-soft
        # shape CLAUDE.md § fail-soft calls out. All three parameters already
        # exist in SSM (/archimedes/prod/CIRCLE_API_KEY,
        # /archimedes/prod/CIRCLE_ENTITY_SECRET, /archimedes/prod/WALLET_ID —
        # SecureString, seeded 2026-07-09 per
        # infra/scripts/setup-ssm-secrets.sh), so no seeding step is needed and
        # the wildcard execution-role policy above (parameter/archimedes/prod/*)
        # already authorizes all three reads.
        { name = "CIRCLE_API_KEY", valueFrom = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/archimedes/prod/CIRCLE_API_KEY" },
        { name = "CIRCLE_ENTITY_SECRET", valueFrom = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/archimedes/prod/CIRCLE_ENTITY_SECRET" },
        { name = "WALLET_ID", valueFrom = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/archimedes/prod/WALLET_ID" },
        # Tiingo market-data credential (#1798). The parameter has existed as a
        # SecureString since 2026-08-31. It already reaches the web-tier app
        # process, but only by accident of the boot-time bulk load:
        # `main.py:47-48` calls `secrets_service.load_ssm_secrets()` when
        # PUBLIC_DOMAIN is set, which GetParametersByPath's the whole
        # /archimedes/prod/ prefix (AWS_SSM_PATH_PREFIX, in the `environment`
        # block above) into os.environ. That loader catches every error and
        # boots degraded — a credential that arrives only through it is a
        # SOFT dependency nothing verifies. This entry makes it a task-launch
        # dependency instead, which is what the runbook's execute-command
        # check can actually observe.
        # `services/market_data_provider._tiingo_api_key()` reads
        # TIINGO_API_TOKEN off the process environment (legacy alias
        # TIINGO_API_KEY second) and raises TiingoAPIKeyMissingError at provider
        # construction when both are blank — it does NOT fall back to yfinance,
        # by design (docs/adr/market-data-sourcing.md § "Never mix vendors
        # inside one run"). The flip itself is deliberately NOT in this change — it
        # is the owner's proof step, and it is guarded (MARKET_DATA_PROVIDER is
        # absent from the `environment` block above on purpose, pinned by
        # backend/tests/test_ecs_backend_secrets.py's FORBIDDEN_WHY).
        #
        # No IAM diff, same reason #1463's trio carried none: the execution
        # role's SSM statement above is a PREFIX WILDCARD over
        # parameter/archimedes/prod/*, which already authorises this read, and
        # its kms:Decrypt statement already covers every SecureString fetched
        # via ssm.<region>.amazonaws.com. That property is itself pinned by
        # test_execution_role_policy_is_a_prefix_wildcard — if someone narrows
        # the policy to an enumeration, that guard fails (its message enumerates
        # REQUIRED_CIRCLE_SECRETS, not this entry). The guard that names THIS
        # secret is test_every_secret_sits_under_the_execution_roles_prefix,
        # which fires if this ARN ever moves outside parameter/archimedes/prod/.
        #
        # This entry is only half the wiring. deploy.yml does not terraform
        # apply: it CLONES the live revision and retags images (#1799 — the two
        # sources of truth). The clone path pins the same secret in
        # .github/scripts/ecs_rewrite_task_def.py, so a deploy that lands
        # before an apply still carries the token.
        { name = "TIINGO_API_TOKEN", valueFrom = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/archimedes/prod/TIINGO_API_TOKEN" }
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.app.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "ecs"
        }
      }
    },
    {
      name      = "auth"
      image     = "${aws_ecr_repository.auth.repository_url}:${var.backend_image_tag}"
      essential = true

      portMappings = [
        { containerPort = 3000, protocol = "tcp" }
      ]

      healthCheck = {
        command     = ["CMD-SHELL", "wget -q -O /dev/null http://127.0.0.1:3000/health || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 15
      }

      environment = [
        { name = "NODE_ENV", value = "production" },
        { name = "BETTER_AUTH_URL", value = "https://${var.domain_name}" },
        { name = "BETTER_AUTH_TRUSTED_ORIGINS", value = "https://${var.domain_name}" },
        # Email verification (SES). Mail is sent on every signup; sign-in
        # refusal for unverified accounts is gated separately below.
        { name = "EMAIL_MAILER", value = "ses" },
        { name = "EMAIL_SENDER", value = "no-reply@${var.domain_name}" },
        # #1804. Every send names this configuration set, which is the only
        # thing that makes SES publish a bounce/complaint event for it
        # (infra/ses_events.tf). auth/mailer.js treats a blank value as "send
        # without a configuration set", so the mailer keeps working unchanged
        # if this is ever removed — it just goes back to being deaf.
        # backend/tests/test_ses_event_wiring.py pins this value to the
        # configuration set's own name; they are one string in two files and
        # must not drift.
        { name = "SES_CONFIGURATION_SET", value = aws_sesv2_configuration_set.mail.configuration_set_name },
        # Flip to "true" when the SES production-access request clears
        # (account is in the SES sandbox until then — sandbox can only send
        # to individually-verified addresses, so enforcing now would lock
        # every new signup out). Enforcement is an env flip, not a deploy.
        { name = "EMAIL_VERIFICATION_ENFORCED", value = "false" }
      ]

      # Optional providers remain absent unless explicitly enabled after both
      # pair values are seeded. Missing optional SSM params therefore cannot
      # prevent default email/password task startup.
      secrets = concat(
        [
          { name = "DATABASE_URL", valueFrom = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/archimedes/prod/DATABASE_URL" },
          { name = "BETTER_AUTH_SECRET", valueFrom = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/archimedes/prod/BETTER_AUTH_SECRET" }
        ],
        var.google_oauth_enabled ? [
          { name = "GOOGLE_CLIENT_ID", valueFrom = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/archimedes/prod/GOOGLE_CLIENT_ID" },
          { name = "GOOGLE_CLIENT_SECRET", valueFrom = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/archimedes/prod/GOOGLE_CLIENT_SECRET" }
        ] : [],
        var.github_oauth_enabled ? [
          { name = "GITHUB_CLIENT_ID", valueFrom = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/archimedes/prod/GITHUB_CLIENT_ID" },
          { name = "GITHUB_CLIENT_SECRET", valueFrom = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/archimedes/prod/GITHUB_CLIENT_SECRET" }
        ] : []
      )

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.app.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "auth"
        }
      }
    },
    {
      name      = "nginx"
      image     = "${aws_ecr_repository.nginx.repository_url}:${var.backend_image_tag}"
      essential = true

      # Render the runtime-agnostic nginx template (nginx.conf's upstream block).
      # In Fargate awsvpc the nginx + backend containers share the task's network
      # namespace (localhost) and there is NO Docker embedded DNS at 127.0.0.11 —
      # so the backend is reached at 127.0.0.1:8000 with an empty resolver line
      # and no `resolve` param. (Compose/local set the Docker-DNS values instead;
      # see docker-compose.yml.) Fixes the awsvpc 502-on-/health that made the
      # ALB target-group health check fail — was formerly "KNOWN GAP #1".
      environment = [
        { name = "NGINX_ENVSUBST_FILTER", value = "^NGINX_" },
        { name = "NGINX_RESOLVER_LINE", value = "" },
        { name = "NGINX_BACKEND_UPSTREAM", value = "127.0.0.1:8000" },
        { name = "NGINX_AUTH_UPSTREAM", value = "127.0.0.1:3000" },
        { name = "NGINX_UPSTREAM_RESOLVE", value = "" },
      ]

      portMappings = [
        { containerPort = 8080, protocol = "tcp" } # the ALB target — matches archimedes-backend-tg's traffic-port health check
      ]

      # Issue #1309 — nginx is the ONE container actually registered as the
      # ALB target (the `load_balancer` block below points at it, not at
      # backend/auth), yet until this healthCheck it was the ONLY essential
      # container in this task WITHOUT one. Per AWS's documented deployment
      # semantics (ECS service-definition-parameters "For services that do
      # use a load balancer"), the rolling-deployment scheduler computes a
      # task's healthStatus from whichever essential containers declare a
      # Docker healthCheck — a container with none simply doesn't
      # participate. With nginx silent, ECS's task-level healthStatus was
      # driven ENTIRELY by backend + auth, and could read HEALTHY as soon as
      # those two containers' checks pass — regardless of whether nginx
      # itself (started only after `dependsOn` is satisfied, then still
      # needs envsubst-render + `nginx -t` + listen) is actually up yet.
      # `dependsOn: HEALTHY` below governs container START ORDER only; it
      # does not make nginx count toward the service's deployment
      # healthStatus. Adding this check closes that real gap as generic
      # deployment-health hygiene.
      #
      # NOT a fix for the observed incident (correction, adversarial review
      # 2026-08-20 — the original comment/PR framed this as closing the
      # mode-1 gap; the timeline below shows it cannot have): the old target
      # deregistered 9s after the new task started — long before backend's
      # own startPeriod (90s as of #1713; 30s at the time of the incident)
      # could produce a first passing check for ANY
      # container, let alone before this nginx check's own startPeriod=15
      # (itself gated behind backend+auth reaching HEALTHY, so its earliest
      # possible verdict lands closer to T+45s) could produce a verdict, let
      # alone before the ALB's healthy_threshold=2 × interval=30s could mark
      # the new target healthy. The T+9s deregistration was not gated on
      # container health at all, so this healthCheck cannot have caused or
      # prevented it — it ships as forward-looking hygiene per AWS's
      # documented semantics above, not as an explanation of the incident.
      # The live candidates for the T+9s timeline are unexamined from this
      # environment (no AWS credentials) — see the PR body: (a) drift
      # between the live service's applied `deploymentConfiguration` and
      # this file (not in the service's `lifecycle.ignore_changes`, so a
      # Terraform change here only takes effect on the next manual `apply`),
      # and (b) the old task already being unhealthy/OOM-killed (mode 2,
      # #1328/#1329/#1337) at 23:59:13 rather than drained by the deployment.
      #
      # Checks a container-LOCAL endpoint (nginx.conf's `/nginx-health`),
      # NOT the ALB's proxied `/health`: proxying to backend on every poll
      # would round-trip into FastAPI's `/health` handler (Arc RPC + DB +
      # Redis work, backend/archimedes/main.py) — the same endpoint named as
      # the OOM crash-loop cause (mode 2, #1328/#1329/#1337) — adding real
      # load for no extra signal, since backend's own container healthCheck
      # (above) and the ALB's target-group check (alb.tf) already poll that
      # path independently at their own cadence. `/nginx-health` confirms
      # nginx is listening with a validly-rendered config at zero backend
      # cost; it does not prove nginx can reach backend/auth — that's what
      # the proxied /health and the ALB's own check are for. wget mirrors
      # the "auth" container's healthCheck pattern above (both images ship
      # BusyBox wget; no curl/python dependency to add).
      healthCheck = {
        command     = ["CMD-SHELL", "wget -q -O /dev/null http://127.0.0.1:8080/nginx-health || exit 1"]
        interval    = 10
        timeout     = 5
        retries     = 3
        startPeriod = 15
      }

      dependsOn = [
        { containerName = "backend", condition = "HEALTHY" },
        { containerName = "auth", condition = "HEALTHY" }
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.nginx.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "ecs"
        }
      }
    }
  ])

  tags = { Project = var.project_name }

  # ── OWNERSHIP SPLIT: container_definitions is PIPELINE-OWNED (#1799) ─────
  #
  # Two registrars write revisions of the `archimedes-backend` family:
  #
  #   terraform (this resource)  — registers a revision on `terraform apply`,
  #                                from the jsonencode() above.
  #   .github/workflows/deploy.yml — registers a revision on EVERY merge to
  #                                main: it clones the family's LATEST
  #                                revision (`aws ecs describe-task-definition
  #                                --task-definition archimedes-backend`,
  #                                which resolves a bare family name to its
  #                                latest ACTIVE revision), rewrites it with
  #                                .github/scripts/ecs_rewrite_task_def.py,
  #                                and calls register-task-definition.
  #
  # Before this block, those two fought. Terraform's state held revision 213
  # (the last apply); the pipeline had walked the family to 233. Every
  # attribute of aws_ecs_task_definition is ForceNew, so any edit to the JSON
  # above — #1778's PAPER_ADVANCE_ENABLED flip was the one that surfaced this
  # — made a plain `terraform plan` say "must be replaced", and an UNTARGETED
  # apply would then register a fresh revision built from this file. The
  # service below does not roll onto it (`ignore_changes = [task_definition]`,
  # kept deliberately), so nothing breaks that minute. The damage lands on the
  # NEXT merge: the pipeline clones the family's latest, which is now
  # terraform's revision, so twenty revisions of pipeline-accumulated
  # container state — the commit-SHA image tags above all — are silently
  # rolled back into production by a deploy that looks completely normal.
  # #1799 is that report; 2026-09-03 the owner picked the pipeline as owner.
  #
  # WHY EXACTLY container_definitions AND NOTHING ELSE. Read
  # `rewrite_registered_task_definition` in ecs_rewrite_task_def.py: the only
  # keys it writes are inside `containerDefinitions` — the backend / nginx /
  # auth `image` tags, and the backend container's `PAPER_ADVANCE_ENABLED`
  # environment pin. Everything else in a registered revision (cpu, memory,
  # execution_role_arn, task_role_arn, runtime_platform, the corpus-artifact
  # volume, network_mode, requires_compatibilities) is copied through the
  # clone untouched, never authored by the pipeline, and so stays
  # TERRAFORM-owned: a change to any of them still shows up in `plan` and
  # still needs a deliberate apply. Adding them here would be strictly worse
  # than the bug — it would hide real drift on attributes nobody else writes.
  # `tags` is likewise NOT ignored: `describe-task-definition --query
  # taskDefinition` omits tags entirely, so the pipeline's revisions carry
  # none, while terraform refreshes the specific revision ARN it registered
  # (which has them) — no diff to suppress.
  #
  # WHAT THIS COSTS. Editing the JSON above no longer reaches production. The
  # env pins that actually ship live in ecs_rewrite_task_def.py; the block
  # above is the declared baseline a from-scratch rebuild would register as
  # revision 1, and the documentation of intent for everything else. To make a
  # deliberate change to the LIVE task definition, follow
  # docs/runbooks/terraform-apply-and-task-definition-ownership.md.
  lifecycle {
    ignore_changes = [container_definitions]
  }
}

# ── ECS Service — behind the existing ALB target group ─────────────────────

resource "aws_ecs_service" "backend" {
  name            = "${var.project_name}-backend"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.backend.arn
  desired_count   = var.ecs_service_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.private[*].id # same private subnets Aurora/ElastiCache SGs already trust (10.0.10.0/24, 10.0.11.0/24) — zero SG edits needed on those
    security_groups  = [aws_security_group.ecs_backend.id]
    assign_public_ip = false # outbound (ECR, Bedrock, Arc RPC) via the existing fck-nat instances (vpc.tf)
  }

  load_balancer {
    target_group_arn = data.aws_lb_target_group.backend.arn
    container_name   = "nginx"
    container_port   = 8080
  }

  # Grace period before the ALB's own unhealthy-target verdict can cause ECS
  # to kill a freshly-started task — gives the backend container's model-load
  # + DB-connect startup room before the health check clock counts against it.
  health_check_grace_period_seconds = 90

  # Rolling, zero-downtime deploys: 100% minimum keeps the full existing
  # fleet serving while up to another 100% of new tasks come up and pass
  # health checks, only then are old tasks drained — never below desired
  # capacity, matching the "zero ALB 5xx during rollout" acceptance criterion.
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  # Health-check-based auto-rollback: if the new deployment's tasks don't
  # reach a steady healthy state, ECS automatically rolls back to the last
  # known-good task definition revision — no human action required.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  # `aws ecs execute-command` — the ECS Exec shell-into-a-running-task
  # acceptance criterion. Needs the task role's ssmmessages:* above.
  enable_execute_command = true

  depends_on = [
    aws_iam_role_policy_attachment.ecs_task_execution_managed,
    aws_iam_role_policy.ecs_task_execution_ssm_secrets,
    aws_iam_role_policy.ecs_task_ssm_params,
    aws_iam_role_policy.ecs_task_bedrock_invoke,
    aws_iam_role_policy.ecs_task_exec_command,
  ]

  lifecycle {
    # Real deploys after bootstrap register a new task-definition revision
    # (commit-SHA image tag) and call `aws ecs update-service
    # --force-new-deployment` directly — NOT `terraform apply`. Ignoring
    # drift on these two fields means that out-of-band deploy path (and the
    # CPU-based autoscaling policy's own DesiredCount changes) is never
    # silently reverted by a later plan/apply, mirroring main.tf's existing
    # `ignore_changes = [ami, user_data]` on the EC2 instance for the same reason.
    ignore_changes = [task_definition, desired_count]
  }

  tags = { Project = var.project_name }
}

# ── Application Auto Scaling — CPU target tracking, min 1 / max N ──────────

resource "aws_appautoscaling_target" "backend" {
  max_capacity       = var.ecs_service_max_count
  min_capacity       = var.ecs_service_min_count
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.backend.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "backend_cpu" {
  name               = "${var.project_name}-backend-cpu-scaling"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.backend.resource_id
  scalable_dimension = aws_appautoscaling_target.backend.scalable_dimension
  service_namespace  = aws_appautoscaling_target.backend.service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value       = var.ecs_autoscale_cpu_target
    scale_in_cooldown  = 300 # slow scale-in, mirrors asg.tf's own "slow, to avoid flapping" rationale
    scale_out_cooldown = 60  # fast scale-out
  }
}
