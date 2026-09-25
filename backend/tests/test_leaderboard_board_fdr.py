"""Board-level Benjamini-Hochberg FDR on GET /api/leaderboard (#1185 → #1564).

Owner decision (Dan, 2026-08-31): the strategy passport carries only
information about the strategy itself; the leaderboard is the one
cross-strategy surface. The correction moved here off the per-strategy gate
(its ABSENCE there is guarded in test_selection_bias_routes.py::
TestBoardFdrStaysOffThePerStrategyGate). This file owns the correction itself:

  1. The served numbers are a real BH run — pinned against a BH computed BY
     HAND, on paper, in the test below. Not "matches compute_board_level_fdr"
     (a stub matches itself trivially); actual arithmetic.
  2. A row with no DSR is never assigned a verdict.
  3. The correction is INVARIANT to the caller's filters and `limit` — the
     adversarial case, since BH's adjusted p is p×m/k and a smaller m makes
     every row look MORE significant.
  4. It stays ADVISORY: it feeds neither `conviction_score` nor
     `passes_rigor_gate`.

Hermetic where it can be (build_leaderboard is pure), plus one live route
check for the wire shape.
"""

from __future__ import annotations

import pytest
from archimedes.api.schemas import StrategyResponse
from archimedes.services.leaderboard import build_leaderboard, compute_conviction


def _strat(
    sid: str, *, dsr_p: float | None, passes_gate: bool = False, regime: str = "regime_neutral"
) -> StrategyResponse:
    return StrategyResponse(
        id=sid,
        methodology_summary=f"methodology for {sid}",
        asset_universe=["SPY"],
        position_sizing="equal_weight",
        rebalance_frequency="monthly",
        status="validated",
        paper_title=sid,
        passes_rigor_gate=passes_gate,
        dsr_p_value=dsr_p,
        regime_tag=regime,
    )


# ── 1. Hand-computed Benjamini-Hochberg ─────────────────────────────────────
#
# The cohort below is chosen so the whole BH procedure can be worked out on
# paper and written down as literals. `dsr_p_value` is a CONFIDENCE (HIGH =
# good), the opposite convention from the classical p-values BH consumes, so
# the correction converts with classical_p = 1 − dsr_p_value first.
#
#   dsr_p_value : 0.999  0.992  0.980  0.800  0.400
#   classical p : 0.001  0.008  0.020  0.200  0.600      (m = 5)
#
# BH critical values q_k = (k/m)·α with α = 0.05:
#   k=1 → 0.01   k=2 → 0.02   k=3 → 0.03   k=4 → 0.04   k=5 → 0.05
# Largest k with p_(k) ≤ q_k:
#   0.001 ≤ 0.01 ✓ | 0.008 ≤ 0.02 ✓ | 0.020 ≤ 0.03 ✓ | 0.200 ≤ 0.04 ✗ | 0.600 ≤ 0.05 ✗
#   ⇒ k* = 3, so the first three are rejected (= board-FDR-significant).
# Adjusted p̃_k = min(1, p_(k)·m/k), already monotone here:
#   0.001·5/1 = 0.005 | 0.008·5/2 = 0.020 | 0.020·5/3 = 0.033333…
#   0.200·5/4 = 0.250 | 0.600·5/5 = 0.600
# (already monotone non-decreasing, so BH's step-down pass changes nothing)
# Reported confidence = 1 − p̃ (so it reads the same direction as dsr_p_value):
#   0.995 | 0.980 | 0.966667 | 0.750 | 0.400
# Tolerance is 1e-7, not exact equality: compute_board_level_fdr rounds the
# reported figures to 8 decimals, so 0.020·5/3 lands as 0.03333333.
_HAND_COHORT = {
    "a": 0.999,
    "b": 0.992,
    "c": 0.980,
    "d": 0.800,
    "e": 0.400,
}
_HAND_ADJUSTED_P = {"a": 0.005, "b": 0.020, "c": 0.02 * 5 / 3, "d": 0.250, "e": 0.600}
_HAND_SIGNIFICANT = {"a": True, "b": True, "c": True, "d": False, "e": False}


