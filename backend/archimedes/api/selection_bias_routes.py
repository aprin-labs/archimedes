"""Selection-bias correction API routes.

Exposes the rigor gate for strategy validation. The main consumer is the
strategy-list page (shows PASS/FAIL per strategy) and the strategy detail
page (shows full gate breakdown).
"""

from __future__ import annotations

import numpy as np
from fastapi import APIRouter, Query, Request, Response

from archimedes.api._route_helpers import strategy_provider as _provider
from archimedes.api.limiter import limiter
from archimedes.services.dsl_lookahead_audit import verdict_from_persisted_row
from archimedes.services.rigor_evaluator import (
    assert_self_contained_cohort_correlation,
    compute_average_pairwise_correlation,
    compute_library_pbo,
    compute_library_pbo_rf_convention,
    compute_pbo,
    load_daily_returns_store,
    run_rigor_gate,
)
from archimedes.services.rigor_profiles import (
    CPCV_MIN_POSITIVE_FRACTION,
    DEFAULT_LEVEL,
    DSR_P_FLOOR,
    LOOSEST_LEVEL,
    OOS_ABS_FLOOR,
    STRICTEST_LEVEL,
    all_profiles,
)

selection_bias_router = APIRouter(prefix="/api/selection-bias", tags=["selection-bias"])

# `_provider` used to be this module's own lazily-cached strategy-provider
# accessor. Three route modules each held a separate cache over the same
# on-disk corpus, constructed at their own first-call time (#1356) — a
# fixtures backfill landing between two first-calls left them permanently
# disagreeing with each other and with the five route modules that already
# shared `_route_helpers.strategy_provider()`. Now an alias for the same
# shared singleton; call sites (`_provider().foo()`) are unchanged.


# ── Schemas ──────────────────────────────────────────────────


from pydantic import BaseModel


class RigorGateDetail(BaseModel):
    """Per-check pass/fail detail.

    Mirrors every key of ``RigorGateResult.gate_details`` so the passport surfaces
    the gate honestly — including ``cpcv`` (a real pass/fail criterion when a
    combinatorial OOS matrix exists), the ``dsr_convention`` disclosure (#547), and
    the advisory ``iid`` diagnostic (#621). Dropping a computed criterion here would
    show ``passes_all=False`` with no rendered reason.
    """

    dsr: str = "MISSING"
    pbo: str = "MISSING"
    oos_sharpe: str = "MISSING"
    look_ahead: str = "MISSING"
    cpcv: str = "MISSING"
    dsr_convention: str = "MISSING"
    # #1409: rides the same payload path as dsr_convention -- "excess_tbill_series"
    # (historical 3-month T-bill, per-window aligned) or "excess_flat_fallback" (the
    # disclosed flat-5% fallback). See RigorGateResult.rf_convention.
    rf_convention: str = "MISSING"
    iid: str = "MISSING"
    regime_robustness: str = "MISSING"


class LibraryPbo(BaseModel):
    """Library-level CSCV PBO (Bailey et al. 2014) over the whole selection set.

    Display-only (#546, option 2): this is a *selection-set property*, identical
    across every strategy, surfaced ALONGSIDE the per-strategy cohort
    ``pbo_score`` — it is strictly additive and does NOT feed any gate verdict.
    The same value is attached to ``RigorGateResponse`` and to every
    ``StrategyRigorResult`` (the passport endpoint renders from the per-strategy
    result, so the field must be present there too even though it is library-wide,
    not strategy-specific). ``value is None`` is the fail-closed / store-absent
    signal, in which case ``source == "unavailable"``.
    """

    value: float | None = None  # the single library CSCV PBO, or None (fail-closed / store absent)
    data_vintage: str | None = None  # store vintage, e.g. "2026-06-11"
    selection_set_size: int = 0  # number of strategies in the selection set
    source: str = "library_cscv"  # provenance label; "unavailable" when value is None
    # #1409 review fix (2026-08-20): `compute_library_pbo` threads the joint
    # date axis into CSCV's per-split Sharpe ranking UNCONDITIONALLY (no
    # caller opt-in, unlike `dates` on `run_rigor_gate`) -- so `value` can
    # already be computed on the historical T-bill series today, even while
    # every `run_rigor_gate` call site still reports
    # `rf_convention=excess_flat_fallback` (no live caller threads `dates`
    # there yet). Without this field, a changed `value` shipped with no
    # disclosure of which rate produced it. "MISSING" when the value itself
    # is unavailable (fewer than two series align) -- there is no convention
    # to disclose for a PBO that wasn't computed.
    rf_convention: str = "MISSING"


# Board-level BH-FDR used to live here, as a `BoardLevelFdr` top-level key plus
# three `board_fdr_*` fields on every StrategyRigorResult below. It MOVED to
# `GET /api/leaderboard` (#1564) — see api/leaderboard_schemas.BoardLevelFdr.
# Owner decision (Dan, 2026-08-31): the strategy passport carries only
# information about the strategy itself; the leaderboard is the one
# cross-strategy surface, and `board_fdr_significant` is relational by
# construction (it flips when unrelated strategies join or leave the cohort).
# `TestBoardFdrStaysOffThePerStrategyGate` in test_selection_bias_routes.py
# fails if any `board_fdr*` key reappears on these models.


