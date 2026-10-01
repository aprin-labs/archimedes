"""Multi-paper utilization is checkable, not self-reported (#1739).

Before this, a citation list was a claim with nothing behind it: a single-
``sma_200`` crossover that named five papers passed ``is_actionable``,
``_dsl_conformance_ok`` and ``validate_strategy_spec`` alike, and was
hash-identical to the same spec citing one unrelated paper. Citation count
reads downstream as evidence depth, so "5 papers" laundered a one-mechanism
strategy — the exact move #1636 called "strictly worse than an honest 2".

What is asserted here is the mapping, end to end:

* ``paper_mechanisms`` ties each CITED id to a mechanism and to indicator
  aliases that literally appear in the validated spec's entry/exit conditions.
  An id the model invented is dropped (the ``valid_ids`` filter's shape); a
  spec element the spec does not use is dropped (``strategy_dsl``'s
  ``parameter_variants`` check, same ``all_indicators`` set).
* ``distinct_mechanism_papers`` LABELS the result. It is never a gate: the
  five-papers/one-mechanism spec still ships, it just says "1".
* ``papers_offered`` / ``distinct_mechanism_papers`` / ``fusion_reasoning``
  survive onto the candidate, into ``source_papers`` (with real titles), and
  into the persisted ``debate_transcripts`` row — the four values that were
  computed and then dropped.

Hermetic: no ``.env``, no network, no Redis, no Docker. The corpus is built
in-test, every LLM call is a stub, and the DB is tmp-file sqlite (fixture
copied from ``test_generated_citation_truth.py``).
"""

from __future__ import annotations

import json
import logging

import archimedes.agents.debate_engine as de
import archimedes.db as db
import pytest
from archimedes.agents.generation_pipeline import (
    _CandidateResult,
    _persist_candidate,
    _persist_debate_transcripts,
)
from archimedes.agents.strategy_fusion import CorpusPaper, FusionBrief, StrategyFusion
from archimedes.api.generate_schemas import GenerateBrief
from archimedes.api.strategies_routes import _resolve_source_papers
from archimedes.models.debate_transcript import DebateTranscriptRecord
from archimedes.models.strategy_passport_record import StrategyPassportRecord

# ── Fixture corpus: five real-shaped papers, distinct mechanisms ─────────────

_PAPERS = [
    CorpusPaper(
        arxiv_id="2401.00001",
        title="Cross-Sectional Equity Momentum with Regime Conditioning",
        abstract="A regime-switching overlay on cross-sectional equity momentum.",
        primary_category="q-fin.PM",
        categories=("q-fin.PM",),
        published="2024-01-05",
    ),
    CorpusPaper(
        arxiv_id="2402.00002",
        title="Treasury Yield Curve Carry and Macro Regimes",
        abstract="A carry strategy on the treasury yield curve conditioned on macro states.",
        primary_category="q-fin.PM",
        categories=("q-fin.PM",),
        published="2024-02-10",
    ),
    CorpusPaper(
        arxiv_id="2403.00003",
        title="Implied Volatility Surface Dynamics for Index Options",
        abstract="Modelling implied volatility surface dynamics and variance risk premia.",
        primary_category="q-fin.PR",
        categories=("q-fin.PR",),
        published="2024-03-15",
    ),
    CorpusPaper(
        arxiv_id="2404.00004",
        title="Trend Following Across Asset Classes",
        abstract="Time-series momentum and moving-average crossovers across asset classes.",
        primary_category="q-fin.PM",
        categories=("q-fin.PM",),
        published="2024-04-20",
    ),
    CorpusPaper(
        arxiv_id="2405.00005",
        title="Low-Volatility Anomalies in Equity Portfolios",
        abstract="Realized-volatility sorting and the low-beta anomaly in equity portfolios.",
        primary_category="q-fin.PM",
        categories=("q-fin.PM",),
        published="2024-05-25",
    ),
]

