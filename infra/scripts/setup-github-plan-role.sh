#!/usr/bin/env bash
# Archimedes — the READ-ONLY role .github/workflows/terraform-drift.yml assumes.
#
# Issue #1799. `terraform plan` is the only instrument that can tell whether
# `infra/` still describes production, and CI could not run one because nothing
# in this account lets GitHub Actions read state. This script creates the one
# thing that does: `archimedes-github-plan`, which can read and cannot write.
#
# ─── WHY NOT JUST WIDEN archimedes-github-deploy ────────────────────────────
# Two independent blockers, and neither is a missing policy statement:
#
#   1. TRUST. setup-github-oidc.sh pins the deploy role to
#      `...:ref:refs/heads/main`. A `pull_request` run mints
#      `...:pull_request` as its OIDC subject and cannot match. Adding
#      `pull_request` to THAT role's trust policy would let any pull-request
#      branch — including one that edits the workflow that assumes it — push to
#      ECR and `ssm:SendCommand` into production. The drift gate is not worth
#      that.
#   2. PERMISSIONS. The deploy role's inline policy is ECR push, SSM
#      SendCommand, `ec2:DescribeInstances`, and the docs-site S3/CloudFront
#      pair. A plan of `infra/` refreshes ~70 resources across ~25 services and
#      reads s3://archimedes-tfstate-037613907429. It has none of that.
#
# So: a second role, read-only, trusted from pull requests AND main. The two
# roles never overlap — this one holds no write permission of any kind, and the
# deploy role's trust policy is not touched by this script.
#
# ─── WHAT THIS GRANTS, AND WHAT THAT HONESTLY MEANS ─────────────────────────
#   * The AWS-managed `ReadOnlyAccess` policy. Verified against the live policy
#     document on 2026-09-03: it covers every service this root touches —
#     ec2, elasticloadbalancing, ecs, ecr, rds, elasticache, elasticfilesystem,
#     iam, logs, cloudwatch, events, scheduler, lambda, sns, wafv2, cloudfront,
#     route53, acm, application-autoscaling, autoscaling, budgets, ses, s3, ssm.
#     Enumerating those by hand instead would buy very little and would turn the
#     gate red the first time a new resource type lands in `infra/`.
#
#     Be clear-eyed about the size of it. Read from the live document on
#     2026-09-03: ReadOnlyAccess is three `Allow` statements on `Resource: "*"`
#     and contains no `Deny` of any kind. Two of its grants matter here —
#     `ssm:Get*` on EVERY parameter in the account, and `s3:Get*` on EVERY
#     object in every bucket.
#
#   * `kms:Decrypt`, restricted to the AWS-managed SSM key AND to calls that
#     arrive `ViaService: ssm.<region>.amazonaws.com`. This is the ONE gap in
#     ReadOnlyAccess that matters here: it grants `ssm:Get*` but deliberately
#     not `kms:Decrypt`, so `get-parameter --with-decryption` fails without it.
#     The workflow needs exactly one SecureString,
#     /archimedes/prod/AURORA_MASTER_PASSWORD, because `infra/outputs.tf`'s
#     `database_url` interpolates `var.aurora_master_password` and a wrong value
#     there is an output diff — a false drift report. (`aws_rds_cluster.main`
#     itself ignores `master_password`, so nothing is at risk of rotation.)
#
#   * TWO EXPLICIT `Deny`s, and they are the load-bearing part of this file.
#     Say plainly what the two bullets above add up to without them: `ssm:Get*`
#     on every parameter, PLUS `kms:Decrypt` on the key those parameters use,
#     is permission to read every SecureString in the account in cleartext. All
#     19 of them, enumerated 2026-09-03 and every one encrypted under
#     `alias/aws/ssm` — CIRCLE_ENTITY_SECRET, BETTER_AUTH_SECRET, DATABASE_URL,
#     EMAIL_ENCRYPTION_KEY, INTERNAL_AGENT_API_KEY, TIINGO_API_TOKEN,
#     GOOGLE_CLIENT_SECRET, WALLET_ADDRESS, and the rest. That is NOT a small
#     increment over "can read terraform state": the state file holds
#     `tls_private_key.deploy`'s private key and the `database_url` output, and
#     none of the other eighteen. And this role is assumable from
#     `repo:<org>/<repo>:pull_request` — from ANY in-repo pull-request branch,
#     including one that edits the workflow that assumes it.
#
#     A `Deny` beats an `Allow` from any policy, so a `NotResource` Deny in
#     this inline policy re-scopes the managed attachment without enumerating
#     it:
#       - ssm:GetParameter / GetParameters / GetParametersByPath /
#         GetParameterHistory on anything that is NOT the Aurora parameter.
#       - s3:GetObject / GetObjectVersion on anything that is NOT an object in
#         the terraform state bucket.
#     Neither costs the plan anything, and that is checked rather than assumed:
#     `infra/` declares no `aws_ssm_parameter` and no `aws_s3_object` (the SSM
#     ARNs in ecs.tf are `valueFrom` STRINGS handed to ECS, never read by
#     terraform), and both lambdas load their code from a local `filename`, not
#     from S3. Verified 2026-09-03 with `aws iam simulate-custom-policy` over
#     this document plus the live ReadOnlyAccess grant: the Aurora parameter,
#     the state object, the state lockfile and `s3:ListBucket` on the state
#     bucket all evaluate `allowed`; GetParameter on CIRCLE_ENTITY_SECRET /
#     BETTER_AUTH_SECRET / DATABASE_URL, GetParametersByPath on
#     /archimedes/prod, and GetObject on another bucket all evaluate
#     `explicitDeny`. Bucket-level reads (`s3:GetBucketPolicy`, `GetBucketAcl`)
#     stay `allowed`, so refreshing bucket configuration is untouched.
#
#     What survives, and is accepted: `ssm:Describe*` still lists parameter
#     NAMES and metadata account-wide (no values), and `s3:Get*`/`List*` still
#     read bucket CONFIGURATION account-wide. If even that becomes
#     unacceptable, replace the managed attachment with an explicit per-service
#     policy; nothing else in the workflow changes.
#
#   * An explicit read grant on the state bucket. Redundant with
#     ReadOnlyAccess today, written out anyway so that tightening the
#     attachment above does not silently break `terraform init` — and it is
#     what the s3 Deny's `NotResource` is carved around.
#
# NOT granted, on purpose: any `s3:Put*`/`s3:Delete*` on the state bucket. The
# workflow passes `-lock=false`, and the backend's S3-native locking
# (`use_lockfile = true`, infra/main.tf) is the only thing that would have
# needed a write. A role that cannot write cannot corrupt state, and a
# scheduled plan cannot wedge an operator's concurrent apply.
#
# ─── OPERATOR RECIPE ────────────────────────────────────────────────────────
#
#     AWS_PROFILE=ArchimedesDanAdmin bash infra/scripts/setup-github-plan-role.sh
#     AWS_PROFILE=ArchimedesDanAdmin bash infra/scripts/setup-github-plan-role.sh --apply
#
# Then, in the repository settings:
#
#     gh variable set TF_PLAN_ROLE_ARN --body "arn:aws:iam::<acct>:role/archimedes-github-plan"
#     gh secret   set TF_VAR_ALARM_EMAIL --body "<the address subscribed to archimedes-alerts>"
#     gh variable set TF_DRIFT_ENABLED --body "true"     # arm it LAST
#
# Re-run this script after ANY repo rename, org rename, or transfer — the OIDC
# `sub` claim carries the repository's current identity, exactly as documented
# at length in setup-github-oidc.sh (read its "THE SUBJECT GITHUB ACTUALLY
# MINTS" section before editing subject handling here; this script uses the
# same resolution and the same both-forms trust).
#
# DRY-RUN BY DEFAULT. Requires: AWS_PROFILE exported, aws CLI v2. `gh`
# (authenticated) strongly recommended — without it the sub prefix falls back
# to the plain form.
set -euo pipefail

