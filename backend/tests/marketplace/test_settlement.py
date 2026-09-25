"""Tests for SettlementSweeper (P5).

All SDK/clients are mocked at module boundary.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from archimedes.marketplace.settlement import (
    _THRESHOLD_RAW,
    GATEWAY_CHAIN,
    SWEEP_MIN_DEPOSIT_RAW,
    SettlementSweeper,
)

from tests.gateway_fake import FakeGatewayClient


@pytest.fixture
def settings():
    s = MagicMock()
    s.payment_splitter_address = "0xSplitter0000000000000000000000000000000001"
    s.usdc_address = "0xUsdc000000000000000000000000000000000001"
    return s


@pytest.fixture
def pub():
    p = MagicMock()
    p.strategy_id = "strat_1"
    p.pool_id = "0xpool0000000000000000000000000000000000000000000001"
    p.gateway_seller_address = "0xAgent000000000000000000000000000000000001"
    p.agent_wallet_id = "wallet-abc-123"
    return p


@pytest.fixture
def sweeper(settings):
    return SettlementSweeper(settings)


@pytest.fixture
def splitter_addr():
    return "0xSplitter0000000000000000000000000000000001"


# ── PAYMENTS_DRY_RUN guard (issue: manual withdraw endpoint bypassed dry-run) ──
# Every fund-moving method must be an inert no-op under dry-run, moving no real
# value and never touching a signer/executor/RPC. The guard lives in the sweeper
# so no caller can forget it.


@pytest.mark.asyncio
async def test_dry_run_sweep_publisher_is_noop(settings, pub):
    """Under PAYMENTS_DRY_RUN, sweep_publisher moves no value — no signer/executor touched."""
    sweeper = SettlementSweeper(settings, payments_dry_run=True)
    sweeper._get_signer = MagicMock(side_effect=AssertionError("signer must not run under dry-run"))
    sweeper._get_executor = MagicMock(side_effect=AssertionError("executor must not run under dry-run"))
    result = await sweeper.sweep_publisher(pub)
    assert result is None
    sweeper._get_signer.assert_not_called()
    sweeper._get_executor.assert_not_called()


@pytest.mark.asyncio
async def test_dry_run_withdraw_publisher_returns_none_without_chain_call(settings, pub):
    """Stage C (PaymentSplitter.withdraw) is skipped entirely under dry-run."""
    sweeper = SettlementSweeper(settings, payments_dry_run=True)
    sweeper._get_executor = MagicMock(side_effect=AssertionError("executor must not run under dry-run"))
    tx = await sweeper.withdraw_publisher(pub, 5_000_000)
    assert tx is None
    sweeper._get_executor.assert_not_called()


@pytest.mark.asyncio
async def test_dry_run_withdraw_subscriber_returns_none_without_chain_call(settings):
    """Subscriber DCW return-transfer is skipped entirely under dry-run."""
    sweeper = SettlementSweeper(settings, payments_dry_run=True)
    sweeper._get_executor = MagicMock(side_effect=AssertionError("executor must not run under dry-run"))
    sweeper._usdc_balance_of = MagicMock(side_effect=AssertionError("balance read must not run under dry-run"))
    tx = await sweeper.withdraw_subscriber(
        circle_wallet_id="w-1", dcw_address="0xdcw", to_wallet="0xto", sub_id="sub-1"
    )
    assert tx is None
    sweeper._get_executor.assert_not_called()


def test_sweeper_defaults_to_live_mode(settings):
    """Constructing without the flag keeps live behavior (backwards-compatible)."""
    assert SettlementSweeper(settings)._payments_dry_run is False
    assert SettlementSweeper(settings, payments_dry_run=True)._payments_dry_run is True


# ── PAYMENTS_HALT guard (#1240 kill switch, second round): the automatic
# per-tick sweep moves real USDC with no human approval step and was
# previously gated only on payments_dry_run — an operator flipping
# PAYMENTS_HALT=true did not stop it. Same fail-safe posture as the
# PAYMENTS_DRY_RUN guard above, read fresh per call (never cached).


@pytest.mark.asyncio
async def test_payments_halt_sweep_publisher_is_noop(settings, pub, monkeypatch):
    """PAYMENTS_HALT=true stops sweep_publisher from moving real value — no
    signer/executor touched — even though payments_dry_run is False (live)."""
    monkeypatch.setenv("PAYMENTS_HALT", "true")
    sweeper = SettlementSweeper(settings, payments_dry_run=False)
    sweeper._get_signer = MagicMock(side_effect=AssertionError("signer must not run while PAYMENTS_HALT"))
    sweeper._get_executor = MagicMock(side_effect=AssertionError("executor must not run while PAYMENTS_HALT"))
    await sweeper.sweep_publisher(pub)
    # No return-value assertion: sweep_publisher is `-> None` on every path,
    # so `result is None` holds whether or not the guard fired and proves
    # nothing. The discriminator is that neither accessor was reached — the
    # `pub` fixture sets agent_wallet_id AND gateway_seller_address, so
    # without the guard this call falls through to _stage_a_gateway_to_wallet
    # and calls _get_signer.
    sweeper._get_signer.assert_not_called()
    sweeper._get_executor.assert_not_called()


@pytest.mark.asyncio
async def test_payments_halt_false_sweep_publisher_still_runs(sweeper, pub, monkeypatch):
    """Adversarial companion: PAYMENTS_HALT=false (the default) must NOT
    block the sweep — proves the guard above isn't a tautology that always
    skips regardless of the flag's value."""
    monkeypatch.setenv("PAYMENTS_HALT", "false")
    with (
        patch.object(sweeper, "_stage_a_gateway_to_wallet") as mock_a,
        patch.object(sweeper, "_stage_b_wallet_to_pool") as mock_b,
    ):
        await sweeper.sweep_publisher(pub)
        mock_a.assert_awaited_once_with(pub)
        mock_b.assert_awaited_once_with(pub)


