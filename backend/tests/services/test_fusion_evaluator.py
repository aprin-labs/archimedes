"""Tests for the fusion evaluator pipeline.

History note: the original test file (PR for issue #128) shipped with several
tautological assertions (``assert X is None or X is not None``) and a
``or True`` short-circuit that made the headline "DSL Faber matches seed"
contract test pass unconditionally. This file's tests have been rewritten to
actually verify the things they name — see ``test_faber_dsl_matches_seed``
docstring for the explicit framing of what is and isn't validated here
(short version: deterministic execution + structural correctness against the
in-tree synthetic data, NOT bit-identity with the analytics-engine's
real-SPY Faber backtest — that contract requires real SPY data and is a
separate piece of work).
"""

from __future__ import annotations

import random
from datetime import date
from pathlib import Path

from archimedes.services.fusion_evaluator import (
    CONSTRUCTION_SINGLE_ASSET,
    CONSTRUCTION_SLEEVES,
    ENGINE_SINGLE_FEED,
    ENGINE_SLEEVES,
    BacktestMetrics,
    _run_variant_backtest,
    apply_rigor_gate,
    evaluate_fusion_spec,
    is_admissible_source,
    run_dsl_backtest,
    run_dsl_backtest_portfolio,
)
from archimedes.services.strategy_dsl import FABER_2007_SPEC, validate_strategy_spec

_SPY_FIXTURE = Path(__file__).parent.parent / "fixtures" / "spy_ohlcv_2004_2026.csv"


def _audited_spec():
    """A spec that clears the structural look-ahead audit.

    ``apply_rigor_gate``'s look-ahead leg is a REAL audit now
    (``services/dsl_lookahead_audit.py``): with no spec to verify it returns
    ``pending``, which is deliberately NOT a pass. Tests below that
    exercise the OTHER legs (provenance, OOS, PBO) pass this so the look-ahead
    leg is satisfied honestly rather than by a bypass. Tests that are ABOUT the
    look-ahead leg live in test_dsl_lookahead_audit.py.
    """
    return validate_strategy_spec(FABER_2007_SPEC)


def _noisy_variant_curve(curve: list[float], seed: int = 42, noise_sigma: float = 0.002) -> list[float]:
    """Build a second equity curve from the same per-bar drift as ``curve`` plus
    independent Gaussian noise, for use as a non-identical CSCV variant.

    Audit 06-14 (Q4) made ``apply_rigor_gate`` fail-closed when ``pbo_score is
    None`` (no variants supplied). Tests that need a genuinely *passing*
    verdict for a single high-Sharpe curve now must also supply >= 2 variant
    backtests so a real CSCV PBO is computed. A perfectly-correlated duplicate
    of ``curve`` yields PBO == 1.0 (the IS-best is always the OOS-worst on an
    identical series) — i.e. an automatic fail. Adding independent per-bar
    noise to the second variant breaks that perfect correlation so PBO lands
    at 0.0 (the original curve's steady drift dominates every split), letting
    the test's intended "this is a passing strategy" setup hold honestly.
    """
    rng = random.Random(seed)
    drifts = [curve[i] / curve[i - 1] - 1.0 for i in range(1, len(curve))]
    noisy = [curve[0]]
    for drift in drifts:
        noisy.append(noisy[-1] * (1.0 + drift + rng.gauss(0, noise_sigma)))
    return noisy


def _two_variant_set(curve: list[float], data_source: str = "csv:test.csv") -> dict[str, BacktestMetrics]:
    """Minimal >= 2-entry ``variants_metrics`` dict so ``apply_rigor_gate``
    computes a real (non-``None``) CSCV PBO — see ``_noisy_variant_curve``.

    Reuses ``_metrics_from_curve`` (defined below; module-level functions
    resolve at call time, so the forward reference is safe).
    """
    return {
        "v0": _metrics_from_curve(curve, data_source=data_source),
        "v1": _metrics_from_curve(_noisy_variant_curve(curve), data_source=data_source),
    }


