#!/usr/bin/env bash
# Archimedes — GitHub Actions -> AWS auth via OIDC (no long-lived keys).
#
# Why: CI/CD should assume a short-lived role via OpenID Connect, NOT store an
# AWS_ACCESS_KEY_ID/SECRET in GitHub secrets. The only thing GitHub holds is the
# role ARN (printed at the end) — not a secret. App runtime secrets live in SSM
# Parameter Store (see setup-ssm-secrets.sh), never in GitHub.
#
# What this creates:
#   1. An IAM OIDC identity provider for token.actions.githubusercontent.com
#   2. A deploy role whose trust policy ONLY accepts THIS repository on
#      refs/heads/main (build-on-deploy), via sts:AssumeRoleWithWebIdentity.
#      "This repository" is not a string we get to choose — read THE SUBJECT
#      GITHUB ACTUALLY MINTS below before editing anything here.
#   3. A STARTER permissions policy (ECR push + SSM SendCommand + EC2 describe),
#      plus the docs-site publish grants (#1634): sync into the
#      docs-site/infra bucket and invalidate its CloudFront distribution.
#      >>> Tighten the Resource ARNs once the ECR repo + instance/ECS service exist. <<<
#
#   NOTE: the docs-site grants must be applied (re-run this script with --apply)
#   before .github/workflows/docs-site.yml can publish. Until then its publish
#   steps fail on AccessDenied — the site keeps building, nothing goes live.
#   `cloudfront:ListDistributions` cannot be resource-scoped (it is a list call,
#   Resource "*" is the only valid form); `CreateInvalidation` is scoped to this
#   account's distributions.
#
# ─── THE SUBJECT GITHUB ACTUALLY MINTS (incident 2026-09-01) ─────────────────
# The trust policy matches the OIDC token's `sub` claim. GitHub mints that
# claim; we only get to match it. It has TWO possible shapes:
#
#     repo:ORG/REPO:ref:refs/heads/main                      <- plain
#     repo:ORG@<org-id>/REPO@<repo-id>:ref:refs/heads/main   <- id-qualified
#
# The id-qualified ("immutable subject") form names the numeric org and repo
# IDs, so it survives renames. Which form a repository gets is a GitHub-side
# setting, and the authoritative answer is the `sub_claim_prefix` field of
#
#     gh api repos/ORG/REPO/actions/oidc/customization/sub
#
# so this script ASKS rather than assumes, and writes BOTH forms into the trust
# policy. Trusting both means a GitHub-side toggle in either direction cannot
# break deploys; both are still pinned to refs/heads/main, so main-only remains
# the rule and no other branch or repo gains anything.
#
# WHAT WENT WRONG. After the org rename a-apin -> aprin-labs, every production
# deploy from 16:55Z to 20:04Z died at "Configure AWS credentials (OIDC)" with
# `Not authorized to perform sts:AssumeRoleWithWebIdentity`. The trust policy
# had already been corrected to the *plain* new name
# (repo:aprin-labs/archimedes:ref:refs/heads/main) — and still failed, because
# this repository mints the id-qualified form:
#
#     repo:aprin-labs@284008417/archimedes@1236816811:ref:refs/heads/main
#
# HOW TO SEE THE REAL SUBJECT (do this first, next time). CloudTrail, event
# name AssumeRoleWithWebIdentity, errorCode AccessDenied: the subject GitHub
# presented is `userIdentity.userName` on the failed event. That field is the
# ground truth — compare it, character for character, against the subjects this
# script prints in its dry run.
#
# OPERATOR RECIPE — after ANY repo rename, org rename, or transfer:
#
#     AWS_PROFILE=<admin-profile> bash infra/scripts/setup-github-oidc.sh
#     AWS_PROFILE=<admin-profile> bash infra/scripts/setup-github-oidc.sh --apply
#
# (dry run first: it prints the exact subjects it will trust). Nothing else
# re-points the role — `archimedes-github-deploy` is deliberately NOT
# Terraform-managed (infra/ecs.tf), so this script is the source of truth.
# Git keeps working throughout, because GitHub redirects clones and STS does
# not, so deploys are the only thing that breaks — silently, before a byte is
# built.
#
# DRY-RUN BY DEFAULT.  ./setup-github-oidc.sh   |   ./setup-github-oidc.sh --apply
# Requires: AWS_PROFILE exported, aws CLI v2. `gh` (authenticated) strongly
# recommended — without it the sub prefix falls back to the plain form.
set -euo pipefail

