"""Tests for selection_bias_routes — HTTP endpoints and schema validation.

Covers:
  - GET /api/selection-bias/gate  (library-level rigor gate)
  - GET /api/selection-bias/gate/{strategy_id}  (single-strategy gate)
  - POST /api/selection-bias/pbo  (pure-math PBO computation)
  - Pydantic schema construction and defaults
  - Pure helper functions (_load_strategy_code)

The GET /gate and GET /gate/{id} endpoints call init_db() internally; an
autouse fixture redirects DATABASE_URL to a per-test temporary SQLite file
so no persistent on-disk state is created and no external DB is required.
Tests pass with:
  env -i HOME=$HOME PATH=$PATH PYTHONPATH=backend python -m pytest \\
      backend/tests/test_selection_bias_routes.py -q
"""

from __future__ import annotations

import re

import numpy as np
import pytest
from archimedes.api.selection_bias_routes import (
    LibraryPbo,
    PBORequest,
    PBOResponse,
    RigorGateDetail,
    RigorGateResponse,
    StrategyRigorResult,
    _load_strategy_code,
)
from archimedes.services.rigor_evaluator import run_rigor_gate
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from tests.db_isolation import redirect_to_tmp_sqlite

# ── Hermetic DB fixture ────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _use_tmp_db(tmp_path):
    """Bind archimedes.db to a per-test temp SQLite file.

    The evaluate_rigor_gate endpoint queries persisted backtest data, so
    without isolation it reads sqlite:///./archimedes_chat.db (CWD-relative),
    which accumulates state across runs and across parallel workers.

    Setting DATABASE_URL and calling init_db() did not achieve that (#1243):
    archimedes.db builds engine/SessionLocal once at import, so the env var
    rebinds nothing and get_session() kept resolving the original engine. This
    file passed under the full suite only because an earlier file left a usable
    engine bound, and failed standalone against a stale on-disk schema.
    """
    yield from redirect_to_tmp_sqlite(tmp_path)


# ── CPCV honest not-run reporting (#771) ───────────────────────────────


class TestCpcvHonestNotRun:
    """#771: the selection-bias surface must not advertise CPCV as a method while
    emitting a bare "MISSING". The live route calls run_rigor_gate WITHOUT a
    cv_returns_matrix (the analytics-engine doesn't emit one yet), so CPCV must be
    reported as an explicit NOT_RUN status with its reason — never a bare placeholder
    and never a fabricated value.
    """

    @staticmethod
    def _series():
        rng = np.random.default_rng(7)
        return list(rng.normal(0.001, 0.01, 400))

    def test_cpcv_reports_explicit_not_run_label(self):
        # Exactly how selection_bias_routes.evaluate_rigor_gate calls it: no matrix.
        result = run_rigor_gate("s1", self._series(), num_trials=6)
        cpcv = result.gate_details["cpcv"]
        assert cpcv.startswith("NOT_RUN"), cpcv
        assert "combinatorial" in cpcv.lower(), "the not-run reason must be surfaced"
        assert cpcv != "MISSING", "no bare placeholder that implies a silent method"

    def test_cpcv_label_is_not_a_fabricated_value(self):
        # Anti-goal: the not-run label must not look like a computed CPCV verdict.
        cpcv = run_rigor_gate("s1", self._series(), num_trials=6).gate_details["cpcv"]
        assert not cpcv.startswith(("PASS", "FAIL")), "must not mimic a computed verdict"

    def test_cpcv_not_run_stays_non_gating(self):
        # CPCV is not enforced when absent: no positive_fraction is computed, so the
        # NOT_RUN status cannot, by itself, flip pass/fail for the other criteria.
        result = run_rigor_gate("s1", self._series(), num_trials=6)
        assert result.cpcv_positive_fraction is None
        assert isinstance(result.passes_all, bool)


# ── Schema unit tests ──────────────────────────────────────────────────


class TestRigorGateDetail:
    def test_default_values_are_missing(self):
        detail = RigorGateDetail()
        assert detail.dsr == "MISSING"
        assert detail.pbo == "MISSING"
        assert detail.oos_sharpe == "MISSING"
        assert detail.look_ahead == "MISSING"

    def test_custom_values_stored(self):
        detail = RigorGateDetail(
            dsr="PASS (p=0.98)",
            pbo="FAIL (PBO=0.6)",
            oos_sharpe="SET (OOS=1.2)",
            look_ahead="PASS",
        )
        assert detail.dsr == "PASS (p=0.98)"
        assert detail.pbo == "FAIL (PBO=0.6)"
        assert detail.oos_sharpe == "SET (OOS=1.2)"
        assert detail.look_ahead == "PASS"


class TestStrategyRigorResult:
    def test_required_fields_present(self):
        result = StrategyRigorResult(
            strategy_id="abc123",
            strategy_name="Test Strategy",
            passes_all=False,
            gate_details=RigorGateDetail(),
        )
        assert result.strategy_id == "abc123"
        assert result.strategy_name == "Test Strategy"
        assert result.passes_all is False

    def test_optional_fields_default_none(self):
        result = StrategyRigorResult(
            strategy_id="abc123",
            strategy_name="Test Strategy",
            passes_all=True,
            gate_details=RigorGateDetail(),
        )
        assert result.deflated_sharpe is None
        assert result.dsr_p_value is None
        assert result.pbo_score is None
        assert result.oos_sharpe is None
        assert result.in_sample_sharpe is None

    def test_optional_fields_accept_floats(self):
        result = StrategyRigorResult(
            strategy_id="abc123",
            strategy_name="Test Strategy",
            passes_all=False,
            gate_details=RigorGateDetail(),
            deflated_sharpe=1.23,
            dsr_p_value=0.97,
            pbo_score=0.2,
            oos_sharpe=0.8,
            in_sample_sharpe=1.5,
        )
        assert result.deflated_sharpe == pytest.approx(1.23)
        assert result.dsr_p_value == pytest.approx(0.97)
        assert result.pbo_score == pytest.approx(0.2)


class TestRigorGateResponse:
    def test_empty_library_response(self):
        resp = RigorGateResponse(strategies=[], total=0, passing=0, failing=0)
        assert resp.total == 0
        assert resp.passing == 0
        assert resp.failing == 0
        assert resp.strategies == []

    def test_counts_consistency(self):
        detail = RigorGateDetail()
        strats = [
            StrategyRigorResult(strategy_id=f"s{i}", strategy_name=f"S{i}", passes_all=(i == 0), gate_details=detail)
            for i in range(3)
        ]
        resp = RigorGateResponse(strategies=strats, total=3, passing=1, failing=2)
        assert resp.total == 3
        assert resp.passing == 1
        assert resp.failing == 2


class TestPBORequest:
    def test_default_s_partitions(self):
        req = PBORequest(returns_matrix={"s1": [0.001] * 50})
        assert req.s_partitions == 16

    def test_custom_s_partitions(self):
        req = PBORequest(returns_matrix={"s1": [0.001] * 50}, s_partitions=4)
        assert req.s_partitions == 4

    def test_matrix_stored(self):
        returns = [0.001, -0.002, 0.003]
        req = PBORequest(returns_matrix={"s1": returns})
        assert req.returns_matrix["s1"] == returns


class TestPBOResponse:
    def test_fields_stored(self):
        resp = PBOResponse(pbo_scores={"s1": 0.3, "s2": 0.3}, interpretation="PASSED rigor gate.")
        assert resp.pbo_scores["s1"] == pytest.approx(0.3)
        assert "PASSED" in resp.interpretation


