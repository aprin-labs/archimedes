#!/usr/bin/env python3
"""Per-route serving-latency report from ALB access logs in S3 (read-only).

Why this exists (issue #1669). The per-route evidence in the 2026-08-31
serving-latency package came from an ad-hoc parse of 766 ALB access-log
objects. A before/after comparison is only trustworthy if *both* sides are
produced by the same code — a hand-rewritten parser is a new variable in the
experiment. This script is that frozen instrument.

What it emits for a UTC window:
  * per-route p50 / p95 / p99 / max / count for a configurable path list
  * the global latency histogram over all forwarded rows
  * distinct target-IP count (task churn — a proxy for how many tasks served
    the window)
  * ``HealthyHostCount`` **Minimum** across the window (CloudWatch)

And, in comparison mode (``--compare-start`` / ``--compare-end``), it
**refuses to print a comparison** when ``HealthyHostCount`` Minimum drops
below 2 anywhere in either window — a window where tasks were being killed
and replaced cannot be used as a before/after latency baseline. Absence of
datapoints is also a refusal: no data is not evidence of health.

Read-only by construction. The only AWS calls made are ``s3:ListObjectsV2``,
``s3:GetObject`` and ``cloudwatch:GetMetricData``. There is deliberately no
CloudWatch Logs Insights path — the S3 access logs carry better per-route
detail with no per-GB scan charge (~$0.0005 per day parsed against
$0.005/GB scanned on a 90-day retention).

Access logs are enabled in ``infra/alb.tf:209-212``; the bucket is declared at
``infra/alb.tf:14-20`` with a 30-day expiry lifecycle (``infra/alb.tf:41-53``).

Usage (repo root, archimedes conda env):

    # one window
    python scripts/alb_latency_report.py --start 2026-08-30 --end 2026-08-31

    # before/after, guarded on HealthyHostCount
    python scripts/alb_latency_report.py \
        --start 2026-08-30T00:00:00Z --end 2026-08-30T12:00:00Z \
        --compare-start 2026-08-31T00:00:00Z --compare-end 2026-08-31T12:00:00Z \
        --load-balancer app/archimedes-alb/50dc6c495c0c9188 \
        --target-group targetgroup/archimedes-backend-tg/abc123

Exit codes: 0 ok · 2 bad arguments · 3 log access failure · 4 window refused.
"""

from __future__ import annotations

import argparse
import bisect
import dataclasses
import gzip
import json
import re
import sys
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta

# ── Defaults ─────────────────────────────────────────────────────────

# infra/alb.tf:14-15 — the ALB access-log bucket.
DEFAULT_BUCKET = "archimedes-alb-logs-159903201072"
DEFAULT_REGION = "us-east-1"

# The hot serving surface. Longest match wins, so "/api/generate/stream"
# takes rows that "/api/generate" would otherwise absorb.
DEFAULT_ROUTES: tuple[str, ...] = (
    "/health",
    "/api/generate/stream",
    "/api/generate",
    "/api/strategies",
    "/api/papers",
    "/api/corpus",
    "/api/explore",
    "/api/config",
    "/api/metrics",
    "/api/agent",
    "/api/vaults",
)

OTHER_ROUTE = "(other)"

# Bucket upper edges in seconds for the global histogram.
HISTOGRAM_EDGES: tuple[float, ...] = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0)

# Below this many healthy hosts a window is churning, not serving.
MIN_HEALTHY_HOSTS = 2

EXIT_OK = 0
EXIT_ACCESS = 3
EXIT_REFUSED = 4


# ── Errors ───────────────────────────────────────────────────────────


class AlbLatencyReportError(Exception):
    """Base for every failure this script reports by name rather than swallowing."""


class AlbLogAccessError(AlbLatencyReportError):
    """S3 or CloudWatch could not be read — never degrade this to an empty report."""


