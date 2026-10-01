"""Optional wallet-address profile API owned by canonical Better Auth user.

Endpoints:
  GET  /api/user/profile/{wallet}   — retrieve own profile (404 if not set or not yours)
  POST /api/user/profile            — create or update profile

Wallet key is legacy provenance, not application identity. Writes require both
account session and matching proof-linked wallet.

Security (Issue #181, #1908):
  - Email is encrypted at rest via Fernet (services/email_crypto.py).
  - Every profile field is owner-only. No field is public: display_name,
    email and marketing_opt_in are personal data/consent, and interests and
    attribution are the user's own answers to the welcome questions. Nothing
    reads another wallet's profile, so a non-owner GET is answered exactly
    like a missing profile: the same 404 status, body and response headers
    (only the measured X-Response-Time-Ms value varies), from the same branch
    after the same lookups (the linked-wallet lookup runs before the profile
    query, not only when a row exists). Timing is not otherwise equalized.
  - All log output routes through log_scrubber to prevent PII leakage; it
    redacts every profile answer, so logs carry only the wallet address.
"""

from __future__ import annotations

import json
import logging

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy.orm import Session

from archimedes.api.account_auth import get_current_user, require_current_user
from archimedes.api.limiter import limiter
from archimedes.api.user_schemas import UserProfileCreate, UserProfileResponse
from archimedes.db import get_session
from archimedes.models.user_profile import UserProfile
from archimedes.services.email_crypto import decrypt_email, encrypt_email
from archimedes.services.log_scrubber import sanitize_log_value, scrub_profile

logger = logging.getLogger(__name__)

user_router = APIRouter(prefix="/api/user", tags=["user"])

_PROFILE_NOT_FOUND = "Profile not found"


def _profile_to_response(p: UserProfile) -> UserProfileResponse:
    """Build the owner's view of a UserProfile ORM object, email decrypted.

    There is deliberately no non-owner view (#1908): callers must establish
    ownership first, and a non-owner gets a 404 instead of a response.
    """
    interests = json.loads(p.interests) if p.interests else None

    try:
        email = decrypt_email(p.email)
    except InvalidToken:
        # Ciphertext can't be decrypted with the current EMAIL_ENCRYPTION_KEY
        # (e.g. a key rotation left old tokens unreadable). Degrade to a null
        # email rather than 500ing the whole profile for this wallet.
        logger.warning("decrypt_email failed for wallet=%s: InvalidToken", p.wallet_address)
        email = None

    return UserProfileResponse(
        wallet_address=p.wallet_address,
        display_name=p.display_name,
        email=email,
        interests=interests,
        attribution=p.attribution,
        marketing_opt_in=p.marketing_opt_in,
    )


def _extract_linked_wallet(request: Request) -> str | None:
    """Return wallet proven and linked to current Better Auth account."""
    from archimedes.api.wallet_routes import get_linked_wallet_address

    return get_linked_wallet_address(request)


@user_router.get("/profile/{wallet}", response_model=UserProfileResponse)
async def get_profile(wallet: str, request: Request):
    """Retrieve the caller's own profile for a wallet.

    Owner-only (#1908): the canonical account owner, or — for an unclaimed
    legacy profile — the account whose verified linked wallet matches. Any
    other caller gets the same 404 as a wallet with no profile.
    """
    # Ownership is the canonical account (legacy rows: the verified linked
    # wallet), never a body/header address. Resolved before the profile query
    # so a missing row and someone else's row do the same lookups.
    user = get_current_user(request)
    caller = _extract_linked_wallet(request)
    session: Session = get_session()
    try:
        wallet_lower = wallet.lower()
        profile = session.query(UserProfile).filter(UserProfile.wallet_address == wallet_lower).first()
        is_owner = bool(
            profile
            and user
            and (
                profile.owner_user_id == user.id or (profile.owner_user_id is None and caller == profile.wallet_address)
            )
        )
        if not is_owner:
            # One branch for "no row" and "not yours": same status, body and
            # headers. The reason is for operators only, never the response.
            logger.info(
                "get_profile: wallet=%s not served reason=%s",
                sanitize_log_value(wallet_lower),
                "not_owner" if profile else "missing",
            )
            raise HTTPException(status_code=404, detail=_PROFILE_NOT_FOUND)

        response = _profile_to_response(profile)

        logger.info(
            "get_profile: wallet=%s owner=True data=%s",
            sanitize_log_value(wallet_lower),
            scrub_profile(response.model_dump()),
        )
        return response
    finally:
        session.close()


@user_router.post("/profile", response_model=UserProfileResponse)
@limiter.limit("1/minute")
async def upsert_profile(payload: UserProfileCreate, request: Request, response: Response):  # noqa: ARG001 — response param threaded for downstream cookie/header setting; not used in current body
    """Create or update a wallet's profile. All fields optional except wallet.

    Email is encrypted at rest. Caller needs canonical account session and
    matching verified linked wallet; client-supplied headers grant nothing.
    """
    user = require_current_user(request)
    caller = _extract_linked_wallet(request)
    if caller is None:
        raise HTTPException(status_code=403, detail="Forbidden: verified linked wallet required for profile writes")
    wallet = payload.wallet_address.lower()
    if caller != wallet:
        raise HTTPException(status_code=403, detail="Forbidden: linked wallet does not match payload wallet")

    session: Session = get_session()
    try:
        profile = session.query(UserProfile).filter(UserProfile.wallet_address == wallet).first()

        interests_json = json.dumps(payload.interests) if payload.interests else "[]"
        encrypted_email = encrypt_email(payload.email)

        if profile:
            if profile.owner_user_id not in {None, user.id}:
                raise HTTPException(status_code=409, detail="Profile belongs to another account")
            if profile.owner_user_id is None:
                canonical_profile = session.query(UserProfile).filter(UserProfile.owner_user_id == user.id).first()
                if canonical_profile is not None:
                    raise HTTPException(status_code=409, detail="Account already has a canonical profile")
                profile.owner_user_id = user.id
            # Update existing
            if payload.display_name is not None:
                profile.display_name = payload.display_name
            if payload.email is not None:
                profile.email = encrypted_email
            profile.interests = interests_json
            if payload.attribution is not None:
                profile.attribution = payload.attribution
            profile.marketing_opt_in = payload.marketing_opt_in
        else:
            if session.query(UserProfile).filter(UserProfile.owner_user_id == user.id).first() is not None:
                raise HTTPException(status_code=409, detail="Account already has a canonical profile")
            # Create new
            profile = UserProfile(
                wallet_address=wallet,
                owner_user_id=user.id,
                display_name=payload.display_name,
                email=encrypted_email,
                interests=interests_json,
                attribution=payload.attribution,
                marketing_opt_in=payload.marketing_opt_in,
            )
            session.add(profile)

        session.commit()
        session.refresh(profile)

        logger.info(
            "upsert_profile: wallet=%s data=%s",
            sanitize_log_value(wallet),
            scrub_profile(
                {
                    "wallet_address": sanitize_log_value(wallet),
                    "email": payload.email,
                    "display_name": payload.display_name,
                    "marketing_opt_in": payload.marketing_opt_in,
                }
            ),
        )

        return _profile_to_response(profile)
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        # Do NOT include PII in error messages
        logger.error("upsert_profile failed for wallet=%s: %s", sanitize_log_value(wallet), type(e).__name__)
        raise HTTPException(status_code=500, detail="Profile update failed") from e
    finally:
        session.close()
