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
