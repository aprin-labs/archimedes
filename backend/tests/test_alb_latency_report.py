"""#1669: the frozen ALB-log latency reporter.

This is the measurement instrument for the serving-latency work. Before/after
claims for the latency issues are only trustworthy if both sides come out of
the *same* parser, so the parser gets pinned here against a committed fixture
of real-shaped ALB access-log lines — including the ``-`` target rows (about a
fifth of a real day's traffic) that a naive float-parse silently drops.

Three properties are asserted adversarially, not just exercised:

* the ``-`` target rows are **counted as unforwarded**, per route, not dropped;
* an unreachable bucket and an empty listing **fail loudly** rather than
  degrading into an empty-but-plausible report;
* the ``HealthyHostCount`` guard **refuses** a comparison when the minimum hits
  zero — and the paired control shows the same harness emitting a comparison
  when both windows stayed healthy, so the refusal comes from the data and not
  from a broken fixture (the non-vacuity discipline of
  ``test_faulthandler_enabled.py``).

Hermetic: no AWS calls, no credentials, no network. The S3 and CloudWatch
clients are faked at the boundary.
"""

from __future__ import annotations

import gzip
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "alb_latency_report.py"
# Named .txt, not .log: the repo's .gitignore excludes *.log, and this fixture
# has to be committed for the before/after comparison to be reproducible.
_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "alb_access_log_sample.txt"


