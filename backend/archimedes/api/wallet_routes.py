"""Verified wallet links for canonical Better Auth users."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal, get_args
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.exc import IntegrityError

from archimedes.api.account_auth import CurrentUser, get_current_user, require_current_user
from archimedes.db import get_session
from archimedes.models.account import LinkedWallet, WalletLinkChallenge

wallet_router = APIRouter(prefix="/api/wallets", tags=["wallets"])
_CHALLENGE_TTL = timedelta(minutes=5)
_SUPPORTED_CHAIN_ID = int(os.getenv("ARC_CHAIN_ID", "5042002"))
_NONCE_RE = re.compile(r"^[A-Za-z0-9]{8,}$")

# How the caller's key is held, recorded as PROVENANCE only — it never widens or
# narrows what a linked wallet may do (that is `is_primary` + the proof itself).
#
# ``headless`` exists because the first three are browser-UI concepts and an
# autonomous API client is none of them (#1293): a script holding a raw key has
# no MetaMask, no injected EIP-1193 provider, and no Circle passkey. Before it
# existed the reference journey sent ``browser``, which recorded a fact that was
# simply untrue. Callers outside the browser should send ``headless``.
WalletProvider = Literal["metamask", "browser", "circle", "headless"]
WALLET_PROVIDERS: tuple[str, ...] = get_args(WalletProvider)


class WalletChallengeRequest(BaseModel):
    address: str = Field(pattern=r"^0x[a-fA-F0-9]{40}$")
    chain_id: int = Field(gt=0)
    provider: WalletProvider
    circle_wallet_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_circle_id(self):
        if self.circle_wallet_id and self.provider != "circle":
            raise ValueError("circle_wallet_id requires the circle provider")
        return self


class WalletVerifyRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4096)
    signature: str = Field(min_length=1, max_length=8192)


class WalletChallengeResponse(BaseModel):
    message: str
    expires_at: datetime


class LinkedWalletResponse(BaseModel):
    id: str
    address: str
    display_address: str
    chain_id: int
    provider: str
    is_primary: bool
    verified_at: datetime


def _configured_site_url() -> str:
    value = (os.getenv("PUBLIC_DOMAIN") or os.getenv("BETTER_AUTH_URL") or "").rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=503, detail="Wallet linking is not configured")
    return f"{parsed.scheme}://{parsed.netloc}"


def _checksum_address(address: str) -> str:
    from eth_utils.address import to_checksum_address

    try:
        return to_checksum_address(address)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid wallet address") from exc


def _nonce_hash(nonce: str) -> str:
    return hashlib.sha256(nonce.encode()).hexdigest()


def _iso(value: datetime) -> str:
    value = value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _challenge_message(
    *,
    domain: str,
    display_address: str,
    uri: str,
    chain_id: int,
    nonce: str,
    issued_at: datetime,
    expires_at: datetime,
) -> str:
    return "\n".join(
        [
            f"{domain} wants you to sign in with your Ethereum account:",
            display_address,
            "",
            "Link this wallet to your authenticated Archimedes account.",
            "",
            f"URI: {uri}",
            "Version: 1",
            f"Chain ID: {chain_id}",
            f"Nonce: {nonce}",
            f"Issued At: {_iso(issued_at)}",
            f"Expiration Time: {_iso(expires_at)}",
        ]
    )


def _wallet_response(wallet: LinkedWallet) -> LinkedWalletResponse:
    return LinkedWalletResponse(
        id=wallet.id,
        address=wallet.address,
        display_address=wallet.display_address,
        chain_id=wallet.chain_id,
        provider=wallet.provider,
        is_primary=wallet.is_primary,
        verified_at=wallet.verified_at,
    )


def issue_wallet_challenge(
    session,
    user: CurrentUser,
    payload: WalletChallengeRequest,
    *,
    site_url: str | None = None,
    now: datetime | None = None,
) -> WalletChallengeResponse:
    if payload.chain_id != _SUPPORTED_CHAIN_ID:
        raise HTTPException(status_code=400, detail="Unsupported wallet chain")
    now = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    expires_at = now + _CHALLENGE_TTL
    site_url = (site_url or _configured_site_url()).rstrip("/")
    parsed_site = urlsplit(site_url)
    if parsed_site.scheme not in {"http", "https"} or not parsed_site.netloc:
        raise HTTPException(status_code=503, detail="Wallet linking is not configured")

    address = payload.address.lower()
    display_address = _checksum_address(payload.address)
    nonce = secrets.token_hex(16)
    domain = parsed_site.netloc.lower()
    message = _challenge_message(
        domain=domain,
        display_address=display_address,
        uri=site_url,
        chain_id=payload.chain_id,
        nonce=nonce,
        issued_at=now,
        expires_at=expires_at,
    )
    session.add(
        WalletLinkChallenge(
            nonce_hash=_nonce_hash(nonce),
            user_id=user.id,
            address=address,
            display_address=display_address,
            chain_id=payload.chain_id,
            provider=payload.provider,
            circle_wallet_id=payload.circle_wallet_id,
            domain=domain,
            uri=site_url,
            issued_at=now,
            expires_at=expires_at,
        )
    )
    session.commit()
    return WalletChallengeResponse(message=message, expires_at=expires_at)


def _parse_message(message: str) -> dict[str, object]:
    lines = message.splitlines()
    if len(lines) < 10 or " wants you to sign in with your Ethereum account:" not in lines[0]:
        raise HTTPException(status_code=400, detail="Malformed wallet proof message")
    fields: dict[str, str] = {}
    for line in lines[2:]:
        if ": " in line:
            key, value = line.split(": ", 1)
            fields[key] = value
    required = {"URI", "Version", "Chain ID", "Nonce", "Issued At", "Expiration Time"}
    if not required <= fields.keys() or not _NONCE_RE.fullmatch(fields["Nonce"]):
        raise HTTPException(status_code=400, detail="Malformed wallet proof message")
    try:
        chain_id = int(fields["Chain ID"])
        issued_at = datetime.fromisoformat(fields["Issued At"].replace("Z", "+00:00")).astimezone(UTC)
        expires_at = datetime.fromisoformat(fields["Expiration Time"].replace("Z", "+00:00")).astimezone(UTC)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="Malformed wallet proof message") from exc
    return {
        "domain": lines[0].split(" wants you to sign in", 1)[0].strip().lower(),
        "address": lines[1].strip().lower(),
        "uri": fields["URI"],
        "version": fields["Version"],
        "chain_id": chain_id,
        "nonce": fields["Nonce"],
        "issued_at": issued_at,
        "expires_at": expires_at,
    }


async def _verify_wallet_proof(address: str, message: str, signature: str, chain_id: int) -> bool:
    if chain_id != _SUPPORTED_CHAIN_ID:
        return False

    from eth_account import Account
    from eth_account.messages import encode_defunct

    try:
        recovered = Account.recover_message(encode_defunct(text=message), signature=signature)
        if recovered.lower() == address:
            return True
    except Exception:
        # EOA recovery failure is expected for smart wallets; try ERC-6492 below.
        pass

    from archimedes.api._erc6492 import verify_smart_wallet_signature

    rpc_url = os.getenv("ARC_RPC_URL") or os.getenv("ARC_ARC_RPC_URL") or "https://rpc.testnet.arc.network"
    return await verify_smart_wallet_signature(address, message, signature, rpc_url)


def claim_legacy_wallet_data(
    session,
    user_id: str,
    address: str,
    *,
    models: tuple[tuple[type, object], ...] | None = None,
    include_profile: bool = True,
) -> None:
    """Stamp ``owner_user_id`` onto every unclaimed pre-account row for
    ``address`` — the effect linking (or re-verifying) a wallet has, and the
    reclaim `rename_strategy`'s legacy-wallet fallback also triggers on write
    so pre-account rows migrate onto canonical ownership as they're touched
    (#1283). Public (not `_`-prefixed) because `rename_strategy` calls it
    from `strategies_routes`; the wallet-link path calls it in-module.

    ``models``/``include_profile`` narrow which tables get stamped. The
    default (both None/True) is the full claim a verified wallet link
    performs: strategy tables + ``vault_metadata`` + ``user_profiles``.
    ``rename_strategy``'s relaxed, non-fresh-signature lookup passes the
    strategy-only trio and ``include_profile=False`` — it must not move
    vault ownership (that gates a separate 409 whose ``None`` case exists
    specifically to admit a legitimately transferred on-chain owner; a
    stale reclaim from a rename would wrongly slam that door shut with no
    un-claim path) or adopt a PII-bearing profile on a caller who has only
    proven a stale wallet link, not fresh signature control.

    RELEASE (#1283, the adoption migration's mirror). The stamp above filters
    on ``owner_user_id IS NULL``, so it cannot reach a row the platform
    legacy account has already adopted. ``release_platform_adopted_rows``
    closes exactly that gap: any row the adoption migration took *because
    this address had never been linked to an account* is handed to
    ``user_id`` here, the first time that address is proven. Scoped to the
    same tables this call was asked to stamp, so a narrowed claim stays
    narrow. Without it, adopting an orphan would be a one-way door and the
    real wallet holder could never recover their own rows.
    """
    from archimedes.models.chat import VaultMetadata
    from archimedes.models.strategy_passport_record import StrategyPassportRecord
    from archimedes.models.strategy_proposal import StrategyProposal
    from archimedes.models.strategy_store import StrategyRecord
    from archimedes.models.user_profile import UserProfile

    default_models = (
        (StrategyRecord, StrategyRecord.owner_wallet),
        (StrategyPassportRecord, StrategyPassportRecord.owner_wallet),
        (StrategyProposal, StrategyProposal.owner_wallet),
        (VaultMetadata, VaultMetadata.creator_address),
    )
    for model, wallet_column in models if models is not None else default_models:
        session.query(model).filter(
            wallet_column == address,
            model.owner_user_id.is_(None),
        ).update({model.owner_user_id: user_id}, synchronize_session=False)

    from archimedes.models.legacy_adoption import release_platform_adopted_rows

    release_platform_adopted_rows(
        session,
        user_id,
        address,
        table_names=tuple(model.__tablename__ for model, _ in (models if models is not None else default_models)),
    )

    if include_profile:
        existing_profile = session.query(UserProfile).filter(UserProfile.owner_user_id == user_id).first()
        if existing_profile is None:
            session.query(UserProfile).filter(
                UserProfile.wallet_address == address,
                UserProfile.owner_user_id.is_(None),
            ).update({UserProfile.owner_user_id: user_id}, synchronize_session=False)


def _link_verified_wallet(session, user: CurrentUser, challenge: WalletLinkChallenge, now: datetime) -> LinkedWallet:
    from archimedes.models.identity import ControlledWallet, WalletIdentity

    normalized_identity = f"{challenge.chain_id}:{challenge.address}"
    existing = session.query(LinkedWallet).filter_by(normalized_identity=normalized_identity).first()
    if existing:
        if existing.user_id != user.id:
            raise HTTPException(status_code=409, detail="Wallet is already linked to another account")
        claim_legacy_wallet_data(session, user.id, challenge.address)
        session.commit()
        session.refresh(existing)
        return existing

    if session.get(ControlledWallet, challenge.address) is not None:
        raise HTTPException(status_code=409, detail="Circle wallet is already platform-controlled")

    if session.get(WalletIdentity, challenge.address) is None:
        session.add(
            WalletIdentity(
                wallet_address=challenge.address,
                actor_class="human",
                first_seen_at=now,
                last_auth_at=None,
            )
        )
        session.flush()

    linked = LinkedWallet(
        user_id=user.id,
        normalized_identity=normalized_identity,
        address=challenge.address,
        display_address=challenge.display_address,
        chain_id=challenge.chain_id,
        provider=challenge.provider,
        circle_wallet_id=challenge.circle_wallet_id,
        is_primary=session.query(LinkedWallet).filter_by(user_id=user.id).count() == 0,
        verified_at=now,
        created_at=now,
        updated_at=now,
    )
    session.add(linked)
    session.flush()
    claim_legacy_wallet_data(session, user.id, challenge.address)
    session.commit()
    session.refresh(linked)
    return linked


async def verify_wallet_challenge(
    session,
    user: CurrentUser,
    payload: WalletVerifyRequest,
    *,
    verifier=_verify_wallet_proof,
    now: datetime | None = None,
) -> LinkedWallet:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    proof = _parse_message(payload.message)
    challenge = session.get(WalletLinkChallenge, _nonce_hash(str(proof["nonce"])))
    if challenge is None or challenge.user_id != user.id:
        raise HTTPException(status_code=401, detail="Wallet challenge is invalid or expired")
    if challenge.consumed_at is not None:
        raise HTTPException(status_code=409, detail="Wallet challenge was already used")
    if challenge.expires_at.replace(tzinfo=challenge.expires_at.tzinfo or UTC) <= now:
        raise HTTPException(status_code=401, detail="Wallet challenge is invalid or expired")

    expected_message = _challenge_message(
        domain=challenge.domain,
        display_address=challenge.display_address,
        uri=challenge.uri,
        chain_id=challenge.chain_id,
        nonce=str(proof["nonce"]),
        issued_at=challenge.issued_at,
        expires_at=challenge.expires_at,
    )
    if payload.message != expected_message:
        raise HTTPException(status_code=401, detail="Wallet proof does not match its challenge")

    expected = {
        "domain": challenge.domain,
        "address": challenge.address,
        "uri": challenge.uri,
        "version": "1",
        "chain_id": challenge.chain_id,
        "issued_at": challenge.issued_at.replace(tzinfo=challenge.issued_at.tzinfo or UTC).astimezone(UTC),
        "expires_at": challenge.expires_at.replace(tzinfo=challenge.expires_at.tzinfo or UTC).astimezone(UTC),
    }
    if any(proof[key] != value for key, value in expected.items()):
        raise HTTPException(status_code=401, detail="Wallet proof does not match its challenge")
    if not await verifier(challenge.address, payload.message, payload.signature, challenge.chain_id):
        raise HTTPException(status_code=401, detail="Invalid wallet signature")

    consumed = (
        session.query(WalletLinkChallenge)
        .filter(
            WalletLinkChallenge.nonce_hash == challenge.nonce_hash,
            WalletLinkChallenge.user_id == user.id,
            WalletLinkChallenge.consumed_at.is_(None),
            WalletLinkChallenge.expires_at > now,
        )
        .update({WalletLinkChallenge.consumed_at: now}, synchronize_session=False)
    )
    session.commit()
    if consumed != 1:
        raise HTTPException(status_code=409, detail="Wallet challenge was already used")

    try:
        return _link_verified_wallet(session, user, challenge, now)
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="Wallet is already linked") from exc


def set_primary_wallet(session, user_id: str, wallet_id: str) -> LinkedWallet:
    wallet = session.query(LinkedWallet).filter_by(id=wallet_id, user_id=user_id).first()
    if wallet is None:
        raise HTTPException(status_code=404, detail="Wallet not found")
    session.query(LinkedWallet).filter_by(user_id=user_id).update(
        {LinkedWallet.is_primary: False}, synchronize_session=False
    )
    wallet.is_primary = True
    session.commit()
    session.refresh(wallet)
    return wallet


def _wallet_has_owned_data(session, address: str) -> bool:
    from archimedes.models.chat import VaultMetadata
    from archimedes.models.strategy_passport_record import StrategyPassportRecord
    from archimedes.models.strategy_proposal import StrategyProposal
    from archimedes.models.strategy_store import StrategyRecord
    from archimedes.models.user_profile import UserProfile

    return any(
        session.query(model).filter(column == address).first() is not None
        for model, column in (
            (StrategyRecord, StrategyRecord.owner_wallet),
            (StrategyPassportRecord, StrategyPassportRecord.owner_wallet),
            (StrategyProposal, StrategyProposal.owner_wallet),
            (VaultMetadata, VaultMetadata.creator_address),
            (UserProfile, UserProfile.wallet_address),
        )
    )


def _wallet_has_unclaimed_legacy_data(session, user_id: str, address: str) -> bool:
    """Would ``claim_legacy_wallet_data(user_id, address)`` claim anything?

    DELIBERATELY a separate function from ``_wallet_has_owned_data`` above,
    which asks the OPPOSITE question ("does this wallet back any data at all",
    claimed or not) and guards unlinking — extending that one with an
    ``owner_user_id IS NULL`` filter would invert the unlink guard's meaning
    for exactly the wallets that back real data. This predicate mirrors the
    claim loop's models and filters one-for-one, so the relink prompt fires
    iff linking would actually attach something (#1194 revision e).

    That mirror has TWO halves since #1283's adoption. The loop below mirrors
    the ``owner_user_id IS NULL`` stamp; the ledger check after it mirrors
    ``release_platform_adopted_rows``, which the claim also performs. Both are
    required for the invariant above to hold — see the ledger block's comment.
    """
    from archimedes.models.chat import VaultMetadata
    from archimedes.models.strategy_passport_record import StrategyPassportRecord
    from archimedes.models.strategy_proposal import StrategyProposal
    from archimedes.models.strategy_store import StrategyRecord
    from archimedes.models.user_profile import UserProfile

    for model, wallet_column in (
        (StrategyRecord, StrategyRecord.owner_wallet),
        (StrategyPassportRecord, StrategyPassportRecord.owner_wallet),
        (StrategyProposal, StrategyProposal.owner_wallet),
        (VaultMetadata, VaultMetadata.creator_address),
    ):
        if session.query(model).filter(wallet_column == address, model.owner_user_id.is_(None)).first() is not None:
            return True

    # #1283 adoption: a row the platform took is no longer `owner_user_id
    # IS NULL`, so the loop above cannot see it — but linking WILL hand it
    # back (`release_platform_adopted_rows`, called from the same
    # `claim_legacy_wallet_data` this predicate mirrors), so the prompt must
    # still fire. Omitting this is what would make adoption hide the row from
    # its real owner: `/api/wallet/check` is the sole gate on the relink
    # banner, and a `false` here means the one UI that tells them their
    # strategies are recoverable never renders.
    from archimedes.models.legacy_adoption import platform_adopted_row_count

    if platform_adopted_row_count(session, address) > 0:
        return True

    # The claim loop only attaches a legacy profile when the account has none —
    # mirror that condition so the prompt never promises a claim that the link
    # would in fact skip.
    account_has_profile = session.query(UserProfile).filter(UserProfile.owner_user_id == user_id).first() is not None
    if not account_has_profile:
        legacy_profile = (
            session.query(UserProfile)
            .filter(UserProfile.wallet_address == address, UserProfile.owner_user_id.is_(None))
            .first()
        )
        if legacy_profile is not None:
            return True
    return False


def unlink_wallet(session, user_id: str, wallet_id: str) -> None:
    wallet = session.query(LinkedWallet).filter_by(id=wallet_id, user_id=user_id).first()
    if wallet is None:
        raise HTTPException(status_code=404, detail="Wallet not found")
    if _wallet_has_owned_data(session, wallet.address):
        raise HTTPException(status_code=409, detail="Wallet backs existing Archimedes data and cannot be unlinked")
    was_primary = wallet.is_primary
    session.delete(wallet)
    session.flush()
    if was_primary:
        replacement = (
            session.query(LinkedWallet).filter_by(user_id=user_id).order_by(LinkedWallet.created_at.asc()).first()
        )
        if replacement:
            replacement.is_primary = True
    session.commit()


def get_linked_wallet_address(request: Request) -> str | None:
    """The caller's LINKED wallet address, resolved from their Better Auth session.

    BOUNDARY: this is a database lookup, not a proof of control. The wallet
    was signature-verified once at LINK time; nothing here proves the caller
    still controls it on this request. That is sufficient for provenance and
    read-scoping (which strategies are "yours" to see), and it is what makes
    anonymous/None a normal outcome rather than an error. It must NEVER gate
    a money-moving or otherwise irreversible action — those need a fresh
    signature (``auth_siwe.get_verified_wallet``: session-proven control).
    Call sites that accept the relaxed guarantee do so by using this name.
    """
    user = get_current_user(request)
    if user is None:
        return None
    requested = request.headers.get("x-wallet-address")
    try:
        chain_id = int(request.headers.get("x-wallet-chain-id", "5042002"))
    except ValueError:
        return None
    with get_session() as session:
        query = session.query(LinkedWallet).filter_by(user_id=user.id, chain_id=chain_id)
        if requested:
            if not re.fullmatch(r"0x[a-fA-F0-9]{40}", requested):
                return None
            query = query.filter(LinkedWallet.address == requested.lower())
        else:
            query = query.filter(LinkedWallet.is_primary.is_(True))
        wallet = query.first()
        return wallet.address if wallet else None


def require_linked_wallet(request: Request) -> str:
    if not isinstance(get_current_user(request), CurrentUser):
        raise HTTPException(status_code=401, detail="Authentication required")
    wallet = get_linked_wallet_address(request)
    if wallet is None:
        raise HTTPException(status_code=403, detail="A verified linked wallet is required")
    return wallet


@wallet_router.get("", response_model=list[LinkedWalletResponse])
def list_wallets(user: CurrentUser = Depends(require_current_user)):
    with get_session() as session:
        wallets = (
            session.query(LinkedWallet)
            .filter_by(user_id=user.id)
            .order_by(LinkedWallet.is_primary.desc(), LinkedWallet.created_at.asc())
            .all()
        )
        return [_wallet_response(wallet) for wallet in wallets]


@wallet_router.get("/check")
def check_wallet_legacy_data(
    address: str = Query(pattern=r"^0x[a-fA-F0-9]{40}$"),
    user: CurrentUser = Depends(require_current_user),
):
    """Does linking ``address`` reclaim pre-account (SIWE-era) data for me?

    Powers the relink prompt for the ~10 wallet users who predate canonical
    identity (#1194 revision e): their strategies stay invisible until they
    link the old wallet, and nothing told them so. Returns only a boolean —
    counts stay behind the signature proof, since the caller has not yet
    proven control of the address. Session-gated, so it cannot be used to
    anonymously sweep addresses for "has data" signals at scale beyond what
    the caller's own rate limits allow.

    Still ``true`` after #1283's adoption migration has stamped those rows
    with the platform account: the predicate counts the adoption ledger too,
    so the banner keeps inviting exactly the users whose rows the platform is
    holding for them. Adoption changes who holds the row, never who can claim
    it — and this endpoint is the only thing that tells them so.
    """
    with get_session() as session:
        return {"has_legacy_data": _wallet_has_unclaimed_legacy_data(session, user.id, address.lower())}


@wallet_router.post("/challenge", response_model=WalletChallengeResponse)
def create_wallet_challenge(
    payload: WalletChallengeRequest,
    user: CurrentUser = Depends(require_current_user),
):
    with get_session() as session:
        return issue_wallet_challenge(session, user, payload)


@wallet_router.post("/verify", response_model=LinkedWalletResponse)
async def verify_wallet(
    payload: WalletVerifyRequest,
    user: CurrentUser = Depends(require_current_user),
):
    with get_session() as session:
        return _wallet_response(await verify_wallet_challenge(session, user, payload))


@wallet_router.post("/{wallet_id}/primary", response_model=LinkedWalletResponse)
def make_primary(wallet_id: str, user: CurrentUser = Depends(require_current_user)):
    with get_session() as session:
        return _wallet_response(set_primary_wallet(session, user.id, wallet_id))


@wallet_router.delete("/{wallet_id}", status_code=204)
def remove_wallet(wallet_id: str, user: CurrentUser = Depends(require_current_user)):
    with get_session() as session:
        unlink_wallet(session, user.id, wallet_id)