_ALL_IDS = [p.arxiv_id for p in _PAPERS]

# The adversarial spec from #1739: ONE mechanism (sma_200) in entry AND exit.
# Anything a paper_mechanisms entry names other than "sma_200" is not part of
# what this strategy trades.
#
# ``indicators`` carries a DECOY. ``validate_strategy_spec`` ignores the key the
# model emits and rebuilds the list from the entry/exit conditions
# (``indicators=sorted(all_indicators)``), so a spec can DECLARE an alias it
# never trades — which is the same self-report problem, one field over. The
# decoy is what makes the spec_elements check falsifiable: validating against
# the model's declared list instead of the derived one would let
# ``bollinger_20`` through, and the test below would still be green without it.
_SINGLE_MECHANISM_SPEC = {
    "name": "Single-mechanism crossover",
    "asset_universe": ["SPY"],
    "rebalance_frequency": "monthly",
    "entry": {"gt": ["close", "sma_200"]},
    "exit": {"lt": ["close", "sma_200"]},
    "position_sizing": {"type": "full_invested_when_in_market"},
    "source_arxiv_ids": list(_ALL_IDS),
    "indicators": ["sma_200", "bollinger_20"],
}


class _MechanismBackend:
    """A stub proposer that cites all five papers and maps ``mechanisms`` of them.

    ``model_id`` / ``served_model`` differ so the true-model honesty rule keeps
    holding on this path too.
    """

    model_id = "claude-sonnet-4-20250514"
    served_model = "glm-4.7"

    def __init__(self, mechanisms: list[dict], *, source_ids: list[str] | None = None) -> None:
        self._mechanisms = mechanisms
        self._source_ids = list(source_ids if source_ids is not None else _ALL_IDS)

    def complete(self, system: str, user: str) -> str:
        # The prompt's paper_mechanisms schema + the omit-don't-pad rule are
        # asserted by test_the_prompts_carry_the_mechanism_map_contract below;
        # this stub only has to answer in the proposal's output shape.
        return json.dumps(
            {
                "strategy_name": "Single-mechanism crossover",
                "thesis": "Pre-backtest hypothesis; empirical validation pending.",
                "source_arxiv_ids": self._source_ids,
                "fusion_reasoning": "Each cited paper is discussed by name.",
                "novelty_rationale": "The combination is unpublished.",
                "risk_notes": "Pre-backtest; the rigor gate still applies.",
                "paper_mechanisms": self._mechanisms,
                "strategy_spec": {**_SINGLE_MECHANISM_SPEC, "source_arxiv_ids": list(self._source_ids)},
            }
        )


def _propose(monkeypatch, mechanisms, *, source_ids=None):
    monkeypatch.delenv("ARCHIMEDES_CORPUS_MANIFEST", raising=False)
    svc = StrategyFusion(
        backend=_MechanismBackend(mechanisms, source_ids=source_ids),
        corpus=list(_PAPERS),
        candidates=list(_PAPERS),  # used verbatim — papers_offered == 5
    )
    return svc.propose(FusionBrief(asset_classes=[]))


# ── DB fixture (copied from test_generated_citation_truth.py:41-53) ──────────


@pytest.fixture
def _use_tmp_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    url = f"sqlite:///{tmp_path / 'utilization.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    eng = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", eng)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=eng, autocommit=False, autoflush=False))
    db.init_db()
    yield


# ── 1. The mapping ──────────────────────────────────────────────────────────