# ── PAYMENTS_HALT guard (#1240 kill switch, third round): withdraw_publisher
# and withdraw_subscriber are reachable directly from authenticated endpoints
# (Withdraw button, unsubscribe) without going through sweep_publisher, so
# gating the sweep alone left PaymentSplitter.withdraw and the DCW USDC
# transfer unprotected. Same fail-safe posture, read fresh per call.


@pytest.mark.asyncio
async def test_payments_halt_withdraw_publisher_is_noop(settings, pub, monkeypatch):
    """PAYMENTS_HALT=true stops withdraw_publisher's real PaymentSplitter.withdraw
    — no executor touched — even though payments_dry_run is False (live)."""
    monkeypatch.setenv("PAYMENTS_HALT", "true")
    sweeper = SettlementSweeper(settings, payments_dry_run=False)
    sweeper._get_executor = MagicMock(side_effect=AssertionError("executor must not run while PAYMENTS_HALT"))
    tx = await sweeper.withdraw_publisher(pub, 5_000_000)
    assert tx is None
    sweeper._get_executor.assert_not_called()


@pytest.mark.asyncio
async def test_payments_halt_false_withdraw_publisher_still_runs(sweeper, pub, splitter_addr, monkeypatch):
    """Adversarial companion: PAYMENTS_HALT=false (the default) must NOT
    block withdraw_publisher — proves the guard above isn't a tautology."""
    monkeypatch.setenv("PAYMENTS_HALT", "false")
    executor = MagicMock()
    executor._submit_and_wait = MagicMock(return_value="0xtxStageC")
    sweeper._get_executor = MagicMock(return_value=executor)

    tx = await sweeper.withdraw_publisher(pub, 5_000_000)

    assert tx == "0xtxStageC"
    executor._submit_and_wait.assert_called_once_with(
        splitter_addr, "withdraw(bytes32,uint256)", [pub.pool_id, "5000000"]
    )


