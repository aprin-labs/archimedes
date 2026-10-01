"""Visitor insights — distinct-visitor geography + device class (issue #787).

We have zero promotion and a zero-conversion problem, so understanding WHO lands
on the site (where from, on what kind of device) is high-value signal. This
records, per day, the distinct visitors broken down by:

  - **country** — the ISO-3166 code CloudFront geolocates from the viewer IP and
    forwards as the ``CloudFront-Viewer-Country`` header (the ALB/origin can't see
    the real client IP — CloudFront masks it — so this header is the only clean
    geo source);
  - **device class** — mobile / tablet / desktop / tv, from CloudFront's
    ``CloudFront-Is-*-Viewer`` headers (falling back to a User-Agent sniff).

Distinct counts use Redis HyperLogLog keyed on the anonymous ``archimedes_vid``
(no PII; an HLL keeps no raw id) — same privacy-friendly approach as the funnel.
The one place a raw id is kept is the first-seen gate below: one marker key per
visitor, which expires one cookie lifetime (180 days) after the visitor is first
recorded (#1908).

Population (issue #830): this is recorded from the **same JS-gated ``landed``
beacon population the conversion funnel uses** — one source of truth for
"distinct visitor" across funnel, geography, and device. The beacon is emitted
by the SPA's JavaScript, which non-JS crawlers don't execute, so the recorded
population is browser-rendered visitors — NOT every server-side human-classified
request (that population leaks browser-UA bots through the open-demo default and
was the source of the 17-vs-50 discrepancy this issue reconciles). It is still an
UA/beacon-derived signal, not a verified-identity count; distinct *users* are the
wallet count in ``user_profiles``.

Mirrors ``services/funnel_store.py`` / ``telemetry_store.py``: same
``redis.asyncio`` convention and **fail-safe by construction** — every method
swallows Redis errors; instrumentation never turns a request into a 5xx.

Known caveat (Insights-page reconciliation follow-up, post-#854): the
first-seen-only attribution gate below (``:attributed`` marker) only prevents
NEW double-counting from the moment it shipped (2026-07-03). The
``:country:total:*`` / ``:device:total:*`` HyperLogLog keys are append-only —
visits recorded before the gate existed (when every landed beacon re-bucketed
the visitor, so a visitor whose apparent country/device varied across visits —
VPN, mobile vs. desktop — inflated multiple buckets) remain permanently baked
into the all-time totals. That pre-existing skew is why the live country/device
sums can still run ahead of the funnel's ``landed`` count even with this gate in
place; it is a stale-data problem, not a logic bug, and can only be fully
resolved by an operator deleting the ``archimedes:visitors:*`` keys (a Redis
counter reset) so the totals start accumulating exclusively under the
first-seen gate. The Insights UI states the relationship as directional
("designed to track… may not match exactly") rather than promising exact
equality, precisely because of this.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

import redis.asyncio as aioredis

from archimedes.api.funnel_middleware import _VID_TTL_SECONDS

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

_PREFIX = "archimedes:visitors"
# Day buckets self-expire after 90 days (trend history without unbounded growth).
_DAY_TTL_SECONDS = 90 * 24 * 60 * 60

# First-seen gate (#1908). One marker key per visitor, ``<prefix>:attributed:<vid>``,
# set with SET NX EX. Its lifetime is the archimedes_vid cookie's: the middleware
# mints that cookie once with this max-age and never refreshes it, and a visitor
# can only be recorded after their cookie exists, so a marker that lives this long
# from the first recording outlives every request that can still carry the id.
# First-seen counting is unchanged; the id is just not kept after it can return.
_ATTRIBUTION_TTL_SECONDS = _VID_TTL_SECONDS

# The gate before #1908: one SET of every raw visitor id, with no TTL. It is no
# longer written. During the transition it is still READ, so nobody it holds is
# counted a second time, and the first post-deploy recording gives it a TTL of
# one cookie lifetime (EXPIRE NX, so later calls never push it back). Every id in
# it came from a cookie minted before this change, so none can arrive after that
# TTL runs out: the key expires by itself and needs no migration step. After it
# is gone, SISMEMBER/EXPIRE on a missing key are no-ops, and this read can be
# deleted.
_LEGACY_ATTRIBUTED_KEY = f"{_PREFIX}:attributed"

DEVICE_CLASSES = ("mobile", "tablet", "desktop", "tv", "unknown")
_UNKNOWN_COUNTRY = "ZZ"  # ISO 3166 user-assigned code — "unknown / not provided"


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _marker_key(visitor_id: str) -> str:
    return f"{_PREFIX}:attributed:{visitor_id}"


def _norm_country(raw: str | None) -> str:
    """Normalize a CloudFront-Viewer-Country value to a 2-letter upper code or ZZ."""
    if not raw:
        return _UNKNOWN_COUNTRY
    code = raw.strip().upper()
    return code if len(code) == 2 and code.isalpha() else _UNKNOWN_COUNTRY


class VisitorInsightsStore:
    """Fail-safe Redis wrapper for distinct-visitor geo + device counters."""

    def __init__(self, url: str | None = None) -> None:
        self._url = url or REDIS_URL
        self._redis: aioredis.Redis | None = None

    async def _get_redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = aioredis.from_url(self._url, decode_responses=True)
        return self._redis

    # ─── Record (write path — JS-gated `landed` beacon, issue #830) ──────

    async def record(self, country: str | None, device: str, visitor_id: str) -> None:
        """Record ``visitor_id``'s country + device, attributed once per visitor.

        Only the visitor's FIRST recorded landed beacon buckets them into a
        country/device — a repeat visitor whose classified country or device
        differs across visits (VPN, mobile hotspot vs. home wifi, a flaky UA
        parse, …) is NOT re-bucketed. Without this gate, summing the
        per-country or per-device breakdowns can exceed the funnel's single
        overall ``landed`` distinct-visitor count, even though each
        individual bucket's own HLL count is internally correct — the
        Insights page promises these sums reconcile with ``landed``
        (issue #854 finding #6), and first-seen attribution is what makes
        that true by construction rather than by coincidence. Never raises.
        """
        if not visitor_id:
            return
        cc = _norm_country(country)
        dev = device if device in DEVICE_CLASSES else "unknown"
        try:
            r = await self._get_redis()
            if not await self._claim_first_seen(r, visitor_id):
                return
            day = _today()
            country_day = f"{_PREFIX}:country:day:{day}:{cc}"
            device_day = f"{_PREFIX}:device:day:{day}:{dev}"
            pipe = r.pipeline()
            # All-time distinct (no TTL).
            pipe.pfadd(f"{_PREFIX}:country:total:{cc}", visitor_id)
            pipe.pfadd(f"{_PREFIX}:device:total:{dev}", visitor_id)
            # Per-day distinct (TTL'd).
            pipe.pfadd(country_day, visitor_id)
            pipe.expire(country_day, _DAY_TTL_SECONDS, nx=True)
            pipe.pfadd(device_day, visitor_id)
            pipe.expire(device_day, _DAY_TTL_SECONDS, nx=True)
            # Index of which country codes have been seen, so reads can enumerate
            # them without SCANning the keyspace.
            pipe.sadd(f"{_PREFIX}:countries", cc)
            await pipe.execute()
        except Exception as exc:
            logger.debug("visitor insight record failed (%s/%s): %s", cc, dev, exc)

    @staticmethod
    async def _claim_first_seen(r: aioredis.Redis, visitor_id: str) -> bool:
        """True exactly once per visitor id per cookie lifetime (#1908).

        SET NX is atomic, so two concurrent beacons for the same new visitor
        cannot both claim it. A claim alone is not enough while the pre-#1908
        set still exists: an id already in it was attributed before the marker
        scheme, so it is not first-seen.
        """
        claimed = await r.set(_marker_key(visitor_id), "1", nx=True, ex=_ATTRIBUTION_TTL_SECONDS)
        if not claimed:
            return False
        await r.expire(_LEGACY_ATTRIBUTED_KEY, _ATTRIBUTION_TTL_SECONDS, nx=True)
        return not await r.sismember(_LEGACY_ATTRIBUTED_KEY, visitor_id)

    # ─── Read (exposure path — GET /api/metrics/visitors) ────────────────

    async def get_insights(self) -> tuple[dict[str, int], dict[str, int]]:
        """Return ``(countries, devices)`` all-time distinct-visitor maps. Zeros on error."""
        try:
            r = await self._get_redis()
            seen = await r.smembers(f"{_PREFIX}:countries")
            countries: dict[str, int] = {}
            if seen:
                codes = sorted(seen)
                pipe = r.pipeline()
                for cc in codes:
                    pipe.pfcount(f"{_PREFIX}:country:total:{cc}")
                for cc, n in zip(codes, await pipe.execute(), strict=False):
                    countries[cc] = int(n or 0)
            pipe = r.pipeline()
            for dev in DEVICE_CLASSES:
                pipe.pfcount(f"{_PREFIX}:device:total:{dev}")
            devices = {dev: int(n or 0) for dev, n in zip(DEVICE_CLASSES, await pipe.execute(), strict=False)}
            return countries, devices
        except Exception as exc:
            logger.debug("visitor insight read failed: %s", exc)
            return {}, dict.fromkeys(DEVICE_CLASSES, 0)

    async def close(self) -> None:
        if self._redis:
            try:
                await self._redis.aclose()
            except Exception as exc:
                logger.debug("visitor insight store close failed: %s", exc)
            self._redis = None
