"""Tests for archimedes.scripts.alembic_migrate_preflight (issue #1039 B3).

Two tiers:

- ``TestNeedsBaselineStamp`` — hermetic, sqlite-backed unit tests for the
  "does this DB need a baseline stamp" decision. No alembic import, no
  Postgres, no network; runs in the default (non-integration) suite.
- ``test_stamp_and_upgrade_on_already_populated_postgres_db`` — the real
  end-to-end scenario the issue asks for: build the pre-Alembic schema via
  ``create_all()``, run the pre-flight, assert it stamps + upgrades cleanly
  against a live Postgres. Marked ``@pytest.mark.integration`` (needs a
  seeded/live service — pytest.ini's own marker description) and skips
  cleanly, not errors, when either of two forward-compat gaps apply on this
  branch today:

  1. ``alembic`` isn't a declared dependency here (dropped as unused in PR
     #354, 2026-05-25; re-added by issue #1028, not yet merged) — CI's unit
     job installs only from ``backend/requirements.txt``, which doesn't list
     it on this branch, so a hard import would error instead of skip there.
  2. ``backend/alembic.ini`` / ``backend/migrations/versions/`` don't exist
     on this branch yet (issue #1028, PR #1042, not yet merged) — same
     self-activation contract as ``.github/workflows/deploy.yml``'s
     ``migrate`` job and ``infra/ecs_migrate.tf``'s header comment: this test
     starts actually running the moment that PR lands, no further change
     needed here.

  Needs a reachable Postgres via ``DATABASE_URL`` (or ``TEST_DATABASE_URL``)
  — e.g. ``docker compose --profile localdb up -d postgres`` locally, or a
  Postgres service container in CI/deploy. Skips (does not fail) if neither
  is set or the connection fails, per the marker's own "run in CI/deploy, not
  the default unit run" contract.

  Runs ``stamp_and_upgrade()`` as a SUBPROCESS
  (``python -m archimedes.scripts.alembic_migrate_preflight``), not an
  in-process call — ``archimedes.db.engine`` / the Alembic ``env.py`` (PR
  #1042) both read ``DATABASE_URL`` at IMPORT time, which may already have
  happened earlier in this test session against a different URL; a fresh
  subprocess with a clean, ``DATABASE_URL``-only env (mirroring
  ``test_alembic_migrations.py``'s own ``_run_alembic`` helper and this
  repo's ``_clean_subprocess_env`` convention — see
  ``test_security_hardening.py``) is also exactly how the real ECS migrate
  task invokes this script, so it's the more faithful test shape too.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa

_BACKEND_DIR = Path(__file__).resolve().parents[2]


def _clean_subprocess_env(database_url: str) -> dict[str, str]:
    """Whitelist-only env — no ambient .env leakage. Mirrors
    test_alembic_migrations.py's own helper (PR #1042) and
    test_security_hardening.py's _clean_subprocess_env docstring rationale."""
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "DATABASE_URL": database_url,
        "PYTHONPATH": str(_BACKEND_DIR),
    }


