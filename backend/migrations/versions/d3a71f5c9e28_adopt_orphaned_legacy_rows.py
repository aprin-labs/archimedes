"""adopt the orphaned legacy strategy rows under a platform account

Issue #1283, owner decision Q1 (2026-09-02): *"backfill the orphaned legacy
rows to a platform account, or delete them if a clean backfill is not
possible — and clean up everything they touch; the prod database must not
break."* A clean backfill IS possible, so nothing is deleted here. This
revision writes exactly one column on exactly three tables, records what it
wrote, and can hand every row back.

────────────────────────────────────────────────────────────────────────────
WHAT AN ORPHAN IS, PRECISELY
────────────────────────────────────────────────────────────────────────────

A row on ``strategy_store`` / ``strategy_passports`` / ``strategy_proposals``
where:

    owner_user_id IS NULL                       -- pre-account row
AND owner_wallet  IS NOT NULL AND <> ''         -- it names a wallet
AND that wallet is in no ``linked_wallets`` row -- and nobody ever proved it

The third clause is what makes the row *orphaned* rather than merely
*unclaimed*. #1283 already shipped the self-healing reclaim: linking or
re-verifying a wallet, or renaming a strategy matched through it, runs
``claim_legacy_wallet_data`` and stamps ``owner_user_id`` onto every
pre-account row for that address. That mechanism can only ever fire for a
wallet an account has proven control of. A wallet that appears in no
``linked_wallets`` row has no account to fire it, so its rows sit at
``owner_user_id IS NULL`` forever — and meanwhile ``owns_strategy``'s tier-2
fallback (``strategy_visibility.py``) grants them to *whoever* turns up
controlling that address. That is the population the owner decided about.

Deliberately NOT orphans, and untouched by this revision:

  * ``owner_user_id IS NULL AND owner_wallet IS NULL`` — nothing identifies
    the author; the owner-scoped readers filter on an exact non-NULL match so
    these rows are already returned to nobody, and a stamp would attribute an
    anonymous artifact to the platform for no gain. Same call
    ``docs/plans/2026-08-30-relations-phase2.md`` § 2.7 made. Curated/example
    rows are in this bucket (house content carries no ``owner_wallet``), so
    the library is untouched.
  * unclaimed rows whose wallet IS linked — the existing reclaim reaches
    those behind a fresh signature. A migration must not race it.
  * ``vault_metadata``, ``user_profiles``, ``paper_deployments`` — see
    ``archimedes/models/legacy_adoption.py``'s docstring for the three
    separate reasons. In short: adopting a vault row permanently 409s the
    real on-chain owner out of their own metadata; ``user_profiles`` is PII
    with a UNIQUE ``owner_user_id``; ``paper_deployments`` cascades on
    account deletion.

────────────────────────────────────────────────────────────────────────────
THE PRE-FLIGHT, AND WHY IT REFUSES
────────────────────────────────────────────────────────────────────────────

``population = candidates + rows already adopted by this revision``.

  * ``population == 0`` -> no-op, silently (the ledger table is still
    created, because it is schema). Every fresh database (CI, a dev clone,
    ``alembic upgrade head`` from zero) lands here; refusing there would
    break the replay-from-empty contract
    ``test_alembic_upgrade_head_from_empty_db`` pins. Under
    ``ORPHAN_MIGRATION_DRY_RUN`` this branch raises like every other, so a
    rehearsal never writes the table or the version stamp.
  * ``0 < population < 140`` or ``population > 165`` -> **RAISE**. The owner
    decided about a measured population of 152 rows. The band is that figure
    ±≈8%: wide enough to absorb rows that self-healed between the measurement
    and the deploy (a wallet linked, a strategy renamed) or that were written
    in the same window, narrow enough that a materially different number
    means this is not the population the decision was made about. Adopting an
    unknown set of rows under a platform account on the owner's say-so about
    a *different* set is precisely the mistake worth failing loudly on.
    Re-measure, take the decision again, and change the band deliberately.

Three structural refusals follow, each naming a way adoption could leave the
database less consistent than it found it:

  * **D1 — split mirror.** A candidate ``strategy_store`` row whose
    ``strategy_passports`` twin already carries a different, real
    ``owner_user_id`` (or vice versa). Stamping one half would make the two
    ownership mirrors disagree about the same strategy, and ``owns_strategy``
    is called on both shapes at different call sites. That is an
    authorization bug, not an inconsistency.
  * **D2 — parentless passport.** A candidate ``strategy_passports`` row with
    no ``strategy_store`` row of the same id (the relations plan's Q1
    population). Adoption would produce a platform-owned passport for a
    strategy that does not exist. The fix for those rows is the FK work in
    #1438, not an ownership stamp.
  * **D3 — non-lowercase wallet.** ``claim_legacy_wallet_data`` and the
    release path both match the wallet by exact equality against a
    lowercased address. A mixed-case ``owner_wallet`` would be adopted and
    then be impossible to release, turning a reversible write into a
    permanent one. Refuse rather than silently normalize somebody's row.

Nothing here can leave a row dangling in the other direction: this revision
DELETES nothing and changes no primary key, so every FK and soft reference in
the touch map (``backtest_results.strategy_id``, ``debate_transcripts``,
``generation_costs``, ``paper_deployments``, ``passport_paper_refs``,
``strategy_daily_returns``, ``strategy_backtest_fixtures``,
``marketplace_agents``, ``vault_metadata.strategy_ids``,
``identity_events.wallet``) still points at exactly the row it pointed at
before. The only column that moves is ``owner_user_id``.

────────────────────────────────────────────────────────────────────────────
DRY RUN
────────────────────────────────────────────────────────────────────────────

    ORPHAN_MIGRATION_DRY_RUN=1 alembic upgrade head

logs the full plan — per-table candidate counts, the population, the band
verdict, every structural check, and the first few row ids — and then RAISES
``OrphanMigrationDryRun``. The raise is the mechanism, not a bug: alembic runs
a revision inside a transaction, so aborting is what guarantees the
``alembic_version`` stamp is not written. A dry run that returned cleanly
would mark this revision applied and the real run would skip it. **A dry run
is expected to exit non-zero.** Read the log, not the exit code.

The stronger guarantee — that a dry run (or a refusal) writes *nothing at
all*, on any dialect — comes from ORDERING, not from rollback: every raise in
``upgrade()`` happens BEFORE the first DDL or DML statement. That ordering is
load-bearing rather than stylistic, because pysqlite does not open a
transaction for DDL, so a ``CREATE TABLE`` issued before the raise would
survive the abort on SQLite even though it would roll back on Postgres.
There is NO exception: the zero-population early return creates the ledger
deliberately on a real run, and re-checks the dry-run flag first so that a
dry run against a database with no orphans also writes nothing — not the
ledger, and not the ``alembic_version`` stamp that would silently mark this
revision applied.

────────────────────────────────────────────────────────────────────────────
IDEMPOTENCY AND DOWNGRADE
────────────────────────────────────────────────────────────────────────────

Second run is a true no-op: every adopted row now has a non-NULL
``owner_user_id`` so it is no longer a candidate, ``population`` is carried by
the ledger instead, the band still passes, and the write phase finds nothing
to do. The platform ``auth_users`` insert is guarded on existence.

``downgrade()`` re-orphans **ownership only**: for every ledger row, it sets
``owner_user_id`` back to NULL — but only where the current value is still
the platform account, so a row released to its real owner in the meantime is
left alone. It then drops the ledger and removes the platform account if it
owns nothing. That is a complete reversal, because this revision only ever
wrote ownership. **No row is deleted by this revision, so there is no
deletion to be irreversible about** — if a future revision ever does delete
an orphan, its downgrade cannot restore it, and that must be said there.

Revision ID: d3a71f5c9e28
Revises: d4b1f7c8e206
Create Date: 2026-09-02 09:00:00.000000

"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d3a71f5c9e28"
down_revision: str | Sequence[str] | None = "d4b1f7c8e206"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = [
    "EXPECTED_MAX",
    "EXPECTED_MIN",
    "OrphanMigrationDryRun",
    "branch_labels",
    "depends_on",
    "down_revision",
    "downgrade",
    "revision",
    "upgrade",
]

logger = logging.getLogger("alembic.runtime.migration")

#: The owner's decision was taken against a measured 152 rows; see the
#: docstring for why the band is this wide and no wider.
EXPECTED_MIN = 140
EXPECTED_MAX = 165

#: Set to "1"/"true"/"yes" to log the plan and abort before any write.
DRY_RUN_ENV = "ORPHAN_MIGRATION_DRY_RUN"

_LEDGER = "legacy_row_adoptions"

#: Every DECLARED ``ForeignKey("auth_users.id")`` in this schema, grepped from
#: ``backend/archimedes/models/``. Read only by ``downgrade()``, to prove the
#: platform account is unreferenced before deleting it; the CASCADE ones are
#: the reason this list exists rather than a bare DELETE.
#:
#: "Declared" is the exact word, not a hedge. ``payment_receipts.user_id``,
#: ``generation_credits.user_id`` and ``free_generation_grants.user_id`` also
#: hold an ``auth_users.id`` with no FK behind it, and are deliberately absent:
#: all three are written only for an account that authenticated, and this
#: account has no credential row and no session row, so it can never hold one.
#: A future soft reference that a NON-login account CAN acquire must be added
#: here, or the downgrade's DELETE would orphan it silently.
_AUTH_USER_REFERENCES: tuple[tuple[str, str], ...] = (
    ("strategy_store", "owner_user_id"),  # SET NULL
    ("strategy_passports", "owner_user_id"),  # SET NULL
    ("strategy_proposals", "owner_user_id"),  # SET NULL
    ("vault_metadata", "owner_user_id"),  # SET NULL
    ("paper_deployments", "owner_user_id"),  # CASCADE
    ("user_profiles", "owner_user_id"),  # CASCADE
    ("api_keys", "user_id"),  # CASCADE
    ("auth_sessions", '"userId"'),  # CASCADE
    ("auth_accounts", '"userId"'),  # CASCADE
    ("linked_wallets", "user_id"),  # CASCADE
    ("wallet_link_challenges", "user_id"),  # CASCADE
)


class OrphanMigrationDryRun(RuntimeError):
    """Raised to abort the transaction after a dry run has logged its plan."""


def _dry_run_requested() -> bool:
    return os.getenv(DRY_RUN_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _adoptable_tables() -> tuple[tuple[str, str], ...]:
    """The (table, pk column) trio, imported from the app rather than re-listed.

    Deferred import: revision modules are not on the app's import path at
    module scope, but ``backend/`` is on ``sys.path`` by the time a migration
    function runs (see ``migrations/env.py``). Importing the app's own
    constant is what keeps this revision, the release path, and the model
    from ever disagreeing about which tables are adoptable.
    """
    from archimedes.models.legacy_adoption import ADOPTABLE_TABLES

    return ADOPTABLE_TABLES


def _platform_identity() -> tuple[str, str, str]:
    from archimedes.models.account import (
        PLATFORM_LEGACY_EMAIL,
        PLATFORM_LEGACY_NAME,
        PLATFORM_LEGACY_USER_ID,
    )

    return PLATFORM_LEGACY_USER_ID, PLATFORM_LEGACY_EMAIL, PLATFORM_LEGACY_NAME


def _candidate_sql(table: str, pk: str) -> str:
    """Rows that are orphaned on *table*. See the docstring for each clause."""
    return (
        f"SELECT t.{pk} AS row_pk, t.owner_wallet AS owner_wallet "  # identifiers are constants, never user input: identifiers come from ADOPTABLE_TABLES
        f"FROM {table} t "
        "WHERE t.owner_user_id IS NULL "
        "  AND t.owner_wallet IS NOT NULL "
        "  AND t.owner_wallet <> '' "
        "  AND NOT EXISTS ("
        "        SELECT 1 FROM linked_wallets lw "
        "         WHERE lower(lw.address) = lower(t.owner_wallet)"
        "      ) "
        f"ORDER BY t.{pk}"
    )


def _candidates(bind) -> dict[str, list[tuple[str, str]]]:
    out: dict[str, list[tuple[str, str]]] = {}
    for table, pk in _adoptable_tables():
        rows = bind.execute(sa.text(_candidate_sql(table, pk))).mappings().all()
        out[table] = [(row["row_pk"], row["owner_wallet"]) for row in rows]
    return out


def _already_adopted(bind, platform_user_id: str) -> list[tuple[str, str, str]]:
    """Ledger rows this revision wrote, or [] when the ledger does not exist."""
    if not sa.inspect(bind).has_table(_LEDGER):
        return []
    rows = (
        bind.execute(
            sa.text(
                "SELECT table_name, row_pk, prior_owner_wallet FROM legacy_row_adoptions "
                "WHERE adopted_by_user_id = :platform ORDER BY id"
            ),
            {"platform": platform_user_id},
        )
        .mappings()
        .all()
    )
    return [(r["table_name"], r["row_pk"], r["prior_owner_wallet"]) for r in rows]


def _structural_failures(bind, candidates: dict[str, list[tuple[str, str]]]) -> list[str]:
    """D1/D2/D3 from the docstring. Empty list == safe to adopt."""
    failures: list[str] = []

    store_ids = {row_pk for row_pk, _ in candidates.get("strategy_store", [])}
    passport_ids = {row_pk for row_pk, _ in candidates.get("strategy_passports", [])}

    # D1 — split mirror. A candidate on one side whose twin on the other side
    # already carries a real (non-NULL) owner.
    for candidate_ids, twin_table in ((store_ids, "strategy_passports"), (passport_ids, "strategy_store")):
        if not candidate_ids:
            continue
        owned_twins = [
            row[0]
            for row in bind.execute(
                sa.text(
                    f"SELECT id FROM {twin_table} WHERE owner_user_id IS NOT NULL"
                )  # identifiers are constants, never user input: literal table names
            ).all()
            if row[0] in candidate_ids
        ]
        if owned_twins:
            failures.append(
                f"D1 split mirror: {len(owned_twins)} candidate id(s) already have a real owner on "
                f"{twin_table} (first: {sorted(owned_twins)[:5]}). Adopting one half would make the "
                "two ownership mirrors disagree about the same strategy."
            )

    # D2 — parentless passport.
    if passport_ids:
        existing_store = {row[0] for row in bind.execute(sa.text("SELECT id FROM strategy_store")).all()}
        parentless = sorted(passport_ids - existing_store)
        if parentless:
            failures.append(
                f"D2 parentless passport: {len(parentless)} candidate strategy_passports row(s) have no "
                f"strategy_store row (first: {parentless[:5]}). Adoption would create a platform-owned "
                "passport for a strategy that does not exist; fix the population (#1438) first."
            )

    # D3 — non-lowercase wallet.
    mixed = sorted(
        f"{table}:{row_pk}" for table, rows in candidates.items() for row_pk, wallet in rows if wallet != wallet.lower()
    )
    if mixed:
        failures.append(
            f"D3 non-lowercase owner_wallet on {len(mixed)} candidate row(s) (first: {mixed[:5]}). The "
            "release path matches the wallet by exact equality against a lowercased address, so these "
            "rows would be adopted and then impossible to hand back."
        )

    return failures


def _create_ledger(bind) -> None:
    if sa.inspect(bind).has_table(_LEDGER):
        return
    op.create_table(
        _LEDGER,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("table_name", sa.String(length=64), nullable=False),
        sa.Column("row_pk", sa.String(length=128), nullable=False),
        sa.Column("prior_owner_wallet", sa.String(length=42), nullable=False),
        sa.Column("adopted_by_user_id", sa.String(length=64), nullable=False),
        sa.Column("adopted_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("table_name", "row_pk", name="uq_legacy_row_adoptions_table_row"),
    )
    op.create_index("ix_legacy_row_adoptions_prior_wallet", _LEDGER, ["prior_owner_wallet"])


def _ensure_platform_account(bind) -> None:
    """Create the platform ``auth_users`` row if it is not already there."""
    from datetime import UTC, datetime

    user_id, email, name = _platform_identity()
    existing = bind.execute(sa.text("SELECT id FROM auth_users WHERE id = :id"), {"id": user_id}).first()
    if existing is not None:
        return

    auth_users = sa.table(
        "auth_users",
        sa.column("id", sa.String),
        sa.column("name", sa.String),
        sa.column("email", sa.String),
        sa.column("emailVerified", sa.Boolean),
        sa.column("createdAt", sa.DateTime(timezone=True)),
        sa.column("updatedAt", sa.DateTime(timezone=True)),
    )
    now = datetime.now(UTC)
    op.execute(
        auth_users.insert().values(
            id=user_id,
            name=name,
            email=email,
            emailVerified=False,
            createdAt=now,
            updatedAt=now,
        )
    )


def upgrade() -> None:
    bind = op.get_bind()
    platform_user_id, platform_email, _ = _platform_identity()

    candidates = _candidates(bind)
    adopted = _already_adopted(bind, platform_user_id)
    n_candidates = sum(len(rows) for rows in candidates.values())
    population = n_candidates + len(adopted)

    per_table = ", ".join(f"{table}={len(rows)}" for table, rows in candidates.items())
    logger.info(
        "#1283 orphan adoption — candidates: %s (total %d); already adopted: %d; population: %d; band [%d, %d]",
        per_table,
        n_candidates,
        len(adopted),
        population,
        EXPECTED_MIN,
        EXPECTED_MAX,
    )

    if population == 0:
        # A DRY RUN WRITES NOTHING, INCLUDING HERE. This branch is the one
        # place upgrade() writes before reaching the dry-run check below, so
        # the check has to be repeated at the top of it — otherwise
        # ORPHAN_MIGRATION_DRY_RUN=1 on a zero-population database creates the
        # ledger, returns cleanly, and STAMPS alembic_version, marking the
        # revision applied so the real run skips it. That is not a corner
        # case: it is precisely the outcome the owner's rehearsal is meant to
        # catch (if prod's orphans are mostly wallet-less, the count comes in
        # under the band), and it would report success while quietly applying
        # the revision.
        if _dry_run_requested():
            logger.info(
                "#1283 DRY RUN — population 0: no orphaned legacy rows on this database. The real "
                "run would create %s and write nothing else. Aborting so NOTHING is written, "
                "alembic_version included; a non-zero exit here is expected.",
                _LEDGER,
            )
            raise OrphanMigrationDryRun(f"{DRY_RUN_ENV} is set: plan logged, transaction aborted, nothing written.")

        # The ledger is SCHEMA, not data: a database with zero orphans (every
        # CI run, every fresh clone, ``alembic upgrade head`` from empty) must
        # still get the table, or an alembic-built database and a
        # ``create_all()``-built one diverge and the release path in
        # ``claim_legacy_wallet_data`` hits a missing table.
        _create_ledger(bind)
        logger.info("#1283 orphan adoption — no orphaned legacy rows on this database; nothing to do.")
        return

    if not EXPECTED_MIN <= population <= EXPECTED_MAX:
        raise RuntimeError(
            f"#1283 orphan adoption REFUSED: found {population} orphaned legacy row(s) "
            f"({per_table}, already adopted {len(adopted)}), outside the expected band "
            f"[{EXPECTED_MIN}, {EXPECTED_MAX}]. The owner's 2026-09-02 decision was taken against a "
            "measured population of 152 rows; a materially different number means this is not that "
            "population. Re-measure, re-take the decision, and change EXPECTED_MIN/EXPECTED_MAX in "
            "this revision deliberately — do not widen the band to make a deploy pass."
        )

    failures = _structural_failures(bind, candidates)
    if failures:
        raise RuntimeError(
            "#1283 orphan adoption REFUSED — structural pre-flight failed:\n  - " + "\n  - ".join(failures)
        )

    if _dry_run_requested():
        for table, rows in candidates.items():
            sample = [row_pk for row_pk, _ in rows[:5]]
            logger.info("#1283 DRY RUN — would adopt %d row(s) on %s; first ids: %s", len(rows), table, sample)
        logger.info(
            "#1283 DRY RUN — would stamp owner_user_id=%r (auth_users row created if absent, email %s), "
            "write %d ledger row(s) to %s, and change nothing else. Aborting the transaction now so "
            "NOTHING is written, including the alembic_version stamp; a non-zero exit here is expected.",
            platform_user_id,
            platform_email,
            n_candidates,
            _LEDGER,
        )
        raise OrphanMigrationDryRun(f"{DRY_RUN_ENV} is set: plan logged, transaction aborted, nothing written.")

    _create_ledger(bind)
    if n_candidates == 0:
        logger.info("#1283 orphan adoption — every orphan is already adopted; second run is a no-op.")
        return

    _ensure_platform_account(bind)

    from datetime import UTC, datetime

    now = datetime.now(UTC)
    ledger = sa.table(
        _LEDGER,
        sa.column("table_name", sa.String),
        sa.column("row_pk", sa.String),
        sa.column("prior_owner_wallet", sa.String),
        sa.column("adopted_by_user_id", sa.String),
        sa.column("adopted_at", sa.DateTime(timezone=True)),
    )

    pk_by_table = dict(_adoptable_tables())
    adopted_now = 0
    for table, rows in candidates.items():
        pk = pk_by_table[table]
        for row_pk, wallet in rows:
            # `AND owner_user_id IS NULL` keeps this write re-entrant: a row
            # claimed by its real owner between the pre-flight and here is
            # left to them, and is then absent from the ledger too.
            result = bind.execute(
                sa.text(
                    f"UPDATE {table} SET owner_user_id = :platform "  # identifiers are constants, never user input: identifiers from ADOPTABLE_TABLES
                    f"WHERE {pk} = :pk AND owner_user_id IS NULL"
                ),
                {"platform": platform_user_id, "pk": row_pk},
            )
            if not result.rowcount:
                logger.info("#1283 orphan adoption — %s:%s was claimed since the pre-flight; skipped.", table, row_pk)
                continue
            adopted_now += 1
            op.execute(
                ledger.insert().values(
                    table_name=table,
                    row_pk=row_pk,
                    prior_owner_wallet=wallet.lower(),
                    adopted_by_user_id=platform_user_id,
                    adopted_at=now,
                )
            )

    # `adopted_now`, not `n_candidates`: a row claimed by its real owner
    # between the pre-flight and the write is skipped above, and a log line
    # that reported the candidate count would be quietly wrong about what
    # this migration actually did.
    logger.info(
        "#1283 orphan adoption — adopted %d of %d candidate row(s) under %s.",
        adopted_now,
        n_candidates,
        platform_user_id,
    )


def downgrade() -> None:
    """Re-orphan ownership. Nothing was deleted, so nothing is unrecoverable."""
    bind = op.get_bind()
    platform_user_id, _, _ = _platform_identity()

    if sa.inspect(bind).has_table(_LEDGER):
        pk_by_table = dict(_adoptable_tables())
        rows = (
            bind.execute(
                sa.text("SELECT table_name, row_pk FROM legacy_row_adoptions WHERE adopted_by_user_id = :platform"),
                {"platform": platform_user_id},
            )
            .mappings()
            .all()
        )
        for row in rows:
            pk = pk_by_table.get(row["table_name"])
            if pk is None:
                continue
            # Only where the platform still owns it: a row released to its
            # real owner by `claim_legacy_wallet_data` is theirs now, and a
            # downgrade must not take it back.
            bind.execute(
                sa.text(
                    f"UPDATE {row['table_name']} SET owner_user_id = NULL "  # identifiers are constants, never user input: identifiers from ADOPTABLE_TABLES
                    f"WHERE {pk} = :pk AND owner_user_id = :platform"
                ),
                {"pk": row["row_pk"], "platform": platform_user_id},
            )
        op.drop_index("ix_legacy_row_adoptions_prior_wallet", table_name=_LEDGER)
        op.drop_table(_LEDGER)

    # Only remove the account when NOTHING anywhere still names it. Seven of
    # these eleven FKs are ON DELETE CASCADE (`paper_deployments`,
    # `user_profiles`, `api_keys`, and the four `auth_*`/wallet-link ones),
    # so a blind DELETE
    # here would silently destroy rows rather than un-own them. Nothing in
    # this revision can create such a row — but a downgrade is exactly when
    # you want the check that proves it rather than the comment that asserts
    # it.
    referencing = 0
    details: list[str] = []
    for table, column in _AUTH_USER_REFERENCES:
        if not sa.inspect(bind).has_table(table):
            continue
        count = int(
            bind.execute(
                sa.text(
                    f"SELECT COUNT(*) FROM {table} WHERE {column} = :platform"
                ),  # identifiers are constants, never user input
                {"platform": platform_user_id},
            ).scalar()
            or 0
        )
        if count:
            referencing += count
            details.append(f"{table}.{column}={count}")

    if referencing == 0:
        bind.execute(sa.text("DELETE FROM auth_users WHERE id = :id"), {"id": platform_user_id})
    else:
        logger.warning(
            "#1283 downgrade — leaving the platform account in place: %d row(s) still name it (%s). "
            "Deleting it would cascade rather than un-own them.",
            referencing,
            ", ".join(details),
        )
