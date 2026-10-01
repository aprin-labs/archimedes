"""The adoption ledger for pre-account ("orphaned legacy") rows — #1283.

WHAT AN ORPHANED LEGACY ROW IS. A row on one of the three strategy-side
owner-bearing tables (``strategy_store`` / ``strategy_passports`` /
``strategy_proposals``) that

  * carries no canonical owner (``owner_user_id IS NULL``), and
  * names a wallet (``owner_wallet IS NOT NULL``) that appears in no
    ``linked_wallets`` row.

Both halves matter. The first makes it pre-account. The second makes it
*unreachable*: ``claim_legacy_wallet_data`` only ever runs for a wallet a
Better Auth account has just proven control of, so a wallet nobody has ever
linked can never trigger the self-healing reclaim #1283 shipped. Those rows
would sit at ``owner_user_id IS NULL`` forever, and until they are adopted
``owns_strategy``'s tier-2 fallback hands them to *whoever* turns up
controlling that address.

WHAT IS DELIBERATELY **NOT** AN ORPHAN HERE:

  * ``owner_user_id IS NULL AND owner_wallet IS NULL`` — nothing identifies
    the author, and the owner-scoped read paths already filter on an exact
    non-NULL match, so these rows are returned to nobody. Adopting them would
    attribute an anonymous artifact to the platform and buy nothing. This is
    the same call ``docs/plans/2026-08-30-relations-phase2.md`` § 2.7 already
    made ("stays NULL, permanently"). Curated/example rows fall out here too:
    house content carries no ``owner_wallet`` at all.
  * ``owner_user_id IS NULL`` with a wallet that IS in ``linked_wallets`` —
    the existing reclaim reaches those the next time that wallet is linked,
    re-verified, or used to rename. A migration must not race a mechanism
    that already works and that runs behind a fresh signature.
  * ``vault_metadata`` — ``store_vault_metadata`` 409s when
    ``owner_user_id`` is neither NULL nor the caller's, and its NULL case
    exists specifically so a legitimately transferred on-chain owner can
    still write metadata (``vaults_routes.py:487``). A platform stamp closes
    that door permanently with no un-claim path. Excluded, exactly as
    ``rename_strategy``'s reclaim excludes it.
  * ``user_profiles`` — PII, and ``owner_user_id`` is UNIQUE there, so one
    platform account could structurally adopt at most one profile row.
  * ``paper_deployments`` — not in the reclaim tuple, and its
    ``owner_user_id`` FK is ``ON DELETE CASCADE``: adopting a ledger under an
    account and later deleting that account would destroy the ledger.

WHY A LEDGER TABLE AND NOT A BARE UPDATE. Stamping ``owner_user_id`` is a
one-way door for the wallet holder: ``claim_legacy_wallet_data`` filters on
``owner_user_id IS NULL``, so once the platform account is on the row, a
later wallet link can never claim it. Recording (table, pk, prior wallet)
here makes the adoption

  1. **reversible** — ``downgrade()`` re-orphans exactly the rows it stamped,
     and nothing else;
  2. **idempotent** — a second ``upgrade()`` sees the rows already recorded
     and writes nothing;
  3. **releasable** — ``release_platform_adopted_rows`` (called from
     ``claim_legacy_wallet_data``) hands a row back to the real owner the
     moment they prove control of the wallet it names, with a fresh
     signature, exactly as if the adoption had never happened;
  4. **auditable** — the prior owner wallet is not destroyed by the write.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from archimedes.models.chat import Base

#: The three tables whose orphaned legacy rows are adoptable, mapped to the
#: name of the column holding the row's primary key. This is exactly the trio
#: ``rename_strategy`` hands ``claim_legacy_wallet_data`` (strategy-side only,
#: ``include_profile=False``) — see this module's docstring for why the other
#: owner-bearing tables are excluded. The migration and the release path both
#: read this constant; neither re-lists the tables.
ADOPTABLE_TABLES: tuple[tuple[str, str], ...] = (
    ("strategy_store", "id"),
    ("strategy_passports", "id"),
    ("strategy_proposals", "id"),
)


class LegacyRowAdoption(Base):
    """One row adopted by the platform account, and what it looked like before."""

    __tablename__ = "legacy_row_adoptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: One of ``ADOPTABLE_TABLES``' table names.
    table_name: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The adopted row's primary key, as text (all three are string PKs).
    row_pk: Mapped[str] = mapped_column(String(128), nullable=False)
    #: The row's ``owner_wallet`` at adoption time — never NULL, because a row
    #: with no wallet is not adoptable (see the module docstring). This is what
    #: makes the release path possible.
    prior_owner_wallet: Mapped[str] = mapped_column(String(42), nullable=False)
    #: The account the row was stamped with. Stored rather than assumed so a
    #: future non-legacy adoption cannot be mistaken for this one.
    adopted_by_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    adopted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        # The idempotency key: a row can be adopted at most once.
        UniqueConstraint("table_name", "row_pk", name="uq_legacy_row_adoptions_table_row"),
        # The release path's only query shape: "what did this wallet lose?"
        Index("ix_legacy_row_adoptions_prior_wallet", "prior_owner_wallet"),
    )


def platform_adopted_row_count(session, address: str) -> int:
    """How many rows the platform holds for *address*, releasable on link.

    The DISCOVERY half of the release. ``release_platform_adopted_rows`` below
    is the door; this is the sign on it. Adoption stamps ``owner_user_id``, so
    ``_wallet_has_unclaimed_legacy_data`` — which mirrors the claim loop's
    ``owner_user_id IS NULL`` filter one-for-one — stops seeing an adopted row
    the moment the migration runs, and ``GET /api/wallet/check`` starts
    answering ``has_legacy_data: false`` for exactly the ~10 pre-account
    wallets whose rows are sitting in the ledger. The relink banner
    (``ui/src/AuthenticatedApp.jsx``) is gated on that boolean and nothing
    else, so without this the real owner of an adopted row is never told the
    row exists: a release path with a key and no sign, and adoption becomes a
    silent one-way door in practice even though it is reversible in code.

    Guarded on the ledger table's existence for the same reason
    ``release_platform_adopted_rows`` is, and via the same
    ``session.connection()`` inspector: this runs on the ``/check`` path of
    every account that has an unlinked browser wallet, including on a database
    that has not reached the adoption revision, where raising would turn a
    "you have nothing to claim" answer into a 500.
    """
    import sqlalchemy as sa

    from archimedes.models.account import PLATFORM_LEGACY_USER_ID

    if not address:
        return 0
    if not sa.inspect(session.connection()).has_table(LegacyRowAdoption.__tablename__):
        return 0
    return (
        session.query(LegacyRowAdoption)
        .filter(
            LegacyRowAdoption.prior_owner_wallet == address.strip().lower(),
            LegacyRowAdoption.adopted_by_user_id == PLATFORM_LEGACY_USER_ID,
        )
        .count()
    )


def release_platform_adopted_rows(session, user_id: str, address: str, *, table_names: tuple[str, ...]) -> int:
    """Hand rows the platform adopted for *address* back to *user_id*.

    The mirror of the adoption, and the reason adopting is not theft: the
    moment the real holder of ``address`` proves control of it, every row the
    platform took because that wallet had never been linked becomes theirs.
    Authorization is identical to the claim this runs inside —
    ``claim_legacy_wallet_data`` is only ever reached behind a verified wallet
    link, a re-verification, or a rename by an account whose linked wallet
    matches — so this grants nothing a caller would not already have had
    under the tier-2 wallet fallback the adoption replaced.

    Scoped to *table_names* so a caller that narrowed the claim (``rename_strategy``
    passes the strategy trio) cannot widen it here.

    Returns the number of rows released, and never raises for a missing
    ledger table. That guard is not theoretical politeness: this runs on
    every wallet link, and a database that has not reached the adoption
    revision has no ``legacy_row_adoptions`` to query — an exception here
    would poison the claim's transaction and break wallet linking outright,
    to release rows that by definition do not exist. One catalog lookup on a
    rare path is the right price.
    """
    import sqlalchemy as sa

    from archimedes.models.account import PLATFORM_LEGACY_USER_ID

    if not user_id or not address or not table_names:
        return 0

    # ``session.connection()``, never ``session.get_bind()``: the inspector
    # must run the catalog lookup on the SESSION'S OWN connection, inside the
    # transaction the caller's pending UPDATEs live in. Inspecting the Engine
    # instead checks out a second connection, and the writes
    # ``claim_legacy_wallet_data`` has already flushed are then invisible to
    # it — or worse, disturbed (it fails four wallet-linking tests outright:
    # "Could not refresh instance <LinkedWallet>", and an already-claimed row
    # reading back as still unclaimed).
    if not sa.inspect(session.connection()).has_table(LegacyRowAdoption.__tablename__):
        return 0

    normalized = address.strip().lower()
    pk_by_table = dict(ADOPTABLE_TABLES)

    released = 0
    records = (
        session.query(LegacyRowAdoption)
        .filter(
            LegacyRowAdoption.prior_owner_wallet == normalized,
            LegacyRowAdoption.adopted_by_user_id == PLATFORM_LEGACY_USER_ID,
            LegacyRowAdoption.table_name.in_(table_names),
        )
        .all()
    )
    for record in records:
        pk_column = pk_by_table.get(record.table_name)
        if pk_column is None:
            # A ledger row naming a table this build does not adopt: leave it
            # alone rather than guess a primary key. Fail-soft, and visible —
            # the row stays in the ledger for the next reader.
            continue
        result = session.execute(
            sa.text(
                f"UPDATE {record.table_name} SET owner_user_id = :new_owner "  # identifiers are constants, never user input: table name from ADOPTABLE_TABLES
                f"WHERE {pk_column} = :pk AND owner_user_id = :platform"
            ),
            {"new_owner": user_id, "pk": record.row_pk, "platform": PLATFORM_LEGACY_USER_ID},
        )
        released += int(result.rowcount or 0)
        session.delete(record)

    return released
