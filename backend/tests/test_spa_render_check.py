"""The SPA render check, run against scripted pages in a real headless Chrome.

``.github/scripts/spa_render_check.mjs`` is the body of two deploy.yml steps:
the pre-push "Render the SPA in headless Chrome (blank-page gate)" in
build-and-push and the post-rollout smoke against https://archimedes-arc.com/
in deploy-ecs. It exists because from 2026-09-25 to 2026-10-01 prod served a
blank page (React error #527: react 19.3.0 with react-dom 19.2.8, #1872) while
every deploy check stayed green: a curl of / returns a perfectly valid
index.html whether or not the bundle can start.

These tests run THE EXACT SCRIPT CI RUNS against a local HTTP server, so the
property under test is fail-closed rendering: a page that throws at startup,
never mounts, mounts and then throws, shows the ErrorBoundary crash card,
logs a React render error, or answers 4xx/5xx is red; a page that mounts,
even after ``load``, is green. Retries are pinned from both sides: a
transport-only failure is retried into green, an app failure never is, and
every attempt runs inside the one wall-clock ceiling. They need Node >= 22
(global WebSocket) and a Chrome/Chromium binary, and skip without them; the
workflow wiring tests at the bottom always run.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / ".github" / "scripts" / "spa_render_check.mjs"
DEPLOY_YML = REPO_ROOT / ".github" / "workflows" / "deploy.yml"
PROBE_SH = REPO_ROOT / ".github" / "scripts" / "post_rollout_probe.sh"
ERROR_BOUNDARY_JSX = REPO_ROOT / "ui" / "src" / "components" / "ErrorBoundary.jsx"

_MOUNT = "document.getElementById('root').innerHTML = '<main><h1>Archimedes</h1></main>';"
# What ui/src/components/ErrorBoundary.jsx renders into #root after a render
# error, reduced to the parts the check can see.
_CRASH_CARD = (
    '<div class="card error" role="alert" data-error-boundary="crashed">'
    "<h2>This page crashed</h2><p>Cannot read properties of undefined (reading 'map')</p>"
    '<button type="button">Reload</button></div>'
)
_SHOW_CRASH_CARD = f"document.getElementById('root').innerHTML = {json.dumps(_CRASH_CARD)};"
# React 19 production builds report a render error a boundary caught with a
# bare console.error(error); ErrorBoundary.jsx's componentDidCatch adds this.
_BOUNDARY_LOG = (
    "console.error('[ErrorBoundary]', new Error(\"Cannot read properties of undefined (reading 'map')\"), "
    "'\\n    at StrategyList');"
)

PAGES: dict[str, tuple[int, str]] = {
    "/ok": (200, f'<div id="root"></div><script type="module">{_MOUNT}</script>'),
    # The incident, reduced: the bundle throws at module evaluation, before
    # React gets to render anything.
    "/react-527": (
        200,
        '<div id="root"></div><script type="module">'
        'throw new Error("Minified React error #527; visit '
        'https://react.dev/errors/527?args[]=19.3.0&args[]=19.2.8");</script>',
    ),
    "/never-mounts": (200, '<div id="root"></div>'),
    "/mounts-after-load": (
        200,
        f'<div id="root"></div><script>addEventListener("load", () => setTimeout(() => {{ {_MOUNT} }}, 1200));</script>',
    ),
    "/throws-after-mount": (
        200,
        f'<div id="root"></div><script type="module">{_MOUNT} '
        'setTimeout(() => { throw new Error("late startup failure"); }, 300);</script>',
    ),
    "/unhandled-rejection": (
        200,
        f'<div id="root"></div><script type="module">{_MOUNT} Promise.reject(new Error("boot fetch exploded"));</script>',
    ),
    "/server-error": (500, f'<div id="root"></div><script type="module">{_MOUNT}</script>'),
    "/not-found": (404, f'<div id="root"></div><script type="module">{_MOUNT}</script>'),
    # A render error the root ErrorBoundary caught, exactly as a production
    # build shows it: the crash card fills #root and the boundary logs.
    "/crash-card": (200, f'<div id="root"></div><script type="module">{_SHOW_CRASH_CARD} {_BOUNDARY_LOG}</script>'),
    # Each detector alone, so neither is load-bearing for the other.
    "/crash-card-without-log": (200, f'<div id="root"></div><script type="module">{_SHOW_CRASH_CARD}</script>'),
    "/render-error-logged": (200, f'<div id="root"></div><script type="module">{_MOUNT} {_BOUNDARY_LOG}</script>'),
    "/react-dev-caught-error": (
        200,
        f'<div id="root"></div><script type="module">{_MOUNT} '
        'console.error("%o\\n\\nThe above error occurred in the <StrategyList> component.", new Error("x"));</script>',
    ),
    # Not every console.error is a render error: a failed API call on a page
    # that rendered is the API's problem, not a blank page.
    "/unrelated-console-error": (
        200,
        f'<div id="root"></div><script type="module">{_MOUNT} '
        'console.error("GET /api/strategies failed", new Error("HTTP 503"));</script>',
    ),
    # /app, reduced: the shell paints a Suspense fallback, then the lazy
    # AuthenticatedApp chunk arrives (slowly, from the edge) and crashes.
    "/lazy-chunk-crashes": (
        200,
        '<div id="root"></div><script type="module">'
        "document.getElementById('root').innerHTML = '<main>Loading application...</main>';"
        "import('/slow-chunk.js').then((m) => m.render());</script>",
    ),
    "/blank-missing-script": (200, '<div id="root"></div><script type="module" src="/no-such-chunk.js"></script>'),
}

_ERROR_PAGE = "<h1>502 Bad Gateway</h1><p>CloudFront could not reach the origin.</p>"
_hits: Counter[str] = Counter()
_hits_lock = threading.Lock()


def _hit(key: str) -> int:
    with _hits_lock:
        _hits[key] += 1
        return _hits[key]


class _Pages(BaseHTTPRequestHandler):
    def _send(self, status: int, body: str, content_type: str = "text/html; charset=utf-8") -> None:
        if content_type.startswith("text/html"):
            body = f"<!doctype html><html><head><title>t</title></head><body>{body}</body></html>"
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/slow-chunk.js":
            time.sleep(1.5)  # longer than the tests' SETTLE_MS of 0.8s
            self._send(200, f"export function render() {{ {_SHOW_CRASH_CARD} }}", "text/javascript")
        elif url.path == "/flaky":
            # The first `fail` loads of this id fail in `mode`, later loads
            # are a healthy page: a transient edge fault, or a flaky app.
            failing = _hit(f"doc:{query['id']}") <= int(query["fail"])
            mode = query["mode"]
            if not failing:
                self._send(200, PAGES["/ok"][1])
            elif mode == "edge-503":
                self._send(503, _ERROR_PAGE)
            elif mode == "503-and-throws":
                self._send(503, '<div id="root"></div><script type="module">throw new Error("boot failed");</script>')
            elif mode == "crash-card":
                self._send(200, PAGES["/crash-card"][1])
            else:
                raise AssertionError(mode)
        elif url.path == "/flaky-script-page":
            src = f"/flaky-chunk.js?id={query['id']}&fail={query['fail']}"
            self._send(200, f'<div id="root"></div><script type="module" src="{src}"></script>')
        elif url.path == "/flaky-chunk.js":
            if _hit(f"chunk:{query['id']}") <= int(query["fail"]):
                self._send(503, "upstream unavailable", "text/plain")
            else:
                self._send(200, _MOUNT, "text/javascript")
        elif url.path == "/always-503":
            self._send(503, _ERROR_PAGE)
        else:
            status, body = PAGES.get(url.path, (404, "not found"))
            self._send(status, body)

    def log_message(self, *args: object) -> None:
        pass


def _node() -> str | None:
    node = shutil.which("node")
    if node is None:
        return None
    probe = subprocess.run(
        [node, "-e", "process.exit(typeof WebSocket === 'function' ? 0 : 1)"], capture_output=True, check=False
    )
    return node if probe.returncode == 0 else None


def _chrome() -> str | None:
    if os.environ.get("CHROME_PATH"):
        return os.environ["CHROME_PATH"]
    mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if os.path.exists(mac):
        return mac
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        if found := shutil.which(name):
            return found
    return None


NODE = _node()
CHROME = _chrome()
needs_browser = pytest.mark.skipif(
    NODE is None or CHROME is None, reason="needs Node >= 22 (global WebSocket) and Chrome/Chromium"
)


@pytest.fixture(scope="module")
def base_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Pages)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def run_check(*args: str, **env: str) -> subprocess.CompletedProcess[str]:
    full_env = {
        **os.environ,
        "RENDER_CHECK_MOUNT_MS": "4000",
        "RENDER_CHECK_SETTLE_MS": "800",
        "RENDER_CHECK_RETRY_DELAY_MS": "200",
        **env,
    }
    if CHROME and "CHROME_PATH" not in env:
        full_env["CHROME_PATH"] = CHROME
    return subprocess.run(
        [NODE or "node", str(SCRIPT), *args], env=full_env, capture_output=True, text=True, timeout=90, check=False
    )


def _errors(proc: subprocess.CompletedProcess[str]) -> list[str]:
    return [line for line in proc.stdout.splitlines() if line.startswith("::error::")]


def _warnings(proc: subprocess.CompletedProcess[str]) -> list[str]:
    return [line for line in proc.stdout.splitlines() if line.startswith("::warning::")]


def _log(proc: subprocess.CompletedProcess[str]) -> str:
    return f"\n--- exit {proc.returncode} ---\n{proc.stdout}{proc.stderr}"


def _fresh() -> str:
    return uuid.uuid4().hex


@needs_browser
class TestRenderVerdicts:
    def test_a_page_that_mounts_is_green(self, base_url: str) -> None:
        proc = run_check(f"{base_url}/ok")
        assert proc.returncode == 0, _log(proc)
        assert "spa_render_check: OK" in proc.stdout, _log(proc)
        assert not _errors(proc), _log(proc)

    def test_the_incident_is_red_and_names_react_527(self, base_url: str) -> None:
        proc = run_check(f"{base_url}/react-527")
        assert proc.returncode == 1, _log(proc)
        errors = _errors(proc)
        assert any("uncaught exception during startup" in e and "React error #527" in e for e in errors), _log(proc)
        assert any("#root is empty after load" in e for e in errors), _log(proc)

    def test_a_page_that_never_mounts_is_red_without_any_exception(self, base_url: str) -> None:
        proc = run_check(f"{base_url}/never-mounts", RENDER_CHECK_MOUNT_MS="1500")
        assert proc.returncode == 1, _log(proc)
        assert _errors(proc) == [
            f"::error::spa_render_check: {base_url}/never-mounts: #root is empty after load (the app never mounted)"
        ], _log(proc)

    def test_a_mount_after_the_load_event_is_still_green(self, base_url: str) -> None:
        """React mounts after module evaluation; `load` is not the finish line."""
        proc = run_check(f"{base_url}/mounts-after-load")
        assert proc.returncode == 0, _log(proc)

    def test_an_exception_shortly_after_mount_is_red(self, base_url: str) -> None:
        proc = run_check(f"{base_url}/throws-after-mount")
        assert proc.returncode == 1, _log(proc)
        assert any("late startup failure" in e for e in _errors(proc)), _log(proc)

    def test_an_unhandled_rejection_is_red(self, base_url: str) -> None:
        proc = run_check(f"{base_url}/unhandled-rejection")
        assert proc.returncode == 1, _log(proc)
        assert any("boot fetch exploded" in e for e in _errors(proc)), _log(proc)

    def test_a_5xx_document_is_red_even_if_it_renders(self, base_url: str) -> None:
        proc = run_check(f"{base_url}/server-error")
        assert proc.returncode == 1, _log(proc)
        assert any("document answered HTTP 500" in e for e in _errors(proc)), _log(proc)

    def test_a_4xx_document_is_red_even_if_it_renders_and_is_never_retried(self, base_url: str) -> None:
        """A 404 for / or /app is a deploy that lost its entry point, not a blip."""
        proc = run_check(f"{base_url}/not-found", RENDER_CHECK_ATTEMPTS="3")
        assert proc.returncode == 1, _log(proc)
        assert _errors(proc) == [f"::error::spa_render_check: {base_url}/not-found: document answered HTTP 404"], _log(
            proc
        )
        assert not _warnings(proc), _log(proc)


@needs_browser
class TestCrashedApp:
    """A render error the ErrorBoundary caught: #root is full, the app is not."""

    def test_the_crash_card_with_its_log_is_red_twice_over(self, base_url: str) -> None:
        proc = run_check(f"{base_url}/crash-card")
        assert proc.returncode == 1, _log(proc)
        errors = _errors(proc)
        assert any(
            'the error-boundary fallback is showing: "This page crashed / Cannot read properties' in e for e in errors
        ), _log(proc)
        assert any("React logged a render error: [ErrorBoundary]" in e for e in errors), _log(proc)

    def test_the_crash_card_alone_is_red(self, base_url: str) -> None:
        proc = run_check(f"{base_url}/crash-card-without-log")
        assert proc.returncode == 1, _log(proc)
        assert len(_errors(proc)) == 1 and "error-boundary fallback is showing" in _errors(proc)[0], _log(proc)

    @pytest.mark.parametrize(
        ("page", "marker"),
        [("/render-error-logged", "[ErrorBoundary]"), ("/react-dev-caught-error", "The above error occurred in")],
        ids=["boundary-log", "react-dev-log"],
    )
    def test_a_logged_render_error_alone_is_red(self, base_url: str, page: str, marker: str) -> None:
        """A boundary without the marker (a future one, a library's) still logs."""
        proc = run_check(f"{base_url}{page}")
        assert proc.returncode == 1, _log(proc)
        errors = _errors(proc)
        assert len(errors) == 1 and "React logged a render error" in errors[0] and marker in errors[0], _log(proc)

    def test_an_unrelated_console_error_on_a_rendered_page_is_green_but_shown(self, base_url: str) -> None:
        proc = run_check(f"{base_url}/unrelated-console-error")
        assert proc.returncode == 0, _log(proc)
        assert (
            "console.error without a render-error marker (not fatal on its own): GET /api/strategies failed"
            in proc.stdout
        ), _log(proc)

    def test_a_lazy_chunk_that_crashes_after_the_first_paint_is_red(self, base_url: str) -> None:
        """/app paints a Suspense fallback first; the verdict waits for the chunk."""
        proc = run_check(f"{base_url}/lazy-chunk-crashes")
        assert proc.returncode == 1, _log(proc)
        assert any("error-boundary fallback is showing" in e for e in _errors(proc)), _log(proc)


