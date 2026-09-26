"""Corpus viability for a generation steer — and the honest ways forward.

Why this module exists
----------------------
``debate_engine._debate_can_run`` answered a single bool: can the society run?
When it said *no* because the corpus had fewer than ``MIN_PAPERS`` candidates
for the steer, ``generation_pipeline`` emitted one red line —

    Generation is unavailable right now: the corpus yielded <2 papers for this
    steer — the society cannot fuse.

— and stopped. That line is true but useless: it never says which steer, never
says how many papers were actually found, and offers nothing to do next.

This module turns that dead end into a first-class outcome. It runs the SAME
retrieval the pipeline runs (``strategy_fusion.select_candidates`` over
``load_corpus``), reports the count it actually measured, and derives concrete
broadening suggestions from the corpus itself — by counting, for each asset
class in our vocabulary, how many corpus papers the lexical haystack filter
matches. **No LLM call**, no second retrieval stack, no invented numbers:
every ``papers`` count in a suggestion is a count this function performed over
the loaded corpus.

Why asset classes are the only axis offered
-------------------------------------------
``select_candidates`` fixes candidate MEMBERSHIP on asset-class terms alone
(``strategy_fusion.select_candidates_scored``: ``terms = _asset_terms(brief.
asset_classes)`` then ``filtered = [p for p in corpus if any(t in p.haystack
for t in terms)]``). The brief's free text reaches only the ranking ``score()``
— it re-orders a set the asset classes already fixed, and never widens it. So a
mechanism term ("breakout", "carry") dropped into the brief box CANNOT change
``candidates_found``: offering one as a way past this wall would be advice with
nothing behind it, and it would cost the user another failed run to find out.
Mechanisms still steer the debate society's proposals — they are simply not a
remedy for a retrieval shortfall, so this card does not offer them.

Claim discipline
----------------
The candidate filter in ``select_candidates`` is a lowercased substring match
over ``primary_category + categories + title + abstract`` (``CorpusPaper.
haystack``). It is LEXICAL. There is a semantic-rerank seam behind it
(``paper_rag.augment_candidate_scores``) but it only re-orders a set the
keyword filter already fixed, and it returns a uniform sentinel when semantic
retrieval is off. So everything this module says about retrieval says
"keyword (lexical)" and nothing says "semantic" — see ``RETRIEVAL_KIND``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from archimedes.api.generate_schemas import GenerateBrief

logger = logging.getLogger(__name__)

# The one word we are allowed to use for how candidate papers are found.
RETRIEVAL_KIND = "lexical"

# Machine reason codes. `code` on the SSE error event stays
# GENERATION_UNAVAILABLE (unchanged wire contract); `reason_code` is the
# finer-grained machine reason the UI branches on.
REASON_OK = "OK"
REASON_TOO_FEW_PAPERS = "CORPUS_TOO_FEW_PAPERS"
REASON_CORPUS_UNAVAILABLE = "CORPUS_UNAVAILABLE"

# Reported as the floor ONLY when strategy_fusion could not be imported at all
# — the one path where the real ``MIN_PAPERS`` is out of reach. Pinned equal to
# it by ``test_corpus_viability.py`` so it cannot drift into reporting a floor
# the pipeline does not enforce. (Same pattern as ``generate_schemas.
# _MAX_PAPERS_FLOOR``.)
_MIN_PAPERS_FALLBACK = 2


@dataclass(frozen=True)
class SteerSuggestion:
    """One concrete broadening move, with the corpus evidence behind it.

    ``papers`` is a measured count over the loaded corpus — how many papers the
    lexical haystack filter matches for ``term`` — never an estimate.
    """

    term: str
    # Always "asset_class" — the only axis that changes what retrieval finds
    # (see "Why asset classes are the only axis offered" above). Kept on the
    # wire so a second axis, if it ever earns one, arrives labelled.
    kind: str
    papers: int

    def as_dict(self) -> dict[str, Any]:
        return {"term": self.term, "kind": self.kind, "papers": self.papers}


@dataclass(frozen=True)
class CorpusViability:
    """What retrieval actually found for this steer, and what to do about it."""

    steer: str
    asset_classes: list[str]
    corpus_size: int
    candidates_found: int
    min_papers: int
    reason_code: str
    suggestions: list[SteerSuggestion] = field(default_factory=list)
    retrieval: str = RETRIEVAL_KIND

    @property
    def can_run(self) -> bool:
        return self.reason_code == REASON_OK

    def message(self) -> str:
        """The user-facing sentence. Names the steer, the count, the floor.

        Never claims semantic search, never claims a cause it did not measure.
        """
        if self.reason_code == REASON_CORPUS_UNAVAILABLE:
            return (
                "Generation stopped before synthesis: the paper corpus is unavailable right now, "
                "so there was nothing to retrieve for this brief. Nothing you change in the brief "
                "will help until the corpus is back."
            )
        found = self.candidates_found
        return (
            f"Generation stopped before synthesis: keyword ({self.retrieval}) retrieval over the paper "
            f"corpus matched {found} paper{'' if found == 1 else 's'} for your brief — "
            f"{_quote(self.steer)} — and fusing a strategy needs at least {self.min_papers}, "
            "so no strategy was drafted or saved."
        )

    def as_event_fields(self) -> dict[str, Any]:
        """The structured payload the SSE ``error`` event carries."""
        return {
            "reason_code": self.reason_code,
            "steer": self.steer,
            "asset_classes": list(self.asset_classes),
            "retrieval": self.retrieval,
            "candidates_found": self.candidates_found,
            "min_papers": self.min_papers,
            "corpus_size": self.corpus_size,
            "suggestions": [s.as_dict() for s in self.suggestions],
        }


def _quote(steer: str, limit: int = 160) -> str:
    """The steer, whitespace-collapsed and length-capped, in quotes."""
    flat = re.sub(r"\s+", " ", (steer or "").strip())
    if not flat:
        return "(no brief text)"
    if len(flat) > limit:
        flat = flat[: limit - 1].rstrip() + "…"
    return f"“{flat}”"


def _already_steered(term: str, haystack: str) -> bool:
    """True when the user's own brief already names this term."""
    return term.lower() in haystack


