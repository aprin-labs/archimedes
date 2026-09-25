"""Tests for the strategy DSL schema validator."""

from __future__ import annotations

import pytest
from archimedes.services.strategy_dsl import (
    FABER_2007_SPEC,
    REFERENCE_EXAMPLES,
    REQUIRED_FIELDS,
    VOL_MANAGED_SPEC,
    DSLError,
    validate_strategy_spec,
)


class TestValidatesReferenceExamples:
    """All reference examples must pass schema validation."""

    @pytest.mark.parametrize("spec", REFERENCE_EXAMPLES, ids=lambda s: s["name"])
    def test_validates_reference_examples(self, spec):
        result = validate_strategy_spec(spec)
        assert result.name == spec["name"]
        assert result.asset_universe == spec["asset_universe"]
        # The reference examples carry no self-declared look-ahead field, and
        # the validated object exposes no attribute to read one back from.
        assert "look_ahead_safe" not in spec
        assert not hasattr(result, "look_ahead_safe")

    def test_faber_spec(self):
        result = validate_strategy_spec(FABER_2007_SPEC)
        assert result.name == "SMA-200 Tactical Allocation"
        assert "SPY" in result.asset_universe
        assert result.rebalance_frequency == "monthly"
        assert result.entry == {"gt": ["close", "sma_200"]}
        assert result.exit == {"lt": ["close", "sma_200"]}
        assert "sma_200" in result.indicators

    def test_vol_managed_spec(self):
        result = validate_strategy_spec(VOL_MANAGED_SPEC)
        assert result.position_sizing["type"] == "volatility_target"
        assert result.position_sizing["annual_pct"] == 0.15


class TestValidationRejectsInvalidSpecs:
    """Invalid specs must be rejected with DSLError."""

    def test_missing_required_field(self):
        with pytest.raises(DSLError, match="missing required fields"):
            validate_strategy_spec({"name": "test"})

    def test_empty_name(self):
        spec = {**FABER_2007_SPEC, "name": ""}
        with pytest.raises(DSLError, match="name must be a non-empty string"):
            validate_strategy_spec(spec)

    def test_empty_asset_universe(self):
        spec = {**FABER_2007_SPEC, "asset_universe": []}
        with pytest.raises(DSLError, match="asset_universe must be a non-empty list"):
            validate_strategy_spec(spec)

    def test_invalid_rebalance_frequency(self):
        spec = {**FABER_2007_SPEC, "rebalance_frequency": "quarterly"}
        with pytest.raises(DSLError, match="rebalance_frequency"):
            validate_strategy_spec(spec)

    def test_unknown_condition_operator(self):
        spec = {**FABER_2007_SPEC, "entry": {"xor": ["close", "sma_200"]}}
        with pytest.raises(DSLError, match="unknown operator"):
            validate_strategy_spec(spec)

    def test_invalid_position_sizing_type(self):
        spec = {**FABER_2007_SPEC, "position_sizing": {"type": "kelly"}}
        with pytest.raises(DSLError, match=r"position_sizing\.type"):
            validate_strategy_spec(spec)

    def test_volatility_target_missing_pct(self):
        spec = {**FABER_2007_SPEC, "position_sizing": {"type": "volatility_target"}}
        with pytest.raises(DSLError, match="annual_pct"):
            validate_strategy_spec(spec)

    def test_not_a_dict(self):
        with pytest.raises(DSLError, match="spec must be a JSON object"):
            validate_strategy_spec("not a dict")

    def test_and_needs_list(self):
        spec = {**FABER_2007_SPEC, "entry": {"and": "close"}}
        with pytest.raises(DSLError, match="'and' needs a list"):
            validate_strategy_spec(spec)

    def test_or_needs_two_conditions(self):
        spec = {**FABER_2007_SPEC, "entry": {"or": [{"gt": ["close", 1]}]}}
        with pytest.raises(DSLError, match="'or' needs a list of >= 2"):
            validate_strategy_spec(spec)


class TestConditionTree:
    """Test complex condition trees."""

    def test_nested_and_or(self):
        spec = {
            **FABER_2007_SPEC,
            "entry": {
                "and": [
                    {"gt": ["close", "sma_200"]},
                    {
                        "or": [
                            {"gt": ["rsi_14", 30]},
                            {"lt": ["realized_vol_20", 0.25]},
                        ]
                    },
                ],
            },
        }
        result = validate_strategy_spec(spec)
        assert "sma_200" in result.indicators
        assert "rsi_14" in result.indicators
        assert "realized_vol_20" in result.indicators

    def test_not_condition(self):
        spec = {
            **FABER_2007_SPEC,
            "entry": {"not": {"lt": ["close", "sma_50"]}},
        }
        result = validate_strategy_spec(spec)
        assert "sma_50" in result.indicators


class TestSpecRoundTrip:
    """Spec can be serialized and re-validated."""

    def test_to_dict_round_trip(self):
        result = validate_strategy_spec(FABER_2007_SPEC)
        d = result.to_dict()
        result2 = validate_strategy_spec(d)
        assert result2.name == result.name
        assert result2.entry == result.entry

    def test_to_json_round_trip(self):
        import json

        result = validate_strategy_spec(FABER_2007_SPEC)
        j = result.to_json()
        d = json.loads(j)
        result2 = validate_strategy_spec(d)
        assert result2.name == result.name


