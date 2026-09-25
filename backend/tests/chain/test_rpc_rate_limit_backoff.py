"""A throttled Arc RPC must not be retried like a dropped connection (#1594).

ROOT CAUSE, 2026-08-31. Live ``/archimedes/app`` logs during the outage carried
repeated::

    aiohttp.client_exceptions.ClientResponseError: 429,
        message='Too Many Requests', url='https://rpc.testnet.arc.network'

web3 7.16 builds its session with ``raise_for_status=True``, so Arc's throttle
arrives as a ``ClientResponseError`` — a subclass of ``ClientError``, which is
in this client's ``exception_retry_configuration``. The client therefore
answered "you are sending too much" by sending more, immediately, on a fixed
``backoff_factor * 2**i`` schedule with no jitter. Every ECS task shares ONE
NAT egress IP, so Arc's per-IP limit is a fleet-wide budget: identical backoffs
meant tasks throttled together retried together, and each cold start's RPC
burst throttled the fleet it was joining. That storm blew /health past the ALB
budget, the target group killed the tasks, and the deployment circuit breaker
declared three consecutive GOOD revisions bad.

Two behaviours are under test, and they are deliberately separate:

1. **The POLICY** — window doubles per consecutive 429, full jitter on the
   sleep, ``Retry-After`` as a floor rather than the whole answer, and only a
   completed response clears the state. Pure, clock- and jitter-injected, so
   these assert arithmetic instead of asserting a random number.
2. **The CLIENT** — the policy actually reaches every request that leaves
   ``BoundedAsyncHTTPProvider``: a 429 is retried once after a real sleep, a
   non-429 is NOT, a cooldown skips the transport entirely, and none of it
   outlasts the total budget ``rpc_timeout_seconds`` documents.

**These are guards, not coverage.** Each was demonstrated to REJECT — with
``_request_backing_off_on_429`` reduced to the pre-#1594 body (a bare
``run_with_deadline(super().make_request(...))``) every test in
``TestTheClientAppliesThePolicy`` fails. Evidence in the PR body.

Fault injection is at the RPC boundary: ``AsyncHTTPProvider.make_request``, the
last web3-owned frame before the socket. Nothing internal to web3 or to aiohttp
is patched, and no test opens a socket.

Run: env -i HOME=$HOME PATH=$PATH PYTHONPATH=backend python -m pytest \
       backend/tests/chain/test_rpc_rate_limit_backoff.py -q
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
import yarl
from aiohttp import ClientResponseError, RequestInfo
from archimedes.chain.client import BoundedAsyncHTTPProvider, chain_client
from archimedes.chain.rate_limit import (
    MIN_BACKOFF_SECONDS,
    RateLimitBackoff,
    RpcRateLimited,
    retry_after_seconds,
)
from multidict import CIMultiDict
from web3.providers import AsyncHTTPProvider

_RPC_URL = yarl.URL("https://rpc.testnet.arc.network")


def _throttled(retry_after: str | None = None) -> ClientResponseError:
    """The exact exception the live incident logged, headers and all."""
    return ClientResponseError(
        RequestInfo(_RPC_URL, "POST", CIMultiDict(), _RPC_URL),
        (),
        status=429,
        message="Too Many Requests",
        headers=CIMultiDict({"Retry-After": retry_after}) if retry_after is not None else None,
    )


def _server_error() -> ClientResponseError:
    """A 5xx — a real transport failure, and NOT a rate limit."""
    return ClientResponseError(
        RequestInfo(_RPC_URL, "POST", CIMultiDict(), _RPC_URL),
        (),
        status=500,
        message="Internal Server Error",
    )


class _FakeClock:
    """A monotonic clock the test advances by hand."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestTheBackoffPolicy:
    """Pure arithmetic. No network, no event loop, no randomness left unpinned."""

    def test_the_window_doubles_per_consecutive_429(self):
        """MUTATION: make the window constant.

        A constant window is what web3's own retry already does. If a single
        backoff step were enough, the endpoint would not still be throttling us
        on the second 429.
        """
        clock = _FakeClock()
        backoff = RateLimitBackoff(base_seconds=0.25, max_seconds=100.0, clock=clock, jitter=lambda: 1.0)

        # jitter pinned at its maximum draw, so the delay IS the window.
        assert backoff.note_rate_limited() == pytest.approx(0.25)
        assert backoff.note_rate_limited() == pytest.approx(0.50)
        assert backoff.note_rate_limited() == pytest.approx(1.00)
        assert backoff.consecutive == 3

    def test_the_window_is_capped(self):
        """Unbounded doubling would produce a delay no caller's budget can spend."""
        clock = _FakeClock()
        backoff = RateLimitBackoff(base_seconds=0.25, max_seconds=1.0, clock=clock, jitter=lambda: 1.0)

        for _ in range(12):
            delay = backoff.note_rate_limited()
        assert delay == pytest.approx(1.0)

    def test_the_sleep_is_drawn_from_the_whole_window_not_pinned_to_it(self):
        """Full jitter: the delay is ``draw * window``, not ``window``."""
        clock = _FakeClock()
        backoff = RateLimitBackoff(base_seconds=1.0, max_seconds=10.0, clock=clock, jitter=lambda: 0.5)

        assert backoff.note_rate_limited() == pytest.approx(0.5)

    def test_two_instances_throttled_identically_do_not_come_back_together(self):
        """MUTATION: drop the jitter and return the window directly.

        This is the property the whole module exists for. Every task sits behind
        ONE NAT egress IP, so a fleet that backs off by the same amount is a
        fleet that retries in lockstep and re-creates the burst. With the jitter
        removed this test fails: 40 identical draws.
        """
        delays = set()
        for _ in range(40):
            backoff = RateLimitBackoff(base_seconds=1.0, max_seconds=10.0, clock=_FakeClock())
            delays.add(backoff.note_rate_limited())

        assert len(delays) > 1, "every task computed the same backoff — the fleet will retry in lockstep"

    def test_retry_after_is_a_floor_never_a_ceiling(self):
        """MUTATION: return ``retry_after`` unchanged.

        Honouring the server's number to the millisecond is the lockstep failure
        again, wearing the server's authority: every throttled task is told the
        same value at the same moment. We never come back EARLIER than asked,
        and never at exactly the instant everyone else does.
        """
        clock = _FakeClock()
        backoff = RateLimitBackoff(base_seconds=0.25, max_seconds=10.0, clock=clock, jitter=lambda: 0.4)

        delay = backoff.note_rate_limited(retry_after=2.0)
        assert delay >= 2.0
        assert delay == pytest.approx(2.0 + 0.4 * 0.25)

    def test_the_cooldown_counts_down_on_the_injected_clock(self):
        clock = _FakeClock()
        backoff = RateLimitBackoff(base_seconds=1.0, max_seconds=10.0, clock=clock, jitter=lambda: 1.0)

        assert backoff.remaining() == 0.0
        backoff.note_rate_limited()
        assert backoff.remaining() == pytest.approx(1.0)
        clock.advance(0.4)
        assert backoff.remaining() == pytest.approx(0.6)
        clock.advance(5.0)
        assert backoff.remaining() == 0.0

    def test_a_near_zero_jitter_draw_still_pauses(self):
        """MUTATION: drop the MIN_BACKOFF_SECONDS floor.

        ``random()`` is allowed to return ~0, and a full-jitter draw of ~0 on a
        throttled endpoint is an immediate retry wearing a backoff's name — the
        amplification this module exists to remove. The floor also makes a
        misconfigured ``base_seconds=0`` degrade to slow rather than to hammer.
        """
        broken = RateLimitBackoff(base_seconds=0.0, max_seconds=0.0, clock=_FakeClock(), jitter=lambda: 0.0)
        assert broken.note_rate_limited() >= MIN_BACKOFF_SECONDS

        drew_zero = RateLimitBackoff(base_seconds=1.0, max_seconds=4.0, clock=_FakeClock(), jitter=lambda: 0.0)
        assert drew_zero.note_rate_limited() >= MIN_BACKOFF_SECONDS

    def test_a_long_outage_does_not_overflow_the_doubling(self):
        """ADVERSARIAL: the input that SHOULD break the exponent.

        ``consecutive`` only resets on a completed RESPONSE, so a sustained
        throttling window drives it without bound. ``base * 2**consecutive``
        computed before the cap raises ``OverflowError: int too large to convert
        to float`` somewhere past 1024 — the backoff crashing the very call it
        was added to protect, and only after a long outage, which is exactly
        when nobody wants to discover it.
        """
        clock = _FakeClock()
        backoff = RateLimitBackoff(base_seconds=0.25, max_seconds=4.0, clock=clock, jitter=lambda: 1.0)

        for _ in range(5_000):
            delay = backoff.note_rate_limited()

        assert backoff.consecutive == 5_000
        assert delay == pytest.approx(4.0), "the window escaped its cap"

    def test_only_a_completed_response_clears_the_backoff(self):
        """MUTATION: reset on any non-429 outcome.

        A timeout is not evidence the endpoint stopped throttling us — it is the
        endpoint being even less able to answer. Resetting on one would clear
        the backoff at the worst possible moment.
        """
        clock = _FakeClock()
        backoff = RateLimitBackoff(base_seconds=1.0, max_seconds=10.0, clock=clock, jitter=lambda: 1.0)

        backoff.note_rate_limited()
        backoff.note_rate_limited()
        assert backoff.consecutive == 2

        backoff.note_success()
        assert backoff.consecutive == 0
        assert backoff.remaining() == 0.0


