"""DB-level correctness tests for ``services/engagement_metrics.py`` (Insights dashboard v2).

Unlike the HTTP-layer gating tests in ``test_metrics_private_routes.py`` /
``test_metrics_routes.py`` (which mock the service boundary), these tests seed
real rows into a tmp-file SQLite via the ``redirect_to_tmp_sqlite`` precedent
(``tests/db_isolation.py`` / ``test_generation_cost_persistence.py``) and
assert against the actual ORM models + queries — the "verify each query
against the models first" requirement, made durable as a regression guard
rather than a one-time manual check.

Hermetic: tmp-file SQLite, no Postgres, no Redis, no network.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from archimedes.db import get_session
from archimedes.models.account import AuthUser, LinkedWallet
from archimedes.models.generation_cost import GenerationCostRecord
from archimedes.models.paper_store import STATUS_ACTIVE, STATUS_STOPPED, PaperDeployment
from archimedes.models.strategy_store import StrategyRecord
from archimedes.services import engagement_metrics

from tests.db_isolation import redirect_to_tmp_sqlite


@pytest.fixture
def tmp_db(tmp_path):
    yield from redirect_to_tmp_sqlite(tmp_path)


def _now() -> datetime:
    return datetime.now(UTC)


def _make_user(user_id: str, *, created_at: datetime) -> AuthUser:
    return AuthUser(
        id=user_id,
        name=f"user-{user_id}",
        email=f"{user_id}@example.test",
        email_verified=True,
        created_at=created_at,
        updated_at=created_at,
    )


def _make_strategy(
    strategy_id: str,
    *,
    owner_user_id: str | None,
    created_at: datetime,
    is_example: bool = False,
) -> StrategyRecord:
    return StrategyRecord(
        id=strategy_id,
        content_hash=f"0x{uuid.uuid4().hex}{uuid.uuid4().hex}"[:66],
        generation_method="curated" if is_example else "fusion",
        owner_user_id=owner_user_id,
        created_at=created_at,
        updated_at=created_at,
        is_example=is_example,
    )


# ── Accounts ────────────────────────────────────────────────────────────


def test_account_metrics_totals_and_windows(tmp_db):
    now = _now()
    with get_session() as session:
        session.add(_make_user("u-fresh", created_at=now - timedelta(hours=1)))
        session.add(_make_user("u-week", created_at=now - timedelta(days=5)))
        session.add(_make_user("u-month", created_at=now - timedelta(days=20)))
        session.add(_make_user("u-old", created_at=now - timedelta(days=90)))
        session.commit()

    result = engagement_metrics.get_account_metrics()
    assert result["total"] == 4
    assert result["new_7d"] == 2  # u-fresh, u-week
    assert result["new_30d"] == 3  # u-fresh, u-week, u-month


def test_account_metrics_empty_db_is_honest_zero(tmp_db):
    assert engagement_metrics.get_account_metrics() == {"total": 0, "new_7d": 0, "new_30d": 0}


def test_account_metrics_never_counts_the_platform_legacy_account(tmp_db):
    """#1283 created a real ``auth_users`` row so adopted orphan rows have an
    owner FK that resolves. It is a bookkeeping owner, not a user, and it was
    created inside a window this dashboard reports on — so if any of these
    three counts included it, every account number the product publishes
    would be overstated by one from the day that migration ran.
    """
    from archimedes.models.account import PLATFORM_LEGACY_USER_ID

    now = _now()
    with get_session() as session:
        session.add(_make_user("u-real", created_at=now - timedelta(hours=1)))
        session.add(
            AuthUser(
                id=PLATFORM_LEGACY_USER_ID,
                name="Archimedes Platform (legacy rows)",
                email="platform-legacy@archimedes.invalid",
                email_verified=False,
                created_at=now - timedelta(hours=1),
                updated_at=now - timedelta(hours=1),
            )
        )
        session.commit()

    assert engagement_metrics.get_account_metrics() == {"total": 1, "new_7d": 1, "new_30d": 1}


def test_distinct_user_count_never_counts_the_platform_legacy_account(tmp_db):
    """The same exclusion on the other account counter — ``user_stats`` feeds
    the public "users" figure, and the two must not disagree."""
    from archimedes.models.account import PLATFORM_LEGACY_USER_ID
    from archimedes.services import user_stats

    now = _now()
    with get_session() as session:
        session.add(_make_user("u-real", created_at=now))
        session.add(
            AuthUser(
                id=PLATFORM_LEGACY_USER_ID,
                name="Archimedes Platform (legacy rows)",
                email="platform-legacy@archimedes.invalid",
                email_verified=False,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

    assert user_stats._query_distinct_user_count() == 1


def test_account_new_7d_uses_same_calendar_day_window_as_strategies(tmp_db, monkeypatch):
    """Round 2 fix: accounts' `new_7d` used to be a plain rolling 168-hour
    window while strategies' `new_7d` was always calendar-day-anchored
    (midnight UTC, _TREND_DAYS days ago) — Insights.jsx renders "New accounts
    (7d)" and "Generated (7d)" side by side as if they meant the same span.
    """
    fixed_now = datetime(2026, 8, 20, 2, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(engagement_metrics, "_now", lambda: fixed_now)

    # 2026-08-13 10:00 UTC sits INSIDE a naive rolling-168h window (>=
    # fixed_now - 7d == 2026-08-13 02:00) but OUTSIDE the calendar-day window
    # strategies use (>= midnight on 2026-08-14, i.e. fixed_now - 6d floored
    # to 00:00) — exactly the gap that used to make the two "(7d)" tiles
    # measure different spans.
    edge_created = datetime(2026, 8, 13, 10, 0, 0, tzinfo=UTC)
    with get_session() as session:
        session.add(_make_user("edge-user", created_at=edge_created))
        session.commit()

    result = engagement_metrics.get_account_metrics()
    assert result["new_7d"] == 0  # excluded: before the calendar-day window start


# ── Linked wallets ──────────────────────────────────────────────────────


def test_linked_wallet_metrics_counts_rows(tmp_db):
    now = _now()
    with get_session() as session:
        session.add(_make_user("u1", created_at=now))
        session.add(_make_user("u2", created_at=now))
        session.flush()
        session.add(
            LinkedWallet(
                user_id="u1",
                normalized_identity="eip155:5042002:0x1111111111111111111111111111111111111111",
                address="0x1111111111111111111111111111111111111111",
                display_address="0x1111111111111111111111111111111111111111",
                chain_id=5042002,
                provider="metamask",
            )
        )
        session.add(
            LinkedWallet(
                user_id="u2",
                normalized_identity="eip155:5042002:0x2222222222222222222222222222222222222222",
                address="0x2222222222222222222222222222222222222222",
                display_address="0x2222222222222222222222222222222222222222",
                chain_id=5042002,
                provider="metamask",
            )
        )
        session.commit()

    assert engagement_metrics.get_linked_wallet_metrics() == {"total": 2}


# ── Timezone normalization helper ────────────────────────────────────────


def test_to_naive_utc_normalizes_aware_and_naive_datetimes():
    """Round 2 fix: strategy_store.created_at is a naive-UTC-by-convention
    column; auth_users.createdAt is genuinely tz-aware. Binding a tz-aware
    Python value straight against the naive column pushes the aware/naive
    cast onto the DB session's TimeZone setting on Postgres, which can shift
    both the trend window boundary and day-bucketing on a non-UTC session.
    """
    from datetime import timezone as tz

    est = tz(timedelta(hours=-5))
    aware_est = datetime(2026, 8, 20, 10, 0, 0, tzinfo=est)  # == 15:00 UTC
    normalized = engagement_metrics._to_naive_utc(aware_est)
    assert normalized.tzinfo is None
    assert normalized == datetime(2026, 8, 20, 15, 0, 0)

    already_naive = datetime(2026, 8, 20, 15, 0, 0)
    result = engagement_metrics._to_naive_utc(already_naive)
    assert result == already_naive
    assert result.tzinfo is None


# ── Strategies generated ────────────────────────────────────────────────


def test_strategy_metrics_total_and_daily_trend(tmp_db):
    now = _now()
    today = now.date()
    with get_session() as session:
        # Two today, one yesterday, one outside the 7-day trend window (but
        # still counted in the all-time total).
        session.add(_make_strategy("s-today-1", owner_user_id=None, created_at=now))
        session.add(_make_strategy("s-today-2", owner_user_id=None, created_at=now))
        session.add(_make_strategy("s-yesterday", owner_user_id=None, created_at=now - timedelta(days=1)))
        session.add(_make_strategy("s-ancient", owner_user_id=None, created_at=now - timedelta(days=40)))
        session.commit()

    result = engagement_metrics.get_strategy_metrics()
    assert result["total"] == 4
    assert result["new_7d"] == 3  # excludes s-ancient
    assert len(result["daily_new"]) == 7
    assert result["daily_new"][-1] == {"date": today.isoformat(), "count": 2}
    assert result["daily_new"][-2] == {"date": (today - timedelta(days=1)).isoformat(), "count": 1}
    # Zero-filled, not sparse.
    assert result["daily_new"][0]["count"] == 0


def test_strategy_metrics_excludes_platform_seeded_example_rows(tmp_db):
    """Round 2 fix: main.py seeds `is_example=True` curated rows into
    strategy_store on every startup — no user generated them, so they must
    not inflate "strategies generated" (matches strategies_routes.py:568's
    existing `is_example.is_(False)` precedent for the same table)."""
    now = _now()
    with get_session() as session:
        session.add(_make_strategy("real-1", owner_user_id=None, created_at=now))
        session.add(_make_strategy("seed-1", owner_user_id=None, created_at=now, is_example=True))
        session.add(_make_strategy("seed-2", owner_user_id=None, created_at=now, is_example=True))
        session.commit()

    result = engagement_metrics.get_strategy_metrics()
    assert result["total"] == 1
    assert result["new_7d"] == 1
    assert result["daily_new"][-1]["count"] == 1


# ── Generation costs ────────────────────────────────────────────────────


def _cost_row(job_id: str, *, input_tokens: int, output_tokens: int) -> GenerationCostRecord:
    measurement = {
        "schema": "cost_v1",
        "job_id": job_id,
        "llm": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }
    return GenerationCostRecord(
        job_id=job_id,
        strategy_id=f"strat-{job_id}",
        schema_version="cost_v1",
        measurement_json=json.dumps(measurement),
        recorded_at=_now(),
    )


def test_generation_cost_metrics_sums_tokens_and_skips_corrupt_rows(tmp_db):
    with get_session() as session:
        session.add(_cost_row("job-1", input_tokens=100, output_tokens=50))
        session.add(_cost_row("job-2", input_tokens=200, output_tokens=75))
        # Corrupt row: must be skipped from BOTH the sum and measured_count —
        # round 2 fix. measured_count counts rows that actually contributed a
        # decoded llm block, not every row.query.count() regardless of
        # whether it decoded (that inflated measured_count relative to the
        # rows the sum below it actually came from). It must ALSO increment
        # calls_missing_usage_total (round 4 fix, see below) rather than
        # silently contributing zero to the completeness flag.
        session.add(
            GenerationCostRecord(
                job_id="job-corrupt",
                strategy_id="strat-corrupt",
                schema_version="cost_v1",
                measurement_json="{not valid json",
                recorded_at=_now(),
            )
        )
        session.commit()

    result = engagement_metrics.get_generation_cost_metrics()
    assert result["measured_count"] == 2  # the corrupt row contributed nothing to the sum
    assert result["rows_total"] == 3  # ...but it still exists as a row
    assert result["total_input_tokens"] == 300
    assert result["total_output_tokens"] == 125
    assert result["total_tokens"] == 425
    # Round 4 fix: the corrupt row must NOT contribute zero to
    # calls_missing_usage_total — it is the MOST incomplete case of all (its
    # usage is entirely unreadable), so it counts as +1, and usage_complete
    # must flip to False rather than reading "complete" while a whole row
    # was silently discarded. Before this fix these were 0 / True.
    assert result["calls_missing_usage"] == 1
    assert result["usage_complete"] is False


def test_generation_cost_metrics_partial_usage_row_is_not_banked_as_measured(tmp_db):
    """Round 3 fix: a row whose llm block reports usage_complete=False (some
    calls in that job never returned usage) must not count toward
    measured_count — folding it in there is indistinguishable on the page
    from a fully-measured job. calls_missing_usage must survive to the
    aggregate payload rather than being silently dropped.

    Mutation-verified: reverting `if usage_complete: measured_count += 1` to
    the old `measured_count += 1` (unconditional) makes this assertion fail
    (`assert 2 == 1`); reverting the calls_missing_usage/usage_complete
    plumbing back out makes the KeyError-shaped assertion fail outright.
    """
    with get_session() as session:
        session.add(_cost_row("job-full", input_tokens=100, output_tokens=50))
        session.add(
            GenerationCostRecord(
                job_id="job-partial",
                strategy_id="strat-partial",
                schema_version="cost_v1",
                measurement_json=json.dumps(
                    {
                        "schema": "cost_v1",
                        "job_id": "job-partial",
                        "llm": {
                            "calls": 8,
                            "calls_missing_usage": 8,
                            "usage_complete": False,
                            "input_tokens": 0,
                            "output_tokens": 0,
                        },
                    }
                ),
                recorded_at=_now(),
            )
        )
        session.commit()

    result = engagement_metrics.get_generation_cost_metrics()
    assert result["measured_count"] == 1  # only job-full counts as measured
    assert result["rows_total"] == 2
    assert result["calls_missing_usage"] == 8
    assert result["usage_complete"] is False
    assert result["total_input_tokens"] == 100
    assert result["total_output_tokens"] == 50


def test_generation_cost_metrics_dedupes_by_job_id_for_k_greater_than_one(tmp_db):
    """Round 3 fix: models/generation_cost.py's docstring says a job-level
    measurement persisted onto more than one strategy row (K>1) must be
    counted ONCE, not once per row, or the sum double-counts. K=1 today, but
    the guard must hold the moment that changes.

    Mutation-verified: removing the `if job_id in seen_job_ids: continue`
    guard makes total_input_tokens read 1000 instead of 500.
    """
    shared_measurement = {
        "schema": "cost_v1",
        "job_id": "job-k2",
        "llm": {"input_tokens": 500, "output_tokens": 200},
    }
    with get_session() as session:
        session.add(
            GenerationCostRecord(
                job_id="job-k2",
                strategy_id="strat-a",
                schema_version="cost_v1",
                measurement_json=json.dumps(shared_measurement),
                recorded_at=_now(),
            )
        )
        session.add(
            GenerationCostRecord(
                job_id="job-k2",
                strategy_id="strat-b",
                schema_version="cost_v1",
                measurement_json=json.dumps(shared_measurement),
                recorded_at=_now(),
            )
        )
        session.commit()

    result = engagement_metrics.get_generation_cost_metrics()
    assert result["rows_total"] == 2
    assert result["measured_count"] == 1  # one job, banked once
    assert result["total_input_tokens"] == 500  # NOT 1000
    assert result["total_output_tokens"] == 200


def test_generation_cost_metrics_corrupt_and_missing_llm_rows_count_as_missing_usage(tmp_db):
    """Round 4 fix: a row whose JSON can't even be decoded, or that decodes
    but carries no `llm` block, is the MOST incomplete case there is — it
    must not contribute zero to calls_missing_usage_total (the old code
    `continue`d past both cases with no increment, so usage_complete could
    read True — "the totals below are a complete accounting" — while an
    entire row's usage was silently unaccounted for).

    Two distinct jobs here (one corrupt-JSON, one decodable-but-no-llm-block)
    so the assertion can't be satisfied by a single incidental +1 — each
    unreadable row must contribute its own increment.

    Mutation-verified: reverting either `calls_missing_usage_total += 1`
    (added in this fix) back to a bare `continue` makes this assertion fail
    (`assert 1 == 2` / `assert False is True`).
    """
    with get_session() as session:
        session.add(_cost_row("job-good", input_tokens=100, output_tokens=50))
        session.add(
            GenerationCostRecord(
                job_id="job-corrupt",
                strategy_id="strat-corrupt",
                schema_version="cost_v1",
                measurement_json="{not valid json",
                recorded_at=_now(),
            )
        )
        session.add(
            GenerationCostRecord(
                job_id="job-no-llm-block",
                strategy_id="strat-no-llm",
                schema_version="cost_v1",
                measurement_json=json.dumps({"schema": "cost_v1", "job_id": "job-no-llm-block"}),
                recorded_at=_now(),
            )
        )
        session.commit()

    result = engagement_metrics.get_generation_cost_metrics()
    assert result["rows_total"] == 3
    assert result["measured_count"] == 1  # only job-good
    assert result["calls_missing_usage"] == 2  # +1 corrupt, +1 missing-llm-block
    assert result["usage_complete"] is False
    assert result["total_input_tokens"] == 100  # only job-good's tokens
    assert result["total_output_tokens"] == 50
    assert isinstance(result["note"], str) and "measured" in result["note"].lower()


def test_generation_cost_metrics_dedupes_missing_usage_increment_by_job_id(tmp_db):
    """A K>1 job whose measurement is corrupt/missing-llm on EVERY persisted
    row must still only add +1 to calls_missing_usage_total, not once per
    row — the same job_id dedup the measured branch already gets.

    Mutation-verified: moving the `if job_id in seen_job_ids` check back to
    after the JSON-decode branches (its round-3 position) makes
    calls_missing_usage read 2 instead of 1.
    """
    with get_session() as session:
        for strategy_id in ("strat-a", "strat-b"):
            session.add(
                GenerationCostRecord(
                    job_id="job-k2-corrupt",
                    strategy_id=strategy_id,
                    schema_version="cost_v1",
                    measurement_json="{not valid json",
                    recorded_at=_now(),
                )
            )
        session.commit()

    result = engagement_metrics.get_generation_cost_metrics()
    assert result["rows_total"] == 2
    assert result["calls_missing_usage"] == 1  # banked once for job-k2-corrupt, not twice
    assert result["usage_complete"] is False


# ── Paper deployments ───────────────────────────────────────────────────


def test_paper_deployment_metrics_counts_by_status(tmp_db):
    today = date.today()
    with get_session() as session:
        session.add(PaperDeployment(strategy_id="s1", spec_json="{}", deployed_at=today, status=STATUS_ACTIVE))
        session.add(PaperDeployment(strategy_id="s2", spec_json="{}", deployed_at=today, status=STATUS_ACTIVE))
        session.add(PaperDeployment(strategy_id="s3", spec_json="{}", deployed_at=today, status=STATUS_STOPPED))
        session.commit()

    assert engagement_metrics.get_paper_deployment_metrics() == {"active": 2, "stopped": 1}


# ── Repeat-generation proxy ─────────────────────────────────────────────


def test_repeat_generation_metrics_counts_distinct_days_per_owner(tmp_db):
    now = _now()
    with get_session() as session:
        session.add(_make_user("repeat-user", created_at=now - timedelta(days=10)))
        session.add(_make_user("single-day-user", created_at=now - timedelta(days=10)))
        session.commit()
        # repeat-user: two strategies on two DIFFERENT days -> repeat.
        session.add(_make_strategy("r-1", owner_user_id="repeat-user", created_at=now - timedelta(days=3)))
        session.add(_make_strategy("r-2", owner_user_id="repeat-user", created_at=now - timedelta(days=1)))
        # single-day-user: two strategies on the SAME day -> not a repeat.
        session.add(_make_strategy("sd-1", owner_user_id="single-day-user", created_at=now.replace(hour=2)))
        session.add(_make_strategy("sd-2", owner_user_id="single-day-user", created_at=now.replace(hour=14)))
        # Pre-account (wallet-only) generation: no owner_user_id -> excluded entirely.
        session.add(_make_strategy("anon-1", owner_user_id=None, created_at=now))
        session.commit()

    result = engagement_metrics.get_repeat_generation_metrics()
    assert result["generating_users"] == 2
    assert result["repeat_users"] == 1


def test_repeat_generation_metrics_empty_db_is_honest_zero(tmp_db):
    result = engagement_metrics.get_repeat_generation_metrics()
    assert result["generating_users"] == 0
    assert result["repeat_users"] == 0


def test_repeat_generation_metrics_excludes_platform_seeded_example_rows(tmp_db):
    """Round 4 fix: get_strategy_metrics excludes is_example rows
    (main.py's curated seed set); this sibling function did not, so an
    example row with a non-NULL owner_user_id (e.g. an admin wallet credited
    as curator) could inflate that account's distinct-day count here even
    though the SAME row is excluded from "Strategies generated" — the two
    adjacent Insights.jsx tiles would silently disagree on population.

    Mutation-verified: removing the `.filter(StrategyRecord.is_example.is_(False))`
    line makes generating_users read 2 and repeat_users read 1 instead of
    0 / 0 — confirmed by hand before this fix was pushed.
    """
    now = _now()
    with get_session() as session:
        session.add(_make_user("curator", created_at=now - timedelta(days=10)))
        # Two example/seeded rows on two different days, credited to an
        # account — would look like a "repeat generator" if not excluded.
        session.add(
            _make_strategy("seed-1", owner_user_id="curator", created_at=now - timedelta(days=3), is_example=True)
        )
        session.add(
            _make_strategy("seed-2", owner_user_id="curator", created_at=now - timedelta(days=1), is_example=True)
        )
        session.commit()

    result = engagement_metrics.get_repeat_generation_metrics()
    assert result["generating_users"] == 0
    assert result["repeat_users"] == 0


def test_repeat_generation_metrics_never_counts_the_platform_legacy_account(tmp_db):
    """#1283: the adoption migration stamps ~60 real ``strategy_store`` rows
    with ``platform-legacy`` in one shot, and their original ``created_at``
    values span many calendar days. The ``is_example`` filter above does NOT
    reach them — an adopted row carries a real ``owner_wallet``, which is
    exactly what house content does not have — so without an explicit
    exclusion the bookkeeping account is counted as a generating user AND as
    a repeat one.

    That is the failure this whole family of filters exists to prevent, and
    it is published: this dict ships in ``get_engagement_snapshot`` beside
    ``account_metrics``, which IS filtered, so the two adjacent tiles would
    read different populations — one insisting the account does not exist
    while the other reports its activity.
    """
    from archimedes.models.account import PLATFORM_LEGACY_USER_ID

    now = _now()
    with get_session() as session:
        session.add(_make_user("u-real", created_at=now - timedelta(days=10)))
        session.add(
            AuthUser(
                id=PLATFORM_LEGACY_USER_ID,
                name="Archimedes Platform (legacy rows)",
                email="platform-legacy@archimedes.invalid",
                email_verified=False,
                created_at=now - timedelta(days=10),
                updated_at=now - timedelta(days=10),
            )
        )
        # One real human, one calendar day: a generating user, not a repeat one.
        session.add(_make_strategy("real-1", owner_user_id="u-real", created_at=now - timedelta(days=2)))
        # Adopted orphans: NOT is_example, spread over two calendar days —
        # the shape ~60 real prod rows will have the moment the migration runs.
        session.add(_make_strategy("orph-1", owner_user_id=PLATFORM_LEGACY_USER_ID, created_at=now - timedelta(days=5)))
        session.add(_make_strategy("orph-2", owner_user_id=PLATFORM_LEGACY_USER_ID, created_at=now - timedelta(days=3)))
        session.commit()

    result = engagement_metrics.get_repeat_generation_metrics()
    # Mutation check: drop the PLATFORM_LEGACY_USER_ID filter and this reads
    # 2 / 1 — the bookkeeping account becomes the product's repeat user.
    assert result["generating_users"] == 1
    assert result["repeat_users"] == 0
    # And the adjacent published tile agrees on the population.
    assert engagement_metrics.get_account_metrics()["total"] == 1


def test_repeat_generation_metrics_normalizes_tz_aware_timestamps_at_day_boundary(monkeypatch):
    """Round 3 fix: day-bucketing here must go through _to_naive_utc, exactly
    like _daily_buckets already does — otherwise a tz-aware created_at
    shifts which calendar day a row counts toward (_to_naive_utc's own
    docstring: "a non-UTC session would silently shift... the day a row
    buckets into" — this was the one bucketing site over
    strategy_store.created_at that claim did not actually cover).

    A real DB round-trip can't exercise this: SQLite (this suite's backend)
    silently truncates a tz-aware bind to its unconverted wall-clock value at
    INSERT time rather than reproducing the Postgres session-TimeZone cast
    hazard _to_naive_utc exists to close — so the query boundary is faked
    here instead, to hand the loop the exact tz-aware values it must
    normalize.

    Two timestamps for ONE owner, chosen so a raw `.date()` (ignoring
    tzinfo, which is what a bare `created_at.date()` does) puts BOTH on
    2026-08-13 — not a repeat — while `_to_naive_utc(...).date()` correctly
    resolves them to two DIFFERENT UTC calendar days — a repeat. Reverting
    the `_to_naive_utc(created_at)` wrap back to a bare `created_at` makes
    `repeat_users` read 0 instead of 1 (verified by hand before this fix was
    pushed).
    """
    from datetime import timezone as tz

    est = tz(timedelta(hours=-5))
    late_est = datetime(2026, 8, 13, 23, 30, 0, tzinfo=est)  # == 2026-08-14 04:30 UTC
    morning_est = datetime(2026, 8, 13, 8, 0, 0, tzinfo=est)  # == 2026-08-13 13:00 UTC

    class _FakeQuery:
        def __init__(self, rows):
            self._rows = rows

        def filter(self, *_a, **_kw):
            return self

        def yield_per(self, _n):
            return self

        def __iter__(self):
            return iter(self._rows)

    class _FakeSession:
        def __init__(self, rows):
            self._rows = rows

        def query(self, *_a, **_kw):
            return _FakeQuery(self._rows)

        def close(self):
            pass

    rows = [("tz-user", late_est), ("tz-user", morning_est)]
    monkeypatch.setattr("archimedes.db.get_session", lambda: _FakeSession(rows))

    result = engagement_metrics.get_repeat_generation_metrics()
    assert result["generating_users"] == 1
    assert result["repeat_users"] == 1  # two DIFFERENT UTC calendar days


# ── Payments (dry-run marker, no query) ─────────────────────────────────


def test_payments_snapshot_is_dry_run_with_no_settled_volume(monkeypatch):
    monkeypatch.setenv("PAYMENTS_DRY_RUN", "true")
    result = engagement_metrics.get_payments_snapshot()
    assert result["dry_run"] is True
    assert result["settled_volume_usd"] is None
    assert "dry-run" in result["note"].lower() or "dry_run" in result["note"].lower()


def test_payments_snapshot_never_claims_a_settled_number_when_dry_run_flips_off(monkeypatch):
    """Negative control: even with PAYMENTS_DRY_RUN=false, settlement_intents'
    amount_usdc column is still never populated by any code path, so there is
    still no summable settled amount — settled_volume_usd must NOT silently
    become 0 (a measured zero) just because the flag flipped. It stays None."""
    monkeypatch.setenv("PAYMENTS_DRY_RUN", "false")
    result = engagement_metrics.get_payments_snapshot()
    assert result["dry_run"] is False
    assert result["settled_volume_usd"] is None


# ── Fail-soft: a DB error degrades to None, never a measured-looking 0 ──


def test_db_failure_degrades_to_none_not_zero(monkeypatch):
    """Round 2 fix (CLAUDE.md fail-soft principle): a DB error must render as
    an honest absence, not a plausible zero indistinguishable from "queried,
    found nothing". Insights.jsx's Stat component renders None as "—" and a
    number (including 0) as a number, so returning 0 here would have made an
    outage look exactly like real, measured zero activity.

    Mutation check: reverting any one of these fields back to 0 makes this
    assertion fail (verified by hand before this fix was pushed — see the PR
    round-2 notes).
    """

    def _broken_get_session():
        raise RuntimeError("DB unavailable (simulated)")

    monkeypatch.setattr("archimedes.db.get_session", _broken_get_session)

    account = engagement_metrics.get_account_metrics()
    assert account == {"total": None, "new_7d": None, "new_30d": None, "unavailable": True}

    wallets = engagement_metrics.get_linked_wallet_metrics()
    assert wallets == {"total": None, "unavailable": True}

    strategies = engagement_metrics.get_strategy_metrics()
    assert strategies == {"total": None, "new_7d": None, "daily_new": None, "note": None, "unavailable": True}

    costs = engagement_metrics.get_generation_cost_metrics()
    assert costs == {
        "measured_count": None,
        "rows_total": None,
        "calls_missing_usage": None,
        "usage_complete": None,
        "total_input_tokens": None,
        "total_output_tokens": None,
        "total_tokens": None,
        "note": None,
        "unavailable": True,
    }

    paper = engagement_metrics.get_paper_deployment_metrics()
    assert paper == {"active": None, "stopped": None, "unavailable": True}

    repeat = engagement_metrics.get_repeat_generation_metrics()
    assert repeat == {
        "generating_users": None,
        "repeat_users": None,
        "note": "unavailable",
        "unavailable": True,
    }


# ── Composed snapshot ────────────────────────────────────────────────────


def test_engagement_snapshot_composes_every_tile(tmp_db):
    snapshot = engagement_metrics.get_engagement_snapshot()
    for key in (
        "accounts",
        "linked_wallets",
        "strategies",
        "generation_costs",
        "paper_deployments",
        "repeat_generation_users",
        "payments",
        "timestamp",
    ):
        assert key in snapshot, f"missing {key} in {list(snapshot.keys())}"
