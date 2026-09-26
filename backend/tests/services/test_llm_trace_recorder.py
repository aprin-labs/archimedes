"""Raw LLM trace capture (#1800) — the recorded body must be the one the caller never sees.

The whole point of this recorder is that ``complete()``'s return value is *already*
lossy: ``_first_text_block`` keeps the first text block and strips it, the Converse
loop skips ``reasoningContent`` entirely, and the OpenAI path reads
``choices[0]`` and nothing else. A trace built downstream of those lines would be
a trace of the truncation, so the guards below are the substance of this file:

* **G1** — round-trip at **all six** ``record_llm_raw`` seams, each driven by a fake
  provider whose response carries a reasoning block *and* a second text block. The
  assertion is that the recorded ``provider_response`` still contains both, i.e.
  capture sits **upstream** of the extraction. If someone moves the call below the
  extraction, or passes ``text`` instead of ``resp``, this fails.
* **G2** — a recorder that raises internally, and a pointer sink that raises, must
  not fail ``complete()``. Instrumentation may not break a generation.
* **G3** — the stream event carries **no body**. Asserted on the pointer itself and
  end-to-end through the real ``generation_pipeline._llm_pointer_sink`` +
  ``_Emitter`` into a fake job store, including the thread hop.
* **G4** — ``unbind`` clears the buffer, and cannot raise while doing it. Nothing
  is persisted in this PR, so a buffer that outlived its job would be an
  unasked-for retention surface; and ``unbind`` runs first in ``run_generation``'s
  ``finally``, above the cost persistence, so an escaping reset would mask the
  job's real exception *and* leave the bodies in memory.
* **G5** — ``run_generation`` actually binds the recorder, the stages beneath it
  record into *that* recorder, and the binding + buffer are gone afterwards. Every
  guard above binds a recorder by hand, so without G5 the production wiring is
  untested: deleting the bind and the unbind from the pipeline leaves the whole
  suite green — the difference between "records every call" and "records nothing".

Hermetic: no AWS, no network, no Postgres, no Redis. Every provider client is a
local fake; the two httpx backends have ``httpx.post`` monkeypatched.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from archimedes.agents import generation_pipeline
from archimedes.services import llm_trace
from archimedes.services.llm_backend import (
    AnthropicBackend,
    AnthropicCompatibleBackend,
    BedrockBackend,
    BedrockConverseBackend,
    OllamaBackend,
    OpenAIBackend,
)
from archimedes.services.llm_trace import LLMTraceRecorder

# Markers that must survive into the recorded body but NOT into the stream event.
SYSTEM = "SYSTEM-PROMPT-MARKER: you are a quantitative researcher."
USER = "USER-PROMPT-MARKER: propose a strategy."
REASONING = "REASONING-MARKER: the chain of thought callers never see."
FIRST_TEXT = "  FIRST-TEXT-MARKER  "
SECOND_TEXT = "SECOND-TEXT-MARKER: the block _first_text_block throws away."

_PROSE_MARKERS = (SYSTEM, USER, REASONING, FIRST_TEXT.strip(), SECOND_TEXT)


# ── Fake providers ────────────────────────────────────────────────────────


@dataclass
class _Block:
    """An Anthropic-style content block. ``thinking`` blocks carry no ``.text``."""

    type: str
    text: str | None = None
    thinking: str | None = None


class _FakeAnthropicResponse:
    """Shaped like an ``anthropic.types.Message``: block objects + ``model_dump``."""

    def __init__(self) -> None:
        self.model = "fake-served-model"
        self.content = [
            _Block(type="thinking", thinking=REASONING),
            _Block(type="text", text=FIRST_TEXT),
            _Block(type="text", text=SECOND_TEXT),
        ]
        self.usage = SimpleNamespace(input_tokens=11, output_tokens=7)

    def model_dump(self, mode: str = "python") -> dict[str, Any]:
        return {
            "model": self.model,
            "content": [{"type": b.type, "text": b.text, "thinking": b.thinking} for b in self.content],
            "usage": {"input_tokens": 11, "output_tokens": 7},
        }


class _FakeAnthropicClient:
    def __init__(self) -> None:
        self.messages = SimpleNamespace(create=lambda **_kw: _FakeAnthropicResponse())


class _FakeConverseClient:
    """boto3 ``bedrock-runtime`` stand-in: ``converse(**kwargs) -> dict``."""

    def converse(self, **_kwargs: Any) -> dict[str, Any]:
        return {
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"reasoningContent": {"reasoningText": {"text": REASONING}}},
                        {"text": FIRST_TEXT},
                        {"text": SECOND_TEXT},
                    ],
                }
            },
            "usage": {"inputTokens": 11, "outputTokens": 7, "totalTokens": 18},
            "stopReason": "end_turn",
        }


class _FakeHTTPResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


_OPENAI_BODY: dict[str, Any] = {
    "model": "fake-served-model",
    "choices": [
        {"message": {"role": "assistant", "content": FIRST_TEXT, "reasoning_content": REASONING}},
        {"message": {"role": "assistant", "content": SECOND_TEXT}},
    ],
    "usage": {"prompt_tokens": 11, "completion_tokens": 7},
}

_OLLAMA_BODY: dict[str, Any] = {
    "model": "fake-served-model",
    "message": {"role": "assistant", "content": FIRST_TEXT, "thinking": REASONING},
    "prompt_eval_count": 11,
    "eval_count": 7,
}


def _anthropic_style(cls: type) -> Any:
    """An Anthropic-SDK-family backend wired to the fake client, no credentials."""
    backend = cls.__new__(cls)
    backend._model = "fake-requested-model"
    backend._served = "fake-requested-model"
    backend._client = _FakeAnthropicClient()
    return backend


def _converse_backend() -> BedrockConverseBackend:
    backend = BedrockConverseBackend.__new__(BedrockConverseBackend)
    backend._model = "fake-requested-model"
    backend._served = "fake-requested-model"
    backend._client = _FakeConverseClient()
    return backend


def _httpx_backend(cls: type) -> Any:
    backend = cls.__new__(cls)
    backend._model = "fake-requested-model"
    backend._served = "fake-requested-model"
    backend._base_url = "http://fake.invalid"
    backend._api_key = "fake-key"
    backend._unavailable_reason = ""
    return backend


def _route_httpx(monkeypatch: pytest.MonkeyPatch) -> None:
    """One ``httpx.post`` fake for both httpx backends, routed by path.

    Patching per-backend would silently leave the LAST body in place for every
    caller — which is how the OpenAI seam first looked like it returned "".
    """
    import httpx

    def _post(url: str, *_a: Any, **_kw: Any) -> _FakeHTTPResponse:
        if url.endswith("/chat/completions"):
            return _FakeHTTPResponse(_OPENAI_BODY)
        if url.endswith("/api/chat"):
            return _FakeHTTPResponse(_OLLAMA_BODY)
        raise AssertionError(f"unexpected POST to {url}")

    monkeypatch.setattr(httpx, "post", _post)


@dataclass(frozen=True)
class _Seam:
    """One ``record_llm_raw`` seam and what its extraction is known to throw away."""

    name: str
    backend: Any
    #: What ``complete()`` reports as the served model. ``bedrock_converse`` is the
    #: odd one out: boto3's ``converse`` response carries no model id, so that
    #: backend never re-reads ``_served`` and it stays the requested id. Today's
    #: behaviour, recorded here, not endorsed.
    served: str
    #: Markers the caller never sees but the recorded body must still contain.
    #: Per-seam because the shapes differ honestly: Ollama's chat response has a
    #: single message, so its only dropped material is `message.thinking`.
    dropped: tuple[str, ...]


def _all_seams(monkeypatch: pytest.MonkeyPatch) -> list[_Seam]:
    """One live backend per ``record_llm_raw`` seam, in ``llm_backend.py`` order."""
    _route_httpx(monkeypatch)
    block_drops = (REASONING, SECOND_TEXT)
    return [
        _Seam("anthropic", _anthropic_style(AnthropicBackend), "fake-served-model", block_drops),
        _Seam(
            "anthropic_compatible",
            _anthropic_style(AnthropicCompatibleBackend),
            "fake-served-model",
            block_drops,
        ),
        _Seam("bedrock", _anthropic_style(BedrockBackend), "fake-served-model", block_drops),
        _Seam("bedrock_converse", _converse_backend(), "fake-requested-model", block_drops),
        _Seam("openai", _httpx_backend(OpenAIBackend), "fake-served-model", block_drops),
        _Seam("ollama", _httpx_backend(OllamaBackend), "fake-served-model", (REASONING,)),
    ]


# ── G1: capture is upstream of the lossy extraction ───────────────────────


class TestCaptureIsUpstreamOfTheMutation:
    """The recorded body must still contain what ``complete()`` throws away."""

    def test_every_seam_records_the_blocks_the_extraction_drops(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for seam in _all_seams(monkeypatch):
            name = seam.name
            with llm_trace.recording(f"job-{name}") as recorder:
                returned = seam.backend.complete(SYSTEM, USER)

                # What the caller gets is the truncated, stripped first block…
                assert returned == FIRST_TEXT.strip(), name
                for marker in seam.dropped:
                    assert marker not in returned, f"{name}: {marker!r} was not supposed to reach the caller"

                calls = recorder.calls()
                assert len(calls) == 1, f"{name}: expected exactly one recorded call"
                record = calls[0]

                # …and what was recorded is the whole provider response.
                body = json.dumps(record.provider_response)
                for marker in seam.dropped:
                    assert marker in body, f"{name}: the recorded body lost {marker!r}"
                # Even the whitespace `.strip()` removes is still in the body.
                assert FIRST_TEXT in body, f"{name}: the unstripped first block was not captured"

                # The lossy string is kept beside the body, for diffing — not instead of it.
                assert record.completion_text == returned, name
                assert record.system == SYSTEM and record.user == USER, name
                assert record.model_requested == "fake-requested-model", name
                assert record.model_served == seam.served, name
                assert record.usage == {"input_tokens": 11, "output_tokens": 7}, name
                assert record.latency_ms is not None and record.latency_ms >= 0.0, name
                assert record.started_at, name
                assert record.capture_error is None, name
                assert record.seq == 1, name

    def test_the_recorded_digest_is_over_the_body_not_the_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Reproducing the digest must require the body — the point of the pointer."""
        import hashlib

        for seam in _all_seams(monkeypatch):
            name = seam.name
            with llm_trace.recording(f"digest-{name}") as recorder:
                seam.backend.complete(SYSTEM, USER)
                record = recorder.calls()[0]

            blob = llm_trace.canonical_bytes(record.provider_response)
            assert record.completion_sha256 == hashlib.sha256(blob).hexdigest(), name
            assert record.completion_bytes == len(blob), name
            # …and NOT the digest of the returned string, which is what a
            # capture placed downstream of the extraction would have produced.
            text_digest = hashlib.sha256(record.completion_text.encode()).hexdigest()
            assert record.completion_sha256 != text_digest, name

    def test_no_recorder_bound_is_a_silent_no_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Outside a recorded job the seams must behave exactly as before."""
        assert llm_trace.current_recorder() is None
        for seam in _all_seams(monkeypatch):
            assert seam.backend.complete(SYSTEM, USER) == FIRST_TEXT.strip(), seam.name


# ── G2: instrumentation cannot fail a generation ──────────────────────────


class _ExplodingRecorder(LLMTraceRecorder):
    def record(self, **_kwargs: Any) -> Any:
        raise RuntimeError("recorder blew up mid-capture")


class TestRecorderNeverBreaksTheCall:
    """The input that SHOULD fail the guard: a recorder that raises."""

    def test_a_raising_recorder_does_not_fail_complete(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for seam in _all_seams(monkeypatch):
            token = llm_trace.bind(_ExplodingRecorder(job_id="boom"))
            try:
                assert seam.backend.complete(SYSTEM, USER) == FIRST_TEXT.strip(), seam.name
            finally:
                llm_trace.unbind(token)

    def test_a_raising_pointer_sink_does_not_fail_complete_or_lose_the_record(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _angry_sink(_pointer: dict[str, Any]) -> None:
            raise RuntimeError("stream is down")

        backend = _all_seams(monkeypatch)[3].backend  # bedrock_converse — the live provider
        recorder = LLMTraceRecorder(job_id="sink-boom", pointer_sink=_angry_sink)
        token = llm_trace.bind(recorder)
        try:
            assert backend.complete(SYSTEM, USER) == FIRST_TEXT.strip()
            # The record is buffered BEFORE the sink runs, so a dead stream
            # costs the event and never the trace.
            assert len(recorder.calls()) == 1
        finally:
            llm_trace.unbind(token)

    def test_an_unserializable_body_is_reported_unavailable_not_empty(self) -> None:
        """A body we could not serialize must never render as an empty completion."""
        cycle: dict[str, Any] = {}
        cycle["self"] = cycle

        with llm_trace.recording("cycle") as recorder:
            llm_trace.record_llm_raw(
                system=SYSTEM,
                user=USER,
                model_requested="m",
                model_served="m",
                provider_response=cycle,
                completion_text="whatever came back",
            )
            record = recorder.calls()[0]

        assert record.provider_response is None
        assert record.capture_error and "Circular" in record.capture_error
        # No fabricated digest over a body we do not have.
        assert record.completion_sha256 is None
        assert record.completion_bytes is None


# ── G3: the stream event carries no body ──────────────────────────────────


class _FakeStore:
    """Just enough ``JobStore`` for ``_Emitter``."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def push_event(self, job_id: str, body: dict[str, Any]) -> int:
        self.events.append(body)
        return len(self.events)


