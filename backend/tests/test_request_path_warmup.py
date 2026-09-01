"""Request-path caches must be warm before a new task is an ALB target (#1713).

Two properties, both demonstrated to reject:

1. The FastAPI lifespan calls ``arm_request_path_warmup`` BEFORE ``yield`` —
   uvicorn is not listening, so the ALB cannot mark the target healthy, until
   the helper has run. A timed-out warmup must not yield (listen cold).
2. The helper actually primes the caches the Library page reads: cohort
   returns, ``strategies_list`` rigor, ``selection_bias_gate`` rigor. A
   subsequent ``_live_rigor_results_for_strategies`` must not re-run
   ``run_rigor_gate``.

Hermetic: no network, no Redis, no .env. Explore is mocked at the
``asset_market_service`` boundary so a CI box cannot hang on yfinance.
Run: env -i HOME=$HOME PATH=$PATH PYTHONPATH=backend python -m pytest \\
       backend/tests/test_request_path_warmup.py -q
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import archimedes.main as main_module
import numpy as np
import pytest
from archimedes.api import selection_bias_routes as sb_routes
from archimedes.api import strategies_routes as sr
from archimedes.services import request_path_warmup as warmup
from archimedes.services.rigor_evaluator import run_rigor_gate


def _passing_series(seed: int = 0, n: int = 250) -> list[float]:
    return np.random.default_rng(seed).normal(0.0015, 0.004, n).tolist()


def _fake_app() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace())


class _TinyProvider:
    def __init__(self, strategies):
        self._strategies = list(strategies)

    def list_strategies(self, status=None):
        return list(self._strategies)


class _SilentExplore:
    async def list_assets(self):
        return SimpleNamespace(items=[], universe_size=0, priced_count=0)


def _lifespan_code() -> str:
    fn = getattr(main_module.lifespan, "__wrapped__", main_module.lifespan)
    lines = []
    for raw in inspect.getsource(fn).splitlines():
        line = raw.split("#", 1)[0]
        if line.strip():
            lines.append(line)
    return "\n".join(lines)


def test_lifespan_warms_request_path_caches_before_yield() -> None:
    """Wiring guard: the helper is useless if lifespan never calls it, or
    calls it after uvicorn is already listening.

    MUTATION: delete the ``await arm_request_path_warmup`` call, or move it
    below ``yield``. Either way this fails.
    """
    code = _lifespan_code()
    assert "arm_request_path_warmup" in code, (
        "lifespan no longer arms request-path warmup — every deploy ships a cold fleet"
    )
    # Match the statement, not the word in the function docstring
    # ("startup before yield, shutdown after").
    lines = [line.strip() for line in code.splitlines()]
    warmup_at = next(i for i, line in enumerate(lines) if "arm_request_path_warmup" in line)
    yield_at = next(i for i, line in enumerate(lines) if line == "yield" or line.startswith("yield "))
    assert warmup_at < yield_at, (
        "request-path warmup runs AFTER yield, so the ALB can mark the target "
        "healthy before the Library caches are primed"
    )
    # A try/except Exception around the await is the #1713 bug: timeout
    # fail-softs and the task still listens cold.
    warmup_block = "\n".join(lines[warmup_at:yield_at])
    assert "except Exception" not in warmup_block, (
        "lifespan swallows warmup failures between arm and yield — a timed-out "
        "warmup would still become an ALB-ready target"
    )
    assert "evaluate_rigor_gate" not in code
    assert "BacktestResultRecord" not in code


def test_warmup_is_skipped_under_testing() -> None:
    """The suite sets TESTING=1; warmup must not run the cohort gate at boot."""
    assert warmup.warmup_enabled() is False


def test_warmup_kill_switch_is_off_when_requested(monkeypatch) -> None:
    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.setenv("REQUEST_PATH_WARMUP", "0")
    assert warmup.warmup_enabled() is False
    monkeypatch.setenv("REQUEST_PATH_WARMUP", "true")
    assert warmup.warmup_enabled() is True


@pytest.mark.asyncio
async def test_arm_is_a_noop_when_disabled(monkeypatch) -> None:
    """MUTATION: drop the ``warmup_enabled`` gate. Under TESTING this would
    then enter ``_prime_sync`` and this spy would fire."""
    called = []

    def _should_not_run():
        called.append("prime")
        return {}

    monkeypatch.setattr(warmup, "_prime_sync", _should_not_run)
    result = await warmup.arm_request_path_warmup(_fake_app())
    assert result == {}
    assert called == []


@pytest.mark.asyncio
async def test_warmup_budget_interrupts_a_sync_prime(monkeypatch) -> None:
    """THE GUARD: ``asyncio.wait_for`` cannot interrupt a sync prime on the
    event loop. The budget is real only if the prime runs in a worker.

    Production primes are ``get_all_daily_returns`` / ``run_rigor_gate`` —
    no await. ``time.sleep`` is the same shape. A test that patches
    ``_prime`` with ``asyncio.sleep(3600)`` proves nothing about that path:
    sleep *is* cancellable at an await.

    MUTATION: drop ``to_thread`` and run ``_prime_sync`` on the loop. The
    3s sleep then blocks the loop, ``wait_for`` cannot fire, and the
    2s wall-clock bar fails. Dropping ``wait_for`` entirely also fails.
    """
    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.setenv("REQUEST_PATH_WARMUP", "true")
    monkeypatch.setattr(warmup, "WARMUP_BUDGET_SECONDS", 0.05)

    def _hang():
        # Blocking, no await — identical to a hung blob-decode. 3s is >>
        # the 0.05s budget and would fail the 2s bar if wait_for cannot
        # interrupt; asyncio.sleep would be cancellable at an await and
        # would not prove the production path.
        time.sleep(3)
        return {"hung": True}

    monkeypatch.setattr(warmup, "_prime_sync", _hang)
    started = time.perf_counter()
    with pytest.raises(warmup.RequestPathWarmupTimeout, match="refusing to listen cold"):
        await warmup.arm_request_path_warmup(_fake_app())
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, f"sync prime was not interrupted ({elapsed:.2f}s); the 60s cap is fake"


@pytest.mark.asyncio
async def test_timed_out_warmup_does_not_yield_lifespan(monkeypatch) -> None:
    """A timed-out warmup must not become an ALB-ready target (#1713).

    MUTATION: restore the lifespan ``except Exception`` swallow around
    ``arm_request_path_warmup``. The context then yields, which is listen
    / register cold. This test drives the real lifespan, not a source grep.
    """
    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.setenv("REQUEST_PATH_WARMUP", "true")
    monkeypatch.setattr(warmup, "WARMUP_BUDGET_SECONDS", 0.05)
    released = threading.Event()

    def _hang():
        released.wait(timeout=3600)
        return {}

    monkeypatch.setattr(warmup, "_prime_sync", _hang)
    yielded: list[str] = []
    try:
        with pytest.raises(warmup.RequestPathWarmupTimeout):
            await asyncio.wait_for(
                _enter_lifespan(yielded),
                timeout=15.0,
            )
        assert yielded == [], (
            "lifespan yielded after a warmup timeout — uvicorn would listen "
            "and the ALB would register a cold task"
        )
    finally:
        released.set()


async def _enter_lifespan(yielded: list[str]) -> None:
    async with main_module.lifespan(_fake_app()):
        yielded.append("yielded")


def test_arm_uses_to_thread_so_the_budget_can_fire() -> None:
    """Source pin: wait_for over an async _prime that does sync work is fake.

    MUTATION: replace ``asyncio.to_thread(_prime_sync)`` with ``_prime(app)``.
    This fails. The interruptibility test above is the behavioral twin.
    """
    source = inspect.getsource(warmup.arm_request_path_warmup)
    assert "to_thread" in source
    assert "wait_for" in source
    assert "_prime_sync" in source


@pytest.mark.asyncio
async def test_prime_fail_soft_when_the_library_is_unavailable(monkeypatch) -> None:
    """A missing strategy corpus must not abort lifespan."""

    def _boom():
        raise RuntimeError("strategy corpus missing")

    monkeypatch.setattr(sr, "strategy_provider", _boom)
    warmed = await warmup._prime(_fake_app())
    assert warmed["cohort_returns"] is False
    assert warmed["strategies_list"] is False
    assert warmed["selection_bias_gate"] is False


@pytest.mark.asyncio
async def test_prime_populates_the_strategies_list_cache(monkeypatch) -> None:
    """THE GUARD (#1713): after warmup, the next Library list must not
    re-run ``run_rigor_gate``.

    MUTATION: delete the ``_live_rigor_results_for_strategies(library)``
    call in ``_prime``. The post-warmup call-count then grows and this
    fails. The two strategies are a real curated pair so look-ahead and
    cohort PBO run on the production path; returns are injected at the
    ``get_all_daily_returns`` boundary.
    """
    library = sr.strategy_provider().list_strategies()[:2]
    assert len(library) == 2, "need a pair for cohort PBO"
    returns = {s.id: _passing_series(i) for i, s in enumerate(library)}
    provider = _TinyProvider(library)

    monkeypatch.setattr(sr, "strategy_provider", lambda: provider)
    monkeypatch.setattr(sb_routes, "_provider", lambda: provider)
    monkeypatch.setattr(
        "archimedes.services.backtest_repository.get_all_daily_returns",
        lambda session, ids: {sid: list(returns[sid]) for sid in ids if sid in returns},
    )
    monkeypatch.setattr(
        "archimedes.services.asset_market_service.asset_market_service",
        _SilentExplore(),
    )

    gate_calls: list[str] = []
    real_gate = run_rigor_gate

    def _counting(*args, **kwargs):
        gate_calls.append(kwargs.get("strategy_id") or (args[0] if args else "?"))
        return real_gate(*args, **kwargs)

    monkeypatch.setattr("archimedes.services.rigor_evaluator.run_rigor_gate", _counting)

    app = _fake_app()
    try:
        warmed = await warmup._prime(app)
        assert warmed["cohort_returns"] is True
        assert warmed["strategies_list"] is True
        assert warmed["selection_bias_gate"] is True
        assert warmed["explore_assets_armed"] is True
        after_warmup = len(gate_calls)
        assert after_warmup >= len(library), f"warmup did not run the cohort gate (calls={gate_calls!r})"

        sr._live_rigor_results_for_strategies(library)
        assert len(gate_calls) == after_warmup, (
            f"post-warmup Library list re-ran run_rigor_gate "
            f"({len(gate_calls) - after_warmup} extra calls) — the task would "
            "still serve the 12s cold hit"
        )
    finally:
        task = getattr(app.state, "explore_warmup_task", None)
        if task is not None:
            await task


@pytest.mark.asyncio
async def test_prime_calls_the_selection_bias_gate(monkeypatch) -> None:
    """Companion to the cache-hit test: the gate route itself is invoked, not
    only the strategies-list helper. Those two caches have different keys.

    MUTATION: drop the ``await evaluate_rigor_gate(...)`` call. ``called``
    stays empty.
    """
    library = sr.strategy_provider().list_strategies()[:2]
    provider = _TinyProvider(library)
    monkeypatch.setattr(sr, "strategy_provider", lambda: provider)
    monkeypatch.setattr(sb_routes, "_provider", lambda: provider)
    monkeypatch.setattr(
        "archimedes.services.backtest_repository.get_all_daily_returns",
        lambda session, ids: {sid: _passing_series(i) for i, sid in enumerate(ids)},
    )
    monkeypatch.setattr(
        "archimedes.services.asset_market_service.asset_market_service",
        _SilentExplore(),
    )
    called: list[int] = []

    async def _spy(*, strictness: int):
        called.append(strictness)
        return SimpleNamespace()

    monkeypatch.setattr(sb_routes, "evaluate_rigor_gate", _spy)

    app = _fake_app()
    try:
        warmed = await warmup._prime(app)
        assert warmed["selection_bias_gate"] is True
        assert called == [sb_routes.DEFAULT_LEVEL]
    finally:
        task = getattr(app.state, "explore_warmup_task", None)
        if task is not None:
            await task


@pytest.mark.asyncio
async def test_explore_arm_failure_does_not_undo_rigor_warmup(monkeypatch) -> None:
    """Explore is best-effort. A broken task-arm must not skip the Library cache.

    MUTATION: move the explore ``create_task`` above the rigor primes, unguarded.
    A missing ``app.state`` would then abort before the Library caches fill.
    """
    library = sr.strategy_provider().list_strategies()[:1]
    provider = _TinyProvider(library)
    monkeypatch.setattr(sr, "strategy_provider", lambda: provider)
    monkeypatch.setattr(sb_routes, "_provider", lambda: provider)
    monkeypatch.setattr(
        "archimedes.services.backtest_repository.get_all_daily_returns",
        lambda session, ids: {sid: _passing_series(0) for sid in ids},
    )
    monkeypatch.setattr(sb_routes, "evaluate_rigor_gate", AsyncMock(return_value=SimpleNamespace()))
    monkeypatch.setattr(sr, "_live_rigor_results_for_strategies", lambda lib: {})

    # No `.state` — the explore arm raises; rigor steps already completed.
    warmed = await warmup._prime(SimpleNamespace())
    assert warmed["cohort_returns"] is True
    assert warmed["strategies_list"] is True
    assert warmed["selection_bias_gate"] is True
    assert warmed["explore_assets_armed"] is False
