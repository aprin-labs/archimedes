"""HTTP-layer tests for /api/metrics + the identity-ledger metrics views (issue #428, #1028).

Mocks the Redis boundary (``TelemetryStore.get_counts_or_none``) so the test is
hermetic — no live Redis. Asserts the response shape, the derived total, and
(issue #1028) the D8 Postgres-snapshot reconciliation + the new
identity-ledger-backed endpoints.

The ``request_count_snapshots`` singleton row is shared process-wide (same
"one bound engine per pytest run" caveat as every other DB-backed test in this
suite — see ``test_api_routes.py``'s ``_use_tmp_db`` docstring), so every test
that cares about its exact value resets it first via ``_reset_request_snapshot``
rather than assuming a fresh row.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient


def _reset_request_snapshot() -> None:
    """Delete the singleton request_count_snapshots row so a test starts from zero.

    Direct-DB reset (not a DATABASE_URL monkeypatch) because the engine this
    process uses was bound at first import — see the module docstring.
    """
    from archimedes.db import get_session
    from archimedes.models.request_snapshot import SNAPSHOT_ROW_ID, RequestCountSnapshot

    session = get_session()
    try:
        session.query(RequestCountSnapshot).filter(RequestCountSnapshot.id == SNAPSHOT_ROW_ID).delete()
        session.commit()
    finally:
        session.close()


@pytest.mark.asyncio
async def test_metrics_endpoint_returns_counts_and_total():
    from archimedes.main import app

    _reset_request_snapshot()
    with patch(
        "archimedes.services.telemetry_store.TelemetryStore.get_counts_or_none",
        new=AsyncMock(return_value=(7, 3)),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/metrics")

    assert resp.status_code == 200
    data = resp.json()
    assert data["human_count"] == 7
    assert data["agent_count"] == 3
    assert data["total_requests"] == 10
    # D8: a fresh snapshot (no prior resets) reports epoch metadata, not None.
    assert data["epoch_started_at"] is not None
    assert data["epoch_resets"] == 0
    assert isinstance(data["timestamp"], str) and data["timestamp"]


@pytest.mark.asyncio
async def test_metrics_endpoint_shape_keys_present():
    from archimedes.main import app

    _reset_request_snapshot()
    with patch(
        "archimedes.services.telemetry_store.TelemetryStore.get_counts_or_none",
        new=AsyncMock(return_value=(0, 0)),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/metrics")

    assert resp.status_code == 200
    data = resp.json()
    for key in ("human_count", "agent_count", "total_requests", "real_users", "timestamp"):
        assert key in data, f"missing {key} in {list(data.keys())}"
    # Zero state is well-formed, not an error.
    assert data["total_requests"] == 0


@pytest.mark.asyncio
async def test_metrics_real_users_is_null_not_zero_on_account_count_failure():
    """Round 4 fix: a DB error reading the account count must render as an
    honest null, not a fabricated 0 that looks like a real, measured "zero
    real users" — the same fail-soft violation CLAUDE.md's claims-must-be-true
    section names, and the same class of bug engagement_metrics.py's round-2
    fix already closed for the adjacent "Accounts (total)" tile.

    Mutation-verified: reverting metrics_routes.get_metrics's
    get_distinct_user_count_or_none() call back to get_distinct_user_count()
    makes this assertion fail (`assert 0 is None` -> real_users reads 0).
    """
    from archimedes.main import app
    from archimedes.services import user_stats

    _reset_request_snapshot()
    user_stats._reset_cache()
    with (
        patch(
            "archimedes.services.telemetry_store.TelemetryStore.get_counts_or_none",
            new=AsyncMock(return_value=(0, 0)),
        ),
        patch("archimedes.api.metrics_routes.get_distinct_user_count_or_none", return_value=None),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/metrics")

    assert resp.status_code == 200
    data = resp.json()
    assert data["real_users"] is None


@pytest.mark.asyncio
async def test_metrics_endpoint_falls_back_to_snapshot_when_redis_down():
    """When Redis is unreachable, the durable Postgres snapshot is served, not a false zero (D8/AC6).

    On a freshly-reset snapshot (no prior successful read), that floor is
    itself zero — so this also exercises the "genuinely nothing recorded yet"
    path and confirms it degrades to 0, not an error.
    """
    from archimedes.main import app
    from archimedes.services.telemetry_store import TelemetryStore

    _reset_request_snapshot()
    # Patch the underlying client accessor to fail; get_counts_or_none swallows -> None.
    with patch.object(
        TelemetryStore,
        "_get_redis",
        new=AsyncMock(side_effect=ConnectionError("redis down")),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/metrics")

    assert resp.status_code == 200
    data = resp.json()
    assert data["human_count"] == 0
    assert data["agent_count"] == 0
    assert data["total_requests"] == 0


@pytest.mark.asyncio
async def test_metrics_survives_a_redis_reset_without_regressing():
    """D8/AC6: a Redis restart (counters drop back to a small number) must not zero the lifetime total.

    First read establishes a baseline (100 human / 20 agent). Second read
    simulates Redis having been restarted (2 human / 1 agent — lower than
    before) — the reconciled total must be the OLD values plus the NEW ones,
    never just the new (post-reset) reading alone.
    """
    from archimedes.main import app

    _reset_request_snapshot()
    with patch(
        "archimedes.services.telemetry_store.TelemetryStore.get_counts_or_none",
        new=AsyncMock(return_value=(100, 20)),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            first = await client.get("/api/metrics")
    assert first.json()["human_count"] == 100
    assert first.json()["agent_count"] == 20
    assert first.json()["epoch_resets"] == 0

    with patch(
        "archimedes.services.telemetry_store.TelemetryStore.get_counts_or_none",
        new=AsyncMock(return_value=(2, 1)),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            second = await client.get("/api/metrics")

    data = second.json()
    # Never regress: 100 + 2, not just 2.
    assert data["human_count"] == 102
    assert data["agent_count"] == 21
    assert data["epoch_resets"] == 1


@pytest.mark.asyncio
async def test_old_public_wallet_roster_paths_are_gone():
    """#1366 guard demo, part 1: the roster endpoints served the complete
    per-wallet user list (addresses + first/last-seen) to anonymous callers in
    production. They are not merely gated — they are OFF the public surface
    entirely (moved to the admin router). The exact requests that leaked now
    404 with no roster in the body."""
    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for path in ("/api/metrics/wallets", "/api/metrics/wallets/connections"):
            resp = await client.get(path)
            assert resp.status_code == 404, path
            body = resp.json()
            assert "wallets" not in body and "connections" not in body


def _wallet_bearing_fields(model, prefix: str = "", seen: frozenset = frozenset()) -> list[str]:
    """Collect dotted paths of every field named ``*wallet*`` in ``model``,
    RECURSING into nested pydantic models (including through list/dict/Optional
    annotations). The re-review of #1373 showed a flat top-level walk is a
    false-negative guard: WalletConnectionsResponse's own field names are
    ``count``/``connections``/``timestamp`` — the per-wallet address lives one
    level down in WalletConnectionOut.wallet, so only recursion catches a
    re-mounted connections roster."""
    import typing

    from pydantic import BaseModel

    if not (isinstance(model, type) and issubclass(model, BaseModel)) or model in seen:
        return []
    seen = seen | {model}
    found: list[str] = []
    for field_name, field in model.model_fields.items():
        path = f"{prefix}{model.__name__}.{field_name}"
        if "wallet" in field_name.lower():
            found.append(path)
        stack = [field.annotation]
        while stack:
            ann = stack.pop()
            if isinstance(ann, type) and issubclass(ann, BaseModel):
                found.extend(_wallet_bearing_fields(ann, prefix=f"{path} -> ", seen=seen))
            else:
                stack.extend(typing.get_args(ann))
    return found


@pytest.mark.asyncio
async def test_no_public_metrics_route_carries_a_wallet_response_model():
    """#1366 AC2 regression guard: no route on the public ``metrics_router`` may
    declare a response model with a wallet-bearing field AT ANY DEPTH. Walks the
    router so the SAME drift (mounting an identity roster on the anonymous
    traction instrument) cannot recur silently."""
    from archimedes.api.metrics_routes import metrics_router

    offending: list[str] = []
    for route in metrics_router.routes:
        model = getattr(route, "response_model", None)
        if model is None:
            continue
        for hit in _wallet_bearing_fields(model):
            offending.append(f"{route.path} -> {hit}")
    assert offending == [], f"public metrics routes expose wallet fields: {offending}"


def test_the_walk_guard_catches_both_roster_models():
    """Negative control for the guard itself (a guard must be shown to reject
    something): mounting either roster model on a throwaway router IS flagged —
    including WalletConnectionsResponse, whose wallet field is nested one level
    down and which a flat top-level walk provably missed (#1373 re-review)."""
    from fastapi import APIRouter

    from archimedes.models.telemetry import WalletConnectionsResponse, WalletsResponse

    throwaway = APIRouter()

    @throwaway.get("/drifted-roster", response_model=WalletsResponse)
    async def _a():  # pragma: no cover - never called
        raise NotImplementedError

    @throwaway.get("/drifted-connections", response_model=WalletConnectionsResponse)
    async def _b():  # pragma: no cover - never called
        raise NotImplementedError

    flagged: dict[str, list[str]] = {}
    for route in throwaway.routes:
        model = getattr(route, "response_model", None)
        if model is not None:
            flagged[route.path] = _wallet_bearing_fields(model)

    assert flagged["/drifted-roster"], "top-level wallets field must be flagged"
    assert flagged["/drifted-connections"], (
        "the NESTED WalletConnectionOut.wallet field must be flagged — a flat "
        "top-level walk misses it, which is exactly the false negative this "
        "control exists to prevent"
    )


# ── The roster endpoints on their new ADMIN paths (#1366 six-case minimum:
# both endpoints x {401 anonymous, 403 non-admin, 200 admin-shape}) ──────

_ADMIN = "0xadmin000000000000000000000000000000000001"
_NON_ADMIN = "0xnotadmin0000000000000000000000000000000002"


def _override_signed_in_account(app, wallet):
    """Sign a request in as a canonical account whose primary wallet is `wallet`.

    Overrides the IDENTITY dependency (``require_current_user``), not the admin
    decision — so the real ``require_platform_admin`` still runs and really
    decides. Since #1648 admin is keyed on the account, so these tests pair
    this with ``PLATFORM_ADMIN_ACCOUNTS`` set (or not set) to the returned
    account id; that also keeps them free of any account-store read, which is
    what let the pre-#1648 version override ``require_linked_wallet`` instead.
    """
    from archimedes.api.account_auth import CurrentUser, require_current_user

    user = CurrentUser(
        id=f"acct-for:{wallet}",
        name="metrics-route test account",
        email=f"{wallet[2:10]}@example.test",
        email_verified=True,
    )
    app.dependency_overrides[require_current_user] = lambda: user
    return require_current_user, user.id


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/metrics/private/wallets", "/api/metrics/private/wallets/connections"])
async def test_private_roster_unauthenticated_is_401(path):
    """#1366 guard demo, part 2: anonymous requests to the moved endpoints are
    rejected by the router-level admin dependency before any query runs."""
    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(path)

    assert resp.status_code == 401
    body = resp.json()
    assert "wallets" not in body and "connections" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/metrics/private/wallets", "/api/metrics/private/wallets/connections"])
async def test_private_roster_verified_non_admin_is_403(path, monkeypatch):
    """A signed-in account that is not a platform admin gets 403 — any
    authenticated user being able to enumerate every other user would still be
    the leak, just behind a signup."""
    from archimedes.main import app

    monkeypatch.setenv("PLATFORM_ADMIN_WALLETS", _ADMIN)
    monkeypatch.delenv("PLATFORM_ADMIN_ACCOUNTS", raising=False)
    dep, _account_id = _override_signed_in_account(app, _NON_ADMIN)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(path)
    finally:
        app.dependency_overrides.pop(dep, None)

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_private_wallets_admin_shape_and_boundary(monkeypatch):
    """The admin path keeps issue #1028 AC1's response shape unchanged.

    Mocks the identity-ledger boundary (consistent with how this file already
    mocks the Redis/user-count boundaries) so the assertion is deterministic
    regardless of what other tests have written into the shared DB.
    """
    from archimedes.main import app

    monkeypatch.setenv("PLATFORM_ADMIN_WALLETS", _ADMIN)
    dep, account_id = _override_signed_in_account(app, _ADMIN)
    monkeypatch.setenv("PLATFORM_ADMIN_ACCOUNTS", account_id)

    fake_wallets = [
        {
            "wallet_address": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "actor_class": "human",
            "first_seen_at": "2026-07-01T00:00:00+00:00",
            "last_auth_at": "2026-07-02T00:00:00+00:00",
        }
    ]
    try:
        with (
            patch("archimedes.api.metrics_private_routes.count_human_wallets", return_value=1),
            patch("archimedes.api.metrics_private_routes.list_human_wallets", return_value=fake_wallets),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get("/api/metrics/private/wallets")
    finally:
        app.dependency_overrides.pop(dep, None)

    assert resp.status_code == 200
    data = resp.json()
    assert data["real_users"] == 1
    assert len(data["wallets"]) == 1
    assert data["wallets"][0]["wallet_address"] == "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


@pytest.mark.asyncio
async def test_private_wallet_connections_admin_shape(monkeypatch):
    """Admin path keeps issue #1028 AC2's response shape unchanged."""
    from archimedes.main import app

    monkeypatch.setenv("PLATFORM_ADMIN_WALLETS", _ADMIN)
    dep, account_id = _override_signed_in_account(app, _ADMIN)
    monkeypatch.setenv("PLATFORM_ADMIN_ACCOUNTS", account_id)

    fake_connections = [
        {"wallet": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "connected_at": "2026-07-01T00:00:00+00:00"},
    ]
    try:
        with patch("archimedes.api.metrics_private_routes.list_wallet_connections", return_value=fake_connections):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get("/api/metrics/private/wallets/connections")
    finally:
        app.dependency_overrides.pop(dep, None)

    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    assert data["connections"][0]["wallet"] == "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


@pytest.mark.asyncio
async def test_get_funnel_defaults_to_visitor_source():
    """GET /api/metrics/funnel with no params stays the pre-#1028 Redis/HLL funnel (no behavior change)."""
    from archimedes.main import app

    with (
        patch(
            "archimedes.api.metrics_routes.FunnelStore.get_totals",
            new=AsyncMock(
                return_value={
                    "landed": 10,
                    "wallet_connected": 4,
                    "generation_started": 2,
                    "vault_deployed": 1,
                }
            ),
        ),
        patch(
            "archimedes.api.metrics_routes.FunnelStore.get_totals_by_agent_type",
            new=AsyncMock(
                return_value={
                    "landed": {"internal": 1, "external": 2, "human": 7},
                    "wallet_connected": {"internal": 0, "external": 0, "human": 4},
                    "generation_started": {"internal": 0, "external": 0, "human": 2},
                    "vault_deployed": {"internal": 0, "external": 0, "human": 1},
                }
            ),
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/metrics/funnel")

    assert resp.status_code == 200
    data = resp.json()
    assert data["source"] == "visitor"
    # Stage vocabulary AND order (#1643 changed both — the free path made a
    # visitor generate before connecting a wallet, so the old order published a
    # step_conversion against a stage that no longer precedes its successor).
    assert [s["stage"] for s in data["stages"]] == [
        "landed",
        "generation_started",
        "free_generation_used",
        "wallet_gate_shown",
        "wallet_connected",
        "vault_deployed",
    ]
    # #788: the per-stage agent_type breakdown rides alongside the unchanged aggregate.
    by_stage = {s["stage"]: s["by_agent_type"] for s in data["stages"]}
    assert by_stage["landed"] == {"internal": 1, "external": 2, "human": 7}
    landed = next(s for s in data["stages"] if s["stage"] == "landed")
    assert landed["distinct_visitors"] == 10  # unchanged by the additive breakdown


@pytest.mark.asyncio
async def test_get_funnel_identity_source_recomputes_from_ledger():
    """GET /api/metrics/funnel?source=identity — issue #1028 AC3.

    No Redis/HLL involved; stages are relabeled onto the pre-existing
    visitor-funnel vocabulary (wallet_connected/generation_started/
    vault_deployed) so the response shape — and the frontend's existing
    label map — stays reusable.
    """
    from archimedes.main import app

    with patch(
        "archimedes.api.metrics_routes.get_identity_funnel",
        return_value={"auth_verified": 6, "generation_started": 3, "vault_created": 1},
    ) as mocked:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/metrics/funnel", params={"source": "identity"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["source"] == "identity"
    stage_names = [s["stage"] for s in data["stages"]]
    assert stage_names == ["wallet_connected", "generation_started", "vault_deployed"]
    counts = {s["stage"]: s["distinct_visitors"] for s in data["stages"]}
    assert counts == {"wallet_connected": 6, "generation_started": 3, "vault_deployed": 1}
    # human_only defaults True (AC3's dogfood/agent exclusion).
    mocked.assert_called_once_with(exclude_dogfood=True)


@pytest.mark.asyncio
async def test_get_funnel_identity_source_human_only_false_disables_exclusion():
    from archimedes.main import app

    with patch(
        "archimedes.api.metrics_routes.get_identity_funnel",
        return_value=dict.fromkeys(("auth_verified", "generation_started", "vault_created"), 0),
    ) as mocked:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/metrics/funnel", params={"source": "identity", "human_only": "false"})

    assert resp.status_code == 200
    mocked.assert_called_once_with(exclude_dogfood=False)
