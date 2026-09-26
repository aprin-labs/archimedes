"""Hermetic tests for the T1.1 debate society (Phase 1 skeleton).

No live LLM / Redis / Postgres / Arc RPC. The proposer/critic LLM seams are
mocked at the boundary (``make_llm_backend`` / ``evaluate_fusion_spec``); the
deterministic cull/rank/abstain logic is tested directly against fake
``FusionEvalResult``-shaped objects.

Covers spec §9 Phase-1 tests:
  1. canned society → leaderboard, every entry has_real_rigor=True
  2. regime divergence (fix A4): bull vs bear evidence sets differ (Jaccard < 1)
  3. ABSTAIN → populated SKIP-shaped result (generation_method="debate_abstain")
  4. DebateUnavailable on empty pool → honest fallback signal (subclass)
  5. pool_size → num_trials (fix A1): evaluate_fusion_spec called with pool_size
  6. model threading (fix A3): FusionProposal.model reflects the user pick
  7. DSL conformance (fix A5): non-interpretable indicator stems dropped, no
     DSLError escapes
  8. the retired ARCHIMEDES_DEBATE_ENABLED flag (fix A2, #880): setting it to
     "false" cannot route _pick_pipeline off the society
  9. cited-paper union non-empty; transcript in fixed role order (R3 determinism)
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import ClassVar

import pytest
from archimedes.agents import debate_engine as de
from archimedes.agents.strategy_fusion import FusionBrief, StrategyFusion, load_corpus, select_candidates
from archimedes.api.generate_schemas import GenerateBrief
from archimedes.services.dsl_lookahead_audit import PASS as LOOK_AHEAD_PASS
from archimedes.services.dsl_lookahead_audit import DslLookAheadAudit

# ── Fixture corpus: 3 momentum-flavored + 3 defensive-flavored + 1 noise ──────

_MOMENTUM_ROWS = [
    {
        "arxiv_id": f"2401.0000{i}",
        "title": f"Cross-Sectional Equity Momentum and Trend Following {i}",
        "authors": ["A. Mom"],
        "primary_category": "q-fin.PM",
        "categories": ["q-fin.PM"],
        "published": f"2024-01-0{i}",
        "updated": f"2024-01-0{i}",
        "abstract": "Momentum, trend-following, breakout and carry factor alpha in the cross-section of equities.",
        "pdf_url": f"http://x/m{i}",
        "pdf_sha256": "a" * 64,
        "pdf_path": f"data/corpus/pdfs/2401.0000{i}.pdf",
        "text_path": f"data/corpus/text/2401.0000{i}.txt",
        "fetched_at": "2026-05-16T00:00:00Z",
    }
    for i in (1, 2, 3)
]

_DEFENSIVE_ROWS = [
    {
        "arxiv_id": f"2402.0000{i}",
        "title": f"Volatility-Managed Defensive Hedging {i}",
        "authors": ["B. Def"],
        "primary_category": "q-fin.PM",
        "categories": ["q-fin.PM"],
        "published": f"2024-02-0{i}",
        "updated": f"2024-02-0{i}",
        "abstract": "Volatility, vol-managed defensive hedge tail risk drawdown and mean-reversion overlays.",
        "pdf_url": f"http://x/d{i}",
        "pdf_sha256": "b" * 64,
        "pdf_path": f"data/corpus/pdfs/2402.0000{i}.pdf",
        "text_path": f"data/corpus/text/2402.0000{i}.txt",
        "fetched_at": "2026-05-16T00:00:00Z",
    }
    for i in (1, 2, 3)
]

_ALL_ROWS = _MOMENTUM_ROWS + _DEFENSIVE_ROWS


@pytest.fixture
def corpus(tmp_path):
    p = tmp_path / "manifest.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in _ALL_ROWS), encoding="utf-8")
    return load_corpus(p)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every test starts with the live flags explicitly cleared.

    ``ARCHIMEDES_DEBATE_ENABLED`` used to be cleared here too. It was retired by
    #880 and has had no reader since, so clearing it cleared nothing — removed
    2026-08-31 (#834). ``test_pick_pipeline_is_debate_unconditionally`` now
    *sets* it instead, which is the assertion the delenv only looked like.
    ``ARCHIMEDES_FUSION_ENABLED`` went the same way on 2026-09-02 (deck Q4):
    fusion is unconditional, so there is no switch left to clear.
    """
    monkeypatch.delenv("ARCHIMEDES_CORPUS_MANIFEST", raising=False)
    monkeypatch.delenv("DEBATE_POOL_MAX", raising=False)


# ── Canned backends ───────────────────────────────────────────────────────────

_CONFORMANT_SPEC = {
    "name": "Momentum/vol fusion",
    "asset_universe": ["SPY"],
    "rebalance_frequency": "monthly",
    "entry": {"gt": ["momentum_20", 0]},
    "exit": {"lt": ["close", "sma_200"]},
    "position_sizing": {"type": "full_invested_when_in_market"},
    "look_ahead_safe": True,
    "parameter_variants": {"momentum": [10, 20, 40]},
}

# The A5 trap: an indicator stem the LLM can plausibly emit that
# ``dsl_to_backtrader._make_indicator`` cannot build. It used to be
# ``realized_vol_5`` — that one is now implemented, so the example moved to a
# stem that is genuinely absent from ``SUPPORTED_INDICATORS``. The guard's job
# is unchanged: keep an un-interpretable spec out of C-rigor, where a DSLError
# would take down the whole leaderboard build.
_NONCONFORMANT_SPEC = {
    "name": "MACD trap",
    "asset_universe": ["SPY"],
    "rebalance_frequency": "monthly",
    "entry": {"gt": ["macd_12", 0]},  # no macd in SUPPORTED_INDICATORS
    "exit": {"lt": ["close", "sma_200"]},
    "position_sizing": {"type": "full_invested_when_in_market"},
    "look_ahead_safe": True,
}