def _make_high_sharpe_metrics(data_source: str = "csv:test.csv") -> BacktestMetrics:
    """800-bar equity curve alternating +0.3%/+0.1% per day.

    Excess Sharpe ≈ 1.8/bar (annualised DSR ≈ 28) — well above the p≥0.95 gate
    even after 5% rf subtraction. Used by provenance tests that need a *passing*
    strategy to verify the admissibility logic, independent of Faber's stats.
    """
    curve = [100_000.0]
    for i in range(799):
        curve.append(curve[-1] * (1.003 if i % 2 == 0 else 1.001))
    return BacktestMetrics(
        sharpe_ratio=2.0,
        sortino_ratio=2.5,
        max_drawdown=0.05,
        cagr=0.20,
        calmar_ratio=4.0,
        win_rate=0.6,
        total_trades=100,
        avg_holding_period_days=5.0,
        equity_curve=curve,
        monthly_returns=[0.01] * 24,
        backtest_start=None,
        backtest_end=None,
        data_source=data_source,
        # Stands in for a real cerebro run: the broker cheat-on-close/open check
        # ran and passed. Without it the look-ahead audit is incomplete and
        # cannot reach a `pass` (that is the point of the None default).
        broker_cheat_check_passed=True,
    )


class TestFixtureFusionToLibrary:
    """End-to-end: fusion spec → backtest → library upsert (no LLM)."""

    def test_fixture_fusion_to_library(self):
        """A fixture-based fusion spec produces a library entry with real metrics."""
        result = evaluate_fusion_spec(FABER_2007_SPEC)
        assert result.success, f"evaluation failed: {result.error}"
        assert result.backtest is not None

        # Equity curve must come from the per-bar Observer analyzer (not the
        # pre-existing linear-interpolation stub which always produced
        # length=n_bars+1 of equally-spaced values — see equity-curve fix).
        assert len(result.backtest.equity_curve) > 100, (
            f"equity curve suspiciously short: {len(result.backtest.equity_curve)} points"
        )

        # Sharpe / Sortino / Max DD must be real floats (not NaN/inf).
        import math

        assert math.isfinite(result.backtest.sharpe_ratio)
        assert math.isfinite(result.backtest.sortino_ratio)
        assert math.isfinite(result.backtest.max_drawdown)
        assert math.isfinite(result.backtest.cagr)

        # Max drawdown is bounded to a sensible range (0 ≤ MaxDD ≤ 1).
        assert 0.0 <= result.backtest.max_drawdown <= 1.0

        # Rigor verdict is fully populated.
        assert result.rigor is not None
        assert isinstance(result.rigor.passing, bool)

    def test_fusion_result_has_generation_method(self):
        """The result carries enough metadata to identify it as fusion-generated."""
        result = evaluate_fusion_spec(FABER_2007_SPEC)
        assert result.success
        assert result.spec.source_arxiv_ids == ["0706.1497"]


class TestFaberDslMatchesSeed:
    """The DSL-interpreted Faber strategy reproduces deterministic results.

    **Scope of this test:** the DSL pipeline executes end-to-end and produces
    deterministic, reproducible metrics on the in-tree synthetic data path.

    **Out of scope (deferred):** bit-identity with the analytics-engine's
    hand-written Faber backtest on real 2004-2026 SPY data. That contract
    requires:
      (a) shipping a real SPY OHLCV fixture (~330 KB of CSV), AND
      (b) careful semantic alignment between the DSL's interpretation of
          ``rebalance_frequency=monthly`` / ``position_sizing=full_invested_when_in_market``
          and the analytics-engine seed strategy's specific implementation
          choices around dividend handling, transaction-cost timing, and
          end-of-bar vs next-bar execution.

    The canonical Faber Sharpe is ``0.6335`` per ``analytics-engine/
    strategies/backtest_fixtures.json`` (key ``faber_2007_sma200_timing``).
    Achieving DSL Faber within ±0.10 of that figure is tracked as a
    separate issue; this test guards the substrate.
    """

    def test_faber_dsl_runs_end_to_end_deterministically(self):
        """Two consecutive runs of the DSL Faber on the same synthetic data
        must produce identical Sharpe / CAGR / Max DD — the DSL + interpreter
        is a deterministic pipeline.
        """
        r1 = evaluate_fusion_spec(FABER_2007_SPEC)
        r2 = evaluate_fusion_spec(FABER_2007_SPEC)
        assert r1.success and r2.success

        # Per-bar equity capture means the Sharpe is computed from real
        # broker values, not from a linear interpolation — making this
        # determinism check substantively stronger than the pre-fix version.
        assert r1.backtest.sharpe_ratio == r2.backtest.sharpe_ratio
        assert r1.backtest.cagr == r2.backtest.cagr
        assert r1.backtest.max_drawdown == r2.backtest.max_drawdown
        assert r1.backtest.equity_curve == r2.backtest.equity_curve

    def test_faber_dsl_produces_structurally_correct_backtest(self):
        """The DSL Faber produces a backtest that's structurally consistent —
        the SMA-200 filter actually fires (some trades happen), the equity
        curve has the right shape (one point per bar after warmup), and the
        derived metrics are arithmetically self-consistent.
        """
        result = evaluate_fusion_spec(FABER_2007_SPEC)
        assert result.success
        bt = result.backtest

        # Equity curve has the right shape — 22 years × ~252 trading days
        # minus 200 warmup bars ≈ 5350+ bars. (The synthetic data feed
        # spans 2004-01-02 to 2026-04-30; without an SMA-200 warmup this
        # would be ~5560 daily bars.)
        assert 4000 < len(bt.equity_curve) < 7000, f"equity curve length suspicious: {len(bt.equity_curve)}"

        # Calmar identity check: calmar = cagr / max_dd (when max_dd > 0).
        if bt.max_drawdown > 0.001:
            expected_calmar = bt.cagr / bt.max_drawdown
            # Allow tiny float drift from the round(.., 4) calls.
            assert abs(bt.calmar_ratio - expected_calmar) < 0.001, (
                f"Calmar identity violated: {bt.calmar_ratio} vs {expected_calmar}"
            )


