"""add paper_marks (intraday mark-to-market) + the position-set cache

Intraday paper trading v1 (docs/plans/2026-08-30-intraday-paper-trading.md §3,
§4.1). Two schema changes, deliberately in ONE revision because they are one
data-shape decision: the marks table is unusable without the position set the
marks value, and the position cache has no other consumer.

  1. ``paper_marks`` — one 15-minute mark-to-market per active deployment.
     A DECORATION WITH A TTL, not the track record: ``paper_daily_returns``
     stays append-only-by-law and stays the recorded paper track record
     (Arc testnet, no real funds).
     That is what makes the retention policy (7d raw → 90d hourly → deleted)
     safe to run, and the policy ships with the table rather than after the
     first bill — the lesson from ``backtest_results`` reaching 6.3 GB with no
     retention policy and no size alarm.

  2. ``paper_deployments.position_cache_json`` / ``position_cache_at`` — the
     position set the DAILY replay last established, written once per advance
     and READ (never written) by the marks loop. The one-way arrow is the
     safety argument: marks cannot change what the strategy does.

**No backfill.** Marks start now. There is no way to reconstruct an intraday
observation after the fact, and a fabricated one would be exactly the
"plausible substitute for a measurement" this repo's fail-soft rule forbids —
so the table starts empty and the UI's no-marks-yet state (an em-dash with a
reason, never ``+0.00%``) is what a pre-existing deployment renders until its
first tick.

Revision ID: e41c7a9b2d63
Revises: 85ca5310b7a1
Create Date: 2026-08-30 21:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e41c7a9b2d63"
down_revision: str | Sequence[str] | None = "85ca5310b7a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

# BIGSERIAL on Postgres; plain INTEGER on SQLite, where a BIGINT primary key is
# not a rowid alias and therefore does not autoincrement. Mirrors the ORM
# column in models/paper_store.py exactly — the two must agree or a fresh
# create_all() DB and a migrated DB disagree on the key's type.
_MARK_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "paper_marks",
        sa.Column("id", _MARK_ID, autoincrement=True, nullable=False),
        sa.Column("deployment_id", sa.String(length=32), nullable=False),
        # UPSTREAM observation time, not write time (§2.4 rule 1).
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        # What was ACTUALLY observed, per symbol — a leg too stale to use is
        # absent rather than carried at a stale price (§1.2).
        sa.Column("prices_json", sa.Text(), nullable=False),
        # An INDEX (1.0 == deploy-time capital), never dollars: there is no
        # deployed-capital column anywhere in this system to render dollars
        # from (§3.1).
        sa.Column("portfolio_value", sa.Float(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("is_delayed", sa.Boolean(), nullable=False),
        sa.Column("granularity", sa.String(length=8), nullable=False, server_default="raw"),
        sa.ForeignKeyConstraint(["deployment_id"], ["paper_deployments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # Makes a re-run of the daily rollup a no-op instead of a duplicate.
        sa.UniqueConstraint("deployment_id", "ts", "granularity", name="uq_paper_marks_dep_ts_gran"),
    )
    # The read shape of every consumer: newest-first marks for one deployment.
    op.create_index("ix_paper_marks_dep_ts", "paper_marks", ["deployment_id", sa.text("ts DESC")])

    op.add_column("paper_deployments", sa.Column("position_cache_json", sa.Text(), nullable=True))
    op.add_column("paper_deployments", sa.Column("position_cache_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("paper_deployments", "position_cache_at")
    op.drop_column("paper_deployments", "position_cache_json")
    op.drop_index("ix_paper_marks_dep_ts", table_name="paper_marks")
    op.drop_table("paper_marks")
