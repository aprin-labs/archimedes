"""Tests for the conversion-funnel instrument (issue #787).

Hermetic — mocks at the Redis boundary (the project standard; no live Redis).
Covers: FunnelStore record/read + fail-safe, the ratio math in
``metrics_routes._build_funnel``, the ``record_funnel`` emit helper, the beacon
stage allowlist, and the visitor-id middleware. Also covers the issue #788
agent_type breakdown (record-side tagging, the by-agent-type reads, threading
through ``_build_funnel``, and the backward-compat default when a request's
``agent_type`` was never classified).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from archimedes.api import metrics_routes
from archimedes.api.funnel_middleware import ensure_visitor_id_middleware, record_funnel
from archimedes.api.metrics_routes import _build_funnel
from archimedes.models.telemetry import FunnelEventRequest
from archimedes.services.funnel_store import AGENT_TYPES, STAGES, FunnelStore


def _mock_redis_with_pipeline(execute_return):
    """A mock redis whose .pipeline() queues sync commands and awaits execute()."""
    pipe = MagicMock()
    pipe.pfadd = MagicMock(return_value=pipe)
    pipe.pfcount = MagicMock(return_value=pipe)
    pipe.expire = MagicMock(return_value=pipe)
    pipe.execute = AsyncMock(return_value=execute_return)
    redis = MagicMock()
    redis.pipeline = MagicMock(return_value=pipe)
    return redis, pipe


# ─── FunnelStore.record ──────────────────────────────────────────────────


async def test_record_pfadds_total_and_day_with_ttl():
    store = FunnelStore()
    redis, pipe = _mock_redis_with_pipeline([1, 1, True])
    store._get_redis = AsyncMock(return_value=redis)

    await store.record("landed", "vid-abc")

    assert pipe.pfadd.call_count == 2  # total + day
    assert pipe.expire.call_count == 1  # TTL on the day bucket only
    total_key = pipe.pfadd.call_args_list[0].args[0]
    day_key = pipe.pfadd.call_args_list[1].args[0]
    assert total_key == "archimedes:funnel:total:landed"
    assert day_key.startswith("archimedes:funnel:day:")
    assert day_key.endswith(":landed")
    pipe.execute.assert_awaited_once()


async def test_record_noops_on_unknown_stage():
    store = FunnelStore()
    redis, _ = _mock_redis_with_pipeline([])
    store._get_redis = AsyncMock(return_value=redis)

    await store.record("not-a-real-stage", "vid")

    redis.pipeline.assert_not_called()


async def test_record_noops_on_empty_visitor():
    store = FunnelStore()
    redis, _ = _mock_redis_with_pipeline([])
    store._get_redis = AsyncMock(return_value=redis)

    await store.record("landed", "")

    redis.pipeline.assert_not_called()


async def test_record_failsafe_on_redis_error():
    store = FunnelStore()
    store._get_redis = AsyncMock(side_effect=ConnectionError("redis down"))

    # Must not raise — telemetry can never break the request it measures.
    await store.record("landed", "vid")


# ─── The free-path stages (#1643) ────────────────────────────────────────


def _fake_hll_redis():
    """A redis double whose PFADD/PFCOUNT actually behave like an HLL.

    Set-backed rather than mock-backed on purpose: a record-then-read
    assertion built from two independent MagicMocks agrees with itself
    whatever the store does with the key, which is the "test that passes
    against unfixed code" trap. Here the read can only see what the write
    genuinely put under the same key.
    """
    keys: dict[str, set[str]] = {}
    queued: list = []

    pipe = MagicMock()

    def _pfadd(key, member):
        queued.append(("pfadd", key, member))
        return pipe

    def _pfcount(key):
        queued.append(("pfcount", key, None))
        return pipe

    def _expire(key, ttl):
        queued.append(("expire", key, ttl))
        return pipe

    async def _execute():
        results = []
        for op, key, arg in queued:
            if op == "pfadd":
                bucket = keys.setdefault(key, set())
                before = len(bucket)
                bucket.add(arg)
                results.append(1 if len(bucket) != before else 0)
            elif op == "pfcount":
                results.append(len(keys.get(key, ())))
            else:
                results.append(True)
        queued.clear()
        return results

    pipe.pfadd = MagicMock(side_effect=_pfadd)
    pipe.pfcount = MagicMock(side_effect=_pfcount)
    pipe.expire = MagicMock(side_effect=_expire)
    pipe.execute = AsyncMock(side_effect=_execute)

    redis = MagicMock()
    redis.pipeline = MagicMock(return_value=pipe)
    return redis, keys


async def test_new_free_path_stages_are_in_stages():
    """Both #1643 stages exist, and in the journey order the ratios assume."""
    assert "free_generation_used" in STAGES
    assert "wallet_gate_shown" in STAGES
    assert STAGES.index("generation_started") < STAGES.index("free_generation_used")
    assert STAGES.index("free_generation_used") < STAGES.index("wallet_gate_shown")
    assert STAGES.index("wallet_gate_shown") < STAGES.index("wallet_connected")


