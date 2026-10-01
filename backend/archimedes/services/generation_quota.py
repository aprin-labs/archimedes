"""Generation volume caps — per-account AND per-IP daily limits (anti-abuse).

Rebuilt for canonical identity (#1194 revision a). The original module capped
only *wallet-less* generations per-IP, because SIWE-authenticated callers were
trusted to bypass. Under Better Auth every caller to ``/api/generate/start`` is
an authenticated ``CurrentUser`` — and account creation is cheap (email format
+ password; no verification email exists yet), so a user-keyed cap alone would
be defeated by minting fresh accounts. Hence TWO stacked day-buckets, both of
which must pass:

  1. ``user:{user_id}``  — the fairness cap: one account's daily allowance.
     (``GENERATION_DAILY_CAP_PER_USER``, default 10; ``<= 0`` disables.)
  2. ``ip:{client_ip}``  — the abuse cap: bounds total LLM spend reachable from
     one address regardless of how many accounts it mints. This is what makes
     disposable accounts pointless, and is the sprint-scoped "signup friction
     equivalent" until real email verification ships (needs SES, which this
     stack does not have yet).
     (``GENERATION_DAILY_CAP_PER_IP``, default 20; ``<= 0`` disables.)

The per-IP default is deliberately higher than the per-user default: NATs and
offices share addresses, and the IP bucket exists to bound abuse, not to be the
first limit an honest user hits.

Ordering: the USER bucket is checked (and counted) first, and the IP bucket is
only touched if the user bucket allowed the request. A caller hammering past
their user cap therefore cannot drain the shared IP bucket for everyone else
behind the same NAT. The cost of this ordering is the converse: a request
rejected by the IP bucket has already consumed one count from its own user
bucket. That is accepted — the buckets self-expire daily, and the alternative
(a cross-key atomic reservation) is a Lua script this cap does not need.

Fail-closed, deliberately (revision-a requirement: "choose the failure mode
deliberately and document it"): a Redis error returns "not allowed". Under the
old module fail-closed only ever blocked anonymous callers; here it blocks
everyone, which sounds like a larger blast radius — but it is not, because the
generation path itself is Redis-backed (``job_queue.get_job_store().enqueue``
uses the same ``REDIS_URL``): when Redis is down, generation is already broken
one line later. Failing closed therefore adds NO new outage surface, and
failing open would leave LLM spend uncapped in exactly the degraded state
where monitoring is least likely to be watching. (Lineage: issue #929 made the
old cap fail-closed after a fail-open bug.)

Defense in depth, current stack: slowapi ``5/minute`` per-route burst limit,
nginx ``limit_req`` zones, Better Auth's own ``/sign-up/email`` rate limit
(5/min, production) — those bound *rates*; this module bounds daily *volume*.
"""

from __future__ import annotations

import ipaddress
import logging
import os
from datetime import UTC, datetime

import redis.asyncio as aioredis
from fastapi import HTTPException
from starlette.requests import Request

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

_DEFAULT_USER_DAILY_CAP = 10
_DEFAULT_IP_DAILY_CAP = 20
# Day-bucket TTL: a bit over 24h to cover the UTC-day boundary + clock skew, so
# each key self-expires and the allowance resets daily without a sweep.
_QUOTA_TTL_SECONDS = 36 * 60 * 60


def _cap_from_env(var: str, default: int) -> int:
    raw = os.getenv(var, str(default))
    try:
        return int(raw)
    except ValueError:
        logger.warning("invalid %s=%r; using default %d", var, raw, default)
        return default


def user_daily_cap() -> int:
    """Per-account daily generation cap. ``<= 0`` disables that layer."""
    return _cap_from_env("GENERATION_DAILY_CAP_PER_USER", _DEFAULT_USER_DAILY_CAP)


def ip_daily_cap() -> int:
    """Per-IP daily generation cap. ``<= 0`` disables that layer."""
    return _cap_from_env("GENERATION_DAILY_CAP_PER_IP", _DEFAULT_IP_DAILY_CAP)


