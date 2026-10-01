"""the passport records WHICH link of the display chain its numbers came from

#1746 PR-B moved the curated display chain (``real_* → persisted backtest →
``stub_*``) from the read side to the write side: the passport sync resolves it
once and stores the ANSWER in ``sharpe_ratio`` / ``cagr`` / ``max_drawdown`` /
``win_rate`` / ``calmar_ratio`` / ``correlation_to_spy``, and every read surface
serves the row. That fixed the disagreement between
``GET /api/strategies/{id}`` and ``GET /api/strategies/passports/{id}``, and it
moved one thing the row did not carry: the LABEL naming which link produced the
number.

Two consequences the label being un-stored caused, both of which this column
closes:

  * ``/api/strategies/passports/{id}`` now publishes numbers resolved through
    the whole chain — INCLUDING ``stub_placeholder``, a constant hand-declared
    in the strategy file. Before PR-B that payload carried link 1 only (NULL for
    a strategy with no fixture row), so a stub could not reach it. With no
    provenance field on the payload, a hand-declared stub is indistinguishable
    from a measured backtest number on an agent-facing route — while
    ``GET /api/strategies/{id}`` labels the very same number.

  * ``display_metrics_source`` on the detail route was derived per request from
    ``LocalStrategyProvider``'s boot-time backtest memo, while the NUMBER came
    from the row. A serving task whose memo predates a grading run could
    therefore label a real persisted-backtest number ``stub_placeholder``. One
    written label, read by both surfaces, cannot do that.

WHAT THE COLUMN IS
  ``display_metrics_source`` — one of ``services.curated_metrics``'s four
  labels: ``strategy_record`` (the #1187 fixture snapshot the curated record
  ships with) | ``persisted_backtest`` | ``stub_placeholder`` | ``unavailable``.
  Nullable, no server default, NO BACKFILL.

WHY NO BACKFILL. The label is a fact about which of three sources supplied a
number, and SQL cannot see two of them: the fixture-vs-stub distinction lives in
the strategy FILE (``BACKTEST_*`` module constants), not in this table, and
``strategy_record`` vs ``persisted_backtest`` needs the provider's view of
``backtest_results`` at write time. Guessing here would be inventing provenance,
which is the class of thing #1187 and #1746 are both about. Existing rows stay
NULL until the passport sync next rewrites them — which happens on the next
process start, because ``_sync_to_unified_table`` runs a ``force_update`` for
every curated strategy on boot. Until then the read path falls back to the old
per-request derivation, so no surface loses a label; it just keeps the weaker
one for one boot.

GENERATED ROWS STAY NULL, permanently and correctly. The display chain is a
curated-library construct; a generated strategy's numbers come from its own
pipeline backtest and there is no link to name.

Owner: Dan Browne. Decision of record: ``docs/adr/rigor-verdict-of-record.md``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a4d7e1b93c2f"
down_revision: str | Sequence[str] | None = "e6b2a19c4d70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic reads these module globals reflectively.
__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

_TABLE = "strategy_passports"
_COLUMN = "display_metrics_source"


def upgrade() -> None:
    # Nullable with no server default: NULL means "this row's provenance was
    # never written", which is a different and honest statement from any of the
    # four labels. See the module docstring on why nothing is backfilled.
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(length=32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_column(_COLUMN)
