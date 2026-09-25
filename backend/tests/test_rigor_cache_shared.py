"""Tests for the SHARED (cross-process) layer of ``services.rigor_cache`` — #1518.

The defect: ``rigor_cache`` was in-process only, so the selection-bias gate's
~21s recompute did not amortise across the Fargate fleet, it multiplied by it —
one recompute per task per TTL window, and every copy reset together on a deploy.
Measured on prod, three rounds seconds apart: 21.9s → 0.68s → 20.9s, which a 600s
TTL cannot produce.

What must hold, and is proven below:

  (a) SHARED, not per-process. Two genuinely independent module instances (each
      with its own ``_store``/``_lock``/``_inflight``, which is what a second ECS
      task is) pointed at one backend see the same entry, and the miss path runs
      ONCE.
  (b) A cached verdict never outlives the data it grades. BOTH pre-existing
      invalidation triggers still reach the new place a verdict can live:
      the data-version token in the key, and ``clear()``. Each has a
      revert-demo test that fails when its trigger is broken.
  (c) The four-state verdict vocabulary round-trips EXACTLY — ``pass`` / ``fail``
      / ``pending`` / ``degenerate`` — and no ``board_fdr*`` key rides the wire
      (#1580's drift guard, extended to this new serialisation surface).
  (d) Fail-open, everywhere. A missing, broken, slow, or corrupt shared backend
      degrades to the process layer and then to a live compute. It can make the
      gate slow; it can never make it wrong.

Hermetic: the Redis client is mocked at the client boundary (``_FakeRedis``), and
``rigor_cache`` refuses to build a real backend while ``TESTING`` is set, so this
file opens no socket.

  env -i HOME=$HOME PATH=$PATH PYTHONPATH=backend python -m pytest \\
      backend/tests/test_rigor_cache_shared.py -q
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import sys
from typing import Any

import pytest
from archimedes.api.selection_bias_routes import RigorGateResponse, StrategyRigorResult
from archimedes.services import rigor_cache

# ── Boundary mock: the sync redis client, not the cache's internals ──────────


class _FakeRedis:
    """In-memory stand-in for the ``redis.Redis`` surface the shared layer uses.

    Implements exactly the three commands ``rigor_cache`` issues — ``GET`` /
    ``SETEX`` / ``INCR`` — with real TTL semantics off an injectable clock, and
    records every op so a test can assert what did and did not reach the wire.
    ``fail_on`` makes a named command raise, for the fail-open tests.
    """

    def __init__(self) -> None:
        self._data: dict[str, tuple[float | None, str]] = {}
        self.ops: list[tuple] = []
        self.now = 0.0
        self.fail_on: set[str] = set()

    def _maybe_fail(self, cmd: str) -> None:
        if cmd in self.fail_on:
            raise ConnectionError(f"fake redis: {cmd} unavailable")

    def get(self, key: str) -> str | None:
        self.ops.append(("get", key))
        self._maybe_fail("get")
        entry = self._data.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at is not None and self.now >= expires_at:
            del self._data[key]
            return None
        return value

    def setex(self, key: str, ttl: int, value: str) -> None:
        self.ops.append(("setex", key, ttl))
        self._maybe_fail("setex")
        assert isinstance(value, str), "the shared layer must write a str payload, never a pickle/bytes blob"
        self._data[key] = (self.now + float(ttl), value)

    def incr(self, key: str) -> int:
        self.ops.append(("incr", key))
        self._maybe_fail("incr")
        _expires_at, current = self._data.get(key, (None, "0"))
        new = int(current) + 1
        self._data[key] = (None, str(new))
        return new

    # Test conveniences (not part of the mocked surface) ────────────────────
    def entry_keys(self) -> list[str]:
        return [k for k in self._data if not k.endswith(":epoch")]

    def ops_named(self, name: str) -> list[tuple]:
        return [op for op in self.ops if op[0] == name]


@pytest.fixture(autouse=True)
def _use_tmp_db(tmp_path, monkeypatch):
    """Redirect DATABASE_URL to a per-test temp SQLite file — same pattern as
    test_rigor_cache.py, so the route tests below touch a real but isolated DB."""
    from archimedes.db import init_db

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test_rigor_cache_shared.db'}")
    init_db()
    yield


@pytest.fixture(autouse=True)
def _isolated_inflight():
    """``clear()`` deliberately does not touch the single-flight bookkeeping (see
    its docstring), so reset it here purely for test isolation."""
    rigor_cache._inflight.clear()
    yield
    rigor_cache._inflight.clear()


@pytest.fixture
def fake_redis() -> Any:
    """A fake shared backend installed on the primary module instance."""
    fake = _FakeRedis()
    rigor_cache.set_shared_backend(fake)
    yield fake
    rigor_cache.reset_shared_backend()


_worker_seq = itertools.count()


def _worker_b():
    """A SECOND, genuinely independent instance of ``rigor_cache``.

    Executing the module file under its own name gives a distinct module object
    with a fresh ``_store`` / ``_lock`` / ``_inflight`` / breaker state — which is
    exactly what a second ECS task is. This is the faithful way to test "shared
    vs per-process" inside one interpreter: clearing the primary instance's dict
    instead would leave the two "workers" sharing every other module global, and
    would keep passing for a cache that was never shared at all.
    """
    name = f"rigor_cache_worker_b_{next(_worker_seq)}"
    spec = importlib.util.spec_from_file_location(name, rigor_cache.__file__)
    module = importlib.util.module_from_spec(spec)
    # `@dataclass` resolves its own module through `sys.modules`, so the copy has
    # to be registered under its own (unique) name. It stays a distinct module
    # object with its own globals — which is the point.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _result(
    sid: str,
    *,
    passes_all: bool = False,
    pending: bool = False,
    degenerate: bool = False,
    dsr_p_value: float | None = None,
) -> StrategyRigorResult:
    return StrategyRigorResult(
        strategy_id=sid,
        strategy_name=f"Paper {sid}",
        passes_all=passes_all,
        gate_details={"dsr": "PASS" if passes_all else "FAIL"},
        dsr_p_value=dsr_p_value,
        pending=pending,
        degenerate=degenerate,
    )


def _codec(module=rigor_cache):
    return module.model_list_codec(StrategyRigorResult)


def _dump(results) -> list[dict]:
    return [r.model_dump(mode="json") for r in results]


# ── (a) the entry is SHARED, and the miss path runs once for the fleet ───────


def test_two_independent_instances_share_one_entry_and_compute_once(fake_redis):
    """#1518 acceptance, literally: two independent cache clients see the same
    entry, and the miss path runs once."""
    worker_b = _worker_b()
    worker_b.set_shared_backend(fake_redis)

    calls: list[int] = []

    def _compute():
        calls.append(1)
        return [_result("s1", passes_all=True), _result("s2")]

    a = rigor_cache.get_or_compute("gate:k", _compute, cache_if=bool, shared_codec=_codec())
    assert len(calls) == 1, "worker A must pay the miss"

    assert worker_b._store == {}, "worker B starts cold — no in-process entry to hit"
    b = worker_b.get_or_compute("gate:k", _compute, cache_if=bool, shared_codec=_codec(worker_b))

    assert len(calls) == 1, (
        "worker B recomputed — the cache is still per-process. One task's compute must serve the whole fleet."
    )
    assert _dump(b) == _dump(a)
    assert all(isinstance(r, StrategyRigorResult) for r in b), "a shared hit must decode to models, not dicts"


def test_a_shared_hit_populates_the_process_layer_so_the_next_hit_skips_redis(fake_redis):
    """The process layer is still layer one: after one shared hit, this task's
    subsequent requests must not touch the backend at all (a Redis outage cannot
    slow down an already-warm task)."""
    worker_b = _worker_b()
    worker_b.set_shared_backend(fake_redis)

    rigor_cache.get_or_compute("gate:k", lambda: [_result("s1")], cache_if=bool, shared_codec=_codec())
    worker_b.get_or_compute("gate:k", lambda: [_result("s1")], cache_if=bool, shared_codec=_codec(worker_b))

    before = len(fake_redis.ops)
    worker_b.get_or_compute("gate:k", lambda: [_result("s1")], cache_if=bool, shared_codec=_codec(worker_b))
    assert len(fake_redis.ops) == before, "a warm process-layer hit must issue no Redis commands"


def test_no_codec_means_no_shared_traffic_at_all(fake_redis):
    """A call site that passes no ``shared_codec`` behaves exactly as it did
    before #1518 — process-local, no serialisation, nothing on the wire."""
    worker_b = _worker_b()
    worker_b.set_shared_backend(fake_redis)

    calls: list[int] = []

    def _compute():
        calls.append(1)
        return [_result("s1")]

    rigor_cache.get_or_compute("gate:k", _compute, cache_if=bool)
    worker_b.get_or_compute("gate:k", _compute, cache_if=bool)

    assert calls == [1, 1], "without a codec each process must compute for itself, as before"
    assert fake_redis.ops == [], "no codec must mean no Redis traffic"


