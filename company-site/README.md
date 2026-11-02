# APRIN Labs — company site (staged)

This directory is a self-contained static one-pager for APRIN Labs. It is staged inside
the `archimedes` repo temporarily, until `aprin.ai` and its hosting exist, at which point
it should be extracted to its own repo (or the source dir of whatever static host is
chosen) and deployed on its own.

## Status (2026-08-20)

- Domain `aprin.ai` is being registered — **not live yet**.
- Discord invite URL and X handle don't exist yet. They're placeholders (`''`) in the
  `LINKS` config at the top of `index.html`'s `<head>` — an empty value renders nothing
  (no dead links, no dangling icons), by design.
- GitHub is the one live link today: `https://github.com/aprin-labs`.

## What's here

- `index.html` — the one-pager. No build step, no framework, no external assets other
  than the system font stack. Open it directly (`file://…/index.html`) or drop it on any
  static host and it works unmodified.
- `DEPLOY.md` — the three hosting options considered, with exact steps for the preferred
  one (S3 + CloudFront + Route 53).
- `infra/main.tf` — Terraform implementing that preferred option: a private S3 bucket
  (`aprin-ai-site-037613907429`) read through CloudFront Origin Access Control, an ACM
  cert, and the Route 53 alias records, with state in the shared
  `archimedes-tfstate-037613907429` bucket under its own key. It stands up **billable AWS
  resources on the production account** — CloudFront, ACM/Route 53 queries, S3 storage —
  so `plan`/`apply` are owner-executed, never CI. Nothing in this repo applies it
  automatically.

## Terraform state backend (S3)

State for `infra/main.tf` lives in the S3 bucket `archimedes-tfstate-037613907429`
(us-east-1) under the key `company-site/terraform.tfstate`, with S3-native locking
(Terraform 1.10+, `use_lockfile = true`; no DynamoDB table). The bucket originally held
the product's own Terraform state as well; that stack was torn down on 2026-11-01, and
the bucket now serves only this stack.

The bucket was created out-of-band with the AWS CLI (infrastructure-of-infrastructure:
it never changes and is deliberately not managed by Terraform). This is the record of
how it was bootstrapped, so it can be audited or rebuilt.

### Bootstrap commands (run once, already done)

```bash
# S3 bucket — versioned, encrypted, no public access
aws s3api create-bucket \
  --bucket archimedes-tfstate-037613907429 \
  --region us-east-1

aws s3api put-bucket-versioning \
  --bucket archimedes-tfstate-037613907429 \
  --versioning-configuration Status=Enabled

aws s3api put-bucket-encryption \
  --bucket archimedes-tfstate-037613907429 \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

aws s3api put-public-access-block \
  --bucket archimedes-tfstate-037613907429 \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

# Bucket policy: deny non-TLS + restrict to account only
aws s3api put-bucket-policy \
  --bucket archimedes-tfstate-037613907429 \
  --policy '{
    "Version": "2012-10-17",
    "Statement": [
      {"Sid":"DenyNonTLS","Effect":"Deny","Principal":"*","Action":"s3:*",
       "Resource":["arn:aws:s3:::archimedes-tfstate-037613907429","arn:aws:s3:::archimedes-tfstate-037613907429/*"],
       "Condition":{"Bool":{"aws:SecureTransport":"false"}}},
      {"Sid":"RestrictToAccount","Effect":"Deny","Principal":"*","Action":"s3:*",
       "Resource":["arn:aws:s3:::archimedes-tfstate-037613907429","arn:aws:s3:::archimedes-tfstate-037613907429/*"],
       "Condition":{"StringNotEquals":{"aws:PrincipalAccount":"037613907429"}}}
    ]}'
```

### Credentials (every session)

The S3 backend and the AWS provider use your SSO credentials, so Terraform needs
`AWS_PROFILE` exported. Without it, Terraform falls through to EC2 instance metadata
and dies with *"No valid credential sources found / no EC2 IMDS role found."*

```bash
aws sso login --profile ArchimedesDanAdmin           # refresh the SSO session
export AWS_PROFILE=ArchimedesDanAdmin AWS_REGION=us-east-1
aws sts get-caller-identity                          # smoke-test (account 037613907429)
```

## Drop-in / extraction steps

When the domain and hosting decision land:

1. `cp -r company-site/ <destination>` — this directory has zero dependency on the rest
   of the `archimedes` tree, so a straight copy is enough.
2. Fill in `LINKS.discord` and `LINKS.x` in the `<script>` block near the top of
   `index.html`'s `<head>` once the Discord invite and X handle exist.
3. Pick a hosting path from `DEPLOY.md` and stand it up (S3 + CloudFront is preferred
   once the `aprin.ai` Route 53 zone exists; this is owner-executed, infra spend).
4. Point `aprin.ai` (and `www.aprin.ai` if desired) at the chosen host.
5. Once the standalone site is live, remove `company-site/` from this repo (or keep it
   as historical reference — team's call at that point).
