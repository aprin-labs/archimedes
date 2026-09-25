"""Live rigor-gate badge — the single source of truth for ``passes_rigor_gate``.

Issue #821. The user-facing library badge (``passes_rigor_gate`` and the
CANDIDATE → VALIDATED 🏆 promotion) must come from a LIVE ``run_rigor_gate``
verdict computed on the strategy's *persisted real returns*, NOT from a stored
fixture boolean. A cached boolean that the gate never re-derives is exactly the
pattern the #1 rule forbids ("claims must be true on the live path") and the one
Bogdan's audit (#710) flagged.

This module reuses the SAME machinery the ``/api/selection-bias/gate`` route
uses — ``get_all_daily_returns`` (real persisted returns from the DB) +
``run_rigor_gate`` (the four-primitive gate) — so the library list and the gate
route share one source of truth. It deliberately does NOT synthesize returns
from stubs: a strategy with no real backtest data surfaces an explicit
``pending`` verdict, never a fixture ``True``/``False``.

Four-state result (``RigorGateVerdict``):
  - ``"pass"``       — real returns exist and the live gate passed.
  - ``"fail"``       — real returns exist, are non-degenerate, and the live gate
                        failed at ≥1 criterion.
  - ``"pending"``    — no real returns yet (genuinely pre-backtest): the gate
                        cannot run, so the badge is honestly "unknown", not a
                        boolean.
  - ``"degenerate"`` — real returns exist but are a mathematically constant
                        (zero-variance) series — broken data or a zero-trade
                        backtest, not a legitimate DSR/OOS input (#1184). Kept
                        distinct from both ``"pending"`` (which would wrongly
                        say "not evaluated yet" for data that WAS evaluated and
                        found broken) and ``"fail"`` (which would wrongly say
                        "evaluated and statistically weak" for data that was
                        never a legitimate gate input at all).

Cheap-by-default: ``passes`` is ``True`` only for ``"pass"``; ``"pending"``,
``"fail"``, and ``"degenerate"`` all map to ``passes is False`` so existing
boolean consumers stay fail-closed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Mirror the selection-bias /gate route: a strategy needs at least this many real
# daily returns before the gate can run. Fewer than this → "pending" (the same
# "no backtest data" branch the route reports as MISSING), never a fixture value.
_MIN_RETURNS_FOR_GATE = 10

# Literal verdict labels for the four-state badge.
PASS = "pass"
FAIL = "fail"
PENDING = "pending"
DEGENERATE = "degenerate"  # #1184: zero-variance persisted series — broken data, distinct from PENDING/FAIL.


def _default_num_trials() -> int:
    """Self-contained default selection-set size = 1 (decouple #2, Dan's principle).

    A strategy's rigor depends ONLY on itself, never on how many OTHER strategies
    happen to sit in the curated library. With no explicit caller-supplied trial
    count, a single strategy's own selection set is one trial — NOT the library
    size (the prior #902 behavior, which deflated every strategy by the count of
    strategies with enough persisted returns to grade). Callers holding real
    cohort context of their OWN (e.g. a strategy's own N-candidate generation
    pool, or its own parameter-variant grid) still pass ``num_trials`` explicitly.
    """
    return 1


@dataclass(frozen=True)
class RigorGateVerdict:
    """A live rigor-gate verdict for one strategy.

    ``status`` is the four-state label ("pass" | "fail" | "pending" |
    "degenerate"). ``passes`` is the fail-closed boolean for legacy consumers:
    only "pass" is truthy — and it is the BADGE verdict, always evaluated at the
    strictest level (the Archimedes Verified bar), never a user's slider level.

    ``min_passing_level`` and ``blocked_by_floor`` carry the strictness-ladder
    info (rigor_profiles) derived from the SAME single gate computation, so a
    per-user deploy gate and the passport can answer "does this pass at level L?"
    without recomputing: deployable at level L iff ``min_passing_level is not None
    and min_passing_level <= L``. ``blocked_by_floor`` means an always-on
    correctness floor failed — the strategy can never deploy at any level.

    ``source`` records provenance — always "live_gate" or "pending" here, NEVER
    "fixture" (that is the whole point of #821).
    """

    status: str
    passes: bool
    source: str
    min_passing_level: int | None = None
    blocked_by_floor: bool = False

    @classmethod
    def pending(cls) -> RigorGateVerdict:
        return cls(status=PENDING, passes=False, source="pending", min_passing_level=None, blocked_by_floor=False)

    @classmethod
    def passed(cls) -> RigorGateVerdict:
        # Badge pass ⟹ passes at the strictest level ⟹ min_passing_level == 1.
        return cls(status=PASS, passes=True, source="live_gate", min_passing_level=1, blocked_by_floor=False)

    @classmethod
    def failed(cls) -> RigorGateVerdict:
        return cls(status=FAIL, passes=False, source="live_gate", min_passing_level=None, blocked_by_floor=False)

    @classmethod
    def degenerate(cls) -> RigorGateVerdict:
        """#1184: a zero-variance persisted return series — never deployable at
        any strictness level (the data itself is broken or the backtest placed
        zero trades), and distinct from both PENDING ("not evaluated yet") and
        FAIL ("evaluated, statistically weak")."""
        return cls(status=DEGENERATE, passes=False, source="live_gate", min_passing_level=None, blocked_by_floor=True)

    @classmethod
    def from_result(cls, result) -> RigorGateVerdict:
        """Build the badge verdict from a computed ``RigorGateResult``.

        ``passes`` is the badge (strictest-level) verdict; ``min_passing_level``
        and ``blocked_by_floor`` are read off the same result so the whole
        strictness ladder is available from one computation.

        #1184: a degenerate (zero-variance) input is checked FIRST and short-
        circuits straight to the ``degenerate()`` verdict — ``result.passes_all``
        would already be ``False`` for it (via ``blocked_by_floor``), but folding
        it into the ordinary PASS/FAIL branch would misreport "the data is
        broken" as "the strategy was graded and lost." Reads the plain
        ``is_degenerate`` attribute rather than ``result.tri_state_status``
        (rigor_evaluator.py) so this stays constructible from any object
        exposing ``is_degenerate``/``passes_all``/``min_passing_level``/
        ``blocked_by_floor`` — including the ``MagicMock`` doubles other test
        suites build — without also having to stub a ``tri_state_status``
        property on every one of them.
        """
        if getattr(result, "is_degenerate", False):
            return cls.degenerate()
        passes = result.passes_all  # run_rigor_gate defaults to the strictest profile
        return cls(
            status=PASS if passes else FAIL,
            passes=passes,
            source="live_gate",
            min_passing_level=result.min_passing_level,
            blocked_by_floor=result.blocked_by_floor,
        )


def verdict_from_returns(
    strategy_id: str,
    daily_returns: list[float],
    *,
    num_trials: int | None = None,
    pbo_scores: dict[str, float] | None = None,
    strategy_code: str | None = None,
    paper_claimed_sharpe: float | None = None,
    average_correlation: float = 0.0,
    look_ahead_audit_passed: bool | None = None,
    look_ahead_status: str | None = None,
    look_ahead_not_run_reason: str | None = None,
) -> RigorGateVerdict:
    """Compute the live four-state verdict from a strategy's persisted returns.

    Reuses ``run_rigor_gate`` — the exact gate the ``/gate`` route runs — rather
    than reinventing it. With fewer than ``_MIN_RETURNS_FOR_GATE`` real returns the
    gate cannot run and the verdict is ``pending`` (NOT a fixture boolean). Any
    unexpected failure inside the gate fails closed to ``pending`` so the badge can
    never claim a pass it did not earn.

    ``num_trials=None`` (the default) derives a self-contained ``1`` via
    ``_default_num_trials()`` — a strategy is graded on its own selection set,
    never deflated by the curated library's count (decouple #2, reverses the
    #902 library-wide-count default). Callers holding a real selection-set of
    their OWN (a strategy's own N-candidate generation pool, or its own
    parameter-variant grid) still pass ``num_trials`` explicitly.

    ``look_ahead_audit_passed=None`` (the default) leaves the look-ahead leg to
    ``run_rigor_gate``'s own AST-audit-over-``strategy_code`` path — the right
    behavior for curated strategies (``verdicts_for_strategies``, which passes
    real cited source). Callers whose "code" is a closed DSL spec rather than
    inspectable source (the fusion/DSL-generation path) have nothing for the AST
    audit to run against, so ``strategy_code=None`` there would otherwise force
    ``look_ahead_passed=False`` unconditionally — failing the always-on
    look-ahead floor (``blocked_by_floor=True``) at every strictness level
    regardless of DSR/PBO/OOS.

    What that path passes here is a REAL audit result, not a declaration.
    ``services.dsl_lookahead_audit`` proves (by AST over the interpreter, plus a
    structural walk of the validated spec, plus the broker cheat-on-close/open
    check) that the strategy reads only bar ``t`` and earlier; only its ``pass``
    state arrives here as ``True``. Its ``pending`` and ``degenerate`` states
    arrive as ``False`` and fail this floor, which is the point: an audit that
    reached no verdict is not evidence. There is no ``spec.look_ahead_safe`` to
    pass into this parameter by mistake — that field was deleted from the DSL
    precisely because "the spec says it is safe" is not evidence.

    ``look_ahead_status`` and ``look_ahead_not_run_reason`` are the
    honest-rendering half of that, and they change NOTHING about admission: the
    floor still fails, the strategy still cannot deploy. They only stop
    ``gate_details["look_ahead"]`` from telling the user their strategy FAILED an
    audit that never reached a verdict — the status says which non-verdict it was
    (``pending`` / ``degenerate``), the reason says why (see
    ``dsl_lookahead_audit.DslLookAheadAudit.not_run_reason``).
    """
    if not daily_returns or len(daily_returns) < _MIN_RETURNS_FOR_GATE:
        return RigorGateVerdict.pending()

    if num_trials is None:
        num_trials = _default_num_trials()

    # Local import keeps this module importable without pulling the full rigor stack
    # at API import time, and avoids any import cycle with rigor_evaluator.
    from archimedes.services.rigor_evaluator import run_rigor_gate

    try:
        # in_sample_sharpe=None on purpose: run_rigor_gate derives the IS denominator
        # from the first 70% of the same series, identical to the /gate route. Passing
        # a full-sample Sharpe would make the OOS/IS cliff trivially passable.
        result = run_rigor_gate(
            strategy_id=strategy_id,
            daily_returns=daily_returns,
            num_trials=num_trials,
            pbo_scores=pbo_scores,
            strategy_code=strategy_code,
            in_sample_sharpe=None,
            paper_claimed_sharpe=paper_claimed_sharpe,
            average_correlation=average_correlation,
            look_ahead_audit_passed=look_ahead_audit_passed,
            look_ahead_status=look_ahead_status,
            look_ahead_not_run_reason=look_ahead_not_run_reason,
        )
    except Exception as exc:  # never let the badge crash the library list
        logger.warning("live rigor gate failed for %s (badge → pending): %s", strategy_id, exc)
        return RigorGateVerdict.pending()

    # from_result carries the badge pass AND the strictness-ladder info
    # (min_passing_level / blocked_by_floor) from one computation.
    return RigorGateVerdict.from_result(result)


def verdicts_for_strategies(strategies: list) -> dict[str, RigorGateVerdict]:
    """Compute live verdicts for a batch of strategies from persisted DB returns.

    Single source of truth for the library-list badge (#821). Mirrors the
    ``/api/selection-bias/gate`` route's data path:
      * load real daily returns from the DB (``get_all_daily_returns``);
      * compute cohort PBO + avg-correlation from the strategies that actually
        HAVE returns (≥10 obs) — ``num_trials`` is self-contained (1 per
        strategy, decouple #2) and does NOT come from this cohort;
      * run ``run_rigor_gate`` per strategy and map the result to the four-state badge
        (``RigorGateVerdict.from_result`` — degenerate check first, #1184).

    Strategies with no real returns map to ``pending`` — never a fixture boolean.
    Returns ``{strategy_id: RigorGateVerdict}`` for every input strategy. Any DB or
    gate failure degrades the whole batch to ``pending`` (fail-closed): the badge
    is never allowed to fabricate a pass.
    """
    if not strategies:
        return {}

    strategy_ids = [s.id for s in strategies]

    try:
        from archimedes.db import get_session
        from archimedes.services.backtest_repository import get_all_daily_returns
        from archimedes.services.rigor_evaluator import (
            assert_self_contained_cohort_correlation,
            compute_average_pairwise_correlation,
            compute_pbo,
        )

        with get_session() as session:
            returns_by_strategy = get_all_daily_returns(session, strategy_ids)
    except Exception as exc:
        logger.warning("live rigor gate batch: DB read failed (all → pending): %s", exc)
        return {sid: RigorGateVerdict.pending() for sid in strategy_ids}

    # Strategies WITHOUT real returns are pending; do NOT synthesize from stubs
    # (that is the circular validation the /gate route explicitly refuses).
    # TODO(A7): cohort filter here diverges from the curated grading job's cohort
    # (curated_grading.grade_cohort also excludes zero-variance series) — see
    # docs/sprint/cluster-4-strategies-route.md. This function now backs the vault
    # deploy gate only; the library badge is the stored verdict (#1746 / PR-B).
    valid_returns = {k: v for k, v in returns_by_strategy.items() if len(v) >= _MIN_RETURNS_FOR_GATE}

    # num_trials = 1: each strategy is graded on ITS OWN Sharpe, NOT deflated by
    # how many OTHER strategies sit in the library (Dan's principle, decouple #2,
    # mirrors selection_bias_routes.evaluate_rigor_gate). PBO and avg_correlation
    # stay cohort-wide (unchanged, out of scope for this decouple) — only
    # num_trials is self-contained. Wrap the cohort-context compute in the same
    # fail-closed contract the docstring promises: if compute_pbo /
    # compute_average_pairwise_correlation raises, degrade the whole batch to
    # pending rather than 500-ing the library list.
    try:
        pbo_scores = compute_pbo(valid_returns) if len(valid_returns) >= 2 else {}
        num_trials = 1
        avg_correlation = compute_average_pairwise_correlation(valid_returns) if len(valid_returns) >= 2 else 0.0
        # V4 guard (num_trials-provenance audit 2026-08-03): cohort-wide
        # avg_correlation is INERT at num_trials=1 — makes it IMPOSSIBLE for a
        # future edit to silently reintroduce num_trials>1 here without
        # re-coupling every strategy's badge DSR to the library's correlation
        # structure; it raises instead (caught below, same fail-closed
        # contract — a pending badge, never a wrong PASS).
        assert_self_contained_cohort_correlation(num_trials, avg_correlation)
    except Exception as exc:
        logger.warning("live rigor gate batch: cohort-context compute failed (all → pending): %s", exc)
        return {sid: RigorGateVerdict.pending() for sid in strategy_ids}

    # Only load source for strategies that can actually run the gate (≥10 returns).
    # A strategy with too few returns short-circuits to `pending` inside
    # verdict_from_returns and never reads strategy_code, so loading it is wasted I/O.
    code_by_id = {s.id: _load_strategy_code_safe(s) for s in strategies if s.id in valid_returns}

    verdicts: dict[str, RigorGateVerdict] = {}
    for s in strategies:
        daily_returns = returns_by_strategy.get(s.id, [])
        verdicts[s.id] = verdict_from_returns(
            s.id,
            daily_returns,
            num_trials=num_trials,
            pbo_scores=pbo_scores,
            strategy_code=code_by_id.get(s.id),
            paper_claimed_sharpe=getattr(s, "paper_claimed_sharpe", None),
            average_correlation=avg_correlation,
        )
    return verdicts


def _load_strategy_code_safe(strategy) -> str | None:
    """Best-effort read of a strategy's source for the look-ahead audit.

    Reuses the path-traversal-guarded loader from the selection-bias route so the
    library badge runs the same look-ahead audit input as the /gate route. Never
    raises: returns None on any failure (the gate then fails the look-ahead leg).
    """
    code_path = getattr(strategy, "strategy_code_path", None)
    if not code_path:
        return None
    try:
        from archimedes.api.selection_bias_routes import _load_strategy_code

        return _load_strategy_code(code_path)
    except Exception:
        return None
