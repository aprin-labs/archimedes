# ── AWS WAF v2 ────────────────────────────────────────────────────────
#
# Attached to the ALB. Per the locked spec:
#   - Core Rule Set (AWSManagedRulesCommonRuleSet)
#   - Known Bad Inputs (AWSManagedRulesKnownBadInputsRuleSet)
#   - IP Reputation (AWSManagedRulesAmazonIpReputationList)
#   - SQL Database (AWSManagedRulesSQLiRuleSet)
#   - Rate-based rule: 1000 requests per 5 minutes per IP
#   - NO Bot Control (cost optimization)
#   - NO geo-blocking

resource "aws_wafv2_web_acl" "main" {
  name  = "${var.project_name}-waf"
  scope = "REGIONAL"

  default_action {
    allow {}
  }

  # ── Rate-based rule: 1000 req / 5 min / IP ────────────────

  rule {
    name     = "rate-limit"
    priority = 1

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit              = 1000
        aggregate_key_type = "IP"
      }
    }

    visibility_config {
      sampled_requests_enabled   = true
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.project_name}-rate-limit"
    }
  }

  # ── AWS Managed Rules ──────────────────────────────────────

  rule {
    name     = "aws-core-rules"
    priority = 10

    override_action {
      none {} # BLOCK mode active (AUDIT I4)
    }

    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesCommonRuleSet"

        # ── #1749: SizeRestrictions_BODY, counted not blocked ──────────
        #
        # The managed CRS rule `SizeRestrictions_BODY` blocks any request whose
        # body exceeds the WAF body-inspection limit for the protected resource
        # — 8,192 bytes for a REGIONAL web ACL on an Application Load Balancer.
        # POST /api/rigor/verify carries one JSON object per bar — shape
        # {"date": "...", "daily_return": ...} — so payload size is linear in the
        # sample size. Dogfood 2026-09-01 (agent-cli), deterministic:
        #
        #     160 rows / 8,076 B -> 200      MEASURED
        #     165 rows / 8,326 B -> 403      MEASURED (HTML from awselb/2.0,
        #                                    not a FastAPI error body)
        #     252 rows            -> 403     MEASURED, every time <- ONE YEAR
        #
        # Those two measured points give 50.5 bytes/row including the envelope
        # ((8,326 - 8,076) / 5 = 50.0; 8,076 / 160 = 50.5), so the 8,192-byte
        # limit is crossed at ~163 rows and a 252-bar year is ~12.7 KB
        # (extrapolated, not measured).
        #
        # i.e. the endpoint whose whole purpose is DSR/OOS statistical power
        # rejects exactly the sample size that gives it power.
        #
        # Overriding the rule to `count` here does NOT drop the 8 KB ceiling
        # site-wide: the custom `oversize-body-except-rigor-verify` rule at
        # priority 11 (immediately below) re-imposes it for every path EXCEPT
        # /api/rigor/verify. Count (rather than deleting the rule) keeps the
        # managed rule's CloudWatch metric and `awswaf:managed:aws:core-rule-set:
        # SizeRestrictions_Body` label, so oversize traffic stays observable.
        #
        # Only this ONE rule is overridden. Every other CRS rule — XSS, LFI,
        # RFI, the other SizeRestrictions_* (URI/QUERYSTRING/COOKIE_HEADER),
        # NoUserAgent_HEADER, … — still BLOCKS on every path, /api/rigor/verify
        # included. Guarded by backend/tests/test_waf_verify_body.py.
        rule_action_override {
          name = "SizeRestrictions_BODY"

          action_to_use {
            count {}
          }
        }
      }
    }

    visibility_config {
      sampled_requests_enabled   = true
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.project_name}-core-rules"
    }
  }

  # ── #1749: re-impose the 8 KB body ceiling everywhere but the verify path ──
  #
  # Priority 11: evaluated immediately after the CRS group above, before
  # known-bad-inputs (20), ip-reputation (30) and sqli (40). Blocks any request
  # whose body exceeds the same 8,192 bytes the managed rule enforced, UNLESS
  # the URI path is exactly /api/rigor/verify.
  #
  # Mechanism (AWS WAF "Oversize handling for request components"): on a
  # REGIONAL web ACL protecting an ALB, only the first 8,192 bytes of a body are
  # forwarded to WAF, so the GT-8192 comparison can never be satisfied by the
  # INSPECTED portion alone. `oversize_handling = "MATCH"` is what actually
  # fires: a body larger than the inspection limit is treated as matching the
  # statement. The GT comparison is kept as a belt-and-braces backstop in case
  # the inspection limit is ever raised (see the association_config note at the
  # bottom of this file). Either way the effective condition is "body > 8 KB".
  #
  # The exception is EXACTLY one literal path, not a prefix: `positional_
  # constraint = "EXACTLY"` on uri_path. /api/rigor/verifyX, /api/rigor/verify/2
  # and every other /api/* route keep the 8 KB ceiling. The narrowing is
  # defensible because the route is auth-gated (require_current_user,
  # rigor_verify_routes.py) and its schema is closed — a list of
  # {date: str, daily_return: float} with an explicit 2,600-row cap
  # (rigor_verify_routes._MAX_RETURN_ROWS), so the application, not the edge,
  # owns the real ceiling and fails closed at a number we chose.
  #
  # KNOWN CONSEQUENCE (documented, NOT fixed here): the ALB body-inspection
  # limit stays at 8,192 bytes, so on /api/rigor/verify the CRS / SQLi /
  # known-bad-inputs signatures still see only the FIRST 8 KB of the body. Bytes
  # past 8,192 on that one path reach the application un-inspected. It cannot be
  # raised for this resource — see the association_config note at the bottom of
  # this file for the verification.
  rule {
    name     = "oversize-body-except-rigor-verify"
    priority = 11

    action {
      block {}
    }

    statement {
      and_statement {
        statement {
          size_constraint_statement {
            comparison_operator = "GT"
            size                = 8192

            field_to_match {
              body {
                oversize_handling = "MATCH"
              }
            }

            text_transformation {
              priority = 0
              type     = "NONE"
            }
          }
        }

        statement {
          not_statement {
            statement {
              byte_match_statement {
                search_string         = "/api/rigor/verify"
                positional_constraint = "EXACTLY"

                field_to_match {
                  uri_path {}
                }

                text_transformation {
                  priority = 0
                  type     = "NONE"
                }
              }
            }
          }
        }
      }
    }

    visibility_config {
      sampled_requests_enabled   = true
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.project_name}-oversize-body"
    }
  }

  rule {
    name     = "aws-known-bad-inputs"
    priority = 20

    override_action {
      none {} # BLOCK mode active (AUDIT I4)
    }

    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesKnownBadInputsRuleSet"
      }
    }

    visibility_config {
      sampled_requests_enabled   = true
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.project_name}-known-bad-inputs"
    }
  }

  rule {
    name     = "aws-ip-reputation"
    priority = 30

    override_action {
      none {} # IP reputation can block immediately — known-bad IPs
    }

    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesAmazonIpReputationList"
      }
    }

    visibility_config {
      sampled_requests_enabled   = true
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.project_name}-ip-reputation"
    }
  }

  rule {
    name     = "aws-sqli"
    priority = 40

    override_action {
      # BLOCK mode active (AUDIT #23). The SQLi managed rule group used to
      # false-positive on LLM prompt bodies; it now enforces, but a scope-down
      # statement below excludes the two LLM endpoints (/api/strategies/generate
      # and /api/chat) so legitimate prompt traffic on those paths is never
      # evaluated by — and therefore never blocked by — the SQLi rules.
      none {}
    }

    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesSQLiRuleSet"

        # Scope-down: only evaluate requests whose URI path is NOT one of the
        # LLM endpoints. LLM prompts routinely contain SQL-like tokens (SELECT,
        # quotes, UNION, --) that trip the SQLi signatures; excluding these two
        # paths lets the group block SQLi everywhere else while leaving the
        # prompt endpoints untouched. Exact-match on the path; the backend
        # routes are mounted at these literal paths.
        scope_down_statement {
          not_statement {
            statement {
              or_statement {
                statement {
                  byte_match_statement {
                    search_string         = "/api/strategies/generate"
                    positional_constraint = "EXACTLY"

                    field_to_match {
                      uri_path {}
                    }

                    text_transformation {
                      priority = 0
                      type     = "NONE"
                    }
                  }
                }

                statement {
                  byte_match_statement {
                    search_string         = "/api/chat"
                    positional_constraint = "EXACTLY"

                    field_to_match {
                      uri_path {}
                    }

                    text_transformation {
                      priority = 0
                      type     = "NONE"
                    }
                  }
                }
              }
            }
          }
        }
      }
    }

    visibility_config {
      sampled_requests_enabled   = true
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.project_name}-sqli"
    }
  }

  visibility_config {
    sampled_requests_enabled   = true
    cloudwatch_metrics_enabled = true
    metric_name                = "${var.project_name}-waf"
  }

  tags = {
    Project = var.project_name
  }
}

