"""Cost kill switch — the last brake on an AWS bill that is running away.

Owner-directed 2026-08-31: "If costs spike hard and I'm not around, we need a
mechanism to cut things off."

WHAT THIS DOES, EXACTLY
-----------------------
Invoked from SNS (an AWS Budgets 120%-of-budget notification, or the
``AWS/Billing`` ``EstimatedCharges`` CloudWatch alarm — see
``infra/cost_kill_switch.tf``), it performs three, and only three, actions:

1. ``application-autoscaling:RegisterScalableTarget`` — sets the ECS service's
   scaling floor AND ceiling to 0.
2. ``ecs:UpdateService`` — sets the service's desired task count to 0.
3. ``ec2:StopInstances`` — **stops** (never terminates) the oracle+agent runner
   instance.

Then it publishes a loud SNS notification saying precisely what it did and the
one command that undoes it.

Step order is load-bearing. Application Auto Scaling actively *enforces* the
scaling floor: with ``MinCapacity = 1`` still registered, setting
``desiredCount = 0`` is reverted within minutes and the kill switch would look
like it worked while the bill kept running. The floor goes to 0 first, then the
service. The ceiling goes to 0 as well, because a floor of 0 alone still leaves
the target-tracking policy free to scale back out.

WHAT THIS NEVER DOES
--------------------
It never touches a data store. No Aurora, no ElastiCache, no S3, no EBS
snapshot, no DynamoDB — not even a read. Nothing here deletes, terminates, or
destroys anything: ``StopInstances`` preserves the root volume, and a scaled-to-
zero ECS service keeps its task definitions, its target group registration, and
its log groups. **This trades availability for spend, and only availability.**
The site goes down; not one byte of data is at risk. That asymmetry is the
entire design, it is what makes an unattended automatic trigger acceptable, and
the IAM policy in ``infra/cost_kill_switch.tf`` is scoped so that the Lambda
*could not* do otherwise even if this file were rewritten to try.

IDEMPOTENT
----------
Firing twice — the budget threshold *and* the billing alarm, or a duplicate SNS
delivery, since SNS is at-least-once — changes nothing the second time and says
so. Precisely:

- The autoscaling and ECS steps read current state first and skip the write.
- The EC2 step re-issues ``StopInstances`` and lets AWS no-op it. That is a
  deliberate trade: ``ec2:DescribeInstances`` supports no resource-level IAM
  scoping, so reading the instance state first would force the EC2 statement in
  the policy from one instance ARN to ``"*"``. One redundant API call is cheaper
  than that widening, and ``StopInstances`` reports ``PreviousState=stopped``,
  which is how this still prints "ALREADY".

FAIL-LOUD, NOT FAIL-FAST
------------------------
Each step is independently guarded. A failure in one does not abort the others:
a kill switch that stops the runner but bails before scaling the service in
because of a transient ECS throttle is worse than useless. Errors are collected
and shipped in the SNS message, and the handler re-raises at the end so the
failure is also visible as a Lambda error metric rather than a silent success —
the fail-soft-becomes-silence trap from CLAUDE.md.
"""

from __future__ import annotations

import json
import os
from typing import Any

import boto3

# ── Configuration (all injected by Terraform; see infra/cost_kill_switch.tf) ──

ECS_CLUSTER = os.environ["ECS_CLUSTER"]
ECS_SERVICE = os.environ["ECS_SERVICE"]
SCALABLE_TARGET_RESOURCE_ID = os.environ["SCALABLE_TARGET_RESOURCE_ID"]
SCALABLE_DIMENSION = "ecs:service:DesiredCount"
SERVICE_NAMESPACE = "ecs"

# May be empty: runner_ec2.tf is a separate lane and the instance is not
# guaranteed to exist in every environment. Empty means "skip that step", it
# does not mean "fail".
RUNNER_INSTANCE_ID = os.environ.get("RUNNER_INSTANCE_ID", "").strip()

NOTIFY_TOPIC_ARN = os.environ["NOTIFY_TOPIC_ARN"]

# Echoed into the recovery command so the operator does not have to look the
# steady-state numbers up while the site is down.
RESTORE_MIN_CAPACITY = os.environ.get("RESTORE_MIN_CAPACITY", "1")
RESTORE_MAX_CAPACITY = os.environ.get("RESTORE_MAX_CAPACITY", "4")
RESTORE_DESIRED_COUNT = os.environ.get("RESTORE_DESIRED_COUNT", "1")

# Rehearsal switch. Terraform pins this to "false" and a pytest guard asserts
# it (backend/tests/test_cost_kill_switch_guards.py): a kill switch left in
# dry-run is a kill switch that does not exist, and that is precisely the kind
# of thing nobody notices until the month it mattered.
DRY_RUN = os.environ.get("COST_KILL_SWITCH_DRY_RUN", "false").strip().lower() == "true"


