"""An abandoned RPC call must never have its connection freed under it (#1632).

BACKGROUND. #1593 gave the chain provider an *abandoning* deadline: when a call
blows its wall-clock budget, ``run_with_deadline`` cancels the task, keeps a
strong reference to it, and returns to the caller immediately. That is correct
and load-bearing — it is what keeps ``/health`` answering when the Arc RPC goes
dark — and nothing here weakens it.

What it also does is invert ownership. The caller walks away while the callee
runs on, so every teardown the caller performs afterwards is a ``free()`` racing
a live reader. #1632 recorded two backend deaths with a bare ``exit 139``
(SIGSEGV) under RPC distress, last log line
``DEADLINE_EXCEEDED: eth-rpc eth_chainId did not answer within 3.00s —
abandoning the call (5 in flight)``.

THE HAZARD, MEASURED (``TestTheHazardIsReal`` below, not prose). At the instant
``run_with_deadline`` hands ``TimeoutError`` back, the stray has not run once:
there is no ``await`` between ``task.cancel()`` and the ``raise``, so the event
loop has had no chance to deliver the cancellation. The stray is therefore still
inside ``session.post`` with a live connection in the connector's ``_acquired``
set — and ``AsyncHTTPProvider.disconnect`` closes every session it can reach,
which ``BaseConnector._close`` implements as ``proto.close()`` over exactly that
set. Same object, two owners, one of them not watching.

THE INVARIANT THIS FILE PINS:

    A teardown of the bounded provider begins only when the abandoned-stray
    count is ZERO. If the strays do not drain inside the budget, the session is
    quarantined — left open and left in the cache — rather than freed.

Declining to free is the safe branch, and deliberately so: the two failure modes
are not comparable. A leaked socket in a process that is on its way out is
invisible; a use-after-free is the class of fault that dies without a Python
traceback — the exact silence #1632 was opened about.

WHAT IS AND IS NOT CLAIMED. The teardown-under-a-live-reader is measured here.
#1632's step from there to SIGSEGV is its own stated *hypothesis*, and nothing in
this file reproduces a crash — the fault-injection test proves the path survives,
not that it used to die. Closing a transport under an in-flight request is worth
fixing whether or not it turns out to be the production cause.

**These are guards, not coverage.** Every one was run against the unfixed code
(``BoundedAsyncHTTPProvider.disconnect`` deleted, so teardown falls through to
web3's unconditional one) and the failing output is in the PR body. A guard
never shown to reject is a guess.

Hermetic: the RPC is a real ``aiohttp`` server on 127.0.0.1 with an ephemeral
port — no network, no Arc, no DB, no ``.env``. It is a *real* server on purpose:
the question is what native aiohttp/TLS-adjacent machinery does when its
transport is closed mid-request, and a ``unittest.mock`` double answers nothing
about that. The RPC endpoint is the boundary, and this is the boundary's double.

Run:
    env -i HOME=$HOME PATH=$PATH PYTHONPATH=backend python -m pytest \
        backend/tests/test_abandoned_call_session_lifecycle.py -q
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from aiohttp import ClientError, ClientTimeout, web
from archimedes import deadline as deadline_mod
from archimedes.chain.client import BoundedAsyncHTTPProvider
from archimedes.deadline import abandoned_task_count, drain_abandoned, run_with_deadline
from web3.providers import AsyncHTTPProvider
from web3.providers.rpc.utils import ExceptionRetryConfiguration

_BACKEND_DIR = str(Path(__file__).resolve().parent.parent)

# Small enough that a blown deadline is instant, large enough that a healthy
# loopback round-trip (sub-millisecond) never trips it by accident.
_BUDGET = 0.15

# A hard stop so a regression FAILS the suite instead of hanging it — the same
# reason test_health_always_answers.py carries one, and not a theoretical one:
# the ``asyncio.wait_for`` mutation of ``run_with_deadline`` (i.e. #1632's
# anti-goal, "don't remove the abandonment behaviour") parks forever on the
# uncancellable stray below, so without this the adversarial run that is
# supposed to prove the guard rejects cannot even finish.
#
# Applied ONLY where a hang is actually reachable, i.e. the uncancellable-stray
# test. Everything else awaits bare, on purpose — see _abandon_one.
_HARD_STOP_SECONDS = 8.0


async def _hard_stopped(awaitable, what: str):
    """Await something that must not be able to hang, whatever the mutation.

    Deliberately NOT ``asyncio.wait_for``: since 3.11 ``asyncio.TimeoutError``
    *is* ``TimeoutError``, so a ``wait_for`` wrapper could not tell its own hard
    stop apart from the ``TimeoutError`` that a blown deadline is supposed to
    raise — and every test here is about that exception. Waiting on a task keeps
    the two signals distinct: the hard stop is an ``AssertionError``, and
    ``task.result()`` re-raises the awaitable's own exception untouched.
    """
    task = asyncio.ensure_future(awaitable)
    done, _pending = await asyncio.wait({task}, timeout=_HARD_STOP_SECONDS)
    if task not in done:
        task.cancel()
        raise AssertionError(f"{what} never returned within {_HARD_STOP_SECONDS}s — it is supposed to be bounded")
    return task.result()


class FakeRPC:
    """A loopback JSON-RPC endpoint that answers only when the test says so.

    "Answers after the deadline" is #1632's literal fault-injection ask, and a
    gate is how you get it *deterministically*: the response cannot possibly
    arrive before ``release.set()``, so no test here depends on a sleep being
    longer than a budget on a loaded CI box.
    """

    def __init__(self) -> None:
        self.hits = 0
        self.release: asyncio.Event | None = None
        self._runner: web.AppRunner | None = None
        self.url = ""

    async def _handle(self, request: web.Request) -> web.Response:
        self.hits += 1
        body = await request.json()
        assert self.release is not None
        await self.release.wait()
        return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": "0x4cef52"})

    async def start(self) -> None:
        self.release = asyncio.Event()
        app = web.Application()
        app.router.add_post("/", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{self._runner.addresses[0][1]}/"

    async def stop(self) -> None:
        assert self.release is not None
        self.release.set()  # never leave a handler parked on the gate
        if self._runner is not None:
            await self._runner.cleanup()


def _provider(url: str, budget: float = _BUDGET) -> BoundedAsyncHTTPProvider:
    """The production provider shape, with retries pinned to 1.

    ``retries=1`` is for determinism, not realism: web3's retry loop would fire
    a *second* request for the same abandoned call, so the stray count and the
    server hit count would stop being readable. The production client passes its
    own explicit ``ExceptionRetryConfiguration`` for the different reason
    documented in ``ChainClient.__init__`` (web3's default of five would
    multiply the total budget by five).
    """
    return BoundedAsyncHTTPProvider(
        url,
        total_budget_seconds=budget,
        request_kwargs={"timeout": ClientTimeout(total=budget * 10)},
        exception_retry_configuration=ExceptionRetryConfiguration(
            errors=(ClientError, TimeoutError), retries=1, backoff_factor=0.0
        ),
    )


def _sessions(provider: AsyncHTTPProvider) -> list:
    """The live sessions in web3's per-provider cache — what disconnect() walks."""
    return [session for _key, session in provider._request_session_manager.session_cache.items()]


