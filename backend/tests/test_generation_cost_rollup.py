"""The measured $/generation rollup (#1217) — arithmetic, and the refusals.

The rollup joins three things that already existed: measured ``cost_v1``
snapshots in ``generation_costs``, the per-snapshot pricing arithmetic in
``generation_cost_model``, and the admin cost dashboard that had a literal
``None`` where the number belonged.

What is tested first-class here is not the averaging — it is the set of inputs
that must NOT produce a number:

* no rate card → nulls, never zeros, and no DB read at all;
* a run whose LLM usage was incomplete, or whose model the card cannot price →
  excluded from the mean and counted out loud, because folding a partial total
  into an average reports a cost lower than the truth (the issue's "do not
  estimate" anti-goal);
* a corrupt row → unpriceable, not silently skipped;
* two rows for one job (K>1) → priced once.

Hermetic: a tmp-file SQLite DB via ``tests/db_isolation``, and a rate card built
in-test from invented round numbers. Real vendor rates never live in this repo.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from archimedes.db import get_session
from archimedes.models.generation_cost import GenerationCostRecord
from archimedes.services import cost_meter
from archimedes.services.generation_cost_model import RATE_CARD_ENV, RateCard
from archimedes.services.generation_cost_rollup import (
    SCHEMA,
    get_measured_generation_cost,
)

from tests.db_isolation import redirect_to_tmp_sqlite

MODEL = "amazon.nova-micro-v1:0"
OTHER_MODEL = "some.unpriced-model-v9:0"

# Invented, round rates. $1.00/Mtok in, $10.00/Mtok out, $0.001 per GB-second
# over 2 GB — so one second of compute is exactly $0.002 and the arithmetic
# below is checkable by hand.
CARD_JSON = {
    "lane": "fargate_inline",
    "compute_usd_per_gb_second": "0.001",
    "compute_gb": "2",
    "billing_granularity_seconds": "0.001",
    "models": {MODEL: {"input_usd_per_mtok": "1.00", "output_usd_per_mtok": "10.00"}},
}


@pytest.fixture
def tmp_db(tmp_path):
    yield from redirect_to_tmp_sqlite(tmp_path)


def _card() -> RateCard:
    return RateCard.from_mapping(CARD_JSON)


def _measurement(
    job_id: str,
    *,
    wall_seconds: float = 100.0,
    input_tokens: int = 1_000_000,
    output_tokens: int = 100_000,
    model: str = MODEL,
    usage_complete: bool = True,
    n_candidates: int | None = 1,
) -> dict:
    meta: dict = {"outcome": "done"}
    if n_candidates is not None:
        meta["n_candidates_requested"] = n_candidates
    return {
        "schema": "cost_v1",
        "job_id": job_id,
        "wall_seconds": wall_seconds,
        "cpu_seconds": wall_seconds * 0.9,
        "llm": {
            "calls": 3,
            "calls_missing_usage": 0 if usage_complete else 2,
            "usage_complete": usage_complete,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "by_model": {
                model: {
                    "calls": 3,
                    "calls_missing_usage": 0 if usage_complete else 2,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                }
            },
        },
        "stages": {"debate_backtest": {"wall_seconds": wall_seconds * 0.7, "cpu_seconds": 1.0, "runs": 1}},
        "writes": {"strategy_store": 1},
        "meta": meta,
    }


def _seed(measurement: dict | str, *, job_id: str, strategy_id: str | None = None, age_seconds: int = 0) -> None:
    raw = measurement if isinstance(measurement, str) else json.dumps(measurement)
    with get_session() as session:
        session.add(
            GenerationCostRecord(
                job_id=job_id,
                strategy_id=strategy_id or f"strat-{job_id}",
                schema_version="cost_v1",
                measurement_json=raw,
                recorded_at=datetime.now(UTC) - timedelta(seconds=age_seconds),
            )
        )
        session.commit()


# ── The happy path, checkable by hand ────────────────────────────────────


def test_prices_one_measured_generation(tmp_db):
    """1M in @ $1 + 100k out @ $10 = $2.00 LLM; 100s × 2GB × $0.001 = $0.20 compute."""
    _seed(_measurement("job-1"), job_id="job-1")

    result = get_measured_generation_cost(_card())

    assert result["schema"] == SCHEMA
    assert result["rate_card_configured"] is True
    assert result["lane"] == "fargate_inline"
    assert result["jobs_priced"] == 1
    assert result["jobs_unpriceable"] == 0
    per_gen = result["cost_per_generation_usd"]
    assert per_gen["llm_mean"] == "2.00000000"
    assert per_gen["compute_mean"] == "0.20000000"
    assert per_gen["mean"] == "2.20000000"
    assert per_gen["median"] == "2.20000000"


def test_mean_and_median_both_reported_for_a_skewed_distribution(tmp_db):
    """A long tail must stay visible, not be averaged into a single number."""
    _seed(_measurement("job-a", wall_seconds=100.0), job_id="job-a")
    _seed(_measurement("job-b", wall_seconds=100.0), job_id="job-b")
    _seed(_measurement("job-c", wall_seconds=1000.0), job_id="job-c")

    per_gen = get_measured_generation_cost(_card())["cost_per_generation_usd"]

    assert per_gen["min"] == "2.20000000"
    assert per_gen["max"] == "4.00000000"  # $2 LLM + 1000s × 2GB × $0.001
    assert per_gen["median"] == "2.20000000"
    assert per_gen["mean"] == "2.80000000"


# ── The refusals. Each one is a case that must NOT produce a number ───────


def test_no_rate_card_yields_nulls_not_zeros(tmp_db):
    """The whole point: an absent price is an absence, never $0.00.

    Also asserts the string forms — a dashboard that received ``"0.00000000"``
    would render "$0.00 per generation", which is a claim, not a gap.
    """
    _seed(_measurement("job-1"), job_id="job-1")

    result = get_measured_generation_cost(None)

    assert result["rate_card_configured"] is False
    assert result["cost_per_generation_usd"] is None
    assert result["by_n_candidates"] == []
    assert result["lane"] is None
    assert "0" not in json.dumps(result["cost_per_generation_usd"])


def test_no_rate_card_does_not_read_the_database(monkeypatch):
    """No card ⇒ nothing to report ⇒ no query. Deliberately has NO tmp_db fixture.

    If the no-card path ever started reading, this test would hit whatever
    process-global engine happens to be bound and the failure would be a
    confusing one somewhere else; making the absence of a DB read explicit is
    cheaper than debugging that later.
    """
    monkeypatch.delenv(RATE_CARD_ENV, raising=False)

    def _explode():
        raise AssertionError("the no-rate-card path must not open a DB session")

    monkeypatch.setattr("archimedes.db.get_session", _explode)

    result = get_measured_generation_cost()
    assert result["rate_card_configured"] is False
    assert result["cost_per_generation_usd"] is None


def test_incomplete_llm_usage_is_excluded_from_the_mean(tmp_db):
    """A ``usage_complete: false`` run is a floor, not a total.

    Adversarial shape: the incomplete run is the CHEAP one. If it were folded
    in, the mean would drop to $1.60 and the dashboard would report a cost
    below the measured truth — the failure this exclusion exists to prevent.
    """
    _seed(_measurement("job-full", wall_seconds=100.0), job_id="job-full")
    _seed(
        _measurement("job-partial", wall_seconds=100.0, input_tokens=200_000, output_tokens=0, usage_complete=False),
        job_id="job-partial",
    )

    result = get_measured_generation_cost(_card())

    assert result["jobs_seen"] == 2
    assert result["jobs_priced"] == 1
    assert result["jobs_unpriceable"] == 1
    assert result["unpriceable_reasons"] == {"llm_usage_incomplete": 1}
    assert result["cost_per_generation_usd"]["mean"] == "2.20000000"  # not 1.60


def test_model_the_card_cannot_price_is_excluded_and_named(tmp_db):
    """An unknown model must not silently contribute only its compute term."""
    _seed(_measurement("job-known"), job_id="job-known")
    _seed(_measurement("job-unknown", model=OTHER_MODEL), job_id="job-unknown")

    result = get_measured_generation_cost(_card())

    assert result["jobs_priced"] == 1
    assert result["unpriceable_reasons"] == {"model_not_on_rate_card": 1}
    assert result["unpriced_models"] == {OTHER_MODEL: 1}
    assert result["cost_per_generation_usd"]["mean"] == "2.20000000"


def test_corrupt_row_is_counted_unpriceable_not_skipped(tmp_db):
    """A row we cannot read at all is the MOST incomplete case.

    Dropping it silently would leave ``jobs_priced == jobs_seen`` and the
    totals looking like a complete accounting of the table.
    """
    _seed(_measurement("job-good"), job_id="job-good")
    _seed("{not valid json", job_id="job-corrupt")

    result = get_measured_generation_cost(_card())

    assert result["jobs_seen"] == 2
    assert result["jobs_priced"] == 1
    assert result["jobs_unpriceable"] == 1
    assert result["unpriceable_reasons"] == {"unreadable_measurement": 1}


def test_k_gt_1_rows_for_one_job_are_priced_once(tmp_db):
    """The row is (job, strategy); the MEASUREMENT is the job's.

    Two strategy rows carrying the same job-level snapshot must not double the
    job into the average — ``models/generation_cost.py``: "summing across rows
    would double-count".
    """
    snapshot = _measurement("job-k")
    _seed(snapshot, job_id="job-k", strategy_id="strat-a")
    _seed(snapshot, job_id="job-k", strategy_id="strat-b")

    result = get_measured_generation_cost(_card())

    assert result["rows_scanned"] == 2
    assert result["jobs_seen"] == 1
    assert result["jobs_priced"] == 1


def test_db_failure_degrades_to_unavailable_not_zero(tmp_db, monkeypatch):
    """A read instrument that cannot read must not report a cost of nothing."""

    def _boom():
        raise RuntimeError("database is down")

    monkeypatch.setattr("archimedes.db.get_session", _boom)

    result = get_measured_generation_cost(_card())

    assert result["unavailable"] is True
    assert result["cost_per_generation_usd"] is None
    assert result["jobs_priced"] == 0


# ── The N-scaling breakdown the issue explicitly asks for ────────────────


def test_by_n_candidates_makes_the_scaling_in_n_visible(tmp_db):
    """ "Repeated for a multi-candidate run so the scaling in N is visible."""
    _seed(_measurement("job-n1", wall_seconds=100.0, n_candidates=1), job_id="job-n1")
    _seed(
        _measurement("job-n5", wall_seconds=500.0, input_tokens=5_000_000, output_tokens=500_000, n_candidates=5),
        job_id="job-n5",
    )

    buckets = get_measured_generation_cost(_card())["by_n_candidates"]

    assert [b["n_candidates_requested"] for b in buckets] == [1, 5]
    assert buckets[0]["mean_usd"] == "2.20000000"
    # $5 in + $5 out = $10 LLM; 500s × 2GB × $0.001 = $1 compute.
    assert buckets[1]["mean_usd"] == "11.00000000"
    assert buckets[0]["mean_wall_seconds"] == 100.0
    assert buckets[1]["mean_total_tokens"] == 5_500_000.0


