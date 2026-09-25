"""Ownership gate on the /api/traces/* READ routes — issue #1556.

The hole: `list_traces`, `get_trace`, `verify_trace` and `get_trace_canonical`
had NO auth dependency. `vault_address` was a filter query param, not a gate —
omit it and you enumerated every trace on the platform; `/canonical` returned
the full hashed body, holdings included.

Every test here is a GUARD test, so each one is paired with proof that it can
fail. The `neutralized_gate` fixture reproduces the pre-#1556 route exactly:
those routes ran no predicate at all, so forcing this codebase's predicate to
`True` puts the app back in the unfixed state. Each `*_leaks_without_the_gate`
test asserts the leak IS reproducible there — if the gate stopped being
load-bearing, those tests fail, and a gate test that passes either way is
worthless coverage (CLAUDE.md § "A test that passes against the unfixed code
proves nothing").

Hermetic: the trace store is mocked at the `AgentStateStore` boundary and the
identity DB is a per-test tmp SQLite. No Redis, no Postgres, no .env.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from tests.db_isolation import redirect_to_tmp_sqlite

pytestmark = pytest.mark.anyio

OWNER_USER_ID = "user-owner-1556"
OTHER_USER_ID = "user-other-1556"
OWNER_WALLET = "0x1111111111111111111111111111111111111111"
OTHER_WALLET = "0x2222222222222222222222222222222222222222"

HOUSE_VAULT = "0xaaaa000000000000000000000000000000000aaa"
USER_VAULT = "0xbbbb000000000000000000000000000000000bbb"

#: Distinctive strings that must never reach an unauthorized reader. Asserting
#: on the CONTENT, not just the status code, is what makes these tests about
#: the leak rather than about a number.
SECRET_REASONING = "PRIVATE-1556 rotate 80% into the user's undisclosed basket"
SECRET_HOLDING = "SECRETTOKEN1556"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path):
    """Per-test SQLite carrying the identity tables the gate reads."""
    yield from redirect_to_tmp_sqlite(tmp_path)


@pytest.fixture(autouse=True)
def _unset_allowlist(monkeypatch):
    """Run against the WEAKEST configuration of the floor.

    With `PUBLIC_TRACE_VAULTS` unset, an unowned trace defaults to house-public
    — so anything these tests prove is hidden is hidden by *ownership*, not by
    an allowlist that happened to be armed. A guard demonstrated only in its
    strictest configuration would not be a demonstration of the ownership
    model at all.
    """
    monkeypatch.delenv("PUBLIC_TRACE_VAULTS", raising=False)
    monkeypatch.delenv("AGENT_VAULT_ADDRESSES", raising=False)


def _house_trace() -> dict:
    return {
        "id": "house-trace-1",
        "vault_address": HOUSE_VAULT,
        "decision_type": "rebalance",
        "trigger": "drift",
        "timestamp": "2026-08-30T00:00:00+00:00",
        "market_context": {"regime": "risk_on"},
        "portfolio_before": {"USDC": 1},
        "portfolio_after": {"USDC": 1},
        "reasoning": "House agent public demo reasoning",
        "confidence": 0.5,
        "trades_executed": [],
        "strategies_referenced": [],
        "trace_hash": "0xhousehash",
        "arc_tx_hash": None,
        "is_verified": False,
    }


def _user_trace(*, stamped: bool) -> dict:
    """A user-owned private trace.

    ``stamped`` picks which of the two ownership layers is under test: the
    on-record stamp written by ``save_trace`` (every trace published after
    #1556), or the legacy row with no stamp whose owner has to be recovered
    from ``vault_metadata``.
    """
    trace = {
        "id": "user-trace-1",
        "vault_address": USER_VAULT,
        "decision_type": "rebalance",
        "trigger": "drift",
        "timestamp": "2026-08-30T01:00:00+00:00",
        "market_context": {"regime": "risk_off"},
        "portfolio_before": {SECRET_HOLDING: 42},
        "portfolio_after": {SECRET_HOLDING: 99},
        "reasoning": SECRET_REASONING,
        "confidence": 0.9,
        "trades_executed": [],
        "strategies_referenced": [],
        "trace_hash": "0xuserhash",
        "arc_tx_hash": None,
        "is_verified": False,
    }
    if stamped:
        trace["owner_user_id"] = OWNER_USER_ID
        trace["owner_wallet"] = OWNER_WALLET
    return trace


def _seed_vault_owner() -> None:
    """`vault_metadata` row making USER_VAULT owned by OWNER_USER_ID."""
    from datetime import UTC, datetime

    from archimedes.db import get_session
    from archimedes.models.account import AuthUser
    from archimedes.models.chat import VaultMetadata
    from archimedes.models.identity import WalletIdentity

    now = datetime.now(UTC)
    with get_session() as session:
        for uid, email in ((OWNER_USER_ID, "owner@example.test"), (OTHER_USER_ID, "other@example.test")):
            session.merge(AuthUser(id=uid, name=uid, email=email, email_verified=True, created_at=now, updated_at=now))
        for wallet in (OWNER_WALLET, OTHER_WALLET):
            session.merge(WalletIdentity(wallet_address=wallet.lower(), actor_class="human", first_seen_at=now))
        session.merge(
            VaultMetadata(
                vault_address=USER_VAULT.lower(),
                name="user vault",
                symbol="UV",
                creator_address=OWNER_WALLET.lower(),
                owner_user_id=OWNER_USER_ID,
                strategy_ids="[]",
            )
        )
        session.commit()


@contextmanager
def _as(user_id: str | None, wallet: str | None = None):
    """Run the request as this caller. ``None`` = anonymous.

    Patches the two identity resolvers the routes consult, which is the
    boundary: `get_current_user` reads the Better Auth session and
    `get_verified_wallet` reads the linked-wallet table. Nothing about the gate
    itself is patched.
    """
    from archimedes.api.account_auth import CurrentUser

    user = (
        None
        if user_id is None
        else CurrentUser(id=user_id, name=user_id, email=f"{user_id}@example.test", email_verified=True)
    )
    with (
        patch("archimedes.api.account_auth.get_current_user", return_value=user),
        patch("archimedes.api.auth_siwe.get_verified_wallet", return_value=wallet),
    ):
        yield


@contextmanager
def _store(traces: list[dict]):
    """Mock the trace store at the `AgentStateStore` boundary."""
    from archimedes.services.redis_state import AgentStateStore

    by_id = {t["id"]: t for t in traces}
    by_id.update({t["trace_hash"]: t for t in traces})

    async def _list(vault_address=None, decision_type=None, strategy_id=None, limit=20, offset=0):
        # `strategy_id` is the real store's signature, filtered through the
        # real predicate: a double that accepted the argument and ignored it
        # would let a scoped listing silently widen back to the whole feed
        # here while the production filter narrowed it.
        from archimedes.services.redis_state import trace_references_strategy

        rows = [
            t
            for t in traces
            if (not vault_address or t["vault_address"].lower() == vault_address.lower())
            and (not decision_type or t["decision_type"] == decision_type)
            and (not strategy_id or trace_references_strategy(t, strategy_id))
        ]
        return rows[offset : offset + limit], len(rows)

    with (
        patch.object(AgentStateStore, "list_traces", AsyncMock(side_effect=_list)),
        patch.object(AgentStateStore, "get_trace", AsyncMock(side_effect=lambda k: by_id.get(k))),
        patch.object(AgentStateStore, "close", AsyncMock()),
    ):
        yield


@contextmanager
def neutralized_gate():
    """Put the app back in its pre-#1556 state: no visibility predicate at all.

    The unfixed routes had no gate, so every read was allowed — which is
    exactly what forcing the predicate to `True` reproduces. Both entry points
    are patched because `list_traces` calls `is_trace_visible` directly while
    the three detail routes go through `can_read_trace`.
    """
    with (
        patch("archimedes.services.trace_visibility.is_trace_visible", return_value=True),
        patch("archimedes.services.trace_visibility.can_read_trace", return_value=True),
    ):
        yield


async def _get(path: str, **params):
    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(path, params=params)


# ── The gate holds ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("stamped", [True, False], ids=["stamped-record", "legacy-row-via-vault-metadata"])
async def test_anonymous_list_returns_only_house_traces(stamped):
    """ACCEPTANCE: anonymous `GET /api/traces/?limit=100` never returns a user's."""
    _seed_vault_owner()
    with _store([_house_trace(), _user_trace(stamped=stamped)]), _as(None):
        resp = await _get("/api/traces/", limit=100)

    assert resp.status_code == 200
    body = resp.json()
    assert [t["id"] for t in body["traces"]] == ["house-trace-1"]
    assert body["total"] == 1  # the private row is not even counted
    assert SECRET_REASONING not in resp.text


@pytest.mark.parametrize("stamped", [True, False], ids=["stamped-record", "legacy-row-via-vault-metadata"])
async def test_different_user_list_does_not_see_another_users_trace(stamped):
    _seed_vault_owner()
    with _store([_house_trace(), _user_trace(stamped=stamped)]), _as(OTHER_USER_ID, OTHER_WALLET):
        resp = await _get("/api/traces/", limit=100)

    assert [t["id"] for t in resp.json()["traces"]] == ["house-trace-1"]
    assert SECRET_REASONING not in resp.text


@pytest.mark.parametrize("stamped", [True, False], ids=["stamped-record", "legacy-row-via-vault-metadata"])
async def test_owner_still_sees_their_own_trace(stamped):
    """The gate must not be a wall: the owner keeps full access."""
    _seed_vault_owner()
    with _store([_house_trace(), _user_trace(stamped=stamped)]), _as(OWNER_USER_ID, OWNER_WALLET):
        listed = await _get("/api/traces/", limit=100)
        detail = await _get("/api/traces/user-trace-1")
        canonical = await _get("/api/traces/user-trace-1/canonical")

    assert {t["id"] for t in listed.json()["traces"]} == {"house-trace-1", "user-trace-1"}
    assert detail.status_code == 200
    assert detail.json()["reasoning"] == SECRET_REASONING
    assert canonical.status_code == 200
    assert SECRET_HOLDING in canonical.text


@pytest.mark.parametrize("stamped", [True, False], ids=["stamped-record", "legacy-row-via-vault-metadata"])
@pytest.mark.parametrize("caller", [None, OTHER_USER_ID], ids=["anonymous", "different-user"])
async def test_non_owner_cannot_read_trace_by_id(stamped, caller):
    """ACCEPTANCE: a non-owner cannot read another user's trace by id.

    404, not 403: a 403 on someone else's id confirms the id exists, which is
    half of the enumeration the gate exists to prevent.
    """
    _seed_vault_owner()
    wallet = OTHER_WALLET if caller else None
    with _store([_user_trace(stamped=stamped)]), _as(caller, wallet):
        detail = await _get("/api/traces/user-trace-1")
        canonical = await _get("/api/traces/user-trace-1/canonical")
        verify = await _get("/api/traces/user-trace-1/verify")

    assert detail.status_code == 404
    assert canonical.status_code == 404
    assert verify.status_code == 404
    for resp in (detail, canonical, verify):
        assert SECRET_REASONING not in resp.text
        assert SECRET_HOLDING not in resp.text


async def test_wallet_alone_does_not_grant_access_to_a_user_owned_trace():
    """Canonical identity is not bypassable by controlling the named wallet.

    Same two-tier rule as `is_strategy_visible`: once a row carries an
    `owner_user_id`, a matching wallet must NOT grant access, or migrating to
    canonical identity would have been a security downgrade.
    """
    _seed_vault_owner()
    with _store([_user_trace(stamped=True)]), _as(None, OWNER_WALLET):
        resp = await _get("/api/traces/user-trace-1")
    assert resp.status_code == 404


async def test_stamped_record_stays_private_when_the_identity_db_is_down():
    """A Postgres outage must not downgrade a private trace to a public one.

    This is why ownership is stamped on the record at write time rather than
    looked up on every read.
    """
    _seed_vault_owner()
    with (
        _store([_user_trace(stamped=True)]),
        _as(None),
        # Both resolvers: `resolve_vault_owners` is the write-path entry point,
        # `_resolve_vault_owners_uncached` the one the memoized read path calls
        # (#1573). Patching only the former would leave the read path talking to
        # a live database and quietly stop simulating the outage.
        patch(
            "archimedes.services.trace_visibility.resolve_vault_owners",
            side_effect=RuntimeError("db down"),
        ),
        patch(
            "archimedes.services.trace_visibility._resolve_vault_owners_uncached",
            side_effect=RuntimeError("db down"),
        ),
    ):
        listed = await _get("/api/traces/", limit=100)
        detail = await _get("/api/traces/user-trace-1")

    assert listed.json()["traces"] == []
    assert detail.status_code == 404


async def test_generation_trace_with_no_vault_is_never_public():
    """`trigger="fusion_generation"` traces carry a user's private thesis.

    They are written with `vault_address=""`, so there is no vault to own them
    — an ownerless body with no vault must not fall through to the house
    default.
    """
    trace = _user_trace(stamped=False)
    trace["vault_address"] = ""
    trace["trigger"] = "fusion_generation"

    with _store([trace]), _as(None):
        listed = await _get("/api/traces/", limit=100)
        detail = await _get("/api/traces/user-trace-1")

    assert listed.json()["traces"] == []
    assert detail.status_code == 404


async def test_allowlist_when_armed_restricts_unowned_traces(monkeypatch):
    """The interim FLOOR: with `PUBLIC_TRACE_VAULTS` set, an unowned trace
    outside the list is private even though nothing owns it."""
    monkeypatch.setenv("PUBLIC_TRACE_VAULTS", HOUSE_VAULT)

    stranger = _house_trace()
    stranger["id"] = "stranger-trace-1"
    stranger["trace_hash"] = "0xstrangerhash"
    stranger["vault_address"] = "0xcccc000000000000000000000000000000000ccc"

    with _store([_house_trace(), stranger]), _as(None):
        resp = await _get("/api/traces/", limit=100)

    assert [t["id"] for t in resp.json()["traces"]] == ["house-trace-1"]


async def test_onchain_fallback_listing_is_gated_too():
    """Taking Redis out of the picture must not reopen enumeration.

    The registry-only projection carries no reasoning body, but it still names
    the vault and the anchor, so the same predicate applies.
    """
    from archimedes.services.redis_state import AgentStateStore

    _seed_vault_owner()
    details = {
        1: {"vault": HOUSE_VAULT, "timestamp": 1_700_000_000, "trace_hash": "0xh1"},
        2: {"vault": USER_VAULT, "timestamp": 1_700_000_100, "trace_hash": "0xh2"},
    }

    with (
        patch.object(AgentStateStore, "list_traces", AsyncMock(side_effect=ConnectionError("redis down"))),
        patch.object(AgentStateStore, "close", AsyncMock()),
        patch("archimedes.chain.trace_publisher.trace_publisher") as mock_pub,
        _as(None),
    ):
        mock_pub.get_total_trace_count = AsyncMock(return_value=2)
        mock_pub.get_trace_by_id = AsyncMock(side_effect=lambda tid: details.get(tid))
        resp = await _get("/api/traces/", limit=10)

    assert [t["vault_address"] for t in resp.json()["traces"]] == [HOUSE_VAULT]


# ── The gate is load-bearing (non-tautology proof) ──────────────────────────


@pytest.mark.parametrize("stamped", [True, False], ids=["stamped-record", "legacy-row-via-vault-metadata"])
async def test_anonymous_list_leaks_without_the_gate(stamped):
    """Same request as `test_anonymous_list_returns_only_house_traces`, run
    against the unfixed behaviour — it MUST leak, or that test guards nothing."""
    _seed_vault_owner()
    with _store([_house_trace(), _user_trace(stamped=stamped)]), _as(None), neutralized_gate():
        resp = await _get("/api/traces/", limit=100)

    assert "user-trace-1" in [t["id"] for t in resp.json()["traces"]]
    assert SECRET_REASONING in resp.text


async def test_canonical_leaks_holdings_without_the_gate():
    """The CRITICAL half of #1556: `/canonical` returns the full hashed body,
    holdings included, to an anonymous caller when the gate is not there."""
    _seed_vault_owner()
    with _store([_user_trace(stamped=True)]), _as(None), neutralized_gate():
        resp = await _get("/api/traces/user-trace-1/canonical")

    assert resp.status_code == 200
    body = json.loads(resp.text)
    assert body["portfolio_after"] == {SECRET_HOLDING: 99}
    assert SECRET_REASONING in resp.text


async def test_detail_leaks_without_the_gate():
    _seed_vault_owner()
    with _store([_user_trace(stamped=True)]), _as(None), neutralized_gate():
        resp = await _get("/api/traces/user-trace-1")

    assert resp.status_code == 200
    assert resp.json()["reasoning"] == SECRET_REASONING


async def test_verify_leaks_without_the_gate():
    _seed_vault_owner()
    with _store([_user_trace(stamped=True)]), _as(None), neutralized_gate():
        resp = await _get("/api/traces/user-trace-1/verify")

    assert resp.status_code == 200
    assert resp.json()["vault"] == USER_VAULT


# ── The WIDENED detail body is gated by the same rule ───────────────────────
#
# `GET /api/traces/{id}` used to return the summary projection. It now returns
# `TraceDetailResponse`: market_context, portfolio_before/after,
# consulted_paper_hashes, settlement_tx_hashes and ipfs_cid — four of the
# thirteen _HASH_FIELDS plus the non-hashed provenance. Those are the SAME
# fields that made `/canonical` the CRITICAL surface in this issue, in a
# friendlier format. Widening a route is a security change to that route, so
# the widened surface gets its own guard rather than inheriting the summary
# route's.


def _user_trace_with_full_body() -> dict:
    """A private trace carrying every field the widened detail route emits."""
    trace = _user_trace(stamped=True)
    trace["consulted_paper_hashes"] = ["2301.00001:SECRETPAPER1556"]
    trace["settlement_tx_hashes"] = ["0xSECRETSETTLE1556"]
    trace["ipfs_cid"] = "bafySECRET1556"
    return trace


#: Every value the widened body adds, and nothing that was already exposed by
#: the pre-widening projection — so a hit here is a leak this branch created.
_WIDENED_SECRETS = ("SECRETTOKEN1556", "risk_off", "SECRETPAPER1556", "0xSECRETSETTLE1556", "bafySECRET1556")


@pytest.mark.parametrize("caller", [None, OTHER_USER_ID], ids=["anonymous", "different-user"])
async def test_widened_detail_body_is_owner_gated(caller):
    """ACCEPTANCE: none of the newly-exposed fields reach a non-owner."""
    _seed_vault_owner()
    wallet = OTHER_WALLET if caller else None
    with _store([_user_trace_with_full_body()]), _as(caller, wallet):
        resp = await _get("/api/traces/user-trace-1")

    assert resp.status_code == 404
    for secret in _WIDENED_SECRETS:
        assert secret not in resp.text, f"{secret} leaked through the widened detail body"


async def test_widened_detail_body_leaks_without_the_gate():
    """Non-tautology proof for the test above.

    Run the identical request against the pre-#1556 behaviour (no predicate at
    all). Every widened field MUST come back, or the guard above is passing for
    a reason other than the gate — e.g. because the fields were never emitted.
    """
    _seed_vault_owner()
    with _store([_user_trace_with_full_body()]), _as(None), neutralized_gate():
        resp = await _get("/api/traces/user-trace-1")

    assert resp.status_code == 200
    body = resp.json()
    # Assert on the parsed body, not just the text: this simultaneously proves
    # the widening actually happened, so the guard is not passing vacuously.
    assert body["portfolio_before"] == {SECRET_HOLDING: 42}
    assert body["portfolio_after"] == {SECRET_HOLDING: 99}
    assert body["market_context"] == {"regime": "risk_off"}
    assert body["consulted_paper_hashes"] == ["2301.00001:SECRETPAPER1556"]
    assert body["settlement_tx_hashes"] == ["0xSECRETSETTLE1556"]
    assert body["ipfs_cid"] == "bafySECRET1556"
    for secret in _WIDENED_SECRETS:
        assert secret in resp.text


async def test_owner_reads_the_whole_widened_body():
    """The gate must not be a wall: the widened body is the point of the change."""
    _seed_vault_owner()
    with _store([_user_trace_with_full_body()]), _as(OWNER_USER_ID, OWNER_WALLET):
        resp = await _get("/api/traces/user-trace-1")

    assert resp.status_code == 200
    body = resp.json()
    assert body["market_context"] == {"regime": "risk_off"}
    assert body["portfolio_before"] == {SECRET_HOLDING: 42}
    assert body["portfolio_after"] == {SECRET_HOLDING: 99}
    assert body["ipfs_cid"] == "bafySECRET1556"
    # The summary fields survive the widening — the model widens, not replaces.
    assert body["reasoning"] == SECRET_REASONING
    assert body["trace_hash"] == "0xuserhash"


async def test_scoped_listing_still_applies_the_ownership_filter():
    """Passing the STRATEGY gate is not a read grant on someone else's traces.

    `?strategy_id=` answers "may you know this strategy exists". #1556 answers
    "may you read this trace". Both run: a published strategy consulted by two
    users' vaults must not turn its passport into a window onto the other
    user's decisions.
    """
    from archimedes.db import get_session
    from archimedes.models.strategy_store import StrategyRecord

    _seed_vault_owner()
    with get_session() as session:
        session.add(
            StrategyRecord(
                id="shared-strategy-1556",
                content_hash="0x" + "5" * 64,
                generation_method="fusion",
                source_papers="[]",
                strategy_name="Shared",
                thesis="t",
                asset_universe="[]",
                risk_profile="moderate",
                status="candidate",
                is_published=True,
            )
        )
        session.commit()

    house, private = _house_trace(), _user_trace(stamped=True)
    house["strategies_referenced"] = ["shared-strategy-1556"]
    private["strategies_referenced"] = ["shared-strategy-1556"]

    with _store([house, private]), _as(None):
        resp = await _get("/api/traces/", strategy_id="shared-strategy-1556", limit=100)

    assert resp.status_code == 200
    assert [t["id"] for t in resp.json()["traces"]] == ["house-trace-1"]
    assert resp.json()["total"] == 1
    assert SECRET_REASONING not in resp.text
