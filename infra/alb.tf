# ── Application Load Balancer ─────────────────────────────────────────
#
# The ALB is the ONLY public-facing resource once EC2 moves to the
# private subnet. Terminates TLS via ACM certificate, forwards to
# the EC2 target group.
#
# Key config per the locked spec:
#   - idle_timeout = 300s (SSE /api/generate/stream/ survival)
#   - Access logs → S3
#   - HTTP → HTTPS redirect (listener rule)

# ── S3 bucket for ALB access logs ────────────────────────────

resource "aws_s3_bucket" "alb_logs" {
  bucket = "${var.project_name}-alb-logs-159903201072"

  tags = {
    Project = var.project_name
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id

  rule {
    id     = "expire-old-logs"
    status = "Enabled"

    filter {}

    expiration {
      days = 30
    }
  }
}

# ALB requires a specific bucket policy for log delivery
data "aws_elb_service_account" "main" {}

resource "aws_s3_bucket_policy" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "ALBLogDelivery"
        Effect    = "Allow"
        Principal = { AWS = data.aws_elb_service_account.main.arn }
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.alb_logs.arn}/*"
      },
      {
        Sid       = "DenyNonTLS"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.alb_logs.arn,
          "${aws_s3_bucket.alb_logs.arn}/*"
        ]
        Condition = {
          Bool = { "aws:SecureTransport" = "false" }
        }
      }
    ]
  })
}

# ── ACM Certificate ──────────────────────────────────────────
# Use the existing Let's Encrypt cert on the EC2 for now.
# For ALB, we need an ACM certificate. Import or create one.
# Using DNS validation via Route 53.

resource "aws_acm_certificate" "main" {
  domain_name       = var.domain_name
  validation_method = "DNS"

  tags = {
    Project = var.project_name
  }

  lifecycle {
    create_before_destroy = true
  }
}

# Route 53 zone lookup (zone already exists from initial setup)
data "aws_route53_zone" "main" {
  name         = "${var.domain_name}."
  private_zone = false
}

# ACM DNS validation records
resource "aws_route53_record" "acm_validation" {
  for_each = {
    for dvo in aws_acm_certificate.main.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  }

  zone_id = data.aws_route53_zone.main.zone_id
  name    = each.value.name
  type    = each.value.type
  records = [each.value.record]
  ttl     = 60

  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "main" {
  certificate_arn         = aws_acm_certificate.main.arn
  validation_record_fqdns = [for r in aws_route53_record.acm_validation : r.fqdn]
}

# ── Security Group ───────────────────────────────────────────

resource "aws_security_group" "alb" {
  name        = "${var.project_name}-alb-sg"
  description = "ALB - public HTTPS only"
  vpc_id      = aws_vpc.main.id

  # HTTPS from internet
  ingress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # HTTP (for redirect to HTTPS)
  ingress {
    description = "HTTP redirect"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # Outbound to EC2 target only (port 80)
  egress {
    description     = "To EC2 backend"
    from_port       = 80
    to_port         = 80
    protocol        = "tcp"
    security_groups = [aws_security_group.archimedes.id]
  }

  # Outbound to the ECS Fargate backend task (issue #1039) — nginx container
  # port, registered directly on the ENI in awsvpc mode (no host port
  # remapping like the EC2/docker-compose "80:8080"). Added as a SECOND
  # inline egress block on this SAME resource (not a standalone
  # aws_security_group_rule) — this resource's inline ingress/egress blocks
  # are authoritative for ALL rules on this SG, and mixing inline blocks with
  # standalone aws_security_group_rule resources makes Terraform fight itself
  # and delete "unmanaged" rules on every apply (vpc.tf documents the same
  # footgun for route tables). Both this rule and the one above stay live
  # simultaneously during the EC2→Fargate cutover window — see
  # infra/runbooks/ecs-fargate-cutover.md.
  egress {
    description     = "To ECS Fargate backend (nginx container, #1039)"
    from_port       = 8080
    to_port         = 8080
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs_backend.id]
  }

  tags = {
    Name    = "${var.project_name}-alb-sg"
    Project = var.project_name
  }
}

# ── ALB ──────────────────────────────────────────────────────

resource "aws_lb" "main" {
  name               = "${var.project_name}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id

  idle_timeout               = 300  # SSE /api/generate/stream/ needs long-lived connections
  drop_invalid_header_fields = true # Strip malformed headers before they reach the backend
  enable_deletion_protection = true # Require explicit console action to delete

  access_logs {
    bucket  = aws_s3_bucket.alb_logs.id
    enabled = true
  }

  tags = {
    Project = var.project_name
  }
}

# ── Target Group ─────────────────────────────────────────────
# Points at the EC2 instance. Health check on /health (not /api/generate/stream/).

resource "aws_lb_target_group" "backend" {
  name        = "${var.project_name}-backend-tg"
  port        = 80
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id # ALB and TG must be in same VPC
  target_type = "ip"            # IP-based targeting for cross-VPC (EC2 in default VPC)

  health_check {
    enabled           = true
    path              = "/health"
    port              = "traffic-port" # ALB sends health checks on the same port as traffic (80)
    protocol          = "HTTP"
    healthy_threshold = 2
    # 5s x 3 was too tight for a cold task: /health does a real Arc RPC round
    # trip, and under upstream rate limiting (rpc.testnet.arc.network answered
    # 429s on 2026-08-31, #1594) cold tasks blew that budget while warming,
    # the ALB killed them, and the deployment circuit breaker declared three
    # consecutive good revisions "bad" — prod ended up held by a single
    # surviving task. 10s x 5 rides out a rate-limited warmup while a truly
    # dead task still drains inside ~3 minutes. Matches the values applied
    # live during the 2026-08-31 recovery — do not tighten without also
    # removing /health's hard RPC dependency (#1594's structural fix).
    unhealthy_threshold = 5
    interval            = 30
    timeout             = 10
    matcher             = "200"
  }

  tags = {
    Project = var.project_name
  }
}

# ── Phase 4 cutover (issue #1039) rollback window: CLOSED at Phase 8 ──────
# The commented-out aws_lb_target_group_attachment that re-registered the EC2
# box (aws_instance.archimedes) alongside Fargate as a one-deploy-cycle
# rollback path was removed 2026-08-19 with the EC2 decommission (main.tf).

# ── Listeners ────────────────────────────────────────────────

# HTTPS listener (main)
resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate.main.arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.backend.arn
  }
}

# HTTP → HTTPS redirect
resource "aws_lb_listener" "http_redirect" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}
