"""A cached session's api_url must win over the global default (review finding:
it was write-only — a staging login silently sent its cookie to prod)."""

from __future__ import annotations

import json

import httpx
from click.testing import CliRunner

from archimedes_cli import cli as cli_mod
from archimedes_cli.cli import DEFAULT_API_URL, _resolve_api_url, main
from archimedes_cli.session import SESSION_FILE_ENV, save_session

STAGING = "https://staging.archimedes.invalid"


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
