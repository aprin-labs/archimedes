"""V4 regression guard: num_trials-provenance audit, 2026-08-03.

Three cohort-computing call sites — ``selection_bias_routes.evaluate_rigor_gate``,
``curated_grading.grade_cohort`` (the curated grading job, which is where the
library's cohort computation moved in #1746 / PR-B), and
``live_rigor_gate.verdicts_for_strategies`` — each compute a COHORT-WIDE
``average_correlation`` across every curated strategy with enough persisted
returns, then grade each strategy at ``num_trials=1`` (Dan's decouple #2
principle). At ``num_trials=1`` that correlation is INERT by construction
(``_dsr_from_stats``: ``E[max_N] == 0`` when ``N == 1``), so today it changes
nothing. The risk is a FUTURE edit at one of those three sites reintroducing
``num_trials > 1`` while leaving the cohort-wide correlation wired in —
silently re-coupling every curated strategy's DSR to the library's
correlation structure again, exactly what the decision forbids outside
Leaderboard/Marketplace.

``assert_self_contained_cohort_correlation`` makes that combination
IMPOSSIBLE (raises ``RuntimeError``) rather than merely unlikely. These tests:
  1. Prove the guard actually rejects the forbidden input (adversarial pass).
  2. Prove it does NOT fire on the legitimate ``num_trials=1`` case (today's
     real usage) or on a genuinely self-contained ``num_trials>1`` case with
     its OWN (non-cohort) correlation of 0.0 (e.g. an uncorrelated
     parameter-variant grid — the documented legitimate exception).
  3. Prove all three call sites are actually WIRED to call the guard, not
     just that the guard itself is correct in isolation (the same
     same-literal-both-sides risk V1's fix guarded against: a helper being
     correct doesn't prove its caller still uses it).
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest
from archimedes.services.rigor_evaluator import assert_self_contained_cohort_correlation


# ── 1 & 2. The guard itself ──────────────────────────────────────────────────


def test_guard_rejects_num_trials_above_one_with_nonzero_cohort_correlation():
    """Adversarial: this is EXACTLY the shape a reintroduced library-size bug
    would produce at one of the three cohort call sites — num_trials pulled
    from cohort size (>1) alongside that same cohort's average_correlation.
    Before this guard existed, this combination would silently compute a
    materially different (wrong) DSR with nothing to catch it."""
    with pytest.raises(RuntimeError, match="re-couples"):
        assert_self_contained_cohort_correlation(25, 0.31)

    with pytest.raises(RuntimeError):
        assert_self_contained_cohort_correlation(2, 1e-9)  # any nonzero correlation trips it


def test_guard_allows_the_real_num_trials_one_cohort_case():
    """Today's actual usage at all three call sites: num_trials=1 with a
    real, nonzero cohort-wide correlation. Must never raise — this is the
    documented-safe combination (E[max_N]=0 makes the correlation inert)."""
    assert assert_self_contained_cohort_correlation(1, 0.73) is None
    assert assert_self_contained_cohort_correlation(1, 0.0) is None


def test_guard_allows_genuinely_self_contained_multi_trial_with_zero_correlation():
    """A strategy's OWN N-candidate generation pool or parameter-variant grid
    legitimately passes num_trials>1 — the guard must not block that, only
    the COMBINATION with a nonzero (cohort) correlation."""
    assert assert_self_contained_cohort_correlation(35, 0.0) is None


# ── 3. Wiring: all three call sites actually invoke the guard ──────────────

_GUARD = "assert_self_contained_cohort_correlation"


def _invokes(func: object, target: str = _GUARD) -> bool:
    """True only if ``func``'s own AST contains a real invocation of ``target``.

    Deliberately NOT ``target in inspect.getsource(func)``. A substring check
    passes on any occurrence of the name — including the one left behind in a
    comment or docstring after the actual call was deleted. That is precisely
    how a wiring guard stops guarding while still reporting green, so the guard
    on the guard has to inspect structure, not text. Matches both ``guard(...)``
    and ``mod.guard(...)`` forms.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    return any(
        isinstance(node, ast.Call)
        and (getattr(node.func, "id", None) == target or getattr(node.func, "attr", None) == target)
        for node in ast.walk(tree)
    )


def test_the_wiring_check_itself_discriminates():
    """Adversarial pass on our own guard-checker, per the pre-merge conventions:
    a check is only worth trusting once it has been shown to REJECT something.
    A function that merely mentions the guard's name in a comment and a string
    -- but never calls it -- must NOT be reported as wired."""

    def _mentions_but_never_calls():
        # assert_self_contained_cohort_correlation(num_trials, corr)
        return "assert_self_contained_cohort_correlation("

    assert _invokes(_mentions_but_never_calls) is False

    def _actually_calls():
        return assert_self_contained_cohort_correlation(1, 0.0)

    assert _invokes(_actually_calls) is True


def test_selection_bias_routes_evaluate_rigor_gate_calls_the_guard():
    from archimedes.api.selection_bias_routes import evaluate_rigor_gate

    assert _invokes(evaluate_rigor_gate)


def test_the_curated_grading_cohort_calls_the_guard():
    from archimedes.services.curated_grading import grade_cohort

    assert _invokes(grade_cohort)


def test_live_rigor_gate_verdicts_for_strategies_calls_the_guard():
    from archimedes.services.live_rigor_gate import verdicts_for_strategies

    assert _invokes(verdicts_for_strategies)
