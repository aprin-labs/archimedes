"""#1908 — clearing the OAuth ID tokens already stored in ``auth_accounts``.

Exercises the REAL alembic revision (``7d2f9a4c1e60``) as a subprocess against
a throwaway SQLite file, with a whitelist-only environment, the same hermetic
pattern ``test_orphan_row_adoption.py`` uses: a developer's ``.env`` must not
be able to point this at a docker-compose Postgres or at production.

Three behaviours, one test each:

  1. upgrade clears every stored ``idToken``, whatever the provider, and
     writes nothing else;
  2. a second upgrade is a no-op;
  3. downgrade succeeds, restores nothing, and leaves the schema as it was.

The new-row half of the fix (the auth service never writing the column) is
pinned by ``auth/test/auth.test.js``'s "#1908" tests.
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
#: Named explicitly rather than "head" — see the same constant's comment in
#: test_orphan_row_adoption.py for why that is load-bearing once another
#: revision chains behind this one.
_REVISION = "7d2f9a4c1e60"
_NOW = datetime(2026, 10, 1, tzinfo=UTC)


def _b64url(value: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()


#: The shape Google issues: a JWT whose payload carries email, name and
#: picture. Built at runtime (unsigned) rather than pasted as a literal.
_GOOGLE_ID_TOKEN = ".".join(
    [
        _b64url({"alg": "RS256", "typ": "JWT"}),
        _b64url({"sub": "100", "email": "ada@example.com", "name": "Ada", "picture": "https://example.com/a.png"}),
        "sig",
    ]
)

#: A non-Google row holding an ID token. GitHub's OAuth flow issues none, so
#: this is not a real GitHub value; it stands in for any non-Google provider.
#: The revision clears the column on every row, not just Google's. With this
#: row seeded, a revision that scoped its UPDATE to ``providerId = 'google'``
#: leaves a token behind and fails the clear test.
_NON_GOOGLE_ID_TOKEN = "non-google-provider-id-token"

_ACCOUNTS = [
    # (id, providerId, accountId, userId, accessToken, refreshToken, idToken, scope, password)
    ("acc-google-1", "google", "100", "user-1", "enc:access-1", "enc:rt-1", _GOOGLE_ID_TOKEN, "openid", None),
    ("acc-google-2", "google", "200", "user-2", "enc:access-2", None, _GOOGLE_ID_TOKEN + "2", "openid", None),
    ("acc-google-3", "google", "300", "user-3", "enc:access-3", None, None, "openid", None),
    ("acc-github-1", "github", "400", "user-1", "enc:access-4", None, None, "read:user,user:email", None),
    ("acc-github-2", "github", "500", "user-3", "enc:access-5", None, _NON_GOOGLE_ID_TOKEN, "read:user", None),
    ("acc-cred-1", "credential", "user-2", "user-2", None, None, None, None, "scrypt:hash"),
]
_OTHER_COLUMNS = (
    '"id", "providerId", "accountId", "userId", "accessToken", "refreshToken", '
    '"scope", "password", "accessTokenExpiresAt", "refreshTokenExpiresAt", "createdAt", "updatedAt"'
)


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


def _down_revision() -> str:
    # A literal, not ScriptDirectory.get_revision(_REVISION).down_revision:
    # it must equal the revision file's own down_revision (asserted below),
    # and a literal keeps the fixture buildable on a checkout where the
    # revision under test does not exist yet.
    return "c8a4d1f70b93"


def test_down_revision_literal_matches_the_revision_file():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(_BACKEND_DIR / "alembic.ini")))
    assert script.get_revision(_REVISION).down_revision == _down_revision()


def _seed(db_path: Path) -> None:
    import sqlalchemy as sa

    engine = sa.create_engine(f"sqlite:///{db_path}")
    meta = sa.MetaData()
    meta.reflect(bind=engine)
    t = meta.tables
    with engine.begin() as conn:
        conn.execute(
            t["auth_users"].insert(),
            [
                {
                    "id": f"user-{n}",
                    "name": f"User {n}",
                    "email": f"user{n}@example.com",
                    "emailVerified": True,
                    "createdAt": _NOW,
                    "updatedAt": _NOW,
                }
                for n in (1, 2, 3)
            ],
        )
        conn.execute(
            t["auth_accounts"].insert(),
            [
                {
                    "id": row[0],
                    "providerId": row[1],
                    "accountId": row[2],
                    "userId": row[3],
                    "accessToken": row[4],
                    "refreshToken": row[5],
                    "idToken": row[6],
                    "scope": row[7],
                    "password": row[8],
                    "accessTokenExpiresAt": _NOW if row[4] else None,
                    "refreshTokenExpiresAt": None,
                    "createdAt": _NOW,
                    "updatedAt": _NOW,
                }
                for row in _ACCOUNTS
            ],
        )
    engine.dispose()


def _rows(db_path: Path, sql: str) -> list[tuple]:
    con = sqlite3.connect(str(db_path))
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _id_tokens(db_path: Path) -> list[tuple]:
    return _rows(db_path, 'SELECT "id", "idToken" FROM auth_accounts ORDER BY "id"')


def _other_columns(db_path: Path) -> list[tuple]:
    return _rows(db_path, f'SELECT {_OTHER_COLUMNS} FROM auth_accounts ORDER BY "id"')


def _prepared(tmp_path: Path, name: str) -> tuple[Path, str]:
    db_path = tmp_path / name
    url = f"sqlite:///{db_path}"
    pre = _run_alembic("upgrade", _down_revision(), database_url=url)
    assert pre.returncode == 0, pre.stderr
    _seed(db_path)
    seeded = _rows(db_path, 'SELECT "providerId" FROM auth_accounts WHERE "idToken" IS NOT NULL ORDER BY "id"')
    # Two Google tokens and one non-Google token, so a Google-only clear fails.
    assert seeded == [("github",), ("google",), ("google",)]
    return db_path, url


def test_upgrade_clears_every_stored_id_token_and_nothing_else(tmp_path):
    db_path, url = _prepared(tmp_path, "clear.db")
    others_before = _other_columns(db_path)

    result = _run_alembic("upgrade", _REVISION, database_url=url)
    assert result.returncode == 0, f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    assert _id_tokens(db_path) == [(row_id, None) for row_id, *_ in sorted(_ACCOUNTS)]
    assert "#1908 cleared 3 stored OAuth ID token(s)" in result.stdout + result.stderr

    assert _other_columns(db_path) == others_before
    assert _rows(db_path, "SELECT COUNT(*) FROM auth_users") == [(3,)]


def test_running_the_upgrade_a_second_time_is_a_no_op(tmp_path):
    db_path, url = _prepared(tmp_path, "idempotent.db")

    first = _run_alembic("upgrade", _REVISION, database_url=url)
    assert first.returncode == 0, first.stderr
    before = _rows(db_path, 'SELECT * FROM auth_accounts ORDER BY "id"')

    # `stamp` rewinds the version pointer WITHOUT running downgrade(), so the
    # next upgrade re-executes upgrade() over already-cleared data.
    stamp = _run_alembic("stamp", _down_revision(), database_url=url)
    assert stamp.returncode == 0, stamp.stderr
    second = _run_alembic("upgrade", _REVISION, database_url=url)
    assert second.returncode == 0, f"STDOUT:\n{second.stdout}\nSTDERR:\n{second.stderr}"
    assert "#1908 cleared 0 stored OAuth ID token(s)" in second.stdout + second.stderr

    assert _rows(db_path, 'SELECT * FROM auth_accounts ORDER BY "id"') == before


def test_downgrade_restores_nothing_and_keeps_the_schema(tmp_path):
    db_path, url = _prepared(tmp_path, "downgrade.db")
    columns_before = _rows(db_path, "PRAGMA table_info(auth_accounts)")

    up = _run_alembic("upgrade", _REVISION, database_url=url)
    assert up.returncode == 0, up.stderr
    after_upgrade = _rows(db_path, 'SELECT * FROM auth_accounts ORDER BY "id"')

    down = _run_alembic("downgrade", _down_revision(), database_url=url)
    assert down.returncode == 0, f"STDOUT:\n{down.stdout}\nSTDERR:\n{down.stderr}"
    assert _rows(db_path, "SELECT version_num FROM alembic_version") == [(_down_revision(),)]
    assert _rows(db_path, "PRAGMA table_info(auth_accounts)") == columns_before
    assert _rows(db_path, 'SELECT * FROM auth_accounts ORDER BY "id"') == after_upgrade

    again = _run_alembic("upgrade", _REVISION, database_url=url)
    assert again.returncode == 0, again.stderr
