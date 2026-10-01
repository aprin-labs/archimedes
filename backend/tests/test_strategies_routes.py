"""Tests for strategies_routes — the curated rigor badge (#821, #1746).

The user-facing ``passes_rigor_gate`` badge (and the CANDIDATE → VALIDATED 🏆
promotion) must come from a real ``run_rigor_gate`` verdict computed on the
strategy's persisted real returns — the SAME machinery the
``/api/selection-bias/gate`` route uses — NOT from the stored fixture boolean in
``analytics-engine/strategies/backtest_fixtures.json``. A strategy with no real
returns surfaces an explicit ``pending`` badge, never a fixture ``True``/``False``.

**Since #1746 / PR-B, that gate run happens on the WRITE side.**
``services.curated_grading.grade_curated_library`` grades the library when a
curated backtest runs, stores the verdict on ``strategy_passports``, and every
read surface — list, detail, leaderboard, ``/passports/{id}`` — serves that one
row (``docs/adr/rigor-verdict-of-record.md``). So the route tests below mock the
DB boundary (``get_all_daily_returns``), run the REAL grading job, and then
assert the served badge equals an independently-computed ``run_rigor_gate``
verdict on the same returns. The #821 claim is unchanged and the assertions are
the same; what moved is when the gate runs.

The tests that used to pin "the read path grades the FULL library cohort"
(#902/#1173) now pin it on the grading job, which is where the cohort lives.

Hermetic gate:
  env -i HOME=$HOME PATH=$PATH PYTHONPATH=backend python -m pytest \\
      backend/tests/test_strategies_routes.py -q
"""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest
from archimedes.services.live_rigor_gate import (
    DEGENERATE,
    FAIL,
    PASS,
    PENDING,
    RigorGateVerdict,
    verdict_from_returns,
)
from archimedes.services.rigor_evaluator import (
    compute_average_pairwise_correlation,
    compute_pbo,
    run_rigor_gate,
)
from httpx import ASGITransport, AsyncClient

# ── Hermetic DB fixture ────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _use_tmp_db(tmp_path, monkeypatch):
    """Redirect the DB to a per-test temp SQLite file — engine and all.

    Setting ``DATABASE_URL`` alone does NOT isolate anything: ``archimedes.db``
    resolves that env var once, at import time, and every ``get_session()``
    goes through the module-global ``SessionLocal`` built from it. The env-only
    version of this fixture asserted isolation it did not provide — harmless
    while no test in this module WROTE anything, and no longer true now that
    they run the real grading job (a degenerate verdict written by one test was
    read as the badge by the next). Rebinding both globals, the way
    ``test_rigor_verdict_of_record.py`` does, makes the docstring true.
    """
    # `from archimedes import db`, not `import archimedes.db as db`: the rest of
    # this file reaches the same module with `from archimedes.db import ...`, and
    # mixing the two import forms for one module is what the code-quality gate
    # flags. Same module object either way — the binding is what monkeypatch
    # needs, not the syntax.
    from archimedes import db
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    url = f"sqlite:///{tmp_path / 'test_strategies_routes.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    eng = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", eng)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=eng, autocommit=False, autoflush=False))
    db.init_db()
    yield


# A clean strategy snippet that passes the AST look-ahead audit (no future/peek
# access). Real curated strategy files also pass; this keeps the test independent
# of any particular file on disk.
_CLEAN_CODE = "def init(self):\n    self.sma = 0\n"


def _grade_curated_now(strategies=None):
    """Run the REAL grading job against this test's DB — the write-side event.

    Every read-surface assertion below is an assertion about what this wrote.
    Calling it explicitly is the point: a test that hits a route without calling
    this first is testing the ungraded state, which is a different (and also
    real) claim — see ``test_strategy_without_real_returns_is_pending``.
    """
    from archimedes.api._route_helpers import strategy_provider
    from archimedes.db import get_session
    from archimedes.services.curated_grading import grade_curated_library

    with get_session() as session:
        summary = grade_curated_library(session, provider=strategy_provider(), strategies=strategies)
        session.commit()
    return summary


def _patch_code_loader(monkeypatch) -> None:
    """Make the look-ahead audit deterministic for the grading job.

    The gate's look-ahead leg reads the strategy's source; pinning it to clean
    code keeps these tests independent of the real files on disk. Patched on the
    grading module, which is the only place the curated gate now loads code.
    """
    monkeypatch.setattr(
        "archimedes.services.curated_grading._load_strategy_code_safe",
        lambda strategy: _CLEAN_CODE,
    )


def _passing_series(seed: int = 0, n: int = 500) -> list[float]:
    """A return series engineered to clear the live gate when paired with a
    cohort (≥2 strategies for PBO) + clean code (look-ahead pass)."""
    return np.random.default_rng(seed).normal(0.0015, 0.004, n).tolist()


def _failing_series(seed: int = 99, n: int = 500) -> list[float]:
    """Pure-noise series — negative/zero drift, high vol — that the gate fails."""
    return np.random.default_rng(seed).normal(0.0, 0.02, n).tolist()


def _failing_result(strategy_id: str):
    """A REAL ``RigorGateResult`` that fails the gate.

    A real result rather than a MagicMock so ``verdict_from_result`` exercises
    the same ``RigorGateVerdict.from_result`` reduction the production path runs
    (``is_degenerate`` → ``passes_all`` → ladder fields), not a mock's
    auto-attributes.
    """
    return run_rigor_gate(strategy_id=strategy_id, daily_returns=_failing_series(), num_trials=1)


# ── live_rigor_gate unit tests (the single source of truth) ─────────────


class TestVerdictFromReturns:
    def test_no_returns_is_pending(self):
        v = verdict_from_returns("s", [])
        assert v.status == PENDING
        assert v.passes is False
        assert v.source == "pending"

    def test_too_few_returns_is_pending(self):
        # Below the 10-obs floor the gate cannot run → pending, NOT a boolean.
        v = verdict_from_returns("s", [0.01] * 9)
        assert v.status == PENDING
        assert v.passes is False

    def test_pending_source_is_never_fixture(self):
        # The whole point of #821: pending must never be a stored boolean.
        v = verdict_from_returns("s", [])
        assert v.source != "fixture"

    def test_real_returns_yield_live_verdict_not_boolean(self):
        # With real returns the verdict comes from run_rigor_gate, not a constant.
        a = _passing_series(0)
        b = _passing_series(1)
        pbo = compute_pbo({"a": a, "b": b})
        v = verdict_from_returns("a", a, num_trials=2, pbo_scores=pbo, strategy_code=_CLEAN_CODE)
        expected = run_rigor_gate("a", a, num_trials=2, pbo_scores=pbo, strategy_code=_CLEAN_CODE)
        assert v.passes == expected.passes_all
        assert v.status == (PASS if expected.passes_all else FAIL)
        assert v.source == "live_gate"

    def test_noise_series_fails_live_gate(self):
        v = verdict_from_returns("noise", _failing_series(), num_trials=4)
        assert v.status == FAIL
        assert v.passes is False

    def test_gate_exception_fails_closed_to_pending(self, monkeypatch):
        # If run_rigor_gate raises, the badge must NOT claim a pass.
        def _boom(*a, **k):
            raise RuntimeError("gate exploded")

        monkeypatch.setattr("archimedes.services.rigor_evaluator.run_rigor_gate", _boom)
        # verdict_from_returns is already imported at module top; patching the source
        # module's run_rigor_gate (which it imports locally) is what makes this work.
        v = verdict_from_returns("s", _passing_series(), num_trials=2)
        assert v.status == PENDING
        assert v.passes is False


class TestDefaultNumTrials:
    """Decouple #2: an unspecified num_trials defaults to a self-contained 1 —
    never derived from the curated library's count."""

    def test_verdict_from_returns_defers_to_default_num_trials(self, monkeypatch):
        # verdict_from_returns still delegates to _default_num_trials() when the
        # caller passes nothing — this pins the indirection, independent of what
        # _default_num_trials() itself resolves to.
        from archimedes.services import live_rigor_gate

        captured = {}

        def _spy_gate(*args, **kwargs):
            captured.update(kwargs)
            return run_rigor_gate(*args, **kwargs)

        monkeypatch.setattr("archimedes.services.rigor_evaluator.run_rigor_gate", _spy_gate)
        monkeypatch.setattr(live_rigor_gate, "_default_num_trials", lambda: 7)

        verdict_from_returns("a", _passing_series(0), strategy_code=_CLEAN_CODE)
        assert captured["num_trials"] == 7

    def test_default_num_trials_is_self_contained_one(self, monkeypatch):
        # A strategy's rigor depends ONLY on itself — _default_num_trials() must
        # always resolve to 1, regardless of the curated library's size or
        # whether the strategy provider is even reachable (decouple #2 removed
        # the library-size lookup entirely, so this must not touch it at all).
        from archimedes.services import live_rigor_gate

        def _boom():
            raise AssertionError("_default_num_trials must not consult the strategy provider")

        monkeypatch.setattr("archimedes.services.strategy_provider.default_provider", _boom)
        assert live_rigor_gate._default_num_trials() == 1

    def test_explicit_num_trials_still_wins(self, monkeypatch):
        from archimedes.services import live_rigor_gate

        captured = {}

        def _spy_gate(*args, **kwargs):
            captured.update(kwargs)
            return run_rigor_gate(*args, **kwargs)

        monkeypatch.setattr("archimedes.services.rigor_evaluator.run_rigor_gate", _spy_gate)
        monkeypatch.setattr(
            live_rigor_gate, "_default_num_trials", lambda: (_ for _ in ()).throw(AssertionError("not called"))
        )

        verdict_from_returns("a", _passing_series(0), num_trials=3, strategy_code=_CLEAN_CODE)
        assert captured["num_trials"] == 3


