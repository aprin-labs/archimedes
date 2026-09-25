"""Platform-admin authorization, keyed on canonical account identity (#1648).

**What changed and why.** Admin used to be a property of *a wallet on a
request*: ``metrics_private_routes.require_platform_admin`` depended on
``wallet_routes.require_linked_wallet``, which resolves "the wallet this
request claims to be acting as" from the ``X-Wallet-Address`` header
(``ui/src/api.js``'s ``walletHeaders()`` attaches it to every ``apiGet`` from
whatever the browser extension has selected *in that tab*). So the answer to
"is this person an admin" changed with the browser, the extension, and the
currently-selected account inside the extension — for one and the same
signed-in Better Auth account. The owner hit it across three
browser/wallet combinations: the Insights page appeared in one and vanished
in the others.

Admin is now a property of **the account** (``auth_users``). A wallet is
*evidence* that established the linkage, never the per-request lookup key:

1. ``PLATFORM_ADMIN_ACCOUNTS`` — the canonical key. Whitespace/comma-separated
   ``auth_users.id`` values and/or email addresses. Answered from the session
   and the environment alone, with **no account-store read**, which also makes
   it the break-glass that survives a database outage (the incident during
   which an ops dashboard is most needed).
2. ``PLATFORM_ADMIN_WALLETS`` — retained, unchanged in spelling and meaning, as
   evidence. An account is admin if **any wallet in its own linked-wallet set**
   (looked up server-side by ``user_id``) is on that allowlist. Every deploy
   that has only ever set this env keeps working with no config change; what
   changes is that the answer no longer depends on which of the account's
   wallets a browser happens to have connected, or on whether it has one
   connected at all.

``docs/api/admin-private.md`` carries the migration steps and the deprecation
posture; ``derive_admin_accounts_from_wallets`` below is the read-only tool
that turns an existing wallet allowlist into the account allowlist.

**BOUNDARY — what this module deliberately does not do.** It never reads the
request. ``X-Wallet-Address``/``X-Wallet-Chain-Id`` are attacker-controlled
and prove nothing; letting a header-named admin address grant admin would
trade the reported bug for a strictly worse authorization hole. It also does
not change ``get_linked_wallet_address``'s general contract for the other
call sites that legitimately want "the wallet this request claims to be
acting as" (read-scoping which strategies are yours) — that helper's own
docstring is explicit that this is a different, lower-stakes guarantee, and
those call sites keep it.

Membership here grants **no fund / custody / treasury authority** — it is a
read gate on the internal cost/ops dashboard (plus the narrow example-strategy
publish exception in ``models/strategy_generators.wallet_can_publish``, which
still reads ``PLATFORM_ADMIN_WALLETS`` directly and is out of this change's
scope).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from archimedes.db import get_session
from archimedes.models.account import AuthUser, LinkedWallet

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a services→api import
    from archimedes.api.account_auth import CurrentUser

logger = logging.getLogger(__name__)

ENV_ADMIN_WALLETS = "PLATFORM_ADMIN_WALLETS"
ENV_ADMIN_ACCOUNTS = "PLATFORM_ADMIN_ACCOUNTS"

AdminBasis = Literal["account_allowlist", "linked_wallet"]


@dataclass(frozen=True, slots=True)
class AdminGrant:
    """Why this account is an admin, and on what evidence.

    ``wallet`` is the account's own allowlisted linked wallet when one exists,
    lowercased — the evidence, reported for provenance. It is ``None`` for an
    account granted admin by ``PLATFORM_ADMIN_ACCOUNTS`` with no allowlisted
    wallet linked; an honest null, never a substituted address. It is never
    the address the request's header named.
    """

    user_id: str
    basis: AdminBasis
    wallet: str | None


def _raw_tokens(env_name: str) -> list[str]:
    """Split a comma/whitespace allowlist env into its raw (uncased) tokens."""
    return [token.strip() for token in os.getenv(env_name, "").replace(",", " ").split() if token.strip()]


def platform_admin_wallets() -> set[str]:
    """``PLATFORM_ADMIN_WALLETS``, lowercased — same parse as ``wallet_can_publish``.

    Case-folded because a hex address is case-insensitive (EIP-55 checksumming
    is purely casing) and ``linked_wallets.address`` is stored lowercased.
    """
    return {token.lower() for token in _raw_tokens(ENV_ADMIN_WALLETS)}


def platform_admin_accounts() -> tuple[set[str], set[str]]:
    """``PLATFORM_ADMIN_ACCOUNTS`` as ``(exact_ids, lowercased_emails)``.

    The two halves are matched differently on purpose. Better Auth user ids are
    opaque, case-SENSITIVE tokens, so folding their case would let a single
    allowlist entry match two genuinely distinct accounts — a widening, in an
    allowlist. Email addresses are case-insensitive identifiers and
    ``auth_users.email`` is unique, so those fold. Every token is offered to
    both halves; an entry containing ``@`` is simply also a plausible email.
    """
    tokens = _raw_tokens(ENV_ADMIN_ACCOUNTS)
    return set(tokens), {token.lower() for token in tokens}


def _allowlisted_linked_wallet(user_id: str, admin_wallets: set[str]) -> str | None:
    """This account's own allowlisted linked wallet, or ``None``.

    Keyed on ``user_id`` and nothing else — the request never reaches here.
    Deliberately NOT filtered by ``chain_id``: a linked wallet is evidence of
    *who this account is*, which is not a per-chain fact, whereas the chain a
    given request targets is a separate concern. The old header-driven lookup
    ANDed the header's chain id into the query, so an admin whose wallet was
    linked on another chain resolved to nothing.

    Fails CLOSED: an unreadable account store yields ``None`` (no grant on this
    path), logged, never an exception out of an authorization check and never
    an admit.
    """
    if not admin_wallets:
        return None
    try:
        with get_session() as session:
            rows = (
                session.query(LinkedWallet.address, LinkedWallet.is_primary, LinkedWallet.created_at)
                .filter(LinkedWallet.user_id == user_id)
                .order_by(
                    LinkedWallet.is_primary.desc(),
                    LinkedWallet.created_at.asc(),
                    LinkedWallet.address.asc(),
                )
                .all()
            )
    # Broad by design: an authorization check must DENY on any datastore
    # failure, never propagate a 500 that leaks the outage to the caller.
    except Exception as exc:
        logger.warning(
            "platform-admin linked-wallet lookup failed, denying (fail closed): %s",
            type(exc).__name__,
        )
        return None
    for address, _is_primary, _created_at in rows:
        lowered = (address or "").lower()
        if lowered in admin_wallets:
            return lowered
    return None


def resolve_platform_admin(user: CurrentUser | Any) -> AdminGrant | None:
    """Is this canonical account a platform admin? ``None`` means no.

    Takes the Better Auth account, not a request and not a wallet. The two
    grant paths are documented on the module docstring; the account allowlist
    is evaluated first so an operator listed there keeps access even when the
    account store cannot be read.
    """
    user_id = (getattr(user, "id", "") or "").strip()
    if not user_id:
        return None
    email = (getattr(user, "email", "") or "").strip().lower()

    admin_wallets = platform_admin_wallets()
    allowlisted_ids, allowlisted_emails = platform_admin_accounts()

    if user_id in allowlisted_ids or (email and email in allowlisted_emails):
        # Report the evidence wallet when there is one, purely as provenance —
        # the grant does not depend on it, so a failed lookup cannot revoke it.
        return AdminGrant(
            user_id=user_id,
            basis="account_allowlist",
            wallet=_allowlisted_linked_wallet(user_id, admin_wallets),
        )

    wallet = _allowlisted_linked_wallet(user_id, admin_wallets)
    if wallet is not None:
        return AdminGrant(user_id=user_id, basis="linked_wallet", wallet=wallet)
    return None


def derive_admin_accounts_from_wallets() -> dict[str, Any]:
    """Migration aid: which accounts does the current wallet allowlist imply?

    Read-only. Resolves every ``PLATFORM_ADMIN_WALLETS`` entry to the
    account(s) that have it linked and returns a ready-to-paste
    ``PLATFORM_ADMIN_ACCOUNTS=`` line, plus the wallets that resolve to no
    account at all — those are the entries that would silently stop granting
    admin if the wallet env were dropped, so they are named rather than
    dropped. An address linked by more than one account yields all of them,
    for the same reason.

    Driven by ``backend/scripts/derive_platform_admin_accounts.py``.
    """
    admin_wallets = sorted(platform_admin_wallets())
    resolved: list[dict[str, str]] = []
    unlinked: list[str] = []
    if admin_wallets:
        with get_session() as session:
            for wallet in admin_wallets:
                rows = (
                    session.query(LinkedWallet.user_id, AuthUser.email)
                    .join(AuthUser, AuthUser.id == LinkedWallet.user_id)
                    .filter(LinkedWallet.address == wallet)
                    .order_by(LinkedWallet.user_id.asc())
                    .all()
                )
                if not rows:
                    unlinked.append(wallet)
                    continue
                for user_id, email in rows:
                    resolved.append({"wallet": wallet, "user_id": user_id, "email": email})

    account_ids: list[str] = []
    for entry in resolved:
        if entry["user_id"] not in account_ids:
            account_ids.append(entry["user_id"])
    return {
        "admin_wallets": admin_wallets,
        "resolved": resolved,
        "account_ids": account_ids,
        "unlinked_wallets": unlinked,
        "env_line": f"{ENV_ADMIN_ACCOUNTS}={' '.join(account_ids)}",
    }
