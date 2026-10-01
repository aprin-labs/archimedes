"""Per-strategy rigor-gate reasons — which checks failed, which passed, against what bar.

The Library's "Rejected (N) — did not pass the rigor gate" section used to
explain every rejected candidate with ONE shared paragraph of prose ("Most
rejections at this stage are 'return series too short' … A longer backtest
window typically unlocks them"). That sentence was written from a guess about
the population, not read from any strategy's record, so for a candidate rejected
for a DIFFERENT reason the surface stated something false about that strategy.

This module turns a strategy's OWN stored ``rigor_verdict`` into a per-check
report the card can render. That blob is the JSON in
``StrategyRecord.rigor_verdict``. On today's live write paths it comes from
``debate_engine._rigor_verdict_dict`` (the graded fusion/debate verdict, the only
writer of the four-state ``look_ahead_status``), from
``debate_engine._abstain_result`` and ``generation_pipeline``'s fixture branch
(both non-graded, both carrying a verbatim ``reason``), and is then patched by
``generation_pipeline._patch_pbo`` / ``_patch_dsr_with_pool_correlation``.
``generation_pipeline._rigor_verdict_for`` — the writer of the too-short-series
branch — currently has NO production callers (repo-wide, only its own ``def``
and tests), so the shapes it produces are reachable only on STORED rows.

It is a PURE function over a dict the caller already holds — no DB read, no gate
re-run — so a list route calls it once per row and adds ZERO queries to the page
(see ``strategies_routes.list_generated_strategies``).

Which bar
---------
Thresholds are read from ``rigor_profiles.get_profile(STRICTEST_LEVEL)`` — the
Archimedes Verified bar the badge is defined against — plus the always-on
``OOS_ABS_FLOOR``. They are never re-declared here, so the number a card prints
is the number the gate uses.

Honesty rules this module keeps
-------------------------------
* ``fail`` is reserved for a check that ran, produced a finite number (or a real
  audit finding), and did not clear its bar. A check with nothing on record is
  ``not_computed`` — never folded into ``fail`` (that would claim an evaluation
  happened), and never counted as ``pass`` (fail-closed: it still blocks
  admission, and the report says so).
* ``recorded_reason`` is the verdict's own ``reason`` string, verbatim. The
  pipeline writes it exactly for the branches where the gate did NOT grade the
  candidate at all — return series too short, fixture mode, debate abstain. When
  it is present it IS this strategy's reason and no derived sentence replaces it,
  AND no numeric leg on that blob is reported as a graded result: the numbers
  sitting on an ungraded verdict are defaults (``_patch_pbo`` stamps ``pbo=0.0``
  as "undefined for N<2" on exactly these candidates), and printing a default as
  a passed check is the same overclaim this module exists to remove.
* ``unattributed`` is True when a row did not pass yet no recorded check fails
  the bar above. The surface then says exactly that instead of naming a culprit
  it cannot support. The original source of that gap was a DSR-bar divergence:
  the agent path's ``passing`` hardcoded its own, stricter bar in
  ``_rigor_verdict_for`` while the badge profile's ``dsr_p_min`` was a looser
  number, so a p-value between the two landed a rejected row with every
  badge-bar check clear. #1794 retired that split — both are now
  ``rigor_profiles.DSR_P_BADGE_MIN``, the one place the bar is written down —
  so no NEW write can open that particular gap. The state is still computed and
  still rendered, because the gap outlives its first cause. Rows STORED under
  the retired bar keep the ``passing`` they were given. And a leg with nothing
  on record blocks admission (the fusion gate fails closed on a missing or
  non-finite DSR p, PBO, or look-ahead verdict) while this report calls it
  ``not_computed`` rather than ``fail`` — so a row rejected for a measurement
  that never happened has no failing check to point at. A row the surface
  cannot attribute must say so either way.
"""

from __future__ import annotations

import math
from typing import Any

from archimedes.services.rigor_profiles import (
    OOS_ABS_FLOOR,
    STRICTEST_LEVEL,
    get_profile,
)