def _valid_ip(value: str) -> bool:
    """True iff ``value`` parses as an IPv4/IPv6 address."""
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def client_ip(request: Request) -> str:
    """Resolve the real client IP behind nginx/ALB for the per-IP cap key.

    Uses ONLY trustworthy sources — a spoofed value must not let a caller rotate
    the quota key:
      1. ``X-Real-IP`` — set by nginx from its ``real_ip``-resolved ``$remote_addr``
         (it OVERWRITES any client-supplied value and binds ``real_ip_header`` to
         the ALB CIDR + CloudFront origin-facing ranges, #1908), so it is the
         viewer's address and not client-spoofable.
      2. the socket peer (``request.client.host``) — for local/non-proxied runs.

    ``X-Forwarded-For`` is DELIBERATELY NOT used: its first hop is the original
    client-supplied value, which an attacker could forge to dodge the cap.
    (Copilot #792). Every candidate is validated as a real IP before use, so a
    malformed header can't create arbitrary Redis keys; falls back to ``"unknown"``
    (a single shared bucket — still capped, just coarser).
    """
    xri = (request.headers.get("x-real-ip") or "").strip()
    if xri and _valid_ip(xri):
        return xri
    client = request.client
    host = (client.host if client and client.host else "").strip()
    if host and _valid_ip(host):
        return host
    return "unknown"


class GenerationQuota:
    """Fail-safe Redis wrapper for the stacked daily generation caps."""

    def __init__(self, url: str | None = None) -> None:
        self._url = url or REDIS_URL
        self._redis: aioredis.Redis | None = None

    async def _get_redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = aioredis.from_url(self._url, decode_responses=True)
        return self._redis

    async def check_and_increment(self, scope: str, identity: str, cap: int) -> tuple[bool, int]:
        """Atomically count one generation for ``identity`` in today's ``scope`` bucket.

        ``scope`` is ``"user"`` or ``"ip"`` (part of the Redis key, so the two
        layers can never collide even if a user id somehow parsed as an IP).
        Returns ``(allowed, used)`` where ``used`` is the count *after* this
        attempt and ``allowed`` is ``used <= cap``. **Fails closed** — a Redis
        error returns ``(False, 0)``; see the module docstring for why that is
        the deliberate choice here.
        """
        try:
            r = await self._get_redis()
            day = datetime.now(UTC).strftime("%Y-%m-%d")
            key = f"archimedes:genquota:{scope}:{day}:{identity}"
            count = await r.incr(key)
        except Exception as exc:
            logger.warning(
                "generation quota check failed for %s=%s — FAILING CLOSED (spend cap held): %s", scope, identity, exc
            )
            return (False, 0)

        # TTL is BEST-EFFORT and must not discard the already-successful INCR.
        # ``EXPIRE ... NX`` sets the TTL only when the key has none, so it
        # (a) self-heals a key whose prior EXPIRE failed / was interrupted between
        # the INCR and EXPIRE — no key is left without a TTL to grow unbounded —
        # and (b) does NOT slide the window on every hit. A failure here is logged
        # and retried (NX) on the next hit, never swallowing the count. (Copilot #792)
        try:
            await r.expire(key, _QUOTA_TTL_SECONDS, nx=True)
        except Exception as exc:
            logger.debug("genquota TTL set failed for %s:%s (retried NX next hit): %s", scope, identity, exc)

        return (count <= cap, int(count))

    async def peek(self, scope: str, identity: str) -> int | None:
        """Read (never increment) today's count for ``scope:identity``.

        Companion to :meth:`check_and_increment` for DISPLAY-only reads (e.g.
        ``GET /api/account/usage``) — it never touches enforcement and never
        writes. Uses the exact same key format and UTC-day bucketing as the
        enforcement path, so a peek always reads the same counter a concurrent
        ``check_and_increment`` would be incrementing.

        Returns the current count (``0`` when the key has never been written
        today), or ``None`` on a Redis error. ``None`` is a DIFFERENT signal
        than ``check_and_increment``'s fail-closed ``(False, 0)`` — this method
        backs a read-only display, not a gate, so the honest degraded state is
        "unknown" (``None``), never a fabricated ``0`` that would tell a caller
        they have used nothing when the truth is merely unreachable (the
        repo's fail-soft rule: an outage renders as a loud absence, not a
        plausible substitute).
        """
        try:
            r = await self._get_redis()
            day = datetime.now(UTC).strftime("%Y-%m-%d")
            key = f"archimedes:genquota:{scope}:{day}:{identity}"
            val = await r.get(key)
            # int() stays INSIDE the try: a corrupt/non-numeric stored value is
            # the same "unknown" as an unreachable Redis — the promised None,
            # never a raised ValueError surfacing as a 500 on a display read.
            return int(val) if val is not None else 0
        except Exception as exc:
            logger.warning("generation quota peek failed for %s=%s: %s", scope, identity, exc)
            return None

    async def close(self) -> None:
        if self._redis:
            try:
                await self._redis.aclose()
            except Exception as exc:
                logger.debug("generation quota close failed: %s", exc)
            self._redis = None


