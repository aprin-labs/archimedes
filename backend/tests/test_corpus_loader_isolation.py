"""Issue #1640 — the corpus loader must not read ambient state.

**The report.** ``pytest backend/tests/test_strategy_fusion.py -q`` passes 41 in a
fresh worktree and fails 6 — headlined ``assert 10000 == 4`` — in a worktree that
has been used, on identical code. Önder's reading was "something about a used
working tree makes the loader ignore ``ARCHIMEDES_CORPUS_MANIFEST``".

**The root cause.** ``ARCHIMEDES_CORPUS_MANIFEST`` was never being ignored: the
loader never reached it. ``strategy_fusion.load_corpus()`` consulted the
``papers`` table *before* any file, and discarded its own ``path`` argument when
that table had rows. The table in question lives in ``archimedes.db``'s
unset-``DATABASE_URL`` default — ``backend/archimedes_chat.db``, an untracked
SQLite file **inside the working tree**, created by ``init_db()`` at
``archimedes.main`` import time and filled with all 10,000 manifest rows by the
FastAPI lifespan's ``seed_from_manifest()`` step. Boot the app once in a
directory (``uvicorn archimedes.main:app``, a ``scripts/`` run, an interrupted
suite) and every later ``pytest`` run in that directory reads the real 10K corpus
where the fixtures asked for 4 — permanently, until someone deletes a file they
have no reason to know exists. Reproduced exactly: seeding that file turns 41
passed into 6 failed / 35 passed, plus 5 in ``test_debate_engine.py`` and 1 in
``test_papers_routes.py``. ``test_corpus_embedding_claims.py``'s module docstring
records an earlier, narrower encounter with the same leak.

**The two fixes these tests pin.**

1. ``backend/tests/conftest.py`` points the unset-``DATABASE_URL`` default at a
   throwaway temp file, so the suite starts from the fresh-worktree state by
   construction and can never read the developer's in-tree dev database.
2. ``load_corpus(path)`` treats an explicit ``path`` as authoritative and does
   not consult the DB at all — a caller who names a manifest is answering "which
   papers", not offering a hint.

Hermetic: temp SQLite via ``tests/db_isolation.py``, temp manifests, no network.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

import pytest
from archimedes import db as archimedes_db
from archimedes.agents.strategy_fusion import load_corpus
from archimedes.models.corpus_store import PaperRecord

from tests.db_isolation import isolated_empty_sqlite, redirect_to_tmp_sqlite

# Old enough to clear the 30-day Outcome Embargo in load_papers_from_db().
_OLD = "2024-01-01"


@contextmanager
def _tmp_db(tmp_path: Path):
    """``redirect_to_tmp_sqlite`` as a context manager — it is written to be
    consumed with ``yield from`` from a fixture, and this file needs it both in
    a fixture and inline inside one test."""
    yield from redirect_to_tmp_sqlite(tmp_path)


def _db_row(arxiv_id: str, title: str) -> PaperRecord:
    return PaperRecord(
        arxiv_id=arxiv_id,
        title=title,
        abstract=f"abstract for {arxiv_id}",
        primary_category="q-fin.PM",
        categories='["q-fin.PM"]',
        published=_OLD,
        updated=_OLD,
        source="seed",
    )


def _write_manifest(path: Path, ids: list[str]) -> Path:
    path.write_text(
        "\n".join(
            json.dumps(
                {
                    "arxiv_id": i,
                    "title": f"file paper {i}",
                    "abstract": f"abstract for {i}",
                    "primary_category": "q-fin.PM",
                    "categories": ["q-fin.PM"],
                    "published": _OLD,
                }
            )
            for i in ids
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def populated_db(tmp_path):
    """A temp SQLite bound to ``archimedes.db``'s globals, with papers in it.

    Stands in for the poisoned ``backend/archimedes_chat.db``: the loader's DB
    branch is live and returns rows, exactly as in a used worktree.
    """
    with _tmp_db(tmp_path):
        with archimedes_db.get_session() as session:
            session.add(_db_row("9001.00001", "DB paper one"))
            session.add(_db_row("9001.00002", "DB paper two"))
            session.add(_db_row("9001.00003", "DB paper three"))
            session.commit()
        yield


# ── Fix 1: the suite never reads the in-tree developer database ──────────────


class TestTheSuiteDatabaseIsNotInTheWorkingTree:
    """Reverting the conftest block fails both tests here.

    Without it ``DATABASE_URL`` is unset, ``archimedes.db`` resolves
    ``_default_database_url()``, and the engine binds
    ``backend/archimedes_chat.db`` inside the repo — the file whose contents
    made the same code pass 41 in one directory and fail 6 in another.
    """

    def test_database_url_is_pinned_away_from_the_in_tree_default(self):
        """Deliberately string-level and backend-agnostic. A teammate running
        the suite with ``DATABASE_URL`` exported to Postgres must not see this
        guard misfire — CI green / local red is itself a bug (CLAUDE.md)."""
        url = os.environ.get("DATABASE_URL")
        assert url, "conftest must pin DATABASE_URL so the in-tree default can never be reached"
        assert url != archimedes_db._default_database_url()
        repo_root = str(archimedes_db._BACKEND_DIR.parent.resolve())
        assert repo_root not in url, f"the suite's database sits inside the checkout: {url}"

    def test_the_live_engine_is_not_bound_to_the_in_tree_sqlite_file(self):
        """Assert on the engine, not only the env var: ``archimedes.db`` reads
        ``DATABASE_URL`` once at import, so the engine is what actually decides
        which file the loader's DB branch reads.

        Compared as an exact string against the one path that carries the bug.
        ``Path(...).resolve()`` would be wrong here: on a Postgres URL
        ``url.database`` is a bare database *name*, and resolving it against the
        CWD would manufacture a repo-relative path and fail for no reason."""
        in_tree_default = archimedes_db._BACKEND_DIR / "archimedes_chat.db"
        assert archimedes_db.engine.url.database != str(in_tree_default)


# ── Fix 2: an explicit manifest path outranks whatever the DB holds ──────────


class TestExplicitPathBeatsAmbientDatabaseState:
    """Reverting the ``if path is not None`` early return in ``load_corpus``
    fails both tests here: each returns the three DB rows instead."""

    def test_explicit_manifest_path_is_not_overridden_by_a_populated_db(self, populated_db, tmp_path):
        manifest = _write_manifest(tmp_path / "manifest.jsonl", ["1111.11111", "2222.22222"])

        corpus = load_corpus(manifest)

        assert [p.arxiv_id for p in corpus] == ["1111.11111", "2222.22222"]
        assert not any(p.arxiv_id.startswith("9001.") for p in corpus)

    def test_explicit_missing_path_yields_empty_not_the_database(self, populated_db, tmp_path):
        """The ``test_loader_no_file_returns_empty`` shape. "That file, which is
        absent" must mean an empty corpus, not a silent substitution — a caller
        that asked for a specific manifest and got 10,000 unrelated papers is
        worse off than one that got nothing."""
        assert load_corpus(tmp_path / "does_not_exist.jsonl") == []


class TestNoPathStillMeansDatabaseFirst:
    """Characterisation, not a guard — these pass with the fix reverted too.

    They are here so the *production* precedence is written down next to the
    change that narrowed it: every production caller (`main.py`,
    `papers_routes`, `strategies_routes`, `debate_engine`, `StrategyFusion`)
    calls ``load_corpus()`` with no argument, and must keep reading the DB,
    which is the source of record post-#1240. Only tests pass a path, so fix 2
    changes no production behaviour.
    """

    def test_no_argument_reads_the_db(self, populated_db):
        corpus = load_corpus()
        assert {p.arxiv_id for p in corpus} == {"9001.00001", "9001.00002", "9001.00003"}

    def test_env_manifest_does_not_bypass_a_populated_db(self, populated_db, tmp_path, monkeypatch):
        """``ARCHIMEDES_CORPUS_MANIFEST`` names where the *file fallback* reads
        from; it is not a DB bypass. Production sets it (``infra/ecs.tf``,
        ``docker-compose.yml``) while still wanting the DB, so promoting it
        above the DB would silently drop every arXiv-intake paper and the
        embargo/decay filtering in prod. #1640's title reads it the other way
        round; this test records which reading is load-bearing."""
        manifest = _write_manifest(tmp_path / "env-manifest.jsonl", ["3333.33333"])
        monkeypatch.setenv("ARCHIMEDES_CORPUS_MANIFEST", str(manifest))

        assert {p.arxiv_id for p in load_corpus()} == {"9001.00001", "9001.00002", "9001.00003"}

    def test_env_manifest_is_used_once_the_db_is_empty(self, tmp_path, monkeypatch):
        """The other half: with no DB rows the env override does resolve — which
        is what ``test_strategy_fusion.py::test_loader_env_override`` asserts,
        and what it could not do while the in-tree database held 10,000 rows."""
        manifest = _write_manifest(tmp_path / "env-manifest.jsonl", ["4444.44444"])
        monkeypatch.setenv("ARCHIMEDES_CORPUS_MANIFEST", str(manifest))

        with _tmp_db(tmp_path):  # empty papers table
            assert [p.arxiv_id for p in load_corpus()] == ["4444.44444"]


class TestFileFallbackSurvivesASeededSharedDatabase:
    """The 18752-vs-4 / 18752-vs-0 Quality Gate leak.

    TestClient / ASGI lifespan seeds the process-global suite DB with the
    full manifest. ``load_corpus()`` with ``path=None`` then prefers that DB
    over ``ARCHIMEDES_CORPUS_MANIFEST`` — which is correct in production
    (issue #1640: the env var is the file-fallback location, not a DB
    bypass) and wrong for the two tests that need an empty papers table to
    measure the file path.

    This plants a distinctive corpus on the *shared* engine (the lifespan
    seed, in miniature), then runs those two shapes behind
    ``isolated_empty_sqlite``. Drop the redirect and both fail with
    ``len == N_PLANTED`` instead of 4 / 0. Cleanup is mandatory: planted
    rows must not outlive the test and poison later files in the process.
    """

    N_PLANTED = 17  # not 4, not 0, not the real ~18k
    _ID_PREFIX = "seedleak."

    def _ids(self) -> list[str]:
        return [f"{self._ID_PREFIX}{i:05d}" for i in range(self.N_PLANTED)]

    def _plant_on_shared_engine(self) -> None:
        archimedes_db.Base.metadata.create_all(bind=archimedes_db.engine)
        with archimedes_db.get_session() as session:
            for arxiv_id in self._ids():
                session.add(_db_row(arxiv_id, f"leaked {arxiv_id}"))
            session.commit()

    def _unplant_from_shared_engine(self) -> None:
        with archimedes_db.get_session() as session:
            session.query(PaperRecord).filter(PaperRecord.arxiv_id.in_(self._ids())).delete(synchronize_session=False)
            session.commit()

    def test_loader_env_override_still_sees_four_after_the_shared_db_is_seeded(self, tmp_path, monkeypatch):
        """The ``test_strategy_fusion.py::test_loader_env_override`` shape."""
        four = _write_manifest(
            tmp_path / "four.jsonl",
            ["2401.00001", "2402.00002", "2403.00003", "2404.00004"],
        )
        monkeypatch.setenv("ARCHIMEDES_CORPUS_MANIFEST", str(four))
        planted_ids = set(self._ids())
        self._plant_on_shared_engine()
        try:
            # The leak is live: path=None prefers the shared DB (which may
            # already hold a sibling lifespan's ~18k rows). If our planted
            # ids are missing, isolation below is vacuous.
            leaked = {p.arxiv_id for p in load_corpus()}
            assert planted_ids <= leaked, "shared-DB plant did not occupy load_corpus()"
            with isolated_empty_sqlite(tmp_path):
                isolated = load_corpus()
            assert len(isolated) == 4
            assert not planted_ids & {p.arxiv_id for p in isolated}
        finally:
            self._unplant_from_shared_engine()

    def test_empty_db_catalog_fallback_still_sees_zero_after_the_shared_db_is_seeded(self, tmp_path, monkeypatch):
        """The ``test_papers_routes.py::test_empty_db_falls_back_to_file_manifest`` shape."""
        import asyncio

        from archimedes.api import papers_routes
        from archimedes.models.chat import Base
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        monkeypatch.setenv("ARCHIMEDES_CORPUS_MANIFEST", str(tmp_path / "does-not-exist.jsonl"))
        empty_engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=empty_engine)
        empty_session = sessionmaker(bind=empty_engine)()

        class _Ctx:
            def __enter__(self):
                return empty_session

            def __exit__(self, *args):
                pass

        monkeypatch.setattr(papers_routes, "get_session", lambda: _Ctx())
        self._plant_on_shared_engine()
        try:
            leaked = asyncio.run(
                papers_routes.list_papers(page=1, page_size=20, category=None, search=None, processed_only=True)
            )
            # Catalog's own session is empty; a non-zero total is load_corpus()
            # reading the shared engine. May be N_PLANTED or N_PLANTED+~18k.
            assert leaked["total"] >= self.N_PLANTED
            with isolated_empty_sqlite(tmp_path):
                isolated = asyncio.run(
                    papers_routes.list_papers(page=1, page_size=20, category=None, search=None, processed_only=True)
                )
            assert isolated["total"] == 0
            assert isolated["papers"] == []
        finally:
            empty_session.close()
            empty_engine.dispose()
            self._unplant_from_shared_engine()
