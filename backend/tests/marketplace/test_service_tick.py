"""Tests for MarketService.tick() — non-dry-run engine path.

Migrated for the per-step charging pipeline (F7).  Tests now patch the
individual step seams (provider.get_strategy, strategy_evaluator) instead
of the fused _evaluate method.
"""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from archimedes.marketplace.service import MarketService, Publisher, Subscriber
from archimedes.marketplace.tick_registry import TickStep
from archimedes.services.strategy_signal_evaluator import StrategySignals


def _dummy_strategy():
    """Return a minimal strategy object for provider.get_strategy."""
    return MagicMock(spec=["id"], id="test_strat")


def _dummy_signals():
    """Build a minimal StrategySignals list for evaluate_strategies mock returns."""
    from archimedes.services.strategy_signal_evaluator import AssetSignal, Signal

    ss = MagicMock(spec=StrategySignals)
    ss.signals = [
        AssetSignal(
            asset="ETH",
            signal=Signal.LONG,
            weight=0.5,
            reason="test",
            strategy_id="test_strat",
            strategy_name="test",
        ),
    ]
    ss.paper_title = "test"
    ss.strategy_id = "test_strat"
    return [ss]


def _dummy_targets():
    """Build a minimal TargetAllocation list that passes VCheck."""
    from archimedes.models.portfolio import TargetAllocation

    return [
        TargetAllocation(symbol="ETH", token_address="0x" + "ee" * 20, weight=0.5, strategy_ids=["test_strat"]),
        TargetAllocation(symbol="USDC", token_address="0x" + "ff" * 20, weight=0.5, strategy_ids=[]),
    ]


@pytest.fixture
def market():
    svc = MarketService(interval_seconds=9999, payments_dry_run=False, paper_trading=False)
    # Mock the heavy dependencies
    svc.executor = MagicMock()
    svc.executor.read_portfolio = AsyncMock(return_value=MagicMock(total_value_usdc=10000, weights_dict={}))
    svc.executor.execute_trades = AsyncMock()
    svc.executor.set_token_oracles = AsyncMock()
    svc.executor.set_target_allocations = AsyncMock()
    svc.signer = MagicMock()
    svc.signer.is_configured = False
    # Mock the chain contract loader
    svc.loader = MagicMock()
    # Mock state
    svc.state = MagicMock()
    svc.state.try_acquire_leader = AsyncMock(return_value="test-token")
    svc.state.renew_leader = AsyncMock()
    svc.state.release_leader = AsyncMock()
    svc.state.append_event = AsyncMock()
    svc.state.save_subscribers = AsyncMock()
    svc.state.load_subscribers = AsyncMock(return_value={})
    # Regime lock gate: skip actual classification, use cache-miss path
    svc.state.try_acquire_regime_lock = AsyncMock(return_value=False)
    svc.state.store = MagicMock()
    svc.state.store.load_regime = AsyncMock(return_value=None)
    svc.state.store.save_last_rebalance = AsyncMock()
    # Mock portfolio_constructor to return passable allocations
    svc.portfolio_constructor = MagicMock()
    svc.portfolio_constructor.construct = MagicMock(
        return_value=[
            MagicMock(symbol="ETH", weight=0.5, token_address="0x" + "ee" * 20),
            MagicMock(symbol="USDC", weight=0.5, token_address="0x" + "ff" * 20),
        ]
    )
    # Mock _weights_to_targets to return targets with valid addresses
    svc._weights_to_targets = MagicMock(return_value=_dummy_targets())
    # Mock _save_publisher_consensus to no-op
    svc._save_publisher_consensus = AsyncMock()
    # Provider mock for _step_load_strategy
    svc.provider = MagicMock()
    svc.provider.get_strategy = MagicMock(return_value=_dummy_strategy())
    # Mock readiness — the loader mock below doesn't have the ERC20 ABI
    svc._subscriber_is_ready = AsyncMock(return_value=True)
    return svc


def _dummy_trades():
    from archimedes.models.portfolio import TradeDirection, TradeOrder

    return [
        TradeOrder(
            symbol="ETH",
            token_address="",
            direction=TradeDirection.BUY,
            amount=100.0,
            estimated_usdc_value=100.0,
        ),
    ]


