"""D4: Subscriber-registry source of truth — Postgres-backed rehydration tests.

Verifies that MarketService.start_publisher treats Postgres as truth when
subscribers are provided, and Redis as a rebuildable cache.
"""

from __future__ import annotations

from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest
from archimedes.marketplace.service import MarketService, Subscriber


@pytest.fixture
def market():
    """MarketService with mocked state and no running publishers."""
    svc = MarketService(interval_seconds=9999, payments_dry_run=True, paper_trading=True)
    svc.state = MagicMock()
    svc.state.load_subscribers = AsyncMock()
    svc.state.save_subscribers = AsyncMock()
    svc.publishers = {}
    return svc


def _make_sub(sub_id_suffix: str, active: bool = True) -> Subscriber:
    return Subscriber(
        sub_id="0x" + sub_id_suffix * 32,
        pool_id="0x" + "aa" * 32,
        vault_address="0xvault",
        ephemeral_wallet="0xeph",
        subscriber_wallet="0xsub",
        active=active,
    )


# ─── start_publisher with subscribers (Postgres truth path) ─────────────


@pytest.mark.asyncio
async def test_start_publisher_with_subscribers_writes_through(market: MarketService):
    """When subscribers are provided, they are written to Redis and used as-is."""
    subs = {"0x" + "bb" * 32: _make_sub("bb")}

    await market.start_publisher(
        strategy_id="strat_a",
        pool_id="0x" + "cc" * 32,
        vault_address="0xvault_a",
        creator_wallet="0xcreator",
        subscribers=subs,
    )

    # Redis was written through
    expected_redis = {"0x" + "bb" * 32: vars(subs["0x" + "bb" * 32])}
    market.state.save_subscribers.assert_awaited_once_with("strat_a", expected_redis)

    # In-memory state is correct
    pub = market.publishers["strat_a"]
    assert pub.strategy_id == "strat_a"
    assert len(pub.subscribers) == 1
    assert pub.subscribers["0x" + "bb" * 32].sub_id == "0x" + "bb" * 32

    # load_subscribers was NOT called (no Redis fallback)
    market.state.load_subscribers.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_publisher_with_empty_subscribers_overwrites_redis(market: MarketService):
    """Providing empty subscribers clears stale Redis data (Postgres truth)."""
    await market.start_publisher(
        strategy_id="strat_b",
        pool_id="0x" + "dd" * 32,
        vault_address="0xvault_b",
        creator_wallet="0xcreator",
        subscribers={},
    )

    # Redis was overwritten with empty dict
    market.state.save_subscribers.assert_awaited_once_with("strat_b", {})

    # No subscribers in memory
    assert len(market.publishers["strat_b"].subscribers) == 0


# ─── start_publisher without subscribers (live /publish path) ───────────


@pytest.mark.asyncio
async def test_start_publisher_without_subscribers_falls_back_to_redis(market: MarketService):
    """When no subscribers arg given, subscribers are loaded from Redis."""
    redis_data = {
        "0x" + "ee" * 32: {
            "sub_id": "0x" + "ee" * 32,
            "pool_id": "0x" + "aa" * 32,
            "vault_address": "0xvault",
            "ephemeral_wallet": "0xeph",
            "subscriber_wallet": "0xsub",
            "active": True,
        }
    }
    market.state.load_subscribers = AsyncMock(return_value=redis_data)

    await market.start_publisher(
        strategy_id="strat_c",
        pool_id="0x" + "ff" * 32,
        vault_address="0xvault_c",
        creator_wallet="0xcreator",
    )

    # load_subscribers was called
    market.state.load_subscribers.assert_awaited_once_with("strat_c")

    # save_subscribers was NOT called (no write-through path)
    market.state.save_subscribers.assert_not_awaited()

    # Subscriber restored from Redis
    assert len(market.publishers["strat_c"].subscribers) == 1
    assert market.publishers["strat_c"].subscribers["0x" + "ee" * 32].sub_id == "0x" + "ee" * 32


# ─── Postgres wins over stale Redis ─────────────────────────────────────


