"""/health must answer even when the chain and the oracle are dark (#1592).

INCIDENT 2026-08-31. ``/health`` awaited ``chain_client.is_connected()`` and the
on-chain oracle probe with no deadline of its own. With the Arc RPC unreachable
from inside the VPC both parked; the handler blew the ALB's 5s check and ECS's
container HEALTHCHECK; no new task ever turned healthy; the rollout wedged at
1/2 for its full 1200s budget while the serving task's event loop starved every
other route. The same RPC answered in 0.1s from OUTSIDE the VPC — nothing was
slow, the calls were simply unbounded.

**These are guards, not coverage.** Each one was demonstrated to REJECT: run
this file with the bounded-probe block in ``main.health`` replaced by the old
``connected = await chain_client.is_connected()`` / ``await
_oracle_health_probe()`` pair and every timing test below fails on the deadline
instead of passing (evidence in the PR body). A guard that has never been shown
to fail is a guess.

Two properties are under test and they are deliberately separate:

1. **The ENDPOINT always answers**, inside ~2s, no matter how dark the outbound
   dependencies are.
2. **The PAYLOAD never lies about it.** A value served from cache says so, with
   its age and the reason the fresh probe missed; a probe with nothing cached
   reports absence rather than a plausible substitute. The staleness fields are
   present EXACTLY when a probe timed out, so their presence is itself the
   signal.

RESIDUAL, 2026-08-31 (#1594). #1592 read correct and MEASURED WRONG: it bounded
the two outbound probes and the handler still went to p95 17.03s / max 30s
against an ALB check of ``timeout 10 x threshold 5``, with ``HealthyHostCount``
averaging 1.03 over 24h and touching 0. The reason is the second half of the
same lesson — the six LOCAL reads below the outbound block (``load_corpus``,
``get_paper_count``, ``get_corpus_meta``, ``paper_rag_health``,
``gmm_regime_health``, ``risk_data_health``) ran synchronously and unbounded,
and **a budget denominated in loop time is not a budget when the loop is what
stalled**: ``asyncio.wait_for``'s timeout is itself a loop callback, so it can
bound an await but never a blocking call. Those six now run in worker threads,
concurrently, under ``HealthProbeCache``. ``TestTheSixLocalReadsAreBoundedToo``
injects a 30s stall into each in turn — the fault-injection point is the module
boundary each read is imported from, never an internal.

Hermetic: no network, no DB, no Redis, no .env. Every outbound probe is patched.
Run: env -i HOME=$HOME PATH=$PATH PYTHONPATH=backend python -m pytest \
       backend/tests/test_health_always_answers.py -q
"""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from archimedes.chain.client import BoundedAsyncHTTPProvider, chain_client
from archimedes.deadline import run_with_deadline
from archimedes.services import oracle_health as oracle_health_mod
from archimedes.services.health_cache import HealthProbeCache, health_probe_cache
from archimedes.services.oracle_health import OracleHealth
from httpx import ASGITransport, AsyncClient
from web3.providers import AsyncHTTPProvider

# The budget /health promises. The ALB target-group check and the ECS container
# HEALTHCHECK both cut at 5s; 2s is the app-side promise with headroom, and it
# is what the incident violated.
_HEALTH_BUDGET_SECONDS = 2.0

# A hard stop so a regression FAILS the suite instead of hanging it. Without
# this, reverting the fix would park these tests forever on a 3600s sleep and
# the "does this guard reject?" demonstration would be unrunnable.
_HARD_STOP_SECONDS = 8.0


async def _load_scaled_budget() -> float:
    """The 2s promise, scaled for ambient box load.

    An absolute wall-clock bar measures the machine, not the code: on a heavily
    loaded test box (dozens of concurrent suites, 2026-08-31) a HEALTHY handler
    measured 2.16-2.51s and these guards went red with the code correct. So each
    test first measures a healthy /health in the same process (probes patched
    fast, so nothing leaves the process) and bounds the dark-dependency call at
    3x that baseline - contention inflates both sides and cancels - never below
    the original 2.0s promise, which stays the bar whenever the box is quiet.
    The absolute anti-hang property remains _HARD_STOP_SECONDS's job.
    """
    with (
        patch.object(chain_client, "is_connected", _returns_connected),
        patch.object(oracle_health_mod, "oracle_health", _fast_oracle),
    ):
        _, healthy_elapsed = await _get_health()
    # The healthy baseline call warms the probe cache; without this clear, the
    # dark-dependency call under test would honestly serve `stale_cached` and
    # the probe_timeout assertions would be measuring the cache, not the budget.
    health_probe_cache.clear()
    return max(_HEALTH_BUDGET_SECONDS, 3.0 * healthy_elapsed)