class WindowHealthError(AlbLatencyReportError):
    """A window failed the HealthyHostCount guard, so no comparison is emitted."""


# ── ALB access-log parsing ───────────────────────────────────────────
#
# Field order per the ALB access-log spec. Quoted fields may contain spaces
# and backslash-escaped quotes, so the line is tokenized rather than split.

_TOKEN_RE = re.compile(r'"((?:[^"\\]|\\.)*)"|(\S+)')

_TS_FIELD = 1
_TARGET_FIELD = 4
_REQUEST_TIME_FIELD = 5
_TARGET_TIME_FIELD = 6
_RESPONSE_TIME_FIELD = 7
_ELB_STATUS_FIELD = 8
_TARGET_STATUS_FIELD = 9
_REQUEST_FIELD = 12
_MIN_FIELDS = 13


def tokenize(line: str) -> list[str]:
    """Split one access-log line into fields, honouring quoted groups."""
    return [m.group(1) if m.group(1) is not None else m.group(2) for m in _TOKEN_RE.finditer(line)]


def parse_instant(text: str) -> datetime:
    """Parse a UTC ISO-8601 instant. A bare date means midnight UTC."""
    raw = text.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise AlbLatencyReportError(
            f"bad timestamp {text!r}: expected UTC ISO-8601 (2026-08-30 or 2026-08-30T12:00:00Z)"
        ) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _seconds(raw: str) -> float:
    """ALB writes -1 for a timing it never measured; so does a malformed field."""
    try:
        return float(raw)
    except ValueError:
        return -1.0


def split_request(request: str) -> tuple[str, str]:
    """``"GET https://host:443/p?q HTTP/1.1"`` → ``("GET", "/p")``."""
    parts = request.split(" ")
    if len(parts) < 2:
        return "-", "-"
    method, url = parts[0], parts[1]
    if "://" in url:
        authority_and_path = url.split("://", 1)[1]
        _, slash, tail = authority_and_path.partition("/")
        path = "/" + tail if slash else "/"
    else:
        path = url
    path = path.split("?", 1)[0].split("#", 1)[0]
    return method, path or "/"


@dataclasses.dataclass(frozen=True)
class LogRecord:
    """One access-log row, already classified as forwarded or not."""

    timestamp: datetime
    target: str
    elb_status: str
    target_status: str
    method: str
    path: str
    request_time: float
    target_time: float
    response_time: float

    @property
    def forwarded(self) -> bool:
        """False for the ``-`` target rows: the ALB answered without a backend."""
        return self.target != "-" and self.target_time >= 0.0

    @property
    def target_ip(self) -> str | None:
        if not self.forwarded:
            return None
        return self.target.rsplit(":", 1)[0]

    @property
    def total_time(self) -> float | None:
        """End-to-end ALB-observed latency; None when nothing was forwarded."""
        if not self.forwarded:
            return None
        return max(self.request_time, 0.0) + self.target_time + max(self.response_time, 0.0)


