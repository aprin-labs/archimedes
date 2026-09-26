"""Tests for the per-user rigor strictness ladder (1–5) and its enforcement.

Covers:
  * rigor_profiles — the ladder table, monotonicity, fail-safe clamping, floors.
  * RigorGateResult — passes_at_level / min_passing_level / blocked_by_floor, and
    that the level-1 badge stays the strictest bar (DSR ``DSR_P_BADGE_MIN``).
  * size_strategies — the strictness-derived deployable_ids override.
  * the deploy gate — refuses at the strictest level (badge), admits a curated
    strategy that only passes at a looser level, and never admits a floor-blocked
    one at any level (the "you can never fully bypass the gate" guarantee).
  * the strictness-ladder disclosure endpoint.

Hermetic: pure computation + mocked boundaries, no DB / network.
"""

from __future__ import annotations

import asyncio
from itertools import pairwise

import pytest
from archimedes.services.rigor_evaluator import RigorGateResult
from archimedes.services.rigor_profiles import (
    DEFAULT_LEVEL,
    DSR_P_BADGE_MIN,
    DSR_P_FLOOR,
    LOOSEST_LEVEL,
    STRICTEST_LEVEL,
    STRICTNESS_LEVELS,
    all_profiles,
    clamp_level,
    get_profile,
)

# ── rigor_profiles ─────────────────────────────────────────────────────────


def test_five_levels_strictest_is_badge_default():
    assert STRICTNESS_LEVELS == (1, 2, 3, 4, 5)
    assert STRICTEST_LEVEL == 1
    assert LOOSEST_LEVEL == 5
    assert DEFAULT_LEVEL == STRICTEST_LEVEL  # unspecified strictness is fail-safe


def test_ladder_is_monotonic():
    profiles = all_profiles()
    for a, b in pairwise(profiles):  # adjacent (level, level+1) pairs
        # Higher level == looser: DSR bar drops, PBO ceiling rises, OOS/IS bar drops.
        assert b.dsr_p_min <= a.dsr_p_min
        assert b.pbo_max >= a.pbo_max
        assert b.oos_is_ratio_min <= a.oos_is_ratio_min


def test_level_1_is_the_one_badge_bar():
    # #1794: the badge rung IS DSR_P_BADGE_MIN — not a copy of its value, so the
    # ladder cannot drift away from what the Generate path and the docs quote.
    p1 = get_profile(1)
    assert p1.dsr_p_min is DSR_P_BADGE_MIN
    assert p1.pbo_max == 0.50
    assert p1.oos_is_ratio_min == 0.50


def test_level_5_dsr_collapses_onto_the_floor():
    assert get_profile(5).dsr_p_min == DSR_P_FLOOR


def test_clamp_is_fail_safe_to_strictest():
    # None / non-int / out-of-range all clamp toward the strictest, never loosest.
    assert clamp_level(None) == STRICTEST_LEVEL
    assert clamp_level("nonsense") == STRICTEST_LEVEL
    assert clamp_level(0) == STRICTEST_LEVEL
    assert clamp_level(-3) == STRICTEST_LEVEL
    assert clamp_level(99) == LOOSEST_LEVEL
    assert clamp_level(3) == 3


# ── RigorGateResult ladder ─────────────────────────────────────────────────


def _mk(**kw) -> RigorGateResult:
    base = {
        "dsr_p_value": 0.97,
        "pbo_score": 0.20,
        "oos_sharpe": 1.0,
        "in_sample_sharpe": 1.0,
        "look_ahead_passed": True,
    }
    base.update(kw)
    return RigorGateResult("s", **base)


def test_strong_strategy_passes_badge():
    r = _mk()
    assert r.passes_all is True
    assert r.min_passing_level == 1
    assert r.blocked_by_floor is False


def test_the_retired_090_band_no_longer_earns_the_badge():
    # #1794 retired PR #901's lower bar. A candidate in the old [0.90, 0.95) band
    # used to earn the badge here while the Generate path called the same numbers
    # a failure. It is now a level-2 (Balanced) strategy: deployable into a user's
    # own vault at their chosen strictness, never "Archimedes Verified".
    r = _mk(dsr_p_value=0.92)
    assert r.passes_all is False
    assert r.min_passing_level == 2
    assert r.blocked_by_floor is False  # statistically weak, not broken


def test_dsr_ladder_maps_to_min_level():
    r = _mk(dsr_p_value=0.72)  # needs level 3 (0.70), fails level 2 (0.80)
    assert r.min_passing_level == 3
    assert r.passes_at_level(2) is False
    assert r.passes_at_level(3) is True


def test_pbo_ladder_maps_to_min_level():
    r = _mk(pbo_score=0.62)  # needs level 4 (PBO < 0.65), fails level 3 (< 0.60)
    assert r.min_passing_level == 4


def test_lookahead_failure_is_a_floor_blocked_everywhere():
    r = _mk(look_ahead_passed=False)
    assert r.blocked_by_floor is True
    assert r.min_passing_level is None
    assert r.passes_at_level(LOOSEST_LEVEL) is False  # even the riskiest level refuses


