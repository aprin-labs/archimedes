"""num_trials multiple-testing correction for the N-candidate society (#770).

Hermetic: pure math over seeded return series — no LLM, no chain, no DB.

The agentic society generates N candidates, backtests + rigor-gates all, and keeps the
best. Selection-from-N is itself a multiple-testing search, so the Deflated Sharpe Ratio
must deflate for N + library_size, not library alone (which under-deflated and inflated
the survivor's DSR). These tests pin the chosen formula (approach A) and prove the
correction rejects a best-of-N survivor that the library-only count would have admitted.

Also covers approach B (#822): feeding the REAL candidate-pool pairwise correlation ρ̄
into the same DSR call, instead of the ρ̄=0 (independent-trials) assumption approach A
ships with. The N society candidates share a brief and overlapping universes, so they
are typically correlated — ρ̄=0 over-deflates and over-states the gate's strictness.
"""

from __future__ import annotations

import numpy as np
import pytest
from archimedes.agents.generation_pipeline import (
    _CandidateResult,
    _is_deployable,
    _patch_dsr_with_pool_correlation,
    _rigor_verdict_for,
    _society_num_trials,
)


def _deployability_candidate(*, passing: bool, has_real_rigor: bool, data_source: str) -> _CandidateResult:
    """Minimal candidate to exercise the SSE deployable signal (#937)."""
    return _CandidateResult(
        candidate_id="c1",
        strategy_name="S",
        thesis="t",
        asset_universe=["sSPY"],
        source_papers=[],
        weights={"sSPY": 1.0},
        reasoning="r",
        rigor_verdict={"passing": passing, "data_source": data_source},
        passes_rigor=passing,
        has_real_rigor=has_real_rigor,
    )


class TestDeployableSignal:
    """deployable must be (passing AND real-data-graded), not just passing (#937)."""

    def test_passing_on_real_data_is_deployable(self):
        c = _deployability_candidate(passing=True, has_real_rigor=True, data_source="real")
        assert _is_deployable(c) is True

    def test_passing_on_synthetic_data_is_not_deployable(self):
        # The bug: a synthetic-data-graded candidate can pass the gate but must
        # not be advertised deployable.
        c = _deployability_candidate(passing=True, has_real_rigor=True, data_source="synthetic")
        assert _is_deployable(c) is False

    def test_passing_without_real_rigor_is_not_deployable(self):
        c = _deployability_candidate(passing=True, has_real_rigor=False, data_source="real")
        assert _is_deployable(c) is False

    def test_missing_data_source_defaults_to_synthetic_not_deployable(self):
        c = _CandidateResult(
            candidate_id="c1",
            strategy_name="S",
            thesis="t",
            asset_universe=["sSPY"],
            source_papers=[],
            weights={"sSPY": 1.0},
            reasoning="r",
            rigor_verdict={"passing": True},  # no data_source key → treated as synthetic
            passes_rigor=True,
            has_real_rigor=True,
        )
        assert _is_deployable(c) is False

    def test_failing_gate_is_never_deployable(self):
        c = _deployability_candidate(passing=False, has_real_rigor=True, data_source="real")
        assert _is_deployable(c) is False


class TestSocietyNumTrialsFormula:
    # Decouple #2 (2026-07-09): num_trials is the strategy's OWN candidate pool (N),
    # NOT N + library_size — a strategy's rigor depends only on itself, never on how
    # many other strategies sit in the library.
    def test_is_own_pool_only(self):
        assert _society_num_trials(20) == 20
        assert _society_num_trials(10) == 10

    def test_single_candidate_is_one(self):
        # N=1 (one generated candidate) is a single-trial grade — no library term added.
        assert _society_num_trials(1) == 1

    def test_floored_at_one(self):
        assert _society_num_trials(0) == 1

    def test_independent_of_library_size(self):
        # The whole point: growing the library must NOT change a strategy's
        # num_trials. Pin it structurally — the function must not even ACCEPT
        # a second (library_size) argument, so a refactor can't quietly
        # reintroduce the coupling. (Review follow-up: the previous assertion
        # compared the call to itself, which could never fail.)
        import inspect

        assert len(inspect.signature(_society_num_trials).parameters) == 1
        with pytest.raises(TypeError):
            _society_num_trials(20, 34)  # the old (N, library_size) shape must be rejected


