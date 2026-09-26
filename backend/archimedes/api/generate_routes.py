"""Streaming Generate API.

Endpoints (per ``docs/specs/generation-streaming-spec.md``):

  POST /api/generate/start                    — create a job (returns job_id)
  GET  /api/generate/stream/{job_id}          — SSE event stream
  POST /api/generate/jobs/{job_id}/cancel     — best-effort cancel
  GET  /api/generate/jobs                     — list recent jobs (status table)
  GET  /api/generate/jobs/{job_id}            — one job's status (poll fallback)
  GET  /api/generate/jobs/{job_id}/candidates — N candidates incl. rejected
  GET  /api/generate/jobs/{job_id}/cost       — raw measurement, no prices (#1217)

This router lives in its own file per the Spine+ v2 plan's cross-cutting
principle #2 — no new endpoints go into ``api/routes.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

from archimedes.agents.generation_pipeline import (
    _invalid_brief_message,
    cheap_brief_reject,
    run_generation,
)
from archimedes.api.account_auth import CurrentUser, require_current_user
from archimedes.api.funnel_middleware import record_funnel
from archimedes.api.generate_schemas import (
    CandidatesListResponse,
    CandidateSummary,
    CreditSummary,
    GenerateBrief,
    GenerateStartRequest,
    GenerateStartResponse,
    JobCostResponse,
    JobsListResponse,
    JobSummary,
)
from archimedes.api.limiter import limiter
from archimedes.api.wallet_routes import get_linked_wallet_address
from archimedes.services import free_generations, generation_credits, generation_payment
from archimedes.services.generation_quota import enforce_generation_quota
from archimedes.services.identity_events import emit_identity_event
from archimedes.services.job_queue import EVENT_LOG_TTL, get_job_store
from archimedes.services.llm_backend import is_allowed_model
from archimedes.services.log_scrubber import sanitize_log_value
from archimedes.services.model_gate import enforce_model_entitlement

logger = logging.getLogger(__name__)

generate_router = APIRouter(prefix="/api/generate", tags=["generate"])

# Public sibling: main.py mounts generate_router behind require_current_user
# wholesale, but the price quote must be readable BEFORE sign-in — a human
# comparing cost, or an agent planning its approval flow (#1293), needs the
# number first. Only /quote lives here; everything stateful stays gated.
generate_public_router = APIRouter(prefix="/api/generate", tags=["generate"])

_TERMINAL_EVENTS = {"done", "error"}
# Terminal job-store statuses — mirrors `_normalize_state`'s known-status set
# minus "queued"/"running". Used by the SSE loop's dead-stream detection.
_TERMINAL_STATUSES = {"done", "error", "cancelled"}
_POLL_INTERVAL_SECONDS = 0.4
_STREAM_TIMEOUT_SECONDS = 300  # cap a single SSE connection at 5 min
# How long the connection may go byte-silent before we push a keep-alive
# comment (#891). Debate/fan-out compute (sequential adversarial LLM turns,
# then a parallel backtest gather across the whole candidate pool) can run
# for tens of seconds without producing a new job-store event — the poll
# loop previously just slept through that stretch and wrote nothing to the
# socket. Intermediaries with an idle-read timeout shorter than that stretch
# (CloudFront's origin idle timeout, corporate/browser proxies) will drop a
# chunked connection that's gone quiet that long, even though the origin is
# still alive and the job keeps running server-side. A ~15s heartbeat
# cadence gives multiple safety margins under any such timeout.
_HEARTBEAT_INTERVAL_SECONDS = 15.0

# How often the run's independent heartbeat task touches the job's
# `heartbeat_at` (#1355). On its own clock, NOT gated on pipeline progress —
# see `_run_with_cleanup`/`_heartbeat_loop`. Comfortably inside
# `_STALLED_AFTER_SECONDS` so a live run never reads as stalled.
_JOB_HEARTBEAT_INTERVAL_SECONDS = 30.0

# A `running` job whose heartbeat is older than this is reported `stalled` on
# read (`_normalize_state`) and, if an SSE stream is open on it, gets one
# synthetic `error`/`STALLED` event so the stream stops claiming to be live.
# Value matches the spec's threshold (docs/specs/generation-streaming-spec.md
# § Failure modes: "Lock-without-progress for > 5 min").
_STALLED_AFTER_SECONDS = 300

# Hard ceiling on one generation run (v8 Lane 1.3b — bound generation hangs).
# `_STALLED_AFTER_SECONDS` only makes a dead run *observable*; nothing
# previously stopped `run_generation` itself from hanging forever (an LLM
# call with no client-side timeout, a stuck backtest thread) while quietly
# holding the payer's consumed credit and a `_GENERATION_GATE` slot the whole
# time. `_run_with_cleanup` wraps the awaited call in `asyncio.wait_for` at
# this bound.
_DEFAULT_GENERATION_TIMEOUT_SECONDS = 600

# Registry of the in-flight asyncio tasks THIS process is running, for local
# liveness/diagnostics only. It is deliberately NOT the cancellation
# mechanism (#1667): a process-local dict cannot see a job started by another
# task, so cancel_job goes through the shared Redis flag
# (`JobStore.request_cancel`) that the pipeline polls at its stage boundaries.
_RUNNING_TASKS: dict[str, asyncio.Task] = {}


def _register_task(job_id: str, task: asyncio.Task) -> None:
    _RUNNING_TASKS[job_id] = task
    task.add_done_callback(lambda _t, jid=job_id: _RUNNING_TASKS.pop(jid, None))


# ── Generation admission control ──────────────────────────────────────────
# The whole web tier shares one Fargate task; a single generation averages
# ~65% of its vCPU for ~48s (measured 2026-08-20), so unbounded parallel
# pipelines starve auth, SSE, and the ALB health check — the task gets
# killed and EVERY in-flight job dies with it. At most
# GENERATION_MAX_CONCURRENT pipelines run at once; up to
# GENERATION_MAX_QUEUE more wait their turn (the job stays `queued` and its
# SSE stream gets a `job_queued` event + heartbeats); beyond that /start
# refuses 429 BEFORE the payment gate, so nobody is ever charged for a slot
# that doesn't exist.

_GENERATION_GATE: asyncio.Semaphore | None = None
_GENERATION_GATE_LOOP: asyncio.AbstractEventLoop | None = None
_WAITING_GENERATIONS = 0


def _max_concurrent_generations() -> int:
    try:
        return max(1, int(os.getenv("GENERATION_MAX_CONCURRENT", "1")))
    except ValueError:
        return 1


def _max_queued_generations() -> int:
    try:
        return max(0, int(os.getenv("GENERATION_MAX_QUEUE", "10")))
    except ValueError:
        return 10


def _generation_timeout_seconds() -> float:
    """Bound for one run, from `GENERATION_TIMEOUT_SECONDS` (v8 Lane 1.3b).

    Parsed as defensively as `_max_concurrent_generations` above (and
    `revenue_sweep._min_usdc`): a missing, non-numeric, or non-positive value
    must never crash startup — it falls back to the default instead.

    `GENERATION_TIMEOUT_SECONDS=inf` is the one intended escape hatch: `float`
    accepts it, it passes the `> 0` floor, and `asyncio.wait_for` with an
    infinite timeout never fires — restoring the old unbounded behaviour on
    purpose (e.g. a long-running local batch) rather than by accident.
    """
    try:
        value = float(os.getenv("GENERATION_TIMEOUT_SECONDS", str(_DEFAULT_GENERATION_TIMEOUT_SECONDS)))
    except ValueError:
        return float(_DEFAULT_GENERATION_TIMEOUT_SECONDS)
    return value if value > 0 else float(_DEFAULT_GENERATION_TIMEOUT_SECONDS)


def _generation_gate() -> asyncio.Semaphore:
    """Per-event-loop singleton.

    asyncio primitives bind to the loop that first awaits them; a module-level
    singleton would leak a closed test loop into the next test. Recreating on
    loop change costs nothing in prod (one loop for the process lifetime).
    """
    global _GENERATION_GATE, _GENERATION_GATE_LOOP
    loop = asyncio.get_running_loop()
    if _GENERATION_GATE is None or _GENERATION_GATE_LOOP is not loop:
        _GENERATION_GATE = asyncio.Semaphore(_max_concurrent_generations())
        _GENERATION_GATE_LOOP = loop
    return _GENERATION_GATE


async def _emit_queued(job_id: str, position: int) -> None:
    """Tell the job's SSE stream it is waiting — informational, never fatal."""
    from datetime import UTC, datetime

    try:
        await get_job_store().push_event(
            job_id,
            {
                "event": "job_queued",
                "data": {
                    "ts": datetime.now(UTC).isoformat(),
                    "job_id": job_id,
                    "position": position,
                    "max_concurrent": _max_concurrent_generations(),
                },
            },
        )
    except Exception:
        logger.warning("could not emit job_queued for %s", job_id, exc_info=True)


