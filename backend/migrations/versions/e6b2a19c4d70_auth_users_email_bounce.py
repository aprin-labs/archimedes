"""record a bounced or complained email address on the user (#1804)

WHAT THIS EXISTS FOR. ``auth_users`` carried exactly one fact about an
address: ``emailVerified``. That single boolean is also the free-generation
gate (``services/free_generations.py``), so an address AWS SES has told us is
dead — a hard bounce, which puts the address on the account suppression list
and makes every later ``SendEmail`` succeed and then silently drop — looks
identical to a real person who simply has not clicked the link yet. Both read
``emailVerified = false`` forever. The person is locked out of the free tier
with no explanation and no path forward, and nothing in the product can tell
the two apart.

These two columns are where the SES feedback loop (``infra/ses_events.tf`` →
``archimedes.scripts.ses_events``) writes what it learned, so signup and the
self-service resend can refuse the address with a reason instead of pretending
mail is on its way (``auth/auth.js``).

COLUMN NAMES ARE camelCase ON PURPOSE. Better Auth owns writes to the
``auth_*`` tables and derives its column names from its own field names; the
table already mixes ``emailVerified``/``createdAt`` with a snake_case ``id``
for that reason. ``auth/auth.js`` declares these two as
``user.additionalFields`` so Better Auth's own schema and this migration
produce the same two columns; renaming either half in isolation makes the auth
service query a column that does not exist.

NULLABLE, NO SERVER DEFAULT, NO BACKFILL. A NULL here means "SES has never
told us anything bad about this address", which is the truth for every row
that exists today: the bounces that already happened were published nowhere
(there was no configuration set — that is the whole of #1804), so they cannot
be reconstructed. The only historical record of them is the SES account
suppression list, which ``archimedes.scripts.ses_suppression list`` reads and
which stays the operator's tool for the pre-loop past. Inventing a timestamp
for those rows would be fabricating evidence.

NO CHECK CONSTRAINT ON ``emailBounceKind``. The vocabulary is closed
(``bounce`` | ``complaint``) and enforced at the one place that writes it
(``scripts/ses_events.py``'s ``_KIND_BY_EVENT_TYPE``, covered by
``backend/tests/test_ses_bounce_consumer.py``). It is deliberately not pinned
in the schema because SES's own event vocabulary is AWS's to extend, and a
CHECK here would turn a new event type into a failed write on a feedback path
whose entire job is to keep recording — the failure mode this issue exists to
remove.

Revision ID: e6b2a19c4d70
Revises: d3a71f5c9e28
Create Date: 2026-09-03 00:00:00.000000

If another migration lands on main first, re-point ``down_revision`` at the
new head rather than forking the chain — ``alembic upgrade head`` refuses to
run with two heads and the pre-rollout migration task blocks every deploy (see
9ad1c4e2b7f0's header for the two times this exact branch-vs-main race
happened). This revision only ADDS two nullable columns to ``auth_users`` and
reads nothing, so serialising it after any other revision is always safe.

Re-pointed twice for exactly that reason: first from ``b3f19d6c47ae`` onto
#1790's ``d4b1f7c8e206``, then onto ``d3a71f5c9e28`` (#1283's orphaned-legacy-
row adoption) when that merged while this was in review. The second is safe on
the same grounds AND on a specific one: ``d3a71f5c9e28`` INSERTs an
``auth_users`` row naming its columns explicitly, so two more nullable columns
appearing after it cannot change what that insert does.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e6b2a19c4d70"
down_revision: str | Sequence[str] | None = "d3a71f5c9e28"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic reads these module globals reflectively.
__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

_TABLE = "auth_users"
_COLUMNS = ("emailBouncedAt", "emailBounceKind")


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("emailBouncedAt", sa.DateTime(timezone=True), nullable=True))
    op.add_column(_TABLE, sa.Column("emailBounceKind", sa.String(length=32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        for column in reversed(_COLUMNS):
            batch_op.drop_column(column)
