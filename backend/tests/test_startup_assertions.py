"""Unit tests for the two #1240 mainnet-gate startup assertions in main.py:

  1. ``_assert_marketplace_live_or_dry`` — refuse to boot when circlekit
     failed to import while PAYMENTS_DRY_RUN=false.
  2. ``_assert_gateway_chain_matches_rpc`` — refuse to boot when GATEWAY_CHAIN
     resolves to a chain_id the configured RPC doesn't actually report.

Both are pulled out of the ``lifespan`` startup sequence into standalone
functions specifically so they can be unit-tested directly, without
exercising the whole app import or the full lifespan (which touches the DB,
Redis, corpus seeding, etc.) — see ``test_lifespan_no_rigor_backfill.py`` for
the precedent of importing ``archimedes.main`` as a module in-process; every
other test file in this suite already does the same, so this file doesn't
need subprocess isolation the way ``test_main_ssm_prod_gate.py`` does for a
true module-level import-time side effect (see
``test_marketplace_kill_switch_boot_gate.py`` for that end-to-end proof of
assertion 1's actual module-level wiring).
"""

from __future__ import annotations

import archimedes.main as main_module
import pytest


class TestAssertMarketplaceLiveOrDry:
    def test_circlekit_missing_plus_real_money_is_fatal(self):
        with pytest.raises(RuntimeError, match="circlekit failed to import"):
            main_module._assert_marketplace_live_or_dry(None, False)

    def test_circlekit_missing_but_dry_run_is_fine(self):
        main_module._assert_marketplace_live_or_dry(None, True)  # must not raise

    def test_circlekit_present_and_real_money_is_fine(self):
        main_module._assert_marketplace_live_or_dry(object(), False)  # must not raise

    def test_circlekit_present_and_dry_run_is_fine(self):
        main_module._assert_marketplace_live_or_dry(object(), True)  # must not raise


class _FakeChainClient:
    def __init__(self, chain_id: int):
        self._chain_id = chain_id

    async def get_chain_id(self) -> int:
        return self._chain_id


class TestAssertGatewayChainMatchesRpc:
    @pytest.mark.asyncio
    async def test_matching_chain_id_is_fine(self):
        # arcTestnet's real chain_id (circlekit.constants) is 5042002.
        await main_module._assert_gateway_chain_matches_rpc(
            _FakeChainClient(5042002), "arcTestnet", payments_dry_run=False
        )  # must not raise

    @pytest.mark.asyncio
    async def test_mismatched_chain_id_is_fatal_when_not_dry_run(self):
        with pytest.raises(RuntimeError, match="resolves to chain_id=5042002"):
            await main_module._assert_gateway_chain_matches_rpc(
                _FakeChainClient(1),  # RPC reports Ethereum mainnet's chain_id
                "arcTestnet",
                payments_dry_run=False,
            )

    @pytest.mark.asyncio
    async def test_mismatched_chain_id_is_only_a_warning_under_dry_run(self, caplog):
        with caplog.at_level("WARNING"):
            await main_module._assert_gateway_chain_matches_rpc(
                _FakeChainClient(1), "arcTestnet", payments_dry_run=True
            )  # must not raise
        assert "resolves to chain_id=5042002" in caplog.text

    @pytest.mark.asyncio
    async def test_the_mainnet_alias_footgun_is_caught(self):
        """The exact scenario named in issue #1240: CHAIN_ALIASES maps
        "mainnet" -> "ethereum" (chain_id 1), so GATEWAY_CHAIN=mainnet on an
        Arc-testnet-configured RPC must be caught, not silently accepted."""
        with pytest.raises(RuntimeError, match="chain_id=1"):
            await main_module._assert_gateway_chain_matches_rpc(
                _FakeChainClient(5042002),  # RPC is actually Arc testnet
                "mainnet",  # resolves to ethereum, chain_id=1
                payments_dry_run=False,
            )

    @pytest.mark.asyncio
    async def test_unresolvable_chain_name_is_fatal_when_not_dry_run(self):
        with pytest.raises(RuntimeError, match="not a chain circlekit recognizes"):
            await main_module._assert_gateway_chain_matches_rpc(
                _FakeChainClient(5042002), "not-a-real-chain", payments_dry_run=False
            )

    @pytest.mark.asyncio
    async def test_unresolvable_chain_name_is_only_a_warning_under_dry_run(self, caplog):
        with caplog.at_level("WARNING"):
            await main_module._assert_gateway_chain_matches_rpc(
                _FakeChainClient(5042002), "not-a-real-chain", payments_dry_run=True
            )  # must not raise
        assert "not a chain circlekit recognizes" in caplog.text
