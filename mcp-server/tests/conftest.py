"""Shared fixtures. Every HTTP test in this suite is served by ``httpx.MockTransport``.

Mocking at the boundary — the ``httpx.Client`` construction point, not the tools' internals
— is the repo's stated convention (CLAUDE.md § Testing conventions) and the reason
``client._http_client`` exists as a single function at all.

``HOME`` is redirected to a tmp dir for every test in the suite, unconditionally. The
credential fallback reads ``~/.config/archimedes/session.json`` through
``Path.home()``, so without this a developer who happens to be logged in would run a
different test than CI does — and the "no credential" tests would silently pass for the
wrong reason. ``ARCHIMEDES_SESSION_FILE`` (which overrides that path outright, #1752),
``ARCHIMEDES_API_KEY`` and ``ARCHIMEDES_API_URL`` are cleared for the same reason.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
from archimedes_cli.session import SESSION_FILE_ENV
from archimedes_mcp import client
from archimedes_mcp.credentials import API_KEY_ENV, API_URL_ENV

TEST_API_URL = "https://api.test.invalid"


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(SESSION_FILE_ENV, raising=False)
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    monkeypatch.setenv(API_URL_ENV, TEST_API_URL)
    return tmp_path


@pytest.fixture
def cached_session(tmp_path):
    """Write a session cache exactly as ``archimedes login`` would against LOCAL HTTP
    (the bare cookie name), and hand back the token."""
    from archimedes_cli.session import SESSION_COOKIE_NAME, save_session

    token = "SESSION-TOKEN-b8b1f0c2e5a94d17"
    save_session(api_url=TEST_API_URL, cookies={SESSION_COOKIE_NAME: token}, email="agent@example.test")
    assert (Path(tmp_path) / ".config" / "archimedes" / "session.json").exists()
    return token


@pytest.fixture
def cached_secure_session(tmp_path):
    """The same, but as ``archimedes login`` would cache it against PRODUCTION — the
    ``__Secure-``-prefixed cookie name Better Auth actually issues over HTTPS
    (``useSecureCookies: production`` in ``auth/auth.js``). A separate fixture, not a
    parametrization of ``cached_session`` above, so a test can assert on the exact name
    it expects without threading a parameter through every caller."""
    from archimedes_cli.session import SECURE_SESSION_COOKIE_NAME, save_session

    token = "SESSION-TOKEN-secure-3a9c7e1f0b6d"
    save_session(api_url=TEST_API_URL, cookies={SECURE_SESSION_COOKIE_NAME: token}, email="agent@example.test")
    assert (Path(tmp_path) / ".config" / "archimedes" / "session.json").exists()
    return token


@pytest.fixture
def api_key(monkeypatch):
    key = "archim_9f3c1a77b204de51_TESTSECRETvalue0000"
    monkeypatch.setenv(API_KEY_ENV, key)
    return key


class Recorder:
    """Captured requests plus the canned responses handed back, in order."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    @property
    def last(self) -> httpx.Request:
        assert self.requests, "no request was made"
        return self.requests[-1]


@pytest.fixture
def mock_api(monkeypatch):
    """``mock_api(handler)`` installs ``handler`` at the HTTP boundary and returns a Recorder.

    ``handler`` is ``(httpx.Request) -> httpx.Response``. The real ``_http_client`` is still
    the thing under test for its own settings, so the patched factory rebuilds a client with
    the SAME arguments and only swaps the transport — a test that asserted on headers would
    otherwise be asserting on a fixture's idea of them rather than on production's.
    """

    def install(handler):
        recorder = Recorder()

        def recording_handler(request: httpx.Request) -> httpx.Response:
            recorder.requests.append(request)
            return handler(request)

        real_factory = client._http_client

        def factory(api_url, credential):
            built = real_factory(api_url, credential)
            return httpx.Client(
                base_url=built.base_url,
                headers=built.headers,
                cookies=built.cookies,
                timeout=built.timeout,
                follow_redirects=built.follow_redirects,
                transport=httpx.MockTransport(recording_handler),
            )

        monkeypatch.setattr(client, "_http_client", factory)
        return recorder

    return install


def json_response(status: int, payload, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json=payload, headers=headers or {})


def read_session_file() -> dict:
    return json.loads((Path(os.environ["HOME"]) / ".config" / "archimedes" / "session.json").read_text())
