"""Tests for rigor_evaluator — DSR, PBO, and OOS Sharpe.

Pinned to the three sanity-check cases from the spec:

  docs/specs/selection-bias-corrections-spec.md
  § "Numerical sanity-check examples (for unit test seed)"

Note: ``gamma_4`` below is **raw (Pearson) kurtosis** (normal = 3.0), matching
Bailey-LdP (2014) eq. 8 directly. Previous versions of this table used Fisher
excess kurtosis with the raw-kurtosis coefficient, biasing the DSR denominator.

| Case | SR_ann | T    | skew  | raw_kurt | N    | SR_zero   | z      | dsr_p_value |
|------|--------|------|-------|----------|------|-----------|--------|-------------|
| A    | 1.8    | 2520 | -0.4  | 6.2      | 10   | 0.0314    | 3.994  | ~1.0000     |
| B    | 0.9    | 1260 | -0.2  | 5.0      | 20   | 0.0536    | 0.110  | ~0.5439     |
| C    | 0.3    |  504 |  0.0  | 3.0      | 1000 | 0.1451    | -2.831 | ~0.0023     |

No network, no on-chain dependencies. TestLoadDailyReturnsStore (#774) uses a
tmp, isolated SQLite session — never a real network DB.
"""

from __future__ import annotations

import ast
import math

import numpy as np
import pytest
from archimedes.services._rigor_helpers import (
    _RF_DAILY,
    _dsr_from_stats,
    _sharpe_per_col,
    benjamini_hochberg_fdr,
    compute_average_pairwise_correlation,
    compute_cpcv_oos_sharpe,
    compute_dsr,
    compute_kelly_fraction,
    compute_oos_sharpe,
    compute_pbo,
    compute_sharpe_ci,
)
from archimedes.services.rigor_evaluator import (
    MIN_LIBRARY_N_FOR_PBO_GATING,
    RigorGateResult,
    _get_func_name,
    align_returns_store,
    compute_board_level_fdr,
    compute_library_pbo,
    compute_library_pbo_rf_convention,
    is_oos_zero_variance_series,
    is_zero_variance_series,
    load_daily_returns_store,
    look_ahead_audit,
    run_rigor_gate,
)
from archimedes.services.rigor_profiles import DSR_P_BADGE_MIN

_ANNUALIZATION = 252


# ─── DSR formula — pinned to spec sanity-check cases ─────────────────


@pytest.mark.parametrize(
    "SR_ann, T, skew, raw_kurt, N, expected_p, p_tol",
    [
        # Case A — strong: long, smooth backtest, small library → DSR clears gate
        (1.8, 2520, -0.4, 6.2, 10, 1.0000, 0.001),
        # Case B — borderline: credibly positive but below the badge bar
        (0.9, 1260, -0.2, 5.0, 20, 0.5439, 0.005),
        # Case C — failure: weak Sharpe from large selection → gate must reject
        (0.3, 504, 0.0, 3.0, 1000, 0.0023, 0.001),
    ],
)
def test_dsr_formula_spec_cases(SR_ann, T, skew, raw_kurt, N, expected_p, p_tol):
    """_dsr_from_stats reproduces the spec's reference values within tolerance."""
    SR_hat = SR_ann / math.sqrt(_ANNUALIZATION)
    dsr, p_val = _dsr_from_stats(SR_hat, T, skew, raw_kurt, N)

    assert dsr is not None, "DSR should not be None for valid inputs"
    assert p_val is not None, "p_value should not be None for valid inputs"
    assert abs(p_val - expected_p) <= p_tol, (
        f"p_value {p_val:.6f} differs from expected {expected_p} by more than {p_tol}"
    )


@pytest.mark.parametrize(
    ("label", "SR_ann", "T", "skew", "raw_kurt", "N", "expectation"),
    [
        # The spec's three canonical cases, graded against the SHIPPED
        # thresholds imported from rigor_profiles — NOT bare 0.95/0.05
        # literals, which were recalibrated away in #901. This is the one
        # place the frozen spec's reference cases meet the live badge bar,
        # so a silent threshold drift breaks here first.
        ("case_a_clears_badge_bar", 1.8, 2520, -0.4, 6.2, 10, "clears_badge"),
        ("case_b_below_badge_bar", 0.9, 1260, -0.2, 5.0, 20, "below_badge"),
        ("case_c_fails_even_the_floor", 0.3, 504, 0.0, 3.0, 1000, "below_floor"),
    ],
)
def test_dsr_spec_cases_grade_against_shipped_thresholds(label, SR_ann, T, skew, raw_kurt, N, expectation):
    from archimedes.services.rigor_profiles import DSR_P_FLOOR, get_profile

    badge_bar = get_profile(1).dsr_p_min
    SR_hat = SR_ann / math.sqrt(_ANNUALIZATION)
    _, p_val = _dsr_from_stats(SR_hat, T, skew, raw_kurt, N)
    assert p_val is not None
    if expectation == "clears_badge":
        assert p_val >= badge_bar, f"{label}: {p_val:.4f} should clear the badge bar {badge_bar}"
    elif expectation == "below_badge":
        assert p_val < badge_bar, f"{label}: {p_val:.4f} should be below the badge bar {badge_bar}"
    else:
        assert p_val < DSR_P_FLOOR, f"{label}: {p_val:.6f} should fail even the always-on floor {DSR_P_FLOOR}"


def test_dsr_no_correction_when_n_equals_1():
    """With N=1, E_max_N = 0 and DSR equals the raw annualized Sharpe."""
    SR_hat = 1.0 / math.sqrt(_ANNUALIZATION)
    T = 252
    dsr, _ = _dsr_from_stats(SR_hat, T, 0.0, 3.0, 1)
    assert dsr is not None
    # SR_zero = 0 when N=1, so deflated SR = SR_hat * sqrt(252) ≈ 1.0
    assert abs(dsr - 1.0) < 0.01, f"N=1 DSR should approximate raw annualized SR, got {dsr:.4f}"


def test_dsr_returns_none_for_short_series():
    assert compute_dsr([0.01, 0.02, 0.01], num_trials=5) == (None, None)


def test_dsr_returns_none_for_zero_vol():
    returns = [0.001] * 100  # constant returns → zero std
    dsr, p_val = compute_dsr(returns, num_trials=5)
    assert dsr is None
    assert p_val is None


def test_dsr_higher_n_lowers_p_value():
    """More trials in selection → more conservative (lower p_value)."""
    SR_hat = 0.8 / math.sqrt(_ANNUALIZATION)
    T = 1000
    _, p_low_n = _dsr_from_stats(SR_hat, T, 0.0, 3.0, 5)
    _, p_high_n = _dsr_from_stats(SR_hat, T, 0.0, 3.0, 500)
    assert p_low_n is not None and p_high_n is not None
    assert p_low_n > p_high_n, "Higher N must reduce the DSR p-value"


# ─── PBO ─────────────────────────────────────────────────────────────


def test_pbo_single_strategy_returns_zero():
    """PBO is undefined for N=1; we return 0 (no overfitting detectable)."""
    result = compute_pbo({"strat_a": [0.001] * 256})
    assert result == {"strat_a": 0.0}


def test_pbo_dominant_strategy_has_low_score():
    """A strategy that dominates OOS on every split should yield low PBO."""
    rng = np.random.default_rng(42)
    T = 512

    # strat_a: strong positive drift; strat_b: weak / noisy
    returns_a = rng.normal(0.001, 0.01, T).tolist()
    returns_b = rng.normal(0.0, 0.02, T).tolist()

    result = compute_pbo({"a": returns_a, "b": returns_b}, s_partitions=8)
    assert "a" in result and "b" in result
    # Same PBO for all strategies in the library (library-level metric)
    assert result["a"] == result["b"]
    # Dominant strategy → low PBO
    assert result["a"] < 0.5, f"Dominant strategy should have PBO < 0.5, got {result['a']}"


def test_pbo_noise_only_strategies_have_high_score():
    """When all strategies are pure noise, PBO should be near 0.5."""
    rng = np.random.default_rng(7)
    T = 512
    n_strats = 8
    matrix = {f"s{i}": rng.normal(0.0, 0.01, T).tolist() for i in range(n_strats)}

    result = compute_pbo(matrix, s_partitions=8)
    pbo = next(iter(result.values()))
    # All noise: PBO should cluster around 0.5
    assert 0.3 <= pbo <= 0.8, f"Noise strategies should have PBO ≈ 0.5, got {pbo}"


def test_pbo_all_scores_identical():
    """All strategies in a library run get the same PBO score."""
    rng = np.random.default_rng(99)
    T = 256
    matrix = {f"s{i}": rng.normal(0.0001 * i, 0.01, T).tolist() for i in range(4)}

    result = compute_pbo(matrix, s_partitions=4)
    scores = list(result.values())
    assert len(set(scores)) == 1, f"All PBO scores must be identical, got {set(scores)}"


def test_pbo_returns_zero_for_insufficient_data():
    """Too few rows per block → graceful zero, no crash."""
    matrix = {"a": [0.01] * 3, "b": [0.02] * 3}
    result = compute_pbo(matrix, s_partitions=16)
    assert all(v == 0.0 for v in result.values())


# ─── OOS Sharpe ──────────────────────────────────────────────────────


def test_oos_sharpe_returns_none_for_short_series():
    assert compute_oos_sharpe([0.001] * 5) is None


def test_oos_sharpe_positive_for_consistently_positive_returns():
    rng = np.random.default_rng(1)
    # Strong positive drift + small noise → OOS Sharpe should be positive
    returns = (rng.normal(0.002, 0.005, 200)).tolist()
    oos = compute_oos_sharpe(returns, train_fraction=0.70)
    assert oos is not None
    assert oos > 0.0


def test_oos_sharpe_negative_for_consistently_negative_returns():
    rng = np.random.default_rng(2)
    # Strong negative drift + small noise → OOS Sharpe should be negative
    returns = (rng.normal(-0.002, 0.005, 200)).tolist()
    oos = compute_oos_sharpe(returns, train_fraction=0.70)
    assert oos is not None
    assert oos < 0.0


def test_oos_sharpe_respects_train_fraction():
    """OOS Sharpe should only use the last (1 - train_fraction) of the series."""
    rng = np.random.default_rng(3)
    n = 300
    # IS slice: strong negative drift; OOS slice: strong positive drift
    is_part = rng.normal(-0.003, 0.005, int(n * 0.70)).tolist()
    oos_part = rng.normal(0.003, 0.005, int(n * 0.30)).tolist()
    returns = is_part + oos_part
    oos = compute_oos_sharpe(returns, train_fraction=0.70)
    assert oos is not None
    assert oos > 0.0, "OOS slice has positive drift; Sharpe should be positive"


# ─── Kelly Criterion ─────────────────────────────────────────────────


def test_kelly_returns_none_for_short_series():
    assert compute_kelly_fraction([0.001] * 3) is None


def test_kelly_returns_none_for_zero_vol():
    returns = [0.001] * 100  # constant → zero std
    assert compute_kelly_fraction(returns) is None


def test_kelly_returns_zero_for_negative_excess_return():
    """Strategy with negative excess return → Kelly says don't bet."""
    rng = np.random.default_rng(10)
    # Mean ≈ -0.001/day → annualized ≈ -25% → well below 5% rf
    returns = rng.normal(-0.001, 0.01, 252).tolist()
    f = compute_kelly_fraction(returns, rf_annual=0.05)
    assert f is not None
    assert f == 0.0, "Negative excess return → Kelly fraction should be 0"


def test_kelly_positive_for_strong_positive_returns():
    """A high-drift strategy should get a positive half-Kelly allocation."""
    rng = np.random.default_rng(11)
    # Mean ≈ 0.002/day → annualized ≈ 50%, vol ≈ 1% daily ≈ 16% ann
    # Full Kelly ≈ (0.50 - 0.05) / 0.16² ≈ 17.6  (capped to 1.0 after fractional)
    returns = rng.normal(0.002, 0.01, 500).tolist()
    f = compute_kelly_fraction(returns, rf_annual=0.05, fractional=0.5)
    assert f is not None
    assert f > 0.0, "High-drift strategy should have positive Kelly fraction"
    assert f <= 1.0, "Kelly fraction must not exceed 1.0 (no leverage)"


def test_kelly_half_kelly_is_smaller_than_full():
    """half-Kelly must be strictly less than full-Kelly when neither is capped."""
    rng = np.random.default_rng(12)
    # High vol (5% daily) keeps full-Kelly below 1.0 so neither value is capped.
    # μ_ann ≈ 0.5, σ_ann² ≈ 0.63 → f_full ≈ 0.72; f_half ≈ 0.36
    returns = rng.normal(0.002, 0.05, 500).tolist()
    f_half = compute_kelly_fraction(returns, rf_annual=0.05, fractional=0.5)
    f_full = compute_kelly_fraction(returns, rf_annual=0.05, fractional=1.0)
    assert f_half is not None and f_full is not None
    assert f_full <= 1.0, "full-Kelly should not be capped for this series"
    assert f_half < f_full, "half-Kelly must be smaller than full-Kelly"
    assert f_half > 0.0, "half-Kelly must be positive for this series"


def test_kelly_is_clipped_to_unit_interval():
    """Extremely high-drift series → fractional Kelly is clipped to 1.0."""
    # very high mean, very low vol → f* >> 1 before clipping
    returns = [0.01] * 499 + [0.009]  # near-constant high drift
    f = compute_kelly_fraction(returns, rf_annual=0.05, fractional=1.0)
    assert f is not None
    assert f <= 1.0, "Kelly fraction must be clipped to ≤ 1.0"
    assert f >= 0.0


# ─── Sharpe CI (Lo 2002) ─────────────────────────────────────────────


def test_sharpe_ci_symmetric_around_point_estimate():
    """CI must be symmetric: point estimate is the midpoint of (lower, upper)."""
    sr = 1.0
    lower, upper = compute_sharpe_ci(sr, n_obs_daily=252, confidence=0.95)
    mid = (lower + upper) / 2
    assert abs(mid - sr) < 1e-9, f"CI midpoint {mid:.6f} must equal SR {sr}"


def test_sharpe_ci_returns_plain_python_float():
    """Regression: the CI must be plain ``float``, never ``np.float64``.

    ``norm.ppf`` returns a numpy scalar, so without an explicit cast the tuple
    leaks ``np.float64`` into ``sharpe_ci_lower``/``sharpe_ci_upper`` on the
    strategy passport. psycopg2 then renders it as ``np.float64(-0.33)`` and
    parses ``np`` as a schema, raising ``InvalidSchemaName: schema "np" does
    not exist`` and rolling back the passport DB sync. ``isinstance(x, float)``
    does NOT catch this (np.float64 subclasses float) — assert the exact type.
    """
    lower, upper = compute_sharpe_ci(1.0, n_obs_daily=252, confidence=0.95)
    assert type(lower) is float, f"lower is {type(lower).__name__}, must be plain float"
    assert type(upper) is float, f"upper is {type(upper).__name__}, must be plain float"


def test_sharpe_ci_wider_with_fewer_obs():
    """Fewer observations → wider confidence interval."""
    sr = 0.8
    lo_wide, hi_wide = compute_sharpe_ci(sr, n_obs_daily=252)
    lo_narrow, hi_narrow = compute_sharpe_ci(sr, n_obs_daily=2520)
    assert (hi_wide - lo_wide) > (hi_narrow - lo_narrow), "CI with 252 obs must be wider than CI with 2520 obs"


def test_sharpe_ci_wider_with_higher_confidence():
    """Higher confidence level → wider interval."""
    sr = 0.7
    lo_95, hi_95 = compute_sharpe_ci(sr, n_obs_daily=500, confidence=0.95)
    lo_99, hi_99 = compute_sharpe_ci(sr, n_obs_daily=500, confidence=0.99)
    assert (hi_99 - lo_99) > (hi_95 - lo_95), "99% CI must be wider than 95% CI"


