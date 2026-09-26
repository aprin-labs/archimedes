"""Claim-integrity guards for the corpus surface (issue #778, extended by #1368).

Adjudicated against production on 2026-08-19: the ``papers`` table holds 10,000 rows
with title + abstract, **no embedding column exists anywhere in the schema**,
``corpus_meta`` is 0 rows, and ``kg_entities``/``kg_relations`` are 0/0. Retrieval is
therefore lexical (TF-IDF cosine computed at request time over title + abstract), and no
knowledge graph exists. ``paper_rag.py`` itself is honest — it reports a runtime tri-state
(``live | degraded | disabled``) — but the prose around it had drifted into asserting
embedded-at-ingest storage and live semantic retrieval as standing facts.

Three guards, all hermetic (no network, no DB, no Redis, no ``.env``):

1. **Prose guard** — the public surfaces listed in ``PUBLIC_SURFACES`` must not contain the
   claim-shapes in ``OVERCLAIM_PATTERNS``: that the corpus is embedded, that retrieval is
   semantically live, that a knowledge graph exists. The guard is negation-blind on purpose
   (see ``find_overclaims``), so the way to describe the gap is to name what runs — the UI
   now quotes ``/health``'s ``paper_rag`` field and lets it say which scorer is active.

2. **UI claim-literal guard** (added #1368) — ``FORBIDDEN_UI_LITERALS`` bans specific bare
   strings ("Knowledge Graph" as a tab label, "10,000 papers" with no qualifier, "No entities
   found." as a KG zero-state) that the prose guard structurally cannot see because they
   carry no claim-verb. A visitor reading a permanent "Knowledge Graph" tab over a 0-row
   table, or "0 entities / 0 relations" indistinguishable from "your query matched nothing,"
   forms the same false impression a prose over-claim would — the guard just needs a
   different shape to catch it.

3. **Honest-endpoint guard** — with no KB artifact, ``GET /api/corpus/graph`` must fail with
   a 503 naming ``kb_artifact_not_found`` rather than synthesising a graph, and
   ``GET /api/corpus/kg/entities`` must return empty entity/relation sets (it does *not*
   503 — overstating that would be the same defect pointing the other way). The README and
   ``docs/architecture.md`` now assert both behaviours in prose, so both are pinned here
   instead of trusted.

Each guard carries its own anti-vacuity coverage: every prose/literal pattern must reject its
canonical example, every declared surface/target file must exist, and the 503 assertion is
shown to be conditional (artifacts present ⇒ 200) and to reject a synthesising route.

**Scope limit, stated plainly.** ``OVERCLAIM_PATTERNS`` catches claim *shapes*, not every
possible over-claim. Prose that describes the mechanism without asserting its state — "a
semantic rerank with MiniLM sentence embeddings" — passes this guard and still needs a
human read. The patterns are the floor, not the ceiling.

**Point-in-time documents are deliberately NOT in ``PUBLIC_SURFACES``.** ``docs/audits/``,
``docs/handovers/``, and ``docs/archive/`` are dated records of what was believed on a given
day; a historical document that contains a claim later found false is not a defect, it is
the evidence of how the error propagated. Scanning them would force a rewrite of history to
make a test pass, which is the wrong repair. They are corrected by **annotation** instead —
a dated claim-integrity banner at the top naming what was adjudicated false and pointing at
the live authority, as done on ``docs/handovers/2026-07-14-architecture-review.md`` (whose
corpus-panel and memory-layer-E bullets asserted a live stored-embedding layer) and on
``docs/corpus-architecture.md``. If a point-in-time doc is ever rewritten into a live
reference, it belongs in ``PUBLIC_SURFACES`` at that point and not before.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from fastapi import HTTPException

# ---------------------------------------------------------------------------
# Repo layout
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    import archimedes  # backend/archimedes/__init__.py → parents: archimedes, backend, <repo>

    return Path(archimedes.__file__).resolve().parents[2]


#: Public, reader-facing surfaces that describe corpus retrieval. A file listed here is
#: scanned in full. Renaming one without updating this tuple is caught by
#: ``test_every_declared_surface_exists`` — otherwise the scan would silently shrink to
#: nothing and the guard would pass vacuously.
PUBLIC_SURFACES: tuple[str, ...] = (
    "README.md",
    "docs/doc-index.md",
    "docs/architecture.md",
    "docs/corpus-architecture.md",
    "docs/specs/architecture-page-design.md",
    "ui/src/components/Architecture.jsx",
    "ui/src/components/CorpusGraph.jsx",
    "ui/src/components/CorpusKG.jsx",
    # Added by #1368: the two public surfaces PR #1315 didn't touch. Listing
    # them here is necessary but NOT sufficient — none of OVERCLAIM_PATTERNS
    # match a bare tab label or a bare "10,000 papers" literal (no claim verb
    # for a prose-shape regex to grab), so the FORBIDDEN_UI_LITERALS table
    # below is what actually catches the #1368 defect.
    "ui/src/components/CorpusExplorer.jsx",
    "ui/src/components/OnboardingTour.jsx",
)

# ---------------------------------------------------------------------------
# Prose guard
# ---------------------------------------------------------------------------

#: ``(name, regex, canonical_example)``. The example is the shortest string that must trip
#: the pattern — ``test_every_pattern_rejects_its_canonical_example`` runs it, so a pattern
#: that stops matching anything (a typo, an over-eager edit) fails loudly instead of
#: silently guarding nothing.
OVERCLAIM_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "embed_at_ingest",
        re.compile(r"\bembed(?:ded|ding)?[ \-]at[ \-]ingest\b", re.IGNORECASE),
        "the corpus is embedded at ingest",
    ),
    (
        "embeddings_are_live",
        re.compile(r"\blive\s+(?:\w+\s+){0,2}embeddings\b|\bembeddings?\s+(?:are|is)\s+live\b", re.IGNORECASE),
        "paper retrieval already uses live semantic embeddings (MiniLM)",
    ),
    (
        "semantic_retrieval_is_live",
        re.compile(
            r"\bsemantic\s+(?:retrieval|rerank(?:ing)?|search)\b[^.\n;]{0,40}?\b(?:is|are)\s+live\b", re.IGNORECASE
        ),
        "the semantic retrieval layer (MiniLM rerank) is live",
    ),
    (
        "minilm_rerank_is_live",
        re.compile(r"\bMiniLM\b[^.\n;]{0,25}?\brerank(?:ing|er)?\s+(?:is\s+)?live\b", re.IGNORECASE),
        "MiniLM rerank live; knowledge graph not built.",
    ),
    (
        "corpus_holds_embeddings",
        re.compile(
            r"\bcorpus\b[^.\n;]{0,60}?\b(?:is\s+embedded\b|(?:has|have|holds?|contains?|carries|stores?)"
            r"\s+(?:\w+\s+){0,3}embeddings\b)",
            re.IGNORECASE,
        ),
        "the corpus stores SPECTER2 embeddings",
    ),
    (
        "knowledge_graph_exists",
        re.compile(
            r"\bknowledge[ \-]graph\b[^.\n;]{0,40}?\b(?:is|has\s+been|was)\s+(?:built|live|populated)\b",
            re.IGNORECASE,
        ),
        "the knowledge graph is built over the corpus",
    ),
    (
        "semantic_search_over_the_corpus",
        re.compile(
            r"\b(?:semantic|vector|embedding|RAG)\b[ \-]?(?:\w+\s+){0,2}?\bover\s+(?:the\s+)?(?:10,?000|10k)\b",
            re.IGNORECASE,
        ),
        "RAG over 10,000 papers",
    ),
)


def find_overclaims(text: str) -> list[tuple[str, str]]:
    """Return ``(pattern_name, matched_text)`` for every banned claim-shape in ``text``.

    Deliberately **negation-blind**: "nothing is embedded at ingest" is rejected exactly
    like "embedded at ingest". Two reasons. (1) A negation window is the wrong primitive
    here — the pre-fix sentence *"The semantic retrieval layer (MiniLM rerank) is live and
    does not depend on the KG"* carries a negator inside the same clause as the over-claim,
    so any proximity or clause-scoped negation check silently waves it through, which is
    precisely the drift this guard exists to stop. (2) A reader skimming a page keeps the
    claim-shape, not the negation. Honest copy must therefore state what *is* true rather
    than restate the false sentence in order to deny it.
    """
    hits: list[tuple[str, str]] = []
    for name, pattern, _example in OVERCLAIM_PATTERNS:
        hits.extend((name, match.group(0)) for match in pattern.finditer(text))
    return hits


def test_every_declared_surface_exists():
    """A renamed/removed surface must fail loudly, not shrink the scan to nothing."""
    missing = [rel for rel in PUBLIC_SURFACES if not (_repo_root() / rel).is_file()]
    assert not missing, f"PUBLIC_SURFACES lists files that do not exist: {missing}"


def test_every_pattern_rejects_its_canonical_example():
    """No dead patterns: each one must still reject the claim it was written for."""
    for name, _pattern, example in OVERCLAIM_PATTERNS:
        hits = find_overclaims(example)
        assert any(hit_name == name for hit_name, _ in hits), (
            f"pattern {name!r} no longer rejects its canonical example {example!r} — "
            f"it is guarding nothing (hits: {hits})"
        )


def test_guard_is_negation_blind_by_design():
    """A denial of the claim is rejected too — see ``find_overclaims`` for why.

    The load-bearing case is the second one: the pre-fix README limitation sentence pairs
    the over-claim with a negator *inside the same clause*, so a negation-aware guard would
    have permitted the exact prose this issue was filed about.
    """
    assert find_overclaims("Nothing is embedded at ingest."), "guard must reject the claim-shape even when denied"

    denied_in_the_same_clause = "The semantic retrieval layer (MiniLM rerank) is live and does not depend on the KG."
    names = {name for name, _ in find_overclaims(denied_in_the_same_clause)}
    assert "semantic_retrieval_is_live" in names, f"a same-clause negator disarmed the guard (hits: {names})"

    honest = "Candidates are ranked at request time over title and abstract; /health names the scorer."
    assert find_overclaims(honest) == [], "honest, health-gated copy must not trip the guard"


@pytest.mark.parametrize("rel_path", PUBLIC_SURFACES)
def test_public_surface_carries_no_corpus_overclaim(rel_path: str):
    text = (_repo_root() / rel_path).read_text(encoding="utf-8")
    hits = find_overclaims(text)
    assert not hits, (
        f"{rel_path} asserts corpus capability that production does not have (#778): {hits}. "
        "Prod retrieval is lexical, the papers schema carries text only, and the KB pipeline "
        "has produced no artifact — name what /health.paper_rag reports instead of asserting "
        "a capability. Note the guard is negation-blind: denying the claim still trips it."
    )


# ---------------------------------------------------------------------------
# UI claim-literal guard (issue #1368)
# ---------------------------------------------------------------------------
#
# OVERCLAIM_PATTERNS above catches claim *shapes* — a verb ("is", "has been",
# "was") pinning a capability to the corpus. It structurally cannot catch a
# bare tab label ("Knowledge Graph") or a bare number with no verb ("10,000
# papers"): there is no claim-shape for a prose regex to grab, only a true
# word placed where it creates a false impression. This table names those
# literal strings directly, file by file, instead of trying to generalize a
# regex for something that isn't a sentence.

#: ``(label, rel_path, regex, canonical_example)`` — same anti-vacuity shape as
#: ``OVERCLAIM_PATTERNS``: ``canonical_example`` is the exact banned literal, and
#: ``test_every_ui_literal_pattern_rejects_its_canonical_example`` proves each
#: pattern still matches it, so a typo'd regex fails loudly instead of quietly
#: guarding nothing.
FORBIDDEN_UI_LITERALS: tuple[tuple[str, str, re.Pattern[str], str], ...] = (
    (
        "kg_tab_labeled_knowledge_graph",
        "ui/src/components/CorpusExplorer.jsx",
        re.compile(r"""['"]Knowledge Graph['"]"""),
        "'knowledge-graph': 'Knowledge Graph',",
    ),
    (
        "corpus_explorer_unqualified_papers_chip",
        "ui/src/components/CorpusExplorer.jsx",
        re.compile(r"total_papers\?\.toLocaleString\(\)\}\s*papers\b"),
        "{overview.total_papers?.toLocaleString()} papers",
    ),
    (
        "onboarding_tour_bare_10000_papers",
        "ui/src/components/OnboardingTour.jsx",
        re.compile(r"10,000 papers\b"),
        "10,000 papers",
    ),
    (
        "kg_zero_state_no_entities_found",
        "ui/src/components/CorpusKG.jsx",
        re.compile(r"No entities found\."),
        "No entities found.",
    ),
)


