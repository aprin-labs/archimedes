"""The intraday-marks laws (design §6 acceptance, made machine-checkable).

Hermetic: an in-memory SQLite session and a hand-rolled fake provider standing
in at the ``MarketDataProvider`` boundary. No network, no real vendor, no
clock dependence — every test drives ``now`` explicitly, because a retention
job that can only be tested by waiting eight days is a retention job nobody
tests, which is how ``backtest_results`` reached 6.3 GB.

What is pinned, one section per §6 row:

  1. CADENCE — 26 marks across a US session and ZERO overnight for an equity
     deployment; 96 across 24 hours for a crypto one.
  2. THE STALE-BAR RULE — a bar older than the staleness cap writes NO row.
     A missing mark is an honest gap; a repeated stale one is a fabricated
     flat line.
  3. THE ROW CAP — a runaway loop is refused, not absorbed.
  4. STOPPED MEANS STOPPED — a stopped deployment stops accumulating marks
     the moment the user hits Stop.
  5. RETENTION — 7d raw → hourly → deleted past 90d, with the counts logged.
  6. MARKS ARE NOT THE TRACK RECORD — ``paper_daily_returns`` row counts are
     byte-identical before and after everything above.
  7. HONESTY COLUMNS — ts is the UPSTREAM observation time, per-leg freshness
     is recorded rather than smoothed over, and source/is_delayed are stored.

Every guard in sections 2–4 was demonstrated to REJECT before this file was
pushed: each has a comment naming the exact mutation that makes it fail.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from archimedes.models.chat import Base
from archimedes.models.paper_store import (
    GRANULARITY_HOURLY,
    GRANULARITY_RAW,
    STATUS_STOPPED,
    PaperDailyReturn,
    PaperMark,
)
from archimedes.services import paper_marks
from archimedes.services.paper_marks import mark_all, rollup_and_prune
from archimedes.services.paper_trading import PositionSet, create_deployment
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

_SPY_SPEC = {
    "name": "marks probe (equity)",
    "asset_universe": ["SPY"],
    "rebalance_frequency": "daily",
    "entry": {"gt": ["momentum_20", 0]},
    "exit": {"lt": ["momentum_20", -0.99]},
    "position_sizing": {"type": "full_invested_when_in_market"},
    "source_arxiv_ids": ["0706.1497"],
    "look_ahead_safe": True,
}
_BTC_SPEC = {**_SPY_SPEC, "name": "marks probe (crypto)", "asset_universe": ["BTC-USD"]}
_MIXED_SPEC = {**_SPY_SPEC, "name": "marks probe (mixed)", "asset_universe": ["SPY", "BTC-USD"]}

# A US session, in UTC: 13:30 open, 20:00 close — 6.5 hours, which at a
# 15-minute cadence is the 26 bars §1.2's arithmetic and §6's acceptance name.
_SESSION_OPEN = datetime(2026, 8, 31, 13, 30, tzinfo=UTC)  # a Monday
_SESSION_CLOSE = datetime(2026, 8, 31, 20, 0, tzinfo=UTC)


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


class _FakeProvider:
    """Stands in at the ``MarketDataProvider`` boundary — the batch quote
    method only, which is all the marks loop touches. ``prices`` maps a vendor
    ticker to ``(price, bar_ts)``; a ticker absent from it is absent from the
    result, exactly as a real vendor miss behaves."""

    def __init__(self, prices: dict[str, tuple[float, datetime]]) -> None:
        self.prices = prices
        self.calls: list[dict[str, str]] = []

    def get_intraday_quotes_batch(self, tickers: dict[str, str]) -> dict[str, tuple[float, datetime]]:
        self.calls.append(dict(tickers))
        return {k: self.prices[t] for k, t in tickers.items() if t in self.prices}


def _deploy(session, spec: dict, *, weights: dict[str, float], ref_prices: dict[str, float], equity: float = 1.0):
    """A deployment with a stamped position cache — the state a daily advance
    leaves behind, built directly so these tests never run a replay."""
    dep = create_deployment(
        session,
        strategy_id="s1",
        spec_dict=spec,
        owner_wallet=None,
        owner_user_id="u1",
        deployed_at=_SESSION_OPEN.date(),
    )
    positions = PositionSet(as_of=_SESSION_OPEN.date(), weights=weights, ref_prices=ref_prices)
    dep.position_cache_json = positions.to_json(equity)
    dep.position_cache_at = _SESSION_OPEN
    session.flush()
    return dep


def _spy(session, **kw):
    return _deploy(session, _SPY_SPEC, weights={"SPY": 1.0}, ref_prices={"SPY": 500.0}, **kw)


def _btc(session, **kw):
    return _deploy(session, _BTC_SPEC, weights={"BTC-USD": 1.0}, ref_prices={"BTC-USD": 60000.0}, **kw)


# ── 1. Cadence: 26 marks a session, 96 a crypto day, zero overnight ─────


def _run_session(session, *, ticks: int, start_tick: datetime, price0: float = 500.0) -> None:
    """Drive the loop tick by tick. At each tick the newest bar is the one
    that closed 15 minutes ago — the live shape: at 13:45 you mark the 13:30
    bar."""
    for i in range(ticks):
        now = start_tick + timedelta(minutes=15 * i)
        bar = now - timedelta(minutes=15)
        mark_all(session, now=now, provider=_FakeProvider({"SPY": (price0 + i, bar)}))


def test_an_equity_deployment_accumulates_26_marks_across_a_us_session():
    """§6 acceptance, row 1. A 6.5-hour US session at a 15-minute cadence is
    26 bars, which is §1.2's own arithmetic."""
    with _session() as s:
        dep = _spy(s)
        _run_session(s, ticks=26, start_tick=_SESSION_OPEN + timedelta(minutes=15))
        assert s.query(PaperMark).filter_by(deployment_id=dep.id).count() == 26