# ── (b) invalidation trigger 1: the data-version token in the key ────────────


def test_changed_returns_are_never_served_the_old_shared_verdict(fake_redis):
    """REVERT DEMO (staleness). Mutate the underlying returns; a cold worker must
    grade the NEW data, never be handed the entry the old data produced.

    The guard is that the shared entry key carries ``cohort_key`` — the
    data-version token — exactly as the in-process key always has. Revert that
    (key the shared entry on the route/schema alone, the naive "the gate result
    is a small keyed blob" implementation) and worker B is served worker A's
    verdict for data worker A never saw.
    """
    worker_b = _worker_b()
    worker_b.set_shared_backend(fake_redis)

    ids = ["s1"]
    returns_v1 = {"s1": [0.01] * 5 + [-0.004] * 5}
    returns_v2 = {"s1": [-0.01] * 5 + [0.004] * 5}  # same length, opposite sign: a real data change

    def _verdict_for(returns: dict[str, list[float]]):
        # Stand-in for the live gate: a deterministic function OF THE DATA, so
        # "served the old verdict" is observable as a wrong answer, not just as
        # a call count.
        mean = sum(returns["s1"]) / len(returns["s1"])
        return [_result("s1", passes_all=mean > 0, dsr_p_value=round(mean, 6))]

    key_v1 = "selection_bias_gate:strictness=1:" + rigor_cache.cohort_key(ids, returns_v1)
    key_v2 = "selection_bias_gate:strictness=1:" + rigor_cache.cohort_key(ids, returns_v2)
    assert key_v1 != key_v2, "sanity: cohort_key must react to the returns change this test makes"

    a = rigor_cache.get_or_compute(key_v1, lambda: _verdict_for(returns_v1), cache_if=bool, shared_codec=_codec())
    assert a[0].passes_all is True

    b = worker_b.get_or_compute(key_v2, lambda: _verdict_for(returns_v2), cache_if=bool, shared_codec=_codec(worker_b))
    assert b[0].passes_all is False, (
        "the shared cache served the verdict computed from the OLD returns — a cached verdict "
        "outlived the data it grades"
    )
    assert b[0].dsr_p_value == pytest.approx(-0.003)


