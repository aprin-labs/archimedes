"""Leaderboard scoring engine — the testnet engagement engine (North Star §5).

Ranks library strategies by a **transparent conviction score** built from *real*
passport data: the rigor gate (DSR / PBO / OOS) plus backtest performance. The
score is a documented weighted sum of four [0,1] inputs — never a black box — and
every input is echoed per-entry so the user sees what drove the rank.

Two axes, honestly separated:
  • Validation axis (LIVE NOW): rigor gate + backtest — real passport fields.
  • Forward axis (PENDING): per-strategy StockBench + live paper-P&L — surfaced
    as honest "pending" until that data flows, so the engine visibly *pairs*
    them with validation (per Dan's call) without inventing values.

TWO BOARDS (Lane 3.4). ``build_leaderboard`` ranks by conviction, which is
100% backtest-era; ``build_live_paper_leaderboard`` ranks ONLY what is running
forward, compounded from the append-only paper ledger. They are separate
functions returning separate response types on purpose — there is no blended
score and no code path that produces one, because a blend would let a strong
backtest carry a strategy that has traded forward for four days. Every row
either function emits carries ``performance_basis`` naming which of the two it
is.

THE CROSS-STRATEGY SURFACE (#1564). Owner decision (Dan, 2026-08-31): the
strategy passport carries only information about the strategy itself, and this
board is the one place a strategy is placed against the field. So the
board-level Benjamini-Hochberg FDR correction (#1185) lives here — it used to
ride ``GET /api/selection-bias/gate``'s per-strategy result, where a
strategy's own numbers moved because unrelated strategies joined the library.
It is ADVISORY: it is not an input to ``compute_conviction`` and it never
changes ``passes_rigor_gate``.

This module is pure: it takes ``StrategyResponse`` objects (or, for the
forward board, already-loaded ``LivePaperLedger`` values) and returns schema
objects. No DB, no network — trivially unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from archimedes.api.leaderboard_schemas import (
    BASIS_BACKTEST,
    BASIS_LIVE_PAPER,
    BoardLevelFdr,
    LeaderboardEntry,
    LeaderboardForwardAxis,
    LeaderboardResponse,
    LeaderboardScoreComponents,
    LeaderboardScoringEngine,
    LivePaperEntry,
    LivePaperLeaderboardResponse,
    StockBenchGlobalContext,
)
from archimedes.api.schemas import StrategyResponse
from archimedes.services.rigor_evaluator import DEFAULT_BOARD_FDR_LEVEL, compute_board_level_fdr

# ── Board-level BH-FDR (#1185, relocated onto this board by #1564) ───────────
# The one-line statement of what the correction did, echoed on the response so
# the board never restates the math by hand (same rule as the conviction
# `methodology` string below). `{alpha}` is filled from the α actually used.
BOARD_FDR_METHODOLOGY = (
    "Benjamini-Hochberg FDR at α={alpha} over the classical p-values (1 − DSR confidence) of "
    "every strategy on this board with a finite DSR — the multiple-testing correction for "
    "picking one strategy out of the field. Advisory: it never changes the rigor-gate badge "
    "or the conviction score."
)

# ── Scoring weights (explicit + echoed in the response) ──────────────────────
# Rationale: passing the selection-bias gate is the single biggest *honest*
# credibility signal, so it carries the most weight; DSR confidence and
# out-of-sample performance are the next strongest "is the edge real?" signals;
# overfitting resistance (low PBO) rounds it out. Sum = 1.0.
WEIGHTS: dict[str, float] = {
    "gate": 0.35,
    "dsr_confidence": 0.25,
    "oos_performance": 0.25,
    "overfitting_resistance": 0.15,
}

#: An out-of-sample Sharpe of 1.0 earns full marks on the OOS component.
OOS_TARGET = 1.0

#: The one real StockBench datum we have — the whole agent pipeline run
#: (Chen et al. 2026), NOT per-strategy. Surfaced as honest global context.
#: Source: docs/benchmarks/stockbench-results.md.
STOCKBENCH_GLOBAL = StockBenchGlobalContext(
    sortino=-0.91,
    return_pct=-2.3,
    max_drawdown_pct=-6.2,
    rank="15/15",
    window="2025-03-03 → 2025-06-30 (82 trading days)",
    source="docs/benchmarks/stockbench-results.md",
)

_DISCLAIMER = (
    "Testnet — paper/simulated performance. Strategies are ranked on real, "
    "rigor-gated backtest results. Per-strategy StockBench and live paper-P&L "
    "are the next inputs to this engine and are not scored per strategy yet; "
    "no number here is fabricated."
)

# Sortable real fields → (StrategyResponse attribute, higher_is_better).
_SORTABLE: dict[str, tuple[str, bool]] = {
    "conviction_score": ("conviction_score", True),  # computed, handled specially
    "sharpe_ratio": ("sharpe_ratio", True),
    "cagr": ("cagr", True),
    "sortino_ratio": ("sortino_ratio", True),
    "calmar_ratio": ("calmar_ratio", True),
    "deflated_sharpe_ratio": ("deflated_sharpe_ratio", True),
    "dsr_p_value": ("dsr_p_value", True),
    "out_of_sample_sharpe": ("out_of_sample_sharpe", True),
    "pbo_score": ("pbo_score", False),  # lower is better
}

_MEDALS = {1: "gold", 2: "silver", 3: "bronze"}


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def compute_conviction(resp: StrategyResponse) -> tuple[float, LeaderboardScoreComponents]:
    """Return (score 0–100, the four real components). Missing inputs score 0 and
    lower ``data_completeness`` — so placeholders honestly sink, never inflate."""
    # A placeholder backtest carries NO real validation data. Even if DSR/OOS/PBO
    # fields happen to be populated (a seeded record can carry placeholder
    # numbers), they are not real backtest output — so EVERY component scores 0
    # and the entry sinks. Without this guard a placeholder could ride borrowed
    # DSR/OOS/PBO values (75% of the weight) above a real-but-partially-missing
    # strategy, which is exactly the inflation the docstring promises not to do.
    if resp.is_backtest_placeholder:
        zero = LeaderboardScoreComponents(
            gate=0.0,
            dsr_confidence=0.0,
            oos_performance=0.0,
            overfitting_resistance=0.0,
            data_completeness=0.0,
        )
        return 0.0, zero

    gate = 1.0 if resp.passes_rigor_gate else 0.0

    dsr_real = resp.dsr_p_value is not None
    dsr_confidence = _clamp01(resp.dsr_p_value) if dsr_real else 0.0

    oos_real = resp.out_of_sample_sharpe is not None
    oos_performance = _clamp01(resp.out_of_sample_sharpe / OOS_TARGET) if oos_real else 0.0

    pbo_real = resp.pbo_score is not None
    overfitting_resistance = _clamp01(1.0 - resp.pbo_score) if pbo_real else 0.0

    # gate is always a real signal for a non-placeholder strategy (+1); the other
    # three count only when their field is populated.
    real_count = 1 + int(dsr_real) + int(oos_real) + int(pbo_real)
    components = LeaderboardScoreComponents(
        gate=gate,
        dsr_confidence=dsr_confidence,
        oos_performance=oos_performance,
        overfitting_resistance=overfitting_resistance,
        data_completeness=real_count / 4.0,
    )

    score = 100.0 * (
        WEIGHTS["gate"] * gate
        + WEIGHTS["dsr_confidence"] * dsr_confidence
        + WEIGHTS["oos_performance"] * oos_performance
        + WEIGHTS["overfitting_resistance"] * overfitting_resistance
    )
    return round(score, 1), components


def _entry(resp: StrategyResponse) -> LeaderboardEntry:
    score, components = compute_conviction(resp)
    name = resp.paper_title or (resp.methodology_summary or resp.id)[:80]
    creator = resp.curator_wallet or "Archimedes"
    return LeaderboardEntry(
        rank=0,  # assigned after sort
        medal=None,
        id=resp.id,
        name=name,
        creator=creator,
        conviction_score=score,
        score_components=components,
        sharpe_ratio=resp.sharpe_ratio,
        cagr=resp.cagr,
        sortino_ratio=resp.sortino_ratio,
        max_drawdown=resp.max_drawdown,
        calmar_ratio=resp.calmar_ratio,
        deflated_sharpe_ratio=resp.deflated_sharpe_ratio,
        dsr_p_value=resp.dsr_p_value,
        pbo_score=resp.pbo_score,
        out_of_sample_sharpe=resp.out_of_sample_sharpe,
        passes_rigor_gate=resp.passes_rigor_gate,
        is_backtest_placeholder=resp.is_backtest_placeholder,
        backtest_engine=resp.backtest_engine,
        cost_model_id=resp.cost_model_id,
        metrics_source=resp.metrics_source,
        forward=LeaderboardForwardAxis(),
        # Provenance (Lane 3.4): this board's numbers are backtest-era, and the
        # window they were measured over travels with them. Both fields already
        # existed on StrategyResponse and simply stopped here — the same defect
        # shape as backtest_engine/cost_model_id before #1187: the information
        # existed all the way to the response builder and was dropped at the one
        # surface that puts rows side by side.
        performance_basis=BASIS_BACKTEST,
        backtest_start=resp.backtest_start,
        backtest_end=resp.backtest_end,
        regime_tag=resp.regime_tag,
        return_source=resp.return_source,
        status=resp.status,
        papers=resp.papers,
    )


def _sort_key(entry: LeaderboardEntry, field: str):
    """Raw value of the sort field for an entry (None if not evaluated).
    None-handling (push to bottom regardless of order) is done by the caller."""
    if field == "conviction_score":
        return entry.conviction_score
    attr, _ = _SORTABLE[field]
    return getattr(entry, attr)


def build_leaderboard(
    responses: list[StrategyResponse],
    *,
    sort_by: str = "conviction_score",
    order: str = "desc",
    regime_tag: str | None = None,
    min_rigor: bool = False,
    limit: int = 50,
    scope: str = "curated",
    degraded: bool = False,
    degraded_reason: str = "",
) -> LeaderboardResponse:
    """Rank strategies into a leaderboard. Pure — no I/O.

    ``scope`` is echoed back verbatim (never derived here) — it is the caller's
    statement of what cohort ``responses`` actually is ('own': the signed-in
    caller's own strategies; 'curated': the curated seed library), so the UI
    can label the board honestly even when the caller's requested scope was
    silently coerced (e.g. an anonymous request for 'own').

    ``degraded`` / ``degraded_reason`` are likewise echoed verbatim — the
    caller (route layer) is the one that knows whether ``responses`` reflects
    a real query or a swallowed failure (#1356); this function stays pure and
    just carries the signal through onto the wire.
    """
    if sort_by not in _SORTABLE:
        sort_by = "conviction_score"
    order = "asc" if order == "asc" else "desc"

    entries = [_entry(r) for r in responses]

    # ── Board-level BH-FDR over this board's cohort (#1185 → #1564) ─────────
    #
    # Placement: this is the ONE cross-strategy surface (owner decision, Dan
    # 2026-08-31), so the correction rides this response and no longer rides
    # the per-strategy gate.
    #
    # Cache: none of its own, deliberately. Every `dsr_p_value` here arrives on
    # an already-built `StrategyResponse` — since #1746 / PR-B those are read
    # off the stored grade on `strategy_passports` (one batched query per
    # request, no gate run at all), and the BH itself is pure numpy over at most
    # a few hundred floats. So the correction always matches the cohort actually
    # being served rather than a cache-write-time one. Note what this makes
    # true: every p-value in the cohort was produced by a gate run whose version
    # the row records, so a board mixing two gate vintages is now VISIBLE
    # (`gate_version`) rather than silently averaged over.
    #
    # Cohort = ALL entries, BEFORE the regime/min_rigor filters and BEFORE
    # `limit`. This is load-bearing, not incidental: BH's adjusted p-value is
    # p_(k) × m/k, so a SMALLER m makes every row MORE significant. If m
    # tracked the filtered/paged view, a reader could narrow a filter or drop
    # `limit` until a row went significant — manufacturing exactly the
    # selection effect this correction exists to price in. The multiple-testing
    # burden is a property of how many strategies were graded, not of what the
    # viewer chose to look at, so a row's correction is invariant to both
    # controls (pinned by test_leaderboard_board_fdr.py).
    board_fdr = compute_board_level_fdr({e.id: e.dsr_p_value for e in entries}, fdr_level=DEFAULT_BOARD_FDR_LEVEL)
    for e in entries:
        # A row absent from `board_fdr` had no finite dsr_p_value to correct.
        # It keeps the schema default (None) — an honest "not corrected", never
        # a fabricated False.
        corrected = board_fdr.get(e.id)
        if corrected is None:
            continue
        e.board_fdr_significant = bool(corrected["board_fdr_significant"])
        e.board_fdr_adjusted_p = float(corrected["board_fdr_adjusted_p"])
        e.board_fdr_confidence = float(corrected["board_fdr_confidence"])
    board_level_fdr = BoardLevelFdr(
        fdr_level=DEFAULT_BOARD_FDR_LEVEL,
        n_tested=len(board_fdr),
        n_significant=sum(1 for v in board_fdr.values() if v["board_fdr_significant"]),
        methodology=BOARD_FDR_METHODOLOGY.format(alpha=DEFAULT_BOARD_FDR_LEVEL),
    )

    if regime_tag:
        entries = [e for e in entries if e.regime_tag == regime_tag]
    if min_rigor:
        entries = [e for e in entries if e.passes_rigor_gate and not e.is_backtest_placeholder]

    # Split present vs missing so None always lands at the bottom, whatever the
    # order. Present values sort by the requested direction.
    def value_of(e: LeaderboardEntry):
        return _sort_key(e, sort_by)

    present = [e for e in entries if value_of(e) is not None]
    missing = [e for e in entries if value_of(e) is None]
    present.sort(key=value_of, reverse=(order == "desc"))
    ranked = present + missing

    for i, e in enumerate(ranked, start=1):
        e.rank = i
        e.medal = _MEDALS.get(i)

    total = len(ranked)
    ranked = ranked[:limit]

    engine = LeaderboardScoringEngine(
        weights=WEIGHTS,
        oos_target=OOS_TARGET,
        methodology=(
            "conviction_score = 100 × (0.35·gate + 0.25·DSR_confidence + "
            "0.25·OOS_performance + 0.15·overfitting_resistance); every input is a "
            "real passport field, clamped to [0,1]."
        ),
        stockbench_global=STOCKBENCH_GLOBAL,
        disclaimer=_DISCLAIMER,
    )
    return LeaderboardResponse(
        entries=ranked,
        total=total,
        performance_basis=BASIS_BACKTEST,
        sort_by=sort_by,
        order=order,
        scope=scope,
        scoring_engine=engine,
        board_level_fdr=board_level_fdr,
        degraded=degraded,
        degraded_reason=degraded_reason,
    )


# ── Live paper board — the forward surface (Lane 3.4) ────────────────────────

LIVE_PAPER_METHODOLOGY = (
    "cumulative_return = ∏(1 + daily_return) − 1 over every observation the "
    "append-only paper ledger holds for this deployment, from its inception "
    "date to `as_of`. Not annualised, not extrapolated, not blended with any "
    "backtest number."
)

LIVE_PAPER_DISCLAIMER = (
    "Testnet — paper/simulated forward performance. These rows are ranked ONLY "
    "on what each deployment has actually done since it went live; a few days "
    "of forward data is a few days of forward data, and is not evidence the "
    "edge is real. The research board's conviction score is a separate, "
    "backtest-era claim and the two are never combined."
)


@dataclass(frozen=True)
class LivePaperLedger:
    """One active paper deployment plus the forward ledger already loaded for it.

    The DTO boundary that keeps this module pure: the route layer does the DB
    work (``paper_deployments`` ⋈ ``paper_daily_returns``) and hands the result
    over as plain values, so the ranking itself stays trivially testable.

    ``returns`` may legitimately be EMPTY — a deployment opened today, before
    its first advance, has no observations. That case is the reason this type
    exists rather than a pre-filtered list: the builder must be the thing that
    drops it, so the drop is one auditable place with one test on it.
    """

    deployment_id: str
    strategy_id: str
    name: str
    inception: date
    returns: list[tuple[date, float]] = field(default_factory=list)
    last_appended_at: datetime | None = None
    drift_detected: bool = False
    # The STORED rigor verdict for this deployment's strategy, as the four
    # JSON-ready fields `passport_loader.stored_rigor_verdicts` produces
    # (#1764). Loaded by the caller — this module does no I/O — and `None`
    # means "none was loaded", which the builder renders as the ungraded
    # verdict rather than as silence. There is no arm here that omits the
    # verdict from a row: a forward return without the verdict that qualifies
    # it is the exact claim this board must not make.
    verdict: dict | None = None


#: The verdict fields a forward row carries, and what they say when no verdict
#: was loaded for it (#1764).
#:
#: A LABEL plus its provenance — deliberately NOT ``passes_rigor_gate``: a bare
#: boolean beside a forward return is the field a consumer would blend or sort
#: on, which is exactly what the two-board split exists to prevent, while a
#: dated four-state reads as the statement about the BACKTEST that it is. The
#: three values are byte-identical to ``passport_loader.UNGRADED_RIGOR_VERDICT``'s
#: — pinned by test rather than imported, because this module is deliberately
#: I/O-free and importing the passport loader would drag the ORM into the pure
#: layer.
UNGRADED_ENTRY_VERDICT: dict = {
    "rigor_gate_status": "pending",
    "graded_at": None,
    "gate_version": None,
}


def _verdict_or_ungraded(verdict: dict | None) -> dict:
    """One row's verdict fields — never fewer, and never a pass by default.

    Takes the ``passport_loader`` verdict dict (which also carries the derived
    ``passes_rigor_gate``) and keeps only what this board serves.
    """
    if not verdict:
        return dict(UNGRADED_ENTRY_VERDICT)
    return {key: verdict.get(key, UNGRADED_ENTRY_VERDICT[key]) for key in UNGRADED_ENTRY_VERDICT}


def _cumulative_return(returns: list[tuple[date, float]]) -> float:
    equity = 1.0
    for _, r in returns:
        equity *= 1.0 + r
    return equity - 1.0


def build_live_paper_leaderboard(
    ledgers: list[LivePaperLedger],
    *,
    scope: str = "own",
    limit: int = 50,
    degraded: bool = False,
    degraded_reason: str = "",
) -> LivePaperLeaderboardResponse:
    """Rank ACTIVE paper deployments by realised forward return. Pure — no I/O.

    The one hard rule, and the only interesting logic in here: **a deployment
    with an empty ledger is dropped, never rendered.** Not as a 0.0% row, not
    as an em-dash row, not as a "pending" row that still occupies a rank. A
    forward board that shows a strategy with no forward observations is making
    a claim about performance out of nothing, which is precisely the failure
    (#381's "invented math", the fixture-column leaderboard in
    architectural-principles.md § fail-soft) this surface was split out to
    stop. The drop is COUNTED into ``withheld_no_ledger`` so it reads as a
    visible absence rather than a silence.

    Ranking is by ``cumulative_return`` descending only. There is no
    configurable sort and no secondary score: this board has exactly one
    honest number.
    """
    graded = [led for led in ledgers if led.returns]
    withheld = len(ledgers) - len(graded)

    rows: list[LivePaperEntry] = []
    for led in graded:
        observations = sorted(led.returns, key=lambda item: item[0])
        rows.append(
            LivePaperEntry(
                rank=0,  # assigned after sort
                deployment_id=led.deployment_id,
                strategy_id=led.strategy_id,
                name=led.name,
                performance_basis=BASIS_LIVE_PAPER,
                cumulative_return=_cumulative_return(observations),
                days_live=len(observations),
                inception_date=led.inception.isoformat(),
                as_of=observations[-1][0].isoformat(),
                last_updated=led.last_appended_at.isoformat() if led.last_appended_at else None,
                drift_detected=led.drift_detected,
                # Unconditional, exactly like `cumulative_return` beside it. A
                # ledger that arrived with no verdict loaded falls back to the
                # ungraded four-state ("pending") — an explicit "no gate has
                # answered", never an omitted field the UI would have to guess
                # about and never a pass.
                **_verdict_or_ungraded(led.verdict),
            )
        )

    rows.sort(key=lambda e: e.cumulative_return, reverse=True)
    for i, entry in enumerate(rows, start=1):
        entry.rank = i

    total = len(rows)
    # as_of must describe the rows actually SERVED: with a truncating limit,
    # max() over the full set can postdate every visible row's ledger.
    board_as_of = max((e.as_of for e in rows[:limit]), default=None)
    return LivePaperLeaderboardResponse(
        entries=rows[:limit],
        total=total,
        performance_basis=BASIS_LIVE_PAPER,
        scope=scope,
        sort_by="cumulative_return",
        order="desc",
        as_of=board_as_of,
        withheld_no_ledger=withheld,
        methodology=LIVE_PAPER_METHODOLOGY,
        disclaimer=LIVE_PAPER_DISCLAIMER,
        degraded=degraded,
        degraded_reason=degraded_reason,
    )
