"""Telemetry data models — human-vs-agent traction counter (issue #428).

The traction counter is the hackathon win-condition instrument: it measures
how much of the live traffic is *humans* (browser sessions) vs *agents*
(internal agent runner + external bots/scripts) so the "agents make markets"
narrative is backed by a real, live number rather than a claim.

Identity model:
  - HUMAN  = resolved Better Auth account or browser request.
  - AGENT  = a valid ``X-Internal-Agent-Key`` header (internal), OR no session
             plus a non-browser User-Agent (external bot/script).
  - Default (browser UA, no session) = HUMAN — the demo is open, so an
    un-signed-in browser visitor still counts as a human.

Only response *shapes* live here; classification logic lives in the telemetry
middleware and the counter persistence lives in ``services/telemetry_store.py``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class MetricsResponse(BaseModel):
    """Public traction counter — humans vs agents over the live deployment.

    Served by ``GET /api/metrics``. The ``*_count`` / ``total_requests`` fields are
    **cumulative per-request tallies (site traffic, NOT users, NOT visitors)** —
    lifetime totals since ``epoch_started_at``. ``real_users`` is the honest
    canonical account count from Better Auth ``auth_users``
    surfaced adjacent so the traffic tallies can never be read as a user count
    (issue #830, superseded by #1028 D1/AC1 — see ``services/user_stats.py``).

    **D8 (issue #1028):** these request tallies are retained (real traffic,
    worth keeping) but are now snapshotted to Postgres on every read
    (``services/request_snapshot_store.py``), so a Redis restart on the box
    does not zero them — Redis is the fast live counter, Postgres is the
    durable floor. ``epoch_started_at`` is when durable snapshotting began
    (counts from before that instant are the un-reconcilable fossil
    Redis-only era); ``epoch_resets`` is how many Redis resets have been
    absorbed since without losing the running total.
    """

    human_count: int = Field(..., description="Human-UA REQUESTS (cumulative, site traffic — NOT users).")
    agent_count: int = Field(..., description="Agent/bot REQUESTS (cumulative, site traffic — NOT users).")
    total_requests: int = Field(..., description="human_count + agent_count (cumulative requests — NOT users).")
    real_users: int | None = Field(
        default=None,
        description=(
            "Distinct real users = canonical Better Auth accounts. "
            "Linked-wallet and profile counts are separate metrics. "
            "None (not 0) means the account-count query failed — a loud absence, "
            "never a fabricated measured zero (round 4 fix)."
        ),
    )
    epoch_started_at: str | None = Field(
        default=None,
        description="ISO-8601 UTC timestamp durable (Postgres-snapshotted) request counting began (D8).",
    )
    epoch_resets: int | None = Field(
        default=None,
        description="Count of Redis resets absorbed into the durable total since epoch_started_at (D8).",
    )
    timestamp: str = Field(..., description="ISO-8601 UTC timestamp this snapshot was read.")


class FunnelStageCount(BaseModel):
    """One stage of the conversion funnel (issue #787).

    ``distinct_visitors`` is an HLL estimate of unique visitors that reached this
    stage. ``pct_of_landed`` is this stage vs the top of funnel; ``step_conversion``
    is this stage vs the immediately preceding stage — the drop-off we care about.
    """

    stage: str = Field(..., description="Funnel stage name.")
    distinct_visitors: int = Field(..., description="Distinct visitors that reached this stage (HLL estimate).")
    pct_of_landed: float = Field(..., description="distinct_visitors / landed, as a fraction (0.0-1.0).")
    step_conversion: float = Field(..., description="distinct_visitors / previous-stage count (0.0-1.0).")
    by_agent_type: dict[str, int] = Field(
        default_factory=dict,
        description="distinct_visitors at this stage, broken out by agent_type "
        "('internal'/'keyed'/'external'/'human') so agent conversion can be measured separately from "
        "human (issue #788). 'keyed' is a caller authenticated by a scoped API key (#1653 D3) — an "
        "identity, unlike 'external', which is a User-Agent guess about an unauthenticated client. "
        "Empty for source=identity, which has no per-request agent_type.",
    )


class FunnelResponse(BaseModel):
    """Conversion funnel over the live deployment (issue #787, extended by #1028).

    Served by ``GET /api/metrics/funnel``. Two sources, selected by the
    ``source`` query param:

    - ``source=visitor`` (default, unchanged from #787) — anonymous
      browser-id HLL counts in Redis. Stages: ``landed -> wallet_connected ->
      generation_started -> vault_deployed``. Pre-auth by construction (D2a):
      this is a fossil instrument kept for the top-of-funnel ``landed`` stage,
      which has no wallet to key on.
    - ``source=identity`` (issue #1028, AC3) — distinct HUMAN WALLET counts
      recomputed live from ``identity_events`` alone, no Redis/HLL involved.
      Stages: ``wallet_connected -> generation_started -> vault_deployed``
      (mapped from the ledger's ``auth_verified`` / ``generation_started`` /
      ``vault_created`` event types). ``human_only=True`` (default) applies
      AC3's exact exclusion (``actor_class NOT IN ('operator_dogfood',
      'agent')``) so dogfooding/agent traffic doesn't inflate the honest
      human funnel.
    """

    window: str = Field(..., description='"all-time" or the ISO date (YYYY-MM-DD) the counts are for.')
    source: str = Field(default="visitor", description='"visitor" (Redis/HLL, fossil) or "identity" (ledger, honest).')
    stages: list[FunnelStageCount] = Field(..., description="Ordered funnel stages with counts + ratios.")
    timestamp: str = Field(..., description="ISO-8601 UTC timestamp this snapshot was read.")


class WalletIdentityOut(BaseModel):
    """One anchored human wallet (issue #1028 AC1)."""

    wallet_address: str = Field(..., description="Lowercased cryptographically verified wallet address.")
    actor_class: str = Field(
        ...,
        description="Always 'human' on this endpoint (see the /api/metrics/private/wallets filter — admin-gated since #1366).",
    )
    first_seen_at: str = Field(..., description="ISO-8601 UTC — first time this wallet was anchored.")
    last_auth_at: str | None = Field(default=None, description="ISO-8601 UTC — most recent wallet proof.")


class WalletsResponse(BaseModel):
    """Legacy enumerable verified-wallet list; separate from canonical account count."""

    real_users: int = Field(..., description="Legacy field: verified human-wallet count, not account count.")
    wallets: list[WalletIdentityOut] = Field(..., description="The wallets behind that count, oldest-first.")
    timestamp: str = Field(..., description="ISO-8601 UTC timestamp this snapshot was read.")


class WalletConnectionOut(BaseModel):
    """One wallet's first cryptographic proof event."""

    wallet: str = Field(..., description="Lowercased cryptographically verified wallet address.")
    connected_at: str = Field(..., description="ISO-8601 UTC — min(occurred_at) over its auth_verified events.")


class WalletConnectionsResponse(BaseModel):
    """ "Which wallets connected, and when" — issue #1028 AC2, impossible before the ledger."""

    count: int = Field(..., description="Number of distinct wallets with cryptographic proof events.")
    connections: list[WalletConnectionOut] = Field(..., description="Earliest-first.")
    timestamp: str = Field(..., description="ISO-8601 UTC timestamp this snapshot was read.")


class FunnelEventRequest(BaseModel):
    """Client beacon body for ``POST /api/metrics/funnel/event`` (issue #787).

    Only top-of-funnel stages are client-emittable (see
    ``services.funnel_store.CLIENT_EMITTABLE_STAGES``); every downstream stage is
    recorded server-side at its authoritative transition so a client can't
    inflate it.
    """

    stage: str = Field(..., description="The funnel stage the browser is reporting (e.g. 'landed').")


class CountryCount(BaseModel):
    """Distinct human visitors from one country (issue #787)."""

    code: str = Field(..., description="ISO-3166 alpha-2 country code, or 'ZZ' when unknown.")
    distinct_visitors: int = Field(..., description="Distinct human visitors from this country (HLL estimate).")


class VisitorInsightsResponse(BaseModel):
    """Where our (un-promoted) human traffic comes from + on what device (issue #787).

    Served by ``GET /api/metrics/visitors``. Counts are distinct HUMAN visitors
    (agents/crawlers excluded) keyed on the anonymous visitor id. Country comes
    from CloudFront's viewer-country geolocation; device from CloudFront device
    headers (UA fallback).
    """

    window: str = Field(..., description='"all-time" snapshot window.')
    countries: list[CountryCount] = Field(..., description="Countries, sorted by distinct visitors (desc).")
    devices: dict[str, int] = Field(
        ..., description="Distinct visitors per device class (mobile/tablet/desktop/tv, or 'unknown' if unclassified)."
    )
    timestamp: str = Field(..., description="ISO-8601 UTC timestamp this snapshot was read.")