def test_sharpe_ci_lo2002_formula_pinned():
    """Pin Lo (2002) SE against a manually computed reference value.

    SR_annual = 1.0, n = 252, confidence = 0.95.
    SR_daily = 1/sqrt(252)
    SE = sqrt((1 + 0.5*(1/252)) * 252 / 252) = sqrt(1 + 0.5/252) ≈ 1.001
    z_0.975 ≈ 1.96 → half-width ≈ 1.96 * 1.001 ≈ 1.962
    """
    import math

    sr = 1.0
    n = 252
    sr_daily = sr / math.sqrt(252)
    se = math.sqrt((1 + 0.5 * sr_daily**2) * 252 / n)
    from scipy.stats import norm

    z = norm.ppf(0.975)
    expected_lower = sr - z * se
    expected_upper = sr + z * se

    lower, upper = compute_sharpe_ci(sr, n_obs_daily=n, confidence=0.95)
    assert abs(lower - expected_lower) < 1e-9
    assert abs(upper - expected_upper) < 1e-9


def test_sharpe_ci_rejects_invalid_n():
    """n_obs_daily ≤ 0 must raise ValueError."""
    import pytest

    with pytest.raises(ValueError, match="n_obs_daily"):
        compute_sharpe_ci(1.0, n_obs_daily=0)
    with pytest.raises(ValueError, match="n_obs_daily"):
        compute_sharpe_ci(1.0, n_obs_daily=-5)


def test_sharpe_ci_rejects_invalid_confidence():
    """Confidence outside (0, 1) must raise ValueError."""
    import pytest

    with pytest.raises(ValueError, match="confidence"):
        compute_sharpe_ci(1.0, n_obs_daily=252, confidence=0.0)
    with pytest.raises(ValueError, match="confidence"):
        compute_sharpe_ci(1.0, n_obs_daily=252, confidence=1.0)
    with pytest.raises(ValueError, match="confidence"):
        compute_sharpe_ci(1.0, n_obs_daily=252, confidence=1.5)


# ─── Look-ahead audit (migrated from selection_bias.py) ──────────────


class TestLookAheadAudit:
    def test_clean_code(self) -> None:
        # Only index [0] (current bar) — no negative indices, no positive indices.
        code = """
class MyStrategy(bt.Strategy):
    def next(self):
        if self.data.close[0] > 100:
            self.buy()
"""
        passed, warnings = look_ahead_audit(code)
        assert passed
        assert len(warnings) == 0

    def test_negative_index_on_backtrader_data_no_longer_warns(self) -> None:
        # #868: self.data.close[-1] is backtrader's standard, CORRECT "1 bar ago"
        # convention — NOT a look-ahead violation. Previously this false-flagged
        # (see the old test_negative_index_emits_warning, now superseded), which
        # meant every strategy using idiomatic backtrader indexing hard-failed
        # look_ahead_audit (passes_all requires zero warnings).
        code = """
class MyStrategy(bt.Strategy):
    def next(self):
        if self.data.close[0] > self.data.close[-1]:
            self.buy()
"""
        passed, warnings = look_ahead_audit(code)
        assert passed
        assert len(warnings) == 0

    def test_positive_index(self) -> None:
        code = """
class MyStrategy(bt.Strategy):
    def next(self):
        future_price = self.data.close[1]
        if future_price > self.data.close[0]:
            self.buy()
"""
        passed, warnings = look_ahead_audit(code)
        assert not passed
        assert any("positive" in w.lower() for w in warnings)

    def test_suspicious_function(self) -> None:
        code = """
class MyStrategy(bt.Strategy):
    def next(self):
        predicted = self.predict(self.data.close[0])
        if predicted > 100:
            self.buy()
"""
        passed, warnings = look_ahead_audit(code)
        assert not passed
        assert any("predict" in w.lower() for w in warnings)

    def test_invalid_syntax(self) -> None:
        passed, warnings = look_ahead_audit("def broken(:")
        assert not passed
        assert any("parse" in w.lower() for w in warnings)


# ─── Look-ahead audit: backtrader bars-ago vs. true look-ahead (#868) ──


class TestLookAheadAuditBacktraderVsLookAhead:
    """#868 fix 2: look_ahead_audit previously flagged ANY negative-index
    subscript as a violation, but backtrader's Lines/LineBuffer objects
    (self.data.<line>[-i], self.datas[k].<line>[-i], a self.datas loop-variable
    alias, or a self-rooted custom bt.Indicator) use negative indices by
    convention to mean "N bars ago" — backward-looking, not a violation.
    Because passes_all requires ZERO warnings, this single false positive
    alone failed an otherwise-clean strategy (concrete failing example:
    analytics-engine/strategies/moreira_muir_2017_volatility_managed.py:99-100).

    These tests cover every negative-index SHAPE actually present in
    analytics-engine/strategies/*.py (verified via a full-corpus grep before
    writing the fix) plus the genuine-look-ahead cases that must still fail.
    """

    # ── Backtrader bars-ago access: must PASS (no warnings) ──────────────

    def test_moreira_muir_realized_vol_pattern_passes(self) -> None:
        """The exact concrete failing example from the issue:
        moreira_muir_2017_volatility_managed.py:99-100 — self.data.close[-i-1]
        and self.data.close[-i] inside a rolling-volatility loop."""
        code = """
class MyStrategy(bt.Strategy):
    def _realized_vol_annual(self):
        returns = []
        for i in range(1, 22):
            prev = float(self.data.close[-i - 1])
            curr = float(self.data.close[-i])
            if prev > 0:
                returns.append((curr / prev) - 1.0)
        return returns
"""
        passed, warnings = look_ahead_audit(code)
        assert passed, f"unexpected warnings on the moreira-muir bars-ago pattern: {warnings}"
        assert len(warnings) == 0

    def test_simple_bars_ago_variable_index(self) -> None:
        # self.data.close[-i] — the exact pattern named in the issue.
        code = "def f(self, i):\n    return self.data.close[-i]\n"
        passed, warnings = look_ahead_audit(code)
        assert passed
        assert warnings == []

    def test_bars_ago_constant_index(self) -> None:
        # self.data.close[-1] — already covered by
        # test_negative_index_on_backtrader_data_no_longer_warns above; repeated
        # here for locality with the rest of this class's coverage.
        code = "def f(self):\n    return self.data.close[-1]\n"
        passed, _warnings = look_ahead_audit(code)
        assert passed

    def test_bars_ago_parenthesized_binop_index(self) -> None:
        # data.close[-(i + 1)] — frazzini_pedersen_2014_bab.py's exact shape
        # (UnaryOp wrapping a BinOp, not a bare Name/Constant operand).
        code = "def f(data, i):\n    return data.close[-(i + 1)]\n"
        passed, warnings = look_ahead_audit(code)
        assert passed
        assert warnings == []

    def test_bars_ago_binop_subtraction_index(self) -> None:
        # self.data.close[-i - 1] (outer node is a BinOp, not a UnaryOp) — the
        # AST shape the OLD code already silently ignored; confirm the new
        # code still does not warn on it either.
        code = "def f(self, i):\n    return self.data.close[-i - 1]\n"
        passed, warnings = look_ahead_audit(code)
        assert passed
        assert warnings == []

    def test_secondary_data_feed_negative_index_is_not_flagged_as_negative(self) -> None:
        """self.datas[1].close[-i] (gatev_2006_pairs_distance.py's pairs-trading
        shape) — neither the NEGATIVE bars-ago index [-i] NOR the POSITIVE
        feed-selector index [1] on self.datas should be flagged.

        Originally (pre-#881) this snippet still failed look_ahead_audit
        overall, because the POSITIVE-index branch (unconditional, untouched
        by #868) separately and incorrectly flagged the `self.datas[1]`
        data-FEED-selector subscript itself ("positive data index [1] may
        reference future bars") — conflating "index 1 selects the 2nd data
        feed" with "index 1 is one bar in the future". #881 fixed that: see
        `_is_datas_feed_selection` and the positive-index branch in
        `look_ahead_audit`.
        """
        code = "def f(self, i):\n    return self.datas[1].close[-i]\n"
        passed, warnings = look_ahead_audit(code)
        assert not any("negative index" in w.lower() for w in warnings), (
            f"the bars-ago [-i] access must not be flagged as a negative-index violation: {warnings}"
        )
        assert not any("positive" in w.lower() for w in warnings), (
            f"self.datas[1] is feed selection, not a look-ahead violation (#881): {warnings}"
        )
        assert passed
        assert warnings == []

    def test_loop_variable_alias_over_datas(self) -> None:
        # d.close[-i] where `d` is a loop variable bound to a self.datas element
        # (avellaneda_lee_2010_pca_statarb.py's exact shape: `for d in
        # self.datas: ... d.close[-i]`). The root is `d`, not `self`/`data` — the
        # fix must key off the backtrader OHLCV line name (`close`), not the
        # root variable's literal spelling, to catch this.
        code = """
class MyStrategy(bt.Strategy):
    def next(self):
        for d in self.datas:
            prev = float(d.close[-2])
            curr = float(d.close[-1])
"""
        passed, warnings = look_ahead_audit(code)
        assert passed, f"loop-variable alias over self.datas incorrectly flagged: {warnings}"
        assert len(warnings) == 0

    def test_self_rooted_custom_indicator(self) -> None:
        # self.highest[-1] (donchian_breakout.py) — a custom bt.Indicator stored
        # on self, not an OHLCV line name. Root == "self" is the fallback signal
        # that whitelists this even though "highest" isn't in the OHLCV name set.
        code = "def f(self):\n    return self.highest[-1]\n"
        passed, warnings = look_ahead_audit(code)
        assert passed
        assert warnings == []

    def test_high_low_open_volume_lines_all_pass(self) -> None:
        for line in ("high", "low", "open", "volume", "openinterest", "datetime"):
            code = f"def f(self, i):\n    return self.data.{line}[-i]\n"
            passed, warnings = look_ahead_audit(code)
            assert passed, f"self.data.{line}[-i] incorrectly flagged: {warnings}"

    # ── Genuine look-ahead: must still FAIL ───────────────────────────────

    def test_plain_list_negative_index_still_warns(self) -> None:
        # spread[-1] where `spread` is a bare local variable (a plain Python
        # list/np.ndarray, e.g. engle_granger_1987_cointegration_pairs.py's
        # spread series) — NOT a backtrader line object. [-1] on a plain
        # sequence means "last element", which is future data on a
        # forward-chronological series if used incorrectly, so this must still
        # be flagged for human review exactly as before.
        code = "def f(spread):\n    return spread[-1]\n"
        passed, warnings = look_ahead_audit(code)
        assert not passed
        assert any("negative index" in w.lower() for w in warnings)

    def test_plain_variable_negative_index_still_warns(self) -> None:
        code = "def f(x):\n    return x[-1]\n"
        passed, warnings = look_ahead_audit(code)
        assert not passed
        assert any("negative index" in w.lower() for w in warnings)

    def test_pandas_iloc_negative_index_still_warns(self) -> None:
        # df.iloc[-1] — pandas last-row access. Structurally similar to a
        # self-rooted backtrader indicator (both are Attribute-chain subscripts)
        # but must NOT be whitelisted: the .iloc accessor is the exact false
        # negative the original docstring worried about ("df.iloc[-1],
        # df['col'][-1]"), so it stays flagged even under a self-rooted chain.
        code = "def f(df):\n    return df.iloc[-1]\n"
        passed, warnings = look_ahead_audit(code)
        assert not passed
        assert any("negative index" in w.lower() for w in warnings)

    def test_self_rooted_pandas_iloc_still_warns(self) -> None:
        # self.df.iloc[-1] — pandas accessor stored on self. The `self` root
        # alone must not whitelist it; the .iloc blocklist must win.
        code = "def f(self):\n    return self.df.iloc[-1]\n"
        passed, warnings = look_ahead_audit(code)
        assert not passed
        assert any("negative index" in w.lower() for w in warnings)

    def test_pandas_bracket_column_negative_index_still_warns(self) -> None:
        # df["col"][-1] — pandas last-row access via bracket column selection.
        code = 'def f(df):\n    return df["col"][-1]\n'
        passed, warnings = look_ahead_audit(code)
        assert not passed
        assert any("negative index" in w.lower() for w in warnings)

    def test_synthetic_true_look_ahead_positive_future_index_still_fails(self) -> None:
        """ACCEPTANCE CRITERION: a synthetic true-look-ahead fixture (positive
        future index) still FAILS after the fix — the bars-ago whitelist must
        not weaken genuine look-ahead detection. Positive indices are suspicious
        on ANY object (backtrader included: bar 0 is "now", any positive offset
        is a future bar), so this behavior is intentionally UNCHANGED by #868."""
        code = """
class MyStrategy(bt.Strategy):
    def next(self):
        future_price = self.data.close[5]
        if future_price > self.data.close[0]:
            self.buy()
"""
        passed, warnings = look_ahead_audit(code)
        assert not passed
        assert any("positive" in w.lower() for w in warnings)

    def test_synthetic_true_look_ahead_on_plain_array_still_fails(self) -> None:
        # A positive future index into a plain array is equally suspicious and
        # was already covered by test_positive_index above for self.data; this
        # confirms the same holds for a non-backtrader object too (the positive-
        # index branch is unconditional — it doesn't consult
        # _is_backtrader_line_access at all).
        code = "def f(prices):\n    return prices[5]\n"
        passed, warnings = look_ahead_audit(code)
        assert not passed
        assert any("positive" in w.lower() for w in warnings)

    def test_mixed_clean_bars_ago_and_genuine_violation_still_fails_overall(self) -> None:
        """A strategy with BOTH legitimate bars-ago access AND one genuine
        look-ahead violation must still fail overall (passes_all requires zero
        warnings) — the fix narrows false positives, it does not turn off
        detection for a strategy that also happens to use clean backtrader
        indexing elsewhere."""
        code = """
class MyStrategy(bt.Strategy):
    def next(self):
        safe = self.data.close[-1]          # legitimate bars-ago — no warning
        future_price = self.data.close[5]   # genuine look-ahead — must warn
        if future_price > safe:
            self.buy()
"""
        passed, warnings = look_ahead_audit(code)
        assert not passed
        assert any("positive" in w.lower() for w in warnings)
        assert not any("negative index" in w.lower() for w in warnings), (
            "the legitimate self.data.close[-1] access must not also warn"
        )


# ─── Look-ahead audit: self.datas[N] feed selection vs. positive look-ahead (#881) ──