def _require_job_access(job: dict, user_id: str, job_id: str, linked_wallet: str | None = None) -> None:
    """Hide account-owned and unclaimed legacy jobs from other users."""
    payload = job.get("payload") or {}
    owner_user_id = payload.get("owner_user_id")
    owner_wallet = payload.get("owner_wallet")
    if owner_user_id and owner_user_id != user_id:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found or expired")
    if not owner_user_id and owner_wallet and owner_wallet.lower() != (linked_wallet or "").lower():
        raise HTTPException(status_code=404, detail=f"job {job_id} not found or expired")


# USDC's on-chain decimal precision — matches marketplace.payments._USDC_DECIMALS.
# PaymentInfo.amount (circlekit) is a string of raw base units, e.g. "2000000"
# for $2.00; this is the sole place generate_routes converts that into a
# display dollar string for the receipt (see models/payment_receipt.py).
_RECEIPT_USDC_DECIMALS = 6


def _format_receipt_usd(amount_base_units: int) -> str:
    from decimal import Decimal

    return f"${Decimal(amount_base_units) / Decimal(10**_RECEIPT_USDC_DECIMALS):.2f}"


def _write_payment_receipt(*, user_id: str, payment, job_id: str) -> None:
    """The persistence boundary — no try/except here. The caller
    (``_persist_payment_receipt``) wraps this; tests patch this exact name to
    exercise the fail-safe path without reaching into the DB layer."""
    from archimedes.db import get_session
    from archimedes.models.payment_receipt import record_payment_receipt

    amount_base_units = int(payment.amount)
    with get_session() as session:
        record_payment_receipt(
            session,
            user_id=user_id,
            payer_wallet=payment.payer,
            amount_base_units=amount_base_units,
            price_usd=_format_receipt_usd(amount_base_units),
            network=payment.network,
            settlement_ref=payment.transaction,
            job_id=job_id,
        )
        session.commit()


def _persist_payment_receipt(*, user_id: str, payment, job_id: str) -> None:
    """Persist one settled generation payment as a receipt (Dan's directive:
    "we must provide people with their receipts").

    FAIL-SAFE, deliberately: the payment already cleared by the time this
    runs — the user already paid. A receipt-write failure must never fail or
    delay the paid generation, so every exception is swallowed here and only
    logged. This is the ONE place in this module that name is true; every
    other write on the happy path (job enqueue, funnel, identity event) is
    allowed to matter to the response.
    """
    try:
        _write_payment_receipt(user_id=user_id, payment=payment, job_id=job_id)
    except Exception:
        logger.warning(
            "payment receipt write failed for job %s (payment already settled — no user impact)",
            sanitize_log_value(job_id),
            exc_info=True,
        )


async def _paywall_with_credit(request: Request, linked_wallet: str, user_id: str):
    """Run the paywall so that money taken is always money accounted for (#1441).

    Returns ``(payment, credit_id)``. ``credit_id`` names the credit this run
    spends once it is safely enqueued; ``payment`` is the settled
    ``PaymentInfo`` only when this request is what settled it.

    The ordering below is the fix. The charge used to settle and the job to be
    enqueued afterwards, with the entitlement gate in between — so a 402 from
    that gate, an enqueue error, or a worker crash left the money taken and
    nothing delivered. Now the charge buys a durable credit first, and only a
    job that actually reaches the queue spends it.
    """
    # An unspent credit from an earlier paid-but-undelivered run pays for this
    # one. Checked BEFORE the paywall, so a payer whose last generation died is
    # never asked for money twice.
    existing = generation_credits.take_credit(user_id)
    if existing is not None:
        logger.info("generation covered by unspent credit %s — no new charge", existing)
        return None, existing

    if not generation_payment.settles_real_value():
        # Flag off or dry-run: the paywall still quotes and still exercises the
        # 402 approval flow, but no value moves. Nothing to owe and nothing to
        # record, so the ledger stays entirely out of the way.
        settled = await generation_payment.enforce_generation_payment(request, linked_wallet)
        return settled, None

    idempotency_key = (request.headers.get("Idempotency-Key") or "").strip() or None
    outcome, credit_id = generation_credits.claim(user_id, idempotency_key)

    if outcome == "already_settled":
        # This key already paid and the credit is still unspent. Spend it.
        return None, credit_id
    if outcome == "already_consumed":
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "idempotency_key_already_used",
                "message": (
                    "This Idempotency-Key already paid for a generation that started. "
                    "Use a new key to start another. No second charge was taken."
                ),
            },
        )
    if outcome == "in_flight":
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "payment_in_flight",
                "message": (
                    "A payment with this Idempotency-Key is still settling. Wait for it to "
                    "finish rather than retrying — a retry signs a fresh authorization and "
                    "would charge you twice."
                ),
            },
        )

    try:
        settled = await generation_payment.enforce_generation_payment(request, linked_wallet)
    except BaseException:
        # Includes the 402 the paywall raises when no payment was presented. The
        # claim must not outlive the attempt: a `pending` row left behind reads
        # as `in_flight` forever and locks that key out.
        generation_credits.void(credit_id)
        raise

    if settled is None:
        # settles_real_value() said otherwise, so this is a configuration race
        # rather than an expected path. No value moved; release the claim.
        generation_credits.void(credit_id)
        return None, None

    generation_credits.settle(credit_id, settled)
    return settled, credit_id


@generate_public_router.get("/quote")
async def get_generation_quote():
    """The upfront generation cost estimate — public, so a human can see the
    price before signing in and an agent can plan before paying (#1293's
    discoverability point). The SAME quote rides inside every 402 from
    /start, so the two surfaces can never disagree: both call
    ``generation_payment.quote()``, whose flat price is the seam #1217's
    measured budget later replaces."""
    return generation_payment.quote()


