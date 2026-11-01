"""Identity-ledger schema tests (issue #1028, "tests" chunk — 5/5).

Covers the three things the epic's Target schema locks in structurally:

  1. **FK retrofits** (AC4: "every wallet column in ``information_schema``
     either FKs to ``wallet_identities`` or is registered in
     ``controlled_wallets`` — zero unclassified wallet columns"). Verified
     via ``sqlalchemy.inspect`` against the live ``create_all()``-built
     engine (the fresh-DB path every test in this suite runs on) rather than
     a fresh alembic-only DB, so this pins the ORM models themselves — the
     thing ``archimedes/db.py``'s ``init_db()`` actually builds for local dev
     + every hermetic test in this repo. ``test_alembic_migrations.py``
     cross-checks the alembic-built path separately.
  2. **Lowercase ``CHECK`` constraints** (D1/D1a + the vault_metadata /
     marketplace_agents.gateway_seller_address casing fixes) are DB-level
     enforced, not just application-level convention. SQLite enforces CHECK
     constraints unconditionally (unlike FKs, which need ``PRAGMA
     foreign_keys=ON`` and are NOT turned on anywhere in this codebase —
     verified empirically; see the FK-enforcement section below for why the
     FK tests above check *presence* via introspection rather than
     *enforcement*).
  3. **FK enforcement against real Postgres** — the ``@pytest.mark.integration``
     section at the bottom of this file. SQLite's non-enforcement (see 2.)
     means the FK-presence tests above cannot prove the retrofit actually
     protects data integrity; this section closes that gap with a real
     orphan-reference insert against Postgres, skipped cleanly when no
     Postgres DSN is configured.

Hermetic (everything above the FK-enforcement section): uses the same shared
SQLite engine every other DB-backed test in this suite uses
(``archimedes.db.engine``); no network, no Postgres. The FK-enforcement
section is the one deliberate exception — it is marked ``integration`` and
excluded from the default unit run precisely because it needs real Postgres.
"""

from __future__ import annotations

import os
import uuid

import pytest
from archimedes.db import Base, _pin_psycopg2_driver, get_session, init_db
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker


@pytest.fixture(autouse=True, scope="module")
def _ensure_tables():
    """See test_identity_metrics.py's identical fixture for the rationale:
    this file must be runnable standalone under the hermetic per-module gate.
    (The FK-introspection helper below builds its own engine, so it does not
    depend on this — this only seeds the shared engine for the CHECK-constraint
    insert tests.)"""
    init_db()


def _unique_wallet() -> str:
    return "0x" + uuid.uuid4().hex.ljust(40, "0")[:40]


def _fk_targets(table: str) -> dict[str, str]:
    """``{constrained_column: referred_table}`` for every FK on ``table``.

    Reflects a throwaway in-memory engine built purely from ``Base.metadata``
    rather than the process-wide file-backed ``engine``. This test pins the ORM
    MODELS' FK definitions (see the module docstring), so building a fresh schema
    and reflecting it is both faithful to that intent AND immune to the shared
    session-wide SQLite file being mid-create/drop when other modules
    (test_marketplace_routes, test_settlement_idempotency) run before this one in
    the full suite — the cause of the intermittent NoSuchTableError otherwise.
    """
    probe = create_engine("sqlite://")  # in-memory, single shared connection
    try:
        Base.metadata.create_all(probe)
        inspector = inspect(probe)
        out: dict[str, str] = {}
        for fk in inspector.get_foreign_keys(table):
            referred = fk["referred_table"]
            for col in fk["constrained_columns"]:
                out[col] = referred
        return out
    finally:
        probe.dispose()


# ─── FK retrofits (AC4) ────────────────────────────────────────────────────
# One assertion per column named in the issue's "FK retrofits" section.

_EXPECTED_WALLET_FKS: list[tuple[str, str]] = [
    ("user_profiles", "wallet_address"),
    ("strategy_store", "owner_wallet"),
    ("strategy_passports", "owner_wallet"),
    ("strategy_passports", "curator_wallet"),
    ("vault_metadata", "creator_address"),
    ("chat_messages", "wallet_address"),
    ("marketplace_agents", "creator_wallet"),
    ("marketplace_agents", "subscriber_wallet"),
    ("strategy_generators", "wallet_address"),
    ("controlled_wallets", "controller_wallet"),
    ("identity_events", "wallet"),
]


@pytest.mark.parametrize("table,column", _EXPECTED_WALLET_FKS)
def test_identity_column_fks_to_wallet_identities(table, column):
    fks = _fk_targets(table)
    assert fks.get(column) == "wallet_identities", (
        f"{table}.{column} must FK to wallet_identities — got {fks.get(column)!r} (all FKs: {fks})"
    )


