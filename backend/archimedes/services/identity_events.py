"""Identity-ledger write helpers — issue #1028 Phase B ("event-emit" chunk).

Three small, fail-soft helpers are the entire write surface for the ledger:

  - ``emit_identity_event`` — the ONE path every lifecycle event goes through.
    Upserts the acting wallet into ``wallet_identities`` (D1) if it isn't
    already anchored, then appends one row to ``identity_events`` (D2). Every
    write point (linked-wallet verify, generation start/complete, strategy publish,
    vault create, marketplace publish/subscribe/unsubscribe) calls this instead
    of hand-rolling its own INSERT.
  - ``register_controlled_wallet`` — upsert into ``controlled_wallets``
    (D1a/D3) for every address the SYSTEM operates: the trading-agent /
    marketplace-engine signer, the treasury wallet, and each per-user Circle
    DCW (publisher settlement / subscriber payment).
  - ``ensure_wallet_identity`` — anchor a wallet into ``wallet_identities``
    BEFORE a caller's own FK-constrained insert, for write paths that accept
    an identity not yet present in the legacy ledger. Request routes resolve
    human wallets from proof-linked accounts; system wallets use
    controlled-wallet registration.

All three open their OWN session, isolated from any caller-held session or
transaction, and NEVER raise — a telemetry write must be invisible to the
request it measures (the same fail-safe shape as
``funnel_middleware.record_funnel``, issue #787). A failure here (DB down, a
race on a not-yet-anchored FK target, etc.) is logged at DEBUG and swallowed;
the caller's own request/response is never affected.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from archimedes.models.identity import ACTOR_CLASSES, CONTROLLED_WALLET_CLASSES

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _ensure_wallet_identity(session: Session, wallet: str, actor_class: str, *, touch_last_auth: bool = False) -> None:
    """Insert ``wallet_identities`` if missing; bump ``last_auth_at`` when asked.

    Shared by ``emit_identity_event`` and ``register_controlled_wallet`` so a
    wallet gets anchored the same way regardless of which call site sees it
    first (SIWE verify vs. a Circle DCW's ``controller_wallet``). ``wallet``
    must already be lowercased by the caller.
    """
    from archimedes.models.identity import WalletIdentity

    now = datetime.now(UTC)
    existing = session.get(WalletIdentity, wallet)
    if existing is None:
        session.add(
            WalletIdentity(
                wallet_address=wallet,
                actor_class=actor_class,
                first_seen_at=now,
                last_auth_at=now if touch_last_auth else None,
            )
        )
        session.flush()
    elif touch_last_auth:
        existing.last_auth_at = now


def emit_identity_event(
    *,
    wallet: str | None,
    event_type: str,
    actor_class: str = "human",
    vid: str | None = None,
    meta: dict[str, Any] | None = None,
    touch_last_auth: bool = False,
) -> None:
    """Append one row to ``identity_events``, anchoring the wallet if new.

    ``wallet`` is lowercased on write (the anchor's own PK is lowercase by
    construction — D1). When ``wallet`` is falsy and ``actor_class`` is not
    ``'system'``, this is a no-op: an anonymous pre-auth action has nothing to
    anchor the row to, and D2a is explicit that pre-auth stays anonymous —
    those transitions keep going through ``funnel_middleware.record_funnel``'s
    Redis HLL, unchanged (#787).

    ``vid`` is the browser visitor id — pass it ONLY post-auth (D2a), e.g. the
    SIWE-verify write. Every other call site should leave it ``None``.

    ``touch_last_auth=True`` additionally bumps ``wallet_identities.last_auth_at``
    to now. Used by the SIWE-verify write (``auth_verified``) only.

    Never raises — see module docstring.
    """
    if not wallet and actor_class != "system":
        return
    if actor_class not in ACTOR_CLASSES:
        actor_class = "human"

    try:
        from archimedes.db import get_session
        from archimedes.models.identity import IdentityEvent

        wallet_norm = wallet.lower() if wallet else None
        session = get_session()
        try:
            if wallet_norm:
                _ensure_wallet_identity(session, wallet_norm, actor_class, touch_last_auth=touch_last_auth)

            session.add(
                IdentityEvent(
                    wallet=wallet_norm,
                    vid=vid,
                    event_type=event_type,
                    actor_class=actor_class,
                    occurred_at=datetime.now(UTC),
                    meta=meta or {},
                )
            )
            session.commit()
        finally:
            session.close()
    except Exception as exc:
        logger.debug("emit_identity_event(%s) failed for wallet=%s: %s", event_type, wallet, exc)


def ensure_wallet_identity(wallet: str | None, actor_class: str = "human") -> None:
    """Anchor ``wallet`` into ``wallet_identities`` if it isn't already there.

    Call this BEFORE inserting a row whose column FKs to
    ``wallet_identities.wallet_address`` when the wallet may not have gone
    through SIWE yet. Calling
    ``emit_identity_event`` afterwards for the same wallet is then a fast
    no-op anchor-check, not a duplicate insert. A no-op when ``wallet`` is
    falsy. Never raises — see module docstring.
    """
    if not wallet:
        return
    if actor_class not in ACTOR_CLASSES:
        actor_class = "human"

    try:
        from archimedes.db import get_session

        session = get_session()
        try:
            _ensure_wallet_identity(session, wallet.lower(), actor_class)
            session.commit()
        finally:
            session.close()
    except Exception as exc:
        logger.debug("ensure_wallet_identity failed for wallet=%s: %s", wallet, exc)


def register_controlled_wallet(
    *,
    address: str | None,
    wallet_class: str,
    controller_wallet: str | None = None,
    key_binding: str | None = None,
) -> None:
    """Upsert one row into ``controlled_wallets`` (D1a/D3).

    Idempotent insert-if-missing — safe to call on every provisioning event
    (publish, subscribe) and on every app boot (agent_runner / treasury).
    ``controller_wallet``, when given, is anchored into ``wallet_identities``
    first (via ``_ensure_wallet_identity``) so the FK never trips on
    call-site ordering. Never raises — see module docstring.
    """
    if not address or wallet_class not in CONTROLLED_WALLET_CLASSES:
        return

    try:
        from archimedes.db import get_session
        from archimedes.models.identity import ControlledWallet

        address_norm = address.lower()
        controller_norm = controller_wallet.lower() if controller_wallet else None
        session = get_session()
        try:
            if controller_norm:
                _ensure_wallet_identity(session, controller_norm, "human")

            existing = session.get(ControlledWallet, address_norm)
            if existing is None:
                session.add(
                    ControlledWallet(
                        address=address_norm,
                        controller_wallet=controller_norm,
                        wallet_class=wallet_class,
                        key_binding=key_binding,
                    )
                )
                session.commit()
        finally:
            session.close()
    except Exception as exc:
        logger.debug("register_controlled_wallet failed for %s (%s): %s", address, wallet_class, exc)
