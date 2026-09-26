"""the passport carries the rigor verdict of record

Owner decision (Dan, 2026-09-01): generation, backtesting and grading are
one-time events. A strategy is graded ONCE, at backtest time, by the real gate,
and that verdict is persisted here with its inputs. Every surface reads the
stored verdict; a re-grade is an explicit, versioned event, never a silent
overwrite and never a recompute on read. See
``docs/adr/rigor-verdict-of-record.md``.

This revision adds the four columns that make a stored verdict readable, and
backfills them for every row that already exists.

WHAT THE COLUMNS ARE
  * ``rigor_gate_status``  — the four-state badge, stored rather than derived
    at read time: ``pass`` | ``fail`` | ``pending`` | ``degenerate`` (the
    vocabulary #1184 defined). NOT NULL with a ``'pending'`` server default, so
    a row nothing has graded says so instead of presenting a fail-closed
    ``passes_rigor_gate = false`` as if it were a verdict.
  * ``graded_at``    — when the grade was produced. NULL ⟺ never graded.
  * ``gate_version`` — WHICH gate produced it
    (``services.rigor_gate_version.gate_version()``). Two rows with different
    values were graded by different gates and are not comparable.
  * ``cohort_n``     — how many return series were in the cohort that supplied
    the grade's cohort-scoped inputs (PBO, average pairwise correlation).

THE BACKFILL RULE, AND WHY EACH BRANCH IS WHAT IT IS

  * ``generation_method = 'curated'`` → **'pending'**, ``gate_version`` NULL.
    Every curated row's ``passes_rigor_gate`` is the literal ``False`` written
    at ``services/strategy_provider.py`` (the deliberate #821 fail-closed
    PLACEHOLDER — its own comment says the served path replaces it with a live
    verdict). It is not a gate result and never was, so carrying it over as
    ``'fail'`` would promote a placeholder into a verdict — the exact class of
    lie this whole change is about. These rows are ungraded, and now say so.
    Grading them is PR-B.

  * every other row → derive **exactly** as the read path derived it before
    this revision (``strategies_routes._passport_rigor_status``, kept in place
    as the oracle a test compares this rule against):
    ``sharpe_ratio IS NULL`` → ``'pending'``; otherwise the stored
    ``passes_rigor_gate`` → ``'pass'`` / ``'fail'``. Those rows get
    ``gate_version = 'legacy-derived'``: a marker, never a real digest, meaning
    "no gate run produced this — it was inferred from pre-existing columns".
    PR-C replaces them with a real re-grade.

  * ``passes_rigor_gate`` is rewritten to ``(status == 'pass')`` on every row,
    which is the coupling invariant the loader now enforces on writes. It also
    REPAIRS a reachable inconsistency: ``passes_rigor_gate = true`` beside
    ``sharpe_ratio IS NULL`` existed in the wild (the generation-time fusion
    verdict wrote the boolean; nothing wrote a status), and the old read path
    reported that row as ``'pending'`` while the deploy gate read the boolean
    as a pass. After this it is ``'pending'`` and ``false``, together.

  * ``graded_at`` stays NULL on every backfilled row. No gate ran at a time we
    know, and inventing one would be a fabricated provenance stamp.

THE VISIBLE CONSEQUENCE, STATED PLAINLY. The old read path had a fifth input
this migration does not: it loaded each row's persisted daily-return series and
reported ``'degenerate'`` for a zero-variance one. SQL cannot reach that (the
series lives inside ``backtest_results.artifact_json``), so a generated row that
today reads "Unevaluable — flat returns" will read "Reference only — gate
failed" after this revision until PR-C re-grades it. Both are non-pass and
neither renders green, so the direction is fail-closed — but it IS a claim
getting less precise for those rows, and the honest fix is the re-grade, not a
recompute on read. Going forward the WRITER stores ``'degenerate'`` as itself
(``verdict_from_returns`` → ``_refresh_passport_real_metrics``), so this is a
one-time cost paid by legacy rows only.

Revision ID: b3f19d6c47ae
Revises: c5e81a4f7b32
Create Date: 2026-09-01 19:10:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3f19d6c47ae"
down_revision: str | Sequence[str] | None = "c5e81a4f7b32"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic reads these module globals reflectively.
__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

_TABLE = "strategy_passports"
_COLUMNS = ("rigor_gate_status", "graded_at", "gate_version", "cohort_n")

# Copied as a LITERAL rather than imported from
# ``archimedes.services.rigor_gate_version.LEGACY_DERIVED``. A migration is a
# historical record: it must keep meaning the same thing when the application
# code around it changes, so it may not depend on a value that code can move.
_LEGACY_DERIVED = "legacy-derived"

_CURATED = "curated"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("rigor_gate_status", sa.String(length=16), nullable=False, server_default="pending"),
    )
    op.add_column(_TABLE, sa.Column("graded_at", sa.DateTime(), nullable=True))
    op.add_column(_TABLE, sa.Column("gate_version", sa.String(length=64), nullable=True))
    op.add_column(_TABLE, sa.Column("cohort_n", sa.Integer(), nullable=True))

    bind = op.get_bind()

    # Booleans are bound, never inlined: sqlite wants 0/1 and Postgres wants
    # true/false, and the driver is the thing that knows which.
    # ORDER NOTE: the two branches are disjoint by construction (curated vs
    # not-curated), and within not-curated the pass/fail pair reads
    # ``passes_rigor_gate`` while the pending branch only writes it, so no
    # statement can observe another's rewrite. Written pass/fail first anyway,
    # so the read-before-write reading is obvious to a reviewer.

    # (1) non-curated, graded, stored boolean true → pass
    bind.execute(
        sa.text(
            f"UPDATE {_TABLE} SET rigor_gate_status = 'pass', gate_version = :legacy "
            "WHERE LOWER(COALESCE(generation_method, '')) <> :curated "
            "AND sharpe_ratio IS NOT NULL AND passes_rigor_gate = :yes"
        ),
        {"legacy": _LEGACY_DERIVED, "curated": _CURATED, "yes": True},
    )

    # (2) non-curated, graded, stored boolean false → fail
    bind.execute(
        sa.text(
            f"UPDATE {_TABLE} SET rigor_gate_status = 'fail', gate_version = :legacy "
            "WHERE LOWER(COALESCE(generation_method, '')) <> :curated "
            "AND sharpe_ratio IS NOT NULL AND (passes_rigor_gate = :no OR passes_rigor_gate IS NULL)"
        ),
        {"legacy": _LEGACY_DERIVED, "curated": _CURATED, "no": False},
    )

    # (3) non-curated, no backtest metrics → pending (never graded)
    bind.execute(
        sa.text(
            f"UPDATE {_TABLE} SET rigor_gate_status = 'pending', gate_version = NULL "
            "WHERE LOWER(COALESCE(generation_method, '')) <> :curated AND sharpe_ratio IS NULL"
        ),
        {"curated": _CURATED},
    )

    # (4) curated → pending. Their stored False is the #821 placeholder, not a
    # verdict. PR-B grades them for real.
    bind.execute(
        sa.text(
            f"UPDATE {_TABLE} SET rigor_gate_status = 'pending', gate_version = NULL "
            "WHERE LOWER(COALESCE(generation_method, '')) = :curated"
        ),
        {"curated": _CURATED},
    )

    # (5) couple the boolean to the four-state on EVERY row, including the ones
    # the branches above did not touch. This is the invariant
    # ``passport_loader._apply_rigor_verdict`` now enforces on every write; the
    # table has to satisfy it before anything relies on it.
    bind.execute(
        sa.text(f"UPDATE {_TABLE} SET passes_rigor_gate = :yes WHERE rigor_gate_status = 'pass'"),
        {"yes": True},
    )
    bind.execute(
        sa.text(f"UPDATE {_TABLE} SET passes_rigor_gate = :no WHERE rigor_gate_status <> 'pass'"),
        {"no": False},
    )


def downgrade() -> None:
    # Drops the columns. ``passes_rigor_gate`` is deliberately NOT restored to
    # its pre-upgrade values: the coupling rewrite above is a REPAIR (a stored
    # true beside a NULL sharpe was never a verdict any read path served as a
    # pass), and re-introducing the inconsistency on the way down would be
    # restoring a bug, not restoring state.
    with op.batch_alter_table(_TABLE) as batch_op:
        for column in reversed(_COLUMNS):
            batch_op.drop_column(column)
