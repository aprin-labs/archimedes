"""Grading the curated library — the write side of the rigor verdict of record.

**Owner decision (Dan, 2026-09-01, ``docs/adr/rigor-verdict-of-record.md``).**
Generation, backtesting and grading are one-time events. A strategy is graded
ONCE, by the real gate, at backtest time; the verdict is persisted on the
passport with its provenance; every surface reads the stored verdict. A re-grade
is an explicit, versioned event — never a silent overwrite, never a recompute on
read.

PR-A covered generated strategies (``generation_pipeline._refresh_passport_real_metrics``
is their grading event). This module is the curated half — issue #1746's PR-B.

WHAT MOVED, AND WHY IT IS THE SAME COMPUTATION
----------------------------------------------
:func:`grade_cohort` is ``strategies_routes._live_rigor_results_for_strategies``,
lifted verbatim onto the write side. Same cohort rule (the FULL library, #1173),
same zero-variance exclusion from the cohort context (#868), same
self-contained ``num_trials=1`` (decouple #2, ``docs/adr/num-trials-self-containment.md``),
same per-strategy ``run_rigor_gate`` call with the strategy's own source loaded
for the look-ahead audit.

Two things did NOT come with it:

* **The memo.** ``services.rigor_cache`` existed to make a per-request cohort
  recompute affordable (~6s per Library page). A grade that runs when a person
  runs a backtest needs no cache — and a cached verdict was always the wrong
  shape for a stored one.
* **The read path.** Nothing under ``backend/archimedes/`` outside the two
  operator scripts may call :func:`grade_curated_library`. That is enforced by
  ``backend/tests/test_curated_grading_is_write_side_only.py``, the same choke-
  point shape ``test_backtests_are_frozen.py`` uses for ``run_backtests`` — so
  renaming the loop does not get past it.

WHEN IT RUNS
------------
``backend/archimedes/scripts/run_backtests.py`` calls it at the end of a curated
backtest run: new evidence, new grade, one job. ``python -m
archimedes.scripts.grade_curated`` runs it on its own, which is how existing
rows get their first real verdict (the one-time backfill; see
``docs/runbooks/curated-backtests.md`` § "Grading on its own").

WHAT A GRADE WRITES
-------------------
One ``ingest_passport(..., rigor_verdict=RigorVerdictWrite(...))`` call per
strategy — the single writer of the verdict columns. It carries:

* the four-state ``rigor_gate_status`` and its fail-closed boolean,
* ``graded_at`` / ``gate_version`` (filled by ``RigorVerdictWrite`` itself),
* ``cohort_n`` — how many return series supplied the cohort-scoped inputs
  (PBO, average pairwise correlation) for THIS grade,
* the four numbers the same gate run produced (DSR, DSR p-value, PBO, OOS
  Sharpe), so the badge and the numbers beside it can never come from two
  different gate runs.

A strategy with fewer than ``_MIN_RETURNS_FOR_GATE`` persisted daily returns is
graded ``pending`` with no numbers and no cohort — the honest state of a
strategy whose backtest has not produced a gradeable series. The pairs family is
the standing example: ``run_backtests`` refuses to persist its artifact
(realized-vol plausibility), so it has no returns, so it stays ``pending``
forever until the code is fixed. That is fail-closed working as designed.

Owner: Dan Browne. Math lane: Önder Akkaya.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from archimedes.services.live_rigor_gate import _MIN_RETURNS_FOR_GATE, RigorGateVerdict

logger = logging.getLogger(__name__)


def verdict_from_result(result) -> RigorGateVerdict:
    """Reduce an already-computed ``RigorGateResult`` to the four-state verdict.

    ``None`` — the gate could not run for this strategy — is ``pending``, never a
    ``fail``: "we have not graded this" and "we graded this and it lost" are
    different claims, and #1184 is what happens when a surface conflates them.

    Delegates to ``RigorGateVerdict.from_result`` (rather than hand-rolling
    ``passed()``/``failed()`` off ``passes_all``) so a zero-variance persisted
    series reports the distinct ``degenerate`` status here too — the two
    badge-producing call sites cannot drift apart on that check.
    """
    if result is None:
        return RigorGateVerdict.pending()
    return RigorGateVerdict.from_result(result)


@dataclass(frozen=True)
class CohortGrade:
    """One cohort gate run: a result per gradeable strategy, plus the cohort size.

    ``cohort_n`` is the number of return series that supplied the cohort-scoped
    inputs (PBO, average pairwise correlation) — the survivors of the
    zero-variance filter. It is stored on each graded passport so a reader can
    tell which cohort a verdict was produced against; ``cohort_n=1`` on a
    degenerate row records that the row was graded against itself alone (its
    series was excluded from the cohort context so it could not dilute
    ``avg_correlation`` for everyone else, #868).

    ``ok`` separates the two ways ``results`` can be empty, and the distinction
    is load-bearing:

    * ``ok=True`` with an empty ``results`` — the run reached the data and found
      nothing gradeable. Every strategy is honestly ``pending``, and writing that
      is correct.
    * ``ok=False`` — the run could **not reach the data** (the returns read
      failed, or the cohort context could not be computed). Nothing was graded
      and nothing is known. Writing ``pending`` here would replace a real stored
      verdict with "never graded" on every row and wipe its ``graded_at`` — the
      silent overwrite of the verdict of record that
      ``docs/adr/rigor-verdict-of-record.md`` forbids outright. The caller
      (:func:`grade_curated_library`) therefore writes NOTHING and reports the
      failure, so the operator scripts exit non-zero instead of printing a
      success summary over a destroyed table.

    ``unavailable_reason`` carries the exception text into that report.
    """

    results: dict = field(default_factory=dict)
    cohort_n: int = 0
    degenerate_ids: frozenset = frozenset()
    ok: bool = True
    unavailable_reason: str | None = None


def _load_strategy_code_safe(strategy) -> str | None:
    """Best-effort read of a strategy's source for the look-ahead audit.

    Never raises: ``None`` on any failure, which makes the gate's look-ahead leg
    fail (fail-closed) rather than crash the run.
    """
    code_path = getattr(strategy, "strategy_code_path", None)
    if not code_path:
        return None
    try:
        from archimedes.api.selection_bias_routes import _load_strategy_code

        return _load_strategy_code(code_path)
    except Exception:
        return None


def grade_cohort(strategies: list) -> CohortGrade:
    """Run the real rigor gate over a cohort of strategies, once.

    Mirrors ``GET /api/selection-bias/gate``'s data path exactly: real persisted
    returns from the DB, zero-variance series excluded from the cohort context
    before they can dilute ``avg_correlation`` (#868), cohort PBO +
    average-correlation over the survivors, one ``run_rigor_gate`` call per
    strategy. ``num_trials`` is self-contained (1 per strategy) and does NOT come
    from this cohort.

    Strategies with fewer than 10 persisted returns are simply absent from
    ``results`` — the caller grades those ``pending``. A degenerate
    (zero-variance) series IS graded and included; it just runs with
    self-contained cohort context.

    A DB or cohort-context failure does not raise; it returns ``ok=False`` with
    the reason. That is NOT the same as an empty grade: a run that cannot reach
    the data knows nothing, so its caller must write nothing (see
    :class:`CohortGrade`). A run that reached the data and found no gradeable
    series returns ``ok=True`` and every strategy is honestly ``pending``.
    """
    if not strategies:
        return CohortGrade()

    strategy_ids = [s.id for s in strategies]

    try:
        from archimedes.db import get_session
        from archimedes.services.backtest_repository import get_all_daily_returns

        with get_session() as session:
            returns_by_strategy = get_all_daily_returns(session, strategy_ids)
    except Exception as exc:
        logger.error("curated grading: DB read failed — nothing will be written: %s", exc)
        return CohortGrade(ok=False, unavailable_reason=f"persisted returns unreadable: {type(exc).__name__}: {exc}")

    from archimedes.services.rigor_evaluator import (
        assert_self_contained_cohort_correlation,
        compute_average_pairwise_correlation,
        compute_pbo,
        run_rigor_gate,
    )

    # Exclude zero-variance (degenerate/placeholder-flat) series from the cohort
    # context — same filter as selection_bias_routes.py (#868), so the cohort
    # this grade was produced against matches that route's exactly.
    valid_returns = {
        k: v
        for k, v in returns_by_strategy.items()
        if len(v) >= _MIN_RETURNS_FOR_GATE and float(np.ptp(np.asarray(v, dtype=float))) > 0.0
    }

    try:
        pbo_scores = compute_pbo(valid_returns) if len(valid_returns) >= 2 else {}
        # num_trials = 1: each strategy is graded on ITS OWN Sharpe, never
        # deflated by how many OTHER strategies sit in the library (decouple #2).
        # PBO/avg_correlation stay cohort-wide.
        num_trials = 1
        avg_correlation = compute_average_pairwise_correlation(valid_returns) if len(valid_returns) >= 2 else 0.0
        # V4 guard (num_trials-provenance audit 2026-08-03): cohort-wide
        # avg_correlation is INERT at num_trials=1 (E[max_N]=0 when N==1) — this
        # makes it impossible for a future edit to silently reintroduce
        # num_trials>1 here without re-coupling every strategy's DSR to the
        # library's correlation structure; it raises instead.
        assert_self_contained_cohort_correlation(num_trials, avg_correlation)
    except Exception as exc:
        logger.error("curated grading: cohort-context compute failed — nothing will be written: %s", exc)
        return CohortGrade(ok=False, unavailable_reason=f"cohort context uncomputable: {type(exc).__name__}: {exc}")

    # Load source only for strategies that can actually run the gate. Degenerate
    # series (excluded from valid_returns) still run their own gate (#868) and
    # need the look-ahead audit input.
    code_by_id = {
        s.id: _load_strategy_code_safe(s)
        for s in strategies
        if len(returns_by_strategy.get(s.id, [])) >= _MIN_RETURNS_FOR_GATE
    }

    computed: dict = {}
    degenerate_ids: set[str] = set()
    for s in strategies:
        daily_returns = returns_by_strategy.get(s.id, [])
        if len(daily_returns) < _MIN_RETURNS_FOR_GATE:
            continue  # graded `pending` by the caller — never a fabricated number
        if s.id in valid_returns:
            gate_kwargs: dict = {
                "num_trials": num_trials,
                "pbo_scores": pbo_scores,
                "average_correlation": avg_correlation,
            }
        else:
            # Degenerate (zero-variance) series: self-contained cohort context so
            # it cannot dilute avg_correlation for the rest (#868). It still runs
            # its own gate, so the stored verdict is `degenerate` (#1184) rather
            # than a silent absence.
            degenerate_ids.add(s.id)
            gate_kwargs = {"num_trials": 1, "pbo_scores": {}, "average_correlation": 0.0}
        try:
            computed[s.id] = run_rigor_gate(
                strategy_id=s.id,
                daily_returns=daily_returns,
                strategy_code=code_by_id.get(s.id),
                in_sample_sharpe=None,
                paper_claimed_sharpe=getattr(s, "paper_claimed_sharpe", None),
                **gate_kwargs,
            )
        except Exception as exc:
            logger.warning("curated grading: gate failed for %s (→ pending): %s", s.id, exc)

    return CohortGrade(
        results=computed,
        cohort_n=len(valid_returns),
        degenerate_ids=frozenset(degenerate_ids),
    )


@dataclass(frozen=True)
class CuratedGradeSummary:
    """What one grading run did, per four-state outcome. Logged and returned."""

    graded: int = 0
    pending: int = 0
    counts: dict = field(default_factory=dict)
    errors: dict = field(default_factory=dict)

    @property
    def wrote_nothing(self) -> bool:
        """No verdict of any kind reached a passport row in this run."""
        return not (self.graded or self.pending)

    def as_dict(self) -> dict:
        """The summary an operator reads, JSON-safe.

        A run that wrote nothing AND recorded an error also carries an
        ``"error"`` key. That is the shape ``docs/runbooks/curated-backtests.md``
        tells the operator to look for in ``run_backtests``'s ``graded`` field,
        and the shape ``scripts/grade_curated.main`` exits non-zero on — so a
        run that could not reach its data cannot present itself as a quiet
        success. A run with SOME failed rows and some written ones keeps only
        ``errors``: it is a partial success, and re-running it is not urgent.
        """
        d = {
            "graded": self.graded,
            "pending": self.pending,
            "counts": dict(self.counts),
            "errors": dict(self.errors),
        }
        if self.errors and self.wrote_nothing:
            d["error"] = "; ".join(f"{k}: {v}" for k, v in sorted(self.errors.items()))
        return d


def grade_curated_library(session, *, provider=None, strategies: list | None = None) -> CuratedGradeSummary:
    """Grade every curated strategy and store the verdict on its passport row.

    THE grading event for the curated library. Call it from an operator-run job
    (``scripts/run_backtests.py`` or ``scripts/grade_curated.py``) and nowhere
    else — a request-time call would be a recompute on read wearing a different
    name, which is exactly what ``docs/adr/rigor-verdict-of-record.md`` forbids
    and what ``test_curated_grading_is_write_side_only.py`` refuses.

    ``session`` is the caller's SQLAlchemy session; this function flushes but
    does NOT commit, so a caller can grade and commit as one unit.

    **A run that cannot reach its data writes NOTHING.** ``grade_cohort``
    reports that as ``ok=False``, and this function returns immediately with a
    populated ``errors`` — it does not fall through to a library-wide
    ``pending`` write, which would erase every stored verdict and its
    ``graded_at`` while reporting success. See :class:`CohortGrade`.

    The cohort is the FULL curated library, always — never a filtered subset
    (#1173). ``provider`` / ``strategies`` exist for tests and for a caller that
    already holds a provider; a ``strategies`` list narrower than the library is
    a deliberately smaller cohort and grades accordingly.
    """
    from archimedes.services.curated_metrics import display_metrics_source, with_display_metrics
    from archimedes.services.passport_loader import RigorVerdictWrite, ingest_passport
    from archimedes.services.strategy_provider import default_provider

    if provider is None:
        provider = default_provider()
    if strategies is None:
        strategies = list(provider.list_strategies())

    cohort = grade_cohort(strategies)
    if not cohort.ok:
        # THE RUN COULD NOT READ ITS DATA. Falling through here would call
        # `verdict_from_result(None)` for every strategy and write
        # `RigorVerdictWrite(status="pending")` — which forces `graded_at`,
        # `gate_version`, `cohort_n` and the four gate numbers to NULL. A real
        # `pass` graded last week would become "never graded", on every row, and
        # the summary would report it as `{'graded': 0, 'pending': 34}` with an
        # empty `errors` — a success. That is the silent overwrite of the verdict
        # of record that docs/adr/rigor-verdict-of-record.md forbids, caused by
        # the one condition (the DB is unreachable) under which we know least.
        #
        # So: write nothing, and say so loudly enough that both operator entry
        # points fail. `wrote_nothing` + a populated `errors` puts an "error" key
        # in the summary, which is what `grade_curated.main` exits 1 on and what
        # `run_backtests` surfaces in its `graded` field.
        reason = cohort.unavailable_reason or "the cohort could not be computed"
        logger.error(
            "curated grading ABORTED — no verdict written for any of %d strategies: %s", len(strategies), reason
        )
        return CuratedGradeSummary(errors={"cohort_unavailable": reason})

    graded = 0
    pending = 0
    counts: dict[str, int] = {}
    errors: dict[str, str] = {}
    for s in strategies:
        result = cohort.results.get(s.id)
        verdict = verdict_from_result(result)
        # cohort_n records the cohort THIS grade was produced against. A
        # degenerate row was graded against itself alone; an ungraded
        # (`pending`) row was graded against nothing at all, and None is the
        # honest answer there — never a guessed 1.
        if result is None:
            cohort_n = None
        elif s.id in cohort.degenerate_ids:
            cohort_n = 1
        else:
            cohort_n = cohort.cohort_n

        bt = provider.get_backtest_result(s.id)
        try:
            ingest_passport(
                session,
                with_display_metrics(s, bt),
                generation_method="curated",
                force_update=True,
                # Same pair, same call — see `_sync_to_unified_table`.
                display_metrics_source=display_metrics_source(s, bt),
                rigor_verdict=RigorVerdictWrite(
                    status=verdict.status,
                    cohort_n=cohort_n,
                    deflated_sharpe_ratio=getattr(result, "deflated_sharpe", None),
                    dsr_p_value=getattr(result, "dsr_p_value", None),
                    pbo_score=getattr(result, "pbo_score", None),
                    out_of_sample_sharpe=getattr(result, "oos_sharpe", None),
                ),
            )
        except Exception as exc:  # one bad row must not abandon the rest
            logger.warning("curated grading: passport write failed for %s: %s", s.id, exc)
            errors[s.id] = f"{type(exc).__name__}: {exc}"
            continue

        counts[verdict.status] = counts.get(verdict.status, 0) + 1
        if result is None:
            pending += 1
        else:
            graded += 1

    summary = CuratedGradeSummary(graded=graded, pending=pending, counts=counts, errors=errors)
    logger.info("curated grading summary: %s", summary.as_dict())
    return summary