class TestNumTrialsCorrection:
    # A seeded series with a real positive edge whose DSR p-value brackets the gate
    # between a single-trial grade (num_trials=1) and a best-of-N pool (N=20). OOS is
    # identical at both counts, so the ONLY thing that flips the verdict is the
    # pool-based num_trials deflation — the honest selection-bias correction.
    _MU, _SD, _N, _SEED = 0.0009, 0.009, 300, 4

    def _series(self):
        return list(np.random.default_rng(self._SEED).normal(self._MU, self._SD, self._N))

    def test_num_trials_overfit_survivor_fails_society_path(self):
        """A best-of-N survivor that passes as a single trial FAILS once the strategy's
        OWN pool (N=20) deflates it — the selection bias, measured on the strategy itself."""
        series = self._series()
        single_trial = _rigor_verdict_for(series, num_trials=1, lookahead_passed=True)
        society = _rigor_verdict_for(series, num_trials=_society_num_trials(20), lookahead_passed=True)

        assert single_trial["passing"] is True, "fixture must pass as a single self-contained trial"
        assert society["passing"] is False, "best-of-N pool deflation must reject the inflated survivor"
        # The flip is the DSR deflation, not OOS: OOS Sharpe is identical at both counts.
        assert single_trial["oos_sharpe"] == society["oos_sharpe"]
        assert society["dsr_p_value"] < single_trial["dsr_p_value"]

    def test_dsr_pvalue_monotonic_stricter_in_trials(self):
        """More trials can only deflate harder — the property the additive count relies on."""
        series = self._series()
        p_small = _rigor_verdict_for(series, num_trials=4, lookahead_passed=True)["dsr_p_value"]
        p_large = _rigor_verdict_for(series, num_trials=24, lookahead_passed=True)["dsr_p_value"]
        assert p_large <= p_small


def _candidate(candidate_id: str, return_series: list[float], num_trials: int) -> _CandidateResult:
    """Build a buy-and-hold society candidate with a real DSR verdict already computed
    at ρ̄=0 (approach A) — the state each candidate is in after the debate runner,
    before the post-loop pool-correlation patch runs."""
    verdict = _rigor_verdict_for(return_series, num_trials=num_trials, lookahead_passed=True)
    verdict["pbo"] = 0.0  # mirrors _patch_pbo's N<2 default; patch under test runs after PBO
    return _CandidateResult(
        candidate_id=candidate_id,
        strategy_name=f"Strategy {candidate_id}",
        thesis="t",
        asset_universe=["sSPY"],
        source_papers=[],
        weights={"sSPY": 1.0},
        reasoning="r",
        rigor_verdict=verdict,
        passes_rigor=verdict.get("passing", False),
        return_series=return_series,
        dsr_num_trials=num_trials,
    )