# ── (b) invalidation trigger 2: clear() ──────────────────────────────────────


def test_clear_invalidates_the_shared_entry_for_every_worker(fake_redis):
    """REVERT DEMO (staleness). ``clear()`` must reach the shared copy too.

    ``backtest_repository.insert_backtest_if_missing`` calls ``clear()`` on the
    write path. It exists as the BACKSTOP for a data change the key cannot see,
    so this test deliberately holds the key fixed and changes what the compute
    returns — that is the only case ``clear()`` is responsible for.

    Before #1518 a stale entry died with the process; in Redis it survives the
    restart AND the writer that invalidated it, and every task serves it. Revert
    ``clear()`` to the process store only and worker B is served the pre-clear
    verdict.
    """
    worker_b = _worker_b()
    worker_b.set_shared_backend(fake_redis)

    verdict = {"passes": True}

    def _compute():
        return [_result("s1", passes_all=verdict["passes"])]

    key = "selection_bias_gate:strictness=1:fixed-data-version-token"
    a = rigor_cache.get_or_compute(key, _compute, cache_if=bool, shared_codec=_codec())
    assert a[0].passes_all is True
    assert fake_redis.entry_keys(), "sanity: worker A must actually have published a shared entry"

    # The underlying returns were rewritten and the writer invalidated the cache.
    verdict["passes"] = False
    rigor_cache.clear()

    b = worker_b.get_or_compute(key, _compute, cache_if=bool, shared_codec=_codec(worker_b))
    assert b[0].passes_all is False, (
        "clear() did not reach the shared layer — the fleet is serving a verdict the writer already invalidated"
    )