async def test_new_stages_round_trip_from_record_funnel_to_the_hll_count(monkeypatch):
    """End to end: the route helper's emit is what the funnel read reports.

    Drives ``record_funnel`` (the exact helper ``start_generation`` calls),
    not ``FunnelStore.record`` directly, so the request-state plumbing is in
    the loop too — and counts DISTINCT visitors, which is the property the
    HLL keyspace exists for.
    """
    redis, keys = _fake_hll_redis()
    store = FunnelStore()
    store._get_redis = AsyncMock(return_value=redis)

    def _request(vid):
        return SimpleNamespace(state=SimpleNamespace(visitor_id=vid, agent_type="human"))

    # record_funnel imports FunnelStore from this module at call time; hand it
    # the instance above so the emit and the read share one keyspace.
    import archimedes.services.funnel_store as fs_module

    monkeypatch.setattr(fs_module, "FunnelStore", lambda *a, **k: store)
    store.close = AsyncMock(return_value=None)

    await record_funnel(_request("vid-1"), "free_generation_used")
    await record_funnel(_request("vid-2"), "free_generation_used")
    await record_funnel(_request("vid-1"), "free_generation_used")  # repeat visitor
    await record_funnel(_request("vid-1"), "wallet_gate_shown")
    await record_funnel(_request("vid-1"), "not-a-real-stage")  # must not create a key

    counts = await store.get_totals()

    assert counts["free_generation_used"] == 2  # distinct visitors, not events
    assert counts["wallet_gate_shown"] == 1
    assert "archimedes:funnel:total:not-a-real-stage" not in keys
    # The agent_type split (#788) works for the new stages with no extra wiring.
    assert keys["archimedes:funnel:total:free_generation_used:human"] == {"vid-1", "vid-2"}


# ─── FunnelStore.record — agent_type breakdown (#788) ────────────────────


async def test_record_pfadds_agent_type_keyed_when_provided():
    store = FunnelStore()
    redis, pipe = _mock_redis_with_pipeline([1, 1, True, 1, 1, True])
    store._get_redis = AsyncMock(return_value=redis)

    await store.record("landed", "vid-abc", agent_type="human")

    assert pipe.pfadd.call_count == 4  # stage-only total+day, PLUS agent_type-keyed total+day
    assert pipe.expire.call_count == 2  # TTL on both day buckets
    keys = [c.args[0] for c in pipe.pfadd.call_args_list]
    # The pre-#788 aggregate keys are untouched...
    assert "archimedes:funnel:total:landed" in keys
    assert any(k.startswith("archimedes:funnel:day:") and k.endswith(":landed") for k in keys)
    # ...and the new agent_type-keyed ones are additive alongside them.
    assert "archimedes:funnel:total:landed:human" in keys
    assert any(k.startswith("archimedes:funnel:day:") and k.endswith(":landed:human") for k in keys)


