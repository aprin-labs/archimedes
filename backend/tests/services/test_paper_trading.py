"""The paper-trading laws (MVP: verdict → paper-trade).

Hermetic: an in-memory SQLite session and a stubbed replay. What is pinned is
the part that must never regress silently:

  1. APPEND-ONLY: advancing twice appends nothing new the second time; a
     replay can only ever contribute unseen dates.
  2. DRIFT IS SURFACED, NEVER REPAIRED: when a fresh replay disagrees with
     rows already written (upstream restatement), the ledger keeps the
     original numbers, the deployment is stamped, and the event is logged —
     mutation-verified: a variant that "repairs" the ledger fails these.
  3. THE SNAPSHOT IS THE CONTRACT: mutating the strategy's spec after deploy
     must not change what the ledger grades.
  4. THE DEPLOY DATE SLICES THE REPLAY: history before deployed_at never
     enters the ledger.
  5. ONE BAD DEPLOYMENT CANNOT STALL THE REST of advance_all.
  6. THE MARKS LOOP READS, NEVER WRITES: the daily advance stamps a position
     cache and that is the only channel to the intraday loop, so marks cannot
     change what the strategy does (intraday design §4.0/§4.1).
  7. A RE-GRADE IS NOT A RESTATEMENT (#1449): every appended row records the
     grading engine that produced it, and a disagreement is reported as the
     data's fault ONLY when the engine did not move. A grading-side change
     (#1379's slippage floor) lands on engine_regrade_at, and a row with no
     recorded version is called unattributable rather than blamed on either
     side. The quiet answers are guarded — an unreadable current version
     fails closed to loud.
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from archimedes.models.chat import Base
from archimedes.models.paper_store import PaperDailyReturn
from archimedes.services.paper_trading import (
    advance_all,
    advance_deployment,
    create_deployment,
    deployment_summary,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

_SPEC = {
    "name": "paper probe",
    "asset_universe": ["SPY"],
    "rebalance_frequency": "daily",
    "entry": {"gt": ["momentum_20", 0]},
    "exit": {"lt": ["momentum_20", -0.99]},
    "position_sizing": {"type": "full_invested_when_in_market"},
    "source_arxiv_ids": ["0706.1497"],
    "look_ahead_safe": True,
}

_DEPLOY = date(2026, 8, 1)


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _replay_v1(spec_dict, deployed_at):
    # A stub replay honouring the real contract: dates >= deployed_at only.
    series = {
        date(2026, 7, 30): 0.9,  # pre-deploy history — must never be ledgered
        date(2026, 8, 1): 0.01,
        date(2026, 8, 4): -0.02,
        date(2026, 8, 5): 0.005,
    }
    return {d: r for d, r in series.items() if d >= deployed_at}


def test_advance_appends_only_from_the_deploy_date():
    with _session() as s:
        dep = create_deployment(s, strategy_id="s1", spec_dict=_SPEC, owner_wallet="0xAB", deployed_at=_DEPLOY)
        out = advance_deployment(s, dep, replay=_replay_v1)
        assert (out["appended"], out["drift"]) == (3, 0)
        dates = [r.date for r in s.query(PaperDailyReturn).order_by(PaperDailyReturn.date)]
        assert dates == [date(2026, 8, 1), date(2026, 8, 4), date(2026, 8, 5)]
        assert date(2026, 7, 30) not in dates  # law 4


def test_second_advance_is_idempotent_and_new_days_append():
    with _session() as s:
        dep = create_deployment(s, strategy_id="s1", spec_dict=_SPEC, owner_wallet="0xAB", deployed_at=_DEPLOY)
        advance_deployment(s, dep, replay=_replay_v1)
        second = advance_deployment(s, dep, replay=_replay_v1)
        assert (second["appended"], second["drift"]) == (0, 0)  # law 1

        def replay_v2(spec_dict, deployed_at):
            out = _replay_v1(spec_dict, deployed_at)
            out[date(2026, 8, 6)] = 0.003  # the next trading day arrives
            return out

        third = advance_deployment(s, dep, replay=replay_v2)
        assert (third["appended"], third["drift"]) == (1, 0)
        assert s.query(PaperDailyReturn).count() == 4


def test_restatement_is_surfaced_never_repaired(caplog):
    """Law 2 — the load-bearing one. Mutation-verified: an implementation that
    overwrites the drifted row makes BOTH assertions here fail (the stored
    value changes, and drift reports 0 on the second pass)."""
    with _session() as s:
        dep = create_deployment(s, strategy_id="s1", spec_dict=_SPEC, owner_wallet="0xAB", deployed_at=_DEPLOY)
        advance_deployment(s, dep, replay=_replay_v1)

        def restated(spec_dict, deployed_at):
            out = _replay_v1(spec_dict, deployed_at)
            out[date(2026, 8, 4)] = -0.5  # upstream rewrote history
            return out

        with caplog.at_level("WARNING"):
            out = advance_deployment(s, dep, replay=restated)
        assert out["drift"] == 1
        row = s.query(PaperDailyReturn).filter_by(date=date(2026, 8, 4)).one()
        assert row.daily_return == pytest.approx(-0.02)  # original stands
        assert dep.drift_detected_at is not None
        assert any("NOT rewritten" in r.message for r in caplog.records)
        # Law 7's other half: the engine did NOT move between the two replays
        # (both rows carry the same version), so this stays attributed to the
        # data and must not be excused as a re-grade.
        assert out["drift_engine"] == 0
        assert out["drift_unversioned"] == 0
        assert dep.engine_regrade_at is None


def test_spec_snapshot_survives_strategy_mutation():
    with _session() as s:
        dep = create_deployment(s, strategy_id="s1", spec_dict=_SPEC, owner_wallet="0xAB", deployed_at=_DEPLOY)
        seen_specs = []

        def spy_replay(spec_dict, deployed_at):
            seen_specs.append(spec_dict)
            return _replay_v1(spec_dict, deployed_at)

        advance_deployment(s, dep, replay=spy_replay)
        # The strategy gets regenerated with a different spec afterwards —
        # the deployment must keep replaying its snapshot.
        advance_deployment(s, dep, replay=spy_replay)
        assert all(spec["entry"] == _SPEC["entry"] for spec in seen_specs)
        assert json.loads(dep.spec_json)["name"] == "paper probe"


def test_advance_all_isolates_failures():
    from archimedes.services import paper_trading

    with _session() as s:
        good = create_deployment(s, strategy_id="ok", spec_dict=_SPEC, owner_wallet="0xAB", deployed_at=_DEPLOY)
        bad = create_deployment(s, strategy_id="bad", spec_dict=_SPEC, owner_wallet="0xAB", deployed_at=_DEPLOY)
        bad.spec_json = json.dumps({**_SPEC, "entry": {"gt": ["nonsense_metric", 0]}})

        calls = {}

        def selective(spec_dict, deployed_at):
            """The settle path's own seam (#1575): ONE replay produces both the
            dated returns and the decision journal, so a trace and the ledger
            row it explains can never come from two different runs."""
            if spec_dict["entry"] == _SPEC["entry"]:
                calls["good"] = True
                return _replay_v1(spec_dict, deployed_at), {}
            raise paper_trading.PaperReplayError("boom")

        original = paper_trading.replay_spec_with_decisions
        paper_trading.replay_spec_with_decisions = selective
        try:
            out = advance_all(s)
        finally:
            paper_trading.replay_spec_with_decisions = original
        assert out["deployments"] == 2
        assert out["ok"] == 1 and out["failed"] == 1
        assert calls.get("good") is True
        assert s.query(PaperDailyReturn).filter_by(deployment_id=good.id).count() == 3


def test_summary_compounds_the_ledger():
    with _session() as s:
        dep = create_deployment(s, strategy_id="s1", spec_dict=_SPEC, owner_wallet="0xAB", deployed_at=_DEPLOY)
        advance_deployment(s, dep, replay=_replay_v1)
        summary = deployment_summary(s, dep)
        assert summary["days"] == 3
        expected = (1.01 * 0.98 * 1.005) - 1.0
        assert summary["total_return"] == pytest.approx(expected)
        assert summary["series"][-1]["equity_index"] == pytest.approx(1.0 + expected)


# ── The position-set cache (intraday design §4.1) ──────────────────────
#
# 6. THE MARKS LOOP READS, NEVER WRITES. The cache is the only thing the
#    daily advance hands the intraday loop, and it is written here — so the
#    marks path has no route back into what the strategy does. That one-way
#    arrow is the whole safety argument for intraday marks (§4.0), and these
#    tests are what make it a checked fact instead of a diagram.


def _replay_with_positions(spec_dict, deployed_at):
    """A stub replay that behaves like the real ``replay_spec``: a dict of
    dated returns, with the position set attached."""
    from archimedes.services.paper_trading import PositionSet, ReplayResult

    out = ReplayResult(_replay_v1(spec_dict, deployed_at))
    out.positions = PositionSet(
        as_of=date(2026, 8, 5),
        weights={"SPY": 0.6, "BTC-USD": 0.4},
        ref_prices={"SPY": 500.0, "BTC-USD": 60000.0},
    )
    return out


def test_the_advance_stamps_the_position_cache_for_the_marks_loop():
    with _session() as s:
        dep = create_deployment(s, strategy_id="s1", spec_dict=_SPEC, owner_wallet="0xAB", deployed_at=_DEPLOY)
        advance_deployment(s, dep, replay=_replay_with_positions)

        cache = json.loads(dep.position_cache_json)
        assert cache["as_of"] == "2026-08-05"
        assert cache["weights"] == {"SPY": 0.6, "BTC-USD": 0.4}
        assert cache["ref_prices"] == {"SPY": 500.0, "BTC-USD": 60000.0}
        assert dep.position_cache_at is not None


def test_the_cached_equity_index_matches_what_the_summary_renders():
    """The intraday value is anchored to the SAME index the settled total
    return shows. If these two ever disagreed, the live line would visibly
    jump at every daily advance — a number moving for a reason that is not a
    price change."""
    with _session() as s:
        dep = create_deployment(s, strategy_id="s1", spec_dict=_SPEC, owner_wallet="0xAB", deployed_at=_DEPLOY)
        advance_deployment(s, dep, replay=_replay_with_positions)

        cache = json.loads(dep.position_cache_json)
        summary = deployment_summary(s, dep)
        assert cache["equity_index"] == pytest.approx(summary["series"][-1]["equity_index"])


def test_a_plain_dict_replay_stamps_no_cache_and_still_appends():
    """The seam's backwards compatibility, stated as a test: every existing
    caller and stub hands back a plain dict, which carries no position set.
    That must cost the cache and nothing else — the ledger append is
    untouched."""
    with _session() as s:
        dep = create_deployment(s, strategy_id="s1", spec_dict=_SPEC, owner_wallet="0xAB", deployed_at=_DEPLOY)
        out = advance_deployment(s, dep, replay=_replay_v1)
        # Subset, not exact-dict: #1575's trace pipeline extends the settle
        # result with its own accounting keys, which ride alongside and are
        # pinned by test_paper_trace_pipeline, not here. This test's guard is
        # the append count and the absent cache stamp.
        assert out["appended"] == 3
        assert out["drift"] == 0
        assert dep.position_cache_json is None


def test_a_later_replay_without_positions_does_not_clear_an_existing_cache():
    """A cache stale by one day still prices the position the ledger last
    settled; a CLEARED one makes a working deployment's live value vanish.
    Keeping the older cache is the smaller, more honest failure — and the mark
    it produces still carries its own as-of time, so nothing is claimed to be
    fresher than it is."""
    with _session() as s:
        dep = create_deployment(s, strategy_id="s1", spec_dict=_SPEC, owner_wallet="0xAB", deployed_at=_DEPLOY)
        advance_deployment(s, dep, replay=_replay_with_positions)
        stamped = dep.position_cache_json
        assert stamped is not None

        advance_deployment(s, dep, replay=_replay_v1)  # no positions this time
        assert dep.position_cache_json == stamped


def test_the_summary_carries_the_latest_mark_and_none_means_none():
    """`latest_mark` is a SEPARATE key from `total_return`, never folded into
    it, and `None` is a real state the UI must render as an em-dash with a
    reason rather than as +0.00%."""
    from datetime import UTC, datetime

    from archimedes.models.paper_store import PaperMark

    with _session() as s:
        dep = create_deployment(s, strategy_id="s1", spec_dict=_SPEC, owner_wallet="0xAB", deployed_at=_DEPLOY)
        advance_deployment(s, dep, replay=_replay_v1)
        assert deployment_summary(s, dep)["latest_mark"] is None

        s.add(
            PaperMark(
                deployment_id=dep.id,
                ts=datetime(2026, 8, 5, 14, 45, tzinfo=UTC),
                prices_json='{"SPY": 512.34}',
                portfolio_value=1.0347,
                source="yfinance",
                is_delayed=True,
                granularity="raw",
            )
        )
        s.flush()
        summary = deployment_summary(s, dep)
        assert summary["latest_mark"]["portfolio_value"] == pytest.approx(1.0347)
        assert summary["latest_mark"]["is_delayed"] is True
        assert summary["latest_mark"]["prices"] == {"SPY": 512.34}
        # The settled figure is unmoved by the presence of a mark.
        assert summary["total_return"] == pytest.approx((1.01 * 0.98 * 1.005) - 1.0)


# ── Law 7: a re-grade is not a restatement (#1449) ─────────────────────


def _restated(spec_dict, deployed_at):
    """`_replay_v1` with one historical date coming back different.

    The ONE input every test below shares, so the only thing that varies
    between them is the engine-version provenance of the row it disagrees with
    — which is exactly the discrimination under test.
    """
    out = _replay_v1(spec_dict, deployed_at)
    out[date(2026, 8, 4)] = -0.5
    return out


def test_every_appended_row_records_the_engine_that_graded_it():
    """The stamp is what makes the classification possible at all. It comes from
    the GRADED path (fusion_evaluator.GRADING_ENGINE_VERSION), not a literal
    re-typed here — a second copy could drift from the behavior it describes."""
    from archimedes.services.fusion_evaluator import GRADING_ENGINE_VERSION

    with _session() as s:
        dep = create_deployment(s, strategy_id="s1", spec_dict=_SPEC, owner_wallet="0xAB", deployed_at=_DEPLOY)
        advance_deployment(s, dep, replay=_replay_v1)
        rows = s.query(PaperDailyReturn).filter_by(deployment_id=dep.id).all()
        assert rows
        assert {row.engine_version for row in rows} == {GRADING_ENGINE_VERSION}
        summary = deployment_summary(s, dep)
        assert summary["ledger_engine_versions"] == [GRADING_ENGINE_VERSION]
        assert summary["unversioned_rows"] == 0
        assert summary["engine_regrade_at"] is None


def test_an_engine_version_change_is_a_regrade_not_the_users_history_restating(monkeypatch, caplog):
    """#1449's whole point. Rows graded under engine v1; the engine moves (this
    is #1379 wiring the slippage floor); the replay now disagrees. That must NOT
    stamp drift_detected_at — the user's track record did not restate itself,
    we changed how it is graded."""
    from archimedes.services import paper_trading

    with _session() as s:
        dep = create_deployment(s, strategy_id="s1", spec_dict=_SPEC, owner_wallet="0xAB", deployed_at=_DEPLOY)
        monkeypatch.setattr(paper_trading, "grading_engine_version", lambda: "dsl-fusion.r1/cm1:d10:s0")
        advance_deployment(s, dep, replay=_replay_v1)
        assert dep.drift_detected_at is None

        # The cost model moves. Same data, same spec, different graded numbers.
        monkeypatch.setattr(paper_trading, "grading_engine_version", lambda: "dsl-fusion.r1/cm1:d10:s5")
        with caplog.at_level("WARNING"):
            out = advance_deployment(s, dep, replay=_restated)

        assert out["drift_engine"] == 1
        assert out["drift"] == 0  # NOT reported as a data restatement
        assert out["drift_unversioned"] == 0
        assert dep.drift_detected_at is None
        assert dep.engine_regrade_at is not None
        # Surfaced, never suppressed: the ledger still stands, and the log says
        # which of the two sentences this is.
        assert s.query(PaperDailyReturn).filter_by(date=date(2026, 8, 4)).one().daily_return == pytest.approx(-0.02)
        assert any("RE-GRADE, not a restatement" in r.message for r in caplog.records)
        assert not any("drift_detected_at so the discrepancy" in r.message for r in caplog.records)


def test_a_row_written_before_versioning_is_called_unattributable_not_restated(caplog):
    """The migration backfills nothing, so pre-#1449 rows carry no version. A
    disagreement with one of those cannot be blamed on the data OR on us — it
    gets its own bucket and its own honest log line, and the count is on the
    payload rather than implied."""
    with _session() as s:
        dep = create_deployment(s, strategy_id="s1", spec_dict=_SPEC, owner_wallet="0xAB", deployed_at=_DEPLOY)
        advance_deployment(s, dep, replay=_replay_v1)
        # Exactly what the migration leaves behind for history written before
        # the column existed.
        for row in s.query(PaperDailyReturn).filter_by(deployment_id=dep.id):
            row.engine_version = None
        s.flush()

        with caplog.at_level("WARNING"):
            out = advance_deployment(s, dep, replay=_restated)

        assert out["drift_unversioned"] == 1
        assert out["drift"] == 0
        assert out["drift_engine"] == 0
        assert dep.drift_detected_at is None
        assert dep.engine_regrade_at is not None
        assert any("cannot be attributed either way" in r.message for r in caplog.records)
        summary = deployment_summary(s, dep)
        assert summary["unversioned_rows"] == 3
        assert summary["engine_regrade_at"] is not None


def test_an_unreadable_engine_version_fails_closed_to_loud_data_drift(monkeypatch, caplog):
    """THE GUARD. Every quiet answer is reached by finding a DIFFERENCE between
    two version strings, so a blank current version would make every row look
    re-graded and switch off the loud path fleet-wide — a config-shaped way to
    silence the one alarm this ledger owes its users.

    The adversarial input is a version string that is present but worthless
    (whitespace): presence alone must not buy absolution."""
    from archimedes.services import paper_trading

    with _session() as s:
        dep = create_deployment(s, strategy_id="s1", spec_dict=_SPEC, owner_wallet="0xAB", deployed_at=_DEPLOY)
        monkeypatch.setattr(paper_trading, "grading_engine_version", lambda: "dsl-fusion.r1/cm1:d10:s0")
        advance_deployment(s, dep, replay=_replay_v1)

        monkeypatch.setattr(paper_trading, "grading_engine_version", lambda: "   ")
        with caplog.at_level("WARNING"):
            out = advance_deployment(s, dep, replay=_restated)

        # A genuine engine change, but with an unreadable version we refuse to
        # claim it: loud, not quiet.
        assert out["drift"] == 1
        assert out["drift_engine"] == 0
        assert out["drift_unversioned"] == 0
        assert dep.drift_detected_at is not None
        assert dep.engine_regrade_at is None
        assert any("NOT rewritten" in r.message for r in caplog.records)


@pytest.mark.parametrize(
    ("row_version", "current_version", "expected"),
    [
        ("e1", "e1", "data"),  # engine held still → the data moved
        ("e1", "e2", "engine"),  # we moved
        (None, "e1", "unversioned"),  # written before the column existed
        ("", "e1", "unversioned"),  # a stored blank is still "unrecorded"
        ("e1", None, "data"),  # guard: unreadable current → fail closed
        ("e1", "", "data"),
        ("e1", "  \t ", "data"),
        (None, None, "data"),  # both unreadable → still the loud answer
    ],
)
def test_classify_drift_truth_table(row_version, current_version, expected):
    from archimedes.services.paper_trading import classify_drift

    assert classify_drift(row_version, current_version) == expected


def test_advance_all_reports_the_fleet_regrade_as_ours_not_as_n_data_problems(monkeypatch):
    """A grading-side change is a FLEET event: it re-grades every open
    deployment on the same pass. The cycle line has to let an operator read that
    as one incident of OURS, not as N independent data problems of the users'.

    Patches the settle-path seam ``replay_spec_with_decisions`` — the one
    ``advance_all`` actually reaches — rather than ``replay_spec``, which is
    only used when a caller injects a replay."""
    from archimedes.services import paper_trading

    monkeypatch.setattr(paper_trading, "grading_engine_version", lambda: "dsl-fusion.r1/cm1:d10:s0")
    with _session() as s:
        for name in ("a", "b"):
            dep = create_deployment(s, strategy_id=name, spec_dict=_SPEC, owner_wallet="0xAB", deployed_at=_DEPLOY)
            advance_deployment(s, dep, replay=_replay_v1)
        s.flush()

        monkeypatch.setattr(paper_trading, "grading_engine_version", lambda: "dsl-fusion.r1/cm1:d10:s5")
        monkeypatch.setattr(
            paper_trading,
            "replay_spec_with_decisions",
            lambda spec_dict, deployed_at: (_restated(spec_dict, deployed_at), {}),
        )
        summary = advance_all(s)

        assert summary["ok"] == 2 and summary["failed"] == 0
        assert summary["drift_engine"] == 2
        assert summary["drift"] == 0
        assert summary["drift_unversioned"] == 0