@needs_browser
class TestRetries:
    """Only a transport-level failure is retried, and only inside the ceiling."""

    def test_a_transient_edge_5xx_is_retried_into_green(self, base_url: str) -> None:
        url = f"{base_url}/flaky?id={_fresh()}&fail=1&mode=edge-503"
        proc = run_check(url, RENDER_CHECK_ATTEMPTS="3")
        assert proc.returncode == 0, _log(proc)
        warnings = _warnings(proc)
        assert len(warnings) == 1 and "attempt 1/3 failed at the transport level" in warnings[0], _log(proc)
        assert "document answered HTTP 503" in warnings[0], _log(proc)

    def test_without_attempts_the_same_blip_is_red(self, base_url: str) -> None:
        """The default (the pre-push gate) is one attempt."""
        proc = run_check(f"{base_url}/flaky?id={_fresh()}&fail=1&mode=edge-503")
        assert proc.returncode == 1, _log(proc)
        assert not _warnings(proc), _log(proc)

    def test_a_script_that_5xxs_and_blanks_the_page_is_retried(self, base_url: str) -> None:
        url = f"{base_url}/flaky-script-page?id={_fresh()}&fail=1"
        proc = run_check(url, RENDER_CHECK_ATTEMPTS="3")
        assert proc.returncode == 0, _log(proc)
        warnings = _warnings(proc)
        assert len(warnings) == 1 and "/flaky-chunk.js" in warnings[0] and "HTTP 503" in warnings[0], _log(proc)

    def test_an_edge_that_stays_down_is_red_after_the_last_attempt(self, base_url: str) -> None:
        url = f"{base_url}/flaky?id={_fresh()}&fail=99&mode=edge-503"
        proc = run_check(url, RENDER_CHECK_ATTEMPTS="3")
        assert proc.returncode == 1, _log(proc)
        assert len(_warnings(proc)) == 2, _log(proc)
        assert any("document answered HTTP 503 (attempt 3/3)" in e for e in _errors(proc)), _log(proc)

    def test_a_navigation_error_is_retried(self, base_url: str) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            closed_port = sock.getsockname()[1]
        proc = run_check(f"http://127.0.0.1:{closed_port}/", RENDER_CHECK_ATTEMPTS="2")
        assert proc.returncode == 1, _log(proc)
        assert len(_warnings(proc)) == 1 and "navigation failed: net::ERR_CONNECTION_REFUSED" in proc.stdout, _log(proc)

    @pytest.mark.parametrize(
        ("mode", "symptom"),
        [("503-and-throws", "uncaught exception during startup: Error: boot failed"), ("crash-card", "fallback")],
        ids=["exception", "crash-card"],
    )
    def test_a_js_exception_or_crash_card_is_never_retried(self, base_url: str, mode: str, symptom: str) -> None:
        """The next load would pass; the gate must still be red."""
        proc = run_check(f"{base_url}/flaky?id={_fresh()}&fail=1&mode={mode}", RENDER_CHECK_ATTEMPTS="3")
        assert proc.returncode == 1, _log(proc)
        assert not _warnings(proc), _log(proc)
        assert any(symptom in e for e in _errors(proc)), _log(proc)

    def test_a_missing_script_is_not_retried(self, base_url: str) -> None:
        proc = run_check(f"{base_url}/blank-missing-script", RENDER_CHECK_ATTEMPTS="3", RENDER_CHECK_MOUNT_MS="1500")
        assert proc.returncode == 1, _log(proc)
        assert not _warnings(proc), _log(proc)
        assert any("/no-such-chunk.js failed: HTTP 404" in e for e in _errors(proc)), _log(proc)

    def test_the_ceiling_covers_every_attempt_and_retry_delay(self, base_url: str) -> None:
        """deploy.yml's tail reserve counts RENDER_CHECK_MAX_SECONDS as the worst case."""
        started = time.monotonic()
        proc = run_check(
            f"{base_url}/always-503",
            RENDER_CHECK_ATTEMPTS="3",
            RENDER_CHECK_RETRY_DELAY_MS="20000",
            RENDER_CHECK_MAX_SECONDS="4",
        )
        elapsed = time.monotonic() - started
        assert proc.returncode == 1, _log(proc)
        assert any("did not finish within 4s" in e for e in _errors(proc)), _log(proc)
        assert elapsed < 4 + 3, f"took {elapsed:.1f}s against a 4s ceiling{_log(proc)}"