# ── Associate WAF with ALB ───────────────────────────────────
#
# #1749 — why there is NO `association_config { request_body { ... } }` on
# aws_wafv2_web_acl.main:
#
# The scope IS "REGIONAL", which is one of the two preconditions for raising the
# body-inspection limit above 8 KB. The other is that the ASSOCIATED RESOURCE
# TYPE supports it, and an Application Load Balancer does not.
#
# Verified locally against the provider schema this stack resolves to
# (`terraform init -backend=false` under `~> 5.0` from infra/main.tf picks
# hashicorp/aws v5.100.0), by adding each candidate block and running
# `terraform validate`:
#
#     api_gateway               Success! The configuration is valid.
#     app_runner_service        Success! The configuration is valid.
#     cloudfront                Success! The configuration is valid.
#     cognito_user_pool         Success! The configuration is valid.
#     verified_access_instance  Success! The configuration is valid.
#     application_load_balancer Blocks of type "application_load_balancer" are
#                               not expected here.
#     alb / load_balancer       (same error)
#
# There is no ALB form of the setting to write, which matches the AWS docs:
# `default_size_inspection_limit` (KB_16/KB_32/KB_48/KB_64) is offered for
# CloudFront, API Gateway, Cognito user pools, App Runner and Verified Access —
# not for Application Load Balancers, whose regional body-inspection limit is
# fixed at 8,192 bytes.
#
# CONSEQUENCE, stated plainly: a 13 KB verify payload now REACHES the
# application (that is the fix), but WAF's signature rules will have inspected
# only its first 8 KB. On /api/rigor/verify — an authenticated route
# (require_current_user) with a closed schema (list of {date, daily_return}) and
# a 2,600-row application-side cap — that residual is bounded and accepted.
# Full-body inspection there would require moving the enforcement point off the
# ALB entirely (e.g. onto CloudFront, cloudfront.tf), which is a different
# change with its own WCU cost.

resource "aws_wafv2_web_acl_association" "main" {
  resource_arn = aws_lb.main.arn
  web_acl_arn  = aws_wafv2_web_acl.main.arn
}