def test_clear_bumps_the_shared_epoch_exactly_once(fake_redis):
    rigor_cache.clear()
    assert fake_redis.ops_named("incr") == [("incr", rigor_cache._SHARED_EPOCH_KEY)]


def test_clear_still_drops_the_local_copy_when_the_shared_bump_fails(fake_redis):
    """A Redis failure must never stop the calling task from dropping its own
    entry — the local clear runs first and unconditionally."""
    rigor_cache.get_or_compute("gate:k", lambda: [_result("s1")], cache_if=bool, shared_codec=_codec())
    assert rigor_cache._store, "sanity: something is cached locally"

    fake_redis.fail_on.add("incr")
    rigor_cache.clear()
    assert rigor_cache._store == {}


def test_a_result_computed_before_a_clear_is_never_published_after_it(fake_redis):
    """The leader's ``compute_fn`` can run for ~20s. A ``clear()`` landing inside
    that window must not be undone by the leader then publishing its pre-clear
    result under the post-clear epoch. The write goes under the epoch observed at
    LOOKUP time, so such a result lands somewhere nobody will look."""
    worker_b = _worker_b()
    worker_b.set_shared_backend(fake_redis)

    key = "selection_bias_gate:strictness=1:fixed-data-version-token"

    def _slow_compute():
        rigor_cache.clear()  # the writer invalidates while we are still computing
        return [_result("s1", passes_all=True)]

    rigor_cache.get_or_compute(key, _slow_compute, cache_if=bool, shared_codec=_codec())

    calls: list[int] = []

    def _fresh():
        calls.append(1)
        return [_result("s1", passes_all=False)]

    b = worker_b.get_or_compute(key, _fresh, cache_if=bool, shared_codec=_codec(worker_b))
    assert calls == [1], "worker B must recompute — the pre-clear result must be unreachable"
    assert b[0].passes_all is False


# ── (c) the four-state verdict vocabulary round-trips exactly ────────────────


_FOUR_STATES = [
    ("pass", {"passes_all": True, "pending": False, "degenerate": False}),
    ("fail", {"passes_all": False, "pending": False, "degenerate": False}),
    ("pending", {"passes_all": False, "pending": True, "degenerate": False}),
    ("degenerate", {"passes_all": False, "pending": False, "degenerate": True}),
]


def test_the_four_state_verdict_vocabulary_round_trips_exactly(fake_redis):
    """``pass`` / ``fail`` / ``pending`` / ``degenerate`` are four DISTINCT states
    (CLAUDE.md § fail-soft): collapsing "never evaluated" or "nothing was
    measurable" into "evaluated and lost" is the exact defect #1358 fixed. A
    shared cache hit serves the decoded value verbatim, so the codec has to
    preserve all four — and every other field with them."""
    worker_b = _worker_b()
    worker_b.set_shared_backend(fake_redis)

    computed = [_result(name, **flags) for name, flags in _FOUR_STATES]
    a = rigor_cache.get_or_compute("gate:states", lambda: computed, cache_if=bool, shared_codec=_codec())
    b = worker_b.get_or_compute("gate:states", list, cache_if=bool, shared_codec=_codec(worker_b))

    assert _dump(b) == _dump(a), "the shared round-trip changed the served payload"
    for row, (name, flags) in zip(b, _FOUR_STATES, strict=True):
        assert row.strategy_id == name
        for field, expected in flags.items():
            assert getattr(row, field) is expected, f"{name}: {field} did not survive the shared round-trip"