def parse_log_line(line: str) -> LogRecord | None:
    """Parse one line, or return None if it is not a usable access-log row.

    Returning None is *counted* by the caller (``malformed_rows``) — a row this
    parser cannot read must show up in the report, never vanish from it.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    fields = tokenize(stripped)
    if len(fields) < _MIN_FIELDS:
        return None
    try:
        timestamp = parse_instant(fields[_TS_FIELD])
    except AlbLatencyReportError:
        return None
    method, path = split_request(fields[_REQUEST_FIELD])
    return LogRecord(
        timestamp=timestamp,
        target=fields[_TARGET_FIELD],
        elb_status=fields[_ELB_STATUS_FIELD],
        target_status=fields[_TARGET_STATUS_FIELD],
        method=method,
        path=path,
        request_time=_seconds(fields[_REQUEST_TIME_FIELD]),
        target_time=_seconds(fields[_TARGET_TIME_FIELD]),
        response_time=_seconds(fields[_RESPONSE_TIME_FIELD]),
    )


def parse_log_text(text: str) -> tuple[list[LogRecord], int]:
    """Parse a whole log object. Returns (records, malformed_row_count)."""
    records: list[LogRecord] = []
    malformed = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        record = parse_log_line(line)
        if record is None:
            malformed += 1
        else:
            records.append(record)
    return records, malformed


# ── Statistics ───────────────────────────────────────────────────────


def percentile(sorted_values: Sequence[float], quantile: float) -> float | None:
    """Linear-interpolation percentile (numpy's default method) over sorted input."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    position = (len(sorted_values) - 1) * quantile
    low = int(position)
    high = min(low + 1, len(sorted_values) - 1)
    if low == high:
        return float(sorted_values[low])
    weight = position - low
    return float(sorted_values[low] + (sorted_values[high] - sorted_values[low]) * weight)


def match_route(path: str, routes: Sequence[str]) -> str:
    """Longest-prefix route match, or ``(other)``."""
    best: str | None = None
    for route in routes:
        base = route.rstrip("/") or "/"
        matches = path in (base, route) or path.startswith(base + "/")
        if matches and (best is None or len(base) > len(best.rstrip("/"))):
            best = route
    return best if best is not None else OTHER_ROUTE


def histogram(values: Iterable[float], edges: Sequence[float] = HISTOGRAM_EDGES) -> list[tuple[str, int]]:
    """Bucket latencies by upper edge; the final bucket is the tail above the last edge."""
    counts = [0] * (len(edges) + 1)
    for value in values:
        counts[bisect.bisect_left(edges, value)] += 1
    labels = [f"<={edge:g}s" for edge in edges] + [f">{edges[-1]:g}s"]
    return list(zip(labels, counts, strict=True))


@dataclasses.dataclass
class RouteStats:
    route: str
    count: int
    forwarded: int
    unforwarded: int
    p50: float | None
    p95: float | None
    p99: float | None
    max: float | None


@dataclasses.dataclass
class WindowReport:
    label: str
    start: datetime
    end: datetime
    objects_scanned: int
    rows_parsed: int
    rows_malformed: int
    rows_in_window: int
    forwarded_rows: int
    unforwarded_rows: int
    distinct_target_ips: int
    target_ips: list[str]
    routes: list[RouteStats]
    latency_histogram: list[tuple[str, int]]
    healthy_host_values: list[float]
    healthy_host_minimum: float | None
    healthy_host_datapoints: int

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def summarize(
    records: Sequence[LogRecord],
    *,
    label: str,
    start: datetime,
    end: datetime,
    routes: Sequence[str],
    objects_scanned: int = 0,
    rows_malformed: int = 0,
    healthy_host_values: Sequence[float] | None = None,
) -> WindowReport:
    """Fold parsed rows into the per-route / histogram / churn view."""
    in_window = [r for r in records if start <= r.timestamp < end]

    latencies_by_route: dict[str, list[float]] = {}
    counts_by_route: dict[str, int] = {}
    unforwarded_by_route: dict[str, int] = {}
    target_ips: set[str] = set()
    all_latencies: list[float] = []

    for record in in_window:
        route = match_route(record.path, routes)
        counts_by_route[route] = counts_by_route.get(route, 0) + 1
        elapsed = record.total_time
        if elapsed is None:
            # The `-` target rows: the ALB replied without ever reaching a task.
            # They are ~20% of a real day's traffic and are counted here, not dropped.
            unforwarded_by_route[route] = unforwarded_by_route.get(route, 0) + 1
            continue
        latencies_by_route.setdefault(route, []).append(elapsed)
        all_latencies.append(elapsed)
        ip = record.target_ip
        if ip:
            target_ips.add(ip)

    stats: list[RouteStats] = []
    for route in sorted(counts_by_route, key=lambda r: (-counts_by_route[r], r)):
        samples = sorted(latencies_by_route.get(route, []))
        stats.append(
            RouteStats(
                route=route,
                count=counts_by_route[route],
                forwarded=len(samples),
                unforwarded=unforwarded_by_route.get(route, 0),
                p50=percentile(samples, 0.50),
                p95=percentile(samples, 0.95),
                p99=percentile(samples, 0.99),
                max=max(samples) if samples else None,
            )
        )

    healthy = list(healthy_host_values or [])
    return WindowReport(
        label=label,
        start=start,
        end=end,
        objects_scanned=objects_scanned,
        rows_parsed=len(records),
        rows_malformed=rows_malformed,
        rows_in_window=len(in_window),
        forwarded_rows=len(all_latencies),
        unforwarded_rows=sum(unforwarded_by_route.values()),
        distinct_target_ips=len(target_ips),
        target_ips=sorted(target_ips),
        routes=stats,
        latency_histogram=histogram(sorted(all_latencies)),
        healthy_host_values=healthy,
        healthy_host_minimum=min(healthy) if healthy else None,
        healthy_host_datapoints=len(healthy),
    )


# ── The guard ────────────────────────────────────────────────────────


def assert_window_healthy(
    values: Sequence[float],
    label: str,
    *,
    threshold: int = MIN_HEALTHY_HOSTS,
) -> None:
    """Raise unless HealthyHostCount stayed at or above ``threshold`` all window.

    Two distinct refusals, both deliberate:

    * **No datapoints** — an unmeasured window is not a healthy window. Passing
      here would let a comparison be built on nothing at all.
    * **A Minimum below the threshold** — tasks were being killed and replaced
      mid-window, so the latency distribution is a picture of the death spiral,
      not of the code under test.
    """
    if not values:
        raise WindowHealthError(
            f"refusing to emit a comparison: no HealthyHostCount datapoints for {label} "
            f"({values!r}) — an unmeasured window cannot certify that tasks stayed up"
        )
    worst = min(values)
    if worst < threshold:
        breaches = [v for v in values if v < threshold]
        raise WindowHealthError(
            f"refusing to emit a comparison: {label} HealthyHostCount Minimum fell to {worst:g} "
            f"({len(breaches)} of {len(values)} datapoints below {threshold}) — targets were being "
            f"replaced during the window, so its latency distribution measures task churn, "
            f"not serving cost"
        )


# ── AWS reads (list / get / get-metric-data only) ────────────────────


def day_stamps(start: datetime, end: datetime) -> list[str]:
    """``YYYY/MM/DD`` path segments covering the window, inclusive of both ends."""
    first: date = start.astimezone(UTC).date()
    last: date = (end - timedelta(microseconds=1)).astimezone(UTC).date()
    if last < first:
        last = first
    stamps = []
    cursor = first
    while cursor <= last:
        stamps.append(f"{cursor:%Y/%m/%d}")
        cursor += timedelta(days=1)
    return stamps


def day_prefixes(start: datetime, end: datetime, account_ids: Sequence[str], region: str) -> list[str]:
    """S3 key prefixes for the window, one per (account, day)."""
    return [
        f"AWSLogs/{account}/elasticloadbalancing/{region}/{stamp}/"
        for account in account_ids
        for stamp in day_stamps(start, end)
    ]


def _listing(s3, bucket: str, prefix: str, **extra) -> dict:
    try:
        return s3.list_objects_v2(Bucket=bucket, Prefix=prefix, **extra)
    except AlbLatencyReportError:
        raise
    except Exception as exc:  # botocore raises a wide family; name the failure, never continue
        raise AlbLogAccessError(f"cannot list s3://{bucket}/{prefix} — {type(exc).__name__}: {exc}") from exc


def discover_account_ids(s3, bucket: str) -> list[str]:
    """Read the account ids the ALB writes under, so the caller need not know them."""
    response = _listing(s3, bucket, "AWSLogs/", Delimiter="/")
    accounts = [
        entry["Prefix"].rstrip("/").rsplit("/", 1)[-1]
        for entry in response.get("CommonPrefixes", [])
        if entry.get("Prefix")
    ]
    if not accounts:
        raise AlbLogAccessError(
            f"no AWSLogs/<account-id>/ prefixes under s3://{bucket}/ — wrong bucket, or access logging is off "
            f"(infra/alb.tf:209-212)"
        )
    return accounts


def list_log_keys(s3, bucket: str, prefixes: Sequence[str]) -> list[str]:
    """Every log object under the given prefixes. Empty is an error, not a report."""
    keys: list[str] = []
    for prefix in prefixes:
        token: str | None = None
        while True:
            extra = {"ContinuationToken": token} if token else {}
            response = _listing(s3, bucket, prefix, **extra)
            keys.extend(obj["Key"] for obj in response.get("Contents", []) if obj.get("Key"))
            token = response.get("NextContinuationToken")
            if not response.get("IsTruncated") or not token:
                break
    if not keys:
        raise AlbLogAccessError(
            f"no ALB log objects under s3://{bucket}/ for {len(prefixes)} prefix(es) "
            f"(first: {prefixes[0] if prefixes else '-'}) — the window may predate the 30-day "
            f"expiry lifecycle (infra/alb.tf:41-53)"
        )
    return keys


def read_log_object(s3, bucket: str, key: str) -> str:
    """Fetch and decompress one access-log object."""
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except AlbLatencyReportError:
        raise
    except Exception as exc:
        raise AlbLogAccessError(f"cannot read s3://{bucket}/{key} — {type(exc).__name__}: {exc}") from exc
    if key.endswith(".gz"):
        body = gzip.decompress(body)
    return body.decode("utf-8", errors="replace")


def healthy_host_minimums(
    cloudwatch,
    *,
    start: datetime,
    end: datetime,
    load_balancer: str | None,
    target_group: str | None,
    period: int = 60,
) -> list[float]:
    """``HealthyHostCount`` Minimum series across the window (empty if undimensioned)."""
    if not load_balancer:
        return []
    dimensions = [{"Name": "LoadBalancer", "Value": load_balancer}]
    if target_group:
        dimensions.append({"Name": "TargetGroup", "Value": target_group})
    query = {
        "Id": "healthyhosts",
        "MetricStat": {
            "Metric": {
                "Namespace": "AWS/ApplicationELB",
                "MetricName": "HealthyHostCount",
                "Dimensions": dimensions,
            },
            "Period": period,
            "Stat": "Minimum",
        },
        "ReturnData": True,
    }
    values: list[float] = []
    token: str | None = None
    while True:
        extra = {"NextToken": token} if token else {}
        try:
            response = cloudwatch.get_metric_data(
                MetricDataQueries=[query],
                StartTime=start,
                EndTime=end,
                ScanBy="TimestampAscending",
                **extra,
            )
        except Exception as exc:
            raise AlbLogAccessError(
                f"cannot read HealthyHostCount for {load_balancer} — {type(exc).__name__}: {exc}"
            ) from exc
        for result in response.get("MetricDataResults", []):
            values.extend(float(v) for v in result.get("Values", []))
        token = response.get("NextToken")
        if not token:
            break
    return values


def load_window(
    s3,
    cloudwatch,
    *,
    label: str,
    start: datetime,
    end: datetime,
    bucket: str,
    region: str,
    routes: Sequence[str],
    account_ids: Sequence[str] | None,
    load_balancer: str | None,
    target_group: str | None,
    period: int = 60,
) -> WindowReport:
    """Read one window end to end and fold it into a report."""
    accounts = list(account_ids) if account_ids else discover_account_ids(s3, bucket)
    keys = list_log_keys(s3, bucket, day_prefixes(start, end, accounts, region))
    records: list[LogRecord] = []
    malformed = 0
    for key in keys:
        parsed, bad = parse_log_text(read_log_object(s3, bucket, key))
        records.extend(parsed)
        malformed += bad
    healthy = healthy_host_minimums(
        cloudwatch,
        start=start,
        end=end,
        load_balancer=load_balancer,
        target_group=target_group,
        period=period,
    )
    return summarize(
        records,
        label=label,
        start=start,
        end=end,
        routes=routes,
        objects_scanned=len(keys),
        rows_malformed=malformed,
        healthy_host_values=healthy,
    )


# ── Rendering ────────────────────────────────────────────────────────


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"


def render_window(report: WindowReport) -> str:
    lines = [
        f"ALB latency report — {report.label}",
        f"  window            : {report.start.isoformat()} .. {report.end.isoformat()} (UTC)",
        f"  log objects read  : {report.objects_scanned}",
        f"  rows in window    : {report.rows_in_window}  (of {report.rows_parsed} parsed across all objects; "
        f"{report.rows_malformed} unparseable)",
        f"  served by a task  : {report.forwarded_rows}",
        f"  never served      : {report.unforwarded_rows}"
        + (
            f"  ({100.0 * report.unforwarded_rows / report.rows_in_window:.1f}% of window)"
            if report.rows_in_window
            else ""
        ),
        f"  distinct target IPs: {report.distinct_target_ips}  {report.target_ips}",
        f"  HealthyHostCount Minimum: {_fmt(report.healthy_host_minimum)} over {report.healthy_host_datapoints} datapoint(s)",
        "",
        f"  {'route':<24}{'count':>8}{'fwd':>7}{'unfwd':>7}{'p50':>10}{'p95':>10}{'p99':>10}{'max':>10}",
    ]
    for route in report.routes:
        lines.append(
            f"  {route.route:<24}{route.count:>8}{route.forwarded:>7}{route.unforwarded:>7}"
            f"{_fmt(route.p50):>10}{_fmt(route.p95):>10}{_fmt(route.p99):>10}{_fmt(route.max):>10}"
        )
    lines.extend(["", "  latency histogram (forwarded rows, seconds):"])
    for bucket_label, count in report.latency_histogram:
        lines.append(f"    {bucket_label:>8}  {count}")
    return "\n".join(lines)


def render_comparison(before: WindowReport, after: WindowReport) -> str:
    by_route: dict[str, list[RouteStats | None]] = {}
    for index, report in enumerate((before, after)):
        for route in report.routes:
            by_route.setdefault(route.route, [None, None])[index] = route
    lines = [
        "",
        f"comparison — {before.label} vs {after.label}",
        f"  {'route':<24}{'p50 before':>12}{'p50 after':>12}{'p95 before':>12}{'p95 after':>12}{'n before':>10}{'n after':>10}",
    ]
    for route_name in sorted(by_route):
        left, right = by_route[route_name]
        lines.append(
            f"  {route_name:<24}"
            f"{_fmt(left.p50 if left else None):>12}{_fmt(right.p50 if right else None):>12}"
            f"{_fmt(left.p95 if left else None):>12}{_fmt(right.p95 if right else None):>12}"
            f"{(left.count if left else 0):>10}{(right.count if right else 0):>10}"
        )
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alb_latency_report.py",
        description="Per-route ALB serving-latency report from S3 access logs (read-only).",
        epilog="Read-only: s3:ListObjectsV2, s3:GetObject, cloudwatch:GetMetricData. Nothing is written.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--start", required=True, help="window start, UTC ISO-8601 (bare date = midnight)")
    parser.add_argument("--end", required=True, help="window end, exclusive, UTC ISO-8601")
    parser.add_argument("--compare-start", help="second window start — enables comparison mode")
    parser.add_argument("--compare-end", help="second window end, exclusive")
    parser.add_argument("--bucket", default=DEFAULT_BUCKET, help=f"access-log bucket (default: {DEFAULT_BUCKET})")
    parser.add_argument("--region", default=DEFAULT_REGION, help=f"log path region (default: {DEFAULT_REGION})")
    parser.add_argument("--profile", help="AWS profile name")
    parser.add_argument(
        "--account-id", action="append", dest="account_ids", help="repeatable; default: discover by listing"
    )
    parser.add_argument("--load-balancer", help="CloudWatch LoadBalancer dimension, e.g. app/archimedes-alb/50dc6c49")
    parser.add_argument("--target-group", help="CloudWatch TargetGroup dimension")
    parser.add_argument(
        "--routes",
        default=",".join(DEFAULT_ROUTES),
        help="comma-separated path prefixes to report; longest match wins",
    )
    parser.add_argument("--period", type=int, default=60, help="HealthyHostCount period in seconds (default: 60)")
    parser.add_argument(
        "--min-healthy-hosts",
        type=int,
        default=MIN_HEALTHY_HOSTS,
        help=f"refuse a comparison below this HealthyHostCount Minimum (default: {MIN_HEALTHY_HOSTS})",
    )
    parser.add_argument("--json", action="store_true", dest="as_json", help="emit JSON instead of the text report")
    return parser


