"""Cross-process cancellation for POST /api/generate/jobs/{id}/cancel (#1667).

The defect: `_RUNNING_TASKS` in `generate_routes.py` is a process-local dict,
so `cancel_job` only ever hard-cancelled a job whose `/start` happened to land
on the SAME task. At `MinCapacity=2` that is a coin flip; on every other
request the endpoint logged "no live task" and returned
`{"status": "cancelled"}` while the pipeline kept running and burning LLM
tokens — a false claim returned to a paying user.

The fix: cancellation is a durable Redis flag the pipeline polls at its stage
boundaries. These tests pin both halves of that contract:

  C1  a cancel served by a DIFFERENT process (a second `JobStore` on a second
      Redis client over one shared fake server, with an EMPTY `_RUNNING_TASKS`
      — precisely the task that did not start the job) stops the pipeline at
      the next stage boundary: the second candidate never runs, the job ends
      `cancelled`, and no `done` event is ever emitted.
  C2  `cancel_job` does NOT report `cancelled` when the flag write fails
      (Redis raises) or is not confirmed by read-back (the write silently did
      not land) — it 503s instead, because the job really is still running.
  C3  store-level unit checks on the flag itself (set + TTL, absent by
      default, idempotent).

Hermetic: `fakeredis.FakeAsyncRedis` over a shared `FakeServer` for the two
"processes" (`test_generate_job_liveness.py` precedent), the real FastAPI app
driven through `httpx.ASGITransport` with dependency-overridden auth, the
fixture generation path (no LLM), and `_persist_candidate` stubbed. No live
Redis/DB/LLM/network.
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, patch

import fakeredis
import pytest
from archimedes.agents import generation_pipeline
from archimedes.agents.generation_pipeline import run_generation
from archimedes.api.account_auth import CurrentUser, require_current_user
from archimedes.api.generate_schemas import GenerateBrief
from archimedes.services.job_queue import CANCEL_SUFFIX, JOB_TTL, KEY_PREFIX, JobStore
from httpx import ASGITransport, AsyncClient

_USER_ID = "user-cancel-xproc"


@pytest.fixture(autouse=True)
def _authenticated_account():
    from archimedes.main import app

    app.dependency_overrides[require_current_user] = lambda: CurrentUser(
        _USER_ID, "Cancel Test", "cancel@example.com", True
    )
    yield
    app.dependency_overrides.pop(require_current_user, None)


@pytest.fixture(autouse=True)
def _force_fixture_path(monkeypatch):
    monkeypatch.setenv("GENERATION_PIPELINE_FIXTURE", "1")


def _two_processes() -> tuple[JobStore, JobStore]:
    """Two JobStores, two Redis clients, ONE shared Redis — i.e. two tasks.

    Sharing a `FakeServer` rather than a client object is the point: nothing
    in-process is shared between the pipeline side and the API side, exactly
    as when the ALB routes /start and /cancel to different Fargate tasks.
    """
    server = fakeredis.FakeServer()
    pipeline_store = JobStore(url="redis://unused")
    pipeline_store._redis = fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
    api_store = JobStore(url="redis://unused")
    api_store._redis = fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
    return pipeline_store, api_store


async def _post_cancel(job_id: str, api_store: JobStore):
    """Serve the cancel from the process that did NOT start the job.

    `_RUNNING_TASKS` is patched to `{}` to make that literal: this task holds
    no handle on the running pipeline, which is the condition under which the
    old code silently did nothing and lied about it.
    """
    from archimedes.main import app

    with (
        patch("archimedes.api.generate_routes.get_job_store", return_value=api_store),
        patch("archimedes.api.generate_routes._RUNNING_TASKS", {}),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.post(f"/api/generate/jobs/{job_id}/cancel")


# ── C1: a cancel from another process actually stops the pipeline ─────────


async def test_cancel_from_another_process_stops_the_pipeline():
    """The input that SHOULD fail against pre-fix code: the cancel is served by
    a task with an EMPTY `_RUNNING_TASKS`. Pre-fix that took the
    "no live task (already finished or restart)" branch, returned
    `{"status": "cancelled"}`, and the pipeline ran the second candidate to
    completion and reported `done` — the exact false claim in #1667.
    """
    pipeline_store, api_store = _two_processes()
    job_id = await pipeline_store.enqueue(
        job_type="generate",
        payload={"owner_user_id": _USER_ID, "brief": {"intent": "cross-process cancel repro"}, "n_candidates": 1},
    )

    calls: list[str] = []
    first_candidate_started = asyncio.Event()
    release_first_candidate = asyncio.Event()
    real_runner = generation_pipeline._run_fixture_candidate

    async def _gated_runner(**kwargs):
        """Hold the FIRST candidate open so the cancel lands mid-stage — the
        realistic case (nobody clicks Cancel between two stages)."""
        calls.append(kwargs["candidate_id"])
        if len(calls) == 1:
            first_candidate_started.set()
            await release_first_candidate.wait()
        return await real_runner(**kwargs)

    brief = GenerateBrief(intent="cross-process cancel repro", risk_appetite="conservative")

    with (
        patch("archimedes.agents.generation_pipeline._llm_available", return_value=False),
        patch("archimedes.agents.generation_pipeline._run_fixture_candidate", new=_gated_runner),
        patch(
            "archimedes.agents.generation_pipeline._persist_candidate",
            new=AsyncMock(return_value=("strat_xproc_001", "0xdeadbeef")),
        ),
    ):
        task = asyncio.create_task(
            run_generation(job_id=job_id, brief=brief, n_candidates=1, store=pipeline_store),
        )
        try:
            await asyncio.wait_for(first_candidate_started.wait(), timeout=10)

            resp = await _post_cancel(job_id, api_store)
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] == "cancelled"

            release_first_candidate.set()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=10)
        finally:
            task.cancel()

    # The pipeline stopped at the next stage boundary: candidate 2 never ran.
    assert calls == ["cand_bull"], f"pipeline kept generating after the cancel: {calls}"
    assert task.cancelled(), "the run must end cancelled, not complete normally"

    job = await pipeline_store.get(job_id)
    assert job["status"] == "cancelled"

    events = await pipeline_store.list_events(job_id)
    names = [e["event"] for e in events]
    assert "done" not in names, f"a cancelled job must never report done: {names}"
    assert "persisted" not in names, f"a cancelled job must not persist a strategy: {names}"
    # The flag is what carried the cancel across the process boundary.
    assert await pipeline_store.is_cancel_requested(job_id) is True


async def test_uncancelled_job_runs_to_completion():
    """Sanity check on the other side of the guard: the stage-boundary poll
    must stop a cancelled run, not every run."""
    pipeline_store, _api_store = _two_processes()
    job_id = await pipeline_store.enqueue(job_type="generate", payload={"owner_user_id": _USER_ID})
    brief = GenerateBrief(intent="uncancelled control run", risk_appetite="conservative")

    with (
        patch("archimedes.agents.generation_pipeline._llm_available", return_value=False),
        patch(
            "archimedes.agents.generation_pipeline._persist_candidate",
            new=AsyncMock(return_value=("strat_xproc_002", "0xfeedface")),
        ),
    ):
        await run_generation(job_id=job_id, brief=brief, n_candidates=1, store=pipeline_store)

    names = [e["event"] for e in await pipeline_store.list_events(job_id)]
    assert names[-1] == "done"
    assert (await pipeline_store.get(job_id))["status"] == "done"


# ── C2: no "cancelled" claim on an unconfirmed flag write ─────────────────


async def test_cancel_does_not_claim_cancelled_when_the_flag_write_raises():
    """Redis down at the flag write. The job IS still running, so the endpoint
    must say so (503) instead of returning `{"status": "cancelled"}`."""
    pipeline_store, api_store = _two_processes()
    job_id = await pipeline_store.enqueue(job_type="generate", payload={"owner_user_id": _USER_ID})

    with patch.object(api_store._redis, "set", new=AsyncMock(side_effect=ConnectionError("redis down"))):
        resp = await _post_cancel(job_id, api_store)

    assert resp.status_code == 503, resp.text
    assert resp.json().get("status") != "cancelled"
    # …and the job record is untouched: still runnable, honestly reported.
    assert (await pipeline_store.get(job_id))["status"] == "queued"
    assert await pipeline_store.is_cancel_requested(job_id) is False


async def test_cancel_does_not_claim_cancelled_when_the_flag_is_not_confirmed():
    """The subtler failure: the write "succeeds" but the key is not there on
    read-back (an eviction/silent loss). An unconfirmed cancel is not a
    cancel — never assume it landed."""
    pipeline_store, api_store = _two_processes()
    job_id = await pipeline_store.enqueue(job_type="generate", payload={"owner_user_id": _USER_ID})

    with patch.object(api_store._redis, "exists", new=AsyncMock(return_value=0)):
        resp = await _post_cancel(job_id, api_store)

    assert resp.status_code == 503, resp.text
    assert resp.json().get("status") != "cancelled"
    assert (await pipeline_store.get(job_id))["status"] == "queued"


async def test_cancel_reports_cancelled_once_the_flag_is_durable():
    """Sanity check on the other side of C2: a confirmed write still returns
    `cancelled`, and the flag is visible to the OTHER process."""
    pipeline_store, api_store = _two_processes()
    job_id = await pipeline_store.enqueue(job_type="generate", payload={"owner_user_id": _USER_ID})

    resp = await _post_cancel(job_id, api_store)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"
    assert await pipeline_store.is_cancel_requested(job_id) is True
    assert (await pipeline_store.get(job_id))["status"] == "cancelled"


# ── C3: the flag itself ───────────────────────────────────────────────────


async def test_request_cancel_sets_a_ttld_flag_visible_to_other_stores():
    pipeline_store, api_store = _two_processes()
    job_id = await pipeline_store.enqueue(job_type="generate", payload={})

    assert await pipeline_store.is_cancel_requested(job_id) is False
    assert await api_store.request_cancel(job_id) is True
    assert await pipeline_store.is_cancel_requested(job_id) is True

    ttl = await pipeline_store._redis.ttl(f"{KEY_PREFIX}{job_id}{CANCEL_SUFFIX}")
    assert 0 < ttl <= JOB_TTL, "the cancel flag must expire with the job, never leak forever"


async def test_request_cancel_is_idempotent():
    _pipeline_store, api_store = _two_processes()
    job_id = await api_store.enqueue(job_type="generate", payload={})

    assert await api_store.request_cancel(job_id) is True
    assert await api_store.request_cancel(job_id) is True
    assert await api_store.is_cancel_requested(job_id) is True
