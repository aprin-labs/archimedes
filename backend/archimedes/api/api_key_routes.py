"""``/api/account/keys`` — create, list, revoke an account's API keys (#1653 D3).

Three endpoints, on the account surface next to ``/api/account/usage``, gated by
:func:`~archimedes.api.account_auth.require_session_credential`: **an API key
cannot manage API keys.** See that function for why.

What each one exposes, precisely
--------------------------------
``POST``   returns the key row **plus** the token, once. This is the only response
           anywhere in the system that contains a token, and it exists for exactly
           one round trip. If the caller loses it, the key is unrecoverable and the
           remedy is to revoke it and mint another — which is the correct behaviour
           for a credential we cannot read back either.

           **Transactional in effect**, and that is a safety property rather than a
           tidiness one: because the token is unrecoverable, a row that is committed
           but not returned is a permanently consumed slot out of
           ``MAX_KEYS_PER_ACCOUNT``, findable only by listing and revoking it. So the
           response body is built *before* the commit and every side effect runs
           *after* it, individually suppressed. See :func:`create_api_key`.

``GET``    returns ``id``, ``name``, ``prefix`` (``archim_<id>`` — the non-secret
           half), ``created_at``, ``last_used_at``, ``revoked_at``. There is no
           code path that can add the token: the serialiser is
           ``ApiKeyRecord.to_payload``, the record does not hold a token, and the
           response model below has no field for one. Two independent reasons,
           because one is a comment and the other is enforced.

``DELETE`` revokes, and answers **404** for a key that is not the caller's —
           the same answer as a key that does not exist. A 403 would confirm the
           id is real and belongs to someone; enumerating other accounts' key ids
           is not a capability this surface should hand out.
"""

from __future__ import annotations

import contextlib
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator

from archimedes.api.account_auth import CurrentUser, require_session_credential
from archimedes.api.api_key_auth import mint
from archimedes.api.limiter import limiter
from archimedes.db import get_session
from archimedes.models.api_key import (
    MAX_KEYS_PER_ACCOUNT,
    get_owned_key,
    list_keys,
    live_key_count,
    revoke_key,
)

logger = logging.getLogger(__name__)

api_key_router = APIRouter(prefix="/api/account/keys", tags=["account"])