def test_board_fdr_matches_a_hand_computed_bh():
    board = build_leaderboard([_strat(sid, dsr_p=p) for sid, p in _HAND_COHORT.items()])
    by_id = {e.id: e for e in board.entries}

    assert board.board_level_fdr.fdr_level == pytest.approx(0.05)
    assert board.board_level_fdr.n_tested == 5
    assert board.board_level_fdr.n_significant == 3

    for sid, expected_p in _HAND_ADJUSTED_P.items():
        assert by_id[sid].board_fdr_adjusted_p == pytest.approx(expected_p, abs=1e-7), (
            f"{sid}: served BH-adjusted p does not match the hand computation above"
        )
        assert by_id[sid].board_fdr_significant is _HAND_SIGNIFICANT[sid], f"{sid}: wrong BH rejection verdict"
        assert by_id[sid].board_fdr_confidence == pytest.approx(1.0 - expected_p, abs=1e-7)


def test_hand_computed_cohort_would_catch_a_bonferroni_substitution():
    """Anti-vacuity for the test above: those literals are BH-SPECIFIC, not
    "any multiple-testing correction would pass them". The nearest plausible
    substitution is Bonferroni, which adjusts to p·m — 0.005 / 0.040 / 0.100 /
    1.000 / 1.000 on this cohort. It rejects two hypotheses where BH rejects
    three, and disagrees numerically on three of the five rows, so a
    Bonferroni implementation fails the test above at both `n_significant`
    and `board_fdr_adjusted_p`. Written as an executable contrast rather than
    a claim in a comment."""
    m = len(_HAND_COHORT)
    bonferroni = {sid: min(1.0, (1.0 - p) * m) for sid, p in _HAND_COHORT.items()}

    n_rejected_bonferroni = sum(1 for v in bonferroni.values() if v <= 0.05)
    n_rejected_bh = sum(1 for v in _HAND_SIGNIFICANT.values() if v)
    assert n_rejected_bonferroni == 2
    assert n_rejected_bh == 3, "BH must be strictly more powerful than Bonferroni on this cohort"

    for sid in ("c", "d", "e"):
        assert bonferroni[sid] != pytest.approx(_HAND_ADJUSTED_P[sid], abs=1e-7), (
            f"{sid}: BH and Bonferroni agree here, so this row cannot tell the two apart"
        )


# ── 2. No verdict without evidence ──────────────────────────────────────────


def test_row_without_a_dsr_is_not_assigned_a_verdict():
    """A strategy with no finite DSR has nothing to correct. It must come back
    None on all three fields — never False, which is a claim ("tested, and it
    did not clear") the data does not support."""
    board = build_leaderboard([_strat("a", dsr_p=0.999), _strat("b", dsr_p=0.992), _strat("nodata", dsr_p=None)])
    by_id = {e.id: e for e in board.entries}

    assert by_id["nodata"].board_fdr_significant is None
    assert by_id["nodata"].board_fdr_adjusted_p is None
    assert by_id["nodata"].board_fdr_confidence is None
    # ...and it is excluded from m, so it cannot stiffen the correction for
    # the rows that do have evidence.
    assert board.board_level_fdr.n_tested == 2


def test_empty_board_reports_nothing_to_correct_rather_than_omitting_the_block():
    board = build_leaderboard([])
    assert board.board_level_fdr.n_tested == 0
    assert board.board_level_fdr.n_significant == 0
    assert board.board_level_fdr.fdr_level == pytest.approx(0.05)
    assert board.board_level_fdr.methodology


# ── 3. Invariance to the viewer's controls (the adversarial case) ───────────


def _wide_cohort() -> list[StrategyResponse]:
    """One strong row plus a field of weak ones. The strong row is NOT
    significant across the full board, but WOULD be if m shrank to the strong
    row alone — which is exactly the p-hack the invariance guards below
    forbid."""
    # classical p = 0.004 for the strong row. Against m=20 the BH critical
    # value for rank 1 is (1/20)·0.05 = 0.0025, so 0.004 does NOT clear it;
    # corrected alone (m=1) the threshold is 0.05 and it does.
    rows = [_strat("strong", dsr_p=0.996, passes_gate=True, regime="bull")]
    rows += [_strat(f"weak{i}", dsr_p=0.55, regime="bear") for i in range(19)]
    return rows


def test_shrinking_the_cohort_really_would_change_the_verdict():
    """Anti-vacuity for the two invariance tests below: prove the p-hack is
    live on this fixture. Corrected alone, the strong row clears BH; corrected
    against the full board it does not. If this ever stops being true the
    invariance guards are asserting nothing."""
    alone = build_leaderboard([_wide_cohort()[0]])
    assert alone.entries[0].board_fdr_significant is True

    full = build_leaderboard(_wide_cohort())
    strong = next(e for e in full.entries if e.id == "strong")
    assert strong.board_fdr_significant is False, (
        "fixture no longer demonstrates the shrink-the-cohort effect these guards exist to block"
    )