class _CannedFusionBackend:
    """Returns a parseable fusion of the first 2 candidate papers + a spec.

    ``served_model`` is derived from the requested model so the A3 model-threading
    test can prove the user's pick flowed through.
    """

    def __init__(self, model=None, *, spec=None):
        self.model_id = model or "env-default"
        self.served_model = f"served::{self.model_id}"
        self.available = True
        self._spec = spec if spec is not None else _CONFORMANT_SPEC

    def complete(self, system: str, user: str) -> str:
        payload = json.loads(user)
        ids = [p["arxiv_id"] for p in payload["candidate_papers"][:2]]
        spec = dict(self._spec)
        spec["source_arxiv_ids"] = ids
        return json.dumps(
            {
                "strategy_name": "Debate canned fusion",
                "thesis": "Fuse momentum + vol mechanisms.",
                "source_arxiv_ids": ids,
                "fusion_reasoning": "canned",
                "novelty_rationale": "canned",
                "risk_notes": "canned",
                "strategy_spec": spec,
            }
        )


# ── Fake FusionEvalResult-shaped objects (deterministic critic inputs) ────────

#: A real look-ahead audit in its clean state, used to fill the rigor double's
#: look-ahead fields. Built from the production dataclass so a change to the
#: status vocabulary or the label text shows up here instead of leaving a stale
#: hand-typed string that no longer matches anything the gate produces.
_CLEAN_LOOK_AHEAD = DslLookAheadAudit(
    status=LOOK_AHEAD_PASS,
    interpreter_verified=True,
    broker_cheat_check=True,
)


def _fake_ev(*, cagr, dsr=1.5, passing=True, oos=1.2, num_trials=5):
    rigor = SimpleNamespace(
        dsr=dsr,
        dsr_p_value=0.01,
        pbo_score=0.1,
        oos_sharpe=oos,
        in_sample_sharpe=1.3,
        # Mirrors a RigorVerdict that cleared the real structural look-ahead
        # audit (services/dsl_lookahead_audit.py). The fields are read off an
        # actual DslLookAheadAudit rather than typed as literals, so this double
        # cannot drift into a state the production verdict never produces.
        look_ahead_clean=_CLEAN_LOOK_AHEAD.passed,
        look_ahead_status=_CLEAN_LOOK_AHEAD.status,
        look_ahead_label=_CLEAN_LOOK_AHEAD.label,
        look_ahead_reason=_CLEAN_LOOK_AHEAD.reason,
        num_trials=num_trials,
        passing=passing,
        data_source="synthetic",
        admissible=False,
    )
    bt = SimpleNamespace(
        sharpe_ratio=1.4,
        sortino_ratio=1.6,
        max_drawdown=-0.1,
        cagr=cagr,
        calmar_ratio=1.1,
        win_rate=0.55,
        total_trades=42,
    )
    return SimpleNamespace(rigor=rigor, backtest=bt, success=True, admissible=False, error=None, spec={})


#: The evidence surface the fixture corpus's proposers would have read — the
#: `{arxiv_id: {"title", "published"}}` map `_propose_pool` now returns (#1636).
#: It is the debate's attribution whitelist: an id outside it is a
#: hallucination and gets stripped from the claim that named it.
_EVIDENCE_BY_ID = {r["arxiv_id"]: {"title": r["title"], "published": r["published"]} for r in _ALL_ROWS}


def _fake_proposal(name, ids, spec=None):
    return SimpleNamespace(
        strategy_name=name,
        thesis="thesis",
        source_arxiv_ids=ids,
        strategy_spec=spec or dict(_CONFORMANT_SPEC),
        fusion_reasoning="reasoning",
        novelty_rationale="novelty",
        is_actionable=True,
    )


class _FakeEmit:
    def __init__(self):
        self.events = []

    async def emit(self, name, **kw):
        self.events.append((name, kw))


# ── Test 1 — leaderboard build, every entry has_real_rigor=True ───────────────


def test_build_leaderboard_all_entries_have_real_rigor():
    rigor_results = [
        (_fake_proposal("A", ["2401.00001", "2402.00001"]), _fake_ev(cagr=0.2, dsr=2.0)),
        (_fake_proposal("B", ["2401.00002", "2402.00002"]), _fake_ev(cagr=0.1, dsr=1.0)),
    ]
    board = de.build_leaderboard(rigor_results, regime="neutral", base_id="cand_1")
    assert len(board) == 2
    assert all(e.has_real_rigor for e in board)
    assert all(e.generation_method == "debate" for e in board)
    # Leader is the higher-DSR candidate and keeps the base id.
    assert board[0].strategy_name == "A"
    assert board[0].candidate_id == "cand_1"
    assert board[1].candidate_id == "cand_1_alt1"


# ── Test 2 — regime divergence (fix A4) ───────────────────────────────────────


def test_regime_steers_diverge_on_fixture_corpus(monkeypatch, corpus):
    # Isolate the KEYWORD+bias divergence guarantee (fix A4). The semantic rerank
    # (FUSION_SEMANTIC_RETRIEVAL, default ON) keys on strategic_direction — IDENTICAL
    # for both steers — so on a degraded/TF-IDF corpus it re-sorts bull and bear to
    # the SAME order and ERASES the divergence. That collapse is precisely the
    # diversity-theater risk the spec flags (§4): genuine divergence needs real
    # embeddings (#778). The bias-at-the-keyword-core guarantee is what must hold now.
    monkeypatch.setenv("FUSION_SEMANTIC_RETRIEVAL", "false")
    brief = FusionBrief(asset_classes=[], strategic_direction="systematic equity strategy", max_papers=3)
    bull = {p.arxiv_id for p in select_candidates(brief, corpus, regime_bias="bull")}
    bear = {p.arxiv_id for p in select_candidates(brief, corpus, regime_bias="bear")}
    assert bull and bear
    # Jaccard < 1.0 — the steers must NOT collapse to the same tail.
    jaccard = len(bull & bear) / len(bull | bear)
    assert jaccard < 1.0


# ── Test 3 — ABSTAIN path ─────────────────────────────────────────────────────


def test_abstain_when_no_candidate_beats_passive_null():
    # Every candidate's edge (cagr) is below the 5 bps null bar → abstain.
    rigor_results = [
        (_fake_proposal("A", ["2401.00001", "2402.00001"]), _fake_ev(cagr=0.0)),
        (_fake_proposal("B", ["2401.00002", "2402.00002"]), _fake_ev(cagr=0.0001)),
    ]
    board = de.build_leaderboard(rigor_results, regime="neutral", base_id="cand_1")
    assert len(board) == 1
    abstain = board[0]
    assert abstain.generation_method == "debate_abstain"
    assert abstain.has_real_rigor is False
    assert abstain.passes_rigor is False
    assert abstain.weights == {}
    assert "abstain" in abstain.strategy_name.lower()