def test_the_same_deployment_accumulates_zero_further_marks_overnight():
    """§6 acceptance, row 1 (the other half) — and the one that would look
    like a bug if it were not stated. An equity deployment's value is
    GENUINELY frozen after the close: the vendor keeps answering, truthfully,
    with the SAME last bar it printed at 19:45. Not one further row is written
    across the whole night — no repeated close, no drifting timestamp.

    Two independent mechanisms have to hold for this, and both are load-bearing:
    the already-marked check (the bar is not new) covers the first hour, and
    the stale-bar rule (§2.4 rule 4) covers the rest of the night.

    Demonstrated to reject: removing the ``bar_ts < cutoff`` skip in
    ``mark_deployment`` still passes here (the dedupe catches it) but fails
    ``test_a_stale_bar_writes_no_row_at_all``; removing the already-marked
    check makes THIS test fail — the two guards are checked separately for
    exactly that reason.
    """
    with _session() as s:
        dep = _spy(s)
        _run_session(s, ticks=26, start_tick=_SESSION_OPEN + timedelta(minutes=15))
        assert s.query(PaperMark).filter_by(deployment_id=dep.id).count() == 26

        last_bar = _SESSION_CLOSE - timedelta(minutes=15)  # 19:45, the last print
        t = _SESSION_CLOSE + timedelta(minutes=15)
        overnight_end = _SESSION_CLOSE + timedelta(hours=17)
        while t <= overnight_end:
            mark_all(s, now=t, provider=_FakeProvider({"SPY": (512.0, last_bar)}))
            t += timedelta(minutes=15)

        assert s.query(PaperMark).filter_by(deployment_id=dep.id).count() == 26


def test_a_crypto_deployment_accumulates_96_marks_across_24_hours():
    """§6 acceptance, row 2. Crypto has no market hours, so every tick has a
    fresh bar and every tick writes."""
    with _session() as s:
        dep = _btc(s)
        start = datetime(2026, 8, 31, 0, 0, tzinfo=UTC)
        for i in range(96):
            t = start + timedelta(minutes=15 * i)
            mark_all(s, now=t, provider=_FakeProvider({"BTC-USD": (60000.0 + i, t)}))
        assert s.query(PaperMark).filter_by(deployment_id=dep.id).count() == 96