# ── Pure helper function tests ──────────────────────────────────────────


class TestLoadStrategyCode:
    def test_nonexistent_path_returns_none(self):
        result = _load_strategy_code("/nonexistent/path/strategy.py")
        assert result is None

    def test_empty_string_returns_none(self):
        result = _load_strategy_code("")
        assert result is None

    def test_none_returns_none(self):
        result = _load_strategy_code(None)
        assert result is None

    def test_path_traversal_blocked(self):
        result = _load_strategy_code("../../../../etc/passwd")
        assert result is None


# ── HTTP endpoint tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gate_endpoint_returns_200():
    """GET /api/selection-bias/gate returns 200."""
    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/selection-bias/gate")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_gate_endpoint_has_required_top_level_keys():
    """Response body contains strategies, total, passing, failing."""
    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/selection-bias/gate")
    data = resp.json()
    for key in ("strategies", "total", "passing", "failing"):
        assert key in data, f"missing key '{key}' in response: {list(data.keys())}"


@pytest.mark.asyncio
async def test_gate_endpoint_counts_are_consistent():
    """total == passing + failing + pending (#1358) and strategies list length
    matches total. Pre-#1358 this asserted total == passing + failing, which
    silently counted every never-scored (pending) row as a rigor-gate failure."""
    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/selection-bias/gate")
    data = resp.json()
    assert data["total"] == data["passing"] + data["failing"] + data["pending"]
    assert len(data["strategies"]) == data["total"]


@pytest.mark.asyncio
async def test_gate_endpoint_pending_rows_excluded_from_failing():
    """#1358: a strategy with no persisted backtest data (this hermetic test's
    fresh tmp-sqlite DB has none) reports as ``pending``, not ``failing`` — a
    strategy with zero statistics computed must never render as a rigor-gate
    failure. Every ``StrategyRigorResult`` in the curated library response is
    marked ``pending`` in this fixture (no backtests have been persisted), so
    ``failing`` must be 0 and ``pending`` must equal ``total``."""
    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/selection-bias/gate")
    data = resp.json()
    assert data["total"] > 0, "expected at least one curated strategy in the fixture library"
    assert data["failing"] == 0
    assert data["pending"] == data["total"]
    assert all(s["pending"] is True for s in data["strategies"])


