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
never mounts, mounts and then throws, crashes when a lazy chunk or an API
response arrives after the first paint, shows the ErrorBoundary crash card,
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
    # The app paints, then an API response arrives (slower than the tests'
    # SETTLE_MS, after the last script) and rendering it crashes: once over
    # fetch, once over XHR.
    "/api-response-crashes-fetch": (
        200,
        f'<div id="root"></div><script type="module">{_MOUNT} '
        f"fetch('/slow-api').then((r) => r.json()).then(() => {{ {_SHOW_CRASH_CARD} }});</script>",
    ),
    "/api-response-crashes-xhr": (
        200,
        f'<div id="root"></div><script type="module">{_MOUNT} '
        "const xhr = new XMLHttpRequest(); xhr.open('GET', '/slow-api'); "
        f"xhr.onload = () => {{ {_SHOW_CRASH_CARD} }}; xhr.send();</script>",
    ),
    # The healthy twin: the same slow response, rendered.
    "/api-response-renders": (
        200,
        '<div id="root"></div><script type="module">'
        "document.getElementById('root').innerHTML = '<main>Loading strategies...</main>';"
        "fetch('/slow-api').then((r) => r.json()).then((d) => { document.getElementById('root').innerHTML = "
        "'<main><h1>Strategies</h1><p>' + d.rows.join(', ') + '</p></main>'; });</script>",
    ),
    # A request that never answers (a long poll, a stuck API call).
    "/api-never-answers": (
        200,
        f'<div id="root"></div><script type="module">{_MOUNT} fetch(\'/hanging-api\');</script>',
    ),
}

_ERROR_PAGE = "<h1>502 Bad Gateway</h1><p>CloudFront could not reach the origin.</p>"
_SLOW_API_ROWS = ["alpha", "beta", "gamma"]
_hits: Counter[str] = Counter()
_hits_lock = threading.Lock()
# /hanging-api answers only when the server shuts down.
_release_hanging_requests = threading.Event()


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
        elif url.path == "/slow-api":
            time.sleep(1.5)  # longer than the tests' SETTLE_MS of 0.8s
            self._send(200, json.dumps({"rows": _SLOW_API_ROWS}), "application/json")
        elif url.path == "/hanging-api":
            _release_hanging_requests.wait(timeout=60)
            try:
                self._send(200, "{}", "application/json")
            except OSError:
                pass  # the browser context that asked is long gone
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
        _release_hanging_requests.set()
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
class TestSettleWaitsForRequests:
    """The verdict waits for scripts AND fetch/XHR, up to MOUNT_MS after the first paint."""

    @pytest.mark.parametrize("via", ["fetch", "xhr"])
    def test_a_crash_on_an_api_response_after_the_first_paint_is_red(self, base_url: str, via: str) -> None:
        """The response lands 1.5s after the last script, past SETTLE_MS (0.8s)."""
        proc = run_check(f"{base_url}/api-response-crashes-{via}")
        assert proc.returncode == 1, _log(proc)
        assert any("error-boundary fallback is showing" in e for e in _errors(proc)), _log(proc)

    def test_a_slow_api_response_that_renders_is_green_and_judged_after_it_lands(self, base_url: str) -> None:
        proc = run_check(f"{base_url}/api-response-renders")
        assert proc.returncode == 0, _log(proc)
        landed = len("Strategies") + len(", ".join(_SLOW_API_ROWS))
        assert f"#root has 1 child element(s), {landed} chars of text" in proc.stdout, _log(proc)

    def test_a_request_that_never_answers_is_judged_mount_ms_after_the_first_paint(self, base_url: str) -> None:
        """The wait is bounded: a long poll cannot hold the verdict past MOUNT_MS."""
        started = time.monotonic()
        proc = run_check(f"{base_url}/api-never-answers", RENDER_CHECK_MOUNT_MS="2500")
        elapsed = time.monotonic() - started
        assert proc.returncode == 0, _log(proc)
        assert not _release_hanging_requests.is_set()
        assert elapsed < 2.5 + 7, f"took {elapsed:.1f}s with MOUNT_MS=2.5s{_log(proc)}"


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
# The exact `env:` each render step may carry. A setting weakens the gate
# without touching the run line (RENDER_CHECK_SETTLE_MS: "0" judges a page
# before a lazy chunk or an API response can crash it; a 60s retry delay
# leaves no room in the ceiling for the retries), so keys AND values are pinned.
PRE_PUSH_ENV: dict[str, str] = {}
POST_ROLLOUT_ENV = {"RENDER_CHECK_ATTEMPTS": "3"}
# Every setting the script reads with a numeric default.
RENDER_CHECK_SETTINGS = (
    "RENDER_CHECK_MAX_SECONDS",
    "RENDER_CHECK_ATTEMPTS",
    "RENDER_CHECK_RETRY_DELAY_MS",
    "RENDER_CHECK_NAV_MS",
    "RENDER_CHECK_MOUNT_MS",
    "RENDER_CHECK_SETTLE_MS",
)


