"""Render every live LLM prompt through its real call path, for fixed inputs.

This is the *golden capture harness* behind ``test_prompt_registry_goldens.py``
and the ``#1800`` prompt-registry refactor. It is deliberately NOT a test module
(no ``test_`` prefix, nothing collected by pytest): it is imported by that test
AND executed as a script against an OLD checkout, so the same fixed inputs
render the same prompts on both sides of the refactor::

    PYTHONPATH=<old-tree>/backend python backend/tests/prompt_capture.py > old.json
    PYTHONPATH=backend            python backend/tests/prompt_capture.py > new.json
    diff old.json new.json      # must be empty — the refactor moves bytes, not meaning

Every entry is captured by driving the *real* caller (``StrategyFusion.propose``,
``_debate_round``, ``_validate_brief``, ``synthesize_passport``,
``PortfolioAgent.propose_portfolio``) with a recording backend that stores the
``(system, user)`` pair it was handed. Capturing the module constants directly
would prove only that the constants moved; driving the callers proves the bytes
that reach the provider are unchanged, including every ``.format`` /
``.substitute`` / f-string render in between.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any

# The prompts are pure text; nothing here should touch a DB, Redis, or a network.
os.environ.setdefault("ARCHIMEDES_FUSION_ENABLED", "1")


# ── Recording backend ────────────────────────────────────────────────────────


@dataclass
class RecordingBackend:
    """LLMBackend-shaped stub that records prompts and replays canned JSON."""

    replies: list[str] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)
    model_id: str = "capture-model"
    served_model: str = "capture-model"
    available: bool = True

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self.replies[min(len(self.calls) - 1, len(self.replies) - 1)] if self.replies else "{}"


# ── Fixed inputs (identical on both sides of the refactor) ───────────────────

FIXED_INTENT = "low-volatility bond alternative with a trend overlay"

_PAPER_ROWS = [
    (
        "2101.00001",
        "Time-Series Momentum in Sovereign Bonds",
        "We document a persistent time-series momentum effect across sovereign bond futures.",
        "q-fin.PM",
        ("q-fin.PM", "q-fin.ST"),
        "2021-01-01",
    ),
    (
        "2102.00002",
        "Volatility-Managed Portfolios Revisited",
        "Scaling exposure by the inverse of realized variance raises risk-adjusted returns.",
        "q-fin.PM",
        ("q-fin.PM",),
        "2021-02-02",
    ),
    (
        "2103.00003",
        "Trend Following and the Cross-Section of Carry",
        "Carry and trend signals are complementary across asset classes.",
        "q-fin.TR",
        ("q-fin.TR", "q-fin.PM"),
        "2021-03-03",
    ),
]

_FUSION_REPLY = json.dumps(
    {
        "strategy_name": "Bond Trend Carry Overlay",
        "thesis": "Pre-backtest hypothesis fusing trend and vol-management on bonds.",
        "source_arxiv_ids": ["2101.00001", "2102.00002"],
        "fusion_reasoning": "Trend from the first paper, vol scaling from the second.",
        "paper_mechanisms": [
            {"arxiv_id": "2101.00001", "mechanism": "time-series momentum", "spec_elements": ["sma_200"]},
            {"arxiv_id": "2102.00002", "mechanism": "volatility management", "spec_elements": ["sma_200"]},
        ],
        "novelty_rationale": "Neither paper combines the two on sovereign bonds.",
        "risk_notes": "Pre-backtest; selection bias not yet corrected.",
        "strategy_spec": {
            "name": "Bond Trend Carry Overlay",
            "asset_universe": ["TLT", "IEF"],
            "rebalance_frequency": "monthly",
            "entry": {"gt": ["close", "sma_200"]},
            "exit": {"lt": ["close", "sma_200"]},
            "position_sizing": {"type": "full_invested_when_in_market"},
            "source_arxiv_ids": ["2101.00001", "2102.00002"],
            "indicators": ["sma_200"],
            "parameter_variants": {"sma_200": [150, 175, 200, 225, 250]},
        },
    }
)

_DEBATE_REPLY = json.dumps(
    {
        "verdict": "act",
        "confidence": 0.6,
        "key_claims": [
            {
                "claim": "Trend persistence survives the embargo window.",
                "candidate_id": "C1",
                "arxiv_ids": ["2101.00001"],
            },
            {"claim": "Vol scaling lowers drawdown.", "candidate_id": "C1", "arxiv_ids": ["2102.00002"]},
        ],
        "discard": [{"arxiv_id": "2103.00003", "reason": "carry is out of universe"}],
    }
)

_PORTFOLIO_REPLY = json.dumps(
    {
        "thesis": "Defensive tilt with a trend overlay.",
        "picks": [
            {"ticker": "TLT", "weight": 0.2, "paper_anchor": "moskowitz_2012_tsmom", "reasoning": "duration trend"}
        ],
    }
)


@dataclass
class _FakeStrategy:
    """Shape ``portfolio_agent._format_strategies`` reads off a Strategy row."""

    id: str
    paper_title: str
    real_sharpe: float | None
    real_cagr: float | None
    strategy_code_path: str


class _NullEmitter:
    """``_debate_round`` only awaits ``emit``; nothing is asserted on it here."""

    async def emit(self, event: str, **payload: Any) -> int:
        return 0


@dataclass
class _FakeProposal:
    """Shape ``_candidate_cards`` / ``_debate_pool_order`` read off a proposal."""

    strategy_name: str
    source_arxiv_ids: list[str]
    strategy_spec: dict[str, Any]


# ── Capture ──────────────────────────────────────────────────────────────────


def _corpus_papers() -> list[Any]:
    from archimedes.agents.strategy_fusion import CorpusPaper

    return [
        CorpusPaper(
            arxiv_id=a,
            title=t,
            abstract=ab,
            primary_category=pc,
            categories=cats,
            published=pub,
        )
        for a, t, ab, pc, cats, pub in _PAPER_ROWS
    ]


def _capture_fusion(out: dict[str, str]) -> None:
    from archimedes.agents.strategy_fusion import FusionBrief, StrategyFusion, _repair_spec

    papers = _corpus_papers()
    brief = FusionBrief(
        asset_classes=["us_bond_long", "us_equity_etf"],
        risk_appetite="conservative",
        strategic_direction=f"{FIXED_INTENT} — favor trend mechanisms",
        max_papers=8,
        market_context={"regime": "neutral", "confidence": 0.42},
    )
    rec = RecordingBackend(replies=[_FUSION_REPLY])
    StrategyFusion(backend=rec, corpus=papers, candidates=papers).propose(brief)
    out["fusion.proposer.system"], out["fusion.proposer.user"] = rec.calls[0]

    rec2 = RecordingBackend(replies=[json.dumps({"strategy_spec": {"entry": {}, "exit": {}}})])
    _repair_spec(rec2, brief, json.loads(_FUSION_REPLY))
    out["fusion.spec_repair.system"], out["fusion.spec_repair.user"] = rec2.calls[0]


def _capture_brief_validation(out: dict[str, str]) -> None:
    import archimedes.services.llm_backend as llm_backend_mod
    from archimedes.agents.generation_pipeline import _validate_brief
    from archimedes.api.generate_schemas import GenerateBrief

    rec = RecordingBackend(
        replies=[json.dumps({"is_valid": True, "intent_summary": FIXED_INTENT, "asset_classes_inferred": []})]
    )
    original = llm_backend_mod.make_llm_backend
    llm_backend_mod.make_llm_backend = lambda *a, **k: rec  # type: ignore[assignment]
    try:
        asyncio.run(
            _validate_brief(
                GenerateBrief(
                    intent=FIXED_INTENT,
                    risk_appetite="conservative",
                    asset_classes=["us_bond_long"],
                    max_papers=8,
                )
            )
        )
    finally:
        llm_backend_mod.make_llm_backend = original  # type: ignore[assignment]
    out["brief_validation.system"], out["brief_validation.user"] = rec.calls[0]


def _capture_debate(out: dict[str, str]) -> None:
    import archimedes.services.llm_backend as llm_backend_mod
    from archimedes.agents.debate_engine import _debate_round

    pool = [
        _FakeProposal(
            "Bond Trend Carry Overlay", ["2101.00001", "2102.00002"], {"entry": {"gt": ["close", "sma_200"]}}
        ),
        _FakeProposal("Defensive Vol Target", ["2102.00002"], {"entry": {"lt": ["close", "sma_50"]}}),
    ]
    evidence_by_id = {a: {"title": t, "published": pub} for a, t, _ab, _pc, _c, pub in _PAPER_ROWS}
    rec = RecordingBackend(replies=[_DEBATE_REPLY])
    original = llm_backend_mod.make_llm_backend
    llm_backend_mod.make_llm_backend = lambda *a, **k: rec  # type: ignore[assignment]
    try:
        asyncio.run(
            _debate_round(
                pool,
                None,
                _NullEmitter(),
                "cand-1",
                evidence_by_id,
            )
        )
    finally:
        llm_backend_mod.make_llm_backend = original  # type: ignore[assignment]
    # Fixed [bull-r1, bear-r1, bull-r2, bear-r2] order (R3 determinism).
    labels = ["bull_r1", "bear_r1", "bull_r2", "bear_r2"]
    for label, (system, _user) in zip(labels, rec.calls, strict=True):
        out[f"debate.turn.system.{label}"] = system
    out["debate.turn.user"] = rec.calls[0][1]


def _capture_paper_passport(out: dict[str, str]) -> None:
    from archimedes.services.arxiv_pipeline import PaperMeta, synthesize_passport

    meta = PaperMeta(
        arxiv_id="2101.00001",
        title="Time-Series Momentum in Sovereign Bonds",
        authors=["A. Author", "B. Author"],
        abstract="We document a persistent time-series momentum effect across sovereign bond futures.",
        year=2021,
        categories=["q-fin.PM"],
    )
    rec = RecordingBackend(replies=["{}"])
    synthesize_passport(meta, "Body text of the paper. " * 8, rec)
    out["paper_passport.synth.system"], out["paper_passport.synth.user"] = rec.calls[0]


def _capture_portfolio(out: dict[str, str]) -> None:
    from archimedes.agents.portfolio_agent import PortfolioAgent

    strategies = [
        _FakeStrategy(
            "11111111-2222-3333-4444-555555555555",
            "Faber 2007 Tactical Asset Allocation",
            0.61,
            0.074,
            "strategies/faber_2007_tactical.py",
        ),
        _FakeStrategy(
            "66666666-7777-8888-9999-aaaaaaaaaaaa",
            "Moskowitz 2012 Time Series Momentum",
            None,
            None,
            "strategies/moskowitz_2012_tsmom.py",
        ),
    ]
    market_ranking = [
        {
            "synth": "sTLT",
            "display": "TLT",
            "asset_class": "us_bond_long",
            "score": 1.25,
            "momentum_90d": 0.031,
            "vol_ann": 0.142,
            "exchange": "NASDAQ",
        },
        {
            "synth": "sSPY",
            "display": "SPY",
            "asset_class": "us_equity_etf",
            "score": -0.5,
            "momentum_90d": -0.012,
            "vol_ann": 0.171,
            "exchange": "NYSE",
        },
    ]
    rec = RecordingBackend(replies=[_PORTFOLIO_REPLY])
    PortfolioAgent(backend=rec).propose_portfolio(
        regime="neutral",
        regime_confidence=0.42,
        risk_profile="conservative",
        usdc_floor=0.4,
        synth_budget=0.6,
        market_ranking=market_ranking,
        strategies=strategies,
        scan_universe_synths={"sTLT"},
        rigor_statuses={"11111111-2222-3333-4444-555555555555": "pass"},
    )
    out["portfolio.construction.system"], out["portfolio.construction.user"] = rec.calls[0]


def capture_prompts() -> dict[str, str]:
    """Every live prompt, rendered through its real caller, for fixed inputs."""
    out: dict[str, str] = {}
    _capture_brief_validation(out)
    _capture_fusion(out)
    _capture_debate(out)
    _capture_paper_passport(out)
    _capture_portfolio(out)
    return dict(sorted(out.items()))


def main() -> int:
    json.dump(capture_prompts(), sys.stdout, indent=2, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
