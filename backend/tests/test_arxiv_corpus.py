"""Tests for the arXiv q-fin corpus scraper.

No live network: the ``arxiv`` search and the PDF downloader are injected as
deterministic fakes. Covers result parsing, the frozen manifest schema,
sha256 content-addressed PDF caching (incl. idempotent re-runs), dedupe by
bare arxiv_id, the recency trim, the cross-list q-fin relevance filter, and
defensive behaviour when a PDF download fails (row still emitted, sha null).
"""

from __future__ import annotations

import hashlib
import inspect
import json
import time
from datetime import UTC, datetime

import pytest
from archimedes.services.arxiv_corpus import (
    _PDF_DOWNLOAD_DELAY,
    CorpusPaper,
    _bare_id,
    _dedupe_and_trim,
    _is_qfin_relevant,
    build_corpus,
)

# ── Fakes ───────────────────────────────────────────────────────


def _mk_paper(
    arxiv_id: str,
    *,
    year: int = 2024,
    month: int = 1,
    day: int = 1,
    primary: str = "q-fin.PM",
    categories: list[str] | None = None,
    pdf_url: str | None = None,
) -> CorpusPaper:
    dt = datetime(year, month, day, tzinfo=UTC)
    return CorpusPaper(
        arxiv_id=arxiv_id,
        title=f"Paper {arxiv_id}",
        authors=["Ada Lovelace", "Carl Gauss"],
        primary_category=primary,
        categories=categories or [primary],
        published=dt.date().isoformat(),
        updated=dt.date().isoformat(),
        abstract=f"Abstract for {arxiv_id}.",
        pdf_url=pdf_url if pdf_url is not None else f"https://arxiv.org/pdf/{arxiv_id}",
        published_dt=dt,
    )


_FAKE_PDF = b"%PDF-1.4 fake bytes for testing"


def _fake_downloader(url: str) -> bytes:
    return _FAKE_PDF


def _failing_downloader(url: str) -> bytes:
    raise RuntimeError("simulated 404 / throttle")


# ── _bare_id ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2401.12345v3", "2401.12345"),
        ("2401.12345", "2401.12345"),
        ("2401.12345v1", "2401.12345"),
        ("q-fin.PM/0703001v2", "q-fin.PM/0703001"),
        ("2401.12345v", "2401.12345v"),  # trailing 'v' w/ no digit: leave it
    ],
)
def test_bare_id_strips_version(raw: str, expected: str) -> None:
    assert _bare_id(raw) == expected


# ── q-fin relevance filter ──────────────────────────────────────


def test_core_qfin_primary_is_relevant() -> None:
    assert _is_qfin_relevant("q-fin.PM", ["q-fin.PM"])


def test_cross_list_with_qfin_cotag_is_relevant() -> None:
    assert _is_qfin_relevant("cs.LG", ["cs.LG", "q-fin.TR"])


def test_pure_ml_without_qfin_is_filtered() -> None:
    assert not _is_qfin_relevant("cs.LG", ["cs.LG", "stat.ML"])


# ── dedupe + recency trim ───────────────────────────────────────


def test_dedupe_by_bare_id_keeps_first() -> None:
    a1 = _mk_paper("2401.00001", year=2024)
    a1_dup = _mk_paper("2401.00001", year=2020)
    b = _mk_paper("2401.00002", year=2023)
    kept = _dedupe_and_trim([a1, a1_dup, b], 10)
    ids = [p.arxiv_id for p in kept]
    assert ids.count("2401.00001") == 1
    assert set(ids) == {"2401.00001", "2401.00002"}


def test_recency_trim_keeps_newest() -> None:
    old = _mk_paper("2001.00001", year=2020)
    mid = _mk_paper("2201.00001", year=2022)
    new = _mk_paper("2501.00001", year=2025)
    kept = _dedupe_and_trim([old, mid, new], 2)
    assert [p.arxiv_id for p in kept] == ["2501.00001", "2201.00001"]
    assert len(kept) == 2


