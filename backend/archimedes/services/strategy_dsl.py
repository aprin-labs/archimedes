"""Strategy DSL — JSON schema definition and validator for fusion-generated strategies.

The DSL is a closed-enum JSON schema that can express momentum, vol-managed,
trend-following, and tactical-asset-allocation strategies. The interpreter
translates validated specs into backtrader.Strategy subclasses at runtime.

No arbitrary code execution — the schema is strictly validated before any
backtrader objects are instantiated.

The schema declares STRUCTURE only. It carries no field in which the generating
model grades its own output: the retired ``look_ahead_safe`` boolean is gone
(from ``REQUIRED_FIELDS``, from :class:`StrategySpec`, and from the generation
prompt), and the look-ahead verdict is derived from the validated spec by
``archimedes.services.dsl_lookahead_audit``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ── Closed enums ──────────────────────────────────────────────────────

REBALANCE_FREQUENCIES = frozenset({"daily", "weekly", "monthly"})

INDICATOR_NAMES = frozenset(
    {
        "sma",
        "ema",
        "rsi",
        "realized_vol",
        "momentum",
    }
)

COMPARISON_OPS = frozenset({"gt", "lt", "gte", "lte"})

LOGIC_OPS = frozenset({"and", "or", "not"})

POSITION_SIZING_TYPES = frozenset(
    {
        "full_invested_when_in_market",
        "equal_weight",
        "inverse_vol",
        "volatility_target",
    }
)

# Keys each sizing type accepts, INCLUDING "type". The closed-enum promise this
# DSL makes is worthless if the dict carrying the enum member is open: a spec
# saying {"type": "inverse_vol", "reference_vol": 0.30} — the plausible
# misspelling of reference_vol_annual — used to validate, interpret, backtest and
# publish at the 0.15 default, with the author's 0.30 silently discarded and
# nothing anywhere saying so. Unknown keys are rejected by name instead. Adding a
# key to a sizing type means adding it here in the same commit the interpreter
# starts reading it.
POSITION_SIZING_KEYS: dict[str, frozenset[str]] = {
    "full_invested_when_in_market": frozenset({"type"}),
    "equal_weight": frozenset({"type"}),
    "inverse_vol": frozenset({"type", "reference_vol_annual"}),
    "volatility_target": frozenset({"type", "annual_pct"}),
}

# ── Errors ────────────────────────────────────────────────────────────


class DSLError(Exception):
    """Raised when a strategy spec fails validation."""


# ── Condition tree validator ──────────────────────────────────────────

# Operands that reference indicator outputs: "close", "sma_200", "rsi_14", etc.
_PRICE_OPERANDS = frozenset({"close", "open", "high", "low", "volume"})


def _parse_indicator_operand(name: str) -> tuple[str, int] | None:
    """Parse 'sma_200' into ('sma', 200). Returns None for non-indicator operands."""
    if name in _PRICE_OPERANDS:
        return None
    parts = name.rsplit("_", 1)
    if len(parts) != 2:
        return None
    indicator, period_str = parts
    if indicator not in INDICATOR_NAMES:
        return None
    try:
        period = int(period_str)
    except ValueError:
        return None
    if period < 1 or period > 10_000:
        raise DSLError(f"indicator period out of range: {name}")
    return (indicator, period)


def _validate_condition(cond: Any, path: str = "root") -> set[str]:
    """Validate a condition tree node. Returns set of indicator operands referenced."""
    if not isinstance(cond, dict):
        raise DSLError(f"{path}: condition must be a dict, got {type(cond).__name__}")

    keys = list(cond.keys())
    if len(keys) != 1:
        raise DSLError(f"{path}: condition must have exactly one key, got {keys}")

    op = keys[0]

    if op in LOGIC_OPS:
        operands = cond[op]
        if op == "not":
            if not isinstance(operands, dict):
                raise DSLError(f"{path}: 'not' operand must be a dict")
            return _validate_condition(operands, f"{path}.not")
        if not isinstance(operands, list) or len(operands) < 2:
            raise DSLError(f"{path}: '{op}' needs a list of >= 2 conditions")
        indicators: set[str] = set()
        for i, child in enumerate(operands):
            indicators |= _validate_condition(child, f"{path}.{op}[{i}]")
        return indicators

    if op in COMPARISON_OPS:
        args = cond[op]
        if not isinstance(args, list) or len(args) != 2:
            raise DSLError(f"{path}: '{op}' needs exactly 2 arguments")
        indicators: set[str] = set()
        for arg in args:
            if isinstance(arg, (int, float)):
                continue
            if isinstance(arg, str):
                parsed = _parse_indicator_operand(arg)
                if parsed is not None:
                    indicators.add(f"{parsed[0]}_{parsed[1]}")
                elif arg not in _PRICE_OPERANDS:
                    raise DSLError(f"{path}: unknown operand '{arg}'")
                continue
            raise DSLError(f"{path}: '{op}' arguments must be strings or numbers")
        return indicators

    raise DSLError(f"{path}: unknown operator '{op}'")


# ── Spec validation ───────────────────────────────────────────────────

REQUIRED_FIELDS = frozenset(
    {
        "name",
        "asset_universe",
        "rebalance_frequency",
        "entry",
        "exit",
        "position_sizing",
        "source_arxiv_ids",
    }
)

# Keys accepted-and-ignored for back-compat with specs persisted before the
# field was removed from the schema. See ``_note_legacy_fields``.
LEGACY_IGNORED_FIELDS = frozenset({"look_ahead_safe"})


def _note_legacy_fields(spec: dict[str, Any]) -> None:
    """Log-and-forget any retired key a persisted spec still carries.

    ``look_ahead_safe`` was a REQUIRED boolean the generating model wrote about
    its own output; the validator only checked that it was ``True``, so a spec
    was admitted on its own assertion of innocence. It is removed from the
    schema, from the prompt, and from :class:`StrategySpec` — the look-ahead
    verdict is now DERIVED by
    ``archimedes.services.dsl_lookahead_audit.audit_dsl_strategy`` over the
    validated spec (ADR ``strategy-dsl-hardening-over-lean4.md`` item 3).

    Specs written before the removal are still in the DB and still arrive here.
    They must load without crashing, and their declared value must never be
    read. Both hold without touching anything: the key is simply not in
    ``REQUIRED_FIELDS`` and is never copied onto the :class:`StrategySpec`, so
    it cannot reach a consumer. This function deliberately does NOT mutate the
    caller's dict — a validator that quietly edited its input would surprise
    every caller holding the same blob — it only records that the key was seen.
    A legacy ``look_ahead_safe: false`` is ignored exactly like a ``true``: the
    declaration carried no information in either direction, and re-honouring the
    ``false`` would be trusting the same slop with the sign flipped.
    """
    for key in LEGACY_IGNORED_FIELDS & set(spec.keys()):
        logger.debug(
            "ignoring retired DSL field %r (value %r) — the look-ahead verdict is derived, not declared",
            key,
            spec[key],
        )


def validate_strategy_spec(spec: dict[str, Any]) -> StrategySpec:
    """Validate a strategy DSL spec. Returns a StrategySpec on success, raises DSLError."""
    if not isinstance(spec, dict):
        raise DSLError("spec must be a JSON object")

    _note_legacy_fields(spec)

    missing = REQUIRED_FIELDS - set(spec.keys())
    if missing:
        raise DSLError(f"missing required fields: {sorted(missing)}")

    # name
    if not isinstance(spec["name"], str) or not spec["name"].strip():
        raise DSLError("name must be a non-empty string")

    # asset_universe
    universe = spec["asset_universe"]
    if not isinstance(universe, list) or len(universe) < 1:
        raise DSLError("asset_universe must be a non-empty list of strings")
    for a in universe:
        if not isinstance(a, str) or not a.strip():
            raise DSLError(f"asset_universe entry must be a non-empty string, got {a!r}")

    # rebalance_frequency
    if spec["rebalance_frequency"] not in REBALANCE_FREQUENCIES:
        raise DSLError(f"rebalance_frequency must be one of {sorted(REBALANCE_FREQUENCIES)}")

    # entry / exit conditions
    entry_indicators = _validate_condition(spec["entry"], "entry")
    exit_indicators = _validate_condition(spec["exit"], "exit")
    all_indicators = entry_indicators | exit_indicators

    # position_sizing
    ps = spec["position_sizing"]
    if not isinstance(ps, dict):
        raise DSLError("position_sizing must be a dict")
    if ps.get("type") not in POSITION_SIZING_TYPES:
        raise DSLError(f"position_sizing.type must be one of {sorted(POSITION_SIZING_TYPES)}")
    allowed_keys = POSITION_SIZING_KEYS[ps["type"]]
    unknown_keys = sorted(set(ps.keys()) - allowed_keys)
    if unknown_keys:
        raise DSLError(
            f"position_sizing['{ps['type']}'] does not accept {unknown_keys}; allowed keys: {sorted(allowed_keys)}"
        )
    if ps["type"] == "volatility_target":
        target = ps.get("annual_pct")
        if not isinstance(target, (int, float)) or target <= 0:
            raise DSLError("volatility_target requires annual_pct > 0")
    if ps["type"] == "inverse_vol" and "reference_vol_annual" in ps:
        # Optional; the interpreter defaults it. But a present-and-nonsensical
        # value must be rejected rather than silently coerced — a zero or
        # negative reference makes the inverse-vol scale meaningless.
        reference = ps["reference_vol_annual"]
        if not isinstance(reference, (int, float)) or isinstance(reference, bool) or reference <= 0:
            raise DSLError("inverse_vol reference_vol_annual must be a number > 0 when present")

    # source_arxiv_ids
    arxiv_ids = spec["source_arxiv_ids"]
    if not isinstance(arxiv_ids, list):
        raise DSLError("source_arxiv_ids must be a list")
    for aid in arxiv_ids:
        if not isinstance(aid, str) or not aid.strip():
            raise DSLError(f"source_arxiv_ids entry must be a non-empty string, got {aid!r}")

    # NOTE: there is deliberately no look-ahead check here. The retired
    # ``look_ahead_safe`` field was the generating model's own assertion about
    # its own output, and validating it proved nothing. The verdict is derived
    # downstream from this validated spec by
    # ``dsl_lookahead_audit.audit_dsl_strategy``.

    # parameter_variants (optional)
    pv = spec.get("parameter_variants")
    if pv is not None:
        if not isinstance(pv, dict):
            raise DSLError("parameter_variants must be a dict")
        for key, values in pv.items():
            if key not in all_indicators:
                raise DSLError(
                    f"parameter_variants key '{key}' must reference an indicator "
                    f"alias from entry/exit conditions; valid: {sorted(all_indicators)}"
                )
            if not isinstance(values, list):
                raise DSLError(f"parameter_variants['{key}'] must be a list")
            if len(values) < 2:
                raise DSLError(f"parameter_variants['{key}'] must have at least 2 entries, got {len(values)}")
            if len(values) > 8:
                raise DSLError(f"parameter_variants['{key}'] must have at most 8 entries, got {len(values)}")
            for v in values:
                if not isinstance(v, (int, float)):
                    raise DSLError(f"parameter_variants['{key}'] entries must be numeric, got {v!r}")

    return StrategySpec(
        name=spec["name"],
        asset_universe=universe,
        rebalance_frequency=spec["rebalance_frequency"],
        entry=spec["entry"],
        exit=spec["exit"],
        position_sizing=ps,
        source_arxiv_ids=arxiv_ids,
        indicators=sorted(all_indicators),
        parameter_variants=pv,
    )


# ── Spec data object ──────────────────────────────────────────────────


@dataclass(frozen=True)
class StrategySpec:
    """A validated strategy DSL specification.

    Carries no ``look_ahead_safe`` field, by design. The look-ahead verdict is
    an OUTPUT derived from this object by
    ``dsl_lookahead_audit.audit_dsl_strategy``, never an input the emitting
    model declared. Because the attribute does not exist, a consumer cannot
    accidentally read a declaration back — the mistake is a ``AttributeError``,
    not a silent false claim.
    """

    name: str
    asset_universe: list[str]
    rebalance_frequency: str
    entry: dict[str, Any]
    exit: dict[str, Any]
    position_sizing: dict[str, Any]
    source_arxiv_ids: list[str]
    indicators: list[str] = field(default_factory=list)
    parameter_variants: dict[str, list[int | float]] | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "asset_universe": self.asset_universe,
            "rebalance_frequency": self.rebalance_frequency,
            "entry": self.entry,
            "exit": self.exit,
            "position_sizing": self.position_sizing,
            "source_arxiv_ids": self.source_arxiv_ids,
        }
        if self.parameter_variants is not None:
            d["parameter_variants"] = self.parameter_variants
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


# ── Reference examples ────────────────────────────────────────────────

FABER_2007_SPEC: dict[str, Any] = {
    "name": "SMA-200 Tactical Allocation",
    "asset_universe": ["SPY"],
    "rebalance_frequency": "monthly",
    "entry": {"gt": ["close", "sma_200"]},
    "exit": {"lt": ["close", "sma_200"]},
    "position_sizing": {"type": "full_invested_when_in_market"},
    "source_arxiv_ids": ["0706.1497"],
}

VOL_MANAGED_SPEC: dict[str, Any] = {
    "name": "Volatility-Managed Portfolio",
    "asset_universe": ["SPY"],
    "rebalance_frequency": "daily",
    "entry": {"gt": ["close", 0]},
    "exit": {"and": [{"lt": ["close", 0]}, {"lt": ["close", 0]}]},
    "position_sizing": {"type": "volatility_target", "annual_pct": 0.15},
    "source_arxiv_ids": ["1704.03022"],
}

REFERENCE_EXAMPLES = [FABER_2007_SPEC, VOL_MANAGED_SPEC]