class TestRigorGateAppliesToDslOutput:
    """Rigor gate must run on DSL-interpreted strategies and surface honest
    rigor results — every field is either a real computed value or
    explicitly ``None`` (NOT 0.0 as a misleading placeholder).
    """

    def test_rigor_gate_applies_to_dsl_output(self):
        result = evaluate_fusion_spec(FABER_2007_SPEC)
        assert result.success
        assert result.rigor is not None

        rigor = result.rigor

        # DSR is a real computed float (positive for trending strategies,
        # could be negative for losing ones — both are valid outcomes that
        # the rigor gate's pass/fail logic responds to).
        import math

        assert rigor.dsr is not None, "DSR must be computed for DSL output"
        assert math.isfinite(rigor.dsr)
        assert rigor.dsr_p_value is not None
        assert 0.0 <= rigor.dsr_p_value <= 1.0

        # PBO for a single strategy is honestly None (not 0.0 — that was the
        # pre-fix misleading default that made every fusion strategy look
        # like it had passed the overfitting test it never ran).
        assert rigor.pbo_score is None, (
            "PBO must be None for single-strategy DSL output. Setting it to "
            "0.0 was misleading; real CSCV PBO needs a parameter-variant grid."
        )

        # OOS Sharpe is a real computed float.
        assert rigor.oos_sharpe is not None
        assert math.isfinite(rigor.oos_sharpe)

        # Look-ahead is DERIVED by the structural audit, not declared. Faber
        # sits inside the audited interpreter surface and the run's broker check
        # passed, so this is a real computed "pass" — not the constant True the
        # removed self-declared look_ahead_safe flag used to produce, and not a
        # word the generator supplied.
        assert rigor.look_ahead_status == "pass"
        assert rigor.look_ahead_clean is True
        assert rigor.look_ahead_label.startswith("PASS (structural)")
        assert "self-attested" not in rigor.look_ahead_label

        # The gate produces a deterministic boolean verdict.
        assert isinstance(rigor.passing, bool)
        assert rigor.num_trials > 0


class TestInvalidSpecHandling:
    """Invalid specs must fail gracefully."""

    def test_invalid_spec_returns_error(self):
        bad_spec = {"name": "broken"}
        result = evaluate_fusion_spec(bad_spec)
        assert not result.success
        assert result.error is not None
        assert "missing required" in result.error.lower() or "invalid" in result.error.lower()

    def test_legacy_self_declared_flag_no_longer_gates_evaluation(self):
        """A persisted spec still carrying the removed flag evaluates normally.

        Both directions: the pipeline neither rejects a legacy ``false`` nor
        credits a legacy ``true``. The declaration is inert.
        """
        verdicts = {}
        for declared in (True, False):
            result = evaluate_fusion_spec({**FABER_2007_SPEC, "look_ahead_safe": declared})
            assert result.success, f"legacy look_ahead_safe={declared} must not block evaluation"
            assert result.error is None
            verdicts[declared] = result.rigor

        # The declaration is inert in BOTH directions: the two runs produce the
        # same derived verdict. A legacy `false` is not honoured as a failure and
        # a legacy `true` is not credited as a pass — the value comes from the
        # structural audit of the spec, which is identical either way.
        assert verdicts[True].look_ahead_status == verdicts[False].look_ahead_status == "pass"
        assert verdicts[True].look_ahead_label == verdicts[False].look_ahead_label
        assert verdicts[True].look_ahead_clean is verdicts[False].look_ahead_clean is True


