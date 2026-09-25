"""429-aware backoff for the Arc RPC client (#1594).

INCIDENT 2026-08-31, root cause. Live ``/archimedes/app`` logs during the
outage carried repeated ``aiohttp.client_exceptions.ClientResponseError: 429,
message='Too Many Requests', url='https://rpc.testnet.arc.network'``. Everything
the incident issue tracked was downstream of that one fact: the "VPC→RPC
intermittent latency" was 429s plus client retries, not a slow network path.

**Why a 429 is not just another transport error.** ``web3`` 7.16 opens its
session with ``raise_for_status=True``, so a throttled response surfaces as
``ClientResponseError`` — a subclass of ``ClientError``, which is in this
client's retry set. web3 therefore already retries a 429, immediately, with a
fixed ``backoff_factor * 2**i`` sleep and **no jitter**. Two consequences, both
live in the incident:

1. Retrying a throttle *adds* to the burst that caused it. A connection reset
   deserves a retry; "you are sending too much" deserves the opposite.
2. A fixed backoff re-synchronises the fleet. Every ECS task shares ONE egress
   IP (the NAT gateway), so Arc's per-IP limit is a **fleet-wide budget** — and
   with identical sleeps, tasks that get throttled together retry together, in
   lockstep, forever. During the incident every cold start burst RPC through
   that shared IP, got 429s, blew the ALB health budget, got killed, and
   restarted: a self-sustaining storm.

This module holds the state that fixes both, and nothing else — it is pure and
clock-injectable so the policy can be tested without a network:

* **Full jitter.** The backoff window doubles per consecutive 429 and the actual
  sleep is drawn uniformly from ``[0, window]``. Two tasks throttled in the same
  millisecond come back at different times. This is the standard
  exponential-backoff-with-full-jitter result, and de-synchronisation is the
  entire reason for the random draw — not politeness.
* **``Retry-After`` is a floor, never the whole answer.** When the server names
  a delay we never sleep less than it asked, and we add a jittered increment on
  top, because a fleet that honours the same ``Retry-After`` to the millisecond
  is a fleet that retries in lockstep — the very thing jitter exists to prevent.
* **A cooldown other callers can see.** After a 429 the endpoint has told us to
  stop; a concurrent caller that has not yet sent its request should not send
  it. :meth:`RateLimitBackoff.remaining` lets the caller fail that request FAST
  and loudly instead of adding to the pressure. Failing fast is not a
  degradation here — under sustained throttling the request was going to fail
  anyway, and this way it fails without costing the fleet another 429.

**Only a completed response clears the state.** A timeout or a connection error
leaves the consecutive count where it was: neither is evidence that the
endpoint has stopped throttling us, and treating them as evidence would reset
the backoff exactly when the endpoint is least able to serve us.

Process-local by design, same as ``services/health_cache.py``: one instance per
provider, so each process backs off on what IT observed. It cannot coordinate
the fleet, and does not claim to — what it can do is stop every task in the
fleet from retrying on the same schedule.
"""

from __future__ import annotations

import logging
import math
import random
import time
from collections.abc import Callable, Mapping
from email.utils import parsedate_to_datetime
from typing import Any

from aiohttp import ClientError

logger = logging.getLogger(__name__)

HTTP_TOO_MANY_REQUESTS = 429

# First backoff window. Doubles per CONSECUTIVE 429; the actual sleep is drawn
# uniformly from [0, window] (full jitter). 0.25s is chosen against the live Arc
# RPC's ~0.1s round trip — long enough to be a real pause, short enough that a
# single throttled call still fits inside chain client's 3s total budget with
# room for the retry.
DEFAULT_BASE_BACKOFF_SECONDS = 0.25

# Ceiling on the window. Past this the doubling stops and the draw stays in
# [0, 4s]: a bigger window would exceed any caller's per-call budget, so it
# would only ever be spent failing fast in the cooldown check rather than
# actually waiting — the cap keeps the number meaningful.
DEFAULT_MAX_BACKOFF_SECONDS = 4.0

# Floor on any returned delay. A full-jitter draw near zero is a legitimate
# outcome of the policy and an illegitimate thing to do to a throttled endpoint:
# it is an immediate retry wearing a backoff's name. 10ms is far below anything
# a caller notices and far above zero.
MIN_BACKOFF_SECONDS = 0.01

# Cap on the doubling exponent, applied before the multiply. Purely defensive
# arithmetic: the window is capped at ``max_seconds`` anyway, but computing
# ``base * 2**consecutive`` first would raise OverflowError once ``consecutive``
# passes ~1024, and ``consecutive`` is unbounded during a long outage because
# only a completed response resets it.
_MAX_DOUBLINGS = 32


