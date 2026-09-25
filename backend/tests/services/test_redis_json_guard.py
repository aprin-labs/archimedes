"""Hermetic tests for malformed-JSON resilience on Redis read paths (#919).

A truncated / partial / externally-tampered Redis value must degrade
gracefully (drop the entry, return null/empty) instead of crashing the read
path with a 500. No live Redis: the async client is a mock whose read methods
return deliberately-malformed bytes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from archimedes.services.job_queue import JobStore, _safe_json
from archimedes.services.redis_state import AgentStateStore, _safe_json_loads

# ── pure helpers ─────────────────────────────────────────────────────────────


def test_safe_json_loads_valid_returns_object():
    assert _safe_json_loads('{"a": 1}', context="t") == {"a": 1}


def test_safe_json_loads_malformed_returns_none():
    assert _safe_json_loads('{"a": 1', context="t") is None
    assert _safe_json_loads("not json", context="t") is None
    assert _safe_json_loads(None, context="t") is None


def test_safe_json_valid_and_default():
    assert _safe_json('{"a": 1}', {}, context="t") == {"a": 1}
    assert _safe_json("", {}, context="t") == {}  # empty → default, no error
    assert _safe_json('{"a"', {"fallback": True}, context="t") == {"fallback": True}


# ── redis_state read paths ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_regime_malformed_returns_none_not_500():
    store = AgentStateStore()
    store._redis = AsyncMock()
    store._redis.get = AsyncMock(return_value='{"regime": "bull"')  # truncated
    # Must not raise — degrades to None.
    assert await store.load_regime() is None


@pytest.mark.asyncio
async def test_get_events_drops_malformed_entry():
    store = AgentStateStore()
    store._redis = AsyncMock()
    store._redis.lrange = AsyncMock(return_value=['{"type": "ok"}', "{bad json", '{"type": "ok2"}'])
    events = await store.get_events()
    assert events == [{"type": "ok"}, {"type": "ok2"}]  # bad one dropped, no raise


@pytest.mark.asyncio
async def test_list_traces_skips_malformed_entry():
    store = AgentStateStore()
    store._redis = AsyncMock()
    store._redis.zrevrange = AsyncMock(return_value=["hash1"])
    # Both read shapes are stubbed with the same corrupt body: `list_traces`
    # batches with MGET (#1577), the single-key readers still use GET, and a
    # bare AsyncMock would answer an unstubbed `mget` with an empty-iterating
    # MagicMock — passing this test vacuously, without a malformed row ever
    # reaching the guard.
    store._redis.get = AsyncMock(return_value="{corrupted")
    store._redis.mget = AsyncMock(return_value=["{corrupted"])
    traces, total = await store.list_traces()
    assert traces == [] and total == 0  # skipped, not a 500


# ── job_queue read paths ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_job_get_malformed_payload_falls_back():
    store = JobStore()
    store._redis = AsyncMock()
    store._redis.hgetall = AsyncMock(
        return_value={"id": "j1", "type": "gen", "status": "done", "payload": "{trunc", "result": "also bad"}
    )
    job = await store.get("j1")
    assert job is not None
    assert job["payload"] == {}  # default on malformed
    assert job["result"] is None  # default on malformed
    assert job["status"] == "done"  # untouched fields still returned
