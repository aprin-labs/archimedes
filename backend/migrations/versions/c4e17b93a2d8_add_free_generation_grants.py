"""add free_generation_grants — the first N generations on a bare account (#1643)

The owner's 2026-08-31 product review reversed the 2026-08-19 "no free path"
directive: an account is still required for every generation, but a wallet is
not, for a small **lifetime** allowance (default 3) — then the existing wallet
gate and paywall apply unchanged.

This table is that allowance. It is a table and not a Redis key on purpose:
``services/generation_quota.py``'s day-buckets already cap generation *volume*
and self-expire every 36 hours, so putting the lifetime allowance there would
regrant three free runs daily, and a cache flush would regrant them at random.
An allowance that resets on an infrastructure event is not a policy.

``uq_free_generation_grants_user_seq`` is the load-bearing constraint, not a
tidy-up: two concurrent first-generation requests compute the same next
``seq``, exactly one INSERT survives, and the loser is answered "no free slot"
rather than being granted a duplicate. Without it a burst of N concurrent
calls each read ``used_count == 0`` and each get a free run.

BACKFILL (migrations-ship-with-their-data rule): **none, and none is needed.**
Free generations did not exist before this revision — production refused the
first wallet-less call with ``409 wallet_link_required`` — so there is no
historical run that consumed an allowance row. Every existing account starts
this policy with its full allowance, which is exactly the intent of a policy
that begins today. No row is derivable from ``generation_credits`` or
``payment_receipts`` either: those record *paid* runs, the complement of what
this table counts.

Revision ID: c4e17b93a2d8
Revises: a7f2c93b1d64
Create Date: 2026-08-31 00:00:00.000000

SEQUENCING: chained onto ``a7f2c93b1d64``, the chain head at authoring time.
If another migration lands on main first, re-point ``down_revision`` at the
new head — ``.github/scripts/migration_chain_guard.py`` catches a fork.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4e17b93a2d8"
down_revision: str | Sequence[str] | None = "c1d8e6f30a72"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "free_generation_grants",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        # The atomic half of the free-slot claim — see the module docstring.
        sa.UniqueConstraint("user_id", "seq", name="uq_free_generation_grants_user_seq"),
    )
    op.create_index(
        "ix_free_generation_grants_user_status",
        "free_generation_grants",
        ["user_id", "status"],
    )


def downgrade() -> None:
    """Downgrade schema.

    Dropping the table discards every record of a spent free generation, so
    every account silently regains its full allowance. That is a deliberate
    give-away, not a neutral rollback — no value is owed to anyone (nothing was
    paid), but the free LLM spend it re-authorises is real.
    """
    op.drop_index("ix_free_generation_grants_user_status", table_name="free_generation_grants")
    op.drop_table("free_generation_grants")
