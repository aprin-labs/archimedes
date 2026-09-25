"""Spec-repair retry + universe steering + friendly brief errors.

These paths all survive the T1.1 debate-society cutover: brief validation lives
in the orchestrator (runs before pipeline dispatch), and ``StrategyFusion.propose``
IS the debate society's proposer (the pool fans it across steers, and the A5
conformance guard DROPS spec-less proposals — so a model that omits the spec
would silently empty the debate pool).

Hermetic: the LLM backend is a stub; no network, no DB.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import archimedes.agents.strategy_fusion as sf
import pytest
from archimedes.agents.generation_pipeline import _invalid_brief_message

# ── fixtures ──────────────────────────────────────────────────────────

_SPEC = {
    "name": "Fused Trend",
    "asset_universe": ["SPY", "GLD"],
    "rebalance_frequency": "monthly",
    "entry": {"gt": ["close", "sma_200"]},
    "exit": {"lt": ["close", "sma_200"]},
    "position_sizing": {"type": "full_invested_when_in_market"},
    "source_arxiv_ids": ["0706.1497", "1704.03022"],
    "indicators": ["sma_200"],
}

_PROPOSAL_NO_SPEC = {
    "strategy_name": "Fused Trend",
    "thesis": "Trend plus vol management.",
    "source_arxiv_ids": ["0706.1497", "1704.03022"],
    "fusion_reasoning": "Faber timing gates Moreira-Muir sizing.",
    "novelty_rationale": "Combination not in either paper.",
    "risk_notes": "Pre-backtest.",
}


class _StubBackend:
    """LLM stub returning queued responses; records every (system, user) call."""

    available = True
    served_model = "stub-model"
    model_id = "stub-model"

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self._responses.pop(0)


def _brief(asset_classes: list[str] | None = None) -> sf.FusionBrief:
    return sf.FusionBrief(asset_classes=asset_classes or [], risk_appetite=sf.RiskProfile.MODERATE)


def _papers(n: int = 3) -> list:
    return [
        SimpleNamespace(
            arxiv_id=f"070{i}.1497",
            title=f"Paper {i}",
            abstract="a",
            categories=["q-fin.PM"],
            published="2007-01-01",
            summary="s",
        )
        for i in range(n)
    ]


def _propose_with(monkeypatch, backend: _StubBackend, brief=None) -> sf.FusionProposal:
    """Run the REAL propose() with corpus/backend/prompt seams stubbed."""
    fusion = sf.StrategyFusion()
    papers = _papers()
    monkeypatch.setattr(sf.StrategyFusion, "_resolve_corpus", lambda self: papers)
    monkeypatch.setattr(sf, "select_candidates", lambda brief, corpus: papers)
    monkeypatch.setattr(sf.StrategyFusion, "_resolve_backend", lambda self: backend)
    monkeypatch.setattr(sf, "_build_user_prompt", lambda brief, candidates: "prompt")
    return fusion.propose(brief or _brief())


def _ok_response(spec=None) -> str:
    payload = dict(_PROPOSAL_NO_SPEC)
    payload["source_arxiv_ids"] = ["0700.1497", "0701.1497"]
    if spec is not None:
        payload["strategy_spec"] = spec
    return json.dumps(payload)


# ── spec-repair retry ─────────────────────────────────────────────────


def test_missing_spec_is_repaired_with_one_retry(monkeypatch):
    spec = dict(_SPEC, source_arxiv_ids=["0700.1497", "0701.1497"])
    backend = _StubBackend([_ok_response(spec=None), json.dumps(spec)])
    proposal = _propose_with(monkeypatch, backend)
    assert proposal.status == "ok"
    assert isinstance(proposal.strategy_spec, dict)
    assert proposal.strategy_spec["entry"] == {"gt": ["close", "sma_200"]}
    # Exactly one extra call, using the repair prompt.
    assert len(backend.calls) == 2
    assert "spec compiler" in backend.calls[1][0]


def test_repair_tolerates_wrapped_response(monkeypatch):
    spec = dict(_SPEC, source_arxiv_ids=["0700.1497", "0701.1497"])
    backend = _StubBackend([_ok_response(spec=None), json.dumps({"strategy_spec": spec})])
    proposal = _propose_with(monkeypatch, backend)
    assert isinstance(proposal.strategy_spec, dict)
    assert proposal.strategy_spec["exit"] == {"lt": ["close", "sma_200"]}


def test_failed_repair_falls_back_to_text_only(monkeypatch):
    backend = _StubBackend([_ok_response(spec=None), "not json at all"])
    proposal = _propose_with(monkeypatch, backend)
    assert proposal.status == "ok"  # the text proposal is still actionable
    assert proposal.strategy_spec is None  # honest text-only, never fabricated
    assert len(backend.calls) == 2  # one retry, no loop


def test_present_spec_needs_no_retry(monkeypatch):
    spec = dict(_SPEC, source_arxiv_ids=["0700.1497", "0701.1497"])
    backend = _StubBackend([_ok_response(spec=spec)])
    proposal = _propose_with(monkeypatch, backend)
    assert isinstance(proposal.strategy_spec, dict)
    assert len(backend.calls) == 1


def test_partially_repaired_spec_degrades_to_text_only(monkeypatch):
    """A repair that returns entry/exit but misses required DSL fields must not
    slip through — the proposal degrades to honest text-only, never a spec that
    explodes later in evaluation/debate."""
    partial = {"entry": {"gt": ["close", "sma_200"]}, "exit": {"lt": ["close", "sma_200"]}}
    backend = _StubBackend([_ok_response(spec=None), json.dumps(partial)])
    proposal = _propose_with(monkeypatch, backend)
    assert proposal.status == "ok"
    assert proposal.strategy_spec is None
    assert len(backend.calls) == 2  # the one bounded retry, no loop


def test_invalid_model_spec_degrades_to_text_only(monkeypatch):
    """A model-emitted spec that fails DSL validation degrades to text-only
    instead of returning a spec the rigor/backtest path would refuse.

    The invalidity here is a closed-enum violation the model plausibly emits
    (``quarterly`` is not a supported cadence). It used to be
    ``look_ahead_safe: false`` — no longer a validation failure, because that
    field is gone from the schema entirely: a spec no longer gets to grade its
    own look-ahead safety in either direction.
    """
    spec = dict(_SPEC, source_arxiv_ids=["0700.1497", "0701.1497"], rebalance_frequency="quarterly")
    backend = _StubBackend([_ok_response(spec=spec)])
    proposal = _propose_with(monkeypatch, backend)
    assert proposal.status == "ok"
    assert proposal.strategy_spec is None
    assert len(backend.calls) == 1  # invalid ≠ missing: no repair retry burned


# ── universe steering (#847) ──────────────────────────────────────────


def test_user_assets_win_over_model_universe():
    spec = {"asset_universe": ["TSLA", "NVDA"]}
    out, source, gaps = sf._spec_universe(_brief(asset_classes=["SPY", "gld"]), spec)
    assert out == ["SPY", "GLD"]  # canonical, order-preserving
    assert source == "user"
    assert gaps == []


def test_unsteered_brief_uses_models_validated_universe():
    spec = {"asset_universe": ["SPY", "GLD", "NOT_A_REAL_TICKER_XYZ"]}
    out, source, gaps = sf._spec_universe(_brief(), spec)
    assert out == ["SPY", "GLD"]  # SSOT-validated; garbage dropped
    assert "NOT_A_REAL_TICKER_XYZ" not in out
    assert source == "model"
    assert gaps == []


def test_model_universe_is_capped():
    many = list(sf.SUPPORTED_UNIVERSE)[: sf._MODEL_UNIVERSE_CAP + 5]
    out, source, gaps = sf._spec_universe(_brief(), {"asset_universe": many})
    assert len(out) == sf._MODEL_UNIVERSE_CAP
    assert source == "model"
    assert gaps == []


def test_no_user_no_model_falls_back_to_full_universe():
    out, source, gaps = sf._spec_universe(_brief(), {"asset_universe": []})
    assert out == list(sf.SUPPORTED_UNIVERSE)  # #682's floor preserved
    assert source == "full"
    assert gaps == []


def test_propose_threads_model_universe_when_unsteered(monkeypatch):
    spec = dict(_SPEC, source_arxiv_ids=["0700.1497", "0701.1497"], asset_universe=["SPY", "GLD"])
    backend = _StubBackend([_ok_response(spec=spec)])
    proposal = _propose_with(monkeypatch, backend, brief=_brief())
    # The thesis's own instruments survive — not the full ~300-asset SSOT dump.
    assert proposal.strategy_spec["asset_universe"] == ["SPY", "GLD"]
    assert proposal.universe_source == "model"


# ── friendly brief-validation errors ──────────────────────────────────


@pytest.mark.parametrize("reason", ["gibberish", "off-topic", None])
def test_invalid_brief_message_guides_instead_of_insults(reason):
    m = _invalid_brief_message(reason)
    assert "Describe what you want" in m
    assert "e.g." in m
    if reason:
        assert reason in m  # the honest reason is kept, framed


def test_single_proxy_model_universe_is_treated_as_parroted_default():
    # A weak model emitting the classic bare ["SPY"] regardless of thesis is a
    # non-choice — fall back to the full universe (never trust the parrot).
    out, source, gaps = sf._spec_universe(_brief(), {"asset_universe": ["SPY"]})
    assert out == list(sf.SUPPORTED_UNIVERSE)
    assert source == "full"
    assert gaps == []
