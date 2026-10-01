"""Strategy endpoints — /api/strategies/*.

Includes: library listing, signals, frontier, correlation, advisor, stress.

**This module hosts no generation route.** The flag-gated fusion bypass
(``POST /api/strategies/generate`` → ``_run_fusion_job``, plus its
``GET /api/strategies/generate/{job_id}`` poll partner) was removed on
2026-08-31; the debate society at ``POST /api/generate/start`` is the sole
generation pipeline, per
``docs/adr/debate-society-sole-generation-pipeline.md``. The route table is
guarded by ``backend/tests/test_sole_generation_route_guard.py`` — adding a
generation route back here fails that test.

**The read routes here do their database work on a worker thread (#1818 P4).**
Every one of them is ``async def`` and every one of them is synchronous and
blocking underneath — ``session.query``, and on ``GET /`` a cohort PBO compute
that measured 6s on a healthy task. On 2026-09-03 that combination took the
site down: ``GET /api/strategies/generated`` blocked on a lock for 5,648,772 ms,
and because it was blocking THE EVENT LOOP, ``/health`` — a sibling coroutine
on the same loop — stopped answering too. The ALB saw two dead targets and
served 504s to everything, so a single slow query became a full outage. Off the
loop, the same blocked query costs one pool thread and every other route
(``/health`` included) keeps being served.

``asyncio.to_thread``, not a private pool: it runs on the loop's default
executor, which ``main.py`` sizes explicitly (``_install_default_executor``,
floor 16) so serving handlers and the generation fan-out share a pool somebody
chose rather than one CPython picked. Note the Postgres pool is 5 + 10 overflow
(``db._get_engine_kwargs``): past 15 concurrent handlers the wait moves to the
connection pool — still off the loop, still not an outage, but that is the next
number to raise if this tier ever saturates.

Each route is a docstring plus ONE ``await asyncio.to_thread(_…_sync, …)``. The
blocking body lives in the module-level ``_…_sync`` twin directly above it, so a
reviewer can see the whole boundary in one screen and
``backend/tests/test_handlers_off_the_loop.py`` can assert, by running them,
that no ``session.query`` on these routes happens on the loop thread.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC

from fastapi import APIRouter, Depends, Query, Request, Response

from archimedes.api._route_helpers import strategy_provider
from archimedes.api.account_auth import CurrentUser, get_current_user, require_current_user
from archimedes.api.limiter import limiter
from archimedes.api.schemas import (
    SignalResponse,
    StrategyListResponse,
    StrategyResponse,
    StrategyReturnsResponse,
    StrategySignalResponse,
    StrategySignalsResponse,
)
from archimedes.api.selection_bias_routes import (
    _SCOPE_CURATED_SELF_CONTAINED,
    _num_trials_for_generated_row,
)
from archimedes.api.wallet_routes import get_linked_wallet_address
from archimedes.models.strategy import Strategy, StrategyStatus
from archimedes.services.passport_spec_parity import reconcile_card_fields

logger = logging.getLogger(__name__)

strategies_router = APIRouter(prefix="/api/strategies", tags=["strategies"])


# The four-state verdict a surface serves for a strategy no stored passport row
# knows about, and the fail-closed answer when the row exists but was never
# graded. Same word, same meaning as ``passport_loader.STATUS_PENDING`` and
# ``ui/src/rigorGateStatus.js`` — "no gate has looked at this", never "it lost".
_UNGRADED_STATUS = "pending"


def served_status(stored_status: str | None, rigor_gate_status: str | None, *, promote: bool = True) -> str:
    """The lifecycle status a CARD shows, derived from the stored verdict.

    A curator hand-declares a strategy file's ``STATUS`` (candidate / validated /
    live / retired). A ``candidate`` whose stored rigor verdict is ``pass`` is
    shown as ``validated``: the promotion is what "Archimedes Verified" means on
    a curated card, and it is the only way a curated row reaches that word.

    **This is a derivation of a STORED value, not a recompute.** Before #1746's
    PR-B the same promotion was driven by a live ``run_rigor_gate`` call made
    during the request, which is how ``GET /api/strategies/{id}`` came to answer
    ``validated`` for a strategy whose own passport row said ``candidate``. The
    inputs are now two columns of one row, so any surface that reads that row
    can produce the same answer — and both of them do:
    ``GET /api/strategies/passports/{id}`` publishes it as ``served_status``
    beside the persisted ``status`` it has always published.

    ``status`` stays the PERSISTED column on the passport payload on purpose: the
    ``?status=`` filter queries that column, so overwriting it in the payload
    would make the list and its own filter disagree.

    ``promote`` is False for a GENERATED strategy, and that asymmetry is the
    truth rather than an oversight: a curated strategy's status is hand-declared
    in its file (``STATUS = "candidate"``) and promotion is the only way a
    curated card reaches "validated", while a generated strategy's status is
    already written by the pipeline that produced it — ``_passport_to_strategy_response``
    serves ``record.status`` verbatim. Passing the flag makes ``served_status``
    equal to what the detail route serves for EVERY id, curated or generated,
    which is the property the parity guard rests on.
    """
    if promote and (stored_status or "").lower() == StrategyStatus.CANDIDATE.value and rigor_gate_status == "pass":
        return StrategyStatus.VALIDATED.value
    return stored_status or StrategyStatus.CANDIDATE.value


def stored_passports_for(session, strategy_ids: list[str]) -> dict:
    """The stored passport rows for ``strategy_ids``, keyed by id — ONE query.

    The read half of the verdict of record: every curated surface (detail, list,
    leaderboard) resolves its rows through here and serves what it finds, so the
    three cannot answer differently for one id. A missing row is a missing key,
    and the caller serves the ungraded shape for it — never a fabricated verdict.

    Never raises: a DB failure degrades to ``{}``, i.e. every strategy reads
    ungraded. Fail-closed — the badge can go grey, never green, on an error.
    """
    if not strategy_ids:
        return {}
    try:
        from archimedes.models.strategy_passport_record import StrategyPassportRecord

        rows = session.query(StrategyPassportRecord).filter(StrategyPassportRecord.id.in_(strategy_ids)).all()
        return {r.id: r for r in rows}
    except Exception as exc:
        logger.warning("stored passports read failed for %d ids (all → ungraded): %s", len(strategy_ids), exc)
        return {}


def _to_strategy_response(s: Strategy, stored=None) -> StrategyResponse:
    """Map a curated ``Strategy`` + its STORED passport row to the API schema.

    ``stored`` is the ``StrategyPassportRecord`` for ``s.id`` — the verdict of
    record (``docs/adr/rigor-verdict-of-record.md``). Every claim this response
    makes about the rigor gate, and every number it shows, is READ from that
    row. Nothing here recomputes a verdict, and nothing here re-resolves the
    display-metric chain.

    **What this replaces, and why (#1746).** This function used to run the live
    gate over the whole library on every call (``_live_verdict_and_result_for_one``)
    and to resolve the ``real_* → backtest → stub`` display chain per request.
    ``GET /api/strategies/passports/{id}`` did neither — it is a pure read of the
    passport row. So one strategy id had two answers: ``1f9cfe96…``
    (``harvey_2018_volatility_targeting``) served ``rigor_gate_status: "pass"``
    with Sharpe ``0.406`` on the detail route and ``candidate`` / ``false`` /
    ``null`` on its own passport, and the Sharpe moved between two reads 37s
    apart because the provider memoises its backtest map per process and prod
    runs two tasks. Both halves are gone: the grade is written by
    ``services.curated_grading`` when a curated backtest runs, the display chain
    is resolved by the passport sync (``services.curated_metrics``), and this
    function serves the row both of them wrote.

    ``stored=None`` means no passport row was found for this strategy. The
    response is then the honest ungraded shape — ``pending``, no gate numbers —
    with the display metrics resolved from the provider as a fallback, so a
    caller holding a bare ``Strategy`` (a just-extracted file, a unit test) still
    gets its card rather than an empty one.
    """
    from archimedes.api.schemas import PaperRefResponse
    from archimedes.services.curated_metrics import (
        DisplayMetrics,
        display_metrics_source,
        resolve_display_metrics,
    )
    from archimedes.services.return_source_classifier import classify_strategy

    bt = strategy_provider().get_backtest_result(s.id)
    # has_real: a BacktestResultRecord (persisted daily-returns row) exists.
    # Strictly tied to that row so ``is_backtest_placeholder`` is honest: False
    # ONLY when we have actual persisted run data a gate could grade.
    has_real = bt is not None
    return_source, return_source_note = classify_strategy(s)

    # ── The verdict of record, read verbatim ────────────────────────────────
    rigor_status = (stored.rigor_gate_status or _UNGRADED_STATUS) if stored is not None else _UNGRADED_STATUS
    # Derived from the four-state and nothing else, exactly as the generated
    # path does it (`_passport_to_strategy_response`), so the boolean and the
    # status cannot be served apart even on a row whose columns were forced
    # apart by hand.
    passes = rigor_status == "pass"
    # ``graded_at`` is set by, and only by, ``_apply_rigor_verdict``. It is the
    # one field that separates "a gate produced this row's numbers" from "the
    # #1187 FIXTURE snapshot did" — curated passports carried fixture DSR/PBO
    # values in the gate-number columns until PR-B stopped writing them there,
    # and those rows are still in the table until the grading job runs. No
    # ``graded_at`` ⇒ no numbers, which is the same fail-closed answer the live
    # gate used to give when it could not run.
    graded = stored is not None and stored.graded_at is not None

    if stored is not None:
        metrics = DisplayMetrics(
            sharpe_ratio=stored.sharpe_ratio,
            sortino_ratio=stored.sortino_ratio,
            cagr=stored.cagr,
            max_drawdown=stored.max_drawdown,
            win_rate=stored.win_rate,
            calmar_ratio=stored.calmar_ratio,
            correlation_to_spy=stored.correlation_to_spy,
            total_trades=stored.total_trades,
            backtest_start=stored.backtest_start,
            backtest_end=stored.backtest_end,
            # Which LINK of the chain the sync resolved these from — READ off
            # the row the sync wrote it on, beside the numbers it names.
            # Deriving it here instead would re-decide it from the provider's
            # boot-time backtest memo, and a task whose memo predates the write
            # would label a real persisted-backtest number "stub_placeholder".
            # The fallback covers a row written before the column existed; it is
            # the old behaviour, and it degrades a label, never a number.
            source=stored.display_metrics_source or display_metrics_source(s, bt),
        )
    else:
        metrics = resolve_display_metrics(s, bt)

    # Build papers list from passport
    papers_list = [
        PaperRefResponse(
            arxiv_id=p.arxiv_id,
            # `or None` (#1637): a blank title is not a title. `""` renders as
            # an empty pair of quotes on the passport; `null` renders as the
            # absence it is.
            title=p.title or None,
            authors=p.authors,
            doi=p.doi,
            venue=p.venue,
            year=p.year,
            citation_count=p.citation_count,
            contribution=p.contribution,
            role=getattr(p, "role", None) or "cited",
            selection_rank=getattr(p, "selection_rank", None),
            semantic_score=getattr(p, "semantic_score", None),
            content_hash=getattr(p, "content_hash", None),
        )
        for p in s.papers
    ]

    return StrategyResponse(
        id=s.id,
        papers=papers_list,
        # Legacy scalar fields from papers[0]
        paper_arxiv_id=s.paper_arxiv_id or None,
        # `or None` (#1637, acceptance 12): a zero-paper passport has no paper
        # title, and `""` printed as `""` on the card.
        paper_title=s.paper_title or None,
        paper_authors=s.paper_authors,
        methodology_summary=s.methodology_summary,
        asset_universe=s.asset_universe,
        universe_source=s.universe_source,
        position_sizing=s.position_sizing.value,
        rebalance_frequency=s.rebalance_frequency.value,
        # Promoted from the STORED verdict by the shared derivation the passport
        # payload publishes as ``served_status`` — see that function.
        status=served_status(s.status.value, rigor_status),
        paper_venue=s.paper_venue,
        paper_year=s.paper_year,
        paper_doi=s.paper_doi,
        paper_citation_count=s.paper_citation_count,
        methodology_hash=s.methodology_hash,
        extraction_llm=s.extraction_llm,
        curator_wallet=s.curator_wallet,
        curator_note=s.curator_note,
        on_chain_registration_tx=s.on_chain_registration_tx,
        paper_claimed_sharpe=bt.paper_claimed_sharpe if bt else s.paper_claimed_sharpe,
        paper_claim_blended_sharpe=s.paper_claim_blended_sharpe,
        # Display metrics: the STORED answer to the `real_* -> persisted backtest
        # -> stub` chain, resolved once by the passport sync. Same precedence and
        # same numbers as before; what changed is that they are decided by a
        # writer instead of re-decided per request per process.
        sharpe_ratio=metrics.sharpe_ratio,
        sortino_ratio=metrics.sortino_ratio,
        cagr=metrics.cagr,
        max_drawdown=metrics.max_drawdown,
        win_rate=metrics.win_rate,
        calmar_ratio=metrics.calmar_ratio,
        correlation_to_spy=metrics.correlation_to_spy,
        total_trades=metrics.total_trades,
        # Numeric rigor fields: the four numbers the GRADE produced, read off the
        # same row as the badge, so a badge from one gate run can never stand
        # beside numbers from another. An ungraded row serves None — the API's
        # honest "not run" — and NEVER the s.<field>/bt.<field> fixture values
        # (#1187: that snapshot predates the current DSR convention and the gate
        # threshold, and cannot be reproduced by any single code version).
        deflated_sharpe_ratio=(stored.deflated_sharpe_ratio if graded else None),
        dsr_p_value=(stored.dsr_p_value if graded else None),
        pbo_score=(stored.pbo_score if graded else None),
        out_of_sample_sharpe=(stored.out_of_sample_sharpe if graded else None),
        kelly_fraction=s.kelly_fraction,
        # THE STORED VERDICT. Graded once, at backtest time, by the real gate;
        # served here without a recompute (docs/adr/rigor-verdict-of-record.md).
        # passes_rigor_gate is the fail-closed boolean (True only when the
        # four-state is "pass"); rigor_gate_status carries the four-state itself
        # (#1184): "pass" | "fail" | "pending" | "degenerate".
        passes_rigor_gate=passes,
        rigor_gate_status=rigor_status,
        # A3: name the source of the numbers instead of leaving the reader to
        # infer it. "stored_grade" iff a real grade produced them; otherwise
        # every rigor field above is None and this says so.
        metrics_source=("stored_grade" if graded else "unavailable"),
        display_metrics_source=metrics.source,
        # is_backtest_placeholder: True when no BacktestResultRecord row exists.
        is_backtest_placeholder=not has_real,
        sharpe_ci_lower=s.sharpe_ci_lower,
        sharpe_ci_upper=s.sharpe_ci_upper,
        # num_trials provenance (#1358): curated strategies are ALWAYS graded
        # self-contained (num_trials=1, decouple #2 — never deflated by the
        # library's size). Reported only once a grade exists; an ungraded row's
        # honest answer is "no provenance to report", not an assumed 1.
        num_trials_in_selection=(1 if graded else None),
        num_trials_scope=(_SCOPE_CURATED_SELF_CONTAINED if graded else "unspecified"),
        backtest_start=metrics.backtest_start,
        backtest_end=metrics.backtest_end,
        # ── Engine attribution ──────────────────────────────────────────────
        # Read off the BacktestResultRecord row the provider hydrated, so these
        # are real or None — never a guessed engine. Not on the passport row, so
        # not part of the stored-verdict block above.
        backtest_engine=(bt.backtest_engine if bt else None),
        cost_model_id=(bt.cost_model_id if bt else None),
        regime_tag=s.regime_tag,
        return_source=return_source,
        return_source_note=return_source_note,
    )


def _publishable_strategy_ids(
    session,
    strategy_ids: list[str],
    wallet_address: str | None,
    *,
    is_example: bool,
) -> set[str]:
    """Which of ``strategy_ids`` this wallet may publish — O(1) queries, not O(N).

    Both Library listings used to call ``wallet_can_publish`` once per response
    row. Each call is a single ``.first()``
    (``models/strategy_generators.py``), so a 34-row curated page issued 34
    extra sequential round trips, every one of them paying a ``pool_pre_ping``
    ``SELECT 1`` (``db.py``) first. Worse, the per-row call short-circuits on an
    anonymous caller — so a visitor paid nothing and the signed-in owner paid
    all 34, which is backwards for a demo (#1663).

    Same answers, one ``IN`` query:

    * **Anonymous callers still pay nothing.** The empty-``wallet_address``
      short-circuit below reproduces the old ``bool(caller) and ...`` guard
      exactly — no query is issued and every row gets ``can_publish=False``.
      This is not relaxed; a visitor must not be told they can publish.
    * **The ``PLATFORM_ADMIN_WALLETS`` override is delegated, never re-derived.**
      It stays inside ``wallet_can_publish``, which this function calls at most
      once. Re-parsing that env var here would create a third copy of the
      parsing (``models/strategy_generators.py`` and
      ``api/metrics_private_routes.py`` already hold two), and a copy that
      drifts silently changes who is allowed to publish. That is why this is
      1 + at-most-1 queries rather than literally one: the extra lookup buys
      single-sourced publish semantics, and it is a constant, not a per-row
      cost.

    The probe is aimed at an id the wallet demonstrably did NOT generate, so
    ``wallet_can_publish``'s DB half is ``False`` by construction and a ``True``
    answer isolates the admin bit exactly. It is skipped entirely when it could
    not change an answer (non-example rows have no admin override; a wallet that
    generated every id is already fully covered), and an actual admin costs zero
    extra queries because the override returns before the row lookup runs.
    """
    from archimedes.models.strategy_generators import StrategyGenerator, wallet_can_publish

    if not wallet_address or not strategy_ids:
        return set()

    ids = list(dict.fromkeys(strategy_ids))
    # wallet_can_publish lower-cases its argument and record_generator stores
    # the lower-cased form; match that here rather than trusting the caller's
    # casing.
    wallet = wallet_address.lower()

    generated = {
        row[0]
        for row in session.query(StrategyGenerator.strategy_id)
        .filter(
            StrategyGenerator.strategy_id.in_(ids),
            StrategyGenerator.wallet_address == wallet,
        )
        .all()
    }

    if not is_example:
        return generated

    ungenerated = [sid for sid in ids if sid not in generated]
    if not ungenerated:
        return generated

    if wallet_can_publish(session, strategy_id=ungenerated[0], wallet_address=wallet, is_example=True):
        return set(ids)
    return generated


# ── Library listing ─────────────────────────────────────────────


def _list_strategies_sync(request: Request, status: str | None, limit: int, offset: int) -> StrategyListResponse:
    """Page the library and serve each row's stored verdict.

    The blocking half of the route below (#1818 P4): it holds every
    ``session.query`` and every synchronous compute, and it runs on a worker
    thread so a slow or lock-blocked read cannot stop the event loop from
    answering ``/health``.

    **This route no longer grades anything (#1746 / PR-B).** It used to run the
    live gate over the whole library on every request, which is what #1173's
    "never a filtered or paginated subset" rule was defending: scoring over a
    page made a badge depend on which page a strategy landed on (a short window
    falls under ``MIN_LIBRARY_N_FOR_PBO_GATING`` and the CSCV/PBO value itself
    shifts with the cohort), and grading ``list_strategies(status=…)`` graded a
    subset, so ``?status=candidate`` and ``?status=validated`` could answer
    differently for one id. Both were the list-vs-detail contradiction dfa8fc1
    was written to prevent. The rule is now structural rather than defended by a
    comment: the cohort belongs to the WRITER
    (``services.curated_grading.grade_cohort``, always the full library), and
    every read surface serves the one stored answer.
    """
    from archimedes.db import get_session

    status_filter = StrategyStatus(status) if status else None

    # Provider failure must be visible on the wire, not a silent empty list
    # (#1356: `total=len(strats)` used to render as a confident, honest-
    # looking "0 strategies" whether the provider raised or the library was
    # genuinely empty — the caller had no way to tell the two apart).
    try:
        library = strategy_provider().list_strategies()
    except Exception as exc:
        # Full exception detail is logged server-side only — never echoed to
        # the client (DB/chain internals, per docs/api/*.md convention).
        # `degraded_reason` stays a fixed, named category string.
        logger.warning("list_strategies: strategy provider unavailable: %s", exc)
        return StrategyListResponse(
            strategies=[],
            total=0,
            degraded=True,
            degraded_reason="strategy provider unavailable",
        )

    degraded = False
    degraded_reason = ""
    if not library:
        # The dominant real cause is the strategy corpus missing from the
        # build (#1039) — count_strategy_files()'s own docstring already
        # names this for /health; reuse the same signal here instead of
        # rendering "0 strategies" as if the library is legitimately empty.
        # But the corpus CAN be present on disk while discovery still comes
        # back empty (e.g. a shared-helper import error skips every file) —
        # that's a different, real degradation and must say so too, so this
        # route agrees with GET /api/leaderboard over the same corpus.
        from archimedes.services.strategy_provider import count_strategy_files

        if count_strategy_files() == 0:
            degraded = True
            degraded_reason = "strategy corpus not found in build"
        else:
            degraded = True
            degraded_reason = "library is empty"

    # Filter/paginate. Delegated to the provider rather than filtered in-process
    # so the `status` semantics stay byte-identical (file-declared status, before
    # the stored-verdict promotion overlay — see the docstring note above).
    strats = strategy_provider().list_strategies(status=status_filter) if status_filter else library
    total = len(strats)
    window = strats[offset : offset + limit]
    caller = get_linked_wallet_address(request)
    responses: list[StrategyResponse] = []
    with get_session() as session:
        window_ids = [s.id for s in window]
        # One IN query for the whole window's publish rights (#1663) — this was
        # a per-row wallet_can_publish call, i.e. one round trip per response
        # row, paid only by signed-in callers.
        publishable = _publishable_strategy_ids(session, window_ids, caller, is_example=True)
        # …and one for the window's stored verdicts. The cohort-wide live gate
        # run this replaced (`_live_rigor_results_for_strategies`, ~6s per page
        # even warm) is gone with it: the verdict is graded once, when a curated
        # backtest runs (`services.curated_grading`), and read here. Nothing
        # about the answer depends on which page a strategy landed on any more,
        # which is what #1173's full-library-cohort rule was defending — that
        # rule now lives on the write side, where the cohort is always the whole
        # library by construction.
        stored = stored_passports_for(session, window_ids)
        for s in window:
            resp = _to_strategy_response(s, stored.get(s.id))
            resp.can_publish = s.id in publishable
            responses.append(resp)
    return StrategyListResponse(
        strategies=responses,
        total=total,
        degraded=degraded,
        degraded_reason=degraded_reason,
    )


@strategies_router.get("/", response_model=StrategyListResponse)
async def list_strategies(
    request: Request,
    status: str | None = Query(None, pattern="^(candidate|validated|live|retired)$"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """List strategies in the library. Backed by LocalStrategyProvider.

    **This route runs no rigor gate.** The ``rigor_gate_status`` four-state, the
    ``passes_rigor_gate`` badge derived from it, and the four numeric rigor
    fields (``deflated_sharpe_ratio``, ``dsr_p_value``, ``pbo_score``,
    ``out_of_sample_sharpe``) are all READ from each strategy's stored verdict
    of record on its ``strategy_passports`` row. A strategy is graded once, by
    the real gate, when its backtest runs; every surface serves that row
    (``docs/adr/rigor-verdict-of-record.md``). A row no gate has produced reads
    ``rigor_gate_status: "pending"`` with ``passes_rigor_gate: false`` and four
    ``null`` numbers — "not graded", never "graded and failed".

    ``graded_at`` and ``gate_version`` on ``GET /api/strategies/passports/{id}``
    are the proof a real gate produced the verdict, and which gate.

    NOTE on the ``status`` filter: it filters on the FILE-DECLARED status, while
    the served ``status`` is the promotion derived from the stored verdict — a
    ``candidate`` whose stored verdict is ``pass`` is served as ``validated``.
    So such a strategy appears under ``?status=candidate`` with a served
    ``status: "validated"``. That is intentional: the persisted column is the
    stable filter key, and the passport route publishes both, as ``status`` and
    ``served_status``.
    """
    return await asyncio.to_thread(_list_strategies_sync, request, status, limit, offset)


# What a generated row carries when strategy_passports has never heard of it:
# the strategy exists, no gate has graded it. `passes_rigor_gate` is None rather
# than False because False is a VERDICT ("the gate ran and it lost") and no gate
# ran — the same distinction rigor_gate_status="pending" makes in words.
_UNGRADED_VERDICT_FIELDS: dict = {
    "passes_rigor_gate": None,
    "rigor_gate_status": "pending",
    "graded_at": None,
    "deflated_sharpe_ratio": None,
    "dsr_p_value": None,
    "pbo_score": None,
    "out_of_sample_sharpe": None,
}


def _passport_verdicts_for(session, strategy_ids: list[str]) -> dict[str, dict]:
    """Stored rigor verdicts for a page of generated strategies — ONE query.

    Reads ``strategy_passports``: the verdict of record and the four rigor
    numbers the SAME grading event produced (docs/adr/rigor-verdict-of-record.md).
    The numbers travel with the verdict deliberately — a badge from the passport
    beside DSR/PBO from ``StrategyRecord.rigor_verdict`` would put two different
    gates' answers on one row, which is the shape #1187/#1340 removed from the
    curated path.

    ``passes_rigor_gate`` is derived from the stored four-state, not copied from
    the stored boolean, so the read side cannot serve the two apart even if a row
    predating the coupling has them apart.

    Non-fatal: a DB failure returns ``{}`` and every row degrades to ungraded —
    fail-closed, never a fabricated pass.
    """
    if not strategy_ids:
        return {}
    try:
        from archimedes.models.strategy_passport_record import StrategyPassportRecord

        rows = session.query(StrategyPassportRecord).filter(StrategyPassportRecord.id.in_(strategy_ids)).all()
    except Exception as exc:  # pragma: no cover — defensive; DB-level failure
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "passport verdict read failed for the generated page (%s) — every row degrades to ungraded",
            type(exc).__name__,
        )
        _rollback_quietly(session)
        return {}
    out: dict[str, dict] = {}
    for row in rows:
        status = row.rigor_gate_status or "pending"
        out[row.id] = {
            "passes_rigor_gate": status == "pass",
            "rigor_gate_status": status,
            "graded_at": row.graded_at.isoformat() if row.graded_at else None,
            "deflated_sharpe_ratio": row.deflated_sharpe_ratio,
            "dsr_p_value": row.dsr_p_value,
            "pbo_score": row.pbo_score,
            "out_of_sample_sharpe": row.out_of_sample_sharpe,
        }
    return out


def _list_generated_strategies_sync(request: Request, limit: int, user: CurrentUser) -> dict:
    """Read the caller-visible page of ``strategy_store`` rows.

    The blocking half of the route below (#1818 P4): it holds every
    ``session.query`` and every synchronous compute, and it runs on a worker
    thread so a slow or lock-blocked read cannot stop the event loop from
    answering ``/health``.
    """
    from sqlalchemy import and_, or_

    from archimedes.db import get_session
    from archimedes.models.strategy_store import StrategyRecord

    caller = get_linked_wallet_address(request)  # None when anonymous — never an error

    rows: list[dict] = []
    # Honest degradation signal (#1356 review round 2): this route used to
    # swallow a DB/store exception into a 200 with a measured `total: 0`,
    # indistinguishable on the wire from a genuinely-empty store. The Library's
    # default tab then painted "No generated strategies yet" with a Generate
    # CTA — the exact false-empty-state shape #1356 was filed to kill,
    # unfixed on the one route the issue's own Summary names first. Mirrors
    # the `degraded`/`degraded_reason` contract `StrategyListResponse` already
    # carries for GET /api/strategies/ — see that route above.
    degraded = False
    degraded_reason = ""
    try:
        with get_session() as session:  # type: _Session
            query = session.query(StrategyRecord).filter(StrategyRecord.is_example.is_(False))
            owner_filters = [StrategyRecord.owner_user_id == user.id]
            if caller:
                owner_filters.append(
                    and_(
                        StrategyRecord.owner_user_id.is_(None),
                        StrategyRecord.owner_wallet == caller.lower(),
                    )
                )
            query = query.filter(StrategyRecord.is_published.is_(True) | or_(*owner_filters))
            records = query.order_by(StrategyRecord.created_at.desc()).limit(limit).all()
            # One query for the whole page's generation-cost records (#1326) —
            # the library's cost column reads this. Strategies with nothing
            # measured are simply absent from the map and stay absent from the
            # row, which the table renders as an em-dash, never as zero.
            from archimedes.models.generation_cost import generation_costs_for_strategies

            costs = generation_costs_for_strategies(session, [r.id for r in records])
            # One IN query for the whole page's publish rights (#1663), same
            # shape as the generation-cost read directly above.
            publishable = _publishable_strategy_ids(session, [r.id for r in records], caller, is_example=False)
            # ONE IN-query for the whole page's stored rigor verdicts, same shape
            # as the two reads above (#1747). Before this the Library's Generated
            # tab was the only surface that never looked at strategy_passports at
            # all: it served StrategyRecord.to_dict(), whose `status` and
            # `rigor_verdict` are BOTH written from the generation-time fusion
            # verdict and never rewritten after a backtest. So the tab's own
            # honesty guard — demote when status=="live" but the gate failed —
            # was structurally unreachable, because the two halves of that
            # condition came from the same blob. Twenty-one rows read "Live ✓"
            # in the Library while their own passports read "Reference only —
            # gate failed".
            verdicts = _passport_verdicts_for(session, [r.id for r in records])
            page = []
            for r in records:
                d = r.to_dict()
                d["can_publish"] = r.id in publishable
                d["generation_cost"] = costs.get(r.id)
                # The verdict of record, overlaid onto the store row. A strategy
                # with no passport row has never been graded — None / "pending",
                # never a boolean, and never green.
                d.update(verdicts.get(r.id, _UNGRADED_VERDICT_FIELDS))
                page.append(d)
            # Citation truth: ``StrategyRecord.to_dict()`` returns source_papers
            # exactly as stored — arxiv_id, no title — so the Library card had no
            # real paper title to print and printed the generated strategy's own
            # name in the cited-paper column. Resolve real titles here, against
            # the SAME papers table the passport path reads, in ONE query for the
            # whole page (see _corpus_paper_meta) rather than one per row.
            corpus_meta = _corpus_paper_meta(
                [p.get("arxiv_id") for d in page for p in (d.get("source_papers") or []) if isinstance(p, dict)],
                session,
            )
            # Each row's OWN rejection reasons, derived from the rigor_verdict
            # blob `to_dict()` already decoded above — a pure function, zero
            # extra queries, so the page cost is unchanged (the Library's
            # "Rejected — did not pass the rigor gate" cards used to share one
            # paragraph of guessed prose because this field did not exist).
            from archimedes.services.rigor_reasons import rigor_reasons_for_verdict

            rows = []
            for d in page:
                d["source_papers"] = _resolve_source_papers(d.get("source_papers"), corpus_meta)
                d["rigor_reasons"] = rigor_reasons_for_verdict(d.get("rigor_verdict"))
                rows.append(_redact_owner_wallet(d, caller))
    except Exception as exc:
        # Full exception detail is logged server-side only — never echoed to
        # the client (DB/chain internals, per docs/api/*.md convention).
        # `degraded_reason` stays a fixed, named category string.
        import logging as _logging

        _logging.getLogger(__name__).warning("list_generated_strategies failed: %s", exc)
        rows = []
        degraded = True
        degraded_reason = "strategy store unavailable"
    return {"strategies": rows, "total": len(rows), "degraded": degraded, "degraded_reason": degraded_reason}


@strategies_router.get("/generated")
async def list_generated_strategies(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    user: CurrentUser = Depends(require_current_user),
):
    """List fusion/architect-generated strategies from the strategy_store table.

    Private-until-published: row is visible when published or owned by current
    canonical user; verified linked wallet handles legacy ``owner_wallet`` rows.
    Legacy ownerless rows remain invisible until purged (scripts/purge_orphan_generated.py)
    or published. Curated examples live on GET /api/strategies/ and stay public.
    """
    return await asyncio.to_thread(_list_generated_strategies_sync, request, limit, user)


@strategies_router.get("/signals", response_model=StrategySignalsResponse)
async def get_strategy_signals():
    """Evaluate all strategies against live market data and return signals."""
    from datetime import datetime

    from archimedes.services.strategy_signal_evaluator import strategy_evaluator

    strategies = strategy_provider().list_strategies()
    from archimedes.chain.client import chain_client

    synth_assets = [sym for sym, addr in chain_client.settings.synth_addresses.items() if addr]

    all_signals = await asyncio.to_thread(
        strategy_evaluator.evaluate_strategies,
        strategies,
        synth_assets,
    )

    target_weights = strategy_evaluator.aggregate_signals(all_signals, usdc_floor=0.20)

    # flat_pct → ensemble-consensus bucket (#659). This is the agent's
    # directional consensus, NOT a market regime; the model owns the thresholds.
    from archimedes.models.regime import EnsembleConsensus

    flat_count = sum(1 for ss in all_signals for s in ss.signals if s.signal.value == "flat")
    total_count = sum(len(ss.signals) for ss in all_signals)
    consensus = EnsembleConsensus.from_signal_counts(flat_count, total_count)
    flat_pct = consensus.flat_pct
    # `regime` kept for backward-compat; it carries the consensus bucket value.
    regime = consensus.label.value

    strat_responses = []
    for ss in all_signals:
        strat_responses.append(
            StrategySignalResponse(
                strategy_id=ss.strategy_id,
                paper_title=ss.paper_title,
                signals=[
                    SignalResponse(
                        asset=s.asset,
                        signal=s.signal.value,
                        weight=s.weight,
                        reason=s.reason,
                        strategy_name=s.strategy_name,
                    )
                    for s in ss.signals
                ],
            )
        )

    return StrategySignalsResponse(
        strategy_count=len(all_signals),
        regime=regime,
        ensemble_consensus=consensus.label.value,
        confidence=round(1.0 - flat_pct, 2),
        target_weights=target_weights,
        strategies=strat_responses,
        timestamp=datetime.now(UTC).isoformat(),
    )


# /frontier and /correlation endpoints deleted (Issue #383).
# They fabricated returns via np.random.default_rng(42) — synthetic data
# masquerading as measured correlations. Honest alternatives require real
# backtest return series, which is a post-submission feature.


# ── Stress scenarios ───────────────────────────────────────────


@strategies_router.get("/stress/scenarios")
async def list_stress_scenarios():
    """List the available stress scenarios with descriptions."""
    from archimedes.services.stress_engine import list_scenarios

    return {"scenarios": list_scenarios()}


@strategies_router.post("/stress/run")
@limiter.limit("20/minute")
async def run_stress_test(payload: dict, request: Request, response: Response):  # noqa: ARG001 — slowapi @limiter.limit inspects param name
    """Apply a stress scenario to a caller-supplied portfolio."""
    from fastapi import HTTPException

    from archimedes.services.stress_engine import SCENARIOS, stress_all, stress_one

    allocations = payload.get("allocations") or []
    if not isinstance(allocations, list) or not allocations:
        raise HTTPException(status_code=400, detail="allocations[] is required")

    # Validate each allocation's shape before handing it to the stress engine.
    # stress_one indexes ``a["symbol"]`` and does ``float(a.get("weight") or 0.0)``,
    # so a missing symbol (KeyError) or a non-numeric weight (ValueError) would
    # otherwise surface as an opaque 500. Reject those as a 422 client error
    # instead (issue #926). ``usdc_weight`` per-element is not consumed by the
    # engine (it is the top-level field below), so it is not required here.
    for i, a in enumerate(allocations):
        if not isinstance(a, dict):
            raise HTTPException(status_code=422, detail=f"allocations[{i}] must be an object")
        sym = a.get("symbol")
        if not isinstance(sym, str) or not sym.strip():
            raise HTTPException(
                status_code=422,
                detail=f"allocations[{i}].symbol is required and must be a non-empty string",
            )
        w = a.get("weight")
        if w is not None:
            try:
                float(w)
            except (TypeError, ValueError):
                raise HTTPException(status_code=422, detail=f"allocations[{i}].weight must be a number") from None

    scenario = payload.get("scenario", "all")
    try:
        usdc_weight = float(payload.get("usdc_weight") or 0.0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="usdc_weight must be a number") from None

    if scenario == "all":
        results = stress_all(allocations, usdc_weight=usdc_weight)
    else:
        if scenario not in SCENARIOS:
            raise HTTPException(status_code=404, detail=f"Unknown scenario: {scenario}")
        results = [stress_one(allocations, scenario, usdc_weight=usdc_weight)]

    return {
        "results": [
            {
                "scenario": r.scenario,
                "label": r.label,
                "description": r.description,
                "portfolio_pnl": r.portfolio_pnl,
                "portfolio_value_after": r.portfolio_value_after,
                "per_asset_pnl": r.per_asset_pnl,
            }
            for r in results
        ],
    }


# ── Unified Passport Store (Issue #160 Phase 2) ───────────────────────────


def _passport_payload(record, caller: str | None) -> dict:
    """The wire shape of one passport row: the stored dict + ``served_status``.

    ``to_dict()`` publishes the PERSISTED ``status`` column, which is what the
    ``?status=`` filter queries — overwriting it here would make the list and its
    own filter disagree. ``served_status`` is the same value a CARD shows for
    this row, produced by the one shared derivation :func:`served_status`, so an
    agent comparing this payload with ``GET /api/strategies/{id}`` gets the two
    under names that say which is which instead of one string with two answers
    (#1746).
    """
    payload = _redact_owner_wallet(record.to_dict(), caller)
    payload["served_status"] = served_status(
        record.status,
        record.rigor_gate_status,
        promote=(record.generation_method or "").lower() == "curated",
    )
    return payload


def _redact_owner_wallet(d: dict, caller: str | None) -> dict:
    """Strip ``owner_wallet`` from a public payload unless the caller IS the owner.

    Wallet addresses are pseudonymous PII and a linkability vector; publishing a
    strategy publishes the strategy, not its creator's wallet. Attribution is a
    marketplace (#713) decision to make deliberately later.
    """
    ow = d.get("owner_wallet")
    if not (caller and ow and str(ow).lower() == caller.lower()):
        d.pop("owner_wallet", None)
    return d


def _visible_passports(session, records: list, caller: str | None = None, caller_user_id: str | None = None) -> list:
    """Apply private-until-published to raw passport records.

    The passports table mirrors ``strategy_store`` ids, so leaving these
    endpoints ungated would defeat the 404-hides-existence design on
    ``GET /api/strategies/{id}`` by simple id substitution. Curated passports
    are always public (the curated corpus has no store row and no owner). For
    generated passports, the per-row DECISION delegates to the single shared
    predicate (``services.strategy_visibility.is_strategy_visible``, #1120) —
    this function only supplies its inputs: the ``is_example``/``is_published``
    flags come from the strategy_store row (the passport record does not carry
    them), ownership fields from the passport record. Ownerless generated
    legacy rows stay hidden (purge-pending — scripts/purge_orphan_generated.py).
    """
    from archimedes.models.strategy_store import StrategyRecord
    from archimedes.services.strategy_visibility import is_strategy_visible

    ids = [r.id for r in records]
    store_flags: dict[str, tuple[bool, bool]] = {}
    if ids:
        rows = (
            session.query(StrategyRecord.id, StrategyRecord.is_example, StrategyRecord.is_published)
            .filter(StrategyRecord.id.in_(ids))
            .all()
        )
        store_flags = {sid: (bool(ex), bool(pub)) for sid, ex, pub in rows}

    visible = []
    for r in records:
        if (r.generation_method or "").lower() == "curated":
            visible.append(r)
            continue
        is_example, is_published = store_flags.get(r.id, (False, False))
        row_view = {
            "is_example": is_example,
            "is_published": is_published,
            "owner_user_id": r.owner_user_id,
            "owner_wallet": r.owner_wallet,
        }
        if is_strategy_visible(row_view, caller, caller_user_id=caller_user_id):
            visible.append(r)
    return visible


def _list_strategy_passports_sync(request: Request, status: str | None, regime_tag: str | None, limit: int) -> dict:
    """Read the caller-visible passports.

    The blocking half of the route below (#1818 P4): it holds every
    ``session.query`` and every synchronous compute, and it runs on a worker
    thread so a slow or lock-blocked read cannot stop the event loop from
    answering ``/health``.
    """
    from archimedes.db import get_session
    from archimedes.services.passport_loader import list_passports

    caller = get_linked_wallet_address(request)  # optional linked-wallet compatibility
    user = get_current_user(request)
    with get_session() as session:
        records = list_passports(session, status=status, regime_tag=regime_tag)
        records = _visible_passports(session, records, caller, user.id if user else None)
        passports = [_passport_payload(r, caller) for r in records[:limit]]

    return {"passports": passports, "total": len(passports), "source": "strategy_passports"}


@strategies_router.get("/passports")
async def list_strategy_passports(
    request: Request,
    status: str | None = Query(None),
    regime_tag: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
):
    """List strategies from the unified strategy_passports table.

    A pure read of the stored rows — no gate runs here. Each row carries the
    verdict of record and the provenance that proves it
    (``docs/adr/rigor-verdict-of-record.md``), plus ``served_status`` beside the
    persisted ``status`` this endpoint's own ``?status=`` filter queries.

    Private-until-published applies here exactly as on ``/generated`` — see
    ``_visible_passports``.
    """
    return await asyncio.to_thread(_list_strategy_passports_sync, request, status, regime_tag, limit)


def _get_strategy_passport_sync(request: Request, strategy_id: str) -> dict:
    """Read one passport, gated on visibility.

    The blocking half of the route below (#1818 P4): it holds every
    ``session.query`` and every synchronous compute, and it runs on a worker
    thread so a slow or lock-blocked read cannot stop the event loop from
    answering ``/health``.
    """
    from fastapi import HTTPException

    from archimedes.db import get_session
    from archimedes.services.passport_loader import get_passport

    caller = get_linked_wallet_address(request)
    user = get_current_user(request)
    with get_session() as session:
        record = get_passport(session, strategy_id)
        if record is None or not _visible_passports(session, [record], caller, user.id if user else None):
            raise HTTPException(status_code=404, detail="Passport not found")
        return _passport_payload(record, caller)


@strategies_router.get("/passports/{strategy_id}")
async def get_strategy_passport(request: Request, strategy_id: str):
    """Get a single passport in its native dict shape from strategy_passports.

    A pure read of the stored row — the verdict of record
    (``docs/adr/rigor-verdict-of-record.md``). No gate runs here, and none runs
    on ``GET /api/strategies/{id}`` either, so the two agree by construction on
    ``rigor_gate_status``, ``passes_rigor_gate`` and the headline metrics. The
    payload publishes both status names: ``status`` is the persisted lifecycle
    column the ``?status=`` filter queries, and ``served_status`` is the card
    status that same stored verdict derives — the one the detail route serves.

    ``graded_at`` / ``gate_version`` / ``cohort_n`` say whether a gate produced
    this verdict, which gate, and against how many return series. ``graded_at:
    null`` means no gate has ever graded this strategy.

    Unpublished non-example passports 404 for non-owners (never 403 — a 403
    would confirm the id exists).
    """
    return await asyncio.to_thread(_get_strategy_passport_sync, request, strategy_id)


def _year_from_published(published: str | None) -> int | None:
    """Publication year from a corpus ``published`` stamp, or None.

    ``PaperRecord.published`` is a free-form arXiv date string ("2019",
    "2019-03-11", "2019-03-11T00:00:00Z"). Only a leading 4-digit year is
    trusted; anything else yields None, because a guessed year on a citation
    is the same class of lie as a guessed title.
    """
    head = (published or "").strip()[:4]
    if len(head) == 4 and head.isdigit():
        return int(head)
    return None


def _corpus_paper_meta(arxiv_ids: list[str | None], session) -> dict[str, dict]:
    """Batch-resolve arXiv ids → ``{"title": str, "year": int | None}`` from the corpus.

    ONE query for every id handed in — callers pass a whole page's ids at once,
    never one call per row. Non-fatal by design: any DB error returns an empty
    map so the caller degrades to its own honest "unresolved" rendering rather
    than failing a list read over a decoration.

    This is the single papers-table lookup shared by the passport path
    (:func:`_enrich_paper_titles_from_corpus`) and the generated-strategy list
    route, so both resolve a citation the same way.
    """
    ids = [i for i in dict.fromkeys(arxiv_ids) if i]
    if not ids:
        return {}
    try:
        from archimedes.models.corpus_store import PaperRecord

        rows = session.query(PaperRecord).filter(PaperRecord.arxiv_id.in_(ids)).all()
    except Exception:
        return {}
    return {row.arxiv_id: {"title": row.title, "year": _year_from_published(row.published)} for row in rows}


def _enrich_paper_titles_from_corpus(
    refs: list,
    session,
) -> dict[str, str]:
    """Return a map of arxiv_id → corpus title for refs with empty stored titles.

    Queries the ``papers`` corpus table (PaperRecord) for refs whose stored
    ``title`` is blank but whose ``arxiv_id`` is known.  Non-fatal — any DB
    error returns an empty map so the caller falls back to the bare arxiv_id.
    Only fires when at least one ref needs enrichment.
    """
    missing_ids = list(dict.fromkeys(r.arxiv_id for r in refs if r.arxiv_id and not (r.title or "").strip()))
    if not missing_ids:
        return {}
    meta = _corpus_paper_meta(missing_ids, session)
    return {arxiv_id: m["title"] for arxiv_id, m in meta.items() if m["title"]}


def _resolve_source_papers(source_papers, corpus_meta: dict[str, dict]) -> list[dict]:
    """Stamp each generated-row citation with a RESOLVED paper title and year.

    ``StrategyRecord.source_papers`` entries carry an ``arxiv_id`` but usually
    no ``title``, so the Library card had nothing real to print and printed the
    generated STRATEGY NAME in the cited-paper column instead — a fabricated
    citation. Resolution order matches the passport's ``_resolved_title``:
    stored title wins, the corpus fills a blank one, and when neither has one
    ``resolved_title`` stays **None**. None is the honest answer; the frontend
    renders it as "title unavailable — arXiv:<id>", never as the strategy name.

    ``resolved_year`` is the CITED PAPER's publication year, which is not the
    row's ``created_at`` — that is when the strategy was generated.
    """
    resolved: list[dict] = []
    for paper in source_papers or []:
        if not isinstance(paper, dict):
            continue
        entry = dict(paper)
        arxiv_id = (entry.get("arxiv_id") or "").strip()
        meta = corpus_meta.get(arxiv_id) or {}
        stored_title = (entry.get("title") or "").strip()
        corpus_title = (meta.get("title") or "").strip()
        entry["resolved_title"] = stored_title or corpus_title or None
        stored_year = entry.get("year")
        entry["resolved_year"] = stored_year if isinstance(stored_year, int) else meta.get("year")
        resolved.append(entry)
    return resolved


def _generation_cost_for(strategy_id: str, session) -> dict | None:
    """The durable generation-cost record for one strategy, or ``None`` (#1326).

    ``None`` covers three genuinely different situations — no session on this
    call path, no record for this strategy, and a record whose measurement will
    not decode — and every one of them is the same claim to a reader: *nothing
    measured this*. That is deliberate. The alternative, distinguishing them in
    the payload, would invite a caller to treat "we didn't look" as "measured
    zero". The corrupt-record case is logged loudly by the model layer.

    A lookup failure must never take down a strategy read: the cost card is
    decoration on a page whose subject is the strategy.
    """
    if session is None or not strategy_id:
        return None
    try:
        from archimedes.models.generation_cost import generation_cost_for_strategy

        return generation_cost_for_strategy(session, strategy_id)
    except Exception as exc:  # pragma: no cover — defensive; DB-level failure
        import logging as _logging

        _logging.getLogger(__name__).warning("generation cost lookup failed for %s: %s", strategy_id, exc)
        # Postgres aborts the whole transaction on a failed statement — see
        # _rollback_quietly. This is now the FIRST swallowed DB read on the
        # passport path (the cohort returns read that used to hold that spot is
        # gone), so without this every later read in the request fails on
        # Postgres and succeeds on sqlite.
        _rollback_quietly(session)
        return None


def _num_trials_for_passport(strategy_id: str, session) -> tuple[int | None, str]:
    """``(num_trials_in_selection, num_trials_scope)`` for a generated/fusion
    passport row (#1358).

    No session, or no persisted ``BacktestResultRecord`` yet (the strategy has
    not been graded), both mean the same thing to a reader: *no provenance to
    report*. Returning ``(None, "unspecified")`` for those cases — rather than a
    silently-assumed ``1`` — is the fix this issue asks for: the passport must
    not claim a self-contained N=1 grading for a strategy the gate never ran.
    When a backtest row exists, delegates to the SAME discriminator
    ``selection_bias_routes.py``'s own per-strategy live gate uses
    (``_num_trials_for_generated_row``, keyed on ``backtest_engine`` provenance,
    not on whether the stored count happens to be populated — see that
    function's docstring) so this can never disagree with what
    ``GET /api/selection-bias/gate/{id}`` reports for the same strategy.
    """
    if session is None or not strategy_id:
        return None, "unspecified"
    try:
        from archimedes.services.backtest_repository import latest_backtests_by_strategy

        latest = latest_backtests_by_strategy(session, [strategy_id]).get(strategy_id)
        if latest is None:
            return None, "unspecified"
        return _num_trials_for_generated_row(latest.backtest_engine, latest.num_trials_in_selection)
    except Exception as exc:  # pragma: no cover — defensive; DB-level failure
        import logging as _logging

        _logging.getLogger(__name__).warning("num_trials provenance lookup failed for %s: %s", strategy_id, exc)
        _rollback_quietly(session)  # same transaction-abort hazard as above
        return None, "unspecified"


def _strategy_spec_for_passport(strategy_id: str, session) -> dict | None:
    """The validated DSL spec stored for a generated row, or ``None`` (#1769).

    The spec column lives on ``StrategyRecord`` (``strategy_store``), not on the
    ``strategy_passports`` row this module reshapes — so reading it costs one
    primary-key lookup, the same shape and the same per-row cost as
    ``_generation_cost_for`` and ``_num_trials_for_passport`` immediately above.
    It does not change the complexity class of the list path.

    **The spec itself does not go on the wire.** It is REASONING under #1557 and
    stays owner-gated at the detail route; what comes back out of
    ``reconcile_card_fields`` is three fields the passport row already serves
    publicly to every caller — a rebalance cadence, a sizing rule and a ticker
    list.

    Their *values* do change, and that is a real disclosure delta the owner
    signed off on rather than an argument this docstring can win. Before #1769
    every generated row served the same ``weekly`` / ``equal_weight`` column
    defaults, which carried no information about the strategy at all. It now
    serves the true cadence, the true sizing rule and the spec's universe —
    three of the seven fields of an artifact #1557 gates. The judgement is that
    a card is *for* saying what the strategy does, and that a card which lies
    about it is worth less than the secrecy it buys; the entry rule, the exit
    rule, the indicator parameters and the condition tree — the parts that make
    the spec reproducible — remain gated.

    Fails soft: a lookup failure means the card keeps its stored values, which is
    exactly today's behaviour, and never takes down a strategy read.
    """
    if session is None or not strategy_id:
        return None
    try:
        from archimedes.models.strategy_store import StrategyRecord

        row = session.query(StrategyRecord).filter_by(id=strategy_id).first()
        return row.decoded_strategy_spec() if row is not None else None
    except Exception as exc:  # pragma: no cover — defensive; DB-level failure
        import logging as _logging

        _logging.getLogger(__name__).warning("strategy_spec lookup failed for %s: %s", strategy_id, exc)
        _rollback_quietly(session)
        return None


def _rollback_quietly(session) -> None:
    """Roll back after a swallowed read so the transaction is usable again.

    Postgres aborts the whole transaction on a failed statement; every later
    statement then raises InFailedSqlTransaction. sqlite does not, so a suite
    that runs on sqlite cannot observe the difference — which is why this is
    written down rather than left to the reader.
    """
    try:
        session.rollback()
    except Exception:  # pragma: no cover — nothing useful to do if rollback fails
        pass


def _passport_rigor_status(record, daily_returns: list[float]) -> tuple[str, bool]:
    """The OLD read-time derivation of a passport row's four-state badge.

    **No longer on any serving path.** The rigor verdict is now graded once, at
    backtest time, and stored on ``strategy_passports.rigor_gate_status``; every
    surface reads that column (``docs/adr/rigor-verdict-of-record.md``). This
    function is kept for exactly two jobs, both of them off the request path:

    1. It is the ORACLE the verdict-of-record migration's backfill rule was
       written from — "derive exactly as today's read path did" — so
       ``test_rigor_verdict_of_record`` can assert the migration and this
       function agree on the same inputs, instead of restating the rule in prose
       and hoping.
    2. It documents what the four states meant before they were stored, which is
       what a reader of a ``legacy-derived`` ``gate_version`` needs to know.

    Returns ``(status, is_placeholder)``.

    #1184: the stored aggregate alone cannot tell a zero-variance persisted
    series apart from an ungraded one. Both leave ``record.sharpe_ratio`` NULL,
    so reading the aggregate by itself reported a flat, broken, or zero-trade
    backtest as ``"pending"`` — "we have not graded this yet", which is a claim,
    and a false one. That distinction is now made by the WRITER
    (``verdict_from_returns`` stores ``degenerate`` as itself), which is why the
    read no longer has to re-derive it from the series.

    ``daily_returns`` empty means no persisted series was found, which is the
    genuine "not graded yet" case — the aggregate three-way is then correct.
    """
    if daily_returns:
        from archimedes.services.rigor_evaluator import (
            is_oos_zero_variance_series,
            is_zero_variance_series,
        )

        # OR'd exactly as run_rigor_gate ORs them into is_degenerate, so this
        # read path and the gate agree by construction on both kinds of
        # flatness (whole-series, and flat only inside the OOS slice).
        if is_zero_variance_series(daily_returns) or is_oos_zero_variance_series(daily_returns):
            # A persisted series exists, so this is not an ungraded placeholder;
            # it is a graded row whose series carries no variance to grade.
            return "degenerate", False

    if record.sharpe_ratio is None:
        return "pending", True
    return ("pass" if bool(record.passes_rigor_gate) else "fail"), False


def _passport_to_strategy_response(record, session=None) -> StrategyResponse:
    """Reshape a StrategyPassportRecord (fusion/architect output) into the
    StrategyResponse schema that StrategyPassport.jsx expects. Curated
    strategies still flow through LocalStrategyProvider above; this is the
    fallback that makes generated strategies clickable from Library.

    ``session`` — optional SQLAlchemy session used to enrich empty paper titles
    from the corpus ``papers`` table at read time.  When titles are missing
    (fusion generation stores only arxiv_ids), the corpus join backfills them so
    the UI can display a human-readable title instead of a bare arxiv id.
    Falls back to the arxiv_id string when the corpus has no matching row.

    **The rigor verdict is READ, not derived.** ``rigor_gate_status`` and
    ``passes_rigor_gate`` come straight off the row, where the post-backtest
    grade wrote them (``docs/adr/rigor-verdict-of-record.md``). This function
    used to load the strategy's persisted return series and re-derive the
    four-state badge on every request — which is why it took a ``daily_returns``
    parameter, and why ``_passport_responses`` paid a whole-cohort
    ``get_all_daily_returns`` per page. Both are gone: a verdict recomputed on
    read is a second gate run whose answer can differ from the stored one, which
    is the disagreement #1746/#1747 are made of. The degenerate state has not
    been lost — the WRITER stores it (see ``_refresh_passport_real_metrics``).
    """
    from archimedes.api.schemas import PaperRefResponse
    from archimedes.services.return_source_classifier import (
        StrategyView,
        classify_return_source,
    )

    # What the generation run that produced this strategy consumed (#1326).
    # Needs the session, so it is None on the session-less call path — and None
    # is also the answer for every strategy generated before the meter existed.
    # Either way the UI renders "not measured"; nothing is zeroed or invented.
    generation_cost = _generation_cost_for(record.id, session)
    num_trials_in_selection, num_trials_scope = _num_trials_for_passport(record.id, session)

    # The verdict of record, read verbatim. NOT NULL with a "pending" server
    # default, so the ``or`` is belt-and-braces for a row an in-memory test
    # built without going through the loader.
    _rigor_status = record.rigor_gate_status or "pending"
    # "Placeholder" means: nothing has been graded here yet. That is exactly the
    # pending state now, and only the pending state — a `degenerate` row HAS a
    # backtest (its returns are just flat), and a `fail` row certainly does.
    _is_placeholder = _rigor_status == "pending"

    refs = list(record.paper_refs or [])
    first = refs[0] if refs else None

    # Enrich missing titles from the corpus when a session is available.
    corpus_titles: dict[str, str] = _enrich_paper_titles_from_corpus(refs, session) if session is not None else {}

    def _resolved_title(r) -> str | None:
        """Stored title wins; fall back to corpus; otherwise **None** (#1637).

        The last fallback used to be the bare ``arxiv_id``, which put an id in
        a column labelled "title" — the same class of defect as printing the
        strategy name there, just less obviously wrong. ``None`` is the honest
        answer and matches ``_resolve_source_papers``'s ``resolved_title``
        rule; the id is already carried in ``arxiv_id`` for the renderer to
        compose "title unavailable — arXiv:<id>" from.
        """
        if (r.title or "").strip():
            return r.title
        if r.arxiv_id and corpus_titles.get(r.arxiv_id):
            return corpus_titles[r.arxiv_id]
        return None

    papers_list = [
        PaperRefResponse(
            arxiv_id=r.arxiv_id,
            title=_resolved_title(r),
            authors=json.loads(r.authors) if r.authors else [],
            doi=r.doi,
            venue=r.venue,
            year=r.year,
            citation_count=r.citation_count,
            contribution=r.contribution,
            role=r.role or "cited",
            selection_rank=r.selection_rank,
            semantic_score=r.semantic_score,
            content_hash=r.content_hash,
        )
        for r in refs
    ]

    # The three executable card fields, reconciled against the validated DSL
    # spec (#1769). The generation path now derives them at WRITE time, but the
    # rows written before that fix are still in the table and the table is
    # append-only — a read that trusted them would keep serving the card that
    # contradicts its own backtest. The spec wins and the disagreement is logged
    # naming this id, ONCE per id per process — this function is the per-row
    # mapper for Library and the public leaderboard and it repairs the response,
    # not the row, so a per-call line would repeat on every request forever. The
    # dedupe lives in services/passport_spec_parity.py (`_LOGGED_DISAGREEMENTS`).
    _card = reconcile_card_fields(
        record.id,
        _strategy_spec_for_passport(record.id, session),
        asset_universe=json.loads(record.asset_universe) if record.asset_universe else [],
        rebalance_frequency=record.rebalance_frequency or "weekly",
        position_sizing=record.position_sizing or "equal_weight",
    )
    asset_universe = _card["asset_universe"]

    # The enriched first-paper title (may have been filled from corpus above).
    first_title = papers_list[0].title if papers_list else (first.title if first else None)

    return_source_enum, return_source_note = classify_return_source(
        StrategyView(
            paper_title=first_title or "",
            methodology_summary=record.methodology_summary or "",
            asset_universe=tuple(asset_universe),
            deflated_sharpe_ratio=record.deflated_sharpe_ratio,
            dsr_p_value=record.dsr_p_value,
            # The SAME derivation the badge uses twenty lines below, not the raw
            # column. The comment there says deriving from the status "removes
            # the last place the two could be served apart" — this was that
            # place: one function reading the stored boolean here and the stored
            # four-state there is precisely the two-sources-for-one-fact shape
            # this ADR exists to remove. They agree today because the migration
            # and the loader couple them; reading one field means they cannot
            # stop agreeing.
            passes_rigor_gate=_rigor_status == "pass",
        )
    )

    return StrategyResponse(
        id=record.id,
        papers=papers_list,
        paper_arxiv_id=first.arxiv_id if first else None,
        paper_title=first_title or None,
        paper_authors=json.loads(first.authors) if first and first.authors else [],
        paper_venue=first.venue if first else None,
        paper_year=first.year if first else None,
        paper_doi=first.doi if first else None,
        paper_citation_count=first.citation_count if first else None,
        methodology_summary=record.methodology_summary or "",
        asset_universe=asset_universe,
        universe_source=record.universe_source,
        position_sizing=_card["position_sizing"],
        rebalance_frequency=_card["rebalance_frequency"],
        status=record.status or "candidate",
        methodology_hash=record.methodology_hash,
        extraction_llm=record.extraction_llm,
        curator_wallet=record.curator_wallet,
        curator_note=record.curator_note,
        on_chain_registration_tx=record.on_chain_registration_tx,
        paper_claimed_sharpe=record.paper_claimed_sharpe,
        paper_claim_blended_sharpe=record.paper_claim_blended_sharpe,
        sharpe_ratio=record.sharpe_ratio,
        sortino_ratio=record.sortino_ratio,
        cagr=record.cagr,
        max_drawdown=record.max_drawdown,
        win_rate=record.win_rate,
        calmar_ratio=record.calmar_ratio,
        correlation_to_spy=record.correlation_to_spy,
        total_trades=record.total_trades,
        deflated_sharpe_ratio=record.deflated_sharpe_ratio,
        dsr_p_value=record.dsr_p_value,
        pbo_score=record.pbo_score,
        out_of_sample_sharpe=record.out_of_sample_sharpe,
        kelly_fraction=None,
        # Generated/fusion strategies carry a PERSISTED live-gate verdict written by
        # the generation pipeline (strategy_passports.passes_rigor_gate) — a stored
        # *live* verdict, not a fixture boolean — so it is a legitimate badge source
        # per #821 ("read a persisted live-gate verdict").
        # Coupled to the four-state below by construction, on the READ side too:
        # `passes` is `status == "pass"` and nothing else. The row's own
        # `passes_rigor_gate` column says the same thing (passport_loader writes
        # the two together), so deriving it from the status here costs nothing
        # and removes the last place the two could be served apart.
        passes_rigor_gate=_rigor_status == "pass",
        # THE STORED VERDICT. Graded once, at backtest time, by the real gate;
        # served here without a recompute (docs/adr/rigor-verdict-of-record.md).
        rigor_gate_status=_rigor_status,
        # Read from the row, like every other number here. NULL on a generated
        # row by construction — the `real_* → backtest → stub` display chain is
        # a curated-library construct and a generated strategy's numbers come
        # from its own pipeline backtest — so this is "unavailable" in practice,
        # which is what the field already served for this branch.
        display_metrics_source=record.display_metrics_source or "unavailable",
        is_backtest_placeholder=_is_placeholder,
        sharpe_ci_lower=None,
        sharpe_ci_upper=None,
        num_trials_in_selection=num_trials_in_selection,
        num_trials_scope=num_trials_scope,
        backtest_start=record.backtest_start,
        backtest_end=record.backtest_end,
        regime_tag=record.regime_tag,
        return_source=return_source_enum.value,
        return_source_note=return_source_note,
        generation_cost=generation_cost,
    )


# ── Curated ∪ generated resolvers for read-surfaces beyond Library ────────
# (leaderboard, risk, chat — the "unify source" decouples in
# docs/CURATED-STRATEGY-DECOUPLE-AND-CONSOLIDATE-2026-07-08.md Part A).
# Curated strategies are UNCHANGED: callers keep sourcing those from
# strategy_provider() and concatenate the GENERATED half these return on top —
# nothing here alters the curated path.


def _passport_responses(records, session) -> list[StrategyResponse]:
    """Map passport rows to responses.

    **The whole-cohort returns read is gone.** This used to call
    ``get_all_daily_returns`` for every page so ``_passport_to_strategy_response``
    could re-derive each row's four-state badge from its persisted series — one
    windowed query whose BYTES still scaled with the generated corpus, because it
    projected and deserialized every winning row's ``artifact_json`` to find a
    ``daily_returns`` list, on a route (``list_passports``) that has no LIMIT.

    The verdict is now graded once and stored (see
    ``docs/adr/rigor-verdict-of-record.md``), so the read needs no return series
    at all. The degenerate state is not lost: the writer stores it. What is lost
    is a per-request recompute that could disagree with the stored answer — which
    was the point, and the cost saving is a consequence, not the motive.
    """
    return [_passport_to_strategy_response(r, session) for r in records]


def _generated_strategy_responses(
    session, caller: str | None = None, caller_user_id: str | None = None
) -> list[StrategyResponse]:
    """GENERATED (non-curated) strategies visible to *caller*, as StrategyResponse.

    Same #850 ownership-visibility rule as ``list_generated_strategies`` /
    ``_visible_passports``: a row is visible when ``is_published`` or the
    caller's verified wallet matches ``owner_wallet`` (``is_example`` is never
    True for a non-curated row). Used by surfaces that need real per-caller
    generated strategies (risk endpoints, chat vault-context) — NOT for
    unauthenticated public surfaces, which want ``_public_generated_strategy_responses``
    instead so a private candidate never leaks.
    """
    from archimedes.services.passport_loader import list_passports

    records = [r for r in list_passports(session) if (r.generation_method or "").lower() != "curated"]
    if not records:
        return []
    visible = _visible_passports(session, records, caller, caller_user_id)
    return _passport_responses(visible, session)


def _public_generated_strategy_responses(session) -> list[StrategyResponse]:
    """GENERATED strategies visible on PUBLIC, unauthenticated surfaces (the
    leaderboard). No wallet context exists here, so visibility requires the
    OWNER to have opted in by PUBLISHING — ``is_published`` ONLY.

    ``status`` is deliberately NOT a visibility criterion: ``upsert_strategy``
    sets ``status="live"`` on ANY strategy whose rigor passes, published or not,
    so keying off it would leak a user's PRIVATE (unpublished) strategy — its
    name + metrics — onto a public ranking the moment it passed rigor. Publish
    is the consent signal, not rigor. (#850 privacy principle.)

    NOTE: ``is_published`` is currently a dormant flag — the publish flow does
    not yet flip it — so this is intentionally inert in prod until that wiring
    lands (tracked as a follow-up). Inert-but-safe beats leaky.
    """
    from archimedes.models.strategy_store import StrategyRecord
    from archimedes.services.passport_loader import list_passports

    records = [r for r in list_passports(session) if (r.generation_method or "").lower() != "curated"]
    if not records:
        return []
    ids = [r.id for r in records]
    published_ids = {
        sid
        for (sid,) in (
            session.query(StrategyRecord.id)
            .filter(StrategyRecord.id.in_(ids), StrategyRecord.is_published.is_(True))
            .all()
        )
    }
    visible = [r for r in records if r.id in published_ids]
    return _passport_responses(visible, session)


def _owned_generated_strategy_responses(
    session, caller_wallet: str | None, caller_user_id: str | None
) -> list[StrategyResponse]:
    """GENERATED strategies OWNED by *caller* — the single-user leaderboard's
    "own" scope (leaderboard-goes-single-user MVP: with no publish mechanism
    live, ranking against a global cohort was incoherent, so a signed-in
    caller instead ranks THEIR OWN strategies against each other).

    Deliberately narrower than ``_generated_strategy_responses`` (published-
    by-anyone ∪ owned-by-caller): here the question is "is this MINE", not
    "am I allowed to see this", so another user's published strategy must
    NOT appear just because it is public. Asks ``owns_strategy`` — the single
    two-tier owner_user_id/owner_wallet match (#1557 named what this used to
    express by pinning ``is_published`` False in a hand-built row_view fed to
    ``is_strategy_visible``; same rule, one implementation, never
    re-implemented at a call site). Curated (``is_example``) rows have no owner
    and are never returned here — they are the separate "curated" scope.
    """
    from archimedes.services.passport_loader import list_passports
    from archimedes.services.strategy_visibility import owns_strategy

    if not caller_user_id and not caller_wallet:
        return []

    records = [r for r in list_passports(session) if (r.generation_method or "").lower() != "curated"]
    if not records:
        return []

    owned = [r for r in records if owns_strategy(r, caller_wallet, caller_user_id=caller_user_id)]
    return _passport_responses(owned, session)


def _get_strategy_returns_sync(strategy_id: str, request: Request) -> StrategyReturnsResponse:
    """Gate on ownership, then load the persisted series.

    The blocking half of the route below (#1818 P4): it holds every
    ``session.query`` and every synchronous compute, and it runs on a worker
    thread so a slow or lock-blocked read cannot stop the event loop from
    answering ``/health``.
    """
    from fastapi import HTTPException

    # ── 1. Existence + OWNERSHIP gate ──────────────────────────────────────
    # Curated strategies (in LocalStrategyProvider) are always public.
    strat = strategy_provider().get_strategy(strategy_id)
    is_curated = strat is not None

    if not is_curated:
        from archimedes.api.auth_siwe import get_verified_wallet
        from archimedes.db import get_session
        from archimedes.models.strategy_store import StrategyRecord
        from archimedes.services.strategy_visibility import is_strategy_reasoning_visible

        with get_session() as session:
            row = session.query(StrategyRecord).filter_by(id=strategy_id).first()
            # The legacy-owner fallback compares a wallet the caller has
            # PROVEN control of this session, so this site uses the SIWE
            # get_verified_wallet, not get_linked_wallet_address.
            caller = get_verified_wallet(request)
            user = get_current_user(request)
            if not is_strategy_reasoning_visible(row, caller, caller_user_id=user.id if user else None):
                raise HTTPException(status_code=404, detail="Strategy not found")

    # ── 2. Load persisted daily returns from backtest_results ────────────────
    try:
        from archimedes.db import get_session
        from archimedes.services.backtest_repository import get_daily_returns, latest_backtests_by_strategy

        with get_session() as session:
            daily_returns = get_daily_returns(session, strategy_id)
            rows = latest_backtests_by_strategy(session, [strategy_id])
            latest_row = rows.get(strategy_id)
    except Exception as exc:
        logger.warning("returns endpoint DB read failed for %s: %s", strategy_id, exc)
        raise HTTPException(status_code=500, detail="Failed to load returns") from exc

    if not daily_returns:
        raise HTTPException(status_code=404, detail="no persisted returns")

    # ── 3. Build date window from the backtest row (best-effort) ─────────────
    start: str | None = None
    end: str | None = None
    if latest_row is not None:
        if latest_row.backtest_start:
            start = str(latest_row.backtest_start)
        if latest_row.backtest_end:
            end = str(latest_row.backtest_end)

    return StrategyReturnsResponse(
        strategy_id=strategy_id,
        source="persisted_backtest",
        start=start,
        end=end,
        n=len(daily_returns),
        daily_returns=daily_returns,
    )


@strategies_router.get("/{strategy_id}/returns", response_model=StrategyReturnsResponse)
async def get_strategy_returns(strategy_id: str, request: Request):
    """Return persisted real daily returns for a strategy.

    Response schema: {strategy_id, source: "persisted_backtest", start, end,
    n, daily_returns: [...]}

    **The per-day series is REASONING, not card content, and gates on
    OWNERSHIP (#1557).** Curated / ``is_example`` strategies stay fully public
    (house demo content; ``/quant`` fetches exactly this for every curated
    library row with no session). For a generated row the series is 404 unless
    the caller OWNS it — a published row is NOT enough, because a full
    day-by-day return series lets a reader reconstruct positions and clone the
    strategy. The HEADLINE stats derived from it (``sharpe_ratio``, ``cagr``,
    ``max_drawdown``, the rigor verdict) remain on the public card served by
    ``GET /api/strategies/{id}`` and the leaderboard — publishing shares the
    result, not the derivation. See the matrix in
    ``services/strategy_visibility.py``.

    404 when the strategy does not exist (or the caller is not entitled to its
    reasoning — 404-hides-existence per the #850 ownership gating contract).
    404 with body ``{"detail": "no persisted returns"}`` when the strategy
    exists but has no BacktestResultRecord row. Never synthesizes data from
    fixture metrics; only real persisted run data is returned (#passport-honesty).

    ``owner_wallet`` is intentionally absent from the response — pseudonymous
    PII, redacted per the same policy as GET /api/strategies/{id}.
    """
    return await asyncio.to_thread(_get_strategy_returns_sync, strategy_id, request)


def _get_strategy_debate_sync(strategy_id: str, request: Request) -> dict:
    """Gate on ownership, then load the persisted transcript.

    The blocking half of the route below (#1818 P4): it holds every
    ``session.query`` and every synchronous compute, and it runs on a worker
    thread so a slow or lock-blocked read cannot stop the event loop from
    answering ``/health``.
    """
    from fastapi import HTTPException

    from archimedes.db import get_session

    # ── 1. Existence + OWNERSHIP gate ─────────────────────────────────────
    # Deliberately NOT the same gate as `get_strategy` (the card): see the
    # matrix in services/strategy_visibility.py. The card of a published
    # strategy is public; its debate transcript is not.
    strat = strategy_provider().get_strategy(strategy_id)
    is_curated = strat is not None

    if not is_curated:
        from archimedes.api.auth_siwe import get_verified_wallet
        from archimedes.models.strategy_store import StrategyRecord
        from archimedes.services.strategy_visibility import is_strategy_reasoning_visible

        with get_session() as session:
            row = session.query(StrategyRecord).filter_by(id=strategy_id).first()
            caller = get_verified_wallet(request)
            user = get_current_user(request)
            if not is_strategy_reasoning_visible(row, caller, caller_user_id=user.id if user else None):
                raise HTTPException(status_code=404, detail="Strategy not found")

    # ── 2. Load the persisted transcript ──────────────────────────────────
    from archimedes.models.debate_transcript import debate_transcript_for_strategy

    try:
        with get_session() as session:
            payload = debate_transcript_for_strategy(session, strategy_id)
    except Exception as exc:
        logger.warning("debate endpoint DB read failed for %s: %s", strategy_id, exc)
        raise HTTPException(status_code=500, detail="Failed to load debate transcript") from exc

    if payload is None:
        raise HTTPException(status_code=404, detail="no debate transcript")
    return payload


@strategies_router.get("/{strategy_id}/debate")
async def get_strategy_debate(strategy_id: str, request: Request):
    """Return the persisted bull/bear debate transcript for a generated strategy.

    Response shape: ``{strategy_id, generation_id, candidate_id, created_at,
    transcript: [{role, round, verdict, claims}, ...]}``.

    **This route is PURE REASONING and gates on OWNERSHIP, not on card-level
    visibility (#1557).** Curated / ``is_example`` strategies are always public
    (house demo content — in practice they carry no transcript at all, the
    debate society never ran for them). For a generated row the transcript is
    404 unless the caller OWNS it — a published row is NOT enough. Existence
    stays hidden either way: 404, never 403.

    Until #1557 this docstring claimed exactly that contract while the code
    asked ``is_strategy_visible``, which returns True on ``is_published`` — so
    an anonymous GET on any published strategy returned its full generation
    debate. The claim was false; the predicate is now the one that makes it
    true. Publishing consents to sharing the strategy, not the multi-agent
    argument that produced it (same reasoning as ``brief_intent`` on the detail
    route, and ``_redact_owner_wallet`` for the owner's wallet).

    404 with ``{"detail": "no debate transcript"}`` when the strategy exists
    and the caller is entitled to its reasoning but no transcript was ever
    persisted for it — every strategy generated before this table existed,
    every curated strategy, and any run whose debate step genuinely produced
    nothing (no LLM backend reachable). Never fabricates a transcript.
    """
    return await asyncio.to_thread(_get_strategy_debate_sync, strategy_id, request)


def _get_strategy_sync(strategy_id: str, request: Request) -> StrategyResponse:
    """Resolve the card from the provider, else from the passport row.

    The blocking half of the route below (#1818 P4): it holds every
    ``session.query`` and every synchronous compute, and it runs on a worker
    thread so a slow or lock-blocked read cannot stop the event loop from
    answering ``/health``.
    """
    from fastapi import HTTPException

    strat = strategy_provider().get_strategy(strategy_id)
    if strat is not None:
        # The curated branch reads the STORED verdict, exactly as the generated
        # branch below does (#1746 / PR-B). This is the endpoint the issue
        # reproduced on: it used to run a live cohort gate here and promote the
        # file's ``candidate`` to ``validated`` off that live pass, so
        # ``GET /api/strategies/1f9cfe96…`` answered ``pass``/``true``/``0.406``
        # while ``GET /api/strategies/passports/1f9cfe96…`` — a pure read of the
        # same strategy's row — answered ``candidate``/``false``/``null``.
        from archimedes.db import get_session as _get_session
        from archimedes.services.passport_loader import get_passport as _get_passport

        with _get_session() as _session:
            resp = _to_strategy_response(strat, _get_passport(_session, strategy_id))
        # The executable DSL spec (#1646). Set HERE, not inside
        # `_to_strategy_response`, because that helper also builds the list
        # route (line ~626) and the leaderboard (`leaderboard_routes.py:94`) —
        # see the field's note on `StrategyResponse` for both reasons this is
        # detail-route-only.
        #
        # Ungated on THIS branch, and that is the deliberate call rather than
        # an oversight: resolving through `strategy_provider()` is what
        # "curated / is_example house row" MEANS on this router, and the
        # #1557 matrix puts curated REASONING in the public column (no owner
        # to protect; the product already renders these rows' reasoning to
        # anonymous visitors). It is the identical curated short-circuit
        # `GET /{id}/returns` (line ~1510) and `GET /{id}/debate` (line ~1601)
        # already take BEFORE any row check. Most curated rows carry a
        # `strategy_code_path` and no spec at all, so this is usually None.
        resp.strategy_spec = strat.strategy_spec
        return resp

    from archimedes.api.auth_siwe import get_verified_wallet
    from archimedes.db import get_session
    from archimedes.models.strategy_store import StrategyRecord
    from archimedes.services.passport_loader import get_passport
    from archimedes.services.strategy_visibility import (
        is_strategy_reasoning_visible,
        is_strategy_visible,
        owns_strategy,
    )

    with get_session() as session:
        row = session.query(StrategyRecord).filter_by(id=strategy_id).first()
        # SIWE-proven wallet, not the linked-wallet lookup — see the note at
        # the sibling call site above.
        caller = get_verified_wallet(request)
        user = get_current_user(request)
        if row is not None and not is_strategy_visible(row, caller, caller_user_id=user.id if user else None):
            raise HTTPException(status_code=404, detail="Strategy not found")
        record = get_passport(session, strategy_id)
        if record is not None:
            resp = _passport_to_strategy_response(record, session=session)
            # The user's own brief (v8 Lane 3.3) lives on strategy_store, not
            # the strategy_passports row `_passport_to_strategy_response`
            # reads — `row` (StrategyRecord) is already loaded above for the
            # visibility check, so this is a free attribute read, not an
            # extra query. Deliberately set HERE, not inside the shared
            # helper: that helper also backs Library and the public
            # leaderboard (`_passport_responses` /
            # `_public_generated_strategy_responses`), and a user's free-text
            # brief has no business on either of those.
            #
            # OWNER-GATED, and deliberately STRICTER than the 404 visibility
            # check above: `is_strategy_visible` lets ANYONE read a PUBLISHED
            # row, but the brief is the user's own words, and publishing a
            # strategy consents to sharing the STRATEGY, not the sentence its
            # owner typed to ask for it (same reasoning as
            # `_redact_owner_wallet` for owner_wallet). Non-owners and
            # anonymous callers keep the schema default (None).
            #
            # `owns_strategy` (#1557) is the named form of what this used to
            # express by calling `is_strategy_visible` with `is_example` and
            # `is_published` pinned False in a hand-built row_view. Same rule,
            # same single implementation — but a reader can no longer mistake
            # the pinned-flags trick for an accident, and the ownership match
            # is not re-implemented at a call site.
            if row is not None and owns_strategy(row, caller, caller_user_id=user.id if user else None):
                resp.brief_intent = row.brief_intent
            # The executable DSL spec (#1646). REASONING, so the gate is
            # `is_strategy_reasoning_visible` — NOT `owns_strategy` above and
            # NOT `is_strategy_visible` from the 404 check. The three differ,
            # and picking the wrong one is the #1557 bug class:
            #   - `is_strategy_visible` would hand every PUBLISHED user row's
            #     executable spec to anonymous callers.
            #   - `owns_strategy` would hide a CURATED row's spec, which the
            #     matrix says is public house content.
            # `is_strategy_reasoning_visible` is the one predicate that says
            # both (is_example → public, otherwise owner-only, is_published
            # deliberately absent). Reached via the shared predicate rather
            # than re-derived here, per its own module docstring.
            #
            # `row is None` (a passport with no strategy_store mirror) fails
            # closed at the predicate AND has no spec to read anyway — the
            # spec column lives on StrategyRecord, not on the passport row.
            if row is not None and is_strategy_reasoning_visible(row, caller, caller_user_id=user.id if user else None):
                resp.strategy_spec = row.decoded_strategy_spec()
            return resp

    raise HTTPException(status_code=404, detail="Strategy not found")


@strategies_router.get("/{strategy_id}", response_model=StrategyResponse)
async def get_strategy(strategy_id: str, request: Request):
    """Get a single strategy by ID. Tries LocalStrategyProvider (curated)
    first; falls through to the strategy_passports table for fusion- and
    architect-generated strategies so they're clickable from Library.

    **This route runs no rigor gate**, on either branch. The badge, the
    four-state and the four rigor numbers are read from the strategy's stored
    verdict of record, and so are the headline metrics — the same row
    ``GET /api/strategies/passports/{strategy_id}`` publishes, which is why the
    two cannot disagree (``docs/adr/rigor-verdict-of-record.md``). The
    ``status`` served here is the promotion derived from that stored verdict,
    published on the passport payload as ``served_status``.

    Private-until-published: non-public row is 404 unless canonical user owns it,
    with linked-wallet fallback for legacy rows. 404 prevents existence probing.
    Curated strategies (provider path / is_example rows) stay fully public.

    **This route is MIXED and stays CARD-gated (#1557).** Everything the
    response carries for a generated row is card content — name, papers,
    methodology writeup, headline metrics, the rigor badge — which is exactly
    what a published strategy is published FOR, so a published row 200s for
    anonymous callers and the public detail page keeps working. The TWO
    REASONING fields on the schema — ``brief_intent`` and ``strategy_spec``
    (#1646) — are stripped for non-owners below rather than 404ing the whole
    route (the strip-don't-404 rule for mixed routes; the purely-reasoning
    siblings ``/{id}/debate`` and ``/{id}/returns`` 404 instead). Audited field
    by field against ``_passport_to_strategy_response``: those two are the only
    reasoning fields reachable here, and NEITHER is set by that shared helper —
    both are attached at this route, from rows it has already loaded.
    ``equity_curve`` is never set on this path, and the rigor/display metrics
    are aggregates, not derivation. They take DIFFERENT gates on purpose
    (``owns_strategy`` vs ``is_strategy_reasoning_visible``) because a curated
    house row has a public spec and no owner to have typed a brief — see each
    call site and the matrix in ``services/strategy_visibility.py``.
    """
    return await asyncio.to_thread(_get_strategy_sync, strategy_id, request)


@strategies_router.patch("/{strategy_id}")
async def rename_strategy(
    strategy_id: str,
    payload: dict,
    request: Request,
    user: CurrentUser = Depends(require_current_user),
):
    """Rename an owned, generated strategy — ``{"name": "<1..80 chars>"}``.

    Owner-gated: requires Better Auth session and canonical row ownership.
    Curated examples (``is_example``) are not renamable.
    The generation-time ``content_hash``/``provenance_hash`` are deliberately
    NOT recomputed — they are provenance of the original generation, not of the
    display name. The strategy_passports table carries no display-name column,
    so only strategy_store is updated.

    Ownership is decided by ``owns_strategy`` — the single implementation of
    the two-tier rule (#1557), the same predicate every sibling reader on this
    router calls. It is NOT re-derived here (#1283): the tiers are (1) a row
    carrying an ``owner_user_id`` is owned by that account and by nobody else,
    so a matching ``owner_wallet`` grants nothing, and (2) only a row with NO
    user stamp falls back to the wallet comparison. Re-implementing that
    ordering inline is how a mutating route drifts out of agreement with the
    readers that gate the same row — an authorization bug, not an
    inconsistency.

    Legacy-wallet fallback (#1283): a pre-account row (``owner_user_id`` NULL)
    matched via the caller's linked wallet — i.e. tier 2 is what granted
    ownership, which is exactly ``owns_strategy() and owner_user_id is None``
    because tier 2 is unreachable when a user stamp exists — is reclaimed onto
    canonical account ownership in the same transaction as the rename, using
    the same bulk claim
    (``claim_legacy_wallet_data``) a verified wallet link performs — but
    scoped to the strategy-side tables only (``StrategyRecord`` /
    ``StrategyPassportRecord`` / ``StrategyProposal``), with
    ``include_profile=False``. The write is irreversible (no un-claim path)
    and reaches every unclaimed strategy row for this wallet, not just the
    one being renamed — that is judged acceptable here because it can only
    ever touch rows already tied to a wallet ``get_linked_wallet_address``
    resolves for *this* account (a wallet linked elsewhere 409s at link time;
    see ``_link_verified_wallet``), the same reach a real wallet re-link would
    have. ``vault_metadata`` and ``user_profiles`` are deliberately excluded:
    a rename has no business moving vault ownership — that 409-gates on
    ``owner_user_id`` being ``None`` specifically so a legitimately
    transferred on-chain owner can still write it, and a stale reclaim here
    would slam that door shut with no way back — or adopting a PII-bearing
    profile on a lookup that never asked for a fresh signature. Full
    reclaim of those two legs stays behind the signature-verified wallet-link
    flow. This migrates pre-account strategy rows toward zero over time;
    deleting the fallback branch entirely is a follow-up gated on verifying
    no unclaimed rows remain.
    """
    from datetime import datetime

    from fastapi import HTTPException

    from archimedes.api.wallet_routes import claim_legacy_wallet_data
    from archimedes.db import get_session
    from archimedes.models.strategy_passport_record import StrategyPassportRecord
    from archimedes.models.strategy_proposal import StrategyProposal
    from archimedes.models.strategy_store import StrategyRecord
    from archimedes.services.strategy_visibility import owns_strategy

    name = payload.get("name")
    if not isinstance(name, str):
        raise HTTPException(status_code=422, detail="'name' (string) is required")
    name = name.strip()
    if not 1 <= len(name) <= 80:
        raise HTTPException(status_code=422, detail="name must be 1–80 characters after trimming")

    with get_session() as session:
        row = session.query(StrategyRecord).filter_by(id=strategy_id).first()
        if row is None or row.is_example:
            # Curated examples are not user-owned — same 404 as a missing row.
            raise HTTPException(status_code=404, detail="Strategy not found")
        caller = get_linked_wallet_address(request)
        # The canonical two-tier match, asked once, from the one place it is
        # implemented. `owns_strategy` consults `owner_wallet` ONLY when
        # `owner_user_id` is NULL, so a row stamped with another account's id
        # is not renamable by whoever happens to control the wallet it names.
        is_owner = owns_strategy(row, caller, caller_user_id=user.id)
        if is_owner and row.owner_user_id is None:
            # Tier 2 is the only tier that can have granted this (tier 1
            # requires a non-NULL stamp), so the caller is proven via a
            # linked-wallet match on a still-unclaimed row: reclaim every
            # pre-account STRATEGY row tied to this wallet (not just this
            # one), matching what re-verifying the wallet link would do for
            # those tables. vault_metadata/user_profiles are excluded — see
            # the docstring above.
            #
            # `claim_legacy_wallet_data` filters on an EXACT `owner_wallet ==
            # address` match, so it is handed the same normalized form
            # `owns_strategy` compared on. Linked wallets are stored
            # lower-cased (`issue_wallet_challenge`), so this is not a
            # live casing fix — it keeps the bulk claim's reach identical to
            # the reach of the check that authorized it, rather than relying
            # on the two agreeing by convention.
            claim_legacy_wallet_data(
                session,
                user.id,
                str(caller).strip().lower(),
                models=(
                    (StrategyRecord, StrategyRecord.owner_wallet),
                    (StrategyPassportRecord, StrategyPassportRecord.owner_wallet),
                    (StrategyProposal, StrategyProposal.owner_wallet),
                ),
                include_profile=False,
            )
            row.owner_user_id = user.id
        if not is_owner:
            # Hide unpublished rows from non-owners (404); published rows are
            # visible, so an honest 403 is returned instead.
            if row.is_published:
                raise HTTPException(status_code=403, detail="Not authorized to rename this strategy.")
            raise HTTPException(status_code=404, detail="Strategy not found")

        row.strategy_name = name
        row.updated_at = datetime.now(UTC)
        session.commit()
        return {"strategy": row.to_dict()}