async def _hangs_forever(*_args, **_kwargs):
    """The dark-RPC stand-in: answers never, exactly like the live incident."""
    await asyncio.sleep(3600)


def _fresh_oracle() -> OracleHealth:
    return OracleHealth(
        status="fresh",
        oracle_fresh=True,
        oracle_oldest_age_s=45,
        oracle_probed_count=2,
        oracle_universe_count=281,
        reason="2/2 probed oracle(s) fresh (of 281 in the universe)",
    )


async def _fast_oracle(*_args, **_kwargs) -> OracleHealth:
    return _fresh_oracle()


async def _returns_connected(*_args, **_kwargs) -> bool:
    return True


async def _get_health() -> tuple[dict, float]:
    """GET /health, returning ``(payload, elapsed_seconds)``.

    ASGITransport rather than TestClient: entering TestClient's context manager
    runs the app's startup lifespan, which seeds the corpus and warms loader
    caches for every test that runs afterwards in the same process. Precedent:
    backend/tests/test_health_is_uncacheable.py.
    """
    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        started = time.perf_counter()
        response = await asyncio.wait_for(client.get("/health"), timeout=_HARD_STOP_SECONDS)
        elapsed = time.perf_counter() - started
    assert response.status_code == 200
    return response.json(), elapsed


class TestTheEndpointAlwaysAnswers:
    """Property 1: bounded probes, whatever the network is doing."""

    async def test_health_answers_within_budget_when_the_chain_client_hangs_forever(self):
        """MUTATION: restore `connected = await chain_client.is_connected()`.

        This is the live incident, reproduced: the chain client never answers.
        Against the unbounded handler this test does not merely run slow — it
        never returns at all, and the hard stop above converts that into a
        failure instead of a hung suite.
        """
        budget = await _load_scaled_budget()
        with (
            patch.object(chain_client, "is_connected", _hangs_forever),
            patch.object(oracle_health_mod, "oracle_health", _fast_oracle),
        ):
            body, elapsed = await _get_health()

        assert elapsed < budget, (
            f"/health took {elapsed:.2f}s (budget {budget:.2f}s) with a hanging chain client — "
            f"the ALB cuts at 5s, so anything near this budget wedges every rollout"
        )
        # The endpoint answering is only half of it: the answer must say that
        # the chain reading is missing rather than quietly reporting a verdict.
        assert body["chain_probe_state"] == "probe_timeout"
        assert body["chain_connected"] is False

    async def test_health_answers_within_budget_when_the_oracle_probe_hangs_forever(self):
        """MUTATION: restore the bare `await _oracle_health_probe()` call.

        The oracle probe already carries its own 1.5s internal budget, and the
        incident proved that is not enough: the budget only covers the gather of
        per-symbol reads, so a stall in contract-loader construction, push-set
        derivation, or web3's uncancellable session-manager lock never reaches
        it. This hangs the probe itself — the path its own wait_for cannot see.
        """
        budget = await _load_scaled_budget()
        with (
            patch.object(chain_client, "is_connected", _returns_connected),
            patch.object(oracle_health_mod, "oracle_health", _hangs_forever),
        ):
            body, elapsed = await _get_health()

        assert elapsed < budget, f"/health took {elapsed:.2f}s (budget {budget:.2f}s) with a hanging oracle probe"
        assert body["oracle_probe_state"] == "probe_timeout"
        assert body["oracle_fresh"] is False

    async def test_health_answers_within_budget_when_both_are_dark(self):
        """Budgets must be spent CONCURRENTLY, not summed.

        Sequential probes would cost 1.5s + 1.8s = 3.3s and blow the promise
        even though each individual probe respected its own bound. This is the
        test that fails if someone later awaits them one after the other.
        """
        budget = await _load_scaled_budget()
        with (
            patch.object(chain_client, "is_connected", _hangs_forever),
            patch.object(oracle_health_mod, "oracle_health", _hangs_forever),
        ):
            body, elapsed = await _get_health()

        assert elapsed < budget, (
            f"/health took {elapsed:.2f}s (budget {budget:.2f}s) with both probes dark — "
            f"budgets are being summed, not shared"
        )
        assert body["chain_probe_state"] == "probe_timeout"
        assert body["oracle_probe_state"] == "probe_timeout"


