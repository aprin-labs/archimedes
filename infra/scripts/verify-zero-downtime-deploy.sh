#!/usr/bin/env bash
# Issue #1309 acceptance criterion: "Verify with a timed probe loop across a
# real deploy: 0 non-200s end to end." Previously this lived only as
# copy/paste bash in infra/runbooks/ecs-fargate-cutover.md ("Phase 5 — Verify
# zero-downtime during a rolling deploy") — the runbook itself said "exercise
# it deliberately, don't just assume the config does the right thing", but
# nothing enforced that anyone actually ran it. This script IS that exercise,
# committed and reusable so the check survives copy/paste drift.
#
# What it does:
#   0. Takes one preflight probe and refuses to go further unless it is a
#      200. A probe that cannot reach a healthy ALB would log nothing but
#      non-200s and then blame the rollout for them; failing here means we
#      never trigger a deploy this run has no ability to measure.
#   1. Starts a 1-request/second GET /health loop against the ALB directly
#      (bypasses CloudFront so it measures the target group, not the CDN
#      cache) and logs every response code + latency.
#   2. Force-triggers a new ECS rolling deployment (same task definition —
#      no new image needed to exercise the rolling-replace path).
#   3. Waits for the service to reach steady state. If that waiter gives up
#      (non-zero exit), the run does NOT abort: it still stops the probe,
#      scores the window, and prints a verdict plus the log path — a deploy
#      that never stabilized is the #1309 symptom itself, so that is the
#      moment the measurement matters most. A clean window under a failed
#      waiter is reported as INCONCLUSIVE (exit 1), never as a PASS.
#   4. Fails loudly (non-zero exit, and prints every offending line) if ANY
#      request during the window was not a 200 — a 502/503/000/timeout is a
#      real regression against issue #1309, not something to wave off as
#      flaky.
#
# Requires: awscli (authenticated — same OIDC-assumed role or an operator's
# IAM session; this repo has NO AWS credentials baked in, see CLAUDE.md
# "AWS: ... ask Dan for a scoped IAM user"), curl 7.49+ (for --connect-to;
# see probe_once below for why that flag is load-bearing), terraform (to
# read outputs — run from infra/ with the S3 backend already initialized,
# see infra/README.md).
#
# Usage:
#   cd infra && ./scripts/verify-zero-downtime-deploy.sh
#   # or, without terraform state access, supply everything explicitly:
#   ./scripts/verify-zero-downtime-deploy.sh \
#     --alb-dns archimedes-alb-xxxx.us-east-1.elb.amazonaws.com \
#     --host archimedes-arc.com \
#     --cluster archimedes-cluster --service archimedes-backend
#
# This script does NOT prove what the ROOT CAUSE of a violation is (task
# healthStatus vs ALB target-group health vs config drift between the live
# service and infra/ecs.tf's deployment_minimum_healthy_percent/
# deployment_maximum_percent — see infra/ecs.tf's nginx healthCheck comment
# and the #1309 issue thread) — it only proves whether the acceptance
# criterion holds for the specific deploy it observed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$(dirname "$SCRIPT_DIR")"

ALB_DNS=""
HOST_HEADER="archimedes-arc.com"
CLUSTER=""
SERVICE=""
PROBE_INTERVAL=1
LOG_FILE="$(mktemp -t rollout-watch.XXXXXX.log)"

usage() {
  cat <<EOF
Usage: $0 [--alb-dns HOST] [--host HOST_HEADER] [--cluster NAME] [--service NAME] [--interval SECONDS]

Any flag omitted is read from 'terraform output' in $INFRA_DIR (requires the
S3 backend to already be initialized — infra/README.md).

--interval must be a positive whole number of seconds (default: 1).
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --alb-dns) ALB_DNS="$2"; shift 2 ;;
    --host) HOST_HEADER="$2"; shift 2 ;;
    --cluster) CLUSTER="$2"; shift 2 ;;
    --service) SERVICE="$2"; shift 2 ;;
    --interval) PROBE_INTERVAL="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "::error::unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