@needs_browser
class TestSeveralUrls:
    def test_every_url_is_judged_and_one_bad_one_fails_the_run(self, base_url: str) -> None:
        proc = run_check(f"{base_url}/ok", f"{base_url}/crash-card-without-log", f"{base_url}/mounts-after-load")
        assert proc.returncode == 1, _log(proc)
        assert f"spa_render_check: OK {base_url}/ok rendered" in proc.stdout, _log(proc)
        assert f"spa_render_check: OK {base_url}/mounts-after-load rendered" in proc.stdout, _log(proc)
        assert all(f"{base_url}/crash-card-without-log:" in e for e in _errors(proc)), _log(proc)

    def test_all_good_is_green(self, base_url: str) -> None:
        proc = run_check(f"{base_url}/ok", f"{base_url}/mounts-after-load")
        assert proc.returncode == 0, _log(proc)
        assert "spa_render_check: OK, all 2 URL(s) rendered" in proc.stdout, _log(proc)


@pytest.mark.skipif(NODE is None, reason="needs Node >= 22")
class TestUsage:
    @pytest.mark.parametrize(
        ("args", "env"),
        [
            ((), {}),
            (("not-a-url",), {}),
            (("ftp://example.com/",), {}),
            (("http://a/", "not-a-url"), {}),
            (("http://a/",), {"RENDER_CHECK_ATTEMPTS": "0"}),
            (("http://a/",), {"RENDER_CHECK_ATTEMPTS": "1.5"}),
            (("http://a/",), {"RENDER_CHECK_MAX_SECONDS": "-1"}),
        ],
    )
    def test_bad_arguments_exit_2_without_launching_chrome(self, args: tuple[str, ...], env: dict[str, str]) -> None:
        proc = run_check(*args, CHROME_PATH="/nonexistent/chrome", **env)
        assert proc.returncode == 2, _log(proc)
        assert "Chrome" not in proc.stdout, _log(proc)


