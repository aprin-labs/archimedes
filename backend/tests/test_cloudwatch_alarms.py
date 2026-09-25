"""The reveal-reconcile and deploy-drift alarms must actually be wired (#1596).

Three failure shapes motivate this file, and none of them is visible to
``terraform validate`` — every one is syntactically perfect HCL:

1. **A filter pointed at the wrong log group.** The chain/oracle pairs watch
   ``/archimedes/app`` (the Fargate web tier). The reveal-reconcile literals
   are logged by the *agent* loop, which runs on the EC2 runner box and ships
   to ``/archimedes/runners``. A filter naming the wrong group matches nothing,
   forever, while reading as correct.
2. **A filter pattern that no longer matches the log line.** These filters key
   on literals inside ``agent_runner.py``'s log messages. Reword the message —
   an ordinary, blameless edit — and the alarm silently stops seeing anything.
   So the patterns are checked against the Python source, not just spelled.
3. **An alarm watching a namespace/metric/dimension nothing publishes.** The
   deploy-drift alarm's coordinates must equal the constants the Lambda probe
   puts data with. A mismatch yields INSUFFICIENT_DATA, which on a
   ``treat_missing_data = "breaching"`` alarm at least pages — and on any other
   reads as calm.

All three are the same defect as the one CLAUDE.md § fail-soft names: an alarm
that cannot fire is indistinguishable from a system that is fine.

Hermetic by construction: the only inputs are files in the repo. No AWS, no
terraform binary, no network, no env vars, no ``.env``, and no ``archimedes``
import. The probe's pure verdict logic is exercised for real (the last class
below) rather than pinned as text — it is ordinary Python and there is no
reason to trust it un-run.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CLOUDWATCH_TF = REPO_ROOT / "infra" / "cloudwatch.tf"
MAIN_TF = REPO_ROOT / "infra" / "main.tf"
AGENT_RUNNER_PY = REPO_ROOT / "backend" / "archimedes" / "chain" / "agent_runner.py"
DRIFT_LAMBDA_PY = REPO_ROOT / "infra" / "lambda" / "deploy_drift" / "index.py"

SNS_ACTION = "aws_sns_topic.alerts.arn"

# The log group the agent loop ships to (runner_ec2.tf), NOT the web tier's.
RUNNERS_LOG_GROUP = "aws_cloudwatch_log_group.runners.name"


# ── A very small HCL reader ──────────────────────────────────────────────────
# Enough to slice a named block and read its scalar attributes. Deliberately
# comment- and string-aware: `#` appears inside alarm_description strings in
# this file and `{`/`}` appear inside "${...}" interpolations, so a naive
# brace count or a naive comment strip both cut blocks short — silently
# shrinking what is under test, which is the failure mode this whole file
# exists to prevent.


def _tf_source(path: Path = CLOUDWATCH_TF) -> str:
    assert path.is_file(), f"missing {path}"
    return path.read_text(encoding="utf-8")


def _body_at(src: str, brace_index: int) -> str:
    depth = 0
    i = brace_index
    in_string = False
    in_comment = False
    while i < len(src):
        char = src[i]
        if in_comment:
            if char == "\n":
                in_comment = False
        elif in_string:
            if char == "\\":
                i += 2
                continue
            if char == '"':
                in_string = False
        elif char == "#":
            in_comment = True
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return src[brace_index + 1 : i]
        i += 1
    raise AssertionError("unbalanced braces while slicing an HCL block")


def _block(kind: str, *labels: str, src: str | None = None) -> str:
    """Body text of e.g. ``resource "aws_cloudwatch_metric_alarm" "deploy_drift"``."""
    source = _tf_source() if src is None else src
    header = re.escape(kind) + "".join(rf'\s+"{re.escape(label)}"' for label in labels) + r"\s*\{"
    match = re.search(header, source)
    assert match, f"no {kind} {' '.join(labels)!r} block found"
    return _body_at(source, match.end() - 1)


def _nested(body: str, keyword: str) -> str:
    match = re.search(rf"^\s*{re.escape(keyword)}\s*\{{", body, re.MULTILINE)
    assert match, f"no nested `{keyword}` block"
    return _body_at(body, match.end() - 1)


def _attr(body: str, key: str) -> str:
    """Raw right-hand side of a single-line ``key = value`` attribute."""
    match = re.search(rf"^\s*{re.escape(key)}\s*=\s*(.+?)\s*$", body, re.MULTILINE)
    assert match, f"no `{key}` attribute in the block"
    return match.group(1)


def _string_attr(body: str, key: str) -> str:
    value = _attr(body, key)
    assert value.startswith('"') and value.endswith('"'), f"`{key}` is not a string literal: {value}"
    return value[1:-1]


def _int_attr(body: str, key: str) -> int:
    value = _attr(body, key).split("#")[0].strip()
    return int(value)


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def drift_probe() -> ModuleType:
    """Import the Lambda source by path — it is not on any package path.

    Loads cleanly without AWS SDKs installed: ``index.py`` imports ``boto3``
    inside ``handler`` precisely so this import stays stdlib-only. The last
    test in this file asserts that property instead of assuming it.
    """
    spec = importlib.util.spec_from_file_location("archimedes_deploy_drift_probe", DRIFT_LAMBDA_PY)
    assert spec and spec.loader, f"cannot load {DRIFT_LAMBDA_PY}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ── 1. Reveal-reconcile: terminal ───────────────────────────────────────────


class TestRevealReconcileTerminalAlarm:
    """`reveal_reconcile_terminal > 0` — #1596 item 2, first half."""

    def test_filter_and_alarm_exist_and_agree(self) -> None:
        filter_body = _block("resource", "aws_cloudwatch_log_metric_filter", "reveal_reconcile_terminal")
        transform = _nested(filter_body, "metric_transformation")
        alarm = _block("resource", "aws_cloudwatch_metric_alarm", "reveal_reconcile_terminal_alarm")

        assert _string_attr(transform, "name") == "RevealReconcileTerminalCount"
        assert _string_attr(transform, "namespace") == "Archimedes/Reveal"
        # The alarm derives both from the filter rather than re-spelling them,
        # the way oracle_stale_alarm does — a rename cannot desynchronise them.
        assert "aws_cloudwatch_log_metric_filter.reveal_reconcile_terminal" in _attr(alarm, "metric_name")
        assert "aws_cloudwatch_log_metric_filter.reveal_reconcile_terminal" in _attr(alarm, "namespace")

    def test_it_fires_on_a_single_occurrence(self) -> None:
        """ "> 0 over one 5-min period" is the whole point: terminal is a never event."""
        alarm = _block("resource", "aws_cloudwatch_metric_alarm", "reveal_reconcile_terminal_alarm")
        assert _string_attr(alarm, "statistic") == "Sum"
        assert _string_attr(alarm, "comparison_operator") == "GreaterThanThreshold"
        assert _int_attr(alarm, "threshold") == 0
        assert _int_attr(alarm, "period") == 300
        assert _int_attr(alarm, "evaluation_periods") == 1

    def test_it_watches_the_agent_loops_log_group(self) -> None:
        """`/archimedes/runners`, not `/archimedes/app` — see this file's header."""
        filter_body = _block("resource", "aws_cloudwatch_log_metric_filter", "reveal_reconcile_terminal")
        assert _attr(filter_body, "log_group_name").split("#")[0].strip() == RUNNERS_LOG_GROUP

    def test_the_pattern_matches_a_literal_the_loop_actually_logs(self) -> None:
        """The check that survives a reworded log message: grep the source.

        Without this the filter is a spelling of a string nobody verified, and
        renaming the log line — a blameless edit in a different file — mutes
        the alarm with no test anywhere going red.
        """
        filter_body = _block("resource", "aws_cloudwatch_log_metric_filter", "reveal_reconcile_terminal")
        literal = _string_attr(filter_body, "pattern").strip('\\"')
        assert literal == "REVEAL RECONCILIATION TERMINAL"
        assert literal in AGENT_RUNNER_PY.read_text(encoding="utf-8"), (
            f"the metric filter keys on {literal!r} but no such literal is logged by "
            f"{AGENT_RUNNER_PY.name} any more — the alarm now watches a string that is "
            "never emitted and can never fire."
        )