class StrategyRigorResult(BaseModel):
    """Rigor gate result for a single strategy.

    ``library_pbo`` is a *selection-set property* (identical across strategies),
    not a per-strategy metric; it is included here only so the passport endpoint
    (which renders from this per-strategy result) can surface it. It is
    display-only and never affects ``passes_all`` or ``pbo_score`` (#546).

    **#1564 item 3, the explicit call: ``library_pbo`` STAYS on this
    per-strategy result; it does NOT follow ``board_fdr_*`` to the
    leaderboard.** It was flagged as the same impurity class, and it isn't —
    three checked reasons:

    1. It is not a RELATIONAL VERDICT. ``board_fdr_significant`` is a different
       answer for each strategy and flips when unrelated strategies join or
       leave the cohort — the library-coupled verdict Decision #3 of
       ``docs/adr/num-trials-self-containment.md`` rejects. ``library_pbo`` is
       byte-identical on every row of a given selection set, so no strategy's
       passport can say a different thing about that strategy because of
       another strategy. That is the property the owner decision is about.
    2. It is the SCOPE DISCLOSURE for a number this same response already
       shows. On the curated path ``pbo_score`` comes from
       ``compute_pbo(valid_returns)``, which by construction assigns ONE
       library-wide CSCV value to every strategy (pinned by
       ``analytics-engine/scripts/compute_library_pbo.py``'s own
       ``assert all(v == library_pbo ...)``, and stated in ``POST
       /api/selection-bias/pbo``'s docstring: "all strategies get the same
       score"). Deleting ``library_pbo`` would remove the label while leaving
       the labelled number — strictly less honest, and it would purify nothing.
    3. Its ``source``/``data_vintage``/``rf_convention`` fields are the
       provenance for that same PBO figure. They are only meaningful next to
       it.

    So the passport surfaces no cross-strategy VERDICT, which is what the
    decision asked for. ``test_library_pbo_is_a_constant_not_a_verdict`` pins
    reason 1 and reason 2 as executable claims rather than prose.
    """

    strategy_id: str
    strategy_name: str
    passes_all: bool
    gate_details: RigorGateDetail
    deflated_sharpe: float | None = None
    dsr_p_value: float | None = None
    pbo_score: float | None = None
    oos_sharpe: float | None = None
    in_sample_sharpe: float | None = None
    library_pbo: LibraryPbo = LibraryPbo()
    # Strictness ladder (rigor_profiles). ``passes_all`` above is the verdict at
    # ``strictness_level``; these carry the whole ladder from one computation so
    # the passport can render "passes at your level" + the deploy gate can enforce
    # it. ``min_passing_level`` is the lowest level (1..5) the strategy passes at
    # (None = fails even the loosest, or blocked_by_floor). A user at level L can
    # deploy iff ``min_passing_level is not None and min_passing_level <= L``.
    strictness_level: int = 1
    min_passing_level: int | None = None
    blocked_by_floor: bool = False
    # PROVENANCE of the num_trials this verdict was graded at (num_trials-
    # provenance audit, 2026-08-03 — the "central item"): num_trials is the
    # number of candidates the selection process that produced THIS result
    # actually evaluated, never borrowed from a neighbour or a cohort. See
    # ``_num_trials_for_generated_row`` for the values this takes and why.
    # "unspecified" is the safe default for shapes (pending/no-data) that never
    # got far enough to compute a num_trials at all — never silently implies a
    # trustworthy value.
    num_trials_scope: str = "unspecified"
    # NOTE (#1564): no `board_fdr_*` field belongs on this model. Board-level
    # BH-FDR is a cohort-relational verdict and now rides
    # `GET /api/leaderboard` only. The `None` this model used to serve was
    # triply overloaded here anyway — "no dsr_p_value to correct" (curated),
    # "generated, no cohort at all" (`_generated_strategy_rigor` never called
    # the correction), and "not significant" were indistinguishable to a
    # consumer. On the leaderboard, where a row is a cohort member by
    # construction, `None` has exactly one meaning.
    # True for a strategy with fewer than 10 persisted daily returns — the gate
    # genuinely could not run (#1358). ``passes_all`` is False for these rows
    # too (an all-MISSING gate never passes), but collapsing "never evaluated"
    # into the same bucket as "evaluated and lost" is the defect this field
    # exists to fix: a transient DB blip or a strategy that hasn't been
    # backtested yet must never render as a rigor-gate FAILURE. Both no-data
    # branches below (the cohort loop and ``_generated_strategy_rigor``) set
    # this explicitly; every other branch leaves the ``False`` default.
    pending: bool = False
    # True when the persisted return series exists but carries no variance —
    # a zero-trade backtest or broken data (``RigorGateResult.is_degenerate``,
    # #1184). This is NOT ``pending`` (there IS data) and NOT an ordinary
    # failure (nothing was measurable to fail), yet it lands on the wire looking
    # exactly like the harshest kind of failure: a degenerate series leaves
    # ``dsr_p_value``/``oos_sharpe`` at None, which trips ``blocked_by_floor``,
    # so ``blocked_by_floor=True, min_passing_level=None`` — indistinguishable
    # from a strategy that was fully graded and found broken. The deployability
    # chip rendered "Fails an always-on correctness floor" for a series no floor
    # ever got to measure. Carried here, alongside ``pending``, so the one
    # endpoint that decides deployability also says which of the three reasons
    # it is (#1358).
    degenerate: bool = False


class RigorGateResponse(BaseModel):
    """Response for the library-level rigor gate check."""

    strategies: list[StrategyRigorResult]
    total: int
    passing: int
    failing: int
    # Rows with fewer than 10 persisted daily returns — the gate genuinely could
    # not run (#1358). ``passing + failing + pending == total`` always; a
    # pending row was never counted toward ``failing`` (previously it was —
    # a strategy with zero statistics computed rendered as a rigor-gate
    # failure on the Library table, mobile cards, and deployability chip).
    pending: int = 0
    library_pbo: LibraryPbo = LibraryPbo()
    # The strictness level the ``passing``/``failing`` counts + each
    # ``passes_all`` were evaluated at (1 = strictest/badge … 5 = loosest).
    strictness_level: int = 1


class StrictnessLevelInfo(BaseModel):
    """One rung of the strictness ladder — disclosed so the UI renders labels and
    thresholds from the backend's single source of truth (rigor_profiles)."""

    level: int
    label: str
    dsr_p_min: float
    pbo_max: float
    oos_is_ratio_min: float
    description: str


class StrictnessLadderResponse(BaseModel):
    """The whole 1–5 ladder + the always-on floors + which level is the badge."""

    levels: list[StrictnessLevelInfo]
    strictest_level: int
    loosest_level: int
    badge_level: int
    default_level: int
    floors: dict[str, float]


class PBORequest(BaseModel):
    """Request to compute PBO for a set of strategy returns."""

    returns_matrix: dict[str, list[float]]
    s_partitions: int = 16


class PBOResponse(BaseModel):
    """PBO computation result."""

    pbo_scores: dict[str, float]
    interpretation: str


# ── Endpoints ────────────────────────────────────────────────