# ── Test 4 — DebateUnavailable on empty pool ──────────────────────────────────


async def test_empty_pool_raises_debate_unavailable(monkeypatch):
    from archimedes.agents.generation_pipeline import FusionUnavailable

    async def _empty_pool(*a, **k):
        return [], {}

    monkeypatch.setattr(de, "_propose_pool", _empty_pool)
    monkeypatch.setattr(de.asyncio, "to_thread", _passthrough_to_thread)

    brief = GenerateBrief(intent="momentum equities")
    with pytest.raises(de.DebateUnavailable) as exc:
        await de._run_debate_candidate(candidate_id="cand_1", brief=brief, emit=_FakeEmit())
    # Subclasses FusionUnavailable so the existing fallback relabels to agent.
    assert isinstance(exc.value, FusionUnavailable)


async def _passthrough_to_thread(fn, *a, **k):
    return fn(*a, **k)


# ── Test 5 — pool_size → num_trials (fix A1) ──────────────────────────────────


async def test_critic_rigor_threads_pool_size_as_num_trials(monkeypatch):
    seen_num_trials = []

    def _fake_eval(spec, *, num_trials=None, **kw):
        seen_num_trials.append(num_trials)
        return _fake_ev(cagr=0.2, num_trials=num_trials)

    monkeypatch.setattr("archimedes.services.fusion_evaluator.evaluate_fusion_spec", _fake_eval)
    monkeypatch.setattr(de.asyncio, "to_thread", _passthrough_to_thread)

    pool = [_fake_proposal(f"P{i}", [f"id{i}a", f"id{i}b"]) for i in range(3)]
    pool_size = len(pool)
    results = await de._critic_rigor(pool, pool_size)

    assert seen_num_trials == [pool_size, pool_size, pool_size]
    # And the persisted verdict echoes pool_size as the DSR denominator.
    board = de.build_leaderboard(results, regime="neutral", base_id="cand_1")
    assert board[0].rigor_verdict["num_trials"] == pool_size


# ── Test 6 — model threading (fix A3) ─────────────────────────────────────────


def test_model_pick_threads_into_served_model(monkeypatch, corpus):
    captured = {}

    def _fake_make(model=None, **kw):
        captured["model"] = model
        return _CannedFusionBackend(model=model)

    monkeypatch.setattr("archimedes.agents.strategy_fusion.make_llm_backend", _fake_make)

    brief = FusionBrief(asset_classes=[], strategic_direction="momentum", max_papers=4)
    proposal = StrategyFusion(model="user-model-x", corpus=corpus).propose(brief)

    assert captured["model"] == "user-model-x"
    assert proposal.requested_model == "user-model-x"  # what we asked for (backend.model_id)
    assert proposal.model == "served::user-model-x"  # the TRUE served model (field of record)
    assert proposal.is_actionable


# ── Test 7 — DSL conformance (fix A5) ─────────────────────────────────────────


def test_dsl_conformance_guard_rejects_uninterpretable_indicators():
    # realized_vol is now implemented by the interpreter, so it must PASS the
    # guard — the guard tracks SUPPORTED_INDICATORS in both directions.
    assert "realized_vol" in de._CONFORMANT_INDICATORS
    assert de._dsl_conformance_ok(_CONFORMANT_SPEC) is True
    assert de._dsl_conformance_ok(_NONCONFORMANT_SPEC) is False
    assert de._dsl_conformance_ok(None) is False
    assert de._dsl_conformance_ok({"name": "no trees"}) is False


async def test_propose_pool_drops_nonconformant_specs(monkeypatch, corpus):

    def _fake_make(model=None, **kw):
        return _CannedFusionBackend(model=model, spec=_NONCONFORMANT_SPEC)

    monkeypatch.setattr("archimedes.agents.strategy_fusion.make_llm_backend", _fake_make)
    monkeypatch.setattr(de.asyncio, "to_thread", _passthrough_to_thread)

    brief = GenerateBrief(intent="momentum equities", max_papers=4)
    pool, evidence_by_id = await de._propose_pool(brief, "m", corpus)
    # Every proposal emitted macd_12 → all dropped by the A5 guard; no DSLError.
    assert pool == []
    # ... but the papers those dropped proposers READ are still real evidence
    # (#1636): the debate's attribution whitelist is the union over attempted
    # proposals, not just the ones whose spec survived.
    assert evidence_by_id
    assert all(set(v) == {"title", "published"} for v in evidence_by_id.values())


# ── Test #893 — proposer pool dedup ────────────────────────────────────────────


def test_canonical_spec_hash_ignores_key_order_and_float_noise():
    a = {"entry": {"gt": ["momentum_20", 0]}, "exit": {"lt": ["close", "sma_200"]}, "weight": 0.1 + 0.2}
    b = {"exit": {"lt": ["close", "sma_200"]}, "entry": {"gt": ["momentum_20", 0]}, "weight": 0.3}
    assert de._canonical_spec_hash(a) == de._canonical_spec_hash(b)
    c = {"exit": {"lt": ["close", "sma_200"]}, "entry": {"gt": ["momentum_40", 0]}, "weight": 0.3}
    assert de._canonical_spec_hash(a) != de._canonical_spec_hash(c)


def test_canonical_spec_hash_ignores_marketing_name_and_citations():
    """Fix #893's actual failure mode: same trading logic, different LLM-picked
    name and citation set. These MUST hash identically, or dedup is a no-op
    against the exact scenario it's meant to catch."""
    base = dict(_CONFORMANT_SPEC)
    a = {**base, "name": "TrendFusionCryptoVolatility", "source_arxiv_ids": ["2401.00001", "2402.00001"]}
    b = {**base, "name": "CryptoVolTrendGuard", "source_arxiv_ids": ["2402.00001", "2401.00001"]}
    assert de._canonical_spec_hash(a) == de._canonical_spec_hash(b)
    # But a genuinely different entry condition must still be caught.
    c = {**b, "entry": {"gt": ["momentum_40", 0]}}
    assert de._canonical_spec_hash(a) != de._canonical_spec_hash(c)