class TestLookAheadAuditDatasFeedSelection:
    """#881: look_ahead_audit's positive-index branch previously flagged ANY
    positive constant subscript, including `self.datas[N]` — backtrader's
    convention for selecting which data feed to address in a multi-asset
    strategy (e.g. `self.datas[1]` = the second asset in a pairs trade), not a
    time offset. This false-positived every multi-leg/pairs strategy that
    writes `self.datas[N]` directly (as opposed to via a pre-bound variable).
    """

    # ── self.datas[N] feed selection: must PASS (no warnings) ────────────

    def test_datas_constant_index_one_not_flagged(self) -> None:
        # self.datas[1] — the exact shape named in the issue.
        code = "def f(self):\n    return self.datas[1]\n"
        passed, warnings = look_ahead_audit(code)
        assert passed
        assert warnings == []

    def test_datas_index_zero_and_one_both_pass(self) -> None:
        code = """
class MyStrategy(bt.Strategy):
    def next(self):
        a = self.datas[0]
        b = self.datas[1]
"""
        passed, warnings = look_ahead_audit(code)
        assert passed
        assert warnings == []

    def test_datas_feed_selection_with_close_attribute_current_bar(self) -> None:
        # self.datas[1].close[0] — feed selection (positive index on
        # self.datas) chained with current-bar access (index 0, not positive).
        code = "def f(self):\n    return self.datas[1].close[0]\n"
        passed, warnings = look_ahead_audit(code)
        assert passed
        assert warnings == []

    def test_datas_feed_selection_used_in_getposition_and_order_calls(self) -> None:
        # self.getposition(self.datas[1]) / self.close(data=self.datas[1]) /
        # self.order_target_size(data=self.datas[0], ...) — the exact call
        # shapes used by gatev_2006_pairs_distance.py and siblings.
        code = """
class MyStrategy(bt.Strategy):
    def next(self):
        in_position = bool(self.getposition(self.datas[0]).size) or bool(self.getposition(self.datas[1]).size)
        if in_position:
            self.close(data=self.datas[0])
            self.close(data=self.datas[1])
        else:
            self.order_target_size(data=self.datas[0], target=-1)
            self.order_target_size(data=self.datas[1], target=1)
"""
        passed, warnings = look_ahead_audit(code)
        assert passed, f"self.datas[N] feed-selection calls incorrectly flagged: {warnings}"
        assert warnings == []

    # ── Genuine look-ahead: must still FAIL, even near self.datas ────────

    def test_positive_index_on_close_after_datas_selection_still_warns(self) -> None:
        # self.datas[1].close[1] — the feed-selector subscript ([1] on
        # self.datas) is exempt, but the CHAINED [1] on .close is a genuine
        # positive time-offset (a future bar) and must still be flagged. The
        # exemption is narrowly for the self.datas[N] subscript itself, not
        # "any positive index anywhere in a chain rooted at self.datas".
        code = "def f(self):\n    return self.datas[1].close[1]\n"
        passed, warnings = look_ahead_audit(code)
        assert not passed
        assert any("positive" in w.lower() for w in warnings)

    def test_positive_index_on_other_self_attribute_still_warns(self) -> None:
        # self.highest[1] — a positive index on a self-rooted attribute that
        # is NOT self.datas must still be flagged; the exemption is specific
        # to the literal `self.datas` chain, not "any self-rooted attribute".
        code = "def f(self):\n    return self.highest[1]\n"
        passed, warnings = look_ahead_audit(code)
        assert not passed
        assert any("positive" in w.lower() for w in warnings)

    def test_positive_index_on_plain_array_still_warns(self) -> None:
        # A genuine future-index violation unrelated to self.datas at all
        # (e.g. indexing a returns array at a computed future position) must
        # still fail — the acceptance-criteria synthetic fixture for #881.
        code = "def f(returns):\n    return returns[3]\n"
        passed, warnings = look_ahead_audit(code)
        assert not passed
        assert any("positive" in w.lower() for w in warnings)

    def test_datas_not_rooted_at_self_still_warns(self) -> None:
        # local_datas[1] — a variable merely NAMED "datas" that is not the
        # `self.datas` attribute chain must not be exempted; the check keys
        # off the actual attribute access (self.datas), not the name "datas".
        code = "def f(local_datas):\n    return local_datas[1]\n"
        passed, warnings = look_ahead_audit(code)
        assert not passed
        assert any("positive" in w.lower() for w in warnings)

    def test_non_self_rooted_datas_attribute_still_warns(self) -> None:
        # other.datas[1] — an attribute chain with attr "datas" but whose root
        # is NOT literally `self` must not be exempted; #881's issue text is
        # specific to backtrader's `self.datas[N]` convention.
        code = "def f(other):\n    return other.datas[1]\n"
        passed, warnings = look_ahead_audit(code)
        assert not passed
        assert any("positive" in w.lower() for w in warnings)


# ─── Look-ahead audit: real pairs-strategy files (#881 acceptance) ────


class TestLookAheadAuditRealPairsStrategyFiles:
    """ACCEPTANCE CRITERION (#881, end-to-end, real files): the three
    multi-asset pairs strategies named in the issue now pass look_ahead_audit,
    loaded from disk via the session-scoped ``strategies_dir`` fixture so this
    exercises the same source run_rigor_gate sees on the live path."""

    @pytest.mark.parametrize(
        "filename",
        [
            "gatev_2006_pairs_distance.py",
            "elliott_2005_kalman_pairs.py",
        ],
    )
    def test_pairs_strategy_file_passes_look_ahead_audit(self, strategies_dir, filename) -> None:
        path = strategies_dir / filename
        assert path.is_file(), f"expected strategy file at {path}"
        code = path.read_text()

        passed, warnings = look_ahead_audit(code)
        assert passed, f"{filename} still fails look-ahead audit: {warnings}"
        assert warnings == []

    def test_cointegration_pairs_file_has_only_unrelated_preexisting_warnings(self, strategies_dir) -> None:
        """engle_granger_1987_cointegration_pairs.py: the self.datas[N]
        false positives (#881's scope) are gone, but the file still has TWO
        pre-existing, UNRELATED false positives this issue does not ask us to
        fix (out of scope — see the issue's anti-goal against widening the
        exemption beyond self.datas[N]):

          - `coef[1]` (lines ~132, ~145): indexing into an OLS/AR(1)
            regression-coefficient array (np.linalg.lstsq output) by position
            — not a self.datas subscript, not a time offset at all.
          - `spread[-1]`: a plain numpy local variable, correctly still
            flagged by the (unrelated, #868-scoped) negative-index branch.

        This test locks in that no NEW/different warnings appear and that the
        self.datas[N] warnings specifically are gone, so a future change to
        either branch that regresses this file is caught, without asserting
        the file passes overall (it doesn't, for reasons outside #881).
        """
        path = strategies_dir / "engle_granger_1987_cointegration_pairs.py"
        assert path.is_file(), f"expected strategy file at {path}"
        code = path.read_text()

        passed, warnings = look_ahead_audit(code)
        assert passed is False, (
            "expected this file to still fail on the two unrelated pre-existing "
            "false positives (coef[1] regression-coefficient indexing, "
            f"spread[-1] plain-array negative index) — got zero warnings: {warnings}"
        )
        # Exactly the two known unrelated categories, nothing else (in
        # particular no self.datas[N] "positive data index" warning survives).
        assert len(warnings) == 3, f"expected exactly 3 warnings (2 coef[1] + 1 spread[-1]), got: {warnings}"
        for w in warnings:
            assert ("negative index on a non-backtrader object" in w) or ("positive data index [1]" in w), (
                f"unexpected new warning shape, investigate: {w}"
            )
        # None of the surviving warnings should be about a self.datas[N] line —
        # the fixed lines (177/178, i.e. self.datas[0]/[1].close[0]) must be
        # absent now.
        assert not any("self.datas" in w for w in warnings)


class TestLookAheadAuditRealMoreiraMuirFile:
    """ACCEPTANCE CRITERION (end-to-end, real file — not a synthetic snippet):
    moreira_muir_2017_volatility_managed no longer fails look-ahead on
    close[-i]. Loads the actual strategy source from disk via the session-scoped
    ``strategies_dir`` fixture (conftest.py) so this is a faithful reproduction
    of what run_rigor_gate sees for this strategy on the live path, not just an
    isolated code snippet."""

    def test_moreira_muir_file_passes_look_ahead_audit(self, strategies_dir) -> None:
        path = strategies_dir / "moreira_muir_2017_volatility_managed.py"
        assert path.is_file(), f"expected strategy file at {path}"
        code = path.read_text()

        passed, warnings = look_ahead_audit(code)
        assert passed, f"moreira_muir_2017_volatility_managed.py still fails look-ahead audit: {warnings}"
        assert warnings == []

    def test_moreira_muir_file_passes_full_rigor_gate_look_ahead_leg(self, strategies_dir) -> None:
        """The look_ahead leg of the composite run_rigor_gate result (not just
        the standalone look_ahead_audit call) reports PASS for this file — the
        exact leg that previously hard-failed passes_all regardless of DSR/PBO/
        OOS, since passes_all requires look_ahead_passed to be True."""
        path = strategies_dir / "moreira_muir_2017_volatility_managed.py"
        code = path.read_text()

        rng = np.random.default_rng(42)
        returns = rng.normal(0.001, 0.008, size=300).tolist()
        result = run_rigor_gate(
            strategy_id="moreira_muir_2017_volatility_managed",
            daily_returns=returns,
            num_trials=1,
            strategy_code=code,
        )
        assert result.look_ahead_passed is True
        assert result.gate_details["look_ahead"] == "PASS"


# ─── Rigor gate composite (migrated from selection_bias.py) ──────────


class TestRigorGate:
    def test_passes_all(self) -> None:
        rng = np.random.default_rng(42)
        returns = rng.normal(0.001, 0.008, size=500).tolist()

        result = run_rigor_gate(
            strategy_id="test_good",
            daily_returns=returns,
            num_trials=5,
            pbo_scores={"test_good": 0.15},
            strategy_code="class S: def next(self): self.buy()",
        )

        assert isinstance(result, RigorGateResult)
        assert isinstance(result.passes_all, bool)
        assert isinstance(result.gate_details, dict)
        assert "dsr" in result.gate_details
        assert "pbo" in result.gate_details
        assert "oos_sharpe" in result.gate_details
        assert "look_ahead" in result.gate_details

    def test_fails_without_pbo(self) -> None:
        rng = np.random.default_rng(42)
        returns = rng.normal(0.001, 0.008, size=500).tolist()

        result = run_rigor_gate(
            strategy_id="test_no_pbo",
            daily_returns=returns,
            num_trials=5,
            pbo_scores=None,
            strategy_code="class S: def next(self): self.buy()",
        )

        assert not result.passes_all
        assert result.gate_details["pbo"] == "MISSING (source=cohort)"

    def test_fails_with_high_pbo(self) -> None:
        result = RigorGateResult(
            strategy_id="test",
            dsr_p_value=0.99,
            pbo_score=0.6,
            oos_sharpe=1.0,
            look_ahead_passed=True,
            in_sample_sharpe=1.5,
        )
        assert not result.passes_all

    def test_fails_with_low_oos_ratio(self) -> None:
        result = RigorGateResult(
            strategy_id="test",
            dsr_p_value=0.99,
            pbo_score=0.2,
            oos_sharpe=0.3,
            look_ahead_passed=True,
            in_sample_sharpe=1.5,
        )
        assert not result.passes_all

    def test_explicit_look_ahead_override(self) -> None:
        rng = np.random.default_rng(42)
        returns = rng.normal(0.001, 0.008, size=500).tolist()

        result = run_rigor_gate(
            strategy_id="test_override",
            daily_returns=returns,
            num_trials=5,
            pbo_scores={"test_override": 0.15},
            look_ahead_audit_passed=True,
        )
        assert result.look_ahead_passed is True


class TestDegenerateSeriesCategory:
    """#1184: a mathematically constant (zero-variance) persisted return series
    is broken data or a zero-trade backtest, not "pending more data" and not an
    ordinary statistical "fail". ``run_rigor_gate`` must categorize it as its own
    honest, distinct state — reusing the SAME test ``_rigor_helpers.py``'s
    ``compute_dsr``/``compute_oos_sharpe`` guards use (``np.ptp(arr) == 0.0``) so
    the two agree by construction, never re-deriving or loosening it.
    """

    def test_is_zero_variance_series_matches_the_dsr_guard(self) -> None:
        """Same predicate as _rigor_helpers.py:186 — by construction, not by luck."""
        assert is_zero_variance_series([0.0] * 100) is True
        assert is_zero_variance_series([0.001] * 5659) is True  # constant non-zero too
        assert is_zero_variance_series([]) is False  # nothing to judge yet
        rng = np.random.default_rng(3)
        assert is_zero_variance_series(rng.normal(0.001, 0.008, size=100).tolist()) is False

    def test_run_rigor_gate_flags_a_constant_5659_point_series_as_degenerate(self) -> None:
        """Matches the real defect: five strategies with a constant 5,659-obs series."""
        result = run_rigor_gate(strategy_id="degenerate-5659", daily_returns=[0.0] * 5659, num_trials=1)

        assert result.is_degenerate is True
        assert result.tri_state_status == "degenerate"
        assert result.tri_state_status != "pending"
        assert result.tri_state_status != "fail"
        assert result.passes_all is False
        # The math itself is untouched: DSR/OOS Sharpe are still correctly None
        # (compute_dsr's own guard, unweakened) — is_degenerate is a NEW signal
        # layered on top, not a replacement for it.
        assert result.deflated_sharpe is None
        assert result.dsr_p_value is None
        assert result.oos_sharpe is None

    def test_gate_details_says_degenerate_not_missing(self) -> None:
        """The per-criterion detail string must not read as an ordinary MISSING —
        that would still look like "not run yet" to a reader of the /gate route."""
        result = run_rigor_gate(strategy_id="degenerate-details", daily_returns=[0.0] * 5659, num_trials=1)
        assert "DEGENERATE" in result.gate_details["dsr"]
        assert "DEGENERATE" in result.gate_details["oos_sharpe"]

    def test_short_series_is_not_flagged_degenerate(self) -> None:
        """The new category must not swallow the pre-existing 'too few observations
        to grade at all' case — that stays the caller's job (verdict_from_returns
        short-circuits to `pending` before run_rigor_gate even runs)."""
        result = run_rigor_gate(strategy_id="too-short", daily_returns=[0.01, -0.02, 0.03], num_trials=1)
        assert result.is_degenerate is False

    def test_a_real_losing_series_is_not_flagged_degenerate(self) -> None:
        """Genuinely graded-and-failed must stay 'fail', never misreported as
        'degenerate' just because it lost — is_degenerate is about the INPUT
        being constant, not about the verdict being negative."""
        rng = np.random.default_rng(9)
        losing = rng.normal(-0.002, 0.01, size=300).tolist()
        result = run_rigor_gate(strategy_id="real-loser", daily_returns=losing, num_trials=1)
        assert result.is_degenerate is False
        assert result.tri_state_status == "fail"

    # ── OOS-window-only flatness (#1184 follow-up) ───────────────────────
    #
    # is_zero_variance_series alone tests the FULL series. compute_oos_sharpe
    # tests only the chronological OOS slice. A series that is non-constant
    # overall but goes flat only inside the OOS window (a strategy that stops
    # trading partway through the backtest — the exact "zero-trade backtest"
    # cause #1184 names) made compute_oos_sharpe return None while
    # is_zero_variance_series(full series) returned False, silently falling
    # through to an undifferentiated "fail" + "MISSING" — the defect #1184
    # exists to eliminate. is_oos_zero_variance_series + its OR into
    # run_rigor_gate's is_degenerate closes that gap.

    def test_is_oos_zero_variance_series_matches_compute_oos_sharpes_guard(self) -> None:
        """Mirrors compute_oos_sharpe's own split/length thresholds exactly."""
        rng = np.random.default_rng(11)
        real = rng.normal(0.001, 0.01, 3900).tolist()
        flat_tail = [0.0] * 1759
        series = real + flat_tail  # len 5659, split@0.70 = 3961 -> oos is the flat tail
        assert is_oos_zero_variance_series(series) is True
        # The FULL series is NOT constant -- the two predicates are independent.
        assert is_zero_variance_series(series) is False
        # Too short for compute_oos_sharpe to even attempt (T < 10).
        assert is_oos_zero_variance_series([0.0, 0.01, -0.02]) is False
        # OOS slice too short (< 21 bars) even though T >= 10.
        assert is_oos_zero_variance_series([0.01] * 25) is False
        # A real (non-constant) OOS slice is not flagged.
        assert is_oos_zero_variance_series(rng.normal(0.001, 0.01, 200).tolist()) is False

    def test_compute_oos_sharpe_returns_none_for_the_same_oos_only_flat_series(self) -> None:
        """Pins the exact defect: compute_oos_sharpe is None for this series even
        though it is not degenerate by the full-series test alone."""
        rng = np.random.default_rng(11)
        series = rng.normal(0.001, 0.01, 3900).tolist() + [0.0] * 1759
        assert compute_oos_sharpe(series) is None
        assert is_zero_variance_series(series) is False

    def test_run_rigor_gate_flags_oos_only_flat_series_as_degenerate_not_fail(self) -> None:
        """Before the OR-in of is_oos_zero_variance_series, this reproduced the
        exact defect the finding names: is_degenerate=False, tri_state='fail',
        gate_details['oos_sharpe']='MISSING' — a real bug hidden behind the
        undifferentiated fail. Now it must categorize as 'degenerate'."""
        rng = np.random.default_rng(11)
        series = rng.normal(0.001, 0.01, 3900).tolist() + [0.0] * 1759
        result = run_rigor_gate(strategy_id="oos-only-flat", daily_returns=series, num_trials=1)

        assert result.is_degenerate is True
        assert result.tri_state_status == "degenerate"
        assert result.tri_state_status != "fail"
        assert "DEGENERATE" in result.gate_details["oos_sharpe"]
        assert result.oos_sharpe is None  # the underlying math is still untouched

    def test_oos_only_degenerate_series_reports_a_legitimate_dsr_not_illegitimate(self) -> None:
        """#1184 round-2 finding: run_rigor_gate previously stored only the OR of
        the two degeneracy predicates on the result, so gate_details["dsr"]
        (gated on that OR) claimed "not a legitimate DSR" even when the FULL
        series was NOT flat and compute_dsr had run over it and produced a real,
        passing p-value. Only the OOS *slice* was flat here — that makes
        compute_oos_sharpe undefined, but it says nothing about compute_dsr,
        which grades the full series. gate_details["dsr"] must report the DSR's
        actual PASS/FAIL, never claim illegitimacy, unless the FULL series itself
        is degenerate (is_full_series_degenerate)."""
        rng = np.random.default_rng(11)
        series = rng.normal(0.001, 0.01, 3900).tolist() + [0.0] * 1759
        result = run_rigor_gate(strategy_id="oos-only-flat-dsr-legit", daily_returns=series, num_trials=1)

        # Preconditions: this is the OOS-only-degenerate case, not full-series.
        assert result.is_degenerate is True
        assert result.is_oos_degenerate is True
        assert result.is_full_series_degenerate is False
        # The DSR itself was computed over the (non-constant) full series and
        # passed the strictest profile's bar (p >= DSR_P_BADGE_MIN) — legitimate.
        assert result.dsr_p_value is not None
        assert result.dsr_p_value >= result.profile.dsr_p_min

        dsr_detail = result.gate_details["dsr"]
        assert "not a legitimate DSR" not in dsr_detail
        assert "DEGENERATE" not in dsr_detail
        assert dsr_detail.startswith("PASS")
        # The OOS half is still honestly reported as degenerate — this test is
        # about the DSR detail specifically, not about hiding the OOS problem.
        assert "DEGENERATE" in result.gate_details["oos_sharpe"]