@selection_bias_router.get("/gate", response_model=RigorGateResponse)
async def evaluate_rigor_gate(
    strictness: int = Query(
        DEFAULT_LEVEL,
        ge=STRICTEST_LEVEL,
        le=LOOSEST_LEVEL,
        description="Strictness level 1 (Conservative/badge) … 5 (Speculative). "
        "Sets which level each passes_all + the passing/failing counts report at. "
        "min_passing_level is always returned regardless, so the caller can reason "
        "about the whole ladder.",
    ),
):
    """Evaluate the rigor gate for all strategies in the library.

    Runs three statistical primitives (DSR, PBO, chronological OOS Sharpe)
    plus the look-ahead static audit for each strategy.

    ``strictness`` sets the reporting level: ``passes_all`` per strategy and the
    ``passing``/``failing`` counts reflect that level's thresholds. The badge
    (``passes_rigor_gate`` on the strategy object) is unaffected — it is always
    the strictest level. Each result also carries ``min_passing_level`` so a
    caller can render the whole ladder from one call.

    CPCV (Combinatorial Purged Cross-Validation) is implemented in
    rigor_evaluator.run_rigor_gate() but requires a 2-D (S, T) matrix of
    per-split OOS returns that comes from re-running the full backtest engine
    across combinatorial window splits.  That rolling re-backtest pipeline is
    not yet wired here, so run_rigor_gate() is called without cv_returns_matrix
    and CPCV is reported as an explicit NOT_RUN status (with the reason) on every
    strategy — never a bare "MISSING" that would imply a method silently producing
    no number (#771).  Wire it once the analytics-engine supports combinatorial
    window output.
    """
    strategies = _provider().list_strategies()

    # Library-level CSCV PBO (#546, option 2): a display-only selection-set
    # property attached to the response and to each per-strategy result. It is
    # strictly additive — it never feeds run_rigor_gate or any gate verdict.
    # Computed once (cached on the store file signature) and reused everywhere.
    library_pbo = _library_pbo_payload()

    if not strategies:
        return RigorGateResponse(
            strategies=[],
            total=0,
            passing=0,
            failing=0,
            pending=0,
            library_pbo=library_pbo,
            strictness_level=strictness,
        )

    # ── Collect real daily returns from persisted backtest results ──
    from archimedes.db import get_session
    from archimedes.services.backtest_repository import (
        get_all_daily_returns,
        update_rigor_gate_fields,
    )

    strategy_ids = [s.id for s in strategies]

    # Load real returns from DB
    with get_session() as session:
        returns_by_strategy = get_all_daily_returns(session, strategy_ids)

    # ── Perf (Library page load latency, ~8-10s measured for this route) ──
    # Everything below this point — code loads for the look-ahead audit, cohort
    # PBO + average correlation, and one run_rigor_gate call per strategy (plus
    # its DB rigor-fields write-back) — is memoized in services.rigor_cache keyed
    # on a data-version token (rigor_cache.cohort_key + strictness, since the
    # strictness level changes which profile run_rigor_gate grades against). The
    # DB read above is NEVER cached — it always runs live, so the key reacts the
    # instant persisted returns change. This is honest caching: a cache hit
    # serves exactly the same StrategyRigorResult list a cache miss would have
    # computed, not a fake or stale one. rigor_cache.get_or_compute fails open —
    # any cache-layer error falls back to calling the compute closure directly —
    # so a cache bug can only make a request slow, never wrong.
    #
    # code_versions folds each strategy's strategy_code_hash into the key
    # (Copilot review, PR #1040): run_rigor_gate's look-ahead audit below reads
    # strategy_code_map[s.id], which is loaded FROM s.strategy_code_path — so the
    # cached result depends on strategy code, not just returns + strictness. A
    # key built from returns+strictness alone could keep serving a stale
    # look-ahead verdict/passes_all after a code edit even though returns are
    # unchanged. strategy_code_hash is already computed onto the Strategy object
    # by LocalStrategyProvider, so reading it here is free — no extra I/O on the
    # request path.
    #
    # NOTE: on a cache HIT the per-strategy DB rigor-fields write-back
    # (update_rigor_gate_fields) below does NOT re-run. That write-back is
    # idempotent — it re-persists the exact numbers already written the first
    # time this cohort+strictness combination was computed — so skipping it on a
    # hit changes no served or stored value; it just skips a redundant write. The
    # moment underlying returns change, the cache key changes and the write-back
    # resumes on the next (now-live) call.
    # #1518: this memo is now TWO layers — the in-process dict, and a shared
    # Redis-backed store underneath it. In-process alone did not amortise across
    # the fleet, it multiplied by it: with N Fargate tasks behind the ALB, each
    # holds its own copy and round-robin routing means a request can land on a
    # task that has not computed this cohort yet and pays the whole ~21s
    # recompute (measured on prod: 21.9s → 0.68s → 20.9s within a minute, which a
    # 600s TTL cannot produce). `model_list_codec(StrategyRigorResult)` is what
    # lets the value cross the process boundary — JSON via pydantic, so a cache
    # HIT decodes to a value indistinguishable from what a MISS computed,
    # four-state verdict vocabulary (`passes_all` / `pending` / `degenerate`)
    # included. Both invalidation triggers survive the move: the key below still
    # carries `cohort_key`, and `rigor_cache.clear()` now bumps a shared epoch.
    from archimedes.services.rigor_cache import cohort_key, get_or_compute, model_list_codec

    code_versions = {s.id: getattr(s, "strategy_code_hash", None) for s in strategies}
    cache_key = f"selection_bias_gate:strictness={strictness}:" + cohort_key(
        strategy_ids, returns_by_strategy, code_versions
    )

    def _compute() -> list[StrategyRigorResult]:
        strategy_code_map: dict[str, str | None] = {}
        for s in strategies:
            code = _load_strategy_code(s.strategy_code_path) if s.strategy_code_path else None
            strategy_code_map[s.id] = code

        # Strategies with no real backtest data report all gate fields as MISSING
        # (handled in the per-strategy loop below). Do NOT synthesize returns from
        # stub_sharpe — DSR would trivially pass because the series was constructed
        # to hit exactly that Sharpe, creating a circular validation that is
        # meaningless. The stubs remain available for UI display (portfolio page)
        # but must not feed into the rigor gate.

        # Compute PBO across all strategies that have returns. Exclude zero-variance
        # (degenerate/placeholder-flat) series BEFORE they can inflate num_trials or
        # dilute avg_correlation (#868): a flat daily_returns series (e.g. a stub row
        # that was never replaced with a real backtest) has no informational content,
        # but counting it toward the multiple-testing correction still stiffens DSR
        # for every REAL strategy in the cohort. Mirrors the exact degeneracy test
        # _rigor_helpers._sharpe_dsr_inputs already uses (np.ptp(arr) == 0 — peak-to-
        # peak range zero) so "degenerate" means the same thing everywhere in the
        # gate. This only trims the cohort-level context (num_trials/avg_correlation/
        # pbo_scores below); the per-strategy gate loop still runs every strategy
        # (including degenerate ones) against its own returns via
        # returns_by_strategy.get(s.id, []) so a degenerate strategy still correctly
        # reports MISSING/FAIL on its own row instead of silently disappearing.
        valid_returns = {
            k: v
            for k, v in returns_by_strategy.items()
            if len(v) >= 10 and float(np.ptp(np.asarray(v, dtype=float))) > 0.0
        }
        pbo_scores = compute_pbo(valid_returns) if len(valid_returns) >= 2 else {}

        # num_trials = 1: each curated strategy is graded on ITS OWN Sharpe, NOT
        # deflated by how many OTHER strategies sit in the library (Dan's principle,
        # decouple #2, 2026-07-09). A curated single-paper strategy carries no
        # generation search of ours, so its self-contained trial count is 1 (the
        # paper's headline config) — with num_trials=1 the DSR expectation-of-max
        # term collapses, so the strategy is judged purely on its own return series.
        # REVERSES the prior library-size deflation (#770/#820) — ratified by Önder
        # 2026-08-31 (#1555); it raises curated pass rates by removing a cross-strategy
        # penalty, reviewed as the removal of a penalty for a search nobody ran.
        num_trials = 1

        # The strategy library is the multiple-testing selection set; correlated
        # strategies (overlapping assets/signals) carry fewer independent trials, so
        # the DSR relaxes the penalty via the equicorrelated E[max] shrinkage
        # √(1−ρ̄) (#1558/#1559 — the earlier N_eff form was the wrong shape).
        #
        # NOTE: this cohort-wide correlation is INERT today because num_trials=1
        # above (see _dsr_from_stats: E[max_N]=0 when N==1) — it is computed and
        # threaded through for whichever criteria might use it, but cannot move
        # the DSR verdict at num_trials=1. The guard below (V4, num_trials-
        # provenance audit 2026-08-03) makes that invariant IMPOSSIBLE to
        # silently break: if a future edit ever reintroduces num_trials>1 here,
        # this raises loudly instead of quietly re-coupling every curated
        # strategy's DSR to the library's correlation structure again.
        avg_correlation = compute_average_pairwise_correlation(valid_returns) if len(valid_returns) >= 2 else 0.0
        assert_self_contained_cohort_correlation(num_trials, avg_correlation)

        # Run rigor gate for each strategy
        computed: list[StrategyRigorResult] = []
        for s in strategies:
            daily_returns = returns_by_strategy.get(s.id, [])

            if len(daily_returns) < 10:
                computed.append(
                    StrategyRigorResult(
                        strategy_id=s.id,
                        strategy_name=s.paper_title,
                        passes_all=False,
                        gate_details=RigorGateDetail(
                            dsr="MISSING (no backtest data)",
                            pbo="MISSING (no backtest data)",
                            oos_sharpe="MISSING (no backtest data)",
                            look_ahead="MISSING (no code)",
                        ),
                        library_pbo=library_pbo,
                        strictness_level=strictness,
                        min_passing_level=None,
                        blocked_by_floor=False,
                        pending=True,
                    )
                )
                continue

            # in_sample_sharpe is left None on purpose: run_rigor_gate derives it
            # from the first 70% of `daily_returns`, the same series whose last 30%
            # produces oos_sharpe. Passing the *full-sample* backtest Sharpe here
            # (the previous `bt_map[s.id].sharpe_ratio`) made the OOS/IS cliff check
            # trivially passable — a bad OOS tail drags the full-sample denominator
            # down, inflating the ratio (see rigor_evaluator.run_rigor_gate's own
            # warning at the IS-slice fallback). Let the gate compute the honest
            # first-70% in-sample denominator instead of overriding it.
            in_sample_sharpe = None

            # cv_returns_matrix intentionally omitted — CPCV requires a 2-D array
            # of per-combinatorial-split OOS returns that the analytics-engine does
            # not yet produce.  run_rigor_gate() reports cpcv as an explicit NOT_RUN
            # status with the reason (#771), not a bare "MISSING".
            gate_result = run_rigor_gate(
                strategy_id=s.id,
                daily_returns=daily_returns,
                num_trials=num_trials,
                pbo_scores=pbo_scores,
                strategy_code=strategy_code_map.get(s.id),
                in_sample_sharpe=in_sample_sharpe,
                paper_claimed_sharpe=s.paper_claimed_sharpe,
                average_correlation=avg_correlation,
                strictness_level=strictness,
            )

            # Persist rigor gate results to DB
            with get_session() as session:
                update_rigor_gate_fields(
                    session,
                    s.id,
                    deflated_sharpe_ratio=gate_result.deflated_sharpe,
                    dsr_p_value=gate_result.dsr_p_value,
                    num_trials_in_selection=num_trials,
                    pbo_score=gate_result.pbo_score,
                    out_of_sample_sharpe=gate_result.oos_sharpe,
                    look_ahead_audit_passed=gate_result.look_ahead_passed,
                )
                session.commit()

            details = gate_result.gate_details
            computed.append(
                StrategyRigorResult(
                    strategy_id=s.id,
                    strategy_name=s.paper_title,
                    passes_all=gate_result.passes_all,
                    gate_details=RigorGateDetail(
                        dsr=details.get("dsr", "MISSING"),
                        pbo=details.get("pbo", "MISSING"),
                        oos_sharpe=details.get("oos_sharpe", "MISSING"),
                        look_ahead=details.get("look_ahead", "MISSING"),
                        cpcv=details.get("cpcv", "MISSING"),
                        dsr_convention=details.get("dsr_convention", "MISSING"),
                        rf_convention=details.get("rf_convention", "MISSING"),
                        iid=details.get("iid", "MISSING"),
                        regime_robustness=details.get("regime_robustness", "MISSING"),
                    ),
                    deflated_sharpe=gate_result.deflated_sharpe,
                    dsr_p_value=gate_result.dsr_p_value,
                    pbo_score=gate_result.pbo_score,
                    oos_sharpe=gate_result.oos_sharpe,
                    in_sample_sharpe=gate_result.in_sample_sharpe,
                    library_pbo=library_pbo,
                    strictness_level=strictness,
                    min_passing_level=gate_result.min_passing_level,
                    blocked_by_floor=gate_result.blocked_by_floor,
                    degenerate=gate_result.is_degenerate,
                    num_trials_scope=_SCOPE_CURATED_SELF_CONTAINED,
                )
            )
        return computed

    # cache_if=bool: `strategies` is non-empty here (checked
    # above), so `_compute()` always appends one result per strategy today —
    # but a hard guard against ever memoizing an empty list matches
    # strategies_routes.py's `_live_rigor_results_for_strategies` (same failure
    # class: an empty result must never get "sticky" for the TTL) and costs
    # nothing when `_compute()` returns its normal non-empty list.
    results = get_or_compute(
        cache_key,
        _compute,
        cache_if=bool,
        shared_codec=model_list_codec(StrategyRigorResult),
    )

    # Copilot review (PR #1040): on a rigor_cache HIT, `results` are memoized
    # StrategyRigorResult objects whose `library_pbo` reflects whatever was
    # current at cache-WRITE time. `library_pbo` (above, line ~211) is always
    # freshly computed for THIS request. Without this reconciliation, a cache
    # hit could serve a response where the top-level `library_pbo` and each
    # per-strategy `result.library_pbo` disagree — an internally inconsistent
    # response. Rebuild (never mutate the cached objects in place, since
    # `results` may be the exact list object shared with a concurrent
    # reader of the same cache entry) with the fresh payload so top-level and
    # per-strategy always agree, on both cache hits and misses.
    results = [r.model_copy(update={"library_pbo": library_pbo}) for r in results]

    # The board-level BH-FDR reconciliation that used to sit here (#1185) moved
    # to services/leaderboard.build_leaderboard (#1564). Nothing replaced it:
    # this route grades each strategy on its own evidence and says nothing about
    # where that strategy sits relative to the field.

    # ``pending`` rows never have ``passes_all=True`` (an all-MISSING gate can't
    # pass), so ``passing`` is unaffected by carving pending out of ``failing``.
    pending = sum(1 for r in results if r.pending)
    passing = sum(1 for r in results if r.passes_all)
    return RigorGateResponse(
        strategies=results,
        total=len(results),
        passing=passing,
        failing=len(results) - passing - pending,
        pending=pending,
        library_pbo=library_pbo,
        strictness_level=strictness,
    )


