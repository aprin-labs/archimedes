"""The runner EC2 must be able to recover itself (#1402).

``archimedes-runner`` (``infra/runner_ec2.tf``) has wedged four times with one
signature: instance status check impaired, system status check ok, SSM agent
stops pinging. Every recorded recovery was a **human** rebooting the box, which
is why the recorded outages are measured in hours (the longest verified window:
06:12→10:23 CDT on 2026-08-20). The alarms that existed before this change all
page and none of them act, so the mean time to recovery was bounded below by
how long it takes Dan to read a phone.

These tests pin the alarms that close that gap, and — more importantly — pin
the AWS constraint that makes the obvious version of this change silently
useless:

    "The recover action can be used only with StatusCheckFailed_System, not
    with StatusCheckFailed_Instance."
    — AWS, *Add recover actions to Amazon CloudWatch alarms*

``StatusCheckFailed_Instance`` is #1402's actual signature. Someone reaching for
"add auto-recovery to the impaired alarm" reaches for ``ec2:recover``, gets HCL
that reads correctly, and ends up with an alarm whose remediation AWS refuses —
an outage converted into a silence, which CLAUDE.md § fail-soft names as the
defect class. ``test_no_recover_action_on_instance_status_check_alarms`` is the
guard that rejects exactly that edit. The valid automatic action for an
instance-status failure is ``ec2:reboot``, which is also what actually worked
by hand all four times.

Hermetic by construction — the only inputs are three files in the repo. No AWS,
no terraform binary, no network, no env vars, no ``.env``. Terraform
``validate`` cannot catch any of this: an ``ec2:recover`` action on the wrong
metric, an automation alarm that acts on missing data, and a reboot alarm that
races a recover alarm are all syntactically valid HCL that only fails in
production, during the incident the change was written to shorten.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CLOUDWATCH_TF = REPO_ROOT / "infra" / "cloudwatch.tf"
RUNNER_TF = REPO_ROOT / "infra" / "runner_ec2.tf"
RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "runner-ec2-wedge.md"

# Every .tf file that may define an alarm pointed at aws_instance.runner. The
# guards below sweep ALL alarms in these files, not just the ones this change
# added, so a future alarm that reintroduces the defect is caught too.
ALARM_SOURCES = (CLOUDWATCH_TF, RUNNER_TF)

# The EC2 automatic actions, as they appear in an alarm_actions list. The
# region is interpolated (`${var.aws_region}`), so match on the suffix.
REBOOT_ACTION_SUFFIX = "ec2:reboot"
RECOVER_ACTION_SUFFIX = "ec2:recover"

# AWS accepts `ec2:recover` ONLY against this metric. Not the combined
# `StatusCheckFailed` (which is Max(_System, _Instance)), not `_Instance`.
RECOVER_ONLY_METRIC = "StatusCheckFailed_System"

# Actions that must never be automated against a funds-adjacent, exactly-once
# singleton whose host-prep state (#1413's swap + container caps) lives on the
# instance itself. Reboot and recover both preserve it; these do not.
DESTRUCTIVE_ACTIONS = ("ec2:terminate", "ec2:stop")

_ALARM_HEADER = re.compile(r'resource\s+"aws_cloudwatch_metric_alarm"\s+"([A-Za-z0-9_]+)"\s*\{')


def _blocks(path: Path) -> dict[str, str]:
    """Every ``aws_cloudwatch_metric_alarm`` body in ``path``, keyed by tf name.

    Brace-counted rather than regex-terminated: alarm bodies contain nested
    braces (``dimensions = { ... }``, ``${var.project_name}``) and a lazy
    ``\\{.*?\\}`` would truncate at the first inner close-brace, silently
    hiding the ``alarm_actions`` line these guards exist to read.
    """
    text = path.read_text(encoding="utf-8")
    out: dict[str, str] = {}
    for match in _ALARM_HEADER.finditer(text):
        depth, i = 1, match.end()
        while depth and i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        out[match.group(1)] = text[match.end() : i - 1]
    return out


@pytest.fixture(scope="module")
def alarms() -> dict[str, str]:
    merged: dict[str, str] = {}
    for path in ALARM_SOURCES:
        for name, body in _blocks(path).items():
            assert name not in merged, f"duplicate alarm resource name {name!r}"
            merged[name] = body
    assert merged, "no aws_cloudwatch_metric_alarm resources parsed — parser is broken"
    return merged


def _field(body: str, key: str) -> str | None:
    """Value of a scalar ``key = value`` line, quotes stripped."""
    match = re.search(rf'^\s*{key}\s*=\s*("?)([^"\n]*)\1\s*$', body, re.MULTILINE)
    return match.group(2).strip() if match else None


def _actions(body: str, key: str = "alarm_actions") -> list[str]:
    """The elements of an ``alarm_actions``/``ok_actions`` list."""
    match = re.search(rf"{key}\s*=\s*\[(.*?)\]", body, re.DOTALL)
    if not match:
        return []
    return [item.strip() for item in match.group(1).split(",") if item.strip()]


# ── The alarms that make the box self-heal ─────────────────────────────────


def test_reboot_alarm_exists_and_carries_the_ec2_reboot_action(alarms):
    """#1402's signature must trigger the action that actually recovered it."""
    body = alarms.get("runner_instance_reboot")
    assert body is not None, (
        "aws_cloudwatch_metric_alarm.runner_instance_reboot is gone. Without it "
        "nothing acts on #1402's wedge signature — every alarm on this box only pages."
    )
    assert _field(body, "metric_name") == "StatusCheckFailed_Instance"
    assert "aws_instance.runner.id" in body, "alarm is not scoped to the runner instance"

    actions = _actions(body)
    assert any(REBOOT_ACTION_SUFFIX in action for action in actions), (
        f"runner_instance_reboot has no ec2:reboot action — alarm_actions={actions}. "
        "It is then just a third alarm that pages, which is what #1402 already had."
    )