def test_run_that_did_not_record_n_gets_its_own_bucket(tmp_db):
    """Never folded into N=1 — assuming an unrecorded N is a fabricated fact."""
    _seed(_measurement("job-known-n", n_candidates=1), job_id="job-known-n")
    _seed(_measurement("job-unknown-n", n_candidates=None), job_id="job-unknown-n")

    buckets = get_measured_generation_cost(_card())["by_n_candidates"]

    assert [b["n_candidates_requested"] for b in buckets] == [1, None]
    assert all(b["jobs_priced"] == 1 for b in buckets)


# ── The structural guard: a priced rollup can never be filed as a cost_v1 ─


def test_rollup_is_structurally_barred_from_the_measurement_column(tmp_db):
    """``assert_measurement_only`` must RAISE on this document.

    That refusal is what keeps ``generation_costs.measurement_json`` free of
    pricing — the two-column design of #1326 enforced, not merely intended. A
    rollup that passed this screen would be storable as a measurement.
    """
    _seed(_measurement("job-1"), job_id="job-1")
    result = get_measured_generation_cost(_card())

    with pytest.raises(cost_meter.PricingLeakError):
        cost_meter.assert_measurement_only(result, where="generation_costs.measurement_json")


def test_truncation_is_reported_rather_than_silently_windowed(tmp_db):
    """A capped window must say so; otherwise it reads as an all-time average."""
    for i in range(3):
        _seed(_measurement(f"job-{i}"), job_id=f"job-{i}", age_seconds=i)

    result = get_measured_generation_cost(_card(), max_jobs=2)

    assert result["truncated"] is True
    assert result["jobs_seen"] == 2
    assert result["jobs_priced"] == 2