class TestStalenessFieldsAppearExactlyWhenAProbeTimedOut:
    """Property 2: the payload never presents a stale value as a fresh one."""

    async def test_a_live_probe_carries_no_staleness_fields(self):
        """MUTATION: emit the age/reason fields unconditionally.

        Their presence is the alarm. Fields that are always there — `null` on
        the happy path — train every reader to ignore them, which is how a real
        staleness incident goes unnoticed.
        """
        with (
            patch.object(chain_client, "is_connected", _returns_connected),
            patch.object(oracle_health_mod, "oracle_health", _fast_oracle),
        ):
            body, _ = await _get_health()

        assert body["chain_probe_state"] == "live"
        assert body["oracle_probe_state"] == "live"
        assert "chain_probe_age_s" not in body
        assert "chain_probe_reason" not in body
        assert "oracle_probe_age_s" not in body
        assert "oracle_probe_reason" not in body
        assert body["status"] == "ok"

    async def test_a_timed_out_probe_with_nothing_cached_reports_loud_absence(self):
        """No cached reading exists, so none may be invented."""
        with (
            patch.object(chain_client, "is_connected", _hangs_forever),
            patch.object(oracle_health_mod, "oracle_health", _hangs_forever),
        ):
            body, _ = await _get_health()

        for prefix in ("chain", "oracle"):
            assert body[f"{prefix}_probe_state"] == "probe_timeout"
            assert f"{prefix}_probe_age_s" in body
            assert f"{prefix}_probe_reason" in body
            # No reading was ever taken, so there is no age to report. `null`
            # here is the honest answer; any number would be fabricated.
            assert body[f"{prefix}_probe_age_s"] is None
            assert "probe_timeout" in body[f"{prefix}_probe_reason"]

        # Absence must never be rendered as good news.
        assert body["chain_connected"] is False
        assert body["oracle_fresh"] is False
        assert body["status"] == "degraded"

    async def test_a_timed_out_probe_serves_the_last_known_value_with_its_age(self):
        """MUTATION: drop `age_s`/`reason` from the stale_cached payload fields.

        This is the whole fail-soft rule in one test: the cached value is served
        (the endpoint stays useful) and it is labelled (the endpoint stays
        honest). Serving it bare would be the plausible substitute.
        """
        # First request: both probes answer, so both values are cached.
        with (
            patch.object(chain_client, "is_connected", _returns_connected),
            patch.object(oracle_health_mod, "oracle_health", _fast_oracle),
        ):
            first, _ = await _get_health()
        assert first["chain_connected"] is True
        assert first["oracle_fresh"] is True

        # Second request: both probes go dark. The cached values come back.
        with (
            patch.object(chain_client, "is_connected", _hangs_forever),
            patch.object(oracle_health_mod, "oracle_health", _hangs_forever),
        ):
            second, elapsed = await _get_health()

        assert elapsed < _HEALTH_BUDGET_SECONDS

        assert second["chain_probe_state"] == "stale_cached"
        assert second["chain_connected"] is True  # the cached reading, unaltered
        assert second["chain_probe_age_s"] >= 0
        assert "probe_timeout" in second["chain_probe_reason"]

        assert second["oracle_probe_state"] == "stale_cached"
        assert second["oracle_fresh"] is True  # likewise
        assert second["oracle_probe_age_s"] >= 0
        assert "probe_timeout" in second["oracle_probe_reason"]
        # oracle_reason is the field an operator actually reads, so the staleness
        # has to be visible there too and not only in the sibling field.
        assert "probe_timeout" in second["oracle_reason"]
        assert "last completed read" in second["oracle_reason"]

    async def test_a_cached_connected_true_never_reports_status_ok(self):
        """MUTATION: keep `status = "ok" if connected else "degraded"`.

        A cached `chain_connected: true` is a fact about the past. Reporting the
        service "ok" off it would be a stale success — the exact failure #1520
        fixed at the CDN layer, reintroduced inside the payload.
        """
        with (
            patch.object(chain_client, "is_connected", _returns_connected),
            patch.object(oracle_health_mod, "oracle_health", _fast_oracle),
        ):
            await _get_health()

        with (
            patch.object(chain_client, "is_connected", _hangs_forever),
            patch.object(oracle_health_mod, "oracle_health", _fast_oracle),
        ):
            body, _ = await _get_health()

        assert body["chain_connected"] is True
        assert body["chain_probe_state"] == "stale_cached"
        assert body["status"] == "degraded"

    async def test_a_probe_error_is_reported_as_an_error_not_as_a_timeout(self):
        """Errors and timeouts are different states and must stay different.

        Collapsing them would let a broken probe hide behind "the network was
        slow". The pre-existing `oracle_health probe_error:` contract is
        unchanged by this issue — asserted here so it stays that way.
        """

        async def _explodes(*_args, **_kwargs):
            raise RuntimeError("boom")

        with (
            patch.object(chain_client, "is_connected", _returns_connected),
            patch.object(oracle_health_mod, "oracle_health", _explodes),
        ):
            body, _ = await _get_health()

        assert body["oracle_probe_state"] == "probe_error"
        assert "probe_error" in body["oracle_reason"]
        assert body["oracle_fresh"] is False

    async def test_a_failed_probe_never_overwrites_a_good_cached_reading(self):
        """An error result must not be cached as "last known health".

        Otherwise the next timeout serves the failure back as though it were a
        measurement, and the cache becomes a way to launder a broken probe.
        """
        cache = HealthProbeCache()

        async def _ok():
            return "measured"

        async def _boom():
            raise RuntimeError("boom")

        assert (await cache.probe("c", _ok, budget_seconds=1.0)).value == "measured"
        with pytest.raises(RuntimeError):
            await cache.probe("c", _boom, budget_seconds=1.0)

        outcome = await cache.probe("c", _hangs_forever, budget_seconds=0.05)
        assert outcome.state == "stale_cached"
        assert outcome.value == "measured"