# Validate --interval eagerly, at parse time — not implicitly via `sleep`
# failing inside the background probe loop. A bad value there used to kill
# the loop silently under `set -e` (e.g. `sleep: invalid time interval`),
# after which the verdict below would report "PASS — 0 non-200s" over
# whatever partial log happened to exist. Restricted to a positive whole
# number of seconds (not fractional) because the sample-count sanity check
# below does integer arithmetic against it.
case "$PROBE_INTERVAL" in
  ''|*[!0-9]*)
    echo "::error::--interval must be a positive whole number of seconds (got: '$PROBE_INTERVAL')" >&2
    exit 1
    ;;
esac
if [ "$PROBE_INTERVAL" -lt 1 ]; then
  echo "::error::--interval must be >= 1" >&2
  exit 1
fi

command -v curl >/dev/null 2>&1 || { echo "::error::curl is required" >&2; exit 1; }
command -v aws >/dev/null 2>&1 || { echo "::error::awscli is required (this repo ships no AWS credentials — see CLAUDE.md 'AWS' section for how to get a scoped IAM user from Dan)" >&2; exit 1; }

if [ -z "$ALB_DNS" ] || [ -z "$CLUSTER" ] || [ -z "$SERVICE" ]; then
  command -v terraform >/dev/null 2>&1 || { echo "::error::terraform is required to read outputs when --alb-dns/--cluster/--service are not all supplied" >&2; exit 1; }
  pushd "$INFRA_DIR" >/dev/null
  [ -n "$ALB_DNS" ] || ALB_DNS="$(terraform output -raw alb_dns_name)"
  [ -n "$CLUSTER" ] || CLUSTER="$(terraform output -raw ecs_cluster_name)"
  [ -n "$SERVICE" ] || SERVICE="$(terraform output -raw ecs_service_name)"
  popd >/dev/null
fi

echo "ALB:     $ALB_DNS (Host: $HOST_HEADER)"
echo "Cluster: $CLUSTER"
echo "Service: $SERVICE"
echo "Log:     $LOG_FILE"
echo