async def test_record_agent_type_none_keeps_legacy_pfadd_count():
    """No agent_type (the pre-#788 call shape) writes exactly the original 2 keys."""
    store = FunnelStore()
    redis, pipe = _mock_redis_with_pipeline([1, 1, True])
    store._get_redis = AsyncMock(return_value=redis)

    await store.record("landed", "vid-abc", agent_type=None)

    assert pipe.pfadd.call_count == 2
    assert pipe.expire.call_count == 1


async def test_record_ignores_unrecognized_agent_type():
    """An agent_type outside AGENT_TYPES no-ops the extra tagging (defensive, mirrors unknown-stage)."""
    store = FunnelStore()
    redis, pipe = _mock_redis_with_pipeline([1, 1, True])
    store._get_redis = AsyncMock(return_value=redis)

    await store.record("landed", "vid-abc", agent_type="bogus")

    assert pipe.pfadd.call_count == 2


# ─── FunnelStore reads ───────────────────────────────────────────────────


async def test_get_totals_reads_pfcount_in_stage_order():
    store = FunnelStore()
    redis, pipe = _mock_redis_with_pipeline([10, 6, 5, 3, 2, 1])
    store._get_redis = AsyncMock(return_value=redis)

    counts = await store.get_totals()

    # Positional: the results come back in STAGES order, so this pins the
    # order itself, not just the key set (#1643 changed both).
    assert counts == {
        "landed": 10,
        "generation_started": 6,
        "free_generation_used": 5,
        "wallet_gate_shown": 3,
        "wallet_connected": 2,
        "vault_deployed": 1,
    }
    assert pipe.pfcount.call_count == len(STAGES)


async def test_get_day_uses_day_keyspace():
    store = FunnelStore()
    redis, pipe = _mock_redis_with_pipeline([3, 1, 0, 0])
    store._get_redis = AsyncMock(return_value=redis)

    counts = await store.get_day("2026-06-28")

    assert counts["landed"] == 3
    first_key = pipe.pfcount.call_args_list[0].args[0]
    assert first_key == "archimedes:funnel:day:2026-06-28:landed"


async def test_get_totals_failsafe_returns_zeros():
    store = FunnelStore()
    store._get_redis = AsyncMock(side_effect=ConnectionError("down"))

    counts = await store.get_totals()

    assert counts == dict.fromkeys(STAGES, 0)


# ─── FunnelStore reads — agent_type breakdown (#788) ─────────────────────


async def test_get_totals_by_agent_type_reads_pfcount_per_stage_and_type():
    store = FunnelStore()
    n = len(STAGES) * len(AGENT_TYPES)
    redis, pipe = _mock_redis_with_pipeline(list(range(n)))
    store._get_redis = AsyncMock(return_value=redis)

    counts = await store.get_totals_by_agent_type()

    assert set(counts.keys()) == set(STAGES)
    for stage in STAGES:
        assert set(counts[stage].keys()) == set(AGENT_TYPES)
    assert pipe.pfcount.call_count == n
    # Key shape matches the write side: total:<stage>:<agent_type>.
    first_key = pipe.pfcount.call_args_list[0].args[0]
    assert first_key == f"archimedes:funnel:total:{STAGES[0]}:{AGENT_TYPES[0]}"


async def test_get_day_by_agent_type_uses_day_keyspace():
    store = FunnelStore()
    n = len(STAGES) * len(AGENT_TYPES)
    redis, pipe = _mock_redis_with_pipeline(list(range(n)))
    store._get_redis = AsyncMock(return_value=redis)

    await store.get_day_by_agent_type("2026-06-28")

    first_key = pipe.pfcount.call_args_list[0].args[0]
    assert first_key == f"archimedes:funnel:day:2026-06-28:{STAGES[0]}:{AGENT_TYPES[0]}"


