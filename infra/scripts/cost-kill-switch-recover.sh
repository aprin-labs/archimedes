#!/usr/bin/env bash
# Undo an AWS cost kill-switch fire (infra/cost_kill_switch.tf).
#
# The kill switch scaled the ECS backend to zero and stopped the runner EC2
# instance. Nothing was deleted; this puts both back. See
# docs/runbooks/cost-kill-switch.md — in particular the section that says to
# find out what spent the money BEFORE running this.
#
# DRY-RUN BY DEFAULT, matching setup-budgets.sh in this directory. Nothing is
# changed unless you pass --apply.
#
#   ./cost-kill-switch-recover.sh              # print exactly what would run
#   ./cost-kill-switch-recover.sh --apply      # actually recover
#
# Requires AWS_PROFILE exported (e.g. ArchimedesDanAdmin) and aws CLI v2.
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
CLUSTER="${ECS_CLUSTER:-archimedes-cluster}"
SERVICE="${ECS_SERVICE:-archimedes-backend}"
RESOURCE_ID="service/${CLUSTER}/${SERVICE}"
MIN_CAPACITY="${MIN_CAPACITY:-1}"   # var.ecs_service_min_count
MAX_CAPACITY="${MAX_CAPACITY:-4}"   # var.ecs_service_max_count
DESIRED_COUNT="${DESIRED_COUNT:-1}" # var.ecs_service_desired_count

# Empty by default: the runner instance id is environment-specific and changes
# if the box is ever replaced. `terraform output` or the tag lookup below finds
# it; override with RUNNER_INSTANCE_ID to skip the lookup.
RUNNER_INSTANCE_ID="${RUNNER_INSTANCE_ID:-}"

APPLY=false
for a in "$@"; do case "$a" in
  --apply) APPLY=true ;;
  -h | --help)
    grep '^#' "$0" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
  *)
    echo "unknown arg: $a" >&2
    exit 2
    ;;
esac done

run() {
  printf '  + %s\n' "$*"
  if $APPLY; then "$@"; fi
}

$APPLY && echo ">>> APPLY MODE — the stack WILL be brought back up in ${REGION}" \
  || echo ">>> DRY RUN — nothing will change. Re-run with --apply to recover."

if [ -z "$RUNNER_INSTANCE_ID" ]; then
  RUNNER_INSTANCE_ID="$(aws ec2 describe-instances --region "$REGION" \
    --filters "Name=tag:Name,Values=archimedes-runner" "Name=instance-state-name,Values=stopped,stopping,running" \
    --query 'Reservations[].Instances[].InstanceId | [0]' --output text 2>/dev/null || true)"
fi

# ORDER IS LOAD-BEARING. Application Auto Scaling enforces the floor: restoring
# desiredCount while the target is still pinned at min=0/max=0 gets scaled
# straight back to zero, and the recovery looks like it failed for no reason.
echo
echo "== 1/3  restore the Application Auto Scaling floor and ceiling =="
run aws application-autoscaling register-scalable-target \
  --region "$REGION" \
  --service-namespace ecs \
  --resource-id "$RESOURCE_ID" \
  --scalable-dimension ecs:service:DesiredCount \
  --min-capacity "$MIN_CAPACITY" \
  --max-capacity "$MAX_CAPACITY"

echo
echo "== 2/3  restore the ECS service desired count =="
run aws ecs update-service \
  --region "$REGION" \
  --cluster "$CLUSTER" \
  --service "$SERVICE" \
  --desired-count "$DESIRED_COUNT"

echo
echo "== 3/3  start the oracle+agent runner =="
if [ -z "$RUNNER_INSTANCE_ID" ] || [ "$RUNNER_INSTANCE_ID" = "None" ]; then
  echo "  !! no archimedes-runner instance found — set RUNNER_INSTANCE_ID and re-run this step"
else
  run aws ec2 start-instances --region "$REGION" --instance-ids "$RUNNER_INSTANCE_ID"
fi

echo
if $APPLY; then
  echo "Recovery issued. Watch the service reach steady state:"
  echo "  aws ecs wait services-stable --region $REGION --cluster $CLUSTER --services $SERVICE"
  echo
  echo "Then re-check the thing that spent the money — the budget threshold for"
  echo "this month has already been crossed, so the kill switch will NOT fire"
  echo "again at the same boundary."
else
  echo "(dry run — re-run with --apply to execute the above)"
fi