# ─── Config ──────────────────────────────────────────────────────────────────
ACCOUNT_ID="${ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)}"
REGION="${AWS_REGION:-us-east-1}"
GITHUB_ORG="${GITHUB_ORG:-aprin-labs}"
GITHUB_REPO="${GITHUB_REPO:-archimedes}"
PLAIN_SUB_PREFIX="repo:${GITHUB_ORG}/${GITHUB_REPO}"
SUB_CLAIM_PREFIX="${SUB_CLAIM_PREFIX:-}"
ROLE_NAME="archimedes-github-plan"
OIDC_URL="token.actions.githubusercontent.com"
OIDC_ARN="arn:aws:iam::${ACCOUNT_ID}:oidc-provider/${OIDC_URL}"
STATE_BUCKET="${STATE_BUCKET:-archimedes-tfstate-${ACCOUNT_ID}}"
AURORA_PARAM="/archimedes/prod/AURORA_MASTER_PASSWORD"
READONLY_POLICY_ARN="arn:aws:iam::aws:policy/ReadOnlyAccess"
# The four ref shapes the workflow can run under:
#   pull_request            -> :pull_request
#   push to main            -> :ref:refs/heads/main
#   schedule                -> :ref:refs/heads/main
#   workflow_dispatch(main) -> :ref:refs/heads/main
# Nothing else. A branch push, a tag, or a dispatch from another branch cannot
# assume this role.
SUB_SUFFIXES=("pull_request" "ref:refs/heads/main")
# ─────────────────────────────────────────────────────────────────────────────

