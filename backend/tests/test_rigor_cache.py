"""Tests for services.rigor_cache — the honest, data-version-keyed TTL cache that
fixes the slow Library page load (``GET /api/strategies/`` ~6s, ``GET
/api/selection-bias/gate`` ~8-10s, both from recomputing the live rigor gate on
every request).

Three properties must hold, proven below:
  (a) Two consecutive calls with UNCHANGED persisted data return IDENTICAL results
      and ``run_rigor_gate`` runs only ONCE per strategy (the second call is a
      cache hit) — the cache never re-does work it doesn't need to.
  (b) Changing a strategy's persisted returns changes the cache key, so
      ``run_rigor_gate`` runs again and the served numbers reflect the new data —
      the cache never serves a stale number for changed data.
  (c) A cache-layer error (the cache itself, not the live computation) falls back
      to live compute — the cache can only make a request slow, never wrong.

Hermetic gate:
  env -i HOME=$HOME PATH=$PATH PYTHONPATH=backend python -m pytest \\
      backend/tests/test_rigor_cache.py -q
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest
from archimedes.services import rigor_cache
from archimedes.services.rigor_evaluator import (
    compute_average_pairwise_correlation,
    compute_pbo,
    run_rigor_gate,
)

# ── Hermetic DB fixture (same pattern as test_strategies_routes.py) ────────


@pytest.fixture(autouse=True)
def _use_tmp_db(tmp_path, monkeypatch):
    """Redirect DATABASE_URL to a per-test temp SQLite file.

    The grading job and the library route both open a real session before the
    (here monkeypatched) DB read, so a real, isolated DB backs every test.
    """
    from archimedes.db import init_db

    db_path = tmp_path / "test_rigor_cache.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    init_db()
    yield


@pytest.fixture(autouse=True)
def _isolated_rigor_cache():
    """Every test gets a clean process-level cache — this module's cache is a
    module-global dict, so without this fixture, state (and the raise-on-access
    monkeypatches used in the fail-open tests) would leak across tests. Also
    resets ``_inflight`` (the single-flight bookkeeping dict, Copilot review,
    PR #1040) — ``rigor_cache.clear()`` deliberately does NOT touch it (see its
    docstring), so it's reset here instead, purely for test isolation."""
    rigor_cache.clear()
    rigor_cache._inflight.clear()
    yield
    rigor_cache.clear()
    rigor_cache._inflight.clear()


# A clean strategy snippet that passes the AST look-ahead audit (no future/peek
# access) — same fixture literal test_strategies_routes.py uses.
_CLEAN_CODE = "def init(self):\n    self.sma = 0\n"


def _series(seed: int, n: int = 300) -> list[float]:
    return np.random.default_rng(seed).normal(0.0015, 0.004, n).tolist()


class _FakeStrategy:
    """Minimal duck-typed stand-in for ``models.strategy.Strategy`` — only the
    attributes ``curated_grading.grade_cohort`` / ``_load_strategy_code_safe``
    actually touch."""

    def __init__(self, id_: str, paper_claimed_sharpe: float | None = None, strategy_code_hash: str | None = None):
        self.id = id_
        self.paper_claimed_sharpe = paper_claimed_sharpe
        self.strategy_code_path = None  # None -> _load_strategy_code_safe short-circuits, no file I/O
        self.strategy_code_hash = strategy_code_hash  # cheap code-version token folded into cohort_key


def _assert_close(actual, expected) -> None:
    """``pytest.approx`` chokes on ``None`` — both metrics can legitimately be
    ``None`` (e.g. an OOS Sharpe the gate could not compute), so compare those
    cases with plain equality and everything else with a NaN-tolerant approx."""
    if expected is None or actual is None:
        assert actual == expected
    else:
        assert actual == pytest.approx(expected, nan_ok=True)


def _expected_result(sid: str, returns_by_strategy: dict[str, list[float]]):
    """Independently reproduce what the (uncached) live gate should compute for
    ``sid`` given ``returns_by_strategy`` — the same cohort-context derivation
    ``curated_grading.grade_cohort`` performs, used to assert that moving that
    computation to the write side did not change it. num_trials is
    self-contained (1, decouple #2) — it does NOT come from this cohort; only
    PBO/avg_correlation do."""
    valid = {k: v for k, v in returns_by_strategy.items() if len(v) >= 10 and float(np.ptp(np.asarray(v))) > 0.0}
    pbo_scores = compute_pbo(valid) if len(valid) >= 2 else {}
    num_trials = 1
    avg_corr = compute_average_pairwise_correlation(valid) if len(valid) >= 2 else 0.0
    return run_rigor_gate(
        strategy_id=sid,
        daily_returns=returns_by_strategy[sid],
        num_trials=num_trials,
        pbo_scores=pbo_scores,
        strategy_code=_CLEAN_CODE,
        in_sample_sharpe=None,
        average_correlation=avg_corr,
    )


