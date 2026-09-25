"""Guards for the AWS cost kill switch (infra/cost_kill_switch.tf).

Owner-directed 2026-08-31: "If costs spike hard and I'm not around, we need a
mechanism to cut things off." The mechanism is a Lambda that scales the ECS
backend to zero and stops the runner EC2 instance when spend crosses a budget
threshold, with nobody watching.

An unattended automatic shutdown is only acceptable because of one property:
**it sacrifices availability and nothing else.** It cannot reach Aurora,
ElastiCache, S3, EBS snapshots or DynamoDB, and it holds no Delete*, Terminate*
or Destroy* permission of any kind. That property is not enforced by the Python
in ``infra/lambda/cost_kill_switch/index.py`` — the Python can be rewritten in a
one-line PR. It is enforced by the IAM policy, and these tests are what stop the
IAM policy from quietly widening. A ``"Resource": "*"`` or an ``"Action": "*"``
added "just to make it work" is a five-character edit that converts a cost brake
into a thing that can delete the database.

``terraform validate`` cannot catch any of this. ``"Action": "*"`` is
syntactically perfect HCL, and so is ``"rds:DeleteDBCluster"``.

Same idiom as ``test_ecs_backend_secrets.py`` and ``test_infra_gate_workflow.py``:
read the repo's own files as text, assert the wiring. Hermetic by construction —
no AWS, no terraform binary, no network, no env vars, no ``.env``. The only
inputs are three files in the repo.

The helpers below take their path as a defaulted argument rather than reading a
module constant, so the same checker can be pointed at a deliberately-broken
copy. Both the adversarial tests at the bottom of this file and the PR body's
demo do exactly that.
"""

from __future__ import annotations

import ast
import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COST_TF = REPO_ROOT / "infra" / "cost_kill_switch.tf"
LAMBDA_SRC = REPO_ROOT / "infra" / "lambda" / "cost_kill_switch" / "index.py"
RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "cost-kill-switch.md"

# The single documented exception to "no wildcard resources". Application Auto
# Scaling defines NO IAM resource types, so RegisterScalableTarget /
# DescribeScalableTargets cannot be written against an ARN at all. The wildcard
# is bounded by the enumerated action list instead — which is why this constant
# is paired with WILDCARD_RESOURCE_ALLOWED_ACTIONS and both are asserted.
WILDCARD_RESOURCE_ALLOWED_SIDS = frozenset({"AutoScalingPinToZero"})
WILDCARD_RESOURCE_ALLOWED_ACTIONS = frozenset(
    {
        "application-autoscaling:DescribeScalableTargets",
        "application-autoscaling:RegisterScalableTarget",
    }
)

# The complete permission surface, pinned. Adding anything to the policy without
# adding it here fails this file — which is the point: a new permission on a
# kill switch should require someone to state it twice.
EXPECTED_ACTIONS = frozenset(
    {
        "application-autoscaling:DescribeScalableTargets",
        "application-autoscaling:RegisterScalableTarget",
        "ecs:DescribeServices",
        "ecs:UpdateService",
        "ec2:StopInstances",
        "sns:Publish",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
    }
)

# IAM service prefixes that own persistent data. Not one of them may appear.
DATA_STORE_PREFIXES = (
    "rds",
    "elasticache",
    "s3",
    "s3express",
    "dynamodb",
    "backup",
    "kms",
    "efs",
    "redshift",
    "docdb",
    "neptune",
    "glacier",
)

# Action verbs that destroy rather than pause. `StopInstances` keeps the root
# volume; `TerminateInstances` does not, and that difference is the whole
# argument for letting this thing run unattended.
DESTRUCTIVE_VERB_RE = re.compile(
    r":(Delete|Terminate|Destroy|Purge|Revoke|Reboot|Restore|Modify|Put|Create)",
    re.IGNORECASE,
)
# ...with the two log-writing actions exempted, since a Lambda that cannot write
# its own log stream cannot be debugged after it fires.
DESTRUCTIVE_VERB_EXEMPT = frozenset({"logs:CreateLogStream", "logs:PutLogEvents"})

# boto3 clients the Lambda is allowed to construct. Anything else — an
# `rds` client, an `s3` client — is a violation regardless of what it then does
# with it, and a dynamically-named client defeats the check entirely so it is a
# violation too.
ALLOWED_BOTO3_CLIENTS = frozenset({"application-autoscaling", "ecs", "ec2", "sns"})