def test_the_same_bar_twice_writes_one_row_not_two():
    """A tick that finds no NEW bar must not write a second row at the same
    observation time. Re-writing the same bar under a fresh wall clock is the
    fabricated-flat-line defect wearing a different hat, and the unique
    constraint would reject it anyway — this checks the loop refuses first,
    rather than relying on an IntegrityError to save it."""
    with _session() as s:
        dep = _btc(s)
        bar = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
        provider = _FakeProvider({"BTC-USD": (61000.0, bar)})
        mark_all(s, now=bar, provider=provider)
        mark_all(s, now=bar + timedelta(minutes=15), provider=provider)
        assert s.query(PaperMark).filter_by(deployment_id=dep.id).count() == 1


# ── 2. The stale-bar rule (§2.4 rule 4 / §6 item 4's named guard) ───────


def test_a_stale_bar_writes_no_row_at_all():
    """THE guard §6 item 4 names explicitly: "a bar older than
    PAPER_MARKS_MAX_STALENESS_MINUTES writes NO row, shown by feeding one and
    asserting the row count did not move".

    Demonstrated to reject: deleting the ``if bar_ts < cutoff`` branch in
    ``mark_deployment`` makes this test fail with 1 row — a mark stamped
    ninety minutes in the past and presented as the current value.
    """
    with _session() as s:
        dep = _btc(s)
        now = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
        before = s.query(PaperMark).filter_by(deployment_id=dep.id).count()

        stale_bar = now - timedelta(minutes=90)  # cap is 60
        mark_all(s, now=now, provider=_FakeProvider({"BTC-USD": (61000.0, stale_bar)}))

        assert s.query(PaperMark).filter_by(deployment_id=dep.id).count() == before == 0


def test_a_fresh_bar_at_the_same_instant_does_write_a_row():
    """The other side of the same guard — a guard that rejects everything is
    not a guard. Same call, same shapes, only the bar age differs."""
    with _session() as s:
        dep = _btc(s)
        now = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
        mark_all(s, now=now, provider=_FakeProvider({"BTC-USD": (61000.0, now - timedelta(minutes=5))}))
        assert s.query(PaperMark).filter_by(deployment_id=dep.id).count() == 1


def test_a_mixed_universe_marks_what_it_can_and_records_which_legs_were_fresh():
    """§1.2's awkward case: an equity leg frozen after the close beside a
    24/7 crypto leg. v1 marks what it can rather than refusing outright OR
    silently carrying the stale equity price as if it were current.

    The frozen leg contributes its weight at exactly 1.0 — the true statement
    that a closed market's price has not moved — and is ABSENT from
    ``prices_json``, so which legs were actually fresh stays recoverable.
    """
    with _session() as s:
        dep = _deploy(
            s,
            _MIXED_SPEC,
            weights={"SPY": 0.5, "BTC-USD": 0.5},
            ref_prices={"SPY": 500.0, "BTC-USD": 60000.0},
        )
        now = datetime(2026, 8, 31, 23, 0, tzinfo=UTC)
        mark_all(
            s,
            now=now,
            provider=_FakeProvider(
                {
                    "SPY": (512.0, _SESSION_CLOSE),  # 3h old — past the 60-min cap
                    "BTC-USD": (66000.0, now),  # live
                }
            ),
        )
        row = s.query(PaperMark).filter_by(deployment_id=dep.id).one()
        prices = json.loads(row.prices_json)
        assert set(prices) == {"BTC-USD"}, "a leg too stale to use must be ABSENT, not carried at a stale price"
        # 0.5 * 1.0 (frozen equity) + 0.5 * (66000/60000) = 0.5 + 0.55
        assert row.portfolio_value == pytest.approx(1.05)
        # The mark is as of the oldest CONTRIBUTING leg — the stale one is
        # excluded, so this advances instead of freezing forever.
        assert row.ts.replace(tzinfo=UTC) == now


# ── 3. The per-deployment row cap (§3.2 guard 1) ────────────────────────


def test_the_row_cap_refuses_the_insert(monkeypatch, caplog):
    """A runaway loop is caught in minutes, not in a quarterly bill.

    Demonstrated to reject: removing the ``count >= cap`` branch in
    ``mark_deployment`` makes this test fail with 3 rows instead of 2.
    """
    monkeypatch.setenv("PAPER_MARKS_MAX_ROWS_PER_DEPLOYMENT", "2")
    with _session() as s:
        dep = _btc(s)
        start = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
        with caplog.at_level("ERROR"):
            for i in range(5):
                t = start + timedelta(minutes=15 * i)
                mark_all(s, now=t, provider=_FakeProvider({"BTC-USD": (60000.0 + i, t)}))
        assert s.query(PaperMark).filter_by(deployment_id=dep.id).count() == 2
        assert any("REFUSING the insert" in r.message for r in caplog.records)


