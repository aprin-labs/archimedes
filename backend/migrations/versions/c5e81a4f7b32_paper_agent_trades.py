"""record the trades the agent tick loop decides for paper deployments

Issue #1410. The vault feature's most-MVP piece — the agent's
signal→target-weights→diff→rebalance loop — had zero vaults to run against and
therefore zero validation, while paper deployments were advanced by a replay
that exercises none of it. Pointing the same decision core at a paper venue
validates the mechanic with no chain risk; this table is where its output lands.

WHY A NEW TABLE RATHER THAN A COLUMN ON AN EXISTING ONE. The two paper tables
that already exist both carry laws this data would violate:

  * ``paper_daily_returns`` is the append-only TRACK RECORD, produced by the
    graded replay. Writing agent-decided rows into it would mean the number a
    user's track record reports was produced by something other than the engine
    that graded the verdict they paid for.
  * ``paper_marks`` is explicitly "a decoration with a TTL", safe to delete
    wholesale, with a three-tier retention policy that ends in DELETE. Trade
    provenance is not a decoration and must not inherit a deletion policy
    written for one.

So agent-driven execution gets its own table and touches neither. #1410's third
anti-goal — "do NOT change paper-trading valuation math" — holds literally:
this revision adds a table and alters nothing.

WEIGHTS, NOT DOLLARS. ``paper_deployments`` has no notional column; there is no
deployed capital amount anywhere in this system. A trade is therefore stored as
the portfolio FRACTION that moved (``prior_weight`` → ``target_weight``, with
the signed ``weight_delta``). Storing a dollar size would require inventing the
notional, which is the claim ``paper_marks`` already refuses to make.

NO BACKFILL, and nothing to backfill: before this revision no agent-driven
paper trade existed. An empty table means "the agent has not traded here yet",
which is true, rather than a manufactured history.

Revision ID: c5e81a4f7b32
Revises: a7f2c93b1d64
Create Date: 2026-08-31 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c5e81a4f7b32"
down_revision: str | Sequence[str] | None = "c1d7f4a9b3e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    op.create_table(
        "paper_agent_trades",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "deployment_id",
            sa.String(length=32),
            sa.ForeignKey("paper_deployments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # NOT NULL on purpose: a trade that cannot name the tick that decided it
        # is the exact thing this table was added to make impossible.
        sa.Column("tick_id", sa.String(length=32), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("direction", sa.String(length=4), nullable=False),
        sa.Column("prior_weight", sa.Float(), nullable=False),
        sa.Column("target_weight", sa.Float(), nullable=False),
        sa.Column("weight_delta", sa.Float(), nullable=False),
        # Nullable only for the USDC residual leg, which no strategy votes on.
        sa.Column("signal_strategy_id", sa.String(length=128), nullable=True),
        sa.Column("signal_state", sa.String(length=8), nullable=True),
        sa.Column("signal_reason", sa.Text(), nullable=True),
        sa.Column("spec_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        # A re-applied tick writes nothing the second time instead of doubling a
        # position — the same idempotent-append rule paper_marks and
        # paper_decision_traces already follow.
        #
        # Declared INSIDE create_table, never as a follow-up
        # `op.create_unique_constraint`: SQLite has no ALTER for constraints, so
        # the separate call raises NotImplementedError and every
        # `alembic upgrade head` test that runs against a tmp SQLite file fails.
        # Same placement as uq_paper_decision_traces_dep_date, for the same reason.
        sa.UniqueConstraint("deployment_id", "tick_id", "symbol", name="uq_paper_agent_trades_dep_tick_symbol"),
    )
    op.create_index(
        "ix_paper_agent_trades_dep_decided",
        "paper_agent_trades",
        ["deployment_id", "decided_at"],
    )


def downgrade() -> None:
    # The unique constraint is part of the table definition, so dropping the
    # table drops it; a separate drop_constraint would be the same SQLite
    # NotImplementedError in reverse.
    op.drop_index("ix_paper_agent_trades_dep_decided", table_name="paper_agent_trades")
    op.drop_table("paper_agent_trades")
