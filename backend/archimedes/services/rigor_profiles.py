"""Rigor strictness profiles — the 1–5 risk-tolerance ladder for the gate.

The rigor gate carries ONE badge meaning — Tier-1 "Archimedes Verified 🏆" —
anchored to the strictest level (level 1, Conservative). Separately, a user may
choose a personal *deployment* strictness from 1 (Conservative) to 5
(Speculative); a higher level accepts statistically weaker strategies into
*their own* vaults, but never rewrites the global badge.

Two kinds of check live in the gate, and only one is a risk-tolerance knob:

  * **Strictness-adjustable thresholds** — the DSR p-value floor, the PBO
    ceiling, and the OOS/IS cliff ratio. These trade statistical confidence for
    breadth and are what the slider moves.
  * **Always-on floors** — look-ahead audit PASS, a strictly-positive OOS Sharpe,
    a DSR p-value ≥ ``DSR_P_FLOOR``, and (when a combinatorial matrix exists)
    CPCV majority-positive. These are CORRECTNESS, not preference: a
    look-ahead-biased backtest is a lie about the past, and a strategy that loses
    money out-of-sample is broken, not "riskier." No strictness level bypasses
    them — this is what makes "you can never fully bypass the rigor gate" true on
    the live path.

The badge (``passes_rigor_gate``) is always evaluated at ``STRICTEST_LEVEL`` and
never moves with a user's slider — otherwise one user's risk appetite would
rewrite a global claim, violating the #1 "claims must be true" rule.

Owner: Önder (math lane). Spec: docs/specs/selection-bias-corrections-spec.md § 5.
"""

from __future__ import annotations

from dataclasses import dataclass

# ── Always-on floors (identical at every strictness level) ─────────────────
# A DSR p-value below this is worse-than-a-coin-flip after deflation — no level
# admits it. At level 5 the adjustable DSR threshold collapses onto this floor by
# design (the riskiest rung that is still not a coin flip).
DSR_P_FLOOR = 0.50
# OOS Sharpe must be strictly greater than this at every level: a strategy that
# loses money out-of-sample is broken, not merely riskier.
OOS_ABS_FLOOR = 0.0
# When a CPCV combinatorial OOS matrix is supplied, the edge must hold on a
# majority of held-out paths regardless of strictness.
CPCV_MIN_POSITIVE_FRACTION = 0.5

# ── The badge DSR bar — ONE definition, imported everywhere (#1794) ────────
# The Deflated-Sharpe p-value a strategy must reach to earn "Archimedes
# Verified": the conventional 95% one-sided bar. This is the ONLY place the
# number is written down. ``_PROFILES[STRICTEST_LEVEL].dsr_p_min`` IS this
# constant, the Generate pipeline imports it, and every other reader reaches it
# through a ``RigorProfile``. A second literal anywhere in the rigor modules is
# exactly the bug this constant exists to prevent —
# ``backend/tests/test_single_dsr_bar.py`` fails the build on one.
#
# History (#1794): PR #901 lowered the badge bar, but two hardcoded comparisons
# in ``generation_pipeline`` (``_rigor_verdict_for`` and
# ``_patch_dsr_with_pool_correlation``) and every public rigor page still said
# 95%. Neither literal reaches a candidate on today's debate-only pipeline —
# the shipped inconsistency was gate-vs-docs, not gate-vs-generator — but they
# were a split waiting to happen the moment a buy-and-hold path returned.
# Owner call, 2026-09-03: one bar, the 95% one; the lower bar and its #902
# rationale are retired. Market it as "deflated-Sharpe evidence at the 95%
# one-sided level", never "statistically proven".
DSR_P_BADGE_MIN = 0.95


@dataclass(frozen=True)
class RigorProfile:
    """Thresholds for one strictness level. Higher level == riskier == looser.

    ``dsr_p_min`` is monotonically non-increasing, and ``pbo_max`` /
    ``oos_is_ratio_min`` monotonic in the loosening direction, as ``level`` rises
    — so "passes at level L" is monotonic in L and a well-defined *minimum*
    passing level exists (see ``RigorGateResult.min_passing_level``).
    """

    level: int
    label: str
    dsr_p_min: float  # DSR p-value must be ≥ this
    pbo_max: float  # PBO must be < this
    oos_is_ratio_min: float  # OOS/IS Sharpe ratio must be ≥ this
    description: str


# The ladder. Level 1 (Conservative) is the badge bar; level 5 (Speculative) is
# the riskiest rung. ``dsr_p_min`` at level 5 equals ``DSR_P_FLOOR`` by design.
#
# Level 1's ``dsr_p_min`` is ``DSR_P_BADGE_MIN`` — the constant above, never a
# literal here, so the badge bar cannot drift away from what the Generate path
# and the public docs quote. Levels 2–5 are a *personal deployment* risk knob:
# a user may accept statistically weaker strategies into their OWN vaults, but
# no level rewrites the badge, which is always graded at ``STRICTEST_LEVEL``.
_PROFILES: dict[int, RigorProfile] = {
    1: RigorProfile(
        1,
        "Conservative",
        DSR_P_BADGE_MIN,
        0.50,
        0.50,
        "Archimedes Verified bar. Only strategies with a statistically strong, overfit-resistant, non-degrading edge.",
    ),
    2: RigorProfile(
        2,
        "Balanced",
        0.80,
        0.55,
        0.45,
        "Slightly relaxed confidence — a strong edge that is a touch below the Verified bar.",
    ),
    3: RigorProfile(
        3,
        "Moderate",
        0.70,
        0.60,
        0.40,
        "Accepts more uncertainty for more breadth. The edge is probable but not highly confident.",
    ),
    4: RigorProfile(
        4,
        "Aggressive",
        0.60,
        0.65,
        0.35,
        "High risk tolerance — thinner statistical margin and more tolerance for overfitting.",
    ),
    5: RigorProfile(
        5,
        "Speculative",
        0.50,
        0.70,
        0.30,
        "Riskiest rung. Only the always-on correctness floors (look-ahead, positive OOS, DSR ≥ 0.50) still apply.",
    ),
}

STRICTNESS_LEVELS: tuple[int, ...] = tuple(sorted(_PROFILES))
STRICTEST_LEVEL: int = min(_PROFILES)  # 1 — the badge / Archimedes Verified level
LOOSEST_LEVEL: int = max(_PROFILES)  # 5
# The gate and the badge default to the strictest level: an unspecified strictness
# is the most conservative, never the most permissive (fail-safe).
DEFAULT_LEVEL: int = STRICTEST_LEVEL


def clamp_level(level: int | None) -> int:
    """Coerce an arbitrary caller-supplied level into ``[STRICTEST, LOOSEST]``.

    ``None`` / non-integer / out-of-range values fail *safe* to the strictest
    level rather than the most permissive one, so a malformed strictness can
    never accidentally loosen the gate.
    """
    if level is None:
        return DEFAULT_LEVEL
    try:
        lv = int(level)
    except (TypeError, ValueError):
        return DEFAULT_LEVEL
    return min(max(lv, STRICTEST_LEVEL), LOOSEST_LEVEL)


def get_profile(level: int | None) -> RigorProfile:
    """Return the :class:`RigorProfile` for ``level`` (clamped, fail-safe)."""
    return _PROFILES[clamp_level(level)]


def all_profiles() -> list[RigorProfile]:
    """Every profile, strictest-first — for API disclosure of the whole ladder."""
    return [_PROFILES[lv] for lv in STRICTNESS_LEVELS]
