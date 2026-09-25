# docs.archimedes-arc.com — the mkdocs site (docs/ + the agent-generated
# openwiki/ tree) served from our own account. Issue #1634; Dan's hosting call,
# recorded on that issue 2026-08-31: S3 + CloudFront in our terraform, NOT
# GitHub Pages. Publishing from a third party meant a second control plane, a
# CNAME pointing off-account, and no way to put the docs behind the same edge
# policy (TLS, security headers, access logs) as everything else we run.
#
# STANDALONE ROOT, deliberately. Same reasoning as company-site/infra (which
# this file is modelled on, resource for resource): its own state key, no
# resource shared with the product's infra/ root, so a mistake here cannot
# touch the archimedes stack — and `terraform plan` in infra/ shows nothing
# attributable to the docs site. Apply with AWS_PROFILE=ArchimedesDanAdmin.
#
#   cd docs-site/infra && terraform init && terraform apply
#   mkdocs build --strict --site-dir _site          # from the repo root
#   aws s3 sync _site "s3://$(terraform output -raw bucket)" --delete
#   aws cloudfront create-invalidation \
#     --distribution-id "$(terraform output -raw distribution_id)" --paths "/*"
#
# CI does the last two steps on every docs-path push to main
# (.github/workflows/docs-site.yml); the commands above are the manual
# equivalent, for the first apply and for a rollback. Full procedure:
# docs/runbooks/docs-site-setup.md.