class TestTheCacheIsPerProcessAndNotShared:
    async def test_a_second_component_does_not_borrow_the_first_ones_optimism(self):
        """Entries are keyed per component — no cross-component fallback."""
        cache = HealthProbeCache()

        async def _ok():
            return True

        await cache.probe("chain_connected", _ok, budget_seconds=1.0)
        outcome = await cache.probe("oracle_health", _hangs_forever, budget_seconds=0.05, absent=None)

        assert outcome.state == "probe_timeout"
        assert outcome.value is None

    def test_the_shared_singleton_is_resettable(self):
        """Guards the conftest fixture: without a reset hook it cannot be cleared."""
        health_probe_cache._entries["x"] = ("v", 0.0)
        health_probe_cache.clear()
        assert health_probe_cache.last_known("x") is None


class TestRunWithDeadlineGivesUpRatherThanWaiting:
    """Why `asyncio.wait_for` is not sufficient for a liveness path."""

    async def test_returns_the_value_when_the_awaitable_answers_in_time(self):
        async def _quick():
            return 42

        assert await run_with_deadline(_quick(), 1.0, label="quick") == 42

    async def test_re_raises_the_awaitables_own_exception_unchanged(self):
        async def _boom():
            raise ValueError("original")

        with pytest.raises(ValueError, match="original"):
            await run_with_deadline(_boom(), 1.0, label="boom")

    async def test_gives_up_on_an_awaitable_that_refuses_to_be_cancelled(self):
        """MUTATION: implement run_with_deadline as `asyncio.wait_for(...)`.

        On timeout `wait_for` cancels the inner task and then AWAITS the
        cancellation to finish. An awaitable that does not stop when asked
        therefore holds the "timeout" open indefinitely — the timeout bounds
        nothing.

        That is not hypothetical here. Every async JSON-RPC in web3 7.16 passes
        through `HTTPSessionManager.async_cache_and_return_session`, which opens
        with `async with async_lock(self.session_pool, self._lock)` — i.e.
        `await loop.run_in_executor(thread_pool, lock.acquire)` over a
        class-level `threading.Lock` and a 5-worker pool. `lock.acquire` in a
        worker thread is uninterruptible, and it runs entirely before aiohttp's
        request timeout is armed. The stand-in below models exactly that: an
        awaitable that swallows cancellation.
        """
        stop = asyncio.Event()

        async def _swallows_cancellation():
            while True:
                try:
                    await asyncio.sleep(0.02)
                except asyncio.CancelledError:
                    if stop.is_set():
                        raise
                    continue  # "not now" — the uninterruptible-wait stand-in

        started = time.perf_counter()
        try:
            # The outer wait_for is the hard stop: if run_with_deadline ever
            # regresses to awaiting the cancellation, this fails in 3s instead
            # of hanging the suite forever.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(
                    run_with_deadline(_swallows_cancellation(), 0.2, label="stubborn"),
                    timeout=3.0,
                )
            elapsed = time.perf_counter() - started
            assert elapsed < 1.0, f"run_with_deadline waited {elapsed:.2f}s for a task that ignores cancellation"
        finally:
            stop.set()
            await asyncio.sleep(0.05)  # let the abandoned task unwind


