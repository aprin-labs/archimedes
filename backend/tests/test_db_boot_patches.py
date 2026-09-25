"""Boot schema patches cannot hold production hostage (#1818).

On 2026-09-03 production served 504s for 94 minutes and the replacement ECS
tasks sat wedged at boot for 91 of them. Nothing was slow: ECS CPU was ~3% and
Aurora ~4% throughout. It was a DDL lock chain, and ``init_db()`` was one end of
it — the hand-rolled ``ALTER TABLE … ADD COLUMN IF NOT EXISTS`` patches that run
on every Postgres boot from ``main.py``.

Two properties of those patches turned a lock contention into an outage:

1. **All of them shared ONE transaction.** The three ``ALTER TABLE papers``
   statements ran first, and even a no-op ``ADD COLUMN IF NOT EXISTS`` holds
   AccessExclusiveLock on the table for the rest of the transaction. So when the
   fourth statement (``ALTER TABLE strategy_store``) queued behind somebody
   else, ``papers`` stayed locked by us — and a *waiting* exclusive-lock request
   queues every later reader of that table behind it. The Aurora log's
   ``deadlock detected`` at 15:02:58 names exactly this pair.
2. **There was no ``lock_timeout``.** A boot could therefore wait forever. It
   waited 91 minutes, logging nothing, until an unrelated OOM kill broke the
   chain.

These tests pin the fix: one statement, one transaction, with
``SET LOCAL lock_timeout`` set inside it on Postgres — and a patch that cannot
take its lock is a WARNING that boot walks past, not a hang and not a crash.
The patches were already declared non-fatal (Alembic owns Postgres schema,
#1028); this makes the declaration true under contention.

Hermetic: no Postgres. The Postgres cases drive a recording fake engine, which
is what makes "each statement in its own transaction" observable at all — a
real connection would not tell you where the transaction boundaries were. The
SQLite case uses a real tmp-file SQLite engine, because the thing being pinned
there is that behaviour did NOT change.
"""

from __future__ import annotations

import logging
import types

import pytest
import sqlalchemy
from archimedes import db
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError

LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = '5s'"

# The papers/strategy_store/chat_messages patch list, in order, as init_db
# issues it. Duplicated here on purpose: this is the ORDER that made the
# incident possible (papers first, strategy_store fourth), so a test that read
# the list out of the module could not notice it being reordered.
EXPECTED_PATCH_ORDER = [
    "ALTER TABLE papers ADD COLUMN IF NOT EXISTS cluster_id TEXT",
    "ALTER TABLE papers ADD COLUMN IF NOT EXISTS topic_label TEXT",
    "ALTER TABLE papers ADD COLUMN IF NOT EXISTS content_hash TEXT",
    "ALTER TABLE strategy_store ADD COLUMN IF NOT EXISTS is_example BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE strategy_store ADD COLUMN IF NOT EXISTS on_chain_registration_tx VARCHAR(66)",
    "ALTER TABLE strategy_store ADD COLUMN IF NOT EXISTS on_chain_registration_block VARCHAR(32)",
    "ALTER TABLE strategy_store ADD COLUMN IF NOT EXISTS parent_id VARCHAR(64)",
    "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS verified BOOLEAN NOT NULL DEFAULT FALSE",
]


def lock_timeout_error(stmt: str) -> OperationalError:
    """What psycopg2 raises when ``lock_timeout`` fires, in SQLAlchemy's wrapper.

    ``LockNotAvailable`` (SQLSTATE 55P03) surfaces as ``sqlalchemy.exc.
    OperationalError``; the message is Postgres's own. Built here rather than
    imported so the test needs no psycopg2 and no live server.
    """
    return OperationalError(stmt, {}, Exception("canceling statement due to lock timeout"))


# ─── The recording engine ──────────────────────────────────────────────


class _RecordingConnection:
    def __init__(self, engine: _RecordingEngine, statements: list[str]) -> None:
        self._engine = engine
        self._statements = statements

    def execute(self, clause, *args, **kwargs):
        sql = str(clause)
        self._statements.append(sql)
        self._engine.events.append(("execute", sql))
        # ``on_statement`` is how a test makes TIME pass: a real lock timeout
        # burns five wall-clock seconds, and the aggregate-budget tests need
        # that without sleeping 45 of them.
        if self._engine.on_statement is not None:
            self._engine.on_statement(sql)
        raiser = self._engine.raise_on
        if raiser is not None and raiser[0] in sql:
            raise raiser[1]
        return