class TestPoolCorrelationDSR:
    """Approach B (#822): feed the real candidate-pool ρ̄ into the society DSR, replacing
    the ρ̄=0 (independent-trials) assumption approach A ships with for the N generated
    candidates' own DSR call. ``_patch_dsr_with_pool_correlation`` is the post-loop patch
    ``run_generation`` calls after ``_patch_pbo``, once every candidate's return series
    is known — mirrors the existing PBO patch's two-pass shape."""

    _NUM_TRIALS = 24  # library_size=4 + selection_pool_size=20, same as TestNumTrialsCorrection

    def _correlated_pool(self, n: int, rho_target: float, seed: int = 11) -> list[list[float]]:
        """N series sharing a common factor + idiosyncratic noise, engineered so the
        realized average pairwise correlation is close to ``rho_target``. Same brief,
        overlapping universe — the scenario #822 describes."""
        rng = np.random.default_rng(seed)
        t = 300
        factor = rng.normal(0.0009, 0.009, t)
        idio_scale = 0.009 * np.sqrt(max(1e-9, (1.0 - rho_target) / max(rho_target, 1e-9)))
        series = []
        for _ in range(n):
            idio = rng.normal(0.0, idio_scale, t)
            series.append((np.sqrt(rho_target) * factor + np.sqrt(1.0 - rho_target) * idio).tolist())
        return series

    def _independent_pool(self, n: int, seed: int = 23) -> list[list[float]]:
        rng = np.random.default_rng(seed)
        return [rng.normal(0.0009, 0.009, 300).tolist() for _ in range(n)]

    def test_correlated_pool_relaxes_dsr_pvalue_never_tightens(self):
        """Monotonicity pin (acceptance criterion #1): a correlated pool's DSR p-value
        is >= the ρ̄=0 (approach A) value for every candidate — the correction only
        relaxes over-deflation, never tightens the gate beyond approach A."""
        series_list = self._correlated_pool(n=5, rho_target=0.75)
        candidates = [_candidate(f"cand_{i}", s, self._NUM_TRIALS) for i, s in enumerate(series_list)]
        baseline_p = [c.rigor_verdict["dsr_p_value"] for c in candidates]  # ρ̄=0, computed in _candidate()

        _patch_dsr_with_pool_correlation(candidates)

        patched_p = [c.rigor_verdict["dsr_p_value"] for c in candidates]
        for base, patched in zip(baseline_p, patched_p, strict=True):
            assert patched >= base, "approach B must relax (never tighten) the ρ̄=0 p-value"
        # A materially correlated pool should show a REAL relaxation somewhere, not a no-op.
        assert any(p > b for b, p in zip(baseline_p, patched_p, strict=True))

    def test_correlated_pool_deflates_less_than_the_nominal_count(self):
        """Acceptance criterion #1 (#822): a correlated pool must deflate LESS than the
        same pool treated as independent. Cross-check via compute_dsr directly at the
        measured ρ̄ vs. ρ̄=0, on the same series the patch would have used.

        Asserts the direction only, which is what #822 asked for and what holds under
        the ``√(1−ρ̄)`` shrinkage #1559 installed. Named for an ``N_eff`` mechanism
        until 2026-08-31; that form never was the correlated ``E[max]`` (see #1558)."""
        from archimedes.services.rigor_evaluator import compute_average_pairwise_correlation, compute_dsr

        series_list = self._correlated_pool(n=5, rho_target=0.75)
        rho_bar = compute_average_pairwise_correlation({str(i): s for i, s in enumerate(series_list)})
        assert 0.0 < rho_bar < 1.0, "the engineered pool must realize a real, non-trivial correlation"

        _, p_nominal = compute_dsr(series_list[0], num_trials=self._NUM_TRIALS, average_correlation=0.0)
        _, p_neff = compute_dsr(series_list[0], num_trials=self._NUM_TRIALS, average_correlation=rho_bar)
        assert p_neff >= p_nominal

    def test_fewer_than_two_series_falls_back_to_rho_zero(self):
        """Acceptance criterion #2: with < 2 candidate return series, stay on approach A
        (ρ̄=0) unchanged — no correlation is estimable from a single series."""
        series_list = self._correlated_pool(n=1, rho_target=0.75)
        candidates = [_candidate("cand_0", series_list[0], self._NUM_TRIALS)]
        before = dict(candidates[0].rigor_verdict)

        _patch_dsr_with_pool_correlation(candidates)

        assert candidates[0].rigor_verdict["dsr_p_value"] == before["dsr_p_value"]
        assert candidates[0].rigor_verdict["dsr"] == before["dsr"]

    def test_no_return_series_at_all_falls_back_to_rho_zero(self):
        """Zero eligible series (e.g. every candidate too-short / fixture path) is also
        the < 2 fallback — must not raise."""
        candidates = [_candidate("cand_0", [], self._NUM_TRIALS)]
        before = dict(candidates[0].rigor_verdict)

        _patch_dsr_with_pool_correlation(candidates)

        assert candidates[0].rigor_verdict == before

    def test_independent_pool_is_a_near_noop(self):
        """A genuinely independent pool (ρ̄ ≈ 0) should leave the DSR verdict close to the
        approach-A baseline — the correction only bites when candidates are actually
        correlated, consistent with ``√(1−ρ̄) → 1`` as ρ̄ → 0."""
        series_list = self._independent_pool(n=5)
        candidates = [_candidate(f"cand_{i}", s, self._NUM_TRIALS) for i, s in enumerate(series_list)]
        baseline_p = [c.rigor_verdict["dsr_p_value"] for c in candidates]

        _patch_dsr_with_pool_correlation(candidates)

        patched_p = [c.rigor_verdict["dsr_p_value"] for c in candidates]
        for base, patched in zip(baseline_p, patched_p, strict=True):
            assert patched >= base  # monotonicity still holds even near ρ̄=0
            assert abs(patched - base) < 0.02, "near-independent pool should barely move the p-value"

    def test_fusion_candidates_excluded_from_pool_and_untouched(self):
        """has_real_rigor candidates (fusion/debate) carry their own CSCV-derived DSR and
        must never be folded into the buy-and-hold pool correlation, or patched — mirrors
        _patch_pbo's existing scope boundary."""
        series_list = self._correlated_pool(n=3, rho_target=0.8)
        buy_and_hold = [_candidate(f"cand_{i}", s, self._NUM_TRIALS) for i, s in enumerate(series_list)]
        fusion_verdict = {
            "dsr": 1.23,
            "dsr_p_value": 0.5,
            "pbo": 0.1,
            "oos_sharpe": 0.4,
            "in_sample_sharpe": 0.5,
            "lookahead_audit_passed": True,
            "passing": True,
        }
        fusion_candidate = _CandidateResult(
            candidate_id="cand_fusion",
            strategy_name="Fusion X",
            thesis="t",
            asset_universe=["sSPY"],
            source_papers=[],
            weights={},
            reasoning="r",
            rigor_verdict=dict(fusion_verdict),
            passes_rigor=True,
            has_real_rigor=True,
            return_series=series_list[0],  # even if it "looks like" pool data, must be ignored
            generation_method="fusion",
        )
        candidates = [*buy_and_hold, fusion_candidate]

        _patch_dsr_with_pool_correlation(candidates)

        assert fusion_candidate.rigor_verdict == fusion_verdict, "fusion candidate's real DSR must be untouched"