async def _abandon_one(provider: BoundedAsyncHTTPProvider) -> None:
    """Fire one call that blows its deadline. Returns with the stray in flight.

    The bare ``await`` is load-bearing and must NOT be wrapped in ``_hard_stopped``:
    that helper spawns a task and awaits ``asyncio.wait``, which yields to the loop
    — and one turn of the loop is all it takes for the cancelled stray to unwind and
    be reaped, so every caller that inspects the stray count would then read 0.
    A hang is unreachable here anyway: the provider carries an aiohttp
    ``ClientTimeout``, which bounds the request even with the deadline mutated away.
    """
    with pytest.raises(TimeoutError):
        await provider.make_request("eth_chainId", [])


@pytest.fixture
async def rpc():
    server = FakeRPC()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture(autouse=True)
async def _no_stray_leaks():
    """``deadline._ABANDONED`` is process-global; a leak poisons later tests.

    Same process-memo hazard as ``_clear_rigor_cache`` / ``_clear_vault_owner_cache``
    / ``_clear_health_probe_cache`` in conftest, with sharper teeth: this module's
    whole subject is the count being non-zero, so a stray escaping into the next
    test would make a *teardown* test see a quarantine it never asked for.
    """
    assert abandoned_task_count() == 0, "a previous test leaked an abandoned task into this one"
    yield
    for task in list(deadline_mod._ABANDONED):
        task.cancel()
    await drain_abandoned(2.0)
    deadline_mod._ABANDONED.clear()


