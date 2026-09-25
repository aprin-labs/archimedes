"""add api_keys — a machine credential that is not the account password (#1653 D3)

Before this table the only credential a CI job or agent runner could hold was the
Better Auth session cookie, and the only way to obtain or refresh one was to POST
the account **password** to ``/api/auth/sign-in/email`` — every seven days, when
the cookie expires. The machine credential was the human's password: not scopable,
not revocable without locking the human out, and re-transmitted on a weekly cycle.

One row per key. The token itself is **not** in the schema and is not anywhere:
``secret_hash`` is ``sha256(salt || secret)`` over a 256-bit secret, and the salt
is per-key. A dump of this table yields nothing replayable. See
``backend/archimedes/models/api_key.py`` for why sha256 rather than a slow KDF is
the correct primitive for a secret of that size, and why the key id is public.

``id`` is a 16-hex-char application-generated string, not a serial: it travels
inside the token as the lookup handle (``archim_<id>_<secret>``), so it must be
opaque and unguessable-in-bulk rather than enumerable.

The FK to ``auth_users`` with ``ON DELETE CASCADE`` is the schema half of the
scoping guarantee: a key cannot outlive the account it speaks for. The
application half — every query filtered by ``user_id`` — is in
``models/api_key.py``; both exist because either alone is a promise the other
could break. Matches the cascade policy revision ``85ca5310b7a1`` applied to the
other user-owned tables.

BACKFILL (migrations-ship-with-their-data rule): **none, and none is possible.**
No API key has ever existed, so there is no historical credential to migrate. The
cookie lane is untouched by this revision and keeps working exactly as before;
this is a second credential added beside it, not a replacement.

Revision ID: c1d8e6f30a72
Revises: a7f2c93b1d64
Create Date: 2026-08-31 00:00:00.000000

SEQUENCING: chained onto ``a7f2c93b1d64``, the chain head at authoring time. If
another migration lands on main first, re-point ``down_revision`` at the new head
— ``.github/scripts/migration_chain_guard.py`` catches a fork, and
``backend/tests/test_alembic_migrations.py`` asserts exactly one head.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1d8e6f30a72"
down_revision: str | Sequence[str] | None = "a7f2c93b1d64"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "api_keys",
        # The PUBLIC key id — the token's lookup handle, not a secret.
        sa.Column("id", sa.String(length=32), nullable=False),
        # The account this key speaks for. This column IS the key's scope.
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        # 16 random bytes, hex. Per key, so no two digests share structure.
        sa.Column("salt", sa.String(length=64), nullable=False),
        # sha256(salt_bytes || secret_bytes), hex. The ONLY trace of the token.
        sa.Column("secret_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        # Coarsened to one write per minute per key on the request path.
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        # Read from the row on every verify — revocation with no cache to expire.
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["auth_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # The list endpoint's query ("this account's keys") and the live-key ceiling
    # check ("this account's un-revoked keys") are both covered by this index.
    op.create_index("ix_api_keys_user_revoked", "api_keys", ["user_id", "revoked_at"])
    op.create_index("ix_api_keys_user_id", "api_keys", ["user_id"])


def downgrade() -> None:
    """Downgrade schema.

    Dropping the table revokes every key at once — irreversibly, since the digests
    it holds cannot be reconstructed from anything. That is the correct failure
    mode for a credential store (a downgrade must never leave keys *working* with
    no record of them), but it means a downgrade logs every agent out and each key
    must be minted again.
    """
    op.drop_index("ix_api_keys_user_id", table_name="api_keys")
    op.drop_index("ix_api_keys_user_revoked", table_name="api_keys")
    op.drop_table("api_keys")
