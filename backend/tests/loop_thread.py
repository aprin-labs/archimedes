"""Was this database read done ON the event loop thread?

The 2026-09-03 outage (#1818, ``docs/incidents/2026-09-03-paper-advance-ddl-wedge.md``)
is what this module exists to make testable. ``GET /api/strategies/generated``
is an ``async def`` handler that ran ``session.query(...)`` directly, so when
that query queued behind a DDL lock it did not make one endpoint slow — it
stopped the event loop turning, which stopped ``/health`` (a sibling coroutine
on the same loop) from answering, which made the ALB mark both targets dead and
serve 504s for the whole site. 5,648,772 ms on one handler, 94 minutes of
outage.

**The detector.** ``asyncio.get_running_loop()`` succeeds on the thread that is
running the loop and raises ``RuntimeError`` on every other thread. A worker
thread from ``asyncio.to_thread`` / ``run_in_threadpool`` has no running loop,
so it raises there. That is an exact answer to the question the incident asks —
"is this blocking the loop?" — rather than a proxy for it, and it needs no
thread ids, no instrumentation of the hop, and no cooperation from the code
under test. It also cannot be satisfied by a handler that merely *looks*
asynchronous: the only way to make ``Session.query`` run where
``get_running_loop()`` raises is to genuinely move it off the loop.

**Why it RECORDS instead of raising.** Two of the handlers under guard wrap
their whole DB block in ``except Exception`` and degrade to a 200 with
``degraded: true`` (``list_strategies``, ``list_generated_strategies``). An
``AssertionError`` raised from inside ``Session.query`` would be caught by
exactly that arm and the test would pass green while the defect is present —
the guard would be decorative. So the watcher appends and the caller asserts on
the record afterwards, where nothing can swallow it.

**Why it also counts the reads that DID go off the loop.** "No query ran on the
loop" is trivially true of a request that ran no query at all — a 404 taken
before the DB, a route renamed, a fixture that stopped seeding. Every assertion
here is therefore two-sided: nothing on the loop, and something off it.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import traceback
from collections.abc import Iterator
from dataclasses import dataclass, field

from sqlalchemy.orm import Session


def on_the_event_loop() -> bool:
    """True when the CALLING thread is currently running an asyncio event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@dataclass
class QueryLog:
    """Where each ``Session.query`` call ran, split by thread."""

    on_loop: list[str] = field(default_factory=list)
    off_loop: list[str] = field(default_factory=list)

    def why(self) -> str:
        return "\n".join(f"  ON THE LOOP: {w}" for w in self.on_loop)


@contextlib.contextmanager
def watch_session_query(*, under: tuple[str, ...] = ()) -> Iterator[QueryLog]:
    """Record where every ``Session.query`` call ran.

    ``under`` scopes the recording to calls whose stack contains one of the
    named functions — pass BOTH halves of a split handler (the coroutine and its
    ``_…_sync`` twin), because on the worker thread only the sync half is on the
    stack and on the loop thread only the coroutine has to be. Scoping is not
    laziness: the auth middleware resolves an API key against the database on the
    loop before any handler runs, and that is a DIFFERENT seam from the one
    #1818 P4 names, with its own fix. Empty ``under`` records everything.

    Patching ``sqlalchemy.orm.Session.query`` (the class, not a session
    instance) is deliberate: every call site in the backend resolves the method
    through the class, so no handler can dodge the watcher by building its
    session somewhere this test does not know about.
    """
    log = QueryLog()
    original = Session.query

    def _watched(self, *args, **kwargs):
        names = [f.name for f in traceback.extract_stack()]
        if not under or any(n in names for n in under):
            trail = [n for n in names if n in under] or names[-6:]
            where = " -> ".join(trail) + f"  [thread={threading.current_thread().name}]"
            (log.on_loop if on_the_event_loop() else log.off_loop).append(where)
        return original(self, *args, **kwargs)

    Session.query = _watched
    try:
        yield log
    finally:
        Session.query = original