@pytest.mark.parametrize(
    "table,column",
    [
        ("marketplace_agents", "ephemeral_wallet"),
        ("marketplace_agents", "gateway_seller_address"),
    ],
)
def test_controlled_columns_have_no_anchor_fk(table, column):
    """D1a: system-operated addresses register in ``controlled_wallets`` (a
    separate, populated-at-runtime table) rather than FK'ing straight to the
    identity anchor — the anchor holds SIWE-capable wallets only."""
    fks = _fk_targets(table)
    assert column not in fks, f"{table}.{column} unexpectedly FKs to {fks.get(column)!r}"


def test_wallet_identities_own_pk_is_not_an_fk():
    """The anchor's own PK is the FK target for everything else, not itself an FK."""
    assert _fk_targets("wallet_identities") == {}


# ─── Lowercase / enum CHECK constraints — DB-level enforcement ────────────


def _assert_check_violation(sql: str, params: dict) -> None:
    """Run ``sql`` and assert it raises IntegrityError (a CHECK violation),
    then roll back so the failed statement doesn't poison later queries on
    the shared session/engine."""
    session = get_session()
    try:
        with pytest.raises(IntegrityError):
            session.execute(text(sql), params)
            session.commit()
    finally:
        session.rollback()
        session.close()


def test_wallet_identities_rejects_uppercase_address():
    _assert_check_violation(
        "INSERT INTO wallet_identities (wallet_address, actor_class, first_seen_at) "
        "VALUES (:addr, 'human', CURRENT_TIMESTAMP)",
        {"addr": "0x" + "AB" * 20},
    )


def test_wallet_identities_rejects_unknown_actor_class():
    _assert_check_violation(
        "INSERT INTO wallet_identities (wallet_address, actor_class, first_seen_at) "
        "VALUES (:addr, 'not_a_real_class', CURRENT_TIMESTAMP)",
        {"addr": _unique_wallet()},
    )


def test_wallet_identities_accepts_every_documented_actor_class():
    """The flip side of the enum CHECK: every value D3 documents must be
    accepted, not just rejected-by-default on typos."""
    for actor_class in ("human", "agent", "operator_dogfood", "system"):
        addr = _unique_wallet()
        session = get_session()
        try:
            session.execute(
                text(
                    "INSERT INTO wallet_identities (wallet_address, actor_class, first_seen_at) "
                    "VALUES (:addr, :actor_class, CURRENT_TIMESTAMP)"
                ),
                {"addr": addr, "actor_class": actor_class},
            )
            session.commit()
        finally:
            session.execute(text("DELETE FROM wallet_identities WHERE wallet_address = :addr"), {"addr": addr})
            session.commit()
            session.close()


def test_controlled_wallets_rejects_uppercase_address():
    _assert_check_violation(
        "INSERT INTO controlled_wallets (address, wallet_class, provisioned_at) "
        "VALUES (:addr, 'treasury', CURRENT_TIMESTAMP)",
        {"addr": "0x" + "CD" * 20},
    )


def test_controlled_wallets_rejects_unknown_wallet_class():
    _assert_check_violation(
        "INSERT INTO controlled_wallets (address, wallet_class, provisioned_at) "
        "VALUES (:addr, 'not_a_real_class', CURRENT_TIMESTAMP)",
        {"addr": _unique_wallet()},
    )


def test_identity_events_rejects_unknown_actor_class():
    _assert_check_violation(
        "INSERT INTO identity_events (wallet, event_type, actor_class, occurred_at, meta) "
        "VALUES (NULL, 'auth_verified', 'not_a_real_class', CURRENT_TIMESTAMP, '{}')",
        {},
    )


def test_vault_metadata_rejects_uppercase_vault_address():
    """Casing fix (issue #1028): the EIP-55-checksummed vault_address bug
    (executor.py) — the CHECK makes lower()-on-write durable at the DB layer."""
    _assert_check_violation(
        "INSERT INTO vault_metadata (vault_address, name, symbol, creator_address, strategy_ids, created_at) "
        "VALUES (:addr, 'v', 'V', 'system', '[]', CURRENT_TIMESTAMP)",
        {"addr": "0x" + "EF" * 20},
    )


def test_marketplace_agents_rejects_uppercase_gateway_seller_address():
    """Casing fix (issue #1028): #958's un-lowercased Circle API response."""
    _assert_check_violation(
        "INSERT INTO marketplace_agents "
        "(role, strategy_id, sub_id, pool_id, vault_address, ephemeral_wallet, status, halted, "
        "gateway_seller_address, created_at) "
        "VALUES ('publisher', :sid, '', '', '', '', 'running', 0, :addr, CURRENT_TIMESTAMP)",
        {"sid": f"schema-test-{uuid.uuid4().hex[:8]}", "addr": "0x" + "AB" * 20},
    )


