"""The passport is the rigor verdict of record — the write side and the read side.

Owner decision (Dan, 2026-09-01, ``docs/adr/rigor-verdict-of-record.md``):
generation, backtesting and grading are one-time events. A strategy is graded
ONCE, at backtest time, by the real gate; the verdict is persisted on the
passport with its provenance; every surface reads the stored verdict. A re-grade
is an explicit, versioned event — never a silent overwrite, never a recompute on
read.

What #1746/#1747 actually were: ``strategy_passports.passes_rigor_gate`` was
MIXED-VINTAGE. Its first writer was the generation-time FUSION verdict
(``_persist_candidate``), replaced by the post-backtest re-grade only when that
happened to run — and the Library's Generated tab did not read the column at
all, serving ``StrategyRecord.status`` and ``rigor_verdict`` instead. Twenty-one
rows read "Live ✓" in the Library while their own passports read "Reference only
— gate failed", with no shared field between the two answers.

Every test here names the mutation that reddens it. Hermetic: tmp-sqlite, no
network, no .env.

Run:
  /opt/homebrew/Caskroom/mambaforge/base/envs/archimedes/bin/pytest -q \\
      -p no:cacheprovider backend/tests/test_rigor_verdict_of_record.py
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import archimedes.db as db
import pytest
from archimedes.api.auth_siwe import _COOKIE_NAME, _sign_session
from archimedes.models.paper_ref import PaperRef
from archimedes.models.strategy import StrategyPassport, StrategyStatus
from archimedes.services.live_rigor_gate import RigorGateVerdict, verdict_from_returns
from archimedes.services.passport_loader import (
    RigorVerdictWrite,
    get_passport,
    ingest_passport,
)
from archimedes.services.rigor_gate_version import LEGACY_DERIVED, gate_version
from httpx import ASGITransport, AsyncClient

_W_OWNER = "0xAbC0000000000000000000000000000000000042"

# A return series the real gate grades and fails (non-degenerate, weak).
_VARIED = [0.001 * ((i % 11) - 5) for i in range(400)]


@pytest.fixture(autouse=True)
def _use_tmp_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    url = f"sqlite:///{tmp_path / 'verdict_of_record.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    eng = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", eng)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=eng, autocommit=False, autoflush=False))
    db.init_db()
    yield


def _siwe_cookies(wallet: str) -> dict[str, str]:
    return {_COOKIE_NAME: _sign_session(wallet, time.time())}


def _passport(strategy_id: str, **kw) -> StrategyPassport:
    """A passport dataclass shaped like the one ``_persist_candidate`` builds."""
    base = {
        "id": strategy_id,
        "papers": [PaperRef(arxiv_id="2301.00042", title="A Paper", authors=[])],
        "methodology_summary": f"methodology for {strategy_id}",
        "asset_universe": ["SPY"],
        "status": StrategyStatus.CANDIDATE,
        "regime_tag": "regime_neutral",
    }
    base.update(kw)
    return StrategyPassport(**base)


# ═══════════════════════════════════════════════════════════════════════════
# 1. SINGLE WRITER — the fusion verdict cannot reach the passport verdict
# ═══════════════════════════════════════════════════════════════════════════


class TestTheFusionVerdictNeverReachesThePassportVerdict:
    """``ingest_passport`` reads ``passport.passes_rigor_gate`` NOWHERE.

    This is the structural half of the fix. ``_persist_candidate`` used to set
    that field from ``c.rigor_verdict["passing"]`` — the synthesis gate's answer,
    computed before any backtest existed — and ``ingest_passport`` copied it
    straight into the column every read surface treats as the strategy's grade.
    Removing the line from the caller alone would leave the door open; the door
    itself is closed here.
    """

    def test_a_passing_fusion_verdict_lands_as_pending_not_pass(self):
        """MUTATION: restore ``passes_rigor_gate=passport.passes_rigor_gate`` in
        ``ingest_passport``'s insert branch. This reddens on all three asserts."""
        with db.get_session() as session:
            ingest_passport(
                session,
                # Exactly the shape the pre-fix _persist_candidate handed in.
                _passport("fusion-said-yes", passes_rigor_gate=True, deflated_sharpe_ratio=0.81),
                generation_method="fusion",
                force_update=True,
            )
            session.commit()
            row = get_passport(session, "fusion-said-yes")

            assert row.rigor_gate_status == "pending", "no gate has graded this strategy"
            assert row.passes_rigor_gate is False
            assert row.graded_at is None
            assert row.gate_version is None

    def test_a_refresh_without_a_grade_does_not_erase_a_stored_grade(self):
        """MUTATION: make ``_update_record`` write the verdict columns again
        (from the dataclass, or to their defaults).

        A ``force_update`` refresh that carries no grade — the curated sync, a
        metadata backfill — must leave the verdict alone. Erasing it would
        silently un-grade a strategy; rewriting it from the dataclass is how the
        fusion verdict got in.
        """
        with db.get_session() as session:
            ingest_passport(session, _passport("graded-then-refreshed"), generation_method="fusion", force_update=True)
            ingest_passport(
                session,
                _passport("graded-then-refreshed"),
                generation_method="fusion",
                force_update=True,
                rigor_verdict=RigorVerdictWrite(status="pass", cohort_n=1),
            )
            session.commit()

            # A later refresh with NO verdict, and a contradictory dataclass flag.
            ingest_passport(
                session,
                _passport("graded-then-refreshed", passes_rigor_gate=False),
                generation_method="fusion",
                force_update=True,
            )
            session.commit()
            row = get_passport(session, "graded-then-refreshed")

            assert row.rigor_gate_status == "pass"
            assert row.passes_rigor_gate is True
            assert row.graded_at is not None

    def test_a_curated_row_is_pending_not_fail(self):
        """The #821 placeholder is not a verdict.

        MUTATION: have the curated sync path write ``passes_rigor_gate`` through
        again. Every curated row carries a hardcoded ``False``
        (``strategy_provider.py``, deliberately fail-closed) which is a
        placeholder, not a gate result — serving it as "fail" asserts that a gate
        ran and the strategy lost. Grading curated strategies for real is PR-B.
        """
        with db.get_session() as session:
            ingest_passport(
                session,
                _passport("curated-placeholder", passes_rigor_gate=False),
                generation_method="curated",
                force_update=True,
            )
            session.commit()
            row = get_passport(session, "curated-placeholder")

            assert row.rigor_gate_status == "pending"
            assert row.rigor_gate_status != "fail"


