"""/health's LLM probe must be bounded, off-thread, and honest (#1044).

#1592 rewrote ``/health`` around one rule — *report what we know, do not go and
find out* — and bounded the two probes that incident named: the chain client
and the on-chain oracle. It left ``make_llm_backend()`` exactly where it was,
because a factory call reads like construction.

It is not construction. On the ollama path — the local-mode default this issue
is about — ``OllamaBackend.available`` is a **synchronous** ``httpx.get(
"{LLM_BASE_URL}/api/tags", timeout=3.0)``. Called from inside the ``async def``
handler, with ollama down or dark, that parks the entire event loop for up to
3s per request, on the one route the ECS container HEALTHCHECK and the ALB
target-group check hammer. Same shape as the incident, different dark endpoint —
and the handler's own docstring claimed "every outbound probe is bounded" the
whole time it was true of only two of three.

**These are guards, not coverage.** Every test below was demonstrated to REJECT:
run this file with the ``_llm_probe`` block in ``main.health`` replaced by the
old inline ``backend = make_llm_backend()`` pair and they fail — the timing ones
on the deadline, the payload ones on the missing ``llm_probe_state`` /
``llm_reason`` keys (transcript in the PR body). A guard that has never been
seen failing is a guess.

Three properties, deliberately separate:

1. **A dark endpoint costs the handler no more than the probe's budget**, even
   when the backend resolution hangs. Asserted as a *difference* against the
   same endpoint with a healthy backend — never as a wall-clock bound. Several
   full suites run in parallel on one laptop here, and an absolute bound
   measures that machine at least as much as it measures the code.
2. **The event loop keeps turning** while it hangs — the property a pure
   wall-clock assertion cannot see, and the one that actually broke /health for
   every *other* route during the incident.
3. **The payload never lies about the LLM.** Unavailable says why, in a
   sentence an operator can act on; a value served from cache says it is from
   cache; a probe that never answered reports absence, not a plausible
   substitute.

Hermetic: no network, no ollama, no DB dependency. Every outbound probe is
patched.
Run: env -i HOME=$HOME PATH=$PATH PYTHONPATH=backend python -m pytest \
       backend/tests/test_health_llm_probe_bounded.py -q
"""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import patch

import pytest
from archimedes.chain.client import chain_client
from archimedes.services import oracle_health as oracle_health_mod
from archimedes.services.health_cache import clear_health_probe_cache
from archimedes.services.oracle_health import OracleHealth
from httpx import ASGITransport, AsyncClient

# A hard stop so a regression FAILS the suite instead of hanging it.
_HARD_STOP_SECONDS = 8.0

# How long the fake backend resolution blocks for. Chosen to be comfortably
# over _LLM_PROBE_BUDGET_SECONDS (1.0s) so the probe MUST time out, and under
# the hard stop so a regression is a fast red rather than a wedged run.
_BLOCK_SECONDS = 3.0

# How much longer /health may take with a DARK LLM endpoint than with a healthy
# one. This is a DIFFERENCE, not a wall-clock bound, and that is the whole
# point: an absolute "/health < 2.0s" assertion measures the machine as much as
# the code, and this repo runs several full suites in parallel on one laptop —
# an earlier revision of this file was written that way and flaked at 2.11s and
# 2.19s against a 2.0s bar while the code under test was perfectly correct.
#
# A guard that fires on a busy box is a guard that gets muted, so the flake is
# a defect in the test, not a tolerance to widen. Timing the SAME endpoint with
# a fast backend and then with a hanging one cancels ambient load: both halves
# pay it. What survives is the LLM probe's own contribution, which is the only
# thing this test has an opinion about.
#
# Budget is 1.0s, so a bounded probe contributes ~1.0s and this passes with a
# full second of slack. An UNBOUNDED probe contributes the entire block — 3.0s,
# three times the bar — so the mutation is still caught by a wide margin at
# both ends. Measured: ~1.0s bounded, ~3.0s unbounded.
_MAX_DARK_ENDPOINT_COST_SECONDS = 2.0