APPLY=false; for a in "$@"; do case "$a" in
  --apply) APPLY=true;; -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
  *) echo "unknown arg: $a" >&2; exit 2;; esac; done
say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
do_() { printf '  + %s\n' "$*"; if $APPLY; then eval "$*"; fi; }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
[ -n "$ACCOUNT_ID" ] || { echo "ERROR: could not resolve ACCOUNT_ID (set AWS_PROFILE / export ACCOUNT_ID)"; exit 1; }
$APPLY && echo ">>> APPLY MODE — account ${ACCOUNT_ID}" || echo ">>> DRY RUN — re-run with --apply to execute."

# ─── 0. The OIDC provider must already exist ─────────────────────────────────
# This script deliberately does not create it: setup-github-oidc.sh owns that
# resource, and two scripts racing to create one provider is how thumbprint
# lists diverge.
say "GitHub OIDC identity provider (owned by setup-github-oidc.sh)"
if aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$OIDC_ARN" >/dev/null 2>&1; then
  echo "  exists: $OIDC_ARN"
else
  echo "ERROR: no OIDC provider at ${OIDC_ARN}." >&2
  echo "       Run infra/scripts/setup-github-oidc.sh --apply first — it owns that resource." >&2
  exit 1
fi

# ─── 1. Repo identity cross-check (same rationale as setup-github-oidc.sh) ───
say "Repo identity cross-check"
if command -v gh >/dev/null 2>&1 && ACTUAL_REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null)" && [ -n "$ACTUAL_REPO" ]; then
  if [ "$ACTUAL_REPO" = "${GITHUB_ORG}/${GITHUB_REPO}" ]; then
    echo "  ok: GitHub reports ${ACTUAL_REPO}, matching the configured trust subject"
  else
    echo "ERROR: this script would trust 'repo:${GITHUB_ORG}/${GITHUB_REPO}:...', but GitHub" >&2
    echo "       reports the repository as '${ACTUAL_REPO}'. That policy could never match." >&2
    exit 1
  fi