def _workflow(text: str | None = None) -> dict:
    return yaml.safe_load(text if text is not None else DEPLOY_YML.read_text(encoding="utf-8"))


def _steps(job: str, text: str | None = None) -> list[dict]:
    return _workflow(text)["jobs"][job]["steps"]


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


def gate_problems(step: dict, expected_run: str, expected_env: dict[str, str]) -> list[str]:
    """Every way a render-check step can stop failing the job on a blank page."""
    problems: list[str] = []
    run = str(step.get("run", ""))
    if _SWALLOWS_EXIT_CODE.search(run):
        problems.append(f"run line can swallow the check's exit code: {run!r}")
    if run.strip() != expected_run:
        problems.append(f"run is {run.strip()!r}, expected exactly {expected_run!r}")
    env = {str(key): str(value) for key, value in (step.get("env") or {}).items()}
    if env != expected_env:
        problems.append(f"env is {env!r}, expected exactly {expected_env!r}")
    if step.get("continue-on-error") not in (None, False):
        problems.append(f"continue-on-error: {step['continue-on-error']!r} makes a red check a green step")
    if "shell" in step:
        problems.append(f"a custom shell ({step['shell']!r}) decides what the exit code means")
    return problems


def _script_default(name: str) -> int:
    match = re.search(rf"process\.env\.{name} \?\? (\d+)\)", SCRIPT.read_text(encoding="utf-8"))
    assert match, f"the script's default for {name} moved"
    return int(match.group(1))


def effective_settings(job: str, step: dict, text: str | None = None) -> dict[str, float]:
    """What the script runs with in this step: its defaults, then workflow, job and step env."""
    workflow = _workflow(text)
    settings = {name: float(_script_default(name)) for name in RENDER_CHECK_SETTINGS}
    for env in (workflow.get("env"), workflow["jobs"][job].get("env"), step.get("env")):
        for name, value in (env or {}).items():
            if name in settings:
                settings[name] = float(value)
    return settings