# ── num_trials provenance discriminator (W3, the central item) ──────────────
#
# The rule (Dan, 2026-07-27): num_trials is the number of candidates the
# selection process that produced THIS result actually evaluated.
#   curated  (a hand-implemented published paper — no search of ours) -> 1
#   generated (the candidate pool IS the search)                      -> selection_pool_size
#
# For curated strategies (`evaluate_rigor_gate` above) that's unconditional —
# num_trials=1 is hardcoded regardless of any stored value (see the comment at
# `num_trials = 1` above). For GENERATED strategies (this function, DB-
# persisted `strategy_store` rows) num_trials has to come from a STORED
# column, and V3 is exactly the bug that read it bare: ANY populated
# ``num_trials_in_selection`` was trusted, regardless of which pipeline wrote
# it. That conflates two different things a positive integer in that column
# could mean — "a real generation search evaluated N candidates" vs. "some
# writer's default/forgotten argument happened to be N" — which the stored
# value alone cannot distinguish (traced concretely: debate_engine.py's
# dsl-fusion path and generation_pipeline.py's portfolio-simulator-v1 path
# both funnel through `_society_num_trials(pool_size)` at their one live call
# site today, but `_backtest_and_persist`'s own signature defaults
# `num_trials: int = 1` — a FUTURE caller that forgets to pass the real pool
# size would silently write the same num_trials=1 a genuinely self-contained
# strategy writes, with nothing in the column to tell them apart).
#
# The fix: discriminate on PROVENANCE — which pipeline actually computed and
# persisted this row — not on whether the column happens to be populated.
# `backtest_engine` (NOT NULL-reliable for the three pipelines that exist
# today: 'backtrader' | 'dsl-fusion' | 'portfolio-simulator-v1' — confirmed by
# repo-wide grep, num_trials-provenance audit 2026-08-03) is the only
# currently-persisted signal that says which pipeline wrote a row, so it is
# the discriminator used here. Only 'dsl-fusion' and 'portfolio-simulator-v1'
# are pipelines whose live writer stamps a genuine, tracked selection-pool
# size (both call `_society_num_trials(n_candidates)` — generation_pipeline.py
# passes it to both `_persist_real_returns` and `_backtest_and_persist`
# identically) — a row tagged with one of those AND carrying a truthy stored
# value can be trusted. Anything else reaching this DB-persisted (generated)
# code path — an unknown/missing engine tag, or a search-tracked engine whose
# writer left num_trials unset/zero — has NOT proven it tracks its own search
# count, so per the rule it must record num_trials=1 and say so explicitly
# via ``num_trials_scope``, rather than silently borrowing whatever number
# happens to be sitting in the column.
#
# Deliberately NOT a new persisted DB column (the task's "decide whether a
# dedicated deflation_scope field is warranted" question): (1) `backtest_engine`
# is already a reliable, live discriminator for every pipeline that exists
# today — there is nothing this needs that isn't already on the row; (2) a
# separate provenance-column migration (source_pipeline/computed_at/
# provenance_inferred) already exists as its own open, unreviewed PR
# (branch `provenance-backtest-results-work`) — landing an overlapping schema
# change here would fork that review and create an avoidable merge conflict
# instead of building on it later; (3) this scope label is a DERIVED,
# request-time classification of already-persisted, reliable data, not new
# ground truth — computing it at read time keeps ONE source of truth
# (`backtest_engine`) instead of two independently-writable columns that could
# drift apart. ``num_trials_scope`` on the response is the "carry an explicit
# scope marker" the task asks for — visible to callers/tests without a schema
# change.
_SEARCH_TRACKED_ENGINES = frozenset({"dsl-fusion", "portfolio-simulator-v1"})