class TestTheChainClientItselfIsBounded:
    """FIX (2): a global ceiling on the client, not just on one call."""

    def test_the_singleton_uses_the_bounded_provider_with_the_documented_budget(self):
        """MUTATION: construct a plain AsyncHTTPProvider again.

        `rpc_timeout_seconds` is documented as a TOTAL wall-clock budget. #1507
        made web3's retry loop respect it; this makes every layer respect it,
        including the ones outside aiohttp entirely.
        """
        provider = chain_client.w3.provider
        assert isinstance(provider, BoundedAsyncHTTPProvider)
        assert provider._total_budget_seconds == chain_client.settings.rpc_timeout_seconds

    async def test_a_hanging_transport_raises_instead_of_parking_the_caller(self):
        provider = BoundedAsyncHTTPProvider("http://127.0.0.1:1", total_budget_seconds=0.2)

        with patch.object(AsyncHTTPProvider, "make_request", _hangs_forever):
            started = time.perf_counter()
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(provider.make_request("eth_chainId", []), timeout=3.0)
            elapsed = time.perf_counter() - started

        assert elapsed < 1.0, f"a bounded RPC took {elapsed:.2f}s against a 0.2s budget"

    async def test_is_connected_reports_false_rather_than_hanging(self):
        """The connection check's established meaning is preserved.

        `is_connected()` already maps every failure to False; a deadline is just
        one more failure. What changes is that it now takes bounded time to say
        so — nothing learns a different answer than it would have.
        """
        with patch.object(AsyncHTTPProvider, "make_request", _hangs_forever):
            started = time.perf_counter()
            connected = await asyncio.wait_for(chain_client.is_connected(), timeout=10.0)
            elapsed = time.perf_counter() - started

        assert connected is False
        assert elapsed < chain_client.settings.rpc_timeout_seconds + 1.0, (
            f"is_connected() took {elapsed:.2f}s against a "
            f"{chain_client.settings.rpc_timeout_seconds}s documented total budget"
        )


# ── #1594: the six LOCAL reads ───────────────────────────────────────────────

# The stall injected into each local read. 30s is the measured `/health` max
# from the incident, and it is deliberately far past every budget in play: the
# ALB's 10s, the endpoint's 2s promise, the 0.8s local-probe budget. A stall
# this size cannot be passed by accident.
_INJECTED_STALL_SECONDS = 30.0

# (fault-injection target, the payload prefix whose probe MUST report the miss).
# Targets are the module attribute the handler imports — the boundary — so a
# refactor that stops going through it fails these tests loudly instead of
# silently testing nothing. Internals are never patched.
_LOCAL_READS = [
    # #1632/#1740: the corpus probe boundary moved from load_corpus (a full
    # 18k-row ORM materialization whose abandoned-probe pileup aborted prod
    # rev 214) to an ORM-free scalar count. Same stall semantics, safe read.
    ("archimedes.services.corpus_service.count_corpus_papers", "corpus"),
    ("archimedes.services.corpus_service.get_paper_count", "corpus_db"),
    ("archimedes.services.corpus_service.get_corpus_meta", "corpus_meta"),
    ("archimedes.services.paper_rag.paper_rag_health", "paper_rag"),
    ("archimedes.services.gmm_regime_detector.gmm_regime_health", "regime_detector"),
    ("archimedes.api.risk_routes.risk_data_health", "risk_data"),
]


@contextmanager
def _stalling(target: str, seconds: float = _INJECTED_STALL_SECONDS):
    """Replace ``target`` with a read that blocks its CALLING THREAD for ``seconds``.

    ``threading.Event.wait``, not ``asyncio.sleep``: the whole failure mode is a
    read that blocks rather than awaits — an Aurora failover, a cold
    sentence-transformer import, an S3-backed corpus file — and an awaitable
    stand-in would leave the event loop free and prove nothing. The loop must
    actually be at risk for the test to be a test.

    The event is set on exit so the abandoned worker thread unwinds instead of
    parking the interpreter for 30s at shutdown. It is NOT set before the
    assertion, so the stall the handler faces is the full 30s.

    Yields the list of thread names the stalled read actually ran on, so a test
    can assert WHERE it ran and not merely that it was survived.
    """
    release = threading.Event()
    ran_on: list[str] = []

    def _stall(*_args, **_kwargs):
        ran_on.append(threading.current_thread().name)
        release.wait(seconds)

    with patch(target, _stall):
        try:
            yield ran_on
        finally:
            release.set()