def test_a_mechanism_for_a_paper_the_proposal_does_not_cite_is_dropped(monkeypatch):
    """The id half of the map is anti-hallucination, same shape as ``valid_ids``.

    An entry naming a paper the proposal did not cite — a real corpus paper it
    left out, or an id that exists nowhere — is dropped, never repaired into
    the citation list. Without this, a model could claim mechanisms for papers
    it never fused and the count would credit them. MUTATION: delete the
    ``cited_ids`` filter in ``StrategyFusion.propose`` — ``2403.00003`` survives
    and the count reads 2.
    """
    mechanisms = [
        {"arxiv_id": "2401.00001", "mechanism": "trend filter", "spec_elements": ["sma_200"]},
        # A real corpus paper this proposal does NOT cite.
        {"arxiv_id": "2403.00003", "mechanism": "vol band", "spec_elements": ["sma_200"]},
        # An id that exists nowhere.
        {"arxiv_id": "2499.99999", "mechanism": "invented", "spec_elements": ["sma_200"]},
    ]

    proposal = _propose(monkeypatch, mechanisms, source_ids=["2401.00001", "2402.00002"])

    assert proposal.status == "ok"
    assert proposal.source_arxiv_ids == ["2401.00001", "2402.00002"], "the citation list is not padded from the map"
    assert [e["arxiv_id"] for e in proposal.paper_mechanisms] == ["2401.00001"]
    assert proposal.distinct_mechanism_papers == 1


def test_the_prompts_carry_the_mechanism_map_contract():
    """Scope item 1's first half lives in prompt text, so it is pinned here.

    The proposer prompt must ask for ``paper_mechanisms`` with ``spec_elements``
    and state the omit-don't-pad rule; the spec-repair prompt (which emits only
    the spec) must say ``paper_mechanisms`` is NOT a spec key, and must not ask
    for the map itself — a repair that re-emitted the map would be the model
    self-reporting twice.
    """
    from archimedes.agents.strategy_fusion import _SPEC_CONTRACT, _SPEC_REPAIR_SYSTEM, _SYSTEM_PROMPT

    assert '"paper_mechanisms": [' in _SYSTEM_PROMPT
    assert '"spec_elements": [' in _SYSTEM_PROMPT
    assert "OMIT that id from `source_arxiv_ids`" in _SYSTEM_PROMPT
    assert "paper_mechanisms is a TOP-LEVEL field of the proposal, NOT a key inside strategy_spec" in _SPEC_CONTRACT
    assert _SPEC_CONTRACT in _SPEC_REPAIR_SYSTEM
    assert '"paper_mechanisms": [' not in _SPEC_REPAIR_SYSTEM


def test_unattributed_citation_does_not_count_as_a_mechanism_paper(monkeypatch, caplog):
    """Citing five papers while naming spec elements for two is a 2, not a 5.

    The citation list is NOT trimmed — #1636's rule is that an unattributable
    paper is recorded as unattributed, never deleted (the debate's
    keep-the-claim/strip-the-id pattern). The honest count travels beside it,
    and the shortfall leaves a log line so it is auditable without re-reading
    the proposal.
    """
    caplog.set_level(logging.WARNING, logger="archimedes.agents.strategy_fusion")
    mechanisms = [
        {"arxiv_id": "2401.00001", "mechanism": "trend filter", "spec_elements": ["sma_200"]},
        {"arxiv_id": "2404.00004", "mechanism": "MA crossover", "spec_elements": ["sma_200"]},
        # Cited, but the model could not tie them to anything this spec trades.
        {"arxiv_id": "2402.00002", "mechanism": "carry", "spec_elements": []},
        {"arxiv_id": "2403.00003", "mechanism": "vol surface", "spec_elements": []},
        {"arxiv_id": "2405.00005", "mechanism": "low-vol anomaly", "spec_elements": []},
    ]

    proposal = _propose(monkeypatch, mechanisms)

    assert proposal.status == "ok"
    assert len(proposal.source_arxiv_ids) == 5, "the claim is kept, not trimmed"
    assert proposal.distinct_mechanism_papers == 2
    # The three unattributed papers are still ON the record, with their claim.
    unattributed = [e for e in proposal.paper_mechanisms if not e["spec_elements"]]
    assert {e["arxiv_id"] for e in unattributed} == {"2402.00002", "2403.00003", "2405.00005"}
    assert all(e["mechanism"] for e in unattributed), "the claim survives; only the unsupported link is stripped"

    lines = [r.getMessage() for r in caplog.records if "attribution" in r.getMessage()]
    assert lines, "an under-attributed citation list must leave a WARNING"
    assert "2 of 5" in lines[0]


