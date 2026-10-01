"""Canonical account count used by metrics and health surfaces.

Better Auth ``auth_users`` is source of truth. Wallet/profile counts are separate
metrics.

**Two read shapes, deliberately (round 4 fix).** :func:`get_distinct_user_count`
fails soft to ``0`` on a DB error — correct for callers like ``/health`` and
``/api/metrics/private/cost`` that need a plain ``int`` and treat this as
operational telemetry, not a claim shown to a human as a measured fact.
:func:`get_distinct_user_count_or_none` fails to ``None`` instead — for any
caller that DISPLAYS the value as a measured number (``GET /api/metrics``'s
``real_users``, rendered on ``Insights.jsx`` as "Real users (accounts)"). The
plain-``0`` variant used to be the ONLY variant, which meant a genuine DB
outage during ``/api/metrics`` rendered as "0 real users" — a plausible,
measured-looking number indistinguishable from an actually-empty user table,
exactly the fail-soft violation CLAUDE.md's "claims must be true" section
warns against, and the same class of bug ``services/engagement_metrics.py``'s
round-2 fix already closed for the adjacent "Accounts (total)" tile.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# Small in-process TTL cache. ``get_distinct_user_count`` is called from the
# highly-polled ``/health`` + ``/api/metrics`` endpoints, and account count is
# a slowly-changing stat — caching it for a few seconds keeps frequent polling
# from running a Postgres COUNT on every request. Per-process and fail-safe: only a
# SUCCESSFUL read (a genuine count, including a real 0) is cached; a query error is
# NOT cached (the next call retries) and still returns 0.
#
# Held in a MUTATED-IN-PLACE dict (never reassigned) so there is no module global to
# rebind — ``"ts" == 0`` means nothing is cached yet.
_CACHE_TTL_SECONDS = 30.0
_cache: dict[str, float] = {"value": 0.0, "ts": 0.0}


def _query_distinct_user_count() -> int | None:
    """Count canonical Better Auth users, distinguishing failure from real zero."""
    try:
        from sqlalchemy import func

        from archimedes.db import get_session
        from archimedes.models.account import PLATFORM_LEGACY_USER_ID, AuthUser

        session = get_session()
        try:
            # The #1283 legacy-adoption account is a bookkeeping owner, not a
            # user — counting it would overstate this number by one forever.
            return int(
                session.query(func.count(AuthUser.id)).filter(AuthUser.id != PLATFORM_LEGACY_USER_ID).scalar() or 0
            )
        finally:
            session.close()
    except Exception as exc:
        logger.debug("distinct user count read failed: %s", exc)
        return None


def get_distinct_user_count_or_none() -> int | None:
    """Canonical Better Auth account count, or ``None`` if the read failed.

    The honest variant (round 4 fix): ``None`` is a loud, visible absence a
    caller can render as "—", never a fabricated measured zero. Shares the
    same TTL cache as :func:`get_distinct_user_count` — only a genuine
    successful read (including a real ``0``) is cached; a query error is
    never cached, so the next call retries.
    """
    now = time.monotonic()
    if _cache["ts"] and (now - _cache["ts"]) < _CACHE_TTL_SECONDS:
        return int(_cache["value"])
    result = _query_distinct_user_count()
    if result is None:
        return None  # query error → don't cache; the next call retries
    _cache["value"] = result
    _cache["ts"] = now
    return result


def get_distinct_user_count() -> int:
    """Return canonical Better Auth account count, cached after successful reads.

    Fails soft to ``0`` on a DB error — the legacy shape, kept for callers
    (``/health``, ``/api/metrics/private/cost``) that need a plain ``int`` and
    treat this as operational telemetry rather than a measured fact shown to
    a human. Anywhere the value is DISPLAYED as a measured number, use
    :func:`get_distinct_user_count_or_none` instead so a DB outage renders as
    an honest absence, not a plausible zero (round 4 fix — see the module
    docstring).
    """
    result = get_distinct_user_count_or_none()
    return 0 if result is None else result


def _reset_cache() -> None:
    """Clear the TTL cache. Test hook so cached state can't leak between tests."""
    _cache["value"] = 0.0
    _cache["ts"] = 0.0
