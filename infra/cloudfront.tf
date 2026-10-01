# ── CloudFront distribution in front of the ALB ───────────────────────
#
# OPTIONAL / virality-prep (issue #155). INERT until `terraform apply` runs
# with AWS credentials — merging the PR changes nothing on the live stack.
#
# Edge layer that sits in front of the existing ALB (aws_lb.main in alb.tf):
#   - Static assets cached 1h at the edge: /assets/*, /static/*, /fonts/*,
#     *.js, *.css, *.png, *.svg, *.jpg, *.webp, *.woff2, *.ico. The suffix
#     patterns sit LAST in the behaviour list, behind every never-cached
#     behaviour, so a gated /app/*.png can never be captured by one (#1776).
#   - /health, /health/*, /api/* and /events/* NEVER cached (liveness probes +
#     dynamic responses + SSE pass-through).
#   - /app, /app/* and /sign-in* NEVER cached. The gated pages under /app
#     must not be: their responses depend on the session cookie and the edge
#     keys without one (2026-09-01 sign-in outage, issue #1768). The public
#     anonymous-browse carve-outs (bare /app, /app/explore, /app/corpus) are
#     de-cached alongside them deliberately — the block comment on those
#     behaviours carries the cost and the reason.
#   - Generated 5xx error responses NEVER cached (custom_error_response with
#     error_caching_min_ttl = 0) — see the block near the bottom of this file.
#   - HTML ("/") cached 60s, respecting origin Cache-Control.
#   - Origin Shield in us-east-1 (cheapest ACM region; collapses origin fetches).
#   - Edge rate-limit 2000 req/min/IP via a CLOUDFRONT-scope WAF (us-east-1),
#     defense-in-depth alongside the REGIONAL WAF on the ALB (waf.tf) and
#     slowapi in the backend.
#
# CloudFront REQUIRES the viewer ACM cert AND any attached WAF to live in
# us-east-1 — hence the aws.us_east_1 provider alias below.

# ── us-east-1 provider (CloudFront ACM + WAF must be us-east-1) ──
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"
}

# ── ACM certificate for CloudFront (us-east-1) ────────────────
# Separate from the REGIONAL ALB cert (aws_acm_certificate.main in alb.tf,
# us-east-1). CloudFront can only attach a us-east-1 cert.
resource "aws_acm_certificate" "cloudfront" {
  provider                  = aws.us_east_1
  domain_name               = var.domain_name
  subject_alternative_names = ["www.${var.domain_name}"]
  validation_method         = "DNS"

  tags = {
    Project = var.project_name
  }

  lifecycle {
    create_before_destroy = true
  }
}

# DNS validation records for the CloudFront cert (reuses the existing zone
# data source defined in alb.tf as data.aws_route53_zone.main).
resource "aws_route53_record" "cloudfront_acm_validation" {
  for_each = {
    for dvo in aws_acm_certificate.cloudfront.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  }

  zone_id         = data.aws_route53_zone.main.zone_id
  name            = each.value.name
  type            = each.value.type
  records         = [each.value.record]
  ttl             = 60
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "cloudfront" {
  provider                = aws.us_east_1
  certificate_arn         = aws_acm_certificate.cloudfront.arn
  validation_record_fqdns = [for r in aws_route53_record.cloudfront_acm_validation : r.fqdn]
}

# ── Edge WAF (CLOUDFRONT scope, us-east-1) ────────────────────
# Rate-limit 2000 req / 5 min / IP. CloudFront/WAFv2 rate statements use a
# 5-minute window; 2000/min ≈ 10000/5min, but the issue specifies a 2000/min
# guard, so we set the 5-min limit to 2000 to enforce the stricter 2000-per-
# rolling-window bound the spec asks for at the edge.
resource "aws_wafv2_web_acl" "cloudfront" {
  provider = aws.us_east_1
  name     = "${var.project_name}-cloudfront-waf"
  scope    = "CLOUDFRONT"

  default_action {
    allow {}
  }

  rule {
    name     = "edge-rate-limit"
    priority = 1

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit              = 2000
        aggregate_key_type = "IP"
      }
    }

    visibility_config {
      sampled_requests_enabled   = true
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.project_name}-edge-rate-limit"
    }
  }

  visibility_config {
    sampled_requests_enabled   = true
    cloudwatch_metrics_enabled = true
    metric_name                = "${var.project_name}-cloudfront-waf"
  }

  tags = {
    Project = var.project_name
  }
}