class TestFusionWithVariantsComputesRealPbo:
    """Integration tests for CSCV PBO via parameter-variant grids.

    These tests exercise the full variant backtest pipeline and verify that
    real PBO values (not None, not 0.0) are produced from the CSCV algorithm.
    """

    def test_fusion_with_variants_computes_real_pbo(self):
        """A spec with 5 SMA variants must produce a real float pbo_score in
        [0.0, 1.0], NOT None and NOT exactly 0.0."""
        spec_dict = {
            **FABER_2007_SPEC,
            "parameter_variants": {"sma_200": [100, 150, 200, 250, 300]},
        }
        result = evaluate_fusion_spec(spec_dict)
        assert result.success, f"evaluation failed: {result.error}"
        assert result.rigor is not None

        pbo = result.rigor.pbo_score
        assert pbo is not None, "PBO must be computed when >= 2 parameter variants are provided"
        assert pbo != 0.0, (
            "PBO of 0.0 is misleading; the CSCV algorithm on 5 SMA variants "
            "over ~5560 bars should produce a non-zero overfitting probability"
        )
        assert 0.0 <= pbo <= 1.0, f"PBO must be in [0.0, 1.0], got {pbo}"

    def test_fusion_without_variants_pbo_stays_none(self):
        """No parameter_variants → pbo_score is None."""
        result = evaluate_fusion_spec(FABER_2007_SPEC)
        assert result.success
        assert result.rigor is not None
        assert result.rigor.pbo_score is None, "PBO must be None when no parameter_variants are provided"

    def test_fusion_variants_too_few_pbo_stays_none(self):
        """A single variant entry (< 2) means no meaningful PBO → None."""
        # 1 variant value is rejected at validation (needs >= 2), so test with
        # a spec that has parameter_variants but only 1 entry — this should
        # raise a validation error. Instead, test the apply_rigor_gate path
        # directly with 1-entry variants_metrics.
        result = evaluate_fusion_spec(FABER_2007_SPEC)
        assert result.success
        metrics = result.backtest

        single_variant = {"base": metrics}
        verdict = apply_rigor_gate(metrics, variants_metrics=single_variant)
        assert verdict.pbo_score is None, "PBO must be None when fewer than 2 variant backtests are provided"

    def test_fusion_short_variant_window_pbo_stays_none_and_fails(self):
        """#918: variant series shorter than the CSCV partition count (S=16) are
        non-computable. compute_pbo returns an all-0.0 sentinel there — a spurious
        'best possible' PASS — so the fusion path must guard it: pbo_score must be
        None (fail-closed) and the verdict must NOT pass. A ~5-day fusion window
        (6-point curve → 5 daily returns, well under S=16) is the canonical
        trigger from the issue."""
        short_curve_a = [100_000.0 * (1.002**i) for i in range(6)]
        short_curve_b = [100_000.0 * (1.001**i) for i in range(6)]
        variants = {
            "v0": _metrics_from_curve(short_curve_a),
            "v1": _metrics_from_curve(short_curve_b),
        }
        verdict = apply_rigor_gate(_metrics_from_curve(short_curve_a), variants_metrics=variants)
        assert verdict.pbo_score is None, (
            "a <16-bar variant window is non-computable; PBO must be None, not the 0.0 sentinel"
        )
        assert verdict.passing is False, "a non-computable PBO must FAIL the fusion gate, not pass it"

    def test_fusion_high_pbo_fails_rigor_gate(self):
        """A synthetic overfit grid where PBO > 0.5 must cause passing=False.

        Constructs two equity curves with dramatically different profiles: one
        that surges early and fades, another that fades early then surges. This
        creates the IS/OOS reversal pattern that CSCV detects as overfitting.
        """

        n = 5000

        # Strategy A: strong early returns, weak late returns.
        curve_a = [100_000.0]
        for i in range(n):
            half = n / 2
            daily_ret = 0.003 if i < half else -0.001
            curve_a.append(curve_a[-1] * (1.0 + daily_ret))

        # Strategy B: weak early returns, strong late returns.
        curve_b = [100_000.0]
        for i in range(n):
            half = n / 2
            daily_ret = -0.001 if i < half else 0.003
            curve_b.append(curve_b[-1] * (1.0 + daily_ret))

        metrics_a = BacktestMetrics(
            sharpe_ratio=1.0,
            sortino_ratio=1.0,
            max_drawdown=0.2,
            cagr=0.1,
            calmar_ratio=0.5,
            win_rate=0.55,
            total_trades=50,
            avg_holding_period_days=10.0,
            equity_curve=curve_a,
            monthly_returns=[],
            backtest_start=None,
            backtest_end=None,
        )
        metrics_b = BacktestMetrics(
            sharpe_ratio=1.0,
            sortino_ratio=1.0,
            max_drawdown=0.2,
            cagr=0.1,
            calmar_ratio=0.5,
            win_rate=0.55,
            total_trades=50,
            avg_holding_period_days=10.0,
            equity_curve=curve_b,
            monthly_returns=[],
            backtest_start=None,
            backtest_end=None,
        )

        variants = {"strategy_a": metrics_a, "strategy_b": metrics_b}
        verdict = apply_rigor_gate(metrics_a, variants_metrics=variants)

        assert verdict.pbo_score is not None, "PBO must be computed for 2 variants"
        assert verdict.pbo_score > 0.5, (
            f"Expected high PBO (> 0.5) for IS/OOS reversal pattern, got {verdict.pbo_score}"
        )
        assert verdict.passing is False, f"Rigor gate must fail when PBO > 0.5, but passing={verdict.passing}"