terraform {
  required_version = ">= 1.10"
  backend "s3" {
    bucket = "archimedes-tfstate-037613907429"
    # Its own key. Not infra/terraform.tfstate, not company-site's — the whole
    # point of a separate root is a separate blast radius.
    key          = "docs-site/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}

provider "aws" {
  region = "us-east-1" # CloudFront requires a us-east-1 ACM cert; nothing here needs another region
  default_tags { tags = { Project = "archimedes-docs", ManagedBy = "terraform" } }
}

locals {
  # Sole docs hostname. The `.app` domain was decommissioned (it caused the
  # Circle passkey rpId bug) — do not add a second alias here.
  domain = "docs.archimedes-arc.com"
  zone   = "archimedes-arc.com"
}

# The product zone, already in this account. Read, never managed here: the
# product root (infra/alb.tf) reads the same zone the same way, so both roots
# can add records to it without either one owning it.
data "aws_route53_zone" "docs" {
  name         = local.zone
  private_zone = false
}

resource "aws_acm_certificate" "docs" {
  domain_name       = local.domain
  validation_method = "DNS"
  lifecycle { create_before_destroy = true }
}

resource "aws_route53_record" "cert_validation" {
  for_each = {
    for dvo in aws_acm_certificate.docs.domain_validation_options :
    dvo.domain_name => { name = dvo.resource_record_name, type = dvo.resource_record_type, record = dvo.resource_record_value }
  }
  zone_id = data.aws_route53_zone.docs.zone_id
  name    = each.value.name
  type    = each.value.type
  ttl     = 300
  records = [each.value.record]
  # The product root validates its own certs in this zone. If a validation
  # record for this name already exists, adopt it rather than failing the
  # apply — same flag, same reason, as infra/cloudfront.tf's validation records.
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "docs" {
  certificate_arn         = aws_acm_certificate.docs.arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

# ── Origin: a private bucket, reachable only through CloudFront ──────────────

resource "aws_s3_bucket" "docs" {
  bucket = "archimedes-docs-site-037613907429"
}

resource "aws_s3_bucket_public_access_block" "docs" {
  bucket                  = aws_s3_bucket.docs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_cloudfront_origin_access_control" "docs" {
  name                              = "archimedes-docs-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_s3_bucket_policy" "docs" {
  bucket = aws_s3_bucket.docs.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "CloudFrontOACRead"
      Effect    = "Allow"
      Principal = { Service = "cloudfront.amazonaws.com" }
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.docs.arn}/*"
      Condition = { StringEquals = { "AWS:SourceArn" = aws_cloudfront_distribution.docs.arn } }
    }]
  })
}

# ── Directory URLs ──────────────────────────────────────────────────────────
#
# mkdocs defaults to `use_directory_urls: true` and mkdocs.yml does not turn it
# off, so every page is written as `<path>/index.html` and linked as `<path>/`.
# CloudFront's `default_root_object` only applies to `/` — it does NOT map
# `/openwiki/quickstart/` onto `/openwiki/quickstart/index.html`. Without this
# function every page below the root serves the 404, and the site would look
# live while being ~1 page deep.
#
# Written in conservative ES5: the cloudfront-js-2.0 runtime is ECMAScript 5.1
# plus a partial ES6 surface, so `endsWith`, arrow functions and template
# literals are avoided on purpose rather than by accident.
resource "aws_cloudfront_function" "directory_index" {
  name    = "archimedes-docs-directory-index"
  runtime = "cloudfront-js-2.0"
  publish = true
  comment = "mkdocs directory URLs: /path/ -> /path/index.html, /path -> 301 /path/"
  code    = <<-JS
    function handler(event) {
      var request = event.request;
      var uri = request.uri;

      // "/openwiki/quickstart/" -> "/openwiki/quickstart/index.html"
      if (uri.charAt(uri.length - 1) === '/') {
        request.uri = uri + 'index.html';
        return request;
      }

      // "/openwiki/quickstart" -> 301 to "/openwiki/quickstart/".
      // A silent rewrite to ".../index.html" would leave the browser's base
      // URL without the trailing slash, so every relative asset link
      // mkdocs-material emits ("../assets/...") would resolve one directory
      // too high. Redirect instead, and let the branch above do the work.
      var last = uri.substring(uri.lastIndexOf('/') + 1);
      if (last.indexOf('.') === -1) {
        return {
          statusCode: 301,
          statusDescription: 'Moved Permanently',
          headers: { location: { value: uri + '/' } }
        };
      }

      // Anything with an extension (assets, sitemap.xml, 404.html) is a real key.
      return request;
    }
  JS
}

# HSTS + the same security headers the product edge sets
# (infra/cloudfront.tf's aws_cloudfront_response_headers_policy.security).
# Putting the docs on our own edge is most of why this issue exists; serving
# them without the policy would give that up for nothing.
resource "aws_cloudfront_response_headers_policy" "docs" {
  name = "archimedes-docs-security-headers"

  security_headers_config {
    strict_transport_security {
      access_control_max_age_sec = 31536000
      include_subdomains         = true
      preload                    = true
      override                   = true
    }
    content_type_options {
      override = true
    }
    frame_options {
      frame_option = "DENY"
      override     = true
    }
    referrer_policy {
      referrer_policy = "strict-origin-when-cross-origin"
      override        = true
    }
  }
}

resource "aws_cloudfront_distribution" "docs" {
  enabled             = true
  is_ipv6_enabled     = true
  default_root_object = "index.html"
  aliases             = [local.domain]
  price_class         = "PriceClass_100" # NA + EU edges, same cost containment as the product edge
  comment             = "archimedes docs site (issue #1634)"

  origin {
    domain_name              = aws_s3_bucket.docs.bucket_regional_domain_name
    origin_id                = "s3-docs"
    origin_access_control_id = aws_cloudfront_origin_access_control.docs.id
  }

  default_cache_behavior {
    target_origin_id       = "s3-docs"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    # AWS managed CachingOptimized — a static site with no cookies and no
    # query strings that change the response.
    cache_policy_id            = "658327ea-f89d-4fab-a63d-7e88639e58f6"
    response_headers_policy_id = aws_cloudfront_response_headers_policy.docs.id

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.directory_index.arn
    }
  }

  # mkdocs writes a real 404.html at the site root; serve it, with a 404 status.
  # S3 + OAC answers a missing key with 403 (ListBucket is deliberately not
  # granted), so both codes map to the same page. `error_caching_min_ttl = 0`
  # keeps a 404 from being cached over the top of a page that a later sync adds.
  custom_error_response {
    error_code            = 403
    response_code         = 404
    response_page_path    = "/404.html"
    error_caching_min_ttl = 0
  }
  custom_error_response {
    error_code            = 404
    response_code         = 404
    response_page_path    = "/404.html"
    error_caching_min_ttl = 0
  }

  restrictions {
    geo_restriction { restriction_type = "none" }
  }

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.docs.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }
}

# A + AAAA aliases. `docs.` was never created as a CNAME to a-apin.github.io —
# the GitHub Pages path never went live — so there is nothing to remove first.
resource "aws_route53_record" "docs" {
  for_each = toset(["A", "AAAA"])
  zone_id  = data.aws_route53_zone.docs.zone_id
  name     = local.domain
  type     = each.key
  alias {
    name                   = aws_cloudfront_distribution.docs.domain_name
    zone_id                = aws_cloudfront_distribution.docs.hosted_zone_id
    evaluate_target_health = false
  }
}

output "bucket" { value = aws_s3_bucket.docs.bucket }
output "distribution_id" { value = aws_cloudfront_distribution.docs.id }
output "url" { value = "https://${local.domain}" }