def test_recover_alarm_exists_and_carries_the_ec2_recover_action(alarms):
    """Hardware-level failures get the migrate-to-new-hardware action."""
    body = alarms.get("runner_system_recover")
    assert body is not None, "aws_cloudwatch_metric_alarm.runner_system_recover is gone"
    assert _field(body, "metric_name") == RECOVER_ONLY_METRIC
    assert "aws_instance.runner.id" in body, "alarm is not scoped to the runner instance"

    actions = _actions(body)
    assert any(RECOVER_ACTION_SUFFIX in action for action in actions), (
        f"runner_system_recover has no ec2:recover action — alarm_actions={actions}"
    )


# ── The AWS constraint that makes the obvious edit useless ─────────────────


def test_no_recover_action_on_instance_status_check_alarms(alarms):
    """``ec2:recover`` on ``StatusCheckFailed_Instance`` is rejected by AWS.

    This is the guard, not a formality. #1402's signature IS the instance
    status check, so "add auto-recovery to the impaired alarm" is the natural
    edit and it produces an alarm that can never remediate anything.
    """
    offenders = [
        name
        for name, body in alarms.items()
        if _field(body, "metric_name") != RECOVER_ONLY_METRIC
        and any(RECOVER_ACTION_SUFFIX in action for action in _actions(body))
    ]
    assert not offenders, (
        f"{offenders} attach ec2:recover to a metric AWS refuses it for. "
        f"The recover action is valid ONLY with {RECOVER_ONLY_METRIC} — not with "
        "StatusCheckFailed_Instance, and not with the combined StatusCheckFailed "
        "(which is Max(_System, _Instance)). Use ec2:reboot for OS-level failures."
    )


def test_reboot_and_recover_alarms_do_not_race(alarms):
    """Two automatic actions on one instance need different evaluation windows.

    AWS's own guidance for running a reboot alarm alongside a recover alarm is
    three 1-minute periods for reboot and two for recover. Identical windows
    let both fire on the same datapoint, which is the documented race.
    """
    reboot = alarms["runner_instance_reboot"]
    recover = alarms["runner_system_recover"]

    def window(body: str) -> int:
        return int(_field(body, "period")) * int(_field(body, "evaluation_periods"))

    assert window(reboot) > window(recover), (
        f"reboot window ({window(reboot)}s) must be strictly longer than the recover "
        f"window ({window(recover)}s) so a genuine hardware failure — where both status "
        "checks fail together — recovers onto new hardware instead of racing a reboot."
    )


def test_automation_alarms_never_act_on_missing_data(alarms):
    """A robot must not remediate an absent datapoint.

    ``treat_missing_data = "breaching"`` plus an EC2 action is a feedback loop:
    the reboot blanks the status-check metric while the box is down, the gap
    reads as breaching, and the automation fires again on its own side effect.
    Humans are still paged on absence — the paging alarms keep "breaching".

    Scoped to the runner deliberately. ``nat_status_check_failed`` (vpc.tf's
    fck-nat pair) pairs ``ec2:recover`` with ``breaching`` and would fail this
    check; whether that is a latent defect or a deliberate choice for a
    two-instance egress tier is a separate question from #1402, and changing a
    live NAT alarm's semantics from inside a runner PR would be the kind of
    drive-by this repo's merge rules exist to prevent. Named, not silently
    excluded.
    """
    for name, body in alarms.items():
        if "aws_instance.runner.id" not in body:
            continue
        if not any("arn:aws:automate:" in action for action in _actions(body)):
            continue
        assert _field(body, "treat_missing_data") == "missing", (
            f"{name} carries an EC2 automatic action but treats missing data as "
            f'{_field(body, "treat_missing_data")!r}. Use "missing" so the action '
            "cannot be triggered by the metric gap the action itself creates."
        )


