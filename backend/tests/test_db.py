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

        monkeypatch.setattr(db, "DATABASE_URL", "postgresql+psycopg2://user:pass@host:5432/db")
        postgres_kwargs = db._get_engine_kwargs()
        assert postgres_kwargs == {"pool_pre_ping": True, "pool_size": 5, "max_overflow": 10}


class TestBarePostgresUrlResolvesToPsycopg2:
    """SQLAlchemy 2.1.0 changed the DEFAULT driver a bare ``postgresql://`` URL
    resolves to — from ``psycopg2`` to ``psycopg`` (v3), which this repo does not
    install. Every URL source (ECS secret, compose, ``.env``) writes the bare
    scheme, and ``create_engine()`` imports the driver up front (no live
    connection needed), so a resolution to the wrong dialect raises
    ``ModuleNotFoundError`` before a single query runs.

    ``db._pin_psycopg2_driver`` is what keeps that from happening on 2.1+. These
    tests pin the OUTCOME through it, not the library default: on SQLAlchemy 2.1
    a bare URL that skipped the pin resolves to psycopg and fails the first test.
    """

    def test_bare_postgres_url_resolves_to_psycopg2_dialect(self):
        from sqlalchemy import create_engine

        url = db._pin_psycopg2_driver("postgresql://user:pass@host:5432/db")
        engine = create_engine(url)
        assert engine.dialect.__class__.__module__ == "sqlalchemy.dialects.postgresql.psycopg2", (
            f"a bare `postgresql://` URL now resolves to {engine.dialect.__class__.__module__!r}, "
            "not the psycopg2 dialect this repo installs. `db._pin_psycopg2_driver` must spell "
            "out `postgresql+psycopg2://`, or `psycopg` (v3) must be installed. Do not loosen "
            "this assertion — fix the cause."
        )

    def test_pin_rewrites_only_the_bare_scheme(self):
        assert db._pin_psycopg2_driver("postgresql://u:p@h:5432/d") == "postgresql+psycopg2://u:p@h:5432/d"
        # A URL that already names a driver, or is not Postgres, passes through.
        for url in (
            "postgresql+psycopg2://u:p@h/d",
            "postgresql+asyncpg://u:p@h/d",
            "sqlite:///x.db",
        ):
            assert db._pin_psycopg2_driver(url) == url

    def test_pin_is_anchored_to_the_scheme(self):
        # SQLAlchemy's URL parser accepts the scheme text later in the URL, in a
        # password or a query value. Only the leading scheme may be rewritten.
        assert (
            db._pin_psycopg2_driver("postgresql://u:postgresql://@h/d") == "postgresql+psycopg2://u:postgresql://@h/d"
        )
        driver_named = "postgresql+asyncpg://u:p@h/d?application_name=postgresql://x"
        assert db._pin_psycopg2_driver(driver_named) == driver_named

    def test_module_database_url_goes_through_the_pin(self):
        # A subprocess, not importlib.reload: reloading `db` here would rebind
        # the engine every other module already imported.
        import subprocess
        import sys
        from pathlib import Path

        backend_dir = Path(db.__file__).resolve().parent.parent
        # Whitelisted env + neutralized load_dotenv, per docs/testing-conventions.md,
        # so a developer's .env cannot replace the URL under test.
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "PYTHONPATH": str(backend_dir),
            "DATABASE_URL": "postgresql://u:p@h:5432/d",
        }
        script = (
            "import dotenv\n"
            "dotenv.load_dotenv = lambda *a, **k: False\n"
            "from archimedes import db\n"
            "print(db.DATABASE_URL, db.engine.dialect.driver)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=backend_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.split()[-2:] == ["postgresql+psycopg2://u:p@h:5432/d", "psycopg2"]


class TestSqlalchemyFloorExcludesKnownRegressions:
    """2.1.1 and 2.1.2 ship regressions that 2.1.3 fixes: #13619, #13625 and
    #13626 (fixed in 2.1.2) and #13635 (fixed in 2.1.3), per SQLAlchemy's 2.1
    changelog. #13635 makes a ``.params()`` value compile to NULL under
    ``literal_binds``. Nothing in the backend uses the affected features today,
    so the floor in ``requirements-base.txt`` is the only thing keeping a resolver
    from installing those releases. A plain text read, like
    ``test_single_openssl.py``'s requirement guard, so it runs everywhere.
    """

    def test_requirements_floor_is_at_least_2_1_3(self):
        import re
        from pathlib import Path

        requirements = Path(__file__).resolve().parents[1] / "requirements-base.txt"
        specs = []
        for line in requirements.read_text().splitlines():
            body = line.split("#", 1)[0].strip()
            match = re.match(r"(?i)^sqlalchemy(?:\[[^\]]*\])?\s*(?P<spec>[<>=!~].*)?$", body)
            if match:
                specs.append(match.group("spec") or "")
        assert len(specs) == 1, f"expected one sqlalchemy line in {requirements.name}, found {specs}"
        floor = re.search(r">=\s*(\d+)\.(\d+)\.(\d+)\b", specs[0])
        assert floor, f"the sqlalchemy line has no >=X.Y.Z floor: {specs[0]!r}"
        assert tuple(int(part) for part in floor.groups()) >= (2, 1, 3), (
            f"sqlalchemy floor {specs[0]!r} admits 2.1.1/2.1.2, which carry regressions 2.1.3 fixes"
        )
