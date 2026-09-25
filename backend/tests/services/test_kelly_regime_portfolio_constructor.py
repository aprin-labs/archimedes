"""Tests for KellyRegimePortfolioConstructor (issue #1264).

Hermetic: no env, no network, no DB, no chain. Pure sync computation over the
frozen `IPortfolioConstructor` interface.

Per this repo's test convention, every core assertion below is proven
discriminating: for the two properties #1264 explicitly calls out (regime
tilt, Kelly-not-equal-weight sizing) there is a companion test that runs a
deliberately broken variant through the SAME assertion and shows it fails —
so a future regression that silently drops the regime tilt or falls back to
equal-weighting cannot pass this suite by accident. This mirrors the gap the
2026-08-18 test audit found in `strategy_guardrail`'s tests (floor assertions
that passed vacuously because every floor was 0.0; that module and its tests
have since been deleted as a zero-caller surface, so this is a record of the
lesson, not a live pointer) — every assertion here
compares against a concrete, nonzero, hand-computed expected value.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from archimedes.models.backtest import BacktestResult
from archimedes.models.portfolio import RISK_PROFILE_PARAMS, RiskProfile
from archimedes.models.regime import Regime, RegimeClassification, RegimeSignals
from archimedes.services.portfolio_constructor import (
    REGIME_CONVENTION_APPLIED,
    REGIME_CONVENTION_NEUTRAL_NO_FEED,
    SAFE_ASSET,
    KellyRegimePortfolioConstructor,
)
from archimedes.services.strategy_signal_evaluator import AssetSignal, Signal, StrategySignals
from archimedes.services.strategy_sizer import (
    kelly_multiplier,
    kelly_weighted_allocations,
    scale_to_budget,
    size_strategies,
)

# ── helpers (mirrors test_strategy_sizer.py's minimal stand-ins) ──────────


@dataclass
class _Strat:
    """Minimal stand-in carrying only the fields size_strategies() reads."""

    id: str
    passes_rigor_gate: bool = False
    kelly_fraction: float | None = None


def _signals(strategy_id: str, votes: dict[str, float]) -> StrategySignals:
    return StrategySignals(
        strategy_id=strategy_id,
        strategy_name=strategy_id,
        paper_title=strategy_id,
        signals=[
            AssetSignal(
                strategy_id=strategy_id,
                strategy_name=strategy_id,
                asset=asset,
                signal=Signal.LONG if w > 0 else Signal.FLAT,
                weight=w,
                reason="test",
            )
            for asset, w in votes.items()
        ],
    )


def _regime_signals() -> RegimeSignals:
    return RegimeSignals(
        vix_level=18.0,
        vix_rate_of_change=0.0,
        sp500_above_ma50=True,
        sp500_above_ma200=True,
    )


def _regime(regime: Regime, confidence: float) -> RegimeClassification:
    return RegimeClassification(
        regime=regime,
        confidence=confidence,
        signals=_regime_signals(),
        timestamp=datetime.now(UTC),
    )


def _backtest(strategy_id: str, sharpe: float, dsr: float | None = None) -> BacktestResult:
    return BacktestResult(
        strategy_id=strategy_id,
        sharpe_ratio=sharpe,
        sortino_ratio=sharpe * 0.8,
        max_drawdown=0.15,
        cagr=0.15,
        calmar_ratio=1.0,
        win_rate=0.55,
        profit_factor=1.3,
        total_trades=100,
        avg_holding_period_days=7.0,
        correlation_to_spy=0.7,
        correlation_to_btc=0.2,
        deflated_sharpe_ratio=dsr,
    )


@pytest.fixture
def ctor() -> KellyRegimePortfolioConstructor:
    return KellyRegimePortfolioConstructor()


# ── Regime tilt: real assets shrink in CRISIS, pass through when scale=1.0 ──


def test_crisis_regime_shrinks_risk_assets_into_safe_asset(ctor: KellyRegimePortfolioConstructor) -> None:
    base = {"sTSLA": 0.6, "sBTC": 0.2, SAFE_ASSET: 0.2}
    allocs = ctor.construct(
        risk_profile=RiskProfile.MODERATE,
        strategies=[],
        backtest_results={},
        regime=_regime(Regime.CRISIS, 1.0),
        base_weights=base,
    )
    by_symbol = {a.symbol: a.weight for a in allocs}

    assert sum(by_symbol.values()) == pytest.approx(1.0)
    # Concrete, nonzero, hand-computed expectation (CRISIS mult=0.1, conf=1.0
    # → scale=0.1): sTSLA 0.6*0.1=0.06, sBTC 0.2*0.1=0.02, SAFE_ASSET absorbs
    # the freed 0.72 on top of its own 0.2 = 0.92.
    assert by_symbol["sTSLA"] == pytest.approx(0.06)
    assert by_symbol["sBTC"] == pytest.approx(0.02)
    assert by_symbol[SAFE_ASSET] == pytest.approx(0.92)


def test_regime_blind_variant_fails_the_crisis_shrink_assertion(ctor: KellyRegimePortfolioConstructor) -> None:
    """Discriminating companion to the test above.

    A constructor that silently ignores `regime` (returns a constant neutral
    scale) must fail the CRISIS-shrink assertion — proving that assertion
    actually exercises the tilt rather than passing regardless of the input.
    """

    class RegimeBlindConstructor(KellyRegimePortfolioConstructor):
        def compute_position_scale(self, regime, ensemble_consensus) -> float:
            return 1.0  # the bug: regime is never consulted

    blind = RegimeBlindConstructor()
    base = {"sTSLA": 0.6, "sBTC": 0.2, SAFE_ASSET: 0.2}
    allocs = blind.construct(
        risk_profile=RiskProfile.MODERATE,
        strategies=[],
        backtest_results={},
        regime=_regime(Regime.CRISIS, 1.0),
        base_weights=base,
    )
    by_symbol = {a.symbol: a.weight for a in allocs}

    with pytest.raises(AssertionError):
        assert by_symbol["sTSLA"] == pytest.approx(0.06)
    # And the blind variant's actual (wrong) output is the untouched input.
    assert by_symbol["sTSLA"] == pytest.approx(0.6)


def test_regime_none_is_neutral_byte_identical_pass_through(ctor: KellyRegimePortfolioConstructor) -> None:
    """The derive-allocations endpoint's real default (see class docstring):
    with no regime signal, weights pass through unchanged — not shrunk by the
    execution-society's conservative REGIME_MULTIPLIER_NONE. Deliberately
    non-round numbers so a stray renormalize/rounding pass would be visible.
    """
    base = {"sSPY": 0.37219, SAFE_ASSET: 0.62781}
    allocs = ctor.construct(
        risk_profile=RiskProfile.MODERATE,
        strategies=[],
        backtest_results={},
        regime=None,
        base_weights=base,
    )
    by_symbol = {a.symbol: a.weight for a in allocs}
    assert by_symbol == base


# ── Kelly sizing: proportional to kelly_fraction, never equal-weight ────────


def test_kelly_sizing_is_proportional_to_kelly_fraction_not_equal_weight(
    ctor: KellyRegimePortfolioConstructor,
) -> None:
    strategies = [
        _Strat("big", passes_rigor_gate=True, kelly_fraction=0.30),
        _Strat("small", passes_rigor_gate=True, kelly_fraction=0.05),
    ]
    allocs = ctor.construct(
        risk_profile=RiskProfile.MODERATE,
        strategies=strategies,
        backtest_results={},
        regime=None,
    )
    by_id = {a.symbol: a.weight for a in allocs}

    mult = kelly_multiplier("moderate")  # 2/3
    # size_strategies() rounds to 6dp, so a tight absolute tolerance (not the
    # default relative one, which is too strict for a number this small).
    assert by_id["big"] == pytest.approx(0.30 * mult, abs=1e-6)
    assert by_id["small"] == pytest.approx(0.05 * mult, abs=1e-6)
    # Nonzero and unequal — the whole point of Kelly sizing over equal-weight.
    assert by_id["big"] > 0.0
    assert by_id["small"] > 0.0
    assert by_id["big"] != pytest.approx(by_id["small"])
    assert by_id["big"] > by_id["small"]


def test_equal_weight_variant_fails_the_kelly_proportionality_assertion() -> None:
    """Discriminating companion: an equal-weight scheme over the same two
    strategies gives IDENTICAL shares, which is exactly the naive behavior
    #1264 / strategy_sizer's Kelly sizing was built to replace (see
    strategy_sizer.py's module docstring). It must fail the real assertion.
    """
    strategies = [
        _Strat("big", passes_rigor_gate=True, kelly_fraction=0.30),
        _Strat("small", passes_rigor_gate=True, kelly_fraction=0.05),
    ]
    investable = 1.0 - RISK_PROFILE_PARAMS[RiskProfile.MODERATE]["usyc_floor"]
    equal_weight = {s.id: investable / len(strategies) for s in strategies}

    mult = kelly_multiplier("moderate")
    with pytest.raises(AssertionError):
        assert equal_weight["big"] == pytest.approx(0.30 * mult)
    with pytest.raises(AssertionError):
        assert equal_weight["big"] != pytest.approx(equal_weight["small"])


def test_gate_failing_strategy_gets_no_capital_gate_passer_does(
    ctor: KellyRegimePortfolioConstructor,
) -> None:
    strategies = [
        _Strat("passer", passes_rigor_gate=True, kelly_fraction=0.30),
        # A huge kelly_fraction that would dominate the book if the gate were
        # bypassed — sizing must not become a side-door past the rigor gate.
        _Strat("failer", passes_rigor_gate=False, kelly_fraction=0.90),
    ]
    allocs = ctor.construct(
        risk_profile=RiskProfile.MODERATE,
        strategies=strategies,
        backtest_results={},
        regime=None,
    )
    by_id = {a.symbol: a.weight for a in allocs}

    assert by_id.get("failer", 0.0) == 0.0
    assert by_id["passer"] > 0.0
    assert by_id["passer"] == pytest.approx(0.30 * kelly_multiplier("moderate"))


def test_fallback_weights_use_profile_usyc_floor(ctor: KellyRegimePortfolioConstructor) -> None:
    """base_weights absent → profile's own usyc_floor is the investable ceiling."""
    strategies = [_Strat("s1", passes_rigor_gate=True, kelly_fraction=0.30)]
    allocs = ctor.construct(
        risk_profile=RiskProfile.CONSERVATIVE,  # usyc_floor=0.40
        strategies=strategies,
        backtest_results={},
        regime=None,
    )
    by_id = {a.symbol: a.weight for a in allocs}
    expected = 0.30 * kelly_multiplier("conservative")
    assert expected > 0.0
    assert by_id["s1"] == pytest.approx(expected)
    assert by_id[SAFE_ASSET] == pytest.approx(1.0 - expected)


