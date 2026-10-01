"""Guards for the too-few-papers outcome: honest numbers, real ways forward.

The owner's screenshot: a run died with a single red line —

    Generation is unavailable right now: the corpus yielded <2 papers for this
    steer — the society cannot fuse.

— and nothing else. These tests pin the three things that line was missing:
the steer it was talking about, the count retrieval actually returned, and
suggestions derived from the corpus rather than invented.
"""

from __future__ import annotations

import pytest
from archimedes.agents.corpus_viability import (
    REASON_CORPUS_UNAVAILABLE,
    REASON_OK,
    REASON_TOO_FEW_PAPERS,
    assess_corpus_viability,
    suggest_steers,
)
from archimedes.agents.strategy_fusion import CorpusPaper
from archimedes.api.generate_schemas import GenerateBrief


def _paper(arxiv_id: str, title: str, abstract: str, category: str = "q-fin.PM") -> CorpusPaper:
    return CorpusPaper(
        arxiv_id=arxiv_id,
        title=title,
        abstract=abstract,
        primary_category=category,
        categories=(category,),
        published="2024-01-01",
    )


# A corpus that is entirely about crypto. A brief steered at "rates" retrieves
# ONE paper from it (the funding-rates one) — fewer than MIN_PAPERS, which is
# exactly the owner's screenshot: not zero, just not enough to cross-read.
_CRYPTO_CORPUS = [
    _paper("2401.0001", "Cross-sectional momentum in crypto markets", "bitcoin momentum and trend following signals"),
    _paper("2401.0002", "Time-series momentum for token portfolios", "trend following on a bitcoin and ether sleeve"),
    _paper("2401.0003", "Volatility-managed crypto portfolios", "volatility target sizing on a bitcoin sleeve"),
    _paper("2401.0004", "Defensive scaling of blockchain exposure", "volatility-managed defensive token allocation"),
    _paper("2401.0005", "Token carry and funding rates", "carry in perpetual crypto markets, defi funding rates"),
    _paper("2401.0006", "Carry portfolios in decentralised markets", "carry harvesting across blockchain venues"),
    _paper("2401.0007", "Breakout signals in blockchain assets", "breakout rules on token price series"),
    _paper("2401.0008", "Range break-out entries for crypto majors", "breakout thresholds on bitcoin daily bars"),
]


@pytest.fixture
def crypto_corpus(monkeypatch):
    """Point the module's retrieval at a corpus we control."""
    from archimedes.agents import strategy_fusion as sf

    monkeypatch.setattr(sf, "load_corpus", lambda *a, **k: list(_CRYPTO_CORPUS))
    return _CRYPTO_CORPUS


# ── The measured count, not an adjective ─────────────────────────────────────


def test_shortfall_reports_the_steer_and_the_count_it_measured(crypto_corpus):
    brief = GenerateBrief(
        intent="build a treasury ladder that beats cash over a two year horizon",
        risk_appetite="conservative",
        asset_classes=["rates"],
    )
    v = assess_corpus_viability(brief)

    assert v.reason_code == REASON_TOO_FEW_PAPERS, v
    assert v.candidates_found == 1, (
        f"the crypto-only corpus matches a rates steer on exactly one paper, got {v.candidates_found}"
    )
    assert v.candidates_found < v.min_papers
    assert v.min_papers == 2
    assert v.corpus_size == len(crypto_corpus)
    assert v.steer == brief.intent, "the steer must be reported verbatim, not summarised"
    assert not v.can_run


def test_a_viable_steer_reports_ok_and_no_suggestions(crypto_corpus):
    brief = GenerateBrief(intent="momentum on crypto majors", risk_appetite="aggressive", asset_classes=["crypto"])
    v = assess_corpus_viability(brief)

    assert v.reason_code == REASON_OK
    assert v.can_run
    assert v.candidates_found >= 2
    assert v.suggestions == [], "a run that CAN proceed must not be handed remedies it does not need"