class TestTheHazardIsReal:
    """The precondition, in numbers. Not a fix — the thing the fix is about."""

    async def test_a_blown_deadline_leaves_a_stray_holding_an_acquired_connection(self, rpc):
        provider = _provider(rpc.url)

        await _abandon_one(provider)

        # No await has run since task.cancel(), so the loop has not delivered the
        # cancellation and the stray cannot have unwound.
        assert abandoned_task_count() == 1, "the stray should still be in flight the instant the caller gives up"
        assert rpc.hits == 1, "the request did reach the server — this is a real in-flight connection"

        session = _sessions(provider)[0]
        assert session.closed is False
        assert len(session.connector._acquired) == 1, (
            "the stray's connection is in the connector's _acquired set — the exact set "
            "BaseConnector._close() calls proto.close() over"
        )

        rpc.release.set()
        assert await drain_abandoned(2.0) == 0


class TestTheInvariant:
    """Teardown begins at zero strays, or it does not begin."""

    async def test_teardown_never_begins_while_a_stray_is_in_flight(self, rpc):
        """THE guard. Ordering, not final state — final state is identical either way.

        Both fixed and unfixed code end with the session closed, because this
        stray unwinds quickly once the loop gets a turn. What differs is *when*
        web3's unconditional closer is entered: with the fix it is entered only
        after the drain reports zero; without it, it is entered with the stray
        still holding a live connection.

        So the probe records the stray count at the moment the closer starts.
        Unfixed, that is 1 — and the PR body carries the failure.
        """
        provider = _provider(rpc.url)
        await _abandon_one(provider)
        assert abandoned_task_count() == 1

        strays_at_teardown: list[int] = []
        unconditional_closer = AsyncHTTPProvider.disconnect

        async def _probe(self):
            strays_at_teardown.append(abandoned_task_count())
            await unconditional_closer(self)

        rpc.release.set()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(AsyncHTTPProvider, "disconnect", _probe)
            await provider.disconnect()

        assert strays_at_teardown == [0], (
            f"web3's unconditional session closer ran with {strays_at_teardown} abandoned "
            "call(s) still in flight — that is a free() racing a live reader (#1632)"
        )

    async def test_teardown_still_closes_once_the_strays_have_drained(self, rpc):
        """ANTI-GOAL GUARD: the gate must not turn into 'never tear down'.

        Refusing forever would be a resource leak dressed up as a fix. Once the
        strays are gone, disconnect must do the whole job web3 does.
        """
        provider = _provider(rpc.url)
        await _abandon_one(provider)
        session = _sessions(provider)[0]
        rpc.release.set()

        await provider.disconnect()

        assert abandoned_task_count() == 0
        assert session.closed is True, "a drained provider must actually close its session"
        assert _sessions(provider) == [], "and must clear the cache, exactly as web3's disconnect does"

    async def test_a_stray_that_ignores_cancellation_quarantines_the_session(self, rpc, caplog):
        """The branch that matters when the drain CANNOT succeed.

        The stand-in is the one already established in
        ``test_health_always_answers.py::test_gives_up_on_an_awaitable_that_refuses_to_be_cancelled``
        — an awaitable that swallows cancellation, modelling web3 7.16's
        ``await loop.run_in_executor(thread_pool, lock.acquire)``, which runs
        uninterruptibly in a worker thread and entirely outside aiohttp's request
        timeout.

        The gate is process-wide by design: it asks "is any call abandoned?",
        never "is *this* session's call abandoned?". It cannot ask the narrower
        question — #1593's stall happens BEFORE a session is in hand, so a stray
        frequently owns no particular session yet. Conservative is the only sound
        direction; the cost of a false quarantine is a leaked socket.
        """
        provider = _provider(rpc.url)

        rpc.release.set()
        assert await provider.make_request("eth_chainId", []) == {"jsonrpc": "2.0", "id": 0, "result": "0x4cef52"}
        session = _sessions(provider)[0]
        assert session.closed is False

        stop = asyncio.Event()

        async def _swallows_cancellation():
            while True:
                try:
                    await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    if stop.is_set():
                        raise
                    continue

        try:
            with pytest.raises(TimeoutError):
                await _hard_stopped(
                    run_with_deadline(_swallows_cancellation(), 0.05, label="unstoppable"),
                    "run_with_deadline on an uncancellable awaitable",
                )
            assert abandoned_task_count() == 1

            with caplog.at_level(logging.ERROR, logger="archimedes.chain.client"):
                await provider.disconnect()

            assert session.closed is False, "a session was freed while an undrainable stray was in flight (#1632)"
            assert _sessions(provider) == [session], (
                "the quarantined session must stay in the cache — that reference is what "
                "keeps Connection.__del__ from closing the transport from a GC pass instead"
            )
            assert "SESSION_QUARANTINED" in caplog.text, "quarantine must be loud; a silent leak is undiagnosable"
        finally:
            stop.set()

    async def test_teardown_with_nothing_abandoned_closes_without_waiting(self, rpc):
        """The quiet path, which is nearly every shutdown.

        ``asyncio.wait`` raises ``ValueError`` on an empty set, so a drain that
        does not special-case "no strays" converts every ordinary teardown into
        an exception while guarding the rare one. Delete the ``if strays:`` in
        ``drain_abandoned`` and this test is what fails.
        """
        provider = _provider(rpc.url)
        rpc.release.set()
        await provider.make_request("eth_chainId", [])
        session = _sessions(provider)[0]

        assert await drain_abandoned(5.0) == 0
        await provider.disconnect()

        assert session.closed is True