def test_negative_oos_is_a_floor_blocked_everywhere():
    r = _mk(oos_sharpe=-0.1, in_sample_sharpe=None)
    assert r.blocked_by_floor is True
    assert r.passes_at_level(LOOSEST_LEVEL) is False


def test_dsr_below_floor_is_blocked_everywhere():
    r = _mk(dsr_p_value=0.45)  # below the 0.50 always-on floor
    assert r.blocked_by_floor is True
    assert r.min_passing_level is None


def test_weak_but_not_floor_blocked_passes_only_at_level_5():
    r = _mk(dsr_p_value=0.55)  # ≥ 0.50 floor, but only clears level-5's 0.50 bar
    assert r.blocked_by_floor is False
    assert r.min_passing_level == 5


def test_cliff_without_negative_oos_is_not_floor_blocked():
    # OOS is positive (floor OK) but OOS/IS ratio 0.30 only clears level 5.
    r = _mk(oos_sharpe=0.3, in_sample_sharpe=1.0)
    assert r.blocked_by_floor is False
    assert r.min_passing_level == 5


def test_gate_details_reference_the_active_profile():
    r = _mk(dsr_p_value=0.65, pbo_score=0.62, profile=get_profile(3))
    assert r.gate_details["dsr"] == "FAIL (p=0.6500, need ≥ 0.70)"
    assert "need < 0.60" in r.gate_details["pbo"]


# ── size_strategies deployable_ids override ────────────────────────────────


class _Strat:
    def __init__(self, sid, passes, kelly=0.5):
        self.id = sid
        self.passes_rigor_gate = passes
        self.kelly_fraction = kelly


def test_deployable_ids_overrides_the_badge():
    from archimedes.services.strategy_sizer import size_strategies

    strategies = [_Strat("a", passes=False), _Strat("b", passes=False)]
    # 'a' is deployable at the user's level even though its level-1 badge is False.
    sized = size_strategies(strategies, "moderate", deployable_ids={"a"})
    assert sized["a"] > 0.0
    assert sized["b"] == 0.0


def test_deployable_ids_none_falls_back_to_badge():
    from archimedes.services.strategy_sizer import size_strategies

    strategies = [_Strat("a", passes=True), _Strat("b", passes=False)]
    sized = size_strategies(strategies, "moderate")  # deployable_ids=None
    assert sized["a"] > 0.0
    assert sized["b"] == 0.0


# ── deploy gate strictness enforcement ─────────────────────────────────────

V = "archimedes.api.vaults_routes"


def test_deploy_at_badge_level_uses_badge_and_refuses_failing():
    from unittest.mock import patch

    from archimedes.api.vaults_routes import _assert_strategies_pass_rigor
    from fastapi import HTTPException

    with patch(f"{V}._strategy_rigor_status", return_value=(True, False)), pytest.raises(HTTPException) as exc:
        _assert_strategies_pass_rigor(["s1"], strictness_level=1)
    assert exc.value.status_code == 422


def test_deploy_at_looser_level_admits_strategy_passing_there():
    from unittest.mock import patch

    from archimedes.api.vaults_routes import _assert_strategies_pass_rigor

    # Strategy passes only at level 3; a user at level 3 may deploy it.
    with patch(f"{V}._deployable_levels", return_value={"s1": (True, 3, False)}):
        _assert_strategies_pass_rigor(["s1"], strictness_level=3)  # no raise


def test_deploy_refused_when_min_level_above_chosen():
    from unittest.mock import patch

    from archimedes.api.vaults_routes import _assert_strategies_pass_rigor
    from fastapi import HTTPException

    with (
        patch(f"{V}._deployable_levels", return_value={"s1": (True, 4, False)}),
        pytest.raises(HTTPException) as exc,
    ):
        _assert_strategies_pass_rigor(["s1"], strictness_level=3)
    assert exc.value.status_code == 422
    assert "requires strictness level ≥ 4" in exc.value.detail


def test_floor_blocked_strategy_refused_at_every_level():
    from unittest.mock import patch

    from archimedes.api.vaults_routes import _assert_strategies_pass_rigor
    from fastapi import HTTPException

    with (
        patch(f"{V}._deployable_levels", return_value={"s1": (True, None, True)}),
        pytest.raises(HTTPException) as exc,
    ):
        _assert_strategies_pass_rigor(["s1"], strictness_level=LOOSEST_LEVEL)
    assert exc.value.status_code == 422
    assert "always-on rigor floor" in exc.value.detail


# ── strictness-ladder disclosure endpoint ──────────────────────────────────


def test_strictness_ladder_endpoint_discloses_full_ladder_and_floors():
    from archimedes.api.selection_bias_routes import get_strictness_ladder

    resp = asyncio.run(get_strictness_ladder())
    assert [lvl.level for lvl in resp.levels] == [1, 2, 3, 4, 5]
    assert resp.badge_level == STRICTEST_LEVEL
    assert resp.default_level == DEFAULT_LEVEL
    assert resp.floors["dsr_p_floor"] == DSR_P_FLOOR
    # Level 1 is the Conservative/Verified bar — the one bar, DSR_P_BADGE_MIN.
    lvl1 = next(lvl for lvl in resp.levels if lvl.level == 1)
    assert lvl1.label == "Conservative"
    assert lvl1.dsr_p_min == DSR_P_BADGE_MIN
