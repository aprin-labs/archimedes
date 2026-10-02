"""purge two privacy leftovers: SIWE vid-wallet pairs and per-vault chat rows

Part of #1432 (Privacy/Terms). Neither table references ``auth_users`` (both
key on ``wallet_identities``), so the account-deletion cascade never reaches
them. This revision clears what the policy pages no longer describe.

(a) ``identity_events.vid`` on SIWE sign-in rows
-------------------------------------------------
From #1028 until the SIWE router stopped serving in production, the SIWE
verify route (``api/auth_siwe.py`` ``verify_signature``) wrote one
``event_type = 'auth_verified'`` row per sign-in carrying both the browser
visitor id (``vid``, the ``archimedes_vid`` cookie) and the verified wallet.
It was the only call site that ever passed ``vid`` to ``emit_identity_event``
(``git log -S "vid=" -- backend/archimedes`` returns only 6591c2d4, the #1028
commit that added both the parameter and that call).

This revision sets ``vid`` to NULL on exactly those rows and changes nothing
else: wallet, event_type, actor_class, occurred_at and meta stay, so the
ledger and every metric built on it (none of which reads ``vid``) keep their
history. The scope is three conditions together:

  * ``event_type = 'auth_verified'`` (the SIWE write),
  * ``vid IS NOT NULL`` (what makes it a pair, and what makes a re-run a no-op),
  * ``WINDOW_START <= occurred_at < WINDOW_END``.

WINDOW_START, 2026-07-06T00:00:00Z: midnight UTC on the day #1028's ledger
revision (07e9c1489199) is dated. ``identity_events`` did not exist before
it, so the lower bound excludes nothing that could exist.

WINDOW_END, 2026-08-19T15:30:21Z (exclusive): the moment production stopped
running the old code. PR #1194 (merge 9c8cbf95, 2026-08-19T14:47:21Z) carried
both halves of the cut-over: ab188742 routes ``/api/auth/`` in
``nginx/nginx.conf`` to the Better Auth container (``auth_service``), and
69cf1edb mounts the SIWE router only when ``TESTING`` is set. The deploy.yml
runs for 9c8cbf95 and 793ba70b were cancelled during the image build and the
run for dfdf163f failed at nginx image validation, so none of them pushed an
image. The first deploy carrying #1194 was run 32269345739 (head 2d2ee562,
PR #1285). Its "Register a new task-definition revision + force-redeploy the
service" step, which ends in ``aws ecs wait services-stable``, completed at
2026-08-19T15:30:20Z (GitHub reports whole seconds), so the bound is the next
whole second. Any ``auth_verified`` row with a vid written after that would
come from a writer this analysis did not find, so it is left alone. The count
script reports such rows separately.

(b) ``chat_messages`` rows
--------------------------
Per-vault chat (service, routes, UI) was deleted in 8082f150, merged to main
in #1595 on 2026-08-31 and first deployed by deploy.yml run 33445549916
(head 848673e0, finished 2026-08-31T22:29:30Z). No code on main reads or
writes ``chat_messages`` rows. This revision deletes every row.

The TABLE is kept, on purpose. ``ChatMessage`` is still mapped in
``models/chat.py``, so the SQLite ``create_all()`` path would recreate it.
``init_db()`` still issues ``ALTER TABLE chat_messages ADD COLUMN IF NOT
EXISTS verified`` on Postgres. No request handler calls ``init_db()`` on
main: the comment above ``main.py``'s call records that the request-handler
calls were removed on 2026-09-03. The web process runs it at boot, once from
``main.py`` at import and again from the lifespan's request-path warmup (on
by default) before uvicorn listens, and batch scripts such as
``scripts/run_paper_marks.py`` call it too. Against a dropped table that
statement would fail, as a logged WARNING, on each of those calls. A drop
belongs in a change that removes the mapping and that patch in the same PR.
A downgrade of a drop could only recreate an empty table, so it would not be
a true reversal.

IDEMPOTENT. The ``vid IS NOT NULL`` condition and an unconditional
``DELETE`` mean a second run changes zero rows. Each statement logs its row
count (``alembic.runtime.migration`` logger, so the ECS migrate task's
CloudWatch stream records the numbers this deploy changed).

ORDERING. Both writers were already gone from production before this runs
(SIWE since 2026-08-19T15:30:20Z, chat since 2026-08-31T22:29:30Z), so the
old containers still serving during this rollout cannot write new rows of
either kind.

DOWNGRADE is a deliberate no-op. The cleared vids and deleted messages are
not recoverable, and nothing needs them back. The schema is unchanged in
both directions.

Revision ID: 5cdd4f098a74
Revises: 7d2f9a4c1e60
Create Date: 2026-10-02 12:00:00.000000
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "5cdd4f098a74"
down_revision: str | Sequence[str] | None = "7d2f9a4c1e60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = [
    "WINDOW_END",
    "WINDOW_START",
    "branch_labels",
    "depends_on",
    "down_revision",
    "downgrade",
    "revision",
    "upgrade",
]

logger = logging.getLogger("alembic.runtime.migration")

#: Inclusive. See the module docstring for the evidence behind both bounds.
WINDOW_START = datetime(2026, 7, 6, 0, 0, 0, tzinfo=UTC)
#: Exclusive. See the module docstring for the evidence behind both bounds.
WINDOW_END = datetime(2026, 8, 19, 15, 30, 21, tzinfo=UTC)

_NULL_SIWE_VIDS = sa.text(
    "UPDATE identity_events SET vid = NULL "
    "WHERE event_type = 'auth_verified' AND vid IS NOT NULL "
    "AND occurred_at >= :window_start AND occurred_at < :window_end"
).bindparams(
    sa.bindparam("window_start", WINDOW_START, type_=sa.DateTime(timezone=True)),
    sa.bindparam("window_end", WINDOW_END, type_=sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    bind = op.get_bind()

    nulled = bind.execute(_NULL_SIWE_VIDS).rowcount
    logger.info(
        "#1432 nulled vid on %d SIWE auth_verified identity_events row(s) in [%s, %s).",
        nulled,
        WINDOW_START.isoformat(),
        WINDOW_END.isoformat(),
    )

    if sa.inspect(bind).has_table("chat_messages"):
        deleted = bind.execute(sa.text("DELETE FROM chat_messages")).rowcount
        logger.info("#1432 deleted %d chat_messages row(s); the table is kept.", deleted)
    else:
        logger.info("#1432 chat_messages table absent; nothing to delete.")


def downgrade() -> None:
    logger.info("#1432 downgrade: cleared vids and deleted chat messages are not restored; schema is unchanged.")
