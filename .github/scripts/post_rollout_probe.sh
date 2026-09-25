#!/usr/bin/env bash
#
# Post-rollout answer probe — "does the deployed app answer a real client,
# inside a client-realistic deadline, on the commit this run built?"
#
# Extracted from the `Assert the deployed app actually answers (post-rollout
# probe)` step of .github/workflows/deploy.yml (issue #1791) so the hermetic
# test backend/tests/test_post_rollout_probe.py can run THE EXACT SCRIPT CI
# RUNS against a fake /api/health, instead of a paraphrase of it that can
# drift from the workflow.
#
# WHY THE VERDICT IS WHAT IT IS (#1791)
#   The original verdict was "the LAST 5 samples must be consecutive 200s
#   under MAX_SECONDS". Two of the last four main deploys went red on it
#   while prod was already serving the new version:
#     run 33566302291 — samples 1-7 = 200 in ~0.4s, sample 8 http=000
#                       time=15.0s curl_exit=28 (client timeout), 9-10 = 200
#                       -> "only 2 consecutive trailing" -> exit 1
#     run 33573075995 — samples 1-9 = 200 in ~0.4s, sample 10 http=000
#                       time=0.025s curl_exit=35 (TLS reset at 25 ms)
#                       -> "only 0 consecutive trailing" -> exit 1
#   Both were edge/TLS transients, not the app. A probe that cries wolf
#   trains everyone to ignore red deploys, which is the one outcome a probe
#   exists to prevent. So:
#     * TRANSPORT-level failures — NO HTTP ANSWER CAME BACK AT ALL, which
#       curl reports as http_code 000 (connect refused, DNS, TLS handshake
#       reset, client timeout) — are RETRIED up to TRANSPORT_RETRIES times
#       after RETRY_PAUSE before they count. A real outage still fails,
#       because the retries fail too.
#     * ANYTHING THE APP ANSWERED counts IMMEDIATELY as NOT-OK with no
#       retry: a 5xx, any non-200, or a 200 slower than MAX_SECONDS. Those
#       are the app talking, and #1714/#1713's rollout-window 503s must not
#       be retried away.
#     * The verdict is "a run of NEED_STREAK consecutive good samples was
#       OBSERVED SOMEWHERE in the window, AND the final (post-retry) sample
#       is good, AND that final sample reports EXPECTED_VERSION, AND no more
#       than MAX_NOT_OK samples were NOT-OK".
#       "Observed somewhere" is what stops one mid-window transient from
#       failing an otherwise healthy deploy; "final sample is good" is what
#       keeps it fail-closed — a run that is broken AT THE END fails no
#       matter how healthy its start was, so a cold-start tail (the #1532
#       failure mode this probe was built for) still goes red; and
#       MAX_NOT_OK is what stops "observed somewhere" from tolerating a
#       pockmarked window. Without that cap the streak rule alone would pass
#       a window with SAMPLES - NEED_STREAK - 1 = 4 bad samples out of 10
#       (owner ruling, #1791 review): a transient is one or two samples, a
#       third is a degraded deploy and goes red.
#   Every sample and every retry is printed with http/time/curl_exit: the
#   timings ARE the evidence (#1532 had to be diagnosed by hand-curling
#   because the pipeline kept none), and a degradation that this verdict
#   tolerates must still be visible in the log — including a sample that
#   FAILED AND THEN RECOVERED ON RETRY, which is counted and warned on
#   separately (it does not appear in the NOT-OK tally, because it recovered).
#
# Parameters (all via environment):
#   HEALTH_URL         required — probed URL (deploy.yml uses /api/health
#                      through CloudFront, which is on CachingDisabled, so
#                      every sample is a real origin round trip)
#   EXPECTED_VERSION   required — the commit this run deployed
#   SAMPLES            default 10
#   NEED_STREAK        default 5
#   MAX_NOT_OK         default 2   — most NOT-OK samples (after retries) a
#                                    passing run may contain. 3+ is a
#                                    degraded window, not a transient.
#   MAX_SECONDS        default 5   — client-realistic deadline, deliberately
#                                    below #1436's recorded p99 of 16.8s
#   INTERVAL           default 3   — seconds between samples
#   TRANSPORT_RETRIES  default 2   — retries per sample, transport failures only
#   RETRY_PAUSE        default 3   — seconds before each retry
#   CURL_MAX_TIME      default 15  — per-request curl --max-time
#   MAX_TOTAL_SECONDS  default 300 — REAL wall-clock ceiling for the whole
#                      probe, not a between-samples check: no request is
#                      started with a deadline that reaches past it (curl's
#                      --max-time is capped to the remaining budget) and no
#                      sleep runs past it. The only possible overshoot is
#                      sub-second rounding plus the 1s floor this keeps on
#                      --max-time (`curl --max-time 0` means NO timeout).
#                      It matters because deploy.yml's poll step reserves 7
#                      minutes of the deploy-ecs job timeout for "the answer
#                      probe and the CloudFront invalidation": a probe that
#                      overran that reserve would turn a clean ::error into
#                      an opaque job timeout AND eat the invalidation's
#                      share of it. Stopping early always FAILS — the probe
#                      can never pass a deploy it did not finish measuring.
#
# Exit 0 = the app answers. Exit 1 = it does not, or it is answering with
# code this run did not build.