def test_the_row_cap_does_not_fire_below_the_cap(monkeypatch):
    """The reject-everything check for the guard above."""
    monkeypatch.setenv("PAPER_MARKS_MAX_ROWS_PER_DEPLOYMENT", "50")
    with _session() as s:
        dep = _btc(s)
        start = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
        for i in range(5):
            t = start + timedelta(minutes=15 * i)
            mark_all(s, now=t, provider=_FakeProvider({"BTC-USD": (60000.0 + i, t)}))
        assert s.query(PaperMark).filter_by(deployment_id=dep.id).count() == 5


# ── 4. Stopped deployments stop accumulating (§3.2 guard 3) ─────────────


def test_a_stopped_deployment_stops_being_marked():
    """The same STATUS_ACTIVE filter ``advance_all`` uses, and for the same
    reason: a stopped deployment's track record is frozen by design.

    Demonstrated to reject: dropping the ``status == STATUS_ACTIVE`` filter in
    ``mark_all`` makes the post-stop count 2 instead of 1.
    """
    with _session() as s:
        dep = _btc(s)
        t0 = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
        mark_all(s, now=t0, provider=_FakeProvider({"BTC-USD": (61000.0, t0)}))
        assert s.query(PaperMark).filter_by(deployment_id=dep.id).count() == 1

        dep.status = STATUS_STOPPED
        s.flush()

        t1 = t0 + timedelta(minutes=15)
        mark_all(s, now=t1, provider=_FakeProvider({"BTC-USD": (62000.0, t1)}))
        assert s.query(PaperMark).filter_by(deployment_id=dep.id).count() == 1


def test_a_deployment_with_no_position_cache_is_skipped_not_guessed():
    """No cache means the daily advance has not established a position set —
    there is nothing to price. The honest result is no row, which the UI
    renders as an em-dash with a reason, not a fabricated 1.0."""
    with _session() as s:
        dep = _btc(s)
        dep.position_cache_json = None
        s.flush()
        t = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
        out = mark_all(s, now=t, provider=_FakeProvider({"BTC-USD": (61000.0, t)}))
        assert out["marked"] == 0
        assert s.query(PaperMark).filter_by(deployment_id=dep.id).count() == 0


# ── 5. Retention: roll up, prune, and log the counts (§3.2) ─────────────


def _seed_raw_marks(session, dep, *, start: datetime, count: int, every_minutes: int = 15) -> None:
    for i in range(count):
        session.add(
            PaperMark(
                deployment_id=dep.id,
                ts=start + timedelta(minutes=every_minutes * i),
                prices_json='{"BTC-USD": 60000.0}',
                portfolio_value=1.0 + i / 10000,
                source="yfinance",
                is_delayed=True,
                granularity=GRANULARITY_RAW,
            )
        )
    session.flush()


def test_the_prune_job_rolls_an_eight_day_old_deployments_raw_rows_up_to_hourly(caplog):
    """§6 acceptance, row 3. Eight days back is inside the 90-day hourly tier
    and outside the 7-day raw one, so those rows roll up rather than vanish."""
    with _session() as s:
        dep = _btc(s)
        now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
        start = now - timedelta(days=8)
        _seed_raw_marks(s, dep, start=start, count=24)  # 6 hours of 15-min marks

        with caplog.at_level("INFO"):
            out = rollup_and_prune(s, now=now)

        rows = s.query(PaperMark).filter_by(deployment_id=dep.id).all()
        assert len(rows) == 6, "one survivor per hour"
        assert all(r.granularity == GRANULARITY_HOURLY for r in rows)
        assert out["promoted_hourly"] == 6
        assert out["deleted_rolled_up"] == 18
        # §3.2 guard 2: the counts are LOGGED, so table growth is visible in
        # CloudWatch without anyone running a query — the thing nobody had for
        # backtest_results.
        logged = " ".join(r.getMessage() for r in caplog.records)
        assert "rolled up" in logged and "paper_marks now holds 6 rows" in logged


