"""Tests for marketplace API routes."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from archimedes.api.marketplace_routes import marketplace_router
from archimedes.api.wallet_routes import require_linked_wallet as require_verified_wallet
from archimedes.chain.constants import MAX_MANAGEMENT_FEE_BPS, MAX_PERFORMANCE_FEE_BPS
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.db_isolation import redirect_to_tmp_sqlite

TEST_WALLET = "0x0000000000000000000000000000000000000001"


def _vaddr(byte: str) -> str:
    """A valid 20-byte hex vault address built from one repeated byte.

    Publish validates user-supplied vault_address format before the #1138
    fee guard runs, so test addresses must be real-shaped."""
    return "0x" + byte * 20


@pytest.fixture(autouse=True)
def _setup_db(tmp_path):
    """Point archimedes.db at a fresh, isolated tmp SQLite file for each test.

    See tests/db_isolation.py — archimedes.db's engine/SessionLocal/DATABASE_URL
    are process-global singletons bound once at import time, so real isolation
    means swapping and restoring them, not create_all/drop_all on whichever
    engine object happens to still be bound to them (issue #1100).
    """
    yield from redirect_to_tmp_sqlite(tmp_path)


@pytest.fixture(autouse=True)
def _roadmap_surfaces_on(monkeypatch):
    """POST /publish and POST /subscribe 404 while FEATURE_ROADMAP_SURFACES is
    off (#1432), the default everywhere. Most tests here pin how those routes
    behave once the flag is on, so they run with it on; the flag-off tests at
    the bottom of this file turn it off themselves."""
    monkeypatch.setenv("FEATURE_ROADMAP_SURFACES", "true")


@pytest.fixture(autouse=True)
def _treasury_wallet(monkeypatch):
    """publish requires a server-side ARCHIMEDES_TREASURY_WALLET (the platform
    revenue address); set a test value so publish does not 503."""
    monkeypatch.setenv("ARCHIMEDES_TREASURY_WALLET", "0x" + "11" * 20)


@pytest.fixture(autouse=True)
def _seed_strategy_records(_setup_db):
    """Insert minimal StrategyRecord rows so the D5 ownership check doesn't 404.

    The D5 gate (wallet_can_publish) is patched separately; this seeds the
    strategy_store table so the prior record-existence check passes.
    Depends on _setup_db so tables exist before inserts.
    """
    from archimedes.db import get_session
    from archimedes.models.strategy_store import StrategyRecord

    _STRATEGY_IDS = [
        "test_strat",
        "dup_strat",
        "check_pool",
        "dup_sid_strat",
        "redact_strat",
        "refund_strat",
        "gen_strat",
    ]
    with get_session() as session:
        for i, sid in enumerate(_STRATEGY_IDS):
            if session.query(StrategyRecord).filter_by(id=sid).first() is None:
                # content_hash must be unique (uq_strategy_content_hash) — use i to differentiate
                session.add(
                    StrategyRecord(
                        id=sid,
                        content_hash="0x" + format(i, "02x") * 32,
                        generation_method="fusion",
                        is_example=False,
                    )
                )
        session.commit()


@pytest.fixture(autouse=True)
def _mock_wallet_can_publish():
    """Patch wallet_can_publish to return True — the D5 gate is unit-tested separately."""
    with patch("archimedes.api.marketplace_routes.wallet_can_publish", return_value=True):
        yield


@pytest.fixture(autouse=True)
def _mock_provision_publisher_wallet():
    """Patch provision_publisher_wallet so tests don't call Circle API."""
    with patch(
        "archimedes.marketplace.wallet_provisioner.provision_publisher_wallet",
        new=AsyncMock(return_value=("test-agent-wallet-uuid", "0x0000000000000000000000000000000000000999")),
    ):
        yield


@pytest.fixture(autouse=True)
def _mock_spend_cap_not_over():
    """subscribe_strategy checks spend_cap.is_over_cap before provisioning a
    wallet (#713). Default every test in this file to "not over cap" so the
    existing subscribe-path tests exercise the happy path without a real
    Redis round-trip; test_subscribe_rejects_over_spend_cap overrides this
    to True to exercise the 429 refusal."""
    with patch("archimedes.api.marketplace_routes.spend_cap.is_over_cap", new=AsyncMock(return_value=False)):
        yield


@pytest.fixture
def app():
    """FastAPI app with marketplace router and a mock market service."""
    from archimedes.marketplace.service import MarketService

    a = FastAPI()
    a.include_router(marketplace_router)

    # Override auth to return a test wallet
    a.dependency_overrides[require_verified_wallet] = lambda: TEST_WALLET

    market = MagicMock(spec=MarketService)
    market.payments_dry_run = True
    # Use the signer path for contract calls (simpler mock surface)
    market.signer = MagicMock()
    market.signer.is_configured = True
    market.signer.execute_contract = AsyncMock()
    market.executor = MagicMock()
    market.executor.create_vault = AsyncMock(return_value="0xvault")
    # Fee-cap guard (issue #1138): publish with a user-supplied vault_address and
    # subscribe both read the vault's on-chain fee bps and refuse over-cap or
    # unreadable vaults. Default the mock to a compliant (0, 0) vault; the guard
    # tests override per-case.
    market.executor.get_vault_fee_bps = AsyncMock(return_value=(0, 0))
    # Funding gate (D7): the publish route rejects (402) when the vault holds less
    # than MARKETPLACE_MIN_VAULT_FUNDS_RAW. Mock the vault as well-funded so the
    # publish-path tests exercise publishing rather than the funding gate; the
    # gate itself is covered by test_publish_underfunded_vault_returns_402.
    market._usdc_balance_of = AsyncMock(return_value=10_000_000)
    market.loader = MagicMock()
    market.loader._contract.return_value.functions.pools.return_value.call = AsyncMock(
        return_value=("0xaddr", "0xaddr", 0, 0, False)
    )
    market.settings = MagicMock()
    market.settings.payment_splitter_address = "0xsplitter"
    market.start_publisher = AsyncMock()
    market.add_subscriber = AsyncMock()
    market.remove_subscriber = AsyncMock()
    market.refund_subscriber = AsyncMock(return_value=None)
    market.stop_publisher = AsyncMock()
    market.state = MagicMock()
    market.state.save_subscribers = AsyncMock()
    market.state.save_payment = AsyncMock()
    market.state.get_events = AsyncMock(return_value=[])
    market.publishers = {}

    a.state.market = market
    return a


@pytest.fixture
def client(app):
    return TestClient(app)


def test_publish_rejects_unknown_strategy(client):
    """Publish with a strategy_id that has no StrategyRecord returns 404.

    Existence is validated against StrategyRecord (not the curated-only, file-backed
    LocalStrategyProvider), so an id that was never generated or seeded 404s. "nonexistent"
    is deliberately absent from _seed_strategy_records.
    """
    resp = client.post(
        "/api/marketplace/publish",
        json={"strategy_id": "nonexistent", "vault_address": _vaddr("aa")},
    )
    assert resp.status_code == 404, resp.text


def test_publish_generated_strategy_not_in_curated_provider(client):
    """A GENERATED strategy (is_example=False, not a curated file id) must be publishable.

    Regression: publish previously gated on strategy_provider().get_strategy() — the
    curated-only file provider — BEFORE the real StrategyRecord check, so every generated
    strategy 404'd "Strategy not found". "gen_strat" is seeded as a StrategyRecord with
    is_example=False and is not a curated file id, so a 200 here proves existence resolves
    from StrategyRecord, not the file provider.
    """
    # The test name's premise, made explicit: gen_strat is genuinely absent from the
    # curated file provider — so the 200 below can only come from the StrategyRecord path.
    from archimedes.api._route_helpers import strategy_provider

    assert strategy_provider().get_strategy("gen_strat") is None

    resp = client.post(
        "/api/marketplace/publish",
        json={"strategy_id": "gen_strat", "vault_address": _vaddr("a1")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["strategy_id"] == "gen_strat"
    assert resp.json()["role"] == "publisher"


def test_publish_creates_publisher_row(client):
    """Publish creates a MarketplaceAgent row with a derived pool_id."""
    resp = client.post(
        "/api/marketplace/publish",
        json={"strategy_id": "test_strat", "vault_address": _vaddr("a2")},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["strategy_id"] == "test_strat"
    assert data["role"] == "publisher"
    assert data["pool_id"].startswith("0x")
    assert len(data["pool_id"]) == 66
    assert data["pool_id"] != "sub_id"  # not accidentally in sub_id column


def test_publish_underfunded_vault_returns_402(client):
    """Publish rejects with 402 when the vault holds less than the minimum funding."""
    client.app.state.market._usdc_balance_of = AsyncMock(return_value=0)
    resp = client.post(
        "/api/marketplace/publish",
        json={"strategy_id": "test_strat", "vault_address": _vaddr("a2")},
    )
    assert resp.status_code == 402, resp.text
    assert "below minimum" in resp.json()["detail"]


def test_subscribe_rejects_blank_sub_id(client):
    """Subscribe with blank sub_id returns 400."""
    resp = client.post(
        "/api/marketplace/subscribe",
        json={
            "strategy_id": "test_strat",
            "pool_id": "0x" + "aa" * 32,
            "sub_id": "",
            "ephemeral_wallet": "0xeph",
            "initial_deposit_usdc": 100,
        },
    )
    assert resp.status_code == 400, resp.text


def test_subscribe_rejects_over_spend_cap(client):
    """A wallet at/over its rolling 24h marketplace spend cap gets 429 on a
    NEW subscription (#713). The guard sits after the existing dedup checks
    and BEFORE wallet provisioning — provision_subscriber_wallet must never
    be reached when the check refuses."""
    resp = client.post(
        "/api/marketplace/publish",
        json={"strategy_id": "test_strat", "vault_address": _vaddr("c1")},
    )
    assert resp.status_code == 200, resp.text

    with (
        patch("archimedes.api.marketplace_routes.spend_cap.is_over_cap", new=AsyncMock(return_value=True)),
        patch(
            "archimedes.marketplace.wallet_provisioner.provision_subscriber_wallet",
            new=AsyncMock(side_effect=AssertionError("must not provision a wallet when over the spend cap")),
        ) as m_provision,
    ):
        resp = client.post(
            "/api/marketplace/subscribe",
            json={"strategy_id": "test_strat", "sub_id": "0x" + "f0" * 32, "ephemeral_wallet": "0xeph"},
        )

    assert resp.status_code == 429, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "marketplace_spend_cap_reached"
    m_provision.assert_not_awaited()


def test_subscribe_under_spend_cap_still_succeeds(client):
    """Sanity-checks the mock boundary the opposite way: with is_over_cap
    False (the file's autouse default), subscribe proceeds normally — guards
    against an accidentally-inverted cap check silently 429-ing everything."""
    resp = client.post(
        "/api/marketplace/publish",
        json={"strategy_id": "test_strat", "vault_address": _vaddr("c2")},
    )
    assert resp.status_code == 200, resp.text

    with patch(
        "archimedes.marketplace.wallet_provisioner.provision_subscriber_wallet",
        new=AsyncMock(return_value=("w-cap-ok", "0x0000000000000000000000000000000000000ee9")),
    ):
        resp = client.post(
            "/api/marketplace/subscribe",
            json={"strategy_id": "test_strat", "sub_id": "0x" + "f1" * 32, "ephemeral_wallet": "0xeph"},
        )
    assert resp.status_code == 200, resp.text


def test_publish_duplicate_returns_409(client):
    """Publishing the same strategy twice returns 409."""
    resp1 = client.post(
        "/api/marketplace/publish",
        json={"strategy_id": "dup_strat", "vault_address": _vaddr("aa")},
    )
    assert resp1.status_code == 200

    resp2 = client.post(
        "/api/marketplace/publish",
        json={"strategy_id": "dup_strat", "vault_address": _vaddr("aa")},
    )
    assert resp2.status_code == 409, resp2.text


def test_publish_pool_id_is_derived_not_accepted(client):
    """The pool_id in the response should match derive_pool_id, not come from client."""
    # Validate that pool_id is non-zero, 66 chars, and starts with 0x
    resp = client.post(
        "/api/marketplace/publish",
        json={"strategy_id": "check_pool", "vault_address": _vaddr("a3")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["pool_id"].startswith("0x")
    assert len(resp.json()["pool_id"]) == 66


def test_list_published_empty(client):
    """GET /published returns empty list when no publishers."""
    resp = client.get("/api/marketplace/published")
    assert resp.status_code == 200
    assert resp.json() == []


def test_subscribe_succeeds_live_mode_no_chain_calls(client, app):
    """Subscribe succeeds in live mode (payments_dry_run=False) with no SubscriptionManager calls."""

    # Create publisher first
    resp = client.post(
        "/api/marketplace/publish",
        json={"strategy_id": "test_strat", "vault_address": _vaddr("aa")},
    )
    assert resp.status_code == 200

    # Enable live mode
    market = app.state.market
    market.payments_dry_run = False

    # Mock the wallet provisioner so it succeeds without real Circle API
    wallet_id = "test-wallet-uuid"
    wallet_address = "0x0000000000000000000000000000000000000eee"
    with patch(
        "archimedes.marketplace.wallet_provisioner.provision_subscriber_wallet",
        new=AsyncMock(return_value=(wallet_id, wallet_address)),
    ):
        resp = client.post(
            "/api/marketplace/subscribe",
            json={
                "strategy_id": "test_strat",
                "sub_id": "0x" + "bb" * 32,
                "ephemeral_wallet": "0xeph",
            },
        )
    # Should succeed — on-chain validation is gone (P7)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["role"] == "subscriber"
    assert data["sub_id"] == "0x" + "bb" * 32
    # No SubscriptionManager contract calls should have been made
    contract = market.loader._contract
    for call_args in contract.call_args_list:
        name = call_args[0][1] if len(call_args[0]) > 1 else ""
        assert "SubscriptionManager" not in name, f"Unexpected SubscriptionManager call: {call_args}"


# x402 payment-webhook route and _WEBHOOK_SECRET are not yet implemented
# in production code — tests deferred to the follow-up issue.


def test_subscribe_rejects_duplicate_sub_id_from_other_wallet(client, app):
    """A sub_id registered by one wallet cannot be reused by another.

    sub_id is client-supplied and keys the in-process engine
    (pub.subscribers[sub_id]) — reuse would silently overwrite the first
    subscriber's engine entry (hijack). The route must 409 on any reuse.
    """
    resp = client.post(
        "/api/marketplace/publish",
        json={"strategy_id": "dup_sid_strat", "vault_address": _vaddr("aa")},
    )
    assert resp.status_code == 200

    sid = "0x" + "cc" * 32
    with patch(
        "archimedes.marketplace.wallet_provisioner.provision_subscriber_wallet",
        new=AsyncMock(return_value=("w-1", "0x0000000000000000000000000000000000000ee1")),
    ):
        r1 = client.post(
            "/api/marketplace/subscribe",
            json={"strategy_id": "dup_sid_strat", "sub_id": sid, "ephemeral_wallet": "0xeph"},
        )
    assert r1.status_code == 200, r1.text

    # A different verified wallet tries to register the SAME sub_id.
    app.dependency_overrides[require_verified_wallet] = lambda: "0x" + "77" * 20
    try:
        with patch(
            "archimedes.marketplace.wallet_provisioner.provision_subscriber_wallet",
            new=AsyncMock(return_value=("w-2", "0x0000000000000000000000000000000000000ee2")),
        ):
            r2 = client.post(
                "/api/marketplace/subscribe",
                json={"strategy_id": "dup_sid_strat", "sub_id": sid, "ephemeral_wallet": "0xeph"},
            )
        assert r2.status_code == 409, r2.text
        assert "sub_id" in r2.json()["detail"]
    finally:
        app.dependency_overrides[require_verified_wallet] = lambda: TEST_WALLET


def test_published_detail_public_surface_count_only(client, app):
    """GET /published/{id} is public: subscriber_count only, ZERO subscriber identity.

    Data-arch decision (PR #958 review): subscribers are consumers, not authors,
    so the public detail exposes only a count — never subscriber addresses,
    sub_ids, ephemeral wallets, or publisher payment plumbing
    (gateway_seller_address / agent_wallet_id).
    """
    resp = client.post(
        "/api/marketplace/publish",
        json={"strategy_id": "redact_strat", "vault_address": _vaddr("aa")},
    )
    assert resp.status_code == 200

    other_wallet = "0x" + "ab" * 20
    app.dependency_overrides[require_verified_wallet] = lambda: other_wallet
    try:
        with patch(
            "archimedes.marketplace.wallet_provisioner.provision_subscriber_wallet",
            new=AsyncMock(return_value=("w-3", "0x0000000000000000000000000000000000000ee3")),
        ):
            r = client.post(
                "/api/marketplace/subscribe",
                json={"strategy_id": "redact_strat", "sub_id": "0x" + "dd" * 32, "ephemeral_wallet": "0xeph"},
            )
        assert r.status_code == 200, r.text
    finally:
        app.dependency_overrides[require_verified_wallet] = lambda: TEST_WALLET

    detail = client.get("/api/marketplace/published/redact_strat")
    assert detail.status_code == 200
    data = detail.json()

    # subscriber_count present; the subscriber LIST is entirely absent.
    assert data["subscriber_count"] == 1
    assert "subscribers" not in data

    # No payment plumbing, no subscriber identity anywhere in the payload.
    for leaked in ("gateway_seller_address", "agent_wallet_id"):
        assert leaked not in data
    body = detail.text
    assert other_wallet.lower() not in body
    assert ("0x" + "dd" * 32) not in body
    assert "0x0000000000000000000000000000000000000ee3" not in body


def test_unsubscribe_triggers_refund_and_returns_tx(client, app):
    """Unsubscribe auto-withdraws the subscriber's remaining DCW balance back to
    their wallet (issue #975 exit) and surfaces the tx in the response."""
    resp = client.post("/api/marketplace/publish", json={"strategy_id": "refund_strat", "vault_address": _vaddr("a5")})
    assert resp.status_code == 200

    with patch(
        "archimedes.marketplace.wallet_provisioner.provision_subscriber_wallet",
        new=AsyncMock(return_value=("w-9", "0x0000000000000000000000000000000000000ee9")),
    ):
        r = client.post(
            "/api/marketplace/subscribe",
            json={"strategy_id": "refund_strat", "sub_id": "0x" + "ee" * 32, "ephemeral_wallet": "0xeph"},
        )
    assert r.status_code == 200, r.text

    # Live refund returns a tx hash; the route surfaces it.
    app.state.market.refund_subscriber = AsyncMock(return_value="0xrefundtx")
    un = client.request("DELETE", "/api/marketplace/subscribe/refund_strat")
    assert un.status_code == 200, un.text
    body = un.json()
    assert body["status"] == "unsubscribed"
    assert body["refund_tx"] == "0xrefundtx"
    # The subscriber's own SIWE wallet is the withdrawal recipient.
    kwargs = app.state.market.refund_subscriber.await_args.kwargs
    assert kwargs["to_wallet"] == TEST_WALLET


# ─── Fee-cap guard (issue #1138) ────────────────────────────────────────────
# The live VaultFactory predates the #1129 constructor caps and fee bps are
# immutable, so a pre-cap vault with hostile fees can never be fixed on-chain.
# publish (user-supplied vault_address) and subscribe (the publisher's vault)
# must refuse over-cap vaults with a 4xx naming the actual values, and must
# fail CLOSED (502) when the fees can't be read.


def _set_fees(app, mgmt: int, perf: int) -> None:
    app.state.market.executor.get_vault_fee_bps = AsyncMock(return_value=(mgmt, perf))


def test_publish_rejects_over_cap_management_fee(client, app):
    _set_fees(app, MAX_MANAGEMENT_FEE_BPS + 100, 0)
    resp = client.post(
        "/api/marketplace/publish",
        json={"strategy_id": "test_strat", "vault_address": _vaddr("bb")},
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    # The reason names the actual on-chain values and the caps.
    assert f"managementFeeBps={MAX_MANAGEMENT_FEE_BPS + 100}" in detail
    assert f"cap {MAX_MANAGEMENT_FEE_BPS}" in detail


def test_publish_rejects_over_cap_performance_fee(client, app):
    _set_fees(app, 0, MAX_PERFORMANCE_FEE_BPS + 1)
    resp = client.post(
        "/api/marketplace/publish",
        json={"strategy_id": "test_strat", "vault_address": _vaddr("bb")},
    )
    assert resp.status_code == 400, resp.text
    assert f"performanceFeeBps={MAX_PERFORMANCE_FEE_BPS + 1}" in resp.json()["detail"]


def test_publish_allows_fees_exactly_at_caps(client, app):
    """A vault at exactly the caps is allowed — the contract allows it too."""
    _set_fees(app, MAX_MANAGEMENT_FEE_BPS, MAX_PERFORMANCE_FEE_BPS)
    resp = client.post(
        "/api/marketplace/publish",
        json={"strategy_id": "test_strat", "vault_address": _vaddr("cc")},
    )
    assert resp.status_code == 200, resp.text


def test_publish_fee_read_failure_fails_closed(client, app):
    """Unreadable fees → refuse (502), never wave the vault through.

    Fail-closed is deliberate: this guard is fund-adjacent — passing a vault
    whose fees we couldn't verify exposes depositors to the C1 fee-drain.
    """
    app.state.market.executor.get_vault_fee_bps = AsyncMock(side_effect=RuntimeError("rpc down"))
    resp = client.post(
        "/api/marketplace/publish",
        json={"strategy_id": "test_strat", "vault_address": _vaddr("dd")},
    )
    assert resp.status_code == 502, resp.text
    assert "fail-closed" in resp.json()["detail"]


def test_publish_rejects_malformed_vault_address(client, app):
    """A malformed vault_address is a client error (400) caught BEFORE any
    chain read — not a 502 from the fee guard's fail-closed path."""
    resp = client.post(
        "/api/marketplace/publish",
        json={"strategy_id": "test_strat", "vault_address": "0xnot-an-address"},
    )
    assert resp.status_code == 400, resp.text
    assert "vault_address" in resp.json()["detail"]
    app.state.market.executor.get_vault_fee_bps.assert_not_awaited()


def test_publish_created_vault_skips_fee_read(client, app):
    """No user-supplied vault_address → backend creates the vault with 0/0 fees;
    the guard must NOT run (a fee-read blip must not block the create path)."""
    app.state.market.executor.get_vault_fee_bps = AsyncMock(side_effect=RuntimeError("rpc down"))
    resp = client.post(
        "/api/marketplace/publish",
        json={"strategy_id": "test_strat"},
    )
    assert resp.status_code == 200, resp.text
    app.state.market.executor.get_vault_fee_bps.assert_not_awaited()


def test_subscribe_rejects_over_cap_publisher_vault(client, app):
    """A vault published before the guard existed stays reachable in the DB —
    subscribe must re-check its fees on-chain and refuse, same as publish."""
    resp = client.post(
        "/api/marketplace/publish",
        json={"strategy_id": "test_strat", "vault_address": _vaddr("ee")},
    )
    assert resp.status_code == 200, resp.text

    # The vault's immutable on-chain fees are hostile; the publish-time check
    # is history. Subscribe reads them fresh.
    _set_fees(app, 0, MAX_PERFORMANCE_FEE_BPS + 1000)
    with patch(
        "archimedes.marketplace.wallet_provisioner.provision_subscriber_wallet",
        new=AsyncMock(return_value=("w-fee", "0x0000000000000000000000000000000000000fe1")),
    ):
        r = client.post(
            "/api/marketplace/subscribe",
            json={"strategy_id": "test_strat", "sub_id": "0x" + "fe" * 32, "ephemeral_wallet": "0xeph"},
        )
    assert r.status_code == 400, r.text
    assert f"performanceFeeBps={MAX_PERFORMANCE_FEE_BPS + 1000}" in r.json()["detail"]


def test_subscribe_fee_read_failure_fails_closed(client, app):
    resp = client.post(
        "/api/marketplace/publish",
        json={"strategy_id": "test_strat", "vault_address": _vaddr("aa")},
    )
    assert resp.status_code == 200, resp.text

    app.state.market.executor.get_vault_fee_bps = AsyncMock(side_effect=RuntimeError("rpc down"))
    with patch(
        "archimedes.marketplace.wallet_provisioner.provision_subscriber_wallet",
        new=AsyncMock(return_value=("w-fee", "0x0000000000000000000000000000000000000fe2")),
    ):
        r = client.post(
            "/api/marketplace/subscribe",
            json={"strategy_id": "test_strat", "sub_id": "0x" + "fd" * 32, "ephemeral_wallet": "0xeph"},
        )
    assert r.status_code == 502, r.text


# ─── Identity ledger (issue #1028, D1a/D2/D3) ──────────────────────────────
# _setup_db redirects to a fresh isolated tmp DB around EACH test in this
# file, so these assertions can be absolute (not delta-based) — no
# shared-process residue from other tests.


def test_publish_registers_dcw_and_ledgers_strategy_published(client):
    """Publish provisions a Circle DCW (gateway_seller_address) — it must be
    registered in controlled_wallets (D1a), controller-linked to the
    publisher's own wallet, and the publish itself must ledger a
    strategy_published event (D2) against that same wallet."""
    from archimedes.db import get_session
    from archimedes.models.identity import ControlledWallet, IdentityEvent

    resp = client.post(
        "/api/marketplace/publish",
        json={"strategy_id": "test_strat", "vault_address": _vaddr("a4")},
    )
    assert resp.status_code == 200, resp.text

    with get_session() as session:
        dcw = session.get(ControlledWallet, "0x0000000000000000000000000000000000000999")
        assert dcw is not None
        assert dcw.wallet_class == "publisher_settlement"
        assert dcw.controller_wallet == TEST_WALLET
        assert dcw.key_binding == "test-agent-wallet-uuid"

        events = session.query(IdentityEvent).filter(IdentityEvent.wallet == TEST_WALLET).all()
        matching = [e for e in events if e.event_type == "strategy_published"]
        assert len(matching) == 1
        assert matching[0].actor_class == "human"
        assert matching[0].meta["strategy_id"] == "test_strat"


def test_subscribe_registers_dcw_and_ledgers_marketplace_subscribed(client):
    """Subscribe provisions a Circle DCW (ephemeral_wallet) — registered in
    controlled_wallets (D1a) and controller-linked to the subscriber; the
    subscribe itself ledgers a marketplace_subscribed event (D2)."""
    from archimedes.db import get_session
    from archimedes.models.identity import ControlledWallet, IdentityEvent

    pub = client.post("/api/marketplace/publish", json={"strategy_id": "test_strat", "vault_address": _vaddr("aa")})
    assert pub.status_code == 200, pub.text

    dcw_address = "0x0000000000000000000000000000000000000eee"
    with patch(
        "archimedes.marketplace.wallet_provisioner.provision_subscriber_wallet",
        new=AsyncMock(return_value=("sub-wallet-uuid", dcw_address)),
    ):
        resp = client.post(
            "/api/marketplace/subscribe",
            json={"strategy_id": "test_strat", "sub_id": "0x" + "ab" * 32, "ephemeral_wallet": "0xeph"},
        )
    assert resp.status_code == 200, resp.text

    with get_session() as session:
        dcw = session.get(ControlledWallet, dcw_address)
        assert dcw is not None
        assert dcw.wallet_class == "subscriber_payment"
        assert dcw.controller_wallet == TEST_WALLET
        assert dcw.key_binding == "sub-wallet-uuid"

        events = session.query(IdentityEvent).filter(IdentityEvent.wallet == TEST_WALLET).all()
        matching = [e for e in events if e.event_type == "marketplace_subscribed"]
        assert len(matching) == 1
        assert matching[0].meta["strategy_id"] == "test_strat"
        assert matching[0].meta["sub_id"] == "0x" + "ab" * 32


def test_unsubscribe_ledgers_marketplace_unsubscribed(client, app):
    """Unsubscribe ledgers a marketplace_unsubscribed event (D2) against the
    subscriber's own wallet, carrying the refund tx in meta."""
    from archimedes.db import get_session
    from archimedes.models.identity import IdentityEvent

    pub = client.post("/api/marketplace/publish", json={"strategy_id": "refund_strat", "vault_address": _vaddr("a5")})
    assert pub.status_code == 200

    with patch(
        "archimedes.marketplace.wallet_provisioner.provision_subscriber_wallet",
        new=AsyncMock(return_value=("w-9", "0x0000000000000000000000000000000000000ee9")),
    ):
        sub = client.post(
            "/api/marketplace/subscribe",
            json={"strategy_id": "refund_strat", "sub_id": "0x" + "ee" * 32, "ephemeral_wallet": "0xeph"},
        )
    assert sub.status_code == 200, sub.text

    app.state.market.refund_subscriber = AsyncMock(return_value="0xrefundtx")
    un = client.request("DELETE", "/api/marketplace/subscribe/refund_strat")
    assert un.status_code == 200, un.text

    with get_session() as session:
        events = session.query(IdentityEvent).filter(IdentityEvent.wallet == TEST_WALLET).all()
        matching = [e for e in events if e.event_type == "marketplace_unsubscribed"]
        assert len(matching) == 1
        assert matching[0].meta["strategy_id"] == "refund_strat"
        assert matching[0].meta["refund_tx"] == "0xrefundtx"


# ---------------------------------------------------------------------------
# Roadmap gate (#1432): publish and subscribe are not offered
# ---------------------------------------------------------------------------
#
# Both routes have the backend signer deploy a vault owned by the caller's
# wallet (publish when no vault_address is given, subscribe always), so both
# sit behind the same FEATURE_ROADMAP_SURFACES gate as POST /api/vaults/create.
# While it is off, nothing in either handler may run: no Circle wallet, no
# vault, no pool, no fee or balance read, no spend-cap read, no row, no
# identity event. The exact set of gated routes is pinned in
# tests/test_vault_create_roadmap_gate.py.

GATE_404 = {"detail": "Not offered: roadmap, not shipped"}
FLAG_OFF_VALUES = [None, "", "false", "0", "1", "yes", "on", "ture"]


def _flag(monkeypatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv("FEATURE_ROADMAP_SURFACES", raising=False)
    else:
        monkeypatch.setenv("FEATURE_ROADMAP_SURFACES", value)


def _marketplace_rows(role: str) -> int:
    from archimedes.db import get_session
    from archimedes.models.marketplace import MarketplaceAgent

    with get_session() as session:
        return session.query(MarketplaceAgent).filter(MarketplaceAgent.role == role).count()


def _reset_market_mocks(market) -> None:
    for mock in (
        market.executor.create_vault,
        market.executor.get_vault_fee_bps,
        market._usdc_balance_of,
        market.signer.execute_contract,
        market.start_publisher,
        market.add_subscriber,
    ):
        mock.reset_mock()


def _assert_market_untouched(market) -> None:
    market.executor.create_vault.assert_not_called()  # no vault deployed, no gas spent
    market.executor.get_vault_fee_bps.assert_not_called()
    market._usdc_balance_of.assert_not_called()
    market.signer.execute_contract.assert_not_called()  # no createPool
    market.start_publisher.assert_not_called()
    market.add_subscriber.assert_not_called()


@pytest.mark.parametrize("value", FLAG_OFF_VALUES)
def test_flag_off_publish_refused_before_anything_runs(client, app, monkeypatch, value):
    """No vault_address: with the flag on this body deploys a vault for the caller."""
    _flag(monkeypatch, value)
    with (
        patch(
            "archimedes.marketplace.wallet_provisioner.provision_publisher_wallet",
            new=AsyncMock(return_value=("w", "0x" + "09" * 20)),
        ) as m_provision,
        patch("archimedes.api.marketplace_routes.wallet_can_publish", return_value=True) as m_can_publish,
        patch("archimedes.api.marketplace_routes.register_controlled_wallet") as m_register,
        patch("archimedes.api.marketplace_routes.emit_identity_event") as m_event,
    ):
        resp = client.post("/api/marketplace/publish", json={"strategy_id": "test_strat"})

    assert resp.status_code == 404, resp.text
    assert resp.json() == GATE_404
    _assert_market_untouched(app.state.market)
    m_provision.assert_not_awaited()
    m_can_publish.assert_not_called()
    m_register.assert_not_called()
    m_event.assert_not_called()
    assert _marketplace_rows("publisher") == 0


@pytest.mark.parametrize("value", FLAG_OFF_VALUES)
def test_flag_off_subscribe_refused_before_anything_runs(client, app, monkeypatch, value):
    """A running publisher exists (published while the flag was on), so with the
    flag on this subscribe would provision a Circle wallet and deploy a vault."""
    pub = client.post("/api/marketplace/publish", json={"strategy_id": "test_strat", "vault_address": _vaddr("b1")})
    assert pub.status_code == 200, pub.text
    _reset_market_mocks(app.state.market)

    _flag(monkeypatch, value)
    with (
        patch(
            "archimedes.marketplace.wallet_provisioner.provision_subscriber_wallet",
            new=AsyncMock(return_value=("w", "0x" + "0a" * 20)),
        ) as m_provision,
        patch("archimedes.api.marketplace_routes.spend_cap.is_over_cap", new=AsyncMock(return_value=False)) as m_cap,
        patch("archimedes.api.marketplace_routes.register_controlled_wallet") as m_register,
        patch("archimedes.api.marketplace_routes.emit_identity_event") as m_event,
    ):
        resp = client.post(
            "/api/marketplace/subscribe",
            json={"strategy_id": "test_strat", "sub_id": "0x" + "b2" * 32, "ephemeral_wallet": "0xeph"},
        )

    assert resp.status_code == 404, resp.text
    assert resp.json() == GATE_404
    _assert_market_untouched(app.state.market)
    m_provision.assert_not_awaited()
    m_cap.assert_not_awaited()
    m_register.assert_not_called()
    m_event.assert_not_called()
    assert _marketplace_rows("subscriber") == 0


@pytest.mark.parametrize("path", ["/api/marketplace/publish", "/api/marketplace/subscribe"])
def test_flag_off_answers_404_before_auth(client, app, monkeypatch, path):
    """No session: still 404, not 401, so the refusal does not tell an anonymous
    caller what it would need. With the flag on the same request is a 401."""
    app.dependency_overrides.pop(require_verified_wallet, None)
    body = {"strategy_id": "test_strat", "sub_id": "0x" + "b3" * 32}

    _flag(monkeypatch, None)
    off = client.post(path, json=body)
    assert off.status_code == 404, off.text
    assert off.json() == GATE_404

    _flag(monkeypatch, "true")
    on = client.post(path, json=body)
    assert on.status_code == 401, on.text


@pytest.mark.parametrize("path", ["/api/marketplace/publish", "/api/marketplace/subscribe"])
def test_flag_off_answers_404_before_body_schema_validation(client, monkeypatch, path):
    """A JSON array is valid JSON but not the ``dict`` both handlers declare.
    With the flag on that is FastAPI's 422; with it off, the gate answers first."""
    _flag(monkeypatch, "true")
    on = client.post(path, json=["not", "an", "object"])
    assert on.status_code == 422, on.text

    _flag(monkeypatch, None)
    off = client.post(path, json=["not", "an", "object"])
    assert off.status_code == 404, off.text
    assert off.json() == GATE_404


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("DELETE", "/api/marketplace/publish/{sid}"),
        ("DELETE", "/api/marketplace/subscribe/{sid}"),
        ("POST", "/api/marketplace/publish/{sid}/withdraw"),
    ],
)
def test_flag_off_leaves_the_exit_routes_open(client, app, monkeypatch, method, path):
    """Stop-publish, unsubscribe and withdraw are how an existing publisher or
    subscriber gets out, or gets money back (unsubscribe refunds the prepaid-fee
    balance; withdraw pays out publisher earnings, a no-op under
    PAYMENTS_DRY_RUN). None of them deploys a vault, so the gate leaves them
    alone: flag off, they still answer."""
    pub = client.post("/api/marketplace/publish", json={"strategy_id": "refund_strat", "vault_address": _vaddr("b4")})
    assert pub.status_code == 200, pub.text
    with patch(
        "archimedes.marketplace.wallet_provisioner.provision_subscriber_wallet",
        new=AsyncMock(return_value=("w-exit", "0x" + "0b" * 20)),
    ):
        sub = client.post(
            "/api/marketplace/subscribe",
            json={"strategy_id": "refund_strat", "sub_id": "0x" + "b5" * 32, "ephemeral_wallet": "0xeph"},
        )
    assert sub.status_code == 200, sub.text

    _flag(monkeypatch, None)
    resp = client.request(method, path.format(sid="refund_strat"))
    assert resp.status_code == 200, resp.text
