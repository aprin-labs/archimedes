"""The out-of-process generation entrypoint (#1411 spike).

The guard under test is not hypothetical. The spike's first real Lambda
invocation failed with

    redis.exceptions.ConnectionError: … Connect call failed ('127.0.0.1', 6379)

because ``archimedes/services/__init__.py`` re-exports ``generation_pipeline``,
so importing ``secrets_service`` to *fetch* the secrets already imported
``job_queue`` and froze ``REDIS_URL`` at its localhost default. The dangerous
version of that bug is not the crash — it is the near miss where a localhost
Redis happens to answer and a paid job's events go somewhere nobody reads.

The second guard here (#1793) is the same species of near miss, one layer up.
This entrypoint called ``run_generation`` bare, so the ``finally`` that hands a
consumed generation credit back when a run delivers nothing — which lives in
``generate_routes._run_with_cleanup`` — never ran for it. Nothing crashed; the
job simply ended and the payer stayed charged. #1785 has since landed a SECOND
refund — the free slot — into that same ``finally``, so this path was losing
both.

The fix is one shared seam, ``generate_routes.release_entitlements_if_undelivered``,
which both refunds live inside, and ``TestBothRunPathsReleaseTheSameThings`` is
the tripwire: it pins each run path to the seam and the seam to the full set of
release helpers, so a third refund cannot be handed to one path only.

Hermetic throughout — no Redis, no DB, no network.
"""

from __future__ import annotations

import asyncio
import re
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from archimedes.api import generate_routes
from archimedes.scripts import run_generation_job as entry


class _FakeStore:
    def __init__(self, url: str) -> None:
        self._url = url


class TestJobStoreBinding:
    def test_localhost_store_is_refused_in_production(self, monkeypatch):
        """The input that SHOULD fail: production signal on, store on loopback."""
        monkeypatch.setenv("PUBLIC_DOMAIN", "https://archimedes-arc.com")
        with pytest.raises(RuntimeError, match="refusing to start"):
            entry._require_configured_store(_FakeStore("redis://localhost:6379/0"))

    def test_localhost_store_is_fine_outside_production(self, monkeypatch):
        """Local dev genuinely runs against a loopback Redis — no false alarm."""
        monkeypatch.delenv("PUBLIC_DOMAIN", raising=False)
        store = _FakeStore("redis://localhost:6379/0")
        assert entry._require_configured_store(store) is store

    def test_a_configured_store_passes_in_production(self, monkeypatch):
        monkeypatch.setenv("PUBLIC_DOMAIN", "https://archimedes-arc.com")
        store = _FakeStore("rediss://archimedes.cache.amazonaws.com:6379/0")
        assert entry._require_configured_store(store) is store

    def test_store_is_bound_to_the_env_not_the_import_time_constant(self, monkeypatch):
        """The fix itself: REDIS_URL set AFTER job_queue was imported still wins.

        ``job_queue`` is already in ``sys.modules`` by the time this test runs
        (the test suite imports the pipeline), so its module-level ``REDIS_URL``
        is frozen — exactly the production-worker situation. The store must
        still come out bound to the value in the environment now.
        """
        monkeypatch.setenv("PUBLIC_DOMAIN", "https://archimedes-arc.com")
        monkeypatch.setenv("REDIS_URL", "rediss://set-after-import:6379/0")

        store = entry._bind_job_store()

        assert store._url == "rediss://set-after-import:6379/0"

    def test_an_explicit_store_is_passed_through_untouched(self):
        sentinel = _FakeStore("redis://localhost:6379/0")
        assert entry._bind_job_store(sentinel) is sentinel


