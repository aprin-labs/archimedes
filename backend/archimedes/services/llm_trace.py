"""Verbatim capture of the LLM **provider response** at every backend seam (#1800).

Owner direction (Dan, 2026-09-02): *"The raw completion should not be thrown away."*
This module is the capture half of that program. It stores nothing durably — S3
bodies and the Aurora index land in the next PR — it buffers in memory for the
life of one generation job and announces each capture on the job's event stream
as a **pointer**, never a body.

**Why the capture cannot sit on ``complete()``'s return value.** The string that
callers receive is already lossy, in two independent ways:

* :func:`archimedes.services.llm_backend._first_text_block` returns
  ``text.strip()`` for the *first* block that carries text and discards every
  block after it — a second text block, and the surrounding whitespace, are gone.
* The Converse extraction loop skips ``reasoningContent`` blocks outright, so a
  reasoning model's chain of thought never reaches the caller at all.

So a trace built from the returned string could not honestly be called "the model's
reasoning exactly as returned". The recorder is therefore called from *inside*
``complete()``, beside the existing :func:`archimedes.services.cost_meter.record_llm_call`
seam, and is handed the **provider response object** — the Converse dict, the
Anthropic SDK message, the parsed httpx JSON — before any extraction runs.
``backend/tests/services/test_llm_trace_recorder.py`` pins that ordering by
asserting the captured body still contains the blocks the extraction drops.

**Never raises, never blocks a generation.** :func:`record_llm_raw` swallows every
exception (logged at DEBUG) exactly as ``cost_meter.record_llm_call`` does. The
honest consequence of a swallow here is a trace with fewer calls in it — visible
to the reader as a gap in ``seq`` — never a failed or altered generation.

**Thread-safety.** ``complete()`` runs off the event loop (``asyncio.to_thread``
in the debate engine), and the society runs several turns in parallel, so the
buffer is guarded by a lock and the sequence counter is allocated under it.
``asyncio.to_thread`` copies the context, so worker threads see the recorder
bound by ``run_generation``.

**What the binding does and does not reach** (same caveat the cost meter carries):
a recorder is a no-op when none is bound, so instrumenting a path proves nothing
on its own about whether that path is *recorded*. Only ``run_generation`` binds a
recorder today. And no call site labels a call yet: ``stage``, ``candidate_id``
and ``prompt_id`` are ``None`` on every record this PR produces. They are declared
because they are part of the envelope contract the S3 writer will persist, and
because a null there must read as "nobody labelled it", not as "the call had no
stage" — the debate engine and the prompt registry fill them in later.
"""

from __future__ import annotations

import contextlib
import contextvars
import dataclasses
import hashlib
import json
import logging
import os
import threading
import uuid
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from archimedes.services.cost_meter import extract_token_usage

logger = logging.getLogger(__name__)

# Bound so a runaway job cannot grow the buffer without limit. ~16 calls per
# generation today (validator + proposer + debate turns), so 512 is roughly a
# 30x headroom; overflow drops the OLDEST record and is COUNTED, never silent —
# see :meth:`LLMTraceRecorder.stats`.
_DEFAULT_MAX_CALLS = 512


def _max_calls() -> int:
    """Buffer cap from ``LLM_TRACE_MAX_CALLS``; the default on junk or absence."""
    raw = os.getenv("LLM_TRACE_MAX_CALLS", "").strip()
    if not raw:
        return _DEFAULT_MAX_CALLS
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning("llm-trace: LLM_TRACE_MAX_CALLS=%r is not an integer; using %d", raw, _DEFAULT_MAX_CALLS)
        return _DEFAULT_MAX_CALLS


# ── JSON coercion of a provider response ─────────────────────────────────


