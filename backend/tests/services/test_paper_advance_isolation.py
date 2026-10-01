"""``paper_advance_loop`` must not be able to kill the web process.

The #1632 faulthandler traceback is a C-level abort inside psycopg2
``do_executemany``, reached from the paper-advance replay. ``except Exception``
cannot catch it. Isolation is therefore a process boundary, not a try arm.

These tests pin six properties:

1. The FastAPI lifespan never schedules ``paper_advance_loop()`` in-process.
2. The lifespan arms it UNCONDITIONALLY. The arming used to sit inside
   ``if refresh_enabled():``, so the deploy that pinned
   ``BACKTEST_REFRESH_ENABLED=false`` also disarmed the paper tick — flipping
   ``PAPER_ADVANCE_ENABLED`` would have produced no tick and no evidence in
   either direction. #1766 hoisted it out; this file keeps it out.
3. ``arm_paper_advance_for_web_tier`` refuses to spawn when the kill switch
   is off (including unset — the :211 hole), and when it is on it spawns a
   child rather than calling ``advance_all`` here.
4. A C-level death in that child (SIGSEGV, the ECS 139) leaves the parent
   alive. That is the proof ``/health`` survives the paper-advance window.
5. The child interpreter's INFO logs reach stdout. The child inherits no
   handlers from anything, so without its own ``basicConfig`` the one line
   that proves a tick ran — ``paper advance: {...}`` — never reaches
   CloudWatch, and an armed tick is unobservable.
6. Shutdown cancels the arming task, which terminates the child. Otherwise a
   task draining out of a deploy keeps ticking beside its replacement.

Hermetic: no DB, no network, no ``archimedes.main`` import. Lifespan wiring is
a source inspection of ``main.py`` — an AST walk for (2) and (6), because "is
this call nested under a branch?" and "is this call before or after the yield?"
are structural questions a substring search cannot answer, and both went green
on the regression they name while they were asked textually. The
SIGSEGV case is a real subprocess with a ``python -c`` child that never imports
archimedes; the logging case is a real subprocess that imports the module with
every boundary stubbed.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import logging
import os
import re
import signal
import subprocess
import sys
import threading
from pathlib import Path

from archimedes.services import paper_trading

BACKEND_ROOT = Path(__file__).resolve().parents[2]
MAIN_PY = BACKEND_ROOT / "archimedes" / "main.py"


class _FakeProc:
    def __init__(self, argv):
        self.argv = argv
        self.returncode = None

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


def _called_name(node: ast.Call) -> str | None:
    """``f(...)`` -> ``"f"``; ``pkg.f(...)`` -> ``"f"``; anything else -> None.

    Both spellings must be recognised, so that moving a call behind a module
    attribute cannot slip it past a guard that only knew ``ast.Name``.
    """
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _main_code() -> str:
    """``main.py`` with comments stripped, so a warning in a comment cannot
    satisfy (or fail) a call-site assertion."""
    lines = []
    for raw in MAIN_PY.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0]
        if line.strip():
            lines.append(line)
    return "\n".join(lines)


class TestLifespanRefusesTheInProcessLoop:
    def test_lifespan_does_not_call_paper_advance_loop(self):
        code = _main_code()
        assert "paper_advance_loop(" not in code, (
            "main.py schedules paper_advance_loop in-process again. A C abort "
            "on that tick kills /health. Arm arm_paper_advance_for_web_tier instead."
        )
        assert "arm_paper_advance_for_web_tier" in code

    def test_module_main_runs_the_loop_not_the_supervisor(self):
        """Child entry must not recurse into another spawn."""
        src = inspect.getsource(paper_trading._module_main)
        # Skip the docstring — it names the supervisor as the thing not to call.
        first = src.find('"""')
        second = src.find('"""', first + 3)
        body = src[second + 3 :]
        assert "asyncio.run(paper_advance_loop())" in body
        assert "arm_paper_advance_for_web_tier(" not in body
        assert "paper_advance_supervisor(" not in body


