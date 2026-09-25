"""``GET /api/paper/deployments/{id}/marks`` — the intraday read surface.

Ownership is checked in ``test_paper_routes_auth.py`` alongside every other
route on this router (401 with no session, 404 for someone else's deployment —
existence is private). What this file pins is the SHAPE and the ordering, both
of which a client's honesty depends on:

  1. marks come back OLDEST FIRST, and ``latest`` is the newest of them;
  2. a ``limit`` smaller than the history returns the most RECENT window, not
     the oldest — the opposite would hand back ancient history and no live
     value, which is the one thing this endpoint exists to provide;
  3. the honesty columns survive the round trip: ``ts`` is the upstream
     observation time, ``prices`` is what was actually observed, and
     ``is_delayed``/``source`` are read from the row rather than inferred;
  4. an empty history is a normal 200 with an empty list and ``latest: null``
     — never a 404 and never a fabricated zero.

Hermetic: tmp sqlite via redirect_to_tmp_sqlite; the Better Auth session fetch
and the spec/advance machinery are stubbed at their boundaries, exactly as
``test_paper_routes_auth.py`` does.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from archimedes.api import account_auth, paper_routes
from fastapi import FastAPI

from tests.db_isolation import redirect_to_tmp_sqlite

_SPEC = {
    "name": "marks route probe",
    "asset_universe": ["SPY"],
    "rebalance_frequency": "daily",
    "entry": {"gt": ["momentum_20", 0]},
    "exit": {"lt": ["momentum_20", -0.99]},
    "position_sizing": {"type": "full_invested_when_in_market"},
    "source_arxiv_ids": ["0706.1497"],
    "look_ahead_safe": True,
}

_T0 = datetime(2026, 8, 30, 14, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path):
    yield from redirect_to_tmp_sqlite(tmp_path)


@pytest.fixture()
def app(monkeypatch):
    application = FastAPI()
    application.middleware("http")(account_auth.better_auth_session_middleware)
    application.include_router(paper_routes.paper_router)
    monkeypatch.setattr(paper_routes, "_spec_for_strategy", lambda *_a, **_k: dict(_SPEC))
    monkeypatch.setattr(paper_routes, "advance_deployment", lambda *_a, **_k: {"appended": 0, "drift": 0})

    async def _session(_request):
        return {
            "user": {"id": "u1", "name": "u1", "email": "u1@example.com", "emailVerified": True},
            "session": {"id": "s-u1", "expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
        }

    monkeypatch.setattr(account_auth, "_fetch_session", _session)
    monkeypatch.setattr(paper_routes, "get_linked_wallet_address", lambda _r: None)
    return application


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"cookie": "better-auth.session_token=opaque", "host": "archimedes-arc.com"},
    )


def _seed_marks(deployment_id: str, n: int) -> None:
    from archimedes.db import get_session
    from archimedes.models.paper_store import PaperMark

    with get_session() as session:
        for i in range(n):
            session.add(
                PaperMark(
                    deployment_id=deployment_id,
                    ts=_T0 + timedelta(minutes=15 * i),
                    prices_json=f'{{"SPY": {500.0 + i}}}',
                    portfolio_value=1.0 + i / 1000,
                    source="yfinance",
                    is_delayed=True,
                    granularity="raw",
                )
            )
        session.commit()


async def _deploy(client) -> str:
    created = await client.post("/api/paper/deployments", json={"strategy_id": "s1"})
    assert created.status_code == 201
    return created.json()["deployment_id"]


@pytest.mark.asyncio
async def test_marks_come_back_oldest_first_with_latest_pointing_at_the_newest(app):
    async with _client(app) as client:
        dep_id = await _deploy(client)
        _seed_marks(dep_id, 5)

        body = (await client.get(f"/api/paper/deployments/{dep_id}/marks")).json()

        assert [m["ts"] for m in body["marks"]] == sorted(m["ts"] for m in body["marks"])
        assert body["latest"] == body["marks"][-1]
        assert body["latest"]["portfolio_value"] == pytest.approx(1.004)


@pytest.mark.asyncio
async def test_a_small_limit_returns_the_most_recent_window_not_the_oldest(app):
    """The ordering trap: returning the OLDEST ``limit`` rows would satisfy
    "oldest first" while giving a live-value widget nothing live to show."""
    async with _client(app) as client:
        dep_id = await _deploy(client)
        _seed_marks(dep_id, 10)

        body = (await client.get(f"/api/paper/deployments/{dep_id}/marks?limit=3")).json()

        assert len(body["marks"]) == 3
        assert body["marks"][-1]["portfolio_value"] == pytest.approx(1.009)  # the 10th, not the 3rd
        assert body["marks"][0]["portfolio_value"] == pytest.approx(1.007)


@pytest.mark.asyncio
async def test_the_honesty_columns_survive_the_round_trip(app):
    async with _client(app) as client:
        dep_id = await _deploy(client)
        _seed_marks(dep_id, 1)

        mark = (await client.get(f"/api/paper/deployments/{dep_id}/marks")).json()["latest"]

        assert mark["ts"].startswith("2026-08-30T14:00:00")
        assert mark["ts"].endswith("+00:00"), "a bare timestamp with no offset is not an as-of claim a client can trust"
        assert mark["source"] == "yfinance"
        assert mark["is_delayed"] is True
        assert mark["granularity"] == "raw"
        assert mark["prices"] == {"SPY": 500.0}


@pytest.mark.asyncio
async def test_no_marks_yet_is_a_normal_200_not_a_404_and_not_a_zero(app):
    """A deployment created between ticks legitimately has zero marks. The
    endpoint says so plainly; inventing a 0.0 here is what would let the card
    render a measured-looking +0.00% for something never measured."""
    async with _client(app) as client:
        dep_id = await _deploy(client)

        res = await client.get(f"/api/paper/deployments/{dep_id}/marks")

        assert res.status_code == 200
        assert res.json() == {"deployment_id": dep_id, "marks": [], "latest": None}


@pytest.mark.asyncio
async def test_the_deployment_summary_carries_the_latest_mark_for_the_list_view(app):
    """So the ledger list can render a live value per card without N extra
    round trips — and so a card is never left with a settled figure and no
    live one merely because a second request had not landed yet."""
    async with _client(app) as client:
        dep_id = await _deploy(client)
        _seed_marks(dep_id, 3)

        listed = (await client.get("/api/paper/deployments")).json()["deployments"]

        assert listed[0]["latest_mark"]["portfolio_value"] == pytest.approx(1.002)
        # Never folded into the settled figure.
        assert listed[0]["total_return"] == 0.0
        assert listed[0]["days"] == 0
