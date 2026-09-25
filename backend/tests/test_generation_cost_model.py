"""The measured-cost model (#1411) — arithmetic, and the refusals around it.

The module under test converts a ``cost_v1`` measurement into dollars. Two
properties matter more than the arithmetic and are tested first-class here:

* an unpriceable input produces a **visible incomplete**, never a total that
  quietly omits the unpriced part, and
* the priced output is structurally barred from being filed as a measurement,
  which is what keeps the ``generation_costs`` row's two-column separation
  (#1326) enforced rather than merely intended.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from archimedes.services import cost_meter
from archimedes.services.generation_cost_model import (
    RATE_CARD_ENV,
    RateCard,
    RateCardError,
    apply_margin,
    assert_priceable,
    billed_seconds,
    estimate_cost,
    rate_card_from_env,
)

MODEL = "amazon.nova-micro-v1:0"

# Deliberately round, invented rates: this file tests arithmetic, and real
# vendor prices belong on a rate card supplied by the environment, not in the
# repo. $1.00 per Mtok in, $10.00 per Mtok out; $0.001 per GB-second.
CARD_JSON = {
    "lane": "lambda",
    "compute_usd_per_gb_second": "0.001",
    "compute_gb": "2",
    "fixed_overhead_usd": "0.0001",
    "billing_granularity_seconds": "0.001",
    "models": {MODEL: {"input_usd_per_mtok": "1.00", "output_usd_per_mtok": "10.00"}},
}


def _card(**overrides) -> RateCard:
    raw = {**CARD_JSON, **overrides}
    return RateCard.from_mapping(raw)


def _snapshot(**overrides) -> dict:
    snap = {
        "schema": "cost_v1",
        "job_id": "j1",
        "wall_seconds": 48.0,
        "cpu_seconds": 30.0,
        "llm": {
            "calls": 15,
            "calls_missing_usage": 0,
            "usage_complete": True,
            "input_tokens": 300_000,
            "output_tokens": 20_000,
            "by_model": {
                MODEL: {"calls": 15, "calls_missing_usage": 0, "input_tokens": 300_000, "output_tokens": 20_000}
            },
        },
        "stages": {},
        "writes": {},
        "meta": {},
    }
    snap.update(overrides)
    return snap


def test_prices_llm_compute_and_overhead_into_one_total():
    """300k in @ $1/Mtok + 20k out @ $10/Mtok = $0.50; 48s × 2GB × $0.001 = $0.096."""
    estimate = estimate_cost(_snapshot(), _card())

    assert estimate["complete"] is True
    assert estimate["lane"] == "lambda"
    assert estimate["components"]["llm_usd"] == "0.50000000"
    assert estimate["components"]["compute_usd"] == "0.09600000"
    assert estimate["components"]["overhead_usd"] == "0.00010000"
    assert estimate["total_usd"] == "0.596100"


def test_a_model_with_no_rate_is_incomplete_not_free():
    """The cost-meter rule ("a missing measurement is never a zero") applied to prices.

    A generation that used a model the card does not know is not a cheap
    generation. It is an unpriceable one, and the total must not be quotable.
    """
    snapshot = _snapshot()
    snapshot["llm"]["by_model"]["some-new-model"] = {
        "calls": 1,
        "calls_missing_usage": 0,
        "input_tokens": 5_000_000,
        "output_tokens": 1_000_000,
    }

    estimate = estimate_cost(snapshot, _card())

    assert estimate["complete"] is False
    assert any("some-new-model" in reason for reason in estimate["incomplete_reasons"])
    with pytest.raises(RateCardError, match="refusing to quote"):
        assert_priceable(estimate)


def test_incomplete_meter_usage_blocks_the_quote():
    snapshot = _snapshot()
    snapshot["llm"]["usage_complete"] = False
    snapshot["llm"]["calls_missing_usage"] = 3

    estimate = estimate_cost(snapshot, _card())

    assert estimate["complete"] is False
    assert any("incomplete LLM usage" in reason for reason in estimate["incomplete_reasons"])


def test_wrong_schema_is_refused():
    estimate = estimate_cost(_snapshot(schema="cost_v2"), _card())
    assert estimate["complete"] is False
    assert any("cost_v1" in reason for reason in estimate["incomplete_reasons"])


def test_missing_wall_seconds_does_not_price_compute_as_zero():
    estimate = estimate_cost(_snapshot(wall_seconds=None), _card())
    assert estimate["complete"] is False
    assert any("wall_seconds" in reason for reason in estimate["incomplete_reasons"])


class TestBilling:
    """The lane's billing rules, which is where under-quoting actually happens."""

    def test_partial_granularity_rounds_up(self):
        # 1.0005s on a 1ms-granularity lane bills 1.001s, not 1.0005s.
        assert billed_seconds(1.0005, _card()) == Decimal("1.001")

    def test_per_second_lane_with_a_one_minute_minimum(self):
        """A 4-second run on a per-task lane costs a full minute of compute."""
        card = _card(billing_granularity_seconds="1", minimum_billed_seconds="60")
        assert billed_seconds(4.0, card) == Decimal("60")

        estimate = estimate_cost(_snapshot(wall_seconds=4.0), card)
        # 60s × 2GB × $0.001 — fifteen times the naive 4s × 2GB × $0.001.
        assert estimate["components"]["compute_usd"] == "0.12000000"

    def test_zero_wall_time_still_pays_the_minimum(self):
        card = _card(billing_granularity_seconds="1", minimum_billed_seconds="60")
        assert billed_seconds(0.0, card) == Decimal("60")