else
  echo "  skipped: gh unavailable or unauthenticated — cannot confirm ${GITHUB_ORG}/${GITHUB_REPO}"
fi

# ─── 2. Resolve the sub prefix GitHub mints for THIS repo ────────────────────
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
    echo "  !! WARNING: could not read the sub prefix from GitHub. Falling back to the PLAIN form:"
    echo "  !!     ${PLAIN_SUB_PREFIX}"
    echo "  !! If this repository mints the ID-QUALIFIED form the fallback CANNOT match and the"
    echo "  !! drift job dies at 'Configure AWS credentials (OIDC)'. Read the real subject from"
    echo "  !! CloudTrail (AssumeRoleWithWebIdentity -> userIdentity.userName) or from"
    echo "  !!     gh api repos/${GITHUB_ORG}/${GITHUB_REPO}/actions/oidc/customization/sub"
    echo "  !! and re-run with SUB_CLAIM_PREFIX=<that value>."
  } >&2
fi

# Same validation as setup-github-oidc.sh: a StringLike match means a stray '*'
# would silently widen who may assume this role.
_bad_prefix() {
  echo "ERROR: refusing to write a trust policy for sub prefix '${SUB_CLAIM_PREFIX}'." >&2
  echo "       $1" >&2
  exit 1
}
case "$SUB_CLAIM_PREFIX" in repo:*) ;; *) _bad_prefix "It does not start with 'repo:'." ;; esac
_sub_body="${SUB_CLAIM_PREFIX#repo:}"
case "$_sub_body" in
  "")    _bad_prefix "It is empty after 'repo:'." ;;
  */*/*) _bad_prefix "It has more than one '/' — that is not an owner/name pair." ;;
  */*)   ;;
  *)     _bad_prefix "It has no '/' — that is not an owner/name pair." ;;
esac
case "$_sub_body" in
  *[!A-Za-z0-9._@/-]*) _bad_prefix "It contains a character outside [A-Za-z0-9._@/-] (a wildcard here would widen the trust policy)." ;;
esac
_org_seg="${_sub_body%%/*}"; _repo_seg="${_sub_body#*/}"
[ "${_org_seg%%@*}" = "$GITHUB_ORG" ]   || _bad_prefix "Its owner segment is '${_org_seg}', not '${GITHUB_ORG}'."
[ "${_repo_seg%%@*}" = "$GITHUB_REPO" ] || _bad_prefix "Its repo segment is '${_repo_seg}', not '${GITHUB_REPO}'."

PREFIXES=("$SUB_CLAIM_PREFIX")
[ "$SUB_CLAIM_PREFIX" = "$PLAIN_SUB_PREFIX" ] || PREFIXES+=("$PLAIN_SUB_PREFIX")
SUBJECTS=()
for _p in "${PREFIXES[@]}"; do for _s in "${SUB_SUFFIXES[@]}"; do SUBJECTS+=("${_p}:${_s}"); done; done
echo "  subjects this run will trust:"
for _s in "${SUBJECTS[@]}"; do echo "    - ${_s}"; done
SUB_JSON=""
for _s in "${SUBJECTS[@]}"; do SUB_JSON="${SUB_JSON:+${SUB_JSON},}\"${_s}\""; done

# ─── 3. The role ─────────────────────────────────────────────────────────────
say "Plan role: ${ROLE_NAME} (${#SUBJECTS[@]} trusted subject(s))"
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
  do_ "aws iam create-role --role-name $ROLE_NAME --assume-role-policy-document file://$TMP/trust.json --description 'GitHub Actions terraform plan (OIDC, read-only, issue #1799)'"
fi

# ─── 4. ReadOnlyAccess ───────────────────────────────────────────────────────
say "Attach ${READONLY_POLICY_ARN}"
do_ "aws iam attach-role-policy --role-name $ROLE_NAME --policy-arn $READONLY_POLICY_ARN"

