"""Hermetic tests for the leaderboard scoring engine (services/leaderboard.py).

No DB, no network — build ``StrategyResponse`` objects directly and assert the
ranking, the transparent score, None-handling, filters, and the honest
forward-axis/pending behaviour. The leaderboard's #1 rule is "never fabricate a
number", so the tests pin: placeholders sink (score 0), missing sort values land
last, and the scoring-engine metadata is always intact.
"""

from __future__ import annotations

import numpy as np
from archimedes.api.schemas import StrategyResponse
from archimedes.services.leaderboard import (
    OOS_TARGET,
    WEIGHTS,
    build_leaderboard,
    compute_conviction,
)


def _strat(
    sid: str,
    *,
    passes_gate: bool = False,
    dsr_p: float | None = None,
    oos: float | None = None,
    pbo: float | None = None,
    sharpe: float | None = None,
    placeholder: bool = False,
    regime: str = "regime_neutral",
    status: str = "validated",
    title: str = "",
    curator: str | None = None,
) -> StrategyResponse:
    return StrategyResponse(
        id=sid,
        methodology_summary=f"methodology for {sid}",
        asset_universe=["SPY", "GLD"],
        position_sizing="equal_weight",
        rebalance_frequency="monthly",
        status=status,
        paper_title=title,
        passes_rigor_gate=passes_gate,
        dsr_p_value=dsr_p,
        out_of_sample_sharpe=oos,
        pbo_score=pbo,
        sharpe_ratio=sharpe,
        is_backtest_placeholder=placeholder,
        regime_tag=regime,
        curator_wallet=curator,
    )


# ── compute_conviction ────────────────────────────────────────────────────


def test_weights_sum_to_one():
    assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9


def test_fully_validated_strategy_scores_high():
    s = _strat("A", passes_gate=True, dsr_p=0.99, oos=1.2, pbo=0.1)
    score, comp = compute_conviction(s)
    # 100*(0.35*1 + 0.25*0.99 + 0.25*1.0(clamped) + 0.15*0.9) = 98.25
    assert score == 98.2 or score == 98.3 or abs(score - 98.25) < 0.2
    assert comp.gate == 1.0
    assert comp.oos_performance == 1.0  # 1.2/1.0 clamps to 1.0
    assert comp.overfitting_resistance == 0.9
    assert comp.data_completeness == 1.0


def test_placeholder_scores_zero_and_incomplete():
    s = _strat("P", placeholder=True)  # all rigor None, gate not real
    score, comp = compute_conviction(s)
    assert score == 0.0
    assert comp.data_completeness == 0.0
    assert comp.gate == 0.0


def test_gate_not_counted_when_placeholder():
    # passes_rigor_gate True but placeholder → gate must NOT credit (no real backtest).
    s = _strat("X", passes_gate=True, placeholder=True)
    score, comp = compute_conviction(s)
    assert comp.gate == 0.0
    assert score == 0.0


def test_placeholder_with_borrowed_rigor_still_sinks():
    # Regression: a placeholder that ALSO carries DSR/OOS/PBO values (e.g. seeded
    # numbers on a placeholder record) must NOT ride them to a non-zero score and
    # outrank a real-but-partially-missing strategy. Every component zeros.
    ph = _strat("ph", passes_gate=True, dsr_p=0.99, oos=1.5, pbo=0.05, placeholder=True)
    score, comp = compute_conviction(ph)
    assert score == 0.0
    assert comp.gate == 0.0
    assert comp.dsr_confidence == 0.0
    assert comp.oos_performance == 0.0
    assert comp.overfitting_resistance == 0.0
    assert comp.data_completeness == 0.0

    # ... and it sorts strictly below a real strategy with only partial data.
    real_partial = _strat("real", passes_gate=True, dsr_p=0.6)  # gate + dsr only
    real_score, _ = compute_conviction(real_partial)
    assert real_score > score


def test_negative_oos_clamps_to_zero():
    s = _strat("N", passes_gate=True, oos=-0.5)
    _, comp = compute_conviction(s)
    assert comp.oos_performance == 0.0


def test_oos_target_constant_is_one():
    assert OOS_TARGET == 1.0


# ── build_leaderboard: ranking + medals ───────────────────────────────────