class TestArmingIsUnconditional:
    """The lift is inert unless the arming is reached on every boot.

    This is the regression that made the first attempt at #1741 a no-op: the
    ``arm_paper_advance_for_web_tier`` call was nested inside
    ``if refresh_enabled():`` — a check on a DIFFERENT flag,
    ``BACKTEST_REFRESH_ENABLED``, which task-def 216+ pinned ``false`` as the
    #1760 mitigation. Pinning ``PAPER_ADVANCE_ENABLED=true`` would have changed
    nothing and proved nothing.

    Asked structurally rather than textually. The call may sit inside a
    ``try`` (fail-soft arming is the house rule) but must not sit under any
    node that can decide whether it runs: the only permitted gate on this work
    is the one ``arm_paper_advance_for_web_tier`` applies to itself, one frame
    in, where it reads ``PAPER_ADVANCE_ENABLED``.

    "Branch" therefore means every form the gate can take, not just the
    statement one. Checking ``If``/``While`` alone let the inert shape back in
    through a ternary — ``create_task(arm(...)) if os.getenv(OTHER) else None``
    is ``ast.IfExp``, which is not an ``ast.If``, and the guard stayed green on
    it (verified by mutation). ``Match`` and ``BoolOp`` (``flag and arm(...)``)
    are the same hole in two other spellings.
    """

    @staticmethod
    def _lifespan_and_parents():
        tree = ast.parse(MAIN_PY.read_text(encoding="utf-8"))
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        lifespan = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == "lifespan"
        )
        return lifespan, parents

    def _arming_calls(self):
        lifespan, parents = self._lifespan_and_parents()
        calls = [
            node
            for node in ast.walk(lifespan)
            if isinstance(node, ast.Call) and _called_name(node) == "arm_paper_advance_for_web_tier"
        ]
        return calls, parents, lifespan

    def test_the_lifespan_arms_it(self):
        calls, _parents, _lifespan = self._arming_calls()
        assert calls, (
            "lifespan() never calls arm_paper_advance_for_web_tier — nothing arms the paper tick, "
            "so PAPER_ADVANCE_ENABLED decides nothing"
        )

    def test_the_arming_is_not_nested_under_any_branch(self):
        calls, parents, lifespan = self._arming_calls()
        for call in calls:
            node = call
            while node is not lifespan:
                node = parents[node]
                assert not isinstance(node, ast.If | ast.While | ast.IfExp | ast.Match | ast.BoolOp), (
                    f"arm_paper_advance_for_web_tier is nested under a {type(node).__name__} branch "
                    f"at main.py line {getattr(node, 'lineno', '?')}. Arming must be unconditional: "
                    "the flag check belongs inside arm_paper_advance_for_web_tier, one frame in. "
                    "Nesting it under another flag — as an if, a ternary, a match arm or an `and` — "
                    "is how the #1741 lift shipped inert under BACKTEST_REFRESH_ENABLED=false."
                )

    def test_the_dead_backtest_gate_is_not_back(self):
        """Belt and braces on the specific shape #1766 removed."""
        code = _main_code()
        assert "refresh_enabled" not in code, (
            "main.py consults refresh_enabled() again — the in-app backtest refresh is retired (#1760) "
            "and its flag must not gate anything, least of all the paper tick"
        )