# ─── Config ──────────────────────────────────────────────────────────────────
# ACCOUNT_ID/REGION are not hardcoded — auto-detected from the active profile (override via env).
ACCOUNT_ID="${ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)}"
REGION="${AWS_REGION:-us-east-1}"
# The OIDC `sub` claim carries the repository's CURRENT identity. Renaming or
# transferring the repo changes the claim, the trust policy below stops
# matching, and every deploy dies at "Configure AWS credentials (OIDC)".
# So: re-run this script after any rename. Override via env for a fork.
GITHUB_ORG="${GITHUB_ORG:-aprin-labs}"
GITHUB_REPO="${GITHUB_REPO:-archimedes}"
DEPLOY_REF="refs/heads/main"                            # main-only stays the rule
PLAIN_SUB_PREFIX="repo:${GITHUB_ORG}/${GITHUB_REPO}"
# Escape hatch: set this only when `gh` is unavailable AND you have read the
# real prefix by hand (CloudTrail userIdentity.userName, minus the ":ref:..."
# suffix, or the customization/sub API). Validated below like any other input.
SUB_CLAIM_PREFIX="${SUB_CLAIM_PREFIX:-}"
ROLE_NAME="archimedes-github-deploy"
OIDC_URL="token.actions.githubusercontent.com"
OIDC_ARN="arn:aws:iam::${ACCOUNT_ID}:oidc-provider/${OIDC_URL}"
# GitHub's OIDC thumbprints (AWS no longer validates these for this provider, but the
# API still wants at least one). Both current values included for safety.
THUMBPRINTS="6938fd4d98bab03faadb97b34396831e3780aea1 1c58a3a8518e8759bf075b76b750d4f2df264fcd"
# ─────────────────────────────────────────────────────────────────────────────

APPLY=false; for a in "$@"; do case "$a" in
  --apply) APPLY=true;; -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
  *) echo "unknown arg: $a" >&2; exit 2;; esac; done
say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
do_() { printf '  + %s\n' "$*"; if $APPLY; then eval "$*"; fi; }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
[ -n "$ACCOUNT_ID" ] || { echo "ERROR: could not resolve ACCOUNT_ID (set AWS_PROFILE / export ACCOUNT_ID)"; exit 1; }
$APPLY && echo ">>> APPLY MODE — account ${ACCOUNT_ID}" || echo ">>> DRY RUN — re-run with --apply to execute."

# ─── 0. The trust policy must name the repo GitHub will actually mint tokens for ──
# A wrong org here is not a visible error: the role is written successfully and
# every later deploy fails at the credentials step instead. `gh` knows the
# canonical owner/name (a local git remote does not — it still resolves through
# GitHub's rename redirect), so cross-check against it and refuse to write a
# policy that cannot match. Skipped, not failed, when gh is missing or unauthed:
# an absent optional tool should not block a valid run.
say "Repo identity cross-check"
if command -v gh >/dev/null 2>&1 && ACTUAL_REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null)" && [ -n "$ACTUAL_REPO" ]; then
  if [ "$ACTUAL_REPO" = "${GITHUB_ORG}/${GITHUB_REPO}" ]; then
    echo "  ok: GitHub reports ${ACTUAL_REPO}, matching the configured trust subject"
  else
    echo "ERROR: this script would trust 'repo:${GITHUB_ORG}/${GITHUB_REPO}:...', but GitHub" >&2
    echo "       reports the repository as '${ACTUAL_REPO}'." >&2
    echo "       The OIDC sub claim uses the name GitHub reports, so that policy could never" >&2
    echo "       match and every deploy would fail at 'Configure AWS credentials (OIDC)'." >&2
    echo "       Fix GITHUB_ORG/GITHUB_REPO (or export them) and re-run." >&2
    exit 1
  fi
else
  echo "  skipped: gh unavailable or unauthenticated — cannot confirm ${GITHUB_ORG}/${GITHUB_REPO}"
fi

# ─── 0b. Resolve the sub prefix GitHub mints for THIS repo ───────────────────
# The name is only half the subject: the prefix may be plain or id-qualified
# (see the header). Ask GitHub; fall back loudly. Never guess quietly.
say "OIDC subject prefix"
if [ -n "$SUB_CLAIM_PREFIX" ]; then
  echo "  using SUB_CLAIM_PREFIX from the environment: ${SUB_CLAIM_PREFIX}"