def _quota_503() -> HTTPException:
    """The quota backend is down — say so, honestly.

    Fail-closed must not masquerade as fail-limit: telling a user "you've
    reached today's limit" during a Redis outage is a confident wrong
    diagnosis (they may have used nothing), and wrong-cause errors are how the
    3-week zombie-oracle outage stayed invisible. ``INCR`` never legitimately
    returns 0, so ``used == 0`` from ``check_and_increment`` unambiguously
    means "error path", and that path gets a truthful 503.
    """
    return HTTPException(
        status_code=503,
        detail={
            "message": "Generation is temporarily unavailable — the usage-limit service could not be reached. Nothing was counted against your allowance.",
            "reason": "generation_quota_unavailable",
        },
    )


def _quota_429(scope: str, cap: int) -> HTTPException:
    if scope == "user":
        message = (
            f"You've reached today's generation limit ({cap}/day for this account). "
            "The allowance resets daily — or reach out if you need more."
        )
    else:
        message = f"This network address has reached today's generation limit ({cap}/day). The allowance resets daily."
    return HTTPException(
        status_code=429,
        detail={"message": message, "reason": "generation_daily_cap", "scope": scope, "cap": cap},
    )


async def enforce_generation_quota(request: Request, user_id: str) -> None:
    """Enforce both daily generation caps. Raises HTTP 429 when either is hit.

    Caller contract: ``user_id`` is the authenticated Better Auth account id
    (``CurrentUser.id``) — the route dependency guarantees it is present, so
    there is no anonymous bypass path anymore. A cap of ``<= 0`` disables that
    layer individually (both disabled = unlimited, matching the old module's
    escape hatch).

    Layer order is user-then-IP on purpose — see the module docstring.
    """
    u_cap = user_daily_cap()
    i_cap = ip_daily_cap()
    if u_cap <= 0 and i_cap <= 0:
        return

    quota = GenerationQuota()
    try:
        if u_cap > 0:
            allowed, used = await quota.check_and_increment("user", user_id, u_cap)
            if not allowed:
                if used == 0:  # error path, not a real limit — see _quota_503
                    raise _quota_503()
                logger.info("generation cap hit user=%s used=%d cap=%d", user_id, used, u_cap)
                raise _quota_429("user", u_cap)
        if i_cap > 0:
            ip = client_ip(request)
            allowed, used = await quota.check_and_increment("ip", ip, i_cap)
            if not allowed:
                if used == 0:
                    raise _quota_503()
                logger.info("generation cap hit ip=%s used=%d cap=%d", ip, used, i_cap)
                raise _quota_429("ip", i_cap)
    finally:
        await quota.close()
