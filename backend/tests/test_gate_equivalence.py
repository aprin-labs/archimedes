"""One gate, not two.

``BacktestResult`` used to carry its own ``passes_rigor_gate`` property with its
own thresholds (sharpe>0.5, dsr_p>0.95, pbo<0.5, oos/is>=0.5,
sharpe_vs_paper>=0.5, max_dd<0.5), and ``generation_pipeline`` graded generated
portfolio strategies with it. The curated read path grades through
``live_rigor_gate.verdict_from_returns`` and the ``rigor_profiles`` ladder. A
comment in the pipeline asserted the two matched; they did not.

The mismatch also had a silent failure mode. ``backtest_portfolio`` sets
``pbo_score=None`` because PBO is a library-level metric a later scheduler
refreshes, and the deleted property short-circuited to False whenever PBO was
None. Every generated portfolio strategy failed it unconditionally, so the
"grade" was a constant.

These tests pin the property gone and the one remaining gate reachable from both
directions. The full seven-implementation golden-vector harness is separate,
larger work; this is the surgical piece that makes the same-scale claim true.
"""

from __future__ import annotations

import numpy as np
import pytest
from archimedes.models.backtest import BacktestResult
from archimedes.services.live_rigor_gate import verdict_from_returns
from archimedes.services.rigor_gate_version import gate_version


def _series(seed: int, n: int = 600, mu: float = 0.0006, sigma: float = 0.01) -> list[float]:
    """Deterministic daily return series — no network, no fixtures."""
    rng = np.random.default_rng(seed)
    return [float(x) for x in rng.normal(mu, sigma, n)]


def _result(**overrides: object) -> BacktestResult:
    base = {
        "strategy_id": "test-strategy",
        "sharpe_ratio": 1.2,
        "sortino_ratio": 1.6,
        "max_drawdown": 0.12,
        "cagr": 0.14,
        "calmar_ratio": 1.1,
        "win_rate": 0.55,
        "profit_factor": 1.4,
        "total_trades": 40,
        "avg_holding_period_days": 12.0,
        "correlation_to_spy": 0.3,
        "correlation_to_btc": 0.1,
    }
    base.update(overrides)
    return BacktestResult(**base)  # type: ignore[arg-type]


class TestSecondGateIsGone:
    def test_backtest_result_exposes_no_gate(self) -> None:
        """Regression guard. A gate on a transport dataclass is invisible to the
        strictness ladder and drifts away from the real one, which is exactly
        what happened. If someone reintroduces either property, this fails.
        """
        result = _result()
        assert not hasattr(result, "passes_rigor_gate")
        assert not hasattr(result, "passes_validation")

    def test_no_gate_definitions_remain_in_the_module(self) -> None:
        """Belt and braces: catch a re-add under a different access pattern."""
        from pathlib import Path

        source = Path(BacktestResult.__module__.replace(".", "/"))
        module_file = Path(__file__).resolve().parents[1] / "archimedes" / "models" / "backtest.py"
        assert module_file.is_file(), f"expected the model at {module_file} (derived {source})"
        text = module_file.read_text(encoding="utf-8")
        assert "def passes_rigor_gate" not in text
        assert "def passes_validation" not in text