# Scope labels for StrategyRigorResult.num_trials_scope.
_SCOPE_CURATED_SELF_CONTAINED = "curated_self_contained"  # hand-implemented paper, no search of ours -> 1
_SCOPE_GENERATED_SEARCH_POOL = "generated_search_pool"  # trusted: engine's own tracked selection-pool size
_SCOPE_GENERATED_UNTRACKED_DEFAULT = "generated_untracked_default"  # untrusted/absent -> forced to 1, explicitly


def _num_trials_for_generated_row(backtest_engine: str | None, stored_num_trials: int | None) -> tuple[int, str]:
    """``(num_trials, scope)`` for a DB-persisted (generated) strategy row.

    Discriminates on PROVENANCE (which pipeline wrote the row via
    ``backtest_engine``), not on whether ``stored_num_trials`` happens to be
    populated (V3). See the module comment above for the full rationale.
    """
    # ``> 0``, not truthiness. A stored 0 or a negative is not a smaller search
    # pool, it is a corrupt row: num_trials is a COUNT of candidates evaluated,
    # so the only values with meaning are >= 1. Truthiness would reject 0 and
    # silently ACCEPT -5, which then flows into the DSR deflation term and makes
    # the correction weaker than no correction at all -- a bad row would grade
    # more favourably than an honest one. Fail closed to 1 instead.
    if backtest_engine in _SEARCH_TRACKED_ENGINES and (stored_num_trials or 0) > 0:
        return int(stored_num_trials), _SCOPE_GENERATED_SEARCH_POOL
    return 1, _SCOPE_GENERATED_UNTRACKED_DEFAULT