# ── The message: honest about how retrieval works ────────────────────────────


def test_message_names_the_steer_the_count_and_the_floor(crypto_corpus):
    brief = GenerateBrief(
        intent="a treasury ladder that beats cash", risk_appetite="conservative", asset_classes=["rates"]
    )
    msg = assess_corpus_viability(brief).message()

    assert "a treasury ladder that beats cash" in msg, msg
    assert "matched 1 paper for your brief" in msg, msg
    assert "1 papers" not in msg, f"count is not pluralised honestly: {msg}"
    assert "at least 2" in msg, msg
    assert "before synthesis" in msg, msg


def test_message_never_claims_semantic_retrieval(crypto_corpus):
    """The candidate filter is a lowercased substring match. Say lexical, only.

    Prod has no embedding column and corpus_meta is empty — a message that
    said "semantic search found nothing" would be a false claim about our own
    machinery, and would send the user off to fix the wrong thing.
    """
    brief = GenerateBrief(
        intent="a treasury ladder that beats cash", risk_appetite="conservative", asset_classes=["rates"]
    )
    msg = assess_corpus_viability(brief).message().lower()

    assert "lexical" in msg
    for banned in ("semantic", "embedding", "vector search", "similarity search"):
        assert banned not in msg, f"message claims {banned!r}: {msg}"


# ── Suggestions: derived from THIS corpus, no LLM ────────────────────────────


def test_suggestions_are_backed_by_papers_actually_in_the_corpus(crypto_corpus):
    brief = GenerateBrief(
        intent="a treasury ladder that beats cash", risk_appetite="conservative", asset_classes=["rates"]
    )
    sugg = assess_corpus_viability(brief).suggestions

    assert sugg, "a corpus with papers in it must yield at least one way forward"
    assert len(sugg) <= 3
    terms = {s.term for s in sugg}
    assert "crypto" in terms, f"the corpus is all crypto; that must be the headline suggestion: {terms}"
    for s in sugg:
        assert s.kind == "asset_class"
        assert s.papers >= 2, f"{s.term} was offered on {s.papers} papers — below the fusion floor"
        assert s.papers <= len(crypto_corpus), (
            f"{s.term} claims {s.papers} papers in a {len(crypto_corpus)}-paper corpus"
        )


def test_every_suggestion_is_a_term_retrieval_can_actually_act_on(crypto_corpus):
    """A chip that cannot move ``candidates_found`` is not a way forward.

    ``select_candidates`` fixes candidate MEMBERSHIP on asset-class terms only
    (``_asset_terms(brief.asset_classes)``); the brief's free text reaches the
    ranking ``score()`` and nothing else. So the suggestion vocabulary has to
    be exactly the vocabulary that filter reads. Offer a mechanism here —
    "breakout · 2 papers", under a heading that says broadening works — and the
    user pays another failed run to discover the chip could never have helped.
    """
    from archimedes.agents.strategy_fusion import _ASSET_SYNONYMS

    brief = GenerateBrief(
        intent="a treasury ladder that beats cash", risk_appetite="conservative", asset_classes=["rates"]
    )
    sugg = assess_corpus_viability(brief).suggestions

    assert sugg, "the crypto corpus has broadening terms to offer"
    for s in sugg:
        assert s.kind == "asset_class", f"{s.term} is offered on an axis the candidate filter never reads"
        assert s.term in _ASSET_SYNONYMS, f"{s.term} is not a term _asset_terms() can filter candidates on"


# A corpus deliberately too thin to fill three slots: only "crypto" clears
# MIN_PAPERS. "commodities" matches exactly one paper ("gold"), so it is the
# term a floor-less ranker would reach for to pad the list — and the one that
# must never be offered.
_THIN_CORPUS = [
    _paper(
        "2402.0001",
        "Bitcoin trend following",
        "bitcoin trend following with a gold hedge",
        category="q-fin.CP",
    ),
    _paper("2402.0002", "Token trend following", "token trend following rules", category="q-fin.CP"),
]


