"""Two-layer TTL cache for the (expensive but always-honest) live rigor gate.

**Why this exists (perf):** ``GET /api/strategies/`` and ``GET /api/selection-bias/gate``
each recompute the full rigor gate LIVE on every request — cohort PBO, average
pairwise correlation, the look-ahead code audit, and one ``run_rigor_gate`` call per
strategy. Measured cost: ~6s and ~8-10s respectively, and the Library page's
``Promise.all`` blocks on the slower one. That is unacceptable page-load latency for a
computation whose inputs (persisted daily returns) change only when a new backtest is
written — i.e. rarely, compared to how often the page is loaded.

**Why this is safe (correctness):** this module caches the REAL computed result, not a
fake or precomputed one. The cache key (``cohort_key``) is a data-version token derived
from the exact returns data the computation would read — the moment any strategy's
persisted returns change, the key changes, and the next call recomputes live. Nothing
here weakens a threshold, skips a check, or serves a stale number for changed data. It
is pure memoization of an idempotent, deterministic function of its inputs.

**Fail-open, always.** Any exception raised by the cache layer itself (lookup or store)
is caught and the call falls back to ``compute_fn()`` — a live, correct result. A cache
bug can only ever cost a slow request, never a wrong or crashed one.

**Why there are two layers (#1518).** The in-process layer alone does not amortise
across the fleet, it multiplies by it. With N ECS tasks behind the ALB, each task holds
its own copy and round-robin routing means a request can land on a task that has not
computed this cohort yet and pays the full recompute — so the ~21s
``/api/selection-bias/gate`` miss is paid N times per TTL window, and any deploy or
scale event resets every copy at once. Measured against prod (#1518): the gate went
21.9s → 0.68s → 20.9s inside about a minute, which a 600s TTL cannot explain. So a
second, SHARED layer sits underneath the process layer: one task computes, every task
reads. The shared layer is opt-in per call site via ``shared_codec`` (see
``get_or_compute``) because the cached value has to be serialisable to cross a process
boundary — a call site that passes no codec behaves exactly as it did before this
change, process-local only.

``cachetools`` is NOT currently a dependency (checked ``backend/requirements.txt`` and
``environment.yml`` on 2026-07-06) — rather than add one for a single call site, this
is a small hand-rolled dict-with-timestamps cache. If ``cachetools`` becomes a
dependency for an unrelated reason later, ``_Store`` below can be swapped for
``cachetools.TTLCache`` without changing the public API (``cohort_key`` /
``get_or_compute`` / ``clear``). The shared layer adds no dependency either: the sync
``redis`` client is already used by ``api/limiter.py`` and
``services/kb_runner.RedisLeaseClient``, and Redis is already in the stack.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from array import array
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)

# Safety cap only. The cohort_key is the real invalidation mechanism — it changes
# the instant underlying returns data changes, so in practice a cache entry is
# almost always invalidated by a key change long before this TTL would matter. The
# TTL exists purely as a backstop against any invalidation-hook gap (e.g. a writer
# path that doesn't call `clear()`) so a cached entry can never live forever.
_TTL_SECONDS = 600.0

# Hard backstop on memory growth (Copilot review, PR #1040). The TTL alone only
# stops STALE entries from being SERVED — it does nothing to reclaim the memory
# an expired/obsolete entry occupies, since nothing ever revisits a key once it
# stops being requested. A frequently-changing key (e.g. a returns-rewrite path,
# or a growing/rotating strategy set) can otherwise accumulate keys in `_store`
# forever. Two backstops, both applied opportunistically on every cache WRITE
# (never on the read/lookup path, so a cache hit stays O(1)):
#   1. Prune every entry older than `_TTL_SECONDS` — cheap, and removes the
#      overwhelming majority of garbage in the common case.
#   2. Cap `_store` at `_MAX_STORE_SIZE` entries, evicting the OLDEST (by
#      `stored_at`) first — a hard ceiling independent of the TTL, so even a
#      pathological key-churn rate can't grow `_store` without bound.
_MAX_STORE_SIZE = 256

_lock = threading.Lock()
_store: dict[str, tuple[float, Any]] = {}

# Single-flight bookkeeping (Copilot review, PR #1040 — thundering herd fix).
# Maps a key currently being computed by some caller to a threading.Event
# that fires when that computation finishes. Guarded by `_lock` exactly like
# `_store` — but never held across `compute_fn()` itself (see `get_or_compute`).
_inflight: dict[str, threading.Event] = {}


def _prune_expired_locked(now: float) -> None:
    """Remove every entry older than ``_TTL_SECONDS``. Caller must hold ``_lock``."""
    expired = [k for k, (stored_at, _value) in _store.items() if now - stored_at >= _TTL_SECONDS]
    for k in expired:
        del _store[k]


def _evict_oldest_locked(max_size: int) -> None:
    """Hard backstop: cap ``_store`` at ``max_size`` entries, evicting the
    oldest (smallest ``stored_at``) first. Caller must hold ``_lock``."""
    while len(_store) > max_size:
        oldest_key = min(_store, key=lambda k: _store[k][0])
        del _store[oldest_key]


def _fingerprint(series: list[float]) -> bytes:
    """Cheap, order-and-value-sensitive fingerprint of one strategy's return series.

    Packed IEEE-754 doubles change on ANY element changing (not just length/sum),
    so this is stronger than the ``(len, first, last, round(sum, 6))`` shorthand
    the docstring elsewhere describes as a minimum bar — this does that one better
    for about the same cost (struct.pack over a few hundred floats is microseconds,
    negligible next to the rigor-gate computation it's guarding).
    """
    if not series:
        return b"\x00empty"
    try:
        # array(...).tobytes() serializes the whole series without expanding it
        # into positional args (struct.pack(fmt, *series) would materialize one
        # arg per element — costly for large cohorts). Same order-and-value
        # sensitivity, C-contiguous double bytes.
        return array("d", series).tobytes()
    except (TypeError, ValueError, OverflowError):
        # Non-float-coercible input (shouldn't happen for persisted daily returns,
        # but never let a fingerprint failure crash the caller) — falls back to a
        # coarser summary that still changes whenever length/edges do.
        return repr((len(series), series[0], series[-1])).encode("utf-8", errors="replace")


def cohort_key(
    strategy_ids: list[str],
    returns_by_strategy: dict[str, list[float]],
    code_versions: dict[str, str | None] | None = None,
) -> str:
    """Stable data-version token for a rigor-gate cohort.

    Hashes ``sorted(strategy_ids)`` together with a fingerprint of each strategy's
    persisted returns series (and, when supplied, a code-version token per
    strategy — see below), so the key is:
      - independent of dict/list ordering (ids are sorted before hashing), and
      - guaranteed to change the moment ANY strategy's returns change, are added,
        or are removed — the property that makes caching the downstream
        computation safe without an explicit invalidation call.

    Strategies present in ``strategy_ids`` but absent from ``returns_by_strategy``
    (no persisted returns yet) still participate in the key via the empty-series
    fingerprint, so a strategy gaining its first persisted returns also changes
    the key.

    ``code_versions`` (optional) maps each strategy id to a cheap code-version
    token — the caller's ``Strategy.strategy_code_hash`` (a SHA-256 of the
    strategy file contents, already computed and held in memory — no extra I/O
    on the request path). This closes a real staleness gap (Copilot review, PR
    #1040): the cached computation this key guards doesn't just read persisted
    returns, it also runs the look-ahead code audit inside ``run_rigor_gate``
    (``strategy_code=...``), so a key built from returns alone would keep
    serving a stale look-ahead verdict / ``passes_all`` for up to the TTL after
    a strategy's code changed, even though its returns didn't. Folding the code
    hash in means a code edit changes the key exactly like a returns change
    does. Omitted or ``None`` for an id is treated as ``""`` — i.e. an id with
    no known code-version token doesn't perturb the key beyond that fixed
    placeholder, matching the prior (returns-only) behavior for callers that
    don't pass ``code_versions`` at all.
    """
    hasher = hashlib.sha256()
    code_versions = code_versions or {}
    for sid in sorted(strategy_ids):
        hasher.update(sid.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(_fingerprint(returns_by_strategy.get(sid) or []))
        hasher.update(b"\x01")
        hasher.update((code_versions.get(sid) or "").encode("utf-8"))
        hasher.update(b"\x02")
    return hasher.hexdigest()


# ───────────────────────── shared (cross-process) layer ─────────────────────────
#
# Everything below implements the second layer described in the module docstring.
# Design constraints, in the order they bind:
#
#   1. A cached verdict must NEVER outlive the data it grades. The shared layer
#      inherits BOTH of the process layer's invalidation triggers, unchanged:
#        a) the data-version token — `cohort_key` is already part of `key`, so the
#           shared entry key changes the instant any strategy's persisted returns
#           or code hash changes, exactly as the in-process key does; and
#        b) `clear()` — which now bumps a shared EPOCH counter (below), so a
#           `clear()` on ANY task makes every task's shared lookup miss on the
#           next request. Without (b), moving the cache off-process would make a
#           stale entry strictly MORE durable than it is today: today a task
#           restart drops it, in Redis it would survive both the restart and the
#           writer that invalidated it, and every task would serve it.
#   2. Fail-open, always. Every Redis touch is wrapped; any failure degrades to
#      the process layer and then to a live `compute_fn()`. A Redis outage can
#      only make the gate slow, never wrong.
#   3. Never add latency to a healthy request. Reads carry a hard socket timeout,
#      and a single failure opens a circuit breaker for `_SHARED_BACKOFF_SECONDS`
#      so a down Redis costs one timeout per backoff window, not one per request.
#
# Invalidation is an EPOCH bump rather than a key sweep on purpose: `clear()` is
# called from `backtest_repository.insert_backtest_if_missing` on the write path,
# so it has to be O(1) and atomic. `INCR` is both. A `SCAN`+`DEL` sweep would be
# O(keyspace) against a Redis shared with the agent state store, and a key-index
# SET would need its own unbounded-growth story. Superseded entries are simply
# unreachable and die on their own TTL.

_SHARED_KEY_PREFIX = "archimedes:rigor_cache:v1:"
_SHARED_EPOCH_KEY = _SHARED_KEY_PREFIX + "epoch"

# Hard ceilings on what a sick Redis can cost a request. Both are small because
# the shared layer is an OPTIMISATION: if it cannot answer in a few hundred
# milliseconds there is nothing to gain by waiting for it.
_SHARED_SOCKET_TIMEOUT = 0.5
_SHARED_CONNECT_TIMEOUT = 0.25

# Circuit breaker. After any shared-layer error, skip the shared layer entirely
# for this long. Without it, a Redis outage would add `_SHARED_CONNECT_TIMEOUT`
# to EVERY request for the duration of the outage — turning a perf feature into
# a perf regression at exactly the wrong moment.
_SHARED_BACKOFF_SECONDS = 30.0

_shared_lock = threading.Lock()
_shared_override: Any | None = None
_shared_override_set = False
_shared_default: Any | None = None
_shared_default_built = False
_shared_disabled_until: float = 0.0


@dataclass(frozen=True)
class SharedCodec:
    """How one call site's cached value crosses a process boundary.

    ``encode``/``decode`` must be exact inverses — a value that round-trips
    through them has to be indistinguishable from the value ``compute_fn()``
    returned, because a shared cache HIT serves the decoded value verbatim. If a
    field is dropped or coerced by the codec, the fleet serves a different answer
    than the computing task did, which is the "claims must be true" line.

    ``schema_token`` fingerprints the SHAPE the codec writes, and is part of the
    shared key. Two tasks running different code versions (a rolling deploy is
    exactly this, for a few minutes) must not read each other's payloads under a
    changed model: a shape change moves the token, so the old payload becomes
    unreachable rather than being mis-parsed into a plausible-looking verdict.
    """

    encode: Callable[[Any], str]
    decode: Callable[[str], Any]
    schema_token: str


@lru_cache(maxsize=8)
def model_list_codec(model_cls: type) -> SharedCodec:
    """``SharedCodec`` for a ``list`` of one pydantic model.

    JSON, not pickle, and deliberately: the shared store is a network service
    whose contents are trusted by every task in the fleet, and
    ``pickle.loads`` on data read from a network service is arbitrary code
    execution if that service is ever reachable by anything else. JSON can only
    ever produce a value the model itself validates.

    The ``schema_token`` is a hash of ``model_json_schema()`` — which includes
    nested models via ``$defs`` — so ADDING, REMOVING, or RETYPING any field
    (including one on ``RigorGateDetail`` / ``LibraryPbo``) changes the token and
    orphans every payload written under the old shape.
    """
    schema = json.dumps(model_cls.model_json_schema(), sort_keys=True, default=str)
    token = hashlib.sha256(schema.encode("utf-8")).hexdigest()[:16]

    def _encode(value: Any) -> str:
        return json.dumps([item.model_dump(mode="json") for item in value], separators=(",", ":"))

    def _decode(payload: str) -> Any:
        return [model_cls.model_validate(item) for item in json.loads(payload)]

    return SharedCodec(encode=_encode, decode=_decode, schema_token=token)


def _build_default_backend() -> Any | None:
    """The production shared backend: a sync Redis client, or ``None``.

    ``None`` (i.e. process-local only, exactly the pre-#1518 behaviour) when:

      - ``TESTING`` is set — the unit suite is hermetic by mandate (see
        CLAUDE.md § Testing conventions) and must never open a socket. Tests that
        exercise the shared layer inject a fake through ``set_shared_backend``,
        which is a boundary mock, not an internals mock.
      - ``REDIS_URL`` is not explicitly set — a bare dev machine has no Redis, and
        defaulting to ``localhost`` there would spend a connect attempt per
        request to learn that. ECS injects ``REDIS_URL`` from SSM
        (``infra/ecs.tf``) and docker compose sets it (``docker-compose.yml``),
        so both real deployments get the shared layer.
    """
    if os.getenv("TESTING"):
        return None
    url = (os.getenv("REDIS_URL") or "").strip()
    if not url:
        logger.info("rigor_cache: REDIS_URL not set — shared layer disabled, cache is process-local only")
        return None
    import redis as sync_redis

    return sync_redis.Redis.from_url(
        url,
        decode_responses=True,
        socket_timeout=_SHARED_SOCKET_TIMEOUT,
        socket_connect_timeout=_SHARED_CONNECT_TIMEOUT,
        retry_on_timeout=False,
    )


def set_shared_backend(client: Any | None) -> None:
    """Install an explicit shared backend (or ``None`` to disable the layer).

    The seam tests use to substitute a fake Redis at the client boundary. Also
    resets the circuit breaker, so an injected backend is always given a chance.
    """
    global _shared_override, _shared_override_set, _shared_disabled_until
    with _shared_lock:
        _shared_override = client
        _shared_override_set = True
        _shared_disabled_until = 0.0


def reset_shared_backend() -> None:
    """Forget any injected backend, any memoized default, and the breaker state."""
    global _shared_override, _shared_override_set, _shared_default, _shared_default_built, _shared_disabled_until
    with _shared_lock:
        _shared_override = None
        _shared_override_set = False
        _shared_default = None
        _shared_default_built = False
        _shared_disabled_until = 0.0


def _shared_client() -> Any | None:
    """The active shared backend, or ``None`` when the layer is unavailable."""
    global _shared_default, _shared_default_built
    try:
        with _shared_lock:
            if _shared_disabled_until and time.monotonic() < _shared_disabled_until:
                return None
            if _shared_override_set:
                return _shared_override
            if _shared_default_built:
                return _shared_default
        client = _build_default_backend()
        with _shared_lock:
            if not _shared_default_built:
                _shared_default = client
                _shared_default_built = True
            return _shared_default
    except Exception as exc:  # pragma: no cover - defensive; never break a request
        logger.warning("rigor_cache: shared backend unavailable (%s) — process-local cache only", exc)
        _trip_shared_breaker()
        return None


def _trip_shared_breaker() -> None:
    """Skip the shared layer for ``_SHARED_BACKOFF_SECONDS`` after any failure."""
    global _shared_disabled_until
    try:
        with _shared_lock:
            _shared_disabled_until = time.monotonic() + _SHARED_BACKOFF_SECONDS
    except Exception:  # pragma: no cover - defensive, best-effort only
        pass


def _shared_entry_key(epoch: int, codec: SharedCodec, key: str) -> str:
    return f"{_SHARED_KEY_PREFIX}e{epoch}:{codec.schema_token}:{key}"


def _shared_epoch(client: Any) -> int:
    """Current invalidation epoch. Absent (never cleared) reads as 0.

    If this key is ever evicted the epoch falls back to 0, which could make a
    surviving epoch-0 entry reachable again. That is bounded by the entries' own
    TTL — i.e. the worst case is exactly the ``_TTL_SECONDS`` staleness backstop
    this module already documents, never worse.
    """
    raw = client.get(_SHARED_EPOCH_KEY)
    if raw is None:
        return 0
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return int(raw)


def _shared_lookup(key: str, codec: SharedCodec) -> tuple[bool, Any, int | None]:
    """``(hit, value, epoch)``. ``epoch`` is captured for the eventual WRITE.

    Writing under the epoch observed at LOOKUP time (not at write time) closes a
    real staleness hole: the leader's ``compute_fn()`` can run for ~20s, and a
    ``clear()`` landing inside that window must not be undone by the leader then
    publishing a pre-``clear()`` verdict under the post-``clear()`` epoch. Writing
    under the captured epoch means such a result lands somewhere nobody will look.
    """
    client = _shared_client()
    if client is None:
        return False, None, None
    try:
        epoch = _shared_epoch(client)
        payload = client.get(_shared_entry_key(epoch, codec, key))
    except Exception as exc:
        logger.warning("rigor_cache: shared lookup failed (%s) — falling back to the process cache", exc)
        _trip_shared_breaker()
        return False, None, None
    if payload is None:
        return False, None, epoch
    try:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        return True, codec.decode(payload), epoch
    except Exception as exc:
        # A payload we cannot decode exactly is a MISS, never a partial verdict.
        # This is the rolling-deploy case the schema_token already tries to
        # prevent, plus plain corruption; either way the only honest answer is to
        # recompute live.
        logger.warning("rigor_cache: shared payload failed to decode (%s) — treating as a miss", exc)
        return False, None, epoch


def _shared_store(key: str, codec: SharedCodec, value: Any, epoch: int | None) -> None:
    """Publish ``value`` for the whole fleet. Best-effort; never raises."""
    client = _shared_client()
    if client is None:
        return
    try:
        if epoch is None:
            epoch = _shared_epoch(client)
        client.setex(_shared_entry_key(epoch, codec, key), int(_TTL_SECONDS), codec.encode(value))
    except Exception as exc:
        logger.warning("rigor_cache: shared store failed (%s) — serving the live result anyway", exc)
        _trip_shared_breaker()


def _shared_clear() -> None:
    """Bump the epoch so every task's shared lookup misses from now on."""
    client = _shared_client()
    if client is None:
        return
    try:
        client.incr(_SHARED_EPOCH_KEY)
    except Exception as exc:
        logger.warning("rigor_cache: shared invalidation failed (%s) — shared entries expire on their TTL", exc)
        _trip_shared_breaker()


def _local_put(key: str, value: Any) -> None:
    """Write to the process layer with the same bound-keeping a normal write does."""
    with _lock:
        store_time = time.monotonic()
        _prune_expired_locked(store_time)
        _store[key] = (store_time, value)
        _evict_oldest_locked(_MAX_STORE_SIZE)


def get_or_compute(
    key: str,
    compute_fn: Callable[[], Any],
    cache_if: Callable[[Any], bool] = lambda _value: True,
    shared_codec: SharedCodec | None = None,
) -> Any:
    """Return the cached value for ``key`` if present and still fresh; else compute
    it via ``compute_fn()``, cache it (subject to ``cache_if``), and return it.

    ``cache_if`` (optional, defaults to "always cache") is evaluated against the
    freshly computed value before it's written to the store. Callers whose
    ``compute_fn`` can legitimately return an empty/failure sentinel on a
    transient error (e.g. ``{}`` on a DB or cohort-compute failure) should pass
    ``cache_if=lambda v: bool(v)`` so that sentinel is never cached (Copilot
    review, PR #1040) — caching it would make a transient failure "sticky" for
    the full TTL, serving every caller a stale fallback long after the failure
    that caused it has passed. The live, correct value is still returned either
    way; ``cache_if`` only controls whether it's memoized for the NEXT caller.
    A ``cache_if`` that itself raises is treated as "don't cache" (never as
    "crash the request") — consistent with this module's fail-open contract.

    FAIL-OPEN: any exception raised while reading or writing the cache store itself
    is logged and swallowed — the function still calls ``compute_fn()`` and returns
    its (real, correct) result. This function never suppresses an exception raised
    BY ``compute_fn()`` itself — that is the caller's live computation failing, and
    must propagate exactly as it would without a cache in front of it.

    SINGLE-FLIGHT (Copilot review, PR #1040 — thundering herd): a cache MISS
    previously released ``_lock`` before calling ``compute_fn()``, so N
    concurrent misses for the SAME key each ran the (expensive) computation in
    parallel. Now, only the first concurrent caller for a given key (the
    "leader") actually calls ``compute_fn()``; every other concurrent caller
    for that SAME key (a "follower") waits on a per-key ``threading.Event``
    (registered in ``_inflight``, guarded by ``_lock`` exactly like ``_store``)
    and then reads whatever the leader stored. ``compute_fn()`` itself is
    NEVER called while holding ``_lock`` — an expensive computation must never
    block other keys' cache reads/writes, and must never run underneath a lock
    other threads are blocked on. A follower that wakes to find nothing usable
    was stored (the leader's ``compute_fn`` raised, or its result failed
    ``cache_if``) simply computes live itself — a follower must never hang or
    fabricate a value. Any error in the single-flight bookkeeping itself
    (registering/looking up/clearing an ``_inflight`` entry) falls back to
    calling ``compute_fn()`` directly, and — critically — never leaves a
    follower waiting forever: any Event this call created is always released
    before that fallback returns.

    SHARED LAYER (#1518): when ``shared_codec`` is supplied, a process-layer miss
    consults the cross-process store before computing, and a computed result is
    published there as well as locally — so one task's ~21s recompute serves the
    whole fleet instead of being repeated on every task. The process layer is
    still checked FIRST and answers without touching Redis, so a warm task's hit
    path stays O(1) and survives a Redis outage entirely. Omitting
    ``shared_codec`` leaves a call site exactly as it was: process-local, no
    serialisation, no Redis. The shared write happens on the SAME condition as
    the local one (``cache_if`` accepted the value), so a failure sentinel can no
    more get sticky across the fleet than it can within one task.
    """
    shared_epoch: int | None = None
    try:
        now = time.monotonic()
        with _lock:
            entry = _store.get(key)
        if entry is not None:
            stored_at, value = entry
            if now - stored_at < _TTL_SECONDS:
                return value
        if shared_codec is not None:
            hit, shared_value, shared_epoch = _shared_lookup(key, shared_codec)
            if hit:
                # Populate the process layer so this task's SUBSEQUENT requests
                # skip Redis entirely. Safe by construction: the entry exists in
                # the shared store only because the writing task's `cache_if`
                # accepted it, under a key that already encodes the data version.
                _local_put(key, shared_value)
                return shared_value
    except Exception as exc:  # pragma: no cover - defensive; cache lookup must never break the request
        logger.warning("rigor_cache: lookup failed, falling back to live compute: %s", exc)
        return compute_fn()

    # ── Single-flight: register as the leader for `key`, or discover we're a
    # follower behind an already-in-flight computation for the same key. ──
    event: threading.Event | None = None
    is_leader = False
    try:
        with _lock:
            existing = _inflight.get(key)
            if existing is None:
                event = threading.Event()
                _inflight[key] = event
                is_leader = True
            else:
                event = existing
    except Exception as exc:  # pragma: no cover - defensive; bookkeeping must never break the request
        logger.warning("rigor_cache: single-flight registration failed, falling back to live compute: %s", exc)
        # Best-effort: if we created (and possibly stored) an Event before the
        # failure, never strand a follower waiting on it forever.
        if event is not None:
            try:
                with _lock:
                    _inflight.pop(key, None)
            except Exception:  # pragma: no cover - defensive, best-effort only
                pass
            event.set()
        return compute_fn()

    if not is_leader:
        # A concurrent caller is already computing this exact key. Wait for
        # it to finish (releasing NO lock while waiting — we never held one),
        # then read whatever it stored. This is the dedup: N followers pay
        # for one compute_fn() call, not N.
        try:
            event.wait()
            with _lock:
                entry = _store.get(key)
            if entry is not None:
                stored_at, value = entry
                if time.monotonic() - stored_at < _TTL_SECONDS:
                    return value
        except Exception as exc:  # pragma: no cover - defensive; a follower must never hang or crash
            logger.warning("rigor_cache: single-flight wait failed, falling back to live compute: %s", exc)
        # The leader's result wasn't usable to us (it computed but `cache_if`
        # said "don't cache", it raised, or re-reading `_store` itself
        # raised) — fall back to computing live ourselves. Never suppresses
        # anything: this is OUR OWN compute_fn() call, so if it raises, that
        # exception propagates exactly as it would with no cache in front.
        return compute_fn()

    # ── Leader path: compute WITHOUT holding `_lock`. ──
    try:
        value = compute_fn()
    except Exception:
        # The LIVE computation itself failed — this is the leader's own
        # exception, not a cache-layer bug, so it must propagate exactly as it
        # would without single-flight in front of it (never suppressed). Still
        # release any followers first so none of them hang forever on a
        # leader that errored; each falls back to computing live itself.
        try:
            with _lock:
                _inflight.pop(key, None)
        except Exception as exc:  # pragma: no cover - defensive, best-effort only
            logger.warning("rigor_cache: single-flight cleanup after a failed compute also failed: %s", exc)
        finally:
            event.set()
        raise

    try:
        should_cache = cache_if(value)
    except Exception as exc:
        logger.warning("rigor_cache: cache_if predicate failed, not caching this result: %s", exc)
        should_cache = False

    if should_cache:
        try:
            # Opportunistic bound-keeping on every write (never on the read
            # path, so a cache hit stays O(1)): first reclaim anything
            # that's aged out, then enforce the hard size cap. See the
            # `_MAX_STORE_SIZE` docstring above for why both are needed.
            _local_put(key, value)
        except Exception as exc:  # pragma: no cover - defensive; cache store must never break the request
            logger.warning("rigor_cache: store failed, serving live result anyway: %s", exc)
        if shared_codec is not None:
            # Publish for the rest of the fleet. `_shared_store` swallows every
            # failure itself, so this can only ever cost the NEXT task a
            # recompute — never this request its result.
            _shared_store(key, shared_codec, value, shared_epoch)

    # Release any followers waiting on this key — they'll read the entry we
    # just (attempted to) store above, or fall back to their own live compute
    # if `should_cache` was False / the store write itself failed.
    try:
        with _lock:
            _inflight.pop(key, None)
    except Exception as exc:  # pragma: no cover - defensive, best-effort only
        logger.warning("rigor_cache: single-flight cleanup failed: %s", exc)
    finally:
        event.set()

    return value


def clear() -> None:
    """Invalidate every cached rigor-gate result.

    Call this wherever persisted daily-returns data is (re)written (e.g.
    ``backtest_repository.insert_backtest_if_missing`` on a real insert) so a
    request immediately after a new backtest is written never has to wait out the
    TTL to see it. Not strictly required for correctness — ``cohort_key`` already
    changes the moment the underlying returns change, which is what makes an
    un-cleared cache still safe — this only tightens the window between "data
    written" and "next read recomputes" from up to ``_TTL_SECONDS`` to ~0.

    **Both layers (#1518).** This clears the process store AND bumps the shared
    epoch, so every other task's shared lookup misses on its next request. That
    is not optional politeness: an entry in Redis outlives the process that wrote
    it and is visible to every task, so a ``clear()`` that reached only the local
    dict would leave the fleet serving a verdict the writer already invalidated —
    strictly worse than the pre-#1518 behaviour, where a task restart at least
    dropped it. ``test_rigor_cache_shared.py`` pins this by reverting it.

    The local clear runs first and unconditionally, so a Redis failure can never
    stop the calling task from dropping its own copy.

    What this still does NOT do, stated plainly: it cannot reach ANOTHER task's
    in-process dict. A task that already holds the entry locally keeps serving it
    until its own ``_TTL_SECONDS`` expires or the data-version key changes —
    exactly the pre-#1518 behaviour for every task, unchanged by this work. The
    data-version token in the key remains the real invalidation mechanism; this
    call is the backstop, and #1518 restores the backstop's reach to the one new
    place a cached verdict can now live.
    """
    with _lock:
        _store.clear()
    _shared_clear()