def _load_reporter():
    spec = importlib.util.spec_from_file_location("alb_latency_report", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["alb_latency_report"] = module
    spec.loader.exec_module(module)
    return module


alr = _load_reporter()

_ACCOUNT = "159903201072"
_LOG_KEY = (
    f"AWSLogs/{_ACCOUNT}/elasticloadbalancing/us-east-1/2026/08/30/"
    f"{_ACCOUNT}_elasticloadbalancing_us-east-1_app.archimedes-alb.50dc6c495c0c9188"
    f"_20260830T1205Z_10.0.2.15_1abcdefg.log.gz"
)


# ── Boundary fakes (S3 / CloudWatch) ─────────────────────────────────


class _FakeBody:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeS3:
    """Minimal stand-in for the two read-only S3 calls the script makes."""

    def __init__(self, objects: dict[str, bytes] | None = None, *, list_error: Exception | None = None) -> None:
        self.objects = objects if objects is not None else {}
        self.list_error = list_error
        self.calls: list[dict] = []

    def list_objects_v2(self, **kwargs):
        self.calls.append({"op": "list", **kwargs})
        if self.list_error is not None:
            raise self.list_error
        prefix = kwargs.get("Prefix", "")
        matching = sorted(k for k in self.objects if k.startswith(prefix))
        if kwargs.get("Delimiter") == "/":
            common = sorted({prefix + k[len(prefix) :].split("/")[0] + "/" for k in matching})
            return {"CommonPrefixes": [{"Prefix": p} for p in common]}
        return {"Contents": [{"Key": k} for k in matching], "IsTruncated": False}

    def get_object(self, **kwargs):
        self.calls.append({"op": "get", **kwargs})
        return {"Body": _FakeBody(self.objects[kwargs["Key"]])}


class FakeCloudWatch:
    """Returns a canned HealthyHostCount Minimum series per window start."""

    def __init__(self, series_by_start: dict[str, list[float]]) -> None:
        self.series_by_start = series_by_start
        self.calls: list[dict] = []

    def get_metric_data(self, **kwargs):
        self.calls.append(kwargs)
        key = kwargs["StartTime"].isoformat()
        return {"MetricDataResults": [{"Id": "healthyhosts", "Values": list(self.series_by_start.get(key, []))}]}


def _fixture_bytes() -> bytes:
    return gzip.compress(_FIXTURE.read_bytes())


def _fake_s3_with_fixture() -> FakeS3:
    return FakeS3({_LOG_KEY: _fixture_bytes()})


def _summarize(start: str = "2026-08-30T12:00:00Z", end: str = "2026-08-30T12:01:00Z", **kwargs):
    records, malformed = alr.parse_log_text(_FIXTURE.read_text())
    return alr.summarize(
        records,
        label="fixture",
        start=alr.parse_instant(start),
        end=alr.parse_instant(end),
        routes=alr.DEFAULT_ROUTES,
        rows_malformed=malformed,
        **kwargs,
    )


def _routes(report) -> dict[str, object]:
    return {r.route: r for r in report.routes}


# ── Parsing the committed fixture ────────────────────────────────────


def test_fixture_parses_every_row_and_counts_the_unparseable_one():
    records, malformed = alr.parse_log_text(_FIXTURE.read_text())
    assert len(records) == 20
    # The truncated last line is reported, not swallowed.
    assert malformed == 1


def test_dash_target_rows_are_counted_as_unforwarded_not_dropped():
    report = _summarize()
    assert report.rows_in_window == 20
    assert report.forwarded_rows == 16
    assert report.unforwarded_rows == 4
    # Every row is accounted for on exactly one side of the split — the failure
    # mode this guards is a `-` row vanishing from both counts.
    assert report.forwarded_rows + report.unforwarded_rows == report.rows_in_window
    # The real-day proportion the fixture reproduces: ~20% of rows never reached a task.
    assert report.unforwarded_rows / report.rows_in_window == pytest.approx(0.20)


def test_unforwarded_rows_keep_their_route_attribution():
    by_route = _routes(_summarize())
    assert by_route["/health"].count == 7
    assert by_route["/health"].unforwarded == 1
    assert by_route["/api/generate/stream"].count == 6
    assert by_route["/api/generate/stream"].unforwarded == 2
    assert by_route["/api/strategies"].count == 5
    assert by_route["/api/strategies"].unforwarded == 1
    assert by_route["/api/papers"].unforwarded == 0
    assert sum(r.unforwarded for r in _summarize().routes) == 4


def test_unforwarded_rows_do_not_pollute_the_latency_distribution():
    """A -1 timing must never be folded in as a fast request."""
    report = _summarize()
    by_route = _routes(report)
    assert by_route["/api/generate/stream"].forwarded == 4
    assert by_route["/api/generate/stream"].p50 == pytest.approx(28.75)
    # A folded-in -1 would land in the fastest histogram bucket. That bucket holds
    # exactly the five real sub-50ms /health rows (0.001 .. 0.005) and nothing else,
    # and the whole histogram sums to the forwarded count, not to the row count.
    first_label, first_count = report.latency_histogram[0]
    assert (first_label, first_count) == ("<=0.05s", 5)
    assert sum(count for _, count in report.latency_histogram) == 16


def test_a_target_that_died_mid_request_counts_as_unforwarded():
    """The 502 row: a target WAS chosen, then closed the connection before responding.

    This is the shape a task killed mid-request leaves in the log — target
    populated, all three timings -1, target status ``-``. A parser that
    classifies on the target field alone calls it served and folds -1 into the
    latency distribution.
    """
    records, _ = alr.parse_log_text(_FIXTURE.read_text())
    died = [r for r in records if r.elb_status == "502"]
    assert len(died) == 1
    row = died[0]
    assert row.target == "10.0.3.42:8000"  # a target was chosen ...
    assert row.target_time == -1.0
    assert row.forwarded is False  # ... but nothing was served
    assert row.total_time is None
    assert row.target_ip is None


def test_per_route_percentiles_match_hand_computed_values():
    by_route = _routes(_summarize())
    health = by_route["/health"]
    assert health.forwarded == 6
    assert health.p50 == pytest.approx(0.0035)
    assert health.p95 == pytest.approx(0.67625)
    assert health.p99 == pytest.approx(0.85525)
    assert health.max == pytest.approx(0.900)
    stream = by_route["/api/generate/stream"]
    assert stream.max == pytest.approx(118.0)


def test_distinct_target_ip_count_tracks_task_churn():
    report = _summarize()
    assert report.distinct_target_ips == 3
    assert report.target_ips == ["10.0.2.15", "10.0.2.99", "10.0.3.42"]


def test_histogram_accounts_for_every_forwarded_row():
    report = _summarize()
    assert sum(count for _, count in report.latency_histogram) == report.forwarded_rows == 16
    assert report.latency_histogram[-1] == (">60s", 1)  # the 118s stream row


def test_longest_prefix_route_match_wins():
    routes = ["/api/generate", "/api/generate/stream", "/health"]
    assert alr.match_route("/api/generate/stream/", routes) == "/api/generate/stream"
    assert alr.match_route("/api/generate", routes) == "/api/generate"
    assert alr.match_route("/api/vaults/0xabc", routes) == alr.OTHER_ROUTE


def test_quoted_fields_with_escaped_quotes_still_parse():
    escaped = [r for r in alr.parse_log_text(_FIXTURE.read_text())[0] if r.path == "/api/strategies"]
    # The row whose user-agent carries an escaped quote is the 0.250s one; if the
    # tokenizer mis-split it, the request field would not have yielded this path.
    assert any(r.target_time == pytest.approx(0.250) for r in escaped)


def test_rows_outside_the_window_are_excluded():
    report = _summarize(start="2026-08-30T12:00:11Z", end="2026-08-30T12:00:22Z")
    assert report.rows_in_window == 10  # the first ten rows are outside the window
    by_route = _routes(report)
    # The six fast forwarded /health rows are before the window; only the 460 remains.
    assert by_route["/health"].count == 1
    assert by_route["/health"].forwarded == 0
    assert by_route["/health"].unforwarded == 1
    assert report.forwarded_rows == 6
    assert report.unforwarded_rows == 4


def test_percentile_is_the_linear_interpolation_method():
    values = [float(v) for v in range(1, 11)]
    assert alr.percentile(values, 0.50) == pytest.approx(5.5)
    assert alr.percentile(values, 0.95) == pytest.approx(9.55)
    assert alr.percentile([], 0.5) is None
    assert alr.percentile([4.0], 0.99) == pytest.approx(4.0)


def test_day_prefixes_cover_the_whole_window():
    prefixes = alr.day_prefixes(
        alr.parse_instant("2026-08-30T22:00:00Z"),
        alr.parse_instant("2026-09-01T02:00:00Z"),
        [_ACCOUNT],
        "us-east-1",
    )
    assert prefixes == [
        f"AWSLogs/{_ACCOUNT}/elasticloadbalancing/us-east-1/2026/08/30/",
        f"AWSLogs/{_ACCOUNT}/elasticloadbalancing/us-east-1/2026/08/31/",
        f"AWSLogs/{_ACCOUNT}/elasticloadbalancing/us-east-1/2026/09/01/",
    ]


# ── Reading a window through the faked clients ───────────────────────


def test_load_window_reads_s3_and_cloudwatch_and_nothing_else():
    s3 = _fake_s3_with_fixture()
    cloudwatch = FakeCloudWatch({"2026-08-30T12:00:00+00:00": [3.0, 2.0, 2.0]})
    report = alr.load_window(
        s3,
        cloudwatch,
        label="window A",
        start=alr.parse_instant("2026-08-30T12:00:00Z"),
        end=alr.parse_instant("2026-08-30T12:01:00Z"),
        bucket="archimedes-alb-logs-test",
        region="us-east-1",
        routes=alr.DEFAULT_ROUTES,
        account_ids=None,
        load_balancer="app/archimedes-alb/50dc6c495c0c9188",
        target_group="targetgroup/archimedes-backend-tg/73e2d6bc24d8a067",
    )
    assert report.objects_scanned == 1
    assert report.rows_in_window == 20
    assert report.healthy_host_minimum == pytest.approx(2.0)
    assert report.healthy_host_datapoints == 3
    assert {call["op"] for call in s3.calls} == {"list", "get"}


# ── Adversarial: failures must be loud ───────────────────────────────


def test_unreachable_bucket_exits_nonzero_with_a_named_error(capsys):
    """A bucket we cannot list must NOT degrade into an empty report."""
    broken = FakeS3(list_error=RuntimeError("An error occurred (AccessDenied) when calling ListObjectsV2"))
    code = alr.main(
        ["--start", "2026-08-30T12:00:00Z", "--end", "2026-08-30T12:01:00Z"],
        clients=(broken, FakeCloudWatch({})),
    )
    captured = capsys.readouterr()
    assert code == alr.EXIT_ACCESS != 0
    assert "AlbLogAccessError" in captured.err
    assert "AccessDenied" in captured.err
    assert captured.out.strip() == ""  # no plausible-looking empty report on stdout


def test_empty_listing_is_an_error_not_an_empty_report(capsys):
    code = alr.main(
        ["--start", "2026-08-30T12:00:00Z", "--end", "2026-08-30T12:01:00Z"],
        clients=(FakeS3({}), FakeCloudWatch({})),
    )
    captured = capsys.readouterr()
    assert code == alr.EXIT_ACCESS != 0
    assert "AlbLogAccessError" in captured.err
    assert captured.out.strip() == ""


def test_a_readable_bucket_still_produces_a_report(capsys):
    """Control for the two failure tests: the same harness succeeds on good input."""
    code = alr.main(
        ["--start", "2026-08-30T12:00:00Z", "--end", "2026-08-30T12:01:00Z"],
        clients=(_fake_s3_with_fixture(), FakeCloudWatch({})),
    )
    captured = capsys.readouterr()
    assert code == alr.EXIT_OK
    assert "ALB latency report" in captured.out
    assert "distinct target IPs: 3" in captured.out


# ── Adversarial: the HealthyHostCount guard ──────────────────────────


def test_guard_refuses_a_window_whose_minimum_hits_zero():
    with pytest.raises(alr.WindowHealthError) as excinfo:
        alr.assert_window_healthy([2.0, 2.0, 0.0, 1.0, 2.0], "window B")
    message = str(excinfo.value)
    assert "refusing to emit a comparison" in message
    assert "window B" in message
    assert "2 of 5 datapoints below 2" in message


def test_guard_refuses_a_window_with_no_datapoints():
    """No data is not evidence of health — an unmeasured window is refused too."""
    with pytest.raises(alr.WindowHealthError, match="no HealthyHostCount datapoints"):
        alr.assert_window_healthy([], "window A")


def test_guard_accepts_a_window_that_stayed_healthy():
    """Non-vacuity: the guard is capable of passing, so a refusal means something."""
    alr.assert_window_healthy([2.0, 3.0, 2.0], "window A")


def test_comparison_is_refused_end_to_end_when_healthy_hosts_hit_zero(capsys):
    cloudwatch = FakeCloudWatch(
        {
            "2026-08-30T12:00:00+00:00": [2.0, 2.0, 2.0],
            "2026-08-30T12:00:11+00:00": [2.0, 0.0, 2.0],  # the task-kill spiral
        }
    )
    code = alr.main(
        [
            "--start",
            "2026-08-30T12:00:00Z",
            "--end",
            "2026-08-30T12:00:11Z",
            "--compare-start",
            "2026-08-30T12:00:11Z",
            "--compare-end",
            "2026-08-30T12:00:22Z",
            "--load-balancer",
            "app/archimedes-alb/50dc6c495c0c9188",
        ],
        clients=(_fake_s3_with_fixture(), cloudwatch),
    )
    captured = capsys.readouterr()
    assert code == alr.EXIT_REFUSED != 0
    assert "WindowHealthError" in captured.err
    assert "window B" in captured.err
    assert "comparison" not in captured.out  # the comparison table never reaches the reader
    assert captured.out.strip() == ""


def test_comparison_is_emitted_when_both_windows_stayed_healthy(capsys):
    """The paired control: identical inputs but a healthy minimum, and it prints."""
    cloudwatch = FakeCloudWatch(
        {
            "2026-08-30T12:00:00+00:00": [2.0, 2.0, 2.0],
            "2026-08-30T12:00:11+00:00": [2.0, 2.0, 2.0],
        }
    )
    code = alr.main(
        [
            "--start",
            "2026-08-30T12:00:00Z",
            "--end",
            "2026-08-30T12:00:11Z",
            "--compare-start",
            "2026-08-30T12:00:11Z",
            "--compare-end",
            "2026-08-30T12:00:22Z",
            "--load-balancer",
            "app/archimedes-alb/50dc6c495c0c9188",
        ],
        clients=(_fake_s3_with_fixture(), cloudwatch),
    )
    captured = capsys.readouterr()
    assert code == alr.EXIT_OK
    assert "comparison — window A vs window B" in captured.out


def test_the_guard_cannot_be_bypassed_by_omitting_load_balancer(capsys):
    """Dropping --load-balancer yields no metric series, which is a refusal, not a pass.

    The obvious way around a health guard is to withhold the health data. Here
    that produces an empty series, and an empty series is refused — so the only
    way to get a comparison out of this script is to prove the windows were up.
    """
    code = alr.main(
        [
            "--start",
            "2026-08-30T12:00:00Z",
            "--end",
            "2026-08-30T12:00:11Z",
            "--compare-start",
            "2026-08-30T12:00:11Z",
            "--compare-end",
            "2026-08-30T12:00:22Z",
        ],
        clients=(_fake_s3_with_fixture(), FakeCloudWatch({})),
    )
    captured = capsys.readouterr()
    assert code == alr.EXIT_REFUSED != 0
    assert "no HealthyHostCount datapoints" in captured.err
    assert captured.out.strip() == ""


def test_json_mode_carries_the_healthy_host_minimum(capsys):
    code = alr.main(
        [
            "--start",
            "2026-08-30T12:00:00Z",
            "--end",
            "2026-08-30T12:01:00Z",
            "--json",
            "--load-balancer",
            "app/archimedes-alb/50dc6c495c0c9188",
        ],
        clients=(_fake_s3_with_fixture(), FakeCloudWatch({"2026-08-30T12:00:00+00:00": [4.0, 2.0]})),
    )
    captured = capsys.readouterr()
    assert code == alr.EXIT_OK
    assert '"healthy_host_minimum": 2.0' in captured.out
    assert '"unforwarded_rows": 4' in captured.out


# ── --help without boto3 or AWS credentials ──────────────────────────

_HELP_PROBE = """
import importlib.util, sys

class _BlockBoto3:
    def find_spec(self, name, path=None, target=None):
        if name == "boto3" or name.startswith("boto3."):
            raise ImportError("boto3 blocked by the test harness")
        return None

sys.meta_path.insert(0, _BlockBoto3())
try:
    import boto3
except ImportError:
    pass
else:
    raise AssertionError("probe is vacuous: boto3 imported despite the block")

spec = importlib.util.spec_from_file_location("alb_latency_report", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules["alb_latency_report"] = module  # dataclasses resolves annotations via sys.modules
spec.loader.exec_module(module)
try:
    module.main(["--help"])
except SystemExit as exit_code:
    assert exit_code.code == 0, f"--help exited {exit_code.code}"
else:
    raise AssertionError("--help did not exit")
print("HELP-OK-NO-BOTO3")
"""

_NO_CREDENTIALS_PROBE = """
import boto3
session = boto3.session.Session()
assert session.get_credentials() is None, "probe is vacuous: AWS credentials ARE resolvable here"
print("NO-CREDENTIALS")
"""


def _credential_free_env(tmp_path: Path) -> dict[str, str]:
    """Whitelist env with no AWS credentials reachable by any provider in the chain."""
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),  # hides ~/.aws
        "AWS_CONFIG_FILE": str(tmp_path / "no-such-config"),
        "AWS_SHARED_CREDENTIALS_FILE": str(tmp_path / "no-such-credentials"),
        "AWS_EC2_METADATA_DISABLED": "true",
    }


