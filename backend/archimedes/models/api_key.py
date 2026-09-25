"""Scoped API keys — a second *credential* for an account, never a second identity.

Owner decision **D3** on #1653: CI jobs and agent runners had exactly one way to
authenticate — the account's Better Auth session cookie, obtained by POSTing the
account **password** to ``/api/auth/sign-in/email`` and refreshed every seven days
when the cookie expires. That makes the machine credential *the human's password*:
un-scopable, un-revocable without locking the human out, and re-transmitted on a
weekly cycle. This table is the fix.

What is stored, and what is not
-------------------------------
One row per key::

    id           the PUBLIC key id — also the token's lookup handle. Not a secret.
    user_id      the account this key speaks for. THE scope. FK → auth_users.
    name         a human label ("ci-nightly"). Not a secret.
    salt         32 hex chars = 16 random bytes, unique per key.
    secret_hash  sha256(salt_bytes || secret_bytes), hex. 64 chars.
    created_at / last_used_at / revoked_at

**The token itself is not here, and is not anywhere.** It is returned exactly once,
in the body of the create call, and then only its hash survives. A dump of this
table — a leaked backup, a `SELECT *` in a log, a support engineer's screen — yields
nothing that can be replayed against the API. That is the whole reason the column
list looks like this, and ``test_api_key_auth.py`` proves it by serialising every
column of every row and asserting the token is absent.

Why sha256 and not bcrypt/argon2
--------------------------------
Those exist because *human passwords* have perhaps 30 bits of entropy, so a fast
hash lets an attacker with the digests enumerate the plaintext. An API key minted
here carries **256 bits** from ``secrets.token_urlsafe(32)``. There is no dictionary
to run and no feasible enumeration, so a slow KDF buys nothing but latency on every
request. The salt is still per-key (it costs one column and removes any shared
structure between digests). This reasoning is written down because "why isn't this
bcrypt" is the first question an auditor asks, and the answer must be in the file,
not in a reviewer's head.

Why the id is public and lives inside the token
-----------------------------------------------
Verification needs to find the right row. Scanning every key in the table and
comparing each digest would be O(keys) per request and would leak timing that grows
with table size. So the token carries its own non-secret lookup handle:
``archim_<id>_<secret>`` — index lookup on ``id``, then one constant-time comparison.
``archim_<id>`` is also the ``prefix`` shown in the list endpoint: enough to tell two
keys apart in a UI, useless to an attacker.

Revocation is a column, not a cache
-----------------------------------
``revoked_at`` is read on the request path, from the row, every time. There is no
TTL and no in-process memo, so "revoked" means revoked on the very next request —
which is the only definition of revocation worth having, and is pinned by
``test_revoked_key_is_401_on_the_next_call``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, Session, mapped_column

from archimedes.models.chat import Base

#: Ceiling on live (un-revoked) keys per account. Not a licence tier — an
#: anti-abuse bound, so a compromised session cannot mint an unbounded set of
#: credentials that a human then has to find and revoke one by one.
MAX_KEYS_PER_ACCOUNT = 25

#: Bound on the list read, mirroring ``generation_credit.MAX_CREDITS``. Revoked
#: rows are kept (an audit trail is the point), so the list can exceed the live
#: ceiling above.
MAX_KEYS_LISTED = 200


def _now() -> datetime:
    return datetime.now(UTC)


class ApiKeyRecord(Base):
    """One API key. Holds a digest, never a token."""

    __tablename__ = "api_keys"

    #: The public key id, and the token's lookup handle. See module docstring.
    id: Mapped[str] = mapped_column(String(32), primary_key=True)

    #: The account this key authenticates as. This column IS the key's scope:
    #: every read the key performs runs as this user and no other.
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("auth_users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    name: Mapped[str] = mapped_column(String(64), nullable=False)

    salt: Mapped[str] = mapped_column(String(64), nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)

    #: Coarsened to the minute on write (see ``touch_last_used``) so a busy key
    #: does not turn every authenticated request into a database UPDATE.
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_api_keys_user_revoked", "user_id", "revoked_at"),)

    @property
    def prefix(self) -> str:
        """The displayable, non-secret half of the token: ``archim_<id>``."""
        return f"archim_{self.id}"

    def to_payload(self) -> dict[str, Any]:
        """The list/create-echo shape. **Contains no secret material.**

        Every field here is either public by construction (``id`` / ``prefix``),
        chosen by the caller (``name``), or a timestamp. There is deliberately no
        branch that can add the token: the token does not exist on this object.
        """
        return {
            "id": self.id,
            "name": self.name,
            "prefix": self.prefix,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
        }


# ── Queries (all of them scoped by user_id — that is not optional) ──────


def live_key_count(session: Session, user_id: str) -> int:
    """How many un-revoked keys *user_id* holds."""
    return (
        session.query(ApiKeyRecord).filter(ApiKeyRecord.user_id == user_id, ApiKeyRecord.revoked_at.is_(None)).count()
    )


def list_keys(session: Session, user_id: str, *, limit: int = MAX_KEYS_LISTED) -> list[dict[str, Any]]:
    """Newest-first keys owned by *user_id*. Never another account's.

    The ``user_id`` filter is what makes A9 (cross-account isolation) true for
    reads; there is no unscoped variant of this function to reach for by mistake.
    """
    if not user_id:
        return []
    rows = (
        session.query(ApiKeyRecord)
        .filter(ApiKeyRecord.user_id == user_id)
        .order_by(ApiKeyRecord.created_at.desc(), ApiKeyRecord.id.desc())
        .limit(limit)
        .all()
    )
    return [r.to_payload() for r in rows]


def get_owned_key(session: Session, user_id: str, key_id: str) -> ApiKeyRecord | None:
    """The key *key_id*, **only** if *user_id* owns it. Otherwise ``None``.

    Ownership is part of the lookup, not a check performed afterwards on a row
    that was already fetched — so a caller cannot forget the check, and the
    "not yours" and "does not exist" answers are literally the same answer.
    That is what lets the revoke route return 404 for both without a branch
    that could drift: a 403 would confirm the id exists in someone's account.
    """
    if not user_id or not key_id:
        return None
    return session.query(ApiKeyRecord).filter(ApiKeyRecord.user_id == user_id, ApiKeyRecord.id == key_id).one_or_none()


def revoke_key(session: Session, record: ApiKeyRecord) -> ApiKeyRecord:
    """Mark *record* revoked. Idempotent — re-revoking keeps the first timestamp.

    The row is kept. A deleted row erases the evidence that the key ever existed,
    which is the opposite of what an audit trail is for.
    """
    if record.revoked_at is None:
        record.revoked_at = _now()
        session.flush()
    return record


def touch_last_used(session: Session, record: ApiKeyRecord, *, now: datetime | None = None) -> bool:
    """Record that *record* was just used. Returns True iff a write happened.

    Coarsened to one write per minute per key: ``last_used_at`` exists so a human
    can answer "is this key still in use / when did the leak start", and
    minute resolution answers both. Writing on every request would put a database
    UPDATE on the hot path of every authenticated agent call for no extra
    information.
    """
    moment = now or _now()
    previous = record.last_used_at
    if previous is not None:
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=UTC)
        if (moment - previous).total_seconds() < 60:
            return False
    record.last_used_at = moment
    session.flush()
    return True