class TestAntiGoals:
    """#1632 forbids removing the abandonment behaviour. It is still here."""

    async def test_the_deadline_still_abandons_rather_than_waiting_for_the_call(self, rpc):
        """The caller's clock stays its own — the property #1593 exists for.

        Bounded by the deadline, NOT by the RPC: the server never answers during
        the measured window (its gate is still shut), so a caller that waited for
        the call would not return at all.
        """
        provider = _provider(rpc.url)
        loop = asyncio.get_running_loop()

        started = loop.time()
        with pytest.raises(TimeoutError):
            await provider.make_request("eth_chainId", [])  # bare: see _abandon_one
        elapsed = loop.time() - started

        assert elapsed < _BUDGET * 4, f"caller waited {elapsed:.3f}s on a call it was supposed to abandon"
        assert abandoned_task_count() == 1, "the call must be abandoned, not awaited to completion"
        rpc.release.set()
        assert await drain_abandoned(2.0) == 0


# --------------------------------------------------------------------------
# Fault injection, in a subprocess, because "does not crash" is a claim about
# the PROCESS. An in-process assertion cannot make it: a SIGSEGV takes pytest
# down with it and reports nothing. Here a native crash is observable as a
# non-zero return code plus faulthandler's frames on stderr — which is the same
# instrument #1633 armed in main.py, pointed at the path #1632 suspects.
# --------------------------------------------------------------------------