# ── step-level patches reused across tests ──────────────────────────────


def _patch_strategy_evaluator(exit_stack: ExitStack, weights: dict | None = None):
    """Patch strategy_evaluator module-level imports in service.py via an ExitStack.

    Returns nothing — callers must already have an ExitStack active.
    """
    w = weights if weights is not None else {"ETH": 0.5}
    exit_stack.enter_context(
        patch("archimedes.marketplace.service.strategy_evaluator.evaluate_strategies", return_value=_dummy_signals()),
    )
    exit_stack.enter_context(
        patch("archimedes.marketplace.service.strategy_evaluator.aggregate_signals", return_value=w),
    )


@pytest.mark.asyncio
async def test_tick_produces_trades_and_verifies_payment(market: MarketService):
    """The economic core: pipeline clears, charge_one is called, publisher vault trades."""
    with ExitStack() as stack:
        _patch_strategy_evaluator(stack)
        stack.enter_context(patch("archimedes.marketplace.service.compute_trades", return_value=_dummy_trades()))
        stack.enter_context(patch.object(market, "_charge_one", AsyncMock(return_value=(True, None, False))))
        stack.enter_context(patch.object(market, "record_subscriber_tick", AsyncMock()))

        market.publishers["strat_a"] = Publisher(
            strategy_id="strat_a",
            pool_id="0x" + "aa" * 32,
            vault_address="0xpublisher_vault",
            creator_wallet="0xpublisher",
            gateway_seller_address="0xSeller000000000000000000000000000000000001",
            agent_wallet_id="wallet_pub_a",
        )
        market.publishers["strat_a"].subscribers["sub_1"] = Subscriber(
            sub_id="0x" + "bb" * 32,
            pool_id="0x" + "cc" * 32,
            vault_address="0xsub_vault",
            ephemeral_wallet="0xephemeral",
            subscriber_wallet="0xsubscriber",
            active=True,
        )

        await market.tick("strat_a")

        # _charge_one was called at least once with the subscriber (2nd arg)
        charge_calls = [c for c in market._charge_one.await_args_list if c[0][1].sub_id == "0x" + "bb" * 32]
        assert len(charge_calls) >= 13  # 13 pipeline steps

        # execute_trades was called for publisher vault
        market.executor.execute_trades.assert_any_call("0xpublisher_vault", _dummy_trades())

        # state.save_subscribers was called
        market.state.save_subscribers.assert_awaited()


@pytest.mark.asyncio
async def test_tick_marks_subscriber_inactive_on_payment_failure(market: MarketService):
    """When charge_one fails for a subscriber, they are deferred."""
    with ExitStack() as stack:
        _patch_strategy_evaluator(stack)
        stack.enter_context(patch("archimedes.marketplace.service.compute_trades", return_value=_dummy_trades()))
        stack.enter_context(patch.object(market, "_charge_one", AsyncMock(return_value=(False, None, False))))
        stack.enter_context(patch.object(market, "record_subscriber_tick", AsyncMock()))

        market.publishers["strat_b"] = Publisher(
            strategy_id="strat_b",
            pool_id="0x" + "dd" * 32,
            vault_address="0xpublisher_vault",
            creator_wallet="0xpublisher",
        )
        market.publishers["strat_b"].subscribers["sub_2"] = Subscriber(
            sub_id="0x" + "ee" * 32,
            pool_id="0x" + "ff" * 32,
            vault_address="0xsub_vault",
            ephemeral_wallet="0xephemeral",
            subscriber_wallet="0xsubscriber",
            active=True,
        )

        await market.tick("strat_b")

        # subscriber marked inactive after first charge failure
        assert market.publishers["strat_b"].subscribers["sub_2"].active is False


