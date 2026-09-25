"""Regression for cross-user job cancellation (audit 2026-06-14).

POST /api/generate/jobs/{job_id}/cancel had no caller scoping — anyone who
learned a job_id could cancel another user's in-flight generation. Jobs are now
tagged with canonical user ownership; legacy wallet-owned jobs remain readable
only through a verified link. Every cancellation now requires an account.

Hermetic: the Redis-backed job store is mocked at the boundary; SIWE sessions
are real signed cookies (test_user_routes precedent).
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

from archimedes.api.auth_siwe import _COOKIE_NAME, _sign_session
from fastapi.testclient import TestClient

_OWNER = "0xAb5801a7D398351b8bE11C439e05C5B3259aeC9B"
_ATTACKER = "0x9999999999999999999999999999999999999999"


def _cookies(wallet: str) -> dict[str, str]:
    return {_COOKIE_NAME: _sign_session(wallet, time.time())}


def _client() -> TestClient:
    from archimedes.main import app

    return TestClient(app)


def _mock_store(owner_wallet: str | None):
    """A job store whose .get returns a queued job owned by owner_wallet."""
    store = MagicMock()
    store.get = AsyncMock(
        return_value={
            "id": "job123",
            "status": "queued",
            "payload": {"owner_wallet": owner_wallet},
        }
    )
    store.update_status = AsyncMock()
    store.push_event = AsyncMock()
    # The durable cross-process cancel flag (#1667) — cancel_job now refuses to
    # report "cancelled" until this write is confirmed. Behaviour when it is
    # NOT confirmed is covered in test_generate_cancel_cross_process.py.
    store.request_cancel = AsyncMock(return_value=True)
    return store


def test_owner_can_cancel_own_job():
    with patch("archimedes.api.generate_routes.get_job_store", return_value=_mock_store(_OWNER.lower())):
        resp = _client().post("/api/generate/jobs/job123/cancel", cookies=_cookies(_OWNER))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"


def test_other_wallet_cannot_cancel_owned_job():
    with patch("archimedes.api.generate_routes.get_job_store", return_value=_mock_store(_OWNER.lower())):
        resp = _client().post("/api/generate/jobs/job123/cancel", cookies=_cookies(_ATTACKER))
    assert resp.status_code == 404, resp.text


def test_anonymous_cannot_cancel_owned_job():
    """No account can never cancel a wallet-owned job."""
    with patch("archimedes.api.generate_routes.get_job_store", return_value=_mock_store(_OWNER.lower())):
        resp = _client().post("/api/generate/jobs/job123/cancel")
    assert resp.status_code == 401, resp.text


def test_legacy_ownerless_job_still_requires_account():
    """Ownerless historical jobs never restore anonymous cancellation."""
    with patch("archimedes.api.generate_routes.get_job_store", return_value=_mock_store(None)):
        resp = _client().post("/api/generate/jobs/job123/cancel")
    assert resp.status_code == 401, resp.text