class TestDataProvenanceGate:
    """A strategy can only be admissible if its rigor was computed on real data."""

    def test_is_admissible_source_helper(self):
        assert is_admissible_source("csv:spy_ohlcv_2004_2026.csv") is True
        assert is_admissible_source("provided") is True
        assert is_admissible_source("synthetic") is False

    def test_synthetic_run_is_labeled_and_not_admissible(self):
        # No data feed → synthetic prices → must never be Tier-1 admissible,
        # even if the statistics happen to "pass".
        result = evaluate_fusion_spec(FABER_2007_SPEC)
        assert result.backtest is not None
        assert result.backtest.data_source == "synthetic"
        assert result.rigor is not None
        assert result.rigor.data_source == "synthetic"
        assert result.rigor.admissible is False
        assert result.admissible is False

    def test_real_csv_run_is_labeled_real(self):
        spec = validate_strategy_spec(FABER_2007_SPEC)
        metrics = run_dsl_backtest(spec, data_csv_path=_SPY_FIXTURE)
        assert metrics.data_source == "csv:spy_ohlcv_2004_2026.csv"

    def test_real_data_passing_strategy_is_admissible(self):
        # Use a high-Sharpe synthetic equity curve with real data provenance.
        # Faber's 6.7% CAGR barely exceeds the 5% rf, so its DSR p-value falls
        # short of 0.95 — this test is about the provenance gate, not Faber's stats.
        # A 2-entry variant set is supplied so CSCV PBO is computed (audit
        # 06-14, Q4: pbo_score=None now fails closed) — see _two_variant_set.
        curve = _make_high_sharpe_metrics(data_source="csv:spy_ohlcv_2004_2026.csv").equity_curve
        metrics = _make_high_sharpe_metrics(data_source="csv:spy_ohlcv_2004_2026.csv")
        verdict = apply_rigor_gate(
            metrics,
            variants_metrics=_two_variant_set(curve, data_source="csv:spy.csv"),
            spec=_audited_spec(),
        )
        assert verdict.pbo_score is not None and verdict.pbo_score < 0.5
        assert verdict.passing is True
        assert verdict.admissible is True
        assert verdict.data_source.startswith("csv:")

    def test_provenance_override_revokes_admissibility(self):
        # Take a passing real-data run and re-judge it as if the data were
        # synthetic — admissibility must flip off even though passing stays on.
        # 2-entry variant set per test_real_data_passing_strategy_is_admissible.
        curve = _make_high_sharpe_metrics(data_source="csv:spy_ohlcv_2004_2026.csv").equity_curve
        metrics = _make_high_sharpe_metrics(data_source="csv:spy_ohlcv_2004_2026.csv")
        variants = _two_variant_set(curve, data_source="csv:spy.csv")
        as_synthetic = apply_rigor_gate(
            metrics, variants_metrics=variants, data_source="synthetic", spec=_audited_spec()
        )
        assert as_synthetic.passing is True
        assert as_synthetic.admissible is False

    def test_admissibility_requires_passing(self):
        # Real data but a flat (zero-return) curve → not passing → not admissible.
        flat = [100_000.0] * 600
        metrics = BacktestMetrics(
            sharpe_ratio=0.0,
            sortino_ratio=0.0,
            max_drawdown=0.0,
            cagr=0.0,
            calmar_ratio=0.0,
            win_rate=0.0,
            total_trades=0,
            avg_holding_period_days=0.0,
            equity_curve=flat,
            monthly_returns=[],
            backtest_start=None,
            backtest_end=None,
            data_source="csv:real.csv",
        )
        verdict = apply_rigor_gate(metrics)
        assert verdict.passing is False
        assert verdict.admissible is False


