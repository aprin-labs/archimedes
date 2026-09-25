"""Canonical arXiv category definitions for the q-fin corpus.

Two things live here, and this module is deliberately dependency-free (no
SQLAlchemy, no httpx) so both the backend services and the standalone
``scripts/`` harvesters can import it:

1. ``QFIN_CATEGORIES`` — **the single source of truth for what we harvest.**
   Before #1635 there were two divergent literals: ``scripts/bulk_ingest_arxiv.py``
   (9 categories, the list that actually produced ``data/corpus/manifest.jsonl``)
   and ``arxiv_corpus.py`` (7 core + 3 cross-lists, no ``GN``). Every harvest
   path now imports this tuple instead of redeclaring one.
2. ``CATEGORY_LABELS`` — plain-English labels applied at API serialization for
   the corpus catalog so non-finance users can read the page without a
   glossary. Per Phase 3b of the Spine+ v2 plan.
"""

from __future__ import annotations

# ── The harvest terms (single source of truth) ──────────────────
#
# arXiv's ``cat:`` operator matches *any* category tag on a paper, not just the
# primary — so a cs.LG-primary paper cross-listed to q-fin.ST is already
# returned by this query. (Proof: the pre-#1635 manifest, harvested by a pure
# q-fin OR-query, holds 428 rows with a ``cs.LG`` primary.) Adding explicit
# ``cat:cs.LG`` / ``cat:stat.ML`` / ``cat:econ.EM`` queries would therefore add
# no q-fin-relevant recall while risking generic-ML leakage — see #1635.
#
# ``q-fin.EC`` is NOT here on purpose: it is retired/aliased to ``econ.GN`` and
# returns 0 results, so querying it was pure noise (#1635).
QFIN_CATEGORIES: tuple[str, ...] = (
    "q-fin.CP",  # Computational Finance
    "q-fin.GN",  # General Finance
    "q-fin.MF",  # Mathematical Finance
    "q-fin.PM",  # Portfolio Management
    "q-fin.PR",  # Pricing of Securities
    "q-fin.RM",  # Risk Management
    "q-fin.ST",  # Statistical Finance
    "q-fin.TR",  # Trading & Market Microstructure
)


CATEGORY_LABELS: dict[str, str] = {
    # q-fin
    "q-fin.ST": "Statistical Finance",
    "q-fin.MF": "Mathematical Finance",
    "q-fin.CP": "Computational Finance",
    "q-fin.RM": "Risk Management",
    "q-fin.PM": "Portfolio Management",
    "q-fin.TR": "Trading & Market Microstructure",
    "q-fin.GN": "General Finance",
    "q-fin.PR": "Pricing of Securities",
    # Retired by arXiv (aliased to econ.GN) and never harvested — kept only so
    # a legacy row that still carries the tag renders with a name, not a code.
    "q-fin.EC": "Economics (within q-fin)",
    # cs
    "cs.LG": "Machine Learning",
    "cs.CL": "Natural Language Processing",
    "cs.CE": "Computational Engineering / Finance",
    "cs.AI": "Artificial Intelligence",
    "cs.NE": "Neural & Evolutionary Computing",
    # stat
    "stat.ME": "Statistical Methodology",
    "stat.ML": "Machine Learning (statistics)",
    "stat.AP": "Applied Statistics",
    "stat.TH": "Statistical Theory",
    # math
    "math.OC": "Optimization & Control",
    "math.PR": "Probability",
    "math.ST": "Mathematical Statistics",
    # econ
    "econ.GN": "General Economics",
    "econ.EM": "Econometrics",
    "econ.TH": "Economic Theory",
    # physics adjacents
    "physics.soc-ph": "Social Physics (econophysics)",
    "physics.data-an": "Data Analysis & Statistics (physics)",
    # quant-ph
    "quant-ph": "Quantum Methods",
}


def label_for(code: str | None) -> str | None:
    """Return the plain-English label for an arxiv code, or None if unknown."""
    if not code:
        return None
    return CATEGORY_LABELS.get(code)