# ── deploy.yml wiring ────────────────────────────────────────────────────

PRE_PUSH_RUN = "node .github/scripts/spa_render_check.mjs http://localhost:18080/ http://localhost:18080/app"
POST_ROLLOUT_RUN = (
    "node .github/scripts/spa_render_check.mjs https://archimedes-arc.com/ https://archimedes-arc.com/app"
)


def _steps(job: str, text: str | None = None) -> list[dict]:
    workflow = yaml.safe_load(text if text is not None else DEPLOY_YML.read_text(encoding="utf-8"))
    return workflow["jobs"][job]["steps"]


def _index(steps: list[dict], predicate: Callable[[dict], bool]) -> int:
    matches = [i for i, step in enumerate(steps) if predicate(step)]
    assert len(matches) == 1, f"expected exactly one matching step, found {len(matches)}"
    return matches[0]


def _runs_the_script(step: dict) -> bool:
    return ".github/scripts/spa_render_check.mjs" in str(step.get("run", ""))


def _gate_step(job: str, url: str, steps: list[dict] | None = None) -> tuple[list[dict], int]:
    steps = steps if steps is not None else _steps(job)
    return steps, _index(steps, lambda step: _runs_the_script(step) and url in str(step["run"]))


def _named(name: str) -> Callable[[dict], bool]:
    return lambda step: step.get("name") == name