# ── The library route's cache consumer is GONE (#1746 / PR-B) ───────────────
#
# ``strategies_routes._live_rigor_results_for_strategies`` — a cohort gate run
# memoized here under a ``strategies_list:`` key — was this module's second
# consumer. The curated verdict is now graded once, when a backtest runs
# (``services.curated_grading``), and every read surface serves the stored row,
# so there is no per-request cohort computation left on that path to memoize.
# The route-level cache tests that lived here went with it; the cache's own
# contract (hit/miss, key derivation, fail-open, single-flight, cache_if,
# bounded store) is proven directly below, and ``GET /api/selection-bias/gate``
# — the deploy ladder, which deliberately still recomputes live — remains a real
# consumer.


def test_the_library_route_writes_no_rigor_cache_entry(monkeypatch):
    """GUARD: nothing on the library read path may memoize a gate run again.

    A cached verdict is the wrong shape for a stored one — it is an answer with
    a TTL, and #1746 is what a second answer costs. If someone reintroduces a
    read-time cohort compute, the fastest way to make it affordable is this
    cache, and this test is what refuses.

    MUTATION: restore ``rigor_results = get_or_compute("strategies_list:" + …)``
    in ``_list_strategies_sync``. The store gains the key and this reddens.
    """
    import asyncio

    from archimedes.main import app
    from httpx import ASGITransport, AsyncClient

    monkeypatch.setattr(
        "archimedes.services.backtest_repository.get_all_daily_returns",
        lambda session, ids: {sid: _series(i) for i, sid in enumerate(ids)},
    )

    async def _go():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.get("/api/strategies/?limit=5")

    resp = asyncio.run(_go())
    assert resp.status_code == 200
    assert not [k for k in rigor_cache._store if str(k).startswith("strategies_list:")], (
        "the library route memoized a cohort gate run; the verdict is stored, not cached"
    )


def test_grade_cohort_matches_an_independently_computed_gate(monkeypatch):
    """The cohort computation that MOVED to the write side still computes what
    it used to: same cohort filter, same self-contained ``num_trials``, same
    cohort PBO / average correlation.

    MUTATION: drop the zero-variance filter from ``valid_returns`` in
    ``grade_cohort``. The cohort context changes and the numbers stop matching
    the independently-derived expectation.
    """
    from archimedes.services import curated_grading

    returns = {"s0": _series(0), "s1": _series(1)}
    monkeypatch.setattr(
        "archimedes.services.backtest_repository.get_all_daily_returns",
        lambda session, ids: dict(returns),
    )
    monkeypatch.setattr(curated_grading, "_load_strategy_code_safe", lambda s: _CLEAN_CODE)

    graded = curated_grading.grade_cohort([_FakeStrategy("s0"), _FakeStrategy("s1")])
    assert set(graded.results) == {"s0", "s1"}
    assert graded.cohort_n == 2, "both series are non-degenerate, so both supply the cohort context"

    for sid in ("s0", "s1"):
        expected = _expected_result(sid, returns)
        _assert_close(graded.results[sid].deflated_sharpe, expected.deflated_sharpe)
        _assert_close(graded.results[sid].pbo_score, expected.pbo_score)
        _assert_close(graded.results[sid].oos_sharpe, expected.oos_sharpe)
        assert graded.results[sid].passes_all == expected.passes_all


def test_cohort_key_changes_when_a_code_version_changes_but_returns_do_not():
    """Pure unit-level proof, isolated from run_rigor_gate/DB entirely: two
    cohort_key calls with byte-identical strategy_ids + returns_by_strategy but
    a different code_versions entry for one strategy must produce different
    keys."""
    returns = {"a": _series(1), "b": _series(2)}

    k1 = rigor_cache.cohort_key(["a", "b"], returns, {"a": "hash-v1", "b": "hash-v1"})
    k2 = rigor_cache.cohort_key(["a", "b"], returns, {"a": "hash-v2", "b": "hash-v1"})
    assert k1 != k2, "changing ONE strategy's code_versions token must change the key"

    # Sanity: re-supplying the ORIGINAL code_versions reproduces the original key
    # (proves the difference above was solely due to the code_versions delta).
    k1_again = rigor_cache.cohort_key(["a", "b"], returns, {"a": "hash-v1", "b": "hash-v1"})
    assert k1 == k1_again


