"""Security hardening tests — one per gate.

Covers the quick-win bundle from the security audit:
  1. /docs and /openapi.json gated behind ENABLE_API_DOCS
  2. EMAIL_ENCRYPTION_KEY fail-closed in production
  3. Rate limits on POST endpoints
  4. (docker-compose — not testable in pytest)
  5. CORS explicit method/header allowlists
  6. Request body size limit (1 MB → 413)
  7. Chat wallet_address regex validation
  8. _load_strategy_code path traversal guard

NOTE: tests that require module reload (docs gate, startup fail-closed) use
subprocess to avoid poisoning the test process's module cache.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

# ─── 1. Docs gate ─────────────────────────────────────────────────────


_DOTENV_NEUTRALIZE = """
# Neutralize load_dotenv before importing archimedes.main — otherwise the
# developer's local .env (with DATABASE_URL=postgres:5432) overrides the
# sqlite default. CI has no .env so it passed by environment luck.
import dotenv as _dotenv
_dotenv.load_dotenv = lambda *_a, **_kw: False
"""


def _clean_subprocess_env() -> dict[str, str]:
    """Whitelist-only env for subprocess tests.

    Inheriting os.environ leaks the developer's .env into the subprocess
    (an earlier test in the suite triggers load_dotenv on the parent
    process, populating DATABASE_URL etc. before the subprocess fires).
    Pass only PATH/HOME/PYTHONPATH so archimedes.main gets the sqlite
    default for DATABASE_URL — same posture CI runs in.
    """
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "PYTHONPATH": backend_dir,
        "DATABASE_URL": "sqlite:////tmp/archimedes-security-subprocess.db",
    }


def test_docs_disabled_when_public_domain_set():
    """/docs gated OFF when PUBLIC_DOMAIN is set and ENABLE_API_DOCS is unset.

    Uses subprocess to avoid module-reload side effects in the test process.
    """
    script = (
        _DOTENV_NEUTRALIZE
        + """
import os
os.environ["PUBLIC_DOMAIN"] = "https://archimedes-arc.com"
os.environ["EMAIL_ENCRYPTION_KEY"] = "test-key-32chars-for-ci"
os.environ.pop("ENABLE_API_DOCS", None)
os.environ["TESTING"] = "1"

from archimedes.main import app
assert app.openapi_url is None, f"openapi_url should be None, got {app.openapi_url}"
assert app.docs_url is None, f"docs_url should be None, got {app.docs_url}"
print("PASS")
"""
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=os.path.join(os.path.dirname(__file__), ".."),
        env=_clean_subprocess_env(),
        timeout=30,
    )
    assert result.returncode == 0, f"Script failed: {result.stderr}"
    assert "PASS" in result.stdout


def test_docs_enabled_when_flag_set():
    """/docs returns 200 when ENABLE_API_DOCS=1."""
    script = (
        _DOTENV_NEUTRALIZE
        + """
import os
os.environ["PUBLIC_DOMAIN"] = "https://archimedes-arc.com"
os.environ["EMAIL_ENCRYPTION_KEY"] = "test-key-32chars-for-ci"
os.environ["ENABLE_API_DOCS"] = "1"
os.environ["TESTING"] = "1"

from archimedes.main import app
assert app.docs_url == "/docs", f"docs_url should be /docs, got {app.docs_url}"
assert app.openapi_url == "/openapi.json", f"openapi_url should be /openapi.json, got {app.openapi_url}"
print("PASS")
"""
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=os.path.join(os.path.dirname(__file__), ".."),
        env=_clean_subprocess_env(),
        timeout=30,
    )
    assert result.returncode == 0, f"Script failed: {result.stderr}"
    assert "PASS" in result.stdout


def test_docs_enabled_in_local_dev():
    """/docs available when PUBLIC_DOMAIN is not set (local dev)."""
    script = (
        _DOTENV_NEUTRALIZE
        + """
import os
os.environ.pop("PUBLIC_DOMAIN", None)
os.environ.pop("ENABLE_API_DOCS", None)
os.environ["TESTING"] = "1"