def _generated_strategy_rigor(strategy_id: str, request: Request, strictness: int) -> StrategyRigorResult | None:
    """Grade a GENERATED (DB-persisted) strategy on its OWN persisted context.

    ``evaluate_rigor_gate`` only knows about the curated, filesystem-backed
    ``LocalStrategyProvider`` cohort, so ``GET /gate/{id}`` 404s for every
    fusion/architect-generated strategy — the Strategy Passport's Deploy button
    and Rigor Strictness slider are dead for them. This closes that gap WITHOUT
    folding the strategy into the curated cohort computation: merging it in would
    inflate ``num_trials`` for every curated strategy (a real strategy the
    curated multiple-testing correction never accounted for), leak a private
    strategy's existence/shape into a public cohort computation, and turn a
    single-strategy request into an O(library) recompute. Instead this grades
    the strategy on its OWN persisted ``num_trials_in_selection`` / ``pbo_score``
    / ``look_ahead_audit_passed`` — the exact numbers already written by the
    generation pipeline (``update_rigor_gate_fields`` / the DSL-fusion insert
    path) — via the same ``run_rigor_gate`` primitive the curated path uses.

    Ownership (security-critical — copied verbatim from
    ``strategies_routes.get_strategy`` pattern): non-public generated row is
    visible only to canonical owner, with linked-wallet fallback for legacy rows.
    Returns ``None`` for BOTH "no such strategy" and "exists but not visible to
    this caller" so the route 404s either way — existence must never leak to a
    non-owner via a different response shape.

    **This is CARD-level and stays on ``is_strategy_visible`` — a deliberate
    call, not an oversight (#1557).** #1557 moved the reasoning-disclosure
    surfaces (debate transcript, full daily-return series, DSL spec) onto
    ``is_strategy_reasoning_visible``, which ignores ``is_published``. This
    route is not one of them, for two reasons that were checked rather than
    assumed:

      1. Every number it returns — ``dsr_p_value``, ``pbo_score``,
         ``out_of_sample_sharpe``, ``deflated_sharpe_ratio``,
         ``passes_rigor_gate`` — is ALREADY served anonymously for published
         rows by ``strategies_routes._public_generated_strategy_responses`` on
         ``GET /api/leaderboard``. Gating here would close nothing while the
         identical values stayed public one route over — a guard that rejects
         nothing real.
      2. The rigor verdict is the CERTIFICATION of a public claim, not the
         derivation behind it. A published card asserts "this passed the gate";
         the product's whole positioning is that a reader can check that
         assertion. Making the check owner-only would leave the claim standing
         and the verification private.

    The existing contract test ``test_generated_strategy_published_visible_to_
    anonymous_caller`` pins this. If a future ticket decides the gate detail IS
    private, it must also stop the leaderboard from publishing the same
    numbers, or it changes nothing.

    Returns a MISSING-shaped result (mirroring the curated "no backtest data"
    branch above) when the strategy exists, is visible, but has fewer than 10
    persisted daily returns — the honest "Pending Backtest" case, not a 404.
    """
    from archimedes.api.account_auth import get_current_user
    from archimedes.api.auth_siwe import get_verified_wallet
    from archimedes.db import get_session
    from archimedes.models.strategy_store import StrategyRecord
    from archimedes.services.backtest_repository import get_daily_returns, latest_backtests_by_strategy
    from archimedes.services.strategy_visibility import is_strategy_visible

    with get_session() as session:
        row = session.query(StrategyRecord).filter_by(id=strategy_id).first()
        caller = get_verified_wallet(request)
        user = get_current_user(request)
        # Canonical-owner check lives in is_strategy_visible, not here. This
        # branch previously re-implemented the owner_user_id rule inline right
        # after already calling the shared predicate, which meant two copies of
        # an authorization rule that could drift apart. The wallet passed for
        # the legacy fallback is deliberately the SIWE-VERIFIED wallet, not the
        # Better Auth linked wallet: legacy rows were created under the SIWE
        # model, so accepting a merely-linked wallet would let someone claim a
        # pre-migration row without ever proving control of it.
        if not is_strategy_visible(row, caller, caller_user_id=user.id if user else None):
            return None

        strategy_name = row.strategy_name
        daily_returns = get_daily_returns(session, strategy_id)

        if len(daily_returns) < 10:
            return StrategyRigorResult(
                strategy_id=strategy_id,
                strategy_name=strategy_name,
                passes_all=False,
                gate_details=RigorGateDetail(
                    dsr="MISSING (no backtest data)",
                    pbo="MISSING (no backtest data)",
                    oos_sharpe="MISSING (no backtest data)",
                    look_ahead="MISSING (no code)",
                ),
                library_pbo=_library_pbo_payload(),
                strictness_level=strictness,
                min_passing_level=None,
                blocked_by_floor=False,
                pending=True,
            )

        # Persisted context from the latest backtest row — never recomputed
        # against any cohort (curated or otherwise). num_trials is resolved by
        # PROVENANCE, not a bare column read (V3 — see
        # ``_num_trials_for_generated_row`` above for the full rationale): only
        # a row from a pipeline whose live writer tracks its own selection-pool
        # size ('dsl-fusion' | 'portfolio-simulator-v1') is trusted to carry a
        # real N; anything else is forced to num_trials=1 with an explicit
        # ``num_trials_scope`` marker rather than silently trusting whatever
        # happens to be in the column. A missing pbo_score stays None, which
        # run_rigor_gate treats fail-closed (criterion 4 FAILs rather than
        # silently passing) — exactly the same fail-closed contract the curated
        # path relies on.
        #
        # NOTE: pbo_library_size is intentionally left unset (None) below. That
        # parameter powers run_rigor_gate's PBO power-floor, which RELAXES
        # criterion 4 when the PBO estimate is drawn from too small a cohort to be
        # trustworthy. A single generated strategy has no such cohort, and we hold
        # Dan's principle that a strategy's rigor must be measured on ITS OWN
        # context — so we deliberately grant no cohort-size relaxation here. The
        # effect is strictly CONSERVATIVE (a generated strategy's persisted PBO is
        # judged on its face, never softened by a library-size argument it doesn't
        # have) — consistent with "never weaken the gate", not an oversight.
        latest = latest_backtests_by_strategy(session, [strategy_id]).get(strategy_id)
        num_trials, num_trials_scope = _num_trials_for_generated_row(
            latest.backtest_engine if latest else None,
            latest.num_trials_in_selection if latest else None,
        )
        persisted_pbo = latest.pbo_score if latest else None
        # The stored boolean is not read on its own: `look_ahead_audit_source`
        # says what produced it, and two provenances mean it is NOT an audit pass
        # — a DSL audit that reached no verdict, and the retired "self_attested"
        # rows whose boolean was the generating model's own look_ahead_safe
        # declaration. Both come back False with a `pending` status, so the
        # detail line says NOT_RUN instead of accusing the strategy of a leak,
        # and neither can deploy on a claim nothing measured. See
        # dsl_lookahead_audit.verdict_from_persisted_row.
        persisted_look_ahead, persisted_la_status, persisted_la_reason = verdict_from_persisted_row(
            latest.look_ahead_audit_passed if latest else False,
            latest.look_ahead_audit_source if latest else None,
        )

    gate_result = run_rigor_gate(
        strategy_id=strategy_id,
        daily_returns=daily_returns,
        num_trials=num_trials,
        library_pbo=persisted_pbo,
        look_ahead_audit_passed=persisted_look_ahead,
        look_ahead_status=persisted_la_status,
        look_ahead_not_run_reason=persisted_la_reason,
        in_sample_sharpe=None,
        average_correlation=0.0,
        strictness_level=strictness,
    )

    details = gate_result.gate_details
    return StrategyRigorResult(
        strategy_id=strategy_id,
        strategy_name=strategy_name,
        passes_all=gate_result.passes_all,
        gate_details=RigorGateDetail(
            dsr=details.get("dsr", "MISSING"),
            pbo=details.get("pbo", "MISSING"),
            oos_sharpe=details.get("oos_sharpe", "MISSING"),
            look_ahead=details.get("look_ahead", "MISSING"),
            cpcv=details.get("cpcv", "MISSING"),
            dsr_convention=details.get("dsr_convention", "MISSING"),
            rf_convention=details.get("rf_convention", "MISSING"),
            iid=details.get("iid", "MISSING"),
            regime_robustness=details.get("regime_robustness", "MISSING"),
        ),
        deflated_sharpe=gate_result.deflated_sharpe,
        dsr_p_value=gate_result.dsr_p_value,
        pbo_score=gate_result.pbo_score,
        oos_sharpe=gate_result.oos_sharpe,
        in_sample_sharpe=gate_result.in_sample_sharpe,
        library_pbo=_library_pbo_payload(),
        strictness_level=strictness,
        min_passing_level=gate_result.min_passing_level,
        blocked_by_floor=gate_result.blocked_by_floor,
        degenerate=gate_result.is_degenerate,
        num_trials_scope=num_trials_scope,
    )


