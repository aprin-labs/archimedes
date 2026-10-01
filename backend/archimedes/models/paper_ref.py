"""PaperRef — a single paper reference in a strategy passport.

Fusion strategies synthesize from N papers; curated strategies reference 1.
Both use the same PaperRef type so the passport shape is fusion-native.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PaperRef:
    """Reference to a single paper backing (or contributing to) a strategy.

    Attributes
    ----------
    arxiv_id : str | None
        e.g. ``"2509.11420"``.  ``None`` for non-arxiv papers.
    title : str
        Full paper title.
    authors : list[str]
        Author list (up to the first N; ``et al.`` in UI when truncated).
    doi : str | None
        Digital Object Identifier, e.g. ``"10.3905/jwm.2007.674809"``.
    venue : str | None
        Journal / conference / ``"arxiv only"``.
    year : int | None
        Publication year.
    citation_count : int | None
        Snapshot at curation time.
    contribution : str | None
        For fusion strategies — what this paper contributed to the synthesis.
        ``None`` for single-paper curated strategies.
    role : str
        ``"cited"`` (the strategy is built on this paper) or ``"considered"``
        (the selector surfaced it and the strategy did not use it). See
        ``models/paper_assoc.py``; the value is closed to those two.
    selection_rank : int | None
        1-based rank this paper held in the selection list that produced the
        strategy. ``None`` when the association predates selection recording.
    semantic_score : float | None
        Reranker score at selection time. ``None`` when the rerank was
        keyword-only or disabled — which is the common case, and why it must
        stay nullable rather than defaulting to ``0.0``.
    content_hash : str | None
        Corpus content hash for this paper, when one exists. **NULL in
        production** (#1091): the corpus's ``content_hash``/``pdf_sha256``
        columns are unhydrated, so ``None`` is the correct answer, not a gap
        to be filled with a synthesized value.
    """

    arxiv_id: str | None = None
    title: str = ""
    authors: list[str] = field(default_factory=list)
    doi: str | None = None
    venue: str | None = None
    year: int | None = None
    citation_count: int | None = None
    contribution: str | None = None
    # ── assoc/v1 fields (#1637) ─────────────────────────────
    # Defaulted so every existing PaperRef(...) call site keeps working; the
    # normalizer in models/paper_assoc.py is what populates them.
    role: str = "cited"
    selection_rank: int | None = None
    semantic_score: float | None = None
    content_hash: str | None = None
