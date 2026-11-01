from __future__ import annotations

import copy
from datetime import date

import pytest
from archimedes.models.backtest import BacktestResult
from archimedes.models.backtest_store import BacktestResultRecord
from archimedes.models.chat import Base
from archimedes.services.backtest_mapper import canonical_artifact_hash
from archimedes.services.backtest_repository import (
    get_all_daily_returns,
    insert_backtest_if_missing,
    latest_backtests_by_strategy,
)
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker


def _sample_result(strategy_id: str, sharpe: float) -> BacktestResult:
    return BacktestResult(
        strategy_id=strategy_id,
        sharpe_ratio=sharpe,
        sortino_ratio=0.5,
        max_drawdown=0.2,
        cagr=0.1,
        calmar_ratio=0.5,
        win_rate=0.5,
        profit_factor=1.2,
        total_trades=10,
        avg_holding_period_days=5.0,
        correlation_to_spy=0.3,
        correlation_to_btc=0.1,
        equity_curve=[100000, 101000],
        monthly_returns=[0.01],
        backtest_start=date(2020, 1, 1),
        backtest_end=date(2020, 12, 31),
        # Required: insert_backtest_if_missing refuses an unattributed row, so
        # a row can always be traced to the engine that produced it.
        backtest_engine="backtrader",
    )


def test_insert_backtest_is_idempotent_on_content_hash() -> None:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(bind=engine)
    Base.metadata.create_all(bind=engine)

    with SessionLocal() as session:
        row1, inserted1 = insert_backtest_if_missing(
            session,
            strategy_id="s1",
            content_hash="abc123",
            result=_sample_result("s1", sharpe=0.7),
            run_id="run1",
            source_pipeline="test",
        )
        row1_id = row1.id
        session.commit()

    with SessionLocal() as session:
        row2, inserted2 = insert_backtest_if_missing(
            session,
            strategy_id="s1",
            content_hash="abc123",
            result=_sample_result("s1", sharpe=0.9),
            run_id="run2",
            source_pipeline="test",
        )
        session.commit()

        rows = session.query(BacktestResultRecord).all()
        assert inserted1 is True
        assert inserted2 is False
        assert row1_id == row2.id
        assert len(rows) == 1


def test_insert_backtest_if_missing_skips_duplicate_run_with_only_run_id_and_timestamp_diff() -> None:
    """End-to-end reproduction of issue #1347's production symptom: two
    "refreshes" of the SAME strategy content (identical metrics, differing
    only in run_id/timestamp_utc — exactly what a scheduled re-run of
    run_backtests.py produces on a container restart) must collapse to ONE
    row via content_hash, not two.

    Mutation-proven: with canonical_artifact_hash reverted to hash the whole
    payload (no volatile-key exclusion), `hash_a != hash_b` below and this
    test fails with `inserted2 is True` / `len(rows) == 2`. See the PR body
    for the revert/re-apply transcript.
    """
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(bind=engine)
    Base.metadata.create_all(bind=engine)

    payload_a = {
        "run_id": "20260518T223743Z",
        "timestamp_utc": "2026-05-18T22:37:43.964677+00:00",
        "strategy": {"backtest_code_hash": "sha256:deadbeef"},
        "assumptions": {"transaction_cost_bps": 10},
        "results": [{"operation": "SPY", "metrics": {"sharpe_ratio": 0.71, "total_trades": 12}}],
    }
    payload_b = copy.deepcopy(payload_a)
    payload_b["run_id"] = "20260519T010101Z"
    payload_b["timestamp_utc"] = "2026-05-19T01:01:01.000000+00:00"

    hash_a = canonical_artifact_hash(payload_a)
    hash_b = canonical_artifact_hash(payload_b)
    assert hash_a == hash_b, "two runs over identical content must produce equal canonical hashes"

    with SessionLocal() as session:
        row1, inserted1 = insert_backtest_if_missing(
            session,
            strategy_id="s1",
            content_hash=hash_a,
            result=_sample_result("s1", sharpe=0.71),
            run_id=payload_a["run_id"],
            source_pipeline="run_backtests",
        )
        session.commit()

        row2, inserted2 = insert_backtest_if_missing(
            session,
            strategy_id="s1",
            content_hash=hash_b,
            result=_sample_result("s1", sharpe=0.71),
            run_id=payload_b["run_id"],
            source_pipeline="run_backtests",
        )
        session.commit()

        rows = session.query(BacktestResultRecord).filter(BacktestResultRecord.strategy_id == "s1").all()

    assert inserted1 is True
    assert inserted2 is False, "the second (content-identical) refresh must be SKIPPED, not re-inserted"
    assert row1.id == row2.id
    assert len(rows) == 1


