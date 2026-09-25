"""Tests for the unified passport loader + StrategyPassportRecord ORM.

All tests use an in-memory SQLite DB — no real Postgres needed.
"""

from __future__ import annotations

import json

import pytest
from archimedes.models.chat import Base
from archimedes.models.paper_ref import PaperRef
from archimedes.models.strategy import (
    PositionSizing,
    RebalanceFrequency,
    StrategyPassport,
    StrategyStatus,
)
from archimedes.models.strategy_passport_record import (
    StrategyPassportRecord,
)
from archimedes.services.passport_loader import (
    get_passport,
    ingest_all_curated,
    ingest_passport,
    list_passports,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture
def session():
    """In-memory SQLite session with all tables created."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    sess = SessionLocal()
    yield sess
    sess.close()


def _make_passport(
    id: str = "test-001",
    title: str = "Test Strategy",
    regime: str = "bull",
    status: str = "candidate",
) -> StrategyPassport:
    return StrategyPassport(
        id=id,
        papers=[
            PaperRef(
                arxiv_id=f"2301.{id}",  # unique per ID so content_hash differs
                title=title,
                authors=["Alice", "Bob"],
                doi="10.1234/test",
                venue="Journal of Testing",
                year=2023,
                citation_count=42,
            )
        ],
        methodology_summary="A simple trend-following strategy.",
        asset_universe=["SPY", "QQQ"],
        position_sizing=PositionSizing.EQUAL_WEIGHT,
        rebalance_frequency=RebalanceFrequency.DAILY,
        status=StrategyStatus(status),
        regime_tag=regime,
        real_sharpe=1.23,
        real_sortino=1.45,
        real_max_dd=0.15,
        passes_rigor_gate=True,
        deflated_sharpe_ratio=0.89,
        pbo_score=0.25,
        kelly_fraction=0.42,
    )


class TestIngestPassport:
    def test_basic_ingest(self, session: Session):
        passport = _make_passport()
        record = ingest_passport(session, passport, generation_method="curated")
        session.commit()

        assert record.id == "test-001"
        assert record.generation_method == "curated"
        assert record.methodology_summary == "A simple trend-following strategy."
        assert record.regime_tag == "bull"
        # UNGRADED, despite the dataclass carrying passes_rigor_gate=True. The
        # verdict of record does not travel on the passport dataclass: it is
        # written only from an explicit rigor_verdict= argument
        # (docs/adr/rigor-verdict-of-record.md). This assertion used to read
        # `is True`, which is exactly how the generation-time fusion verdict
        # became every generated strategy's badge (#1747).
        assert record.passes_rigor_gate is False
        assert record.rigor_gate_status == "pending"
        assert record.graded_at is None
        assert record.gate_version is None
        assert record.sharpe_ratio == 1.23
        assert record.sortino_ratio == 1.45
        # …and the four GATE numbers do not travel on the dataclass either
        # (#1746 / PR-B). They are the output of a gate run, so they move with
        # the verdict and nothing else writes them. On a curated passport those
        # dataclass fields carry the #1187 fixture snapshot, which is exactly
        # what must not end up in a column that names a gate.
        assert record.deflated_sharpe_ratio is None
        assert record.pbo_score is None

    def test_an_explicit_verdict_is_what_grades_a_row(self, session: Session):
        """The other half of the contract above: a caller that IS the grading
        event supplies a RigorVerdictWrite, and all NINE columns move together.

        MUTATION: drop the `rigor_verdict` argument — every assert below reddens,
        which is what makes the `is False` assertion above a contract rather than
        a bug.
        """
        from archimedes.services.passport_loader import RigorVerdictWrite
        from archimedes.services.rigor_gate_version import gate_version

        record = ingest_passport(
            session,
            _make_passport(id="graded-001"),
            generation_method="fusion",
            rigor_verdict=RigorVerdictWrite(
                status="pass",
                cohort_n=1,
                deflated_sharpe_ratio=0.71,
                dsr_p_value=0.98,
                pbo_score=0.12,
                out_of_sample_sharpe=0.63,
            ),
        )
        session.commit()

        assert record.rigor_gate_status == "pass"
        assert record.passes_rigor_gate is True
        assert record.graded_at is not None
        assert record.gate_version == gate_version()
        assert record.cohort_n == 1
        # The gate numbers this run produced — the dataclass's fixture values
        # (0.89 / 0.25) must not be what landed.
        assert record.deflated_sharpe_ratio == 0.71
        assert record.dsr_p_value == 0.98
        assert record.pbo_score == 0.12
        assert record.out_of_sample_sharpe == 0.63

    def test_an_ungraded_verdict_carries_no_provenance_and_no_numbers(self, session: Session):
        """``pending`` means NO GATE RAN, so there is nothing to date, nothing to
        attribute and nothing to show.

        The column's own contract says so — ``graded_at`` NULL ⟺
        ``rigor_gate_status == "pending"`` — and the verdict-of-record migration
        honours it for every row it derived. This makes it true of WRITES too, so
        ``graded_at is not None`` is a reliable test for "a gate produced these
        numbers" and a never-graded row cannot carry a fabricated timestamp.

        MUTATION: delete the ``if self.status == STATUS_PENDING`` branch from
        ``RigorVerdictWrite.__post_init__``. Every ungraded curated row starts
        claiming it was graded, now, by the current gate.
        """
        from archimedes.services.passport_loader import RigorVerdictWrite

        record = ingest_passport(
            session,
            _make_passport(id="ungraded-001"),
            generation_method="curated",
            rigor_verdict=RigorVerdictWrite(status="pending", cohort_n=7, dsr_p_value=0.5),
        )
        session.commit()

        assert record.rigor_gate_status == "pending"
        assert record.passes_rigor_gate is False
        assert record.graded_at is None
        assert record.gate_version is None
        assert record.cohort_n is None, "graded against nothing — never a guessed cohort"
        assert record.dsr_p_value is None, "a run that did not happen produced no numbers"

    def test_paper_refs_persisted(self, session: Session):
        passport = _make_passport()
        record = ingest_passport(session, passport)
        session.commit()

        refs = record.paper_refs
        assert len(refs) == 1
        assert refs[0].arxiv_id == "2301.test-001"
        assert refs[0].title == "Test Strategy"
        assert json.loads(refs[0].authors) == ["Alice", "Bob"]
        assert refs[0].year == 2023

    def test_idempotent_skip(self, session: Session):
        passport = _make_passport()
        r1 = ingest_passport(session, passport)
        session.commit()
        r2 = ingest_passport(session, passport)
        session.commit()

        assert r1.id == r2.id
        # Only one row
        count = session.query(StrategyPassportRecord).count()
        assert count == 1

    def test_force_update(self, session: Session):
        passport = _make_passport()
        ingest_passport(session, passport, generation_method="curated")
        session.commit()

        # Update with new data
        passport.real_sharpe = 2.0
        passport.regime_tag = "bear"
        ingest_passport(session, passport, generation_method="curated", force_update=True)
        session.commit()

        record = session.query(StrategyPassportRecord).filter_by(id="test-001").first()
        assert record.sharpe_ratio == 2.0
        assert record.regime_tag == "bear"

    def test_multiple_strategies(self, session: Session):
        for i in range(5):
            passport = _make_passport(id=f"strat-{i}", title=f"Strategy {i}")
            ingest_passport(session, passport)
        session.commit()

        count = session.query(StrategyPassportRecord).count()
        assert count == 5

    def test_multi_paper_passport(self, session: Session):
        passport = _make_passport()
        passport.papers.append(
            PaperRef(
                arxiv_id="2302.56789",
                title="Second Paper",
                authors=["Charlie"],
                year=2024,
            )
        )
        record = ingest_passport(session, passport)
        session.commit()

        assert len(record.paper_refs) == 2
        arxiv_ids = {r.arxiv_id for r in record.paper_refs}
        assert arxiv_ids == {"2301.test-001", "2302.56789"}


class TestToStrategyPassport:
    def test_roundtrip(self, session: Session):
        from archimedes.services.passport_loader import RigorVerdictWrite

        original = _make_passport()
        # Graded explicitly, because that is now the only way a verdict reaches
        # the row — a round-trip through the ungraded default would compare
        # False against False and prove nothing about the verdict field.
        record = ingest_passport(session, original, rigor_verdict=RigorVerdictWrite(status="pass"))
        session.commit()

        restored = record.to_strategy_passport()
        assert restored.id == original.id
        assert restored.methodology_summary == original.methodology_summary
        assert restored.regime_tag == original.regime_tag
        assert restored.passes_rigor_gate is True
        assert restored.real_sharpe == original.real_sharpe
        assert len(restored.papers) == 1
        assert restored.papers[0].arxiv_id == "2301.test-001"


class TestListPassports:
    def test_filter_by_status(self, session: Session):
        ingest_passport(session, _make_passport(id="a", status="candidate"))
        ingest_passport(session, _make_passport(id="b", status="live"))
        ingest_passport(session, _make_passport(id="c", status="live"))
        session.commit()

        live = list_passports(session, status="live")
        assert len(live) == 2

    def test_filter_by_regime(self, session: Session):
        ingest_passport(session, _make_passport(id="a", regime="bull"))
        ingest_passport(session, _make_passport(id="b", regime="bear"))
        ingest_passport(session, _make_passport(id="c", regime="bull"))
        session.commit()

        bears = list_passports(session, regime_tag="bear")
        assert len(bears) == 1
        assert bears[0].id == "b"


class TestGetPassport:
    def test_found(self, session: Session):
        ingest_passport(session, _make_passport(id="find-me"))
        session.commit()

        result = get_passport(session, "find-me")
        assert result is not None
        assert result.id == "find-me"

    def test_not_found(self, session: Session):
        result = get_passport(session, "nonexistent")
        assert result is None


class TestBulkIngest:
    def test_ingest_all_curated(self, session: Session):
        strategies = [_make_passport(id=f"bulk-{i}") for i in range(4)]
        count = ingest_all_curated(session, strategies)
        assert count == 4

        total = session.query(StrategyPassportRecord).count()
        assert total == 4