def test_automation_alarms_still_page_a_human(alarms):
    """Self-healing must not become self-hiding.

    A box that silently reboots itself every night looks healthy on a
    dashboard. Every automatic action fires alongside the SNS topic so the
    recurrence is visible even when the outage is not.
    """
    for name, body in alarms.items():
        actions = _actions(body)
        if not any("arn:aws:automate:" in action for action in actions):
            continue
        assert any("aws_sns_topic.alerts.arn" in action for action in actions), (
            f"{name} remediates without paging — a repeating wedge would be invisible."
        )


def test_no_destructive_automation_on_the_singleton_runner(alarms):
    """Reboot and recover preserve the instance; stop/terminate do not.

    The runner is a funds-adjacent exactly-once singleton (#1065 decision #1)
    whose #1413 host-prep state (swapfile, per-container memory/CPU caps) lives
    on the instance itself.
    """
    for name, body in alarms.items():
        if "aws_instance.runner.id" not in body:
            continue
        for action in _actions(body) + _actions(body, "ok_actions"):
            for destructive in DESTRUCTIVE_ACTIONS:
                assert destructive not in action, f"{name} automates {destructive} against the singleton runner"


# ── SSM-agent liveness, by proxy ───────────────────────────────────────────


def test_log_silence_alarm_detects_the_quiet_box(alarms):
    """The only free machine-visible shadow of "the SSM agent stopped pinging".

    SSM's liveness signal (``LastPingDateTime``) is an API field, not a metric,
    so it cannot be alarmed on directly. The runner log group can be, and
    #1402's forensics put the agent's last log event and SSM's last ping in the
    same minute.
    """
    body = alarms.get("runner_log_silence")
    assert body is not None, "aws_cloudwatch_metric_alarm.runner_log_silence is gone"
    assert _field(body, "namespace") == "AWS/Logs"
    assert _field(body, "metric_name") == "IncomingLogEvents"
    assert _field(body, "comparison_operator") == "LessThanThreshold"
    assert "aws_cloudwatch_log_group.runners.name" in body, (
        "the silence alarm must watch the runner log group, not some other group"
    )
    assert _field(body, "treat_missing_data") == "breaching", (
        "CloudWatch publishes NOTHING for an idle log group rather than a zero. "
        'Any value but "breaching" leaves this alarm asleep through the exact '
        "total-silence condition it exists to detect."
    )
    assert not any("arn:aws:automate:" in action for action in _actions(body)), (
        "log silence is an inference (a stopped container looks identical), so it must page rather than reboot."
    )


# ── Comment truthfulness: the tf points at a runbook that must exist ───────


def test_runbook_exists_and_documents_the_direct_ssm_check(alarms):
    """``cloudwatch.tf`` tells the reader the direct check lives in the runbook.

    A prose claim the tree does not back is the same defect as an unenforced
    guard (CLAUDE.md § "A guard must be shown to reject something", rule 4),
    just harder to grep for.
    """
    tf = CLOUDWATCH_TF.read_text(encoding="utf-8")
    assert "docs/runbooks/runner-ec2-wedge.md" in tf, (
        "cloudwatch.tf no longer references the runbook it defers the direct SSM-agent check to"
    )
    assert RUNBOOK.exists(), f"{RUNBOOK} is referenced by infra/cloudwatch.tf but missing"

    runbook = RUNBOOK.read_text(encoding="utf-8")
    assert "describe-instance-information" in runbook, (
        "the runbook must carry the ssm:DescribeInstanceInformation call — it is the "
        "documented detection that stands in for the SSM-agent metric AWS does not "
        "publish, and cloudwatch.tf promises the reader it is here."
    )
    assert "LastPingDateTime" in runbook, "the runbook must name the field that actually answers 'is the agent alive'"


def test_new_alarms_are_visible_on_the_machines_dashboard(alarms):
    """An alarm nobody can see is an alarm nobody trusts."""
    tf = CLOUDWATCH_TF.read_text(encoding="utf-8")
    widget = tf.split('dashboard_name = "${var.project_name}-machines-and-network"', 1)
    assert len(widget) == 2, "machines-and-network dashboard is gone"
    for name in ("runner_instance_reboot", "runner_system_recover", "runner_log_silence"):
        assert f"aws_cloudwatch_metric_alarm.{name}.arn" in widget[1], (
            f"{name} is not listed on the machines-and-network dashboard's alarm widget"
        )
