"""Private metrics routes — the admin-only cost/ops dashboard.

The public ``metrics_router`` (``/api/metrics``, ``/funnel``, ``/visitors``) is
PII-free by design and stays anonymous — it is the "agents make markets" traction
instrument. This router is the OTHER half of the split: the internal cost / ops /
infra dashboard (the ARCH-COST-DASHBOARDS content), which must NOT be public.

Gating requires a Better Auth account that is a platform admin. Cost/ops data
(Bedrock spend, infra spend, cost-per-user) is operationally sensitive, not
merely PII — "has a linked wallet" is not an appropriate bar for it.
A signed-in non-admin gets **403**; an unauthenticated request gets **401**
(the session check runs first).

**Admin is keyed on the ACCOUNT, not on the request's wallet (#1648).** The
gate previously depended on ``require_linked_wallet``, which resolves the
caller's wallet from the ``X-Wallet-Address`` header — and ``ui/src/api.js``
sends that header from whatever the browser extension has selected in that
tab. Admin visibility therefore followed the browser rather than the account:
the same signed-in owner saw Insights in one browser and a not-found page in
the next. ``services/platform_admin.resolve_platform_admin`` now answers from
server-side state only — the ``PLATFORM_ADMIN_ACCOUNTS`` allowlist (canonical
``auth_users.id``/email) and the account's OWN linked-wallet set intersected
with ``PLATFORM_ADMIN_WALLETS``. The wallet is *evidence*, never the lookup
key, and the request header is not read on this path at all: it can neither
grant admin (header spoofing) nor revoke it (the reported bug). See that
module for the migration story and
``backend/tests/test_platform_admin_gate.py`` for both guard demos.

Account denominator comes from canonical Better Auth users, never cumulative
request tallies or optional linked-wallet/profile counts.

The account-level AWS cost fields are DRAFT/illustrative placeholders (the live
Bedrock/infra billing wiring — AWS Cost Explorer + Bedrock token metering — is
roadmap work); they are labelled ``draft`` so a reader can't mistake them for
metered live spend. ``cost_per_generation_usd`` is the exception as of #1217:
it is derived from real ``generation_costs`` measurements priced against the
``GENERATION_COST_RATE_CARD`` env rate card (``services/generation_cost_rollup``),
and carries its own provenance rather than sharing the ``draft`` label.
Real distinct users are read live so the per-user denominators are honest.

Owner directive (2026-08-20, SUPERSEDES issue #1028 D8 "public Insights page"):
``/app/insights`` moved from the public app surface to ADMIN-ONLY. It remains
the owner traction dashboard and gained new current-schema engagement/adoption
tiles (``GET /whoami``, ``GET /engagement`` below); the public aggregate
endpoints on ``metrics_router`` (``/api/metrics``, ``/funnel``, ``/visitors``)
are UNCHANGED and stay public — only the Insights *page* and these new
per-entity admin endpoints are gated. ``/whoami`` is a server-truth gate probe:
the frontend calls it before rendering anything Insights-shaped so a
non-admin/anonymous visitor gets the exact "page does not exist" treatment
(``ui/src/routes.js``'s not-found handling), not a page that discloses "you
need admin access" — which would itself advertise the page's existence.
This closes only that one disclosure vector (the denial screen's wording),
not concealment of the endpoint or route in general — a non-admin's browser
still calls this probe on every app page and still ships the gated route
and component in its main JS bundle; see ``ui/src/adminProbe.js``'s "Scope
of 'does not advertise the page exists'" note (round 2, 2026-08-20).
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException

from archimedes.api.account_auth import CurrentUser, require_current_user
from archimedes.models.telemetry import (
    WalletConnectionOut,
    WalletConnectionsResponse,
    WalletIdentityOut,
    WalletsResponse,
)
from archimedes.services.engagement_metrics import get_engagement_snapshot
from archimedes.services.generation_cost_rollup import get_measured_generation_cost
from archimedes.services.identity_metrics import (
    count_human_wallets,
    list_human_wallets,
    list_wallet_connections,
)
from archimedes.services.platform_admin import AdminGrant, resolve_platform_admin
from archimedes.services.user_stats import get_distinct_user_count


def require_platform_admin(user: CurrentUser = Depends(require_current_user)) -> AdminGrant:
    """Require a canonical account that is a platform admin (#1648).

    401 with no session (raised by ``require_current_user``, which runs
    first); 403 for any signed-in account that is not an admin. Cost/ops data
    must not be readable by "anyone with a linked wallet" — only platform
    admins.

    The account is the key. This dependency takes ``CurrentUser`` and passes
    it to ``services/platform_admin.resolve_platform_admin``, which reads only
    server-side state (the ``PLATFORM_ADMIN_ACCOUNTS`` allowlist and the
    account's own linked-wallet set). It deliberately does NOT depend on
    ``require_linked_wallet`` any more: that helper resolves the wallet the
    *request* claims to be acting as, from a client-supplied header, which
    made admin status a function of the browser's currently-connected wallet
    rather than of who was signed in. Both failure directions are guarded in
    ``backend/tests/test_platform_admin_gate.py``.

    The two pre-#1648 403 flavours ("A verified linked wallet is required" vs
    "Admin access required.") collapse into the second one: with the account
    as the key, "you have no linked wallet" is no longer a precondition that
    can fail on its own — an account listed in ``PLATFORM_ADMIN_ACCOUNTS`` is
    an admin with no wallet at all. One message also discloses less.
    """
    grant = resolve_platform_admin(user)
    if grant is None:
        raise HTTPException(status_code=403, detail="Admin access required.")
    return grant


# Every route requires a signed-in account with platform-admin status
# (403 for a signed-in non-admin, 401 for no session at all).
metrics_private_router = APIRouter(
    prefix="/api/metrics/private",
    tags=["metrics", "private"],
    dependencies=[Depends(require_platform_admin)],
)


@metrics_private_router.get("/whoami")
async def get_whoami(grant: AdminGrant = Depends(require_platform_admin)) -> dict:
    """Admin-gate probe for the frontend — the server-truth half of the gate.

    The UI calls this on entry to the Insights page (and to decide whether to
    render the Ops nav item) BEFORE rendering anything Insights-shaped.
    Anonymous → 401; signed-in non-admin → 403; admin → 200
    ``{admin: true, wallet}``. There is no "admin: false" 200 response —
    non-admin is always a 4xx, so the frontend's error branch is the only
    path that ever needs to fall back to the not-found treatment, and a
    network/parse failure degrades the same way (fail closed, not open).

    ``wallet`` is the account's allowlisted linked wallet — the EVIDENCE for
    the grant, reported for provenance, never the address the caller's header
    named. It is ``null`` for an account granted admin by
    ``PLATFORM_ADMIN_ACCOUNTS`` with no allowlisted wallet linked (#1648): an
    honest absence rather than a substituted address. The response shape is
    otherwise unchanged, and ``ui/src/adminProbe.js`` already reads only
    ``.admin`` for the gate decision (``ui/src/insightsGate.js``).
    """
    return {"admin": True, "wallet": grant.wallet}


@metrics_private_router.get("/engagement")
async def get_engagement(grant: AdminGrant = Depends(require_platform_admin)) -> dict:
    """Dashboard v2 — current-schema engagement/adoption tiles (admin-only).

    See ``services/engagement_metrics.py`` for the per-tile query docs and
    what is/isn't joinable today; the PR that introduced this endpoint carries
    the Phase 2 list of metrics deferred pending schema-relations work.
    """
    snapshot = get_engagement_snapshot()
    # Provenance only: the account's allowlisted linked wallet, or null when
    # the grant came from PLATFORM_ADMIN_ACCOUNTS with none linked (#1648).
    snapshot["authenticated_wallet"] = grant.wallet
    return snapshot


@metrics_private_router.get("/cost")
async def get_private_cost(grant: AdminGrant = Depends(require_platform_admin)) -> dict:
    """Account-gated cost/ops dashboard. Anonymous → 401; signed-in
    non-admin → 403.

    **Two provenances on one payload, and they are labelled separately (#1217).**
    The account-level AWS spend fields (``bedrock_*``, ``infra_monthly_usd``,
    ``cost_per_user_usd``) are still DRAFT placeholders pending the live billing
    wiring (roadmap: AWS Cost Explorer + Bedrock token metering) — that is what
    the top-level ``source: "draft"`` describes, and it is deliberately
    unchanged. ``cost_per_generation_usd`` is no longer one of them: it is now
    derived from the ``generation_costs`` measurements this platform actually
    recorded, priced against the ``GENERATION_COST_RATE_CARD`` environment rate
    card, with the full distribution and the N-scaling breakdown under
    ``generation_cost``.

    It stays ``None`` — never ``0`` — when no rate card is configured or no
    measured run was priceable, and ``generation_cost.rate_card_configured`` /
    ``.unpriceable_reasons`` say which. A null here means "not measured or not
    priceable", exactly as before; what changed is that it can now also be a
    real, measured number.

    ``real_users`` is read live so any per-user figure a consumer computes is
    anchored to the honest distinct-user denominator, never the request counter.

    This is the ONLY surface carrying the priced figure, and it is admin-gated:
    the public ``/api/metrics`` family stays aggregate, PII-free and unpriced.
    """
    real_users = get_distinct_user_count()
    generation_cost = get_measured_generation_cost()
    measured = generation_cost.get("cost_per_generation_usd") or {}
    return {
        # Describes the AWS-billing placeholders below, NOT generation_cost —
        # which carries its own provenance (rate_card_configured / jobs_priced /
        # unavailable) precisely so the two are never read as one claim.
        "source": "draft",
        "real_users": real_users,
        "bedrock_monthly_usd": None,
        "bedrock_daily_usd": None,
        "infra_monthly_usd": None,
        "cost_per_user_usd": None,
        # The mean of the priced runs, or None. Kept flat for the existing
        # consumer contract; read `generation_cost` for the distribution, the
        # LLM-vs-compute split, and how many runs could not be priced.
        "cost_per_generation_usd": measured.get("mean"),
        "generation_cost": generation_cost,
        "note": (
            "source='draft' applies to the bedrock_*/infra/cost_per_user placeholders only. "
            "cost_per_generation_usd is measured: generation_costs rows priced against the "
            "GENERATION_COST_RATE_CARD env rate card (null, never 0, when unset or unpriceable). "
            "Per-user figures must be derived from real_users (canonical accounts) — never the "
            "cumulative request tallies on /api/metrics (issue #830)."
        ),
        # Provenance only (#1648): the account's allowlisted linked wallet, or
        # null for a PLATFORM_ADMIN_ACCOUNTS grant with none linked.
        "authenticated_wallet": grant.wallet,
        "timestamp": datetime.now(UTC).isoformat(),
    }


@metrics_private_router.get("/wallets", response_model=WalletsResponse)
async def get_wallets() -> WalletsResponse:
    """Enumerate legacy verified human wallets, separate from account count.

    ADMIN-ONLY, moved here from the public metrics router (#1366): this returns
    the full per-wallet roster — wallet addresses are pseudonymous but
    permanently linkable to on-chain activity, so an open enumeration endpoint
    let anyone list every user of the platform and trace their chain history.
    The public metrics surface is aggregate and PII-free BY DESIGN (module
    docstring above); a per-identity roster belongs behind the same admin gate
    as the rest of the ops dashboard. Anonymous → 401; verified non-admin → 403
    (router-level dependency).

    ``real_users`` field is retained for response compatibility but means wallet
    count on this endpoint only (#1028 AC1). Fail-safe: empty list / zero on DB
    error.
    """
    return WalletsResponse(
        real_users=count_human_wallets(),
        wallets=[WalletIdentityOut(**row) for row in list_human_wallets()],
        timestamp=datetime.now(UTC).isoformat(),
    )


@metrics_private_router.get("/wallets/connections", response_model=WalletConnectionsResponse)
async def get_wallet_connections() -> WalletConnectionsResponse:
    """ "Which wallets connected, and when" — issue #1028 AC2.

    ADMIN-ONLY, moved here from the public metrics router for the same reason
    as ``/wallets`` above (#1366): a per-wallet first-connection ledger is
    identity data, not aggregate traction.

    ``SELECT wallet, min(occurred_at) FROM identity_events WHERE event_type =
    'auth_verified' GROUP BY wallet`` — a query that was impossible before the
    ledger (the SIWE-verify path used to discard the wallet into a stateless
    cookie with no durable write). Fail-safe: an empty list on any DB error.
    """
    connections = list_wallet_connections()
    return WalletConnectionsResponse(
        count=len(connections),
        connections=[WalletConnectionOut(**row) for row in connections],
        timestamp=datetime.now(UTC).isoformat(),
    )