class TestStreamEventIsPointerOnly:
    def test_pointer_has_exactly_the_five_fields_and_no_prose(self) -> None:
        with llm_trace.recording("ptr") as recorder:
            llm_trace.record_llm_raw(
                system=SYSTEM,
                user=USER,
                model_requested="req",
                model_served="served",
                provider_response={"reasoning": REASONING, "text": SECOND_TEXT},
                completion_text=FIRST_TEXT,
            )
            pointer = recorder.calls()[0].pointer()

        assert set(pointer) == {"call_id", "seq", "model_served", "completion_bytes", "completion_sha256"}
        blob = json.dumps(pointer)
        for marker in _PROSE_MARKERS:
            assert marker not in blob, f"the pointer leaked {marker!r}"

    async def test_end_to_end_through_the_real_emitter_carries_no_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The live wiring: a threaded ``complete()`` → sink → ``_Emitter`` → store."""
        store = _FakeStore()
        emit = generation_pipeline._Emitter("job-e2e", store)
        recorder = LLMTraceRecorder(job_id="job-e2e", pointer_sink=generation_pipeline._llm_pointer_sink(emit))
        token = llm_trace.bind(recorder)
        try:
            backend = _all_seams(monkeypatch)[3].backend  # bedrock_converse — the live provider
            # `to_thread` copies the context, which is the only reason the
            # recorder is visible from the worker thread at all.
            assert await asyncio.to_thread(backend.complete, SYSTEM, USER) == FIRST_TEXT.strip()

            for _ in range(200):  # let the loop hop + the push task run
                if store.events:
                    break
                await asyncio.sleep(0.005)
        finally:
            llm_trace.unbind(token)

        assert len(store.events) == 1, "exactly one pointer event per recorded call"
        event = store.events[0]
        assert event["event"] == "llm_call_recorded"
        assert set(event["data"]) == {
            "ts",
            "job_id",
            "call_id",
            "seq",
            "model_served",
            "completion_bytes",
            "completion_sha256",
        }
        blob = json.dumps(event)
        for marker in _PROSE_MARKERS:
            assert marker not in blob, f"the stream event leaked {marker!r}"

    async def test_a_dead_stream_does_not_fail_the_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A store whose push raises: the generation still completes and the trace survives."""

        class _DeadStore(_FakeStore):
            async def push_event(self, job_id: str, body: dict[str, Any]) -> int:
                raise RuntimeError("redis is gone")

        emit = generation_pipeline._Emitter("job-dead", _DeadStore())
        recorder = LLMTraceRecorder(job_id="job-dead", pointer_sink=generation_pipeline._llm_pointer_sink(emit))
        token = llm_trace.bind(recorder)
        try:
            backend = _all_seams(monkeypatch)[3].backend
            assert await asyncio.to_thread(backend.complete, SYSTEM, USER) == FIRST_TEXT.strip()
            await asyncio.sleep(0.05)
            assert len(recorder.calls()) == 1
        finally:
            llm_trace.unbind(token)