# `set +e` is load-bearing, exactly as it was in the workflow step (#1762):
# GitHub runs `run:` under `bash -e`, and `sample="$(curl ...)"` is an
# assignment whose exit status IS curl's. With -e in force a single timed-out
# sample (curl exit 28) aborted the whole step before rc was read and before a
# single `probe N/10` line was printed — run 33553292497 went red that way with
# the rollout COMPLETED, having never measured whether the app was answering.
# Every failure below is handled explicitly; nothing here may rely on -e.
set +e -uo pipefail

HEALTH_URL="${HEALTH_URL:-}"
EXPECTED_VERSION="${EXPECTED_VERSION:-}"
SAMPLES="${SAMPLES:-10}"
NEED_STREAK="${NEED_STREAK:-5}"
MAX_NOT_OK="${MAX_NOT_OK:-2}"
MAX_SECONDS="${MAX_SECONDS:-5}"
INTERVAL="${INTERVAL:-3}"
TRANSPORT_RETRIES="${TRANSPORT_RETRIES:-2}"
RETRY_PAUSE="${RETRY_PAUSE:-3}"
CURL_MAX_TIME="${CURL_MAX_TIME:-15}"
MAX_TOTAL_SECONDS="${MAX_TOTAL_SECONDS:-300}"

if [ -z "$HEALTH_URL" ] || [ -z "$EXPECTED_VERSION" ]; then
  echo "::error::post-rollout probe: HEALTH_URL and EXPECTED_VERSION are both required (got HEALTH_URL='${HEALTH_URL}', EXPECTED_VERSION='${EXPECTED_VERSION}'). Refusing to report a deploy as verified without probing it."
  exit 1
fi

# Every parameter used in integer arithmetic or an integer test is validated
# BEFORE it is used. `set -e` is off by design (see above), so a `[ "$a" -gt
# "$b" ]` against a non-integer would print a bash error, return 2, and be
# read as FALSE — i.e. a garbage MAX_NOT_OK would silently DISABLE the cap
# and pass a window it must fail. Fail-closed on the misconfiguration instead.
for _int_param in SAMPLES NEED_STREAK MAX_NOT_OK TRANSPORT_RETRIES MAX_TOTAL_SECONDS; do
  eval "_int_value=\${${_int_param}}"
  case "$_int_value" in
    '' | *[!0-9]*)
      echo "::error::post-rollout probe misconfigured: ${_int_param}='${_int_value}' is not a non-negative integer. Refusing to run with a parameter that would silently disable a check."
      exit 1
      ;;
  esac