class _Transaction:
    def __init__(self, engine: _RecordingEngine) -> None:
        self._engine = engine
        self._statements: list[str] = []

    def __enter__(self) -> _RecordingConnection:
        self._engine.transactions.append(self._statements)
        self._engine.events.append(("begin", None))
        return _RecordingConnection(self._engine, self._statements)

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._engine.events.append(("rollback" if exc_type else "commit", None))
        return False


class _RecordingEngine:
    """A stand-in for ``archimedes.db.engine`` that records its transactions.

    ``transactions`` is a list of statement-lists, one per ``engine.begin()``.
    That structure is the whole point: the #1818 bug is invisible in a flat SQL
    log and obvious in this one.
    """

    def __init__(
        self,
        *,
        dialect_name: str = "postgresql",
        raise_on: tuple[str, Exception] | None = None,
        on_statement=None,
    ) -> None:
        self.dialect = types.SimpleNamespace(name=dialect_name)
        self.transactions: list[list[str]] = []
        self.events: list[tuple[str, str | None]] = []
        self.raise_on = raise_on
        self.on_statement = on_statement

    def begin(self) -> _Transaction:
        return _Transaction(self)


@pytest.fixture
def pg_engine(monkeypatch):
    """Install a recording Postgres-dialect engine as the module's engine."""
    fake = _RecordingEngine()
    monkeypatch.setattr(db, "engine", fake)
    monkeypatch.setattr(db, "DATABASE_URL", "postgresql://user:pw@host:5432/archimedes")
    return fake


# ─── _apply_patch_statement: the unit contract ─────────────────────────


class TestApplyPatchStatement:
    def test_postgres_sets_lock_timeout_before_the_statement_in_one_transaction(self, pg_engine):
        assert db._apply_patch_statement("ALTER TABLE papers ADD COLUMN IF NOT EXISTS x TEXT", context="papers") is True

        assert pg_engine.transactions == [[LOCK_TIMEOUT_SQL, "ALTER TABLE papers ADD COLUMN IF NOT EXISTS x TEXT"]], (
            "the lock timeout must be set inside the same transaction, before the DDL"
        )
        assert pg_engine.events[-1] == ("commit", None)

    def test_the_timeout_value_is_configurable_and_defaults_to_five_seconds(self, pg_engine, monkeypatch):
        """5s is longer than any healthy patch here (all no-ops on a current
        schema) and short enough that a boot is never held hostage. The env
        override exists so an operator can widen it during a migration window
        without a deploy."""
        assert db.PATCH_LOCK_TIMEOUT == "5s"

        monkeypatch.setattr(db, "PATCH_LOCK_TIMEOUT", "250ms")
        db._apply_patch_statement("ALTER TABLE papers ADD COLUMN IF NOT EXISTS x TEXT", context="papers")

        assert pg_engine.transactions[0][0] == "SET LOCAL lock_timeout = '250ms'"

    def test_sqlite_gets_no_set_local(self, monkeypatch):
        """SQLite has no ``SET LOCAL`` and no lock queue. Issuing it there would
        turn every local-dev and hermetic-test boot into a syntax error — this
        is the assertion that keeps the fix Postgres-only."""
        fake = _RecordingEngine(dialect_name="sqlite")
        monkeypatch.setattr(db, "engine", fake)

        assert db._apply_patch_statement("ALTER TABLE strategy_store ADD COLUMN owner_wallet VARCHAR(42)", context="x")

        assert fake.transactions == [["ALTER TABLE strategy_store ADD COLUMN owner_wallet VARCHAR(42)"]]

    def test_a_lock_timeout_is_a_warning_and_does_not_propagate(self, monkeypatch, caplog):
        """The whole outage in one assertion: a patch that cannot take its lock
        must not stop the boot."""
        stmt = "ALTER TABLE papers ADD COLUMN IF NOT EXISTS cluster_id TEXT"
        fake = _RecordingEngine(raise_on=("ALTER TABLE papers", lock_timeout_error(stmt)))
        monkeypatch.setattr(db, "engine", fake)

        with caplog.at_level(logging.WARNING, logger=db.__name__):
            assert db._apply_patch_statement(stmt, context="papers") is False

        assert any("could not take its lock within 5s" in r.getMessage() for r in caplog.records), (
            f"a lock timeout was silent; records: {[r.getMessage() for r in caplog.records]}"
        )
        assert fake.events[-1] == ("rollback", None), "the failed patch's transaction must not be left open"

    def test_any_other_failure_is_also_non_fatal(self, monkeypatch, caplog):
        """The patches were already declared non-fatal. Narrowing the arm to
        OperationalError only would have made an UndefinedTable a boot crash."""
        fake = _RecordingEngine(raise_on=("ALTER", RuntimeError("connection reset")))
        monkeypatch.setattr(db, "engine", fake)

        with caplog.at_level(logging.WARNING, logger=db.__name__):
            assert db._apply_patch_statement("ALTER TABLE nope ADD COLUMN x TEXT", context="papers") is False

        assert any("patch failed (non-fatal)" in r.getMessage() for r in caplog.records)