class TestRateCardParsing:
    def test_absent_env_means_no_measured_pricing(self):
        assert rate_card_from_env({}) is None

    def test_malformed_env_fails_safe_to_none(self):
        """A typo must not make generation free, and must not 500 the quote."""
        assert rate_card_from_env({RATE_CARD_ENV: "{not json"}) is None
        assert rate_card_from_env({RATE_CARD_ENV: '{"lane": "lambda"}'}) is None

    def test_valid_env_parses(self):
        import json

        card = rate_card_from_env({RATE_CARD_ENV: json.dumps(CARD_JSON)})
        assert card is not None
        assert card.lane == "lambda"
        assert card.models[MODEL].output_usd_per_mtok == Decimal("10.00")

    def test_float_rates_are_read_as_written(self):
        """JSON floats go through str(), so 0.1 is one tenth, not 0.1000000000000000055."""
        card = _card(compute_usd_per_gb_second=0.1)
        assert card.compute_usd_per_gb_second == Decimal("0.1")

    def test_negative_rate_is_refused(self):
        with pytest.raises(RateCardError):
            _card(compute_usd_per_gb_second="-1")

    def test_missing_lane_is_refused(self):
        raw = {k: v for k, v in CARD_JSON.items() if k != "lane"}
        with pytest.raises(RateCardError, match="lane"):
            RateCard.from_mapping(raw)


class TestMargin:
    def test_price_rounds_up_never_below_cost(self):
        estimate = estimate_cost(_snapshot(), _card())
        # 0.596100 × 1.5 = 0.894150 exactly.
        assert apply_margin(estimate, multiplier="1.5") == "$0.894150"

    def test_sub_micro_dollar_price_rounds_up_not_to_zero(self):
        """$0.0000001 of compute must quote as one microdollar, not as free."""
        no_llm = _snapshot(
            wall_seconds=0.001,
            llm={
                "calls": 0,
                "calls_missing_usage": 0,
                "usage_complete": True,
                "input_tokens": 0,
                "output_tokens": 0,
                "by_model": {},
            },
        )
        cheap = estimate_cost(no_llm, _card(fixed_overhead_usd="0", compute_gb="0.1"))

        assert cheap["complete"] is True
        assert cheap["components"]["compute_usd"] == "0.00000010"
        assert cheap["total_usd"] == "0.000000"  # the modelled cost, rounded to USDC precision
        assert apply_margin(cheap, multiplier="1") == "$0.000001"

    def test_floor_wins_when_higher(self):
        estimate = estimate_cost(_snapshot(), _card())
        assert apply_margin(estimate, multiplier="1", floor_usd="2.00") == "$2.000000"

    def test_incomplete_estimate_cannot_be_priced(self):
        estimate = estimate_cost(_snapshot(schema="nope"), _card())
        with pytest.raises(RateCardError):
            apply_margin(estimate, multiplier="1.5")


def test_the_estimate_is_barred_from_the_measurement_column():
    """A priced document must never be filed as a ``cost_v1`` measurement.

    ``assert_measurement_only`` is the guard on the persistence boundary
    (#1326). Proving it rejects THIS module's output is what makes the
    two-column separation enforced rather than a convention: the obvious future
    shortcut — merging the quote into the measurement — raises here.
    """
    estimate = estimate_cost(_snapshot(), _card())
    with pytest.raises(cost_meter.PricingLeakError):
        cost_meter.assert_measurement_only(estimate)


def test_a_real_meter_snapshot_prices_end_to_end():
    """Consume a snapshot produced by the real meter, not a hand-written dict."""
    with cost_meter.measure(job_id="j-real") as meter:
        meter.record_usage(model=MODEL, input_tokens=1000, output_tokens=100)
        with meter.stage("debate_backtest"):
            pass
        snapshot = meter.snapshot()

    estimate = estimate_cost(snapshot, _card())
    assert estimate["complete"] is True
    # 1000/1e6 × $1 + 100/1e6 × $10 = $0.002
    assert estimate["components"]["llm_usd"] == "0.00200000"