done
if [ "$NEED_STREAK" -gt "$SAMPLES" ]; then
  echo "::error::post-rollout probe misconfigured: NEED_STREAK=${NEED_STREAK} exceeds SAMPLES=${SAMPLES}, so no run of samples could ever satisfy it and this step could never pass."
  exit 1
fi

BODY_FILE="$(mktemp "${TMPDIR:-/tmp}/post-rollout-probe-body.XXXXXX")"
ERR_FILE="$(mktemp "${TMPDIR:-/tmp}/post-rollout-probe-err.XXXXXX")"
trap 'rm -f "$BODY_FILE" "$ERR_FILE"' EXIT

sample_code=""
sample_secs=""
sample_rc=0
started_at="$(date +%s)"

# Whole seconds of MAX_TOTAL_SECONDS left, never negative. Every piece of work
# the probe starts — request or sleep — is bounded by this, which is what makes
# MAX_TOTAL_SECONDS a ceiling rather than a suggestion.
remaining_budget() {
  local left
  left=$(( MAX_TOTAL_SECONDS - ( $(date +%s) - started_at ) ))
  [ "$left" -gt 0 ] || left=0
  printf '%s' "$left"
}

# Sleep, truncated to the remaining budget (and skipped entirely when there is
# none). RETRY_PAUSE/INTERVAL may be fractional, hence awk rather than `[ -lt ]`.
capped_sleep() {
  local want left
  want="$1"
  left="$(remaining_budget)"
  [ "$left" -gt 0 ] || return 0
  sleep "$(awk -v w="$want" -v l="$left" 'BEGIN { print (w + 0 < l + 0) ? w + 0 : l + 0 }')"
}

# Takes one sample. The body of the LAST request made lands in $BODY_FILE, so
# the version assertion below is made against the very response that closed
# the window rather than an extra request taken afterwards.
take_sample() {
  local out max_time
  # Per-request deadline: CURL_MAX_TIME, but never more wall clock than the
  # probe has left. Floored at 1 because `curl --max-time 0` means NO timeout —
  # the exact opposite of a cap (the caller does not start a sample with zero
  # budget left, so this floor is only ever the sub-second rounding tail).
  max_time="$(awk -v c="$CURL_MAX_TIME" -v l="$(remaining_budget)" \
    'BEGIN { m = (c + 0 < l + 0) ? c + 0 : l + 0; if (m < 1) m = 1; print m }')"
  # Declared and assigned separately on purpose: `local out="$(curl ...)"`
  # would make $? the exit status of `local`, not of curl.
  out="$(curl -sS -o "$BODY_FILE" -w '%{http_code} %{time_total}' \
    --max-time "$max_time" "$HEALTH_URL" 2>"$ERR_FILE")"
  sample_rc=$?
  # curl writes the -w line even on timeout/connect failure (http_code 000);
  # this only backfills the case where it wrote nothing at all, so an empty
  # sample can never be mistaken for a fast 200.
  [ -n "$out" ] || out="000 0"
  sample_code="${out%% *}"
  sample_secs="${out##* }"
}

# Transport level: NO HTTP ANSWER CAME BACK AT ALL. http_code 000 is curl's
# report for connect / DNS / TLS / client-timeout failures, and it is the only
# thing retried here.
#
# Deliberately NOT `|| [ "$sample_rc" -ne 0 ]` (#1791 review, defect 1): curl
# also exits non-zero on a TRUNCATED answer that DID carry a real status line —
# exit 18 "transfer closed with N bytes remaining", exit 56 "recv failure" —
# and an origin that 503s while dropping the body under load is exactly the
# #1714/#1713 rollout-window shape. Retrying those retries a 5xx, which is the
# one thing this probe promises never to do. Both #1791 production shapes
# (curl 28 client timeout, curl 35 TLS reset) report http=000, so nothing real
# is lost by keying on the status line alone.
is_transport_failure() {
  [ "$sample_code" = "000" ]
}

# Good: the app answered 200 inside the client deadline.
is_good() {
  [ "$sample_code" = "200" ] &&
    awk -v t="$sample_secs" -v m="$MAX_SECONDS" 'BEGIN { exit !(t + 0 < m + 0) }'
}

