"""Passport Honesty tests — four fixes, one file.

Covers:
1. has_real false-positive: strategy with metrics but no BacktestResultRecord
   must have is_backtest_placeholder=True (has_real derives from persisted row).
2. dsr_p_value plumbing: persisted record carries dsr_p_value from rigor verdict;
   _update_record propagates it; API passport returns the correct number.
3. GET /api/strategies/{id}/returns endpoint:
   - exists + real returns → 200 with daily_returns list
   - exists + no returns  → 404 with {"detail": "no persisted returns"}
   - private strategy as anonymous → 404 (hides existence)
4. BacktestVisualizer wired: the component fetches the /returns endpoint and
   does not fall back to mock data (UI lint gate; no backend test needed here).

Gates:
  env -i HOME=$HOME PATH=$PATH PYTHONPATH=backend python -m pytest \\
      backend/tests/test_passport_honesty.py -q
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from tests.db_isolation import redirect_to_tmp_sqlite

# ── DB isolation fixture ────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _use_tmp_db(tmp_path):
    """Per-test SQLite DB so each test starts empty.

    Setting DATABASE_URL and calling init_db() did not give one (#1243):
    archimedes.db builds engine/SessionLocal once at import, so the env var
    rebinds nothing and get_session() kept resolving the original engine — the
    tmp file was never written to. This file passed under the full suite only
    because an earlier file left a usable engine bound, and failed standalone
    against a stale on-disk schema.
    """
    yield from redirect_to_tmp_sqlite(tmp_path)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _sid() -> str:
    return uuid.uuid4().hex[:16]


def _make_strategy(
    *,
    real_sharpe: float | None = 1.2,
    strategy_id: str | None = None,
) -> object:
    """Fabricate a Strategy dataclass with real_sharpe set but NO backtest row.

    This is the canonical 'metrics-but-no-returns' scenario for Fix 1.
    """
    from archimedes.models.paper_ref import PaperRef
    from archimedes.models.strategy import (
        PositionSizing,
        RebalanceFrequency,
        StrategyPassport,
        StrategyStatus,
    )

    sid = strategy_id or _sid()
    return StrategyPassport(
        id=sid,
        papers=[PaperRef(arxiv_id="2301.99999", title="Test", authors=[])],
        methodology_summary="Test strategy",
        asset_universe=["SPY"],
        position_sizing=PositionSizing.EQUAL_WEIGHT,
        rebalance_frequency=RebalanceFrequency.WEEKLY,
        status=StrategyStatus.CANDIDATE,
        regime_tag="regime_neutral",
        real_sharpe=real_sharpe,
        deflated_sharpe_ratio=0.5,
        dsr_p_value=0.85,
        pbo_score=0.3,
    )


def _passing_returns(n: int = 300) -> list[float]:
    import numpy as np

    return np.random.default_rng(42).normal(0.0015, 0.004, n).tolist()


# ═══════════════════════════════════════════════════════════════════════════
# Fix 1 — has_real false-positive
# ═══════════════════════════════════════════════════════════════════════════


class TestHasRealFalsePositive:
    """has_real must derive from the presence of a BacktestResultRecord, not
    from a metric field (real_sharpe).  A strategy with real_sharpe set but
    no persisted row must report is_backtest_placeholder=True (#passport-honesty).
    """

    def _call_to_strategy_response(self, strategy, mock_bt=None):
        """Call _to_strategy_response with a controlled get_backtest_result.

        ``stored=None`` — no passport row — so the response takes the ungraded
        fallback and resolves its display metrics from the provider, which is
        the chain these tests are about.
        """
        from unittest.mock import MagicMock, patch

        from archimedes.api.strategies_routes import _to_strategy_response

        provider = MagicMock()
        provider.get_backtest_result.return_value = mock_bt

        with patch("archimedes.api.strategies_routes.strategy_provider", return_value=provider):
            return _to_strategy_response(strategy, None)

    def test_metrics_but_no_returns_is_placeholder(self):
        """FAILING before the fix: real_sharpe=1.2 but bt=None → is_backtest_placeholder must be True."""
        s = _make_strategy(real_sharpe=1.2)
        resp = self._call_to_strategy_response(s, mock_bt=None)
        assert resp.is_backtest_placeholder is True, (
            f"Expected is_backtest_placeholder=True when bt=None; got {resp.is_backtest_placeholder}. "
            "has_real must come from the persisted row, not from real_sharpe."
        )

    def test_metrics_with_returns_is_not_placeholder(self):
        """Strategy with a BacktestResultRecord must report is_backtest_placeholder=False."""
        from archimedes.models.backtest import BacktestResult

        s = _make_strategy(real_sharpe=1.2)
        bt = BacktestResult(
            strategy_id=s.id,
            sharpe_ratio=1.2,
            sortino_ratio=1.5,
            max_drawdown=0.1,
            cagr=0.2,
            calmar_ratio=2.0,
            win_rate=0.55,
            profit_factor=1.3,
            total_trades=50,
            avg_holding_period_days=5.0,
            correlation_to_spy=0.3,
            correlation_to_btc=0.0,
            equity_curve=[1.0, 1.05, 1.1],
        )
        resp = self._call_to_strategy_response(s, mock_bt=bt)
        assert resp.is_backtest_placeholder is False

    def test_no_metrics_no_returns_is_placeholder(self):
        """A strategy with neither real_sharpe nor bt must also be placeholder."""
        s = _make_strategy(real_sharpe=None)
        resp = self._call_to_strategy_response(s, mock_bt=None)
        assert resp.is_backtest_placeholder is True

    def test_fixture_metrics_preserved_when_bt_absent(self):
        """When bt=None, the display metrics should still use s.real_* from fixture
        (not be silently None).  This verifies the companion metric-chain fix."""
        s = _make_strategy(real_sharpe=1.23)
        resp = self._call_to_strategy_response(s, mock_bt=None)
        # is_backtest_placeholder correctly True (no bt row)
        assert resp.is_backtest_placeholder is True
        # But the stored metric is still surfaced (fixture data is real data)
        assert resp.sharpe_ratio == pytest.approx(1.23)


# ═══════════════════════════════════════════════════════════════════════════
# Fix 2 — dsr_p_value plumbing
# ═══════════════════════════════════════════════════════════════════════════


class TestGateNumbersTravelWithTheGrade:
    """The four gate numbers reach the row through the GRADE, and only the grade.

    Regression history, in two layers. First (#passport-honesty):
    ``_persist_candidate``'s initial write omitted ``dsr_p_value`` and
    ``_update_record`` did not propagate it, so ``strategy_passports.dsr_p_value``
    stayed NULL even after a backtest ran. Then (#1746 / PR-B): propagating them
    off the passport DATACLASS turned out to be the wrong mechanism — DSR, its
    p-value, PBO and the OOS Sharpe are what a gate run PRODUCED, so a
    ``force_update`` refresh carrying no grade (the curated boot-time passport
    sync runs one on every process start) could overwrite a real grade's numbers
    with a fixture's, or with NULL. They now move only through
    ``RigorVerdictWrite`` → ``_apply_rigor_verdict``, together with the verdict
    the same run produced.
    """

    def _make_session(self, tmp_path):
        """Return a fresh SQLAlchemy session backed by an on-disk SQLite DB."""
        from archimedes.models.chat import Base
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        engine = create_engine(f"sqlite:///{tmp_path}/dsr_test.db")
        Base.metadata.create_all(engine)
        return sessionmaker(bind=engine)()

    def _passport(self, sid: str, **kw):
        from archimedes.models.paper_ref import PaperRef
        from archimedes.models.strategy import (
            PositionSizing,
            RebalanceFrequency,
            StrategyPassport,
            StrategyStatus,
        )

        base = {
            "id": sid,
            "papers": [PaperRef(arxiv_id="2301.00001", title="DSR Test", authors=[])],
            "methodology_summary": "DSR plumbing test",
            "asset_universe": ["SPY"],
            "position_sizing": PositionSizing.EQUAL_WEIGHT,
            "rebalance_frequency": RebalanceFrequency.WEEKLY,
            "status": StrategyStatus.CANDIDATE,
            "regime_tag": "regime_neutral",
        }
        base.update(kw)
        return StrategyPassport(**base)

    def test_a_grade_writes_the_numbers_its_own_run_produced(self, tmp_path):
        """MUTATION: drop the four ``record.<field> = verdict.<field>`` lines from
        ``_apply_rigor_verdict``. The numbers never reach the row and the API
        serves NULLs beside a real verdict — the #passport-honesty defect, one
        layer down."""
        from archimedes.services.passport_loader import RigorVerdictWrite, ingest_passport

        session = self._make_session(tmp_path)
        record = ingest_passport(
            session,
            self._passport("dsr-test-001"),
            generation_method="fusion",
            rigor_verdict=RigorVerdictWrite(
                status="fail",
                cohort_n=1,
                dsr_p_value=0.941561,
                deflated_sharpe_ratio=0.451103,
                pbo_score=0.550894,
                out_of_sample_sharpe=0.31,
            ),
        )
        session.flush()

        assert record.dsr_p_value == pytest.approx(0.941561)
        assert record.deflated_sharpe_ratio == pytest.approx(0.451103)
        assert record.pbo_score == pytest.approx(0.550894)
        assert record.out_of_sample_sharpe == pytest.approx(0.31)
        assert record.rigor_gate_status == "fail", "the numbers and the verdict are one write"

    def test_the_passport_dataclass_cannot_write_them(self, tmp_path):
        """A passport carrying gate numbers but no grade writes NONE of them.

        This is the #1746 half. A curated ``Strategy`` carries the #1187 FIXTURE
        snapshot in exactly these four fields; letting the dataclass write them
        is how a fixture number ended up in a column that names a gate.

        MUTATION: restore ``record.dsr_p_value = passport.dsr_p_value`` (and its
        three siblings) in ``_update_record``, or the same four in the INSERT
        branch. The fixture value lands in the row and this reddens.
        """
        from archimedes.services.passport_loader import ingest_passport

        session = self._make_session(tmp_path)
        record = ingest_passport(
            session,
            self._passport("dsr-test-fixture", dsr_p_value=0.941561, deflated_sharpe_ratio=0.451103),
            generation_method="curated",
            force_update=True,
        )
        session.flush()

        assert record.dsr_p_value is None
        assert record.deflated_sharpe_ratio is None
        assert record.rigor_gate_status == "pending", "no grade ran, so the row says so"

    def test_a_refresh_without_a_grade_cannot_overwrite_a_graded_number(self, tmp_path):
        """The boot-time curated passport sync runs ``force_update=True`` on every
        process start, with the strategy files' fixture values on the dataclass.
        It must not be able to touch what a gate decided.

        MUTATION: same as above — restore the four ``record.<field> =
        passport.<field>`` lines in ``_update_record``. The refresh replaces
        0.941561 with the fixture's 0.111111 and this reddens.
        """
        from archimedes.services.passport_loader import RigorVerdictWrite, ingest_passport

        session = self._make_session(tmp_path)
        graded = ingest_passport(
            session,
            self._passport("dsr-test-002"),
            generation_method="curated",
            force_update=True,
            rigor_verdict=RigorVerdictWrite(status="pass", cohort_n=3, dsr_p_value=0.941561),
        )
        session.flush()
        assert graded.dsr_p_value == pytest.approx(0.941561)

        refreshed = ingest_passport(
            session,
            self._passport("dsr-test-002", dsr_p_value=0.111111, real_sharpe=1.4),
            generation_method="curated",
            force_update=True,
        )
        session.flush()

        assert refreshed.id == graded.id, "force_update must update the existing row"
        assert refreshed.dsr_p_value == pytest.approx(0.941561), "a gradeless refresh overwrote a graded number"
        assert refreshed.rigor_gate_status == "pass", "…and it must not have touched the verdict either"
        assert refreshed.sharpe_ratio == pytest.approx(1.4), "the DISPLAY metrics are still refreshed, on purpose"

    @pytest.mark.asyncio
    async def test_api_returns_dsr_p_value_from_persisted_record(self, tmp_path, monkeypatch):
        """GET /api/strategies/{id} returns dsr_p_value from the persisted grade.

        Simulates the prod scenario: a fusion strategy whose post-backtest grade
        stored its numbers on strategy_passports.
        """
        from archimedes.db import get_session, init_db
        from archimedes.main import app
        from archimedes.models.strategy_store import upsert_strategy
        from archimedes.services.passport_loader import RigorVerdictWrite, ingest_passport

        db_path = tmp_path / "dsr_api.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
        init_db()

        with get_session() as session:
            # Create a strategy_store row via the proper upsert (handles content_hash)
            row = upsert_strategy(
                session,
                generation_method="fusion",
                strategy_name="DSR Test Strategy",
                thesis="Test",
                source_papers=[{"arxiv_id": "2301.99999"}],
                asset_universe=["SPY"],
                is_example=True,
            )
            session.flush()
            sid = row.id

            ingest_passport(
                session,
                self._passport(sid, real_sharpe=1.1),
                generation_method="fusion",
                rigor_verdict=RigorVerdictWrite(
                    status="fail",
                    cohort_n=1,
                    dsr_p_value=0.941561,
                    deflated_sharpe_ratio=0.451103,
                    pbo_score=0.550894,
                ),
            )
            session.commit()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(f"/api/strategies/{sid}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["dsr_p_value"] == pytest.approx(0.941561), (
            f"Expected dsr_p_value=0.941561 from persisted passport; got {body['dsr_p_value']}"
        )
        assert body["deflated_sharpe_ratio"] == pytest.approx(0.451103)
        assert body["pbo_score"] == pytest.approx(0.550894)


# ═══════════════════════════════════════════════════════════════════════════
# universe_source plumbing (#857, follow-up to #855/#847)
# ═══════════════════════════════════════════════════════════════════════════


class TestUniverseSourcePlumbing:
    """universe_source ("user" | "model" | "full") reaches the passport API.

    Mirrors TestDsrPValuePlumbing's shape: the fusion backtest universe can
    come from three places (#855), and the passport must say which one was
    used so a model-picked universe (a mild look-ahead channel) is auditable.
    """

    def _make_session(self, tmp_path):
        from archimedes.models.chat import Base
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        engine = create_engine(f"sqlite:///{tmp_path}/universe_source_test.db")
        Base.metadata.create_all(engine)
        return sessionmaker(bind=engine)()

    def test_ingest_passport_with_universe_source(self, tmp_path):
        """A passport with universe_source persisted correctly on first insert."""
        from archimedes.models.paper_ref import PaperRef
        from archimedes.models.strategy import (
            PositionSizing,
            RebalanceFrequency,
            StrategyPassport,
            StrategyStatus,
        )
        from archimedes.services.passport_loader import ingest_passport

        session = self._make_session(tmp_path)
        passport = StrategyPassport(
            id="universe-source-test-001",
            papers=[PaperRef(arxiv_id="2301.00003", title="Universe Source Test", authors=[])],
            methodology_summary="universe_source plumbing test",
            asset_universe=["QQQ", "TLT"],
            universe_source="model",
            position_sizing=PositionSizing.EQUAL_WEIGHT,
            rebalance_frequency=RebalanceFrequency.WEEKLY,
            status=StrategyStatus.CANDIDATE,
            regime_tag="regime_neutral",
        )
        record = ingest_passport(session, passport, generation_method="fusion")
        session.flush()

        assert record.universe_source == "model", (
            f"Expected universe_source='model' after first ingest; got {record.universe_source!r}"
        )

    def test_update_record_propagates_universe_source(self, tmp_path):
        """force_update=True must propagate universe_source without clobbering
        it with None on a later refresh that doesn't carry the field (mirrors
        the dsr_p_value force_update regression)."""
        from archimedes.models.paper_ref import PaperRef
        from archimedes.models.strategy import (
            PositionSizing,
            RebalanceFrequency,
            StrategyPassport,
            StrategyStatus,
        )
        from archimedes.services.passport_loader import ingest_passport

        session = self._make_session(tmp_path)

        passport_v1 = StrategyPassport(
            id="universe-source-test-002",
            papers=[PaperRef(arxiv_id="2301.00004", title="Universe Source Refresh Test", authors=[])],
            methodology_summary="universe_source update test",
            asset_universe=["SPY", "IWM"],
            universe_source="user",
            position_sizing=PositionSizing.EQUAL_WEIGHT,
            rebalance_frequency=RebalanceFrequency.WEEKLY,
            status=StrategyStatus.CANDIDATE,
            regime_tag="regime_neutral",
        )
        r1 = ingest_passport(session, passport_v1, generation_method="fusion", force_update=True)
        session.flush()
        assert r1.universe_source == "user"

        # A later refresh (mimics _refresh_passport_real_metrics, which does not
        # thread universe_source through StrategyPassport's real_* backtest-only
        # update) must not clobber the already-stored value with None.
        passport_v2 = StrategyPassport(
            id="universe-source-test-002",
            papers=[PaperRef(arxiv_id="2301.00004", title="Universe Source Refresh Test", authors=[])],
            methodology_summary="universe_source update test",
            asset_universe=["SPY", "IWM"],
            universe_source=None,
            deflated_sharpe_ratio=0.451103,
            position_sizing=PositionSizing.EQUAL_WEIGHT,
            rebalance_frequency=RebalanceFrequency.WEEKLY,
            status=StrategyStatus.CANDIDATE,
            regime_tag="regime_neutral",
        )
        r2 = ingest_passport(session, passport_v2, generation_method="fusion", force_update=True)
        session.flush()

        assert r2.universe_source == "user", (
            f"Expected universe_source to survive a refresh that doesn't carry it; got {r2.universe_source!r}"
        )
        assert r2.id == r1.id, "force_update must update the existing row, not insert a new one"

    @pytest.mark.asyncio
    async def test_api_returns_universe_source_from_persisted_record(self, tmp_path, monkeypatch):
        """GET /api/strategies/{id} returns universe_source with one of the
        three provenance values (#857 acceptance criterion 2)."""
        from archimedes.db import get_session, init_db
        from archimedes.main import app
        from archimedes.models.paper_ref import PaperRef
        from archimedes.models.strategy import (
            PositionSizing,
            RebalanceFrequency,
            StrategyPassport,
            StrategyStatus,
        )
        from archimedes.models.strategy_store import upsert_strategy
        from archimedes.services.passport_loader import ingest_passport

        db_path = tmp_path / "universe_source_api.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
        init_db()

        with get_session() as session:
            row = upsert_strategy(
                session,
                generation_method="fusion",
                strategy_name="Universe Source Test Strategy",
                thesis="Test",
                source_papers=[{"arxiv_id": "2301.00005"}],
                asset_universe=["QQQ", "GLD"],
                is_example=True,
            )
            session.flush()
            sid = row.id

            passport = StrategyPassport(
                id=sid,
                papers=[PaperRef(arxiv_id="2301.00005", title="Universe Source API Test", authors=[])],
                methodology_summary="universe_source API test",
                asset_universe=["QQQ", "GLD"],
                universe_source="model",
                position_sizing=PositionSizing.EQUAL_WEIGHT,
                rebalance_frequency=RebalanceFrequency.WEEKLY,
                status=StrategyStatus.CANDIDATE,
                regime_tag="regime_neutral",
            )
            ingest_passport(session, passport, generation_method="fusion")
            session.commit()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(f"/api/strategies/{sid}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["universe_source"] in ("user", "model", "full"), (
            f"Expected universe_source in ('user','model','full'); got {body.get('universe_source')!r}"
        )
        assert body["universe_source"] == "model"


# ═══════════════════════════════════════════════════════════════════════════
# Fix 3 — GET /api/strategies/{id}/returns endpoint
# ═══════════════════════════════════════════════════════════════════════════


class TestStrategyReturnsEndpoint:
    """Hermetic tests for GET /api/strategies/{id}/returns."""

    def _insert_strategy_and_backtest(
        self,
        session,
        sid: str,
        returns: list[float],
        *,
        owner_wallet: str | None = None,
        is_example: bool = True,
        is_published: bool = True,
    ):
        """Insert a strategy_store + strategy_passports row + BacktestResultRecord."""
        import json as _json

        from archimedes.models.backtest import BacktestResult
        from archimedes.models.paper_ref import PaperRef
        from archimedes.models.strategy import (
            PositionSizing,
            RebalanceFrequency,
            StrategyPassport,
            StrategyStatus,
        )
        from archimedes.models.strategy_store import StrategyRecord
        from archimedes.services.backtest_repository import insert_backtest_if_missing
        from archimedes.services.passport_loader import ingest_passport

        # Use a fake but valid-format content_hash derived from the sid.
        fake_hash = ("0x" + sid).ljust(66, "0")
        row = StrategyRecord(
            id=sid,
            content_hash=fake_hash,
            generation_method="fusion",
            source_papers="[]",
            strategy_name=f"Test {sid[:8]}",
            thesis="Returns endpoint test",
            asset_universe='["SPY"]',
            risk_profile="moderate",
            status="candidate",
            is_example=is_example,
            is_published=is_published,
            owner_wallet=owner_wallet,
        )
        session.add(row)
        session.flush()

        passport = StrategyPassport(
            id=sid,
            # Unique arxiv_id + summary per sid so ingest_passport's content_hash
            # is distinct across tests that share the autouse DB.
            papers=[PaperRef(arxiv_id=f"2301.{sid[:8]}", title=f"Returns Test {sid[:4]}", authors=[])],
            methodology_summary=f"Returns endpoint test {sid}",
            asset_universe=["SPY"],
            position_sizing=PositionSizing.EQUAL_WEIGHT,
            rebalance_frequency=RebalanceFrequency.WEEKLY,
            status=StrategyStatus.CANDIDATE,
            regime_tag="regime_neutral",
        )
        ingest_passport(session, passport, generation_method="fusion")

        if returns:
            equity = [1.0]
            for r in returns:
                equity.append(equity[-1] * (1.0 + r))
            result = BacktestResult(
                strategy_id=sid,
                sharpe_ratio=1.0,
                sortino_ratio=1.2,
                max_drawdown=0.1,
                cagr=0.15,
                calmar_ratio=1.5,
                win_rate=0.55,
                profit_factor=1.2,
                total_trades=20,
                avg_holding_period_days=5.0,
                correlation_to_spy=0.3,
                correlation_to_btc=0.0,
                equity_curve=equity,
                # Required: insert_backtest_if_missing refuses an unattributed
                # row, so a row can always be traced to its producing engine.
                backtest_engine="backtrader",
            )
            artifact = {"results": [{"metrics": {"daily_returns": returns}}]}
            import hashlib

            artifact_json = _json.dumps(artifact)
            content_hash = hashlib.sha256(artifact_json.encode()).hexdigest()
            insert_backtest_if_missing(
                session,
                strategy_id=sid,
                content_hash=content_hash,
                result=result,
                artifact_json=artifact_json,
                source_pipeline="test",
            )

        session.commit()

    @pytest.mark.asyncio
    async def test_exists_with_returns_200(self, tmp_path, monkeypatch):
        """Strategy with persisted daily returns → 200 with correct payload."""
        from archimedes.db import get_session, init_db
        from archimedes.main import app

        db_path = tmp_path / "returns_200.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
        init_db()

        sid = "ret-" + _sid()
        returns = _passing_returns(50)

        with get_session() as session:
            self._insert_strategy_and_backtest(session, sid, returns)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(f"/api/strategies/{sid}/returns")

        assert resp.status_code == 200, f"Expected 200; got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["strategy_id"] == sid
        assert body["source"] == "persisted_backtest"
        assert body["n"] == len(returns)
        assert body["n"] == len(body["daily_returns"])
        assert body["daily_returns"] == pytest.approx(returns, rel=1e-6)
        # owner_wallet must NOT appear in the response
        assert "owner_wallet" not in body

    @pytest.mark.asyncio
    async def test_exists_no_returns_404(self, tmp_path, monkeypatch):
        """Strategy exists but has no BacktestResultRecord → 404 with JSON error."""
        from archimedes.db import get_session, init_db
        from archimedes.main import app

        db_path = tmp_path / "returns_404.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
        init_db()

        sid = "ret-" + _sid()

        with get_session() as session:
            self._insert_strategy_and_backtest(session, sid, [])  # no returns

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(f"/api/strategies/{sid}/returns")

        assert resp.status_code == 404, f"Expected 404; got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body.get("detail") == "no persisted returns", f"Expected detail='no persisted returns'; got {body}"

    @pytest.mark.asyncio
    async def test_nonexistent_strategy_404(self, tmp_path, monkeypatch):
        """Strategy does not exist → 404."""
        from archimedes.db import init_db
        from archimedes.main import app

        db_path = tmp_path / "returns_nonexistent.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
        init_db()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/strategies/does-not-exist-123/returns")

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_private_strategy_as_anonymous_404(self, tmp_path, monkeypatch):
        """Private (not is_example, not is_published) strategy → 404 for anonymous caller.

        404-hides-existence: a non-owner must not learn whether the strategy exists.
        """
        from archimedes.db import get_session, init_db
        from archimedes.main import app

        db_path = tmp_path / "returns_private.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
        init_db()

        sid = "prv-" + _sid()
        owner = "0x" + "a" * 40

        with get_session() as session:
            self._insert_strategy_and_backtest(
                session,
                sid,
                _passing_returns(50),
                owner_wallet=owner,
                is_example=False,
                is_published=False,
            )

        # Anonymous caller — no SIWE session — must get 404
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(f"/api/strategies/{sid}/returns")

        assert resp.status_code == 404, f"Private strategy must 404 for anonymous caller; got {resp.status_code}"
