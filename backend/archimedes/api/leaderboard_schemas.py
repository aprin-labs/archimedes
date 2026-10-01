"""Leaderboard API schemas — /api/leaderboard.

The public, gamified strategy leaderboard (North Star §5 — the testnet
engagement engine). It ranks every library strategy by a **transparent**
conviction score built from *real* passport data (rigor gate + backtest), and
pairs that validation axis with a clearly-labelled **forward axis** (per-strategy
StockBench + live paper-P&L) that is honest about what is live now vs pending.

Design rule (the #1 rule — claims must be true): the leaderboard NEVER invents a
number. Every ranking input is a real passport field; the score weights are
explicit and echoed in the response; and the StockBench / live-P&L axis carries an explicit "pending" value per strategy until that data actually
flows — the UI omits the axis rather than rendering it (#1365). (A prior
marketplace surface was removed for "hardcoded fees + invented math", #381 — we
do not repeat that.)

TWO BOARDS, NEVER BLENDED (Lane 3.4). The conviction board above is entirely
BACKTEST-ERA: gate, DSR, OOS and PBO are all measured on history the strategy
was fitted and graded against. A separate surface — ``LivePaperLeaderboard
Response``, served from /api/leaderboard/live-paper — ranks only what is
actually running forward, computed from the append-only paper ledger
(``paper_daily_returns``). They are deliberately NOT combined into one score:
a blended number would let a strong backtest carry a strategy that has traded
forward for four days, which is the precise claim this product exists to
refuse. Instead every row on both boards carries ``performance_basis`` —
``"backtest_research"`` or ``"live_paper"`` — so a number can never be read
off either board without its provenance attached.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from archimedes.api.schemas import PaperRefResponse
from archimedes.services.rigor_evaluator import DEFAULT_BOARD_FDR_LEVEL

#: Provenance tags for a displayed number. These are the ONLY two bases the
#: product measures on, and every row on every board carries exactly one.
BASIS_BACKTEST = "backtest_research"
BASIS_LIVE_PAPER = "live_paper"


class LeaderboardScoreComponents(BaseModel):
    """The four real, [0,1]-normalised inputs to the conviction score. Surfaced
    per-entry so the gamified score is never a black box — the user sees exactly
    what drove it. None inputs (e.g. a placeholder strategy with no DSR) score 0
    and are reflected in ``data_completeness``."""

    gate: float = Field(..., description="1.0 if passes_rigor_gate else 0.0")
    dsr_confidence: float = Field(
        ...,
        description=(
            "DSR confidence in [0,1] — the probability the Sharpe survives "
            "deflation/multiple-testing. HIGHER IS BETTER. (Sourced from the "
            "`dsr_p_value` field, which despite its legacy name holds a 0–1 "
            "confidence, NOT a classical p-value where lower is better.)"
        ),
    )
    oos_performance: float = Field(..., description="out_of_sample_sharpe / OOS_TARGET, clamped [0,1]")
    overfitting_resistance: float = Field(..., description="1 - pbo_score, clamped [0,1]")
    data_completeness: float = Field(..., description="Fraction of the four inputs backed by real data [0,1]")


class LeaderboardForwardAxis(BaseModel):
    """The forward-looking axis paired with validation in the scoring engine.
    Per-strategy StockBench and live paper-P&L are not tracked yet, so these
    carry an explicit ``pending`` value until the engagement-engine wiring
    lands. The API surfaces them (never fabricates them) so the pairing with
    validation stays visible to consumers; the UI omits the axis until real
    data flows (#1365)."""

    stockbench_status: str = Field(
        "pending",
        description="'pending' until per-strategy StockBench eval exists; the global benchmark context lives in the engine metadata",
    )
    stockbench_sortino: float | None = None
    live_pnl_status: str = Field(
        "pending", description="'pending' until live paper-P&L tracking is wired (testnet — paper/simulated)"
    )
    live_pnl_pct: float | None = None


class BoardLevelFdr(BaseModel):
    """Board-level Benjamini-Hochberg FDR correction across this board's cohort
    (#1185, moved here from the per-strategy gate by #1564).

    **Why it lives on the leaderboard and nowhere else.** Disclosing board-level
    selection bias ("the best of N strategies is a stronger claim than one
    strategy graded on its own merits") is not the same as CORRECTING for it —
    this is the correction. It is inherently RELATIONAL: the same strategy's
    ``board_fdr_significant`` flips as *other, unrelated* strategies join or
    leave the cohort. Owner decision (Dan, 2026-08-31, #1564): the strategy
    passport carries only information about the strategy itself, and the
    leaderboard is the one cross-strategy surface — so a metric with that
    property may only ride this response. ``GET /api/selection-bias/gate*``
    carries no ``board_fdr``/``board_level_fdr`` key at all, and
    ``test_selection_bias_routes.TestBoardFdrStaysOffThePerStrategyGate``
    fails if one reappears.

    Distinct axis from ``num_trials`` (#1075's PER-STRATEGY, self-contained
    multiple-testing convention, unaffected by this): this corrects across the
    SIMULTANEOUS "true Sharpe > 0" claims made by every strategy on the board.

    ADVISORY — see ``rigor_evaluator.compute_board_level_fdr``'s docstring for
    the full scope rationale. It does NOT gate ``passes_rigor_gate`` /
    ``passes_all`` at any strictness level, and it is NOT an input to
    ``conviction_score``. An empty cohort yields ``n_tested=0,
    n_significant=0``, which is honest (nothing to correct) rather than
    fabricated.
    """

    # Sourced from DEFAULT_BOARD_FDR_LEVEL (rigor_evaluator) as a schema default
    # ONLY — the value any real response reports is passed explicitly at
    # construction (see services/leaderboard.build_leaderboard) so this field can
    # never silently diverge from the α the correction actually ran at.
    fdr_level: float = Field(
        DEFAULT_BOARD_FDR_LEVEL, description="Target board-level FDR (α) the correction was run at."
    )
    n_tested: int = Field(0, description="Cohort size m — strategies with a finite dsr_p_value to correct.")
    n_significant: int = Field(0, description="How many of those clear the board-level BH threshold at `fdr_level`.")
    # The cohort the correction ran over, stated so a reader can tell it apart
    # from the row count they can see. DELIBERATELY the whole board, BEFORE the
    # caller's regime/min_rigor filters and BEFORE `limit` — see
    # build_leaderboard's comment: making m depend on a view control would let a
    # reader shrink the cohort until a row went significant, which is the exact
    # p-hacking this correction exists to price in.
    cohort_basis: str = Field(
        "board_cohort_before_filters",
        description=(
            "Which set m was measured over. Always the full board cohort as assembled, before "
            "any regime/min_rigor filter and before `limit` — so a row's correction never moves "
            "because of what the viewer chose to look at."
        ),
    )
    methodology: str = Field(
        ...,
        description="Plain statement of exactly how the displayed correction was computed.",
    )


class LeaderboardEntry(BaseModel):
    rank: int
    medal: str | None = Field(None, description="'gold' | 'silver' | 'bronze' for the top 3, else null")
    id: str
    name: str = Field(..., description="Paper title, else methodology summary — human label for the strategy")
    creator: str = Field(..., description="curator_wallet, else 'Archimedes' for the curated seed library")

    # The gamified, transparent score (0–100) and its real components.
    conviction_score: float
    score_components: LeaderboardScoreComponents

    # Validation axis — real backtest metrics (None = not yet evaluated).
    sharpe_ratio: float | None = None
    cagr: float | None = None
    sortino_ratio: float | None = None
    max_drawdown: float | None = None
    calmar_ratio: float | None = None

    # Rigor (selection-bias gate) — the credibility moat, surfaced honestly.
    deflated_sharpe_ratio: float | None = None
    dsr_p_value: float | None = Field(
        None,
        description=(
            "DSR confidence in [0,1] — HIGHER IS BETTER. Despite the legacy "
            "`p_value` name this is a confidence (probability the Sharpe survives "
            "deflation), not a classical p-value where lower is better."
        ),
    )
    pbo_score: float | None = None
    out_of_sample_sharpe: float | None = None
    passes_rigor_gate: bool = False
    is_backtest_placeholder: bool = False
    # ── Board-level BH-FDR, per row (#1564) ────────────────────────────────
    # The cohort-relational half of this row's rigor story. It belongs HERE and
    # only here: on the leaderboard a row is, by construction, a member of a
    # cohort being compared, which is precisely the reading these numbers
    # support and precisely what the per-strategy passport must not carry.
    #
    # `None` means the correction did not run for this row, and there are
    # exactly two ways that happens, both honest absences: the row has no
    # finite `dsr_p_value` to correct (no backtest data / degenerate series),
    # or the board cohort was empty. `None` is NEVER "not significant" — the
    # UI renders an em-dash for it and never a verdict (guarded in
    # ui/test/leaderboard-board-fdr.test.js).
    board_fdr_significant: bool | None = Field(
        None,
        description=(
            "True if this row clears the board-level Benjamini-Hochberg threshold at the "
            "response's `board_level_fdr.fdr_level`. ADVISORY — never gates `passes_rigor_gate` "
            "and never feeds `conviction_score`. None = not corrected (no finite dsr_p_value), "
            "which is not the same as False."
        ),
    )
    board_fdr_adjusted_p: float | None = Field(
        None,
        description="BH-adjusted CLASSICAL p-value for this row (LOW = significant). None = not corrected.",
    )
    board_fdr_confidence: float | None = Field(
        None,
        description=(
            "1 − board_fdr_adjusted_p, so it reads the same direction as `dsr_p_value` "
            "(HIGH = confident) and the two can sit side by side. None = not corrected."
        ),
    )
    # Provenance of the numbers in this row. Three engines write
    # backtest_results and this one board ranks them together, so a reader
    # comparing two rows needs to know which engine produced each and on what
    # cost basis. Both columns existed on the store and reached
    # StrategyResponse, but stopped at `_entry` and never reached the board —
    # the one surface where rows are placed side by side.
    backtest_engine: str | None = None
    cost_model_id: str | None = None
    # See StrategyResponse.metrics_source: "live_gate" | "unavailable", with no
    # "persisted_backtest" value by construction (#1187).
    metrics_source: str = "unavailable"

    # Forward axis (paired, honest-pending).
    forward: LeaderboardForwardAxis

    # ── Provenance of the numbers in this row (Lane 3.4) ────────────────────
    # Every metric above — conviction score, Sharpe, CAGR, DSR, PBO, OOS — is
    # measured on the BACKTEST window named by backtest_start/backtest_end,
    # not on anything the strategy has done forward. The constant below is not
    # decoration: it is what lets a reader (or another surface) tell a row on
    # this board apart from a row on the live-paper board, which carries
    # ``live_paper`` and an inception date instead.
    performance_basis: str = Field(
        BASIS_BACKTEST,
        description="Always 'backtest_research' on this board — the numbers are backtest-era, not forward.",
    )
    # The window the metrics were computed over (ISO dates, threaded from the
    # passport's backtest_start/backtest_end). None where the source row
    # predates the field or was never evaluated — rendered as an explicit
    # "window unknown", never as a silently absent qualifier.
    backtest_start: str | None = None
    backtest_end: str | None = None

    # Provenance + context.
    regime_tag: str = "regime_neutral"
    return_source: str = "noise"
    status: str = "candidate"
    papers: list[PaperRefResponse] = []


class StockBenchGlobalContext(BaseModel):
    """The one real StockBench result we have: the *whole* agent pipeline run
    (Chen et al. 2026), not per-strategy. Surfaced as honest context so the
    forward axis means something today without faking per-strategy numbers."""

    scope: str = "agent_pipeline_global"
    sortino: float
    return_pct: float
    max_drawdown_pct: float
    rank: str
    window: str
    source: str


class LeaderboardScoringEngine(BaseModel):
    """Transparent metadata: the weights, the methodology, what's live vs pending,
    and the StockBench global context. Rendered alongside the board so the score
    is explainable and the testnet/paper framing is loud."""

    weights: dict[str, float]
    oos_target: float
    methodology: str
    validation_axis: str = "live"
    forward_axis: str = "pending"
    stockbench_global: StockBenchGlobalContext
    disclaimer: str


class LeaderboardResponse(BaseModel):
    entries: list[LeaderboardEntry]
    total: int
    # Board-level restatement of every row's basis, so a consumer can label the
    # whole surface without inspecting rows (and so an empty board still says
    # what it would have been showing).
    performance_basis: str = Field(
        BASIS_BACKTEST,
        description="'backtest_research' — this board ranks backtest-era metrics only. Never blended with live paper.",
    )
    sort_by: str
    order: str
    scope: str = Field(
        "curated",
        description=(
            "The cohort actually served: 'own' (the signed-in caller's own "
            "strategies, ranked against each other) or 'curated' (the curated "
            "seed library, shown as reference — never a competing cohort). May "
            "differ from a requested ?scope= — an anonymous caller requesting "
            "'own' is served 'curated' instead, and this field reports what was "
            "actually served, not what was asked for."
        ),
    )
    scoring_engine: LeaderboardScoringEngine
    # Board-level BH-FDR correction over this board's cohort (#1185, relocated
    # here by #1564). Always present — an empty board reports n_tested=0
    # rather than omitting the block, so a consumer never has to guess whether
    # "no correction shown" means "not computed" or "nothing to correct".
    board_level_fdr: BoardLevelFdr
    # Honest degradation signal (#1356): True when the underlying strategy
    # provider raised, or the curated cohort came back empty for a reason
    # other than a legitimate filter (e.g. the corpus is missing from the
    # build). `degraded_reason` names which. The UI must never render "No
    # strategies match these filters yet." while this is True — that is a
    # different, false claim.
    degraded: bool = False
    degraded_reason: str = ""


# ── Live paper board — the forward surface (Lane 3.4) ────────────────────────


class LivePaperEntry(BaseModel):
    """One ACTIVE paper deployment with a non-empty forward ledger.

    Hard construction rule, enforced in ``build_live_paper_leaderboard`` and
    pinned by test: a deployment with zero ``paper_daily_returns`` rows NEVER
    becomes an entry. There is no zero-filled row, no "0.0% since inception",
    no placeholder — a deployment that has not produced a single observation
    has no forward performance to display, and rendering one would fabricate
    the exact thing this board exists to prove. Withheld deployments are
    counted on the response (``withheld_no_ledger``) so the omission is a
    loud absence rather than a silence.
    """

    rank: int
    deployment_id: str
    strategy_id: str
    name: str = Field(..., description="Human label — the strategy's name, else the deployed spec's name, else its id")

    performance_basis: str = Field(
        BASIS_LIVE_PAPER,
        description="Always 'live_paper' — every number in this row is compounded from the append-only forward ledger.",
    )
    # Compounded product of the ledger's daily returns, minus 1. Never
    # annualised, never extrapolated: over a handful of days an annualised
    # figure is a fiction, and this board's whole point is that four days of
    # forward data must look like four days.
    cumulative_return: float = Field(..., description="∏(1 + daily_return) − 1 over the whole ledger, since inception")
    days_live: int = Field(..., ge=1, description="Count of ledger observations — always ≥ 1 by construction")
    inception_date: str = Field(..., description="ISO date the deployment opened (paper_deployments.deployed_at)")
    as_of: str = Field(..., description="ISO date of the LAST ledger observation — what the return actually reflects")
    last_updated: str | None = Field(
        None, description="ISO timestamp the last ledger row was appended (paper_daily_returns.appended_at)"
    )
    # The ledger is append-only by law; a replay that disagrees with already
    # written rows stamps the deployment instead of rewriting it. Surfaced so
    # a drifted track record is visibly drifted on the board too.
    #
    # True for EITHER cause (#1449): an upstream data restatement
    # (`drift_detected_at`) or a grading-engine re-grade (`engine_regrade_at`).
    # The board does not yet distinguish them — GET /api/paper/deployments does,
    # per deployment. Widening this to a cause is follow-up work; what it must
    # not do is go false while a disagreement stands.
    drift_detected: bool = False

    # ── The verdict of record, beside the forward record (#1764) ────────────
    #
    # This board's every row is a paper deployment's realised return, and
    # deploy is AT WILL: a strategy whose rigor gate said `fail`, `pending` or
    # `degenerate` can be paper-traded exactly like one that passed. That
    # freedom is only honest if the verdict travels with the number — a
    # "+12.4% since inception" row for a gate-REJECTED strategy, with nothing
    # on the row saying so, reads as an endorsement of the strategy instead of
    # as evidence about the gate.
    #
    # This is NOT the blended board the split forbids, and the shape is what
    # keeps the two apart:
    #
    #   * it is a LABEL with its provenance (a four-state word plus the date
    #     and the gate version that produced it), never a backtest performance
    #     number — no DSR, no conviction score, no Sharpe reaches this row;
    #   * `passes_rigor_gate` is deliberately ABSENT. A bare boolean beside a
    #     forward return is the field a consumer would blend or sort on; the
    #     dated four-state cannot be mistaken for a property of the forward
    #     record. `sort_by` stays `cumulative_return`, the only forward number
    #     this board has;
    #   * it is a pure READ of `strategy_passports`
    #     (docs/adr/rigor-verdict-of-record.md), served exactly as
    #     `GET /api/paper/deployments` serves it and derived by the same
    #     `passport_loader` code — never a recompute, so this board and the
    #     deployment card can never show two verdicts for one strategy.
    #
    # A row whose strategy has no passport row, or whose passport read failed,
    # carries "pending" — never a fabricated pass.
    rigor_gate_status: str = Field(
        "pending",
        description=(
            "The STORED four-state verdict of record for this row's strategy: pass | fail | pending | degenerate. "
            "'pending' also covers 'never graded'. A backtest-era LABEL with its provenance beside it, not a "
            "backtest metric: it is never ranked on, never blended, and never recomputed here."
        ),
    )
    graded_at: str | None = Field(
        None,
        description=(
            "ISO timestamp the verdict was recorded — the provenance that keeps it readable as a statement about "
            "the BACKTEST rather than about the forward ledger. Null when the strategy was never graded."
        ),
    )
    gate_version: str | None = Field(
        None,
        description=(
            "Digest of the gate that produced the verdict. The literal 'legacy-derived' means the verdict was "
            "INFERRED by the verdict-of-record migration rather than produced by a gate run."
        ),
    )


class LivePaperLeaderboardResponse(BaseModel):
    """The forward board. Deliberately carries NO conviction score and no
    backtest metric: the two bases never share a row, never share a sort, and
    are never averaged into one number.

    The one backtest-era thing a row does carry is the VERDICT OF RECORD
    (#1764) — a four-state label with the date and gate version that produced
    it — because deploy is at will and a forward return for a gate-rejected
    strategy, shown bare, reads as an endorsement. It is a labelled statement,
    not a number: nothing ranks or sorts on it (``sort_by`` is fixed), and no
    backtest METRIC (DSR, PBO, Sharpe, conviction) reaches this response at
    all. See ``LivePaperEntry``."""

    entries: list[LivePaperEntry]
    total: int = Field(..., description="Qualifying rows BEFORE `limit` — deployments with ledger data, nothing else")
    performance_basis: str = Field(
        BASIS_LIVE_PAPER, description="'live_paper' — forward, paper/simulated, compounded from the ledger"
    )
    scope: str = Field(
        "own",
        description=(
            "'own' — the signed-in caller's own paper deployments (a paper "
            "track record is private, #850). 'anonymous' — no session, so "
            "there is no cohort to show; entries is empty and that is an "
            "honest empty state, not a degradation."
        ),
    )
    sort_by: str = Field("cumulative_return", description="Fixed — the only forward number this board has")
    order: str = "desc"
    #: Latest ledger date across all entries; None on an empty board.
    as_of: str | None = None
    #: Active deployments dropped for having no ledger rows yet. Counted, not
    #: hidden — see LivePaperEntry's docstring.
    withheld_no_ledger: int = 0
    methodology: str = Field(
        ...,
        description="One-line, plain statement of exactly how the displayed number was computed.",
    )
    disclaimer: str
    degraded: bool = False
    degraded_reason: str = ""