# ── G4: the buffer does not outlive the job ───────────────────────────────


class TestUnbindClearsTheBuffer:
    def test_unbind_clears_the_buffer_and_the_binding(self) -> None:
        recorder = LLMTraceRecorder(job_id="job-clear")
        token = llm_trace.bind(recorder)
        llm_trace.record_llm_raw(
            system=SYSTEM,
            user=USER,
            model_requested="m",
            model_served="m",
            provider_response={"text": SECOND_TEXT},
            completion_text=FIRST_TEXT,
        )
        assert len(recorder.calls()) == 1
        assert llm_trace.current_recorder() is recorder

        llm_trace.unbind(token)

        assert recorder.calls() == [], "prompts + completions must not outlive the job"
        assert llm_trace.current_recorder() is None
        # The counts survive the clear, so "0 buffered" cannot be misread as
        # "this job made no LLM calls".
        assert recorder.stats()["recorded"] == 1
        assert recorder.stats()["buffered"] == 0

    def test_unbind_survives_a_stale_token_and_still_clears(self) -> None:
        """A raising ``unbind`` would mask the job's real exception — and keep the bodies.

        ``unbind`` is the FIRST statement in ``run_generation``'s ``finally``,
        above ``meter.snapshot()`` / ``merge_result`` / ``_persist_generation_cost``
        (#1217/#1326). ``ContextVar.reset`` raises on a token from another context
        (``ValueError``) or one already used (``RuntimeError``); either escaping
        there masks the primary error, loses the cost row, and skips ``clear()``.
        Same guard ``cost_meter.unbind`` has, plus the clear must still run.
        """
        recorder = LLMTraceRecorder(job_id="job-stale")
        token = llm_trace.bind(recorder)
        llm_trace.unbind(token)
        assert llm_trace.current_recorder() is None

        # Re-bind, buffer a body, then unbind with the ALREADY-USED token.
        token2 = llm_trace.bind(recorder)
        try:
            llm_trace.record_llm_raw(
                system=SYSTEM,
                user=USER,
                model_requested="m",
                model_served="m",
                provider_response={"body": SECOND_TEXT},
                completion_text=FIRST_TEXT,
            )
            assert len(recorder.calls()) == 1
            llm_trace.unbind(token)  # must not raise
            assert recorder.calls() == [], "a raising reset must not skip the clear"
        finally:
            llm_trace.unbind(token2)

    def test_overflow_is_counted_not_silent(self) -> None:
        recorder = LLMTraceRecorder(job_id="job-cap", max_calls=2)
        token = llm_trace.bind(recorder)
        try:
            for i in range(5):
                llm_trace.record_llm_raw(
                    system=SYSTEM,
                    user=USER,
                    model_requested="m",
                    model_served="m",
                    provider_response={"i": i},
                    completion_text=str(i),
                )
            stats = recorder.stats()
            assert stats == {"recorded": 5, "buffered": 2, "dropped": 3, "max_calls": 2}
            # The cap evicts the OLDEST, and `seq` never restarts — the gap is
            # what tells a reader the trace is incomplete.
            assert [c.seq for c in recorder.calls()] == [4, 5]
        finally:
            llm_trace.unbind(token)


