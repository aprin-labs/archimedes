"""Hermetic tests for multi-paper passport provenance rendering.

Covers the server-side paper title enrichment in
``_passport_to_strategy_response``: when a ``PassportPaperRef`` row has an
empty title but the corpus ``papers`` table has a matching arxiv_id, the API
response should use the corpus title instead of an empty string.  Falls back
to the bare arxiv_id string when the corpus has no matching row.

All tests use in-memory SQLite — no real Postgres, no network, no .env.
Run: env -i HOME=$HOME PATH=$PATH PYTHONPATH=backend python -m pytest \
       backend/tests/test_multipaper_passport.py -q
"""

from __future__ import annotations

import pytest
from archimedes.models.chat import Base
from archimedes.models.corpus_store import PaperRecord
from archimedes.models.strategy_passport_record import PassportPaperRef, StrategyPassportRecord
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session() -> Session:
    """In-memory SQLite session with all ORM tables created."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    sess = SessionLocal()
    yield sess
    sess.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _add_corpus_paper(
    session: Session,
    arxiv_id: str,
    title: str,
) -> PaperRecord:
    row = PaperRecord(
        arxiv_id=arxiv_id,
        title=title,
        authors="[]",
        abstract="",
        primary_category="q-fin",
        categories="[]",
        published="2023-01-01",
        updated="2023-01-01",
        source="seed",
    )
    session.add(row)
    session.flush()
    return row


def _make_passport_record(
    session: Session,
    strategy_id: str,
    papers: list[dict],  # [{arxiv_id, title}]
) -> StrategyPassportRecord:
    """Create a StrategyPassportRecord with the given paper refs."""
    record = StrategyPassportRecord(
        id=strategy_id,
        generation_method="fusion",
        methodology_summary="Test fusion methodology",
        asset_universe="[]",
        position_sizing="equal_weight",
        rebalance_frequency="weekly",
        status="candidate",
        regime_tag="regime_neutral",
        passes_rigor_gate=False,
    )
    session.add(record)
    session.flush()

    for p in papers:
        ref = PassportPaperRef(
            passport_id=strategy_id,
            arxiv_id=p.get("arxiv_id"),
            title=p.get("title", ""),
            authors="[]",
        )
        session.add(ref)
    session.flush()
    return record


# ---------------------------------------------------------------------------
# _enrich_paper_titles_from_corpus (unit-level)
# ---------------------------------------------------------------------------


class TestEnrichPaperTitlesFromCorpus:
    """Unit tests for the standalone enrichment helper."""

    def test_returns_title_for_matching_arxiv_id(self, session: Session):
        _add_corpus_paper(session, "2301.00001", "Momentum Everywhere")

        from archimedes.api.strategies_routes import _enrich_paper_titles_from_corpus
        from archimedes.models.strategy_passport_record import PassportPaperRef

        refs = [PassportPaperRef(arxiv_id="2301.00001", title="", authors="[]")]
        result = _enrich_paper_titles_from_corpus(refs, session)
        assert result == {"2301.00001": "Momentum Everywhere"}

    def test_skips_refs_with_stored_titles(self, session: Session):
        _add_corpus_paper(session, "2301.00002", "Corpus Title")

        from archimedes.api.strategies_routes import _enrich_paper_titles_from_corpus
        from archimedes.models.strategy_passport_record import PassportPaperRef

        # Title already stored — should NOT appear in missing_ids query
        refs = [PassportPaperRef(arxiv_id="2301.00002", title="Stored Title", authors="[]")]
        result = _enrich_paper_titles_from_corpus(refs, session)
        # No missing ids → empty map
        assert result == {}

    def test_returns_empty_map_when_no_corpus_match(self, session: Session):
        from archimedes.api.strategies_routes import _enrich_paper_titles_from_corpus
        from archimedes.models.strategy_passport_record import PassportPaperRef

        refs = [PassportPaperRef(arxiv_id="2301.99999", title="", authors="[]")]
        result = _enrich_paper_titles_from_corpus(refs, session)
        assert result == {}

    def test_empty_refs_returns_empty_map(self, session: Session):
        from archimedes.api.strategies_routes import _enrich_paper_titles_from_corpus

        result = _enrich_paper_titles_from_corpus([], session)
        assert result == {}


# ---------------------------------------------------------------------------
# _passport_to_strategy_response (integration-level, SQLite)
# ---------------------------------------------------------------------------


class TestPassportToStrategyResponse:
    """Integration tests for the full API response builder with title enrichment."""

    def test_multi_paper_titles_enriched_from_corpus(self, session: Session):
        """Two fusion papers with empty titles get filled from corpus."""
        _add_corpus_paper(session, "2301.00001", "Momentum Everywhere")
        _add_corpus_paper(session, "2301.00002", "Volatility-Managed Portfolios")

        _make_passport_record(
            session,
            "fuse-001",
            [
                {"arxiv_id": "2301.00001", "title": ""},
                {"arxiv_id": "2301.00002", "title": ""},
            ],
        )
        session.flush()

        from archimedes.api.strategies_routes import _passport_to_strategy_response
        from archimedes.services.passport_loader import get_passport

        record = get_passport(session, "fuse-001")
        assert record is not None

        resp = _passport_to_strategy_response(record, session=session)

        assert len(resp.papers) == 2
        title_by_id = {p.arxiv_id: p.title for p in resp.papers}
        assert title_by_id["2301.00001"] == "Momentum Everywhere"
        assert title_by_id["2301.00002"] == "Volatility-Managed Portfolios"

    def test_stored_titles_take_priority_over_corpus(self, session: Session):
        """Pre-stored non-empty titles are NOT overwritten by corpus."""
        _add_corpus_paper(session, "2301.00003", "Corpus Title")

        _make_passport_record(
            session,
            "fuse-002",
            [{"arxiv_id": "2301.00003", "title": "Hand-curated Title"}],
        )
        session.flush()

        from archimedes.api.strategies_routes import _passport_to_strategy_response
        from archimedes.services.passport_loader import get_passport

        record = get_passport(session, "fuse-002")
        resp = _passport_to_strategy_response(record, session=session)

        assert resp.papers[0].title == "Hand-curated Title"

    def test_unknown_arxiv_id_reports_a_null_title_not_the_id(self, session: Session):
        """An unresolvable title is **None**, not the arXiv id (#1637).

        Was: the id was returned in the ``title`` field. An id presented as a
        title is a small fabrication of the same family as printing the
        strategy's own name there — and it is not needed, because ``arxiv_id``
        is right beside it and the renderer already composes its own fallback
        (``{p.title || p.arxiv_id || "—"}``). This also aligns the passport
        with ``_resolve_source_papers``, whose ``resolved_title`` has always
        stayed None for exactly this case. Owner decision Q3 on #1688.
        """
        _make_passport_record(
            session,
            "fuse-003",
            [{"arxiv_id": "2301.99999", "title": ""}],
        )
        session.flush()

        from archimedes.api.strategies_routes import _passport_to_strategy_response
        from archimedes.services.passport_loader import get_passport

        record = get_passport(session, "fuse-003")
        resp = _passport_to_strategy_response(record, session=session)

        assert resp.papers[0].title is None
        assert resp.papers[0].arxiv_id == "2301.99999"
        assert resp.paper_title is None

    def test_no_session_reports_a_null_title_not_the_id(self, session: Session):
        """Without a session, enrichment is skipped and the title stays None."""
        _add_corpus_paper(session, "2301.00004", "Would be enriched")

        _make_passport_record(
            session,
            "fuse-004",
            [{"arxiv_id": "2301.00004", "title": ""}],
        )
        session.flush()

        from archimedes.api.strategies_routes import _passport_to_strategy_response
        from archimedes.services.passport_loader import get_passport

        record = get_passport(session, "fuse-004")
        resp = _passport_to_strategy_response(record, session=None)

        # No session → no enrichment, and the final fallback is None, not the id.
        assert resp.papers[0].title is None
        assert resp.papers[0].arxiv_id == "2301.00004"

    def test_papers_field_populated_for_multi_paper_strategy(self, session: Session):
        """The ``papers`` list on the response contains all source papers."""
        _add_corpus_paper(session, "2301.00010", "Paper Alpha")
        _add_corpus_paper(session, "2301.00011", "Paper Beta")
        _add_corpus_paper(session, "2301.00012", "Paper Gamma")

        _make_passport_record(
            session,
            "fuse-005",
            [
                {"arxiv_id": "2301.00010", "title": ""},
                {"arxiv_id": "2301.00011", "title": ""},
                {"arxiv_id": "2301.00012", "title": ""},
            ],
        )
        session.flush()

        from archimedes.api.strategies_routes import _passport_to_strategy_response
        from archimedes.services.passport_loader import get_passport

        record = get_passport(session, "fuse-005")
        resp = _passport_to_strategy_response(record, session=session)

        assert len(resp.papers) == 3
        assert resp.paper_title == "Paper Alpha"  # legacy field = enriched first paper title

    def test_single_paper_curated_strategy_unaffected(self, session: Session):
        """Single-paper curated strategies render correctly — no regression."""
        _make_passport_record(
            session,
            "cur-001",
            [{"arxiv_id": "0706.2631", "title": "Quantitative Trading with ML"}],
        )
        session.flush()

        from archimedes.api.strategies_routes import _passport_to_strategy_response
        from archimedes.services.passport_loader import get_passport

        record = get_passport(session, "cur-001")
        resp = _passport_to_strategy_response(record, session=session)

        assert len(resp.papers) == 1
        assert resp.papers[0].title == "Quantitative Trading with ML"
        assert resp.papers[0].arxiv_id == "0706.2631"
        # Legacy scalar fields still populated
        assert resp.paper_title == "Quantitative Trading with ML"
        assert resp.paper_arxiv_id == "0706.2631"


# ---------------------------------------------------------------------------
# The verdict of record — what the read path serves, and where #1184's
# zero-variance property is now enforced.
#
# HISTORY, because these tests changed shape and the reason matters. #1184
# fixed a real defect: the passport read path graded the STORED AGGREGATE alone
# ("pending" if sharpe_ratio is NULL, else the stored boolean), so a flat
# persisted series — broken data, or a zero-trade backtest — surfaced as
# "pending", which is a claim ("not graded yet") and for those rows a false
# one. The fix loaded each row's persisted return series on every request and
# re-derived the four-state badge from it.
#
# The owner decision of 2026-09-01 (docs/adr/rigor-verdict-of-record.md) keeps
# the property and moves where it is enforced: a strategy is graded ONCE, at
# backtest time, and `verdict_from_returns` — the real gate — stores
# "degenerate" as itself. The read path serves that stored word. So the
# property is now proven at the WRITER (TestAFlatSeriesIsGradedDegenerate,
# which runs the actual gate over the actual flat shapes #1184 named) and the
# read path is proven to serve what was stored, unchanged
# (TestTheStoredVerdictIsServedVerbatim). Nothing about the four states is
# weaker; the derivation simply stopped happening twice, in two places, with
# two possible answers.
# ---------------------------------------------------------------------------


def _add_backtest_row(
    session: Session,
    strategy_id: str,
    daily_returns: list[float],
) -> None:
    """Persist a backtest row carrying ``daily_returns`` in ``artifact_json``.

    The nesting mirrors what ``get_daily_returns`` actually parses
    (``backtest_repository.py`` — ``artifact["results"][i]["metrics"]["daily_returns"]``),
    not a shape invented for the test.
    """
    import json as _json

    from archimedes.models.backtest_store import BacktestResultRecord
    from archimedes.services.backtest_repository import SOURCE_PIPELINE_DSL_FUSION

    session.add(
        BacktestResultRecord(
            strategy_id=strategy_id,
            content_hash="deadbeef",
            # Required by the model, no default — these rows stand in for the
            # fusion pipeline's own writes, which is the path #1184 is about.
            source_pipeline=SOURCE_PIPELINE_DSL_FUSION,
            artifact_json=_json.dumps({"results": [{"metrics": {"daily_returns": daily_returns}}]}),
        )
    )
    session.commit()


def _passport_with_verdict(
    session: Session,
    strategy_id: str,
    *,
    sharpe_ratio: float | None,
    status: str,
) -> StrategyPassportRecord:
    """A passport row carrying a STORED verdict, coupled the way the loader writes it."""
    record = _make_passport_record(session, strategy_id, [{"arxiv_id": "2301.00009", "title": "T"}])
    record.sharpe_ratio = sharpe_ratio
    record.rigor_gate_status = status
    record.passes_rigor_gate = status == "pass"
    session.commit()  # see _add_backtest_row
    return record


class TestTheStoredVerdictIsServedVerbatim:
    """The read path serves ``strategy_passports.rigor_gate_status``, full stop.

    Anti-vacuity: every case names the mutation that reddens it. The single
    mutation that reddens ALL of them is restoring the read-time derivation —
    ``_rigor_status, _is_placeholder = _passport_rigor_status(record, returns)``
    in ``_passport_to_strategy_response`` — because that expression cannot
    return "degenerate" for a row with no persisted series, cannot return
    "pass" for a row whose stored sharpe is NULL, and answers from a second
    source that can disagree with the first.
    """

    def test_a_stored_degenerate_is_served_as_degenerate(self, session: Session):
        """MUTATION: derive from the aggregate — this row has NO persisted series,
        so the old expression returns "pending" and loses the fourth state."""
        from archimedes.api.strategies_routes import _passport_to_strategy_response

        record = _passport_with_verdict(session, "stored-degen", sharpe_ratio=0.0, status="degenerate")

        resp = _passport_to_strategy_response(record, session)

        assert resp.rigor_gate_status == "degenerate"
        assert resp.passes_rigor_gate is False
        assert resp.is_backtest_placeholder is False, "a degenerate row HAS a backtest; its returns are just flat"

    def test_a_stored_pass_is_served_as_a_pass(self, session: Session):
        """MUTATION: serve ``record.passes_rigor_gate`` instead of the four-state."""
        from archimedes.api.strategies_routes import _passport_to_strategy_response

        record = _passport_with_verdict(session, "stored-pass", sharpe_ratio=1.4, status="pass")

        resp = _passport_to_strategy_response(record, session)

        assert resp.rigor_gate_status == "pass"
        assert resp.passes_rigor_gate is True
        assert resp.is_backtest_placeholder is False

    def test_a_stored_fail_is_served_as_a_fail(self, session: Session):
        from archimedes.api.strategies_routes import _passport_to_strategy_response

        record = _passport_with_verdict(session, "stored-fail", sharpe_ratio=0.2, status="fail")

        resp = _passport_to_strategy_response(record, session)

        assert resp.rigor_gate_status == "fail"
        assert resp.passes_rigor_gate is False

    def test_a_stored_pending_is_served_as_pending_and_marked_a_placeholder(self, session: Session):
        """CONTROL: "pending" has to survive where it is still true."""
        from archimedes.api.strategies_routes import _passport_to_strategy_response

        record = _passport_with_verdict(session, "stored-pending", sharpe_ratio=None, status="pending")

        resp = _passport_to_strategy_response(record, session)

        assert resp.rigor_gate_status == "pending"
        assert resp.passes_rigor_gate is False
        assert resp.is_backtest_placeholder is True

    def test_a_stored_sharpe_does_not_promote_an_ungraded_row(self, session: Session):
        """MUTATION: restore ``"pending" if record.sharpe_ratio is None else ...``.

        The old derivation read a NON-NULL sharpe as proof the gate had run and
        answered. It is not: ``_update_record`` writes ``sharpe_ratio`` from any
        refresh, graded or not. A row with real backtest metrics and no grade is
        exactly the shape a crashed post-backtest refresh leaves behind — and it
        must stay "pending", not become a "fail" nothing decided.
        """
        from archimedes.api.strategies_routes import _passport_to_strategy_response

        record = _passport_with_verdict(session, "metrics-no-grade", sharpe_ratio=1.9, status="pending")

        assert _passport_to_strategy_response(record, session).rigor_gate_status == "pending"

    def test_a_stored_true_boolean_cannot_outvote_a_non_pass_status(self, session: Session):
        """MUTATION: ``passes_rigor_gate=bool(record.passes_rigor_gate)``.

        A row that predates the coupling (the generation-time fusion verdict
        wrote the boolean; nothing wrote a status) can carry ``True`` beside a
        non-pass four-state. The served boolean is derived from the status, so
        the contradiction cannot reach a badge.
        """
        from archimedes.api.strategies_routes import _passport_to_strategy_response

        record = _passport_with_verdict(session, "legacy-true", sharpe_ratio=1.4, status="degenerate")
        record.passes_rigor_gate = True  # deliberately decoupled, as legacy rows were
        session.commit()

        resp = _passport_to_strategy_response(record, session)

        assert resp.rigor_gate_status == "degenerate"
        assert resp.passes_rigor_gate is False

    def test_the_list_path_agrees_with_the_detail_path(self, session: Session):
        """The list endpoints are a second code path to the same claim.

        MUTATION: make ``_passport_responses`` return anything but the same
        per-row mapping (e.g. re-derive from a cohort read for list callers
        only) — the two answers diverge and this reddens.
        """
        from archimedes.api.strategies_routes import _passport_responses, _passport_to_strategy_response

        degen = _passport_with_verdict(session, "bulk-degen", sharpe_ratio=0.0, status="degenerate")
        pending = _passport_with_verdict(session, "bulk-pending", sharpe_ratio=None, status="pending")

        bulk = {r.id: r.rigor_gate_status for r in _passport_responses([degen, pending], session)}

        assert bulk == {"bulk-degen": "degenerate", "bulk-pending": "pending"}
        assert bulk["bulk-degen"] == _passport_to_strategy_response(degen, session).rigor_gate_status
        assert bulk["bulk-pending"] == _passport_to_strategy_response(pending, session).rigor_gate_status


class TestAFlatSeriesIsGradedDegenerate:
    """#1184's property, proven where it is now enforced: the GRADING EVENT.

    These run the real gate (``verdict_from_returns`` → ``run_rigor_gate``) over
    the exact series shapes #1184 named, persist the verdict through the real
    loader, and read it back through the real route. Nothing is stubbed, so the
    chain writer → column → badge is proven end to end.
    """

    @staticmethod
    def _grade_and_store(session: Session, strategy_id: str, series: list[float]) -> None:
        from archimedes.services.live_rigor_gate import verdict_from_returns
        from archimedes.services.passport_loader import RigorVerdictWrite, _apply_rigor_verdict

        record = session.query(StrategyPassportRecord).filter_by(id=strategy_id).one()
        _apply_rigor_verdict(record, RigorVerdictWrite.from_verdict(verdict_from_returns(strategy_id, series)))
        session.commit()

    def test_a_long_flat_series_grades_degenerate(self, session: Session):
        """MUTATION: store ``verdict.passes`` and re-derive a status from it —
        the boolean is False for BOTH "fail" and "degenerate", so the fourth
        state is lost at the write, not at the read."""
        from archimedes.api.strategies_routes import _passport_to_strategy_response

        record = _passport_with_verdict(session, "grade-flat", sharpe_ratio=None, status="pending")
        self._grade_and_store(session, "grade-flat", [0.0] * 400)

        assert _passport_to_strategy_response(record, session).rigor_gate_status == "degenerate"

    def test_a_SHORT_flat_series_grades_degenerate(self, session: Session):
        """MUTATION: drop ``is_zero_variance_series`` from the gate's degeneracy OR.

        The mirror of the OOS case below, and the half that is only load-bearing
        below ~60 bars, where the OOS slice is too short for
        ``is_oos_zero_variance_series`` to fire:

            n=25  is_zero_variance_series=True  is_oos_zero_variance_series=False

        A zero-trade backtest is exactly this shape — one of the two causes
        #1184 names.
        """
        from archimedes.api.strategies_routes import _passport_to_strategy_response

        record = _passport_with_verdict(session, "grade-flat-short", sharpe_ratio=None, status="pending")
        self._grade_and_store(session, "grade-flat-short", [0.0] * 25)

        assert _passport_to_strategy_response(record, session).rigor_gate_status == "degenerate"

    def test_a_series_flat_only_inside_the_oos_window_grades_degenerate(self, session: Session):
        """MUTATION: drop ``is_oos_zero_variance_series`` from that OR.

        A strategy that stops trading partway through — the other cause #1184
        names — leaves the full series non-constant, so the full-series predicate
        alone misses it.
        """
        from archimedes.api.strategies_routes import _passport_to_strategy_response

        varied = [0.001 * ((i % 7) - 3) for i in range(280)]
        record = _passport_with_verdict(session, "grade-flat-oos", sharpe_ratio=None, status="pending")
        self._grade_and_store(session, "grade-flat-oos", varied + [0.0] * 120)

        assert _passport_to_strategy_response(record, session).rigor_gate_status == "degenerate"

    def test_a_real_varied_series_does_not_grade_degenerate(self, session: Session):
        """CONTROL. MUTATION: label everything degenerate — every test above
        would still pass without this one."""
        from archimedes.api.strategies_routes import _passport_to_strategy_response

        record = _passport_with_verdict(session, "grade-varied", sharpe_ratio=None, status="pending")
        self._grade_and_store(session, "grade-varied", [0.001 * ((i % 11) - 5) for i in range(400)])

        assert _passport_to_strategy_response(record, session).rigor_gate_status == "fail"
