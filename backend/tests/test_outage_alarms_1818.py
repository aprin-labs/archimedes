"""The 2026-09-03 detection gaps, and a paging destination that must be chosen (#1818 P5).

Issue #1818 P5 reads "there was no alarm, the owner found out by using the
site". **The first half is not what happened**, and a guard file built on it
would pin the wrong invariants. Read from the AWS account on 2026-09-03 (all
times UTC):

* 13:38:46 ``archimedes-alb-unhealthy-hosts`` OK → ALARM (outage began 13:29)
* 13:39:16 ``archimedes-alb-5xx-rate-high`` OK → ALARM, flapping to 13:45
* 13:35–13:50 SNS ``NumberOfNotificationsDelivered`` = 5, ``…Failed`` = 0
* 15:06:46 unhealthy-hosts ALARM → OK, one more delivered

Two alarms fired and six emails were delivered to a confirmed subscriber, the
first 9m46s into a 94-minute outage — and the owner still found it by loading
the site. So P5 is three gaps, of which Terraform owns two:

1. **Detection shape.** Nothing watched what was already abnormal ten hours
   earlier (Aurora connections flat at 33 from 03:33; ECS memory ramping).
   The alarms guarded below close that; ``aurora_connections_wedge`` fires at
   ~03:48 and is the only reason this set is worth applying.
2. **Detection latency.** 9m46s is the 5-of-5-minute window plus lag. The
   unhealthy-hosts retune to 2-of-2 drops three of the five one-minute
   datapoints, so it buys back ~3 minutes; the 2m46s evaluation lag stays.
3. **The page did not reach the owner.** Nothing in Terraform fixes that. What
   the code can do is refuse to leave the destination unchosen —
   ``var.owner_alert_email`` has no default — and refuse to let that choice be
   the mailbox that already received six ignored emails, which is what
   ``test_the_owner_subscription_refuses_to_duplicate_the_working_one``
   guards. Choosing the channel is the owner's, and is the open half of P5.

Two anti-goals are load-bearing here and each has a test:

* **The working subscription is not touched.** ``alerts_email`` is live,
  confirmed, and delivering. SNS keys a subscription by (topic, protocol,
  endpoint), so pointing both resources at one address would give them one
  ``SubscriptionArn`` — and an apply dropping either would ``Unsubscribe`` the
  one the other still claims, leaving zero subscribers while state says
  otherwise. That is #1818's own failure mode manufactured by its fix.
* **The alarms stay off the ECS task definition's dependency chain.** Measured:
  with direct resource references, ``terraform plan -target=`` on either ECS
  alarm proposed replacing the task definition with ``PAPER_ADVANCE_ENABLED
  "false" -> "true"`` — an observability apply re-arming the loop that caused
  the outage.

Hermetic: reads tracked files off disk (``infra/cloudwatch.tf``,
``variables.tf``, ``terraform.tfvars.example``, ``README.md``, the DR runbook,
``.gitignore``) and runs one regex. No AWS, no terraform binary, no network,
no ``archimedes`` import.

The HCL reader is imported from ``test_cloudwatch_alarms`` rather than copied.
Six files in this directory each carry their own copy of that comment- and
string-aware brace scanner; a seventh would be the reuse smell, and this file
reads the same ``infra/cloudwatch.tf`` those helpers already default to and are
already exercised against.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.test_cloudwatch_alarms import (
    SNS_ACTION,
    _attr,
    _block,
    _body_at,
    _int_attr,
    _nested,
    _string_attr,
    _tf_source,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
VARIABLES_TF = REPO_ROOT / "infra" / "variables.tf"
TFVARS_EXAMPLE = REPO_ROOT / "infra" / "terraform.tfvars.example"
INFRA_README = REPO_ROOT / "infra" / "README.md"
DR_RUNBOOK = REPO_ROOT / "infra" / "runbooks" / "disaster-recovery.md"
INFRA_DIR = REPO_ROOT / "infra"

OWNER_VAR = "owner_alert_email"

# ── Numbers taken from the incident, not invented ───────────────────────────
# issue #1818 § Timeline, 03:31–03:33 row: "Aurora DatabaseConnections 16 → 33
# and flat at 33 for 11.5 h; CPU ~4%". 16 is the healthy steady state this
# fleet returns to; 33 is where the wedge parked. A connections alarm is only
# an #1818 detector if its threshold lies strictly between them.
AURORA_CONNECTIONS_HEALTHY = 16
AURORA_CONNECTIONS_WEDGED = 33


# ── The five alarms, as the issue specifies them ────────────────────────────
# `window_s` is the DURATION the issue asks for. It is checked against
# `period * datapoints_to_alarm` rather than pinned field-by-field, because
# those two fields are the pair that can drift apart while each still looks
# reasonable on its own line.
ALARM_SPECS: dict[str, dict[str, object]] = {
    # "ALB UnHealthyHostCount >= 1 for 2 minutes"
    "alb_unhealthy_hosts": {
        "namespace": "AWS/ApplicationELB",
        "metric_name": "UnHealthyHostCount",
        "statistic": "Maximum",
        "comparison_operator": "GreaterThanOrEqualToThreshold",
        "threshold": 1,
        "period": 60,
        "window_s": 120,
    },
    # "HTTPCode_ELB_5XX_Count >= 5 in 5 minutes"
    "alb_elb_5xx_count": {
        "namespace": "AWS/ApplicationELB",
        "metric_name": "HTTPCode_ELB_5XX_Count",
        "statistic": "Sum",
        "comparison_operator": "GreaterThanOrEqualToThreshold",
        "threshold": 5,
        "period": 300,
        "window_s": 300,
    },
    # "ECS service MemoryUtilization max > 85% for 5 minutes"
    "ecs_service_memory_high": {
        "namespace": "AWS/ECS",
        "metric_name": "MemoryUtilization",
        "statistic": "Maximum",
        "comparison_operator": "GreaterThanThreshold",
        "threshold": 85,
        "period": 60,
        "window_s": 300,
    },
    # "ECS CPUUtilization > 90% for 10 minutes"
    "ecs_service_cpu_high": {
        "namespace": "AWS/ECS",
        "metric_name": "CPUUtilization",
        "statistic": "Average",
        "comparison_operator": "GreaterThanThreshold",
        "threshold": 90,
        "period": 60,
        "window_s": 600,
    },
    # "RDS DatabaseConnections > 30 for 15 minutes (today's wedge sat at 33)"
    "aurora_connections_wedge": {
        "namespace": "AWS/RDS",
        "metric_name": "DatabaseConnections",
        "statistic": "Average",
        "comparison_operator": "GreaterThanThreshold",
        "threshold": 30,
        "period": 300,
        "window_s": 900,
    },
}

ALARM_NAMES = tuple(ALARM_SPECS)
ECS_ALARMS = ("ecs_service_memory_high", "ecs_service_cpu_high")


def _alarm(name: str) -> str:
    return _block("resource", "aws_cloudwatch_metric_alarm", name)


def _dimensions(body: str) -> str:
    """Dimension map text, whether written on one line or as a block.

    ``_attr`` reads a single-line right-hand side, so on the multi-entry ECS
    alarms it returns a bare ``"{"`` — and ``"TargetGroup" not in "{"`` passes
    vacuously. That is the same class of defect this file is guarding against,
    so the reader handles both spellings rather than the .tf being bent to
    match a weaker reader.
    """
    match = re.search(r"^[ \t]*dimensions[ \t]*=[ \t]*\{[ \t]*$", body, re.MULTILINE)
    if match:
        return _body_at(body, body.index("{", match.start()))
    return _attr(body, "dimensions")


def _variable_block(name: str) -> str:
    return _block("variable", name, src=VARIABLES_TF.read_text(encoding="utf-8"))


# ── 1. The topic can reach a human ──────────────────────────────────────────


class TestThePagingDestinationIsChosenDeliberately:
    """Gap (3): the delivered page did not reach the owner.

    Terraform cannot make anyone read their mail. It can refuse to let the
    destination be unchosen, or be a duplicate of the mailbox that already
    received six ignored emails. That is all these tests assert — no more.
    """

    def test_the_owner_subscription_exists_and_is_unconditional(self) -> None:
        """No ``count``, because a ``count`` is a way for this to quietly not exist.

        ``alerts_email`` next to it is ``count``-gated on a variable that
        defaults to ``""`` and is in no tfvars, so a bare apply silently drops
        it. That gate is a live landmine (see
        ``test_the_working_subscription_is_left_alone``); this resource does
        not get one.
        """
        body = _block("resource", "aws_sns_topic_subscription", "owner_alerts_email")
        assert "aws_sns_topic.alerts.arn" in _attr(body, "topic_arn")
        assert _string_attr(body, "protocol") == "email"
        assert _attr(body, "endpoint").strip() == f"var.{OWNER_VAR}"
        assert not re.search(r"^\s*count\s*=", body, re.MULTILINE), (
            "owner_alerts_email is count-gated; a false condition makes it zero resources "
            "and the apply still succeeds, so the destination this variable exists to force "
            "a decision about would silently not exist"
        )

    def test_owner_alert_email_has_no_default(self) -> None:
        """The load-bearing guard in this file.

        With a default — ``""``, a placeholder, anything — the question "who
        actually sees this" goes unanswered and the apply succeeds anyway.
        Without one, Terraform refuses to plan until a destination is named,
        so it is re-answered on every apply. Same reasoning that keeps
        ``aurora_master_password`` defaultless; it is the only other
        defaultless variable in the file.
        """
        body = _variable_block(OWNER_VAR)
        assert not re.search(r"^\s*default\s*=", body, re.MULTILINE), (
            f"var.{OWNER_VAR} has a default, so an apply can now succeed without anyone "
            "deciding where a page goes. #1818 P5's measured finding is that alarms fired and "
            "six emails were delivered on 2026-09-03 and still reached nobody — an undecided "
            "destination is the same defect with fewer steps. If an apply is blocked on this, "
            "supply the address in infra/terraform.tfvars; do not give the variable a default."
        )

    def test_the_validation_rejects_an_unusable_destination(self) -> None:
        """Runs the declared regex rather than pinning its text.

        ``""`` is not a hypothetical bad input: it is the tracked value of the
        sibling ``alarm_email`` in ``terraform.tfvars.example``, i.e. the
        spelling this repo already reaches for when nobody has decided.
        """
        pattern = self._validation_regex()
        for rejected in ("", "   ", "dan", "dan@example", "dan@ex ample.com", "@archimedes-arc.com"):
            assert not re.match(pattern, rejected), (
                f"the owner_alert_email validation accepts {rejected!r}, which cannot receive mail"
            )
        for accepted in ("ops@archimedes-arc.com", "alerts+prod@archimedes-arc.com"):
            assert re.match(pattern, accepted), (
                f"the validation rejects {accepted!r}; an over-strict rule blocks every apply"
            )

    @staticmethod
    def _validation_regex() -> str:
        body = _variable_block(OWNER_VAR)
        match = re.search(r"regex\(\s*\"(.+?)\"\s*,", body)
        assert match, f"var.{OWNER_VAR} has no regex() validation — an empty string would apply cleanly"
        # The HCL string literal escapes each backslash; recover the pattern.
        return match.group(1).replace("\\\\", "\\")

    def test_no_personal_address_is_committed(self) -> None:
        """The public-repo half of the requirement.

        The live destination is a personal inbox and this repository is
        public. The realistic mistake is not a ``.tf`` default (that one is
        already blocked above) — it is filling the real address into
        ``terraform.tfvars.EXAMPLE``, which is tracked and sits right next to
        the gitignored file it is a template for. So the scan covers every
        tracked ``.tf`` and ``.tfvars*`` under ``infra/``.
        """
        candidates = sorted(INFRA_DIR.rglob("*.tf")) + sorted(INFRA_DIR.rglob("*.tfvars*"))
        for path in candidates:
            if ".terraform" in path.parts or path.name == "terraform.tfvars":
                continue  # the gitignored real file is where the address belongs
            for line in path.read_text(encoding="utf-8").splitlines():
                # The assigned VALUE only — an "@" in a trailing `# e.g. you@…`
                # comment is documentation, not a committed address.
                assigned = re.match(rf"\s*{OWNER_VAR}\s*=\s*\"([^\"]*)\"", line)
                if assigned and "@" in assigned.group(1):
                    pytest.fail(
                        f"{path.relative_to(REPO_ROOT)} carries a literal address for "
                        f"{OWNER_VAR}: {assigned.group(1)!r}. This repository is public; the "
                        "real address goes in infra/terraform.tfvars, which .gitignore excludes."
                    )

    def test_the_file_the_address_belongs_in_is_gitignored(self) -> None:
        """Makes the advice above safe to follow.

        Every doc this PR touches says "put it in infra/terraform.tfvars".
        That is only sound advice while .gitignore actually excludes that file
        and still un-excludes the example.
        """
        ignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        assert "infra/*.tfvars" in ignore, (
            ".gitignore no longer excludes infra/*.tfvars — every doc in this PR tells the "
            "operator to put a personal address there, and it would now be committable"
        )
        assert "!infra/*.tfvars.example" in ignore

    def test_the_owner_subscription_refuses_to_duplicate_the_working_one(self) -> None:
        """A precondition, not a ``count`` — refuse to plan, never destroy.

        SNS keys a subscription by (topic, protocol, endpoint), so the same
        address in both variables gives the two resources ONE
        ``SubscriptionArn``. Standing one of them down with ``count`` would
        then call ``Unsubscribe`` on the ARN the other still claims: zero
        subscribers, state saying otherwise — #1818's failure mode rebuilt by
        its own fix. Failing the plan has no such path.

        It is the right answer on the merits too. The measured finding is that
        six emails reached ``alarm_email``'s address on 2026-09-03 and none
        reached the owner's attention, so a second copy to that same mailbox
        would be a no-op dressed as a fix.
        """
        body = _block("resource", "aws_sns_topic_subscription", "owner_alerts_email")
        lifecycle = _nested(body, "lifecycle")
        precondition = _nested(lifecycle, "precondition")
        condition = _attr(precondition, "condition")
        assert f"var.{OWNER_VAR}" in condition and "var.alarm_email" in condition
        assert "!=" in condition, (
            f"the precondition does not require var.{OWNER_VAR} to DIFFER from var.alarm_email; "
            "the two resources could then hold one SNS subscription ARN"
        )

    def test_the_working_subscription_is_left_alone(self) -> None:
        """``alerts_email`` is live, confirmed, and delivering — do not touch it.

        Its ``count`` gate is a real landmine (a bare apply without
        ``TF_VAR_alarm_email`` unsubscribes the only subscription this topic
        has), but that landmine predates #1818 P5 and closing it
        means capturing the applied address in ``terraform.tfvars`` — the
        owner's to do. What this PR must not do is *widen* it: adding another
        term to the condition would make a plain apply destroy the working
        subscription in a new case.
        """
        body = _block("resource", "aws_sns_topic_subscription", "alerts_email")
        assert _attr(body, "count").strip() == 'var.alarm_email == "" ? 0 : 1', (
            "alerts_email's count changed. It gates the ONLY subscription that has ever "
            "delivered a page on this topic; any new false case destroys it."
        )
        assert _attr(body, "endpoint").strip() == "var.alarm_email"


# ── 2. The five alarms ──────────────────────────────────────────────────────


class TestOutageAlarmShapes:
    @pytest.mark.parametrize("name", ALARM_NAMES)
    def test_metric_coordinates_match_the_issue(self, name: str) -> None:
        body = _alarm(name)
        spec = ALARM_SPECS[name]
        assert _string_attr(body, "namespace") == spec["namespace"]
        assert _string_attr(body, "metric_name") == spec["metric_name"]
        assert _string_attr(body, "statistic") == spec["statistic"]
        assert _string_attr(body, "comparison_operator") == spec["comparison_operator"]
        assert _int_attr(body, "threshold") == spec["threshold"]

    @pytest.mark.parametrize("name", ALARM_NAMES)
    def test_the_window_is_the_duration_the_issue_asks_for(self, name: str) -> None:
        """Both ``period`` and the ``period × datapoints_to_alarm`` product.

        The product alone is NOT sufficient, and assuming it was is a real hole
        this file shipped with. A ``Sum`` alarm applies its threshold *per
        period*, so reshuffling ``alb_elb_5xx_count`` from 300 × 1 to 60 × 5
        holds the product at 300s and keeps ``datapoints_to_alarm ==
        evaluation_periods`` — passing both this test and the consecutive-
        datapoints test — while silently changing the alarm from ">= 5 errors
        in five minutes" to ">= 5 errors in each of five consecutive minutes",
        i.e. >= 25. That is a 5x weakening with no failing test.

        So ``period`` is pinned directly. The product is kept as well, because
        it is what catches the two fields drifting apart while each still reads
        plausibly on its own line — the shape that turned the unhealthy-hosts
        alarm into a 5-minute window in the first place.
        """
        body = _alarm(name)
        period = _int_attr(body, "period")
        assert period == ALARM_SPECS[name]["period"], (
            f"{name} uses period {period}s but issue #1818 P5 specifies "
            f"{ALARM_SPECS[name]['period']}s. For a Sum statistic the threshold applies "
            f"PER PERIOD, so a shorter period with proportionally more datapoints is a "
            f"threshold increase, not the same alarm."
        )
        window_s = period * _int_attr(body, "datapoints_to_alarm")
        assert window_s == ALARM_SPECS[name]["window_s"], (
            f"{name} breaches after {window_s}s but issue #1818 P5 specifies {ALARM_SPECS[name]['window_s']}s"
        )

    @pytest.mark.parametrize("name", ALARM_NAMES)
    def test_the_breaching_datapoints_are_consecutive(self, name: str) -> None:
        """ "for N minutes" must mean N *sustained* minutes.

        M-of-N damping (``datapoints_to_alarm < evaluation_periods``) is a
        deliberate tool elsewhere in this file for noisy signals, but here it
        would silently stretch every window past its stated length.
        """
        body = _alarm(name)
        assert _int_attr(body, "datapoints_to_alarm") == _int_attr(body, "evaluation_periods")

    @pytest.mark.parametrize("name", ALARM_NAMES)
    def test_it_pages_and_is_tagged(self, name: str) -> None:
        body = _alarm(name)
        assert SNS_ACTION in _attr(body, "alarm_actions")
        assert SNS_ACTION in _attr(body, "ok_actions")
        assert "var.project_name" in _attr(body, "tags")


# ── 3. The thresholds are tied to the incident, not to round numbers ────────


class TestThresholdsAreTiedToTheIncident:
    def test_the_connections_alarm_sits_between_healthy_and_wedged(self) -> None:
        """30 is not a round number, it is 33 minus headroom.

        Above 33 the alarm cannot see the wedge; at or below 16 it fires on a
        healthy fleet forever and gets muted, which is worse than absent.
        """
        threshold = _int_attr(_alarm("aurora_connections_wedge"), "threshold")
        assert AURORA_CONNECTIONS_HEALTHY < threshold < AURORA_CONNECTIONS_WEDGED, (
            f"threshold {threshold} is outside ({AURORA_CONNECTIONS_HEALTHY}, "
            f"{AURORA_CONNECTIONS_WEDGED}) — the 2026-09-03 wedge sat flat at "
            f"{AURORA_CONNECTIONS_WEDGED} while the healthy fleet sits at "
            f"{AURORA_CONNECTIONS_HEALTHY}"
        )

    def test_the_wedge_alarm_is_far_below_the_pool_pressure_alarm(self) -> None:
        """Two alarms on one metric only earn their keep if they mean different things."""
        wedge = _int_attr(_alarm("aurora_connections_wedge"), "threshold")
        pressure = _int_attr(_alarm("aurora_connections_high"), "threshold")
        assert wedge < pressure, (
            "the wedge alarm must trip well before the pool-pressure alarm; otherwise it is "
            "a duplicate and the 2026-09-03 signature (flat at 33, CPU 4%) stays invisible"
        )

    def test_the_elb_5xx_alarm_watches_the_balancer_not_the_target(self) -> None:
        """Two different metrics, and the alarm must stay on the ELB-side one.

        ``alb_5xx_high`` counts ``HTTPCode_Target_5XX_Count`` — 5xx the backend
        produced and the ALB relayed. This alarm counts
        ``HTTPCode_ELB_5XX_Count`` — 5xx the balancer generated itself.

        On 2026-09-03 the *ELB-side* metric published no datapoints at all (all
        of ``HTTPCode_ELB_5XX/500/502/503/504_Count`` return zero datapoints for
        the day, while ``RequestCount`` returns them over the same minutes), so
        this alarm would not have fired. The only 5xx were four target-side
        responses at 13:31–13:33Z, which is what put ``alb_5xx_rate_high``
        (#418) into ALARM at 13:39:16Z. The alarm is therefore general ELB-side
        cover for a metric nothing watched — 1,254 errors on UTC day
        2026-08-20 — not an #1818 detector.

        What this test pins is the split: ``HTTPCode_ELB_5XX_Count`` has no
        ``TargetGroup`` dimension, so adding one selects nothing and parks the
        alarm in INSUFFICIENT_DATA; and if ``alb_5xx_high`` ever moves off the
        target-side metric, this alarm becomes a duplicate rather than a
        complement.
        """
        body = _alarm("alb_elb_5xx_count")
        dimensions = _dimensions(body)
        assert "TargetGroup" not in dimensions, (
            "HTTPCode_ELB_5XX_Count is published per LoadBalancer (and per AZ), never per "
            "TargetGroup; a TargetGroup dimension matches no metric and the alarm can never fire"
        )
        assert "aws_lb.main.arn_suffix" in dimensions
        assert _string_attr(_alarm("alb_5xx_high"), "metric_name") == "HTTPCode_Target_5XX_Count", (
            "the pre-existing alarm no longer watches the target-side metric, so the new "
            "ELB-side alarm may now be a duplicate rather than the complement it was added as"
        )

    def test_the_memory_alarm_reads_the_max_across_tasks(self) -> None:
        """``Average`` would have hidden this incident.

        The ramp to 100% ran on the two wedged tasks while two fresh
        replacements sat near idle; averaged across four tasks that reads ~50%
        at the moment the OOM killer was about to fire.
        """
        assert _string_attr(_alarm("ecs_service_memory_high"), "statistic") == "Maximum"

    @pytest.mark.parametrize("name", ECS_ALARMS)
    def test_the_ecs_alarms_name_the_live_cluster_and_service(self, name: str) -> None:
        """An alarm on coordinates nothing publishes reads as calm forever.

        Cross-checked against the product-health dashboard, which plots the
        same two metrics at the same coordinates and is known to have data.
        """
        dimensions = _dimensions(_alarm(name))
        assert "ClusterName = data.aws_ecs_cluster.main.cluster_name" in dimensions
        assert "ServiceName = data.aws_ecs_service.backend.service_name" in dimensions

        metric = _string_attr(_alarm(name), "metric_name")
        dashboard = _block("resource", "aws_cloudwatch_dashboard", "product_health")
        assert f'["AWS/ECS", "{metric}", "ClusterName", ' in dashboard, (
            f"the dashboard no longer plots AWS/ECS {metric} — that dashboard is the evidence "
            "these coordinates carry data, so verify the alarm before trusting it"
        )

    @pytest.mark.parametrize("name", ECS_ALARMS)
    def test_the_ecs_alarms_stay_off_the_task_definitions_dependency_chain(self, name: str) -> None:
        """The measured reason these dimensions go through data sources.

        Referencing ``aws_ecs_service.backend`` directly makes the alarm depend
        on ``aws_ecs_task_definition.backend``. A targeted plan on 2026-09-03
        then proposed replacing the task definition with
        ``PAPER_ADVANCE_ENABLED "false" -> "true"`` and
        ``PLATFORM_ADMIN_WALLETS "0x2a29…5105" -> ""`` — applying an
        observability alarm would have re-armed the loop that caused the very
        outage the alarm exists to detect. Dropping the two ECS alarms from
        that command took the plan from "6 to add, 1 to change, 1 to destroy"
        to "3 to add, 1 to change, 0 to destroy". A data source is a read and
        carries no such edge.
        """
        for value in re.findall(r"=\s*(\S+)\s*$", _dimensions(_alarm(name)), re.MULTILINE):
            assert value.startswith("data."), (
                f"{name}'s dimension value {value!r} is a direct resource reference, which puts "
                "aws_ecs_task_definition.backend inside any `terraform apply -target=` of this "
                "alarm. Use the data sources in cloudwatch.tf instead."
            )

    def test_the_ecs_data_sources_fail_loudly_on_a_rename(self) -> None:
        """The property a hardcoded dimension string would not have.

        Reading the cluster and service by name is what makes a rename fail
        the plan, rather than leaving an alarm quietly watching a metric
        nobody publishes.
        """
        cluster = _block("data", "aws_ecs_cluster", "main")
        assert _attr(cluster, "cluster_name").strip() == "aws_ecs_cluster.main.name"

        service = _block("data", "aws_ecs_service", "backend")
        assert _string_attr(service, "service_name") == "${var.project_name}-backend"
        assert _attr(service, "cluster_arn").strip() == "data.aws_ecs_cluster.main.arn"

    def test_the_alarms_that_would_have_caught_this_one(self) -> None:
        """Records the honest subset, so "five alarms" is never read as "five nets".

        Replayed on the 2026-09-03 numbers: connections held above 30 from
        ~03:33 (fires ~03:48, the only one that beats the 13:38:46 alarm that
        actually fired that day), both targets unhealthy from 13:29 (~13:32),
        memory crossing 85% ~68 min into the 13:31–15:01 ramp (~14:39). The
        ALB logged four 504s against a threshold of five, and CPU never left
        single digits — those two are saturation cover, not detectors.
        """
        detectors = ("alb_unhealthy_hosts", "ecs_service_memory_high", "aurora_connections_wedge")
        saturation_cover = ("alb_elb_5xx_count", "ecs_service_cpu_high")
        assert set(detectors) | set(saturation_cover) == set(ALARM_NAMES)
        for name in detectors:
            body = _alarm(name)
            assert "1818" in _string_attr(body, "alarm_description"), (
                f"{name} is one of the three alarms that would have fired on 2026-09-03; its "
                "description must cite #1818 so whoever receives the page at 03:00 can find "
                "the incident write-up"
            )


# ── 4. Documented where an operator will actually look ──────────────────────


class TestTheApplyPathDocumentsIt:
    def test_the_variable_is_declared_in_variables_tf(self) -> None:
        """The issue asks for variables.tf specifically, not cloudwatch.tf.

        ``alarm_email`` is declared inside ``cloudwatch.tf`` and is the one
        operational variable an operator reading ``variables.tf`` would miss.
        """
        assert f'variable "{OWNER_VAR}"' in VARIABLES_TF.read_text(encoding="utf-8")
        assert f'variable "{OWNER_VAR}"' not in _tf_source()

    def test_apply_sh_will_block_on_an_unset_value(self) -> None:
        """``apply.sh`` derives its required list by grepping the example file.

        It matches ``^<name> =`` only, so an entry written any other way (or
        omitted) drops out of the preflight silently — and this variable's
        whole purpose is to not be silently absent.
        """
        example = TFVARS_EXAMPLE.read_text(encoding="utf-8")
        assert re.search(rf"^{OWNER_VAR}[ \t]*=", example, re.MULTILINE), (
            f"{OWNER_VAR} is missing from terraform.tfvars.example (or not written as a "
            "top-level assignment), so infra/apply.sh will not include it in the "
            "missing-or-empty preflight it derives from that file"
        )

    @pytest.mark.parametrize("doc", (INFRA_README, DR_RUNBOOK))
    def test_the_operator_docs_name_it(self, doc: Path) -> None:
        """README = the apply runbook; disaster-recovery.md = the alarm philosophy."""
        assert OWNER_VAR in doc.read_text(encoding="utf-8"), (
            f"{doc.relative_to(REPO_ROOT)} does not mention {OWNER_VAR}; an operator following "
            "it would hit a hard 'No value for required variable' with no explanation to hand"
        )
