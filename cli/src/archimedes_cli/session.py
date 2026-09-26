"""Session cache for the Better Auth cookie ``archimedes login`` obtains.

Stored at ``~/.config/archimedes/session.json`` by default (overridable — see "One file
per lane" below), mode ``600`` — this file holds a live session credential, so it gets the
same treatment any other credential store in this repo does (compare ``.env`` staying
gitignored, ``~/.arc-canteen/`` team credentials). The value cached here is opaque to the
CLI: it is whatever cookie(s) ``POST /api/auth/sign-in/email`` sets, forwarded verbatim
on later requests. The CLI never parses or validates the cookie
itself — same division of responsibility as the server side, which also never parses it and
just forwards it to Better Auth's ``/api/auth/get-session``
(``backend/archimedes/api/account_auth.py``, ``_fetch_session``).

**Two names, one cookie.** Better Auth issues :data:`SECURE_SESSION_COOKIE_NAME`
(``__Secure-better-auth.session_token``) on any deploy with ``useSecureCookies: true``
(``auth/auth.js`` sets that from ``production`` — so every HTTPS deploy, including
``archimedes-arc.com``) and the bare :data:`SESSION_COOKIE_NAME` everywhere else, because
the ``__Secure-`` prefix cannot legally be set without TLS. This module and
``archimedes_mcp.credentials`` both go through :func:`pick_session_cookie` rather than a
single hardcoded name, so they work against local HTTP and production alike, and store
back exactly the name+value pair that was actually issued.

**One file per lane.** The path is resolved per process, not baked in:
``--session-file`` (highest precedence), then :data:`SESSION_FILE_ENV`
(``ARCHIMEDES_SESSION_FILE``), then the default above. Before that override existed the
only lever was ``HOME``, so two CLI lanes sharing one runner shared one file and the
second lane's ``login`` silently clobbered the first lane's identity *between* two
otherwise clean calls — observed live on 2026-09-01 (issue #1752). Fleet operation is a
first-class use case here, so isolating a lane is now one flag rather than a fabricated
``HOME``. ``archimedes_mcp.credentials`` imports :func:`session_path` rather than
rebuilding it, so the same override moves the MCP server's credential too.

**Reads and writes take an advisory ``flock``** (shared to read, exclusive to write) on
the session file itself, for the case the override is *not* used and two processes do
share one path: without it a reader can observe the truncated-but-not-yet-rewritten file
a concurrent ``login`` leaves behind for a few microseconds, and read "not logged in"
from a perfectly good session. The lock is advisory — it binds the processes that go
through this module, which is every reader and writer of this file that we ship.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

try:  # pragma: no cover - the except branch is unreachable on POSIX, where CI and prod run
    import fcntl
except ImportError:  # Windows has no fcntl; see `_locked` for what that costs.
    fcntl = None  # type: ignore[assignment]

SESSION_COOKIE_NAME = "better-auth.session_token"
"""The bare cookie name Better Auth issues on sign-in over plain HTTP — matching the
fragment ``backend/archimedes/api/account_auth.py``'s ``_SESSION_COOKIE_FRAGMENT`` looks
for (``"better-auth.session_token="``, which matches this name as a substring of either
form below). This is the ONLY name a local ``docker compose`` stack can ever set: the
``__Secure-`` prefix is a browser/client-enforced rule (RFC 6265bis) forbidding it without
TLS, so local HTTP keeps using this bare name."""

SECURE_SESSION_COOKIE_NAME = f"__Secure-{SESSION_COOKIE_NAME}"
"""The name Better Auth issues instead, in production. ``auth/auth.js`` sets
``useSecureCookies: production``, which prefixes every auth cookie with ``__Secure-``
when the deploy is production — so ``archimedes-arc.com`` never sets the bare name above,
it sets this one. A client that only recognized :data:`SESSION_COOKIE_NAME` could sign in
against prod (``POST /api/auth/sign-in/email`` 200s) and then find no session cookie in
the response at all — the #1653-adjacent P0 this module fixes."""

SESSION_COOKIE_NAMES = (SECURE_SESSION_COOKIE_NAME, SESSION_COOKIE_NAME)
"""Both cookie names this CLI (and anything built on :func:`load_session`) accepts, in
preference order. Secure-prefixed first because that is the one a real deployment
actually sets; the bare name second so local HTTP keeps working. Never both at once in
practice — a given host's Better Auth config sets exactly one — but checking in this
order means whichever one shows up is picked without the caller having to know which
environment it is talking to."""


def pick_session_cookie(cookies) -> tuple[str, str] | None:
    """The session cookie in ``cookies``, as ``(name, value)`` — or ``None`` if neither
    name in :data:`SESSION_COOKIE_NAMES` is present with a non-empty string value.

    ``cookies`` is anything with a ``.get(name)`` — an ``httpx.Cookies`` (reading a
    sign-in response) or a plain ``dict`` (reading the cached session file) both work.
    Checked in :data:`SESSION_COOKIE_NAMES` order, so the ``__Secure-`` prefixed name
    wins when both are somehow present; returns the first match, never a merge of both.
    """
    for name in SESSION_COOKIE_NAMES:
        value = cookies.get(name)
        if isinstance(value, str) and value:
            return name, value
    return None


SESSION_FILE_ENV = "ARCHIMEDES_SESSION_FILE"
"""Env var naming the session cache file. Set it per lane (``ARCHIMEDES_SESSION_FILE=
/run/lane-a.json archimedes meter``) and two concurrent agents on one runner keep their
own identities. Blank or whitespace-only is treated as unset — an empty env var is how a
shell says "not configured", the same reading ``archimedes_mcp.credentials`` gives
``ARCHIMEDES_API_KEY``."""

