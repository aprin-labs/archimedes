"""Shared per-test isolation for archimedes.db's process-global engine.

``archimedes.db`` builds ``engine`` / ``SessionLocal`` once at import time from
whatever ``DATABASE_URL`` is set at that moment. Monkeypatching the
``DATABASE_URL`` env var afterward does not rebind an already-constructed
engine, and every runtime code path that resolves a session via
``get_session()`` looks up ``archimedes.db.SessionLocal`` dynamically at call
time — so the only way to give a test a genuinely fresh, isolated database is
to reassign the ``archimedes.db`` module attributes directly, and the only way
to do that safely when many test files share one pytest process is to restore
the originals afterward. Without the restore step, one test file's tmp-db
redirect silently outlives it and leaks into every test that runs later in
the same process (see issue #1100).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from archimedes import db as archimedes_db
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def redirect_to_tmp_sqlite(tmp_path: Path) -> Iterator[None]:
    """Point archimedes.db at a fresh tmp-file SQLite DB, then restore.

    Use from an autouse fixture: ``yield from redirect_to_tmp_sqlite(tmp_path)``.
    Saves the current engine/SessionLocal/DATABASE_URL, builds a new engine
    bound to a tmp_path-scoped file, creates every Base-registered table on
    it, yields to the test, then disposes the tmp engine and restores the
    originals — so no later test, in this file or any other, can inherit a
    dangling redirected engine.
    """
    orig_engine = archimedes_db.engine
    orig_session_local = archimedes_db.SessionLocal
    orig_database_url = archimedes_db.DATABASE_URL

    tmp_engine = None
    try:
        # Setup happens INSIDE the try: if create_engine/sessionmaker/create_all
        # raises after some module attributes were already reassigned, the
        # finally below still restores the originals — otherwise a setup-time
        # failure would leak the half-redirected globals into every later test
        # in the process, the exact #1100 failure mode this helper exists to fix.
        #
        # Same side-effect imports init_db() does before ITS create_all: ORM
        # models must register with Base.metadata or their tables (kg_*,
        # strategy_passports) silently won't exist on the tmp DB — whether
        # they do would otherwise depend on which other test file imported
        # those models first, the exact import-order roulette this helper
        # exists to eliminate (review follow-up).
        from archimedes.models import (  # noqa: F401
            kg,
            strategy_passport_record,
        )

        db_path = tmp_path / "test_archimedes.db"
        archimedes_db.DATABASE_URL = f"sqlite:///{db_path}"
        tmp_engine = create_engine(
            archimedes_db.DATABASE_URL,
            connect_args={"check_same_thread": False},
        )
        archimedes_db.engine = tmp_engine
        archimedes_db.SessionLocal = sessionmaker(
            bind=tmp_engine,
            autocommit=False,
            autoflush=False,
        )
        archimedes_db.Base.metadata.create_all(bind=tmp_engine)
        yield
    finally:
        if tmp_engine is not None:
            tmp_engine.dispose()
        archimedes_db.engine = orig_engine
        archimedes_db.SessionLocal = orig_session_local
        archimedes_db.DATABASE_URL = orig_database_url


@contextmanager
def isolated_empty_sqlite(tmp_path: Path) -> Iterator[None]:
    """``with`` form of :func:`redirect_to_tmp_sqlite`.

    Use this when a test calls ``load_corpus()`` with ``path=None`` and
    therefore takes the DB-first branch. A sibling TestClient / ASGI lifespan
    can have already seeded the process-global suite DB with the full
    ~18k-row manifest (the 18752-vs-4 failure on Quality Gate). Redirecting
    to an empty tmp sqlite lets the test measure the *file* fallback it
    claims — ``ARCHIMEDES_CORPUS_MANIFEST`` is that fallback's location, not
    a production DB bypass (issue #1640).
    """
    yield from redirect_to_tmp_sqlite(tmp_path)