@contextmanager
def _fast_local_reads():
    """Pin all six local reads to instant, in-range answers.

    Used by the live-path tests so they assert on the probe machinery rather
    than on how quickly this machine happens to load a corpus file.
    """
    from archimedes.api.risk_routes import RiskDataHealth
    from archimedes.services.gmm_regime_detector import GmmRegimeHealth
    from archimedes.services.paper_rag import PaperRAGHealth

    with (
        patch("archimedes.services.corpus_service.count_corpus_papers", lambda *_a, **_k: 1),
        patch("archimedes.services.corpus_service.get_paper_count", lambda *_a, **_k: 7),
        patch("archimedes.services.corpus_service.get_corpus_meta", lambda *_a, **_k: {"source": "db"}),
        patch(
            "archimedes.services.paper_rag.paper_rag_health",
            lambda *_a, **_k: PaperRAGHealth(status="live", reason="test"),
        ),
        patch(
            "archimedes.services.gmm_regime_detector.gmm_regime_health",
            lambda *_a, **_k: GmmRegimeHealth(status="live", reason="test"),
        ),
        patch(
            "archimedes.api.risk_routes.risk_data_health",
            lambda *_a, **_k: RiskDataHealth(status="real", reason="test"),
        ),
    ):
        yield


class TestTheSixLocalReadsAreBoundedToo:
    """#1594: a stalled LOCAL read must not hold the endpoint either.

    MUTATION for every case below: restore the plain synchronous calls
    (``corpus_count = count_corpus_papers()``, the ``try: db_count = get_paper_count()``
    block, and the three ``*_health()`` try-blocks) under the concurrent gather.
    Each test then blocks the event loop for the full 30s and fails on the
    budget assertion — including the ones whose read is "just a local DB query".
    Evidence in the PR body.
    """

    @pytest.mark.parametrize(("target", "prefix"), _LOCAL_READS, ids=[p for _, p in _LOCAL_READS])
    async def test_a_stalled_local_read_still_answers_inside_the_budget(self, target, prefix):
        with (
            patch.object(chain_client, "is_connected", _returns_connected),
            patch.object(oracle_health_mod, "oracle_health", _fast_oracle),
            _stalling(target),
        ):
            body, elapsed = await _get_health()

        assert elapsed < _HEALTH_BUDGET_SECONDS, (
            f"/health took {elapsed:.2f}s with {target} stalled — the ALB kills a target after "
            f"5 consecutive misses, which is how a good revision gets declared bad"
        )
        # Answering fast is half of it. The payload must say WHICH reading is
        # missing, or a fabricated-looking default (corpus_papers: 0,
        # regime_detector: "unknown") is indistinguishable from a real one.
        assert body[f"{prefix}_probe_state"] == "probe_timeout"
        assert body[f"{prefix}_probe_age_s"] is None  # nothing cached ⇒ no age to report
        assert "probe_timeout" in body[f"{prefix}_probe_reason"]

    async def test_all_six_stalling_at_once_still_answers(self):
        """Budgets must be shared, not summed.

        Six local reads at 0.8s each plus the two outbound probes would be 6.0s
        sequentially — past the ALB's 10s only in aggregate, but well past the
        2s this endpoint promises, and the shape that produced a 17s p95.
        """
        with (
            patch.object(chain_client, "is_connected", _returns_connected),
            patch.object(oracle_health_mod, "oracle_health", _fast_oracle),
            _stalling(_LOCAL_READS[0][0]),
            _stalling(_LOCAL_READS[1][0]),
            _stalling(_LOCAL_READS[2][0]),
            _stalling(_LOCAL_READS[3][0]),
            _stalling(_LOCAL_READS[4][0]),
            _stalling(_LOCAL_READS[5][0]),
        ):
            body, elapsed = await _get_health()

        assert elapsed < _HEALTH_BUDGET_SECONDS, (
            f"/health took {elapsed:.2f}s with all six local reads stalled — budgets are being summed"
        )
        for _target, prefix in _LOCAL_READS:
            assert body[f"{prefix}_probe_state"] == "probe_timeout"

    async def test_a_stalled_read_parks_a_dedicated_thread_not_the_loops_default_pool(self):
        """MUTATION: use ``asyncio.to_thread`` instead of the dedicated executor.

        ``to_thread`` runs on the loop's DEFAULT executor — which is also where
        asyncio runs ``getaddrinfo``. Abandoned health reads accumulating there
        would eventually make DNS for the DB, for Redis, and for the RPC queue
        behind a stuck corpus load: a liveness probe damaging the very thing it
        reports on. Under the mutation the thread is named ``asyncio_N`` and
        this fails.
        """
        with (
            patch.object(chain_client, "is_connected", _returns_connected),
            patch.object(oracle_health_mod, "oracle_health", _fast_oracle),
            _stalling(_LOCAL_READS[0][0]) as ran_on,
        ):
            body, elapsed = await _get_health()

        assert elapsed < _HEALTH_BUDGET_SECONDS
        assert body["corpus_probe_state"] == "probe_timeout"
        assert ran_on, "the corpus read never ran"
        assert all(name.startswith("health-probe") for name in ran_on), f"a health probe ran on a shared pool: {ran_on}"

    async def test_the_whole_endpoint_survives_every_probe_being_dark(self):
        """The incident's worst case: RPC throttled AND the box unresponsive."""
        with (
            patch.object(chain_client, "is_connected", _hangs_forever),
            patch.object(oracle_health_mod, "oracle_health", _hangs_forever),
            _stalling(_LOCAL_READS[0][0]),
            _stalling(_LOCAL_READS[3][0]),
        ):
            body, elapsed = await _get_health()

        assert elapsed < _HEALTH_BUDGET_SECONDS
        assert body["status"] == "degraded"
        assert body["chain_probe_state"] == "probe_timeout"
        assert body["corpus_probe_state"] == "probe_timeout"
        assert body["paper_rag_probe_state"] == "probe_timeout"