@pytest.mark.asyncio
async def test_payments_halt_withdraw_subscriber_is_noop(settings, monkeypatch):
    """PAYMENTS_HALT=true stops withdraw_subscriber's real DCW USDC transfer
    — no balance read, no executor touched — even though payments_dry_run is
    False (live). Asserting on ``_usdc_balance_of`` (the first mock the live
    path touches) rather than ``_get_executor`` matters: with the guard
    removed, the code still reaches the try/except before the balance read,
    so a mock that *raises* there gets swallowed by that except and returns
    None either way — ``_get_executor.assert_not_called()`` alone would pass
    whether or not the guard exists. ``_usdc_balance_of.assert_not_called()``
    is the assertion that actually distinguishes the two."""
    monkeypatch.setenv("PAYMENTS_HALT", "true")
    sweeper = SettlementSweeper(settings, payments_dry_run=False)
    sweeper._usdc_balance_of = MagicMock(side_effect=AssertionError("balance read must not run while PAYMENTS_HALT"))
    sweeper._get_executor = MagicMock(side_effect=AssertionError("executor must not run while PAYMENTS_HALT"))
    tx = await sweeper.withdraw_subscriber(
        circle_wallet_id="w-1", dcw_address="0xdcw", to_wallet="0xto", sub_id="sub-1"
    )
    assert tx is None
    sweeper._usdc_balance_of.assert_not_called()
    sweeper._get_executor.assert_not_called()


@pytest.mark.asyncio
async def test_payments_halt_false_withdraw_subscriber_still_runs(sweeper, settings, monkeypatch):
    """Adversarial companion: PAYMENTS_HALT=false (the default) must NOT
    block withdraw_subscriber — proves the guard above isn't a tautology."""
    monkeypatch.setenv("PAYMENTS_HALT", "false")
    sweeper._usdc_balance_of = MagicMock(return_value=7_250_000)
    executor = MagicMock()
    executor._submit_and_wait = MagicMock(return_value="0xtxReturn")
    sweeper._get_executor = MagicMock(return_value=executor)

    tx = await sweeper.withdraw_subscriber(
        circle_wallet_id="w-dcw-1",
        dcw_address="0xDCW0000000000000000000000000000000000001",
        to_wallet="0xS1WE0000000000000000000000000000000000001",
        sub_id="sub-9",
    )

    assert tx == "0xtxReturn"
    executor._submit_and_wait.assert_called_once_with(
        settings.usdc_address,
        "transfer(address,uint256)",
        ["0xS1WE0000000000000000000000000000000000001", "7250000"],
    )


# ── Stage A: Gateway balance below threshold → no withdraw ──────────────


@pytest.mark.asyncio
async def test_stage_a_below_threshold_does_not_withdraw(sweeper, pub):
    """Available balance below threshold => no withdraw call."""
    mock_balance = MagicMock()
    mock_balance.available = _THRESHOLD_RAW - 1  # just below threshold

    with (
        patch("archimedes.marketplace.settlement.CircleWalletSigner"),
        patch("archimedes.marketplace.settlement.CircleTxExecutor"),
        patch("archimedes.marketplace.settlement.GatewayClient") as MockGW,
    ):
        instance = MockGW.return_value
        instance.get_gateway_balance = AsyncMock(return_value=mock_balance)
        instance.withdraw = AsyncMock()

        await sweeper._stage_a_gateway_to_wallet(pub)

        instance.withdraw.assert_not_called()


# ── Stage A: Gateway balance above threshold → withdraw called ──────────


@pytest.mark.asyncio
async def test_stage_a_above_threshold_withdraws_less_fee_reserve(sweeper, pub, monkeypatch):
    """Available balance above threshold => withdraw called once, for an
    amount Circle will actually accept.

    Guard demonstration for the fee-reserve fix. ``FakeGatewayClient``
    enforces Circle's real ``amount + fee <= available`` rule, so the previous
    ``amount = balances.formatted_available`` raises here rather than passing
    against an AsyncMock that accepts anything.
    """
    monkeypatch.delenv("GATEWAY_WITHDRAW_FEE_RESERVE_USDC", raising=False)
    available = _THRESHOLD_RAW + 5_000_000  # $15.00
    fake = FakeGatewayClient(available, mint_tx_hash="0xminttx")

    with (
        patch("archimedes.marketplace.settlement.CircleWalletSigner"),
        patch("archimedes.marketplace.settlement.CircleTxExecutor"),
        patch("archimedes.marketplace.settlement.GatewayClient", return_value=fake),
    ):
        await sweeper._stage_a_gateway_to_wallet(pub)

    assert fake.last_withdraw["amount"] == "14.950000"  # $15.00 - $0.05 reserve
    assert fake.last_withdraw["max_fee"] == 50_000