class TestOneGateGradesBothPaths:
    def test_same_series_gets_the_same_verdict_regardless_of_caller(self) -> None:
        """The generated path and the curated path grade one series identically.

        Both now go through verdict_from_returns, so this is true by
        construction — which is the point. The test exists so it stays true.
        """
        returns = _series(seed=7)

        generated = verdict_from_returns("gen-1", returns, num_trials=5, look_ahead_audit_passed=True)
        curated = verdict_from_returns("cur-1", returns, num_trials=5, look_ahead_audit_passed=True)

        assert generated.passes == curated.passes
        assert generated.status == curated.status

    def test_missing_pbo_no_longer_forces_a_fail(self) -> None:
        """The specific defect: pbo_score=None was an automatic False.

        backtest_portfolio always leaves PBO unset, so under the old property
        every generated portfolio strategy was ungradeable-but-reported-as-failed.
        The live gate treats a strong series on its own terms instead.
        """
        strong = _series(seed=11, mu=0.0012, sigma=0.008)
        verdict = verdict_from_returns("gen-nopbo", strong, num_trials=1, look_ahead_audit_passed=True)

        # Not asserting it passes — that is the gate's call, and the gate is
        # allowed to fail it. Asserting it was actually graded rather than
        # short-circuited on a missing library-level metric.
        assert verdict.status in {"pass", "fail"}
        assert verdict.status != "pending"

    def test_short_series_is_pending_not_a_pass(self) -> None:
        """Fail-closed direction is preserved: too little data is never a pass."""
        verdict = verdict_from_returns("gen-short", _series(seed=3, n=5), num_trials=1)
        assert verdict.status == "pending"
        assert verdict.passes is False

    def test_degenerate_series_is_never_a_pass(self) -> None:
        """A zero-variance series is the zero-trade artifact from the leaderboard.

        Eight rows currently show Sharpe of exactly 0.0 from runs that never
        placed a trade. Whatever the gate labels it, it must not be a pass.
        """
        verdict = verdict_from_returns("gen-degenerate", [0.0] * 600, num_trials=1)
        assert verdict.passes is False

    def test_degenerate_series_reports_its_own_category_not_pending(self) -> None:
        """#1184: a mathematically constant (zero-variance) persisted return
        series is broken data or a zero-trade backtest — NOT "not yet
        evaluated" (``pending``) and not an undifferentiated ``fail`` either
        (that reads as "graded and statistically weak", which is not what
        happened here: the series was never a legitimate gate input). Matches
        five real strategies with a constant 5,659-observation series that were
        surfacing as indistinguishable from genuinely-queued strategies.
        """
        verdict = verdict_from_returns("gen-degenerate-long", [0.0] * 5659, num_trials=1)
        assert verdict.status == "degenerate"
        assert verdict.status != "pending"
        assert verdict.status != "fail"
        assert verdict.passes is False

        # A short series (too few observations to grade at all) must still be
        # the ORIGINAL "pending" — the new category must not swallow it.
        short = verdict_from_returns("gen-short-not-degenerate", [0.0] * 5, num_trials=1)
        assert short.status == "pending"

        # A real, non-constant series must still be gradeable as PASS/FAIL,
        # never mislabeled "degenerate" just because it eventually fails.
        real = verdict_from_returns("gen-real-not-degenerate", _series(seed=11, mu=0.0012, sigma=0.008), num_trials=1)
        assert real.status in {"pass", "fail"}


class TestPipelineUsesTheLiveGate:
    def test_pipeline_reads_returns_out_of_the_artifact(self) -> None:
        """The helper feeding the gate must find the series the simulator wrote."""
        from archimedes.agents.generation_pipeline import _portfolio_daily_returns

        artifact = {"results": [{"metrics": {"daily_returns": [0.01, -0.02, 0.03]}}]}
        assert _portfolio_daily_returns(artifact) == pytest.approx([0.01, -0.02, 0.03])

    @pytest.mark.parametrize(
        "artifact",
        [
            {},
            {"results": []},
            {"results": [{}]},
            {"results": [{"metrics": {}}]},
        ],
    )
    def test_malformed_artifact_yields_no_returns_not_a_pass(self, artifact: dict) -> None:
        """An unexpected shape must degrade to pending, never to a pass."""
        from archimedes.agents.generation_pipeline import _portfolio_daily_returns

        returns = _portfolio_daily_returns(artifact)
        assert returns == []
        assert verdict_from_returns("gen-broken", returns, num_trials=1).passes is False