def test_ranks_by_conviction_desc_with_medals():
    weak = _strat("weak", passes_gate=False, dsr_p=0.2, oos=0.1, pbo=0.8)
    strong = _strat("strong", passes_gate=True, dsr_p=0.99, oos=1.5, pbo=0.05)
    mid = _strat("mid", passes_gate=True, dsr_p=0.7, oos=0.6, pbo=0.4)
    board = build_leaderboard([weak, strong, mid])
    ids = [e.id for e in board.entries]
    assert ids == ["strong", "mid", "weak"]
    assert board.entries[0].rank == 1 and board.entries[0].medal == "gold"
    assert board.entries[1].medal == "silver"
    assert board.entries[2].medal == "bronze"


def test_placeholders_sink_to_bottom():
    real = _strat("real", passes_gate=True, dsr_p=0.9, oos=1.0, pbo=0.1)
    ph = _strat("ph", placeholder=True)
    board = build_leaderboard([ph, real])
    assert board.entries[0].id == "real"
    assert board.entries[-1].id == "ph"


def test_missing_sort_value_pushed_to_bottom_regardless_of_order():
    has = _strat("has", sharpe=1.5)
    none1 = _strat("none1", sharpe=None)
    # desc
    board = build_leaderboard([none1, has], sort_by="sharpe_ratio", order="desc")
    assert board.entries[0].id == "has"
    assert board.entries[-1].id == "none1"
    # asc — None still last, not first
    board2 = build_leaderboard([none1, has], sort_by="sharpe_ratio", order="asc")
    assert board2.entries[-1].id == "none1"


def test_sort_by_pbo_respects_order():
    low = _strat("low", passes_gate=True, pbo=0.1, dsr_p=0.9, oos=1.0)
    high = _strat("high", passes_gate=True, pbo=0.6, dsr_p=0.9, oos=1.0)
    board = build_leaderboard([high, low], sort_by="pbo_score", order="asc")
    assert [e.id for e in board.entries] == ["low", "high"]


# ── filters ───────────────────────────────────────────────────────────────


def test_min_rigor_filters_out_failing_and_placeholders():
    passing = _strat("pass", passes_gate=True, dsr_p=0.9, oos=1.0, pbo=0.1)
    failing = _strat("fail", passes_gate=False, dsr_p=0.3)
    ph = _strat("ph", passes_gate=True, placeholder=True)
    board = build_leaderboard([passing, failing, ph], min_rigor=True)
    assert [e.id for e in board.entries] == ["pass"]
    assert board.total == 1


def test_regime_filter():
    bull = _strat("bull", regime="bull", passes_gate=True, dsr_p=0.9, oos=1.0, pbo=0.1)
    bear = _strat("bear", regime="bear", passes_gate=True, dsr_p=0.9, oos=1.0, pbo=0.1)
    board = build_leaderboard([bull, bear], regime_tag="bull")
    assert [e.id for e in board.entries] == ["bull"]


def test_limit_caps_entries_but_total_is_full_count():
    strats = [_strat(f"s{i}", passes_gate=True, dsr_p=0.5 + i * 0.01, oos=0.5, pbo=0.3) for i in range(10)]
    board = build_leaderboard(strats, limit=3)
    assert len(board.entries) == 3
    assert board.total == 10
    # ranks are global (1,2,3), not re-based after the cap
    assert [e.rank for e in board.entries] == [1, 2, 3]


# ── honesty / metadata ────────────────────────────────────────────────────


def test_forward_axis_is_honest_pending():
    board = build_leaderboard([_strat("a", passes_gate=True, dsr_p=0.9, oos=1.0, pbo=0.1)])
    fwd = board.entries[0].forward
    assert fwd.stockbench_status == "pending"
    assert fwd.stockbench_sortino is None
    assert fwd.live_pnl_status == "pending"
    assert fwd.live_pnl_pct is None


def test_scoring_engine_metadata_intact_even_when_empty():
    board = build_leaderboard([])
    assert board.entries == []
    assert board.total == 0
    eng = board.scoring_engine
    assert abs(sum(eng.weights.values()) - 1.0) < 1e-9
    assert eng.validation_axis == "live"
    assert eng.forward_axis == "pending"
    assert eng.stockbench_global.rank == "15/15"
    assert "fabricat" in eng.disclaimer.lower()  # the no-fabrication promise is loud