@pytest.mark.asyncio
async def test_stage_a_swallows_the_circle_rejection_and_lets_stage_b_run(sweeper, pub):
    """Stage A already catches its own exceptions — confirm the Circle 400
    path stays inside that contract rather than escaping the sweeper."""
    fake = FakeGatewayClient(_THRESHOLD_RAW + 5_000_000, fee_raw=9_000_000)

    with (
        patch("archimedes.marketplace.settlement.CircleWalletSigner"),
        patch("archimedes.marketplace.settlement.CircleTxExecutor"),
        patch("archimedes.marketplace.settlement.GatewayClient", return_value=fake),
    ):
        await sweeper._stage_a_gateway_to_wallet(pub)  # must not raise

    assert fake.withdraw_calls, "withdraw should have been attempted"


# ── Stage B: wallet balance below min deposit → no action ───────────────


@pytest.mark.asyncio
async def test_stage_b_below_min_deposit_skips(sweeper, pub):
    """Wallet USDC below SWEEP_MIN_DEPOSIT_RAW => no approve/deposit."""
    with (
        patch.object(sweeper, "_usdc_balance_of") as mock_balance,
        patch("archimedes.marketplace.settlement.CircleWalletSigner"),
        patch("archimedes.marketplace.settlement.CircleTxExecutor") as MockExec,
    ):
        mock_balance.return_value = SWEEP_MIN_DEPOSIT_RAW - 1
        exec_instance = MockExec.return_value
        exec_instance.execute_approve = MagicMock()
        exec_instance._submit_and_wait = MagicMock()

        await sweeper._stage_b_wallet_to_pool(pub)

        exec_instance.execute_approve.assert_not_called()
        exec_instance._submit_and_wait.assert_not_called()


# ── Stage B: wallet balance above min → approve then depositToPool ──────


@pytest.mark.asyncio
async def test_stage_b_approve_then_deposit(sweeper, pub, splitter_addr):
    """Wallet USDC above min => approve then depositToPool in order."""
    amount = SWEEP_MIN_DEPOSIT_RAW + 5000000

    with (
        patch.object(sweeper, "_usdc_balance_of") as mock_balance,
        patch("archimedes.marketplace.settlement.CircleWalletSigner"),
        patch("archimedes.marketplace.settlement.CircleTxExecutor") as MockExec,
    ):
        mock_balance.return_value = amount
        exec_instance = MockExec.return_value
        exec_instance.execute_approve = MagicMock(return_value="0xapprv")
        exec_instance._submit_and_wait = MagicMock(return_value="0xdeposit")

        await sweeper._stage_b_wallet_to_pool(pub)

        # approve called first with correct params
        exec_instance.execute_approve.assert_called_once_with(
            GATEWAY_CHAIN,
            pub.gateway_seller_address,
            splitter_addr,
            amount,
        )
        # then depositToPool
        exec_instance._submit_and_wait.assert_called_once_with(
            splitter_addr,
            "depositToPool(bytes32,uint256)",
            [pub.pool_id, str(amount)],
        )


# ── Stage A raises => Stage B still runs ─────────────────────────────────


@pytest.mark.asyncio
async def test_stage_a_failure_does_not_block_stage_b(sweeper, pub):
    """When Stage A raises, Stage B still executes."""
    with (
        patch("archimedes.marketplace.settlement.CircleWalletSigner"),
        patch("archimedes.marketplace.settlement.CircleTxExecutor") as MockExec,
        patch("archimedes.marketplace.settlement.GatewayClient") as MockGW,
        patch.object(sweeper, "_usdc_balance_of") as mock_bal,
    ):
        gw_instance = MockGW.return_value
        gw_instance.get_gateway_balance = AsyncMock(side_effect=RuntimeError("boom"))
        gw_instance.withdraw = AsyncMock()

        mock_bal.return_value = SWEEP_MIN_DEPOSIT_RAW + 1000000
        exec_instance = MockExec.return_value
        exec_instance.execute_approve = MagicMock(return_value="0xapprv")
        exec_instance._submit_and_wait = MagicMock(return_value="0xdep")

        await sweeper._stage_a_gateway_to_wallet(pub)
        await sweeper._stage_b_wallet_to_pool(pub)

        exec_instance.execute_approve.assert_called_once()