class TestEventParsing:
    def test_brief_is_validated_by_the_production_model(self):
        brief = entry._coerce_brief({"intent": "momentum on ETFs", "max_papers": 5})
        assert brief.intent == "momentum on ETFs"
        assert brief.risk_appetite == "moderate"

    def test_brief_accepts_a_json_string(self):
        brief = entry._coerce_brief('{"intent": "carry trade"}')
        assert brief.intent == "carry trade"

    def test_out_of_range_field_is_rejected_here_too(self):
        """An offloaded worker must not accept a brief the HTTP route would refuse."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            entry._coerce_brief({"intent": "x", "max_papers": 9999})

    def test_non_object_brief_is_a_type_error(self):
        with pytest.raises(TypeError):
            entry._coerce_brief(42)

    async def test_missing_job_id_is_refused_before_any_work(self):
        with pytest.raises(ValueError, match="job_id"):
            await entry.run_job({"brief": {"intent": "x"}})


class TestBootstrap:
    def test_ssm_is_not_read_without_the_production_signal(self, monkeypatch):
        """Mirrors main.py's #1044 gate: no PUBLIC_DOMAIN, no SSM fetch."""
        monkeypatch.setattr(entry, "_BOOTSTRAPPED", False)
        monkeypatch.delenv("PUBLIC_DOMAIN", raising=False)
        monkeypatch.setenv("AWS_SSM_PATH_PREFIX", "/archimedes/prod/")

        called = []
        import archimedes.services.secrets_service as secrets

        monkeypatch.setattr(secrets, "load_ssm_secrets", lambda *a, **k: called.append(1) or 1)

        assert entry.bootstrap_environment() == 0
        assert called == []

    def test_bootstrap_is_idempotent(self, monkeypatch):
        monkeypatch.setattr(entry, "_BOOTSTRAPPED", True)
        assert entry.bootstrap_environment() == 0


class _BoundStore:
    """The store the WORKER bound, stateful enough to answer the refund question.

    ``_release_credit_if_undelivered`` decides from ``get(job_id)["status"]``,
    so a stateless mock could not tell a delivered run from a dead one — which
    is the distinction every test below turns on.

    ``list_events`` is here for the same reason and is NOT optional padding.
    ``_release_free_slot_if_undelivered`` asks ``_job_persisted_a_strategy``
    whether the run put a strategy in the library, and that helper treats a
    store with no event log as "cannot tell" and leaves the slot spent. A
    double missing this method makes every free-slot assertion below pass
    vacuously — the test would watch for a release that never had a chance to
    happen. Measured, not assumed: on the double without it, a raising pipeline
    on this path called ``restore_for_job`` once and ``release_for_job`` zero
    times, and the assertions still "passed" because nobody had written them.
    """

    def __init__(self, status: str = "running") -> None:
        self._url = "rediss://bound-by-the-worker:6379/0"
        self.status = status
        self.events: list[dict] = []

    async def get(self, job_id: str) -> dict:
        return {"status": self.status, "error": ""}

    async def update_status(self, job_id: str, status: str, *, result=None, error: str = "") -> None:
        self.status = status

    async def event_count(self, job_id: str) -> int:
        return len(self.events)

    async def touch(self, job_id: str) -> bool:
        return True

    async def push_event(self, job_id: str, payload: dict) -> int:
        self.events.append(payload)
        return len(self.events)

    async def list_events(self, job_id: str, *args, **kwargs) -> list[dict]:
        return list(self.events)


def _no_bootstrap(monkeypatch) -> None:
    """Keep ``run_job`` out of dotenv/SSM. Hermeticity, not convenience:
    ``bootstrap_environment`` calls ``load_dotenv(..., override=True)``, which
    would let a developer's real ``.env`` reach into this test."""
    monkeypatch.setattr(entry, "_BOOTSTRAPPED", True)


