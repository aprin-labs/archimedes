"""The marks runner entrypoint — the loop contract, not the marking itself.

``services/paper_marks`` owns what a mark is (and is tested in
``tests/services/test_paper_marks.py``). This file owns the three properties
of the PROCESS around it, each of which has burned this repo before:

  1. **Fail-soft per tick.** A bad cycle logs and retries next tick and must
     never take the process down. The vendor endpoint has no SLA (#1218), and
     a crash loop over a missing decoration would be a worse outage than the
     missing decoration.
  2. **The retention job actually runs**, once per UTC day, from the same
     process and the same clock — so there is no second thing to deploy and
     no second thing to forget. ``backtest_results`` reached 6.3 GB because a
     retention job nobody scheduled is a retention job that does not exist.
  3. **The loop is testable at all.** ``max_ticks``/``sleep`` are seams for
     exactly that reason: an unbounded `while True` with a real `time.sleep`
     cannot be tested, and an untested retention job is the failure above.

Hermetic: ``run_once`` is stubbed at the module boundary, so nothing here
touches a DB, a vendor, or a clock it does not control.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from archimedes.scripts import run_paper_marks


def _no_sleep(_seconds):
    return None


def test_the_loop_survives_a_failing_tick_and_keeps_going(monkeypatch, caplog):
    """Demonstrated to reject: removing the ``except Exception`` around
    ``run_once`` makes this test error out on the first tick instead of
    reaching four."""
    calls = {"n": 0}

    def _boom(*, prune=False):
        calls["n"] += 1
        raise RuntimeError("vendor said no")

    monkeypatch.setattr(run_paper_marks, "run_once", _boom)
    with caplog.at_level("WARNING"):
        ticks = run_paper_marks.run_loop(max_ticks=4, sleep=_no_sleep)

    assert ticks == 4
    assert calls["n"] == 4
    assert any("will retry next tick" in r.getMessage() for r in caplog.records)


def test_the_prune_runs_once_per_day_not_once_per_tick(monkeypatch):
    """A 15-minute prune would re-scan the table 96 times a day for nothing;
    a prune that never fires is how the table grows without bound. Once per
    UTC day is the stated policy, so it is the thing checked."""
    seen: list[bool] = []

    def _record(*, prune=False):
        seen.append(prune)
        return {}

    fixed_day = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

    class _Clock:
        @staticmethod
        def now(tz=None):
            return fixed_day

    monkeypatch.setattr(run_paper_marks, "run_once", _record)
    monkeypatch.setattr(run_paper_marks, "datetime", _Clock)

    run_paper_marks.run_loop(max_ticks=5, sleep=_no_sleep)

    assert seen == [True, False, False, False, False]


def test_a_new_utc_day_re_arms_the_prune(monkeypatch):
    """The other half of the same guard: the day-stamp must ADVANCE, not latch.
    A prune that fires once and never again is indistinguishable from no
    prune at all after 24 hours."""
    seen: list[bool] = []
    day = {"value": datetime(2026, 9, 1, 23, 55, tzinfo=UTC)}

    def _record(*, prune=False):
        seen.append(prune)
        day["value"] += timedelta(hours=12)  # crosses midnight on the 2nd tick
        return {}

    class _Clock:
        @staticmethod
        def now(tz=None):
            return day["value"]

    monkeypatch.setattr(run_paper_marks, "run_once", _record)
    monkeypatch.setattr(run_paper_marks, "datetime", _Clock)

    run_paper_marks.run_loop(max_ticks=3, sleep=_no_sleep)

    assert seen == [True, True, False]


def test_a_failing_tick_does_not_consume_the_days_prune(monkeypatch):
    """If a crashed tick were allowed to mark the day pruned, one bad cycle at
    midnight would skip retention for the whole day — silently, and exactly on
    the day it was needed."""
    seen: list[bool] = []
    fixed_day = datetime(2026, 9, 1, 0, 5, tzinfo=UTC)

    def _first_fails(*, prune=False):
        seen.append(prune)
        if len(seen) == 1:
            raise RuntimeError("transient")
        return {}

    class _Clock:
        @staticmethod
        def now(tz=None):
            return fixed_day

    monkeypatch.setattr(run_paper_marks, "run_once", _first_fails)
    monkeypatch.setattr(run_paper_marks, "datetime", _Clock)

    run_paper_marks.run_loop(max_ticks=3, sleep=_no_sleep)

    assert seen[0] is True and seen[1] is True, "the failed tick must not have consumed the day's prune"
    assert seen[2] is False


def test_the_tick_interval_comes_from_the_configured_cadence(monkeypatch):
    """The loop sleeps the cadence, not a hardcoded 15 minutes — otherwise
    PAPER_MARKS_INTERVAL_MINUTES would be a knob that changes nothing."""
    monkeypatch.setenv("PAPER_MARKS_INTERVAL_MINUTES", "5")
    monkeypatch.setattr(run_paper_marks, "run_once", lambda **_kw: {})
    slept: list[float] = []

    run_paper_marks.run_loop(max_ticks=3, sleep=slept.append)

    assert slept == [300, 300]  # 5 minutes, and none after the final tick
