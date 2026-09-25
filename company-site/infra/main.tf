# aprin.ai company site — standalone stack, deliberately separate from the
# product's infra/ root: its own state key, no shared resources, so a mistake
# here cannot touch the archimedes stack. Apply with AWS_PROFILE=ArchimedesDanAdmin.
#
#   cd company-site/infra && terraform init && terraform apply
#   aws s3 sync ../ "s3://$(terraform output -raw bucket)" \
#     --exclude "infra/*" --exclude "*.md" --delete
#   aws cloudfront create-invalidation \
#     --distribution-id "$(terraform output -raw distribution_id)" --paths "/*"

terraform {
  required_version = ">= 1.10"
  backend "s3" {
    bucket       = "archimedes-tfstate-037613907429"
    key          = "company-site/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}

provider "aws" {
  region = "us-east-1" # CloudFront requires us-east-1 ACM certs; everything else lives here too
  default_tags { tags = { Project = "aprin-site", ManagedBy = "terraform" } }
}

locals {
  domain = "aprin.ai"
}

data "aws_route53_zone" "site" {
  name         = local.domain
  private_zone = false
}

resource "aws_acm_certificate" "site" {
  domain_name               = local.domain
  subject_alternative_names = ["www.${local.domain}"]
  validation_method         = "DNS"
  lifecycle { create_before_destroy = true }
}

resource "aws_route53_record" "cert_validation" {
  for_each = {
    for dvo in aws_acm_certificate.site.domain_validation_options :
    dvo.domain_name => { name = dvo.resource_record_name, type = dvo.resource_record_type, record = dvo.resource_record_value }
  }
  zone_id = data.aws_route53_zone.site.zone_id
  name    = each.value.name
  type    = each.value.type
  ttl     = 300
  records = [each.value.record]
}

resource "aws_acm_certificate_validation" "site" {
  certificate_arn         = aws_acm_certificate.site.arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

resource "aws_s3_bucket" "site" {
  bucket = "aprin-ai-site-037613907429"
}

resource "aws_s3_bucket_public_access_block" "site" {
  bucket                  = aws_s3_bucket.site.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_cloudfront_origin_access_control" "site" {
  name                              = "aprin-site-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_s3_bucket_policy" "site" {
  bucket = aws_s3_bucket.site.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "CloudFrontOACRead"
      Effect    = "Allow"
      Principal = { Service = "cloudfront.amazonaws.com" }
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.site.arn}/*"
      Condition = { StringEquals = { "AWS:SourceArn" = aws_cloudfront_distribution.site.arn } }
    }]
  })
}

resource "aws_cloudfront_distribution" "site" {
  enabled             = true
  is_ipv6_enabled     = true
  default_root_object = "index.html"
  aliases             = [local.domain, "www.${local.domain}"]
  price_class         = "PriceClass_100"

  origin {
    domain_name              = aws_s3_bucket.site.bucket_regional_domain_name
    origin_id                = "s3-site"
    origin_access_control_id = aws_cloudfront_origin_access_control.site.id
  }

  default_cache_behavior {
    target_origin_id       = "s3-site"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    # AWS managed CachingOptimized policy — static site, no cookies/queries.
    cache_policy_id = "658327ea-f89d-4fab-a63d-7e88639e58f6"
  }

  # Single-page site: unknown paths render the page instead of S3's XML error, but they
  # stay 404s. This is a one-pager, not an SPA with client-side routing — a typo'd path
  # is genuinely not found, and answering 200 would lie to crawlers and link checkers.
  # (S3 + OAC returns 403, not 404, for a missing key because ListBucket isn't granted,
  # so both codes map to the same page.)
  custom_error_response {
    error_code         = 403
    response_code      = 404
    response_page_path = "/index.html"
  }
  custom_error_response {
    error_code         = 404
    response_code      = 404
    response_page_path = "/index.html"
  }

  restrictions {
    geo_restriction { restriction_type = "none" }
  }

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.site.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }
}

resource "aws_route53_record" "apex" {
  for_each = toset(["A", "AAAA"])
  zone_id  = data.aws_route53_zone.site.zone_id
  name     = local.domain
  type     = each.key
  alias {
    name                   = aws_cloudfront_distribution.site.domain_name
    zone_id                = aws_cloudfront_distribution.site.hosted_zone_id
    evaluate_target_health = false
  }
}

resource "aws_route53_record" "www" {
  for_each = toset(["A", "AAAA"])
  zone_id  = data.aws_route53_zone.site.zone_id
  name     = "www.${local.domain}"
  type     = each.key
  alias {
    name                   = aws_cloudfront_distribution.site.domain_name
    zone_id                = aws_cloudfront_distribution.site.hosted_zone_id
    evaluate_target_health = false
  }
}

output "bucket" { value = aws_s3_bucket.site.bucket }
output "distribution_id" { value = aws_cloudfront_distribution.site.id }
output "url" { value = "https://${local.domain}" }