@pytest.fixture(scope="module", autouse=True)
def _warm_the_app():
    """Pay /health's one-time costs BEFORE anything is timed.

    The first ``/health`` in a process also loads the corpus and walks the
    DB-miss paths — hundreds of milliseconds that have nothing to do with the
    LLM probe. The differential assertions above already cancel *steady* load;
    this cancels the *one-time* cost, which a difference between two calls
    cannot see if only the first call pays it. Measuring the warm path is also
    the honest thing to hold to a promise: the ECS container HEALTHCHECK and
    the ALB target-group check hit a warm process every 30s forever.

    ``asyncio.run`` on a throwaway loop, then the probe cache is cleared: this
    call runs UNPATCHED, so its real chain/oracle/LLM readings must not survive
    into a test that is about what happens when those probes go dark. (The
    conftest's function-scoped ``_clear_health_probe_cache`` clears again before
    each test; this is belt-and-braces for the module-scope ordering.)
    """

    async def _once():
        from archimedes.main import app

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.get("/health")

    asyncio.run(_once())
    clear_health_probe_cache()


class _LiveBackend:
    """A reachable ollama with the configured model pulled."""

    model_id = "llama3.1"
    served_model = "llama3.1"
    available = True
    unavailable_reason = ""


class _CannedBackend:
    """What ``make_llm_backend()`` returns when ollama is unreachable."""

    model_id = "canned-fallback"
    served_model = "canned-fallback"
    available = False
    unavailable_reason = "ollama unreachable at http://10.255.255.1:11434: ConnectTimeout: timed out"