# ── score_strategy: DSR-preferred, matches PortfolioConstructor's rule ──────


def test_score_strategy_prefers_dsr_over_sharpe(ctor: KellyRegimePortfolioConstructor) -> None:
    result = _backtest("s1", sharpe=1.0, dsr=0.42)
    score = ctor.score_strategy(strategy=None, result=result, risk_profile=RiskProfile.MODERATE)  # type: ignore[arg-type]
    assert score == pytest.approx(0.42)
    assert score != pytest.approx(1.0)


def test_score_strategy_falls_back_to_sharpe_without_dsr(ctor: KellyRegimePortfolioConstructor) -> None:
    result = _backtest("s1", sharpe=1.25, dsr=None)
    score = ctor.score_strategy(strategy=None, result=result, risk_profile=RiskProfile.MODERATE)  # type: ignore[arg-type]
    assert score == pytest.approx(1.25)


# ── Behavior-compatibility proof: old strategy_sizer pipeline vs new construct() ──


def test_construct_reproduces_old_strategy_sizer_pipeline_on_fixed_fixture(
    ctor: KellyRegimePortfolioConstructor,
) -> None:
    """#1264's wire-up requirement: the pre-existing strategy_sizer-only
    pipeline (size_strategies -> scale_to_budget -> kelly_weighted_allocations,
    exactly as derive_vault_allocations called it before this change) routed
    through construct() with the endpoint's real regime=None default must be
    byte-identical to the raw pipeline output, on a fixed multi-strategy,
    multi-asset fixture with a gate-failer in the mix.
    """
    strategies = [
        _Strat("s1", passes_rigor_gate=True, kelly_fraction=0.30),
        _Strat("s2", passes_rigor_gate=True, kelly_fraction=0.15),
        _Strat("s3", passes_rigor_gate=False, kelly_fraction=0.90),  # gate-failer
    ]
    signals = [
        _signals("s1", {"sSPY": 1.0}),
        _signals("s2", {"sGOLD": 0.6, "sTLT": 0.4}),
        _signals("s3", {"sSPY": 1.0}),
    ]
    usdc_floor = 0.20

    # OLD pipeline — exactly what derive_vault_allocations called before #1264.
    old_sized = size_strategies(strategies, "moderate")
    old_sized = scale_to_budget(old_sized, 1.0 - usdc_floor)
    old_weights = kelly_weighted_allocations(signals, old_sized, usdc_floor=usdc_floor)

    # Sanity: fixture actually exercises real, nonzero sizing (not vacuous).
    assert old_weights["sSPY"] > 0.0
    assert old_weights["sGOLD"] > 0.0
    assert old_weights["sTLT"] > 0.0

    # NEW — same old_weights routed through construct() as base_weights, with
    # the endpoint's actual regime=None default.
    allocs = ctor.construct(
        risk_profile=RiskProfile.MODERATE,
        strategies=strategies,
        backtest_results={},
        regime=None,
        ensemble_consensus=None,
        base_weights=old_weights,
    )
    new_weights = {a.symbol: a.weight for a in allocs}

    assert new_weights == old_weights