curl_error_text() {
  local line
  line="$(head -1 "$ERR_FILE" 2>/dev/null)"
  [ -n "$line" ] || line="no curl diagnostic"
  printf '%s' "$line"
}

streak=0
best_streak=0
not_ok=0
retried=0
taken=0
final_good=0
deadline_hit=0

echo "Probing $HEALTH_URL — ${SAMPLES} samples every ${INTERVAL}s; need ${NEED_STREAK} consecutive good (200 under ${MAX_SECONDS}s) anywhere in the window AND a good final sample reporting version ${EXPECTED_VERSION} AND at most ${MAX_NOT_OK} NOT-OK samples. ONLY transport-level failures (no HTTP answer at all, http=000) are retried, ${TRANSPORT_RETRIES}x after ${RETRY_PAUSE}s; anything the app answered — a 5xx, any non-200, a 200 slower than ${MAX_SECONDS}s — counts immediately and is NEVER retried. Whole-probe wall-clock ceiling ${MAX_TOTAL_SECONDS}s; hitting it fails the run."

for i in $(seq 1 "$SAMPLES"); do
  elapsed=$(( $(date +%s) - started_at ))
  if [ "$elapsed" -ge "$MAX_TOTAL_SECONDS" ]; then
    echo "probe ${i}/${SAMPLES}: not taken — the probe has already run ${elapsed}s of its ${MAX_TOTAL_SECONDS}s wall-clock ceiling."
    deadline_hit=1
    break
  fi
  attempt=0
  while : ; do
    take_sample
    if [ "$attempt" -eq 0 ]; then
      prefix="$(printf 'probe %2d/%s:' "$i" "$SAMPLES")"
    else
      prefix="$(printf 'probe %2d/%s retry %d/%s:' "$i" "$SAMPLES" "$attempt" "$TRANSPORT_RETRIES")"
    fi
    if is_transport_failure && [ "$attempt" -lt "$TRANSPORT_RETRIES" ]; then
      if [ "$(remaining_budget)" -le 0 ]; then
        # The retry is work, and work stops at the ceiling. The sample counts
        # as it stands (NOT-OK), and the run fails on the ceiling regardless.
        printf '%s transport failure (%s) — NOT retried: the probe has used all %ss of its wall-clock ceiling; the sample counts as it stands\n' \
          "$prefix" "$(curl_error_text)" "$MAX_TOTAL_SECONDS"
        deadline_hit=1
        break
      fi
      printf '%s http=%s time=%ss curl_exit=%s -> transport failure (%s) — retrying in %ss\n' \
        "$prefix" "$sample_code" "$sample_secs" "$sample_rc" "$(curl_error_text)" "$RETRY_PAUSE"
      attempt=$(( attempt + 1 ))
      retried=$(( retried + 1 ))
      capped_sleep "$RETRY_PAUSE"
      continue
    fi
    break
  done

  taken=$(( taken + 1 ))
  if is_good; then
    streak=$(( streak + 1 ))
    final_good=1
    verdict="ok"
  else
    streak=0
    final_good=0
    not_ok=$(( not_ok + 1 ))
    verdict="NOT-OK"
  fi
  [ "$streak" -le "$best_streak" ] || best_streak="$streak"

  # Every sample is printed, passing or not.
  printf '%s http=%s time=%ss curl_exit=%s -> %-6s (consecutive good: %d, best so far: %d)\n' \
    "$prefix" "$sample_code" "$sample_secs" "$sample_rc" "$verdict" "$streak" "$best_streak"

  if [ "$i" -lt "$SAMPLES" ]; then
    capped_sleep "$INTERVAL"
  fi
done

fail=0