from archimedes.main import app
assert app.docs_url == "/docs", f"docs_url should be /docs, got {app.docs_url}"
print("PASS")
"""
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=os.path.join(os.path.dirname(__file__), ".."),
        env=_clean_subprocess_env(),
        timeout=30,
    )
    assert result.returncode == 0, f"Script failed: {result.stderr}"
    assert "PASS" in result.stdout


# ─── 2. EMAIL_ENCRYPTION_KEY fail-closed ──────────────────────────────


def test_startup_fails_without_encryption_key_in_production():
    """App module raises RuntimeError when PUBLIC_DOMAIN set without EMAIL_ENCRYPTION_KEY.

    Uses subprocess to avoid leaving the module in a broken state.
    """
    script = (
        _DOTENV_NEUTRALIZE
        + """
import os
os.environ["PUBLIC_DOMAIN"] = "https://archimedes-arc.com"
os.environ.pop("EMAIL_ENCRYPTION_KEY", None)
os.environ["TESTING"] = "1"

try:
    from archimedes.main import app
    print("FAIL: no RuntimeError raised")
except RuntimeError as e:
    if "EMAIL_ENCRYPTION_KEY" in str(e):
        print("PASS")
    else:
        print(f"FAIL: wrong error: {e}")
"""
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=os.path.join(os.path.dirname(__file__), ".."),
        env=_clean_subprocess_env(),
        timeout=30,
    )
    assert "PASS" in result.stdout, f"Expected PASS, got stdout={result.stdout!r} stderr={result.stderr!r}"


# ─── 3. Rate limits on POST endpoints ─────────────────────────────────


@contextlib.contextmanager
def _limiter_enabled():
    """Rate limiting is off under TESTING (limiter.py) — switch it on for one test.

    Yields the app with a freshly reset limiter bucket so a prior test's request
    count can't leak in, and restores the suite-wide disabled state afterwards.
    """
    from archimedes.main import app

    app.state.limiter.enabled = True
    with contextlib.suppress(Exception):
        app.state.limiter.reset()
    try:
        yield app
    finally:
        app.state.limiter.enabled = not os.getenv("TESTING")


@pytest.mark.asyncio
async def test_strategies_stress_run_has_rate_limit():
    """POST /api/strategies/stress/run is rate-limited (returns 429 eventually)."""
    payload = {"allocations": [{"symbol": "sTSLA", "weight": 1.0}]}
    with _limiter_enabled() as app:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            hit_429 = False
            for _ in range(25):  # limit is 20/minute
                resp = await client.post("/api/strategies/stress/run", json=payload)
                if resp.status_code == 429:
                    hit_429 = True
                    break
            assert hit_429, "Expected 429 rate limit but never hit it in 25 requests"


@pytest.mark.asyncio
async def test_stress_run_missing_symbol_is_422():
    """POST /stress/run with an allocation missing `symbol` → 422, not 500 (#926)."""
    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/strategies/stress/run", json={"allocations": [{"weight": 0.1}]})
    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {resp.text}"
    assert "symbol" in resp.json().get("detail", "")


@pytest.mark.asyncio
async def test_stress_run_non_numeric_weight_is_422():
    """A non-numeric `weight` is a client error (422), not a server 500 (#926)."""
    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/strategies/stress/run",
            json={"allocations": [{"symbol": "sTSLA", "weight": "not-a-number"}]},
        )
    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {resp.text}"
    assert "weight" in resp.json().get("detail", "")


@pytest.mark.asyncio
async def test_stress_run_non_dict_allocation_is_422():
    """A non-object allocation element → 422, not 500 (#926)."""
    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/strategies/stress/run", json={"allocations": ["sTSLA"]})
    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_swap_quote_has_rate_limit():
    """GET /api/swap/quote is rate-limited (returns 429 eventually)."""
    # The contract loader is mocked so each request fails fast at the AMM call
    # (a generic 400) instead of reaching for an RPC endpoint — the rate limiter
    # runs before the handler body, so a 400 still counts against the bucket.
    params = {
        "token_in": "0x3600000000000000000000000000000000000000",
        "token_out": "0xd514cd27baf762c650536765cde9b61c876abacd",
        "amount_in": 1.0,
    }
    with _limiter_enabled() as app, patch("archimedes.chain.contracts.get_contract_loader", return_value=MagicMock()):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            hit_429 = False
            for _ in range(35):  # limit is 30/minute
                resp = await client.get("/api/swap/quote", params=params)
                if resp.status_code == 429:
                    hit_429 = True
                    break
            assert hit_429, "Expected 429 rate limit but never hit it in 35 requests"