# ── Regime provenance (#1264 review) ──────────────────────────────────────


def test_regime_convention_tracks_whether_the_tilt_actually_applied() -> None:
    """The disclosed convention must agree with the scale actually used.

    `regime=None` yields scale 1.0 here (the documented divergence from the
    execution society's conservative 0.7), so a caller cannot tell an inert
    tilt apart from a benign regime that happened to score 1.0. The response
    marker exists to make that difference visible, and it is only worth
    anything if it is derived from the SAME input the arithmetic used --
    the `rf_convention` lesson from #1409, applied here.
    """
    c = KellyRegimePortfolioConstructor()

    assert c.compute_position_scale(None, None) == 1.0
    assert c.regime_convention(None) == REGIME_CONVENTION_NEUTRAL_NO_FEED

    crisis = _regime(Regime.CRISIS, confidence=0.9)
    assert c.compute_position_scale(crisis, None) < 1.0
    assert c.regime_convention(crisis) == REGIME_CONVENTION_APPLIED


def test_regime_convention_says_applied_even_when_the_tilt_scores_neutral() -> None:
    """A real regime that happens to produce scale 1.0 must NOT read as
    'no feed'. This is the case the marker exists to separate: same weights
    out, materially different provenance."""
    c = KellyRegimePortfolioConstructor()

    # RISK_ON's multiplier IS 1.0, so at full confidence this produces the
    # identical scale the no-feed path produces. Identical weights out, and
    # the only thing separating them is the marker.
    risk_on = _regime(Regime.RISK_ON, confidence=1.0)
    assert c.compute_position_scale(risk_on, None) == c.compute_position_scale(None, None)
    assert c.regime_convention(risk_on) == REGIME_CONVENTION_APPLIED
    assert c.regime_convention(None) == REGIME_CONVENTION_NEUTRAL_NO_FEED
