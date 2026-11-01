"""#1432: purging the SIWE vid-wallet pairs and the per-vault chat rows.

Exercises the REAL alembic revision (``5cdd4f098a74``) and the read-only count
script as subprocesses against a throwaway SQLite file with a whitelist-only
environment, the same hermetic pattern ``test_clear_oauth_id_tokens_migration.py``
uses: a developer's ``.env`` must not be able to point this at a
docker-compose Postgres or at production.

The seeded ``identity_events`` rows sit on both sides of each window bound,
one microsecond apart, and include an in-window row of another event type
that carries a vid. A revision that widened or narrowed the scope fails here.
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import subprocess
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
#: Named explicitly rather than "head": see test_orphan_row_adoption.py.
_REVISION = "5cdd4f098a74"
_REVISION_FILE = _BACKEND_DIR / "migrations" / "versions" / "5cdd4f098a74_purge_vid_wallet_pairs_and_chat_rows.py"

_START = datetime(2026, 7, 6, tzinfo=UTC)
_END = datetime(2026, 8, 19, 15, 30, 21, tzinfo=UTC)
_TICK = timedelta(microseconds=1)
_IN_WINDOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)

_W1 = "0x" + "11" * 20
_W2 = "0x" + "22" * 20
_AI = "0x" + "a1" * 20

# (id, wallet, vid, event_type, actor_class, occurred_at)
_EVENTS = [
    # In scope: the revision nulls these three vids.
    (1, _W1, "a" * 32, "auth_verified", "human", _START),
    (2, _W2, "b" * 32, "auth_verified", "human", _IN_WINDOW),
    (3, _W1, "c" * 32, "auth_verified", "human", _END - _TICK),
    # Carry a vid but sit outside the scope: left exactly as they are.
    (4, _W1, "d" * 32, "auth_verified", "human", _START - _TICK),
    (5, _W2, "e" * 32, "auth_verified", "human", _END),
    (6, _W2, "f" * 32, "auth_verified", "human", datetime(2026, 9, 15, tzinfo=UTC)),
    (7, _W1, "0" * 32, "generation_started", "human", _IN_WINDOW),
    # No vid at all: nothing to clear.
    (8, _W2, None, "auth_verified", "human", _IN_WINDOW),
    (9, _W1, None, "vault_created", "human", _IN_WINDOW),
    (10, _AI, None, "chat_posted", "agent", _IN_WINDOW),
    (11, None, None, "treasury_swept", "system", _IN_WINDOW),
]
_IN_SCOPE_IDS = {1, 2, 3}
_OUTSIDE_SCOPE_VID_IDS = {4, 5, 6, 7}

# (id, vault_address, wallet_address, message, is_ai, verified)
_CHAT = [
    (1, "0x" + "c0" * 20, _W1, "hello vault", False, True),
    (2, "0x" + "c0" * 20, _W2, "@archimedes why rebalance?", False, False),
    (3, "0x" + "c0" * 20, _AI, "Momentum turned.", True, False),
]


def _clean_env(database_url: str) -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""), "DATABASE_URL": database_url}


def _run_alembic(*args: str, database_url: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["alembic", *args],
        cwd=str(_BACKEND_DIR),
        env=_clean_env(database_url),
        capture_output=True,
        text=True,
        timeout=120,
    )


def _run_count_script(database_url: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "archimedes.scripts.count_privacy_leftovers"],
        cwd=str(_BACKEND_DIR),
        env=_clean_env(database_url),
        capture_output=True,
        text=True,
        timeout=120,
    )


def _down_revision() -> str:
    # A literal, so the fixture still builds on a checkout where the revision
    # under test does not exist (main), and asserted against the file below.
    return "7d2f9a4c1e60"


def test_down_revision_literal_matches_the_revision_file():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(_BACKEND_DIR / "alembic.ini")))
    assert script.get_revision(_REVISION).down_revision == _down_revision()


def test_count_script_window_matches_the_revision_window():
    spec = importlib.util.spec_from_file_location("_purge_revision", _REVISION_FILE)
    assert spec is not None and spec.loader is not None, f"revision file missing: {_REVISION_FILE}"
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)

    from archimedes.scripts import count_privacy_leftovers

    assert (revision.WINDOW_START, revision.WINDOW_END) == (_START, _END)
    assert (count_privacy_leftovers.WINDOW_START, count_privacy_leftovers.WINDOW_END) == (_START, _END)


def _seed(db_path: Path) -> None:
    import sqlalchemy as sa

    engine = sa.create_engine(f"sqlite:///{db_path}")
    meta = sa.MetaData()
    meta.reflect(bind=engine)
    t = meta.tables
    with engine.begin() as conn:
        conn.execute(
            t["wallet_identities"].insert(),
            [
                {"wallet_address": _W1, "actor_class": "human", "first_seen_at": _START},
                {"wallet_address": _W2, "actor_class": "human", "first_seen_at": _START},
                {"wallet_address": _AI, "actor_class": "agent", "first_seen_at": _START},
            ],
        )
        conn.execute(
            t["identity_events"].insert(),
            [
                {
                    "id": row_id,
                    "wallet": wallet,
                    "vid": vid,
                    "event_type": event_type,
                    "actor_class": actor_class,
                    "occurred_at": occurred_at,
                    "meta": {"seed": row_id},
                }
                for row_id, wallet, vid, event_type, actor_class, occurred_at in _EVENTS
            ],
        )
        conn.execute(
            t["chat_messages"].insert(),
            [
                {
                    "id": row_id,
                    "vault_address": vault,
                    "wallet_address": wallet,
                    "message": message,
                    "is_ai": is_ai,
                    "verified": verified,
                    "created_at": _IN_WINDOW,
                }
                for row_id, vault, wallet, message, is_ai, verified in _CHAT
            ],
        )
    engine.dispose()


def _rows(db_path: Path, sql: str) -> list[tuple]:
    con = sqlite3.connect(str(db_path))
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _vids(db_path: Path) -> dict[int, str | None]:
    return dict(_rows(db_path, "SELECT id, vid FROM identity_events ORDER BY id"))


def _events_without_vid(db_path: Path) -> list[tuple]:
    return _rows(
        db_path, "SELECT id, wallet, event_type, actor_class, occurred_at, meta FROM identity_events ORDER BY id"
    )


def _dump(db_path: Path) -> list[str]:
    con = sqlite3.connect(str(db_path))
    try:
        return list(con.iterdump())
    finally:
        con.close()


def _prepared(tmp_path: Path, name: str) -> tuple[Path, str]:
    db_path = tmp_path / name
    url = f"sqlite:///{db_path}"
    pre = _run_alembic("upgrade", _down_revision(), database_url=url)
    assert pre.returncode == 0, pre.stderr
    _seed(db_path)
    assert {i for i, v in _vids(db_path).items() if v is not None} == _IN_SCOPE_IDS | _OUTSIDE_SCOPE_VID_IDS
    assert _rows(db_path, "SELECT COUNT(*) FROM chat_messages") == [(len(_CHAT),)]
    return db_path, url


def test_upgrade_nulls_only_in_window_siwe_vids_and_deletes_every_chat_row(tmp_path):
    db_path, url = _prepared(tmp_path, "purge.db")
    vids_before = _vids(db_path)
    events_before = _events_without_vid(db_path)
    wallets_before = _rows(db_path, "SELECT * FROM wallet_identities ORDER BY wallet_address")

    result = _run_alembic("upgrade", _REVISION, database_url=url)
    assert result.returncode == 0, f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    log = result.stdout + result.stderr
    assert "#1432 nulled vid on 3 SIWE auth_verified identity_events row(s)" in log
    assert "#1432 deleted 3 chat_messages row(s); the table is kept." in log

    vids_after = _vids(db_path)
    assert {i for i, v in vids_after.items() if v is None} == {i for i, *_ in _EVENTS} - _OUTSIDE_SCOPE_VID_IDS
    for row_id in _OUTSIDE_SCOPE_VID_IDS:
        assert vids_after[row_id] == vids_before[row_id], f"row {row_id} is outside the scope and must keep its vid"
    # Every other column of every ledger row, and the row count, are untouched.
    assert _events_without_vid(db_path) == events_before

    assert _rows(db_path, "SELECT COUNT(*) FROM chat_messages") == [(0,)]
    assert "chat_messages" in {name for (name,) in _rows(db_path, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert _rows(db_path, "SELECT * FROM wallet_identities ORDER BY wallet_address") == wallets_before


def test_running_the_upgrade_a_second_time_is_a_no_op(tmp_path):
    db_path, url = _prepared(tmp_path, "idempotent.db")

    first = _run_alembic("upgrade", _REVISION, database_url=url)
    assert first.returncode == 0, first.stderr
    after_first = _dump(db_path)

    # `stamp` rewinds the version pointer WITHOUT running downgrade(), so the
    # next upgrade re-executes upgrade() over already-purged data.
    stamp = _run_alembic("stamp", _down_revision(), database_url=url)
    assert stamp.returncode == 0, stamp.stderr
    second = _run_alembic("upgrade", _REVISION, database_url=url)
    assert second.returncode == 0, f"STDOUT:\n{second.stdout}\nSTDERR:\n{second.stderr}"
    log = second.stdout + second.stderr
    assert "#1432 nulled vid on 0 SIWE auth_verified identity_events row(s)" in log
    assert "#1432 deleted 0 chat_messages row(s); the table is kept." in log

    assert _dump(db_path) == after_first


def test_downgrade_restores_nothing_and_keeps_the_schema(tmp_path):
    db_path, url = _prepared(tmp_path, "downgrade.db")
    schema_before = _rows(
        db_path, "SELECT type, name, sql FROM sqlite_master WHERE name != 'alembic_version' ORDER BY name"
    )

    up = _run_alembic("upgrade", _REVISION, database_url=url)
    assert up.returncode == 0, up.stderr
    events_after_upgrade = _rows(db_path, "SELECT * FROM identity_events ORDER BY id")

    down = _run_alembic("downgrade", _down_revision(), database_url=url)
    assert down.returncode == 0, f"STDOUT:\n{down.stdout}\nSTDERR:\n{down.stderr}"
    assert _rows(db_path, "SELECT version_num FROM alembic_version") == [(_down_revision(),)]
    assert (
        _rows(db_path, "SELECT type, name, sql FROM sqlite_master WHERE name != 'alembic_version' ORDER BY name")
        == schema_before
    )
    assert _rows(db_path, "SELECT * FROM identity_events ORDER BY id") == events_after_upgrade
    assert _rows(db_path, "SELECT COUNT(*) FROM chat_messages") == [(0,)]

    again = _run_alembic("upgrade", _REVISION, database_url=url)
    assert again.returncode == 0, again.stderr


def _printed_counts(result: subprocess.CompletedProcess) -> dict[str, str]:
    assert result.returncode == 0, f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    return dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line and not line.startswith("window=")
    )


def test_count_script_reports_the_counts_and_writes_nothing(tmp_path):
    db_path, url = _prepared(tmp_path, "count.db")
    before = _dump(db_path)

    counts = _printed_counts(_run_count_script(url))

    assert counts == {
        "identity_events_siwe_vid_pairs_in_window": "3",
        "identity_events_vid_rows_outside_scope": str(len(_OUTSIDE_SCOPE_VID_IDS)),
        "chat_messages_rows": str(len(_CHAT)),
    }
    assert _dump(db_path) == before


def test_count_script_after_the_upgrade_reports_nothing_left_in_scope(tmp_path):
    _db_path, url = _prepared(tmp_path, "count-after.db")
    up = _run_alembic("upgrade", _REVISION, database_url=url)
    assert up.returncode == 0, up.stderr

    counts = _printed_counts(_run_count_script(url))

    assert counts == {
        "identity_events_siwe_vid_pairs_in_window": "0",
        # The seeded out-of-scope rows are still there, by design.
        "identity_events_vid_rows_outside_scope": str(len(_OUTSIDE_SCOPE_VID_IDS)),
        "chat_messages_rows": "0",
    }


def test_count_script_reports_a_missing_chat_table_as_absent(tmp_path):
    db_path, url = _prepared(tmp_path, "count-absent.db")
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("DROP TABLE chat_messages")
        con.commit()
    finally:
        con.close()

    counts = _printed_counts(_run_count_script(url))

    assert counts["chat_messages_rows"] == "absent"
    assert counts["identity_events_siwe_vid_pairs_in_window"] == "3"


# ─── The count script's read-only guards ────────────────────────────────────
#
# The tests above check that the database is unchanged after the script runs.
# A write the script rolls back would pass them too. These check that the
# DATABASE refuses a write on the script's connection: remove ``PRAGMA
# query_only = ON`` or ``SET TRANSACTION READ ONLY`` (or move either after the
# first query) and the matching test below fails.

_WRITE_ATTEMPT = "UPDATE identity_events SET vid = NULL WHERE vid IS NOT NULL"


def test_count_script_sqlite_connection_refuses_a_write_before_every_query(tmp_path):
    """Attempt a real write on the script's own DBAPI connection immediately
    before each of its SELECTs. ``PRAGMA query_only`` must make SQLite refuse
    every one of them. Without the guard the write succeeds, and the script's
    closing rollback then undoes it, so the dump check in
    ``test_count_script_reports_the_counts_and_writes_nothing`` cannot see the
    difference. That is why this test exists."""
    import sqlalchemy as sa
    from archimedes.scripts.count_privacy_leftovers import count_leftovers

    db_path, _url = _prepared(tmp_path, "readonly.db")
    before = _dump(db_path)
    engine = sa.create_engine(f"sqlite:///{db_path}")
    outcomes: list[str] = []

    @sa.event.listens_for(engine, "before_cursor_execute")
    def _try_to_write(_conn, cursor, statement, _params, _context, _executemany):
        if not statement.lstrip().upper().startswith("SELECT"):
            return
        try:
            cursor.connection.execute(_WRITE_ATTEMPT)
        except sqlite3.OperationalError as exc:
            outcomes.append(f"refused: {exc}")
        else:
            outcomes.append("ACCEPTED")

    try:
        counts = count_leftovers(engine)
    finally:
        engine.dispose()

    assert len(outcomes) == 3, f"expected one write attempt per SELECT, got {outcomes}"
    assert outcomes == ["refused: attempt to write a readonly database"] * 3, (
        f"a write through the count script's connection was not refused: {outcomes}"
    )
    # An accepted write would have nulled the vids before the first count read them.
    assert counts["identity_events_siwe_vid_pairs_in_window"] == 3
    assert _dump(db_path) == before


class _RecordingPostgresConnection:
    """Enough of a SQLAlchemy ``Connection`` for ``count_leftovers``' Postgres
    branch: it records every statement, commit and rollback in order. The suite
    has no Postgres server, so this asserts the guard's ORDER (Postgres only
    accepts ``SET TRANSACTION`` before the transaction's first query, and from
    then on refuses writes in it) rather than a live refusal."""

    dialect = types.SimpleNamespace(name="postgresql")

    def __init__(self, log: list[str]) -> None:
        self._log = log

    def __enter__(self) -> _RecordingPostgresConnection:
        return self

    def __exit__(self, *_exc) -> bool:
        self._log.append("<close>")
        return False

    def execute(self, clause):
        self._log.append(" ".join(str(clause).split()))
        return types.SimpleNamespace(scalar_one=lambda: 0)

    def commit(self) -> None:
        self._log.append("<commit>")

    def rollback(self) -> None:
        self._log.append("<rollback>")


def test_count_script_opens_its_postgres_transaction_read_only_before_any_query(monkeypatch):
    import sqlalchemy as sa
    from archimedes.scripts.count_privacy_leftovers import count_leftovers

    log: list[str] = []
    engine = types.SimpleNamespace(connect=lambda: _RecordingPostgresConnection(log))
    # ``has_table`` is the one call that needs a live inspector; answer it here.
    monkeypatch.setattr(sa, "inspect", lambda _conn: types.SimpleNamespace(has_table=lambda _name: True))

    count_leftovers(engine)

    assert log[0] == "SET TRANSACTION READ ONLY", f"the first statement must open the transaction read-only: {log}"
    queries = log[1 : log.index("<rollback>")]
    assert len(queries) == 3 and all(q.startswith("SELECT COUNT(*) FROM ") for q in queries), log
    assert "<commit>" not in log, f"the read-only transaction must be rolled back, never committed: {log}"
    assert log[-2:] == ["<rollback>", "<close>"], log
