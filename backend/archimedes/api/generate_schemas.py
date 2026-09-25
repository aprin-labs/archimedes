"""Pydantic models for the streaming Generate pipeline.

Implements the event protocol in docs/specs/generation-streaming-spec.md.
Each event is shipped as a single SSE `data:` payload — these models exist
so route handlers, the pipeline orchestrator, and the test suite all share
one definition of what an event looks like on the wire.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# ── Request side ──────────────────────────────────────────────────────────

# NOTE: max_papers below is bounded to [_MAX_PAPERS_FLOOR, _MAX_PAPERS_CEILING]
# rather than importing archimedes.agents.strategy_fusion.MIN_PAPERS /
# FUSION_MAX_PAPERS directly — a top-level import here creates a real
# circular import (strategy_fusion -> generation_json -> llm_backend ->
# archimedes.services.__init__ [re-exports generation_pipeline for
# backwards compat] -> generation_pipeline -> back to this module, which is
# still mid-definition). The values are kept in lockstep by a drift-guard
# test (test_generate_schemas_depth_drift.py) that imports both modules
# independently and asserts equality — raising the cap on one side without
# the other fails that test.
_MAX_PAPERS_FLOOR = 2  # == archimedes.agents.strategy_fusion.MIN_PAPERS
_MAX_PAPERS_CEILING = 30  # == archimedes.agents.strategy_fusion.FUSION_MAX_PAPERS
_MAX_PAPERS_DEFAULT = 8  # == archimedes.agents.strategy_fusion.DEFAULT_MAX_PAPERS

# User-chosen strategy name limits. Whitespace is collapsed BEFORE these
# checks, so \t/\n arriving in a paste are normalized rather than rejected;
# any control character that survives collapsing (NUL, ESC, DEL, …) is a
# hard reject — those never belong in a display name.
NAME_MAX_LEN = 80

#: Control characters that survive whitespace collapsing. Public because
#: ``archimedes.services.brief_screen`` screens the BRIEF with the same rule —
#: one definition, not two held together by a drift test (#1801).
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_NAME_CONTROL_CHARS = CONTROL_CHARS  # back-compat alias for the private name

# Brief length bounds (#1801). Before this the intent was UNBOUNDED: it lands
# verbatim in the validator message and, as ``strategic_direction``, verbatim
# in the fusion proposer prompt for every steer, so an unbounded intent is an
# unbounded per-generation token bill and an unbounded injection surface.
# 600 is ~3x the longest entry in the in-app Surprise Me bank (211 chars) and
# ~3x the 90–200 char ceiling docs/writing-a-brief.md already recommends.
# ``brief_screen`` reads INTENT_MAX_LEN from here so the schema bound and the
# screener's ``shape.too_long`` can never drift apart; the UI's ``maxLength``
# is pinned to the same number by ui/test/generate-brief-limits.test.js.
INTENT_MIN_LEN = 1
INTENT_MAX_LEN = 600


class GenerateBrief(BaseModel):
    """User-supplied generation brief."""

    intent: str = Field(
        ...,
        min_length=INTENT_MIN_LEN,
        max_length=INTENT_MAX_LEN,
        description=(
            f"Free-text strategy request, {INTENT_MIN_LEN}\u2013{INTENT_MAX_LEN} characters. "
            "Bounded because it is interpolated verbatim into every prompt the "
            "generation spends on (#1801); content screening lives in "
            "archimedes.services.brief_screen, which runs before the paywall."
        ),
    )
    risk_appetite: Literal["fixed_income", "conservative", "moderate", "aggressive", "hyper_risky"] = "moderate"
    asset_classes: list[str] | None = None
    capital_usdc: float | None = None
    max_papers: int = Field(
        default=_MAX_PAPERS_DEFAULT,
        ge=_MAX_PAPERS_FLOOR,
        le=_MAX_PAPERS_CEILING,
        description=(
            f"How many papers the pipeline RETRIEVES and shows the model. "
            f"Bounded to [{_MAX_PAPERS_FLOOR}, {_MAX_PAPERS_CEILING}] — "
            "strategy_fusion.py's MIN_PAPERS/FUSION_MAX_PAPERS, the range the "
            "pipeline actually enforces (a token + cross-paper-coherence "
            "budget), not a wider nominal cap it would silently clamp anyway. "
            "This is retrieval WIDTH, not a citation quota: how many the model "
            "is asked to fuse is strategy_fusion.FUSE_TARGET_MIN, and a "
            "justified shortfall against it is accepted, never blocked."
        ),
    )
    name: str | None = Field(
        default=None,
        description=(
            "Optional user-chosen strategy name (1–80 chars after whitespace normalization). "
            "Applied verbatim to the WINNING candidate only; considered-rejects keep their "
            "auto-derived names so the library never shows N identically-named strategies "
            "per generation. Empty/whitespace-only is treated as unset."
        ),
    )

    @field_validator("name")
    @classmethod
    def _sanitize_name(cls, v: str | None) -> str | None:
        """Strip + collapse internal whitespace; empty → None; reject control chars / >80 chars."""
        if v is None:
            return None
        v = re.sub(r"\s+", " ", v).strip()
        if not v:
            return None
        if _NAME_CONTROL_CHARS.search(v):
            raise ValueError("name must not contain control characters")
        if len(v) > NAME_MAX_LEN:
            raise ValueError(f"name must be at most {NAME_MAX_LEN} characters")
        return v


class GenerateStartRequest(BaseModel):
    brief: GenerateBrief
    n_candidates: int = Field(default=1, ge=1, le=5, description="How many candidates to consider internally")
    mode: str | None = Field(
        default=None,
        description=(
            "Optional pipeline override (API compatibility; ignored as of T1.1 Phase-3 cutover). "
            "The debate society is the sole generation pipeline — non-debate overrides are logged and discarded."
        ),
    )
    model: str | None = Field(
        default=None,
        description=(
            "Optional LLM model id chosen on the Generate page (matches ui/src/data/modelPricing.json). "
            "Two server-side gates apply: (1) the paid-tier entitlement gate (T1.8) rejects a premium "
            "(Anthropic) model from a non-entitled caller with HTTP 402 — see PREMIUM_MODELS_ENABLED / "
            "PREMIUM_MODELS_ALLOWLIST; (2) the free-tier allowlist then honors the id only if it is an "
            "allowlisted free model, otherwise it falls back to the env default. Absent → env default."
        ),
    )


class GenerateStartResponse(BaseModel):
    job_id: str
    stream_url: str
    ttl_seconds: int


# ── Event payloads ────────────────────────────────────────────────────────


EventName = Literal[
    "job_queued",
    "brief_validated",
    "pipeline_selected",
    "candidates_selected",
    "agent_iteration",
    "tool_called",
    "tool_result",
    "candidate_drafted",
    "candidate_evaluated",
    "best_selected",
    "trace_hashed",
    "persisted",
    "done",
    "error",
]


def _ts() -> str:
    return datetime.now(UTC).isoformat()


class GenerateEvent(BaseModel):
    """Generic envelope for any event shipped on the SSE stream.

    `event` is the SSE event name; `data` is the JSON payload that the
    frontend's `addEventListener(event, …)` callback receives.
    """

    id: int
    event: EventName
    data: dict[str, Any]

    @classmethod
    def make(cls, *, event_id: int, event: EventName, **payload: Any) -> GenerateEvent:
        payload.setdefault("ts", _ts())
        return cls(id=event_id, event=event, data=payload)


# ── Job listing + candidates list ─────────────────────────────────────────


#: What the cost meter measured for one job. Free-form on purpose: the payload
#: carries its own ``schema`` version (``cost_v1``), so the measurement record
#: can gain fields without a lockstep API-model change. It is RAW MEASUREMENT
#: ONLY — token counts, seconds, byte counts, write tallies. No prices, and no
#: money is derivable from it without the pricing table, which lives outside the
#: server. The paywall quote seam (``generation_payment.quote()``) is unaffected
#: and still reports ``pricing_model: "flat_v1"`` (#1217).
JobCost = dict[str, Any]


class JobSummary(BaseModel):
    job_id: str
    # "stalled" (#1355) is a READ-TIME derived state, never a value stored in
    # Redis: a "running" job whose heartbeat_at has gone stale is normalized
    # to it in generate_routes._normalize_state — see that function's
    # docstring. Not written by anything, only produced on output.
    state: Literal["queued", "running", "stalled", "done", "error", "cancelled"]
    brief_intent: str
    created_at: str
    updated_at: str
    n_candidates: int
    best_strategy_id: str | None = None
    cost: JobCost | None = None


class JobsListResponse(BaseModel):
    jobs: list[JobSummary]


class JobCostResponse(BaseModel):
    """One job's measured resource consumption.

    ``cost`` is ``None`` while the job is still running (the snapshot is written
    once, on the terminal path) and stays ``None`` for a job that predates the
    instrumentation — an honest absence, not a zeroed record.
    """

    job_id: str
    # Widened to include "stalled" alongside JobSummary.state (#1355 review
    # follow-up) — this endpoint routes `heartbeat_at` through the same
    # `_normalize_state` as `/jobs` and `/jobs/{id}`, so all three surfaces
    # must accept the same derived state or a stale job would 500 here
    # instead of reporting `stalled` like its siblings.
    state: Literal["queued", "running", "stalled", "done", "error", "cancelled"]
    cost: JobCost | None = None


class CandidateSummary(BaseModel):
    candidate_id: str
    strategy_id: str | None
    strategy_name: str
    rigor_verdict: dict[str, Any] | None = None
    passes_rigor: bool
    selected: bool
    regime: str | None = None  # "bull", "bear", or "neutral" (Issue #163)
    generation_method: str | None = None  # "debate" | "debate_abstain" | "fusion" | …


class CandidatesListResponse(BaseModel):
    job_id: str
    best_candidate_id: str | None
    candidates: list[CandidateSummary]


class CreditSummary(BaseModel):
    """One row of the calling user's own generation-credit ledger (#1441 ->
    v8 Lane 1.3a: make it visible, not just correct). Deliberately narrower
    than ``GenerationCreditRecord.to_payload()`` — payer_wallet, network, and
    settlement_ref are payment-plumbing internals the UI has no use for;
    only what a human needs to recognize "I have an unspent credit" ships.
    """

    id: int
    status: Literal["pending", "available", "consumed", "void"]
    created_at: str | None = None
    job_id: str | None = None
    # USDC dollars, derived from the ledger's raw base units — None until a
    # claim settles (mirrors the nullability of amount_base_units itself).
    amount_usdc: float | None = None