# ─── Additional coverage: gate_details branches + run_rigor_gate paths ──────

# Deterministic return series (no np.random to avoid VoidDType issue).
# _RETURNS_50: DSR p=0.917 — BELOW the badge bar since #1794 restored it to
# DSR_P_BADGE_MIN. Used only for structural tests that don't hinge on the verdict.
# _RETURNS_80: DSR p=1.0 (clears gate) — used where a strong series is needed.
_RETURNS_50 = [0.01 * ((-1) ** i) * 0.5 + 0.001 for i in range(50)]
_RETURNS_80 = [0.01, -0.005, 0.008, 0.003] * 20


# ─── DSR edge-cases not hit by the first 40 tests ────────────────────


def test_dsr_from_stats_returns_none_for_t_less_than_4():
    """_dsr_from_stats with T < 4 must return (None, None) before any math."""
    assert _dsr_from_stats(0.01, 3, 0.0, 3.0, 1) == (None, None)
    assert _dsr_from_stats(0.05, 1, 0.0, 3.0, 5) == (None, None)
    assert _dsr_from_stats(0.02, 2, -0.2, 5.0, 10) == (None, None)


def test_dsr_from_stats_returns_none_when_denom_sq_nonpositive():
    """denom_sq = 1 - gamma_3*SR + (gamma_4-1)/4*SR^2 <= 0 must return (None, None).

    With SR_hat=1.0, gamma_3=3.0, gamma_4=3.0 (raw Pearson kurtosis):
      denom_sq = 1 - 3*1 + (3-1)/4*1^2 = 1 - 3 + 0.5 = -1.5 <= 0
    """
    result = _dsr_from_stats(1.0, 100, 3.0, 3.0, 1)
    assert result == (None, None)


def test_dsr_from_stats_returns_none_when_denom_sq_strictly_negative():
    """A larger SR amplifies the negative denom_sq further.

    With SR_hat=2.0, gamma_3=3.0, gamma_4=3.0:
      denom_sq = 1 - 3*2 + (3-1)/4*4 = 1 - 6 + 2 = -3.0 (unambiguously < 0)
    This is a distinct parameter set from the SR=1.0 case, verifying that the
    guard fires for different magnitudes of negative denom_sq.
    """
    result = _dsr_from_stats(2.0, 100, 3.0, 3.0, 1)
    assert result == (None, None)


def test_compute_dsr_minimal_valid_series():
    """compute_dsr with exactly T=4 non-constant returns must produce a result."""
    returns = [0.01, 0.02, -0.01, 0.03]
    dsr, p_val = compute_dsr(returns, num_trials=1)
    assert dsr is not None
    assert p_val is not None
    # Pinned reference: T=4 with these values yields p=0.855 (verified manually).
    assert p_val == pytest.approx(0.855, abs=0.01)


def test_compute_dsr_with_five_returns_and_multiple_trials():
    """compute_dsr with a slightly larger series exercises the full lines 85-88 path."""
    returns = [0.005, -0.003, 0.010, -0.002, 0.007]
    dsr, p_val = compute_dsr(returns, num_trials=3)
    assert dsr is not None
    assert p_val is not None
    assert isinstance(dsr, float)
    assert isinstance(p_val, float)


# ─── OOS Sharpe edge-cases ────────────────────────────────────────────


def test_oos_sharpe_returns_none_when_oos_slice_too_short():
    """Line 246: OOS slice < 5 bars after split must return None.

    T=12, train_fraction=0.7 -> split=8, oos=4 items < 5.
    """
    returns = [0.01, 0.02] * 6  # 12 items
    result = compute_oos_sharpe(returns, train_fraction=0.7)
    assert result is None


def test_oos_sharpe_returns_none_for_constant_oos_slice():
    """Line 249: OOS slice with zero variance (ptp == 0) must return None.

    T=20, train_fraction=0.6 -> split=12, oos=8 items all identical.
    """
    returns = [0.01] * 12 + [1.0] * 8  # oos is constant 1.0
    result = compute_oos_sharpe(returns, train_fraction=0.6)
    assert result is None


def test_oos_sharpe_returns_none_for_constant_oos_slice_with_varied_train():
    """Variant: varied IS slice but constant OOS still hits the ptp==0 guard."""
    is_part = [0.01 * (i % 5 - 2) for i in range(15)]  # 15 varied items
    oos_part = [0.005] * 5  # exactly 5 constant items
    returns = is_part + oos_part  # T=20, split=15 with fraction=0.75
    result = compute_oos_sharpe(returns, train_fraction=0.75)
    assert result is None


# ─── _sharpe_per_col single-row guard ────────────────────────────────


def test_sharpe_per_col_single_row_returns_zeros():
    """Line 338: R.shape[0] < 2 must return zero vector of length n_cols."""
    R = np.array([[0.01, 0.02, 0.03]])  # shape (1, 3)
    result = _sharpe_per_col(R)
    assert result.shape == (3,)
    assert np.all(result == 0.0)


def test_sharpe_per_col_single_row_single_col():
    """Single-row, single-column matrix also returns [0.0]."""
    R = np.array([[0.05]])  # shape (1, 1)
    result = _sharpe_per_col(R)
    assert result.shape == (1,)
    assert result[0] == 0.0


# ─── _get_func_name — all three branches ─────────────────────────────


def test_get_func_name_ast_name_returns_id():
    """Line 407: _get_func_name(ast.Name) must return the identifier string."""
    call_node = ast.parse("future(x)").body[0].value
    assert isinstance(call_node.func, ast.Name)
    assert _get_func_name(call_node.func) == "future"


def test_get_func_name_ast_attribute_returns_attr():
    """Line 410: _get_func_name(ast.Attribute) must return the attribute name."""
    call_node = ast.parse("self.predict(x)").body[0].value
    assert isinstance(call_node.func, ast.Attribute)
    assert _get_func_name(call_node.func) == "predict"


def test_get_func_name_unknown_node_returns_none():
    """Line 411: _get_func_name with a non-Name, non-Attribute node must return None."""
    const_node = ast.Constant(value=42)
    assert _get_func_name(const_node) is None


def test_look_ahead_audit_bare_function_name_triggers_warning():
    """Bare function named 'future' (ast.Name path) must be flagged."""
    code = "result = future(prices)"
    passed, warnings = look_ahead_audit(code)
    assert not passed
    assert any("future" in w for w in warnings)


def test_look_ahead_audit_bare_look_ahead_function():
    """Bare function named 'look_ahead' (ast.Name) must be flagged."""
    code = "val = look_ahead(bar)"
    passed, warnings = look_ahead_audit(code)
    assert not passed
    assert len(warnings) == 1


# ─── RigorGateResult.passes_all — every early-return branch ─────────


class TestPassesAllBranches:
    def test_dsr_p_value_none_returns_false(self):
        """Line 444: dsr_p_value is None -> passes_all is False."""
        r = RigorGateResult("s", dsr_p_value=None)
        assert r.passes_all is False

    def test_dsr_p_value_below_threshold_returns_false(self):
        """Line 446: dsr_p_value < 0.95 -> passes_all is False."""
        r = RigorGateResult("s", dsr_p_value=0.80)
        assert r.passes_all is False

    def test_dsr_p_value_exactly_threshold_not_blocked_by_dsr(self):
        """dsr_p_value == 0.95 clears the DSR gate (does not hit line 446 return)."""
        r = RigorGateResult("s", dsr_p_value=0.95, pbo_score=None)
        # Falls through DSR check but blocked by missing pbo -> still False
        assert r.passes_all is False

    def test_pbo_score_none_returns_false(self):
        """Line 448: pbo_score is None (even with passing DSR) -> passes_all is False."""
        r = RigorGateResult("s", dsr_p_value=0.97, pbo_score=None)
        assert r.passes_all is False

    def test_pbo_score_at_boundary_fails(self):
        """pbo_score == 0.5 hits the >= 0.5 branch -> passes_all is False."""
        r = RigorGateResult("s", dsr_p_value=0.97, pbo_score=0.5)
        assert r.passes_all is False

    def test_oos_sharpe_none_returns_false(self):
        """Line 452: oos_sharpe is None (passing DSR + PBO) -> passes_all is False."""
        r = RigorGateResult("s", dsr_p_value=0.97, pbo_score=0.2, oos_sharpe=None)
        assert r.passes_all is False

    def test_look_ahead_passed_true_returns_true(self):
        """Line 455: all checks clear + look_ahead_passed=True -> passes_all is True."""
        r = RigorGateResult(
            "s",
            dsr_p_value=0.97,
            pbo_score=0.2,
            oos_sharpe=1.5,
            look_ahead_passed=True,
            in_sample_sharpe=2.0,  # ratio 0.75 >= 0.5
        )
        assert r.passes_all is True

    def test_look_ahead_passed_false_returns_false_at_line_455(self):
        """Line 455: all checks clear but look_ahead_passed=False -> passes_all is False."""
        r = RigorGateResult(
            "s",
            dsr_p_value=0.97,
            pbo_score=0.2,
            oos_sharpe=1.5,
            look_ahead_passed=False,
            in_sample_sharpe=2.0,
        )
        assert r.passes_all is False

    def test_passes_all_no_in_sample_sharpe_skips_ratio_check(self):
        """When in_sample_sharpe is None the OOS/IS ratio check is skipped entirely."""
        r = RigorGateResult(
            "s",
            dsr_p_value=0.97,
            pbo_score=0.2,
            oos_sharpe=0.1,  # very low, but ratio check is skipped
            look_ahead_passed=True,
            in_sample_sharpe=None,
        )
        assert r.passes_all is True

    def test_passes_all_negative_in_sample_sharpe_skips_ratio_check(self):
        """When in_sample_sharpe <= 0 the condition in_sample_sharpe > 0 is False,
        so the OOS/IS ratio check is bypassed."""
        r = RigorGateResult(
            "s",
            dsr_p_value=0.97,
            pbo_score=0.2,
            oos_sharpe=0.1,
            look_ahead_passed=True,
            in_sample_sharpe=-0.5,  # negative -> ratio check skipped
        )
        assert r.passes_all is True

    # NaN-hardening: every IEEE-754 comparison against NaN is False, so a NaN
    # metric (not None) used to skip its fail branch and let the gate continue
    # toward PASS. Each numeric metric must now fail on a non-finite value.
    def test_nan_dsr_p_value_fails(self):
        r = RigorGateResult("s", dsr_p_value=float("nan"), pbo_score=0.2, oos_sharpe=1.0, look_ahead_passed=True)
        assert r.passes_all is False

    def test_nan_pbo_score_fails(self):
        r = RigorGateResult("s", dsr_p_value=0.97, pbo_score=float("nan"), oos_sharpe=1.0, look_ahead_passed=True)
        assert r.passes_all is False

    def test_nan_oos_sharpe_fails(self):
        r = RigorGateResult("s", dsr_p_value=0.97, pbo_score=0.2, oos_sharpe=float("nan"), look_ahead_passed=True)
        assert r.passes_all is False

    def test_nan_cpcv_positive_fraction_fails(self):
        r = RigorGateResult(
            "s",
            dsr_p_value=0.97,
            pbo_score=0.2,
            oos_sharpe=1.0,
            look_ahead_passed=True,
            in_sample_sharpe=2.0,
            cpcv_positive_fraction=float("nan"),
        )
        assert r.passes_all is False


# ─── RigorGateResult.gate_details — every branch ─────────────────────