def _setup_node_22(step: dict) -> bool:
    return (
        str(step.get("uses", "")).startswith("actions/setup-node@")
        and str(step.get("with", {}).get("node-version")) == "22"
    )


# `cmd || true`, `cmd || echo ...`, `cmd; exit 0`, `cmd & ...`, `set +e`: any
# of these turns the step's exit code into something other than the check's.
_SWALLOWS_EXIT_CODE = re.compile(r"\|\||;|&|\bexit\s+0\b|\bset\s+\+e\b|\|")


def gate_problems(step: dict, expected_run: str) -> list[str]:
    """Every way a render-check step can stop failing the job on a blank page."""
    problems: list[str] = []
    run = str(step.get("run", ""))
    if _SWALLOWS_EXIT_CODE.search(run):
        problems.append(f"run line can swallow the check's exit code: {run!r}")
    if run.strip() != expected_run:
        problems.append(f"run is {run.strip()!r}, expected exactly {expected_run!r}")
    if step.get("continue-on-error") not in (None, False):
        problems.append(f"continue-on-error: {step['continue-on-error']!r} makes a red check a green step")
    if "shell" in step:
        problems.append(f"a custom shell ({step['shell']!r}) decides what the exit code means")
    return problems


def _script_default(name: str) -> int:
    match = re.search(rf"process\.env\.{name} \?\? (\d+)\)", SCRIPT.read_text(encoding="utf-8"))
    assert match, f"the script's default for {name} moved"
    return int(match.group(1))