class TestNeedsBaselineStamp:
    """Hermetic sqlite-backed coverage of the stamp/no-stamp decision."""

    def test_fresh_db_does_not_need_a_stamp(self):
        """A genuinely empty DB (no alembic_version, no baseline sentinel) —
        the real "fresh CI / new dev clone" path — must NOT be stamped; a
        real alembic upgrade head should run every revision including the
        baseline."""
        from archimedes.scripts.alembic_migrate_preflight import _needs_baseline_stamp

        engine = sa.create_engine("sqlite:///:memory:")
        assert _needs_baseline_stamp(engine) is False

    def test_already_populated_pre_alembic_db_needs_a_stamp(self):
        """The exact B3 scenario: backtest_results exists (pre-Alembic
        create_all()-built schema) but alembic_version does not — this DB
        must be stamped before upgrade head, or upgrade head will try to
        CREATE backtest_results again and fail."""
        from archimedes.scripts.alembic_migrate_preflight import (
            BASELINE_SENTINEL_TABLE,
            _needs_baseline_stamp,
        )

        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(sa.text(f"CREATE TABLE {BASELINE_SENTINEL_TABLE} (id INTEGER PRIMARY KEY)"))

        assert _needs_baseline_stamp(engine) is True

    def test_already_stamped_db_does_not_need_a_stamp_again(self):
        """Once alembic_version exists (this task has already run once
        against this DB), the stamp step must be skipped on every later
        run — idempotency is the whole point of a pre-rollout task that
        runs on every deploy."""
        from archimedes.scripts.alembic_migrate_preflight import (
            BASELINE_SENTINEL_TABLE,
            _needs_baseline_stamp,
        )

        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(sa.text(f"CREATE TABLE {BASELINE_SENTINEL_TABLE} (id INTEGER PRIMARY KEY)"))
            conn.execute(sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"))

        assert _needs_baseline_stamp(engine) is False


def _postgres_test_url() -> str | None:
    url = os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL")
    if url and url.startswith("postgresql"):
        from archimedes.db import _pin_psycopg2_driver

        return _pin_psycopg2_driver(url)
    return None


def test_postgres_test_url_pins_the_psycopg2_driver(monkeypatch):
    """SQLAlchemy 2.1 resolves a bare ``postgresql://`` to psycopg (v3), which is
    not installed. Unpinned, ``sa.create_engine`` in the integration test below
    raises ModuleNotFoundError before its unreachable-DB skip can run."""
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/d")
    url = _postgres_test_url()
    assert url == "postgresql+psycopg2://u:p@h:5432/d"
    engine = sa.create_engine(url)
    try:
        assert engine.dialect.driver == "psycopg2"
    finally:
        engine.dispose()

    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://t:p@h:5432/d")
    assert _postgres_test_url() == "postgresql+psycopg2://t:p@h:5432/d"


@pytest.mark.integration
def test_stamp_and_upgrade_on_already_populated_postgres_db():
    """End-to-end: create_all() (the pre-Alembic path) → stamp_and_upgrade()
    → assert the DB ends up on the real Alembic head with no error, exactly
    the prod-Aurora scenario B3 exists to make safe."""
    pytest.importorskip("alembic", reason="alembic not installed on this branch yet (issue #1028, PR #1042)")

    alembic_ini = _BACKEND_DIR / "alembic.ini"
    if not alembic_ini.exists():
        pytest.skip(
            "backend/alembic.ini not present on this branch yet (issue #1028, PR #1042) — self-activates once it lands"
        )

    database_url = _postgres_test_url()
    if not database_url:
        pytest.skip(
            "no Postgres DATABASE_URL/TEST_DATABASE_URL set — set one (e.g. via `docker compose --profile localdb up -d postgres`) to run this integration test"
        )

    import archimedes.db as db_module
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from archimedes.scripts.alembic_migrate_preflight import BASELINE_REVISION

    engine = sa.create_engine(database_url)
    try:
        with engine.connect():
            pass
    except Exception as exc:
        pytest.skip(f"Postgres at {database_url!r} not reachable: {exc}")

    # Start from a clean slate so this test is re-runnable against a
    # persistent local Postgres, not just a throwaway CI service container.
    with engine.begin() as conn:
        conn.execute(sa.text("DROP SCHEMA public CASCADE"))
        conn.execute(sa.text("CREATE SCHEMA public"))

    # Build the PRE-ALEMBIC schema exactly the way prod Aurora got its schema
    # — archimedes.db.init_db()'s create_all() path — deliberately NOT via
    # `alembic upgrade head`, which is the thing under test. Uses an
    # explicit `bind=engine`, not the module-level `db_module.engine` (which
    # is frozen at whatever DATABASE_URL was live when `archimedes.db` was
    # FIRST imported in this test session, not necessarily this test's URL).
    db_module.Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        inspector = sa.inspect(conn)
        assert inspector.has_table("backtest_results"), "create_all() must produce the baseline sentinel table"
        assert not inspector.has_table("alembic_version"), "a create_all()-only DB must have no alembic_version table"

    # The system under test — as a subprocess (see module docstring for why):
    # `python -m archimedes.scripts.alembic_migrate_preflight`, the EXACT
    # command the real ECS migrate task runs.
    result = subprocess.run(
        [sys.executable, "-m", "archimedes.scripts.alembic_migrate_preflight"],
        cwd=str(_BACKEND_DIR),
        env=_clean_subprocess_env(database_url),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"stamp_and_upgrade failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    assert "MIGRATE_PREFLIGHT_STAMP" in result.stderr, (
        f"expected the pre-flight to log that it stamped the baseline (alembic_version was absent, "
        f"backtest_results existed) — got:\n{result.stderr}"
    )

    cfg = Config(str(alembic_ini))
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1

    with engine.connect() as conn:
        row = conn.execute(sa.text("SELECT version_num FROM alembic_version")).fetchone()
    assert row is not None, "alembic_version must be populated after stamp_and_upgrade()"
    assert row[0] == heads[0], f"expected DB to be at head {heads[0]!r}, got {row[0]!r}"
    # The baseline revision itself must be part of the applied history — not
    # just the newest revision — confirming the stamp actually anchored the
    # chain at af9c6a9376e4 rather than upgrade head silently starting fresh.
    assert BASELINE_REVISION in {rev.revision for rev in script.walk_revisions(heads[0])}

    with engine.begin() as conn:
        conn.execute(sa.text("DROP SCHEMA public CASCADE"))
        conn.execute(sa.text("CREATE SCHEMA public"))