def _blocking_factory(release: threading.Event):
    """A ``make_llm_backend`` that blocks the way the real ollama probe does.

    ``threading.Event.wait`` rather than ``asyncio.sleep`` on purpose: the
    defect under test is a *synchronous* call inside an async handler, so the
    stand-in has to block a thread, not yield to the loop. The event lets the
    test release it the instant the assertion is made, so the abandoned worker
    thread does not outlive the test (a bare ``time.sleep(3600)`` would hang
    interpreter shutdown — the default executor joins its threads at exit).
    """

    def _factory(*_args, **_kwargs):
        release.wait(timeout=_BLOCK_SECONDS)
        return _LiveBackend()

    return _factory


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
    backend/tests/test_health_always_answers.py.
    """
    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        started = time.perf_counter()
        response = await asyncio.wait_for(client.get("/health"), timeout=_HARD_STOP_SECONDS)
        elapsed = time.perf_counter() - started
    assert response.status_code == 200
    return response.json(), elapsed


def _quiet_neighbours():
    """Patch the chain and oracle probes so only the LLM probe is under test."""
    return (
        patch.object(chain_client, "is_connected", _returns_connected),
        patch.object(oracle_health_mod, "oracle_health", _fast_oracle),
    )


def _with_backend(backend):
    return patch("archimedes.services.llm_backend.make_llm_backend", lambda *a, **k: backend)


class TestTheLlmProbeIsBounded:
    """Property 1: the handler answers, whatever the LLM endpoint is doing."""

    async def test_a_dark_llm_endpoint_costs_health_no_more_than_the_probe_budget(self):
        """MUTATION: restore the inline `backend = make_llm_backend()`.

        This is the live shape of a dark ollama: LLM_BASE_URL points somewhere
        that accepts the connection and never replies, so httpx sits on its 3s
        timeout and the handler sits with it. Unbounded, /health spends that
        entire 3s before it even starts composing a payload — which on a 5s ALB
        check is most of the way to wedging a rollout.

        Measured as a DIFFERENCE against the same endpoint with a healthy
        backend, so the assertion is about the probe and not about how busy the
        machine is. See ``_MAX_DARK_ENDPOINT_COST_SECONDS``.
        """
        chain_p, oracle_p = _quiet_neighbours()
        with chain_p, oracle_p, _with_backend(_LiveBackend()):
            healthy_body, healthy_elapsed = await _get_health()

        # The baseline call is a real /health, so it cached a real reading —
        # which would make the dark call below report `stale_cached` and quietly
        # change what this test is about. Drop it: the timing baseline is
        # wanted, the cached VALUE is not. (`stale_cached` has its own test.)
        clear_health_probe_cache()

        release = threading.Event()
        chain_p, oracle_p = _quiet_neighbours()
        try:
            with (
                chain_p,
                oracle_p,
                patch("archimedes.services.llm_backend.make_llm_backend", _blocking_factory(release)),
            ):
                body, dark_elapsed = await _get_health()
        finally:
            release.set()

        cost = dark_elapsed - healthy_elapsed
        assert cost < _MAX_DARK_ENDPOINT_COST_SECONDS, (
            f"a dark LLM endpoint cost /health {cost:.2f}s "
            f"({dark_elapsed:.2f}s dark vs {healthy_elapsed:.2f}s healthy) — the probe is being "
            f"awaited to completion ({_BLOCK_SECONDS:.1f}s) instead of bounded at "
            f"{_MAX_DARK_ENDPOINT_COST_SECONDS:.1f}s. The ALB cuts at 5s."
        )
        # Asserted AFTER the timing, deliberately: against the unbounded handler
        # these keys do not exist at all, and a KeyError here would mask the
        # timing failure that is the actual point of this test.
        assert healthy_body["llm_probe_state"] == "live"  # the baseline really was the fast path
        # Answering is only half of it: the answer must say the reading is
        # missing rather than quietly reporting a verdict.
        assert body["llm_probe_state"] == "probe_timeout"
        assert body["llm_available"] is False

    async def test_the_event_loop_keeps_turning_while_the_llm_probe_blocks(self):
        """MUTATION: restore the inline `backend = make_llm_backend()`.

        **This is the test the wall-clock assertions cannot make.** A blocking
        call inside the handler does not just make /health slow — it stops the
        loop, so every OTHER in-flight request on that worker stalls with it.
        That is precisely how one dark endpoint took the leaderboard down to
        10s in the incident. A ticker coroutine counts loop turns while /health
        runs: off-thread the loop keeps servicing it; inline it flatlines.
        """
        release = threading.Event()
        ticks = 0
        stop = asyncio.Event()

        async def _ticker() -> None:
            nonlocal ticks
            while not stop.is_set():
                await asyncio.sleep(0.01)
                ticks += 1

        ticker = asyncio.create_task(_ticker())
        chain_p, oracle_p = _quiet_neighbours()
        try:
            with (
                chain_p,
                oracle_p,
                patch("archimedes.services.llm_backend.make_llm_backend", _blocking_factory(release)),
            ):
                _body, elapsed = await _get_health()
        finally:
            release.set()
            stop.set()
            await ticker

        # The probe's own budget is 1.0s, so a turning loop gets ~100 ticks at
        # 10ms. Measured against the pre-fix handler it drops to 3-4 — the only
        # turns that slip in around the blocking call, not during it. 10 is the
        # floor: comfortably above what a blocked loop can produce, far below a
        # healthy one, and loose enough for a loaded CI box.
        assert ticks >= 10, (
            f"the event loop turned only {ticks} times during a {elapsed:.2f}s /health — "
            f"the LLM probe is blocking the loop, not just its own request"
        )


class TestThePayloadTellsTheTruthAboutTheLlm:
    """Property 3: honest states, and a reason an operator can act on."""

    async def test_a_live_probe_carries_no_staleness_fields_and_no_reason(self):
        """MUTATION: emit the age/reason fields unconditionally.

        Their presence is the alarm. Fields that are always there — `null` on
        the happy path — train every reader to ignore them.
        """
        chain_p, oracle_p = _quiet_neighbours()
        with chain_p, oracle_p, _with_backend(_LiveBackend()):
            body, _ = await _get_health()

        assert body["llm_available"] is True
        assert body["llm_backend"] == "live"
        assert body["llm_model"] == "llama3.1"
        assert body["llm_reason"] == ""
        assert body["llm_probe_state"] == "live"
        assert "llm_probe_age_s" not in body
        assert "llm_probe_reason" not in body

    async def test_an_unavailable_backend_reports_why_not_just_false(self):
        """MUTATION: drop `llm_reason` from the payload (pre-#1044 behaviour).

        `llm_available: false` is not actionable: it reads identically for "you
        never started ollama", "you forgot LLM_MODEL", and "you set a model you
        never pulled". The reason is what turns a support round-trip into a
        one-line fix, and it is the whole point of the honesty contract — the
        canned fallback must be LOUD, not merely present.
        """
        chain_p, oracle_p = _quiet_neighbours()
        with chain_p, oracle_p, _with_backend(_CannedBackend()):
            body, _ = await _get_health()

        assert body["llm_available"] is False
        assert body["llm_backend"] == "canned-fallback"
        assert "ollama unreachable" in body["llm_reason"]
        # A fast honest "no" is a live reading, not a timeout — the two states
        # must stay distinguishable.
        assert body["llm_probe_state"] == "live"

    async def test_a_timed_out_probe_with_nothing_cached_reports_loud_absence(self):
        """No reading was ever taken, so none may be invented — and absence
        must never be rendered as good news."""
        release = threading.Event()
        chain_p, oracle_p = _quiet_neighbours()
        try:
            with (
                chain_p,
                oracle_p,
                patch("archimedes.services.llm_backend.make_llm_backend", _blocking_factory(release)),
            ):
                body, _ = await _get_health()
        finally:
            release.set()

        assert body["llm_probe_state"] == "probe_timeout"
        assert body["llm_probe_age_s"] is None
        assert "probe_timeout" in body["llm_probe_reason"]
        assert body["llm_available"] is False
        assert body["llm_backend"] == "unavailable"
        assert "probe_timeout" in body["llm_reason"]

    async def test_a_timed_out_probe_serves_the_last_known_value_with_its_age(self):
        """The fail-soft rule in one test: the cached value is served (useful)
        and it is labelled (honest). Serving it bare is the plausible
        substitute this codebase treats as its primary defect class."""
        chain_p, oracle_p = _quiet_neighbours()
        with chain_p, oracle_p, _with_backend(_LiveBackend()):
            first, healthy_elapsed = await _get_health()
        assert first["llm_available"] is True

        release = threading.Event()
        chain_p, oracle_p = _quiet_neighbours()
        try:
            with (
                chain_p,
                oracle_p,
                patch("archimedes.services.llm_backend.make_llm_backend", _blocking_factory(release)),
            ):
                second, dark_elapsed = await _get_health()
        finally:
            release.set()

        # Serving from cache must also be FAST — the point of the cache is that
        # the handler stops waiting, not that it waits and then lies. Same
        # load-cancelling difference as the bounded-probe test above; the first
        # call here doubles as the baseline.
        assert dark_elapsed - healthy_elapsed < _MAX_DARK_ENDPOINT_COST_SECONDS
        assert second["llm_probe_state"] == "stale_cached"
        assert second["llm_available"] is True  # the cached reading, unaltered
        assert second["llm_probe_age_s"] >= 0
        assert "probe_timeout" in second["llm_probe_reason"]
        # llm_reason is the field an operator actually reads, so the staleness
        # has to be visible there too and not only in the sibling field.
        assert "probe_timeout" in second["llm_reason"]
        assert "last completed read" in second["llm_reason"]

    async def test_a_probe_error_is_reported_as_an_error_not_as_a_timeout(self):
        """Errors and timeouts are different states and must stay different.

        Collapsing them would let a broken factory hide behind "the network was
        slow", and — worse — a raised probe must not take the whole endpoint
        down with it. Mirrors the chain probe's `probe_error` contract.
        """

        def _explodes(*_args, **_kwargs):
            raise RuntimeError("boom")

        chain_p, oracle_p = _quiet_neighbours()
        with chain_p, oracle_p, patch("archimedes.services.llm_backend.make_llm_backend", _explodes):
            body, _ = await _get_health()

        assert body["llm_probe_state"] == "probe_error"
        assert body["llm_available"] is False
        assert "probe_error" in body["llm_reason"]
        assert "boom" in body["llm_reason"]
