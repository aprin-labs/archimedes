"""A cached session's api_url must win over the global default (review finding:
it was write-only — a staging login silently sent its cookie to prod)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
from click.testing import CliRunner

from archimedes_cli import cli as cli_mod
from archimedes_cli.cli import DEFAULT_API_URL, _resolve_api_url, main
from archimedes_cli.session import SESSION_FILE_ENV, save_session

STAGING = "https://staging.archimedes.invalid"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _local_stack_url(root: Path = REPO_ROOT) -> str:
    """The URL the repo's own `docker compose` stack serves on, read from the two files
    that decide it: docker-compose.yml publishes nginx (the one ingress) on host port
    ``${ARCHIMEDES_HTTP_PORT:-80}``, and .env.example sets ARCHIMEDES_HTTP_PORT."""
    compose = (root / "docker-compose.yml").read_text()
    assert '"${ARCHIMEDES_HTTP_PORT:-80}:8080"' in compose, (
        "docker-compose.yml no longer publishes nginx on ${ARCHIMEDES_HTTP_PORT:-80}; "
        "re-derive the CLI default from the new mapping"
    )
    env = (root / ".env.example").read_text()
    match = re.search(r"^ARCHIMEDES_HTTP_PORT=(\d+)\s*$", env, re.MULTILINE)
    assert match, ".env.example no longer sets ARCHIMEDES_HTTP_PORT"
    return f"http://localhost:{match.group(1)}"


def test_default_is_the_local_compose_stack():
    """The default is the stack `docker compose up` starts from this repo with
    .env.example's settings. It named the hosted site until 2026-11-01, when the project
    went local-first."""
    assert DEFAULT_API_URL == "http://localhost:8080"
    assert _local_stack_url() == DEFAULT_API_URL


def test_local_stack_url_reader_tracks_the_port(tmp_path):
    """Anti-vacuity: the reader follows .env.example, so a port change there turns the
    default test red instead of passing on a stale literal."""
    (tmp_path / "docker-compose.yml").write_text((REPO_ROOT / "docker-compose.yml").read_text())
    (tmp_path / ".env.example").write_text("ARCHIMEDES_HTTP_PORT=9090\n")
    assert _local_stack_url(tmp_path) == "http://localhost:9090"
    assert _local_stack_url(tmp_path) != DEFAULT_API_URL


def test_login_help_shows_the_local_default():
    result = CliRunner().invoke(main, ["login", "--help"])
    assert result.exit_code == 0, result.output
    assert "http://localhost:8080" in result.output
    assert "archimedes-arc.com" not in result.output


def test_resolution_order_explicit_then_session_then_default():
    assert _resolve_api_url("https://x", {"api_url": STAGING}) == "https://x"
    assert _resolve_api_url(None, {"api_url": STAGING}) == STAGING
    assert _resolve_api_url(None, {}) == DEFAULT_API_URL
    assert _resolve_api_url(None, None) == DEFAULT_API_URL


def test_meter_uses_the_session_url_not_the_default(monkeypatch, tmp_path):
    """The regression the finding describes: login against a non-default URL,
    then run plain `meter` — the request must go to the session's host.
    Pre-fix, seen_base_urls captured DEFAULT_API_URL and this test fails."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(SESSION_FILE_ENV, raising=False)  # overrides HOME outright (#1752)
    monkeypatch.delenv("ARCHIMEDES_API_URL", raising=False)
    save_session(api_url=STAGING, cookies={"better-auth.session_token": "t"}, email="a@b.co")

    seen_base_urls: list[str] = []
    real_factory = cli_mod._http_client

    def capturing_factory(api_url, *, cookies=None):
        seen_base_urls.append(api_url)
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"user": {"used": 1, "cap": 10}, "ip": {"used": 1, "cap": 20}, "quote": {}}
            )
        )
        return httpx.Client(base_url=api_url, cookies=cookies, transport=transport)

    monkeypatch.setattr(cli_mod, "_http_client", capturing_factory)
    result = CliRunner().invoke(main, ["meter", "--json"])
    assert result.exit_code == 0, result.output
    assert seen_base_urls == [STAGING]
    assert json.loads(result.stdout)["user"]["used"] == 1
    del real_factory