def test_latest_backtests_by_strategy_picks_newest_row() -> None:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(bind=engine)
    Base.metadata.create_all(bind=engine)

    with SessionLocal() as session:
        insert_backtest_if_missing(
            session,
            strategy_id="s1",
            content_hash="h1",
            result=_sample_result("s1", sharpe=0.5),
            run_id="run1",
            source_pipeline="test",
        )
        insert_backtest_if_missing(
            session,
            strategy_id="s1",
            content_hash="h2",
            result=_sample_result("s1", sharpe=0.8),
            run_id="run2",
            source_pipeline="test",
        )
        insert_backtest_if_missing(
            session,
            strategy_id="s2",
            content_hash="h3",
            result=_sample_result("s2", sharpe=1.1),
            run_id="run3",
            source_pipeline="test",
        )
        session.commit()

        latest = latest_backtests_by_strategy(session, ["s1", "s2"])

    assert latest["s1"].sharpe_ratio == 0.8
    assert latest["s2"].sharpe_ratio == 1.1


def test_omitted_source_git_sha_stamps_the_running_build() -> None:
    """Omitting the argument means "stamp the running build"."""
    import os
    from unittest.mock import patch

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(bind=engine)
    Base.metadata.create_all(bind=engine)

    with patch.dict(os.environ, {"ARCHIMEDES_GIT_SHA": "deadbeefcafe"}), SessionLocal() as session:
        row, _ = insert_backtest_if_missing(
            session,
            strategy_id="s1",
            content_hash="omitted",
            result=_sample_result("s1", sharpe=0.7),
            run_id="run1",
            source_pipeline="test",
        )
        assert row.source_git_sha == "deadbeefcafe"


def test_explicit_none_source_git_sha_persists_null_even_with_env_set() -> None:
    """An EXPLICIT None means "the producing commit is not knowable" and must
    persist NULL — even when ARCHIMEDES_GIT_SHA is set.

    This is the case that matters: seed_backtests_from_artifacts.py replays
    artifacts produced by some earlier, unrecorded commit. If explicit-unknown
    collapsed into omission, every replayed row would claim the CURRENT deploy
    SHA — a confident, false provenance claim in the very column added to make
    provenance trustworthy. A plausible wrong SHA is worse than no SHA.

    Note the env var is deliberately SET here. With it unset the test would
    pass whether or not the sentinel exists, and would prove nothing.
    """
    import os
    from unittest.mock import patch

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(bind=engine)
    Base.metadata.create_all(bind=engine)

    with patch.dict(os.environ, {"ARCHIMEDES_GIT_SHA": "deadbeefcafe"}), SessionLocal() as session:
        row, _ = insert_backtest_if_missing(
            session,
            strategy_id="s2",
            content_hash="explicit-none",
            result=_sample_result("s2", sharpe=0.7),
            run_id="run2",
            source_pipeline="seed_from_artifacts",
            source_git_sha=None,
        )
        assert row.source_git_sha is None


def test_replay_script_passes_explicit_none() -> None:
    """Wiring guard: the replay script must pass ``source_git_sha=None``.

    Asserted against the parsed AST, not a substring of the source: a raw
    ``"source_git_sha=None" in inspect.getsource(...)`` check would still pass
    if the real keyword argument were deleted and the text survived in a
    comment or docstring. That is the same vacuous-guard shape this repo keeps
    producing, so the guard itself is written to be unfoolable.
    """
    import ast
    import inspect

    from archimedes.scripts import seed_backtests_from_artifacts as mod

    tree = ast.parse(inspect.getsource(mod))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "insert_backtest_if_missing"
    ]
    assert calls, "seed_backtests_from_artifacts no longer calls insert_backtest_if_missing"

    for call in calls:
        kw = {k.arg: k.value for k in call.keywords}
        assert "source_git_sha" in kw, "replay must pass source_git_sha explicitly, not omit it"
        assert isinstance(kw["source_git_sha"], ast.Constant) and kw["source_git_sha"].value is None, (
            "replay must pass source_git_sha=None (explicit unknown), not the running build's SHA"
        )


# ── Bounded reader (2026-08-19 OOM regression guard) ─────────────────────────
#
# `latest_backtests_by_strategy` used to `.all()` every row for every strategy
# and reduce in Python. Because `canonical_artifact_hash` includes `run_id` (a
# timestamp), `insert_backtest_if_missing` never dedupes, so the table grows by
# ~30 rows on every refresh cycle forever. Each row carries a ~489 KB
# artifact_json, and the old read cost a measured 0.58 MB of peak RSS per
# persisted row — the staircase that pushed the backend past its 3072 MB task
# budget on 2026-08-19. These tests pin the "resolve in SQL, hydrate only the
# winners" contract.