def test_the_survivor_of_each_hour_is_the_last_mark_in_it():
    """ "One mark per hour" is only meaningful if WHICH mark is defined. The
    last one in the hour is the closest thing to that hour's close."""
    with _session() as s:
        dep = _btc(s)
        now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
        start = (now - timedelta(days=8)).replace(minute=0, second=0, microsecond=0)
        _seed_raw_marks(s, dep, start=start, count=4)  # :00 :15 :30 :45
        rollup_and_prune(s, now=now)
        row = s.query(PaperMark).filter_by(deployment_id=dep.id).one()
        assert row.ts.replace(tzinfo=UTC) == start + timedelta(minutes=45)


def test_rows_past_the_hourly_tier_are_deleted_not_aggregated():
    """The third tier is DELETE, deliberately. Beyond 90 days the daily close
    is already stored permanently in paper_daily_returns; rolling marks up to
    daily would be a second, less-trustworthy source of truth for a fact the
    ledger already owns."""
    with _session() as s:
        dep = _btc(s)
        now = datetime(2026, 12, 1, 12, 0, tzinfo=UTC)
        _seed_raw_marks(s, dep, start=now - timedelta(days=200), count=8)
        out = rollup_and_prune(s, now=now)
        assert out["deleted_expired"] == 8
        assert s.query(PaperMark).filter_by(deployment_id=dep.id).count() == 0


def test_recent_raw_marks_are_left_alone():
    """The reject-everything check for the retention job: inside the 7-day raw
    tier, nothing is touched."""
    with _session() as s:
        dep = _btc(s)
        now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
        _seed_raw_marks(s, dep, start=now - timedelta(hours=6), count=24)
        out = rollup_and_prune(s, now=now)
        assert out == {"deleted_expired": 0, "deleted_rolled_up": 0, "promoted_hourly": 0, "total_rows": 24}
        assert all(r.granularity == GRANULARITY_RAW for r in s.query(PaperMark).all())


def test_rerunning_the_prune_is_a_no_op():
    """The rollup rewrites in place, so a second pass finds nothing left in
    'raw' for those hours. An operator re-running it (or a cron double-fire)
    must not halve the history each time."""
    with _session() as s:
        dep = _btc(s)
        now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
        _seed_raw_marks(s, dep, start=now - timedelta(days=8), count=24)
        rollup_and_prune(s, now=now)
        after_first = s.query(PaperMark).filter_by(deployment_id=dep.id).count()
        second = rollup_and_prune(s, now=now)
        assert second["deleted_rolled_up"] == 0 and second["promoted_hourly"] == 0
        assert s.query(PaperMark).filter_by(deployment_id=dep.id).count() == after_first


# ── 6. Marks are NOT the track record (§6 acceptance, row 4) ────────────


def test_the_daily_ledger_is_untouched_by_everything_marks_do():
    """§6 acceptance, row 4, stated as the invariant it is: paper_daily_returns
    row counts AND values are identical before and after a full marking +
    retention cycle. The marks path has no write to that table at all — this
    test is what makes that a checked fact rather than a claim in a docstring.
    """
    with _session() as s:
        dep = _btc(s)
        for i in range(3):
            s.add(
                PaperDailyReturn(
                    deployment_id=dep.id,
                    date=(_SESSION_OPEN + timedelta(days=i)).date(),
                    daily_return=0.01 * (i + 1),
                )
            )
        s.flush()
        before = [(r.date, r.daily_return) for r in s.query(PaperDailyReturn).order_by(PaperDailyReturn.date)]

        start = datetime(2026, 8, 31, 0, 0, tzinfo=UTC)
        for i in range(20):
            t = start + timedelta(minutes=15 * i)
            mark_all(s, now=t, provider=_FakeProvider({"BTC-USD": (60000.0 + i, t)}))
        rollup_and_prune(s, now=start + timedelta(days=200))

        after = [(r.date, r.daily_return) for r in s.query(PaperDailyReturn).order_by(PaperDailyReturn.date)]
        assert after == before
        assert len(after) == 3


# ── 7. The honesty columns (§2.4) ──────────────────────────────────────