def test_the_four_states_stay_distinguishable_after_the_round_trip(fake_redis):
    """Anti-vacuity for the test above: the four states must remain four, not
    collapse into two. A codec that dropped ``pending``/``degenerate`` would
    still pass a field-by-field check against a payload that had already lost
    them, so assert the states are pairwise distinct on the DECODED side."""
    worker_b = _worker_b()
    worker_b.set_shared_backend(fake_redis)

    computed = [_result(name, **flags) for name, flags in _FOUR_STATES]
    rigor_cache.get_or_compute("gate:states", lambda: computed, cache_if=bool, shared_codec=_codec())
    b = worker_b.get_or_compute("gate:states", list, cache_if=bool, shared_codec=_codec(worker_b))

    signatures = {(r.passes_all, r.pending, r.degenerate) for r in b}
    assert len(signatures) == 4, f"the four verdict states collapsed to {len(signatures)} after the round trip"


# ── (c) #1580's drift guard, extended to the shared-payload surface ──────────


def _board_fdr_json_paths(node, path: str = "$") -> list[str]:
    """Every JSON path whose key mentions board-level FDR. Same shape of scan as
    ``_relational_json_keys`` in test_selection_bias_routes.py, applied to the
    payload the shared cache writes."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}"
            if "board_fdr" in key or "board_level_fdr" in key:
                found.append(here)
            found.extend(_board_fdr_json_paths(value, here))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            found.extend(_board_fdr_json_paths(value, f"{path}[{i}]"))
    return found


def test_the_board_fdr_scan_flags_a_deliberate_reappearance():
    """Adversarial pass: build the payload that SHOULD fail the scan and show it
    does — including the NESTED per-strategy position a shallow scan would miss."""
    bad = [{"strategy_id": "a", "board_fdr_significant": False, "library_pbo": {"board_level_fdr": 1}}]
    flagged = _board_fdr_json_paths(bad)
    assert "$[0].board_fdr_significant" in flagged
    assert "$[0].library_pbo.board_level_fdr" in flagged


def test_the_shared_payload_carries_no_board_fdr_key(fake_redis):
    """#1580 moved board-level FDR onto the leaderboard and off the per-strategy
    gate. The shared cache is a NEW place that shape could drift back in, so the
    same guard applies to what actually goes on the wire."""
    codec = _codec()
    payload = codec.encode([_result(name, **flags) for name, flags in _FOUR_STATES])
    assert _board_fdr_json_paths(json.loads(payload)) == []
    assert "board_fdr" not in payload and "board_level_fdr" not in payload


def test_the_gate_response_model_still_carries_no_board_fdr_field():
    """Belt to the above: the model the payload is built from is still clean, so
    a reappearance would have to be introduced on purpose in two places."""
    for model in (StrategyRigorResult, RigorGateResponse):
        assert [f for f in model.model_fields if "board_fdr" in f or "board_level_fdr" in f] == []


# ── (c) cross-version safety: the schema token orphans a changed shape ───────


def test_a_shape_change_orphans_payloads_written_under_the_old_shape(fake_redis):
    """A rolling deploy runs two code versions against one Redis for a few
    minutes. A task must never mis-parse a neighbour's payload into a
    plausible-looking verdict, so the shared key carries a hash of the model's
    JSON schema: change the shape, and old payloads become unreachable rather
    than readable."""
    from pydantic import BaseModel

    class _V1(BaseModel):
        strategy_id: str = ""
        passes_all: bool = False

    class _V2(BaseModel):
        strategy_id: str = ""
        passes_all: bool = False
        board_fdr_significant: bool | None = None  # the exact drift #1580 forbids

    v1, v2 = rigor_cache.model_list_codec(_V1), rigor_cache.model_list_codec(_V2)
    assert v1.schema_token != v2.schema_token, "the schema token does not react to a field being added"

    rigor_cache.get_or_compute("gate:k", lambda: [_V1(strategy_id="s1")], cache_if=bool, shared_codec=v1)

    calls: list[int] = []

    def _compute_v2():
        calls.append(1)
        return [_V2(strategy_id="s1")]

    rigor_cache._store.clear()  # a task that has not computed this cohort yet
    rigor_cache.get_or_compute("gate:k", _compute_v2, cache_if=bool, shared_codec=v2)
    assert calls == [1], "a reader on a different shape must MISS, never decode a neighbour's payload"


def test_the_schema_token_is_stable_across_instances():
    """The token has to be the same number in every task, or the fleet shards
    itself and nothing is shared."""
    worker_b = _worker_b()
    assert _codec().schema_token == _codec(worker_b).schema_token


# ── (d) fail-open: a sick shared layer costs latency, never correctness ──────


@pytest.mark.parametrize("failing_command", ["get", "setex"])
def test_a_broken_shared_backend_still_serves_a_live_result(fake_redis, failing_command):
    fake_redis.fail_on.add(failing_command)
    out = rigor_cache.get_or_compute(
        "gate:k", lambda: [_result("s1", passes_all=True)], cache_if=bool, shared_codec=_codec()
    )
    assert out[0].passes_all is True


def test_a_corrupt_shared_payload_is_a_miss_not_a_partial_verdict(fake_redis):
    """Anything we cannot decode EXACTLY must recompute. Half a verdict is worse
    than a slow one."""
    rigor_cache.get_or_compute("gate:k", lambda: [_result("s1")], cache_if=bool, shared_codec=_codec())
    (entry_key,) = fake_redis.entry_keys()
    fake_redis._data[entry_key] = (None, "{not json")

    calls: list[int] = []

    def _compute():
        calls.append(1)
        return [_result("s1", passes_all=True)]

    rigor_cache._store.clear()
    out = rigor_cache.get_or_compute("gate:k", _compute, cache_if=bool, shared_codec=_codec())
    assert calls == [1]
    assert out[0].passes_all is True


def test_one_shared_failure_opens_the_circuit_for_the_backoff_window(fake_redis):
    """A down Redis must cost one timeout per backoff window, not one per
    request — otherwise the perf feature becomes a perf regression at exactly
    the wrong moment."""
    fake_redis.fail_on.add("get")
    rigor_cache.get_or_compute("gate:k", lambda: [_result("s1")], cache_if=bool, shared_codec=_codec())
    ops_after_first = len(fake_redis.ops)

    rigor_cache._store.clear()
    rigor_cache.get_or_compute("gate:k2", lambda: [_result("s1")], cache_if=bool, shared_codec=_codec())
    assert len(fake_redis.ops) == ops_after_first, "the breaker did not open — every request pays the failure"


def test_a_failure_sentinel_is_never_published_to_the_fleet(fake_redis):
    """``cache_if`` guards both layers. A transient cohort-compute failure that
    got sticky in Redis would strand the WHOLE fleet, not just one task."""
    rigor_cache.get_or_compute("gate:k", list, cache_if=bool, shared_codec=_codec())
    assert fake_redis.entry_keys() == [], "an empty/failure result must never be published"


def test_the_shared_entry_carries_the_module_ttl(fake_redis):
    rigor_cache.get_or_compute("gate:k", lambda: [_result("s1")], cache_if=bool, shared_codec=_codec())
    ((_cmd, _key, ttl),) = fake_redis.ops_named("setex")
    assert ttl == int(rigor_cache._TTL_SECONDS)


def test_an_expired_shared_entry_is_not_served(fake_redis):
    """The TTL is the backstop against any invalidation-hook gap, and it has to
    hold in the shared layer too."""
    worker_b = _worker_b()
    worker_b.set_shared_backend(fake_redis)

    rigor_cache.get_or_compute("gate:k", lambda: [_result("s1", passes_all=True)], cache_if=bool, shared_codec=_codec())
    fake_redis.now += rigor_cache._TTL_SECONDS + 1

    calls: list[int] = []

    def _compute():
        calls.append(1)
        return [_result("s1")]

    worker_b.get_or_compute("gate:k", _compute, cache_if=bool, shared_codec=_codec(worker_b))
    assert calls == [1], "an expired shared entry must not be served"


# ── the default backend: hermetic, and built from the deployment's own config ─


def test_no_backend_is_built_while_testing_is_set(monkeypatch):
    """The unit suite is hermetic by mandate — this file's tests inject a fake,
    and nothing else in the suite may open a socket."""
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("REDIS_URL", "redis://should-never-be-dialled:6379/0")
    assert rigor_cache._build_default_backend() is None


def test_no_backend_without_an_explicit_redis_url(monkeypatch):
    """A bare dev box has no Redis; it must get exactly the pre-#1518 behaviour
    rather than spend a connect attempt per request learning that."""
    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert rigor_cache._build_default_backend() is None


def test_the_default_backend_is_built_from_redis_url_with_bounded_timeouts(monkeypatch):
    """Prod (``infra/ecs.tf``) and docker compose both inject ``REDIS_URL``. The
    client must carry hard socket timeouts, or a hung Redis becomes the page's
    latency. Patched at the redis boundary — no socket is opened."""
    import redis as sync_redis

    captured: dict = {}

    def _from_url(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return "sentinel-client"

    monkeypatch.setattr(sync_redis.Redis, "from_url", staticmethod(_from_url))
    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.setenv("REDIS_URL", "rediss://elasticache.example:6379/0")

    assert rigor_cache._build_default_backend() == "sentinel-client"
    assert captured["url"] == "rediss://elasticache.example:6379/0"
    assert captured["kwargs"]["socket_timeout"] == rigor_cache._SHARED_SOCKET_TIMEOUT
    assert captured["kwargs"]["socket_connect_timeout"] == rigor_cache._SHARED_CONNECT_TIMEOUT
    assert captured["kwargs"]["decode_responses"] is True


# ── the call site is actually wired ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_selection_bias_gate_passes_a_shared_codec(monkeypatch):
    """A perfect shared layer nobody calls fixes nothing. Assert the route that
    #1518 measured hands ``get_or_compute`` the codec for the model it caches."""
    from archimedes.main import app
    from httpx import ASGITransport, AsyncClient

    seen: dict = {}
    real = rigor_cache.get_or_compute

    def _spy(key, compute_fn, cache_if=lambda _v: True, shared_codec=None):
        seen["shared_codec"] = shared_codec
        return real(key, compute_fn, cache_if=cache_if, shared_codec=shared_codec)

    monkeypatch.setattr(rigor_cache, "get_or_compute", _spy)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/selection-bias/gate")
    assert resp.status_code == 200

    codec = seen.get("shared_codec")
    assert codec is not None, "/api/selection-bias/gate is not using the shared cache layer"
    assert codec.schema_token == _codec().schema_token


