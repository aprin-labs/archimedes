"""``gate_version`` — which gate produced a stored rigor verdict.

The passport is the rigor verdict of record (``docs/adr/rigor-verdict-of-record.md``):
a strategy is graded ONCE, at backtest time, and every surface reads that stored
verdict. A stored verdict is only meaningful if a reader can tell WHICH gate
produced it — otherwise two rows graded months apart, under different thresholds,
present as one comparable number.

``gate_version()`` is that answer: a short, stable digest of everything that can
move a verdict **without the strategy's own return series moving**. Two rows with
the same ``gate_version`` were graded by the same gate and are comparable. Two
rows with different ones are not, and a re-grade (PR-C) is what closes the gap.

WHAT GOES INTO THE DIGEST — the complete list, and why each item is in it:

* **The strictness ladder** (``rigor_profiles._PROFILES``): every level's
  ``dsr_p_min`` / ``pbo_max`` / ``oos_is_ratio_min``. The badge is graded at
  ``STRICTEST_LEVEL``, but the whole ladder participates because
  ``RigorGateResult.min_passing_level`` — also persisted downstream — moves with
  any rung.
* **The always-on floors** (``DSR_P_FLOOR``, ``OOS_ABS_FLOOR``,
  ``CPCV_MIN_POSITIVE_FRACTION``): correctness floors no strictness level
  bypasses. A change here can flip a pass to a fail with no threshold on the
  ladder moving.
* **``STRICTEST_LEVEL``** — which rung the badge is anchored to.
* **``DSR_P_BADGE_MIN``** — the one place the Archimedes Verified DSR bar is
  written down (#1794). Today it IS level 1's ``dsr_p_min`` above, so hashing it
  is redundant *while that holds*. It is hashed on its own anyway for two
  reasons: ``generation_pipeline`` compares against the constant directly rather
  than through a profile, so the constant is a gate input in its own right; and
  the identity between the two is a source-level property that no runtime check
  can enforce (equal float constants in one module are the same object, so an
  ``is`` assertion cannot see them come apart). Hashing the constant means the
  bar cannot move without this digest moving, however the ladder is wired.
* **``_MIN_RETURNS_FOR_GATE``** (``live_rigor_gate``): the boundary between
  ``pending`` and a graded verdict. Moving it re-labels rows without regrading
  anything.
* **The DSR / risk-free convention** (``_rigor_helpers._ANNUALIZATION``,
  ``_RF_ANNUAL``, and the ``rf_series`` convention label): the gate deflates an
  EXCESS-return Sharpe, and the rf convention is the excess part. #1187 exists
  because a fixture snapshot outlived a change to exactly this.
* **``MIN_LIBRARY_N_FOR_PBO_GATING``** (``rigor_evaluator``): the cohort size at
  which the PBO criterion becomes gating rather than advisory.
* **``GATE_CODE_REVISION``** — the escape hatch below.

WHAT DOES **NOT** GO IN, deliberately:

* **The git commit / package version.** A digest that changes on every commit
  would mark every stored verdict stale on every deploy, which is the same as
  carrying no version at all — the field would stop being read.
* **Board-level FDR** (``DEFAULT_BOARD_FDR_LEVEL``). Per the owner's option-1
  call on #1654 board FDR is a live *relational* signal on the leaderboard, it
  never flips ``passes_all``, and it is deliberately not on the passport — so it
  cannot move a stored verdict and must not move this digest.
* **Per-strategy inputs** (``num_trials``, the strategy's own returns, its cohort).
  Those describe the GRADE, not the GATE. ``cohort_n`` on the passport records
  the cohort half separately.

``GATE_CODE_REVISION`` is the honest admission that a digest over constants
cannot see a logic change. When ``run_rigor_gate``'s behaviour changes in a way
that can move a verdict without any constant above changing — a new criterion, a
fixed bug in an existing one, a different degeneracy predicate — **bump it in the
same commit**. That is a human obligation, stated here rather than pretended
away, and ``backend/tests/test_rigor_gate_version.py`` pins the constant so the
bump is a deliberate, reviewed edit rather than a drift.

Owner: Dan Browne (decision of record: docs/adr/rigor-verdict-of-record.md).
Math lane: Önder Akkaya.
"""

