"""Strategy data models — shared across all components.

Passport-aware: every strategy carries enough provenance metadata to be
independently auditable per `docs/specs/strategy-passport-spec.md`. Selection-
bias controls (DSR, PBO, OOS sharpe split) live on `BacktestResult` rather
than here — see `models/backtest.py`.

Renamed ``Strategy`` → ``StrategyPassport`` per Issue #159 (T-PE.2).
The multi-paper ``papers: list[PaperRef]`` shape replaces the old scalar
``paper_*`` fields so the passport is fusion-native from day one.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from archimedes.models.paper_ref import PaperRef

__all__ = [
    "PaperRef",
    "PositionSizing",
    "RebalanceFrequency",
    "SignalDefinition",
    # Backwards-compat alias
    "Strategy",
    "StrategyPassport",
    "StrategyStatus",
]


class StrategyStatus(str, Enum):
    """Lifecycle state of a strategy."""

    CANDIDATE = "candidate"  # Extracted from paper, not yet validated
    VALIDATED = "validated"  # Passed backtesting validation gate
    LIVE = "live"  # Active in at least one portfolio
    RETIRED = "retired"  # Removed from active use
    REJECTED = "rejected"  # Failed the rigor gate — visible failure, not silently dropped


class PositionSizing(str, Enum):
    """Sizing rules a passport can declare.

    This enum must remain a SUPERSET of ``strategy_dsl.POSITION_SIZING_TYPES``
    (#1769). The passport card's sizing field is derived from the validated DSL
    spec, and ``StrategyPassportRecord.to_strategy_passport`` coerces the stored
    column back through this enum — so a DSL sizing type with no member here is
    not "unsupported", it is a ``ValueError`` on read for every row that uses
    it. ``test_passport_spec_parity.py`` fails if the DSL grows a type this list
    has not grown with it. ``risk_parity`` / ``kelly`` have no DSL counterpart
    and are curated-YAML vocabulary; the superset direction is the safe one.
    """

    EQUAL_WEIGHT = "equal_weight"
    RISK_PARITY = "risk_parity"
    KELLY = "kelly"
    INVERSE_VOL = "inverse_vol"
    FULL_INVESTED_WHEN_IN_MARKET = "full_invested_when_in_market"
    VOLATILITY_TARGET = "volatility_target"


class RebalanceFrequency(str, Enum):
    """Rebalance cadences a passport can declare.

    Same superset rule as ``PositionSizing`` above, and pinned by the same file:
    ``_passport_spec_fields`` constructs this enum from a validated spec's
    ``rebalance_frequency``, so a DSL cadence with no member here is a
    ``ValueError`` out of every generated persist — not an unsupported option.
    Today the two vocabularies happen to be identical; the test is what keeps
    them from drifting apart silently.
    """

    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


@dataclass(frozen=True)
class SignalDefinition:
    """A single entry/exit signal used by a strategy.

    Kept intentionally simple for the hackathon — a human-readable
    description + the key parameters. Post-hackathon this becomes
    executable code.
    """

    name: str  # e.g. "50/200 MA crossover"
    direction: str  # "entry" | "exit"
    description: str  # Human-readable explanation
    parameters: dict[str, float] = field(default_factory=dict)  # e.g. {"fast_period": 50}


RegimeTag = Literal["bull", "bear", "regime_neutral"]


@dataclass
class StrategyPassport:
    """A trading strategy — paper-grounded and passport-bearing.

    Produced by: Dan (curated library + LLM extraction)
    Consumed by: Önder (backtest evaluation, portfolio construction),
                 Chuan (strategy DB, API), Daniel (strategy explorer UI)

    Passport reference: docs/specs/strategy-passport-spec.md

    ``papers`` is a list of ``PaperRef`` — curated strategies have 1,
    fusion strategies have ≥2.  All paper provenance (title, authors,
    DOI, venue, year, citation count) lives inside each ``PaperRef``.
    """

    id: str  # Deterministic hash of paper + methodology
    papers: list[PaperRef] = field(default_factory=list)
    methodology_summary: str = ""  # 2-3 sentence plain English
    asset_universe: list[str] = field(default_factory=list)  # Ticker symbols
    # Provenance of the asset_universe pick (#857, follow-up to #847's
    # user > model > full precedence): "user" | "model" | "full". A
    # model-picked universe is a mild look-ahead channel (the model can pick
    # names it already "knows" did well over the window from training data);
    # this makes the choice auditable on the passport without gating on it.
    universe_source: str | None = None
    signals: list[SignalDefinition] = field(default_factory=list)
    position_sizing: PositionSizing = PositionSizing.EQUAL_WEIGHT
    rebalance_frequency: RebalanceFrequency = RebalanceFrequency.WEEKLY
    risk_constraints: dict[str, float] = field(default_factory=dict)  # e.g. {"max_drawdown": 0.20, "max_leverage": 1.0}
    risk_profiles: list[str] = field(
        default_factory=list
    )  # Risk-tier tags: "conservative" | "moderate" | "aggressive" | "hyper_risky"
    status: StrategyStatus = StrategyStatus.CANDIDATE
    extraction_reasoning: str = ""  # Full LLM reasoning for extraction
    created_at: datetime | None = None
    updated_at: datetime | None = None

    # ── Methodology integrity ───────────────────────────────
    # SHA-256 over the canonical methodology — methodology_text when populated,
    # otherwise methodology_summary. See compute_methodology_hash() for the rule.
    methodology_hash: str | None = None
    methodology_text: str | None = None  # Full extracted methodology (longer than summary)
    extraction_llm: str | None = None  # e.g. "claude-opus-4-7" — null if hand-curated
    extraction_prompt_hash: str | None = None  # SHA-256 of the extraction prompt

    # ── Curation trail (v1: Dan is the sole curator) ────────
    curator_wallet: str | None = None  # Wallet of human curator who validated
    curator_validation_at: datetime | None = None
    curator_note: str | None = None  # Per-paper rationale

    # ── Backtest engine binding ─────────────────────────────
    strategy_code_path: str | None = None  # Path to the analytics-engine strategy file
    strategy_code_hash: str | None = None  # SHA-256 of the strategy file contents
    # Raw validated DSL spec JSON (the same dict a fusion proposal emits). When
    # present and non-empty, the live signal evaluator interprets it directly via
    # the shared condition tree — the SAME evaluator the backtest uses — instead
    # of keyword-matching into a hardcoded evaluator (and silently buy-and-holding
    # when no keyword matches). Live↔backtest signal parity is the whole point.
    strategy_spec: dict[str, Any] | None = None

    # ── On-chain anchor ─────────────────────────────────────
    on_chain_registration_tx: str | None = None  # StrategyRegistry contract tx hash
    on_chain_registration_block: int | None = None  # Block number of registration tx
    arcscan_url: str | None = None  # e.g. https://testnet.arcscan.app/tx/0x...

    # ── Paper claims (for paper-vs-actual delta in UI) ───────
    # Blended across all papers in the passport; for single-paper curated
    # strategies this is just the one paper's claim.
    paper_claimed_sharpe: float | None = None
    paper_claimed_cagr: float | None = None
    paper_claimed_max_dd: float | None = None
    paper_claim_blended_sharpe: float | None = None  # Weighted blend across papers

    # ── Regime suitability ──────────────────────────────────
    regime_tag: RegimeTag = "regime_neutral"

    # ── Placeholder backtest stubs (pre-analytics-engine-run) ──
    stub_sharpe: float | None = None
    stub_cagr: float | None = None
    stub_max_dd: float | None = None
    stub_win_rate: float | None = None
    stub_calmar: float | None = None
    stub_corr_spy: float | None = None

    # ── Real backtest results (from backtest_fixtures.json) ────
    real_sharpe: float | None = None
    real_sortino: float | None = None
    real_cagr: float | None = None
    real_max_dd: float | None = None
    real_win_rate: float | None = None
    real_calmar: float | None = None
    real_corr_spy: float | None = None
    real_total_trades: int | None = None
    real_backtest_start: str | None = None
    real_backtest_end: str | None = None
    deflated_sharpe_ratio: float | None = None
    dsr_p_value: float | None = None
    num_trials_in_selection: int | None = None
    pbo_score: float | None = None
    out_of_sample_sharpe: float | None = None
    passes_rigor_gate: bool = False
    kelly_fraction: float | None = None
    sharpe_ci_lower: float | None = None
    sharpe_ci_upper: float | None = None
    n_obs_daily: int | None = None

    # ── Convenience accessors (read first paper) ────────────
    # These provide a simple migration path for code that only
    # needs the primary paper. For multi-paper strategies callers
    # should iterate ``papers`` directly.

    @property
    def paper_arxiv_id(self) -> str:
        """arxiv_id of the primary (first) paper, or empty string.

        **Display only. Never use this for provenance (#1637).** A fusion
        strategy synthesizes from several papers and this names exactly one of
        them, so anything that binds a decision, a trace or a hash to it
        under-reports the strategy by construction — which is precisely how
        ``agent_runner`` came to write a single-element ``consulted_paper_hashes``
        for multi-paper strategies and sign it. The provenance surface is
        ``papers`` — the full cited list.
        """
        return (self.papers[0].arxiv_id or "") if self.papers else ""

    @property
    def paper_title(self) -> str:
        """Title of the primary (first) paper, or empty string."""
        return self.papers[0].title if self.papers else ""

    @property
    def paper_authors(self) -> list[str]:
        """Authors of the primary (first) paper."""
        return self.papers[0].authors if self.papers else []

    @property
    def paper_venue(self) -> str | None:
        return self.papers[0].venue if self.papers else None

    @property
    def paper_year(self) -> int | None:
        return self.papers[0].year if self.papers else None

    @property
    def paper_doi(self) -> str | None:
        return self.papers[0].doi if self.papers else None

    @property
    def paper_citation_count(self) -> int | None:
        return self.papers[0].citation_count if self.papers else None

    @property
    def is_active(self) -> bool:
        return self.status in (StrategyStatus.VALIDATED, StrategyStatus.LIVE)

    @property
    def is_paper_grounded(self) -> bool:
        """True iff the strategy traces back to a real published paper."""
        return any(p.arxiv_id or p.doi for p in self.papers)

    def compute_methodology_hash(self) -> str:
        """Compute deterministic SHA-256 hash of the methodology.

        Hashes `methodology_text` if present, else `methodology_summary`.
        UTF-8 with stripped leading/trailing whitespace; the canonical form
        anyone can recompute from the stored content.
        """
        source = (self.methodology_text or self.methodology_summary).strip()
        return hashlib.sha256(source.encode("utf-8")).hexdigest()

    @staticmethod
    def compute_methodology_hash_keccak(text: str) -> str:
        """Compute keccak256 hash matching on-chain ``eth_utils.keccak``.

        Uses ``hashlib`` with the keccak variant (SHA-3 / keccak-256).
        Returns ``0x``-prefixed hex string.
        """
        import hashlib as _hl

        return "0x" + _hl.new("sha3_256", text.encode("utf-8")).hexdigest()


# ── Backwards-compat alias ──────────────────────────────────────
# Everything that imported ``Strategy`` keeps working. New code
# should use ``StrategyPassport`` directly.
Strategy = StrategyPassport
