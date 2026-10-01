"""Tests for ChainExecutor — on-chain vault operations.

Target: backend/archimedes/chain/executor.py
Goal: ≥85% coverage. Consolidates #405 sub-task 5 + #408.

Hermetic: all chain/contract calls mocked. No network, no Arc RPC, no Circle.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from archimedes.chain.executor import (
    MIN_HEALTHY_LIQUIDITY_USDC,
    ChainExecutor,
    InsufficientLiquidityError,
    OraclePriceUnavailableError,
    TradeRevertedError,
    VaultCreationRevertedError,
)
from archimedes.models.portfolio import TradeDirection, TradeOrder
from hexbytes import HexBytes

# ── Helpers ───────────────────────────────────────────────────


class _Awaitable:
    """Re-awaitable value wrapper for attribute-access await patterns.

    The executor reads `chain_client.w3.eth.gas_price` like a property —
    that is, `await mock.gas_price` where `gas_price` is accessed by
    attribute, not called. `AsyncMock` instances satisfy `await mock()`
    (call pattern) but NOT `await mock` (attribute pattern); the call
    pattern is what makes a method awaitable, the attribute pattern needs
    the attribute *itself* to be awaitable. This helper wraps any value
    so `await _Awaitable(x)` returns `x`, and is safe to await repeatedly
    (each `__await__` creates a fresh coroutine).
    """

    def __init__(self, value):
        self._value = value

    def __await__(self):
        async def _coro():
            return self._value

        return _coro().__await__()


# ── Fixtures ──────────────────────────────────────────────────


@pytest.fixture
def mock_loader():
    """Mock ContractLoader with all contract accessors."""
    loader = MagicMock()
    # amm_router is a @property
    mock_router = MagicMock()
    type(loader).amm_router = PropertyMock(return_value=mock_router)
    loader.amm_pool.return_value = MagicMock()
    loader.vault.return_value = MagicMock()
    loader.oracle_for.return_value = MagicMock()
    loader.usdc.return_value = MagicMock()
    return loader


@pytest.fixture
def executor(mock_loader):
    """ChainExecutor with mocked loader + chain_client."""
    with patch("archimedes.chain.executor.chain_client") as mock_cc:
        mock_cc.settings = MagicMock()
        mock_cc.settings.usdc_address = "0x3600000000000000000000000000000000000000"
        mock_cc.settings.synth_addresses = {
            "sTSLA": "0xE745C07d7d32A1Ca0d6162A1c50e876619CF7388",
            "sSPY": "0x04315D3c35639288949cEE1d1E01Bd6100aDf3f5",
        }
        mock_cc.settings.oracle_addresses = {
            "sTSLA": "0xe1c9f2b11be97097223a66a188fca541e07873a6",
        }
        mock_cc.settings.chain_id = 5042002
        mock_cc.settings.agent_account = None  # No raw key
        mock_cc.to_checksum = lambda addr: addr  # pass-through
        mock_cc.w3 = MagicMock()
        mock_cc.w3.eth = MagicMock()
        # Property pattern: `await w3.eth.gas_price` (attribute access, then await).
        # _Awaitable satisfies this; AsyncMock does not (latent bug in prior fixture
        # versions; surfaced and worked around in PR #430, fixed properly here).
        mock_cc.w3.eth.gas_price = _Awaitable(1000000000)
        # Method pattern: `await w3.eth.method(args)` (call, then await). AsyncMock
        # is the right shape here. Set defaults so raw-key path tests don't have to
        # re-spell common values; individual tests can override per-scenario.
        mock_cc.w3.eth.get_transaction_count = AsyncMock(return_value=1)
        mock_cc.w3.eth.send_raw_transaction = AsyncMock(return_value=HexBytes(b"\x00" * 32))
        mock_cc.w3.eth.wait_for_transaction_receipt = AsyncMock(return_value={"status": 1})

        ex = ChainExecutor(loader=mock_loader)
        ex._mock_cc = mock_cc  # expose for test access
        yield ex


def _make_trade(symbol="sTSLA", direction=TradeDirection.BUY, amount=10.0, token_address=None):
    return TradeOrder(
        symbol=symbol,
        direction=direction,
        amount=amount,
        token_address=token_address or "0xE745C07d7d32A1Ca0d6162A1c50e876619CF7388",
        estimated_usdc_value=amount,
    )


# ── _validate_trade_liquidity ─────────────────────────────────


class TestValidateTradeLiquidity:
    def test_skips_usdc_self_swap(self, executor, mock_loader):
        """USDC→USDC leg should be silently skipped (Issue #399)."""
        trade = _make_trade(
            symbol="USDC",
            token_address="0x3600000000000000000000000000000000000000",
        )
        # Should not call getPool at all
        asyncio.run(executor._validate_trade_liquidity([trade]))
        mock_loader.amm_router.functions.getPool.assert_not_called()

    def test_raises_on_zero_pool(self, executor, mock_loader):
        """getPool returning zero address → InsufficientLiquidityError."""
        trade = _make_trade()
        mock_loader.amm_router.functions.getPool.return_value.call = AsyncMock(return_value="0x" + "0" * 40)

        with pytest.raises(InsufficientLiquidityError, match="zero address"):
            asyncio.run(executor._validate_trade_liquidity([trade]))

    def test_raises_on_low_reserves(self, executor, mock_loader):
        """Pool with reserves below threshold → InsufficientLiquidityError.

        MUTATION: replace the side-detection at `executor.py` with an
        unconditional ``usdc_reserve = r1 / 1e6`` — the fat synth reserve1
        here would read as $1,000,000 and let the $0.10 pool through. This is
        the token0 half of the side-detection pair; the token1 half is
        ``test_raises_when_usdc_is_token1_and_thin`` below.
        """
        trade = _make_trade()
        pool_addr = "0x38c3A5f52044a72C9cC11Ce621f1bfD7754BF8Bd"
        mock_loader.amm_router.functions.getPool.return_value.call = AsyncMock(return_value=pool_addr)

        mock_pool = mock_loader.amm_pool.return_value
        mock_pool.functions.reserve0.return_value.call = AsyncMock(return_value=100_000)  # $0.10
        mock_pool.functions.reserve1.return_value.call = AsyncMock(return_value=1_000_000_000_000)
        mock_pool.functions.token0.return_value.call = AsyncMock(
            return_value="0x3600000000000000000000000000000000000000"  # USDC is token0
        )

        with pytest.raises(InsufficientLiquidityError, match="below threshold"):
            asyncio.run(executor._validate_trade_liquidity([trade]))

    def test_passes_on_sufficient_reserves(self, executor, mock_loader):
        """Pool with reserves above threshold → no error."""
        trade = _make_trade()
        pool_addr = "0x38c3A5f52044a72C9cC11Ce621f1bfD7754BF8Bd"
        mock_loader.amm_router.functions.getPool.return_value.call = AsyncMock(return_value=pool_addr)

        mock_pool = mock_loader.amm_pool.return_value
        # $10,000 USDC (6 decimals)
        mock_pool.functions.reserve0.return_value.call = AsyncMock(return_value=10_000_000_000)
        mock_pool.functions.reserve1.return_value.call = AsyncMock(return_value=1_000_000_000_000_000)
        mock_pool.functions.token0.return_value.call = AsyncMock(
            return_value="0x3600000000000000000000000000000000000000"
        )

        # Should not raise
        asyncio.run(executor._validate_trade_liquidity([trade]))

    def test_usdc_as_token1(self, executor, mock_loader):
        """When USDC is token1, reserve1 is the USDC-side reserve.

        MUTATION: replace the side-detection at `executor.py` with an
        unconditional ``usdc_reserve = r0 / 1e6``.

        The reserves here are deliberately on OPPOSITE sides of the $5
        threshold once divided by 1e6, so this assertion can tell which
        integer the executor read. Before this fixture change reserve0 was
        500_000_000_000_000 — fat on both readings — and the whole chain
        suite (336 tests) stayed green with the side-detection deleted.

        Note the executor's ``/ 1e6`` is USDC-specific: the synth side is an
        18-decimal token, so ``reserve0 / 1e6`` is not a dollar figure at all.
        That is exactly why reading the wrong side is a money bug and why the
        raw synth integer here is small — it is chosen to be distinguishable,
        not to model a realistic synth balance.
        """
        trade = _make_trade()
        pool_addr = "0xe0725Db0eC0e793Cde04Fa32BA21A4D211a5E685"
        mock_loader.amm_router.functions.getPool.return_value.call = AsyncMock(return_value=pool_addr)

        mock_pool = mock_loader.amm_pool.return_value
        # Synth side: misread as USDC this is $4.00, BELOW the $5 threshold.
        mock_pool.functions.reserve0.return_value.call = AsyncMock(return_value=4_000_000)  # synth
        mock_pool.functions.reserve1.return_value.call = AsyncMock(return_value=10_000_000_000)  # USDC = $10k
        mock_pool.functions.token0.return_value.call = AsyncMock(
            return_value="0xE745C07d7d32A1Ca0d6162A1c50e876619CF7388"  # synth is token0
        )

        # Should pass — USDC (token1) has $10k. Reading reserve0 instead raises.
        asyncio.run(executor._validate_trade_liquidity([trade]))

    def test_raises_when_usdc_is_token1_and_thin(self, executor, mock_loader):
        """USDC as token1, reserve1 BELOW threshold, fat synth reserve0 → raise.

        MUTATION: replace the side-detection at `executor.py` with an
        unconditional ``usdc_reserve = r0 / 1e6``. The mutant reads the fat
        synth side, computes a nonsense "$900,000,000.00" of USDC liquidity,
        and lets a $2 pool through — a real trade sent into a pool that
        cannot fill it.

        The inverse (USDC as token0, thin reserve0, fat reserve1) is pinned by
        ``test_raises_on_low_reserves`` above, which kills the mirror mutant
        (``usdc_reserve = r1 / 1e6``). Together the two are the full 2x2.
        """
        trade = _make_trade()
        pool_addr = "0xe0725Db0eC0e793Cde04Fa32BA21A4D211a5E685"
        mock_loader.amm_router.functions.getPool.return_value.call = AsyncMock(return_value=pool_addr)

        mock_pool = mock_loader.amm_pool.return_value
        # Fat synth side (misread as USDC it would read $900,000,000.00)...
        mock_pool.functions.reserve0.return_value.call = AsyncMock(return_value=900_000_000_000_000)  # synth
        # ...but the actual USDC side holds $2.00, below the $5 threshold.
        mock_pool.functions.reserve1.return_value.call = AsyncMock(return_value=2_000_000)  # USDC = $2
        mock_pool.functions.token0.return_value.call = AsyncMock(
            return_value="0xE745C07d7d32A1Ca0d6162A1c50e876619CF7388"  # synth is token0
        )

        with pytest.raises(InsufficientLiquidityError, match="below threshold"):
            asyncio.run(executor._validate_trade_liquidity([trade]))

    def test_fails_closed_on_unexpected_error(self, executor, mock_loader):
        """Unexpected probe errors (RPC/ABI) fail closed — leg is skipped, not allowed.

        B4 (AUDIT_2026-06-14.md): a probe failure must NOT be treated as "liquidity
        OK". It raises InsufficientLiquidityError (probe-failure variant) so the
        caller skips this leg the same way it would for a confirmed thin pool.
        """
        trade = _make_trade()
        mock_loader.amm_router.functions.getPool.return_value.call = AsyncMock(side_effect=RuntimeError("RPC timeout"))

        with pytest.raises(InsufficientLiquidityError, match="probe failed"):
            asyncio.run(executor._validate_trade_liquidity([trade]))


# ── execute_trades ────────────────────────────────────────────


class TestExecuteTrades:
    def test_calls_validate_liquidity_before_swap(self, executor, mock_loader):
        """execute_trades must call _validate_trade_liquidity first."""
        trade = _make_trade()

        with (
            patch.object(executor, "_validate_trade_liquidity", new=AsyncMock()) as mock_validate,
            # _confirm_receipt added by #403 — must mock so the Circle path doesn't
            # try to await the real wait_for_transaction_receipt chain on a MagicMock.
            patch.object(executor, "_confirm_receipt", new=AsyncMock(return_value="0xtxhash")),
            patch("archimedes.chain.executor.circle_signer") as mock_signer,
        ):
            mock_signer.is_configured = True
            mock_signer.execute_contract = AsyncMock(return_value="0xtxhash")

            asyncio.run(executor.execute_trades("0xvault", [trade]))
            mock_validate.assert_called_once_with([trade])

    def test_raises_insufficient_liquidity(self, executor):
        """execute_trades propagates InsufficientLiquidityError."""
        trade = _make_trade()

        with (
            patch.object(
                executor,
                "_validate_trade_liquidity",
                new=AsyncMock(side_effect=InsufficientLiquidityError("thin pool")),
            ),
            pytest.raises(InsufficientLiquidityError),
        ):
            asyncio.run(executor.execute_trades("0xvault", [trade]))

    def test_circle_signer_path(self, executor, mock_loader):
        """When Circle signer is configured, uses circle_signer.execute_contract.

        After #403, execute_trades also awaits _confirm_receipt on the returned
        tx_hash. _confirm_receipt is mocked here to return the hash unchanged
        so we still assert the same end-to-end shape.
        """
        trade = _make_trade()

        with (
            patch.object(executor, "_validate_trade_liquidity", new=AsyncMock()),
            patch.object(executor, "_confirm_receipt", new=AsyncMock(return_value="0xdeadbeef")) as mock_confirm,
            patch("archimedes.chain.executor.circle_signer") as mock_signer,
        ):
            mock_signer.is_configured = True
            mock_signer.execute_contract = AsyncMock(return_value="0xdeadbeef")

            result = asyncio.run(executor.execute_trades("0xvault", [trade]))
            assert result == ["0xdeadbeef"]
            mock_signer.execute_contract.assert_called_once()
            # Receipt-confirm must run after Circle submit (regression guard for #403)
            mock_confirm.assert_called_once_with("0xdeadbeef")

    def test_no_signer_raises(self, executor):
        """No Circle signer + no raw key → RuntimeError."""
        trade = _make_trade()

        with (
            patch.object(executor, "_validate_trade_liquidity", new=AsyncMock()),
            patch("archimedes.chain.executor.circle_signer") as mock_signer,
        ):
            mock_signer.is_configured = False
            executor._mock_cc.settings.agent_account = None

            with pytest.raises(RuntimeError, match="No agent account"):
                asyncio.run(executor.execute_trades("0xvault", [trade]))


# ── execute_trades depth coverage (#408) ──────────────────────


def _install_raw_key_vault(mock_loader, mock_account):
    """Wire up vault.functions.rebalance(...).build_transaction(...) to
    return a plausible tx dict. Per-test helper because each test's
    mock_account.address differs.
    """
    mock_loader.vault.return_value.functions.rebalance.return_value.build_transaction = AsyncMock(
        return_value={"from": mock_account.address, "nonce": 1, "gas": 2_000_000}
    )


class TestExecuteTradesDepth:
    """Scenario-coverage matrix for execute_trades (#408).

    Complements TestExecuteTrades above by exercising the failure modes
    and signer-path branches that previously went uncovered. Production
    path uses HexBytes from web3's send_raw_transaction; the Circle
    path uses str. Both must be tested.
    """

    def test_empty_trades_skips_tx(self, executor, mock_loader):
        """Empty trades list — no synth legs to swap, so the executor skips the
        rebalance tx entirely rather than submitting rebalance([],[],[],[])
        (issue #925). No AMM getPool calls and no signer call fire.
        """
        with (
            patch.object(executor, "_confirm_receipt", new=AsyncMock(return_value="0xtxhash")),
            patch("archimedes.chain.executor.circle_signer") as mock_signer,
        ):
            mock_signer.is_configured = True
            mock_signer.execute_contract = AsyncMock(return_value="0xtxhash")
            result = asyncio.run(executor.execute_trades("0xvault", []))
            assert result == []
            mock_signer.execute_contract.assert_not_called()
            mock_loader.amm_router.functions.getPool.assert_not_called()

    def test_all_usdc_legs_filtered_skips_tx(self, executor, mock_loader):
        """All-USDC (cash) trades filtered out (#399) — nothing left to swap, so
        no AMM `getPool` calls and no rebalance tx is submitted (issue #925)."""
        usdc_trade = _make_trade(
            symbol="USDC",
            token_address="0x3600000000000000000000000000000000000000",
        )
        with (
            patch.object(executor, "_confirm_receipt", new=AsyncMock(return_value="0xtxhash")),
            patch("archimedes.chain.executor.circle_signer") as mock_signer,
        ):
            mock_signer.is_configured = True
            mock_signer.execute_contract = AsyncMock(return_value="0xtxhash")
            result = asyncio.run(executor.execute_trades("0xvault", [usdc_trade]))
            # Cash-only rebalance is skipped: empty result, no swap lookups, no tx.
            assert result == []
            mock_signer.execute_contract.assert_not_called()
            mock_loader.amm_router.functions.getPool.assert_not_called()

    def test_sell_direction_calls_usdc_value_to_token_raw(self, executor, mock_loader):
        """SELL trades route through _usdc_value_to_token_raw for decimal conversion.

        Covers lines 216-223 of execute_trades that the existing tests
        (all-BUY) didn't exercise.
        """
        trade = _make_trade(direction=TradeDirection.SELL)

        with (
            patch.object(executor, "_validate_trade_liquidity", new=AsyncMock()),
            patch.object(
                executor,
                "_usdc_value_to_token_raw",
                new=AsyncMock(return_value=5_000_000_000_000_000_000),
            ) as mock_convert,
            patch.object(executor, "_confirm_receipt", new=AsyncMock(return_value="0xtxhash")),
            patch("archimedes.chain.executor.circle_signer") as mock_signer,
        ):
            mock_signer.is_configured = True
            mock_signer.execute_contract = AsyncMock(return_value="0xtxhash")
            asyncio.run(executor.execute_trades("0xvault", [trade]))
            # SELL branch converts USDC value → token raw amount
            mock_convert.assert_called_once()

    def test_mixed_usdc_and_synth_only_synth_validated(self, executor, mock_loader):
        """Mixed USDC+synth: synth leg gets pool-checked, USDC leg filtered."""
        usdc_trade = _make_trade(
            symbol="USDC",
            token_address="0x3600000000000000000000000000000000000000",
        )
        synth_trade = _make_trade()  # sTSLA — non-USDC
        pool_addr = "0x38c3A5f52044a72C9cC11Ce621f1bfD7754BF8Bd"
        mock_loader.amm_router.functions.getPool.return_value.call = AsyncMock(return_value=pool_addr)
        mock_pool = mock_loader.amm_pool.return_value
        mock_pool.functions.reserve0.return_value.call = AsyncMock(return_value=10_000_000_000)
        mock_pool.functions.reserve1.return_value.call = AsyncMock(return_value=1_000_000_000_000_000)
        mock_pool.functions.token0.return_value.call = AsyncMock(
            return_value="0x3600000000000000000000000000000000000000"
        )
        with (
            patch.object(executor, "_confirm_receipt", new=AsyncMock(return_value="0xtxhash")),
            patch("archimedes.chain.executor.circle_signer") as mock_signer,
        ):
            mock_signer.is_configured = True
            mock_signer.execute_contract = AsyncMock(return_value="0xtxhash")
            asyncio.run(executor.execute_trades("0xvault", [usdc_trade, synth_trade]))
            # getPool fires exactly once — for the synth leg only
            mock_loader.amm_router.functions.getPool.assert_called_once()

    def test_circle_path_propagates_revert(self, executor, mock_loader):
        """Circle signer path: receipt revert raises TradeRevertedError."""
        trade = _make_trade()
        with (
            patch.object(executor, "_validate_trade_liquidity", new=AsyncMock()),
            patch.object(
                executor,
                "_confirm_receipt",
                new=AsyncMock(side_effect=TradeRevertedError("Rebalance tx reverted on-chain: 0xabc")),
            ),
            patch("archimedes.chain.executor.circle_signer") as mock_signer,
        ):
            mock_signer.is_configured = True
            mock_signer.execute_contract = AsyncMock(return_value="0xabc")
            with pytest.raises(TradeRevertedError, match="reverted on-chain"):
                asyncio.run(executor.execute_trades("0xvault", [trade]))

    def test_circle_path_propagates_rate_limit_error(self, executor, mock_loader):
        """Circle signer raising a rate-limit-shaped exception propagates."""
        trade = _make_trade()
        with (
            patch.object(executor, "_validate_trade_liquidity", new=AsyncMock()),
            patch("archimedes.chain.executor.circle_signer") as mock_signer,
        ):
            mock_signer.is_configured = True
            mock_signer.execute_contract = AsyncMock(
                side_effect=RuntimeError("Rate limit exceeded: 429 Too Many Requests")
            )
            with pytest.raises(RuntimeError, match="Rate limit"):
                asyncio.run(executor.execute_trades("0xvault", [trade]))

    def test_circle_path_propagates_network_failure(self, executor, mock_loader):
        """Network failure (ConnectionError-shaped) propagates from the signer."""
        trade = _make_trade()
        with (
            patch.object(executor, "_validate_trade_liquidity", new=AsyncMock()),
            patch("archimedes.chain.executor.circle_signer") as mock_signer,
        ):
            mock_signer.is_configured = True
            mock_signer.execute_contract = AsyncMock(side_effect=ConnectionError("upstream RPC unreachable"))
            with pytest.raises(ConnectionError, match="RPC unreachable"):
                asyncio.run(executor.execute_trades("0xvault", [trade]))

    def test_raw_key_path_succeeds_with_hexbytes_tx_hash(self, executor, mock_loader):
        """Raw-key signer path: send_raw_transaction returns HexBytes; flow completes.

        This is the production raw-key path that previously went uncovered
        (per the #408 issue body). The HexBytes return type is what web3.py
        actually emits from send_raw_transaction.
        """
        trade = _make_trade()
        hex_hash = HexBytes("0x" + "ab" * 32)

        mock_account = MagicMock()
        mock_account.address = "0xAGENT0000000000000000000000000000000000ab"
        signed = MagicMock()
        signed.raw_transaction = b"\x01\x02\x03"
        mock_account.sign_transaction.return_value = signed

        executor._mock_cc.settings.agent_account = mock_account
        executor._mock_cc.w3.eth.send_raw_transaction = AsyncMock(return_value=hex_hash)
        _install_raw_key_vault(mock_loader, mock_account)

        with (
            patch.object(executor, "_validate_trade_liquidity", new=AsyncMock()),
            patch.object(executor, "_confirm_receipt", new=AsyncMock(return_value="0x" + "ab" * 32)) as mock_confirm,
            patch("archimedes.chain.executor.circle_signer") as mock_signer,
        ):
            mock_signer.is_configured = False
            result = asyncio.run(executor.execute_trades("0xvault", [trade]))
            assert result == ["0x" + "ab" * 32]
            # Critical: _confirm_receipt must have been called with HexBytes (not str)
            mock_confirm.assert_called_once()
            (call_arg,) = mock_confirm.call_args.args
            assert isinstance(call_arg, (bytes, bytearray)), (
                f"Production raw-key path passes HexBytes to _confirm_receipt; got {type(call_arg).__name__}"
            )

    def test_raw_key_path_propagates_revert(self, executor, mock_loader):
        """Raw-key path: receipt revert raises TradeRevertedError."""
        trade = _make_trade()
        hex_hash = HexBytes("0x" + "cd" * 32)

        mock_account = MagicMock()
        mock_account.address = "0xAGENT0000000000000000000000000000000000cd"
        mock_account.sign_transaction.return_value = MagicMock(raw_transaction=b"\x04\x05\x06")

        executor._mock_cc.settings.agent_account = mock_account
        executor._mock_cc.w3.eth.send_raw_transaction = AsyncMock(return_value=hex_hash)
        _install_raw_key_vault(mock_loader, mock_account)

        with (
            patch.object(executor, "_validate_trade_liquidity", new=AsyncMock()),
            patch.object(
                executor,
                "_confirm_receipt",
                new=AsyncMock(side_effect=TradeRevertedError("Rebalance tx reverted on-chain: 0xcd")),
            ),
            patch("archimedes.chain.executor.circle_signer") as mock_signer,
        ):
            mock_signer.is_configured = False
            with pytest.raises(TradeRevertedError):
                asyncio.run(executor.execute_trades("0xvault", [trade]))

    def test_raw_key_path_propagates_timeout(self, executor, mock_loader):
        """Raw-key path: timeout during receipt wait propagates."""
        trade = _make_trade()
        hex_hash = HexBytes("0x" + "ef" * 32)

        mock_account = MagicMock()
        mock_account.address = "0xAGENT0000000000000000000000000000000000ef"
        mock_account.sign_transaction.return_value = MagicMock(raw_transaction=b"\x07\x08\x09")

        executor._mock_cc.settings.agent_account = mock_account
        executor._mock_cc.w3.eth.send_raw_transaction = AsyncMock(return_value=hex_hash)
        _install_raw_key_vault(mock_loader, mock_account)

        with (
            patch.object(executor, "_validate_trade_liquidity", new=AsyncMock()),
            patch.object(
                executor,
                "_confirm_receipt",
                new=AsyncMock(side_effect=TimeoutError("receipt wait timed out")),
            ),
            patch("archimedes.chain.executor.circle_signer") as mock_signer,
        ):
            mock_signer.is_configured = False
            with pytest.raises(TimeoutError, match="timed out"):
                asyncio.run(executor.execute_trades("0xvault", [trade]))

    def test_raw_key_path_propagates_network_failure_on_send(self, executor, mock_loader):
        """Raw-key path: send_raw_transaction failure propagates before receipt-wait."""
        trade = _make_trade()

        mock_account = MagicMock()
        mock_account.address = "0xAGENT0000000000000000000000000000000000ff"
        mock_account.sign_transaction.return_value = MagicMock(raw_transaction=b"\x0a\x0b\x0c")

        executor._mock_cc.settings.agent_account = mock_account
        executor._mock_cc.w3.eth.send_raw_transaction = AsyncMock(side_effect=ConnectionError("RPC down"))
        _install_raw_key_vault(mock_loader, mock_account)

        with (
            patch.object(executor, "_validate_trade_liquidity", new=AsyncMock()),
            patch("archimedes.chain.executor.circle_signer") as mock_signer,
        ):
            mock_signer.is_configured = False
            with pytest.raises(ConnectionError, match="RPC down"):
                asyncio.run(executor.execute_trades("0xvault", [trade]))


# ── create_vault ─────────────────────────────────────────────


class TestCreateVault:
    """Coverage for create_vault's Circle-signer path (#651).

    Hermetic: _parse_vault_created and factory.functions.getVaults().call()
    are mocked per-scenario; no network, no Arc RPC, no Circle.
    """

    def test_circle_path_happy_returns_parsed_vault_address(self, executor, mock_loader):
        """status=1 + VaultCreated event found → returns the parsed address
        without ever calling getVaults()."""
        with (
            patch.object(executor, "_parse_vault_created", return_value="0xNewVault"),
            patch("archimedes.chain.executor.circle_signer") as mock_signer,
        ):
            mock_signer.is_configured = True
            mock_signer.execute_contract = AsyncMock(return_value="0xtxhash")
            result = asyncio.run(executor.create_vault("Momentum Alpha", "vMOM", 150, 2000, True))
            assert result == "0xNewVault"
            mock_loader.vault_factory.functions.getVaults.assert_not_called()

    def test_circle_path_revert_raises(self, executor, mock_loader):
        """status=0 → raises VaultCreationRevertedError and never falls back
        to all_vaults[-1] (the bug #651 guards against)."""
        executor._mock_cc.w3.eth.wait_for_transaction_receipt = AsyncMock(return_value={"status": 0})
        with patch("archimedes.chain.executor.circle_signer") as mock_signer:
            mock_signer.is_configured = True
            mock_signer.execute_contract = AsyncMock(return_value="0xtxhash")
            with pytest.raises(VaultCreationRevertedError, match="reverted on-chain"):
                asyncio.run(executor.create_vault("Momentum Alpha", "vMOM", 150, 2000, True))
        mock_loader.vault_factory.functions.getVaults.assert_not_called()

    def test_circle_path_no_event_falls_back_with_warning(self, executor, mock_loader, caplog):
        """status=1 but no VaultCreated event found → falls back to
        all_vaults[-1] and logs a warning flagging the indexing gap."""
        mock_loader.vault_factory.functions.getVaults.return_value.call = AsyncMock(
            return_value=["0xOldVault1", "0xOldVault2"]
        )
        with (
            patch.object(executor, "_parse_vault_created", return_value=None),
            patch("archimedes.chain.executor.circle_signer") as mock_signer,
            caplog.at_level("WARNING", logger="archimedes.chain.executor"),
        ):
            mock_signer.is_configured = True
            mock_signer.execute_contract = AsyncMock(return_value="0xtxhash")
            result = asyncio.run(executor.create_vault("Momentum Alpha", "vMOM", 150, 2000, True))
        assert result == "0xOldVault2"
        assert any("no VaultCreated event found" in r.message for r in caplog.records)


# ── T0.2: non-custodial owner≠agent handoff ──────────────────


class TestNonCustodialOwnership:
    """create_vault must make the vault non-custodial after creation: owner =
    user (or governance), agent = backend signer. Re-lands reverted PR #646's
    intent WITHOUT changing the live 5-arg createVault selector — it uses the
    setAgent/transferOwnership functions already in the deployed Vault ABI.

    Hermetic: Circle execute_contract + receipt waits are mocked; no network.
    """

    def _circle_create_with_owner(self, executor, mock_loader, *, owner_wallet, env):
        """Run create_vault on the Circle path and return the list of
        (abi_function, abi_params) tuples submitted via circle_signer."""
        calls: list[tuple[str, list]] = []

        async def _exec(*, contract_address, abi_function, abi_params, **_):
            calls.append((abi_function, abi_params))
            return "0xtxhash"

        with (
            patch.object(executor, "_parse_vault_created", return_value="0xNewVault"),
            patch("archimedes.chain.executor.circle_signer") as mock_signer,
            patch.dict("os.environ", env, clear=False),
        ):
            mock_signer.is_configured = True
            mock_signer.execute_contract = AsyncMock(side_effect=_exec)
            result = asyncio.run(
                executor.create_vault("Momentum Alpha", "vMOM", 150, 2000, True, owner_wallet=owner_wallet)
            )
        assert result == "0xNewVault"
        return calls

    def test_user_wallet_becomes_owner_agent_is_backend(self, executor, mock_loader):
        """User-facing path: owner_wallet is passed → setAgent(backend) then
        transferOwnership(user). owner != agent."""
        user = "0x00000000000000000000000000000000000A11cE"
        agent = "0x00000000000000000000000000000000000aBcDe"
        calls = self._circle_create_with_owner(executor, mock_loader, owner_wallet=user, env={"WALLET_ADDRESS": agent})
        sigs = [c[0] for c in calls]
        assert "createVault(string,string,uint16,uint16,bool)" in sigs
        assert "setAgent(address)" in sigs
        assert "transferOwnership(address)" in sigs
        # The agent is pinned to the backend signer; ownership goes to the user.
        set_agent = next(c for c in calls if c[0] == "setAgent(address)")
        transfer = next(c for c in calls if c[0] == "transferOwnership(address)")
        assert set_agent[1] == [agent]
        assert transfer[1] == [user]
        assert transfer[1] != set_agent[1]  # owner != agent — the whole point

    def test_no_user_uses_governance_address(self, executor, mock_loader):
        """Bootstrap/auto-create: no owner_wallet but ARC_VAULT_GOVERNANCE_ADDRESS
        set → ownership transfers to governance (distinct from the agent)."""
        gov = "0x000000000000000000000000000000000060A111"  # governance/cold key
        agent = "0x00000000000000000000000000000000000aBcDe"
        calls = self._circle_create_with_owner(
            executor,
            mock_loader,
            owner_wallet=None,
            env={"WALLET_ADDRESS": agent, "ARC_VAULT_GOVERNANCE_ADDRESS": gov},
        )
        transfer = next(c for c in calls if c[0] == "transferOwnership(address)")
        assert transfer[1] == [gov]

    def test_no_owner_no_governance_warns_and_skips_transfer(self, executor, mock_loader, caplog):
        """No user AND no governance → DON'T transfer (no regression), but log a
        loud warning. Crucially: never silently transfer ownership to the agent."""
        agent = "0x00000000000000000000000000000000000aBcDe"
        with caplog.at_level("WARNING", logger="archimedes.chain.executor"):
            calls = self._circle_create_with_owner(
                executor,
                mock_loader,
                owner_wallet=None,
                env={"WALLET_ADDRESS": agent, "ARC_VAULT_GOVERNANCE_ADDRESS": ""},
            )
        sigs = [c[0] for c in calls]
        assert "transferOwnership(address)" not in sigs
        assert "setAgent(address)" not in sigs  # handoff skipped entirely
        assert any("without an owner≠agent handoff" in r.message.lower() for r in caplog.records)

    def test_refuses_to_transfer_ownership_to_agent(self, executor, mock_loader, caplog):
        """Defensive: if the resolved owner equals the agent address, refuse the
        transfer (owner == agent would be custodial) and warn."""
        agent = "0x00000000000000000000000000000000000aBcDe"
        with caplog.at_level("WARNING", logger="archimedes.chain.executor"):
            calls = self._circle_create_with_owner(
                executor,
                mock_loader,
                owner_wallet=agent,  # owner == agent
                env={"WALLET_ADDRESS": agent},
            )
        sigs = [c[0] for c in calls]
        assert "transferOwnership(address)" not in sigs
        assert any("owner would equal agent" in r.message.lower() for r in caplog.records)

    def test_handoff_failure_is_non_fatal(self, executor, mock_loader, caplog):
        """A failed setAgent/transferOwnership must NOT fail the create call — the
        vault exists and holds no funds yet. It logs an exception for the operator."""
        user = "0x00000000000000000000000000000000000A11cE"
        agent = "0x00000000000000000000000000000000000aBcDe"

        async def _exec(*, contract_address, abi_function, abi_params, **_):
            if abi_function == "createVault(string,string,uint16,uint16,bool)":
                return "0xtxhash"
            raise RuntimeError("handoff tx failed")

        with (
            patch.object(executor, "_parse_vault_created", return_value="0xNewVault"),
            patch("archimedes.chain.executor.circle_signer") as mock_signer,
            patch.dict("os.environ", {"WALLET_ADDRESS": agent}, clear=False),
            caplog.at_level("ERROR", logger="archimedes.chain.executor"),
        ):
            mock_signer.is_configured = True
            mock_signer.execute_contract = AsyncMock(side_effect=_exec)
            # Must still return the vault address despite the handoff failure.
            result = asyncio.run(executor.create_vault("M", "vM", 0, 0, True, owner_wallet=user))
        assert result == "0xNewVault"
        assert any("handoff failed" in r.message.lower() for r in caplog.records)

    def test_resolve_vault_owner_priority(self, executor):
        """Unit: owner_wallet wins over governance; governance is the fallback;
        None when neither is set — NEVER the agent by default."""
        with patch.dict("os.environ", {"ARC_VAULT_GOVERNANCE_ADDRESS": "0xGov"}, clear=False):
            assert executor._resolve_vault_owner("0xUser") == "0xUser"  # user wins
            assert executor._resolve_vault_owner(None) == "0xGov"  # governance fallback
            assert executor._resolve_vault_owner("   ") == "0xGov"  # blank → fallback
        with patch.dict("os.environ", {"ARC_VAULT_GOVERNANCE_ADDRESS": ""}, clear=False):
            assert executor._resolve_vault_owner(None) is None  # neither → None (not agent)


# ── _usdc_value_to_token_raw ─────────────────────────────────


class TestUsdcValueToTokenRaw:
    def test_usdc_to_usdc_returns_6_decimals(self, executor):
        """USDC→USDC: amount × 1e6."""
        result = asyncio.run(executor._usdc_value_to_token_raw("0x3600000000000000000000000000000000000000", 10.0))
        assert result == 10_000_000  # 10 × 1e6

    def test_synth_with_oracle_price(self, executor, mock_loader):
        """Synth with oracle price: usdc_value / price × 1e18."""
        mock_oracle = mock_loader.oracle_for.return_value
        mock_oracle.functions.price.return_value.call = AsyncMock(return_value=500_000_000)  # $500

        result = asyncio.run(
            executor._usdc_value_to_token_raw(
                "0xE745C07d7d32A1Ca0d6162A1c50e876619CF7388",
                100.0,  # $100 of sTSLA
            )
        )
        # $100 / $500 = 0.2 tokens × 1e18
        assert result == 200_000_000_000_000_000  # 0.2 × 1e18

    def test_oracle_failure_raises_not_1to1(self, executor, mock_loader):
        """Oracle failure must FAIL the rebalance, not fall back to a 1:1 ($1) price (#923).

        The old 1:1 fallback ordered ~price× too many units on a high-priced synth.
        """
        mock_oracle = mock_loader.oracle_for.return_value
        mock_oracle.functions.price.return_value.call = AsyncMock(side_effect=RuntimeError("oracle down"))

        with pytest.raises(OraclePriceUnavailableError):
            asyncio.run(executor._usdc_value_to_token_raw("0xE745C07d7d32A1Ca0d6162A1c50e876619CF7388", 5.0))

    def test_unknown_token_raises_not_1to1(self, executor):
        """Unknown token (no oracle) must raise, not guess a 1:1 ($1) price (#923)."""
        with pytest.raises(OraclePriceUnavailableError):
            asyncio.run(executor._usdc_value_to_token_raw("0x0000000000000000000000000000000000099999", 1.0))


# ── InsufficientLiquidityError ────────────────────────────────


class TestInsufficientLiquidityError:
    def test_is_runtime_error(self):
        assert issubclass(InsufficientLiquidityError, RuntimeError)

    def test_message_preserved(self):
        err = InsufficientLiquidityError("Pool 0x38c3: $0.10 below $5")
        assert "below" in str(err)

    def test_min_threshold_is_positive(self):
        assert MIN_HEALTHY_LIQUIDITY_USDC > 0


# ── Nonce handling on the raw-key path (#909) ─────────────────


class TestSequentialNonce:
    @pytest.mark.asyncio
    async def test_set_oracles_and_allocations_read_pending_nonce(self, executor, mock_loader):
        """Regression for #909: set_token_oracles + set_target_allocations run
        back-to-back in one _process_vault tick. Both must read the PENDING-block
        nonce, otherwise the second reuses the first's still-in-mempool nonce and
        one tx is silently dropped."""
        mock_cc = executor._mock_cc
        account = MagicMock()
        account.address = "0xAbC0000000000000000000000000000000000001"
        account.sign_transaction.return_value.raw_transaction = b"\x01" * 32
        mock_cc.settings.agent_account = account

        vault = mock_loader.vault.return_value
        vault.functions.setTokenOracles.return_value.build_transaction = AsyncMock(return_value={})
        vault.functions.setTargetAllocations.return_value.build_transaction = AsyncMock(return_value={})

        with patch("archimedes.chain.executor.circle_signer") as mock_signer:
            mock_signer.is_configured = False  # force the raw-key path
            await executor.set_token_oracles("0xVault", ["0xT"], ["0xO"])
            await executor.set_target_allocations("0xVault", ["0xT"], [10000])

        assert mock_cc.w3.eth.get_transaction_count.await_count == 2
        for call in mock_cc.w3.eth.get_transaction_count.await_args_list:
            # (address, "pending") — the unfixed code passed only (address,).
            assert call.args == (account.address, "pending"), f"nonce read used {call.args}, must request pending"


# ── Price baseline write — #1103 follow-up to #1152 ────────────
#
# set_target_allocations is where a vault's allocations first become known
# on-chain, so it is the write point for the per-vault oracle-price BASELINE
# that vault_service._compute_returns later reads back (key
# vault:prices:{vault_address}, JSON: token address -> human-scale price).
# PR #1152 wrote a DIFFERENT key (oracle:prices:latest, global, keyed by
# symbol) that nothing downstream ever consumed — these tests guard the
# writer/reader pair actually matching.


class TestPriceBaselineWrite:
    @pytest.mark.asyncio
    async def test_writes_baseline_keyed_by_token_address_with_nx(self, executor, mock_loader):
        mock_cc = executor._mock_cc
        account = MagicMock()
        account.address = "0xAbC0000000000000000000000000000000000001"
        account.sign_transaction.return_value.raw_transaction = b"\x01" * 32
        mock_cc.settings.agent_account = account

        vault = mock_loader.vault.return_value
        vault.functions.setTargetAllocations.return_value.build_transaction = AsyncMock(return_value={})

        # sTSLA oracle reports raw 123_450_000 -> $123.45 human-scale (/1e6),
        # the SAME scaling vault_service._compute_returns uses for "current".
        oracle = MagicMock()
        oracle.functions.price.return_value.call = AsyncMock(return_value=123_450_000)
        mock_loader.oracle_for.return_value = oracle

        fake_redis = MagicMock()
        fake_redis.exists = AsyncMock(return_value=0)
        fake_redis.set = AsyncMock(return_value=True)
        fake_redis.aclose = AsyncMock()

        tsla = "0xE745C07d7d32A1Ca0d6162A1c50e876619CF7388"
        with (
            patch("archimedes.chain.executor.circle_signer") as mock_signer,
            patch("redis.asyncio.from_url", return_value=fake_redis),
        ):
            mock_signer.is_configured = False  # force the raw-key path
            await executor.set_target_allocations("0xVault", [tsla], [10000])

        fake_redis.set.assert_awaited_once()
        args, kwargs = fake_redis.set.call_args
        # Key AND token keys are normalized lowercase (services/price_baseline —
        # the same helpers the reader uses), so mixed-case addresses can never
        # split the writer/reader pair (#1201 review).
        assert args[0] == "vault:prices:0xvault"
        assert json.loads(args[1]) == {tsla.lower(): pytest.approx(123.45)}
        # NX: only the FIRST call for a vault may take effect — set_target_allocations
        # fires every agent tick, and overwriting the baseline on a later tick
        # would drift creation_price to match current_price and permanently
        # pin computed returns near 0%.
        assert kwargs.get("nx") is True

    @pytest.mark.asyncio
    async def test_usdc_priced_at_one_dollar_no_oracle_read(self, executor, mock_loader):
        fake_redis = MagicMock()
        fake_redis.exists = AsyncMock(return_value=0)
        fake_redis.set = AsyncMock(return_value=True)
        fake_redis.aclose = AsyncMock()

        usdc = executor._mock_cc.settings.usdc_address
        with patch("redis.asyncio.from_url", return_value=fake_redis):
            await executor._write_price_baseline_if_absent("0xVault", [usdc], [10000])

        mock_loader.oracle_for.assert_not_called()
        args, _kwargs = fake_redis.set.call_args
        assert json.loads(args[1]) == {usdc.lower(): 1.0}

    @pytest.mark.asyncio
    async def test_redis_failure_does_not_raise_or_block_allocation_set(self, executor, mock_loader):
        """Fail-soft asymmetry (#1103): a Redis failure while writing the
        baseline must never break the already-succeeded on-chain
        setTargetAllocations call — log and continue."""
        mock_cc = executor._mock_cc
        account = MagicMock()
        account.address = "0xAbC0000000000000000000000000000000000001"
        account.sign_transaction.return_value.raw_transaction = b"\x01" * 32
        mock_cc.settings.agent_account = account

        vault = mock_loader.vault.return_value
        vault.functions.setTargetAllocations.return_value.build_transaction = AsyncMock(return_value={})

        oracle = MagicMock()
        oracle.functions.price.return_value.call = AsyncMock(return_value=100_000_000)
        mock_loader.oracle_for.return_value = oracle

        tsla = "0xE745C07d7d32A1Ca0d6162A1c50e876619CF7388"
        with (
            patch("archimedes.chain.executor.circle_signer") as mock_signer,
            patch("redis.asyncio.from_url", side_effect=ConnectionError("redis down")),
        ):
            mock_signer.is_configured = False
            tx_hash = await executor.set_target_allocations("0xVault", [tsla], [10000])

        assert tx_hash  # the on-chain allocation-setting call still "succeeded"

    @pytest.mark.asyncio
    async def test_unknown_token_skipped_not_priced(self, executor, mock_loader):
        """A token with no matching synth/USDC address (e.g. an unrecognized
        or not-yet-onboarded asset) is left out of the baseline entirely
        rather than guessed at — _compute_returns then correctly treats it as
        having no baseline instead of a wrong one."""
        fake_redis = MagicMock()
        fake_redis.exists = AsyncMock(return_value=0)
        fake_redis.set = AsyncMock(return_value=True)
        fake_redis.aclose = AsyncMock()

        with patch("redis.asyncio.from_url", return_value=fake_redis):
            await executor._write_price_baseline_if_absent("0xVault", ["0xDeadBeef"], [10000])

        fake_redis.set.assert_not_called()  # nothing usable to write


class TestBaselineCompleteOrNothing:
    """#1201 review major: a PARTIAL snapshot written via SET NX permanently
    forecloses a vault's returns (the reader requires every allocated token;
    NX blocks repair). The writer is therefore complete-or-nothing: any
    failed or unpriceable token aborts the WRITE, leaving no key, so the
    next tick retries with a fresh chance at a complete baseline.
    Mutation-proven: the pre-fix writer (swallow-and-continue + non-empty
    check) fails test_transient_oracle_failure_writes_nothing by persisting
    the one-token partial."""

    @pytest.mark.asyncio
    async def test_transient_oracle_failure_writes_nothing(self, executor, mock_loader):
        fake_redis = MagicMock()
        fake_redis.exists = AsyncMock(return_value=0)
        fake_redis.set = AsyncMock(return_value=True)
        fake_redis.aclose = AsyncMock()

        good = MagicMock()
        good.functions.price.return_value.call = AsyncMock(return_value=100_000_000)
        bad = MagicMock()
        bad.functions.price.return_value.call = AsyncMock(side_effect=ConnectionError("rpc blip"))
        mock_loader.oracle_for.side_effect = lambda sym: {"sTSLA": good, "sSPY": bad}[sym]

        cc = executor._mock_cc
        tsla = next(a for s_, a in cc.settings.synth_addresses.items() if s_ == "sTSLA")
        spy = next(a for s_, a in cc.settings.synth_addresses.items() if s_ == "sSPY")

        with patch("redis.asyncio.from_url", return_value=fake_redis):
            await executor._write_price_baseline_if_absent("0xVault", [tsla, spy], [5000, 5000])

        fake_redis.set.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_token_writes_nothing(self, executor, mock_loader):
        fake_redis = MagicMock()
        fake_redis.exists = AsyncMock(return_value=0)
        fake_redis.set = AsyncMock(return_value=True)
        fake_redis.aclose = AsyncMock()

        with patch("redis.asyncio.from_url", return_value=fake_redis):
            await executor._write_price_baseline_if_absent(
                "0xVault", ["0x000000000000000000000000000000000000dEaD"], [10000]
            )

        fake_redis.set.assert_not_awaited()
        mock_loader.oracle_for.assert_not_called()

    @pytest.mark.asyncio
    async def test_existing_baseline_skips_all_oracle_reads(self, executor, mock_loader):
        """#1201 review minor: an immutable already-written baseline means the
        per-tick oracle RPC reads are pure waste — the EXISTS pre-check skips
        them entirely."""
        fake_redis = MagicMock()
        fake_redis.exists = AsyncMock(return_value=1)
        fake_redis.set = AsyncMock(return_value=True)
        fake_redis.aclose = AsyncMock()

        cc = executor._mock_cc
        tsla = next(a for s_, a in cc.settings.synth_addresses.items() if s_ == "sTSLA")
        with patch("redis.asyncio.from_url", return_value=fake_redis):
            await executor._write_price_baseline_if_absent("0xVault", [tsla], [10000])

        mock_loader.oracle_for.assert_not_called()
        fake_redis.set.assert_not_awaited()