from __future__ import annotations

import hashlib
import json

# Bumped BY HAND when the gate's logic (not its constants) changes in a way that
# can move a verdict. See the module docstring. Starts at 1 with the
# verdict-of-record migration; every row the migration derives instead carries
# the literal ``LEGACY_DERIVED`` below, never a real digest.
GATE_CODE_REVISION = 1

# The digest's own layout version. Bump when the INPUT SET above changes (an item
# added or removed), so a reader can tell "different thresholds" from "different
# things being hashed".
#
# 1 → 2: ``badge_bar`` (``DSR_P_BADGE_MIN``) joined the inputs with #1794, so the
# one place the badge's DSR bar is written down is hashed directly rather than
# only through level 1 of the ladder.
GATE_VERSION_SCHEMA = 2

# Marker written by the verdict-of-record migration onto rows whose verdict was
# DERIVED from pre-existing columns rather than produced by a gate run. It is not
# a gate version and must never be compared to one: it means "no gate produced
# this — re-grade before trusting it".
LEGACY_DERIVED = "legacy-derived"


def gate_version_inputs() -> dict:
    """The exact dict that gets hashed — exposed so a test can read it, and so a
    reviewer can see what a version bump would be reacting to."""
    from archimedes.services import rf_series
    from archimedes.services._rigor_helpers import _ANNUALIZATION, _RF_ANNUAL
    from archimedes.services.live_rigor_gate import _MIN_RETURNS_FOR_GATE
    from archimedes.services.rigor_evaluator import MIN_LIBRARY_N_FOR_PBO_GATING
    from archimedes.services.rigor_profiles import (
        CPCV_MIN_POSITIVE_FRACTION,
        DSR_P_BADGE_MIN,
        DSR_P_FLOOR,
        OOS_ABS_FLOOR,
        STRICTEST_LEVEL,
        all_profiles,
    )

    return {
        "schema": GATE_VERSION_SCHEMA,
        "code_revision": GATE_CODE_REVISION,
        "badge_level": STRICTEST_LEVEL,
        "badge_bar": DSR_P_BADGE_MIN,
        "profiles": [
            {
                "level": p.level,
                "dsr_p_min": p.dsr_p_min,
                "pbo_max": p.pbo_max,
                "oos_is_ratio_min": p.oos_is_ratio_min,
            }
            for p in all_profiles()
        ],
        "floors": {
            "dsr_p": DSR_P_FLOOR,
            "oos_abs": OOS_ABS_FLOOR,
            "cpcv_min_positive_fraction": CPCV_MIN_POSITIVE_FRACTION,
        },
        "min_returns_for_gate": _MIN_RETURNS_FOR_GATE,
        "min_library_n_for_pbo_gating": MIN_LIBRARY_N_FOR_PBO_GATING,
        "dsr_convention": {
            "annualization": _ANNUALIZATION,
            "rf_annual_fallback": _RF_ANNUAL,
            "rf_convention_fallback": rf_series.RF_CONVENTION_FALLBACK,
            "returns": "excess",
        },
    }


def gate_version() -> str:
    """Stable, short identifier for the gate that graded a strategy.

    Shape: ``"gate-v<schema>-<16 hex>"`` — fits ``strategy_passports.gate_version``
    (``String(64)``) with room to spare, and is greppable in a DB dump.

    Deterministic within a build: the same constants always produce the same
    string, on any machine, in any order of dict construction (``sort_keys``).
    """
    canonical = json.dumps(gate_version_inputs(), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"gate-v{GATE_VERSION_SCHEMA}-{digest}"
