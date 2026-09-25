"""Account-deletion cascade-vs-anonymize policy (issue #1367, D3).

Verifies the per-table decision recorded in migration ``85ca5310b7a1`` is
what actually happens when the owning ``auth_users`` row is deleted:

  * CASCADE  — ``user_profiles`` (the encrypted-email row), ``paper_deployments``
  * SET NULL — ``strategy_store``, ``strategy_passports``, ``strategy_proposals``,
    ``vault_metadata``

**Against the migrated schema, not the ORM's.** The first version of this
file built its throwaway database with ``Base.metadata.create_all()``, which
made it a guard over ``models/user_profile.py``'s ``ondelete=`` kwarg rather
than over the migration. On Postgres — the only place this policy actually
runs — the migration alone shapes the schema, so that guard stayed green
even with the migration's CASCADEs gutted, and it could not see the
``BEFORE DELETE`` trigger at all (``create_all()`` does not carry triggers;
see the migration's own "Path note"). Every test here therefore runs a real
``alembic upgrade head`` into a throwaway SQLite file and exercises the
schema that produces.

``alembic upgrade head`` is run ONCE per module into a pristine template
file (a subprocess, whitelist-only env, mirroring
``test_alembic_migrations.py``'s ``_clean_subprocess_env`` — an inherited
``DATABASE_URL=postgresql://...@postgres:...`` from a developer's ``.env``
would send it at a docker-compose-only hostname); each test then copies that
file so it gets a fresh database without paying for the migration replay
again.

SQLite does not enforce FK ``ON DELETE`` actions unless ``PRAGMA
foreign_keys=ON`` is issued per-connection — see ``test_identity_schema.py``'s
module docstring, which is why every other FK test in this suite checks
presence via introspection rather than enforcement. This file's whole point
is to prove the actions FIRE, so it turns the pragma on for its own engine.
Anti-goal (issue #1367): stay on sqlite, no Postgres/Redis — this satisfies
that by enabling real enforcement on sqlite rather than widening to an
integration test.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from archimedes.models.account import AuthUser, LinkedWallet
from archimedes.models.chat import VaultMetadata
from archimedes.models.identity import WalletIdentity
from archimedes.models.paper_store import PaperDailyReturn, PaperDeployment
from archimedes.models.strategy_passport_record import StrategyPassportRecord
from archimedes.models.strategy_proposal import StrategyProposal
from archimedes.models.strategy_store import StrategyRecord, upsert_strategy
from archimedes.models.user_profile import UserProfile
from archimedes.services.email_crypto import encrypt_email
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

_BACKEND_DIR = Path(__file__).resolve().parent.parent

# The six columns the issue's evidence names — SET NULL first (content
# survives, ownership detaches), CASCADE last (the whole row goes).
_SET_NULL_TABLES = ("strategy_store", "strategy_passports", "strategy_proposals", "vault_metadata")
_CASCADE_TABLES = ("user_profiles", "paper_deployments")
_ALL_OWNED_TABLES = _SET_NULL_TABLES + _CASCADE_TABLES

_USER_ID = "user-cascade-del"
_WALLET = "0x" + "b" * 40  # the account's FIRST linked wallet — profile is claimed
_SECOND_WALLET = "0x" + "d" * 40  # linked LATER; its profile predates the link, stays unclaimed
_STRANGER_WALLET = "0x" + "e" * 40  # never linked to this account — must survive untouched
_VAULT_ADDR = "0x" + "c" * 40
_STRATEGY_ID = "strategy-cascade-1"
_PLAINTEXT_EMAIL = "private-profile-email@example.com"
_SECOND_EMAIL = "second-wallet-email@example.com"
_STRANGER_EMAIL = "someone-elses-email@example.com"


def _clean_subprocess_env(database_url: str) -> dict[str, str]:
    """Whitelist-only env for the alembic subprocess — see module docstring."""
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "DATABASE_URL": database_url,
    }


@pytest.fixture(scope="module")
def migrated_template(tmp_path_factory) -> Path:
    """One real ``alembic upgrade head`` per module, into a pristine file."""
    db_path = tmp_path_factory.mktemp("migrated") / "head.db"
    result = subprocess.run(
        ["alembic", "upgrade", "head"],
        cwd=str(_BACKEND_DIR),
        env=_clean_subprocess_env(f"sqlite:///{db_path}"),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"alembic upgrade head failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    assert db_path.exists()
    return db_path


@pytest.fixture()
def engine(migrated_template, tmp_path):
    """A fresh copy of the migrated schema, with ON DELETE actions enforced."""
    db_path = tmp_path / "deletion.db"
    shutil.copyfile(migrated_template, db_path)
    engine = create_engine(f"sqlite:///{db_path}")

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    # NO Base.metadata.create_all() — the schema under test is the migrated
    # one. create_all() here would paper over exactly the divergence this
    # file exists to catch (and would still miss the trigger entirely).
    try:
        yield engine
    finally:
        engine.dispose()


def _add_account(session: Session, user_id: str = _USER_ID) -> None:
    now = datetime.now(UTC)
    session.add(
        AuthUser(
            id=user_id,
            name="Deletable User",
            email=f"{user_id}@example.com",
            email_verified=True,
            created_at=now,
            updated_at=now,
        )
    )


def _link_wallet(session: Session, user_id: str, address: str, *, primary: bool = False) -> None:
    """Register the wallet on the identity anchor and link it to the account.

    The link is what the migration's trigger keys off: it is the account's
    own proof of signature control over that address (``wallet_routes``'
    challenge flow), and therefore the only defensible basis for treating an
    unclaimed row on that address as the deleting account's PII.
    """
    if session.get(WalletIdentity, address) is None:
        session.add(WalletIdentity(wallet_address=address, actor_class="human"))
        session.flush()
    session.add(
        LinkedWallet(
            user_id=user_id,
            normalized_identity=f"5042002:{address}",
            address=address,
            display_address=address,
            chain_id=5042002,
            provider="test",
            is_primary=primary,
        )
    )


def _delete_the_account(session: Session, user_id: str = _USER_ID) -> None:
    """Mirror the real trigger: a bare ``DELETE FROM auth_users``.

    The migration's own rationale is that the database's FK actions must do
    this work regardless of which of the two services (Node Better Auth, or
    the Python backend) issues the delete, so the test issues the same bare
    statement rather than going through any application-level cleanup helper
    that only one of them would run.
    """
    session.execute(text("DELETE FROM auth_users WHERE id = :id"), {"id": user_id})
    session.commit()


def _seed_strategy_anchor(session: Session) -> None:
    """A ``strategy_store`` row at ``_STRATEGY_ID``, unowned.

    Exists purely to satisfy ``paper_deployments.strategy_id ->
    strategy_store.id``, the FK `fb8d0bae8112` (#1438) added. Before that FK
    a deployment could name a strategy id that was not in the table; with
    ``PRAGMA foreign_keys=ON`` — which this file turns on precisely so ON
    DELETE actions really fire — it cannot. Unowned (``owner_user_id`` is
    NULL) so it takes no part in the deletion policy under test.
    """
    session.add(
        StrategyRecord(
            id=_STRATEGY_ID,
            content_hash="0x" + "d" * 64,
            generation_method="fusion",
            strategy_name="Deployment FK anchor",
        )
    )
    session.flush()


def _seed_one_row_in_each_owned_table(session: Session) -> str:
    """One AuthUser plus one row it owns in each of the six tables.

    Returns the ``strategy_store.id`` the seeded paper deployment points at —
    the owned strategy's own id, so the fixture holds exactly ONE
    ``strategy_store`` row and the FK `fb8d0bae8112` added is satisfied by
    real data rather than by an extra anchor row.
    """
    _add_account(session)
    _link_wallet(session, _USER_ID, _WALLET, primary=True)
    session.flush()  # wallet_identities row must exist before FK-dependent inserts below

    strategy = upsert_strategy(
        session,
        generation_method="fusion",
        strategy_name="Cascade test strategy",
        thesis="thesis",
        source_papers=[],
        asset_universe=["SPY"],
        owner_user_id=_USER_ID,
    )
    session.flush()
    session.add(StrategyPassportRecord(id="passport-cascade-1", owner_user_id=_USER_ID))
    session.add(
        StrategyProposal(
            id="proposal-cascade-1",
            generation_id="gen-cascade-1",
            proposal_id="proposal-cascade-1",
            content_hash="0x" + "a" * 64,
            owner_user_id=_USER_ID,
        )
    )
    session.add(
        UserProfile(
            wallet_address=_WALLET,
            owner_user_id=_USER_ID,
            email=encrypt_email(_PLAINTEXT_EMAIL),
        )
    )
    session.add(VaultMetadata(vault_address=_VAULT_ADDR, creator_address=_WALLET, owner_user_id=_USER_ID))
    session.add(
        PaperDeployment(
            id="deployment-claimed-1",
            strategy_id=strategy.id,
            owner_wallet=_WALLET,
            owner_user_id=_USER_ID,
            spec_json="{}",
            deployed_at=date.today(),
        )
    )
    session.flush()
    session.add(PaperDailyReturn(deployment_id="deployment-claimed-1", date=date.today(), daily_return=0.01))
    session.commit()
    return strategy.id


def _owner_user_id_fks(conn, table: str) -> list[dict[str, str]]:
    """Every FK on ``table.owner_user_id`` that points at ``auth_users``, as a
    LIST — never a dict keyed by column.

    ``PRAGMA foreign_key_list`` yields one row per foreign key:
    ``(id, seq, referenced_table, from_col, to_col, on_update, on_delete,
    match)``. Two constraints on the SAME column are two rows with different
    ``id``s, and folding them into a ``{from_col: on_delete}`` dict silently
    keeps only the last — which is exactly the failure this file has to be
    able to see, since `85ca5310b7a1` ALTERS a constraint `fb8d0bae8112`
    created rather than creating its own.
    """
    return [
        {"id": row[0], "to": row[4], "on_delete": row[6]}
        for row in conn.exec_driver_sql(f"PRAGMA foreign_key_list({table})")
        if row[3] == "owner_user_id" and row[2] == "auth_users"
    ]


def test_migrated_schema_is_what_is_under_test(engine):
    """The premise the rest of this file rests on: these tests run against the
    Alembic-built schema, so a gutted migration cannot hide behind a correct
    ORM model. Asserted directly rather than assumed — ``create_all()`` and
    ``upgrade head`` are two different schema-management paths (see
    ``migrations/env.py``), and only one of them is the one that ships.

    Covers **all six** ``owner_user_id`` columns, not just the two CASCADE
    ones: a wrong ``ondelete=`` in the MIGRATION for any of the four SET NULL
    tables is caught here by introspection, in addition to being caught
    behaviourally by ``test_account_deletion_cascades_or_nulls_every_owned_table``.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    with engine.connect() as conn:
        stamped = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        # The premise is "this ran `upgrade head`", NOT "85ca5310b7a1 is
        # forever the newest revision". Pinning the literal made every later
        # migration break this file for a reason unrelated to what it guards
        # (#1575's paper_decision_traces was the first). Resolve head from the
        # script directory instead, and assert separately that the cascade
        # policy revision this file is ABOUT is in the applied chain.
        script = ScriptDirectory.from_config(Config(str(_BACKEND_DIR / "alembic.ini")))
        heads = script.get_heads()
        assert len(heads) == 1, f"the migration chain must stay serial; found heads {heads}"
        assert stamped == heads[0], f"expected head {heads[0]!r} to be stamped, got {stamped!r}"
        applied = {rev.revision for rev in script.iterate_revisions(stamped, "base")}
        assert "85ca5310b7a1" in applied, "the cascade-policy migration this file guards was not applied"

        for table in _ALL_OWNED_TABLES:
            expected = "CASCADE" if table in _CASCADE_TABLES else "SET NULL"
            owner_fks = _owner_user_id_fks(conn, table)

            # Exactly ONE, deliberately. `fb8d0bae8112` (#1438) creates
            # `fk_paper_deployments_owner_user_id` with SET NULL and this
            # revision ALTERS it; if it ever went back to *creating* the
            # constraint instead, the SQLite batch-rebuild path would leave
            # TWO foreign keys on one column with contradictory ON DELETE
            # actions, and SQLite would apply whichever it reached first.
            # A dict keyed by from-column cannot see that — it collapses the
            # duplicate — so count the rows before reading the action.
            assert len(owner_fks) == 1, (
                f"migrated {table}.owner_user_id should carry exactly one FK to auth_users, "
                f"found {len(owner_fks)}: {owner_fks}"
            )
            assert owner_fks[0]["on_delete"] == expected, (
                f"migrated {table}.owner_user_id is ON DELETE {owner_fks[0]['on_delete']}, expected {expected}"
            )

        # The index belongs to #1438 in both directions — this revision must
        # neither create a second one nor drop it on downgrade.
        paper_indices = [
            row[1]
            for row in conn.exec_driver_sql("PRAGMA index_list(paper_deployments)")
            if row[1] == "ix_paper_deployments_owner_user_id"
        ]
        assert paper_indices == ["ix_paper_deployments_owner_user_id"], (
            f"expected exactly one ix_paper_deployments_owner_user_id in the migrated schema, got {paper_indices}"
        )

        triggers = {row[0] for row in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
        assert "trg_auth_users_purge_unclaimed_owned_rows" in triggers, (
            f"the migration's second-reach trigger is missing from the migrated schema: {sorted(triggers)}"
        )


def test_migration_alters_the_paper_deployments_fk_rather_than_creating_a_second_one():
    """The Postgres DDL this revision emits, read directly.

    ``fb8d0bae8112`` (#1438) CREATES ``fk_paper_deployments_owner_user_id``
    (SET NULL) and ``ix_paper_deployments_owner_user_id``. This revision must
    only change that constraint's ON DELETE action — if it ever went back to
    *creating* the constraint, Postgres would abort the deploy with
    ``constraint "fk_paper_deployments_owner_user_id" ... already exists``,
    and re-creating the index would fail the same way.

    **Why this test renders Postgres SQL instead of introspecting the sqlite
    database the rest of this file uses:** it can't be caught on sqlite.
    ``batch_alter_table`` on sqlite is a reflect-rename-copy-drop table
    rebuild, so a ``create_foreign_key`` for a name that already exists is
    silently absorbed into the rebuild and the resulting table still carries
    exactly one, correct-looking FK — verified by mutating the migration that
    way and watching all four sqlite tests stay green. Postgres is the only
    place this policy runs and the only place the collision is fatal, so the
    guard has to read Postgres DDL. ``alembic upgrade --sql`` renders it with
    no live connection: the URL only selects the dialect.
    """
    result = subprocess.run(
        ["alembic", "upgrade", "9c2e7b5a1f4d:85ca5310b7a1", "--sql"],
        cwd=str(_BACKEND_DIR),
        env=_clean_subprocess_env("postgresql://user:pass@localhost:5432/dbname"),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"offline render failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    sql = result.stdout

    drop_at = sql.find("ALTER TABLE paper_deployments DROP CONSTRAINT fk_paper_deployments_owner_user_id")
    assert drop_at != -1, (
        "this revision does not DROP fk_paper_deployments_owner_user_id before re-adding it; "
        "on Postgres that is a duplicate-constraint abort, because #1438 already created it.\n"
        f"rendered SQL:\n{sql}"
    )
    add_at = sql.find(
        "ALTER TABLE paper_deployments ADD CONSTRAINT fk_paper_deployments_owner_user_id "
        "FOREIGN KEY(owner_user_id) REFERENCES auth_users (id) ON DELETE CASCADE"
    )
    assert add_at != -1, f"the CASCADE re-add is missing from the rendered SQL:\n{sql}"
    assert drop_at < add_at, "the FK is re-added before it is dropped"

    # The index is #1438's in both directions — neither created nor dropped here.
    assert "ix_paper_deployments_owner_user_id" not in sql, (
        "this revision touches ix_paper_deployments_owner_user_id; #1438's fb8d0bae8112 already "
        f"creates it, so CREATE INDEX here aborts the deploy:\n{sql}"
    )

    # user_profiles is the same shape — its FK comes from b7e3f1a2c9d4.
    assert "ALTER TABLE user_profiles DROP CONSTRAINT fk_user_profiles_owner_user_id" in sql
    assert (
        "ALTER TABLE user_profiles ADD CONSTRAINT fk_user_profiles_owner_user_id "
        "FOREIGN KEY(owner_user_id) REFERENCES auth_users (id) ON DELETE CASCADE" in sql
    )


def test_account_deletion_cascades_or_nulls_every_owned_table(engine):
    """Both policy branches, on the schema the migration builds."""
    with Session(engine) as session:
        seeded_strategy_id = _seed_one_row_in_each_owned_table(session)
        assert session.get(AuthUser, _USER_ID) is not None, "seed did not create the account"

        _delete_the_account(session)

    # Fresh session/connection for assertions: no stale identity-map entry
    # from before the delete can mask a bug that only shows up on reload.
    with Session(engine) as session:
        # (a) Zero rows anywhere still carry the deleted user's id — true for
        # SET NULL tables by detachment, and trivially true for CASCADE
        # tables because the row itself is gone (checked explicitly below).
        for table in _ALL_OWNED_TABLES:
            # table is drawn from the fixed local _ALL_OWNED_TABLES tuple, never user input.
            remaining = session.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE owner_user_id = :uid"),
                {"uid": _USER_ID},
            ).scalar_one()
            assert remaining == 0, f"{table} still has a row with owner_user_id = {_USER_ID!r}"

        # SET NULL branch: the content survives — detached, not destroyed.
        assert (
            session.get(StrategyRecord, session.execute(text("SELECT id FROM strategy_store")).scalar_one()) is not None
        )
        assert session.get(StrategyPassportRecord, "passport-cascade-1") is not None
        assert session.get(StrategyProposal, "proposal-cascade-1") is not None
        vault_row = session.execute(
            text("SELECT owner_user_id FROM vault_metadata WHERE vault_address = :v"), {"v": _VAULT_ADDR}
        ).one_or_none()
        assert vault_row is not None, "vault_metadata row was deleted; expected SET NULL, not CASCADE"
        assert vault_row[0] is None

        # CASCADE branch: the row itself is gone — including the
        # Fernet-encrypted email, not merely detached from the account.
        assert session.get(UserProfile, _WALLET) is None, (
            "user_profiles row survived account deletion; the encrypted email is still in the DB"
        )
        remaining_email_rows = session.execute(text("SELECT COUNT(*) FROM user_profiles")).scalar_one()
        assert remaining_email_rows == 0, "an encrypted email row remains in user_profiles after account deletion"

        remaining_deployments = session.execute(
            text("SELECT COUNT(*) FROM paper_deployments WHERE strategy_id = :sid"), {"sid": seeded_strategy_id}
        ).scalar_one()
        assert remaining_deployments == 0, "paper_deployments row survived account deletion"

        # The ledger under the deployment goes with it (paper_daily_returns
        # already cascades off paper_deployments.id) — the migration's
        # "removes the whole ledger cleanly in one direction" claim, checked
        # rather than asserted in prose.
        remaining_ledger = session.execute(text("SELECT COUNT(*) FROM paper_daily_returns")).scalar_one()
        assert remaining_ledger == 0, "paper_daily_returns rows survived their deployment's deletion"


def test_account_deletion_reaches_unclaimed_rows_on_a_second_linked_wallet(engine):
    """The gap a single ``ON DELETE CASCADE`` cannot close.

    ``user_profiles.owner_user_id`` is UNIQUE and the claim path stamps at
    most one profile per account, so an account that links a SECOND wallet
    whose profile row predates the link keeps that row at
    ``owner_user_id IS NULL`` — outside the FK's reach. Without the
    migration's trigger, deleting the account leaves that row, and its
    Fernet-encrypted email, in the database forever. Same shape for a
    pre-link ``paper_deployments`` row, which the claim path never touches
    at all.
    """
    with Session(engine) as session:
        _add_account(session)
        _link_wallet(session, _USER_ID, _WALLET, primary=True)
        _link_wallet(session, _USER_ID, _SECOND_WALLET)
        session.flush()
        _seed_strategy_anchor(session)

        # The canonical, claimed profile — reached by the FK.
        session.add(
            UserProfile(
                wallet_address=_WALLET,
                owner_user_id=_USER_ID,
                email=encrypt_email(_PLAINTEXT_EMAIL),
            )
        )
        # The second wallet's legacy profile: created before the link, never
        # claimed (the account already has a canonical profile — see
        # ``upsert_profile``'s 409). Only the trigger can reach this one.
        session.add(
            UserProfile(
                wallet_address=_SECOND_WALLET,
                owner_user_id=None,
                email=encrypt_email(_SECOND_EMAIL),
            )
        )
        # A pre-link paper deployment on the second wallet: claim_legacy_wallet_data
        # does not list PaperDeployment at all, so this is never claimed either.
        session.add(
            PaperDeployment(
                id="deployment-unclaimed-1",
                strategy_id=_STRATEGY_ID,
                owner_wallet=_SECOND_WALLET,
                owner_user_id=None,
                spec_json="{}",
                deployed_at=date.today(),
            )
        )
        session.commit()

        _delete_the_account(session)

    with Session(engine) as session:
        assert session.get(UserProfile, _SECOND_WALLET) is None, (
            "the second linked wallet's unclaimed profile survived account deletion; "
            "its Fernet-encrypted email is still in the DB"
        )
        assert session.get(UserProfile, _WALLET) is None, "the canonical profile survived account deletion"
        assert session.execute(text("SELECT COUNT(*) FROM user_profiles")).scalar_one() == 0

        assert session.get(PaperDeployment, "deployment-unclaimed-1") is None, (
            "the second linked wallet's unclaimed paper deployment survived account deletion"
        )


def test_deletion_does_not_reach_an_unclaimed_row_on_a_wallet_the_account_never_linked(engine):
    """The trigger is scoped to the account's OWN proven wallets.

    The reason the trigger keys off ``linked_wallets`` and not simply
    "``owner_user_id IS NULL``" is that unclaimed rows are the normal state
    for every wallet-only visitor who never signed up. A purge that ignored
    the link would destroy strangers' data on one unrelated account deletion
    — a far worse bug than the one being fixed. This is the input that must
    NOT be swept up, asserted so the scoping cannot silently regress.
    """
    with Session(engine) as session:
        _add_account(session)
        _link_wallet(session, _USER_ID, _WALLET, primary=True)
        # A wallet-only visitor: known to the identity anchor, has a profile
        # and a paper deployment, never linked to any Better Auth account.
        session.add(WalletIdentity(wallet_address=_STRANGER_WALLET, actor_class="human"))
        session.flush()
        _seed_strategy_anchor(session)

        session.add(
            UserProfile(
                wallet_address=_STRANGER_WALLET,
                owner_user_id=None,
                email=encrypt_email(_STRANGER_EMAIL),
            )
        )
        session.add(
            PaperDeployment(
                id="deployment-stranger-1",
                strategy_id=_STRATEGY_ID,
                owner_wallet=_STRANGER_WALLET,
                owner_user_id=None,
                spec_json="{}",
                deployed_at=date.today(),
            )
        )
        session.commit()

        _delete_the_account(session)

    with Session(engine) as session:
        assert session.get(UserProfile, _STRANGER_WALLET) is not None, (
            "an unrelated wallet's profile was destroyed by another account's deletion — "
            "the purge is not scoped to the deleting account's linked wallets"
        )
        assert session.get(PaperDeployment, "deployment-stranger-1") is not None, (
            "an unrelated wallet's paper deployment was destroyed by another account's deletion"
        )


def test_payment_and_credit_records_survive_account_deletion(engine):
    """The honest half of the deletion UI's promise (#1367, D4).

    ``payment_receipts.user_id`` (`1752121b8d7c`, #1456) and
    ``generation_credits.user_id`` (`a3f19c7d2e84`, #1441) carry no foreign
    key to ``auth_users`` at all, so the DB does nothing to them when the
    account row goes — a deliberate non-decision this revision's docstring
    records under SCOPE ("a receipt is a financial record ... that is a call
    for the humans who own the money path").

    Account Settings' Delete-account panel therefore lists both under "Not
    removed by this, and you should know it"
    (``ui/src/account-deletion.js``'s ``DELETION_RETAINED``, mirrored against
    the models by ``ui/test/account-deletion.test.js``). That is a claim
    about runtime behaviour, so it gets a runtime check rather than only a
    schema one: if a later revision gives either column a CASCADE, the copy
    silently starts telling users the opposite of the truth, and this test
    is what fails first.
    """
    with Session(engine) as session:
        _add_account(session)
        session.flush()
        # Bound as an ISO string, not a datetime: sqlite3's default datetime
        # adapter is deprecated in 3.12 and raises a DeprecationWarning on a
        # raw-SQL bind, which this file has none of elsewhere.
        now = datetime.now(UTC).isoformat()
        session.execute(
            text(
                "INSERT INTO payment_receipts (user_id, payer_wallet, amount_base_units, price_usd, "
                "network, settlement_ref, job_id, created_at) "
                "VALUES (:uid, :wallet, 2000000, '2.00', 'arc-testnet', 'circle-ref-1', 'job-1', :now)"
            ),
            {"uid": _USER_ID, "wallet": _WALLET, "now": now},
        )
        session.execute(
            text(
                "INSERT INTO generation_credits (user_id, idempotency_key, status, created_at) "
                "VALUES (:uid, 'idem-1', 'available', :now)"
            ),
            {"uid": _USER_ID, "now": now},
        )
        session.commit()

        _delete_the_account(session)

    with Session(engine) as session:
        receipts = session.execute(
            text("SELECT COUNT(*) FROM payment_receipts WHERE user_id = :uid"), {"uid": _USER_ID}
        ).scalar_one()
        credits = session.execute(
            text("SELECT COUNT(*) FROM generation_credits WHERE user_id = :uid"), {"uid": _USER_ID}
        ).scalar_one()

        assert receipts == 1, (
            "payment_receipts no longer survives account deletion — ui/src/account-deletion.js's "
            "DELETION_RETAINED tells users their receipts are kept, and that copy is now false"
        )
        assert credits == 1, (
            "generation_credits no longer survives account deletion — ui/src/account-deletion.js's "
            "DELETION_RETAINED tells users the credit ledger is kept, and that copy is now false"
        )