class TestWebTierArming:
    def test_unset_does_not_spawn_and_does_not_advance(self, monkeypatch, caplog):
        """The :211 hole at the process boundary: name absent, must not spawn."""
        monkeypatch.delenv("PAPER_ADVANCE_ENABLED", raising=False)

        def boom(*_a, **_k):
            raise AssertionError("spawned a child with PAPER_ADVANCE_ENABLED unset")

        def advance_boom(*_a, **_k):
            raise AssertionError("advance_all ran in the web process")

        monkeypatch.setattr(paper_trading, "advance_all", advance_boom)
        with caplog.at_level(logging.WARNING, logger=paper_trading.__name__):
            result = asyncio.run(paper_trading.arm_paper_advance_for_web_tier(popen=boom))

        assert result is None
        assert any("PAPER_ADVANCE_ENABLED is off" in r.message for r in caplog.records)

    def test_kill_switch_off_does_not_spawn_and_does_not_advance(self, monkeypatch, caplog):
        monkeypatch.setenv("PAPER_ADVANCE_ENABLED", "false")

        def boom(*_a, **_k):
            raise AssertionError("spawned a child with the kill switch pulled")

        def advance_boom(*_a, **_k):
            raise AssertionError("advance_all ran in the web process")

        monkeypatch.setattr(paper_trading, "advance_all", advance_boom)
        with caplog.at_level(logging.WARNING, logger=paper_trading.__name__):
            result = asyncio.run(paper_trading.arm_paper_advance_for_web_tier(popen=boom))

        assert result is None
        assert any("PAPER_ADVANCE_ENABLED is off" in r.message for r in caplog.records)
        assert any("#1632" in r.message for r in caplog.records)

    def test_kill_switch_on_spawns_a_child_and_does_not_advance_here(self, monkeypatch):
        monkeypatch.setenv("PAPER_ADVANCE_ENABLED", "true")
        spawned: list[list[str]] = []

        def fake_popen(argv, **_kwargs):
            spawned.append(list(argv))
            return _FakeProc(argv)

        def advance_boom(*_a, **_k):
            raise AssertionError("advance_all ran in the web process")

        monkeypatch.setattr(paper_trading, "advance_all", advance_boom)
        asyncio.run(paper_trading.arm_paper_advance_for_web_tier(popen=fake_popen))

        assert spawned, "enabled arming did not spawn a child"
        assert spawned[0][-2:] == ["-m", "archimedes.services.paper_trading"]


class TestChildAbortDoesNotKillParent:
    def test_sigsegv_in_the_child_leaves_this_process_alive(self):
        """The load-bearing isolation proof.

        ECS recorded essential-container exit 139 (SIGSEGV) on the web task.
        A child that raises SIGSEGV must not take this interpreter with it.
        Getting past ``asyncio.run`` *is* the assertion; the returncode shows
        the death happened in the child.
        """
        child = "import os, signal; os.kill(os.getpid(), signal.SIGSEGV)"
        rc = asyncio.run(
            paper_trading.paper_advance_supervisor(argv=[sys.executable, "-c", child]),
        )
        assert os.getpid() > 0  # parent still here
        assert rc in (-signal.SIGSEGV, 128 + signal.SIGSEGV), (
            f"child did not die of SIGSEGV; returncode={rc!r} — isolation unproven"
        )


# The child interpreter, driven for exactly one cycle with every boundary
# replaced. Runs as a real subprocess because the thing under test is what the
# ROOT logger does in a fresh interpreter — which is not observable from
# inside a pytest process that has already configured logging.
_CHILD_DRIVER = r"""
import sys, types, asyncio

# Stub the agent pass before the loop can import the real one.
agent = types.ModuleType("archimedes.services.paper_agent_execution")
async def advance_agent_execution(session):
    return {"agent": "stub"}
agent.advance_agent_execution = advance_agent_execution
sys.modules["archimedes.services.paper_agent_execution"] = agent

from archimedes.services import paper_trading
import archimedes.db as db


class _Stop(Exception):
    pass


class _Result:
    def scalar(self):
        return True


class _Session:
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return False
    def commit(self):
        return None
    def execute(self, statement, params=None):
        return _Result()


# No init_db stub: the loop must not touch schema at all (#1818). If it ever
# does again, this child hits a real sqlite file and the guard tests in
# test_paper_advance_kill_switch.py go red.
db.get_session = lambda *a, **k: _Session()
paper_trading.advance_all = lambda session: {"deployments": 1, "appended": 1}

_real_sleep = asyncio.sleep
_seen = []
async def _sleep(seconds, *a, **k):
    _seen.append(seconds)
    if len(_seen) >= 2:
        raise _Stop
    return await _real_sleep(0)
asyncio.sleep = _sleep

try:
    paper_trading._module_main()
except _Stop:
    pass
"""


