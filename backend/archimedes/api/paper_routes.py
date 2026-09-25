"""Paper-trading endpoints — /api/paper/* (MVP: verdict → paper-trade).

Ownership is CANONICAL account identity (#1194): every route requires a Better
Auth session and every deployment is owned by ``owner_user_id``. Account-first
users never hold a SIWE session post-#1194, so a wallet-session gate here
would 401 exactly the users the product creates. Paper deploys are SIMULATED —
no funds move — so an account session is the correct strength of proof (the
relaxed-lookup boundary in ``wallet_routes.get_linked_wallet_address`` applies:
no fresh wallet signature is required for a non-money-moving action; the day a
paper→real promotion exists, THAT action takes the fresh-signature gate).

``owner_wallet`` is recorded as provenance only (the caller's linked wallet at
deploy time, when one exists) — it never grants access. The table shipped with
this feature, so there are no legacy wallet-owned rows and no fallback tier.

**Every read route here is `async def` and every one of them does SYNCHRONOUS,
BLOCKING database work, so the work runs on a worker thread (#1818 P4).** The
2026-09-03 outage is the argument: a query that could not take its lock froze
the event loop of both web processes, and `/health` — a sibling coroutine on
that same loop — stopped answering, so the ALB saw a DEAD target instead of a
slow endpoint and pulled the whole fleet out of service. Off the loop, the same
blocked query costs one pool thread and `/health` keeps answering, which is the
difference between a slow page and a 504.

`asyncio.to_thread`, not a private pool: it uses the loop's default executor,
which `main.py` sizes explicitly (`_install_default_executor`, floor 16) precisely
so serving handlers and the generation fan-out share a pool somebody chose. The
health probes are the one deliberate exception (`main._HEALTH_PROBE_EXECUTOR`) and
they say why at their own definition.

The sync half of each route is a module-level `_*_sync` function rather than a
closure, so the guard in `backend/tests/test_handlers_off_the_loop.py` can name
it and a reviewer can see at a glance that the coroutine body is the hop and
nothing else.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from archimedes.api.account_auth import CurrentUser, require_current_user
from archimedes.api.limiter import limiter
from archimedes.api.wallet_routes import get_linked_wallet_address
from archimedes.db import get_session
from archimedes.models.paper_store import STATUS_STOPPED, PaperDeployment
from archimedes.services.paper_marks import list_marks, mark_to_dict
from archimedes.services.paper_trading import (
    COVERAGE_BROKEN_LOG,
    PaperTraceCoverageError,
    advance_deployment,
    create_deployment,
    deployment_summary,
)
from archimedes.services.passport_loader import stored_rigor_verdicts, ungraded_verdicts_for
from archimedes.services.strategy_dsl import DSLError

logger = logging.getLogger(__name__)

paper_router = APIRouter(prefix="/api/paper", tags=["paper"])


def _spec_for_strategy(session, strategy_id: str, caller_wallet: str | None, caller_user_id: str | None) -> dict:
    """The spec to snapshot, gated on OWNERSHIP of the source strategy.

    The validated DSL spec is the strategy's EXECUTABLE LOGIC — the derivation,
    not the card. Publishing a strategy puts its name, methodology and metrics
    on a public board; it does not hand over the machine-readable
    implementation for anyone to snapshot into their own ledger. So this asks
    ``is_strategy_reasoning_visible``, not ``is_strategy_visible``: curated /
    ``is_example`` house content stays available to everyone, a user's row is
    available to its owner, and ``is_published`` alone grants nothing (#1557 —
    this call site used the card predicate, so any published row's spec was
    deployable by any signed-in stranger).

    This fails CLOSED on purpose. If a marketplace/licensing flow later makes
    "paper-trade someone else's published strategy" a deliberate product
    decision, that flow reopens it explicitly — with whatever attribution or
    licensing check it decides on — rather than inheriting the permission by
    accident from a predicate that was never about reasoning.

    404 for both "no such strategy" and "not yours": existence stays private
    (#850 idiom, same as ``_owned_deployment`` below).
    """
    from archimedes.models.strategy_store import StrategyRecord
    from archimedes.services.strategy_visibility import is_strategy_reasoning_visible

    row = session.query(StrategyRecord).filter_by(id=strategy_id).first()
    if row is None or not is_strategy_reasoning_visible(row, caller_wallet, caller_user_id=caller_user_id):
        raise HTTPException(status_code=404, detail="Strategy not found")
    spec = (row.to_dict() or {}).get("strategy_spec")
    if not spec:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "This strategy has no machine-readable spec to paper-trade.",
                "reason": "no_strategy_spec",
            },
        )
    return spec


def _owned_deployment(session, deployment_id: str, user_id: str) -> PaperDeployment:
    """Fetch a deployment the caller owns — 404 for missing AND for not-yours:
    existence is private (#850 idiom). Ownership is owner_user_id only."""
    dep = session.query(PaperDeployment).filter_by(id=deployment_id).first()
    if dep is None or dep.owner_user_id != user_id:
        raise HTTPException(status_code=404, detail="Paper deployment not found")
    return dep


@paper_router.post("/deployments", status_code=201)
@limiter.limit("10/minute")
async def deploy_paper(
    request: Request,
    response: Response,  # noqa: ARG001 — slowapi header injection (#1182 invariant)
    body: dict,
    user: CurrentUser = Depends(require_current_user),
):
    strategy_id = str(body.get("strategy_id") or "").strip()
    if not strategy_id:
        raise HTTPException(status_code=422, detail="strategy_id is required")

    # Provenance only — never an access grant, may be None (account-only user).
    linked_wallet = get_linked_wallet_address(request)

    with get_session() as session:
        spec = _spec_for_strategy(session, strategy_id, linked_wallet, user.id)
        try:
            dep = create_deployment(
                session,
                strategy_id=strategy_id,
                spec_dict=spec,
                owner_wallet=linked_wallet,
                owner_user_id=user.id,
            )
        except DSLError as exc:
            raise HTTPException(
                status_code=422,
                detail={"message": f"Stored spec fails validation: {exc}", "reason": "invalid_strategy_spec"},
            ) from exc
        # First advance is best-effort and SYNCHRONOUS-failure-soft: the
        # deployment exists even if data is momentarily unavailable — the
        # scheduler's daily pass will backfill from deployed_at.
        try:
            advance_deployment(session, dep)
        except PaperTraceCoverageError:
            # NOT the deferred-warning path. "Deferred to the scheduler" says
            # the data was momentarily unavailable and the next pass will fix
            # it; a broken coverage identity is a bug in the trace pipeline and
            # the next pass will break the same way. Distinct ERROR with its
            # own literal. The deployment is still returned rather than 500ing:
            # it exists, its ledger is fine, and its trace_coverage on the
            # payload below is what carries the hole to the user.
            logger.error("%s for deployment %s (initial advance)", COVERAGE_BROKEN_LOG, dep.id, exc_info=True)
        except Exception as exc:
            logger.warning("paper: initial advance for %s deferred to the scheduler: %s", dep.id, exc)
        # COMMIT BEFORE SUMMARISING. The summary reads `strategy_passports`
        # (#1764's verdict of record); a read that fails on a session still
        # holding the just-created deployment used to take the deployment with
        # it — the route returned 201 carrying a deployment_id for a row that
        # had been discarded. Committing first makes the write durable before
        # anything else touches the session, so the worst a broken passport
        # read can now do is degrade THIS payload's verdict to "not graded".
        # Pinned by test_a_broken_passport_read_never_discards_the_deployment.
        session.commit()
        return deployment_summary(session, dep)


def _page_verdicts(session, strategy_ids: list[str]) -> dict[str, dict]:
    """The stored rigor verdict for a whole page of deployments — ONE query.

    Batched here rather than per row (``deployment_summary`` would otherwise
    issue one ``SELECT … FROM strategy_passports`` per deployment), and — more
    importantly — this is where the RECOVERY lives. ``stored_rigor_verdicts``
    raises on a DB-level failure by design, because un-poisoning a session is
    only safe where there is nothing uncommitted to lose. This route is exactly
    that place: a pure read, no writes in flight, so the rollback below can
    discard nothing, and the page degrades to "every row not graded" while
    still serving the ledger — the track record is the user's, and a broken
    passport read must not take it down. No row is ever degraded to a PASS.
    """
    try:
        found = stored_rigor_verdicts(session, strategy_ids)
    except Exception as exc:  # pragma: no cover — defensive; DB-level failure
        logger.warning(
            "paper: passport verdict read failed for the deployments page (%s) — every row degrades to ungraded",
            type(exc).__name__,
        )
        try:
            session.rollback()
        except Exception:
            logger.debug("rollback after a failed passport verdict read also failed", exc_info=True)
        return ungraded_verdicts_for(strategy_ids)
    return {**ungraded_verdicts_for(strategy_ids), **found}


def _list_paper_deployments_sync(user_id: str) -> dict:
    """The blocking half of ``GET /deployments`` — runs on a worker thread."""
    with get_session() as session:
        deps = (
            session.query(PaperDeployment)
            .filter(PaperDeployment.owner_user_id == user_id)
            .order_by(PaperDeployment.created_at.desc())
            .all()
        )
        verdicts = _page_verdicts(session, [d.strategy_id for d in deps])
        return {"deployments": [deployment_summary(session, d, verdict=verdicts[d.strategy_id]) for d in deps]}


@paper_router.get("/deployments")
async def list_paper_deployments(request: Request, user: CurrentUser = Depends(require_current_user)):  # noqa: ARG001
    return await asyncio.to_thread(_list_paper_deployments_sync, user.id)


def _get_paper_deployment_sync(deployment_id: str, user_id: str) -> dict:
    """The blocking half of ``GET /deployments/{id}`` — runs on a worker thread."""
    with get_session() as session:
        dep = _owned_deployment(session, deployment_id, user_id)
        return deployment_summary(session, dep)


@paper_router.get("/deployments/{deployment_id}")
async def get_paper_deployment(
    request: Request,  # noqa: ARG001 — route signature parity
    deployment_id: str,
    user: CurrentUser = Depends(require_current_user),
):
    return await asyncio.to_thread(_get_paper_deployment_sync, deployment_id, user.id)


def _get_paper_deployment_marks_sync(deployment_id: str, limit: int, user_id: str) -> dict:
    """The blocking half of ``GET /deployments/{id}/marks`` — runs on a worker thread.

    The clamp lives here, with the query it bounds, rather than in the coroutine:
    the coroutine is the hop and nothing else, so there is no second place a
    reviewer has to look to know what the database was asked for.
    """
    with get_session() as session:
        dep = _owned_deployment(session, deployment_id, user_id)
        rows = list_marks(session, dep.id, limit=max(1, min(limit, 2000)))
        return {
            "deployment_id": dep.id,
            "marks": [mark_to_dict(row) for row in rows],
            "latest": mark_to_dict(rows[-1]) if rows else None,
        }


@paper_router.get("/deployments/{deployment_id}/marks")
async def get_paper_deployment_marks(
    request: Request,  # noqa: ARG001 — route signature parity
    deployment_id: str,
    limit: int = 500,
    user: CurrentUser = Depends(require_current_user),
):
    """Intraday marks for one owned deployment, oldest first.

    Same ``_owned_deployment`` gate as every other route here — a mark is
    still the caller's private track-record decoration, and a wrong-owner
    lookup 404s exactly like an unknown id.

    Marks are the UNSETTLED view: ``paper_daily_returns`` (the ``series`` on
    the deployment summary) is the append-only track record that carries to
    mainnet, and a mark is a decoration with a TTL that the retention job
    deletes past 90 days. A client must render the two distinguishably; it
    must never present a mark as a settled return.

    **A mark cannot see cash — the disclosed v1 limitation.** A mark
    re-prices the strategy's ASSET BASKET (the sleeve weights the last daily
    settle established) by applying each asset's move since that settle. It
    does NOT know whether the strategy is currently invested or flat:
    ``replay_spec`` returns dated portfolio returns, not a per-sleeve
    invested/flat vector, so v1 has no position vector to read and inferring
    one from the return series would be a guess dressed as a measurement.

    Concretely: a strategy sitting in CASH still shows a live value that moves
    with the assets it would hold — a settled ``+0.00%`` day can carry a
    ``+10.00%`` mark if the underlying rose 10%
    (``test_a_cash_sleeve_is_still_marked_as_if_invested`` pins exactly that).
    The error never touches the ledger and the next daily advance re-settles
    the anchor, so it is bounded to one session and cannot accumulate — but a
    client must DISCLOSE it at the point of render, not bury it. The settled
    daily return is the honest number. Closing the gap needs a position vector
    out of the graded engine: the marks-v2 follow-up.

    ``limit`` is clamped to the same bound the storage tier is designed for
    (a day of raw crypto marks is 96 rows, a week is 672) so a client cannot
    ask for an unbounded scan; the newest ``limit`` rows are returned.
    """
    return await asyncio.to_thread(_get_paper_deployment_marks_sync, deployment_id, limit, user.id)


@paper_router.post("/deployments/{deployment_id}/stop")
async def stop_paper_deployment(
    request: Request,  # noqa: ARG001 — route signature parity
    deployment_id: str,
    user: CurrentUser = Depends(require_current_user),
):
    with get_session() as session:
        dep = _owned_deployment(session, deployment_id, user.id)
        dep.status = STATUS_STOPPED
        session.commit()
        return {"deployment_id": dep.id, "status": dep.status}