_FAULT_INJECTION = """
import asyncio, faulthandler, sys
faulthandler.enable()

from aiohttp import ClientError, ClientTimeout, web
from web3.providers import AsyncHTTPProvider
from web3.providers.rpc.utils import ExceptionRetryConfiguration
from archimedes.chain.client import BoundedAsyncHTTPProvider
from archimedes.deadline import abandoned_task_count, drain_abandoned

CONCURRENT = 8
BUDGET = 0.15

# Pin the invariant inside the fault injection too, not only alongside it:
# record how many strays were in flight each time web3's unconditional session
# closer was entered. Every entry must read zero.
strays_at_teardown = []
_unconditional_closer = AsyncHTTPProvider.disconnect

async def _recording_closer(self):
    strays_at_teardown.append(abandoned_task_count())
    await _unconditional_closer(self)

AsyncHTTPProvider.disconnect = _recording_closer

async def main():
    gate = asyncio.Event()
    hits = []

    async def handle(request):
        hits.append(1)
        body = await request.json()
        await gate.wait()                      # answer strictly AFTER the deadline
        return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": "0x4cef52"})

    app = web.Application()
    app.router.add_post("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = "http://127.0.0.1:%d/" % runner.addresses[0][1]

    provider = BoundedAsyncHTTPProvider(
        url,
        total_budget_seconds=BUDGET,
        request_kwargs={"timeout": ClientTimeout(total=BUDGET * 10)},
        exception_retry_configuration=ExceptionRetryConfiguration(
            errors=(ClientError, TimeoutError), retries=1, backoff_factor=0.0
        ),
    )

    # PHASE 1 — the race at its sharpest, repeated. Each iteration tears the
    # provider down with EXACTLY ONE stray guaranteed in flight: there is no
    # await between run_with_deadline's task.cancel() and its raise, so the loop
    # has not delivered the cancellation and the connection is still acquired.
    for i in range(CONCURRENT):
        try:
            await provider.make_request("eth_chainId", [])
            raise AssertionError("call %d should have blown its deadline" % i)
        except TimeoutError:
            pass
        assert abandoned_task_count() == 1, (i, abandoned_task_count())
        await provider.disconnect()             # <- free() racing a live reader
        assert await drain_abandoned(10.0) == 0, "stray %d never unwound" % i

    # PHASE 2 — the same fault at width: many calls blowing their deadlines at
    # once, which is the shape the production log recorded ("5 in flight").
    results = await asyncio.gather(
        *(provider.make_request("eth_chainId", []) for _ in range(CONCURRENT)),
        return_exceptions=True,
    )
    assert all(isinstance(r, TimeoutError) for r in results), results
    await provider.disconnect()

    gate.set()                                  # the RPC finally answers everybody
    assert await drain_abandoned(10.0) == 0, "strays never unwound"
    assert len(hits) == CONCURRENT * 2, hits

    # A provider that survived the storm must still work.
    assert (await provider.make_request("eth_chainId", []))["result"] == "0x4cef52"
    await provider.disconnect()

    await runner.cleanup()

    assert strays_at_teardown, "the recording closer never ran — the probe proves nothing"
    assert set(strays_at_teardown) == {0}, (
        "web3's unconditional session closer was entered with strays in flight: %r" % (strays_at_teardown,)
    )

asyncio.run(main())
print("SURVIVED")
"""


def test_an_rpc_that_answers_after_the_deadline_does_not_crash_the_interpreter():
    """#1632 item 3: the fault-injection no-crash proof.

    Eight concurrent calls all blow their deadline against a loopback RPC that
    answers only later; teardown is invoked in the same breath as the timeouts;
    then the server answers all eight strays. The subprocess must exit 0 with no
    ``Fatal Python error``, and the provider must still serve a request
    afterwards.

    Note what is and is not claimed: this demonstrates the abandonment path
    unwinding cleanly under fault injection on this interpreter and this
    aiohttp. It does not reproduce the production SIGSEGV — nobody has — and a
    green run here is not by itself the week-of-prod evidence #1632 item 4 asks
    for.
    """
    result = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", _FAULT_INJECTION],
        cwd=_BACKEND_DIR,
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "PYTHONPATH": _BACKEND_DIR,
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert "Fatal Python error" not in result.stderr, f"native crash under fault injection:\n{result.stderr}"
    assert "Segmentation fault" not in result.stderr, result.stderr
    assert result.returncode == 0, (
        f"fault injection exited {result.returncode}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "SURVIVED" in result.stdout