# ── PORTFOLIO_BACKTESTER writer hash unification (issue #1347 follow-up) ────
#
# NOT hermetic-in-the-"no DB" sense the module docstring above describes —
# these seed a per-test in-memory sqlite DB (monkeypatching
# `archimedes.db.engine`/`SessionLocal` directly, NOT the
# `monkeypatch.setenv DATABASE_URL` + `init_db()` pattern used elsewhere in
# this suite — see `_isolated_db`'s own docstring for why that pattern
# doesn't actually isolate here) — but still fully offline: no live Redis,
# no live Postgres, no network, no LLM.
#
# `_backtest_and_persist`'s `content_hash` computation used to be an ad hoc
# `hashlib.sha256(artifact_json)` over an artifact that
# `portfolio_backtester.backtest_portfolio` stamps with its own fresh
# `run_id`/`timestamp_utc` on every call — the same volatile-field shape
# `canonical_artifact_hash` was fixed to exclude, but this ONE writer never
# routed through that fixed function. Two "real" backtests of identical
# content through this exact writer therefore never deduped, even after the
# `canonical_artifact_hash` fix landed. This test exercises the REAL
# `_backtest_and_persist` closure end to end — not a reimplementation of its
# hashing logic — with `backtest_portfolio` mocked at the module boundary
# (CLAUDE.md: mock at boundaries, not internals) so it is genuinely tied to
# the writer's own code, not to a copy of it.


