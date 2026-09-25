"""Deploy-drift probe — is the live ECS service running ``origin/main``'s tip?

Issue #1596 (forked from #1346 AC2). Nothing in the stack compared the image
tag the Fargate service is actually running against the tip of ``origin/main``,
so "prod is running a stale image while main carries the fix" — the exact
failure that motivated the deploy workflow's queue-never-cancel concurrency
change — was invisible. Countable nowhere, alertable nowhere.

**Why a scheduled probe and not a log-metric-filter.** Every other alarm in
``infra/cloudwatch.tf`` keys on a literal the app itself logs. Deploy drift has
no such literal and cannot have one: the answer depends on a fact that lives
*outside* AWS (the tip of ``origin/main``), so no amount of log filtering can
derive it. #1596's anti-goals fence off the two in-repo places that do know the
tip (the deploy workflow, the ``/health`` handler), and its acceptance
criterion names the alternative explicitly — "a CloudWatch alarm **or scheduled
job emitting a metric**". This is that job.

**What it publishes.** One datapoint per run of ``Archimedes/Deploy ->
DeployDrift``: ``1`` when prod demonstrably is not running main's tip (or when
that question cannot be answered), ``0`` when it demonstrably is. The
"cannot be answered" cases publish ``1`` on purpose. An unreadable ECS service,
an unreachable git remote, and an image tagged with something that is not a
commit are all states in which nobody can say prod matches main — and per
``docs/architectural-principles.md`` § fail-soft, the correct degraded value
for a claim-bearing signal is a loud one, never a plausible substitute. Every
verdict carries a machine-readable ``reason`` in the log line so a pager
response can tell "behind" from "cannot tell" in one glance.

**How the tip is read.** Git's own smart-HTTP ref advertisement
(``GET <repo>.git/info/refs?service=git-upload-pack``) — the wire request
``git ls-remote`` makes. Unauthenticated, public, and NOT the GitHub REST API:
``api.github.com`` rate-limits anonymous callers at 60 requests/hour *per source
IP*, and a Lambda outside a VPC draws from a shared AWS egress pool, so a REST
call here would be sharing that budget with strangers. The ref advertisement
has no such quota.

**Boundaries.** ``boto3`` is imported inside :func:`handler`, and every function
above it is pure — string parsing and comparison over values the caller passes
in. That is what lets ``backend/tests/test_cloudwatch_alarms.py`` exercise the
real verdict logic hermetically (no AWS, no network, no boto3 import) instead
of pinning this file as text.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request

# CloudWatch coordinates. These four names are a CONTRACT with the alarm in
# infra/cloudwatch.tf: an alarm watches a namespace/metric/dimension triple
# exactly, and a rename on either side produces an alarm that silently watches
# nothing forever (INSUFFICIENT_DATA reads as calm, not as broken). The guard
# test parses both sides and asserts they agree.
METRIC_NAMESPACE = "Archimedes/Deploy"
METRIC_NAME = "DeployDrift"
METRIC_DIMENSION_NAME = "Service"

# Git's smart-HTTP ref-advertisement suffix. See the module docstring.
_REFS_SUFFIX = "/info/refs?service=git-upload-pack"

# The shortest prefix this will accept as identifying a commit. Git's own
# default abbreviation is 7; anything shorter is ambiguous enough that treating
# a match as proof of alignment would be guessing.
_MIN_PREFIX = 7

# Commit-shaped: lowercase hex, at least an abbreviation, at most a SHA-256.
# Anchored, so "latest" / "v1.2.3" / "main" can never be mistaken for a commit
# and silently compared against the tip.
_COMMITISH_RE = re.compile(rf"\A[0-9a-f]{{{_MIN_PREFIX},64}}\Z")

_HTTP_TIMEOUT_S = 10


def parse_image_tag(image_uri: str | None) -> str | None:
    """Return the tag from an ECR image URI, or ``None`` when it has none.

    ``123.dkr.ecr.us-east-1.amazonaws.com/archimedes-backend:abc123`` -> ``abc123``.

    Two shapes deliberately return ``None`` rather than something tag-shaped:
    a digest reference (``...@sha256:...``), which pins content but says
    nothing about which commit produced it, and a bare untagged repository.
    A registry host may itself carry a ``:port``, so the tag is only ever
    looked for in the final path segment.
    """
    if not image_uri:
        return None
    if "@" in image_uri:
        return None
    last_segment = image_uri.rsplit("/", 1)[-1]
    if ":" not in last_segment:
        return None
    tag = last_segment.rsplit(":", 1)[1].strip()
    return tag or None


def parse_ls_remote(payload: bytes | str, ref: str) -> str | None:
    """Pull one ref's object id out of a git ref advertisement.

    The advertisement is pkt-line framed (a 4-hex length prefix per line) and
    the first entry carries a NUL-separated capability list, so this scans for
    ``<sha> <ref>`` at a token boundary rather than parsing the framing —
    exactly what is needed, and immune to the framing details changing.

    The length prefix is itself hex and butts directly against the object id
    with no separator, so the object-id lengths here are EXACT alternatives
    (64 for SHA-256, else 40 for SHA-1) rather than a ``{40,64}`` range. A
    range is greedy and matches ``003f`` + the first 40 characters of the id,
    yielding a plausible-looking 44-character string that is not any commit —
    every probe would then read "drifted" and page forever. There is no
    lookbehind that fixes this (the preceding character is legitimately hex);
    requiring the exact length is what forces the match onto the id itself.
    The trailing lookahead is what keeps ``refs/heads/main`` from resolving to
    ``refs/heads/main-backup``.
    """
    text = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else payload
    match = re.search(rf"(?:[0-9a-f]{{64}}|[0-9a-f]{{40}})(?=\s+{re.escape(ref)}(?![^\s\0]))", text)
    return match.group(0) if match else None


def looks_like_commit(value: str | None) -> bool:
    """True when ``value`` could name a commit — hex, at least an abbreviation."""
    return bool(value) and bool(_COMMITISH_RE.match(value.lower()))


def tags_match(deployed_tag: str | None, head_sha: str | None) -> bool:
    """Does ``deployed_tag`` identify the commit ``head_sha``?

    Full-SHA equality is the normal case — ``.github/workflows/deploy.yml``
    tags every image with the full ``github.sha``. Prefix matching is accepted
    (in either direction, down to ``_MIN_PREFIX``) so that a hand-rolled deploy
    tagged with an abbreviated SHA does not read as drift when it is not.
    """
    if not deployed_tag or not head_sha:
        return False
    deployed = deployed_tag.lower()
    head = head_sha.lower()
    shorter, longer = sorted((deployed, head), key=len)
    if len(shorter) < _MIN_PREFIX:
        return False
    return longer.startswith(shorter)


def drift_verdict(deployed_tag: str | None, head_sha: str | None) -> tuple[int, str]:
    """``(metric_value, reason)`` — the whole decision, in one pure function.

    ``0`` is returned for exactly one state: a deployed tag that identifies the
    branch tip. Every other state — including all three "cannot tell" states —
    returns ``1``. See the module docstring for why an unanswerable question
    is treated as drift rather than as silence.
    """
    if head_sha is None:
        return 1, "head-unreadable"
    if deployed_tag is None:
        return 1, "image-untagged"
    if not looks_like_commit(deployed_tag):
        # A moving tag such as "latest" cannot be tied back to a commit at all.
        # Kept distinct from "drifted" so the pager knows which question failed.
        return 1, "image-tag-not-a-commit"
    if tags_match(deployed_tag, head_sha):
        return 0, "aligned"
    return 1, "drifted"


def fetch_head_sha(repo_url: str, ref: str) -> str | None:
    """Tip of ``ref`` on ``repo_url``, or ``None`` if the remote cannot be read.

    Never raises: an unreachable remote is a verdict input ("cannot tell"),
    not a crash. Crashing would drop the datapoint entirely, and a dropped
    datapoint is the silence this probe exists to prevent.
    """
    url = repo_url.rstrip("/").removesuffix(".git") + ".git" + _REFS_SUFFIX
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "archimedes-deploy-drift/1"})
        # The URL is assembled from configuration, never from request input.
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_S) as response:
            return parse_ls_remote(response.read(), ref)
    # Broad on purpose: DNS, TLS, HTTP, IAM and parse failures are one verdict
    # input here ("cannot tell"), and narrowing this would let a new failure
    # mode escape as a raise — which drops the datapoint instead of publishing 1.
    except Exception as exc:
        print(json.dumps({"event": "head_fetch_failed", "url": url, "error": repr(exc)}))
        return None


def read_running_image(ecs_client, cluster: str, service: str, container: str) -> str | None:
    """Image URI the service's PRIMARY deployment is running, or ``None``.

    Reads the PRIMARY *deployment*'s task definition rather than the service's
    top-level ``taskDefinition`` field: during a rollout those differ, and the
    PRIMARY deployment is the one being rolled out to. Same never-raises
    contract as :func:`fetch_head_sha`, for the same reason.
    """
    try:
        described = ecs_client.describe_services(cluster=cluster, services=[service])
        services = described.get("services") or []
        if not services:
            print(json.dumps({"event": "service_not_found", "cluster": cluster, "service": service}))
            return None
        entry = services[0]
        deployments = entry.get("deployments") or []
        primary = next((d for d in deployments if d.get("status") == "PRIMARY"), None)
        task_def_arn = (primary or {}).get("taskDefinition") or entry.get("taskDefinition")
        if not task_def_arn:
            return None
        task_def = ecs_client.describe_task_definition(taskDefinition=task_def_arn)
        containers = task_def.get("taskDefinition", {}).get("containerDefinitions") or []
        for definition in containers:
            if definition.get("name") == container:
                return definition.get("image")
        print(json.dumps({"event": "container_not_found", "container": container}))
        return None
    # Broad on purpose, same reasoning as fetch_head_sha: an AccessDenied, a
    # deleted service, a throttle and an unexpected response shape are one
    # verdict input here ("cannot tell"). Narrowing this would let a new
    # failure mode escape as a raise, dropping the datapoint instead of
    # publishing 1 — and a dropped datapoint is the silence this probe exists
    # to prevent.
    except Exception as exc:
        print(json.dumps({"event": "ecs_read_failed", "error": repr(exc)}))
        return None


def handler(event, context):  # noqa: ARG001 - Lambda signature
    """Probe once and publish one datapoint. Scheduled by EventBridge."""
    import boto3

    cluster = os.environ["ECS_CLUSTER"]
    service = os.environ["ECS_SERVICE"]
    container = os.environ["ECS_CONTAINER"]
    repo_url = os.environ["REPO_URL"]
    ref = os.environ["GIT_REF"]

    image_uri = read_running_image(boto3.client("ecs"), cluster, service, container)
    deployed_tag = parse_image_tag(image_uri)
    head_sha = fetch_head_sha(repo_url, ref)
    value, reason = drift_verdict(deployed_tag, head_sha)

    print(
        json.dumps(
            {
                "event": "deploy_drift_probe",
                "metric": f"{METRIC_NAMESPACE}/{METRIC_NAME}",
                "value": value,
                "reason": reason,
                "deployed_tag": deployed_tag,
                "head_sha": head_sha,
                "image": image_uri,
            }
        )
    )

    boto3.client("cloudwatch").put_metric_data(
        Namespace=METRIC_NAMESPACE,
        MetricData=[
            {
                "MetricName": METRIC_NAME,
                "Dimensions": [{"Name": METRIC_DIMENSION_NAME, "Value": service}],
                "Value": value,
                "Unit": "None",
            }
        ],
    )
    return {"value": value, "reason": reason, "deployed_tag": deployed_tag, "head_sha": head_sha}
