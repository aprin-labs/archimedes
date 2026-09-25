"""Tests for the DSL → backtrader interpreter."""

from __future__ import annotations

from itertools import pairwise

import pytest
from archimedes.services.dsl_to_backtrader import (
    _eval_condition,
    interpret_spec,
    interpret_variant,
)
from archimedes.services.strategy_dsl import (
    FABER_2007_SPEC,
    validate_strategy_spec,
)


class TestInterpretsReferenceExamples:
    """Interpreter must produce valid backtrader.Strategy subclasses."""

    @pytest.fixture
    def faber_spec(self):
        return validate_strategy_spec(FABER_2007_SPEC)

    def test_interprets_reference_examples(self, faber_spec):
        cls = interpret_spec(faber_spec)
        assert cls is not None
        assert issubclass(cls, _get_bt_strategy_class())
        assert cls.__name__.startswith("DSL_")

    def test_strategy_has_params(self, faber_spec):
        cls = interpret_spec(faber_spec)
        params_dict = dict(cls.params._getitems())
        assert "dsl_spec" in params_dict
        assert params_dict["dsl_spec"]["name"] == faber_spec.name

    def test_faber_class_name(self, faber_spec):
        cls = interpret_spec(faber_spec)
        assert "SMA_200" in cls.__name__ or "Tactical" in cls.__name__


class TestLegacyLookAheadDeclarationIsIgnored:
    """The interpreter no longer gates on a self-declared look-ahead flag.

    It used to refuse any spec carrying ``look_ahead_safe: false`` — which made
    the mirror-image claim, that ``true`` meant something, look load-bearing. A
    persisted legacy spec must now interpret in either direction, and the
    interpreted class must carry no trace of the declaration.
    """

    @pytest.mark.parametrize("declared", [True, False])
    def test_legacy_declaration_neither_blocks_nor_reaches_the_strategy_class(self, declared):
        legacy = {**FABER_2007_SPEC, "look_ahead_safe": declared}
        spec = validate_strategy_spec(legacy)
        cls = interpret_spec(spec)
        assert not hasattr(spec, "look_ahead_safe")
        assert not hasattr(cls, "look_ahead_safe")

    def test_variant_interpretation_carries_no_declaration(self):
        """interpret_variant rebuilds a StrategySpec by hand — the removed field
        must not creep back in through that constructor.
        """
        spec = validate_strategy_spec({**FABER_2007_SPEC, "look_ahead_safe": True})
        cls = interpret_variant(spec, {"sma_200": 150})
        assert not hasattr(cls, "look_ahead_safe")


class TestConditionEvaluation:
    """Condition tree evaluation logic."""

    def test_gt_true(self):
        assert _eval_condition({"gt": ["close", 100]}, {"close": 110}) is True

    def test_gt_false(self):
        assert _eval_condition({"gt": ["close", 100]}, {"close": 90}) is False

    def test_lt(self):
        assert _eval_condition({"lt": ["close", 100]}, {"close": 90}) is True

    def test_gte(self):
        assert _eval_condition({"gte": ["close", 100]}, {"close": 100}) is True

    def test_lte(self):
        assert _eval_condition({"lte": ["close", 100]}, {"close": 100}) is True

    def test_and(self):
        cond = {
            "and": [
                {"gt": ["close", 100]},
                {"lt": ["close", 200]},
            ]
        }
        assert _eval_condition(cond, {"close": 150}) is True
        assert _eval_condition(cond, {"close": 50}) is False

    def test_or(self):
        cond = {
            "or": [
                {"gt": ["close", 200]},
                {"lt": ["close", 50]},
            ]
        }
        assert _eval_condition(cond, {"close": 30}) is True
        assert _eval_condition(cond, {"close": 100}) is False

    def test_not(self):
        cond = {"not": {"gt": ["close", 100]}}
        assert _eval_condition(cond, {"close": 90}) is True
        assert _eval_condition(cond, {"close": 110}) is False


def _get_bt_strategy_class():
    """Import backtrader and return the Strategy base class."""
    import backtrader as bt

    return bt.Strategy