def test_spec_element_outside_the_spec_is_dropped(monkeypatch):
    """``bollinger_20`` is not in entry/exit, so a paper "attributed" to it is not.

    Mirrors ``strategy_dsl.py:269-274``, where a ``parameter_variants`` key
    must reference an indicator alias the conditions actually use — same
    ``all_indicators`` set, different key. Without it, ``spec_elements`` is
    free text and the mapping proves nothing.

    The alias set has to be the DERIVED one (``StrategySpec.indicators``, built
    from entry/exit) and not the ``indicators`` list the model wrote: the
    fixture declares ``bollinger_20`` there and trades it nowhere, so checking
    the declared list would validate one self-report against another.
    """
    mechanisms = [
        {"arxiv_id": "2401.00001", "mechanism": "trend filter", "spec_elements": ["sma_200"]},
        {"arxiv_id": "2403.00003", "mechanism": "vol band", "spec_elements": ["bollinger_20"]},
    ]

    proposal = _propose(monkeypatch, mechanisms)

    by_id = {e["arxiv_id"]: e for e in proposal.paper_mechanisms}
    assert by_id["2403.00003"]["spec_elements"] == [], "an alias the spec never uses must not survive"
    assert by_id["2403.00003"]["mechanism"] == "vol band", "the claim itself is kept, unlaundered"
    assert by_id["2401.00001"]["spec_elements"] == ["sma_200"]
    # It contributed nothing: only the sma_200 paper counts.
    assert proposal.distinct_mechanism_papers == 1


def test_single_mechanism_spec_claiming_five_papers_is_labelled_not_hidden(monkeypatch):
    """#1739's adversarial case. It SHIPS — and it says "1".

    ``distinct_mechanism_papers`` is deliberately not a gate: converting it into
    a reject would fail generation instead of improving it, which #1636 ruled
    out. An honest single-mechanism strategy is a legitimate outcome; a
    single-mechanism strategy wearing five citations as evidence depth is not.
    """
    mechanisms = [
        {"arxiv_id": "2401.00001", "mechanism": "200-day trend filter", "spec_elements": ["sma_200"]},
        {"arxiv_id": "2402.00002", "mechanism": "carry", "spec_elements": []},
        {"arxiv_id": "2403.00003", "mechanism": "variance risk premium", "spec_elements": []},
        {"arxiv_id": "2404.00004", "mechanism": "time-series momentum", "spec_elements": []},
        {"arxiv_id": "2405.00005", "mechanism": "low-vol anomaly", "spec_elements": []},
    ]

    proposal = _propose(monkeypatch, mechanisms)

    assert proposal.is_actionable is True, "labelled, never rejected (#1636's honest-shortfall rule)"
    assert len(proposal.source_arxiv_ids) == 5
    assert proposal.distinct_mechanism_papers == 1
    assert proposal.papers_offered == 5


# ── 2. Survival onto the candidate ──────────────────────────────────────────


