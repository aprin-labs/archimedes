"""assoc/v1: passport projection columns (schema only, no data rewrite)

Issue #1637, PR-1 of the #1688 split. **A paper association is a record, not a
string** — and this revision adds the four columns the passport projection was
silently dropping, and nothing else.

``passport_paper_refs`` gains ``role`` / ``selection_rank`` /
``semantic_score`` / ``content_hash``. These are the ``assoc/v1`` fields
(``archimedes/models/paper_assoc.py``) the projection could not carry, so the
renderer had no way to show them. All nullable except ``role``, which defaults
to ``"cited"`` — every association that existed before this column WAS a
citation, so that default is a recoverable fact rather than a guess. The other
three have **no** server_default on purpose: ``NULL`` must stay
distinguishable from a real rank / score / hash, and #1091 means
``content_hash`` is genuinely NULL for every paper in production.

────────────────────────────────────────────────────────────────────────────
WHAT THIS REVISION DELIBERATELY DOES NOT DO
────────────────────────────────────────────────────────────────────────────

#1688 also re-stamped ``strategy_store.content_hash`` under the new
identity-only hash definition, and normalized ``strategy_store.source_papers``
to ``assoc/v1`` on every row. Owner decision on #1688 (2026-09-03): *"Yes to
the redefinition, not yet to the re-stamp… a dedup-hygiene step does not get
to be irreversible on an unmeasured table. Restamp lands only after the
dry-run."* So both halves of that data rewrite are held for PR-2, and this
revision is a pure ``ADD COLUMN`` / ``DROP COLUMN`` pair — reversible, byte
for byte.

**Holding the ``source_papers`` normalization back is a DEVIATION from the
owner's PR-1 list, and it needs the owner's sign-off** — the list says "new
``passport_paper_refs`` columns + ``source_papers`` normalization". The reason
it is deferred, and the true cost of deferring it, both belong here rather
than in a merge comment.

The reason is ordering. PR-2 is specified to gate its re-stamp on a read-only
dry-run that recomputes each row's *historical* hash with the frozen pre-#1637
function **over that row's stored ``source_papers`` JSON**, and to re-stamp
only rows where the recomputation reproduces what is stored. That dry-run's
only input is the raw stored shape. Normalize the column in this revision and
the input is gone before the gate ever runs: the legacy hash reproduces for no
row, the dry-run reports a ~0 reproduce rate, and the re-stamp it gates
correctly refuses to act — permanently. (#1688's own
``test_assoc_migration_upgrade_is_idempotent`` leans on the same property to
prove a second pass is a no-op.) So the normalization belongs in the pass that
reads the raw shape, not ahead of it. That is a claim about PR-2's *design*,
not about code that exists yet — which is precisely why it is an owner call
and not a maintainer's tidy-up.

The cost of waiting, stated honestly rather than waved away: **the column
holds two shapes until PR-2 lands.** It is NOT true that every reader
normalizes — only ``StrategyRecord.to_strategy_passport`` does, via
``paper_assoc.cited``. ``to_dict``, ``strategies_by_paper`` and
``resolve_source_papers`` hand the stored JSON back verbatim, so a legacy row
and an ``assoc/v1`` row decode to different dicts on those paths. What holds
instead is narrower: every verbatim path addresses an entry through ``.get()``
on ``arxiv_id`` (plus ``title``/``year`` on the ``/generated`` render path),
which every historical shape either carries or omits, and an omitted key reads
``None`` exactly where ``assoc/v1`` stores ``None``; the ``assoc/v1``-only
keys are additive and no verbatim reader requires one; and the single reader
that branches on ``role`` normalizes first, where a role-less legacy entry
defaults to ``cited`` — what every pre-#1637 association was. The full
argument, with the standing cost, is in ``strategy_store``'s module docstring
under "Legacy rows are not rewritten".

A second, separate cost — this one from the hash REDEFINITION rather than from
the deferred normalization: ``_compute_content_hash`` now
produces a value no pre-existing row carries, so re-upserting the *same*
content inserts a second row instead of matching the first. Nothing downstream
joins on ``content_hash`` (``id`` is the FK every other table uses, and it
never moves), and #1792's stored rigor verdict keys off ``id`` too — so this
is the pre-existing per-writer split-brain moved, not widened. PR-2 closes it.

Revision ID: c8a4d1f70b93
Revises: a4d7e1b93c2f
Create Date: 2026-09-03 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8a4d1f70b93"
down_revision: str | Sequence[str] | None = "a4d7e1b93c2f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

#: The four columns, newest-first for the drop. Written once so upgrade and
#: downgrade cannot disagree about which columns this revision owns.
_COLUMNS = (
    ("role", sa.String(length=16), {"nullable": False, "server_default": "cited"}),
    ("selection_rank", sa.Integer(), {"nullable": True}),
    ("semantic_score", sa.Float(), {"nullable": True}),
    ("content_hash", sa.String(length=64), {"nullable": True}),
)


def upgrade() -> None:
    with op.batch_alter_table("passport_paper_refs") as batch_op:
        for name, type_, kwargs in _COLUMNS:
            batch_op.add_column(sa.Column(name, type_, **kwargs))


def downgrade() -> None:
    with op.batch_alter_table("passport_paper_refs") as batch_op:
        for name, _type, _kwargs in reversed(_COLUMNS):
            batch_op.drop_column(name)
