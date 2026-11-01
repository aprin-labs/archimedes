"""clear every OAuth ID token already stored in auth_accounts

Issue #1908, checkbox "Google ID token stored unencrypted". Better Auth's
``encryptOAuthTokens: true`` (auth/auth.js) encrypts ``accessToken`` and
``refreshToken`` only; ``auth_accounts."idToken"`` held the token exactly as
the provider issued it. For Google that is a JWT whose payload carries the
user's email, name and picture, readable by anyone who can read the table or
a backup of it.

From this change on the auth service never writes the column (auth/auth.js
``dropIdToken``, a ``databaseHooks.account.{create,update}.before`` hook).
This revision clears the rows written before that. Nothing reads the value:
sign-in and linking take the ID token from the token exchange, not from the
row, and the user's email, name and image already live in ``auth_users``.

ORDERING. The deploy's migrate task runs before the new auth container takes
traffic, so a Google sign-in served by the OLD container during that rollout
can still write a token. That row is cleared the next time the account is
written (a later sign-in or re-link runs the update hook). A row written in
that window by someone who never signs in again keeps its token: a known
residual, bounded by one deploy's rollout.

IDEMPOTENT. ``WHERE "idToken" IS NOT NULL`` makes a second run touch zero
rows. No other column is written.

DOWNGRADE is a deliberate no-op. The cleared tokens are not recoverable, and
nothing needs them back: the column stays (schema unchanged in both
directions), so code from before this change keeps working after a downgrade
and simply starts storing tokens again.

Revision ID: 7d2f9a4c1e60
Revises: c8a4d1f70b93
Create Date: 2026-10-01 12:00:00.000000
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7d2f9a4c1e60"
down_revision: str | Sequence[str] | None = "c8a4d1f70b93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    result = op.get_bind().execute(sa.text('UPDATE auth_accounts SET "idToken" = NULL WHERE "idToken" IS NOT NULL'))
    logger.info("#1908 cleared %d stored OAuth ID token(s) from auth_accounts.", result.rowcount)


def downgrade() -> None:
    logger.info("#1908 downgrade: cleared ID tokens are not restored; schema is unchanged.")