@pytest.mark.asyncio
async def test_a_cold_task_serves_the_gate_from_the_shared_cache(fake_redis, monkeypatch):
    """End to end, on the real route and the real gate: a task that has never
    computed this cohort must answer from the shared entry instead of paying the
    recompute — which is the whole of #1518."""
    import numpy as np
    from archimedes.api import selection_bias_routes as routes
    from archimedes.main import app
    from archimedes.services.rigor_evaluator import run_rigor_gate as real_run_rigor_gate
    from httpx import ASGITransport, AsyncClient

    strategies = routes._provider().list_strategies()
    assert strategies, "the curated corpus must be non-empty for this to bite"

    # Force a usable persisted series for every strategy so the route takes the
    # EXPENSIVE branch (the one that calls run_rigor_gate) rather than reporting
    # every row `pending` — otherwise the call-count assertion below is vacuous.
    returns = {
        s.id: np.random.default_rng(abs(hash(s.id)) % 10_000).normal(0.001, 0.01, 300).tolist() for s in strategies
    }
    monkeypatch.setattr(
        "archimedes.services.backtest_repository.get_all_daily_returns",
        lambda session, ids: dict(returns),
    )

    calls: list[int] = []

    def _spy(*args, **kwargs):
        calls.append(1)
        return real_run_rigor_gate(*args, **kwargs)

    monkeypatch.setattr(routes, "run_rigor_gate", _spy)

    async def _get():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.get("/api/selection-bias/gate")

    first = await _get()
    assert first.status_code == 200
    assert calls, "the first request must run the live gate"
    warm = len(calls)

    # Simulate the request landing on a DIFFERENT task: same shared Redis, empty
    # in-process store. (`clear()` is deliberately not used here — it would also
    # invalidate the shared entry, which is a different scenario, covered above.)
    rigor_cache._store.clear()

    second = await _get()
    assert second.status_code == 200
    assert len(calls) == warm, "a cold task recomputed the whole cohort — the shared cache is not being read"
    assert second.json()["strategies"] == first.json()["strategies"]