# Per-check status vocabulary. Only PASS clears; NOT_COMPUTED is fail-closed
# (it blocks admission exactly as hard as FAIL) but must never be RENDERED as a
# failure — same distinction ``RigorGateResult.gate_details`` already draws
# between "FAIL" and "NOT_RUN".
PASS = "pass"
FAIL = "fail"
NOT_COMPUTED = "not_computed"

# The badge this report grades against, named for the surface.
BAR_NAME = "Archimedes Verified"

# The gate needs at least this many persisted daily returns before it can score
# anything. Pinned to the pipeline's own behaviour by
# ``test_rejected_reason_payload.test_pipeline_still_writes_the_short_series_reason``
# rather than shared as a constant, so the string and the threshold below can
# never silently drift away from what the pipeline actually writes.
MIN_RETURNS_FOR_GATE = 10

# The verbatim ``reason`` strings the pipeline writes for its two non-graded
# branches. Matching them yields a stable ``reason_code`` the frontend can
# classify on without string-matching prose in JS. An unrecognised reason gets
# ``reason_code = None`` and is still surfaced verbatim — never dropped.
#
# ``SHORT_SERIES_REASON``'s only writer is ``_rigor_verdict_for``, which has no
# production callers today, so it matches STORED rows only — the short-series
# summary sentence on the frontend is a legacy path, not one new rows enter.
# ``FIXTURE_REASON`` is written inline by the fixture branch and is live.
SHORT_SERIES_REASON = "return series too short for rigor evaluation"
FIXTURE_REASON = "fixture mode — no LLM call, rigor gate not run"

_REASON_CODES = {
    SHORT_SERIES_REASON: "short_return_series",
    FIXTURE_REASON: "fixture_mode",
}

# ``look_ahead_status`` values that are NOT verdicts (see
# services/dsl_lookahead_audit.py). They block admission and are reported as
# not-computed, never as a failed audit.
_LA_INCONCLUSIVE = ("pending", "degenerate")