def _metrics_from_curve(curve: list[float], data_source: str = "csv:test.csv") -> BacktestMetrics:
    return BacktestMetrics(
        sharpe_ratio=1.0,
        sortino_ratio=1.0,
        max_drawdown=0.2,
        cagr=0.1,
        calmar_ratio=0.5,
        win_rate=0.55,
        total_trades=50,
        avg_holding_period_days=10.0,
        equity_curve=curve,
        monthly_returns=[],
        backtest_start=None,
        backtest_end=None,
        # See _make_high_sharpe_metrics: stands in for a real cerebro run whose
        # broker cheat-on-close/open check ran and passed.
        broker_cheat_check_passed=True,
        data_source=data_source,
    )


class TestFusionGateEnforcesOosSharpe:
    """Regression for the audit finding that the fusion gate computed the OOS
    Sharpe but never enforced it in the `passing` condition."""

    def test_negative_oos_fails_even_when_dsr_passes(self):
        # In-sample (first half): strong, low-vol uptrend → very high full-sample
        # DSR (p ≈ 1). Out-of-sample (second half): noisy but net-negative drift
        # → OOS Sharpe < 0. Pre-fix this passed on DSR alone; it must now fail.
        curve = [100_000.0]
        for _ in range(400):
            curve.append(curve[-1] * 1.005)  # IS: steady +0.5%/bar
        for i in range(400):
            curve.append(curve[-1] * (0.9990 if i % 2 == 0 else 0.9996))  # OOS: net down

        verdict = apply_rigor_gate(_metrics_from_curve(curve))
        assert verdict.dsr_p_value is not None and verdict.dsr_p_value >= 0.95, (
            "test setup invalid: IS drift should make DSR pass"
        )
        assert verdict.oos_sharpe is not None and verdict.oos_sharpe <= 0.0
        assert verdict.passing is False  # OOS gate is the deciding factor

    def test_positive_oos_can_pass(self):
        # Steady uptrend across the whole window → OOS Sharpe > 0 and DSR passes.
        # 2-entry variant set so CSCV PBO is computed (audit 06-14, Q4:
        # pbo_score=None now fails closed) — see _two_variant_set.
        curve = [100_000.0]
        for i in range(800):
            curve.append(curve[-1] * (1.003 if i % 2 == 0 else 1.001))
        verdict = apply_rigor_gate(
            _metrics_from_curve(curve), variants_metrics=_two_variant_set(curve), spec=_audited_spec()
        )
        assert verdict.pbo_score is not None and verdict.pbo_score < 0.5
        assert verdict.oos_sharpe is not None and verdict.oos_sharpe > 0.0
        assert verdict.passing is True