@generate_router.get("/credits", response_model=list[CreditSummary])
async def list_generation_credits(
    user: CurrentUser = Depends(require_current_user),
) -> list[CreditSummary]:
    """The calling user's own generation-credit ledger (v8 Lane 1.3a).

    Makes visible what ``_paywall_with_credit`` already does silently: an
    unspent (``available``) credit from an earlier paid-but-undelivered run
    pays for the NEXT generation, with no new charge (#1441). Requires
    sign-in — unlike ``/quote``, a credit belongs to a specific account, so
    there is nothing honest to show before auth.

    A bare list, not the wrapped ``{jobs: [...]}`` shape sibling listings use
    — the frontend's only question is "does an unspent credit exist", so
    there is no envelope worth adding.
    """
    rows = generation_credits.list_credits(user.id)
    return [
        CreditSummary(
            id=row["id"],
            status=row["status"],
            created_at=row["created_at"],
            job_id=row["job_id"],
            amount_usdc=(round(row["amount_base_units"] / 10**6, 2) if row["amount_base_units"] is not None else None),
        )
        for row in rows
    ]


@generate_router.post("/start", response_model=GenerateStartResponse, status_code=202)
@limiter.limit("5/minute")
async def start_generation(
    req: GenerateStartRequest,
    request: Request,  # used for slowapi rate-limit keying AND funnel attribution (#787)
    response: Response,  # slowapi header injection (#1182) + PAYMENT-RESPONSE receipt
    user: CurrentUser = Depends(require_current_user),
) -> GenerateStartResponse:
    """Create account-owned generation job and start pipeline in background."""
    linked_wallet = get_linked_wallet_address(request)

    # Daily volume caps (per-account AND per-IP, both must pass) — the rebuilt
    # generation_quota (#1194 revision a). Runs FIRST: cheapest anti-abuse
    # check before any entitlement or enqueue work, same ordering the old
    # wallet-less cap had. Disabled under TESTING (conftest sets it), matching
    # the slowapi limiter; the quota logic is unit-tested directly in
    # test_generation_quota.py.
    if not os.getenv("TESTING"):
        await enforce_generation_quota(request, user.id)

    # Admission control: refuse when the wait queue is full. Deliberately
    # BEFORE the payment gate — a caller must never pay for a slot that
    # doesn't exist. (Counts only waiting jobs; running ones aren't queued.)
    if _max_queued_generations() <= _WAITING_GENERATIONS:
        raise HTTPException(
            status_code=429,
            detail={
                "reason": "generation_queue_full",
                "message": (
                    f"The generation queue is full ({_WAITING_GENERATIONS} jobs waiting). "
                    "No payment was taken. Retry in a few minutes."
                ),
            },
        )

    # Deterministic brief screen (Lane 1.3c: "never charge for a brief we can
    # cheaply reject"). Deliberately BEFORE the payment gate — a caller must
    # never be charged for a brief that is empty, gibberish, over-length, or
    # carrying a prompt-injection payload. Shares its exact criteria with the
    # LLM validator's own prelude in generation_pipeline._validate_brief via
    # `cheap_brief_reject` — see that function's docstring for why the two
    # call sites can never drift apart. What this still misses is SEMANTIC
    # (off-topic but grammatical text): that gets the LLM check post-payment,
    # exactly as before — that outcome legitimately consumes work, so it
    # stays a credit spend, not a pre-payment refusal.
    cheap_reject = cheap_brief_reject(req.brief)
    if cheap_reject is not None:
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "brief_invalid",
                "code": "BRIEF_INVALID",
                "message": _invalid_brief_message(cheap_reject.get("reason")),
                "hint": cheap_reject.get("hint") or "Mention an asset class, a goal, or a risk appetite.",
                # Machine-readable code from services.brief_screen's versioned
                # vocabulary (#1801). Additive — the four keys above are
                # unchanged, so no existing client moves.
                "reason_code": cheap_reject.get("code") or "",
            },
        )

    # Payment gate (flag: GENERATION_PAYMENT_REQUIRED — Dan flips deliberately,
    # see the #834 flip-list). Order is deliberate: AFTER the quota (a
    # quota-blocked caller is refused 429 before ever being asked to pay) and
    # BEFORE entitlement/enqueue (no work is burned for an unpaid request).
    # The 402 carries the full x402 requirements — that response IS the
    # quote-approval flow for humans and agents alike. Paper trading stays free.
    # None under flag-off / dry-run (see enforce_generation_payment); a real
    # settled PaymentInfo only when the flag is on and the payment cleared —
    # that is also the ONLY case a payment receipt is persisted (below, once
    # job_id exists).
    payment = None
    credit_id = None
    free_grant_id = None
    if generation_payment.payment_required():
        # FREE PATH (#1643 — the owner's 2026-08-31 product review REVERSES the
        # 2026-08-19 "no free path" directive). An account is still required for
        # every generation, free or paid — `require_current_user` above is
        # unconditional and there is deliberately no wallet-only path — but the
        # first FREE_GENERATIONS_PER_ACCOUNT (default 3) runs on a VERIFIED
        # account need no wallet and no payment. The slot is claimed BEFORE the
        # gate it opens, so N concurrent first-generation calls cannot each be
        # granted one (services/free_generations.py, and the unique constraint
        # behind it). Everything from grant #4 onward is byte-for-byte the
        # behaviour below, unchanged.
        #
        # `email_verified` is owner decision D1 (2026-08-31, recorded on #1653):
        # the allowance unlocks on a verified email, not on account creation
        # alone — accounts are free and unlimited, a working inbox is not, so
        # verification is what prices disposable-account farming of free LLM
        # runs. An unverified caller is NOT refused here; it simply falls
        # through to the wallet gate + paywall it had before this path existed.
        # The flag is threaded from CurrentUser (api/account_auth.py parses
        # Better Auth's `emailVerified` in exactly one place) and is a required
        # keyword argument, so a future call site cannot forget it.
        #
        # The claim lives INSIDE this flag branch on purpose: under flag-off
        # nothing is gated and nothing is charged, so burning a lifetime
        # allowance there would silently spend a user's free runs during a
        # period when generation was free for everyone anyway.
        free_grant_id = free_generations.claim(user.id, email_verified=user.email_verified)
        if free_grant_id is None:
            if not linked_wallet:
                # The wallet-connection precondition. 409 (not 402): the blocker is
                # account state, not a missing payment. NOTE the faucet is the one
                # human-only step (#1294) — an agent hitting this must have its
                # wallet funded by a human before payment can succeed.
                # Funnel (#1643): this is the exhausted-the-free-tier boundary —
                # the transition the conversion instrument most needs to see.
                await record_funnel(request, "wallet_gate_shown")
                # Two DIFFERENT dead ends share this status code, and telling a
                # caller the wrong one wastes its next request: an account that
                # spent its three free runs has only the wallet left, while an
                # unverified account has two ways out and the cheaper one is the
                # inbox it already owns. `reason` stays `wallet_link_required`
                # (the machine contract every client already branches on);
                # `free_generations_locked_reason` is the new, additive field
                # that distinguishes them, and the message names BOTH unlocks
                # when both are real.
                locked = free_generations.locked_reason(email_verified=user.email_verified)
                if locked == free_generations.LOCK_EMAIL_UNVERIFIED:
                    message = (
                        "Two ways to generate. (1) Verify your email to unlock "
                        f"{free_generations.allowance()} free generations on this account — no wallet and no "
                        "payment needed. We emailed a verification link when you signed up; "
                        "POST /api/auth/send-verification-email re-sends it. "
                        "(2) Or link a wallet now (POST /api/wallets/challenge → /api/wallets/verify), "
                        "fund it with testnet USDC (the faucet currently requires a human), and pay per run. "
                        "See GET /api/generate/quote for the price."
                    )
                else:
                    message = (
                        "Your free generations are used up. Generation now requires a linked, funded "
                        "wallet. Link a wallet to your account "
                        "(POST /api/wallets/challenge → /api/wallets/verify), fund it with testnet USDC "
                        "(the faucet currently requires a human), then retry. "
                        "See GET /api/generate/quote for the price."
                    )
                raise HTTPException(
                    status_code=409,
                    detail={
                        "reason": "wallet_link_required",
                        "free_generations_locked_reason": locked,
                        "message": message,
                    },
                )
            payment, credit_id = await _paywall_with_credit(request, linked_wallet, user.id)
            if payment is not None:
                # Surface the settlement receipt (PAYMENT-RESPONSE) to the payer.
                for name, value in (payment.response_headers or {}).items():
                    response.headers[name] = value

    # From here to the enqueue, a claimed free slot is at risk: the entitlement
    # gate can raise 402 and the enqueue can error, and either would leave the
    # allowance spent on a generation that never ran. Same shape, and the same
    # reason, as _paywall_with_credit's `except BaseException: void; raise`.
    # This covers only what THIS request can see fail. Everything that goes
    # wrong after the enqueue — the corpus yielding too few papers to fuse, a
    # pipeline crash, a cancel — is released by
    # `release_entitlements_if_undelivered` in the run's own `finally`, keyed on
    # the job id stamped below. That seam, not this helper's caller, is what
    # makes the release reach BOTH run paths (#1793).
    try:
        # Paid-tier gating (T1.8): a premium (Anthropic) model requires a
        # wallet-connected entitlement. Enforced BEFORE the job is enqueued so a
        # non-entitled premium request is rejected (HTTP 402) without burning any
        # work — and is NOT silently downgraded to the free default model. Free
        # models (and the unset/default case) always pass.
        # Normal account use and free models need no wallet. A free-tier caller
        # is NOT exempt: the free allowance buys the default model, not premium.
        enforce_model_entitlement(req.model, linked_wallet)

        store = get_job_store()
        # Free-tier selection (defense in depth — the UI also restricts this).
        # Runs AFTER the entitlement gate above: a non-entitled premium request has
        # already been rejected with 402, so this only ever sees an allowlisted free
        # model, an entitled premium id, junk, or None. Only an allowlisted free-tier
        # id is honored for the pipeline; everything else (incl. entitled premium,
        # which cannot serve until Bedrock activation — roadmap T3.8) falls back to
        # the env default, so behavior is UNCHANGED when no valid free model is picked.
        selected_model = req.model if is_allowed_model(req.model) else None
        if req.model and selected_model is None:
            logger.info("generate: ignoring non-allowlisted model %r; using env default", sanitize_log_value(req.model))
        # Canonical Better Auth ownership is server-derived and follows the job
        # through persistence. Wallet provenance stays optional.
        job_id = await store.enqueue(
            job_type="generate",
            payload={
                "brief": req.brief.model_dump(),
                "n_candidates": req.n_candidates,
                "owner_user_id": user.id,
                "owner_wallet": linked_wallet,
                # The allowlist-filtered model the pipeline will actually use (None →
                # env default). enforce_model_entitlement (above) has already rejected
                # a non-entitled premium request with 402, so anything reaching here is
                # either an allowlisted free model or None — auditable provenance for
                # the tier the run was authorized for.
                "model": selected_model,
            },
        )
    except BaseException:
        if free_grant_id is not None:
            free_generations.release(free_grant_id)
        raise

    # The free slot is bound to its generation only now, once the job is queued
    # — the same "spend only what was delivered" point at which a paid credit is
    # consumed below. The stamp is also what the terminal-failure release finds
    # the row by, so an unstamped slot cannot be handed back later (see
    # `free_generations.stamp_job`, which says so where it fails).
    if free_grant_id is not None:
        free_generations.stamp_job(free_grant_id, job_id=job_id)

    # Payment receipt (Dan's directive: "we must provide people with their
    # receipts"). Only when a real settled PaymentInfo exists — flag-off and
    # dry-run leave `payment` None and nothing is written. Deliberately AFTER
    # enqueue succeeds, so the receipt carries a real job_id.
    if payment is not None:
        _persist_payment_receipt(user_id=user.id, payment=payment, job_id=job_id)

    # The credit is spent only now, once the job is queued — that is what makes
    # every failure before this point cost the payer nothing (#1441).
    if credit_id is not None:
        generation_credits.consume(credit_id, job_id=job_id)

    # Fire-and-forget; the SSE stream tails events. User ownership is threaded
    # from this authenticated request, never client-supplied.
    task = asyncio.create_task(
        _run_with_cleanup(
            job_id,
            req.brief,
            req.n_candidates,
            req.mode,
            selected_model,
            owner_user_id=user.id,
            owner_wallet=linked_wallet,
        )
    )
    _register_task(job_id, task)

    # Conversion funnel (#787): a generation actually started for this visitor —
    # the key "tried the product" transition. Fail-safe; never blocks the response.
    await record_funnel(request, "generation_started")
    # …and, when it was one of the account's free runs (#1643), which side of
    # the new gate it fell on. Emitted here rather than at claim time so a
    # released slot (the except above) is never counted as a free generation
    # the visitor actually received.
    if free_grant_id is not None:
        await record_funnel(request, "free_generation_used")
    emit_identity_event(
        wallet=linked_wallet,
        event_type="generation_started",
        actor_class="human",
        meta={"job_id": job_id, "model": selected_model},
    )

    return GenerateStartResponse(
        job_id=job_id,
        stream_url=f"/api/generate/stream/{job_id}",
        ttl_seconds=EVENT_LOG_TTL,
    )