class TestGateDetailsBranches:
    def test_dsr_pass_branch(self):
        """dsr_p_value >= 0.95 renders 'PASS (p=...)'."""
        r = RigorGateResult("s", dsr_p_value=0.9700)
        assert r.gate_details["dsr"] == "PASS (p=0.9700)"

    def test_dsr_fail_branch(self):
        """dsr_p_value < the badge bar (level 1) but not None renders
        'FAIL (p=..., need >= <bar>)' using the Unicode greater-than-or-equal sign
        (U+2265) as in the source. The bar is rigor_profiles.DSR_P_BADGE_MIN — the
        one definition (#1794), read here rather than restated."""
        r = RigorGateResult("s", dsr_p_value=0.8000)
        assert r.gate_details["dsr"] == f"FAIL (p=0.8000, need ≥ {DSR_P_BADGE_MIN:.2f})"

    def test_dsr_missing_branch(self):
        """dsr_p_value is None renders 'MISSING'."""
        r = RigorGateResult("s", dsr_p_value=None)
        assert r.gate_details["dsr"] == "MISSING"

    def test_pbo_pass_branch(self):
        """pbo_score < 0.5 renders 'PASS (PBO=..., source=...)' (#546)."""
        r = RigorGateResult("s", pbo_score=0.3000)
        assert r.gate_details["pbo"] == "PASS (PBO=0.3000, source=cohort)"

    def test_pbo_fail_branch(self):
        """pbo_score >= 0.5 but not None renders 'FAIL (..., source=...)' (#546)."""
        r = RigorGateResult("s", pbo_score=0.6000)
        assert r.gate_details["pbo"] == "FAIL (PBO=0.6000, need < 0.50, source=cohort)"

    def test_pbo_missing_branch(self):
        """pbo_score is None renders 'MISSING (source=...)' (#546)."""
        r = RigorGateResult("s", pbo_score=None)
        assert r.gate_details["pbo"] == "MISSING (source=cohort)"

    def test_pbo_source_library_label(self):
        """A library-level PBO labels the detail source=library (#546)."""
        r = RigorGateResult("s", pbo_score=0.3000, pbo_source="library")
        assert r.gate_details["pbo"] == "PASS (PBO=0.3000, source=library)"

    def test_oos_sharpe_pass_ratio(self):
        """oos_sharpe set, in_sample_sharpe > 0, ratio >= 0.5 renders 'PASS (OOS/IS=...)'."""
        r = RigorGateResult("s", oos_sharpe=1.5, in_sample_sharpe=2.0)
        detail = r.gate_details["oos_sharpe"]
        assert detail.startswith("PASS (OOS/IS=")
        assert "0.75" in detail

    def test_oos_sharpe_fail_ratio(self):
        """oos_sharpe set, in_sample_sharpe > 0, ratio < 0.5 renders 'FAIL (OOS/IS=...)'
        using the Unicode >= sign (U+2265) as in the source f-string."""
        r = RigorGateResult("s", oos_sharpe=0.3, in_sample_sharpe=2.0)
        detail = r.gate_details["oos_sharpe"]
        assert detail.startswith("FAIL (OOS/IS=")
        assert "need ≥ 0.50" in detail

    def test_oos_sharpe_set_no_is_reference(self):
        """Line 482: oos_sharpe is set but in_sample_sharpe is None renders 'SET (OOS=...)'."""
        r = RigorGateResult("s", oos_sharpe=1.5, in_sample_sharpe=None)
        detail = r.gate_details["oos_sharpe"]
        assert detail == "SET (OOS=1.5000, no IS reference)"

    def test_oos_sharpe_set_with_negative_in_sample(self):
        """oos_sharpe is set but in_sample_sharpe <= 0 falls through to SET branch."""
        r = RigorGateResult("s", oos_sharpe=0.8, in_sample_sharpe=-0.5)
        detail = r.gate_details["oos_sharpe"]
        assert detail.startswith("SET (OOS=")
        assert "no IS reference" in detail

    def test_oos_sharpe_missing_branch(self):
        """Line 484: oos_sharpe is None renders 'MISSING'."""
        r = RigorGateResult("s", oos_sharpe=None)
        assert r.gate_details["oos_sharpe"] == "MISSING"

    def test_look_ahead_pass(self):
        """look_ahead_passed=True renders 'PASS'."""
        r = RigorGateResult("s", look_ahead_passed=True)
        assert r.gate_details["look_ahead"] == "PASS"

    def test_look_ahead_fail(self):
        """look_ahead_passed=False (default) renders 'FAIL'."""
        r = RigorGateResult("s")
        assert r.gate_details["look_ahead"] == "FAIL"

    def test_gate_details_returns_all_four_keys(self):
        """gate_details must contain the four gate keys + DSR convention (#547) +
        the DSR SE model (#621 follow-up) + the IID advisory (#621) + the rf
        convention disclosure (#1409)."""
        r = RigorGateResult("s")
        keys = set(r.gate_details.keys())
        assert keys == {
            "dsr",
            "dsr_convention",
            "rf_convention",
            "dsr_se",
            "pbo",
            "oos_sharpe",
            "look_ahead",
            "cpcv",
            "iid",
            "regime_robustness",
        }
        assert r.gate_details["dsr_convention"] == "excess"
        # Default RigorGateResult carries no HAC verdict -> classical IID SE label.
        assert r.gate_details["dsr_se"] == "IID (classical Bailey-LdP)"
        # Advisories are unset by default (no return series) -> MISSING.
        assert r.gate_details["iid"] == "MISSING"
        assert r.gate_details["regime_robustness"] == "MISSING"


# ─── run_rigor_gate — all branches in lines 509-555 ──────────────────


class TestRunRigorGatePaths:
    def test_strategy_code_none_sets_la_passed_false(self):
        """Line 521: strategy_code=None -> la_passed defaults to False."""
        result = run_rigor_gate("s", _RETURNS_50, strategy_code=None)
        assert result.look_ahead_passed is False

    def test_look_ahead_audit_passed_override_true(self):
        """Line 524: look_ahead_audit_passed=True overrides the computed la_passed."""
        result = run_rigor_gate("s", _RETURNS_50, strategy_code=None, look_ahead_audit_passed=True)
        assert result.look_ahead_passed is True

    def test_look_ahead_audit_passed_override_false_overrides_clean_code(self):
        """Line 524: look_ahead_audit_passed=False overrides even clean code audit."""
        clean_code = "class S:\n    def next(self):\n        self.buy()"
        result = run_rigor_gate("s", _RETURNS_80, strategy_code=clean_code, look_ahead_audit_passed=False)
        assert result.look_ahead_passed is False

    def test_strategy_code_with_look_ahead_warning_logs_and_fails(self):
        """Lines 517-519: code with a look-ahead warning -> la_passed=False + logged."""
        code_with_warning = "price = data[2]"  # positive index triggers warning
        result = run_rigor_gate("s", _RETURNS_80, strategy_code=code_with_warning)
        assert result.look_ahead_passed is False

    def test_in_sample_sharpe_derived_when_not_provided(self):
        """Lines 527-531: in_sample_sharpe is None and returns have variance -> derived."""
        result = run_rigor_gate("s", _RETURNS_80, in_sample_sharpe=None)
        assert result.in_sample_sharpe is not None
        assert isinstance(result.in_sample_sharpe, float)

    def test_in_sample_sharpe_explicit_not_overwritten(self):
        """When in_sample_sharpe is provided explicitly it must be preserved unchanged."""
        result = run_rigor_gate("s", _RETURNS_80, in_sample_sharpe=2.5)
        assert result.in_sample_sharpe == 2.5

    def test_in_sample_sharpe_none_for_single_item_series(self):
        """Lines 527: len(daily_returns) < 2 -> in_sample_sharpe remains None."""
        result = run_rigor_gate("s", [0.01])
        assert result.in_sample_sharpe is None

    def test_in_sample_sharpe_none_for_zero_variance_series(self):
        """Lines 529-531: sigma == 0.0 exactly -> in_sample_sharpe remains None.

        [1.0]*20 gives std(ddof=1) == 0.0 exactly (exact IEEE-754 representation).
        """
        result = run_rigor_gate("s", [1.0] * 20)
        assert result.in_sample_sharpe is None

    def test_pbo_score_looked_up_from_dict(self):
        """Line 509: pbo_scores dict present -> pbo_score is fetched by strategy_id."""
        result = run_rigor_gate("my_strat", _RETURNS_80, pbo_scores={"my_strat": 0.3})
        assert result.pbo_score == 0.3

    def test_pbo_score_none_when_id_missing_from_dict(self):
        """pbo_scores dict present but strategy_id absent -> pbo_score is None."""
        result = run_rigor_gate("missing_id", _RETURNS_80, pbo_scores={"other_strat": 0.3})
        assert result.pbo_score is None

    def test_pbo_score_none_when_no_dict(self):
        """pbo_scores=None -> pbo_score is None."""
        result = run_rigor_gate("s", _RETURNS_80, pbo_scores=None)
        assert result.pbo_score is None

    def test_paper_claimed_sharpe_stored(self):
        """paper_claimed_sharpe is passed through to the result object unchanged."""
        result = run_rigor_gate("s", _RETURNS_80, paper_claimed_sharpe=1.8)
        assert result.paper_claimed_sharpe == 1.8

    def test_result_has_strategy_id(self):
        """run_rigor_gate result.strategy_id must match the input strategy_id."""
        result = run_rigor_gate("unique_id_xyz", _RETURNS_50)
        assert result.strategy_id == "unique_id_xyz"

    def test_result_is_rigor_gate_result_instance(self):
        """run_rigor_gate must always return a RigorGateResult."""
        result = run_rigor_gate("s", _RETURNS_50)
        assert isinstance(result, RigorGateResult)

    def test_gate_details_populated_by_run_rigor_gate(self):
        """gate_details on the returned result must have the four gate keys + DSR
        convention (#547) + DSR SE model (#621 follow-up) + IID (#621) + rf
        convention (#1409)."""
        result = run_rigor_gate("s", _RETURNS_80)
        assert set(result.gate_details.keys()) == {
            "dsr",
            "dsr_convention",
            "rf_convention",
            "dsr_se",
            "pbo",
            "oos_sharpe",
            "look_ahead",
            "cpcv",
            "iid",
            "regime_robustness",
        }
        # run_rigor_gate computes the gating DSR with the HAC-robust SE (#621 follow-up).
        assert result.dsr_se_method == "hac"
        assert "HAC" in result.gate_details["dsr_se"]

    def test_regime_robustness_surfaced_but_advisory(self):
        """Regime-robustness is computed for a long-enough series, surfaced, and never gates."""
        # 80 bars >= 63 → regime classification runs and robustness is populated.
        result = run_rigor_gate("s", _RETURNS_80, num_trials=1, pbo_scores={"s": 0.1}, look_ahead_audit_passed=True)
        assert result.regime_robustness is not None
        assert result.gate_details["regime_robustness"].startswith("ADVISORY")
        # toggling the advisory does not move passes_all
        before = result.passes_all
        result.regime_robustness = {"min_regime_sharpe": -9.0, "consistency": 0.0, "robust": False}
        assert result.passes_all == before
        # a short series simply reports MISSING (no crash)
        short = run_rigor_gate("s2", [0.01, -0.01, 0.02, 0.0, 0.01])
        assert short.gate_details["regime_robustness"] == "MISSING"

    def test_iid_diagnostic_surfaced_but_advisory(self):
        """#621: run_rigor_gate computes + surfaces the IID diagnostic, but it never gates pass/fail.

        Autocorrelated returns are the edge for trend/momentum strategies, so an IID
        violation must be advisory only.
        """
        result = run_rigor_gate("s", _RETURNS_80)
        # computed + surfaced (not None / not dropped)
        assert result.iid_diagnostics is not None
        assert "iid_assumption_violated" in result.iid_diagnostics
        assert result.gate_details["iid"].startswith("ADVISORY")
        # A clearly autocorrelated series (strong trend) must NOT, by itself, fail the gate.
        trending = [0.01 * (1 + 0.5 * (i % 3 == 0)) for i in range(80)]
        r2 = run_rigor_gate("s2", trending, num_trials=1, pbo_scores={"s2": 0.1}, look_ahead_audit_passed=True)
        # whatever passes_all decides, it is decided WITHOUT reference to the IID flag:
        # toggling the advisory flag on the result does not change passes_all.
        before = r2.passes_all
        r2.iid_assumption_violated = not r2.iid_assumption_violated
        assert r2.passes_all == before

    def test_num_trials_stored_on_result(self):
        """num_trials argument must be stored on the result."""
        result = run_rigor_gate("s", _RETURNS_80, num_trials=7)
        assert result.num_trials == 7


# ─── CPCV Edge Cases ──────────────────────────────────────────────────


def test_cpcv_returns_none_for_empty_array():
    assert compute_cpcv_oos_sharpe([]) is None


def test_cpcv_returns_none_for_single_asset_zero_variance():
    assert compute_cpcv_oos_sharpe([[0.01] * 100] * 15) is None


def test_cpcv_returns_none_for_infinite_values():
    # Numpy calculations on inf cause warnings and usually return nan/inf std
    res = compute_cpcv_oos_sharpe([[0.01] * 50 + [np.inf] * 50] * 15)
    assert res is None or res["mean_oos_sharpe"] is None or math.isnan(res["mean_oos_sharpe"])


def test_cpcv_returns_none_for_insufficient_splits():
    assert compute_cpcv_oos_sharpe([[0.01, -0.01] * 2] * 15, n_groups=6, test_groups=2) is None


# ─── Effective-N correlation wiring (DSR) ────────────────────────────


class TestAveragePairwiseCorrelation:
    """compute_average_pairwise_correlation — the input to the DSR effective-N
    correction that was previously never computed by any caller."""

    def test_identical_series_correlation_is_one(self):
        s = [0.01, -0.02, 0.03, 0.0, 0.015, -0.005]
        assert compute_average_pairwise_correlation({"a": s, "b": s}) == pytest.approx(1.0, abs=1e-9)

    def test_independent_series_correlation_near_zero(self):
        rng = np.random.default_rng(0)
        m = {f"s{i}": list(rng.normal(0.0, 0.01, 600)) for i in range(8)}
        assert 0.0 <= compute_average_pairwise_correlation(m) < 0.15

    def test_negative_correlation_clamped_to_zero(self):
        s = [0.01, -0.02, 0.03, -0.01, 0.02, -0.03]
        anti = [-x for x in s]
        # Perfectly anti-correlated → raw mean corr = -1 → clamped to 0 (no relief).
        assert compute_average_pairwise_correlation([s, anti]) == 0.0

    def test_fewer_than_two_series_returns_zero(self):
        assert compute_average_pairwise_correlation({"only": [0.01, 0.02, 0.03]}) == 0.0
        assert compute_average_pairwise_correlation([]) == 0.0

    def test_zero_variance_rows_dropped(self):
        flat = [0.01] * 100
        live = list(np.random.default_rng(1).normal(0.0, 0.01, 100))
        # One live + one flat → < 2 usable series after dropping the flat row.
        assert compute_average_pairwise_correlation([flat, live]) == 0.0


class TestDsrCorrelationRelaxesPenalty:
    """Higher trial correlation → fewer effective independent trials → smaller
    best-of-N null → higher (less-penalized) deflated Sharpe. This proves the
    average_correlation parameter actually flows through compute_dsr."""

    def test_correlated_trials_raise_deflated_sharpe(self):
        rng = np.random.default_rng(7)
        rets = list(rng.normal(0.0012, 0.01, 600))
        dsr_indep, p_indep = compute_dsr(rets, num_trials=25, average_correlation=0.0)
        dsr_corr, p_corr = compute_dsr(rets, num_trials=25, average_correlation=0.9)
        assert dsr_indep is not None and dsr_corr is not None
        assert dsr_corr > dsr_indep
        assert p_corr >= p_indep

    def test_single_trial_correlation_is_inert(self):
        # With N=1 there is no multiple-testing penalty, so correlation can't change it.
        rng = np.random.default_rng(8)
        rets = list(rng.normal(0.001, 0.01, 400))
        assert compute_dsr(rets, num_trials=1, average_correlation=0.9) == compute_dsr(
            rets, num_trials=1, average_correlation=0.0
        )

    def test_full_correlation_collapses_to_single_effective_trial(self):
        # ρ=1 means every trial is the same test, so there is no selection
        # bias to deflate and the DSR must equal the N=1 (no-penalty) result.
        # Note this endpoint does NOT discriminate between formulas: √(1−ρ) and
        # the old N_eff form both vanish here, which is part of why #1558 went
        # unnoticed. The interior is pinned in TestEquicorrelatedExpectedMax.
        rng = np.random.default_rng(9)
        rets = list(rng.normal(0.0012, 0.01, 600))
        fully_correlated = compute_dsr(rets, num_trials=25, average_correlation=1.0)
        no_penalty = compute_dsr(rets, num_trials=1, average_correlation=0.0)
        assert fully_correlated == no_penalty

    def test_intermediate_correlation_lies_between_endpoints(self):
        # Effective-N is monotonic in ρ: a partially-correlated grid deflates
        # less than an independent one (ρ=0) and more than a degenerate one (ρ=1).
        rng = np.random.default_rng(10)
        rets = list(rng.normal(0.0012, 0.01, 600))
        dsr_indep = compute_dsr(rets, num_trials=25, average_correlation=0.0)[0]
        dsr_mid = compute_dsr(rets, num_trials=25, average_correlation=0.5)[0]
        dsr_full = compute_dsr(rets, num_trials=25, average_correlation=1.0)[0]
        assert dsr_indep < dsr_mid < dsr_full

    def test_uncorrelated_path_unchanged_from_nominal_n(self):
        # ρ=0 must deflate by the full nominal N (the change only touches the
        # correlated relief, leaving independent-trial deflation untouched).
        rng = np.random.default_rng(12)
        rets = list(rng.normal(0.001, 0.01, 500))
        dsr_n10 = compute_dsr(rets, num_trials=10, average_correlation=0.0)[0]
        dsr_n50 = compute_dsr(rets, num_trials=50, average_correlation=0.0)[0]
        # More independent trials → larger best-of-N null → lower deflated Sharpe.
        assert dsr_n50 < dsr_n10