@pytest.mark.asyncio
async def test_tick_no_trades_skips_payment(market: MarketService):
    """When compute_trades returns empty, pipeline halts at NO_DRIFT_DEDUP."""
    with ExitStack() as stack:
        _patch_strategy_evaluator(stack, weights={"ETH": 0.5})
        stack.enter_context(patch("archimedes.marketplace.service.compute_trades", return_value=[]))
        stack.enter_context(patch.object(market, "record_subscriber_tick", AsyncMock()))
        mock_charge = stack.enter_context(
            patch.object(market, "_charge_one", AsyncMock(return_value=(True, None, False)))
        )

        market.publishers["strat_c"] = Publisher(
            strategy_id="strat_c",
            pool_id="0x" + "11" * 32,
            vault_address="0xpublisher_vault",
            creator_wallet="0xpublisher",
        )

        await market.tick("strat_c")

        # _charge_one was called (charged for pipeline steps up to NO_DRIFT_DEDUP)
        # but not for REBALANCE (pipeline halted)
        charge_steps = [c.kwargs["step"] for c in mock_charge.await_args_list if "step" in c.kwargs]
        assert TickStep.REBALANCE.value not in charge_steps
        market.executor.execute_trades.assert_not_called()


# ── H3 — Liability ledger (charge-succeeds / mirror-fails) ─────────────


@pytest.mark.asyncio
async def test_tick_records_liability_when_mirror_fails(market: MarketService):
    """When payment succeeds but the subscriber mirror trade fails, a liability
    is recorded for that subscriber."""
    with ExitStack() as stack:
        _patch_strategy_evaluator(stack)
        stack.enter_context(patch("archimedes.marketplace.service.compute_trades", return_value=_dummy_trades()))
        stack.enter_context(patch.object(market, "_charge_one", AsyncMock(return_value=(True, None, False))))
        stack.enter_context(patch.object(market, "record_subscriber_tick", AsyncMock()))

        market.publishers["strat_d"] = Publisher(
            strategy_id="strat_d",
            pool_id="0x" + "11" * 32,
            vault_address="0xpublisher_vault",
            creator_wallet="0xpublisher",
        )
        market.publishers["strat_d"].subscribers["sub_3"] = Subscriber(
            sub_id="0x" + "22" * 32,
            pool_id="0x" + "33" * 32,
            vault_address="0xsub_vault",
            ephemeral_wallet="0xephemeral",
            subscriber_wallet="0xsubscriber",
            active=True,
        )

        mock_record = stack.enter_context(patch.object(market, "_record_liability", AsyncMock()))
        stack.enter_context(
            patch.object(market, "_apply_to_subscriber", AsyncMock(return_value=(False, RuntimeError("test"))))
        )

        await market.tick("strat_d")

        # Liability was recorded for the subscriber
        mock_record.assert_awaited_once()
        args = mock_record.await_args.args
        assert args[0].sub_id == "0x" + "22" * 32
        assert args[1] == "strat_d"
        assert args[2] is not None  # tick_id
        assert args[3] == len(_dummy_trades())  # action_count


@pytest.mark.asyncio
async def test_tick_no_liability_when_mirror_succeeds(market: MarketService):
    """When mirror succeeds, no liability is recorded."""
    with ExitStack() as stack:
        _patch_strategy_evaluator(stack)
        stack.enter_context(patch("archimedes.marketplace.service.compute_trades", return_value=_dummy_trades()))
        stack.enter_context(patch.object(market, "_charge_one", AsyncMock(return_value=(True, None, False))))
        stack.enter_context(patch.object(market, "record_subscriber_tick", AsyncMock()))

        market.publishers["strat_e"] = Publisher(
            strategy_id="strat_e",
            pool_id="0x" + "44" * 32,
            vault_address="0xpublisher_vault",
            creator_wallet="0xpublisher",
        )
        market.publishers["strat_e"].subscribers["sub_4"] = Subscriber(
            sub_id="0x" + "55" * 32,
            pool_id="0x" + "66" * 32,
            vault_address="0xsub_vault",
            ephemeral_wallet="0xephemeral",
            subscriber_wallet="0xsubscriber",
            active=True,
        )

        mock_record = stack.enter_context(patch.object(market, "_record_liability", AsyncMock()))
        stack.enter_context(patch.object(market, "_apply_to_subscriber", AsyncMock(return_value=(True, []))))

        await market.tick("strat_e")
        mock_record.assert_not_called()


