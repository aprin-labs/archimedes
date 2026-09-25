"""add paper_decision_traces and the deployment's trace-coverage columns

Issue #1575 — wire paper-trading decisions into the commit-reveal trace
pipeline. The table is BOTH the idempotency key and the durable record of a
publish that did not happen:

  * ``UniqueConstraint(deployment_id, decision_date)`` is the key.
    ``advance_deployment`` re-derives every historical decision on every
    settle (the backtest engine is a position FSM with no serialisable
    state), so without a durable key a deployment would republish its whole
    decision history daily.
  * ``status`` in {published, failed, unowned, disabled} + ``error`` is the
    loud absence. A design where "published" is durable and "failed" is a log
    line degrades into a silent zero the first time Redis blips, which is the
    fail-soft defect CLAUDE.md names.

``paper_deployments`` gains three columns: ``anchor_traces`` (per-deployment
opt-in to on-chain anchoring — see the design doc §6 on consent),
``trace_gap_at`` (a decision without a published trace) and ``trace_drift_at``
(a re-replay disagreeing with a decision date whose trace is already
published; the trace is never rewritten, the disagreement is stamped).

Revision ID: d7c41f9b2e58
Revises: e41c7a9b2d63
Create Date: 2026-08-30 21:00:00.000000

SEQUENCING: authored against 85ca5310b7a1, verified the single head at write
time. The two-heads fork has bitten this repo twice — keep the chain serial.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d7c41f9b2e58"
down_revision: str | Sequence[str] | None = "e41c7a9b2d63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    op.create_table(
        "paper_decision_traces",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("deployment_id", sa.String(length=32), nullable=False),
        sa.Column("decision_date", sa.Date(), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("trace_hash", sa.String(length=80), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("provenance", sa.String(length=16), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        # CASCADE matches paper_daily_returns: the decision journal is part of
        # the same private per-user ledger and must not outlive it.
        sa.ForeignKeyConstraint(["deployment_id"], ["paper_deployments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("deployment_id", "decision_date", name="uq_paper_decision_traces_dep_date"),
    )
    op.create_index("ix_paper_decision_traces_dep", "paper_decision_traces", ["deployment_id"])

    # server_default on anchor_traces so existing rows get an explicit false
    # rather than a NULL that would read as "unset" at the anchoring gate.
    op.add_column(
        "paper_deployments",
        sa.Column("anchor_traces", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("paper_deployments", sa.Column("trace_gap_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("paper_deployments", sa.Column("trace_drift_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("paper_deployments", "trace_drift_at")
    op.drop_column("paper_deployments", "trace_gap_at")
    op.drop_column("paper_deployments", "anchor_traces")
    op.drop_index("ix_paper_decision_traces_dep", table_name="paper_decision_traces")
    op.drop_table("paper_decision_traces")
