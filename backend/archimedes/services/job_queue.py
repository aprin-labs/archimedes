"""Redis-backed async job queue for strategy generation.

Jobs are stored as Redis hashes with a TTL. States: queued → running → done | failed.
Uses the same aioredis pattern as redis_state.py.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as aioredis

from archimedes.services.log_scrubber import sanitize_log_value

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
KEY_PREFIX = "archimedes:job:"
EVENT_LOG_SUFFIX = ":events"
# Cross-process cancellation flag (#1667). Redis is the ONLY surface every
# task/worker shares, so a cancel request has to live here rather than in a
# process-local dict — with MinCapacity=2 the POST lands on the task that
# started the job barely half the time, and any offload lane has no in-process
# task registry at all.
CANCEL_SUFFIX = ":cancel"
JOB_TTL = 3600  # 1 hour
EVENT_LOG_TTL = 900  # 15 minutes after terminal state — spec 0.5 § Reconnection


def _safe_json(raw, default, *, context: str):
    """``json.loads`` that returns ``default`` on malformed data (#919).

    A truncated / tampered ``payload`` or ``result`` field must not 500 the job
    read path; log and fall back to the default so the job still lists.
    """
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("Malformed JSON in job store (%s) — using default: %s", context, exc)
        return default


class JobStore:
    """Thin Redis wrapper for async job lifecycle."""

    def __init__(self, url: str | None = None) -> None:
        self._url = url or REDIS_URL
        self._redis: aioredis.Redis | None = None

    async def _get_redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = aioredis.from_url(self._url, decode_responses=True)
        return self._redis

    async def enqueue(
        self,
        *,
        job_type: str,
        payload: dict[str, Any],
    ) -> str:
        """Create a queued job and return its ID."""
        job_id = uuid.uuid4().hex[:16]
        now = datetime.now(UTC).isoformat()
        data = {
            "id": job_id,
            "type": job_type,
            "status": "queued",
            "payload": json.dumps(payload, default=str),
            "result": "",
            "error": "",
            "created_at": now,
            "updated_at": now,
            # Liveness signal (#1355): distinct from `updated_at`, which only
            # moves on a STATUS transition. `heartbeat_at` also moves on every
            # `touch()` call from the run's independent heartbeat task, so it
            # keeps advancing through a long silent compute stretch (a debate
            # turn, a fan-out backtest gather) that produces no status write at
            # all. That gap between "the process is alive" and "the status
            # changed" is exactly what let a dead job read as `running` forever.
            "heartbeat_at": now,
        }
        r = await self._get_redis()
        key = f"{KEY_PREFIX}{job_id}"
        await r.hset(key, mapping=data)
        await r.expire(key, JOB_TTL)
        logger.info("job: enqueued %s (%s)", job_id, job_type)
        return job_id

    async def get(self, job_id: str) -> dict[str, Any] | None:
        """Get job data by ID. Returns None if not found."""
        r = await self._get_redis()
        key = f"{KEY_PREFIX}{job_id}"
        raw = await r.hgetall(key)
        if not raw:
            return None
        return {
            "id": raw.get("id", job_id),
            "type": raw.get("type", ""),
            "status": raw.get("status", "unknown"),
            "payload": _safe_json(raw.get("payload"), {}, context=f"job:{job_id}:payload"),
            "result": _safe_json(raw.get("result"), None, context=f"job:{job_id}:result"),
            "error": raw.get("error", ""),
            "created_at": raw.get("created_at", ""),
            "updated_at": raw.get("updated_at", ""),
            # "" (not None) for a pre-#1355 job that predates this field — an
            # honest absence read-time normalisation treats as "no signal",
            # never as "assume stale" or "assume fresh".
            "heartbeat_at": raw.get("heartbeat_at", ""),
        }

    async def update_status(
        self,
        job_id: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        """Transition a job to a new status with optional result/error.

        Also refreshes `heartbeat_at` and re-`expire`s the hash to the full
        `JOB_TTL` (#1355). Before this, `hset` alone left Redis's original TTL
        counting down from `enqueue` untouched — an hour-long job's own record
        could disappear mid-run no matter how active it was, because `HSET`
        preserves an existing key's TTL rather than resetting it.
        """
        r = await self._get_redis()
        key = f"{KEY_PREFIX}{job_id}"
        now = datetime.now(UTC).isoformat()
        updates: dict[str, str] = {
            "status": status,
            "updated_at": now,
            "heartbeat_at": now,
        }
        if result is not None:
            updates["result"] = json.dumps(result, default=str)
        if error:
            updates["error"] = error
        await r.hset(key, mapping=updates)
        await r.expire(key, JOB_TTL)
        logger.info("job: %s → %s", sanitize_log_value(job_id), status)

    async def touch(self, job_id: str) -> bool:
        """Refresh `heartbeat_at` + the job's TTL only — no status change.

        Called by the run's independent heartbeat task (`generate_routes.py`)
        every `_JOB_HEARTBEAT_INTERVAL_SECONDS` for the life of the run, on its
        own clock — NOT gated on pipeline progress, so a long silent compute
        stretch (a debate turn, a fan-out backtest gather) still proves the
        process is alive even though it emits no job-store event.

        Returns `False` (no-op) when the job no longer exists: a heartbeat
        racing its own task's cancellation/cleanup must never resurrect an
        expired or already-deleted hash.
        """
        r = await self._get_redis()
        key = f"{KEY_PREFIX}{job_id}"
        if not await r.exists(key):
            return False
        now = datetime.now(UTC).isoformat()
        await r.hset(key, "heartbeat_at", now)
        await r.expire(key, JOB_TTL)
        return True

    # ── Cross-process cancellation (#1667) ────────────────────────────────

    async def request_cancel(self, job_id: str) -> bool:
        """Durably record a cancellation request. Returns True only if CONFIRMED.

        The flag is a separate key (`archimedes:job:{id}:cancel`) rather than a
        field on the job hash so it survives independently of the status write
        and can be set on a job whose hash is being rewritten concurrently by
        the running pipeline.

        The write is read back before returning: a caller may only tell a user
        "cancelled" once the request is provably visible to whichever task is
        actually running the pipeline. An unconfirmed write returns False —
        never "assume it landed" (`runner_lease.py`'s fail-closed discipline).
        Redis errors propagate; the caller decides what to report.
        """
        r = await self._get_redis()
        key = f"{KEY_PREFIX}{job_id}{CANCEL_SUFFIX}"
        await r.set(key, datetime.now(UTC).isoformat(), ex=JOB_TTL)
        confirmed = bool(await r.exists(key))
        if confirmed:
            logger.info("job: cancel requested for %s", sanitize_log_value(job_id))
        else:
            logger.error(
                "job: cancel flag for %s did not survive read-back — NOT reporting cancelled",
                sanitize_log_value(job_id),
            )
        return confirmed

    async def is_cancel_requested(self, job_id: str) -> bool:
        """True once a cancel has been durably requested for this job.

        Polled by the pipeline at its stage boundaries, from whatever task
        happens to be running it — that is the whole point: the request is
        shared state, not a reference held by one process.
        """
        r = await self._get_redis()
        return bool(await r.exists(f"{KEY_PREFIX}{job_id}{CANCEL_SUFFIX}"))

    async def update_terminal_status(
        self,
        job_id: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> bool:
        """Terminal-state write that never clobbers an already-`cancelled` job.

        A user's Cancel and the pipeline's own terminal write can race
        (#1355): `asyncio.Task.cancel()` cannot interrupt code that is mid
        `asyncio.to_thread(llm_call)` — the awaiter only unblocks once that
        thread finishes on its own, so the pipeline can still reach its
        terminal write well after `cancel_job` already flipped Redis's status
        to `cancelled`. Read-before-write here makes `cancelled` sticky
        against a same-run terminal write landing after it — this is what
        makes the Cancel button's guarantee honest rather than cosmetic.

        Returns `True` when the write landed, `False` when it was skipped
        because the job was already `cancelled`.
        """
        r = await self._get_redis()
        key = f"{KEY_PREFIX}{job_id}"
        current = await r.hget(key, "status")
        if current == "cancelled":
            logger.info(
                "job: terminal write (%s) suppressed for %s — already cancelled",
                status,
                sanitize_log_value(job_id),
            )
            return False
        await self.update_status(job_id, status, result=result, error=error)
        return True

    async def merge_result(self, job_id: str, patch: dict[str, Any]) -> bool:
        """Merge top-level keys into an EXISTING job's persisted ``result``.

        Used by the cost meter (#1217) to attach the per-job measurement record
        after the terminal ``update_status`` has already written the result —
        including on the error / cancelled paths, where there is no result write
        at all but the compute was still spent.

        Returns ``True`` when the patch landed. **Never creates a job.** A bare
        ``hset`` on a missing key would materialise a TTL-less phantom hash that
        then shows up in ``list_recent_jobs`` as a statusless job, so existence
        is checked before the write and re-checked after it (the key can expire
        in between); a hash the write resurrected is deleted again.
        """
        r = await self._get_redis()
        key = f"{KEY_PREFIX}{job_id}"
        if not await r.exists(key):
            logger.debug("job: merge_result skipped — %s not found", sanitize_log_value(job_id))
            return False
        current = _safe_json(await r.hget(key, "result"), {}, context=f"job:{job_id}:result")
        if not isinstance(current, dict):
            # A non-dict result (legacy / tampered) is left intact rather than
            # clobbered — the measurement is not worth destroying a payload for.
            logger.warning("job: merge_result skipped — %s has a non-dict result", sanitize_log_value(job_id))
            return False
        current.update(patch)
        await r.hset(key, "result", json.dumps(current, default=str))
        if not await r.hexists(key, "id"):
            # The key expired between the check and the write; the hset above
            # recreated it as a fragment. Undo rather than leave a phantom.
            await r.delete(key)
            return False
        return True

    # ── Event log (for streaming jobs) ────────────────────────────────────

    async def push_event(self, job_id: str, event_payload: dict[str, Any]) -> int:
        """Append an event to the job's event log and return its monotonic ID.

        Events are stored as JSON strings in a Redis list keyed
        ``archimedes:job:{id}:events``. The returned ID is the 1-based list
        index used as the SSE ``id:`` header — the client sends it back as
        ``Last-Event-ID`` to resume from a known point.
        """
        r = await self._get_redis()
        key = f"{KEY_PREFIX}{job_id}{EVENT_LOG_SUFFIX}"
        new_length = await r.rpush(key, json.dumps(event_payload, default=str))
        await r.expire(key, EVENT_LOG_TTL)
        return new_length

    async def list_events(self, job_id: str, *, after_id: int = 0) -> list[dict[str, Any]]:
        """Return events with ID > ``after_id`` in order.

        The returned dicts each have ``id`` (the monotonic event ID) plus
        whatever keys ``push_event`` was given.
        """
        r = await self._get_redis()
        key = f"{KEY_PREFIX}{job_id}{EVENT_LOG_SUFFIX}"
        if after_id < 0:
            after_id = 0
        raw = await r.lrange(key, after_id, -1)
        out: list[dict[str, Any]] = []
        for i, blob in enumerate(raw, start=after_id + 1):
            try:
                payload = json.loads(blob)
            except (TypeError, ValueError):
                continue
            payload["id"] = i
            out.append(payload)
        return out

    async def event_count(self, job_id: str) -> int:
        """Length of the event log."""
        r = await self._get_redis()
        key = f"{KEY_PREFIX}{job_id}{EVENT_LOG_SUFFIX}"
        return await r.llen(key)

    async def list_recent_jobs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Scan recent jobs for the GenerationStatus UI.

        SCAN-based; bounded by ``limit`` so the call stays cheap. Sorted by
        ``updated_at`` desc.
        """
        r = await self._get_redis()
        pattern = f"{KEY_PREFIX}*"
        keys: list[str] = []
        async for key in r.scan_iter(match=pattern, count=200):
            if key.endswith(EVENT_LOG_SUFFIX):
                continue
            keys.append(key)
            if len(keys) >= limit * 4:
                break
        jobs: list[dict[str, Any]] = []
        for key in keys:
            raw = await r.hgetall(key)
            if not raw:
                continue
            jobs.append(
                {
                    "id": raw.get("id", key.removeprefix(KEY_PREFIX)),
                    "type": raw.get("type", ""),
                    "status": raw.get("status", "unknown"),
                    "payload": _safe_json(raw.get("payload"), {}, context=f"job:{key}:payload"),
                    "result": _safe_json(raw.get("result"), None, context=f"job:{key}:result"),
                    "error": raw.get("error", ""),
                    "created_at": raw.get("created_at", ""),
                    "updated_at": raw.get("updated_at", ""),
                    "heartbeat_at": raw.get("heartbeat_at", ""),
                }
            )
        jobs.sort(key=lambda j: j.get("updated_at", ""), reverse=True)
        return jobs[:limit]

    async def close(self) -> None:
        if self._redis:
            await self._redis.aclose()
            self._redis = None


_default_store: JobStore | None = None


def get_job_store() -> JobStore:
    """Singleton accessor used by routes + the pipeline."""
    global _default_store
    if _default_store is None:
        _default_store = JobStore()
    return _default_store