class TestTheLocalProbesObeyTheSameHonestyRules:
    """The staleness contract is identical for local reads — no second standard."""

    async def test_a_live_local_read_carries_no_staleness_fields(self):
        """MUTATION: emit the age/reason fields unconditionally.

        Presence is the alarm; always-present fields train readers to skip them.
        """
        with (
            patch.object(chain_client, "is_connected", _returns_connected),
            patch.object(oracle_health_mod, "oracle_health", _fast_oracle),
            _fast_local_reads(),
        ):
            body, _ = await _get_health()

        for _target, prefix in _LOCAL_READS:
            assert body[f"{prefix}_probe_state"] == "live"
            assert f"{prefix}_probe_age_s" not in body
            assert f"{prefix}_probe_reason" not in body

    async def test_a_stalled_local_read_serves_its_last_known_value_with_the_age(self):
        """The ALB poller gets the CACHE, not a fresh trip — labelled as cache.

        This is the fix's whole point: liveness is what the poller needs, and a
        past reading answers that question honestly as long as it says it is a
        past reading. Serving it bare would be the plausible substitute.
        """
        with (
            patch.object(chain_client, "is_connected", _returns_connected),
            patch.object(oracle_health_mod, "oracle_health", _fast_oracle),
            _fast_local_reads(),
        ):
            first, _ = await _get_health()
        assert first["corpus_papers"] == 1
        assert first["regime_detector"] == "live"

        with (
            patch.object(chain_client, "is_connected", _returns_connected),
            patch.object(oracle_health_mod, "oracle_health", _fast_oracle),
            _stalling(_LOCAL_READS[0][0]),
            _stalling(_LOCAL_READS[4][0]),
        ):
            second, elapsed = await _get_health()

        assert elapsed < _HEALTH_BUDGET_SECONDS
        assert second["corpus_probe_state"] == "stale_cached"
        assert second["corpus_papers"] == 1  # the cached reading, unaltered
        assert second["corpus_probe_age_s"] >= 0
        assert "probe_timeout" in second["corpus_probe_reason"]

        assert second["regime_detector_probe_state"] == "stale_cached"
        assert second["regime_detector"] == "live"
        # The *_reason field is what an operator actually reads, so the
        # staleness has to be visible there too, not only in the sibling field.
        assert "probe_timeout" in second["regime_detector_reason"]
        assert "last completed read" in second["regime_detector_reason"]

    async def test_a_raising_local_read_is_an_error_not_a_timeout(self):
        """Errors and timeouts stay different states.

        Collapsing them lets a broken import hide behind "the box was busy".
        """

        def _explodes(*_args, **_kwargs):
            raise RuntimeError("boom")

        with (
            patch.object(chain_client, "is_connected", _returns_connected),
            patch.object(oracle_health_mod, "oracle_health", _fast_oracle),
            patch("archimedes.services.paper_rag.paper_rag_health", _explodes),
        ):
            body, _ = await _get_health()

        assert body["paper_rag_probe_state"] == "probe_error"
        assert "probe_error" in body["paper_rag_probe_reason"]
        assert body["paper_rag_reason"] == "import failed"

    async def test_a_timed_out_local_read_never_reports_a_configured_state(self):
        """ "unknown" (never read) must not collapse into "disabled" (a real setting).

        `paper_rag: "disabled"` is a deliberate configuration. Reporting it for a
        read that never completed would present an absence as a decision.
        """
        with (
            patch.object(chain_client, "is_connected", _returns_connected),
            patch.object(oracle_health_mod, "oracle_health", _fast_oracle),
            _stalling(_LOCAL_READS[3][0]),
        ):
            body, _ = await _get_health()

        assert body["paper_rag"] == "unknown"
        assert body["paper_rag_probe_state"] == "probe_timeout"


