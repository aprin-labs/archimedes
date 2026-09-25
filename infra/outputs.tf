# instance_id / public_ip / public_dns / ssh_command / api_url were removed
# 2026-08-19 with aws_instance.archimedes (main.tf) — the single-EC2 Docker
# host decommission. See main.tf's decommission note for the full rationale.

output "private_key_path" {
  description = "Path to the SSH private key"
  value       = local_sensitive_file.private_key.filename
}

output "ssh_private_key" {
  description = "SSH private key (for GitHub Actions secret)"
  value       = tls_private_key.deploy.private_key_openssh
  sensitive   = true
}

# ── New VPC infrastructure outputs ────────────────────────────

output "vpc_id" {
  description = "New VPC ID"
  value       = aws_vpc.main.id
}

output "aurora_endpoint" {
  description = "Aurora cluster endpoint (for DATABASE_URL)"
  value       = aws_rds_cluster.main.endpoint
}

output "aurora_reader_endpoint" {
  description = "Aurora reader endpoint"
  value       = aws_rds_cluster.main.reader_endpoint
}

output "redis_endpoint" {
  description = "ElastiCache Redis endpoint (for REDIS_URL)"
  value       = aws_elasticache_replication_group.main.primary_endpoint_address
}

output "database_url" {
  description = <<-DESC
    Full DATABASE_URL for backend .env. STATE-SENSITIVE: this output stores
    the master password in Terraform state (which lives in the S3 backend).
    The bucket policy restricts access to the AWS account principal and TLS-only,
    but the password is still in the state file. Recommended pattern going forward:
    backend fetches the password from AWS Secrets Manager / SSM Parameter Store at
    runtime and constructs the URL from `aurora_endpoint` + password — that way
    the secret never lands in Terraform state at all. Tracked as a follow-up.
  DESC
  value       = "postgresql://archimedes:${var.aurora_master_password}@${aws_rds_cluster.main.endpoint}:5432/archimedes"
  sensitive   = true
}

output "redis_url" {
  description = "Full REDIS_URL for backend .env"
  value       = "rediss://${aws_elasticache_replication_group.main.primary_endpoint_address}:6379/0"
}

output "alb_dns_name" {
  description = "ALB DNS name (for Route 53 ALIAS record)"
  value       = aws_lb.main.dns_name
}

output "alb_zone_id" {
  description = "ALB hosted zone ID (for Route 53 ALIAS record)"
  value       = aws_lb.main.zone_id
}

output "acm_certificate_arn" {
  description = "ACM certificate ARN"
  value       = aws_acm_certificate.main.arn
}

output "waf_web_acl_arn" {
  description = "WAF Web ACL ARN"
  value       = aws_wafv2_web_acl.main.arn
}

# ── CloudFront + ASG (virality tier, issue #155) ──────────────

output "cloudfront_domain_name" {
  description = "CloudFront distribution domain (the *.cloudfront.net name behind archimedes-arc.app)"
  value       = aws_cloudfront_distribution.main.domain_name
}

output "cloudfront_distribution_id" {
  description = "CloudFront distribution id (for cache invalidations)"
  value       = aws_cloudfront_distribution.main.id
}

output "backend_asg_name" {
  description = "Backend auto-scaling group name (null unless the optional ASG tier is enabled via backend_ami_id)"
  value       = one(aws_autoscaling_group.backend[*].name)
}

# ── ECS Fargate outputs (issue #1039) ─────────────────────────────────────

output "ecr_backend_repository_url" {
  description = "ECR repository URL for the archimedes-backend image (backend + oracle + agent + kb-runner)"
  value       = aws_ecr_repository.backend.repository_url
}

output "ecr_nginx_repository_url" {
  description = "ECR repository URL for the archimedes-nginx image"
  value       = aws_ecr_repository.nginx.repository_url
}

output "ecs_cluster_name" {
  description = "ECS cluster name"
  value       = aws_ecs_cluster.main.name
}

output "ecs_cluster_arn" {
  description = "ECS cluster ARN"
  value       = aws_ecs_cluster.main.arn
}

output "ecs_service_name" {
  description = "ECS service name (behind the existing archimedes-backend-tg target group)"
  value       = aws_ecs_service.backend.name
}

output "ecs_task_definition_family" {
  description = "ECS task definition family (register new revisions against this family for CI deploys)"
  value       = aws_ecs_task_definition.backend.family
}

output "ecs_task_execution_role_arn" {
  description = "ECS task execution role ARN (image pull, log shipping, secrets resolution)"
  value       = aws_iam_role.ecs_task_execution.arn
}

output "ecs_task_role_arn" {
  description = "ECS task role ARN (application runtime permissions — SSM read, Bedrock invoke, ECS Exec channel)"
  value       = aws_iam_role.ecs_task.arn
}

output "ecs_exec_shell_command" {
  description = "Template for shelling into a running backend task via ECS Exec (fill in the task id from `aws ecs list-tasks --cluster <ecs_cluster_name> --service-name <ecs_service_name>`)"
  value       = "aws ecs execute-command --cluster ${aws_ecs_cluster.main.name} --task <task-id> --container backend --interactive --command \"/bin/sh\""
}

output "ecs_migrate_task_definition_family" {
  description = "ECS task definition family for the one-off Alembic migrate task (infra/ecs_migrate.tf) — distinct from ecs_task_definition_family (the SERVICE family, backend+nginx). .github/workflows/deploy.yml's migrate job's ECS_MIGRATE_TASK_FAMILY literal must match this value."
  value       = aws_ecs_task_definition.migrate.family
}