class RpcRateLimited(ClientError):
    """The endpoint is throttling us and we chose not to send this request.

    Deliberately an ``aiohttp.ClientError``: every caller in this repo already
    treats that class as "the read failed" — ``ChainClient.is_connected`` maps
    it to ``False``, ``oracle_health`` records it as a read error — so nothing
    learns a *different* answer than it would have learned from the 429 itself.
    What changes is that it learns it without spending another request on an
    endpoint that just told us to stop.

    NOT a ``TimeoutError``. services/health_cache.py keeps errors and timeouts
    apart on purpose: collapsing them lets a throttled endpoint hide behind "the
    network was slow", which is precisely the misdiagnosis that cost this
    incident its first several hours.
    """


def retry_after_seconds(headers: Mapping[str, Any] | None, *, now: float | None = None) -> float | None:
    """Parse an HTTP ``Retry-After`` header into seconds, or ``None``.

    Both RFC 9110 forms are accepted: delay-seconds (``Retry-After: 5``) and
    HTTP-date (``Retry-After: Wed, 21 Oct 2026 07:28:00 GMT``). Anything
    unparseable, negative, or absent returns ``None`` — the caller then falls
    back to its own jittered window rather than inventing a server instruction.
    """
    if not headers:
        return None
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return None
    text = str(raw).strip()
    try:
        seconds = float(text)
    except ValueError:
        seconds = None
    if seconds is not None:
        # ``inf`` and ``nan`` parse as floats and are not delays. An infinite
        # value would set a cooldown that never expires, and every RPC in this
        # process would fail fast forever off one malformed header — a
        # permanent self-inflicted outage from a byte we do not control.
        return max(0.0, seconds) if math.isfinite(seconds) else None
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    reference = time.time() if now is None else now
    return max(0.0, when.timestamp() - reference)


class RateLimitBackoff:
    """Consecutive-429 counter plus the cooldown window it implies.

    Pure and clock-injectable. ``clock`` must be monotonic in production (wall
    time can step backwards under NTP correction and would shorten or extend a
    cooldown for reasons that have nothing to do with the endpoint); ``jitter``
    returns a float in ``[0, 1)`` and exists so a test can pin the draw and
    assert the window arithmetic instead of asserting a random number.
    """

    def __init__(
        self,
        *,
        base_seconds: float = DEFAULT_BASE_BACKOFF_SECONDS,
        max_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._base_seconds = base_seconds
        self._max_seconds = max_seconds
        self._clock = clock
        self._jitter = jitter
        self._consecutive = 0
        self._cooldown_until = 0.0

    @property
    def consecutive(self) -> int:
        """How many 429s in a row, with no completed response in between."""
        return self._consecutive

    def remaining(self) -> float:
        """Seconds left before this process should send another request.

        ``0.0`` means "go ahead". A positive number is the endpoint's own
        instruction, not a guess: it exists only because a 429 was observed.
        """
        return max(0.0, self._cooldown_until - self._clock())

    def note_rate_limited(self, retry_after: float | None = None) -> float:
        """Record a 429 and return the delay to wait before retrying.

        The window doubles per consecutive 429 up to ``max_seconds`` and the
        delay is drawn uniformly from ``[0, window]`` (full jitter). A server
        ``retry_after`` is a FLOOR: the returned delay is ``retry_after`` plus a
        jittered increment, so we never come back earlier than we were told and
        never come back at the same instant as every other task that was told
        the same thing.

        The returned delay is never below :data:`MIN_BACKOFF_SECONDS`. A full
        jitter draw can legitimately land near zero, and a near-zero backoff on
        a throttled endpoint is an immediate retry — the amplification this
        module exists to remove. The floor also makes a misconfigured
        ``base_seconds=0`` degrade to "slow" rather than to "hammer".
        """
        self._consecutive += 1
        if retry_after is not None:
            delay = retry_after + self._jitter() * self._base_seconds
        else:
            # The exponent is clamped before it reaches float arithmetic.
            # ``consecutive`` only resets on a completed response, so a long
            # throttling window drives it into the thousands, and
            # ``base * 2**1024`` raises OverflowError rather than returning a
            # large float — the backoff would crash the very call it protects.
            window = min(self._max_seconds, self._base_seconds * (2 ** min(self._consecutive - 1, _MAX_DOUBLINGS)))
            delay = self._jitter() * window
        delay = max(MIN_BACKOFF_SECONDS, delay)
        self._cooldown_until = self._clock() + delay
        return delay

    def note_success(self) -> None:
        """Clear the backoff — a completed response is the only evidence that clears it."""
        self._consecutive = 0
        self._cooldown_until = 0.0

    def reset(self) -> None:
        """Drop all state (test hygiene; production clears via :meth:`note_success`)."""
        self.note_success()
