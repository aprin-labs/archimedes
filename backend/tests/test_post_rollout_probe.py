"""The post-rollout probe, run against a fake ``/api/health`` (#1791).

``.github/scripts/post_rollout_probe.sh`` is the body of deploy.yml's
"Assert the deployed app actually answers (post-rollout probe)" step. It was
inline in the workflow, which meant nothing could execute it: the two red
main deploys in #1791 —

    run 33566302291  samples 1-7 = 200 in ~0.4s, sample 8 http=000
                     time=15.0s curl_exit=28 (client timeout), 9-10 = 200
                     -> "only 2 consecutive trailing" -> exit 1
    run 33573075995  samples 1-9 = 200 in ~0.4s, sample 10 http=000
                     time=0.025s curl_exit=35 (TLS reset at 25 ms)
                     -> "only 0 consecutive trailing" -> exit 1

were both green deploys reported red by an unexecutable probe. These tests
run THE EXACT SCRIPT CI RUNS against a local HTTP server that can be told to
drop a connection, answer 503, answer slowly, answer with the wrong version,
or answer 503 and then hang up mid-body — no AWS, no network, no CloudFront.

Fail-closed is the property under test as much as flake-tolerance is: a
transient inside a healthy window passes, an outage that reaches the end of
the window still fails, and a window that is bad more than MAX_NOT_OK times
fails even when a good streak was observed.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import socket
import subprocess
import threading
import time
from collections import deque
from collections.abc import Iterator, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PROBE_SH = REPO_ROOT / ".github" / "scripts" / "post_rollout_probe.sh"
DEPLOY_YML = REPO_ROOT / ".github" / "workflows" / "deploy.yml"
PROBE_STEP_NAME = "Assert the deployed app actually answers (post-rollout probe)"

DEPLOYED_SHA = "7a6c5b7300112233445566778899aabbccddeeff"
OTHER_SHA = "25dc64dcffeeddccbbaa99887766554433221100"


class _FakeHealth:
    """A scripted ``/api/health``.

    ``actions`` is consumed one entry per HTTP REQUEST (a retry consumes its
    own entry); once exhausted the last entry repeats forever, so
    ``["ok"] * 5 + ["reset"]`` means "healthy, then broken from sample 6 on".

    Actions:
      ``ok``            200 ``{"version": DEPLOYED_SHA}``
      ``ok:<version>``  200 with that version instead
      ``reset``         close the connection without writing a response —
                        curl reports http_code 000 with a non-zero exit, the
                        transport-level flake class from #1791's two red runs
      ``status:<code>`` that status with a JSON body (the app answering)
      ``slow:<secs>``   sleep, then 200 — a 200 outside the client deadline
    """

    def __init__(self, actions: Sequence[str]) -> None:
        if not actions:
            raise ValueError("at least one action is required")
        self._queue = deque(actions)
        self._last = actions[-1]
        self._lock = threading.Lock()
        self.served: list[str] = []

    def next_action(self) -> str:
        with self._lock:
            action = self._queue.popleft() if self._queue else self._last
            self._last = action
            self.served.append(action)
            return action

    @property
    def request_count(self) -> int:
        with self._lock:
            return len(self.served)


def _handler_for(fake: _FakeHealth) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:  # keep pytest output readable
            pass

        def do_GET(self) -> None:  # BaseHTTPRequestHandler's own casing
            action = fake.next_action()

            if action == "reset":
                # Not a 5xx and not a timeout — no HTTP answer at all. curl
                # reports http_code 000 with exit 52/56, exactly what an edge
                # or TLS transient looks like to the probe.
                self.close_connection = True
                with contextlib.suppress(OSError):
                    self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return

            if action.startswith("slow:"):
                time.sleep(float(action.split(":", 1)[1]))
                action = "ok"

            if action.startswith("status:"):
                self._respond(int(action.split(":", 1)[1]), {"detail": "service unavailable"})
                return

            version = DEPLOYED_SHA if action == "ok" else action.split(":", 1)[1]
            self._respond(200, {"status": "ok", "version": version})

        def _respond(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


@contextlib.contextmanager
def _serving(fake: _FakeHealth) -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(fake))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/api/health"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextlib.contextmanager
def _serving_truncated_503() -> Iterator[tuple[str, list[str]]]:
    """A raw socket that answers ``503`` and then hangs up mid-body.

    This is the shape ``is_transport_failure`` used to get wrong (#1791
    review, defect 1): the status line DID come back, so it is the app
    answering — but curl still exits non-zero (18, "transfer closed with N
    bytes remaining to read") because the promised body never arrived. An
    origin/ALB that 503s while dropping bodies under load is precisely the
    #1714/#1713 rollout-window shape that must never be retried away.

    http.server cannot produce this — it always finishes what it starts — so
    the response is written byte by byte onto a raw socket.
    """
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    requests: list[str] = []
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            try:
                conn.settimeout(5)
                with contextlib.suppress(OSError):
                    requests.append(conn.recv(4096).decode("latin-1", "replace"))
                with contextlib.suppress(OSError):
                    conn.sendall(
                        b"HTTP/1.1 503 Service Unavailable\r\n"
                        b"Content-Type: application/json\r\n"
                        b"Content-Length: 100\r\n"
                        b"\r\n"
                        b'{"detail":'  # 10 bytes of the 100 promised, then gone
                    )
                    conn.shutdown(socket.SHUT_RDWR)
            finally:
                conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}/api/health", requests
    finally:
        stop.set()
        listener.close()
        thread.join(timeout=5)


def _run_script(
    url: str,
    *,
    samples: int = 10,
    need_streak: int = 5,
    max_not_ok: str = "2",
    max_seconds: str = "5",
    transport_retries: int = 2,
    expected_version: str = DEPLOYED_SHA,
    max_total_seconds: str = "300",
    curl_max_time: str = "10",
    retry_pause: str = "0.1",
    interval: str = "0.1",
) -> subprocess.CompletedProcess[str]:
    """Run the real script against ``url``.

    INTERVAL and RETRY_PAUSE default to 0.1s here and 3s in CI; every other
    number is the one deploy.yml ships with unless a test says otherwise.
    """
    env = {
        **os.environ,
        "HEALTH_URL": url,
        "EXPECTED_VERSION": expected_version,
        "SAMPLES": str(samples),
        "NEED_STREAK": str(need_streak),
        "MAX_NOT_OK": max_not_ok,
        "MAX_SECONDS": max_seconds,
        "INTERVAL": interval,
        "TRANSPORT_RETRIES": str(transport_retries),
        "RETRY_PAUSE": retry_pause,
        "CURL_MAX_TIME": curl_max_time,
        "MAX_TOTAL_SECONDS": max_total_seconds,
    }
    return subprocess.run(
        ["bash", str(PROBE_SH)],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def run_probe(
    actions: Sequence[str],
    *,
    url_override: str | None = None,
    **kwargs: object,
) -> tuple[subprocess.CompletedProcess[str], _FakeHealth]:
    """Run the real script against a scripted fake health endpoint."""
    fake = _FakeHealth(actions)
    with _serving(fake) as url:
        proc = _run_script(url_override or url, **kwargs)  # type: ignore[arg-type]
    return proc, fake


def _log(proc: subprocess.CompletedProcess[str]) -> str:
    return f"\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"


def _errors(proc: subprocess.CompletedProcess[str]) -> list[str]:
    return [line for line in proc.stdout.splitlines() if line.startswith("::error::")]


def _retry_lines(proc: subprocess.CompletedProcess[str]) -> list[str]:
    """Lines for an actual retried REQUEST — not prose that contains "retry"."""
    return re.findall(r"^.*retry \d+/\d+:.*$", proc.stdout, re.M)


class TestHealthyWindow:
    def test_all_good_samples_pass(self) -> None:
        proc, fake = run_probe(["ok"])
        assert proc.returncode == 0, _log(proc)
        assert "post-rollout probe OK" in proc.stdout
        assert fake.request_count == 10, "one request per sample, no retries needed"
        # Every sample printed — the timings ARE the evidence (#1532).
        assert len(re.findall(r"^probe\s+\d+/10:", proc.stdout, re.M)) == 10, _log(proc)
        # A clean window earns no warning at all.
        assert "::warning::" not in proc.stdout, _log(proc)


class TestTransportRetry:
    def test_one_transport_reset_mid_window_is_retried_and_passes(self) -> None:
        """Run 33566302291's shape: healthy window, one dropped connection."""
        proc, fake = run_probe(["ok"] * 6 + ["reset", "ok"])
        assert proc.returncode == 0, _log(proc)
        assert re.search(r"^probe\s+7/10: http=000 .*transport failure.*retrying", proc.stdout, re.M), _log(proc)
        assert re.search(r"^probe\s+7/10 retry 1/2: http=200 .*-> ok", proc.stdout, re.M), _log(proc)
        assert fake.request_count == 11, "the retry is one extra request, not a re-run of the window"

    def test_a_sample_that_recovered_on_retry_is_still_annotated(self) -> None:
        """#1791 review, defect 3: a recovered retry left NO trace in the verdict.

        The sample counts as good — the origin still failed to answer the
        first request, which is the modal shape of both #1791 red runs, and a
        run that reports "10/10, no warnings" would be hiding it.

        MUTATION: gate the warning on ``not_ok`` alone and this goes red.
        """
        proc, _ = run_probe(["ok"] * 6 + ["reset", "ok"])
        assert proc.returncode == 0, _log(proc)
        warnings = [line for line in proc.stdout.splitlines() if line.startswith("::warning::")]
        assert warnings, "a retried-and-recovered sample must not pass silently" + _log(proc)
        assert "0 of 10 samples NOT-OK" in warnings[0], _log(proc)
        assert "1 transport retry attempt(s) spent" in warnings[0], _log(proc)
        assert "1 transport retry attempt(s)" in proc.stdout.splitlines()[-1], "the summary line names it too"

    def test_transport_reset_on_the_final_sample_is_retried_and_passes(self) -> None:
        """Run 33573075995's shape: the TLS reset landed on sample 10 of 10.

        MUTATION: delete the retry branch from the script and this goes red —
        the final sample counts NOT-OK and the deploy is reported failed while
        prod serves the new version.
        """
        proc, fake = run_probe(["ok"] * 9 + ["reset", "ok"])
        assert proc.returncode == 0, _log(proc)
        assert re.search(r"^probe\s+10/10 retry 1/2: http=200 .*-> ok", proc.stdout, re.M), _log(proc)
        assert fake.request_count == 11

    def test_mid_window_outage_burst_passes_on_an_observed_streak_and_a_good_final_sample(self) -> None:
        """Sample 6 fails through both retries; samples 1-5 and 7-10 are good.

        The trailing run is only 4, so the OLD "last 5 consecutive" verdict
        called this a failed deploy. The observed streak of 5 plus a good
        final sample is what makes it a pass.

        MUTATION: judge on the trailing streak instead of the observed one and
        this goes red.
        """
        proc, fake = run_probe(["ok"] * 5 + ["reset"] * 3 + ["ok"])
        assert proc.returncode == 0, _log(proc)
        assert re.search(r"^probe\s+6/10 retry 2/2: http=000 .*-> NOT-OK", proc.stdout, re.M), _log(proc)
        assert "best so far: 5" in proc.stdout
        # Tolerated, but never hidden: the log still carries a warning.
        assert "::warning::post-rollout probe passed with 1 of 10 samples NOT-OK" in proc.stdout, _log(proc)
        assert fake.request_count == 12

    def test_transport_failure_that_persists_to_the_end_fails(self) -> None:
        """Fail-closed: five consecutive broken samples at the end of a window.

        A healthy streak of 5 WAS observed (samples 1-5) — the run still fails,
        because the final sample is what says whether the deploy landed.
        """
        proc, _ = run_probe(["ok"] * 5 + ["reset"])
        assert proc.returncode == 1, _log(proc)
        assert any("the FINAL sample" in e for e in _errors(proc)), _log(proc)
        assert any("version check: not attempted" in line for line in proc.stdout.splitlines())

    def test_total_transport_outage_fails(self) -> None:
        """Every sample and every retry dropped — the app is down."""
        proc, fake = run_probe(["reset"])
        assert proc.returncode == 1, _log(proc)
        errors = _errors(proc)
        assert any("longest run of consecutive good samples anywhere in the window was 0" in e for e in errors), _log(
            proc
        )
        assert any("the FINAL sample" in e for e in errors), _log(proc)
        assert fake.request_count == 30, "10 samples x (1 attempt + 2 retries) — the retries are bounded"


class TestAppLevelFailuresAreNotRetried:
    def test_one_503_inside_the_window_restarts_the_streak_but_a_new_streak_forms(self) -> None:
        proc, fake = run_probe(["ok", "ok", "status:503", "ok"])
        assert proc.returncode == 0, _log(proc)
        assert re.search(r"^probe\s+3/10: http=503 .*-> NOT-OK \(consecutive good: 0", proc.stdout, re.M), _log(proc)
        assert not _retry_lines(proc), "a 5xx is the app answering — retrying it would mask #1714's 503 windows"
        assert fake.request_count == 10

    def test_a_truncated_5xx_is_not_retried(self) -> None:
        """#1791 review, defect 1: ``|| curl_exit != 0`` retried a REAL 503.

        The origin sends ``503`` with ``Content-Length: 100`` and hangs up
        after 10 bytes: curl reports ``http_code=503`` AND exits 18. That is
        the app answering — #1714/#1713's rollout-window 503s must stay
        visible, so it counts NOT-OK immediately and burns no retry.

        MUTATION: restore ``|| [ "$sample_rc" -ne 0 ]`` in
        ``is_transport_failure`` and this goes red (3 retry lines appear and
        the request count triples).
        """
        with _serving_truncated_503() as (url, requests):
            proc = _run_script(url, samples=3, need_streak=2)
        assert proc.returncode == 1, _log(proc)
        assert re.search(r"^probe\s+1/3: http=503 .*curl_exit=(18|56) .*-> NOT-OK", proc.stdout, re.M), _log(proc)
        assert not _retry_lines(proc), "a truncated 5xx is still a 5xx" + _log(proc)
        assert len(requests) == 3, f"one request per sample, no retry burned — got {len(requests)}" + _log(proc)
        assert any("the FINAL sample" in e and "http=503" in e for e in _errors(proc)), _log(proc)

    def test_503_that_prevents_any_streak_fails(self) -> None:
        proc, _ = run_probe(["ok", "ok", "status:503", "ok"], samples=6)
        assert proc.returncode == 1, _log(proc)
        assert any("was 3 (needed 5)" in e for e in _errors(proc)), _log(proc)

    def test_503_on_the_final_sample_fails(self) -> None:
        proc, fake = run_probe(["ok"] * 9 + ["status:503"])
        assert proc.returncode == 1, _log(proc)
        assert any("the FINAL sample" in e and "http=503" in e for e in _errors(proc)), _log(proc)
        assert fake.request_count == 10, "no retry burned on a 5xx"

    def test_slow_200_counts_not_ok_and_is_not_retried(self) -> None:
        proc, fake = run_probe(["ok"] * 9 + ["slow:1.6"], max_seconds="1")
        assert proc.returncode == 1, _log(proc)
        assert re.search(r"^probe\s+10/10: http=200 .*-> NOT-OK", proc.stdout, re.M), _log(proc)
        assert any("the FINAL sample" in e and "http=200" in e for e in _errors(proc)), _log(proc)
        assert fake.request_count == 10, "a slow 200 is the app answering slowly, not a transport flake"


class TestNotOkCap:
    """Owner ruling (#1791 review): a streak alone must not pass a bad window.

    "Observed somewhere in the window" on its own tolerates
    ``SAMPLES - NEED_STREAK - 1`` = 4 bad samples out of 10. Two is a
    transient; three is a degraded deploy.
    """

    def test_two_not_ok_samples_with_a_streak_and_a_good_final_sample_pass_with_a_warning(self) -> None:
        proc, fake = run_probe(["ok"] * 5 + ["status:503"] * 2 + ["ok"])
        assert proc.returncode == 0, _log(proc)
        assert "::warning::post-rollout probe passed with 2 of 10 samples NOT-OK" in proc.stdout, _log(proc)
        assert not any("were NOT-OK after retries" in e for e in _errors(proc)), _log(proc)
        assert fake.request_count == 10

    def test_three_not_ok_samples_fail_even_with_a_streak_and_a_good_final_sample(self) -> None:
        """MUTATION: delete the MAX_NOT_OK check and this exits 0."""
        proc, fake = run_probe(["ok"] * 5 + ["status:503"] * 3 + ["ok"])
        assert proc.returncode == 1, _log(proc)
        cap_errors = [e for e in _errors(proc) if "were NOT-OK after retries" in e]
        assert cap_errors, "the cap must be the thing that failed this run" + _log(proc)
        assert "3 of the 10 samples were NOT-OK after retries (MAX_NOT_OK=2)" in cap_errors[0], _log(proc)
        # The other three conjuncts all held — only the cap failed it.
        assert not any("longest run of consecutive good samples" in e for e in _errors(proc)), _log(proc)
        assert not any("the FINAL sample" in e for e in _errors(proc)), _log(proc)
        assert "best so far: 5" in proc.stdout
        assert fake.request_count == 10

    def test_the_cap_can_be_tightened_to_zero(self) -> None:
        proc, _ = run_probe(["ok"] * 5 + ["status:503"] + ["ok"], max_not_ok="0")
        assert proc.returncode == 1, _log(proc)
        assert any("1 of the 10 samples were NOT-OK after retries (MAX_NOT_OK=0)" in e for e in _errors(proc)), _log(
            proc
        )

    def test_a_non_integer_cap_is_rejected_instead_of_silently_disabling_the_check(self) -> None:
        """`set -e` is off: `[ 3 -gt "two" ]` returns 2, which reads as FALSE.

        A typo'd cap must not quietly pass a window the cap exists to fail.
        """
        proc, fake = run_probe(["ok"] * 5 + ["status:503"] * 3 + ["ok"], max_not_ok="two")
        assert proc.returncode == 1, _log(proc)
        assert any("MAX_NOT_OK='two' is not a non-negative integer" in e for e in _errors(proc)), _log(proc)
        assert fake.request_count == 0, "rejected before a single request"


class TestVersionAssertion:
    def test_wrong_version_fails_and_the_error_names_both_versions(self) -> None:
        """MUTATION: drop the version comparison and this goes red."""
        proc, _ = run_probe([f"ok:{OTHER_SHA}"])
        assert proc.returncode == 1, _log(proc)
        version_errors = [e for e in _errors(proc) if "version check" in e]
        assert version_errors, _log(proc)
        assert OTHER_SHA in version_errors[0] and DEPLOYED_SHA in version_errors[0], _log(proc)

    def test_version_is_read_from_the_final_sample_not_an_extra_request(self) -> None:
        """The final sample is served by its retry — the version must come from it."""
        proc, fake = run_probe(["ok"] * 9 + ["reset", f"ok:{OTHER_SHA}"])
        assert proc.returncode == 1, _log(proc)
        assert any(OTHER_SHA in e for e in _errors(proc)), _log(proc)
        assert fake.request_count == 11, "no extra request after the window — the verdict is on what was measured"

    def test_body_that_is_not_json_fails(self) -> None:
        proc, _ = run_probe(["ok:"])  # 200 with .version == ""
        assert proc.returncode == 1, _log(proc)
        assert any("could not read .version" in e for e in _errors(proc)), _log(proc)


class TestBudgetsAndMisconfiguration:
    def test_probe_stops_at_its_wall_clock_ceiling_and_fails(self) -> None:
        """Fail-closed: an unfinished probe cannot report a deploy as verified.

        The samples it did take satisfied NEED_STREAK, so the wall-clock
        ceiling is the only thing failing this run.
        """
        proc, _ = run_probe(["slow:1.0"], need_streak=1, max_seconds="5", max_total_seconds="3")
        assert proc.returncode == 1, _log(proc)
        errors = _errors(proc)
        assert any("wall-clock budget" in e for e in errors), _log(proc)
        assert not any("longest run of consecutive good samples" in e for e in errors), _log(proc)

    def test_the_ceiling_bounds_the_whole_probe_not_just_the_gap_between_samples(self) -> None:
        """#1791 review, defect 2: MAX_TOTAL_SECONDS was checked only BEFORE a
        sample, so the last one could still start a full retry cycle
        (``(TRANSPORT_RETRIES + 1) x CURL_MAX_TIME + retries x RETRY_PAUSE +
        INTERVAL``) past it — 353s of overshoot at shipped defaults, eating
        the reserve deploy.yml's poll step holds back for the CloudFront
        invalidation.

        4s ceiling, 4s retry pause, 4s interval: the old between-samples check
        let this run ~12s.

        MUTATION: drop the budget check on the retry path and the `capped_`
        halves of the sleeps and this goes red on wall clock alone.
        """
        started = time.monotonic()
        proc, _ = run_probe(["reset"], max_total_seconds="4", retry_pause="4", interval="4")
        elapsed = time.monotonic() - started
        assert proc.returncode == 1, _log(proc)
        assert any("wall-clock budget" in e for e in _errors(proc)), _log(proc)
        assert elapsed < 8, f"probe ran {elapsed:.1f}s against a 4s ceiling{_log(proc)}"

    def test_a_single_slow_request_cannot_run_past_the_ceiling(self) -> None:
        """curl's own --max-time is capped to the budget the probe has left."""
        started = time.monotonic()
        proc, _ = run_probe(["slow:8"], samples=3, need_streak=1, max_total_seconds="3", curl_max_time="20")
        elapsed = time.monotonic() - started
        assert proc.returncode == 1, _log(proc)
        assert elapsed < 6, f"probe ran {elapsed:.1f}s against a 3s ceiling{_log(proc)}"

    def test_missing_required_env_fails_without_probing(self) -> None:
        env = {**os.environ, "HEALTH_URL": "", "EXPECTED_VERSION": ""}
        proc = subprocess.run(["bash", str(PROBE_SH)], env=env, capture_output=True, text=True, timeout=30, check=False)
        assert proc.returncode == 1, _log(proc)
        assert "HEALTH_URL and EXPECTED_VERSION are both required" in proc.stdout, _log(proc)

    def test_a_streak_longer_than_the_window_is_rejected_as_misconfiguration(self) -> None:
        proc, fake = run_probe(["ok"], samples=3, need_streak=5)
        assert proc.returncode == 1, _log(proc)
        assert any("could ever satisfy it" in e for e in _errors(proc)), _log(proc)
        assert fake.request_count == 0

    def test_script_is_syntactically_valid_bash(self) -> None:
        proc = subprocess.run(["bash", "-n", str(PROBE_SH)], capture_output=True, text=True, check=False)
        assert proc.returncode == 0, proc.stderr


def _probe_step() -> dict:
    workflow = yaml.safe_load(DEPLOY_YML.read_text(encoding="utf-8"))
    steps = [step for job in workflow["jobs"].values() for step in job.get("steps", [])]
    matches = [step for step in steps if step.get("name") == PROBE_STEP_NAME]
    assert len(matches) == 1, f"expected exactly one {PROBE_STEP_NAME!r} step, found {len(matches)}"
    return matches[0]


class TestWorkflowWiring:
    """deploy.yml and the script cannot drift: the step must call the script."""

    def test_the_step_invokes_the_script_under_test(self) -> None:
        run = _probe_step()["run"]
        assert ".github/scripts/post_rollout_probe.sh" in run, run
        assert PROBE_SH.exists()
        assert os.access(PROBE_SH, os.X_OK), "the script is committed executable"

    def test_the_step_does_not_reimplement_the_probe_inline(self) -> None:
        """A second inline curl loop would be a copy this suite cannot test."""
        code = [line for line in _probe_step()["run"].splitlines() if not line.strip().startswith("#")]
        assert not any("curl" in line for line in code), (
            "the probe body belongs in the script, not back in the step: " + "\n".join(code)
        )

    def test_the_step_keeps_set_plus_e_semantics(self) -> None:
        """#1762: `run:` is `bash -e`; a curl assignment's status is curl's."""
        run = _probe_step()["run"]
        assert re.search(r"^\s*set \+e -uo pipefail\s*$", run, re.M), run
        script = PROBE_SH.read_text(encoding="utf-8")
        assert re.search(r"^set \+e -uo pipefail$", script, re.M), "the script sets it for itself too"
        assert not re.search(r"^set -e", script, re.M), "-e would abort on the first timed-out sample"

    def test_the_step_passes_the_url_and_the_expected_commit(self) -> None:
        env = _probe_step()["env"]
        assert env["HEALTH_URL"] == "https://archimedes-arc.com/api/health", (
            "/api/* is on CloudFront's CachingDisabled policy — every sample must be an origin round trip"
        )
        assert env["EXPECTED_VERSION"] == "${{ github.sha }}"

    def test_the_step_still_runs_only_on_a_completed_rollout(self) -> None:
        assert _probe_step()["if"] == "steps.rollout.outputs.rollout == 'completed'"

    def test_script_defaults_are_the_numbers_the_workflow_documents(self) -> None:
        """The step's comment names these; nothing else sets them in CI."""
        script = PROBE_SH.read_text(encoding="utf-8")
        expected = {
            "SAMPLES": "10",
            "NEED_STREAK": "5",
            "MAX_NOT_OK": "2",
            "MAX_SECONDS": "5",
            "INTERVAL": "3",
            "TRANSPORT_RETRIES": "2",
            "RETRY_PAUSE": "3",
            "CURL_MAX_TIME": "15",
            "MAX_TOTAL_SECONDS": "300",
        }
        for name, value in expected.items():
            assert re.search(rf'^{name}="\$\{{{name}:-{value}\}}"$', script, re.M), f"{name} default drifted"
        run = _probe_step()["run"]
        assert "10 / 5 / 5s / 3s / 2 / 3s" in run, "the step's comment must quote the script's live defaults"
        assert "MAX_NOT_OK=2" in run, "the step's comment must name the NOT-OK cap too"

    def test_the_probe_fits_inside_the_deploy_job_timeout(self) -> None:
        """MAX_TOTAL_SECONDS + the rollout budget must leave the job room.

        Retrying transport flakes widens the probe's worst case; the job
        timeout must not silently become the real budget (#1532). This holds
        as stated only because MAX_TOTAL_SECONDS is a REAL ceiling — no
        request or sleep starts that reaches past it (#1791 review, defect 2).
        """
        workflow_text = DEPLOY_YML.read_text(encoding="utf-8")
        rollout_budget_s = int(re.search(r"DEPLOY_ROLLOUT_BUDGET_SECONDS:\s*(\d+)", workflow_text).group(1))
        job_timeout_s = int(re.search(r"DEPLOY_JOB_TIMEOUT_MINUTES:\s*(\d+)", workflow_text).group(1)) * 60
        probe_ceiling_s = int(
            re.search(r'MAX_TOTAL_SECONDS="\$\{MAX_TOTAL_SECONDS:-(\d+)\}"', PROBE_SH.read_text()).group(1)
        )
        assert rollout_budget_s + probe_ceiling_s < job_timeout_s, (
            f"rollout budget {rollout_budget_s}s + probe ceiling {probe_ceiling_s}s does not fit in the "
            f"deploy-ecs job's {job_timeout_s}s timeout, leaving nothing for the CloudFront invalidation"
        )

    def test_the_probe_ceiling_fits_the_reserve_the_poll_step_holds_back(self) -> None:
        """The poll step refuses a rollout budget that eats the tail reserve.

        That reserve is for the probe AND the CloudFront invalidation wait, so
        the probe's ceiling has to be strictly smaller than all of it.
        """
        workflow_text = DEPLOY_YML.read_text(encoding="utf-8")
        reserve_s = int(re.search(r"JOB_TIMEOUT_SECONDS - (\d+)", workflow_text).group(1))
        probe_ceiling_s = int(
            re.search(r'MAX_TOTAL_SECONDS="\$\{MAX_TOTAL_SECONDS:-(\d+)\}"', PROBE_SH.read_text()).group(1)
        )
        assert probe_ceiling_s < reserve_s, (
            f"the probe's {probe_ceiling_s}s ceiling consumes the whole {reserve_s}s the poll step reserves "
            "for the probe plus the CloudFront invalidation wait"
        )


if __name__ == "__main__":  # pragma: no cover - convenience
    raise SystemExit(pytest.main([__file__, "-v"]))