class TestAFailedRunOnTheScriptPathStillReleases:
    """#1793: the refunds must not depend on WHICH runner ran the generation.

    Every test here patches BOTH ledgers. The free one is an assertion where it
    is named, and hermeticity everywhere else: now that the store double can
    answer the persistence question, an unpatched
    ``free_generations.release_for_job`` opens a real DB session, and this
    module is "no Redis, no DB, no network".
    """

    async def test_a_raising_pipeline_still_hands_the_credit_back(self, monkeypatch):
        """The bug, exactly: an exception escapes ``run_generation`` and the
        payer's consumed credit has to come back. Also pins that the failure
        still reaches the invoker — a worker that absorbed it would report a
        successful invocation for a generation that never happened."""
        _no_bootstrap(monkeypatch)
        store = _BoundStore(status="running")
        boom = AsyncMock(side_effect=RuntimeError("bedrock refused the call"))

        with (
            patch("archimedes.agents.generation_pipeline.run_generation", boom),
            patch.object(generate_routes.generation_credits, "restore_for_job", return_value=True) as restore,
            patch.object(generate_routes.free_generations, "release_for_job", return_value=True),
            pytest.raises(RuntimeError, match="bedrock refused the call"),
        ):
            await entry.run_job({"job_id": "job-boom", "brief": {"intent": "x"}, "store": store})

        restore.assert_called_once_with("job-boom")

    async def test_a_failed_run_hands_back_both_the_credit_and_the_free_slot(self, monkeypatch):
        """The acceptance item this merge delivers: a failing run on the script
        path releases BOTH entitlements, not just the paid one.

        A generation is funded by a paid credit **or** by a free slot, and the
        enqueue spends whichever applied before this entrypoint is reached. The
        terminal path cannot know which it was, so it offers both back and each
        ledger no-ops for the funding it does not own — which is why both mocks
        are asserted rather than one being "the" refund.

        This is the test that goes red if a refund is moved back out of
        ``release_entitlements_if_undelivered`` into a caller's ``finally``.
        That is not hypothetical: #1785 landed
        ``_release_free_slot_if_undelivered`` in ``_run_with_cleanup``'s
        ``finally`` while this branch was open, and on that tree this path
        called ``restore_for_job`` once and ``release_for_job`` zero times.
        """
        _no_bootstrap(monkeypatch)
        store = _BoundStore(status="running")
        boom = AsyncMock(side_effect=RuntimeError("the society cannot fuse"))

        with (
            patch("archimedes.agents.generation_pipeline.run_generation", boom),
            patch.object(generate_routes.generation_credits, "restore_for_job", return_value=True) as restore,
            patch.object(generate_routes.free_generations, "release_for_job", return_value=True) as release,
            pytest.raises(RuntimeError, match="the society cannot fuse"),
        ):
            await entry.run_job({"job_id": "job-both", "brief": {"intent": "x"}, "store": store})

        restore.assert_called_once_with("job-both")
        release.assert_called_once_with("job-both")

    async def test_a_run_that_persisted_a_strategy_keeps_the_free_slot_spent(self, monkeypatch):
        """The free slot's own negative direction, on this path.

        ``_release_free_slot_if_undelivered`` is deliberately stricter than the
        paid helper: a run that persisted a strategy and then died keeps the
        slot, or the account gets the library row AND the generation back.
        Asserting it here proves the script path reaches the *real* helper and
        its real decision — not merely that some coroutine was awaited — and it
        is what makes the store double's ``list_events`` load-bearing rather
        than decorative.
        """
        _no_bootstrap(monkeypatch)
        store = _BoundStore(status="running")

        async def _persist_then_die(*, job_id: str, store, **_kwargs) -> None:
            await store.push_event(job_id, {"event": "persisted", "data": {"strategy_id": "strat-1"}})
            await store.update_status(job_id, "error", error="backtest fan-out crashed")

        with (
            patch("archimedes.agents.generation_pipeline.run_generation", _persist_then_die),
            patch.object(generate_routes.generation_credits, "restore_for_job", return_value=True) as restore,
            patch.object(generate_routes.free_generations, "release_for_job", return_value=True) as release,
        ):
            await entry.run_job({"job_id": "job-kept", "brief": {"intent": "x"}, "store": store})

        release.assert_not_called()
        restore.assert_called_once_with("job-kept")  # the PAID policy still refunds a non-done run

    async def test_the_silent_corpus_failure_shape_also_hands_the_credit_back(self, monkeypatch):
        """The shape that made this invisible (#1785's root cause): the
        insufficient-corpus branch writes an ``error`` status and **returns**.
        A cleanup keyed on exceptions would sail straight past it, which is why
        the fix is a ``finally`` and this stand-in does not raise."""
        _no_bootstrap(monkeypatch)
        store = _BoundStore(status="running")

        async def _corpus_failure(*, job_id: str, store, **_kwargs) -> None:
            await store.push_event(
                job_id,
                {"event": "error", "data": {"code": "GENERATION_UNAVAILABLE", "message": "<2 papers"}},
            )
            await store.update_status(job_id, "error", error="<2 papers")
            return  # the point of the stand-in: it does NOT raise

        with (
            patch("archimedes.agents.generation_pipeline.run_generation", _corpus_failure),
            patch.object(generate_routes.generation_credits, "restore_for_job", return_value=True) as restore,
            patch.object(generate_routes.free_generations, "release_for_job", return_value=True) as release,
        ):
            summary = await entry.run_job({"job_id": "job-thin", "brief": {"intent": "x"}, "store": store})

        assert summary["status"] == "error"
        restore.assert_called_once_with("job-thin")
        release.assert_called_once_with("job-thin")

    async def test_a_delivered_run_keeps_the_credit_spent(self, monkeypatch):
        """The other direction — the guard must not refund a run that worked."""
        _no_bootstrap(monkeypatch)
        store = _BoundStore(status="running")

        async def _delivers(*, job_id: str, store, **_kwargs) -> None:
            await store.update_status(job_id, "done")

        with (
            patch("archimedes.agents.generation_pipeline.run_generation", _delivers),
            patch.object(generate_routes.generation_credits, "restore_for_job", return_value=True) as restore,
            patch.object(generate_routes.free_generations, "release_for_job", return_value=True) as release,
        ):
            summary = await entry.run_job({"job_id": "job-ok", "brief": {"intent": "x"}, "store": store})

        assert summary["status"] == "done"
        restore.assert_not_called()
        release.assert_not_called()

    async def test_the_refund_reads_the_bound_store_not_the_import_time_singleton(self, monkeypatch):
        """This module's whole reason to exist, applied to the new code path.

        ``get_job_store()`` resolves a ``REDIS_URL`` frozen at import — for a
        worker that loads its secrets from SSM, the localhost default. Here the
        singleton says ``done`` and the bound store says ``error``: a refund
        decided from the singleton would keep a dead run's credit spent."""
        _no_bootstrap(monkeypatch)
        bound = _BoundStore(status="error")
        singleton = MagicMock()
        singleton.get = AsyncMock(return_value={"status": "done"})

        with (
            patch("archimedes.agents.generation_pipeline.run_generation", AsyncMock(return_value=None)),
            patch.object(generate_routes, "get_job_store", return_value=singleton),
            patch.object(generate_routes.generation_credits, "restore_for_job", return_value=True) as restore,
            patch.object(generate_routes.free_generations, "release_for_job", return_value=True) as release,
        ):
            await entry.run_job({"job_id": "job-bound", "brief": {"intent": "x"}, "store": bound})

        restore.assert_called_once_with("job-bound")
        release.assert_called_once_with("job-bound")
        singleton.get.assert_not_awaited()