# ═══════════════════════════════════════════════════════════════════════════
# 2. THE GRADING EVENT — coupled, with provenance
# ═══════════════════════════════════════════════════════════════════════════


class TestTheGradingEventWritesACoupledVerdictWithProvenance:
    def test_passes_and_status_cannot_be_constructed_apart(self):
        """MUTATION: give ``RigorVerdictWrite`` a ``passes`` FIELD instead of a
        derived property. The pair could then be set independently — which is
        precisely the state the old column was in."""
        assert RigorVerdictWrite(status="pass").passes is True
        for status in ("fail", "pending", "degenerate"):
            assert RigorVerdictWrite(status=status).passes is False

    def test_an_unknown_status_is_refused_at_construction(self):
        """MUTATION: drop the ``__post_init__`` membership check. A typo'd or
        invented state would then reach a NOT NULL column and every surface would
        render an unrecognised verdict."""
        with pytest.raises(ValueError, match="rigor_gate_status must be one of"):
            RigorVerdictWrite(status="passed")

    def test_provenance_is_filled_in_even_when_the_caller_omits_it(self):
        """MUTATION: drop the ``__post_init__`` defaults. A verdict with a NULL
        ``gate_version`` is a verdict nobody can date or compare."""
        v = RigorVerdictWrite(status="fail")
        assert v.gate_version == gate_version()
        assert v.gate_version != LEGACY_DERIVED
        assert isinstance(v.graded_at, datetime)

    def test_from_verdict_carries_the_live_gates_four_state_through(self):
        """MUTATION: build from ``verdict.passes`` instead of ``verdict.status``.

        The boolean is False for BOTH "fail" and "degenerate", so a write keyed
        on it loses the fourth state at the write — where no read can recover it.
        """
        assert RigorVerdictWrite.from_verdict(RigorGateVerdict.degenerate()).status == "degenerate"
        assert RigorVerdictWrite.from_verdict(RigorGateVerdict.failed()).status == "fail"
        assert RigorVerdictWrite.from_verdict(RigorGateVerdict.passed()).status == "pass"
        assert RigorVerdictWrite.from_verdict(RigorGateVerdict.pending()).status == "pending"

    def test_the_post_backtest_refresh_stores_the_real_gates_verdict(self):
        """The whole chain: real gate → RigorVerdictWrite → column → read.

        MUTATION: revert ``_refresh_passport_real_metrics`` to its old
        ``passes_rigor_gate: bool`` parameter and drop the ``rigor_verdict=``
        argument. ``rigor_gate_status`` then stays at its "pending" default while
        the boolean says otherwise — the decoupled pair this work removes.
        """
        from archimedes.agents.generation_pipeline import _refresh_passport_real_metrics
        from archimedes.api.strategies_routes import _passport_to_strategy_response

        candidate = _fake_candidate()
        result = _fake_backtest_result("graded-for-real")
        live = verdict_from_returns("graded-for-real", _VARIED)
        assert live.status == "fail", "fixture guard: this series must be gradeable and weak"

        with db.get_session() as session:
            ingest_passport(session, _passport("graded-for-real"), generation_method="fusion", force_update=True)
            session.commit()

            _refresh_passport_real_metrics(
                session, candidate, "graded-for-real", result, verdict=live, n_obs=len(_VARIED)
            )
            session.commit()

            row = get_passport(session, "graded-for-real")
            assert row.rigor_gate_status == "fail"
            assert row.passes_rigor_gate is False
            assert row.graded_at is not None
            assert row.gate_version == gate_version()
            assert row.cohort_n == 1, "the generation path grades a strategy against itself alone"

            # …and the read surface serves exactly that, with no recompute.
            served = _passport_to_strategy_response(row, session=session)
            assert served.rigor_gate_status == "fail"
            assert served.passes_rigor_gate is False

    def test_the_refresh_also_writes_the_metric_columns_it_used_to_drop(self):
        """MUTATION: delete the eight ``record.<field> = passport.real_*`` lines
        added to ``_update_record``.

        ``win_rate`` / ``calmar_ratio`` / ``correlation_to_spy`` / ``total_trades``
        / ``backtest_start`` / ``backtest_end`` / ``n_obs_daily`` /
        ``sharpe_ci_*`` were written by the INSERT branch only. Every
        post-backtest refresh takes the force_update branch, so on any row that
        already existed they stayed frozen at the first ingest's values — usually
        NULL — while ``_passport_to_strategy_response`` served them beside a fresh
        Sharpe from the same run. One row, two backtests.
        """
        from archimedes.agents.generation_pipeline import _refresh_passport_real_metrics

        with db.get_session() as session:
            ingest_passport(session, _passport("metrics-refresh"), generation_method="fusion", force_update=True)
            session.commit()
            assert get_passport(session, "metrics-refresh").win_rate is None

            _refresh_passport_real_metrics(
                session,
                _fake_candidate(),
                "metrics-refresh",
                _fake_backtest_result("metrics-refresh"),
                verdict=verdict_from_returns("metrics-refresh", _VARIED),
                n_obs=len(_VARIED),
            )
            session.commit()
            row = get_passport(session, "metrics-refresh")

            assert row.win_rate == pytest.approx(0.55)
            assert row.calmar_ratio == pytest.approx(0.4)
            assert row.correlation_to_spy == pytest.approx(0.12)
            assert row.total_trades == 87
            assert row.backtest_start == "2020-01-02"
            assert row.backtest_end == "2021-12-31"
            assert row.n_obs_daily == len(_VARIED)


