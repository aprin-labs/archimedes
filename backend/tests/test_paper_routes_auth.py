"""Paper routes are account-owned (#1194 cutover): the auth-swap contract.

Post-#1194, account-first users never hold a SIWE session, so these routes
gate on the Better Auth session and own rows by ``owner_user_id``. The linked
wallet is provenance only. Pinned here:

  1. every route 401s without a session (including /marks — a new route on
     this router inherits the gate by construction, but "inherits" is a claim
     and this file's job is to check claims);
  2. deploy works for an account with NO linked wallet — the exact user the
     swap exists for;
  3. ownership is user-scoped: another signed-in user gets 404 (existence is
     private) and an empty list;
  4. owner_user_id is stamped and the linked wallet, when present, is recorded
     as provenance.

Hermetic: tmp sqlite via redirect_to_tmp_sqlite; Better Auth's session fetch
and the spec/advance machinery are stubbed at their boundaries.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from archimedes.api import account_auth, paper_routes
from fastapi import FastAPI

from tests.db_isolation import redirect_to_tmp_sqlite

_SPEC = {
    "name": "route probe",
    "asset_universe": ["SPY"],
    "rebalance_frequency": "daily",
    "entry": {"gt": ["momentum_20", 0]},
    "exit": {"lt": ["momentum_20", -0.99]},
    "position_sizing": {"type": "full_invested_when_in_market"},
    "source_arxiv_ids": ["0706.1497"],
    "look_ahead_safe": True,
}


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path):
    yield from redirect_to_tmp_sqlite(tmp_path)


@pytest.fixture()
def app(monkeypatch):
    application = FastAPI()
    application.middleware("http")(account_auth.better_auth_session_middleware)
    application.include_router(paper_routes.paper_router)
    # Boundary stubs: strategy lookup/visibility and the panel-driven first
    # advance are their own tested machinery — not this file's subject.
    monkeypatch.setattr(paper_routes, "_spec_for_strategy", lambda *_a, **_k: dict(_SPEC))
    monkeypatch.setattr(paper_routes, "advance_deployment", lambda *_a, **_k: {"appended": 0, "drift": 0})
    return application


def _session_for(user_id: str):
    async def fetch(_request):
        return {
            "user": {"id": user_id, "name": user_id, "email": f"{user_id}@example.com", "emailVerified": True},
            "session": {"id": f"s-{user_id}", "expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
        }

    return fetch


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"cookie": "better-auth.session_token=opaque", "host": "archimedes-arc.com"},
    )


def _sign_in(monkeypatch, user_id: str):
    monkeypatch.setattr(account_auth, "_fetch_session", _session_for(user_id))


@pytest.mark.asyncio
async def test_every_route_requires_a_better_auth_session(app, monkeypatch):
    monkeypatch.setattr(account_auth, "_fetch_session", AsyncMock(return_value=None))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/api/paper/deployments", json={"strategy_id": "s1"})).status_code == 401
        assert (await client.get("/api/paper/deployments")).status_code == 401
        assert (await client.get("/api/paper/deployments/anything")).status_code == 401
        assert (await client.get("/api/paper/deployments/anything/marks")).status_code == 401
        assert (await client.post("/api/paper/deployments/anything/stop")).status_code == 401


@pytest.mark.asyncio
async def test_account_only_user_with_no_linked_wallet_can_deploy(app, monkeypatch):
    """The motivating case: a signed-in user who never linked a wallet. A
    SIWE-gated implementation 401s here; the account-owned one succeeds."""
    _sign_in(monkeypatch, "user-nowallet")
    monkeypatch.setattr(paper_routes, "get_linked_wallet_address", lambda _r: None)

    async with _client(app) as client:
        created = await client.post("/api/paper/deployments", json={"strategy_id": "s1"})
        assert created.status_code == 201
        listed = await client.get("/api/paper/deployments")
        assert listed.status_code == 200
        assert len(listed.json()["deployments"]) == 1

    from archimedes.db import get_session
    from archimedes.models.paper_store import PaperDeployment

    with get_session() as session:
        dep = session.query(PaperDeployment).one()
        assert dep.owner_user_id == "user-nowallet"
        assert dep.owner_wallet is None  # provenance honestly absent


@pytest.mark.asyncio
async def test_ownership_is_user_scoped_and_existence_is_private(app, monkeypatch):
    _sign_in(monkeypatch, "user-a")
    monkeypatch.setattr(
        paper_routes, "get_linked_wallet_address", lambda _r: "0xAbCd000000000000000000000000000000000001"
    )

    async with _client(app) as client:
        created = await client.post("/api/paper/deployments", json={"strategy_id": "s1"})
        assert created.status_code == 201
        dep_id = created.json()["deployment_id"]

        # Owner sees and can stop it.
        assert (await client.get(f"/api/paper/deployments/{dep_id}")).status_code == 200

    # A DIFFERENT signed-in user: 404 on get/stop (never 403 — existence is
    # private), and an empty list. A wallet match could not help them even if
    # they linked the same address — ownership is owner_user_id only.
    _sign_in(monkeypatch, "user-b")
    async with _client(app) as client:
        assert (await client.get(f"/api/paper/deployments/{dep_id}")).status_code == 404
        assert (await client.get(f"/api/paper/deployments/{dep_id}/marks")).status_code == 404
        assert (await client.post(f"/api/paper/deployments/{dep_id}/stop")).status_code == 404
        assert (await client.get("/api/paper/deployments")).json()["deployments"] == []

    _sign_in(monkeypatch, "user-a")
    async with _client(app) as client:
        stopped = await client.post(f"/api/paper/deployments/{dep_id}/stop")
        assert stopped.status_code == 200
        assert stopped.json()["status"] == "stopped"

    from archimedes.db import get_session
    from archimedes.models.paper_store import PaperDeployment

    with get_session() as session:
        dep = session.query(PaperDeployment).one()
        assert dep.owner_user_id == "user-a"
        assert dep.owner_wallet == "0xabcd000000000000000000000000000000000001"  # provenance, lowercased