def test_correction_is_invariant_to_limit():
    """`limit` is a page size. If it moved m, a caller could page their way to
    significance."""
    full = build_leaderboard(_wide_cohort(), limit=100)
    paged = build_leaderboard(_wide_cohort(), limit=1)

    assert paged.board_level_fdr.n_tested == full.board_level_fdr.n_tested == 20
    assert len(paged.entries) == 1
    paged_strong = next(e for e in paged.entries if e.id == "strong")
    full_strong = next(e for e in full.entries if e.id == "strong")
    assert paged_strong.board_fdr_significant is full_strong.board_fdr_significant is False
    assert paged_strong.board_fdr_adjusted_p == pytest.approx(full_strong.board_fdr_adjusted_p)


def test_correction_is_invariant_to_regime_and_min_rigor_filters():
    """Same argument for the display filters. `regime_tag=bull` leaves exactly
    the strong row on screen; its correction must still be the one computed
    against the whole board."""
    full = build_leaderboard(_wide_cohort())
    full_strong = next(e for e in full.entries if e.id == "strong")

    for kwargs in ({"regime_tag": "bull"}, {"min_rigor": True}):
        filtered = build_leaderboard(_wide_cohort(), **kwargs)
        assert [e.id for e in filtered.entries] == ["strong"], f"fixture assumption broken for {kwargs}"
        assert filtered.board_level_fdr.n_tested == 20, (
            f"{kwargs} shrank the correction cohort — a viewer control must never change m"
        )
        strong = filtered.entries[0]
        assert strong.board_fdr_significant is False
        assert strong.board_fdr_adjusted_p == pytest.approx(full_strong.board_fdr_adjusted_p)


# ── 4. Advisory: it gates nothing and scores nothing ────────────────────────


def test_board_fdr_is_not_an_input_to_conviction_or_the_gate_badge():
    """The correction annotates; it does not grade. Two rows identical except
    for their board-FDR outcome must score identically on conviction, and the
    badge must be untouched by the correction.

    Constructed adversarially: `winner` and `loser` carry the SAME
    `dsr_p_value` (so conviction's dsr_confidence input is identical) but land
    on opposite sides of the BH threshold because of the company they keep."""
    same_dsr = 0.97
    cohort_a = [_strat("winner", dsr_p=same_dsr, passes_gate=True)] + [_strat(f"co{i}", dsr_p=0.999) for i in range(3)]
    cohort_b = [_strat("loser", dsr_p=same_dsr, passes_gate=True)] + [_strat(f"co{i}", dsr_p=0.5) for i in range(19)]

    a = build_leaderboard(cohort_a)
    b = build_leaderboard(cohort_b)
    winner = next(e for e in a.entries if e.id == "winner")
    loser = next(e for e in b.entries if e.id == "loser")

    assert winner.board_fdr_significant is True
    assert loser.board_fdr_significant is False, "fixture must put these two on opposite sides of BH"

    # Same conviction, same badge, same score components — the correction
    # changed the annotation and nothing else.
    assert winner.conviction_score == loser.conviction_score
    assert winner.score_components.model_dump() == loser.score_components.model_dump()
    assert winner.passes_rigor_gate is loser.passes_rigor_gate is True

    # And conviction is computed from the StrategyResponse alone — it has no
    # access to a cohort, so it structurally cannot see the correction.
    assert compute_conviction(cohort_a[0])[0] == winner.conviction_score


# ── 5. The wire ─────────────────────────────────────────────────────────────


def _strong_series(seed: int, n: int = 756) -> list[float]:
    """Long, low-vol, solidly positive drift → a DSR confidence near 1.0."""
    import numpy as np

    return np.random.default_rng(seed).normal(0.002, 0.008, n).tolist()


def _weak_series(seed: int, n: int = 300) -> list[float]:
    """Near-zero drift → weak DSR evidence, individually insignificant."""
    import numpy as np

    return np.random.default_rng(seed).normal(0.0001, 0.01, n).tolist()


