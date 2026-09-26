#!/usr/bin/env bash
#
# setup-branch-protection.sh — codify branch protection for `main` (audit #10 / issues #519, #526).
#
# WHY THIS EXISTS
#   `main` is currently unprotected, so every push auto-deploys to the live EC2 host
#   (build-on-deploy) with no human or status-check gate. This script declares the agreed
#   protection ruleset as code so a repo admin can apply it in one command — and re-apply
#   or audit it later. The PUT payload is declarative, so the script is idempotent:
#   re-running converges to the same state.
#
# THE t2o2 / build-on-deploy TRADEOFF  (the team-decision knob — read before applying)
#
#   ⚠ REVISIT (2026-08-03): the justification below is stale. The `t2o2` agent account is
#   dormant and nothing is being dispatched to it, so the direct-push exemption that
#   enforce_admins=false exists to preserve is no longer buying anything. Every human
#   admin still bypasses branch protection as a side effect. This should be re-decided,
#   not inherited. Tracked as a follow-up; not changed here because flipping it affects
#   live merge behaviour for the whole team.
#   The `t2o2` agentic user pushes directly to `main` (build-on-deploy is the accepted
#   workflow per CLAUDE.md). Branch protection would normally block that. We preserve it
#   with `enforce_admins=false`: repo *admins* (which includes t2o2) keep their direct-push
#   path, while every non-admin contributor is gated behind a passing CI + 1 approval.
#   If the team would rather gate t2o2 too, flip ENFORCE_ADMINS=true below — but then the
#   agentic system must switch to PR-based merges. This is Chuan's call as repo admin.
#
# USAGE
#   ./scripts/setup-branch-protection.sh            # dry-run: print the payload + commands, apply nothing
#   ./scripts/setup-branch-protection.sh --apply    # apply the protection (needs admin on the repo)
#   ./scripts/setup-branch-protection.sh --verify   # print the currently-applied protection
#
# REQUIREMENTS: gh (authenticated, with admin scope for --apply), python3 (for pretty JSON).
set -euo pipefail

REPO="${REPO:-aprin-labs/archimedes}"
BRANCH="${BRANCH:-main}"

# enforce_admins=false → admins (incl. the t2o2 build-on-deploy user) bypass the gate.
# Set to "true" to gate everyone, including t2o2 (forces the agentic system onto PRs).
ENFORCE_ADMINS="${ENFORCE_ADMINS:-false}"

# Hard-block CI contexts from quality-gate.yml. The informational checks
# ("Lint — report table", "Complexity analysis", coverage) are deliberately NOT required.
#
# "Contracts — forge build + test" (.github/workflows/contracts-test.yml) is gated behind
# INCLUDE_CONTRACTS_CHECK (default "false", i.e. NOT included) because that workflow is
# scoped with `on.pull_request.paths: ["contracts/**", ...]`, so it never runs on a PR
# that doesn't touch contracts/. GitHub's classic required-status-checks model leaves a
# required check that never posts a status stuck at "Expected — waiting for status to be
# reported" FOREVER, which blocks merging every non-contract PR too (a documented GitHub
# limitation, not a bug in this script — see
# https://github.com/orgs/community/discussions/44490 and #54877). Flipping this flag on
# as-is would make every non-contract PR permanently unmergeable — do not do that. The
# correct remedy is a fallback job in contracts-test.yml that runs on ALL PRs (no path
# filter, or the inverse of contracts/**) and reports the SAME check name as a trivial
# pass when contracts/** is untouched; that fallback job is what should be required, not
# the path-filtered one. Once that job exists, set INCLUDE_CONTRACTS_CHECK=true here.
# Tracked as a follow-up; deliberately not fixed in this sweep (touching
# contracts-test.yml is CI/CD wiring that needs its own review).
INCLUDE_CONTRACTS_CHECK="${INCLUDE_CONTRACTS_CHECK:-false}"

CONTEXTS='"Backend — unit tests", "Ruff — format + critical lint rules"'
if [ "$INCLUDE_CONTRACTS_CHECK" = "true" ]; then
  CONTEXTS="${CONTEXTS}, \"Contracts — forge build + test\""
fi

read -r -d '' PAYLOAD <<JSON || true
{
  "required_status_checks": {
    "strict": false,
    "contexts": [${CONTEXTS}]
  },
  "enforce_admins": ${ENFORCE_ADMINS},
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": false,
    "require_code_owner_reviews": false,
    "required_approving_review_count": 1
  },
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_linear_history": false,
  "required_conversation_resolution": true
}
JSON

apply() {
  echo "Applying branch protection to ${REPO}@${BRANCH} (enforce_admins=${ENFORCE_ADMINS}) ..."
  printf '%s' "$PAYLOAD" | gh api -X PUT \
    -H "Accept: application/vnd.github+json" \
    "repos/${REPO}/branches/${BRANCH}/protection" --input - >/dev/null
  echo "Applied. Verify with: $0 --verify"
}

verify() {
  gh api "repos/${REPO}/branches/${BRANCH}/protection" \
    --jq '{required_status_checks: .required_status_checks.contexts, enforce_admins: .enforce_admins.enabled, required_approving_review_count: .required_pull_request_reviews.required_approving_review_count, allow_force_pushes: .allow_force_pushes.enabled, allow_deletions: .allow_deletions.enabled, required_linear_history: .required_linear_history.enabled}'
}

case "${1:-}" in
  --apply)  apply ;;
  --verify) verify ;;
  ""|--dry-run)
    echo "DRY RUN — nothing applied. Target: ${REPO}@${BRANCH}"
    echo "Payload that --apply would PUT:"
    printf '%s\n' "$PAYLOAD" | python3 -m json.tool
    echo
    echo "To apply (needs repo admin):   $0 --apply"
    echo "To inspect current state:      $0 --verify"
    ;;
  *)
    echo "Unknown arg: $1" >&2
    echo "Usage: $0 [--apply | --verify | --dry-run]" >&2
    exit 2 ;;
esac