def test_marketplace_agents_allows_null_gateway_seller_address():
    """The lowercase CHECK is ``IS NULL OR = lower(...)`` — NULL (subscriber
    rows, or a publisher pre-provisioning) must not be rejected."""
    sid = f"schema-test-null-{uuid.uuid4().hex[:8]}"
    session = get_session()
    try:
        session.execute(
            text(
                "INSERT INTO marketplace_agents "
                "(role, strategy_id, sub_id, pool_id, vault_address, ephemeral_wallet, status, halted, "
                "gateway_seller_address, created_at) "
                "VALUES ('publisher', :sid, '', '', '', '', 'running', 0, NULL, CURRENT_TIMESTAMP)"
            ),
            {"sid": sid},
        )
        session.commit()
    finally:
        session.execute(text("DELETE FROM marketplace_agents WHERE strategy_id = :sid"), {"sid": sid})
        session.commit()
        session.close()


# ─── FK enforcement (real Postgres only) ───────────────────────────────────
#
# Every test above pins FK *presence* via `sqlalchemy.inspect` against the
# SQLite-built schema, per the module docstring: SQLite doesn't enforce FKs
# without `PRAGMA foreign_keys=ON`, which nothing in this codebase sets, so
# an SQLite insert of an orphan wallet reference would silently succeed and
# prove nothing about the retrofit actually protecting data integrity. This
# test closes that gap by running the real insert against real Postgres —
# the FK is only meaningfully verified once an IntegrityError is observed
# from the database, not merely inferred from the constraint existing in
# `information_schema`.
#
# Marked `integration` (registered in `pytest.ini`) and skipped cleanly when
# no Postgres DSN is configured — the default unit run (`pytest -m "not
# integration"`, per `quality-gate.yml`) never touches this test; it's meant
# to run in an environment with a real Postgres available (e.g. `docker
# compose up -d postgres` locally, or a future CI Postgres service).


def _postgres_test_dsn() -> str | None:
    """The DSN to run this test against, or None to skip.

    Deliberately reads the raw `DATABASE_URL` env var directly rather than
    `archimedes.db.DATABASE_URL` — the latter falls back to a SQLite file
    when unset (see `db.py`'s `_default_database_url()`), which would make
    this test silently exercise SQLite instead of skipping. The value it reads
    still goes through `_pin_psycopg2_driver`, as `db.DATABASE_URL` does.
    """
    dsn = os.environ.get("DATABASE_URL", "")
    return _pin_psycopg2_driver(dsn) if dsn.startswith("postgresql") else None


def test_postgres_test_dsn_pins_the_psycopg2_driver(monkeypatch):
    """The skip message below asks for a ``postgresql://`` DSN, and SQLAlchemy 2.1
    resolves that bare scheme to psycopg (v3), which is not installed. Unpinned,
    ``create_engine`` raises ModuleNotFoundError before the unreachable-DB skip
    can run, so the integration test fails instead of testing the FK."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/d")
    dsn = _postgres_test_dsn()
    assert dsn == "postgresql+psycopg2://u:p@h:5432/d"
    engine = create_engine(dsn)
    try:
        assert engine.dialect.driver == "psycopg2"
    finally:
        engine.dispose()

    monkeypatch.setenv("DATABASE_URL", "sqlite:///x.db")
    assert _postgres_test_dsn() is None


@pytest.mark.integration
def test_vault_metadata_creator_address_fk_enforced_on_postgres():
    """Inserting a vault_metadata row whose creator_address references a
    wallet absent from wallet_identities must raise IntegrityError — the
    FK retrofit (issue #1028) actually enforced, not just declared."""
    dsn = _postgres_test_dsn()
    if not dsn:
        pytest.skip("no Postgres DATABASE_URL configured — set DATABASE_URL to a postgresql:// DSN to run")

    engine = create_engine(dsn)
    try:
        try:
            conn = engine.connect()
            conn.close()
        except OperationalError as exc:
            pytest.skip(f"Postgres DSN configured but unreachable: {exc}")

        session = sessionmaker(bind=engine)()
        orphan_wallet = "0x" + uuid.uuid4().hex[:40]  # not backfilled into wallet_identities
        try:
            with pytest.raises(IntegrityError):
                session.execute(
                    text(
                        "INSERT INTO vault_metadata "
                        "(vault_address, name, symbol, creator_address, strategy_ids, created_at) "
                        "VALUES (:addr, 'fk-test-vault', 'FKT', :creator, '[]', CURRENT_TIMESTAMP)"
                    ),
                    {"addr": orphan_wallet, "creator": orphan_wallet},
                )
                session.commit()
        finally:
            session.rollback()
            session.close()
    finally:
        engine.dispose()