def test_cohort_key_omitting_code_versions_matches_empty_code_versions():
    """Backward compatibility: a caller that omits code_versions entirely (the
    pre-#1040 call shape, still used by any caller that hasn't been updated)
    must produce the same key as one that explicitly passes an all-empty-string
    code_versions map — so existing callers aren't silently broken by the new
    optional parameter."""
    returns = {"a": _series(1), "b": _series(2)}
    k_omitted = rigor_cache.cohort_key(["a", "b"], returns)
    k_explicit_empty = rigor_cache.cohort_key(["a", "b"], returns, {"a": "", "b": ""})
    k_none_values = rigor_cache.cohort_key(["a", "b"], returns, {"a": None, "b": None})
    assert k_omitted == k_explicit_empty == k_none_values


# ── (c) cache-layer error -> fail open to live compute ──────────────────────


class _BoomOnGet(dict):
    """A dict-like whose ``.get`` raises — simulates a cache-layer bug (e.g. a
    corrupted store, a serialization error) independent of the live computation."""

    def get(self, *args, **kwargs):
        raise RuntimeError("simulated cache-layer failure")


def test_get_or_compute_fails_open_on_lookup_error(monkeypatch):
    """Pure unit-level proof: if reading the cache store raises, get_or_compute
    still calls compute_fn and returns its (real, correct) result — never raises,
    never serves a stale/fabricated value."""
    monkeypatch.setattr(rigor_cache, "_store", _BoomOnGet())

    sentinel = object()
    calls = {"n": 0}

    def _compute():
        calls["n"] += 1
        return sentinel

    result = rigor_cache.get_or_compute("some-key", _compute)
    assert result is sentinel
    assert calls["n"] == 1


def test_get_or_compute_fails_open_on_store_error(monkeypatch):
    """Same fail-open guarantee when WRITING to the cache raises (e.g. the
    store rejects the value) — compute_fn's result must still be returned."""

    class _BoomOnSet(dict):
        def __setitem__(self, key, value):
            raise RuntimeError("simulated cache write failure")

    monkeypatch.setattr(rigor_cache, "_store", _BoomOnSet())

    sentinel = object()
    result = rigor_cache.get_or_compute("some-key", lambda: sentinel)
    assert result is sentinel


# ── single-flight: thundering-herd fix (Copilot review, PR #1040) ──────────
# ``get_or_compute`` previously released ``_lock`` before calling
# ``compute_fn()``, so N concurrent misses for the SAME key each ran the
# (expensive) live computation in parallel. Proven below: N threads racing on
# one key invoke ``compute_fn`` exactly once; every other thread waits for
# that call and reuses its result.


def test_concurrent_misses_for_the_same_key_invoke_compute_fn_once():
    """N concurrent get_or_compute MISSES for the SAME key must invoke
    compute_fn exactly once. The lone caller who becomes the "leader" is held
    inside compute_fn (via ``release``) until every other thread has had time
    to reach the follower's wait state — this proves the dedup holds under
    real contention, not just when the leader happens to finish instantly."""
    n_threads = 8
    call_count = {"n": 0}
    call_lock = threading.Lock()
    start_barrier = threading.Barrier(n_threads)
    release = threading.Event()

    def _slow_compute():
        with call_lock:
            call_count["n"] += 1
        assert release.wait(timeout=5.0), "test setup: release was never signaled"
        return "computed-value"

    results: list[str | None] = [None] * n_threads
    errors: list[Exception] = []

    def _worker(i):
        start_barrier.wait(timeout=5.0)
        try:
            results[i] = rigor_cache.get_or_compute("single-flight-key", _slow_compute)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()

    # Give every thread time to register as leader-or-follower (i.e. reach
    # either compute_fn's release.wait() or the follower's event.wait())
    # before letting the single compute_fn call complete.
    time.sleep(0.2)
    release.set()

    for t in threads:
        t.join(timeout=5.0)
        assert not t.is_alive(), "a worker thread never finished — likely a single-flight deadlock"

    assert not errors, f"worker thread(s) raised: {errors}"
    assert call_count["n"] == 1, (
        f"compute_fn ran {call_count['n']} times for {n_threads} concurrent same-key misses — "
        "single-flight failed to dedup the thundering herd"
    )
    assert results == ["computed-value"] * n_threads


