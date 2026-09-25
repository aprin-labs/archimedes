"""A key is a credential, never a bypass — proved on the real gates (#1680 A10/N1/N2).

The design claim behind the API-key lane is that the credential stops existing as
a distinction at ``api/account_auth.py``, so every gate downstream applies to a
keyed caller identically and *without any gate knowing keys exist*. That claim is
worthless as prose: the whole failure mode of a machine credential is that someone
adds ``if api_key: skip`` to a paywall six months later and nobody notices.

So this file drives the **live** ``archimedes.main.app`` through the real
``POST /api/generate/start`` twice — once with a session cookie, once with a
bearer key — and asserts the two are byte-identical where it matters, then pins
the structural property that makes it so.

Hermetic, and the same harness as ``test_generate_payment_gate.py``: mocked job
store, pipeline task closed rather than scheduled, per-test tmp SQLite.
"""

from __future__ import annotations

import inspect as py_inspect
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from archimedes.api import api_key_auth
from archimedes.db import get_session
from archimedes.models.account import AuthUser
from fastapi import HTTPException
from fastapi.testclient import TestClient

from tests.auth_helpers import auth_cookies
from tests.db_isolation import redirect_to_tmp_sqlite

RECIPIENT = "0x00000000000000000000000000000000000000a1"
PAYER = "0x" + "cd" * 20
_BODY = {"brief": {"intent": "low-vol treasury alternative", "risk_appetite": "moderate"}}


@pytest.fixture(autouse=True)
def _no_free_generations(monkeypatch):
    """Pin the free allowance to 0 so the gates these tests name stay reachable.

    #1643's free path (merged after this file was written) serves a fresh
    account's first three generations without touching the paywall or the
    wallet gate — a 202 from the free path is indistinguishable from a 202 a
    payment bought, so with the default allowance these parity tests would stop
    measuring the thing they name. Same fixture, same reason, as
    test_generate_payment_gate.py. The free path has its own file.
    """
    monkeypatch.setenv("FREE_GENERATIONS_PER_ACCOUNT", "0")


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path):
    yield from redirect_to_tmp_sqlite(tmp_path)


