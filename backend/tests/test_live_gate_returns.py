"""#788/#818 — fusion/debate candidates persist REAL returns so the live rigor
gate reads pass/fail, not "pending".

Real tmp-sqlite integration (per the deep review): the earlier mock-only tests
mocked away exactly the two broken behaviors. These drive `_persist_real_returns`
against a real sqlite DB and prove:
  1. the write COMMITS — the row survives → `get_daily_returns` reads it (catches the
     flush-only rollback that kept the gate at "pending");
  2. a SYNTHETIC-sourced backtest is NEVER persisted (grading random-walk noise as a
     real pass/fail is a #1-rule claims-integrity violation) — it stays honestly
     "pending".
"""

from __future__ import annotations

import numpy as np
import pytest
from archimedes.agents.generation_pipeline import _CandidateResult, _persist_real_returns
from archimedes.db import get_session
from archimedes.services.dsl_lookahead_audit import PASS as LOOK_AHEAD_PASS
from archimedes.services.dsl_lookahead_audit import (
    SOURCE_DSL_AUDIT,
    SOURCE_DSL_AUDIT_NOT_RUN,
    SOURCE_SELF_ATTESTED_RETIRED,
    DslLookAheadAudit,
    audit_dsl_strategy,
    verdict_from_persisted_row,
)


@pytest.fixture(autouse=True)
def _use_tmp_db(tmp_path, monkeypatch):
    """Point the DB at a FRESH temp sqlite so the persist path runs for real.

    db.engine/SessionLocal are created once at import, so setenv alone doesn't
    re-point them; rebind both to a per-test engine (else tests share one stale DB).
    """
    import archimedes.db as db
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    url = f"sqlite:///{tmp_path / 'live_gate.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    eng = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", eng)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=eng, autocommit=False, autoflush=False))
    db.init_db()  # uses the rebound engine + registers ALL tables (passport/kg side-effect imports)

    # Run the persist inline (not on a worker thread) so the write + read share one
    # connection context — deterministic under sqlite.
    async def _inline(fn, *a, **k):
        return fn(*a, **k)

    monkeypatch.setattr("archimedes.agents.generation_pipeline.asyncio.to_thread", _inline)
    yield


class _FakeEmit:
    def __init__(self):
        self.events = []

    async def emit(self, name, **kw):
        self.events.append((name, kw))


#: A real clean look-ahead audit, and a real never-ran one. The rigor blob's
#: look-ahead legs are read off these rather than hand-typed: a fixture that
#: hardcoded ``"lookahead_audit_passed": True`` with no accompanying status
#: described a verdict the pipeline cannot produce — a pass with no audit behind
#: it, which is precisely the state this whole change exists to make
#: unrepresentable.
_CLEAN_AUDIT = DslLookAheadAudit(status=LOOK_AHEAD_PASS, interpreter_verified=True, broker_cheat_check=True)
_NEVER_RAN_AUDIT = audit_dsl_strategy(None, broker_cheat_check=True)


def _rigor(data_source="live", audit: DslLookAheadAudit | None = None):
    la = audit or _CLEAN_AUDIT
    return {
        "dsr": 1.6,
        "dsr_p_value": 0.99,
        "pbo": 0.2,
        "oos_sharpe": 1.1,
        "sharpe_ratio": 1.3,
        "sortino_ratio": 1.5,
        "max_drawdown": 0.12,
        "cagr": 0.15,
        "calmar_ratio": 1.25,
        "win_rate": 0.55,
        "total_trades": 64,
        # The three legs a real RigorVerdict carries, always consistent with each
        # other because they come from one audit object.
        "lookahead_audit_passed": la.passed,
        "look_ahead_status": la.status,
        "look_ahead_reason": la.reason,
        "passing": True,
        "data_source": data_source,
        "admissible": data_source != "synthetic",
    }