def find_ui_literal_violations() -> list[tuple[str, str, str]]:
    """Return ``(label, rel_path, matched_text)`` for every banned literal still on disk."""
    hits: list[tuple[str, str, str]] = []
    for label, rel_path, pattern, _example in FORBIDDEN_UI_LITERALS:
        text = (_repo_root() / rel_path).read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            hits.append((label, rel_path, match.group(0)))
    return hits


def test_every_ui_literal_declared_file_exists():
    """Same discipline as ``test_every_declared_surface_exists``: a bad rel_path must fail
    loudly rather than silently scanning nothing."""
    missing = [rel for _, rel, _pattern, _example in FORBIDDEN_UI_LITERALS if not (_repo_root() / rel).is_file()]
    assert not missing, f"FORBIDDEN_UI_LITERALS lists files that do not exist: {missing}"


def test_every_ui_literal_pattern_rejects_its_canonical_example():
    """No dead patterns: each one must still match the literal it was written to ban."""
    for label, _rel_path, pattern, example in FORBIDDEN_UI_LITERALS:
        assert pattern.search(example), (
            f"UI literal pattern {label!r} no longer rejects its canonical example {example!r} — it is guarding nothing"
        )


def test_no_forbidden_ui_literals_on_disk():
    """The guard PR #1315 shipped without: bare claim-literals with no verb, on the two
    surfaces PUBLIC_SURFACES didn't cover (#1368). Verified vacuous against OVERCLAIM_PATTERNS
    before this table existed — see the module docstring's Evidence 1."""
    hits = find_ui_literal_violations()
    assert not hits, (
        f"Public corpus UI surfaces still carry a claim-literal the prose-shape guard cannot "
        f"see (#1368): {hits}. These are bare labels/numbers, not sentences — remove the "
        f"literal at its source rather than extending OVERCLAIM_PATTERNS for it."
    )