def test_a_suggestion_below_the_fusion_floor_is_never_offered_to_pad_the_list():
    """Padding to three with a one-paper term recommends a steer that would
    fail for exactly the same reason the user just hit."""
    sugg = suggest_steers(_THIN_CORPUS, steer_text="a treasury ladder", min_papers=2)

    assert [s.term for s in sugg] == ["crypto"], sugg
    assert len(sugg) < 3, "a thin corpus must return fewer suggestions, not weaker ones"
    for s in sugg:
        assert s.papers >= 2, f"{s.term} offered on {s.papers} paper(s) — below the floor it must clear"
    assert "commodities" not in {s.term for s in sugg}


def test_suggestions_never_repeat_what_the_brief_already_says(crypto_corpus):
    """Telling a user who wrote "crypto momentum" to try crypto is not a way forward."""
    sugg = suggest_steers(_CRYPTO_CORPUS, steer_text="crypto momentum sleeve", min_papers=2)
    terms = {s.term for s in sugg}
    assert "crypto" not in terms, terms


def test_suggestions_are_deterministic(crypto_corpus):
    a = suggest_steers(_CRYPTO_CORPUS, steer_text="rates ladder", min_papers=2)
    b = suggest_steers(_CRYPTO_CORPUS, steer_text="rates ladder", min_papers=2)
    assert [s.as_dict() for s in a] == [s.as_dict() for s in b]


# ── Fail-closed: no corpus, no false remedy ──────────────────────────────────


def test_empty_corpus_says_so_and_offers_nothing_to_broaden(monkeypatch):
    """With an empty corpus, "broaden your brief" would be advice with nothing behind it."""
    from archimedes.agents import strategy_fusion as sf

    monkeypatch.setattr(sf, "load_corpus", lambda *a, **k: [])
    brief = GenerateBrief(intent="a treasury ladder that beats cash", risk_appetite="conservative")
    v = assess_corpus_viability(brief)

    assert v.reason_code == REASON_CORPUS_UNAVAILABLE
    assert v.suggestions == []
    assert "corpus is unavailable" in v.message()
    assert "Nothing you change in the brief will help" in v.message()


def test_retrieval_failure_degrades_to_corpus_unavailable(monkeypatch):
    from archimedes.agents import strategy_fusion as sf

    def _boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(sf, "load_corpus", _boom)
    v = assess_corpus_viability(GenerateBrief(intent="a treasury ladder that beats cash", risk_appetite="moderate"))

    assert v.reason_code == REASON_CORPUS_UNAVAILABLE
    assert v.candidates_found == 0
    assert not v.can_run


# ── Drift guards ─────────────────────────────────────────────────────────────


def test_min_papers_fallback_matches_the_enforced_floor():
    """The literal used when strategy_fusion cannot be imported must not drift.

    It is the number the message quotes as "needs at least N"; a stale copy
    would state a floor the pipeline does not enforce.
    """
    from archimedes.agents.corpus_viability import _MIN_PAPERS_FALLBACK
    from archimedes.agents.strategy_fusion import MIN_PAPERS

    assert _MIN_PAPERS_FALLBACK == MIN_PAPERS


def test_debate_can_run_delegates_to_the_same_assessment(crypto_corpus):
    """The gate and the explanation must be one retrieval, not two implementations."""
    from archimedes.agents.debate_engine import _debate_can_run

    viable = GenerateBrief(intent="momentum on crypto majors", risk_appetite="aggressive", asset_classes=["crypto"])
    thin = GenerateBrief(
        intent="a treasury ladder that beats cash", risk_appetite="conservative", asset_classes=["rates"]
    )

    assert _debate_can_run(viable) is True
    assert _debate_can_run(thin) is False
    assert _debate_can_run(viable) is assess_corpus_viability(viable).can_run
    assert _debate_can_run(thin) is assess_corpus_viability(thin).can_run