# ─── init_db(): the papers/strategy_store/chat_messages block ──────────


class TestInitDbPatchTransactions:
    @pytest.fixture(autouse=True)
    def _isolate_ownership(self, monkeypatch):
        """``_ensure_ownership_columns`` has its own tests below; stub it here so
        the transaction ledger this class reads is exactly the patch block."""
        monkeypatch.setattr(db, "_ensure_ownership_columns", lambda **_kw: None)

    def test_every_patch_gets_its_own_transaction_and_its_own_lock_timeout(self, pg_engine):
        """THE regression test for #1818.

        Before the fix this was one transaction holding eight statements, so
        ``papers`` stayed exclusively locked while ``strategy_store`` waited.
        Each pair here — timeout, then one DDL — is a transaction that commits
        and releases before the next one asks for anything.
        """
        db.init_db()

        assert len(pg_engine.transactions) == len(EXPECTED_PATCH_ORDER), (
            f"expected one transaction per patch statement, got {len(pg_engine.transactions)}: {pg_engine.transactions}"
        )
        for txn, stmt in zip(pg_engine.transactions, EXPECTED_PATCH_ORDER, strict=True):
            assert txn == [LOCK_TIMEOUT_SQL, stmt]

    def test_no_transaction_ever_holds_two_tables_at_once(self, pg_engine):
        """The mechanism, stated directly.

        The deadlock DETAIL at 15:02:58 was a reader of ``papers`` blocked by an
        ``ALTER TABLE strategy_store`` in the same transaction that had already
        locked ``papers``. If no transaction touches two tables, that shape
        cannot exist regardless of what else is running.
        """
        db.init_db()

        for txn in pg_engine.transactions:
            tables = {stmt.split()[2] for stmt in txn if stmt.startswith("ALTER TABLE")}
            assert len(tables) <= 1, f"one transaction locks {tables}: {txn}"

    def test_a_blocked_patch_does_not_stop_the_ones_behind_it(self, monkeypatch, caplog):
        """The second half of the fix.

        With one shared transaction, a timeout on statement 1 rolled back all
        eight. Now the boot logs the one it could not take and applies the other
        seven — which matters because ``strategy_store.is_example`` missing is a
        500 on every Generate.
        """
        fake = _RecordingEngine(
            raise_on=("ALTER TABLE papers ADD COLUMN IF NOT EXISTS cluster_id", lock_timeout_error("cluster_id")),
        )
        monkeypatch.setattr(db, "engine", fake)
        monkeypatch.setattr(db, "DATABASE_URL", "postgresql://user:pw@host:5432/archimedes")

        with caplog.at_level(logging.INFO, logger=db.__name__):
            db.init_db()  # must not raise

        ran = [stmt for txn in fake.transactions for stmt in txn if stmt.startswith(("ALTER", "CREATE"))]
        assert ran == EXPECTED_PATCH_ORDER, "a blocked patch swallowed the statements behind it"
        assert any("7/8 statements" in r.getMessage() for r in caplog.records), (
            f"the summary line does not report the partial application; saw {[r.getMessage() for r in caplog.records]}"
        )

    def test_sqlite_urls_run_no_postgres_patches(self, monkeypatch):
        """Unchanged behaviour: the ``ADD COLUMN IF NOT EXISTS`` list is Postgres
        syntax and is gated on the URL, not merely on the dialect."""
        fake = _RecordingEngine(dialect_name="sqlite")
        monkeypatch.setattr(db, "engine", fake)
        monkeypatch.setattr(db, "DATABASE_URL", "postgres-lookalike://nope")

        db.init_db()

        assert fake.transactions == []