def _ceiling_s(step: dict) -> float:
    """The step's real worst case: the script's one ceiling, or its override."""
    return float(step.get("env", {}).get("RENDER_CHECK_MAX_SECONDS", _script_default("RENDER_CHECK_MAX_SECONDS")))


class TestWorkflowWiring:
    """deploy.yml runs the script under test, in the right place, both times."""

    def test_the_script_is_committed_executable(self) -> None:
        assert os.access(SCRIPT, os.X_OK)

    def test_the_pre_push_gate_renders_both_routes_of_the_validated_image_before_anything_is_pushed(self) -> None:
        steps, gate = _gate_step("build-and-push", "http://localhost:18080/")
        assert gate > _index(steps, _named("Validate nginx image boots + proxies (GET / and /health)"))
        assert gate < _index(steps, _named("Teardown validation containers")), "nginx-validate must still be up"
        pushes = [i for i, step in enumerate(steps) if str(step.get("name", "")).startswith("Tag + push")]
        assert pushes and gate < min(pushes), "a blank SPA must never reach ECR"
        assert _setup_node_22(steps[gate - 1]), "the script needs Node >= 22 for its global WebSocket"
        assert "if" not in steps[gate], "the gate must not be skippable"
        assert gate_problems(steps[gate], PRE_PUSH_RUN) == []

    def test_the_post_rollout_smoke_loads_both_routes_of_prod_after_the_cache_purge(self) -> None:
        steps, smoke = _gate_step("deploy-ecs", "https://archimedes-arc.com/")
        invalidate = _index(steps, _named("Invalidate CloudFront (unhashed SPA entry points only)"))
        assert smoke > invalidate, "before the invalidation it would render the edge's cached index.html"
        assert steps[smoke]["if"] == steps[invalidate]["if"], "runs exactly when the new content is live"
        assert _setup_node_22(steps[smoke - 1])
        assert steps[smoke - 1]["if"] == steps[smoke]["if"]
        assert gate_problems(steps[smoke], POST_ROLLOUT_RUN) == []

    def test_only_the_post_rollout_smoke_retries(self) -> None:
        """Through CloudFront a 5xx can be a blip; from nginx-validate it is the image."""
        _, smoke = _gate_step("deploy-ecs", "https://archimedes-arc.com/")
        assert _steps("deploy-ecs")[smoke]["env"]["RENDER_CHECK_ATTEMPTS"] == "3"
        steps, gate = _gate_step("build-and-push", "http://localhost:18080/")
        assert "RENDER_CHECK_ATTEMPTS" not in steps[gate].get("env", {})
        assert _script_default("RENDER_CHECK_ATTEMPTS") == 1

    def test_step_timeouts_are_only_a_backstop_for_the_scripts_own_ceiling(self) -> None:
        for job, url in (("build-and-push", "http://localhost:18080/"), ("deploy-ecs", "https://archimedes-arc.com/")):
            steps, i = _gate_step(job, url)
            assert steps[i]["timeout-minutes"] * 60 > _ceiling_s(steps[i]), (job, steps[i]["timeout-minutes"])

    def test_probe_and_render_ceilings_fit_the_tail_reserve(self) -> None:
        """The poll step holds back a reserve for everything after the rollout.

        The render step's worst case is its RENDER_CHECK_MAX_SECONDS: the
        script runs both URLs, all attempts and every retry delay inside that
        one wall clock (TestRetries pins it behaviourally).
        """
        workflow_text = DEPLOY_YML.read_text(encoding="utf-8")
        reserve_s = int(re.search(r"JOB_TIMEOUT_SECONDS - (\d+)", workflow_text).group(1))
        probe_ceiling_s = int(
            re.search(r'MAX_TOTAL_SECONDS="\$\{MAX_TOTAL_SECONDS:-(\d+)\}"', PROBE_SH.read_text()).group(1)
        )
        steps, smoke = _gate_step("deploy-ecs", "https://archimedes-arc.com/")
        render_worst_case_s = _ceiling_s(steps[smoke])
        attempts = int(steps[smoke]["env"]["RENDER_CHECK_ATTEMPTS"])
        # Even with a ceiling, a run must be ABLE to finish its retries in it,
        # or the retries are fiction: two URLs x (an instant 5xx + the delay).
        assert 2 * (attempts - 1) * _script_default("RENDER_CHECK_RETRY_DELAY_MS") / 1000 < render_worst_case_s
        assert probe_ceiling_s + render_worst_case_s < reserve_s

    def test_the_crash_marker_and_log_prefix_are_the_ones_error_boundary_jsx_emits(self) -> None:
        script = SCRIPT.read_text(encoding="utf-8")
        boundary = ERROR_BOUNDARY_JSX.read_text(encoding="utf-8")
        selector = re.search(r"const CRASH_SELECTOR = '\[(data-[\w-]+)=\"(\w+)\"\]';", script)
        assert selector, "the script's crash-card selector moved"
        assert re.search(
            rf'<div(?=[^>]*\brole="alert")(?=[^>]*\b{selector.group(1)}="{selector.group(2)}")[^>]*>', boundary
        ), "ErrorBoundary.jsx's fallback no longer carries the marker the deploy gate looks for"
        prefix = re.search(r'const LOG_PREFIX = "([^"]+)";', boundary)
        assert prefix and f"'{prefix.group(1)}'," in script, "the boundary's console.error prefix is not a marker"