@pytest.mark.asyncio
async def test_tick_no_liability_when_payment_fails(market: MarketService):
    """When payment fails, no liability is recorded."""
    with ExitStack() as stack:
        _patch_strategy_evaluator(stack)
        stack.enter_context(patch("archimedes.marketplace.service.compute_trades", return_value=_dummy_trades()))
        stack.enter_context(patch.object(market, "_charge_one", AsyncMock(return_value=(False, None, False))))
        stack.enter_context(patch.object(market, "record_subscriber_tick", AsyncMock()))

        market.publishers["strat_f"] = Publisher(
            strategy_id="strat_f",
            pool_id="0x" + "77" * 32,
            vault_address="0xpublisher_vault",
            creator_wallet="0xpublisher",
        )
        market.publishers["strat_f"].subscribers["sub_5"] = Subscriber(
            sub_id="0x" + "88" * 32,
            pool_id="0x" + "99" * 32,
            vault_address="0xsub_vault",
            ephemeral_wallet="0xephemeral",
            subscriber_wallet="0xsubscriber",
            active=True,
        )

        mock_record = stack.enter_context(patch.object(market, "_record_liability", AsyncMock()))

        await market.tick("strat_f")
        mock_record.assert_not_called()


@pytest.mark.asyncio
async def test_record_liability_best_effort(market: MarketService):
    """``_record_liability`` is best-effort: a DB failure logs but does not raise."""
    sub = Subscriber(
        sub_id="0x" + "aa" * 32,
        pool_id="0x" + "bb" * 32,
        vault_address="0xsub_vault",
        ephemeral_wallet="0xephemeral",
        subscriber_wallet="0xsubscriber",
        active=True,
    )

    before_count = market.state.append_event.await_count

    with patch("archimedes.marketplace.service.get_session", side_effect=RuntimeError("DB down")):
        await market._record_liability(sub, "strat_g", "tick_1", 3)

    assert market.state.append_event.await_count == before_count


@pytest.mark.asyncio
async def test_record_liability_uses_env_fee_no_chain_call(market: MarketService):
    """``_record_liability`` uses FLAT_FEE_PER_ACTION directly (never hits chain)."""
    from archimedes.marketplace.service import FLAT_FEE_PER_ACTION

    sub = Subscriber(
        sub_id="0x" + "cc" * 32,
        pool_id="0x" + "dd" * 32,
        vault_address="0xsub_vault2",
        ephemeral_wallet="0xeph2",
        subscriber_wallet="0xsub2",
        active=True,
    )

    inserted = {}

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def add(self, obj):
            inserted["obj"] = obj

        def commit(self):
            pass

    with patch("archimedes.marketplace.service.get_session", return_value=FakeSession()):
        await market._record_liability(sub, "strat_h", "tick_2", 5)

    assert inserted["obj"].unit_price_usdc == FLAT_FEE_PER_ACTION
    assert inserted["obj"].amount_owed_usdc == 5 * FLAT_FEE_PER_ACTION


# ── TASK 17 — Leader lock: per-strategy isolation + explicit release + renewal ─


@pytest.mark.asyncio
async def test_tick_releases_leader_lock_in_finally(market: MarketService):
    """Tick releases the per-strategy leader lock in ``finally`` after a
    successful run (C3, explicit release)."""
    market.publishers["strat_release"] = Publisher(
        strategy_id="strat_release",
        pool_id="0x" + "aa" * 32,
        vault_address="0xpub",
        creator_wallet="0xcreator",
    )
    with ExitStack() as stack:
        _patch_strategy_evaluator(stack, weights={})
        stack.enter_context(patch("archimedes.marketplace.service.compute_trades", return_value=[]))
        stack.enter_context(patch.object(market, "record_subscriber_tick", AsyncMock()))
        await market.tick("strat_release", leader_token="t1")

    market.state.release_leader.assert_awaited_once_with("strat_release", token="t1")