async def test_leaderboard_route_serves_the_board_fdr_block(monkeypatch):
    """Live route over the real corpus: the block is on the wire, its α is
    explicit, and every row's verdict is consistent with the block's counts.

    THE RETURNS ARE PATCHED ON PURPOSE — the same seam the per-strategy gate's
    wiring test used before this correction moved off it
    (``TestBoardLevelFdrWiring._patch_returns``). Without it, a test box with an
    empty ``backtest_results`` store gives every row ``dsr_p_value is None``,
    ``n_tested == 0``, and the two count assertions below reduce to ``0 == 0``
    — the block would be served entirely unexercised and this test would pass
    against a route that computed nothing. ``n_tested > 0`` is asserted
    explicitly so the vacuum can never come back quietly.

    THE GRADING RUN IS ALSO ON PURPOSE (#1746 / PR-B). The board no longer grades
    on read: it serves each strategy's stored verdict, and the numbers this
    correction runs over are the ones a real gate run wrote. The vacuum this
    guards against therefore has a second door — an ungraded corpus — and
    ``n_tested > 0`` closes both.

    Patching at ``backtest_repository.get_all_daily_returns`` is the honest
    boundary (CLAUDE.md § "Mock at boundaries, not internals"): it substitutes
    the DB read only. The real ``run_rigor_gate`` computes every DSR, and the
    real ``compute_board_level_fdr`` runs the real BH over them.
    """
    from archimedes.api.strategies_routes import strategy_provider
    from archimedes.main import app
    from httpx import ASGITransport, AsyncClient

    strategies = strategy_provider().list_strategies()
    assert len(strategies) >= 6, "need >=6 curated strategies for a cohort with a real m"
    ids = [s.id for s in strategies]

    # One strong series and the rest weak, plus one series too short to grade
    # (<10 points → MISSING). That mix guarantees BOTH populations exist on the
    # wire: rows that got a verdict and a row that must not have one.
    returns = {ids[0]: _strong_series(0)}
    returns.update({sid: _weak_series(i + 1) for i, sid in enumerate(ids[1:-1])})
    returns[ids[-1]] = [0.001] * 5
    monkeypatch.setattr(
        "archimedes.services.backtest_repository.get_all_daily_returns",
        lambda session, sids: {sid: list(returns[sid]) for sid in sids if sid in returns},
    )

    # THE grading event. Since #1746 / PR-B the board READS each row's stored
    # verdict instead of grading the cohort per request, so the `dsr_p_value`s
    # the BH correction runs over have to exist before the request — which is
    # what the real gate run below writes. The patched DB read above is still
    # the only seam; the gate and the correction are both real.
    from archimedes.db import get_session
    from archimedes.services.curated_grading import grade_curated_library

    with get_session() as session:
        grade_curated_library(session, provider=strategy_provider())
        session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/leaderboard?scope=curated&limit=200")
    assert resp.status_code == 200
    body = resp.json()

    block = body["board_level_fdr"]
    assert block["fdr_level"] == pytest.approx(0.05)
    assert block["cohort_basis"] == "board_cohort_before_filters"
    assert block["methodology"]
    assert body["entries"], "the corpus must be non-empty for this to bite"

    corrected = [e for e in body["entries"] if e["board_fdr_significant"] is not None]
    # THE ANTI-VACUITY ASSERTION. Everything below it is a tautology at zero.
    assert block["n_tested"] > 0, (
        "the route served a board_level_fdr block over an empty cohort — the count assertions "
        "below would be 0 == 0 and would pass against a route that corrected nothing"
    )
    assert corrected, "at least one row must carry a real verdict for this test to measure anything"
    assert len(corrected) == block["n_tested"], (
        "n_tested must equal the number of rows that actually got a verdict — the served limit is "
        "200 and the corpus is smaller, so no row is paged out of this comparison"
    )
    assert sum(1 for e in corrected if e["board_fdr_significant"]) == block["n_significant"]

    # The too-short series must reach the wire as an honest absence, so the
    # "no verdict ⇒ no numbers" loop below is exercised too, not just skipped.
    by_id = {e["id"]: e for e in body["entries"]}
    if ids[-1] in by_id:
        assert by_id[ids[-1]]["board_fdr_significant"] is None
        assert by_id[ids[-1]]["dsr_p_value"] is None

    for e in body["entries"]:
        if e["board_fdr_significant"] is None:
            # No verdict ⇒ no numbers either. Never a half-populated row.
            assert e["board_fdr_adjusted_p"] is None
            assert e["board_fdr_confidence"] is None
            assert e["dsr_p_value"] is None
        else:
            assert 0.0 <= e["board_fdr_adjusted_p"] <= 1.0
            assert e["board_fdr_confidence"] == pytest.approx(1.0 - e["board_fdr_adjusted_p"], abs=1e-7)