# ── Cache policies ────────────────────────────────────────────

# Long-lived caching for static assets (1h default, respects max-age up to 1d).
resource "aws_cloudfront_cache_policy" "static_assets" {
  name        = "${var.project_name}-static-assets"
  default_ttl = 3600
  min_ttl     = 0
  max_ttl     = 86400

  parameters_in_cache_key_and_forwarded_to_origin {
    cookies_config {
      cookie_behavior = "none"
    }
    headers_config {
      header_behavior = "none"
    }
    query_strings_config {
      query_string_behavior = "none"
    }
    enable_accept_encoding_brotli = true
    enable_accept_encoding_gzip   = true
  }
}

# Short-lived caching for HTML (60s), respecting origin Cache-Control.
resource "aws_cloudfront_cache_policy" "html" {
  name        = "${var.project_name}-html"
  default_ttl = 60
  min_ttl     = 0
  max_ttl     = 60

  parameters_in_cache_key_and_forwarded_to_origin {
    cookies_config {
      cookie_behavior = "none"
    }
    headers_config {
      header_behavior = "whitelist"
      headers {
        items = ["Host"]
      }
    }
    query_strings_config {
      query_string_behavior = "all"
    }
    enable_accept_encoding_brotli = true
    enable_accept_encoding_gzip   = true
  }
}

# Origin request policy: forward everything to the ALB for dynamic paths so
# the backend sees the real Host, headers, cookies, and query string.
resource "aws_cloudfront_origin_request_policy" "all_viewer" {
  name = "${var.project_name}-all-viewer"

  cookies_config {
    cookie_behavior = "all"
  }
  headers_config {
    header_behavior = "allViewerAndWhitelistCloudFront"
    headers {
      # CloudFront-Viewer-Country + device headers feed the visitor-insights
      # instrument (#787): the backend can't see the real client IP (CloudFront
      # masks it), so these CloudFront-derived headers are the only clean source
      # for visitor geography + device class. Forwarded to origin here.
      items = [
        "CloudFront-Forwarded-Proto",
        "CloudFront-Viewer-Country",
        "CloudFront-Is-Mobile-Viewer",
        "CloudFront-Is-Tablet-Viewer",
        "CloudFront-Is-Desktop-Viewer",
        "CloudFront-Is-SmartTV-Viewer",
      ]
    }
  }
  query_strings_config {
    query_string_behavior = "all"
  }
}