def _candidate(
    *,
    return_series,
    has_real_rigor=True,
    data_source="live",
    audit: DslLookAheadAudit | None = None,
) -> _CandidateResult:
    return _CandidateResult(
        candidate_id="c1",
        strategy_name="Fusion X",
        thesis="Fuse momentum + vol.",
        asset_universe=["SPY"],
        source_papers=[{"arxiv_id": "2401.00001", "title": "P"}],
        weights={},
        reasoning="r",
        rigor_verdict=_rigor(data_source, audit),
        passes_rigor=True,
        generation_method="fusion",
        source_arxiv_ids=["2401.00001", "2402.00001"],
        has_real_rigor=has_real_rigor,
        return_series=return_series,
    )


# A realistic daily series with genuine variance (real DSL backtests produce hundreds
# of bars). A deterministic-but-degenerate/repeating series makes the DSR/OOS math
# raise → the gate fails closed to "pending"; a real return distribution grades cleanly.
_REAL_RETURNS = np.random.default_rng(7).normal(0.0006, 0.011, 250).tolist()


async def test_real_returns_persist_and_survive_commit():
    from archimedes.services.backtest_repository import get_daily_returns

    emit = _FakeEmit()
    await _persist_real_returns(_candidate(return_series=_REAL_RETURNS), "strat_live", emit, num_trials=24)

    with get_session() as session:
        persisted = get_daily_returns(session, "strat_live")
    # Row SURVIVED the session close → commit fired (flush-only would give []).
    assert len(persisted) == len(_REAL_RETURNS)
    assert any(name == "backtest_done" for name, _ in emit.events)
    assert not any(name == "backtest_failed" for name, _ in emit.events)


async def test_synthetic_returns_are_never_persisted():
    from archimedes.services.backtest_repository import get_daily_returns

    # Same returns, but data_source="synthetic" → must NOT be persisted (claims-integrity).
    await _persist_real_returns(
        _candidate(return_series=_REAL_RETURNS, data_source="synthetic"), "strat_synth", _FakeEmit(), num_trials=24
    )
    with get_session() as session:
        assert get_daily_returns(session, "strat_synth") == []  # honestly "pending"


async def test_live_verdict_readable_from_persisted_strategy():
    # After persistence, the strategy's live gate + passport read the REAL returns —
    # not "pending" — so the single-strategy read path + deploy gate see the verdict.
    from archimedes.services.live_rigor_gate import verdict_from_returns

    await _persist_real_returns(_candidate(return_series=_REAL_RETURNS), "strat_read", _FakeEmit(), num_trials=24)
    with get_session() as session:
        from archimedes.services.backtest_repository import get_daily_returns

        returns = get_daily_returns(session, "strat_read")
    verdict = verdict_from_returns("strat_read", returns, num_trials=24)
    assert verdict.status in ("pass", "fail")  # NOT "pending"


