"""Unit coverage for ``archimedes_cli.session.pick_session_cookie`` and the two cookie
name constants it picks between.

Production sets the ``__Secure-``-prefixed cookie (Better Auth's ``useSecureCookies:
production`` in ``auth/auth.js``); local HTTP can only ever set the bare name. Everything
downstream of :func:`load_session` — this CLI's own commands and the MCP server's
credential resolver — goes through :func:`pick_session_cookie` rather than a single
hardcoded name, so this file is the one place that logic is pinned directly, independent
of the HTTP-level login/meter tests in ``test_cli.py``.
"""

from __future__ import annotations

from archimedes_cli.session import (
    SECURE_SESSION_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    SESSION_COOKIE_NAMES,
    pick_session_cookie,
)


def test_the_secure_name_is_the_bare_name_with_the_prefix():
    assert f"__Secure-{SESSION_COOKIE_NAME}" == SECURE_SESSION_COOKIE_NAME


def test_names_are_distinct_and_secure_is_preferred():
    assert SESSION_COOKIE_NAME != SECURE_SESSION_COOKIE_NAME
    assert SESSION_COOKIE_NAMES == (SECURE_SESSION_COOKIE_NAME, SESSION_COOKIE_NAME)


def test_picks_the_bare_cookie_local_http_sets():
    assert pick_session_cookie({SESSION_COOKIE_NAME: "tok"}) == (SESSION_COOKIE_NAME, "tok")


def test_picks_the_secure_prefixed_cookie_production_sets():
    """This is the exact shape production returns and the bare-name-only reader missed —
    the P0 this module fixes."""
    assert pick_session_cookie({SECURE_SESSION_COOKIE_NAME: "tok"}) == (SECURE_SESSION_COOKIE_NAME, "tok")


def test_prefers_the_secure_name_when_both_are_somehow_present():
    cookies = {SESSION_COOKIE_NAME: "bare-value", SECURE_SESSION_COOKIE_NAME: "secure-value"}
    assert pick_session_cookie(cookies) == (SECURE_SESSION_COOKIE_NAME, "secure-value")


def test_adversarial_an_unrelated_cookie_name_is_not_picked_up():
    """The input that should fail: a cookie jar holding something else entirely (a CSRF
    token, a load-balancer affinity cookie) must not be mistaken for a session."""
    assert pick_session_cookie({"csrf_token": "irrelevant", "other": "x"}) is None


def test_adversarial_an_empty_value_is_not_a_session():
    """A key present with an empty string is not a usable credential — same as absent."""
    assert pick_session_cookie({SECURE_SESSION_COOKIE_NAME: ""}) is None


def test_adversarial_a_non_string_value_is_not_a_session():
    """Defensive against a malformed/corrupted cache (e.g. a stray int from bad JSON)."""
    assert pick_session_cookie({SECURE_SESSION_COOKIE_NAME: 12345}) is None


def test_empty_cookies_yield_nothing():
    assert pick_session_cookie({}) is None
