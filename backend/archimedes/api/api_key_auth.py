"""Bearer API keys: minting and verification. The whole of the key crypto, in one file.

Owner decision **D3** on #1653 — "strict, sane, secure, safe; auditable by anyone
reading the code; exposing no sensitive information". Everything an auditor needs
to check that sentence is in this module and in ``models/api_key.py``; no other
file performs a comparison, a hash, or a token parse.

The token
---------
::

    archim_<key_id>_<secret>
    ^^^^^^ ^^^^^^^^ ^^^^^^^^
    |      |        32 random bytes, urlsafe-base64 (43 chars, 256 bits)
    |      16 hex chars — PUBLIC. The database lookup handle. Not a secret.
    fixed prefix, so a leaked token is greppable in a log by its shape

``archim_<key_id>`` is the ``prefix`` the list endpoint shows. It identifies a key
without being usable as one.

Verification, step by step (this is the part to audit)
------------------------------------------------------
1. Parse. A header that is not ``Bearer archim_<id>_<secret>`` is rejected before
   any database work. No partial matching, no fallback to another scheme.
2. Look the row up by ``key_id``.
3. Compute ``sha256(salt || secret)`` and compare it to the stored digest with
   :func:`hmac.compare_digest` — the same constant-time primitive
   ``auth_guard.require_internal_agent_key`` uses for the internal key.
4. **On a miss (unknown id, or a revoked key) the comparison still runs**, against
   a fixed dummy salt and digest generated at import time. An attacker therefore
   cannot distinguish "no such key id" from "wrong secret" by timing the response,
   and cannot enumerate valid key ids. This is the one place where doing useless
   work is the correct implementation, so it is spelled out rather than optimised
   away by a later reader; ``test_unknown_key_id_still_performs_a_comparison``
   fails if someone adds the early return back.

Why not a JWT / a signed token
------------------------------
A signed token is verifiable without a database read, which sounds like the
better design until you ask how it is revoked: it is not, short of a
denylist — which is a database read, plus a way to get the denylist wrong.
Revocation that takes effect on the *next* request was a stated requirement, and
an opaque token checked against a row gives it for free.

What is NOT logged, ever
------------------------
No function in this module passes a token, a secret, or a digest to a logger. The
failure log line records the *type name* of an exception and nothing else — the
same discipline ``account_auth.better_auth_session_middleware`` already follows.
``test_no_key_material_reaches_the_logs`` drives mint → verify → revoke with a
``caplog`` handler attached at DEBUG on the root logger and asserts the secret
appears in no emitted record.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session
    from starlette.requests import Request

    from archimedes.api.account_auth import CurrentUser
    from archimedes.models.api_key import ApiKeyRecord

logger = logging.getLogger(__name__)

#: The token's fixed, greppable opening. Chosen so that a key that leaks into a
#: log, a screenshot, or a pasted terminal buffer can be *found* by searching for
#: this string — the same reason ``ghp_`` / ``sk-`` exist.
TOKEN_PREFIX = "archim_"

#: Bytes of entropy in the secret half. 32 bytes = 256 bits. The issue's A1 is a
#: test against this constant, not against a literal, so lowering it breaks a test.
SECRET_BYTES = 32

#: Hex characters in the public key id (8 random bytes). Wide enough that minting
#: collisions are not a practical concern; the mint loop below handles them anyway.
KEY_ID_HEX_CHARS = 16

_SCHEME = "Bearer "


def _digest(salt: str, secret: str) -> str:
    """``sha256(salt_bytes || secret_bytes)`` as hex.

    The salt is stored as hex and hashed as *bytes*, so the digest input has no
    ambiguous separator to get wrong. See ``models/api_key.py`` for why a plain
    sha256 (and not bcrypt/argon2) is the right primitive for a 256-bit secret.
    """
    return hashlib.sha256(bytes.fromhex(salt) + secret.encode("utf-8")).hexdigest()


#: A salt/digest pair that no real key can match, used to keep the miss path
#: doing the same work as the hit path. Generated per process at import: it is
#: never stored, never compared against anything meaningful, and its only job is
#: to consume the same time a real comparison would.
_DUMMY_SALT = secrets.token_hex(16)
_DUMMY_DIGEST = _digest(_DUMMY_SALT, secrets.token_urlsafe(SECRET_BYTES))


def parse_authorization_header(header_value: str | None) -> tuple[str, str] | None:
    """``"Bearer archim_<id>_<secret>"`` → ``(key_id, secret)``, else ``None``.

    Strict by construction: the scheme must be exactly ``Bearer``, the token must
    carry our prefix, and the remainder must split into exactly two non-empty
    parts on the single separating underscore. Anything else — a JWT, a basic-auth
    header, a truncated paste, a token with our prefix but no id — returns
    ``None`` and the request continues as unauthenticated.
    """
    if not header_value or not header_value.startswith(_SCHEME):
        return None
    token = header_value[len(_SCHEME) :].strip()
    if not token.startswith(TOKEN_PREFIX):
        return None
    body = token[len(TOKEN_PREFIX) :]
    key_id, separator, secret = body.partition("_")
    if not separator or not key_id or not secret:
        return None
    return key_id, secret


def mint(session: Session, *, user_id: str, name: str) -> tuple[ApiKeyRecord, str]:
    """Create a key for *user_id*. Returns ``(record, token)``.

    The returned token is the **only** copy that will ever exist: the record holds
    a salt and a digest, and this function is the only place the two are ever
    together. The caller must hand the token to the account owner in the response
    body and then let it fall out of scope — it is never persisted, cached, or
    logged.
    """
    from archimedes.models.api_key import ApiKeyRecord

    if not user_id:
        raise ValueError("mint requires a user_id")

    # Collisions are astronomically unlikely at 64 bits, but "unlikely" is not
    # "handled": a collision would silently overwrite another account's key, so
    # the loop re-rolls rather than trusting the odds.
    for _ in range(5):
        key_id = secrets.token_hex(KEY_ID_HEX_CHARS // 2)
        if session.get(ApiKeyRecord, key_id) is None:
            break
    else:  # pragma: no cover - requires five consecutive 64-bit collisions
        raise RuntimeError("could not allocate a unique api key id")

    secret = secrets.token_urlsafe(SECRET_BYTES)
    salt = secrets.token_hex(16)

    record = ApiKeyRecord(
        id=key_id,
        user_id=user_id,
        name=name,
        salt=salt,
        secret_hash=_digest(salt, secret),
    )
    session.add(record)
    session.flush()

    return record, f"{TOKEN_PREFIX}{key_id}_{secret}"


def verify(session: Session, key_id: str, secret: str) -> ApiKeyRecord | None:
    """The live, un-revoked key matching ``(key_id, secret)``, or ``None``.

    Constant-time on both paths — see the module docstring, step 4. A revoked key
    takes the same path as an unknown one: the comparison runs, and the result is
    discarded in favour of ``None``. Revocation is read from the row on every
    call, so it takes effect on the very next request with no cache to expire.
    """
    from archimedes.models.api_key import ApiKeyRecord

    record = session.get(ApiKeyRecord, key_id) if key_id else None

    if record is None:
        # Deliberate: burn the same work an existing key would, so timing does
        # not answer "is this a real key id?". Do NOT replace with `return None`.
        hmac.compare_digest(_digest(_DUMMY_SALT, secret), _DUMMY_DIGEST)
        return None

    matches = hmac.compare_digest(_digest(record.salt, secret), record.secret_hash)
    if not matches or record.revoked_at is not None:
        return None
    return record


def resolve_api_key_user(request: Request) -> CurrentUser | None:
    """Resolve ``Authorization: Bearer archim_…`` to the account that owns the key.

    Returns the **same** :class:`~archimedes.api.account_auth.CurrentUser` shape a
    cookie session resolves to, built from the same ``auth_users`` row — which is
    what makes "one identity, two credentials" true rather than aspirational. No
    route, dependency, quota, or paywall can tell the two apart, because after
    this function returns there is nothing left to tell apart.

    Fails closed and silent: any database or lookup error resolves to ``None``
    (an unauthenticated request), logging the exception *type* only.
    """
    from archimedes.api.account_auth import CurrentUser
    from archimedes.db import get_session
    from archimedes.models.account import AuthUser
    from archimedes.models.api_key import touch_last_used

    parsed = parse_authorization_header(request.headers.get("authorization"))
    if parsed is None:
        return None
    key_id, secret = parsed

    session = get_session()
    try:
        record = verify(session, key_id, secret)
        if record is None:
            return None

        account = session.get(AuthUser, record.user_id)
        if account is None:
            # The key outlived its account (a deletion that skipped the cascade).
            # Fail closed: a credential with no account behind it is not an
            # identity, and inventing one here would be the exact fail-soft
            # substitution CONVENTIONS.md forbids.
            return None

        user = CurrentUser(
            id=account.id,
            name=str(account.name or ""),
            email=account.email,
            email_verified=bool(account.email_verified),
            image=account.image,
        )
        if touch_last_used(session, record):
            session.commit()
        return user
    except Exception as exc:
        # Type name only — never the header, never the token, never the row.
        logger.warning("api key resolution failed: %s", type(exc).__name__)
        return None
    finally:
        session.close()