class ApiKeyCreateRequest(BaseModel):
    """A key needs a label so a human can decide which one to revoke."""

    name: str = Field(..., min_length=1, max_length=64)

    @field_validator("name")
    @classmethod
    def _clean(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("name must not be blank")
        return cleaned


class ApiKeySummary(BaseModel):
    """A key as the owner sees it afterwards. **No field can carry a secret.**"""

    id: str
    name: str
    prefix: str = Field(..., description="archim_<id> — identifies the key, cannot be used as one.")
    created_at: str | None = None
    last_used_at: str | None = None
    revoked_at: str | None = None


class ApiKeyCreateResponse(ApiKeySummary):
    """The create response, and the only place a token is ever returned."""

    key: str = Field(
        ...,
        description=(
            "The full token, shown exactly once. It is stored only as a salted hash, "
            "so it cannot be recovered from this system by anyone, including its operators."
        ),
    )


def _after_mint(response: Response, key_id: str, user_id: str) -> None:
    """Side effects that run *after* the key is durable. **Nothing here may raise.**

    Past this point the row is committed and the caller is owed the one and only
    copy of its token. An exception escaping here does not undo the key — it
    replaces the response that carries the token with a 500, which strands a
    slot the account can never use and can only find by listing and revoking it.
    So every side effect is suppressed individually: one failing must not skip
    the next, and none of them may reach the client.

    Add future side effects (telemetry, funnel, a webhook) *inside this function*,
    each in its own ``suppress`` — never between the commit and the return.
    """
    with contextlib.suppress(Exception):
        # The id is safe to log (it is the public half and appears in the token
        # prefix); the token is not, and is not passed to any logger anywhere.
        logger.info("api key created: id=%s user=%s", key_id, user_id)

    with contextlib.suppress(Exception):
        # This is the only response in the system that carries a token, so it
        # must not sit in a proxy, a browser cache, or a `curl -O` on disk.
        # It also gives `response` a second, visible job — the parameter is
        # load-bearing for slowapi (see the signature) and a reader who deletes
        # it as unused reintroduces the 500 this function documents.
        response.headers["Cache-Control"] = "no-store"


@api_key_router.post("", response_model=ApiKeyCreateResponse, status_code=201)
@limiter.limit("10/hour")
async def create_api_key(
    payload: ApiKeyCreateRequest,
    request: Request,  # noqa: ARG001 — slowapi's decorator requires it by name; also read by require_session_credential
    response: Response,
    user: CurrentUser = Depends(require_session_credential),
) -> ApiKeyCreateResponse:
    """Mint a key for the calling account. The token is in the response, once.

    Transactional in effect: the response body is built **before** the commit, so
    anything that can fail while turning the row into a body fails while the
    transaction is still open and the ``except`` below rolls the row back. A
    caller who gets a 500 from this endpoint has not burned one of their
    ``MAX_KEYS_PER_ACCOUNT`` slots on a key whose token nobody will ever hold.

    ``response`` is **not** optional decoration. ``@limiter.limit`` wraps this
    coroutine, and slowapi injects its ``X-RateLimit-*`` headers into whatever the
    handler returned — but a handler returning a pydantic model instead of a
    ``Response`` sends it to ``kwargs["response"]`` instead, and
    ``Limiter._inject_headers`` *raises* when that is ``None``. That raise happens
    in slowapi's wrapper, outside this function's ``try``, after the commit: a
    plain-text 500 with the key already persisted. It is invisible to the unit
    suite because ``limiter.enabled`` is ``False`` under ``TESTING``, which is why
    this endpoint 500'd on every production mint while CI stayed green.
    ``test_api_key_mint.py`` pins both halves.
    """
    session = get_session()
    try:
        if live_key_count(session, user.id) >= MAX_KEYS_PER_ACCOUNT:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "api_key_limit_reached",
                    "message": (
                        f"This account already holds {MAX_KEYS_PER_ACCOUNT} live API keys. "
                        "Revoke one before creating another."
                    ),
                },
            )

        record, token = mint(session, user_id=user.id, name=payload.name)
        # Build the body BEFORE the commit — see the docstring. Serialisation,
        # a null timestamp, a model-validation error: all of them land in the
        # ``except`` below while the row is still rollback-able.
        body = ApiKeyCreateResponse(**record.to_payload(), key=token)
        session.commit()
    except HTTPException:
        session.rollback()
        raise
    except Exception as exc:
        session.rollback()
        logger.error("api key creation failed: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Could not create API key") from exc
    finally:
        session.close()

    # ── The key is durable and the caller is owed its token. Nothing below raises. ──
    _after_mint(response, body.id, user.id)
    return body


@api_key_router.get("", response_model=list[ApiKeySummary])
async def list_api_keys(
    request: Request,  # noqa: ARG001 — consumed by require_session_credential via Depends
    user: CurrentUser = Depends(require_session_credential),
) -> list[ApiKeySummary]:
    """The calling account's keys, newest first. Never another account's."""
    session = get_session()
    try:
        return [ApiKeySummary(**row) for row in list_keys(session, user.id)]
    finally:
        session.close()


@api_key_router.delete("/{key_id}", status_code=204, response_class=Response)
async def revoke_api_key(
    key_id: str,
    request: Request,  # noqa: ARG001 — consumed by require_session_credential via Depends
    user: CurrentUser = Depends(require_session_credential),
) -> Response:
    """Revoke a key. Effective on the very next request that presents it.

    Idempotent: revoking an already-revoked key is a 204, not an error, so a
    retried automation does not have to distinguish the two.
    """
    session = get_session()
    try:
        record = get_owned_key(session, user.id, key_id)
        if record is None:
            # Not yours and does not exist are the same answer — see module docstring.
            raise HTTPException(status_code=404, detail="API key not found")
        revoke_key(session, record)
        session.commit()
        # ``record.id``, not the raw path parameter. They are equal by construction
        # here — ``get_owned_key`` matched on it — but logging the value that came
        # out of the database rather than the one that came off the wire means no
        # caller-controlled string can ever reach a log line, so log forging is not
        # a question a reader has to reason about.
        logger.info("api key revoked: id=%s user=%s", record.id, user.id)
        return Response(status_code=204)
    except HTTPException:
        session.rollback()
        raise
    except Exception as exc:
        session.rollback()
        logger.error("api key revocation failed: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Could not revoke API key") from exc
    finally:
        session.close()