class TestFusionGateEnforcesIsOosCliff:
    """Regression for the audit finding that the fusion gate enforced only the
    absolute OOS floor (OOS > 0) and omitted the in-/out-of-sample cliff
    (OOS/IS >= 0.5) that the curated RigorGateResult.passes_all enforces. An
    overfit strategy with a huge in-sample Sharpe but a collapsed (yet still
    positive) OOS Sharpe used to pass the fusion gate while failing the curated
    one."""

    @staticmethod
    def _overfit_curve() -> list[float]:
        # IS slice (first 70% = 560 bars): very strong, low-vol uptrend → huge IS
        # Sharpe and a high full-sample DSR. OOS slice (last 30% = 240 bars):
        # weakly positive, higher relative vol → OOS Sharpe > 0 (clears the floor)
        # but OOS/IS << 0.5 (fails the cliff).
        curve = [100_000.0]
        for i in range(560):
            curve.append(curve[-1] * (1.010 if i % 2 == 0 else 1.006))  # IS: ~+0.8%/bar
        for i in range(240):
            curve.append(curve[-1] * (1.004 if i % 2 == 0 else 0.998))  # OOS: weakly +, noisy
        return curve

    def test_overfit_is_high_oos_collapsed_fails_on_cliff(self):
        verdict = apply_rigor_gate(_metrics_from_curve(self._overfit_curve()))
        # Test-setup invariants: DSR passes, OOS clears the absolute floor, and IS
        # Sharpe is strongly positive — so ONLY the cliff can be the deciding gate.
        assert verdict.dsr_p_value is not None and verdict.dsr_p_value >= 0.95, "setup: DSR should pass"
        assert verdict.oos_sharpe is not None and verdict.oos_sharpe > 0.0, "setup: OOS must clear the floor"
        assert verdict.in_sample_sharpe is not None and verdict.in_sample_sharpe > 0.0, "setup: IS Sharpe positive"
        assert verdict.oos_sharpe / verdict.in_sample_sharpe < 0.5, "setup: ratio must trip the cliff"
        assert verdict.passing is False, "cliff must reject an overfit strategy with a collapsed OOS Sharpe"

    def test_in_sample_sharpe_surfaced_on_verdict(self):
        verdict = apply_rigor_gate(_metrics_from_curve(self._overfit_curve()))
        assert verdict.in_sample_sharpe is not None


class TestFusionGateUsesRealTrialCount:
    """Regression for the audit finding that num_trials was hardcoded to 10
    regardless of the actual variant-selection set size."""

    def test_num_trials_tracks_variant_count(self):
        base = [100_000.0]
        for i in range(800):
            base.append(base[-1] * (1.003 if i % 2 == 0 else 1.001))
        base_metrics = _metrics_from_curve(base)

        # 30 correlated variants → trial count must reflect 30, not the default 10.
        variants = {f"v{i}": _metrics_from_curve(base) for i in range(30)}
        verdict = apply_rigor_gate(base_metrics, num_trials=10, variants_metrics=variants)
        assert verdict.num_trials == 30

    def test_falls_back_to_passed_count_without_variants(self):
        base = [100_000.0]
        for i in range(800):
            base.append(base[-1] * (1.003 if i % 2 == 0 else 1.001))
        verdict = apply_rigor_gate(_metrics_from_curve(base), num_trials=7)
        assert verdict.num_trials == 7

    def test_larger_passed_num_trials_is_not_overridden_by_a_small_variant_grid(self):
        """#820: a small parameter-variant grid must not silently undercut a
        bigger, correct caller-supplied num_trials (e.g. the society count,
        library_size + selection_pool_size, threaded from the debate C-rigor path).
        Before this fix, any >=2-entry variants_metrics unconditionally replaced
        num_trials, so a 3-variant grid could under-deflate a strategy that
        actually needed a much larger society-wide trial count."""
        base = [100_000.0]
        for i in range(800):
            base.append(base[-1] * (1.003 if i % 2 == 0 else 1.001))
        base_metrics = _metrics_from_curve(base)

        # Only 3 variants (a small parameter sweep), but num_trials=15 reflects a
        # bigger selection set (e.g. library_size=10 + selection_pool_size=5).
        variants = {f"v{i}": _metrics_from_curve(base) for i in range(3)}
        verdict = apply_rigor_gate(base_metrics, num_trials=15, variants_metrics=variants)
        assert verdict.num_trials == 15

    def test_smaller_passed_num_trials_still_yields_to_a_larger_variant_grid(self):
        """The max() composition is symmetric: a genuinely larger variant grid
        still wins over a smaller passed-in num_trials, preserving the original
        under-deflation fix this class's first test pins."""
        base = [100_000.0]
        for i in range(800):
            base.append(base[-1] * (1.003 if i % 2 == 0 else 1.001))
        base_metrics = _metrics_from_curve(base)

        variants = {f"v{i}": _metrics_from_curve(base) for i in range(30)}
        verdict = apply_rigor_gate(base_metrics, num_trials=10, variants_metrics=variants)
        assert verdict.num_trials == 30


# ── A8 / A4: the persisted DSL row must describe the run that happened ──