# ── sweep_publisher runs both stages ────────────────────────────────────


@pytest.mark.asyncio
async def test_sweep_publisher_runs_both_stages(sweeper, pub):
    """sweep_publisher calls both Stage A and Stage B."""
    with (
        patch.object(sweeper, "_stage_a_gateway_to_wallet") as mock_a,
        patch.object(sweeper, "_stage_b_wallet_to_pool") as mock_b,
    ):
        mock_a.return_value = None
        mock_b.return_value = None

        await sweeper.sweep_publisher(pub)

        mock_a.assert_awaited_once_with(pub)
        mock_b.assert_awaited_once_with(pub)


# ── sweep_publisher skips when missing wallet info ──────────────────────


@pytest.mark.asyncio
async def test_sweep_skips_missing_wallet_info(sweeper, pub):
    """Missing agent_wallet_id or gateway_seller_address => no stages run."""
    pub.agent_wallet_id = ""

    with (
        patch.object(sweeper, "_stage_a_gateway_to_wallet") as mock_a,
        patch.object(sweeper, "_stage_b_wallet_to_pool") as mock_b,
    ):
        await sweeper.sweep_publisher(pub)
        mock_a.assert_not_called()
        mock_b.assert_not_called()

    pub.agent_wallet_id = "wallet-abc-123"
    pub.gateway_seller_address = ""

    with (
        patch.object(sweeper, "_stage_a_gateway_to_wallet") as mock_a,
        patch.object(sweeper, "_stage_b_wallet_to_pool") as mock_b,
    ):
        await sweeper.sweep_publisher(pub)
        mock_a.assert_not_called()
        mock_b.assert_not_called()


# ── G1: the live money-out paths (audit 2026-08-18: withdraw_subscriber was
# 4/18 statements covered, withdraw_publisher 4/16 — only dry-run and guard
# clauses executed; the balance read, executor construction, and ERC-20
# transfer had never run under test). Everything below exercises the LIVE
# path with a stubbed executor at the module boundary.


@pytest.mark.asyncio
async def test_withdraw_publisher_live_calls_splitter_withdraw(sweeper, pub, splitter_addr):
    """Stage C live: the exact PaymentSplitter.withdraw calldata, from the
    publisher's own executor, and the tx hash surfaced to the caller."""
    executor = MagicMock()
    executor._submit_and_wait = MagicMock(return_value="0xtxStageC")
    sweeper._get_executor = MagicMock(return_value=executor)

    tx = await sweeper.withdraw_publisher(pub, 5_000_000)

    assert tx == "0xtxStageC"
    executor._submit_and_wait.assert_called_once_with(
        splitter_addr, "withdraw(bytes32,uint256)", [pub.pool_id, "5000000"]
    )
    sweeper._get_executor.assert_called_once_with(pub.agent_wallet_id, pub.gateway_seller_address)


@pytest.mark.asyncio
async def test_withdraw_publisher_without_splitter_never_reaches_chain(settings, pub):
    settings.payment_splitter_address = ""
    sweeper = SettlementSweeper(settings)
    executor = MagicMock()
    executor._submit_and_wait = MagicMock(side_effect=AssertionError("no splitter configured — must not reach chain"))
    sweeper._get_executor = MagicMock(return_value=executor)

    assert await sweeper.withdraw_publisher(pub, 1_000_000) is None
    executor._submit_and_wait.assert_not_called()


@pytest.mark.asyncio
async def test_withdraw_publisher_chain_failure_returns_none_never_raises(sweeper, pub):
    executor = MagicMock()
    executor._submit_and_wait = MagicMock(side_effect=RuntimeError("chain down"))
    sweeper._get_executor = MagicMock(return_value=executor)

    assert await sweeper.withdraw_publisher(pub, 1_000_000) is None