def test_papers_offered_and_mechanism_count_survive_to_the_candidate():
    """``_make_entry`` used to drop all three, plus the paper titles.

    ``papers_offered`` was computed and logged, ``fusion_reasoning`` was folded
    into ``reasoning`` (which falls back to ``novelty_rationale``, so it could
    not be read as per-paper prose), and ``title`` was the literal ``""``.
    """
    from types import SimpleNamespace

    from tests.test_debate_engine import _fake_ev

    proposal = SimpleNamespace(
        strategy_name="Single-mechanism crossover",
        thesis="thesis",
        source_arxiv_ids=["2401.00001", "2404.00004", "2402.00002"],
        strategy_spec=dict(_SINGLE_MECHANISM_SPEC),
        fusion_reasoning="Paper A gives the trend filter; paper D gives the crossover period.",
        novelty_rationale="novelty",
        is_actionable=True,
        papers_offered=30,
        distinct_mechanism_papers=2,
        paper_mechanisms=[
            {"arxiv_id": "2401.00001", "mechanism": "200-day trend filter", "spec_elements": ["sma_200"]},
            {"arxiv_id": "2404.00004", "mechanism": "crossover period", "spec_elements": ["sma_200"]},
            {"arxiv_id": "2402.00002", "mechanism": "carry", "spec_elements": []},
        ],
    )
    evidence_by_id = {p.arxiv_id: {"title": p.title, "published": p.published} for p in _PAPERS}

    entry = de._make_entry(
        "cand_1",
        proposal,
        _fake_ev(cagr=0.2),
        regime="neutral",
        evidence_by_id=evidence_by_id,
    )

    assert entry.papers_offered == 30
    assert entry.distinct_mechanism_papers == 2
    assert entry.fusion_reasoning, "the per-paper mechanism prose must reach the candidate"
    # …and the titles the evidence map already knew.
    titles = {p["arxiv_id"]: p["title"] for p in entry.source_papers}
    assert titles["2401.00001"] == "Cross-Sectional Equity Momentum with Regime Conditioning"
    assert all(p["title"] for p in entry.source_papers), 'no citation ships with the literal ""'
    mechanisms = {p["arxiv_id"]: p for p in entry.source_papers}
    assert mechanisms["2404.00004"]["spec_elements"] == ["sma_200"]
    assert mechanisms["2402.00002"]["spec_elements"] == [], "cited but unattributed, and it says so"


# ── 3. Durability ───────────────────────────────────────────────────────────


async def test_persisted_source_papers_carry_title_and_mechanism(_use_tmp_db):
    """The enriched dicts ride the EXISTING ``source_papers`` JSON column.

    No migration and no route change: ``upsert_strategy`` serializes whatever
    dicts it is handed, and ``_resolve_source_papers`` does ``dict(paper)``, so
    the extra keys reach the API response unchanged.
    """
    from archimedes.models.strategy_store import StrategyRecord

    candidate = _CandidateResult(
        candidate_id="cand_1",
        strategy_name="Single-mechanism crossover",
        thesis="thesis",
        asset_universe=["SPY"],
        source_papers=[
            {
                "arxiv_id": "2401.00001",
                "title": "Cross-Sectional Equity Momentum with Regime Conditioning",
                "mechanism": "200-day trend filter",
                "spec_elements": ["sma_200"],
            },
            {
                "arxiv_id": "2402.00002",
                "title": "Treasury Yield Curve Carry and Macro Regimes",
                "mechanism": "carry",
                "spec_elements": [],
            },
        ],
        weights={},
        reasoning="r",
        rigor_verdict={"passing": True, "dsr": 1.5},
        passes_rigor=True,
        generation_method="debate",
        source_arxiv_ids=["2401.00001", "2402.00002"],
        distinct_mechanism_papers=1,
        papers_offered=5,
        fusion_reasoning="Paper A gives the trend filter.",
    )

    strategy_id, _trace = await _persist_candidate(candidate, GenerateBrief(intent="momentum equities"))

    with db.get_session() as session:
        record = session.get(StrategyRecord, strategy_id)
        stored = json.loads(record.source_papers)

    assert len(stored) == 2
    for paper in stored:
        assert paper["title"], "a persisted citation must carry the real paper title"
        assert "mechanism" in paper, "the paper→mechanism link must be durable, not request-scoped"

    # The read path preserves both — nothing between the store and the API
    # response strips the extra keys.
    resolved = _resolve_source_papers(stored, {})
    assert [p["mechanism"] for p in resolved] == ["200-day trend filter", "carry"]
    assert all(p["resolved_title"] for p in resolved)
    assert resolved[0]["spec_elements"] == ["sma_200"]

    # …and the PASSPORT, which is what GET /strategies/{id} actually serves —
    # `strategy_passports.paper_refs`, not `StrategyRecord.source_papers`. The
    # debate panel that carries the attribution summary is owner-only, so this
    # is the only path on which a public reader sees the link at all.
    # `contribution` is the column StrategyPassport.jsx renders; it had no
    # writer before this, so every generated row showed an em-dash.
    with db.get_session() as session:
        passport = session.get(StrategyPassportRecord, strategy_id)
        refs = {r.arxiv_id: r for r in passport.paper_refs}

    assert refs["2401.00001"].title == "Cross-Sectional Equity Momentum with Regime Conditioning"
    assert refs["2401.00001"].contribution == "200-day trend filter"
    # The unattributed citation keeps its row and its title, and its
    # contribution stays NULL — the em-dash IS the honest record. Writing the
    # unvalidated mechanism prose there would launder it one column over.
    assert refs["2402.00002"].title == "Treasury Yield Curve Carry and Macro Regimes"
    assert refs["2402.00002"].contribution is None