def _fake_candidate():
    from archimedes.agents.generation_pipeline import _CandidateResult

    return _CandidateResult(
        candidate_id="cand-1",
        strategy_name="Test Strategy",
        thesis="a thesis",
        asset_universe=["SPY"],
        source_papers=[{"arxiv_id": "2301.00042"}],
        weights={"SPY": 1.0},
        reasoning="",
        # Deliberately a PASSING fusion verdict: if any of it leaked into the
        # passport's verdict the tests above would go green for the wrong reason.
        rigor_verdict={"passing": True, "dsr": 0.9},
        passes_rigor=True,
        generation_method="fusion",
    )


def _fake_backtest_result(strategy_id: str):
    from datetime import date

    from archimedes.models.backtest import BacktestResult

    return BacktestResult(
        strategy_id=strategy_id,
        sharpe_ratio=0.31,
        sortino_ratio=0.4,
        max_drawdown=0.22,
        cagr=0.09,
        calmar_ratio=0.4,
        win_rate=0.55,
        profit_factor=1.1,
        total_trades=87,
        avg_holding_period_days=5.0,
        correlation_to_spy=0.12,
        correlation_to_btc=None,
        backtest_start=date(2020, 1, 2),
        backtest_end=date(2021, 12, 31),
        deflated_sharpe_ratio=0.21,
        dsr_p_value=0.44,
        pbo_score=0.4,
        out_of_sample_sharpe=0.1,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 3. PARITY — three surfaces, one verdict
# ═══════════════════════════════════════════════════════════════════════════


def _seed_generated_row(strategy_id: str, *, status: str, fusion_says_passing: bool) -> None:
    """A generated strategy as prod actually holds one: a strategy_store row
    carrying the GENERATION-TIME fusion verdict, and a passport row carrying the
    graded verdict of record. The two disagree on purpose — that is #1747."""
    import json as _json

    from archimedes.models.strategy_store import StrategyRecord

    with db.get_session() as session:
        session.add(
            StrategyRecord(
                id=strategy_id,
                content_hash=("0x" + strategy_id).ljust(66, "0"),
                generation_method="fusion",
                source_papers=_json.dumps([{"arxiv_id": "2301.00042"}]),
                strategy_name="Parity Strategy",
                thesis="a thesis",
                asset_universe="[]",
                risk_profile="moderate",
                # Both halves of the OLD badge, from the one blob that fed them.
                status="live" if fusion_says_passing else "rejected",
                rigor_verdict=_json.dumps(
                    {"passing": fusion_says_passing, "dsr": 0.88, "pbo": 0.12, "oos_sharpe": 1.4, "dsr_p_value": 0.97}
                ),
                is_example=False,
                owner_wallet=_W_OWNER.lower(),
                is_published=False,
            )
        )
        session.commit()

    with db.get_session() as session:
        ingest_passport(
            session,
            _passport(strategy_id, real_sharpe=0.31, deflated_sharpe_ratio=0.21, dsr_p_value=0.44),
            generation_method="fusion",
            force_update=True,
            owner_wallet=_W_OWNER,
            rigor_verdict=RigorVerdictWrite(status=status, cohort_n=1),
        )
        session.commit()


@pytest.mark.asyncio
async def test_the_three_read_surfaces_serve_one_verdict():
    """``/generated`` row == ``/strategies/{id}`` == ``/passports/{id}``.

    MUTATION 1: drop the ``verdicts.get(...)`` overlay from
    ``list_generated_strategies`` (restore the bare ``r.to_dict()``). The
    Generated row then carries no verdict at all and the first assert reddens.

    MUTATION 2: overlay from ``StrategyRecord.rigor_verdict["passing"]`` instead
    of the passport row — the row claims a pass the passport denies, which is
    #1747 exactly.
    """
    from archimedes.main import app

    _seed_generated_row("parity-fail", status="fail", fusion_says_passing=True)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        cookies = _siwe_cookies(_W_OWNER)
        listed = await client.get("/api/strategies/generated", cookies=cookies)
        detail = await client.get("/api/strategies/parity-fail", cookies=cookies)
        passport = await client.get("/api/strategies/passports/parity-fail", cookies=cookies)

    assert listed.status_code == 200, listed.text
    assert detail.status_code == 200, detail.text
    assert passport.status_code == 200, passport.text

    row = next(r for r in listed.json()["strategies"] if r["id"] == "parity-fail")

    assert row["rigor_gate_status"] == "fail"
    assert row["passes_rigor_gate"] is False
    assert row["rigor_gate_status"] == detail.json()["rigor_gate_status"] == passport.json()["rigor_gate_status"]
    assert row["passes_rigor_gate"] == detail.json()["passes_rigor_gate"] == passport.json()["passes_rigor_gate"]

    # The generation-time verdict is still on the wire — as the debate record,
    # under its own name. What it must never be is the badge.
    assert row["rigor_verdict"]["passing"] is True
    assert row["status"] == "live"


@pytest.mark.asyncio
async def test_a_generated_row_with_no_passport_is_pending_never_green():
    """MUTATION: default the overlay to ``passes_rigor_gate: False`` instead of
    ``None``.

    False is a VERDICT ("the gate ran and it lost"). No gate ran. The distinction
    is the difference between a red X and a clock on the Library row.
    """
    import json as _json

    from archimedes.main import app
    from archimedes.models.strategy_store import StrategyRecord

    with db.get_session() as session:
        session.add(
            StrategyRecord(
                id="no-passport-row",
                content_hash=("0x" + "no-passport-row").ljust(66, "0"),
                generation_method="fusion",
                source_papers=_json.dumps([]),
                strategy_name="Orphan",
                thesis="t",
                asset_universe="[]",
                risk_profile="moderate",
                status="live",
                rigor_verdict=_json.dumps({"passing": True}),
                is_example=False,
                owner_wallet=_W_OWNER.lower(),
                is_published=False,
            )
        )
        session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        listed = await client.get("/api/strategies/generated", cookies=_siwe_cookies(_W_OWNER))

    row = next(r for r in listed.json()["strategies"] if r["id"] == "no-passport-row")
    assert row["passes_rigor_gate"] is None
    assert row["rigor_gate_status"] == "pending"
    assert row["deflated_sharpe_ratio"] is None, "no grade means no numbers from a grade"


@pytest.mark.asyncio
async def test_the_passport_payload_carries_its_verdicts_provenance():
    """MUTATION: drop the four provenance keys from ``to_dict()``.

    Without them a stored verdict is unreadable: an agent cannot tell a real
    grade from an ungraded row, nor a current gate's verdict from a
    legacy-derived one — which is the whole reason the columns exist.
    """
    from archimedes.main import app

    _seed_generated_row("provenance-row", status="pass", fusion_says_passing=False)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/strategies/passports/provenance-row", cookies=_siwe_cookies(_W_OWNER))

    assert resp.status_code == 200, resp.text
    passport = resp.json()
    assert passport["rigor_gate_status"] == "pass"
    assert passport["passes_rigor_gate"] is True
    assert passport["gate_version"] == gate_version()
    assert passport["cohort_n"] == 1
    assert datetime.fromisoformat(passport["graded_at"]).replace(tzinfo=UTC) <= datetime.now(UTC)


@pytest.mark.asyncio
async def test_the_detail_route_derives_the_boolean_even_when_the_row_has_them_apart():
    """ONE function, ONE source for "does this pass".

    ``_passport_to_strategy_response`` answers "does this strategy pass" twice:
    once for the badge it serves, and once for the ``StrategyView`` it hands
    ``classify_return_source``. Those two answers used to come from two
    different fields — the derived four-state for the badge, the raw
    ``record.passes_rigor_gate`` column for the classifier. The column and the
    status agree today only because the migration and ``_apply_rigor_verdict``
    couple them, which is exactly the "coupled by convention" shape this ADR
    replaces with a structural one.

    The row seeded here is a MIXED-VINTAGE row: ``rigor_gate_status = 'pending'``
    beside ``passes_rigor_gate = 1``. That is not hypothetical — it is the
    pre-ADR prod state (#1746), reachable again through any manual UPDATE or a
    restored old backup. ``_passport_verdicts_for`` already promises the LIST
    route cannot serve the two apart; this pins the same promise on the DETAIL
    route, on both of its consumers.

    MUTATION: restore ``passes_rigor_gate=bool(record.passes_rigor_gate)`` in the
    ``StrategyView`` passed to ``classify_return_source``. The classifier then
    sees ``True`` for a row the badge calls ``pending``, and the captured-view
    assert reddens.
    """
    import archimedes.services.return_source_classifier as rsc
    from archimedes.main import app
    from sqlalchemy import text

    with db.get_session() as session:
        ingest_passport(
            session,
            _passport("mixed-vintage", real_sharpe=0.31, deflated_sharpe_ratio=0.21, dsr_p_value=0.44),
            generation_method="fusion",
            force_update=True,
            owner_wallet=_W_OWNER,
            rigor_verdict=RigorVerdictWrite(status="pending", cohort_n=1),
        )
        session.commit()

    # Force the two apart, the way a legacy row or a hand-run UPDATE would.
    with db.get_session() as session:
        session.execute(
            text("UPDATE strategy_passports SET passes_rigor_gate = 1 WHERE id = :i"),
            {"i": "mixed-vintage"},
        )
        session.commit()

    with db.get_session() as session:
        stored = session.execute(
            text("SELECT rigor_gate_status, passes_rigor_gate FROM strategy_passports WHERE id = :i"),
            {"i": "mixed-vintage"},
        ).one()
    assert stored[0] == "pending" and bool(stored[1]) is True, "the row must really hold them apart"

    # `strategies_routes` imports the classifier INSIDE the function, so the
    # patch has to land on the defining module for the route to pick it up.
    seen: list[bool] = []
    original = rsc.classify_return_source

    def _capture(view):
        seen.append(view.passes_rigor_gate)
        return original(view)

    rsc.classify_return_source = _capture
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/strategies/mixed-vintage", cookies=_siwe_cookies(_W_OWNER))
    finally:
        rsc.classify_return_source = original

    assert resp.status_code == 200, resp.text
    body = resp.json()

    # The badge: derived from the stored four-state, never the stray boolean.
    assert body["rigor_gate_status"] == "pending"
    assert body["passes_rigor_gate"] is False

    # The classifier: the SAME answer, from the SAME derivation.
    assert seen == [False], f"classify_return_source was handed {seen}, not the derived verdict"
