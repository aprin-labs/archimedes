"""REST API response schemas — Daniel's frontend depends on these.

These are Pydantic models that define the JSON shape of every API response.
Chuan implements the FastAPI endpoints; Daniel codes the frontend against
these schemas. Changes here require a heads-up to Daniel.

Convention:
  - All monetary values in USDC are floats (display-friendly)
  - All on-chain addresses are checksummed hex strings
  - All timestamps are ISO 8601 strings
  - Pagination uses limit/offset
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, model_validator

# ═══════════════════════════════════════════════════════════════
# Assets
# ═══════════════════════════════════════════════════════════════


class AssetResponse(BaseModel):
    """Single asset in the ecosystem."""

    address: str
    symbol: str
    name: str
    asset_type: str  # "synthetic" | "bridged" | "native" | "vault_token"
    decimals: int
    price_usd: float
    price_change_24h: float = 0.0  # Percentage change
    oracle_address: str | None = None


class AssetListResponse(BaseModel):
    assets: list[AssetResponse]


class AssetPriceHistoryResponse(BaseModel):
    """Historical prices for charting."""

    symbol: str
    prices: list[PricePoint]
    interval: str  # "1h" | "1d" | "1w"


class PricePoint(BaseModel):
    timestamp: str  # ISO 8601
    price: float


# ═══════════════════════════════════════════════════════════════
# Vaults
# ═══════════════════════════════════════════════════════════════


class VaultHolding(BaseModel):
    """A single position in a vault."""

    symbol: str
    token_address: str
    amount: float
    value_usdc: float
    weight_pct: float  # e.g. 30.5 = 30.5%


class VaultSummaryResponse(BaseModel):
    """Vault card for the leaderboard/list view."""

    address: str
    name: str
    symbol: str
    tier: int  # 1 or 2
    creator: str  # Wallet address
    aum_usdc: float
    share_price: float
    # Nullable (#1103): a value here MUST come from a real oracle-price
    # baseline comparison, never a synthesized placeholder. `returns_source`
    # is the honest provenance marker for these fields — mirrors
    # RigorGateVerdict.source ("live_gate" | "pending") in live_rigor_gate.py.
    return_24h: float | None
    return_7d: float | None
    return_30d: float | None
    return_inception: float | None
    returns_source: Literal["oracle_baseline", "unavailable"]
    sharpe_ratio: float | None = None
    management_fee_pct: float  # e.g. 1.5
    performance_fee_pct: float  # e.g. 20.0
    is_agent_assisted: bool
    depositors: int
    last_rebalance: str | None = None  # ISO 8601
    created_at: str


class VaultDetailResponse(BaseModel):
    """Full vault detail page data."""

    # Core info (same as summary)
    address: str
    name: str
    symbol: str
    tier: int
    creator: str
    aum_usdc: float
    share_price: float
    is_agent_assisted: bool

    # Fees
    management_fee_pct: float
    performance_fee_pct: float
    high_water_mark: float

    # Holdings
    holdings: list[VaultHolding]
    target_allocations: list[VaultHolding]  # Target weights

    # Performance — nullable (#1103): see VaultSummaryResponse.returns_source.
    return_24h: float | None
    return_7d: float | None
    return_30d: float | None
    return_inception: float | None
    returns_source: Literal["oracle_baseline", "unavailable"]
    sharpe_ratio: float | None = None
    max_drawdown: float | None = None

    # Equity curve for charting
    equity_curve: list[PricePoint] = []

    # Strategy info (Tier 1 only)
    strategy_ids: list[str] = []
    current_regime: str | None = None  # "risk_on" | "risk_off" | etc.

    # Recent reasoning traces
    recent_traces: list[TraceResponse] = []

    # Metadata
    depositors: int = 0
    last_rebalance: str | None = None
    created_at: str = ""


class VaultListResponse(BaseModel):
    vaults: list[VaultSummaryResponse]
    total: int


# ═══════════════════════════════════════════════════════════════
# Strategies
# ═══════════════════════════════════════════════════════════════


class PaperRefResponse(BaseModel):
    """A single paper reference in a strategy passport.

    The wire projection of one ``assoc/v1`` association (#1637) — see
    ``models/paper_assoc.py``. Every enrichment field is nullable and stays
    null when unknown: authors, venue, year and DOI are structurally NULL for
    generated strategies today, and ``null`` is the honest rendering of that.
    """

    arxiv_id: str | None = None
    #: ``None``, not ``""``, when no title resolves — the renderer prints
    #: "title unavailable — arXiv:<id>" rather than an empty pair of quotes.
    title: str | None = None
    authors: list[str] = []
    doi: str | None = None
    venue: str | None = None
    year: int | None = None
    citation_count: int | None = None
    contribution: str | None = None
    #: "cited" | "considered". ``papers[]`` carries only cited associations
    #: today; the field is here so a consumer never has to assume.
    role: str = "cited"
    selection_rank: int | None = None
    #: Reranker score at selection time. ``None`` whenever the rerank was
    #: keyword-only or disabled — which is the common case. Never 0.0.
    semantic_score: float | None = None
    #: Corpus content hash. NULL in production until #1091 hydrates it.
    content_hash: str | None = None


class StrategyResponse(BaseModel):
    """Strategy detail for the strategy explorer."""

    id: str
    papers: list[PaperRefResponse] = []
    methodology_summary: str
    # The user's own free-text ask that produced this strategy (v8 Lane 3.3),
    # read from strategy_store.brief_intent — distinct from
    # methodology_summary, which is the DERIVED writeup. Populated ONLY by
    # the single-strategy detail route (``get_strategy``), and there only for
    # the row's OWNER — publishing a strategy shares the strategy, not the
    # sentence its owner typed to ask for it. The shared
    # ``_passport_to_strategy_response``/``_passport_responses`` helpers that
    # also back Library and the public leaderboard never set it, so it never
    # reaches those list payloads. None = not the caller's row, a curated
    # strategy (no brief), or a legacy generated row the backfill migration
    # could not resolve.
    brief_intent: str | None = None
    # The validated machine-readable DSL spec that RUNS this strategy — the
    # same dict a fusion proposal emits, stored on ``StrategyPassport.
    # strategy_spec`` / ``strategy_store.strategy_spec`` (#1646). The passport
    # page renders it so a reader can see the executable rules behind the
    # prose, instead of taking ``methodology_summary`` on faith.
    #
    # REASONING, not card content — and that classification is not a judgement
    # call made here, it is quoted from the #1557 matrix in
    # ``services/strategy_visibility.py``, which names "machine-readable DSL
    # spec" in the REASONING column beside the debate transcript and the raw
    # return series. So the gate is ``is_strategy_reasoning_visible``: public
    # for ``is_example`` house rows, OWNER-ONLY for a user's row **including a
    # published one**. Publishing consents to sharing the result, not the
    # executable derivation — the identical rule ``GET /{id}/returns`` and
    # ``GET /{id}/debate`` already enforce by 404ing, and the same reason
    # ``POST /api/paper/deployments``'s ``_spec_for_strategy`` fails closed
    # ("the thing a marketplace would license, not give away").
    #
    # Populated ONLY by the single-strategy detail route (``get_strategy``),
    # exactly like ``brief_intent`` above and for a second, independent
    # reason: the shared ``_passport_to_strategy_response`` /
    # ``_passport_responses`` builders back Library and the public
    # leaderboard, and a 100-row list payload (already heavy with full-library
    # grading, #1173) must not carry 100 arbitrary-size JSON blobs. Both
    # reasons point the same way, so this field is set at the route, never in
    # a shared helper. ``None`` = not the caller's to read, a row with no spec
    # (curated code-path strategies carry a ``strategy_code_path`` instead),
    # or a row persisted before the column existed.
    strategy_spec: dict[str, Any] | None = None
    asset_universe: list[str]
    # Provenance of the asset_universe pick (#857): "user" | "model" | "full",
    # or None for rows written before this field existed (curated strategies,
    # pre-#857 fusion/architect rows). A model-picked universe is a mild
    # look-ahead channel (the model can pick names it already "knows" did well
    # over the window from training data) — surfaced here for audit, not gated.
    universe_source: str | None = None
    position_sizing: str
    rebalance_frequency: str
    status: str  # "candidate" | "validated" | "live" | "retired" | "rejected"

    # Legacy scalar fields (populated from papers[0] for backwards compat)
    paper_arxiv_id: str | None = None
    paper_title: str | None = None
    paper_authors: list[str] = []
    paper_venue: str | None = None
    paper_year: int | None = None
    paper_doi: str | None = None
    paper_citation_count: int | None = None

    # Passport integrity
    methodology_hash: str | None = None
    extraction_llm: str | None = None
    curator_wallet: str | None = None
    curator_note: str | None = None
    on_chain_registration_tx: str | None = None

    # Backtest results (if evaluated; None = not yet run)
    sharpe_ratio: float | None = None
    sortino_ratio: float | None = None
    max_drawdown: float | None = None
    cagr: float | None = None
    win_rate: float | None = None
    total_trades: int | None = None
    calmar_ratio: float | None = None
    correlation_to_spy: float | None = None
    deflated_sharpe_ratio: float | None = None
    dsr_p_value: float | None = None
    pbo_score: float | None = None
    out_of_sample_sharpe: float | None = None
    kelly_fraction: float | None = None
    passes_rigor_gate: bool = False
    # Four-state live rigor-gate badge (#821, #1184): "pass" | "fail" | "pending" |
    # "degenerate". Sourced from the LIVE run_rigor_gate verdict on persisted real
    # returns — never from a stored fixture boolean. "pending" = no real backtest
    # data yet (the gate cannot run); "degenerate" = real data exists but is a
    # mathematically constant (zero-variance) series — broken data or a zero-trade
    # backtest, distinct from both "pending" (not evaluated yet) and "fail"
    # (evaluated, statistically weak). Surfaced honestly instead of defaulting to a
    # fixture True/False. ``passes_rigor_gate`` stays the fail-closed boolean (True
    # only when status == "pass").
    rigor_gate_status: str = "pending"
    # ── Metric provenance (A3 / #1187) ──────────────────────────────────────
    # Which source produced the RIGOR numbers above (deflated_sharpe_ratio,
    # dsr_p_value, pbo_score, out_of_sample_sharpe):
    #   "stored_grade" — the numbers the STORED grade produced, read off
    #                    strategy_passports beside the verdict that same gate run
    #                    produced (docs/adr/rigor-verdict-of-record.md)
    #   "live_gate"    — a live run_rigor_gate call on persisted real returns.
    #                    No longer reachable from the curated read path (#1746 /
    #                    PR-B moved that grade to the write side); still the
    #                    honest label for a surface that genuinely recomputes,
    #                    which the deploy ladder at
    #                    GET /api/selection-bias/gate/{id} deliberately does.
    #   "unavailable"  — no grade to read; every rigor field is None
    #
    # There is deliberately no "persisted_backtest" value. #1187/#1340 removed
    # the `s.<field> ?? bt.<field>` fallback that served fixture constants
    # beside live numbers, so a persisted rigor column can no longer reach a
    # response at all. The value's ABSENCE from this enum is the assertion that
    # the fallback is gone — if it ever reappears, something has to add it back
    # here and that shows up in a diff. "stored_grade" is not that fallback
    # returning: those columns are now written by, and only by, a gate run
    # (passport_loader._apply_rigor_verdict), and a row with no `graded_at`
    # serves None rather than whatever a fixture sync left behind.
    metrics_source: str = "unavailable"
    # Which source produced the DISPLAY metrics (sharpe_ratio, cagr, win_rate,
    # max_drawdown, calmar_ratio, sortino_ratio, correlation_to_spy,
    # total_trades). These are descriptive backtest stats rather than a
    # gate pass/fail claim, so unlike the rigor fields they still fall through a
    # chain — and the last link is a STUB. Naming the link is what stops a
    # placeholder reading as a measurement:
    #   "strategy_record"    — the passport's own stored metrics (s.real_*)
    #   "persisted_backtest" — the BacktestResultRecord row
    #   "stub_placeholder"   — a hardcoded placeholder, measured nothing
    #   "unavailable"        — no source at all; fields are None
    # Deliberately NOT called "measured": s.real_* is stored on the strategy
    # record and for the curated library traces to the #1187 fixture snapshot,
    # so calling it measured would make exactly the claim this field exists to
    # avoid.
    display_metrics_source: str = "unavailable"
    paper_claimed_sharpe: float | None = None
    paper_claim_blended_sharpe: float | None = None
    is_backtest_placeholder: bool = False
    sharpe_ci_lower: float | None = None
    sharpe_ci_upper: float | None = None

    # Selection-set size this verdict was graded at, plus its provenance (#1358).
    # ``None``/``"unspecified"`` for a strategy the live gate has not graded yet
    # — either no/insufficient persisted returns, OR a batch/DB-read failure
    # (both collapse to the same "no number to report" shape; a reader must not
    # infer which one from this field alone) — never a silently-assumed 1.
    # Once graded: "curated_self_contained" (a
    # hand-implemented paper, graded on its own Sharpe, N=1 by design — decouple
    # #2) | "generated_search_pool" (the generation pipeline's own tracked
    # N-candidate search, N>1 possible) | "generated_untracked_default"
    # (DB-persisted but the writing pipeline never proved it tracks its own
    # search size, so forced to N=1 and said so explicitly). Mirrors
    # ``selection_bias_routes.py``'s ``StrategyRigorResult.num_trials_scope`` —
    # same discriminator, same labels, so this can never disagree with what
    # ``GET /api/selection-bias/gate/{id}`` reports for the same strategy.
    num_trials_in_selection: int | None = None
    num_trials_scope: str = "unspecified"

    # Backtest period (ISO date strings; what window the metrics were computed over)
    backtest_start: str | None = None
    backtest_end: str | None = None

    # ── Engine attribution ──────────────────────────────────────────────────
    # Three engines write backtest_results and one gate ranks them together, so
    # a reader needs to know which produced a row and on what cost basis before
    # comparing two of them. Both columns already existed on the store;
    # neither reached any API schema, so the information stopped at the DB.
    # None on rows written before each was introduced.
    backtest_engine: str | None = None
    cost_model_id: str | None = None
    # Where look_ahead_audit_passed came from: "broker_config_only" (an
    # execution-timing check that never fails) | "ast_audit" |
    # "dsl_structural_audit" (the DSL path's derived verdict: the spec was
    # checked against the audited interpreter surface and the audit concluded) |
    # "dsl_audit_not_run" (DSL path, the audit reached no verdict — the boolean
    # beside it is False because nothing was checked, not because a check
    # failed) | "self_attested" (RETIRED — the LLM's own removed
    # look_ahead_safe declaration; historical rows only, never an audit result).
    # Without it a constant True reads as a passed audit.
    look_ahead_audit_source: str | None = None

    # Equity curve for charting
    equity_curve: list[PricePoint] = []

    # Regime suitability
    regime_tag: str = "regime_neutral"  # "bull" | "bear" | "regime_neutral"

    # Return-source classification (T2.5) — the dominant economic source of the
    # strategy's return, plus a one-sentence durability note. Computed by the
    # deterministic heuristic in services/return_source_classifier.py.
    return_source: str = "noise"  # "risk_premium" | "mispricing" | "productive_growth" | "noise"
    return_source_note: str = ""

    # ── Generation cost (#1326) ─────────────────────────────────────────────
    # What the generation run that produced this strategy actually consumed,
    # read from the durable ``generation_costs`` row rather than the Redis job
    # record (which expires after an hour). Shape:
    #   {"schema": "cost_v1", "job_id", "recorded_at",
    #    "measurement": {…the cost_v1 snapshot…},
    #    "quote": {…the literal generation_payment.quote() payload…} | null}
    # RAW MEASUREMENT beside a RECORDED PRICE, never a conversion of one into
    # the other: no server-side $-conversion of token counts (#1217's remaining
    # pricing work). ``None`` = nothing measured this strategy — every curated
    # strategy, and every generated one from before the meter — which the UI
    # renders as "not measured", never as zero.
    generation_cost: dict[str, Any] | None = None

    # Whether the caller's wallet is permitted to publish this strategy.
    # Always False for anonymous requests; True only when the caller is the
    # generating wallet (generated strategies) or a platform admin (examples).
    # Backend-authoritative — the frontend uses this to hide the publish affordance.
    can_publish: bool = False


class StrategyListResponse(BaseModel):
    strategies: list[StrategyResponse]
    total: int
    # Honest degradation signal (#1356): True when the strategy provider
    # raised or the library came back empty for a reason other than a
    # legitimate filter (e.g. the corpus is missing from the build).
    # `degraded_reason` names which, so the UI can show a loud, specific
    # unavailable state instead of rendering the false claim "no strategies".
    degraded: bool = False
    degraded_reason: str = ""


# ═══════════════════════════════════════════════════════════════
# Reasoning Traces
# ═══════════════════════════════════════════════════════════════


class TraceResponse(BaseModel):
    """A single reasoning trace for display."""

    id: str
    vault_address: str
    decision_type: str  # "construction" | "rebalance" | "rotation" | "regime_change" | "skip"
    trigger: str
    timestamp: str  # ISO 8601
    reasoning: str  # Human-readable explanation
    confidence: float

    # On-chain verification
    trace_hash: str
    arc_tx_hash: str | None = None
    is_verified: bool = False  # Has on-chain hash been confirmed?

    # How that confirmation was reached — same vocabulary as
    # TraceVerifyResponse.verification_mode (#1359), so the display routes and
    # the verify route cannot invent two different words for the same state.
    #
    # None is the honest default: this response makes NO verification claim.
    # The off-chain path replays whatever `is_verified` was stored with the
    # trace and compares nothing itself, so it has no mode to report — call
    # /api/traces/{id}/verify for one.
    #
    # "anchored_only" is what the on-chain-only path reports (#1407): an anchor
    # exists in the registry and ZERO hashes were compared against it. It must
    # not render with the same affordance as a hash match.
    verification_mode: Literal["hash_matched", "anchored_only", "failed"] | None = None

    # Context
    regime_at_decision: str | None = None
    trades_executed: list[TradeExecutedResponse] = []
    strategies_referenced: list[str] = []

    # Commit-reveal temporal binding (v1.5)
    commit_tx_hash: str | None = None
    commit_block_number: int | None = None
    reveal_tx_hash: str | None = None
    reveal_block_number: int | None = None
    trade_tx_hash: str | None = None
    trade_block_number: int | None = None
    temporal_binding_valid: bool | None = None
    # Provenance of the temporal-binding claim (#714 / T0.3). "chain" only when the
    # real commit-reveal path ran (an on-chain trace_id was minted); "none" otherwise
    # (legacy publishTrace anchor, dry-run, or no commit recorded). The UI "Temporal
    # Binding ✓ VERIFIED" badge must key off this, not a bare boolean from Redis.
    temporal_binding_source: Literal["chain", "none"] = "none"

    @model_validator(mode="after")
    def _temporal_binding_requires_chain_source(self) -> TraceResponse:
        """Claim-integrity guard (#714): temporal_binding_valid may only assert a
        verified binding when it is backed by on-chain commit-reveal receipts.

        Closes the audit finding (AUDIT_2026-06-14 #3) where the badge could read True
        off a Redis boolean: regardless of what the persisted dict holds, a non-"chain"
        source can never surface a True binding — it is coerced to None ("not applicable").
        ``is_verified`` is deliberately left untouched: a publishTrace anchor is a genuine
        on-chain hash confirmation, so reporting it verified is honest — only the stronger
        *temporal* binding claim is gated here.
        """
        if self.temporal_binding_source != "chain" and self.temporal_binding_valid:
            self.temporal_binding_valid = None
        return self


class TradeExecutedResponse(BaseModel):
    symbol: str
    direction: str  # "buy" | "sell"
    amount: float = 0.0
    value_usdc: float = 0.0


class TraceDetailResponse(TraceResponse):
    """A single trace with the rest of the body the hash was computed over.

    Everything in :class:`TraceResponse` is what a *list row* needs. This adds
    the fields a reader needs to actually audit one decision, and they are not
    decoration: ``market_context``, ``portfolio_before``, ``portfolio_after``
    and ``consulted_paper_hashes`` are four of the thirteen ``_HASH_FIELDS``
    (``models/trace.py``) that go into the anchored keccak256. Without them the
    only way to see what was committed was ``GET /api/traces/{id}/canonical``,
    a raw-JSON developer surface — so the anchored claim was, in practice,
    unreadable by the person whose money the decision moved.

    Defaults are empty rather than ``None``: a trace persisted before a field
    existed, or one projected from the on-chain registry alone (which carries
    no body at all), genuinely has nothing here. An empty dict renders as an
    honest absence; the caller must not read it as "the agent considered
    nothing". ``verification_mode`` already carries whether a body existed.

    ``settlement_tx_hashes`` and ``ipfs_cid`` are deliberately OUTSIDE the
    hashed set — they are only knowable after the trade, and the committed
    bytes are immutable (#903). They are surfaced here as provenance, never as
    part of the hash preimage.

    ``ipfs_cid`` is the registry ``storagePointer`` if one exists. The field
    name is leftover from an unused pin design. Live reveals write an empty
    pointer (#1526); a non-empty value is historical or copied from chain.
    """

    market_context: dict = {}
    portfolio_before: dict = {}
    portfolio_after: dict = {}
    consulted_paper_hashes: list[str] = []
    settlement_tx_hashes: list[str] = []
    ipfs_cid: str | None = None


class TraceListResponse(BaseModel):
    traces: list[TraceResponse]
    total: int


class TracePublishRequest(BaseModel):
    """Request to publish a reasoning trace on-chain."""

    vault_address: str
    decision_type: str = "construction"  # construction | rebalance | rotation | regime_change | skip
    trigger: str = "manual"
    reasoning: str = ""
    confidence: float = 0.0
    market_context: dict = {}
    portfolio_before: dict = {}
    portfolio_after: dict = {}
    trades_executed: list[dict] = []
    strategies_referenced: list[str] = []


class TracePublishResponse(BaseModel):
    """Response after publishing a trace on-chain."""

    id: str  # UUID
    trace_hash: str  # keccak256 hex
    arc_tx_hash: str | None = None
    is_anchored: bool = False
    timestamp: str  # ISO 8601
    vault_address: str
    decision_type: str


class TraceVerifyResponse(BaseModel):
    """Verification result for a single trace."""

    trace_id: int  # On-chain trace ID
    trace_hash: str
    is_verified: bool
    # Tri-state verification outcome (#1359). No default — every code path
    # that returns a TraceVerifyResponse must say which of these actually
    # happened, so a future branch can't silently omit it and fall back to
    # a value that reads as a pass:
    #   hash_matched  — off-chain trace_hash re-fetched from the on-chain
    #                   receipt and compared byte-for-byte; they matched.
    #   anchored_only — no off-chain record to compare against (the store
    #                   was reachable and simply had no entry), so only the
    #                   on-chain anchor itself was confirmed. Zero hashes
    #                   were compared — this is NOT a hash match and must
    #                   not render with the same affordance as one.
    #   failed        — off-chain record exists but was never anchored, the
    #                   on-chain receipt is missing, or the hashes disagree.
    # A Redis outage is deliberately NOT one of these states: it raises a
    # 503 (see verify_trace) rather than falling through to anchored_only,
    # because "the store is unreachable" and "the store is empty" are
    # different facts and only the second one is a real anchored_only.
    verification_mode: Literal["hash_matched", "anchored_only", "failed"]
    agent: str = ""
    vault: str = ""
    on_chain_timestamp: int = 0
    details: str  # Human-readable result
    # ── Source-paper verification (#1637) ────────────────────────────────
    # ``verify_source_papers`` had ZERO production callers: /verify re-hashed
    # the trace body and never checked that the papers it claims to have
    # consulted exist. The "trace-verify button" could not verify the half of
    # the trace that carries the research provenance.
    #
    # Tri-state for the same reason ``verification_mode`` is: None means NOT
    # CHECKED, and the two ways that happens are named in
    # ``source_paper_verification.mode`` — the trace claimed no papers, or the
    # corpus was unreachable. Neither is a pass and neither is a failure, and
    # collapsing either into ``False`` would report a fabricated provenance
    # failure while collapsing it into ``True`` would report a fabricated pass.
    #
    # And what a ``True`` here means is EXISTENCE, not a hash comparison:
    # corpus ``content_hash``/``pdf_sha256`` are NULL until #1091, so a claimed
    # suffix is empty and the check is "the corpus has this paper". Every
    # surface that renders this must say so (owner decision Q8 on #1688) —
    # ``ui/src/trace-binding.js:sourcePapersCopy`` is the one place that copy
    # lives.
    papers_verified: bool | None = None
    #: ``{"mode", "checked", "verified", "missing", "hash_mismatch"}`` — None
    #: when nothing was attempted (the anchored-only branch has no off-chain
    #: body to read a cited set out of).
    source_paper_verification: dict[str, Any] | None = None
    # Temporal binding verification
    temporal_binding_valid: bool | None = None
    commit_block_number: int | None = None
    trade_block_number: int | None = None
    reveal_block_number: int | None = None


# ═══════════════════════════════════════════════════════════════
# Regime
# ═══════════════════════════════════════════════════════════════


class RegimeResponse(BaseModel):
    """Current market regime for display."""

    regime: str  # "risk_on" | "risk_off" | "transition" | "crisis"
    confidence: float
    timestamp: str
    previous_regime: str | None = None
    regime_changed: bool = False
    signals: RegimeSignalsResponse
    transition_probabilities: dict | None = None  # From get_transition_probabilities()
    transitions_source: str = "default_prior"  # "redis_measured" | "default_prior"
    regime_history: dict | None = None  # From get_regime_history_summary()
    recommended_strategies: list[str] | None = None  # Strategy IDs best for this regime
    # Paper titles for each recommended_strategies id, in matching order.
    # Surfaced so the UI can show "Volatility-Managed Portfolios" instead of
    # a raw strategy hash (red-team report 2026-05-24 H3).
    recommended_strategy_titles: list[str] | None = None


class RegimeSignalsResponse(BaseModel):
    # vix_level is nullable: the VIX feed can be unavailable (no data) and we
    # MUST NOT render that as 0.0 — VIX is a price-of-insurance index that
    # floors around 10, so 0 is dishonest. None means "agent feed not
    # connected" (red-team report 2026-05-24 H2).
    vix_level: float | None = None
    sp500_above_ma50: bool
    sp500_above_ma200: bool
    vix_rate_of_change: float | None = None  # VIX momentum
    vix_score: float | None = None  # 0-1 danger score from VIX level
    ma_score: float | None = None  # 0-1 from MA positioning
    composite_score: float | None = None  # Final 0-1 composite
    credit_spread_ig: float | None = None
    credit_spread_hy: float | None = None
    btc_dominance: float | None = None


# ═══════════════════════════════════════════════════════════════
# Swap (AMM preview)
# ═══════════════════════════════════════════════════════════════


class SwapQuoteResponse(BaseModel):
    """Preview a swap before user signs the transaction."""

    token_in: str  # Address
    token_out: str
    amount_in: float
    amount_out: float
    price_impact_pct: float  # e.g. 0.5 = 0.5%
    fee_pct: float  # e.g. 0.3 = 0.3%
    min_amount_out: float  # After slippage tolerance


class PoolResponse(BaseModel):
    """AMM pool summary for the exchange UI."""

    address: str
    token0: str
    token1: str
    symbol0: str
    symbol1: str
    reserve0: float
    reserve1: float
    tvl_usdc: float
    volume_24h_usdc: float = 0.0
    fee_pct: float
    apr_pct: float | None = None
    total_supply: float


class PoolListResponse(BaseModel):
    pools: list[PoolResponse]
    total: int


# ═══════════════════════════════════════════════════════════════
# Contract Addresses (for frontend to call on-chain directly)
# ═══════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════
# Contract Addresses
# ═══════════════════════════════════════════════════════════════


class ChainEndpointResponse(BaseModel):
    """One chain a client may need to talk to."""

    chain_id: int
    rpc_url: str
    # False means this chain was inherited from the single-chain configuration
    # rather than chosen for this role. A client that must not guess — anything
    # about to move real money — can tell a decision from a default.
    explicit: bool


class ContractAddressesResponse(BaseModel):
    """All deployed contract addresses. Frontend needs these for direct on-chain calls."""

    usdc: str
    synthetic_factory: str
    amm_router: str
    vault_factory: str
    reasoning_trace_registry: str
    asset_registry: str
    price_oracle: str

    # Individual synthetic token addresses
    synthetics: dict[str, str]  # symbol → address, e.g. {"sTSLA": "0x..."}

    # AMM pool addresses. None means the on-chain read failed (RPC error) —
    # distinct from {}, which means the chain was read and genuinely reports
    # zero pools. Collapsing these into one falsy value is exactly the #1356
    # defect: a failed read must never render as a measured zero.
    pools: dict[str, str] | None  # pair → address, e.g. {"USDC/sTSLA": "0x..."}

    # Vault addresses. Same None-vs-{} distinction as `pools`.
    vaults: dict[str, str] | None  # symbol → address, e.g. {"vMOMENTUM": "0x..."}

    # Chain info.
    #
    # These two keep their original meaning: the chain the contract addresses
    # above are deployed on, i.e. the EXECUTION chain. Existing clients read
    # them and stay correct. New clients should prefer the explicit blocks
    # below, which say which chain they mean instead of leaving it implied.
    chain_id: int
    rpc_url: str

    # Two-chain split (#1240). Payments and execution are the same chain today
    # and diverge at the Arc mainnet cutover. Serving both unconditionally —
    # rather than only once they differ — means a client never has to infer a
    # missing block, and `split` says outright whether a wallet flow needs
    # chain switching.
    payments_chain: ChainEndpointResponse
    execution_chain: ChainEndpointResponse
    split_chain: bool


# ═══════════════════════════════════════════════════════════════
# Strategy Signals (live evaluation)
# ═══════════════════════════════════════════════════════════════


class SignalResponse(BaseModel):
    asset: str
    signal: str  # "long" | "flat" | "scaled"
    weight: float
    reason: str
    strategy_name: str


class StrategySignalResponse(BaseModel):
    strategy_id: str
    paper_title: str
    signals: list[SignalResponse]


class StrategySignalsResponse(BaseModel):
    strategy_count: int
    # `regime` is retained for backward compatibility with existing frontend
    # reads. It is the flat_pct-derived ENSEMBLE CONSENSUS bucket, not a market
    # regime (#659) — `ensemble_consensus` carries the same value under the
    # correct name. A true market regime would come from a detector.
    regime: str
    ensemble_consensus: str | None = None
    confidence: float
    target_weights: dict[str, float]
    strategies: list[StrategySignalResponse]
    timestamp: str


# ═══════════════════════════════════════════════════════════════
# Agent Status (monitoring)
# ═══════════════════════════════════════════════════════════════


class AgentStatusResponse(BaseModel):
    alive: bool
    last_heartbeat: str | None = None
    regime: str | None = None
    regime_confidence: float | None = None
    regime_source: str | None = None
    strategy_count: int = 0
    managed_vaults: int = 0
    last_rebalance: str | None = None
    recent_events: list[dict] = []


# ═══════════════════════════════════════════════════════════════
# AMM Health
# ═══════════════════════════════════════════════════════════════


class AMMPoolHealth(BaseModel):
    """Health status of a single AMM pool (synth/USDC pair)."""

    symbol: str
    status: str  # "healthy" | "low_liquidity" | "empty" | "error"
    liquidity_usdc: float = 0.0
    oracle_price: float | None = None
    reserve_token: float = 0.0
    reserve_usdc: float = 0.0
    last_update: str  # ISO 8601


class AMMHealthResponse(BaseModel):
    """Health status of all AMM pools."""

    pools: list[AMMPoolHealth]
    healthy_count: int = 0
    total_pools: int = 0


class StrategyReturnsResponse(BaseModel):
    """Persisted real daily returns for a strategy.

    Returned by GET /api/strategies/{id}/returns. Only present when the
    strategy has a real BacktestResultRecord row — never synthesized from
    fixture metrics (#passport-honesty).
    """

    strategy_id: str
    source: str = "persisted_backtest"
    start: str | None = None
    end: str | None = None
    n: int
    daily_returns: list[float]