async def test_debate_paper_verdicts_are_durable(_use_tmp_db):
    """The per-paper tally outlives the request.

    It was computed for free off a transcript we already paid for and then
    thrown away — "in-process + SSE-visible only" — so a run that put 30 papers
    in front of the proposers left no record of which ones it engaged with.
    It now rides the same ``transcript_json`` list the turns ride: no
    migration, no new table. ``unused`` rows are the point, not noise — they
    are what makes "retrieved 5, argued over 2" readable.
    """
    verdicts = [
        {
            "arxiv_id": "2401.00001",
            "title": "Cross-Sectional Equity Momentum with Regime Conditioning",
            "cited_by": ["bull", "bear"],
            "citations": 4,
            "discarded_by": [],
            "discard_reasons": [],
            "verdict": "cited",
        },
        {
            "arxiv_id": "2402.00002",
            "title": "Treasury Yield Curve Carry and Macro Regimes",
            "cited_by": [],
            "citations": 0,
            "discarded_by": ["bear"],
            "discard_reasons": ["no distinct mechanism"],
            "verdict": "discarded",
        },
        *(
            {
                "arxiv_id": p.arxiv_id,
                "title": p.title,
                "cited_by": [],
                "citations": 0,
                "discarded_by": [],
                "discard_reasons": [],
                "verdict": "unused",
            }
            for p in _PAPERS[2:]
        ),
    ]
    candidate = _CandidateResult(
        candidate_id="cand_1",
        strategy_name="Single-mechanism crossover",
        thesis="thesis",
        asset_universe=["SPY"],
        source_papers=[],
        weights={},
        reasoning="r",
        rigor_verdict={"passing": True},
        passes_rigor=True,
        generation_method="debate",
        source_arxiv_ids=["2401.00001", "2402.00002"],
        distinct_mechanism_papers=1,
        papers_offered=5,
        fusion_reasoning="Paper A gives the trend filter; paper B contributes no distinct mechanism.",
        debate_transcript=[{"role": "bull", "round": 1, "verdict": "act", "claims": ["c1"]}],
        debate_paper_verdicts=verdicts,
    )

    await _persist_debate_transcripts(job_id="job-1", candidates=[candidate], strategy_ids={"cand_1": "strat-abc"})

    with db.get_session() as session:
        row = session.query(DebateTranscriptRecord).filter_by(generation_id="job-1").one()
        stored = json.loads(row.transcript_json)

    # The bull turn is untouched; the tally rides alongside it.
    assert stored[0]["role"] == "bull"
    attribution = [t for t in stored if t.get("paper_verdicts") is not None]
    assert len(attribution) == 1, "exactly one attribution record per row"
    persisted = attribution[0]["paper_verdicts"]

    assert {v["arxiv_id"] for v in persisted} == set(_ALL_IDS), "every RETRIEVED paper, not just the cited ones"
    by_id = {v["arxiv_id"]: v for v in persisted}
    assert by_id["2401.00001"]["verdict"] == "cited"
    assert by_id["2402.00002"]["discard_reasons"] == ["no distinct mechanism"]
    assert [v["arxiv_id"] for v in persisted if v["verdict"] == "unused"] == _ALL_IDS[2:]
    assert attribution[0]["fusion_reasoning"], "the per-paper prose is durable too"


