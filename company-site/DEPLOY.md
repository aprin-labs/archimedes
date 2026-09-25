# Deploying the APRIN Labs one-pager

Three hosting options, in preference order. All three need nothing but the contents of
this directory — no build step, no dependency install.

## 1. S3 + CloudFront + Route 53 alias (preferred)

Preferred because the `aprin.ai` Route 53 hosted zone will already exist once the domain
finishes registering, so an ALIAS record is a two-minute step, not a new zone.

**This option is now implemented as Terraform in `company-site/infra/main.tf`** — that is
the path to use: `cd company-site/infra && terraform init && terraform apply`, then
`aws s3 sync` the page up and invalidate. The raw `aws` CLI recipe below is kept as the
fallback (and as a plain-English description of what the Terraform creates) for anyone who
would rather click through it once than adopt a state file.

`terraform plan` requires the `aprin.ai` hosted zone to already exist in account
`037613907429` — `main.tf` reads it with `data "aws_route53_zone"`, so a plan run before
the zone is there fails on the data source rather than on anything you wrote.

**Owner-executed — this is infra spend on Dan's AWS account.** Exact steps once the zone
is live:

```bash
# 1. Create a private bucket (CloudFront reads it via Origin Access Control —
#    the bucket itself is never public)
aws s3 mb s3://aprin-ai-site-037613907429 --region us-east-1

# 2. Upload the site
aws s3 cp company-site/index.html s3://aprin-ai-site-037613907429/index.html

# 3. Request an ACM certificate in us-east-1 — CloudFront requires the cert to
#    live in us-east-1 regardless of where the bucket is
aws acm request-certificate --domain-name aprin.ai --validation-method DNS --region us-east-1

# 4. Create the CloudFront distribution
#    - Origin: aprin-ai-site-037613907429.s3.us-east-1.amazonaws.com, via Origin Access Control
#    - Default root object: index.html
#    - Alternate domain name (CNAME): aprin.ai
#    - Attach the ACM certificate from step 3

# 5. Route 53: create the aprin.ai hosted zone if it doesn't already exist from
#    registration, then add an A/AAAA ALIAS record pointing aprin.ai at the
#    CloudFront distribution's domain name
aws route53 change-resource-record-sets \
  --hosted-zone-id <ZONE_ID> \
  --change-batch file://alias.json
```

To publish an update later: re-run step 2, then invalidate the CloudFront cache:

```bash
aws cloudfront create-invalidation --distribution-id <DISTRIBUTION_ID> --paths "/*"
```

## 2. GitHub Pages (public repo)

1. Extract `company-site/` into its own public repo (or a `gh-pages` branch of one).
2. Repo Settings → Pages → deploy from branch → root.
3. Once DNS is ready, set the custom domain (`aprin.ai`) in the Pages settings — this
   writes a `CNAME` file automatically — then point Route 53 at GitHub Pages per
   [GitHub's custom-domain docs](https://docs.github.com/pages/configuring-a-custom-domain-for-your-github-pages-site).

## 3. Any other static host

`index.html` has zero build step and zero external dependencies beyond system fonts, so
copying it to Netlify, Vercel, Cloudflare Pages, or any host that serves static files
works unmodified — no option here is load-bearing for the page itself.

---

Options 1 and 2 both involve a DNS/infra decision that belongs to Dan (AWS account owner,
domain registrant). This document lists the mechanics for whichever path he picks; it
doesn't make the pick.