# Response headers policy: HSTS + standard security headers at the edge.
resource "aws_cloudfront_response_headers_policy" "security" {
  name = "${var.project_name}-security-headers"

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

# ── Distribution ──────────────────────────────────────────────

locals {
  alb_origin_id = "${var.project_name}-alb-origin"
}

resource "aws_cloudfront_distribution" "main" {
  enabled         = true
  is_ipv6_enabled = true
  comment         = "${var.project_name} edge CDN (virality tier, issue #155)"
  price_class     = "PriceClass_100" # NA + EU edges only (cost containment)
  aliases         = ["${var.domain_name}"]

  origin {
    domain_name = aws_lb.main.dns_name
    origin_id   = local.alb_origin_id

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only" # CloudFront → ALB always over TLS
      origin_ssl_protocols   = ["TLSv1.2"]
    }

    # Origin Shield in us-east-1 — collapses concurrent origin fetches and
    # cuts cost (cheapest region; per the issue spec).
    origin_shield {
      enabled              = true
      origin_shield_region = "us-east-1"
    }
  }

  # Default behavior: HTML — cached 60s, respects origin Cache-Control.
  default_cache_behavior {
    target_origin_id           = local.alb_origin_id
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = aws_cloudfront_cache_policy.html.id
    origin_request_policy_id   = aws_cloudfront_origin_request_policy.all_viewer.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security.id
    compress                   = true
  }

  # /health and /health/* — liveness probes. NEVER cached (#1520).
  #
  # These are listed FIRST deliberately. CloudFront evaluates
  # `ordered_cache_behavior` blocks in the order they appear (precedence 0, 1,
  # 2, ...) and takes the FIRST pattern that matches — NOT the most specific
  # one. A future behaviour with a broad pattern added above these would
  # silently recapture the liveness paths, so they sit at precedence 0 and 1
  # where nothing can get in front of them.
  #
  # Before this existed, `/health` matched no ordered behaviour and fell
  # through to `default_cache_behavior`, whose `html` policy has
  # `default_ttl = 60`. Measured on the live site: `x-cache: Hit from
  # cloudfront` with `age: 17`. A cached health check is not a health check —
  # it keeps answering "ok" for up to a minute after the origin has started
  # failing, which is the plausible-substitute degradation
  # docs/architectural-principles.md § fail-soft names as the primary defect
  # class.
  #
  # The application also sends `Cache-Control: no-store` on every liveness
  # route (#1521, guarded by backend/tests/test_health_is_uncacheable.py). That
  # half closes the stale-200 case on its own, because the `html` policy has
  # `min_ttl = 0` and CloudFront honours an origin `no-store`. It cannot close
  # the error case: a 502/504 is generated by CloudFront or the ALB when the
  # origin does not answer, so there is no origin `Cache-Control` on it to
  # honour. That needs BOTH this behaviour and the `custom_error_response`
  # blocks below.
  #
  # Two patterns, not one `/health*`: `/health` matches the exact path only,
  # and `/health/*` matches the sub-paths (`/health/amm`,
  # `/health/paper-rag`). `/health*` would also capture any future
  # `/health-dashboard.html`, which is a page and should stay cacheable.
  # `/api/health` and `/api/health/amm` are already covered by `/api/*` below,
  # which is bound to the same CachingDisabled policy — no separate behaviour
  # needed, and adding one would burn a cache-behaviour slot for no change in
  # outcome.
  ordered_cache_behavior {
    path_pattern               = "/health"
    target_origin_id           = local.alb_origin_id
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = data.aws_cloudfront_cache_policy.caching_disabled.id
    origin_request_policy_id   = aws_cloudfront_origin_request_policy.all_viewer.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security.id
    compress                   = false
  }

  ordered_cache_behavior {
    path_pattern               = "/health/*"
    target_origin_id           = local.alb_origin_id
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = data.aws_cloudfront_cache_policy.caching_disabled.id
    origin_request_policy_id   = aws_cloudfront_origin_request_policy.all_viewer.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security.id
    compress                   = false
  }

  # /api/* — NEVER cached (dynamic responses: live prices, traces). Uses the
  # AWS-managed CachingDisabled policy so nothing is cached at the edge.
  ordered_cache_behavior {
    path_pattern               = "/api/*"
    target_origin_id           = local.alb_origin_id
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = data.aws_cloudfront_cache_policy.caching_disabled.id
    origin_request_policy_id   = aws_cloudfront_origin_request_policy.all_viewer.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security.id
    compress                   = false
  }

  # /events/* — SSE stream. NEVER cached + no compression (compression buffers
  # and breaks streaming). Pass straight through to the origin.
  ordered_cache_behavior {
    path_pattern             = "/events/*"
    target_origin_id         = local.alb_origin_id
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD", "OPTIONS"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = data.aws_cloudfront_cache_policy.caching_disabled.id
    origin_request_policy_id = aws_cloudfront_origin_request_policy.all_viewer.id
    compress                 = false
  }

  # /assets/* — built JS/CSS bundles. Cached 1h at the edge.
  ordered_cache_behavior {
    path_pattern               = "/assets/*"
    target_origin_id           = local.alb_origin_id
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = aws_cloudfront_cache_policy.static_assets.id
    origin_request_policy_id   = aws_cloudfront_origin_request_policy.all_viewer.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security.id
    compress                   = true
  }

  # /static/* — static files. Cached 1h at the edge.
  ordered_cache_behavior {
    path_pattern               = "/static/*"
    target_origin_id           = local.alb_origin_id
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = aws_cloudfront_cache_policy.static_assets.id
    origin_request_policy_id   = aws_cloudfront_origin_request_policy.all_viewer.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security.id
    compress                   = true
  }

  # /fonts/* — self-hosted webfonts and the OFL licence files beside them.
  # Cached 1h at the edge (#1776).
  #
  # `ui/index.html` carries `<link rel="preload" href="/fonts/gabarito-latin.woff2">`,
  # so these are on the critical render path — and they matched no ordered
  # pattern, which put them on the `html` policy's 60s TTL and made every edge
  # POP re-fetch them from the origin once a minute. They are immutable-by-build
  # files under `ui/public/fonts/`, served straight off nginx's disk
  # (`location /` → `root /usr/share/nginx/html`), not HTML.
  #
  # A prefix pattern rather than a suffix one, so it also covers the `OFL-*.txt`
  # licence files that ship beside the fonts, and any future font format, without
  # spending another cache-behaviour slot. `*.woff2` below is the belt for a font
  # emitted anywhere outside this directory.
  #
  # Safe this far up the list: `/fonts/` cannot collide with any gated path, so
  # unlike the suffix patterns it does not have to sit behind the CachingDisabled
  # behaviours.
  ordered_cache_behavior {
    path_pattern               = "/fonts/*"
    target_origin_id           = local.alb_origin_id
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = aws_cloudfront_cache_policy.static_assets.id
    origin_request_policy_id   = aws_cloudfront_origin_request_policy.all_viewer.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security.id
    compress                   = true
  }

  # /app, /app/* and /sign-in* — NEVER cached at the edge (#1768).
  #
  # 2026-09-01 sign-in outage ("I can't sign into the production site. It
  # thrashes endlessly"). nginx's `auth_request` gate on `^~ /app` answers an
  # anonymous visitor with `302 /sign-in?next=/app/generate`. That response
  # matched no ordered behaviour, so it fell through to
  # `default_cache_behavior` and the `html` policy — `default_ttl = 60`,
  # `cookie_behavior = "none"`. Keyed WITHOUT the session cookie, CloudFront
  # replayed the anonymous redirect to the next request that carried one:
  #
  #   GET /app/generate                            -> 302  x-cache: Miss
  #   GET /app/generate                            -> 302  x-cache: Hit
  #   GET /app/generate  (Cookie: __Secure-...=…)  -> 302  x-cache: Hit   # the bug
  #
  # After sign-in the SPA saw a valid session and jumped to `next`, CloudFront
  # answered with the cached anonymous 302, the sign-in page loaded, saw the
  # session, and jumped again: 165 `GET /api/auth/get-session` calls in 45
  # minutes for one user, while the backend saw no failing request at all.
  #
  # PR #1767 fixed it AT THE ORIGIN — `Cache-Control: private, no-store` on the
  # gated `/app` locations and on the `@sign_in` redirect, honoured because the
  # `html` policy has `min_ttl = 0`. That is necessary but NOT sufficient, and
  # the gap is the whole reason these three behaviours exist:
  #
  #   - It is opt-IN, per response, in a different file. Every session-
  #     dependent response has to remember to say `no-store`; the guard on it
  #     (backend/tests/test_nginx_gated_responses_uncached.py) enumerates the
  #     locations that gate on `auth_request /_auth_session`, so it catches a
  #     deleted header but cannot see a gated response produced anywhere else.
  #     This is opt-OUT, per path: with CachingDisabled the edge has nothing to
  #     replay even when the origin forgets to say so.
  #   - The `html` policy is unchanged and stays right for what it covers. These
  #     paths simply stop matching it, which is the same shape as the /health
  #     behaviours above (#1520) — the fix is "do not match the caching policy",
  #     not "make the caching policy weaker".
  #
  # NOT every path these three patterns cover is session-dependent, and that is
  # a decision rather than an oversight. nginx/nginx.conf carries the
  # anonymous-browse carve-out block (#1753, the owner's product call
  # narrowing #1194 revision d), and nginx picks the LONGEST
  # matching prefix, so those locations win over the gated `^~ /app` below
  # them. Bare `/app` (the SPA's alias for Explore), `/app/explore` and
  # `/app/corpus` are PUBLIC: no `auth_request`, no
  # `error_page 401 = @sign_in`, the same shell for every viewer.
  # `/app/leaderboard` and `/app/strategy/*` used to be on that list and are
  # NOT any more — they are gated, and this file changes nothing about them
  # either way, because `/app/*` already covered both halves. `/app` and
  # `/app/*` pull the public pages off the 60s `html` policy along with the
  # gated ones, on purpose (owner's ruling on the review of PR #1772,
  # 2026-09-01):
  #
  #   Cost. Every anonymous visitor to those pages now reaches the origin
  #   instead of being answered at the edge for up to 60s. What they fetch is
  #   the ~4 KB `ui/index.html` shell, read straight off nginx's disk
  #   (`root /usr/share/nginx/html; try_files $uri $uri/ /index.html`) with no
  #   backend call; the SPA's data then comes over `/api/*`, which was never
  #   cached at the edge anyway.
  #
  #   Benefit. Promoting one of those carve-outs to gated later — which is
  #   exactly what #1753 then did to `/app/leaderboard` and `/app/strategy/*`,
  #   with no edit needed here — cannot reintroduce the cached anonymous 302
  #   of #1767/#1768. The alternative, per-carve-out behaviours
  #   on the `html` policy, makes this file a fourth copy of the anon-page list
  #   that nginx/nginx.conf and `ANON_APP_PAGES` in ui/src/routes.js already
  #   have to keep in lockstep (ui/public/sitemap.xml carries the same set
  #   under its top-level aliases), and a fourth copy needs a fourth lockstep
  #   guard. Origin hits on a static file are the cheaper thing to spend.
  #
  # `all_viewer` forwards cookies, headers and the query string to the origin,
  # so `auth_request` still sees the session cookie and `?next=` still reaches
  # the SPA. The response headers policy is the same one the default behaviour
  # uses: these are the same HTML pages, and HSTS / frame-options must not
  # silently drop off the gated half of the site. `allowed_methods` mirrors the
  # default behaviour's list too, so what changes for these paths is caching and
  # edge compression — not which verbs the edge forwards.
  #
  # `compress = false` matches the other CachingDisabled behaviours, and here it
  # describes rather than decides: the AWS-managed CachingDisabled policy sets
  # `EnableAcceptEncodingGzip = false` (Brotli likewise), and CloudFront
  # compresses only when the behaviour's `compress` flag AND the cache policy's
  # accept-encoding settings are on — so `compress = true` on these blocks would
  # be inert. The cost is real and it lands at the origin: nginx sets `gzip off`
  # on every one of these locations, so the shell reaches the browser
  # uncompressed, ~4.0 KB where gzip would have sent ~1.4 KB. Named, not waved
  # off as free.
  #
  # Ordered AHEAD of `*.js` / `*.css`, not after them. Those patterns match any
  # path ending in `.js` / `.css` — `/app/x.js` included — and bind it to
  # `static_assets`: 1h at the edge with `cookie_behavior = "none"`, the same
  # defect with a longer TTL. Nothing is served under `/app/*.js` today (the
  # SPA's bundles live under `/assets/`), which is precisely why the ordering
  # has to be right before someone puts one there.
  #
  # Two patterns for /app, not one: CloudFront's `/app/*` does not match the
  # bare `/app` — the trailing `/` is literal — the same reason `/health` and
  # `/health/*` are both declared above. `/sign-in*` is prophylactic and honest
  # about it: today `/sign-in` is the identical anonymous SPA shell for every
  # viewer, so caching it harms nothing. It is the other end of the redirect
  # loop, and one nginx edit away (bounce a signed-in visitor off the sign-in
  # page) from being session-dependent.
  #
  # Residual, named rather than papered over: nginx's `^~ /app` is a prefix
  # match, so `/appfoo` is gated too and still resolves to the default
  # behaviour here. A single `/app*` pattern would cover it, at the cost of
  # swallowing any future top-level path that starts with "app" (`/apply`,
  # `/app-store`). What is left uncovered is an anonymous 302 for a path the
  # router does not have.
  ordered_cache_behavior {
    path_pattern               = "/app/*"
    target_origin_id           = local.alb_origin_id
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = data.aws_cloudfront_cache_policy.caching_disabled.id
    origin_request_policy_id   = aws_cloudfront_origin_request_policy.all_viewer.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security.id
    compress                   = false
  }

  ordered_cache_behavior {
    path_pattern               = "/app"
    target_origin_id           = local.alb_origin_id
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = data.aws_cloudfront_cache_policy.caching_disabled.id
    origin_request_policy_id   = aws_cloudfront_origin_request_policy.all_viewer.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security.id
    compress                   = false
  }

  ordered_cache_behavior {
    path_pattern               = "/sign-in*"
    target_origin_id           = local.alb_origin_id
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = data.aws_cloudfront_cache_policy.caching_disabled.id
    origin_request_policy_id   = aws_cloudfront_origin_request_policy.all_viewer.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security.id
    compress                   = false
  }

  # *.js — top-level JS files. Cached 1h at the edge.
  ordered_cache_behavior {
    path_pattern               = "*.js"
    target_origin_id           = local.alb_origin_id
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = aws_cloudfront_cache_policy.static_assets.id
    origin_request_policy_id   = aws_cloudfront_origin_request_policy.all_viewer.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security.id
    compress                   = true
  }

  # *.css — top-level CSS files. Cached 1h at the edge.
  ordered_cache_behavior {
    path_pattern               = "*.css"
    target_origin_id           = local.alb_origin_id
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = aws_cloudfront_cache_policy.static_assets.id
    origin_request_policy_id   = aws_cloudfront_origin_request_policy.all_viewer.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security.id
    compress                   = true
  }

  # ── Image, icon and font suffixes — cached 1h at the edge (#1776) ──
  #
  # Found while verifying PR #1772. CloudFront takes the FIRST matching
  # `ordered_cache_behavior`, and before this block no pattern matched these
  # files at all, so they fell through to `default_cache_behavior` and the
  # `html` policy — `default_ttl = 60`, `query_string_behavior = "all"`:
  #
  #   /og-image.png            the Open Graph card, fetched by every link unfurl
  #   /product-workspace.png   136 KB — the largest single asset on the landing page
  #   /favicon.svg /logo.svg /icons.svg
  #   /favicon-16x16.png /favicon-32x32.png /apple-touch-icon.png
  #   /icon-192.png /icon-512.png
  #
  # All immutable-by-build files under `ui/public/`, served straight off nginx's
  # disk (`location /` → `root /usr/share/nginx/html`), none of them HTML. The
  # 136 KB PNG was re-fetched from the origin every 60s per edge POP, once per
  # distinct query string on top of that.
  #
  # Bound to `static_assets`: `default_ttl = 3600`, `max_ttl = 86400`, and a
  # cache key with `cookie_behavior`, `header_behavior` and
  # `query_string_behavior` all `"none"` — no viewer cookie, header or query
  # string enters it. That is the same policy `/assets/*`, `/static/*`, `*.js`
  # and `*.css` already use. (Precisely: the policy also sets
  # `enable_accept_encoding_brotli/gzip = true`, so CloudFront still splits the
  # key by NORMALISED accept-encoding, exactly as it does for `/assets/*`. That
  # is a compression variant, not a viewer identity.) (`all_viewer` forwards
  # cookies and headers to the ORIGIN on a miss; it is the CACHE POLICY, not the
  # origin request policy, that sets the cache key, so the key stays
  # cookie-blind. Same split as the pre-existing static behaviours, and it is why
  # this file can hand these paths a 1h TTL safely.)
  # Hygiene as well as cost: the 60s `html` policy is the cookie-blind one that
  # carried #1767's cache-poisoning shape, so keeping non-HTML off it is worth
  # doing on its own terms.
  #
  # ORDERED LAST, behind every CachingDisabled behaviour — that is the whole
  # reason this group sits here rather than beside `/assets/*` further up. A
  # suffix pattern matches ANY path ending that way, `/app/hero.png` included,
  # and binding a gated path to a 1h cookie-blind cache is #1768's defect with a
  # longer TTL. `/health`, `/health/*`, `/api/*`, `/events/*`, `/app/*`, `/app`
  # and `/sign-in*` all win first-match ahead of these. Nothing is served under
  # `/app/*.png` today, which is precisely why the ordering has to be right
  # before someone puts one there — the same argument the `/app` blocks above
  # make about `*.js`. `backend/tests/test_cloudfront_static_assets_cached.py`
  # pins the ordering, not just the bindings.
  #
  # Residuals, named rather than papered over:
  #
  #   - nginx's `location /` ends in `try_files $uri $uri/ /index.html`, so a
  #     path with one of these suffixes that does NOT exist on disk — `/typo.png`,
  #     or `/favicon.ico`, which some browsers request by default and which this
  #     repo does not ship — answers 200 with the ~4 KB SPA shell, now cached 1h
  #     instead of 60s and NOT covered by deploy.yml's deliberately narrow
  #     invalidation (`/`, `/?*`, `/index.html*`). That shell is anonymous and
  #     identical for every viewer, so the cost is staleness, never a session
  #     leak — and it is the same shape `*.js` and `*.css` have had since this
  #     file was written.
  #   - Suffixes NOT covered: `.jpeg` (a distinct pattern from `.jpg`), `.gif`,
  #     `.avif`, `.woff`. Nothing under `ui/public/` uses them today, and each
  #     pattern costs one of CloudFront's default 25 cache behaviours per
  #     distribution. The guard enumerates `ui/public/` from disk, so adding such
  #     a file makes this a red test rather than a silent 60s TTL.
  #   - `robots.txt`, `llms.txt`, `sitemap.xml` and `site.webmanifest` stay on
  #     the `html` policy on purpose: they are rewritten by the build, they are
  #     small, and 60s is the right staleness for a file a crawler re-reads.

  # *.png — /og-image.png, /product-workspace.png, the favicons and PWA icons.
  ordered_cache_behavior {
    path_pattern               = "*.png"
    target_origin_id           = local.alb_origin_id
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = aws_cloudfront_cache_policy.static_assets.id
    origin_request_policy_id   = aws_cloudfront_origin_request_policy.all_viewer.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security.id
    compress                   = true
  }

  # *.svg — /favicon.svg, /logo.svg, /icons.svg (the sprite sheet).
  ordered_cache_behavior {
    path_pattern               = "*.svg"
    target_origin_id           = local.alb_origin_id
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = aws_cloudfront_cache_policy.static_assets.id
    origin_request_policy_id   = aws_cloudfront_origin_request_policy.all_viewer.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security.id
    compress                   = true
  }

  # *.jpg — no .jpg ships today; declared so the first one added is cached.
  ordered_cache_behavior {
    path_pattern               = "*.jpg"
    target_origin_id           = local.alb_origin_id
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = aws_cloudfront_cache_policy.static_assets.id
    origin_request_policy_id   = aws_cloudfront_origin_request_policy.all_viewer.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security.id
    compress                   = true
  }

  # *.webp — no .webp ships today; declared so the first one added is cached.
  ordered_cache_behavior {
    path_pattern               = "*.webp"
    target_origin_id           = local.alb_origin_id
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = aws_cloudfront_cache_policy.static_assets.id
    origin_request_policy_id   = aws_cloudfront_origin_request_policy.all_viewer.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security.id
    compress                   = true
  }

  # *.woff2 — a webfont emitted anywhere outside /fonts/ (belt for the prefix above).
  ordered_cache_behavior {
    path_pattern               = "*.woff2"
    target_origin_id           = local.alb_origin_id
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = aws_cloudfront_cache_policy.static_assets.id
    origin_request_policy_id   = aws_cloudfront_origin_request_policy.all_viewer.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security.id
    compress                   = true
  }

  # *.ico — no .ico ships today; see the /favicon.ico residual noted above.
  ordered_cache_behavior {
    path_pattern               = "*.ico"
    target_origin_id           = local.alb_origin_id
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = aws_cloudfront_cache_policy.static_assets.id
    origin_request_policy_id   = aws_cloudfront_origin_request_policy.all_viewer.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security.id
    compress                   = true
  }

  # ── Never cache a 5xx (#1520) ───────────────────────────────
  #
  # `custom_error_response` is a DISTRIBUTION-level setting — CloudFront gives
  # no way to scope error caching to a single cache behaviour, so this applies
  # to every path, not just /health. That is deliberate and not a workaround:
  # a cached 5xx is wrong everywhere on a dynamic app. It converts one slow
  # origin response into an instant error for every viewer hitting that POP
  # until the TTL expires, which is how a blip is amplified into an outage.
  #
  # Measured on the live site during the 2026-08-31 incident, three consecutive
  # probes of /health:
  #
  #   health #1  code=504  total=90.40s   x-response-time-ms: 3203.0
  #   health #2  code=504  total=0.12s
  #   health #3  code=504  total=0.07s
  #
  # The application answered #1 in 3.2s (the X-Response-Time-Ms header added by
  # #1505) while the client saw a 504 at 90.4s; #2 and #3 came back in 0.12s
  # and 0.07s, far too fast to have reached the origin. Those were cached 504s.
  # There was no `custom_error_response` block anywhere in this file, so
  # CloudFront's default error-caching TTL applied.
  #
  # No `response_code` / `response_page_path`: we are not customising the error
  # body, only refusing to cache it. The origin's own response passes through
  # unchanged.
  #
  # 5xx only. 403/404 are left at the CloudFront default on purpose — a missing
  # static asset is a stable fact worth caching, and this issue's anti-goals
  # scope the change to the liveness/error-masking path.
  custom_error_response {
    error_code            = 500
    error_caching_min_ttl = 0
  }

  custom_error_response {
    error_code            = 502
    error_caching_min_ttl = 0
  }

  custom_error_response {
    error_code            = 503
    error_caching_min_ttl = 0
  }

  custom_error_response {
    error_code            = 504
    error_caching_min_ttl = 0
  }

  web_acl_id = aws_wafv2_web_acl.cloudfront.arn

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.cloudfront.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }

  tags = {
    Project = var.project_name
  }
}