async def test_persisted_paper_verdicts_do_not_leak_internal_jargon(_use_tmp_db):
    """Making the tally durable must not walk model prose past the sanitizer.

    ``debate_transcript``'s contract is that sanitization happens at WRITE
    time, so "a raw read of ``transcript_json`` is already safe". The tally's
    ``discard_reasons`` are copied by ``_aggregate_paper_verdicts`` out of the
    RAW ``_debate_round`` turns — the same sentences that reach this table as
    ``discard[].reason``, which ``_sanitize_claim`` scrubs. Persisting the
    tally under a key the sanitizer did not know about would re-open that leak
    by a shape change: precisely the regression ``_sanitize_claim`` was written
    for after claims went from strings to dicts.

    Titles are the deliberate exception and are asserted here too: they are
    corpus metadata, not model text, and blanket-scrubbing them would mangle a
    real paper ("Phase-Locked …" → "[redacted] …"), which is the same reason
    validated arXiv ids are left alone.
    """
    leak = "rejected: blocked on the T3.5 cutover until Phase-4 lands"
    candidate = _CandidateResult(
        candidate_id="cand_1",
        strategy_name="Single-mechanism crossover",
        thesis="thesis",
        asset_universe=["SPY"],
        source_papers=[],
        weights={},
        reasoning="r",
        rigor_verdict={"passing": True},
        passes_rigor=True,
        generation_method="debate",
        source_arxiv_ids=["2401.00001"],
        distinct_mechanism_papers=1,
        papers_offered=5,
        fusion_reasoning="Paper A gives the trend filter; the carry leg is deferred to the T3.5 cutover.",
        # The identical sentence on BOTH paths: the turn's discard reason (long
        # sanitized) and the tally row's discard_reasons (the new key).
        debate_transcript=[
            {
                "role": "bear",
                "round": 1,
                "verdict": "hold",
                "claims": [],
                "discard": [{"arxiv_id": "2402.00002", "reason": leak}],
            }
        ],
        debate_paper_verdicts=[
            {
                "arxiv_id": "2402.00002",
                "title": "Phase-Locked Cycles in Commodity Futures",
                "cited_by": [],
                "citations": 0,
                "discarded_by": ["bear"],
                "discard_reasons": [leak],
                "verdict": "discarded",
            }
        ],
    )

    await _persist_debate_transcripts(job_id="job-1", candidates=[candidate], strategy_ids={"cand_1": "strat-abc"})

    with db.get_session() as session:
        row = session.query(DebateTranscriptRecord).filter_by(generation_id="job-1").one()
        raw = row.transcript_json

    assert "T3.5" not in raw, "a raw read of transcript_json must already be safe (module docstring)"
    assert "cutover" not in raw.lower()
    assert "Phase-4" not in raw

    stored = json.loads(raw)
    attribution = next(t for t in stored if t.get("paper_verdicts") is not None)
    tally = attribution["paper_verdicts"][0]
    # Scrubbed, not dropped — the discard is still on the record, and so is the
    # id that carries its provenance.
    assert tally["discard_reasons"] == ["rejected: blocked on the [redacted] [redacted] until [redacted] lands"]
    assert tally["arxiv_id"] == "2402.00002"
    assert tally["verdict"] == "discarded"
    # The corpus title is untouched: scrubbing it would redact a real paper.
    assert tally["title"] == "Phase-Locked Cycles in Commodity Futures"