def test_kg_zero_state_names_the_pipeline_not_the_query():
    """The ``entities.length === 0`` branch of CorpusKG.jsx must branch on the live
    ``/health`` ``corpus_kg_built`` value, not assert "pipeline hasn't run" unconditionally.

    A plain substring check (does "corpus_kg_built" / "#1090" appear anywhere in the file)
    would also pass for an *unconditional* render of that copy — which is itself an
    over-claim once #1090 lands and a legitimate empty search reaches this same branch
    (adversarial reviewer finding on the first version of this fix). So this test locates
    the actual zero-state ternary and requires: (1) it reads ``health.corpus_kg_built``
    rather than asserting a value, (2) the ``corpus_kg_built === false`` side names the
    pipeline + #1090, and (3) the ``corpus_kg_built === true`` side (a real empty search)
    names the search term instead of repeating the pipeline claim.
    """
    text = (_repo_root() / "ui/src/components/CorpusKG.jsx").read_text(encoding="utf-8")

    zero_state_match = re.search(
        r"entities\.length === 0 \? \((?P<body>.*?)\n\s*\) : \(\s*\n\s*<div style=\{\{ overflow",
        text,
        re.DOTALL,
    )
    assert zero_state_match, (
        "ui/src/components/CorpusKG.jsx: could not locate the `entities.length === 0` "
        "zero-state branch — has the render structure changed? Update this test's anchor "
        "regex to match."
    )
    zero_state = zero_state_match.group("body")

    assert "health.corpus_kg_built ?" in zero_state, (
        "ui/src/components/CorpusKG.jsx: the zero-state must branch on the live "
        "health.corpus_kg_built value from /health, not assert a pipeline state "
        "unconditionally (#1368)"
    )

    _cond, _sep, rest = zero_state.partition("health.corpus_kg_built ?")
    built_branch, _sep, not_built_branch = rest.partition(") : (")

    assert re.search(r"#1090\b", not_built_branch), (
        "ui/src/components/CorpusKG.jsx: the corpus_kg_built===false branch must reference "
        "#1090 (the KB pipeline artifact issue) so it names the pipeline, not the query"
    )
    assert "corpus_kg_built" in not_built_branch, (
        "ui/src/components/CorpusKG.jsx: the corpus_kg_built===false branch must point at "
        "/health's corpus_kg_built field as the live authority"
    )
    assert "searchedTerm" in built_branch or "query" in built_branch, (
        "ui/src/components/CorpusKG.jsx: the corpus_kg_built===true branch (a legitimate "
        "empty search once the pipeline HAS run) must name the search term, not repeat the "
        "pipeline-not-built claim"
    )