def test_recency_sort_is_descending_by_published_date() -> None:
    papers = [
        _mk_paper("2401.00001", year=2024, month=3),
        _mk_paper("2401.00002", year=2024, month=6),
        _mk_paper("2401.00003", year=2024, month=1),
    ]
    kept = _dedupe_and_trim(papers, 10)
    assert [p.arxiv_id for p in kept] == [
        "2401.00002",
        "2401.00001",
        "2401.00003",
    ]


# ── build_corpus: schema, caching, manifest ─────────────────────

_FROZEN_KEYS = [
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
]


def _search_factory(papers: list[CorpusPaper]):
    def _search(categories, limit):
        return list(papers)

    return _search


def test_manifest_schema_is_frozen(tmp_path) -> None:
    papers = [_mk_paper("2401.00001"), _mk_paper("2401.00002")]
    out = tmp_path / "manifest.jsonl"
    rows = build_corpus(
        max_papers=10,
        out_path=out,
        pdf_dir=tmp_path / "pdfs",
        text_dir=tmp_path / "text",
        search=_search_factory(papers),
        pdf_downloader=_fake_downloader,
        delay_seconds=0,
    )
    assert len(rows) == 2
    for row in rows:
        assert list(row.keys()) == _FROZEN_KEYS
    # manifest file is one JSON object per line, parseable
    lines = out.read_text().strip().splitlines()
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert {p["arxiv_id"] for p in parsed} == {"2401.00001", "2401.00002"}


def test_pdf_sha256_is_content_addressed(tmp_path) -> None:
    papers = [_mk_paper("2401.00001")]
    rows = build_corpus(
        max_papers=10,
        out_path=tmp_path / "m.jsonl",
        pdf_dir=tmp_path / "pdfs",
        text_dir=tmp_path / "text",
        search=_search_factory(papers),
        pdf_downloader=_fake_downloader,
        delay_seconds=0,
    )
    expected_sha = hashlib.sha256(_FAKE_PDF).hexdigest()
    assert rows[0]["pdf_sha256"] == expected_sha
    pdf_file = tmp_path / "pdfs" / "2401.00001.pdf"
    assert pdf_file.exists()
    assert pdf_file.read_bytes() == _FAKE_PDF
    # paths are repo-root-relative + named by bare id
    assert rows[0]["pdf_path"] == "data/corpus/pdfs/2401.00001.pdf"
    assert rows[0]["text_path"] == "data/corpus/text/2401.00001.txt"


def test_cache_is_idempotent_on_rerun(tmp_path) -> None:
    papers = [_mk_paper("2401.00001")]
    call_count = {"n": 0}

    def _counting_downloader(url: str) -> bytes:
        call_count["n"] += 1
        return _FAKE_PDF

    kwargs = {
        "max_papers": 10,
        "out_path": tmp_path / "m.jsonl",
        "pdf_dir": tmp_path / "pdfs",
        "text_dir": tmp_path / "text",
        "search": _search_factory(papers),
        "pdf_downloader": _counting_downloader,
        "delay_seconds": 0,
    }
    build_corpus(**kwargs)
    build_corpus(**kwargs)
    # second run reuses the cached PDF: downloader hit exactly once
    assert call_count["n"] == 1


def test_failed_pdf_still_emits_metadata_row(tmp_path) -> None:
    papers = [_mk_paper("2401.00001"), _mk_paper("2401.00002")]
    rows = build_corpus(
        max_papers=10,
        out_path=tmp_path / "m.jsonl",
        pdf_dir=tmp_path / "pdfs",
        text_dir=tmp_path / "text",
        search=_search_factory(papers),
        pdf_downloader=_failing_downloader,
        delay_seconds=0,
    )
    # metadata-complete for the full N even though every PDF failed
    assert len(rows) == 2
    for row in rows:
        assert row["pdf_sha256"] is None
        assert row["title"]
        assert row["abstract"]
        assert row["authors"] == ["Ada Lovelace", "Carl Gauss"]
        # paths are still named deterministically
        assert row["pdf_path"].endswith(f"{row['arxiv_id']}.pdf")
        assert row["text_path"].endswith(f"{row['arxiv_id']}.txt")