@pytest.mark.asyncio
async def test_gate_endpoint_scored_row_is_not_pending(monkeypatch):
    """#1358 round-2 review: the guard above is one-directional — every existing
    pending assertion is satisfied by "everything is pending" (this file's fresh,
    unpopulated tmp-sqlite DB), so a mutation that hoists ``pending=True`` out of
    the ``len(daily_returns) < 10`` branch onto EVERY row (converting a real
    rigor-gate FAILURE into a neutral "pending" chip) would still pass the whole
    suite. This is the missing inverse: a strategy the gate actually SCORES
    (>=10 persisted daily returns, injected at the ``get_all_daily_returns`` DB
    boundary per ``TestDegenerateSeriesExcludedFromCohort``'s pattern) must
    report ``pending is False`` — and since the injected series is the same
    deterministic strong-IS/weak-OOS shape ``TestOosCliffDenominator`` uses to
    force a genuine oos_sharpe FAIL, it must also be counted in ``failing``."""
    from archimedes.api import selection_bias_routes as routes
    from archimedes.main import app

    strategies = routes._provider().list_strategies()
    if not strategies:
        pytest.skip("no strategies in provider")
    target_id = strategies[0].id

    # Same deterministic strong-IS / weak-OOS shape as TestOosCliffDenominator
    # (no RNG -> version-independent): guaranteed to FAIL the oos_sharpe cliff
    # criterion, so this row's passes_all is deterministically False.
    amp = 0.01
    is_part = [0.003 + (amp if i % 2 == 0 else -amp) for i in range(700)]
    oos_part = [0.0015 + (amp if i % 2 == 0 else -amp) for i in range(300)]
    series = is_part + oos_part

    monkeypatch.setattr(
        "archimedes.services.backtest_repository.get_all_daily_returns",
        lambda session, ids: {target_id: series},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/selection-bias/gate")
    data = resp.json()
    row = next(s for s in data["strategies"] if s["strategy_id"] == target_id)
    assert row["pending"] is False, "a strategy the gate actually scored must not report pending"
    assert row["passes_all"] is False, "the deterministic series must fail the oos cliff"
    assert data["failing"] >= 1, (
        "a real rigor-gate failure must be counted in `failing`, not silently reclassified as pending"
    )


@pytest.mark.asyncio
async def test_gate_endpoint_degenerate_row_is_distinguishable_from_a_floor_failure(monkeypatch):
    """#1358 round-3: a zero-variance persisted series must be reported as its own
    thing on the wire, not left indistinguishable from a graded correctness failure.

    Why the field is needed at all — the mechanism, asserted here rather than
    described: a flat series makes ``dsr_p_value`` and ``oos_sharpe`` both None,
    and ``RigorGateResult.blocked_by_floor`` treats a None on either as a floor
    failure. So this row arrives with ``blocked_by_floor is True`` and
    ``min_passing_level is None`` — byte-for-byte the shape of a strategy that
    WAS fully graded and found broken. Every UI consumer of those two fields then
    says "fails an always-on correctness floor" about a series no floor ever got
    to measure. ``degenerate`` is the discriminator that makes the honest
    rendering possible; ``pending`` cannot serve, because there IS data here.

    Note the row is still *scored* (>= 10 returns), so ``pending`` must stay
    False — the two neutral states are not interchangeable and this asserts it.
    """
    from archimedes.api import selection_bias_routes as routes
    from archimedes.main import app

    strategies = routes._provider().list_strategies()
    if not strategies:
        pytest.skip("no strategies in provider")
    target_id = strategies[0].id

    # Zero-variance and long enough to clear the `< 10 returns` pending branch,
    # so the ONLY thing that can explain a neutral verdict here is degeneracy.
    series = [0.0] * 300

    monkeypatch.setattr(
        "archimedes.services.backtest_repository.get_all_daily_returns",
        lambda session, ids: {target_id: series},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/selection-bias/gate")
    assert resp.status_code == 200
    data = resp.json()
    row = next(s for s in data["strategies"] if s["strategy_id"] == target_id)

    assert row["degenerate"] is True, (
        "a zero-variance persisted series must be reported as degenerate on the wire — "
        "without this the UI cannot tell it apart from a graded correctness failure"
    )
    assert row["pending"] is False, (
        "degenerate is not pending: this row HAS persisted returns, they are merely flat, "
        "so 'not yet evaluated' would be a false claim"
    )
    # The shape that makes the field load-bearing. If these two ever stop
    # holding, the honest-rendering problem this field solves has changed and
    # the UI branches keyed on `degenerate` need re-reading.
    assert row["blocked_by_floor"] is True, (
        "documents WHY `degenerate` is needed: a flat series trips the floor mechanically"
    )
    assert row["min_passing_level"] is None


@pytest.mark.asyncio
async def test_gate_endpoint_ordinary_failure_is_not_marked_degenerate(monkeypatch):
    """The inverse of the guard above, without which it is one-directional.

    Every strategy in this file's fresh tmp-sqlite DB is pending, so a mutation
    that hoisted ``degenerate=True`` onto every scored row would satisfy the test
    above and quietly convert every genuine rigor-gate failure into a neutral
    "unevaluable" chip — the exact defect class #1358 exists to fix, just pointed
    the other way. A real (non-flat) series that fails the gate must report
    ``degenerate is False``.
    """
    from archimedes.api import selection_bias_routes as routes
    from archimedes.main import app

    strategies = routes._provider().list_strategies()
    if not strategies:
        pytest.skip("no strategies in provider")
    target_id = strategies[0].id

    # Same deterministic strong-IS / weak-OOS shape the pending inverse uses:
    # real variance throughout, guaranteed to fail the oos_sharpe cliff.
    amp = 0.01
    is_part = [0.003 + (amp if i % 2 == 0 else -amp) for i in range(700)]
    oos_part = [0.0015 + (amp if i % 2 == 0 else -amp) for i in range(300)]
    series = is_part + oos_part

    monkeypatch.setattr(
        "archimedes.services.backtest_repository.get_all_daily_returns",
        lambda session, ids: {target_id: series},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/selection-bias/gate")
    data = resp.json()
    row = next(s for s in data["strategies"] if s["strategy_id"] == target_id)

    assert row["passes_all"] is False, "the deterministic series must fail the oos cliff"
    assert row["degenerate"] is False, (
        "a graded failure on a real series must NOT be marked degenerate — that would "
        "excuse a genuine rigor-gate loss as 'nothing to measure'"
    )


@pytest.mark.asyncio
async def test_gate_endpoint_counts_non_negative():
    """total, passing, failing, pending are all non-negative integers."""
    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/selection-bias/gate")
    data = resp.json()
    assert data["total"] >= 0
    assert data["passing"] >= 0
    assert data["failing"] >= 0
    assert data["pending"] >= 0


@pytest.mark.asyncio
async def test_gate_endpoint_strategy_types():
    """strategy_id and strategy_name are strings; passes_all is bool."""
    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/selection-bias/gate")
    data = resp.json()
    for strat in data["strategies"]:
        assert isinstance(strat["strategy_id"], str)
        assert isinstance(strat["strategy_name"], str)
        assert isinstance(strat["passes_all"], bool)


@pytest.mark.asyncio
async def test_gate_single_strategy_404_for_nonexistent():
    """GET /api/selection-bias/gate/{strategy_id} with unknown ID returns 404."""
    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/selection-bias/gate/nonexistent-id-that-doesnt-exist-xyz")
    assert resp.status_code == 404
    assert "detail" in resp.json()


@pytest.mark.asyncio
async def test_pbo_endpoint_two_strategies_returns_200():
    """POST /api/selection-bias/pbo with valid two-strategy matrix returns 200."""
    from archimedes.main import app

    payload = {"returns_matrix": {"s1": [0.001] * 100, "s2": [-0.0005] * 100}}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/selection-bias/pbo", json=payload)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_pbo_endpoint_has_required_keys():
    """POST /api/selection-bias/pbo response has pbo_scores and interpretation."""
    from archimedes.main import app

    payload = {"returns_matrix": {"s1": [0.001] * 100, "s2": [-0.0005] * 100}}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/selection-bias/pbo", json=payload)
    data = resp.json()
    assert "pbo_scores" in data, f"missing 'pbo_scores' in {list(data.keys())}"
    assert "interpretation" in data, f"missing 'interpretation' in {list(data.keys())}"


@pytest.mark.asyncio
async def test_pbo_endpoint_scores_keyed_by_strategy_id():
    """pbo_scores dict contains entries for each submitted strategy."""
    from archimedes.main import app

    payload = {"returns_matrix": {"alpha": [0.001] * 100, "beta": [-0.0005] * 100}}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/selection-bias/pbo", json=payload)
    scores = resp.json()["pbo_scores"]
    assert "alpha" in scores
    assert "beta" in scores


@pytest.mark.asyncio
async def test_pbo_endpoint_scores_are_floats():
    """pbo_scores values are numeric and within [0, 1]."""
    from archimedes.main import app

    payload = {"returns_matrix": {"s1": [0.001] * 100, "s2": [-0.001] * 100}}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/selection-bias/pbo", json=payload)
    scores = resp.json()["pbo_scores"]
    for sid, score in scores.items():
        assert isinstance(score, (int, float)), f"score for {sid} is not numeric: {score}"
        assert 0.0 <= score <= 1.0, f"score for {sid} out of [0,1]: {score}"


@pytest.mark.asyncio
async def test_pbo_endpoint_interpretation_string():
    """interpretation field is a non-empty string."""
    from archimedes.main import app

    payload = {"returns_matrix": {"s1": [0.001] * 100, "s2": [-0.001] * 100}}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/selection-bias/pbo", json=payload)
    interpretation = resp.json()["interpretation"]
    assert isinstance(interpretation, str)
    assert len(interpretation) > 0


@pytest.mark.asyncio
async def test_pbo_endpoint_high_pbo_interpretation_says_failed():
    """PBO=1.0 is deterministic for s1-positive vs s2-negative constant returns.

    Constant series cause std=0 in every CSCV block, so _sharpe_per_col returns
    0.0 for both strategies via the safe_sigma=inf guard.  argsort([0,0]) is
    stable, assigning s1 (index 0) rank 1 (worst) out of 2.  omega=0.5/2=0.5,
    lambda=log(0.5/0.5)=0.0, which satisfies lam<=0, so every split votes for
    overfitting and PBO=1.0.
    """
    from archimedes.main import app

    payload = {"returns_matrix": {"s1": [0.001] * 100, "s2": [-0.001] * 100}}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/selection-bias/pbo", json=payload)
    data = resp.json()
    score = next(iter(data["pbo_scores"].values()))
    assert score == pytest.approx(1.0), f"expected PBO=1.0 for this payload, got {score}"
    assert "FAILED" in data["interpretation"]


@pytest.mark.asyncio
async def test_pbo_endpoint_low_pbo_interpretation_says_passed():
    """PBO=0.0 is deterministic when one strategy clearly dominates.

    With rng(42), 'good' (mean=+0.002, vol=0.003) dominates 'poor'
    (mean=-0.002, vol=0.01) on every IS/OOS split, so compute_pbo returns 0.0.
    """
    from archimedes.main import app

    rng = np.random.default_rng(42)
    good = rng.normal(0.002, 0.003, 300).tolist()
    poor = rng.normal(-0.002, 0.01, 300).tolist()
    payload = {"returns_matrix": {"good": good, "poor": poor}}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/selection-bias/pbo", json=payload)
    data = resp.json()
    score = next(iter(data["pbo_scores"].values()))
    assert score == pytest.approx(0.0), f"expected PBO=0.0 for dominant strategy, got {score}"
    assert "PASSED" in data["interpretation"]


@pytest.mark.asyncio
async def test_pbo_endpoint_single_strategy_returns_zero():
    """Single strategy -> compute_pbo returns 0.0 (no comparison possible)."""
    from archimedes.main import app

    payload = {"returns_matrix": {"s1": [0.001] * 100}}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/selection-bias/pbo", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert "pbo_scores" in data
    assert data["pbo_scores"].get("s1") == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_pbo_endpoint_short_series_edge_case():
    """Very short series (10 bars) with s_partitions=4 returns a valid response."""
    from archimedes.main import app

    payload = {
        "returns_matrix": {"s1": [0.001] * 10, "s2": [-0.001] * 10},
        "s_partitions": 4,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/selection-bias/pbo", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert "pbo_scores" in data
    assert "interpretation" in data


@pytest.mark.asyncio
async def test_pbo_endpoint_three_strategies():
    """Three strategies are all present in pbo_scores output."""
    from archimedes.main import app

    payload = {
        "returns_matrix": {
            "strategy_a": [0.001] * 80,
            "strategy_b": [-0.0005] * 80,
            "strategy_c": [0.0002] * 80,
        }
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/selection-bias/pbo", json=payload)
    assert resp.status_code == 200
    scores = resp.json()["pbo_scores"]
    assert set(scores.keys()) == {"strategy_a", "strategy_b", "strategy_c"}


@pytest.mark.asyncio
async def test_pbo_endpoint_custom_s_partitions():
    """s_partitions parameter is accepted and response remains valid."""
    from archimedes.main import app

    payload = {
        "returns_matrix": {"s1": [0.001] * 200, "s2": [-0.001] * 200},
        "s_partitions": 8,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/selection-bias/pbo", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert "pbo_scores" in data
    assert isinstance(data["interpretation"], str)


@pytest.mark.asyncio
async def test_gate_endpoint_empty_provider(monkeypatch):
    """When provider returns no strategies, gate returns empty-list response."""
    from archimedes.api import selection_bias_routes
    from archimedes.main import app

    # _provider is now a lazily-cached accessor (_provider()); patch the attribute
    # on the resolved (cached) instance rather than on the accessor function itself.
    monkeypatch.setattr(selection_bias_routes._provider(), "list_strategies", list)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/selection-bias/gate")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["passing"] == 0
    assert data["failing"] == 0
    assert data["strategies"] == []


@pytest.mark.asyncio
async def test_gate_endpoint_empty_provider_404_for_strategy(monkeypatch):
    """When provider returns no strategies, any strategy_id returns 404."""
    from archimedes.api import selection_bias_routes
    from archimedes.main import app

    # _provider is now a lazily-cached accessor (_provider()); patch the attribute
    # on the resolved (cached) instance rather than on the accessor function itself.
    monkeypatch.setattr(selection_bias_routes._provider(), "get_strategy", lambda sid: None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/selection-bias/gate/any-id-at-all")
    assert resp.status_code == 404


# ── OOS/IS cliff denominator (audit finding #7) ─────────────────────────


class TestOosCliffDenominator:
    """The /gate route must pass in_sample_sharpe=None so run_rigor_gate derives
    the IS denominator from the first 70% of the series — NOT the full-sample
    backtest Sharpe (the previous `bt_map[s.id].sharpe_ratio` override), which
    blends IS+OOS and makes the OOS/IS cliff trivially passable.

    These deterministic series (no RNG → version-independent) demonstrate the
    denominator choice flipping the verdict on a strong-IS / weak-positive-OOS
    strategy: the honest first-70% denominator fails the cliff; the inflated
    full-sample denominator passes it.
    """

    @staticmethod
    def _strong_is_weak_oos() -> list[float]:
        # IS (first 700 bars): drift 0.003 ± 0.01 → high Sharpe.
        # OOS (last 300 bars): drift 0.0015 ± 0.01 → ~half the IS edge.
        amp = 0.01
        is_part = [0.003 + (amp if i % 2 == 0 else -amp) for i in range(700)]
        oos_part = [0.0015 + (amp if i % 2 == 0 else -amp) for i in range(300)]
        return is_part + oos_part

    def test_first70_denominator_fails_cliff(self):
        from archimedes.services.rigor_evaluator import run_rigor_gate

        series = self._strong_is_weak_oos()
        gate = run_rigor_gate(
            "s",
            series,
            num_trials=1,
            pbo_scores=None,
            strategy_code=None,
            in_sample_sharpe=None,
            average_correlation=0.0,
        )
        assert "FAIL" in gate.gate_details["oos_sharpe"]

    def test_fullsample_denominator_would_inflate_and_pass(self):
        import math

        from archimedes.services.rigor_evaluator import run_rigor_gate

        series = self._strong_is_weak_oos()
        arr = np.asarray(series, dtype=float)
        full_sample_sharpe = (arr.mean() / arr.std(ddof=1)) * math.sqrt(252)
        gate = run_rigor_gate(
            "s",
            series,
            num_trials=1,
            pbo_scores=None,
            strategy_code=None,
            in_sample_sharpe=full_sample_sharpe,
            average_correlation=0.0,
        )
        # Same series, only the denominator differs → the bug let it pass.
        assert "PASS" in gate.gate_details["oos_sharpe"]


# ── Library PBO: schema + cached helper (#546, display-only) ────────────


class TestLibraryPboSchema:
    def test_defaults_are_unavailable_shape(self):
        lp = LibraryPbo()
        assert lp.value is None
        assert lp.data_vintage is None
        assert lp.selection_set_size == 0
        assert lp.source == "library_cscv"

    def test_custom_values_stored(self):
        lp = LibraryPbo(value=0.31, data_vintage="2026-06-11", selection_set_size=22, source="library_cscv")
        assert lp.value == pytest.approx(0.31)
        assert lp.data_vintage == "2026-06-11"
        assert lp.selection_set_size == 22

    def test_present_on_response_models_by_default(self):
        resp = RigorGateResponse(strategies=[], total=0, passing=0, failing=0)
        assert isinstance(resp.library_pbo, LibraryPbo)
        result = StrategyRigorResult(
            strategy_id="x",
            strategy_name="X",
            passes_all=False,
            gate_details=RigorGateDetail(),
        )
        assert isinstance(result.library_pbo, LibraryPbo)


class TestCachedLibraryPbo:
    """The module-level cached helper computes a value for a valid tmp store and
    fails closed gracefully for an absent one — without re-running CSCV per call.

    #774: the store moved from committed JSON files to the strategy_daily_returns
    table, so these use a tmp, isolated SQLite session (via a patched
    archimedes.db.get_session) instead of a tmp directory of JSON files."""

    def _session(self, tmp_path):
        from archimedes.db import Base
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        engine = create_engine(
            f"sqlite:///{tmp_path / 'cached_pbo_test.db'}", connect_args={"check_same_thread": False}
        )
        Base.metadata.create_all(bind=engine)
        return sessionmaker(bind=engine)()

    def _write_store(
        self, session, n_series: int = 4, n_obs: int = 256, vintage: str = "2026-06-11", stem_offset: int = 0
    ):
        from datetime import date as date_cls
        from datetime import timedelta

        import numpy as np
        from archimedes.models.daily_returns_store import StrategyDailyReturn

        rng = np.random.default_rng(7)
        base_date = date_cls(2020, 1, 1)
        for k in range(stem_offset, stem_offset + n_series):
            returns = rng.normal(0.0005, 0.01, n_obs)
            for i in range(n_obs):
                session.add(
                    StrategyDailyReturn(
                        stem=f"strat_{k}",
                        date=base_date + timedelta(days=i),
                        daily_return=float(returns[i]),
                        data_vintage=vintage,
                    )
                )
        session.commit()

    def _patch_get_session(self, monkeypatch, session):
        import contextlib

        import archimedes.db as db_module

        @contextlib.contextmanager
        def _fake_get_session():
            yield session  # no close() on exit — lifecycle is the test's to manage

        monkeypatch.setattr(db_module, "get_session", _fake_get_session)

    def test_returns_value_for_valid_store(self, tmp_path, monkeypatch):
        from archimedes.api import selection_bias_routes as routes

        session = self._session(tmp_path)
        self._write_store(session)
        self._patch_get_session(monkeypatch, session)
        # Clear any cached value from a prior test's signature.
        monkeypatch.setattr(routes, "_LIBRARY_PBO_CACHE", {})

        value, vintage, size, rf_convention = routes._cached_library_pbo()
        assert value is not None
        assert 0.0 <= value <= 1.0
        assert vintage == "2026-06-11"
        assert size == 4
        # #1409 round-4 review fix: `_cached_library_pbo` calls
        # `compute_library_pbo`/`compute_library_pbo_rf_convention` at their
        # not-yet-wired `use_tbill_series=False` default (matching every
        # `run_rigor_gate` call site), so the disclosed convention is the
        # flat fallback here even though the store's dates (2020-01-01
        # onward) are all inside the vendored DGS3MO series' coverage and
        # WOULD resolve to the series if this route ever opts in.
        from archimedes.services import rf_series

        assert rf_convention == rf_series.RF_CONVENTION_FALLBACK

    def test_absent_store_fails_closed(self, tmp_path, monkeypatch):
        from archimedes.api import selection_bias_routes as routes

        session = self._session(tmp_path)  # empty table, nothing inserted
        self._patch_get_session(monkeypatch, session)
        monkeypatch.setattr(routes, "_LIBRARY_PBO_CACHE", {})

        value, vintage, size, rf_convention = routes._cached_library_pbo()
        assert value is None
        assert vintage is None
        assert size == 0
        assert rf_convention == "MISSING"

    def test_payload_unavailable_when_value_none(self, monkeypatch):
        from archimedes.api import selection_bias_routes as routes

        monkeypatch.setattr(routes, "_cached_library_pbo", lambda: (None, None, 0, "MISSING"))
        payload = routes._library_pbo_payload()
        assert payload.value is None
        assert payload.source == "unavailable"
        assert payload.rf_convention == "MISSING"

    def test_cache_avoids_recompute_on_unchanged_store(self, tmp_path, monkeypatch):
        """Second call with an unchanged store does NOT re-run compute_library_pbo."""
        from archimedes.api import selection_bias_routes as routes

        session = self._session(tmp_path)
        self._write_store(session)
        self._patch_get_session(monkeypatch, session)
        monkeypatch.setattr(routes, "_LIBRARY_PBO_CACHE", {})

        calls = {"n": 0}
        real = routes.compute_library_pbo

        def _counting(*a, **k):
            calls["n"] += 1
            return real(*a, **k)

        monkeypatch.setattr(routes, "compute_library_pbo", _counting)

        routes._cached_library_pbo()
        routes._cached_library_pbo()
        assert calls["n"] == 1  # cached on the unchanged DB signature

    def test_cache_recomputes_after_store_changes(self, tmp_path, monkeypatch):
        """A row added after the first call changes the DB signature — the
        second call must recompute, not reuse the stale cached value."""
        from archimedes.api import selection_bias_routes as routes

        session = self._session(tmp_path)
        self._write_store(session, n_series=4)
        self._patch_get_session(monkeypatch, session)
        monkeypatch.setattr(routes, "_LIBRARY_PBO_CACHE", {})

        routes._cached_library_pbo()
        assert len(routes._LIBRARY_PBO_CACHE) == 1

        self._write_store(session, n_series=1, vintage="2026-07-03", stem_offset=4)  # a 5th, new stem
        routes._cached_library_pbo()
        assert len(routes._LIBRARY_PBO_CACHE) == 2  # new signature, not reused


# ── Library PBO: endpoint wiring + additivity guarantee (#546) ──────────


@pytest.mark.asyncio
async def test_gate_response_includes_library_pbo_object():
    """GET /api/selection-bias/gate carries a library_pbo object on the response
    AND on every per-strategy result (selection-set property)."""
    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/selection-bias/gate")
    data = resp.json()
    assert "library_pbo" in data
    lp = data["library_pbo"]
    for key in ("value", "data_vintage", "selection_set_size", "source"):
        assert key in lp, f"missing '{key}' in library_pbo: {list(lp.keys())}"
    for strat in data["strategies"]:
        assert "library_pbo" in strat


@pytest.mark.asyncio
async def test_single_strategy_result_includes_library_pbo():
    """GET /gate/{id} renders the selection-set library_pbo on the passport result."""
    from archimedes.api import selection_bias_routes
    from archimedes.main import app

    # Pick a real strategy id from the provider so the route resolves it.
    strategies = selection_bias_routes._provider().list_strategies()
    if not strategies:
        pytest.skip("no strategies in provider")
    sid = strategies[0].id

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/api/selection-bias/gate/{sid}")
    assert resp.status_code == 200
    assert "library_pbo" in resp.json()


@pytest.mark.asyncio
async def test_library_pbo_does_not_change_verdict_or_pbo_score(monkeypatch):
    """ADDITIVITY GUARANTEE: injecting vs removing the library PBO must NOT change
    any strategy's passes_all or pbo_score. We run the gate twice — once with the
    library PBO forced to a concrete value, once forced unavailable — and assert
    the per-strategy verdict + cohort pbo_score are byte-for-byte identical."""
    from archimedes.api import selection_bias_routes as routes
    from archimedes.main import app

    async def _run_once():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return (await client.get("/api/selection-bias/gate")).json()

    # Run A: library PBO present with a concrete value.
    monkeypatch.setattr(
        routes,
        "_library_pbo_payload",
        lambda: routes.LibraryPbo(value=0.42, data_vintage="2026-06-11", selection_set_size=22),
    )
    data_with = await _run_once()

    # Run B: library PBO unavailable (None).
    monkeypatch.setattr(
        routes,
        "_library_pbo_payload",
        lambda: routes.LibraryPbo(value=None, source="unavailable"),
    )
    data_without = await _run_once()

    by_id_with = {s["strategy_id"]: s for s in data_with["strategies"]}
    by_id_without = {s["strategy_id"]: s for s in data_without["strategies"]}
    assert by_id_with.keys() == by_id_without.keys()
    for sid, a in by_id_with.items():
        b = by_id_without[sid]
        assert a["passes_all"] == b["passes_all"], f"passes_all changed for {sid}"
        assert a["pbo_score"] == b["pbo_score"], f"pbo_score changed for {sid}"
    # Sanity: the only thing that differs is the additive library_pbo field.
    assert data_with["passing"] == data_without["passing"]
    assert data_with["library_pbo"]["value"] == pytest.approx(0.42)
    assert data_without["library_pbo"]["value"] is None


@pytest.mark.asyncio
async def test_library_pbo_stays_consistent_between_top_level_and_per_strategy_on_cache_hit(monkeypatch):
    """Regression test (Copilot review, PR #1040): ``get_or_compute`` may return a
    CACHED list of ``StrategyRigorResult`` objects whose ``library_pbo`` reflects
    an earlier request, while the top-level ``library_pbo`` on the response is
    always freshly computed via ``_library_pbo_payload()``. Without reconciling
    the two, a cache HIT could serve an internally inconsistent response
    (top-level != some per-strategy ``library_pbo``). Call the gate endpoint
    twice with the SAME underlying returns/code (so the rigor_cache
    ``cohort_key`` is identical -> the second call is guaranteed to be a cache
    hit) but a DIFFERENT ``_library_pbo_payload`` mock each time, and assert
    every per-strategy ``library_pbo`` always matches the top-level one — on
    both the cache-priming call and the cache-hit call."""
    from archimedes.api import selection_bias_routes as routes
    from archimedes.main import app

    async def _run_once():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return (await client.get("/api/selection-bias/gate")).json()

    def _assert_internally_consistent(data):
        top = data["library_pbo"]
        for strat in data["strategies"]:
            assert strat["library_pbo"] == top, (
                f"strategy {strat['strategy_id']}'s library_pbo {strat['library_pbo']} "
                f"disagrees with the top-level library_pbo {top}"
            )

    # Call A: primes the rigor_cache (this cohort has not been computed yet in
    # this test) with library_pbo forced to a concrete value.
    monkeypatch.setattr(
        routes,
        "_library_pbo_payload",
        lambda: routes.LibraryPbo(value=0.42, data_vintage="2026-06-11", selection_set_size=22),
    )
    data_a = await _run_once()
    _assert_internally_consistent(data_a)

    # Call B: SAME underlying returns/code (nothing else changed) -> the exact
    # same rigor_cache cohort_key -> `results` from get_or_compute is a CACHE
    # HIT carrying run-A's per-strategy library_pbo (0.42) unless the route
    # reconciles it against the freshly-computed top-level value. The
    # top-level library_pbo is recomputed as "unavailable" (None) this time —
    # the two must still agree.
    monkeypatch.setattr(
        routes,
        "_library_pbo_payload",
        lambda: routes.LibraryPbo(value=None, source="unavailable"),
    )
    data_b = await _run_once()
    _assert_internally_consistent(data_b)
    assert data_b["library_pbo"]["value"] is None
    assert data_b["library_pbo"]["source"] == "unavailable"


@pytest.mark.asyncio
async def test_run_rigor_gate_called_without_library_pbo_kwarg(monkeypatch):
    """The gate verdict path must be provably unchanged: run_rigor_gate is called
    WITHOUT a library_pbo= argument (passing it would alter criterion 4 = option 3,
    which is out of scope). Patch run_rigor_gate, force the DB to yield a usable
    return series so the full branch executes, and assert library_pbo is absent
    from every call's kwargs."""
    from archimedes.api import selection_bias_routes as routes
    from archimedes.main import app
    from archimedes.services.rigor_evaluator import run_rigor_gate as real_run_rigor_gate

    strategies = routes._provider().list_strategies()
    if not strategies:
        pytest.skip("no strategies in provider")

    # Force the DB read to return a usable series for at least one strategy so the
    # full (non-MISSING) branch — the one that calls run_rigor_gate — executes.
    import numpy as np

    rng = np.random.default_rng(3)
    series = rng.normal(0.001, 0.01, 400).tolist()
    target_id = strategies[0].id
    # The route imports get_all_daily_returns inside the function body
    # (`from archimedes.services.backtest_repository import get_all_daily_returns`),
    # so the effective patch point is the definition module, not the route module.
    monkeypatch.setattr(
        "archimedes.services.backtest_repository.get_all_daily_returns",
        lambda session, ids: {target_id: series},
    )

    captured_kwargs = []

    def _spy(*args, **kwargs):
        captured_kwargs.append(kwargs)
        return real_run_rigor_gate(*args, **kwargs)

    monkeypatch.setattr(routes, "run_rigor_gate", _spy)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/selection-bias/gate")
    assert resp.status_code == 200
    assert captured_kwargs, "run_rigor_gate was never called — the full branch did not execute"
    for kwargs in captured_kwargs:
        assert "library_pbo" not in kwargs, "run_rigor_gate must NOT receive library_pbo (option-3 guard)"


# ── Degenerate (zero-variance) series excluded from num_trials/avg_corr (#868) ──


class TestDegenerateSeriesExcludedFromCohort:
    """#868 fix 1: a zero-variance daily_returns series (a flat placeholder row
    that was never replaced with a real backtest — 11/31 in the live library at
    filing time) must not count toward num_trials or feed avg_correlation/PBO,
    since that unfairly stiffens the DSR multiple-testing correction applied to
    every REAL strategy in the cohort. Mirrors the exact degeneracy test
    _rigor_helpers._sharpe_dsr_inputs already uses (np.ptp(arr) == 0) rather than
    inventing a new heuristic (per the issue's explicit instruction).

    These tests inject a KNOWN mixed cohort at the get_all_daily_returns DB
    boundary — some real (non-degenerate) series, some flat (degenerate) ones —
    and spy on run_rigor_gate's num_trials/average_correlation kwargs to assert
    only the real series were counted.
    """

    @staticmethod
    def _real_series(seed: int, n: int = 300) -> list[float]:
        return np.random.default_rng(seed).normal(0.001, 0.01, n).tolist()

    @staticmethod
    def _flat_series(value: float = 0.0, n: int = 300) -> list[float]:
        # A zero-variance placeholder row: every observation identical.
        # len >= 10 so it clears the pre-existing length filter and would have
        # counted toward num_trials before this fix.
        return [value] * n

    def _patch_returns(self, monkeypatch, returns_by_strategy: dict[str, list[float]]):
        monkeypatch.setattr(
            "archimedes.services.backtest_repository.get_all_daily_returns",
            lambda session, ids: dict(returns_by_strategy),
        )

    @pytest.mark.asyncio
    async def test_degenerate_series_excluded_from_avg_correlation(self, monkeypatch):
        """3 real + 2 degenerate (flat) series in the cohort. Post-decouple #2,
        num_trials is a self-contained 1 (each curated strategy graded on its own
        Sharpe, never deflated by cohort size), so the degeneracy filter no longer
        affects num_trials — but it MUST still keep the 2 flat placeholders out of
        the cohort avg_correlation/PBO context (#868), so avg_correlation stays a
        finite number computed from only the 3 real series."""
        from archimedes.api import selection_bias_routes as routes
        from archimedes.main import app
        from archimedes.services.rigor_evaluator import run_rigor_gate as real_run_rigor_gate

        strategies = routes._provider().list_strategies()
        assert len(strategies) >= 5, "need >=5 curated strategies to build this cohort"
        ids = [s.id for s in strategies[:5]]

        returns = {
            ids[0]: self._real_series(0),
            ids[1]: self._real_series(1),
            ids[2]: self._real_series(2),
            ids[3]: self._flat_series(0.0),  # degenerate: constant zero
            ids[4]: self._flat_series(0.0007),  # degenerate: constant nonzero
        }
        self._patch_returns(monkeypatch, returns)

        captured_kwargs = []

        def _spy(*args, **kwargs):
            captured_kwargs.append(kwargs)
            return real_run_rigor_gate(*args, **kwargs)

        monkeypatch.setattr(routes, "run_rigor_gate", _spy)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/selection-bias/gate")
        assert resp.status_code == 200
        assert captured_kwargs, "run_rigor_gate was never called"

        # Decouple #2: num_trials is a self-contained 1 for every curated strategy,
        # independent of cohort size (degenerate or not).
        num_trials_seen = {kwargs["num_trials"] for kwargs in captured_kwargs}
        assert num_trials_seen == {1}, f"curated num_trials must be self-contained 1, got {num_trials_seen}"

        # The #868 protection that REMAINS meaningful: the 2 flat placeholders must
        # not corrupt the cohort avg_correlation (computed from the 3 real series).
        avg_corr_seen = {kwargs["average_correlation"] for kwargs in captured_kwargs}
        assert all(np.isfinite(c) for c in avg_corr_seen), (
            f"avg_correlation must be finite (degenerate series excluded), got {avg_corr_seen}"
        )

    @pytest.mark.asyncio
    async def test_avg_correlation_excludes_degenerate_series(self, monkeypatch):
        """The average_correlation fed to run_rigor_gate is computed only over
        the non-degenerate cohort — a degenerate (zero-variance) series has an
        undefined correlation with anything and must not dilute the average."""
        from archimedes.api import selection_bias_routes as routes
        from archimedes.main import app
        from archimedes.services.rigor_evaluator import (
            compute_average_pairwise_correlation,
        )
        from archimedes.services.rigor_evaluator import (
            run_rigor_gate as real_run_rigor_gate,
        )

        strategies = routes._provider().list_strategies()
        assert len(strategies) >= 4
        ids = [s.id for s in strategies[:4]]

        real_a, real_b, real_c = self._real_series(10), self._real_series(11), self._real_series(12)
        returns = {
            ids[0]: real_a,
            ids[1]: real_b,
            ids[2]: real_c,
            ids[3]: self._flat_series(0.0),  # degenerate
        }
        self._patch_returns(monkeypatch, returns)

        expected_avg_corr = compute_average_pairwise_correlation({"a": real_a, "b": real_b, "c": real_c})

        captured_kwargs = []

        def _spy(*args, **kwargs):
            captured_kwargs.append(kwargs)
            return real_run_rigor_gate(*args, **kwargs)

        monkeypatch.setattr(routes, "run_rigor_gate", _spy)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/selection-bias/gate")
        assert resp.status_code == 200
        assert captured_kwargs

        for kwargs in captured_kwargs:
            assert kwargs["average_correlation"] == pytest.approx(expected_avg_corr), (
                "average_correlation must be computed over the non-degenerate cohort only "
                f"(expected {expected_avg_corr}, got {kwargs['average_correlation']})"
            )

    @pytest.mark.asyncio
    async def test_all_degenerate_cohort_yields_num_trials_floor_of_one(self, monkeypatch):
        """If EVERY persisted series in the cohort is degenerate, valid_returns is
        empty and num_trials floors to 1 (max(len(valid_returns), 1)) — matching
        the pre-existing floor semantics for an empty/near-empty cohort."""
        from archimedes.api import selection_bias_routes as routes
        from archimedes.main import app
        from archimedes.services.rigor_evaluator import run_rigor_gate as real_run_rigor_gate

        strategies = routes._provider().list_strategies()
        assert len(strategies) >= 2
        ids = [s.id for s in strategies[:2]]

        returns = {
            ids[0]: self._flat_series(0.0),
            ids[1]: self._flat_series(0.001),
        }
        self._patch_returns(monkeypatch, returns)

        captured_kwargs = []

        def _spy(*args, **kwargs):
            captured_kwargs.append(kwargs)
            return real_run_rigor_gate(*args, **kwargs)

        monkeypatch.setattr(routes, "run_rigor_gate", _spy)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/selection-bias/gate")
        assert resp.status_code == 200
        # Both strategies still have len(daily_returns) >= 10 individually, so
        # BOTH still go through the per-strategy run_rigor_gate call (each reports
        # its own honest MISSING/FAIL from _rigor_helpers's own degeneracy guard)
        # — only the cohort-level num_trials context floors to 1.
        assert captured_kwargs, "degenerate-but-len>=10 strategies must still run their own gate"
        num_trials_seen = {kwargs["num_trials"] for kwargs in captured_kwargs}
        assert num_trials_seen == {1}

    @pytest.mark.asyncio
    async def test_short_series_still_excluded_alongside_degenerate(self, monkeypatch):
        """Pre-existing len>=10 filter and the new variance filter compose
        correctly: a too-short series and a flat series are both excluded,
        leaving only the real series in num_trials."""
        from archimedes.api import selection_bias_routes as routes
        from archimedes.main import app
        from archimedes.services.rigor_evaluator import run_rigor_gate as real_run_rigor_gate

        strategies = routes._provider().list_strategies()
        assert len(strategies) >= 3
        ids = [s.id for s in strategies[:3]]

        returns = {
            ids[0]: self._real_series(20),
            ids[1]: [0.001] * 5,  # too short (< 10) — pre-existing filter
            ids[2]: self._flat_series(0.002),  # long enough but degenerate
        }
        self._patch_returns(monkeypatch, returns)

        captured_kwargs = []

        def _spy(*args, **kwargs):
            captured_kwargs.append(kwargs)
            return real_run_rigor_gate(*args, **kwargs)

        monkeypatch.setattr(routes, "run_rigor_gate", _spy)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/selection-bias/gate")
        assert resp.status_code == 200
        num_trials_seen = {kwargs["num_trials"] for kwargs in captured_kwargs}
        assert num_trials_seen == {1}, "only 1 real series survives both filters"


class TestCacheKeyReactsToStrategyCodeChange:
    """Copilot review, PR #1040: the /api/selection-bias/gate cache key
    (rigor_cache.cohort_key + strictness) previously fingerprinted only
    persisted returns, but run_rigor_gate's look-ahead audit also depends on
    strategy_code (loaded from s.strategy_code_path). Editing a strategy's code
    without touching its returns therefore served a STALE cached look-ahead
    verdict/passes_all for up to the TTL. Proves the fix: run_rigor_gate reruns
    when a strategy's strategy_code_hash changes, even with byte-identical
    persisted returns.
    """

    @staticmethod
    def _real_series(seed: int, n: int = 300) -> list[float]:
        return np.random.default_rng(seed).normal(0.001, 0.01, n).tolist()

    @pytest.mark.asyncio
    async def test_code_hash_change_busts_cache_with_unchanged_returns(self, monkeypatch):
        from archimedes.api import selection_bias_routes as routes
        from archimedes.main import app
        from archimedes.services.rigor_evaluator import run_rigor_gate as real_run_rigor_gate

        strategies = routes._provider().list_strategies()
        assert len(strategies) >= 1, "need >=1 curated strategy for this cohort"
        target = strategies[0]
        original_hash = target.strategy_code_hash

        returns = {s.id: self._real_series(abs(hash(s.id)) % 10_000) for s in strategies}
        monkeypatch.setattr(
            "archimedes.services.backtest_repository.get_all_daily_returns",
            lambda session, ids: dict(returns),
        )

        call_count = {"n": 0}

        def _spy(*args, **kwargs):
            call_count["n"] += 1
            return real_run_rigor_gate(*args, **kwargs)

        monkeypatch.setattr(routes, "run_rigor_gate", _spy)

        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp1 = await client.get("/api/selection-bias/gate")
            assert resp1.status_code == 200
            first_calls = call_count["n"]
            assert first_calls > 0, "first call must run the live gate"

            # Second call: everything unchanged (same returns, same code) — pure
            # cache hit, no new run_rigor_gate invocations.
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp2 = await client.get("/api/selection-bias/gate")
            assert resp2.status_code == 200
            assert call_count["n"] == first_calls, "unchanged returns+code must be a cache hit"

            # Simulate a code edit on `target`: persisted returns are UNCHANGED,
            # only its code hash differs (mirrors what LocalStrategyProvider.refresh()
            # would compute after the underlying file's contents actually changed).
            target.strategy_code_hash = f"{original_hash}-edited"

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp3 = await client.get("/api/selection-bias/gate")
            assert resp3.status_code == 200
            assert call_count["n"] > first_calls, (
                "a strategy's strategy_code_hash changing (with byte-identical "
                "returns) must bust the /api/selection-bias/gate cache key and "
                "rerun run_rigor_gate — otherwise a stale look-ahead verdict can "
                "be served for up to the TTL after a real code edit"
            )
        finally:
            # `_provider()` is a process-lifetime lru_cache singleton (shared
            # across the whole test session) — restore the mutated attribute so
            # this test can't leak state into any other test.
            target.strategy_code_hash = original_hash


# ── #1564: board-level FDR must stay OFF the per-strategy gate ───────────────
#
# Owner decision (Dan, 2026-08-31): the strategy passport carries only
# information about the strategy itself; the leaderboard is the one
# cross-strategy surface. `board_fdr_*` / `board_level_fdr` moved to
# `GET /api/leaderboard` (see backend/tests/test_leaderboard_board_fdr.py,
# which owns the correction's own correctness). What is guarded HERE is the
# absence — an absence is exactly the kind of property that decays silently,
# because re-adding a convenient field to a per-strategy response is a
# one-line, locally-sensible edit.
#
# The scan is deliberately PATTERN-based (`board_fdr`, `board_level_fdr`), not
# a hard-coded list of the three field names that were removed, so a
# reappearance under a near-miss name (`board_fdr_q`, `board_level_fdr_block`)
# is caught too.

_BOARD_FDR_PATTERN = re.compile(r"board_fdr|board_level_fdr")


def _relational_field_names(model: type[BaseModel]) -> list[str]:
    """Field names on a pydantic model that name the board-level correction."""
    return sorted(name for name in model.model_fields if _BOARD_FDR_PATTERN.search(name))


def _relational_json_keys(payload: object, path: str = "$") -> list[str]:
    """Every key path in a decoded JSON payload naming the board-level correction.

    Recursive on purpose: the fields lived BOTH at the top level of
    `RigorGateResponse` and nested one level down inside each
    `StrategyRigorResult`, so a shallow `key in payload` check would have
    missed the per-strategy half entirely.
    """
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if _BOARD_FDR_PATTERN.search(str(key)):
                found.append(f"{path}.{key}")
            found.extend(_relational_json_keys(value, f"{path}.{key}"))
    elif isinstance(payload, list):
        for idx, value in enumerate(payload):
            found.extend(_relational_json_keys(value, f"{path}[{idx}]"))
    return found


class TestBoardFdrStaysOffThePerStrategyGate:
    """#1564 acceptance: `GET /api/selection-bias/gate*` responses contain no
    `board_fdr` or `board_level_fdr` key — on the response MODELS and on a
    LIVE response shape."""

    # ── the guards are not vacuous ──────────────────────────────────────────

    def test_field_scan_flags_a_deliberate_reappearance(self):
        """Adversarial pass: build the input that SHOULD fail and show it does.

        A field scan that has stopped matching would pass silently on a model
        with the field right back on it, which is the whole failure mode. So
        run the scan against a model deliberately carrying the reappearance —
        under the exact removed name AND under a near-miss name a future edit
        might reach for."""

        class _ReappearedRigorResult(BaseModel):
            strategy_id: str = ""
            board_fdr_significant: bool | None = None
            board_level_fdr_block: dict | None = None

        flagged = _relational_field_names(_ReappearedRigorResult)
        assert flagged == ["board_fdr_significant", "board_level_fdr_block"], (
            "the field scan no longer detects a board-FDR field on a model — it is guarding nothing"
        )

    def test_json_scan_flags_a_deliberate_reappearance(self):
        """Same adversarial pass for the live-shape scan, including the NESTED
        per-strategy position (a shallow scan would miss it)."""
        bad = {
            "strategies": [{"strategy_id": "a", "board_fdr_significant": False}],
            "board_level_fdr": {"n_tested": 3},
        }
        flagged = _relational_json_keys(bad)
        assert "$.strategies[0].board_fdr_significant" in flagged, (
            "the JSON scan no longer reaches the nested per-strategy position — it is guarding nothing"
        )
        assert "$.board_level_fdr" in flagged

    # ── the guards, applied ─────────────────────────────────────────────────

    def test_no_board_fdr_field_on_the_per_strategy_result_model(self):
        assert _relational_field_names(StrategyRigorResult) == [], (
            "board-level FDR is a CROSS-STRATEGY metric and must not ride the per-strategy gate "
            "(#1564, owner decision 2026-08-31). It belongs on GET /api/leaderboard — see "
            "api/leaderboard_schemas.BoardLevelFdr."
        )

    def test_no_board_fdr_field_on_the_gate_response_model(self):
        assert _relational_field_names(RigorGateResponse) == [], (
            "the board_level_fdr top-level key moved to GET /api/leaderboard (#1564)"
        )

    @pytest.mark.asyncio
    async def test_live_gate_response_shape_carries_no_board_fdr_key(self):
        """The batch route, over the real corpus and the real gate."""
        from archimedes.main import app

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/selection-bias/gate")
        assert resp.status_code == 200
        body = resp.json()
        assert body["strategies"], "the corpus must be non-empty for this shape check to bite"
        assert _relational_json_keys(body) == []

    @pytest.mark.asyncio
    async def test_live_single_strategy_gate_response_shape_carries_no_board_fdr_key(self):
        """The per-strategy route — the passport's own endpoint, and the one
        the owner decision is actually about."""
        from archimedes.api import selection_bias_routes as routes
        from archimedes.main import app

        strategies = routes._provider().list_strategies()
        assert strategies, "the corpus must be non-empty for this shape check to bite"
        sid = strategies[0].id

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(f"/api/selection-bias/gate/{sid}")
        assert resp.status_code == 200
        assert _relational_json_keys(resp.json()) == []


class TestLibraryPboStaysOnThePassport:
    """#1564 item 3, the explicit call, made executable.

    DECISION: `library_pbo` STAYS on `StrategyRigorResult` as display-only
    context; it does NOT follow `board_fdr_*` to the leaderboard. It was
    owner-flagged as the same impurity class and it is not one, for two
    reasons that are checkable rather than rhetorical — this class checks
    both. (The third reason — its `source`/`data_vintage`/`rf_convention`
    fields are provenance for the PBO figure and are meaningless apart from
    it — is a structural fact of the model, not a runtime property.)
    """

    def test_library_pbo_is_still_on_the_per_strategy_result(self):
        """Not removed silently — the issue's explicit instruction."""
        assert "library_pbo" in StrategyRigorResult.model_fields

    def test_library_pbo_is_a_constant_not_a_verdict(self):
        """Reason 1: PBO is byte-identical for every strategy in a selection
        set, so it cannot make one strategy's passport say a different thing
        about that strategy because of another strategy — which is the whole
        property the owner decision turns on (`board_fdr_significant` DOES
        differ per strategy, and flips as the cohort changes).

        Reason 2 rides along: `pbo_score`, ALREADY on the per-strategy result
        on the curated path, is itself this same library-wide value. Removing
        `library_pbo` would delete the scope label while leaving the labelled
        number — strictly less honest, and it would purify nothing.
        """
        from archimedes.services.rigor_evaluator import compute_pbo

        rng = np.random.default_rng(1564)
        returns = {f"s{i}": rng.normal(0.0005, 0.01, 400).tolist() for i in range(6)}
        scores = compute_pbo(returns)

        assert len(scores) == len(returns)
        distinct = set(scores.values())
        assert len(distinct) == 1, (
            "compute_pbo is documented (and asserted in analytics-engine/scripts/compute_library_pbo.py) "
            "to assign ONE library-wide value to every strategy; if that ever stops being true, the "
            "#1564 decision to keep library_pbo on the passport has to be revisited"
        )
