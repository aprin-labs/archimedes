terraform {
  required_version = ">= 1.0"

  # Remote state in S3 with S3-native locking (use_lockfile = true).
  # Bootstrap: S3 bucket created out-of-band via AWS CLI.
  # See infra/README.md for the bootstrap commands.
  backend "s3" {
    bucket       = "archimedes-tfstate-037613907429"
    key          = "infra/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true # S3-native locking (Terraform 1.10+), no DynamoDB needed
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.0"
    }
    # Zips Lambda sources into deployment packages at plan time — used by BOTH
    # the deploy-drift function (issue #1596, infra/cloudwatch.tf) and the cost
    # kill-switch (cost_kill_switch.tf). aws_lambda_function has no inline-source
    # form — it needs a real zip on disk — and this first-party provider is the
    # standard way to produce one without committing a binary. NOTE: adding a
    # provider means the next `terraform apply` must be preceded by
    # `terraform init` (it fails with "provider not installed" otherwise).
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------

# Latest Ubuntu 24.04 LTS x86_64 AMI
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }

  filter {
    name   = "architecture"
    values = ["x86_64"]
  }
}

# Default VPC
data "aws_vpc" "default" {
  default = true
}

# ---------------------------------------------------------------------------
# SSH key pair — generated in Terraform, private key saved locally
# ---------------------------------------------------------------------------

# WARNING: tls_private_key puts the private key into Terraform state.
# State is in S3 (encrypted, account-restricted, TLS-only bucket policy).
# Long-term: generate keys out-of-band, only store public key in Terraform.
# The key in state was rotated on 2026-05-26; the old key is revoked.
resource "tls_private_key" "deploy" {
  algorithm = "ED25519"
}

resource "aws_key_pair" "deploy" {
  key_name   = var.key_name
  public_key = tls_private_key.deploy.public_key_openssh

  tags = {
    Project = var.project_name
  }
}

resource "local_sensitive_file" "private_key" {
  content         = tls_private_key.deploy.private_key_openssh
  filename        = "${path.module}/${var.key_name}.pem"
  file_permission = "0600"
}

# ---------------------------------------------------------------------------
# Security group
# ---------------------------------------------------------------------------

resource "aws_security_group" "archimedes" {
  name        = "${var.project_name}-sg"
  description = "Archimedes EC2 - HTTP, HTTPS (admin access via SSM, no inbound SSH)"
  vpc_id      = data.aws_vpc.default.id

  # No SSH ingress. Admin access is via AWS SSM Session Manager (see CLAUDE.md /
  # infra-setup.md), which needs no inbound port. Port 22 open to 0.0.0.0/0 on a
  # funds-holding host was unused attack surface — removed per audit finding #12.

  # HTTP — restricted to ALB VPC CIDR (10.0.0.0/16) only.
  # EC2 (default VPC 172.31.0.0/16) and ALB (aws_vpc.main 10.0.0.0/16) are in
  # separate VPCs connected by VPC peering. SG references can't span VPCs, so
  # we use the ALB VPC CIDR to block direct internet access to the EC2's nginx
  # and force all traffic through the ALB/WAF. (AUDIT I1)
  ingress {
    description = "HTTP from ALB VPC only"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/16"]
  }

  # HTTPS — restricted to ALB VPC CIDR (10.0.0.0/16) only. Same rationale as HTTP above.
  ingress {
    description = "HTTPS from ALB VPC only"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/16"]
  }

  # Ports 8000 (FastAPI), 3000 (Next.js dev), 5432 (Postgres), 6379 (Redis)
  # are deliberately NOT exposed. All traffic goes through nginx on 80/443.
  # Backend, DB, and cache are only reachable via Docker's internal network.

  # Egress — all
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name    = "${var.project_name}-sg"
    Project = var.project_name
  }
}

# ---------------------------------------------------------------------------
# EC2 instance — DECOMMISSIONED 2026-08-19.
#
# aws_instance.archimedes (i-01803d3abc271d39b) and its aws_eip.archimedes
# were the original single-EC2 Docker-Compose host. Superseded by the ECS
# Fargate backend service (ecs.tf, issue #1039) at the Phase 4 cutover — the
# ALB stopped forwarding to it (see alb.tf's Phase 4 note) and it sat RUNNING
# but unreferenced as a one-deploy-cycle rollback window. Both the box's
# rollback usefulness and the relocated runners (runner_ec2.tf, #1065/#1043)
# have since been proven out; the instance was stopped and snapshotted
# (snap-02edf9e4a9ac7f201) ahead of this removal. Dan-authorized teardown.
#
# `aws_security_group.archimedes` (above) is NOT removed here: aurora.tf and
# elasticache.tf still carry live "transitional" ingress rules keyed off its
# SG id, and this PR deliberately does not touch those other-lane files (see
# the PR body) — so the SG stays as an orphaned-but-harmless ingress source.
# ---------------------------------------------------------------------------