class _NameVaryingFusionBackend:
    """Like _CannedFusionBackend, but emits a different strategy_name and
    source_arxiv_ids ORDER on every call while keeping strategy_spec's
    behavioral fields (entry/exit/universe/sizing) fixed — the real #893
    scenario: independently-generated proposals that converge on the same
    trading logic under different marketing names."""

    _MARKETING_NAMES: ClassVar[list[str]] = [
        "TrendFusionCryptoVolatility",
        "CryptoVolTrendGuard",
        "Volatility-Hedge Trend-Follow",
        "Crypto-TailHedging TrendFollow",
    ]

    def __init__(self, model=None, *, spec):
        self.model_id = model or "env-default"
        self.served_model = f"served::{self.model_id}"
        self.available = True
        self._spec = spec
        self._call_n = 0

    def complete(self, system: str, user: str) -> str:
        payload = json.loads(user)
        ids = [p["arxiv_id"] for p in payload["candidate_papers"][:2]]
        if self._call_n % 2:
            ids = list(reversed(ids))
        name = self._MARKETING_NAMES[self._call_n % len(self._MARKETING_NAMES)]
        self._call_n += 1
        spec = dict(self._spec)
        spec["name"] = name
        spec["source_arxiv_ids"] = ids
        return json.dumps(
            {
                "strategy_name": name,
                "thesis": "Fuse momentum + vol mechanisms.",
                "source_arxiv_ids": ids,
                "fusion_reasoning": "canned",
                "novelty_rationale": "canned",
                "risk_notes": "canned",
                "strategy_spec": spec,
            }
        )


async def test_propose_pool_dedupes_identical_specs_across_steers(monkeypatch, corpus):
    """Fix #893: different regime/mechanism steers converging on the same
    trading logic — but with a different LLM-picked marketing name and
    citation order each time — must collapse to one pool entry, not be
    counted as independent trials."""
    monkeypatch.setenv("DEBATE_POOL_MAX", "4")

    # A conformant spec that also survives the full validate_strategy_spec pass
    # (unlike _CONFORMANT_SPEC, whose parameter_variants key doesn't reference an
    # entry/exit alias — fine for the narrow _dsl_conformance_ok check elsewhere,
    # but rejected by the full propose() validation, which nulls strategy_spec).
    dedup_spec = {k: v for k, v in _CONFORMANT_SPEC.items() if k != "parameter_variants"}

    backend = _NameVaryingFusionBackend(spec=dedup_spec)

    def _fake_make(model=None, **kw):
        return backend

    # Force every steer to select the SAME evidence set, so the only things
    # that vary between calls are the marketing name and citation order
    # (mimicking independent LLM calls converging on the same evidence).
    monkeypatch.setattr(
        "archimedes.agents.strategy_fusion.select_candidates",
        lambda fb, corpus, regime_bias=None: list(corpus)[:2],
    )
    monkeypatch.setattr("archimedes.agents.strategy_fusion.make_llm_backend", _fake_make)
    monkeypatch.setattr(de.asyncio, "to_thread", _passthrough_to_thread)

    brief = GenerateBrief(intent="momentum equities", max_papers=4)
    pool, _evidence_by_id = await de._propose_pool(brief, "m", corpus)

    # Sanity: the backend really did emit >1 distinct marketing name across
    # steers (i.e. this test exercises the actual #893 failure mode, not an
    # already-identical pool that would collapse trivially).
    assert backend._call_n > 1
    assert len({backend._MARKETING_NAMES[i % 4] for i in range(backend._call_n)}) > 1

    # Despite differently-named proposals, the trading logic is identical —
    # must collapse to a single unique entry.
    assert len(pool) == 1
    hashes = {de._canonical_spec_hash(p.strategy_spec) for p in pool}
    assert len(hashes) == len(pool)


# ── Test 8 — the retired flag cannot re-route the pipeline (fix A2) ───────────


def test_pick_pipeline_is_debate_unconditionally(monkeypatch):
    """T1.1 Phase-3 cutover: the society IS the pipeline — no flag, no legacy
    decision tree, and client mode overrides are accepted-but-ignored.

    The retired flag is *set to the value that used to disable the society*
    rather than deleted. A ``delenv`` for a name nothing reads asserts nothing;
    a ``setenv`` proves the retirement actually holds — someone who finds
    ``ARCHIMEDES_DEBATE_ENABLED=false`` in an old runbook and exports it cannot
    route generation off the debate society (#834, 2026-08-31).
    """
    from archimedes.agents.generation_pipeline import _pick_pipeline

    monkeypatch.setenv("ARCHIMEDES_DEBATE_ENABLED", "false")
    for override in (None, "fusion", "architect", "agent", "debate"):
        name, reason = _pick_pipeline(mode_override=override)
        assert name == "debate", f"override {override!r} must not route off the society"
        # The reason string is user-facing SSE copy — it must describe the
        # debate society in product language, never internal planning
        # shorthand ("T1.1 Phase-3 cutover" shipped to prod screens; see
        # test_generation_pipeline.py's no-internal-jargon guard).
        assert "debate" in reason.lower()


# ── Test 9 — cited-paper union non-empty; transcript fixed role order ─────────


def test_leaderboard_entries_carry_cited_papers():
    rigor_results = [
        (_fake_proposal("A", ["2401.00001", "2402.00001"]), _fake_ev(cagr=0.2)),
    ]
    board = de.build_leaderboard(rigor_results, regime="neutral", base_id="cand_1")
    union = {a for e in board for a in e.source_arxiv_ids}
    assert union == {"2401.00001", "2402.00001"}


class _DebateBackend:
    """Records prompts; returns role-appropriate claims so the rebuttal is testable."""

    model_id = "x"
    served_model = "x"
    available = True

    def __init__(self):
        self.prompts = []

    def complete(self, system, user):
        self.prompts.append(system)
        role = "bull" if "bull researcher" in system else "bear"
        return json.dumps(
            {
                "verdict": "act" if role == "bull" else "decline",
                "confidence": 0.7,
                "key_claims": [f"{role}-claim"],
            }
        )