def test_ts_is_the_upstream_observation_time_not_the_write_time():
    """§2.4 rule 1: "a mark written at 14:47 from a 14:32 bar is a 14:32 mark".

    Demonstrated to reject: stamping ``ts=now`` instead of the bar time makes
    this fail — and it is the failure that matters most, because a wrong price
    is obvious and a wrong TIME looks exactly like a right one.
    """
    with _session() as s:
        dep = _btc(s)
        write_time = datetime(2026, 8, 31, 14, 47, tzinfo=UTC)
        bar_time = datetime(2026, 8, 31, 14, 32, tzinfo=UTC)
        mark_all(s, now=write_time, provider=_FakeProvider({"BTC-USD": (61000.0, bar_time)}))
        row = s.query(PaperMark).filter_by(deployment_id=dep.id).one()
        assert row.ts.replace(tzinfo=UTC) == bar_time
        assert row.ts.replace(tzinfo=UTC) != write_time


def test_source_and_is_delayed_are_stored_from_the_provider_not_inferred():
    with _session() as s:
        dep = _btc(s)
        t = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
        mark_all(s, now=t, provider=_FakeProvider({"BTC-USD": (61000.0, t)}))
        row = s.query(PaperMark).filter_by(deployment_id=dep.id).one()
        assert row.source == "yfinance"
        assert row.is_delayed is True  # yfinance declares its intraday feed delayed


def test_portfolio_value_is_an_index_anchored_to_the_settled_equity():
    """The intraday value continues the ledger's own index rather than
    restarting at 1.0, so the line does not jump at every daily advance."""
    with _session() as s:
        dep = _btc(s, equity=1.20)
        t = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
        mark_all(s, now=t, provider=_FakeProvider({"BTC-USD": (66000.0, t)}))  # +10% off 60000
        row = s.query(PaperMark).filter_by(deployment_id=dep.id).one()
        assert row.portfolio_value == pytest.approx(1.20 * 1.10)


def test_one_batched_vendor_call_serves_every_deployment():
    """§2.2's cost argument, checked rather than asserted: the vendor-call
    count is driven by tick cadence, not by deployment count or universe
    breadth. Three deployments over two symbols is ONE call for two tickers."""
    with _session() as s:
        _spy(s)
        _btc(s)
        _deploy(s, _MIXED_SPEC, weights={"SPY": 0.5, "BTC-USD": 0.5}, ref_prices={"SPY": 500.0, "BTC-USD": 60000.0})
        t = datetime(2026, 8, 31, 15, 0, tzinfo=UTC)
        provider = _FakeProvider({"SPY": (505.0, t), "BTC-USD": (61000.0, t)})
        out = mark_all(s, now=t, provider=provider)
        assert len(provider.calls) == 1
        assert set(provider.calls[0].values()) == {"SPY", "BTC-USD"}
        assert out["marked"] == 3


def test_the_env_knobs_fail_safe_to_their_defaults(monkeypatch, caplog):
    """A config typo must not take down a long-lived loop — the same fail-safe
    convention as ``market_data_provider._int_env``."""
    monkeypatch.setenv("PAPER_MARKS_INTERVAL_MINUTES", "not-a-number")
    with caplog.at_level("WARNING"):
        assert paper_marks.interval_minutes() == 15
    assert any("not an integer" in r.message for r in caplog.records)


# ── 8. The DISCLOSED limitations, pinned so they cannot regress silently ──
#
# These three do not test that the code is right. They test that the code is
# what the docs, the route docstring, and the UI say it is. Each one pins a
# known v1 gap with the exact fixture the disclosure describes, so:
#
#   * the gap cannot quietly widen;
#   * a FIX for the gap cannot land without touching the pin — which forces
#     the docs, the route docstring, and the UI copy to be updated in the same
#     change, instead of leaving three stale honesty claims behind.
#
# A limitation you have written down but never executed is a claim, not a
# known quantity.


