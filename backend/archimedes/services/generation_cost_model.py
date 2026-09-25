"""Turn one generation's measurement into a per-generation cost (issue #1411).

:mod:`archimedes.services.cost_meter` records what a generation *consumed* —
tokens, seconds, writes — and refuses to hold any pricing vocabulary at all.
This module is the other half: the pure arithmetic that converts one
``cost_v1`` snapshot plus a **rate card** into dollars, so the quote seam in
:func:`archimedes.services.generation_payment.quote` can eventually be backed
by a measurement instead of a flat env constant (#1217).

Three boundaries make this safe to add without weakening anything:

1. **The rates are not in the repo.** A :class:`RateCard` carries vendor prices
   and the compute lane's memory allocation, and is supplied by the caller or
   parsed from one JSON environment variable. This repo holds code and
   technical documentation; pricing and margin strategy live in the private
   docs repo (``CLAUDE.md`` § Project), and a hard-coded rate here would be
   exactly that material leaking into a public tree. It is also the practical
   choice: vendor prices change without a deploy.

2. **A missing rate is never a zero.** The measurement layer's first rule is
   that an absent measurement must be a visible absence rather than a plausible
   substitute; the same rule applies to an absent *price*. A model id the card
   does not know, or a snapshot whose ``usage_complete`` is ``False``, produces
   a result with ``complete: False`` and a stated reason — never a total that
   silently omits the unpriced part. A caller must not quote from an incomplete
   estimate, and :func:`assert_priceable` is the one-line way to enforce that.

3. **This output is a quote, not a measurement.** Its keys are deliberately
   priced (``llm_usd``, ``total_usd``), which means
   :func:`archimedes.services.cost_meter.assert_measurement_only` **raises** on
   it. That is the intended relationship, not an accident: the durable
   ``generation_costs`` row stores measurement and quote in two separate
   columns precisely so a priced document can never be filed as a ``cost_v1``
   record, and that guard is what enforces it.

**Nothing on a customer-facing path imports this module.** One reader exists —
:mod:`archimedes.services.generation_cost_rollup`, which aggregates these
estimates for the admin-only ``GET /api/metrics/private/cost`` dashboard
(#1217). That is a *report*, not a charge. Wiring this into ``quote()`` is
still a separate, flag-gated change designed in
``docs/adr/lambda-generation-offload.md``; the flat ``GENERATION_PRICE_USD``
remains the default and what a user pays is unchanged by this module existing.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, ROUND_UP, Decimal, InvalidOperation
from typing import Any

logger = logging.getLogger(__name__)

#: Bumped whenever the estimate's shape changes, so a stored estimate is
#: self-describing to whatever reads it later — same contract as ``cost_v1``.
SCHEMA = "cost_model_v1"

#: The environment variable carrying the rate card, as a JSON object. Absent
#: (or malformed) means "no measured pricing available", which is a supported
#: state: the flat price is still the default.
RATE_CARD_ENV = "GENERATION_COST_RATE_CARD"

# Stable machine-readable codes for the ways an estimate can come out
# incomplete. ``incomplete_reasons`` stays human prose (it is what a refusal
# message quotes); these are what an aggregate groups by. A rollup that had to
# group by the prose would be parsing a sentence containing a call count and a
# model id — two unbounded values — so "3 calls without usage" and "4 calls
# without usage" would be two different buckets for one cause.
REASON_SCHEMA_MISMATCH = "schema_mismatch"
REASON_LLM_USAGE_INCOMPLETE = "llm_usage_incomplete"
REASON_NO_PER_MODEL_TOKENS = "llm_no_per_model_tokens"
REASON_MODEL_NOT_ON_CARD = "model_not_on_rate_card"
REASON_NO_WALL_SECONDS = "no_wall_seconds"

#: USDC settles at six decimals, and circlekit's price strings are
#: ``"$X.XXXXXX"``. Quoting more precision than the asset can express would be
#: a number that cannot actually be charged.
_QUOTE_EXPONENT = Decimal("0.000001")

#: Components are reported with more precision than a quote so the small terms
#: stay legible — a $0.0000015 compute term rounds to zero at six decimals and
#: would read as "compute is free", which is the wrong conclusion to hand a
#: reader.
_COMPONENT_EXPONENT = Decimal("0.00000001")

_MILLION = Decimal(1_000_000)


def _fixed(value: Decimal) -> str:
    """Render a Decimal in plain fixed-point notation, always.

    ``str(Decimal("0.00000010"))`` is ``"1.0E-7"`` — Decimal switches to
    scientific notation once the adjusted exponent drops below -6, which is
    exactly the range the compute term lives in. A price rendered as ``1.0E-7``
    is not a price anything downstream will parse.
    """
    return format(value, "f")


class RateCardError(ValueError):
    """The rate card is unusable. Raised eagerly; never silently defaulted."""


def _decimal(value: Any, *, where: str) -> Decimal:
    """Parse a rate into a non-negative finite ``Decimal``.

    Rates arrive as JSON, where a price is either a string or a float. Floats
    are accepted but routed through ``str`` so ``0.1`` means one tenth rather
    than the binary value nearest to it — over a million generations the
    difference is real money and the wrong direction is not predictable.
    """
    if isinstance(value, bool) or value is None:
        raise RateCardError(f"{where}: expected a number, got {value!r}")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise RateCardError(f"{where}: {value!r} is not a number") from exc
    if not parsed.is_finite() or parsed < 0:
        raise RateCardError(f"{where}: {value!r} must be finite and non-negative")
    return parsed


@dataclass(frozen=True)
class ModelRate:
    """Per-million-token prices for one model id, as the vendor publishes them."""

    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal


@dataclass(frozen=True)
class RateCard:
    """Everything needed to price one generation, and nothing about margin.

    ``lane`` names where the generation ran (``fargate_inline``, ``lambda``,
    ``ecs_task``…). It is carried through to the estimate because the same
    measurement costs different amounts in different lanes, and a stored
    estimate that does not say which lane it priced is not interpretable later.
    This is the ``compute_lane`` field the spike's scoping comment asks the
    ``cost_v1`` meta block to carry.

    ``billing_granularity_seconds`` and ``minimum_billed_seconds`` are what keep
    the module honest across lanes rather than hard-coding one vendor's rules:
    Lambda bills per millisecond with no floor, while a per-task lane bills
    whole seconds with a one-minute minimum, and a 4-second run in the second
    lane costs sixty seconds of compute. Pricing a short run as if it were
    billed continuously is the single easiest way to under-quote.
    """

    lane: str
    compute_usd_per_gb_second: Decimal
    compute_gb: Decimal
    fixed_overhead_usd: Decimal = Decimal(0)
    billing_granularity_seconds: Decimal = Decimal("0.001")
    minimum_billed_seconds: Decimal = Decimal(0)
    models: dict[str, ModelRate] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> RateCard:
        if not isinstance(raw, dict):
            raise RateCardError(f"rate card must be a JSON object, got {type(raw).__name__}")
        lane = str(raw.get("lane") or "").strip()
        if not lane:
            raise RateCardError("rate card is missing 'lane'")
        models: dict[str, ModelRate] = {}
        for model_id, rates in (raw.get("models") or {}).items():
            if not isinstance(rates, dict):
                raise RateCardError(f"models[{model_id!r}] must be an object")
            models[str(model_id)] = ModelRate(
                input_usd_per_mtok=_decimal(rates.get("input_usd_per_mtok"), where=f"models[{model_id!r}].input"),
                output_usd_per_mtok=_decimal(rates.get("output_usd_per_mtok"), where=f"models[{model_id!r}].output"),
            )
        granularity = _decimal(raw.get("billing_granularity_seconds", "0.001"), where="billing_granularity_seconds")
        if granularity <= 0:
            raise RateCardError("billing_granularity_seconds must be > 0")
        return cls(
            lane=lane,
            compute_usd_per_gb_second=_decimal(raw.get("compute_usd_per_gb_second"), where="compute_usd_per_gb_second"),
            compute_gb=_decimal(raw.get("compute_gb"), where="compute_gb"),
            fixed_overhead_usd=_decimal(raw.get("fixed_overhead_usd", 0), where="fixed_overhead_usd"),
            billing_granularity_seconds=granularity,
            minimum_billed_seconds=_decimal(raw.get("minimum_billed_seconds", 0), where="minimum_billed_seconds"),
            models=models,
        )


def rate_card_from_env(env: dict[str, str] | None = None) -> RateCard | None:
    """Parse :data:`RATE_CARD_ENV`; ``None`` when unset.

    Fails SAFE the way ``generation_payment._price()`` does — a malformed card
    logs loudly and returns ``None``, which leaves the flat price in force. A
    typo in a rate must not be able to make generation free, and it must not be
    able to take the paywall down either.
    """
    raw = (env or os.environ).get(RATE_CARD_ENV, "").strip()
    if not raw:
        return None
    try:
        return RateCard.from_mapping(json.loads(raw))
    except (json.JSONDecodeError, RateCardError) as exc:
        logger.error(
            "invalid %s — measured pricing unavailable, falling back to the flat price: %s", RATE_CARD_ENV, exc
        )
        return None


def billed_seconds(wall_seconds: float | Decimal, card: RateCard) -> Decimal:
    """The seconds the vendor will actually bill for ``wall_seconds``.

    Rounded UP to the lane's granularity and then floored at its minimum: both
    directions are what the invoice does, and estimating either the other way
    quotes below cost.
    """
    seconds = _decimal(wall_seconds, where="wall_seconds")
    steps = (seconds / card.billing_granularity_seconds).to_integral_value(rounding=ROUND_UP)
    return max(steps * card.billing_granularity_seconds, card.minimum_billed_seconds)


def _llm_cost(snapshot: dict[str, Any], card: RateCard) -> tuple[Decimal, list[tuple[str, str]], list[str]]:
    """Price the LLM term from the snapshot's per-model token counts.

    Per-model, never on the aggregate: models differ by an order of magnitude in
    price, so a total priced at one model's rate is not an estimate of anything.

    Returns the priced total, the ``(code, prose)`` reasons it is incomplete,
    and the model ids the card could not price.
    """
    llm = snapshot.get("llm") or {}
    reasons: list[tuple[str, str]] = []
    unpriced_models: list[str] = []
    if not llm.get("usage_complete", False):
        missing = llm.get("calls_missing_usage")
        reasons.append(
            (
                REASON_LLM_USAGE_INCOMPLETE,
                f"cost_meter reported incomplete LLM usage ({missing} call(s) without a usage block)",
            )
        )

    total = Decimal(0)
    by_model = llm.get("by_model") or {}
    if not by_model and llm.get("calls"):
        reasons.append((REASON_NO_PER_MODEL_TOKENS, "snapshot records LLM calls but no per-model token counts"))
    for model_id, counts in by_model.items():
        rate = card.models.get(str(model_id))
        if rate is None:
            reasons.append((REASON_MODEL_NOT_ON_CARD, f"no rate on the card for model {model_id!r}"))
            unpriced_models.append(str(model_id))
            continue
        input_tokens = _decimal(counts.get("input_tokens", 0), where=f"by_model[{model_id!r}].input_tokens")
        output_tokens = _decimal(counts.get("output_tokens", 0), where=f"by_model[{model_id!r}].output_tokens")
        total += input_tokens / _MILLION * rate.input_usd_per_mtok
        total += output_tokens / _MILLION * rate.output_usd_per_mtok
    return total, reasons, unpriced_models


def estimate_cost(snapshot: dict[str, Any], card: RateCard) -> dict[str, Any]:
    """Price one ``cost_v1`` snapshot against ``card``.

    Returns a self-describing estimate. ``complete`` is the only field a caller
    needs to read before deciding whether the total is safe to quote from;
    ``incomplete_reasons`` says why when it is not, and
    ``incomplete_reason_codes`` says the same thing in a form an aggregate can
    group by without parsing prose.
    """
    if not isinstance(snapshot, dict):
        raise TypeError(f"snapshot must be a dict, got {type(snapshot).__name__}")
    reasons: list[tuple[str, str]] = []
    schema = snapshot.get("schema")
    if schema != "cost_v1":
        reasons.append((REASON_SCHEMA_MISMATCH, f"snapshot schema {schema!r} is not the cost_v1 this model prices"))

    llm_usd, llm_reasons, unpriced_models = _llm_cost(snapshot, card)
    reasons.extend(llm_reasons)

    wall = snapshot.get("wall_seconds")
    if wall is None or (isinstance(wall, float) and not math.isfinite(wall)):
        reasons.append(
            (REASON_NO_WALL_SECONDS, "snapshot has no usable wall_seconds — the compute term cannot be priced")
        )
        seconds = Decimal(0)
    else:
        seconds = billed_seconds(wall, card)
    compute_usd = seconds * card.compute_gb * card.compute_usd_per_gb_second

    total = llm_usd + compute_usd + card.fixed_overhead_usd
    return {
        "schema": SCHEMA,
        "lane": card.lane,
        "complete": not reasons,
        "incomplete_reasons": [prose for _, prose in reasons],
        # Deduplicated, first-seen order: two calls missing usage is one cause,
        # not two, and a bucket count must not double-count one snapshot.
        "incomplete_reason_codes": list(dict.fromkeys(code for code, _ in reasons)),
        "unpriced_models": list(dict.fromkeys(unpriced_models)),
        "components": {
            "llm_usd": _fixed(llm_usd.quantize(_COMPONENT_EXPONENT, rounding=ROUND_HALF_UP)),
            "compute_usd": _fixed(compute_usd.quantize(_COMPONENT_EXPONENT, rounding=ROUND_HALF_UP)),
            "overhead_usd": _fixed(card.fixed_overhead_usd.quantize(_COMPONENT_EXPONENT, rounding=ROUND_HALF_UP)),
        },
        "total_usd": _fixed(total.quantize(_QUOTE_EXPONENT, rounding=ROUND_HALF_UP)),
        # The unrounded total, kept because rounding to USDC precision is a
        # PRESENTATION step and doing it before the margin multiplication throws
        # away real money in both directions. A generation costing $0.0000001
        # displays as $0.000000; multiplying that by any margin still yields
        # zero, and the paywall would quote a free generation. `apply_margin`
        # therefore multiplies this value and rounds once, at the end.
        "total_usd_exact": _fixed(total),
        "inputs": {
            "billed_seconds": _fixed(seconds),
            "memory_gb": _fixed(card.compute_gb),
            "input_tokens": (snapshot.get("llm") or {}).get("input_tokens"),
            "output_tokens": (snapshot.get("llm") or {}).get("output_tokens"),
            "llm_calls": (snapshot.get("llm") or {}).get("calls"),
        },
    }


def assert_priceable(estimate: dict[str, Any]) -> dict[str, Any]:
    """Return ``estimate`` if it is safe to quote from; raise otherwise.

    The one call that stands between an incomplete measurement and a customer
    price. Without it, ``estimate["total_usd"]`` reads like any other number and
    an under-priced generation is indistinguishable from a cheap one.
    """
    if not estimate.get("complete"):
        raise RateCardError(
            "refusing to quote from an incomplete cost estimate: " + "; ".join(estimate.get("incomplete_reasons") or [])
        )
    return estimate


def apply_margin(estimate: dict[str, Any], *, multiplier: Decimal | str, floor_usd: Decimal | str = 0) -> str:
    """Cost → price, as a circlekit ``"$X.XXXXXX"`` string.

    The multiplier and the floor are **arguments, never defaults**: what margin
    Archimedes charges is business strategy, and this repo does not hold it.

    Multiplies the *unrounded* total and rounds once, UP, at the end — so the
    quoted price is never a fraction of a cent below the modelled cost, and a
    cost too small to display at USDC precision still quotes as one microdollar
    rather than as free.
    """
    assert_priceable(estimate)
    factor = _decimal(multiplier, where="margin multiplier")
    floor = _decimal(floor_usd, where="price floor")
    exact = estimate.get("total_usd_exact", estimate.get("total_usd"))
    priced = _decimal(exact, where="total_usd_exact") * factor
    return f"${max(priced, floor).quantize(_QUOTE_EXPONENT, rounding=ROUND_UP):.6f}"