class TestGradingCohortIsTheFullLibrary:
    """#902 / #1173: a strategy's badge must not depend on how it was requested.

    That used to be defended on the READ side — every route re-graded the whole
    library so a paginated or status-filtered request could not grade a subset.
    Since #1746 / PR-B the cohort belongs to the WRITER: the grading job grades
    the full library once, stores each verdict, and the read surfaces serve the
    stored row. The invariant is now structural; these tests pin it where it
    lives.
    """

    def test_the_job_grades_the_whole_provider_library(self, monkeypatch):
        """MUTATION: pass a slice (``strategies[:2]``) to ``grade_cohort`` inside
        ``grade_curated_library``. The captured cohort shrinks and this reddens.
        """
        from archimedes.db import get_session
        from archimedes.services import curated_grading

        lib = [MagicMock(id=f"s{i}") for i in range(3)]
        provider = MagicMock()
        provider.list_strategies.return_value = lib
        provider.get_backtest_result.return_value = None

        captured = {}

        def _fake_cohort(strategies):
            captured["cohort_ids"] = [s.id for s in strategies]
            return curated_grading.CohortGrade()

        monkeypatch.setattr(curated_grading, "grade_cohort", _fake_cohort)
        # The passport write is not what this test is about; a MagicMock
        # strategy cannot be ingested, so count the attempts instead.
        written: list = []
        monkeypatch.setattr(
            "archimedes.services.passport_loader.ingest_passport",
            lambda *a, **kw: written.append(a[1].id),
        )
        monkeypatch.setattr(curated_grading, "with_display_metrics", lambda s, bt: s, raising=False)

        with get_session() as session:
            curated_grading.grade_curated_library(session, provider=provider)

        assert captured["cohort_ids"] == ["s0", "s1", "s2"]

    def test_a_strategy_the_cohort_could_not_grade_is_stored_pending(self, monkeypatch):
        """A gate that produced no result for an id must store ``pending`` — not
        skip the row, and never a ``fail``.

        MUTATION: make ``verdict_from_result(None)`` return
        ``RigorGateVerdict.failed()``. "We have not graded this" becomes "we
        graded this and it lost", which is #1184 in one line.
        """
        from archimedes.services.curated_grading import verdict_from_result

        assert verdict_from_result(None).status == PENDING
        assert verdict_from_result(None).passes is False

    @pytest.mark.asyncio
    async def test_the_list_route_never_runs_the_gate(self, monkeypatch):
        """REGRESSION (#1173, restated): the list route cannot grade a page,
        because it cannot grade at all.

        The original defect was that scoring over ``strats[offset:offset+limit]``
        made a badge depend on which page a strategy landed on — a short window
        can fall under ``MIN_LIBRARY_N_FOR_PBO_GATING`` and the cohort-scoped
        PBO/CSCV value itself shifts with membership. Verified against production
        before that fix: strategy ``d90b357a…4bbd`` graded False in a 5-item
        window but True in the full-library view. A route that runs no gate has
        no cohort to get wrong.

        MUTATION: restore a ``rigor_results = grade_cohort(library)`` call in
        ``_list_strategies_sync``. The spy fires and this reddens.
        """
        from archimedes.main import app

        calls: list = []
        real = run_rigor_gate

        def _counting(*args, **kwargs):
            calls.append(kwargs.get("strategy_id"))
            return real(*args, **kwargs)

        monkeypatch.setattr("archimedes.services.rigor_evaluator.run_rigor_gate", _counting)
        monkeypatch.setattr(
            "archimedes.services.backtest_repository.get_all_daily_returns",
            lambda session, ids: {sid: _passing_series(i) for i, sid in enumerate(ids)},
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/strategies/?limit=5&offset=25")

        assert resp.status_code == 200
        assert calls == [], f"the list route ran the rigor gate {len(calls)} times; it must read a stored verdict"


class TestReadSurfacesServeTheStoredVerdictWithoutRegrading:
    """REGRESSION (#1645 → #1746): ``GET /api/strategies/{id}`` runs no gate.

    #1645 got one cohort gate run per detail request down from two, and leaned on
    ``services.rigor_cache`` to make even that affordable (measured on prod
    2026-08-31, anonymous: ``X-Response-Time-Ms: 28144.0 / 25072.2 / 28554.9`` on
    three consecutive calls). #1746 removes the remaining one: the verdict is
    graded when a backtest runs and read here. A request that runs no gate cannot
    disagree with the row, cannot drift between two reads, and cannot cost 28s.
    """

    @pytest.mark.asyncio
    async def test_detail_route_runs_the_gate_zero_times_cold_and_warm(self, monkeypatch):
        """MUTATION: put ``_live_verdict_and_result_for_one`` back into
        ``_to_strategy_response``. Both assertions redden — cold at
        ``len(library)``, warm at ``len(library)`` too, since there is no cache
        left to hide behind.
        """
        from archimedes.api import strategies_routes as sr
        from archimedes.main import app

        library = sr.strategy_provider().list_strategies()
        assert len(library) >= 2, "need a real curated cohort"
        returns = {s.id: _passing_series(i) for i, s in enumerate(library)}

        monkeypatch.setattr(
            "archimedes.services.backtest_repository.get_all_daily_returns",
            lambda session, ids: {sid: returns.get(sid, []) for sid in ids},
        )
        _patch_code_loader(monkeypatch)
        _grade_curated_now()

        calls: list = []
        real = run_rigor_gate

        def _counting(*args, **kwargs):
            calls.append(kwargs.get("strategy_id"))
            return real(*args, **kwargs)

        monkeypatch.setattr("archimedes.services.rigor_evaluator.run_rigor_gate", _counting)

        target = library[0]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            cold = await client.get(f"/api/strategies/{target.id}")
            assert cold.status_code == 200
            assert calls == [], f"a detail request must run no gate at all; got {len(calls)} run_rigor_gate calls"
            warm = await client.get(f"/api/strategies/{target.id}")
            assert warm.status_code == 200
        assert calls == []

        # Same row, same answer, both times — the drift the issue reported
        # ("Sharpe drifted between reads 37s apart") is not expressible here.
        assert warm.json()["passes_rigor_gate"] == cold.json()["passes_rigor_gate"]
        assert warm.json()["dsr_p_value"] == cold.json()["dsr_p_value"]
        assert warm.json()["sharpe_ratio"] == cold.json()["sharpe_ratio"]

    def test_an_ungraded_row_fail_closes_to_pending_with_no_numbers(self):
        """A strategy with no stored passport row is ``pending`` and carries no
        gate numbers — never a fixture value, never a fabricated pass.

        MUTATION: serve ``stored.deflated_sharpe_ratio`` without the ``graded``
        guard in ``_to_strategy_response``. A legacy curated row still carrying
        the #1187 fixture DSR then serves it beside a ``pending`` badge.
        """
        from archimedes.api.strategies_routes import _to_strategy_response
        from archimedes.models.strategy import Strategy

        resp = _to_strategy_response(Strategy(id="never-graded"), None)
        assert resp.rigor_gate_status == PENDING
        assert resp.passes_rigor_gate is False
        assert resp.deflated_sharpe_ratio is None
        assert resp.dsr_p_value is None
        assert resp.pbo_score is None
        assert resp.out_of_sample_sharpe is None
        assert resp.metrics_source == "unavailable"
        assert resp.num_trials_in_selection is None
        assert resp.num_trials_scope == "unspecified"


class TestRigorGateVerdict:
    def test_passes_only_truthy_for_pass(self):
        assert RigorGateVerdict.passed().passes is True
        assert RigorGateVerdict.failed().passes is False
        assert RigorGateVerdict.pending().passes is False
        assert RigorGateVerdict.degenerate().passes is False

    def test_status_labels(self):
        assert RigorGateVerdict.passed().status == PASS
        assert RigorGateVerdict.failed().status == FAIL
        assert RigorGateVerdict.pending().status == PENDING
        assert RigorGateVerdict.degenerate().status == DEGENERATE

    def test_degenerate_never_deployable_at_any_level(self):
        """#1184: broken/zero-trade data blocks every strictness level, same as
        any other always-on correctness floor — never just the strictest bar."""
        v = RigorGateVerdict.degenerate()
        assert v.blocked_by_floor is True
        assert v.min_passing_level is None


class TestVerdictFromResultDegenerate:
    """#1184: the STORED badge (``curated_grading.verdict_from_result``) must
    record a zero-variance persisted series as ``degenerate`` — the same category
    ``live_rigor_gate.verdict_from_returns`` reports for the identical input —
    not silently collapse into a plain ``fail`` just because the grading job
    reduces an already-computed ``RigorGateResult`` instead of calling the gate
    itself. The distinction has to survive the WRITE now: nothing on the read
    path can re-derive it (that was the point), so if the writer loses it, it is
    gone.
    """

    def test_degenerate_result_maps_to_degenerate_verdict(self):
        from archimedes.services.curated_grading import verdict_from_result

        result = run_rigor_gate(strategy_id="lib-degenerate", daily_returns=[0.0] * 5659, num_trials=1)
        assert result.is_degenerate is True  # sanity: the input really is degenerate

        v = verdict_from_result(result)
        assert v.status == DEGENERATE
        assert v.status != FAIL
        assert v.status != PENDING
        assert v.passes is False

    def test_none_result_still_maps_to_pending(self):
        """No live gate result at all (insufficient/no persisted returns) stays
        the pre-existing PENDING — the new category must not swallow this case."""
        from archimedes.services.curated_grading import verdict_from_result

        assert verdict_from_result(None).status == PENDING

    def test_non_degenerate_fail_result_still_maps_to_fail(self):
        rng = np.random.default_rng(9)
        losing = rng.normal(-0.002, 0.01, size=300).tolist()
        result = run_rigor_gate(strategy_id="lib-real-loser", daily_returns=losing, num_trials=1)
        assert result.is_degenerate is False

        from archimedes.services.curated_grading import verdict_from_result

        v = verdict_from_result(result)
        assert v.status == FAIL


# ── Acceptance #1: served badge == live run_rigor_gate verdict ──────────


@pytest.mark.asyncio
async def test_library_badge_equals_live_gate_verdict_on_persisted_returns(monkeypatch):
    """ACCEPTANCE #1: the library-list ``passes_rigor_gate`` for a real strategy
    equals the live ``run_rigor_gate`` verdict computed on its persisted returns.

    We inject known persisted returns at the DB boundary (get_all_daily_returns),
    serve the real ``GET /api/strategies/`` endpoint, then independently recompute
    the gate verdict over the SAME returns (same num_trials / cohort PBO /
    avg-correlation the route derives) and assert every served badge matches.
    """
    from archimedes.api import strategies_routes as sr
    from archimedes.main import app

    strategies = sr.strategy_provider().list_strategies()
    assert len(strategies) >= 2, "need ≥2 curated strategies for a PBO cohort"

    # Give the first two real strategies persisted returns: one engineered to
    # pass, one engineered to fail. The rest get no returns (→ pending).
    s_pass, s_fail = strategies[0], strategies[1]
    returns = {s_pass.id: _passing_series(0), s_fail.id: _failing_series()}

    # Patch the DB boundary used by verdicts_for_strategies (imported inside the
    # function body, so patch the definition module).
    monkeypatch.setattr(
        "archimedes.services.backtest_repository.get_all_daily_returns",
        lambda session, ids: dict(returns),
    )
    # Make the look-ahead audit deterministic (clean code → pass) for both. The
    # served badge on GET /api/strategies/ comes from _live_rigor_results_for_
    # strategies (#868), which reads its OWN local loader — patch both loaders
    # so this test and test_leaderboard_numeric_fields_equal_live_gate_on_
    # persisted_returns (below) see identical code. Only patching the
    # live_rigor_gate loader left the served badge reading REAL (uncontrolled)
    # strategy source, a gap that stayed masked while num_trials=len(cohort)
    # deflation dominated passes_all; it surfaces now that num_trials=1
    # (decouple #2) makes DSR pass easily and the look-ahead leg decide.
    monkeypatch.setattr(
        "archimedes.services.live_rigor_gate._load_strategy_code_safe",
        lambda strategy: _CLEAN_CODE,
    )
    _patch_code_loader(monkeypatch)

    # Independently reproduce the route's cohort context. num_trials is
    # self-contained (1, decouple #2) — it does NOT come from this cohort;
    # only PBO/avg_correlation are cohort-derived.
    valid = {k: v for k, v in returns.items() if len(v) >= 10}
    pbo_scores = compute_pbo(valid) if len(valid) >= 2 else {}
    num_trials = 1
    avg_corr = compute_average_pairwise_correlation(valid) if len(valid) >= 2 else 0.0

    # THE grading event — the real gate, over the real cohort, written to the
    # passport rows the route below reads (#1746 / PR-B).
    _grade_curated_now()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/strategies/?limit=100")
    assert resp.status_code == 200
    by_id = {s["id"]: s for s in resp.json()["strategies"]}

    for sid, series in returns.items():
        expected = run_rigor_gate(
            strategy_id=sid,
            daily_returns=series,
            num_trials=num_trials,
            pbo_scores=pbo_scores,
            strategy_code=_CLEAN_CODE,
            in_sample_sharpe=None,
            average_correlation=avg_corr,
        )
        served = by_id[sid]
        assert served["passes_rigor_gate"] == expected.passes_all, (
            f"served badge for {sid} ({served['passes_rigor_gate']}) != live gate verdict ({expected.passes_all})"
        )
        # Four-state comparison (#1184): degenerate is neither PASS nor FAIL, so
        # comparing only against passes_all would misclassify a degenerate series
        # as FAIL. Use the same tri_state_status the route's badge is built from.
        assert served["rigor_gate_status"] == expected.tri_state_status

    # The engineered pass strategy passes; the noise strategy fails — on the LIVE path.
    assert by_id[s_pass.id]["passes_rigor_gate"] is True
    assert by_id[s_fail.id]["passes_rigor_gate"] is False


@pytest.mark.asyncio
async def test_degenerate_persisted_series_serves_degenerate_badge_via_route(monkeypatch):
    """ACCEPTANCE #4 (#1184): a zero-variance persisted return series is served
    as the DEGENERATE badge by the REAL ``GET /api/strategies/`` route.

    The unit tests in ``TestDegenerateSeriesCategory`` (test_rigor_evaluator.py)
    and ``TestVerdictFromResultDegenerate`` above already cover
    ``run_rigor_gate`` / ``verdict_from_result`` in isolation; this exercises
    the same category end-to-end through the ASGI transport, the way
    Acceptance #1 exercises the non-degenerate case above — otherwise the enum
    assertions elsewhere in this file (e.g.
    ``test_list_endpoint_returns_200_and_status_field``) could silently start
    rejecting a real degenerate strategy without any route-level test catching
    it.
    """
    from archimedes.api import strategies_routes as sr
    from archimedes.main import app

    strategies = sr.strategy_provider().list_strategies()
    assert strategies, "need at least 1 curated strategy"
    target = strategies[0]

    # A constant (all-zero) 5,659-observation series — the exact shape #1184
    # names (mathematically constant, most plausibly all-zero).
    degenerate_series = [0.0] * 5659
    monkeypatch.setattr(
        "archimedes.services.backtest_repository.get_all_daily_returns",
        lambda session, ids: {target.id: degenerate_series},
    )
    # The route's badge reads its OWN local code loader (#868) — patch both so
    # this test doesn't depend on the real strategy source files (mirrors the
    # pattern in test_library_badge_equals_live_gate_verdict_on_persisted_returns
    # above). Not load-bearing for the DEGENERATE verdict itself — is_degenerate
    # short-circuits tri_state_status before the look-ahead leg is consulted —
    # but keeps this test deterministic and independent of on-disk source.
    monkeypatch.setattr(
        "archimedes.services.live_rigor_gate._load_strategy_code_safe",
        lambda strategy: _CLEAN_CODE,
    )
    _patch_code_loader(monkeypatch)
    _grade_curated_now()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/strategies/?limit=100")
    assert resp.status_code == 200
    by_id = {s["id"]: s for s in resp.json()["strategies"]}

    served = by_id[target.id]
    assert served["rigor_gate_status"] == DEGENERATE
    assert served["passes_rigor_gate"] is False


# ── Acceptance #2: no real returns → pending, not a fixture boolean ─────


@pytest.mark.asyncio
async def test_strategy_without_real_returns_is_pending(monkeypatch):
    """ACCEPTANCE #2: a strategy with NO persisted returns surfaces a ``pending``
    badge — never a fixture True/False. We force the DB to return nothing for
    every strategy, so the live gate cannot run for any of them."""
    from archimedes.main import app

    monkeypatch.setattr(
        "archimedes.services.backtest_repository.get_all_daily_returns",
        lambda session, ids: {},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/strategies/?limit=100")
    assert resp.status_code == 200
    served = resp.json()["strategies"]
    assert served, "expected curated strategies in the library"

    # EVERY strategy is pending with passes_rigor_gate=False — including the two
    # strategies whose FIXTURE value is True (moreira_muir, moskowitz_ooi_pedersen).
    # If the fixture boolean were still the source, those two would read True.
    for s in served:
        assert s["rigor_gate_status"] == PENDING, f"{s['id']} not pending: {s['rigor_gate_status']}"
        assert s["passes_rigor_gate"] is False, f"{s['id']} leaked a non-live pass badge"
        # #1358 A4: the live gate never ran for this strategy, so there is no
        # provenance to report — never a silently-assumed self-contained 1.
        assert s["num_trials_in_selection"] is None, f"{s['id']} claimed a num_trials with no live gate run"
        assert s["num_trials_scope"] == "unspecified", f"{s['id']} scope: {s['num_trials_scope']}"


@pytest.mark.asyncio
async def test_fixture_true_strategies_do_not_read_true_without_live_returns(monkeypatch):
    """The two fixture-True strategies (moreira_muir, moskowitz_ooi_pedersen) must
    NOT show passes_rigor_gate=True purely from the fixture: with no live returns
    they are ``pending``. This is the direct anti-regression for #821."""
    from archimedes.api import strategies_routes as sr
    from archimedes.main import app

    monkeypatch.setattr(
        "archimedes.services.backtest_repository.get_all_daily_returns",
        lambda session, ids: {},
    )

    # Resolve the deterministic strategy ids for the two fixture-True stems.
    by_path = {}
    for s in sr.strategy_provider().list_strategies():
        by_path[s.strategy_code_path or ""] = s
    fixture_true_stems = ("moreira_muir_2017_volatility_managed", "moskowitz_ooi_pedersen_2012_tsmom")
    targets = [s for path, s in by_path.items() if any(stem in path for stem in fixture_true_stems)]
    assert targets, "could not resolve the fixture-True strategies"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/strategies/?limit=100")
    by_id = {s["id"]: s for s in resp.json()["strategies"]}

    for t in targets:
        served = by_id[t.id]
        assert served["passes_rigor_gate"] is False, f"{t.id} read fixture True on the live path"
        assert served["rigor_gate_status"] == PENDING


# ── CANDIDATE → VALIDATED promotion is live, not fixture-driven ─────────


@pytest.mark.asyncio
async def test_validated_promotion_only_when_live_gate_passes(monkeypatch):
    """A CANDIDATE is promoted to VALIDATED only when the LIVE gate passes on real
    returns — not because a fixture said so. With no live returns, every CANDIDATE
    stays CANDIDATE (no fixture-driven 🏆)."""
    from archimedes.main import app

    monkeypatch.setattr(
        "archimedes.services.backtest_repository.get_all_daily_returns",
        lambda session, ids: {},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/strategies/?limit=100")
    served = resp.json()["strategies"]

    # No live returns → no strategy may be served as "validated" purely from a fixture.
    for s in served:
        if s["rigor_gate_status"] == PENDING:
            assert s["status"] != "validated", f"{s['id']} promoted to validated without a live pass"


@pytest.mark.asyncio
async def test_validated_promotion_fires_on_live_pass(monkeypatch):
    """When the live gate passes on persisted returns for a CANDIDATE strategy, the
    served status is promoted to VALIDATED."""
    from archimedes.api import strategies_routes as sr
    from archimedes.main import app
    from archimedes.models.strategy import StrategyStatus

    strategies = sr.strategy_provider().list_strategies()
    candidates = [s for s in strategies if s.status == StrategyStatus.CANDIDATE]
    assert len(candidates) >= 2, "need ≥2 CANDIDATE strategies"
    s_pass = candidates[0]
    cohort = candidates[1]
    returns = {s_pass.id: _passing_series(0), cohort.id: _passing_series(1)}

    monkeypatch.setattr(
        "archimedes.services.backtest_repository.get_all_daily_returns",
        lambda session, ids: dict(returns),
    )
    _patch_code_loader(monkeypatch)
    _grade_curated_now()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/strategies/?limit=100")
    by_id = {s["id"]: s for s in resp.json()["strategies"]}

    if by_id[s_pass.id]["passes_rigor_gate"]:
        assert by_id[s_pass.id]["status"] == "validated"


# ── Endpoint smoke + schema shape ───────────────────────────────────────


@pytest.mark.asyncio
async def test_list_endpoint_returns_200_and_status_field():
    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/strategies/?limit=5")
    assert resp.status_code == 200
    data = resp.json()
    assert "strategies" in data and "total" in data
    for s in data["strategies"]:
        assert s["rigor_gate_status"] in (PASS, FAIL, PENDING, DEGENERATE)
        assert isinstance(s["passes_rigor_gate"], bool)


# ── Acceptance #3 (#868): leaderboard numeric fields == live gate values ────
#
# GET /api/strategies/ must serve dsr_p_value / pbo_score / out_of_sample_sharpe /
# deflated_sharpe_ratio computed by the SAME live run_rigor_gate call that backs
# GET /api/selection-bias/gate for the same strategy id — previously only the
# passes_rigor_gate BOOLEAN read the live verdict (#821) while these numeric
# fields still read stale s.<field>/bt.<field> fixture values, so the two
# surfaces could disagree on the numbers for one strategy even when they agreed
# on pass/fail.


@pytest.mark.asyncio
async def test_leaderboard_numeric_fields_equal_live_gate_on_persisted_returns(monkeypatch):
    """ACCEPTANCE #3: for a strategy with real persisted returns, the served
    dsr_p_value/pbo_score/out_of_sample_sharpe/deflated_sharpe_ratio on
    GET /api/strategies/ equal an independently-computed run_rigor_gate result
    over the SAME returns + SAME cohort context the route derives — mirroring
    test_library_badge_equals_live_gate_verdict_on_persisted_returns's
    established pattern for the boolean badge, extended to the numeric fields."""
    from archimedes.api import strategies_routes as sr
    from archimedes.main import app

    strategies = sr.strategy_provider().list_strategies()
    assert len(strategies) >= 2, "need ≥2 curated strategies for a PBO cohort"

    s_pass, s_fail = strategies[0], strategies[1]
    returns = {s_pass.id: _passing_series(0), s_fail.id: _failing_series()}

    monkeypatch.setattr(
        "archimedes.services.backtest_repository.get_all_daily_returns",
        lambda session, ids: dict(returns),
    )
    # verdicts_for_strategies (the boolean path) reads live_rigor_gate's loader;
    # _live_rigor_results_for_strategies (the new numeric path) reads its own
    # local loader — patch BOTH so the two paths see identical code and the
    # look-ahead leg is deterministic across both.
    monkeypatch.setattr(
        "archimedes.services.live_rigor_gate._load_strategy_code_safe",
        lambda strategy: _CLEAN_CODE,
    )
    _patch_code_loader(monkeypatch)

    # Independently reproduce the route's cohort context (same recipe as
    # test_library_badge_equals_live_gate_verdict_on_persisted_returns).
    # num_trials is self-contained (1, decouple #2), not cohort-derived.
    valid = {k: v for k, v in returns.items() if len(v) >= 10}
    pbo_scores = compute_pbo(valid) if len(valid) >= 2 else {}
    num_trials = 1
    avg_corr = compute_average_pairwise_correlation(valid) if len(valid) >= 2 else 0.0

    # THE grading event — the real gate, over the real cohort, written to the
    # passport rows the route below reads (#1746 / PR-B).
    _grade_curated_now()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/strategies/?limit=100")
    assert resp.status_code == 200
    by_id = {s["id"]: s for s in resp.json()["strategies"]}

    for sid, series in returns.items():
        expected = run_rigor_gate(
            strategy_id=sid,
            daily_returns=series,
            num_trials=num_trials,
            pbo_scores=pbo_scores,
            strategy_code=_CLEAN_CODE,
            in_sample_sharpe=None,
            average_correlation=avg_corr,
        )
        served = by_id[sid]
        assert served["dsr_p_value"] == pytest.approx(expected.dsr_p_value, rel=1e-9), (
            f"dsr_p_value for {sid}: served={served['dsr_p_value']} vs live gate={expected.dsr_p_value}"
        )
        assert served["pbo_score"] == pytest.approx(expected.pbo_score, rel=1e-9), (
            f"pbo_score for {sid}: served={served['pbo_score']} vs live gate={expected.pbo_score}"
        )
        assert served["out_of_sample_sharpe"] == pytest.approx(expected.oos_sharpe, rel=1e-9), (
            f"out_of_sample_sharpe for {sid}: served={served['out_of_sample_sharpe']} vs live gate={expected.oos_sharpe}"
        )
        assert served["deflated_sharpe_ratio"] == pytest.approx(expected.deflated_sharpe, rel=1e-9), (
            f"deflated_sharpe_ratio for {sid}: served={served['deflated_sharpe_ratio']} vs "
            f"live gate={expected.deflated_sharpe}"
        )


@pytest.mark.asyncio
async def test_leaderboard_never_disagrees_with_selection_bias_gate_route(monkeypatch):
    """ACCEPTANCE #3 (framed exactly as the issue states it): for a given
    strategy id, GET /api/strategies/ 's numeric rigor fields equal what
    GET /api/selection-bias/gate computes for that SAME id right now — the two
    live surfaces are queried back-to-back against the same injected DB state
    and must not disagree, rather than each being checked against an
    independently-reproduced expectation (belt-and-suspenders vs. the test
    above, which pins the exact machinery instead)."""
    from archimedes.api import strategies_routes as sr
    from archimedes.main import app

    strategies = sr.strategy_provider().list_strategies()
    assert len(strategies) >= 3, "need ≥3 curated strategies for a shared cohort"

    ids = [s.id for s in strategies[:3]]
    returns = {
        ids[0]: _passing_series(2),
        ids[1]: _passing_series(3),
        ids[2]: _failing_series(seed=101),
    }

    monkeypatch.setattr(
        "archimedes.services.backtest_repository.get_all_daily_returns",
        lambda session, ids: dict(returns),
    )
    monkeypatch.setattr(
        "archimedes.services.live_rigor_gate._load_strategy_code_safe",
        lambda strategy: _CLEAN_CODE,
    )
    _patch_code_loader(monkeypatch)
    monkeypatch.setattr(
        "archimedes.api.selection_bias_routes._load_strategy_code",
        lambda path: _CLEAN_CODE,
    )
    # The library route serves the STORED grade; /api/selection-bias/gate still
    # recomputes live (the deploy ladder — a deliberate, named seam in
    # docs/adr/rigor-verdict-of-record.md). Grading here is what puts the two on
    # the same gate vintage, which is exactly the condition under which they are
    # required to agree.
    _grade_curated_now()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        leaderboard_resp = await client.get("/api/strategies/?limit=100")
        gate_resp = await client.get("/api/selection-bias/gate")
    assert leaderboard_resp.status_code == 200
    assert gate_resp.status_code == 200

    leaderboard_by_id = {s["id"]: s for s in leaderboard_resp.json()["strategies"]}
    gate_by_id = {s["strategy_id"]: s for s in gate_resp.json()["strategies"]}

    checked = 0
    for sid in ids:
        lb = leaderboard_by_id.get(sid)
        gate = gate_by_id.get(sid)
        if lb is None or gate is None:
            continue
        # Both routes recompute num_trials/cohort context from the FULL injected
        # returns dict (3 series here), so — for THIS shared fixture — the two
        # independently-computed live gates should agree on every number.
        checked += 1
        assert lb["dsr_p_value"] == pytest.approx(gate["dsr_p_value"], rel=1e-6, abs=1e-9), (
            f"dsr_p_value disagreement for {sid}: leaderboard={lb['dsr_p_value']} vs gate={gate['dsr_p_value']}"
        )
        assert lb["pbo_score"] == pytest.approx(gate["pbo_score"], rel=1e-6, abs=1e-9), (
            f"pbo_score disagreement for {sid}: leaderboard={lb['pbo_score']} vs gate={gate['pbo_score']}"
        )
        assert lb["out_of_sample_sharpe"] == pytest.approx(gate["oos_sharpe"], rel=1e-6, abs=1e-9), (
            f"out_of_sample_sharpe disagreement for {sid}: "
            f"leaderboard={lb['out_of_sample_sharpe']} vs gate={gate['oos_sharpe']}"
        )
        assert lb["deflated_sharpe_ratio"] == pytest.approx(gate["deflated_sharpe"], rel=1e-6, abs=1e-9), (
            f"deflated_sharpe_ratio disagreement for {sid}: "
            f"leaderboard={lb['deflated_sharpe_ratio']} vs gate={gate['deflated_sharpe']}"
        )
    assert checked >= 1, "no shared ids resolved between the two routes — fixture setup is broken"


def test_to_strategy_response_serves_null_not_fixture_without_live_rigor_result():
    """#1187 (claim-integrity): when the live rigor gate could not run
    (``rigor_result is None`` — insufficient/no persisted returns, or a batch
    failure), the four numeric rigor fields must serve ``None`` — the honest
    "not run" — NEVER the strategy's stored ``s.<field>`` values. Those columns
    trace back to a migrated test-fixture snapshot
    (``backend/tests/fixtures/backtest_fixtures_snapshot.json``, PR #863) that
    predates the current DSR convention and gate threshold (#901) and cannot be
    reproduced by any single code version — presenting one as measured next to
    a ``pending`` badge is exactly the defect #1187 tracks.

    Direct unit call (no HTTP, no DB seeding needed): construct a ``Strategy``
    carrying non-None values for all four fields — mirroring exactly what the
    real migrated fixture rows look like — and confirm none of them leak
    through when the live gate result is unavailable. Adversarial check: with
    the pre-#1187 fallback chain restored (``s.<field> if s.<field> is not None
    else (bt.<field> if bt else None)``), this test fails — it would observe
    0.283312 / 0.611531 / 0.373116 / 0.930283 instead of ``None``.
    """
    from archimedes.api.strategies_routes import _to_strategy_response
    from archimedes.models.strategy import Strategy

    # Values lifted from the real migrated fixture (faber_2007_sma200_timing in
    # backend/tests/fixtures/backtest_fixtures_snapshot.json) so this test fails
    # exactly the way the live #1187 bug did, not against an arbitrary sentinel.
    s = Strategy(
        id="test-1187-fixture-stub",
        deflated_sharpe_ratio=0.283312,
        dsr_p_value=0.611531,
        pbo_score=0.373116,
        out_of_sample_sharpe=0.930283,
    )

    resp = _to_strategy_response(s, None)

    assert resp.rigor_gate_status == "pending"
    assert resp.deflated_sharpe_ratio is None, (
        f"deflated_sharpe_ratio must be None, not the fixture value; got {resp.deflated_sharpe_ratio}"
    )
    assert resp.dsr_p_value is None, f"dsr_p_value must be None, not the fixture value; got {resp.dsr_p_value}"
    assert resp.pbo_score is None, f"pbo_score must be None, not the fixture value; got {resp.pbo_score}"
    assert resp.out_of_sample_sharpe is None, (
        f"out_of_sample_sharpe must be None, not the fixture value; got {resp.out_of_sample_sharpe}"
    )


def test_to_strategy_response_serves_null_not_bt_fixture_without_live_rigor_result(monkeypatch):
    """#1187 adversarial gap (found in re-review): the sibling test above only
    guards the ``s.<field>`` half of the removed fallback chain — ``bt`` is
    always ``None`` there (a bare ``Strategy`` against an empty tmp DB), so a
    partial revert that restored ONLY ``(bt.<field> if bt else None)`` would
    pass every existing guard. This test persists the ``bt`` half: a
    ``BacktestResult`` carrying non-None rigor numbers is what
    ``strategy_provider().get_backtest_result`` returns for this strategy id,
    while ``s.<field>`` stays ``None`` and ``rigor_result`` stays ``None``
    (live gate could not run). Adversarial check: reintroducing
    ``(bt.deflated_sharpe_ratio if bt else None)`` (etc.) on any of the four
    keys makes this fail — it would observe 0.283312 / 0.611531 / 0.373116 /
    0.930283 (the same real fixture values as the sibling test) instead of
    ``None``.
    """
    from archimedes.api.strategies_routes import _to_strategy_response, strategy_provider
    from archimedes.models.backtest import BacktestResult
    from archimedes.models.strategy import Strategy

    s = Strategy(id="test-1187-bt-stub")  # s.<field> defaults None — bt half only
    bt = BacktestResult(
        strategy_id=s.id,
        sharpe_ratio=1.0,
        sortino_ratio=1.0,
        max_drawdown=0.1,
        cagr=0.1,
        calmar_ratio=1.0,
        win_rate=0.5,
        profit_factor=1.5,
        total_trades=10,
        avg_holding_period_days=5.0,
        correlation_to_spy=0.5,
        correlation_to_btc=0.0,
        deflated_sharpe_ratio=0.283312,
        dsr_p_value=0.611531,
        pbo_score=0.373116,
        out_of_sample_sharpe=0.930283,
    )
    monkeypatch.setattr(strategy_provider(), "get_backtest_result", lambda strategy_id: bt)

    resp = _to_strategy_response(s, None)

    assert resp.rigor_gate_status == "pending"
    assert resp.deflated_sharpe_ratio is None, (
        f"deflated_sharpe_ratio must be None, not the bt fixture value; got {resp.deflated_sharpe_ratio}"
    )
    assert resp.dsr_p_value is None, f"dsr_p_value must be None, not the bt fixture value; got {resp.dsr_p_value}"
    assert resp.pbo_score is None, f"pbo_score must be None, not the bt fixture value; got {resp.pbo_score}"
    assert resp.out_of_sample_sharpe is None, (
        f"out_of_sample_sharpe must be None, not the bt fixture value; got {resp.out_of_sample_sharpe}"
    )


def test_to_strategy_response_surfaces_backtest_provenance(monkeypatch):
    """Left-behind batch close (docs/sprint/a6-rerun.md / sprint README row 5):
    ``backtest_engine`` and ``cost_model_id`` have lived on ``BacktestResultRecord``
    since the cost SSOT / 2026-08-03 provenance audit and were already declared on
    ``StrategyResponse``, but no construction site in strategies_routes.py ever
    populated them from ``bt`` — the values stopped at the DB. Same
    monkeypatch-the-provider pattern as the #1187 sibling tests above: a
    ``BacktestResult`` carrying real engine/cost-model values is what
    ``strategy_provider().get_backtest_result`` returns, and both must reach the
    served response verbatim.

    Adversarial check: with the two ``backtest_engine=``/``cost_model_id=`` kwargs
    removed from ``_to_strategy_response``'s ``StrategyResponse(...)`` call, this
    test fails — it would observe ``None`` for both instead of the real values.
    """
    from archimedes.api.strategies_routes import _to_strategy_response, strategy_provider
    from archimedes.models.backtest import BacktestResult
    from archimedes.models.strategy import Strategy

    s = Strategy(id="test-provenance-bt-stub")
    bt = BacktestResult(
        strategy_id=s.id,
        sharpe_ratio=1.0,
        sortino_ratio=1.0,
        max_drawdown=0.1,
        cagr=0.1,
        calmar_ratio=1.0,
        win_rate=0.5,
        profit_factor=1.5,
        total_trades=10,
        avg_holding_period_days=5.0,
        correlation_to_spy=0.5,
        correlation_to_btc=0.0,
        backtest_engine="backtrader",
        cost_model_id="cm1:d10:s5",
    )
    monkeypatch.setattr(strategy_provider(), "get_backtest_result", lambda strategy_id: bt)

    resp = _to_strategy_response(s, None)

    assert resp.backtest_engine == "backtrader"
    assert resp.cost_model_id == "cm1:d10:s5"


def test_to_strategy_response_backtest_provenance_is_none_without_persisted_backtest():
    """No BacktestResultRecord row (``bt is None``) must serve None for both
    provenance fields — never a fabricated engine name or cost-model id."""
    from archimedes.api.strategies_routes import _to_strategy_response
    from archimedes.models.strategy import Strategy

    s = Strategy(id="test-provenance-no-bt")

    resp = _to_strategy_response(s, None)

    assert resp.backtest_engine is None
    assert resp.cost_model_id is None


def test_to_strategy_response_provenance_from_real_persisted_backtest_row():
    """The same claim as the monkeypatched test above, but end-to-end through the
    REAL stack: a row written by ``insert_backtest_if_missing`` into the actual
    ``backtest_results`` table, read back by the real
    ``LocalStrategyProvider.get_backtest_result``.

    The monkeypatched sibling stubs ``get_backtest_result`` to hand back a
    hand-built ``BacktestResult``, so it only proves ``_to_strategy_response``
    copies two attributes off whatever object it is given — it cannot see a break
    anywhere in the chain that actually carries the values:
    ``BacktestResultRecord.from_backtest_result`` (write) →
    ``backtest_results.backtest_engine`` / ``.cost_model_id`` (columns) →
    ``latest_backtests_by_strategy`` (read) →
    ``BacktestResultRecord.to_backtest_result`` (hydrate). Drop either column
    from either mapper and the stubbed test still passes; this one fails.
    """
    from archimedes.api._route_helpers import strategy_provider
    from archimedes.api.strategies_routes import _to_strategy_response
    from archimedes.db import get_session
    from archimedes.models.backtest import BacktestResult
    from archimedes.models.strategy import Strategy
    from archimedes.services.backtest_repository import insert_backtest_if_missing

    strategy_id = "test-provenance-real-db-row"
    result = BacktestResult(
        strategy_id=strategy_id,
        sharpe_ratio=1.0,
        sortino_ratio=1.0,
        max_drawdown=0.1,
        cagr=0.1,
        calmar_ratio=1.0,
        win_rate=0.5,
        profit_factor=1.5,
        total_trades=10,
        avg_holding_period_days=5.0,
        correlation_to_spy=0.5,
        correlation_to_btc=0.0,
        backtest_engine="vectorbt",
        cost_model_id="cm2:d20:s7",
    )
    with get_session() as session:
        insert_backtest_if_missing(
            session,
            strategy_id=strategy_id,
            content_hash="provenance-real-db-h1",
            result=result,
            source_pipeline="test",
        )
        session.commit()

    # Fresh provider: LocalStrategyProvider memoises ``_backtests`` per instance,
    # and the lru_cached singleton may already have been built (and its cache
    # populated) by an earlier test in this module.
    strategy_provider.cache_clear()

    resp = _to_strategy_response(Strategy(id=strategy_id), None)

    assert resp.backtest_engine == "vectorbt"
    assert resp.cost_model_id == "cm2:d20:s7"


@pytest.mark.asyncio
async def test_leaderboard_serves_null_not_fixture_without_real_returns(monkeypatch):
    """End-to-end companion to the unit test above, over the REAL curated
    library with the REAL migrated fixture data seeded into the DB (#863's
    ``strategy_backtest_fixtures`` table — the exact source the issue names) —
    not a synthetic Strategy. With NO persisted daily returns for anyone,
    ``GET /api/strategies/`` must still serve every strategy's numeric rigor
    fields as ``None`` and its badge as ``pending``, never the seeded fixture
    number. Seeding the fixture table (mirroring ``test_api_routes.py``'s
    ``_use_tmp_db``) is what makes this a real regression guard rather than
    a vacuous one: against pre-#1187 code every strategy here has a non-None
    ``s.deflated_sharpe_ratio`` fixture value available to leak through."""
    import json
    from pathlib import Path

    from archimedes.api._route_helpers import strategy_provider
    from archimedes.db import get_session
    from archimedes.main import app
    from archimedes.models.backtest_fixtures_store import FIXTURE_FIELDS, StrategyBacktestFixture

    snapshot_path = Path(__file__).parent / "fixtures" / "backtest_fixtures_snapshot.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    with get_session() as session:
        for stem, rec in snapshot.items():
            session.merge(StrategyBacktestFixture(stem=stem, **{field: rec[field] for field in FIXTURE_FIELDS}))
        session.commit()
    strategy_provider.cache_clear()

    monkeypatch.setattr(
        "archimedes.services.backtest_repository.get_all_daily_returns",
        lambda session, ids: {},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/strategies/?limit=100")
    assert resp.status_code == 200
    served = resp.json()["strategies"]
    assert served
    # Sanity: the seeded fixture data actually landed on the in-memory Strategy
    # objects (else this test would vacuously pass regardless of the fix, the
    # exact trap CLAUDE.md's guard-adversarial-pass rule warns about).
    strategies = strategy_provider().list_strategies()
    assert any(s.deflated_sharpe_ratio is not None for s in strategies), (
        "fixture seed did not reach strategy_provider() — this test would pass vacuously regardless of the #1187 fix"
    )
    for s in served:
        assert s["rigor_gate_status"] == "pending", (
            f"{s['id']}: expected pending badge with no persisted returns, got {s['rigor_gate_status']}"
        )
        assert s["deflated_sharpe_ratio"] is None, (
            f"{s['id']}: deflated_sharpe_ratio must be None (not the seeded fixture number) when the "
            f"live gate could not run; got {s['deflated_sharpe_ratio']}"
        )
        assert s["dsr_p_value"] is None, f"{s['id']}: dsr_p_value must be None; got {s['dsr_p_value']}"
        assert s["pbo_score"] is None, f"{s['id']}: pbo_score must be None; got {s['pbo_score']}"
        assert s["out_of_sample_sharpe"] is None, (
            f"{s['id']}: out_of_sample_sharpe must be None; got {s['out_of_sample_sharpe']}"
        )


@pytest.mark.asyncio
async def test_single_strategy_endpoint_numeric_fields_equal_live_gate(monkeypatch):
    """The single-strategy fetch path (GET /api/strategies/{id}) serves the
    numbers the real gate produced — the same ones the list path serves, from
    the same stored grade. Before #1746 this route computed them per request,
    which is how it came to disagree with the strategy's own passport."""
    from archimedes.api import strategies_routes as sr
    from archimedes.main import app

    strategies = sr.strategy_provider().list_strategies()
    assert strategies, "need at least one curated strategy"
    target = strategies[0]
    series = _passing_series(5)

    # Patch BOTH persisted-returns readers. This route grades through
    # `_live_rigor_result_for_one` -> `_live_rigor_results_for_strategies`,
    # whose DB boundary is `get_all_daily_returns` (the same boundary the
    # sibling tests above stub) — it is NOT a loop over `get_daily_returns`
    # since #1662 batched it into one query, so stubbing only the single-row
    # reader leaves the cohort read live and the assertions below fall through
    # to `None`. Both stubs express the identical fixture: `target` has a
    # passing series, every other strategy has none.
    monkeypatch.setattr(
        "archimedes.services.backtest_repository.get_daily_returns",
        lambda session, sid: series if sid == target.id else [],
    )
    monkeypatch.setattr(
        "archimedes.services.backtest_repository.get_all_daily_returns",
        lambda session, ids: {target.id: series} if target.id in ids else {},
    )
    monkeypatch.setattr(
        "archimedes.api.selection_bias_routes._load_strategy_code",
        lambda path: _CLEAN_CODE,
    )
    _grade_curated_now()

    # One valid series in the cohort, so the grading job runs with no cohort PBO
    # and zero average correlation — exactly the arguments reproduced here.
    expected = run_rigor_gate(
        strategy_id=target.id,
        daily_returns=series,
        num_trials=1,
        strategy_code=_CLEAN_CODE,
        in_sample_sharpe=None,
        paper_claimed_sharpe=target.paper_claimed_sharpe,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/api/strategies/{target.id}")
    assert resp.status_code == 200
    served = resp.json()

    assert served["dsr_p_value"] == pytest.approx(expected.dsr_p_value, rel=1e-9)
    assert served["pbo_score"] == pytest.approx(expected.pbo_score, rel=1e-9)
    assert served["out_of_sample_sharpe"] == pytest.approx(expected.oos_sharpe, rel=1e-9)
    assert served["deflated_sharpe_ratio"] == pytest.approx(expected.deflated_sharpe, rel=1e-9)
    assert served["passes_rigor_gate"] == expected.passes_all
    # #1358 A4: a curated strategy the live gate actually graded carries its
    # provenance — self-contained N=1 (decouple #2), never a bare/absent number.
    assert served["num_trials_in_selection"] == 1
    assert served["num_trials_scope"] == "curated_self_contained"


# ── Cohort invariance under the ?status= filter (#1172 review follow-up) ────
#
# Fixing the PAGINATION dependence alone left a second way for the same badge to
# change with how it was requested: the route graded
# `list_strategies(status=...)`, a SUBSET, while the detail/passport path grades
# `_library_cohort_including()` — which calls `list_strategies()` with NO filter.
# So `?status=candidate` and `?status=validated` could disagree with each other
# and with the passport for one strategy. The cohort must be the full library;
# `status` is a display concern only.


@pytest.mark.asyncio
async def test_the_served_verdict_is_identical_under_every_status_filter(monkeypatch):
    """One strategy id, one verdict — whatever ``?status=`` the caller passed.

    The original #1172 defect was that the route graded
    ``list_strategies(status=…)``, a SUBSET, so ``?status=candidate`` and
    ``?status=validated`` could disagree with each other and with the passport
    for one strategy. The route grades nothing now, so the three requests are
    three reads of one row — asserted on the SERVED payload rather than on a
    spy over a cohort that no longer exists.

    MUTATION: re-derive the badge in ``_to_strategy_response`` from a gate run
    over ``strategy_provider().list_strategies(status=…)``. The filtered cohorts
    differ, so the served verdicts diverge and this reddens.
    """
    import archimedes.api.strategies_routes as sr
    from archimedes.main import app

    library = sr.strategy_provider().list_strategies()
    returns = {s.id: _passing_series(i) for i, s in enumerate(library)}
    monkeypatch.setattr(
        "archimedes.services.backtest_repository.get_all_daily_returns",
        lambda session, ids: {sid: returns[sid] for sid in ids if sid in returns},
    )
    _patch_code_loader(monkeypatch)
    _grade_curated_now()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r_all = await client.get("/api/strategies/?limit=100")
        r_candidate = await client.get("/api/strategies/?limit=100&status=candidate")
        r_validated = await client.get("/api/strategies/?limit=100&status=validated")

    assert r_all.status_code == r_candidate.status_code == r_validated.status_code == 200

    _VERDICT_KEYS = ("rigor_gate_status", "passes_rigor_gate", "dsr_p_value", "pbo_score", "sharpe_ratio")
    unfiltered = {s["id"]: s for s in r_all.json()["strategies"]}
    assert unfiltered, "expected curated strategies in the library"
    for filtered in (r_candidate, r_validated):
        for row in filtered.json()["strategies"]:
            for key in _VERDICT_KEYS:
                assert row[key] == unfiltered[row["id"]][key], (
                    f"{row['id']} answered differently for {key} under a status filter: "
                    f"{row[key]} vs {unfiltered[row['id']][key]}"
                )

    # And the filter still actually filters the RESPONSE — this must not have
    # turned ?status= into a no-op.
    assert r_candidate.json()["total"] <= r_all.json()["total"]


@pytest.mark.asyncio
async def test_the_served_verdict_is_identical_under_every_page(monkeypatch):
    """Companion to the above, for the original #1173 defect: a badge must not
    depend on which page the strategy landed on.

    Verified against production before that fix: strategy ``d90b357a…4bbd``
    graded False in a 5-item window but True in the full-library view. Same
    mutation reddens this: grade the window instead of reading the row.
    """
    import archimedes.api.strategies_routes as sr
    from archimedes.main import app

    library = sr.strategy_provider().list_strategies()
    assert len(library) >= 6, "need a library big enough to page"
    returns = {s.id: _passing_series(i) for i, s in enumerate(library)}
    monkeypatch.setattr(
        "archimedes.services.backtest_repository.get_all_daily_returns",
        lambda session, ids: {sid: returns[sid] for sid in ids if sid in returns},
    )
    _patch_code_loader(monkeypatch)
    _grade_curated_now()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        page_a = await client.get("/api/strategies/?limit=2&offset=4")
        whole = await client.get("/api/strategies/?limit=100&offset=0")

    by_id = {s["id"]: s for s in whole.json()["strategies"]}
    rows = page_a.json()["strategies"]
    assert rows, "expected a non-empty page"
    for row in rows:
        for key in ("rigor_gate_status", "passes_rigor_gate", "dsr_p_value", "pbo_score", "sharpe_ratio"):
            assert row[key] == by_id[row["id"]][key], (
                f"{row['id']} answered differently for {key} on a 2-item page than in the full library"
            )


# ── GET /api/strategies/{id} must not 500 for a strategy with no linked
# paper (#1342) ──────────────────────────────────────────────────────────
#
# Curated strategies always have papers[0], so the legacy scalar fields
# (paper_arxiv_id / paper_title) never went through the None branch for
# them. GENERATED strategies (fusion/architect, ingested straight into
# strategy_passports with no paper_refs) do: _passport_to_strategy_response
# passes ``first.arxiv_id if first else None`` into a field declared
# ``str = ""``, which pydantic 2.x rejects — turning a genuine "no paper"
# into an HTTP 500 instead of a null field.


@pytest.mark.asyncio
async def test_single_strategy_endpoint_200s_with_no_linked_paper():
    """A generated (passport) strategy with zero linked papers must serve
    200 with paper_arxiv_id/paper_title == None, not 500."""
    from archimedes.db import get_session
    from archimedes.main import app
    from archimedes.models.strategy import StrategyPassport
    from archimedes.services.passport_loader import ingest_passport

    strategy_id = "test-1342-no-linked-paper"
    passport = StrategyPassport(
        id=strategy_id,
        papers=[],
        methodology_summary="A strategy with no linked paper (e.g. a fresh generation).",
        asset_universe=["SPY"],
    )
    with get_session() as session:
        ingest_passport(session, passport, generation_method="fusion")
        session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/api/strategies/{strategy_id}")

    assert resp.status_code == 200, resp.text
    served = resp.json()
    assert served["paper_arxiv_id"] is None
    assert served["paper_title"] is None
    assert served["papers"] == []


# ── Publish rights: same responses, O(1) queries (#1663) ────────────────────
#
# `GET /api/strategies/` called `wallet_can_publish` once per response row — one
# round trip per row, each paying a `pool_pre_ping` SELECT 1 first, and paid
# ONLY by signed-in callers (the per-row call short-circuits on anonymous), which
# is backwards for a demo. `_publishable_strategy_ids` replaces the loop with one
# IN query. The query-count guards and their adversarial control live in
# `test_publishable_strategy_ids.py`; what these two tests pin is that the
# SERVED RESPONSE did not move — anonymous and wallet-linked alike.

_PUBLISH_CALLER = "0x" + "d4" * 20


def _per_row_publishable_ids(session, strategy_ids, wallet_address, *, is_example):
    """The pre-#1663 per-row gate, drop-in-shaped for _publishable_strategy_ids.

    `bool(caller) and wallet_can_publish(...)` per row is verbatim what both
    Library loops evaluated, so swapping this in serves the OLD answers through
    the CURRENT route.
    """
    from archimedes.models.strategy_generators import wallet_can_publish

    return {
        sid
        for sid in strategy_ids
        if bool(wallet_address)
        and wallet_can_publish(session, strategy_id=sid, wallet_address=wallet_address, is_example=is_example)
    }


async def _library_json(monkeypatch, caller, publishable_impl=None):
    """GET /api/strategies/?limit=10, optionally with the per-row gate swapped in."""
    import archimedes.api.strategies_routes as sr
    from archimedes.main import app

    # Hermeticity: an ambient PLATFORM_ADMIN_WALLETS would make the caller an
    # admin, flip every can_publish to True, and silently defeat the
    # "not all(flags)" non-vacuity check below.
    monkeypatch.delenv("PLATFORM_ADMIN_WALLETS", raising=False)
    monkeypatch.setattr(sr, "get_linked_wallet_address", lambda request: caller)
    monkeypatch.setattr(
        "archimedes.services.backtest_repository.get_all_daily_returns",
        lambda session, ids: {},
    )
    if publishable_impl is not None:
        monkeypatch.setattr(sr, "_publishable_strategy_ids", publishable_impl)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/strategies/?limit=10")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _seed_one_generator_row(strategy_id: str, wallet: str) -> None:
    """Give `wallet` publish rights over exactly ONE curated id.

    Exactly one, deliberately: an all-True or all-False page would make the
    byte-identity assertion below pass for an implementation that ignored the
    DB entirely.
    """
    from archimedes.db import get_session
    from archimedes.models.identity import WalletIdentity
    from archimedes.models.strategy_generators import record_generator

    with get_session() as session:
        session.merge(WalletIdentity(wallet_address=wallet, actor_class="human"))
        # The production writer, which is insert-if-not-exists — `archimedes.db`'s
        # engine is a module-level singleton, so the per-test DATABASE_URL does
        # not actually give each test a fresh file and a raw INSERT would trip
        # the (strategy_id, wallet_address) unique constraint on the second test.
        record_generator(session, strategy_id=strategy_id, wallet_address=wallet)
        session.commit()


@pytest.mark.asyncio
async def test_library_response_identical_to_per_row_gate_for_a_linked_wallet(monkeypatch):
    """A signed-in caller's page is byte-identical to what the per-row gate served."""
    import archimedes.api.strategies_routes as sr

    strategies = sr.strategy_provider().list_strategies()
    assert strategies, "need at least one curated strategy"
    _seed_one_generator_row(strategies[0].id, _PUBLISH_CALLER)

    batched = await _library_json(monkeypatch, _PUBLISH_CALLER)
    legacy = await _library_json(monkeypatch, _PUBLISH_CALLER, publishable_impl=_per_row_publishable_ids)

    # Non-vacuity: the fixture must produce a genuine MIX, or "identical" would
    # hold for an implementation that hardcoded one answer.
    flags = [s["can_publish"] for s in batched["strategies"]]
    assert any(flags), "seed did not reach the route — every row is can_publish=False"
    assert not all(flags), "every row is can_publish=True — the fixture grants too much to discriminate"

    assert batched == legacy


@pytest.mark.asyncio
async def test_library_response_identical_to_per_row_gate_for_an_anonymous_visitor(monkeypatch):
    """The anonymous short-circuit is not relaxed: a visitor still sees
    can_publish=False everywhere, exactly as before."""
    import archimedes.api.strategies_routes as sr

    strategies = sr.strategy_provider().list_strategies()
    assert strategies, "need at least one curated strategy"
    # Seeded for a DIFFERENT wallet — a visitor must not inherit its rights.
    _seed_one_generator_row(strategies[0].id, _PUBLISH_CALLER)

    batched = await _library_json(monkeypatch, None)
    legacy = await _library_json(monkeypatch, None, publishable_impl=_per_row_publishable_ids)

    assert batched == legacy
    assert not any(s["can_publish"] for s in batched["strategies"])


# ═══════════════════════════════════════════════════════════════════════════
# The executable DSL spec on the passport detail route (#1646)
# ═══════════════════════════════════════════════════════════════════════════
#
# `StrategyResponse.strategy_spec` is the validated machine-readable DSL that
# actually runs the strategy. It existed on `StrategyPassport.strategy_spec`
# and `strategy_store.strategy_spec` but was stripped before the API boundary,
# so the passport page could never show a reader the rules behind the prose.
#
# The cases below pin the whole contract, positive and negative:
#   1. the owner of a row with a spec gets the spec back, unredacted;
#   2. a non-owner does NOT — INCLUDING on a published row the same request
#      reads in full, because the #1557 matrix files "machine-readable DSL
#      spec" under REASONING, and publishing shares the result, not the
#      derivation;
#   3. no LIST surface carries it (the issue's explicit anti-goal), pinned
#      structurally — every `.strategy_spec =` assignment in the routes module
#      must sit inside `get_strategy` — AND at each of the two shared response
#      builders, which is the seam a future refactor would actually break;
#   4. a corrupt spec column degrades to null instead of 500ing the route.
#
# Hermetic: `_spec_tmp_db` genuinely rebinds `archimedes.db.engine` /
# `SessionLocal` to a per-test tmp SQLite via the shared `redirect_to_tmp_sqlite`
# helper and restores them after. That is deliberately NOT the file-level
# autouse `_use_tmp_db` above, which only sets `DATABASE_URL` + calls
# `init_db()`: `archimedes.db` binds its engine once at import time, so setting
# the env var afterwards rebinds nothing and these tests would read and WRITE
# the ambient dev database (observed while writing them: 37 real curated
# passports appearing in a list assertion, and a UNIQUE-constraint collision
# between two parametrizations of the same test, which under a genuinely
# per-test DB is impossible). Fixing this file's autouse fixture wholesale is a separate
# change; scoping the real isolation to the new tests keeps them honest without
# perturbing the 45 that already pass.
#
# Auth is the signed SIWE fixture cookie that conftest's autouse
# `_legacy_siwe_test_adapter` maps onto a canonical Better Auth user (the shape
# established by test_brief_on_passport.py / test_strategy_ownership.py) — a
# real signed session, not header spoofing. No live DB, no Redis, no network.

_SPEC_W_OWNER = "0xAbC0000000000000000000000000000000001646"  # mixed case on purpose
_SPEC_W_STRANGER = "0x0000000000000000000000000000000000001647"

# A real DSL-shaped spec, not a `{"a": 1}` placeholder: a nested dict is what
# the field actually carries, and a flat one would not prove the structure
# survives the round trip through a Text column intact.
_SPEC_FIXTURE = {
    "asset_universe": ["SPY", "TLT"],
    "entry": {"all": [{"indicator": "rsi", "window": 14, "op": "<", "value": 30}]},
    "exit": {"any": [{"indicator": "rsi", "window": 14, "op": ">", "value": 70}]},
    "position_sizing": "equal_weight",
    "rebalance_frequency": "weekly",
}
# The distinctive leaf of the fixture. Asserting on THIS rather than on the
# `strategy_spec` key is what makes the redaction tests real: a refactor that
# free-forms the spec into some other key (a nested passport blob, a debug
# echo) still trips them.
_SPEC_TELL = "rsi"


@pytest.fixture
def _spec_tmp_db(tmp_path):
    """Genuinely-isolated per-test SQLite — see the section note above."""
    from tests.db_isolation import redirect_to_tmp_sqlite

    yield from redirect_to_tmp_sqlite(tmp_path)


def _spec_user_id(wallet: str) -> str:
    """The canonical user id conftest's legacy SIWE adapter mints for a fixture
    cookie. Derived from the wallet rather than hard-coded so it cannot drift
    from the adapter (whose payload is lower-cased at signing)."""
    return f"legacy-test:{wallet.lower()}"


def _spec_cookies(wallet: str) -> dict[str, str]:
    import time

    from archimedes.api.auth_siwe import _COOKIE_NAME, _sign_session

    return {_COOKIE_NAME: _sign_session(wallet, time.time())}


def _seed_spec_row(
    sid: str,
    *,
    strategy_spec: str | None,
    owner_user_id: str | None = None,
    is_published: bool = False,
) -> None:
    """A StrategyRecord (carrying the spec column + ownership) plus its
    StrategyPassportRecord mirror under the SAME id — what `_persist_candidate`
    writes for every generated strategy.

    `strategy_spec` is passed as the RAW column value (a JSON string, or a
    deliberately corrupt one) because that is what the defensive decoder under
    test actually receives.
    """
    from archimedes.db import get_session
    from archimedes.models.strategy_passport_record import StrategyPassportRecord
    from archimedes.models.strategy_store import StrategyRecord

    with get_session() as session:
        session.add(
            StrategyRecord(
                id=sid,
                content_hash=("0x" + sid).ljust(66, "0"),
                generation_method="debate",
                source_papers="[]",
                strategy_name="Spec Test Strategy",
                thesis="test thesis",
                asset_universe='["SPY"]',
                risk_profile="moderate",
                status="candidate",
                is_example=False,
                is_published=is_published,
                owner_user_id=owner_user_id,
                strategy_spec=strategy_spec,
            )
        )
        session.add(
            StrategyPassportRecord(
                id=sid,
                generation_method="debate",
                methodology_summary="Test methodology",
                asset_universe='["SPY"]',
                position_sizing="equal_weight",
                rebalance_frequency="weekly",
                status="candidate",
                regime_tag="regime_neutral",
                passes_rigor_gate=False,
                owner_user_id=owner_user_id,
            )
        )
        session.commit()


def _spec_client(wallet: str | None = None) -> AsyncClient:
    from archimedes.main import app

    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies=_spec_cookies(wallet) if wallet else None,
    )


@pytest.mark.asyncio
async def test_detail_route_returns_strategy_spec_to_the_row_owner(_spec_tmp_db):
    """THE feature: `GET /api/strategies/{id}` hands the owner back the exact
    validated DSL spec stored for their row, structurally intact.

    Asserted as whole-dict equality rather than a truthiness check — a
    partially-serialized spec (nested conditions flattened, numbers
    stringified) would still be truthy, and a spec the page renders as code is
    only worth rendering if it is the spec that runs.
    """
    import json

    sid = "spec-detail-owner"
    _seed_spec_row(sid, strategy_spec=json.dumps(_SPEC_FIXTURE), owner_user_id=_spec_user_id(_SPEC_W_OWNER))

    async with _spec_client(_SPEC_W_OWNER) as client:
        resp = await client.get(f"/api/strategies/{sid}")

    assert resp.status_code == 200, resp.text
    assert resp.json()["strategy_spec"] == _SPEC_FIXTURE


@pytest.mark.asyncio
@pytest.mark.parametrize("caller_wallet", [_SPEC_W_STRANGER, None], ids=["signed-in-stranger", "anonymous"])
async def test_detail_route_hides_strategy_spec_from_non_owners(_spec_tmp_db, caller_wallet):
    """PRIVACY GUARD — the negative half, and the reason the spec takes
    `is_strategy_reasoning_visible` rather than either of the two predicates
    already in scope at that line.

    The row is PUBLISHED, so `is_strategy_visible` grants the request in full:
    the caller legitimately reads the card, gets a 200, and sees the
    methodology. What they must NOT get is the executable spec. Reusing
    `is_strategy_visible` (the check that already ran ten lines up, and the
    obvious thing to reach for) would look correct and would hand every
    published user strategy's runnable definition to anonymous callers —
    exactly the #1557 hole, reopened on a new field.
    """
    import json

    sid = "spec-detail-published"
    _seed_spec_row(
        sid,
        strategy_spec=json.dumps(_SPEC_FIXTURE),
        owner_user_id=_spec_user_id(_SPEC_W_OWNER),
        is_published=True,
    )

    async with _spec_client(caller_wallet) as client:
        resp = await client.get(f"/api/strategies/{sid}")

    assert resp.status_code == 200, "published row must stay readable — this is a redaction, not a 404"
    body = resp.json()
    assert body["id"] == sid
    assert body["methodology_summary"] == "Test methodology", "the card itself is still public"
    assert body["strategy_spec"] is None
    assert _SPEC_TELL not in resp.text


def test_strategy_spec_is_assigned_only_inside_the_detail_route():
    """ANTI-GOAL GUARD (stated verbatim in #1646: "Do NOT expose
    ``strategy_spec`` on the **list** response"), enforced structurally.

    Parses ``strategies_routes.py`` and asserts that EVERY assignment to a
    ``.strategy_spec`` attribute in the module sits inside the single-strategy
    detail route's body. AST, not grep, so a comment mentioning the field or a
    read like ``row.strategy_spec`` cannot trip it and a real assignment cannot
    hide behind formatting.

    This is the guard a route-level list assertion could not honestly give.
    The two shared response builders below are each pinned individually, but
    "no list surface carries the spec" is a claim about the module as a whole:
    a third builder added next quarter would satisfy both of those tests and
    still leak. Here it fails.

    **#1818 P4 renamed the site, and this test now checks TWO things instead of
    one.** Every read route in this module is now a coroutine that hops to a
    worker thread plus a module-level ``_…_sync`` twin holding the body, so the
    detail route's two assignments live in ``_get_strategy_sync``. Allowing that
    name on its own would be weaker than what this guard used to claim: "only
    ``_get_strategy_sync`` assigns the spec" stops meaning "only the detail route
    does" the moment anything else starts calling that twin. So the second
    assertion pins the twin's callers to exactly the ``get_strategy`` route.
    """
    import ast
    import pathlib

    import archimedes.api.strategies_routes as mod

    tree = ast.parse(pathlib.Path(mod.__file__).read_text(encoding="utf-8"))

    offenders: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Assign):
                continue
            for target in sub.targets:
                if isinstance(target, ast.Attribute) and target.attr == "strategy_spec":
                    offenders.append((node.name, sub.lineno))

    assert offenders, "no `.strategy_spec = ` assignment found at all — the wiring is gone, not merely misplaced"

    detail_body = "_get_strategy_sync"
    bad = [(fn, line) for fn, line in offenders if fn != detail_body]
    assert not bad, (
        "`strategy_spec` may only be populated by the single-strategy detail route "
        f"(`get_strategy` → `{detail_body}`); found assignments in {bad} (function, line). "
        "A shared response builder that sets it puts an unbounded JSON blob on every row "
        "of every list payload, and on the public leaderboard."
    )

    callers = sorted(
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name != detail_body
        and any(isinstance(sub, ast.Name) and sub.id == detail_body for sub in ast.walk(node))
    )
    assert callers == ["get_strategy"], (
        f"`{detail_body}` holds the only `strategy_spec` assignments, so it must stay reachable "
        f"from the single-strategy detail route and nothing else; it is referenced by {callers}. "
        "A list route calling it would put the spec on a list payload without any assignment "
        "moving, which is the anti-goal with an extra hop in front of it."
    )


def test_passport_to_strategy_response_never_sets_strategy_spec(_spec_tmp_db):
    """GUARD: the SHARED passport response builder — behind Library
    (`_passport_responses`), the public leaderboard
    (`_public_generated_strategy_responses`) and the owned-rows board — must
    never populate `strategy_spec`, even from a row that has one.

    Pinned at the seam rather than only at a route: if a future edit moves the
    spec lookup INTO this helper, the detail-route test above still passes
    while every list surface silently starts shipping the field. Mirrors
    `test_passport_to_strategy_response_never_sets_brief_intent`, which guards
    `brief_intent` at this exact seam for the same reason.
    """
    import json

    from archimedes.api.strategies_routes import _passport_to_strategy_response
    from archimedes.db import get_session
    from archimedes.models.strategy_passport_record import StrategyPassportRecord

    sid = "spec-shared-builder-guard"
    _seed_spec_row(sid, strategy_spec=json.dumps(_SPEC_FIXTURE), owner_user_id=_spec_user_id(_SPEC_W_OWNER))

    with get_session() as session:
        record = session.query(StrategyPassportRecord).filter_by(id=sid).first()
        assert record is not None
        resp = _passport_to_strategy_response(record, session=session)

    assert resp.strategy_spec is None


def test_to_strategy_response_never_sets_strategy_spec():
    """GUARD, second seam: `_to_strategy_response` is the OTHER shared builder
    — it serves the curated library list (`list_strategies`, the route the
    anti-goal names by name) and `leaderboard_routes.py`'s curated board, as
    well as the curated branch of the detail route.

    So the spec is attached on the detail route's curated branch instead, and
    this pins that it is not attached here. Fed a passport that HAS a spec: a
    builder that ignored the field would pass a `strategy_spec=None` fixture
    trivially, which is exactly the "test passes against the unfixed code"
    trap.
    """
    from archimedes.api.strategies_routes import _to_strategy_response
    from archimedes.models.strategy import StrategyPassport

    s = StrategyPassport(
        id="spec-curated-builder-guard",
        papers=[],
        methodology_summary="curated",
        asset_universe=["SPY"],
        strategy_spec=_SPEC_FIXTURE,
    )
    resp = _to_strategy_response(s, None)

    assert resp.strategy_spec is None


@pytest.mark.asyncio
async def test_detail_route_serves_null_strategy_spec_for_a_corrupt_column(_spec_tmp_db):
    """A corrupt spec column degrades to `null` — not a 500, not a plausible
    fragment.

    `strategy_spec` is a Text column holding JSON, so a truncated write or a
    bad backfill is representable. `StrategyRecord.decoded_strategy_spec()`
    already fails soft for `to_dict()`; this pins that the passport route
    inherits that behaviour rather than acquiring a new 500 surface — the
    honest-absence rule applied to a field the page renders.
    """
    sid = "spec-detail-corrupt"
    _seed_spec_row(sid, strategy_spec='{"entry": [', owner_user_id=_spec_user_id(_SPEC_W_OWNER))

    async with _spec_client(_SPEC_W_OWNER) as client:
        resp = await client.get(f"/api/strategies/{sid}")

    assert resp.status_code == 200, resp.text
    assert resp.json()["strategy_spec"] is None
