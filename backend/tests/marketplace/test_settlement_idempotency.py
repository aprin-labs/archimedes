"""Money-safety tests for PR #958: x402 charge idempotency (#7) + auto-withdraw
on unsubscribe (#8).

x402 / Circle Gateway is NOT idempotent for a crash-retry (a retry signs a
fresh EIP-3009 nonce that settles as a new payment). The SettlementIntent row
is the load-bearing guard; these tests pin its behaviour and the unsubscribe
refund path.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from archimedes.db import get_session
from archimedes.marketplace.service import MarketService, Publisher, Subscriber
from archimedes.marketplace.tick_registry import TickStep
from archimedes.models.marketplace import SettlementIntent

# DB isolation for this file's tests comes from the autouse fixture in
# backend/tests/marketplace/conftest.py (see tests/db_isolation.py) — it
# replaces a create_all/drop_all pair that operated on `engine` captured at
# import time, which silently stopped matching what get_session() resolved
# to once another file in this directory redirected archimedes.db.engine
# without restoring it (issue #1100).


@pytest.fixture(autouse=True)
def _spend_cap_default_not_over():
    """_charge_one reserves against spend_cap.try_reserve_usdc before every
    charge (#713, atomicity fix in #1099 review). Default every test in this
    file to "reserved successfully" so none of them reach out to a real Redis
    connection — the spend-cap section below overrides this per-test to
    exercise the refusal path."""
    with patch("archimedes.marketplace.service.spend_cap.try_reserve_usdc", new=AsyncMock(return_value=True)):
        yield


def _svc(dry_run: bool = False) -> MarketService:
    return MarketService(interval_seconds=9999, payments_dry_run=dry_run, paper_trading=True)


def _pub():
    return Publisher(
        strategy_id="strat_a",
        pool_id="0x" + "aa" * 32,
        vault_address="0xvault",
        creator_wallet="0xcreator",
        gateway_seller_address="0xgateway_seller",
    )


def _sub(sub_id="0x" + "cc" * 32):
    return Subscriber(
        sub_id=sub_id,
        pool_id="0x" + "bb" * 32,
        vault_address="0xsubvault",
        ephemeral_wallet="0xdcw",
        subscriber_wallet="0xsubscriber",
        circle_wallet_id="wallet-uuid",
    )


# ── #7 — idempotency guard ────────────────────────────────────────────────


def test_claim_settlement_intent_lifecycle():
    svc = _svc()
    # Unique tick_id per test — assertions are scoped to this logical charge so
    # they never depend on cross-test DB isolation.
    key = {"strategy_id": "strat_a", "tick_id": "lifecycle:1", "sub_id": "0x" + "cc" * 32, "step": "rebalance"}

    assert svc._claim_settlement_intent(**key) == "claimed"
    # A second claim for the same logical charge is blocked (pending in-flight).
    assert svc._claim_settlement_intent(**key) == "in_flight"

    # Once finalized as settled, a re-claim reports already-settled (idempotent).
    svc._finalize_settlement_intent(**key, settled=True)
    assert svc._claim_settlement_intent(**key) == "already_settled"

    with get_session() as session:
        row = session.query(SettlementIntent).filter_by(**key).one()
        assert row.status == "settled"
        assert row.settled_at is not None


async def test_charge_one_settles_once_then_skips_on_retry():
    svc = _svc()
    pub, sub = _pub(), _sub()
    tick = "settle-once:1"
    with patch("archimedes.marketplace.payments.charge", new=AsyncMock(return_value=True)) as m_charge:
        first_paid, first_override, first_suppressed = await svc._charge_one(
            pub, sub, "strat_a", tick, TickStep.REBALANCE, 1
        )
        second_paid, second_override, second_suppressed = await svc._charge_one(
            pub, sub, "strat_a", tick, TickStep.REBALANCE, 1
        )

    assert first_paid is True
    assert first_override is None
    assert first_suppressed is False
    # The retry of the SAME (strategy, tick, sub, step) returns paid WITHOUT a
    # second settle — the idempotency guard prevented a double charge.
    assert second_paid is True
    assert second_override is None
    assert second_suppressed is False
    assert m_charge.await_count == 1


async def test_charge_one_in_flight_pending_does_not_recharge():
    """A pre-existing PENDING intent (a prior attempt that settled-but-crashed
    before recording) must block a re-charge, not settle again."""
    svc = _svc()
    pub, sub = _pub(), _sub()
    tick = "in-flight:1"
    # Simulate a crashed prior attempt: pending intent already present (use the
    # enum value the charge path writes, TickStep.REBALANCE.value == "rebalance").
    svc._claim_settlement_intent("strat_a", tick, sub.sub_id, TickStep.REBALANCE.value)

    with patch("archimedes.marketplace.payments.charge", new=AsyncMock(return_value=True)) as m_charge:
        paid, halt_reason_override, charge_suppressed = await svc._charge_one(
            pub, sub, "strat_a", tick, TickStep.REBALANCE, 1
        )

    assert paid is False
    assert halt_reason_override is None
    assert charge_suppressed is False
    m_charge.assert_not_awaited()


async def test_charge_one_dry_run_never_claims_or_charges():
    svc = _svc(dry_run=True)
    pub, sub = _pub(), _sub()
    tick = "dry-run:1"
    with patch("archimedes.marketplace.payments.charge", new=AsyncMock(return_value=True)) as m_charge:
        paid, halt_reason_override, charge_suppressed = await svc._charge_one(
            pub, sub, "strat_a", tick, TickStep.REBALANCE, 1
        )
    assert paid is True
    assert halt_reason_override is None
    assert charge_suppressed is False
    m_charge.assert_not_awaited()
    # Scoped to THIS charge's key (never claims under dry-run) — independent of
    # any rows other tests may have left in a shared DB.
    with get_session() as session:
        assert session.query(SettlementIntent).filter_by(tick_id=tick).count() == 0


# ── #713 — spend cap ───────────────────────────────────────────────────────


async def test_charge_one_refuses_when_over_spend_cap():
    """A subscriber wallet at/over its rolling 24h spend cap is refused with
    the specific override reason and never reaches payments.charge.

    The settlement-intent slot IS claimed (and finalized as failed) before
    the refusal — reordered in the #1099 review so the spend-cap reservation
    sits right before payments.charge(), gated behind the idempotency claim.
    That ordering is what makes the reservation itself safe against a
    crash-retry (see try_reserve_usdc's docstring); the cost is a `failed`
    SettlementIntent row for the refused attempt instead of none at all."""
    from archimedes.marketplace.service import FLAT_FEE_PER_ACTION

    svc = _svc()
    pub, sub = _pub(), _sub()
    tick = "spend-cap:1"
    action_count = 1
    with (
        patch(
            "archimedes.marketplace.service.spend_cap.try_reserve_usdc", new=AsyncMock(return_value=False)
        ) as m_reserve,
        patch("archimedes.marketplace.payments.charge", new=AsyncMock(return_value=True)) as m_charge,
    ):
        paid, halt_reason_override, charge_suppressed = await svc._charge_one(
            pub, sub, "strat_a", tick, TickStep.REBALANCE, action_count
        )

    assert paid is False
    assert halt_reason_override == "24h spend cap reached"
    assert charge_suppressed is False
    m_charge.assert_not_awaited()
    m_reserve.assert_awaited_once_with(
        sub.subscriber_wallet, action_count * FLAT_FEE_PER_ACTION, f"{tick}:{sub.sub_id}:{TickStep.REBALANCE.value}"
    )

    with get_session() as session:
        row = (
            session.query(SettlementIntent)
            .filter_by(strategy_id="strat_a", tick_id=tick, sub_id=sub.sub_id, step=TickStep.REBALANCE.value)
            .first()
        )
        assert row is not None
        assert row.status == "failed"


async def test_charge_one_under_cap_proceeds_normally():
    """Sanity-checks the mock boundary the opposite way: when the reservation
    succeeds, the charge proceeds and nothing gets released — guards against
    an accidentally-inverted cap check or a spurious release on the happy path."""
    svc = _svc()
    pub, sub = _pub(), _sub()
    tick = "spend-cap:2"
    with (
        patch("archimedes.marketplace.service.spend_cap.try_reserve_usdc", new=AsyncMock(return_value=True)),
        patch("archimedes.marketplace.payments.charge", new=AsyncMock(return_value=True)) as m_charge,
        patch("archimedes.marketplace.service.spend_cap.release_reservation", new=AsyncMock()) as m_release,
    ):
        paid, halt_reason_override, charge_suppressed = await svc._charge_one(
            pub, sub, "strat_a", tick, TickStep.REBALANCE, 1
        )

    assert paid is True
    assert halt_reason_override is None
    assert charge_suppressed is False
    m_charge.assert_awaited_once()
    m_release.assert_not_awaited()


async def test_charge_one_reserves_with_correct_wallet_and_amount():
    """A charge reserves against the SUBSCRIBER wallet (not sub_id) for
    action_count * FLAT_FEE_PER_ACTION raw units — the exact amount
    payments.charge was invoked for — BEFORE payments.charge runs."""
    from archimedes.marketplace.service import FLAT_FEE_PER_ACTION

    svc = _svc()
    pub, sub = _pub(), _sub()
    tick = "spend-cap:3"
    action_count = 3
    with (
        patch(
            "archimedes.marketplace.service.spend_cap.try_reserve_usdc", new=AsyncMock(return_value=True)
        ) as m_reserve,
        patch("archimedes.marketplace.payments.charge", new=AsyncMock(return_value=True)),
    ):
        paid, _, _ = await svc._charge_one(pub, sub, "strat_a", tick, TickStep.REBALANCE, action_count)

    assert paid is True
    m_reserve.assert_awaited_once_with(
        sub.subscriber_wallet, action_count * FLAT_FEE_PER_ACTION, f"{tick}:{sub.sub_id}:{TickStep.REBALANCE.value}"
    )


async def test_charge_one_releases_reservation_when_charge_fails():
    """release_reservation must fire when a RESERVED charge's payments.charge
    then fails — otherwise the wallet's rolling window would overstate its
    spend for money that was never actually moved."""
    from archimedes.marketplace.service import FLAT_FEE_PER_ACTION

    svc = _svc()
    pub, sub = _pub(), _sub()
    tick = "spend-cap:4"
    with (
        patch("archimedes.marketplace.service.spend_cap.try_reserve_usdc", new=AsyncMock(return_value=True)),
        patch("archimedes.marketplace.payments.charge", new=AsyncMock(return_value=False)),
        patch("archimedes.marketplace.service.spend_cap.release_reservation", new=AsyncMock()) as m_release,
    ):
        paid, _, _ = await svc._charge_one(pub, sub, "strat_a", tick, TickStep.REBALANCE, 1)

    assert paid is False
    m_release.assert_awaited_once_with(
        sub.subscriber_wallet, f"{tick}:{sub.sub_id}:{TickStep.REBALANCE.value}", FLAT_FEE_PER_ACTION
    )


async def test_charge_one_releases_reservation_when_charge_raises():
    """payments.charge documents "never raises", but the reservation must not
    depend on that contract holding forever. A raise escaping _charge_one used
    to leave the reserved amount stuck in the wallet's window for 24h and the
    intent pending — now it's treated as a failed charge: released, finalized
    failed, (False, None, False) returned instead of propagating."""
    from archimedes.marketplace.service import FLAT_FEE_PER_ACTION

    svc = _svc()
    pub, sub = _pub(), _sub()
    tick = "spend-cap:5"
    with (
        patch("archimedes.marketplace.service.spend_cap.try_reserve_usdc", new=AsyncMock(return_value=True)),
        patch("archimedes.marketplace.payments.charge", new=AsyncMock(side_effect=RuntimeError("gateway blew up"))),
        patch("archimedes.marketplace.service.spend_cap.release_reservation", new=AsyncMock()) as m_release,
    ):
        paid, halt_reason_override, charge_suppressed = await svc._charge_one(
            pub, sub, "strat_a", tick, TickStep.REBALANCE, 1
        )

    assert paid is False
    assert halt_reason_override is None
    assert charge_suppressed is False
    m_release.assert_awaited_once_with(
        sub.subscriber_wallet, f"{tick}:{sub.sub_id}:{TickStep.REBALANCE.value}", FLAT_FEE_PER_ACTION
    )
    with get_session() as session:
        row = (
            session.query(SettlementIntent)
            .filter_by(strategy_id="strat_a", tick_id=tick, sub_id=sub.sub_id, step=TickStep.REBALANCE.value)
            .first()
        )
        assert row is not None
        assert row.status == "failed"


# ── #8 — auto-withdraw on unsubscribe ─────────────────────────────────────


async def test_refund_subscriber_noop_under_dry_run():
    svc = _svc(dry_run=True)
    svc._sweeper = MagicMock()
    svc._sweeper.withdraw_subscriber = AsyncMock(return_value="0xtx")
    tx = await svc.refund_subscriber(
        circle_wallet_id="w", dcw_address="0xdcw", to_wallet="0xsub", sub_id="0x" + "cc" * 32
    )
    assert tx is None
    svc._sweeper.withdraw_subscriber.assert_not_awaited()


async def test_refund_subscriber_sweeps_when_live():
    svc = _svc(dry_run=False)
    svc._sweeper = MagicMock()
    svc._sweeper.withdraw_subscriber = AsyncMock(return_value="0xtx")
    tx = await svc.refund_subscriber(
        circle_wallet_id="w", dcw_address="0xdcw", to_wallet="0xsub", sub_id="0x" + "cc" * 32
    )
    assert tx == "0xtx"
    svc._sweeper.withdraw_subscriber.assert_awaited_once()
    kwargs = svc._sweeper.withdraw_subscriber.await_args.kwargs
    assert kwargs["to_wallet"] == "0xsub"
    assert kwargs["dcw_address"] == "0xdcw"