@pytest.mark.asyncio
async def test_withdraw_subscriber_returns_exact_balance_to_the_siwe_wallet(sweeper, settings):
    """The three claims the docstring makes and nothing checked (G1):
    the transfer amount EQUALS the balance read (not a caller-supplied
    number), the token is the configured USDC contract, and the destination
    is the subscriber's own SIWE wallet — not the DCW. Executed FROM the
    DCW: the executor is bound to (circle_wallet_id, dcw_address). A mutant
    that swaps destination and DCW, or invents an amount, fails here."""
    sweeper._usdc_balance_of = MagicMock(return_value=7_250_000)
    executor = MagicMock()
    executor._submit_and_wait = MagicMock(return_value="0xtxReturn")
    sweeper._get_executor = MagicMock(return_value=executor)

    tx = await sweeper.withdraw_subscriber(
        circle_wallet_id="w-dcw-1",
        dcw_address="0xDCW0000000000000000000000000000000000001",
        to_wallet="0xS1WE0000000000000000000000000000000000001",
        sub_id="sub-9",
    )

    assert tx == "0xtxReturn"
    sweeper._usdc_balance_of.assert_called_once_with("0xDCW0000000000000000000000000000000000001")
    executor._submit_and_wait.assert_called_once_with(
        settings.usdc_address,
        "transfer(address,uint256)",
        ["0xS1WE0000000000000000000000000000000000001", "7250000"],
    )
    sweeper._get_executor.assert_called_once_with("w-dcw-1", "0xDCW0000000000000000000000000000000000001")


@pytest.mark.asyncio
async def test_withdraw_subscriber_empty_dcw_transfers_nothing(sweeper):
    sweeper._usdc_balance_of = MagicMock(return_value=0)
    sweeper._get_executor = MagicMock(side_effect=AssertionError("empty DCW — executor must not be built"))

    tx = await sweeper.withdraw_subscriber(circle_wallet_id="w-1", dcw_address="0xdcw", to_wallet="0xto", sub_id="s")

    assert tx is None
    sweeper._get_executor.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["circle_wallet_id", "dcw_address", "to_wallet"])
async def test_withdraw_subscriber_missing_identifier_short_circuits(sweeper, missing):
    kwargs = {"circle_wallet_id": "w-1", "dcw_address": "0xdcw", "to_wallet": "0xto", "sub_id": "s"}
    kwargs[missing] = ""
    sweeper._usdc_balance_of = MagicMock(side_effect=AssertionError("must not read balance with missing ids"))

    assert await sweeper.withdraw_subscriber(**kwargs) is None
    sweeper._usdc_balance_of.assert_not_called()


@pytest.mark.asyncio
async def test_withdraw_subscriber_failed_sweep_is_recoverable_by_retry(sweeper):
    """The docstring's promise, previously unchecked: 'a failed sweep leaves
    the balance recoverable by a later retry.' Nothing may mark the sweep
    done on failure — the retry must re-read the balance and transfer the
    full amount. A mutant that records the attempt as settled fails here."""
    sweeper._usdc_balance_of = MagicMock(return_value=3_000_000)
    executor = MagicMock()
    executor._submit_and_wait = MagicMock(side_effect=[RuntimeError("circle 502"), "0xretryTx"])
    sweeper._get_executor = MagicMock(return_value=executor)

    first = await sweeper.withdraw_subscriber(circle_wallet_id="w-1", dcw_address="0xdcw", to_wallet="0xto", sub_id="s")
    second = await sweeper.withdraw_subscriber(
        circle_wallet_id="w-1", dcw_address="0xdcw", to_wallet="0xto", sub_id="s"
    )

    assert first is None  # failure surfaced softly, never raised
    assert second == "0xretryTx"
    assert sweeper._usdc_balance_of.call_count == 2  # retry re-reads, no sticky state
    assert executor._submit_and_wait.call_args[0][2] == ["0xto", "3000000"]  # full balance again