def test_help_exits_zero_without_boto3_or_aws_credentials(tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", _HELP_PROBE, str(_SCRIPT)],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        env=_credential_free_env(tmp_path),
        timeout=60,
    )
    assert result.returncode == 0, f"probe failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    assert "HELP-OK-NO-BOTO3" in result.stdout


def test_the_credential_free_env_really_has_no_credentials(tmp_path):
    """Non-vacuity for the test above: prove the env it runs in resolves nothing."""
    result = subprocess.run(
        [sys.executable, "-c", _NO_CREDENTIALS_PROBE],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        env=_credential_free_env(tmp_path),
        timeout=60,
    )
    if result.returncode != 0 and "ModuleNotFoundError" in result.stderr:
        pytest.skip("boto3 not installed — credential absence is trivially true")
    assert result.returncode == 0, f"probe failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    assert "NO-CREDENTIALS" in result.stdout


# ── Anti-goals (issue #1669) ─────────────────────────────────────────


def test_reporter_calls_no_mutating_aws_api_and_no_logs_insights():
    source = _SCRIPT.read_text()
    forbidden = re.findall(r"create_|delete_|put_|update_|start_query", source)
    assert forbidden == [], f"mutating / Logs-Insights call surface in {_SCRIPT.name}: {sorted(set(forbidden))}"
    assert "logs" not in re.findall(r"client\(\"(\w+)\"\)", source)
    assert sorted(set(re.findall(r"client\(\"(\w+)\"\)", source))) == ["cloudwatch", "s3"]