async def test_debate_round_two_rounds_with_visible_rebuttal(monkeypatch):
    backend = _DebateBackend()
    monkeypatch.setattr("archimedes.services.llm_backend.make_llm_backend", lambda model=None, **k: backend)
    monkeypatch.setattr(de.asyncio, "to_thread", _passthrough_to_thread)

    pool = [_fake_proposal("A", ["2401.00001", "2402.00001"])]
    transcript = await de._debate_round(pool, "m", _FakeEmit(), "cand_1")
    # Fixed [bull-r1, bear-r1, bull-r2, bear-r2] order (R3 determinism).
    assert [(t["role"], t["round"]) for t in transcript] == [("bull", 1), ("bear", 1), ("bull", 2), ("bear", 2)]
    # The visible rebuttal: each round-2 prompt carries the OTHER side's round-1 claim.
    bull_r2_prompt, bear_r2_prompt = backend.prompts[2], backend.prompts[3]
    assert "bear-claim" in bull_r2_prompt  # bull rebuts the bear
    assert "bull-claim" in bear_r2_prompt  # bear rebuts the bull


async def test_debate_round_degrades_when_backend_unavailable(monkeypatch):
    class _Down:
        available = False

    monkeypatch.setattr("archimedes.services.llm_backend.make_llm_backend", lambda model=None, **k: _Down())
    monkeypatch.setattr(de.asyncio, "to_thread", _passthrough_to_thread)
    transcript = await de._debate_round([_fake_proposal("A", ["1", "2"])], "m", _FakeEmit(), "cand_1")
    assert transcript == []  # never gates; no backend → empty best-effort transcript


# ── Transcript capture (debate-transcript-capture): the return value of
#    _debate_round used to be discarded outright at the _run_debate_leaderboard
#    call site (`await _debate_round(...)` with no assignment) — a real, paid
#    4-turn bull/bear transcript vanished the instant the coroutine returned.
#    This is the end-to-end regression guard: run the FULL leaderboard builder
#    (not just _debate_round in isolation, which was already covered above)
#    and assert every entry — winner AND the tail alternate — carries it. ────


async def test_run_debate_leaderboard_attaches_transcript_to_every_entry(monkeypatch, corpus):
    """Stashing the capture line in debate_engine.py (reverting to a bare
    `await _debate_round(...)`) makes this fail: every entry's
    `debate_transcript` stays at its dataclass default of None. See the PR
    description / task report for the actual stash-and-rerun demonstration."""
    from archimedes.models.regime import Regime

    _patch_regime(monkeypatch, Regime.RISK_ON)  # non-crisis: must not force ABSTAIN
    monkeypatch.setattr(de.asyncio, "to_thread", _passthrough_to_thread)
    monkeypatch.setattr("archimedes.agents.strategy_fusion.load_corpus", lambda *a, **k: corpus)

    debate_backend = _DebateBackend()
    monkeypatch.setattr("archimedes.services.llm_backend.make_llm_backend", lambda model=None, **k: debate_backend)

    pool = [
        _fake_proposal("A", ["2401.00001", "2402.00001"]),
        _fake_proposal("B", ["2401.00002", "2402.00002"]),
    ]

    async def _fake_propose_pool(brief, model, corpus_arg):
        return pool, _EVIDENCE_BY_ID

    monkeypatch.setattr(de, "_propose_pool", _fake_propose_pool)

    _call_n = {"i": 0}

    def _fake_eval(spec, *, num_trials=None, **kw):
        _call_n["i"] += 1
        # Distinct DSR per candidate so build_leaderboard produces a real
        # winner + a real (non-abstain) alternate, not a tie or a cull-to-one.
        return _fake_ev(cagr=0.2, dsr=float(_call_n["i"]), num_trials=num_trials)

    monkeypatch.setattr("archimedes.services.fusion_evaluator.evaluate_fusion_spec", _fake_eval)

    brief = GenerateBrief(intent="momentum equities", max_papers=4)
    leaderboard = await de._run_debate_leaderboard(candidate_id="cand_1", brief=brief, emit=_FakeEmit())

    assert len(leaderboard) == 2, "need winner + one alternate to prove BOTH carry the transcript"
    expected_roles = [("bull", 1), ("bear", 1), ("bull", 2), ("bear", 2)]
    for entry in leaderboard:
        assert entry.debate_transcript is not None, (
            f"{entry.candidate_id} lost its debate transcript — the exact discard this guards against"
        )
        assert [(t["role"], t["round"]) for t in entry.debate_transcript] == expected_roles
    # One debate happened, not one per candidate: every entry shares the SAME
    # transcript object (the debate runs once, over the whole pool, before
    # C-null culls it to a winner).
    assert leaderboard[0].debate_transcript is leaderboard[1].debate_transcript


async def test_run_debate_leaderboard_abstain_entry_still_carries_transcript(monkeypatch, corpus):
    """The C-regime ABSTAIN path builds its result via `_abstain_result`, a
    different code path than the normal survivor path — must not skip the
    transcript stamp just because it takes the early-return branch."""
    from archimedes.models.regime import Regime

    _patch_regime(monkeypatch, Regime.CRISIS)  # forces ABSTAIN
    monkeypatch.setattr(de.asyncio, "to_thread", _passthrough_to_thread)
    monkeypatch.setattr("archimedes.agents.strategy_fusion.load_corpus", lambda *a, **k: corpus)

    debate_backend = _DebateBackend()
    monkeypatch.setattr("archimedes.services.llm_backend.make_llm_backend", lambda model=None, **k: debate_backend)

    pool = [_fake_proposal("A", ["2401.00001", "2402.00001"])]

    async def _fake_propose_pool(brief, model, corpus_arg):
        return pool, _EVIDENCE_BY_ID

    monkeypatch.setattr(de, "_propose_pool", _fake_propose_pool)
    monkeypatch.setattr(
        "archimedes.services.fusion_evaluator.evaluate_fusion_spec",
        lambda spec, *, num_trials=None, **kw: _fake_ev(cagr=0.2, num_trials=num_trials),
    )

    brief = GenerateBrief(intent="momentum equities", max_papers=4)
    leaderboard = await de._run_debate_leaderboard(candidate_id="cand_1", brief=brief, emit=_FakeEmit())

    assert len(leaderboard) == 1
    assert leaderboard[0].generation_method == "debate_abstain"
    assert leaderboard[0].debate_transcript is not None