def test_no_pdfs_mode_skips_download(tmp_path) -> None:
    papers = [_mk_paper("2401.00001")]
    rows = build_corpus(
        max_papers=10,
        out_path=tmp_path / "m.jsonl",
        pdf_dir=tmp_path / "pdfs",
        text_dir=tmp_path / "text",
        search=_search_factory(papers),
        pdf_downloader=_failing_downloader,
        delay_seconds=0,
        fetch_pdfs=False,
    )
    assert rows[0]["pdf_sha256"] is None
    assert not (tmp_path / "pdfs" / "2401.00001.pdf").exists()


def test_build_corpus_dedupes_and_trims_end_to_end(tmp_path) -> None:
    papers = [
        _mk_paper("2501.00001", year=2025),
        _mk_paper("2501.00001", year=2025),  # dup
        _mk_paper("2401.00001", year=2024),
        _mk_paper("2301.00001", year=2023),
    ]
    rows = build_corpus(
        max_papers=2,
        out_path=tmp_path / "m.jsonl",
        pdf_dir=tmp_path / "pdfs",
        text_dir=tmp_path / "text",
        search=_search_factory(papers),
        pdf_downloader=_fake_downloader,
        delay_seconds=0,
    )
    assert len(rows) == 2
    assert [r["arxiv_id"] for r in rows] == ["2501.00001", "2401.00001"]


# ── Polite-delay seam (P1-2) ────────────────────────────────────


def test_polite_pdf_delay_default_is_three_seconds() -> None:
    """A live harvest still waits arXiv's guided 3.0s between PDFs.

    The delay is injectable only so offline tests can pass 0; the *production*
    default must not drift. Both halves are pinned: the module constant and the
    ``build_corpus`` signature default that is bound to it.

    MUTATION: set ``_PDF_DOWNLOAD_DELAY = 0.0``, or change the
    ``delay_seconds`` default in ``build_corpus`` to anything but the
    constant, and this fails — that is a real harvest hammering arXiv.
    """
    assert _PDF_DOWNLOAD_DELAY == 3.0
    default = inspect.signature(build_corpus).parameters["delay_seconds"].default
    assert default == _PDF_DOWNLOAD_DELAY
    assert default == 3.0


def test_build_corpus_honours_the_injected_delay(tmp_path, monkeypatch) -> None:
    """The polite sleep uses the injected delay, and 0 sleeps not at all.

    ``build_corpus`` imports ``time`` inside the loop, so patching
    ``time.sleep`` on the module object is what the call actually resolves to.

    MUTATION: revert the seam (``time.sleep(_PDF_DOWNLOAD_DELAY)`` instead of
    ``time.sleep(delay_seconds)``) and the recorded sleeps become
    ``[3.0, 3.0]`` in the injected case and ``[3.0, 3.0]`` in the zero case —
    i.e. this file pays ~21s of wall clock again.
    """
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: slept.append(seconds))

    papers = [_mk_paper("2401.00001"), _mk_paper("2401.00002")]
    kwargs = {
        "max_papers": 10,
        "out_path": tmp_path / "m.jsonl",
        "pdf_dir": tmp_path / "pdfs",
        "text_dir": tmp_path / "text",
        "search": _search_factory(papers),
        "pdf_downloader": _fake_downloader,
    }

    rows = build_corpus(**kwargs, delay_seconds=0.25)
    # one polite pause per successfully cached PDF, at the injected value
    assert len(rows) == 2
    assert slept == [0.25, 0.25]

    # 0 short-circuits the sleep entirely (cache is warm, sha still non-null,
    # so the delay branch is still reached — it just must not fire)
    slept.clear()
    rows = build_corpus(**kwargs, delay_seconds=0)
    assert len(rows) == 2
    assert all(r["pdf_sha256"] for r in rows)
    assert slept == []