# AWS-managed CachingDisabled policy (well-known id) for the dynamic paths.
data "aws_cloudfront_cache_policy" "caching_disabled" {
  name = "Managed-CachingDisabled"
}

# ── Route 53 alias: ${var.domain_name} → CloudFront ───────────
# Replaces the direct A record to the EC2 EIP. Uses the existing zone data
# source (data.aws_route53_zone.main, defined in alb.tf).
resource "aws_route53_record" "apex_cloudfront" {
  zone_id = data.aws_route53_zone.main.zone_id
  name    = var.domain_name
  type    = "A"

  alias {
    name                   = aws_cloudfront_distribution.main.domain_name
    zone_id                = aws_cloudfront_distribution.main.hosted_zone_id
    evaluate_target_health = false
  }
}

resource "aws_route53_record" "apex_cloudfront_ipv6" {
  zone_id = data.aws_route53_zone.main.zone_id
  name    = var.domain_name
  type    = "AAAA"

  alias {
    name                   = aws_cloudfront_distribution.main.domain_name
    zone_id                = aws_cloudfront_distribution.main.hosted_zone_id
    evaluate_target_health = false
  }
}

# ── GitHub Actions deploy role: CloudFront invalidation ────────────────
# Without this, a deploy publishes a new index.html + new hashed asset
# filenames, but CloudFront keeps serving the OLD cached index.html (which
# references asset filenames that no longer exist) until the html cache
# policy's TTL expires — a blank-page/404 outage window on every deploy.
# `.github/workflows/deploy.yml`'s "Invalidate CloudFront" step (deploy-ecs
# job) needs `cloudfront:CreateInvalidation` (+ `GetInvalidation` so it can
# poll the invalidation to completion) on this specific distribution.
# `data.aws_iam_role.github_deploy` is declared once in ecs.tf and reused
# here (same module — no re-declaration needed). Scoped to THIS
# distribution's ARN only, matching the resource-scoped-Sid convention the
# other github_deploy_* / ecs_task_* policies in ecs.tf use — never `"*"`.
resource "aws_iam_role_policy" "github_deploy_cloudfront" {
  name = "archimedes-cloudfront-invalidate"
  role = data.aws_iam_role.github_deploy.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "CloudFrontInvalidate"
        Effect   = "Allow"
        Action   = ["cloudfront:CreateInvalidation", "cloudfront:GetInvalidation"]
        Resource = aws_cloudfront_distribution.main.arn
      }
    ]
  })
}