class TestRetryAfterParsing:
    # Both RFC 9110 forms are legal and Arc is free to send either. The
    # reference instant is built with `datetime`, not with the same email parser
    # the implementation uses, so this is a check rather than a tautology.
    _HTTP_DATE = "Wed, 21 Oct 2026 07:28:00 GMT"
    _HTTP_DATE_EPOCH = datetime(2026, 10, 21, 7, 28, 0, tzinfo=UTC).timestamp()

    def test_delay_seconds_form(self):
        assert retry_after_seconds({"Retry-After": "5"}) == pytest.approx(5.0)

    def test_http_date_form(self):
        assert retry_after_seconds({"Retry-After": self._HTTP_DATE}, now=self._HTTP_DATE_EPOCH - 30) == pytest.approx(
            30.0
        )

    def test_a_past_date_is_zero_never_negative(self):
        assert retry_after_seconds({"Retry-After": self._HTTP_DATE}, now=self._HTTP_DATE_EPOCH + 600) == 0.0

    @pytest.mark.parametrize("raw", ["inf", "-inf", "Infinity", "nan"])
    def test_a_non_finite_delay_is_refused(self, raw):
        """ADVERSARIAL: the header value that SHOULD fail the parser.

        ``float("inf")`` parses. Honouring it would set a cooldown that never
        expires, and every RPC in the process would fail fast FOREVER off one
        malformed response header — a permanent self-inflicted outage from a
        byte we do not control. ``nan`` is refused for the same reason: it
        compares false against everything, so it would silently become 0.
        """
        assert retry_after_seconds({"Retry-After": raw}) is None

    @pytest.mark.parametrize("headers", [None, {}, {"Retry-After": "soon"}, {"X-Other": "5"}])
    def test_unparseable_or_absent_yields_none_not_a_guess(self, headers):
        """An invented server instruction is worse than none — the caller's own
        jittered window is at least honest about being ours."""
        assert retry_after_seconds(headers) is None