DEFAULT_SESSION_FILE = "~/.config/archimedes/session.json"
"""The default location, in display form (help text, docs, error messages). The real
path is built from ``Path.home()`` at call time in :func:`session_path` — never cached at
import — so a test that redirects ``HOME`` still gets an isolated cache."""

_session_file_override: Path | None = None
"""Set by ``--session-file`` through :func:`set_session_file`. Process-wide by design: a
CLI process runs exactly one command, and the alternative — threading a path through
``load_session``/``save_session`` and every caller — would leave
``archimedes_mcp.credentials`` (which has no flags to thread) unable to be redirected at
all."""


def set_session_file(path: str | os.PathLike[str] | None) -> Path | None:
    """Point every later :func:`session_path` in this process at ``path``.

    ``None`` (or an empty string — an unset click option arrives as ``None``, and a
    ``--session-file ''`` is a user saying nothing rather than naming a file) restores
    the env-var-or-default resolution, which is what makes calling this unconditionally
    at the top of a command safe. ``~`` is expanded here so ``--session-file
    ~/lane-a.json`` works from a shell that did not expand it (a quoted argument, an
    ``execve`` with no shell in between).
    """
    global _session_file_override
    _session_file_override = Path(path).expanduser() if path else None
    return _session_file_override


def session_path() -> Path:
    """Where the session cache lives, resolved fresh on every call.

    Precedence: ``--session-file`` (via :func:`set_session_file`), then
    :data:`SESSION_FILE_ENV`, then :data:`DEFAULT_SESSION_FILE` built from
    ``Path.home()`` — so a test can still isolate the cache by setting ``HOME``
    (``Path.home()`` reads that on POSIX) instead of monkeypatching this function.
    """
    if _session_file_override is not None:
        return _session_file_override
    from_env = os.environ.get(SESSION_FILE_ENV, "").strip()
    if from_env:
        return Path(from_env).expanduser()
    return Path.home() / ".config" / "archimedes" / "session.json"


_LOCK_SHARED = getattr(fcntl, "LOCK_SH", 0)
_LOCK_EXCLUSIVE = getattr(fcntl, "LOCK_EX", 0)


@contextmanager
def _locked(fd: int, operation: int) -> Iterator[None]:
    """Hold an advisory ``flock`` of ``operation`` on ``fd`` for the block.

    Blocking on purpose: the lock is held for one small write, so waiting for it is
    measured in microseconds, and the alternative (``LOCK_NB`` plus a retry loop) would
    have to invent a timeout policy and a failure mode for a contention window this
    short. Released explicitly rather than relying on the close that follows, so the
    window where the fd is open and unlocked is as small as it can be.

    On a platform without ``fcntl`` (Windows) this degrades to no lock at all rather
    than to an import error that would make the whole CLI unusable there. That is a
    real, documented gap — not a claim that the file is protected everywhere.
    """
    if fcntl is None:  # pragma: no cover - POSIX-only lock; CI, prod and dev are POSIX
        yield
        return
    fcntl.flock(fd, operation)
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _read_all(fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def load_session() -> dict | None:
    """The cached session, or ``None`` if there isn't a usable one.

    Never raises. A missing file, a permissions error, a corrupt JSON body, a body that
    is not even UTF-8, or a body missing its cookie jar are all just "not logged in" from
    the caller's point of view — the CLI can always fall back to telling the user to run
    ``archimedes login``.

    The read happens under a shared ``flock``, so it never observes a concurrent
    :func:`save_session` half-written (see the module docstring). Shared locks do not
    exclude each other, so any number of readers still run at once.
    """
    path = session_path()
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        with _locked(fd, _LOCK_SHARED):
            raw = _read_all(fd).decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        os.close(fd)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    cookies = data.get("cookies")
    if not isinstance(cookies, dict) or not cookies:
        return None
    return data


def save_session(*, api_url: str, cookies: dict[str, str], email: str) -> Path:
    """Cache the session cookie(s) at :func:`session_path`, mode 600.

    Uses ``os.open`` with an explicit mode rather than ``Path.write_text`` so the file is
    never briefly world- or group-readable between creation and a follow-up ``chmod`` — and
    still ``chmod``s afterward, in case the path already existed with looser permissions
    from an older version (a reused file keeps its own mode; ``O_CREAT``'s mode argument
    only applies when the file is created).

    ``O_TRUNC`` is deliberately NOT in the open flags: the kernel would apply it before
    this process could take the lock, so a concurrent reader could catch an empty file
    even with locking in place. The truncation happens inside the exclusive lock instead,
    which is the whole point of taking one.
    """
    path = session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"api_url": api_url, "cookies": cookies, "email": email}, indent=2) + "\n"
    data = payload.encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        with _locked(fd, _LOCK_EXCLUSIVE):
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            written = 0
            while written < len(data):
                written += os.write(fd, data[written:])
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            else:  # pragma: no cover - Windows, where fchmod does not exist
                path.chmod(0o600)
    finally:
        os.close(fd)
    return path


__all__ = [
    "DEFAULT_SESSION_FILE",
    "SECURE_SESSION_COOKIE_NAME",
    "SESSION_COOKIE_NAME",
    "SESSION_COOKIE_NAMES",
    "SESSION_FILE_ENV",
    "load_session",
    "pick_session_cookie",
    "save_session",
    "session_path",
    "set_session_file",
]