def _heartbeat_age_seconds(heartbeat_at: str | None) -> float | None:
    """Seconds since ``heartbeat_at``, or ``None`` if absent/unparseable.

    ``None`` is a real "no signal" — a job that predates this field, or a
    malformed value — and every caller must treat that as "cannot determine
    staleness", never as "assume stale" or "assume fresh" (the same fail-soft
    discipline as the rest of this module's honest-absence fields).
    """
    if not heartbeat_at:
        return None
    try:
        ts = datetime.fromisoformat(heartbeat_at)
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (datetime.now(UTC) - ts).total_seconds()


async def _heartbeat_loop(job_id: str, store) -> None:
    """Independent liveness proof for one run — see `runner_lease.py`'s
    `start_renewal` for the precedent this copies: its own clock, not tied to
    the caller's tick/progress. A single debate turn or fan-out backtest
    gather can run for tens of seconds without the pipeline emitting any
    job-store event at all, so gating the touch on pipeline progress would
    reintroduce the exact gap #1355 closes.
    """
    while True:
        await asyncio.sleep(_JOB_HEARTBEAT_INTERVAL_SECONDS)
        try:
            await store.touch(job_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A missed heartbeat write must never abort the run itself — the
            # TTL + stale-heartbeat read path is the safety net, matching the
            # cost meter's "instrumentation never changes the outcome" rule.
            logger.exception("heartbeat: touch failed for job %s", sanitize_log_value(job_id))


async def _release_credit_if_undelivered(job_id: str, store) -> None:
    """Give the credit back unless the job actually delivered (#1441).

    The charge bought a credit and the enqueue spent it. If the run then died —
    worker crash, LLM failure, a container roll, or the payer cancelling — the
    payer holds nothing, so the credit goes back and their next generation
    spends it instead of asking for money again.

    Terminal states other than ``done`` all restore. Cancellation is included
    deliberately: a cancelled run produced no strategy, and charging for it
    would make the paywall a fee on trying rather than a price for delivery.

    A job whose Redis record has already expired reads as undelivered, which is
    the safe direction to be wrong in — the alternative silently keeps money for
    a run nobody can prove finished. ``restore_credit_for_job`` only ever moves
    a ``consumed`` credit, so a second call cannot mint a second credit.

    Fail-safe: this runs in a background task's ``finally`` with nobody to
    report to, so it logs and returns.
    """
    try:
        job = await store.get(job_id)
        status = (job or {}).get("status")
        if status == "done":
            return
        if generation_credits.restore_for_job(job_id):
            logger.warning(
                "job %s ended as %s — generation credit restored, the payer owes nothing",
                sanitize_log_value(job_id),
                sanitize_log_value(str(status)),
            )
    except Exception:
        logger.exception("could not evaluate credit release for job %s", sanitize_log_value(job_id))


async def _job_persisted_a_strategy(job_id: str, store) -> bool | None:
    """Did this run put a strategy in the caller's library? ``None`` = cannot tell.

    ``status == "done"`` is NOT the same question. A run can persist the
    winning strategy (``generation_pipeline`` § "K=1 persistence") and only
    then die — the backtest fan-out crashing, the user cancelling at the
    ``backtest_persist`` stage boundary, the process rolling — which leaves a
    terminal status of ``error``/``cancelled`` beside a strategy that is
    genuinely in the library. Handing a free generation back for that run would
    give the account the strategy AND the slot.

    The oracle is the ``persisted`` event the pipeline pushes immediately after
    ``_persist_candidate`` returns a real ``strategy_id``. Read-only: this is a
    consumer of the event log, and nothing here writes to it.

    Three-valued on purpose. The event log has a shorter TTL than the job
    record, the read can fail, and a store double may not expose the surface at
    all (the same "a store that predates this surface is skipped" allowance
    ``_abort_if_cancel_requested`` makes). Collapsing any of those onto
    ``False`` would report "nothing was delivered" on no evidence, and the
    caller would hand back a slot that may have bought a library row.
    ``None`` says only what is true — we could not look — and the caller keeps
    the slot spent, which is recoverable: the ledger row is on disk with its
    job id.
    """
    lister = getattr(store, "list_events", None)
    if lister is None:
        logger.debug("free-slot release: store exposes no event log for job %s", sanitize_log_value(job_id))
        return None
    try:
        events = await lister(job_id)
    except Exception:
        logger.warning(
            "free-slot release: could not read the event log for job %s",
            sanitize_log_value(job_id),
            exc_info=True,
        )
        return None
    for ev in events:
        if ev.get("event") != "persisted":
            continue
        data = ev.get("data")
        if isinstance(data, dict) and data.get("strategy_id"):
            return True
    return False


async def _release_free_slot_if_undelivered(job_id: str, store) -> None:
    """Give a free generation back unless the job actually delivered (#1643).

    The free-tier sibling of :func:`_release_credit_if_undelivered`, and the
    fix for what the owner saw on 2026-09-01: a brief whose corpus yielded
    fewer than two papers failed INSIDE the pipeline with "the society cannot
    fuse", and the allowance still went 3 → 2. ``/start`` only ever released a
    slot for failures it could see itself (the entitlement gate, the enqueue —
    its ``except BaseException: release; raise``); every failure after the
    enqueue kept the slot spent on a generation that produced nothing.

    Two states keep the slot spent, and only two:

    ``done``
        The run delivered. Checked first and cheaply.
    a persisted strategy
        The run put a strategy in the library and died afterwards. Giving the
        slot back here would hand out the strategy and the free generation.
        Deliberately stricter than the paid path, which restores on every
        non-``done`` status (see :func:`_release_credit_if_undelivered`) —
        money erring toward the payer is the right direction for an accounting
        bug on a paywall, but re-granting an allowance that DID buy a library
        row is just an over-grant.

    …and one more that keeps it spent without deciding anything: a job record
    or event log this cannot read. Silence is not evidence of non-delivery.

    Idempotent through ``release_grant_for_job``: only a ``used`` row with this
    ``job_id`` moves, so a retried cleanup cannot mint a second free slot out of
    one claim. A paid or flag-off run matches no row and this is a no-op.

    Fail-safe: this runs in a background task's ``finally`` with nobody to
    report to, so it logs and returns. A read failure leaves the slot spent —
    the honest direction when we cannot tell whether the run delivered, and
    recoverable, since the ledger row is still on disk with its job id.
    """
    try:
        job = await store.get(job_id)
        if job is None:
            # Deliberately NOT the credit path's "reads as undelivered". This
            # runs in the run's own `finally`, so a job record that is already
            # gone means the store lost it, not that time passed — and with no
            # record there is no event log either, so "no strategy persisted"
            # would be an inference from missing data rather than a reading of
            # it. The slot stays spent and says so; the ledger row keeps its
            # job id, so it can still be released by hand.
            logger.warning(
                "job %s has no job record at cleanup — the free generation stays spent (undecidable)",
                sanitize_log_value(job_id),
            )
            return
        status = job.get("status")
        if status == "done":
            return
        persisted = await _job_persisted_a_strategy(job_id, store)
        if persisted is None:
            logger.warning(
                "job %s ended as %s and its event log could not be read — the free generation "
                "stays spent rather than being handed back on no evidence",
                sanitize_log_value(job_id),
                sanitize_log_value(str(status)),
            )
            return
        if persisted:
            logger.info(
                "job %s ended as %s but persisted a strategy — the free generation stays spent",
                sanitize_log_value(job_id),
                sanitize_log_value(str(status)),
            )
            return
        if free_generations.release_for_job(job_id):
            logger.warning(
                "job %s ended as %s with no strategy — free generation returned to the account",
                sanitize_log_value(job_id),
                sanitize_log_value(str(status)),
            )
    except Exception:
        logger.exception(
            "could not evaluate the free-generation release for job %s — the slot stays spent",
            sanitize_log_value(job_id),
        )


async def release_entitlements_if_undelivered(job_id: str, store) -> None:
    """Every refund an undelivered run owes, in ONE place both run paths call.

    Two code paths run a generation, and only one of them used to clean up:

    * ``_run_with_cleanup`` — the serving task's background coroutine, spawned
      by ``POST /api/generate/start``;
    * ``scripts/run_generation_job.run_job`` — the out-of-process entrypoint
      that ``docs/adr/lambda-generation-offload.md`` ADOPTED (an
      ``ecs:RunTask``, a Lambda invocation, or an operator's ``python -m``).

    The enqueue spends the caller's entitlements *before* either of them
    starts, so both owe the same refunds when the run then delivers nothing.
    The refunds lived inside ``_run_with_cleanup``'s ``finally``, which the
    script never enters, so on that path every post-enqueue failure — thin
    corpus, crash, cancel, timeout — kept the caller's credit, and after #1785
    their free slot, spent (#1793).

    **A new release goes HERE, not into a caller's ``finally``.** That is the
    whole point of the seam, and it is enforced rather than asked for:
    ``test_run_generation_job.py::TestBothRunPathsReleaseTheSameThings``
    discovers every release helper in this module and fails if one of them is
    not reached through this function. #1785's
    ``_release_free_slot_if_undelivered`` — the free tier's equivalent — landed
    on main in ``_run_with_cleanup``'s ``finally``, i.e. the serving path only,
    which is the very shape that test fails on; moving it into this function is
    what gives the offload entrypoint the free slot back too.

    **The limit of that discovery, stated where the next author will read it:**
    it matches a NAMING CONVENTION —
    ``_(release|void|refund|restore)_<thing>_(if|when)_undelivered`` on this
    module — not reachability. A refund helper named outside that pattern is
    invisible to the tripwire and can be given to one run path only without
    anything going red. Name a new one ``_release_<thing>_if_undelivered``.

    ``store`` is a parameter rather than a ``get_job_store()`` call because the
    offload worker binds its store from the environment *as it is at run time*
    — ``get_job_store()``'s singleton reads a ``REDIS_URL`` frozen at import,
    which for that worker is the localhost default. See
    ``run_generation_job``'s module docstring for why no import ordering can
    win that race.
    """
    await _release_credit_if_undelivered(job_id, store)
    # …and the free-tier equivalent. Both run: a run is funded by a credit or
    # by a free slot, never both, and each helper is a no-op for the funding it
    # does not own.
    await _release_free_slot_if_undelivered(job_id, store)


async def _run_with_cleanup(
    job_id: str,
    brief: GenerateBrief,
    n_candidates: int,
    mode: str | None = None,
    model: str | None = None,
    owner_user_id: str | None = None,
    owner_wallet: str | None = None,
) -> None:
    global _WAITING_GENERATIONS
    store = get_job_store()
    # Heartbeat runs from entry — a QUEUED job (waiting on the admission
    # gate) is alive, and must not read as stalled while it waits its turn.
    heartbeat_task = asyncio.create_task(_heartbeat_loop(job_id, store), name=f"job-heartbeat-{job_id}")
    try:
        gate = _generation_gate()
        if gate.locked():
            _WAITING_GENERATIONS += 1
            try:
                await _emit_queued(job_id, _WAITING_GENERATIONS)
                await gate.acquire()
            finally:
                _WAITING_GENERATIONS -= 1
        else:
            await gate.acquire()
        timeout_seconds = _generation_timeout_seconds()
        try:
            try:
                await asyncio.wait_for(
                    run_generation(
                        job_id=job_id,
                        brief=brief,
                        n_candidates=n_candidates,
                        mode=mode,
                        model=model,
                        owner_user_id=owner_user_id,
                        owner_wallet=owner_wallet,
                    ),
                    timeout=timeout_seconds,
                )
            # `asyncio.TimeoutError` IS the builtin `TimeoutError` on 3.11+ (a
            # pure alias), so UP041 is right that this is redundant — but the
            # alias is the intent: this catches the timeout `asyncio.wait_for`
            # raises, NOT some `TimeoutError` bubbling up out of the pipeline's
            # own socket/HTTP layer. Spelling it `asyncio.` keeps the next
            # reader from having to rediscover which one is meant.
            except asyncio.TimeoutError:  # noqa: UP041
                # wait_for cancels the still-running run_generation coroutine
                # on timeout and waits for it to unwind, so its own
                # `except asyncio.CancelledError` branch has ALREADY run by the
                # time we get here. That branch left two dishonest traces —
                # nobody clicked Cancel:
                #   1. status "cancelled"/"cancelled by client", and
                #   2. an `error`/`CANCELLED` "job cancelled" event.
                # (1) is overwritten below by update_status — not
                # update_terminal_status, which treats "cancelled" as sticky on
                # purpose for the real-cancel race. Any non-"done" terminal
                # status still reaches `_release_credit_if_undelivered` in the
                # outer finally, so the payer's credit restores either way.
                # (2) cannot be overwritten: the event log is append-only, so
                # the honest TIMEOUT frame is APPENDED after it. Who actually
                # reads it: `_TERMINAL_EVENTS` makes `stream_events` return on
                # the FIRST `error` frame, so a client connected for the whole
                # run still closes on "job cancelled" — but a client resuming
                # with `Last-Event-ID` past that frame gets TIMEOUT, and the
                # log itself is the durable record of why the job really died.
                message = f"generation exceeded the {timeout_seconds:g}-second limit"
                logger.warning(
                    "job %s exceeded GENERATION_TIMEOUT_SECONDS=%s — marking error",
                    sanitize_log_value(job_id),
                    timeout_seconds,
                )
                # Status first: it is the authoritative record, and it stays
                # honest even if the event push below fails.
                await store.update_status(job_id, "error", error=message)
                await store.push_event(
                    job_id,
                    {
                        "event": "error",
                        "data": {
                            "job_id": job_id,
                            "message": message,
                            "recoverable": False,
                            "code": "TIMEOUT",
                        },
                    },
                )
        finally:
            gate.release()
    except asyncio.CancelledError:
        raise
    except Exception:  # safety net — run_generation already emits error events
        logger.exception("background job %s crashed outside run_generation", job_id)
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
        # ONE seam, both run paths (#1793). A new refund goes INSIDE
        # `release_entitlements_if_undelivered`, never on the next line here:
        # a release added to this `finally` is a release the offload
        # entrypoint does not get. That is how #1785 arrived.
        await release_entitlements_if_undelivered(job_id, store)


@generate_router.get("/stream/{job_id}")
async def stream_events(
    job_id: str,
    request: Request,
    user: CurrentUser = Depends(require_current_user),
) -> StreamingResponse:
    """Server-Sent Events for one account-owned generation job."""
    store = get_job_store()
    job = await store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found or expired")
    _require_job_access(job, user.id, job_id, get_linked_wallet_address(request))

    try:
        last_event_id = int(request.headers.get("Last-Event-ID", "0"))
    except (TypeError, ValueError):
        last_event_id = 0

    async def event_generator() -> AsyncIterator[str]:
        # Yield a comment to flush the response headers immediately so the
        # client's onopen fires within the spec's 500 ms target.
        yield ": stream opened\n\n"

        cursor = last_event_id
        # Wall-clock, not accumulated sleep duration: list_events() latency or
        # scheduler jitter can make a single loop iteration take longer than
        # _POLL_INTERVAL_SECONDS, and summing the intended sleep length instead
        # of measuring real elapsed time would undercount silence and could
        # delay a heartbeat past _HEARTBEAT_INTERVAL_SECONDS -- reintroducing
        # the idle-disconnect this fix exists to prevent.
        loop = asyncio.get_running_loop()
        stream_start = loop.time()
        last_heartbeat_at = stream_start
        while loop.time() - stream_start < _STREAM_TIMEOUT_SECONDS:
            if await request.is_disconnected():
                logger.info("sse client disconnected (job=%s, after=%d)", sanitize_log_value(job_id), cursor)
                return

            new_events = await store.list_events(job_id, after_id=cursor)
            for ev in new_events:
                cursor = ev["id"]
                yield _format_sse(ev)
                last_heartbeat_at = loop.time()  # a real event resets the silence clock too
                if ev.get("event") in _TERMINAL_EVENTS:
                    return

            # Dead-job detection (#1355): the event log alone can't tell a
            # slow job from a dead one, so also read the job record itself
            # every cycle. Two cases:
            #   1. `running` with a heartbeat older than _STALLED_AFTER_SECONDS
            #      — the backend process that owned this job died mid-run
            #      (routine trigger: build-on-deploy rolling the Fargate task).
            #      Closed by a synthetic `error`/`STALLED` frame.
            #   2. Redis already shows a terminal status (done/error/cancelled)
            #      but no terminal event ever reached the log. This is the
            #      ROUTINE case, not a corner case: `EVENT_LOG_TTL` (15 min) is
            #      shorter than `JOB_TTL` (1 hour, refreshed on every write),
            #      so any client that reconnects to a completed job more than
            #      15 minutes after it finished hits this branch. It also
            #      covers the writer dying between the status write and the
            #      event push. A second `list_events` read is given a beat to
            #      catch the ordinary case first — the pipeline writes status
            #      THEN pushes the terminal event as two separate awaits, so a
            #      genuinely-current job racing the very end of that window is
            #      not misreported.
            #
            #      The synthetic frame branches on the REAL terminal status —
            #      collapsing all three onto `error`/STALLED would report a
            #      SUCCESSFUL job as failed to the client, which is exactly
            #      the false claim CLAUDE.md's `Claims must be true` rule
            #      forbids. `STALLED` is reserved for case 1 above.
            job = await store.get(job_id)
            if job is not None:
                status = job.get("status")
                if status in _TERMINAL_STATUSES:
                    trailing = await store.list_events(job_id, after_id=cursor)
                    saw_terminal = False
                    for ev in trailing:
                        cursor = ev["id"]
                        yield _format_sse(ev)
                        if ev.get("event") in _TERMINAL_EVENTS:
                            saw_terminal = True
                            break
                    if saw_terminal:
                        return
                    result = job.get("result") or {}
                    if status == "done":
                        candidates = result.get("candidates") or []
                        yield _format_sse(
                            {
                                "id": cursor + 1,
                                "event": "done",
                                "data": {
                                    "job_id": job_id,
                                    "strategy_id": result.get("best_strategy_id"),
                                    "all_strategy_ids": {
                                        c.get("candidate_id"): c.get("strategy_id")
                                        for c in candidates
                                        if c.get("candidate_id")
                                    },
                                },
                            }
                        )
                    elif status == "cancelled":
                        yield _format_sse(
                            {
                                "id": cursor + 1,
                                "event": "error",
                                "data": {
                                    "job_id": job_id,
                                    "message": "this generation was cancelled",
                                    "recoverable": False,
                                    "code": "CANCELLED",
                                },
                            }
                        )
                    else:  # status == "error"
                        yield _format_sse(
                            {
                                "id": cursor + 1,
                                "event": "error",
                                "data": {
                                    "job_id": job_id,
                                    "message": "this generation failed — check its status directly",
                                    "recoverable": False,
                                    "code": "JOB_FAILED",
                                },
                            }
                        )
                    return
                if status == "running":
                    stale_for = _heartbeat_age_seconds(job.get("heartbeat_at"))
                    if stale_for is not None and stale_for > _STALLED_AFTER_SECONDS:
                        logger.info(
                            "sse: job %s reads stalled (heartbeat %.0fs old) — closing stream",
                            sanitize_log_value(job_id),
                            stale_for,
                        )
                        yield _format_sse(
                            {
                                "id": cursor + 1,
                                "event": "error",
                                "data": {
                                    "job_id": job_id,
                                    "message": (
                                        f"no heartbeat from this job in over {_STALLED_AFTER_SECONDS}s "
                                        "— it likely died mid-run"
                                    ),
                                    "recoverable": False,
                                    "code": "STALLED",
                                },
                            }
                        )
                        return

            # No new events this cycle — a long-running compute step (debate
            # turns, candidate-fan-out backtests) may keep the job busy for a
            # while yet. Emit a keep-alive comment so the connection never
            # goes byte-silent long enough for an intermediary to decide it's
            # dead (#891). SSE comment lines are ignored by EventSource/any
            # spec-compliant parser, so this is invisible to application code.
            now = loop.time()
            if now - last_heartbeat_at >= _HEARTBEAT_INTERVAL_SECONDS:
                yield ": heartbeat\n\n"
                last_heartbeat_at = now

            await asyncio.sleep(_POLL_INTERVAL_SECONDS)

        # Heartbeat-timeout exit — client can reconnect with Last-Event-ID.
        yield ": stream timeout\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _format_sse(ev: dict) -> str:
    """Encode one event log entry as an SSE frame."""
    event_id = ev["id"]
    event_name = ev.get("event", "message")
    data = ev.get("data", {})
    return f"id: {event_id}\nevent: {event_name}\ndata: {json.dumps(data, default=str)}\n\n"


@generate_router.post("/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: str,
    request: Request,
    user: CurrentUser = Depends(require_current_user),
) -> dict[str, str]:
    """Cancel a running job. Idempotent.

    Cancellation is a **durable Redis flag**, not a local task handle (#1667).
    The pipeline polls that flag at its stage boundaries and stops from
    whichever task is actually running it. This is the only shape that works
    once the service runs more than one task: ``_RUNNING_TASKS`` is
    process-local, so at ``MinCapacity=2`` this POST landed on the task that
    started the job barely half the time — and every other time the old code
    returned ``{"status": "cancelled"}`` while the pipeline kept running and
    burning LLM tokens. A worker/offload lane has no task registry at all.

    The flag is written and read back BEFORE the job is reported cancelled;
    if that write cannot be confirmed we return 503 rather than claim a
    cancellation that never happened.

    Caveat (unchanged): if the agent is mid-``asyncio.to_thread(llm_call)``,
    that OS thread isn't cancellable, so the in-flight call finishes and its
    result is discarded at the next stage boundary — no events emitted after
    the cancellation, no strategy persisted.
    """
    store = get_job_store()
    job = await store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found or expired")

    _require_job_access(job, user.id, job_id, get_linked_wallet_address(request))

    if job["status"] in ("done", "error", "cancelled"):
        return {"job_id": job_id, "status": job["status"]}

    # Durably request the cancel FIRST — this is what actually stops the
    # pipeline, from any task. Nothing below may claim "cancelled" until this
    # write is confirmed.
    try:
        requested = await store.request_cancel(job_id)
    except Exception:
        logger.exception("cancel: flag write failed for job %s", sanitize_log_value(job_id))
        requested = False
    if not requested:
        # Honest failure: the job IS still running. Reporting "cancelled" here
        # is the claims-truth violation this endpoint used to commit every time
        # the POST landed on a task that didn't own the job.
        raise HTTPException(
            status_code=503,
            detail=f"cancellation for job {job_id} could not be recorded — the job is still running; retry",
        )

    # Now the flag is durable, the status may be flipped and the terminal
    # event pushed: observers see "cancelled" and the claim is backed.
    await store.update_status(job_id, "cancelled", error="cancelled by user")
    await store.push_event(
        job_id,
        {
            "event": "error",
            "data": {"job_id": job_id, "message": "cancelled by user", "recoverable": False, "code": "CANCELLED"},
        },
    )

    return {"job_id": job_id, "status": "cancelled"}


@generate_router.get("/jobs", response_model=JobsListResponse)
async def list_jobs(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    user: CurrentUser = Depends(require_current_user),
) -> JobsListResponse:
    """Recent jobs for the GenerationStatus UI.

    Better Auth session is required. Results are filtered to canonical owner;
    linked wallet resolves pre-migration wallet-owned jobs. Cross-user jobs stay hidden.
    """
    store = get_job_store()
    raw = await store.list_recent_jobs(limit=limit)
    linked_wallet = get_linked_wallet_address(request)
    summaries: list[JobSummary] = []
    for j in raw:
        if j.get("type") != "generate":
            continue
        payload = j.get("payload") or {}
        owner_user_id = payload.get("owner_user_id")
        owner_wallet = payload.get("owner_wallet")
        if owner_user_id and owner_user_id != user.id:
            continue
        if not owner_user_id and owner_wallet and owner_wallet.lower() != (linked_wallet or "").lower():
            continue
        summaries.append(_job_summary(j))
    return JobsListResponse(jobs=summaries)


def _job_summary(job: dict, job_id: str | None = None) -> JobSummary:
    """Project one job-store record onto the wire shape.

    Shared by the listing and the single-job read so the two surfaces can never
    disagree about a job's state — an agent that switches from ``GET /jobs`` to
    ``GET /jobs/{job_id}`` reads the identical record.
    """
    payload = job.get("payload") or {}
    brief = payload.get("brief") or {}
    result = job.get("result") or {}
    return JobSummary(
        job_id=job.get("id") or job_id or "",
        state=_normalize_state(job.get("status") or "queued", job.get("heartbeat_at")),
        brief_intent=brief.get("intent", ""),
        created_at=job.get("created_at", ""),
        updated_at=job.get("updated_at", ""),
        n_candidates=int(payload.get("n_candidates") or 1),
        best_strategy_id=result.get("best_strategy_id"),
        # Raw measurement for this job (#1217): token counts, per-stage
        # seconds, write tallies. None until the job reaches a terminal
        # state, and None for jobs generated before the meter existed.
        cost=result.get("cost"),
    )


def _normalize_state(s: str, heartbeat_at: str | None = None) -> str:
    """Map a raw job-store status onto the wire vocabulary.

    ``heartbeat_at`` is optional and additive (#1355): when given and ``s`` is
    ``"running"``, a heartbeat older than ``_STALLED_AFTER_SECONDS`` reads as
    the derived ``"stalled"`` state — nothing is written back to Redis, this
    is purely a read-time reinterpretation, so ``GET /jobs`` and
    ``GET /jobs/{id}`` (both route through this) can never disagree. Omitting
    ``heartbeat_at`` (the default) preserves the exact pre-#1355 behavior for
    any caller that hasn't been updated to pass it.
    """
    if s not in ("queued", "running", "done", "error", "cancelled"):
        return "queued"
    if s == "running":
        stale_for = _heartbeat_age_seconds(heartbeat_at)
        if stale_for is not None and stale_for > _STALLED_AFTER_SECONDS:
            return "stalled"
    return s


@generate_router.get("/jobs/{job_id}", response_model=JobSummary)
async def get_job(
    job_id: str,
    request: Request,
    user: CurrentUser = Depends(require_current_user),
) -> JobSummary:
    """One job's current state — the poll fallback for a client with no live stream (#1292).

    An agent that never opened the SSE stream, or whose connection dropped past
    the 15-minute event-log TTL, previously had to pull ``GET /jobs`` and scan
    the whole listing to learn whether its single job had finished. This returns
    the same ``JobSummary`` record for one job.

    Three refusals, all rendered as the byte-identical 404 the other per-job
    reads use, so none of them is an existence oracle:

    * unknown / expired ``job_id``;
    * a job whose ``type`` is not ``generate`` — this endpoint is the generate
      surface, not a general job reader. The filter mirrors ``list_jobs`` and is
      load-bearing rather than cosmetic: sibling job types use states outside
      this router's vocabulary (``strategies_routes`` writes ``failed``), which
      ``_normalize_state`` would coerce to ``queued`` — reporting a crashed job
      as still-waiting;
    * a job owned by another account, or a legacy wallet-owned job whose owner
      is not the caller's linked wallet (``_require_job_access``).

    The stored ``error`` string is deliberately not surfaced: the pipeline
    writes raw ``str(exc)`` into it, which is unscrubbed internal detail. The
    ``error`` state plus the SSE ``error`` event remain the reporting path.
    """
    store = get_job_store()
    job = await store.get(job_id)
    if not job or job.get("type") != "generate":
        raise HTTPException(status_code=404, detail=f"job {job_id} not found or expired")
    _require_job_access(job, user.id, job_id, get_linked_wallet_address(request))
    return _job_summary(job, job_id)


@generate_router.get("/jobs/{job_id}/cost", response_model=JobCostResponse)
async def get_job_cost(
    job_id: str,
    request: Request,
    user: CurrentUser = Depends(require_current_user),
) -> JobCostResponse:
    """What this generation actually consumed (#1217).

    Raw measurement only — Bedrock input/output token counts taken from the
    provider's own ``usage`` block, wall + CPU seconds per pipeline stage, peak
    RSS, and the rows the pipeline wrote. **No prices**: the paywall quote seam
    (``GET /api/generate/quote``) stays ``flat_v1`` and is untouched by this
    endpoint; converting these counts into dollars is done off-server, and this
    is the input that lets it stop being an estimate.

    Owner-scoped exactly like ``/candidates`` — a job you do not own 404s rather
    than leaking its existence. ``cost`` is ``null`` for a job that has not
    reached a terminal state yet, and for jobs older than the instrumentation.
    """
    store = get_job_store()
    job = await store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found or expired")
    _require_job_access(job, user.id, job_id, get_linked_wallet_address(request))
    result = job.get("result") or {}
    cost = result.get("cost") if isinstance(result, dict) else None
    return JobCostResponse(
        job_id=job_id,
        # heartbeat_at passed exactly like `_job_summary` (#1355) so this
        # third read surface can't disagree with `/jobs` and `/jobs/{id}` —
        # all three route through the same `_normalize_state`.
        state=_normalize_state(job.get("status") or "queued", job.get("heartbeat_at")),
        cost=cost if isinstance(cost, dict) else None,
    )


@generate_router.get("/jobs/{job_id}/candidates", response_model=CandidatesListResponse)
async def list_candidates(
    job_id: str,
    request: Request,
    user: CurrentUser = Depends(require_current_user),
) -> CandidatesListResponse:
    """Rejected-candidate viewer. Empty list until ``done``."""
    store = get_job_store()
    job = await store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found or expired")
    _require_job_access(job, user.id, job_id, get_linked_wallet_address(request))
    result = job.get("result") or {}
    cands = result.get("candidates", []) or []
    return CandidatesListResponse(
        job_id=job_id,
        best_candidate_id=result.get("best_candidate_id"),
        candidates=[CandidateSummary(**c) for c in cands],
    )