# Static awsvpcConfiguration for `aws ecs run-task --network-configuration`
# against the migrate task family (issue #1039 B2). Sourced from aws_subnet.private
# (vpc.tf) + aws_security_group.ecs_backend (ecs.tf) — BOTH exist independently of
# aws_ecs_service.backend, unlike the `aws ecs describe-services archimedes-backend`
# call deploy.yml's migrate job previously used, which by definition can't resolve
# until the service (which the migrate task must run BEFORE) already exists. Run
# `terraform output -raw ecs_migrate_network_configuration` after applying
# aws_subnet.private + aws_security_group.ecs_backend and paste the result into
# .github/workflows/deploy.yml's ECS_MIGRATE_NETWORK_CONFIGURATION literal — same
# "CI can't run terraform output" literal-constant pattern as ECS_CLUSTER /
# ECS_MIGRATE_TASK_FAMILY there.
output "ecs_migrate_network_configuration" {
  description = "Static awsvpcConfiguration JSON for `aws ecs run-task --network-configuration` against the migrate task family. Copy into .github/workflows/deploy.yml's ECS_MIGRATE_NETWORK_CONFIGURATION literal (issue #1039 B2)."
  value = jsonencode({
    awsvpcConfiguration = {
      subnets        = aws_subnet.private[*].id
      securityGroups = [aws_security_group.ecs_backend.id]
      assignPublicIp = "DISABLED"
    }
  })
}

# ── Runner relocation outputs (issue #1065 / #1043) ────────────────────────

output "runner_instance_id" {
  description = "Instance id of the dedicated oracle+agent runner EC2 (infra/runner_ec2.tf) — target for `aws ssm start-session` / `aws ssm send-command`, and for #1065 Step 4's Phase 8 gate check."
  value       = aws_instance.runner.id
}

output "runner_log_group_name" {
  description = "CloudWatch log group both runner systemd units ship to (docker awslogs driver, distinct stream prefixes 'oracle'/'agent') — `aws logs tail <this> --since 10m --filter-pattern oracle` per #1065 Step 3."
  value       = aws_cloudwatch_log_group.runners.name
}

output "efs_file_system_id" {
  description = "EFS file system id for the shared KB corpus-artifact storage (infra/efs.tf) — mounted by both the backend service task (ecs.tf) and the kb-runner scheduled task (infra/kb_runner.tf)."
  value       = aws_efs_file_system.corpus_artifact.id
}

output "efs_access_point_id" {
  description = "EFS access point id (posix uid/gid 1001, matches backend/Dockerfile's nonroot user) — referenced by both task definitions' `efs_volume_configuration.authorization_config.access_point_id`."
  value       = aws_efs_access_point.corpus_artifact.id
}

output "kb_runner_task_definition_family" {
  description = "ECS task definition family for the scheduled kb-runner task (infra/kb_runner.tf) — register new revisions against this family for CI deploys (.github/workflows/deploy-runners.yml); the EventBridge Schedule always targets the latest ACTIVE revision (revision-less ARN), not a pinned one."
  value       = aws_ecs_task_definition.kb_runner.family
}

output "kb_runner_schedule_name" {
  description = "EventBridge Scheduler schedule name for kb-runner — `aws scheduler get-schedule --name <this>` to confirm ENABLED state (#1065 Step 3)."
  value       = aws_scheduler_schedule.kb_runner.name
}

output "kb_runner_log_group_name" {
  description = "CloudWatch log group the kb-runner scheduled task ships to — `aws logs tail <this> --since 10m` to verify a run."
  value       = aws_cloudwatch_log_group.kb_runner.name
}

# ── SES bounce/complaint feedback (#1804, infra/ses_events.tf) ──────────────

output "ses_configuration_set_name" {
  description = "SES configuration set every outgoing message names (auth container's SES_CONFIGURATION_SET) — without it SES publishes no bounce or complaint event at all. `aws sesv2 get-configuration-set-event-destinations --configuration-set-name <this>` to confirm the destination is ENABLED."
  value       = aws_sesv2_configuration_set.mail.configuration_set_name
}

output "ses_events_queue_url" {
  description = "SQS queue URL the SES bounce/complaint events land in (via SNS). The value the backend container receives as SES_EVENTS_QUEUE_URL, and the argument `python -m archimedes.scripts.ses_events drain --queue-url <this>` takes when run from an operator shell."
  value       = aws_sqs_queue.ses_events.id
}

output "ses_events_drain_task_definition_family" {
  description = "ECS task definition family for the scheduled SES bounce/complaint drain (infra/ses_events.tf) — the family aws_scheduler_schedule.ses_events_drain invokes every tick. `aws ecs run-task --task-definition <this>` to force one drain by hand; `aws logs tail /archimedes/app --log-stream-name-prefix ses-events-drain --since 30m` to read the last one."
  value       = aws_ecs_task_definition.ses_events_drain.family
}

output "ses_events_dlq_url" {
  description = "Dead-letter queue for SES events the consumer could not parse or write five times running — a non-empty ApproximateNumberOfMessages here means the parser is behind an AWS schema change, not that mail is fine."
  value       = aws_sqs_queue.ses_events_dlq.id
}

output "dmarc_reports_bucket" {
  description = "S3 bucket the SES receipt rule writes DMARC aggregate reports into (infra/dmarc_reports.tf, #1504). Feed it to the parser: `python scripts/dmarc_report_summary.py --bucket $(terraform output -raw dmarc_reports_bucket) --since-days 14`. An empty bucket means no reports have been collected, NOT that nothing is spoofing the domain — see docs/runbooks/dmarc-reports.md."
  value       = aws_s3_bucket.dmarc_reports.id
}
