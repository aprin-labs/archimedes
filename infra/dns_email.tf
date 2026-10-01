# ── Email authentication for ${var.domain_name} (#1462) ──────────────────────
#
# The zone publishes three SES DKIM CNAMEs and the app sends real transactional
# mail (Better Auth verification + password reset), but had no SPF and no DMARC.
# Verified against live DNS on 2026-08-29:
#
#   dig +short TXT archimedes-arc.com @8.8.8.8
#     "google-site-verification=nHeZsrl8SxRsJeKWIQx0kaSQkHOlzPDdfRZU_ZCUqk8"
#   dig +short TXT _dmarc.archimedes-arc.com @8.8.8.8
#     (empty)
#
# DKIM alone does not close this. DKIM proves a message we signed is authentic;
# it says nothing about an unsigned forgery, and with no published policy a
# receiver has nothing to check a forged envelope sender against. This matters
# more now that /privacy and /terms publish privacy@archimedes-arc.com as the
# contact for data-protection requests — a spoofable domain on the address
# users are told to trust is a ready-made phishing pretext.

# ── SPF ──────────────────────────────────────────────────────────────────────
#
# CAREFUL: Route 53 holds ONE record set per (name, type), so the apex TXT
# cannot be split across two `aws_route53_record` resources. The Google Search
# Console verification string already lives there and is NOT currently managed
# by Terraform, so this resource has to ADOPT that record set rather than add
# alongside it — which is why `allow_overwrite` is true and why the Google
# string is carried explicitly below.
#
# Dropping that string from this list would un-verify Search Console on the
# next apply. It is a published DNS value, not a secret.
resource "aws_route53_record" "apex_txt" {
  zone_id = data.aws_route53_zone.main.zone_id
  name    = var.domain_name
  type    = "TXT"
  ttl     = 300

  records = [
    var.google_site_verification,
    # -all (hard fail) rather than ~all: SES is the only sender, so there is no
    # legitimate mail from anywhere else to soft-fail. If another sender is
    # added later, extend this include list in the SAME apply that enables it.
    "v=spf1 include:amazonses.com -all",
  ]

  # Adopts the existing unmanaged record set. Without this the first apply
  # fails with "record already exists"; with it, the record is replaced by the
  # full list above — which is why both strings must be present.
  allow_overwrite = true
}

# ── DMARC ────────────────────────────────────────────────────────────────────
#
# p=none deliberately. It publishes a policy and asks for aggregate reports
# WITHOUT changing how any receiver treats our mail, so it cannot silently drop
# our own sign-in and password-reset mail. Moving to p=quarantine and then
# p=reject is a separate, evidence-led change: do it once the rua reports show
# every legitimate sending path is aligned, not before.
#
# The reports themselves are collected by dmarc_reports.tf (#1504): an SES
# receipt rule for var.dmarc_rua_address writing to a private S3 bucket. Until
# that is applied this record names a mailbox nothing delivers to, so there is
# no evidence and any change to `p=` below is a guess. The four conditions that
# have to hold before it moves, and the quarantine → reject ramp, are in
# docs/runbooks/dmarc-reports.md; scripts/dmarc_report_summary.py produces the
# table they are read from.
#
# p=none is not decorative even before reports arrive — Gmail's and Yahoo's
# bulk-sender rules require a DMARC record to exist at all.
resource "aws_route53_record" "dmarc" {
  zone_id = data.aws_route53_zone.main.zone_id
  name    = "_dmarc.${var.domain_name}"
  type    = "TXT"
  ttl     = 300

  # fo=1 asks for a failure report when EITHER SPF or DKIM fails, rather than
  # only when both do — the useful setting while observing.
  records = [
    "v=DMARC1; p=none; rua=mailto:${var.dmarc_rua_address}; fo=1",
  ]
}
