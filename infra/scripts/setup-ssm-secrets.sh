#!/usr/bin/env bash
# Archimedes — push app secrets to SSM Parameter Store (SecureString).
#
# Secrets NEVER live in the repo, in Terraform state, or in GitHub. They live in
# SSM Parameter Store under /archimedes/prod/* as SecureString, and the EC2/ECS
# instance role reads them at deploy/runtime. This script pushes them.
#
# Values are read from your SHELL ENVIRONMENT (never hardcoded here). Export the
# ones you have, then run. Missing ones are skipped, so partial runs are fine:
#
#   export CIRCLE_API_KEY='...'
#   AWS_PROFILE=ArchimedesDanAdmin conda run -n archimedes ./setup-ssm-secrets.sh          # dry run
#   AWS_PROFILE=ArchimedesDanAdmin conda run -n archimedes ./setup-ssm-secrets.sh --apply  # write them
#
# Tip: keep values in a gitignored file and `set -a; source secrets.env; set +a` first.
# The script prints parameter NAMES only — never the secret values.
set -euo pipefail

PREFIX="/archimedes/prod"
# NAMES match what services/secrets_service.load_ssm_secrets() reads under
# /archimedes/prod/*. Missing env vars are skipped, so partial runs are fine.
PARAMS=(
  # --- Current runtime secrets (loaded into os.environ at backend startup) ---
  LLM_PROVIDER             # LLM backend selector (GLM today; revisited when Bedrock lands, T3.1)
  LLM_AUTH_TOKEN           # LLM API auth token (BYOK / current provider)
  LLM_BASE_URL             # LLM endpoint base URL
  EMAIL_ENCRYPTION_KEY     # at-rest encryption key for stored user emails
  BETTER_AUTH_SECRET       # >=32-char session-signing secret for Better Auth sidecar
  GOOGLE_CLIENT_ID         # optional; seed with GOOGLE_CLIENT_SECRET before enabling Terraform flag
  GOOGLE_CLIENT_SECRET     # optional Google OAuth credential
  GITHUB_CLIENT_ID         # optional; seed with GITHUB_CLIENT_SECRET before enabling Terraform flag
  GITHUB_CLIENT_SECRET     # optional GitHub OAuth credential
  AURORA_MASTER_PASSWORD   # DB master password (mirror TF_VAR_aurora_master_password)
  DATABASE_URL             # Aurora connection URL — consumed by the backend Fargate task (ecs.tf secrets) AND the relocated oracle/agent runners (fetch-secrets.sh); ecs.tf's header flags this as not-yet-seeded
  REDIS_URL                # ElastiCache connection URL — same two consumers; same not-yet-seeded gap
  # --- Forthcoming, as features land (roadmap T1.x) ---
  CIRCLE_API_KEY           # Circle wallets / Gateway nanopayments (T1.2) — also the oracle+agent Circle DCW signer (#1065)
  CIRCLE_ENTITY_SECRET     # Circle dev-controlled wallet entity secret (oracle+agent, #1065)
  # --- Runner relocation (issue #1065 / #1043) — oracle+agent EC2 + kb-runner ---
  # Names only, matching issue #1065's execution checklist Step 1 (the
  # Agora-workspace-level coordination doc T32-COORDINATION-DELTA-2026-07-08.md
  # §2 has the same list). Values set by Dan post-T3.2, once decision #1
  # (agent signer: Circle DCW vs raw key) is resolved.
  WALLET_ID                # Circle DCW wallet UUID (oracle/agent Circle signer)
  WALLET_ADDRESS           # that wallet's EVM address (public, informational)
  INTERNAL_AGENT_API_KEY   # X-Internal-Agent-Key shared secret, agent runner -> backend internal API
  ARC_AGENT_PRIVATE_KEY    # raw-key agent signer FALLBACK (chain/executor.py) — only if not using Circle DCW for the agent (decision #1)
  # ALL mutable ARC_*_ADDRESS values are SSM-sourced (never hardcoded): they
  # change at every contract redeploy (T3.2), so the runner reads them from
  # here via fetch-secrets.sh → --env-file (single source of truth; the
  # systemd units pass NO `-e ARC_*_ADDRESS` flag that could override them,
  # and a missing address fails the container closed rather than signing a
  # dead contract). Seed every new address in ONE `--apply` at T3.2.
  ARC_VAULT_FACTORY_ADDRESS           # VaultFactory — oracle + agent runner (fetch-secrets.sh)
  ARC_AMM_ROUTER_ADDRESS              # AMMRouter — agent runner rebalance path (fetch-secrets.sh)
  ARC_REASONING_TRACE_REGISTRY_ADDRESS # ReasoningTraceRegistry — oracle + agent runner (fetch-secrets.sh)
  ARC_STRATEGY_REGISTRY_ADDRESS       # StrategyRegistry — changes at every contract redeploy (T3.2), so SSM-sourced, not hardcoded
  ARC_PAYMENT_SPLITTER_ADDRESS        # PaymentSplitter (marketplace payouts) — same rationale
  # --- Runner behaviour switches (SSM-sourced so they can change without an
  #     instance replacement — see runner-user-data.sh's systemd-unit preamble) ---
  AGENT_DRY_RUN                       # "true" = agent computes trades but signs NOTHING on-chain.
                                      # Funds-behaviour switch for the relocated agent runner. Deliberately
                                      # NOT a `docker run -e` flag (that would override --env-file and, because
                                      # aws_instance.runner sets ignore_changes=[user_data], be unchangeable
                                      # without replacing the box). Seed "true", flip to "false" only after a
                                      # dry-run smoke pass, then `systemctl restart archimedes-agent`.
                                      # WARNING: agent_runner.py defaults AGENT_DRY_RUN to "false" when unset,
                                      # so leaving this unseeded means LIVE trading on first boot.
)
# NOTE: VITE_CIRCLE_CLIENT_KEY is a BUILD-TIME secret baked into the UI bundle at
# `docker compose build` — it lives in the box-local .env (seeded by user-data.sh),
# NOT read from SSM at runtime. Do not add build-time secrets here.