if [ "$deadline_hit" -eq 1 ]; then
  # Fail-closed by construction: a probe that ran out of wall clock did not
  # finish measuring, so it cannot report a deploy as verified — no matter how
  # good the samples it did take were.
  echo "::error::post-rollout answer probe: ran out of its ${MAX_TOTAL_SECONDS}s wall-clock budget after ${taken} of ${SAMPLES} samples. Samples were taking long enough that the probe would have eaten the deploy-ecs job's remaining timeout; whatever the app is doing, it is not answering at the speed a finished deploy answers at. Timings are printed above."
  fail=1
fi

if [ "$best_streak" -lt "$NEED_STREAK" ]; then
  echo "::error::post-rollout answer probe: the longest run of consecutive good samples anywhere in the window was ${best_streak} (needed ${NEED_STREAK}) out of ${SAMPLES}. ECS reported the rollout COMPLETED, but the app never answered 200 under ${MAX_SECONDS}s ${NEED_STREAK} times in a row — this is the 58e337dc failure mode from #1532, and it is an outage whether or not the badge would have been green. Every sample is printed above."
  fail=1
fi

if [ "$not_ok" -gt "$MAX_NOT_OK" ]; then
  echo "::error::post-rollout answer probe: ${not_ok} of the ${SAMPLES} samples were NOT-OK after retries (MAX_NOT_OK=${MAX_NOT_OK}). A ${NEED_STREAK}-sample good run was allowed to be observed ANYWHERE in the window (#1791) so that one transient cannot fail a healthy deploy — this cap is what keeps that from tolerating a pockmarked one. ${not_ok} bad samples in a window of ${SAMPLES} is a degraded deploy, not a transient: read the sample lines above for what came back."
  fail=1
fi

if [ "$final_good" -ne 1 ]; then
  if [ "$taken" -eq 0 ]; then
    echo "::error::post-rollout answer probe: the FINAL sample never happened — no sample completed at all (see the error above). Nothing about this deploy has been verified."
  else
    echo "::error::post-rollout answer probe: the FINAL sample taken (${taken}/${SAMPLES}, after up to ${TRANSPORT_RETRIES} transport retries) did not answer 200 under ${MAX_SECONDS}s — http=${sample_code} time=${sample_secs}s curl_exit=${sample_rc}. The window may have looked healthy earlier; it is not healthy now, and now is when the deploy is finishing."
  fi
  fail=1
fi

if [ "$final_good" -eq 1 ]; then
  version="$(jq -r '.version // empty' < "$BODY_FILE" 2>/dev/null)"
  if [ -z "$version" ]; then
    echo "::error::post-rollout version check: could not read .version from ${HEALTH_URL} on the final sample (body was empty or not JSON). Cannot confirm which commit is serving."
    fail=1
  elif [ "$version" != "$EXPECTED_VERSION" ]; then
    echo "::error::post-rollout version check: ${HEALTH_URL} reports version=${version} but this job deployed ${EXPECTED_VERSION}. The rollout completed and the app answers — but it is answering with different code than this run built, so this deploy did not take."
    fail=1
  fi
else
  echo "post-rollout version check: not attempted — the final sample did not answer (see the error above)."
fi

[ "$fail" = 0 ] || exit 1

if [ "$not_ok" -gt 0 ] || [ "$retried" -gt 0 ]; then
  echo "::warning::post-rollout probe passed with ${not_ok} of ${SAMPLES} samples NOT-OK (cap MAX_NOT_OK=${MAX_NOT_OK}) and ${retried} transport retry attempt(s) spent. A sample that failed at transport level and RECOVERED on retry counts as good and never reaches the NOT-OK tally — which is exactly why the retry count is warned on too: a window that needed retries is a window where the origin did not answer the first request (#1791 review, defect 3). Repeated warnings here are a real finding about post-rollout latency/availability (#1309, #1436, #1714) — read the sample lines above."
fi

echo "post-rollout probe OK: longest good streak ${best_streak}/${SAMPLES} (needed ${NEED_STREAK}), ${not_ok} NOT-OK (cap ${MAX_NOT_OK}), ${retried} transport retry attempt(s), final sample 200 in ${sample_secs}s under ${MAX_SECONDS}s, serving version ${version}."