def _recovery_command() -> str:
    """The single copy-pasteable command that undoes everything above.

    Chained with ``&&`` on purpose: the scaling floor must be restored before
    the desired count, for the same reason the kill path scales the floor to 0
    first.
    """
    parts = [
        "aws application-autoscaling register-scalable-target"
        f" --service-namespace {SERVICE_NAMESPACE}"
        f" --resource-id {SCALABLE_TARGET_RESOURCE_ID}"
        f" --scalable-dimension {SCALABLE_DIMENSION}"
        f" --min-capacity {RESTORE_MIN_CAPACITY}"
        f" --max-capacity {RESTORE_MAX_CAPACITY}",
        f"aws ecs update-service --cluster {ECS_CLUSTER}"
        f" --service {ECS_SERVICE} --desired-count {RESTORE_DESIRED_COUNT}",
    ]
    if RUNNER_INSTANCE_ID:
        parts.append(f"aws ec2 start-instances --instance-ids {RUNNER_INSTANCE_ID}")
    return " \\\n  && ".join(parts)


# ── Step 1: scaling floor and ceiling to zero ────────────────────────────────


def _scale_target_to_zero(actions: list[str], errors: list[str]) -> None:
    client = boto3.client("application-autoscaling")
    try:
        described = client.describe_scalable_targets(
            ServiceNamespace=SERVICE_NAMESPACE,
            ResourceIds=[SCALABLE_TARGET_RESOURCE_ID],
            ScalableDimension=SCALABLE_DIMENSION,
        )
        targets = described.get("ScalableTargets", [])
        if not targets:
            actions.append(
                f"autoscaling: NO scalable target registered for {SCALABLE_TARGET_RESOURCE_ID}"
                " — nothing to pin; the ECS step below is the only floor."
            )
            return
        current_min = targets[0].get("MinCapacity")
        current_max = targets[0].get("MaxCapacity")
        if current_min == 0 and current_max == 0:
            actions.append(
                f"autoscaling: ALREADY pinned at min=0 max=0 for {SCALABLE_TARGET_RESOURCE_ID} (idempotent no-op)"
            )
            return
        if DRY_RUN:
            actions.append(
                f"autoscaling: DRY RUN — would pin {SCALABLE_TARGET_RESOURCE_ID}"
                f" from min={current_min} max={current_max} to min=0 max=0"
            )
            return
        client.register_scalable_target(
            ServiceNamespace=SERVICE_NAMESPACE,
            ResourceId=SCALABLE_TARGET_RESOURCE_ID,
            ScalableDimension=SCALABLE_DIMENSION,
            MinCapacity=0,
            MaxCapacity=0,
        )
        actions.append(
            f"autoscaling: pinned {SCALABLE_TARGET_RESOURCE_ID} from min={current_min} max={current_max} to min=0 max=0"
        )
    except Exception as exc:  # broad on purpose — collected, reported, re-raised at the end
        errors.append(f"autoscaling: FAILED — {type(exc).__name__}: {exc}")


# ── Step 2: ECS service desired count to zero ────────────────────────────────


def _scale_service_to_zero(actions: list[str], errors: list[str]) -> None:
    client = boto3.client("ecs")
    try:
        described = client.describe_services(cluster=ECS_CLUSTER, services=[ECS_SERVICE])
        services = described.get("services", [])
        if not services:
            errors.append(f"ecs: service {ECS_SERVICE} not found in cluster {ECS_CLUSTER}")
            return
        current_desired = services[0].get("desiredCount")
        if current_desired == 0:
            actions.append(f"ecs: {ECS_CLUSTER}/{ECS_SERVICE} ALREADY at desiredCount=0 (idempotent no-op)")
            return
        if DRY_RUN:
            actions.append(
                f"ecs: DRY RUN — would set {ECS_CLUSTER}/{ECS_SERVICE} desiredCount from {current_desired} to 0"
            )
            return
        client.update_service(cluster=ECS_CLUSTER, service=ECS_SERVICE, desiredCount=0)
        actions.append(
            f"ecs: set {ECS_CLUSTER}/{ECS_SERVICE} desiredCount"
            f" from {current_desired} to 0 (tasks drain; task definitions, target"
            " group registration and log groups are untouched)"
        )
    except Exception as exc:  # broad on purpose — see the module docstring
        errors.append(f"ecs: FAILED — {type(exc).__name__}: {exc}")


# ── Step 3: stop (never terminate) the runner instance ───────────────────────


