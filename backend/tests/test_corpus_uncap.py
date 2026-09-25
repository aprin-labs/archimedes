"""#1635 — the seed manifest is uncapped, the seed terms are canonical, and
both growth paths can actually grow.

Four defects are guarded here, each with its own class:

1. ``scripts/bulk_ingest_arxiv.py`` had ``--max`` defaulted to ``10000`` — a
   hard-coded harvest ceiling, not the size of the responsive literature. It
   also stopped at the first page containing no new papers, which on a
   *resumed* run terminates thousands of papers before the older tail (measured
   2026-08-31: 10,674 of 18,907 available).
2. Two divergent ``QFIN_CATEGORIES`` literals existed, one of them carrying the
   retired ``q-fin.EC`` (aliased to ``econ.GN``; returns 0 results).
3. The harvester wrote a 10-key row while ``data/corpus/README.md`` calls a
   13-key schema frozen, so 8,000 of 10,000 committed rows were missing
   ``pdf_path`` / ``text_path`` / ``fetched_at``.
4. ``corpus_service.intake_from_arxiv`` built its URL with no ``start=``
   parameter, so it re-requested the same newest page forever and could not
   grow the corpus at all.

Hermetic: the arXiv HTTP boundary is faked (``httpx.get`` / ``fetch_batch``),
the DB is in-memory SQLite, and every polite-delay sleep is stubbed. No
network, no Postgres, no Redis.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
from archimedes.models.chat import Base
from archimedes.models.corpus_store import PaperRecord
from archimedes.services import corpus_categories
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BULK_SCRIPT = _REPO_ROOT / "scripts" / "bulk_ingest_arxiv.py"
_MANIFEST = _REPO_ROOT / "data" / "corpus" / "manifest.jsonl"

# The frozen manifest schema, per data/corpus/README.md.
FROZEN_KEYS = {
    "arxiv_id",
    "title",
    "authors",
    "primary_category",
    "categories",
    "published",
    "updated",
    "abstract",
    "pdf_url",
    "pdf_sha256",
    "pdf_path",
    "text_path",
    "fetched_at",
}


def _load_bulk_module():
    """Load the repo-root harvester by path (``scripts/`` is not a package).

    Same pattern as ``test_import_daily_returns.py`` / ``test_strategy_ownership.py``.
    """
    spec = importlib.util.spec_from_file_location("bulk_ingest_arxiv", _BULK_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def bulk(monkeypatch):
    mod = _load_bulk_module()
    monkeypatch.setattr(mod, "POLITE_DELAY", 0)
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    return mod


def _fake_paper(idx: int) -> dict:
    """One paper as ``fetch_batch`` returns it — the 10-key pre-#1635 shape."""
    return {
        "arxiv_id": f"2600.{idx:05d}",
        "title": f"Paper {idx}",
        "authors": ["Ada Lovelace"],
        "abstract": f"Abstract {idx}",
        "primary_category": "q-fin.PM",
        "categories": ["q-fin.PM"],
        # Descending published date so the feed order matches submittedDate desc.
        "published": f"2026-01-01T{idx:05d}",
        "updated": "2026-01-01",
        "pdf_url": f"https://arxiv.org/pdf/2600.{idx:05d}v1",
        "pdf_sha256": None,
    }


def _paged_feed(total: int, *, calls: list | None = None, categories: tuple[str, ...] | None = None):
    """A ``fetch_batch`` stand-in serving ``total`` papers across the categories.

    The real harvester issues one paged query per category and unions the
    results, so the fake shards the corpus round-robin across
    ``QFIN_CATEGORIES``: paper *i* is only reachable through category
    ``cats[i % len(cats)]``. A test that asserts on the union therefore only
    passes if every category was actually walked.
    """
    cats = categories or corpus_categories.QFIN_CATEGORIES
    corpus = [_fake_paper(i) for i in range(total)]
    shards: dict[str, list[dict]] = {c: [] for c in cats}
    for i, paper in enumerate(corpus):
        shards[cats[i % len(cats)]].append(paper)

    def fetch_batch(search_query: str, start: int, max_results: int):
        if calls is not None:
            calls.append((search_query, start, max_results))
        shard = shards.get(search_query.removeprefix("cat:"), [])
        return shard[start : start + max_results], len(shard)

    return fetch_batch