class TestParameterVariants:
    """Validation of the optional parameter_variants field."""

    def test_valid_variants(self):
        spec = {**FABER_2007_SPEC, "parameter_variants": {"sma_200": [150, 175, 200, 225, 250]}}
        result = validate_strategy_spec(spec)
        assert result.parameter_variants is not None
        assert result.parameter_variants["sma_200"] == [150, 175, 200, 225, 250]

    def test_no_variants_is_none(self):
        result = validate_strategy_spec(FABER_2007_SPEC)
        assert result.parameter_variants is None

    def test_variant_key_not_in_indicators(self):
        spec = {**FABER_2007_SPEC, "parameter_variants": {"rsi_14": [10, 14, 20]}}
        with pytest.raises(DSLError, match="must reference an indicator alias"):
            validate_strategy_spec(spec)

    def test_variant_values_not_list(self):
        spec = {**FABER_2007_SPEC, "parameter_variants": {"sma_200": "bad"}}
        with pytest.raises(DSLError, match="must be a list"):
            validate_strategy_spec(spec)

    def test_variant_empty_list(self):
        spec = {**FABER_2007_SPEC, "parameter_variants": {"sma_200": []}}
        with pytest.raises(DSLError, match="at least 2 entries"):
            validate_strategy_spec(spec)

    def test_variant_single_entry(self):
        spec = {**FABER_2007_SPEC, "parameter_variants": {"sma_200": [200]}}
        with pytest.raises(DSLError, match="at least 2 entries"):
            validate_strategy_spec(spec)

    def test_variant_too_many_entries(self):
        spec = {**FABER_2007_SPEC, "parameter_variants": {"sma_200": list(range(9))}}
        with pytest.raises(DSLError, match="at most 8 entries"):
            validate_strategy_spec(spec)

    def test_variant_non_numeric_values(self):
        spec = {**FABER_2007_SPEC, "parameter_variants": {"sma_200": [200, "bad"]}}
        with pytest.raises(DSLError, match="must be numeric"):
            validate_strategy_spec(spec)

    def test_variants_not_dict(self):
        spec = {**FABER_2007_SPEC, "parameter_variants": "bad"}
        with pytest.raises(DSLError, match="must be a dict"):
            validate_strategy_spec(spec)

    def test_variants_round_trip(self):
        import json

        spec = {**FABER_2007_SPEC, "parameter_variants": {"sma_200": [150, 200, 250]}}
        result = validate_strategy_spec(spec)
        d = result.to_dict()
        assert "parameter_variants" in d
        assert d["parameter_variants"]["sma_200"] == [150, 200, 250]
        j = result.to_json()
        parsed = json.loads(j)
        result2 = validate_strategy_spec(parsed)
        assert result2.parameter_variants == result.parameter_variants

    def test_variants_omitted_from_to_dict_when_none(self):
        result = validate_strategy_spec(FABER_2007_SPEC)
        d = result.to_dict()
        assert "parameter_variants" not in d


class TestLookAheadSafeIsRemovedFromTheSchema:
    """``look_ahead_safe`` was a boolean the generating model wrote about its own
    output; the validator only checked it was ``True``, so a spec was admitted on
    its own assertion of innocence. It is REMOVED — not demoted, not
    deprecated-but-read. These tests pin the removal and the back-compat contract
    for specs persisted before it.
    """

    def test_spec_without_the_field_validates(self):
        """The point of the change: no spec ever has to declare this again."""
        spec = {k: v for k, v in FABER_2007_SPEC.items() if k != "look_ahead_safe"}
        assert "look_ahead_safe" not in spec
        result = validate_strategy_spec(spec)
        assert result.name == FABER_2007_SPEC["name"]

    def test_it_is_not_a_required_field(self):
        assert "look_ahead_safe" not in REQUIRED_FIELDS

    def test_missing_field_error_never_names_it(self):
        """A model that omits it must not be told to put it back."""
        with pytest.raises(DSLError) as exc:
            validate_strategy_spec({"name": "bare"})
        assert "look_ahead_safe" not in str(exc.value)

    @pytest.mark.parametrize("declared", [True, False])
    def test_legacy_persisted_spec_carrying_the_field_loads_without_crashing(self, declared):
        """Migration/back-compat: rows written before the removal still carry the
        key. The reader must ignore it gracefully — including a legacy ``false``,
        which is exactly as uninformative as a ``true`` and must not be re-honoured
        as a rejection.
        """
        legacy = {**FABER_2007_SPEC, "look_ahead_safe": declared}
        result = validate_strategy_spec(legacy)
        assert result.name == FABER_2007_SPEC["name"]

    @pytest.mark.parametrize("declared", [True, False])
    def test_the_declared_value_is_never_trusted_or_carried(self, declared):
        """Ignoring it is not enough — it must be unreadable downstream. The
        validated object exposes no attribute and round-trips without the key, so
        a consumer cannot resurrect the declaration by accident.
        """
        legacy = {**FABER_2007_SPEC, "look_ahead_safe": declared}
        result = validate_strategy_spec(legacy)
        assert not hasattr(result, "look_ahead_safe")
        assert "look_ahead_safe" not in result.to_dict()
        assert "look_ahead_safe" not in result.to_json()

    def test_a_legacy_spec_round_trips_into_a_clean_one(self):
        """Re-validating the serialised form of a legacy spec is stable and drops
        the field permanently, so the key dies out as rows are rewritten.
        """
        import json

        legacy = {**FABER_2007_SPEC, "look_ahead_safe": True}
        once = validate_strategy_spec(legacy)
        twice = validate_strategy_spec(json.loads(once.to_json()))
        assert twice.to_dict() == once.to_dict()
        assert "look_ahead_safe" not in twice.to_dict()

    def test_validation_does_not_mutate_the_callers_dict(self):
        """The legacy key is dropped from the DSL's view of the spec, not from the
        caller's object — a persisted blob passed in by reference must come back
        unchanged so nothing downstream sees a surprise in-place edit.
        """
        legacy = {**FABER_2007_SPEC, "look_ahead_safe": True}
        validate_strategy_spec(legacy)
        assert legacy["look_ahead_safe"] is True