@pytest.mark.asyncio
async def test_postgres_subscribers_win_over_stale_redis(market: MarketService):
    """Providing subscribers (Postgres truth) overwrites Redis even if stale data exists there."""
    # Redis has stale data that differs from what Postgres provides
    stale_redis = {
        "0x" + "gg" * 32: {
            "sub_id": "0x" + "gg" * 32,
            "pool_id": "0x" + "aa" * 32,
            "vault_address": "0xstale_vault",
            "ephemeral_wallet": "0xstale_eph",
            "subscriber_wallet": "0xstale_sub",
            "active": True,
        }
    }
    market.state.load_subscribers = AsyncMock(return_value=stale_redis)

    # Postgres provides different subscribers (empty — strategy had no running subs)
    await market.start_publisher(
        strategy_id="strat_d",
        pool_id="0x" + "hh" * 32,
        vault_address="0xvault_d",
        creator_wallet="0xcreator",
        subscribers={},  # Postgres says zero running subs
    )

    # Redis was overwritten with Postgres truth (empty)
    market.state.save_subscribers.assert_awaited_once_with("strat_d", {})

    # load_subscribers was NOT called — Postgres path skips Redis read
    market.state.load_subscribers.assert_not_awaited()

    # No subscribers loaded
    assert len(market.publishers["strat_d"].subscribers) == 0


# ─── idempotency preserved ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_publisher_idempotent_with_subscribers(market: MarketService):
    """Calling start_publisher twice with subscribers is idempotent."""
    subs = {"0x" + "ii" * 32: _make_sub("ii")}

    await market.start_publisher(
        strategy_id="strat_e",
        pool_id="0x" + "jj" * 32,
        vault_address="0xvault_e",
        creator_wallet="0xcreator",
        subscribers=subs,
    )
    assert "strat_e" in market.publishers

    # Second call is a no-op (task already set)
    market.state.save_subscribers.reset_mock()
    await market.start_publisher(
        strategy_id="strat_e",
        pool_id="0x" + "jj" * 32,
        vault_address="0xvault_e",
        creator_wallet="0xcreator",
        subscribers=subs,
    )
    market.state.save_subscribers.assert_not_awaited()


# ─── two strategies, each with its own subscribers ──────────────────────


@pytest.mark.asyncio
async def test_two_strategies_independent_subscribers(market: MarketService):
    """Two publishers started with distinct subscriber sets work independently."""
    subs_a = {"0x" + "kk" * 32: _make_sub("kk")}
    subs_b = {
        "0x" + "ll" * 32: _make_sub("ll"),
        "0x" + "mm" * 32: _make_sub("mm"),
    }

    await market.start_publisher(
        strategy_id="strat_f",
        pool_id="0x" + "nn" * 32,
        vault_address="0xvault_f",
        creator_wallet="0xcreator",
        subscribers=subs_a,
    )
    await market.start_publisher(
        strategy_id="strat_g",
        pool_id="0x" + "oo" * 32,
        vault_address="0xvault_g",
        creator_wallet="0xcreator",
        subscribers=subs_b,
    )

    assert len(market.publishers["strat_f"].subscribers) == 1
    assert len(market.publishers["strat_g"].subscribers) == 2

    # Each wrote its own cache
    expected_a = {"0x" + "kk" * 32: vars(subs_a["0x" + "kk" * 32])}
    expected_b = {
        "0x" + "ll" * 32: vars(subs_b["0x" + "ll" * 32]),
        "0x" + "mm" * 32: vars(subs_b["0x" + "mm" * 32]),
    }
    market.state.save_subscribers.assert_has_awaits(
        [
            mock.call("strat_f", expected_a),
            mock.call("strat_g", expected_b),
        ]
    )


# ─── halt-state rehydration (C-5) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_halted_subscriber_rehydrates_as_inactive(market: MarketService):
    """A previously halted subscriber rehydrates as inactive (C-5)."""
    subs = {"0x" + "nn" * 32: _make_sub("nn", active=False)}

    await market.start_publisher(
        strategy_id="strat_h",
        pool_id="0x" + "oo" * 32,
        vault_address="0xvault_h",
        creator_wallet="0xcreator",
        subscribers=subs,
    )

    sub = market.publishers["strat_h"].subscribers["0x" + "nn" * 32]
    assert sub.active is False

    # Redis was written through with active=False
    saved = market.state.save_subscribers.call_args[0][1]
    assert saved["0x" + "nn" * 32]["active"] is False


@pytest.mark.asyncio
async def test_never_halted_subscriber_still_rehydrates_active(market: MarketService):
    """A never-halted subscriber still rehydrates as active (C-5 regression)."""
    subs = {"0x" + "pp" * 32: _make_sub("pp", active=True)}

    await market.start_publisher(
        strategy_id="strat_i",
        pool_id="0x" + "qq" * 32,
        vault_address="0xvault_i",
        creator_wallet="0xcreator",
        subscribers=subs,
    )

    sub = market.publishers["strat_i"].subscribers["0x" + "pp" * 32]
    assert sub.active is True
