"""``assoc/v1`` — the paper-association contract and its projections.

Issue #1637, PR-1 of the #1688 split: the CONTRACT half. The trace-writing half
(``consulted_paper_hashes``) rides #1800's preimage boundary in PR-3, and the
one-time ``content_hash`` re-stamp is PR-2, gated on a read-only Aurora
dry-run — so nothing here asserts either. The passport's *visual* half is
#1646's; nothing here asserts layout.

Hermetic by construction: in-memory SQLite for every DB test, the corpus lookup
and the Redis trace store mocked at their boundaries. No network, no `.env`.

Each test names the defect it pins. Where a test is a **guard**, the same test
also feeds it the input that must be rejected — a guard nobody has watched
reject something is not known to guard anything (CLAUDE.md § "A guard must be
shown to reject something").
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from archimedes.models.chat import Base
from archimedes.models.paper_assoc import (
    ASSOC_KEYS,
    ASSOC_SCHEMA,
    ROLE_CITED,
    ROLE_CONSIDERED,
    assert_assoc,
    assoc_handle,
    assoc_identity,
    assoc_to_paper_ref,
    is_assoc,
    make_assoc,
    normalize_assoc,
    normalize_assocs,
    paper_ref_to_assoc,
)
from archimedes.models.paper_ref import PaperRef
from archimedes.models.strategy_passport_record import PassportPaperRef
from archimedes.models.strategy_store import (
    StrategyRecord,
    _compute_content_hash,
    upsert_strategy,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


# ── The historical shapes, verbatim ──────────────────────────────────────────
#
# Reproduced from the writers as they stood before this issue. They are the
# fixture, not a paraphrase: the whole defect was that these were *different*.
LEGACY_MAIN_PY = [{"arxiv_id": "2301.00001", "title": "Momentum", "authors": ["Ada"]}]
LEGACY_DEBATE = [{"arxiv_id": "2301.00001", "title": ""}]
LEGACY_FUSION_JOB = [{"arxiv_id": "2301.00001", "sha256": ""}]
#: What `debate_engine._make_entry` emits on current `main` (#1739 added the
#: per-paper mechanism prose) — a FIFTH shape, and the reason the contract is
#: enforced at the write boundary rather than at each writer.
CURRENT_DEBATE = [{"arxiv_id": "2301.00001", "title": "", "mechanism": "trend filter", "spec_elements": ["sma_200"]}]
LEGACY_SHAPES = (LEGACY_MAIN_PY, LEGACY_DEBATE, LEGACY_FUSION_JOB, CURRENT_DEBATE)


# ═══════════════════════════════════════════════════════════════════════════
# 1. One shape
# ═══════════════════════════════════════════════════════════════════════════


class TestAssocSchemaIsSingleShape:
    """Acceptance 1. Before: four key sets. After: one."""

    def test_the_writer_shapes_really_did_disagree(self):
        """The premise, pinned — so this file cannot quietly test nothing."""
        key_sets = [frozenset(shape[0]) for shape in LEGACY_SHAPES]
        assert len({*key_sets}) == 4, "fixture no longer reproduces the four-shape split"

    def test_every_writer_shape_normalizes_to_one_key_set(self):
        normalized = [normalize_assocs(shape)[0] for shape in LEGACY_SHAPES]
        for a in normalized:
            assert set(a) == ASSOC_KEYS
            assert a["schema"] == ASSOC_SCHEMA
        assert len({frozenset(a) for a in normalized}) == 1

    def test_stored_shape_is_identical_whichever_writer_wrote_it(self, session):
        """The end-to-end version: four writer shapes, one stored key set.

        This is the property that matters, and it is enforced at ONE choke
        point (``upsert_strategy``) rather than at N call sites — which is why
        a pre-store carrier is still allowed to hold extra in-process keys.
        """
        stored = []
        for i, shape in enumerate(LEGACY_SHAPES):
            record = upsert_strategy(
                session,
                generation_method="fusion",
                strategy_name=f"s{i}",
                thesis="t",
                source_papers=shape,
                asset_universe=["SPY"],
            )
            stored.append(json.loads(record.source_papers)[0])
        assert len({frozenset(a) for a in stored}) == 1
        for a in stored:
            assert_assoc(a)

    def test_the_1739_attribution_pair_survives_the_write(self, session):
        """``mechanism``/``spec_elements`` are DURABLE (#1739), not
        request-scoped: ``_resolve_source_papers`` carries both to the API
        response. Normalizing them away would have been a silent regression —
        so they are part of the record, not extras a normalizer may drop."""
        record = upsert_strategy(
            session,
            generation_method="debate",
            strategy_name="d",
            thesis="t",
            source_papers=CURRENT_DEBATE,
            asset_universe=["SPY"],
        )
        stored = json.loads(record.source_papers)[0]
        assert stored["mechanism"] == "trend filter"
        assert stored["spec_elements"] == ["sma_200"]

    def test_mechanism_is_not_contribution(self, session):
        """The raw claim and the ATTRIBUTED statement are different fields.

        ``_passport_paper_refs`` shows ``mechanism`` as ``contribution`` only
        when ``spec_elements`` backs it; collapsing the two here would launder
        an unverified claim into the cited-paper column (#1739).
        """
        a = normalize_assoc({"arxiv_id": "2401.1", "mechanism": "carry", "spec_elements": []})
        assert a["mechanism"] == "carry"
        assert a["contribution"] is None

    def test_blank_strings_normalize_to_null_not_to_empty(self):
        """``""`` is not a value. ``None`` is the honest absence."""
        a = normalize_assoc({"arxiv_id": "2301.1", "title": "", "sha256": "", "doi": "  "})
        assert a["title"] is None
        assert a["content_hash"] is None
        assert a["doi"] is None

    def test_an_association_naming_nothing_at_all_is_dropped(self):
        """The ``PaperRef(title=strategy_name)`` placeholder is not a paper."""
        assert normalize_assocs([{}]) == []
        assert normalize_assocs([{"arxiv_id": "", "doi": "", "title": "  "}]) == []

    def test_a_curated_reference_with_no_arxiv_id_is_KEPT(self):
        """Every curated strategy in this repo declares ``PAPER_ARXIV_ID = None``.

        An arXiv-only id space would have silently dropped all 34 of them from
        the passport projection. ``doi`` and the case-folded ``title`` are
        documented fallbacks, not optional niceties — see ``assoc_handle``.
        Owner decision Q9 on #1688: accepted as written.
        """
        by_doi = normalize_assocs([{"doi": "10.3905/jwm.2007.674809", "title": "Tactical Allocation"}])
        assert len(by_doi) == 1
        assert assoc_handle(by_doi[0]) == "doi:10.3905/jwm.2007.674809"

        by_title = normalize_assocs([{"title": "A Quantitative Approach to Tactical Asset Allocation"}])
        assert len(by_title) == 1
        assert assoc_handle(by_title[0]) == "title:a quantitative approach to tactical asset allocation"

    def test_the_handle_precedence_is_arxiv_then_doi_then_title(self):
        assert assoc_handle(make_assoc("2301.1", doi="10.1/x", title="T")) == "2301.1"
        assert assoc_handle(make_assoc(None, doi="10.1/x", title="T")) == "doi:10.1/x"
        assert assoc_handle(make_assoc(None, title="T")) == "title:t"
        assert assoc_handle(make_assoc(None)) is None

    def test_the_store_and_the_passport_share_ONE_identity_definition(self):
        """A second definition is how the two drift into disagreeing."""
        from archimedes.services.passport_loader import _ref_key

        for arxiv_id, doi, title in (
            ("2301.1", "10.1/x", "T"),
            (None, "10.1/x", "T"),
            (None, None, "Tactical Allocation"),
        ):
            assert _ref_key(arxiv_id, doi, title) == assoc_handle({"arxiv_id": arxiv_id, "doi": doi, "title": title})

    def test_role_is_closed_and_rejects_anything_else(self):
        """GUARD + its adversarial companion, in one test."""
        assert make_assoc("2301.1", role=ROLE_CONSIDERED)["role"] == ROLE_CONSIDERED
        with pytest.raises(ValueError, match="role"):
            make_assoc("2301.1", role="probably")
        with pytest.raises(ValueError, match="role"):
            normalize_assoc({"arxiv_id": "2301.1", "role": "maybe-cited"})

    def test_assert_assoc_rejects_every_legacy_shape(self):
        """GUARD: the validator must not accept what it exists to replace."""
        for shape in LEGACY_SHAPES:
            assert not is_assoc(shape[0])
            with pytest.raises(ValueError, match="key mismatch"):
                assert_assoc(shape[0])
        # …and a record that is assoc/v1 except for a tampered schema tag.
        tampered = make_assoc("2301.1") | {"schema": "assoc/v2"}
        with pytest.raises(ValueError, match="schema tag"):
            assert_assoc(tampered)

    def test_the_two_write_boundaries_both_normalize(self):
        """GUARD over the sources, plus the input it must reject.

        There are exactly two paths into ``strategy_store.source_papers``:
        ``upsert_strategy`` (every generation writer) and ``main.py``'s example
        seed, which builds a ``StrategyRecord`` directly and therefore has to
        normalize itself. A guard that only ran over the current tree would
        pass forever without anyone knowing whether it can fail, so the same
        predicates are run against a literal regression below.
        """

        def normalizes(text: str, needle: str) -> bool:
            # Comment lines are stripped first: both files QUOTE the shape they
            # replaced, in the comment explaining why. A guard that counted the
            # explanation as the offence would force the fix to be undocumented
            # to stay green.
            code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
            return needle in code

        store = (REPO_ROOT / "backend/archimedes/models/strategy_store.py").read_text(encoding="utf-8")
        assert normalizes(store, "source_papers = normalize_assocs(source_papers)"), (
            "upsert_strategy stopped normalizing — the column can hold N shapes again"
        )

        main_py = (REPO_ROOT / "backend/archimedes/main.py").read_text(encoding="utf-8")
        assert normalizes(main_py, "paper_ref_to_assoc(p)"), "the example seed writes an un-normalized shape"
        assert not normalizes(main_py, '{"arxiv_id": p.arxiv_id, "title": p.title, "authors": p.authors}'), (
            "main.py reintroduced its legacy association shape"
        )

        # Adversarial companions: both predicates DO fire.
        assert not normalizes(
            "papers = [{'arxiv_id': a} for a in ids]", "source_papers = normalize_assocs(source_papers)"
        )
        assert normalizes(
            'papers_list = [{"arxiv_id": p.arxiv_id, "title": p.title, "authors": p.authors} for p in s.papers]',
            '{"arxiv_id": p.arxiv_id, "title": p.title, "authors": p.authors}',
        )


# ═══════════════════════════════════════════════════════════════════════════
# 2. Identity, not shape, is what the hash sees
# ═══════════════════════════════════════════════════════════════════════════


class TestContentHashIgnoresEnrichment:
    """Acceptance 2. Backfilling a title must not fork a strategy."""

    def _h(self, papers):
        return _compute_content_hash("fusion", "Name", "Thesis", papers, ["SPY"])

    def test_enrichment_does_not_change_the_hash(self):
        bare = self._h([{"arxiv_id": "2301.1"}])
        enriched = self._h(
            [
                make_assoc(
                    "2301.1",
                    title="A Paper",
                    authors=["Ada", "Grace"],
                    year=2023,
                    venue="JF",
                    doi="10.1/x",
                    contribution="supplies the timing rule",
                    selection_rank=3,
                    semantic_score=0.81,
                    content_hash="deadbeef",
                )
            ]
        )
        assert bare == enriched

    def test_all_writer_shapes_hash_to_one_value(self):
        assert len({self._h(shape) for shape in LEGACY_SHAPES}) == 1

    def test_order_and_duplicates_do_not_change_identity(self):
        a = self._h([{"arxiv_id": "2301.1"}, {"arxiv_id": "2301.2"}])
        b = self._h([{"arxiv_id": "2301.2"}, {"arxiv_id": "2301.1"}, {"arxiv_id": "2301.1"}])
        assert a == b

    def test_a_doi_identified_paper_keeps_its_identity_under_enrichment(self):
        """The curated case: no arXiv id, so the DOI is the handle — and adding
        authors/year around it must still not move the hash."""
        bare = self._h([{"doi": "10.3905/jwm.2007.674809"}])
        enriched = self._h(
            [make_assoc(None, doi="10.3905/jwm.2007.674809", authors=["Faber"], year=2007, title="Tactical")]
        )
        assert bare == enriched

    def test_the_hash_still_distinguishes_a_different_paper_set(self):
        """The complement — an identity-only hash must not collapse everything."""
        assert self._h([{"arxiv_id": "2301.1"}]) != self._h([{"arxiv_id": "2301.9"}])

    def test_role_is_part_of_identity(self):
        """A considered paper is not a cited one; the two are different strategies."""
        assert self._h([make_assoc("2301.1", role=ROLE_CITED)]) != self._h([make_assoc("2301.1", role=ROLE_CONSIDERED)])

    def test_assoc_identity_emits_only_id_and_role(self):
        rows = assoc_identity([make_assoc("2301.1", title="T", authors=["Ada"])])
        assert rows == [["2301.1", "cited"]]

    def test_two_writers_now_dedup_to_a_single_row(self, session):
        """The user-visible payoff: one strategy, not two."""
        for shape in (LEGACY_DEBATE, LEGACY_FUSION_JOB):
            upsert_strategy(
                session,
                generation_method="fusion",
                strategy_name="Same Strategy",
                thesis="Same thesis",
                source_papers=shape,
                asset_universe=["SPY"],
            )
        assert session.query(StrategyRecord).count() == 1

    def test_a_pre_existing_legacy_hash_is_NOT_matched_by_this_pr(self, session):
        """The honest interim cost, pinned rather than discovered in prod.

        PR-1 redefines the hash and deliberately does NOT re-stamp stored rows
        (owner call on #1688: a dedup-hygiene step does not get to be
        irreversible on an unmeasured table; PR-2 lands it behind a dry-run).
        So a row carrying a *legacy* content_hash is not found by the new
        lookup and a re-upsert of the same content inserts a second row.

        This test exists so that cost is a recorded decision instead of a
        surprise, and so PR-2 has something concrete to flip.
        """
        legacy = StrategyRecord(
            id="legacy0000000001",
            content_hash="0xlegacyhashthatthenewfunctionneverproduces",
            generation_method="fusion",
            source_papers=json.dumps(LEGACY_DEBATE),
            strategy_name="Same Strategy",
            thesis="Same thesis",
            asset_universe=json.dumps(["SPY"]),
        )
        session.add(legacy)
        session.flush()

        upsert_strategy(
            session,
            generation_method="fusion",
            strategy_name="Same Strategy",
            thesis="Same thesis",
            source_papers=LEGACY_DEBATE,
            asset_universe=["SPY"],
        )
        assert session.query(StrategyRecord).count() == 2, (
            "a legacy row was matched — if this now passes with 1, the re-stamp landed and this test is stale"
        )
        # …and the legacy row's id, the FK every other table joins on, is intact.
        assert session.query(StrategyRecord).filter_by(id="legacy0000000001").first() is not None


# ═══════════════════════════════════════════════════════════════════════════
# 3. The pin — what already works and must keep working
# ═══════════════════════════════════════════════════════════════════════════


class TestFullCitedSetSurvivesToPassport:
    """Acceptance 4. A pin, not a regression test: it exists so the write-path
    surgery above cannot quietly drop a paper on its way to the passport."""

    IDS = ("2301.00001", "2301.00002", "2301.00003", "2301.00004")

    def test_four_ids_reach_store_passport_refs_and_papers_identically(self, session):
        from archimedes.services.passport_loader import ingest_passport

        record = upsert_strategy(
            session,
            generation_method="fusion",
            strategy_name="Four-paper fusion",
            thesis="A synthesis of four mechanisms.",
            source_papers=[make_assoc(i) for i in self.IDS],
            asset_universe=["SPY"],
        )

        in_store = {p["arxiv_id"] for p in json.loads(record.source_papers)}

        passport = record.to_strategy_passport()
        in_papers = {p.arxiv_id for p in passport.papers}

        ingest_passport(session, passport, generation_method="fusion")
        session.flush()
        in_refs = {r.arxiv_id for r in session.query(PassportPaperRef).filter_by(passport_id=record.id).all()}

        assert in_store == set(self.IDS)
        assert in_papers == set(self.IDS)
        assert in_refs == set(self.IDS)

    def test_considered_papers_never_reach_the_passport(self, session):
        """The one addition to the pin: role gates the projection."""
        record = upsert_strategy(
            session,
            generation_method="fusion",
            strategy_name="Two cited, one considered",
            thesis="t",
            source_papers=[
                make_assoc("2301.00001"),
                make_assoc("2301.00002"),
                make_assoc("2301.00009", role=ROLE_CONSIDERED),
            ],
            asset_universe=["SPY"],
        )
        assert len(json.loads(record.source_papers)) == 3
        assert {p.arxiv_id for p in record.to_strategy_passport().papers} == {"2301.00001", "2301.00002"}

    def test_the_display_title_fallback_is_evaluator_only(self, session):
        """``to_strategy_passport`` fills a blank title with the strategy name
        because the legacy keyword evaluator selects on ``paper_title`` and a
        blank one drops the strategy from the market scan. That fallback must
        NOT reach any wire projection — see the null-not-a-stand-in tests."""
        record = upsert_strategy(
            session,
            generation_method="fusion",
            strategy_name="Faber SMA200",
            thesis="t",
            source_papers=[make_assoc("2301.00001")],
            asset_universe=["SPY"],
        )
        assert record.to_strategy_passport().papers[0].title == "Faber SMA200"


# ═══════════════════════════════════════════════════════════════════════════
# 4. Papers go in the paper field, strategies in the strategy field
# ═══════════════════════════════════════════════════════════════════════════


class TestPapersNeverLandInTheStrategyField:
    """Acceptance 7, adjusted for what `main` now is.

    The issue asked for the fusion job's construction trace to stop writing
    arXiv ids into ``strategies_referenced``. **#1595 deleted that route
    entirely** and ``services/construction_trace.py`` was deleted before that
    as a zero-caller surface — so nothing writes a construction trace at all,
    and there is no writer left to fix.

    What survives is the rule underneath: **papers go in
    ``consulted_paper_hashes``; ``strategies_referenced`` holds strategy ids.**
    Widening ``STRATEGY_REFERENCE_DECISION_TYPES`` was deliberately NOT done
    (owner decision Q4 on #1688).
    """

    def test_construction_stays_out_of_the_scope_while_nothing_writes_one(self):
        from archimedes.services.redis_state import STRATEGY_REFERENCE_DECISION_TYPES

        assert "construction" not in STRATEGY_REFERENCE_DECISION_TYPES

    def test_no_live_writer_puts_an_arxiv_id_in_strategies_referenced(self):
        """GUARD over every module that writes a trace record, + its rejection."""

        def arxiv_into_strategy_field(text: str) -> list[str]:
            code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
            needles = (
                "strategies_referenced=result.source_arxiv_ids",
                '"strategies_referenced": result.source_arxiv_ids',
                "strategies_referenced=source_arxiv_ids",
                "strategies_referenced=arxiv_ids",
            )
            return [n for n in needles if n in code]

        writers = [
            "backend/archimedes/chain/agent_runner.py",
            "backend/archimedes/services/paper_trace.py",
            "backend/archimedes/api/_route_helpers.py",
            "backend/archimedes/api/strategies_routes.py",
        ]
        for rel in writers:
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            assert arxiv_into_strategy_field(text) == [], f"{rel} routes papers into the strategy field"

        # Adversarial companion: the predicate fires on the deleted writer's
        # actual line, verbatim from `_run_fusion_job` before #1595 removed it.
        assert arxiv_into_strategy_field('"strategies_referenced": result.source_arxiv_ids,')

    def test_every_trace_writer_carries_the_hashed_paper_field(self):
        """``consulted_paper_hashes`` is in ``_HASH_FIELDS``: a writer that
        drops it produces a record whose canonical bytes cannot be rebuilt.

        ``api/_route_helpers.persist_trace_off_chain`` was doing exactly that
        (#1637). It has no callers, which is why nobody noticed — and it is
        also the shape ``/verify``'s new source-paper check reads.
        """
        for rel in (
            "backend/archimedes/chain/agent_runner.py",
            "backend/archimedes/services/paper_trace.py",
            "backend/archimedes/api/_route_helpers.py",
        ):
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            assert "consulted_paper_hashes" in text, rel

    def test_an_arxiv_id_could_never_be_mistaken_for_a_strategy_id_anyway(self):
        """Defence in depth: the matcher compares WHOLE strings, and a
        ``strategy_store`` id is ``content_hash[:16]``."""
        from archimedes.services.redis_state import trace_references_strategy

        row = {"decision_type": "rebalance", "strategies_referenced": ["2301.00001", "2405.09876"]}
        assert trace_references_strategy(row, "abcdef0123456789") is False
        assert trace_references_strategy(row, "2301.0000") is False  # no substring match


# ═══════════════════════════════════════════════════════════════════════════
# 5. Enrichment survives re-ingest
# ═══════════════════════════════════════════════════════════════════════════


def _passport(papers, sid="strat-001"):
    from archimedes.models.strategy import (
        PositionSizing,
        RebalanceFrequency,
        StrategyPassport,
        StrategyStatus,
    )

    return StrategyPassport(
        id=sid,
        papers=papers,
        methodology_summary="Trend following.",
        asset_universe=["SPY"],
        position_sizing=PositionSizing.EQUAL_WEIGHT,
        rebalance_frequency=RebalanceFrequency.DAILY,
        status=StrategyStatus.CANDIDATE,
    )


class TestReingestPreservesEnrichment:
    """Acceptance 8. ``ingest_passport(force_update=True)`` ran on every
    real-returns refresh and DELETEd the refs first, so enrichment could not
    survive by construction."""

    ENRICHED = PaperRef(
        arxiv_id="2301.00001",
        title="Momentum Everywhere",
        authors=["Ada", "Grace"],
        doi="10.1/x",
        venue="JF",
        year=2013,
        citation_count=99,
        contribution="supplies the cross-sectional ranking rule",
        selection_rank=2,
        semantic_score=0.77,
        content_hash="c0ffee",
    )
    #: What the refresh path actually has in hand: id + title, nothing else.
    THIN = PaperRef(arxiv_id="2301.00001", title="Momentum Everywhere")

    def test_backfilled_fields_survive_an_id_and_title_only_refresh(self, session):
        from archimedes.services.passport_loader import ingest_passport

        ingest_passport(session, _passport([self.ENRICHED]), generation_method="fusion")
        session.flush()

        ingest_passport(session, _passport([self.THIN]), generation_method="fusion", force_update=True)
        session.flush()

        refs = session.query(PassportPaperRef).filter_by(passport_id="strat-001").all()
        assert len(refs) == 1, "the merge must not duplicate the row it matched"
        ref = refs[0]
        assert ref.authors == json.dumps(["Ada", "Grace"])
        assert ref.year == 2013
        assert ref.venue == "JF"
        assert ref.doi == "10.1/x"
        assert ref.citation_count == 99
        assert ref.contribution == "supplies the cross-sectional ranking rule"
        assert ref.selection_rank == 2
        assert ref.semantic_score == pytest.approx(0.77)
        assert ref.content_hash == "c0ffee"

    def test_a_real_new_value_still_overwrites(self, session):
        """Merge, not freeze: a caller that HAS a better value must win."""
        from archimedes.services.passport_loader import ingest_passport

        ingest_passport(session, _passport([self.ENRICHED]), generation_method="fusion")
        session.flush()
        corrected = PaperRef(arxiv_id="2301.00001", title="Momentum Everywhere", year=2012)
        ingest_passport(session, _passport([corrected]), generation_method="fusion", force_update=True)
        session.flush()

        ref = session.query(PassportPaperRef).filter_by(passport_id="strat-001").one()
        assert ref.year == 2012
        assert ref.authors == json.dumps(["Ada", "Grace"]), "an unrelated column must not be collateral"

    def test_an_empty_paper_list_is_ignorance_not_a_deletion(self, session):
        from archimedes.services.passport_loader import ingest_passport

        ingest_passport(session, _passport([self.ENRICHED]), generation_method="fusion")
        session.flush()
        ingest_passport(session, _passport([]), generation_method="fusion", force_update=True)
        session.flush()

        assert session.query(PassportPaperRef).filter_by(passport_id="strat-001").count() == 1

    def test_a_genuinely_changed_cited_set_does_drop_the_old_paper(self, session):
        """The complement — merge must not become an append-only pile."""
        from archimedes.services.passport_loader import ingest_passport

        ingest_passport(session, _passport([self.ENRICHED]), generation_method="fusion")
        session.flush()
        replacement = PaperRef(arxiv_id="2409.99999", title="Something Else")
        ingest_passport(session, _passport([replacement]), generation_method="fusion", force_update=True)
        session.flush()

        ids = {r.arxiv_id for r in session.query(PassportPaperRef).filter_by(passport_id="strat-001").all()}
        assert ids == {"2409.99999"}

    def test_a_curated_title_only_reference_merges_instead_of_duplicating(self, session):
        """The 34 curated rows carry no arXiv id, so the title is the handle.
        A blind key on ``arxiv_id`` would append a new row on every refresh."""
        from archimedes.services.passport_loader import ingest_passport

        curated = PaperRef(title="A Quantitative Approach to Tactical Asset Allocation", year=2007)
        ingest_passport(session, _passport([curated], sid="curated-1"), generation_method="curated")
        session.flush()
        ingest_passport(
            session,
            _passport([PaperRef(title="A Quantitative Approach to Tactical Asset Allocation")], sid="curated-1"),
            generation_method="curated",
            force_update=True,
        )
        session.flush()

        refs = session.query(PassportPaperRef).filter_by(passport_id="curated-1").all()
        assert len(refs) == 1
        assert refs[0].year == 2007


class TestContributionRoundTrips:
    """Acceptance 9. ``contribution`` was written to the column and then
    silently omitted from ``to_dict()`` — the passport rendered a column the
    projection could never fill."""

    def test_contribution_reaches_the_column_the_dict_and_the_response(self, session):
        from archimedes.api.schemas import PaperRefResponse
        from archimedes.services.passport_loader import ingest_passport

        ref_in = PaperRef(
            arxiv_id="2301.00001",
            title="Momentum Everywhere",
            contribution="supplies the cross-sectional ranking rule",
        )
        ingest_passport(session, _passport([ref_in]), generation_method="fusion")
        session.flush()

        row = session.query(PassportPaperRef).filter_by(passport_id="strat-001").one()
        assert row.contribution == "supplies the cross-sectional ranking rule"

        as_dict = row.to_dict()
        assert "contribution" in as_dict
        assert as_dict["contribution"] == "supplies the cross-sectional ranking rule"

        response = PaperRefResponse(**{k: v for k, v in as_dict.items() if k in PaperRefResponse.model_fields})
        assert response.contribution == "supplies the cross-sectional ranking rule"

    def test_it_survives_the_assoc_round_trip(self):
        a = paper_ref_to_assoc(PaperRef(arxiv_id="2301.1", contribution="the vol overlay"))
        assert a["contribution"] == "the vol overlay"
        assert assoc_to_paper_ref(a).contribution == "the vol overlay"

    def test_to_dict_never_prints_an_empty_title_as_a_title(self):
        row = PassportPaperRef(passport_id="p", arxiv_id="2301.1", title="")
        assert row.to_dict()["title"] is None


# ═══════════════════════════════════════════════════════════════════════════
# 6. Honest rendering of the degenerate cases (backend half of acceptance 12)
# ═══════════════════════════════════════════════════════════════════════════


class TestSingleAndZeroPaperPassportsAreHonest:
    """Acceptance 12, backend half. The render conditions belong to #1646; what
    is tested here is the payload those renderers receive.

    Both cases go through the REAL response builder
    (``_passport_to_strategy_response``), not through a re-derivation of what it
    ought to return — a test that computes ``x or None`` itself would pass
    identically against the unfixed code.
    """

    @staticmethod
    def _response(session, sid, papers):
        from archimedes.api.strategies_routes import _passport_to_strategy_response
        from archimedes.services.passport_loader import get_passport, ingest_passport

        ingest_passport(session, _passport(papers, sid=sid), generation_method="fusion")
        session.flush()
        return _passport_to_strategy_response(get_passport(session, sid), session=session)

    def test_one_paper_row_projects_exactly_one_paper(self, session):
        resp = self._response(session, "one", [PaperRef(arxiv_id="2301.1", title="Only Paper")])
        assert len(resp.papers) == 1
        assert resp.papers[0].arxiv_id == "2301.1"
        assert resp.paper_title == "Only Paper"

    def test_zero_paper_row_reports_a_null_title_not_empty_quotes(self, session):
        resp = self._response(session, "none", [])
        assert resp.papers == []
        assert resp.paper_title is None
        assert resp.paper_arxiv_id is None

    def test_an_unresolvable_title_is_null_never_the_arxiv_id(self, session):
        """Owner decision Q3: an arXiv id printed in a title slot is a small
        fabrication, and the id is already carried in ``arxiv_id``."""
        resp = self._response(session, "untitled", [PaperRef(arxiv_id="2301.99999", title="")])
        assert resp.papers[0].title is None
        assert resp.papers[0].arxiv_id == "2301.99999"
        assert resp.paper_title is None


# ═══════════════════════════════════════════════════════════════════════════
# 7. No semantic claims on provenance surfaces
# ═══════════════════════════════════════════════════════════════════════════


class TestNoSemanticClaimOnProvenanceSurfaces:
    """Acceptance 13 / precedent #778. Retrieval is LEXICAL: the corpus has no
    embedding column, ``corpus_meta`` is 0 and the KG is 0/0. A provenance
    payload that says "semantic" or "knowledge graph" is making a claim the
    system cannot back."""

    #: The builders that assemble what a passport or trace SAYS about itself.
    PAYLOAD_BUILDERS = (
        "backend/archimedes/models/paper_assoc.py",
        "backend/archimedes/models/strategy_passport_record.py",
        "backend/archimedes/models/paper_ref.py",
        "backend/archimedes/services/source_tracker.py",
    )

    #: Not in the list on purpose: ``semantic_score`` is a FIELD NAME for the
    #: reranker's own output, which is a real measured number when a rerank
    #: ran and ``None`` when it did not. The banned thing is prose asserting
    #: semantic/embedding/knowledge-graph retrieval to a reader.
    BANNED = ("embedding", "knowledge graph", "knowledge-graph")

    @staticmethod
    def _claims(text: str, banned) -> list[str]:
        lowered = text.lower()
        return [w for w in banned if w in lowered]

    def test_provenance_payload_builders_make_no_semantic_claim(self):
        for rel in self.PAYLOAD_BUILDERS:
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            assert self._claims(text, self.BANNED) == [], f"{rel} asserts retrieval it cannot back"

    def test_the_guard_fires_on_a_claim(self):
        """GUARD's adversarial companion — the predicate must be able to fail."""
        assert self._claims("papers found by embedding similarity", self.BANNED) == ["embedding"]
        assert self._claims("sourced from the Knowledge Graph", self.BANNED) == ["knowledge graph"]

    def test_the_honest_vocabulary_is_available_instead(self):
        """``semantic_score`` is nullable — absence is how "no rerank" is said."""
        a = make_assoc("2301.1")
        assert a["semantic_score"] is None
        assert a["selection_rank"] is None


# ═══════════════════════════════════════════════════════════════════════════
# 8. The verifier has a caller
# ═══════════════════════════════════════════════════════════════════════════


class TestVerifySourcePapersIsWired:
    """Acceptance 6. ``verify_source_papers`` had zero production callers;
    ``/verify`` re-hashed the body and never checked the papers."""

    @pytest.fixture
    def anyio_backend(self):
        return "asyncio"

    @staticmethod
    def _off_chain(consulted):
        return {
            "id": "trace-1",
            "vault_address": "",
            "trace_hash": "0xaabbccdd",
            "arc_tx_hash": None,
            "consulted_paper_hashes": consulted,
        }

    async def _verify(self, off_chain, corpus_hashes=None, corpus_error=None):
        from archimedes.main import app
        from archimedes.services.redis_state import AgentStateStore
        from httpx import ASGITransport, AsyncClient

        target = "archimedes.services.source_tracker.corpus_content_hashes"
        kwargs = {"side_effect": corpus_error} if corpus_error else {"return_value": corpus_hashes or {}}
        with (
            patch.object(AgentStateStore, "get_trace", AsyncMock(return_value=off_chain)),
            patch.object(AgentStateStore, "close", AsyncMock()),
            patch(target, **kwargs),
            patch("archimedes.api.traces_routes._assert_can_read", AsyncMock(return_value=None)),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                return await client.get("/api/traces/1/verify")

    @pytest.mark.anyio
    async def test_absent_paper_reports_papers_verified_false_and_names_it(self):
        resp = await self._verify(
            self._off_chain(["2301.00001:", "2999.99999:"]),
            corpus_hashes={"2301.00001": ""},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["papers_verified"] is False
        assert body["source_paper_verification"]["missing"] == ["2999.99999"]
        assert body["source_paper_verification"]["mode"] == "checked"

    @pytest.mark.anyio
    async def test_present_papers_report_verified_true(self):
        resp = await self._verify(
            self._off_chain(["2301.00001:", "2301.00002:"]),
            corpus_hashes={"2301.00001": "", "2301.00002": ""},
        )
        body = resp.json()
        assert body["papers_verified"] is True
        assert body["source_paper_verification"]["checked"] == 2
        assert body["source_paper_verification"]["missing"] == []

    @pytest.mark.anyio
    async def test_a_claimed_hash_that_disagrees_is_a_mismatch(self):
        resp = await self._verify(
            self._off_chain(["2301.00001:deadbeef"]),
            corpus_hashes={"2301.00001": "c0ffee"},
        )
        body = resp.json()
        assert body["papers_verified"] is False
        assert body["source_paper_verification"]["hash_mismatch"] == ["2301.00001"]

    @pytest.mark.anyio
    async def test_no_claimed_papers_is_not_checked_rather_than_passed(self):
        resp = await self._verify(self._off_chain([]))
        body = resp.json()
        assert body["papers_verified"] is None
        assert body["source_paper_verification"]["mode"] == "no_papers_claimed"

    @pytest.mark.anyio
    async def test_a_corpus_outage_is_not_a_provenance_failure(self):
        """GUARD, adversarial: an outage must not read as either verdict."""
        from archimedes.services.source_tracker import CorpusUnavailable

        resp = await self._verify(
            self._off_chain(["2301.00001:"]),
            corpus_error=CorpusUnavailable("down"),
        )
        body = resp.json()
        assert body["papers_verified"] is None
        assert body["source_paper_verification"]["mode"] == "corpus_unavailable"
        assert body["source_paper_verification"]["missing"] == []

    def test_empty_claimed_hash_asserts_nothing_and_is_not_a_mismatch(self):
        """#1091: the corpus columns are NULL, so existence is all we can check."""
        from archimedes.services.source_tracker import verify_source_papers

        result = verify_source_papers(["2301.1:"], [{"arxiv_id": "2301.1", "content_hash": ""}])
        assert result == {"verified": True, "missing": [], "hash_mismatch": []}

    def test_split_consulted_entry_handles_both_shapes(self):
        from archimedes.services.source_tracker import split_consulted_entry

        assert split_consulted_entry("2301.1:abc") == ("2301.1", "abc")
        assert split_consulted_entry("2301.1:") == ("2301.1", "")
        assert split_consulted_entry("2301.1") == ("2301.1", "")


# ═══════════════════════════════════════════════════════════════════════════
# 9. "Corpus unavailable" must mean what SQLAlchemy actually raises
# ═══════════════════════════════════════════════════════════════════════════


class TestCorpusUnavailableCoversRealFailures:
    """``corpus_content_hashes`` converted only ``DBAPIError``.

    ``OperationalError`` / ``InterfaceError`` / ``DatabaseError`` are all
    ``DBAPIError`` subclasses, so a refused connect or a server that went away
    was already handled. What was NOT handled is the family SQLAlchemy raises
    *itself*, before a statement ever reaches the driver — above all
    ``sqlalchemy.exc.TimeoutError``, the pool giving up on a checkout because
    every connection is blocked. That is what a lock-wedged database looks like
    from a read path (2026-09-03), and it escaped the catch: ``/verify`` 500'd
    instead of reporting ``corpus_unavailable``, which is a worse answer than
    either verdict — the endpoint whose whole job is to say what it checked
    stopped answering at all.
    """

    @staticmethod
    def _raising(exc):
        """A ``get_session`` stand-in whose query raises ``exc``."""
        from contextlib import contextmanager

        class _Session:
            def query(self, *_a, **_k):
                raise exc

            def close(self):
                pass

        @contextmanager
        def _factory():
            yield _Session()

        return _factory

    def test_corpus_unavailable_covers_what_sqlalchemy_actually_raises(self):
        """The classes named in ``CORPUS_UNAVAILABLE_ERRORS``, checked against
        the INSTALLED SQLAlchemy rather than assumed from their names."""
        import sqlalchemy.exc as sa_exc
        from archimedes.services.source_tracker import (
            CORPUS_UNAVAILABLE_ERRORS,
            CorpusUnavailable,
            corpus_content_hashes,
        )

        # The three that were already covered — and the reason they were:
        # every one of them IS a DBAPIError.
        for cls in (sa_exc.OperationalError, sa_exc.InterfaceError, sa_exc.DatabaseError):
            assert issubclass(cls, sa_exc.DBAPIError), f"{cls.__name__} is no longer a DBAPIError"

        # The ones that were not, and are the point of this test.
        escaped = (sa_exc.TimeoutError, sa_exc.DisconnectionError, sa_exc.ResourceClosedError)
        for cls in escaped:
            assert not issubclass(cls, sa_exc.DBAPIError), (
                f"{cls.__name__} is now a DBAPIError — this test no longer proves anything"
            )
            assert issubclass(cls, CORPUS_UNAVAILABLE_ERRORS), f"{cls.__name__} is not in CORPUS_UNAVAILABLE_ERRORS"

        # …and each is actually converted, not merely listed.
        for cls in (*escaped, sa_exc.OperationalError):
            exc = cls("boom", None, Exception("boom")) if issubclass(cls, sa_exc.DBAPIError) else cls("boom")
            with patch("archimedes.db.get_session", self._raising(exc)), pytest.raises(CorpusUnavailable):
                corpus_content_hashes(["2301.00001"])

    def test_a_real_pool_timeout_is_reported_as_an_outage(self):
        """The end-to-end shape, not a hand-constructed exception.

        A pool with one connection, already checked out, is the smallest honest
        model of "every connection is blocked". The class this produces is
        whatever the installed SQLAlchemy produces — that is the whole point of
        raising it for real rather than naming it.
        """
        import sqlalchemy as sa
        from archimedes.services.source_tracker import CorpusUnavailable, corpus_content_hashes
        from sqlalchemy.orm import sessionmaker

        engine = sa.create_engine(
            "sqlite://",
            poolclass=sa.pool.QueuePool,
            pool_size=1,
            max_overflow=0,
            pool_timeout=0.05,
        )
        held = engine.connect()  # the only connection in the pool
        try:
            with (
                patch("archimedes.db.get_session", sessionmaker(bind=engine)),
                pytest.raises(CorpusUnavailable),
            ):
                corpus_content_hashes(["2301.00001"])
        finally:
            held.close()
            engine.dispose()

    def test_a_bug_in_this_function_is_NOT_laundered_into_an_outage(self):
        """GUARD, adversarial: the catch must stay narrow.

        Widening to ``SQLAlchemyError`` would swallow a misconfigured URL
        (``ArgumentError``) and a query this module built wrong
        (``InvalidRequestError``), and a bug that reports itself as an outage
        is the defect ``resolve_paper_hashes`` already shipped once.
        """
        import sqlalchemy.exc as sa_exc
        from archimedes.services.source_tracker import corpus_content_hashes

        for exc in (
            AttributeError("PaperRecord has no attribute 'content_hsah'"),
            sa_exc.InvalidRequestError("bad query"),
            sa_exc.ArgumentError("bad url"),
            sa_exc.NoSuchModuleError("no dialect"),
        ):
            with patch("archimedes.db.get_session", self._raising(exc)), pytest.raises(type(exc)):
                corpus_content_hashes(["2301.00001"])

    def test_no_ids_never_touches_the_database(self):
        """An empty request is answered without a session, so an outage that
        coincides with one is not reported as an outage."""
        from archimedes.services.source_tracker import corpus_content_hashes

        with patch("archimedes.db.get_session", self._raising(AssertionError("must not open a session"))):
            assert corpus_content_hashes([]) == {}
            assert corpus_content_hashes(["", "   "]) == {}