def _aws_clients(region: str, profile: str | None):
    """Build the two read-only clients. Imported here so --help needs no boto3."""
    import boto3

    session = boto3.session.Session(region_name=region, profile_name=profile)
    return session.client("s3"), session.client("cloudwatch")


def run(args: argparse.Namespace, s3, cloudwatch) -> str:
    """Produce the report text. Raises AlbLatencyReportError rather than degrading."""
    routes = [r.strip() for r in args.routes.split(",") if r.strip()]
    start, end = parse_instant(args.start), parse_instant(args.end)
    if end <= start:
        raise AlbLatencyReportError(f"--end ({end.isoformat()}) must be after --start ({start.isoformat()})")

    common = {
        "bucket": args.bucket,
        "region": args.region,
        "routes": routes,
        "account_ids": args.account_ids,
        "load_balancer": args.load_balancer,
        "target_group": args.target_group,
        "period": args.period,
    }
    before = load_window(s3, cloudwatch, label="window A", start=start, end=end, **common)

    comparing = bool(args.compare_start or args.compare_end)
    if not comparing:
        return json.dumps(before.as_dict(), indent=2, default=str) if args.as_json else render_window(before)

    if not (args.compare_start and args.compare_end):
        raise AlbLatencyReportError("comparison mode needs both --compare-start and --compare-end")
    other_start, other_end = parse_instant(args.compare_start), parse_instant(args.compare_end)
    if other_end <= other_start:
        raise AlbLatencyReportError("--compare-end must be after --compare-start")
    after = load_window(s3, cloudwatch, label="window B", start=other_start, end=other_end, **common)

    # The guard runs before anything comparative is rendered, so a churning
    # window can never reach the reader as a before/after claim.
    for report in (before, after):
        assert_window_healthy(report.healthy_host_values, report.label, threshold=args.min_healthy_hosts)

    if args.as_json:
        return json.dumps({"before": before.as_dict(), "after": after.as_dict()}, indent=2, default=str)
    return "\n".join([render_window(before), "", render_window(after), render_comparison(before, after)])


def main(argv: Sequence[str] | None = None, *, clients: tuple | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        s3, cloudwatch = clients if clients is not None else _aws_clients(args.region, args.profile)
        print(run(args, s3, cloudwatch))
    except WindowHealthError as exc:
        print(f"ERROR [WindowHealthError] {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except AlbLogAccessError as exc:
        print(f"ERROR [AlbLogAccessError] {exc}", file=sys.stderr)
        return EXIT_ACCESS
    except AlbLatencyReportError as exc:
        print(f"ERROR [{type(exc).__name__}] {exc}", file=sys.stderr)
        return EXIT_ACCESS
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