# ── Background probe loop ───────────────────────────────────────────────
probe_pid=""
# Set by cleanup() iff the loop was still alive (kill -0 succeeded) at the
# moment we stop it. If the loop died earlier (a failure inside it under
# `set -e` — a bad sleep arg, a failed log write, ...) this stays 0 and the
# verdict below hard-fails instead of grading whatever partial log exists.
probe_survived=0
cleanup() {
  if [ -n "$probe_pid" ] && kill -0 "$probe_pid" 2>/dev/null; then
    probe_survived=1
    kill "$probe_pid" 2>/dev/null || true
    wait "$probe_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# One probe, in exactly one place. The preflight below and the background
# loop after it both call this, so the preflight genuinely exercises the
# shipped probe form rather than an approximation of it.
#
# `--connect-to` is load-bearing, not a stylistic choice. The obvious form
# — `-H "Host: $HOST_HEADER" "https://$ALB_DNS/health"` — CANNOT WORK over
# TLS: a `Host:` header is an HTTP-layer thing, set after the TLS handshake
# has already happened, so it changes neither the SNI curl sends nor the
# name curl verifies the presented certificate against. Both are taken from
# the URL's host, which in that form is the raw ALB DNS name
# (`archimedes-alb-*.us-east-1.elb.amazonaws.com`), and the ALB's listener
# certificate (`aws_acm_certificate.main`, `infra/alb.tf` — `domain_name =
# var.domain_name`, no `subject_alternative_names`) covers
# archimedes-arc.com and nothing else. So every
# probe would fail at the TLS layer before sending any request (curl exits
# 35 or 60 depending on the TLS backend) and `%{http_code}` would be `000`
# — i.e. the script could only ever report a total outage, on every run, no
# matter how healthy the service was. Reproduced against a real host:
# `curl -H "Host: example.com" https://<example.com's IP>/` returns
# http_code=000 (exit 35), while the `--connect-to` form below against the
# identical IP returns http_code=200.
# `--connect-to HOST:443:ALB:443` instead keeps the URL (and
# therefore the SNI, the certificate check, and the Host header) as
# $HOST_HEADER while pointing the TCP connection at the ALB — which is the
# whole point of this probe: hit the target group directly, bypassing
# CloudFront, without disabling TLS verification. (`-k` would also "work"
# and is deliberately not used: it would mask a genuinely broken listener
# certificate, which is itself an outage this probe should catch.)
#
# `-w` output is written by curl even when curl itself exits non-zero (a
# connect failure or a --max-time abort still prints "000 <time>s"), so the
# old `... || echo "000 0s"` appended a SECOND verdict onto the first: a
# failed probe logged the mangled `000 0.000367s000 0s` rather than one
# clean sample. The `|| :` keeps a non-zero curl from killing the caller
# under `set -e` without adding that duplicate; callers handle empty output
# (the case where curl printed nothing at all) themselves.
probe_once() {
  curl -o /dev/null -s -w "%{http_code} %{time_total}s" \
    --max-time 5 \
    --connect-to "$HOST_HEADER:443:$ALB_DNS:443" \
    "https://$HOST_HEADER/health" 2>/dev/null || :
}

# ── Preflight ───────────────────────────────────────────────────────────
# Take ONE probe, through the identical code path, BEFORE touching the
# service. A probe that cannot reach a healthy ALB records nothing but
# non-200s and then blames the rollout for them — a guaranteed FAIL that
# says nothing about issue #1309 (the pre-`--connect-to` version of this
# script had exactly that defect: TLS verification failed on every request,
# so every run reported a total outage). Aborting here also means we never
# trigger a deployment we have no ability to measure.
echo "Preflight: one probe through the ALB before touching the service..."
preflight_sample="$(probe_once)"
preflight_code="${preflight_sample%% *}"
if [ "$preflight_code" != "200" ]; then
  echo "::error::preflight probe returned '${preflight_sample:-<no output>}', expected 200 — refusing to trigger a deploy this run could not measure." >&2
  echo "Check, in order: (a) --alb-dns and --host are correct and the ALB listener serves a certificate valid for '$HOST_HEADER'; (b) this curl supports --connect-to (7.49+); (c) the service is actually healthy right now — a real ongoing outage fails here too, which is the point." >&2
  exit 1
fi
echo "Preflight OK ($preflight_sample)."
echo

loop_start_epoch="$(date -u +%s)"
(
  while true; do
    code_and_time="$(probe_once)"
    [ -n "$code_and_time" ] || code_and_time="000 0s"
    # Epoch seconds FIRST — the field the verdict's window comparisons key
    # off of. A raw HH:MM:SS *string* compare (the previous approach) wraps
    # incorrectly across UTC midnight (e.g. "23:59:58" >= "00:00:09" is
    # false), which would silently score a real, clean rollout as "0 probes
    # fell inside the window" and fail it for no reason. Epoch seconds are
    # monotonic and immune to that. HH:MM:SS is still logged second, purely
    # for a human reading the raw file — not %3N: GNU date supports
    # sub-second precision but BSD/macOS date (Dan's local shell, darwin)
    # does not, and silently prints the literal "3N" instead of erroring,
    # which would corrupt every logged line. 1-second resolution is enough
    # at this probe interval.
    echo "$(date -u +%s) $(date -u +%H:%M:%S) $code_and_time" >> "$LOG_FILE"
    sleep "$PROBE_INTERVAL"
  done
) &
probe_pid=$!
echo "Probe loop running (pid $probe_pid, 1 req/${PROBE_INTERVAL}s)..."

# Let the probe establish a healthy baseline before triggering the deploy —
# a cold-start 000/5xx before the loop is warmed up would be a false positive
# unrelated to the rollout itself.
sleep 5

# ── Trigger + wait for the rolling deployment ───────────────────────────
echo "Triggering force-new-deployment on $SERVICE..."
aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" --force-new-deployment >/dev/null

deploy_start="$(date -u +%H:%M:%S)"
deploy_start_epoch="$(date -u +%s)"
echo "Deploy started at $deploy_start (UTC) — waiting for steady state..."
# The waiter's exit code is CAPTURED, not allowed to abort the run under
# `set -e`. It gives up after its own fixed budget (40 attempts x 15s = 10
# min) and a failure there is exactly the case where the operator most needs
# the measurement — a deploy that never stabilized is the #1309 symptom, and
# dying here would have thrown away the probe log and printed no verdict at
# all. The waiter's own reliability is separately tracked in issue #1532
# (which replaces it in `.github/workflows/deploy.yml`); this script only has
# to stop treating its failure as a reason to report nothing.
wait_rc=0
aws ecs wait services-stable --cluster "$CLUSTER" --services "$SERVICE" || wait_rc=$?
deploy_end="$(date -u +%H:%M:%S)"
if [ "$wait_rc" -eq 0 ]; then
  echo "Steady state reached at $deploy_end (UTC)."
else
  echo "WARNING — 'aws ecs wait services-stable' exited $wait_rc at $deploy_end (UTC): the service did NOT reach steady state within the waiter's budget."
  echo "Continuing to score the probe window anyway — the measurement below is the point."
fi

# Give the probe a few more seconds past steady-state before stopping, to
# catch any post-"stable" wobble (e.g. a CloudFront/DNS propagation tail).
# This tail IS part of the scored window below (it exists specifically to
# catch a real regression) — only the pre-trigger baseline, before
# deploy_start, is excluded from the verdict.
sleep 5
probe_end="$(date -u +%H:%M:%S)"
probe_end_epoch="$(date -u +%s)"
cleanup
trap - EXIT

# ── Verdict ──────────────────────────────────────────────────────────────
# The probe loop must actually have been alive for cleanup() to stop it —
# otherwise whatever is in $LOG_FILE is a truncated fragment from wherever
# the loop died, not a measurement spanning the rollout. An empty or
# partial log must never read as "PASS — 0 non-200s".
if [ "$probe_survived" -ne 1 ]; then
  echo
  echo "FAIL — the probe loop was not running at cleanup time (it died, or never started)."
  echo "This is not a measurement of issue #1309's acceptance criterion — it's a missing one."
  echo "Partial log (if any) kept at $LOG_FILE"
  exit 1
fi

total_all=$(wc -l < "$LOG_FILE" | tr -d ' ')
if [ "$total_all" -eq 0 ]; then
  echo
  echo "FAIL — 0 probes were recorded end to end; no measurement was taken."
  echo "Log kept at $LOG_FILE"
  exit 1
fi

# Score only samples from deploy_start through probe_end (the rollout +
# post-steady-state tail). Pre-trigger baseline samples are a known
# false-positive source (a cold-start 000/5xx before the loop is warmed up
# — see the baseline-sleep comment above) and are reported separately as a
# warning, never as an acceptance-criterion violation. Compared on field 1
# (epoch seconds), NOT field 2 (the HH:MM:SS string) — a raw string
# compare wraps incorrectly across UTC midnight (e.g. "23:59:58" >=
# "00:00:09" is false), which would score a clean rollout as "0 probes
# fell inside the window" and fail it for no reason. Field 3 is the http
# status (field indices shifted by one now that field 1 is epoch seconds).
window_total=$(awk -v s="$deploy_start_epoch" -v e="$probe_end_epoch" '$1 >= s && $1 <= e { c++ } END { print c+0 }' "$LOG_FILE")
bad_lines=$(awk -v s="$deploy_start_epoch" -v e="$probe_end_epoch" '$1 >= s && $1 <= e && $3 != "200" { print }' "$LOG_FILE")
pre_window_bad=$(awk -v s="$deploy_start_epoch" '$1 < s && $3 != "200" { print }' "$LOG_FILE")

echo
echo "=== $(basename "$LOG_FILE") — $window_total probes, window ${deploy_start}Z .. ${probe_end}Z ==="

if [ -n "$pre_window_bad" ]; then
  echo "WARNING — non-200 response(s) before the rollout window (pre-trigger baseline, not scored):"
  echo "$pre_window_bad"
  echo
fi

if [ "$window_total" -eq 0 ]; then
  echo "FAIL — 0 probes fell inside the rollout window (${deploy_start}Z .. ${probe_end}Z); nothing was measured."
  echo "Full log kept at $LOG_FILE"
  exit 1
fi

# bad_lines is evaluated BEFORE the sample-count sanity check below — a
# genuine in-window outage (e.g. every request timing out) must fail here
# as an acceptance-criterion violation, with its offending lines printed,
# never get reclassified by the sample-count check as "not a reliable
# measurement" and have those lines swallowed. A timed-out probe (curl's
# --max-time 5 below) still logs a real "000" sample — it costs ~6s
# instead of ~1s, so an in-window outage covering a majority of the window
# also depresses the sample count, which is exactly the failure mode the
# old ordering mis-triaged. See #1309's own incident: 144s of a ~180s
# window down.
if [ -n "$bad_lines" ]; then
  echo "FAIL — non-200 response(s) observed during the rollout window (issue #1309 acceptance criterion violated):"
  echo "$bad_lines"
  echo
  echo "Full log kept at $LOG_FILE"
  exit 1
fi

# Sanity-check the sample count against how long the loop actually ran
# (loop start .. tail end). The loop can stay "alive" per kill -0 while
# stalling partway (a wedged write, a hung curl) and produce far fewer
# samples than the window warrants — that's not caught by probe_survived
# alone. Downgraded to a WARNING attached to the PASS verdict (not a hard
# exit): by the time we reach here, bad_lines is already known to be
# empty, so an in-window outage (the case that genuinely needs a FAIL) is
# ruled out — what's left is a merely slow/stalled loop, worth flagging
# but not worth turning a real 0-non-200s result into an unscored FAIL.
elapsed_seconds=$(( probe_end_epoch - loop_start_epoch ))
[ "$elapsed_seconds" -gt 0 ] || elapsed_seconds=1
expected=$(( elapsed_seconds / PROBE_INTERVAL ))
[ "$expected" -ge 1 ] || expected=1
if [ $(( total_all * 2 )) -lt "$expected" ]; then
  echo "WARNING — only $total_all probes recorded over a ${elapsed_seconds}s run at ${PROBE_INTERVAL}s/probe (~$expected expected). The loop may have stalled partway through; treat this PASS with caution."
  echo
fi

# 0 non-200s is necessary but not sufficient: if the waiter never confirmed
# steady state, the window this scored may simply have ended before the
# rollout finished, so "0 non-200s" is not yet the acceptance criterion —
# it's an inconclusive partial measurement, and reporting it as PASS would
# be exactly the kind of false green this script exists to prevent.
if [ "$wait_rc" -ne 0 ]; then
  echo "INCONCLUSIVE — 0 non-200 responses across $window_total probes, but 'aws ecs wait services-stable' exited $wait_rc, so the rollout was never confirmed complete."
  echo "The scored window may have closed mid-rollout; this is NOT a pass of issue #1309's acceptance criterion. Re-run once the service stabilizes."
  echo "Full log kept at $LOG_FILE"
  exit 1
fi

echo "PASS — 0 non-200 responses across $window_total probes during the rollout window."
echo "Full log kept at $LOG_FILE"
exit 0