REQUIRED_LAMBDA_CALLS = frozenset(
    {
        "describe_scalable_targets",
        "register_scalable_target",
        "describe_services",
        "update_service",
        "stop_instances",
        "publish",
    }
)

FORBIDDEN_LAMBDA_CALL_PREFIXES = ("delete_", "terminate_", "destroy_", "purge_", "restore_")
FORBIDDEN_LAMBDA_CALLS = frozenset(
    {
        "terminate_instances",
        "delete_service",
        "delete_cluster",
        "delete_db_cluster",
        "delete_db_instance",
        "modify_db_cluster",
        "modify_db_instance",
        "delete_cache_cluster",
        "delete_bucket",
        "delete_object",
        "delete_objects",
        "delete_table",
        "deregister_scalable_target",
    }
)


# ── HCL text helpers ─────────────────────────────────────────────────────────


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text(encoding="utf-8")


def _block_body(text: str, header: str) -> str:
    """The body of the first HCL block whose header line matches ``header``.

    Brace-counting rather than regex: notification/environment sub-blocks nest,
    and a regex that stops at the first ``}`` silently returns a fragment, which
    would make every assertion below pass vacuously.
    """
    start = text.index(header)
    open_brace = text.index("{", start)
    depth = 0
    for i in range(open_brace, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[open_brace + 1 : i]
    raise AssertionError(f"unbalanced braces after {header!r}")


def _sub_blocks(body: str, name: str) -> list[str]:
    """Every ``name { ... }`` sub-block directly inside ``body``."""
    out: list[str] = []
    for match in re.finditer(rf"(?m)^\s*{re.escape(name)}\s*\{{", body):
        out.append(_block_body(body[match.start() :], f"{name}"))
    return out


def _attr(body: str, key: str) -> str | None:
    match = re.search(rf"(?m)^\s*{re.escape(key)}\s*=\s*(.+?)\s*$", body)
    return match.group(1) if match else None


# Terraform interpolations are resolved to *shapes*, not values, so the JSON
# parses and the wildcard rules stay meaningful. A `${...arn}` reference is by
# construction a fully-qualified ARN for exactly one resource, so it becomes a
# well-formed placeholder ARN; anything else becomes an opaque token. Neither
# substitution can introduce a `*`, so it cannot mask a real widening.
_ARN_REF_RE = re.compile(r"\$\{[^{}]*\.arn\}")
_ANY_REF_RE = re.compile(r"\$\{[^{}]*\}")
_PLACEHOLDER_ARN = "arn:aws:svc:us-east-1:000000000000:resource/tf-ref"


def _resolve_interpolations(text: str) -> str:
    return _ANY_REF_RE.sub("tf-ref", _ARN_REF_RE.sub(_PLACEHOLDER_ARN, text))


def _heredoc(body: str, key: str = "policy") -> str:
    """The contents of a ``key = <<-JSON ... JSON`` heredoc inside ``body``."""
    match = re.search(rf"(?ms)^\s*{re.escape(key)}\s*=\s*<<-(\w+)\n(.*?)^\s*\1\s*$", body)
    assert match, f"no heredoc for {key!r}"
    return match.group(2)


def lambda_role_policy(tf_path: Path = COST_TF) -> dict[str, Any]:
    """The kill-switch Lambda's inline execution-role policy, as parsed JSON."""
    body = _block_body(_read(tf_path), 'resource "aws_iam_role_policy" "cost_kill_switch"')
    return json.loads(_resolve_interpolations(_heredoc(body)))


def _statements(doc: dict[str, Any]) -> list[dict[str, Any]]:
    statements = doc.get("Statement", [])
    return statements if isinstance(statements, list) else [statements]


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


# ── The checkers. Public, path/dict-taking, so a broken copy can be fed in. ──


def wildcard_action_violations(doc: dict[str, Any]) -> list[str]:
    """Actions that are ``*`` or carry a ``*`` anywhere (e.g. ``ecs:*``)."""
    bad: list[str] = []
    for st in _statements(doc):
        sid = st.get("Sid", "<no Sid>")
        for action in _as_list(st.get("Action")) + _as_list(st.get("NotAction")):
            if not isinstance(action, str) or "*" in action:
                bad.append(f"{sid}: wildcard action {action!r}")
    return bad


def wildcard_resource_violations(
    doc: dict[str, Any], allowed_sids: frozenset[str] = WILDCARD_RESOURCE_ALLOWED_SIDS
) -> list[str]:
    """Resources that are ``*``, or ARNs wildcarded in a structural position.

    A trailing ``:*`` on a log-group ARN is fine (it names that group's streams);
    ``arn:aws:ecs:*:*:*`` is not, and neither is a bare ``*`` outside the one
    documented exception.
    """
    bad: list[str] = []
    for st in _statements(doc):
        sid = st.get("Sid", "<no Sid>")
        for resource in _as_list(st.get("Resource")) + _as_list(st.get("NotResource")):
            if not isinstance(resource, str):
                bad.append(f"{sid}: non-string resource {resource!r}")
                continue
            if resource == "*":
                if sid not in allowed_sids:
                    bad.append(f"{sid}: bare wildcard resource '*'")
                continue
            if not resource.startswith("arn:"):
                bad.append(f"{sid}: resource is not an ARN: {resource!r}")
                continue
            fields = resource.split(":", 5)
            if len(fields) < 6:
                bad.append(f"{sid}: malformed ARN {resource!r}")
                continue
            if any("*" in field for field in fields[:5]):
                bad.append(f"{sid}: wildcard in ARN partition/service/region/account: {resource!r}")
            if fields[5] == "*" or fields[5].startswith("*"):
                bad.append(f"{sid}: wildcard resource-id in {resource!r}")
    return bad


def data_store_violations(doc: dict[str, Any]) -> list[str]:
    """Any action against a service that owns persistent data."""
    bad: list[str] = []
    for st in _statements(doc):
        sid = st.get("Sid", "<no Sid>")
        for action in _as_list(st.get("Action")) + _as_list(st.get("NotAction")):
            if not isinstance(action, str):
                continue
            prefix = action.split(":", 1)[0].lower()
            if prefix in DATA_STORE_PREFIXES:
                bad.append(f"{sid}: data-store action {action!r}")
    return bad


def destructive_action_violations(doc: dict[str, Any]) -> list[str]:
    """Delete/Terminate/Destroy-class actions, log writes excepted."""
    bad: list[str] = []
    for st in _statements(doc):
        sid = st.get("Sid", "<no Sid>")
        for action in _as_list(st.get("Action")) + _as_list(st.get("NotAction")):
            if not isinstance(action, str) or action in DESTRUCTIVE_VERB_EXEMPT:
                continue
            if DESTRUCTIVE_VERB_RE.search(action):
                bad.append(f"{sid}: destructive action {action!r}")
    return bad


def _lambda_tree(src_path: Path = LAMBDA_SRC) -> ast.Module:
    return ast.parse(_read(src_path))


def boto3_clients(src_path: Path = LAMBDA_SRC) -> list[str | None]:
    """Every service name passed to ``boto3.client``/``boto3.resource``.

    ``None`` marks a non-literal argument — a dynamically named client, which
    would make this whole check unenforceable and is therefore reported rather
    than ignored.
    """
    found: list[str | None] = []
    for node in ast.walk(_lambda_tree(src_path)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in ("client", "resource"):
            continue
        if not (isinstance(node.func.value, ast.Name) and node.func.value.id == "boto3"):
            continue
        if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            found.append(node.args[0].value)
        else:
            found.append(None)
    return found


def lambda_method_calls(src_path: Path = LAMBDA_SRC) -> set[str]:
    """Names of every attribute-style call in the Lambda source.

    AST rather than substring search: the module's docstring says the words
    "Aurora", "S3" and "terminate" while explaining that it touches none of
    them, and a grep-based guard would either flag that prose or be weakened
    until it flagged nothing.
    """
    return {
        node.func.attr
        for node in ast.walk(_lambda_tree(src_path))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


# ── Budget ladder ────────────────────────────────────────────────────────────


def _budget_notifications(tf_path: Path = COST_TF) -> list[dict[str, str]]:
    body = _block_body(_read(tf_path), 'resource "aws_budgets_budget" "cost_kill_switch"')
    out = []
    for block in _sub_blocks(body, "notification"):
        out.append(
            {
                key: _attr(block, key) or ""
                for key in (
                    "comparison_operator",
                    "threshold",
                    "threshold_type",
                    "notification_type",
                    "subscriber_sns_topic_arns",
                )
            }
        )
    return out


def test_budget_has_exactly_the_three_documented_thresholds():
    notifications = _budget_notifications()
    thresholds = sorted(int(n["threshold"]) for n in notifications)
    assert thresholds == [50, 80, 100, 120], (
        "The ladder is 50/80/100% notify / 120% KILL. Found "
        f"{thresholds}. Dropping a rung removes a warning the owner is "
        "supposed to get before anything shuts itself off. (100 exists because "
        "the Budgets API allows ONE SNS subscriber per notification — "
        "CreationLimitExceededException, 2026-09-01 — so the kill rung cannot "
        "also carry the human topic; the human hears at 100 instead.)"
    )


@pytest.mark.parametrize("threshold", [50, 80, 100, 120])
def test_every_threshold_is_actual_not_forecast(threshold):
    notification = next(n for n in _budget_notifications() if int(n["threshold"]) == threshold)
    assert notification["notification_type"] == '"ACTUAL"', (
        "FORECASTED would let AWS's projection — noisy in the first days of a "
        "month on an account this small — take production down over money that "
        "has not been spent."
    )
    assert notification["threshold_type"] == '"PERCENTAGE"'
    assert notification["comparison_operator"] == '"GREATER_THAN"'


@pytest.mark.parametrize("threshold", [50, 80])
def test_notify_rungs_go_to_the_existing_alerts_topic_and_not_the_kill_topic(threshold):
    notification = next(n for n in _budget_notifications() if int(n["threshold"]) == threshold)
    subscribers = notification["subscriber_sns_topic_arns"]
    assert "aws_sns_topic.alerts.arn" in subscribers
    assert "cost_kill_switch" not in subscribers, (
        f"The {threshold}% rung must NOT reach the kill topic. That topic's only "
        "subscriber turns production off; it belongs to the 120% rung alone."
    )


def test_kill_rung_reaches_the_kill_topic_and_still_tells_a_human():
    """The redundancy moved: Budgets allows ONE SNS subscriber per notification
    (CreationLimitExceededException on the 2026-09-01 apply), so 120% carries
    the Lambda topic ALONE, and "a broken Lambda cannot mean silence" is held
    by two other wires this test pins: the 100% rung mails the human first,
    and the EstimatedCharges tripwire fires BOTH topics at the same boundary.
    """
    kill = next(n for n in _budget_notifications() if int(n["threshold"]) == 120)
    assert kill["subscriber_sns_topic_arns"] == "[aws_sns_topic.cost_kill_switch.arn]", (
        "120% must invoke the Lambda, and ONLY the Lambda — a second subscriber "
        "is rejected by the Budgets API and would fail the apply."
    )
    human = next(n for n in _budget_notifications() if int(n["threshold"]) == 100)
    assert human["subscriber_sns_topic_arns"] == "[aws_sns_topic.alerts.arn]", (
        "100% is where the human hears the budget is blown before the kill fires."
    )
    tripwire = _read(COST_TF)
    assert "aws_sns_topic.alerts.arn" in tripwire and "aws_sns_topic.cost_kill_switch.arn" in tripwire, (
        "the tripwire must still reach both topics — that is the broken-Lambda redundancy now"
    )


def test_kill_switch_topic_has_exactly_one_subscriber_and_it_is_the_lambda():
    text = _read(COST_TF)
    subscriptions = re.findall(r'resource "aws_sns_topic_subscription" "(\w+)"', text)
    kill_subs = [
        name
        for name in subscriptions
        if "cost_kill_switch" in _block_body(text, f'resource "aws_sns_topic_subscription" "{name}"')
    ]
    assert len(kill_subs) == 1, f"expected one kill-topic subscriber, found {kill_subs}"
    body = _block_body(text, f'resource "aws_sns_topic_subscription" "{kill_subs[0]}"')
    assert _attr(body, "protocol") == '"lambda"'
    assert "aws_lambda_function.cost_kill_switch" in (_attr(body, "endpoint") or "")


# ── IAM: the real specification of what the kill switch can do ───────────────


def test_lambda_iam_grants_no_wildcard_actions():
    assert wildcard_action_violations(lambda_role_policy()) == []


def test_lambda_iam_grants_no_wildcard_resources_outside_the_documented_exception():
    assert wildcard_resource_violations(lambda_role_policy()) == []


def test_the_wildcard_resource_exception_is_one_sid_bounded_by_two_actions():
    """The exception is only defensible while it stays this small.

    Application Auto Scaling defines no IAM resource types, so those two calls
    can only be written against ``*``. That is a real widening and it is
    accepted; what is not accepted is a second statement quietly joining the
    allowlist, or these two actions growing into ``application-autoscaling:*``.
    """
    doc = lambda_role_policy()
    wildcard_sids = {st.get("Sid") for st in _statements(doc) if "*" in _as_list(st.get("Resource"))}
    assert wildcard_sids == set(WILDCARD_RESOURCE_ALLOWED_SIDS)
    statement = next(st for st in _statements(doc) if st.get("Sid") in wildcard_sids)
    assert set(_as_list(statement["Action"])) == set(WILDCARD_RESOURCE_ALLOWED_ACTIONS)


def test_lambda_iam_never_reaches_a_data_store():
    """Aurora, ElastiCache, S3, EBS snapshots, DynamoDB — not even a read.

    This is the property that makes an unattended automatic trigger acceptable.
    """
    assert data_store_violations(lambda_role_policy()) == []


def test_lambda_iam_holds_no_destructive_action():
    assert destructive_action_violations(lambda_role_policy()) == []


def test_lambda_iam_permission_surface_is_exactly_the_pinned_set():
    doc = lambda_role_policy()
    granted = {a for st in _statements(doc) for a in _as_list(st.get("Action"))}
    assert granted == set(EXPECTED_ACTIONS), (
        "The kill switch's permission surface changed. Adding a permission to a "
        "thing that runs unattended should take two deliberate edits, not one:\n"
        f"  added:   {sorted(granted - EXPECTED_ACTIONS)}\n"
        f"  removed: {sorted(EXPECTED_ACTIONS - granted)}"
    )


def test_every_statement_is_an_allow_with_a_sid():
    for st in _statements(lambda_role_policy()):
        assert st.get("Effect") == "Allow"
        assert st.get("Sid"), "every statement needs a Sid — the guards key off it"


# ── The Lambda source ────────────────────────────────────────────────────────


def test_lambda_constructs_only_the_four_permitted_clients():
    clients = boto3_clients()
    assert None not in clients, "dynamically-named boto3 client defeats this guard"
    assert set(clients) <= set(ALLOWED_BOTO3_CLIENTS), (
        f"unexpected boto3 clients: {sorted(set(clients) - ALLOWED_BOTO3_CLIENTS)}"
    )


def test_lambda_actually_performs_all_three_documented_actions():
    """A kill switch that quietly stopped doing one of its three jobs would look
    healthy in every other test in this file."""
    calls = lambda_method_calls()
    missing = REQUIRED_LAMBDA_CALLS - calls
    assert not missing, f"kill switch no longer performs: {sorted(missing)}"


def test_lambda_calls_no_destructive_api():
    calls = lambda_method_calls()
    assert not (calls & FORBIDDEN_LAMBDA_CALLS), sorted(calls & FORBIDDEN_LAMBDA_CALLS)
    prefixed = {c for c in calls if c.startswith(FORBIDDEN_LAMBDA_CALL_PREFIXES)}
    assert not prefixed, sorted(prefixed)


def test_lambda_stops_the_runner_and_never_terminates_it():
    calls = lambda_method_calls()
    assert "stop_instances" in calls
    assert "terminate_instances" not in calls, (
        "StopInstances preserves the root volume; TerminateInstances does not. "
        "That difference is the entire argument for letting this run unattended."
    )


# ── The fast tripwire ────────────────────────────────────────────────────────


def test_billing_alarm_is_the_fast_tripwire_and_fails_safe_on_missing_data():
    body = _block_body(
        _read(COST_TF),
        'resource "aws_cloudwatch_metric_alarm" "billing_estimated_charges_high"',
    )
    assert _attr(body, "namespace") == '"AWS/Billing"'
    assert _attr(body, "metric_name") == '"EstimatedCharges"'
    # AWS publishes this metric roughly every 6h; a shorter period spends most of
    # its life in INSUFFICIENT_DATA.
    assert int(_attr(body, "period") or 0) >= 21600
    assert _attr(body, "treat_missing_data") == '"notBreaching"', (
        "'breaching' here would shut production down every 1st of the month, "
        "when the month-to-date metric has not been published yet. This is the "
        "opposite of the right choice for the runner status-check alarm, and "
        "the difference is deliberate."
    )
    assert "aws_sns_topic.cost_kill_switch.arn" in body, (
        "the fast tripwire has to be able to reach the kill switch, or the "
        "~6h-lag path buys nothing over the ~8-12h budget path"
    )


def test_dry_run_is_pinned_false_in_terraform():
    body = _block_body(_read(COST_TF), 'resource "aws_lambda_function" "cost_kill_switch"')
    assert _attr(body, "COST_KILL_SWITCH_DRY_RUN") == '"false"', (
        "A kill switch left in rehearsal mode is a kill switch that does not "
        "exist, and nobody finds out until the month it mattered."
    )


def test_terraform_never_references_a_data_store_resource():
    text = _read(COST_TF)
    for forbidden in ("aws_rds_", "aws_db_", "aws_elasticache_", "aws_s3_bucket", "aws_dynamodb_"):
        assert forbidden not in text, f"{forbidden} has no business in the kill switch"


# ── Runbook ──────────────────────────────────────────────────────────────────


def test_runbook_exists_and_states_the_lag_caveat_and_a_real_baseline():
    text = _read(RUNBOOK).lower()
    assert "lag" in text
    assert "does not make overspend impossible" in text, (
        "the runbook must not let a reader believe this is a hard spending cap"
    )
    assert "cost explorer" in text, "the baseline figure must name where it came from"
    assert re.search(r"\$\s?2\d\d(\.\d\d)?\s*/?\s*(month|mo)", text), (
        "the runbook must carry the real measured monthly baseline, not a guess"
    )
    assert "aws ec2 start-instances" in text, "the recovery command must be in the runbook"


# ── Adversarial: the guards, demonstrated rejecting ──────────────────────────
#
# Each of these takes the REAL policy, applies the exact edit a hurried
# "just make it work" PR would make, and asserts the corresponding checker
# catches it. Without these, a checker that silently stopped checking — a regex
# that no longer matches, a set that was emptied — would leave every test above
# passing on an empty input.


def test_guard_rejects_a_widened_action():
    doc = copy.deepcopy(lambda_role_policy())
    doc["Statement"][1]["Action"] = "*"
    violations = wildcard_action_violations(doc)
    assert violations, "widening an action to '*' must be caught"
    assert "EcsScaleServiceToZero" in violations[0]


def test_guard_rejects_a_service_level_action_wildcard():
    doc = copy.deepcopy(lambda_role_policy())
    doc["Statement"][2]["Action"] = ["ec2:*"]
    assert wildcard_action_violations(doc), "'ec2:*' is a wildcard action too"


def test_guard_rejects_a_widened_resource():
    doc = copy.deepcopy(lambda_role_policy())
    doc["Statement"][1]["Resource"] = "*"
    assert wildcard_resource_violations(doc), "a bare '*' resource must be caught"


def test_guard_rejects_a_structurally_wildcarded_arn():
    doc = copy.deepcopy(lambda_role_policy())
    doc["Statement"][1]["Resource"] = "arn:aws:ecs:*:*:service/*/*"
    assert wildcard_resource_violations(doc), "a wildcarded ARN prefix must be caught"


def test_guard_rejects_a_second_statement_joining_the_wildcard_allowlist():
    doc = copy.deepcopy(lambda_role_policy())
    doc["Statement"].append({"Sid": "JustThisOnce", "Effect": "Allow", "Action": ["ecs:ListTasks"], "Resource": "*"})
    assert wildcard_resource_violations(doc), (
        "the wildcard exception is allowlisted by Sid; a new Sid must not inherit it"
    )


def test_guard_rejects_a_smuggled_data_store_action():
    for action in ("rds:DescribeDBClusters", "s3:GetObject", "elasticache:DescribeCacheClusters"):
        doc = copy.deepcopy(lambda_role_policy())
        doc["Statement"][1]["Action"] = [action]
        assert data_store_violations(doc), f"{action} must be caught"


def test_guard_rejects_a_destructive_action():
    for action in ("ec2:TerminateInstances", "ecs:DeleteService", "rds:DeleteDBCluster"):
        doc = copy.deepcopy(lambda_role_policy())
        doc["Statement"][1]["Action"] = [action]
        assert destructive_action_violations(doc), f"{action} must be caught"


def test_guard_still_accepts_the_legitimate_trailing_wildcard_on_a_log_group():
    """The inverse check: the rules above must not be so blunt they would have
    forced someone to loosen them. A log-group ARN ending in ``:*`` names that
    group's streams and is correct."""
    doc = {
        "Statement": [
            {
                "Sid": "OwnLogsOnly",
                "Effect": "Allow",
                "Action": ["logs:PutLogEvents"],
                "Resource": "arn:aws:logs:us-east-1:000000000000:log-group:/aws/lambda/x:*",
            }
        ]
    }
    assert wildcard_resource_violations(doc) == []
