"""Canonical account identity for FastAPI — one identity, two credentials.

FastAPI never parses Better Auth cookies. It asks the colocated Better Auth
service for the session and exposes a small immutable user object to route
dependencies. Missing or unavailable auth fails closed on protected routes
without breaking intentionally public APIs.

**This module is the single chokepoint where a request acquires an identity.**
Two credentials can establish that identity, and they are tried in this order:

1. the Better Auth **session cookie** (browsers, and the quickstart's cookie jar);
2. an ``Authorization: Bearer archim_…`` **API key** (#1653 decision D3 — CI jobs
   and agent runners that would otherwise have to re-POST the account password
   every seven days to refresh a cookie).

Both produce the same :class:`CurrentUser`, built from the same ``auth_users``
row. Nothing downstream — no route, dependency, quota, paywall or rate limit —
has, or needs, a branch on which one was used: a key is a credential, never a
bypass, and the way that property is *guaranteed* rather than merely intended is
that the difference stops existing here.

The one place the difference is legible is ``request.state.auth_credential``
(``"session"`` / ``"api_key"`` / ``None``). Exactly two things read it:
:func:`require_session_credential`, which keeps key management off the keys
themselves, and the telemetry classifier, which counts a keyed caller as an agent
instead of mislabelling it a human. Neither grants or withholds account access.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)
_SESSION_COOKIE_FRAGMENT = "better-auth.session_token="

#: Values of ``request.state.auth_credential``. Which credential proved the
#: identity — never which identity, and never what it is allowed to do.
CREDENTIAL_SESSION = "session"
CREDENTIAL_API_KEY = "api_key"


@dataclass(frozen=True, slots=True)
class CurrentUser:
    id: str
    name: str
    email: str
    email_verified: bool
    image: str | None = None


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _user_from_payload(payload: object) -> CurrentUser | None:
    if not isinstance(payload, dict):
        return None
    user = payload.get("user")
    session = payload.get("session")
    if not isinstance(user, dict) or not isinstance(session, dict):
        return None

    expires_at = _parse_datetime(session.get("expiresAt"))
    if expires_at is None or expires_at <= datetime.now(UTC):
        return None

    user_id = user.get("id")
    email = user.get("email")
    if not isinstance(user_id, str) or not user_id or not isinstance(email, str) or not email:
        return None

    return CurrentUser(
        id=user_id,
        name=str(user.get("name") or ""),
        email=email,
        email_verified=bool(user.get("emailVerified")),
        image=user.get("image") if isinstance(user.get("image"), str) else None,
    )


async def _fetch_session(request: Request) -> object:
    internal_url = os.getenv("BETTER_AUTH_INTERNAL_URL", "http://127.0.0.1:3000").rstrip("/")
    forwarded_proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.netloc)
    headers = {
        "cookie": request.headers.get("cookie", ""),
        "host": host,
        "x-forwarded-host": host,
        "x-forwarded-proto": forwarded_proto,
        "user-agent": request.headers.get("user-agent", ""),
    }
    async with httpx.AsyncClient(timeout=2.0) as client:
        response = await client.get(f"{internal_url}/api/auth/get-session", headers=headers)
    if response.status_code in {401, 403}:
        return None
    response.raise_for_status()
    return response.json()


async def better_auth_session_middleware(request: Request, call_next):
    """Resolve the request's account identity, once, from either credential.

    Cookie first (the browser majority, and the existing behaviour, unchanged),
    then the API key. The key path is attempted only when the cookie produced no
    user, so a signed-in browser never pays for a database read it does not need,
    and ``parse_authorization_header`` rejects any header that is not one of our
    tokens before touching the database at all.
    """
    request.state.current_user = None
    request.state.auth_credential = None

    if _SESSION_COOKIE_FRAGMENT in request.headers.get("cookie", ""):
        try:
            request.state.current_user = _user_from_payload(await _fetch_session(request))
        except Exception as exc:
            logger.warning("Better Auth session resolution failed: %s", type(exc).__name__)
        if request.state.current_user is not None:
            request.state.auth_credential = CREDENTIAL_SESSION

    if request.state.current_user is None:
        # Imported here, not at module scope: the key path pulls in the ORM and
        # the database module, and this file is imported by ``main.py`` before
        # those are configured.
        from archimedes.api.api_key_auth import resolve_api_key_user

        keyed_user = resolve_api_key_user(request)
        if keyed_user is not None:
            request.state.current_user = keyed_user
            request.state.auth_credential = CREDENTIAL_API_KEY

    return await call_next(request)


def get_current_user(request: Request) -> CurrentUser | None:
    return getattr(getattr(request, "state", None), "current_user", None)


def get_auth_credential(request: Request) -> str | None:
    """Which credential proved this request's identity, if any.

    ``CREDENTIAL_SESSION`` / ``CREDENTIAL_API_KEY`` / ``None``. Read by exactly
    two callers (see the module docstring); it is not an authorisation input.
    """
    return getattr(getattr(request, "state", None), "auth_credential", None)


def require_current_user(request: Request) -> CurrentUser:
    user = get_current_user(request)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Session"},
        )
    return user


def require_session_credential(request: Request) -> CurrentUser:
    """Require an interactive session — an API key is refused with 403.

    Guards the key-management endpoints, and only those. The reason is
    containment: if a key could mint keys, a single leaked token would be able to
    issue itself successors, and revoking the token an operator knows about would
    not end the compromise. Requiring the human credential to manage credentials
    means the blast radius of a leaked key is bounded by what that key can do —
    and what it can do is exactly what the account can do, minus this.

    401 when there is no identity at all, 403 when the identity is real but the
    credential is the wrong kind: the caller is authenticated, they are simply
    holding a credential this surface does not accept.
    """
    user = require_current_user(request)
    if get_auth_credential(request) != CREDENTIAL_SESSION:
        raise HTTPException(
            status_code=403,
            detail=(
                "API keys cannot manage API keys. Sign in with your account session to create, list, or revoke keys."
            ),
        )
    return user