# ─── CPCV wiring into run_rigor_gate ─────────────────────────────────


class TestEquicorrelatedExpectedMax:
    """#1558: the correlated-trials E[max] must be the equicorrelated one.

    For N standard normals with equicorrelation ρ ≥ 0, the one-factor form
    ``X_i = √ρ·Z + √(1−ρ)·ε_i`` makes the common term factor out of the maximum:

        E[max_i X_i] = √(1−ρ) · E[max of N iid]

    The prior implementation instead converted N to an effective count
    ``N_eff = N/(1 + (N−1)ρ)`` (the Kish design effect) and pushed that through
    the quantile approximation, which under-deflated by 2×–10× over
    ρ ∈ [0.3, 0.9] and let the null gate admit noise at up to 29%.

    Every test in this class FAILS against that prior implementation. The three
    tests in ``TestDsrCorrelationRelaxesPenalty`` do not: they pin the ρ=0 and
    ρ=1 endpoints (where the two formulas agree exactly) and monotonicity in
    between (which both satisfy), so the whole file passed against either one.
    """

    ANNUALIZATION = 252
    EULER = 0.5772156649

    @classmethod
    def _a_n(cls, n: int) -> float:
        """Bailey-LdP two-quantile E[max of n iid standard normals]."""
        from scipy.stats import norm

        return float((1.0 - cls.EULER) * norm.ppf(1.0 - 1.0 / n) + cls.EULER * norm.ppf(1.0 - 1.0 / (n * math.e)))

    @classmethod
    def _recover_e_max(cls, sr_hat: float, t: int, n: int, rho: float) -> float:
        """Back E_max_N out of the returned deflated Sharpe.

        ``dsr = (SR_hat − SR_zero)·√252`` and ``SR_zero = √(1/(T−1))·E_max_N``,
        so E_max_N is recoverable exactly from the public return value. Reading
        it this way keeps the test on the observable output rather than on a
        private intermediate, so it still bites if the block is refactored.
        """
        dsr, _ = _dsr_from_stats(sr_hat, t, 0.0, 3.0, n, rho)
        assert dsr is not None
        return (sr_hat - dsr / math.sqrt(cls.ANNUALIZATION)) * math.sqrt(t - 1)

    @pytest.mark.parametrize("n", [4, 8, 16])
    @pytest.mark.parametrize("rho", [0.0, 0.3, 0.5, 0.7, 0.9])
    def test_expected_max_matches_the_equicorrelated_closed_form(self, n: int, rho: float) -> None:
        """The interior, pinned by value — this is what the old tests left free."""
        recovered = self._recover_e_max(0.03, 756, n, rho)
        expected = math.sqrt(1.0 - rho) * self._a_n(n)
        assert recovered == pytest.approx(expected, abs=1e-3), (
            f"N={n}, rho={rho}: E[max] is {recovered:.4f}, equicorrelated closed form is "
            f"{expected:.4f} (ratio {recovered / expected:.2f}x)"
        )

    @pytest.mark.parametrize(("n", "rho"), [(4, 0.5), (8, 0.7), (16, 0.5), (16, 0.9)])
    def test_correlation_shrinkage_factor_matches_monte_carlo(self, n: int, rho: float) -> None:
        """Independent of the closed form: simulate the maximum directly.

        Draws from a Cholesky factor of Σ = (1−ρ)I + ρ·11ᵀ rather than the
        one-factor construction, so the simulation does not assume the identity
        under test. Seeded, so it cannot go flaky.

        Compares the RATIO E[max](ρ)/E[max](0) on both sides rather than the
        levels. The two-quantile approximation to E[max of N iid] is itself
        ~2–3% high (at ρ=0 as much as anywhere), and that bias is common to
        numerator and denominator, so taking the ratio cancels it and leaves
        exactly the correlation factor this test is about. Comparing levels
        would fold a pre-existing approximation error into a correlation test.
        """
        rng = np.random.default_rng(1558)
        cov = np.full((n, n), rho) + np.eye(n) * (1.0 - rho)
        chol = np.linalg.cholesky(cov).T
        correlated = float((rng.standard_normal((200_000, n)) @ chol).max(axis=1).mean())
        independent = float(rng.standard_normal((200_000, n)).max(axis=1).mean())
        simulated_factor = correlated / independent

        recovered_factor = self._recover_e_max(0.03, 756, n, rho) / self._recover_e_max(0.03, 756, n, 0.0)
        assert recovered_factor == pytest.approx(simulated_factor, abs=0.01), (
            f"N={n}, rho={rho}: correlation shrinks E[max] by {recovered_factor:.4f}, "
            f"simulation says {simulated_factor:.4f} (closed form: {math.sqrt(1 - rho):.4f})"
        )

    def test_correlated_deflation_keeps_growing_with_trial_count(self) -> None:
        """Shape in N, not just scale.

        ``N_eff = N/(1 + (N−1)ρ)`` tends to 1/ρ, so the old form's penalty
        SATURATED: at ρ=0.7 it was 0.203 at N=16 and 0.222 at N=1000, a 9% rise
        across a 60-fold larger search. The true term grows like √(1−ρ)·√(2 ln N).
        A large correlated sweep must be charged more than a small one.
        """
        small = self._recover_e_max(0.03, 756, 16, 0.7)
        large = self._recover_e_max(0.03, 756, 1000, 0.7)
        assert large > 1.5 * small, (
            f"E[max] barely moved from N=16 ({small:.4f}) to N=1000 ({large:.4f}) — "
            "the multiple-testing penalty is saturating in N"
        )

    @pytest.mark.parametrize("n", [2, 4, 16, 100])
    def test_independent_trial_deflation_is_unchanged(self, n: int) -> None:
        """Regression guard on the ρ=0 path, which #1558 must not move.

        ρ=0 was already correct and is the only case ``test_dsr_parity`` can
        reach (the analytics-engine mirror takes no correlation argument), so
        pin it against the closed form directly.
        """
        assert self._recover_e_max(0.03, 756, n, 0.0) == pytest.approx(self._a_n(n), abs=1e-3)

    def test_null_gate_does_not_admit_noise_above_the_nominal_rate(self) -> None:
        """Calibration, which is the reason any of this matters.

        Every trial is drifted at exactly ``_RF_DAILY`` so its true EXCESS Sharpe
        is zero, matching the excess convention ``compute_dsr`` grades on (#547).
        Getting this wrong hides the defect: drifting at zero instead makes every
        trial underperform cash by 5%/yr, a far harsher null under which even the
        broken form scores a respectable 10.3%.

        The winner is then picked by in-sample Sharpe, which is precisely the
        selection event the DSR exists to correct, so a gate at ``dsr_p >= 0.90``
        should admit about 10%. The pre-#1558 form admitted 25.5% here.

        The 0.90 here is a NOMINAL calibration level chosen for a clean 10%
        expected rate and the tight binomial bound below — it is NOT the badge
        bar (``rigor_profiles.DSR_P_BADGE_MIN``, #1794). This test asks whether
        ``compute_dsr``'s null distribution is calibrated at a stated level, a
        property of the estimator; the product's admission bar is a separate
        choice and moving it must not silently re-tune this test's arithmetic.

        Seeded and bounded at 600 runs to stay fast. Binomial noise on a 10% rate
        at n=600 is ~1.2pp, so the 0.16 threshold sits ~5σ above the true rate and
        ~8σ below the broken one: it cannot flake in either direction.
        """
        rng = np.random.default_rng(1558)
        n, rho, t, runs = 16, 0.5, 756, 600
        cov = np.full((n, n), rho) + np.eye(n) * (1.0 - rho)
        chol = np.linalg.cholesky(cov).T
        false_passes = 0
        for _ in range(runs):
            paths = (rng.standard_normal((t, n)) @ chol) * 0.01 + _RF_DAILY
            in_sample = (paths.mean(axis=0) - _RF_DAILY) / paths.std(axis=0, ddof=1)
            best = paths[:, int(np.argmax(in_sample))]
            _, p_value = compute_dsr(best.tolist(), num_trials=n, average_correlation=rho)
            false_passes += p_value is not None and p_value >= 0.90
        rate = false_passes / runs
        assert rate < 0.16, (
            f"the gate admitted {rate:.1%} of pure-noise winners at N={n}, rho={rho}; "
            "a calibrated gate at dsr_p >= 0.90 admits about 10%"
        )


class TestRunRigorGateCpcvWiring:
    """run_rigor_gate previously passed a 1-D series to a 2-D-only CPCV function,
    so CPCV always returned None. These prove the corrected wiring: CPCV fires on
    a real combinatorial matrix and is honestly None without one."""

    def test_cpcv_fires_with_combinatorial_matrix(self):
        rng = np.random.default_rng(11)
        # 15 rows = C(6, 2) splits; 90 cols ≥ 5 bars/block for 6 groups.
        matrix = rng.normal(0.0006, 0.01, size=(15, 90))
        daily = list(rng.normal(0.001, 0.01, 400))
        result = run_rigor_gate("s1", daily, num_trials=6, cv_returns_matrix=matrix)
        assert result.cpcv_positive_fraction is not None
        assert 0.0 <= result.cpcv_positive_fraction <= 1.0
        assert result.cpcv_mean_oos_sharpe is not None

    def test_cpcv_honestly_none_without_matrix(self):
        rng = np.random.default_rng(12)
        daily = list(rng.normal(0.001, 0.01, 400))
        result = run_rigor_gate("s1", daily, num_trials=6)
        assert result.cpcv_positive_fraction is None
        assert result.cpcv_mean_oos_sharpe is None

    def test_run_rigor_gate_average_correlation_flows_to_dsr(self):
        rng = np.random.default_rng(13)
        daily = list(rng.normal(0.0012, 0.01, 600))
        r_indep = run_rigor_gate("s1", daily, num_trials=25, average_correlation=0.0)
        r_corr = run_rigor_gate("s1", daily, num_trials=25, average_correlation=0.9)
        assert r_corr.deflated_sharpe > r_indep.deflated_sharpe


# ─── Library-level PBO (criterion-4 input, #546) ─────────────────────


class TestAlignReturnsStore:
    """Date-aligned inner-join of the daily-returns store (parity with the
    offline analytics-engine/scripts/compute_library_pbo.py::build_aligned_matrix)."""

    def test_aligns_on_joint_dates_in_order(self) -> None:
        store = {
            "a": {"dates": ["2020-01-01", "2020-01-02", "2020-01-03"], "daily_returns": [0.1, 0.2, 0.3]},
            "b": {"dates": ["2020-01-02", "2020-01-03", "2020-01-04"], "daily_returns": [0.5, 0.6, 0.7]},
        }
        matrix = align_returns_store(store)
        # Joint dates are {01-02, 01-03}, sorted; each row maps to the right value.
        assert matrix == {"a": [0.2, 0.3], "b": [0.5, 0.6]}

    def test_fewer_than_two_series_returns_empty(self) -> None:
        store = {"a": {"dates": ["2020-01-01"], "daily_returns": [0.1]}}
        assert align_returns_store(store) == {}

    def test_empty_date_intersection_returns_empty(self) -> None:
        store = {
            "a": {"dates": ["2020-01-01", "2020-01-02"], "daily_returns": [0.1, 0.2]},
            "b": {"dates": ["2021-06-01", "2021-06-02"], "daily_returns": [0.5, 0.6]},
        }
        assert align_returns_store(store) == {}

    def test_drops_series_missing_dates_or_returns(self) -> None:
        store = {
            "a": {"dates": ["2020-01-01", "2020-01-02"], "daily_returns": [0.1, 0.2]},
            "b": {"dates": [], "daily_returns": []},  # unusable → dropped
            "c": {"dates": ["2020-01-02", "2020-01-03"], "daily_returns": [0.5, 0.6]},
        }
        matrix = align_returns_store(store)
        assert set(matrix.keys()) == {"a", "c"}
        assert matrix == {"a": [0.2], "c": [0.5]}

    def test_malformed_record_length_mismatch_raises(self) -> None:
        # strict=True (parity with the offline build_aligned_matrix): a record
        # whose dates and daily_returns lengths disagree fails loud rather than
        # silently producing a misaligned row.
        store = {
            "a": {"dates": ["2020-01-01", "2020-01-02"], "daily_returns": [0.1, 0.2]},
            "b": {"dates": ["2020-01-01", "2020-01-02", "2020-01-03"], "daily_returns": [0.5, 0.6]},
        }
        with pytest.raises(ValueError):
            align_returns_store(store)