class TestPortfolioBacktesterWriterDedupe:
    @pytest.fixture(autouse=True)
    def _isolated_db(self, monkeypatch):
        """A genuinely per-test-isolated DB, NOT the ``monkeypatch.setenv
        DATABASE_URL`` + ``init_db()`` pattern used elsewhere in this suite.

        ``archimedes.db``'s ``engine``/``SessionLocal`` are module-level
        singletons bound to ``DATABASE_URL`` at FIRST IMPORT — and
        ``conftest.py``'s own top-level import of
        ``archimedes.services.strategy_provider`` transitively imports
        ``archimedes.db`` during collection, before any test fixture runs.
        Setting the env var afterward is a no-op against the already-created
        engine, so that pattern silently falls back to sharing
        ``backend/archimedes_chat.db`` on disk — which then ACCUMULATES rows
        across separate test runs (verified: two runs of this test's own
        precursor left 3 rows for one strategy_id, not the expected 1/2 —
        pre-existing rows from a prior process leaking in). Monkeypatching
        the module's ``engine``/``SessionLocal`` attributes directly — what
        ``get_session()`` actually reads at call time — sidesteps the
        singleton-binding problem instead of fighting it.

        ``poolclass=StaticPool`` is required, not cosmetic: SQLAlchemy's
        default pool for a ``sqlite:///:memory:`` URL is
        ``SingletonThreadPool`` — one connection PER THREAD, i.e. a fresh,
        empty in-memory DB on any thread other than the one that ran
        ``create_all``. ``_backtest_and_persist`` does its DB work inside
        ``asyncio.to_thread(...)`` specifically to keep it off the event
        loop, so without ``StaticPool`` (one shared connection across every
        thread) the worker thread sees "no such table: backtest_results" —
        caught by ``_backtest_and_persist``'s own broad ``except Exception``
        and merely logged, so the failure mode is a silently-empty table, not
        a raised error. Reproduced directly before adding this.
        """
        import sqlalchemy as sa
        from archimedes.models import kg, strategy_passport_record  # noqa: F401 — register their tables
        from archimedes.models.chat import Base
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        engine = sa.create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=engine)
        session_factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

        import archimedes.db as db_module

        monkeypatch.setattr(db_module, "engine", engine)
        monkeypatch.setattr(db_module, "SessionLocal", session_factory)

    @staticmethod
    def _candidate() -> _CandidateResult:
        return _CandidateResult(
            candidate_id="cand-1",
            strategy_name="Test Portfolio Strategy",
            thesis="thesis",
            asset_universe=["sSPY", "sGLD"],
            source_papers=[],
            weights={"sSPY": 0.6, "sGLD": 0.4},
            reasoning="r",
            rigor_verdict={},
            passes_rigor=True,
            generation_method="portfolio_agent_streaming",
        )

    @staticmethod
    def _fake_artifact(run_id: str, timestamp_utc: str) -> dict:
        """Shaped like the real ``backtest_portfolio`` artifact
        (``portfolio_backtester.py``'s own construction): ``run_id`` and
        ``timestamp_utc`` as top-level siblings, everything else content."""
        return {
            "run_id": run_id,
            "timestamp_utc": timestamp_utc,
            "operations": ["sSPY", "sGLD"],
            "strategy": {"path": "generated/strat-1", "class_name": "GeneratedStaticRebalance"},
            "assumptions": {"transaction_cost_bps": 10, "backtest_engine": "portfolio-simulator-v1"},
            "results": [
                {
                    "operation": "PORTFOLIO",
                    "symbol": "PORTFOLIO",
                    "metrics": {
                        "daily_returns": [0.001, 0.002, -0.001, 0.0015] * 5,
                        "sharpe_ratio": 0.8,
                    },
                }
            ],
        }

    @staticmethod
    def _fake_result() -> "BacktestResult":  # noqa: F821,UP037 — deferred import, only inside this function
        from archimedes.models.backtest import BacktestResult

        return BacktestResult(
            strategy_id="strat-1",
            sharpe_ratio=0.8,
            sortino_ratio=0.9,
            max_drawdown=0.1,
            cagr=0.15,
            calmar_ratio=1.5,
            win_rate=0.55,
            profit_factor=1.2,
            total_trades=4,
            avg_holding_period_days=30.0,
            correlation_to_spy=0.5,
            correlation_to_btc=0.1,
            equity_curve=[1.0, 1.01, 1.02],
            monthly_returns=[0.01],
            backtest_start=None,
            backtest_end=None,
            look_ahead_audit_passed=True,
            backtest_engine="portfolio-simulator-v1",
        )

    async def test_second_persist_of_identical_content_is_skipped(self, monkeypatch):
        """Two persists through ``_backtest_and_persist`` whose
        ``backtest_portfolio`` artifacts differ ONLY in
        ``run_id``/``timestamp_utc`` must collapse to ONE ``backtest_results``
        row — the exact production symptom (identical strategy, identical
        content, re-run through the society/portfolio-backtester path).

        Mutation-proven: fails against the pre-fix ad hoc
        ``hashlib.sha256(artifact_json)`` — see the PR body for the
        revert/re-apply transcript.
        """
        from archimedes.agents.generation_pipeline import _backtest_and_persist
        from archimedes.db import get_session
        from archimedes.models.backtest_store import BacktestResultRecord
        from archimedes.services.backtest_repository import SOURCE_PIPELINE_PORTFOLIO_BACKTESTER

        artifacts = iter(
            [
                self._fake_artifact("gen-20260501T000000Z-strat1", "2026-05-01T00:00:00+00:00"),
                self._fake_artifact("gen-20260502T000000Z-strat1", "2026-05-02T00:00:00+00:00"),
            ]
        )

        def _fake_backtest_portfolio(*, strategy_id, weights, paper_title=None, num_trials_for_dsr=1, **kwargs):
            return self._fake_result(), next(artifacts)

        monkeypatch.setattr(
            "archimedes.services.portfolio_backtester.backtest_portfolio",
            _fake_backtest_portfolio,
        )

        class _NullEmitter:
            async def emit(self, event, **payload):
                return 0

        strategy_id = "strat-1"
        c = self._candidate()
        emit = _NullEmitter()

        await _backtest_and_persist(c, strategy_id, emit, num_trials=1)
        await _backtest_and_persist(c, strategy_id, emit, num_trials=1)

        with get_session() as session:
            rows = (
                session.query(BacktestResultRecord)
                .filter(
                    BacktestResultRecord.strategy_id == strategy_id,
                    BacktestResultRecord.source_pipeline == SOURCE_PIPELINE_PORTFOLIO_BACKTESTER,
                )
                .all()
            )
            row_info = [(r.id, r.content_hash, r.run_id) for r in rows]

        assert len(rows) == 1, (
            "expected the second persist to be SKIPPED (identical content, "
            f"differing only in run_id/timestamp_utc); got {len(rows)} rows: {row_info}"
        )
