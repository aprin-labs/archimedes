"""Unit tests for the circlekit seam. circlekit is mocked at the
module boundary — no live facilitator calls in CI."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from archimedes.marketplace import payments

WALLET_ID = "wallet-abc-123"
WALLET_ADDR = "0xSubscriber0000000000000000000000000000001"
SELLER_ADDR = "0xSeller000000000000000000000000000000000001"


def test_fee_to_price_basic():
    # 100 raw units = $0.0001; 3 actions = $0.0003
    assert payments.fee_to_price(3, 100) == "$0.000300"


def test_fee_to_price_zero():
    assert payments.fee_to_price(0, 100) == "$0.000000"


def test_fee_to_price_no_float_drift():
    # 1_000_000 raw = exactly $1
    assert payments.fee_to_price(1, 1_000_000) == "$1.000000"


def test_fee_to_price_negative_raises():
    with pytest.raises(ValueError):
        payments.fee_to_price(-1, 100)


@pytest.mark.asyncio
async def test_charge_zero_amount_is_paid_without_network():
    with patch.object(payments, "get_gateway_middleware"):
        ok = await payments.charge(
            sub_id="0x" + "11" * 32,
            wallet_id=WALLET_ID,
            wallet_address=WALLET_ADDR,
            seller_address=SELLER_ADDR,
            strategy_id="s",
            tick_id="t",
            action_count=0,
            flat_fee_raw=100,
        )
    assert ok is True


@pytest.mark.asyncio
async def test_charge_success_path():
    """The happy path, including the AMOUNT handed to circlekit.

    MUTATION: `payments.py:141` -> `fee_to_price(min(action_count, 1), ...)`.

    This test already passed action_count=2 but only asserted that settle was
    awaited, so it never saw the price: the whole marketplace package (202
    tests) stayed green while every multi-action tick under-charged to a
    single action. `test_fee_to_price_basic` pins the helper, not this call
    site — nothing connected the two until the assertions below.

    All three circlekit legs are checked with the same string: require (the
    402 the publisher advertises), verify, and settle. A price that drifts
    between advertise and settle is its own money bug.
    """
    middleware = MagicMock()
    middleware.require.return_value = {"status": 402, "headers": {}, "body": {}}
    middleware.verify = AsyncMock(return_value=MagicMock(is_valid=True))
    middleware.settle = AsyncMock()
    fake_reqs = MagicMock()
    fake_x402 = MagicMock()
    fake_x402.get_gateway_option.return_value = fake_reqs
    with (
        patch.object(payments, "get_gateway_middleware", return_value=middleware),
        patch.object(payments, "get_payment_required", return_value=fake_x402),
        patch.object(payments, "_get_signer"),
        patch.object(payments, "create_payment_header", return_value="hdr"),
    ):
        ok = await payments.charge(
            sub_id="0x" + "11" * 32,
            wallet_id=WALLET_ID,
            wallet_address=WALLET_ADDR,
            seller_address=SELLER_ADDR,
            strategy_id="s",
            tick_id="t",
            action_count=2,
            flat_fee_raw=100,
        )
    assert ok is True
    middleware.settle.assert_awaited_once()

    # 2 actions x 100 raw units (6-decimal USDC) = $0.000200, not $0.000100.
    expected_price = "$0.000200"
    assert middleware.require.call_args.args[0] == expected_price  # require(price, path)
    assert middleware.verify.await_args.args[1] == expected_price  # verify(header, price)
    assert middleware.settle.await_args.args[1] == expected_price  # settle(header, price)


@pytest.mark.asyncio
async def test_charge_verify_invalid_returns_false():
    middleware = MagicMock()
    middleware.require.return_value = {"status": 402, "headers": {}, "body": {}}
    middleware.verify = AsyncMock(return_value=MagicMock(is_valid=False, invalid_reason="insufficient"))
    middleware.settle = AsyncMock()
    fake_x402 = MagicMock()
    fake_x402.get_gateway_option.return_value = MagicMock()
    with (
        patch.object(payments, "get_gateway_middleware", return_value=middleware),
        patch.object(payments, "get_payment_required", return_value=fake_x402),
        patch.object(payments, "_get_signer"),
        patch.object(payments, "create_payment_header", return_value="hdr"),
    ):
        ok = await payments.charge(
            sub_id="0x" + "11" * 32,
            wallet_id=WALLET_ID,
            wallet_address=WALLET_ADDR,
            seller_address=SELLER_ADDR,
            strategy_id="s",
            tick_id="t",
            action_count=2,
            flat_fee_raw=100,
        )
    assert ok is False
    middleware.settle.assert_not_awaited()


@pytest.mark.asyncio
async def test_charge_exception_returns_false():
    with patch.object(payments, "get_gateway_middleware", side_effect=RuntimeError("no seller")):
        ok = await payments.charge(
            sub_id="0x" + "11" * 32,
            wallet_id=WALLET_ID,
            wallet_address=WALLET_ADDR,
            seller_address=SELLER_ADDR,
            strategy_id="s",
            tick_id="t",
            action_count=2,
            flat_fee_raw=100,
        )
    assert ok is False


def test_get_gateway_middleware_zero_address_raises():
    with pytest.raises(RuntimeError):
        payments.get_gateway_middleware("0x0000000000000000000000000000000000000000")


def test_get_gateway_middleware_caches_per_address():
    addr1 = "0x" + "01" * 20
    addr2 = "0x" + "02" * 20
    payments._middleware_cache.clear()
    mw1 = payments.get_gateway_middleware(addr1)
    mw2 = payments.get_gateway_middleware(addr1)
    mw3 = payments.get_gateway_middleware(addr2)
    assert mw1 is mw2  # same address returns cached
    assert mw1 is not mw3  # different addresses are distinct


def test_get_signer_caches_per_wallet_id():
    payments._signer_cache.clear()
    with patch.object(payments, "CircleWalletSigner") as mock_cls:
        mock_cls.side_effect = lambda wallet_id, wallet_address: MagicMock(
            wallet_id=wallet_id,
            wallet_address=wallet_address,
        )
        s1 = payments._get_signer("wid-1", WALLET_ADDR)
        s2 = payments._get_signer("wid-1", "0xOther")
        s3 = payments._get_signer("wid-2", WALLET_ADDR)
        assert s1 is s2  # same wallet_id returns cached
        assert s1 is not s3  # different wallet_ids are distinct
        assert mock_cls.call_count == 2  # only wid-1 and wid-2 constructed
