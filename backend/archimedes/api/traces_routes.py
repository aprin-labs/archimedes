"""Reasoning trace endpoints — /api/traces/*.

Every READ route here is ownership-gated (#1556). Before that issue the four
read routes carried no auth dependency at all and ``vault_address`` was a
filter, not a gate — omitting it enumerated every trace on the platform, and
``/canonical`` returned the full hashed body including holdings. The predicate
lives in ``services.trace_visibility`` and is never re-implemented here; these
routes only decide the *status code* for a denial, which is uniformly **404**,
matching the #850 ownership contract in ``strategies_routes``: a caller who may
not read a trace must not learn that it exists.

``?strategy_id=`` carries a SECOND gate for a different subject. #1556 decides
who may read a *trace*; ``assert_strategy_visible`` decides who may learn that a
*strategy* exists, and "how many traces reference id X" is a statement about X.
Both run — a caller who passes the strategy gate still only sees the traces
#1556 lets them see. Neither substitutes for the other.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC

from fastapi import APIRouter, Depends, Query, Request

from archimedes.api._route_helpers import assert_strategy_visible
from archimedes.api.auth_guard import require_internal_agent_key
from archimedes.api.limiter import limiter
from archimedes.api.schemas import (
    TraceDetailResponse,
    TraceListResponse,
    TracePublishRequest,
    TracePublishResponse,
    TraceResponse,
    TraceVerifyResponse,
)
from archimedes.models.trace import DecisionType, ReasoningTrace

logger = logging.getLogger(__name__)

traces_router = APIRouter(prefix="/api/traces", tags=["traces"])


#: The trace as the on-chain registry ALONE can describe it. Same word as
#: ``TraceVerifyResponse.verification_mode`` uses for this state (#1359).
ANCHORED_ONLY = "anchored_only"


def _anchored_only_trace(trace_id: str, detail: dict) -> TraceResponse:
    """Project a registry entry with no off-chain body behind it (#1407).

    Both display routes fall back to this when the off-chain store has no
    record — or is unreachable, which this path deliberately does not
    distinguish, because from here the two are the same fact: **nothing was
    compared.**

    What is genuinely known is that an anchor exists at this id; we just read
    it out of the registry. That is why ``is_verified`` stays true, and why
    flipping it false would be a different fabrication rather than a fix:
    ``Portfolio.jsx`` renders the false branch as "anchor pending — registry
    write didn't complete yet", which would be an invented denial of the one
    thing this path is certain about.

    What was never true is the implication that a hash was *compared*. Nothing
    on this path re-derives or matches anything, so ``verification_mode`` says
    ``anchored_only`` and ``arc_tx_hash`` stays ``None`` — the registry read
    does not surface the anchoring transaction, and an absent reference must
    render as absent rather than be invented.
    """
    from datetime import datetime

    return TraceResponse(
        id=trace_id,
        vault_address=detail["vault"],
        # "unknown", not "rebalance" (#1356): the on-chain anchor does not
        # record which decision type produced it, so asserting "rebalance" for
        # every trace on this path is an invented fact. "unknown" is the same
        # default the off-chain path already uses when its data lacks the field.
        decision_type="unknown",
        trigger="on-chain",
        timestamp=datetime.fromtimestamp(detail["timestamp"], tz=UTC).isoformat(),
        reasoning="On-chain trace (off-chain metadata not available)",
        confidence=0.0,
        trace_hash=detail["trace_hash"],
        is_verified=True,
        verification_mode=ANCHORED_ONLY,
    )


def _caller_identity(request: Request) -> tuple[str | None, str | None]:
    """``(user_id, linked_wallet)`` for the caller — both ``None`` if anonymous.

    Anonymous is a normal outcome, not an error: the public proof pages read
    this API without a session and are entitled to the house-public traces.
    The wallet lookup is a DB read (``get_linked_wallet_address``) and is
    wrapped because its failure must degrade the caller to "no wallet" — which
    can only ever *reduce* what they can see — never 500 a read route.
    """
    from archimedes.api.account_auth import get_current_user

    user = get_current_user(request)
    try:
        from archimedes.api.auth_siwe import get_verified_wallet

        wallet = get_verified_wallet(request)
    except Exception:
        logger.warning("trace read: linked-wallet resolution failed — treating caller as wallet-less", exc_info=True)
        wallet = None
    return (user.id if user else None), wallet


def _offchain_trace_response(t: dict, fallback_id: str = "") -> TraceResponse:
    """Project a stored off-chain trace record onto the wire schema.

    The ONE projection both display routes use. ``get_trace`` widens the result
    to :class:`TraceDetailResponse`; it does not re-derive it. Two
    hand-maintained copies of a twenty-field constructor is how a field lands on
    the detail route and silently never appears in the list.
    """
    return TraceResponse(
        id=t.get("id", fallback_id),
        vault_address=t.get("vault_address", ""),
        decision_type=t.get("decision_type", "unknown"),
        trigger=t.get("trigger", "unknown"),
        timestamp=t.get("timestamp", ""),
        reasoning=t.get("reasoning", ""),
        confidence=t.get("confidence", 0.0),
        trace_hash=t.get("trace_hash", ""),
        arc_tx_hash=t.get("arc_tx_hash"),
        is_verified=t.get("is_verified", False),
        # `or {}` not a default: a persisted trace can carry an explicit
        # market_context of null, and `.get(k, {})` returns that null.
        regime_at_decision=(t.get("market_context") or {}).get("regime"),
        trades_executed=t.get("trades_executed", []),
        strategies_referenced=t.get("strategies_referenced", []),
        commit_tx_hash=t.get("commit_tx_hash"),
        commit_block_number=t.get("commit_block_number"),
        reveal_tx_hash=t.get("reveal_tx_hash"),
        reveal_block_number=t.get("reveal_block_number"),
        trade_tx_hash=t.get("trade_tx_hash"),
        trade_block_number=t.get("trade_block_number"),
        temporal_binding_valid=t.get("temporal_binding_valid"),
        temporal_binding_source=t.get("temporal_binding_source", "none"),
    )


async def _assert_can_read(trace: dict, request: Request) -> None:
    """404 unless the caller may read this trace (#1556).

    404 rather than 403 deliberately: a 403 on someone else's trace id
    confirms the id exists, which is half of the enumeration the gate is here
    to prevent.

    ``can_read_trace`` may open a synchronous SQLAlchemy session (only for an
    unstamped legacy row — a stamped one is answered from the record), so it
    runs in a worker thread: a blocking DB call made directly here stalls the
    whole event loop, every other in-flight request included (#1573).
    """
    from fastapi import HTTPException

    from archimedes.services.trace_visibility import can_read_trace

    caller_user_id, caller_wallet = _caller_identity(request)
    allowed = await asyncio.to_thread(can_read_trace, trace, caller_wallet, caller_user_id=caller_user_id)
    if not allowed:
        raise HTTPException(status_code=404, detail="Trace not found")


@traces_router.get("/", response_model=TraceListResponse)
async def list_traces(
    request: Request,
    vault_address: str | None = None,
    decision_type: str | None = Query(None, pattern="^(construction|rebalance|rotation|regime_change|skip)$"),
    strategy_id: str | None = Query(
        None,
        # min_length=1 is a GATE, not validation. `?strategy_id=` (present but
        # empty) is falsy, so a truthiness check would have skipped
        # assert_strategy_visible and served the whole unfiltered feed — the
        # gate bypassed by an empty value it was never asked about. FastAPI
        # rejects the empty value with 422 before the handler runs, and the
        # handler additionally branches on `is not None` rather than on
        # truthiness so the gate cannot be re-opened by relaxing this.
        min_length=1,
        description=(
            "Restrict to DECISION traces (rebalance / rotation / regime_change / skip) whose "
            "strategies_referenced contains exactly this strategy id. Construction and "
            "generation traces are excluded: their strategies_referenced holds paper anchors and "
            "arXiv ids, not strategy ids. Subject to the same visibility gate as "
            "GET /api/strategies/{id} — a strategy you cannot read is 404 here too."
        ),
    ),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """List reasoning traces -- merges on-chain IDs with off-chain metadata.

    Scoped to what the caller may read (#1556): house-public traces for an
    anonymous caller, plus their own for a signed-in one. ``vault_address``
    stays a *filter* — it narrows the visible set, it never widens it — so
    dropping it can no longer enumerate the platform.

    ``strategy_id`` needs a gate of its own, and it is not about the traces.
    Reporting "0 traces" vs "3 traces" for an id is an existence oracle for the
    *strategy*, and generated strategies are private-until-published (#850). So
    the scoped listing runs ``assert_strategy_visible`` first and 404s exactly
    as ``GET /api/strategies/{id}`` does — same card-level gate, one
    implementation, never a 403 (which would confirm the id). Card-level is the
    right tier: existence is card content, so a published strategy is scopeable
    by anyone, while ``/debate`` and ``/returns`` gate one tier tighter on
    ``is_strategy_reasoning_visible`` (#1557) because they disclose the
    derivation. The #1556 per-row filter still runs afterwards: passing the
    strategy gate grants no read on a trace you do not own.
    """
    from archimedes.services.redis_state import AgentStateStore
    from archimedes.services.trace_visibility import (
        MAX_TRACE_SCAN,
        is_trace_visible,
        safe_resolve_vault_owners,
        trace_owner_view,
    )

    if strategy_id is not None:
        assert_strategy_visible(strategy_id, request)

    caller_user_id, caller_wallet = _caller_identity(request)

    state = AgentStateStore()
    try:
        try:
            # Fetch the whole candidate set and window AFTER filtering. Windowing
            # first would return short pages whose length leaks how many of
            # somebody else's traces were skipped.
            off_chain_traces, _ = await state.list_traces(
                vault_address=vault_address,
                decision_type=decision_type,
                strategy_id=strategy_id,
                limit=MAX_TRACE_SCAN,
                offset=0,
            )
        except Exception:
            logger.warning("list_traces: Redis unavailable — falling back to on-chain-only listing", exc_info=True)
            off_chain_traces = []

        if off_chain_traces:
            # One batched ownership lookup for the whole page, and only for rows
            # that lack an on-record stamp — a stamped row needs no DB at all.
            # `safe_resolve_vault_owners` memoizes per vault, so the handful of
            # distinct vaults behind a 2000-row candidate set costs at most one
            # lookup each per TTL rather than one per request; `to_thread` keeps
            # the synchronous session it may still open off the event loop,
            # where it was blocking every other in-flight request (#1573).
            owners = await asyncio.to_thread(
                safe_resolve_vault_owners,
                {
                    str(t.get("vault_address") or "")
                    for t in off_chain_traces
                    if not (t.get("owner_user_id") or t.get("owner_wallet"))
                },
            )
            visible = [
                t
                for t in off_chain_traces
                if t.get("trigger") != "empty_vault"
                and is_trace_visible(trace_owner_view(t, owners), caller_wallet, caller_user_id=caller_user_id)
            ]
            # `total` counts what survived BOTH filters and the empty_vault
            # drop, so "N of TOTAL" never promises a row the caller cannot
            # reach. Windowing happens after, on the same list.
            total = len(visible)
            return TraceListResponse(
                traces=[_offchain_trace_response(t) for t in visible[offset : offset + limit]],
                total=total,
            )

        if strategy_id is not None:
            # The on-chain fallback below CANNOT answer a strategy-scoped
            # question: a registry entry is (agent, vault, hash, timestamp) and
            # records no strategy reference at all. Falling through would return
            # the whole unfiltered registry under a filter the caller asked for
            # — every row a false positive. An empty result is the honest answer
            # to "no off-chain body, so no strategy link".
            return TraceListResponse(traces=[], total=0)

        from archimedes.chain.trace_publisher import trace_publisher

        traces: list[TraceResponse] = []
        # Real registry size (#1356): read once up front so it survives even
        # if a later per-trace fetch in the loop fails partway through. The
        # old code returned `total=len(traces)` here, which is the CURRENT
        # PAGE size (capped at `limit`), not the registry size — pagination
        # reported a smaller universe than actually exists. `total_count` is
        # the value this route promises callers via `start`/`end` below; it
        # must be the same value in the response.
        total_count = 0
        try:
            total_count = await trace_publisher.get_total_trace_count()
            start = max(1, total_count - offset - limit + 1)
            end = max(1, total_count - offset)

            on_chain_details: list[tuple[int, dict]] = []
            for trace_id in range(end, start - 1, -1):
                detail = await trace_publisher.get_trace_by_id(trace_id)
                if detail is None:
                    continue

                if vault_address and detail["vault"].lower() != vault_address.lower():
                    continue

                on_chain_details.append((trace_id, detail))

            # The registry-only projection carries no reasoning body, but it
            # still names the vault and the anchor — gate it on exactly the
            # same predicate so a caller cannot enumerate another user's
            # vault's trace ids by taking Redis out of the picture.
            owners = await asyncio.to_thread(safe_resolve_vault_owners, {str(d["vault"]) for _, d in on_chain_details})
            for trace_id, detail in on_chain_details:
                view = trace_owner_view({"vault_address": detail["vault"]}, owners)
                if not is_trace_visible(view, caller_wallet, caller_user_id=caller_user_id):
                    continue
                traces.append(_anchored_only_trace(str(trace_id), detail))
        except Exception:
            logger.debug("on-chain trace listing failed", exc_info=True)

        return TraceListResponse(traces=traces, total=total_count)
    finally:
        await state.close()


@traces_router.get("/{trace_id}", response_model=TraceDetailResponse)
async def get_trace(trace_id: str, request: Request):
    """Get one reasoning trace in full, by UUID, trace hash, or on-chain id.

    Returns the reasoning text plus the rest of the hashed body — the market
    context the agent read, the portfolio before and after, and the paper hashes
    it consulted. That is the readable form of what the anchor commits to;
    ``/canonical`` remains the byte-exact form for re-hashing.

    **The widened body is exactly why the #1556 gate is load-bearing here.**
    ``portfolio_before``/``portfolio_after`` are holdings and ``market_context``
    is what the agent saw — the same fields that made ``/canonical`` the
    CRITICAL surface in that issue. Widening this route without the gate would
    have re-opened it in a friendlier format, so a non-owner gets 404 (never
    403, never a redacted 200) whether the trace resolves off-chain or only
    from the registry.

    The on-chain-only fallback widens to the same model but leaves every added
    field at its empty default, because the registry stores no body. That is a
    real absence, not a formatting choice: ``verification_mode`` is
    ``anchored_only`` on exactly that path and says so.
    """
    from fastapi import HTTPException

    from archimedes.chain.trace_publisher import trace_publisher
    from archimedes.services.redis_state import AgentStateStore

    state = AgentStateStore()
    try:
        try:
            off_chain = await state.get_trace(trace_id)
        except Exception:
            logger.warning("get_trace: Redis unavailable — falling back to on-chain-only lookup", exc_info=True)
            off_chain = None
        if off_chain:
            # Gate BEFORE projecting: the projection below is the widened body,
            # and a 404 must be indistinguishable from "no such trace".
            await _assert_can_read(off_chain, request)
            row = _offchain_trace_response(off_chain, fallback_id=trace_id)
            return TraceDetailResponse(
                **row.model_dump(),
                market_context=off_chain.get("market_context") or {},
                portfolio_before=off_chain.get("portfolio_before") or {},
                portfolio_after=off_chain.get("portfolio_after") or {},
                consulted_paper_hashes=off_chain.get("consulted_paper_hashes") or [],
                settlement_tx_hashes=off_chain.get("settlement_tx_hashes") or [],
                ipfs_cid=off_chain.get("ipfs_cid"),
            )

        try:
            int_id = int(trace_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="Trace not found") from None

        detail = await trace_publisher.get_trace_by_id(int_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Trace not found")

        await _assert_can_read({"vault_address": detail["vault"]}, request)
        return TraceDetailResponse(**_anchored_only_trace(trace_id, detail).model_dump())
    finally:
        await state.close()


@traces_router.post("/publish", response_model=TracePublishResponse)
async def publish_trace(req: TracePublishRequest, _: None = Depends(require_internal_agent_key)):
    """Publish a reasoning trace: compute hash, anchor on Arc, persist off-chain.

    Internal-only: requires X-Internal-Agent-Key header.

    Ownership (#1556) is stamped onto the stored record by
    ``AgentStateStore.save_trace``, resolved from the vault this trace is for.
    It is done there rather than here on purpose: this route is one of five
    trace write paths, and the guarantee that matters is "every persisted trace
    knows its owner", which only a single choke point can make true.
    """
    import uuid
    from datetime import datetime

    from fastapi import HTTPException

    from archimedes.chain.trace_publisher import trace_publisher
    from archimedes.services.redis_state import AgentStateStore

    try:
        dt = DecisionType(req.decision_type)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid decision_type: {req.decision_type}. "
            f"Must be one of: construction, rebalance, rotation, regime_change, skip",
        ) from None

    trace = ReasoningTrace(
        id=str(uuid.uuid4()),
        vault_address=req.vault_address,
        decision_type=dt,
        trigger=req.trigger,
        timestamp=datetime.now(UTC),
        market_context=req.market_context,
        portfolio_before=req.portfolio_before,
        portfolio_after=req.portfolio_after,
        reasoning=req.reasoning,
        confidence=req.confidence,
        trades_executed=req.trades_executed,
        strategies_referenced=req.strategies_referenced,
    )

    trace.compute_hash()

    off_chain_data = {
        "id": trace.id,
        "vault_address": trace.vault_address,
        "decision_type": trace.decision_type.value,
        "trigger": trace.trigger,
        "timestamp": trace.timestamp.isoformat(),
        "market_context": trace.market_context,
        "portfolio_before": trace.portfolio_before,
        "portfolio_after": trace.portfolio_after,
        "reasoning": trace.reasoning,
        "confidence": trace.confidence,
        "trades_executed": trace.trades_executed,
        "strategies_referenced": trace.strategies_referenced,
        "trace_hash": trace.trace_hash,
        "arc_tx_hash": None,
        "is_verified": False,
    }

    arc_tx_hash = None
    try:
        arc_tx_hash = await trace_publisher.publish(trace)
        if arc_tx_hash:
            off_chain_data["arc_tx_hash"] = arc_tx_hash
            off_chain_data["is_verified"] = True
    except Exception as e:
        logging.getLogger(__name__).error(f"On-chain publish failed: {e}")

    state = AgentStateStore()
    try:
        await state.save_trace(off_chain_data)
    except Exception:
        # WRITE path: unlike the read endpoints' graceful degradation above,
        # a failed off-chain persist must NOT return 200 — the caller would
        # get is_anchored=True for a trace whose full reasoning body was never
        # stored, i.e. an on-chain anchor that can never be re-verified against
        # its off-chain content (claim-integrity). Fail loudly and retryably;
        # include the anchor tx so the caller knows the on-chain half landed.
        logger.error("publish_trace: failed to persist off-chain trace data (Redis unavailable?)", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail=(
                f"Trace anchored on-chain (tx {arc_tx_hash}) but off-chain persistence failed — retry publish."
                if arc_tx_hash
                else "Off-chain trace persistence failed (no on-chain anchor was recorded either) — retry publish."
            ),
        ) from None
    finally:
        await state.close()

    return TracePublishResponse(
        id=trace.id,
        trace_hash=trace.trace_hash,
        arc_tx_hash=arc_tx_hash,
        is_anchored=arc_tx_hash is not None,
        timestamp=trace.timestamp.isoformat(),
        vault_address=trace.vault_address,
        decision_type=trace.decision_type.value,
    )


def _verify_consulted_papers(off_chain: dict) -> tuple[bool | None, dict | None]:
    """Check a trace's ``consulted_paper_hashes`` against the live corpus (#1637).

    This is the production caller ``services.source_tracker.verify_source_papers``
    never had. Before it, ``/verify`` re-hashed the trace body and compared it to
    the on-chain anchor — a real check, but only of the *body*. The papers the
    trace claims to have consulted were never checked against anything, so
    "verified" meant "these bytes are the bytes that were anchored", not "the
    research this decision cites exists".

    Returns ``(papers_verified, detail)``. ``papers_verified`` is **tri-state**:

    * ``True`` / ``False`` — the corpus answered and every claimed id was, or
      was not, found (and any non-empty claimed hash matched).
    * ``None`` — nothing was checked. Two ways: the trace claims no papers
      (``no_papers_claimed``), or the corpus is unreachable
      (``corpus_unavailable``). An outage must not read as a provenance
      failure, and it must not read as a pass either — that is the exact
      failure #1359 fixed on the on-chain half of this endpoint.

    **What ``True`` means, precisely: the corpus HAS these papers.** Claimed
    hashes are empty in production (#1091: the corpus's ``content_hash`` /
    ``pdf_sha256`` columns are unhydrated), so this is an EXISTENCE check, not
    a hash comparison, and ``verify_source_papers`` says so by treating an
    empty claim as "no hash asserted". Nothing here synthesizes a hash on
    either side to make a comparison come out clean, and the ``mode`` field
    exists so every renderer can state which check actually ran instead of
    printing an unqualified "verified" (owner decision Q8 on #1688).
    """
    from archimedes.services.source_tracker import (
        CorpusUnavailable,
        corpus_content_hashes,
        split_consulted_entry,
        verify_source_papers,
    )

    claimed = [e for e in (off_chain.get("consulted_paper_hashes") or []) if isinstance(e, str) and e]
    if not claimed:
        return None, {
            "mode": "no_papers_claimed",
            "checked": 0,
            "verified": None,
            "missing": [],
            "hash_mismatch": [],
        }

    arxiv_ids = [split_consulted_entry(e)[0] for e in claimed]
    try:
        resolved = corpus_content_hashes(arxiv_ids)
    except CorpusUnavailable:
        logger.warning("verify_trace: corpus unavailable — source papers NOT checked", exc_info=True)
        return None, {
            "mode": "corpus_unavailable",
            "checked": 0,
            "verified": None,
            "missing": [],
            "hash_mismatch": [],
        }

    corpus = [{"arxiv_id": aid, "content_hash": h} for aid, h in resolved.items()]
    result = verify_source_papers(claimed, corpus)
    return bool(result["verified"]), {
        "mode": "checked",
        "checked": len(claimed),
        "verified": bool(result["verified"]),
        "missing": result["missing"],
        "hash_mismatch": result["hash_mismatch"],
    }


@traces_router.get("/{trace_id}/verify", response_model=TraceVerifyResponse)
@limiter.exempt
async def verify_trace(trace_id: str, request: Request):
    """Verify a reasoning trace against its on-chain anchor AND its source papers.

    Ownership-gated (#1556) on the same predicate as the display routes: the
    response names the vault, the agent and the anchor timestamp, so an
    ungated verify is an enumeration oracle even though it carries no
    reasoning body.

    Two independent checks, reported independently (#1637):
    ``verification_mode``/``is_verified`` answer "were these bytes anchored";
    ``papers_verified``/``source_paper_verification`` answer "does the corpus
    have the papers this decision cites". Neither is folded into the other — a
    trace can be correctly anchored while citing a paper that has since left
    the corpus, and that is a fact a reader needs to see rather than have
    averaged away into one boolean.
    """
    from fastapi import HTTPException

    from archimedes.chain.trace_publisher import trace_publisher
    from archimedes.services.redis_state import AgentStateStore

    state = AgentStateStore()
    try:
        try:
            off_chain = await state.get_trace(trace_id)
        except Exception:
            # A Redis outage is a loud absence, not a verification result
            # (CLAUDE.md § fail-soft) — mirrors get_trace_canonical's 503
            # below. Previously this exception was swallowed and fell
            # through to the "no off-chain data" branch, which returns
            # is_verified=True with zero hashes compared — an outage was
            # silently upgrading every trace to a green check (#1359).
            logger.warning("verify_trace: Redis unavailable — cannot verify without the store", exc_info=True)
            raise HTTPException(
                status_code=503, detail="Trace store temporarily unavailable — retry verification."
            ) from None
        if not off_chain:
            # The store IS reachable and simply has no record for this id —
            # the only case allowed to report anchored_only.
            try:
                int_id = int(trace_id)
            except ValueError:
                raise HTTPException(status_code=404, detail="Trace not found") from None

            detail = await trace_publisher.get_trace_by_id(int_id)
            if not detail:
                raise HTTPException(status_code=404, detail="Trace not found")

            await _assert_can_read({"vault_address": detail["vault"]}, request)
            return TraceVerifyResponse(
                trace_id=int_id,
                trace_hash=detail["trace_hash"],
                is_verified=True,
                verification_mode="anchored_only",
                agent=detail["agent"],
                vault=detail["vault"],
                on_chain_timestamp=detail["timestamp"],
                details="Hash is anchored on-chain — no off-chain trace body was stored, so no hashes were compared",
                # No off-chain body means no cited set to check. Not a pass,
                # not a failure — nothing was attempted (#1637).
                papers_verified=None,
                source_paper_verification=None,
            )

        await _assert_can_read(off_chain, request)

        # to_thread: _verify_consulted_papers opens a synchronous SQLAlchemy
        # session against the corpus, and a blocking DB call made directly here
        # stalls the whole event loop — the same reason `_assert_can_read` above
        # runs in a worker thread (#1573).
        papers_verified, papers_detail = await asyncio.to_thread(_verify_consulted_papers, off_chain)

        trace_hash = off_chain.get("trace_hash", "")
        is_verified = False
        verification_mode = "failed"
        agent = ""
        vault = off_chain.get("vault_address", "")
        on_chain_ts = 0
        details = ""

        arc_tx_hash = off_chain.get("arc_tx_hash")
        if not arc_tx_hash:
            details = "Trace was not published on-chain -- cannot verify"
        else:
            try:
                # O(1): fetch the receipt for the cached arc_tx_hash and decode
                # the TracePublished event directly. Replaces the prior O(N)
                # getTracesByVault → getTraceById scan that 504'd on vaults with
                # 40+ traces.
                detail = await trace_publisher.get_trace_by_tx_hash(arc_tx_hash)
                if detail is None:
                    details = "On-chain receipt not found for cached arc_tx_hash"
                else:
                    expected = trace_hash.removeprefix("0x").lower()
                    on_chain = detail["trace_hash"].removeprefix("0x").lower()
                    if expected and expected == on_chain:
                        is_verified = True
                        verification_mode = "hash_matched"
                        agent = detail["agent"]
                        on_chain_ts = detail["timestamp"]
                        # Keep vault as recorded off-chain; surface the on-chain
                        # vault when off-chain didn't record one.
                        if not vault:
                            vault = detail["vault"]
                        details = "Hash verified on-chain ✓"
                    else:
                        details = "Hash mismatch: on-chain trace does not match off-chain hash"
            except Exception as e:
                details = f"Verification failed: {e}"

        return TraceVerifyResponse(
            trace_id=int(trace_id) if trace_id.isdigit() else 0,
            trace_hash=trace_hash,
            is_verified=is_verified,
            verification_mode=verification_mode,
            agent=agent,
            vault=vault,
            on_chain_timestamp=on_chain_ts,
            details=details,
            commit_block_number=off_chain.get("commit_block_number"),
            trade_block_number=off_chain.get("trade_block_number"),
            reveal_block_number=off_chain.get("reveal_block_number"),
            temporal_binding_valid=off_chain.get("temporal_binding_valid"),
            papers_verified=papers_verified,
            source_paper_verification=papers_detail,
        )
    finally:
        await state.close()


@traces_router.get("/{trace_id}/canonical")
async def get_trace_canonical(trace_id: str, request: Request):
    """Get the canonical JSON used to compute the trace hash.

    The most sensitive of the four reads and the reason #1556 was filed
    CRITICAL: the canonical body is the FULL hashed record —
    ``portfolio_before`` / ``portfolio_after`` (holdings) and
    ``market_context`` — so it is ownership-gated like the rest.
    """
    from fastapi import HTTPException
    from fastapi.responses import PlainTextResponse

    from archimedes.services.redis_state import AgentStateStore

    state = AgentStateStore()
    try:
        try:
            off_chain = await state.get_trace(trace_id)
        except Exception:
            # 503, not 404: "store unavailable" must stay distinguishable from
            # "trace doesn't exist" — a canonical-JSON consumer (hash
            # re-verification) should retry, not conclude the trace is gone.
            logger.warning("get_trace_canonical: Redis unavailable", exc_info=True)
            raise HTTPException(status_code=503, detail="Trace store temporarily unavailable — retry.") from None
        if not off_chain:
            raise HTTPException(status_code=404, detail="Trace not found")

        await _assert_can_read(off_chain, request)

        trace = ReasoningTrace(
            id=off_chain["id"],
            vault_address=off_chain["vault_address"],
            decision_type=DecisionType(off_chain["decision_type"]),
            trigger=off_chain["trigger"],
            timestamp=off_chain["timestamp"],
            market_context=off_chain.get("market_context", {}),
            portfolio_before=off_chain.get("portfolio_before", {}),
            portfolio_after=off_chain.get("portfolio_after", {}),
            reasoning=off_chain.get("reasoning", ""),
            confidence=off_chain.get("confidence", 0.0),
            trades_executed=off_chain.get("trades_executed", []),
            strategies_referenced=off_chain.get("strategies_referenced", []),
            # Hashed field (#903): without it the rebuilt canonical bytes can
            # never match the committed hash for paper-grounded decisions.
            consulted_paper_hashes=off_chain.get("consulted_paper_hashes", []),
        )
        return PlainTextResponse(trace.canonical_json(), media_type="application/json")
    finally:
        await state.close()
