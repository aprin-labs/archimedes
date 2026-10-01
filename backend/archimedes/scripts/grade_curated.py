"""Grade the curated library — the standalone, operator-run grading job.

Usage:
  cd backend
  DATABASE_URL=postgresql://... python -m archimedes.scripts.grade_curated

Runs ``services.curated_grading.grade_curated_library`` over the whole curated
library and stores each strategy's rigor verdict of record on its passport row
(``docs/adr/rigor-verdict-of-record.md``).

WHEN YOU RUN THIS
-----------------
1. **The one-time backfill.** Existing curated rows have never been graded —
   before #1746's PR-B nothing wrote a curated verdict at all, so every one of
   them reads ``rigor_gate_status: "pending"``. This run is what gives them a
   real verdict. It is the deploy step for that PR; see
   ``docs/runbooks/curated-backtests.md`` § "Grading on its own".
2. **After a gate change.** New thresholds, a new criterion, a bumped
   ``GATE_CODE_REVISION`` — the stored verdicts now name a gate that no longer
   exists. Re-grading is an explicit, versioned event, which is exactly what
   this script is.

You do NOT run it on a clock, and you do not run it to "refresh" a board. A
strategy with no new evidence grades to the same verdict it already has;
``run_backtests`` already grades as part of its own run, so a backtest run does
not need a grading run after it.

WHAT IT DOES NOT DO
-------------------
It never runs a backtest. It grades the returns that are already persisted, so a
strategy with no persisted series grades ``pending`` and stays there until a
backtest produces one. That is the honest surface, and re-running this does not
change it — see ``curated-backtests.md`` § "When to run it" on the pairs
family.

It is also never called from the serving process. ``backend/tests/
test_curated_grading_is_write_side_only.py`` enforces that at the choke point.
"""

from __future__ import annotations

import json
import logging
import sys

from archimedes.db import get_session, init_db
from archimedes.services.curated_grading import grade_curated_library

logger = logging.getLogger(__name__)


def grade_curated() -> dict:
    """Grade the curated library and commit. Returns the run summary."""
    init_db()
    with get_session() as session:
        summary = grade_curated_library(session)
        session.commit()
    return summary.as_dict()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    summary = grade_curated()
    print(json.dumps(summary, indent=2))
    # A run that could not write a single verdict is a failure, not a quiet
    # success: exit non-zero so an operator's `&&` chain and the ECS task's exit
    # code both say so.
    if summary["errors"] and not (summary["graded"] or summary["pending"]):
        sys.exit(1)


if __name__ == "__main__":
    main()