def _stop_runner_instance(actions: list[str], errors: list[str]) -> None:
    if not RUNNER_INSTANCE_ID:
        actions.append("ec2: no RUNNER_INSTANCE_ID configured — skipped")
        return
    if DRY_RUN:
        actions.append(f"ec2: DRY RUN — would stop instance {RUNNER_INSTANCE_ID}")
        return
    client = boto3.client("ec2")
    try:
        # StopInstances is itself idempotent: stopping an already-stopped
        # instance succeeds and reports PreviousState=stopped. Reading the
        # response is why this needs no ec2:DescribeInstances permission —
        # Describe* actions do not support resource-level IAM scoping, so
        # avoiding the call is what keeps the EC2 statement pinned to this one
        # instance ARN instead of "*".
        response = client.stop_instances(InstanceIds=[RUNNER_INSTANCE_ID])
        stopping = response.get("StoppingInstances", [])
        if not stopping:
            errors.append(f"ec2: stop_instances returned no state for {RUNNER_INSTANCE_ID}")
            return
        previous = stopping[0].get("PreviousState", {}).get("name", "unknown")
        current = stopping[0].get("CurrentState", {}).get("name", "unknown")
        if previous in ("stopped", "stopping"):
            actions.append(f"ec2: {RUNNER_INSTANCE_ID} ALREADY {previous} (idempotent no-op)")
        else:
            actions.append(
                f"ec2: stopped {RUNNER_INSTANCE_ID} ({previous} -> {current});"
                " root EBS volume preserved, instance NOT terminated"
            )
    except Exception as exc:  # broad on purpose — see the module docstring
        errors.append(f"ec2: FAILED — {type(exc).__name__}: {exc}")


# ── Reporting ────────────────────────────────────────────────────────────────


def _trigger_description(event: dict[str, Any]) -> str:
    """Best-effort human description of what fired us, from the SNS envelope."""
    try:
        records = event.get("Records") or []
        if not records:
            return "manual invocation (no SNS Records in the event)"
        sns = records[0].get("Sns", {})
        subject = (sns.get("Subject") or "").strip()
        message = (sns.get("Message") or "").strip()
        # CloudWatch alarm notifications are JSON; budget notifications are prose.
        try:
            parsed = json.loads(message)
            if isinstance(parsed, dict) and "AlarmName" in parsed:
                return (
                    f"CloudWatch alarm {parsed.get('AlarmName')}"
                    f" -> {parsed.get('NewStateValue')}: {parsed.get('NewStateReason', '')}"
                )
        except (ValueError, TypeError):
            pass
        if subject:
            return f"{subject} :: {message[:400]}"
        return message[:400] or "SNS notification with no subject or body"
    except Exception:  # broad on purpose — reporting must never break the kill path
        return "unparseable trigger event"


def _publish(actions: list[str], errors: list[str], trigger: str) -> str:
    banner = "AWS COST KILL SWITCH FIRED" if not DRY_RUN else "AWS COST KILL SWITCH — DRY RUN"
    lines = [
        f"*** {banner} ***",
        "",
        f"Trigger: {trigger}",
        "",
        "WHAT I DID:",
        *(f"  - {a}" for a in actions),
    ]
    if errors:
        lines += ["", "WHAT FAILED (these were NOT done — check manually):", *(f"  - {e}" for e in errors)]
    lines += [
        "",
        "WHAT I DID NOT TOUCH: Aurora, ElastiCache, S3, EBS snapshots. No data",
        "store was read, modified, or deleted. This is an availability sacrifice",
        "only — the site is DOWN, nothing is lost. Aurora and ElastiCache keep",
        "billing while stopped-service traffic is zero; this brake removes the",
        "compute/egress spike, not the whole bill.",
        "",
        "RECOVERY — one command (AWS CloudShell or any shell with the admin profile):",
        "",
        _recovery_command(),
        "",
        "Or, from a repo checkout:  ./infra/scripts/cost-kill-switch-recover.sh",
        "",
        "Runbook: docs/runbooks/cost-kill-switch.md",
        "",
        "BEFORE YOU RECOVER: find out what spent the money. Recovering without",
        "fixing the cause re-arms the same spike, and the budget threshold has",
        "already been crossed for the month, so the switch will not fire again",
        "at the same boundary.",
    ]
    body = "\n".join(lines)
    subject = ("[DRY RUN] " if DRY_RUN else "") + "ARCHIMEDES COST KILL SWITCH FIRED"
    boto3.client("sns").publish(
        TopicArn=NOTIFY_TOPIC_ARN,
        Subject=subject[:100],  # SNS hard limit
        Message=body,
    )
    return body


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    trigger = _trigger_description(event or {})
    actions: list[str] = []
    errors: list[str] = []

    _scale_target_to_zero(actions, errors)
    _scale_service_to_zero(actions, errors)
    _stop_runner_instance(actions, errors)

    published = True
    try:
        _publish(actions, errors, trigger)
    except Exception as exc:  # broad on purpose — see the module docstring
        published = False
        errors.append(f"sns: FAILED to publish — {type(exc).__name__}: {exc}")

    result = {
        "dryRun": DRY_RUN,
        # Correlates this structured line with the CloudWatch log stream and with
        # the SNS message a human is reading at the same moment.
        "requestId": getattr(context, "aws_request_id", None),
        "trigger": trigger,
        "actions": actions,
        "errors": errors,
        "notified": published,
    }
    print(json.dumps(result))

    if errors:
        # Surface as a Lambda error metric too. The SNS message (if it went out)
        # already carries the detail; this exists so a silent partial failure
        # cannot look like a clean run in the Lambda console.
        raise RuntimeError(f"cost kill switch completed with {len(errors)} error(s): {errors}")
    return result