# --- Adversarial demonstrations: the exact prose this PR removed must be rejected ------
#
# Verbatim pre-fix text. Each of these lived on a public surface and each must trip the
# guard, otherwise the guard would have permitted the very drift it was written to stop.

PRE_FIX_README_STATUS_LINE = (
    "- **Paper corpus + MiniLM semantic retrieval** — a ~10,000-paper quant-finance corpus "
    "(file-sourced, embedded at ingest) backs `paper_rag.py`, which runs `all-MiniLM-L6-v2` "
    "semantic reranking as a second pass over the keyword-pre-filtered candidate set."
)

PRE_FIX_README_LIMITATION_LINE = (
    "- **Knowledge graph not yet built:** The corpus graph (`corpus_kg_built=false` in `/health`) "
    "is planned — citation-link extraction over the 10k-paper corpus is roadmap, not current. "
    "The semantic retrieval layer (MiniLM rerank) is live and does not depend on the KG."
)

PRE_FIX_ARCHITECTURE_DOC_LINE = (
    "2. **Embed at ingest**: title+abstract embeddings are the query-time lookup key "
    "([`docs/corpus-architecture.md`](corpus-architecture.md))."
)

PRE_FIX_DOCS_INDEX_CELL = "10,000 arXiv preprints (not peer-reviewed). MiniLM rerank live; knowledge graph not built."