@pytest.fixture()
def token() -> str:
    """A real, minted key belonging to a real account row."""
    session = get_session()
    try:
        now = datetime.now(UTC)
        session.add(
            AuthUser(
                id="user-parity",
                name="parity",
                email="parity@example.test",
                email_verified=True,
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        _, minted = api_key_auth.mint(session, user_id="user-parity", name="ci")
        session.commit()
        return minted
    finally:
        session.close()


def _client() -> TestClient:
    from archimedes.main import app

    return TestClient(app)


def _mock_store() -> MagicMock:
    store = MagicMock()
    store.enqueue = AsyncMock(return_value="job-parity")
    return store


def _close_background_coroutine(coro):
    coro.close()
    return MagicMock()


def _harness(store):
    return (
        patch("archimedes.api.generate_routes.get_job_store", return_value=store),
        patch("archimedes.api.generate_routes.asyncio.create_task", side_effect=_close_background_coroutine),
        # Both callers present the same linked wallet, so the only variable left
        # between the two requests is which credential proved the identity.
        patch("archimedes.api.generate_routes.get_linked_wallet_address", return_value=PAYER),
    )


# ── A10: the paywall does not care which credential you hold ──────────


def test_keyed_call_hits_the_same_paywall_a_session_call_hits(monkeypatch, token):
    """A10 — 402, with the *same* x402 requirements, for both credentials."""
    monkeypatch.setenv("GENERATION_PAYMENT_REQUIRED", "true")
    monkeypatch.setenv("GENERATION_PAYMENT_RECIPIENT", RECIPIENT)
    monkeypatch.setenv("PAYMENTS_DRY_RUN", "false")

    store = _mock_store()
    p1, p2, p3 = _harness(store)
    with p1, p2, p3:
        by_cookie = _client().post("/api/generate/start", json=_BODY, cookies=auth_cookies())
        by_key = _client().post("/api/generate/start", json=_BODY, headers={"Authorization": f"Bearer {token}"})

    assert by_cookie.status_code == 402
    assert by_key.status_code == 402, "a key walked past the paywall a session is stopped by"

    assert "PAYMENT-REQUIRED" in by_cookie.headers
    assert "PAYMENT-REQUIRED" in by_key.headers

    cookie_detail = by_cookie.json()["detail"]
    key_detail = by_key.json()["detail"]
    assert key_detail["reason"] == cookie_detail["reason"] == "payment_required"
    # The full quote — price, asset, network, recipient — must be identical.
    assert key_detail["quote"] == cookie_detail["quote"]
    assert key_detail.get("accepts") == cookie_detail.get("accepts")

    store.enqueue.assert_not_called()


def test_keyed_call_hits_the_same_wallet_precondition(monkeypatch, token):
    """A10 — and the 409 that precedes the paywall applies equally.

    A key holder with no linked wallet is refused for exactly the reason a cookie
    holder is, with the same faucet guidance (#1294's human-only step). Nothing
    about holding a machine credential moves the money boundary.
    """
    monkeypatch.setenv("GENERATION_PAYMENT_REQUIRED", "true")
    monkeypatch.setenv("GENERATION_PAYMENT_RECIPIENT", RECIPIENT)

    store = _mock_store()
    with (
        patch("archimedes.api.generate_routes.get_job_store", return_value=store),
        patch("archimedes.api.generate_routes.asyncio.create_task", side_effect=_close_background_coroutine),
        patch("archimedes.api.generate_routes.get_linked_wallet_address", return_value=None),
    ):
        by_cookie = _client().post("/api/generate/start", json=_BODY, cookies=auth_cookies())
        by_key = _client().post("/api/generate/start", json=_BODY, headers={"Authorization": f"Bearer {token}"})

    assert by_cookie.status_code == by_key.status_code == 409
    assert by_key.json()["detail"]["reason"] == by_cookie.json()["detail"]["reason"] == "wallet_link_required"
    store.enqueue.assert_not_called()


def test_keyed_call_is_subject_to_the_daily_generation_quota(monkeypatch, token):
    """N2 — the quota runs for a keyed caller, keyed on ITS account id.

    ``TESTING`` is unset so the quota branch actually executes (the same trick
    ``test_generate_payment_gate.py::test_quota_runs_before_the_paywall`` uses),
    and the quota is stubbed to refuse. A key that skipped the quota would come
    back 202.
    """
    monkeypatch.delenv("TESTING", raising=False)
    seen: list[str] = []

    async def _blocked(request, user_id):  # signature must match the real enforce_generation_quota
        seen.append(user_id)
        raise HTTPException(status_code=429, detail="daily cap reached")

    store = _mock_store()
    p1, p2, p3 = _harness(store)
    with p1, p2, p3, patch("archimedes.api.generate_routes.enforce_generation_quota", _blocked):
        response = _client().post("/api/generate/start", json=_BODY, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 429
    assert seen == ["user-parity"], "the quota was not keyed on the key's own account"
    store.enqueue.assert_not_called()


def test_a_bad_key_never_reaches_the_gates_at_all(monkeypatch):
    """A7 on the live app — an unauthenticated caller is 401 at the router-level
    ``require_current_user``, before any quota, paywall or queue admission."""
    monkeypatch.setenv("GENERATION_PAYMENT_REQUIRED", "true")
    store = _mock_store()
    p1, p2, p3 = _harness(store)
    with p1, p2, p3:
        response = _client().post(
            "/api/generate/start",
            json=_BODY,
            headers={"Authorization": "Bearer archim_0000000000000000_never-minted"},
        )
    assert response.status_code == 401
    store.enqueue.assert_not_called()


# ── N1: no gate learned the word "key" ────────────────────────────────


def test_no_gate_module_knows_that_api_keys_exist():
    """N1/N2, structurally — the property that keeps A10 true next quarter.

    A10 proves today's behaviour; this proves there is *no place to put* a bypass
    without it being obvious. If a future change makes a gate branch on the
    credential, this fails and the reviewer is forced to justify it in the diff
    rather than in a comment nobody reads.

    Deliberately narrow: it checks the four modules that actually gate access, not
    the whole tree, and it looks for the credential vocabulary, not the word
    "key" (which legitimately appears as ``Idempotency-Key``, dict keys, and
    ``INTERNAL_AGENT_API_KEY``).
    """
    from archimedes.api import generate_routes
    from archimedes.services import generation_payment, generation_quota

    forbidden = ("api_key_auth", "auth_credential", "CREDENTIAL_API_KEY", "archim_")
    for module in (generate_routes, generation_payment, generation_quota):
        source = py_inspect.getsource(module)
        for needle in forbidden:
            assert needle not in source, (
                f"{module.__name__} references {needle!r} — a gate that can tell credentials "
                "apart is a gate that can be made to skip one of them"
            )
