"""Funnel store — distinct-visitor conversion-funnel counters in Redis (#787).

Backs the conversion-funnel instrument. We have a zero-conversion problem
(~40k requests → 2 wallets → 2 vaults) and, until now, no way to see *where*
visitors drop. This records the distinct visitors that reach each stage of the
journey:

    landed → generation_started → free_generation_used → wallet_gate_shown
           → wallet_connected → vault_deployed

(the two middle stages, and this order, arrived with the free path — #1643;
see ``STAGES`` below for why the order itself had to change)

using Redis HyperLogLog (``PFADD`` / ``PFCOUNT``) so the counts are *distinct
visitors* without retaining any raw identifier — privacy-friendly (no tracking
dossier) and O(1) memory per stage.

The funnel is naturally human-weighted, unlike the raw human/agent counters in
``telemetry_store.py``: ``landed`` is emitted by the SPA's JS (crawlers don't
run JS) and the downstream stages are real product actions, so the bot floor
that dominates the raw request counts largely drops out here.

Design mirrors ``services/telemetry_store.py`` deliberately:
  - same ``redis.asyncio.from_url`` + ``REDIS_URL`` convention,
  - **fail-safe by construction** — every method swallows Redis errors and logs
    at ``debug``; a Redis outage must never turn a request into a 5xx. The write
    path returns silently; the read path returns zeros.

Two keyspaces per stage:
  - ``archimedes:funnel:total:<stage>`` — all-time distinct visitors (no TTL).
  - ``archimedes:funnel:day:<YYYY-MM-DD>:<stage>`` — per-day distinct visitors,
    with a TTL so old day-buckets self-expire (no unbounded growth).

Issue #788 — agent-vs-human breakdown: alongside the two keyspaces above, each
``record()`` call additionally tags the same visitor into a per-``agent_type``
HLL (``...:<stage>:<agent_type>``, same TTL rules) whenever the caller has a
classified ``agent_type`` (see ``api/telemetry_middleware.py``). This is
purely additive — the stage-only aggregate is written and read exactly as
before, so it stays the source of truth for every existing consumer. The
breakdown lets the funnel measure agent conversion separately from human.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

_PREFIX = "archimedes:funnel"

# Ordered funnel stages. Order is load-bearing: ratios are computed against the
# first stage (``landed``) and against the immediately preceding stage.
#
# **The order changed with the free path (#1643), because the journey did.**
# Until 2026-08-31 a wallet was required before the FIRST generation, so
# ``wallet_connected`` genuinely preceded ``generation_started`` (the post-#851
# order). Now the first three generations need only an account, so a visitor
# generates first and meets the wallet gate afterwards. Leaving the old order
# in place would have published a ``step_conversion`` computed against a stage
# that no longer precedes its successor — a number that reads as a conversion
# rate and measures nothing.
#
#   landed              — the SPA's JS beacon fired
#   generation_started  — a generation actually queued (free or paid)
#   free_generation_used— …and it was spent from the account's free allowance
#   wallet_gate_shown   — the allowance ran out; 409 wallet_link_required
#   wallet_connected    — a wallet was linked after seeing that gate
#   vault_deployed      — roadmap (ROADMAP_SURFACES_ENABLED), still tracked
STAGES: tuple[str, ...] = (
    "landed",
    "generation_started",
    "free_generation_used",
    "wallet_gate_shown",
    "wallet_connected",
    "vault_deployed",
)

# Stages a browser client is allowed to self-report via the beacon endpoint.
# Only the top of funnel is client-emittable; every downstream stage is recorded
# server-side at the authoritative transition so a client can't inflate them.
CLIENT_EMITTABLE_STAGES: frozenset[str] = frozenset({"landed"})

# The closed set of classifier verdicts (#788). Mirrors the return values of
# ``api.telemetry_middleware.classify_request`` — duplicated here rather than
# imported to keep this services-layer module independent of the api layer.
# An agent_type outside this set is treated like an unknown stage: no-op on
# write, not a bogus key.
#
# ``keyed`` joined the set with the scoped API-key lane (#1653 decision D3): a
# caller authenticated by ``Authorization: Bearer archim_…``. It is deliberately
# NOT folded into ``external`` — ``external`` is a User-Agent *guess* about an
# unauthenticated client, while ``keyed`` is a credential minted for machine use,
# and merging them would put the only high-confidence agent signal we have back
# into a bucket of heuristics. Because an unrecognised type silently no-ops on
# write (see ``record``), forgetting this line would have made keyed traffic
# vanish from the breakdown rather than fail loudly — which is why the classifier
# module names this file as one of the four sites that move together.
AGENT_TYPES: tuple[str, ...] = ("internal", "keyed", "external", "human")

# Per-day buckets self-expire after this window (90 days of trend history).
_DAY_TTL_SECONDS = 90 * 24 * 60 * 60


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


class FunnelStore:
    """Thin, fail-safe Redis wrapper for the conversion-funnel HLL counters."""

    def __init__(self, url: str | None = None) -> None:
        self._url = url or REDIS_URL
        self._redis: aioredis.Redis | None = None

    async def _get_redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = aioredis.from_url(self._url, decode_responses=True)
        return self._redis

    # ─── Record (write path — server-side emit + client beacon) ──────────

    async def record(self, stage: str, visitor_id: str, agent_type: str | None = None) -> None:
        """Record that ``visitor_id`` reached ``stage``. Never raises.

        No-ops on an unknown stage or an empty visitor id (defensive — a missing
        id must not create a bogus distinct count).

        ``agent_type`` (#788), when it's one of ``AGENT_TYPES``, additionally
        tags the same visitor into a per-agent_type HLL so agent conversion can
        be measured separately from human. Omitted or unrecognized values just
        skip the extra tagging — the stage-only aggregate above is unaffected,
        so a legacy caller (or one whose request never got classified) records
        exactly as it did before this parameter existed.
        """
        if stage not in STAGES or not visitor_id:
            return
        try:
            r = await self._get_redis()
            # One _today() call for both buckets: two calls straddling a UTC
            # midnight would file the aggregate and the agent_type split of
            # the SAME record under different days (review follow-up).
            today = _today()
            day_key = f"{_PREFIX}:day:{today}:{stage}"
            pipe = r.pipeline()
            pipe.pfadd(f"{_PREFIX}:total:{stage}", visitor_id)
            pipe.pfadd(day_key, visitor_id)
            pipe.expire(day_key, _DAY_TTL_SECONDS)
            if agent_type in AGENT_TYPES:
                at_day_key = f"{_PREFIX}:day:{today}:{stage}:{agent_type}"
                pipe.pfadd(f"{_PREFIX}:total:{stage}:{agent_type}", visitor_id)
                pipe.pfadd(at_day_key, visitor_id)
                pipe.expire(at_day_key, _DAY_TTL_SECONDS)
            await pipe.execute()
        except Exception as exc:
            # Fail-safe: a Redis outage must never break the request it measures.
            logger.debug("funnel record failed for stage %s: %s", stage, exc)

    # ─── Read (exposure path — GET /api/metrics/funnel) ──────────────────

    async def get_totals(self) -> dict[str, int]:
        """All-time distinct visitors per stage. Returns zeros on error."""
        return await self._counts(f"{_PREFIX}:total:{{stage}}")

    async def get_day(self, date_str: str | None = None) -> dict[str, int]:
        """Per-day distinct visitors per stage (defaults to today). Zeros on error."""
        day = date_str or _today()
        return await self._counts(f"{_PREFIX}:day:{day}:{{stage}}")

    async def _counts(self, key_template: str) -> dict[str, int]:
        counts = dict.fromkeys(STAGES, 0)
        try:
            r = await self._get_redis()
            pipe = r.pipeline()
            for stage in STAGES:
                pipe.pfcount(key_template.format(stage=stage))
            results = await pipe.execute()
            for stage, count in zip(STAGES, results, strict=False):
                counts[stage] = int(count or 0)
        except Exception as exc:
            logger.debug("funnel read failed: %s", exc)
        return counts

    # ─── Read — agent_type breakdown (#788) ───────────────────────────────

    async def get_totals_by_agent_type(self) -> dict[str, dict[str, int]]:
        """All-time distinct visitors per stage, broken out by agent_type. Zeros on error."""
        return await self._counts_by_agent_type(f"{_PREFIX}:total:{{stage}}:{{agent_type}}")

    async def get_day_by_agent_type(self, date_str: str | None = None) -> dict[str, dict[str, int]]:
        """Per-day distinct visitors per stage, broken out by agent_type (defaults to today). Zeros on error."""
        day = date_str or _today()
        return await self._counts_by_agent_type(f"{_PREFIX}:day:{day}:{{stage}}:{{agent_type}}")

    async def _counts_by_agent_type(self, key_template: str) -> dict[str, dict[str, int]]:
        counts: dict[str, dict[str, int]] = {stage: dict.fromkeys(AGENT_TYPES, 0) for stage in STAGES}
        try:
            r = await self._get_redis()
            pipe = r.pipeline()
            pairs = [(stage, agent_type) for stage in STAGES for agent_type in AGENT_TYPES]
            for stage, agent_type in pairs:
                pipe.pfcount(key_template.format(stage=stage, agent_type=agent_type))
            results = await pipe.execute()
            for (stage, agent_type), count in zip(pairs, results, strict=False):
                counts[stage][agent_type] = int(count or 0)
        except Exception as exc:
            logger.debug("funnel agent-type read failed: %s", exc)
        return counts

    # ─── Lifecycle ───────────────────────────────────────────────────────

    async def close(self) -> None:
        if self._redis:
            try:
                await self._redis.aclose()
            except Exception as exc:
                logger.debug("funnel store close failed: %s", exc)
            self._redis = None