# ── 2. Reveal-reconcile: pending stuck ──────────────────────────────────────


class TestRevealReconcilePendingStuckAlarm:
    """`pending` stuck past a threshold age — #1596 item 2, second half."""

    def test_filter_and_alarm_exist_and_agree(self) -> None:
        filter_body = _block("resource", "aws_cloudwatch_log_metric_filter", "reveal_reconcile_pending")
        transform = _nested(filter_body, "metric_transformation")
        alarm = _block("resource", "aws_cloudwatch_metric_alarm", "reveal_reconcile_pending_stuck")

        assert _string_attr(transform, "name") == "RevealReconcilePendingRetryCount"
        assert _string_attr(transform, "namespace") == "Archimedes/Reveal"
        assert "aws_cloudwatch_log_metric_filter.reveal_reconcile_pending" in _attr(alarm, "metric_name")
        assert "aws_cloudwatch_log_metric_filter.reveal_reconcile_pending" in _attr(alarm, "namespace")

    def test_the_window_outlasts_the_attempt_cap(self) -> None:
        """The threshold's justification, asserted rather than left in prose.

        With ``AGENT_INTERVAL_SECONDS`` = 300 (one tick per period) and
        ``REVEAL_RECONCILE_MAX_ATTEMPTS`` = 3, the ordinary path to a give-up
        emits this literal in at most ~3 consecutive periods. Requiring more
        breaching datapoints than that is what makes this alarm mean "stuck"
        rather than "retrying". Both constants are read from the loop's source,
        so raising the cap without re-tuning the window fails here.
        """
        source = AGENT_RUNNER_PY.read_text(encoding="utf-8")
        cap = int(re.search(r'REVEAL_RECONCILE_MAX_ATTEMPTS\s*=\s*int\(os\.getenv\([^,]+,\s*"(\d+)"', source).group(1))
        tick = int(re.search(r"AGENT_INTERVAL_SECONDS\s*—.*?default:\s*(\d+)", source).group(1))

        alarm = _block("resource", "aws_cloudwatch_metric_alarm", "reveal_reconcile_pending_stuck")
        period = _int_attr(alarm, "period")
        datapoints = _int_attr(alarm, "datapoints_to_alarm")
        evaluations = _int_attr(alarm, "evaluation_periods")

        assert period == tick, "one tick per period keeps 'a breaching datapoint' equal to 'a failing tick'"
        assert datapoints > cap, (
            f"the alarm needs {datapoints} breaching 5-min periods but the attempt cap is {cap}; "
            "an ordinary give-up could reach that, so the alarm would fire on normal retry churn"
        )
        assert datapoints < evaluations, (
            "M-of-N damping is deliberate here (see the file's alb_5xx_rate_high / "
            "alb_unhealthy_hosts precedents): a runner restart or a lease handover must not "
            "reset the clock on a genuinely stuck record"
        )

    def test_it_watches_the_agent_loops_log_group(self) -> None:
        filter_body = _block("resource", "aws_cloudwatch_log_metric_filter", "reveal_reconcile_pending")
        assert _attr(filter_body, "log_group_name").split("#")[0].strip() == RUNNERS_LOG_GROUP

    def test_the_pattern_matches_a_literal_the_loop_actually_logs(self) -> None:
        filter_body = _block("resource", "aws_cloudwatch_log_metric_filter", "reveal_reconcile_pending")
        literal = _string_attr(filter_body, "pattern").strip('\\"')
        assert literal == "Reveal reconciliation attempt"
        assert literal in AGENT_RUNNER_PY.read_text(encoding="utf-8")

    def test_the_pattern_does_not_swallow_the_sibling_failures(self) -> None:
        """`_seed_missing_first_seen` and `_persist_reconciled` log their own failures.

        Both start "Reveal reconciliation ..." too. Folding them into this
        metric would make one number mean three different incidents with three
        different responses, so the pattern is narrowed to "attempt" and this
        test is what keeps it narrow.
        """
        filter_body = _block("resource", "aws_cloudwatch_log_metric_filter", "reveal_reconcile_pending")
        literal = _string_attr(filter_body, "pattern").strip('\\"')
        for sibling in ("Reveal reconciliation first-seen SEED FAILED", "Reveal reconciliation persist FAILED"):
            assert sibling in AGENT_RUNNER_PY.read_text(encoding="utf-8"), f"precedent moved: {sibling!r}"
            assert literal not in sibling, f"the pending pattern also matches {sibling!r}"