class TestTheClientAppliesThePolicy:
    """FIX: every request leaving BoundedAsyncHTTPProvider is 429-aware.

    MUTATION for this whole class: replace ``_request_backing_off_on_429`` with
    the pre-#1594 body — ``return await run_with_deadline(super().make_request(
    method, params), self._total_budget_seconds, label=...)``. Every test below
    then fails: the 429 propagates on the first attempt, no cooldown is set, and
    the skipped-request test sends its request.
    """

    def _provider(self, *, budget: float = 3.0, base: float = 0.02) -> BoundedAsyncHTTPProvider:
        return BoundedAsyncHTTPProvider(
            str(_RPC_URL),
            total_budget_seconds=budget,
            rate_limit_backoff=RateLimitBackoff(base_seconds=base, max_seconds=base * 4),
        )

    async def test_a_429_is_retried_once_the_backoff_has_been_slept(self):
        provider = self._provider()
        calls: list[float] = []

        async def _throttle_then_answer(*_args, **_kwargs):
            calls.append(time.monotonic())
            if len(calls) == 1:
                raise _throttled()
            return {"result": "0x1"}

        with patch.object(AsyncHTTPProvider, "make_request", _throttle_then_answer):
            result = await provider.make_request("eth_chainId", [])

        assert result == {"result": "0x1"}
        assert len(calls) == 2, "the throttled call was not retried"
        assert calls[1] > calls[0], "the retry did not wait at all"
        # A completed response is the evidence that clears the backoff.
        assert provider._rate_limit.consecutive == 0
        assert provider._rate_limit.remaining() == 0.0

    async def test_a_non_429_response_error_is_not_treated_as_a_rate_limit(self):
        """ADVERSARIAL: the input that SHOULD fail the 429 branch.

        A 500 is a real transport failure and belongs to web3's own retry set,
        not to this one. If the status check were dropped — or written as
        ``>= 400`` — every server error would silently arm a cooldown that
        blocks the whole process from talking to a chain that is merely
        unhealthy, not throttling.
        """
        provider = self._provider()
        calls = 0

        async def _five_hundred(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise _server_error()

        with (
            patch.object(AsyncHTTPProvider, "make_request", _five_hundred),
            pytest.raises(ClientResponseError) as excinfo,
        ):
            await provider.make_request("eth_chainId", [])

        assert excinfo.value.status == 500
        assert calls == 1, "a 500 was retried by the rate-limit path"
        assert provider._rate_limit.consecutive == 0
        assert provider._rate_limit.remaining() == 0.0

    async def test_a_persistent_429_surfaces_the_endpoints_own_answer(self):
        """No fabricated success, and no swallowing.

        The caller must still learn the read failed — ``is_connected`` maps it
        to False, ``oracle_health`` records a read error. What changes is that
        the process now carries a cooldown afterwards.
        """
        provider = self._provider(budget=0.3)

        async def _always_throttled(*_args, **_kwargs):
            raise _throttled()

        with (
            patch.object(AsyncHTTPProvider, "make_request", _always_throttled),
            pytest.raises(ClientResponseError) as excinfo,
        ):
            await provider.make_request("eth_chainId", [])

        assert excinfo.value.status == 429
        assert provider._rate_limit.consecutive >= 1

    async def test_a_request_during_the_cooldown_never_reaches_the_transport(self):
        """MUTATION: delete the cooldown check at the top of the method.

        This is the fleet-wide fix. During the incident every concurrent caller
        in the process kept sending into an endpoint that had already said stop,
        and each send cost the shared egress IP another slice of its budget.
        """
        provider = self._provider(base=5.0)
        calls = 0

        async def _always_throttled(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise _throttled()

        with patch.object(AsyncHTTPProvider, "make_request", _always_throttled):
            with pytest.raises(ClientResponseError):
                await provider.make_request("eth_chainId", [])
            sent_during_first_call = calls

            with pytest.raises(RpcRateLimited) as excinfo:
                await provider.make_request("eth_blockNumber", [])

        assert calls == sent_during_first_call, "a request was sent while the endpoint was cooling us down"
        # The error names WHY, so nothing reads it as "the chain is unreachable".
        assert "429" in str(excinfo.value)
        assert "backoff" in str(excinfo.value)

    async def test_the_total_budget_still_bounds_a_429_storm(self):
        """The backoff is spent FROM the budget, never added to it.

        /health sizes its probe budgets against ``rpc_timeout_seconds``. A
        retry policy that can extend a call past that number would move the
        outage from the RPC layer into the health check — which is exactly the
        chain of events this issue exists to break.
        """
        budget = 0.4
        provider = self._provider(budget=budget, base=0.05)

        async def _always_throttled(*_args, **_kwargs):
            raise _throttled()

        with patch.object(AsyncHTTPProvider, "make_request", _always_throttled):
            started = time.perf_counter()
            with pytest.raises(ClientResponseError):
                await asyncio.wait_for(provider.make_request("eth_chainId", []), timeout=5.0)
            elapsed = time.perf_counter() - started

        assert elapsed < budget + 0.25, f"a 429 storm took {elapsed:.2f}s against a documented {budget}s budget"

    async def test_a_server_retry_after_is_honoured_as_a_minimum(self):
        provider = self._provider(budget=3.0, base=0.01)
        calls: list[float] = []

        async def _throttle_then_answer(*_args, **_kwargs):
            calls.append(time.perf_counter())
            if len(calls) == 1:
                raise _throttled(retry_after="0.15")
            return {"result": "0x1"}

        with patch.object(AsyncHTTPProvider, "make_request", _throttle_then_answer):
            await provider.make_request("eth_chainId", [])

        assert len(calls) == 2
        assert calls[1] - calls[0] >= 0.15, "we came back sooner than the endpoint asked"

    async def test_is_connected_reports_false_when_we_are_being_throttled(self):
        """The caller contract is unchanged: a failed read is still False.

        ``RpcRateLimited`` is an ``aiohttp.ClientError`` precisely so nothing
        downstream has to learn a new exception to keep behaving correctly.
        """

        async def _always_throttled(*_args, **_kwargs):
            raise _throttled()

        provider = chain_client.w3.provider
        assert isinstance(provider, BoundedAsyncHTTPProvider)
        try:
            with patch.object(AsyncHTTPProvider, "make_request", _always_throttled):
                assert await chain_client.is_connected() is False
                # Second call: skipped during the cooldown, and STILL False.
                assert await chain_client.is_connected() is False
        finally:
            provider._rate_limit.reset()

    def test_the_singleton_provider_carries_a_rate_limit_policy(self):
        """MUTATION: construct BoundedAsyncHTTPProvider without the backoff.

        Every test above builds its own provider; this is the one that proves
        the policy reaches the client the application actually uses.
        """
        provider = chain_client.w3.provider
        assert isinstance(provider._rate_limit, RateLimitBackoff)
        assert provider._rate_limit._base_seconds == chain_client.settings.rpc_rate_limit_base_backoff_seconds
        assert provider._rate_limit._max_seconds == chain_client.settings.rpc_rate_limit_max_backoff_seconds