# ─── _ensure_ownership_columns() ───────────────────────────────────────


class _FakeInspector:
    def __init__(self, tables: dict[str, list[str]]) -> None:
        self._tables = tables

    def has_table(self, name: str) -> bool:
        return name in self._tables

    def get_columns(self, name: str) -> list[dict[str, str]]:
        return [{"name": c} for c in self._tables[name]]


class TestEnsureOwnershipColumnsOnPostgres:
    @pytest.fixture
    def missing_everything(self, monkeypatch, pg_engine):
        monkeypatch.setattr(
            sqlalchemy,
            "inspect",
            lambda _bind: _FakeInspector({"strategy_store": ["id"], "strategy_passports": ["id"]}),
        )
        return pg_engine

    def test_each_alter_and_the_create_index_get_their_own_guarded_transaction(self, missing_everything):
        """The ``CREATE INDEX IF NOT EXISTS`` is the sharpest case here: it takes
        a lock on ``strategy_store`` too, and it used to ride in the same
        transaction as the ALTERs above it."""
        db._ensure_ownership_columns()

        ddl = [stmt for txn in missing_everything.transactions for stmt in txn if stmt != LOCK_TIMEOUT_SQL]
        assert ddl == [
            "ALTER TABLE strategy_store ADD COLUMN owner_wallet VARCHAR(42)",
            "ALTER TABLE strategy_store ADD COLUMN is_published BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE strategy_passports ADD COLUMN owner_wallet VARCHAR(42)",
            "ALTER TABLE strategy_passports ADD COLUMN universe_source VARCHAR(16)",
            "CREATE INDEX IF NOT EXISTS ix_strategy_store_owner_wallet ON strategy_store (owner_wallet)",
        ]
        for txn in missing_everything.transactions:
            assert len(txn) == 2 and txn[0] == LOCK_TIMEOUT_SQL, f"unguarded or shared transaction: {txn}"

    def test_a_blocked_index_creation_is_a_warning_not_a_crash(self, monkeypatch, missing_everything, caplog):
        monkeypatch.setattr(missing_everything, "raise_on", ("CREATE INDEX", lock_timeout_error("CREATE INDEX")))

        with caplog.at_level(logging.WARNING, logger=db.__name__):
            db._ensure_ownership_columns()  # must not raise

        assert any("could not take its lock" in r.getMessage() for r in caplog.records)

    def test_columns_that_already_exist_issue_no_ddl_at_all(self, monkeypatch, pg_engine):
        """MUTATION CHECK for the list above: if this helper ALTERed
        unconditionally it would take an exclusive lock on ``strategy_store``
        on every single boot, which is the failure mode the whole PR is about.
        The index statement is idempotent and stays."""
        monkeypatch.setattr(
            sqlalchemy,
            "inspect",
            lambda _bind: _FakeInspector(
                {
                    "strategy_store": ["id", "owner_wallet", "is_published"],
                    "strategy_passports": ["id", "owner_wallet", "universe_source"],
                }
            ),
        )

        db._ensure_ownership_columns()

        ddl = [stmt for txn in pg_engine.transactions for stmt in txn if stmt != LOCK_TIMEOUT_SQL]
        assert ddl == ["CREATE INDEX IF NOT EXISTS ix_strategy_store_owner_wallet ON strategy_store (owner_wallet)"]

    def test_a_broken_inspector_is_non_fatal_and_issues_nothing(self, monkeypatch, pg_engine, caplog):
        def boom(_bind):
            raise RuntimeError("could not reflect")

        monkeypatch.setattr(sqlalchemy, "inspect", boom)

        with caplog.at_level(logging.WARNING, logger=db.__name__):
            db._ensure_ownership_columns()

        assert pg_engine.transactions == []
        assert any("ownership column inspection failed" in r.getMessage() for r in caplog.records)


