"""Tests for `archimedes.db` — default SQLite path resolution.

Covers the CWD-independence fix for `_default_database_url()`: the default
SQLite path must always anchor to `backend/archimedes_chat.db` regardless of
the process's current working directory, and the `DATABASE_URL` env var
override must remain unaffected.
"""

from __future__ import annotations

import os

from archimedes import db


class TestDefaultDatabaseUrl:
    def test_default_database_url_is_absolute(self):
        """The default SQLite URL must use an absolute path, not `./`."""
        url = db._default_database_url()

        assert url.startswith("sqlite:///")
        path = url.removeprefix("sqlite:///")
        assert os.path.isabs(path)

    def test_default_database_url_points_at_backend_dir(self):
        """The default path must resolve to `backend/archimedes_chat.db`."""
        url = db._default_database_url()
        path = url.removeprefix("sqlite:///")

        assert path.endswith("/backend/archimedes_chat.db")
        # Anchored to db.py's parent's parent (backend/), not the caller's CWD.
        assert path == str(db._BACKEND_DIR / "archimedes_chat.db")

    def test_default_database_url_independent_of_cwd(self, tmp_path, monkeypatch):
        """Changing CWD must not change the resolved default path."""
        baseline = db._default_database_url()

        monkeypatch.chdir(tmp_path)
        from_tmp_cwd = db._default_database_url()

        assert from_tmp_cwd == baseline

    def test_module_level_database_url_matches_default_when_unset(self, monkeypatch):
        """DATABASE_URL (module constant) falls back to _default_database_url()
        when the env var is unset — verified by re-deriving via the same
        os.getenv call the module performs at import time."""
        monkeypatch.delenv("DATABASE_URL", raising=False)

        resolved = os.getenv("DATABASE_URL", db._default_database_url())

        assert resolved == db._default_database_url()
        assert os.path.isabs(resolved.removeprefix("sqlite:///"))


class TestDatabaseUrlEnvOverride:
    def test_get_engine_kwargs_sqlite_vs_postgres(self, monkeypatch):
        """_get_engine_kwargs branches on the DATABASE_URL prefix — verify both
        branches independent of the module-level constant's current value."""
        monkeypatch.setattr(db, "DATABASE_URL", "sqlite:////tmp/whatever.db")
        sqlite_kwargs = db._get_engine_kwargs()
        assert sqlite_kwargs == {"connect_args": {"check_same_thread": False}}

        monkeypatch.setattr(db, "DATABASE_URL", "postgresql://user:pass@host:5432/db")
        postgres_kwargs = db._get_engine_kwargs()
        assert postgres_kwargs == {"pool_pre_ping": True, "pool_size": 5, "max_overflow": 10}


class TestBarePostgresUrlResolvesToPsycopg2:
    """SQLAlchemy 2.1.0 changed the DEFAULT driver a bare ``postgresql://`` URL
    resolves to — from ``psycopg2`` to ``psycopg`` (v3), which this repo does not
    install (see the pin comment on ``sqlalchemy`` in requirements-base.txt).
    Every bare-scheme call site — this module's own ``DATABASE_URL``,
    ``migrations/env.py``, and every alembic offline-SQL test — imports the
    driver at ``create_engine()`` time (no live connection needed), so a
    resolution to the wrong dialect raises ``ModuleNotFoundError`` before a
    single query runs. Reproduced directly: an isolated venv with sqlalchemy
    2.1.1 + psycopg2-binary and no ``psycopg`` raises exactly that; the same
    venv with 2.0.52 resolves ``...postgresql.psycopg2`` cleanly.

    This guard does not re-litigate the version pin (that is a text-only
    comparison a future dependabot PR is meant to be able to bump past, with
    review); it pins the OUTCOME the pin protects, so any future upgrade —
    whether or not it changes this exact number — is caught by whether it
    breaks bare-scheme resolution, not by whether it changed a version string.
    """

    def test_bare_postgres_url_resolves_to_psycopg2_dialect(self):
        from sqlalchemy import create_engine

        engine = create_engine("postgresql://user:pass@host:5432/db")
        assert engine.dialect.__class__.__module__ == "sqlalchemy.dialects.postgresql.psycopg2", (
            f"a bare `postgresql://` URL now resolves to {engine.dialect.__class__.__module__!r}, "
            "not the psycopg2 dialect this repo installs — this is the exact SQLAlchemy 2.1 "
            "regression the <2.1 ceiling on requirements-base.txt's sqlalchemy pin exists to "
            "prevent. Either the ceiling was raised without also installing `psycopg` (v3) or "
            "making every DATABASE_URL construction say `postgresql+psycopg2://` explicitly, "
            "or something else changed the default. Do not loosen this assertion — fix the cause."
        )