class TestNoReportedFieldWasDropped:
    """Anti-goal guard: this change adds fields, it never removes one.

    The issue's own check is `grep -c` over four field literals. That grep is a
    PROXY for "nothing was deleted" and it moves whenever a probe *name* is
    added, so this asserts the property directly instead: every key /health
    published before #1594 is still published.

    One key has been removed since, deliberately and by owner decision:
    `fusion_enabled`, dropped 2026-09-03 (deck Q4 follow-up) once
    `ARCHIMEDES_FUSION_ENABLED` was retired and the field could only ever be the
    literal `True`. It is off the set above and pinned ABSENT by
    `test_fusion_enabled_is_not_published` below, so re-adding a constant
    dressed as a health signal trips a named test rather than passing as a
    field addition.
    """

    # Captured from origin/main's handler by AST-walking its return dict, so the
    # list is the pre-change contract rather than someone's recollection of it.
    _PRE_1594_KEYS = frozenset(
        {
            "agent_count",
            "artifact_hash",
            "chain_connected",
            "corpus_artifact_present",
            "corpus_db_count",
            "corpus_embedded_at_rest",
            "corpus_embedded_at_rest_reason",
            "corpus_kg_built",
            "corpus_kg_entities",
            "corpus_kg_relations",
            "corpus_last_intake",
            "corpus_papers",
            "corpus_source",
            "human_count",
            "llm_available",
            "llm_backend",
            "llm_has_api_key",
            "llm_has_auth_token",
            "llm_has_base_url",
            "llm_model",
            "llm_provider",
            "oracle_fresh",
            "oracle_oldest_age_s",
            "oracle_probed_count",
            "oracle_reason",
            "oracle_universe_count",
            "paper_rag",
            "paper_rag_reason",
            "paper_rerank_model_live",
            "real_users",
            "regime_detector",
            "regime_detector_reason",
            "rerank_candidate_cap",
            "reveal_reconcile_pending",
            "reveal_reconcile_terminal",
            "risk_data",
            "risk_data_reason",
            "service",
            "status",
            "strategy_count",
            "version",
        }
    )

    async def test_every_field_published_before_1594_is_still_published(self):
        with (
            patch.object(chain_client, "is_connected", _returns_connected),
            patch.object(oracle_health_mod, "oracle_health", _fast_oracle),
            _fast_local_reads(),
        ):
            body, _ = await _get_health()

        assert set(body) >= self._PRE_1594_KEYS, f"/health stopped reporting {sorted(self._PRE_1594_KEYS - set(body))}"

    async def test_fusion_enabled_is_not_published(self):
        """The one deliberate removal: `fusion_enabled` is gone, not constant-`True`.

        `ARCHIMEDES_FUSION_ENABLED` was retired in the deck-Q4 change; fusion is
        the unconditional generation path, so the field had no state left to
        report. Publishing `True` forever would be a constant wearing a health
        signal's clothes — the exact failure `corpus_embedded_at_rest` and
        friends were added to avoid. Nothing consumed it (searched backend, ui,
        cli, mcp-server, docs, scripts, tests; the only reader was this file's
        own pinned set), so the key was dropped rather than frozen.

        This asserts ABSENCE, so it is red the moment the key comes back.
        """
        with (
            patch.object(chain_client, "is_connected", _returns_connected),
            patch.object(oracle_health_mod, "oracle_health", _fast_oracle),
            _fast_local_reads(),
        ):
            body, _ = await _get_health()

        assert "fusion_enabled" not in body, (
            "/health published `fusion_enabled` again. It was dropped on 2026-09-03 "
            "(deck Q4 follow-up) because ARCHIMEDES_FUSION_ENABLED is retired and the "
            f"field could only be a constant. Value seen: {body.get('fusion_enabled')!r}"
        )

    async def test_the_status_code_stays_200_while_degraded(self):
        """Anti-goal: 200-while-degraded is deliberate.

        A 503 here would let an RPC blip or a slow disk cascade the whole ECS
        service down — the exact mechanism that took prod off the air on
        2026-08-31. _get_health already asserts 200; this states the intent so a
        later "make /health honest by 503ing" change trips a named test.
        """
        with (
            patch.object(chain_client, "is_connected", _hangs_forever),
            patch.object(oracle_health_mod, "oracle_health", _hangs_forever),
            _stalling(_LOCAL_READS[0][0]),
        ):
            body, _ = await _get_health()  # asserts status_code == 200

        assert body["status"] == "degraded"