def _count_matches(haystacks: list[str], terms: tuple[str, ...]) -> int:
    """How many corpus papers match ANY of ``terms`` — the select_candidates rule."""
    return sum(1 for h in haystacks if any(t in h for t in terms))


def suggest_steers(
    corpus: list[Any],
    *,
    steer_text: str,
    min_papers: int,
    limit: int = 3,
) -> list[SteerSuggestion]:
    """Corpus-derived broadening suggestions. Deterministic, no LLM.

    For every asset class in ``strategy_fusion._ASSET_SYNONYMS``, count the
    corpus papers the lexical filter matches, drop the ones the brief already
    names, drop anything that could not clear ``min_papers`` on its own, and
    return the strongest ``limit``. Asset classes are the whole vocabulary
    here: they are the only terms ``select_candidates`` filters membership on,
    so they are the only terms that can move ``candidates_found``.

    Sorted by (-papers, term) — a total order, so the same corpus always yields
    the same suggestions.
    """
    if not corpus:
        return []
    from archimedes.agents.strategy_fusion import _ASSET_SYNONYMS

    haystacks = [p.haystack for p in corpus]  # `haystack` rebuilds a string per access
    flat_steer = re.sub(r"\s+", " ", (steer_text or "").lower())

    scored: list[SteerSuggestion] = []
    for term, synonyms in _ASSET_SYNONYMS.items():
        if _already_steered(term, flat_steer):
            continue
        scored.append(SteerSuggestion(term, "asset_class", _count_matches(haystacks, (term, *synonyms))))

    viable = sorted((s for s in scored if s.papers >= min_papers), key=lambda s: (-s.papers, s.term))
    return viable[:limit]


def assess_corpus_viability(brief: GenerateBrief) -> CorpusViability:
    """Run the pipeline's own retrieval for ``brief`` and report what it found.

    Never raises: any failure degrades to ``CORPUS_UNAVAILABLE`` with
    ``candidates_found=0``, which is the honest reading (we could not retrieve)
    rather than a guess about why.

    Synchronous and corpus-loading — call it from a thread
    (``asyncio.to_thread``), and only on the path that needs it.
    """
    steer = (brief.intent or "").strip()
    asset_classes = list(brief.asset_classes or [])
    min_papers = _MIN_PAPERS_FALLBACK

    # Everything that can fail — the import included — is inside the try, so
    # the "never raises" promise in the docstring holds for the caller that
    # relies on it (`_debate_can_run`, whose False is what makes the pipeline
    # report GENERATION_UNAVAILABLE instead of crashing).
    try:
        from archimedes.agents.strategy_fusion import (
            MIN_PAPERS,
            FusionBrief,
            load_corpus,
            select_candidates,
        )

        min_papers = MIN_PAPERS
        corpus = load_corpus()
        fb = FusionBrief(
            asset_classes=asset_classes,
            risk_appetite=brief.risk_appetite,
            strategic_direction=steer,
            max_papers=brief.max_papers,
        )
        candidates_found = len(select_candidates(fb, corpus))
        # Only the actionable shortfall pays for the suggestion scan.
        suggestions = (
            suggest_steers(
                corpus,
                steer_text=f"{steer} {' '.join(asset_classes)}",
                min_papers=min_papers,
            )
            if corpus and candidates_found < min_papers
            else []
        )
    except Exception:
        logger.debug("corpus viability assessment failed; reporting corpus unavailable", exc_info=True)
        return CorpusViability(
            steer=steer,
            asset_classes=asset_classes,
            corpus_size=0,
            candidates_found=0,
            min_papers=min_papers,
            reason_code=REASON_CORPUS_UNAVAILABLE,
        )

    if candidates_found >= min_papers:
        reason_code = REASON_OK
    elif corpus:
        reason_code = REASON_TOO_FEW_PAPERS
    else:
        # Nothing loaded at all — "broaden your brief" would be a lie here.
        reason_code = REASON_CORPUS_UNAVAILABLE

    return CorpusViability(
        steer=steer,
        asset_classes=asset_classes,
        corpus_size=len(corpus),
        candidates_found=candidates_found,
        min_papers=min_papers,
        reason_code=reason_code,
        suggestions=suggestions,
    )