elif command -v gh >/dev/null 2>&1 \
     && SUB_CLAIM_PREFIX="$(gh api "repos/${GITHUB_ORG}/${GITHUB_REPO}/actions/oidc/customization/sub" --jq .sub_claim_prefix 2>/dev/null)" \
     && [ -n "$SUB_CLAIM_PREFIX" ] && [ "$SUB_CLAIM_PREFIX" != "null" ]; then
  echo "  GitHub reports sub_claim_prefix = ${SUB_CLAIM_PREFIX}"
else
  SUB_CLAIM_PREFIX="$PLAIN_SUB_PREFIX"
  {
    echo "  !! WARNING: could not read the sub prefix from GitHub (gh missing,"
    echo "  !! unauthenticated, or the API call failed). Falling back to the PLAIN form:"
    echo "  !!     ${PLAIN_SUB_PREFIX}"
    echo "  !! If this repository mints the ID-QUALIFIED form"
    echo "  !! (repo:ORG@<org-id>/REPO@<repo-id>) that fallback CANNOT match, and every"
    echo "  !! deploy will fail at 'Configure AWS credentials (OIDC)'. Read the real"
    echo "  !! subject from CloudTrail (AssumeRoleWithWebIdentity -> userIdentity.userName)"
    echo "  !! or from"
    echo "  !!     gh api repos/${GITHUB_ORG}/${GITHUB_REPO}/actions/oidc/customization/sub"
    echo "  !! and re-run with SUB_CLAIM_PREFIX=<that value>."
  } >&2
fi

# Validate before it reaches a trust policy. A prefix is matched with StringLike,
# so a stray '*' would silently widen who may assume this role; anything that is
# not this repository must not be written at all.
_bad_prefix() {
  echo "ERROR: refusing to write a trust policy for sub prefix '${SUB_CLAIM_PREFIX}'." >&2
  echo "       $1" >&2
  echo "       Expected 'repo:${GITHUB_ORG}/${GITHUB_REPO}' or the id-qualified" >&2
  echo "       'repo:${GITHUB_ORG}@<org-id>/${GITHUB_REPO}@<repo-id>'." >&2
  exit 1
}
case "$SUB_CLAIM_PREFIX" in
  repo:*) ;;
  *) _bad_prefix "It does not start with 'repo:'." ;;
esac
_sub_body="${SUB_CLAIM_PREFIX#repo:}"
case "$_sub_body" in
  "")            _bad_prefix "It is empty after 'repo:'." ;;
  */*/*)         _bad_prefix "It has more than one '/' — that is not an owner/name pair." ;;
  */*)           ;;
  *)             _bad_prefix "It has no '/' — that is not an owner/name pair." ;;
esac
case "$_sub_body" in
  *[!A-Za-z0-9._@/-]*) _bad_prefix "It contains a character outside [A-Za-z0-9._@/-] (a wildcard here would widen the trust policy)." ;;
esac
# Strip any '@<id>' qualifier from each segment, then the names must be OURS.
_org_seg="${_sub_body%%/*}"; _repo_seg="${_sub_body#*/}"
[ "${_org_seg%%@*}" = "$GITHUB_ORG" ] || _bad_prefix "Its owner segment is '${_org_seg}', not '${GITHUB_ORG}'."
[ "${_repo_seg%%@*}" = "$GITHUB_REPO" ] || _bad_prefix "Its repo segment is '${_repo_seg}', not '${GITHUB_REPO}'."

# Trust the resolved prefix AND the plain one (deduped when equal), both pinned
# to main: a GitHub-side toggle of the immutable-subject format in either
# direction then cannot break deploys.
SUBJECTS=("${SUB_CLAIM_PREFIX}:ref:${DEPLOY_REF}")
[ "$SUB_CLAIM_PREFIX" = "$PLAIN_SUB_PREFIX" ] || SUBJECTS+=("${PLAIN_SUB_PREFIX}:ref:${DEPLOY_REF}")
echo "  subjects this run will trust (main only) — compare these character for"
echo "  character against CloudTrail userIdentity.userName on a failed"
echo "  AssumeRoleWithWebIdentity event:"
for _s in "${SUBJECTS[@]}"; do echo "    - ${_s}"; done
SUB_JSON=""
for _s in "${SUBJECTS[@]}"; do SUB_JSON="${SUB_JSON:+${SUB_JSON},}\"${_s}\""; done