def _finite(value: Any) -> float | None:
    """Coerce to a finite float, or ``None``.

    ``None``/NaN/inf/non-numeric all become ``None`` — a claim may not rest on a
    number that isn't one. ``bool`` is rejected explicitly because ``float(True)``
    is ``1.0``, and a boolean leg (``lookahead_audit_passed``) landing in a
    numeric slot must not be printed as a measurement.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _check(
    key: str,
    label: str,
    status: str,
    detail: str,
    *,
    value: float | None = None,
    threshold: float | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "status": status,
        "detail": detail,
        "value": value,
        "threshold": threshold,
    }


def _dsr_check(verdict: dict[str, Any], profile) -> dict[str, Any]:
    label = "DSR confidence"
    bar = profile.dsr_p_min
    p = _finite(verdict.get("dsr_p_value"))
    if p is None:
        return _check("dsr", label, NOT_COMPUTED, "no deflated-Sharpe p-value on record", threshold=bar)
    if p >= bar:
        return _check("dsr", label, PASS, f"{p:.2f} ≥ {bar:.2f} required", value=p, threshold=bar)
    return _check("dsr", label, FAIL, f"{p:.2f} < {bar:.2f} required", value=p, threshold=bar)


def _pbo_check(verdict: dict[str, Any], profile) -> dict[str, Any]:
    label = "PBO (overfitting probability)"
    bar = profile.pbo_max
    pbo = _finite(verdict.get("pbo"))
    if pbo is None:
        return _check("pbo", label, NOT_COMPUTED, "no PBO on record", threshold=bar)
    if pbo < bar:
        return _check("pbo", label, PASS, f"{pbo:.2f} < {bar:.2f} required", value=pbo, threshold=bar)
    return _check(
        "pbo", label, FAIL, f"{pbo:.2f} ≥ {bar:.2f} — at or above the overfitting ceiling", value=pbo, threshold=bar
    )


def _oos_floor_check(verdict: dict[str, Any]) -> dict[str, Any]:
    label = "Out-of-sample Sharpe"
    oos = _finite(verdict.get("oos_sharpe"))
    if oos is None:
        return _check("oos_sharpe", label, NOT_COMPUTED, "no out-of-sample Sharpe on record", threshold=OOS_ABS_FLOOR)
    if oos > OOS_ABS_FLOOR:
        return _check(
            "oos_sharpe", label, PASS, f"{oos:.2f} > {OOS_ABS_FLOOR:.2f} required", value=oos, threshold=OOS_ABS_FLOOR
        )
    return _check(
        "oos_sharpe",
        label,
        FAIL,
        f"{oos:.2f} ≤ {OOS_ABS_FLOOR:.2f} — no out-of-sample edge",
        value=oos,
        threshold=OOS_ABS_FLOOR,
    )


def _oos_cliff_check(verdict: dict[str, Any], profile) -> dict[str, Any]:
    """The in-/out-of-sample cliff leg.

    The gate only applies this ratio when there is a POSITIVE in-sample Sharpe to
    divide by (the ``in_sample_sharpe > 0`` guard in ``_rigor_verdict_for`` and in
    the fusion evaluator's cliff leg). With no such reference — or with no
    out-of-sample Sharpe to divide — the check did not run, and is reported as
    not-computed naming the side that is missing, never as a pass it never earned.
    """
    label = "Out-of-sample / in-sample ratio"
    bar = profile.oos_is_ratio_min
    oos = _finite(verdict.get("oos_sharpe"))
    in_sample = _finite(verdict.get("in_sample_sharpe"))
    if oos is None or in_sample is None or in_sample <= 0:
        # Name the side that is actually missing. "No positive in-sample Sharpe"
        # is false on a row that HAS one (say 1.20) and is missing the OOS leg.
        detail = (
            "no out-of-sample Sharpe to compare" if oos is None else "no positive in-sample Sharpe to compare against"
        )
        return _check("oos_is_ratio", label, NOT_COMPUTED, detail, threshold=bar)
    ratio = oos / in_sample
    if ratio >= bar:
        return _check("oos_is_ratio", label, PASS, f"{ratio:.2f} ≥ {bar:.2f} required", value=ratio, threshold=bar)
    return _check(
        "oos_is_ratio",
        label,
        FAIL,
        f"{ratio:.2f} < {bar:.2f} required — the edge collapses out of sample",
        value=ratio,
        threshold=bar,
    )


def _look_ahead_check(verdict: dict[str, Any], *, has_recorded_reason: bool) -> dict[str, Any]:
    """The look-ahead leg, four-state where the writer supplied four states.

    ``look_ahead_status`` (fusion/debate path) is the honest four-state verdict
    and is the ONLY field a rendered claim may key off. ``lookahead_audit_passed``
    is the fail-closed boolean every path writes, and neither of its values is an
    audit result on its own:

    * a bare ``False`` is a real audit finding ONLY when the gate actually graded
      this candidate — on a row whose verdict carries its own ``reason`` the gate
      never ran, and reporting "the audit found a leak" there would invent a
      finding out of a default;
    * a bare ``True`` is never a finding at all — it is what the agent path
      writes when nothing auditable was found to audit.
    """
    label = "Look-ahead audit"
    status = verdict.get("look_ahead_status")
    reason = verdict.get("look_ahead_reason")
    if status == PASS:
        return _check("look_ahead", label, PASS, "no forward-looking data access found")
    if status in _LA_INCONCLUSIVE:
        detail = reason.strip() if isinstance(reason, str) and reason.strip() else "the audit reached no verdict"
        return _check("look_ahead", label, NOT_COMPUTED, f"{detail} — blocks admission (fail-closed)")
    if has_recorded_reason:
        return _check("look_ahead", label, NOT_COMPUTED, "not run — see the reason on record")
    if verdict.get("lookahead_audit_passed") is True:
        # A bare ``True`` with no four-state status is a DEFAULT, not an audit
        # result. ``_lookahead_for_candidate`` is documented as "vacuously True
        # when none expose auditable source" (generation_pipeline.py:622), and
        # the repo has explicitly retired this rendering:
        # ``dsl_lookahead_audit.verdict_from_persisted_row`` refuses to honour a
        # stored True because "the alternative is a live surface showing
        # 'look_ahead: PASS' on the strength of a sentence an LLM wrote about
        # itself". Any look-ahead CLAIM keys off ``look_ahead_status``
        # (debate_engine.py:770-773) — which the fusion writer always sets, so no
        # conclusive row reaches this branch.
        return _check(
            "look_ahead",
            label,
            NOT_COMPUTED,
            "no four-state audit verdict on record — the stored boolean is not an audit result",
        )
    if verdict.get("lookahead_audit_passed") is False:
        return _check("look_ahead", label, FAIL, "the audit found a forward-looking pattern")
    return _check("look_ahead", label, NOT_COMPUTED, "no look-ahead audit result on record")


def rigor_reasons_for_verdict(verdict: Any) -> dict[str, Any] | None:
    """Per-check report for ONE strategy's stored ``rigor_verdict``.

    ``None`` in (no verdict persisted) → ``None`` out: a row with nothing on
    record gets no reasons block at all, which the card renders as an honest
    absence rather than as a set of checks that never ran.

    The returned dict is JSON-safe and additive — nothing existing changes shape:

    ``bar`` / ``bar_level``
        The badge and its strictness level, so the card can name the bar it is
        quoting instead of printing bare thresholds.
    ``passing``
        The verdict's own stored boolean, echoed for callers that hold the
        report without the verdict.
    ``recorded_reason`` / ``reason_code``
        The strategy's own reason string verbatim, plus a stable code
        (``short_return_series`` / ``fixture_mode`` / ``None``) so a frontend can
        classify without matching prose.
    ``min_returns_for_gate``
        The observation count the gate needs before it can score anything — the
        number behind ``short_return_series``, carried so no surface invents one.
    ``checks``
        Every leg, in gate order, each ``pass`` / ``fail`` / ``not_computed``
        with the number and the threshold that decided it.
    ``unattributed``
        True when the row did not pass, carries no recorded reason, and no check
        fails the bar — the surface must then say so, not guess.
    """
    if not isinstance(verdict, dict):
        return None

    profile = get_profile(STRICTEST_LEVEL)
    raw_reason = verdict.get("reason")
    recorded_reason = raw_reason.strip() if isinstance(raw_reason, str) else ""

    checks = [
        _dsr_check(verdict, profile),
        _pbo_check(verdict, profile),
        _oos_floor_check(verdict),
        _oos_cliff_check(verdict, profile),
        _look_ahead_check(verdict, has_recorded_reason=bool(recorded_reason)),
    ]
    # A verdict that carries its own ``reason`` was NEVER GRADED — the pipeline
    # writes that string exactly for the branches where the gate did not run
    # (too-short series, fixture mode, debate abstain). Numbers left on such a
    # blob are defaults, not measurements: ``_patch_pbo``
    # (agents/generation_pipeline.py:660) stamps ``pbo = 0.0  # PBO undefined
    # for N<2`` onto every one of those candidates (they are all
    # ``has_real_rigor=False``, so they are all in its ``agent_cands``), and
    # patches ``0.0`` in for any agent candidate missing from the CSCV map even
    # when N≥2. Reporting that sentinel as "PBO 0.00 < 0.50 required — passed"
    # claims an overfitting evaluation that never happened, which is the exact
    # class of overclaim this module exists to remove. The look-ahead leg is
    # excluded because ``_look_ahead_check`` already handles the recorded-reason
    # case with its own wording.
    if recorded_reason:
        for c in checks:
            if c["key"] != "look_ahead" and c["status"] != NOT_COMPUTED:
                c.update(status=NOT_COMPUTED, detail="not scored — see the reason on record", value=None)

    passing = bool(verdict.get("passing"))
    any_failed = any(c["status"] == FAIL for c in checks)

    return {
        "bar": BAR_NAME,
        "bar_level": profile.level,
        "passing": passing,
        "recorded_reason": recorded_reason or None,
        "reason_code": _REASON_CODES.get(recorded_reason),
        "min_returns_for_gate": MIN_RETURNS_FOR_GATE,
        "checks": checks,
        "unattributed": bool(not passing and not recorded_reason and not any_failed),
    }
