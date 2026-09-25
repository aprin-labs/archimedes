"""Serving-latency guard (#1666): the backtest fan-out is bounded, and the
process-wide default executor is chosen rather than inherited.

Two defects, one package:

1. ``agents/debate_engine.py`` ran ``asyncio.gather(*(asyncio.to_thread(_backtest, p)
   for p in pool))`` — up to ``DEBATE_POOL_MAX`` (10) simultaneous ``cerebro.run()``
   calls. backtrader is pure Python and GIL-bound, so those are 10 *runnable* CPU
   threads contending with the uvicorn event loop on a 1-2 vCPU task. Worse, they
   ran on the **default** executor, the same pool ``asset_market_service``,
   ``traces_routes``, ``chat_routes``, ``portfolio_routes`` and ``strategies_routes``
   block on.
2. ``grep -rn "set_default_executor" backend/`` returned nothing, so that shared pool's
   width was whatever CPython picked — ``min(32, os.cpu_count() + 4)``, i.e. 5 on a
   1-vCPU task. Ten backtest threads exhaust a 5-wide pool outright.

``GENERATION_MAX_CONCURRENT`` caps *pipelines*, not *threads*, so it never bounded
either of these.

These tests are behavioural, not cosmetic: reverting either fix flips them (the
adversarial transcript is in the PR body).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import logging
import os
from types import SimpleNamespace

import archimedes.main as main_module
import pytest
from archimedes.agents import debate_engine as de

# ── Doubles ───────────────────────────────────────────────────────────────────


def _proposal(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, strategy_spec={"name": name}, cited_paper_ids=[])


def _ev() -> SimpleNamespace:
    return SimpleNamespace(success=True, rigor=SimpleNamespace(passing=True), backtest=None, error=None)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    # real_data_enabled() is off under TESTING; belt-and-suspenders so no test
    # here can reach yfinance even if the evaluator double is bypassed.
    monkeypatch.setenv("TESTING", "1")


# ── 1. The backtest pool is named and bounded ────────────────────────────────


def test_backtest_pool_is_a_named_bounded_thread_pool() -> None:
    ex = de._backtest_executor()
    assert isinstance(ex, concurrent.futures.ThreadPoolExecutor)
    assert ex._max_workers <= 2, (
        f"backtest pool widened to {ex._max_workers}. cerebro.run() is GIL-bound; "
        "more than 2 concurrent runs buys no throughput and starves the event loop."
    )
    assert ex._thread_name_prefix == "debate-backtest"


def test_backtest_pool_is_a_singleton() -> None:
    assert de._backtest_executor() is de._backtest_executor()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 2),  # unset → 2
        ("1", 1),  # narrowing is allowed
        ("2", 2),
        ("10", 2),  # ← the adversarial input: widening is CLAMPED, not honoured
        ("999", 2),
        ("0", 1),
        ("-4", 1),
        ("garbage", 2),  # unparseable → the safe default, never the CPython pool
    ],
)
def test_backtest_worker_knob_can_narrow_but_never_widen(monkeypatch, raw, expected) -> None:
    """``DEBATE_BACKTEST_WORKERS=10`` must NOT produce a 10-wide pool.

    The clamp is the guard: re-widening the fan-out is exactly the regression
    this issue exists to prevent, and an env knob is the likeliest way back in.
    """
    monkeypatch.delenv("DEBATE_BACKTEST_WORKERS", raising=False)
    if raw is not None:
        monkeypatch.setenv("DEBATE_BACKTEST_WORKERS", raw)
    assert de._backtest_max_workers() == expected


# ── 2. The fan-out actually submits there — not to the default pool ──────────


async def test_critic_rigor_submits_to_the_bounded_pool_not_the_default(monkeypatch) -> None:
    """Every backtest goes through the dedicated executor, and the loop's default
    executor is left untouched.

    Both halves matter. Counting submissions proves the work landed on the bounded
    pool; asserting ``loop._default_executor`` is unchanged proves it did not *also*
    (or instead) go to the shared pool — ``asyncio.to_thread`` lazily instantiates
    that pool on first use, so its identity changing is a direct fingerprint of the
    old code path.
    """
    monkeypatch.setattr(
        "archimedes.services.fusion_evaluator.evaluate_fusion_spec",
        lambda spec, **kw: _ev(),
    )

    ex = de._backtest_executor()
    submitted: list[object] = []
    real_submit = ex.submit

    def _spy(fn, /, *args, **kwargs):
        submitted.append(fn)
        return real_submit(fn, *args, **kwargs)

    monkeypatch.setattr(ex, "submit", _spy)

    loop = asyncio.get_running_loop()
    default_before = getattr(loop, "_default_executor", None)

    pool = [_proposal(f"P{i}") for i in range(4)]
    out = await de._critic_rigor(pool, num_trials=len(pool))

    assert len(out) == 4, "the bounded pool must still run every candidate, just not all at once"
    assert len(submitted) == 4, (
        "backtests did not go through the dedicated pool — they fell back to "
        "asyncio.to_thread / the shared default executor."
    )
    assert getattr(loop, "_default_executor", None) is default_before, (
        "the backtest fan-out instantiated or used the loop's DEFAULT executor; "
        "that is the pool the serving routes block on."
    )


async def test_critic_rigor_source_does_not_use_to_thread() -> None:
    """Source-level backstop for the behavioural test above.

    ``asyncio.to_thread`` is the one-token way to reintroduce the unbounded
    fan-out, and it is easy to add back without noticing the executor argument
    that disappeared.
    """
    src = inspect.getsource(de._critic_rigor)
    code = "\n".join(line.split("#", 1)[0] for line in src.splitlines())
    assert "to_thread" not in code
    assert "run_in_executor" in code


# ── 3. main.py's lifespan chooses the default executor explicitly ────────────


async def test_install_default_executor_binds_an_explicitly_sized_named_pool(caplog) -> None:
    loop = asyncio.get_running_loop()
    default_before = getattr(loop, "_default_executor", None)
    log = logging.getLogger("archimedes.startup")

    with caplog.at_level(logging.INFO, logger="archimedes.startup"):
        ex = main_module._install_default_executor(log)
    try:
        assert loop._default_executor is ex, "set_default_executor was not called with our pool"
        assert ex._thread_name_prefix == "archimedes-default"
        assert ex._max_workers == main_module._default_executor_workers()
        assert ex._max_workers >= main_module._DEFAULT_EXECUTOR_FLOOR, (
            "the default pool must exceed the 10-thread debate proposer fan-out, "
            "or serving blocking-calls queue behind a generation."
        )

        # The ONE startup INFO line the issue asks for — it carries the number
        # that decides whether executor exhaustion (not just GIL contention) was
        # the live term on this task size.
        lines = [r.getMessage() for r in caplog.records if "os.cpu_count()" in r.getMessage()]
        assert len(lines) == 1, f"expected exactly one cpu_count startup line, got {lines}"
        assert f"os.cpu_count()={os.cpu_count()}" in lines[0]
        assert f"default_executor_max_workers={ex._max_workers}" in lines[0]
        assert f"cpython_default_would_be={min(32, (os.cpu_count() or 1) + 4)}" in lines[0]
    finally:
        ex.shutdown(wait=False)
        loop._default_executor = default_before


def test_lifespan_installs_the_default_executor_before_anything_else() -> None:
    """Wiring guard: the helper above is useless if the lifespan never calls it.

    Position matters too — the pool must be bound before any startup step can
    park work on the (lazily created) inherited one.
    """
    fn = getattr(main_module.lifespan, "__wrapped__", main_module.lifespan)
    body = [line.split("#", 1)[0] for line in inspect.getsource(fn).splitlines()]
    body = [line for line in body if line.strip()]
    calls = [i for i, line in enumerate(body) if "_install_default_executor(" in line]
    assert calls, "lifespan no longer installs an explicit default executor"
    first_other_startup_step = next(
        (i for i, line in enumerate(body) if "seed_from_manifest" in line),
        len(body),
    )
    assert calls[0] < first_other_startup_step


@pytest.mark.parametrize(
    ("override", "expect"),
    [
        (None, None),  # unset → the computed width
        ("24", 24),
        ("64", 64),
        ("4096", 64),  # clamped: an absurd value must not spawn 4096 threads
        ("0", None),  # 0 / garbage fall back to the computed width
        ("garbage", None),
    ],
)
def test_default_executor_width_is_explicit_and_overridable(monkeypatch, override, expect) -> None:
    monkeypatch.delenv("SERVER_THREAD_POOL_WORKERS", raising=False)
    computed = min(32, max(main_module._DEFAULT_EXECUTOR_FLOOR, (os.cpu_count() or 1) * 4))
    if override is not None:
        monkeypatch.setenv("SERVER_THREAD_POOL_WORKERS", override)
    assert main_module._default_executor_workers() == (computed if expect is None else expect)
