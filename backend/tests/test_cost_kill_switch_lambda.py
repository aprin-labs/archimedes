"""Behaviour of the cost kill-switch Lambda (infra/lambda/cost_kill_switch/index.py).

``test_cost_kill_switch_guards.py`` pins what the thing is *allowed* to do. This
file pins what it actually *does*, because two of the claims made about it in
``docs/runbooks/cost-kill-switch.md`` and in the PR body are properties of the
control flow that no amount of reading the IAM policy can confirm:

**Order.** Application Auto Scaling *enforces* the scaling floor. Setting
``desiredCount = 0`` while ``MinCapacity = 1`` is still registered gets reverted
within minutes — the kill switch would report success, the SNS mail would arrive,
and the bill would keep running. The floor must go to zero first. Swapping two
adjacent function calls is an easy, plausible edit and nothing else in the repo
would notice.

**Idempotency.** SNS delivery is at-least-once, and both the budget's 120% rung
and the billing alarm point at the same Lambda, so a double fire is expected
rather than exotic. The claim is that the second run leaves observable state
untouched and reports it as a no-op — see ``SKIPPED_ON_SECOND_FIRE`` below for
the one step where that is bought at AWS rather than in the handler, and why.

Hermetic: ``boto3`` is replaced with a recording fake in ``sys.modules`` before
the module is loaded, so this test never constructs a real client, never reads
credentials and never touches the network — and it works whether or not boto3 is
installed. The module is loaded by path (it lives under ``infra/``, not on the
backend import path) with its environment supplied explicitly.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import pytest

LAMBDA_SRC = Path(__file__).resolve().parents[2] / "infra" / "lambda" / "cost_kill_switch" / "index.py"

ENV = {
    "ECS_CLUSTER": "archimedes-cluster",
    "ECS_SERVICE": "archimedes-backend",
    "SCALABLE_TARGET_RESOURCE_ID": "service/archimedes-cluster/archimedes-backend",
    "RUNNER_INSTANCE_ID": "i-0123456789abcdef0",
    "NOTIFY_TOPIC_ARN": "arn:aws:sns:us-east-1:000000000000:archimedes-alerts",
    "RESTORE_MIN_CAPACITY": "1",
    "RESTORE_MAX_CAPACITY": "4",
    "RESTORE_DESIRED_COUNT": "1",
    "COST_KILL_SWITCH_DRY_RUN": "false",
}

# Calls the handler SKIPS on a second fire, because it reads current state first
# and finds nothing to do.
#
# `stop_instances` is deliberately NOT in this set. Idempotency there is bought
# at AWS rather than in the handler: `ec2:DescribeInstances` supports no
# resource-level IAM scoping, so reading the instance's state before acting would
# force the EC2 statement in the policy from one instance ARN to `"*"`. Trading
# that for one redundant API call is the right way round — StopInstances on an
# already-stopped instance succeeds and reports PreviousState=stopped, which is
# how the handler still prints "ALREADY". The test below asserts both halves.
SKIPPED_ON_SECOND_FIRE = {"register_scalable_target", "update_service"}


class _FakeClient:
    def __init__(self, service: str, recorder: list[tuple[str, str, dict[str, Any]]], state: dict[str, Any]):
        self._service = service
        self._recorder = recorder
        self._state = state

    def __getattr__(self, name: str):
        def call(**kwargs: Any) -> dict[str, Any]:
            self._recorder.append((self._service, name, kwargs))
            if self._state.get("raise_on") == (self._service, name):
                raise RuntimeError("simulated AWS failure")
            if name == "describe_scalable_targets":
                return {"ScalableTargets": [{"MinCapacity": self._state["min"], "MaxCapacity": self._state["max"]}]}
            if name == "register_scalable_target":
                self._state["min"] = kwargs["MinCapacity"]
                self._state["max"] = kwargs["MaxCapacity"]
                return {}
            if name == "describe_services":
                return {"services": [{"desiredCount": self._state["desired"]}]}
            if name == "update_service":
                self._state["desired"] = kwargs["desiredCount"]
                return {}
            if name == "stop_instances":
                previous = self._state["instance"]
                self._state["instance"] = "stopping"
                return {
                    "StoppingInstances": [{"PreviousState": {"name": previous}, "CurrentState": {"name": "stopping"}}]
                }
            if name == "publish":
                self._state.setdefault("published", []).append(kwargs)
                return {"MessageId": "fake"}
            raise AssertionError(f"unexpected API call {self._service}.{name}")

        return call


@pytest.fixture
def kill_switch(monkeypatch):
    """The Lambda module, loaded fresh against a recording boto3 fake."""
    calls: list[tuple[str, str, dict[str, Any]]] = []
    state: dict[str, Any] = {"min": 1, "max": 4, "desired": 1, "instance": "running"}

    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = lambda service, *a, **kw: _FakeClient(service, calls, state)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)

    spec = importlib.util.spec_from_file_location("cost_kill_switch_under_test", LAMBDA_SRC)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, calls, state


def _names(calls, service: str | None = None) -> list[str]:
    return [name for svc, name, _ in calls if service is None or svc == service]


def test_it_does_all_three_things_and_says_so(kill_switch):
    module, _calls, state = kill_switch
    result = module.lambda_handler({})

    assert (state["min"], state["max"]) == (0, 0), "autoscaling floor and ceiling pinned"
    assert state["desired"] == 0, "ECS service scaled in"
    assert state["instance"] == "stopping", "runner stopped"
    assert result["errors"] == []
    assert result["dryRun"] is False
    assert len(state["published"]) == 1


def test_the_scaling_floor_is_pinned_BEFORE_the_service_is_scaled_in(kill_switch):
    """The one ordering bug that would make this whole mechanism a no-op.

    With MinCapacity=1 still registered, Application Auto Scaling puts the
    desired count straight back and the kill switch reports a clean success
    while the bill keeps running.
    """
    module, calls, _ = kill_switch
    module.lambda_handler({})
    names = _names(calls)
    assert names.index("register_scalable_target") < names.index("update_service")


def test_firing_twice_changes_nothing_the_second_time(kill_switch):
    module, calls, state = kill_switch

    module.lambda_handler({})
    before = dict(state)
    calls.clear()
    result = module.lambda_handler({})

    re_issued = set(_names(calls)) & SKIPPED_ON_SECOND_FIRE
    assert not re_issued, f"second fire re-issued state-changing calls: {sorted(re_issued)}"
    assert result["errors"] == []

    # The observable state is identical either way — including for the EC2 step,
    # whose redundant StopInstances call AWS itself treats as a no-op.
    assert {k: v for k, v in state.items() if k != "published"} == {k: v for k, v in before.items() if k != "published"}
    body = state["published"][-1]["Message"]
    assert body.count("ALREADY") == 3, "all three steps should report an idempotent no-op"


def test_a_failure_in_one_step_does_not_abort_the_others(kill_switch):
    """A kill switch that stops the runner but bails before scaling the service
    in, because ECS threw once, is worse than useless."""
    module, calls, state = kill_switch
    state["raise_on"] = ("ecs", "update_service")

    with pytest.raises(RuntimeError):
        module.lambda_handler({})

    assert state["min"] == 0 and state["max"] == 0, "autoscaling step still ran"
    assert state["instance"] == "stopping", "EC2 step still ran after the ECS failure"
    assert "publish" in _names(calls, "sns"), "the failure was still reported"
    assert "ecs: FAILED" in state["published"][-1]["Message"]


def test_it_raises_so_a_partial_failure_is_not_a_silent_success(kill_switch):
    """CLAUDE.md § fail-soft: a degraded run must not look clean in the console."""
    module, _, state = kill_switch
    state["raise_on"] = ("ec2", "stop_instances")
    with pytest.raises(RuntimeError, match="completed with 1 error"):
        module.lambda_handler({})


def test_the_notification_carries_the_recovery_command(kill_switch):
    module, _, state = kill_switch
    module.lambda_handler({})
    body = state["published"][-1]["Message"]

    assert "aws application-autoscaling register-scalable-target" in body
    assert "--min-capacity 1 --max-capacity 4" in body, "restore values come from Terraform"
    assert "aws ecs update-service" in body and "--desired-count 1" in body
    assert "aws ec2 start-instances --instance-ids i-0123456789abcdef0" in body
    assert "docs/runbooks/cost-kill-switch.md" in body
    # The order of the recovery steps matters as much as the kill order does.
    assert body.index("register-scalable-target") < body.index("update-service")


def test_the_notification_states_what_was_not_touched(kill_switch):
    module, _, state = kill_switch
    module.lambda_handler({})
    body = state["published"][-1]["Message"]
    for datastore in ("Aurora", "ElastiCache", "S3"):
        assert datastore in body
    assert "availability sacrifice" in body


def test_it_only_ever_constructs_the_four_permitted_clients(kill_switch):
    """The static guard checks the source; this checks what actually ran."""
    module, calls, _ = kill_switch
    module.lambda_handler({})
    assert {svc for svc, _, _ in calls} <= {"application-autoscaling", "ecs", "ec2", "sns"}


def test_it_stops_the_instance_and_never_terminates_it(kill_switch):
    module, calls, _ = kill_switch
    module.lambda_handler({})
    ec2_calls = _names(calls, "ec2")
    assert ec2_calls == ["stop_instances"], f"EC2 surface must be exactly one call, got {ec2_calls}"


def test_a_cloudwatch_alarm_trigger_is_reported_by_name(kill_switch):
    module, _, state = kill_switch
    module.lambda_handler(
        {
            "Records": [
                {
                    "Sns": {
                        "Subject": "ALARM: archimedes-billing-estimated-charges-high",
                        "Message": '{"AlarmName":"archimedes-billing-estimated-charges-high",'
                        '"NewStateValue":"ALARM","NewStateReason":"Threshold Crossed"}',
                    }
                }
            ]
        }
    )
    body = state["published"][-1]["Message"]
    assert "archimedes-billing-estimated-charges-high" in body
    assert "Threshold Crossed" in body


def test_an_unparseable_trigger_does_not_stop_the_kill_path(kill_switch):
    """Reporting is best-effort; the shutdown is not."""
    module, _, state = kill_switch
    result = module.lambda_handler({"Records": [{"Sns": {"Message": "not json at all"}}]})
    assert result["errors"] == []
    assert state["desired"] == 0