def test_a_cash_sleeve_is_still_marked_as_if_invested():
    """THE cash-sleeve gap, as the disclosure states it: settled +0.00%, live
    +10.00%, on the same deployment at the same instant.

    A mark re-prices the strategy's ASSET BASKET — the sleeve weights the last
    daily settle established — and applies each asset's move since that
    settle. It has no idea whether the strategy is actually holding those
    assets: ``replay_spec`` returns dated portfolio returns, not a per-sleeve
    invested/flat vector, so a strategy whose exit condition fired and is
    sitting in CASH is marked exactly like one that is fully invested.

    The fixture is that strategy. The settled ledger records 0.00% for the day
    (it held cash; nothing moved). The underlying rose 10%. The mark reads
    +10.00%, and ``deployment_summary`` hands the UI both numbers at once.

    THIS TEST ASSERTING +10% IS NOT AN ENDORSEMENT — it is the disclosed
    behavior, pinned. If a future change gives the marks loop a real position
    vector, this test MUST fail, and whoever fixes it must then also update:
      * docs/api/paper-trading.md ("Known limitation: a mark cannot see cash")
      * ``paper_routes.get_paper_deployment_marks``'s docstring
      * ``paper_marks``'s module docstring
      * ``ui/src/paperCopy.js``'s ``MARK_BASIS_DISCLOSURE`` (and its UI test)
    That forced coupling is the entire point of pinning it here.
    """
    with _session() as s:
        dep = _spy(s)
        settle_date = _SESSION_OPEN.date()
        # The settled truth: the strategy was FLAT this day. A real 0.0, not a
        # missing row — that is what makes the divergence visible instead of
        # merely absent.
        s.add(PaperDailyReturn(deployment_id=dep.id, date=settle_date, daily_return=0.0))
        s.flush()

        now = _SESSION_OPEN + timedelta(hours=3)
        mark_all(s, now=now, provider=_FakeProvider({"SPY": (550.0, now)}))  # 500 → 550, +10%

        from archimedes.services.paper_trading import deployment_summary

        summary = deployment_summary(s, dep)
        assert summary["total_return"] == pytest.approx(0.0), "the settled ledger says the strategy made nothing"
        assert summary["latest_mark"] is not None
        live_return = summary["latest_mark"]["portfolio_value"] - 1.0
        assert live_return == pytest.approx(0.10), (
            "DISCLOSED v1 gap: the mark prices the asset basket, not the position — a cash sleeve "
            "is marked as if invested. If this now fails, the gap was closed: update the disclosure "
            "in docs/api/paper-trading.md, paper_routes, paper_marks, and ui/src/paperCopy.js."
        )
        # The gap is bounded to the DECORATION and never reaches the ledger:
        # one settled row, still 0.0, whatever the mark said.
        rows = s.query(PaperDailyReturn).filter_by(deployment_id=dep.id).all()
        assert [(r.date, r.daily_return) for r in rows] == [(settle_date, 0.0)]


def test_ts_is_the_oldest_fresh_leg_not_the_newest():
    """THE min→max mutation catcher, which nothing else in this file catches.

    ``mark_deployment`` stamps ``ts = min(fresh_bar_times)``: a portfolio is
    only as current as the stalest price inside it, and taking the newest
    would let one live leg lend its timestamp to legs that have not moved
    since — a stale price wearing a fresh label.

    Every other mixed-universe test here has exactly ONE fresh leg, so
    ``min == max`` and a mutation from one to the other passes them all. This
    test is the one with TWO fresh legs at DIFFERENT bar times, which is the
    only shape where the two differ.

    Demonstrated to reject: changing ``min(fresh_bar_times)`` to
    ``max(fresh_bar_times)`` makes this test fail (ts becomes 15:10). Measured
    under that mutation: 23 passed, 2 failed — this test and its sibling cadence
    pin below, both added here. Every PRE-EXISTING test in the file passed, which
    is the point: nothing that shipped before could catch the mutation.
    """
    with _session() as s:
        dep = _deploy(
            s,
            _MIXED_SPEC,
            weights={"SPY": 0.5, "BTC-USD": 0.5},
            ref_prices={"SPY": 500.0, "BTC-USD": 60000.0},
        )
        older = datetime(2026, 8, 31, 15, 0, tzinfo=UTC)
        newer = datetime(2026, 8, 31, 15, 10, tzinfo=UTC)
        now = datetime(2026, 8, 31, 15, 12, tzinfo=UTC)
        mark_all(
            s,
            now=now,
            provider=_FakeProvider({"SPY": (505.0, older), "BTC-USD": (61000.0, newer)}),
        )
        row = s.query(PaperMark).filter_by(deployment_id=dep.id).one()
        # Both legs really did contribute — otherwise this would pass for the
        # trivial reason that there was only one bar time to choose from.
        assert set(json.loads(row.prices_json)) == {"SPY", "BTC-USD"}
        assert row.ts.replace(tzinfo=UTC) == older
        assert row.ts.replace(tzinfo=UTC) != newer