async def test_get_totals_by_agent_type_failsafe_returns_zeros():
    store = FunnelStore()
    store._get_redis = AsyncMock(side_effect=ConnectionError("down"))

    counts = await store.get_totals_by_agent_type()

    assert counts == {stage: dict.fromkeys(AGENT_TYPES, 0) for stage in STAGES}


# ─── Ratio math (_build_funnel) ──────────────────────────────────────────


def test_build_funnel_ratios():
    # Post-#1643 journey: the first three generations need only an account, so
    # a visitor GENERATES first and meets the wallet gate afterwards — the
    # reverse of the post-#851 order this test used to assert.
    counts = {
        "landed": 100,
        "generation_started": 50,
        "free_generation_used": 40,
        "wallet_gate_shown": 20,
        "wallet_connected": 5,
        "vault_deployed": 2,
    }
    resp = _build_funnel(counts, "all-time")
    by = {s.stage: s for s in resp.stages}

    assert resp.window == "all-time"
    assert by["landed"].pct_of_landed == 1.0
    assert by["landed"].step_conversion == 1.0
    assert by["generation_started"].pct_of_landed == 0.5
    assert by["generation_started"].step_conversion == 0.5  # 50 / 100
    assert by["free_generation_used"].pct_of_landed == 0.4
    assert by["free_generation_used"].step_conversion == 0.8  # 40 / 50
    assert by["wallet_gate_shown"].pct_of_landed == 0.2
    assert by["wallet_gate_shown"].step_conversion == 0.5  # 20 / 40
    assert by["wallet_connected"].pct_of_landed == 0.05
    assert by["wallet_connected"].step_conversion == 0.25  # 5 / 20
    assert by["vault_deployed"].pct_of_landed == 0.02
    assert by["vault_deployed"].step_conversion == 0.4  # 2 / 5


def test_build_funnel_zero_landed_no_divzero():
    counts = dict.fromkeys(STAGES, 0)
    resp = _build_funnel(counts, "all-time")

    for s in resp.stages:
        assert s.pct_of_landed == 0.0
    # The top of funnel is defined as 1.0 step-conversion (nothing precedes it).
    assert resp.stages[0].step_conversion == 1.0
    # A zero previous stage yields 0.0, never a ZeroDivisionError.
    assert resp.stages[1].step_conversion == 0.0


def test_build_funnel_no_breakdown_defaults_empty():
    """Omitting ``breakdown`` (e.g. the source=identity call site) yields {} per stage, not a crash."""
    counts = {"landed": 100, "wallet_connected": 25, "generation_started": 5, "vault_deployed": 2}
    resp = _build_funnel(counts, "all-time")

    assert all(s.by_agent_type == {} for s in resp.stages)


def test_build_funnel_threads_agent_type_breakdown():
    counts = {"landed": 100, "wallet_connected": 25, "generation_started": 5, "vault_deployed": 2}
    breakdown = {
        "landed": {"internal": 1, "external": 9, "human": 90},
        "wallet_connected": {"internal": 0, "external": 0, "human": 25},
        # generation_started intentionally absent — a stage missing from the
        # breakdown map must still yield {}, not a KeyError.
        "vault_deployed": {"internal": 0, "external": 0, "human": 2},
    }
    resp = _build_funnel(counts, "all-time", breakdown=breakdown)
    by = {s.stage: s for s in resp.stages}

    assert by["landed"].by_agent_type == {"internal": 1, "external": 9, "human": 90}
    assert by["landed"].distinct_visitors == 100  # aggregate field unchanged by the breakdown
    assert by["generation_started"].by_agent_type == {}


# ─── record_funnel emit helper ───────────────────────────────────────────