PRE_FIX_CORPUS_GRAPH_UI_STRING = (
    "Knowledge-graph pipeline hasn't run yet (SPECTER2 clustering + entity extraction) — paper "
    "retrieval already uses live semantic embeddings (MiniLM); this similarity graph will populate "
    "once the KB pipeline produces its first artifact."
)


@pytest.mark.parametrize(
    ("label", "prose", "expected_pattern"),
    [
        ("README § Status", PRE_FIX_README_STATUS_LINE, "embed_at_ingest"),
        ("README § Known Limitations", PRE_FIX_README_LIMITATION_LINE, "semantic_retrieval_is_live"),
        ("docs/architecture.md § corpus flow", PRE_FIX_ARCHITECTURE_DOC_LINE, "embed_at_ingest"),
        ("doc register row", PRE_FIX_DOCS_INDEX_CELL, "minilm_rerank_is_live"),
        ("CorpusGraph.jsx empty state", PRE_FIX_CORPUS_GRAPH_UI_STRING, "embeddings_are_live"),
    ],
)
def test_guard_rejects_the_prose_it_replaced(label: str, prose: str, expected_pattern: str):
    hits = find_overclaims(prose)
    names = {name for name, _ in hits}
    assert expected_pattern in names, f"{label}: guard failed to reject the pre-fix prose (hits: {hits})"


# ---------------------------------------------------------------------------
# Honest-503 guard
# ---------------------------------------------------------------------------


def _assert_503_on_missing_artifact(route) -> None:
    """The guard body: ``route`` must refuse with a structured 503, not invent a graph."""
    try:
        result = asyncio.run(route())
    except HTTPException as exc:
        assert exc.status_code == 503, f"expected 503, got {exc.status_code}"
        assert isinstance(exc.detail, dict), f"503 detail should be structured, got {exc.detail!r}"
        assert exc.detail.get("error") == "kb_artifact_not_found", f"unexpected 503 detail: {exc.detail!r}"
        return
    raise AssertionError(f"route returned a graph instead of 503 with no artifact present: {result!r}")