def _middle_band(total: int) -> list[dict]:
    """Papers occupying pages 2-of-3 of every category shard.

    ``_paged_feed`` shards round-robin, so paper *i* sits at position
    ``i // len(cats)`` inside shard ``cats[i % len(cats)]``. Selecting shard
    positions 200-399 puts a *whole page* of duplicates in the middle of every
    category walk — the exact shape a resumed harvest meets on the live API,
    and the shape the pre-#1635 zero-new-page stop terminated on.
    """
    cats = len(corpus_categories.QFIN_CATEGORIES)
    return [_fake_paper(i) for i in range(total) if 200 <= i // cats < 400]


def _run_bulk(bulk, out: Path, argv: list[str], fetch_batch, monkeypatch) -> list[dict]:
    monkeypatch.setattr(bulk, "fetch_batch", fetch_batch)
    monkeypatch.setattr("sys.argv", ["bulk_ingest_arxiv.py", "--output", str(out), *argv])
    bulk.main()
    return [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line.strip()]


# ── 1. The harvest ceiling ─────────────────────────────────────


class TestHarvestIsUncapped:
    def test_max_defaults_to_unbounded(self):
        """``--max`` must default to None. ``default=10000`` was the ceiling.

        Read out of the real script's source rather than a re-declared parser,
        so the assertion cannot pass against a copy of the argument.
        """
        tree = ast.parse(_BULK_SCRIPT.read_text(encoding="utf-8"))
        defaults = [
            kw.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "--max"
            for kw in node.keywords
            if kw.arg == "default"
        ]
        assert defaults, "--max argument not found"
        assert all(isinstance(d, ast.Constant) and d.value is None for d in defaults), (
            "--max must default to None (unbounded); a numeric default is a harvest ceiling"
        )

    def test_unbounded_run_drains_the_full_result_set(self, bulk, tmp_path, monkeypatch):
        """The whole point of #1635: no --max ⇒ walk to arXiv's totalResults."""
        out = tmp_path / "manifest.jsonl"
        rows = _run_bulk(bulk, out, [], _paged_feed(1000), monkeypatch)
        assert len(rows) == 1000

    def test_resumed_run_does_not_stop_at_the_already_harvested_region(self, bulk, tmp_path, monkeypatch):
        """The defect that actually preserved the 10,000 ceiling.

        The feed is ordered by submission date, so a resumed harvest walks back
        into the region the existing manifest already covers. The pre-#1635
        "stop at the first page with no new papers" rule fires *there* — long
        before the older tail. Seeded here with only the middle page of each
        category shard already present: page 1 is all-new, page 2 is
        all-duplicate, and page 3 is reachable only by continuing past it.

        Revert demo: restore the unconditional
        ``if batch_new == 0 and start > 0: break`` and this drops to 3,200 rows.
        """
        out = tmp_path / "manifest.jsonl"
        preexisting = _middle_band(4000)
        assert len(preexisting) == 1600
        out.write_text("\n".join(json.dumps(p) for p in preexisting) + "\n", encoding="utf-8")

        rows = _run_bulk(bulk, out, [], _paged_feed(4000), monkeypatch)
        assert len(rows) == 4000, "harvest stopped inside the already-covered region"

    def test_stop_when_caught_up_flag_restores_the_cheap_top_up(self, bulk, tmp_path, monkeypatch):
        """The old behaviour survives, but only as an explicit opt-in.

        Same fixture as the test above: the flag reproduces the pre-#1635 stop,
        which is the point — it is now a documented trade-off, not the default.
        """
        out = tmp_path / "manifest.jsonl"
        preexisting = _middle_band(4000)
        out.write_text("\n".join(json.dumps(p) for p in preexisting) + "\n", encoding="utf-8")

        rows = _run_bulk(bulk, out, ["--stop-when-caught-up"], _paged_feed(4000), monkeypatch)
        # 1600 carried over + the first (all-new) page of each of the 8 shards.
        assert len(rows) == 1600 + 8 * 200, "top-up mode should stop at the first all-duplicate page"

    def test_max_still_caps_a_smoke_run(self, bulk, tmp_path, monkeypatch):
        """``--max`` survives as an opt-in ceiling for smoke runs."""
        out = tmp_path / "manifest.jsonl"
        calls: list = []
        rows = _run_bulk(bulk, out, ["--max", "150"], _paged_feed(1000, calls=calls), monkeypatch)
        # 8 shards of 125; the cap trips partway through the second category.
        assert 150 <= len(rows) < 1000
        assert len({q for q, _s, _m in calls}) < len(corpus_categories.QFIN_CATEGORIES), (
            "a capped smoke run must stop before walking every category"
        )

    def test_max_results_stays_an_int_when_unbounded(self, bulk, tmp_path, monkeypatch):
        """``remaining`` is ``inf`` with no --max; the URL must never see it.

        Revert demo: drop the ``int(...)`` coercion and pass a float
        ``remaining`` and ``max_results`` renders as ``200.0``/``inf`` — arXiv
        answers HTTP 400.
        """
        out = tmp_path / "manifest.jsonl"
        calls: list = []
        _run_bulk(bulk, out, [], _paged_feed(600, calls=calls), monkeypatch)
        assert calls
        for _query, _start, max_results in calls:
            assert isinstance(max_results, int), f"max_results was {max_results!r}"
            assert max_results == bulk.BATCH_SIZE

    def test_deep_pagination_wall_stops_loudly_never_silently(self, bulk, tmp_path, monkeypatch, caplog):
        """arXiv 500s at ``start >= 10000``; an over-large query must say so.

        Fail-soft is wrong here: a query with more results than the API will
        paginate to produces a *short* harvest that looks complete. The correct
        degraded state is a loud, logged absence.

        Guard demo: delete the ``start >= ARXIV_DEEP_PAGE_LIMIT`` branch and
        this run finishes with 8,000 rows and no ERROR line — a silent
        truncation dressed up as a full harvest.
        """
        monkeypatch.setattr(bulk, "ARXIV_DEEP_PAGE_LIMIT", 400)
        out = tmp_path / "manifest.jsonl"
        with caplog.at_level(logging.ERROR):
            rows = _run_bulk(bulk, out, [], _paged_feed(8000), monkeypatch)
        assert "INCOMPLETE" in caplog.text, "an unreachable tail was truncated silently"
        assert len(rows) == 8 * 400, "each category should stop at the pagination wall"

    def test_transient_empty_page_is_retried_not_treated_as_exhaustion(self, bulk, tmp_path, monkeypatch):
        """One blank deep-pagination page must not silently truncate the harvest."""
        out = tmp_path / "manifest.jsonl"
        real = _paged_feed(1600)
        state = {"blanked": False}

        def flaky(search_query: str, start: int, max_results: int):
            if start == 200 and not state["blanked"]:
                state["blanked"] = True
                return [], 200
            return real(search_query, start, max_results)

        rows = _run_bulk(bulk, out, [], flaky, monkeypatch)
        assert len(rows) == 1600


# ── 2. One canonical category list ─────────────────────────────


class TestCanonicalCategories:
    def test_exactly_one_qfin_categories_literal_in_the_tree(self):
        """Acceptance: one literal assignment across backend/ + scripts/.

        Guard demo: add ``QFIN_CATEGORIES = ("q-fin.PM",)`` to any module under
        those roots and this fails with both paths listed.
        """
        roots = [_REPO_ROOT / "backend", _REPO_ROOT / "scripts"]
        literals: list[str] = []
        for root in roots:
            for path in root.rglob("*.py"):
                try:
                    tree = ast.parse(path.read_text(encoding="utf-8"))
                except (SyntaxError, UnicodeDecodeError):
                    continue
                for node in tree.body:  # module level only
                    targets = []
                    if isinstance(node, ast.Assign):
                        targets = node.targets
                        value = node.value
                    elif isinstance(node, ast.AnnAssign):
                        targets = [node.target]
                        value = node.value
                    else:
                        continue
                    names = {t.id for t in targets if isinstance(t, ast.Name)}
                    if "QFIN_CATEGORIES" not in names:
                        continue
                    # An import re-export is not a literal; a list/tuple display is.
                    if isinstance(value, ast.List | ast.Tuple):
                        literals.append(str(path.relative_to(_REPO_ROOT)))
        assert literals == ["backend/archimedes/services/corpus_categories.py"], (
            f"expected exactly one QFIN_CATEGORIES literal, found: {literals}"
        )

    def test_every_consumer_imports_the_canonical_tuple(self, bulk):
        from archimedes.services import arxiv_corpus, corpus_service

        canonical = corpus_categories.QFIN_CATEGORIES
        assert corpus_service.QFIN_CATEGORIES is canonical
        assert arxiv_corpus.QFIN_CATEGORIES is canonical
        assert bulk.QFIN_CATEGORIES is canonical

    def test_retired_q_fin_ec_is_not_a_harvest_term(self, bulk):
        """``q-fin.EC`` is aliased to ``econ.GN`` and returns 0 — pure noise.

        Guard demo: re-add ``"q-fin.EC"`` to the canonical tuple and this fails.
        """
        assert "q-fin.EC" not in corpus_categories.QFIN_CATEGORIES
        assert len(corpus_categories.QFIN_CATEGORIES) == 8

    def test_named_source_files_are_free_of_the_retired_term(self):
        """The literal acceptance grep, as a test."""
        for rel in (
            "scripts/bulk_ingest_arxiv.py",
            "backend/archimedes/services/corpus_service.py",
        ):
            body = (_REPO_ROOT / rel).read_text(encoding="utf-8")
            assert "q-fin.EC" not in body, f"{rel} still references the retired category"


# ── 3. The frozen manifest schema ──────────────────────────────


class TestManifestSchema:
    def test_new_rows_are_written_with_all_thirteen_keys(self, bulk, tmp_path, monkeypatch):
        out = tmp_path / "manifest.jsonl"
        rows = _run_bulk(bulk, out, [], _paged_feed(20), monkeypatch)
        assert rows
        for row in rows:
            assert set(row) == FROZEN_KEYS, f"row {row.get('arxiv_id')} keys: {sorted(row)}"

    def test_legacy_rows_are_normalized_on_write(self, bulk, tmp_path, monkeypatch):
        """The 8,000 committed rows missing the 3 cache keys get repaired.

        Revert demo: drop the ``normalize_row`` call from the write path and
        the carried-over row comes back out with 10 keys.
        """
        out = tmp_path / "manifest.jsonl"
        legacy = _fake_paper(500)  # 10-key shape, no pdf_path/text_path/fetched_at
        assert not set(legacy) >= FROZEN_KEYS
        out.write_text(json.dumps(legacy) + "\n", encoding="utf-8")

        rows = _run_bulk(bulk, out, [], _paged_feed(600), monkeypatch)
        carried = next(r for r in rows if r["arxiv_id"] == legacy["arxiv_id"])
        assert set(carried) == FROZEN_KEYS
        assert carried["pdf_path"] == f"data/corpus/pdfs/{legacy['arxiv_id']}.pdf"
        assert carried["text_path"] == f"data/corpus/text/{legacy['arxiv_id']}.txt"
        assert carried["fetched_at"]

    def test_existing_provenance_is_preserved_not_overwritten(self, bulk):
        row = bulk.normalize_row(
            {"arxiv_id": "2401.00001", "fetched_at": "2020-01-01T00:00:00Z", "pdf_sha256": "abc"},
            fetched_at="2026-08-31T00:00:00Z",
        )
        assert row["fetched_at"] == "2020-01-01T00:00:00Z"
        assert row["pdf_sha256"] == "abc"

    def test_second_run_is_idempotent(self, bulk, tmp_path, monkeypatch):
        out = tmp_path / "manifest.jsonl"
        first = _run_bulk(bulk, out, [], _paged_feed(600), monkeypatch)
        second = _run_bulk(bulk, out, [], _paged_feed(600), monkeypatch)
        assert len(first) == len(second) == 600
        assert [r["arxiv_id"] for r in first] == [r["arxiv_id"] for r in second]

    @pytest.mark.skipif(not _MANIFEST.exists(), reason="committed manifest not present")
    def test_committed_manifest_carries_the_frozen_schema_on_every_row(self):
        """The #1635 acceptance criterion, as a standing guard.

        Guard demo: delete ``pdf_path`` from any line of
        ``data/corpus/manifest.jsonl`` and this fails naming that arxiv_id.
        """
        missing: list[str] = []
        with _MANIFEST.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                if not set(row) >= FROZEN_KEYS:
                    missing.append(str(row.get("arxiv_id")))
                    if len(missing) >= 5:
                        break
        assert not missing, f"manifest rows missing frozen keys: {missing}"

    @pytest.mark.skipif(not _MANIFEST.exists(), reason="committed manifest not present")
    def test_committed_manifest_has_no_retired_primary_category(self):
        with _MANIFEST.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and json.loads(line).get("primary_category") == "q-fin.EC":
                    pytest.fail("manifest carries a q-fin.EC primary category")


# ── 4. Intake can actually grow the corpus ─────────────────────


_ATOM_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <opensearch:totalResults xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">{total}</opensearch:totalResults>
  {entries}
</feed>"""

_ENTRY_TEMPLATE = """<entry>
    <id>http://arxiv.org/abs/{aid}v1</id>
    <title>Paper {aid}</title>
    <summary>Abstract {aid}</summary>
    <published>2026-01-01T00:00:00Z</published>
    <updated>2026-01-01T00:00:00Z</updated>
    <author><name>Ada Lovelace</name></author>
    <category term="q-fin.PM"/>
    <arxiv:primary_category term="q-fin.PM"/>
  </entry>"""


class _FakeResponse:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self):
        return None


class _FakeArxiv:
    """Serves a paginated Atom feed and records every URL requested."""

    def __init__(self, total: int):
        self.total = total
        self.urls: list[str] = []

    def get(self, url: str, **_kwargs):
        self.urls.append(url)
        start, page_size = 0, 200
        for part in url.split("&"):
            if part.startswith("start="):
                start = int(part.split("=", 1)[1])
            elif part.startswith("max_results="):
                page_size = int(part.split("=", 1)[1])
        ids = [f"2600.{i:05d}" for i in range(start, min(start + page_size, self.total))]
        entries = "\n  ".join(_ENTRY_TEMPLATE.format(aid=aid) for aid in ids)
        return _FakeResponse(_ATOM_TEMPLATE.format(total=self.total, entries=entries))


class _CtxSession:
    def __init__(self, session):
        self._session = session

    def __enter__(self):
        return self._session

    def __exit__(self, *_args):
        return None


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


@pytest.fixture
def corpus_svc(monkeypatch, session):
    import httpx
    from archimedes.services import corpus_service

    monkeypatch.setattr(corpus_service, "get_session", lambda: _CtxSession(session))
    monkeypatch.setattr(corpus_service.time, "sleep", lambda _s: None)
    monkeypatch.setattr(corpus_service, "_INTAKE_PAGE_DELAY_SECONDS", 0)
    return corpus_service, httpx


def _seed(session, count: int, *, offset: int = 0) -> None:
    now = datetime.now(UTC)
    for i in range(offset, offset + count):
        session.add(
            PaperRecord(
                arxiv_id=f"2600.{i:05d}",
                title=f"Paper {i}",
                authors="[]",
                abstract="",
                primary_category="q-fin.PM",
                categories='["q-fin.PM"]',
                published="2026-01-01",
                updated="2026-01-01",
                source="seed",
                ingested_at=now,
            )
        )
    session.commit()


class TestIntakePagination:
    def test_query_url_carries_a_start_offset(self, corpus_svc):
        corpus_service, _ = corpus_svc
        url = corpus_service.intake_query_url(400, 200)
        assert "start=400" in url
        assert "max_results=200" in url
        assert "q-fin.EC" not in url

    def test_intake_pages_past_the_first_two_hundred(self, corpus_svc, session, monkeypatch):
        """The blocker in #1635: no ``start=`` ⇒ intake inserts ~0 forever.

        The DB already holds the newest 200 papers (as it would after a seed).
        Under the pre-#1635 URL every request returns exactly those 200, so
        ``inserted`` is 0 and the corpus can never grow.

        Revert demo: drop ``&start={start}`` from ``intake_query_url`` and this
        asserts ``0 > 0``.
        """
        corpus_service, httpx = corpus_svc
        _seed(session, 200)
        fake = _FakeArxiv(total=1000)
        monkeypatch.setattr(httpx, "get", fake.get)

        inserted = corpus_service.intake_from_arxiv(max_results=500)

        assert inserted == 500, f"intake inserted {inserted}"
        assert any("start=200" in u for u in fake.urls), f"never paged past the first page: {fake.urls}"
        assert session.query(PaperRecord).count() == 700

    def test_intake_stops_loudly_at_the_deep_pagination_wall(self, corpus_svc, session, monkeypatch, caplog):
        """Same fail-loud contract as the bulk harvester's wall.

        Guard demo: delete the ``start >= _ARXIV_DEEP_PAGE_LIMIT`` branch and
        intake silently returns a short count instead of logging INCOMPLETE.
        """
        corpus_service, httpx = corpus_svc
        monkeypatch.setattr(corpus_service, "_ARXIV_DEEP_PAGE_LIMIT", 400)
        fake = _FakeArxiv(total=10_000)
        monkeypatch.setattr(httpx, "get", fake.get)

        with caplog.at_level(logging.ERROR):
            inserted = corpus_service.intake_from_arxiv(max_results=1000)
        assert inserted == 400
        assert "INCOMPLETE" in caplog.text

    def test_intake_stops_when_caught_up(self, corpus_svc, session, monkeypatch):
        corpus_service, httpx = corpus_svc
        _seed(session, 400)
        fake = _FakeArxiv(total=400)
        monkeypatch.setattr(httpx, "get", fake.get)

        assert corpus_service.intake_from_arxiv(max_results=500) == 0
        assert session.query(PaperRecord).count() == 400

    def test_intake_returns_zero_without_stamping_meta_when_arxiv_is_down(self, corpus_svc, session, monkeypatch):
        corpus_service, httpx = corpus_svc

        def boom(*_a, **_k):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(httpx, "get", boom)
        assert corpus_service.intake_from_arxiv(max_results=100) == 0

    def test_intake_respects_corpus_max_headroom(self, corpus_svc, session, monkeypatch):
        corpus_service, httpx = corpus_svc
        _seed(session, 10)
        monkeypatch.setattr(corpus_service, "CORPUS_MAX", 210)
        fake = _FakeArxiv(total=1000)
        monkeypatch.setattr(httpx, "get", fake.get)

        # headroom = 210 - 10 already stored = 200, and the first 10 are dups.
        inserted = corpus_service.intake_from_arxiv()
        assert inserted == 200
        assert session.query(PaperRecord).count() == 210


class TestCorpusMaxDefault:
    def test_default_is_lifted_to_twenty_five_thousand(self, monkeypatch):
        """Cold environments must match prod's env override.

        Revert demo: restore ``os.getenv("CORPUS_MAX", "2000")`` and this fails.
        """
        monkeypatch.delenv("CORPUS_MAX", raising=False)
        from archimedes.services import corpus_service

        reloaded = importlib.reload(corpus_service)
        try:
            assert reloaded.CORPUS_MAX == 25000
        finally:
            importlib.reload(corpus_service)

    def test_env_override_still_wins(self, monkeypatch):
        monkeypatch.setenv("CORPUS_MAX", "777")
        from archimedes.services import corpus_service

        reloaded = importlib.reload(corpus_service)
        try:
            assert reloaded.CORPUS_MAX == 777
        finally:
            monkeypatch.delenv("CORPUS_MAX", raising=False)
            importlib.reload(corpus_service)


class TestRerankCapUntouched:
    def test_rerank_candidate_cap_is_still_150(self):
        """#1635 anti-goal: a bigger corpus must not move the honesty cap."""
        from archimedes.services.paper_rag import rerank_candidate_cap

        assert rerank_candidate_cap() == 150