# ─── 5. What ReadOnlyAccess misses, and what it over-grants ──────────────────
# Misses: kms:Decrypt. Over-grants: ssm:Get* on every parameter and s3:GetObject
# on every object. The two Denys below are what keep the pairing from becoming
# "decrypt every SecureString in the account" — read the WHAT THIS GRANTS
# section at the top of this file before touching either of them.
#
# The SSM KMS key is resolved rather than hardcoded: it is account- and
# region-specific, and a wrong ARN here fails at plan time with an opaque
# AccessDenied on a parameter read.
say "Inline policy: state-bucket read, the one SecureString, and the two Denys"
SSM_KEY_ARN="$(aws kms describe-key --key-id alias/aws/ssm --query KeyMetadata.Arn --output text 2>/dev/null || true)"
if [ -z "$SSM_KEY_ARN" ] || [ "$SSM_KEY_ARN" = "None" ]; then
  echo "ERROR: could not resolve the alias/aws/ssm KMS key in ${REGION}." >&2
  echo "       Without kms:Decrypt on it the drift workflow cannot read ${AURORA_PARAM}" >&2
  echo "       and every plan reports a false diff on the database_url output." >&2
  exit 1
fi
echo "  alias/aws/ssm resolves to ${SSM_KEY_ARN}"
cat > "$TMP/perms.json" <<JSON
{ "Version":"2012-10-17",
  "Statement":[
    {"Sid":"TerraformStateRead","Effect":"Allow",
     "Action":["s3:ListBucket","s3:GetBucketLocation"],
     "Resource":"arn:aws:s3:::${STATE_BUCKET}"},
    {"Sid":"TerraformStateObjectRead","Effect":"Allow",
     "Action":["s3:GetObject","s3:GetObjectVersion"],
     "Resource":"arn:aws:s3:::${STATE_BUCKET}/*"},
    {"Sid":"AuroraPasswordParameter","Effect":"Allow",
     "Action":["ssm:GetParameter"],
     "Resource":"arn:aws:ssm:${REGION}:${ACCOUNT_ID}:parameter${AURORA_PARAM}"},
    {"Sid":"AuroraPasswordDecrypt","Effect":"Allow",
     "Action":["kms:Decrypt"],
     "Resource":"${SSM_KEY_ARN}",
     "Condition":{"StringEquals":{"kms:ViaService":"ssm.${REGION}.amazonaws.com"}}},
    {"Sid":"DenyEveryOtherParameter","Effect":"Deny",
     "Action":["ssm:GetParameter","ssm:GetParameters","ssm:GetParametersByPath","ssm:GetParameterHistory"],
     "NotResource":"arn:aws:ssm:${REGION}:${ACCOUNT_ID}:parameter${AURORA_PARAM}"},
    {"Sid":"DenyEveryOtherObjectRead","Effect":"Deny",
     "Action":["s3:GetObject","s3:GetObjectVersion"],
     "NotResource":"arn:aws:s3:::${STATE_BUCKET}/*"}
  ] }
JSON
$APPLY || { echo "  inline policy that would be written:"; sed 's/^/    /' "$TMP/perms.json"; }
do_ "aws iam put-role-policy --role-name $ROLE_NAME --policy-name archimedes-plan-extra --policy-document file://$TMP/perms.json"

say "Role ARN for the TF_PLAN_ROLE_ARN repository VARIABLE (not a secret):"
echo "  arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"
cat <<TXT

  gh variable set TF_PLAN_ROLE_ARN --body "arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"
  gh secret   set TF_VAR_ALARM_EMAIL --body "<address subscribed to the archimedes-alerts SNS topic>"
  gh variable set TF_DRIFT_ENABLED --body "true"          # arm the workflow LAST

  Then: Actions -> terraform-drift -> Run workflow, and read the plan artifact.
TXT
$APPLY || echo "(dry run — re-run with --apply to create the above)"
