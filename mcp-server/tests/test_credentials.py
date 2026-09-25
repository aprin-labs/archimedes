"""Credential resolution, precedence, and the one-credential-on-the-wire rule."""

from __future__ import annotations

from archimedes_cli.session import SECURE_SESSION_COOKIE_NAME, SESSION_COOKIE_NAME
from archimedes_mcp import client
from archimedes_mcp.credentials import (
    KIND_API_KEY,
    KIND_SESSION_COOKIE,
    resolve_api_url,
    resolve_credential,
)
from conftest import TEST_API_URL, json_response


def test_no_credential_when_neither_source_exists():
    assert resolve_credential() is None


def test_env_key_is_used_when_set(api_key):
    credential = resolve_credential()
    assert credential is not None
    assert credential.kind == KIND_API_KEY
    assert credential.auth_headers() == {"Authorization": f"Bearer {api_key}"}
    assert credential.auth_cookies() == {}


def test_blank_env_key_is_treated_as_unset(monkeypatch, cached_session):
    """An empty env var is how a shell says 'not configured', not 'use this key'."""
    monkeypatch.setenv("ARCHIMEDES_API_KEY", "   ")
    credential = resolve_credential()
    assert credential is not None
    assert credential.kind == KIND_SESSION_COOKIE


def test_session_cache_is_the_fallback(cached_session):
    credential = resolve_credential()
    assert credential is not None
    assert credential.kind == KIND_SESSION_COOKIE
    assert credential.auth_cookies() == {SESSION_COOKIE_NAME: cached_session}
    assert credential.auth_headers() == {}


def test_a_session_cached_against_production_uses_the_secure_prefixed_cookie(cached_secure_session):
    """The P0 this fix closes, at the credential-resolution layer: a session cached from
    a login against production (``__Secure-better-auth.session_token``) must resolve to a
    usable credential, and the cookie it sends must be the SAME name it was cached
    under — never silently rewritten to the bare name, which prod would not recognize."""
    credential = resolve_credential()
    assert credential is not None
    assert credential.kind == KIND_SESSION_COOKIE
    assert credential.auth_cookies() == {SECURE_SESSION_COOKIE_NAME: cached_secure_session}
    assert SESSION_COOKIE_NAME not in credential.auth_cookies()
    assert credential.auth_headers() == {}


def test_env_key_wins_over_a_cached_session(api_key, cached_session):
    credential = resolve_credential()
    assert credential.kind == KIND_API_KEY


def test_corrupt_session_cache_is_not_a_credential(tmp_path):
    """Same never-raises posture as ``archimedes_cli.session.load_session``: a broken cache
    is 'not logged in', not a crash inside a long-lived stdio server."""
    path = tmp_path / ".config" / "archimedes" / "session.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert resolve_credential() is None


def test_session_cache_without_the_session_cookie_is_not_a_credential(tmp_path):
    from archimedes_cli.session import save_session

    save_session(api_url=TEST_API_URL, cookies={"some_other_cookie": "x"}, email="a@b.test")
    assert resolve_credential() is None


def test_api_url_env_wins(monkeypatch, cached_session):
    monkeypatch.setenv("ARCHIMEDES_API_URL", "https://explicit.invalid")
    assert resolve_api_url() == "https://explicit.invalid"


def test_api_url_falls_back_to_the_cached_session_host(monkeypatch, cached_session):
    monkeypatch.delenv("ARCHIMEDES_API_URL", raising=False)
    assert resolve_api_url() == TEST_API_URL


def test_api_url_default_when_nothing_is_configured(monkeypatch):
    monkeypatch.delenv("ARCHIMEDES_API_URL", raising=False)
    assert resolve_api_url() == "https://archimedes-arc.com"


# ── the wire ────────────────────────────────────────────────────────


def _capture(mock_api, credential):
    recorder = mock_api(lambda request: json_response(200, {"ok_from_server": True}))
    client.request("GET", "/api/account/usage", api_url=TEST_API_URL, credential=credential)
    return recorder.last


def test_api_key_call_sends_a_bearer_header_and_no_cookie(mock_api, api_key, cached_session):
    """Both credentials present. Exactly one goes out.

    The server pins the opposite precedence (cookie wins — ``dbrowneup/1653-scoped-api-keys``
    commit f60fb27a). Sending one credential means that disagreement can never decide which
    account a tool call acts as.
    """
    request = _capture(mock_api, resolve_credential())
    assert request.headers["Authorization"] == f"Bearer {api_key}"
    assert "cookie" not in {k.lower() for k in request.headers}


def test_session_call_sends_a_cookie_and_no_authorization_header(mock_api, cached_session):
    request = _capture(mock_api, resolve_credential())
    assert SESSION_COOKIE_NAME in request.headers["Cookie"]
    assert "authorization" not in {k.lower() for k in request.headers}


def test_session_call_against_a_production_style_cache_sends_the_secure_cookie(mock_api, cached_secure_session):
    """Wire-level version of the credential-resolution test above: a call made with a
    production-cached credential must put ``__Secure-better-auth.session_token`` on the
    request, not the bare name — this is what actually reaches ``archimedes-arc.com``."""
    request = _capture(mock_api, resolve_credential())
    assert f"{SECURE_SESSION_COOKIE_NAME}=" in request.headers["Cookie"]
    assert "authorization" not in {k.lower() for k in request.headers}


def test_anonymous_call_sends_neither(mock_api):
    request = _capture(mock_api, None)
    header_names = {k.lower() for k in request.headers}
    assert "authorization" not in header_names
    assert "cookie" not in header_names


def test_client_never_follows_redirects(api_key):
    """A redirect could bounce a request carrying a bearer token to another host."""
    built = client._http_client(TEST_API_URL, resolve_credential())
    try:
        assert built.follow_redirects is False
    finally:
        built.close()


def test_client_sends_a_non_browser_user_agent(mock_api):
    request = _capture(mock_api, None)
    assert request.headers["User-Agent"] == "archimedes-mcp"
