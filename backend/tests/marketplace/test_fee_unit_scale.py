"""G3: pin the fee's UNIT SCALE with literals, and let the real ``_charge_one`` run.

Audit finding (2026-08-18): every existing charge test re-derived its
expectation from the live ``FLAT_FEE_PER_ACTION`` symbol and every ``tick()``
test monkeypatched the charge away — so a 10⁶ unit-scale regression would have
kept the whole suite green at 100% file coverage. The literals below are the
only assertions in the repo that break when the SCALE (not the logic) moves.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from archimedes.marketplace.service import FLAT_FEE_PER_ACTION, MarketService, Publisher, Subscriber
from archimedes.marketplace.tick_registry import TickStep


def test_flat_fee_per_action_is_100_raw_microusdc():
    # 100 raw 6-decimal USDC units = $0.000100 per action. If this fails,
    # either the price changed on purpose (update this literal AND the
    # fee_to_price literals below together) or a unit-scale regression is
    # trying to ship. Deliberately a literal — never re-derive from the
    # constant under test.
    assert FLAT_FEE_PER_ACTION == 100


def test_fee_to_price_pins_the_six_decimal_dollar_scale():
    from archimedes.marketplace.payments import fee_to_price

    assert fee_to_price(1, 100) == "$0.000100"  # one action at the shipped fee
    assert fee_to_price(3, 100) == "$0.000300"
    assert fee_to_price(0, 100) == "$0.000000"  # zero-action tick is free
    assert fee_to_price(1, 1_000_000) == "$1.000000"  # 10^6 raw = exactly one dollar


def _live_service_with_one_sub():
    svc = MarketService(interval_seconds=9999, payments_dry_run=False, paper_trading=True)
    svc._claim_settlement_intent = MagicMock(return_value="claimed")
    svc._finalize_settlement_intent = MagicMock()
    pub = Publisher(
        strategy_id="strat_fee",
        pool_id="0x" + "aa" * 32,
        vault_address="0xpub_vault",
        creator_wallet="0xpublisher",
        gateway_seller_address="0xgateway_seller",
    )
    sub = Subscriber(
        sub_id="0x" + "cd" * 32,
        pool_id="0x" + "bb" * 32,
        vault_address="0xsub_vault",
        ephemeral_wallet="0xephemeral",
        subscriber_wallet="0xsubscriber",
        active=True,
        circle_wallet_id="wallet_circle_id",
    )
    return svc, pub, sub


@pytest.mark.asyncio
async def test_real_charge_one_debits_the_literal_fee_from_the_right_wallets():
    """The REAL ``_charge_one`` runs (payments_dry_run=False) against a stubbed
    payment client: the debited flat fee is the literal 100 raw, the spend-cap
    reservation matches it exactly, and the charge is billed from the
    subscriber's Circle wallet to the publisher's seller address."""
    svc, pub, sub = _live_service_with_one_sub()

    charged: dict = {}
    reserved: dict = {}

    async def fake_charge(**kwargs):
        charged.update(kwargs)
        return True

    async def fake_reserve(wallet, amount, charge_id):
        reserved.update(wallet=wallet, amount=amount, charge_id=charge_id)
        return True

    with (
        patch("archimedes.marketplace.service.payments.charge", fake_charge),
        patch("archimedes.marketplace.service.spend_cap.try_reserve_usdc", fake_reserve),
    ):
        paid, override, charge_suppressed = await svc._charge_one(
            pub, sub, "strat_fee", "tick-1", TickStep.LOAD_STRATEGY, action_count=1
        )

    assert paid is True and override is None and charge_suppressed is False
    assert charged["flat_fee_raw"] == 100  # the literal — not a re-derivation
    assert reserved["amount"] == 100  # reservation and charge must agree on scale
    assert reserved["wallet"] == "0xsubscriber"  # cap is per SIWE wallet, not per sub
    assert charged["wallet_id"] == "wallet_circle_id"
    assert charged["wallet_address"] == "0xephemeral"
    assert charged["seller_address"] == "0xgateway_seller"
    assert charged["action_count"] == 1
    svc._finalize_settlement_intent.assert_called_once_with(
        "strat_fee", "tick-1", sub.sub_id, TickStep.LOAD_STRATEGY.value, settled=True
    )


@pytest.mark.asyncio
async def test_failed_charge_releases_the_exact_reserved_amount():
    """A refused charge must hand back precisely what it reserved — same
    wallet, same charge id, same raw amount — or the 24h cap silently leaks."""
    svc, pub, sub = _live_service_with_one_sub()

    released: dict = {}

    async def fake_charge(**kwargs):
        return False

    async def fake_reserve(wallet, amount, charge_id):
        return True

    async def fake_release(wallet, charge_id, amount):
        released.update(wallet=wallet, charge_id=charge_id, amount=amount)

    with (
        patch("archimedes.marketplace.service.payments.charge", fake_charge),
        patch("archimedes.marketplace.service.spend_cap.try_reserve_usdc", fake_reserve),
        patch("archimedes.marketplace.service.spend_cap.release_reservation", fake_release),
    ):
        paid, _, _ = await svc._charge_one(pub, sub, "strat_fee", "tick-2", TickStep.LOAD_STRATEGY, action_count=1)

    assert paid is False
    assert released["amount"] == 100
    assert released["wallet"] == "0xsubscriber"
    assert released["charge_id"] == f"tick-2:{sub.sub_id}:{TickStep.LOAD_STRATEGY.value}"
    svc._finalize_settlement_intent.assert_called_once_with(
        "strat_fee", "tick-2", sub.sub_id, TickStep.LOAD_STRATEGY.value, settled=False
    )