@selection_bias_router.get("/gate/{strategy_id}", response_model=StrategyRigorResult)
@limiter.limit("10/minute")
async def evaluate_strategy_rigor(
    request: Request,
    response: Response,  # noqa: ARG001 — slowapi injects rate-limit headers into it; omitting it 500s every SUCCESSFUL call (#1182)
    strategy_id: str,
    strictness: int = Query(DEFAULT_LEVEL, ge=STRICTEST_LEVEL, le=LOOSEST_LEVEL),
):
    """Evaluate rigor gate for a single strategy at ``strictness`` (default = badge).

    The response's ``passes_all`` reflects ``strictness``; ``min_passing_level``
    tells the passport the lowest level at which the strategy is deployable.

    Tries the curated ``LocalStrategyProvider`` cohort first (unchanged
    behavior); when the id isn't curated, falls through to
    ``_generated_strategy_rigor`` so fusion/architect-generated strategies grade
    on their own persisted context instead of 404ing outright (#deploy-gate).
    """
    strategy = _provider().get_strategy(strategy_id)
    if strategy is None:
        generated = _generated_strategy_rigor(strategy_id, request, strictness)
        if generated is not None:
            return generated

        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Strategy not found")

    # Run the full gate (cohort context matters) and extract the matching strategy.
    full_response = await evaluate_rigor_gate(strictness=strictness)
    for result in full_response.strategies:
        if result.strategy_id == strategy_id:
            return result

    from fastapi import HTTPException

    raise HTTPException(status_code=404, detail="Strategy not found in gate results")


@selection_bias_router.get("/strictness-ladder", response_model=StrictnessLadderResponse)
async def get_strictness_ladder():
    """Disclose the full 1–5 strictness ladder + always-on floors.

    Single source of truth for the frontend slider: labels, per-level thresholds,
    which level is the Archimedes Verified badge bar, and the correctness floors
    that no level bypasses (so the UI can honestly say "even the riskiest level
    still enforces X").
    """
    levels = [
        StrictnessLevelInfo(
            level=p.level,
            label=p.label,
            dsr_p_min=p.dsr_p_min,
            pbo_max=p.pbo_max,
            oos_is_ratio_min=p.oos_is_ratio_min,
            description=p.description,
        )
        for p in all_profiles()
    ]
    return StrictnessLadderResponse(
        levels=levels,
        strictest_level=STRICTEST_LEVEL,
        loosest_level=LOOSEST_LEVEL,
        badge_level=STRICTEST_LEVEL,
        default_level=DEFAULT_LEVEL,
        floors={
            "dsr_p_floor": DSR_P_FLOOR,
            "oos_abs_floor": OOS_ABS_FLOOR,
            "cpcv_min_positive_fraction": CPCV_MIN_POSITIVE_FRACTION,
        },
    )


