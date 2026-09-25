"""Scoped API keys — the security contract, proved rather than asserted (#1680, #1653 D3).

Every test here corresponds to a numbered acceptance criterion or anti-goal on the
issue, and the adversarial ones are named for the thing they try and fail to do.
The organising idea: a key is a **second credential for one account**, so the
properties worth pinning are (1) the token exists in exactly one place for exactly
one round trip, (2) the credential resolves to the same identity a cookie does and
therefore cannot bypass anything, and (3) every way of asking "whose key is this"
is scoped by ``user_id``.

Hermetic: a per-test tmp-file SQLite database via ``tests/db_isolation.py`` (the
same helper the account/ledger tests use), the Better Auth session fetch stubbed
at ``account_auth._fetch_session``, and no Redis / Postgres / network anywhere.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from archimedes.api import account_auth, api_key_auth, api_key_routes
from archimedes.api.account_auth import CurrentUser, require_current_user
from archimedes.db import get_session
from archimedes.models.account import AuthUser
from archimedes.models.api_key import MAX_KEYS_PER_ACCOUNT, ApiKeyRecord
from fastapi import Depends, FastAPI, Request
from sqlalchemy import inspect, text

from tests.db_isolation import redirect_to_tmp_sqlite

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path):
    """A fresh SQLite file per test; restored afterwards (issue #1100's lesson)."""
    yield from redirect_to_tmp_sqlite(tmp_path)


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_archimedes.db"


def _make_account(user_id: str) -> None:
    session = get_session()
    try:
        now = datetime.now(UTC)
        session.add(
            AuthUser(
                id=user_id,
                name=user_id,
                email=f"{user_id}@example.test",
                email_verified=True,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    finally:
        session.close()


def _mint_for(user_id: str, name: str = "ci") -> str:
    """Create *user_id*'s account if needed and return a fresh token."""
    session = get_session()
    try:
        if session.get(AuthUser, user_id) is None:
            session.close()
            _make_account(user_id)
            session = get_session()
        _, token = api_key_auth.mint(session, user_id=user_id, name=name)
        session.commit()
        return token
    finally:
        session.close()


@pytest.fixture()
def app() -> FastAPI:
    """The identity chokepoint + the key-management router + one probe route.

    ``/whoami`` depends on the unmodified ``require_current_user`` — the point of
    the design is that a route can be written without knowing API keys exist, so
    the probe is deliberately written that way.
    """
    application = FastAPI()
    application.middleware("http")(account_auth.better_auth_session_middleware)
    application.include_router(api_key_routes.api_key_router)

    @application.get("/whoami")
    async def whoami(request: Request, user: CurrentUser = Depends(require_current_user)):
        return {
            "user_id": user.id,
            "email": user.email,
            "credential": account_auth.get_auth_credential(request),
        }

    return application


def _client(app: FastAPI, *, token: str | None = None, cookie: bool = False) -> httpx.AsyncClient:
    headers: dict[str, str] = {"host": "archimedes-arc.com"}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    if cookie:
        headers["cookie"] = "better-auth.session_token=opaque"
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers)


def _sign_in(monkeypatch, user_id: str) -> None:
    """Point the Better Auth fetch at *user_id* (overrides conftest's adapter)."""

    async def fetch(_request):
        return {
            "user": {
                "id": user_id,
                "name": user_id,
                "email": f"{user_id}@example.test",
                "emailVerified": True,
            },
            "session": {"id": f"s-{user_id}", "expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
        }

    monkeypatch.setattr(account_auth, "_fetch_session", fetch)


# ── A1: token shape and entropy ───────────────────────────────────────


def test_minted_token_shape_and_entropy():
    """A1 — ``archim_<id>_<secret>``; the secret carries ≥32 bytes; mints are unique."""
    _make_account("user-a1")
    session = get_session()
    tokens = set()
    try:
        for i in range(100):
            record, token = api_key_auth.mint(session, user_id="user-a1", name=f"k{i}")
            tokens.add(token)
            assert token.startswith("archim_")
            parsed = api_key_auth.parse_authorization_header(f"Bearer {token}")
            assert parsed is not None
            key_id, secret = parsed
            assert key_id == record.id
            assert len(key_id) == api_key_auth.KEY_ID_HEX_CHARS
            # token_urlsafe(32) is 32 bytes of entropy rendered as ~43 chars.
            # Asserting on the character count would pin the encoding; asserting
            # on the decoded entropy pins the property that matters.
            assert len(secret) >= (api_key_auth.SECRET_BYTES * 4) // 3
        session.commit()
    finally:
        session.close()

    assert len(tokens) == 100, "100 mints produced a duplicate token"


# ── A2: the token is not stored, anywhere ─────────────────────────────


def test_token_never_appears_in_any_column_or_in_the_database_file(db_path):
    """A2 — the strongest form: dump every column of every row, AND grep the
    on-disk database file for the secret. Both must come back empty."""
    token = _mint_for("user-a2")
    secret = token.split("_", 2)[2]

    session = get_session()
    try:
        columns = [c["name"] for c in inspect(session.get_bind()).get_columns("api_keys")]
        rows = session.execute(text("SELECT * FROM api_keys")).mappings().all()
        assert rows, "no key row was written"
        for row in rows:
            for column in columns:
                value = str(row[column])
                assert token not in value, f"the full token is stored in api_keys.{column}"
                assert secret not in value, f"the secret half is stored in api_keys.{column}"
    finally:
        session.close()

    # And the file itself — a column dump only covers columns we thought to look
    # at; the raw bytes cover the ones we did not.
    blob = db_path.read_bytes()
    assert secret.encode() not in blob, "the secret is recoverable from the database file"
    assert token.encode() not in blob, "the token is recoverable from the database file"


# ── A3: constant-time on the hit path AND the miss path ───────────────


def test_unknown_key_id_still_performs_a_comparison():
    """A3 (adversarial, timing) — the miss path must not short-circuit.

    An early ``return None`` on an unknown key id makes "no such key" measurably
    faster than "wrong secret", which lets an attacker enumerate valid key ids by
    timing alone. The guard is that ``verify`` runs a dummy comparison; this test
    fails the moment someone "optimises" it away.
    """
    session = get_session()
    try:
        with patch.object(api_key_auth.hmac, "compare_digest", wraps=api_key_auth.hmac.compare_digest) as spy:
            assert api_key_auth.verify(session, "0" * 16, "no-such-secret") is None
        assert spy.call_count == 1, "the unknown-key-id path skipped the constant-time comparison"
    finally:
        session.close()


def test_verification_uses_constant_time_comparison_on_the_hit_path():
    """A3 — and the real comparison goes through ``hmac.compare_digest`` too."""
    token = _mint_for("user-a3")
    key_id, secret = api_key_auth.parse_authorization_header(f"Bearer {token}")

    session = get_session()
    try:
        with patch.object(api_key_auth.hmac, "compare_digest", wraps=api_key_auth.hmac.compare_digest) as spy:
            assert api_key_auth.verify(session, key_id, secret) is not None
        assert spy.call_count == 1
    finally:
        session.close()


# ── A4: one identity, two credentials ─────────────────────────────────


@pytest.mark.asyncio
async def test_key_and_cookie_resolve_the_same_account_identity(app, monkeypatch):
    """A4 — the whole design in one assertion: same account, either credential."""
    token = _mint_for("user-a4")
    _sign_in(monkeypatch, "user-a4")

    async with _client(app, cookie=True) as client:
        by_cookie = await client.get("/whoami")
    async with _client(app, token=token) as client:
        by_key = await client.get("/whoami")

    assert by_cookie.status_code == 200
    assert by_key.status_code == 200
    assert by_key.json()["user_id"] == by_cookie.json()["user_id"] == "user-a4"
    assert by_key.json()["email"] == by_cookie.json()["email"]
    # The ONLY thing that differs is which credential proved it.
    assert by_cookie.json()["credential"] == "session"
    assert by_key.json()["credential"] == "api_key"


@pytest.mark.asyncio
async def test_when_both_credentials_are_present_the_cookie_wins_deterministically(app, monkeypatch):
    """Two credentials on one request must resolve to ONE identity, always the same one.

    Not a security boundary — holding another account's session cookie is already
    full compromise of that account — but ambiguity here would be resolved by
    whichever branch a future refactor happened to run first, and a caller could
    not predict which account it was acting as. The cookie wins because it is the
    pre-existing behaviour and the browser majority; the key is attempted only
    when the cookie produced no user. Pinned so the order is a decision rather
    than an accident.
    """
    token = _mint_for("user-key-side")
    _sign_in(monkeypatch, "user-cookie-side")

    headers = {
        "host": "archimedes-arc.com",
        "cookie": "better-auth.session_token=opaque",
        "authorization": f"Bearer {token}",
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=headers) as client:
        response = await client.get("/whoami")

    assert response.status_code == 200
    assert response.json()["user_id"] == "user-cookie-side"
    assert response.json()["credential"] == "session"


# ── A5 / A6: shown once, never again ──────────────────────────────────


@pytest.mark.asyncio
async def test_create_returns_the_secret_once_and_list_never_does(app, monkeypatch):
    """A5 + A6 (adversarial, leak) — serialise the list response and grep it."""
    _make_account("user-a5")
    _sign_in(monkeypatch, "user-a5")

    async with _client(app, cookie=True) as client:
        created = await client.post("/api/account/keys", json={"name": "ci-nightly"})
        listed = await client.get("/api/account/keys")

    assert created.status_code == 201
    token = created.json()["key"]
    secret = token.split("_", 2)[2]
    assert token.startswith("archim_")

    assert listed.status_code == 200
    body = json.dumps(listed.json())
    assert token not in body, "the list endpoint returned the token"
    assert secret not in body, "the list endpoint returned the secret"

    row = listed.json()[0]
    assert set(row) == {"id", "name", "prefix", "created_at", "last_used_at", "revoked_at"}
    assert row["prefix"] == f"archim_{row['id']}"
    assert row["name"] == "ci-nightly"
    assert row["revoked_at"] is None
    # The prefix identifies the key and cannot be used as one.
    async with _client(app, token=row["prefix"]) as client:
        assert (await client.get("/whoami")).status_code == 401


# ── A7: a wrong key is 401 ────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bogus",
    [
        "archim_0000000000000000_ZmFrZS1zZWNyZXQtdGhhdC13YXMtbmV2ZXItbWludGVk",  # well-formed, never minted
        "archim_deadbeefdeadbeef_",  # empty secret
        "archim_",  # prefix only
        "archim_nounderscoreafterprefix",  # no separator
        "not-even-close",
    ],
)
async def test_wrong_key_is_401(app, bogus):
    """A7 (adversarial) — nothing that was not minted here gets in."""
    async with _client(app, token=bogus) as client:
        response = await client.get("/whoami")
    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication required"}


@pytest.mark.asyncio
async def test_right_key_id_wrong_secret_is_401(app):
    """A7 (adversarial) — knowing the public key id buys nothing."""
    token = _mint_for("user-a7")
    key_id = token.split("_")[1]
    async with _client(app, token=f"archim_{key_id}_wrong-secret-entirely") as client:
        assert (await client.get("/whoami")).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "header",
    [
        "archim_abc_def",  # no scheme
        "Basic archim_abc_def",  # wrong scheme
        "bearer archim_abc_def",  # case-sensitive scheme, deliberately strict
        "Bearer eyJhbGciOiJIUzI1NiJ9.e30.x",  # a JWT — not ours, not attempted
    ],
)
async def test_non_conforming_authorization_headers_are_ignored(app, header):
    """N-adjacent — the parser is strict; a foreign scheme is not half-accepted."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/whoami", headers={"authorization": header})
    assert response.status_code == 401


# ── A8: revocation takes effect on the next call ──────────────────────


@pytest.mark.asyncio
async def test_revoked_key_is_401_on_the_next_call(app, monkeypatch):
    """A8 (adversarial) — 200, revoke, 401. No cache, no TTL, no grace period."""
    _make_account("user-a8")
    _sign_in(monkeypatch, "user-a8")

    async with _client(app, cookie=True) as client:
        created = await client.post("/api/account/keys", json={"name": "to-be-revoked"})
    token = created.json()["key"]
    key_id = created.json()["id"]

    async with _client(app, token=token) as client:
        assert (await client.get("/whoami")).status_code == 200

    async with _client(app, cookie=True) as client:
        deleted = await client.delete(f"/api/account/keys/{key_id}")
        assert deleted.status_code == 204
        # Idempotent: a retried automation is not punished for it.
        assert (await client.delete(f"/api/account/keys/{key_id}")).status_code == 204

    async with _client(app, token=token) as client:
        assert (await client.get("/whoami")).status_code == 401

    async with _client(app, cookie=True) as client:
        listed = (await client.get("/api/account/keys")).json()
    assert listed[0]["revoked_at"] is not None, "the row must survive revocation as an audit record"


# ── A9: a key is scoped to its account ────────────────────────────────


@pytest.mark.asyncio
async def test_key_a_cannot_read_or_revoke_account_b(app, monkeypatch):
    """A9 (adversarial, cross-account) — the isolation test, both directions."""
    token_a = _mint_for("user-a", name="a-key")
    token_b = _mint_for("user-b", name="b-key")
    b_key_id = token_b.split("_")[1]

    # 1. A's credential is A's identity, never B's.
    async with _client(app, token=token_a) as client:
        assert (await client.get("/whoami")).json()["user_id"] == "user-a"

    # 2. A's session cannot see B's key in the list…
    _sign_in(monkeypatch, "user-a")
    async with _client(app, cookie=True) as client:
        listed = (await client.get("/api/account/keys")).json()
        assert [row["id"] for row in listed] == [token_a.split("_")[1]]
        assert b_key_id not in [row["id"] for row in listed]

        # 3. …and cannot revoke it. 404, not 403: a 403 would confirm the id is
        #    real and owned by someone, which is an enumeration oracle.
        forbidden = await client.delete(f"/api/account/keys/{b_key_id}")
    assert forbidden.status_code == 404
    assert forbidden.json() == {"detail": "API key not found"}

    # 4. And B's key still works — the failed attempt changed nothing.
    async with _client(app, token=token_b) as client:
        assert (await client.get("/whoami")).json()["user_id"] == "user-b"


# ── The containment property: a key cannot mint a key ─────────────────


@pytest.mark.asyncio
async def test_api_key_cannot_manage_api_keys(app):
    """Adversarial (privilege containment) — a leaked key cannot issue successors.

    If it could, revoking the token an operator knows about would not end the
    compromise: the attacker would already hold a key the operator has never
    seen. 403, not 401 — the caller IS authenticated; the credential is the wrong
    kind for this surface.
    """
    token = _mint_for("user-contain")

    async with _client(app, token=token) as client:
        # It is a real, working credential on the rest of the surface…
        assert (await client.get("/whoami")).status_code == 200
        # …and refused on all three key-management verbs.
        created = await client.post("/api/account/keys", json={"name": "successor"})
        listed = await client.get("/api/account/keys")
        deleted = await client.delete(f"/api/account/keys/{token.split('_')[1]}")

    assert created.status_code == 403
    assert listed.status_code == 403
    assert deleted.status_code == 403
    assert "cannot manage" in created.json()["detail"]

    # And nothing was minted.
    session = get_session()
    try:
        assert session.query(ApiKeyRecord).filter_by(user_id="user-contain").count() == 1
    finally:
        session.close()


@pytest.mark.asyncio
async def test_key_management_without_any_credential_is_401(app):
    """The unauthenticated case stays 401, not 403 — 403 would imply an identity."""
    async with _client(app) as client:
        assert (await client.post("/api/account/keys", json={"name": "x"})).status_code == 401
        assert (await client.get("/api/account/keys")).status_code == 401


# ── A12: no key material in logs ──────────────────────────────────────


@pytest.mark.asyncio
async def test_no_key_material_reaches_the_logs(app, monkeypatch, caplog):
    """A12 (adversarial, leak) — drive the full lifecycle with DEBUG capture on."""
    _make_account("user-log")
    _sign_in(monkeypatch, "user-log")

    with caplog.at_level(logging.DEBUG):
        async with _client(app, cookie=True) as client:
            created = await client.post("/api/account/keys", json={"name": "logged"})
        token = created.json()["key"]
        secret = token.split("_", 2)[2]
        key_id = created.json()["id"]

        async with _client(app, token=token) as client:
            await client.get("/whoami")
        async with _client(app, token="archim_0000000000000000_wrong") as client:
            await client.get("/whoami")  # the failure path logs too
        async with _client(app, cookie=True) as client:
            await client.get("/api/account/keys")
            await client.delete(f"/api/account/keys/{key_id}")

    assert token not in caplog.text, "a log record contains the full token"
    assert secret not in caplog.text, "a log record contains the secret"
    # The public id IS logged, deliberately — that is how an operator ties an
    # audit line to a row without the line being a credential.
    assert key_id in caplog.text


# ── Fail-closed corners ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_key_whose_account_no_longer_exists_fails_closed(app):
    """A credential with no account behind it is not an identity."""
    token = _mint_for("user-gone")
    session = get_session()
    try:
        session.query(AuthUser).filter_by(id="user-gone").delete()
        session.commit()
    finally:
        session.close()

    async with _client(app, token=token) as client:
        assert (await client.get("/whoami")).status_code == 401


@pytest.mark.asyncio
async def test_database_failure_during_key_resolution_is_401_not_500(app, monkeypatch):
    """A broken datastore must refuse the request, never authenticate it and
    never 500 — the same fail-closed posture the cookie path already has."""
    monkeypatch.setattr(api_key_auth, "verify", MagicMock(side_effect=RuntimeError("db down")))
    async with _client(app, token="archim_abcdef0123456789_whatever") as client:
        response = await client.get("/whoami")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_live_key_ceiling_is_enforced_and_revoking_frees_a_slot(app, monkeypatch):
    """An account cannot mint an unbounded set of credentials someone then has
    to hunt down one at a time."""
    _make_account("user-cap")
    _sign_in(monkeypatch, "user-cap")

    async with _client(app, cookie=True) as client:
        ids = []
        for i in range(MAX_KEYS_PER_ACCOUNT):
            response = await client.post("/api/account/keys", json={"name": f"k{i}"})
            assert response.status_code == 201
            ids.append(response.json()["id"])

        over = await client.post("/api/account/keys", json={"name": "one-too-many"})
        assert over.status_code == 409
        assert over.json()["detail"]["reason"] == "api_key_limit_reached"

        assert (await client.delete(f"/api/account/keys/{ids[0]}")).status_code == 204
        assert (await client.post("/api/account/keys", json={"name": "now-there-is-room"})).status_code == 201


@pytest.mark.asyncio
async def test_blank_name_is_rejected(app, monkeypatch):
    _make_account("user-name")
    _sign_in(monkeypatch, "user-name")
    async with _client(app, cookie=True) as client:
        assert (await client.post("/api/account/keys", json={"name": "   "})).status_code == 422
        assert (await client.post("/api/account/keys", json={"name": "x" * 65})).status_code == 422


@pytest.mark.asyncio
async def test_last_used_is_recorded_and_coarsened(app):
    """``last_used_at`` answers "is this key still live / when did the leak start"
    at minute resolution, without an UPDATE on every authenticated request."""
    token = _mint_for("user-touch")
    key_id = token.split("_")[1]

    async with _client(app, token=token) as client:
        await client.get("/whoami")

    session = get_session()
    try:
        first = session.get(ApiKeyRecord, key_id).last_used_at
        assert first is not None
    finally:
        session.close()

    async with _client(app, token=token) as client:
        await client.get("/whoami")

    session = get_session()
    try:
        assert session.get(ApiKeyRecord, key_id).last_used_at == first, "a second call inside the minute re-wrote"
    finally:
        session.close()


# ── A11: the classifier learns a distinct type ────────────────────────


def test_keyed_caller_classifies_as_agent_with_its_own_type():
    """A11 — and the funnel vocabulary was extended, not left to silently drop it."""
    from archimedes.api.telemetry_middleware import classify_request
    from archimedes.services.funnel_store import AGENT_TYPES
    from starlette.requests import Request as StarletteRequest

    def _request(credential: str | None, user: CurrentUser | None, ua: str = "curl/8.0") -> StarletteRequest:
        request = StarletteRequest(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/x",
                "headers": [(b"user-agent", ua.encode())],
                "query_string": b"",
            }
        )
        request.state.current_user = user
        request.state.auth_credential = credential
        return request

    user = CurrentUser(id="u", name="u", email="u@example.test", email_verified=True)

    assert classify_request(_request("api_key", user)) == (True, "keyed")
    # The pre-existing verdicts are untouched.
    assert classify_request(_request("session", user)) == (False, "human")
    assert classify_request(_request(None, None)) == (True, "external")
    assert classify_request(_request(None, None, ua="Mozilla/5.0")) == (False, "human")

    assert "keyed" in AGENT_TYPES, "funnel_store would silently drop keyed traffic from the breakdown"


@pytest.mark.asyncio
async def test_keyed_request_is_tagged_as_an_agent_end_to_end():
    """A11 — through the real middleware stack, not just the pure function.

    Before this lane, the #1653 finding held: an authenticated agent was
    indistinguishable from a human because "session ⇒ human" fired first. A keyed
    caller now carries ``X-Telemetry-Agent: true``.

    Middleware order matters and mirrors ``main.py``: ``app.middleware`` inserts
    at position 0, so the LAST registration is the OUTERMOST. Identity must be
    resolved before telemetry reads it, so identity is registered last here — as
    it is in ``main.py``, where ``better_auth_session_middleware`` is the final
    registration.
    """
    from archimedes.api.telemetry_middleware import telemetry_middleware

    token = _mint_for("user-telemetry")

    application = FastAPI()
    application.middleware("http")(telemetry_middleware)
    application.middleware("http")(account_auth.better_auth_session_middleware)

    @application.get("/whoami")
    async def whoami(user: CurrentUser = Depends(require_current_user)):
        return {"user_id": user.id}

    store = MagicMock()
    store.increment_agent = AsyncMock()
    store.increment_human = AsyncMock()
    store.close = AsyncMock()

    with patch("archimedes.services.telemetry_store.TelemetryStore", return_value=store):
        async with _client(application, token=token) as client:
            response = await client.get("/whoami")

    assert response.status_code == 200
    assert response.headers["X-Telemetry-Agent"] == "true"
    store.increment_agent.assert_awaited_once()
    store.increment_human.assert_not_awaited()