def _run_child(tmp_path, extra_env=None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(
        {
            "PAPER_ADVANCE_ENABLED": "true",
            "PAPER_ADVANCE_STARTUP_DELAY_S": "0",
            "PAPER_ADVANCE_INTERVAL_HOURS": "24",
            "DATABASE_URL": f"sqlite:///{tmp_path / 'child.db'}",
            "TESTING": "1",
            # The child is a bare interpreter, not a pytest process: nothing
            # has put ``backend/`` on its path for it.
            "PYTHONPATH": os.pathsep.join([str(BACKEND_ROOT), env.get("PYTHONPATH", "")]).rstrip(os.pathsep),
        }
    )
    env.pop("LOG_LEVEL", None)
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-c", _CHILD_DRIVER],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
        check=False,
    )


class TestChildInterpreterLogsWhatItDid:
    """An armed tick that logs nothing is indistinguishable from no tick.

    ``_module_main`` is the whole logging configuration this interpreter gets:
    the web process inherits uvicorn's handlers, the child inherits none, and
    Python's ``lastResort`` fallback drops everything below WARNING. The cycle
    summary is INFO, so before #1778 it went nowhere — the deploy could be
    observed only by watching the database.
    """

    def test_the_cycle_summary_reaches_stdout(self, tmp_path):
        result = _run_child(tmp_path)

        assert result.returncode == 0, f"child failed: {result.stderr[-2000:]}"
        assert "paper advance:" in result.stdout, (
            "the cycle summary did not reach stdout — CloudWatch would show an armed tick "
            f"that never says it ran. stdout={result.stdout!r} stderr={result.stderr[-2000:]!r}"
        )
        assert "appended" in result.stdout, "the summary reached stdout without its contents"

    def test_the_line_is_timestamped(self, tmp_path):
        """A tick line with no clock cannot be matched to a deploy or an exit."""
        result = _run_child(tmp_path)

        summary = next(line for line in result.stdout.splitlines() if "paper advance:" in line)
        assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", summary), (
            f"cycle summary is not timestamped: {summary!r}"
        )

    def test_log_level_is_honoured(self, tmp_path):
        """The level is a knob, not a hard-coded INFO."""
        result = _run_child(tmp_path, {"LOG_LEVEL": "WARNING"})

        assert result.returncode == 0, result.stderr[-2000:]
        assert "paper advance:" not in result.stdout

    def test_a_typo_in_log_level_does_not_kill_the_child(self, tmp_path):
        """``basicConfig(level="INFP")`` raises ValueError.

        A malformed env var must not be a way to stop the tick at startup —
        that is a kill switch nobody decided to pull.
        """
        result = _run_child(tmp_path, {"LOG_LEVEL": "INFP"})

        assert result.returncode == 0, f"a bad LOG_LEVEL killed the child: {result.stderr[-2000:]}"
        assert "paper advance:" in result.stdout, "a bad LOG_LEVEL silently changed the level"


class _BlockingProc:
    """A child that does not exit until it is terminated."""

    def __init__(self, argv):
        self.argv = argv
        self.returncode = None
        self.terminated = False
        self._done = threading.Event()

    def wait(self, timeout=None):
        self._done.wait(timeout)
        return self.returncode if self.returncode is not None else 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15
        self._done.set()

    def kill(self):  # pragma: no cover - only on a terminate timeout
        self.returncode = -9
        self._done.set()


