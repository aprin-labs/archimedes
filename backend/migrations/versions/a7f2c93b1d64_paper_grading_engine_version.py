"""stamp the grading engine on paper ledger rows; separate re-grade from drift

Issue #1449. ``paper_trading.replay_spec`` calls the SAME ``run_dsl_backtest``
the grader uses — by design, so paper semantics track graded semantics — which
means a change to the graded path's own cost model re-grades every open
deployment's replayed history at once. #1379 (the Engine C slippage floor) is
exactly such a change. Before this revision the resulting disagreement was
indistinguishable from an upstream data restatement, so it would have stamped
``drift_detected_at`` on every active deployment and told every user their
track record had restated itself — a claim about THEM for a change that was
OURS.

Two columns, one decision:

  1. ``paper_daily_returns.engine_version`` — which grading engine produced
     this number (``fusion_evaluator.GRADING_ENGINE_VERSION`` at append time).
  2. ``paper_deployments.engine_regrade_at`` — set when a replay disagrees
     with written rows and the cause is the engine, not the data.

**No backfill, on purpose.** Rows written before this column existed were
graded by a build that did not record its own version, and there is no
recoverable fact that says which. Stamping them with today's string would
manufacture the provenance needed to make a comparison come out clean; deriving
one from ``appended_at`` versus a deploy timestamp would encode an operator
guess as data. NULL means "unrecorded", and ``paper_trading.classify_drift``
gives NULL its own bucket (``DRIFT_UNVERSIONED``) — annotated on
``engine_regrade_at``, counted on the deployment payload as ``unversioned_rows``,
and named as unattributable in the log — rather than folding it into either
answer.

The consequence is disclosed rather than hidden: a genuine upstream restatement
of a PRE-EXISTING row will read as unattributable rather than as loud data
drift, permanently, because the ledger is append-only and those rows are never
rewritten to gain a stamp. Every row appended from this revision onward carries
one, so the blind spot is bounded to history that already exists on the day
this ships.

The ledger itself is untouched. This revision adds columns; it rewrites no
``daily_return``, which is the whole law of the table.

Revision ID: a7f2c93b1d64
Revises: d7c41f9b2e58
Create Date: 2026-08-31 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7f2c93b1d64"
down_revision: str | Sequence[str] | None = "d7c41f9b2e58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    # Nullable with no server_default: "unrecorded" must stay distinguishable
    # from any real version string. A default would silently assert that every
    # historical row was graded by whatever value we picked.
    op.add_column("paper_daily_returns", sa.Column("engine_version", sa.String(length=64), nullable=True))
    op.add_column("paper_deployments", sa.Column("engine_regrade_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("paper_deployments", "engine_regrade_at")
    op.drop_column("paper_daily_returns", "engine_version")