# ── Review fixes: pool-max bound + leaderboard top-N cap ──────────────────────


def test_pool_max_meaningfully_bounds_the_steer_grid(monkeypatch):
    # The steer grid is regime×mechanism (18), so DEBATE_POOL_MAX can actually cap
    # the fan-out (the prior 5-element _STEERS made the knob a no-op above 5).
    assert len(de._STEERS) == 18
    monkeypatch.setenv("DEBATE_POOL_MAX", "6")
    assert de._pool_max() == 6
    monkeypatch.setenv("DEBATE_POOL_MAX", "99")
    assert de._pool_max() == len(de._STEERS)  # clamped to the grid size, not unbounded


def test_build_leaderboard_caps_to_top_n(monkeypatch):
    monkeypatch.setenv("DEBATE_LEADERBOARD_MAX", "3")
    rigor_results = [
        (_fake_proposal(f"c{i}", [f"24{i}.0001", f"24{i}.0002"]), _fake_ev(cagr=0.2, dsr=float(i))) for i in range(8)
    ]
    board = de.build_leaderboard(rigor_results, regime="neutral", base_id="cand_1")
    assert len(board) == 3  # capped, not all 8 survivors
    # Highest-DSR leader kept its base id; the cap takes the top-3 by score.
    assert board[0].candidate_id == "cand_1"
    assert all(e.has_real_rigor for e in board)


# ── Phase 2 — C-regime non-votable Hierarchy-of-Truth gate ────────────────────


def _patch_regime(monkeypatch, regime, *, confidence=0.9, status="live"):
    """Patch the SHARED live-regime read (`current_regime`) + health at the boundary.

    `_critic_regime` reads the most-recently-constructed detector via the module-level
    `current_regime()` accessor (NOT a fresh GmmRegimeDetector, which would have no
    classification), so the test patches that accessor.
    """
    from types import SimpleNamespace as NS

    rc = NS(regime=regime, confidence=confidence) if regime is not None else None
    monkeypatch.setattr("archimedes.services.gmm_regime_detector.current_regime", lambda: rc)
    monkeypatch.setattr(
        "archimedes.services.gmm_regime_detector.gmm_regime_health",
        lambda: NS(status=status, reason="test"),
    )


def test_critic_regime_crisis_forces_abstain(monkeypatch):
    from archimedes.models.regime import Regime

    _patch_regime(monkeypatch, Regime.CRISIS)
    gate = de._critic_regime()
    assert gate["force_abstain"] is True
    assert gate["regime"] == "crisis"
    assert "CRISIS" in gate["reason"]


def test_critic_regime_risk_on_does_not_gate(monkeypatch):
    from archimedes.models.regime import Regime

    _patch_regime(monkeypatch, Regime.RISK_ON)
    gate = de._critic_regime()
    assert gate["force_abstain"] is False
    assert gate["regime"] == "risk_on"


def test_critic_regime_unavailable_fails_safe_to_no_gate(monkeypatch):
    # No regime read (None) must NEVER force-approve and must not crash — it simply
    # does not gate (the critic can only ABSTAIN, never spuriously APPROVE).
    _patch_regime(monkeypatch, None)
    gate = de._critic_regime()
    assert gate["force_abstain"] is False
    assert gate["regime"] is None


def test_regime_degraded_does_not_force_abstain(monkeypatch):
    # DEGRADED (VIX fallback) on a non-crisis regime must not force abstain — else
    # the society would always abstain whenever the GMM artifact is missing.
    from archimedes.models.regime import Regime

    _patch_regime(monkeypatch, Regime.RISK_OFF, status="degraded")
    gate = de._critic_regime()
    assert gate["force_abstain"] is False
    assert gate["degraded"] is True


def test_build_leaderboard_regime_gate_overrides_strong_survivors():
    # Even with candidates that clear C-null, a non-votable CRISIS gate abstains.
    rigor_results = [
        (_fake_proposal("A", ["2401.00001", "2402.00001"]), _fake_ev(cagr=0.3, dsr=3.0)),
        (_fake_proposal("B", ["2401.00002", "2402.00002"]), _fake_ev(cagr=0.2, dsr=2.0)),
    ]
    board = de.build_leaderboard(
        rigor_results,
        regime="neutral",
        base_id="cand_1",
        regime_force_abstain=True,
        regime_reason="CRISIS regime (confidence=0.90) — non-votable ABSTAIN",
    )
    assert len(board) == 1
    assert board[0].generation_method == "debate_abstain"
    assert "Hierarchy-of-Truth" in board[0].thesis or "Regime gate" in board[0].reasoning


# ── Phase 2 — C-prov non-votable provenance/embargo gate (Xia 1/2/4) ──────────


def test_critic_prov_drops_citations_outside_the_surface(corpus):
    # The fixture corpus surface = {2401.0000{1,2,3}, 2402.0000{1,2,3}} (embargo-clean).
    clean = _fake_proposal("clean", ["2401.00001", "2402.00001"])
    dirty = _fake_proposal("dirty", ["2401.00001", "9999.99999"])  # 9999.* not in the surface
    empty = _fake_proposal("empty", [])  # no citations → cannot verify provenance
    kept, dropped = de._critic_prov([clean, dirty, empty], corpus)
    assert [p.strategy_name for p in kept] == ["clean"]
    assert {p.strategy_name for p in dropped} == {"dirty", "empty"}


def test_critic_prov_keeps_fully_grounded_candidates(corpus):
    a = _fake_proposal("a", ["2401.00002", "2402.00002"])
    b = _fake_proposal("b", ["2401.00003", "2402.00003"])
    kept, dropped = de._critic_prov([a, b], corpus)
    assert len(kept) == 2
    assert dropped == []


def test_critic_prov_robust_to_missing_citations(corpus):
    # A proposal missing source_arxiv_ids (None / absent attr) must DROP, not raise
    # and abort the whole run (Copilot review).
    none_cited = SimpleNamespace(strategy_name="none", source_arxiv_ids=None)
    no_attr = SimpleNamespace(strategy_name="noattr")  # attribute absent entirely
    good = _fake_proposal("good", ["2401.00001", "2402.00001"])
    kept, dropped = de._critic_prov([none_cited, no_attr, good], corpus)
    assert [p.strategy_name for p in kept] == ["good"]
    assert {p.strategy_name for p in dropped} == {"none", "noattr"}


