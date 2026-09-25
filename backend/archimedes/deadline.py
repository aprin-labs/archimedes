"""Hard wall-clock deadlines for awaitables that may ignore cancellation (#1592).

``asyncio.wait_for`` is the obvious tool and it is not sufficient here. On
timeout it cancels the inner task and then **awaits the cancellation to
complete** (``_cancel_and_wait``). That is the right default for ordinary
code and the wrong one for a liveness path: an awaitable parked in something
uncancellable does not stop when asked, so ``wait_for`` waits with it and the
caller's "timeout" bounds nothing at all.

That is not hypothetical for this repo's chain path. ``web3`` 7.16 routes
EVERY async JSON-RPC request through
``HTTPSessionManager.async_cache_and_return_session``, which opens with::

    async with async_lock(self.session_pool, self._lock):

and ``async_lock`` is ``await loop.run_in_executor(thread_pool, lock.acquire)``
over a **class-level ``threading.Lock``** and a **5-worker**
``ThreadPoolExecutor`` (``web3/_utils/async_caching.py``,
``web3/_utils/http_session_manager.py``). Three consequences, all live in the
2026-08-31 incident:

1. ``lock.acquire`` runs in a worker thread and is uninterruptible. Cancelling
   the ``run_in_executor`` future never unblocks the thread.
2. That await sits ENTIRELY OUTSIDE the ``aiohttp`` ``ClientTimeout`` the
   provider is configured with — the request timeout starts after the session
   is in hand, so it cannot bound the queue in front of it.
3. Five stalled acquires exhaust the pool and every later RPC in the process
   queues behind them, unbounded. This is how one dark RPC endpoint starves an
   entire event loop rather than failing one call.

So the deadline here is deliberately **abandoning**, not cancelling-and-waiting:
it asks the task to stop, keeps a reference so the interpreter does not warn
about a never-retrieved result, and returns to the caller immediately. The
abandoned task finishes on its own schedule (or never); what matters is that
the caller's clock is its own.

Cost of abandonment, stated plainly: the task keeps whatever resource it holds
until it unwinds, so this is a tool for READ-ONLY probes on a liveness path.
Do not wrap a state-changing call in it — an abandoned write is a write whose
outcome you stopped watching.

THE LIFECYCLE INVARIANT (#1632)
-------------------------------
"The task keeps whatever resource it holds" is the sentence with teeth, and the
2026-08-31 exit-139 deaths are what it costs when nobody enforces it:

    **No code path in this process may free a resource that an abandoned task
    may still be touching. Every teardown of a resource reachable from an
    abandonable call must first drain the strays to ZERO, and must decline to
    free anything if the drain does not reach zero.**

Abandonment inverts the usual ownership rule. Normally the caller owns the call:
it awaits, the call finishes, the caller tears down. Abandonment *severs* that —
the caller walks away while the callee runs on — so every teardown the caller
still performs afterwards is a free() racing a live reader.

Honest about how far this is established: the race is measured (see
``BoundedAsyncHTTPProvider.disconnect`` and
``tests/test_abandoned_call_session_lifecycle.py``); the step from it to #1632's
SIGSEGV is #1632's stated *hypothesis* and is not reproduced. The invariant is
worth holding either way — closing a transport under an in-flight request is a
defect on its own terms, and it is cheap not to.

:func:`drain_abandoned` is the enforcement primitive. Declining to free is always
the correct fallback, because the two failure modes are not comparable: leaking a
socket in a process that is on its way out is invisible, whereas a use-after-free
is exactly the class of fault that dies without a Python traceback — which is why
#1632 needed ``faulthandler`` (#1633) before it could be diagnosed at all.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable

logger = logging.getLogger(__name__)

# Strong references to abandoned tasks. asyncio only holds weak references to
# running tasks, so without this an abandoned task can be garbage-collected
# mid-flight; the set also keeps "Task exception was never retrieved" noise off
# the logs via the reaper below. Entries are removed as the tasks complete, so
# this cannot grow without bound while calls eventually unwind.
_ABANDONED: set[asyncio.Task] = set()


def _reap(task: asyncio.Task) -> None:
    """Drop the strong reference and consume the abandoned task's outcome."""
    _ABANDONED.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.debug("abandoned task finished with %s: %s", type(exc).__name__, exc)


def abandoned_task_count() -> int:
    """How many deadline-exceeded tasks are still unwinding in this process.

    Exposed for tests and for anyone diagnosing a stuck RPC endpoint: a number
    that climbs and does not fall means calls are being abandoned faster than
    they unwind, which is the signature of a dark endpoint rather than a slow
    one.
    """
    return len(_ABANDONED)


async def drain_abandoned(timeout: float) -> int:
    """Wait up to ``timeout`` for the in-flight strays; return how many remain.

    **Zero is the only safe number to tear down on.** An abandoned task is still
    holding whatever the caller stopped watching — for the RPC path that is an
    aiohttp connection sitting in its connector's ``_acquired`` set (#1632). A
    non-zero return means a teardown MUST NOT proceed; see
    ``BoundedAsyncHTTPProvider.disconnect``.

    Waits on a snapshot because :func:`asyncio.wait` needs a fixed set, but
    reports ``abandoned_task_count()``, the LIVE number: a stray abandoned while
    the drain was running is still a stray, and answering from the snapshot
    would report a clear floor that no longer holds.

    Never raises on the empty case. ``asyncio.wait`` raises ``ValueError`` on an
    empty set, and "nothing was abandoned" is the overwhelmingly common shutdown
    — turning the quiet path into an exception would break every ordinary
    teardown to guard the rare one.
    """
    strays = set(_ABANDONED)
    if strays:
        await asyncio.wait(strays, timeout=timeout)
    return len(_ABANDONED)


async def run_with_deadline[T](awaitable: Awaitable[T], timeout: float, *, label: str) -> T:
    """Await ``awaitable`` for at most ``timeout`` seconds, then give up on it.

    Returns the awaitable's result, or re-raises whatever it raised, exactly as
    a direct ``await`` would. Raises :class:`TimeoutError` when the deadline is
    blown — at which point the underlying task is cancelled but NOT awaited (see
    the module docstring for why that difference is the whole point).
    """
    task = asyncio.ensure_future(awaitable)
    done, _pending = await asyncio.wait({task}, timeout=timeout)
    if task in done:
        return task.result()

    task.cancel()
    _ABANDONED.add(task)
    task.add_done_callback(_reap)
    logger.warning(
        "DEADLINE_EXCEEDED: %s did not answer within %.2fs — abandoning the call (%d in flight)",
        label,
        timeout,
        len(_ABANDONED),
    )
    raise TimeoutError(f"{label} exceeded its {timeout:.2f}s deadline")