APPLY=false; for a in "$@"; do case "$a" in
  --apply) APPLY=true;; -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
  *) echo "unknown arg: $a" >&2; exit 2;; esac; done
$APPLY && echo ">>> APPLY MODE — writing SecureString params under ${PREFIX}/" \
        || echo ">>> DRY RUN — re-run with --apply to write. Values are read from env; names only are shown."

# ── Fail-closed guard for funds-behaviour parameters ────────────────────────
# Everything else in this script is intentionally skip-if-unset, so partial runs
# work. That default is WRONG for AGENT_DRY_RUN: agent_runner.py resolves an
# unset AGENT_DRY_RUN to "false" (= LIVE on-chain signing), so "operator ran
# --apply and didn't notice one skip line among twenty" silently arms the
# funds-adjacent agent. Enforce the invariant in the tool instead of relying on
# the caller reading output (Copilot review, PR #1173).
#
# Deliberately narrow so partial runs keep working:
#   - only in --apply mode (a dry run writes nothing and stays advisory)
#   - only for AGENT_DRY_RUN
#   - satisfied by EITHER an exported value OR the parameter already existing in
#     SSM (re-seeding one unrelated secret must not require re-passing it)
#   - checked BEFORE any put, so a failure writes nothing at all
if $APPLY; then
  if [ -n "${BETTER_AUTH_SECRET:-}" ] && [ "${#BETTER_AUTH_SECRET}" -lt 32 ]; then
    echo "ERROR: refusing to --apply. BETTER_AUTH_SECRET must contain at least 32 characters." >&2
    exit 3
  fi

  if [ -z "${AGENT_DRY_RUN:-}" ]; then
    if ! aws ssm get-parameter --name "${PREFIX}/AGENT_DRY_RUN" >/dev/null 2>&1; then
      cat >&2 <<EOM
ERROR: refusing to --apply. AGENT_DRY_RUN is not set and ${PREFIX}/AGENT_DRY_RUN
       does not exist yet.

       agent_runner.py treats an UNSET AGENT_DRY_RUN as "false" — i.e. the
       relocated agent runner would boot into LIVE on-chain signing.

       Seed it explicitly, dry-run first:
           export AGENT_DRY_RUN=true    # flip to false only after a smoke pass
       then re-run with --apply.
EOM
      exit 3
    fi
  else
    # VALUE validation, not just presence (Copilot review, PR #1174).
    # agent_runner.py parses this as:  os.getenv("AGENT_DRY_RUN","false").lower() == "true"
    # so ONLY the literal "true" (any case) enables dry-run and EVERYTHING else —
    # including the intuitive-looking "1", "yes", "on", "True " with whitespace —
    # silently resolves to LIVE signing. An operator typing `AGENT_DRY_RUN=1`
    # reasonably believes they armed dry-run; they armed the opposite. Accept only
    # the two values that mean what they look like.
    case "$(printf '%s' "$AGENT_DRY_RUN" | tr 'A-Z' 'a-z')" in
      true|false) ;;
      *)
        cat >&2 <<EOM
ERROR: refusing to --apply. AGENT_DRY_RUN="${AGENT_DRY_RUN}" is not a recognised value.

       agent_runner.py evaluates:  AGENT_DRY_RUN.lower() == "true"
       so ONLY "true" enables dry-run. Values like "1", "yes" or "on" look like
       they enable it but resolve to LIVE on-chain signing.

       Use exactly one of:
           export AGENT_DRY_RUN=true     # agent computes trades, signs nothing
           export AGENT_DRY_RUN=false    # LIVE signing (only after a smoke pass)
EOM
        exit 3
        ;;
    esac
  fi
fi

put=0; skip=0
for name in "${PARAMS[@]}"; do
  val="${!name:-}"
  path="${PREFIX}/${name}"
  if [ -z "$val" ]; then
    printf '  skip  %s   (env var %s not set)\n' "$path" "$name"; skip=$((skip+1)); continue
  fi
  printf '  put   %s   (SecureString, %d chars)\n' "$path" "${#val}"
  if $APPLY; then
    aws ssm put-parameter --name "$path" --type SecureString --value "$val" --overwrite >/dev/null
  fi
  put=$((put+1))
done

echo
echo "summary: ${put} to write, ${skip} skipped (env not set)."
$APPLY || echo "(dry run — re-run with --apply to write the ${put} parameter(s))"
echo "Verify (names + metadata only, never values):"
echo "  aws ssm get-parameters-by-path --path ${PREFIX} --query 'Parameters[].Name'"