def _patch_artifacts_absent(monkeypatch) -> None:
    from archimedes.services import kb_artifacts

    def _raise_missing():
        raise kb_artifacts.ArtifactNotFound("embeddings.npy")

    monkeypatch.setattr(kb_artifacts, "load_umap_projection", lambda: None)
    monkeypatch.setattr(kb_artifacts, "load_embeddings", _raise_missing)


def test_corpus_graph_503s_when_no_kb_artifact_exists(monkeypatch):
    """No artifact ⇒ an explicit 503, never a synthesised graph.

    Mocked at the artifact-store boundary (``kb_artifacts``), which is the seam that talks
    to S3 / the mounted volume — the route's own logic runs for real.
    """
    from archimedes.api.corpus_routes import corpus_graph

    _patch_artifacts_absent(monkeypatch)
    _assert_503_on_missing_artifact(corpus_graph)


def test_the_503_guard_rejects_a_graph_synthesised_from_nothing(monkeypatch):
    """Adversarial: hand the guard the implementation it exists to catch.

    A route that quietly fabricates points when the artifact is missing is the exact defect
    ("no fake fallbacks", ``kb_artifacts`` module docstring) — it must fail the assertion
    above, otherwise that assertion is decoration.
    """
    _patch_artifacts_absent(monkeypatch)

    async def synthesising_graph() -> dict:
        points = [{"arxiv_id": "0000.00000", "x": 0.0, "y": 0.0, "cluster_id": 1}]
        return {"points": points, "topics": {}, "cluster_count": 1, "point_count": len(points)}

    with pytest.raises(AssertionError, match="returned a graph instead of 503"):
        _assert_503_on_missing_artifact(synthesising_graph)


def test_corpus_graph_503_is_conditional_not_unconditional(monkeypatch):
    """Anti-vacuity: with an artifact present the same route returns 200 with its points.

    Without this, the assertion above would also pass against a route hard-wired to 503,
    which would prove nothing about artifact detection.
    """
    from archimedes.api.corpus_routes import corpus_graph
    from archimedes.services import kb_artifacts

    points = [{"arxiv_id": "2501.00001", "x": 0.1, "y": 0.2, "cluster_id": 3}]
    monkeypatch.setattr(kb_artifacts, "load_umap_projection", lambda: points)
    monkeypatch.setattr(kb_artifacts, "load_topics", lambda: {"3": "momentum"})

    result = asyncio.run(corpus_graph())

    assert result["point_count"] == 1
    assert result["points"] == points
    assert result["cluster_count"] == 1


def test_kg_search_returns_empty_sets_not_synthesised_entities(monkeypatch):
    """The other half of the endpoint claim the README now makes.

    ``/api/corpus/kg/*`` does **not** 503 on an un-built graph — it returns empty entity and
    relation sets. Stating "503" for these routes would be the same defect in the opposite
    direction, so the honest wording is pinned here. Mocked at the DB-session boundary with
    an in-memory SQLite carrying the real KG tables and no rows (the prod shape: 0/0).
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from archimedes import db as db_module
    from archimedes.api.corpus_routes import kg_search_entities
    from archimedes.models.chat import Base
    from archimedes.models.kg import KGEntity, KGRelation  # noqa: F401 — KGRelation registers its table on Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()

    class _CtxSession:
        def __enter__(self):
            return session

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(db_module, "get_session", lambda: _CtxSession())
    try:
        empty = asyncio.run(kg_search_entities(q="momentum"))

        # Anti-vacuity: the route swallows SQLAlchemy errors into the *same* empty shape, so
        # an empty result alone would also be produced by a query that never ran. Seed one
        # row and confirm it comes back — that proves the query path is live and the 0/0
        # answer above is the data speaking, not an exception.
        session.add(KGEntity(canonical_name="momentum factor", entity_type="method", paper_count=7))
        session.commit()
        populated = asyncio.run(kg_search_entities(q="momentum"))
    finally:
        session.close()

    assert empty == {"query": "momentum", "entities": [], "relations": []}
    assert [e["canonical_name"] for e in populated["entities"]] == ["momentum factor"]
