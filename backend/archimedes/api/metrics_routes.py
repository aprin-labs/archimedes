"""Metrics routes — public traction counter + honest identity-ledger views (issue #428, #1028).

Exposes ``GET /api/metrics`` with the live cumulative human/agent request
counts maintained by the telemetry middleware, now Postgres-snapshotted (D8)
so a Redis restart doesn't zero them. This is the read side of the traction
instrument; the write side is ``api/telemetry_middleware.py`` +
``services/telemetry_store.py`` (live Redis counters) plus
``services/request_snapshot_store.py`` (the durable Postgres floor).

``real_users`` now reads canonical Better Auth accounts. The identity-ledger
roster views (per-wallet enumeration + connection events) live on the ADMIN
router in ``metrics_private_routes.py`` (#1366 — this public router is
aggregate and PII-free by design; no route here may carry a wallet-bearing
response model, and a test walks the router to enforce that). The funnel keeps
its ``source=identity`` mode, which recomputes distinct-wallet conversion from
``identity_events`` alone (AC3) as an aggregate — see
``services/identity_metrics.py`` for the queries themselves.

# TODO(T-observability): Phase 2 — Prometheus ``/metrics`` exposition endpoint
# and an arc-canteen async task that mirrors these counts onto the traction
# dashboard. Intentionally out of scope for this app-layer MVP to keep it tight.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Query, Request

from archimedes.api.funnel_middleware import record_funnel
from archimedes.api.visitor_insights import record_visitor_insight
from archimedes.models.telemetry import (
    CountryCount,
    FunnelEventRequest,
    FunnelResponse,
    FunnelStageCount,
    MetricsResponse,
    VisitorInsightsResponse,
)
from archimedes.services.funnel_store import CLIENT_EMITTABLE_STAGES, STAGES, FunnelStore
from archimedes.services.identity_metrics import get_identity_funnel
from archimedes.services.request_snapshot_store import get_last_snapshot_totals, record_and_get_totals
from archimedes.services.telemetry_store import TelemetryStore
from archimedes.services.user_stats import get_distinct_user_count_or_none
from archimedes.services.visitor_insights_store import VisitorInsightsStore

metrics_router = APIRouter(prefix="/api", tags=["metrics"])

# Maps identity_events.event_type -> the funnel stage label the pre-#1028
# visitor funnel already uses (services/funnel_store.STAGES), so the response
# shape (and the frontend's existing stage->label map) is reused rather than
# inventing a parallel vocabulary. Order is load-bearing: ratios are computed
# against the first entry. ``landed`` has no analog here — it's pre-auth and
# has no wallet to key on (D2a keeps that instrument anonymous / Redis-only).
_IDENTITY_STAGE_ORDER: tuple[str, ...] = ("auth_verified", "generation_started", "vault_created")
_IDENTITY_STAGE_LABELS: Mapping[str, str] = {
    "auth_verified": "wallet_connected",
    "generation_started": "generation_started",
    "vault_created": "vault_deployed",
}


def _build_funnel(
    counts: Mapping[str, int],
    window: str,
    *,
    source: str = "visitor",
    stage_order: Sequence[str] = STAGES,
    stage_label: Mapping[str, str] | None = None,
    breakdown: Mapping[str, Mapping[str, int]] | None = None,
) -> FunnelResponse:
    """Turn an ordered stage->count map into a funnel with ratios.

    ``pct_of_landed`` is each stage vs the first stage; ``step_conversion`` is
    each stage vs the previous one. Both default to 0.0 when the denominator is
    zero so the response is always well-formed (no divide-by-zero).

    ``stage_order`` is the raw key sequence to read out of ``counts``;
    ``stage_label`` (optional) renames each key for display in the response
    (e.g. the identity ledger's ``auth_verified`` -> the funnel's existing
    ``wallet_connected`` label) without changing the ratio math, which always
    runs over the raw, ordered counts.

    ``breakdown`` (optional, issue #788): stage -> {agent_type: count}, surfaced
    per-stage as ``by_agent_type``. Omitted (e.g. the ``source=identity`` path,
    which has no per-request agent_type) or missing a stage just yields ``{}``
    for that stage.
    """
    labels = stage_label or {}
    first = counts.get(stage_order[0], 0) if stage_order else 0
    stages: list[FunnelStageCount] = []
    prev = first
    for i, stage in enumerate(stage_order):
        n = counts.get(stage, 0)
        pct = (n / first) if first else 0.0
        step = 1.0 if i == 0 else ((n / prev) if prev else 0.0)
        stages.append(
            FunnelStageCount(
                stage=labels.get(stage, stage),
                distinct_visitors=n,
                pct_of_landed=round(pct, 4),
                step_conversion=round(step, 4),
                by_agent_type=dict(breakdown[stage]) if breakdown and stage in breakdown else {},
            )
        )
        prev = n
    return FunnelResponse(window=window, source=source, stages=stages, timestamp=datetime.now(UTC).isoformat())


@metrics_router.get("/metrics", response_model=MetricsResponse)
async def get_metrics() -> MetricsResponse:
    """Return the live human-vs-agent traction counts + the honest user count.

    ``human_count`` / ``agent_count`` / ``total_requests`` are cumulative
    per-request tallies (site traffic, NOT users, NOT visitors — D8). They are
    reconciled against a Postgres snapshot on every read
    (``services/request_snapshot_store.py``) so a Redis restart on the box
    does not zero them; ``epoch_started_at`` / ``epoch_resets`` surface that
    durability honestly instead of implying one unbroken counter.

    ``real_users`` is canonical Better Auth account count (see
    ``services/user_stats.py``), surfaced
    alongside so the request tallies can't be mis-cited as users (issue #830).

    Fail-safe: a Redis outage falls back to the last Postgres snapshot instead
    of reporting a false zero (AC6). ``real_users`` itself uses
    ``get_distinct_user_count_or_none`` (round 4 fix) and stays ``None`` — not
    a fabricated ``0`` — if the account-count query fails, so a DB outage
    renders on ``Insights.jsx`` as an honest "—" rather than a plausible
    "0 real users" indistinguishable from a genuinely empty user table. This
    endpoint always responds 200 with a well-formed shape either way.
    """
    store = TelemetryStore()
    try:
        counts = await store.get_counts_or_none()
    finally:
        await store.close()

    if counts is None:
        # Redis unreachable — the durable Postgres floor is the fallback, not
        # a false zero (D8/AC6: "no Insights number changes beyond sub-30s
        # cache staleness" across a Redis restart).
        totals = get_last_snapshot_totals()
    else:
        human_count, agent_count = counts
        totals = record_and_get_totals(human_count, agent_count)

    real_users = get_distinct_user_count_or_none()

    return MetricsResponse(
        human_count=totals["human_total"],
        agent_count=totals["agent_total"],
        total_requests=totals["human_total"] + totals["agent_total"],
        real_users=real_users,
        epoch_started_at=totals["epoch_started_at"],
        epoch_resets=(totals["epoch_count"] - 1) if totals["epoch_count"] else None,
        timestamp=datetime.now(UTC).isoformat(),
    )


@metrics_router.get("/metrics/funnel", response_model=FunnelResponse)
async def get_funnel(
    day: str | None = Query(
        default=None,
        description="ISO date (YYYY-MM-DD) for a single-day window; omit for the all-time funnel. "
        "Ignored when source=identity (the ledger is queried live, not day-bucketed).",
    ),
    source: Literal["visitor", "identity"] = Query(
        default="visitor",
        description="'visitor' (Redis/HLL, fossil, pre-auth-capable) or 'identity' "
        "(issue #1028 AC3 — recomputed live from identity_events, no Redis involved).",
    ),
    human_only: bool = Query(
        default=True,
        description="source=identity only: exclude actor_class IN ('operator_dogfood','agent') — AC3's exact clause.",
    ),
) -> FunnelResponse:
    """Return the conversion funnel — either the fossil visitor-HLL one or the honest ledger one.

    ``source=visitor`` (default, unchanged behavior): the pre-#1028 anonymous
    browser-id funnel, ``landed -> wallet_connected -> generation_started ->
    vault_deployed``, backed by Redis HyperLogLog (issue #787). Fail-safe:
    ``FunnelStore`` returns zeros when Redis is unreachable.

    ``source=identity`` (issue #1028, AC3): ``wallet_connected ->
    generation_started -> vault_deployed``, each a live ``COUNT(DISTINCT
    wallet)`` over ``identity_events`` — no HLL, no Redis. ``human_only=True``
    (default) applies AC3's ``actor_class NOT IN ('operator_dogfood',
    'agent')`` exclusion so dogfooding/agent traffic can't inflate the honest
    human funnel; pass ``human_only=false`` to see everyone. Fail-safe: an
    all-zero funnel on any DB error.

    Every stage in the ``source=visitor`` response also carries
    ``by_agent_type`` (issue #788): the same distinct-visitor count, broken out
    by the telemetry middleware's classification (``internal``/``keyed``/
    ``external``/``human``), so agent conversion through the funnel can be measured
    separately from human conversion. Additive — ``distinct_visitors`` and the
    ratios are unchanged. Empty per stage on ``source=identity`` (no
    per-request agent_type there).
    """
    if source == "identity":
        counts = get_identity_funnel(exclude_dogfood=human_only)
        window = "all-time"
        return _build_funnel(
            counts,
            window,
            source="identity",
            stage_order=_IDENTITY_STAGE_ORDER,
            stage_label=_IDENTITY_STAGE_LABELS,
        )

    store = FunnelStore()
    try:
        if day:
            counts = await store.get_day(day)
            breakdown = await store.get_day_by_agent_type(day)
            window = day
        else:
            counts = await store.get_totals()
            breakdown = await store.get_totals_by_agent_type()
            window = "all-time"
    finally:
        await store.close()
    return _build_funnel(counts, window, source="visitor", breakdown=breakdown)


@metrics_router.post("/metrics/funnel/event")
async def record_funnel_event(req: FunnelEventRequest, request: Request) -> dict[str, bool]:
    """Client beacon for top-of-funnel stages (issue #787).

    Only ``CLIENT_EMITTABLE_STAGES`` (today: ``landed``) are accepted — every
    downstream stage is recorded server-side at its authoritative transition so a
    client cannot inflate it. Always 200 (``recorded`` flags whether it counted);
    the visitor id is read from the ``archimedes_vid`` cookie via request state.

    Issue #830: this JS-gated ``landed`` beacon is the single source of truth for
    "distinct visitor". We record geography + device here (same ``archimedes_vid``
    dedup key as the funnel) so geo/device count exactly the ``landed`` population
    — reconciling the funnel-vs-visitors discrepancy. Both writes are fail-safe.
    """
    if req.stage not in CLIENT_EMITTABLE_STAGES:
        return {"recorded": False}
    await record_funnel(request, req.stage)
    # Geography + device off the same landed beacon (one distinct-visitor source).
    # Pass the telemetry classifier's verdict through so the defense-in-depth
    # ``is_agent`` skip in ``record_visitor_insight`` actually fires — a beacon
    # carrying an internal-agent key / bot UA must not inflate the visitor count.
    # ``request.state.is_agent`` is set by the telemetry middleware; default to
    # ``False`` (an ordinary human beacon) if that middleware error-swallowed.
    is_agent = bool(getattr(request.state, "is_agent", False))
    await record_visitor_insight(request, is_agent=is_agent)
    return {"recorded": True}


@metrics_router.get("/metrics/visitors", response_model=VisitorInsightsResponse)
async def get_visitor_insights() -> VisitorInsightsResponse:
    """Where our human traffic comes from + on what device (issue #787).

    Distinct HUMAN visitors (agents excluded), country via CloudFront viewer-country,
    device via CloudFront device headers (UA fallback). Fail-safe: returns empty
    maps when Redis is unreachable, so this always responds 200.
    """
    store = VisitorInsightsStore()
    try:
        countries, devices = await store.get_insights()
    finally:
        await store.close()
    ranked = sorted(countries.items(), key=lambda kv: (-kv[1], kv[0]))
    return VisitorInsightsResponse(
        window="all-time",
        countries=[CountryCount(code=c, distinct_visitors=n) for c, n in ranked],
        devices=devices,
        timestamp=datetime.now(UTC).isoformat(),
    )