class TestPipelineCallSiteReadsTheLiveVerdict:
    """Drives the ACTUAL changed call site, not the gate function on its own.

    #1242 review: every test above this class calls ``verdict_from_returns``
    directly — including the two comparing it against itself with a different
    id string, which is true by the docstring's own admission ("this is true
    by construction"). None of them touch
    ``generation_pipeline.py``'s ``passes = live.passes`` line, which is the
    line the fix actually changed (from ``bool(result.passes_rigor_gate)``).
    A regression here — someone reverting that line back to the deleted
    property, or a typo that reads the wrong verdict field — would pass every
    other test in this file and only be caught here.

    This runs ``_backtest_and_persist`` (the real async pipeline function)
    against a fake ``backtest_portfolio`` that reproduces the exact pre-fix
    trigger: ``pbo_score=None`` (the library-level metric ``backtest_portfolio``
    always defers). The live gate's own ``verdict_from_returns`` is stubbed to
    return a controlled, known verdict — the real function is exhaustively
    covered elsewhere (``TestOneGateGradesBothPaths`` above,
    ``test_live_gate_returns.py``, ``test_selection_bias_generated_gate.py``);
    this call site never supplies ``pbo_scores`` to it, so criterion 4 fails
    closed regardless of series quality on this path today — a real,
    pre-existing limitation, not something this fix changes, and not something
    an organic returns series could use to distinguish "graded" from
    "hardcoded" here. Stubbing the gate isolates the one thing this test is
    actually for: does the call site propagate the live gate's OWN verdict, or
    something else.

    Parametrized over THREE of the four states, not two booleans
    (docs/adr/rigor-verdict-of-record.md): the call site now persists the live
    gate's four-state ``status``, so a stuck value in either boolean direction is
    still caught AND a call site that collapsed "degenerate" into "fail" — which
    a boolean cannot distinguish, since both are ``passes=False`` — is caught too.
    """

    @staticmethod
    def _strong_returns(seed: int = 11, n: int = 600) -> list[float]:
        rng = np.random.default_rng(seed)
        return [float(x) for x in rng.normal(0.0012, 0.008, n)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("live_gate_status", ["pass", "fail", "degenerate"])
    async def test_live_gate_verdict_reaches_the_persisted_passport(
        self, tmp_path, monkeypatch, live_gate_status: str
    ) -> None:
        import archimedes.db as _db
        import archimedes.services.live_rigor_gate as lrg
        import archimedes.services.portfolio_backtester as pb
        from archimedes.agents.generation_pipeline import (
            _backtest_and_persist,
            _CandidateResult,
            _Emitter,
        )
        from archimedes.models.strategy_passport_record import StrategyPassportRecord
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        strategy_id = f"gate-call-site-{live_gate_status}"

        test_engine = create_engine(
            f"sqlite:///{tmp_path / 'gate_call_site.db'}",
            connect_args={"check_same_thread": False},
        )
        monkeypatch.setattr(_db, "engine", test_engine)
        monkeypatch.setattr(_db, "SessionLocal", sessionmaker(bind=test_engine, autocommit=False, autoflush=False))
        from archimedes.models import kg, strategy_passport_record, strategy_store  # noqa: F401

        _db.Base.metadata.create_all(bind=test_engine)

        monkeypatch.delenv("GENERATION_PIPELINE_FIXTURE", raising=False)
        monkeypatch.delenv("GENERATION_PIPELINE_SKIP_BACKTEST", raising=False)

        strong_returns = self._strong_returns()
        equity = [1.0]
        for r in strong_returns:
            equity.append(equity[-1] * (1.0 + r))

        fake_result = _result(
            strategy_id=strategy_id,
            equity_curve=equity,
            pbo_score=None,  # the exact pre-fix trigger: backtest_portfolio always leaves this None
            look_ahead_audit_passed=True,
            backtest_engine="portfolio-simulator-v1",
        )
        fake_artifact = {
            "run_id": f"gen-test-{strategy_id}",
            "results": [{"metrics": {"daily_returns": strong_returns}}],
        }

        monkeypatch.setattr(pb, "backtest_portfolio", lambda **kwargs: (fake_result, fake_artifact))

        gate_calls: list[dict] = []

        class _StubVerdict:
            """The four-state shape ``RigorGateVerdict`` has, with ``passes``
            derived from ``status`` exactly as the real one derives it — so this
            double cannot express a decoupled pair the real gate never emits."""

            def __init__(self, status: str) -> None:
                self.status = status
                self.passes = status == "pass"

        def _fake_verdict_from_returns(sid, daily_returns, **kwargs):
            gate_calls.append({"strategy_id": sid, "daily_returns": list(daily_returns), **kwargs})
            return _StubVerdict(status=live_gate_status)

        monkeypatch.setattr(lrg, "verdict_from_returns", _fake_verdict_from_returns)

        class _FakeStore:
            async def push_event(self, job_id: str, body: dict) -> int:
                return 0

        emit = _Emitter(f"job-{strategy_id}", _FakeStore())
        c = _CandidateResult(
            candidate_id="cand-1",
            strategy_name="Gate Call Site",
            thesis="t",
            asset_universe=["SPY"],
            source_papers=[],
            weights={"SPY": 1.0},
            reasoning="r",
            rigor_verdict={},
            passes_rigor=False,
        )

        await _backtest_and_persist(c, strategy_id, emit, num_trials=1)

        with _db.get_session() as session:
            record = session.query(StrategyPassportRecord).filter_by(id=strategy_id).first()

        assert record is not None, "no passport persisted — _backtest_and_persist did not run to completion"

        # The call site must actually call the live gate with the real series
        # extracted from the artifact — not skip it, not feed it something else.
        assert len(gate_calls) == 1, "generation_pipeline must call verdict_from_returns exactly once"
        assert gate_calls[0]["daily_returns"] == pytest.approx(strong_returns)
        assert gate_calls[0]["look_ahead_audit_passed"] is True

        # And the persisted verdict must be EXACTLY what the live gate returned —
        # not a hardcoded value, and not the deleted property's pbo_score-is-None
        # short-circuit (which was unconditionally False regardless of this
        # stub's answer). Parametrized over three states so neither a stuck-True
        # nor a stuck-False persisted value can slip past, and neither can a call
        # site that folds "degenerate" into "fail".
        assert record.rigor_gate_status == live_gate_status, (
            f"persisted rigor_gate_status={record.rigor_gate_status!r} but the live "
            f"gate returned status={live_gate_status!r} — the call site is not "
            "propagating the live verdict verbatim."
        )
        assert record.passes_rigor_gate is (live_gate_status == "pass"), (
            "passes_rigor_gate and rigor_gate_status must be written coupled"
        )
        # Provenance ships with the grade, or the stored verdict is undatable.
        assert record.graded_at is not None
        assert record.gate_version == gate_version()
        assert record.cohort_n == 1