class TestEnsureOwnershipColumnsOnSqlite:
    """SQLite behaviour is unchanged — a real engine, so this is not a mock
    agreeing with itself.

    This is the path every hermetic test and every local dev boot takes. The
    columns must still land on a pre-existing table (create_all only covers
    FRESH databases), and no ``SET LOCAL`` may be issued, because SQLite would
    reject it outright.
    """

    @pytest.fixture
    def sqlite_engine(self, tmp_path, monkeypatch):
        engine = create_engine(f"sqlite:///{tmp_path / 'ownership.db'}")
        with engine.begin() as conn:
            # A strategy_store as it looked BEFORE the ownership columns landed.
            conn.execute(text("CREATE TABLE strategy_store (id VARCHAR(64) PRIMARY KEY, name TEXT)"))
        monkeypatch.setattr(db, "engine", engine)
        monkeypatch.setattr(db, "DATABASE_URL", f"sqlite:///{tmp_path / 'ownership.db'}")
        try:
            yield engine
        finally:
            engine.dispose()

    @staticmethod
    def _record(engine) -> list[str]:
        seen: list[str] = []

        @event.listens_for(engine, "before_cursor_execute")
        def _on(conn, cursor, statement, parameters, context, executemany):
            seen.append(statement)

        return seen

    def test_missing_columns_and_the_index_still_land(self, sqlite_engine):
        seen = self._record(sqlite_engine)

        db._ensure_ownership_columns()

        columns = {c["name"] for c in sqlalchemy.inspect(sqlite_engine).get_columns("strategy_store")}
        assert {"owner_wallet", "is_published"} <= columns
        indexes = {i["name"] for i in sqlalchemy.inspect(sqlite_engine).get_indexes("strategy_store")}
        assert "ix_strategy_store_owner_wallet" in indexes
        assert not any("lock_timeout" in s for s in seen), f"SET LOCAL leaked onto SQLite: {seen}"

    def test_a_second_run_is_a_no_op(self, sqlite_engine):
        """Idempotence is what makes this safe to run on every boot. SQLite has
        no ``ADD COLUMN IF NOT EXISTS``, so a second ALTER would be a hard
        error — caught and warned, but the column check is what prevents it."""
        db._ensure_ownership_columns()
        seen = self._record(sqlite_engine)

        db._ensure_ownership_columns()

        assert not any(s.startswith("ALTER TABLE") for s in seen), f"re-ALTERed an existing column: {seen}"


# ─── The aggregate budget ──────────────────────────────────────────────