# ── Live-gate persistence seam (#788): entries carry real return_series ──────


def test_leaderboard_entries_carry_return_series_for_live_gate():
    """Without return_series, _persist_real_returns SKIPS debate winners and the
    passport reads 'pending' forever despite C-rigor's real backtest (first prod
    debate run, 2026-07-04). Entries must derive per-bar returns from the
    evaluator's equity curve — and stay None when no curve exists (never fabricate)."""
    ev = _fake_ev(cagr=0.2, dsr=2.0)
    ev.backtest.equity_curve = [100.0, 101.0, 99.99, 102.0]
    board = de.build_leaderboard(
        [(_fake_proposal("A", ["2401.00001", "2402.00001"]), ev)], regime="neutral", base_id="c1"
    )
    rs = board[0].return_series
    assert rs is not None and len(rs) == 3
    assert abs(rs[0] - 0.01) < 1e-9

    board2 = de.build_leaderboard(
        [(_fake_proposal("B", ["2401.00001", "2402.00001"]), _fake_ev(cagr=0.1, dsr=1.0))],
        regime="neutral",
        base_id="c2",
    )
    assert board2[0].return_series is None


# ── #1636 — the papers go INTO the debate ─────────────────────────────────────
#
# `_debate_round` used to send a `"; "`-joined list of strategy NAMES as the
# entire user message for all four paid turns. A judge reading "multi-agent
# debate over 10,000 papers" was being told something the live path did not do.
# The turns now argue over per-candidate evidence cards, every claim names the
# arXiv ids it rests on, and an id nobody was shown is stripped rather than
# recorded.


class _AttributingDebateBackend:
    """Emits structured, paper-attributed claims. `bad_ids` lets a test inject
    an id that never entered any proposer prompt — the hallucination probe."""

    model_id = "x"
    served_model = "x"
    available = True

    def __init__(self, *, claim_ids=("2401.00001",), discard_ids=("2402.00003",)):
        self.prompts: list[str] = []
        self.user_messages: list[str] = []
        self._claim_ids = list(claim_ids)
        self._discard_ids = list(discard_ids)

    def complete(self, system, user):
        self.prompts.append(system)
        self.user_messages.append(user)
        role = "bull" if "bull researcher" in system else "bear"
        return json.dumps(
            {
                "verdict": "act" if role == "bull" else "decline",
                "confidence": 0.7,
                "key_claims": [
                    {
                        "claim": f"{role}-claim",
                        "candidate_id": "C1",
                        "arxiv_ids": self._claim_ids,
                    }
                ],
                "discard": [{"arxiv_id": a, "reason": "no distinct mechanism"} for a in self._discard_ids],
            }
        )


async def test_debate_prompt_carries_arxiv_ids(monkeypatch):
    """The user message must be evidence cards, not a name list: every turn
    sees `[C1] Name — cites arXiv:xxxx "Title"`."""
    backend = _AttributingDebateBackend()
    monkeypatch.setattr("archimedes.services.llm_backend.make_llm_backend", lambda model=None, **k: backend)
    monkeypatch.setattr(de.asyncio, "to_thread", _passthrough_to_thread)

    pool = [_fake_proposal("Momentum/vol fusion", ["2401.00001", "2402.00001"])]
    await de._debate_round(pool, "m", _FakeEmit(), "cand_1", _EVIDENCE_BY_ID)

    assert backend.user_messages, "the debate must have run"
    for msg in backend.user_messages:
        assert "arXiv:2401.00001" in msg
        assert "arXiv:2402.00001" in msg
        # The TITLE too — an id with no title is not evidence a researcher can weigh.
        assert _EVIDENCE_BY_ID["2401.00001"]["title"] in msg
        assert "[C1]" in msg
    # The system prompt tells them the id vocabulary is closed.
    assert all("arxiv_ids" in p for p in backend.prompts)


async def test_debate_claim_with_unknown_arxiv_id_is_stripped_not_rendered(monkeypatch):
    """The anti-hallucination guard, mirroring strategy_fusion's valid_ids
    filter. An id that entered no proposer prompt is dropped — but the CLAIM
    survives with an empty `arxiv_ids`, so an unattributed claim is recorded
    as unattributed rather than deleted (which would shrink the transcript) or
    kept with its invented citation (which would launder it)."""
    backend = _AttributingDebateBackend(claim_ids=("9999.99999", "2401.00002"), discard_ids=("9999.99998",))
    monkeypatch.setattr("archimedes.services.llm_backend.make_llm_backend", lambda model=None, **k: backend)
    monkeypatch.setattr(de.asyncio, "to_thread", _passthrough_to_thread)

    pool = [_fake_proposal("A", ["2401.00001", "2402.00001"])]
    transcript = await de._debate_round(pool, "m", _FakeEmit(), "cand_1", _EVIDENCE_BY_ID)

    assert len(transcript) == 4
    for turn in transcript:
        assert len(turn["claims"]) == 1, "the claim itself must survive"
        claim = turn["claims"][0]
        assert claim["claim"] in {"bull-claim", "bear-claim"}
        assert claim["arxiv_ids"] == ["2401.00002"]  # the real id kept, 9999.* gone
        assert "9999.99999" not in json.dumps(turn)
        # A discard naming an id nobody was shown carries no prose worth
        # keeping, so it is dropped outright.
        assert turn["discard"] == []


async def test_debate_claim_with_only_unknown_ids_is_recorded_unattributed(monkeypatch):
    """The stronger half: when EVERY cited id is invented, the claim is kept
    with `arxiv_ids: []`. Dropping it would hide that the researcher argued
    something it could not ground."""
    backend = _AttributingDebateBackend(claim_ids=("9999.99999",), discard_ids=())
    monkeypatch.setattr("archimedes.services.llm_backend.make_llm_backend", lambda model=None, **k: backend)
    monkeypatch.setattr(de.asyncio, "to_thread", _passthrough_to_thread)

    transcript = await de._debate_round(
        [_fake_proposal("A", ["2401.00001", "2402.00001"])], "m", _FakeEmit(), "cand_1", _EVIDENCE_BY_ID
    )
    for turn in transcript:
        assert turn["claims"][0]["arxiv_ids"] == []
        assert turn["claims"][0]["claim"]