def _default(obj: Any) -> Any:
    """Last-resort encoder for objects ``json`` does not know. Total by design.

    Must never raise: a serializer that throws here would take the whole capture
    down with it, and a missing body is a worse outcome than an approximate one
    — provided the approximation is visibly an approximation, which ``repr`` is.
    """
    if isinstance(obj, bytes | bytearray):
        return bytes(obj).decode("utf-8", "replace")
    if isinstance(obj, datetime):
        return obj.isoformat()
    for attr, kwargs in (("model_dump", {"mode": "json"}), ("to_dict", {}), ("dict", {})):
        method = getattr(obj, attr, None)
        if callable(method):
            with contextlib.suppress(Exception):  # fall through to the next strategy
                return method(**kwargs)
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        with contextlib.suppress(Exception):  # pragma: no cover — defensive
            return dataclasses.asdict(obj)
    data = getattr(obj, "__dict__", None)
    if isinstance(data, dict) and data:
        return {k: v for k, v in data.items() if not k.startswith("_")}
    try:
        return repr(obj)
    except Exception:  # pragma: no cover — a __repr__ that throws
        return f"<unserializable {type(obj).__name__}>"


def to_jsonable(response: Any) -> Any:
    """JSON-safe view of a provider response, preserving every field it carries.

    Handles the three live shapes: a pydantic v2 model (Anthropic SDK), a plain
    dict (Converse / httpx ``.json()``), and anything else via :func:`_default`.
    Raises only on a response JSON genuinely cannot represent (a cycle) — callers
    record that as ``capture_error`` rather than inventing an empty body.
    """
    candidate = response
    dump = getattr(response, "model_dump", None)
    if callable(dump):
        try:
            candidate = dump(mode="json")
        except TypeError:  # pydantic v1 / a look-alike without `mode`
            candidate = dump()
    return json.loads(json.dumps(candidate, default=_default))