def test_creator_defaults_to_archimedes_for_curated():
    board = build_leaderboard([_strat("c", curator=None)])
    assert board.entries[0].creator == "Archimedes"
    board2 = build_leaderboard([_strat("c2", curator="0xabc")])
    assert board2.entries[0].creator == "0xabc"


def test_invalid_sort_by_falls_back_to_conviction():
    a = _strat("a", passes_gate=True, dsr_p=0.99, oos=1.5, pbo=0.05)
    b = _strat("b", passes_gate=False, dsr_p=0.1, oos=0.1, pbo=0.9)
    board = build_leaderboard([b, a], sort_by="nonsense_field")
    assert board.sort_by == "conviction_score"
    assert board.entries[0].id == "a"


def test_name_falls_back_to_methodology_when_no_paper_title():
    board = build_leaderboard([_strat("nm", title="")])
    assert board.entries[0].name.startswith("methodology for nm")


# ── HTTP route (full app boot → route → service) ──────────────────────────
# Mirrors test_risk_routes: import the app and exercise the real wiring. The
# route is fail-safe, so it returns 200 with intact scoring-engine metadata even
# if the strategy provider has no data.


async def test_leaderboard_endpoint_returns_200_with_scoring_engine():
    from archimedes.main import app
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/leaderboard")
    assert resp.status_code == 200
    data = resp.json()
    assert "entries" in data and isinstance(data["entries"], list)
    assert "scoring_engine" in data
    eng = data["scoring_engine"]
    assert abs(sum(eng["weights"].values()) - 1.0) < 1e-9
    assert eng["stockbench_global"]["rank"] == "15/15"
    assert "fabricat" in eng["disclaimer"].lower()
    # Every entry (if any) must carry the honest forward axis.
    for e in data["entries"]:
        assert e["forward"]["stockbench_status"] == "pending"
        assert e["forward"]["live_pnl_status"] == "pending"


async def test_leaderboard_endpoint_respects_query_params():
    from archimedes.main import app
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/leaderboard?sort_by=sharpe_ratio&order=asc&min_rigor=true&limit=5")
    assert resp.status_code == 200
    data = resp.json()
    assert data["sort_by"] == "sharpe_ratio"
    assert data["order"] == "asc"
    assert len(data["entries"]) <= 5
    # min_rigor → every returned entry passes the gate and is not a placeholder.
    for e in data["entries"]:
        assert e["passes_rigor_gate"] is True
        assert e["is_backtest_placeholder"] is False


async def test_leaderboard_rejects_bad_sort_param():
    from archimedes.main import app
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/leaderboard?sort_by=DROP_TABLE")
    # Query pattern guard → 422, never a 500.
    assert resp.status_code == 422


