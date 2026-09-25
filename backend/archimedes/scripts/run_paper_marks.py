"""Paper-marks runner — 15-minute mark-to-market on open paper positions.

Run as a standalone process (the ``run_kb_pipeline.py`` entry-point pattern)::

    python -m archimedes.scripts.run_paper_marks --once      # one tick, exit
    python -m archimedes.scripts.run_paper_marks             # long-lived loop
    python -m archimedes.scripts.run_paper_marks --prune-only # retention only

Env:
    PAPER_MARKS_INTERVAL_MINUTES        tick cadence (default 15)
    PAPER_MARKS_MAX_STALENESS_MINUTES   a bar older than this writes NO row (60)
    PAPER_MARKS_RAW_RETENTION_DAYS      raw marks kept in full (7)
    PAPER_MARKS_HOURLY_RETENTION_DAYS   hourly marks kept, then deleted (90)
    PAPER_MARKS_MAX_ROWS_PER_DEPLOYMENT runaway-loop tripwire (20000)

WHERE THIS IS INTENDED TO RUN — and why it is not wired here
------------------------------------------------------------
Deployment wiring is a SEPARATE infra PR, because a third container on shared
infrastructure needs Dan's ack (design §7, "Decisions Dan owns" #3). This
module is the runnable unit that PR will point at; nothing here changes any
running process on its own.

The recommendation (§4.2) is the **runner EC2 box** (``infra/runner_ec2.tf``,
a single ``t3.small``): a third ``docker run`` unit in
``infra/runner-user-data.sh`` beside ``oracle_runner`` and ``agent_runner``,
off the SAME ``archimedes-backend`` image — no new image, no new secret,
**marginal infra cost $0** on an existing, under-used box that already
reaches Aurora and ElastiCache from the private subnets and already proves
this exact loop shape at a 60-second cadence.

The alternatives, and why not:

  - **Web tier** (Fargate, where ``paper_advance_loop`` lives): the task is
    ``ecs_backend_cpu = "1024"`` — ONE vCPU shared between nginx and the
    backend container, of which generation already takes ~65% for ~48s per
    run (#1411; it is why admission control #1408 exists). Adding an
    always-on polling loop to the most contended resource in the system is
    the wrong direction.
  - **#1411's Lambda offload target**: that spike is about a bursty
    48-second batch job priced per invocation. A 15-minute always-on poll is
    the opposite shape — low duty cycle, long lifetime, VPC-bound.
  - **Scheduled Fargate** (the ``infra/kb_runner.tf`` + EventBridge pattern):
    a real fallback with a working in-repo precedent if the runner box is
    ever retired. Costs more than $0 and adds cold-start latency per tick.

A cron/EventBridge schedule can drive ``--once`` instead of the built-in loop
if the operator prefers a scheduler to a long-lived process; both are
supported, and ``--once`` is the shape a cron entry wants::

    */15 * * * *  python -m archimedes.scripts.run_paper_marks --once
    17   4 * * *  python -m archimedes.scripts.run_paper_marks --prune-only

ON THE LEASE
------------
Not taken here, and that is deliberate rather than an oversight. Marks are
**not funds-adjacent**: no money moves, and ``uq_paper_marks_dep_ts_gran``
makes a duplicate insert a no-op. The honest reason to add
``RunnerLeaseGuard`` when this is wired to the runner box is that it stops two
copies from burning double the vendor quota during a deploy overlap — that
sentence belongs in the infra PR alongside the container, not a copy of
``oracle_runner``'s funds-adjacent language, because a false safety claim is a
defect even when the mechanism is identical.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import UTC, datetime

logger = logging.getLogger(__name__)


def run_once(*, prune: bool = False) -> dict:
    """One tick: mark every active deployment, optionally prune afterwards.

    Commits per phase so a prune failure cannot roll back marks that were
    already valid, and vice versa. Returns the merged summary dicts.
    """
    from archimedes.db import get_session, init_db
    from archimedes.services import paper_marks

    init_db()
    summary: dict = {}
    with get_session() as session:
        summary.update(paper_marks.mark_all(session))
        session.commit()
    if prune:
        with get_session() as session:
            summary.update(paper_marks.rollup_and_prune(session))
            session.commit()
    return summary


def run_prune_only() -> dict:
    from archimedes.db import get_session, init_db
    from archimedes.services import paper_marks

    init_db()
    with get_session() as session:
        summary = paper_marks.rollup_and_prune(session)
        session.commit()
    return summary


def run_loop(*, max_ticks: int | None = None, sleep=time.sleep) -> int:
    """Long-lived loop. Fail-soft per tick, same contract as
    ``paper_advance_loop``: a bad cycle logs and retries on the next tick and
    must never take the process down — the vendor endpoint has no SLA, and a
    gap in a decoration is not worth a crash loop.

    The daily rollup runs on the first tick of each new UTC day rather than on
    its own schedule, so there is one process, one clock, and no second thing
    to deploy. ``max_ticks``/``sleep`` are test seams — a loop with no exit is
    not testable, and an untested retention job is how ``backtest_results``
    got to 6.3 GB.
    """
    from archimedes.services import paper_marks

    interval_s = paper_marks.interval_minutes() * 60
    ticks = 0
    last_prune_day = None
    while max_ticks is None or ticks < max_ticks:
        today = datetime.now(UTC).date()
        prune = last_prune_day != today
        try:
            summary = run_once(prune=prune)
            if prune:
                last_prune_day = today
            logger.info("paper marks tick: %s", summary)
        except Exception as exc:
            logger.warning(
                "paper marks: cycle failed (%s: %s) — will retry next tick", type(exc).__name__, exc, exc_info=True
            )
        ticks += 1
        if max_ticks is None or ticks < max_ticks:
            sleep(interval_s)
    return ticks


def main() -> None:
    parser = argparse.ArgumentParser(description="Intraday mark-to-market for paper deployments.")
    parser.add_argument("--once", action="store_true", help="run a single tick and exit (the cron shape)")
    parser.add_argument("--prune-only", action="store_true", help="run the rollup + prune job and exit")
    parser.add_argument("--prune", action="store_true", help="with --once: also run the rollup + prune job")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )

    if args.prune_only:
        print(json.dumps(run_prune_only(), indent=2))
        return
    if args.once:
        print(json.dumps(run_once(prune=args.prune), indent=2))
        return
    run_loop()


if __name__ == "__main__":
    # Standalone entrypoint: load .env into os.environ so DATABASE_URL and the
    # PAPER_MARKS_* knobs resolve under a bare `python -m` with no FastAPI
    # bootstrap and no docker env_file. Mirrors oracle_runner; override=False
    # so an exported env / docker env_file still wins. Under __main__ so
    # importing this module in a test never loads .env.
    from dotenv import load_dotenv

    load_dotenv("../.env", override=False)
    load_dotenv(".env", override=False)
    main()
