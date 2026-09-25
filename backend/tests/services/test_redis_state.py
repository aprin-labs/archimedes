"""Unit coverage for AgentStateStore — Redis is mocked via a fake client.

Verifies the key-shape conventions, JSON serialization, sorted-set indexing,
and the list/get/save trace flows. No real Redis connection.

Added 2026-05-24 as part of the #147 coverage-gate lift.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from archimedes.models.regime import (
    ConsensusLabel,
    EnsembleConsensus,
    Regime,
    RegimeClassification,
    RegimeSignals,
)
from archimedes.services.redis_state import (
    KEY_ENSEMBLE_CONSENSUS,
    KEY_HEARTBEAT,
    KEY_LAST_REBALANCE_PREFIX,
    KEY_REGIME,
    KEY_SIWE_NONCE_PREFIX,
    KEY_TRACE_INDEX,
    KEY_TRACE_PREFIX,
    KEY_TRACE_RECONCILE_FIRST_SEEN,
    KEY_TRACE_RECONCILE_PENDING,
    KEY_TRACE_RECONCILE_TERMINAL,
    TRACE_MGET_BATCH,
    AgentStateStore,
    is_dangling_reveal,
)


def _fake_redis() -> MagicMock:
    """Build an AsyncMock-flavored Redis client."""
    r = MagicMock()
    r.get = AsyncMock(return_value=None)
    r.mget = AsyncMock(return_value=[])
    r.set = AsyncMock()
    r.setex = AsyncMock()
    r.getdel = AsyncMock(return_value=None)
    r.lpush = AsyncMock()
    r.ltrim = AsyncMock()
    r.lrange = AsyncMock(return_value=[])
    r.zadd = AsyncMock()
    r.zrevrange = AsyncMock(return_value=[])
    r.zcard = AsyncMock(return_value=0)
    # Reveal-reconciliation durable index (#1353)
    r.sadd = AsyncMock()
    r.srem = AsyncMock()
    r.smembers = AsyncMock(return_value=[])
    r.scard = AsyncMock(return_value=0)
    r.hsetnx = AsyncMock()
    r.hget = AsyncMock(return_value=None)
    r.hdel = AsyncMock()
    r.aclose = AsyncMock()
    return r


async def _store_with_fake_redis() -> tuple[AgentStateStore, MagicMock]:
    """Bind a fake Redis to a fresh AgentStateStore."""
    store = AgentStateStore(url="redis://fake/")
    fake = _fake_redis()
    store._redis = fake  # bypass _get_redis lazy init
    return store, fake


def _seed_trace_bodies(fake: MagicMock, bodies: dict[str, str]) -> None:
    """Back BOTH ``get`` and ``mget`` with one key → raw-JSON map.

    Faithful to the real client on the two points the read paths depend on:
    ``MGET`` returns one slot per requested key, **in request order**, holding
    ``None`` where the key is absent — the same value a ``GET`` on that key
    would have returned.

    Both methods are stubbed even though ``list_traces`` now only calls
    ``mget``, per CLAUDE.md rule 5's corollary: a double that covers only the
    branch under test silently drops a sibling's calls (``get_trace`` and
    ``list_recent_traces`` still read one key at a time). It is also what lets
    the round-trip regression test below fail on the *round-trip count* rather
    than on a missing row, since the reverted per-key loop still resolves
    every body correctly against this same map.
    """

    async def _get(key: str):
        return bodies.get(key)

    async def _mget(keys):
        return [bodies.get(k) for k in keys]

    fake.get = AsyncMock(side_effect=_get)
    fake.mget = AsyncMock(side_effect=_mget)


def _classification(regime: Regime = Regime.RISK_ON, confidence: float = 0.8) -> RegimeClassification:
    return RegimeClassification(
        regime=regime,
        confidence=confidence,
        signals=RegimeSignals(
            vix_level=15.0,
            vix_rate_of_change=0.0,
            sp500_above_ma50=True,
            sp500_above_ma200=True,
        ),
        timestamp=datetime.now(UTC),
    )


class TestRegime:
    @pytest.mark.asyncio
    async def test_save_regime_writes_canonical_json(self) -> None:
        store, fake = await _store_with_fake_redis()
        await store.save_regime(_classification())
        fake.set.assert_awaited_once()
        key, value = fake.set.await_args.args
        assert key == KEY_REGIME
        payload = json.loads(value)
        assert payload["regime"] == Regime.RISK_ON.value
        assert payload["confidence"] == 0.8

    @pytest.mark.asyncio
    async def test_save_ensemble_consensus_writes_to_distinct_key(self) -> None:
        """Ensemble consensus is persisted under its OWN key, not KEY_REGIME (#659)."""
        store, fake = await _store_with_fake_redis()
        signal_summary = MagicMock()
        signal_summary.signals = []
        signal_summary.paper_title = "Faber 2007"
        consensus = EnsembleConsensus.from_signal_counts(flat_count=2, total_count=5)
        await store.save_ensemble_consensus(consensus, [signal_summary])
        fake.set.assert_awaited_once()
        key, value = fake.set.await_args.args
        # Critical: must NOT shadow the market-regime key.
        assert key == KEY_ENSEMBLE_CONSENSUS
        assert key != KEY_REGIME
        payload = json.loads(value)
        # The persisted payload carries a "label" (consensus bucket), not a
        # "regime" — the consensus is not a market regime.
        assert payload["label"] == ConsensusLabel.TRANSITION.value
        assert "regime" not in payload
        assert payload["flat_pct"] == 0.4
        assert payload["strategy_count"] == 5
        # Dynamic confidence: with flat_pct=0.4 and the empty-signals fixture:
        #   vote_ratio = 1.0 - 0.4 = 0.6 ; avg_strength = 0.0 ; dispersion = 0.0
        #   dyn = clamp(0.6 * (0.5 + 0.5 * 0.0) - 0.0, 0.05, 0.99) = 0.3
        assert payload["confidence"] == 0.3
        assert payload["source"] == "strategy_consensus"

    @pytest.mark.asyncio
    async def test_load_regime_returns_none_when_missing(self) -> None:
        store, _ = await _store_with_fake_redis()
        assert await store.load_regime() is None

    @pytest.mark.asyncio
    async def test_load_regime_parses_json(self) -> None:
        store, fake = await _store_with_fake_redis()
        fake.get.return_value = json.dumps({"regime": "risk_on", "confidence": 0.9})
        loaded = await store.load_regime()
        assert loaded["regime"] == "risk_on"

    @pytest.mark.asyncio
    async def test_load_ensemble_consensus_returns_none_when_missing(self) -> None:
        store, _ = await _store_with_fake_redis()
        assert await store.load_ensemble_consensus() is None

    @pytest.mark.asyncio
    async def test_load_ensemble_consensus_parses_json(self) -> None:
        store, fake = await _store_with_fake_redis()
        fake.get.return_value = json.dumps({"label": "risk_off", "confidence": 0.2, "flat_pct": 0.8})
        loaded = await store.load_ensemble_consensus()
        assert loaded["label"] == "risk_off"
        assert loaded["flat_pct"] == 0.8


class TestHeartbeat:
    @pytest.mark.asyncio
    async def test_save_heartbeat_writes_iso_timestamp(self) -> None:
        store, fake = await _store_with_fake_redis()
        await store.save_heartbeat()
        key, ts = fake.set.await_args.args
        assert key == KEY_HEARTBEAT
        # ISO format roundtrips
        datetime.fromisoformat(ts)

    @pytest.mark.asyncio
    async def test_get_heartbeat_returns_raw_string(self) -> None:
        store, fake = await _store_with_fake_redis()
        fake.get.return_value = "2026-05-24T12:00:00+00:00"
        assert await store.get_heartbeat() == "2026-05-24T12:00:00+00:00"


class TestLastRebalance:
    @pytest.mark.asyncio
    async def test_save_uses_lowercased_vault_key(self) -> None:
        store, fake = await _store_with_fake_redis()
        await store.save_last_rebalance("0xV-UPPER")
        key = fake.set.await_args.args[0]
        assert key == f"{KEY_LAST_REBALANCE_PREFIX}0xv-upper"

    @pytest.mark.asyncio
    async def test_get_returns_parsed_datetime(self) -> None:
        store, fake = await _store_with_fake_redis()
        ts = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
        fake.get.return_value = ts
        result = await store.get_last_rebalance("0xV")
        assert isinstance(result, datetime)

    @pytest.mark.asyncio
    async def test_get_returns_none_when_missing(self) -> None:
        store, _ = await _store_with_fake_redis()
        assert await store.get_last_rebalance("0xV") is None


class TestSiweNonces:
    @pytest.mark.asyncio
    async def test_save_nonce_setex_with_ttl(self) -> None:
        store, fake = await _store_with_fake_redis()
        await store.save_nonce("abc123", 300)
        fake.setex.assert_awaited_once_with(f"{KEY_SIWE_NONCE_PREFIX}abc123", 300, "1")

    @pytest.mark.asyncio
    async def test_pop_nonce_present_returns_true(self) -> None:
        store, fake = await _store_with_fake_redis()
        fake.getdel.return_value = "1"
        assert await store.pop_nonce("abc123") is True
        fake.getdel.assert_awaited_once_with(f"{KEY_SIWE_NONCE_PREFIX}abc123")

    @pytest.mark.asyncio
    async def test_pop_nonce_missing_returns_false(self) -> None:
        store, fake = await _store_with_fake_redis()
        fake.getdel.return_value = None
        assert await store.pop_nonce("abc123") is False

    @pytest.mark.asyncio
    async def test_pop_nonce_is_single_use(self) -> None:
        """GETDEL semantics: a second pop for the same key returns False
        once Redis has deleted it (simulated here via side_effect)."""
        store, fake = await _store_with_fake_redis()
        fake.getdel = AsyncMock(side_effect=["1", None])
        assert await store.pop_nonce("abc123") is True
        assert await store.pop_nonce("abc123") is False


class TestEvents:
    @pytest.mark.asyncio
    async def test_save_event_lpushes_and_trims_to_100(self) -> None:
        store, fake = await _store_with_fake_redis()
        await store.save_event("rebalance", {"vault": "0xV"})
        fake.lpush.assert_awaited_once()
        fake.ltrim.assert_awaited_once_with("archimedes:agent:events", 0, 99)
        # JSON payload includes type + data + timestamp
        payload = json.loads(fake.lpush.await_args.args[1])
        assert payload["type"] == "rebalance"
        assert payload["data"] == {"vault": "0xV"}

    @pytest.mark.asyncio
    async def test_get_events_parses_each_entry(self) -> None:
        store, fake = await _store_with_fake_redis()
        fake.lrange.return_value = [
            json.dumps({"type": "a", "data": {}, "timestamp": "x"}),
            json.dumps({"type": "b", "data": {}, "timestamp": "y"}),
        ]
        events = await store.get_events(count=2)
        assert [e["type"] for e in events] == ["a", "b"]


class TestVaultSnapshots:
    @pytest.mark.asyncio
    async def test_save_lpushes_and_trims_to_288(self) -> None:
        store, fake = await _store_with_fake_redis()
        await store.save_vault_snapshot("0xV", {"aum_usdc": 1000})
        fake.lpush.assert_awaited_once()
        # 287 inclusive → keeps 288 entries
        fake.ltrim.assert_awaited_once_with("archimedes:vault:snapshots:0xv", 0, 287)

    @pytest.mark.asyncio
    async def test_get_returns_decoded_snapshots(self) -> None:
        store, fake = await _store_with_fake_redis()
        fake.lrange.return_value = [json.dumps({"aum_usdc": 1100})]
        snaps = await store.get_vault_snapshots("0xV", count=5)
        assert snaps == [{"aum_usdc": 1100}]


class TestTraces:
    @pytest.mark.asyncio
    async def test_save_trace_without_hash_is_silently_dropped(self) -> None:
        store, fake = await _store_with_fake_redis()
        await store.save_trace({"id": "abc", "decision_type": "rebalance"})
        fake.set.assert_not_awaited()
        fake.zadd.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_save_trace_writes_hash_uuid_and_index(self) -> None:
        store, fake = await _store_with_fake_redis()
        trace = {
            "id": "uuid-1",
            "trace_hash": "0xhash",
            "timestamp": "2026-05-24T12:00:00+00:00",
            "vault_address": "0xV",
        }
        await store.save_trace(trace)
        # 2 sets: hash → data, id → hash
        assert fake.set.await_count == 2
        fake.zadd.assert_awaited_once()
        zadd_key, mapping = fake.zadd.await_args.args
        assert zadd_key == KEY_TRACE_INDEX
        assert "0xhash" in mapping

    @pytest.mark.asyncio
    async def test_save_trace_malformed_timestamp_uses_now_score(self) -> None:
        store, fake = await _store_with_fake_redis()
        await store.save_trace({"id": "x", "trace_hash": "0xh", "timestamp": "not-iso"})
        fake.zadd.assert_awaited_once()  # still indexed, just with current ts

    @pytest.mark.asyncio
    async def test_get_trace_direct_hash_hit(self) -> None:
        store, fake = await _store_with_fake_redis()
        fake.get = AsyncMock(side_effect=[json.dumps({"trace_hash": "0xh"})])
        result = await store.get_trace("0xh")
        assert result == {"trace_hash": "0xh"}

    @pytest.mark.asyncio
    async def test_get_trace_uuid_lookup_chain(self) -> None:
        store, fake = await _store_with_fake_redis()
        # First .get (hash key) misses; then UUID → hash; then hash → data
        fake.get = AsyncMock(side_effect=[None, "0xhash", json.dumps({"trace_hash": "0xhash"})])
        result = await store.get_trace("uuid-1")
        assert result == {"trace_hash": "0xhash"}

    @pytest.mark.asyncio
    async def test_get_trace_returns_none_when_missing(self) -> None:
        store, fake = await _store_with_fake_redis()
        fake.get = AsyncMock(return_value=None)
        assert await store.get_trace("missing") is None

    @pytest.mark.asyncio
    async def test_list_traces_filters_vault_and_decision(self) -> None:
        store, fake = await _store_with_fake_redis()
        fake.zrevrange.return_value = ["h1", "h2", "h3"]
        traces = [
            {"vault_address": "0xV", "decision_type": "rebalance"},
            {"vault_address": "0xV", "decision_type": "skip"},
            {"vault_address": "0xOTHER", "decision_type": "rebalance"},
        ]
        _seed_trace_bodies(fake, {f"{KEY_TRACE_PREFIX}h{i + 1}": json.dumps(t) for i, t in enumerate(traces)})
        window, total = await store.list_traces(vault_address="0xV", decision_type="rebalance")
        assert total == 1
        assert window[0]["decision_type"] == "rebalance"

    @pytest.mark.asyncio
    async def test_list_traces_batches_body_reads_into_one_mget(self) -> None:
        """#1577: the body reads are ONE batched round trip, not one per trace.

        The pre-#1577 loop awaited a separate ``GET`` per member of the whole
        index — on the TLS ElastiCache connection that is a full round trip
        each, and the index is read unwindowed, so the cost scaled with all of
        history rather than with the page.

        The body map is shared by ``get`` and ``mget`` (see
        ``_seed_trace_bodies``) precisely so the reverted implementation still
        returns all 50 rows: the only thing that changes when the fix is
        reverted is the round-trip count asserted here.
        """
        store, fake = await _store_with_fake_redis()
        n = 50
        fake.zrevrange.return_value = [f"h{i}" for i in range(n)]
        _seed_trace_bodies(
            fake,
            {f"{KEY_TRACE_PREFIX}h{i}": json.dumps({"id": f"t{i}", "vault_address": "0xV"}) for i in range(n)},
        )

        window, total = await store.list_traces(limit=n)

        # Every row really was read and parsed — the batching is not "cheap"
        # because it quietly dropped work.
        assert total == n
        assert [t["id"] for t in window] == [f"t{i}" for i in range(n)]
        # ONE batched call for 50 bodies, and zero single-key reads.
        assert fake.mget.await_count == 1, "the body reads were not batched"
        assert fake.get.await_count == 0, f"{fake.get.await_count} sequential GETs — one per trace is the bug"

    @pytest.mark.asyncio
    async def test_list_traces_mget_is_chunked_and_preserves_index_order(self) -> None:
        """Batches are bounded at ``TRACE_MGET_BATCH`` and walked in order.

        The chunking is why the claim is ``1 + ceil(N / TRACE_MGET_BATCH)``
        round trips rather than exactly one: this read is unbounded at the
        index, and a single index-sized MGET would block Redis' one thread for
        every other client. Ordering is load-bearing — the newest-first
        contract callers page against comes from ``zrevrange``, and it must
        survive being cut into batches.
        """
        store, fake = await _store_with_fake_redis()
        n = TRACE_MGET_BATCH * 2 + 7
        fake.zrevrange.return_value = [f"h{i}" for i in range(n)]
        _seed_trace_bodies(
            fake,
            {f"{KEY_TRACE_PREFIX}h{i}": json.dumps({"id": f"t{i}", "vault_address": "0xV"}) for i in range(n)},
        )

        window, total = await store.list_traces(limit=n)

        assert total == n
        assert [t["id"] for t in window] == [f"t{i}" for i in range(n)]  # newest-first order intact
        assert fake.mget.await_count == 3  # ceil(1007 / 500), not 1007 and not 1
        # No batch exceeds the bound.
        assert all(len(call.args[0]) <= TRACE_MGET_BATCH for call in fake.mget.await_args_list)

    @pytest.mark.asyncio
    async def test_list_traces_empty_index_issues_no_body_read(self) -> None:
        """An empty index must not send ``MGET`` with zero keys — real Redis
        answers that with ``ERR wrong number of arguments``, which would turn
        the quietest case (no traces yet) into a 500."""
        store, fake = await _store_with_fake_redis()
        fake.zrevrange.return_value = []

        window, total = await store.list_traces()

        assert (window, total) == ([], 0)
        assert fake.mget.await_count == 0

    @pytest.mark.asyncio
    async def test_list_traces_tolerates_missing_and_malformed_rows(self) -> None:
        """A hash whose blob is gone (``None`` in the MGET slot) and a blob
        that no longer parses are both skipped, exactly as the per-key loop
        skipped them — a corrupt row never breaks the listing."""
        store, fake = await _store_with_fake_redis()
        fake.zrevrange.return_value = ["h1", "h2", "h3"]
        _seed_trace_bodies(
            fake,
            {
                # h1 absent entirely → MGET returns None in its slot
                f"{KEY_TRACE_PREFIX}h2": "{not json",
                f"{KEY_TRACE_PREFIX}h3": json.dumps({"id": "t3", "vault_address": "0xV"}),
            },
        )

        window, total = await store.list_traces()

        assert [t["id"] for t in window] == ["t3"]
        assert total == 1

    @pytest.mark.asyncio
    async def test_list_recent_traces_bounds_the_range_at_the_index(self) -> None:
        """#1276: the tick's reconciliation scan must be O(limit), not O(history)
        — the zrevrange itself carries the bound, unlike list_traces."""
        store, fake = await _store_with_fake_redis()
        fake.zrevrange.return_value = ["h1", "h2"]
        fake.get = AsyncMock(side_effect=[json.dumps({"id": "t1"}), json.dumps({"id": "t2"})])

        traces = await store.list_recent_traces(limit=2)

        assert [t["id"] for t in traces] == ["t1", "t2"]
        key, start, stop = fake.zrevrange.await_args.args
        assert (key, start, stop) == (KEY_TRACE_INDEX, 0, 1)  # inclusive stop => exactly 2

    @pytest.mark.asyncio
    async def test_list_recent_traces_skips_missing_and_malformed_entries(self) -> None:
        store, fake = await _store_with_fake_redis()
        fake.zrevrange.return_value = ["h1", "h2", "h3"]
        fake.get = AsyncMock(side_effect=[None, "{not json", json.dumps({"id": "t3"})])

        traces = await store.list_recent_traces(limit=3)

        assert [t["id"] for t in traces] == ["t3"]  # a corrupt entry never breaks the scan

    @pytest.mark.asyncio
    async def test_get_last_trace_returns_first(self) -> None:
        store, fake = await _store_with_fake_redis()
        fake.zrevrange.return_value = ["h1"]
        _seed_trace_bodies(fake, {f"{KEY_TRACE_PREFIX}h1": json.dumps({"vault_address": "0xV", "id": "t1"})})
        result = await store.get_last_trace("0xV")
        assert result["id"] == "t1"

    @pytest.mark.asyncio
    async def test_get_last_trace_returns_none_when_empty(self) -> None:
        store, fake = await _store_with_fake_redis()
        fake.zrevrange.return_value = []
        assert await store.get_last_trace("0xV") is None

    @pytest.mark.asyncio
    async def test_get_trace_count(self) -> None:
        store, fake = await _store_with_fake_redis()
        fake.zcard.return_value = 42
        assert await store.get_trace_count() == 42


def _dangling_trace(trace_hash: str = "0xdangle", **overrides) -> dict:
    """The exact dangling shape: commit + trade, no reveal, no closed state."""
    base = {
        "id": "t-dangle",
        "trace_hash": trace_hash,
        "timestamp": "2026-08-20T00:00:00+00:00",
        "vault_address": "0xV",
        "commit_tx_hash": "0xcommit",
        "trade_tx_hash": "0xtrade",
        "reveal_tx_hash": None,
    }
    base.update(overrides)
    return base


class TestIsDanglingReveal:
    """The canonical predicate (#1353) — save_trace's index maintenance and
    agent_runner's scan filter both delegate to this single implementation."""

    def test_matches_the_dangling_shape(self) -> None:
        assert is_dangling_reveal(_dangling_trace()) is True

    @pytest.mark.parametrize(
        ("label", "overrides"),
        [
            ("no_trade", {"trade_tx_hash": None}),
            ("already_revealed", {"reveal_tx_hash": "0xREVEALED"}),
            ("no_commit", {"commit_tx_hash": None}),
            ("terminal", {"reveal_reconcile_state": "terminal"}),
            ("reconciled", {"reveal_reconcile_state": "reconciled"}),
            ("reconciled_from_chain", {"reveal_reconcile_state": "reconciled_from_chain"}),
        ],
    )
    def test_near_misses_are_excluded(self, label, overrides) -> None:
        assert is_dangling_reveal(_dangling_trace(**overrides)) is False, label

    def test_corrupt_entry_is_not_a_candidate(self) -> None:
        assert is_dangling_reveal("not-a-dict") is False  # type: ignore[arg-type]


class TestRevealReconcileIndex:
    """save_trace's durable dangling-index maintenance (#1353).

    save_trace is the single write choke-point every trace record passes
    through (dangling or not) — these tests prove the index actually tracks
    dangling-ness THROUGH that one call site, not via a separate update path
    that could be forgotten.
    """

    @pytest.mark.asyncio
    async def test_dangling_trace_is_added_to_pending_and_first_seen(self) -> None:
        store, fake = await _store_with_fake_redis()
        await store.save_trace(_dangling_trace())

        fake.sadd.assert_awaited_once_with(KEY_TRACE_RECONCILE_PENDING, "0xdangle")
        fake.hsetnx.assert_awaited_once()
        key, field, _value = fake.hsetnx.await_args.args
        assert (key, field) == (KEY_TRACE_RECONCILE_FIRST_SEEN, "0xdangle")
        # HSETNX, never HSET — a record retried many times must keep its
        # ORIGINAL first-seen timestamp, which is what makes it a valid age
        # bound (#1353 compound-failure guard). Redis enforces the
        # set-once-only semantics; this asserts we call the right primitive.
        fake.hset.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_longer_dangling_trace_is_removed_from_pending_and_first_seen(self) -> None:
        """GUARD DEMO: an already-revealed record must NOT sit in the pending
        index — left in by mistake, it would send reconciliation straight at
        a reveal() that reverts 'Already revealed' forever."""
        store, fake = await _store_with_fake_redis()
        await store.save_trace(_dangling_trace(reveal_tx_hash="0xREVEALED"))

        fake.srem.assert_awaited_once_with(KEY_TRACE_RECONCILE_PENDING, "0xdangle")
        fake.hdel.assert_awaited_once_with(KEY_TRACE_RECONCILE_FIRST_SEEN, "0xdangle")
        fake.sadd.assert_not_awaited()  # neither pending nor terminal

    @pytest.mark.asyncio
    async def test_terminal_trace_is_added_to_the_terminal_set(self) -> None:
        store, fake = await _store_with_fake_redis()
        await store.save_trace(_dangling_trace(reveal_reconcile_state="terminal"))

        fake.sadd.assert_awaited_once_with(KEY_TRACE_RECONCILE_TERMINAL, "0xdangle")
        fake.srem.assert_awaited_once_with(KEY_TRACE_RECONCILE_PENDING, "0xdangle")

    @pytest.mark.asyncio
    async def test_ordinary_non_reconciliation_trace_never_touches_the_terminal_set(self) -> None:
        """GUARD DEMO: a plain SKIP/error trace (no commit/trade hashes at
        all) must never land in the terminal set — it was never dangling, so
        it must not inflate the cumulative terminal count."""
        store, fake = await _store_with_fake_redis()
        await store.save_trace({"id": "skip-1", "trace_hash": "0xskip", "timestamp": "2026-08-20T00:00:00+00:00"})

        fake.sadd.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_list_dangling_reveal_traces_reads_the_index_not_a_scan(self) -> None:
        store, fake = await _store_with_fake_redis()
        fake.smembers.return_value = ["h1", "h2"]
        fake.get = AsyncMock(side_effect=[json.dumps({"id": "d1"}), json.dumps({"id": "d2"})])

        traces = await store.list_dangling_reveal_traces()

        fake.smembers.assert_awaited_once_with(KEY_TRACE_RECONCILE_PENDING)
        assert [t["id"] for t in traces] == ["d1", "d2"]

    @pytest.mark.asyncio
    async def test_list_dangling_reveal_traces_skips_missing_and_malformed(self) -> None:
        store, fake = await _store_with_fake_redis()
        fake.smembers.return_value = ["h1", "h2", "h3"]
        fake.get = AsyncMock(side_effect=[None, "{not json", json.dumps({"id": "d3"})])

        traces = await store.list_dangling_reveal_traces()

        assert [t["id"] for t in traces] == ["d3"]

    @pytest.mark.asyncio
    async def test_list_dangling_reveal_traces_prunes_orphaned_index_members(self) -> None:
        """#1403 review: a member whose blob is GONE (h1) must be removed
        from the pending set and its first-seen field cleared — otherwise the
        SCARD-backed pending gauge leaks upward forever with no path back to
        zero. A malformed-but-PRESENT blob (h2) is left alone — that's a
        content problem, not proof the index member is stale."""
        store, fake = await _store_with_fake_redis()
        fake.smembers.return_value = ["h1", "h2", "h3"]
        fake.get = AsyncMock(side_effect=[None, "{not json", json.dumps({"id": "d3"})])

        traces = await store.list_dangling_reveal_traces()

        assert [t["id"] for t in traces] == ["d3"]
        fake.srem.assert_awaited_once_with(KEY_TRACE_RECONCILE_PENDING, "h1")
        fake.hdel.assert_awaited_once_with(KEY_TRACE_RECONCILE_FIRST_SEEN, "h1")

    @pytest.mark.asyncio
    async def test_list_dangling_reveal_traces_prune_is_loud_and_marks_terminal(self, caplog) -> None:
        """Round-2 review finding: an orphan prune with no log and no terminal
        SADD is a silent, untelemetered vanish from BOTH /health gauges — in a
        PR whose stated purpose is making dangling-reveal state countable.
        The prune must (a) log at WARNING with the trace hash and
        (b) SADD the member into the terminal set, since a blob-less member
        can never be revealed again."""
        store, fake = await _store_with_fake_redis()
        fake.smembers.return_value = ["h1"]
        fake.get = AsyncMock(return_value=None)

        with caplog.at_level("WARNING"):
            traces = await store.list_dangling_reveal_traces()

        assert traces == []
        assert "Pruned orphaned reconcile-index member" in caplog.text
        assert "h1"[:16] in caplog.text
        fake.sadd.assert_awaited_once_with(KEY_TRACE_RECONCILE_TERMINAL, "h1")

    @pytest.mark.asyncio
    async def test_get_reveal_reconcile_pending_count_is_scard(self) -> None:
        store, fake = await _store_with_fake_redis()
        fake.scard.return_value = 3
        assert await store.get_reveal_reconcile_pending_count() == 3
        fake.scard.assert_awaited_once_with(KEY_TRACE_RECONCILE_PENDING)

    @pytest.mark.asyncio
    async def test_get_reveal_reconcile_terminal_count_is_scard(self) -> None:
        store, fake = await _store_with_fake_redis()
        fake.scard.return_value = 7
        assert await store.get_reveal_reconcile_terminal_count() == 7
        fake.scard.assert_awaited_once_with(KEY_TRACE_RECONCILE_TERMINAL)

    @pytest.mark.asyncio
    async def test_terminal_set_is_cumulative_no_path_ever_removes_a_member(self) -> None:
        """The terminal gauge's documented reading (#1403 review) is "CUMULATIVE
        counter — alert on rate of increase, not level", and that is only true
        if NO code path removes a member. This drives every AgentStateStore
        path that touches the three reconcile keys and asserts none of them
        ever issues a removal against KEY_TRACE_RECONCILE_TERMINAL — so the
        moment someone adds an SREM/DEL there (making the count a level that
        can fall back), the docs and /health's comment become wrong and this
        fails."""
        store, fake = await _store_with_fake_redis()

        base = {"timestamp": "2026-08-20T00:00:00+00:00"}
        # 1. a dangling save (enters the pending index)
        await store.save_trace(
            {**base, "id": "a", "trace_hash": "0xa", "commit_tx_hash": "0xc", "trade_tx_hash": "0xt"}
        )
        # 2. the same record resolved (leaves pending, not terminal)
        await store.save_trace(
            {
                **base,
                "id": "a",
                "trace_hash": "0xa",
                "commit_tx_hash": "0xc",
                "trade_tx_hash": "0xt",
                "reveal_tx_hash": "0xr",
                "reveal_reconcile_state": "reconciled",
            }
        )
        # 3. a record saved in the terminal state (enters the terminal set)
        await store.save_trace(
            {
                **base,
                "id": "b",
                "trace_hash": "0xb",
                "commit_tx_hash": "0xc",
                "trade_tx_hash": "0xt",
                "reveal_reconcile_state": "terminal",
            }
        )
        # 4. the direct, blob-independent terminal close
        await store.mark_reveal_reconcile_terminal("0xb")
        # 5. the orphan prune inside the index read
        fake.smembers.return_value = ["0xgone"]
        fake.get = AsyncMock(return_value=None)
        await store.list_dangling_reveal_traces()
        # 6. the independent first-seen seed
        await store.seed_reveal_reconcile_first_seen("0xa")

        removal_targets = [c.args[0] for c in fake.srem.await_args_list] + [
            c.args[0] for c in fake.hdel.await_args_list
        ]
        assert KEY_TRACE_RECONCILE_TERMINAL not in removal_targets
        # ...and the paths that DO write it only ever add.
        assert [c.args[0] for c in fake.sadd.await_args_list] == [KEY_TRACE_RECONCILE_PENDING] + [
            KEY_TRACE_RECONCILE_TERMINAL
        ] * 3

    @pytest.mark.asyncio
    async def test_get_reveal_reconcile_first_seen_parses_iso(self) -> None:
        store, fake = await _store_with_fake_redis()
        ts = datetime(2026, 8, 20, tzinfo=UTC)
        fake.hget.return_value = ts.isoformat()
        result = await store.get_reveal_reconcile_first_seen("0xh")
        assert result == ts
        fake.hget.assert_awaited_once_with(KEY_TRACE_RECONCILE_FIRST_SEEN, "0xh")

    @pytest.mark.asyncio
    async def test_get_reveal_reconcile_first_seen_returns_none_when_absent(self) -> None:
        store, fake = await _store_with_fake_redis()
        fake.hget.return_value = None
        assert await store.get_reveal_reconcile_first_seen("0xh") is None

    @pytest.mark.asyncio
    async def test_get_reveal_reconcile_first_seen_returns_none_on_malformed_value(self) -> None:
        store, fake = await _store_with_fake_redis()
        fake.hget.return_value = "not-a-timestamp"
        assert await store.get_reveal_reconcile_first_seen("0xh") is None

    @pytest.mark.asyncio
    async def test_get_reveal_reconcile_first_seen_empty_hash_short_circuits(self) -> None:
        """No Redis round-trip for a falsy trace_hash — nothing to look up."""
        store, fake = await _store_with_fake_redis()
        assert await store.get_reveal_reconcile_first_seen("") is None
        fake.hget.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_mark_reveal_reconcile_terminal_closes_all_three_structures(self) -> None:
        """#1403 review round 2: agent_runner._reconcile_terminal and
        _reconcile_failure both call this method with the assumption that it
        durably closes the record — independent of the trace blob's own save
        succeeding. Only a real AgentStateStore call proves the three ops
        (SADD terminal / SREM pending / HDEL first_seen) actually fire against
        the right keys; a MagicMock with these attributes hand-attached would
        pass even if the method were renamed or its body deleted."""
        store, fake = await _store_with_fake_redis()
        await store.mark_reveal_reconcile_terminal("0xh")

        fake.sadd.assert_awaited_once_with(KEY_TRACE_RECONCILE_TERMINAL, "0xh")
        fake.srem.assert_awaited_once_with(KEY_TRACE_RECONCILE_PENDING, "0xh")
        fake.hdel.assert_awaited_once_with(KEY_TRACE_RECONCILE_FIRST_SEEN, "0xh")

    @pytest.mark.asyncio
    async def test_mark_reveal_reconcile_terminal_empty_hash_short_circuits(self) -> None:
        """No Redis round-trip for a falsy trace_hash — nothing to close."""
        store, fake = await _store_with_fake_redis()
        await store.mark_reveal_reconcile_terminal("")

        fake.sadd.assert_not_awaited()
        fake.srem.assert_not_awaited()
        fake.hdel.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_list_reveal_reconcile_terminal_hashes_reads_the_terminal_set(self) -> None:
        """_reconcile_dangling_reveals cross-checks every scan/index candidate
        against this set so a record closed via mark_reveal_reconcile_terminal
        can never re-enter the retry loop even while its own JSON blob still
        reads 'pending' (#1403 review round 2). Confirms the real method reads
        SMEMBERS on the terminal key and returns a `set`, not a list — the
        caller does set membership checks (`in`) against the result."""
        store, fake = await _store_with_fake_redis()
        fake.smembers.return_value = ["h1", "h2"]

        result = await store.list_reveal_reconcile_terminal_hashes()

        fake.smembers.assert_awaited_once_with(KEY_TRACE_RECONCILE_TERMINAL)
        assert result == {"h1", "h2"}
        assert isinstance(result, set)

    @pytest.mark.asyncio
    async def test_seed_reveal_reconcile_first_seen_uses_hsetnx_not_hset(self) -> None:
        """#1403 review round 3: this method exists specifically so a
        migration-era dangling record (no marker yet, because it predates the
        index) can acquire a first-seen clock even when the blob write path
        that normally seeds it is broken. It must use HSETNX (set-once) — a
        plain HSET here would let every reconciliation pass reset the clock,
        permanently disabling the max-age circuit breaker it exists to
        support."""
        store, fake = await _store_with_fake_redis()
        await store.seed_reveal_reconcile_first_seen("0xh")

        fake.hsetnx.assert_awaited_once()
        key, field, _value = fake.hsetnx.await_args.args
        assert (key, field) == (KEY_TRACE_RECONCILE_FIRST_SEEN, "0xh")
        fake.hset.assert_not_called()

    @pytest.mark.asyncio
    async def test_seed_reveal_reconcile_first_seen_empty_hash_short_circuits(self) -> None:
        """No Redis round-trip for a falsy trace_hash — nothing to seed."""
        store, fake = await _store_with_fake_redis()
        await store.seed_reveal_reconcile_first_seen("")
        fake.hsetnx.assert_not_awaited()


class TestReconcileClosedStatesStaySynced:
    """redis_state._RECONCILE_CLOSED_STATES duplicates agent_runner's set
    (documented as intentional — importing back would be circular). This
    test is the tripwire: it fails loudly the moment the two diverge, which
    is the failure mode duplication risks."""

    def test_both_closed_state_sets_are_identical(self) -> None:
        from archimedes.chain.agent_runner import _RECONCILE_CLOSED_STATES as agent_runner_states
        from archimedes.services.redis_state import _RECONCILE_CLOSED_STATES as redis_state_states

        assert redis_state_states == agent_runner_states


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_close_clears_singleton(self) -> None:
        store, fake = await _store_with_fake_redis()
        await store.close()
        fake.aclose.assert_awaited_once()
        assert store._redis is None

    @pytest.mark.asyncio
    async def test_close_no_op_when_never_opened(self) -> None:
        store = AgentStateStore(url="redis://fake/")
        # _redis stays None; close should be a clean no-op
        await store.close()
