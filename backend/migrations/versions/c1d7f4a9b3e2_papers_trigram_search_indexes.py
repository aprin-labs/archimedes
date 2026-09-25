"""trigram GIN indexes on papers.title / papers.abstract for catalog search

Issue #1665.

Catalog search is an unanchored substring match — ``title ILIKE '%momentum%'``
OR ``abstract ILIKE '%momentum%'`` OR the author leg. A leading wildcard makes
every B-tree index on those columns useless, and ``papers`` carried only
``ix_papers_primary_category`` / ``ix_papers_published``, so **every search was
a sequential scan of the whole table** — 10 000 rows with Text abstracts, on
the request path, for each keystroke.

``pg_trgm`` + a GIN index over trigrams is the fix that keeps the predicate
honest. It indexes the three-character shingles of a string, so the planner can
answer ``LIKE '%mom%'`` from the index instead of reading every row, and
``gin_trgm_ops`` supports ``ILIKE`` as well as ``LIKE`` (Postgres rewrites
``ILIKE`` through the same operator class). Nothing about the query changes:
the same rows come back in the same order. Only the plan changes, from
``Seq Scan`` to ``Bitmap Index Scan``.

Two constraints shaped how this is written.

**CONCURRENTLY, always.** A plain ``CREATE INDEX`` takes an ``ACCESS
EXCLUSIVE`` lock on ``papers`` for the duration of the build — on a live table
that is a hard outage of the catalog, the KB intake job, and anything else that
touches the corpus, for as long as the build runs. ``CONCURRENTLY`` builds in
two passes without blocking reads or writes; it cannot run inside a transaction,
which is why both statements sit in ``autocommit_block()``. Alembic commits the
migration's transaction on entry to that block and opens a new one on exit, so
this revision is deliberately *not* atomic. The known consequence, stated
rather than hidden: if a concurrent build is interrupted, Postgres leaves an
**INVALID** index behind that is never used by the planner and is not rebuilt by
a re-run (``IF NOT EXISTS`` sees the invalid index and skips). Recovery is
manual and cheap —

    SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid;
    DROP INDEX CONCURRENTLY <name>;   -- then re-run this migration

— and that is the right trade against locking a live table.

**Postgres only.** ``pg_trgm`` is a Postgres extension; SQLite (every test, and
local dev via ``create_all()``) has no equivalent and no seq-scan problem at
these row counts. The whole revision is a no-op off Postgres in both
directions, decided from ``bind.dialect.name`` so it renders correctly in
``--sql`` offline mode too — same idiom as
``fb8d0bae8112_schema_relations_phase1``.

The extension is created but **never dropped on downgrade**. ``pg_trgm`` is
database-scoped and something else may since have come to depend on it;
dropping an extension this migration did not necessarily install would take
their indexes with it. Downgrade removes exactly what upgrade added: the two
indexes.

Revision ID: c1d7f4a9b3e2
Revises: a7f2c93b1d64
Create Date: 2026-08-31 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "c1d7f4a9b3e2"
down_revision: str | Sequence[str] | None = "c4e17b93a2d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

# Both legs of the title/abstract predicate get one. Indexing only `title`
# would leave the abstract leg — the larger column, and the expensive half of
# the scan — unindexed, and the OR means the planner still has to read every
# row to evaluate it, so a one-sided index buys nothing. The `authors` leg is
# deliberately left alone: it is guarded to >= 3 characters and is a serialised
# JSON list rather than prose, so it is a separate question from this one.
_TRIGRAM_INDEXES: tuple[tuple[str, str], ...] = (
    ("ix_papers_title_trgm", "title"),
    ("ix_papers_abstract_trgm", "abstract"),
)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return

    # Its own statement, inside the migration's transaction: CREATE EXTENSION
    # is transactional and must land before the autocommit block below, since
    # `gin_trgm_ops` does not exist until it does.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    with op.get_context().autocommit_block():
        for index_name, column in _TRIGRAM_INDEXES:
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {index_name} ON papers USING gin ({column} gin_trgm_ops)"
            )


def downgrade() -> None:
    if not _is_postgres():
        return

    # DROP INDEX CONCURRENTLY carries the same no-transaction rule as the
    # build, and the same reason: a plain DROP takes ACCESS EXCLUSIVE on the
    # table. Reverse order of creation, for symmetry with upgrade().
    with op.get_context().autocommit_block():
        for index_name, _column in reversed(_TRIGRAM_INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {index_name}")

    # pg_trgm itself is deliberately left installed — see the module docstring.
