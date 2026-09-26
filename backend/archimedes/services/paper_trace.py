"""Paper-trading reasoning traces — issue #1575.

The product's central claim is *auditable reasoning behind every move*. The
house agent honoured it; paper trading — the surface users actually touch —
did not: ``services/paper_trading.py`` re-ran the graded engine and appended
rows to ``paper_daily_returns``, and that was the entire artifact.

This module is the missing producer. It is deliberately NOT a second trace
system: it builds the same ``ReasoningTrace``, hashes it with the same
``canonical_json()`` → keccak, and publishes it through the same
``AgentStateStore.save_trace`` choke point, so #1556's ownership stamp,
#1569's passport reachability, ``/verify``, ``/canonical`` and the tamper
detection all apply to paper traces for free.

Split at the I/O seam: :func:`build_paper_trace` STOPS at the hash and touches
neither Redis nor the chain, so it is pure and testable without fixtures.
:func:`publish_paper_trace` is the only function here that does I/O. (The
retired ``services/construction_trace.py`` held the same seam; it was deleted
as a zero-caller surface, and this module is now the only place the shape
lives.)

Four honesty rules, each enforced by a test rather than asserted here:

- **No LLM.** There is no LLM in the paper settle path and there must not be
  one: a sentence written at settle time is a post-hoc rationalisation of a
  decision a deterministic engine already made, which is precisely the attack
  the commit-reveal spec exists to defeat. ``reasoning`` is rendered from the
  snapshotted spec and the legs that actually filled. Guard: a grep over this
  module for the LLM client imports.
- **Paper-ness is inside the hash.** ``trigger="paper_settle"`` and
  ``market_context.venue="paper"`` are both hashed fields, so a paper trace
  cannot be laundered into a live one without breaking ``/verify``. The same
  goes for ``trace_provenance``: a backfilled trace cannot be relabelled
  real-time.
- **``vault_address=""``, never the zero-address sentinel.** See
  :func:`publish_paper_trace`.
- **``confidence`` stays 0.0.** There is no calibrated source for it on this
  path and inventing one contradicts the selection-bias thesis the rigor gate
  enforces. The absence is stated in ``expected_outcome``.

Design: ``docs/plans/2026-08-30-paper-trading-reasoning-traces.md``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from datetime import UTC, date, datetime
from typing import Any

from archimedes.models.paper_store import (
    TRACE_DISABLED,
    TRACE_FAILED,
    TRACE_PUBLISHED,
    TRACE_UNOWNED,
)
from archimedes.models.trace import DecisionType, ReasoningTrace

logger = logging.getLogger(__name__)

#: The venue tag, inside the hash. Not decoration: it is what makes "this was
#: simulated" a property of the signed body rather than of a database column
#: someone could edit.
PAPER_VENUE = "paper"
PAPER_TRIGGER = "paper_settle"

#: Where a trace came from, also inside the hash. The first settle after this
#: ships is a bounded backfill of a deployment's past decisions; the
#: commit-reveal threat model is entirely about post-hoc trace construction,
#: so a trace written after the fact must admit it, unstrippably.
PROVENANCE_SETTLE = "settle"
PROVENANCE_BACKFILL = "backfill"

#: v1 traces ACTED decisions only. A rebalance-eligible bar where the
#: condition did not fire is a real decision (``DecisionType.SKIP``) but
#: produces no order, and an order observer cannot see it. Inferring skips by
#: re-deriving the cadence outside the engine would be a second cadence
#: implementation that can silently disagree with the first. Surfaced at the
#: API rather than papered over.
DECISION_KINDS = ("rebalance",)

_PUBLISH_ENV = "PAPER_TRACE_PUBLISH"
_ANCHOR_ENV = "PAPER_TRACE_ANCHOR"
BACKFILL_MAX_ENV = "PAPER_TRACE_BACKFILL_MAX"
_DEFAULT_BACKFILL_MAX = 500


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def publishing_enabled() -> bool:
    """Is paper-trace publishing armed? Default ON.

    Off is a legitimate operator choice, but it is never a quiet one: the
    decision still gets a durable ``disabled`` row, the settle logs a WARNING
    naming this env var, and ``trace_coverage.status`` reports ``disabled`` on
    the deployment payload. Coverage is a claim the product makes, and
    fail-soft is wrong for anything a claim depends on.
    """
    return _env_bool(_PUBLISH_ENV, True)


def anchoring_enabled() -> bool:
    """Is on-chain anchoring armed platform-wide? Default OFF.

    Two independent gates (this and ``PaperDeployment.anchor_traces``) because
    ``reveal()`` writes ``portfolio_before``/``portfolio_after`` — the user's
    holdings — on-chain **permanently**. #1556 exists precisely because a
    user-owned trace publishing its full portfolio to the internet is a leak;
    doing that by default for a simulation the user ran privately would defeat
    that gate on purpose. Consent is per deployment and, since nothing on-chain
    can be recalled, revocable going forward only.
    """
    return _env_bool(_ANCHOR_ENV, False)


def backfill_max() -> int:
    try:
        return int(os.getenv(BACKFILL_MAX_ENV) or _DEFAULT_BACKFILL_MAX)
    except ValueError:
        logger.warning("%s is not an integer — using %d", BACKFILL_MAX_ENV, _DEFAULT_BACKFILL_MAX)
        return _DEFAULT_BACKFILL_MAX


# ── The trace body (pure — no I/O below this line until publish) ──────────


def spec_sha256(spec_dict: dict) -> str:
    """Stable fingerprint of the snapshotted spec, hashed into the trace.

    The deployment grades the spec the user actually deployed; recording its
    fingerprint is what lets a reader prove the trace and the ledger row
    describe the same strategy, even after the strategy row is regenerated.
    """
    return hashlib.sha256(json.dumps(spec_dict, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _mark_price(closes: dict[date, float] | None, decision_date: date, last_leg: dict | None) -> float | None:
    """The mark for a sleeve that did NOT trade on ``decision_date``.

    Preference order, most honest first: that sleeve's close on the decision
    bar; the as-of close (the last bar at or before it, for a sleeve whose
    calendar has a hole); the price of its most recent fill. ``None`` when
    none of those exist — an absence, never a zero, because a zero mark reads
    as "this position is worthless" inside a hashed field.
    """
    if closes:
        exact = closes.get(decision_date)
        if exact is not None:
            return float(exact)
        earlier = [d for d in closes if d <= decision_date]
        if earlier:
            return float(closes[max(earlier)])
    if last_leg is not None:
        return float(last_leg["price"])
    return None


def deployment_portfolio(
    *,
    decision_date: date,
    side: str,
    sleeve_legs: dict[str, list[dict]],
    sleeve_initial_cash: float,
    sleeve_closes: dict[str, dict[date, float]] | None = None,
) -> dict[str, Any]:
    """The FULL deployment's portfolio on one side of a decision.

    ``side`` is ``"before"`` or ``"after"``.

    This is deployment-scoped, not leg-scoped, and the distinction is the whole
    reason this function exists. A deployment runs its universe as *N*
    independent dollar sleeves (``paper_trading._replay``), so a 2-sleeve
    deployment holds 2 × ``sleeve_initial_cash``. Summing only the cash carried
    on the legs of the one sleeve that happened to trade reported a deployment
    holding $200,000 as holding $100,000, and left every untraded symbol out of
    ``holdings`` entirely — in a field that is hashed and, on the anchoring
    path, written on-chain by ``reveal()``. A wrong portfolio is worse than no
    portfolio: it is a false statement with a keccak over it.

    So every sleeve contributes, whether or not it traded on this date:

    * traded — the bracketing leg's own ``cash_*``/``position_*`` (first leg of
      the date for ``before``, last for ``after``, so multiple fills on one bar
      still bracket the whole date);
    * untraded — the state carried forward from that sleeve's most recent
      earlier fill, marked at the decision bar's close (:func:`_mark_price`);
    * never traded at all — flat, holding its full opening sleeve capital.

    ``sleeve_legs`` must carry each sleeve's COMPLETE leg history, not just the
    post-deploy dates: a sleeve's state on a decision date is the sum of every
    fill before it, including fills from before the deployment opened. The
    replay starts at the feed's first bar, so that is the state the graded
    numbers are computed against too.
    """
    if side not in ("before", "after"):
        raise ValueError(f"deployment_portfolio: side must be 'before' or 'after', got {side!r}")

    holdings: dict[str, Any] = {}
    cash = 0.0
    for symbol in sorted(sleeve_legs):
        legs = sorted(sleeve_legs[symbol], key=lambda leg: (leg["decided_on"], leg["filled_on"]))
        on_date = [leg for leg in legs if leg["decided_on"] == decision_date]
        earlier = [leg for leg in legs if leg["decided_on"] < decision_date]
        if on_date:
            leg = on_date[0] if side == "before" else on_date[-1]
            size = float(leg[f"position_{side}"])
            sleeve_cash = float(leg[f"cash_{side}"])
            price: float | None = float(leg["price"])
        else:
            last = earlier[-1] if earlier else None
            size = float(last["position_after"]) if last is not None else 0.0
            sleeve_cash = float(last["cash_after"]) if last is not None else float(sleeve_initial_cash)
            price = _mark_price((sleeve_closes or {}).get(symbol), decision_date, last)
        cash += sleeve_cash
        holdings[symbol] = {
            "size": round(size, 6),
            "price": None if price is None else round(price, 6),
            "value": None if price is None else round(size * price, 6),
        }
    return {"holdings": holdings, "cash": round(cash, 6)}


def _render_reasoning(
    *,
    spec: Any,
    strategy_id: str,
    decision_date: date,
    legs: list[dict],
    provenance: str,
    fingerprint: str,
) -> str:
    """Deterministic, spec-derived prose. No LLM — see the module docstring."""
    lines = [
        f"Rebalance (paper) for strategy {strategy_id} on {decision_date.isoformat()}.",
        f'Spec: "{spec.name}" — rebalance_frequency={spec.rebalance_frequency}, '
        f"universe={sorted(spec.asset_universe)}.",
        f"Entry condition {json.dumps(spec.entry, sort_keys=True)}; "
        f"exit condition {json.dumps(spec.exit, sort_keys=True)}.",
        f"Position sizing: {json.dumps(spec.position_sizing, sort_keys=True)}.",
    ]
    for leg in legs:
        verb = "enter" if leg["side"] == "buy" else "exit"
        lines.append(
            f"Action: {verb} {leg['symbol']}, {abs(float(leg['size'])):g} shares @ "
            f"{float(leg['price']):.4f} (commission {float(leg['commission']):.4f}); "
            f"decided on the {leg['decided_on']} bar, filled on the {leg['filled_on']} bar."
        )
    lines.append(f"Provenance: {provenance}; graded spec snapshot sha256={fingerprint}; no LLM produced this text.")
    return "\n".join(lines)


def build_paper_trace(
    *,
    deployment_id: str,
    strategy_id: str,
    spec: Any,
    spec_dict: dict,
    decision_date: date,
    legs: list[dict],
    portfolio_before: dict,
    portfolio_after: dict,
    provenance: str = PROVENANCE_SETTLE,
    paper_hashes: list[str] | None = None,
) -> ReasoningTrace:
    """Build and hash one paper decision's trace. Pure; no chain or Redis I/O.

    ``legs`` are the executed order legs for this decision date, as produced by
    the observer-only decision journal. One trace per ``(deployment,
    decision_date)``: the universe runs as independent dollar sleeves, so a
    single decision date can carry several symbol legs, and the user-visible
    unit of "a move" is the date. The legs land in ``trades_executed``.

    ``portfolio_before``/``portfolio_after`` are the FULL deployment's holdings
    and cash on each side of the decision — every sleeve, including the ones
    that did not trade — as produced by :func:`deployment_portfolio`. They are
    REQUIRED rather than derived from ``legs`` on purpose: this builder cannot
    see the sleeves it was not handed legs for, and a leg-derived snapshot
    silently under-reports both cash and holdings on any multi-symbol universe.
    Both fields are hashed and, on the opt-in anchoring path, land on-chain via
    ``reveal()``.

    ``timestamp`` is the DECISION BAR's date at 00:00 UTC, not wall clock —
    the honest decision time, and a hashed field, so a backfilled trace cannot
    claim to have been written when it was not.
    """
    if not legs:
        raise ValueError("build_paper_trace: a decision with no legs is not a decision")
    if provenance not in (PROVENANCE_SETTLE, PROVENANCE_BACKFILL):
        raise ValueError(f"build_paper_trace: unknown provenance {provenance!r}")
    for name, snapshot in (("portfolio_before", portfolio_before), ("portfolio_after", portfolio_after)):
        if not isinstance(snapshot, dict) or "holdings" not in snapshot or "cash" not in snapshot:
            raise ValueError(
                f"build_paper_trace: {name} must be a deployment-scoped snapshot "
                f"{{'holdings': …, 'cash': …}} from deployment_portfolio(), got {snapshot!r}"
            )

    fingerprint = spec_sha256(spec_dict)
    trace = ReasoningTrace(
        # DETERMINISTIC, not uuid4: `id` is a hashed field, so a random id
        # would make two builds of the same decision hash differently and
        # there would be no way to tell a re-derived decision from a changed
        # one. Deriving it from the decision key also makes a re-publish
        # overwrite the same record instead of minting a duplicate.
        id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"archimedes:paper-trace:{deployment_id}:{decision_date.isoformat()}")),
        # NOT the zero-address sentinel (`0x000…000`, the value the retired
        # construction_trace.py used for "no vault yet"). That sentinel is a
        # non-blank address, and `is_public_trace_vault` returns True for any non-blank
        # address while `PUBLIC_TRACE_VAULTS` is unarmed (it is set nowhere in
        # this tree), so an unstamped trace on the sentinel would be
        # WORLD-READABLE, holdings and all. A blank vault is fail-closed twice:
        # the owner stamp, and then the blank-vault floor if the stamp is ever
        # missing.
        vault_address="",
        # "rebalance", never "paper_rebalance". #1569's matcher is a frozenset
        # of {rebalance, rotation, regime_change, skip} and `list_traces`'
        # decision_type regex is the same set; a new value fails the frozenset
        # SILENTLY — the passport would say "no traces for this strategy" while
        # traces existed. The venue is disclosed by `trigger` and
        # `market_context.venue`, both hashed.
        decision_type=DecisionType.REBALANCE,
        trigger=PAPER_TRIGGER,
        timestamp=datetime(decision_date.year, decision_date.month, decision_date.day, tzinfo=UTC),
        market_context={
            "venue": PAPER_VENUE,
            "deployment_id": deployment_id,
            "strategy_id": strategy_id,
            "decided_on": decision_date.isoformat(),
            "filled_on": sorted({str(leg["filled_on"]) for leg in legs}),
            "rebalance_frequency": spec.rebalance_frequency,
            "asset_universe": sorted(spec.asset_universe),
            "source_arxiv_ids": sorted(spec.source_arxiv_ids),
            "spec_sha256": fingerprint,
            "trace_provenance": provenance,
            "decision_kinds": list(DECISION_KINDS),
        },
        # Deployment-scoped, not leg-scoped — see the parameter docs above and
        # :func:`deployment_portfolio`.
        portfolio_before=portfolio_before,
        portfolio_after=portfolio_after,
        reasoning=_render_reasoning(
            spec=spec,
            strategy_id=strategy_id,
            decision_date=decision_date,
            legs=legs,
            provenance=provenance,
            fingerprint=fingerprint,
        ),
        # No calibrated source on this path; the absence is stated, not filled
        # with a plausible float.
        confidence=0.0,
        expected_outcome=(
            "Paper (simulated) execution — no capital moved and no counterparty exists. "
            "The trace is hashed and owner-verifiable off-chain; it is NOT anchored on-chain "
            "unless the deployment opted in, so it carries no third-party proof that the "
            "reasoning preceded the fill. No confidence score is asserted: there is no "
            "calibrated source for one on this path."
        ),
        # `symbol`/`direction`/`amount` are the shape `TradeExecutedResponse`
        # requires — the SAME conformance argument as `decision_type`. A leg
        # keyed on "side" instead validates nowhere and 500s GET /api/traces/,
        # which makes the trace unreachable just as surely as a non-conforming
        # decision_type does, only louder and later. The paper-specific
        # numbers ride alongside: they are inside the hash, and the wire
        # schema ignores what it does not declare.
        trades_executed=[
            {
                "symbol": leg["symbol"],
                "direction": leg["side"],
                # Shares, not USDC. `value_usdc` is deliberately left unset:
                # paper legs are not denominated in USDC and asserting a
                # dollar figure here would be inventing one.
                "amount": round(abs(float(leg["size"])), 6),
                "size": round(float(leg["size"]), 6),
                "price": round(float(leg["price"]), 6),
                "value": round(float(leg["value"]), 6),
                "commission": round(float(leg["commission"]), 6),
            }
            for leg in legs
        ],
        # Exactly one element, exact string equality — #1569's matcher compares
        # whole strings, and `strategy_store.id` is `content_hash[:16]`, which
        # the deployment already carries as an FK. No prefixes, no composites.
        strategies_referenced=[strategy_id],
        # Only ids whose content hash actually resolves (see
        # :func:`resolve_paper_hashes`). The bare ids are always in
        # `market_context.source_arxiv_ids`, so absence is visible here rather
        # than filled with a half-formed "2301.00001:".
        consulted_paper_hashes=sorted(paper_hashes or []),
    )
    trace.compute_hash()
    return trace


def resolve_paper_hashes(session, arxiv_ids: list[str]) -> list[str]:
    """``["arxiv_id:content_hash", …]`` for ids whose hash RESOLVES.

    Reads the corpus on the CALLER'S session (#1818 P2). It used to open its
    own — ``with get_session() as session:`` — while the caller's transaction
    was still open, and that second connection is one half of the wedge that
    took production down for 94 minutes on 2026-09-03
    (``docs/incidents/2026-09-03-paper-advance-ddl-wedge.md``, mechanism item
    3): the cycle transaction held ``AccessShareLock`` on ``papers``, the
    sibling task's ``ALTER TABLE papers`` queued an ``AccessExclusiveLock``
    behind it, and this function's session then queued behind the ALTER. The
    cycle waited on itself through two connections, which PostgreSQL cannot
    see — no deadlock detection, no timeout, no log line. P1 removed the DDL
    from the cycle path; taking the caller's session removes the other half,
    so the shape cannot come back the next time something takes an exclusive
    lock on ``papers``. ``session`` is REQUIRED rather than optional for the
    same reason: an ``= None`` default would leave a live route back to the
    second connection, and every production caller already has a session in
    hand.

    The field's contract is ``"arxiv_id:content_hash"``. The snapshotted spec
    carries ``source_arxiv_ids`` but no content hash, and the prod corpus has
    ``corpus_meta = 0``, so for most deployments nothing resolves. Emitting
    ``"2301.00001:"`` would be a half-formed value that reads as provenance;
    an empty list reads as the absence it is.

    Fail-soft is correct here for a corpus OUTAGE and nothing else: a database
    that is down or a corpus table that does not exist must not block the
    trace, and an id that does not appear is *already* recorded in
    ``market_context.source_arxiv_ids`` — nothing is claimed that isn't there.

    Sharing the caller's session is what makes the SAVEPOINT necessary. A
    failed statement aborts the whole PostgreSQL transaction, so without one a
    missing ``papers`` table would no longer cost this lookup — it would cost
    the caller everything after it: ``advance_all``'s per-deployment
    ``except Exception`` would catch a ``PendingRollbackError`` for every
    remaining deployment and the final ``session.commit()`` would raise, i.e.
    the fail-soft return below would be a lie about a whole wedged cycle.
    ``session.begin_nested()`` scopes the CORPUS QUERY's damage, so the
    caller's work after it still commits. What the savepoint does NOT scope is
    the caller's work BEFORE it: ``begin_nested()`` flushes any pending ORM
    state first, outside the savepoint it is about to open — see the comment at
    the call, which is why that call sits outside the ``try``. A
    connection-level failure is not recoverable either way — but then the
    cycle's own writes were never going to commit, so nothing is being
    laundered.

    It is NOT correct for a bug in this function, which is what the original
    ``except Exception`` around the imports converted a typo into. The class is
    ``PaperRecord``; this imported ``Paper``, so every call raised
    ``ImportError``, was swallowed, returned ``[]``, and logged a WARNING on
    every settle — a permanent "nothing resolves" that was indistinguishable
    from the honest empty result the docstring above describes.

    So the imports sit OUTSIDE the try, and the catch is ``DBAPIError`` — the
    database-side family (connection refused, table absent, permission denied),
    which is what "the corpus is unavailable" actually raises. Deliberately NOT
    the ``SQLAlchemyError`` base: that also covers ``ArgumentError`` /
    ``InvalidRequestError``, which is how a wrong model class or a malformed
    query surfaces, and those are this module being wrong rather than the
    corpus being down. ``ImportError``/``AttributeError``/``NameError`` reach
    the caller for the same reason.
    """
    wanted = [i for i in (arxiv_ids or []) if i]
    if not wanted:
        return []

    from sqlalchemy.exc import DBAPIError

    from archimedes.models.corpus_store import PaperRecord

    # The savepoint is opened OUTSIDE the ``try``, deliberately.
    # ``begin_nested()`` FLUSHES before it emits any SAVEPOINT:
    # ``SessionTransaction.__init__`` calls ``_take_snapshot``, which runs
    # ``self.session.flush()`` for a BEGIN_NESTED origin (sqlalchemy 2.0,
    # ``orm/session.py``), and ``SessionLocal`` is ``autoflush=False`` — so the
    # caller's pending ORM rows are written by this function's own savepoint,
    # before that savepoint exists and outside its protection.
    #
    # With this call inside the ``try``, an ``IntegrityError`` from THAT flush
    # — a ``DBAPIError`` like any other — was caught by the corpus handler
    # below. Two lies at once: the trace was stored and HASHED with
    # ``consulted_paper_hashes=[]`` while the corpus was perfectly healthy, and
    # on PostgreSQL the caller's transaction was already aborted with no
    # savepoint to roll back to — exactly the wedge the savepoint exists to
    # prevent, manufactured by the savepoint's own flush. Out here that failure
    # reaches the caller as the honest per-deployment failure it is, and the
    # catch covers the corpus query and nothing else.
    nested = session.begin_nested()
    try:
        with nested:
            rows = session.query(PaperRecord).filter(PaperRecord.arxiv_id.in_(sorted(set(wanted)))).all()
    except DBAPIError:
        logger.warning("paper trace: corpus content-hash lookup failed — recording no consulted hashes", exc_info=True)
        return []

    # Outside the try: an attribute this code is wrong about is a bug, not an
    # outage, and must not be laundered into an empty list.
    return sorted(
        f"{row.arxiv_id}:{content_hash}" for row in rows if (content_hash := (row.content_hash or row.pdf_sha256 or ""))
    )


# ── Publishing (the only I/O in this module) ─────────────────────────────


def _run_coro(coro):
    """Run a coroutine from a sync caller, on or off an event loop.

    ``advance_deployment`` is sync and is called from two places: the
    scheduler (``asyncio.to_thread`` — no running loop) and ``POST
    /api/paper/deployments`` (inside a running loop). ``asyncio.run`` raises in
    the second case, so a running loop gets a dedicated thread with its own
    loop. The Redis client is created inside whichever loop runs it, so no
    handle ever crosses loops.
    """
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


async def _save(trace: ReasoningTrace, owner_user_id: str | None, owner_wallet: str | None) -> None:
    from archimedes.services.redis_state import AgentStateStore

    state = AgentStateStore()
    try:
        await state.save_trace(
            {
                "id": trace.id,
                "vault_address": trace.vault_address,
                # #1556: `save_trace` resolves the owner FROM THE VAULT, and a
                # paper deployment has no vault. Setting either key suppresses
                # that lookup — including when the value is None — so the
                # deployment's own ownership columns are copied verbatim rather
                # than guessed. No DB round-trip at read time, and a Postgres
                # outage cannot downgrade a private paper trace to a public one.
                "owner_user_id": owner_user_id,
                "owner_wallet": owner_wallet,
                "decision_type": trace.decision_type.value,
                "trigger": trace.trigger,
                "timestamp": trace.timestamp.isoformat(),
                "market_context": trace.market_context,
                "portfolio_before": trace.portfolio_before,
                "portfolio_after": trace.portfolio_after,
                "reasoning": trace.reasoning,
                "confidence": trace.confidence,
                "expected_outcome": trace.expected_outcome,
                "trades_executed": trace.trades_executed,
                "strategies_referenced": trace.strategies_referenced,
                "consulted_paper_hashes": trace.consulted_paper_hashes,
                "trace_hash": trace.trace_hash,
                # Nothing is anchored by default (§6). The honest values are a
                # null tx and an unverified flag — the same shape the agent
                # runner's no-trade path already persists, for a different
                # reason. The UI must render this as "not anchored (paper)",
                # never "anchor pending", which asserts a registry write that
                # was never attempted.
                "arc_tx_hash": None,
                "is_verified": False,
            }
        )
    finally:
        await state.close()


def publish_paper_trace(dep, trace: ReasoningTrace) -> tuple[str, str | None]:
    """Publish one paper trace. Returns ``(status, error)``; never raises.

    ``status`` is one of the four ``TRACE_*`` constants. It never raises
    because a Redis outage must not freeze every user's paper ledger — the
    ledger is the honest number of record and it keeps advancing. Per the
    fail-soft rule, though, the *absence* is loud and durable: the caller
    writes the status to Postgres in the same transaction as the ledger rows,
    stamps ``trace_gap_at``, logs, and surfaces it on the API. A visible gap
    beats an invisible stall.
    """
    if not publishing_enabled():
        logger.warning(
            "paper: deployment %s decision %s NOT traced — %s is off. The deployment's "
            "provenance claim has a hole in it for this decision and trace_coverage will "
            "report status=disabled until it is re-attempted with publishing armed.",
            dep.id,
            trace.market_context.get("decided_on"),
            _PUBLISH_ENV,
        )
        return TRACE_DISABLED, f"{_PUBLISH_ENV} is off"

    owner_user_id = getattr(dep, "owner_user_id", None)
    owner_wallet = getattr(dep, "owner_wallet", None)
    if not owner_user_id and not owner_wallet:
        # A trace we cannot scope is worse than no trace, and a silent skip is
        # worse than both: with neither ownership column set the read gate
        # falls through to the house-vault floor, and a blank vault is the only
        # reason that is not a leak. Fail closed and say so.
        logger.error(
            "paper: deployment %s has NEITHER owner_user_id NOR owner_wallet — refusing to publish a "
            "trace that cannot be ownership-scoped. Decision %s is recorded as an unowned gap.",
            dep.id,
            trace.market_context.get("decided_on"),
        )
        return TRACE_UNOWNED, "deployment carries no ownership identity"

    try:
        _run_coro(_save(trace, owner_user_id, owner_wallet))
    except Exception as exc:
        logger.warning(
            "paper: deployment %s decision %s trace publish FAILED (%s: %s) — the ledger still "
            "advances, the gap is recorded durably and retried on the next settle.",
            dep.id,
            trace.market_context.get("decided_on"),
            type(exc).__name__,
            exc,
        )
        return TRACE_FAILED, f"{type(exc).__name__}: {exc}"

    if anchoring_enabled() and getattr(dep, "anchor_traces", False):
        # Both gates on. Left as an explicit, reachable-only-here branch rather
        # than silently doing nothing: the existing commit-reveal publisher is
        # the intended path (no new contract surface), and wiring it is the
        # follow-up the design names. Announcing it would be a claim we cannot
        # yet back, so it is a WARNING that the opt-in did not take effect.
        logger.warning(
            "paper: deployment %s opted into anchoring and %s is on, but the paper anchor path is not "
            "wired yet — the trace is published off-chain and hashed, NOT anchored. arc_tx_hash stays "
            "null and is_verified stays false rather than claiming an anchor that does not exist.",
            dep.id,
            _ANCHOR_ENV,
        )

    return TRACE_PUBLISHED, None
