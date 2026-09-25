"""Run ONE generation job outside the serving process (spike, issue #1411).

Today a generation runs *inside* the web task: ``generate_routes.py`` fires
``asyncio.create_task(_run_with_cleanup(...))`` on the serving event loop, so
the ~48s of debate + backtest compute is spent on the same 1-vCPU Fargate task
that answers HTTP. This module is the entrypoint that lets that same work run
somewhere else — a Lambda container invocation, an ``ecs:RunTask`` one-off, or a
plain ``python -m`` on a box — **without forking the pipeline**. It calls the
production :func:`archimedes.agents.generation_pipeline.run_generation` with the
production job store, so:

* events still land in the SAME Redis event log the SSE route reads, which is
  what keeps ``GET /api/generate/stream`` unchanged by an offload;
* persistence, the rigor gate, the cost meter and the credit ledger all run on
  their normal code paths — nothing here re-implements a pipeline step;
* a run that ends without delivering hands back **both** entitlements the
  enqueue can spend — the paid generation credit and the free-tier slot —
  through the same ``generate_routes.release_entitlements_if_undelivered`` the
  serving path calls (#1793). This entrypoint used to skip that cleanup
  entirely, because it lived inside ``_run_with_cleanup``'s ``finally``; the
  free slot (#1785) was written into that same ``finally`` and inherited the
  same gap, which is why both refunds now sit behind one seam.

Nothing in the serving path imports this module. It is an *entrypoint*, peer to
``run_kb_pipeline.py``: the offload decision itself is recorded in
``docs/adr/lambda-generation-offload.md`` and is not enabled by this file
existing.

**Startup ordering is load-bearing, and ordering alone is not enough.**
``job_queue.REDIS_URL`` is read at *module import* time, so the environment has
to be complete before the pipeline modules are imported — the sequence
``main.py`` performs for the web process (``.env`` → SSM-when-production → then
route/service imports). :func:`bootstrap_environment` reproduces that ordering
and every heavy import in this file is deferred inside a function so importing
this module cannot beat it.

That is still not sufficient, and the spike proved it on the first real
invocation:

    redis.exceptions.ConnectionError: Error Multiple exceptions:
    [Errno 111] Connect call failed ('127.0.0.1', 6379) …

``archimedes/services/__init__.py`` eagerly re-exports ``generation_pipeline``
for backwards compatibility, so importing *any* ``archimedes.services.*``
module — including ``secrets_service``, the one the bootstrap needs to fetch
the secrets — transitively imports ``job_queue`` and freezes ``REDIS_URL`` at
its ``redis://localhost:6379/0`` default **before** the bootstrap can populate
the environment. No ordering of statements inside this file can win that race.
Production never trips over it because ECS injects ``REDIS_URL`` and
``DATABASE_URL`` as native task-definition secrets, so the module-level read
already sees the right value; a worker that leans on the SSM loader for them
does not have that luxury.

:func:`_bind_job_store` is the fix and :func:`_require_configured_store` is the
guard: the store is constructed from the environment *as it is at run time*,
and a production-shaped worker that cannot find a configured Redis refuses to
start rather than pushing a paid job's events into a local store nobody reads.
A silent localhost bind is precisely the fail-soft-shaped defect this codebase
treats as worse than a crash.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

#: Set once :func:`bootstrap_environment` has run. Idempotent because a warm
#: Lambda execution environment calls the handler many times per process, and
#: re-reading SSM per invocation would add a network round trip to every run.
_BOOTSTRAPPED = False


def bootstrap_environment() -> int:
    """Populate ``os.environ`` the way ``main.py`` does, before pipeline imports.

    Returns the number of SSM parameters loaded (0 when the SSM path is not
    taken). Mirrors ``main.py``'s gate exactly: SSM is read only when
    ``PUBLIC_DOMAIN`` is set, so a local or CI run can never pull production
    secrets even with ambient AWS credentials and a real
    ``AWS_SSM_PATH_PREFIX`` (issue #1044).
    """
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return 0

    from dotenv import load_dotenv

    load_dotenv("../.env", override=True)
    load_dotenv(".env", override=False)

    loaded = 0
    if os.getenv("PUBLIC_DOMAIN"):
        from archimedes.services.secrets_service import load_ssm_secrets

        loaded = load_ssm_secrets()

    _BOOTSTRAPPED = True
    return loaded


#: The URL ``job_queue`` falls back to when ``REDIS_URL`` was unset at the
#: moment that module was first imported. Matched by value rather than by
#: importing the constant, because importing it is the very thing that freezes
#: it — see the module docstring.
_LOCALHOST_REDIS = "redis://localhost:6379/0"


def _require_configured_store(store: Any) -> Any:
    """Refuse a store that is silently pointed at localhost in production.

    ``PUBLIC_DOMAIN`` is this codebase's "am I production" signal (``main.py``
    gates the SSM load and the docs route on it). When it is set, a job store
    bound to the localhost default cannot be right: there is no Redis on the
    worker's loopback, and the failure mode without this guard is not a crash
    but a *success* — events pushed into a store the SSE route will never read,
    for a generation the caller already paid for.
    """
    url = getattr(store, "_url", "")
    if os.getenv("PUBLIC_DOMAIN") and url == _LOCALHOST_REDIS:
        raise RuntimeError(
            "generation worker refusing to start: the job store resolved to "
            f"{_LOCALHOST_REDIS} while PUBLIC_DOMAIN is set. REDIS_URL must be supplied to the "
            "worker as a real environment variable (ECS does this from the task definition's "
            "`secrets` block) — loading it from SSM inside the process is too late, because "
            "importing archimedes.services.* has already frozen job_queue.REDIS_URL."
        )
    return store


def _bind_job_store(explicit: Any = None) -> Any:
    """Build the job store from the environment as it is NOW.

    ``get_job_store()``'s singleton reads ``job_queue.REDIS_URL``, a constant
    captured at import time. Passing the live value explicitly is what makes
    this entrypoint safe to run in a process where the environment was
    completed after that import.
    """
    from archimedes.services.job_queue import JobStore, get_job_store

    if explicit is not None:
        return explicit
    url = os.environ.get("REDIS_URL", "").strip()
    store = JobStore(url=url) if url else get_job_store()
    return _require_configured_store(store)


def _coerce_brief(payload: Any):
    """Build a ``GenerateBrief`` from a dict (or pass one through unchanged).

    The brief is validated by the *model*, not by this entrypoint: an offloaded
    worker must apply the same field constraints (``max_papers`` bounds, the
    ``name`` normalizer, the ``risk_appetite`` enum) the HTTP route applies, and
    the only way to guarantee that is to run the same pydantic model.
    """
    from archimedes.api.generate_schemas import GenerateBrief

    if isinstance(payload, GenerateBrief):
        return payload
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise TypeError(f"brief must be a dict or GenerateBrief, got {type(payload).__name__}")
    return GenerateBrief(**payload)


async def run_job(event: dict[str, Any]) -> dict[str, Any]:
    """Execute one generation job described by ``event``; return a summary.

    ``event`` keys mirror the route's own call into ``run_generation``:
    ``job_id`` (required), ``brief`` (required), ``n_candidates``, ``mode``,
    ``model``, ``owner_user_id``, ``owner_wallet``.

    The summary is deliberately thin — job id, terminal status, wall seconds,
    event count. The *result* of a generation is the rows the pipeline wrote and
    the events it pushed, both of which are already durable in their normal
    places; re-serialising them into an invocation response would create a
    second copy that can disagree with the first.

    Errors are not swallowed. ``run_generation`` already converts pipeline
    failures into ``error`` events and a terminal status, so anything that
    escapes it is an *infrastructure* failure (no Redis, no DB, bad config) —
    precisely the class an offloaded worker must surface to its invoker as a
    failed invocation rather than absorb into a job that looks merely unlucky.

    **The cleanup is not optional on this path either (#1793).** The caller's
    entitlements — a paid generation credit or a free-tier slot — were spent by
    the enqueue, before this function was reached, and the serving path hands
    them back from ``_run_with_cleanup``'s ``finally``, which this entrypoint
    never enters. So the awaited call is wrapped in the same ``finally``,
    calling the same
    :func:`~archimedes.api.generate_routes.release_entitlements_if_undelivered`
    the serving path calls — one seam, so a refund added later cannot reach one
    path only. ``finally``, not ``except``: the silent shape of this bug is the
    insufficient-corpus branch, which writes an ``error`` status and *returns*
    rather than raising, so a handler keyed on exceptions would miss the very
    failure that surfaced the defect. Both refunds are evaluated from the job's
    terminal status (the free one also from the event log), so a delivered run
    keeps paying and a run that persisted a strategy keeps its slot spent.
    """
    bootstrap_environment()

    from archimedes.agents.generation_pipeline import run_generation

    # Deferred with the rest of the heavy imports (see the module docstring's
    # ordering rule). The import statement RUNS on every call, so it resolves
    # `generate_routes.release_entitlements_if_undelivered` at call time rather
    # than at module import — patching that attribute still takes effect here,
    # which is what `test_the_script_path_releases_through_the_same_seam` pins.
    # Importing the shared seam rather than copying the release logic is what
    # keeps this path from drifting away from the route's.
    from archimedes.api.generate_routes import release_entitlements_if_undelivered

    job_id = str(event.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("event is missing job_id")
    brief = _coerce_brief(event.get("brief"))

    store = _bind_job_store(event.get("store"))
    started = time.monotonic()
    try:
        await run_generation(
            job_id=job_id,
            brief=brief,
            n_candidates=int(event.get("n_candidates") or 1),
            store=store,
            mode=event.get("mode"),
            model=event.get("model"),
            owner_wallet=event.get("owner_wallet"),
            owner_user_id=event.get("owner_user_id"),
            dual_regime=bool(event.get("dual_regime", True)),
        )
    finally:
        # The store this worker BOUND, never `get_job_store()`: the singleton
        # reads the import-time `REDIS_URL`, which in a worker that leans on
        # the SSM loader is the localhost default. A refund decided from an
        # empty localhost store would read every delivered run as undelivered.
        await release_entitlements_if_undelivered(job_id, store)
    elapsed = time.monotonic() - started

    record = await store.get(job_id) or {}
    return {
        "job_id": job_id,
        "status": record.get("status") or "unknown",
        "error": record.get("error") or "",
        "wall_seconds": round(elapsed, 3),
        "events": await store.event_count(job_id),
    }


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """AWS Lambda entrypoint. One invocation == one generation job.

    ``asyncio.run`` per invocation (rather than a loop cached on the execution
    environment) is intentional: a warm container reuses the process, and a
    persisted event loop would also persist any task, connection or context var
    the previous job left behind — including the cost meter's context binding,
    whose whole correctness argument is that it is scoped to one job.
    """
    return asyncio.run(run_job(event))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one generation job out-of-process.")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--brief", required=True, help='JSON object, e.g. \'{"intent": "..."}\'')
    parser.add_argument("--n-candidates", type=int, default=1)
    parser.add_argument("--mode", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--owner-user-id", default=None)
    parser.add_argument("--owner-wallet", default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    summary = handler(
        {
            "job_id": args.job_id,
            "brief": args.brief,
            "n_candidates": args.n_candidates,
            "mode": args.mode,
            "model": args.model,
            "owner_user_id": args.owner_user_id,
            "owner_wallet": args.owner_wallet,
        }
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