# ── G5: the pipeline actually binds it — and clears it ────────────────────


class TestPipelineWiring:
    """The half that makes this PR do anything in production.

    Every guard above binds its own recorder, so deleting the bind and the
    unbind from `run_generation` leaves the whole suite green — the difference
    between "records every call" and "records nothing", and between "cleared in
    the finally" and "full prompts outlive the job". Mirrors
    `test_generation_cost_meter.py::test_meter_is_unbound_after_the_job_finishes`.
    """

    async def test_run_generation_binds_the_recorder_and_clears_it_after(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        store = MagicMock()
        store.update_status = AsyncMock()
        store.push_event = AsyncMock(return_value=1)
        store.merge_result = AsyncMock(return_value=True)

        seen: dict[str, Any] = {}

        async def _peek(_brief: Any) -> dict[str, Any]:
            # A stage running inside the job: what it hands `record_llm_raw`
            # must land in THIS job's recorder.
            seen["recorder"] = llm_trace.current_recorder()
            llm_trace.record_llm_raw(
                system=SYSTEM,
                user=USER,
                model_requested="m",
                model_served="m",
                provider_response={"text": SECOND_TEXT},
                completion_text=FIRST_TEXT,
            )
            return {"is_valid": False, "reason": "too vague", "hint": "name an asset class"}

        with (
            patch.object(generation_pipeline, "_llm_available", return_value=True),
            patch.object(generation_pipeline, "_validate_brief", new=_peek),
        ):
            await generation_pipeline.run_generation(
                job_id="job-trace",
                brief=generation_pipeline.GenerateBrief(intent="uh"),
                store=store,
            )

        recorder = seen.get("recorder")
        assert recorder is not None, "run_generation never bound a trace recorder"
        assert recorder.job_id == "job-trace"
        assert recorder.stats()["recorded"] == 1, "the stage's call never reached this job's recorder"
        assert recorder.calls() == [], "buffered prompts + bodies outlived the job"
        assert llm_trace.current_recorder() is None, "the binding leaked past the job"
