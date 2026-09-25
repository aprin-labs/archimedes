"""Better Auth accounts and verified wallet links.

Better Auth owns writes to the five ``auth_*`` tables. SQLAlchemy maps the
same schema so application rows can reference the canonical user ID without
parsing Better Auth cookies or duplicating account state.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from archimedes.models.chat import Base


def _now() -> datetime:
    return datetime.now(UTC)


# ── The platform legacy-adoption account (#1283) ───────────────────────────
#
# A real ``auth_users`` row, created idempotently by the alembic revision that
# adopts orphaned pre-account rows, so the ownership FK those rows carry
# (``owner_user_id -> auth_users.id``) points at something that exists. It is
# an OWNER, never a LOGIN:
#
#   * no ``auth_accounts`` credential row and no ``auth_sessions`` row is ever
#     written for it, so nothing can authenticate as it — adopted rows are
#     therefore readable by nobody, which is the fail-closed direction;
#   * the address is in the RFC 6761 ``.invalid`` TLD, which by definition
#     resolves nowhere, so no email flow can ever reach it — and because
#     ``auth_users.email`` is UNIQUE, a signup attempting this address is
#     rejected by the database rather than taking the account over;
#   * every published count of ACCOUNTS or of ACCOUNTS-WHO-DID-SOMETHING
#     excludes it by id — a bookkeeping row must never inflate a user number
#     the product publishes. Three filters, all pinned by tests:
#       - ``services/user_stats.py::_query_distinct_user_count`` (the public
#         "users" figure);
#       - ``services/engagement_metrics.py::get_account_metrics``
#         (total / new_7d / new_30d);
#       - ``services/engagement_metrics.py::get_repeat_generation_metrics``
#         — filtered on ``strategy_store.owner_user_id``, not on
#         ``auth_users.id``, because that tile counts accounts by the rows
#         they own and the adopted rows are the ones this account owns. The
#         adjacent ``is_example`` filter does NOT cover them: an adopted row
#         carries a real ``owner_wallet``, so it is not house content.
#     Anything counting owners of adopted rows in future must join this list.
#
# The id is fixed and documented rather than generated: the migration, its
# downgrade, the release path, and the metrics filters all have to name the
# same account, and a generated id would make the migration non-idempotent.
PLATFORM_LEGACY_USER_ID = "platform-legacy"
PLATFORM_LEGACY_EMAIL = "platform-legacy@archimedes.invalid"
PLATFORM_LEGACY_NAME = "Archimedes Platform (legacy rows)"


class AuthUser(Base):
    __tablename__ = "auth_users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    email_verified: Mapped[bool] = mapped_column("emailVerified", Boolean, nullable=False, default=False)
    image: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column("createdAt", DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        "updatedAt", DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )
    # #1804 — what SES told us about this address, as opposed to what the
    # person did. ``email_verified`` above cannot carry it: false there means
    # both "hasn't clicked yet" and "the mailbox does not exist", and the free
    # tier gates on it, so a dead address is locked out indistinguishably from
    # an impatient human. NULL means SES has never reported a bounce or a
    # complaint for this address.
    #
    # Written by exactly one thing — ``archimedes.scripts.ses_events`` draining
    # the SES → SNS → SQS feedback queue (``infra/ses_events.tf``). Better Auth
    # declares the same two columns as ``user.additionalFields`` with
    # ``input: false`` (``auth/auth.js``) so no request body can set them; it
    # only READS them, to refuse signup and the self-service resend.
    email_bounced_at: Mapped[datetime | None] = mapped_column("emailBouncedAt", DateTime(timezone=True), nullable=True)
    #: ``bounce`` (permanent — the mailbox does not exist) or ``complaint``
    #: (a human told their provider our mail is spam). Transient bounces are
    #: NOT recorded here: a full mailbox or a temporary DNS failure is not a
    #: fake address, and stamping one would lock out a real user.
    email_bounce_kind: Mapped[str | None] = mapped_column("emailBounceKind", String(32), nullable=True)


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column("expiresAt", DateTime(timezone=True), nullable=False)
    token: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column("createdAt", DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        "updatedAt", DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )
    ip_address: Mapped[str | None] = mapped_column("ipAddress", Text, nullable=True)
    user_agent: Mapped[str | None] = mapped_column("userAgent", Text, nullable=True)
    user_id: Mapped[str] = mapped_column(
        "userId", String(64), ForeignKey("auth_users.id", ondelete="CASCADE"), nullable=False, index=True
    )


class AuthAccount(Base):
    __tablename__ = "auth_accounts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column("accountId", Text, nullable=False)
    provider_id: Mapped[str] = mapped_column("providerId", String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(
        "userId", String(64), ForeignKey("auth_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    access_token: Mapped[str | None] = mapped_column("accessToken", Text, nullable=True)
    refresh_token: Mapped[str | None] = mapped_column("refreshToken", Text, nullable=True)
    id_token: Mapped[str | None] = mapped_column("idToken", Text, nullable=True)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(
        "accessTokenExpiresAt", DateTime(timezone=True), nullable=True
    )
    refresh_token_expires_at: Mapped[datetime | None] = mapped_column(
        "refreshTokenExpiresAt", DateTime(timezone=True), nullable=True
    )
    scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    password: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column("createdAt", DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        "updatedAt", DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )


class AuthVerification(Base):
    __tablename__ = "auth_verifications"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    identifier: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column("expiresAt", DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column("createdAt", DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        "updatedAt", DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )


class AuthEmailDelivery(Base):
    """One row per outbound message the auth sidecar hands to a mailer (#1748).

    WHY IT EXISTS. ``POST /api/auth/send-verification-email`` answered
    ``200 {status: true}`` forever — for an address SES had already dropped
    onto the account suppression list, for an address whose last send threw,
    for every address. The only trace a send left was a ``console.error`` on
    the failure path, so nothing in the product could tell those apart and
    nothing could tell the user. ``auth/mailer.js`` now writes one row here per
    send and ``GET /api/auth/verification-status`` reads them back for the
    signed-in caller's own address.

    ORDER COMES FROM ``seq``, NOT ``created_at``. The status endpoint reads the
    newest row as THE latest attempt, and that row is what decides whether the
    owner is told "our provider accepted it" or "the last attempt was refused".
    ``created_at`` has millisecond resolution and back-to-back sends routinely
    share one, so ``ORDER BY created_at DESC`` is a tie the database may break
    either way — and a per-process key only fixes that inside one process,
    while the auth service autoscales. ``seq`` is DB-assigned
    (``BIGINT GENERATED BY DEFAULT AS IDENTITY``) and UNIQUE, so the order is
    total across every writer sharing the database. ``created_at`` stays: it is
    what the 24h window and the resend countdown are computed from.

    WRITTEN BY NODE, SHAPED BY ALEMBIC — the same split every other ``auth_*``
    table already lives under. Nothing in the Python backend inserts here; this
    model exists so the DDL has one home (migration
    ``d4b1f7c8e206``) and so ``create_all()`` and ``alembic upgrade head``
    cannot drift apart.

    WHAT IS DELIBERATELY ABSENT: the subject, the body, the verification URL
    (a one-time bearer sign-in credential), and the error MESSAGE. ``error``
    holds the AWS SDK error's *name* only — a fixed vocabulary
    (``MessageRejected``, ``AccessDeniedException``, ...) that cannot carry an
    address, a token, or a body fragment into the log.

    ``user_id`` is ON DELETE CASCADE, so deleting an account takes its delivery
    rows — and the addresses in them — with it, matching migration
    ``85ca5310b7a1``'s erasure policy for the other account-owned tables. It is
    nullable only because a send is recorded even if the owning row cannot be
    resolved; the ``email`` column is what the read path actually matches on,
    because ``changeEmail`` can move an account's address while old rows keep
    the address they were actually sent to.
    """

    __tablename__ = "auth_email_deliveries"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    #: The write order, assigned by the database. BIGSERIAL-equivalent on
    #: Postgres, a plain NOT NULL integer on SQLite (no IDENTITY, no sequences)
    #: — which is only ever the alembic round-trip tests, never a real writer.
    seq: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), Identity(), nullable=False, unique=True
    )
    user_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("auth_users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    #: "verification" | "reset" | "change_email" | "account_change"
    #: (auth/delivery-log.js DELIVERY_KINDS — that module is the vocabulary's home).
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    #: "sent" (the mailer accepted it) | "failed" (it threw). NEVER "delivered":
    #: SES returns a MessageId for a suppressed address and then drops the mail,
    #: which is precisely why the status endpoint also queries the suppression list.
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)

    __table_args__ = (
        # The status endpoint's only query: rows for one address and kind
        # inside a 24h window, ordered by ``seq`` DESC. This index serves the
        # WHERE — the selective half — and the ORDER BY is a sort over what
        # survives it, which the resend limiter bounds to a handful of rows
        # per address per day.
        Index("ix_auth_email_deliveries_email_kind_created", "email", "kind", "created_at"),
    )


class AuthRateLimit(Base):
    __tablename__ = "auth_rate_limits"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False)
    last_request: Mapped[int] = mapped_column("lastRequest", BigInteger, nullable=False)


class LinkedWallet(Base):
    """Wallet ownership proven by a Better Auth user; never an app session."""

    __tablename__ = "linked_wallets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("auth_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    normalized_identity: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    # FK retrofit (schema-relations Phase 1): the sole bridge between Better
    # Auth and the SIWE identity ledger — every row here is written only
    # AFTER `_link_verified_wallet` (wallet_routes.py) has flushed the
    # matching WalletIdentity, so this converts an existing app-code
    # invariant into a schema guarantee. Indexed: this is the column every
    # "which account owns wallet W" query filters on.
    address: Mapped[str] = mapped_column(
        String(42), ForeignKey("wallet_identities.wallet_address"), nullable=False, index=True
    )
    display_address: Mapped[str] = mapped_column(String(42), nullable=False)
    chain_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    circle_wallet_id: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    __table_args__ = (
        CheckConstraint("address = lower(address)", name="ck_linked_wallets_address_lower"),
        CheckConstraint("chain_id > 0", name="ck_linked_wallets_chain_positive"),
        Index(
            "uq_linked_wallets_one_primary_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("is_primary"),
            sqlite_where=text("is_primary = 1"),
        ),
    )


class WalletLinkChallenge(Base):
    """Hashed, user-bound, short-lived, single-use wallet proof challenge."""

    __tablename__ = "wallet_link_challenges"

    nonce_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("auth_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    address: Mapped[str] = mapped_column(String(42), nullable=False)
    display_address: Mapped[str] = mapped_column(String(42), nullable=False)
    chain_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    circle_wallet_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    uri: Mapped[str] = mapped_column(String(512), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)

    __table_args__ = (
        CheckConstraint("address = lower(address)", name="ck_wallet_link_challenges_address_lower"),
        CheckConstraint("chain_id > 0", name="ck_wallet_link_challenges_chain_positive"),
    )
