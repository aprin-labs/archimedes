# Deployment Runbook — Archimedes on AWS (account 037613907429 / us-east-1)

> **ARCHIVED 2026-07-28 — historical. DO NOT EXECUTE.**
> This procedure routes traffic to EC2 instance `i-01803d3abc271d39b`, which was detached
> from the ALB target group during the 2026-07-09 Fargate cutover (`infra/alb.tf`) and then
> **decommissioned outright on 2026-08-19** — stopped, snapshotted
> (`snap-02edf9e4a9ac7f201`) and its terraform deleted (`infra/main.tf`, PR #1265). Every
> command below now names an instance that no longer exists; the SSM calls would fail rather
> than mislead, but the runbook is history, not a fallback.
> Current rollback procedure: [`infra/runbooks/ecs-fargate-cutover.md`](../../infra/runbooks/ecs-fargate-cutover.md) § rollback and
> [`infra/runbooks/disaster-recovery.md`](../../infra/runbooks/disaster-recovery.md).
> **A Fargate-era break-glass runbook has not yet been written — see [`docs/runbooks/README.md`](../runbooks/README.md).**

> **status:** archived
> **owner:** Dan Browne
> **updated:** 2026-07-28
> **superseded-by:** `infra/runbooks/ecs-fargate-cutover.md`

> **Status (historical, as written 2026-06-24):** Written after the AWS account migration
> + the blank-UI / CloudFront-502 fix. It was a living doc until the 2026-07-09 Fargate
> cutover; it is no longer maintained and no longer describes the running system.
>
> **Primary deploy path is GitHub Actions** (`.github/workflows/deploy.yml`,
> OIDC + SSM SendCommand, gated by `vars.DEPLOY_ENABLED == 'true'`). Once that
> workflow is re-pointed at this account and re-enabled, **merging to `main`
> deploys automatically and you should not need anything below.** This runbook
> documents the **manual / break-glass** path for when CI is down, the change
> is pre-merge, or you need to touch infra directly. It is a backup capability,
> not the day-to-day flow.

---

## 0. The live architecture (what you're deploying into)

```
client ──HTTPS──▶ CloudFront (ACM cert, us-east-1, dist E34KG22GWPO075)
                   │  Compress: true  (single brotli/gzip compressor)
                   │  behaviors: default(HTML) /api/* /events/* /assets/* /static/* *.js *.css
                   ▼
                 ALB (archimedes-alb, HTTPS:443 ACM)  ──HTTP:80──▶  EC2:80
                   │  HTTP:80 listener → 301 redirect to 443                │
                   │  target group archimedes-backend-tg = HTTP:80         ▼
                   │                                              docker compose stack
                   │                                              nginx:8080 (plain HTTP)
                   ▼                                                 ├─ / + /assets  (static, gzip OFF)
              TLS TERMINATES HERE (CloudFront + ALB)                 ├─ /api/  → backend:8000
              nginx terminates NOTHING — plain HTTP only            ├─ /health→ backend:8000
                                                                     backend / postgres / redis /
                                                                     oracle / agent / kb-runner
```

**Load-bearing facts (learned the hard way — do not "fix" these):**

- **nginx serves plain HTTP on :8080 only.** TLS is terminated upstream at
  CloudFront and the ALB. There is no `:8443` listener, no in-container cert,
  no HTTP→HTTPS redirect in nginx (CloudFront's `redirect-to-https` viewer
  policy does that). A redirect in nginx would loop.
- **gzip is OFF for the static `location /`.** CloudFront is the single
  compressor. If nginx also gzips static assets it emits them chunked with no
  `Content-Length`, and **CloudFront returns 502 on the larger chunked-gzip
  responses.** Static assets must ship from disk with a `Content-Length` so
  CloudFront can brotli them at the edge. (Proxied `/api/` JSON keeps gzip.)
- **Every CloudFront cache behavior must attach `origin_request_policy_id =
  all_viewer`.** A behavior missing it 502s on its objects regardless of size
  (a 938-byte JS failed identically to the 1.5 MB bundle). All 7 behaviors now
  have it; keep it that way.
- **The ALB target group has always been plain `HTTP:80`** (`archimedes-backend-tg`).
  Do not point it at HTTPS/8443.

### Key resource IDs

| Thing | Value |
| --- | --- |
| AWS account / region | `037613907429` / `us-east-1` |
| CLI profile (SSO) | `ArchimedesDanAdmin` |
| EC2 instance | `i-01803d3abc271d39b` |
| App dir on box | `/opt/archimedes` (git clone, `docker compose` stack) |
| ALB | `archimedes-alb` · DNS `archimedes-alb-656113319.us-east-1.elb.amazonaws.com` |
| ALB ARN | `arn:aws:elasticloadbalancing:us-east-1:037613907429:loadbalancer/app/archimedes-alb/955aeff03e643d11` |
| Target group | `archimedes-backend-tg` (HTTP:80) · `.../targetgroup/archimedes-backend-tg/d2fc0d779cb1e9d4` |
| CloudFront distribution | `E34KG22GWPO075` |
| ALB ACM cert (us-east-1) | `arn:aws:acm:us-east-1:037613907429:certificate/b9d46feb-14bc-447a-a6c0-17a07035d5e6` |
| Domain / hosted zone | `archimedes-arc.com` |
| Repo | `github.com/a-apin/archimedes` |
| Frontend build arg | `VITE_CIRCLE_CLIENT_KEY` (sourced from `/opt/archimedes/.env` on the box) |

---

## 1. Prerequisites (every manual session)

```bash
# 1. Authenticate — the SSO token EXPIRES between sessions.
aws sso login --profile ArchimedesDanAdmin

# 2. Use the archimedes conda env for aws / terraform.
#    (awscli v2 + terraform are installed in this env.)
export AWS_PROFILE=ArchimedesDanAdmin

# 3. Smoke-test creds.
conda run -n archimedes aws sts get-caller-identity --profile ArchimedesDanAdmin --region us-east-1
```

**zsh gotcha:** zsh does **not** word-split unquoted variables. Do NOT do
`RG="--region us-east-1"; aws ... $RG` — it passes `--region us-east-1` as a
single token and fails with "Unknown options". **Inline flags literally**, or
quote-and-expand each one. Always pass `--profile ArchimedesDanAdmin --region
us-east-1` explicitly (a bare `aws` with no profile → `NoCredentials`).

We reach the box via **SSM SendCommand** (no SSH, no port 22). No key pair
needed — the EC2 instance profile grants `AmazonSSMManagedInstanceCore`.

---

## 2. Deploy the app (nginx/UI + backend) — manual

The app is a `docker compose` stack in `/opt/archimedes`. A deploy = get the
new code/config onto the box, rebuild the affected image(s), `up -d`.

### 2a. Preferred: git-based deploy via SSM

Use when the change is **merged (or on a branch the box can fetch)**.

```bash
export AWS_PROFILE=ArchimedesDanAdmin
KEY=$(conda run -n archimedes aws ssm get-parameter --region us-east-1 \
        --name /archimedes/prod/vite_circle_client_key --with-decryption \
        --query Parameter.Value --output text 2>/dev/null || echo "FROM_DOTENV")

cat > /tmp/deploy.json <<JSON
{ "commands": [
  "set -e",
  "cd /opt/archimedes",
  "git fetch --all --prune",
  "git checkout main && git pull --ff-only",
  "docker compose build --build-arg VITE_CIRCLE_CLIENT_KEY=\$(grep -E '^VITE_CIRCLE_CLIENT_KEY=' .env | cut -d= -f2-) nginx backend",
  "docker compose up -d",
  "sleep 6",
  "docker compose ps",
  "curl -s -o /dev/null -w 'local / -> %{http_code}\\n' http://localhost/",
  "curl -s -o /dev/null -w 'local /assets js -> %{http_code} (CL present?)\\n' http://localhost/assets/$(ls /opt/archimedes 2>/dev/null >/dev/null; echo index)*.js 2>/dev/null || true"
] }
JSON

CMD_ID=$(conda run -n archimedes aws ssm send-command --region us-east-1 \
  --instance-ids i-01803d3abc271d39b --document-name AWS-RunShellScript \
  --comment "manual git deploy" --timeout-seconds 900 \
  --parameters file:///tmp/deploy.json --query Command.CommandId --output text)
echo "CMD_ID=$CMD_ID"

# Wait + read result:
conda run -n archimedes aws ssm wait command-executed --region us-east-1 \
  --command-id "$CMD_ID" --instance-id i-01803d3abc271d39b || true
conda run -n archimedes aws ssm get-command-invocation --region us-east-1 \
  --command-id "$CMD_ID" --instance-id i-01803d3abc271d39b \
  --query '{Status:Status,Out:StandardOutputContent,Err:StandardErrorContent}' --output text
```

> **Note on the Vite build arg:** `VITE_CIRCLE_CLIENT_KEY` is embedded into the
> JS bundle **at build time** (Vite reads `VITE_*` during `npm run build`).
> The box `.env` holds it; `docker compose build` reads it via the `args:`
> interpolation. Passing `--build-arg` explicitly (as above) is belt-and-braces
> in case the `.env` interpolation is empty. After a key rotation you MUST
> rebuild the nginx image — a runtime env change does nothing to an already-
> built bundle.
>
> **Cache behavior:** if `ui/` and the build args are unchanged, Docker reuses
> the cached stage-1 (`npm ci` + `npm run build`) layers and the rebuild is
> seconds (only the `COPY nginx.conf` layer changes). Use `--no-cache` only
> when you actually need to re-run the UI build (e.g. a key rotation that must
> bust the embedded value).

### 2b. Break-glass: push changed files directly (pre-merge / CI down)

This is what was used to land the 502 fix live. Writes specific files to the
box via base64 (quoting-safe), then rebuilds. Use when the change isn't merged.

```bash
export AWS_PROFILE=ArchimedesDanAdmin
NGINX_B64=$(base64 < nginx/nginx.conf | tr -d '\n')
COMPOSE_B64=$(base64 < docker-compose.yml | tr -d '\n')
KEY=$(grep -E '^VITE_CIRCLE_CLIENT_KEY=' .env 2>/dev/null | cut -d= -f2-)   # or the known value

python3 - "$NGINX_B64" "$COMPOSE_B64" "$KEY" > /tmp/deploy.json <<'PY'
import json,sys
nb,cb,key=sys.argv[1],sys.argv[2],sys.argv[3]
cmds=[
 "set -e","cd /opt/archimedes",
 "cp nginx/nginx.conf nginx/nginx.conf.bak.$(date +%s) || true",
 "cp docker-compose.yml docker-compose.yml.bak.$(date +%s) || true",
 f"echo {nb} | base64 -d > nginx/nginx.conf",
 f"echo {cb} | base64 -d > docker-compose.yml",
 f"docker compose build --build-arg VITE_CIRCLE_CLIENT_KEY={key} nginx",
 "docker compose up -d nginx",
 "sleep 6","docker compose ps nginx",
 "curl -s -o /dev/null -D - http://localhost/assets/$(ls /opt/archimedes/ >/dev/null; echo)index*.js 2>/dev/null | grep -iE 'HTTP/|content-length|content-encoding' || true",
]
print(json.dumps({"commands":cmds}))
PY
# send-command + wait + get-command-invocation exactly as in 2a.
```

Backups are written as `nginx.conf.bak.<epoch>` / `docker-compose.yml.bak.<epoch>`
on the box — `cp` one back and rebuild to roll back.

### 2c. Invalidate CloudFront (required after any UI/asset change)

CloudFront caches assets (and caches error responses). After a UI rebuild you
**must** invalidate or users keep the stale/error object:

```bash
export AWS_PROFILE=ArchimedesDanAdmin
INV=$(conda run -n archimedes aws cloudfront create-invalidation \
  --distribution-id E34KG22GWPO075 --paths "/*" --query Invalidation.Id --output text)
conda run -n archimedes aws cloudfront wait invalidation-completed \
  --distribution-id E34KG22GWPO075 --id "$INV"
```

---

## 3. Deploy infrastructure changes — Terraform

For anything in `infra/*.tf` (ALB, CloudFront behaviors, WAF, Aurora, IAM, …).

```bash
cd infra
aws sso login --profile ArchimedesDanAdmin   # if token expired
export AWS_PROFILE=ArchimedesDanAdmin
conda run -n archimedes terraform fmt
conda run -n archimedes terraform init        # first time / backend change
conda run -n archimedes terraform plan -out tf.plan
# READ THE PLAN. Then:
conda run -n archimedes terraform apply tf.plan
```

State is the S3 backend `archimedes-tfstate-037613907429` (us-east-1,
`use_lockfile = true`). Never commit `*.tfstate`.

> **Live-vs-Terraform drift:** the 502 fix was applied **live first**
> (`aws cloudfront update-distribution` + invalidation) to restore the site
> immediately, then the same change was committed to `infra/cloudfront.tf`. If
> you ever hot-patch CloudFront/ALB live to fight a fire, **back-port it into
> Terraform in the same session** or the next `apply` reverts it. Run
> `terraform plan` after any live hot-patch to see the drift and reconcile.

---

## 4. Verification checklist (every deploy)

```bash
export AWS_PROFILE=ArchimedesDanAdmin
# Target healthy?
conda run -n archimedes aws elbv2 describe-target-health --region us-east-1 \
  --target-group-arn arn:aws:elasticloadbalancing:us-east-1:037613907429:targetgroup/archimedes-backend-tg/d2fc0d779cb1e9d4 \
  --query 'TargetHealthDescriptions[].TargetHealth.State' --output text   # → healthy

# Through CloudFront (the real user path):
curl -s -o /dev/null -w '/        -> %{http_code}\n' https://archimedes-arc.com/
curl -s -o /dev/null -w '/health  -> %{http_code}\n' https://archimedes-arc.com/health
curl -s -o /dev/null -w 'asset.js -> %{http_code} enc=%{content_type}\n' \
  -H 'Accept-Encoding: gzip, br' https://archimedes-arc.com/assets/<hashed>.js   # → 200, br
```

Then **load the site in a browser** and confirm React mounts (the app shell +
left nav render, not a blank page) and the console has **zero errors**. The
Circle "Connect Wallet" button rendering is the signal the embedded
`VITE_CIRCLE_CLIENT_KEY` is present and the SDK didn't crash the app.

If `/` is 200 but `/assets/*` is 502: re-check (a) nginx `gzip off` in the
static `location /`, and (b) every CloudFront behavior has
`origin_request_policy_id = all_viewer`. Those are the two failure modes that
produced the blank page on 2026-06-24.

---

## 5. Primary path (for completeness): GitHub Actions

`.github/workflows/deploy.yml` deploys on push to `main` when
`vars.DEPLOY_ENABLED == 'true'`. It assumes an AWS role via **OIDC** (no
long-lived keys) and runs an **SSM SendCommand** against the instance — the
same mechanism as §2, just automated. Outstanding work before it's the live
path again: re-point the workflow's account/region/instance to this account
(`037613907429` / `us-east-1` / `i-01803d3abc271d39b`), confirm the OIDC role
+ trust policy, and flip `DEPLOY_ENABLED=true`. Until then, use §2.