# ── 3. Deploy drift ─────────────────────────────────────────────────────────


class TestDeployDriftAlarm:
    """#1596 item 1 / #1346 AC2 — running image tag vs origin/main's tip."""

    def test_the_alarm_exists_with_the_documented_shape(self) -> None:
        alarm = _block("resource", "aws_cloudwatch_metric_alarm", "deploy_drift")
        assert _string_attr(alarm, "statistic") == "Maximum"
        assert _string_attr(alarm, "comparison_operator") == "GreaterThanThreshold"
        assert _int_attr(alarm, "threshold") == 0
        assert _int_attr(alarm, "period") == 600
        assert _int_attr(alarm, "evaluation_periods") == 12
        assert _int_attr(alarm, "datapoints_to_alarm") == 10

    def test_the_window_outlasts_one_deploy_cycle(self) -> None:
        """ "Longer than one deploy cycle" checked against the workflow's own numbers.

        deploy.yml gives build-and-push ``timeout-minutes: 30`` and budgets
        ``DEPLOY_ROLLOUT_BUDGET_SECONDS`` for the rollout. A window shorter
        than their sum would page on every slow-but-healthy deploy — the
        flapping that already forced two re-tunes in this file.
        """
        workflow = (REPO_ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
        build_timeout_s = int(re.search(r"timeout-minutes:\s*(\d+)", workflow).group(1)) * 60
        rollout_budget_s = int(re.search(r"DEPLOY_ROLLOUT_BUDGET_SECONDS:\s*(\d+)", workflow).group(1))

        alarm = _block("resource", "aws_cloudwatch_metric_alarm", "deploy_drift")
        breach_after_s = _int_attr(alarm, "period") * _int_attr(alarm, "datapoints_to_alarm")
        assert breach_after_s > build_timeout_s + rollout_budget_s, (
            f"the alarm breaches after {breach_after_s}s of drift but one worst-case deploy "
            f"cycle is {build_timeout_s + rollout_budget_s}s — every slow deploy would page"
        )

    def test_a_dead_probe_pages_instead_of_going_quiet(self) -> None:
        """The whole alarm is worthless if "no data" reads as "aligned"."""
        alarm = _block("resource", "aws_cloudwatch_metric_alarm", "deploy_drift")
        assert _string_attr(alarm, "treat_missing_data") == "breaching"

    def test_the_alarm_watches_what_the_probe_publishes(self, drift_probe: ModuleType) -> None:
        """Namespace / metric / dimension are a contract across two languages."""
        alarm = _block("resource", "aws_cloudwatch_metric_alarm", "deploy_drift")
        assert _string_attr(alarm, "namespace") == drift_probe.METRIC_NAMESPACE
        assert _string_attr(alarm, "metric_name") == drift_probe.METRIC_NAME

        dimensions = _attr(alarm, "dimensions")
        assert re.match(rf"\{{\s*{re.escape(drift_probe.METRIC_DIMENSION_NAME)}\s*=", dimensions), (
            f"the alarm's dimension key is {dimensions!r} but the probe publishes "
            f"{drift_probe.METRIC_DIMENSION_NAME!r} — the alarm would watch a metric "
            "nothing writes and sit in INSUFFICIENT_DATA forever"
        )

    def test_the_dimension_value_is_the_service_the_probe_is_pointed_at(self) -> None:
        """The probe tags the datapoint with ``ECS_SERVICE``; the alarm must use the same."""
        alarm = _block("resource", "aws_cloudwatch_metric_alarm", "deploy_drift")
        dimension_value = _attr(alarm, "dimensions").split("=", 1)[1].strip().rstrip("}").strip()
        lambda_env = _nested(_block("resource", "aws_lambda_function", "deploy_drift"), "environment")
        assert _attr(lambda_env, "ECS_SERVICE") == dimension_value

    def test_the_probe_is_actually_scheduled(self) -> None:
        """An unscheduled function publishes nothing; with breaching-on-missing that pages forever."""
        rule = _block("resource", "aws_cloudwatch_event_rule", "deploy_drift")
        assert _string_attr(rule, "schedule_expression") == "rate(5 minutes)"
        target = _block("resource", "aws_cloudwatch_event_target", "deploy_drift")
        assert "aws_lambda_function.deploy_drift.arn" in _attr(target, "arn")
        # Without the invoke permission EventBridge silently drops every invocation.
        permission = _block("resource", "aws_lambda_permission", "deploy_drift_events")
        assert _string_attr(permission, "principal") == "events.amazonaws.com"
        assert "aws_cloudwatch_event_rule.deploy_drift.arn" in _attr(permission, "source_arn")

    def test_the_deployment_package_is_built_from_the_reviewed_source(self) -> None:
        archive = _block("data", "archive_file", "deploy_drift")
        assert _string_attr(archive, "source_dir") == "${path.module}/lambda/deploy_drift"
        assert DRIFT_LAMBDA_PY.is_file()
        function = _block("resource", "aws_lambda_function", "deploy_drift")
        assert _string_attr(function, "handler") == "index.handler"
        assert "data.archive_file.deploy_drift.output_base64sha256" in _attr(function, "source_code_hash"), (
            "without source_code_hash a code-only edit never redeploys the function"
        )

    def test_the_archive_provider_is_declared(self) -> None:
        """`data "archive_file"` without the provider fails at `terraform init`."""
        assert re.search(r'archive\s*=\s*\{\s*source\s*=\s*"hashicorp/archive"', _tf_source(MAIN_TF)), (
            "infra/main.tf does not declare hashicorp/archive, so the deploy-drift "
            "deployment package cannot be built and `terraform plan` fails outright"
        )


# ── 4. Cross-cutting wiring + anti-goals ────────────────────────────────────


NEW_ALARMS = (
    "reveal_reconcile_terminal_alarm",
    "reveal_reconcile_pending_stuck",
    "deploy_drift",
)


class TestEveryNewAlarmPages:
    @pytest.mark.parametrize("alarm_name", NEW_ALARMS)
    def test_it_routes_to_the_shared_sns_topic(self, alarm_name: str) -> None:
        """An alarm with no action changes colour in a console nobody is watching."""
        alarm = _block("resource", "aws_cloudwatch_metric_alarm", alarm_name)
        assert SNS_ACTION in _attr(alarm, "alarm_actions")
        assert SNS_ACTION in _attr(alarm, "ok_actions")

    @pytest.mark.parametrize("alarm_name", NEW_ALARMS)
    def test_it_is_project_tagged(self, alarm_name: str) -> None:
        alarm = _block("resource", "aws_cloudwatch_metric_alarm", alarm_name)
        assert "var.project_name" in _attr(alarm, "tags")


class TestAntiGoals:
    """#1596 fences off the oracle-stale pair — #1594 owns that investigation."""

    def test_the_oracle_stale_alarm_is_untouched(self) -> None:
        alarm = _block("resource", "aws_cloudwatch_metric_alarm", "oracle_stale_alarm")
        assert _string_attr(alarm, "comparison_operator") == "GreaterThanOrEqualToThreshold"
        assert _int_attr(alarm, "threshold") == 3
        assert _int_attr(alarm, "period") == 300
        # 1 -> 3 with datapoints_to_alarm 2: the #1601 retune (2-of-3 windows must
        # breach before paging), merged after this fence was written. The fence's
        # point stands -- these values change only via a deliberate, cited retune.
        assert _int_attr(alarm, "evaluation_periods") == 3
        assert _int_attr(alarm, "datapoints_to_alarm") == 2
        assert _string_attr(alarm, "treat_missing_data") == "notBreaching"

    def test_the_oracle_stale_filter_is_untouched(self) -> None:
        filter_body = _block("resource", "aws_cloudwatch_log_metric_filter", "oracle_stale")
        assert _string_attr(filter_body, "pattern").strip('\\"') == "HEALTH_ORACLE_STALE"
        assert _attr(filter_body, "log_group_name").split("#")[0].strip() == "aws_cloudwatch_log_group.app.name"

    def test_no_new_sns_topic_was_introduced(self) -> None:
        """A second topic splits alarm routing; the file says so and this keeps it true."""
        assert len(re.findall(r'resource\s+"aws_sns_topic"\s+"', _tf_source())) == 1


# ── 5. The probe's verdict logic, run for real ──────────────────────────────


class TestImageTagParsing:
    @pytest.mark.parametrize(
        ("image", "expected"),
        [
            ("037613907429.dkr.ecr.us-east-1.amazonaws.com/archimedes-backend:7c2a4b05", "7c2a4b05"),
            ("registry.example.com:5000/archimedes-backend:abcdef1234567890", "abcdef1234567890"),
            ("archimedes-backend:latest", "latest"),
            # A digest pins content but names no commit — deliberately not a tag.
            ("037613907429.dkr.ecr.us-east-1.amazonaws.com/archimedes-backend@sha256:" + "a" * 64, None),
            # A registry port must never be mistaken for the tag.
            ("registry.example.com:5000/archimedes-backend", None),
            ("", None),
            (None, None),
        ],
    )
    def test_parse_image_tag(self, drift_probe: ModuleType, image: str | None, expected: str | None) -> None:
        assert drift_probe.parse_image_tag(image) == expected


class TestRefAdvertisementParsing:
    # A realistic pkt-line advertisement: the first ref line carries a
    # NUL-separated capability list, which is why the parser looks for a token
    # boundary rather than end-of-line.
    ADVERTISEMENT = (
        b"001e# service=git-upload-pack\n0000"
        b"0155" + b"a" * 40 + b" HEAD\x00multi_ack thin-pack side-band\n"
        b"003f" + b"b" * 40 + b" refs/heads/main\n"
        b"0045" + b"c" * 40 + b" refs/heads/main-backup\n"
        b"0000"
    )

    def test_it_finds_the_branch_tip(self, drift_probe: ModuleType) -> None:
        assert drift_probe.parse_ls_remote(self.ADVERTISEMENT, "refs/heads/main") == "b" * 40

    def test_it_does_not_match_a_longer_ref_that_starts_the_same(self, drift_probe: ModuleType) -> None:
        """`refs/heads/main` must not silently resolve to `refs/heads/main-backup`."""
        assert drift_probe.parse_ls_remote(self.ADVERTISEMENT, "refs/heads/main") != "c" * 40

    def test_a_missing_ref_is_none_not_a_guess(self, drift_probe: ModuleType) -> None:
        assert drift_probe.parse_ls_remote(self.ADVERTISEMENT, "refs/heads/release") is None

    def test_an_empty_or_error_body_is_none(self, drift_probe: ModuleType) -> None:
        assert drift_probe.parse_ls_remote(b"", "refs/heads/main") is None
        assert drift_probe.parse_ls_remote(b"404 Not Found", "refs/heads/main") is None


class TestDriftVerdict:
    HEAD = "7c2a4b05" + "0" * 32

    def test_matching_full_sha_is_aligned(self, drift_probe: ModuleType) -> None:
        assert drift_probe.drift_verdict(self.HEAD, self.HEAD) == (0, "aligned")

    def test_case_is_not_drift(self, drift_probe: ModuleType) -> None:
        assert drift_probe.drift_verdict(self.HEAD.upper(), self.HEAD)[0] == 0

    def test_an_abbreviated_tag_still_aligns(self, drift_probe: ModuleType) -> None:
        assert drift_probe.drift_verdict(self.HEAD[:7], self.HEAD) == (0, "aligned")

    def test_a_six_character_prefix_is_too_ambiguous_to_accept(self, drift_probe: ModuleType) -> None:
        assert drift_probe.drift_verdict(self.HEAD[:6], self.HEAD)[0] == 1

    def test_a_different_commit_is_drift(self, drift_probe: ModuleType) -> None:
        assert drift_probe.drift_verdict("f" * 40, self.HEAD) == (1, "drifted")

    @pytest.mark.parametrize(
        ("deployed", "head", "reason"),
        [
            ("latest", HEAD, "image-tag-not-a-commit"),
            ("v1.2.3", HEAD, "image-tag-not-a-commit"),
            (None, HEAD, "image-untagged"),
            (HEAD, None, "head-unreadable"),
            (None, None, "head-unreadable"),
        ],
    )
    def test_every_unanswerable_state_is_loud(
        self, drift_probe: ModuleType, deployed: str | None, head: str | None, reason: str
    ) -> None:
        """ "Cannot tell" publishes 1, never 0 and never nothing (§ fail-soft).

        A moving tag, an untagged image and an unreachable remote are all
        states in which nobody can say prod matches main. Publishing 0 for any
        of them would be the plausible substitute this codebase treats as its
        primary defect class.
        """
        assert drift_probe.drift_verdict(deployed, head) == (1, reason)


class TestRunningImageRead:
    """ECS is mocked at the client boundary — a stub with the two API calls."""

    @staticmethod
    def _client(services, task_def):
        return SimpleNamespace(
            describe_services=lambda **_: {"services": services},
            describe_task_definition=lambda **_: task_def,
        )

    def test_it_prefers_the_primary_deployment(self, drift_probe: ModuleType) -> None:
        """Mid-rollout the service field and the PRIMARY deployment disagree.

        Reading the service's top-level ``taskDefinition`` during a rollout
        answers a question nobody asked ("what was here before"), so the probe
        reads the deployment being rolled out to.
        """
        services = [
            {
                "taskDefinition": "arn:...:task-definition/archimedes-backend:41",
                "deployments": [
                    {"status": "ACTIVE", "taskDefinition": "arn:...:task-definition/archimedes-backend:41"},
                    {"status": "PRIMARY", "taskDefinition": "arn:...:task-definition/archimedes-backend:42"},
                ],
            }
        ]
        seen: dict[str, str] = {}

        def describe_task_definition(**kwargs):
            seen["arn"] = kwargs["taskDefinition"]
            return {"taskDefinition": {"containerDefinitions": [{"name": "backend", "image": "repo:deadbee"}]}}

        client = SimpleNamespace(
            describe_services=lambda **_: {"services": services},
            describe_task_definition=describe_task_definition,
        )
        assert drift_probe.read_running_image(client, "c", "s", "backend") == "repo:deadbee"
        assert seen["arn"].endswith(":42")

    def test_a_missing_service_reads_as_none(self, drift_probe: ModuleType) -> None:
        client = self._client([], {})
        assert drift_probe.read_running_image(client, "c", "s", "backend") is None

    def test_a_missing_container_reads_as_none(self, drift_probe: ModuleType) -> None:
        client = self._client(
            [{"deployments": [{"status": "PRIMARY", "taskDefinition": "arn:x"}]}],
            {"taskDefinition": {"containerDefinitions": [{"name": "nginx", "image": "repo:x"}]}},
        )
        assert drift_probe.read_running_image(client, "c", "s", "backend") is None

    def test_an_api_failure_reads_as_none_rather_than_raising(self, drift_probe: ModuleType) -> None:
        """A raise would drop the datapoint; None becomes a loud verdict instead."""

        def boom(**_):
            raise RuntimeError("AccessDeniedException")

        client = SimpleNamespace(describe_services=boom, describe_task_definition=boom)
        assert drift_probe.read_running_image(client, "c", "s", "backend") is None


class TestTheProbeStaysImportableWithoutAws:
    def test_boto3_is_imported_inside_the_handler_only(self) -> None:
        """Guards this file's own hermeticity, and the probe's cold-start cost.

        A module-level ``import boto3`` would make these tests depend on the
        AWS SDK being installed — exactly the environment coupling CLAUDE.md
        § testing conventions forbids.
        """
        tree = ast.parse(DRIFT_LAMBDA_PY.read_text(encoding="utf-8"))
        top_level_imports = {
            alias.name.split(".")[0]
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in getattr(node, "names", [])
        }
        assert "boto3" not in top_level_imports, (
            f"index.py imports boto3 at module level ({sorted(top_level_imports)}); move it "
            "into handler() so the probe's pure logic stays testable without the AWS SDK"
        )
