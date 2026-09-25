"""``POST /api/account/keys`` — the two properties the production 500 broke.

The incident: every mint on production returned a plain-text ``500 Internal
Server Error`` while persisting the key row, so each attempt consumed one of the
account's 25 slots and returned no token — a slot that can only be recovered by
listing and revoking it. The unit suite was green throughout.

**Why green meant nothing.** ``api/limiter.py`` builds the shared ``Limiter``
with ``enabled=not os.getenv("TESTING")`` and ``headers_enabled=True``.
``conftest.py`` sets ``TESTING=1`` before any archimedes import, so under pytest
``limiter.enabled`` is ``False`` and slowapi's wrapper — including the
header-injection branch that raised — is skipped in its entirety. The suite was
not testing a passing version of the endpoint; it was testing a version of the
stack where the failing code does not run. That is the shape of defect
``CLAUDE.md`` § "the green check may not mean what you think" is about, so the
first test here turns the limiter back **on** and exercises the real wrapper.

Hermetic: no Redis. The limiter's storage is swapped for a fresh in-memory one
so re-enabling it cannot reach a developer's local Redis, and the database is a
per-test tmp-file SQLite via ``tests/db_isolation.py``.

Three guards, in widening scope:

1. the endpoint answers 201 with the token **with the rate limiter enabled** —
   the production configuration, not the test one;
2. a mint whose response body cannot be built leaves **no row behind** — the
   burned-slot safeguard, which is what makes a future 500 recoverable rather
   than a permanently consumed slot;
3. **no** ``@limiter.limit``-decorated handler anywhere in ``archimedes/api``
   can hit the same slowapi raise — the class of bug, not this instance of it.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from archimedes.api import account_auth, api_key_routes
from archimedes.api.limiter import limiter
from archimedes.db import get_session
from archimedes.models.account import AuthUser
from archimedes.models.api_key import ApiKeyRecord
from fastapi import FastAPI
from limits.storage import MemoryStorage
from slowapi.errors import RateLimitExceeded

from tests.db_isolation import redirect_to_tmp_sqlite

USER_ID = "mint-user"

#: Every FastAPI route module. The scan below reads all of them.
_API_DIR = Path(__file__).resolve().parents[1] / "archimedes" / "api"


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path):
    yield from redirect_to_tmp_sqlite(tmp_path)


@pytest.fixture()
def account() -> str:
    session = get_session()
    try:
        now = datetime.now(UTC)
        session.add(
            AuthUser(
                id=USER_ID,
                name=USER_ID,
                email=f"{USER_ID}@example.test",
                email_verified=True,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    finally:
        session.close()
    return USER_ID


@pytest.fixture()
def live_limiter(monkeypatch):
    """Run the limiter the way production does: enabled, headers on, in memory.

    ``enabled=True`` is the whole point — it restores slowapi's wrapper around
    the endpoint, which is where the production exception was raised and which
    the rest of the suite never executes. The storage swap keeps that hermetic:
    the module-level limiter picked its backend at import from ``REDIS_URL``, and
    a developer with Redis on the default port would otherwise have this test
    write to it.
    """
    storage = MemoryStorage()
    monkeypatch.setattr(limiter, "_limiter", type(limiter.limiter)(storage))
    monkeypatch.setattr(limiter, "_storage", storage)
    monkeypatch.setattr(limiter, "enabled", True)
    return limiter


@pytest.fixture()
def app(live_limiter) -> FastAPI:
    application = FastAPI()
    application.state.limiter = live_limiter
    application.add_exception_handler(RateLimitExceeded, lambda _request, _exc: None)
    application.middleware("http")(account_auth.better_auth_session_middleware)
    application.include_router(api_key_routes.api_key_router)
    return application


@pytest.fixture()
def client(app, monkeypatch) -> httpx.AsyncClient:
    """A signed-in browser session — the only credential this surface accepts."""

    async def _fetch(_request):
        return {
            "user": {
                "id": USER_ID,
                "email": f"{USER_ID}@example.test",
                "name": USER_ID,
                "emailVerified": True,
            },
            "session": {"expiresAt": "2099-01-01T00:00:00Z"},
        }

    monkeypatch.setattr(account_auth, "_fetch_session", _fetch)
    # raise_app_exceptions=False so an escaping exception becomes the plain-text
    # 500 a real client sees, instead of surfacing as a test error.
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
        headers={"host": "archimedes-arc.com", "cookie": "better-auth.session_token=opaque"},
    )


def _rows() -> list[ApiKeyRecord]:
    session = get_session()
    try:
        return session.query(ApiKeyRecord).all()
    finally:
        session.close()


# ── 1. The production path ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mint_returns_the_token_with_the_rate_limiter_enabled(account, client):
    """The endpoint must work in the configuration production actually runs.

    Before the fix this was a plain-text 500: ``@limiter.limit`` sends the
    header injection to ``kwargs["response"]`` whenever the handler returns
    something that is not a ``Response``, and ``Limiter._inject_headers`` raises
    ``Exception("parameter `response` must be an instance of
    starlette.responses.Response")`` when that key is absent. The raise lands in
    slowapi's wrapper, outside the endpoint's own ``try``, after the commit.
    """
    async with client as http:
        resp = await http.post("/api/account/keys", json={"name": "ci-nightly"})

    assert resp.status_code == 201, f"{resp.status_code}: {resp.text[:200]}"
    body = resp.json()
    assert body["key"].startswith(f"archim_{body['id']}_")
    assert body["prefix"] == f"archim_{body['id']}"

    # The headers the injection was trying to add. Their presence is what proves
    # the wrapper ran to completion rather than being skipped.
    assert resp.headers["x-ratelimit-limit"] == "10"
    assert "x-ratelimit-remaining" in resp.headers
    # The one response in the system that carries a token must not be cached.
    assert resp.headers["cache-control"] == "no-store"

    assert len(_rows()) == 1


# ── 2. The burned-slot safeguard ──────────────────────────────────────


@pytest.mark.asyncio
async def test_a_failed_response_build_leaves_no_row_behind(account, client, monkeypatch):
    """A 500 must not consume one of the account's 25 slots.

    The row is only durable once the body it will be returned in already exists,
    so any failure between minting and answering rolls the mint back. Without
    that ordering the caller is left holding a key id they cannot see and a
    secret nobody has — recoverable only by listing and revoking it.
    """

    def _explode(**_kwargs):
        raise TypeError("simulated response-construction failure")

    monkeypatch.setattr(api_key_routes, "ApiKeyCreateResponse", _explode)

    async with client as http:
        resp = await http.post("/api/account/keys", json={"name": "doomed"})

    assert resp.status_code == 500
    # Handled, not escaped: a JSON detail rather than plain-text "Internal Server Error".
    assert resp.json()["detail"] == "Could not create API key"
    assert _rows() == [], "a failed mint stranded a key slot"


# ── 3. The class of bug ───────────────────────────────────────────────


def rate_limited_handlers_missing_response(paths: list[Path]) -> list[str]:
    """Names of ``@limiter.limit`` handlers in *paths* that omit ``response``.

    slowapi injects its ``X-RateLimit-*`` headers into the handler's return value
    when that value is a ``Response``, and into ``kwargs["response"]`` otherwise
    — raising if the parameter is not declared. A FastAPI handler returning a
    pydantic model, a dict, or a list therefore **must** declare ``response:
    Response``; every other rate-limited route in this repo already does.
    """
    offenders: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            decorated = any(
                isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr == "limit"
                for dec in node.decorator_list
            )
            if not decorated:
                continue
            names = {arg.arg for arg in node.args.args} | {arg.arg for arg in node.args.kwonlyargs}
            if "response" not in names:
                offenders.append(f"{path.name}:{node.lineno} {node.name}")
    return offenders


def test_no_rate_limited_handler_omits_the_response_parameter():
    """The whole API surface, not just the endpoint that broke."""
    modules = sorted(_API_DIR.glob("*.py"))
    assert modules, f"no route modules found under {_API_DIR}"
    assert rate_limited_handlers_missing_response(modules) == []


def test_the_guard_rejects_a_handler_that_omits_response(tmp_path):
    """The guard above must fail something — this is the input it has to reject.

    ``bad.py`` is the exact pre-fix signature of ``create_api_key``; ``good.py``
    is the fixed one. A guard that passed both would enforce nothing.
    """
    bad = tmp_path / "bad.py"
    bad.write_text(
        "@api_key_router.post('')\n"
        "@limiter.limit('10/hour')\n"
        "async def create_api_key(payload, request, user=Depends(require_session_credential)):\n"
        "    ...\n",
        encoding="utf-8",
    )
    good = tmp_path / "good.py"
    good.write_text(
        "@api_key_router.post('')\n"
        "@limiter.limit('10/hour')\n"
        "async def create_api_key(payload, request, response, user=Depends(require_session_credential)):\n"
        "    ...\n",
        encoding="utf-8",
    )

    assert rate_limited_handlers_missing_response([bad]) == ["bad.py:3 create_api_key"]
    assert rate_limited_handlers_missing_response([good]) == []