def _seeded_session(n_strategies: int, n_cycles: int):
    """A session over a DB holding n_strategies x n_cycles rows."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    ids = [f"strat_{i:03d}" for i in range(n_strategies)]
    for cycle in range(n_cycles):
        for sid in ids:
            # sharpe encodes the cycle so we can assert WHICH row came back.
            insert_backtest_if_missing(
                session,
                strategy_id=sid,
                content_hash=f"{sid}-cycle-{cycle}",
                result=_sample_result(sid, float(cycle)),
                source_pipeline="run_backtests",
                run_id=f"run-{cycle}",
            )
    session.commit()
    return session, ids


def test_latest_backtests_returns_the_newest_row_per_strategy() -> None:
    session, ids = _seeded_session(n_strategies=5, n_cycles=8)
    try:
        latest = latest_backtests_by_strategy(session, ids)
        assert set(latest) == set(ids)
        # Cycle 7 was inserted last, so it must be the one returned for each id.
        for sid in ids:
            assert latest[sid].sharpe_ratio == 7.0, sid
    finally:
        session.close()


def test_latest_backtests_hydrates_only_one_row_per_strategy() -> None:
    """THE GUARD: 34 strategies x 20 cycles = 680 rows on disk must still
    hydrate exactly 34 ORM objects. The old `.all()` implementation loaded all
    680 — this assertion is what makes that regression impossible to reintroduce.
    """
    n_strategies, n_cycles = 34, 20
    session, ids = _seeded_session(n_strategies=n_strategies, n_cycles=n_cycles)
    try:
        total_rows = session.query(BacktestResultRecord).count()
        assert total_rows == n_strategies * n_cycles, "seed did not produce duplicates"

        session.expunge_all()

        # Count hydration via the mapper "load" event, which fires once per row
        # the ORM materialises from the DB. Counting session.identity_map instead
        # does NOT work: the identity map holds WEAK references, so rows the old
        # implementation loaded and then discarded are garbage-collected before
        # the assertion runs and the test passes against the unfixed code.
        loaded: list[str] = []

        def _count(target, _context) -> None:
            loaded.append(target.strategy_id)

        event.listen(BacktestResultRecord, "load", _count)
        try:
            latest = latest_backtests_by_strategy(session, ids)
        finally:
            event.remove(BacktestResultRecord, "load", _count)

        assert len(latest) == n_strategies
        assert len(loaded) == n_strategies, (
            f"hydrated {len(loaded)} BacktestResultRecord rows to return {n_strategies}; "
            f"the reader is loading the whole table ({total_rows} rows on disk)"
        )
    finally:
        session.close()


def test_latest_backtests_ignores_unrequested_strategies() -> None:
    session, ids = _seeded_session(n_strategies=6, n_cycles=3)
    try:
        subset = ids[:2]
        latest = latest_backtests_by_strategy(session, subset)
        assert set(latest) == set(subset)
    finally:
        session.close()


def test_latest_backtests_empty_ids_short_circuits() -> None:
    session, _ = _seeded_session(n_strategies=2, n_cycles=2)
    try:
        assert latest_backtests_by_strategy(session, []) == {}
    finally:
        session.close()


# ── Query SHAPE: the hot read must not select artifact_json (issue #1543) ──
#
# The row-count tests above pin how MANY rows this reader hydrates. They say
# nothing about how WIDE each row is, and that is the other half of the same
# defect: the outer query selected every column of the winners, so each
# returned row dragged its `artifact_json` blob (~349 KB/row by this repo's own
# verified 2026-08-19 averages, quoted in scripts/archive_backtest_results.py)
# across the wire on a path that never reads it. These tests pin the projection.


def _capture_sql(engine) -> tuple[list[str], object]:
    """Attach a before_cursor_execute listener; return (statements, detach)."""
    statements: list[str] = []

    def _on_exec(_conn, _cursor, statement, _params, _context, _executemany) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _on_exec)

    def _detach() -> None:
        event.remove(engine, "before_cursor_execute", _on_exec)

    return statements, _detach


def _selects_from_backtest_results(statements: list[str]) -> list[str]:
    """The captured statements that are SELECTs against backtest_results."""
    return [s for s in statements if s.lstrip().upper().startswith("SELECT") and "backtest_results" in s]


def _seeded_session_with_artifacts(n_strategies: int, n_cycles: int, artifact_bytes: int = 4096):
    """Like _seeded_session, but every row carries a non-empty artifact_json.

    A NULL blob would make "the reader does not transfer it" trivially true, so
    the fixture gives the deferred column real content to omit.
    """
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    ids = [f"strat_{i:03d}" for i in range(n_strategies)]
    blob = '{"results": [{"metrics": {"pad": "' + ("x" * artifact_bytes) + '"}}]}'
    for cycle in range(n_cycles):
        for sid in ids:
            insert_backtest_if_missing(
                session,
                strategy_id=sid,
                content_hash=f"{sid}-cycle-{cycle}",
                result=_sample_result(sid, float(cycle)),
                source_pipeline="run_backtests",
                run_id=f"run-{cycle}",
                artifact_json=blob,
            )
    session.commit()
    return session, SessionLocal, ids


def test_latest_backtests_does_not_select_artifact_json() -> None:
    """THE GUARD (#1543): the hot read must not put artifact_json in its SELECT.

    Reverting the `.options(defer(...))` in latest_backtests_by_strategy makes
    this fail — the ORM renders `backtest_results.artifact_json` in the column
    list again.
    """
    session, _SessionLocal, ids = _seeded_session_with_artifacts(n_strategies=6, n_cycles=3)
    try:
        session.expunge_all()
        statements, detach = _capture_sql(session.get_bind())
        try:
            latest = latest_backtests_by_strategy(session, ids)
        finally:
            detach()

        assert set(latest) == set(ids)

        selects = _selects_from_backtest_results(statements)
        # Non-vacuity: if the listener never fired (wrong engine, wrong event)
        # the "artifact_json absent" assertion below would pass over an empty
        # list and guard nothing. Pin that a real, recognisable ORM projection
        # was observed before trusting its contents.
        assert selects, "captured no SELECT against backtest_results — the listener did not fire"
        assert any("backtest_results.sharpe_ratio" in s for s in selects), (
            "captured no hydrating SELECT with a column list; "
            f"the assertion below would be vacuous. statements={selects!r}"
        )

        offenders = [s for s in selects if "artifact_json" in s]
        assert not offenders, (
            "latest_backtests_by_strategy selected artifact_json "
            f"(~349 KB/row) on a path that never reads it: {offenders!r}"
        )
    finally:
        session.close()


def test_sql_capture_detects_artifact_json_when_it_is_present() -> None:
    """ADVERSARIAL CONTROL: feed the guard a query that SHOULD fail it.

    The un-projected query below is exactly what latest_backtests_by_strategy
    emitted before the fix. If `_selects_from_backtest_results` + the substring
    check could not see artifact_json here, the test above would be a
    tautology that passes against the unfixed code too.
    """
    session, _SessionLocal, ids = _seeded_session_with_artifacts(n_strategies=3, n_cycles=2)
    try:
        session.expunge_all()
        statements, detach = _capture_sql(session.get_bind())
        try:
            # No .options(defer(...)) — the pre-fix shape.
            session.query(BacktestResultRecord).filter(BacktestResultRecord.strategy_id.in_(ids)).all()
        finally:
            detach()

        selects = _selects_from_backtest_results(statements)
        assert selects, "captured no SELECT against backtest_results"
        assert any("artifact_json" in s for s in selects), (
            "the detector failed to see artifact_json in an un-projected SELECT, "
            "so it cannot prove the projected one omits it"
        )
    finally:
        session.close()


def test_deferred_artifact_json_does_not_change_what_callers_read() -> None:
    """Projection is a transfer-size change, not a data change.

    Every field to_backtest_result() exposes must survive, equity_curve
    (deliberately NOT deferred — api/risk_routes.py consumes it off the
    provider cache) included, and reading them must emit no follow-up SQL.
    """
    session, _SessionLocal, ids = _seeded_session_with_artifacts(n_strategies=4, n_cycles=3)
    try:
        session.expunge_all()
        latest = latest_backtests_by_strategy(session, ids)

        statements, detach = _capture_sql(session.get_bind())
        try:
            results = {sid: row.to_backtest_result() for sid, row in latest.items()}
        finally:
            detach()

        assert not _selects_from_backtest_results(statements), (
            "to_backtest_result() triggered a lazy load — the deferred column "
            "is being touched on the hot path after all"
        )
        for sid in ids:
            res = results[sid]
            assert res.strategy_id == sid
            assert res.sharpe_ratio == 2.0  # newest cycle
            assert res.equity_curve == [100000, 101000]
            assert res.monthly_returns == [0.01]
            assert res.backtest_engine == "backtrader"
            assert res.backtest_start == date(2020, 1, 1)
    finally:
        session.close()


def test_deferred_artifact_json_is_still_reachable_in_session() -> None:
    """Deferral must not make the column unreadable — only unfetched by default.

    `defer` (not `defer(..., raiseload=True)`) is the deliberate choice: a
    caller that does touch the attribute inside the session gets one extra
    SELECT rather than an exception that strategy_provider._load_backtests
    would swallow into "no backtests at all".
    """
    session, _SessionLocal, ids = _seeded_session_with_artifacts(n_strategies=2, n_cycles=2)
    try:
        session.expunge_all()
        latest = latest_backtests_by_strategy(session, ids)
        row = latest[ids[0]]
        assert row.artifact_json is not None
        assert "results" in row.artifact_json
    finally:
        session.close()


def test_provider_backtest_load_does_not_select_artifact_json(tmp_path, monkeypatch) -> None:
    """The production hot path, not just the repository helper.

    LocalStrategyProvider._load_backtests is what runs on every
    default_provider() construction (issue #1543's amplifier). Mocked at the
    DB boundary only — the session factory — so the real provider method,
    the real repository query and the real ORM mapping all execute.
    """
    from archimedes.services import strategy_provider as sp

    session, SessionLocal, ids = _seeded_session_with_artifacts(n_strategies=5, n_cycles=3)
    engine = session.get_bind()
    session.close()

    monkeypatch.setattr(sp, "get_session", SessionLocal)

    # A non-existent strategies dir makes refresh() return early without any DB
    # access, so construction cannot pollute the capture below.
    provider = sp.LocalStrategyProvider(tmp_path / "no-such-strategies-dir")

    statements, detach = _capture_sql(engine)
    try:
        loaded = provider._load_backtests(ids)
    finally:
        detach()

    assert set(loaded) == set(ids)
    selects = _selects_from_backtest_results(statements)
    assert selects, "provider hot path emitted no SELECT against backtest_results"
    assert any("backtest_results.sharpe_ratio" in s for s in selects), f"no hydrating SELECT captured: {selects!r}"
    assert not [s for s in selects if "artifact_json" in s], (
        f"provider hot path still transfers artifact_json: {[s for s in selects if 'artifact_json' in s]!r}"
    )


# ── Batched cohort read: ONE query, not one per strategy (issue #1662) ───────
#
# `get_all_daily_returns` was a Python loop over `get_daily_returns`, i.e. N
# un-projected single-row reads each dragging a ~349 KB artifact_json blob
# through the wire and through json.loads on the event loop. The Library runs it
# over the full curated cohort and /api/selection-bias/gate runs the IDENTICAL
# read over the IDENTICAL ids from the same page load, so one Library mount paid
# it twice (~24 MB, 68 deserializations; /api/strategies/ p50 12.85 s,
# /api/selection-bias/gate p50 15.62 s — 2026-08-31 ALB evidence sprint).
#
# The shared rigor cache cannot absorb this: the returns ARE the cohort cache
# key (rigor_cache.cohort_key), so a cache HIT still pays the full read. That
# also makes the key-invariance test below load-bearing, not decorative.


def _legacy_get_all_daily_returns(session, strategy_ids: list[str]) -> dict[str, list[float]]:
    """The PRE-#1662 implementation, copied verbatim, as the parity oracle.

    Comparing the batched reader against the current `get_daily_returns` would
    be partly tautological — since #1662 both decode through the same private
    helper. This function is the literal old code (the per-strategy loop, the
    un-projected `.first()`, and `row.to_backtest_result().equity_curve` for the
    fallback), so "byte-identical to the pre-change implementation" is measured
    against the pre-change implementation and nothing else.
    """
    import json as _json

    import numpy as np

    def _one(strategy_id: str) -> list[float]:
        row = (
            session.query(BacktestResultRecord)
            .filter(BacktestResultRecord.strategy_id == strategy_id)
            .order_by(BacktestResultRecord.created_at.desc(), BacktestResultRecord.id.desc())
            .first()
        )
        if row is None:
            return []
        if row.artifact_json:
            try:
                artifact = _json.loads(row.artifact_json)
                for r in artifact.get("results", []):
                    daily = r.get("metrics", {}).get("daily_returns", [])
                    if daily:
                        return daily
            except (_json.JSONDecodeError, KeyError):
                # Malformed or shape-shifted artifact_json: fall through to the
                # equity-curve fallback below, same as the production reader.
                pass
        result = row.to_backtest_result()
        if result.equity_curve and len(result.equity_curve) > 1:
            ec = np.array(result.equity_curve)
            return ((ec[1:] - ec[:-1]) / ec[:-1]).tolist()
        return []

    out: dict[str, list[float]] = {}
    for sid in strategy_ids:
        returns = _one(sid)
        if returns:
            out[sid] = returns
    return out


def _returns_result(strategy_id: str, equity_curve: list[float]) -> BacktestResult:
    """_sample_result with a caller-chosen equity curve (the fallback's input)."""
    import dataclasses

    return dataclasses.replace(_sample_result(strategy_id, sharpe=1.0), equity_curve=equity_curve)


def _artifact_with(daily: list[float] | None) -> str:
    """An analytics-engine-shaped artifact blob, optionally carrying daily_returns."""
    import json as _json

    metrics: dict = {"sharpe_ratio": 1.0}
    if daily is not None:
        metrics["daily_returns"] = daily
    return _json.dumps({"results": [{"metrics": metrics}]})


def _mixed_cohort_session():
    """Every decode branch `get_all_daily_returns` can take, in one fixture.

    A single-branch fixture would let the batched reader pass while silently
    dropping the equity-curve fallback — the exact branch #1662 warns cannot be
    replaced by a naive IN query.
    """
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()

    # (strategy_id, artifact_json, equity_curve) — one row per decode branch.
    spec = [
        # artifact carries daily_returns → artifact wins over a DIFFERENT curve
        ("s_artifact", _artifact_with([0.011, -0.022, 0.033]), [100.0, 500.0, 250.0]),
        # no artifact at all → equity-curve fallback
        ("s_equity_only", None, [100.0, 110.0, 99.0]),
        # artifact present but unparseable → caught, falls through to the curve
        ("s_bad_artifact", "{not json at all", [100.0, 101.0, 102.5]),
        # artifact parses but carries no daily_returns → falls through to the curve
        ("s_artifact_no_daily", _artifact_with(None), [100.0, 90.0, 99.0]),
        # artifact carries an EMPTY daily_returns → `if daily` is falsy → curve
        ("s_artifact_empty_daily", _artifact_with([]), [100.0, 102.0]),
        # neither → absent from the returned dict entirely
        ("s_neither", None, []),
        # a one-point curve cannot produce a return → also absent
        ("s_single_point", None, [100.0]),
    ]
    for sid, artifact, curve in spec:
        insert_backtest_if_missing(
            session,
            strategy_id=sid,
            content_hash=f"{sid}-h",
            result=_returns_result(sid, curve),
            source_pipeline="run_backtests",
            run_id="run-0",
            artifact_json=artifact,
        )
    session.commit()

    # "s_missing" is never persisted — the no-row branch.
    ids = [sid for sid, _a, _c in spec] + ["s_missing"]
    return session, ids


def test_get_all_daily_returns_issues_exactly_one_query() -> None:
    """THE GUARD (#1662): 10 ids, one SELECT — not ten.

    Counted at the session/engine boundary (before_cursor_execute), which is
    where the round trips and the blob transfers actually happen.
    """
    session, _SessionLocal, ids = _seeded_session_with_artifacts(n_strategies=10, n_cycles=3)
    try:
        assert len(ids) == 10
        session.expunge_all()
        statements, detach = _capture_sql(session.get_bind())
        try:
            returns = get_all_daily_returns(session, ids)
        finally:
            detach()

        selects = _selects_from_backtest_results(statements)
        # Non-vacuity: if the listener never fired, "exactly 1" would be a
        # tautology over an empty list.
        assert selects, "captured no SELECT against backtest_results — the listener did not fire"
        assert len(selects) == 1, (
            f"get_all_daily_returns issued {len(selects)} queries for {len(ids)} strategies; "
            f"the reader is still looping per strategy. statements={selects!r}"
        )
        # And it must have actually read something, or a broken query would
        # trivially satisfy the count.
        assert set(returns) == set(ids)
    finally:
        session.close()


def test_per_strategy_loop_reports_ten_queries_for_ten_ids() -> None:
    """ADVERSARIAL CONTROL for the guard above: the pre-#1662 shape, measured.

    If the counter could not see the N+1 here, the "exactly 1" assertion above
    would pass against the unfixed code too and guard nothing.
    """
    session, _SessionLocal, ids = _seeded_session_with_artifacts(n_strategies=10, n_cycles=3)
    try:
        session.expunge_all()
        statements, detach = _capture_sql(session.get_bind())
        try:
            _legacy_get_all_daily_returns(session, ids)
        finally:
            detach()

        selects = _selects_from_backtest_results(statements)
        assert len(selects) == 10, f"expected the old loop to emit one SELECT per strategy; got {len(selects)}"
    finally:
        session.close()


def test_get_all_daily_returns_projects_only_the_columns_it_decodes() -> None:
    """The batched query must select strategy_id/artifact_json/equity_curve_json
    and nothing else — no ORM hydration of ~30 unread scalar columns.

    equity_curve_json is deliberately REQUIRED here, not deferred: it is the
    fallback branch's only input, and #1543 spared it from deferral for
    api/risk_routes.py's sake.
    """
    session, _SessionLocal, ids = _seeded_session_with_artifacts(n_strategies=4, n_cycles=2)
    try:
        session.expunge_all()
        statements, detach = _capture_sql(session.get_bind())
        try:
            get_all_daily_returns(session, ids)
        finally:
            detach()

        selects = _selects_from_backtest_results(statements)
        assert len(selects) == 1
        sql = selects[0]
        assert "artifact_json" in sql, f"the decoder's primary input is not in the projection: {sql!r}"
        assert "equity_curve_json" in sql, f"the fallback branch's only input is not in the projection: {sql!r}"
        assert "sharpe_ratio" not in sql, f"hydrating the full row again, not projecting: {sql!r}"
        assert "monthly_returns_json" not in sql, f"hydrating the full row again, not projecting: {sql!r}"
    finally:
        session.close()


def test_get_all_daily_returns_hydrates_no_orm_rows() -> None:
    """Projection, not hydration: the mapper `load` event must never fire."""
    session, _SessionLocal, ids = _seeded_session_with_artifacts(n_strategies=6, n_cycles=3)
    try:
        session.expunge_all()
        loaded: list[str] = []

        def _count(target, _context) -> None:
            loaded.append(target.strategy_id)

        event.listen(BacktestResultRecord, "load", _count)
        try:
            get_all_daily_returns(session, ids)
        finally:
            event.remove(BacktestResultRecord, "load", _count)

        assert loaded == [], f"batched read hydrated {len(loaded)} BacktestResultRecord objects it never uses"
    finally:
        session.close()


def test_get_all_daily_returns_matches_the_pre_change_implementation_exactly() -> None:
    """Byte-identical output against the verbatim pre-#1662 code, over a cohort
    that exercises EVERY decode branch: artifact returns, equity-curve
    derivation, unparseable artifact, artifact-without-daily-returns, empty
    daily_returns, no series at all, a one-point curve, and a missing row."""
    session, ids = _mixed_cohort_session()
    try:
        expected = _legacy_get_all_daily_returns(session, ids)
        session.expunge_all()
        actual = get_all_daily_returns(session, ids)

        assert actual == expected
        # Key order too — the dict is built in caller order in both.
        assert list(actual) == list(expected)

        # Non-vacuity: the fixture must actually have exercised both the
        # artifact branch AND the equity-curve fallback, or "identical" is a
        # statement about one code path.
        assert expected["s_artifact"] == [0.011, -0.022, 0.033], "artifact branch not exercised"
        assert expected["s_equity_only"] == pytest.approx([0.1, -0.1]), "equity-curve fallback not exercised"
        assert "s_bad_artifact" in expected, "unparseable-artifact fallback not exercised"
        assert "s_artifact_no_daily" in expected, "artifact-without-daily-returns fallback not exercised"
        assert "s_artifact_empty_daily" in expected, "empty-daily_returns fallback not exercised"
        # …and that the empty cases are ABSENT, not present-and-empty.
        assert "s_neither" not in actual
        assert "s_single_point" not in actual
        assert "s_missing" not in actual
    finally:
        session.close()


def test_get_all_daily_returns_reads_the_latest_row_per_strategy() -> None:
    """The window function must pick the same row the old ORDER BY ... LIMIT 1
    picked — newest created_at, id as tiebreak."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        for cycle in range(4):
            insert_backtest_if_missing(
                session,
                strategy_id="s1",
                content_hash=f"s1-{cycle}",
                result=_sample_result("s1", float(cycle)),
                source_pipeline="run_backtests",
                run_id=f"run-{cycle}",
                artifact_json=_artifact_with([float(cycle)]),
            )
        session.commit()
        session.expunge_all()

        assert get_all_daily_returns(session, ["s1"]) == {"s1": [3.0]}
        assert get_all_daily_returns(session, ["s1"]) == _legacy_get_all_daily_returns(session, ["s1"])
    finally:
        session.close()


def test_get_all_daily_returns_does_not_change_the_cohort_cache_key() -> None:
    """THE CACHE-KEY SUBTLETY: the returns ARE the key.

    `rigor_cache.cohort_key` hashes each strategy's persisted series, so a
    batching change that perturbed any series — reordering, coercing, or
    dropping one — would silently invalidate every shared rigor-cache entry and
    make the Library recompute the ~6 s cohort pass on every request. Pin that
    the key derived from the batched read equals the key derived from the
    pre-change read over the same fixture.
    """
    from archimedes.services.rigor_cache import cohort_key

    session, ids = _mixed_cohort_session()
    try:
        legacy = _legacy_get_all_daily_returns(session, ids)
        session.expunge_all()
        batched = get_all_daily_returns(session, ids)

        code_versions = {sid: f"code-{sid}" for sid in ids}
        assert cohort_key(ids, batched, code_versions) == cohort_key(ids, legacy, code_versions)
        assert cohort_key(ids, batched) == cohort_key(ids, legacy)
    finally:
        session.close()


def test_cohort_key_control_detects_a_perturbed_series() -> None:
    """ADVERSARIAL CONTROL: a key that ignored the returns would make the test
    above vacuous. Perturb one strategy's series and confirm the key moves."""
    from archimedes.services.rigor_cache import cohort_key

    session, ids = _mixed_cohort_session()
    try:
        batched = get_all_daily_returns(session, ids)
        perturbed = dict(batched)
        perturbed["s_artifact"] = [*batched["s_artifact"][:-1], batched["s_artifact"][-1] + 1e-9]
        assert cohort_key(ids, batched) != cohort_key(ids, perturbed)
    finally:
        session.close()


def test_get_all_daily_returns_empty_ids_issues_no_query() -> None:
    session, _SessionLocal, _ids = _seeded_session_with_artifacts(n_strategies=2, n_cycles=1)
    try:
        statements, detach = _capture_sql(session.get_bind())
        try:
            assert get_all_daily_returns(session, []) == {}
        finally:
            detach()
        assert _selects_from_backtest_results(statements) == []
    finally:
        session.close()


def test_get_all_daily_returns_dedupes_repeated_ids_into_one_query() -> None:
    """A repeated id used to cost a repeated round trip; it must now cost none,
    and the returned dict must be unchanged."""
    session, _SessionLocal, ids = _seeded_session_with_artifacts(n_strategies=3, n_cycles=2)
    try:
        session.expunge_all()
        repeated = ids + ids + [ids[0]]
        statements, detach = _capture_sql(session.get_bind())
        try:
            actual = get_all_daily_returns(session, repeated)
        finally:
            detach()

        assert len(_selects_from_backtest_results(statements)) == 1
        assert actual == _legacy_get_all_daily_returns(session, repeated)
    finally:
        session.close()


def test_get_all_daily_returns_ignores_unrequested_strategies() -> None:
    session, _SessionLocal, ids = _seeded_session_with_artifacts(n_strategies=6, n_cycles=2)
    try:
        session.expunge_all()
        subset = ids[:2]
        assert set(get_all_daily_returns(session, subset)) == set(subset)
    finally:
        session.close()


def test_get_all_daily_returns_cache_hit_issues_no_query() -> None:
    """#1713: the second Library read must not re-decode artifact blobs.

    MUTATION: delete the cache lookup in ``get_all_daily_returns``. This then
    sees a SELECT on the second call and fails. The first-call one-query
    guard above still requires a live read — the cache is a memo, not a fixture.
    """
    session, _SessionLocal, ids = _seeded_session_with_artifacts(n_strategies=10, n_cycles=3)
    try:
        first = get_all_daily_returns(session, ids)
        session.expunge_all()
        statements, detach = _capture_sql(session.get_bind())
        try:
            second = get_all_daily_returns(session, ids)
        finally:
            detach()

        selects = _selects_from_backtest_results(statements)
        assert selects == [], (
            "cohort-returns cache miss on the second call — the Library page "
            f"would still pay the blob decode. statements={selects!r}"
        )
        assert second == first
        # The two page-load callers pass the same ids in different orders;
        # a key that included caller order would make them miss each other.
        statements, detach = _capture_sql(session.get_bind())
        try:
            reversed_order = get_all_daily_returns(session, list(reversed(ids)))
        finally:
            detach()
        assert _selects_from_backtest_results(statements) == []
        assert set(reversed_order) == set(first)
    finally:
        session.close()


def test_get_all_daily_returns_cache_is_invalidated_by_a_new_backtest_row() -> None:
    """A cached series must not outlive ``insert_backtest_if_missing``.

    MUTATION: drop ``clear_cohort_returns_cache()`` from the writer. This
    then reads ``[0.0]`` after inserting ``[9.0]``.
    """
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        insert_backtest_if_missing(
            session,
            strategy_id="s1",
            content_hash="s1-0",
            result=_sample_result("s1", 0.0),
            source_pipeline="run_backtests",
            run_id="run-0",
            artifact_json=_artifact_with([0.0]),
        )
        session.commit()
        assert get_all_daily_returns(session, ["s1"]) == {"s1": [0.0]}

        insert_backtest_if_missing(
            session,
            strategy_id="s1",
            content_hash="s1-1",
            result=_sample_result("s1", 9.0),
            source_pipeline="run_backtests",
            run_id="run-1",
            artifact_json=_artifact_with([9.0]),
        )
        session.commit()
        session.expunge_all()
        assert get_all_daily_returns(session, ["s1"]) == {"s1": [9.0]}, (
            "cohort-returns cache served the pre-insert series after a new "
            "backtest row — the Library would grade stale returns"
        )
    finally:
        session.close()


def test_get_all_daily_returns_cache_returns_a_copy() -> None:
    """Mutating the returned lists must not poison the memo for the next caller."""
    session, _SessionLocal, ids = _seeded_session_with_artifacts(n_strategies=3, n_cycles=1)
    try:
        first = get_all_daily_returns(session, ids)
        sid = next(iter(first))
        first[sid].append(99.0)
        second = get_all_daily_returns(session, ids)
        assert 99.0 not in second[sid]
    finally:
        session.close()