class TestComputeLibraryPbo:
    """Single library-wide CSCV PBO over the whole selection set (#546)."""

    @staticmethod
    def _series(seed: int, n: int = 256) -> dict[str, list]:
        rng = np.random.default_rng(seed)
        dates = [f"2020-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(n)]
        return {"dates": dates, "daily_returns": rng.normal(0.0005, 0.01, n).tolist()}

    def test_returns_float_in_unit_interval(self) -> None:
        store = {f"s{i}": self._series(i) for i in range(4)}
        pbo = compute_library_pbo(store, s_partitions=8)
        assert pbo is not None
        assert 0.0 <= pbo <= 1.0

    def test_fewer_than_two_series_returns_none(self) -> None:
        store = {"a": self._series(1)}
        assert compute_library_pbo(store) is None

    def test_empty_store_returns_none(self) -> None:
        assert compute_library_pbo({}) is None

    def test_short_joint_window_fails_closed(self) -> None:
        # A joint window shorter than s_partitions is non-computable: must fail
        # closed to None, NOT return compute_pbo's all-0.0 PASS sentinel (#546).
        dates = [f"2020-01-{d:02d}" for d in range(1, 6)]  # 5 dates < default S=16
        store = {
            "a": {"dates": dates, "daily_returns": [0.01] * 5},
            "b": {"dates": dates, "daily_returns": [0.02] * 5},
        }
        assert compute_library_pbo(store) is None

    def test_empty_pbo_scores_fail_closed(self, monkeypatch) -> None:
        # If compute_pbo yields no scores, fail closed to None (not silent pass).
        store = {f"s{i}": self._series(i) for i in range(2)}
        monkeypatch.setattr("archimedes.services.rigor_evaluator.compute_pbo", lambda *a, **k: {})
        assert compute_library_pbo(store) is None

    def test_non_finite_pbo_fails_closed(self, monkeypatch) -> None:
        # A NaN/inf library PBO must not pass criterion 4 — fail closed to None.
        store = {f"s{i}": self._series(i) for i in range(2)}
        monkeypatch.setattr(
            "archimedes.services.rigor_evaluator.compute_pbo",
            lambda *a, **k: dict.fromkeys(store, float("nan")),
        )
        assert compute_library_pbo(store) is None


class TestComputeLibraryPboRfConvention:
    """2026-08-21 round-4 review fix: `compute_library_pbo` no longer threads
    the joint date axis into CSCV unconditionally. An earlier draft did so,
    making the library PBO the one live production number this PR's rf-series
    change moved with no explicit opt-in and no before/after value in the
    re-grade delta table. `use_tbill_series` (default `False`) now gates that
    behind an explicit flag, matching every other not-yet-wired call site in
    this module — `compute_library_pbo_rf_convention` mirrors the SAME flag so
    `LibraryPbo.rf_convention` (selection_bias_routes.py) can disclose
    whichever convention was ACTUALLY used, honestly, either way."""

    @staticmethod
    def _series(seed: int, n: int = 256) -> dict[str, list]:
        rng = np.random.default_rng(seed)
        dates = [f"2020-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(n)]
        return {"dates": dates, "daily_returns": rng.normal(0.0005, 0.01, n).tolist()}

    def test_default_never_passes_dates_to_compute_pbo_at_all(self, monkeypatch) -> None:
        """The not-yet-wired default (`use_tbill_series` omitted): `dates`
        must never reach `compute_pbo` at all — asserted at the CALL
        BOUNDARY (not via a PBO value comparison, which is a quantized rank
        statistic that can tie across two different rf regimes for an
        unlucky seed/window, as happened with an earlier draft of this test).
        Monkeypatches the exact `compute_pbo` symbol `compute_library_pbo`
        calls and records the `dates` kwarg every invocation actually
        received — the only way to prove the wiring, not just the output,
        without depending on PBO's discrete quantization landing on
        different buckets."""
        import archimedes.services.rigor_evaluator as rigor_evaluator_module

        seen_dates: list[object] = []
        real_compute_pbo = rigor_evaluator_module.compute_pbo

        def _recording_compute_pbo(*args, **kwargs):
            seen_dates.append(kwargs.get("dates"))
            return real_compute_pbo(*args, **kwargs)

        monkeypatch.setattr(rigor_evaluator_module, "compute_pbo", _recording_compute_pbo)

        store = {f"s{i}": self._series(i) for i in range(4)}  # real, in-coverage 2020 dates

        compute_library_pbo(store, s_partitions=8)
        assert seen_dates == [None]  # default: dates never reached compute_pbo

        compute_library_pbo(store, s_partitions=8, use_tbill_series=True)
        assert seen_dates[-1] is not None
        assert len(seen_dates[-1]) == len(next(iter(store.values()))["dates"])

    def test_matches_the_convention_compute_library_pbo_actually_used(self) -> None:
        from archimedes.services import rf_series

        store = {f"s{i}": self._series(i) for i in range(4)}
        # These dates (2020 onward) are all inside the vendored series'
        # coverage, so both the PBO value and its convention resolve to the
        # series ONLY when explicitly opted in — assert BOTH so a future edit
        # that decouples them again is caught here, not just at the payload
        # layer, and assert the default (no opt-in) stays FALLBACK even
        # though these SAME dates would resolve to the series if asked.
        assert compute_library_pbo(store, s_partitions=8, use_tbill_series=True) is not None
        assert compute_library_pbo_rf_convention(store, use_tbill_series=True) == rf_series.RF_CONVENTION_SERIES
        assert compute_library_pbo_rf_convention(store) == rf_series.RF_CONVENTION_FALLBACK
        # `use_tbill_series=True` has a real, not merely cosmetic, effect on
        # the number too (not just the disclosed label) — proven at the
        # `compute_pbo` level by `test_compute_pbo_dates_materially_changes_the_value`
        # / `test_compute_pbo_dates_axis_truncates_from_head_not_tail`
        # (`test_rf_convention_gate.py`) with a seed/window pair verified not
        # to land on the same quantized PBO bucket; this class's own seeds
        # (2020 calendar dates) are NOT guaranteed not to tie (PBO is a rank
        # statistic quantized to 1/S-sized steps), so this test does not
        # repeat that value-level claim here — only the convention wiring.

    def test_fewer_than_two_series_reports_fallback(self) -> None:
        from archimedes.services import rf_series

        store = {"a": self._series(1)}
        assert compute_library_pbo(store, use_tbill_series=True) is None
        assert compute_library_pbo_rf_convention(store, use_tbill_series=True) == rf_series.RF_CONVENTION_FALLBACK

    def test_empty_store_reports_fallback(self) -> None:
        from archimedes.services import rf_series

        assert compute_library_pbo({}, use_tbill_series=True) is None
        assert compute_library_pbo_rf_convention({}, use_tbill_series=True) == rf_series.RF_CONVENTION_FALLBACK

    def test_out_of_coverage_dates_report_fallback(self) -> None:
        """A store whose joint dates are all outside the vendored series'
        coverage must disclose FALLBACK when opted in, not silently claim the
        series."""
        from archimedes.services import rf_series

        far_future = [f"2999-01-{1 + i:02d}" for i in range(20)]
        store = {
            "a": {"dates": far_future, "daily_returns": np.random.default_rng(1).normal(0.0005, 0.01, 20).tolist()},
            "b": {"dates": far_future, "daily_returns": np.random.default_rng(2).normal(0.0005, 0.01, 20).tolist()},
        }
        assert compute_library_pbo_rf_convention(store, use_tbill_series=True) == rf_series.RF_CONVENTION_FALLBACK


class TestRigorGateLibraryPbo:
    """run_rigor_gate prefers the library PBO and labels its provenance (#546)."""

    @staticmethod
    def _returns() -> list[float]:
        rng = np.random.default_rng(42)
        return rng.normal(0.001, 0.008, size=500).tolist()

    def test_library_pbo_takes_precedence_and_labels_source(self) -> None:
        result = run_rigor_gate(
            strategy_id="s",
            daily_returns=self._returns(),
            num_trials=5,
            pbo_scores={"s": 0.9},  # cohort score would FAIL...
            library_pbo=0.2,  # ...but the library PBO (0.2) is the real input
            strategy_code="class S: def next(self): self.buy()",
        )
        assert result.pbo_source == "library"
        assert result.pbo_score == 0.2
        assert "source=library" in result.gate_details["pbo"]

    def test_falls_back_to_cohort_when_no_library_pbo(self) -> None:
        result = run_rigor_gate(
            strategy_id="s",
            daily_returns=self._returns(),
            num_trials=5,
            pbo_scores={"s": 0.2},
            strategy_code="class S: def next(self): self.buy()",
        )
        assert result.pbo_source == "cohort"
        assert result.pbo_score == 0.2
        assert "source=cohort" in result.gate_details["pbo"]

    def test_none_library_pbo_with_no_cohort_fails_closed(self) -> None:
        result = run_rigor_gate(
            strategy_id="s",
            daily_returns=self._returns(),
            num_trials=5,
            pbo_scores=None,
            library_pbo=None,
            strategy_code="class S: def next(self): self.buy()",
        )
        assert not result.passes_all
        assert result.gate_details["pbo"] == "MISSING (source=cohort)"


class TestBenjaminiHochbergFDR:
    """Tests for benjamini_hochberg_fdr — BH step-up procedure.

    Restored (#1185) after Tranche-1's dead-code excision deleted this class
    along with the function it tests (PR #1259 / commit c02d5fad, 2026-08-18) —
    #1185 had asked for the function to be WIRED IN three weeks before that
    deletion landed. See _rigor_helpers.benjamini_hochberg_fdr's docstring.
    """

    def test_all_significant(self):
        """Very small p-values should all be rejected."""
        pvalues = [0.001, 0.002, 0.003, 0.004, 0.005]
        result = benjamini_hochberg_fdr(pvalues, fdr_level=0.05)
        assert result["n_rejected"] == 5
        assert all(result["rejected"])

    def test_all_insignificant(self):
        """Large p-values should not be rejected."""
        pvalues = [0.80, 0.85, 0.90, 0.95]
        result = benjamini_hochberg_fdr(pvalues, fdr_level=0.05)
        assert result["n_rejected"] == 0
        assert not any(result["rejected"])

    def test_mixed_pvalues_correct_threshold(self):
        """BH threshold check: k/m * q at rank k."""
        # m=5, q=0.05 → thresholds [0.01, 0.02, 0.03, 0.04, 0.05]
        pvalues = [0.009, 0.019, 0.04, 0.06, 0.10]
        result = benjamini_hochberg_fdr(pvalues, fdr_level=0.05)
        # Sorted: 0.009 ≤ 0.01 ✓, 0.019 ≤ 0.02 ✓, 0.04 > 0.03 ✗ → k*=2
        assert result["n_rejected"] == 2

    def test_output_keys(self):
        result = benjamini_hochberg_fdr([0.01, 0.05, 0.10], fdr_level=0.05)
        for key in ("rejected", "bh_critical_values", "n_rejected", "adjusted_pvalues"):
            assert key in result

    def test_empty_input(self):
        result = benjamini_hochberg_fdr([], fdr_level=0.05)
        assert result == {"rejected": [], "bh_critical_values": [], "n_rejected": 0, "adjusted_pvalues": []}

    def test_adjusted_pvalues_monotone(self):
        """BH adjusted p-values should be monotone non-decreasing in sorted order."""
        rng = np.random.default_rng(5)
        pvalues = list(rng.uniform(0, 1, 20))
        result = benjamini_hochberg_fdr(pvalues, fdr_level=0.05)
        adj = result["adjusted_pvalues"]
        # All adjusted p-values must be in [0,1]
        assert all(0.0 <= p <= 1.0 for p in adj)
        # adjusted_pvalues is returned in original input order; re-order by
        # ascending p-value (BH's natural rank order) and check the
        # running-minimum step enforced non-decreasing values.
        sorted_adj = [a for _, a in sorted(zip(pvalues, adj, strict=True))]
        assert all(sorted_adj[i] <= sorted_adj[i + 1] for i in range(len(sorted_adj) - 1))


class TestComputeBoardLevelFdr:
    """Tests for rigor_evaluator.compute_board_level_fdr — the board-level
    orchestration layer (#1185): direction-converts DSR p-values then calls
    benjamini_hochberg_fdr. Distinct from TestBenjaminiHochbergFDR above (which
    tests the pure BH math on hand-supplied classical p-values) — these tests
    exercise the NEW conversion + cohort-assembly path this issue adds, with a
    known cohort of dsr_p_values chosen so the expected rejection set can be
    hand-verified against the BH step-up procedure.
    """

    def test_direction_conversion_low_dsr_p_is_significant_after_correction(self):
        """dsr_p_value uses P(true SR>0) convention (HIGH=confident); a strategy
        with dsr_p_value close to 1.0 must convert to a SMALL classical p-value
        and be far more likely to survive BH-FDR than one near 0.5 (weak
        evidence). This is the specific bug class a flipped conversion would
        produce silently (both directions are 'plausible-looking' floats in
        [0,1], so a reversed sign would not crash — it would just be wrong)."""
        # 5 strong strategies (dsr_p_value ~0.999, classical p ~0.001 → BH-significant)
        # + 5 weak strategies (dsr_p_value ~0.5, classical p ~0.5 → BH-insignificant)
        dsr_p_values = {f"strong_{i}": 0.999 for i in range(5)} | {f"weak_{i}": 0.5 for i in range(5)}
        result = compute_board_level_fdr(dsr_p_values, fdr_level=0.05)

        assert len(result) == 10
        for i in range(5):
            assert result[f"strong_{i}"]["board_fdr_significant"] is True
        for i in range(5):
            assert result[f"weak_{i}"]["board_fdr_significant"] is False

    def test_matches_hand_computed_bh_on_known_cohort(self):
        """Cross-check against a hand-verified BH step-up computation.

        classical_p = 1 - dsr_p_value for each, chosen with margin away from
        the exact BH threshold boundaries (an exact-boundary hit like
        classical_p == threshold is float-noise-sensitive: 1.0 - 0.99 !=
        0.01 in IEEE-754, so a case pinned exactly on the boundary can flip on
        floating-point error alone — this deliberately avoids that trap).
        classical_p: [0.005, 0.015, 0.028, 0.15, 0.50] (dsr_p_values: [0.995,
        0.985, 0.972, 0.85, 0.50]). m=5, q=0.05 → BH thresholds [0.01, 0.02,
        0.03, 0.04, 0.05]. Sorted classical p vs threshold: 0.005<=0.01 ✓,
        0.015<=0.02 ✓, 0.028<=0.03 ✓, 0.15>0.04 ✗, 0.50>0.05 ✗ → k*=3 rejected
        (the first three), each with margin >= 0.002 from its threshold.
        """
        dsr_p_values = {"a": 0.995, "b": 0.985, "c": 0.972, "d": 0.85, "e": 0.50}
        result = compute_board_level_fdr(dsr_p_values, fdr_level=0.05)

        assert result["a"]["board_fdr_significant"] is True
        assert result["b"]["board_fdr_significant"] is True
        assert result["c"]["board_fdr_significant"] is True
        assert result["d"]["board_fdr_significant"] is False
        assert result["e"]["board_fdr_significant"] is False

        # Cross-check directly against benjamini_hochberg_fdr on the same
        # manually-converted classical p-values, in the same order.
        ids = ["a", "b", "c", "d", "e"]
        classical = [1.0 - dsr_p_values[k] for k in ids]
        expected = benjamini_hochberg_fdr(classical, fdr_level=0.05)
        for i, k in enumerate(ids):
            assert result[k]["board_fdr_adjusted_p"] == pytest.approx(expected["adjusted_pvalues"][i])
            assert result[k]["board_fdr_significant"] == expected["rejected"][i]

    def test_none_and_non_finite_pvalues_excluded_not_fabricated(self):
        """A strategy with no backtest data (dsr_p_value=None) or a non-finite
        value must be OMITTED from the returned dict, not assigned a fabricated
        corrected value — mirrors the MISSING convention used everywhere else
        in the rigor gate."""
        dsr_p_values = {"has_data": 0.9, "missing": None, "nan_case": float("nan")}
        result = compute_board_level_fdr(dsr_p_values)
        assert set(result.keys()) == {"has_data"}

    def test_empty_cohort_returns_empty_dict(self):
        assert compute_board_level_fdr({}) == {}
        assert compute_board_level_fdr({"only": None}) == {}

    def test_saturated_dsr_p_value_is_floored_not_zero(self):
        """dsr_p_value == 1.0 (DSR rounding saturation — e.g. compute_dsr on a
        long, low-vol, confidently-positive series rounds to exactly 1.0) must
        NOT convert to a classical p-value of exactly 0.0: an impossible
        zero-probability certainty claim. It is floored at
        ``_CLASSICAL_P_FLOOR`` (1e-6) before BH-FDR runs (#1185 code review,
        2026-08-20), so ``board_fdr_adjusted_p`` / ``board_fdr_confidence``
        stay strictly inside (0.0, 1.0) rather than snapping to the endpoint.
        This is not hypothetical: TestBoardLevelFdrWiring's
        ``_strong_series(0)`` in test_selection_bias_routes.py hits this
        exact saturated case in the PR's own end-to-end test cohort."""
        dsr_p_values = {"saturated": 1.0, "b": 0.5, "c": 0.5}
        result = compute_board_level_fdr(dsr_p_values, fdr_level=0.05)

        assert result["saturated"]["board_fdr_significant"] is True
        assert 0.0 < result["saturated"]["board_fdr_adjusted_p"] < 1e-4
        assert 1.0 - 1e-4 < result["saturated"]["board_fdr_confidence"] < 1.0

    def test_board_level_correction_is_cohort_size_sensitive(self):
        """The defining property of a BOARD-level (not per-strategy) correction:
        the SAME strategy's verdict can change purely as a function of how many
        OTHER strategies are being simultaneously tested alongside it — unlike
        the per-strategy num_trials convention (#1075), which is deliberately
        NOT library-coupled (docs/adr/num-trials-self-containment.md). ``target``
        keeps the exact same dsr_p_value (0.98, classical p=0.02) in both
        cohorts; only the number of OTHER (individually insignificant,
        dsr_p_value=0.5 → classical p=0.5) strategies alongside it changes. In
        the 2-strategy cohort target's rank-1 BH threshold (q/m = 0.025) still
        clears its own p-value (0.02 <= 0.025), so it is significant. Diluted
        into a 31-strategy cohort, target's rank-1 threshold shrinks to
        q/31 ≈ 0.0016 — no longer clearing 0.02 — and no later, weaker rank
        rescues it (BH step-up: a rank is only rescued if some rank AT OR
        BELOW its own p-value clears its own threshold, and every filler's own
        p=0.5 never clears its own threshold, capped at q=0.05). So target
        flips to non-significant purely from cohort growth."""
        small_cohort = {"target": 0.98, "b": 0.5}  # classical p: [0.02, 0.5]
        large_cohort = {"target": 0.98} | {f"filler_{i}": 0.5 for i in range(30)}  # 31-strategy board

        small_result = compute_board_level_fdr(small_cohort, fdr_level=0.05)
        large_result = compute_board_level_fdr(large_cohort, fdr_level=0.05)

        assert small_result["target"]["board_fdr_significant"] is True
        assert large_result["target"]["board_fdr_significant"] is False
        # The raw per-strategy DSR p-value (0.98, i.e. classical p=0.02) never
        # changed — only the board it was evaluated against did.
        assert large_result["target"]["board_fdr_adjusted_p"] > small_result["target"]["board_fdr_adjusted_p"]


class TestPboPowerFloor:
    """#819: below MIN_LIBRARY_N_FOR_PBO_GATING, PBO must NOT_RUN (not gate) —
    still computed/surfaced, but criterion 4 becomes neutral rather than a
    hard pass/fail. At or above the floor, behavior is exactly as before."""

    @staticmethod
    def _returns() -> list[float]:
        rng = np.random.default_rng(7)
        return rng.normal(0.001, 0.008, size=500).tolist()

    @staticmethod
    def _strong_returns() -> list[float]:
        """Deterministic alternating +0.3%/+0.1% daily returns — clears DSR
        (excess Sharpe ~1.8/bar) and the OOS/IS cliff (identical stats in both
        slices) comfortably, so a below-floor test can assert an end-to-end
        passes_all=True is caused by the PBO fix, not an incidental DSR/OOS
        pass on a noisy series."""
        return [0.003 if i % 2 == 0 else 0.001 for i in range(500)]

    def test_below_floor_high_pbo_does_not_fail_criterion_4(self) -> None:
        """A pbo_score >= 0.5 would normally fail passes_all — below the floor
        it must not, since the statistic isn't powered enough to gate on."""
        below = MIN_LIBRARY_N_FOR_PBO_GATING - 1
        r = RigorGateResult(
            "s",
            dsr_p_value=0.97,
            pbo_score=0.9,
            pbo_library_size=below,
            oos_sharpe=1.0,
            in_sample_sharpe=1.5,
            look_ahead_passed=True,
        )
        assert r.passes_all is True
        assert r.gate_details["pbo"].startswith("NOT_RUN")
        assert f"N={below}" in r.gate_details["pbo"]
        assert f"below the CSCV power floor of {MIN_LIBRARY_N_FOR_PBO_GATING}" in r.gate_details["pbo"]
        assert "PBO=0.9000" in r.gate_details["pbo"]  # still surfaced, advisory

    def test_below_floor_missing_pbo_does_not_fail_criterion_4(self) -> None:
        """Same neutrality when there's no PBO number at all below the floor."""
        below = MIN_LIBRARY_N_FOR_PBO_GATING - 1
        r = RigorGateResult(
            "s",
            dsr_p_value=0.97,
            pbo_score=None,
            pbo_library_size=below,
            oos_sharpe=1.0,
            in_sample_sharpe=1.5,
            look_ahead_passed=True,
        )
        assert r.passes_all is True
        assert (
            r.gate_details["pbo"] == f"NOT_RUN (N={below} below the CSCV power floor of {MIN_LIBRARY_N_FOR_PBO_GATING})"
        )

    def test_at_floor_high_pbo_still_fails(self) -> None:
        """Exactly at the floor, gating is unchanged from pre-#819 behavior."""
        r = RigorGateResult(
            "s",
            dsr_p_value=0.97,
            pbo_score=0.9,
            pbo_library_size=MIN_LIBRARY_N_FOR_PBO_GATING,
            oos_sharpe=1.0,
            in_sample_sharpe=1.5,
            look_ahead_passed=True,
        )
        assert r.passes_all is False
        assert r.gate_details["pbo"] == "FAIL (PBO=0.9000, need < 0.50, source=cohort)"

    def test_above_floor_low_pbo_still_passes(self) -> None:
        r = RigorGateResult(
            "s",
            dsr_p_value=0.97,
            pbo_score=0.2,
            pbo_library_size=MIN_LIBRARY_N_FOR_PBO_GATING + 5,
            oos_sharpe=1.0,
            in_sample_sharpe=1.5,
            look_ahead_passed=True,
        )
        assert r.passes_all is True
        assert r.gate_details["pbo"] == "PASS (PBO=0.2000, source=cohort)"

    def test_pbo_library_size_none_is_pre_819_behavior(self) -> None:
        """The default (no pbo_library_size given at all) must reproduce the
        exact pre-#819 gating — a direct RigorGateResult construction that
        doesn't know about the floor stays on the old hard PASS/FAIL/MISSING."""
        r = RigorGateResult("s", dsr_p_value=0.97, pbo_score=0.9, oos_sharpe=1.0, look_ahead_passed=True)
        assert r.passes_all is False
        assert r.gate_details["pbo"] == "FAIL (PBO=0.9000, need < 0.50, source=cohort)"

    def test_run_rigor_gate_auto_derives_library_size_from_cohort_dict(self) -> None:
        """The common case needs no caller change: run_rigor_gate derives N from
        len(pbo_scores) on the cohort path, so an existing small-library caller
        gets the floor fix automatically."""
        below = MIN_LIBRARY_N_FOR_PBO_GATING - 1
        pbo_scores = {f"s{i}": 0.9 for i in range(below)}  # len == below, all "failing" PBO
        result = run_rigor_gate(
            strategy_id="s0",
            daily_returns=self._strong_returns(),
            num_trials=5,
            pbo_scores=pbo_scores,
            look_ahead_audit_passed=True,
        )
        assert result.pbo_library_size == below
        assert result.gate_details["pbo"].startswith("NOT_RUN")
        assert result.passes_all is True  # would have FAILed on PBO=0.9 pre-#819

    def test_run_rigor_gate_at_floor_cohort_dict_still_gates(self) -> None:
        pbo_scores = {f"s{i}": 0.9 for i in range(MIN_LIBRARY_N_FOR_PBO_GATING)}
        result = run_rigor_gate(
            strategy_id="s0",
            daily_returns=self._returns(),
            num_trials=5,
            pbo_scores=pbo_scores,
            strategy_code="class S: def next(self): self.buy()",
        )
        assert result.pbo_library_size == MIN_LIBRARY_N_FOR_PBO_GATING
        assert result.passes_all is False
        assert "FAIL" in result.gate_details["pbo"]

    def test_run_rigor_gate_explicit_pbo_library_size_overrides_auto_derivation(self) -> None:
        """An explicit pbo_library_size wins over the len(pbo_scores) guess —
        needed once the library-PBO path (#546) wants to gate on the floor too,
        since pbo_scores's length isn't that cross-section's N."""
        result = run_rigor_gate(
            strategy_id="s",
            daily_returns=self._returns(),
            num_trials=5,
            library_pbo=0.9,
            pbo_library_size=MIN_LIBRARY_N_FOR_PBO_GATING - 1,
            strategy_code="class S: def next(self): self.buy()",
        )
        assert result.pbo_source == "library"
        assert result.pbo_library_size == MIN_LIBRARY_N_FOR_PBO_GATING - 1
        assert result.gate_details["pbo"].startswith("NOT_RUN")

    def test_run_rigor_gate_library_pbo_path_does_not_auto_derive_from_cohort_dict(self) -> None:
        """When library_pbo is supplied, pbo_scores's length must NOT be used as
        a stand-in library size — that dict describes a different (cohort)
        cross-section. Without an explicit pbo_library_size, the floor must not
        apply here even if pbo_scores happens to be small."""
        result = run_rigor_gate(
            strategy_id="s",
            daily_returns=self._returns(),
            num_trials=5,
            pbo_scores={"other": 0.9},  # len 1 — must NOT be read as N=1 for library_pbo
            library_pbo=0.9,
            strategy_code="class S: def next(self): self.buy()",
        )
        assert result.pbo_source == "library"
        assert result.pbo_library_size is None
        assert result.passes_all is False  # gates normally: PBO=0.9 >= 0.5, no floor applies
        assert result.gate_details["pbo"] == "FAIL (PBO=0.9000, need < 0.50, source=library)"


# ─── Daily-returns store loader (#546) ───────────────────────────────


class TestLoadDailyReturnsStore:
    """load_daily_returns_store: read the store into compute_library_pbo's shape,
    surface the max data_vintage, and degrade gracefully (never raise). #774:
    DB-backed (strategy_daily_returns table) — a tmp, isolated SQLite session
    stands in for production, passed explicitly so no shared-engine
    monkeypatching of archimedes.db is needed."""

    @staticmethod
    def _session(tmp_path):
        from archimedes.db import Base
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        engine = create_engine(
            f"sqlite:///{tmp_path / 'daily_returns_test.db'}", connect_args={"check_same_thread": False}
        )
        Base.metadata.create_all(bind=engine)
        return sessionmaker(bind=engine)()

    @staticmethod
    def _insert(session, stem: str, *, vintage: str | None, n: int = 4) -> None:
        from datetime import date as date_cls

        from archimedes.models.daily_returns_store import StrategyDailyReturn

        for i in range(n):
            session.add(
                StrategyDailyReturn(
                    stem=stem,
                    date=date_cls(2020, 1, 1 + i),
                    daily_return=0.001 * (i + 1),
                    data_vintage=vintage,
                )
            )
        session.commit()

    def test_loads_shape_and_max_vintage(self, tmp_path) -> None:
        session = self._session(tmp_path)
        self._insert(session, "alpha", vintage="2026-06-10")
        self._insert(session, "beta", vintage="2026-06-11")
        self._insert(session, "gamma", vintage="2026-06-09")

        store, vintage = load_daily_returns_store(session=session)

        assert set(store.keys()) == {"alpha", "beta", "gamma"}
        # Each entry projected onto exactly {dates, daily_returns}.
        for rec in store.values():
            assert set(rec.keys()) == {"dates", "daily_returns"}
            assert len(rec["dates"]) == len(rec["daily_returns"]) == 4
        # Max vintage across stems.
        assert vintage == "2026-06-11"

    def test_empty_table_returns_empty_no_raise(self, tmp_path) -> None:
        session = self._session(tmp_path)
        store, vintage = load_daily_returns_store(session=session)
        assert store == {}
        assert vintage is None

    def test_db_read_failure_degrades_gracefully(self) -> None:
        """Mirrors the pre-#774 'never raise' contract: a broken session (DB
        unreachable, table missing) must degrade to ({}, None), not raise."""
        from unittest.mock import Mock

        broken_session = Mock()
        broken_session.query.side_effect = RuntimeError("connection lost")

        store, vintage = load_daily_returns_store(session=broken_session)
        assert store == {}
        assert vintage is None

    def test_record_without_vintage_yields_none_vintage(self, tmp_path) -> None:
        session = self._session(tmp_path)
        self._insert(session, "novintage", vintage=None, n=2)

        store, vintage = load_daily_returns_store(session=session)
        assert set(store.keys()) == {"novintage"}
        assert vintage is None

    def test_default_session_falls_back_to_get_session(self, tmp_path, monkeypatch) -> None:
        """No explicit session → uses archimedes.db.get_session() (production
        default). Verified by patching get_session to return our tmp session."""
        import contextlib

        import archimedes.db as db_module

        session = self._session(tmp_path)
        self._insert(session, "solo", vintage="2026-07-03")

        @contextlib.contextmanager
        def _fake_get_session():
            yield session

        monkeypatch.setattr(db_module, "get_session", _fake_get_session)

        store, vintage = load_daily_returns_store()
        assert set(store.keys()) == {"solo"}
        assert vintage == "2026-07-03"


# ─── G5 (audit 2026-08-18): the adversarial end-to-end rejection ─────


class TestAdversarialOverfitRejection:
    """A deliberately-overfit strategy must be rejected END-TO-END by the full
    gate — no hand-built verdicts, no pre-chosen numbers.

    The named repeat failure class: guards need an adversarial pass. Every
    prior composite-gate test either constructed the RigorGateResult directly
    with chosen numbers or fed i.i.d. noise never designed to fool the gate.
    Mutation-verified against the trivial acceptor: hardcoding
    ``_passes_profile`` to True fails the rejection test here while the
    honest-twin test still passes.
    """

    @staticmethod
    def _overfit_returns() -> list[float]:
        """The classic overfit signature: stellar in-sample (the first 70% the
        gate grades as IS when ``in_sample_sharpe=None``), collapse in the
        out-of-sample tail."""
        rng = np.random.default_rng(7)
        n = 500
        is_len = int(n * 0.7)
        is_part = rng.normal(0.0030, 0.006, size=is_len)  # ann. Sharpe ≫ 2
        oos_part = rng.normal(-0.0004, 0.010, size=n - is_len)  # collapse
        return np.concatenate([is_part, oos_part]).tolist()

    def test_overfit_series_is_rejected_on_statistics_not_technicalities(self) -> None:
        result = run_rigor_gate(
            strategy_id="overfit_probe",
            daily_returns=self._overfit_returns(),
            num_trials=40,  # its own selection pool: reported best-of-40
            pbo_scores={"overfit_probe": 0.2},  # PBO satisfiable — not the rejector
            look_ahead_audit_passed=True,  # look-ahead clean: statistics must do the work
        )

        assert result.passes_all is False
        assert result.min_passing_level is None  # unrescuable by loosening the slider
        # Attribution — the rejection must be STATISTICAL, not a technicality:
        details = result.gate_details
        assert details["look_ahead"] == "PASS"  # not rejected on the code audit
        assert "MISSING" not in details["dsr"]  # not rejected on absent inputs
        assert details["oos_sharpe"] != "MISSING"
        # The overfit shape itself is what fails: the OOS collapse trips the
        # always-on floor and/or the OOS/IS cliff, exactly as designed.
        assert result.oos_sharpe is not None and result.in_sample_sharpe is not None
        assert result.oos_sharpe < result.in_sample_sharpe
        assert result.blocked_by_floor or details["oos_sharpe"].startswith("FAIL")

    def test_honest_twin_is_not_rejected_by_the_same_legs(self) -> None:
        """Anti-vacuity: a stationary series with the same overall character
        must NOT trip the overfit rejectors — proving the test above detects
        the overfit SHAPE, not that the gate rejects everything it is fed.

        Seed 42, not 7: seed 7's raw noise happens to draw a genuinely weak
        sample (its own OOS tail annualizes to −1.4 — honestly rejectable),
        which would make this anti-vacuity check assert the wrong thing."""
        rng = np.random.default_rng(42)
        honest = rng.normal(0.001, 0.008, size=500).tolist()

        result = run_rigor_gate(
            strategy_id="honest_twin",
            daily_returns=honest,
            num_trials=1,  # single-configuration honesty
            pbo_scores={"honest_twin": 0.2},
            look_ahead_audit_passed=True,
        )

        # No claim that it PASSES the strictest badge bar (that depends on
        # thresholds owned by the quant lane) — only that the overfit
        # rejectors specifically do not fire on a stationary series.
        assert result.blocked_by_floor is False
        assert not result.gate_details["oos_sharpe"].startswith("FAIL")