@pytest.mark.asyncio
async def test_tick_releases_leader_lock_on_exception(market: MarketService):
    """Tick releases the per-strategy lock even when tick fails midway."""
    market.publishers["strat_err"] = Publisher(
        strategy_id="strat_err",
        pool_id="0x" + "bb" * 32,
        vault_address="0xpub",
        creator_wallet="0xcreator",
    )

    # provider.get_strategy raising causes an exception in _step_load_strategy
    # which is caught by the pipeline loop → no RuntimeError propagates.
    # Instead verify release_leader is still called.
    with patch.object(market, "provider", MagicMock()) as mock_prov:
        mock_prov.get_strategy = MagicMock(side_effect=RuntimeError("boom"))
        # tick catches the exception internally
        await market.tick("strat_err", leader_token="t2")

    market.state.release_leader.assert_awaited_once_with("strat_err", token="t2")


@pytest.mark.asyncio
async def test_tick_renews_leader_at_midpoint(market: MarketService):
    """renew_leader is called exactly once per tick, midway through the
    subscriber list (crash-safety insurance)."""
    with ExitStack() as stack:
        _patch_strategy_evaluator(stack)
        stack.enter_context(patch("archimedes.marketplace.service.compute_trades", return_value=_dummy_trades()))
        stack.enter_context(patch.object(market, "_charge_one", AsyncMock(return_value=(True, None, False))))
        stack.enter_context(patch.object(market, "record_subscriber_tick", AsyncMock()))
        stack.enter_context(patch.object(market, "_apply_to_subscriber", AsyncMock(return_value=(True, []))))

        market.publishers["strat_renew"] = Publisher(
            strategy_id="strat_renew",
            pool_id="0x" + "cc" * 32,
            vault_address="0xpub",
            creator_wallet="0xcreator",
        )
        market.publishers["strat_renew"].subscribers["sub_1"] = Subscriber(
            sub_id="0x" + "11" * 32,
            pool_id="0x" + "22" * 32,
            vault_address="0xsub_vault",
            ephemeral_wallet="0xephemeral",
            subscriber_wallet="0xsub",
            active=True,
        )
        market.publishers["strat_renew"].subscribers["sub_2"] = Subscriber(
            sub_id="0x" + "33" * 32,
            pool_id="0x" + "44" * 32,
            vault_address="0xsub_vault",
            ephemeral_wallet="0xephemeral",
            subscriber_wallet="0xsub",
            active=True,
        )

        await market.tick("strat_renew", leader_token="t3")

    market.state.renew_leader.assert_awaited_once_with("strat_renew", token="t3")


@pytest.mark.asyncio
async def test_two_strategies_tick_independently(market: MarketService):
    """Two strategies both complete their tick bodies — the tick() method
    is not gated by a global lock."""
    with ExitStack() as stack:
        _patch_strategy_evaluator(stack, weights={})
        stack.enter_context(patch("archimedes.marketplace.service.compute_trades", return_value=[]))
        stack.enter_context(patch.object(market, "record_subscriber_tick", AsyncMock()))

        market.publishers["strat_a"] = Publisher(
            strategy_id="strat_a",
            pool_id="0x" + "aa" * 32,
            vault_address="0xpub_a",
            creator_wallet="0xcreator",
        )
        market.publishers["strat_b"] = Publisher(
            strategy_id="strat_b",
            pool_id="0x" + "bb" * 32,
            vault_address="0xpub_b",
            creator_wallet="0xcreator",
        )

        await market.tick("strat_a")
        await market.tick("strat_b")

    assert market.state.release_leader.await_count == 2


@pytest.mark.asyncio
async def test_run_loop_uses_per_strategy_key(market: MarketService):
    """_run_loop acquires the per-strategy lock on each iteration."""
    market.state.try_acquire_leader = AsyncMock(return_value="loop-token")

    pub = Publisher(
        strategy_id="strat_loop",
        pool_id="0x" + "dd" * 32,
        vault_address="0xpub",
        creator_wallet="0xcreator",
    )
    pub.retired = True
    market.publishers["strat_loop"] = pub

    with patch.object(market, "tick", AsyncMock()):
        await market._run_loop("strat_loop")

    market.state.try_acquire_leader.assert_awaited_with("strat_loop")