def tail_reserve_problems(text: str | None = None) -> list[str]:
    """What the steps after the rollout poll can take, against the reserve it holds back.

    After the poll: the answer probe, the CloudFront invalidation, Set up Node
    22 and the render smoke. The probe and the render check each hard-cap
    their own wall clock and Set up Node 22 has a step timeout; what is left
    of the reserve is the invalidation wait's.
    """
    workflow_text = text if text is not None else DEPLOY_YML.read_text(encoding="utf-8")
    reserve_s = int(re.search(r"JOB_TIMEOUT_SECONDS - (\d+)", workflow_text).group(1))
    probe_ceiling_s = int(
        re.search(r'MAX_TOTAL_SECONDS="\$\{MAX_TOTAL_SECONDS:-(\d+)\}"', PROBE_SH.read_text()).group(1)
    )
    steps, smoke = _gate_step("deploy-ecs", "https://archimedes-arc.com/", _steps("deploy-ecs", workflow_text))
    settings = effective_settings("deploy-ecs", steps[smoke], workflow_text)
    problems: list[str] = []
    # The render step's worst case is its RENDER_CHECK_MAX_SECONDS: the script
    # runs every URL, attempt and retry delay inside that one wall clock
    # (TestRetries pins it behaviourally). Retries that cannot fit in it are
    # fiction: every URL x (an instant 5xx + the delay) must.
    render_worst_case_s = settings["RENDER_CHECK_MAX_SECONDS"]
    urls = len(str(steps[smoke]["run"]).split()) - 2
    retry_delays_s = urls * (settings["RENDER_CHECK_ATTEMPTS"] - 1) * settings["RENDER_CHECK_RETRY_DELAY_MS"] / 1000
    if retry_delays_s >= render_worst_case_s:
        problems.append(f"{retry_delays_s:g}s of retry delays do not fit in the {render_worst_case_s:g}s ceiling")
    setup_node = steps[smoke - 1]
    if not _setup_node_22(setup_node) or "timeout-minutes" not in setup_node:
        problems.append("the step before the smoke must be Set up Node 22 with a timeout-minutes")
        return problems
    setup_node_worst_case_s = setup_node["timeout-minutes"] * 60
    tail_s = probe_ceiling_s + setup_node_worst_case_s + render_worst_case_s
    if tail_s >= reserve_s:
        problems.append(
            f"probe {probe_ceiling_s}s + Set up Node 22 {setup_node_worst_case_s}s + render check "
            f"{render_worst_case_s:g}s = {tail_s:g}s leaves nothing of the {reserve_s}s reserve"
        )
    return problems


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
        assert gate_problems(steps[gate], PRE_PUSH_RUN, PRE_PUSH_ENV) == []

    def test_the_post_rollout_smoke_loads_both_routes_of_prod_after_the_cache_purge(self) -> None:
        steps, smoke = _gate_step("deploy-ecs", "https://archimedes-arc.com/")
        invalidate = _index(steps, _named("Invalidate CloudFront (unhashed SPA entry points only)"))
        assert smoke > invalidate, "before the invalidation it would render the edge's cached index.html"
        assert steps[smoke]["if"] == steps[invalidate]["if"], "runs exactly when the new content is live"
        assert _setup_node_22(steps[smoke - 1])
        assert steps[smoke - 1]["if"] == steps[smoke]["if"]
        assert gate_problems(steps[smoke], POST_ROLLOUT_RUN, POST_ROLLOUT_ENV) == []

    def test_nothing_else_sets_a_render_check_setting(self) -> None:
        """Workflow env, job env, or another step (an export, a write to $GITHUB_ENV)."""
        workflow = _workflow()
        found = [f"workflow env: {k}" for k in workflow.get("env") or {} if k.startswith("RENDER_CHECK_")]
        for job_name, job in workflow["jobs"].items():
            found += [f"{job_name} env: {k}" for k in job.get("env") or {} if k.startswith("RENDER_CHECK_")]
            for step in job.get("steps", []):
                if not _runs_the_script(step) and "RENDER_CHECK_" in f"{step.get('run', '')} {step.get('env', '')}":
                    found.append(f"{job_name} step {step.get('name', step.get('uses'))!r}")
        assert found == []

    def test_only_the_post_rollout_smoke_retries(self) -> None:
        """Through CloudFront a 5xx can be a blip; from nginx-validate it is the image."""
        steps, smoke = _gate_step("deploy-ecs", "https://archimedes-arc.com/")
        assert effective_settings("deploy-ecs", steps[smoke])["RENDER_CHECK_ATTEMPTS"] == 3
        steps, gate = _gate_step("build-and-push", "http://localhost:18080/")
        assert effective_settings("build-and-push", steps[gate])["RENDER_CHECK_ATTEMPTS"] == 1

    def test_step_timeouts_are_only_a_backstop_for_the_scripts_own_ceiling(self) -> None:
        for job, url in (("build-and-push", "http://localhost:18080/"), ("deploy-ecs", "https://archimedes-arc.com/")):
            steps, i = _gate_step(job, url)
            ceiling_s = effective_settings(job, steps[i])["RENDER_CHECK_MAX_SECONDS"]
            assert steps[i]["timeout-minutes"] * 60 > ceiling_s, (job, steps[i]["timeout-minutes"])

    def test_probe_and_render_ceilings_fit_the_tail_reserve(self) -> None:
        """The poll step holds back a reserve for everything after the rollout."""
        assert tail_reserve_problems() == []

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
    """gate_problems and tail_reserve_problems, fed the ways a gate gets quietly weakened."""

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
        assert gate_problems({**steps[gate], **mutation}, PRE_PUSH_RUN, PRE_PUSH_ENV), mutation

    def test_the_real_pre_push_text_with_or_true_appended_is_rejected(self) -> None:
        text = DEPLOY_YML.read_text(encoding="utf-8").replace(
            f"        run: {PRE_PUSH_RUN}\n", f"        run: {PRE_PUSH_RUN} || true\n"
        )
        assert text != DEPLOY_YML.read_text(encoding="utf-8"), "the pre-push run line moved; update this test"
        steps, gate = _gate_step("build-and-push", "http://localhost:18080/", _steps("build-and-push", text))
        assert gate_problems(steps[gate], PRE_PUSH_RUN, PRE_PUSH_ENV)

    @pytest.mark.parametrize(
        ("job", "url", "expected_run", "expected_env", "env"),
        [
            ("build-and-push", "http://localhost:18080/", PRE_PUSH_RUN, PRE_PUSH_ENV, {"RENDER_CHECK_SETTLE_MS": "0"}),
            ("build-and-push", "http://localhost:18080/", PRE_PUSH_RUN, PRE_PUSH_ENV, {"RENDER_CHECK_ATTEMPTS": "3"}),
            (
                "deploy-ecs",
                "https://archimedes-arc.com/",
                POST_ROLLOUT_RUN,
                POST_ROLLOUT_ENV,
                {**POST_ROLLOUT_ENV, "RENDER_CHECK_SETTLE_MS": "0"},
            ),
            (
                "deploy-ecs",
                "https://archimedes-arc.com/",
                POST_ROLLOUT_RUN,
                POST_ROLLOUT_ENV,
                {**POST_ROLLOUT_ENV, "RENDER_CHECK_RETRY_DELAY_MS": "60000"},
            ),
            ("deploy-ecs", "https://archimedes-arc.com/", POST_ROLLOUT_RUN, POST_ROLLOUT_ENV, {}),
        ],
        ids=["pre-push-settle-0", "pre-push-retries", "post-rollout-settle-0", "post-rollout-60s-delay", "no-retries"],
    )
    def test_any_env_but_the_pinned_one_is_named(
        self, job: str, url: str, expected_run: str, expected_env: dict[str, str], env: dict[str, str]
    ) -> None:
        steps, i = _gate_step(job, url)
        assert gate_problems({**steps[i], "env": env}, expected_run, expected_env), env

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            # A retry delay the ceiling cannot hold: 2 URLs x 2 retries x 60s.
            (
                '          RENDER_CHECK_ATTEMPTS: "3"\n',
                '          RENDER_CHECK_ATTEMPTS: "3"\n          RENDER_CHECK_RETRY_DELAY_MS: "60000"\n',
            ),
            # A ceiling the reserve cannot hold.
            (
                '          RENDER_CHECK_ATTEMPTS: "3"\n',
                '          RENDER_CHECK_ATTEMPTS: "3"\n          RENDER_CHECK_MAX_SECONDS: "200"\n',
            ),
            # The same, set for the whole workflow instead of the step.
            (
                "  DEPLOY_ROLLOUT_BUDGET_SECONDS: 1200\n",
                '  DEPLOY_ROLLOUT_BUDGET_SECONDS: 1200\n  RENDER_CHECK_MAX_SECONDS: "200"\n',
            ),
            # Set up Node 22 unbounded, or bounded past what the reserve holds.
            (
                "        uses: actions/setup-node@820762786026740c76f36085b0efc47a31fe5020  # v7.0.0\n"
                "        timeout-minutes: 1\n",
                "        uses: actions/setup-node@820762786026740c76f36085b0efc47a31fe5020  # v7.0.0\n",
            ),
            (
                "        uses: actions/setup-node@820762786026740c76f36085b0efc47a31fe5020  # v7.0.0\n"
                "        timeout-minutes: 1\n",
                "        uses: actions/setup-node@820762786026740c76f36085b0efc47a31fe5020  # v7.0.0\n"
                "        timeout-minutes: 3\n",
            ),
        ],
        ids=[
            "retry-delay-60s",
            "step-ceiling-200s",
            "workflow-ceiling-200s",
            "setup-node-unbounded",
            "setup-node-3-min",
        ],
    )
    def test_the_real_tail_with_a_ceiling_it_cannot_hold_is_rejected(self, old: str, new: str) -> None:
        real = DEPLOY_YML.read_text(encoding="utf-8")
        assert real.count(old) == 1, f"{old!r} moved; update this test"
        assert tail_reserve_problems(real.replace(old, new)), new