def _record_rebalance_bars(cls, n_bars: int) -> list[int]:
    """Run ``cls`` over ``n_bars`` of synthetic data, returning rebalance bar indices.

    Wraps the generated strategy's ``_should_rebalance`` so that every bar where it
    returns True records ``len(self)`` (backtrader's 1-indexed bar count). This lets
    the test observe the rebalance cadence without depending on order fills.
    """
    import backtrader as bt
    import pandas as pd

    fired: list[int] = []
    original = cls._should_rebalance

    def _recording_should_rebalance(self):
        result = original(self)
        if result:
            fired.append(len(self))
        return result

    cls._should_rebalance = _recording_should_rebalance
    try:
        # Monotone-rising close keeps entry (close > sma) true so rebalances that
        # act do so consistently; the cadence itself is what we measure.
        dates = pd.date_range("2020-01-01", periods=n_bars, freq="D")
        prices = [100.0 + i for i in range(n_bars)]
        frame = pd.DataFrame(
            {
                "open": prices,
                "high": [p + 0.5 for p in prices],
                "low": [p - 0.5 for p in prices],
                "close": prices,
                "volume": [1000] * n_bars,
            },
            index=dates,
        )
        cerebro = bt.Cerebro()
        cerebro.adddata(bt.feeds.PandasData(dataname=frame))
        cerebro.addstrategy(cls)
        cerebro.broker.setcash(100000.0)
        cerebro.run()
    finally:
        cls._should_rebalance = original
    return fired


class TestRebalanceCadencePhase:
    """The first rebalance must land right after warmup, not a full period later."""

    def _monthly_sma21_spec(self):
        # sma_21 -> warmup == 21; monthly -> period == 21. Before the phase-shift
        # fix the counter started at 0, so the first rebalance fired ~21 bars after
        # warmup ended (~bar 42) instead of on the first post-warmup bar (~bar 21).
        spec_dict = {
            "name": "SMA-21 Monthly",
            "asset_universe": ["SPY"],
            "rebalance_frequency": "monthly",
            "entry": {"gt": ["close", "sma_21"]},
            "exit": {"lt": ["close", "sma_21"]},
            "position_sizing": {"type": "full_invested_when_in_market"},
            "source_arxiv_ids": ["0706.1497"],
            "look_ahead_safe": True,
        }
        return validate_strategy_spec(spec_dict)

    def test_first_rebalance_near_warmup_not_double(self):
        cls = interpret_spec(self._monthly_sma21_spec())
        fired = _record_rebalance_bars(cls, n_bars=252)

        assert fired, "expected at least one rebalance over a 252-bar year"
        first = fired[0]
        # First rebalance should be the first post-warmup bar (~22), well below the
        # phase-shifted ~42 the old counter produced.
        assert first <= 30, f"first rebalance at bar {first}, expected near warmup (~22)"
        assert first < 40, f"first rebalance at bar {first} shows the period-1 phase shift"

    def test_steady_state_cadence_preserved(self):
        cls = interpret_spec(self._monthly_sma21_spec())
        fired = _record_rebalance_bars(cls, n_bars=252)

        assert len(fired) >= 3, "need several rebalances to check cadence"
        gaps = [b - a for a, b in pairwise(fired)]
        # Every rebalance after the first stays on the 21-bar monthly cadence.
        assert all(gap == 21 for gap in gaps), f"steady-state cadence broke: gaps={gaps}"


class TestInterpretVariant:
    """interpret_variant produces distinct strategy classes per override."""

    @pytest.fixture
    def faber_spec(self):
        return validate_strategy_spec(FABER_2007_SPEC)

    def test_variant_produces_strategy_class(self, faber_spec):
        cls = interpret_variant(faber_spec, {"sma_200": 150})
        assert cls is not None
        assert issubclass(cls, _get_bt_strategy_class())

    def test_variants_produce_distinct_classes(self, faber_spec):
        cls_150 = interpret_variant(faber_spec, {"sma_200": 150})
        cls_250 = interpret_variant(faber_spec, {"sma_200": 250})
        assert cls_150 is not cls_250
        assert cls_150.__name__ != cls_250.__name__

    def test_variant_class_name_reflects_override(self, faber_spec):
        cls = interpret_variant(faber_spec, {"sma_200": 150})
        assert "150" in cls.__name__

    def test_variant_rewrites_condition_tree(self, faber_spec):
        """The variant strategy uses the overridden period in conditions."""
        cls = interpret_variant(faber_spec, {"sma_200": 50})
        params_dict = dict(cls.params._getitems())
        spec = params_dict["dsl_spec"]
        assert "sma_50" in str(spec["entry"])
        assert "sma_50" in str(spec["exit"])