class _Clock:
    """A monotonic clock a test can wind forward.

    Installed as ``db.time`` (``db.py`` calls ``time.monotonic()``), so the
    budget can be tested against thirteen five-second timeouts without spending
    sixty-five real seconds.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestTheAggregateLockBudget:
    """A per-statement timeout is not a bound on the boot.

    ``lock_timeout`` is per statement, and one ``init_db()`` issues 8 patch
    statements plus up to 5 more from ``_ensure_ownership_columns`` — 13 on a
    stale schema. At 5s each, a fully contended boot would burn 45-65s and blow
    straight past the 30s ALB health-check timeout that the 2026-09-03 timeline
    names, which is the same "task is up but answers nothing" shape the incident
    was about. So the whole call shares one deadline.
    """

    @staticmethod
    def _contended(clock: _Clock):
        """An engine where every DDL statement burns its full 5s and times out."""

        def burn(sql: str) -> None:
            if sql.startswith(("ALTER", "CREATE")):
                clock.advance(5.0)
                raise lock_timeout_error(sql)

        return _RecordingEngine(on_statement=burn)

    def test_a_fully_contended_boot_stops_at_the_budget_not_after_every_statement(self, monkeypatch, caplog):
        """THE regression test for the aggregate case.

        Eight statements that each take their full 5s is 40s of boot. With a
        10s budget the deadline is checked before each one, so the third never
        starts and the run ends at 10s.
        """
        clock = _Clock()
        monkeypatch.setattr(db, "time", clock)
        fake = self._contended(clock)
        monkeypatch.setattr(db, "engine", fake)
        monkeypatch.setattr(db, "DATABASE_URL", "postgresql://user:pw@host:5432/archimedes")
        monkeypatch.setattr(db, "PATCH_LOCK_BUDGET_SECONDS", 10.0)
        monkeypatch.setattr(db, "_ensure_ownership_columns", lambda **_kw: None)

        with caplog.at_level(logging.WARNING, logger=db.__name__):
            db.init_db()  # must not raise

        attempted = [s for txn in fake.transactions for s in txn if s.startswith("ALTER")]
        assert attempted == EXPECTED_PATCH_ORDER[:2], (
            f"the budget did not stop the run: {len(attempted)} statements attempted, {clock.now}s spent"
        )
        assert clock.now == 10.0, f"spent {clock.now}s of a 10s budget"

    def test_the_budget_gives_up_once_and_says_what_it_skipped(self, monkeypatch, caplog):
        """One WARNING, not one per skipped statement — a boot that gave up
        should be legible, and #1818 was invisible partly because nothing said
        anything at all."""
        clock = _Clock()
        monkeypatch.setattr(db, "time", clock)
        fake = self._contended(clock)
        monkeypatch.setattr(db, "engine", fake)
        monkeypatch.setattr(db, "DATABASE_URL", "postgresql://user:pw@host:5432/archimedes")
        monkeypatch.setattr(db, "PATCH_LOCK_BUDGET_SECONDS", 10.0)
        monkeypatch.setattr(db, "_ensure_ownership_columns", lambda **_kw: None)

        with caplog.at_level(logging.WARNING, logger=db.__name__):
            db.init_db()

        gave_up = [r.getMessage() for r in caplog.records if "aggregate lock budget" in r.getMessage()]
        assert len(gave_up) == 1, f"expected exactly one give-up line, got {gave_up}"
        assert "6 statement(s) skipped" in gave_up[0], gave_up[0]
        assert "is_example" in gave_up[0], "the give-up line must name what did not land"

    def test_the_ownership_block_shares_the_budget_rather_than_starting_a_fresh_one(self, monkeypatch, caplog):
        """The whole point of the aggregate bound.

        If ``_ensure_ownership_columns`` took its own 10s, ``init_db()`` would
        be bounded at 20s + 5s rather than 10s + 5s, and two of those are on
        the wrong side of the health check. The deadline is threaded through.
        """
        clock = _Clock()
        monkeypatch.setattr(db, "time", clock)
        fake = self._contended(clock)
        monkeypatch.setattr(db, "engine", fake)
        monkeypatch.setattr(db, "DATABASE_URL", "postgresql://user:pw@host:5432/archimedes")
        monkeypatch.setattr(db, "PATCH_LOCK_BUDGET_SECONDS", 10.0)
        monkeypatch.setattr(
            sqlalchemy,
            "inspect",
            lambda _bind: _FakeInspector({"strategy_store": ["id"], "strategy_passports": ["id"]}),
        )

        with caplog.at_level(logging.WARNING, logger=db.__name__):
            db.init_db()  # papers block alone spends the whole budget

        ownership_ddl = [s for txn in fake.transactions for s in txn if "owner_wallet" in s or "universe_source" in s]
        assert ownership_ddl == [], f"the ownership block started a fresh budget: {ownership_ddl}"
        assert clock.now == 10.0, f"init_db spent {clock.now}s, not the 10s budget"

    def test_an_uncontended_boot_still_applies_everything(self, monkeypatch):
        """The budget must not cost a healthy boot anything. Real patches are
        no-ops on a current schema and take microseconds; nothing is skipped."""
        clock = _Clock()
        monkeypatch.setattr(db, "time", clock)
        fake = _RecordingEngine()
        monkeypatch.setattr(db, "engine", fake)
        monkeypatch.setattr(db, "DATABASE_URL", "postgresql://user:pw@host:5432/archimedes")
        monkeypatch.setattr(db, "_ensure_ownership_columns", lambda **_kw: None)

        db.init_db()

        ran = [s for txn in fake.transactions for s in txn if s.startswith("ALTER")]
        assert ran == EXPECTED_PATCH_ORDER

    def test_a_malformed_budget_falls_back_to_ten_seconds(self, caplog):
        with caplog.at_level(logging.WARNING, logger=db.__name__):
            assert db._sanitised_lock_budget("abc") == 10.0
            assert db._sanitised_lock_budget("0") == 10.0
            assert db._sanitised_lock_budget("-3") == 10.0
        assert db._sanitised_lock_budget(None) == 10.0, "an unset env var is not a misconfiguration"
        assert db._sanitised_lock_budget("2.5") == 2.5, "a valid override must survive"


# ─── DB_PATCH_LOCK_TIMEOUT is interpolated into SQL, so it is validated ─


class TestTheLockTimeoutValueIsValidated:
    """``SET LOCAL`` takes a literal, not a bind parameter, so this env value
    reaches Postgres as text. An unvalidated typo is not a cosmetic bug: the
    ``SET LOCAL`` becomes a syntax error, the deliberately broad non-fatal arm
    swallows it into a WARNING, and EVERY transitional column is skipped —
    which is the "``strategy_store.is_example`` is missing, so Generate 500s"
    failure this whole block exists to avoid.
    """

    @pytest.mark.parametrize("value", ["5s", "250ms", "2min", "5000", "1h", "30us", "7d"])
    def test_valid_postgres_intervals_pass_through(self, value):
        assert db._sanitised_lock_timeout(value) == value

    @pytest.mark.parametrize(
        "value",
        [
            "5 s",  # the plausible typo: Postgres wants no space here
            "abc",
            "",
            "-5s",
            "5seconds",
            "'5s'",  # already-quoted: would close the literal early
            "5s'; DROP TABLE papers --",
        ],
    )
    def test_a_malformed_value_falls_back_to_five_seconds(self, value, caplog):
        with caplog.at_level(logging.WARNING, logger=db.__name__):
            assert db._sanitised_lock_timeout(value) == "5s"

        assert any("not a usable Postgres lock_timeout" in r.getMessage() for r in caplog.records), (
            f"a bad lock_timeout was accepted silently; records: {[r.getMessage() for r in caplog.records]}"
        )

    @pytest.mark.parametrize("value", ["0", "0s", "0ms"])
    def test_zero_is_rejected_because_postgres_reads_it_as_no_timeout(self, value, caplog):
        """The sharpest well-formed input there is.

        ``lock_timeout = 0`` does not mean "fail immediately" — in Postgres it
        DISABLES the timeout. It is the 2026-09-03 configuration exactly, and a
        regex that only checked shape would wave it through.
        """
        with caplog.at_level(logging.WARNING, logger=db.__name__):
            assert db._sanitised_lock_timeout(value) == "5s", f"{value!r} would restore the #1818 unbounded wait"

        assert any("would disable the timeout entirely" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("value", ["2147483648ms", "999999999s", "100d"])
    def test_values_past_int_max_milliseconds_are_rejected(self, value, caplog):
        """Well-formed but out of range: Postgres answers "outside the valid
        range for parameter", the broad non-fatal arm swallows it, and every
        patch is skipped — the same silent outcome as a syntax error."""
        with caplog.at_level(logging.WARNING, logger=db.__name__):
            assert db._sanitised_lock_timeout(value) == "5s"

    @pytest.mark.parametrize("value", ["2147483647ms", "1d"])
    def test_the_top_of_the_valid_range_is_still_accepted(self, value):
        """The bound must not be off by one, or an operator widening the window
        for a real migration gets silently overridden."""
        assert db._sanitised_lock_timeout(value) == value

    def test_a_malformed_value_cannot_silently_skip_every_patch(self, monkeypatch, caplog):
        """The consequence, end to end.

        This engine rejects the SQL a RAW ``5 s`` would produce, exactly as
        Postgres would. If the value reached the string unvalidated, all eight
        patches would be swallowed as WARNINGs and the columns would never
        land; with validation the run is clean and every ``SET LOCAL`` is the
        ``5s`` fallback.
        """
        syntax_error = OperationalError("SET LOCAL", {}, Exception('near "SET": syntax error'))
        fake = _RecordingEngine(raise_on=("'5 s'", syntax_error))
        monkeypatch.setattr(db, "engine", fake)
        monkeypatch.setattr(db, "DATABASE_URL", "postgresql://user:pw@host:5432/archimedes")
        monkeypatch.setattr(db, "_ensure_ownership_columns", lambda **_kw: None)
        monkeypatch.setattr(db, "PATCH_LOCK_TIMEOUT", "5 s")

        with caplog.at_level(logging.INFO, logger=db.__name__):
            db.init_db()

        ran = [s for txn in fake.transactions for s in txn if s.startswith("ALTER")]
        assert ran == EXPECTED_PATCH_ORDER, "a malformed DB_PATCH_LOCK_TIMEOUT skipped the transitional columns"
        assert {s for txn in fake.transactions for s in txn if s.startswith("SET")} == {LOCK_TIMEOUT_SQL}, (
            f"the raw value reached the SQL: {fake.transactions[0]}"
        )
        assert any("8/8 statements" in r.getMessage() for r in caplog.records)