class TestBacktestWindowIsReal:
    """`backtest_start`/`backtest_end` came from two dead sentinel conditions.

    In ``run_dsl_backtest`` the sentinel keyed on ``data_feed is None``, but
    ``data_feed`` is unconditionally assigned a few lines above, so it was never
    None and EVERY DSL row persisted a null window — un-auditable. In
    ``_run_variant_backtest`` the sentinel keyed on ``data_csv_path is None``,
    which IS reachable, so a run over a real feed factory persisted a fabricated
    2004-01-02 -> 2026-04-30 window instead.

    The dates now come from the bars backtrader actually iterated, so they can be
    checked against the feed. The fixture spans 2004-01-02..2026-02-06 — note the
    old sentinel's end date (2026-04-30) was not even the file's last bar.
    """

    _TRUE_FIRST_BAR = date(2004, 1, 2)
    _TRUE_LAST_BAR = date(2026, 2, 6)
    _FABRICATED_END = date(2026, 4, 30)

    def _factory(self):
        from archimedes.services._fusion_helpers import _csv_data_feed

        return lambda: _csv_data_feed(_SPY_FIXTURE)

    def test_single_feed_run_reports_the_real_window(self):
        spec = validate_strategy_spec(FABER_2007_SPEC)
        metrics = run_dsl_backtest(spec, data_csv_path=_SPY_FIXTURE)

        # Previously None on this path, for every DSL row ever written.
        assert metrics.backtest_start == self._TRUE_FIRST_BAR
        assert metrics.backtest_end == self._TRUE_LAST_BAR

    def test_variant_run_over_a_real_feed_does_not_fabricate_a_window(self):
        """The worse half of the bug: real data, invented dates."""
        from archimedes.services.dsl_to_backtrader import interpret_spec

        spec = validate_strategy_spec(FABER_2007_SPEC)
        metrics = _run_variant_backtest(
            interpret_spec(spec),
            data_feed_factory=self._factory(),
            data_source_label="csv:spy_ohlcv_2004_2026.csv",
        )

        assert metrics.backtest_end != self._FABRICATED_END
        assert metrics.backtest_start == self._TRUE_FIRST_BAR
        assert metrics.backtest_end == self._TRUE_LAST_BAR


class TestSleeveConstructionIsLabelled:
    """A8. ``run_dsl_backtest_portfolio`` runs the same single-asset spec once per
    asset on ``initial_cash/N`` and sums the sleeves — there is no
    cross-sectional allocation and no rebalance between them. The multi-feed
    interpreter that would fix that is out of scope; labelling the row is what
    turns a silent lie into a disclosed limitation."""

    def _factory(self):
        from archimedes.services._fusion_helpers import _csv_data_feed

        return lambda: _csv_data_feed(_SPY_FIXTURE)

    def test_single_feed_rows_are_labelled_single_asset(self):
        spec = validate_strategy_spec(FABER_2007_SPEC)
        metrics = run_dsl_backtest(spec, data_csv_path=_SPY_FIXTURE)
        assert metrics.backtest_engine == ENGINE_SINGLE_FEED == "dsl-fusion"
        assert metrics.portfolio_construction == CONSTRUCTION_SINGLE_ASSET == "single_asset"

    def test_sleeve_rows_are_labelled_as_independent_sleeves(self):
        spec = validate_strategy_spec(FABER_2007_SPEC)
        metrics = run_dsl_backtest_portfolio(
            spec,
            {"SPY": self._factory(), "SPY2": self._factory()},
            label="csv:spy_ohlcv_2004_2026.csv",
        )
        # The label must distinguish this from a genuine single-feed run, so a
        # reader of the passport can tell the two apart.
        assert metrics.backtest_engine == ENGINE_SLEEVES == "dsl-fusion-sleeves"
        assert metrics.backtest_engine != ENGINE_SINGLE_FEED
        assert metrics.portfolio_construction == CONSTRUCTION_SLEEVES == "n_independent_sleeves_equal_weight"

    def test_the_label_reaches_the_canonical_rigor_verdict_dict(self):
        """The label is useless if it dies at the debate/pipeline boundary — that
        boundary is exactly where `backtest_engine` used to be hardcoded."""
        from archimedes.agents.debate_engine import _rigor_verdict_dict

        spec = validate_strategy_spec(FABER_2007_SPEC)
        metrics = run_dsl_backtest_portfolio(
            spec,
            {"SPY": self._factory(), "SPY2": self._factory()},
            label="csv:spy_ohlcv_2004_2026.csv",
        )
        ev = evaluate_fusion_spec(FABER_2007_SPEC)
        ev = ev.__class__(spec=ev.spec, backtest=metrics, rigor=ev.rigor)

        verdict = _rigor_verdict_dict(ev)
        assert verdict["backtest_engine"] == "dsl-fusion-sleeves"
        assert verdict["portfolio_construction"] == "n_independent_sleeves_equal_weight"