def canonical_bytes(payload: Any) -> bytes:
    """Stable byte encoding of a captured body — the digest preimage.

    Sorted keys and tight separators so the same body digests identically here,
    in the S3 object the next PR writes, and in any later verification.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


# ── The envelope ─────────────────────────────────────────────────────────


@dataclass(slots=True)
class LLMCallRecord:
    """One LLM call, captured whole.

    ``completion_bytes`` / ``completion_sha256`` are over the canonical JSON of
    **``provider_response``** — the artifact this recorder preserves and the next
    PR writes to S3 — *not* over ``completion_text``. Digesting the text would
    attest to the lossy string, which is the exact thing #1800 exists to stop
    trusting. ``completion_text`` is kept beside the body so the two can be
    diffed and the loss measured, not so it can stand in for the body.

    Both digest fields are ``None`` when ``capture_error`` is set: a body we
    failed to serialize is reported as unavailable, never as an empty completion.

    ``model_served`` is a served id only where the provider reports one. The
    Converse response carries no model id, so on ``bedrock_converse`` — the live
    backend — it repeats ``model_requested``. Equal values there mean "the
    provider did not say", not "the requested model ran".
    """

    call_id: str
    job_id: str
    seq: int
    model_requested: str
    model_served: str
    system: str
    user: str
    provider_response: Any | None
    completion_text: str
    usage: dict[str, int | None]
    started_at: str
    latency_ms: float | None
    completion_bytes: int | None
    completion_sha256: str | None
    capture_error: str | None = None
    stage: str | None = None
    candidate_id: str | None = None
    prompt_id: str | None = None

    def pointer(self) -> dict[str, Any]:
        """The stream event payload: identity + integrity, no prose.

        The generation SSE stream has **no per-event owner gate** — everything
        pushed to a job's event log is readable by whoever holds the stream — so
        the body must never travel on it. Reading a body is a separate,
        owner-gated route in a later PR.
        """
        return {
            "call_id": self.call_id,
            "seq": self.seq,
            "model_served": self.model_served,
            "completion_bytes": self.completion_bytes,
            "completion_sha256": self.completion_sha256,
        }

    def as_dict(self) -> dict[str, Any]:
        """The full envelope — what the S3 writer will serialize, one per line."""
        return dataclasses.asdict(self)


# ── The recorder ─────────────────────────────────────────────────────────


@dataclass(eq=False)
class LLMTraceRecorder:
    """Per-job in-memory buffer of :class:`LLMCallRecord`. Thread-safe.

    ``pointer_sink`` is called once per recorded call with :meth:`LLMCallRecord.pointer`.
    It runs on whatever thread made the LLM call, so it must be non-blocking and
    thread-safe; ``generation_pipeline._llm_pointer_sink`` hops back to the event
    loop. A sink that raises is logged and swallowed — the record is already
    buffered by then, so a broken stream costs the event, never the trace.
    """

    job_id: str = ""
    pointer_sink: Callable[[dict[str, Any]], None] | None = None
    max_calls: int = field(default_factory=_max_calls)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _calls: deque[LLMCallRecord] = field(default_factory=deque, repr=False)
    _seq: int = field(default=0, repr=False)
    _recorded: int = field(default=0, repr=False)
    _dropped: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        self.max_calls = max(1, int(self.max_calls))
        self._calls = deque(self._calls, maxlen=self.max_calls)

    # ── write ────────────────────────────────────────────────────────────

    def record(
        self,
        *,
        system: str,
        user: str,
        model_requested: str,
        model_served: str,
        provider_response: Any,
        completion_text: str,
        started_at: datetime | str | None = None,
        latency_ms: float | None = None,
        stage: str | None = None,
        candidate_id: str | None = None,
        prompt_id: str | None = None,
    ) -> LLMCallRecord:
        """Buffer one call and hand its pointer to the sink. Returns the record.

        Serialization of ``provider_response`` happens here, on the calling
        thread, so the buffer holds plain JSON-safe data with no reference to a
        provider SDK object whose lifetime we do not control.
        """
        body: Any | None
        capture_error: str | None = None
        try:
            body = to_jsonable(provider_response)
        except Exception as exc:
            body = None
            capture_error = f"{type(exc).__name__}: {exc}"
            logger.debug("llm-trace: provider response not serializable", exc_info=True)

        if body is None and capture_error is not None:
            n_bytes: int | None = None
            digest: str | None = None
        else:
            blob = canonical_bytes(body)
            n_bytes = len(blob)
            digest = hashlib.sha256(blob).hexdigest()

        # Token counts come from the SAME response object, through the same
        # extractor the cost meter uses (`cost_meter.extract_token_usage`), so a
        # trace row and a cost row can never disagree about one call. `None`
        # means the provider reported nothing — never a substituted zero.
        try:
            input_tokens, output_tokens = extract_token_usage(provider_response)
        except Exception:  # pragma: no cover — defensive; the extractor is total
            input_tokens = output_tokens = None
        usage: dict[str, int | None] = {"input_tokens": input_tokens, "output_tokens": output_tokens}

        if isinstance(started_at, datetime):
            started = started_at.astimezone(UTC).isoformat()
        elif isinstance(started_at, str) and started_at:
            started = started_at
        else:
            started = datetime.now(UTC).isoformat()

        with self._lock:
            self._seq += 1
            self._recorded += 1
            record = LLMCallRecord(
                call_id=uuid.uuid4().hex,
                job_id=self.job_id,
                seq=self._seq,
                model_requested=model_requested,
                model_served=model_served,
                system=system,
                user=user,
                provider_response=body,
                completion_text=completion_text,
                usage=usage,
                started_at=started,
                latency_ms=latency_ms,
                completion_bytes=n_bytes,
                completion_sha256=digest,
                capture_error=capture_error,
                stage=stage,
                candidate_id=candidate_id,
                prompt_id=prompt_id,
            )
            before = len(self._calls)
            self._calls.append(record)
            if len(self._calls) == before == self.max_calls:
                self._dropped += 1

        sink = self.pointer_sink
        if sink is not None:
            try:
                sink(record.pointer())
            except Exception:
                logger.debug("llm-trace: pointer sink failed for call %s", record.call_id, exc_info=True)
        return record

    # ── read ─────────────────────────────────────────────────────────────

    def calls(self) -> list[LLMCallRecord]:
        """Snapshot of the buffered records, oldest first."""
        with self._lock:
            return list(self._calls)

    def stats(self) -> dict[str, int]:
        """Counts the reader needs to tell a short trace from a lossy one.

        ``recorded`` is every call the recorder saw; ``buffered`` is how many it
        still holds; ``dropped`` is how many the cap evicted. Until :meth:`clear`
        runs, ``recorded == buffered + dropped``; after it ``buffered`` falls to 0
        while the counts stand, so a shortfall means "the buffer was cleared", and
        ``dropped > 0`` is the only honest way to say "this trace is missing its
        oldest calls".
        """
        with self._lock:
            return {
                "recorded": self._recorded,
                "buffered": len(self._calls),
                "dropped": self._dropped,
                "max_calls": self.max_calls,
            }

    def clear(self) -> None:
        """Drop the buffered bodies. Counts survive so the reader keeps the truth."""
        with self._lock:
            self._calls.clear()


# ── Context binding ──────────────────────────────────────────────────────

_CURRENT: contextvars.ContextVar[LLMTraceRecorder | None] = contextvars.ContextVar(
    "archimedes_llm_trace_recorder", default=None
)


def current_recorder() -> LLMTraceRecorder | None:
    """The recorder bound to this context, or ``None`` outside a recorded job."""
    return _CURRENT.get()


def bind(recorder: LLMTraceRecorder) -> contextvars.Token:
    """Bind ``recorder`` to the current context; pass the token to :func:`unbind`."""
    return _CURRENT.set(recorder)


def unbind(token: contextvars.Token) -> None:
    """Unbind and **clear the buffer** — the bodies do not outlive the job.

    Nothing is persisted in this PR, so the buffer is the only copy; the flush
    the next PR adds must therefore run *before* this call. Clearing here is
    deliberate: an in-memory buffer of full prompts and completions that
    outlived its job would be a retention surface nobody asked for.

    **Never raises.** This is the first statement in ``run_generation``'s
    ``finally``, above the cost snapshot and its durable row (#1217/#1326). A
    ``reset`` that escaped there would mask the job's real exception, skip the
    cost persistence *and* skip the ``clear()`` below — leaving the bodies in
    memory, the one outcome this function exists to prevent. So the reset is
    guarded the way :func:`archimedes.services.cost_meter.unbind` guards its
    own (``ValueError`` for a token from another context, ``RuntimeError`` for
    a token already used) and the clear runs either way.
    """
    recorder = _CURRENT.get()
    try:
        _CURRENT.reset(token)
    except (ValueError, RuntimeError):
        logger.debug("llm-trace: token reset out of context", exc_info=True)
    if recorder is not None:
        recorder.clear()


@contextmanager
def recording(job_id: str = "", **kwargs: Any) -> Iterator[LLMTraceRecorder]:
    """Bind a fresh recorder for the duration of the block."""
    recorder = LLMTraceRecorder(job_id=job_id, **kwargs)
    token = bind(recorder)
    try:
        yield recorder
    finally:
        unbind(token)


# ── Module-level recorder (a no-op outside a recorded job) ────────────────


def record_llm_raw(
    *,
    system: str,
    user: str,
    model_requested: str,
    model_served: str,
    provider_response: Any,
    completion_text: str,
    started_at: datetime | str | None = None,
    latency_ms: float | None = None,
    stage: str | None = None,
    candidate_id: str | None = None,
    prompt_id: str | None = None,
) -> None:
    """Record one raw provider response. **Never raises.**

    Called from inside every backend's ``complete()``. Instrumentation must not
    be able to fail a real generation, so every error — including a recorder
    subclass or a sink that throws — is logged at DEBUG and swallowed.
    """
    recorder = _CURRENT.get()
    if recorder is None:
        return
    try:
        recorder.record(
            system=system,
            user=user,
            model_requested=model_requested,
            model_served=model_served,
            provider_response=provider_response,
            completion_text=completion_text,
            started_at=started_at,
            latency_ms=latency_ms,
            stage=stage,
            candidate_id=candidate_id,
            prompt_id=prompt_id,
        )
    except Exception:  # pragma: no cover — defensive; exercised by the guard test
        logger.debug("llm-trace: failed to record a raw LLM call", exc_info=True)