# ─── 1. OIDC provider ─────────────────────────────────────────────────────────
say "GitHub OIDC identity provider"
if aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$OIDC_ARN" >/dev/null 2>&1; then
  echo "  exists: $OIDC_ARN"
else
  do_ "aws iam create-open-id-connect-provider --url https://${OIDC_URL} --client-id-list sts.amazonaws.com --thumbprint-list ${THUMBPRINTS}"
fi

# ─── 2. Deploy role (trust scoped to main branch only) ────────────────────────
say "Deploy role: ${ROLE_NAME} (${#SUBJECTS[@]} trusted subject(s), main only)"
cat > "$TMP/trust.json" <<JSON
{ "Version":"2012-10-17",
  "Statement":[{
    "Effect":"Allow",
    "Principal":{"Federated":"${OIDC_ARN}"},
    "Action":"sts:AssumeRoleWithWebIdentity",
    "Condition":{
      "StringEquals":{"${OIDC_URL}:aud":"sts.amazonaws.com"},
      "StringLike":{"${OIDC_URL}:sub":[${SUB_JSON}]}
    } }] }
JSON
$APPLY || { echo "  trust policy that would be written:"; sed 's/^/    /' "$TMP/trust.json"; }
if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  echo "  role exists — updating trust policy"
  do_ "aws iam update-assume-role-policy --role-name $ROLE_NAME --policy-document file://$TMP/trust.json"
else
  do_ "aws iam create-role --role-name $ROLE_NAME --assume-role-policy-document file://$TMP/trust.json --description 'GitHub Actions deploy (OIDC, main only)'"
fi

# ─── 3. STARTER permissions (TIGHTEN once ECR repo + deploy target exist) ─────
say "Deploy permissions (STARTER — scope Resources after the stack is up)"
cat > "$TMP/perms.json" <<JSON
{ "Version":"2012-10-17",
  "Statement":[
    {"Sid":"EcrAuth","Effect":"Allow","Action":["ecr:GetAuthorizationToken"],"Resource":"*"},
    {"Sid":"EcrPush","Effect":"Allow",
     "Action":["ecr:BatchCheckLayerAvailability","ecr:InitiateLayerUpload","ecr:UploadLayerPart",
               "ecr:CompleteLayerUpload","ecr:PutImage","ecr:BatchGetImage"],
     "Resource":"arn:aws:ecr:${REGION}:${ACCOUNT_ID}:repository/archimedes*"},
    {"Sid":"SsmDeploy","Effect":"Allow",
     "Action":["ssm:SendCommand","ssm:GetCommandInvocation","ssm:ListCommands","ssm:ListCommandInvocations"],
     "Resource":"*"},
    {"Sid":"Ec2Discover","Effect":"Allow","Action":["ec2:DescribeInstances"],"Resource":"*"},
    {"Sid":"DocsSiteBucket","Effect":"Allow",
     "Action":["s3:ListBucket"],
     "Resource":"arn:aws:s3:::archimedes-docs-site-${ACCOUNT_ID}"},
    {"Sid":"DocsSiteObjects","Effect":"Allow",
     "Action":["s3:GetObject","s3:PutObject","s3:DeleteObject"],
     "Resource":"arn:aws:s3:::archimedes-docs-site-${ACCOUNT_ID}/*"},
    {"Sid":"DocsSiteInvalidate","Effect":"Allow",
     "Action":["cloudfront:CreateInvalidation"],
     "Resource":"arn:aws:cloudfront::${ACCOUNT_ID}:distribution/*"},
    {"Sid":"DocsSiteFindDistribution","Effect":"Allow",
     "Action":["cloudfront:ListDistributions"],"Resource":"*"}
  ] }
JSON
do_ "aws iam put-role-policy --role-name $ROLE_NAME --policy-name archimedes-deploy --policy-document file://$TMP/perms.json"

say "Role ARN to put in GitHub (NOT a secret — a repo variable is fine):"
echo "  arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"
cat <<YAML

  # In .github/workflows/deploy.yml, replace stored AWS keys with:
  permissions:
    id-token: write   # required for OIDC
    contents: read
  steps:
    - uses: aws-actions/configure-aws-credentials@v4
      with:
        role-to-assume: arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}
        aws-region: ${REGION}
  # No AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY secrets needed.
YAML
$APPLY || echo "(dry run — re-run with --apply to create the above)"