class TestBothRunPathsReleaseTheSameThings:
    """The tripwire. #1793 happened because a refund was written into ONE
    path's ``finally``, and #1785 then did exactly that again with the free
    slot. Three pins, and all three earn their place: the discovery test alone
    stays green while a caller goes around the seam, and either path pin alone
    stays green while a new helper is added beside the seam's calls instead of
    inside it.

    Scope, stated rather than implied: discovery matches a NAMING CONVENTION on
    this module, so a refund helper named something else is invisible to it.
    That limit is written into ``release_entitlements_if_undelivered``'s
    docstring too, next to the instruction a future author will actually read.
    """

    #: Every shape a refund helper is allowed to be named. Deliberately wider
    #: than the two that exist (``_release_credit_…``, ``_release_free_slot_…``):
    #: an author reaching for ``_void_…`` or ``_refund_…`` should be caught by
    #: the tripwire, not silently exempted by it. Proved rather than assumed —
    #: an unwired ``_void_free_slot_when_undelivered`` added to
    #: ``generate_routes`` passed 21/21 under the old ``_release_\w+`` pattern
    #: and fails the discovery test under this one.
    _HELPER_PATTERN = r"_(release|void|refund|restore)_\w+_(if|when)_undelivered"

    async def test_every_release_helper_is_reached_through_the_shared_seam(self):
        helpers = sorted(n for n in dir(generate_routes) if re.fullmatch(self._HELPER_PATTERN, n))
        assert helpers, f"nothing in generate_routes matches {self._HELPER_PATTERN} — has the convention moved?"

        reached: list[str] = []
        with ExitStack() as stack:
            for name in helpers:
                stack.enter_context(
                    patch.object(
                        generate_routes,
                        name,
                        AsyncMock(side_effect=lambda *a, _n=name, **k: reached.append(_n)),
                    )
                )
            await generate_routes.release_entitlements_if_undelivered("job-any", MagicMock())

        assert sorted(reached) == helpers, (
            "a release helper is not called by release_entitlements_if_undelivered — "
            "adding it to a caller's finally gives it to that path only, which is issue #1793"
        )

    async def test_the_serving_path_releases_through_the_same_seam(self, monkeypatch):
        """``_run_with_cleanup`` must go through the seam too, not around it —
        otherwise the discovery test above passes while the two paths diverge."""
        monkeypatch.setattr(generate_routes, "_GENERATION_GATE", None)
        monkeypatch.setattr(generate_routes, "_GENERATION_GATE_LOOP", None)
        monkeypatch.setattr(generate_routes, "_WAITING_GENERATIONS", 0)
        monkeypatch.setenv("GENERATION_MAX_CONCURRENT", "1")
        monkeypatch.setenv("GENERATION_TIMEOUT_SECONDS", "30")
        store = _BoundStore(status="running")
        seam = AsyncMock(return_value=None)

        with (
            patch.object(generate_routes, "get_job_store", return_value=store),
            patch.object(generate_routes, "run_generation", AsyncMock(side_effect=RuntimeError("crash"))),
            patch.object(generate_routes, "release_entitlements_if_undelivered", seam),
        ):
            await asyncio.wait_for(generate_routes._run_with_cleanup("job-web", MagicMock(), 1), timeout=5)

        seam.assert_awaited_once_with("job-web", store)

    async def test_the_script_path_releases_through_the_same_seam(self, monkeypatch):
        """The mirror of the test above, on the path #1793 is actually about.

        Without it the suite is green while ``run_job`` awaits a single helper
        directly: pointing its deferred import at
        ``_release_credit_if_undelivered`` leaves every other pin here passing
        and quietly stops handing the free slot back — #1793 re-opened on its
        own path.

        The awaited argument matters as much as the call. It must be the store
        this worker BOUND from the event, never ``get_job_store()``: that
        singleton reads an import-time ``REDIS_URL`` which is localhost for an
        SSM-loading worker, and a refund decided from an empty localhost store
        reads every delivered run as undelivered.
        """
        _no_bootstrap(monkeypatch)
        store = _BoundStore(status="running")
        seam = AsyncMock(return_value=None)

        with (
            patch(
                "archimedes.agents.generation_pipeline.run_generation",
                AsyncMock(side_effect=RuntimeError("crash")),
            ),
            patch.object(generate_routes, "release_entitlements_if_undelivered", seam),
            pytest.raises(RuntimeError, match="crash"),
        ):
            await entry.run_job({"job_id": "job-script", "brief": {"intent": "x"}, "store": store})

        seam.assert_awaited_once_with("job-script", store)
