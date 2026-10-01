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
never mounts, mounts and then throws, or answers 5xx is red; a page that
mounts, even after ``load``, is green. They need Node >= 22 (global
WebSocket) and a Chrome/Chromium binary, and skip without them; the workflow
wiring tests at the bottom always run.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / ".github" / "scripts" / "spa_render_check.mjs"
DEPLOY_YML = REPO_ROOT / ".github" / "workflows" / "deploy.yml"
PROBE_SH = REPO_ROOT / ".github" / "scripts" / "post_rollout_probe.sh"

_MOUNT = "document.getElementById('root').innerHTML = '<main><h1>Archimedes</h1></main>';"

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
}


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


class _Pages(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        status, body = PAGES.get(self.path, (404, "not found"))
        data = f"<!doctype html><html><head><title>t</title></head><body>{body}</body></html>".encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: object) -> None:
        pass


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
    full_env = {**os.environ, "RENDER_CHECK_MOUNT_MS": "4000", "RENDER_CHECK_SETTLE_MS": "800", **env}
    if CHROME:
        full_env["CHROME_PATH"] = CHROME
    return subprocess.run(
        [NODE or "node", str(SCRIPT), *args], env=full_env, capture_output=True, text=True, timeout=90, check=False
    )


def _errors(proc: subprocess.CompletedProcess[str]) -> list[str]:
    return [line for line in proc.stdout.splitlines() if line.startswith("::error::")]


def _log(proc: subprocess.CompletedProcess[str]) -> str:
    return f"\n--- exit {proc.returncode} ---\n{proc.stdout}{proc.stderr}"


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


@pytest.mark.skipif(NODE is None, reason="needs Node >= 22")
class TestUsage:
    @pytest.mark.parametrize("args", [(), ("not-a-url",), ("ftp://example.com/",), ("http://a/", "http://b/")])
    def test_bad_arguments_exit_2_without_launching_chrome(self, args: tuple[str, ...]) -> None:
        proc = run_check(*args, CHROME_PATH="/nonexistent/chrome")
        assert proc.returncode == 2, _log(proc)
        assert "Chrome" not in proc.stdout, _log(proc)


def _steps(job: str) -> list[dict]:
    workflow = yaml.safe_load(DEPLOY_YML.read_text(encoding="utf-8"))
    return workflow["jobs"][job]["steps"]


def _index(steps: list[dict], predicate) -> int:
    matches = [i for i, step in enumerate(steps) if predicate(step)]
    assert len(matches) == 1, f"expected exactly one matching step, found {len(matches)}"
    return matches[0]


def _invokes(url: str):
    return lambda step: f".github/scripts/spa_render_check.mjs {url}" in step.get("run", "")


def _named(name: str):
    return lambda step: step.get("name") == name


def _setup_node_22(step: dict) -> bool:
    return (
        str(step.get("uses", "")).startswith("actions/setup-node@")
        and str(step.get("with", {}).get("node-version")) == "22"
    )


def _script_ceiling_s() -> int:
    match = re.search(r"RENDER_CHECK_MAX_SECONDS \?\? (\d+)\)", SCRIPT.read_text(encoding="utf-8"))
    assert match, "the script's default wall-clock ceiling moved"
    return int(match.group(1))


class TestWorkflowWiring:
    """deploy.yml runs the script under test, in the right place, both times."""

    def test_the_script_is_committed_executable(self) -> None:
        assert os.access(SCRIPT, os.X_OK)

    def test_the_pre_push_gate_renders_the_validated_image_before_anything_is_pushed(self) -> None:
        steps = _steps("build-and-push")
        gate = _index(steps, _invokes("http://localhost:18080/"))
        assert gate > _index(steps, _named("Validate nginx image boots + proxies (GET / and /health)"))
        assert gate < _index(steps, _named("Teardown validation containers")), "nginx-validate must still be up"
        pushes = [i for i, step in enumerate(steps) if str(step.get("name", "")).startswith("Tag + push")]
        assert pushes and gate < min(pushes), "a blank SPA must never reach ECR"
        assert _setup_node_22(steps[gate - 1]), "the script needs Node >= 22 for its global WebSocket"
        assert "if" not in steps[gate], "the gate must not be skippable"

    def test_the_post_rollout_smoke_loads_prod_after_the_cache_purge(self) -> None:
        steps = _steps("deploy-ecs")
        smoke = _index(steps, _invokes("https://archimedes-arc.com/"))
        invalidate = _index(steps, _named("Invalidate CloudFront (unhashed SPA entry points only)"))
        assert smoke > invalidate, "before the invalidation it would render the edge's cached index.html"
        assert steps[smoke]["if"] == steps[invalidate]["if"], "runs exactly when the new content is live"
        assert _setup_node_22(steps[smoke - 1])
        assert steps[smoke - 1]["if"] == steps[smoke]["if"]
        assert not steps[smoke].get("continue-on-error"), "a blank prod must be a red run"

    def test_step_timeouts_are_only_a_backstop_for_the_scripts_own_ceiling(self) -> None:
        for job, url in (("build-and-push", "http://localhost:18080/"), ("deploy-ecs", "https://archimedes-arc.com/")):
            steps = _steps(job)
            step = steps[_index(steps, _invokes(url))]
            assert step["timeout-minutes"] * 60 > _script_ceiling_s(), (job, step["timeout-minutes"])

    def test_probe_and_render_ceilings_fit_the_tail_reserve(self) -> None:
        """The poll step holds back a reserve for everything after the rollout."""
        workflow_text = DEPLOY_YML.read_text(encoding="utf-8")
        reserve_s = int(re.search(r"JOB_TIMEOUT_SECONDS - (\d+)", workflow_text).group(1))
        probe_ceiling_s = int(
            re.search(r'MAX_TOTAL_SECONDS="\$\{MAX_TOTAL_SECONDS:-(\d+)\}"', PROBE_SH.read_text()).group(1)
        )
        assert probe_ceiling_s + _script_ceiling_s() < reserve_s