def test_a_mixed_universe_loses_marks_while_a_frozen_leg_pins_the_stamp(monkeypatch):
    """The DISCLOSED cadence cost of ``ts`` being both the honesty stamp and
    the dedupe key (see the long comment in ``mark_deployment``).

    For as long as a frozen equity leg is still inside the staleness window it
    pins ``ts = min(fresh_bar_times)`` to its last bar, so every subsequent
    tick dedupes against the row already written at that timestamp — and the
    live crypto leg's marks are dropped. Roughly an hour of crypto marks per
    equity close, at the 60-minute default.

    Five ticks from 20:00 to 21:00, one frozen SPY bar at 19:45 and a fresh
    BTC bar at every tick:
      * the crypto-only deployment gets all five;
      * the mixed deployment gets TWO — the 19:45 row, then nothing until the
        equity leg finally ages out of the window at 21:00.

    The second half pins the part that is genuinely surprising: the staleness
    knob is NON-MONOTONIC here. RAISING the cap to 180 minutes makes it WORSE
    (one row, not two), because the frozen leg stays inside the window for the
    whole run. An operator who sees marks stalling and reaches for the
    tolerance dial would make it worse; that is exactly why it is written down
    and executed rather than left to be discovered in prod.

    This test pins v1 AS IT IS, not as it should be. Splitting the dedupe key
    from the stamp needs a second timestamp column and a migration (the unique
    constraint is on the stored ``ts``); stamping ``max`` instead is rejected
    outright because it buys cadence with a false freshness claim. If a v2
    fixes this, these counts change and the disclosure changes with them.
    """
    frozen_equity_bar = datetime(2026, 8, 31, 19, 45, tzinfo=UTC)
    ticks = [datetime(2026, 8, 31, 20, 0, tzinfo=UTC) + timedelta(minutes=15 * i) for i in range(5)]

    def _run() -> tuple[int, int]:
        with _session() as s:
            mixed = _deploy(
                s,
                _MIXED_SPEC,
                weights={"SPY": 0.5, "BTC-USD": 0.5},
                ref_prices={"SPY": 500.0, "BTC-USD": 60000.0},
            )
            crypto_only = _btc(s)
            for i, t in enumerate(ticks):
                mark_all(
                    s,
                    now=t,
                    provider=_FakeProvider(
                        {"SPY": (512.0, frozen_equity_bar), "BTC-USD": (60000.0 + i, t)},
                    ),
                )
            return (
                s.query(PaperMark).filter_by(deployment_id=mixed.id).count(),
                s.query(PaperMark).filter_by(deployment_id=crypto_only.id).count(),
            )

    mixed_at_60, crypto_at_60 = _run()
    assert crypto_at_60 == 5, "a crypto-only deployment marks every tick — nothing pins its stamp"
    assert mixed_at_60 == 2, (
        "DISCLOSED v1 cadence cost: the frozen equity leg pins ts to 19:45 until it ages past the "
        "60-minute cap, so the live crypto leg's marks are deduped away in between"
    )

    # Non-monotonic: MORE tolerance, FEWER marks.
    monkeypatch.setenv("PAPER_MARKS_MAX_STALENESS_MINUTES", "180")
    mixed_at_180, crypto_at_180 = _run()
    assert crypto_at_180 == 5, "the knob does not affect a single-leg universe"
    assert mixed_at_180 < mixed_at_60, (
        "raising PAPER_MARKS_MAX_STALENESS_MINUTES must be shown to REDUCE mixed-universe marks — "
        "the non-monotonicity is the disclosed wart, and an operator reaching for this dial to fix "
        "stalled marks would make it worse"
    )
    assert mixed_at_180 == 1
