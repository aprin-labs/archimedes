"""What a generation actually costs, in dollars, from the rows we measured (#1217).

Three pieces already existed and never met:

* :mod:`archimedes.services.cost_meter` measures one generation — tokens,
  seconds, peak RSS, writes — and holds no pricing vocabulary at all.
* :mod:`archimedes.models.generation_cost` keeps those ``cost_v1`` snapshots
  durably, one row per ``(job_id, strategy_id)``.
* :mod:`archimedes.services.generation_cost_model` converts **one** snapshot
  plus a rate card into dollars, and refuses to total anything it could not
  price.

Nothing joined them, so the admin cost dashboard's ``cost_per_generation_usd``
was a literal ``None`` placeholder while the measurements to answer it sat in
the table. This module is that join: read the measured rows, price each one,
and report the distribution — which is the number #1217 exists to produce
("until this number exists, 'near-zero marginal cost' is an assumption wearing
the clothes of a finding").

**Four rules this module does not bend.**

1. **A missing rate card is not a zero, and not an estimate.** The rates live in
   the environment, never in this repo (``CLAUDE.md`` § Project: pricing and
   margin strategy are private-docs material, and vendor prices change without
   a deploy). With no card configured, every dollar field is ``None`` and
   ``rate_card_configured`` is ``False``. There is no code path here that
   invents a price.

2. **An unpriceable run is excluded from the mean and counted out loud.** A
   snapshot whose LLM usage was incomplete, or that used a model the card does
   not price, produces an estimate with ``complete: False``; folding its partial
   total into an average would quietly report a cost lower than the truth, which
   is the specific error the issue's "do not estimate" anti-goal names. Those
   runs land in ``jobs_unpriceable`` with a per-cause tally instead.

3. **The rejected path is in the numbers.** The issue is explicit that a
   generation failing the rigor gate spends the same backtest compute and is the
   common case. Nothing here filters on outcome; every measured row is priced.
   (What is *not* here is any run that failed before persisting a strategy —
   those never get a ``generation_costs`` row at all. Stated in
   ``coverage_note`` rather than left for a reader to assume.)

4. **This is admin-only output, and it is priced.** It is served from
   ``/api/metrics/private/cost`` behind ``PLATFORM_ADMIN_WALLETS`` and nowhere
   else. Its keys are deliberately priced, so
   :func:`archimedes.services.cost_meter.assert_measurement_only` **raises** on
   this document — that is the enforcement keeping a priced rollup from ever
   being filed back as a ``cost_v1`` measurement.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any

from archimedes.services.generation_cost_model import (
    RateCard,
    estimate_cost,
    rate_card_from_env,
)

logger = logging.getLogger(__name__)

#: Bumped whenever this rollup's shape changes, same contract as ``cost_v1``
#: and ``cost_model_v1`` — a stored or screenshotted rollup stays self-describing.
SCHEMA = "cost_rollup_v1"

#: Newest-first cap on how many distinct jobs are priced in one call. The
#: aggregate needs every priced total in memory at once (a median is not
#: streamable), so this is bounded on purpose rather than growing with the
#: table. When the cap bites, ``truncated`` says so — a silently-windowed
#: average would be presented as an all-time one.
MAX_JOBS = 1000

#: Aggregate dollars are rendered at the component precision of
#: ``generation_cost_model``, not at USDC's six decimals: a compute term of
#: $0.0000015 rounds to zero at six places and would read as "compute is free",
#: which is the wrong conclusion to hand a reader. This is a report, not a
#: quote — nothing downstream charges from it.
_USD_EXPONENT = Decimal("0.00000001")


def _usd(value: Decimal) -> str:
    """Render a dollar figure in plain fixed-point, always.

    ``str(Decimal("0.00000010"))`` is ``"1.0E-7"`` — Decimal flips to scientific
    notation exactly in the range the compute term lives in, and ``1.0E-7`` is
    not a string a dashboard or a spreadsheet will read as a price.
    """
    return format(value.quantize(_USD_EXPONENT), "f")


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal(0)) / Decimal(len(values))


def _median(values: list[Decimal]) -> Decimal:
    """Median of a non-empty list. Reported beside the mean deliberately.

    One 300-second run among four 40-second ones drags a mean somewhere neither
    describes; the pair is what makes a skewed distribution visible instead of
    averaged away.
    """
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / Decimal(2)


def _decimal_or_none(raw: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _number_or_none(raw: Any) -> float | None:
    """A finite non-negative float, or ``None``.

    Same refusal ``cost_meter._coerce_count`` makes on the way in: a string, a
    bool, a NaN or a negative is *not measured*, and averaging it in would put
    a fabricated number in a column whose whole purpose is to be trustworthy.
    """
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")) or value < 0:
        return None
    return value


def _empty(
    *,
    card: RateCard | None,
    unavailable: bool = False,
    rows_scanned: int = 0,
    jobs_seen: int = 0,
) -> dict[str, Any]:
    """The shape with no priced runs in it. Every dollar field ``None``."""
    return {
        "schema": SCHEMA,
        "rate_card_configured": card is not None,
        "lane": card.lane if card is not None else None,
        "rows_scanned": rows_scanned,
        "jobs_seen": jobs_seen,
        "jobs_priced": 0,
        "jobs_unpriceable": jobs_seen,
        "unpriceable_reasons": {},
        "unpriced_models": {},
        "truncated": False,
        "cost_per_generation_usd": None,
        "by_n_candidates": [],
        "unavailable": unavailable,
        "coverage_note": _COVERAGE_NOTE,
    }


_COVERAGE_NOTE = (
    "Priced from generation_costs rows only. A run that failed before persisting a strategy "
    "(invalid brief, early crash) never gets a row, so it is absent here rather than counted "
    "as cheap. Runs that failed the rigor gate ARE included — they spend the same backtest "
    "compute. Dollar fields are null, never 0, when no rate card is configured or no run was "
    "priceable."
)


def _bucket_key(measurement: dict[str, Any]) -> int | None:
    """``meta.n_candidates_requested``, or ``None`` when the row does not say.

    The N-scaling breakdown the issue asks for ("repeated for a multi-candidate
    run so the scaling in N is visible, not assumed") is grouped on this. A row
    with no usable value gets its own ``None`` bucket rather than being folded
    into ``1`` — assuming N=1 for a run that never recorded N is exactly the
    kind of plausible substitute this instrumentation exists to end.
    """
    meta = measurement.get("meta")
    raw = meta.get("n_candidates_requested") if isinstance(meta, dict) else None
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _iter_measurements(max_jobs: int) -> tuple[list[dict[str, Any]], int, int, bool]:
    """Newest-first measurements, one per job. Raises on a DB failure.

    De-duplicated by ``job_id`` because a ``generation_costs`` row is keyed to a
    ``(job_id, strategy_id)`` pair while **the measurement is the job's**: if a
    job ever persists more than one strategy (K>1), each row carries the *same*
    job-level snapshot and pricing both would double-count that job into every
    average. K=1 today makes this a no-op in practice; it is unconditional so a
    future K>1 cannot silently start over-counting.
    """
    from archimedes.db import get_session
    from archimedes.models.generation_cost import GenerationCostRecord

    measurements: list[dict[str, Any]] = []
    rows_scanned = 0
    seen_jobs: set[str] = set()
    truncated = False
    session = get_session()
    try:
        query = (
            session.query(GenerationCostRecord.job_id, GenerationCostRecord.measurement_json)
            .order_by(GenerationCostRecord.recorded_at.desc(), GenerationCostRecord.id.desc())
            .yield_per(200)
        )
        for job_id, raw in query:
            rows_scanned += 1
            if job_id in seen_jobs:
                continue
            if len(seen_jobs) >= max_jobs:
                # Stop consuming rows, but remember that we stopped. Reporting
                # the window as if it were the whole table is the failure mode
                # this flag exists to prevent.
                truncated = True
                break
            seen_jobs.add(job_id)
            try:
                measurement = json.loads(raw) if raw else None
            except (json.JSONDecodeError, TypeError):
                logger.warning("generation_cost_rollup: corrupt measurement_json for job %s — unpriceable", job_id)
                measurement = None
            measurements.append({"job_id": job_id, "measurement": measurement})
    finally:
        session.close()
    return measurements, rows_scanned, len(seen_jobs), truncated


def get_measured_generation_cost(
    card: RateCard | None = None,
    *,
    max_jobs: int = MAX_JOBS,
) -> dict[str, Any]:
    """Price every measured generation and return the distribution.

    ``card`` defaults to :func:`generation_cost_model.rate_card_from_env`; pass
    one explicitly in tests so no rate ever has to live in the repo. With no
    card — the default in every environment that has not been given one — this
    returns the empty shape with ``rate_card_configured: False`` and does not
    touch the database: there is nothing it could honestly report.

    Fail-safe like every other read instrument on this dashboard
    (``engagement_metrics``, ``identity_metrics``, ``user_stats``): a DB error
    logs and returns ``unavailable: True`` with null dollars, never a 5xx and
    never a zero. A subsystem that cannot read must not be able to claim the
    cost is nothing.
    """
    card = card if card is not None else rate_card_from_env()
    if card is None:
        return _empty(card=None)

    try:
        measurements, rows_scanned, jobs_seen, truncated = _iter_measurements(max_jobs)
    except Exception:  # the dashboard degrades, it does not 500
        logger.exception("generation_cost_rollup: could not read generation_costs")
        return _empty(card=card, unavailable=True)

    totals: list[Decimal] = []
    llm_totals: list[Decimal] = []
    compute_totals: list[Decimal] = []
    overhead_totals: list[Decimal] = []
    reason_counts: Counter[str] = Counter()
    unpriced_model_counts: Counter[str] = Counter()
    buckets: dict[int | None, dict[str, list]] = {}

    for entry in measurements:
        measurement = entry["measurement"]
        if not isinstance(measurement, dict):
            # Corrupt or absent JSON: the most incomplete case there is, so it
            # is counted as unpriceable rather than skipped silently — a row we
            # could not read at all must not leave the totals looking complete.
            reason_counts["unreadable_measurement"] += 1
            continue
        try:
            estimate = estimate_cost(measurement, card)
        except Exception:  # one bad row must not blank the tile
            logger.exception("generation_cost_rollup: could not price job %s", entry["job_id"])
            reason_counts["pricing_error"] += 1
            continue

        if not estimate.get("complete"):
            for code in estimate.get("incomplete_reason_codes") or ["unspecified"]:
                reason_counts[code] += 1
            for model_id in estimate.get("unpriced_models") or []:
                unpriced_model_counts[model_id] += 1
            continue

        total = _decimal_or_none(estimate.get("total_usd_exact"))
        if total is None:
            reason_counts["pricing_error"] += 1
            continue
        components = estimate.get("components") or {}
        totals.append(total)
        llm_totals.append(_decimal_or_none(components.get("llm_usd")) or Decimal(0))
        compute_totals.append(_decimal_or_none(components.get("compute_usd")) or Decimal(0))
        overhead_totals.append(_decimal_or_none(components.get("overhead_usd")) or Decimal(0))

        bucket = buckets.setdefault(_bucket_key(measurement), {"totals": [], "tokens": [], "wall": []})
        bucket["totals"].append(total)
        llm_block = measurement.get("llm")
        tokens = _number_or_none((llm_block or {}).get("total_tokens")) if isinstance(llm_block, dict) else None
        if tokens is not None:
            bucket["tokens"].append(tokens)
        wall = _number_or_none(measurement.get("wall_seconds"))
        if wall is not None:
            bucket["wall"].append(wall)

    result = _empty(card=card, rows_scanned=rows_scanned, jobs_seen=jobs_seen)
    result["jobs_priced"] = len(totals)
    result["jobs_unpriceable"] = jobs_seen - len(totals)
    result["unpriceable_reasons"] = dict(sorted(reason_counts.items()))
    result["unpriced_models"] = dict(sorted(unpriced_model_counts.items()))
    result["truncated"] = truncated
    if totals:
        result["cost_per_generation_usd"] = {
            "mean": _usd(_mean(totals)),
            "median": _usd(_median(totals)),
            "min": _usd(min(totals)),
            "max": _usd(max(totals)),
            "llm_mean": _usd(_mean(llm_totals)),
            "compute_mean": _usd(_mean(compute_totals)),
            "overhead_mean": _usd(_mean(overhead_totals)),
        }
    result["by_n_candidates"] = [
        {
            # ``None`` = the run did not record N. Kept as its own row, never
            # merged into N=1, so the scaling read off this table is read off
            # runs that actually stated their N.
            "n_candidates_requested": key,
            "jobs_priced": len(bucket["totals"]),
            "mean_usd": _usd(_mean(bucket["totals"])),
            "median_usd": _usd(_median(bucket["totals"])),
            "mean_total_tokens": (sum(bucket["tokens"]) / len(bucket["tokens"])) if bucket["tokens"] else None,
            "mean_wall_seconds": (sum(bucket["wall"]) / len(bucket["wall"])) if bucket["wall"] else None,
        }
        # ``None`` sorts last; the numeric buckets ascend so the scaling in N
        # reads top to bottom.
        for key, bucket in sorted(buckets.items(), key=lambda kv: (kv[0] is None, kv[0] or 0))
    ]
    return result