async def test_record_funnel_uses_request_visitor_id(monkeypatch):
    recorded = {}

    class FakeStore:
        async def record(self, stage, vid, agent_type=None):
            recorded["call"] = (stage, vid, agent_type)

        async def close(self):
            pass

    monkeypatch.setattr("archimedes.services.funnel_store.FunnelStore", FakeStore)
    req = SimpleNamespace(state=SimpleNamespace(visitor_id="vid-xyz", agent_type="external"))

    await record_funnel(req, "generation_started")

    assert recorded["call"] == ("generation_started", "vid-xyz", "external")


async def test_record_funnel_defaults_agent_type_when_unset(monkeypatch):
    """#788 backward compat: request.state.agent_type isn't always set (e.g. a request

    that never passed through ``telemetry_middleware``, or a legacy caller predating
    this attribute). ``record_funnel`` must still record — degrading to ``agent_type=None``
    (the pre-#788 aggregate-only write), not raising.
    """
    recorded = {}

    class FakeStore:
        async def record(self, stage, vid, agent_type=None):
            recorded["call"] = (stage, vid, agent_type)

        async def close(self):
            pass

    monkeypatch.setattr("archimedes.services.funnel_store.FunnelStore", FakeStore)
    req = SimpleNamespace(state=SimpleNamespace(visitor_id="vid-legacy"))  # no agent_type attribute

    await record_funnel(req, "landed")

    assert recorded["call"] == ("landed", "vid-legacy", None)


async def test_record_funnel_noop_without_visitor_id(monkeypatch):
    called = {"n": 0}

    class FakeStore:
        async def record(self, *a):
            called["n"] += 1

        async def close(self):
            pass

    monkeypatch.setattr("archimedes.services.funnel_store.FunnelStore", FakeStore)
    req = SimpleNamespace(state=SimpleNamespace())  # no visitor_id attribute

    await record_funnel(req, "landed")

    assert called["n"] == 0


# ─── Beacon stage allowlist ──────────────────────────────────────────────


async def test_funnel_event_rejects_non_landed_stage(monkeypatch):
    calls = []

    async def fake_record(request, stage):
        calls.append(stage)

    monkeypatch.setattr(metrics_routes, "record_funnel", fake_record)
    req = SimpleNamespace(state=SimpleNamespace(visitor_id="v"))

    out = await metrics_routes.record_funnel_event(FunnelEventRequest(stage="vault_deployed"), req)

    assert out == {"recorded": False}
    assert calls == []  # a client can NEVER record a server-authoritative stage


async def test_funnel_event_accepts_landed(monkeypatch):
    calls = []

    async def fake_record(request, stage):
        calls.append(stage)

    monkeypatch.setattr(metrics_routes, "record_funnel", fake_record)
    req = SimpleNamespace(state=SimpleNamespace(visitor_id="v"))

    out = await metrics_routes.record_funnel_event(FunnelEventRequest(stage="landed"), req)

    assert out == {"recorded": True}
    assert calls == ["landed"]


# ─── Visitor-id middleware ───────────────────────────────────────────────


async def test_visitor_id_middleware_sets_state_and_cookie():
    req = SimpleNamespace(cookies={}, state=SimpleNamespace())
    set_cookies = {}

    class FakeResp:
        def set_cookie(self, **kw):
            set_cookies.update(kw)

    async def call_next(r):
        # The id must be on request.state BEFORE the route runs.
        assert getattr(r.state, "visitor_id", None)
        return FakeResp()

    await ensure_visitor_id_middleware(req, call_next)

    assert set_cookies["key"] == "archimedes_vid"
    assert set_cookies["httponly"] is True
    assert len(set_cookies["value"]) >= 16


async def test_visitor_id_middleware_reuses_existing_cookie():
    req = SimpleNamespace(cookies={"archimedes_vid": "existing-vid"}, state=SimpleNamespace())
    set_cookies = {}

    class FakeResp:
        def set_cookie(self, **kw):
            set_cookies.update(kw)

    async def call_next(r):
        assert r.state.visitor_id == "existing-vid"
        return FakeResp()

    await ensure_visitor_id_middleware(req, call_next)

    assert set_cookies == {}  # no new cookie when one already exists