def test_single_flight_does_not_dedup_across_different_keys():
    """Sanity companion: single-flight scopes to ONE key — concurrent misses
    for DIFFERENT keys must each still invoke their own compute_fn (dedup must
    never over-apply and merge unrelated computations)."""
    n_threads = 4
    calls: dict[str, int] = {f"key-{i}": 0 for i in range(n_threads)}
    calls_lock = threading.Lock()
    start_barrier = threading.Barrier(n_threads)

    def _make_compute(key):
        def _compute():
            with calls_lock:
                calls[key] += 1
            return f"value-for-{key}"

        return _compute

    results: dict[str, str] = {}
    results_lock = threading.Lock()

    def _worker(key):
        start_barrier.wait(timeout=5.0)
        value = rigor_cache.get_or_compute(key, _make_compute(key))
        with results_lock:
            results[key] = value

    threads = [threading.Thread(target=_worker, args=(f"key-{i}",)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert all(n == 1 for n in calls.values()), f"each distinct key must compute exactly once: {calls}"
    assert results == {f"key-{i}": f"value-for-key-{i}" for i in range(n_threads)}


def test_follower_falls_back_to_live_compute_when_leader_result_is_not_cached():
    """A follower waking up to find the leader's result was NOT written to
    `_store` (because `cache_if` rejected it) must fall back to computing
    live itself — never hang, never fabricate a value, never wait forever."""
    release = threading.Event()
    leader_started = threading.Event()

    def _leader_compute():
        leader_started.set()
        assert release.wait(timeout=5.0), "test setup: release was never signaled"
        return {}  # falsy -> rejected by cache_if=bool, nothing gets stored

    follower_result = {}

    def _follower_compute():
        return {"from": "follower-live-compute"}

    def _leader_worker():
        follower_result["leader"] = rigor_cache.get_or_compute("cache-if-key", _leader_compute, cache_if=bool)

    def _follower_worker():
        assert leader_started.wait(timeout=5.0), "leader never started"
        # Give the leader a moment to actually register in `_inflight` (it does
        # so before signaling `leader_started` via compute_fn, so this is a
        # belt-and-suspenders wait, not load-bearing on its own).
        time.sleep(0.05)
        follower_result["follower"] = rigor_cache.get_or_compute("cache-if-key", _follower_compute, cache_if=bool)

    t_leader = threading.Thread(target=_leader_worker)
    t_follower = threading.Thread(target=_follower_worker)
    t_leader.start()
    t_follower.start()

    # Let the follower reach event.wait() before releasing the leader.
    time.sleep(0.2)
    release.set()

    t_leader.join(timeout=5.0)
    t_follower.join(timeout=5.0)
    assert not t_leader.is_alive()
    assert not t_follower.is_alive()

    assert follower_result["leader"] == {}
    assert follower_result["follower"] == {"from": "follower-live-compute"}
    assert "cache-if-key" not in rigor_cache._store


# ── cache_if: a transient failure result must never get "sticky" ───────────
# (Copilot review, PR #1040): ``_compute()`` returns ``{}`` on a transient
# cohort-context compute failure, and an un-guarded get_or_compute would cache
# that ``{}`` — every strategy then falls back to stale fields for the FULL
# TTL, even though the very next request would have succeeded live. Proven
# below at both the pure get_or_compute unit level and the route level.


def test_get_or_compute_cache_if_false_does_not_cache_the_result():
    """Pure unit-level proof: when cache_if(value) is False, get_or_compute
    still returns the live value but does NOT write it to the store — the next
    call recomputes rather than replaying the un-cached (e.g. failure) value."""
    calls = {"n": 0}

    def _compute():
        calls["n"] += 1
        return {}  # falsy — the failure-sentinel shape this predicate guards against

    result = rigor_cache.get_or_compute("k-cache-if", _compute, cache_if=bool)
    assert result == {}
    assert calls["n"] == 1
    assert "k-cache-if" not in rigor_cache._store, "a falsy result must never be written to the store"

    # Second call: still a miss (nothing was cached), so compute_fn runs again.
    rigor_cache.get_or_compute("k-cache-if", _compute, cache_if=bool)
    assert calls["n"] == 2, "an un-cached falsy result must force recompute on the next call"


def test_get_or_compute_cache_if_true_caches_normally():
    """Sanity companion: a truthy result under the same predicate IS cached and
    the second call is a pure hit — cache_if doesn't disable caching outright,
    only for values that fail the predicate."""
    calls = {"n": 0}

    def _compute():
        calls["n"] += 1
        return {"strategy": "real result"}

    first = rigor_cache.get_or_compute("k-cache-if-true", _compute, cache_if=bool)
    second = rigor_cache.get_or_compute("k-cache-if-true", _compute, cache_if=bool)
    assert first == second == {"strategy": "real result"}
    assert calls["n"] == 1, "a truthy result under cache_if must still be cached (second call is a hit)"


def test_get_or_compute_cache_if_predicate_error_falls_back_to_not_caching():
    """A cache_if predicate that itself raises must never crash the request —
    consistent with this module's fail-open contract, the safe default on any
    doubt is "don't cache" (never "crash" or "cache blindly")."""

    def _boom_predicate(_value):
        raise RuntimeError("simulated predicate bug")

    sentinel = object()
    result = rigor_cache.get_or_compute("k-cache-if-boom", lambda: sentinel, cache_if=_boom_predicate)
    assert result is sentinel
    assert "k-cache-if-boom" not in rigor_cache._store


def test_a_transient_cohort_failure_degrades_to_empty_and_never_persists(monkeypatch):
    """The concrete bug this closed, restated on the write side: a cohort-context
    compute that fails must degrade to ``{}`` (the fail-closed contract — nothing
    is graded, nothing is claimed) and must not stick.

    It cannot stick any more, because nothing memoizes it: the second call runs
    live. That is the property, asserted rather than assumed — a future edit that
    reintroduces a memo around ``grade_cohort`` would make a transient failure
    strand the whole library on ``pending``, which is the same defect wearing the
    write side's clothes.

    MUTATION: wrap ``grade_cohort``'s body in ``get_or_compute(key, …)`` with no
    ``cache_if``. The second call replays the cached ``{}`` and this reddens.
    """
    from archimedes.services import curated_grading

    returns = {"s0": _series(0), "s1": _series(1)}
    monkeypatch.setattr(
        "archimedes.services.backtest_repository.get_all_daily_returns",
        lambda session, ids: dict(returns),
    )
    monkeypatch.setattr(curated_grading, "_load_strategy_code_safe", lambda s: _CLEAN_CODE)

    strategies = [_FakeStrategy("s0"), _FakeStrategy("s1")]

    from archimedes.services.rigor_evaluator import compute_pbo as _real_compute_pbo

    pbo_call_count = {"n": 0}

    def _flaky_pbo(*args, **kwargs):
        pbo_call_count["n"] += 1
        if pbo_call_count["n"] == 1:
            raise RuntimeError("simulated transient cohort-compute failure")
        return _real_compute_pbo(*args, **kwargs)

    monkeypatch.setattr("archimedes.services.rigor_evaluator.compute_pbo", _flaky_pbo)

    first = curated_grading.grade_cohort(strategies)
    assert first.results == {}, "a cohort-compute failure must degrade to no results (fail-closed)"

    second = curated_grading.grade_cohort(strategies)
    assert set(second.results) == {"s0", "s1"}, (
        "the failed run must not have been memoized — a transient failure must not "
        "strand the whole library on `pending`"
    )


# ── cohort_key unit properties ──────────────────────────────────────────────


def test_cohort_key_stable_for_identical_input():
    returns = {"a": _series(1), "b": _series(2)}
    k1 = rigor_cache.cohort_key(["a", "b"], returns)
    k2 = rigor_cache.cohort_key(["b", "a"], dict(returns))  # order-independent
    assert k1 == k2


def test_cohort_key_changes_when_any_series_changes():
    returns = {"a": _series(1), "b": _series(2)}
    k1 = rigor_cache.cohort_key(["a", "b"], returns)

    mutated = dict(returns)
    mutated["a"] = [*_series(1)[:-1], 0.999]  # tweak one strategy's series
    k2 = rigor_cache.cohort_key(["a", "b"], mutated)
    assert k1 != k2


def test_cohort_key_changes_when_a_strategy_is_added_or_removed():
    returns = {"a": _series(1)}
    k1 = rigor_cache.cohort_key(["a"], returns)
    k2 = rigor_cache.cohort_key(["a", "b"], {**returns, "b": _series(2)})
    assert k1 != k2


def test_clear_removes_all_entries():
    rigor_cache.get_or_compute("k1", lambda: 1)
    rigor_cache.get_or_compute("k2", lambda: 2)
    assert rigor_cache._store  # sanity: something is cached
    rigor_cache.clear()
    assert rigor_cache._store == {}


# ── unbounded-store backstop (Copilot review, PR #1040) ─────────────────────
# The TTL only stops STALE entries from being SERVED — nothing previously
# reclaimed the memory an expired/obsolete key occupied, so a frequently-
# changing key (a returns-rewrite path, a growing/rotating strategy set) could
# grow `_store` without bound despite the TTL. Two independent backstops, both
# proven below: (1) opportunistic pruning of expired entries on every write,
# and (2) a hard `_MAX_STORE_SIZE` cap, oldest-evicted-first, as a ceiling that
# doesn't depend on anything ever expiring.


def test_store_stays_bounded_across_many_distinct_keys():
    """The store must never exceed `_MAX_STORE_SIZE` entries even when many
    more distinct keys than that are written across the cache's lifetime —
    proving the hard cap backstop actually bounds memory, not just the TTL."""
    n_keys = rigor_cache._MAX_STORE_SIZE * 3
    for i in range(n_keys):
        rigor_cache.get_or_compute(f"bounded-key-{i}", lambda i=i: i)

    assert len(rigor_cache._store) <= rigor_cache._MAX_STORE_SIZE, (
        f"_store grew to {len(rigor_cache._store)} entries across {n_keys} distinct "
        f"keys — the hard cap of {rigor_cache._MAX_STORE_SIZE} was not enforced"
    )


def test_store_cap_evicts_oldest_entries_first():
    """The hard-cap eviction removes the OLDEST entries (by stored_at), not an
    arbitrary subset — so the most-recently-written keys survive."""
    max_size = rigor_cache._MAX_STORE_SIZE
    for i in range(max_size + 10):
        rigor_cache.get_or_compute(f"evict-order-{i}", lambda i=i: i)

    # The 10 oldest keys (0..9) must have been evicted; the most recent
    # max_size keys (10..max_size+9) must all still be present.
    for i in range(10):
        assert f"evict-order-{i}" not in rigor_cache._store, f"oldest key evict-order-{i} should have been evicted"
    for i in range(10, max_size + 10):
        assert f"evict-order-{i}" in rigor_cache._store, f"recent key evict-order-{i} should still be cached"


def test_prune_expired_locked_removes_only_entries_past_ttl(monkeypatch):
    """Unit-level proof of the opportunistic-pruning half of the backstop:
    entries older than `_TTL_SECONDS` are removed by `_prune_expired_locked`;
    fresh entries are left alone."""
    now = 10_000.0
    rigor_cache._store["fresh"] = (now - 1.0, "fresh-value")
    rigor_cache._store["stale"] = (now - rigor_cache._TTL_SECONDS - 1.0, "stale-value")

    rigor_cache._prune_expired_locked(now)

    assert "fresh" in rigor_cache._store
    assert "stale" not in rigor_cache._store


def test_pruning_reclaims_expired_entries_on_the_next_write(monkeypatch):
    """Route-adjacent proof: an entry that has aged past the TTL is reclaimed
    the next time get_or_compute WRITES (not merely skipped on read) — i.e. the
    prune actually runs as part of the normal get_or_compute write path, not
    just as an isolated helper."""
    fake_now = {"t": 0.0}
    monkeypatch.setattr(rigor_cache.time, "monotonic", lambda: fake_now["t"])

    rigor_cache.get_or_compute("old-entry", lambda: "v1")
    assert "old-entry" in rigor_cache._store

    # Advance time past the TTL, then write a DIFFERENT key — the opportunistic
    # prune on that write must reclaim "old-entry" even though nothing ever
    # looked it up again.
    fake_now["t"] = rigor_cache._TTL_SECONDS + 1.0
    rigor_cache.get_or_compute("new-entry", lambda: "v2")

    assert "old-entry" not in rigor_cache._store, "expired entry must be reclaimed on the next cache write"
    assert "new-entry" in rigor_cache._store