class TestAnAuditThatNeverRanRendersPendingNotFail:
    """End-to-end honest rendering, across the persistence boundary.

    A generated strategy whose look-ahead audit never reached a verdict must,
    when a surface later grades its STORED row:

      * be non-deployable (fail-closed — the always-on floor blocks it), and
      * be rendered as NOT_RUN, never as "FAIL".

    Both halves matter. Rendering it FAIL tells the user their strategy was
    caught leaking when nothing looked; letting it pass would deploy on an audit
    that never happened. The route under test is
    ``selection_bias_routes._generated_strategy_rigor``'s exact composition:
    ``verdict_from_persisted_row`` over the two stored columns, then
    ``run_rigor_gate``.
    """

    @staticmethod
    def _gate_for(row):
        from archimedes.services.rigor_evaluator import run_rigor_gate

        passed, status, reason = verdict_from_persisted_row(row.look_ahead_audit_passed, row.look_ahead_audit_source)
        return run_rigor_gate(
            strategy_id="graded",
            daily_returns=_REAL_RETURNS,
            num_trials=1,
            look_ahead_audit_passed=passed,
            look_ahead_status=status,
            look_ahead_not_run_reason=reason,
        )

    @staticmethod
    def _latest(strategy_id):
        from archimedes.services.backtest_repository import latest_backtests_by_strategy

        with get_session() as session:
            return latest_backtests_by_strategy(session, [strategy_id])[strategy_id]

    async def test_a_never_ran_audit_persists_as_not_run_and_renders_NOT_RUN(self):
        await _persist_real_returns(
            _candidate(return_series=_REAL_RETURNS, audit=_NEVER_RAN_AUDIT),
            "strat_unaudited",
            _FakeEmit(),
            num_trials=24,
        )
        row = self._latest("strat_unaudited")
        assert row.look_ahead_audit_passed is False
        assert row.look_ahead_audit_source == SOURCE_DSL_AUDIT_NOT_RUN
        assert row.look_ahead_audit_source != SOURCE_SELF_ATTESTED_RETIRED, "nothing is attested any more"

        gate = self._gate_for(row)
        detail = gate.gate_details["look_ahead"]
        assert detail.startswith("NOT_RUN ("), detail
        assert detail != "FAIL"
        assert "blocks admission" in detail
        # ...and it really does block: honest rendering is not a loosened gate.
        assert gate.look_ahead_passed is False
        assert gate.blocked_by_floor is True
        assert gate.passes_all is False

    async def test_a_completed_audit_persists_as_the_audit_and_renders_PASS(self):
        """The control arm — without it the assertions above would hold against
        code that had stopped distinguishing the two states entirely."""
        await _persist_real_returns(
            _candidate(return_series=_REAL_RETURNS, audit=_CLEAN_AUDIT),
            "strat_audited",
            _FakeEmit(),
            num_trials=24,
        )
        row = self._latest("strat_audited")
        assert row.look_ahead_audit_passed is True
        assert row.look_ahead_audit_source == SOURCE_DSL_AUDIT

        gate = self._gate_for(row)
        assert gate.gate_details["look_ahead"] == "PASS"
        assert gate.look_ahead_passed is True

    async def test_a_legacy_self_attested_row_is_pending_not_a_pass(self):
        """A stored ``True`` whose provenance is the LLM's own declaration.

        These rows exist in the DB from before the field was removed. Honouring
        the boolean would deploy a strategy on a sentence the generator wrote
        about itself — the exact defect. It reads ``pending``: blocked, and not
        accused.
        """
        from archimedes.services.rigor_evaluator import run_rigor_gate

        passed, status, reason = verdict_from_persisted_row(True, SOURCE_SELF_ATTESTED_RETIRED)
        assert passed is False, "a self-attested True is a claim, not a measurement"
        assert status == "pending"

        gate = run_rigor_gate(
            strategy_id="legacy",
            daily_returns=_REAL_RETURNS,
            num_trials=1,
            look_ahead_audit_passed=passed,
            look_ahead_status=status,
            look_ahead_not_run_reason=reason,
        )
        assert gate.gate_details["look_ahead"].startswith("NOT_RUN (")
        assert gate.blocked_by_floor is True

    def test_an_unrelated_provenance_still_honours_its_boolean(self):
        """The mirror: this must not turn into "nothing ever passes".

        ``ast_audit`` / ``broker_config_only`` rows are graded by other paths and
        are untouched by any of the above.
        """
        assert verdict_from_persisted_row(True, "ast_audit") == (True, None, None)
        assert verdict_from_persisted_row(True, "broker_config_only") == (True, None, None)
        assert verdict_from_persisted_row(False, "ast_audit") == (False, None, None)


async def test_persist_skips_when_no_real_backtest():
    from archimedes.services.backtest_repository import get_daily_returns

    await _persist_real_returns(_candidate(return_series=None), "s1", _FakeEmit(), 5)
    await _persist_real_returns(_candidate(return_series=_REAL_RETURNS, has_real_rigor=False), "s2", _FakeEmit(), 5)
    await _persist_real_returns(_candidate(return_series=[0.01, 0.02]), "s3", _FakeEmit(), 5)
    with get_session() as session:
        assert get_daily_returns(session, "s1") == []
        assert get_daily_returns(session, "s2") == []
        assert get_daily_returns(session, "s3") == []
