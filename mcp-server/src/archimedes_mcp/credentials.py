"""Credential resolution — one credential, chosen in a fixed order, never printed.

Two ways to authenticate as an account, in this order:

1. **``ARCHIMEDES_API_KEY``** → sent as ``Authorization: Bearer <key>``. This is the
   scoped-API-key lane opened by owner decision **D3** on
   `PR #1653 <https://github.com/aprin-labs/archimedes/pull/1653>`_ and implemented on branch
   ``dbrowneup/1653-scoped-api-keys`` (``docs/api/api-keys.md`` there). **That branch is
   not merged to ``main`` at the time of writing**, so against today's production a bearer
   key resolves to no user and the API answers ``401``. The header is written now, exactly
   as that branch specifies, so this server starts working on the day it merges and needs
   no change here. Nothing in this file blocks on it: the fallback below carries the
   server until then.
2. **The CLI session cache** at ``~/.config/archimedes/session.json`` — the cookie
   ``archimedes login`` writes at mode ``600``, or wherever ``ARCHIMEDES_SESSION_FILE``
   points, which is how two agents on one runner keep separate identities (#1752: set it
   in this server's ``env`` block, one file per agent, and their sessions stop clobbering
   each other). Loaded through ``archimedes_cli.session.load_session``: imported, not
   reimplemented. Copying that loader would mean two definitions of "is there a usable
   session", and the copy would be the one that drifts — the exact second-surface failure
   mode this whole server was scoped to avoid. That cookie is one of *two* names depending on which host issued it —
   ``__Secure-better-auth.session_token`` in production, the bare
   ``better-auth.session_token`` on local HTTP (``archimedes_cli.session`` picks between
   them; see :func:`~archimedes_cli.session.pick_session_cookie`) — and this module sends
   back exactly the name it was captured under, never a hardcoded one.

**One credential goes on the wire, never two.** If both are present the API key wins here,
and no ``Cookie`` header is sent at all. The server side pins the opposite precedence
(cookie wins, ``dbrowneup/1653-scoped-api-keys`` commit ``f60fb27a``); sending only one
means that disagreement can never decide which account a call acts as.

**The secret never leaves this module in readable form.** :class:`Credential` carries it
in a field excluded from ``repr``/``str``, exposes it only through
:meth:`Credential.auth_headers` / :meth:`Credential.auth_cookies`, and this module logs
nothing at all. Same treatment ``cli/src/archimedes_cli/session.py`` gives the cache file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from archimedes_cli.session import SESSION_COOKIE_NAME, load_session, pick_session_cookie, session_path

API_KEY_ENV = "ARCHIMEDES_API_KEY"
API_URL_ENV = "ARCHIMEDES_API_URL"
DEFAULT_API_URL = "https://archimedes-arc.com"

KIND_API_KEY = "api_key"
KIND_SESSION_COOKIE = "session_cookie"


@dataclass(frozen=True, repr=False)
class Credential:
    """One resolved credential. ``kind`` and ``source`` are safe to show; ``_secret`` is not.

    ``repr=False`` plus the explicit :meth:`__repr__` below is the whole leak defence for
    accidental interpolation — an f-string, a ``logger.debug("%s", cred)``, a pytest
    assertion diff, or a traceback frame that renders locals all go through one of the two
    dunders, and both are redacted.
    """

    kind: str
    source: str
    _secret: str = field(repr=False)
    cookie_name: str = SESSION_COOKIE_NAME
    """Which of ``SESSION_COOKIE_NAMES`` this secret was actually captured under —
    ``__Secure-better-auth.session_token`` for a session cached against production,
    the bare name for one cached against local HTTP. Meaningless (and ignored) for an
    API-key credential; defaults to the bare name only because a dataclass field needs
    *some* default, never read in that branch."""

    def auth_headers(self) -> dict[str, str]:
        """Headers that carry this credential. Empty for a cookie credential."""
        if self.kind == KIND_API_KEY:
            return {"Authorization": f"Bearer {self._secret}"}
        return {}

    def auth_cookies(self) -> dict[str, str]:
        """Cookies that carry this credential, under the name it was captured as.
        Empty for an API-key credential."""
        if self.kind == KIND_SESSION_COOKIE:
            return {self.cookie_name: self._secret}
        return {}

    def __repr__(self) -> str:  # pragma: no cover - exercised via str()/f-string in tests
        return f"Credential(kind={self.kind!r}, source={self.source!r}, secret=<redacted>)"

    __str__ = __repr__


def resolve_credential() -> Credential | None:
    """The credential to use, or ``None`` if there isn't one.

    Never raises, for the same reason ``load_session`` never raises: "no usable
    credential" is an ordinary answer a tool has to be able to report, not an exception
    that kills a long-lived stdio server. A blank or whitespace-only ``ARCHIMEDES_API_KEY``
    is treated as unset rather than as a key that will certainly 401 — an empty env var is
    how a shell says "not configured".
    """
    raw_key = os.environ.get(API_KEY_ENV, "")
    key = raw_key.strip()
    if key:
        return Credential(kind=KIND_API_KEY, source=API_KEY_ENV, _secret=key)

    session = load_session()
    if session is None:
        return None
    picked = pick_session_cookie(session["cookies"])
    if picked is None:
        # A cache holding some other cookie is not a session this API can use. Same
        # fail-quiet posture as `load_session` itself. Checks BOTH the `__Secure-`
        # prefixed name (a session cached against production) and the bare one (local
        # HTTP) — see `archimedes_cli.session.pick_session_cookie`.
        return None
    cookie_name, token = picked
    return Credential(kind=KIND_SESSION_COOKIE, source=str(session_path()), _secret=token, cookie_name=cookie_name)


def resolve_api_url(credential_session: dict | None = None) -> str:
    """Base URL: ``ARCHIMEDES_API_URL``, else the cached session's host, else the default.

    Same precedence as the CLI's ``_api_url_session_option`` (``cli.py``): a session cached
    against a non-default server must not have its cookie sent to a different host, which
    would surface as a mystifying 401.
    """
    explicit = os.environ.get(API_URL_ENV, "").strip()
    if explicit:
        return explicit
    session = load_session() if credential_session is None else credential_session
    cached = (session or {}).get("api_url")
    return cached if isinstance(cached, str) and cached else DEFAULT_API_URL


def credential_help() -> str:
    """The remedy string every ``no_credential`` failure carries. Names both lanes."""
    return (
        f"No credential. Either export {API_KEY_ENV}=<key> (minted at "
        f"POST /api/account/keys, sent as 'Authorization: Bearer <key>'), or run "
        f"`archimedes login` to cache a session at {session_path()}."
    )


__all__ = [
    "API_KEY_ENV",
    "API_URL_ENV",
    "DEFAULT_API_URL",
    "KIND_API_KEY",
    "KIND_SESSION_COOKIE",
    "Credential",
    "credential_help",
    "resolve_api_url",
    "resolve_credential",
]
