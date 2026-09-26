"""The #1632 abort class: concurrent corpus loads racing in session teardown.

Prod rev 214 died (exit 139, tick OFF) with faulthandler showing two executor
threads simultaneously inside ``load_corpus`` → ``load_papers_from_db``, one in
SQLAlchemy ``_detach_states``, one in ``InstanceState._cleanup`` — piled up by
abandoned /health corpus probes on a cold task ("abandoning the call (6 in
flight)"). Two guards keep the class dead:

1. ``load_corpus`` is serialized behind ``_CORPUS_LOAD_LOCK`` — the race is
   unrepresentable for any pair of callers.
2. The /health corpus probe never calls ``load_corpus`` at all — it reads
   ``count_corpus_papers`` (a scalar COUNT with no ORM state to race).

Each guard is exercised against the input that killed prod: real concurrency
for (1), a live /health request for (2).
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import pytest
from archimedes.agents import strategy_fusion

from tests.db_isolation import redirect_to_tmp_sqlite


class TestLoadCorpusIsSerialized:
    def test_concurrent_loaders_never_overlap(self, monkeypatch):
        """Four threads through load_corpus: the DB read runs strictly one-at-a-time."""
        in_flight = 0
        max_in_flight = 0
        gauge = threading.Lock()

        def slow_db_load():
            nonlocal in_flight, max_in_flight
            with gauge:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            time.sleep(0.05)  # wide-open window: unlocked, 4 threads WILL overlap here
            with gauge:
                in_flight -= 1
            return [{"arxiv_id": "2401.00001", "title": "t", "abstract": "a", "published": "2024-01-01"}]

        import archimedes.services.corpus_service as corpus_service

        monkeypatch.setattr(corpus_service, "load_papers_from_db", slow_db_load)

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: strategy_fusion.load_corpus(), range(4)))

        assert all(len(r) == 1 for r in results)
        # THE assertion: with _CORPUS_LOAD_LOCK removed this reads 2..4 —
        # verified by reverting the lock before this file was pushed.
        assert max_in_flight == 1, (
            f"load_corpus ran {max_in_flight} DB loads concurrently — the #1632 "
            "session-teardown race is representable again"
        )


class TestHealthProbeDoesNotLoadTheCorpus:
    # ASGITransport, NOT TestClient: entering TestClient's context manager runs
    # the app's startup lifespan, whose background corpus seed writes ~18k rows
    # into the shared test DB and poisons every empty-DB test that runs after
    # this one in the same process. Precedent: test_health_always_answers.py's
    # _get_health, which exists for exactly this reason.
    async def test_health_reports_the_count_and_never_calls_load_corpus(self, monkeypatch):
        import archimedes.services.corpus_service as corpus_service
        from archimedes.main import app
        from archimedes.services.health_cache import health_probe_cache
        from httpx import ASGITransport, AsyncClient

        def _forbidden(*_a, **_k):  # pragma: no cover - the guard IS the failure
            raise AssertionError(
                "/health called load_corpus — the full-ORM load the #1632 "
                "abort piled up. The probe must read count_corpus_papers."
            )

        monkeypatch.setattr(strategy_fusion, "load_corpus", _forbidden)
        monkeypatch.setattr(corpus_service, "count_corpus_papers", lambda **_k: 1234)

        health_probe_cache.clear()
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get("/health")
            assert resp.status_code == 200
            assert resp.json()["corpus_papers"] == 1234
        finally:
            health_probe_cache.clear()


class TestCountMatchesTheEmbargoRule:
    """``count_corpus_papers`` answers what the old probe's ``len(load_corpus())`` did.

    Driven against a real ``papers`` table (tmp-file sqlite via
    ``tests/db_isolation``), never a Python list checked against itself: the
    SQL COUNT and ``load_papers_from_db`` — whose rows go through
    ``apply_outcome_embargo`` — must agree on the SAME stored rows, including
    both edges of the embargo boundary.
    """

    @pytest.fixture(autouse=True)
    def _tmp_db(self, tmp_path):
        yield from redirect_to_tmp_sqlite(tmp_path)

    @staticmethod
    def _seed(rows: list[tuple[str, str]]) -> None:
        from archimedes.db import get_session
        from archimedes.models.corpus_store import PaperRecord

        with get_session() as session:
            for arxiv_id, published in rows:
                session.add(PaperRecord(arxiv_id=arxiv_id, title=f"t {arxiv_id}", abstract="a", published=published))
            session.commit()

    def test_sql_count_agrees_with_load_papers_from_db_on_both_embargo_edges(self):
        from archimedes.services.corpus_service import count_corpus_papers, load_papers_from_db

        def day(n: int) -> str:
            return (date.today() - timedelta(days=n)).isoformat()

        self._seed(
            [
                ("old.1", day(40)),
                ("edge.in", day(30)),  # exactly embargo_days old: apply_outcome_embargo KEEPS it (<=)
                ("edge.in.ts", day(30) + "T23:59:59Z"),  # same day, arXiv's timestamp form
                ("edge.out", day(29)),  # one day inside the embargo: dropped
                ("fresh.1", day(10)),
                ("blank", ""),  # no date: dropped, fail closed
            ]
        )

        loaded = load_papers_from_db(embargo_days=30, apply_decay=False)
        assert sorted(p["arxiv_id"] for p in loaded) == ["edge.in", "edge.in.ts", "old.1"]

        # THE assertion: the scalar the probe reports IS the size of the corpus
        # generation would load. Mutations that fail it: `<` for `<=` at the
        # cutoff (reads 1 low on the edge day), dropping the != '' clause
        # (reads 1 high), comparing against today instead of the cutoff.
        assert count_corpus_papers(embargo_days=30) == len(loaded) == 3

    def test_an_unparseable_date_can_only_make_the_count_read_high(self):
        """The documented delta: SQL cannot parse, so garbage that sorts as old is counted.

        ``apply_outcome_embargo`` drops it (fail closed). The honest direction
        for a health number is over-reporting by the unparseable rows — never a
        fabricated low — and this pins that the delta is exactly those rows.
        """
        from archimedes.services.corpus_service import count_corpus_papers, load_papers_from_db

        old = (date.today() - timedelta(days=40)).isoformat()
        self._seed([("old.1", old), ("garbage.old", "1999-99-99"), ("garbage.new", "9999-99-99")])

        loaded = load_papers_from_db(embargo_days=30, apply_decay=False)
        assert [p["arxiv_id"] for p in loaded] == ["old.1"]
        assert count_corpus_papers(embargo_days=30) == len(loaded) + 1  # garbage.old, and only it