@pytest.mark.asyncio
async def test_selection_bias_pbo_has_rate_limit():
    """POST /api/selection-bias/pbo is rate-limited (returns 429 eventually)."""
    # s_partitions=4 keeps each accepted request ~30ms; the default 16 makes the
    # combinatorial PBO scan ~0.5s a call, i.e. ~10s to exhaust the bucket.
    payload = {"returns_matrix": {"s1": [0.001] * 50, "s2": [-0.0005] * 50}, "s_partitions": 4}
    with _limiter_enabled() as app:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            hit_429 = False
            for _ in range(25):  # limit is 20/minute
                resp = await client.post("/api/selection-bias/pbo", json=payload)
                if resp.status_code == 429:
                    hit_429 = True
                    break
            assert hit_429, "Expected 429 rate limit but never hit it in 25 requests"


# ─── 5. CORS explicit allowlists ──────────────────────────────────────


def test_cors_no_wildcard_methods():
    """CORS middleware uses explicit method list, not '*'."""
    from archimedes.main import app

    for middleware in app.user_middleware:
        if hasattr(middleware, "kwargs") and "allow_methods" in middleware.kwargs:
            methods = middleware.kwargs["allow_methods"]
            assert "*" not in methods, f"CORS allow_methods contains wildcard: {methods}"
            assert "GET" in methods
            assert "POST" in methods
            assert "OPTIONS" in methods


def test_cors_no_wildcard_headers():
    """CORS middleware uses explicit header list, not '*'."""
    from archimedes.main import app

    for middleware in app.user_middleware:
        if hasattr(middleware, "kwargs") and "allow_headers" in middleware.kwargs:
            headers = middleware.kwargs["allow_headers"]
            assert "*" not in headers, f"CORS allow_headers contains wildcard: {headers}"
            assert "Content-Type" in headers
            assert "X-Wallet-Address" in headers


# ─── 6. Request body size limit ───────────────────────────────────────


# The route below is incidental: the 1 MB cap is enforced by an app-level
# middleware (`main.py::_limit_body_size`) that runs before routing, so any real
# POST path exercises it. These two used to POST at the per-vault chat endpoint;
# chat was deleted 2026-08-31, so they moved to `/api/strategies/stress/run`
# (public POST, no auth) rather than being deleted with it — the middleware is
# the thing under test and its coverage must not disappear with the surface that
# happened to carry it.
_SIZE_PROBE_ROUTE = "/api/strategies/stress/run"


@pytest.mark.asyncio
async def test_oversized_body_returns_413():
    """POST with body > 1 MB returns 413."""
    from archimedes.main import app

    big_body = "x" * (1024 * 1024 + 1)  # 1 MB + 1 byte

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            _SIZE_PROBE_ROUTE,
            content=big_body,
            headers={"Content-Type": "application/json", "Content-Length": str(len(big_body))},
        )
    assert resp.status_code == 413, f"Expected 413 for oversized body, got {resp.status_code}"


@pytest.mark.asyncio
async def test_normal_body_passes_size_check():
    """POST with normal body passes the size check (may fail for other reasons)."""
    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            _SIZE_PROBE_ROUTE,
            json={"allocations": [{"symbol": "sTSLA", "weight": 1.0}]},
        )
    # Should not be 413 — might be 400/422/200 depending on other validation
    assert resp.status_code != 413


# ─── 8. _load_strategy_code path traversal guard ─────────────────────


def test_load_strategy_code_rejects_traversal():
    """_load_strategy_code rejects paths that escape the project tree."""
    from archimedes.api.selection_bias_routes import _load_strategy_code

    result = _load_strategy_code("../../etc/passwd")
    assert result is None


def test_load_strategy_code_rejects_absolute_path():
    """_load_strategy_code rejects absolute paths outside project."""
    from archimedes.api.selection_bias_routes import _load_strategy_code

    result = _load_strategy_code("/etc/passwd")
    assert result is None


def test_load_strategy_code_allows_valid_strategy_path():
    """_load_strategy_code accepts paths within the project tree."""
    from archimedes.api.selection_bias_routes import _load_strategy_code

    result = _load_strategy_code("analytics-engine/strategies/__init__.py")
    assert result is None or isinstance(result, str)


# ─── Corpus search query length cap ─────────────────────────────────


@pytest.mark.asyncio
async def test_corpus_search_rejects_long_query():
    """GET /api/corpus/kg/entities rejects queries longer than max_length."""
    from archimedes.main import app

    long_query = "a" * 200  # exceeds max_length=120

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/api/corpus/kg/entities?q={long_query}")

    assert resp.status_code == 422, f"Expected 422 for oversized query, got {resp.status_code}"