async def test_leaderboard_reports_degraded_when_provider_raises():
    """#1356: a curated-cohort failure must be visible on the wire, not
    silently rendered as an empty board (which the frontend previously
    could not tell apart from "the corpus genuinely has zero strategies")."""
    from unittest.mock import MagicMock, patch

    from archimedes.main import app
    from httpx import ASGITransport, AsyncClient

    with patch(
        "archimedes.api.leaderboard_routes.strategy_provider",
        MagicMock(side_effect=RuntimeError("provider down")),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/leaderboard?scope=curated")

    assert resp.status_code == 200
    body = resp.json()
    assert body["degraded"] is True
    assert body["degraded_reason"]
    # The raw exception string must never reach an anonymous, public caller
    # (it can carry DB/RPC internals — CLAUDE.md / docs/api/*.md convention).
    # `degraded_reason` is a fixed category string, not an interpolation of
    # `exc`; assert against the literal so a reintroduced f-string fails
    # this test even if the mock message happens not to.
    assert "provider down" not in body["degraded_reason"]
    assert body["degraded_reason"] == "strategy provider unavailable"


async def test_leaderboard_reports_degraded_when_corpus_missing_from_build():
    """#1356: an empty (non-raising) curated cohort must still say WHY —
    count_strategy_files()==0 is the #1039 corpus-missing-from-build signal
    /health already reports; the board must say the same thing rather than
    render a confident "0 strategies" indistinguishable from a genuinely
    empty corpus."""
    from unittest.mock import MagicMock, patch

    from archimedes.main import app
    from httpx import ASGITransport, AsyncClient

    empty_provider = MagicMock()
    empty_provider.list_strategies.return_value = []

    with (
        patch("archimedes.api.leaderboard_routes.strategy_provider", return_value=empty_provider),
        patch("archimedes.api.strategies_routes._public_generated_strategy_responses", return_value=[]),
        patch("archimedes.services.strategy_provider.count_strategy_files", return_value=0),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/leaderboard?scope=curated")

    assert resp.status_code == 200
    body = resp.json()
    assert body["degraded"] is True
    assert body["degraded_reason"] == "strategy corpus not found in build"


async def test_leaderboard_reports_degraded_when_curated_cohort_empty_but_corpus_present():
    """Same empty-cohort path, but count_strategy_files() > 0 — the corpus IS
    present in the build, yet the cohort still came back empty (e.g. every
    file failed to parse, or filtered to nothing). Must be distinguished from
    the #1039 corpus-missing reason so an operator isn't sent chasing the
    wrong root cause."""
    from unittest.mock import MagicMock, patch

    from archimedes.main import app
    from httpx import ASGITransport, AsyncClient

    empty_provider = MagicMock()
    empty_provider.list_strategies.return_value = []

    with (
        patch("archimedes.api.leaderboard_routes.strategy_provider", return_value=empty_provider),
        patch("archimedes.api.strategies_routes._public_generated_strategy_responses", return_value=[]),
        patch("archimedes.services.strategy_provider.count_strategy_files", return_value=5),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/leaderboard?scope=curated")

    assert resp.status_code == 200
    body = resp.json()
    assert body["degraded"] is True
    assert body["degraded_reason"] == "curated strategy cohort is empty"


async def test_leaderboard_not_degraded_when_curated_cohort_has_real_strategies():
    """Negative case: a populated, non-raising curated cohort must NOT be
    marked degraded — the empty-cohort branches above must not fire when
    there is real data."""
    from archimedes.main import app
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/leaderboard?scope=curated")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["entries"]) > 0, "test corpus must have real curated strategies for this assertion to be meaningful"
    assert body["degraded"] is False


async def test_leaderboard_runs_no_rigor_gate_at_all(monkeypatch):
    """Regression for #912, strengthened by #1746.

    #912: the public leaderboard must not recompute the rigor gate per strategy —
    two DB reads and two gate runs each, on an unauthenticated, unratelimited
    endpoint, is a trivial DoS. The fix then was to batch it into one cohort run.

    #1746 removed the run entirely: the verdict is graded once, when a curated
    backtest runs, and this route READS it (one batched query over
    ``strategy_passports``). Zero is the new one, and it is a stronger property
    than "batched" — a board that grades nothing cannot disagree with the same
    strategy's passport, which is what #1746 was about.

    MUTATION: restore ``grade_cohort(strategies)`` in
    ``leaderboard_routes._curated_cohort_responses``. The spy fires and this
    reddens.
    """
    from archimedes.main import app
    from archimedes.services.rigor_evaluator import run_rigor_gate as _real_gate
    from httpx import ASGITransport, AsyncClient

    calls: list = []

    def _counting(*args, **kwargs):
        calls.append(kwargs.get("strategy_id"))
        return _real_gate(*args, **kwargs)

    monkeypatch.setattr("archimedes.services.rigor_evaluator.run_rigor_gate", _counting)
    # Real persisted returns for every id — ANTI-VACUITY. With an empty returns
    # map a reintroduced cohort run finds nothing to grade and calls the gate
    # zero times anyway, so this guard would pass against the very code it
    # exists to refuse (caught by running MUTATION 11 against the empty-map
    # version of this test).
    rng = np.random.default_rng(11)
    monkeypatch.setattr(
        "archimedes.services.backtest_repository.get_all_daily_returns",
        lambda session, ids: {sid: rng.normal(0.001, 0.005, 300).tolist() for sid in ids},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/leaderboard")

    assert resp.status_code == 200
    assert calls == [], f"the public leaderboard ran the rigor gate {len(calls)} times; it must read a stored verdict"