class TestShutdownStopsTheChild:
    """A draining task must not keep ticking beside its replacement.

    ECS stops the old task by signalling the container's PID 1; the
    paper-advance child is not reaped by that. Without an explicit cancel, a
    rolling deploy has two children writing the same ledger rows for the length
    of the drain — the collision the fleet lock cannot see, because the
    draining task legitimately holds it.
    """

    def test_stop_cancels_a_running_arming_task(self):
        async def _forever():
            await asyncio.sleep(3600)

        async def _drive():
            task = asyncio.create_task(_forever())
            await asyncio.sleep(0)
            await paper_trading.stop_paper_advance_task(task)
            return task

        task = asyncio.run(asyncio.wait_for(_drive(), timeout=10))
        assert task.cancelled()

    def test_stop_tolerates_no_task_and_a_finished_one(self):
        """Shutdown must not raise because arming never happened."""

        async def _drive():
            await paper_trading.stop_paper_advance_task(None)

            async def _done():
                return 0

            task = asyncio.create_task(_done())
            await task
            await paper_trading.stop_paper_advance_task(task)
            return task.result()

        assert asyncio.run(asyncio.wait_for(_drive(), timeout=10)) == 0

    def test_cancelling_the_arming_task_terminates_the_child(self, monkeypatch):
        """The load-bearing half: the cancel has to reach ``proc.terminate()``.

        A ``task.cancel()`` that is never awaited can return before the
        supervisor's ``except CancelledError`` arm runs, leaving the child
        alive with nobody waiting on it.
        """
        monkeypatch.setenv("PAPER_ADVANCE_ENABLED", "true")
        spawned: list[_BlockingProc] = []

        def fake_popen(argv, **_kwargs):
            proc = _BlockingProc(argv)
            spawned.append(proc)
            return proc

        async def _drive():
            task = asyncio.create_task(paper_trading.arm_paper_advance_for_web_tier(popen=fake_popen))
            while not spawned:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)  # let the supervisor reach its wait
            await paper_trading.stop_paper_advance_task(task)
            # Observed the instant the helper RETURNS, not after the event loop
            # is torn down: ``asyncio.run`` cancels and drains whatever is left
            # when it exits, so a cancel that was never awaited would still end
            # up terminating the child by the time this function's caller looks
            # — and the test would pass on code that leaks a live child through
            # the whole drain window.
            return spawned[0].terminated, task.done(), task

        terminated, done, task = asyncio.run(asyncio.wait_for(_drive(), timeout=15))

        assert spawned, "nothing was spawned, so nothing was proven"
        assert done, "stop_paper_advance_task returned while the arming task was still running"
        assert terminated, "the child was left running after shutdown cancelled its supervisor"
        assert task.cancelled()

    def test_the_lifespan_shutdown_actually_calls_it(self):
        """Source inspection: the helper is worthless if nobody calls it.

        The cancel must live in the lifespan's SHUTDOWN half — after the
        ``yield`` — and not in the startup half, where it would cancel the task
        the lifespan has just armed and make the whole lift inert again.

        Asked of the AST, because the textual version of this test did not
        hold. Splitting the source on the first substring ``"yield"`` finds the
        word in the lifespan's own docstring ("startup before yield, shutdown
        after"), so the "shutdown half" swallowed the startup half and the
        assertion passed for a cancel placed anywhere at all — including
        immediately BEFORE the yield, the one placement it exists to forbid
        (verified: moving the shutdown block above the yield kept this file
        green). A ``Yield`` node is the statement; the word in prose is not.
        """
        tree = ast.parse(MAIN_PY.read_text(encoding="utf-8"))
        lifespan = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == "lifespan"
        )
        yields = [node for node in ast.walk(lifespan) if isinstance(node, ast.Yield)]
        assert len(yields) == 1, (
            f"lifespan has {len(yields)} yield statements, so the startup/shutdown split this test "
            "reasons about is no longer well defined — re-aim the guard rather than deleting it"
        )
        calls = [
            node
            for node in ast.walk(lifespan)
            if isinstance(node, ast.Call) and _called_name(node) == "stop_paper_advance_task"
        ]
        assert calls, (
            "lifespan() never calls stop_paper_advance_task — an ECS task draining out of a deploy "
            "keeps its paper-advance child ticking against the same ledger rows its replacement's "
            "child is starting on"
        )
        assert all(call.lineno > yields[0].lineno for call in calls), (
            f"stop_paper_advance_task is called at main.py line {min(c.lineno for c in calls)}, "
            f"before the lifespan's yield at line {yields[0].lineno} — that is the STARTUP half, "
            "where it cancels the task the lifespan has just armed and the tick never runs"
        )