async def test_debate_paper_verdicts_written(monkeypatch, corpus):
    """The deterministic tally (0 tokens) that makes "the papers were debated"
    checkable: per paper, who cited it, who discarded it, and which retrieved
    papers nobody touched."""
    from archimedes.models.regime import Regime

    _patch_regime(monkeypatch, Regime.RISK_ON)
    monkeypatch.setattr(de.asyncio, "to_thread", _passthrough_to_thread)
    monkeypatch.setattr("archimedes.agents.strategy_fusion.load_corpus", lambda *a, **k: corpus)
    monkeypatch.setattr(
        "archimedes.services.llm_backend.make_llm_backend",
        lambda model=None, **k: _AttributingDebateBackend(),
    )

    pool = [_fake_proposal("A", ["2401.00001", "2402.00001"])]

    async def _fake_propose_pool(brief, model, corpus_arg):
        return pool, _EVIDENCE_BY_ID

    monkeypatch.setattr(de, "_propose_pool", _fake_propose_pool)
    monkeypatch.setattr(
        "archimedes.services.fusion_evaluator.evaluate_fusion_spec",
        lambda spec, *, num_trials=None, **kw: _fake_ev(cagr=0.2, num_trials=num_trials),
    )

    brief = GenerateBrief(intent="momentum equities", max_papers=4)
    board = await de._run_debate_leaderboard(candidate_id="cand_1", brief=brief, emit=_FakeEmit())

    verdicts = board[0].debate_paper_verdicts
    assert verdicts is not None, "every leaderboard entry must carry the tally"
    by_id = {v["arxiv_id"]: v for v in verdicts}
    # Every retrieved paper is listed — including the untouched ones, which is
    # the interesting part when only a few were argued over.
    assert set(by_id) == set(_EVIDENCE_BY_ID)
    assert [v["arxiv_id"] for v in verdicts] == sorted(by_id), "deterministic order"

    cited = by_id["2401.00001"]
    assert cited["verdict"] == "cited"
    assert sorted(cited["cited_by"]) == ["bear", "bull"]
    assert cited["citations"] == 4  # 4 turns × 1 claim each
    assert cited["title"] == _EVIDENCE_BY_ID["2401.00001"]["title"]

    discarded = by_id["2402.00003"]
    assert discarded["verdict"] == "discarded"
    assert discarded["discard_reasons"] == ["no distinct mechanism"]

    assert by_id["2401.00003"]["verdict"] == "unused"


def test_debate_pool_order_is_deterministic_not_the_steer_order():
    """`pool[:5]` used to take whichever five steers finished first. The order
    is now (-cited-paper-count, name, spec-hash) — total, stable, and about
    evidence rather than enumeration order. It is NOT a quality ranking:
    nothing is backtested at debate time."""
    thin = _fake_proposal("Zeta", ["2401.00001", "2402.00001"])
    thick = _fake_proposal("Alpha", ["2401.00001", "2402.00001", "2401.00002"])
    also_thin = _fake_proposal("Alpha2", ["2401.00002", "2402.00002"])

    ordered = de._debate_pool_order([thin, also_thin, thick])
    assert [p.strategy_name for p in ordered] == ["Alpha", "Alpha2", "Zeta"]
    # Same members in a different input order → same output order.
    assert de._debate_pool_order([thick, thin, also_thin]) == ordered


def test_debate_cards_name_the_papers_not_just_the_strategies():
    cards = de._candidate_cards([_fake_proposal("Momentum fusion", ["2401.00001"])], _EVIDENCE_BY_ID)
    assert cards.startswith("[C1] Momentum fusion — cites arXiv:2401.00001")
    assert _EVIDENCE_BY_ID["2401.00001"]["title"] in cards
    # A pre-#1636 build's whole message was just the name; that must not be
    # what a card degrades to.
    assert cards != "Momentum fusion"


def test_debate_cards_degrade_honestly_without_an_evidence_map():
    """No evidence map (a caller that never ran the proposer pool) ⇒ ids with
    no titles, and — critically — no claim can be attributed at all, rather
    than every claim being trusted."""
    cards = de._candidate_cards([_fake_proposal("A", ["2401.00001"])], {})
    assert "arXiv:2401.00001" in cards
    assert de._normalize_claim({"claim": "c", "arxiv_ids": ["2401.00001"]}, set())["arxiv_ids"] == []


async def test_no_backend_means_no_paper_verdicts_not_an_all_unused_table(monkeypatch, corpus):
    """Fail-soft, honestly (CLAUDE.md § fail-soft). With no LLM backend the
    debate never runs, so there is nothing to tally — and a table of
    all-`unused` rows would read as "the researchers saw 6 papers and engaged
    with none" rather than as the absence it is."""
    from archimedes.models.regime import Regime

    class _Down:
        available = False

    _patch_regime(monkeypatch, Regime.RISK_ON)
    monkeypatch.setattr(de.asyncio, "to_thread", _passthrough_to_thread)
    monkeypatch.setattr("archimedes.agents.strategy_fusion.load_corpus", lambda *a, **k: corpus)
    monkeypatch.setattr("archimedes.services.llm_backend.make_llm_backend", lambda model=None, **k: _Down())

    pool = [_fake_proposal("A", ["2401.00001", "2402.00001"])]

    async def _fake_propose_pool(brief, model, corpus_arg):
        return pool, _EVIDENCE_BY_ID

    monkeypatch.setattr(de, "_propose_pool", _fake_propose_pool)
    monkeypatch.setattr(
        "archimedes.services.fusion_evaluator.evaluate_fusion_spec",
        lambda spec, *, num_trials=None, **kw: _fake_ev(cagr=0.2, num_trials=num_trials),
    )

    board = await de._run_debate_leaderboard(
        candidate_id="cand_1", brief=GenerateBrief(intent="momentum equities", max_papers=4), emit=_FakeEmit()
    )
    assert board[0].debate_transcript is None
    assert board[0].debate_paper_verdicts is None
