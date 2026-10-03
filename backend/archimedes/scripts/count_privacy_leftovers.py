r"""Read-only count of the two privacy leftovers alembic revision 5cdd4f098a74 purges (#1432).

Prints three counts and changes nothing:

  * ``identity_events_siwe_vid_pairs_in_window``: ``auth_verified`` rows inside
    the SIWE window that still carry a ``vid``. The revision nulls these.
  * ``identity_events_vid_rows_outside_scope``: every other row that carries
    a ``vid``. The revision leaves these alone, and the expected value is 0.
    A non-zero value means a writer this PR's analysis did not find.
  * ``chat_messages_rows``: rows left from the per-vault chat deleted in
    8082f150. The revision deletes these. Printed as ``absent`` when the table
    does not exist.

Read-only by construction, not just by intent. The whole run is one
transaction opened READ ONLY (Postgres ``SET TRANSACTION READ ONLY``, SQLite
``PRAGMA query_only``) and rolled back at the end. The script issues only
SELECTs, and the database itself refuses a write from it.

The window bounds are literals copied from the revision, not imported from
it: the revision file is not part of the ``archimedes`` package, so the
script would otherwise depend on the migrations directory being shipped in
the image. ``tests/test_purge_privacy_leftovers_migration.py`` asserts the two
copies are equal.

Usage, locally (``DATABASE_URL`` set, run from ``backend/``):

    python -m archimedes.scripts.count_privacy_leftovers

or as a one-off Fargate task on the ``archimedes-migrate`` task family, with
the same cluster and network configuration as ``.github/workflows/deploy.yml``'s
``migrate`` job (ECS_CLUSTER, ECS_MIGRATE_NETWORK_CONFIGURATION there):

    aws ecs run-task --region us-east-1 --cluster archimedes-cluster \
      --task-definition archimedes-migrate --launch-type FARGATE \
      --network-configuration "$ECS_MIGRATE_NETWORK_CONFIGURATION" \
      --overrides '{"containerOverrides":[{"name":"migrate","command":["python","-m","archimedes.scripts.count_privacy_leftovers"]}]}'

The output lands in CloudWatch Logs, group ``/archimedes/app``, stream prefix
``ecs-migrate``. The migrate task definition's image tag is
``var.backend_image_tag`` (default ``latest``, ``infra/ecs_migrate.tf``) and
every deploy pushes ``:latest``, so the script is in that image only after
the deploy that ships it, and that deploy's migrate job has already run the
revision. A run after it therefore reports what is left (expected 0, 0, 0).
The revision logs the counts it changed in the same log group.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

import sqlalchemy as sa

#: Same values as WINDOW_START / WINDOW_END in
#: backend/migrations/versions/5cdd4f098a74_purge_vid_wallet_pairs_and_chat_rows.py.
WINDOW_START = datetime(2026, 7, 6, 0, 0, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 8, 19, 15, 30, 21, tzinfo=UTC)

_IN_SCOPE = (
    "event_type = 'auth_verified' AND vid IS NOT NULL AND occurred_at >= :window_start AND occurred_at < :window_end"
)


def _window_bound(statement: str) -> sa.TextClause:
    return sa.text(statement).bindparams(
        sa.bindparam("window_start", WINDOW_START, type_=sa.DateTime(timezone=True)),
        sa.bindparam("window_end", WINDOW_END, type_=sa.DateTime(timezone=True)),
    )


def count_leftovers(engine: sa.engine.Engine) -> dict[str, int | None]:
    """Return the three counts. ``chat_messages_rows`` is None when the table is absent."""
    with engine.connect() as conn:
        if conn.dialect.name == "postgresql":
            conn.execute(sa.text("SET TRANSACTION READ ONLY"))
        elif conn.dialect.name == "sqlite":
            conn.execute(sa.text("PRAGMA query_only = ON"))
        try:
            in_window = conn.execute(
                _window_bound(f"SELECT COUNT(*) FROM identity_events WHERE {_IN_SCOPE}")
            ).scalar_one()
            outside = conn.execute(
                _window_bound(f"SELECT COUNT(*) FROM identity_events WHERE vid IS NOT NULL AND NOT ({_IN_SCOPE})")
            ).scalar_one()
            chat: int | None = None
            if sa.inspect(conn).has_table("chat_messages"):
                chat = conn.execute(sa.text("SELECT COUNT(*) FROM chat_messages")).scalar_one()
        finally:
            conn.rollback()
            if conn.dialect.name == "sqlite":
                conn.execute(sa.text("PRAGMA query_only = OFF"))
                conn.rollback()
    return {
        "identity_events_siwe_vid_pairs_in_window": int(in_window),
        "identity_events_vid_rows_outside_scope": int(outside),
        "chat_messages_rows": None if chat is None else int(chat),
    }


def main() -> int:
    from archimedes.db import engine

    counts = count_leftovers(engine)
    print(f"window=[{WINDOW_START.isoformat()}, {WINDOW_END.isoformat()})")
    for key, value in counts.items():
        print(f"{key}={'absent' if value is None else value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