@selection_bias_router.post("/pbo", response_model=PBOResponse)
@limiter.limit("20/minute")
async def compute_pbo_endpoint(req: PBORequest, request: Request, response: Response):  # noqa: ARG001 — slowapi @limiter.limit inspects param name
    """Compute PBO across a set of strategy return series.

    This is the library-level metric — all strategies get the same score.
    """
    pbo_scores = compute_pbo(req.returns_matrix, s_partitions=req.s_partitions)

    score = next(iter(pbo_scores.values())) if pbo_scores else 0.0
    if score >= 0.5:
        interpretation = (
            f"PBO={score:.4f}: The in-sample-optimal strategy is expected to "
            f"underperform the median out-of-sample. FAILED rigor gate."
        )
    else:
        interpretation = f"PBO={score:.4f}: Low overfitting probability. PASSED rigor gate."

    return PBOResponse(pbo_scores=pbo_scores, interpretation=interpretation)


# ── Helpers ──────────────────────────────────────────────────


# Cache the (expensive) library CSCV PBO keyed on the store's DB signature so
# the C(16, 8) = 12,870-combination CSCV runs only when the daily-returns store
# actually changes — matching compute_library_pbo's documented "recompute on
# store growth" refresh cadence. #774: the store moved from committed JSON files
# to the strategy_daily_returns table; (row_count, max_id) is the DB-native
# equivalent of the old (filename, st_mtime_ns) file signature — it changes
# whenever rows are added, and a stem's replace-on-remeasure (delete + reinsert)
# gets fresh autoincrement ids, so a same-count replace still flips max_id.
_LIBRARY_PBO_CACHE: dict[tuple[int, int], tuple[float | None, str | None, int]] = {}


def _store_signature() -> tuple[int, int] | None:
    """``(row_count, max_id)`` over ``strategy_daily_returns``.

    Returns ``None`` when the table is empty or unreachable (degrades
    gracefully). Used as the cache key: any row added/replaced changes the
    signature and forces a recompute; an unchanged store reuses the cached
    value.
    """
    from sqlalchemy import func

    from archimedes.db import get_session
    from archimedes.models.daily_returns_store import StrategyDailyReturn

    try:
        with get_session() as session:
            count, max_id = session.query(func.count(StrategyDailyReturn.id), func.max(StrategyDailyReturn.id)).one()
    except Exception:
        return None
    if not count:
        return None
    return (count, max_id or 0)


def _cached_library_pbo() -> tuple[float | None, str | None, int, str]:
    """Load the daily-returns store and compute the single library CSCV PBO.

    Returns ``(value, data_vintage, selection_set_size, rf_convention)`` where
    ``value`` is the library PBO (``None`` fail-closed / store empty),
    ``data_vintage`` is the store's max vintage, ``selection_set_size`` is the
    number of aligned series actually used by the CSCV, and ``rf_convention``
    (#1409 review fix) discloses which rf rate ``value`` was actually computed
    against. ``compute_library_pbo`` is called here at its default
    ``use_tbill_series=False`` (#1409 round-4 review fix — the mechanism to
    thread the joint date axis exists and is tested, but is not flipped on for
    this live route, matching every ``run_rigor_gate`` call site's own
    not-yet-wired ``dates`` default), so ``rf_convention`` is always
    ``excess_flat_fallback`` here today — byte-identical to every pre-#1409
    grade. Cached on the store's DB signature so the expensive CSCV does not
    re-run on every request.

    Never raises: an empty store or a DB read failure yields
    ``(None, None, 0, "MISSING")``.
    """
    from archimedes.services.rigor_evaluator import align_returns_store

    signature = _store_signature()
    if signature is None:
        return None, None, 0, "MISSING"
    if signature in _LIBRARY_PBO_CACHE:
        return _LIBRARY_PBO_CACHE[signature]

    store, data_vintage = load_daily_returns_store()
    # selection_set_size = number of series that actually survive date-alignment
    # (the count CSCV runs over), not the raw row count.
    selection_set_size = len(align_returns_store(store))
    value = compute_library_pbo(store)
    # "MISSING" (not whatever compute_library_pbo_rf_convention resolved) when
    # there's no value to attribute a convention to — e.g. the joint window is
    # too short for compute_library_pbo's own s_partitions guard, a case that
    # guard doesn't need to know about since it only resolves dates, not PBO.
    rf_convention = compute_library_pbo_rf_convention(store) if value is not None else "MISSING"
    result = (value, data_vintage, selection_set_size, rf_convention)
    _LIBRARY_PBO_CACHE[signature] = result
    return result


def _library_pbo_payload() -> LibraryPbo:
    """Build the display-only ``LibraryPbo`` for attachment to gate responses.

    Display-only (#546): never feeds the gate verdict. When the store is
    unavailable or the PBO fails closed, returns ``LibraryPbo(value=None,
    source="unavailable")`` and never crashes.
    """
    value, data_vintage, selection_set_size, rf_convention = _cached_library_pbo()
    if value is None:
        return LibraryPbo(
            value=None,
            data_vintage=data_vintage,
            selection_set_size=selection_set_size,
            source="unavailable",
            rf_convention=rf_convention,
        )
    return LibraryPbo(
        value=value,
        data_vintage=data_vintage,
        selection_set_size=selection_set_size,
        source="library_cscv",
        rf_convention=rf_convention,
    )


def _load_strategy_code(code_path: str) -> str | None:
    """Load strategy source code for look-ahead audit.

    Security: resolved path must stay within the project tree to prevent
    path-traversal reads of arbitrary files (e.g. ``../../etc/passwd``).
    """
    import os
    from pathlib import Path

    if not code_path:
        return None

    project_root = Path(os.getcwd()).resolve()
    strategies_dir = (project_root / "analytics-engine" / "strategies").resolve()

    # Resolve relative to project root
    candidates = [
        code_path,
        os.path.join(os.getcwd(), code_path),
        os.path.join(os.getcwd(), "analytics-engine", code_path),
    ]

    for raw_path in candidates:
        resolved = Path(raw_path).resolve()
        # Guard: must be within the project tree
        if not (resolved.is_relative_to(project_root) or resolved.is_relative_to(strategies_dir)):
            continue
        if resolved.is_file():
            try:
                return resolved.read_text()
            except Exception:
                pass

    return None
