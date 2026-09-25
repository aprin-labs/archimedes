"""``GET /api/account/usage`` — the CLI's ``meter`` command backend (#1305).

Reports the caller's daily generation-quota usage against the SAME two Redis
buckets ``services/generation_quota.py`` enforces (``GenerationQuota.peek`` —
a read, never a write — added there for exactly this route; enforcement in
``enforce_generation_quota`` is untouched), plus the current price quote from
``generation_payment.quote()``. Both the enforcement path
(``POST /api/generate/start``) and this display path read the price from the
same ``quote()`` call, so the CLI's number and the API's number can never
drift apart.

Also reports the account's LIFETIME free-generation allowance (#1643) —
``free_generations_allowance`` / ``free_generations_remaining`` — read from
``services/free_generations.py``, the same module the gate in
``generate_routes.start_generation`` claims slots from. That is a different
axis from the daily caps above and the response keeps them visibly separate:
the caps reset every UTC day, the allowance never does.

``free_generations_locked_reason`` is the third fact in that group and the
newest (owner decision D1, 2026-08-31): the allowance unlocks on a VERIFIED
email. It is deliberately not folded into the remaining count — "3 slots, not
yet unlocked" and "0 slots left" are different situations with different
answers, and only the pair distinguishes them.

Account-session-gated (Better Auth, ``require_current_user``) — mirrors
``paper_routes.py``'s per-route ``Depends`` style, not the router-level
``dependencies=[...]`` style some other routers use in ``main.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from archimedes.api.account_auth import CurrentUser, require_current_user
from archimedes.services import free_generations, generation_payment
from archimedes.services.generation_quota import (
    GenerationQuota,
    client_ip,
    ip_daily_cap,
    user_daily_cap,
)

account_usage_router = APIRouter(prefix="/api/account", tags=["account"])


class DailyCapUsage(BaseModel):
    """One bucket's usage today. ``used`` is honestly ``None`` — never a
    fabricated ``0`` — when the quota backend could not be reached."""

    used: int | None = None
    cap: int
    unlimited: bool
    remaining: int | None = None
    error: str | None = None


class AccountUsageResponse(BaseModel):
    date: str  # UTC day, YYYY-MM-DD — the same day-bucket key generation_quota uses
    user_id: str
    user: DailyCapUsage
    ip: DailyCapUsage
    quote: dict

    # ─── Free generations (#1643) — a DIFFERENT axis from the caps above ───
    # ``user``/``ip`` are rolling DAILY volume caps that reset every day.
    # These two are the account's LIFETIME free allowance: how many
    # generations it may run before a wallet is required at all. Both apply,
    # and neither substitutes for the other — a caller with free generations
    # left is still refused 429 once the daily cap is hit.
    free_generations_allowance: int

    #: ``None`` — never a fabricated ``0`` or ``3`` — when the ledger could not
    #: be read, same honesty rule as ``DailyCapUsage.used``. A ``0`` here would
    #: tell a brand-new account it has nothing left; the allowance number would
    #: promise free runs the gate may refuse.
    free_generations_remaining: int | None = None
    free_generations_error: str | None = None

    #: Why the allowance above cannot be spent right now, or ``None`` when it
    #: can. ``"email_unverified"`` is the only value today (owner decision D1,
    #: 2026-08-31): the free tier unlocks on a verified email, not on account
    #: creation alone. Reported as a SEPARATE field rather than folded into
    #: ``free_generations_remaining`` because the two facts are different: an
    #: unverified fresh account genuinely has 3 unspent slots waiting for it,
    #: so a ``0`` here would be as false as pretending they are spendable now.
    #: A client renders the pair: locked + a positive remaining is the
    #: "verify your email to unlock these" state, not the "you are out" state.
    free_generations_locked_reason: str | None = None


def _bucket(used: int | None, cap: int) -> DailyCapUsage:
    unlimited = cap <= 0
    if used is None:
        return DailyCapUsage(used=None, cap=cap, unlimited=unlimited, remaining=None, error="quota_backend_unavailable")
    remaining = None if unlimited else max(0, cap - used)
    return DailyCapUsage(used=used, cap=cap, unlimited=unlimited, remaining=remaining, error=None)


@account_usage_router.get("/usage", response_model=AccountUsageResponse)
async def get_account_usage(
    request: Request,
    user: CurrentUser = Depends(require_current_user),
):
    """Today's generation usage vs both daily caps, the lifetime free-generation
    allowance (#1643), and the live price quote."""
    quota = GenerationQuota()
    try:
        user_used = await quota.peek("user", user.id)
        ip_used = await quota.peek("ip", client_ip(request))
    finally:
        await quota.close()

    # Reads the SAME ledger the gate in ``generate_routes.start_generation``
    # claims from, through the same service — so the number shown here and the
    # number the gate enforces cannot drift apart, exactly as ``quote()`` above
    # is shared with the paywall rather than re-derived.
    free_remaining = free_generations.remaining(user.id)

    return AccountUsageResponse(
        date=datetime.now(UTC).strftime("%Y-%m-%d"),
        user_id=user.id,
        user=_bucket(user_used, user_daily_cap()),
        ip=_bucket(ip_used, ip_daily_cap()),
        quote=generation_payment.quote(),
        free_generations_allowance=free_generations.allowance(),
        free_generations_remaining=free_remaining,
        free_generations_error=(None if free_remaining is not None else "free_generation_backend_unavailable"),
        # The same predicate the gate applies (services/free_generations.py),
        # against the same `email_verified` the gate reads — so the reason the
        # UI shows and the reason the 409 gives cannot drift apart.
        free_generations_locked_reason=free_generations.locked_reason(email_verified=user.email_verified),
    )