class TestTheWiringCheckRejects:
    """gate_problems, fed the ways a gate gets quietly made advisory."""

    @pytest.mark.parametrize(
        "mutation",
        [
            {"run": PRE_PUSH_RUN + " || true"},
            {"run": PRE_PUSH_RUN + ' || echo "::warning::render check failed"'},
            {"run": PRE_PUSH_RUN + "; exit 0"},
            {"run": PRE_PUSH_RUN + "\nexit 0\n"},
            {"run": "set +e\n" + PRE_PUSH_RUN},
            {"run": PRE_PUSH_RUN + " | tee render.log"},
            {"run": PRE_PUSH_RUN.replace(" http://localhost:18080/app", "")},
            {"continue-on-error": True},
            {"continue-on-error": "${{ true }}"},
            {"shell": "bash +e {0}"},
        ],
        ids=[
            "or-true",
            "or-echo",
            "semicolon-exit-0",
            "newline-exit-0",
            "set-plus-e",
            "pipe-to-tee",
            "app-dropped",
            "continue-on-error",
            "continue-on-error-expression",
            "custom-shell",
        ],
    )
    def test_each_advisory_shape_is_named(self, mutation: dict) -> None:
        steps, gate = _gate_step("build-and-push", "http://localhost:18080/")
        assert gate_problems({**steps[gate], **mutation}, PRE_PUSH_RUN), mutation

    def test_the_real_pre_push_text_with_or_true_appended_is_rejected(self) -> None:
        text = DEPLOY_YML.read_text(encoding="utf-8").replace(
            f"        run: {PRE_PUSH_RUN}\n", f"        run: {PRE_PUSH_RUN} || true\n"
        )
        assert text != DEPLOY_YML.read_text(encoding="utf-8"), "the pre-push run line moved; update this test"
        steps, gate = _gate_step("build-and-push", "http://localhost:18080/", _steps("build-and-push", text))
        assert gate_problems(steps[gate], PRE_PUSH_RUN)
